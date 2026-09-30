# -*- coding: utf-8 -*-
"""T03 持久化编排器离线单测：阶段推进 / 检查点 / 取消 / 仅重试整理 / 重启恢复。

全部离线（``source='demo'`` + 规则分析器），不访问任何平台、不调用真实模型。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="finintel_pipeline_"))
os.environ["FININTEL_DB"] = str(_TMP_DIR / "pipeline.sqlite3")

import channel_state  # noqa: E402
import login_bridge  # noqa: E402
import pipeline as pipeline_mod  # noqa: E402
import runs as runs_mod  # noqa: E402
import schemas as schemas_mod  # noqa: E402
import store  # noqa: E402
import user_view  # noqa: E402
from store import jdumps, jloads, query_all, query_one  # noqa: E402

store._local = threading.local()
store.apply_migrations()

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
from upstream_envelope import UpstreamError  # noqa: E402

PIPELINE = pipeline_mod.pipeline
DEMO_QUERY = "消费金融 产品经理"


def _setting(key: str, value: str) -> None:
    store.execute("INSERT INTO app_setting (key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


@pytest.fixture(autouse=True)
def _demo_mode():
    _setting("demo_mode", "1")
    store.execute("DELETE FROM login_task")
    yield
    _setting("demo_mode", "1")


def _counter(run_id: str, key: str) -> int:
    run = runs_mod.get_run(run_id)
    return int(run["counters"].get(key, 0) or 0)


def _wait_stage(run_id: str, stages, timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    view = PIPELINE.task_view(run_id)["task"]
    while time.monotonic() < deadline:
        view = PIPELINE.task_view(run_id)["task"]
        if view["stage"] in stages:
            return view
        time.sleep(0.05)
    return view


def _wait_collected(run_id: str, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _counter(run_id, "jobs_unique") > 0:
            return True
        time.sleep(0.05)
    return False


def _run_demo(query: str) -> str:
    task = PIPELINE.start_task(query, source="demo")["task"]
    run_id = task["run_id"]
    view = _wait_stage(run_id, {"completed", "partial", "failed", "cancelled"})
    assert view["stage"] in {"completed", "partial"}, (
        view["stage"], view["user_message"], query)
    return run_id


# ---------------------------------------------------------------------------
# 阶段推进 + 结果物化（UX-05 / UX-07 / UX-11）
# ---------------------------------------------------------------------------

def test_demo_pipeline_reaches_completed_and_materializes_directions() -> None:
    task = PIPELINE.start_task(DEMO_QUERY, source="demo")["task"]
    run_id = task["run_id"]
    assert task["stage"] in pipeline_mod.ACTIVE_RUN_STAGES
    view = _wait_stage(run_id, {"completed", "partial", "failed", "cancelled"})
    assert view["stage"] in {"completed", "partial"}, view["user_message"]
    assert view["user_stage"] in {"completed", "partial"}
    assert view["counters"]["collected"] > 0
    assert view["counters"]["organized"] > 0

    run = runs_mod.get_run(run_id)
    assert run["origin"] == "simple"
    assert run["state"] in {"completed", "partial"}
    assert run["stage_updated_at"]

    checkpoint = jloads(run["checkpoint"], {})
    assert checkpoint["pages_done"]["q1"] == [1, 2]           # ≤2 个搜索页
    assert checkpoint["detail_done_refs"]
    assert checkpoint["analyzed_snapshot_ids"]
    assert checkpoint["directions_built"] is True
    assert len(checkpoint["detail_done_refs"]) <= pipeline_mod.MAX_JOBS

    directions = query_all("SELECT * FROM run_direction WHERE run_id=?", (run_id,))
    assert directions, "run_direction 未物化"
    assert all(row["evidence_count"] > 0 for row in directions)  # 无证据不入表
    assert all(row["job_count"] >= 1 for row in directions)

    result = PIPELINE.result_view(run_id)["summary"]
    assert result["collected"] > 0 and result["organized"] > 0
    assert result["query"] == DEMO_QUERY
    assert result["searched_at"]
    detail = PIPELINE.result_view(run_id)["directions"]
    first = detail[0]
    assert {"company", "display_name", "identity_status", "business_domain", "statement",
            "action_type", "support_level", "review_status", "job_count", "evidence_count",
            "latest_fetched_at"} <= set(first)
    # 有证据的结论优先：multi/unverified 排在 single 之前
    order = {"multi": 0, "unverified": 1, "single": 2}
    levels = [order[row["support_level"]] for row in detail]
    assert levels == sorted(levels), levels
    assert any(row["review_status"] == "unreviewed" for row in detail)


def test_empty_result_keeps_query_and_reports_reason() -> None:
    query = "完全不存在的岗位词 zzz"
    task = PIPELINE.start_task(query, source="demo")["task"]
    run_id = task["run_id"]
    view = _wait_stage(run_id, {"completed", "partial", "failed", "cancelled"})
    assert view["stage"] == "completed"
    assert view["counters"]["collected"] == 0
    assert view["user_stage"] == "empty"
    assert view["user_message"] == "没有找到匹配岗位，保留搜索词"
    assert PIPELINE.result_view(run_id)["empty_reason"] == "没有找到匹配岗位，保留搜索词"
    assert PIPELINE.task_view(run_id)["task"]["query"] == query


# ---------------------------------------------------------------------------
# 检查点 / 重复点击 / 恢复（UX-08 / UX-12）
# ---------------------------------------------------------------------------

def test_resume_from_checkpoint_does_not_recollect() -> None:
    run_id = _run_demo("信贷 检查点续跑")
    search_calls = _counter(run_id, "search_calls")
    detail_calls = _counter(run_id, "detail_calls")
    job_rows = query_one("SELECT COUNT(*) AS n FROM run_job WHERE run_id=?", (run_id,))["n"]

    store.execute("UPDATE research_run SET stage='paused', user_message='任务已暂停，可继续' "
                  "WHERE id=?", (run_id,))
    view = PIPELINE.resume_task(run_id)["task"]
    assert view["stage"] in pipeline_mod.ACTIVE_RUN_STAGES
    view = _wait_stage(run_id, {"completed", "partial", "failed", "cancelled"})
    assert view["stage"] in {"completed", "partial"}

    assert _counter(run_id, "search_calls") == search_calls      # 不重复搜索
    assert _counter(run_id, "detail_calls") == detail_calls      # 不重复取详情
    assert query_one("SELECT COUNT(*) AS n FROM run_job WHERE run_id=?", (run_id,))["n"] == job_rows
    assert query_all("SELECT * FROM search_observation WHERE run_id=?", (run_id,))


def test_recover_pending_pauses_instead_of_recollecting() -> None:
    run_id = _run_demo("资金 重启恢复")
    search_calls = _counter(run_id, "search_calls")
    store.execute("UPDATE research_run SET stage='fetching_details', cancel_requested=0 "
                  "WHERE id=?", (run_id,))
    recovered = PIPELINE.recover_pending()
    assert run_id in recovered
    run = runs_mod.get_run(run_id)
    assert run["stage"] == "paused"
    assert run["user_message"] == "任务已暂停，可继续"
    # 不自动 spawn worker：等待后计数不变、阶段仍为 paused
    time.sleep(1.2)
    assert _counter(run_id, "search_calls") == search_calls
    assert runs_mod.get_run(run_id)["stage"] == "paused"
    view = PIPELINE.task_view(run_id)["task"]
    assert view["can"]["resume"] is True
    assert view["user_message"] == "任务已暂停，可继续"


def test_offline_search_keeps_intent_without_platform_calls() -> None:
    """未登录先搜索（UX-04）：保留关键词、0 次平台调用、不报错。"""
    query = "信贷 未登录先搜索"
    before = len(query_all("SELECT * FROM upstream_call"))
    payload = PIPELINE.start_task(query)
    task, boss = payload["task"], payload["boss"]
    assert task["stage"] == "pending_login"
    assert task["user_stage"] == "pending_login"
    assert boss["state"] == "disconnected"
    assert boss["user_stage"] == "disconnected"
    assert len(query_all("SELECT * FROM upstream_call")) == before
    checkpoint = jloads(runs_mod.get_run(task["run_id"])["checkpoint"], {})
    assert checkpoint["intent"] == "search" and checkpoint["intent_at"]
    # 重复点击返回同一 run_id（同 query 未终态）
    again = PIPELINE.start_task(query)["task"]
    assert again["run_id"] == task["run_id"]
    assert len(query_all("SELECT * FROM research_run WHERE query_text=?", (query,))) == 1
    # 过期的待执行意图不自动执行（护栏）
    store.execute("UPDATE research_run SET checkpoint=? WHERE id=?",
                  (jdumps({**checkpoint, "intent_at": "2000-01-01T00:00:00Z"}), task["run_id"]))
    assert PIPELINE.on_login_connected() is None
    assert runs_mod.get_run(task["run_id"])["stage"] == "pending_login"
    # 清理该待执行意图，避免影响后续「登录后自动继续」用例
    store.execute("UPDATE research_run SET stage='cancelled' WHERE id=?", (task["run_id"],))


def test_login_connected_resumes_pending_intent_once() -> None:
    task = PIPELINE.start_task("账务 登录后自动继续", source="demo")["task"]
    run_id = task["run_id"]
    _wait_stage(run_id, {"completed", "partial", "failed", "cancelled"})
    # 构造一个待登录意图，模拟「登录成功后自动继续」
    store.execute("UPDATE research_run SET stage='pending_login', state='planned', "
                  "cancel_requested=0, checkpoint=?, user_message='等登录' WHERE id=?",
                  (jdumps({"intent": "search", "intent_at": store.utcnow_iso()}), run_id))
    resumed = PIPELINE.on_login_connected()
    assert resumed == run_id
    view = _wait_stage(run_id, {"completed", "partial", "failed", "cancelled"})
    assert view["stage"] in {"completed", "partial"}


# ---------------------------------------------------------------------------
# 取消（UX-12）
# ---------------------------------------------------------------------------

def test_cancel_stops_new_requests_and_keeps_data() -> None:
    task = PIPELINE.start_task("风控 取消保数据", source="demo")["task"]
    run_id = task["run_id"]
    cancelled = PIPELINE.cancel_task(run_id, "测试取消")["task"]
    assert cancelled["stage"] == "cancelled"
    assert cancelled["user_stage"] == "cancelled"
    assert cancelled["user_message"] == "已停止，保留已获取内容"
    run = runs_mod.get_run(run_id)
    assert run["cancel_requested"] == 1 and run["state"] == "stopped"
    snapshot = (
        len(query_all("SELECT * FROM run_job WHERE run_id=?", (run_id,))),
        len(query_all("SELECT * FROM analysis WHERE snapshot_id IN "
                      "(SELECT snapshot_id FROM run_job WHERE run_id=?)", (run_id,))),
        _counter(run_id, "search_calls"),
        _counter(run_id, "detail_calls"),
    )
    time.sleep(1.0)
    assert (
        len(query_all("SELECT * FROM run_job WHERE run_id=?", (run_id,))),
        len(query_all("SELECT * FROM analysis WHERE snapshot_id IN "
                      "(SELECT snapshot_id FROM run_job WHERE run_id=?)", (run_id,))),
        _counter(run_id, "search_calls"),
        _counter(run_id, "detail_calls"),
    ) == snapshot
    assert runs_mod.get_run(run_id)["stage"] == "cancelled"
    with pytest.raises(pipeline_mod.PipelineError) as raised:
        PIPELINE.resume_task(run_id)
    assert raised.value.code == "TOOL_NOT_ALLOWED"
    with pytest.raises(pipeline_mod.PipelineError) as raised:
        PIPELINE.retry_organize(run_id)
    assert raised.value.code == "TOOL_NOT_ALLOWED"


def test_cancel_after_collection_keeps_all_rows() -> None:
    run_id = _run_demo("海外 取消保留已整理")
    counts = {
        "jobs": len(query_all("SELECT * FROM run_job WHERE run_id=?", (run_id,))),
        "directions": len(query_all("SELECT * FROM run_direction WHERE run_id=?", (run_id,))),
    }
    assert counts["jobs"] > 0 and counts["directions"] > 0
    store.execute("UPDATE research_run SET stage='organizing', cancel_requested=0 WHERE id=?",
                  (run_id,))
    PIPELINE.cancel_task(run_id)
    assert len(query_all("SELECT * FROM run_job WHERE run_id=?", (run_id,))) == counts["jobs"]
    assert len(query_all("SELECT * FROM run_direction WHERE run_id=?", (run_id,))) == counts["directions"]
    assert PIPELINE.result_view(run_id)["directions"]


# ---------------------------------------------------------------------------
# 仅重试整理（UX-10）
# ---------------------------------------------------------------------------

def test_retry_organize_does_not_touch_search_or_detail() -> None:
    run_id = _run_demo("催收 仅重试整理")
    search_calls = _counter(run_id, "search_calls")
    detail_calls = _counter(run_id, "detail_calls")
    run = runs_mod.get_run(run_id)
    checkpoint = jloads(run["checkpoint"], {})
    checkpoint["analyzed_snapshot_ids"] = []          # 强迫重新整理
    store.execute("UPDATE research_run SET stage='failed', state='failed', checkpoint=?, "
                  "stop_reason='AI_OUTPUT_INVALID' WHERE id=?",
                  (jdumps(checkpoint), run_id))
    view = PIPELINE.retry_organize(run_id)["task"]
    assert view["stage"] in pipeline_mod.ACTIVE_RUN_STAGES
    view = _wait_stage(run_id, {"completed", "partial", "failed", "cancelled"})
    assert view["stage"] in {"completed", "partial"}
    assert _counter(run_id, "search_calls") == search_calls
    assert _counter(run_id, "detail_calls") == detail_calls
    assert query_all("SELECT * FROM run_direction WHERE run_id=?", (run_id,))
    assert PIPELINE.result_view(run_id)["directions"]


def test_retry_organize_requires_ai_configuration() -> None:
    run_id = _run_demo("账单 未配置模型")
    _setting("demo_mode", "0")
    store.execute("UPDATE research_run SET stage='paused' WHERE id=?", (run_id,))
    with pytest.raises(pipeline_mod.PipelineError) as raised:
        PIPELINE.retry_organize(run_id)
    assert raised.value.code == "AI_NOT_CONFIGURED"
    # 已取得岗位与正文保留，不回退为规则结果
    assert query_all("SELECT * FROM run_job WHERE run_id=?", (run_id,))


def test_ai_not_configured_blocks_before_any_collection() -> None:
    _setting("demo_mode", "0")
    before_runs = len(query_all("SELECT * FROM research_run"))
    before_calls = len(query_all("SELECT * FROM upstream_call"))
    with pytest.raises(pipeline_mod.PipelineError) as raised:
        PIPELINE.start_task("解决方案 未配置模型前置拦截")
    assert raised.value.code == "AI_NOT_CONFIGURED"
    assert len(query_all("SELECT * FROM research_run")) == before_runs
    assert len(query_all("SELECT * FROM upstream_call")) == before_calls


def test_pending_login_runs_are_not_auto_paused() -> None:
    """重启恢复不碰「待登录」意图（设计 §7.3）。"""
    _setting("demo_mode", "1")
    run_id = PIPELINE.start_task("催收 待登录不被暂停", source="demo")["task"]["run_id"]
    store.execute("UPDATE research_run SET stage='pending_login' WHERE id=?", (run_id,))
    PIPELINE.recover_pending()
    assert runs_mod.get_run(run_id)["stage"] == "pending_login"
    # 清理：取消该意图，避免影响后续用例的「登录后自动继续」选择
    store.execute("UPDATE research_run SET stage='cancelled' WHERE id=?", (run_id,))


# ---------------------------------------------------------------------------
# HTTP 接口层（设计 §9.1）
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    with TestClient(app_module.app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_pipeline_http_routes(client) -> None:
    home = client.get("/api/home").json()
    assert home["ok"]
    data = home["data"]
    assert {"boss", "recent", "suggestions", "scope"} <= set(data)
    assert data["suggestions"]
    assert {"state", "user_stage", "user_message", "actions"} <= set(data["boss"])
    assert "AUTH_REQUIRED" not in data["boss"]["user_message"]
    assert "MCP_PROTOCOL_ERROR" not in data["boss"]["user_message"]

    started = client.post("/api/search", json={"query": "电商 产品经理 HTTP 流程", "city": "北京",
                                               "job_type": "product"}).json()
    assert started["ok"], started
    run_id = started["data"]["task"]["run_id"]
    assert set(started["data"]) >= {"task", "boss"}

    fetched = client.get(f"/api/tasks/{run_id}").json()["data"]["task"]
    assert fetched["run_id"] == run_id
    assert set(fetched["counters"]) >= {"collected", "organized", "failed"}
    assert set(fetched["can"]) == {"cancel", "resume", "retry_organize", "view_result",
                                   "export", "refresh"}
    assert fetched["stages"] and all("status" in item for item in fetched["stages"])
    assert "AUTH_REQUIRED" not in fetched["user_message"]
    assert "AI_NOT_CONFIGURED" not in fetched["user_message"]

    assert client.get(f"/api/tasks/{run_id}/result").json()["ok"]
    assert client.get(f"/api/tasks/{run_id}/jobs").json()["ok"]
    assert client.get("/api/history").json()["ok"]
    assert client.get("/api/tasks/run_not_exist").json()["error"]["code"] == "INVALID_ANALYSIS"
    assert client.post("/api/search", json={"query": "   "}).json()["error"]["code"] == \
        "INVALID_ANALYSIS"
    assert client.post(f"/api/tasks/{run_id}/retry-organize").json()["error"]["code"] in {
        "TOOL_NOT_ALLOWED", "AI_NOT_CONFIGURED"}
    assert client.post(f"/api/tasks/{run_id}/cancel", json={}).json()["ok"]

    history = client.get("/api/history").json()["data"]["runs"]
    assert any(item["run_id"] == run_id for item in history)
    assert all(item["origin"] == "simple" for item in history)
    assert {"run_id", "query", "searched_at", "collected", "organized", "stage",
            "state"} <= set(history[0])


def test_pipeline_http_pending_login_flow(client) -> None:
    """HTTP 层的「未登录先搜索」：返回未登录态而不是工程错误。"""
    response = client.post("/api/search", json={"query": "数据研发 未登录 HTTP"}).json()
    assert response["ok"], response
    task = response["data"]["task"]
    assert task["stage"] == "pending_login"
    assert response["data"]["boss"]["state"] == "disconnected"
    assert task["user_message"] == "已保留本次搜索，登录 BOSS 后将自动继续"
    assert client.get("/api/auth/status").json()["data"]["boss"]["user_stage"] == "disconnected"


def test_history_excludes_non_simple_runs(client) -> None:
    advanced = runs_mod.create_run("高级入口运行", {"source": "demo", "queries": ["高级 产品经理"]})
    history = client.get("/api/history").json()["data"]["runs"]
    assert all(item["origin"] == "simple" for item in history)
    assert all(item["run_id"] != advanced["id"] for item in history)
    # 旧路由行为未变
    assert client.get(f"/api/runs/{advanced['id']}").json()["ok"]
    assert client.get("/api/runs").json()["ok"]


# ---------------------------------------------------------------------------
# 岗位类型取值（需求 §3）
# ---------------------------------------------------------------------------

def test_job_type_choices_are_the_four_ruled_values() -> None:
    """取值集合与展示顺序固定；product_engineering 已收编。"""
    assert pipeline_mod.JOB_TYPE_ORDER == (
        "product", "engineering", "product_engineering", "all")
    assert pipeline_mod.JOB_TYPES == frozenset(
        {"product", "engineering", "product_engineering", "all"})


def test_job_type_product_engineering_is_accepted_and_echoed() -> None:
    """新取值被接受，并在 filters 快照与结果 scope 原样回显。"""
    # 独立关键词（仅本用例使用），避免命中既有用例的 run 复用
    task = PIPELINE.start_task(f"{DEMO_QUERY} 岗位类型回显", job_type="product_engineering",
                               source="demo")["task"]
    run_id = task["run_id"]
    view = _wait_stage(run_id, {"completed", "partial", "failed", "cancelled"})
    assert view["stage"] in {"completed", "partial"}, view["user_message"]

    assert jloads(runs_mod.get_run(run_id)["filters"], {})["job_type"] == "product_engineering"
    assert PIPELINE.result_view(run_id)["summary"]["scope"]["job_type"] == "product_engineering"


def test_job_type_rejects_unknown_value_and_lists_all_choices() -> None:
    """非法值仍返回既有错误码；文案列全可接受取值（含新值）。"""
    with pytest.raises(pipeline_mod.PipelineError) as raised:
        PIPELINE.start_task("岗位类型非法值验证", job_type="designer", source="demo")
    assert raised.value.code == "INVALID_ANALYSIS"
    for choice in pipeline_mod.JOB_TYPE_ORDER:
        assert choice in raised.value.operator_action, raised.value.operator_action


def test_job_type_product_engineering_accepted_over_http(client) -> None:
    """HTTP 层同样收编新取值：合法值不因取值被拒，非法值列全可接受取值。"""
    accepted = client.post("/api/search", json={"query": "电商 产品与研发取值 HTTP",
                                               "job_type": "product_engineering"}).json()
    assert accepted["ok"], accepted
    # 未登录：仍是「未登录先搜索」语义，而不是取值相关的报错
    assert accepted["data"]["task"]["stage"] == "pending_login"

    rejected = client.post("/api/search", json={"query": "电商 非法取值 HTTP",
                                               "job_type": "designer"}).json()
    assert rejected["error"]["code"] == "INVALID_ANALYSIS"
    for choice in pipeline_mod.JOB_TYPE_ORDER:
        assert choice in rejected["error"]["operator_action"]


# ---------------------------------------------------------------------------
# 岗位类型 = 读时展示口径（方案 A）
# ---------------------------------------------------------------------------

def test_job_type_display_scope_is_union_over_same_run() -> None:
    """同一 run 的同一批 directions 下：
    product_engineering == product ∪ engineering；all = 不过滤（超集）。"""
    task = PIPELINE.start_task(f"{DEMO_QUERY} 展示口径并集", job_type="all",
                               source="demo")["task"]
    run_id = task["run_id"]
    view = _wait_stage(run_id, {"completed", "partial", "failed", "cancelled"})
    assert view["stage"] in {"completed", "partial"}, view["user_message"]

    def shown(job_type):
        result = PIPELINE.result_view(run_id, job_type=job_type)
        scope = result["summary"]["scope"]
        assert scope["job_type"] == job_type
        assert scope["job_type_applies_to"] == "display"     # 防误读护栏
        return result, {(d["company"], d["business_domain"], d["statement"])
                        for d in result["directions"]}

    everything, all_keys = shown("all")
    prod, prod_keys = shown("product")
    eng, eng_keys = shown("engineering")
    both, both_keys = shown("product_engineering")

    # 先证明过滤真的发生了，否则本用例会退化为恒真断言
    assert prod_keys < all_keys, "product 未过滤"
    assert eng_keys < all_keys, "engineering 未过滤"

    # 核心关系
    assert both_keys == (prod_keys | eng_keys)
    assert all_keys >= both_keys

    # 隐藏条数口径
    assert everything["summary"]["scope"]["hidden_by_job_type"] == 0
    assert both["summary"]["scope"]["hidden_by_job_type"] == 0
    assert prod["summary"]["scope"]["hidden_by_job_type"] == len(all_keys - prod_keys)
    assert eng["summary"]["scope"]["hidden_by_job_type"] == len(all_keys - eng_keys)
    assert (prod["summary"]["scope"]["hidden_by_job_type"]
            + eng["summary"]["scope"]["hidden_by_job_type"]) > 0

    # 展示口径不得改变采集口径与计数
    for other in (prod, eng, both):
        assert other["summary"]["collected"] == everything["summary"]["collected"]
        assert other["summary"]["organized"] == everything["summary"]["organized"]
        assert other["summary"]["failed"] == everything["summary"]["failed"]

    # 复核状态文案：unreviewed 显示为「待核对」
    labels = {d["review_status_label"] for d in everything["directions"]}
    assert "未复核" not in labels
    assert "待核对" in labels
    assert schemas_mod.REVIEW_STATUS_LABELS["unreviewed"] == "待核对"


def test_result_view_rejects_unknown_job_type() -> None:
    """结果视图的展示口径与 /api/search 用同一套取值校验。"""
    run_id = _run_demo("资金 结果口径非法值")
    with pytest.raises(pipeline_mod.PipelineError) as raised:
        PIPELINE.result_view(run_id, job_type="designer")
    assert raised.value.code == "INVALID_ANALYSIS"
    for choice in pipeline_mod.JOB_TYPE_ORDER:
        assert choice in raised.value.operator_action


def test_same_query_rerun_syncs_display_scope_without_new_run() -> None:
    """同 query 改 job_type 再点搜索：复用同一 run、不重复采集，但口径刷新。"""
    query = "资金 清结算 口径刷新"
    run_id = PIPELINE.start_task(query, job_type="all", source="demo")["task"]["run_id"]
    _wait_stage(run_id, {"completed", "partial", "failed", "cancelled"})
    # 造非终态，模拟「用户中途改筛选再点搜索」
    store.execute("UPDATE research_run SET stage='paused', cancel_requested=0 WHERE id=?",
                  (run_id,))
    runs_before = len(query_all("SELECT * FROM research_run"))

    second = PIPELINE.start_task(query, job_type="product", source="demo")["task"]
    assert second["run_id"] == run_id                                     # 复用，不新建
    assert len(query_all("SELECT * FROM research_run")) == runs_before    # 未重复采集
    assert jloads(runs_mod.get_run(run_id)["filters"], {})["job_type"] == "product"
    assert PIPELINE.result_view(run_id)["summary"]["scope"]["job_type"] == "product"

    # 未显式传 job_type 时保留原口径，不清空用户此前的选择
    PIPELINE.start_task(query, source="demo")
    assert jloads(runs_mod.get_run(run_id)["filters"], {})["job_type"] == "product"


def test_algorithm_data_is_included_in_default_scope() -> None:
    """algorithm_data（算法/数据研发）算研发：默认口径不隐藏，product 口径隐藏。"""
    run_id = _run_demo("消费金融 算法口径")
    everything = PIPELINE.result_view(run_id, job_type="all")
    target = everything["directions"][0]
    key = (target["company"], target["business_domain"], target["statement"])
    assert target["finding_ids"], "方向必须带证据才能被测"
    # 把这条方向的证据角色整体改成 algorithm_data
    for finding_id in target["finding_ids"]:
        store.execute("UPDATE analysis SET role_class='algorithm_data' "
                      "WHERE id=(SELECT analysis_id FROM finding WHERE id=?)", (finding_id,))

    def shown(job_type):
        return {(d["company"], d["business_domain"], d["statement"])
                for d in PIPELINE.result_view(run_id, job_type=job_type)["directions"]}

    assert key in shown("all")                  # all = 不过滤
    assert key in shown("product_engineering")  # 默认口径：算法/数据研发并入研发
    assert key not in shown("product")          # product 口径不含 algorithm_data
    assert key not in shown("engineering")      # engineering 口径也不含 algorithm_data


def test_search_http_all_three_paths_share_one_shape(client) -> None:
    """POST /api/search 的三条返回路径必须同一顶层形状 {task, boss}。

    路径：①首次创建 ②同 query 复用 ③显式 reuse_run_id（前端「继续搜索」）。
    前端会无条件读 data.boss，任一路径缺键都会让登录态判断失效。
    """
    payload = {"query": "电商 三路径顶层键 HTTP", "job_type": "all"}
    created = client.post("/api/search", json=payload).json()
    assert created["ok"], created
    run_id = created["data"]["task"]["run_id"]

    reused = client.post("/api/search", json={**payload, "job_type": "product"}).json()
    assert reused["ok"], reused
    assert reused["data"]["task"]["run_id"] == run_id

    explicit = client.post("/api/search", json={"query": "任意词 走显式复用",
                                               "job_type": "all",
                                               "reuse_run_id": run_id}).json()
    assert explicit["ok"], explicit
    assert explicit["data"]["task"]["run_id"] == run_id

    for label, body in (("首次创建", created), ("同 query 复用", reused),
                        ("reuse_run_id", explicit)):
        assert set(body["data"]) == {"task", "boss"}, label
        assert body["data"]["boss"]["state"] in {"connected", "connecting",
                                                 "disconnected"}, label


# ---------------------------------------------------------------------------
# 已连接（含凭证不完整的降级连接）不得被退回 pending_login
# ---------------------------------------------------------------------------

def _insert_partial_session() -> str:
    """写一条「凭证不完整但在线复核通过」的登录会话行（`connected_degraded`）。

    `live_verified_at` 必须有值：这个状态只能由在线复核产出，「不伪造已连接」
    这条红线由 tests/test_login_bridge.py 的端到端用例守住，这里只冻结局面的输入。
    """
    now = store.utcnow_iso()
    store.execute("DELETE FROM login_task")
    store.execute(
        """INSERT INTO login_task
           (id,status,stage,data_dir,cli_path,pid,timeout_seconds,message,stderr_tail,
            error_code,user_message,active,started_at,updated_at,finished_at,
            live_verified_at,exit_code)
           VALUES (?,?,?,?,?,NULL,?,NULL,'',NULL,?,0,?,?,?,?,NULL)""",
        ("login_partial_session", "connected_degraded", "verifying", str(_TMP_DIR), "", 180,
         "BOSS 已连接，但登录信息不完整，长时间搜索可能中断，建议重新登录一次",
         now, now, now, now),
    )
    return "login_partial_session"


@pytest.fixture
def _partial_session():
    login_id = _insert_partial_session()
    yield login_id
    store.execute("DELETE FROM login_task")


@pytest.fixture
def _no_network_worker(monkeypatch):
    """只记录 worker 派发、不真跑：既不断言采集结果，也不会真的去 BOSS 发请求。

    这些用例验证的是「要不要放行采集」这个决策点；采集能否成功由上面 demo 链路
    的用例与 test_login_bridge 的真机链路分别守住。
    """
    calls: list[str] = []
    monkeypatch.setattr(PIPELINE, "_spawn_worker", lambda run_id, **kw: calls.append(run_id))
    return calls


def test_partial_session_starts_creating_not_pending_login(_partial_session,
                                                           _no_network_worker) -> None:
    """降级连接 + 首次搜索 → 直接进 creating 并派发采集 worker。"""
    assert PIPELINE._login_state()["state"] == "connected"
    payload = PIPELINE.start_task("信贷 降级连接直接采集")
    task, boss = payload["task"], payload["boss"]
    assert boss["state"] == "connected"
    assert task["stage"] == "creating", (task["stage"], task["user_message"])
    assert task["user_stage"] == "creating"
    assert task["stage"] != "pending_login"
    assert _no_network_worker == [task["run_id"]]     # 真的派发了采集 worker
    PIPELINE.cancel_task(task["run_id"], "测试收尾")


def test_partial_session_research_same_query_after_login_goes_creating(_partial_session,
                                                                      _no_network_worker) -> None:
    """真机回归：未登录时搜过的同一个词，登录后重复点必须放行，不能退回待登录。"""
    query = "消费信贷 同一词已登录后重搜"
    store.execute("DELETE FROM login_task")           # 第一次点搜索时尚未登录
    offline = PIPELINE.start_task(query)["task"]
    assert offline["stage"] == "pending_login"
    assert _no_network_worker == []

    _insert_partial_session()                          # 会话变为「已连接（凭证不完整）」
    again = PIPELINE.start_task(query)["task"]
    assert again["run_id"] == offline["run_id"]        # 复用同一个 run（不重复采集）
    assert again["stage"] != "pending_login"
    assert again["stage"] in pipeline_mod.ACTIVE_RUN_STAGES
    assert again["user_message"] != "已保留本次搜索，登录 BOSS 后将自动继续"
    assert runs_mod.get_run(again["run_id"])["stage"] == "creating"
    assert again["boss"]["state"] == "connected"
    assert _no_network_worker == [offline["run_id"]]
    PIPELINE.cancel_task(again["run_id"], "测试收尾")


def test_pipeline_http_partial_session_search_is_not_blocked(_partial_session,
                                                             _no_network_worker,
                                                             client) -> None:
    """端到端形状：partial 会话 → POST /api/search → 返回体不得是待登录。"""
    body = client.post("/api/search", json={"query": "资金 降级连接 HTTP 放行"}).json()
    assert body["ok"], body
    task, boss = body["data"]["task"], body["data"]["boss"]
    assert boss["state"] == "connected"
    assert boss["live_verified_at"]
    assert task["stage"] != "pending_login"
    assert task["stage"] in pipeline_mod.ACTIVE_RUN_STAGES
    assert task["user_message"] != "已保留本次搜索，登录 BOSS 后将自动继续"
    assert "登录" not in task["user_message"]
    PIPELINE.cancel_task(task["run_id"], "测试收尾")


# ---------------------------------------------------------------------------
# 进度真实化（T04）：失败/暂停后**未执行**的阶段必须是 skipped，不是 done
# ---------------------------------------------------------------------------

def _demo_run(query: str = "进度真实化 用例") -> str:
    """造一个不派发 worker 的离线任务（demo 源），只用来驱动阶段推进。"""
    return runs_mod.create_run("阶段真实化", {"queries": [query], "source": "demo"})["id"]


def _stage_statuses(run_id: str) -> dict[str, str]:
    return {item["stage"]: item["status"]
            for item in PIPELINE.task_view(run_id)["task"]["stages"]}


def test_search_failure_keeps_later_stages_skipped() -> None:
    """搜索阶段失败：详情/去重/分析/整理/完成一律 skipped（修复前的 bug 是全 done）。"""
    run_id = _demo_run("搜索阶段失败 用例")
    PIPELINE._set_stage(run_id, "searching", "正在搜索岗位")
    PIPELINE._settle_failure(run_id, "OWN_BROWSER_NO_SESSION")

    task = PIPELINE.task_view(run_id)["task"]
    assert task["stage_reached"] == "searching"
    statuses = _stage_statuses(run_id)
    assert statuses["searching"] == "failed"
    for stage in ("fetching_details", "deduping", "analyzing", "organizing", "completed"):
        assert statuses[stage] == "skipped", stage
    # 通道类失败：给恢复动作，且不许出现「换个关键词」的空态话术
    assert task["failure_kind"] == "channel"
    assert task["user_message"] == user_view.CHANNEL_NOT_READY_MESSAGE
    assert "没有找到匹配岗位" not in task["user_message"]


def test_channel_not_ready_settlement_keeps_the_account_fact_intact() -> None:
    """账号已连接但通道未就绪 → 落「连接未就绪」，绝不退回 pending_login。"""
    run_id = _demo_run("通道未就绪 用例")
    PIPELINE._set_stage(run_id, "searching")
    PIPELINE._settle_failure(run_id, pipeline_mod.CHANNEL_NOT_READY_CODE)

    task = PIPELINE.task_view(run_id)["task"]
    assert task["stage"] == "paused"
    assert task["failure_kind"] == "channel"
    assert task["user_message"] == user_view.CHANNEL_NOT_READY_MESSAGE
    assert "登录" not in task["user_message"]


def test_partial_run_keeps_the_stages_that_really_ran() -> None:
    """部分失败 ≠ 阶段失败：真实跑过的阶段保留 done，没跑到的才是 skipped。"""
    run_id = _demo_run("部分失败 用例")
    for stage in ("searching", "fetching_details", "deduping", "analyzing"):
        PIPELINE._set_stage(run_id, stage)
    store.execute("UPDATE research_run SET stage='partial', state='partial' WHERE id=?",
                  (run_id,))

    statuses = _stage_statuses(run_id)
    for stage in ("searching", "fetching_details", "deduping", "analyzing"):
        assert statuses[stage] == "done", stage
    assert statuses["organizing"] == "skipped"
    assert PIPELINE.task_view(run_id)["task"]["stage_reached"] == "analyzing"


def test_stage_reached_never_goes_backwards() -> None:
    """到达点只增不减：回退写阶段不会把已完成的部分抹掉。"""
    run_id = _demo_run("到达点单调 用例")
    PIPELINE._set_stage(run_id, "searching")
    PIPELINE._set_stage(run_id, "analyzing")
    PIPELINE._set_stage(run_id, "searching")
    assert PIPELINE.task_view(run_id)["task"]["stage_reached"] == "analyzing"


def test_completed_run_shows_every_stage_done() -> None:
    """跑完（含部分失败但各阶段都跑过）的任务：阶段条全部 done，不得出现 skipped。"""
    run_id = _run_demo(DEMO_QUERY)
    task = PIPELINE.task_view(run_id)["task"]
    assert task["stage_reached"] == "completed"
    assert {item["status"] for item in task["stages"]} == {"done"}


def test_result_view_never_fakes_the_empty_reason_on_channel_failure() -> None:
    """通道失败的结果页：不许说「没有找到匹配岗位」，只给 failure_kind=channel。"""
    run_id = _demo_run("结果页空态 用例")
    PIPELINE._set_stage(run_id, "searching")
    PIPELINE._settle_failure(run_id, "OWN_BROWSER_NO_SESSION")

    result = PIPELINE.result_view(run_id)
    assert result["failure_kind"] == "channel"
    assert result["empty_reason"] != user_view.EMPTY_RESULT_MESSAGE
    assert "没有找到匹配岗位" not in (result["empty_reason"] or "")


# ---------------------------------------------------------------------------
# 预算边界：搜索预算耗尽 ≠ 详情不能取（两个预算彼此独立）
# ---------------------------------------------------------------------------

def _budget_run(query: str, **limits: int) -> str:
    """造一个 demo 源任务（不派发 worker），可自定义预算。"""
    config: dict = {"queries": [query], "source": "demo"}
    config.update(limits)
    return runs_mod.create_run("预算边界", config)["id"]


def test_search_budget_exhaustion_does_not_block_details() -> None:
    """真机故障回归：搜索预算在循环中途打满，详情阶段仍要取到详情。

    修之前：搜索打满 → state=budget_stopped → fetch_details 的状态门禁把
    所有详情调用拒掉 → 15 个已搜到的岗位永远取不到详情，任务还不算失败。
    """
    run_id = _budget_run(DEMO_QUERY, max_search_calls=1)
    PIPELINE._collect_phase(run_id)

    run = runs_mod.get_run(run_id)
    assert int(run["counters"]["jobs_unique"]) > 0
    assert int(run["counters"]["detail_calls"]) > 0, "详情一次都没调用：被搜索预算误伤了"
    assert int(run["counters"]["snapshots_complete"]) + \
        int(run["counters"]["snapshots_reused"]) > 0
    assert run["state"] != "budget_stopped"
    assert runs_mod.pending_detail_count(run_id) == 0
    checkpoint = jloads(runs_mod.get_run(run_id)["checkpoint"], {})
    assert checkpoint.get("search_budget_exhausted") is True   # 哪个预算打满要留痕


def test_search_budget_exhaustion_sets_budget_stopped_only_when_no_detail_left() -> None:
    """没有待读详情时，搜索打满才是真的「无事可做」→ 保留 budget_stopped。"""
    run_id = _budget_run(DEMO_QUERY, max_search_calls=1)
    runs_mod.do_search(run_id, "q1", 1)                     # 用掉唯一一次搜索预算
    store.execute("UPDATE run_job SET detail_outcome='complete' WHERE run_id=?", (run_id,))

    with pytest.raises(runs_mod.RunError) as raised:
        runs_mod.do_search(run_id, "q1", 2)
    assert raised.value.code == "BUDGET_EXHAUSTED"
    assert runs_mod.get_run(run_id)["state"] == "budget_stopped"


def test_resume_collection_revives_a_budget_stopped_run_with_pending_details() -> None:
    """已 budget_stopped 但仍有 pending 详情的任务必须能救回来。"""
    run_id = _budget_run(DEMO_QUERY)
    runs_mod.do_search(run_id, "q1", 1)
    assert runs_mod.pending_detail_count(run_id) > 0

    store.execute("UPDATE research_run SET state='budget_stopped' WHERE id=?", (run_id,))
    assert runs_mod.resume_collection(run_id)["state"] == "collecting"

    # 反之：没有待读详情时不允许恢复（点了继续也不会新增采集，不许给假象）
    store.execute("UPDATE research_run SET state='budget_stopped' WHERE id=?", (run_id,))
    store.execute("UPDATE run_job SET detail_outcome='complete' WHERE run_id=?", (run_id,))
    with pytest.raises(runs_mod.RunError) as raised:
        runs_mod.resume_collection(run_id)
    assert raised.value.code == "BUDGET_EXHAUSTED"


@pytest.mark.parametrize('state,code',[('auth_required','AUTH_REQUIRED'),
    ('failed','UPSTREAM_NETWORK'),('failed','UPSTREAM_CONTRACT_CHANGED')])
def test_auth_resume_retries_blocked_job_without_refunding_budget(state,code) -> None:
    run_id = _budget_run(DEMO_QUERY)
    runs_mod.do_search(run_id,'q1',1)
    job = query_one('SELECT job_id FROM run_job WHERE run_id=? LIMIT 1',(run_id,))
    store.execute("UPDATE run_job SET detail_outcome='failed',last_error=? WHERE run_id=? AND job_id=?",(code+': offline',run_id,job['job_id']))
    store.execute("UPDATE research_run SET state=?,stop_reason=? WHERE id=?",(state,code,run_id))
    before = runs_mod.get_run(run_id)['counters']
    resumed = runs_mod.resume_collection(run_id)
    assert resumed['state']=='collecting'
    assert all(resumed['counters'][key]==value for key,value in before.items())
    row = query_one('SELECT detail_outcome,last_error FROM run_job WHERE run_id=? AND job_id=?',(run_id,job['job_id']))
    assert row['detail_outcome']=='pending' and row['last_error'] is None


def test_collect_phase_rescues_a_budget_stopped_run() -> None:
    """端到端：恢复一个 budget_stopped 的老任务，详情阶段真的跑起来。"""
    run_id = _budget_run(DEMO_QUERY)
    runs_mod.do_search(run_id, "q1", 1)
    pending_before = runs_mod.pending_detail_count(run_id)
    assert pending_before > 0
    store.execute("UPDATE research_run SET state='budget_stopped' WHERE id=?", (run_id,))

    PIPELINE._collect_phase(run_id)
    run = runs_mod.get_run(run_id)
    assert int(run["counters"]["detail_calls"]) > 0
    assert runs_mod.pending_detail_count(run_id) == 0


def test_detail_budget_exhaustion_keeps_the_details_already_read() -> None:
    """详情预算打满：保留已取到的部分继续整理，不把整轮判死。"""
    run_id = _budget_run(DEMO_QUERY, max_detail_calls=2)
    # 前面的用例可能已给这些岗位建过完整快照（会被 reused，不消耗详情预算）：
    # 统一置为 missing，保证本轮真的会去取详情，才会撞上详情预算上限。
    store.execute("UPDATE job_snapshot SET detail_status='missing'")
    PIPELINE._collect_phase(run_id)

    run = runs_mod.get_run(run_id)
    assert int(run["counters"]["detail_calls"]) <= 2          # 预算没有被偷偷放宽
    assert run["stage"] == "deduping"                          # 走到了去重，没有中途判死
    checkpoint = jloads(run["checkpoint"], {})
    assert checkpoint.get("detail_budget_exhausted") is True
    assert run["state"] == "budget_stopped"                    # 如实：详情确实不能再取了


def test_budget_stopped_copy_offers_resume_only_when_details_remain() -> None:
    """「任务已暂停，可继续」在没法继续时是误导：预算耗尽要说得清还能不能继续。"""
    resumable = user_view.user_view("paused", "budget_stopped", None,
                                    {"collected": 15, "detail_read": 0})
    assert resumable["user_message"] == user_view.BUDGET_STOPPED_DETAIL_PENDING_MESSAGE
    assert any(action["id"] == "resume" for action in resumable["actions"])

    exhausted = user_view.user_view("paused", "budget_stopped", None,
                                    {"collected": 15, "detail_read": 15})
    assert exhausted["user_message"] == user_view.BUDGET_STOPPED_MESSAGE
    assert all(action["id"] != "resume" for action in exhausted["actions"])


# ---------------------------------------------------------------------------
# 定位符过期（30 分钟 TTL）：显式恢复才给一次有界刷新额度
# ---------------------------------------------------------------------------

_UPSTREAM_DIR = Path(tempfile.gettempdir()) / "finintel-locator-test"


def _fake_zhipin_adapter(jobs: list[dict]) -> object:
    """离线假上游：一次 search 返回与任务岗位一一对应的新卡片（带新定位符）。"""

    class _Adapter:
        async def search(self, cli_path, query, city, page, data_dir, timeout_seconds,
                         no_cache=True, top_level_options=None):
            return {
                "cards": [
                    {"source_job_id": job["source_job_id"],
                     "security_id": f"sec-{index}-{job['source_job_id']}",
                     "lid": f"lid-{index}",
                     "title": job["title"],
                     "company_name": job["company_name_raw"]}
                    for index, job in enumerate(jobs)
                ],
                "trace": {},
            }

        async def detail(self, cli_path, security_id, job_id, lid, data_dir,
                         timeout_seconds, top_level_options=None):
            return {
                "responsibilities": "负责消费信贷产品的需求分析与上线推动",
                "requirements": "三年以上消费金融相关经验",
                "title": "产品经理",
                "company_name": "示例公司",
                "city": "北京",
                "source_status": "unknown",
                "detail_status": "complete",
                "trace": {},
            }

    return _Adapter()


def _failing_search_adapter(code: str = "UPSTREAM_NETWORK") -> object:
    """离线假上游：search/detail 直接抛上游错（模拟网络抖动，零真实请求）。"""
    from upstream_envelope import FailureLayer

    class _Adapter:
        async def search(self, *args, **kwargs):
            raise UpstreamError(code, "连接 upstream 失败", True, "稍后重试一次",
                                layer=FailureLayer.TRANSPORT)

        async def detail(self, *args, **kwargs):
            raise UpstreamError(code, "连接 upstream 失败", True, "稍后重试一次",
                                layer=FailureLayer.TRANSPORT)

    return _Adapter()


def _expired_locator_run(monkeypatch, *, max_search_calls: int = 1) -> tuple[str, list[dict]]:
    """造一个「已搜过、定位符全过期」的 zhipin 风格任务（离线，零真实请求）。

    行由 demo 源的真实代码路径建出来（job / run_job / job_locator 都是真的），
    然后只把「源」伪装成 zhipin、把定位符过期时间改到过去 —— 后面测的
    ``_revalidate_locator`` 是一段纯本地逻辑，正好只依赖这些行。
    """
    run_id = runs_mod.create_run(
        "定位符过期", {"queries": [DEMO_QUERY], "source": "demo",
                       "max_search_calls": max_search_calls})["id"]
    runs_mod.do_search(run_id, "q1", 1)          # 离线建岗位与定位符
    run = runs_mod.get_run(run_id)
    snapshot = dict(run["config_snapshot"])
    snapshot["source"] = "zhipin"
    store.execute("UPDATE research_run SET config_snapshot=? WHERE id=?",
                  (jdumps(snapshot), run_id))
    # 定位符全部过期：模拟 30 分钟 TTL 早已过去（真机是 6 小时+）
    store.execute("UPDATE job_locator SET expires_at='2020-01-01T00:00:00Z'")

    jobs = query_all(
        """SELECT j.* FROM run_job rj JOIN job j ON j.id=rj.job_id
           WHERE rj.run_id=? ORDER BY rj.rowid""",
        (run_id,),
    )
    assert jobs, "demo 搜索没有建出岗位，用例前提不成立"
    monkeypatch.setattr(runs_mod, "_upstream",
                        lambda channel=None: ("boss.exe", _UPSTREAM_DIR))
    monkeypatch.setattr(runs_mod, "_source_adapter",
                        lambda source: _fake_zhipin_adapter(jobs))
    return run_id, jobs


def test_expired_locator_refresh_needs_an_explicit_resume_grant(monkeypatch) -> None:
    """没恢复之前不允许偷用额度：如实抛「需重新搜索」，不静默花预算。"""
    run_id, jobs = _expired_locator_run(monkeypatch, max_search_calls=1)
    run = runs_mod.get_run(run_id)
    assert run["counters"]["search_calls"] == 1        # 搜索预算（1/1）已打满
    assert runs_mod.locator_refresh_granted(run_id) is False

    with pytest.raises(UpstreamError) as raised:
        runs_mod._revalidate_locator(run, jobs[0])
    assert raised.value.code == "STALE_LOCATOR"
    assert runs_mod.locator_refresh_count(run_id) == 0


def test_resume_grants_one_bounded_refresh_that_really_unblocks_details(monkeypatch) -> None:
    """① 过期定位符 + 显式恢复 → 一次搜索刷新**整批**，详情真的能取到。"""
    run_id, jobs = _expired_locator_run(monkeypatch, max_search_calls=1)
    store.execute("UPDATE research_run SET state='failed', stop_reason='STALE_LOCATOR' "
                  "WHERE id=?", (run_id,))

    runs_mod.resume_collection(run_id)                 # 显式恢复 = 发放一次性额度
    assert runs_mod.locator_refresh_granted(run_id) is True

    run = runs_mod.get_run(run_id)
    refreshed = runs_mod._revalidate_locator(run, jobs[0])     # 花掉额度
    assert refreshed["security_id"] and refreshed["expires_at"] > store.utcnow_iso()
    assert runs_mod.locator_refresh_count(run_id) == 1
    assert runs_mod.locator_refresh_granted(run_id) is False   # 一次性：用完即销

    # 一次刷新覆盖整批：其余岗位直接命中「新鲜」，不再重复花额度
    for job in jobs[1:]:
        locator = query_one("SELECT * FROM job_locator WHERE job_id=?", (job["id"],))
        assert runs_mod._locator_is_fresh(locator), job["id"]
    runs_mod._revalidate_locator(run, jobs[1])
    assert runs_mod.locator_refresh_count(run_id) == 1
    # 采集预算没被污染：max_search_calls 仍然被严格遵守
    assert runs_mod.get_run(run_id)["counters"]["search_calls"] == 1

    # 审计：这次搜索在 upstream_call 里可区分，且不占 search_calls
    refs = [row["ref"] for row in query_all(
        "SELECT ref FROM upstream_call WHERE run_id=? AND command='search'", (run_id,))]
    assert "q1:locator-refresh" in refs
    diagnostics = runs_mod.run_status(run_id)["locator_refresh"]
    assert diagnostics == {"used": 1, "limit": runs_mod.MAX_LOCATOR_REFRESH_CALLS, "left": False}


def test_normal_collection_path_still_strictly_respects_search_budget(monkeypatch) -> None:
    """③ 正常路径（未恢复）用 search_calls，额度计数保持 0。"""
    run_id, jobs = _expired_locator_run(monkeypatch, max_search_calls=3)
    assert runs_mod.get_run(run_id)["counters"]["search_calls"] == 1

    runs_mod._revalidate_locator(runs_mod.get_run(run_id), jobs[0])
    counters = runs_mod.get_run(run_id)["counters"]
    assert counters["search_calls"] == 2                 # 花的是采集预算
    assert runs_mod.locator_refresh_count(run_id) == 0    # 没动刷新额度
    assert runs_mod.locator_refresh_granted(run_id) is False


def test_exhausted_refresh_quota_is_honest_and_offers_no_resume(monkeypatch) -> None:
    """② 额度用尽 → 诚实说「需重新搜索」，且不给 can.resume / 不给「继续」按钮。"""
    run_id, jobs = _expired_locator_run(monkeypatch, max_search_calls=1)
    store.execute("UPDATE research_run SET state='failed', stop_reason='STALE_LOCATOR' "
                  "WHERE id=?", (run_id,))
    runs_mod.resume_collection(run_id)
    runs_mod._revalidate_locator(runs_mod.get_run(run_id), jobs[0])
    assert runs_mod.locator_refresh_count(run_id) == runs_mod.MAX_LOCATOR_REFRESH_CALLS

    # 同一批岗位再次过期：额度已经用尽
    store.execute("UPDATE job_locator SET expires_at='2020-01-01T00:00:00Z'")
    store.execute("UPDATE research_run SET state='failed', stage='paused', "
                  "stop_reason='STALE_LOCATOR' WHERE id=?", (run_id,))

    with pytest.raises(runs_mod.RunError) as raised:
        runs_mod.resume_collection(run_id)
    assert raised.value.code == "STALE_LOCATOR"
    assert "重新搜索" in raised.value.operator_action

    with pytest.raises(UpstreamError) as raised2:
        runs_mod._revalidate_locator(runs_mod.get_run(run_id), jobs[0])
    assert raised2.value.code == "STALE_LOCATOR"

    task = PIPELINE.task_view(run_id)["task"]
    assert task["failure_kind"] == "stale"
    assert task["can"]["resume"] is False                 # 不许假按钮
    assert task["user_message"] == user_view.STALE_LOCATOR_MESSAGE
    assert "换个关键词" not in task["user_message"]
    assert [action["id"] for action in task["actions"]] == ["search_again", "view_result"]

    result = PIPELINE.result_view(run_id)
    assert result["failure_kind"] == "stale"
    assert result["empty_reason"] != user_view.EMPTY_RESULT_MESSAGE


def test_failed_refresh_search_does_not_permanently_seal_the_task(monkeypatch) -> None:
    """① 一次**失败**的刷新搜索不得罚没额度：再显式恢复仍能拿到一次尝试。

    真机缺陷（run_d7207cd19ee74c4b）：刷新搜索撞上上游网络抖动（UPSTREAM_NETWORK）→
    旧的累计计数器 ``locator_refresh_calls`` 被打满 → 判「额度用尽」→ 15 个岗位永久
    停在「重新搜索」，用户连重试入口都没有。一次上游抖动 = 任务被永久砖化。
    """
    run_id, jobs = _expired_locator_run(monkeypatch, max_search_calls=1)
    store.execute("UPDATE research_run SET state='failed', stage='paused', "
                  "stop_reason='STALE_LOCATOR' WHERE id=?", (run_id,))

    # 第一次显式恢复：额度发了，但刷新搜索**失败**（上游网络抖动）
    runs_mod.resume_collection(run_id)
    assert runs_mod.locator_refresh_granted(run_id) is True
    monkeypatch.setattr(runs_mod, "_source_adapter",
                        lambda source: _failing_search_adapter("UPSTREAM_NETWORK"))
    with pytest.raises(UpstreamError) as raised:
        runs_mod._revalidate_locator(runs_mod.get_run(run_id), jobs[0])
    assert raised.value.code == "UPSTREAM_NETWORK"
    assert runs_mod.locator_refresh_count(run_id) == 1        # 确实尝试过（诊断计数）

    # 这一批详情就此收尾（真实流程里 fetch_details / worker 会把运行落到 failed +
    # stop_reason=UPSTREAM_NETWORK；定位符仍过期、待读岗位还在）
    store.execute("UPDATE research_run SET state='failed', stage='paused', "
                  "stop_reason='UPSTREAM_NETWORK' WHERE id=?", (run_id,))

    # 关键：失败**没有**罚没额度 —— 还有「点继续再试一把」的机会（绝不永久 False），
    # 且用户界面真的给出「继续」（这才是真机「永久停在重新搜索」被修好的证据）
    assert runs_mod.locator_refresh_left(run_id) is True
    assert runs_mod.locator_refresh_granted(run_id) is False  # 本次恢复那一次已用掉
    task = PIPELINE.task_view(run_id)["task"]
    assert task["can"]["resume"] is True                      # 入口还在（旧口径这里是 False）
    assert "resume" in [action["id"] for action in task["actions"]]
    assert task["user_message"] == user_view.STALE_LOCATOR_RESUMABLE_MESSAGE

    # 用户再点一次「继续」= 新的一次恢复，仍能拿到一次尝试，且这次真的刷新成功
    runs_mod.resume_collection(run_id)                        # 不抛 = 入口真的还在
    assert runs_mod.locator_refresh_granted(run_id) is True
    assert runs_mod.locator_refresh_left(run_id) is True
    monkeypatch.setattr(runs_mod, "_source_adapter",
                        lambda source: _fake_zhipin_adapter(jobs))
    refreshed = runs_mod._revalidate_locator(runs_mod.get_run(run_id), jobs[0])
    assert refreshed["security_id"] and refreshed["expires_at"] > store.utcnow_iso()
    assert runs_mod.locator_refresh_count(run_id) == 2
    # 成功兑现过一次 → 本任务的一次刷新额度才算真正用尽（失败从不计入）
    assert runs_mod.locator_refresh_left(run_id) is False


def test_one_resume_only_gets_a_single_refresh_attempt(monkeypatch) -> None:
    """② 同一次恢复只允许试一次：刷新失败后不再偷偷重试（避免无限循环）。"""
    run_id, jobs = _expired_locator_run(monkeypatch, max_search_calls=1)
    store.execute("UPDATE research_run SET state='failed', stage='paused', "
                  "stop_reason='STALE_LOCATOR' WHERE id=?", (run_id,))
    runs_mod.resume_collection(run_id)                        # 本轮的一次性额度
    monkeypatch.setattr(runs_mod, "_source_adapter",
                        lambda source: _failing_search_adapter("UPSTREAM_NETWORK"))

    with pytest.raises(UpstreamError) as raised:
        runs_mod._revalidate_locator(runs_mod.get_run(run_id), jobs[0])
    assert raised.value.code == "UPSTREAM_NETWORK"
    assert runs_mod.locator_refresh_count(run_id) == 1

    # 同一轮里再来一个岗位：授权已在尝试时消费 → 不再发起第二次刷新搜索，如实抛过期
    with pytest.raises(UpstreamError) as raised2:
        runs_mod._revalidate_locator(runs_mod.get_run(run_id), jobs[1])
    assert raised2.value.code == "STALE_LOCATOR"
    assert runs_mod.locator_refresh_count(run_id) == 1        # 没有第二次尝试


# ---------------------------------------------------------------------------
# 任务页 / 结果页同源：都用「事实判定」_stale_locator_state，绝不各说各话
#
# 真机形状（run_d7207cd19ee74c4b）：有 stale 待读岗位，但**最近一次**失败是
# UPSTREAM_NETWORK（失败的刷新搜索覆盖了 stop_reason）。修之前 task_view 走 stale 视图
# （给「继续」），result_view 却按 failure_kind=channel 分流（说「连接失败」）——同一任务
# 两页两说。现在两页共用同一个事实判定 + 同一套优先级。
# ---------------------------------------------------------------------------

def test_stale_task_and_result_views_tell_the_same_story(monkeypatch) -> None:
    """同一条任务，任务页与结果页必须归到同一个 kind、给出同一族的文案与按钮。"""
    run_id, _jobs = _expired_locator_run(monkeypatch, max_search_calls=1)
    store.execute("UPDATE research_run SET state='failed', stage='paused', "
                  "stop_reason='UPSTREAM_NETWORK' WHERE id=?", (run_id,))
    assert runs_mod.stale_locator_pending_count(run_id) > 0      # 前提：stale 事实确实成立

    task = PIPELINE.task_view(run_id)["task"]
    result = PIPELINE.result_view(run_id)

    # 不因「最近一次失败的码」是 channel/UPSTREAM_NETWORK 就跑偏
    assert task["failure_kind"] == "stale"
    assert result["failure_kind"] == task["failure_kind"]
    # 两边「能不能继续」同源（都来自 locator_refresh_left）
    assert task["can"]["resume"] is True
    assert result["can_resume"] is True
    assert "resume" in [action["id"] for action in task["actions"]]
    # 文案同族：可继续版 stale 文案，绝不退回 channel 文案
    assert task["user_message"] == user_view.STALE_LOCATOR_RESUMABLE_MESSAGE
    assert task["user_message"] != user_view.CHANNEL_NOT_READY_MESSAGE


def test_account_and_risk_outrank_stale_in_both_views(monkeypatch) -> None:
    """优先级：账号/风控压过「定位符过期」——没登录、风控中，刷新定位也读不了详情。

    stale 事实同时成立也不许把两页带偏；两页都必须给「重新登录并继续」，而不是 stale。
    """
    run_id, _jobs = _expired_locator_run(monkeypatch, max_search_calls=1)
    store.execute("UPDATE research_run SET state='auth_required', stage='auth_expired', "
                  "stop_reason='AUTH_EXPIRED' WHERE id=?", (run_id,))
    assert runs_mod.stale_locator_pending_count(run_id) > 0      # stale 事实同时成立

    task = PIPELINE.task_view(run_id)["task"]
    result = PIPELINE.result_view(run_id)
    assert task["failure_kind"] == "auth"
    assert result["failure_kind"] == "auth"                      # 两页同源
    assert "login_and_resume" in [action["id"] for action in task["actions"]]
    assert task["user_message"] != user_view.STALE_LOCATOR_RESUMABLE_MESSAGE

    # 风控同理（state=risk_stopped，stage=paused 属可恢复阶段）
    store.execute("UPDATE research_run SET state='risk_stopped', stage='paused', "
                  "stop_reason='ACCOUNT_RISK' WHERE id=?", (run_id,))
    assert PIPELINE.task_view(run_id)["task"]["failure_kind"] == "risk"
    assert PIPELINE.result_view(run_id)["failure_kind"] == "risk"


def test_stale_fact_alone_still_classifies_as_stale(monkeypatch) -> None:
    """老任务兜底不变：stop_reason 是上游口径（UPSTREAM_RESULT_LIMIT）但确实还有过期待读
    岗位时，两页都应归到 stale（事实优先），不能因为码不匹配就丢掉失败信号。"""
    run_id, _jobs = _expired_locator_run(monkeypatch, max_search_calls=1)
    store.execute("UPDATE research_run SET state='failed', stage='paused', "
                  "stop_reason='UPSTREAM_RESULT_LIMIT' WHERE id=?", (run_id,))
    assert PIPELINE.task_view(run_id)["task"]["failure_kind"] == "stale"
    assert PIPELINE.result_view(run_id)["failure_kind"] == "stale"


def test_failure_kind_maps_stale_locator_codes() -> None:
    """定位符过期走专用码；上游「搜索范围过大」不得被误判成 stale。"""
    assert user_view.failure_kind("paused", "failed", "STALE_LOCATOR", {}) == "stale"
    assert "stale" in user_view.FAILURE_KINDS
    # 上游 RESULT_LIMIT_REACHED 是「范围过大」，补救方式是缩小范围，不是重新搜索
    assert user_view.failure_kind("paused", "failed", "UPSTREAM_RESULT_LIMIT", {}) != "stale"
    assert "UPSTREAM_RESULT_LIMIT" not in user_view.FAILURE_STALE_CODES


def test_locator_without_expiry_is_still_treated_as_fresh() -> None:
    """④ 既有语义不许破坏：``expires_at`` 为空算新鲜。"""
    assert runs_mod._locator_is_fresh({"security_id": "x", "expires_at": None}) is True
    assert runs_mod._locator_is_fresh({"security_id": "x"}) is True
    assert runs_mod._locator_is_fresh(
        {"security_id": "x", "expires_at": "2000-01-01T00:00:00Z"}) is False
    assert runs_mod._locator_is_fresh({"security_id": "", "expires_at": None}) is False
    assert runs_mod._locator_is_fresh(None) is False


def test_fetch_details_recovers_from_expired_locators_after_resume(monkeypatch) -> None:
    """端到端（离线）：恢复后详情真的取到了，而且整批只花了一次刷新额度。

    修之前：定位符过期 + 搜索预算打满 → ``STALE_LOCATOR`` →
    ``detail_calls`` 永远是 0、岗位上一条 detail 记录都没有。
    """
    run_id, _jobs = _expired_locator_run(monkeypatch, max_search_calls=1)
    store.execute("UPDATE research_run SET state='failed', stage='paused', "
                  "stop_reason='STALE_LOCATOR' WHERE id=?", (run_id,))
    runs_mod.resume_collection(run_id)

    refs = [row["job_ref"] for row in query_all(
        "SELECT job_ref FROM run_job WHERE run_id=? ORDER BY rowid", (run_id,))]
    result = runs_mod.fetch_details(run_id, refs[:3])

    assert runs_mod.get_run(run_id)["counters"]["detail_calls"] == 3
    assert [outcome["outcome"] for outcome in result["outcomes"]] == ["complete"] * 3
    assert all("消费信贷产品的需求分析" in (row["responsibilities_text"] or "")
               for row in query_all(
                   "SELECT responsibilities_text FROM job_snapshot s "
                   "JOIN run_job rj ON rj.snapshot_id=s.id WHERE rj.run_id=?", (run_id,)))
    # 一次刷新覆盖整批：3 个岗位只花 1 次刷新额度、0 次额外采集预算
    assert runs_mod.locator_refresh_count(run_id) == 1
    assert runs_mod.get_run(run_id)["counters"]["search_calls"] == 1
    commands = [row["command"] for row in query_all(
        "SELECT command FROM upstream_call WHERE run_id=?", (run_id,))]
    assert commands.count("detail") == 3


# ---------------------------------------------------------------------------
# 恢复时的通道 re-arm：同一次恢复只一次，失败结论不得被「探测许可」覆盖
#
# 真机故障（run_d7207cd19ee74c4b）：同一次恢复里 re-arm 被触发两次，第二次把本轮刚写下
# 的 not_ready / check_source=collect_failed 擦成 unknown；collect_gate() 对 auto+unknown
# 的规则是「放行一次探测」，于是刚失败过的通道在顶栏又变绿。修法是 run 级幂等标记：
# 一次恢复最多 re-arm 一次，且该标记在本次恢复收尾时释放（用户下次显式「继续」= 新恢复）。
# ---------------------------------------------------------------------------

def _current_fingerprint_row_id() -> str:
    return channel_state.current_fingerprint().digest()


@pytest.fixture
def _clean_current_channel_state():
    """只清理「当前指纹」那条通道事实，避免污染/被污染（跨测试共用一个临时库）。"""
    def _wipe() -> None:
        store.execute("DELETE FROM channel_state WHERE fingerprint=?",
                      (_current_fingerprint_row_id(),))

    _wipe()
    yield
    _wipe()


def _paused_run_with_a_previous_channel_failure() -> tuple[str, dict]:
    """可恢复任务 + 一条「上一轮」留下的通道失败事实（离线，零真实请求）。"""
    run_id = runs_mod.create_run(
        "通道 re-arm 用例",
        {"queries": [DEMO_QUERY], "source": "demo", "max_search_calls": 1})["id"]
    runs_mod.do_search(run_id, "q1", 1)          # demo 源：离线建岗位与定位符
    store.execute("UPDATE research_run SET stage='paused', state='failed', "
                  "stop_reason='UPSTREAM_NETWORK', cancel_requested=0 WHERE id=?", (run_id,))
    # 上一轮的失败事实：not_ready / collect_failed（checked_at 是「上一轮」的时间）
    channel_state.STORE.mark_collect_failed(
        channel_state.current_fingerprint(), "UPSTREAM_NETWORK", "网络不可达")
    return run_id, channel_state.channel_view()


def _record_real_collect_failure(run_id: str) -> None:
    """按生产口径回写「本轮真实采集失败」：用任务冻结的通道快照，而不是 UI 视图。"""
    runs_mod._record_channel_result(
        runs_mod._channel_for(runs_mod.get_run(run_id)),
        ok=False, code="UPSTREAM_NETWORK", summary="网络不可达")


def test_same_resume_rearms_the_channel_at_most_once(_clean_current_channel_state) -> None:
    """① 一次恢复里搜索失败 → 详情阶段不得把 not_ready 降回 unknown。"""
    run_id, previous = _paused_run_with_a_previous_channel_failure()
    assert previous["state"] == channel_state.STATE_NOT_READY
    assert previous["check_source"] == channel_state.CHECK_COLLECT_FAILED

    # 用户显式「继续」：允许把**上一轮**的 not_ready re-arm 成 unknown（既有设计不变）
    assert PIPELINE._rearm_channel_for_resume(run_id) is True
    assert channel_state.channel_view()["state"] == channel_state.STATE_UNKNOWN

    # 本轮搜索如实失败 → not_ready / collect_failed（这一步本身就是对的）
    _record_real_collect_failure(run_id)
    failed = channel_state.channel_view()
    assert failed["state"] == channel_state.STATE_NOT_READY
    assert failed["check_source"] == channel_state.CHECK_COLLECT_FAILED

    # 详情阶段之前的「第二次 re-arm」：同一次恢复内必须被拒，绝不擦掉本轮失败
    assert PIPELINE._rearm_channel_for_resume(run_id) is False
    after = channel_state.channel_view()
    assert after["state"] == channel_state.STATE_NOT_READY
    assert after["check_source"] == channel_state.CHECK_COLLECT_FAILED
    assert after["last_error_code"] == "UPSTREAM_NETWORK"


def test_an_explicit_resume_again_may_rearm_once(_clean_current_channel_state) -> None:
    """② 用户显式再次「继续」= 新的一次恢复：允许 not_ready → unknown，且只放行一次。"""
    run_id, _previous = _paused_run_with_a_previous_channel_failure()
    assert PIPELINE._rearm_channel_for_resume(run_id) is True
    # 本轮失败 → not_ready；随后本次恢复收尾（worker 结束会释放一次性标记）
    _record_real_collect_failure(run_id)
    PIPELINE._clear_channel_rearm_mark(run_id)

    # 用户又点了一次「继续」：新恢复，允许再探一次
    assert PIPELINE._rearm_channel_for_resume(run_id) is True
    assert channel_state.channel_view()["state"] == channel_state.STATE_UNKNOWN
    # 同一次恢复内再来一次：拒绝（一次恢复只放行一次探测）
    assert PIPELINE._rearm_channel_for_resume(run_id) is False


def test_a_rearm_while_the_attempt_is_still_running_is_refused(
        _clean_current_channel_state, monkeypatch) -> None:
    """②' 采集中途（worker 还在跑）不得 re-arm：此刻的 not_ready 只可能是本轮刚写的。"""
    run_id, _previous = _paused_run_with_a_previous_channel_failure()
    _record_real_collect_failure(run_id)                    # 本轮失败 → not_ready
    monkeypatch.setattr(PIPELINE, "_channel_attempt_in_flight", lambda _run_id: True)
    assert PIPELINE._rearm_channel_for_resume(run_id) is False
    view = channel_state.channel_view()
    assert view["state"] == channel_state.STATE_NOT_READY
    assert view["check_source"] == channel_state.CHECK_COLLECT_FAILED


def test_collect_ready_is_false_right_after_a_channel_failure(
        _clean_current_channel_state) -> None:
    """③ 刚失败过 → collect_ready 必须为 false（顶栏不许绿）。"""
    _run_id, failed = _paused_run_with_a_previous_channel_failure()
    policy = channel_state.current_policy()
    assert failed["state"] == channel_state.STATE_NOT_READY
    assert channel_state.collect_gate("connected", failed, policy) is False
    # 对照：同一条证据若被错误地 re-arm 成 unknown，auto 通道会被放行一次（绿点）
    unknown = dict(failed, state=channel_state.STATE_UNKNOWN, check_source=None)
    if policy.source == channel_state.SOURCE_AUTO:
        assert channel_state.collect_gate("connected", unknown, policy) is True


def test_a_resume_that_never_starts_does_not_burn_the_probe_allowance(
        _clean_current_channel_state, monkeypatch) -> None:
    """恢复在「账号已验证但通道未就绪」处被拦下 → 不占一次性额度，下次仍可再探。"""
    run_id, _previous = _paused_run_with_a_previous_channel_failure()
    monkeypatch.setattr(PIPELINE, "_login_state", lambda: {"state": "connected"})
    monkeypatch.setattr(PIPELINE, "_login_collectable", lambda: False)

    with pytest.raises(pipeline_mod.PipelineError) as raised:
        PIPELINE.resume_task(run_id)
    assert raised.value.code == pipeline_mod.CHANNEL_NOT_READY_CODE
    # 没有真的开跑：标记已归还，下一次恢复依然能 re-arm
    checkpoint = jloads(runs_mod.get_run(run_id)["checkpoint"], {})
    assert not checkpoint.get(pipeline_mod.CHANNEL_REARM_MARK)
    channel_state.STORE.mark_collect_failed(
        channel_state.current_fingerprint(), "UPSTREAM_NETWORK", "网络不可达")
    assert PIPELINE._rearm_channel_for_resume(run_id) is True


# ---------------------------------------------------------------------------
# empty_reason 说谎字段收口：stale（详情没取到）时绝不出现「未发现/没有找到」
#
# 真机缺陷：details 根本没取到（failure_kind=stale、directions=0），result_view.empty_reason
# 却说「本次未发现足以判断的业务方向」——把「失败」说成「无结果」（用户指令 §4.1）。
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stop_reason", ["STALE_LOCATOR", "UPSTREAM_NETWORK",
                                         "UPSTREAM_RESULT_LIMIT"])
def test_stale_result_empty_reason_is_factual_never_a_no_result_claim(
        monkeypatch, stop_reason) -> None:
    """定位符过期时 ``empty_reason`` 只能是事实描述，绝不下「没有方向」的结论。

    三种 stop_reason 都要过：``STALE_LOCATOR``（本任务自己的码）、
    ``UPSTREAM_NETWORK``（真机里失败的刷新搜索覆盖了 stop_reason）、
    ``UPSTREAM_RESULT_LIMIT``（迁移前老任务的码）。判定走 ``_stale_locator_state``
    这个**事实**口径，而不是「最近一次失败的码」。
    """
    run_id, _jobs = _expired_locator_run(monkeypatch, max_search_calls=1)
    store.execute("UPDATE research_run SET state='failed', stage='paused', "
                  "stop_reason=? WHERE id=?", (stop_reason, run_id))
    assert runs_mod.stale_locator_pending_count(run_id) > 0      # 前提：stale 事实成立

    result = PIPELINE.result_view(run_id)
    assert result["failure_kind"] == "stale"
    assert result["directions"] == []                            # 详情没取到 → 没有方向
    reason = result["empty_reason"] or ""
    # 事实句：说明「详情尚未取得」，而不是「没有找到」
    assert reason == user_view.STALE_DETAILS_PENDING_MESSAGE
    for lie in ("未发现", "没有找到", "没有匹配"):
        assert lie not in reason, reason
    # 也不得出现「换个关键词」这类把失败当无结果的引导
    assert "换个关键词" not in reason


def test_only_a_genuine_empty_run_may_claim_no_directions() -> None:
    """对照：只有「search 真实成功返回空列表」才允许下「没有方向」的结论。

    stale 已由上面的参数化用例钉死；这里补两个反向——通道失败不得下结论，
    真实成功的空列表才可以。
    """
    # (a) 通道失败 → 绝不出现「未发现/没有找到」这类结论性措辞
    failed_id = _demo_run("空态对照 通道失败")
    PIPELINE._set_stage(failed_id, "searching")
    PIPELINE._settle_failure(failed_id, "OWN_BROWSER_NO_SESSION")
    failed = PIPELINE.result_view(failed_id)
    assert failed["failure_kind"] == "channel"
    assert failed["empty_reason"] != user_view.EMPTY_RESULT_MESSAGE
    assert "未发现" not in (failed["empty_reason"] or "")
    assert "没有找到" not in (failed["empty_reason"] or "")

    # (b) 真实成功返回的空列表（search 信封 ok + 零方向 + completed）才是唯一允许下结论的形状
    empty_id = _demo_run("空态对照 真实空结果")
    store.execute("UPDATE research_run SET stage='completed', state='completed' WHERE id=?",
                  (empty_id,))
    runs_mod._record_observation(empty_id, "q1", 1, "success", [])
    empty = PIPELINE.result_view(empty_id)
    assert empty["failure_kind"] == "empty"
    assert empty["empty_reason"] == user_view.EMPTY_RESULT_MESSAGE


# ---------------------------------------------------------------------------
# 顶栏「账号 × 通道」两维口径：account_state / channel_ready 透传到 /api/auth/status 与
# /api/home（前端据此定文案与绿点；执行级契约见 tests/test_frontend_contract.py）
# ---------------------------------------------------------------------------

def _insert_connected_session() -> str:
    """写一条「在线复核通过的已连接」登录会话行（真机绿点的前提）。"""
    now = store.utcnow_iso()
    store.execute("DELETE FROM login_task")
    store.execute(
        """INSERT INTO login_task
           (id,status,stage,data_dir,cli_path,pid,timeout_seconds,message,stderr_tail,
            error_code,user_message,active,started_at,updated_at,finished_at,
            live_verified_at,exit_code)
           VALUES (?,?,?,?,?,NULL,?,NULL,'',NULL,?,0,?,?,?,?,NULL)""",
        ("login_connected_session", "connected", "verifying", str(_TMP_DIR), "", 180,
         user_view.LOGIN_MESSAGES["connected"], now, now, now, now),
    )
    return "login_connected_session"


def test_auth_status_and_home_expose_account_and_channel_dimensions(
        client, _clean_current_channel_state) -> None:
    """账号事实（account_state）与通道事实（channel_ready）都要透传到两个首页接口。

    真机缺陷：``user_stage`` 会被「通道未就绪」打扮成 ``env_not_ready``；前端若只看
    ``user_stage``，就会把「账号已验证、通道尚未就绪」误显示成「搜索服务未就绪」。
    修法是把**未被打扮**的账号事实单独送出来，与通道维度各走各的。
    """
    _insert_connected_session()
    fingerprint = channel_state.current_fingerprint()

    # (1) 账号已验证 + 通道未就绪：两个维度必须能分别读出，文案是「账号侧 + 通道未就绪」
    channel_state.STORE.mark_collect_failed(fingerprint, "UPSTREAM_NETWORK", "网络不可达")
    status_boss = client.get("/api/auth/status").json()["data"]["boss"]
    assert status_boss["state"] == "connected"                 # 账号事实（兼容旧字段）
    assert status_boss["account_state"] == "connected"         # 未被打扮的账号事实（前端要用它）
    assert status_boss["user_stage"] == "env_not_ready"        # 被通道打扮过 → 不可当账号事实
    assert status_boss["channel_ready"] is False               # 通道确凿就绪才为真
    assert status_boss["user_message"] == user_view.ACCOUNT_OK_CHANNEL_NOT_READY
    assert "未登录" not in status_boss["user_message"]

    home_boss = client.get("/api/home").json()["data"]["boss"]
    for key in ("account_state", "channel_ready", "channel", "collect_ready"):
        assert key in home_boss, key
    assert home_boss["account_state"] == status_boss["account_state"]     # 两处同源
    assert home_boss["channel_ready"] == status_boss["channel_ready"]
    assert home_boss["user_stage"] == status_boss["user_stage"]

    # (2) 通道确凿就绪：channel_ready 才为真，文案才是「已连接」
    channel_state.record_collect_ok(fingerprint, 1)
    ready_boss = client.get("/api/auth/status").json()["data"]["boss"]
    assert ready_boss["account_state"] == "connected"
    assert ready_boss["channel_ready"] is True
    assert ready_boss["user_message"] == user_view.LOGIN_MESSAGES["connected"]

    # (3) 通道尚未确认（unknown）：既不宣称已就绪（channel_ready=False），也不说成未登录
    store.execute("DELETE FROM channel_state WHERE fingerprint=?", (fingerprint.digest(),))
    unknown_boss = client.get("/api/auth/status").json()["data"]["boss"]
    assert unknown_boss["account_state"] == "connected"
    assert unknown_boss["channel_ready"] is False
    assert unknown_boss["user_stage"] == "connected"           # 中性：「正在确认搜索通道」
    assert unknown_boss["user_message"] == user_view.ACCOUNT_OK_CHANNEL_CONFIRMING


# ---------------------------------------------------------------------------
# boss 块「同一份实现」：三处接口键集必须完全相同、同一事实源
#
# 真机缺陷：``/api/home`` 的 boss 曾漏 ``channel`` → 前端 ``sfSetBoss()`` 覆盖后
# ``SF_BOSS.channel`` 变 null → 顶栏从「账号已验证、当前搜索连接尚未就绪」退化成
# 「正在确认搜索通道」。三处 boss 原本是三份**手写字面量**，所以必须有一条测试
# 卡住「键集完全相同」，任何一处漏字段立刻红。第三处是 ``/api/tasks/{id}``。
# ---------------------------------------------------------------------------

_BOSS_BLOCK_REQUIRED_KEYS = frozenset({
    "state", "account_state", "user_stage", "user_message", "actions",
    "live_verified_at", "login", "channel", "channel_ready", "collect_ready",
})


def _three_boss_blocks(client, run_id: str) -> dict[str, dict]:
    """同一条事实下，三处接口各自返回的 ``boss`` 块。

    ``/api/home`` 与 ``/api/auth/status`` 把 boss 放在顶层 ``data.boss``；
    ``/api/tasks/{id}`` 由 ``task_view()`` 返回 ``{"task": {...}}``，boss 在
    ``data.task.boss``（前端 ``sfSetBoss(t.boss)`` 正是在轮询时读它）。
    位置不同、内容必须是同一份。
    """
    return {
        "/api/home": client.get("/api/home").json()["data"]["boss"],
        "/api/auth/status": client.get("/api/auth/status").json()["data"]["boss"],
        f"/api/tasks/{run_id}": client.get(f"/api/tasks/{run_id}").json()["data"]["task"]["boss"],
    }


def _assert_boss_blocks_agree(client, run_id: str, label: str) -> dict[str, dict]:
    """断言三处 boss 键集完全相同，且逐字段同源、绿点判据三处都成立。"""
    blocks = _three_boss_blocks(client, run_id)
    keysets = {path: set(block) for path, block in blocks.items()}
    baseline_path = "/api/home"
    baseline = keysets[baseline_path]
    for path, keys in keysets.items():
        # 键集必须一字不差：多一个少一个都不行（缺 channel 就是真机那条 bug）
        assert keys == baseline, (label, path, sorted(baseline ^ keys))
    assert _BOSS_BLOCK_REQUIRED_KEYS <= baseline, \
        (label, sorted(_BOSS_BLOCK_REQUIRED_KEYS - baseline))

    ref = blocks["/api/auth/status"]
    assert ref["account_state"] == "connected", label          # 前提：账号已验证
    for path, block in blocks.items():
        # 绿点判据三处都成立：channel_ready ⇔ 通道确凿就绪（且必须是布尔，不是 None/字典）
        assert block["channel_ready"] == (block["channel"]["state"] == "ready"), (label, path)
        assert isinstance(block["channel_ready"], bool), (label, path)
        assert isinstance(block["collect_ready"], bool), (label, path)
        assert block["channel"] is not None, (label, path)
        # 三处取自同一份 status_view()：同一时刻逐字段一致（含 channel 全字段）
        for key in ("state", "account_state", "collect_ready", "channel_ready",
                    "user_stage", "user_message"):
            assert block[key] == ref[key], (label, path, key)
        assert block["channel"] == ref["channel"], (label, path, "channel")
        assert block["actions"] == ref["actions"], (label, path, "actions")
    return blocks


def test_every_boss_block_is_the_same_shape_and_same_facts(
        client, _clean_current_channel_state) -> None:
    """``/api/home``、``/api/auth/status``、``/api/tasks/{id}`` 的 boss 必须同源同形。

    逐一切换三种通道状态（not_ready / ready / unknown），每种状态下都要求：
    ① 三处键集完全相同；② ``channel_ready == (channel.state == 'ready')``；
    ③ ``account_state`` / ``channel`` / ``collect_ready`` 三处一致（同一份 status_view）。
    """
    _insert_connected_session()
    fingerprint = channel_state.current_fingerprint()
    run_id = _demo_run("boss 块同源 用例")

    # (1) 通道未就绪：这正是真机退化的场景
    channel_state.STORE.mark_collect_failed(fingerprint, "UPSTREAM_NETWORK", "网络不可达")
    not_ready = _assert_boss_blocks_agree(client, run_id, "not_ready")
    assert not_ready["/api/home"]["channel_ready"] is False
    assert not_ready[f"/api/tasks/{run_id}"]["channel"]["state"] == "not_ready"
    # 三处文案都是「账号已验证 + 通道未就绪」，绝不是「正在确认搜索通道」
    for path, block in not_ready.items():
        assert block["user_message"] == user_view.ACCOUNT_OK_CHANNEL_NOT_READY, path
        assert block["user_message"] != user_view.ACCOUNT_OK_CHANNEL_CONFIRMING, path

    # (2) 通道确凿就绪：三处一起变绿（channel_ready=True）
    channel_state.record_collect_ok(fingerprint, 1)
    ready = _assert_boss_blocks_agree(client, run_id, "ready")
    for path, block in ready.items():
        assert block["channel_ready"] is True, path
        assert block["user_message"] == user_view.LOGIN_MESSAGES["connected"], path

    # (3) 通道尚未确认（unknown）：三处一起中性，都不宣称已就绪
    store.execute("DELETE FROM channel_state WHERE fingerprint=?", (fingerprint.digest(),))
    unknown = _assert_boss_blocks_agree(client, run_id, "unknown")
    for path, block in unknown.items():
        assert block["channel_ready"] is False, path
        assert block["user_message"] == user_view.ACCOUNT_OK_CHANNEL_CONFIRMING, path


def test_boss_block_has_exactly_one_implementation() -> None:
    """源码级钉子：boss 块只由 ``login_bridge.boss_block()`` 产出，三处接口只做转发。

    防止有人再手写第二份字面量——真机 bug（漏 ``channel``）就是那么来的。
    """
    backend = Path(__file__).resolve().parent.parent
    app_src = (backend / "app.py").read_text(encoding="utf-8")
    pipeline_src = (backend / "pipeline.py").read_text(encoding="utf-8")

    # 三处调用点全部走同一个函数（task_view / _search_payload / home_view）
    assert pipeline_src.count('"boss": login_bridge.boss_block(') == 3, \
        "pipeline.py 必须恰有 3 处 boss 转发（task_view / _search_payload / home_view）"
    assert "return login_bridge.boss_block()" in app_src, \
        "app._boss_block() 必须转发给 login_bridge.boss_block()"
    # 不许再有手写的 boss 字面量（漏字段的温床）
    for src, name in ((app_src, "app.py"), (pipeline_src, "pipeline.py")):
        assert '"boss": {"state"' not in src, f"{name} 仍有手写的 boss 字面量"
    # 唯一实现只在 login_bridge.py 里
    login_src = (backend / "login_bridge.py").read_text(encoding="utf-8")
    assert "def boss_block(" in login_src
    # 导出的字段清单 = 前端/契约真正需要的那一份，两边不许各自漂移
    assert set(login_bridge.BOSS_BLOCK_FIELDS) == set(_BOSS_BLOCK_REQUIRED_KEYS)


def test_environment_risk_fact_is_consistent_across_topbar_task_and_result(
        client, _clean_current_channel_state) -> None:
    """H3：同一次「访问环境风控」在顶栏 / 任务页 / 结果页必须讲同一件事。

    真机缺陷：迁移 012 只把 ``research_run.stop_reason`` 修正为 ``ENVIRONMENT_RISK``，
    却漏了 ``channel_state.last_error_code``（真机仍是旧映射塌缩后的 ``ACCOUNT_RISK``，
    run_f05ef3d9e1 ↔ fingerprint e184b8dfb334）。于是任务/结果页说「环境被拦截」，
    顶栏却说「连接尚未就绪」——同一件事实两处两说。

    修法：两处派生态都取自 ``upstream_envelope.map_upstream_error`` 归一后的 ``error.code``
    （同源、同判据），顶栏再按 ``channel.last_error_code`` 给环境风控的诚实文案。
    """
    import upstream_envelope

    _insert_connected_session()
    fingerprint = channel_state.current_fingerprint()
    run_id = _demo_run("环境风控三接口一致 用例")

    # 真机原文：环境风控签名与旧映射塌缩后的 ACCOUNT_RISK 同时出现
    raw_message = ("BOSS 直聘访问环境风控 (code 37): 您的环境存在异常.。"
                   "当前浏览器模式: CDP。已停止且未刷新或重试。")
    mapped_code = upstream_envelope.refine_risk_code("ACCOUNT_RISK", raw_message)
    assert mapped_code == "ENVIRONMENT_RISK"          # 同源归一的预备条件

    # 按生产口径写入两处派生态：run.stop_reason 与 channel.last_error_code
    store.execute("UPDATE research_run SET state='risk_stopped', stage='paused', "
                  "stop_reason=? WHERE id=?", (mapped_code, run_id))
    channel_state.STORE.mark_collect_failed(fingerprint, mapped_code, raw_message)

    topbar = client.get("/api/auth/status").json()["data"]["boss"]
    task = client.get(f"/api/tasks/{run_id}").json()["data"]["task"]
    result = client.get(f"/api/tasks/{run_id}/result").json()["data"]

    # 顶栏：必须说出真实原因「环境被拦截」，绝不说成「连接尚未就绪 / 正在确认」
    assert topbar["account_state"] == "connected"          # 账号事实仍如实（未被打扮）
    assert topbar["user_message"] == user_view.ENVIRONMENT_TOPBAR_MESSAGE
    assert user_view.ENVIRONMENT_BLOCKED_COPY in topbar["user_message"]
    assert topbar["user_message"] != user_view.ACCOUNT_OK_CHANNEL_NOT_READY
    assert topbar["user_message"] != user_view.ACCOUNT_OK_CHANNEL_CONFIRMING
    assert "尚未就绪" not in topbar["user_message"]

    # 任务页：同一句核心话 + risk 分类；环境风控**不得**给「重新登录」动作
    assert task["user_message"] == user_view.ENVIRONMENT_PAUSED_MESSAGE
    assert user_view.ENVIRONMENT_BLOCKED_COPY in task["user_message"]
    assert task["failure_kind"] == "risk"
    assert not any(a["id"] in ("login", "login_and_resume") for a in task["actions"])
    assert any(a["id"] == "check_official" for a in task["actions"])

    # 结果页：以 environment_blocked / failure_kind 表达同一件事，
    # 且**绝不**给「没有找到匹配岗位」这类结论性文案
    assert result["environment_blocked"] is True
    assert result["failure_kind"] == "risk"
    assert result["empty_reason"] != user_view.EMPTY_RESULT_MESSAGE

    # 三处共用的唯一核心句（同一件事实，不许各写一份）
    assert user_view.ENVIRONMENT_BLOCKED_COPY in topbar["user_message"]
    assert user_view.ENVIRONMENT_BLOCKED_COPY in task["user_message"]


def test_environment_risk_channel_code_survives_the_same_normalization_as_stop_reason(
        client, _clean_current_channel_state) -> None:
    """H3 反向：若上游只给了 ACCOUNT_RISK（无环境风控签名），两处仍须**各自原样**。

    避免把「归一」写成「一律改判」：没有 ``访问环境风控`` 签名时，账号风控就该是账号风控，
    顶栏与任务页都不得冒充成环境风控。
    """
    _insert_connected_session()
    fingerprint = channel_state.current_fingerprint()
    run_id = _demo_run("账号风控不被误判为环境风控 用例")

    store.execute("UPDATE research_run SET state='risk_stopped', stage='paused', "
                  "stop_reason='ACCOUNT_RISK' WHERE id=?", (run_id,))
    channel_state.STORE.mark_collect_failed(fingerprint, "ACCOUNT_RISK", "账号风控拦截")

    topbar = client.get("/api/auth/status").json()["data"]["boss"]
    task = client.get(f"/api/tasks/{run_id}").json()["data"]["task"]

    # 账号风控：顶栏不得说成环境风控（不得复用 ENVIRONMENT_BLOCKED_COPY）
    assert user_view.ENVIRONMENT_BLOCKED_COPY not in topbar["user_message"]
    assert user_view.ENVIRONMENT_BLOCKED_COPY not in task["user_message"]
    assert topbar["user_message"] == user_view.ACCOUNT_OK_CHANNEL_NOT_READY


# ---------------------------------------------------------------------------
# 详情阶段的认证失败 ≠ 搜索阶段没登录
#
# **口径更正（为什么改了断言）**：详情页已改走 ``boss_browser_worker``，与搜索共用
# **同一份冻结通道**、同一个已登录浏览器会话；旧实现那句「详情走独立的 HTTP 通道、
# 优先使用本地保存的登录凭证」描述的是一个**现在已经不存在的架构**。因此原先两条
# 断言（``"搜索不同的数据通道"`` / ``"重新登录可能无法修复"``）断言的是错误归因，
# 保留它们只会让测试把过时说法焊死；现改为在同一通道口径下断言真实表述：
# 「同一个浏览器会话」+ 重登可重试但「不保证恢复」。
#
# 原用例的**意图**保持不变：详情失败绝不能被笼统说成「你没登录」，也不给必然无用的
# 假按钮（``can.resume`` 仍为 False）。
# ---------------------------------------------------------------------------

def test_detail_stage_auth_failure_does_not_ask_for_a_relogin_loop(monkeypatch) -> None:
    """详情阶段认证失败 → 必须如实说明「同一条会话在读详情时登录态不可用」，而不是地方归因。"""
    run_id, _jobs = _expired_locator_run(monkeypatch, max_search_calls=1)
    store.execute(
        "UPDATE research_run SET state='auth_required', stage='paused', "
        "stop_reason='AUTH_REQUIRED', stage_reached='fetching_details' WHERE id=?",
        (run_id,),
    )

    task = PIPELINE.task_view(run_id)["task"]
    result = PIPELINE.result_view(run_id)
    collected = task["counters"]["collected"]

    # 两页同源：都归到 detail_auth
    assert task["failure_kind"] == "detail_auth"
    assert result["failure_kind"] == "detail_auth"

    # 文案：同一条通道口径（详情已不走独立 HTTP 通道 + 本地凭证），且不给假保证
    assert task["user_message"] == user_view.detail_auth_message(collected)
    assert "同一个浏览器会话" in task["user_message"]
    assert "重新登录" in task["user_message"]
    assert "不保证恢复" in task["user_message"]
    # 「详情是另一套本地 HTTP 凭据」这条旧归因必须彻底消失
    assert "不同的数据通道" not in task["user_message"]
    assert "本地保存的登录凭证" not in task["user_message"]

    # 动作：不再给「重新登录并继续」（旧误导），也不给必然无用的「继续」
    ids = [action["id"] for action in task["actions"]]
    assert "login_and_resume" not in ids
    assert ids == ["login", "view_result"]
    assert task["can"]["resume"] is False


def test_search_stage_auth_failure_still_asks_to_login(monkeypatch) -> None:
    """因果对照：**搜索阶段**（从未到达详情）认证失败 → 维持「重新登录并继续」，不能误伤。

    这条是上面那条的对照锁：证明区分靠的是「失败发生在哪个阶段」这个事实，
    而不是把所有认证失败一律改成新文案。
    """
    run_id, _jobs = _expired_locator_run(monkeypatch, max_search_calls=1)
    store.execute(
        "UPDATE research_run SET state='auth_required', stage='auth_expired', "
        "stop_reason='AUTH_EXPIRED', stage_reached='searching' WHERE id=?",
        (run_id,),
    )

    task = PIPELINE.task_view(run_id)["task"]
    result = PIPELINE.result_view(run_id)

    assert task["failure_kind"] == "auth"
    assert result["failure_kind"] == "auth"
    assert task["user_message"] == user_view.LOGIN_MESSAGES["auth_expired"]
    assert "login_and_resume" in [action["id"] for action in task["actions"]]


def test_detail_auth_ranked_before_the_generic_auth_branch(monkeypatch) -> None:
    """分类顺序锁：``detail_auth`` 必须排在通用 ``auth`` 之前（否则永远取不到新分支）。"""
    assert user_view.failure_kind(
        "paused", "auth_required", "AUTH_REQUIRED", {"collected": 3},
        detail_phase=True,
    ) == "detail_auth"
    assert user_view.failure_kind(
        "paused", "auth_required", "AUTH_REQUIRED", {"collected": 3},
        detail_phase=False,
    ) == "auth"
