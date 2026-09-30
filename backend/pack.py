"""finintel pack：分析/匹配/准备共用的无状态包原语。

只包含无业务语义的公共函数：批次令牌、幂等提交、提交日志、引用偏移校验。
新增的 matching/prep 与既有 analysis 复用同一套原语；偏移校验（validate_quote /
validate_quotes）为全局唯一实现。

引用偏移语义：半开区间 [start, end)，Python code point 计数，quote == text[start:end]。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

import store
from store import get_conn, jdumps, jloads, new_id, query_one, utcnow_iso


class PackError(Exception):
    """包原语错误。属性与 AnalysisError/MatchError 对齐：code/message/retryable/operator_action。"""

    def __init__(self, code: str, message: str, retryable: bool = False,
                 operator_action: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.operator_action = operator_action


def hash_payload(obj: Any) -> str:
    """对 payload 生成确定性 sha256（JSON sort_keys）。"""
    blob = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def create_batch(payload_kind: str, ref_id: str, refs: dict, prompt_version: str,
                 schema_version: str, chunks: Optional[dict] = None) -> str:
    """创建分析包，返回 token。

    - payload_kind: company_finding / jd_match / prep_plan
    - ref_id: 非研究侧包的会话 id（研究侧为空字符串）
    - refs: 引用对象（研究侧含 run_id；求职侧含 target/resume 等），写入 snapshot_ids 列（JSON）
    - schema_version 存入 analysis_batch.taxonomy_version 列（该列无独立 schema 列，仅作版本标记）
    """
    token = new_id("batch")
    run_id = ""
    if isinstance(refs, dict):
        run_id = refs.get("run_id", "") or ""
    conn = get_conn()
    conn.execute(
        """INSERT INTO analysis_batch (token, run_id, snapshot_ids, prompt_version,
           taxonomy_version, chunks, used, created_at, payload_kind, ref_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (token, run_id, jdumps(refs), prompt_version, schema_version,
         jdumps(chunks or {}), 0, utcnow_iso(), payload_kind, ref_id),
    )
    conn.commit()
    return token


def require_batch(token: str, payload_kind: Optional[str] = None) -> dict:
    """校验包存在、未使用、类型一致；返回批次行。"""
    batch = query_one("SELECT * FROM analysis_batch WHERE token=?", (token,))
    if not batch:
        raise PackError("TOOL_NOT_ALLOWED", "batch_token 无效或不存在", False,
                        "先调用 prepare 获取有效 batch_token")
    if batch["used"]:
        raise PackError("TOOL_NOT_ALLOWED", "batch_token 已使用", False,
                        "每个分析包只能提交一次；重新分析请生成新的分析包")
    if payload_kind and batch["payload_kind"] != payload_kind:
        raise PackError("TOOL_NOT_ALLOWED",
                        f"batch_token 类型不匹配：期望 {payload_kind}，实际 {batch['payload_kind']}",
                        False, "使用对应类型的包提交")
    return batch


def consume_batch(token: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE analysis_batch SET used=1 WHERE token=?", (token,))
    conn.commit()


def idempotent_result(submission_id: str, payload_hash: str) -> Optional[dict]:
    """幂等：相同 submission_id 同 payload 返回旧结果；不同 payload 抛 VERSION_CONFLICT。"""
    prior = query_one("SELECT * FROM submission_log WHERE submission_id=?", (submission_id,))
    if not prior:
        return None
    if prior["payload_hash"] == payload_hash:
        return jloads(prior["result_json"], {})
    raise PackError("VERSION_CONFLICT",
                    f"submission_id={submission_id} 已被不同 payload 使用", False,
                    "更换 submission_id 重新提交")


def record_submission(submission_id: str, batch_token: str, payload_hash: str,
                      result: dict) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO submission_log (submission_id, batch_token, payload_hash, result_json, created_at) VALUES (?,?,?,?,?)",
        (submission_id, batch_token, payload_hash, jdumps(result), utcnow_iso()),
    )
    conn.commit()


def repair_quote_offsets(quote: Optional[str], text: str,
                         start: Any, end: Any) -> tuple[Any, Any, bool]:
    """引用位置修正（真实模型的常见 off-by-N 偏差）。

    原则：**只修位置，不放宽真实性**——
    - 若 (start,end) 切片已逐字等于 quote → 原样返回（不做任何改动）；
    - 否则若 quote 非空且在整个 text 中**唯一出现** → 用该唯一位置替换 start/end；
    - 若 quote 不在 text 中（疑似编造）或多次出现（位置不确定）→ 原样返回，
      交由 validate_quote 严格拒绝，避免"猜位置"制造假引用。

    返回 (start, end, repaired)。repaired=True 表示位置被程序修正过。
    """
    q = quote or ""
    if isinstance(start, int) and isinstance(end, int) \
            and 0 <= start < end <= len(text) and text[start:end] == q:
        return start, end, False
    if not q:
        return start, end, False
    pos = text.find(q)
    if pos < 0:
        return start, end, False
    if text.find(q, pos + 1) >= 0:
        return start, end, False
    return pos, pos + len(q), True


def repair_quotes_in_list(items: list, text: str,
                          quote_key: str = "quote") -> int:
    """对 [{start,end,quote,...}] 列表就地做位置修正，返回修正条数。"""
    fixed = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        s, e, rep = repair_quote_offsets(it.get(quote_key), text, it.get("start"), it.get("end"))
        if rep:
            it["start"], it["end"] = s, e
            fixed += 1
    return fixed


def validate_quote(quote: dict, evidence_text: str, field: str,
                   section_ranges: Optional[dict] = None,
                   forbid_cross_section: bool = False) -> Optional[dict]:
    """单条引用校验。通过返回 None；失败返回 {"field","code","message"}。

    校验顺序：quote 非空 → 0 <= start < end <= len(text) → text[start:end] == quote
             →（可选）section 归属与跨段检查。
    错误信息措辞与 analysis 改造前保持一致（"evidence_text[...] != quote" 等）。
    """
    quote_str = (quote.get("quote") or "").strip()
    start = quote.get("start")
    end = quote.get("end")
    section = quote.get("section")

    if not quote_str:
        return {"field": field + ".quote", "code": "INVALID_EVIDENCE", "message": "空引用无效"}
    if start is None or end is None or start < 0 or end > len(evidence_text):
        return {"field": field, "code": "INVALID_EVIDENCE",
                "message": f"偏移越界：[0,{len(evidence_text)})"}
    if end <= start:
        return {"field": field, "code": "INVALID_EVIDENCE",
                "message": "end 必须大于 start（半开区间 [start,end)）"}
    actual = evidence_text[start:end]
    if actual != quote.get("quote"):
        return {"field": field, "code": "INVALID_EVIDENCE",
                "message": f"evidence_text[{start}:{end}] != quote（实际切片: {actual[:50]!r}）"}

    if forbid_cross_section and section_ranges and section == "responsibility":
        req_range = section_ranges.get("requirement")
        resp_range = section_ranges.get("responsibility")
        if req_range and resp_range:
            if start >= req_range[0] and end <= req_range[1]:
                return {"field": field + ".section", "code": "INVALID_EVIDENCE",
                        "message": "引用区间落在任职要求段，不能作为职责证据"}
            if start < resp_range[0] and end > req_range[1]:
                return {"field": field + ".section", "code": "INVALID_EVIDENCE",
                        "message": "引用区间跨职责/要求边界，无法确认职责事实，进入待核实区"}
    return None


def validate_quotes(quotes: list, texts: dict, field_prefix: str) -> list:
    """批量校验。texts = {"jd": <jd evidence_text>, "resume": <resume evidence_text>}；
    按 quote["side"] 选文本；返回错误列表（空 = 全部通过）。"""
    errors: list = []
    for i, q in enumerate(quotes):
        side = q.get("side", "jd")
        text = texts.get(side, "")
        err = validate_quote(q, text, f"{field_prefix}[{i}]")
        if err:
            errors.append(err)
    return errors
