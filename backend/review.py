"""finintel review：人工复核（任务书 3.7 / US-06）。

- 修改留痕：before/after JSON + 修改原因 + 版本号；版本冲突拒绝（AC-24）。
- 修正后的事项仍须有有效引用；排除属软排除，报告默认不展示但可追溯。
- 模型重新分析不覆盖人工修正（在 analysis._save_analysis 中处理）。
"""
from __future__ import annotations

from typing import Any, Optional

import analysis as analysis_mod
import company as company_mod
import dedupe
import schemas
import store
from store import get_conn, jdumps, jloads, new_id, query_all, query_one, utcnow_iso


class ReviewError(Exception):
    def __init__(self, code: str, message: str, operator_action: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.operator_action = operator_action


ALLOWED_ACTIONS = {"confirm", "correct", "exclude", "confirm_company_mapping",
                   "mark_duplicate", "mark_distinct"}

GENERIC_REVIEW_TARGETS = {"jd_requirement", "match_row", "interview_focus",
                          "prep_task", "resume_item", "capability_item"}

TARGET_FOR_ACTION = {
    "confirm": {"finding"} | GENERIC_REVIEW_TARGETS,
    "correct": {"finding", "analysis"} | GENERIC_REVIEW_TARGETS,
    "exclude": {"finding", "analysis"} | GENERIC_REVIEW_TARGETS,
    "confirm_company_mapping": {"company_alias"},
    "mark_duplicate": {"job_pair"},
    "mark_distinct": {"company_alias", "job_pair"},
}

# correct 允许修改的字段（按 target_type 严格限定，禁止借 patch 修改预算/快照/凭据）
FINDING_PATCH_FIELDS = {"statement", "business_domain", "action_type", "audience",
                        "market", "interpretation", "limitations"}
ANALYSIS_PATCH_FIELDS = {"role_class", "relevance", "reason"}
JD_REQUIREMENT_PATCH_FIELDS = {"category", "text", "judgement_note"}
MATCH_ROW_PATCH_FIELDS = {"match_status", "jd_note", "resume_note", "user_supplement",
                          "reasoning", "uncertainties", "prep_advice"}
INTERVIEW_FOCUS_PATCH_FIELDS = {"category", "title", "why_asked", "questions",
                                "materials", "uncertainties"}
PREP_TASK_PATCH_FIELDS = {"priority", "purpose", "actions", "suggested_hours",
                          "deliverable", "self_check", "learnability", "is_practice_project"}
RESUME_ITEM_PATCH_FIELDS = {"kind", "title", "org", "period_raw", "start", "end"}
CAPABILITY_ITEM_PATCH_FIELDS = {"frequency_class", "business_reason"}

GENERIC_TARGETS = {
    "jd_requirement": {"table": "jd_requirement", "patch": JD_REQUIREMENT_PATCH_FIELDS,
                       "json_fields": set(), "int_fields": set(),
                       "enums": {"category": schemas.RequirementCategory}},
    "match_row": {"table": "match_row", "patch": MATCH_ROW_PATCH_FIELDS,
                  "json_fields": {"uncertainties"}, "int_fields": set(),
                  "enums": {"match_status": schemas.MatchStatus}},
    "interview_focus": {"table": "interview_focus", "patch": INTERVIEW_FOCUS_PATCH_FIELDS,
                        "json_fields": {"questions", "materials", "uncertainties"},
                        "int_fields": set(), "enums": {"category": schemas.FocusCategory}},
    "prep_task": {"table": "prep_task", "patch": PREP_TASK_PATCH_FIELDS,
                  "json_fields": {"actions"}, "int_fields": {"priority", "is_practice_project"},
                  "enums": {"learnability": schemas.Learnability}},
    "resume_item": {"table": "resume_item", "patch": RESUME_ITEM_PATCH_FIELDS,
                    "json_fields": set(), "int_fields": set(), "enums": {}},
    "capability_item": {"table": "capability_item", "patch": CAPABILITY_ITEM_PATCH_FIELDS,
                        "json_fields": set(), "int_fields": set(),
                        "enums": {"frequency_class": schemas.FrequencyClass}},
}


def review(target_type: str, target_id: str, action: str, patch: Optional[dict],
           reason: str, expected_version: int) -> dict[str, Any]:
    if action not in ALLOWED_ACTIONS:
        raise ReviewError("TOOL_NOT_ALLOWED", f"不支持的复核动作: {action}",
                          "动作限定为 confirm/correct/exclude/confirm_company_mapping/mark_duplicate/mark_distinct")
    if target_type not in TARGET_FOR_ACTION[action]:
        raise ReviewError("TOOL_NOT_ALLOWED",
                          f"动作 {action} 不适用于 target_type={target_type}",
                          f"该动作仅适用于 {TARGET_FOR_ACTION[action]}")
    if not reason or not reason.strip():
        raise ReviewError("INVALID_ANALYSIS", "复核必须填写修改原因（留痕要求）", "填写 reason 后重试")

    if target_type == "finding":
        return _review_finding(target_id, action, patch or {}, reason, expected_version)
    if target_type == "analysis":
        return _review_analysis(target_id, action, patch or {}, reason, expected_version)
    if target_type == "company_alias":
        return dict(company_mod.confirm_alias(action, target_id, patch or {}, reason, expected_version))
    if target_type == "job_pair":
        return dict(dedupe.mark_pair(target_id, action, reason, expected_version))
    if target_type in GENERIC_TARGETS:
        return _review_generic(target_type, GENERIC_TARGETS[target_type], target_id, action,
                               patch or {}, reason, expected_version)
    raise ReviewError("TOOL_NOT_ALLOWED", f"未知 target_type: {target_type}")


def _review_finding(finding_id: str, action: str, patch: dict, reason: str,
                    expected_version: int) -> dict:
    conn = get_conn()
    f = query_one("SELECT * FROM finding WHERE id=?", (finding_id,))
    if not f:
        raise ReviewError("INVALID_ANALYSIS", "finding 不存在")
    if expected_version != f["review_version"]:
        raise ReviewError("VERSION_CONFLICT",
                          f"expected_version={expected_version} 落后于当前版本 {f['review_version']}，"
                          "前一修正不会被覆盖（AC-24）",
                          "重新读取最新数据后再提交修正")
    before = dict(f)

    if action == "confirm":
        conn.execute("UPDATE finding SET review_status='confirmed', review_version=review_version+1 WHERE id=?",
                     (finding_id,))
    elif action == "exclude":
        conn.execute("UPDATE finding SET review_status='excluded', review_version=review_version+1 WHERE id=?",
                     (finding_id,))
    elif action == "correct":
        illegal = set(patch.keys()) - FINDING_PATCH_FIELDS
        if illegal:
            raise ReviewError("TOOL_NOT_ALLOWED",
                              f"patch 含不允许的字段: {sorted(illegal)}",
                              f"finding 仅允许修改 {sorted(FINDING_PATCH_FIELDS)}")
        if "business_domain" in patch and patch["business_domain"] not in schemas.TAXONOMY:
            raise ReviewError("INVALID_ANALYSIS",
                              f"business_domain 必须为 {schemas.TAXONOMY_VERSION} 字典取值")
        if "action_type" in patch and patch["action_type"] not in {a.value for a in schemas.ActionType}:
            raise ReviewError("INVALID_ANALYSIS", "action_type 取值非法")
        sets, params = [], []
        for k in FINDING_PATCH_FIELDS:
            if k in patch:
                v = patch[k]
                if k in ("limitations",):
                    v = jdumps(v if isinstance(v, list) else [v])
                if k == "statement" and (not v or len(v) > 300):
                    raise ReviewError("INVALID_ANALYSIS", "statement 需为 1~300 字")
                sets.append(f"{k}=?")
                params.append(v)
        sets.append("review_status='corrected'")
        sets.append("review_version=review_version+1")
        params.append(finding_id)
        conn.execute(f"UPDATE finding SET {', '.join(sets)} WHERE id=?", tuple(params))

        # 修正 statement 等字段后重算合并键（任务书 3.6）
        f2 = query_one("SELECT * FROM finding WHERE id=?", (finding_id,))
        job = query_one(
            """SELECT j.company_id FROM job j JOIN job_snapshot s ON s.job_id=j.id
               JOIN analysis a ON a.snapshot_id=s.id WHERE a.id=?""", (f2["analysis_id"],))
        merged_key = "|".join([
            (job["company_id"] if job and job["company_id"] else "-"),
            f2["business_domain"],
            analysis_mod._normalize_statement_key(f2["statement"]),
            f2["action_type"], f2["audience"] or "-", f2["market"] or "-",
            ",".join(sorted(jloads(f2["product_names"], []))),
        ])
        conn.execute("UPDATE finding SET merged_key=? WHERE id=?", (merged_key, finding_id))

    # 修正后的事项仍须有有效引用（任务书 3.7）
    ev_count = query_one("SELECT COUNT(*) AS n FROM evidence WHERE finding_id=?", (finding_id,))["n"]
    after_row = query_one("SELECT * FROM finding WHERE id=?", (finding_id,))
    if after_row["review_status"] != "excluded" and ev_count == 0:
        raise ReviewError("INVALID_EVIDENCE",
                          "修正后的事项没有任何证据引用，不能保留为事实（软删除请使用 exclude）")

    conn.execute(
        """INSERT INTO review_event (id, target_type, target_id, expected_version, action,
           before_json, after_json, reason, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (new_id("rev"), "finding", finding_id, expected_version, action,
         jdumps(before), jdumps(dict(after_row)), reason, "user", utcnow_iso()))
    conn.commit()
    dedupe.update_support_levels()
    return dict(after_row, evidences=ev_count)


def _review_analysis(analysis_id: str, action: str, patch: dict, reason: str,
                     expected_version: int) -> dict:
    conn = get_conn()
    a = query_one("SELECT * FROM analysis WHERE id=?", (analysis_id,))
    if not a:
        raise ReviewError("INVALID_ANALYSIS", "analysis 不存在")
    if expected_version != a["review_version"]:
        raise ReviewError("VERSION_CONFLICT",
                          f"expected_version={expected_version} 落后于当前版本 {a['review_version']}",
                          "重新读取最新数据后再提交修正")
    before = dict(a)
    if action == "confirm":
        conn.execute("UPDATE analysis SET review_status='confirmed', review_version=review_version+1 WHERE id=?",
                     (analysis_id,))
    elif action == "exclude":
        conn.execute("UPDATE analysis SET review_status='excluded', review_version=review_version+1 WHERE id=?",
                     (analysis_id,))
        # 排除 analysis 时级联软排除其 findings
        conn.execute("UPDATE finding SET review_status='excluded', review_version=review_version+1 WHERE analysis_id=?",
                     (analysis_id,))
    elif action == "correct":
        illegal = set(patch.keys()) - ANALYSIS_PATCH_FIELDS
        if illegal:
            raise ReviewError("TOOL_NOT_ALLOWED",
                              f"patch 含不允许的字段: {sorted(illegal)}",
                              f"analysis 仅允许修改 {sorted(ANALYSIS_PATCH_FIELDS)}")
        if "role_class" in patch and patch["role_class"] not in {x.value for x in schemas.RoleClass}:
            raise ReviewError("INVALID_ANALYSIS", "role_class 取值非法")
        if "relevance" in patch and patch["relevance"] not in {x.value for x in schemas.Relevance}:
            raise ReviewError("INVALID_ANALYSIS", "relevance 取值非法")
        sets, params = [], []
        for k in ANALYSIS_PATCH_FIELDS:
            if k in patch:
                sets.append(f"{k}=?")
                params.append(patch[k])
        sets.append("review_status='corrected'")
        sets.append("review_version=review_version+1")
        params.append(analysis_id)
        conn.execute(f"UPDATE analysis SET {', '.join(sets)} WHERE id=?", tuple(params))
    after_row = query_one("SELECT * FROM analysis WHERE id=?", (analysis_id,))
    conn.execute(
        """INSERT INTO review_event (id, target_type, target_id, expected_version, action,
           before_json, after_json, reason, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (new_id("rev"), "analysis", analysis_id, expected_version, action,
         jdumps(before), jdumps(dict(after_row)), reason, "user", utcnow_iso()))
    conn.commit()
    return dict(after_row)


def _review_generic(target_type: str, spec: dict, target_id: str, action: str,
                    patch: dict, reason: str, expected_version: int) -> dict:
    """求职者侧新增 target_type 的通用复核（match_row/jd_requirement/...）。

    与 finding/analysis 复核一致：版本冲突拒绝、字段白名单、留痕、review_version+1。
    """
    table = spec["table"]
    row = query_one(f"SELECT * FROM {table} WHERE id=?", (target_id,))
    if not row:
        raise ReviewError("INVALID_ANALYSIS", f"{target_type} 不存在")
    if expected_version != row["review_version"]:
        raise ReviewError("VERSION_CONFLICT",
                          f"expected_version={expected_version} 落后于当前版本 {row['review_version']}，"
                          "前一修正不会被覆盖（AC-24）",
                          "重新读取最新数据后再提交修正")
    before = dict(row)
    conn = get_conn()

    if action == "confirm":
        sql = f"UPDATE {table} SET review_status='confirmed', review_version=review_version+1 WHERE id=?"
        store.assert_scope(sql, "jobseeker")
        conn.execute(sql, (target_id,))
    elif action == "exclude":
        sql = f"UPDATE {table} SET review_status='excluded', review_version=review_version+1 WHERE id=?"
        store.assert_scope(sql, "jobseeker")
        conn.execute(sql, (target_id,))
    elif action == "correct":
        illegal = set(patch.keys()) - spec["patch"]
        if illegal:
            raise ReviewError("TOOL_NOT_ALLOWED",
                              f"patch 含不允许的字段: {sorted(illegal)}",
                              f"{target_type} 仅允许修改 {sorted(spec['patch'])}")
        enums = spec.get("enums", {})
        json_fields = spec.get("json_fields", set())
        int_fields = spec.get("int_fields", set())
        sets, params = [], []
        for k in spec["patch"]:
            if k not in patch:
                continue
            v = patch[k]
            if k in enums and v not in {x.value for x in enums[k]}:
                raise ReviewError("INVALID_ANALYSIS", f"{k} 取值非法")
            if k in json_fields:
                v = jdumps(v if isinstance(v, list) else [v])
            if k in int_fields:
                v = int(v)
            sets.append(f"{k}=?")
            params.append(v)
        sets.append("review_status='corrected'")
        sets.append("review_version=review_version+1")
        params.append(target_id)
        sql = f"UPDATE {table} SET {', '.join(sets)} WHERE id=?"
        store.assert_scope(sql, "jobseeker")
        conn.execute(sql, tuple(params))

    after_row = query_one(f"SELECT * FROM {table} WHERE id=?", (target_id,))
    conn.execute(
        """INSERT INTO review_event (id, target_type, target_id, expected_version, action,
           before_json, after_json, reason, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (new_id("rev"), target_type, target_id, expected_version, action,
         jdumps(before), jdumps(dict(after_row)), reason, "user", utcnow_iso()))
    conn.commit()
    return dict(after_row)


# ---------------------------------------------------------------------------
# 待复核与历史查询
# ---------------------------------------------------------------------------

def pending_reviews() -> dict[str, list[dict]]:
    """待复核清单：未复核 findings、待判断 analysis、疑似重复对、未核实公司映射。"""
    findings = query_all(
        """SELECT f.id, f.statement, f.business_domain, f.action_type, f.support_level,
                  a.result_status, j.company_name_raw, j.title, a.id AS analysis_id
           FROM finding f JOIN analysis a ON f.analysis_id=a.id
           JOIN job_snapshot s ON a.snapshot_id=s.id
           JOIN job j ON s.job_id=j.id
           WHERE f.review_status='unreviewed' ORDER BY f.created_at""")
    analyses = query_all(
        """SELECT a.id, a.role_class, a.relevance, a.result_status, a.reason,
                  j.company_name_raw, j.title
           FROM analysis a JOIN job_snapshot s ON a.snapshot_id=s.id
           JOIN job j ON s.job_id=j.id
           WHERE a.review_status='unreviewed'
             AND (a.relevance IN ('adjacent','unknown') OR a.result_status='needs_review'
                  OR a.role_class='unknown')
           ORDER BY a.created_at""")
    pairs = query_all(
        """SELECT p.*, ja.title AS left_title, jb.title AS right_title,
                  ja.company_name_raw AS company
           FROM job_pair p
           JOIN job_snapshot sa ON sa.id=p.left_snapshot_id
           JOIN job_snapshot sb ON sb.id=p.right_snapshot_id
           JOIN job ja ON ja.id=sa.job_id
           JOIN job jb ON jb.id=sb.job_id
           WHERE p.review_status='unreviewed' ORDER BY p.created_at""")
    aliases = query_all(
        """SELECT a.*, c.display_name, c.identity_status,
                  (SELECT COUNT(*) FROM job j WHERE j.company_id=a.company_id) AS job_count
           FROM company_alias a JOIN company c ON c.id=a.company_id
           WHERE a.verified=0 ORDER BY a.created_at""")
    return {"findings": findings, "analyses": analyses, "pairs": pairs, "aliases": aliases}


def review_events(target_type: Optional[str] = None, target_id: Optional[str] = None,
                  limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM review_event"
    conds, params = [], []
    if target_type:
        conds.append("target_type=?")
        params.append(target_type)
    if target_id:
        conds.append("target_id=?")
        params.append(target_id)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = query_all(sql, tuple(params))
    for r in rows:
        r["before_json"] = jloads(r["before_json"], {})
        r["after_json"] = jloads(r["after_json"], {})
    return rows
