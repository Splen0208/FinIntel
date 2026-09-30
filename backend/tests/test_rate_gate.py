# -*- coding: utf-8 -*-
"""二轮·C：应用层**持久化**频率闸门（账号级串行 / 最小间隔 / 窗口上限 / 风险码硬冷却）。

为什么需要它：上游 CLI 的节流（``api/throttle.py``）是**进程内**的，而本项目每次搜索都新起一个
CLI 进程——进程内节流失效。真正能约束「同一账号短时间反复访问平台」的状态必须落在应用层、落库、
跨重启仍在。全程离线：假适配器 + 可注入时钟，零真实请求、零 sleep。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import test_pipeline as base
import pipeline as pipeline_mod
import rate_gate
import runs as runs_mod
import store
import user_view
from store import jdumps

PIPELINE = pipeline_mod.pipeline
_NO_DIR = Path(__file__).resolve().parent / "_no_upstream_cache_here"


@pytest.fixture(autouse=True)
def _reset_rate_gate():
    """每条用例前清空闸门：DSN 是共享库，闸门是全局状态，测试之间必须隔离。"""
    rate_gate.reset()
    yield
    rate_gate.reset()


@pytest.fixture
def clock(monkeypatch):
    """可注入时钟：``rate_gate._now`` 指向可变时间，测试直接推进，不 sleep。"""
    state = {"t": 1_000_000.0}
    monkeypatch.setattr(rate_gate, "_now", lambda: state["t"])
    return state


def _upstream_calls(run_id: str) -> int:
    row = store.query_one("SELECT COUNT(*) AS n FROM upstream_call WHERE run_id=?", (run_id,))
    return int(row["n"])


def _capturing_adapter(seen: list, *, raise_code: str | None = None) -> object:
    class _Adapter:
        async def search(self, cli_path, query, city, page, data_dir, timeout_seconds,
                         no_cache=False, top_level_options=None):
            seen.append(page)
            if raise_code:
                from upstream_envelope import FailureLayer, UpstreamError
                raise UpstreamError(raise_code, "上游拒绝", False, "核对账号/环境",
                                    layer=FailureLayer.BUSINESS)
            return {"cards": [], "trace": {}, "page": page, "total": 0,
                    "has_more": False, "empty_result": True}

        async def detail(self, *args, **kwargs):
            return {}

    return _Adapter()


def _zhipin_run(query: str, *, user_initiated: bool = False) -> str:
    run_id = runs_mod.create_run(
        query, {"queries": [query], "source": "demo", "max_search_calls": 5,
                "user_initiated": user_initiated})["id"]
    run = runs_mod.get_run(run_id)
    snapshot = dict(run["config_snapshot"])
    snapshot["source"] = "zhipin"
    store.execute("UPDATE research_run SET config_snapshot=? WHERE id=?",
                  (jdumps(snapshot), run_id))
    return run_id


def _patch_upstream(monkeypatch, seen: list, *, raise_code: str | None = None) -> None:
    monkeypatch.setattr(runs_mod, "_upstream",
                        lambda channel=None: ("boss.exe", SimpleNamespace(path=_NO_DIR)))
    monkeypatch.setattr(runs_mod, "_source_adapter",
                        lambda source: _capturing_adapter(seen, raise_code=raise_code))


def test_detail_risk_preserves_pending_job_and_blocks_further_details(monkeypatch):
    from upstream_envelope import UpstreamError
    run_id = runs_mod.create_run("信贷产品经理", {"queries": ["信贷产品经理"], "source": "demo"})["id"]
    runs_mod.do_search(run_id, "q1", 1)
    refs = [row["job_ref"] for row in store.query_all("SELECT job_ref FROM run_job WHERE run_id=?", (run_id,))]
    assert len(refs) >= 2
    run = runs_mod.get_run(run_id)
    config = dict(run["config_snapshot"], source="zhipin")
    store.execute("UPDATE research_run SET config_snapshot=? WHERE id=?", (jdumps(config), run_id))
    seen = []
    class Adapter:
        async def detail(self, *args, **kwargs):
            seen.append(1)
            raise UpstreamError("ENVIRONMENT_RISK", "您的IP地址存在异常行为.", False, "检查官方页面")
    monkeypatch.setattr(runs_mod, "_source_adapter", lambda source: Adapter())
    monkeypatch.setattr(runs_mod, "_upstream", lambda channel=None: ("boss.exe", _NO_DIR))
    monkeypatch.setattr(runs_mod, "_revalidate_locator", lambda run, job: {"security_id": "test", "lid": "test"})
    with pytest.raises(runs_mod.RunError) as caught:
        runs_mod.fetch_details(run_id, refs[:2])
    assert caught.value.code == "ENVIRONMENT_RISK"
    assert seen == [1]
    assert store.query_one("SELECT detail_outcome FROM run_job WHERE run_id=? AND job_ref=?",
                           (run_id, refs[0]))["detail_outcome"] == "pending"
    assert rate_gate.check_list_request(run_id, manual=True)["code"] == rate_gate.RATE_COOLDOWN
    # Even a restored collectable task cannot send another detail during cooldown.
    store.execute("UPDATE research_run SET state='collecting' WHERE id=?", (run_id,))
    with pytest.raises(runs_mod.RunError) as blocked:
        runs_mod.fetch_details(run_id, refs[1:2])
    assert blocked.value.code == rate_gate.RATE_COOLDOWN
    assert seen == [1]


# ---------------------------------------------------------------------------
# 最小间隔：第二次列表请求被拦，且**不新增** upstream_call
# ---------------------------------------------------------------------------

def test_min_interval_blocks_second_list_request_without_new_upstream_call(monkeypatch, clock) -> None:
    seen: list = []
    _patch_upstream(monkeypatch, seen)
    run_id = _zhipin_run("闸门 最小间隔 用例")

    runs_mod.do_search(run_id, "q1", 1)                 # 第 1 次：放行
    assert seen == [1]
    calls_after_first = _upstream_calls(run_id)
    assert calls_after_first >= 1

    with pytest.raises(runs_mod.RunError) as excinfo:
        runs_mod.do_search(run_id, "q1", 2)             # 60s 内第 2 次：被拦
    assert excinfo.value.code == rate_gate.RATE_MIN_INTERVAL
    assert seen == [1], "被闸门拦下时绝不调用适配器（零新请求）"
    assert _upstream_calls(run_id) == calls_after_first, "被拦不得新增 upstream_call 行"

    clock["t"] += rate_gate.MIN_LIST_INTERVAL_SECONDS + 1
    runs_mod.do_search(run_id, "q1", 2)                 # 间隔满足后放行
    assert seen == [1, 2]


def test_pipeline_waits_without_requests_then_continues(monkeypatch, clock):
    seen = []
    _patch_upstream(monkeypatch, seen)
    run_id = _zhipin_run("自动等待列表间隔")
    runs_mod.do_search(run_id, "q1", 1)
    started = clock["t"]
    def advance(seconds):
        assert seen == [1]
        assert "自动继续" in PIPELINE.task_view(run_id)["task"]["user_message"]
        clock["t"] += seconds
    monkeypatch.setattr(pipeline_mod.time, "sleep", advance)
    monkeypatch.setattr(pipeline_mod.time, "monotonic", lambda: clock["t"])
    PIPELINE._wait_for_list_slot(run_id, manual=False)
    assert clock["t"] - started == 60
    runs_mod.do_search(run_id, "q1", 2, manual=False)
    assert seen == [1, 2]


@pytest.mark.parametrize("stop", ["cancel", "risk"])
def test_pipeline_interval_wait_stops_on_cancel_or_risk(monkeypatch, clock, stop):
    run_id = _zhipin_run("等待期间停止 " + stop)
    rate_gate.record_list_request(run_id)
    def interrupt(seconds):
        clock["t"] += seconds
        if stop == "cancel":
            store.execute("UPDATE research_run SET cancel_requested=1 WHERE id=?", (run_id,))
        else:
            rate_gate.note_risk_stop("ENVIRONMENT_RISK")
    monkeypatch.setattr(pipeline_mod.time, "sleep", interrupt)
    expected = pipeline_mod._Cancelled if stop == "cancel" else runs_mod.RunError
    with pytest.raises(expected) as exc:
        PIPELINE._wait_for_list_slot(run_id, manual=True)
    if stop == "risk":
        assert exc.value.code == rate_gate.RATE_COOLDOWN
    assert _upstream_calls(run_id) == 0

# ---------------------------------------------------------------------------
# 窗口上限：10 分钟内 ≤20 次
# ---------------------------------------------------------------------------

def test_window_limit_rejects_after_twenty_requests_in_ten_minutes() -> None:
    base_t = 2_000_000.0
    for index in range(rate_gate.MAX_REQUESTS_PER_WINDOW):
        rate_gate.record_list_request("r", now=base_t + index * 25.0)
    decision = rate_gate.check_list_request(
        "r", now=base_t + (rate_gate.MAX_REQUESTS_PER_WINDOW - 1) * 25.0 + 61.0)
    assert decision["allowed"] is False
    assert decision["code"] == rate_gate.RATE_WINDOW_LIMIT
    assert decision["wait_seconds"] > 0
    # 窗口滑过之后恢复
    later = base_t + rate_gate.WINDOW_SECONDS + rate_gate.MIN_LIST_INTERVAL_SECONDS + 1
    assert rate_gate.check_list_request("r", now=later)["allowed"] is True


# ---------------------------------------------------------------------------
# 风险码 → 硬冷却：冷却期内**任何**路径都不得发请求
# ---------------------------------------------------------------------------

def test_risk_code_triggers_hard_cooldown_blocking_all_paths(monkeypatch, clock) -> None:
    seen: list = []
    _patch_upstream(monkeypatch, seen, raise_code="ENVIRONMENT_RISK")
    run_id = _zhipin_run("闸门 风控冷却 用例")

    with pytest.raises(runs_mod.RunError):
        runs_mod.do_search(run_id, "q1", 1)             # 上游回风险码 → 立即终止

    snap = rate_gate.snapshot()["state"]
    assert snap["cooldown_reason"] == "ENVIRONMENT_RISK"
    assert int(snap["manual_required"]) == 1
    assert snap["cooldown_until"] is not None

    assert rate_gate.check_list_request(run_id, manual=False)["code"] == rate_gate.RATE_COOLDOWN
    # 冷却期内，人工路径**也**被拦（任何路径都不得发请求）
    assert rate_gate.check_list_request(run_id, manual=True)["code"] == rate_gate.RATE_COOLDOWN

    # 另一个全新任务（账号级）同样被冷却挡住，且不新增 upstream_call
    other = _zhipin_run("闸门 风控冷却 另一个任务")
    before = _upstream_calls(other)
    with pytest.raises(runs_mod.RunError) as excinfo:
        runs_mod.do_search(other, "q1", 1)
    assert excinfo.value.code == rate_gate.RATE_COOLDOWN
    assert _upstream_calls(other) == before
    assert seen == [1], "冷却期内绝不发起新请求"


# ---------------------------------------------------------------------------
# 冷却结束：自动路径仍停，只放行一次人工请求
# ---------------------------------------------------------------------------

def test_after_cooldown_automatic_blocked_but_one_manual_request_allowed(clock) -> None:
    rate_gate.note_risk_stop("ACCOUNT_RISK", now=clock["t"])
    clock["t"] += rate_gate.COOLDOWN_SECONDS + 1         # 冷却结束

    auto = rate_gate.check_list_request("r", manual=False)
    assert auto["allowed"] is False and auto["code"] == rate_gate.RATE_MANUAL_REQUIRED

    manual = rate_gate.check_list_request("r", manual=True)
    assert manual["allowed"] is True, "冷却后允许一次人工请求"

    rate_gate.record_list_request("r")                   # 人工请求已发 → 恢复正常闸门
    assert rate_gate._load()["manual_required"] == 0
    clock["t"] += rate_gate.MIN_LIST_INTERVAL_SECONDS + 1
    assert rate_gate.check_list_request("r", manual=False)["allowed"] is True

    # 冷却仍在时（另一账号路径）仍不放行
    rate_gate.note_risk_stop("ENVIRONMENT_RISK", now=clock["t"])
    assert rate_gate.check_list_request("r", manual=True)["code"] == rate_gate.RATE_COOLDOWN


# ---------------------------------------------------------------------------
# 账号级串行：同一时刻只允许一个采集任务
# ---------------------------------------------------------------------------

def test_account_level_serialization_allows_only_one_collecting_task() -> None:
    granted_a, _ = rate_gate.begin_collection("runA")
    assert granted_a is True
    granted_b, wait = rate_gate.begin_collection("runB")
    assert granted_b is False and wait > 0
    assert rate_gate.check_list_request("runB")["code"] == rate_gate.RATE_BUSY

    rate_gate.end_collection("runA")
    granted_b2, _ = rate_gate.begin_collection("runB")
    assert granted_b2 is True


# ---------------------------------------------------------------------------
# 持久化：跨重启仍在
# ---------------------------------------------------------------------------

def test_gate_state_survives_restart(clock) -> None:
    import threading

    rate_gate.note_risk_stop("ENVIRONMENT_RISK", now=clock["t"])
    row = store.query_one("SELECT cooldown_reason, manual_required FROM rate_gate WHERE id=1")
    assert row["cooldown_reason"] == "ENVIRONMENT_RISK"
    assert int(row["manual_required"]) == 1

    store._local = threading.local()                     # 「重启」：丢弃连接，重新读库
    assert rate_gate.snapshot()["state"]["cooldown_reason"] == "ENVIRONMENT_RISK"


# ---------------------------------------------------------------------------
# 如实告知：任务页 / 结果页必须说清「在等什么、还要等多久」
# ---------------------------------------------------------------------------

def test_gate_views_are_honest_about_why_waiting() -> None:
    view = user_view.user_view("paused", None, "RATE_COOLDOWN")
    assert "频率保护" in view["user_message"]
    assert any(action["id"] == "refresh" for action in view["actions"])
    assert user_view.failure_kind("paused", None, "RATE_MIN_INTERVAL") == "gated"
    assert "10 分钟" in user_view.rate_gate_message(600)
    assert user_view.rate_gate_message(0)          # 无具体秒数也给一句人话


def test_result_and_task_views_surface_the_gate_reason() -> None:
    run_id = base._demo_run(base.DEMO_QUERY)
    PIPELINE._collect_phase(run_id)
    store.execute("UPDATE research_run SET stage='paused', stop_reason=? WHERE id=?",
                  ("RATE_MIN_INTERVAL", run_id))

    result = PIPELINE.result_view(run_id)
    assert result["failure_kind"] == "gated"
    assert "频率保护" in (result["empty_reason"] or "")

    task = PIPELINE.task_view(run_id)["task"]
    assert "频率保护" in task["user_message"]
    assert task["failure_kind"] == "gated"
    assert any(action["id"] == "resume" for action in task["actions"])


# ---------------------------------------------------------------------------
# 集成：采集 worker 真的接上了账号级串行租约（不只测原语）
# ---------------------------------------------------------------------------

def test_worker_respects_account_level_serialization(monkeypatch) -> None:
    """别人持有采集租约时，``_worker`` 必须在**进入采集前**就落成「忙」，且零请求。

    用 ``_collect_phase`` 探针把「worker 级租约」与 ``do_search`` 的逐请求门禁**区分开**：
    必须证明被拦发生在 worker 层（压根没进 ``_collect_phase``），否则两个采集 worker 可能
    同时进详情阶段（``fetch_details`` 不走 ``check_list_request``）。
    """
    seen: list = []
    _patch_upstream(monkeypatch, seen)
    entered: list = []
    monkeypatch.setattr(PIPELINE, "_collect_phase",
                        lambda run_id, **kw: entered.append(run_id))
    rate_gate.begin_collection("holder-run")             # 另一个采集任务正持有租约

    run_id = _zhipin_run("闸门 串行集成 用例")
    PIPELINE._worker(run_id, organize_only=False)        # 直接跑 worker（无网络）

    run = runs_mod.get_run(run_id)
    assert run["stage"] == "paused"
    assert run["stop_reason"] == rate_gate.RATE_BUSY
    assert entered == [], "被串行闸门挡下时绝不进入采集阶段（worker 级就拦住）"
    assert seen == [], "被串行闸门挡下时绝不调用适配器（零新请求）"
    # 他人持有的租约不因本次被拒而被误释放
    assert rate_gate.snapshot()["state"]["lease_run_id"] == "holder-run"


def test_worker_releases_lease_after_finish(monkeypatch) -> None:
    """采集 worker 正常收尾后必须释放租约，下一个任务才能开跑（否则账号被永久锁死）。"""
    seen: list = []
    _patch_upstream(monkeypatch, seen)
    run_id = _zhipin_run("闸门 租约释放 用例")

    PIPELINE._worker(run_id, organize_only=False)

    assert rate_gate.snapshot()["state"]["lease_run_id"] is None
    # 租约释放后，另一个任务能立刻取得租约
    granted, _ = rate_gate.begin_collection("next-run")
    assert granted is True


# ---------------------------------------------------------------------------
# 「用户点搜索」必须算 manual（否则冷却后用户自己搜索会被拒 = 死胡同）
# ---------------------------------------------------------------------------

def test_user_initiated_search_is_manual_but_auto_continuation_is_not(monkeypatch, clock) -> None:
    """回归锁（因果对照）：用户显式发起的任务放行一次；同状态下自动续跑被拒。

    背景缺陷：``manual`` 原先只看 ``force_refresh``，只有「获取最新结果」算人工；用户从首页
    点「搜索并整理」不算 → 冷却结束后用户自己搜索会被 ``RATE_MANUAL_REQUIRED`` 拒掉，
    而界面唯一算人工的入口在**旧任务**上 → 死胡同。
    """
    seen: list = []
    _patch_upstream(monkeypatch, seen)
    user_run = _zhipin_run("闸门 用户发起 用例", user_initiated=True)
    auto_run = _zhipin_run("闸门 自动续跑 用例", user_initiated=False)

    # 造出「风控冷却已结束、manual_required=1」这一状态
    rate_gate.note_risk_stop("ENVIRONMENT_RISK", now=clock["t"])
    clock["t"] += rate_gate.COOLDOWN_SECONDS + 1

    # 自动续跑路径（manual=False）：被拒，零请求
    with pytest.raises(runs_mod.RunError) as auto_exc:
        runs_mod.do_search(auto_run, "q1", 1, manual=False)
    assert auto_exc.value.code == rate_gate.RATE_MANUAL_REQUIRED
    assert seen == []

    # 用户显式发起（快照 user_initiated=True → manual 推导为 True）：放行一次
    runs_mod.do_search(user_run, "q1", 1)
    assert seen == [1], "用户自己点搜索必须被放行，不能拿自动路径的规则拒掉"


def test_start_task_marks_run_user_initiated(monkeypatch) -> None:
    """用户从 /api/search 发起 → 任务快照 ``user_initiated=True``（闸门据此放行）。"""
    captured: dict = {}
    monkeypatch.setattr(PIPELINE, "_assert_env_ready", lambda source: None)
    monkeypatch.setattr(PIPELINE, "_spawn_worker",
                        lambda run_id, **kw: captured.update(run_id=run_id, **kw))

    PIPELINE.start_task("闸门 start_task 显式发起 用例", source="demo")

    assert captured.get("run_id"), "显式搜索应当派发采集 worker"
    run = runs_mod.get_run(captured["run_id"])
    assert run["config_snapshot"]["user_initiated"] is True


def test_resume_task_spawns_non_manual(monkeypatch) -> None:
    """「继续」= 自动续跑路径，必须 manual=False（冷却后自动路径要被拦住）。"""
    run_id = _zhipin_run("闸门 resume 非人工 用例")
    store.execute("UPDATE research_run SET stage='paused' WHERE id=?", (run_id,))
    captured: dict = {}
    monkeypatch.setattr(PIPELINE, "_rearm_channel_for_resume", lambda run_id: False)
    monkeypatch.setattr(PIPELINE, "_login_state", lambda: None)      # 不触发「通道未就绪」
    monkeypatch.setattr(PIPELINE, "_ensure_collectable", lambda run_id: None)
    monkeypatch.setattr(PIPELINE, "_spawn_worker",
                        lambda run_id, **kw: captured.update(run_id=run_id, **kw))

    PIPELINE.resume_task(run_id)

    assert captured.get("run_id") == run_id
    assert captured.get("manual") is False


# ---------------------------------------------------------------------------
# manual 也不能绕过节流边界：最小间隔 / 窗口上限同样生效
# ---------------------------------------------------------------------------

def test_manual_request_still_respects_min_interval(monkeypatch, clock) -> None:
    """用户连点：两次列表请求的最小间隔对 manual 请求同样生效。"""
    seen: list = []
    _patch_upstream(monkeypatch, seen)
    run_id = _zhipin_run("闸门 连点 最小间隔 用例", user_initiated=True)

    runs_mod.do_search(run_id, "q1", 1)                 # 第一次（manual）：放行
    assert seen == [1]
    with pytest.raises(runs_mod.RunError) as exc:
        runs_mod.do_search(run_id, "q1", 2)             # 60s 内第二次（也是 manual）：仍被拦
    assert exc.value.code == rate_gate.RATE_MIN_INTERVAL
    assert seen == [1]


def test_manual_request_still_respects_window_limit() -> None:
    """用户连点：10 分钟窗口上限对 manual 请求同样生效。"""
    base_t = 3_000_000.0
    for index in range(rate_gate.MAX_REQUESTS_PER_WINDOW):
        rate_gate.record_list_request("r", now=base_t + index * 25.0)
    decision = rate_gate.check_list_request(
        "r", manual=True, now=base_t + (rate_gate.MAX_REQUESTS_PER_WINDOW - 1) * 25.0 + 61.0)
    assert decision["allowed"] is False
    assert decision["code"] == rate_gate.RATE_WINDOW_LIMIT


def test_cooldown_blocks_manual_and_reports_wait(clock) -> None:
    """冷却期内人工路径也被拒，并给出「还需等待 X 分钟」的诚实文案。"""
    rate_gate.note_risk_stop("ENVIRONMENT_RISK", now=clock["t"])
    decision = rate_gate.check_list_request("r", manual=True)
    assert decision["allowed"] is False
    assert decision["code"] == rate_gate.RATE_COOLDOWN
    assert decision["wait_seconds"] > 0
    assert "分钟" in decision["message"]
