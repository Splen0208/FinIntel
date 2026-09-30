"""finintel analysis：分析往返与确定性校验（任务书 3.4/3.5）。

流程：prepare_analysis -> 宿主/规则分析器生成结构化分析 -> submit_analysis
     -> 本地严格校验（Schema + 证据偏移 + 快照归属 + 幂等）-> 入库。

校验失败返回字段级错误；引用正确只证明文字存在，不证明解释正确（3.5 第 9 条）。
"""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Optional

import ai_provider
import analyzer
import company as company_mod
import dedupe
import pack
import schemas
import store
from schemas import (AnalysisItem, SubmitPayload, TAXONOMY_VERSION)
from store import get_conn, jdumps, jloads, new_id, query_all, query_one, utcnow_iso

RULE_ANALYZER_LABEL = "rule-analyzer-demo-v1"

PROMPT_VERSION = "extract_v1"
SCHEMA_VERSION = "analysis_schema_v1"
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "extract_v1.md"


def is_generic_statement(statement: str) -> bool:
    """Only reject statements consisting entirely of known object-free boilerplate."""
    clauses = [s.strip() for s in re.split(r"[，,。；;、\n]+", statement) if s.strip()]
    generic = {"独立主导项目", "主导项目", "跨团队沟通", "协调资源", "对项目结果负责",
               "良好的沟通能力", "具有团队合作精神", "具备团队合作精神"}
    return bool(clauses) and all(s in generic for s in clauses)


class AnalysisError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False,
                 operator_action: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.operator_action = operator_action


def load_prompt() -> str:
    with open(PROMPT_PATH, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# prepare_analysis
# ---------------------------------------------------------------------------

def prepare_analysis(run_id: str, snapshot_ids: list[str]) -> dict[str, Any]:
    run = _require_run(run_id)
    if run["state"] in ("planned", "collecting"):
        raise AnalysisError("TOOL_NOT_ALLOWED", "采集尚未结束，先执行 finish_collection", False,
                            "在运行详情页结束采集阶段后再进入分析")
    if not snapshot_ids or len(snapshot_ids) > run["limits"].get("analysis_batch_size",
                                                                schemas.DEFAULT_BUDGET["analysis_batch_size"]):
        raise AnalysisError("INVALID_ANALYSIS",
                            f"snapshot_ids 数量需为 1~{run['limits'].get('analysis_batch_size', 5)}", False,
                            "减少每次分析的快照数")
    chunk_chars = run["limits"].get("analysis_chunk_chars", schemas.ANALYSIS_CHUNK_CHARS)

    conn = get_conn()
    token = new_id("batch")
    chunks: dict[str, list[dict]] = {}
    snapshots: list[dict] = []
    for sid in snapshot_ids:
        snap = query_one("SELECT * FROM job_snapshot WHERE id=?", (sid,))
        if not snap:
            raise AnalysisError("INVALID_ANALYSIS", f"快照 {sid} 不存在", False, "检查 snapshot_id")
        rj = query_one(
            """SELECT analysis_error FROM run_job WHERE run_id=? AND job_id=? AND snapshot_id=?""",
            (run_id, snap["job_id"], sid))
        if not rj:
            raise AnalysisError("TOOL_NOT_ALLOWED",
                                f"快照 {sid} 不属于运行 {run_id}（不得跨批次引用未提供的快照）", False,
                                "只分析本运行采集到的快照")
        if snap["detail_status"] not in ("complete", "partial"):
            raise AnalysisError("DETAIL_MISSING",
                                f"快照 {sid} 状态为 {snap['detail_status']}，无可分析正文", False,
                                "正文缺失/失败的岗位进入异常素材，不用列表摘要假装完整职责（AC-08）")
        text = snap["evidence_text"]
        text_chunks = []
        if len(text) <= chunk_chars:
            text_chunks.append({"start": 0, "end": len(text), "text": text})
        else:
            pos = 0
            while pos < len(text):
                end = min(pos + chunk_chars, len(text))
                text_chunks.append({"start": pos, "end": end, "text": text[pos:end]})
                pos = end
        chunks[sid] = text_chunks
        view = _snapshot_view(snap)
        previous_error = jloads(rj.get("analysis_error"), None)
        if previous_error:
            view["validation_feedback"] = previous_error
        snapshots.append(view)

    conn.execute(
        """INSERT INTO analysis_batch (token, run_id, snapshot_ids, prompt_version,
           taxonomy_version, chunks, created_at) VALUES (?,?,?,?,?,?,?)""",
        (token, run_id, jdumps(snapshot_ids), PROMPT_VERSION, TAXONOMY_VERSION,
         jdumps(chunks), utcnow_iso()),
    )
    conn.commit()
    if run["state"] in ("ready_for_analysis", "partial"):
        _set_run_state(run_id, "analyzing")

    return {
        "batch_token": token,
        "prompt_version": PROMPT_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "instructions": load_prompt(),
        "snapshots": snapshots,
        "chunks": chunks,
    }


def _snapshot_view(snap: dict) -> dict:
    job = query_one("SELECT * FROM job WHERE id=?", (snap["job_id"],))
    comp = query_one("SELECT * FROM company WHERE id=?", (job["company_id"],)) if job and job["company_id"] else None
    return {
        "snapshot_id": snap["id"],
        "job_id": snap["job_id"],
        "title": job["title"] if job else "",
        "company_name": job["company_name_raw"] if job else "",
        "company_display_name": comp["display_name"] if comp else "归属待核实",
        "city": job["city"] if job else None,
        "detail_status": snap["detail_status"],
        "fetched_at": snap["fetched_at"],
        "published_at_raw": snap["published_at_raw"],
        "source_url": snap["source_url"],
        "evidence_text": snap["evidence_text"],
        "responsibilities_text": snap["responsibilities_text"],
        "requirements_text": snap["requirements_text"],
    }


def _require_run(run_id: str) -> dict:
    import runs as runs_mod
    return runs_mod.get_run(run_id)


def _set_run_state(run_id: str, state: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE research_run SET state=? WHERE id=?", (state, run_id))
    conn.commit()


# ---------------------------------------------------------------------------
# submit_analysis：严格校验与入库
# ---------------------------------------------------------------------------

def submit_analysis(payload_dict: dict) -> dict[str, Any]:
    """入口：dict -> Pydantic 严格校验 -> 语义校验 -> 入库。"""
    try:
        payload = SubmitPayload(**payload_dict)
    except Exception as e:  # pydantic ValidationError
        raise AnalysisError("INVALID_ANALYSIS", f"Schema 校验失败：{e}", True,
                            "宿主最多修正提交 1 次；仍失败将标记 analysis_failed")

    try:
        batch = pack.require_batch(payload.batch_token, "company_finding")
    except pack.PackError as e:
        raise AnalysisError(e.code, e.message, e.retryable, e.operator_action)
    allowed_snapshots = set(jloads(batch["snapshot_ids"], []))

    # 幂等：相同 submission_id 同 payload 返回相同结果；不同 payload 冲突（AC-31）
    payload_hash = pack.hash_payload([a.model_dump() for a in payload.analyses])
    try:
        prior = pack.idempotent_result(payload.submission_id, payload_hash)
    except pack.PackError as e:
        raise AnalysisError(e.code, e.message, e.retryable, e.operator_action)
    if prior is not None:
        return {"idempotent": True, "result": prior, "outcomes": prior.get("outcomes", [])}

    outcomes: list[dict[str, Any]] = []
    ok_count = 0
    returned_ids = [item.snapshot_id for item in payload.analyses]
    for sid in sorted(allowed_snapshots - set(returned_ids)):
        errors = [{"code": "AI_OUTPUT_MISSING", "field": "snapshot_id", "message": "模型未返回该岗位的分析"}]
        outcomes.append({"snapshot_id": sid, "ok": False, "errors": errors})
    handled = set()
    for item in payload.analyses:
        if item.snapshot_id in handled:
            continue
        handled.add(item.snapshot_id)
        errors = _validate_item(item, allowed_snapshots)
        if returned_ids.count(item.snapshot_id) != 1:
            errors.append({"code": "AI_OUTPUT_DUPLICATE", "field": "snapshot_id", "message": "模型重复返回同一岗位"})
        if errors:
            outcomes.append({"snapshot_id": item.snapshot_id, "ok": False, "errors": errors})
            continue
        item.findings = [f for f in item.findings if not is_generic_statement(f.statement)]
        if not item.findings and item.result_status.value == "findings_found":
            item.result_status = schemas.ResultStatus.insufficient_information
        saved = _save_analysis(batch, item, payload.model_label, payload.submission_id)
        outcomes.append({"snapshot_id": item.snapshot_id, "ok": True,
                         "analysis_id": saved["analysis_id"],
                         "finding_ids": saved["finding_ids"],
                         "result_status": item.result_status.value})
        ok_count += 1

    conn = get_conn()
    for outcome in outcomes:
        if outcome["snapshot_id"] not in allowed_snapshots:
            continue
        conn.execute(
            "UPDATE run_job SET analysis_id=?, analysis_error=? WHERE run_id=? AND snapshot_id=?",
            (outcome.get("analysis_id") if outcome["ok"] else None,
             None if outcome["ok"] else jdumps(outcome["errors"]),
             batch["run_id"], outcome["snapshot_id"]),
        )
    conn.commit()
    result = {"outcomes": outcomes, "ok_count": ok_count,
              "failed_count": len(outcomes) - ok_count}
    pack.record_submission(payload.submission_id, payload.batch_token, payload_hash, result)
    pack.consume_batch(payload.batch_token)

    # 分析入库后：生成疑似重复对并重算支持程度（任务书 3.6）
    dedupe.generate_pairs_for_run(batch["run_id"])
    dedupe.update_support_levels(batch["run_id"])
    return result


def _validate_item(item: AnalysisItem, allowed_snapshots: set) -> list[dict]:
    """字段级语义校验，返回错误列表（空 = 通过）。"""
    errors: list[dict] = []

    if item.snapshot_id not in allowed_snapshots:
        errors.append({"field": "snapshot_id",
                       "code": "TOOL_NOT_ALLOWED",
                       "message": f"快照 {item.snapshot_id} 不在本分析包内（不得跨批次引用）"})
        return errors

    snap = query_one("SELECT * FROM job_snapshot WHERE id=?", (item.snapshot_id,))
    if not snap:
        return [{"field": "snapshot_id", "code": "INVALID_ANALYSIS", "message": "快照不存在"}]

    evidence_text = snap["evidence_text"]
    _, ranges = analyzer.build_evidence_text(snap["responsibilities_text"] or "",
                                             snap["requirements_text"] or "")
    resp_range = ranges["responsibility"]
    req_range = ranges["requirement"]

    # 数量与状态一致性（任务书 2.4 / 3.5）
    if item.result_status == schemas.ResultStatus.findings_found and not item.findings:
        errors.append({"field": "findings", "code": "INVALID_ANALYSIS",
                       "message": "findings_found 要求至少一项事项"})
    if item.result_status in (schemas.ResultStatus.insufficient_information,
                              schemas.ResultStatus.excluded) and item.findings:
        errors.append({"field": "findings", "code": "INVALID_ANALYSIS",
                       "message": f"{item.result_status.value} 要求零事项，不能为了填充报表强制生成"})
    if item.role_class in ("sales", "other") or item.relevance == "irrelevant":
        if item.result_status != schemas.ResultStatus.excluded and item.findings:
            errors.append({"field": "result_status", "code": "INVALID_ANALYSIS",
                           "message": "排除类别的岗位不得写入正常公司事项（保留排除理由即可）"})

    for fi, f in enumerate(item.findings):
        for ei, ev in enumerate(f.evidence):
            prefix = f"findings[{fi}].evidence[{ei}]"
            err = pack.validate_quote(
                {"quote": ev.quote, "start": ev.start, "end": ev.end,
                 "section": ev.section.value},
                evidence_text, prefix,
                section_ranges={"responsibility": resp_range, "requirement": req_range},
                forbid_cross_section=True)
            if err:
                errors.append(err)
        if not f.evidence:
            errors.append({"field": f"findings[{fi}].evidence", "code": "INVALID_EVIDENCE",
                           "message": "每项有效事项至少一个证据片段（没有职责证据不得发布 finding）"})
        if not f.limitations:
            errors.append({"field": f"findings[{fi}].limitations", "code": "INVALID_ANALYSIS",
                           "message": "局限必填：默认包含无法确认真实项目进展的说明"})
    return errors


def _normalize_statement_key(statement: str) -> str:
    """合并键规范化：只处理空白和等价标点，不改写语义（任务书 3.6）。"""
    import re
    s = statement.strip()
    s = re.sub(r"\s+", "", s)
    s = s.replace("（", "(").replace("）", ")").replace("，", ",")
    return s.lower()


def _save_analysis(batch, item: AnalysisItem, model_label: str, submission_id: str) -> dict:
    conn = get_conn()
    snap = query_one("SELECT * FROM job_snapshot WHERE id=?", (item.snapshot_id,))
    job = query_one("SELECT * FROM job WHERE id=?", (snap["job_id"],))

    analysis_id = new_id("ana")
    status = "validated" if item.result_status != "needs_review" else "partial"
    conn.execute(
        """INSERT INTO analysis (id, snapshot_id, schema_version, prompt_version,
           taxonomy_version, model_label, role_class, relevance, reason, status,
           result_status, submission_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (analysis_id, item.snapshot_id, SCHEMA_VERSION, PROMPT_VERSION, TAXONOMY_VERSION,
         model_label, item.role_class.value, item.relevance.value,
         item.classification_reason, status, item.result_status.value,
         submission_id, utcnow_iso()))

    finding_ids: list[str] = []
    for f in item.findings:
        finding_id = new_id("fnd")
        merged_key = "|".join([
            job["company_id"] or "-", f.business_domain,
            _normalize_statement_key(f.statement), f.action_type.value,
            f.audience or "-", f.market or "-",
            ",".join(sorted(f.product_names)),
        ])
        conn.execute(
            """INSERT INTO finding (id, analysis_id, statement, business_domain, action_type,
               audience, market, product_names, interpretation, limitations, merged_key, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (finding_id, analysis_id, f.statement, f.business_domain, f.action_type.value,
             f.audience, f.market, jdumps(f.product_names), f.interpretation,
             jdumps(list(dict.fromkeys(f.limitations + (
                ["正文完整性未确认，结论仅基于已取得片段"] if snap["detail_status"] != "complete" else [])))),
             merged_key, utcnow_iso()))
        for ev in f.evidence:
            conn.execute(
                "INSERT INTO evidence (id, finding_id, snapshot_id, section, quote, start, end) VALUES (?,?,?,?,?,?,?)",
                (new_id("ev"), finding_id, item.snapshot_id, ev.section.value, ev.quote,
                 ev.start, ev.end))
        finding_ids.append(finding_id)
    conn.commit()
    return {"analysis_id": analysis_id, "finding_ids": finding_ids}


# ---------------------------------------------------------------------------
# 查询视图
# ---------------------------------------------------------------------------

def list_analyses(run_id: Optional[str] = None) -> list[dict]:
    sql = """SELECT a.*, j.title, j.company_name_raw, j.company_id, s.fetched_at
             FROM analysis a
             JOIN job_snapshot s ON a.snapshot_id = s.id
             JOIN job j ON s.job_id = j.id"""
    params: tuple = ()
    if run_id:
        sql += " WHERE a.id IN (SELECT analysis_id FROM run_job WHERE run_id=?)"
        params = (run_id,)
    sql += " ORDER BY a.created_at DESC"
    rows = query_all(sql, params)
    for r in rows:
        r["finding_count"] = query_one(
            "SELECT COUNT(*) AS n FROM finding WHERE analysis_id=?", (r["id"],))["n"]
    return rows


def finding_detail(finding_id: str) -> Optional[dict]:
    f = query_one(
        """SELECT f.*, a.role_class, a.relevance, a.reason, a.model_label,
                  j.title, j.company_name_raw, j.company_id, s.evidence_text, s.fetched_at
           FROM finding f JOIN analysis a ON f.analysis_id=a.id
           JOIN job_snapshot s ON a.snapshot_id=s.id
           JOIN job j ON s.job_id=j.id WHERE f.id=?""", (finding_id,))
    if not f:
        return None
    f["product_names"] = jloads(f["product_names"], [])
    f["limitations"] = jloads(f["limitations"], [])
    f["evidences"] = query_all(
        "SELECT * FROM evidence WHERE finding_id=? ORDER BY start", (finding_id,))
    return f


def company_findings(company_id: str, include_excluded: bool = False) -> list[dict]:
    sql = """SELECT f.*, a.role_class, a.relevance, a.result_status,
                    j.title, j.company_name_raw, j.city, s.id AS snapshot_id, s.fetched_at
             FROM finding f
             JOIN analysis a ON f.analysis_id = a.id
             JOIN job_snapshot s ON a.snapshot_id = s.id
             JOIN job j ON s.job_id = j.id
             WHERE j.company_id = ?"""
    if not include_excluded:
        sql += " AND f.review_status != 'excluded'"
    sql += " ORDER BY f.business_domain, f.created_at"
    rows = query_all(sql, (company_id,))
    for r in rows:
        r["product_names"] = jloads(r["product_names"], [])
        r["limitations"] = jloads(r["limitations"], [])
        r["evidences"] = query_all(
            "SELECT * FROM evidence WHERE finding_id=? ORDER BY start", (r["id"],))
    return rows


def auto_analyze(run_id: str, snapshot_ids: list[str]) -> dict:
    """工作事项抽取：真实模型优先；显式 demo 模式才走规则分析器；未配置凭据绝不回退。

    - 配置了模型 → 真实模型抽取（引用偏移仍由 submit_analysis 严格校验）
    - 未配置 + demo_mode=1 → 内置规则分析器（model_label=rule-analyzer-demo-v1，仅演示）
    - 未配置且非 demo → AI_NOT_CONFIGURED（不回退为假结果）
    """
    batch = prepare_analysis(run_id, snapshot_ids)
    if ai_provider.ai_config()["configured"]:
        return analyze_via_model(batch)
    if ai_provider.demo_enabled():
        analyses = [analyzer.analyze_snapshot(s) for s in batch["snapshots"]]
        submission = {
            "batch_token": batch["batch_token"],
            "submission_id": new_id("sub"),
            "model_label": RULE_ANALYZER_LABEL,
            "analyses": analyses,
        }
        return submit_analysis(submission)
    raise AnalysisError(
        "AI_NOT_CONFIGURED",
        "AI 服务未配置，无法执行工作事项抽取（不回退为内置规则结果）", False,
        "在设置页配置模型后重试；或开启 demo_mode 使用规则分析器（仅演示）")


def analyze_via_model(batch: dict) -> dict:
    """调用真实模型抽取工作事项，走 submit_analysis 严格校验（Schema + 引用偏移）后入库。"""
    user_content = _batch_to_user_content(batch)
    try:
        raw = ai_provider.chat_json(load_prompt(), user_content, temperature=0.0)
    except ai_provider.AIError as e:
        raise AnalysisError(e.code, e.message, e.retryable, e.operator_action)
    analyses = raw.get("analyses") if isinstance(raw, dict) else None
    if not isinstance(analyses, list) or not analyses:
        raise AnalysisError("AI_OUTPUT_INVALID", "模型输出缺少 analyses 数组", True,
                            "重试或检查模型是否按 Schema 输出")
    # 引用位置修正：模型对中文 code point 常有 off-by-N，只修位置、不放宽真实性
    text_by_snap = {s.get("snapshot_id"): (s.get("evidence_text") or "")
                    for s in batch.get("snapshots", [])}
    repaired = 0
    for a in analyses:
        if not isinstance(a, dict):
            continue
        text = text_by_snap.get(a.get("snapshot_id"), "")
        for f in (a.get("findings") or []):
            if isinstance(f, dict):
                repaired += pack.repair_quotes_in_list(f.get("evidence") or [], text)
    cfg = ai_provider.ai_config()
    submission = {
        "batch_token": batch["batch_token"],
        "submission_id": new_id("sub"),
        "model_label": f"{cfg['provider']}:{cfg['model']}",
        "analyses": analyses,
    }
    result = submit_analysis(submission)
    if repaired:
        result["repaired_offsets"] = repaired
    return result


def _batch_to_user_content(batch: dict) -> str:
    """把分析包序列化为给模型的用户输入（证据文本 + 段落区间 + 快照 ID）。"""
    items = []
    for s in batch.get("snapshots", []):
        items.append({
            "snapshot_id": s.get("snapshot_id"),
            "title": s.get("title"),
            "company_name": s.get("company_name"),
            "company_display_name": s.get("company_display_name"),
            "detail_status": s.get("detail_status"),
            "evidence_text": s.get("evidence_text"),
            "responsibilities_text": s.get("responsibilities_text"),
            "requirements_text": s.get("requirements_text"),
        })
        if s.get("validation_feedback"):
            items[-1]["validation_feedback"] = s["validation_feedback"]
    return (jdumps({"snapshots": items}) +
            "\n\n请返回一个 JSON 对象，形如 {\"analyses\": [...]}，"
            "数组内每个元素对应一个输入 snapshot（snapshot_id 必须与输入完全一致），"
            "字段严格遵循系统提示中的输出 Schema；evidence 的 start/end 为相对该快照 "
            "evidence_text 的 code point 半开区间，quote 必须与切片逐字一致。"
            "若输入含 validation_feedback，表示上次输出未通过服务校验，请修正对应字段。"
            "销售或其他岗位应 result_status=excluded 且 findings=[]，保留真实分类和排除理由；"
            "不得为了通过校验改变岗位分类或编造事项。")
