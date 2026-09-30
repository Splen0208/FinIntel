# -*- coding: utf-8 -*-
"""社交多源离线契约测试（任务书 §12.2）：不依赖真实平台与网络。

覆盖：社交表入库幂等、证据校验（引用对不上原文丢弃）、父评论缺失、scope 隔离。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

_TMP = Path(tempfile.mkdtemp(prefix="finintel_social_test_"))
os.environ["FININTEL_DB"] = str(_TMP / "social.sqlite3")

import store  # noqa: E402
import social_store  # noqa: E402
import social_analysis  # noqa: E402

store.apply_migrations()


def _ensure_run(run_id: str) -> str:
    """017 起 social_observation.run_id 有 research_run 外键，测试必须先有真实 run。

    这是迁移 017 有意加上的约束（指令 E3），不是测试可以绕过的东西。
    """
    if not store.query_one("SELECT id FROM research_run WHERE id=?", (run_id,)):
        store.execute(
            "INSERT INTO research_run (id, name, config_snapshot, started_at) "
            "VALUES (?,?,?,?)",
            (run_id, "离线契约测试", "{}", "2026-09-28T00:00:00Z"),
        )
    return run_id


def _seed_x(run_id: str = "run_x1"):
    run_id = _ensure_run(run_id)
    item_id = social_store.upsert_item(
        "x", "tweet_001", "x_post",
        canonical_url="https://x.com/i/status/001", author_key="author_a")
    snap = social_store.insert_snapshot(
        item_id, title=None, evidence_text="BNPL adoption is surging among Gen Z",
        language="en", published_at="2026-09-27T10:00:00Z",
        detail_status="complete", collector_version="twitter-cli")
    social_store.insert_observation(
        run_id, item_id, snap, query_text="BNPL", retrieval_mode="keyword_search",
        metrics={"likes": 100, "views": None})
    return item_id, snap


def test_snapshot_idempotent() -> None:
    item_id, snap = _seed_x()
    snap2 = social_store.insert_snapshot(
        item_id, title=None, evidence_text="BNPL adoption is surging among Gen Z",
        language="en", published_at="2026-09-27T10:00:00Z",
        detail_status="complete", collector_version="twitter-cli")
    assert snap == snap2  # 相同内容不产生新快照


def test_snapshot_new_version_on_change() -> None:
    item_id, snap = _seed_x()
    snap2 = social_store.insert_snapshot(
        item_id, title=None, evidence_text="BNPL adoption is surging among Gen Z (updated)",
        language="en", published_at="2026-09-27T10:00:00Z",
        detail_status="complete", collector_version="twitter-cli")
    assert snap != snap2  # 内容变化 → 新版本


def test_evidence_quote_validation() -> None:
    """引用对不上原文 → 丢弃（不产出无据结论）。"""
    text_by_snapshot = {"snap1": "hello world"}
    refs = [{"snapshot_id": "snap1", "quote": "hello", "stance": "support"},
            {"snapshot_id": "snap1", "quote": "nonexistent", "stance": "support"}]
    valid = social_analysis._validate_evidence_refs(
        refs, text_by_snapshot=text_by_snapshot, text_by_comment={})
    assert len(valid) == 1  # 只有 "hello" 是原文子串
    assert valid[0]["start"] == 0 and valid[0]["end"] == 5


def test_comment_parent_missing() -> None:
    """父评论未采集 → 保存外部 parent id 并标记 parent_missing。"""
    item_id, _ = _seed_x()
    cid = social_store.upsert_comment(
        item_id, "c_child", parent_source_id="c_missing_parent",
        root_source_id="c_root", author_key="user_b", parent_missing=True)
    # comments_by_item 走 INNER JOIN 评论快照，需先建快照
    social_store.insert_comment_snapshot(
        cid, evidence_text="child comment", published_at="2026-09-27T11:00:00Z",
        content_status="complete")
    rows = social_store.comments_by_item(item_id)
    assert rows and rows[0]["parent_missing"] == 1
    assert rows[0]["parent_source_id"] == "c_missing_parent"


def test_evidence_requires_one_snapshot() -> None:
    """evidence 必须且只能绑定一个快照。"""
    with pytest.raises(ValueError):
        social_store.save_evidence("finding_x", quote="q", start=0, end=1,
                                   stance="support", snapshot_id=None,
                                   comment_snapshot_id=None)


def test_scope_isolation_blocks_social_in_jobseeker() -> None:
    """scope 断言：求职者写路径不得写社交表。"""
    with pytest.raises(RuntimeError):
        store.assert_scope("INSERT INTO social_item (id) VALUES ('x')", scope="jobseeker")
