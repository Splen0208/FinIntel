"""finintel prep：面试重点（6 类 5 要素）+ 准备清单 + 任务状态（F-09/10/24）。

复用 pack.* 原语。内置规则生成器 model_label 固定 rule-matcher-demo-v1。
硬约束：每条 focus 的 uncertainties 非空；措辞限"可能考察/这一类常见会被追问"；
is_practice_project 的任务在 UI/报告强制标注"练习项目（非真实经历）"。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import ai_provider
import interview_research
import pack
import schemas
import store
import targets
from store import (assert_scope, get_conn, jdumps, jloads, new_id, query_all,
                   query_one, utcnow_iso)

PREP_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "prep_plan_v1.md"


class PrepError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False,
                 operator_action: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.operator_action = operator_action


def load_prompt() -> str:
    with open(PREP_PROMPT_PATH, encoding="utf-8") as f:
        return f.read()


def _session(session_id: str) -> dict:
    s = query_one("SELECT * FROM jobseeker_session WHERE id=?", (session_id,))
    if not s:
        raise PrepError("INVALID_ANALYSIS", "会话不存在")
    return s


# ---------------------------------------------------------------------------
# prepare / submit / auto
# ---------------------------------------------------------------------------

def prepare_prep(session_id: str) -> dict:
    s = _session(session_id)
    if s["status"] not in ("matched", "focus_ready", "plan_ready"):
        raise PrepError("INVALID_ANALYSIS", f"会话状态为 {s['status']}，需先完成匹配分析", False,
                        "先执行匹配分析（auto-match 或 submit-match）")
    if not s["job_target_id"] or not s["resume_id"]:
        raise PrepError("INVALID_ANALYSIS", "缺少目标 JD 或简历", False, "先完成 JD 录入与简历解析")

    research = interview_research.materials_view(session_id)
    token = pack.create_batch("prep_plan", ref_id=session_id,
                              refs={"target": s["job_target_id"], "resume": s["resume_id"],
                                    "research_version": research['version'],
                                    "research_material_ids": [r['id'] for r in research['materials']]},
                              prompt_version=schemas.PREP_PROMPT_VERSION,
                              schema_version=schemas.PREP_SCHEMA_VERSION)

    jd = targets.target_view(s["job_target_id"])
    resume = query_one("SELECT * FROM resume WHERE id=?", (s["resume_id"],))
    # 匹配上下文（含真实 requirement id / match_row）
    import matching
    requirements = matching.requirements_view(session_id)["requirements"]
    rows = matching.match_view(session_id)["rows"]

    return {
        "batch_token": token,
        "payload_kind": "prep_plan",
        "prompt_version": schemas.PREP_PROMPT_VERSION,
        "schema_version": schemas.PREP_SCHEMA_VERSION,
        "instructions": load_prompt(),
        "jd": {"target_id": jd["id"], "title": jd["title"], "company_display": jd["company_display"],
               "evidence_text": jd["evidence_text"], "section_ranges": jd["section_ranges"],
               "responsibilities_text": jd["responsibilities_text"],
               "requirements_text": jd["requirements_text"]},
        "resume": {"resume_id": resume["id"], "label": resume["label"],
                   "evidence_text": resume["evidence_text"],
                   "section_ranges": jloads(resume["section_ranges"], {}),
                   "items": [{"id": r["id"], "kind": r["kind"], "title": r["title"],
                              "org": r["org"], "period_raw": r["period_raw"],
                              "start": r["start"], "end": r["end"]}
                             for r in query_all("SELECT * FROM resume_item WHERE resume_id=? ORDER BY seq",
                                                (resume["id"],))]},
        "matching": {"requirements": requirements, "rows": rows},
        "research_background": research,
    }


def submit_prep(payload_dict: dict) -> dict:
    research_questions = payload_dict.get('research_questions', [])
    try:
        payload = schemas.PrepSubmitPayload(**payload_dict)
    except Exception as e:  # pydantic ValidationError
        raise PrepError("INVALID_ANALYSIS", f"Schema 校验失败：{e}", True, "修正后重新提交")
    try:
        batch = pack.require_batch(payload.batch_token, "prep_plan")
    except pack.PackError as e:
        raise PrepError(e.code, e.message, e.retryable, e.operator_action)

    session_id = batch["ref_id"]
    refs = jloads(batch["snapshot_ids"], {})
    jd = targets.target_view(refs["target"]) if refs.get("target") else None
    resume = query_one("SELECT * FROM resume WHERE id=?", (refs.get("resume") or "",)) if refs.get("resume") else None
    if not jd or not resume:
        raise PrepError("INVALID_ANALYSIS", "包内 JD 或简历引用失效，请重新 prepare")
    jd_text = jd["evidence_text"]
    resume_text = resume["evidence_text"]

    current = _session(session_id)
    if current['job_target_id'] != refs.get('target') or current['resume_id'] != refs.get('resume'):
        raise PrepError('VERSION_CONFLICT', 'JD 或简历已变更，请重新匹配并生成准备计划')
    try:
        research_questions = interview_research.validate_questions(
            session_id, refs.get('research_version', 0), research_questions,
            refs.get('research_material_ids', []))
    except interview_research.ResearchError as e:
        raise PrepError(e.code, e.message, True, '重新生成准备计划')
    payload_hash = pack.hash_payload({**payload.model_dump(), 'research_questions': research_questions})
    try:
        prior = pack.idempotent_result(payload.submission_id, payload_hash)
    except pack.PackError as e:
        raise PrepError(e.code, e.message, e.retryable, e.operator_action)
    if prior is not None:
        return prior

    errors = _validate_prep_payload(payload, jd_text, resume_text)
    if errors:
        _set_status(session_id, "failed", "INVALID_ANALYSIS", "校验失败，详见字段级错误；修正后重新提交")
        return {"outcomes": [{"ok": False, "errors": errors}], "ok_count": 0, "failed_count": 1,
                "focus_ids": [], "task_ids": []}

    focus_ids = _save_focus(session_id, [f.model_dump() for f in payload.focus_items])
    task_ids = _save_tasks(session_id, [t.model_dump() for t in payload.prep_tasks])
    interview_research.save_questions(session_id, refs.get('research_version', 0), research_questions)

    result = {"outcomes": [{"ok": True}], "ok_count": 1, "failed_count": 0,
              "focus_ids": focus_ids, "task_ids": task_ids}
    pack.record_submission(payload.submission_id, payload.batch_token, payload_hash, result)
    pack.consume_batch(payload.batch_token)
    conn = get_conn()
    sql = ("UPDATE jobseeker_session SET status='plan_ready', failure_code=NULL, failure_message=NULL, "
           "model_label=?, prompt_version=?, schema_version=?, updated_at=? WHERE id=?")
    assert_scope(sql, "jobseeker")
    conn.execute(sql, (payload.model_label, schemas.PREP_PROMPT_VERSION,
                       schemas.PREP_SCHEMA_VERSION, utcnow_iso(), session_id))
    conn.commit()
    return result


def auto_prep(session_id: str) -> dict:
    prepared = prepare_prep(session_id)
    # 真实模型优先；显式 demo 模式才走规则生成器；未配置凭据绝不回退 mock
    if ai_provider.ai_config()["configured"]:
        return prep_via_model(prepared)
    if ai_provider.demo_enabled():
        body = rule_prep(prepared)
        submission = {
            "batch_token": prepared["batch_token"],
            "submission_id": new_id("sub"),
            "model_label": schemas.RULE_MATCHER_LABEL,
            "focus_items": body["focus_items"],
            "prep_tasks": body["prep_tasks"],
            "research_questions": interview_research.demo_questions(prepared),
        }
        return submit_prep(submission)
    raise PrepError("AI_NOT_CONFIGURED",
                    "AI 服务未配置，无法生成面试重点与准备清单（不回退为演示结果）", False,
                    "在设置页配置模型后重试；或开启 demo_mode 使用规则生成器（仅演示）")


def prep_via_model(prepared: dict) -> dict:
    """调用真实模型生成 focus_items/prep_tasks，走 submit_prep 严格校验入库。"""
    user_content = _prep_to_user_content(prepared)
    try:
        raw = ai_provider.chat_json(load_prompt(), user_content, temperature=0.0)
    except ai_provider.AIError as e:
        raise PrepError(e.code, e.message, e.retryable, e.operator_action)
    texts = {"jd": prepared["jd"].get("evidence_text") or "",
             "resume": prepared["resume"].get("evidence_text") or ""}
    repaired = 0
    for f in (raw.get("focus_items") or []):
        if not isinstance(f, dict):
            continue
        for link in (f.get("links") or []):
            if not isinstance(link, dict):
                continue
            if link.get("side") not in ("jd", "resume", "requirement"):
                link["side"] = "requirement"
            q, rep = _normalize_link_quote(link, texts)
            link["quote"] = q
            if rep:
                repaired += 1
    materials = {m['id']: m for m in prepared.get('research_background', {}).get('materials', [])}
    for question in raw.get('research_questions') or []:
        if not isinstance(question, dict) or not isinstance(question.get('quote'), str):
            continue
        material = materials.get(question.get('material_id'))
        if not material:
            continue
        base = material['start']
        start = question.get('start')
        end = question.get('end')
        relative_start = start-base if isinstance(start, int) else None
        relative_end = end-base if isinstance(end, int) else None
        start, end, fixed = pack.repair_quote_offsets(question['quote'], material['quote'], relative_start, relative_end)
        if fixed:
            question['start'], question['end'] = base+start, base+end
            repaired += 1
    cfg = ai_provider.ai_config()
    submission = {
        "batch_token": prepared["batch_token"],
        "submission_id": new_id("sub"),
        "model_label": f"{cfg['provider']}:{cfg['model']}",
        "focus_items": raw.get("focus_items", []),
        "prep_tasks": raw.get("prep_tasks", []),
        "research_questions": raw.get("research_questions", []),
    }
    result = submit_prep(submission)
    if repaired:
        result["repaired_offsets"] = repaired
    return result


def _normalize_link_quote(link: dict, texts: dict) -> tuple[Optional[dict], bool]:
    """把 links[].quote 归一为 QuoteRef 或 None，返回 (quote, 是否修正过)。

    模型常见两种偏差：
    1) 直接给字符串（应给 QuoteRef 对象）→ 用原文定位补出 start/end；
    2) 给了对象但 start/end 有 off-by-N → 用唯一匹配位置修正。
    两者都**不放宽真实性**：定位不到就置 None（LinkRef.quote 允许为空）。
    """
    q = link.get("quote")
    side = link.get("side") if link.get("side") in ("jd", "resume") else None
    if isinstance(q, dict):
        quote_str = q.get("quote") or ""
        sd = q.get("side") if q.get("side") in ("jd", "resume") else (side or "jd")
        text = texts.get(sd, "")
        s, e, rep = pack.repair_quote_offsets(quote_str, text, q.get("start"), q.get("end"))
        if quote_str and 0 <= s < e <= len(text) and text[s:e] == quote_str:
            return {"side": sd, "section": q.get("section") or "", "quote": quote_str,
                    "start": s, "end": e}, rep
        return None, False
    if isinstance(q, str) and q.strip():
        quote_str = q.strip()
        for sd in ([side] if side else []) + ["jd", "resume"]:
            text = texts.get(sd, "")
            pos = text.find(quote_str)
            if pos >= 0:
                return {"side": sd, "section": "", "quote": quote_str,
                        "start": pos, "end": pos + len(quote_str)}, True
        return None, False
    return None, False


def _prep_to_user_content(prepared: dict) -> str:
    jd = prepared["jd"]
    resume = prepared["resume"]
    contract = schemas.PrepSubmitPayload.model_json_schema()
    for key in ('batch_token', 'submission_id', 'model_label'):
        contract['properties'].pop(key, None)
    contract['required'] = ['focus_items', 'prep_tasks', 'research_questions']
    contract['properties']['research_questions'] = {
        'type': 'array', 'maxItems': interview_research.MAX_MATERIALS,
        'items': {'type': 'object', 'required': ['material_id', 'question', 'why_relevant', 'quote', 'start', 'end', 'limitations'],
                  'properties': {'material_id': {'type': 'string'}, 'question': {'type': 'string'},
                      'why_relevant': {'type': 'string'}, 'quote': {'type': 'string'},
                      'start': {'type': 'integer'}, 'end': {'type': 'integer'},
                      'limitations': {'type': 'array', 'minItems': 1, 'items': {'type': 'string'}}}}}
    return jdumps({
        "output_contract": contract,
        "jd": {"title": jd["title"], "evidence_text": jd["evidence_text"],
               "responsibilities_text": jd.get("responsibilities_text"),
               "requirements_text": jd.get("requirements_text")},
        "resume": {"label": resume["label"], "evidence_text": resume["evidence_text"],
                   "items": resume.get("items", [])},
        "matching": prepared.get("matching", {}),
        "research_background": prepared.get("research_background", {}),
    })


def _set_status(session_id: str, status: str, failure_code: Optional[str] = None,
                failure_message: Optional[str] = None) -> None:
    conn = get_conn()
    sql = "UPDATE jobseeker_session SET status=?, failure_code=?, failure_message=?, updated_at=? WHERE id=?"
    assert_scope(sql, "jobseeker")
    conn.execute(sql, (status, failure_code, failure_message, utcnow_iso(), session_id))
    conn.commit()


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _validate_prep_payload(payload: schemas.PrepSubmitPayload, jd_text: str,
                           resume_text: str) -> list[dict]:
    errors: list[dict] = []
    texts = {"jd": jd_text, "resume": resume_text}

    def banned(field: str, text: str) -> None:
        if schemas.has_banned_phrase(text):
            errors.append({"field": field, "code": "INVALID_ANALYSIS",
                           "message": "文本含禁用措辞（一定会问/真题/匹配度百分比等）"})

    for i, f in enumerate(payload.focus_items):
        prefix = f"focus_items[{i}]"
        banned(f"{prefix}.title", f.title)
        banned(f"{prefix}.why_asked", f.why_asked)
        for j, q in enumerate(f.questions):
            banned(f"{prefix}.questions[{j}].question", q.question)
            for k, fu in enumerate(q.followups):
                banned(f"{prefix}.questions[{j}].followups[{k}]", fu)
        for k, m in enumerate(f.materials):
            banned(f"{prefix}.materials[{k}]", m)
        for k, u in enumerate(f.uncertainties):
            banned(f"{prefix}.uncertainties[{k}]", u)
        if not f.uncertainties or any(not u.strip() for u in f.uncertainties):
            errors.append({"field": f"{prefix}.uncertainties", "code": "INVALID_ANALYSIS",
                           "message": "uncertainties 不得为空（R8）"})
        if not f.links:
            errors.append({"field": f"{prefix}.links", "code": "INVALID_ANALYSIS",
                           "message": "每条考察重点至少一条依据（G2）"})
        for j, link in enumerate(f.links):
            if link.quote is not None:
                if link.side in ("jd", "resume"):
                    errors.extend(pack.validate_quotes([link.quote.model_dump()], texts,
                                                       f"{prefix}.links[{j}].quote"))
                else:
                    errors.append({"field": f"{prefix}.links[{j}].side", "code": "INVALID_EVIDENCE",
                                   "message": "requirement 侧 link 不应带 quote（无偏移基准）"})
            banned(f"{prefix}.links[{j}].note", link.note)

    for i, t in enumerate(payload.prep_tasks):
        prefix = f"prep_tasks[{i}]"
        for field in ("purpose", "deliverable", "self_check"):
            banned(f"{prefix}.{field}", getattr(t, field))
        for k, a in enumerate(t.actions):
            banned(f"{prefix}.actions[{k}]", a)
        if t.is_practice_project and t.learnability != schemas.Learnability.short_term_learnable:
            errors.append({"field": f"{prefix}.is_practice_project", "code": "INVALID_ANALYSIS",
                           "message": "练习项目仅适用于可短期学习的能力（learnability=short_term_learnable）"})
        for k, r in enumerate(t.resources):
            banned(f"{prefix}.resources[{k}].title", r.title)
    return errors


# ---------------------------------------------------------------------------
# 入库
# ---------------------------------------------------------------------------

def _save_focus(session_id: str, items: list) -> list[str]:
    conn = get_conn()
    now = utcnow_iso()
    del_l = ("DELETE FROM focus_link WHERE focus_id IN "
             "(SELECT id FROM interview_focus WHERE session_id=? AND review_status='unreviewed')")
    assert_scope(del_l, "jobseeker")
    conn.execute(del_l, (session_id,))
    del_f = "DELETE FROM interview_focus WHERE session_id=? AND review_status='unreviewed'"
    assert_scope(del_f, "jobseeker")
    conn.execute(del_f, (session_id,))
    ids: list[str] = []
    for i, f in enumerate(items):
        fid = new_id("foc")
        ins = ("INSERT INTO interview_focus (id, session_id, category, title, why_asked, questions, "
               "materials, uncertainties, review_status, review_version, seq, created_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")
        assert_scope(ins, "jobseeker")
        conn.execute(ins, (fid, session_id, f["category"], f["title"], f["why_asked"],
                           jdumps(f["questions"]), jdumps(f["materials"]), jdumps(f["uncertainties"]),
                           "unreviewed", 0, i, now))
        for link in f["links"]:
            q = link.get("quote")
            quote = q["quote"] if q else None
            start = q["start"] if q else None
            end = q["end"] if q else None
            lins = ("INSERT INTO focus_link (id, focus_id, side, ref_type, ref_id, quote, start, end, "
                    "note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)")
            assert_scope(lins, "jobseeker")
            conn.execute(lins, (new_id("flk"), fid, link["side"], link.get("ref_type", ""),
                                link.get("ref_id"), quote, start, end, link.get("note", ""), now))
        ids.append(fid)
    conn.commit()
    return ids


def _resolve_req_ids(session_id: str, refs: list) -> list[str]:
    real: list[str] = []
    for ref in refs:
        row = query_one("SELECT id FROM jd_requirement WHERE session_id=? AND (source_ref=? OR id=?)",
                        (session_id, ref, ref))
        if row:
            real.append(row["id"])
    return real


def _save_tasks(session_id: str, items: list) -> list[str]:
    conn = get_conn()
    now = utcnow_iso()
    del_r = ("DELETE FROM prep_resource WHERE prep_task_id IN "
             "(SELECT id FROM prep_task WHERE session_id=? AND review_status='unreviewed')")
    assert_scope(del_r, "jobseeker")
    conn.execute(del_r, (session_id,))
    del_t = "DELETE FROM prep_task WHERE session_id=? AND review_status='unreviewed'"
    assert_scope(del_t, "jobseeker")
    conn.execute(del_t, (session_id,))
    ids: list[str] = []
    for i, t in enumerate(items):
        tid = new_id("ptsk")
        linked = _resolve_req_ids(session_id, t.get("requirement_refs", []))
        ins = ("INSERT INTO prep_task (id, session_id, priority, purpose, linked_requirement_ids, "
               "actions, suggested_hours, deliverable, self_check, learnability, is_practice_project, "
               "resources_note, status, seq, review_status, review_version, created_at, updated_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
        assert_scope(ins, "jobseeker")
        conn.execute(ins, (tid, session_id, t["priority"], t["purpose"], jdumps(linked),
                           jdumps(t["actions"]), t["suggested_hours"], t["deliverable"], t["self_check"],
                           t["learnability"], 1 if t["is_practice_project"] else 0,
                           "未检索到可核验资料（内置规则匹配器无检索能力）", "planned", i,
                           "unreviewed", 0, now, now))
        for r in t.get("resources", []):
            if r.get("verified_retrievable"):
                rins = ("INSERT INTO prep_resource (id, prep_task_id, title, url, publisher, "
                        "verified_retrievable, note, created_at) VALUES (?,?,?,?,?,?,?,?)")
                assert_scope(rins, "jobseeker")
                conn.execute(rins, (new_id("pres"), tid, r["title"], r.get("url"), r.get("publisher"),
                                    r.get("verified_retrievable", 0), r.get("note"), now))
        ids.append(tid)
    conn.commit()
    return ids


# ---------------------------------------------------------------------------
# 查询视图 / 任务状态
# ---------------------------------------------------------------------------

def focus_view(session_id: str) -> dict:
    session = query_one("SELECT * FROM jobseeker_session WHERE id=?", (session_id,))
    focus = query_all("SELECT * FROM interview_focus WHERE session_id=? ORDER BY seq, created_at",
                      (session_id,))
    for f in focus:
        f["questions"] = jloads(f["questions"], [])
        f["materials"] = jloads(f["materials"], [])
        f["uncertainties"] = jloads(f["uncertainties"], [])
        f["links"] = query_all("SELECT * FROM focus_link WHERE focus_id=? ORDER BY created_at", (f["id"],))
    model_label = session["model_label"] if session else None
    return {"focus": focus, "model_label": model_label,
            "research_background": interview_research.questions_view(session_id) if session else None,
            "demo": model_label == schemas.RULE_MATCHER_LABEL}


def prep_view(session_id: str) -> dict:
    tasks = query_all("SELECT * FROM prep_task WHERE session_id=? ORDER BY priority, seq", (session_id,))
    total = 0.0
    done = 0.0
    by_learnability: dict[str, float] = {}
    for t in tasks:
        t["linked_requirement_ids"] = jloads(t["linked_requirement_ids"], [])
        t["actions"] = jloads(t["actions"], [])
        t["resources"] = query_all("SELECT * FROM prep_resource WHERE prep_task_id=?", (t["id"],))
        hours = float(t["suggested_hours"] or 0)
        total += hours
        by_learnability[t["learnability"]] = by_learnability.get(t["learnability"], 0.0) + hours
        if t["status"] == "done":
            done += hours
    return {"tasks": tasks, "research_background": interview_research.questions_view(session_id),
            "summary": {"total_hours": total, "done_hours": done,
                                        "remaining_hours": max(0.0, total - done),
                                        "by_learnability": by_learnability}}


def set_task_status(task_id: str, status: str) -> dict:
    if status not in {p.value for p in schemas.PrepStatus}:
        raise PrepError("INVALID_ANALYSIS", "status 必须为 planned/in_progress/done")
    task = query_one("SELECT * FROM prep_task WHERE id=?", (task_id,))
    if not task:
        raise PrepError("INVALID_ANALYSIS", "任务不存在")
    conn = get_conn()
    sql = "UPDATE prep_task SET status=?, updated_at=? WHERE id=?"
    assert_scope(sql, "jobseeker")
    conn.execute(sql, (status, utcnow_iso(), task_id))
    conn.commit()
    t = query_one("SELECT * FROM prep_task WHERE id=?", (task_id,))
    t["linked_requirement_ids"] = jloads(t["linked_requirement_ids"], [])
    t["actions"] = jloads(t["actions"], [])
    return {"task": t}


# ---------------------------------------------------------------------------
# 内置规则生成器（确定性，model_label=rule-matcher-demo-v1）
# ---------------------------------------------------------------------------

_FOCUS_TITLES = {
    "business_scenario": "业务场景理解",
    "project_contribution": "项目实际职责与个人贡献",
    "decision_tradeoff": "决策与方案取舍",
    "result_metric": "结果指标与验证",
    "failure_improvement": "异常失败经历与改进",
    "transfer_ability": "迁移能力",
}


def rule_prep(prepared: dict) -> dict:
    """确定性规则实现：按匹配矩阵生成 6 类考察重点与准备任务。"""
    matching = prepared.get("matching", {})
    requirements = {r["id"]: r for r in matching.get("requirements", [])}
    rows = matching.get("rows", [])
    req_ids = [r["id"] for r in matching.get("requirements", [])]

    by_status: dict[str, list[dict]] = {}
    for r in rows:
        by_status.setdefault(r.get("match_status"), []).append(r)

    def link(req_id: str) -> dict:
        return {"side": "requirement", "ref_type": "requirement", "ref_id": req_id, "note": ""}

    def first_req(statuses: list[str]) -> Optional[str]:
        for st in statuses:
            for r in by_status.get(st, []):
                if r.get("requirement_id") in requirements:
                    return r["requirement_id"]
        return req_ids[0] if req_ids else None

    # 6 类考察重点（每类 5 要素齐全）
    focus_items: list[dict] = []
    specs = [
        ("business_scenario", ["direct_evidence", "transferable"],
         ["讲清目标岗位的核心业务流程与关键环节，说明你如何参与其中"],
         ["目标 JD 原文", "对应项目材料"], ["业务边界依赖 JD 表述，需本人补充实际场景细节"]),
        ("project_contribution", ["direct_evidence", "transferable"],
         ["说明你在项目中的实际职责与个人贡献，避免只讲团队不讲自己"],
         ["项目复盘笔记", "关键产出物"], ["个人贡献占比需本人据实确认"]),
        ("decision_tradeoff", ["direct_evidence", "transferable"],
         ["讲清你做过的方案取舍、被否决选项与原因"],
         ["方案对比记录", "决策依据"], ["推断的方案取舍需本人确认真实发生"]),
        ("result_metric", ["direct_evidence"],
         ["用可量化指标说明方案效果与验证方法"],
         ["效果数据", "验证口径"], ["指标口径是否可对外讲需本人确认"]),
        ("failure_improvement", ["direct_evidence", "transferable", "not_in_resume", "insufficient"],
         ["讲一次失败/异常经历与后续改进，体现复盘能力"],
         ["失败案例复盘"], ["具体失败案例需本人据实选择"]),
        ("transfer_ability", ["transferable", "not_in_resume"],
         ["说明旧经验如何迁移到目标行业/业务口径，并承认差异"],
         ["迁移逻辑梳理", "目标行业资料"], ["迁移是否成立需本人确认与面试方核实"]),
    ]
    for category, statuses, qs, mats, uncts in specs:
        req_id = first_req(statuses)
        if not req_id:
            continue
        focus_items.append({
            "category": category,
            "title": _FOCUS_TITLES[category],
            "why_asked": "这一类常见会被追问，用于验证候选人能否把经历讲清并落到目标岗位",
            "questions": [{"question": q, "followups": []} for q in qs],
            "materials": mats,
            "uncertainties": uncts,
            "links": [link(req_id)],
        })

    # 准备任务：按匹配状态 → learnability 三态
    prep_tasks: list[dict] = []
    prio = 0
    for row in rows:
        st = row.get("match_status")
        req_id = row.get("requirement_id")
        if not req_id or req_id not in requirements:
            continue
        req_text = requirements[req_id]["text"]
        prio += 1
        if st == "confirmed_gap":
            learn = "real_project_required"
            practice = False
            purpose = f"补齐缺口：{req_text[:40]}（本人已确认无该经历）"
        elif st == "not_in_resume":
            learn = "short_term_learnable"
            practice = True
            purpose = f"补齐能力：{req_text[:40]}（练习项目，非真实经历）"
        elif st == "insufficient":
            learn = "short_term_learnable"
            practice = True
            purpose = f"补充信息与基础：{req_text[:40]}（练习项目，非真实经历）"
        elif st == "transferable":
            learn = "needs_expression_practice"
            practice = False
            purpose = f"讲清迁移逻辑：{req_text[:40]}"
        else:  # direct_evidence
            learn = "needs_expression_practice"
            practice = False
            purpose = f"讲清已有经历：{req_text[:40]}"
        prep_tasks.append({
            "priority": min(99, prio),
            "purpose": purpose,
            "requirement_refs": [req_id],
            "actions": [_action_for(st)],
            "suggested_hours": 4.0 if st in ("not_in_resume", "insufficient", "confirmed_gap") else 2.0,
            "deliverable": "一份可复述的面试讲稿/复盘材料",
            "self_check": "能用自己的话讲清背景、动作、结果与局限",
            "learnability": learn,
            "is_practice_project": practice,
            "resources": [],
        })

    if not prep_tasks:
        prep_tasks.append({
            "priority": 1, "purpose": "梳理整体经历与求职方向",
            "requirement_refs": [], "actions": ["整理简历要点与目标岗位对应关系"],
            "suggested_hours": 2.0, "deliverable": "一页纸自我介绍", "self_check": "能 60 秒讲清自己与目标岗位的关联",
            "learnability": "needs_expression_practice", "is_practice_project": False, "resources": [],
        })

    return {"focus_items": focus_items, "prep_tasks": prep_tasks}


def _action_for(status: str) -> str:
    if status in ("not_in_resume", "insufficient", "confirmed_gap"):
        return "对照目标要求，完成一个可展示的练习项目并沉淀讲稿"
    if status == "transferable":
        return "写一份迁移说明：旧经验如何对应目标业务口径，明确差异"
    return "把该经历的背景、动作、结果、局限写成 3 分钟讲稿并练习复述"
