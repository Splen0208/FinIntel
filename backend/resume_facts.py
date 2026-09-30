"""finintel resume_facts：真实模型事实提取（步骤 A）+ 用户核对（步骤 B）。

- 调用 ai_provider 真实模型，把简历文本块抽取为结构化事实（resume_claim），每条带来源引用。
- 引用强校验：quote 必须等于 evidence_text[start:end]，否则该条事实被拒绝，不进入有效事实。
- 事实确认状态与"解析成功/结构校验"分离：unconfirmed/confirmed/needs_input/omitted。
- 模型未配置时抛 AI_NOT_CONFIGURED，绝不回退 mock；仅显式 demo 模式允许规则提取。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import ai_provider
import pack
import schemas
import store
from store import (assert_scope, get_conn, jdumps, jloads, new_id, query_all,
                   query_one, utcnow_iso)

FACTS_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "extract_facts_v1.md"
FACTS_PROMPT_VERSION = "extract_facts_v1"
FACTS_SCHEMA_VERSION = "facts_schema_v1"
FACTS_MODEL_LABEL = "ai-facts-v1"

CLAIM_TYPES = ("education", "experience", "project", "skill", "result", "other")
CONFIRM_STATUSES = ("unconfirmed", "confirmed", "needs_input", "omitted")


class FactsError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False,
                 operator_action: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.operator_action = operator_action


def load_prompt() -> str:
    with open(FACTS_PROMPT_PATH, encoding="utf-8") as f:
        return f.read()


def _resume_with_blocks(resume_id: str) -> dict:
    r = query_one("SELECT * FROM resume WHERE id=?", (resume_id,))
    if not r:
        raise FactsError("INVALID_ANALYSIS", "简历不存在")
    if r["deleted_at"]:
        raise FactsError("TOOL_NOT_ALLOWED", "简历已删除")
    if r["parse_status"] not in ("parsed", "partial"):
        raise FactsError("INVALID_ANALYSIS",
                         f"简历解析状态为 {r['parse_status']}，无法提取事实",
                         False, "先完成文件解析（扫描件请粘贴文本版）")
    blocks = query_all("SELECT block_id, page_no, start, end, text FROM resume_text "
                       "WHERE resume_id=? ORDER BY seq", (resume_id,))
    return {"resume": r, "blocks": blocks}


def _build_input(resume_id: str) -> tuple[str, dict]:
    d = _resume_with_blocks(resume_id)
    text = d["resume"]["evidence_text"]
    lines = [f"【整篇正文（共 {len(text)} 字符，索引为 Unicode code point）】", text, "",
             "【分块（block_id / page_no / 全局偏移）】"]
    for b in d["blocks"]:
        lines.append(f"- block_id={b['block_id']} page_no={b['page_no']} "
                     f"[{b['start']},{b['end']}) :: {b['text']}")
    return "\n".join(lines), d


def extract_facts(resume_id: str) -> dict:
    """调用真实模型提取事实并入库。返回 {claims, rejected, model_label, ...}。"""
    input_text, d = _build_input(resume_id)
    resume = d["resume"]
    text = resume["evidence_text"]

    try:
        raw = ai_provider.chat_json(load_prompt(), input_text, temperature=0.0)
    except ai_provider.AIError as e:
        raise FactsError(e.code, e.message, e.retryable, e.operator_action)

    claims, rejected = _normalize_claims(raw, text)
    # 引用校验：不通过的事实被拒绝，不进入有效事实
    valid, invalid = [], []
    for c in claims:
        if c.get("source_valid"):
            valid.append(c)
        else:
            invalid.append(c)

    # 有界修正一次：引用错误过多时把错误回喂重试（§8.1）
    if valid and len(invalid) > len(valid):
        try:
            retry_note = ("上一次抽取有较多引用无法回切原文，请只保留能逐字定位到原文的事实，"
                          "并确保 quote 与 [start,end) 切片完全一致。")
            raw2 = ai_provider.chat_json(load_prompt(), input_text + "\n\n" + retry_note, temperature=0.0)
            claims2, _ = _normalize_claims(raw2, text)
            valid2 = [c for c in claims2 if c.get("source_valid")]
            if len(valid2) >= len(valid):
                valid = valid2
        except ai_provider.AIError:
            pass  # 重试失败保留首次有效事实

    saved = _save_claims(resume_id, valid, resume)
    return {"claims": saved, "rejected_count": len(invalid),
            "model_label": FACTS_MODEL_LABEL, "prompt_version": FACTS_PROMPT_VERSION,
            "schema_version": FACTS_SCHEMA_VERSION,
            "provider": ai_provider.ai_config()["provider"],
            "model": ai_provider.ai_config()["model"]}


def _normalize_claims(raw: dict, text: str) -> tuple[list[dict], list[dict]]:
    """把模型 JSON 归一为 claim 列表，并做引用切片校验。"""
    claims: list[dict] = []
    rejected: list[dict] = []
    if not isinstance(raw, dict):
        return claims, rejected

    def _add(ctype: str, obj: dict) -> None:
        if not isinstance(obj, dict):
            return
        src = obj.get("source") or {}
        quote = (src.get("quote") or "") if isinstance(src, dict) else ""
        start = src.get("start") if isinstance(src, dict) else None
        end = src.get("end") if isinstance(src, dict) else None
        block_id = src.get("block_id") if isinstance(src, dict) else None
        source_valid = False
        if isinstance(quote, str) and isinstance(start, int) and isinstance(end, int):
            # 位置修正：模型对中文 code point 常有 off-by-N，只修位置、不放宽真实性
            s2, e2, _rep = pack.repair_quote_offsets(quote, text, start, end)
            if 0 <= s2 < e2 <= len(text) and text[s2:e2] == quote:
                start, end = s2, e2
                source_valid = True
        # 提取字段（把 null 归一为 None）
        fields = {k: (None if v is None else v) for k, v in obj.items() if k != "source"}
        statement = _statement_for(ctype, fields)
        c = {"claim_type": ctype, "statement": statement,
             "source_refs": [{"block_id": block_id, "start": start, "end": end, "quote": quote}]
             if source_valid else [], "source_valid": source_valid, "fields": fields}
        if statement:
            (claims if source_valid else rejected).append(c)

    for e in (raw.get("education") or []):
        _add("education", e)
    for e in (raw.get("experiences") or []):
        _add("experience", e)
    for e in (raw.get("projects") or []):
        _add("project", e)
    for e in (raw.get("skills") or []):
        _add("skill", e)
    return claims, rejected


def _statement_for(ctype: str, fields: dict) -> str:
    """把字段组装成一句可展示的事实陈述（不润色，仅拼接原文字段）。"""
    if ctype == "education":
        parts = [fields.get("period_raw"), fields.get("school"), fields.get("degree"),
                 fields.get("major")]
        return "｜".join(str(p) for p in parts if p)
    if ctype == "experience":
        return "｜".join(str(p) for p in [fields.get("period_raw"), fields.get("company"),
                                          fields.get("title")] if p)
    if ctype == "project":
        return "｜".join(str(p) for p in [fields.get("period_raw"), fields.get("name"),
                                          fields.get("role")] if p)
    if ctype == "skill":
        return str(fields.get("name") or "") + (f"（{fields.get('level_raw')}）"
                                                if fields.get("level_raw") else "")
    return str(fields.get("statement") or fields.get("name") or "")


def _save_claims(resume_id: str, claims: list[dict], resume: dict) -> list[dict]:
    conn = get_conn()
    now = utcnow_iso()
    # 只删未确认的旧 claim（已确认的不被覆盖）
    del_sql = "DELETE FROM resume_claim WHERE resume_id=? AND confirmation_status='unconfirmed'"
    assert_scope(del_sql, "jobseeker")
    conn.execute(del_sql, (resume_id,))
    saved: list[dict] = []
    for i, c in enumerate(claims):
        cid = new_id("claim")
        ins = ("INSERT INTO resume_claim (id, resume_id, claim_type, statement, source_refs, "
               "confirmation_status, revision, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)")
        assert_scope(ins, "jobseeker")
        conn.execute(ins, (cid, resume_id, c["claim_type"], c["statement"],
                           jdumps(c["source_refs"]), "unconfirmed", 0, now, now))
        saved.append({"id": cid, "claim_type": c["claim_type"], "statement": c["statement"],
                      "source_refs": c["source_refs"], "confirmation_status": "unconfirmed"})
    conn.commit()
    return saved


def claims_view(resume_id: str) -> list[dict]:
    rows = query_all("SELECT * FROM resume_claim WHERE resume_id=? ORDER BY created_at, id",
                     (resume_id,))
    for r in rows:
        r["source_refs"] = jloads(r["source_refs"], [])
    return rows


def confirm_claims(resume_id: str, claim_ids: list[str], expected_revision: int) -> dict:
    """本人确认一组事实（confirm）。返回新 revision。"""
    if not claim_ids:
        raise FactsError("INVALID_ANALYSIS", "claim_ids 不能为空")
    resume = query_one("SELECT * FROM resume WHERE id=?", (resume_id,))
    if not resume or resume["deleted_at"]:
        raise FactsError("INVALID_ANALYSIS", "简历不存在或已删除")
    conn = get_conn()
    now = utcnow_iso()
    confirmed = 0
    for cid in claim_ids:
        c = query_one("SELECT * FROM resume_claim WHERE id=? AND resume_id=?", (cid, resume_id))
        if not c:
            continue
        if c["revision"] != expected_revision:
            raise FactsError("VERSION_CONFLICT",
                             f"事实 {cid} 版本为 {c['revision']}，与提交的 {expected_revision} 不符",
                             False, "重新读取最新版本后再确认")
        up = ("UPDATE resume_claim SET confirmation_status='confirmed', confirmed_by='local', "
              "confirmed_at=?, revision=revision+1, updated_at=? WHERE id=?")
        assert_scope(up, "jobseeker")
        conn.execute(up, (now, now, cid))
        confirmed += 1
    conn.commit()
    return {"confirmed": confirmed, "revision": expected_revision + 1}


def add_supplement(resume_id: str, question: str, answer: str) -> dict:
    """保存用户补充答复（与模型生成内容分开）。"""
    if not question or not question.strip() or not answer or not answer.strip():
        raise FactsError("INVALID_ANALYSIS", "question 与 answer 不能为空")
    resume = query_one("SELECT * FROM resume WHERE id=?", (resume_id,))
    if not resume or resume["deleted_at"]:
        raise FactsError("INVALID_ANALYSIS", "简历不存在或已删除")
    conn = get_conn()
    now = utcnow_iso()
    sid = new_id("usup")
    ins = ("INSERT INTO user_supplement (id, resume_id, question, answer, revision, created_at) "
           "VALUES (?,?,?,?,?,?)")
    assert_scope(ins, "jobseeker")
    conn.execute(ins, (sid, resume_id, question.strip(), answer.strip(), 0, now))
    conn.commit()
    return {"supplement_id": sid, "question": question.strip(), "answer": answer.strip()}


def supplements_view(resume_id: str) -> list[dict]:
    return query_all("SELECT * FROM user_supplement WHERE resume_id=? ORDER BY created_at", (resume_id,))
