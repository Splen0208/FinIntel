"""finintel matching：目标 JD 与匹配包链路（prepare/submit/auto）+ 会话管理 + 修正/补充。

复用 pack.* 原语（批次令牌、幂等、引用偏移校验）。内置规则匹配器 model_label 固定
rule-matcher-demo-v1，不得输出 confirmed_gap，也不得输出连续数值评分（R1/R9/§7.5）。

五状态严格分离：direct_evidence / transferable / not_in_resume / confirmed_gap / insufficient；
禁止由"简历没找到证据"自动落到 confirmed_gap（只能到 not_in_resume）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import ai_provider
import analyzer
import pack
import schemas
import store
import targets
from store import (assert_scope, get_conn, jdumps, jloads, new_id, query_all,
                   query_one, utcnow_iso)

MATCH_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "match_v1.md"


class MatchError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False,
                 operator_action: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.operator_action = operator_action


def load_prompt() -> str:
    with open(MATCH_PROMPT_PATH, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 会话管理（F-01/26，§4.3.4）
# ---------------------------------------------------------------------------

def create_session(route: str = "match", industry: Optional[str] = None,
                   role_direction: Optional[str] = None,
                   experience_level: str = "unknown",
                   target_company_ids: Optional[list] = None,
                   background_note: Optional[str] = None,
                   job_target_id: Optional[str] = None,
                   resume_id: Optional[str] = None) -> dict:
    if route not in ("match", "explore"):
        raise MatchError("INVALID_ANALYSIS", "route 必须为 match 或 explore")
    if experience_level not in {e.value for e in schemas.ExperienceLevel}:
        raise MatchError("INVALID_ANALYSIS", "experience_level 取值非法")
    sid = new_id("jss")
    now = utcnow_iso()
    conn = get_conn()
    sql = ("INSERT INTO jobseeker_session (id, mode, route, industry, role_direction, "
           "experience_level, target_company_ids, background_note, job_target_id, resume_id, "
           "status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")
    assert_scope(sql, "jobseeker")
    conn.execute(sql, (sid, "jobseeker", route, industry, role_direction, experience_level,
                       jdumps(target_company_ids or []), background_note, job_target_id, resume_id,
                       "draft", now, now))
    conn.commit()
    return get_session(sid)


def list_sessions() -> list[dict]:
    rows = query_all(
        """SELECT s.*, jt.title, jt.company_display, r.label AS resume_label
           FROM jobseeker_session s
           LEFT JOIN job_target jt ON jt.id = s.job_target_id
           LEFT JOIN resume r ON r.id = s.resume_id
           ORDER BY s.updated_at DESC""")
    for r in rows:
        r["target_company_ids"] = jloads(r["target_company_ids"], [])
        r.pop("background_note", None)  # 列表不含简历正文/背景全文
    return rows


def get_session(session_id: str) -> dict:
    s = query_one("SELECT * FROM jobseeker_session WHERE id=?", (session_id,))
    if not s:
        raise MatchError("INVALID_ANALYSIS", "会话不存在")
    s["target_company_ids"] = jloads(s["target_company_ids"], [])
    job_target = None
    if s["job_target_id"]:
        try:
            t = targets.target_view(s["job_target_id"])
            job_target = {"id": t["id"], "title": t["title"], "company_display": t["company_display"],
                          "source_kind": t["source_kind"], "char_count": t["char_count"]}
        except targets.TargetError:
            job_target = None
    resume = None
    if s["resume_id"]:
        r = query_one("SELECT id, label, source_type, parse_status, char_count, created_at "
                      "FROM resume WHERE id=?", (s["resume_id"],))
        resume = r
    counts = {
        "requirements": query_one("SELECT COUNT(*) AS n FROM jd_requirement WHERE session_id=?", (session_id,))["n"],
        "match_rows": query_one("SELECT COUNT(*) AS n FROM match_row WHERE session_id=?", (session_id,))["n"],
        "focus": query_one("SELECT COUNT(*) AS n FROM interview_focus WHERE session_id=?", (session_id,))["n"],
        "tasks": query_one("SELECT COUNT(*) AS n FROM prep_task WHERE session_id=?", (session_id,))["n"],
    }
    import interview_research
    if job_target and t.get('snapshot_id'):
        snap = query_one('SELECT detail_status,source_url FROM job_snapshot WHERE id=?',(t['snapshot_id'],))
        job_target.update(snap or {})
        job_target['snapshot_id'] = t['snapshot_id']
    return {"session": s, "job_target": job_target, "resume": resume, "counts": counts,
            "research_background": interview_research.materials_view(session_id)}


def update_session(session_id: str, job_target_id: Optional[str] = None,
                   resume_id: Optional[str] = None, industry: Optional[str] = None,
                   role_direction: Optional[str] = None,
                   experience_level: Optional[str] = None,
                   background_note: Optional[str] = None) -> dict:
    s = query_one("SELECT * FROM jobseeker_session WHERE id=?", (session_id,))
    if not s:
        raise MatchError("INVALID_ANALYSIS", "会话不存在")
    conn = get_conn()
    sets, params = [], []
    if job_target_id is not None:
        sets.append("job_target_id=?")
        params.append(job_target_id or None)
    if resume_id is not None:
        sets.append("resume_id=?")
        params.append(resume_id or None)
    for key, val in (("industry", industry), ("role_direction", role_direction),
                     ("experience_level", experience_level), ("background_note", background_note)):
        if val is not None:
            sets.append(f"{key}=?")
            params.append(val)
    if experience_level is not None and experience_level not in {e.value for e in schemas.ExperienceLevel}:
        raise MatchError("INVALID_ANALYSIS", "experience_level 取值非法")
    # 状态推进（仅向前，失败态可被覆盖）
    new_status = None
    if (job_target_id or s["job_target_id"]) and (resume_id or s["resume_id"]):
        new_status = "resume_ready"
    elif (job_target_id or s["job_target_id"]):
        new_status = "jd_ready"
    changed_input = ((job_target_id is not None and job_target_id != s['job_target_id']) or
                     (resume_id is not None and resume_id != s['resume_id']))
    if changed_input:
        sets.append('research_version=research_version+1')
    if new_status and (changed_input or s["status"] not in ("matched", "focus_ready", "plan_ready")):
        sets.append("status=?")
        params.append(new_status)
    sets.append("updated_at=?")
    params.append(utcnow_iso())
    params.append(session_id)
    sql = f"UPDATE jobseeker_session SET {', '.join(sets)} WHERE id=?"
    assert_scope(sql, "jobseeker")
    conn.execute(sql, tuple(params))
    conn.commit()
    return get_session(session_id)


def retry_session(session_id: str) -> dict:
    s = query_one("SELECT * FROM jobseeker_session WHERE id=?", (session_id,))
    if not s:
        raise MatchError("INVALID_ANALYSIS", "会话不存在")
    if s["job_target_id"] and s["resume_id"]:
        status = "resume_ready"
    elif s["job_target_id"]:
        status = "jd_ready"
    else:
        status = "draft"
    set_status(session_id, status, None, None)
    return get_session(session_id)


def set_status(session_id: str, status: str, failure_code: Optional[str] = None,
               failure_message: Optional[str] = None) -> None:
    conn = get_conn()
    sql = "UPDATE jobseeker_session SET status=?, failure_code=?, failure_message=?, updated_at=? WHERE id=?"
    assert_scope(sql, "jobseeker")
    conn.execute(sql, (status, failure_code, failure_message, utcnow_iso(), session_id))
    conn.commit()


# ---------------------------------------------------------------------------
# prepare / submit / auto
# ---------------------------------------------------------------------------

def prepare_match(session_id: str) -> dict:
    s = query_one("SELECT * FROM jobseeker_session WHERE id=?", (session_id,))
    if not s:
        raise MatchError("INVALID_ANALYSIS", "会话不存在")
    if not s["job_target_id"] or not s["resume_id"]:
        raise MatchError("INVALID_ANALYSIS", "缺少目标 JD 或简历", False,
                         "先完成 JD 录入与简历解析")
    target = targets.get_target(s["job_target_id"])
    if not target:
        raise MatchError("INVALID_ANALYSIS", "目标 JD 不存在", False, "先完成 JD 录入")
    resume = query_one("SELECT * FROM resume WHERE id=?", (s["resume_id"],))
    if not resume:
        raise MatchError("INVALID_ANALYSIS", "简历不存在", False, "先完成简历导入")
    if resume["parse_status"] not in ("parsed", "partial"):
        raise MatchError("INVALID_ANALYSIS", f"简历解析状态为 {resume['parse_status']}，无法进入匹配分析",
                         False, "先完成简历解析（解析失败请粘贴文本版简历）")

    token = pack.create_batch("jd_match", ref_id=session_id,
                              refs={"target": target["id"], "resume": resume["id"]},
                              prompt_version=schemas.MATCH_PROMPT_VERSION,
                              schema_version=schemas.MATCH_SCHEMA_VERSION)

    items = [{"id": r["id"], "kind": r["kind"], "title": r["title"], "org": r["org"],
              "period_raw": r["period_raw"], "start": r["start"], "end": r["end"]}
             for r in query_all("SELECT * FROM resume_item WHERE resume_id=? ORDER BY seq", (resume["id"],))]
    answered = query_all("SELECT * FROM clarification WHERE session_id=? AND answer_text IS NOT NULL ORDER BY seq",
                         (session_id,))
    for a in answered:
        a["target_requirement_ids"] = jloads(a["target_requirement_ids"], [])

    return {
        "batch_token": token,
        "payload_kind": "jd_match",
        "prompt_version": schemas.MATCH_PROMPT_VERSION,
        "schema_version": schemas.MATCH_SCHEMA_VERSION,
        "instructions": load_prompt(),
        "limits": {"max_requirements": schemas.MAX_REQUIREMENTS,
                   "max_questions": schemas.MAX_CLARIFICATIONS,
                   "max_quotes": schemas.MAX_QUOTES_PER_OWNER,
                   "max_resume_chars": schemas.MAX_RESUME_TEXT_CHARS},
        "jd": {"target_id": target["id"], "title": target["title"],
               "company_display": target["company_display"], "evidence_text": target["evidence_text"],
               "section_ranges": target["section_ranges"],
               "responsibilities_text": target["responsibilities_text"],
               "requirements_text": target["requirements_text"]},
        "resume": {"resume_id": resume["id"], "label": resume["label"],
                   "evidence_text": resume["evidence_text"],
                   "section_ranges": jloads(resume["section_ranges"], {}), "items": items},
        "clarifications_answered": answered,
    }


def submit_match(payload_dict: dict) -> dict:
    try:
        payload = schemas.MatchSubmitPayload(**payload_dict)
    except Exception as e:  # pydantic ValidationError
        raise MatchError("INVALID_ANALYSIS", f"Schema 校验失败：{e}", True,
                         "修正后重新提交（最多 1 次）")
    try:
        batch = pack.require_batch(payload.batch_token, "jd_match")
    except pack.PackError as e:
        raise MatchError(e.code, e.message, e.retryable, e.operator_action)

    session_id = batch["ref_id"]
    refs = jloads(batch["snapshot_ids"], {})
    target_id = refs.get("target")
    resume_id = refs.get("resume")
    target = targets.get_target(target_id) if target_id else None
    resume = query_one("SELECT * FROM resume WHERE id=?", (resume_id or "",)) if resume_id else None
    if not target or not resume:
        raise MatchError("INVALID_ANALYSIS", "包内 JD 或简历引用失效，请重新 prepare")
    current = query_one('SELECT job_target_id,resume_id FROM jobseeker_session WHERE id=?',(session_id,))
    if not current or current['job_target_id'] != target_id or current['resume_id'] != resume_id:
        raise MatchError('VERSION_CONFLICT', 'JD 或简历已更换，请按当前材料重新匹配', True)
    jd_text = target["evidence_text"]
    resume_text = resume["evidence_text"]

    payload_hash = pack.hash_payload(payload.model_dump())
    try:
        prior = pack.idempotent_result(payload.submission_id, payload_hash)
    except pack.PackError as e:
        raise MatchError(e.code, e.message, e.retryable, e.operator_action)
    if prior is not None:
        return prior

    errors = _validate_match_payload(payload, jd_text, resume_text)
    if errors:
        set_status(session_id, "failed", "INVALID_ANALYSIS",
                   "校验失败，详见字段级错误；修正后重新提交")
        return {"outcomes": [{"ok": False, "errors": errors}], "ok_count": 0, "failed_count": 1,
                "requirement_ids": [], "row_ids": []}

    req_map = _save_requirements(session_id, target_id, [r.model_dump() for r in payload.requirements])
    row_ids = _save_match_rows(session_id, req_map, [r.model_dump() for r in payload.match_rows])
    _save_clarifications(session_id, req_map, [c.model_dump() for c in payload.clarifications])

    result = {"outcomes": [{"ok": True}], "ok_count": 1, "failed_count": 0,
              "requirement_ids": list(req_map.values()), "row_ids": row_ids}
    pack.record_submission(payload.submission_id, payload.batch_token, payload_hash, result)
    pack.consume_batch(payload.batch_token)
    conn = get_conn()
    sql = ("UPDATE jobseeker_session SET status='matched', failure_code=NULL, failure_message=NULL, "
           "model_label=?, prompt_version=?, schema_version=?, updated_at=? WHERE id=?")
    assert_scope(sql, "jobseeker")
    conn.execute(sql, (payload.model_label, schemas.MATCH_PROMPT_VERSION,
                       schemas.MATCH_SCHEMA_VERSION, utcnow_iso(), session_id))
    conn.commit()
    return result


def auto_match(session_id: str) -> dict:
    prepared = prepare_match(session_id)
    # 真实模型优先；显式 demo 模式才走规则匹配器；未配置凭据绝不回退 mock（R-05/R-20）
    if ai_provider.ai_config()["configured"]:
        return match_via_model(prepared)
    if ai_provider.demo_enabled():
        body = rule_match(prepared)
        submission = {
            "batch_token": prepared["batch_token"],
            "submission_id": new_id("sub"),
            "model_label": schemas.RULE_MATCHER_LABEL,
            "requirements": body["requirements"],
            "match_rows": body["match_rows"],
            "clarifications": body["clarifications"],
        }
        return submit_match(submission)
    raise MatchError("AI_NOT_CONFIGURED",
                     "AI 服务未配置，无法执行匹配分析（不回退为演示结果）", False,
                     "在设置页配置模型后重试；或开启 demo_mode 使用规则匹配器（仅演示）")


def match_via_model(prepared: dict) -> dict:
    """调用真实模型生成 requirements/match_rows/clarifications，走 submit_match 严格校验入库。"""
    user_content = _prepared_to_user_content(prepared)
    try:
        raw = (_batched_match_output(prepared) if len(prepared['jd'].get('evidence_text') or '') > 1000
               else ai_provider.chat_json(load_prompt(), user_content, temperature=0.0))
    except ai_provider.AIError as e:
        raise MatchError(e.code, e.message, e.retryable, e.operator_action)
    texts = {"jd": prepared["jd"].get("evidence_text") or "",
             "resume": prepared["resume"].get("evidence_text") or ""}
    repaired = 0
    for req in (raw.get("requirements") or []):
        if isinstance(req, dict):
            repaired += pack.repair_quotes_in_list(req.get("quotes") or [], texts["jd"])
    for row in (raw.get("match_rows") or []):
        if not isinstance(row, dict):
            continue
        for q in (row.get("quotes") or []):
            if not isinstance(q, dict):
                continue
            side = q.get("side", "jd")
            s, e, rep = pack.repair_quote_offsets(q.get("quote"), texts.get(side, ""),
                                                  q.get("start"), q.get("end"))
            if rep:
                q["start"], q["end"] = s, e
                repaired += 1
    cfg = ai_provider.ai_config()
    submission = {
        "batch_token": prepared["batch_token"],
        "submission_id": new_id("sub"),
        "model_label": f"{cfg['provider']}:{cfg['model']}",
        "requirements": raw.get("requirements", []),
        "match_rows": raw.get("match_rows", []),
        "clarifications": raw.get("clarifications", []),
    }
    result = submit_match(submission)
    if repaired:
        result["repaired_offsets"] = repaired
    return result


def _batched_match_output(prepared: dict) -> dict:
    """Bound output size without dropping JD requirements or accepting a truncated JSON."""
    extraction = load_prompt() + '\n本轮仅抽取 JD 要求，返回紧凑 JSON {"requirements":[...]}; 不输出匹配矩阵或简历判断。保留所有独立要求，合并重复要求。'
    raw = ai_provider.chat_json(extraction, jdumps({'jd': prepared['jd']}), temperature=0.0)
    requirements = raw.get('requirements') if isinstance(raw, dict) else None
    if not isinstance(requirements, list) or not 1 <= len(requirements) <= schemas.MAX_REQUIREMENTS:
        raise MatchError('AI_OUTPUT_INVALID', '岗位要求抽取返回格式或数量不正确', True)
    try:
        requirements = [schemas.RequirementItem(**r).model_dump(mode='json') for r in requirements]
    except Exception:
        raise MatchError('AI_OUTPUT_INVALID', '岗位要求抽取未通过 Schema 校验', True)
    jd_text = prepared['jd']['evidence_text']
    for req in requirements:
        pack.repair_quotes_in_list(req['quotes'], jd_text)
        if req['category'] != 'ambiguous' and not req['quotes']:
            raise MatchError('INVALID_EVIDENCE', '岗位要求抽取缺少 JD 原文引用', True)
        if pack.validate_quotes(req['quotes'], {'jd': jd_text}, 'requirements.quotes'):
            raise MatchError('INVALID_EVIDENCE', '岗位要求抽取引用未逐字对应 JD', True)
    refs = [r['ref'] for r in requirements]
    if len(set(refs)) != len(refs):
        raise MatchError('AI_OUTPUT_INVALID', '岗位要求引用号重复', True)
    rows, clarifications = [], []
    context = jloads(_prepared_to_user_content(prepared), {})
    for start in range(0, len(requirements), 5):
        selected = requirements[start:start+5]
        prompt = load_prompt() + '\n本轮仅为 selected_requirements 生成匹配行，返回紧凑 JSON {"match_rows":[...],"clarifications":[...]}; 不再抽取或输出 requirements。每个选定 ref 恰好一行，不能引用本批之外的 ref。'
        part = ai_provider.chat_json(prompt, jdumps({**context,'selected_requirements': selected}), temperature=0.0)
        part_rows = part.get('match_rows') if isinstance(part, dict) else None
        expected = {r['ref'] for r in selected}
        if (not isinstance(part_rows, list) or any(not isinstance(r, dict) for r in part_rows)
                or len(part_rows) != len(expected)
                or {r.get('requirement_ref') for r in part_rows} != expected):
            raise MatchError('AI_OUTPUT_INVALID', '匹配批次未完整覆盖本批岗位要求', True)
        part_clars = part.get('clarifications', [])
        if not isinstance(part_clars, list) or any(not isinstance(c, dict) or not set(c.get('requirement_refs', [])).issubset(expected) for c in part_clars):
            raise MatchError('AI_OUTPUT_INVALID', '匹配批次的补充问题引用不正确', True)
        rows.extend(part_rows)
        clarifications.extend(part_clars)
    return {'requirements': requirements, 'match_rows': rows,
            'clarifications': clarifications[:schemas.MAX_CLARIFICATIONS]}


def _prepared_to_user_content(prepared: dict) -> str:
    """把 prepared 包序列化为给模型的用户输入（脱敏后正文 + 结构化条目，不含姓名/联系方式）。"""
    jd = prepared["jd"]
    resume = prepared["resume"]
    return jdumps({
        "jd": {"title": jd["title"], "company_display": jd.get("company_display"),
               "evidence_text": jd["evidence_text"],
               "responsibilities_text": jd.get("responsibilities_text"),
               "requirements_text": jd.get("requirements_text")},
        "resume": {"label": resume["label"], "evidence_text": resume["evidence_text"],
                   "items": resume.get("items", [])},
        "clarifications_answered": prepared.get("clarifications_answered", []),
    })


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _validate_match_payload(payload: schemas.MatchSubmitPayload, jd_text: str,
                            resume_text: str) -> list[dict]:
    errors: list[dict] = []
    texts = {"jd": jd_text, "resume": resume_text}
    refs = {r.ref for r in payload.requirements}

    for i, req in enumerate(payload.requirements):
        prefix = f"requirements[{i}]"
        if schemas.has_banned_phrase(req.text) or schemas.has_banned_phrase(req.judgement_note):
            errors.append({"field": prefix, "code": "INVALID_ANALYSIS",
                           "message": "文本含禁用措辞（保证通过/真题/匹配度百分比等）"})
        # 引用偏移校验
        errors.extend(pack.validate_quotes([q.model_dump() for q in req.quotes], texts,
                                           f"{prefix}.quotes"))
        # requirement 引用只允许 JD 侧
        for j, q in enumerate(req.quotes):
            if q.side != schemas.EvidenceSide.jd:
                errors.append({"field": f"{prefix}.quotes[{j}].side", "code": "INVALID_EVIDENCE",
                               "message": "requirement 引用必须为 JD 侧（side='jd'）"})
        if req.category == schemas.RequirementCategory.ambiguous and not req.quotes:
            if len(req.judgement_note) < 10:
                errors.append({"field": f"{prefix}.judgement_note", "code": "INVALID_ANALYSIS",
                               "message": "ambiguous 且无 JD 引用时，必须用 ≥10 字说明模糊原因"})
        if req.category != schemas.RequirementCategory.ambiguous and not req.quotes:
            errors.append({"field": f"{prefix}.quotes", "code": "INVALID_EVIDENCE",
                           "message": "非 ambiguous 要求至少需要 1 条 JD 引用"})

    for i, row in enumerate(payload.match_rows):
        prefix = f"match_rows[{i}]"
        if row.requirement_ref not in refs:
            errors.append({"field": f"{prefix}.requirement_ref", "code": "INVALID_ANALYSIS",
                           "message": f"requirement_ref={row.requirement_ref!r} 不存在于 requirements"})
        for field_name in ("jd_note", "resume_note", "reasoning", "prep_advice"):
            if schemas.has_banned_phrase(getattr(row, field_name)):
                errors.append({"field": f"{prefix}.{field_name}", "code": "INVALID_ANALYSIS",
                               "message": "文本含禁用措辞"})
        if row.match_status == schemas.MatchStatus.confirmed_gap:
            errors.append({"field": f"{prefix}.match_status", "code": "INVALID_ANALYSIS",
                           "message": "confirmed_gap 只能由用户本人通过复核/补充确认，不能由分析产出"})
        if row.match_status == schemas.MatchStatus.not_in_resume and not row.uncertainties:
            errors.append({"field": f"{prefix}.uncertainties", "code": "INVALID_ANALYSIS",
                           "message": "not_in_resume 状态必须写明不确定性（不得为空）"})
        errors.extend(pack.validate_quotes([q.model_dump() for q in row.quotes], texts,
                                           f"{prefix}.quotes"))

    for i, c in enumerate(payload.clarifications):
        if schemas.has_banned_phrase(c.question) or schemas.has_banned_phrase(c.why_asked):
            errors.append({"field": f"clarifications[{i}]", "code": "INVALID_ANALYSIS",
                           "message": "文本含禁用措辞"})
    return errors


# ---------------------------------------------------------------------------
# 入库
# ---------------------------------------------------------------------------

def _insert_quote(conn, session_id: str, owner_type: str, owner_id: str, q: dict, now: str) -> None:
    sql = ("INSERT INTO match_quote (id, session_id, owner_type, owner_id, side, ref_type, ref_id, "
           "section, quote, start, end, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")
    conn.execute(sql, (new_id("mqu"), session_id, owner_type, owner_id, q["side"], "target",
                       None, q.get("section", ""), q["quote"], q["start"], q["end"], now))


def _save_requirements(session_id: str, target_id: str, items: list) -> dict[str, str]:
    conn = get_conn()
    now = utcnow_iso()
    del_q = ("DELETE FROM match_quote WHERE owner_type='jd_requirement' AND owner_id IN "
             "(SELECT id FROM jd_requirement WHERE session_id=? AND review_status='unreviewed')")
    assert_scope(del_q, "jobseeker")
    conn.execute(del_q, (session_id,))
    del_r = "DELETE FROM jd_requirement WHERE session_id=? AND review_status='unreviewed'"
    assert_scope(del_r, "jobseeker")
    conn.execute(del_r, (session_id,))
    ref_map: dict[str, str] = {}
    for i, it in enumerate(items):
        rid = new_id("req")
        ins = ("INSERT INTO jd_requirement (id, session_id, job_target_id, category, text, "
               "judgement_note, source_ref, review_status, review_version, seq, created_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?)")
        assert_scope(ins, "jobseeker")
        conn.execute(ins, (rid, session_id, target_id, it["category"], it["text"],
                           it["judgement_note"], it["ref"], "unreviewed", 0, i, now))
        ref_map[it["ref"]] = rid
        for q in it["quotes"]:
            _insert_quote(conn, session_id, "jd_requirement", rid, q, now)
    conn.commit()
    return ref_map


def _save_match_rows(session_id: str, req_map: dict, rows: list) -> list[str]:
    conn = get_conn()
    now = utcnow_iso()
    del_q = ("DELETE FROM match_quote WHERE owner_type='match_row' AND owner_id IN "
             "(SELECT id FROM match_row WHERE session_id=? AND review_status='unreviewed')")
    assert_scope(del_q, "jobseeker")
    conn.execute(del_q, (session_id,))
    del_r = "DELETE FROM match_row WHERE session_id=? AND review_status='unreviewed'"
    assert_scope(del_r, "jobseeker")
    conn.execute(del_r, (session_id,))
    row_ids: list[str] = []
    for i, r in enumerate(rows):
        req_id = req_map.get(r["requirement_ref"])
        if not req_id:
            continue
        rid = new_id("mrow")
        ins = ("INSERT INTO match_row (id, session_id, requirement_id, match_status, jd_note, "
               "resume_note, user_supplement, reasoning, uncertainties, prep_advice, needs_rejudge, "
               "review_status, review_version, created_at, updated_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
        assert_scope(ins, "jobseeker")
        conn.execute(ins, (rid, session_id, req_id, r["match_status"], r["jd_note"], r["resume_note"],
                           None, r["reasoning"], jdumps(r["uncertainties"]), r["prep_advice"], 0,
                           "unreviewed", 0, now, now))
        row_ids.append(rid)
        for q in r["quotes"]:
            _insert_quote(conn, session_id, "match_row", rid, q, now)
    conn.commit()
    return row_ids


def _save_clarifications(session_id: str, req_map: dict, items: list) -> int:
    conn = get_conn()
    now = utcnow_iso()
    del_sql = "DELETE FROM clarification WHERE session_id=? AND answer_text IS NULL"
    assert_scope(del_sql, "jobseeker")
    conn.execute(del_sql, (session_id,))
    count = 0
    for i, c in enumerate(items):
        target_ids = [req_map[r] for r in c["requirement_refs"] if r in req_map]
        cid = new_id("clar")
        ins = ("INSERT INTO clarification (id, session_id, question, why_asked, "
               "target_requirement_ids, seq, created_at) VALUES (?,?,?,?,?,?,?)")
        assert_scope(ins, "jobseeker")
        conn.execute(ins, (cid, session_id, c["question"], c["why_asked"], jdumps(target_ids), i, now))
        count += 1
    conn.commit()
    return count


# ---------------------------------------------------------------------------
# 查询视图
# ---------------------------------------------------------------------------

def _quotes_for(owner_type: str, owner_id: str) -> list[dict]:
    return query_all("SELECT * FROM match_quote WHERE owner_type=? AND owner_id=? ORDER BY start",
                     (owner_type, owner_id))


def requirements_view(session_id: str) -> dict:
    reqs = query_all("SELECT * FROM jd_requirement WHERE session_id=? ORDER BY seq, created_at", (session_id,))
    for r in reqs:
        r["quotes"] = _quotes_for("jd_requirement", r["id"])
    return {"requirements": reqs}


def match_view(session_id: str) -> dict:
    session = query_one("SELECT * FROM jobseeker_session WHERE id=?", (session_id,))
    rows = query_all("SELECT * FROM match_row WHERE session_id=? ORDER BY created_at", (session_id,))
    out = []
    for r in rows:
        req = query_one("SELECT * FROM jd_requirement WHERE id=?", (r["requirement_id"],))
        r2 = dict(r)
        r2["uncertainties"] = jloads(r["uncertainties"], [])
        r2["requirement"] = {"category": req["category"], "text": req["text"]} if req else None
        r2["quotes"] = _quotes_for("match_row", r["id"])
        out.append(r2)
    clars = query_all("SELECT * FROM clarification WHERE session_id=? ORDER BY seq", (session_id,))
    for c in clars:
        c["target_requirement_ids"] = jloads(c["target_requirement_ids"], [])
    model_label = session["model_label"] if session else None
    return {"rows": out, "clarifications": clars, "model_label": model_label,
            "demo": model_label == schemas.RULE_MATCHER_LABEL}


def _row_view(row: dict) -> dict:
    r = dict(row)
    r["uncertainties"] = jloads(r["uncertainties"], [])
    r["quotes"] = _quotes_for("match_row", r["id"])
    return r


# ---------------------------------------------------------------------------
# 补充经历 / 回答提问
# ---------------------------------------------------------------------------

def supplement_row(row_id: str, user_supplement: str, match_status: Optional[str],
                   reason: str) -> dict:
    row = query_one("SELECT * FROM match_row WHERE id=?", (row_id,))
    if not row:
        raise MatchError("INVALID_ANALYSIS", "匹配行不存在")
    if not user_supplement or not user_supplement.strip():
        raise MatchError("INVALID_ANALYSIS", "补充内容不能为空", "填写本人补充经历")
    if not reason or not reason.strip():
        raise MatchError("INVALID_ANALYSIS", "修正必须填写原因（留痕要求）", "填写 reason 后重试")
    if match_status is not None and match_status not in {m.value for m in schemas.MatchStatus}:
        raise MatchError("INVALID_ANALYSIS", "match_status 取值非法")
    before = dict(row)
    conn = get_conn()
    now = utcnow_iso()
    if match_status is not None:
        sql = ("UPDATE match_row SET user_supplement=?, match_status=?, needs_rejudge=0, "
               "review_status='corrected', review_version=review_version+1, updated_at=? WHERE id=?")
        conn.execute(sql, (user_supplement, match_status, now, row_id))
    else:
        sql = ("UPDATE match_row SET user_supplement=?, needs_rejudge=1, "
               "review_status='corrected', review_version=review_version+1, updated_at=? WHERE id=?")
        conn.execute(sql, (user_supplement, now, row_id))
    after = query_one("SELECT * FROM match_row WHERE id=?", (row_id,))
    ev = ("INSERT INTO review_event (id, target_type, target_id, expected_version, action, "
          "before_json, after_json, reason, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)")
    conn.execute(ev, (new_id("rev"), "match_row", row_id, row["review_version"], "correct",
                      jdumps(before), jdumps(dict(after)), reason, "user", now))
    conn.commit()
    return {"row": _row_view(after)}


def answer_clarification(clar_id: str, answer_text: str, reason: str) -> dict:
    c = query_one("SELECT * FROM clarification WHERE id=?", (clar_id,))
    if not c:
        raise MatchError("INVALID_ANALYSIS", "补充提问不存在")
    if not answer_text or not answer_text.strip():
        raise MatchError("INVALID_ANALYSIS", "回答不能为空")
    if not reason or not reason.strip():
        raise MatchError("INVALID_ANALYSIS", "回答必须填写原因（留痕要求）")
    before = dict(c)
    conn = get_conn()
    now = utcnow_iso()
    conn.execute("UPDATE clarification SET answer_text=?, answered_at=? WHERE id=?",
                 (answer_text, now, clar_id))
    after = query_one("SELECT * FROM clarification WHERE id=?", (clar_id,))
    ev = ("INSERT INTO review_event (id, target_type, target_id, expected_version, action, "
          "before_json, after_json, reason, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)")
    conn.execute(ev, (new_id("rev"), "clarification", clar_id, 0, "answer",
                      jdumps(before), jdumps(dict(after)), reason, "user", now))
    conn.commit()
    after["target_requirement_ids"] = jloads(after["target_requirement_ids"], [])
    return {"clarification": after}


# ---------------------------------------------------------------------------
# 内置规则匹配器（确定性，model_label=rule-matcher-demo-v1）
# ---------------------------------------------------------------------------

SKILL_PATTERNS: list[tuple[str, list[str]]] = [
    ("funding_settlement", ["资金方", "资金路由", "资金合作", "清结算", "对账", "分单", "放款", "账务", "资金成本", "挂账", "冲正"]),
    ("credit_risk_pricing", ["授信", "额度", "定价", "风控", "反欺诈", "准入"]),
    ("billing_repayment", ["账单", "还款", "分期", "扣款"]),
    ("borrowing_conversion", ["借款", "用信", "复借", "放款成功率"]),
    ("postloan_service", ["催收", "贷后", "客服"]),
    ("acquisition_channel", ["获客", "渠道", "流量", "拉新"]),
    ("data_analysis", ["sql", "数据", "取数", "分析", "excel", "同比", "环比", "复盘"]),
    ("product_design", ["产品方案", "产品设计", "需求文档", "prd", "原型", "axure", "产品化"]),
    ("compliance", ["监管", "合规", "资金来源", "资金流向"]),
    ("credit_general", ["信贷", "消费金融", "贷款", "现金贷"]),
]

DOMAIN_LABELS = {
    "funding_settlement": "资金与清结算", "credit_risk_pricing": "授信与定价",
    "billing_repayment": "账单与还款", "borrowing_conversion": "借款与用信",
    "postloan_service": "贷后与客服", "acquisition_channel": "获客与渠道",
    "data_analysis": "数据分析", "product_design": "产品设计",
    "compliance": "监管合规", "credit_general": "消费信贷",
}

# 信贷业务域（用于判断"经验可迁移"：简历有信贷域经验但缺目标域 → transferable）
CREDIT_DOMAINS = {"funding_settlement", "credit_risk_pricing", "billing_repayment",
                  "borrowing_conversion", "postloan_service", "credit_general", "compliance"}


def _domains_hit(text: str) -> list[str]:
    low = (text or "").lower()
    hits: list[str] = []
    for domain, kws in SKILL_PATTERNS:
        if any(k in low for k in kws):
            hits.append(domain)
    return hits


def _locate_quote(text: str, substring: str, side: str, section: str) -> Optional[dict]:
    pos = text.find(substring)
    if pos < 0:
        return None
    return {"side": side, "section": section, "quote": substring, "start": pos, "end": pos + len(substring)}


def _resume_sentence_for(resume_text: str, domains: list[str]) -> Optional[dict]:
    for _, sent in analyzer.split_sentences(resume_text):
        sent_domains = _domains_hit(sent)
        if any(d in sent_domains for d in domains):
            return _locate_quote(resume_text, sent, "resume", "experience")
    return None


def rule_match(prepared: dict) -> dict:
    """确定性规则实现：关键词 + 业务域词典 + 段落归属。不准时输出 insufficient。"""
    jd = prepared["jd"]
    resume = prepared["resume"]
    jd_text = jd["evidence_text"]
    resume_text = resume["evidence_text"]
    resp_text = jd.get("responsibilities_text") or ""
    req_text = jd.get("requirements_text") or ""

    requirements: list[dict] = []
    match_rows: list[dict] = []
    clarifications: list[dict] = []

    def add_requirement(ref: str, category: str, text: str, note: str, section: str) -> None:
        jd_quote = _locate_quote(jd_text, text, "jd", section)
        requirements.append({
            "ref": ref, "category": category, "text": text[:500], "judgement_note": note,
            "quotes": [jd_quote] if jd_quote else [],
        })
        domains = _domains_hit(text)
        if not domains:
            status = "insufficient"
            resume_quote = None
            jd_note = f"JD 表述宽泛，无法映射到具体业务域（{text[:40]}）"
            resume_note = "需结合更多信息判断"
            uncertainties = ["无法从文本确定考察的具体能力项"]
        else:
            resume_domains = set(_domains_hit(resume_text))
            direct = [d for d in domains if d in resume_domains]
            resume_quote = None
            if direct:
                status = "direct_evidence"
                jd_note = f"JD 要求涉及：{_domains_label(domains)}"
                resume_quote = _resume_sentence_for(resume_text, direct)
                resume_note = "简历中存在对应业务域的职责表述"
                uncertainties = ["引用只证明文字存在，不代表掌握程度一致"]
            elif (set(domains) & CREDIT_DOMAINS) and (resume_domains & CREDIT_DOMAINS):
                status = "transferable"
                jd_note = f"JD 要求涉及：{_domains_label(domains)}"
                resume_note = f"简历有信贷相关经历（{_domains_label(sorted(resume_domains & CREDIT_DOMAINS))}），但未直接体现 {_domains_label(domains)}，存在业务口径差异"
                uncertainties = ["行业/业务口径不同，迁移需本人确认与补齐"]
            else:
                status = "not_in_resume"
                jd_note = f"JD 要求涉及：{_domains_label(domains)}"
                resume_note = "简历中未体现该要求（需本人确认，不代表不具备）"
                uncertainties = ["简历未体现不等于不具备，需本人确认"]
        quotes = [q for q in (jd_quote, resume_quote) if q]
        match_rows.append({
            "requirement_ref": ref,
            "match_status": status,
            "jd_note": jd_note, "resume_note": resume_note,
            "reasoning": f"规则匹配器按业务域词典判定（{schemas.RULE_MATCHER_LABEL}），语义待人工确认",
            "uncertainties": uncertainties,
            "prep_advice": _prep_advice(status, domains),
            "quotes": quotes,
        })

    ref_no = 0
    for _, sent in analyzer.split_sentences(resp_text):
        ref_no += 1
        cat = "scenario_experience" if ("场景" in sent or "案例" in sent or "实战" in sent) else "core_task"
        add_requirement(f"R{ref_no}", cat, sent, "职责段自动抽取", "responsibility")
    for _, sent in analyzer.split_sentences(req_text):
        ref_no += 1
        cat = _req_category(sent)
        if cat == "ambiguous":
            add_requirement(f"R{ref_no}", "ambiguous", sent, "表述模糊，无法确认考察边界，需面试确认",
                            "requirement")
        else:
            add_requirement(f"R{ref_no}", cat, sent, "任职要求段自动抽取", "requirement")

    # 补充提问：对 not_in_resume / insufficient / ambiguous 生成 ≤5 条（去重）
    asked = 0
    seen_refs: set[str] = set()
    for req in requirements:
        if asked >= schemas.MAX_CLARIFICATIONS:
            break
        row = next((r for r in match_rows if r["requirement_ref"] == req["ref"]), None)
        if row and row["match_status"] in ("not_in_resume", "insufficient"):
            clarifications.append({
                "question": f"关于「{req['text'][:40]}」，您是否有相关经历或补充说明？",
                "why_asked": "简历未直接体现，需本人确认以避免误判为能力缺口",
                "requirement_refs": [req["ref"]],
            })
            seen_refs.add(req["ref"])
            asked += 1
        elif req["category"] == "ambiguous" and req["ref"] not in seen_refs:
            clarifications.append({
                "question": f"关于「{req['text'][:40]}」，该要求具体指什么？",
                "why_asked": "JD 表述模糊，需向对方确认考察边界",
                "requirement_refs": [req["ref"]],
            })
            seen_refs.add(req["ref"])
            asked += 1

    return {"requirements": requirements, "match_rows": match_rows, "clarifications": clarifications}


def _domains_label(domains: list[str]) -> str:
    return "、".join(DOMAIN_LABELS.get(d, d) for d in domains)


def _prep_advice(status: str, domains: list[str]) -> str:
    if status == "direct_evidence":
        return "准备可量化结果与复盘，能说清方案取舍与效果验证"
    if status == "transferable":
        return "补齐目标行业业务知识，准备迁移逻辑：旧经验如何对应新业务口径"
    if status == "not_in_resume":
        return "先与本人确认是否具备；若不具备，评估是否可短期学习或需真实项目经验"
    return "补充信息后再判断准备方向"


def _req_category(sent: str) -> str:
    if "优先" in sent or "加分" in sent:
        return "nice_to_have"
    if any(k in sent for k in ("必须", "至少", "以上", "学历", "本科", "硕士", "博士")):
        return "must_have"
    if any(k in sent for k in ("良好", "具备", "沟通", "责任心", "团队", "熟悉")):
        # 泛化软性要求 → 模糊，需面试确认
        return "ambiguous"
    return "must_have"
