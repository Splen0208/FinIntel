"""finintel reports：公司事项报告（任务书 4.6）。

- 报告固定顺序：覆盖信息 → 统计口径 → 公司清单 → 事项明细 → 待核实区 → 附录。
- 统计数字由程序计算，模型只提供事项解释（任务书 4.3）。
- 导出仅写项目 outputs/，路径由服务生成，用户输入不得穿越输出根目录（AC-33）。
- Markdown 特殊内容转义；来源链接不带访问凭据。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import schemas
import store
from store import get_conn, jdumps, jloads, new_id, query_all, query_one, utcnow_iso

OUTPUTS_DIR = store.OUTPUTS_DIR
MAX_EVIDENCE_CHARS = 160


class ReportError(Exception):
    def __init__(self, code: str, message: str, operator_action: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.operator_action = operator_action


def _md_escape(text: Optional[str]) -> str:
    """Markdown 表格安全转义（管道、换行），HTML 特殊内容转义（AC-33）。"""
    if text is None:
        return ""
    t = str(text).replace("|", "\\|").replace("\n", " ")
    t = t.replace("<", "&lt;").replace(">", "&gt;")
    return t


def _trim_quote(quote: str) -> str:
    q = quote.strip()
    return q[:MAX_EVIDENCE_CHARS] + ("…" if len(q) > MAX_EVIDENCE_CHARS else "")


# ---------------------------------------------------------------------------
# 统计口径（任务书 4.6：每个数字注明定义，程序计算）
# ---------------------------------------------------------------------------

def compute_stats(run_ids: list[str], include_demo: bool = False) -> dict[str, Any]:
    source_clause = "" if include_demo else " AND j.source!='demo'"
    placeholders = ",".join("?" * len(run_ids))
    cards = sum(r["n"] for r in query_all(
        f"""SELECT COUNT(*) AS n FROM run_job WHERE run_id IN ({placeholders})""", tuple(run_ids)))
    obs = query_all(
        f"SELECT source_job_ids FROM search_observation WHERE run_id IN ({placeholders})",
        tuple(run_ids))
    cards_seen = sum(len(jloads(o["source_job_ids"], [])) for o in obs)
    unique_jobs = query_one(
        f"""SELECT COUNT(DISTINCT rj.job_id) AS n FROM run_job rj JOIN job j ON j.id=rj.job_id
            WHERE rj.run_id IN ({placeholders}){source_clause}""",
        tuple(run_ids))["n"]
    detail_rows = query_all(
        f"""SELECT rj.detail_outcome, COUNT(DISTINCT rj.job_id) AS n
            FROM run_job rj JOIN job j ON j.id=rj.job_id
            WHERE rj.run_id IN ({placeholders}){source_clause} GROUP BY rj.detail_outcome""",
        tuple(run_ids))
    outcomes = {r["detail_outcome"]: r["n"] for r in detail_rows}
    analyses = query_all(
        f"""SELECT a.* FROM analysis a WHERE a.snapshot_id IN (
              SELECT snapshot_id FROM run_job WHERE run_id IN ({placeholders})
                AND snapshot_id IS NOT NULL)""",
        tuple(run_ids))
    if not include_demo:
        real_snapshot_ids = {
            row["snapshot_id"] for row in query_all(
                f"""SELECT DISTINCT rj.snapshot_id FROM run_job rj JOIN job j ON j.id=rj.job_id
                    WHERE rj.run_id IN ({placeholders}) AND rj.snapshot_id IS NOT NULL
                    {source_clause}""",
                tuple(run_ids))
        }
        analyses = [a for a in analyses if a["snapshot_id"] in real_snapshot_ids]
    included = [a for a in analyses
                if a["role_class"] in ("product", "engineering", "algorithm_data")
                and a["relevance"] == "direct"
                and a["result_status"] == "findings_found"]
    excluded = [a for a in analyses
                if a["role_class"] == "sales" or a["relevance"] == "irrelevant"
                or a["result_status"] == "excluded"]
    pending = [a for a in analyses
               if a not in included and a not in excluded]
    finding_rows = query_all(
        f"""SELECT f.* FROM finding f WHERE f.analysis_id IN (
              SELECT id FROM analysis WHERE snapshot_id IN (
                SELECT snapshot_id FROM run_job WHERE run_id IN ({placeholders})
                  AND snapshot_id IS NOT NULL))""",
        tuple(run_ids))
    if not include_demo:
        analyzed_ids = {a["id"] for a in analyses}
        finding_rows = [f for f in finding_rows if f["analysis_id"] in analyzed_ids]
    findings_total = len([f for f in finding_rows if f["review_status"] != "excluded"])
    return {
        "scope": "including_demo" if include_demo else "real_only",
        "cards_seen": cards_seen,
        "unique_jobs": unique_jobs,
        "snapshots_complete": outcomes.get("complete", 0) + outcomes.get("reused", 0),
        "snapshots_missing": outcomes.get("missing", 0),
        "snapshots_failed": outcomes.get("failed", 0),
        "snapshots_reused": outcomes.get("reused", 0),
        "analyses_total": len(analyses),
        "included_analyses": len(included),
        "excluded_analyses": len(excluded),
        "pending_analyses": len(pending),
        "findings_total": findings_total,
    }


# ---------------------------------------------------------------------------
# 报告生成
# ---------------------------------------------------------------------------

def generate_report(run_ids: list[str], include_pending: bool = True,
                    formats: Optional[list[str]] = None,
                    include_demo: bool = False) -> dict[str, Any]:
    if not run_ids:
        raise ReportError("INVALID_ANALYSIS", "至少选择一个运行", "")
    formats = formats or ["md", "json"]
    runs = []
    for rid in run_ids:
        r = query_one("SELECT * FROM research_run WHERE id=?", (rid,))
        if not r:
            raise ReportError("INVALID_ANALYSIS", f"运行 {rid} 不存在", "")
        r["config_snapshot"] = jloads(r["config_snapshot"], {})
        r["counters"] = jloads(r["counters"], {})
        runs.append(r)

    stats = compute_stats(run_ids, include_demo=include_demo)
    body = _build_body(runs, stats, include_pending, include_demo)
    report_id = new_id("rep")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    safe_name = re.sub(r"[^\w\-]", "_", f"report_{now.replace(':', '')}")

    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    if "md" in formats:
        p = (OUTPUTS_DIR / f"{safe_name}.md").resolve()
        _ensure_within_outputs(p)
        p.write_text(body["markdown"], encoding="utf-8")
        paths["md"] = str(p)
    if "json" in formats:
        p = (OUTPUTS_DIR / f"{safe_name}.json").resolve()
        _ensure_within_outputs(p)
        p.write_text(_to_json(body, stats, runs, report_id, include_demo), encoding="utf-8")
        paths["json"] = str(p)

    conn = get_conn()
    analysis_ids = [a["id"] for a in query_all(
        f"""SELECT id FROM analysis WHERE snapshot_id IN (
              SELECT snapshot_id FROM run_job WHERE run_id IN ({','.join('?' * len(run_ids))})
                AND snapshot_id IS NOT NULL)""", tuple(run_ids))]
    conn.execute(
        """INSERT INTO report (id, run_ids, filters, analysis_ids, review_revision,
           created_at, output_paths, status) VALUES (?,?,?,?,?,?,?,?)""",
        (report_id, jdumps(run_ids), jdumps({"include_pending": include_pending}),
         jdumps(analysis_ids), 0, now, jdumps(paths), "generated"))
    conn.commit()
    return {"report_id": report_id, "paths": paths, "stats": stats,
            "markdown": body["markdown"] if "md" in formats else None}


def _ensure_within_outputs(path: Path) -> None:
    root = OUTPUTS_DIR.resolve()
    if not str(path).startswith(str(root)):
        raise ReportError("OUTPUT_WRITE_FAILED", "导出路径越界，已拒绝", "")


def _build_body(runs: list[dict], stats: dict, include_pending: bool,
                include_demo: bool = False) -> dict:
    lines: list[str] = []
    run_names = "、".join(f"{r['name']}({r['id']})" for r in runs)

    # 1. 报告名称、生成时间、运行编号、来源覆盖与采集限制
    lines.append("# 消费信贷招聘情报：公司工作事项报告")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"- 运行编号：{run_names}")
    sources = "、".join(sorted({r['config_snapshot'].get('source', 'demo') for r in runs}))
    lines.append(f"- 数据来源：{sources}" + ("（**合成演示数据，非真实采集结果**）" if sources == "demo" else ""))
    queries = []
    for r in runs:
        for q in r["config_snapshot"].get("queries", []):
            queries.append(q["text"])
    lines.append(f"- 查询词：{'、'.join(sorted(set(queries))) or '（无）'}")
    cities = []
    for r in runs:
        cities.extend(r["config_snapshot"].get("cities") or [])
    lines.append(f"- 地域：{'、'.join(set(cities)) if cities else '来源默认范围（未指定城市，不声称全国）'}")
    stop_reasons = [r["stop_reason"] for r in runs if r.get("stop_reason")]
    if stop_reasons:
        lines.append(f"- 采集停止原因：{'、'.join(set(stop_reasons))}")
    lines.append(f"- 统计口径：{'真实来源 + 演示数据（整体标注含演示）' if include_demo else '仅真实来源，演示数据已排除'}")
    lines.append("- 覆盖范围声明：本报告仅覆盖上述查询词与分页的实际采集结果，不声称穷尽行业招聘。")
    lines.append("")

    # 2. 统计口径（每个数字注明定义）
    lines.append("## 统计总览")
    lines.append("")
    lines.append("| 指标 | 数值 | 口径说明 |")
    lines.append("|---|---|---|")
    lines.append(f"| 搜索卡片数 | {stats['cards_seen']} | 各次返回卡片总数，可以重复 |")
    lines.append(f"| 唯一岗位数 | {stats['unique_jobs']} | 按本项目 identity_key 去重，不代表真实岗位空缺人数 |")
    lines.append(f"| 完整详情数 | {stats['snapshots_complete']} | 本运行有可分析快照的唯一岗位计数（含复用 {stats['snapshots_reused']}） |")
    lines.append(f"| 缺失数 | {stats['snapshots_missing']} | 正文为空的岗位，未进入有效分析 |")
    lines.append(f"| 失败数 | {stats['snapshots_failed']} | 详情读取失败的岗位 |")
    lines.append(f"| 纳入分析数 | {stats['included_analyses']} | 产品/研发/算法且直接相关的岗位分析 |")
    lines.append(f"| 排除数 | {stats['excluded_analyses']} | 销售或不相关岗位，保留排除理由 |")
    lines.append(f"| 待核数 | {stats['pending_analyses']} | 相邻/待判断/待复核，进入待核实区 |")
    lines.append(f"| 工作事项数 | {stats['findings_total']} | 不是项目数；独立证据组数不是招聘人数 |")
    lines.append("")

    # 3. 公司清单
    companies = _company_rows(runs, include_demo)
    lines.append("## 公司清单")
    lines.append("")
    lines.append("| 公司 | 归属状态 | 岗位数 | 事项数 | 独立证据组数 |")
    lines.append("|---|---|---|---|---|")
    for c in companies:
        status = "已核实" if c["identity_status"] == "verified" else "归属待核实"
        lines.append(f"| {_md_escape(c['display_name'])} | {status} | {c['job_count']} | "
                     f"{c['finding_count']} | {c['evidence_groups']} |")
    lines.append("")

    # 4. 每家公司工作事项
    lines.append("## 公司工作事项")
    for c in companies:
        findings = _company_findings(c["id"], include_pending)
        if not findings:
            continue
        lines.append("")
        lines.append(f"### {_md_escape(c['display_name'])}")
        domain_label = schemas.TAXONOMY
        for f in findings:
            support = schemas.SUPPORT_LEVEL_LABELS.get(f["support_level"], f["support_level"])
            review = schemas.REVIEW_STATUS_LABELS.get(f["review_status"], f["review_status"])
            wording = {"single": "一个岗位的职责提到", "multi": "多个不同岗位的职责共同指向",
                       "unverified": "暂存线索，尚不能归入明确公司方向"}[f["support_level"]]
            lines.append("")
            lines.append(f"**[{domain_label.get(f['business_domain'], f['business_domain'])}] "
                         f"{_md_escape(f['statement'])}**")
            lines.append("")
            lines.append(f"- 工作动作：{schemas.ACTION_TYPE_LABELS.get(f['action_type'], f['action_type'])}"
                         f"｜证据支持程度：{support}（{wording}）｜复核状态：{review}")
            if f.get("audience"):
                lines.append(f"- 客群/对象：{_md_escape(f['audience'])}")
            if f.get("market"):
                lines.append(f"- 市场：{_md_escape(f['market'])}")
            for ev in f["evidences"][:3]:
                lines.append(f"- 职责原文：\u201c{_md_escape(_trim_quote(ev['quote']))}\u201d"
                             f"（快照 {f['snapshot_id'][:14]}…，{f['fetched_at'][:10]} 采集）")
            if f.get("interpretation"):
                lines.append(f"- 分析判断：{_md_escape(f['interpretation'])}")
            for lim in f["limitations"][:3]:
                lines.append(f"- 不能确认：{_md_escape(lim)}")
            if f["support_level"] != "multi":
                lines.append("- 不能确认：是否为新增业务、是否已经上线")
    lines.append("")

    # 5. 待核实线索与信息不足岗位
    pending_items = _pending_items(runs, include_demo)
    lines.append("## 待核实线索与信息不足岗位")
    lines.append("")
    if pending_items:
        lines.append("| 岗位 | 公司 | 原因 |")
        lines.append("|---|---|---|")
        for p in pending_items:
            lines.append(f"| {_md_escape(p['title'])} | {_md_escape(p['company'])} | {_md_escape(p['reason'])} |")
    else:
        lines.append("（无待核实项）")
    lines.append("")

    # 6. 历史对比（P1 首次报告写"尚无可比历史"）
    lines.append("## 历史对比")
    lines.append("")
    lines.append("尚无可比历史（P2 提供；首次发现不等于业务上线）。")
    lines.append("")

    # 7. 附录：查询计划与实际执行、排除岗位、版本信息
    lines.append("## 附录")
    lines.append("")
    lines.append("### 查询计划与实际执行")
    lines.append("")
    lines.append("| 运行 | 查询 | 采集页数 | 结果 |")
    lines.append("|---|---|---|---|")
    for r in runs:
        obs = query_all("SELECT query_id, page, outcome FROM search_observation WHERE run_id=? ORDER BY observed_at",
                        (r["id"],))
        per_query: dict[str, list] = {}
        for o in obs:
            per_query.setdefault(o["query_id"], []).append(o)
        qmap = {q["query_id"]: q["text"] for q in r["config_snapshot"].get("queries", [])}
        for qid, items in per_query.items():
            outcomes = "、".join(sorted({i["outcome"] for i in items}))
            lines.append(f"| {_md_escape(r['name'])} | {_md_escape(qmap.get(qid, qid))} | {len(items)} | {outcomes} |")
    lines.append("")
    lines.append("### 被排除岗位及原因")
    lines.append("")
    excluded = _excluded_items(runs, include_demo)
    if excluded:
        lines.append("| 岗位 | 公司 | 排除原因 |")
        lines.append("|---|---|---|")
        for e in excluded:
            lines.append(f"| {_md_escape(e['title'])} | {_md_escape(e['company'])} | {_md_escape(e['reason'])} |")
    else:
        lines.append("（无）")
    lines.append("")
    lines.append("### 版本信息")
    lines.append("")
    lines.append(f"- 分类字典：{schemas.TAXONOMY_VERSION}")
    lines.append(f"- 分析提示词：extract_v1")
    lines.append(f"- 证据文本映射：{schemas.MAPPING_VERSION}")
    lines.append("")

    return {"markdown": "\n".join(lines)}


def _company_rows(runs: list[dict], include_demo: bool = False) -> list[dict]:
    ids = [r["id"] for r in runs]
    ph = ",".join("?" * len(ids))
    source_clause = "" if include_demo else " AND j.source!='demo'"
    rows = query_all(
        f"""SELECT DISTINCT j.company_id AS id FROM run_job rj
            JOIN job j ON j.id=rj.job_id WHERE rj.run_id IN ({ph})
              AND j.company_id IS NOT NULL{source_clause}""",
        tuple(ids))
    result = []
    for r in rows:
        comp = query_one("SELECT * FROM company WHERE id=?", (r["id"],))
        job_count = query_one(
            f"""SELECT COUNT(DISTINCT j.id) AS n FROM run_job rj JOIN job j ON j.id=rj.job_id
                WHERE rj.run_id IN ({ph}) AND j.company_id=?{source_clause}""",
            tuple(ids) + (r["id"],))["n"]
        findings = query_all(
            f"""SELECT f.merged_key FROM finding f
                JOIN analysis a ON f.analysis_id=a.id
                JOIN job_snapshot s ON a.snapshot_id=s.id
                JOIN job j ON s.job_id=j.id
                WHERE j.company_id=? AND f.review_status!='excluded' AND a.snapshot_id IN (
                  SELECT snapshot_id FROM run_job WHERE run_id IN ({ph}) AND snapshot_id IS NOT NULL)
                {source_clause}""",
            (r["id"],) + tuple(ids))
        result.append({"id": r["id"], "display_name": comp["display_name"],
                       "identity_status": comp["identity_status"], "job_count": job_count,
                       "finding_count": len(findings),
                       "evidence_groups": len({f["merged_key"] for f in findings})})
    return result


def _company_findings(company_id: str, include_pending: bool) -> list[dict]:
    import analysis as analysis_mod
    rows = analysis_mod.company_findings(company_id)
    if not include_pending:
        rows = [r for r in rows if r["support_level"] in ("single", "multi")
                and r["result_status"] == "findings_found"]
    # 排序：已人工确认 → 多岗一致 → 单岗线索 → 待核实（任务书 3.6）
    order = {"confirmed": 0, "corrected": 0, "unreviewed": 1}
    support_order = {"multi": 0, "single": 1, "unverified": 2}
    rows.sort(key=lambda r: (order.get(r["review_status"], 3),
                             support_order.get(r["support_level"], 3),
                             r["fetched_at"] or ""))
    return rows


def _pending_items(runs: list[dict], include_demo: bool = False) -> list[dict]:
    ids = [r["id"] for r in runs]
    ph = ",".join("?" * len(ids))
    source_clause = "" if include_demo else " AND j.source!='demo'"
    rows = query_all(
        f"""SELECT j.title, j.company_name_raw, a.relevance, a.role_class,
                  a.result_status, a.reason, a.status
            FROM analysis a
            JOIN job_snapshot s ON a.snapshot_id=s.id
            JOIN job j ON s.job_id=j.id
            WHERE a.snapshot_id IN (SELECT snapshot_id FROM run_job WHERE run_id IN ({ph})
                                    AND snapshot_id IS NOT NULL)
              {source_clause}
              AND (a.relevance IN ('adjacent','unknown') OR a.result_status='needs_review'
                   OR a.role_class='unknown' OR a.status='partial')""",
        tuple(ids))
    out = []
    for r in rows:
        reasons = []
        if r["relevance"] in ("adjacent", "unknown"):
            reasons.append(f"行业相关性待判断（{schemas.RELEVANCE_LABELS.get(r['relevance'])}）")
        if r["role_class"] == "unknown":
            reasons.append("岗位类别待判断")
        if r["result_status"] == "needs_review":
            reasons.append("分析结果标记待复核")
        if r["status"] == "partial":
            reasons.append("分析不完整（多块覆盖不全或证据跨段）")
        out.append({"title": r["title"], "company": r["company_name_raw"],
                    "reason": "；".join(reasons) or "待核实"})
    return out


def _excluded_items(runs: list[dict], include_demo: bool = False) -> list[dict]:
    ids = [r["id"] for r in runs]
    ph = ",".join("?" * len(ids))
    source_clause = "" if include_demo else " AND j.source!='demo'"
    rows = query_all(
        f"""SELECT j.title, j.company_name_raw, a.role_class, a.relevance, a.reason, a.result_status
            FROM analysis a
            JOIN job_snapshot s ON a.snapshot_id=s.id
            JOIN job j ON s.job_id=j.id
            WHERE a.snapshot_id IN (SELECT snapshot_id FROM run_job WHERE run_id IN ({ph})
                                    AND snapshot_id IS NOT NULL)
              {source_clause}
              AND (a.role_class='sales' OR a.relevance='irrelevant' OR a.result_status='excluded')""",
        tuple(ids))
    return [{"title": r["title"], "company": r["company_name_raw"],
             "reason": r["reason"] or "销售/不相关岗位排除"} for r in rows]


def _to_json(body: dict, stats: dict, runs: list[dict], report_id: str,
             include_demo: bool = False) -> str:
    import json
    payload = {
        "report_id": report_id,
        "generated_at": utcnow_iso(),
        "runs": [{"id": r["id"], "name": r["name"], "state": r["state"],
                  "config": r["config_snapshot"], "counters": r["counters"]} for r in runs],
        "stats": stats,
        "scope": "including_demo" if include_demo else "real_only",
        "coverage_note": "本报告仅覆盖实际采集的查询、地域与分页，不声称穷尽行业招聘。",
        "taxonomy_version": schemas.TAXONOMY_VERSION,
        "prompt_version": "extract_v1",
        "mapping_version": schemas.MAPPING_VERSION,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def list_reports() -> list[dict]:
    rows = query_all("SELECT * FROM report ORDER BY created_at DESC")
    for r in rows:
        r["run_ids"] = jloads(r["run_ids"], [])
        r["output_paths"] = jloads(r["output_paths"], {})
    return rows


# ---------------------------------------------------------------------------
# 求职准备报告（F-12，report.kind='jobseeker_prep'）
# ---------------------------------------------------------------------------

REPORT_KINDS = {
    "company_intel": "公司情报报告",
    "jobseeker_prep": "求职准备报告",
    "industry_demand": "行业需求报告",
}


def _write_outputs(safe_name: str, body: dict) -> dict[str, str]:
    """写出 body 中存在的 md/json 到 outputs/，路径服务生成、越界拒绝。"""
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    if "markdown" in body:
        p = (OUTPUTS_DIR / f"{safe_name}.md").resolve()
        _ensure_within_outputs(p)
        p.write_text(body["markdown"], encoding="utf-8")
        paths["md"] = str(p)
    if "json" in body:
        p = (OUTPUTS_DIR / f"{safe_name}.json").resolve()
        _ensure_within_outputs(p)
        p.write_text(body["json"], encoding="utf-8")
        paths["json"] = str(p)
    return paths


def generate_prep_report(session_id: str, formats: Optional[list[str]] = None) -> dict[str, Any]:
    formats = formats or ["md"]
    s = query_one("SELECT * FROM jobseeker_session WHERE id=?", (session_id,))
    if not s:
        raise ReportError("INVALID_ANALYSIS", "会话不存在", "")
    report_id = new_id("rep")
    markdown = _build_prep_body(s)["markdown"]
    body: dict[str, Any] = {}
    if "md" in formats:
        body["markdown"] = markdown
    if "json" in formats:
        body["json"] = _prep_json(s, report_id, markdown)
    now = utcnow_iso()
    safe_name = "prep_" + now.replace(":", "").replace("-", "")
    paths = _write_outputs(safe_name, body)

    conn = get_conn()
    conn.execute(
        """INSERT INTO report (id, run_ids, filters, analysis_ids, review_revision, created_at,
           output_paths, status, kind, ref_id) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (report_id, "[]", "{}", "[]", 0, now, jdumps(paths), "generated",
         "jobseeker_prep", session_id))
    conn.commit()
    return {"report_id": report_id, "paths": paths, "stats": {},
            "markdown": markdown if "md" in formats else None}


def _quotes_text(owner_type: str, owner_id: str, side: str) -> str:
    rows = query_all(
        "SELECT quote FROM match_quote WHERE owner_type=? AND owner_id=? AND side=? ORDER BY start",
        (owner_type, owner_id, side))
    return "；".join(_md_escape(_trim_quote(r["quote"])) for r in rows)


def _build_prep_body(session: dict) -> dict:
    import interview_research
    sid = session["id"]
    research = interview_research.questions_view(sid)
    lines: list[str] = []
    model_label = session["model_label"] or "-"
    demo = model_label == schemas.RULE_MATCHER_LABEL

    # 1. 会话与来源
    target = query_one("SELECT * FROM job_target WHERE id=?", (session["job_target_id"] or "",)) \
        if session["job_target_id"] else None
    resume = query_one("SELECT * FROM resume WHERE id=?", (session["resume_id"] or "",)) \
        if session["resume_id"] else None
    lines.append("# 求职面试准备报告")
    lines.append("")
    lines.append(f"- 目标岗位：{_md_escape(target['title'] if target else '-')}"
                 f"｜公司：{_md_escape(target['company_display'] if target else '-')}"
                 f"｜来源：{target['source_kind'] if target else '-'}")
    lines.append(f"- 简历：{_md_escape(resume['label'] if resume else '-')}")
    lines.append(f"- 模型标注：{_md_escape(model_label)}" + ("（内置规则匹配器，语义待人工确认）" if demo else ""))
    lines.append("")
    lines.append("> 本报告基于 JD 与简历的原文引用与匹配矩阵生成；所有推断均单独标注不确定性。")
    if research['plan_stale']:
        lines.append('> **计划已过期：JD、简历或背景材料已改变。请重新匹配（如需）并生成计划；以下保留的旧计划不代表当前材料。**')
    lines.append("")

    # 2. 匹配矩阵
    requirements = {r["id"]: r for r in query_all("SELECT * FROM jd_requirement WHERE session_id=?", (sid,))}
    rows = query_all("SELECT * FROM match_row WHERE session_id=? ORDER BY created_at", (sid,))
    lines.append("## 匹配矩阵")
    lines.append("")
    lines.append("| 岗位要求 | JD 原文依据 | 简历对应证据 | 匹配状态 | 说明 | 准备建议 |")
    lines.append("|---|---|---|---|---|---|")
    for r in rows:
        req = requirements.get(r["requirement_id"])
        req_text = _md_escape(req["text"]) if req else "-"
        status = schemas.MATCH_STATUS_LABELS.get(r["match_status"], r["match_status"])
        jd_quote = _quotes_text("match_row", r["id"], "jd") or "-"
        resume_quote = _quotes_text("match_row", r["id"], "resume") or "-"
        note = _md_escape(r["resume_note"] or r["jd_note"] or "-")
        advice = _md_escape(r["prep_advice"] or "-")
        lines.append(f"| {req_text} | {jd_quote} | {resume_quote} | {status} | {note} | {advice} |")
    lines.append("")

    # 3. 待本人确认与补充提问
    clars = query_all("SELECT * FROM clarification WHERE session_id=? ORDER BY seq", (sid,))
    lines.append("## 待本人确认与补充提问")
    lines.append("")
    if clars:
        for c in clars:
            answered = "（已答）" if c["answer_text"] else "（未回答）"
            lines.append(f"- {_md_escape(c['question'])} {answered}")
            if c["answer_text"]:
                lines.append(f"  - 回答：{_md_escape(c['answer_text'])}")
            if c["why_asked"]:
                lines.append(f"  - 为什么问：{_md_escape(c['why_asked'])}")
    else:
        lines.append("（无）")
    lines.append("")

    # 4. 面试考察重点（6 类分组）
    focus = query_all("SELECT * FROM interview_focus WHERE session_id=? ORDER BY seq", (sid,))
    lines.append("## 面试考察重点")
    lines.append("")
    if not focus:
        lines.append("（尚未生成）")
    for f in focus:
        cat = schemas.FOCUS_CATEGORY_LABELS.get(f["category"], f["category"])
        lines.append(f"### {cat}：{_md_escape(f['title'])}")
        lines.append("")
        lines.append(f"- 为什么可能被问：{_md_escape(f['why_asked'])}")
        for q in jloads(f["questions"], []):
            lines.append(f"- 可能问题：{_md_escape(q.get('question', ''))}")
            for fu in q.get("followups", []):
                lines.append(f"  - 追问：{_md_escape(fu)}")
        if jloads(f["materials"], []):
            lines.append(f"- 应准备材料：{'、'.join(_md_escape(m) for m in jloads(f['materials'], []))}")
        for u in jloads(f["uncertainties"], []):
            lines.append(f"- 仍不确定：{_md_escape(u)}")
        lines.append("")
    lines.append("")

    # 5. 准备计划
    tasks = query_all("SELECT * FROM prep_task WHERE session_id=? ORDER BY priority, seq", (sid,))
    lines.append("## 准备计划")
    lines.append("")
    if not tasks:
        lines.append("（尚未生成）")
    for t in tasks:
        practice = "**练习项目（非真实经历，不可作为真实经历陈述）**" if t["is_practice_project"] else ""
        learn = schemas.LEARNABILITY_LABELS.get(t["learnability"], t["learnability"])
        prefix = f"{practice}｜" if practice else ""
        lines.append(f"### {t['priority']}. {prefix}{_md_escape(t['purpose'])}")
        lines.append("")
        lines.append(f"- 能力属性：{learn}")
        lines.append(f"- 建议投入：{t['suggested_hours']} 小时")
        for a in jloads(t["actions"], []):
            lines.append(f"- 行动：{_md_escape(a)}")
        lines.append(f"- 完成产物：{_md_escape(t['deliverable'])}")
        lines.append(f"- 自检标准：{_md_escape(t['self_check'])}")
        lines.append("")
    lines.append("")

    # 6. 依据与不确定性（S9 独立章节）
    lines.append("## 依据与不确定性")
    lines.append("")
    lines.append("本报告每条结论均来自以下之一：JD 原文引用、简历原文引用、或标记为推断并写明不确定原因。")
    lines.append("")
    if focus:
        lines.append("| 考察点 | 依据（requirement） | 仍不确定 |")
        lines.append("|---|---|---|")
        for f in focus:
            links = query_all("SELECT * FROM focus_link WHERE focus_id=?", (f["id"],))
            link_desc = "、".join(_md_escape(l["ref_id"] or l["note"] or "-") for l in links)
            unct = "；".join(_md_escape(u) for u in jloads(f["uncertainties"], []))
            lines.append(f"| {_md_escape(f['title'])} | {link_desc} | {unct} |")
    lines.append("")
    lines.append("引用只证明文字存在，不证明解释正确；匹配状态中 not_in_resume 不等于求职者不具备该能力。")
    lines.append("")

    # 7. 版本信息
    lines.append("## 版本信息")
    lines.append("")
    lines.append(f"- 分类字典：{schemas.TAXONOMY_VERSION}")
    lines.append(f"- 匹配提示词：{schemas.MATCH_PROMPT_VERSION}")
    lines.append(f"- 准备提示词：{schemas.PREP_PROMPT_VERSION}")
    lines.append(f"- 证据文本映射：{schemas.MAPPING_VERSION}")
    lines.append(f"- 模型标注：{_md_escape(model_label)}" + ("（内置规则匹配器，语义待人工确认）" if demo else ""))
    lines.append("")

    lines.extend(['## 公开讨论背景与练习问题', '', research['boundary'], ''])
    for material in research['materials']:
        lines.extend([f"### {_md_escape(material['title'])}（{material['platform']}）",
                      f"- 保存的原文引用：{_md_escape(material['quote'])}",
                      f"- 证据范围：[{material['start']}, {material['end']})；材料状态：{material['source_status']}",
                      f"- 原始来源：{material['source_url'] or '无可用公开链接'}",
                      f"- 本机搜索记录：{material['source_run_id']}", ''])
    for q in research['questions']:
        lines.extend([f"- 背景练习问题：{_md_escape(q['question'])}",
                      f"  - 与岗位的联系：{_md_escape(q['why_relevant'])}",
                      f"  - 原文依据：{_md_escape(q['quote'])}",
                      f"  - 不确定性：{'；'.join(_md_escape(v) for v in q['limitations'])}", ''])
    if not research['materials']:
        lines.append('未选择公开讨论材料；本次准备仅使用目标 JD 与简历。')
    return {"markdown": "\n".join(lines)}


def _prep_json(session: dict, report_id: str, markdown: str) -> str:
    import json as _json
    import interview_research
    sid = session["id"]
    payload = {
        "report_id": report_id,
        "kind": "jobseeker_prep",
        "research_background": interview_research.questions_view(sid),
        "session_id": sid,
        "generated_at": utcnow_iso(),
        "model_label": session["model_label"],
        "markdown": markdown,
        "coverage_note": "本报告仅基于该会话的 JD 与简历，不构成录用建议。",
        "match_prompt_version": schemas.MATCH_PROMPT_VERSION,
        "prep_prompt_version": schemas.PREP_PROMPT_VERSION,
        "mapping_version": schemas.MAPPING_VERSION,
    }
    return _json.dumps(payload, ensure_ascii=False, indent=2)
