# -*- coding: utf-8 -*-
"""二轮·B：去掉写死的 ``--no-cache``、如实标注上游缓存年龄、显式「获取最新结果」。

真机缺陷（用户指令）：采集走的是上游 CLI，而它的搜索缓存门禁是「未传 ``--no-cache``」。
本项目此前在 ``runs.do_search`` 里把 ``--no-cache`` **写死**为 ``True``，等于每次搜索都强制联网，
既浪费额度、又平白增加被平台风控的概率。

修法：

1. ``source_boss.search`` / ``runs.do_search`` 默认 ``no_cache=False``（允许命中上游缓存）；
2. 只有用户显式「获取最新结果」才传 ``--no-cache``（真正联网重取）；
3. 命中缓存时必须**如实标注**（读上游 ``<data_dir>/cache/boss_agent.db`` 的 ``created_at``
   算出年龄）——绝不沉默地把旧数据当实时结果端上。

全程离线：用假适配器 + 假上游缓存库，零真实请求。
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import test_pipeline as base
import pipeline as pipeline_mod
import rate_gate
import runs as runs_mod
import store
import upstream_cache
import user_view
from store import jdumps

PIPELINE = pipeline_mod.pipeline
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client():
    with TestClient(base.app_module.app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _reset_rate_gate():
    """本文件只关心「缓存/刷新」；频率闸门是另一条护栏，测试之间清零，互不串扰。"""
    rate_gate.reset()
    yield
    rate_gate.reset()


# ---------------------------------------------------------------------------
# 键算法 & 年龄读取（纯本地）
# ---------------------------------------------------------------------------

def test_search_cache_key_matches_upstream_cli_implementation() -> None:
    """本模块复算的缓存键必须与上游 ``CacheStore._make_search_key`` **逐字一致**。

    否则结果页的「是否命中缓存 / 多旧」会整体算错——宁可钉死这条，也不靠猜。
    """
    store_mod = pytest.importorskip("boss_agent_cli.cache.store")
    params = {
        "cache_schema": 2, "query": "风控 产品经理", "city": "上海", "salary": None,
        "experience": None, "education": None, "industry": None, "scale": None,
        "stage": None, "job_type": None, "url": None, "raw_params": {}, "page": 2,
    }
    assert upstream_cache.search_cache_key("风控 产品经理", "上海", 2) == \
        store_mod.CacheStore._make_search_key(params)


def _seed_cache(data_dir: Path, query: str, city: str | None, page: int,
                age_seconds: float) -> None:
    db = upstream_cache.cache_db_path(data_dir)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS search_cache "
        "(cache_key TEXT PRIMARY KEY, response TEXT NOT NULL, created_at REAL NOT NULL)")
    conn.execute(
        "INSERT OR REPLACE INTO search_cache VALUES (?,?,?)",
        (upstream_cache.search_cache_key(query, city, page),
         '{"data": [], "pagination": {"page": 1, "has_more": false, "total": 0}}',
         time.time() - age_seconds))
    conn.commit()
    conn.close()


def test_cache_age_reads_upstream_db_and_honours_ttl(tmp_path) -> None:
    """读不到/无行/过期一律 None（宁可不说，绝不虚报）；命中给出整秒年龄。"""
    data_dir = tmp_path / "upstream"
    assert upstream_cache.cache_age_seconds(data_dir, "q", None, 1) is None      # 库不存在

    _seed_cache(data_dir, "q", None, 1, age_seconds=600)
    age = upstream_cache.cache_age_seconds(data_dir, "q", None, 1)
    assert age is not None and 599 <= age <= 601
    # 条件不同 → 不同键 → 未命中
    assert upstream_cache.cache_age_seconds(data_dir, "q", None, 2) is None
    assert upstream_cache.cache_age_seconds(data_dir, "q", "北京", 1) is None

    # 过期（> TTL）：上游会当作未命中，这里也必须是 None
    _seed_cache(data_dir, "old", "北京", 1,
                age_seconds=upstream_cache.SEARCH_CACHE_TTL_SECONDS + 10)
    assert upstream_cache.cache_age_seconds(data_dir, "old", "北京", 1) is None


def test_cache_served_message_formats() -> None:
    assert user_view.cache_served_message(30) == "结果来自本地缓存（刚刚）"
    assert user_view.cache_served_message(600) == "结果来自本地缓存（10 分钟前）"
    assert user_view.cache_served_message(7200) == "结果来自本地缓存（2 小时前）"
    assert user_view.cache_served_message(600).startswith(user_view.CACHE_SERVED_PREFIX)


# ---------------------------------------------------------------------------
# source_boss.search：默认不传 --no-cache，显式才传
# ---------------------------------------------------------------------------

def test_source_boss_search_only_adds_no_cache_when_asked(monkeypatch, tmp_path) -> None:
    import source_boss

    seen: dict = {}

    async def fake_call(cli_path, command, *, data_dir, positional=(), options=None,
                        top_level_options=None, timeout_seconds=120):
        seen["options"] = dict(options or {})
        invocation = SimpleNamespace(argv_redacted=[], data_dir=str(data_dir),
                                     elapsed_ms=1, returncode=0)
        envelope = SimpleNamespace(data=[], command="search", layer="business", ok=True,
                                   error=None,
                                   pagination={"page": 1, "total": 0, "has_more": False})
        return invocation, envelope

    monkeypatch.setattr(source_boss, "_call", fake_call)
    common = dict(cli_path="boss.exe", query="q", city=None, page=1,
                  data_dir=tmp_path, timeout_seconds=10,
                  top_level_options={"--browser-source": "auto"})

    asyncio.run(source_boss.search(**common))
    assert "--no-cache" not in seen["options"], "默认不得强制跳过上游缓存"

    asyncio.run(source_boss.search(**common, no_cache=True))
    assert seen["options"].get("--no-cache") is True, "显式刷新才允许带 --no-cache"


# ---------------------------------------------------------------------------
# runs.do_search：默认命中缓存并如实记年龄；显式刷新才联网
# ---------------------------------------------------------------------------

def _capturing_adapter(seen: list) -> object:
    class _Adapter:
        async def search(self, cli_path, query, city, page, data_dir, timeout_seconds,
                         no_cache=False, top_level_options=None):
            seen.append(bool(no_cache))
            return {"cards": [], "trace": {}, "page": page, "total": 0,
                    "has_more": False, "empty_result": True}

        async def detail(self, *args, **kwargs):
            return {}

    return _Adapter()


def _zhipin_run(query: str, city: str | None) -> str:
    """demo 路径建真实行，再把「源」伪装成 zhipin（离线，零真实请求）。"""
    run_id = runs_mod.create_run(
        query, {"queries": [query], "source": "demo", "cities": [city] if city else [],
                "max_search_calls": 5})["id"]
    run = runs_mod.get_run(run_id)
    snapshot = dict(run["config_snapshot"])
    snapshot["source"] = "zhipin"
    store.execute("UPDATE research_run SET config_snapshot=? WHERE id=?",
                  (jdumps(snapshot), run_id))
    return run_id


def test_do_search_defaults_to_cache_then_force_refresh_sends_no_cache(monkeypatch, tmp_path) -> None:
    data_dir = tmp_path / "upstream"
    # 本条只验缓存/刷新语义；关掉「两次列表请求最小间隔」以免闸门干扰（闸门见 test_rate_gate.py）
    monkeypatch.setattr(rate_gate, "MIN_LIST_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(runs_mod, "_upstream",
                        lambda channel=None: ("boss.exe", SimpleNamespace(path=data_dir)))
    seen: list = []
    monkeypatch.setattr(runs_mod, "_source_adapter", lambda source: _capturing_adapter(seen))

    query, city = "缓存命中 产品经理", "北京"
    run_id = _zhipin_run(query, city)
    _seed_cache(data_dir, query, city, 1, age_seconds=600)

    first = runs_mod.do_search(run_id, "q1", 1)
    assert seen == [False], "默认必须允许命中缓存（不传 --no-cache）"
    assert first["cache_hit"] is True
    assert first["cache_age_seconds"] is not None
    assert 600 <= first["cache_age_seconds"] < 1200

    second = runs_mod.do_search(run_id, "q1", 1)          # 应用层复用：不再调上游
    assert seen == [False]
    assert second["reused"] is True

    third = runs_mod.do_search(run_id, "q1", 1, force_refresh=True)
    assert seen == [False, True], "显式「获取最新结果」才带 --no-cache"
    assert third["reused"] is False
    assert third["cache_hit"] is False                    # 强制联网 → 不是缓存命中


def test_second_search_without_cache_hit_records_no_age(monkeypatch, tmp_path) -> None:
    """库里没有同条件缓存 → 会真正联网，年龄如实为空（绝不假称命中缓存）。"""
    data_dir = tmp_path / "upstream"
    monkeypatch.setattr(runs_mod, "_upstream",
                        lambda channel=None: ("boss.exe", SimpleNamespace(path=data_dir)))
    monkeypatch.setattr(runs_mod, "_source_adapter", lambda source: _capturing_adapter([]))

    run_id = _zhipin_run("无缓存 产品经理", None)
    result = runs_mod.do_search(run_id, "q1", 1)
    assert result["cache_hit"] is False
    assert result["cache_age_seconds"] is None
    assert runs_mod.latest_search_cache_age(run_id) is None


# ---------------------------------------------------------------------------
# 结果视图：如实标注
# ---------------------------------------------------------------------------

def test_result_view_reports_cache_hit_and_age() -> None:
    run_id = base._demo_run(base.DEMO_QUERY)
    PIPELINE._collect_phase(run_id)
    PIPELINE._set_stage(run_id, "completed")             # 模拟一份已出结果

    store.execute("UPDATE search_observation SET cache_age_seconds=NULL WHERE run_id=?", (run_id,))
    assert PIPELINE.result_view(run_id)["cache"]["hit"] is False

    runs_mod._record_observation(run_id, "q1", 1, "success", [], cache_age_seconds=600)
    data = PIPELINE.result_view(run_id)
    assert data["cache"]["hit"] is True
    assert data["cache"]["age_seconds"] == 600
    assert data["cache"]["text"] == user_view.cache_served_message(600)
    assert data["cache"]["text"].startswith("结果来自本地缓存")
    assert data["can"]["refresh"] is True


# ---------------------------------------------------------------------------
# HTTP：显式刷新的唯一入口
# ---------------------------------------------------------------------------

def test_refresh_endpoint_spawns_collection_with_force_refresh(monkeypatch) -> None:
    run_id = base._demo_run(base.DEMO_QUERY)
    PIPELINE._collect_phase(run_id)                       # 建出岗位（jobs_unique>0）
    PIPELINE._set_stage(run_id, "completed")
    captured: dict = {}
    monkeypatch.setattr(PIPELINE, "_spawn_worker",
                        lambda rid, **kw: captured.update({"run_id": rid, **kw}))

    with TestClient(base.app_module.app, raise_server_exceptions=False) as test_client:
        body = test_client.post(f"/api/tasks/{run_id}/refresh").json()

    assert body["ok"], body
    assert captured.get("run_id") == run_id
    assert captured.get("force_refresh") is True, "刷新必须把 force_refresh=True 交给采集线程"


def test_refresh_rejected_while_collection_is_active() -> None:
    run_id = base._demo_run(base.DEMO_QUERY)
    PIPELINE._collect_phase(run_id)
    PIPELINE._set_stage(run_id, "fetching_details")

    with TestClient(base.app_module.app, raise_server_exceptions=False) as test_client:
        body = test_client.post(f"/api/tasks/{run_id}/refresh").json()

    assert body["ok"] is False
    assert body["error"]["code"] == "TOOL_NOT_ALLOWED"


def test_refresh_with_spent_search_budget_creates_new_run_and_preserves_history(monkeypatch) -> None:
    base._setting('demo_mode','1')
    run_id=base._demo_run(base.DEMO_QUERY)
    PIPELINE._collect_phase(run_id)
    PIPELINE._set_stage(run_id,'completed')
    run=runs_mod.get_run(run_id)
    counters=dict(run['counters'])
    counters['search_calls']=run['limits']['max_search_calls']
    store.execute('UPDATE research_run SET counters=? WHERE id=?',(jdumps(counters),run_id))
    before=runs_mod.get_run(run_id)
    captured={}
    monkeypatch.setattr(PIPELINE,'_spawn_worker',lambda rid,**kwargs:captured.update(run_id=rid,**kwargs))
    body=PIPELINE.refresh_task(run_id)
    new_id=body['task']['run_id']
    assert new_id!=run_id and captured['run_id']==new_id and captured['force_refresh'] is True
    assert runs_mod.get_run(run_id)['counters']==before['counters']
    assert runs_mod.get_run(run_id)['stage']=='completed'
    assert runs_mod.get_run(new_id)['counters']['search_calls']==0
    # This test intentionally suppresses the worker; do not leave an active mock run.
    PIPELINE._set_stage(new_id,'cancelled')


def test_refresh_rejected_without_any_collected_jobs() -> None:
    run_id = base._demo_run("无岗位不可刷新 用例")
    with TestClient(base.app_module.app, raise_server_exceptions=False) as test_client:
        body = test_client.post(f"/api/tasks/{run_id}/refresh").json()
    assert body["ok"] is False
    assert body["error"]["code"] == "TOOL_NOT_ALLOWED"


# ---------------------------------------------------------------------------
# 文案单一来源（源码级钉子）
# ---------------------------------------------------------------------------

def test_frontend_result_page_uses_backend_cache_text() -> None:
    """结果页必须渲染**后端**给的缓存文案，不得自己硬编码一份（同族缺陷的防线）。"""
    src = (PROJECT_ROOT / "frontend" / "simple.js").read_text(encoding="utf-8")
    assert "d.cache.text" in src
    assert "sfRefreshTask" in src
    assert "结果来自本地缓存（" not in src, "缓存文案唯一来源是后端 user_view.cache_served_message"
