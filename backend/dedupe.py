"""finintel dedupe：岗位重复与疑似重复（任务书 3.6）。

- 同来源稳定岗位 ID 识别同岗；相同 job+content_hash 复用快照。
- 跨 ID 完全相同的职责内容，在已确认同一公司前提下归为一个证据内容组。
- 近似重复只产生 suspected 标记，不自动删除：
  同一确定公司 + 规范化职责不少于 80 字符 + SequenceMatcher 比率不低于 0.90。
- 未复核的疑似重复不能用来升级"多岗一致"。
"""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any, Optional

import store
from company import VersionConflict
from store import get_conn, new_id, query_all, query_one, utcnow_iso

MIN_SIMILARITY = 0.90
MIN_CHARS = 80


def _norm_for_similarity(text: str) -> str:
    return "".join(text.split()).lower()


def compute_similarity(left_resp: str, right_resp: str) -> float:
    return SequenceMatcher(None, _norm_for_similarity(left_resp), _norm_for_similarity(right_resp)).ratio()


def generate_pairs_for_run(run_id: str) -> list[dict[str, Any]]:
    """为运行内同一公司下的快照两两生成疑似重复对（新 run 提交分析后调用）。"""
    snapshots = query_all(
        """SELECT s.id, s.responsibilities_text, j.company_id, j.title, j.city, j.company_name_raw
           FROM run_job rj
           JOIN job_snapshot s ON s.id = rj.snapshot_id
           JOIN job j ON j.id = s.job_id
           WHERE rj.run_id=? AND s.responsibilities_text IS NOT NULL
             AND j.company_id IS NOT NULL""",
        (run_id,),
    )
    created: list[dict[str, Any]] = []
    conn = get_conn()
    for i in range(len(snapshots)):
        for k in range(i + 1, len(snapshots)):
            a, b = snapshots[i], snapshots[k]
            if a["company_id"] != b["company_id"]:
                continue
            if not a["company_id"]:
                continue
            left, right = sorted([a["id"], b["id"]])
            if query_one("SELECT id FROM job_pair WHERE left_snapshot_id=? AND right_snapshot_id=?", (left, right)):
                continue
            resp_a = _norm_for_similarity(a["responsibilities_text"] or "")
            resp_b = _norm_for_similarity(b["responsibilities_text"] or "")
            if len(resp_a) < MIN_CHARS or len(resp_b) < MIN_CHARS:
                continue
            sim = SequenceMatcher(None, resp_a, resp_b).ratio()
            if sim >= MIN_SIMILARITY:
                relation = "duplicate" if sim >= 0.999 else "suspected"
                conn.execute(
                    """INSERT OR IGNORE INTO job_pair
                       (id, left_snapshot_id, right_snapshot_id, similarity, relation, basis, created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (new_id("pair"), left, right, sim, relation,
                     f"同公司职责相似度 {sim:.3f}（阈值 {MIN_SIMILARITY}，≥{MIN_CHARS} 字符），自动生成待人工复核",
                     utcnow_iso()),
                )
                created.append({"left": left, "right": right, "similarity": sim, "relation": relation})
    conn.commit()
    return created


def duplicate_snapshot_group(snapshot_id: str) -> Optional[str]:
    """快照是否与某已复核为 duplicate 的快照成组（用于证据组计数）。"""
    row = query_one(
        """SELECT id FROM job_pair
           WHERE (left_snapshot_id=? OR right_snapshot_id=?) AND relation='duplicate'
           LIMIT 1""",
        (snapshot_id, snapshot_id),
    )
    return row["id"] if row else None


def update_support_levels(run_id: Optional[str] = None) -> int:
    """重算事项支持程度（任务书 3.6）：
    - 多岗一致：至少两组非重复、无待核重复关系的不同岗位支持同一合并事项；
    - 未复核的疑似重复不能升级为多岗一致；
    - 单岗线索：一组有效职责证据。
    返回更新数量。
    """
    conn = get_conn()
    sql = """
        SELECT f.id, f.merged_key, s.id AS snapshot_id, j.id AS job_id
        FROM finding f
        JOIN analysis a ON f.analysis_id = a.id
        JOIN job_snapshot s ON a.snapshot_id = s.id
        JOIN job j ON s.job_id = j.id
        WHERE f.review_status != 'excluded' AND f.merged_key IS NOT NULL
    """
    params: tuple = ()
    if run_id:
        sql += " AND s.job_id IN (SELECT job_id FROM run_job WHERE run_id=?)"
        params = (run_id,)
    rows = query_all(sql, params)

    # 收集每个 merged_key 的岗位集合与疑似重复对
    from collections import defaultdict
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["merged_key"]].append(r)

    updated = 0
    for key, items in groups.items():
        snap_ids = sorted({i["snapshot_id"] for i in items})
        # union-find：已判定 duplicate 的快照（如同一 JD 跨城市转载）
        # 属于同一证据内容组，不加权（BR-09 / 任务书 3.6）
        parent = {s: s for s in snap_ids}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        has_unreviewed_suspect = False
        if len(snap_ids) > 1:
            pairs = query_all(
                """SELECT left_snapshot_id, right_snapshot_id, relation, review_status
                   FROM job_pair WHERE left_snapshot_id IN (%s) AND right_snapshot_id IN (%s)"""
                % (",".join("?" * len(snap_ids)), ",".join("?" * len(snap_ids))),
                tuple(snap_ids) + tuple(snap_ids))
            for p in pairs:
                if p["relation"] == "duplicate":
                    union(p["left_snapshot_id"], p["right_snapshot_id"])
                elif p["relation"] == "suspected" and p["review_status"] == "unreviewed":
                    has_unreviewed_suspect = True
        evidence_groups = {find(s) for s in snap_ids}
        if len(evidence_groups) >= 2:
            level = "unverified" if has_unreviewed_suspect else "multi"
        else:
            level = "single"
        for i in items:
            conn.execute("UPDATE finding SET support_level=? WHERE id=?", (level, i["id"]))
            updated += 1
    conn.commit()
    return updated


def mark_pair(pair_id: str, action: str, reason: str, expected_version: int) -> dict[str, Any]:
    """人工确认/否定疑似重复（mark_duplicate / mark_distinct）。"""
    conn = get_conn()
    pair = query_one("SELECT * FROM job_pair WHERE id=?", (pair_id,))
    if not pair:
        raise KeyError("job_pair 不存在")
    if expected_version != pair["review_version"]:
        raise VersionConflict(
            f"expected_version={expected_version} 与当前版本 {pair['review_version']} 不符")
    before = dict(pair)
    if action == "mark_duplicate":
        conn.execute(
            "UPDATE job_pair SET relation='duplicate', review_version=review_version+1, review_status='confirmed' WHERE id=?",
            (pair_id,))
    elif action == "mark_distinct":
        conn.execute(
            "UPDATE job_pair SET relation='distinct', review_version=review_version+1, review_status='confirmed' WHERE id=?",
            (pair_id,))
    else:
        raise ValueError(f"不支持的 job_pair 复核动作: {action}")
    conn.execute(
        """INSERT INTO review_event (id, target_type, target_id, expected_version, action,
           before_json, after_json, reason, actor, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (new_id("rev"), "job_pair", pair_id, expected_version, action,
         store.jdumps(before), store.jdumps({"relation": "duplicate" if action == "mark_duplicate" else "distinct"}),
         reason, "user", utcnow_iso()),
    )
    conn.commit()
    return query_one("SELECT * FROM job_pair WHERE id=?", (pair_id,))
