"""finintel FastAPI 入口：将 9 个 MCP 工具契约暴露为本地 HTTP API。

统一信封（任务书 4.4）：{ok, data, error, warnings, meta}
error 至少包含 code / message / retryable / operator_action。
部分成功由 data.outcomes 与 meta.completeness 表达。
"""
from __future__ import annotations

import sys
import os
from pathlib import Path
from typing import Any, Optional

# 保证 backend 目标可导入（无论从哪里启动）
BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import analysis
import ai_provider
import channel_state
import classify
import company as company_mod
import dedupe
import health as health_mod
import login_bridge
import interview_research
import matching
import pipeline
import prep as prep_mod
import reports
import resume as resume_mod
import resume_facts
import review as review_mod
import runs as runs_mod
import schemas
import social_analysis
import social_pipeline
import social_redact
import social_source_status
import social_store
import source_boss
import store
import targets
import upstream_runtime
from upstream_config import (
    LOGIN_DEFAULT_TIMEOUT_SECONDS,
    LOGIN_TIMEOUT_BOUNDS,
    env_proxy_stripped,
    install_command_hint,
    resolve_cli_path,
    resolve_upstream_data_dir,
    validate_browser_option,
)
from store import jloads, query_all, query_one

app = FastAPI(title="finintel 消费信贷招聘情报工具", version="1.0.0-p1")

# 登录成功回调在下方「三步主流程」段统一挂载（先做有界通道预检，再自动继续）。


# ---------------------------------------------------------------------------
# 统一信封
# ---------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False,
                 operator_action: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.operator_action = operator_action


def ok(data: Any, warnings: Optional[list] = None, meta: Optional[dict] = None) -> dict:
    return {"ok": True, "data": data, "error": None,
            "warnings": warnings or [], "meta": meta or {}}


def err(code: str, message: str, retryable: bool = False, operator_action: str = "",
        *, upstream_code: Optional[str] = None, layer: Optional[str] = None) -> dict:
    error = {"code": code, "message": message, "retryable": retryable,
             "operator_action": operator_action}
    if upstream_code:
        error["upstream_code"] = upstream_code
    if layer:
        error["layer"] = layer
    return {"ok": False, "data": None, "error": error,
            "warnings": [], "meta": {}}


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse(status_code=200, content=err(exc.code, exc.message, exc.retryable, exc.operator_action))


@app.on_event("startup")
def startup():
    applied = store.apply_migrations()
    if applied:
        print(f"[finintel] 已应用迁移: {applied}", file=sys.stderr)
    login_bridge.controller.recover_pending()
    # 服务重启语义（设计 D10 / UX-12）：未完成任务置 paused，绝不自动重新采集
    social_pipeline.pipeline.recover_pending()
    # X 凭据：把已保存的设置灌进进程环境（X 适配器只读环境变量）。
    # 幂等，且**绝不清除**同名环境变量——用户在 shell / 部署环境里配好的凭据
    # 必须原样保留；只有用户显式清空设置项（POST /api/settings 传空值）才会清除。
    social_source_status.load_credentials_from_settings()
    recovered = pipeline.pipeline.recover_pending()
    if recovered:
        print(f"[finintel] 服务重启：{len(recovered)} 个任务已置为暂停，等待用户继续",
              file=sys.stderr)
    # 运行实例自证：让"页面是不是最新代码/跑在哪个端口"由进程自己回答
    rt = upstream_runtime.write_server_json("backend/app.py")
    if rt.get("port") and sys.platform == "win32":
        import managed_browser
        login_bridge.controller.browser_provider = managed_browser.ensure
    if rt.get("port"):
        print(f"[finintel] 运行实例: pid={rt.get('pid')} port={rt.get('port')} "
              f"server.json={upstream_runtime.SERVER_JSON}", file=sys.stderr)
    else:
        print("[finintel] 未解析到监听端口（测试/脚本导入），保留既有 server.json 记录",
              file=sys.stderr)


# ---------------------------------------------------------------------------
# BOSS 真实接入分层健康状态
# ---------------------------------------------------------------------------

def _ai_layer() -> dict:
    """Return AI configuration status without exposing a secret value."""
    cfg = ai_provider.ai_config()
    if cfg["configured"]:
        return {"status": "ok",
                "detail": f"已配置 provider={cfg['provider']} model={cfg['model']}；"
                          "简历事实提取/岗位匹配/面试准备/工作事项抽取均走真实模型",
                "demo_mode": cfg["demo_mode"]}
    missing = []
    if not cfg.get("base_url"):
        missing.append("llm_api_base")
    if not cfg.get("model"):
        missing.append("llm_model")
    if not cfg.get("has_api_key"):
        missing.append("llm_api_key")
    detail = (f"AI 服务未就绪：缺 {'、'.join(missing)}" if missing else "AI 服务未配置")
    return {"status": "unavailable",
            "detail": detail + "；未配置时四类 AI 入口返回 AI_NOT_CONFIGURED，不回退为规则结果",
            "missing": missing, "demo_mode": cfg["demo_mode"]}


def _health_stats() -> dict[str, int]:
    return {
        "companies": query_one("SELECT COUNT(*) AS n FROM company")["n"],
        "jobs": query_one("SELECT COUNT(*) AS n FROM job")["n"],
        "jobs_real": query_one("SELECT COUNT(*) AS n FROM job WHERE source!='demo'")["n"],
        "jobs_demo": query_one("SELECT COUNT(*) AS n FROM job WHERE source='demo'")["n"],
        "snapshots": query_one("SELECT COUNT(*) AS n FROM job_snapshot")["n"],
        "snapshots_real": query_one(
            "SELECT COUNT(*) AS n FROM job_snapshot s JOIN job j ON j.id=s.job_id WHERE j.source!='demo'"
        )["n"],
        "snapshots_demo": query_one(
            "SELECT COUNT(*) AS n FROM job_snapshot s JOIN job j ON j.id=s.job_id WHERE j.source='demo'"
        )["n"],
        "analyses": query_one("SELECT COUNT(*) AS n FROM analysis")["n"],
        "findings": query_one("SELECT COUNT(*) AS n FROM finding WHERE review_status!='excluded'")["n"],
        "runs": query_one("SELECT COUNT(*) AS n FROM research_run")["n"],
        "reports": query_one("SELECT COUNT(*) AS n FROM report")["n"],
        "resumes": query_one("SELECT COUNT(*) AS n FROM resume WHERE deleted_at IS NULL")["n"],
        "sessions": query_one("SELECT COUNT(*) AS n FROM jobseeker_session")["n"],
    }


def _health_labels() -> dict[str, dict]:
    return {
        "role_class": schemas.ROLE_CLASS_LABELS,
        "relevance": schemas.RELEVANCE_LABELS,
        "action_type": schemas.ACTION_TYPE_LABELS,
        "result_status": schemas.RESULT_STATUS_LABELS,
        "detail_status": schemas.DETAIL_STATUS_LABELS,
        "run_state": schemas.RUN_STATE_LABELS,
        "support_level": schemas.SUPPORT_LEVEL_LABELS,
        "review_status": schemas.REVIEW_STATUS_LABELS,
        "usage_mode": schemas.USAGE_MODE_LABELS,
        "match_status": schemas.MATCH_STATUS_LABELS,
        "requirement_category": schemas.REQUIREMENT_CATEGORY_LABELS,
        "evidence_side": schemas.EVIDENCE_SIDE_LABELS,
        "parse_status": schemas.PARSE_STATUS_LABELS,
        "focus_category": schemas.FOCUS_CATEGORY_LABELS,
        "learnability": schemas.LEARNABILITY_LABELS,
        "experience_level": schemas.EXPERIENCE_LEVEL_LABELS,
        "capability_type": schemas.CAPABILITY_TYPE_LABELS,
        "frequency_class": schemas.FREQUENCY_CLASS_LABELS,
        "prep_status": schemas.PREP_STATUS_LABELS,
        "session_status": schemas.SESSION_STATUS_LABELS,
        "confirmation_status": schemas.CONFIRMATION_STATUS_LABELS,
    }


def _health_versions() -> dict[str, str]:
    return {
        "taxonomy": schemas.TAXONOMY_VERSION,
        "prompt": "extract_v1",
        "schema": "analysis_schema_v1",
        "mapping": schemas.MAPPING_VERSION,
        "upstream": "boss-agent-cli==2.0.0",
        "website_channel": "CLI",
        "mcp_role": "独立协议验证，不参与网站采集链路",
        "match_prompt": schemas.MATCH_PROMPT_VERSION,
        "prep_prompt": schemas.PREP_PROMPT_VERSION,
        "match_schema": schemas.MATCH_SCHEMA_VERSION,
        "prep_schema": schemas.PREP_SCHEMA_VERSION,
        "facts_prompt": resume_facts.FACTS_PROMPT_VERSION,
        "facts_schema": resume_facts.FACTS_SCHEMA_VERSION,
    }


def _health_payload() -> dict[str, Any]:
    data_dir = resolve_upstream_data_dir()
    layers = health_mod.cached_layer_view()
    state, missing = health_mod.integration_state()
    cli_evidence = layers["cli_path"].evidence
    data_dir_echo = cli_evidence.get("data_dir_echo")
    consistent = None
    if data_dir_echo:
        from upstream_config import same_path

        consistent = same_path(data_dir_echo, data_dir.path)
    notice = "BOSS 真实接入证据已齐全" if state == "ready" else (
        "离线演示可用；BOSS 真实接入未完成（缺：" + "、".join(missing) + "）"
    )
    return {
        "layers": {key: value.as_view() for key, value in layers.items()},
        "data_dir": {
            **data_dir.as_view(),
            "upstream_echo": data_dir_echo,
            "consistent": consistent,
        },
        "integration_state": state,
        "integration_missing": missing,
        "notice": notice,
        "stats": _health_stats(),
        "versions": _health_versions(),
        "taxonomy": schemas.TAXONOMY,
        "labels": _health_labels(),
        "ai": _ai_layer(),
        "runtime": upstream_runtime.runtime_block(),
        # 诊断页才给路径与脱敏后的失败原因（普通 UI 只看状态）
        "channel_diagnostic": channel_state.channel_diagnostic_view(),
        # 诊断：当前环境是否注入了回环代理（会被从上游子进程删除，以保证出口与浏览器一致）
        "env_proxy_stripped": env_proxy_stripped(),
    }


@app.get("/api/health")
def health(live: bool = False):
    """Read cached health only; even legacy live=1 never calls search or detail."""
    if live:
        configured = query_one("SELECT value FROM app_setting WHERE key='boss_cli_path'")
        cli_path = configured["value"] if configured else None
        data_dir = resolve_upstream_data_dir()
        try:
            runs_mod._run_async(health_mod.probe(cli_path, data_dir, ["cli_path"], 60))
        except (ValueError, source_boss.UpstreamError):
            pass
    return ok(_health_payload())


class HealthProbeReq(BaseModel):
    layers: list[str] = ["cli_path", "local_credential"]
    timeout_seconds: int = 60


@app.post("/api/health/probe")
async def health_probe(req: HealthProbeReq):
    """Run only explicitly selected doctor/status probes; never search or detail."""
    configured = query_one("SELECT value FROM app_setting WHERE key='boss_cli_path'")
    cli_path = configured["value"] if configured else None
    data_dir = resolve_upstream_data_dir()
    try:
        states = await health_mod.probe(cli_path, data_dir, req.layers, req.timeout_seconds)
    except ValueError as exc:
        return err("TOOL_NOT_ALLOWED", str(exc), operator_action="仅允许探测 cli_path/local_credential/online_auth")
    return ok({
        "probed": {key: state.as_view() for key, state in states.items()},
        "health": _health_payload(),
    })


# ---------------------------------------------------------------------------
# intel_start_run
# ---------------------------------------------------------------------------

class RunConfig(BaseModel):
    name: str
    source: str = "zhipin"
    queries: list[str]
    cities: list[str] = []
    company_watchlist: list[str] = []
    max_search_calls: int = 6
    max_detail_calls: int = 20
    call_timeout_seconds: int = 120
    details_per_call: int = 3
    analysis_batch_size: int = 5


@app.post("/api/runs")
def start_run(cfg: RunConfig):
    try:
        run = runs_mod.create_run(cfg.name, cfg.model_dump())
    except runs_mod.RunError as e:
        return err(e.code, e.message, e.retryable, e.operator_action,
                   upstream_code=e.upstream_code, layer=e.layer)
    return ok({"run": run, "query_plan": run["config_snapshot"]["queries"],
               "budget": run["limits"],
               "coverage": "按配置查询词与预算采集，不声称穷尽行业招聘"})


@app.get("/api/runs")
def list_runs():
    return ok({"runs": runs_mod.list_runs()})


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    try:
        return ok(runs_mod.run_status(run_id))
    except KeyError:
        return err("INVALID_ANALYSIS", f"运行 {run_id} 不存在")


@app.get("/api/runs/{run_id}/calls")
def run_calls(run_id: str):
    """Return credential-redacted upstream calls for audit and reproduction."""
    try:
        runs_mod.get_run(run_id)
    except KeyError:
        return err("INVALID_ANALYSIS", f"运行 {run_id} 不存在")
    return ok({"calls": runs_mod.list_upstream_calls(run_id)})


# ---------------------------------------------------------------------------
# intel_search
# ---------------------------------------------------------------------------

class SearchReq(BaseModel):
    query_id: str
    page: int = 1


@app.post("/api/runs/{run_id}/search")
def search(run_id: str, req: SearchReq):
    try:
        return ok(runs_mod.do_search(run_id, req.query_id, req.page))
    except runs_mod.RunError as e:
        return err(e.code, e.message, e.retryable, e.operator_action,
                   upstream_code=e.upstream_code, layer=e.layer)


# ---------------------------------------------------------------------------
# intel_fetch_details
# ---------------------------------------------------------------------------

class DetailsReq(BaseModel):
    job_refs: list[str]


@app.post("/api/runs/{run_id}/details")
def fetch_details(run_id: str, req: DetailsReq):
    try:
        result = runs_mod.fetch_details(run_id, req.job_refs)
        return ok(result, meta={"completeness": result["completeness"]})
    except runs_mod.RunError as e:
        return err(e.code, e.message, e.retryable, e.operator_action,
                   upstream_code=e.upstream_code, layer=e.layer)


# ---------------------------------------------------------------------------
# intel_prepare_analysis / intel_submit_analysis
# ---------------------------------------------------------------------------

class PrepareReq(BaseModel):
    snapshot_ids: list[str]


@app.post("/api/runs/{run_id}/prepare-analysis")
def prepare_analysis(run_id: str, req: PrepareReq):
    try:
        return ok(analysis.prepare_analysis(run_id, req.snapshot_ids))
    except analysis.AnalysisError as e:
        return err(e.code, e.message, e.retryable, e.operator_action)


@app.post("/api/runs/{run_id}/submit-analysis")
async def submit_analysis(run_id: str, request: Request):
    try:
        payload = await request.json()
    except Exception:
        return err("INVALID_ANALYSIS", "请求体必须是 JSON")
    try:
        return ok(analysis.submit_analysis(payload))
    except analysis.AnalysisError as e:
        return err(e.code, e.message, e.retryable, e.operator_action)


class AutoAnalyzeReq(BaseModel):
    snapshot_ids: list[str]


@app.post("/api/runs/{run_id}/auto-analyze")
def auto_analyze(run_id: str, req: AutoAnalyzeReq):
    """演示模式：内置规则分析器走完整 prepare->submit 链路（model_label 明确标注 demo）。"""
    try:
        result = analysis.auto_analyze(run_id, req.snapshot_ids)
        return ok(result, warnings=["结果由内置规则分析器生成（rule-analyzer-demo-v1），"
                                    "语义准确性需人工复核，不代表宿主模型质量"])
    except (analysis.AnalysisError, runs_mod.RunError) as e:
        return err(getattr(e, "code", "INVALID_ANALYSIS"), str(e),
                   getattr(e, "retryable", False), getattr(e, "operator_action", ""))


class RunControlReq(BaseModel):
    reason: str = "用户手动停止"


@app.post("/api/runs/{run_id}/control/{action}")
def run_control(run_id: str, action: str, req: RunControlReq = None):
    try:
        if action == "status":
            return ok(runs_mod.run_status(run_id))
        if action == "stop":
            return ok(runs_mod.stop_run(run_id, req.reason if req else "用户手动停止"))
        if action == "finish_collection":
            return ok(runs_mod.finish_collection(run_id))
        if action == "resume_collection":
            return ok(runs_mod.resume_collection(run_id))
        return err("TOOL_NOT_ALLOWED", f"未知控制动作: {action}",
                   operator_action="动作限定为 status/stop/finish_collection/resume_collection")
    except runs_mod.RunError as e:
        return err(e.code, e.message, e.retryable, e.operator_action,
                   upstream_code=e.upstream_code, layer=e.layer)
    except KeyError:
        return err("INVALID_ANALYSIS", f"运行 {run_id} 不存在")


@app.get("/api/runs/{run_id}/analyses")
def run_analyses(run_id: str):
    return ok({"analyses": analysis.list_analyses(run_id)})


@app.get("/api/runs/{run_id}/pending-snapshots")
def pending_snapshots(run_id: str):
    rows = query_all(
        """SELECT s.id, j.title, j.company_name_raw, j.city, s.detail_status, s.fetched_at
           FROM run_job rj
           JOIN job_snapshot s ON s.id = rj.snapshot_id
           JOIN job j ON j.id = rj.job_id
           WHERE rj.run_id=? AND rj.detail_outcome IN ('complete','partial','reused')
             AND NOT EXISTS (SELECT 1 FROM analysis a WHERE a.snapshot_id=rj.snapshot_id)""",
        (run_id,))
    analyzed = query_all(
        """SELECT s.id, j.title, j.company_name_raw, a.result_status, a.model_label
           FROM run_job rj
           JOIN job_snapshot s ON s.id = rj.snapshot_id
           JOIN job j ON j.id = rj.job_id
           LEFT JOIN analysis a ON a.snapshot_id = rj.snapshot_id
           WHERE rj.run_id=? AND rj.detail_outcome IN ('complete','partial','reused')""",
        (run_id,))
    return ok({"pending": rows,
               "all": [r for r in analyzed if r["result_status"]]})


# ---------------------------------------------------------------------------
# intel_review
# ---------------------------------------------------------------------------

class ReviewReq(BaseModel):
    target_type: str
    target_id: str
    action: str
    patch: Optional[dict] = None
    reason: str
    expected_version: int


@app.post("/api/review")
def do_review(req: ReviewReq):
    try:
        result = review_mod.review(req.target_type, req.target_id, req.action,
                                   req.patch, req.reason, req.expected_version)
        affected = reports.list_reports()
        return ok({"result": result, "affected_reports": len(affected),
                   "note": "后续修正会使旧报告标记过期，重新生成后生效"})
    except review_mod.ReviewError as e:
        return err(e.code, e.message, operator_action=e.operator_action)
    except company_mod.VersionConflict as e:
        return err("VERSION_CONFLICT", str(e), operator_action="重新读取最新数据后再提交修正")
    except KeyError as e:
        return err("INVALID_ANALYSIS", str(e))


@app.get("/api/review/pending")
def pending_review():
    return ok(review_mod.pending_reviews())


@app.get("/api/review/events")
def review_events(target_type: Optional[str] = None, target_id: Optional[str] = None):
    return ok({"events": review_mod.review_events(target_type, target_id)})


# ---------------------------------------------------------------------------
# 岗位库 / 公司方向 / 事项查询
# ---------------------------------------------------------------------------

def _jobs_payload(company_id: Optional[str] = None, role_class: Optional[str] = None,
                  relevance: Optional[str] = None, run_id: Optional[str] = None,
                  include_demo: bool = False) -> list[dict]:
    """岗位库查询（原 /api/jobs 行为原样抽出，供任务视图复用）。"""
    sql = """SELECT j.*, c.display_name, c.identity_status,
                    (SELECT detail_status FROM job_snapshot s WHERE s.job_id=j.id
                     ORDER BY fetched_at DESC LIMIT 1) AS detail_status,
                    (SELECT COUNT(*) FROM job_snapshot s WHERE s.job_id=j.id) AS snapshot_count,
                    rj.job_ref, rj.snapshot_id AS run_snapshot_id
             FROM job j LEFT JOIN company c ON c.id=j.company_id"""
    conds, params = [], []
    if run_id:
        sql += " JOIN run_job rj ON rj.job_id=j.id AND rj.run_id=?"
        params.append(run_id)
    else:
        sql += " LEFT JOIN run_job rj ON rj.job_id=j.id"
    if company_id:
        conds.append("j.company_id=?")
        params.append(company_id)
    if not include_demo:
        conds.append("j.source!='demo'")
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    rows = query_all(sql, tuple(params))
    for r in rows:
        if run_id:
            snap = query_one('SELECT detail_status FROM job_snapshot WHERE id=?', (r['run_snapshot_id'],))
            r['detail_status'] = snap['detail_status'] if snap else 'missing'
        a = query_one(
            """SELECT role_class, relevance, result_status FROM analysis a
               JOIN job_snapshot s ON a.snapshot_id=s.id WHERE s.job_id=?
               ORDER BY a.created_at DESC LIMIT 1""", (r["id"],))
        if a:
            r["role_class"], r["relevance"], r["result_status"] = \
                a["role_class"], a["relevance"], a["result_status"]
        else:
            r["role_class"] = r["relevance"] = r["result_status"] = None
    if role_class:
        rows = [r for r in rows if r["role_class"] == role_class]
    if relevance:
        rows = [r for r in rows if r["relevance"] == relevance]
    return rows


@app.get("/api/jobs")
def list_jobs(company_id: Optional[str] = None, role_class: Optional[str] = None,
              relevance: Optional[str] = None, run_id: Optional[str] = None,
              include_demo: bool = False):
    return ok({"jobs": _jobs_payload(company_id, role_class, relevance, run_id, include_demo)})


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str):
    job = query_one("SELECT * FROM job WHERE id=?", (job_id,))
    if not job:
        return err("INVALID_ANALYSIS", "岗位不存在")
    snapshots = query_all(
        "SELECT * FROM job_snapshot WHERE job_id=? ORDER BY fetched_at DESC", (job_id,))
    analyses = query_all(
        """SELECT a.* FROM analysis a JOIN job_snapshot s ON a.snapshot_id=s.id
           WHERE s.job_id=? ORDER BY a.created_at DESC""", (job_id,))
    for a in analyses:
        a["findings"] = query_all(
            """SELECT * FROM finding WHERE analysis_id=? ORDER BY created_at""", (a["id"],))
        for f in a["findings"]:
            f["product_names"] = jloads(f["product_names"], [])
            f["limitations"] = jloads(f["limitations"], [])
            f["evidences"] = query_all(
                "SELECT * FROM evidence WHERE finding_id=? ORDER BY start", (f["id"],))
    comp = query_one("SELECT * FROM company WHERE id=?", (job["company_id"],)) if job["company_id"] else None
    return ok({"job": job, "company": comp, "snapshots": snapshots, "analyses": analyses})


@app.get("/api/companies")
def companies(include_demo: bool = False):
    return ok({"companies": company_mod.company_summary(include_demo=include_demo),
               "scope": "including_demo" if include_demo else "real_only"})


@app.get("/api/companies/{company_id}")
def company_detail(company_id: str):
    comp = query_one("SELECT * FROM company WHERE id=?", (company_id,))
    if not comp:
        return err("INVALID_ANALYSIS", "公司不存在")
    aliases = query_all("SELECT * FROM company_alias WHERE company_id=?", (company_id,))
    findings = analysis.company_findings(company_id, include_excluded=False)
    return ok({"company": comp, "aliases": aliases, "findings": findings})


@app.get("/api/findings/{finding_id}")
def finding_detail(finding_id: str):
    f = analysis.finding_detail(finding_id)
    if not f:
        return err("INVALID_ANALYSIS", "事项不存在")
    return ok(f)


@app.get("/api/runs/{run_id}/pairs")
def run_pairs(run_id: str):
    pairs = query_all(
        """SELECT p.*, ja.title AS left_title, jb.title AS right_title, jc.company_name_raw AS company
           FROM job_pair p
           JOIN job_snapshot sa ON sa.id=p.left_snapshot_id
           JOIN job_snapshot sb ON sb.id=p.right_snapshot_id
           JOIN job ja ON ja.id=sa.job_id
           JOIN job jb ON jb.id=sb.job_id
           JOIN job jc ON jc.id=sa.job_id
           WHERE sa.job_id IN (SELECT job_id FROM run_job WHERE run_id=?)""",
        (run_id,))
    return ok({"pairs": pairs})


# ---------------------------------------------------------------------------
# intel_report
# ---------------------------------------------------------------------------

class ReportReq(BaseModel):
    run_ids: list[str]
    include_pending: bool = True
    include_demo: bool = False
    formats: list[str] = ["md", "json"]


@app.post("/api/reports")
def create_report(req: ReportReq):
    try:
        result = reports.generate_report(
            req.run_ids, req.include_pending, req.formats, include_demo=req.include_demo
        )
        return ok(result)
    except reports.ReportError as e:
        return err(e.code, e.message, operator_action=e.operator_action)


@app.get("/api/reports")
def list_reports():
    return ok({"reports": reports.list_reports()})


@app.get("/api/reports/{report_id}/file")
def report_file(report_id: str, fmt: str = "md"):
    rep = query_one("SELECT * FROM report WHERE id=?", (report_id,))
    if not rep:
        return err("INVALID_ANALYSIS", "报告不存在")
    paths = jloads(rep["output_paths"], {})
    if fmt not in paths:
        return err("INVALID_ANALYSIS", f"报告不含 {fmt} 格式文件")
    return FileResponse(paths[fmt], filename=Path(paths[fmt]).name,
                        media_type="text/markdown" if fmt == "md" else "application/json")


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------

@app.get("/api/settings")
def get_settings():
    rows = query_all("SELECT key, value FROM app_setting")
    settings = {r["key"]: r["value"] for r in rows}
    settings.pop("llm_api_key", None)  # 不回显密钥
    # X 凭据同样不回显值（G 第一条）：只回"已配置/未配置"
    for _secret_key in social_source_status.X_CREDENTIAL_KEYS:
        settings.pop(_secret_key, None)
    data_dir = resolve_upstream_data_dir()
    cli_path = resolve_cli_path(settings.get("boss_cli_path"))
    return ok({"settings": settings,
               "credential_flags": {key: social_source_status.credential_present(key)
                                    for key in social_source_status.X_CREDENTIAL_KEYS},
               "cli_detected": bool(cli_path),
               "effective_cli_path": cli_path,
               "outputs_dir": str(store.OUTPUTS_DIR),
               "db_path": str(store.get_db_path()),
               "effective_data_dir": str(data_dir.path),
               "data_dir_source": data_dir.source,
               "install_hint": install_command_hint(),
               "login_steps": health_mod.boot_steps(cli_path, data_dir),
               "website_channel": "CLI",
               "mcp_note": "MCP 仅用于独立协议验证，不参与 finintel 网站数据采集链路",
               **_channel_block()})


class SettingReq(BaseModel):
    key: str
    value: str


@app.post("/api/settings")
def set_setting(req: SettingReq):
    allowed = {"boss_cli_path", "upstream_data_dir", "llm_provider", "llm_api_base",
               "llm_api_key", "llm_model", "llm_timeout_seconds", "demo_mode",
               "boss_browser_source", "boss_cdp_url",
               *social_source_status.X_CREDENTIAL_KEYS}
    if req.key not in allowed:
        return err("TOOL_NOT_ALLOWED", f"不支持的设置项: {req.key}",
                   operator_action=f"允许的设置项: {sorted(allowed)}")
    if req.key in {"boss_browser_source", "boss_cdp_url"}:
        # 采集通道选项先校验再落库：来源只允许三种取值，调试地址只允许本机回环地址
        try:
            validate_browser_option(req.key, req.value)
        except source_boss.UpstreamError as exc:
            return err(exc.code, exc.message, operator_action=exc.operator_action)
    conn = store.get_conn()
    conn.execute("INSERT INTO app_setting (key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (req.key, req.value))
    conn.commit()
    payload = {"key": req.key, "saved": True}
    if req.key in social_source_status.X_CREDENTIAL_KEYS:
        # X 适配器只认进程环境变量：设置落库后必须**桥接**到 env，
        # 否则用户以为配好了、采集子进程却读不到（等于把人支使到不存在的入口）。
        # 只动用户刚保存的这一项：值为空即"用户显式清空"，其余键一律不碰。
        injected = social_source_status.apply_x_credentials_to_env(
            {req.key: req.value}, clear_missing=True)
        auth_state, missing = social_source_status.x_credentials_state()
        payload["credential_flags"] = injected
        payload["x_auth_state"] = auth_state
        payload["missing_credentials"] = missing
        payload["operator_action"] = ("" if not missing else
                                      social_source_status.X_CREDENTIAL_ACTION)
        from dataclasses import replace
        prior = social_source_status.get_status(social_source_status.PLATFORM_X)
        social_source_status.record_status(replace(prior, connection_state="unknown",
            auth_state=auth_state, checked_at=store.utcnow_iso(), check_source="probe",
            last_error_code="AUTH_REQUIRED" if missing else None, last_error_summary=None))
        if not missing:
            # 依赖/连接状态要跟着刷新，否则页面还显示旧的 auth_required
            social_source_status.check_platform(social_source_status.PLATFORM_X)
    if req.key in channel_state.CHANNEL_SETTING_KEYS:
        # 换通道（来源/调试地址/CLI/数据目录）= 换指纹：
        # 旧通道的 ready 一律失效，新通道从 unknown 重新开始（设计 §2.3 铁律 3）。
        channel_state.invalidate_all()
        try:
            channel_state.precheck_current(reason=f"settings:{req.key}")
        except Exception as exc:  # 预检失败只影响诊断事实，不影响设置已保存
            print(f"[finintel] 设置变更后的通道预检失败: {type(exc).__name__}", file=sys.stderr)
        payload.update(_channel_block())
    return ok(payload)


# ---------------------------------------------------------------------------
# 求职者模式：简历（F-03/04/13）
# ---------------------------------------------------------------------------

class ResumeTextReq(BaseModel):
    label: str
    text: str = ""
    file_name: Optional[str] = None


class ResumeItemsReq(BaseModel):
    items: list[dict]
    reason: str


class ConfirmReq(BaseModel):
    confirm: bool = False


@app.post("/api/resumes")
def resume_import(req: ResumeTextReq):
    try:
        return ok(resume_mod.import_text(req.label, req.text, req.file_name or ""))
    except resume_mod.ResumeError as e:
        return err(e.code, e.message, operator_action=e.operator_action)


@app.post("/api/resumes/file")
async def resume_file_import(file: UploadFile = File(...), label: str = Form(...)):
    try:
        data = await file.read()
        return ok(resume_mod.import_file(label, file.filename or "resume.pdf", data))
    except resume_mod.ResumeError as e:
        return err(e.code, e.message, operator_action=e.operator_action)


@app.get("/api/resumes")
def resume_list():
    return ok({"resumes": resume_mod.list_resumes()})


@app.get("/api/resumes/storage-info")
def resume_storage_info():
    return ok(resume_mod.storage_info())


@app.get("/api/resumes/{resume_id}")
def resume_detail(resume_id: str):
    r = resume_mod.get_resume(resume_id, with_text=True)
    if not r:
        return err("INVALID_ANALYSIS", "简历不存在")
    return ok(r)


@app.post("/api/resumes/{resume_id}/items")
def resume_replace_items(resume_id: str, req: ResumeItemsReq):
    try:
        return ok(resume_mod.replace_items(resume_id, req.items, req.reason))
    except resume_mod.ResumeError as e:
        return err(e.code, e.message, operator_action=e.operator_action)


@app.delete("/api/resumes/{resume_id}")
def resume_delete(resume_id: str, req: ConfirmReq):
    try:
        return ok(resume_mod.delete_resume(resume_id, req.confirm))
    except resume_mod.ResumeError as e:
        return err(e.code, e.message, operator_action=e.operator_action)


@app.delete("/api/resumes")
def resume_delete_all(req: ConfirmReq):
    try:
        return ok(resume_mod.delete_all(req.confirm))
    except resume_mod.ResumeError as e:
        return err(e.code, e.message, operator_action=e.operator_action)


# ---------------------------------------------------------------------------
# 求职者模式：简历事实提取与核对（阶段 2，真实模型调用）
# ---------------------------------------------------------------------------

class ConfirmClaimsReq(BaseModel):
    claim_ids: list[str]
    expected_revision: int = 0


class SupplementReq(BaseModel):
    question: str
    answer: str


@app.post("/api/resumes/{resume_id}/extract-facts")
def resume_extract_facts(resume_id: str):
    try:
        return ok(resume_facts.extract_facts(resume_id))
    except resume_facts.FactsError as e:
        return err(e.code, e.message, retryable=e.retryable, operator_action=e.operator_action)


@app.get("/api/resumes/{resume_id}/claims")
def resume_claims(resume_id: str):
    return ok({"claims": resume_facts.claims_view(resume_id),
               "supplements": resume_facts.supplements_view(resume_id)})


@app.post("/api/resumes/{resume_id}/confirm")
def resume_confirm(resume_id: str, req: ConfirmClaimsReq):
    try:
        return ok(resume_facts.confirm_claims(resume_id, req.claim_ids, req.expected_revision))
    except resume_facts.FactsError as e:
        return err(e.code, e.message, retryable=e.retryable, operator_action=e.operator_action)


@app.post("/api/resumes/{resume_id}/supplements")
def resume_supplement(resume_id: str, req: SupplementReq):
    try:
        return ok(resume_facts.add_supplement(resume_id, req.question, req.answer))
    except resume_facts.FactsError as e:
        return err(e.code, e.message, operator_action=e.operator_action)


@app.get("/api/ai/status")
def ai_status():
    return ok(ai_provider.ai_config())


# ---------------------------------------------------------------------------
# 求职者模式：目标 JD（F-02）
# ---------------------------------------------------------------------------

class JobTargetReq(BaseModel):
    session_id: Optional[str] = None
    source_kind: str
    snapshot_id: Optional[str] = None
    job_id: Optional[str] = None
    title: Optional[str] = None
    company_raw: Optional[str] = None
    responsibilities_text: Optional[str] = None
    requirements_text: Optional[str] = None


@app.post("/api/job-targets")
def create_job_target(req: JobTargetReq):
    try:
        return ok({"job_target": targets.create_target(
            req.session_id, req.source_kind, req.snapshot_id, req.job_id, req.title,
            req.company_raw, req.responsibilities_text, req.requirements_text)})
    except targets.TargetError as e:
        return err(e.code, e.message, operator_action=e.operator_action)


@app.get("/api/job-targets/{job_target_id}")
def get_job_target(job_target_id: str):
    try:
        return ok({"job_target": targets.target_view(job_target_id)})
    except targets.TargetError as e:
        return err(e.code, e.message, operator_action=e.operator_action)


# ---------------------------------------------------------------------------
# 求职者模式：会话（F-01/26）
# ---------------------------------------------------------------------------

class SessionCreateReq(BaseModel):
    route: str = "match"
    industry: Optional[str] = None
    role_direction: Optional[str] = None
    experience_level: str = "unknown"
    target_company_ids: Optional[list] = None
    background_note: Optional[str] = None
    job_target_id: Optional[str] = None
    resume_id: Optional[str] = None


class InterviewStartReq(BaseModel):
    job_id: str
    run_id: Optional[str] = None
    snapshot_id: Optional[str] = None


class InterviewMaterialReq(BaseModel):
    finding_id: str


@app.post('/api/interview/start')
def start_interview(req: InterviewStartReq):
    try:
        return ok(interview_research.start_from_job(req.job_id, req.run_id, req.snapshot_id))
    except (interview_research.ResearchError, matching.MatchError, targets.TargetError) as e:
        return err(e.code, e.message)


@app.get('/api/js/sessions/{session_id}/research')
def interview_materials(session_id: str):
    try:
        return ok(interview_research.questions_view(session_id))
    except interview_research.ResearchError as e:
        return err(e.code, e.message)


@app.post('/api/js/sessions/{session_id}/research')
def add_interview_material(session_id: str, req: InterviewMaterialReq):
    try:
        return ok(interview_research.add_finding(session_id, req.finding_id))
    except interview_research.ResearchError as e:
        return err(e.code, e.message)


@app.delete('/api/js/sessions/{session_id}/research/{material_id}')
def remove_interview_material(session_id: str, material_id: str):
    try:
        return ok(interview_research.remove_material(session_id, material_id))
    except interview_research.ResearchError as e:
        return err(e.code, e.message)


class SessionUpdateReq(BaseModel):
    job_target_id: Optional[str] = None
    resume_id: Optional[str] = None
    industry: Optional[str] = None
    role_direction: Optional[str] = None
    experience_level: Optional[str] = None
    background_note: Optional[str] = None


@app.post("/api/js/sessions")
def create_js_session(req: SessionCreateReq):
    try:
        return ok({"session": matching.create_session(
            req.route, req.industry, req.role_direction, req.experience_level,
            req.target_company_ids, req.background_note, req.job_target_id, req.resume_id)})
    except matching.MatchError as e:
        return err(e.code, e.message, e.retryable, e.operator_action)


@app.get("/api/js/sessions")
def list_js_sessions():
    return ok({"sessions": matching.list_sessions()})


@app.get("/api/js/sessions/{session_id}")
def get_js_session(session_id: str):
    try:
        return ok(matching.get_session(session_id))
    except matching.MatchError as e:
        return err(e.code, e.message, e.retryable, e.operator_action)


@app.post("/api/js/sessions/{session_id}")
def update_js_session(session_id: str, req: SessionUpdateReq):
    try:
        return ok({"session": matching.update_session(
            session_id, req.job_target_id, req.resume_id, req.industry,
            req.role_direction, req.experience_level, req.background_note)})
    except matching.MatchError as e:
        return err(e.code, e.message, e.retryable, e.operator_action)


@app.post("/api/js/sessions/{session_id}/retry")
def retry_js_session(session_id: str):
    try:
        return ok({"session": matching.retry_session(session_id)})
    except matching.MatchError as e:
        return err(e.code, e.message, e.retryable, e.operator_action)


# ---------------------------------------------------------------------------
# 求职者模式：匹配包（F-05/06/07/08）
# ---------------------------------------------------------------------------

@app.post("/api/js/sessions/{session_id}/prepare-match")
def prepare_match(session_id: str):
    try:
        return ok(matching.prepare_match(session_id))
    except matching.MatchError as e:
        return err(e.code, e.message, e.retryable, e.operator_action)


@app.post("/api/js/sessions/{session_id}/submit-match")
async def submit_match(session_id: str, request: Request):
    try:
        payload = await request.json()
    except Exception:
        return err("INVALID_ANALYSIS", "请求体必须是 JSON")
    try:
        return ok(matching.submit_match(payload))
    except matching.MatchError as e:
        return err(e.code, e.message, e.retryable, e.operator_action)


@app.post("/api/js/sessions/{session_id}/auto-match")
def auto_match(session_id: str):
    try:
        result = matching.auto_match(session_id)
        return ok(result, warnings=["结果由内置规则匹配器生成（rule-matcher-demo-v1），"
                                    "语义需人工确认，不代表宿主模型质量"])
    except matching.MatchError as e:
        return err(e.code, e.message, e.retryable, e.operator_action)


@app.get("/api/js/sessions/{session_id}/requirements")
def js_requirements(session_id: str):
    return ok(matching.requirements_view(session_id))


@app.get("/api/js/sessions/{session_id}/match")
def js_match(session_id: str):
    return ok(matching.match_view(session_id))


class SupplementReq(BaseModel):
    user_supplement: str
    match_status: Optional[str] = None
    reason: str


class AnswerReq(BaseModel):
    answer_text: str
    reason: str


@app.post("/api/js/match-rows/{row_id}/supplement")
def supplement_row(row_id: str, req: SupplementReq):
    try:
        return ok(matching.supplement_row(row_id, req.user_supplement, req.match_status, req.reason))
    except matching.MatchError as e:
        return err(e.code, e.message, e.retryable, e.operator_action)


@app.post("/api/js/clarifications/{clar_id}/answer")
def answer_clarification(clar_id: str, req: AnswerReq):
    try:
        return ok(matching.answer_clarification(clar_id, req.answer_text, req.reason))
    except matching.MatchError as e:
        return err(e.code, e.message, e.retryable, e.operator_action)


# ---------------------------------------------------------------------------
# 求职者模式：准备包（F-09/10/24）+ 导出（F-12）
# ---------------------------------------------------------------------------

@app.post("/api/js/sessions/{session_id}/prepare-prep")
def prepare_prep(session_id: str):
    try:
        return ok(prep_mod.prepare_prep(session_id))
    except prep_mod.PrepError as e:
        return err(e.code, e.message, e.retryable, e.operator_action)


@app.post("/api/js/sessions/{session_id}/submit-prep")
async def submit_prep(session_id: str, request: Request):
    try:
        payload = await request.json()
    except Exception:
        return err("INVALID_ANALYSIS", "请求体必须是 JSON")
    try:
        return ok(prep_mod.submit_prep(payload))
    except prep_mod.PrepError as e:
        return err(e.code, e.message, e.retryable, e.operator_action)


@app.post("/api/js/sessions/{session_id}/auto-prep")
def auto_prep(session_id: str):
    try:
        result = prep_mod.auto_prep(session_id)
        return ok(result, warnings=["结果由内置规则匹配器生成（rule-matcher-demo-v1），"
                                    "语义需人工确认，不代表宿主模型质量"])
    except prep_mod.PrepError as e:
        return err(e.code, e.message, e.retryable, e.operator_action)


@app.get("/api/js/sessions/{session_id}/focus")
def js_focus(session_id: str):
    return ok(prep_mod.focus_view(session_id))


@app.get("/api/js/sessions/{session_id}/prep")
def js_prep(session_id: str):
    return ok(prep_mod.prep_view(session_id))


class TaskStatusReq(BaseModel):
    status: str


@app.post("/api/js/prep-tasks/{task_id}/status")
def set_task_status(task_id: str, req: TaskStatusReq):
    try:
        return ok(prep_mod.set_task_status(task_id, req.status))
    except prep_mod.PrepError as e:
        return err(e.code, e.message, e.retryable, e.operator_action)


class PrepReportReq(BaseModel):
    formats: list[str] = ["md"]


@app.post("/api/js/sessions/{session_id}/report")
def export_prep_report(session_id: str, req: PrepReportReq):
    try:
        return ok(reports.generate_prep_report(session_id, req.formats))
    except reports.ReportError as e:
        return err(e.code, e.message, operator_action=e.operator_action)


# ---------------------------------------------------------------------------
# 三步主流程（设计 §9.1）：登录 BOSS → 搜索并整理 → 结果 / 历史
#
# 普通 UI 只消费 user_stage / user_message / actions；AUTH_REQUIRED、
# AI_NOT_CONFIGURED 等工程码只出现在技术诊断字段里。
# ---------------------------------------------------------------------------

def _channel_block() -> dict:
    """设置页/诊断页用的采集通道块（**只读 DB**，不拉子进程、不发起搜索）。

    - ``channel``：普通视图（只有状态与一行摘要，不含路径、不含详细原因）；
    - ``collect_ready``：与顶栏 pill、搜索前置检查**同一个** ``collect_gate()`` 判定。
    """
    view = channel_state.channel_view()
    account = str((login_bridge.controller.status_view() or {}).get("state") or "")
    return {
        "channel": view,
        "collect_ready": bool(channel_state.collect_gate(account, view)),
    }


def _boss_block() -> dict:
    """首页/顶栏用的一个用户状态（只读 DB，绝不触发在线探测）。

    **唯一实现在 ``login_bridge.boss_block()``**：``/api/home``、``/api/auth/status``、
    ``/api/tasks/{id}`` 三处的 ``boss`` 块必须是逐字段同源的同一份——任何一处手写副本
    漏字段（真机就是漏了 ``channel``），前端 ``sfSetBoss()`` 一覆盖就会让顶栏退化。
    这里只做转发，不再自己拼字面量。
    """
    return login_bridge.boss_block()


def _on_login_connected() -> Optional[str]:
    """登录成功后：做一次**有界**通道预检（doctor，绝不发起搜索），再自动继续待执行意图。

    默认 auto 通道的预检不下结论（headless 兜底不依赖外部通道），因此这里通常
    不会拉起子进程；只有 existing-browser / stored-cookie 才会真的跑一次 doctor。
    """
    try:
        # A fresh verified login permits one new attempt; unknown is never a green success badge.
        channel_state.rearm_current()
        channel_state.precheck_current(reason="login_connected")
    except Exception as exc:  # 预检失败只影响诊断事实，不得影响登录结果
        print(f"[finintel] 登录成功后的通道预检失败: {type(exc).__name__}", file=sys.stderr)
    return pipeline.pipeline.on_login_connected()


# 登录成功 → 通道预检 → 自动继续「未登录先搜索」的那一次请求（设计 §7.6）
login_bridge.controller.on_connected = _on_login_connected


class LoginReq(BaseModel):
    timeout_seconds: int = LOGIN_DEFAULT_TIMEOUT_SECONDS


class LoginCancelReq(BaseModel):
    reason: str = "用户取消登录"


@app.post("/api/auth/login")
def auth_login(req: LoginReq = None):
    """点击「登录 BOSS」：启动受控官方登录流程；重复点击返回同一流程。"""
    requested = (req or LoginReq()).timeout_seconds
    problem = login_bridge.timeout_bounds_error(requested)
    if problem:
        low, high = LOGIN_TIMEOUT_BOUNDS
        return err("TOOL_NOT_ALLOWED", problem, False, f"登录超时取 {low}~{high} 秒")
    try:
        view = login_bridge.controller.start(requested)
    except login_bridge.LoginError as e:
        return err(e.code, e.message, e.retryable, e.operator_action)
    return ok({"login": view, "boss": _boss_block()})


@app.post("/api/auth/browser/repair")
def auth_browser_repair(request: Request):
    """Explicitly reopen the project-owned browser; no login, token refresh or search."""
    origin = request.headers.get("origin")
    expected = str(request.base_url).rstrip("/")
    if (request.url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or request.headers.get("sec-fetch-site") == "cross-site"
            or (origin and origin.rstrip("/") != expected)):
        return err("TOOL_NOT_ALLOWED", "仅允许从本地项目页面修复专用浏览器")
    import managed_browser
    with login_bridge.controller._lock, pipeline.pipeline._lock:
        if login_bridge.controller.active() or any(t.is_alive() for t in pipeline.pipeline._threads.values()):
            return err("TOOL_NOT_ALLOWED", "请先等待当前登录或搜索结束，再修复浏览器")
        try:
            managed_browser.restart_owned()
            channel_state.precheck_current(reason="browser_repair")
        except Exception as exc:
            # No endpoint identifiers, process data or credentials in the ordinary UI.
            print(f"[finintel] browser repair failed: {type(exc).__name__}", file=sys.stderr)
            return err("BROWSER_REPAIR_FAILED", "专用浏览器未能重开，请关闭专用窗口后点击登录 BOSS")
    return ok({"message": "专用浏览器已按修正配置重开，登录数据已保留；请先在官方页面确认能否正常搜索。平台限制不保证解除。",
               "boss": _boss_block()})


@app.get("/api/auth/status")
def auth_status():
    return ok({"boss": _boss_block()})


@app.post("/api/channel/open-official")
def channel_open_official():
    """在**本项目的专用浏览器**里打开 BOSS 官方页面（纯 CDP 浏览器动作，零业务请求）。

    环境风控（code 37）的诚实出口：被标记的是专用 profile 的会话，只有在该浏览器里
    打开官方页面才可能有意义。这里**只**发浏览器级 CDP 命令，绝不发 search/detail/login，
    也不触发重新登录。
    """
    import managed_browser

    try:
        result = managed_browser.open_official_page()
    except source_boss.UpstreamError as e:
        return err(e.code, e.message, e.retryable, e.operator_action)
    return ok({"url": result["url"], "already_open": bool(result.get("already_open"))})


@app.get("/api/auth/login/{login_id}")
def auth_login_get(login_id: str, diagnostic: bool = False):
    """前端轮询入口，只读 DB；diagnostic=1 时附带技术诊断字段。"""
    try:
        view = login_bridge.controller.view(login_bridge.controller.get(login_id))
    except login_bridge.LoginError as e:
        return err(e.code, e.message, e.retryable, e.operator_action)
    data: dict = {"login": view, "boss": _boss_block()}
    if diagnostic:
        data["diagnostic"] = login_bridge.controller.diagnostic_view(login_id)
    return ok(data)


@app.post("/api/auth/login/{login_id}/cancel")
def auth_login_cancel(login_id: str, req: LoginCancelReq = None):
    try:
        view = login_bridge.controller.cancel(login_id, (req or LoginCancelReq()).reason)
    except login_bridge.LoginError as e:
        return err(e.code, e.message, e.retryable, e.operator_action)
    return ok({"login": view, "boss": _boss_block()})


@app.post("/api/auth/login/{login_id}/reopen")
def auth_login_reopen(login_id: str):
    try:
        view = login_bridge.controller.reopen(login_id)
    except login_bridge.LoginError as e:
        return err(e.code, e.message, e.retryable, e.operator_action)
    return ok({"login": view, "boss": _boss_block()})


class SimpleSearchReq(BaseModel):
    query: str
    city: Optional[str] = None
    job_type: Optional[str] = None
    # 信息源：zhipin（默认，BOSS 岗位）/ x（海外热点）/ xiaohongshu（岗位讨论）
    source: str = "zhipin"
    # X/小红书专用筛选（时间窗等）
    filters: Optional[dict] = None
    # 「继续搜索」专用：指向既有 run_id，语义是"沿用该任务继续"，不是"用新筛选重跑"。
    # 因此后端保留该 run 已存的展示口径；前端若要换口径，走 GET /result?job_type=
    reuse_run_id: Optional[str] = None


@app.post("/api/search")
def simple_search(req: SimpleSearchReq):
    """唯一主按钮：收关键词 + 信息源，立即返回任务标识。"""
    source = (req.source or "zhipin").strip()
    if source in ("x", "xiaohongshu"):
        # 社交多源：走独立 social_pipeline，不进 BOSS 岗位 pipeline
        try:
            result = social_pipeline.pipeline.start_task(
                source, req.query, filters=req.filters)
        except social_pipeline.SocialPipelineError as e:
            return err(e.code, e.message, e.retryable, e.operator_action)
        return ok(result)
    if source != "zhipin":
        return err("INVALID_ANALYSIS", "不支持的信息源")
    try:
        result = pipeline.pipeline.start_task(
            req.query, city=req.city, job_type=req.job_type, reuse_run_id=req.reuse_run_id)
    except (pipeline.PipelineError, social_pipeline.SocialPipelineError) as e:
        return err(e.code, e.message, e.retryable, e.operator_action)
    return ok(result)


def _task_route(fn, *args, **kwargs):
    try:
        return ok(fn(*args, **kwargs))
    except (pipeline.PipelineError, social_pipeline.SocialPipelineError) as e:
        return err(e.code, e.message, e.retryable, e.operator_action)


@app.get("/api/tasks/{run_id}")
def task_get(run_id: str):
    if _is_social_run(run_id):
        return ok(social_pipeline.pipeline.task_view(run_id))
    return _task_route(pipeline.pipeline.task_view, run_id)


@app.post("/api/tasks/{run_id}/cancel")
def task_cancel(run_id: str, req: LoginCancelReq = None):
    if _is_social_run(run_id):
        return _task_route(social_pipeline.pipeline.cancel_task, run_id)
    return _task_route(pipeline.pipeline.cancel_task, run_id,
                       (req or LoginCancelReq(reason="用户主动取消")).reason)


@app.post("/api/tasks/{run_id}/resume")
def task_resume(run_id: str):
    if _is_social_run(run_id):
        return err("UNSUPPORTED_CAPABILITY", "当前社交任务保留已采材料；请仅重新整理或从首页新建搜索")
    return _task_route(pipeline.pipeline.resume_task, run_id, manual=True)


@app.post("/api/tasks/{run_id}/retry-organize")
def task_retry_organize(run_id: str):
    if _is_social_run(run_id):
        return _task_route(social_pipeline.pipeline.retry_organize, run_id)
    return _task_route(pipeline.pipeline.retry_organize, run_id)


@app.post("/api/tasks/{run_id}/refresh")
def task_refresh(run_id: str):
    """用户显式「获取最新结果」：重跑采集，首个页签带 ``--no-cache`` 真正联网取最新列表。

    只有用户明确点这个动作，才会跳过上游本地缓存——默认路径一律允许命中缓存（少打扰平台）。
    """
    if _is_social_run(run_id):
        return err("UNSUPPORTED_CAPABILITY", "请从首页显式新建社交搜索")
    return _task_route(pipeline.pipeline.refresh_task, run_id)


@app.get("/api/tasks/{run_id}/result")
def task_result(run_id: str, job_type: Optional[str] = None):
    """结果视图。社交任务返回 findings；BOSS 任务返回方向（job_type 为可选展示口径）。"""
    if _is_social_run(run_id):
        return ok(social_pipeline.pipeline.result_view(run_id))
    return _task_route(pipeline.pipeline.result_view, run_id, job_type=job_type)


@app.get("/api/tasks/{run_id}/jobs")
def task_jobs(run_id: str, page: int = 1):
    """岗位明细节（复用 /api/jobs?run_id=）。"""
    try:
        run = runs_mod.get_run(run_id)
    except KeyError:
        return err("INVALID_ANALYSIS", f"任务 {run_id} 不存在")
    include_demo = (run["config_snapshot"].get("source") == "demo")
    rows = _jobs_payload(run_id=run_id, include_demo=include_demo)
    return ok({"jobs": rows}, meta={"page": page, "scope": "including_demo" if include_demo else "real_only"})


class TaskChannelReq(BaseModel):
    """诊断页显式换通道（高级）：空字符串表示关闭该项。"""

    browser_source: str = ""
    cdp_url: str = ""


@app.post("/api/tasks/{run_id}/channel")
def task_channel(run_id: str, req: TaskChannelReq = None):
    """同一任务换通道的唯一出口：重新冻结 + 预检 + 回写快照。

    search 与 detail 之间**不允许隐式切换**：只有这里显式切换后，后续调用才用新通道。
    """
    try:
        result = runs_mod.set_run_channel(
            run_id, (req or TaskChannelReq()).model_dump())
    except KeyError:
        return err("INVALID_ANALYSIS", f"任务 {run_id} 不存在")
    except source_boss.UpstreamError as exc:
        return err(exc.code, exc.message, exc.retryable, exc.operator_action)
    return ok(result)


class TaskReportReq(BaseModel):
    formats: list[str] = ["md"]


@app.post("/api/tasks/{run_id}/report")
def task_report(run_id: str, req: TaskReportReq = None):
    """导出报告：**按任务类型分派**。

    招聘（BOSS）任务走 ``reports.generate_report``；社交任务（X / 小红书）的数据形状是
    条目 + 结论 + 证据，与招聘的 ``run_job``/``job`` 完全不是一张表。历史缺陷正是"一律
    走招聘报告"——于是社交任务导出一份**空报告却报成功**。这里显式分派：未实现的类型
    如实返回 ``UNSUPPORTED_CAPABILITY``，绝不拿空报告冒充成功。
    """
    try:
        runs_mod.get_run(run_id)
    except KeyError:
        return err("INVALID_ANALYSIS", f"任务 {run_id} 不存在")
    if _is_social_run(run_id):
        return err("UNSUPPORTED_CAPABILITY",
                   "社交任务的报告导出尚未实现（不提供空报告冒充成功）",
                   operator_action=("请在任务结果页查看条目、结论与证据；"
                                    "社交报告导出将在后续版本提供"))
    try:
        return ok(reports.generate_report([run_id], include_pending=True,
                                          formats=(req or TaskReportReq()).formats))
    except reports.ReportError as e:
        return err(e.code, e.message, operator_action=e.operator_action)


@app.get("/api/history")
def history(limit: int = 20):
    rows = pipeline.pipeline.history_list(limit) + social_pipeline.pipeline.history_list(limit)
    rows.sort(key=lambda r: r.get("searched_at") or r.get("started_at") or "", reverse=True)
    return ok({"runs": rows[:max(1, min(limit, 200))]})


# ---------------------------------------------------------------------------
# 社交多源 API（X 海外热点 / 小红书岗位讨论）
# ---------------------------------------------------------------------------

def _is_social_run(run_id: str) -> bool:
    row = query_one("SELECT task_type FROM research_run WHERE id=?", (run_id,))
    return bool(row and row["task_type"] in (social_pipeline.TASK_TYPE_X,
                                             social_pipeline.TASK_TYPE_XHS))


def _redact_row(row: Optional[dict], *fields: str) -> Optional[dict]:
    """读取边界脱敏（D4）：T14 之前写入的历史行可能仍带凭据/联系方式。

    新写入的行本来就已脱敏，这里是**幂等的兜底**——出口统一再过一遍，
    保证任何历史数据都不会通过 API 泄漏；本函数绝不改写数据库。
    """
    if not row:
        return row
    out = dict(row)
    for name in fields:
        if isinstance(out.get(name), str):
            out[name] = social_redact.redact(out[name])
    return out


def _redact_rows(rows: list[dict], *fields: str) -> list[dict]:
    return [r for r in (_redact_row(row, *fields) for row in rows) if r is not None]


@app.get("/api/sources")
def sources(refresh: bool = False):
    """信息源能力与状态（三层状态各自取证，带 checked_at，脱敏输出）。

    - 默认只读**已记录**的状态：新库/未检查 → 三层全部 ``unknown``、``checked_at`` 为 None；
    - ``refresh=true`` 时做一次**只读体检**（依赖发现 + 凭据存在性 + 通道记录），
      **不发起任何采集**；依赖/凭据/通道缺失一律如实报 missing / auth_required / not_ready。
    """
    return ok(social_source_status.sources_view(refresh=bool(refresh)))


class SourcesCheckReq(BaseModel):
    platforms: list[str] = list(social_source_status.PLATFORMS)


@app.post("/api/sources/check")
def sources_check(req: SourcesCheckReq = None):
    """只读体检（不采集）：X 只回答"凭据有无"，小红书只看采集通道记录。"""
    wanted = (req or SourcesCheckReq()).platforms or list(social_source_status.PLATFORMS)
    unknown = [p for p in wanted if p not in social_source_status.PLATFORMS]
    if unknown:
        return err("TOOL_NOT_ALLOWED", f"不支持的来源：{unknown}",
                   operator_action=f"可体检的来源：{list(social_source_status.PLATFORMS)}")
    statuses = [social_source_status.check_platform(p) for p in wanted]
    return ok({"sources": [s.as_view() for s in statuses],
               "checked": True,
               "credential_flags": {key: social_source_status.credential_present(key)
                                    for key in social_source_status.X_CREDENTIAL_KEYS}})


@app.get("/api/social/items/{item_id}")
def social_item_detail(item_id: str, snapshot_id: Optional[str] = None):
    """指定版本原文、来源、采集信息（读取边界：URL 去参数、文本过脱敏）。"""
    row = query_one(
        """SELECT si.id, si.platform, si.source_id, si.kind, si.canonical_url, si.author_key,
                  ss.id AS snapshot_id, ss.title, ss.evidence_text, ss.published_at,
                  ss.fetched_at, ss.detail_status, ss.source_url_verified
           FROM social_item si
           LEFT JOIN social_snapshot ss ON ss.item_id = si.id
           WHERE si.id=? AND (? IS NULL OR ss.id=?) ORDER BY ss.fetched_at DESC LIMIT 1""",
        (item_id, snapshot_id, snapshot_id),
    )
    if not row:
        return err("INVALID_ANALYSIS", f"内容 {item_id} 不存在")
    out = _redact_row(row, "title", "evidence_text")
    # D4：公开 URL 强制去查询串（xsec_token / auth_token 等只允许留在 locator 里）
    out["canonical_url"] = social_redact.public_url(out.get("canonical_url"))
    return ok(out)


@app.get("/api/social/items/{item_id}/comments")
def social_item_comments(item_id: str, run_id: Optional[str] = None):
    """只列该任务实际读取的评论版本（读取边界：文本过脱敏）。"""
    rows = social_store.comments_by_item(item_id, run_id=run_id)
    return ok({"comments": _redact_rows(rows, "evidence_text"),
               "scope": "run" if run_id else "cross_run"})


@app.get("/api/social/findings/{finding_id}/evidence")
def social_finding_evidence(finding_id: str):
    """支持/反例证据、原文和定位（读取边界：quote 与原文过脱敏）。"""
    rows = social_store.evidence_by_finding(finding_id)
    return ok({"evidence": _redact_rows(rows, "quote", "source_text")})


@app.get("/api/social/history")
def social_history(limit: int = 20):
    return ok({"runs": social_pipeline.pipeline.history_list(limit)})


@app.get("/api/social/redaction-audit")
def social_redaction_audit(limit: int = 500):
    """D4 旧数据只读审计（dry-run）：只报条数与主键，绝不回显疑似凭据的值。

    **不提供任何改写动作**：清理历史数据需要用户单独授权后另行执行。
    """
    return ok(social_redact.audit_legacy_sensitive_urls(limit=limit))


@app.get("/api/home")
def home():
    view=pipeline.pipeline.home_view()
    view['demo_launch']=os.environ.get('FININTEL_DEMO')=='1'
    return ok(view)


# ---------------------------------------------------------------------------
# 前端静态文件
# ---------------------------------------------------------------------------

FRONTEND_DIR = BACKEND_DIR.parent / "frontend"
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
