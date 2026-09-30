# -*- coding: utf-8 -*-
"""社交 AI 分批与证据回归测试（指令 §5-A 第 1–5 项 / 设计 T09、T10）。

**本文件全部是离线测试**
------------------------
- 模型 = 本地打桩（``social_testkit.fake_ai`` 或本文件内自建假函数），
  **绝不发起任何真实网络请求**；
- 采集器 = ``social_testkit.FakeCollector``（内存假实现，不是真实采集能力）；
- 数据库 = 临时目录里的全新 SQLite，与 ``.local/data/research.sqlite3`` 完全无关。

被测行为（设计 T09 / T10）
--------------------------
1. 长输入触发**多批**：批次回执条数 == 批次数；每批覆盖条数之和 == 输入条数；
   每批 ``input_char_count <= MAX_BATCH_CHARS``。
2. 单条超长内容**单独成批并被截断标注**，不因一条超长而整批失败。
3. **单批失败**：该批标记 failed、其余批结论保留、任务收口为 ``partial``、原始材料仍在。
4. **跨批次引用被拒**（白名单生效，进隔离区）。
5. ``is_current`` 过滤：同一 item 有新旧快照时，只有当前版本进 AI 输入。
6. 缓存命中：相同输入 + 相同 prompt + 相同模型 → 不重复调用模型、不重复产出结论；
   **prompt 文件内容变化 → 缓存失效**（重新分析）。
7. 相同输入重试 AI → 模型调用数不增加、结论不重复、采集调用次数不增加。
8. 有标题但引用不存在 → 不产生 finding（**走完整分析链路**，不只测校验函数）。
"""
from __future__ import annotations

import re
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

# 与 social_analysis 约定的块格式一致：`[<kind>=<id>] ...\n<text>`，块间空行分隔。
_BLOCK_RE = re.compile(
    r"\[(?P<kind>snapshot_id|comment_snapshot_id)=(?P<rid>[^\]]+)\][^\n]*\n"
    r"(?P<text>.*?)(?=\n\n\[|\Z)", re.S)


def _blocks(content: str) -> list[dict[str, str]]:
    return [{"kind": m.group("kind"), "rid": m.group("rid"), "text": m.group("text")}
            for m in _BLOCK_RE.finditer(content)]


def _quote(text: str, n: int = 12) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:n]
    return text.strip()[:n]


def _receipts(analysis_id: str) -> list[dict]:
    return store.query_all(
        "SELECT batch_index, input_hash, input_char_count, item_count, model_label, "
        "prompt_version, status, error_code, elapsed_ms, result_hash "
        "FROM social_analysis_batch WHERE analysis_id=? ORDER BY batch_index",
        (analysis_id,))


def _counters(run_id: str) -> dict:
    import json
    row = store.query_one("SELECT counters FROM research_run WHERE id=?", (run_id,))
    return json.loads(row["counters"]) if row and row["counters"] else {}


def _run_state(run_id: str) -> str:
    return store.query_one("SELECT state FROM research_run WHERE id=?", (run_id,))["state"]


# ---------------------------------------------------------------------------
# T09-1 长输入触发多批、回执条数 == 批次数、覆盖条数之和 == 输入条数（离线）
# ---------------------------------------------------------------------------

def test_long_input_spans_multiple_batches_with_consistent_receipts() -> None:
    """离线：30 条长文本 → ≥3 批；每批字符数不超预算；覆盖条数之和 == 输入条数。"""
    with kit.isolated_db():
        run_id = kit.with_run()
        n = 30
        for i in range(n):
            kit.seed_note(run_id, f"note_offline_{i}", "正文内容" * 60)  # 每条约 240 字

        # 直接用 build_batches 断言边界（不依赖模型）
        snapshots = [s for s in social_analysis._run_snapshots(run_id)
                     if s["detail_status"] in ("complete", "partial")]
        batches = social_analysis.build_batches(snapshots, [])
        assert len(batches) >= 3, f"30 条应切出 ≥3 批：{len(batches)}"
        assert all(b["char_count"] <= social_analysis.MAX_BATCH_CHARS for b in batches)
        assert all(b["item_count"] <= social_analysis.MAX_ITEMS_PER_BATCH for b in batches)
        assert sum(b["item_count"] for b in batches) == n, "覆盖条数之和应等于输入条数"


def test_long_input_receipts_match_batch_count(monkeypatch) -> None:
    """离线：多批分析 → 回执条数 == 批次数；每批覆盖条数之和 == 输入条数。"""
    with kit.isolated_db():
        run_id = kit.with_run()
        n = 30
        for i in range(n):
            kit.seed_note(run_id, f"note_offline_{i}", "正文内容" * 60)
        fake = kit.install_fake_ai(monkeypatch, "valid")

        result = social_analysis.analyze_xhs(run_id)

        assert result["status"] == "ok", result
        assert result["batches_total"] >= 3, result
        assert result["batches_failed"] == 0, result
        receipts = _receipts(result["analysis_id"])
        assert len(receipts) == result["batches_total"], "回执条数应等于批次数"
        assert sum(r["item_count"] for r in receipts) == n, "每批覆盖条数之和应等于输入条数"
        assert all(r["input_char_count"] <= social_analysis.MAX_BATCH_CHARS for r in receipts)
        assert all(r["status"] == "ok" for r in receipts)
        assert all(r["model_label"] and r["prompt_version"] == social_analysis.PROMPT_XHS
                   and r["result_hash"] for r in receipts), "回执字段不完整"
        assert len(fake.calls) == result["batches_total"], "每批恰好一次模型调用"


# ---------------------------------------------------------------------------
# T09-2 单条超长内容单独成批并截断标注（离线）
# ---------------------------------------------------------------------------

def test_oversized_single_item_is_its_own_truncated_batch(monkeypatch) -> None:
    """离线：一条 2 万字符的笔记 → 独占一批、被截断标注、字符数仍在预算内。"""
    with kit.isolated_db():
        run_id = kit.with_run()
        kit.seed_note(run_id, "note_offline_big", "超" * 20000)
        kit.install_fake_ai(monkeypatch, "valid")

        snapshots = [s for s in social_analysis._run_snapshots(run_id)
                     if s["detail_status"] in ("complete", "partial")]
        batches = social_analysis.build_batches(snapshots, [])
        assert len(batches) == 1, "单条超长应独占一批"
        assert batches[0]["truncated"] is True, "超长条必须被标注截断"
        assert batches[0]["char_count"] <= social_analysis.MAX_BATCH_CHARS
        assert social_analysis.TRUNCATION_MARK.strip() in batches[0]["content"]

        result = social_analysis.analyze_xhs(run_id)
        assert result["status"] == "ok", ("超长不应导致整批失败", result)
        receipts = _receipts(result["analysis_id"])
        assert receipts[0]["input_char_count"] <= social_analysis.MAX_BATCH_CHARS


# ---------------------------------------------------------------------------
# T09-3 单批失败：其余批保留、任务 partial、材料仍在（离线）
# ---------------------------------------------------------------------------

def test_single_batch_failure_keeps_others_and_task_is_partial(monkeypatch) -> None:
    """离线：第 1 批失败、第 2 批成功 → 回执 1 failed/1 ok；结论保留；任务 partial。"""
    fail_first = {"n": 0}

    def fake_ai(system_prompt: str, user_content: str, **kwargs: object):
        fail_first["n"] += 1
        if fail_first["n"] == 1:
            raise ai_provider.AIError("AI_TIMEOUT", "离线：模拟第 1 批失败", True, "")
        refs = [{"snapshot_id": b["rid"], "quote": _quote(b["text"]), "stance": "support"}
                for b in _blocks(user_content) if b["kind"] == "snapshot_id"]
        return {"topics": [{"title": "离线主题", "summary_zh": "离线",
                            "evidence_refs": refs, "limitations": ["离线"]}]}

    with kit.isolated_db():
        big = kit.make_note("note_offline_big", "超" * 13000)   # 独占批 0（截断）
        small = kit.make_note("note_offline_small", "小正文一条")  # 批 1
        collector = kit.fake_collector(records=[big, small])
        counter = kit.install_fake_collector(monkeypatch, collector)
        kit.fast_gate(monkeypatch)
        monkeypatch.setattr(ai_provider, "chat_json", fake_ai)

        pipe = social_pipeline.SocialPipeline()
        run_id = kit.with_run()
        import asyncio
        asyncio.run(pipe._worker(run_id))

        assert counter.count("detail") >= 2, f"防空跑：应读到两条正文 {counter.kinds}"
        receipts = store.query_all(
            "SELECT sab.status AS status FROM social_analysis_batch sab JOIN social_analysis sa "
            "ON sa.id = sab.analysis_id WHERE sa.run_id=? ORDER BY sab.batch_index",
            (run_id,))
        assert len(receipts) >= 2, receipts
        statuses = [r["status"] for r in receipts]
        assert "failed" in statuses and "ok" in statuses, statuses

        # 结论保留（第 2 批的 finding 可查）
        assert len(social_store.findings_by_run(run_id)) >= 1, "成功批次的结论必须保留"
        # 原始材料仍在
        assert kit.snapshot_counter()["snapshots"] >= 2, "原始快照必须保留"
        # 任务按 partial 收口
        assert _counters(run_id).get("ai_batch_failed") == 1, _counters(run_id)
        assert _run_state(run_id) == "partial", _run_state(run_id)


# ---------------------------------------------------------------------------
# T09-4 跨批次引用被白名单拒绝（离线）
# ---------------------------------------------------------------------------

def test_cross_batch_reference_rejected_by_whitelist(tmp_path, monkeypatch) -> None:
    """离线：批次引用指向别的批次的快照 → 被 save_evidence 白名单拒绝并进隔离区。"""
    with kit.isolated_db():
        run_id = kit.with_run()
        kit.seed_note(run_id, "note_offline_big", "大正文" * 5000)   # 批 0（截断）
        kit.seed_note(run_id, "note_offline_small", "小正文可以被引用")  # 批 1

        # 映射 source_id → snapshot_id，供假模型构造跨批次引用
        by_source = {s["source_id"]: s["snapshot_id"]
                     for s in social_analysis._run_snapshots(run_id)}
        big_id = by_source["note_offline_big"]
        small_id = by_source["note_offline_small"]

        def fake_ai(system_prompt: str, user_content: str, **kwargs: object):
            blocks = _blocks(user_content)
            refs: list[dict] = []
            for b in blocks:
                if b["kind"] != "snapshot_id":
                    continue
                # 本批次自身的有效引用
                refs.append({"snapshot_id": b["rid"], "quote": _quote(b["text"]),
                             "stance": "support"})
                if b["rid"] == big_id:
                    # 追加一条**跨批次**引用：指向批 1 的快照，且引文确实对得上原文
                    refs.append({"snapshot_id": small_id,
                                 "quote": "小正文可以被引用"[:12], "stance": "support"})
            return {"topics": [{"title": "离线主题", "summary_zh": "离线",
                                "evidence_refs": refs, "limitations": ["离线"]}]}

        monkeypatch.setattr(ai_provider, "chat_json", fake_ai)
        result = social_analysis.analyze_xhs(run_id)

        assert result["status"] == "ok", result
        # 跨批次引用被拒 → 隔离区留下 REF_NOT_ALLOWED 记录
        quarantined = social_store.quarantined_evidence()
        assert any(q["reason"] == "REF_NOT_ALLOWED" and q["snapshot_id"] == small_id
                   for q in quarantined), quarantined
        # 且没有任何证据把批 0 的 finding 绑到批 1 的快照（同一 finding 内不混批）
        for finding in social_store.findings_by_run(run_id):
            ev = social_store.evidence_by_finding(finding["id"])
            sids = {e["snapshot_id"] for e in ev if e["snapshot_id"]}
            assert not ({big_id, small_id} <= sids), "同一结论内混入了跨批次引用"


# ---------------------------------------------------------------------------
# T09-6 is_current 过滤：只有当前快照进 AI 输入（离线）
# ---------------------------------------------------------------------------

def test_is_current_filter_excludes_stale_snapshot(monkeypatch) -> None:
    """离线：同一 item 的新旧快照 → 只有当前版本进入 AI 输入。"""
    with kit.isolated_db():
        run_id = kit.with_run()
        item_id = social_store.upsert_item(
            "xiaohongshu", "note_offline_cur", "xhs_note",
            canonical_url=None, author_key="offline_author")
        old_snap = social_store.insert_snapshot(
            item_id, title="标题", evidence_text="旧版正文不该进模型",
            language="zh", published_at="2026-09-20T10:00:00Z",
            detail_status="complete", collector_version="offline")
        social_store.insert_observation(run_id, item_id, old_snap,
                                        query_text="offline",
                                        retrieval_mode="keyword_search")
        new_snap = social_store.insert_snapshot(
            item_id, title="标题", evidence_text="新版正文可以引用",
            language="zh", published_at="2026-09-21T10:00:00Z",
            detail_status="complete", collector_version="offline")
        social_store.insert_observation(run_id, item_id, new_snap,
                                        query_text="offline",
                                        retrieval_mode="keyword_search")

        current = social_analysis._run_snapshots(run_id)
        assert [s["snapshot_id"] for s in current] == [new_snap], \
            f"只有当前快照应进输入：{[s['snapshot_id'] for s in current]}"

        seen: list[str] = []

        def fake_ai(system_prompt: str, user_content: str, **kwargs: object):
            seen.append(user_content)
            refs = [{"snapshot_id": b["rid"], "quote": _quote(b["text"]),
                     "stance": "support"}
                    for b in _blocks(user_content) if b["kind"] == "snapshot_id"]
            return {"topics": [{"title": "离线主题", "summary_zh": "离线",
                                "evidence_refs": refs, "limitations": ["离线"]}]}

        monkeypatch.setattr(ai_provider, "chat_json", fake_ai)
        social_analysis.analyze_xhs(run_id)

        assert seen, "防空跑：模型应被调用"
        blob = "\n".join(seen)
        assert old_snap not in blob and "旧版正文不该进模型" not in blob, \
            "旧快照泄漏进了 AI 输入"
        assert new_snap in blob


# ---------------------------------------------------------------------------
# T09-5 缓存命中：不重复调用模型、不重复产出结论（离线）
# ---------------------------------------------------------------------------

def test_cache_hit_reuses_result_without_new_model_calls(monkeypatch) -> None:
    """离线：相同输入 + 相同 prompt + 相同模型 → 命中缓存，不重复调用与产出。"""
    with kit.isolated_db():
        run_id = kit.with_run()
        kit.seed_note(run_id, "note_offline_1", "正文内容可以被引用")
        fake = kit.install_fake_ai(monkeypatch, "valid")

        first = social_analysis.analyze_xhs(run_id)
        assert first["status"] == "ok" and first["reused"] is False, first
        calls_after_first = len(fake.calls)
        counts_after_first = kit.snapshot_counter()

        second = social_analysis.analyze_xhs(run_id)
        assert second["reused"] is True, "相同输入必须命中缓存"
        assert second["analysis_id"] == first["analysis_id"], "缓存复用同一条分析"
        assert len(fake.calls) == calls_after_first, "命中缓存不得再次调用模型"
        assert kit.snapshot_counter()["analyses"] == counts_after_first["analyses"] == 1
        assert kit.snapshot_counter()["findings"] == counts_after_first["findings"] == 1


def test_prompt_content_change_invalidates_cache(monkeypatch) -> None:
    """离线：prompt 文件内容变化 → 缓存失效 → 重新分析并再次调用模型。"""
    with kit.isolated_db():
        run_id = kit.with_run()
        kit.seed_note(run_id, "note_offline_1", "正文内容可以被引用")
        fake = kit.install_fake_ai(monkeypatch, "valid")

        first = social_analysis.analyze_xhs(run_id)
        calls_after_first = len(fake.calls)

        original = social_analysis._prompt(social_analysis.PROMPT_XHS)
        monkeypatch.setattr(social_analysis, "_prompt",
                            lambda name: original + "\n【新增约束：只输出中文】")

        second = social_analysis.analyze_xhs(run_id)
        assert second["reused"] is False, "prompt 变化后不得命中旧缓存"
        assert second["analysis_id"] != first["analysis_id"], "应为一次新的分析"
        assert len(fake.calls) == calls_after_first + 1, "prompt 变化必须重新调用模型"
        assert kit.snapshot_counter()["analyses"] == 2


# ---------------------------------------------------------------------------
# T10-5 相同输入重试 → 结论不重复、采集调用次数不增加（离线）
# ---------------------------------------------------------------------------

def test_retry_does_not_duplicate_findings_or_call_platform(monkeypatch) -> None:
    """离线：采集后重跑分析 → 不新增平台请求、不重复产出结论。"""
    import asyncio
    from collectors.contracts import CommentRecord

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

        platform_calls = counter.total
        findings_before = len(social_store.findings_by_run(run_id))
        analyses_before = kit.snapshot_counter()["analyses"]
        assert platform_calls >= 5, f"首次采集应有多笔平台调用：{counter.kinds}"
        assert findings_before >= 1, findings_before

        again = social_analysis.analyze_xhs(run_id)

        assert again["reused"] is True, "相同输入重跑应命中缓存"
        assert counter.total == platform_calls, \
            f"重跑分析不得再发起平台请求：{counter.kinds}"
        assert len(social_store.findings_by_run(run_id)) == findings_before, \
            "不得重复产出结论"
        assert kit.snapshot_counter()["analyses"] == analyses_before == 1, \
            "不得新增分析版本"


# ---------------------------------------------------------------------------
# T10-2 有标题但引用不存在 → 不产生 finding（走完整链路，离线）
# ---------------------------------------------------------------------------

def test_title_without_valid_ref_produces_no_finding(monkeypatch) -> None:
    """离线：有标题、引文对不上原文 → insufficient_evidence，不落库任何结论/证据。"""
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
# 自检：批次数与字符预算的不变量（离线）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text_len", [0, 1, 100, 11999, 12000, 12001, 50000])
def test_batch_char_budget_invariant(text_len: int) -> None:
    """离线：任意单条长度下，批次字符数都不超过预算（超长则截断标注）。"""
    snapshots = [{"snapshot_id": f"ssn_{i}", "platform": "xiaohongshu",
                  "source_id": f"n{i}", "published_at": None,
                  "detail_status": "complete", "identity_hash": "h",
                  "evidence_text": "字" * text_len} for i in range(3)]
    batches = social_analysis.build_batches(snapshots, [])
    assert batches, "非空输入应产生至少一个批次"
    for b in batches:
        assert b["char_count"] <= social_analysis.MAX_BATCH_CHARS, b["char_count"]
        assert b["item_count"] <= social_analysis.MAX_ITEMS_PER_BATCH
    assert sum(b["item_count"] for b in batches) == 3
