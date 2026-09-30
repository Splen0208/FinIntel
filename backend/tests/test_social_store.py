# -*- coding: utf-8 -*-
"""社交存储层离线测试（T03）：身份 hash、证据校验、当前分析版本、列表口径。

**离线**：不发起任何网络请求，不调用真实平台采集器。
本文件里的 source_id / author_key / 正文均为**脱敏形状样本，非真实数据**，
仅用于验证存储层行为，不代表任何平台可用。

覆盖
  1. identity_hash：正文相同但 detail_status / title 变化 → 新版本快照，旧版本不覆写；
  2. 已被证据引用的旧快照永不覆写；
  3. items_by_run 按 is_current 去重（observation 保留来源记录），复合游标正确分页；
  4. save_evidence 全部校验项：双空 FK / 非法 stance / 非法偏移 / 跨任务 / 跨 item；
     非法证据进 quarantine，不静默丢弃；
  5. 当前分析版本：set_current_analysis 后 findings_by_run 只展示当前版本；
  6. comments_by_item：结果页强制任务范围，一般浏览模式显式标注 cross_run。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = BACKEND_DIR / "tests"
for _p in (str(BACKEND_DIR), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import social_store  # noqa: E402
import social_testkit  # noqa: E402
from store import query_all, query_one  # noqa: E402

OFFLINE = "offline"  # 脱敏形状样本标记，非真实平台数据


def _seed_item(source_id: str = "note_offline_1", *, platform: str = "xiaohongshu",
               kind: str = "xhs_note") -> str:
    return social_store.upsert_item(
        platform, source_id, kind,
        canonical_url=f"https://www.xiaohongshu.com/explore/{source_id}",
        author_key=f"{OFFLINE}_author")


def _snap(item_id: str, *, text: str = "离线正文：消费金融岗位讨论",
          title: str = "离线标题", detail_status: str = "complete") -> str:
    snap_id = social_store.insert_snapshot(
        item_id, title=title, evidence_text=text, language="zh",
        published_at="2026-09-20T10:00:00Z", detail_status=detail_status,
        collector_version=f"{OFFLINE}-fixture")
    assert snap_id
    return snap_id


def _chain(run_id: str, *, status: str = "ok") -> tuple[str, str]:
    """建一条 analysis → finding 链，供证据校验使用。"""
    aid = social_store.save_analysis(
        run_id, input_hash=f"{OFFLINE}-ih", schema_version="1.0",
        prompt_version="social_xhs_v1", model_label=f"{OFFLINE}/model", status=status)
    fid = social_store.save_finding(aid, kind="topic", title=f"{OFFLINE}主题",
                                    summary_zh=f"{OFFLINE}摘要")
    return aid, fid


# ---------------------------------------------------------------------------
# 1) identity_hash / 不可变快照
# ---------------------------------------------------------------------------

def test_identity_hash_is_stable_and_field_sensitive() -> None:
    """离线：同一输入稳定；title 或 detail_status 变化 → 身份不同。"""
    base = dict(platform="x", source_id="t1", kind="x_post", title="T",
                evidence_text="body", detail_status="complete", language="en",
                published_at="2026-09-27T10:00:00Z", collector_version="twitter-cli")
    assert social_store.identity_hash(**base) == social_store.identity_hash(**base)
    changed_title = dict(base, title="T2")
    changed_status = dict(base, detail_status="summary")
    assert social_store.identity_hash(**changed_title) != social_store.identity_hash(**base)
    assert social_store.identity_hash(**changed_status) != social_store.identity_hash(**base)
    # 归一化：换行差异不影响身份
    lf = dict(base, evidence_text="a\r\nb  ")
    crlf = dict(base, evidence_text="a\nb")
    assert social_store.identity_hash(**lf) == social_store.identity_hash(**crlf)


def test_detail_status_change_creates_new_snapshot() -> None:
    """离线：正文相同但 summary→complete → 新快照，旧快照仍可查且原文不变。"""
    with social_testkit.isolated_db():
        run_id = social_testkit.with_run()
        item_id = _seed_item()
        text = "离线正文：同一段正文"
        s1 = _snap(item_id, text=text, title="离线标题", detail_status="summary")
        s2 = _snap(item_id, text=text, title="离线标题", detail_status="complete")
        assert s1 != s2, "detail_status 变化应产生新版本快照"

        rows = query_all("SELECT id, evidence_text, identity_quality, detail_status, "
                         "identity_hash FROM social_snapshot WHERE item_id=? ORDER BY rowid",
                         (item_id,))
        assert len(rows) == 2
        assert rows[0]["evidence_text"] == text, "旧快照原文不得被覆写"
        assert rows[0]["identity_quality"] == rows[1]["identity_quality"] == "platform"
        assert rows[0]["identity_hash"] != rows[1]["identity_hash"]
        # 同参数重放 → 幂等，不新增
        s3 = _snap(item_id, text=text, title="离线标题", detail_status="complete")
        assert s3 == s2
        assert social_testkit.snapshot_counter()["snapshots"] == 2
        assert run_id


def test_referenced_snapshot_is_never_overwritten() -> None:
    """离线：已被证据引用的快照，不因后续版本变化被改动。"""
    with social_testkit.isolated_db():
        run_id = social_testkit.with_run()
        item_id = _seed_item()
        text = "离线正文：被引用的一段原文"
        snap_a = _snap(item_id, text=text, title="标题A")
        social_store.insert_observation(run_id, item_id, snap_a, query_text=OFFLINE,
                                        retrieval_mode="keyword_search")
        _, fid = _chain(run_id)
        eid = social_store.save_evidence(fid, quote="被引用的一段原文", start=5, end=13,
                                         stance="support", snapshot_id=snap_a)
        assert eid
        assert social_store.snapshot_is_referenced(snap_a) is True

        before = query_one("SELECT identity_hash, evidence_text FROM social_snapshot WHERE id=?",
                           (snap_a,))
        snap_b = _snap(item_id, text=text, title="标题B")  # 同正文不同标题 → 新版本
        assert snap_b != snap_a
        after = query_one("SELECT identity_hash, evidence_text FROM social_snapshot WHERE id=?",
                          (snap_a,))
        assert before == after, "被引用的旧快照版本不得被覆写"
        # 被引用 → ensure_snapshot_identity 明确不动
        assert social_store.ensure_snapshot_identity(snap_a) == before["identity_hash"]
        assert social_store.evidence_by_finding(fid)[0]["snapshot_id"] == snap_a


# ---------------------------------------------------------------------------
# 2) items_by_run 去重与游标
# ---------------------------------------------------------------------------

def test_items_by_run_dedupes_but_keeps_observations() -> None:
    """离线：同 item 被两个 query 命中 → 列表 1 行，观察 2 行，is_current 恰 1 条。"""
    with social_testkit.isolated_db():
        run_id = social_testkit.with_run()
        item_id = _seed_item()
        snap_id = _snap(item_id)
        social_store.insert_observation(run_id, item_id, snap_id, query_text=f"{OFFLINE}-q1",
                                        retrieval_mode="keyword_search")
        social_store.insert_observation(run_id, item_id, snap_id, query_text=f"{OFFLINE}-q2",
                                        retrieval_mode="keyword_search")

        rows = social_store.items_by_run(run_id)
        assert len(rows) == 1, f"同一 item 只应出现一行：{len(rows)}"
        assert rows[0]["item_id"] == item_id
        assert rows[0]["query_text"] == f"{OFFLINE}-q2"

        observations = query_all(
            "SELECT id, is_current FROM social_observation WHERE run_id=? AND item_id=?",
            (run_id, item_id))
        assert len(observations) == 2, "每次命中都要保留来源记录"
        assert sum(1 for o in observations if o["is_current"] == 1) == 1


def test_items_by_run_cursor_paginates_without_dup() -> None:
    """离线：复合游标 (item_id, snapshot_id) 能正确翻页，不重不漏。"""
    with social_testkit.isolated_db():
        run_id = social_testkit.with_run()
        item_ids = []
        for i in range(5):
            item_id = _seed_item(f"note_offline_{i}")
            snap_id = _snap(item_id, text=f"离线正文 {i}")
            social_store.insert_observation(run_id, item_id, snap_id, query_text=OFFLINE,
                                            retrieval_mode="keyword_search")
            item_ids.append(item_id)

        seen: list[str] = []
        cursor = None
        for _ in range(10):
            page = social_store.items_by_run(run_id, limit=2, cursor=cursor)
            if not page:
                break
            for row in page:
                seen.append(row["item_id"])
            last = page[-1]
            cursor = social_store.encode_cursor(last["item_id"], last["snapshot_id"])
        assert len(seen) == 5, f"应恰好遍历 5 条：{seen}"
        assert len(set(seen)) == 5, f"出现重复：{seen}"
        assert set(seen) == set(item_ids)


# ---------------------------------------------------------------------------
# 3) save_evidence 校验与隔离
# ---------------------------------------------------------------------------

def test_save_evidence_rejects_invalid_and_quarantines() -> None:
    """离线：双空 FK / 非法 stance / 非法偏移 → ValueError，且进隔离区不静默丢弃。"""
    with social_testkit.isolated_db():
        run_id = social_testkit.with_run()
        item_id = _seed_item()
        snap_id = _snap(item_id, text="离线正文：可被引用的原文")
        social_store.insert_observation(run_id, item_id, snap_id, query_text=OFFLINE,
                                        retrieval_mode="keyword_search")
        _, fid = _chain(run_id)

        with pytest.raises(ValueError):
            social_store.save_evidence(fid, quote="离线正文", start=0, end=4,
                                       stance="support")  # 两个 FK 都空
        with pytest.raises(ValueError):
            social_store.save_evidence(fid, quote="离线正文", start=0, end=4,
                                       stance="maybe", snapshot_id=snap_id)
        with pytest.raises(ValueError):
            social_store.save_evidence(fid, quote="离线正文", start=5, end=2,
                                       stance="support", snapshot_id=snap_id)
        with pytest.raises(ValueError):
            social_store.save_evidence(fid, quote="不在原文里的引文", start=0, end=7,
                                       stance="support", snapshot_id=snap_id)

        reasons = {r["reason"] for r in social_store.quarantined_evidence()}
        assert {"FK_PAIR_INVALID", "STANCE_INVALID", "OFFSET_INVALID",
                "QUOTE_NOT_IN_TEXT"} <= reasons, f"非法证据未进隔离区：{reasons}"
        assert social_testkit.snapshot_counter()["evidence"] == 0, "非法证据不应入库"


def test_save_evidence_accepts_valid_and_blocks_cross_run() -> None:
    """离线：合法证据入库；跨任务 / 跨 item 的引用一律拒绝。"""
    with social_testkit.isolated_db():
        run_a = social_testkit.with_run()
        run_b = social_testkit.with_run()
        item_a = _seed_item("note_offline_a")
        item_b = _seed_item("note_offline_b")
        snap_a = _snap(item_a, text="离线正文 A：属于任务 A")
        _snap(item_b, text="离线正文 B：属于任务 B")
        social_store.insert_observation(run_a, item_a, snap_a, query_text=OFFLINE,
                                        retrieval_mode="keyword_search")
        _, fid_a = _chain(run_a)

        eid = social_store.save_evidence(fid_a, quote="离线正文 A", start=0, end=6,
                                         stance="support", snapshot_id=snap_a)
        assert eid
        assert len(social_store.evidence_by_finding(fid_a)) == 1

        # 跨任务：snap_a 没有在 run_b 里被观察过
        _, fid_b = _chain(run_b)
        with pytest.raises(ValueError):
            social_store.save_evidence(fid_b, quote="离线正文 A", start=0, end=6,
                                       stance="support", snapshot_id=snap_a)
        # 跨 item：显式要求属于 item_b
        with pytest.raises(ValueError):
            social_store.save_evidence(fid_a, quote="离线正文 A", start=0, end=6,
                                       stance="support", snapshot_id=snap_a,
                                       item_id=item_b)
        # 批次白名单之外
        with pytest.raises(ValueError):
            social_store.save_evidence(fid_a, quote="离线正文 A", start=0, end=6,
                                       stance="support", snapshot_id=snap_a,
                                       allowed_snapshot_ids=["ssn_other"])
        assert "CROSS_RUN_REF" in {r["reason"] for r in social_store.quarantined_evidence()}


# ---------------------------------------------------------------------------
# 4) 当前分析版本口径
# ---------------------------------------------------------------------------

def test_current_analysis_version_isolation() -> None:
    """离线：旧版本保留审计，展示只用当前版本，不把历史版本混成一份。"""
    with social_testkit.isolated_db():
        run_id = social_testkit.with_run()
        a1, f1 = _chain(run_id)
        a2, f2 = _chain(run_id)

        shown = social_store.findings_by_run(run_id)
        assert {r["analysis_id"] for r in shown} == {a1, a2}, "未指定当前版本时兼容旧行为"
        assert social_store.current_analysis(run_id) is None

        social_store.set_current_analysis(run_id, a2)
        assert social_store.current_analysis(run_id)["id"] == a2
        shown = social_store.findings_by_run(run_id)
        assert [r["id"] for r in shown] == [f2], f"只应展示当前版本结论：{shown}"

        history = social_store.findings_by_run(run_id, include_history=True)
        assert {r["id"] for r in history} == {f1, f2}, "旧版本必须保留可审计"
        # 切换后旧版本仍在库里
        assert query_one("SELECT is_current FROM social_analysis WHERE id=?", (a1,))["is_current"] == 0
        assert query_one("SELECT is_current FROM social_analysis WHERE id=?", (a2,))["is_current"] == 1


# ---------------------------------------------------------------------------
# 5) 评论范围
# ---------------------------------------------------------------------------

def test_comments_scope_is_explicit() -> None:
    """离线：结果页强制任务范围；一般浏览模式显式标注 cross_run。"""
    with social_testkit.isolated_db():
        run_id = social_testkit.with_run()
        item_id = _seed_item()
        comment_id = social_store.upsert_comment(
            item_id, f"{OFFLINE}_c1", parent_source_id=None, root_source_id=None,
            author_key=f"{OFFLINE}_user")
        scs_id = social_store.insert_comment_snapshot(
            comment_id, evidence_text="离线评论正文", published_at="2026-09-20T11:00:00Z",
            content_status="complete")
        social_store.link_run_comment(run_id, scs_id, None)

        scoped = social_store.comments_by_item(item_id, run_id=run_id)
        assert [r["comment_snapshot_id"] for r in scoped] == [scs_id]
        assert all(r["scope"] == "run" for r in scoped)

        browsing = social_store.comments_by_item(item_id)
        assert len(browsing) == 1
        assert browsing[0]["scope"] == "cross_run", "一般浏览模式必须显式标注跨任务"

        with pytest.raises(ValueError):
            social_store.comments_by_item(item_id, require_run=True)
        result_rows = social_store.comments_for_result(run_id, item_id)
        assert [r["comment_snapshot_id"] for r in result_rows] == [scs_id]
