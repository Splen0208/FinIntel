"""finintel resume：简历导入（文本/PDF/DOCX）、规范化、分段与条目解析、校对修正、删除。

约束（设计 D4/D5 / §7.6/§7.7）：
- 不落库简历原文 raw_text；只存 evidence_text（normalize_evidence_text 脱敏后正文，偏移基准）。
- 手机/邮箱导入时替换为 [已移除联系方式]；文件名疑似姓名经 redact_file_name 处理。
- PDF/DOCX 用环境已装的 pdfplumber / python-docx（lazy import）；扫描件判 failed。
- evidence_text 生成后永不 UPDATE 修改（唯一例外：删除时清空）。
"""
from __future__ import annotations

import io
import os
import re
from pathlib import Path
from typing import Any, Optional

import schemas
import store
from store import (assert_scope, get_conn, get_db_path, get_resume_store_dir,
                   jdumps, jloads, new_id, query_all, query_one, utcnow_iso)

RESUME_MIN_EFFECTIVE_CHARS = 50
REDACTION_NOTE = "手机号与邮箱在导入时已替换为 [已移除联系方式]。"
PROCESSING_NOTE = ("简历正文保存在本机 SQLite（见 db_path）与本地目录（见 resume_store_dir），不会上传第三方；"
                   "提交分析时只发送脱敏后的正文与结构化条目，不含姓名与联系方式。")


class ResumeError(Exception):
    def __init__(self, code: str, message: str, operator_action: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.operator_action = operator_action


# ---------------------------------------------------------------------------
# 文件名脱敏
# ---------------------------------------------------------------------------

def redact_file_name(name: str) -> str:
    """文件名含疑似中文姓名（连续 2~4 个 CJK 字符）时替换，避免姓名落库。"""
    if not name:
        return name
    stem, ext = os.path.splitext(name)
    stem = re.sub(r"[\u4e00-\u9fa5]{2,4}", "[已脱敏]", stem)
    return stem + ext


# ---------------------------------------------------------------------------
# 分段与条目解析
# ---------------------------------------------------------------------------

SECTION_KIND = {
    "基本信息": "basic",
    "教育经历": "education",
    "工作经历": "experience",
    "项目经历": "project",
    "实习经历": "experience",
    "校园经历": "experience",
    "技能": "skill",
    "技能证书": "skill",
    "专业技能": "skill",
    "证书": "skill",
    "自我评价": "summary",
    "个人总结": "summary",
    "个人简介": "summary",
    "求职意向": "other",
    "期望职位": "other",
}

_HEADER_RE = re.compile(r"【([^】\n]+)】")
_PERIOD_IN_LINE_RE = re.compile(r"(\d{4}[.\-/年]\s*\d{1,2}[.\-/月]?\s*[-~—至到]\s*[^ \t\n]{0,20})")


def split_sections(text: str) -> tuple[dict[str, tuple[int, int]], list[str]]:
    """识别 【教育经历】/【工作经历】/【项目经历】/【技能】 等段头。

    返回 (ranges: {kind: (start, end) 半开区间}, warnings)。偏移相对 text。
    """
    ranges: dict[str, tuple[int, int]] = {}
    warnings: list[str] = []
    matches = list(_HEADER_RE.finditer(text))
    for i, m in enumerate(matches):
        name = m.group(1).strip()
        content_start = m.end()
        while content_start < len(text) and text[content_start] in "\r\n":
            content_start += 1
        content_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        e = content_end
        while e > content_start and text[e - 1] in "\r\n":
            e -= 1
        kind = SECTION_KIND.get(name)
        if kind is None:
            kind = "other"
            warnings.append(f"未识别段头【{name}】，已归入 other")
        if e > content_start:
            ranges[kind] = (content_start, e)
    if not ranges:
        warnings.append("未识别到任何【】段头，按整篇启发式解析")
    return ranges, warnings


def _line_spans(text: str, s: int, e: int) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    pos = s
    while pos < e:
        nl = text.find("\n", pos, e)
        end = nl if nl != -1 else e
        ls, le = pos, end
        while ls < le and text[ls] in " \t":
            ls += 1
        while le > ls and text[le - 1] in " \t":
            le -= 1
        if le > ls:
            spans.append((ls, le, text[ls:le]))
        if nl == -1:
            break
        pos = nl + 1
    return spans


def _block_spans(text: str, s: int, e: int) -> list[tuple[int, int, str]]:
    seg = text[s:e]
    spans: list[tuple[int, int, str]] = []
    for m in re.finditer(r"[^\n]+(?:\n[^\n]+)*", seg):
        bs, be = s + m.start(), s + m.end()
        ls, le = bs, be
        while ls < le and text[ls] in " \t\n\r":
            ls += 1
        while le > ls and text[le - 1] in " \t\n\r":
            le -= 1
        if le > ls:
            spans.append((ls, le, text[ls:le]))
    return spans


def _parse_org_title(first_line: str) -> tuple[str, Optional[str], str]:
    """从首行拆出 (period_raw, org, title)。"""
    parts = re.split(r"\s{2,}", first_line.strip())
    period = ""
    org: Optional[str] = None
    title = first_line.strip()
    if parts:
        p0 = parts[0]
        if re.search(r"\d{4}", p0) and re.search(r"[-~—至到]", p0):
            period = p0
            rest = parts[1:]
        else:
            rest = parts
        if rest:
            org = rest[0]
            title = " ".join(rest[1:]) if len(rest) > 1 else first_line.strip()
    return period, org, title


def _extract_period(line: str) -> Optional[str]:
    m = _PERIOD_IN_LINE_RE.search(line)
    return m.group(1) if m else None


def extract_items(text: str, ranges: dict) -> list[dict]:
    """按段落生成 resume_item（source='parser'，start/end 相对 text，可回切）。"""
    items: list[dict] = []
    seq = 0
    order = ("education", "experience", "project", "skill", "summary", "other")
    for kind in order:
        if kind not in ranges:
            continue
        s, e = ranges[kind]
        if kind == "basic":
            continue  # 基本信息含联系方式，不生成条目
        if kind == "education":
            for ls, le, line in _line_spans(text, s, e):
                period = _extract_period(line)
                seq += 1
                items.append(_item(kind, line, period=period, start=ls, end=le, seq=seq))
        elif kind in ("experience", "project"):
            for bs, be, block in _block_spans(text, s, e):
                first_line = block.split("\n", 1)[0]
                period = _extract_period(block)
                if kind == "experience":
                    period, org, title = _parse_org_title(first_line)
                else:
                    org = None
                    title = first_line
                seq += 1
                items.append(_item(kind, title, org=org, period=period, start=bs, end=be, seq=seq))
        elif kind == "skill":
            for ls, le, line in _line_spans(text, s, e):
                seq += 1
                items.append(_item(kind, line, start=ls, end=le, seq=seq))
        elif kind == "summary":
            seq += 1
            items.append(_item(kind, text[s:e], start=s, end=e, seq=seq))
        else:  # other
            for bs, be, block in _block_spans(text, s, e):
                seq += 1
                items.append(_item(kind, block.split("\n", 1)[0], start=bs, end=be, seq=seq))
    return items


def _item(kind: str, title: str, org: Optional[str] = None, period: Optional[str] = None,
          start: Optional[int] = None, end: Optional[int] = None, seq: int = 0) -> dict:
    return {
        "kind": kind, "title": (title or "")[:200], "org": org, "period_raw": period,
        "start": start, "end": end, "confidence": 0.9, "source": "parser", "seq": seq,
    }


# ---------------------------------------------------------------------------
# 文件解析（lazy import）
# ---------------------------------------------------------------------------

def _extract_pdf(data: bytes) -> tuple[str, list[dict], dict]:
    """PDF 提取：保留 page_no 与块级文本；检测无文本层（needs_ocr）。

    返回 (full_text, blocks, meta)。
    blocks: [{block_id, page_no, text}]；meta: {needs_ocr, encrypted, page_count, warnings}。
    """
    import pdfplumber
    blocks: list[dict] = []
    warnings: list[str] = []
    page_count = 0
    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as e:
        # 加密或损坏：pdfplumber 抛出 PasswordRequired / 解析错误
        raise ResumeError("PARSE_FAILED", f"PDF 无法打开（可能加密或损坏）：{type(e).__name__}",
                          "请提供未加密文件或粘贴文本版简历")
    try:
        page_count = len(pdf.pages)
        if page_count > schemas.MAX_PDF_PAGES:
            raise ResumeError("FILE_TOO_LARGE",
                              f"PDF 页数 {page_count} 超过上限 {schemas.MAX_PDF_PAGES}",
                              "请拆分或粘贴文本版简历")
        for i, page in enumerate(pdf.pages):
            t = page.extract_text() or ""
            blocks.append({"block_id": f"p{i + 1}", "page_no": i + 1, "text": t})
        effective = _effective_chars("\n".join(b["text"] for b in blocks))
        if effective == 0:
            # 无文本层：可能为扫描件
            return "", blocks, {"needs_ocr": True, "encrypted": False,
                                "page_count": page_count,
                                "warnings": ["未提取到文本层，疑似扫描件，需 OCR 或粘贴文本"]}
        return "\n".join(b["text"] for b in blocks), blocks, \
            {"needs_ocr": False, "encrypted": False, "page_count": page_count, "warnings": warnings}
    finally:
        try:
            pdf.close()
        except Exception:
            pass


def _extract_docx(data: bytes) -> tuple[str, list[dict], dict]:
    """DOCX 提取：按文档遍历顺序建立 block_id；含 zip 炸弹防护。

    返回 (full_text, blocks, meta)。
    blocks: [{block_id, page_no(None), text}]；meta: {needs_ocr, warnings}。
    DOCX 无稳定页码，page_no 恒为 NULL（不编造）。
    """
    import docx
    import zipfile
    blocks: list[dict] = []
    warnings: list[str] = []
    # zip 炸弹防护：限制条目数与解压体积（§5.1）
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            total = sum(i.file_size for i in zf.infolist())
            if total > schemas.MAX_DOCX_UNZIP_BYTES:
                raise ResumeError("FILE_TOO_LARGE",
                                  f"DOCX 解压体积 {total} 超过上限 {schemas.MAX_DOCX_UNZIP_BYTES}",
                                  "文件疑似异常膨胀，请重新导出")
            if len(zf.infolist()) > schemas.MAX_DOCX_ENTRIES:
                raise ResumeError("FILE_TOO_LARGE",
                                  f"DOCX 条目数 {len(zf.infolist())} 超过上限 {schemas.MAX_DOCX_ENTRIES}",
                                  "文件疑似异常，请重新导出")
            # 路径穿越防护：任何条目名不得以绝对路径或 ../ 开头
            for info in zf.infolist():
                if info.filename.startswith("/") or ".." in info.filename.replace("\\", "/").split("/"):
                    raise ResumeError("PARSE_FAILED", "DOCX 含非法路径条目，已拒绝解析",
                                      "请使用来源可信的文件")
    except zipfile.BadZipFile:
        raise ResumeError("PARSE_FAILED", "DOCX 文件损坏（非有效 zip 容器）", "请重新导出或粘贴文本")
    except ResumeError:
        raise
    try:
        doc = docx.Document(io.BytesIO(data))
    except Exception as e:
        raise ResumeError("PARSE_FAILED", f"DOCX 解析失败：{type(e).__name__}", "请重新导出或粘贴文本")
    idx = 0
    # 按可复现的文档遍历顺序：段落 + 表格（交替按 document body 顺序处理）
    parts: list[tuple[str, str]] = []
    from docx.document import Document as _Doc
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    body = doc.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            if p.text.strip():
                parts.append(("p", p.text))
        elif tag == "tbl":
            tbl = Table(child, doc)
            for row in tbl.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    parts.append(("tbl", " ".join(cells)))
    for kind, text in parts:
        idx += 1
        blocks.append({"block_id": f"d{idx}", "page_no": None, "text": text})
    full = "\n".join(b["text"] for b in blocks)
    if _effective_chars(full) == 0:
        return "", blocks, {"needs_ocr": True, "warnings": ["未提取到文本，可能为图片型 DOCX"]}
    return full, blocks, {"needs_ocr": False, "warnings": warnings}


def _effective_chars(text: str) -> int:
    return len(re.sub(r"\s", "", text))


def _resume_view(row: dict, with_text: bool) -> dict:
    d = {
        "id": row["id"], "label": row["label"], "source_type": row["source_type"],
        "file_name": row["file_name"], "parse_status": row["parse_status"],
        "char_count": row["char_count"], "failure_code": row["failure_code"],
        "failure_message": row["failure_message"], "redaction_note": row["redaction_note"],
        "deleted_at": row["deleted_at"], "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "warnings": jloads(row["parse_warnings"], []),
        "section_ranges": jloads(row["section_ranges"], {}),
        "owner_id": row.get("owner_id", "local"),
        "file_hash": row.get("file_hash"),
        "mime": row.get("mime"),
        "size": row.get("size"),
        "extractor_version": row.get("extractor_version"),
        "needs_ocr": bool(row.get("needs_ocr", 0)),
    }
    if with_text:
        d["evidence_text"] = row["evidence_text"]
    return d


def _items_view(resume_id: str) -> list[dict]:
    return query_all("SELECT * FROM resume_item WHERE resume_id=? ORDER BY seq, created_at", (resume_id,))


# ---------------------------------------------------------------------------
# 导入入口
# ---------------------------------------------------------------------------

def import_text(label: str, text: str, file_name: str = "") -> dict:
    if not label or not label.strip():
        raise ResumeError("INVALID_ANALYSIS", "简历 label 不能为空", "填写简历名称")
    normalized = schemas.normalize_evidence_text(text or "")
    blocks = _split_text_blocks(normalized)
    return _create_resume(label, "text", file_name, normalized, raw=None, blocks=blocks)


def _split_text_blocks(text: str) -> list[dict]:
    """文本来源按空行分块，生成稳定 block_id（供事实提取引用）。"""
    blocks: list[dict] = []
    idx = 0
    for para in re.split(r"\n\s*\n", text):
        if para.strip():
            idx += 1
            blocks.append({"block_id": f"t{idx}", "page_no": None, "text": para.strip()})
    return blocks


def import_file(label: str, file_name: str, data: bytes) -> dict:
    if not label or not label.strip():
        raise ResumeError("INVALID_ANALYSIS", "简历 label 不能为空", "填写简历名称")
    ext = os.path.splitext(file_name or "")[1].lower()
    # 扩展名/大小校验（§5.1：首版不支持 .doc/图片/压缩包/宏文档）
    if ext not in schemas.ALLOWED_RESUME_EXTS:
        raise ResumeError("FILE_UNSUPPORTED",
                          f"不支持的文件类型 {ext or '(无扩展名)'}，仅支持 PDF/DOCX/TXT/MD",
                          "请上传 PDF、DOCX 或粘贴文本")
    if len(data) > schemas.MAX_RESUME_FILE_BYTES:
        raise ResumeError("FILE_TOO_LARGE",
                          f"文件 {len(data)} 字节超过上限 {schemas.MAX_RESUME_FILE_BYTES}",
                          "请精简文件或粘贴文本版简历")
    file_hash = _hash_bytes(data)
    source_type = "docx" if ext == ".docx" else "pdf"
    blocks: list[dict] = []
    meta: dict = {"needs_ocr": False, "warnings": []}
    try:
        if source_type == "pdf":
            text, blocks, meta = _extract_pdf(data)
            extractor_version = "pdfplumber"
        else:
            text, blocks, meta = _extract_docx(data)
            extractor_version = "python-docx"
    except ResumeError:
        raise
    except ImportError:
        return _create_resume(label, source_type, file_name, "", raw=None,
                              failed=True, code="DEPENDENCY_MISSING",
                              message=f"环境缺少 {('pdfplumber' if source_type == 'pdf' else 'python-docx')} 依赖")
    except Exception as e:
        return _create_resume(label, source_type, file_name, "", raw=None,
                              failed=True, code="PARSE_FAILED",
                              message=f"解析失败：{type(e).__name__}；请粘贴文本版简历")
    # 扫描件（无文本层）：单独 needs_ocr 状态，不当作解析成功（R-03）
    if meta.get("needs_ocr"):
        return _create_resume(label, source_type, file_name, "", raw=data,
                              file_hash=file_hash, mime=_mime_for(source_type),
                              size=len(data), extractor_version=extractor_version,
                              needs_ocr=True, blocks=blocks, meta=meta)
    normalized = schemas.normalize_evidence_text(text)
    return _create_resume(label, source_type, file_name, normalized, raw=data,
                          file_hash=file_hash, mime=_mime_for(source_type),
                          size=len(data), extractor_version=extractor_version,
                          needs_ocr=False, blocks=blocks, meta=meta)


def _hash_bytes(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _mime_for(source_type: str) -> str:
    return "application/pdf" if source_type == "pdf" else \
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _create_resume(label: str, source_type: str, file_name: str, text: str,
                   raw: Optional[bytes] = None, failed: bool = False,
                   code: Optional[str] = None, message: Optional[str] = None,
                   owner_id: str = "local", file_hash: Optional[str] = None,
                   mime: Optional[str] = None, size: Optional[int] = None,
                   extractor_version: Optional[str] = None, needs_ocr: bool = False,
                   blocks: Optional[list[dict]] = None, meta: Optional[dict] = None) -> dict:
    conn = get_conn()
    now = utcnow_iso()
    resume_id = new_id("resume")
    effective = _effective_chars(text)
    meta = meta or {}
    warnings = list(meta.get("warnings", []))

    def _insert(parse_status: str, failure_code: Optional[str],
                failure_message: Optional[str], _text: str) -> dict:
        sql = ("INSERT INTO resume (id, label, source_type, file_name, evidence_text, char_count, "
               "parse_status, parse_warnings, failure_code, failure_message, section_ranges, "
               "mapping_version, redaction_note, owner_id, file_hash, mime, size, extractor_version, "
               "needs_ocr, created_at, updated_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
        assert_scope(sql, "jobseeker")
        conn.execute(sql, (resume_id, label.strip(), source_type, redact_file_name(file_name), _text,
                           len(_text), parse_status, jdumps(warnings), failure_code, failure_message,
                           "{}", schemas.MAPPING_VERSION, REDACTION_NOTE, owner_id, file_hash, mime,
                           size, extractor_version, 1 if needs_ocr else 0, now, now))
        conn.commit()
        return {"resume": _resume_view(query_one("SELECT * FROM resume WHERE id=?", (resume_id,)), False),
                "items": []}

    # 超长：不保留原文，直接标记 too_large 并阻断（D4/A3）
    if len(text) > schemas.MAX_RESUME_TEXT_CHARS:
        return _insert("too_large", "RESUME_TOO_LARGE",
                       "简历文本超过 60000 字符，请精简后重试", "")

    # 扫描件（无文本层）：单独 needs_ocr 状态，不当作解析成功（R-03）
    if needs_ocr:
        return _insert("needs_ocr", "NEEDS_OCR",
                       "文件无可提取文本层（疑似扫描件/图片），需 OCR 或粘贴文本版简历", "")

    # 空文本 / 有效字符过少 → failed，阻断（S6）
    if failed or effective < RESUME_MIN_EFFECTIVE_CHARS:
        parse_status = "failed"
        failure_code = code or "RESUME_UNPARSEABLE"
        failure_message = message or "未识别到文本层或文本过短，请粘贴文本版简历"
        return _insert(parse_status, failure_code, failure_message, text)

    # 文件落盘：仅在解析成功时保留原始文件（失败不落盘，降低留存面）
    file_path: Optional[str] = None
    if source_type in ("pdf", "docx") and raw is not None:
        store_dir = get_resume_store_dir()
        store_dir.mkdir(parents=True, exist_ok=True)
        safe_name = redact_file_name(file_name) or f"{resume_id}.{source_type}"
        dest = store_dir / f"{resume_id}_{safe_name}"
        dest.write_bytes(raw)
        file_path = str(dest.resolve())

    sql = ("INSERT INTO resume (id, label, source_type, file_name, file_path, evidence_text, char_count, "
           "parse_status, parse_warnings, failure_code, failure_message, section_ranges, "
           "mapping_version, redaction_note, owner_id, file_hash, mime, size, extractor_version, "
           "needs_ocr, created_at, updated_at) "
           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    assert_scope(sql, "jobseeker")
    conn.execute(sql, (resume_id, label.strip(), source_type, redact_file_name(file_name), file_path,
                       text, len(text), "pending", jdumps(warnings), None, None, "{}",
                       schemas.MAPPING_VERSION, REDACTION_NOTE, owner_id, file_hash, mime,
                       size, extractor_version, 0, now, now))
    conn.commit()

    # 写入块级原文（resume_text）：块文本相对 evidence_text 定位（可回切）
    if blocks:
        _save_text_blocks(resume_id, text, blocks)
    return parse_resume(resume_id)


def _save_text_blocks(resume_id: str, evidence_text: str, blocks: list[dict]) -> None:
    """块文本 normalize 后在 evidence_text 中定位，写入 resume_text（不可变版本）。"""
    conn = get_conn()
    now = utcnow_iso()
    seq = 0
    for b in blocks:
        bt = schemas.normalize_evidence_text(b.get("text") or "")
        start: Optional[int] = None
        end: Optional[int] = None
        if bt:
            pos = evidence_text.find(bt)
            if pos >= 0:
                start, end = pos, pos + len(bt)
        ins = ("INSERT INTO resume_text (id, resume_id, text_version, block_id, page_no, start, end, "
               "text, completeness, warnings, seq, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")
        assert_scope(ins, "jobseeker")
        conn.execute(ins, (new_id("rtext"), resume_id, "v1", b.get("block_id", f"b{seq}"),
                           b.get("page_no"), start, end, bt, "complete", "[]", seq, now))
        seq += 1
    conn.commit()


def text_blocks_view(resume_id: str) -> list[dict]:
    """块级原文视图（供前端展示原文 + 来源定位）。"""
    return query_all("SELECT id, block_id, page_no, start, end, text, completeness, warnings, seq "
                     "FROM resume_text WHERE resume_id=? ORDER BY seq", (resume_id,))


def parse_resume(resume_id: str) -> dict:
    row = query_one("SELECT * FROM resume WHERE id=?", (resume_id,))
    if not row:
        raise ResumeError("INVALID_ANALYSIS", "简历不存在")
    if row["deleted_at"]:
        raise ResumeError("TOOL_NOT_ALLOWED", "简历已删除")
    text = row["evidence_text"]
    effective = _effective_chars(text)
    if effective < RESUME_MIN_EFFECTIVE_CHARS:
        conn = get_conn()
        sql = ("UPDATE resume SET parse_status='failed', failure_code='RESUME_UNPARSEABLE', "
               "failure_message='未识别到文本层或文本过短，请粘贴文本版简历', updated_at=? WHERE id=?")
        assert_scope(sql, "jobseeker")
        conn.execute(sql, (utcnow_iso(), resume_id))
        conn.commit()
        row = query_one("SELECT * FROM resume WHERE id=?", (resume_id,))
        return {"resume": _resume_view(row, False), "items": []}

    ranges, warnings = split_sections(text)
    items = extract_items(text, ranges)
    parse_status = "parsed" if not warnings else "partial"
    if not items:
        parse_status = "partial"
        warnings.append("未解析出结构化条目，可在校对页手工补充")

    conn = get_conn()
    now = utcnow_iso()
    # 重建 parser 条目（user 项不得被重新解析覆盖）
    del_sql = "DELETE FROM resume_item WHERE resume_id=? AND source='parser'"
    assert_scope(del_sql, "jobseeker")
    conn.execute(del_sql, (resume_id,))
    for i, it in enumerate(items):
        ins_sql = ("INSERT INTO resume_item (id, resume_id, kind, title, org, period_raw, start, end, "
                   "confidence, source, review_status, review_version, seq, created_at, updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
        assert_scope(ins_sql, "jobseeker")
        conn.execute(ins_sql, (new_id("ritem"), resume_id, it["kind"], it["title"], it["org"],
                               it["period_raw"], it["start"], it["end"], it["confidence"],
                               it["source"], "unreviewed", 0, i, now, now))
    upd_sql = ("UPDATE resume SET parse_status=?, section_ranges=?, parse_warnings=?, char_count=?, "
               "failure_code=NULL, failure_message=NULL, updated_at=? WHERE id=?")
    assert_scope(upd_sql, "jobseeker")
    conn.execute(upd_sql, (parse_status, jdumps(ranges), jdumps(warnings), len(text), now, resume_id))
    conn.commit()
    row = query_one("SELECT * FROM resume WHERE id=?", (resume_id,))
    return {"resume": _resume_view(row, False), "items": _items_view(resume_id)}


def list_resumes() -> list[dict]:
    """不含正文。"""
    return query_all(
        "SELECT id, label, source_type, parse_status, char_count, created_at, deleted_at "
        "FROM resume ORDER BY created_at DESC")


def get_resume(resume_id: str, with_text: bool = True) -> Optional[dict]:
    row = query_one("SELECT * FROM resume WHERE id=?", (resume_id,))
    if not row:
        return None
    return {"resume": _resume_view(row, with_text), "items": _items_view(resume_id),
            "blocks": text_blocks_view(resume_id)}


# ---------------------------------------------------------------------------
# 校对修正 / 删除 / 存储说明
# ---------------------------------------------------------------------------

def replace_items(resume_id: str, items: list[dict], reason: str) -> dict:
    if not reason or not reason.strip():
        raise ResumeError("INVALID_ANALYSIS", "修正必须填写原因（留痕要求）", "填写 reason 后重试")
    resume = query_one("SELECT * FROM resume WHERE id=?", (resume_id,))
    if not resume:
        raise ResumeError("INVALID_ANALYSIS", "简历不存在")
    if resume["deleted_at"]:
        raise ResumeError("TOOL_NOT_ALLOWED", "简历已删除")
    validated: list[schemas.ResumeItemIn] = []
    for it in items:
        try:
            validated.append(schemas.ResumeItemIn(**it))
        except Exception as e:  # pydantic ValidationError
            raise ResumeError("INVALID_ANALYSIS", f"条目 Schema 校验失败：{e}")
    text = resume["evidence_text"]
    for v in validated:
        if v.start is not None and v.end is not None:
            if v.end > len(text) or v.start > v.end:
                raise ResumeError("INVALID_EVIDENCE", f"条目 {v.title!r} 偏移越界：[0,{len(text)})")
        elif v.start is not None and v.start > len(text):
            raise ResumeError("INVALID_EVIDENCE", f"条目 {v.title!r} 偏移越界")

    before_items = _items_view(resume_id)
    before_by_id = {r["id"]: r for r in before_items}
    conn = get_conn()
    now = utcnow_iso()
    kept_ids: set[str] = set()
    seq = 0
    for v in validated:
        if v.id and v.id in before_by_id:
            upd = ("UPDATE resume_item SET kind=?, title=?, org=?, period_raw=?, start=?, end=?, "
                   "confidence=1.0, source='user', review_version=review_version+1, seq=?, updated_at=? "
                   "WHERE id=?")
            assert_scope(upd, "jobseeker")
            conn.execute(upd, (v.kind, v.title, v.org, v.period_raw, v.start, v.end, seq, now, v.id))
            kept_ids.add(v.id)
        else:
            new_item_id = new_id("ritem")
            ins = ("INSERT INTO resume_item (id, resume_id, kind, title, org, period_raw, start, end, "
                   "confidence, source, review_status, review_version, seq, created_at, updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
            assert_scope(ins, "jobseeker")
            conn.execute(ins, (new_item_id, resume_id, v.kind, v.title, v.org, v.period_raw,
                               v.start, v.end, 1.0, "user", "unreviewed", 0, seq, now, now))
            kept_ids.add(new_item_id)
        seq += 1
    for r in before_items:
        if r["id"] not in kept_ids:
            conn.execute("DELETE FROM resume_item WHERE id=?", (r["id"],))
    ev = ("INSERT INTO review_event (id, target_type, target_id, expected_version, action, "
          "before_json, after_json, reason, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)")
    conn.execute(ev, (new_id("rev"), "resume", resume_id, 0, "correct",
                      jdumps(before_items), jdumps(_items_view(resume_id)), reason, "user", now))
    conn.commit()
    return {"items": _items_view(resume_id), "replaced": len(before_items)}


def delete_resume(resume_id: str, confirm: bool) -> dict:
    if not confirm:
        raise ResumeError("INVALID_ANALYSIS", "删除需二次确认（confirm=true）",
                          operator_action="确认删除后重试")
    resume = query_one("SELECT * FROM resume WHERE id=?", (resume_id,))
    if not resume:
        raise ResumeError("INVALID_ANALYSIS", "简历不存在")
    if resume["deleted_at"]:
        return {"deleted": True, "cleared_bytes": 0, "affected_sessions": []}
    cleared_bytes = len((resume["evidence_text"] or "").encode("utf-8"))
    if resume["file_path"]:
        try:
            os.remove(resume["file_path"])
        except OSError:
            pass
    conn = get_conn()
    now = utcnow_iso()
    conn.execute("DELETE FROM resume_item WHERE resume_id=?", (resume_id,))
    upd = ("UPDATE resume SET deleted_at=?, evidence_text='', char_count=0, section_ranges='{}', "
           "parse_warnings='[]', failure_code=NULL, failure_message=NULL, file_path=NULL, updated_at=? "
           "WHERE id=?")
    assert_scope(upd, "jobseeker")
    conn.execute(upd, (now, now, resume_id))
    sessions = query_all("SELECT * FROM jobseeker_session WHERE resume_id=?", (resume_id,))
    affected: list[str] = []
    for s in sessions:
        new_status = "jd_ready" if s["job_target_id"] else "draft"
        s_upd = "UPDATE jobseeker_session SET resume_id=NULL, status=?, updated_at=? WHERE id=?"
        assert_scope(s_upd, "jobseeker")
        conn.execute(s_upd, (new_status, now, s["id"]))
        affected.append(s["id"])
    conn.commit()
    return {"deleted": True, "cleared_bytes": cleared_bytes, "affected_sessions": affected}


def delete_all(confirm: bool) -> dict:
    if not confirm:
        raise ResumeError("INVALID_ANALYSIS", "删除需二次确认（confirm=true）",
                          operator_action="确认删除后重试")
    rows = query_all("SELECT id FROM resume WHERE deleted_at IS NULL")
    deleted = 0
    for r in rows:
        delete_resume(r["id"], True)
        deleted += 1
    return {"deleted": deleted}


def storage_info() -> dict:
    return {
        "resume_store_dir": str(get_resume_store_dir()),
        "db_path": str(get_db_path()),
        "outputs_dir": str(store.OUTPUTS_DIR),
        "processing_note": PROCESSING_NOTE,
        "redaction_note": REDACTION_NOTE,
        "retention_note": "简历仅保存在本机，不做云端同步；删除后正文与文件一并清除，不可恢复。",
        "delete_note": "删除需二次确认；单份删除清空正文并移除上传文件，全部删除清空所有简历。",
    }
