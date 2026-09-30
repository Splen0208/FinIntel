# -*- coding: utf-8 -*-
"""社交流水线针对性测试（指令 §5-A / B1 / B4）。

**本文件全部是离线测试**：
- 采集器 = ``social_testkit.FakeCollector``（内存假实现，不是真实采集能力）；
- OpenCLI 子进程 = ``social_testkit.fake_opencli`` 打桩；
- 模型 = ``social_testkit.fake_ai`` 打桩；
- 数据库 = 临时目录里的全新 SQLite，与 ``.local/data/research.sqlite3`` 完全无关。

判定原则：断言按需求文档（指令 §5-A）书写。若某条失败，说明**源码有缺陷**，
不得为了变绿放宽断言或删除用例；请在报告里点名源码位置。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(BACKEND_DIR), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ai_provider  # noqa: E402
import social_analysis  # noqa: E402
import social_pipeline  # noqa: E402
import social_store  # noqa: E402
import store  # noqa: E402
import social_testkit as kit  # noqa: E402
from collectors.contracts import CollectorError, CommentRecord  # noqa: E402

XHS_URL = "https://www.xiaohongshu.com/explore/note_offline_1"


def _counters(run_id: str) -> dict:
    row = store.query_one("SELECT counters FROM research_run WHERE id=?", (run_id,))
    return json.loads(row["counters"]) if row and row["counters"] else {}


def _run_row(run_id: str) -> dict:
    return store.query_one("SELECT * FROM research_run WHERE id=?", (run_id,))


# ---------------------------------------------------------------------------
# 0. 隔离性自检
# ---------------------------------------------------------------------------

def test_isolated_db_never_points_at_business_db() -> None:
    """脚手架自检：隔离库必须是临时路径，绝不能指向真实业务库。"""
    with kit.isolated_db() as db:
        assert store.get_db_path() == db
        assert db != kit.BUSINESS_DB
        assert ".local" not in str(db), f"隔离库落在了项目 .local 下：{db}"


# ---------------------------------------------------------------------------
# A-1 有效引用 → 分析保存成功，finding 与 evidence 均可查（离线）
# ---------------------------------------------------------------------------

def test_valid_refs_produce_finding_and_evidence(monkeypatch) -> None:
    """离线：模型返回落在原文内的引用 → 保存成功，finding/evidence 可查。"""
    with kit.isolated_db():
        run_id = kit.with_run()
        kit.seed_note(run_id, "note_offline_1",
                      "消费金融风控岗位最近招聘需求明显增加", title="招聘趋势")
        fake = kit.install_fake_ai(monkeypatch, "valid")

        result = social_analysis.analyze_xhs(run_id)

        assert result["status"] == "ok", result
        assert len(fake.calls) == 1, "假模型应被调用一次（防止空跑通过）"
        findings = social_store.findings_by_run(run_id)
        assert len(findings) == 1, findings
        evidence = social_store.evidence_by_finding(findings[0]["id"])
        assert len(evidence) == 1
        assert evidence[0]["quote"] in evidence[0]["source_text"], evidence[0]


# ---------------------------------------------------------------------------
# A-2 有标题但引用不存在 → 不产生 finding，返回证据不足（离线）
# ---------------------------------------------------------------------------

def test_title_without_valid_ref_yields_insufficient_evidence(monkeypatch) -> None:
    """离线：走完整分析保存路径，有标题但引文对不上原文 → insufficient_evidence。"""
    with kit.isolated_db():
        run_id = kit.with_run()
        kit.seed_note(run_id, "note_offline_1", "这是可以被引用的原文正文",
                      title="有标题但没有证据支撑")
        kit.install_fake_ai(monkeypatch, "missing_quote")

        result = social_analysis.analyze_xhs(run_id)

        assert result["status"] == "insufficient_evidence", result
        assert social_store.findings_by_run(run_id) == []
        assert kit.snapshot_counter()["evidence"] == 0, "不应落库任何证据"
        row = store.query_one("SELECT status FROM social_analysis WHERE run_id=?",
                              (run_id,))
        assert row is not None and row["status"] == "insufficient_evidence"


# ---------------------------------------------------------------------------
# A-3 非法引用被拒绝（离线）
# ---------------------------------------------------------------------------

def test_empty_topics_yields_insufficient_evidence(monkeypatch) -> None:
    """离线：模型返回空 topics → 证据不足，不产出结论。"""
    with kit.isolated_db():
        run_id = kit.with_run()
        kit.seed_note(run_id, "note_offline_1", "原文正文内容")
        kit.install_fake_ai(monkeypatch, "empty_topics")

        result = social_analysis.analyze_xhs(run_id)

        assert result["status"] == "insufficient_evidence", result
        assert social_store.findings_by_run(run_id) == []
        assert kit.snapshot_counter()["evidence"] == 0


@pytest.mark.parametrize("mode", ["both_ids", "bad_stance", "foreign_id"])
def test_invalid_evidence_refs_are_rejected(monkeypatch, mode: str) -> None:
    """离线：同时给两个快照 ID / 非法 stance / 跨任务快照 ID → 一律拒绝。"""
    with kit.isolated_db():
        run_id = kit.with_run()
        kit.seed_note(run_id, "note_offline_1", "原文正文内容")
        kit.install_fake_ai(monkeypatch, mode)

        result = social_analysis.analyze_xhs(run_id)

        assert result["status"] == "insufficient_evidence", (mode, result)
        assert social_store.findings_by_run(run_id) == [], mode
        assert kit.snapshot_counter()["evidence"] == 0, mode


def test_evidence_without_support_stance_is_rejected(monkeypatch) -> None:
    """离线：只有 context 证据、没有 support → 不产出结论。"""
    with kit.isolated_db():
        run_id = kit.with_run()
        kit.seed_note(run_id, "note_offline_1", "原文正文内容")
        kit.install_fake_ai(monkeypatch, "context_only")

        result = social_analysis.analyze_xhs(run_id)

        assert result["status"] == "insufficient_evidence", result
        assert kit.snapshot_counter()["evidence"] == 0


# ---------------------------------------------------------------------------
# A-4 模型调用失败 → 保存失败回执，原始材料仍可查（离线）
# ---------------------------------------------------------------------------

def test_model_failure_saves_failed_receipt_and_keeps_material(monkeypatch) -> None:
    """离线：模型失败 → social_analysis.status='failed'，材料不丢。"""
    with kit.isolated_db():
        run_id = kit.with_run()
        kit.seed_note(run_id, "note_offline_1", "原文正文内容")
        kit.install_fake_ai(monkeypatch, "raise")

        with pytest.raises(ai_provider.AIError):
            social_analysis.analyze_xhs(run_id)

        row = store.query_one("SELECT status FROM social_analysis WHERE run_id=?",
                              (run_id,))
        assert row is not None, "模型失败也必须留下分析回执"
        assert row["status"] == "failed", row
        assert kit.snapshot_counter()["snapshots"] == 1, "原始快照必须保留"
        assert len(social_store.items_by_run(run_id)) == 1, "原始材料必须可查"


# ---------------------------------------------------------------------------
# A-5 相同输入重试 AI → 不重复结论；采集调用次数不增加（离线）
# ---------------------------------------------------------------------------

def test_retry_organize_reuses_ai_result_and_no_new_platform_calls(monkeypatch) -> None:
    """离线：AI-only 重试复用缓存结论，且不再次发起平台请求。"""
    with kit.isolated_db():
        kit.install_fake_ai(monkeypatch, "valid")
        notes = [kit.make_note("note_offline_1", "第一条笔记正文"),
                 kit.make_note("note_offline_2", "第二条笔记正文")]
        comment = CommentRecord(
            source_comment_id="synthetic_offline_c1", parent_source_id=None,
            root_source_id=None, author_key="offline_user",
            evidence_text="第一条评论内容", published_at="2026-09-20T11:00:00Z",
            fetched_at="", content_status="complete", like_count=0)
        collector = kit.fake_collector(records=notes, comments=[comment])
        counter = kit.install_fake_collector(monkeypatch, collector)
        kit.fast_gate(monkeypatch)

        pipe = social_pipeline.SocialPipeline()
        run_id = kit.with_run()
        asyncio.run(pipe._worker(run_id))

        findings_first = len(social_store.findings_by_run(run_id))
        calls_first = counter.total
        assert calls_first >= 5, f"首次采集应发生多次平台调用：{counter.kinds}"
        assert findings_first == 1, findings_first

        # AI-only 重试
        pipe.retry_organize(run_id)
        thread = pipe._workers.get(run_id)
        assert thread is not None, "retry_organize 未启动重试线程（用例不可空跑通过）"
        # 宽限到 120s：断言的是"线程必须结束"，不是"必须很快结束"。
        # 机器高负载时整机可慢 10 倍（曾观测到全量套件 158s → 1558s），
        # 过紧的墙钟阈值会变成偶发红灯。
        thread.join(timeout=120)
        assert not thread.is_alive(), "重试线程未在 120s 内结束"

        final = _run_row(run_id)
        assert final["state"] in ("completed", "partial"), \
            f"重试不应把任务打成失败/取消：{final['state']} / {final.get('stop_reason')}"
        assert len(social_store.findings_by_run(run_id)) == findings_first, \
            "相同输入重试不得重复产出结论"
        assert counter.total == calls_first, \
            f"重试不得再发起平台请求：{counter.kinds}"
        assert kit.snapshot_counter()["analyses"] == 1, "重试不得新增分析批次"


# ---------------------------------------------------------------------------
# A-6 OpenCLI argv / note 字段映射 / 非预期结构（离线）
# ---------------------------------------------------------------------------

def test_opencli_argv_carries_platform_prefix(tmp_path, monkeypatch) -> None:
    """离线：argv 必须含 xiaohongshu 平台前缀（否则等价于裸 opencli 调用）。"""
    exe = kit.fake_cli_path(tmp_path)
    rec = kit.fake_opencli(monkeypatch, [kit.ok_json({"data": []})])

    asyncio.run(kit.xhs_source(exe).search("风控岗位", limit=5))

    argv = rec.last_argv
    assert argv[1] == "xiaohongshu", f"缺少平台前缀：{argv}"
    assert argv[2] == "search", f"子命令位置不对：{argv}"
    assert "风控岗位" in argv


def test_note_field_value_array_maps_to_content(tmp_path, monkeypatch) -> None:
    """离线：note 的 [{field,value}] 数组必须能映射成正文/标题。"""
    exe = kit.fake_cli_path(tmp_path)
    payload = [{"field": "title", "value": "小红书标题"},
               {"field": "content", "value": "这是笔记正文内容"}]
    kit.fake_opencli(monkeypatch, [kit.ok_json(payload)])

    rec = asyncio.run(kit.xhs_source(exe).detail(XHS_URL))

    assert rec.evidence_text == "这是笔记正文内容", rec
    assert rec.title == "小红书标题", rec
    assert rec.detail_status == "complete", rec


@pytest.mark.parametrize("method,payload", [
    ("search", {"ok": True}),
    ("comments", {"ok": True, "data": {"foo": "bar"}}),
])
def test_unexpected_structure_raises_contract_changed(tmp_path, monkeypatch,
                                                      method: str, payload: dict) -> None:
    """离线：非预期结构必须报 CONTRACT_CHANGED，不能退化成"空结果"。"""
    exe = kit.fake_cli_path(tmp_path)
    kit.fake_opencli(monkeypatch, [kit.ok_json(payload)])
    src = kit.xhs_source(exe)
    coro = (src.search("关键词") if method == "search"
            else src.comments(XHS_URL, limit=20))

    with pytest.raises(CollectorError) as exc:
        asyncio.run(coro)

    assert exc.value.code == "CONTRACT_CHANGED", (method, exc.value.code)


# ---------------------------------------------------------------------------
# A-7 无平台 ID 的评论：合成键 / 幂等 / 不伪造 parent（离线）
# ---------------------------------------------------------------------------

def test_comments_without_platform_id_get_distinct_synthetic_keys(tmp_path,
                                                                  monkeypatch) -> None:
    """离线：多条无平台 ID 的评论 → 不同合成键；reply_to 不得当父评论 ID。"""
    exe = kit.fake_cli_path(tmp_path)
    payload = [
        kit.make_comment("第一条评论内容", author="用户A", user_id="u1"),
        kit.make_comment("第二条评论内容", author="用户A", user_id="u1"),
        kit.make_comment("回复内容", author="用户B", user_id="u2",
                         is_reply=True, reply_to="用户A"),
    ]
    kit.fake_opencli(monkeypatch, [kit.ok_json(payload)])

    batch = asyncio.run(kit.xhs_source(exe).comments(XHS_URL, limit=20))

    ids = [c.source_comment_id for c in batch.comments]
    assert len(ids) == 3, ids
    assert len(set(ids)) == 3, f"合成键发生碰撞：{ids}"
    assert all(i.startswith("synthetic_") for i in ids), ids
    assert all(c.identity_quality == "synthetic" for c in batch.comments)

    reply = batch.comments[2]
    assert reply.parent_source_id is None, "reply_to 是展示名，不得当作父评论 ID"
    assert reply.parent_missing is True, "无法确认父关系必须标记 parent_missing"


def test_comment_persist_is_idempotent(tmp_path, monkeypatch) -> None:
    """离线：重复读取同一批评论 → 落库幂等，不重复建快照。"""
    exe = kit.fake_cli_path(tmp_path)
    payload = [
        kit.make_comment("第一条评论内容", author="用户A", user_id="u1"),
        kit.make_comment("第二条评论内容", author="用户A", user_id="u1"),
        kit.make_comment("第三条评论内容", author="用户C", user_id="u3"),
    ]
    kit.fake_opencli(monkeypatch, [kit.ok_json(payload), kit.ok_json(payload)])
    src = kit.xhs_source(exe)
    first_read = asyncio.run(src.comments(XHS_URL, limit=20))
    second_read = asyncio.run(src.comments(XHS_URL, limit=20))

    assert ([c.source_comment_id for c in first_read.comments]
            == [c.source_comment_id for c in second_read.comments]), "重复读取合成键不稳定"

    with kit.isolated_db():
        run_id = kit.with_run()
        item_id = social_store.upsert_item(
            "xiaohongshu", "note_offline_1", "xhs_note",
            canonical_url=XHS_URL, author_key="offline_author")
        social_store.insert_observation(run_id, item_id, None,
                                        query_text="offline",
                                        retrieval_mode="keyword_search")
        pipe = social_pipeline.SocialPipeline()

        pipe._persist_comments(run_id, item_id, first_read)
        after_first = kit.snapshot_counter()["comment_snapshots"]
        pipe._persist_comments(run_id, item_id, second_read)
        after_second = kit.snapshot_counter()["comment_snapshots"]

        assert after_first == 3, after_first
        assert after_second == 3, f"重复读取产生了新快照：{after_second}"
        assert len(social_store.comments_by_item(item_id, run_id)) == 3


# ---------------------------------------------------------------------------
# A-8 第二个详情出现风险 → 停止后续读取，保留已有结果（离线）
# ---------------------------------------------------------------------------

def test_second_detail_risk_stops_pipeline_and_keeps_prior_result(monkeypatch) -> None:
    """离线：第二条详情命中风控 → 不再读第三条、不再读评论；已成功结果保留。"""
    with kit.isolated_db():
        kit.install_fake_ai(monkeypatch, "valid")
        notes = [kit.make_note(f"note_offline_{i}", f"摘要{i}") for i in (1, 2, 3)]
        risk = CollectorError("ACCOUNT_RISK", "小红书账号风控，采集已暂停")
        collector = kit.fake_collector(records=notes).plan_detail(
            kit.make_note("note_offline_1", "完整正文一", detail_status="complete"),
            risk,
            kit.make_note("note_offline_3", "完整正文三", detail_status="complete"),
        )
        counter = kit.install_fake_collector(monkeypatch, collector)
        kit.fast_gate(monkeypatch)

        pipe = social_pipeline.SocialPipeline()
        run_id = kit.with_run()
        asyncio.run(pipe._worker(run_id))

        assert counter.count("detail") == 2, f"风险后不应再读第三条：{counter.kinds}"
        assert counter.count("comments") == 0, f"风险后不应再读评论：{counter.kinds}"

        row = _run_row(run_id)
        assert row["state"] == "paused", row["state"]
        assert row["stop_reason"] == "ACCOUNT_RISK", row["stop_reason"]

        items = social_store.items_by_run(run_id)
        ids = [i["source_id"] for i in items]
        assert "note_offline_1" in ids, "此前成功结果必须保留"
        assert "note_offline_3" not in ids, "第三条不应被读取或保存"
        kept = next(i for i in items if i["source_id"] == "note_offline_1")
        assert kept["detail_status"] == "complete"
        assert kept["evidence_text"].strip()


# ---------------------------------------------------------------------------
# B1 搜索命中必须先全部持久化（离线；当前源码预期不满足）
# ---------------------------------------------------------------------------

def test_all_search_hits_persisted_even_when_first_detail_fails(monkeypatch) -> None:
    """离线：指令 B1 —— 第一条详情就失败时，3 条搜索命中也应已入库。

    当前实现先读详情再入库，命中未落库就退出，页面计数与库内材料会不一致。
    """
    with kit.isolated_db():
        kit.install_fake_ai(monkeypatch, "valid")
        notes = [kit.make_note(f"note_offline_{i}", f"摘要{i}") for i in (1, 2, 3)]
        risk = CollectorError("ACCOUNT_RISK", "小红书账号风控，采集已暂停")
        collector = kit.fake_collector(records=notes).plan_detail(risk)
        kit.install_fake_collector(monkeypatch, collector)
        kit.fast_gate(monkeypatch)

        pipe = social_pipeline.SocialPipeline()
        run_id = kit.with_run()
        asyncio.run(pipe._worker(run_id))

        counters = _counters(run_id)
        ids = [i["source_id"] for i in social_store.items_by_run(run_id)]
        assert sorted(ids) == ["note_offline_1", "note_offline_2", "note_offline_3"], \
            f"搜索命中未全部持久化：库内={ids}，" \
            f"但页面计数 unique_items={counters.get('unique_items')}"


# ---------------------------------------------------------------------------
# B4 中途计数口径：partial 详情不得计入 detail_missing（离线；当前源码预期不满足）
# ---------------------------------------------------------------------------

def test_partial_detail_counted_as_partial_not_missing(monkeypatch) -> None:
    """离线：指令 B4 —— detail_status='partial' 应计入 detail_partial。"""
    with kit.isolated_db():
        kit.install_fake_ai(monkeypatch, "valid")
        notes = [kit.make_note("note_offline_1", "摘要一")]
        partial = kit.make_note("note_offline_1", "正文一（截断）",
                                detail_status="partial")
        collector = kit.fake_collector(records=notes).plan_detail(partial)
        kit.install_fake_collector(monkeypatch, collector)
        kit.fast_gate(monkeypatch)

        pipe = social_pipeline.SocialPipeline()
        run_id = kit.with_run()
        asyncio.run(pipe._worker(run_id))

        counters = _counters(run_id)
        assert counters.get("detail_partial") == 1, f"partial 未单独计数：{counters}"
        assert counters.get("detail_missing") == 0, f"partial 被误计为 missing：{counters}"


# ===========================================================================
# T04 / T05 回归：独立验证新队列语义（离线）
#
# 下面 6 条针对 Engineer 在 T04/T05 里声明的新语义**独立编写**，不复用其结论。
# 每条都带"防空跑"断言，确保真的走到了被测路径。
# ===========================================================================

def _timeout_error() -> CollectorError:
    return CollectorError("TIMEOUT", "小红书采集超时")


def test_unique_items_equals_result_set_size(monkeypatch) -> None:
    """不变量：`unique_items` 必须等于结果集条数（B1 缺陷正是两者不一致）。"""
    with kit.isolated_db():
        kit.install_fake_ai(monkeypatch, "valid")
        notes = [kit.make_note(f"note_offline_{i}", f"正文{i}") for i in (1, 2, 3)]
        collector = kit.fake_collector(records=notes)
        counter = kit.install_fake_collector(monkeypatch, collector)
        kit.fast_gate(monkeypatch)

        pipe = social_pipeline.SocialPipeline()
        run_id = kit.with_run()
        asyncio.run(pipe._worker(run_id))

        assert counter.count("detail") == 3, f"防空跑：正文未逐条读取 {counter.kinds}"
        items = social_store.items_by_run(run_id)
        assert len(items) == 3, [i["source_id"] for i in items]
        assert _counters(run_id)["unique_items"] == len(items), \
            f"计数与库内不一致：{_counters(run_id)['unique_items']} vs {len(items)}"


def test_dependency_missing_is_immediate_stop(monkeypatch) -> None:
    """一票否决：DEPENDENCY_MISSING 第一条命中即停，剩余命中也补进结果集（B1 一致）。"""
    with kit.isolated_db():
        kit.install_fake_ai(monkeypatch, "valid")
        notes = [kit.make_note(f"note_offline_{i}", f"摘要{i}") for i in (1, 2, 3)]
        collector = kit.fake_collector(records=notes).plan_detail(
            CollectorError("DEPENDENCY_MISSING", "opencli 不可用"))
        counter = kit.install_fake_collector(monkeypatch, collector)
        kit.fast_gate(monkeypatch)

        pipe = social_pipeline.SocialPipeline()
        run_id = kit.with_run()
        asyncio.run(pipe._worker(run_id))

        assert counter.count("detail") == 1, f"一票否决后仍在请求详情：{counter.kinds}"
        assert counter.count("comments") == 0, f"一票否决后仍去读评论：{counter.kinds}"
        row = _run_row(run_id)
        assert row["state"] == "paused", row["state"]
        assert row["stop_reason"] == "DEPENDENCY_MISSING", row["stop_reason"]
        counters = _counters(run_id)
        items = social_store.items_by_run(run_id)
        assert len(items) == 3, [i["source_id"] for i in items]
        assert counters["unique_items"] == 3, counters


def test_three_consecutive_timeouts_stop_the_drain(monkeypatch) -> None:
    """TIMEOUT 连续 3 次即停：第 4、5 条不得再发请求，未读命中补进结果集。"""
    with kit.isolated_db():
        kit.install_fake_ai(monkeypatch, "valid")
        notes = [kit.make_note(f"note_offline_{i}", f"摘要{i}") for i in (1, 2, 3, 4, 5)]
        collector = kit.fake_collector(records=notes).plan_detail(
            _timeout_error(), _timeout_error(), _timeout_error())
        counter = kit.install_fake_collector(monkeypatch, collector)
        kit.fast_gate(monkeypatch)

        pipe = social_pipeline.SocialPipeline()
        run_id = kit.with_run()
        asyncio.run(pipe._worker(run_id))

        assert counter.count("detail") == 3, f"连续 3 次超时后不应继续：{counter.kinds}"
        row = _run_row(run_id)
        assert row["state"] == "paused" and row["stop_reason"] == "TIMEOUT", \
            (row["state"], row["stop_reason"])
        assert len(social_store.items_by_run(run_id)) == 5, "5 条命中都应有材料"
        assert _counters(run_id)["unique_items"] == 5


def test_timeout_counter_resets_after_success(monkeypatch) -> None:
    """TIMEOUT 非连续（中途成功）不应误停：5 条都要读到。"""
    with kit.isolated_db():
        kit.install_fake_ai(monkeypatch, "valid")
        notes = [kit.make_note(f"note_offline_{i}", f"摘要{i}") for i in (1, 2, 3, 4, 5)]
        recovered = kit.make_note("note_offline_3", "正文三",
                                  detail_status="complete")
        collector = kit.fake_collector(records=notes).plan_detail(
            _timeout_error(), _timeout_error(), recovered,
            _timeout_error(), _timeout_error())
        counter = kit.install_fake_collector(monkeypatch, collector)
        kit.fast_gate(monkeypatch)

        pipe = social_pipeline.SocialPipeline()
        run_id = kit.with_run()
        asyncio.run(pipe._worker(run_id))

        assert counter.count("detail") == 5, \
            f"第 3 条成功应清零超时计数，不该提前停：{counter.kinds}"
        assert _run_row(run_id)["state"] != "paused", "中途成功后不应被暂停"


def test_locator_token_never_leaks_to_views(monkeypatch) -> None:
    """安全（D3）：带 xsec_token 的定位 URL 只能留在 locator，不得进对外 view。"""
    sentinel = "SECRET_TOKEN_SENTINEL_abc123"
    with kit.isolated_db():
        kit.install_fake_ai(monkeypatch, "valid")
        url = (f"https://www.xiaohongshu.com/explore/note_offline_1"
               f"?xsec_token={sentinel}")
        note = kit.make_note("note_offline_1", "正文一", url=url)
        collector = kit.fake_collector(records=[note])
        counter = kit.install_fake_collector(monkeypatch, collector)
        kit.fast_gate(monkeypatch)

        pipe = social_pipeline.SocialPipeline()
        run_id = kit.with_run()
        asyncio.run(pipe._worker(run_id))

        # 防空跑：token 必须真的被采集路径用上（locator 生效），否则本用例无意义
        assert counter.count("detail") == 1, counter.kinds
        locators = store.query_all("SELECT locator_value, public_url FROM social_locator")
        assert locators, "未写入 locator，用例空跑"
        assert any(sentinel in (r["locator_value"] or "") for r in locators), \
            "locator 未保留带 token 的定位 URL，用例空跑"
        assert all(sentinel not in (r["public_url"] or "") for r in locators), \
            "public_url 仍带 token"

        blob = json.dumps({"task_view": pipe.task_view(run_id),
                           "result_view": pipe.result_view(run_id)},
                          ensure_ascii=False)
        assert sentinel not in blob, "token 泄漏到了 task_view / result_view"


def test_ai_counters_are_batch_scoped_not_post_scoped(monkeypatch) -> None:
    """计数口径：AI 记「批次 / 结论主题」，不得记成「已分析帖子数」，且不留旧键。"""
    with kit.isolated_db():
        kit.install_fake_ai(monkeypatch, "valid")
        notes = [kit.make_note(f"note_offline_{i}", f"正文{i}") for i in (1, 2, 3)]
        kit.install_fake_collector(monkeypatch, kit.fake_collector(records=notes))
        kit.fast_gate(monkeypatch)

        pipe = social_pipeline.SocialPipeline()
        run_id = kit.with_run()
        asyncio.run(pipe._worker(run_id))

        counters = _counters(run_id)
        assert len(social_store.items_by_run(run_id)) == 3, "防空跑：应有 3 条材料"
        assert counters["ai_batch_success"] == 1, f"3 条帖子只能是 1 个批次：{counters}"
        assert counters["ai_topic_count"] == 1, counters
        for legacy in ("ai_success", "ai_failed", "ai_pending", "ai_skipped"):
            assert legacy not in counters, f"旧计数键残留：{legacy}"

        message = _run_row(run_id)["user_message"] or ""
        assert "分析批次" in message and "结论主题" in message, message
