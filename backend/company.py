"""finintel company：公司归一（任务书 3.6）。

归一顺序：同来源稳定公司 ID → 已核实别名表 → 原始名称临时实体。
没有同一性依据时不自动合并；同名企业不得自动合并（BR-07）。
"""
from __future__ import annotations

from typing import Any, Optional

import store
from store import get_conn, jloads, new_id, query_one, utcnow_iso


def ensure_company(company_name_raw: str, source: str,
                   source_company_id: Optional[str] = None) -> dict[str, Any]:
    """查找或创建公司实体，返回 company 行。

    1. 同来源稳定公司 ID 命中已核实别名 → 直接复用；
    2. 未核实的同名来源公司 → 保留为独立临时实体（不自动合并）；
    3. 完全未见过的名称 → 新建 provisional 实体。
    """
    conn = get_conn()

    # 1) 稳定公司 ID 命中已核实别名
    if source_company_id:
        alias = query_one(
            "SELECT * FROM company_alias WHERE source=? AND source_company_id=? AND verified=1",
            (source, source_company_id),
        )
        if alias:
            return query_one("SELECT * FROM company WHERE id=?", (alias["company_id"],))

    # 2) 未核实映射：同来源同名 → 找最近同名临时实体（同一招聘主体的原始名称）
    existing = query_one(
        """SELECT c.* FROM company c
           JOIN company_alias a ON a.company_id = c.id
           WHERE a.source=? AND a.alias=? ORDER BY c.created_at LIMIT 1""",
        (source, company_name_raw),
    )
    if existing:
        return existing

    # 3) 新建临时实体 + 别名记录
    company_id = new_id("cmp")
    now = utcnow_iso()
    conn.execute(
        "INSERT INTO company (id, display_name, identity_status, created_at) VALUES (?,?,?,?)",
        (company_id, company_name_raw, "provisional", now),
    )
    conn.execute(
        """INSERT INTO company_alias (id, company_id, source, alias, source_company_id, basis, verified, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (new_id("al"), company_id, source, company_name_raw, source_company_id,
         "来源原始名称自动建档，未核实法人归属", 0, now),
    )
    conn.commit()
    return query_one("SELECT * FROM company WHERE id=?", (company_id,))


def confirm_alias(action: str, target_alias_id: str, patch: dict[str, Any],
                  reason: str, expected_version: int) -> dict[str, Any]:
    """人工确认/修正公司映射（US-06）。

    confirm_company_mapping：把别名 verified=1 并指向确认的 company_id；
    mark_distinct：把别名标记为独立实体（保持 verified=0，basis 记录理由）。
    版本冲突拒绝（AC-24）。
    """
    conn = get_conn()
    alias = query_one("SELECT * FROM company_alias WHERE id=?", (target_alias_id,))
    if not alias:
        raise KeyError("别名记录不存在")
    current_version = 1 if alias["verified"] else 0
    if expected_version != current_version:
        raise VersionConflict(f"expected_version={expected_version} 落后于当前版本 {current_version}")

    before = dict(alias)
    if action == "confirm_company_mapping":
        target_company_id = patch.get("company_id")
        if not target_company_id or not query_one("SELECT id FROM company WHERE id=?", (target_company_id,)):
            raise ValueError("patch.company_id 必须指向已存在的公司实体")
        conn.execute(
            "UPDATE company_alias SET company_id=?, verified=1, basis=? WHERE id=?",
            (target_company_id, reason or "人工确认映射", target_alias_id),
        )
        # 将该别名下的岗位迁移到确认的公司
        conn.execute(
            """UPDATE job SET company_id=? WHERE company_id=?""",
            (target_company_id, before["company_id"]),
        )
    elif action == "mark_distinct":
        conn.execute(
            "UPDATE company_alias SET basis=? WHERE id=?",
            (reason or "人工确认同名不同主体", target_alias_id),
        )
    else:
        raise ValueError(f"不支持的公司复核动作: {action}")

    conn.execute(
        """INSERT INTO review_event (id, target_type, target_id, expected_version, action,
           before_json, after_json, reason, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (new_id("rev"), "company_alias", target_alias_id, expected_version, action,
         store.jdumps(before), store.jdumps({"verified": 1 if action == "confirm_company_mapping" else alias["verified"]}),
         reason, "user", utcnow_iso()),
    )
    conn.commit()
    return query_one("SELECT * FROM company_alias WHERE id=?", (target_alias_id,))


class VersionConflict(Exception):
    pass


def company_summary(include_demo: bool = False) -> list[dict[str, Any]]:
    """Return company statistics, excluding synthetic jobs unless explicitly requested."""
    source_clause = "" if include_demo else " WHERE j.source!='demo'"
    rows = store.query_all(
        f"""SELECT c.id, c.display_name, c.identity_status,
                   COUNT(DISTINCT j.id) AS job_count
            FROM company c JOIN job j ON j.company_id = c.id
            {source_clause}
            GROUP BY c.id ORDER BY c.created_at"""
    )
    for row in rows:
        finding_source_clause = "" if include_demo else " AND j.source!='demo'"
        findings = store.query_all(
            f"""SELECT f.id, f.merged_key, f.review_status, a.relevance, a.role_class
                FROM finding f JOIN analysis a ON f.analysis_id = a.id
                JOIN job_snapshot s ON a.snapshot_id = s.id
                JOIN job j ON s.job_id = j.id
                WHERE j.company_id = ? AND f.review_status != 'excluded'
                {finding_source_clause}""",
            (row["id"],),
        )
        row["finding_count"] = len(findings)
        row["evidence_groups"] = len({item["merged_key"] for item in findings})
        row["unverified"] = sum(1 for item in findings if item["review_status"] == "unreviewed")
        row["scope"] = "including_demo" if include_demo else "real_only"
    return rows
