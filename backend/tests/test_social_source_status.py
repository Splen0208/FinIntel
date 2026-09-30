# -*- coding: utf-8 -*-
"""T15 来源状态 / 凭据入口 / 导出分派 / 定位失效加固（**全部离线**）。

被测行为（逐条对应下发规格）：

1. ``GET /api/sources`` 接**真实状态**：未检查 = ``unknown``（带 ``checked_at``），
   绝不硬编码成功（新的空库上不得出现 ``ready``）。
2. ``/api/settings`` 新增 ``x_auth_token`` / ``x_ct0``：保存后**采集子进程确实能读到**
   （桥接进 ``os.environ``，X 适配器只读环境变量），且 ``GET`` **绝不回显凭据值**。
3. 导出分派：社交任务的报告**不得**走 BOSS 的 ``generate_report``；未实现的部分
   显式返回 ``UNSUPPORTED_CAPABILITY``，绝不报成功。
4. locator 三条加固：
   - 条目详情响应体**不含** ``xsec_token``（读取边界去掉查询串）；
   - locator TTL 过期 → 条目 ``need_relocate`` + 用户消息含「需重新定位」，
     且**零次平台请求**（不得拿过期定位去撞平台）；
   - 错误摘要统一过 ``redact()``（键值对里的凭据不得落库/回传）。

**全部是离线测试**：数据库 = 临时目录里的全新 SQLite（与 ``.local/data/research.sqlite3``
完全无关）；采集器 / 模型 = ``social_testkit`` 内存替身；凭据 = ``SENTINEL_...`` 哨兵
字符串（形状样本，非真实数据）。断言失败即源码缺陷，禁止放宽断言。
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

import app as app_module  # noqa: E402
import social_pipeline  # noqa: E402
import social_source_status as sss  # noqa: E402
import store  # noqa: E402
import social_testkit as kit  # noqa: E402
from collectors.contracts import CollectorError  # noqa: E402

# 哨兵凭据（形状样本；绝不用真实 token）
SENT_AUTH = "SENTINEL_X_AUTH_4c1d9e77"
SENT_CT0 = "SENTINEL_X_CT0_7b22af10"
SENT_XSEC = "SENTINEL_XSEC_zz42"
SENT_ERR = "SENTINEL_ERR_SUMMARY_0a9"

XHS_TOKEN_URL = ("https://www.xiaohongshu.com/explore/note_t15_1"
                 f"?xsec_token={SENT_XSEC}&xsec_source=pc_search")


def _blob(obj) -> str:
    """把任意响应/视图压成一段文本，用于「不得出现哨兵」的断言。"""
    return json.dumps(obj, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# 0. 装配：端点函数直接调用（同线程 → 同一个 store 连接，完全确定）
# ---------------------------------------------------------------------------

@pytest.fixture()
def clean_source_state(monkeypatch):
    """清空 X 凭据/来源状态，并保证测试后环境变量不残留。"""
    monkeypatch.delenv("TWITTER_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TWITTER_CT0", raising=False)
    with kit.isolated_db():
        store.execute("DELETE FROM social_source_status")
        store.execute("DELETE FROM app_setting WHERE key IN ('x_auth_token','x_ct0')")
        yield


# ---------------------------------------------------------------------------
# 1. 新库 = 未检查 = unknown（无假绿灯）
# ---------------------------------------------------------------------------

def test_routes_are_registered_on_the_app() -> None:
    """接线自检：端点是**挂在 app 上**的，而不是只有同名普通函数。"""
    paths = {getattr(route, "path", None) for route in app_module.app.routes}
    for expected in ("/api/sources", "/api/sources/check", "/api/settings",
                     "/api/social/items/{item_id}",
                     "/api/social/items/{item_id}/comments",
                     "/api/social/findings/{finding_id}/evidence",
                     "/api/social/redaction-audit"):
        assert expected in paths, f"{expected} 未注册：{sorted(p for p in paths if p)}"


def test_new_db_reports_unknown_never_hardcoded_success(clean_source_state) -> None:
    """空库：三个来源都**没有**真实证据 → 一律 unknown / checked_at=None。"""
    body = app_module.sources(refresh=False)
    assert body["ok"] is True, body
    data = body["data"]
    assert data["checked"] is False
    assert all(flag is False for flag in data["credential_flags"].values()), \
        data["credential_flags"]

    by_id = {s["id"]: s for s in data["sources"]}
    assert set(by_id) == set(sss.PLATFORMS), by_id
    # 铁律：没有任何来源可以"默认成功"
    assert all(s["status"] != "ready" for s in data["sources"]), data["sources"]
    assert all(s["checked_at"] is None for s in data["sources"]), data["sources"]
    # X / 小红书必须三层全 unknown（未检查就是 unknown）
    for platform in (sss.PLATFORM_X, sss.PLATFORM_XHS):
        s = by_id[platform]
        assert s["status"] == "unknown", s
        assert s["dependency_state"] == sss.DEP_UNKNOWN, s
        assert s["connection_state"] == sss.CONN_UNKNOWN, s
        assert s["auth_state"] == sss.AUTH_UNKNOWN, s


def test_refresh_is_read_only_probe_and_never_fakes_ready(clean_source_state) -> None:
    """refresh=true：做一次只读体检并**留下 checked_at**；X 无浏览器通道证据 → 不 ready。"""
    before = len(store.query_all("SELECT id FROM social_source_status"))
    body = app_module.sources(refresh=True)
    assert body["ok"] is True, body
    data = body["data"]
    assert data["checked"] is True
    by_id = {s["id"]: s for s in data["sources"]}

    x = by_id[sss.PLATFORM_X]
    assert x["checked_at"] is not None, "体检后必须留下取证时间，否则等于没查"
    # X 的连接层没有浏览器通道证据 → 无论依赖是否就绪都不能报 ready
    assert x["status"] != "ready", x
    assert x["connection_state"] == sss.CONN_UNKNOWN, x

    # 体检确实落库（只读探测也要留痕，供页面"上次检查时间"）
    after = len(store.query_all("SELECT id FROM social_source_status"))
    assert after >= before, (before, after)
    assert store.query_one(
        "SELECT 1 AS hit FROM social_source_status WHERE platform=?",
        (sss.PLATFORM_X,)) is not None, "X 体检结果未落库"

    # 体检绝不能是采集：不得留下任何真实平台调用痕迹
    calls = store.query_all(
        "SELECT id FROM upstream_call WHERE command IN ('search','detail','comments')")
    assert calls == [], f"体检发起了平台请求：{len(calls)} 条"


# ---------------------------------------------------------------------------
# 2. 凭据：设置 → 子进程环境（可读）+ 绝不回显
# ---------------------------------------------------------------------------

def test_credentials_bridge_to_env_and_are_never_echoed(clean_source_state) -> None:
    """保存 X 凭据后：子进程环境能读到；GET /api/settings 不含值、不回显。"""
    import os

    # 只填 auth_token：还缺 ct0 → auth_required，且**不发**依赖探测
    first = app_module.set_setting(
        app_module.SettingReq(key="x_auth_token", value=SENT_AUTH))
    assert first["ok"] is True, first
    payload = first["data"]
    assert payload["credential_flags"]["x_auth_token"] is True, payload
    assert payload["missing_credentials"] == ["x_ct0"], payload
    assert payload["x_auth_state"] == sss.AUTH_REQUIRED, payload
    # 关键：值不得出现在任何响应里
    assert SENT_AUTH not in _blob(first), "响应体泄漏了凭据值"
    # 关键：桥接进进程环境（X 适配器只读 TWITTER_AUTH_TOKEN）
    assert os.environ.get("TWITTER_AUTH_TOKEN") == SENT_AUTH, \
        "设置未桥接到子进程环境，采集子进程读不到凭据"

    # 补齐 ct0 → authenticated
    second = app_module.set_setting(
        app_module.SettingReq(key="x_ct0", value=SENT_CT0))
    assert second["ok"] is True, second
    assert second["data"]["x_auth_state"] == sss.AUTH_AUTHENTICATED, second
    assert second["data"]["missing_credentials"] == [], second
    assert SENT_CT0 not in _blob(second)
    assert os.environ.get("TWITTER_CT0") == SENT_CT0

    # GET /api/settings：值一律不返回，只回"已配置/未配置"
    got = app_module.get_settings()
    assert got["ok"] is True, got
    settings = got["data"]["settings"]
    assert "x_auth_token" not in settings and "x_ct0" not in settings, settings
    assert got["data"]["credential_flags"] == {"x_auth_token": True, "x_ct0": True}, \
        got["data"]["credential_flags"]
    assert SENT_AUTH not in _blob(got) and SENT_CT0 not in _blob(got), \
        "GET /api/settings 泄漏了凭据值"

    # 用户显式清空 → 对应环境变量一并清除（避免旧值继续冒充"已配置"）
    cleared = app_module.set_setting(
        app_module.SettingReq(key="x_auth_token", value=""))
    assert cleared["ok"] is True, cleared
    assert cleared["data"]["credential_flags"]["x_auth_token"] is False, cleared
    assert "TWITTER_AUTH_TOKEN" not in os.environ, \
        "显式清空后环境变量仍残留 → 旧凭据会冒充已配置"
    assert sss.credential_present("x_auth_token") is False


def test_startup_bridge_never_deletes_external_credentials(clean_source_state,
                                                           monkeypatch) -> None:
    """启动桥接必须**保留**外部环境里的凭据（不得误删用户 shell 里配好的值）。"""
    import os

    monkeypatch.setenv("TWITTER_AUTH_TOKEN", SENT_AUTH)
    monkeypatch.setenv("TWITTER_CT0", SENT_CT0)
    # 设置表里没有 → 启动桥接也不得清除外部变量
    injected = sss.load_credentials_from_settings()
    assert injected == {"x_auth_token": True, "x_ct0": True}, injected
    assert os.environ["TWITTER_AUTH_TOKEN"] == SENT_AUTH
    assert os.environ["TWITTER_CT0"] == SENT_CT0


# ---------------------------------------------------------------------------
# 3. 导出分派：社交任务绝不冒充成功
# ---------------------------------------------------------------------------

def test_social_run_report_is_unsupported_capability(clean_source_state) -> None:
    """社交任务的报告导出必须如实返回 UNSUPPORTED_CAPABILITY（不得空报告冒充成功）。"""
    run_id = kit.with_run()  # task_type = xhs_career（社交）
    body = app_module.task_report(run_id)
    assert body["ok"] is False, body
    assert body["error"]["code"] == "UNSUPPORTED_CAPABILITY", body["error"]
    assert body["error"]["operator_action"], "未实现的能力必须给用户下一步指引"


def test_non_social_run_is_not_dispatched_to_social_branch(clean_source_state) -> None:
    """非社交任务不得被误判成社交：不会返回 UNSUPPORTED_CAPABILITY。"""
    run_id = kit.with_run(task_type="job_search", source="zhipin")
    body = app_module.task_report(run_id)
    assert (body.get("error") or {}).get("code") != "UNSUPPORTED_CAPABILITY", body


# ---------------------------------------------------------------------------
# 4. 来源体检入参校验
# ---------------------------------------------------------------------------

def test_sources_check_rejects_unknown_platform(clean_source_state) -> None:
    """体检未知来源 → TOOL_NOT_ALLOWED，且不猜测一个"应该没问题"。"""
    body = app_module.sources_check(app_module.SourcesCheckReq(platforms=["evil"]))
    assert body["ok"] is False, body
    assert body["error"]["code"] == "TOOL_NOT_ALLOWED", body["error"]

    ok_body = app_module.sources_check(app_module.SourcesCheckReq(platforms=[sss.PLATFORM_X]))
    assert ok_body["ok"] is True, ok_body
    assert [s["id"] for s in ok_body["data"]["sources"]] == [sss.PLATFORM_X], ok_body


# ---------------------------------------------------------------------------
# 5. locator 三条加固
# ---------------------------------------------------------------------------

def test_expired_locator_needs_relocate_and_sends_no_platform_request(monkeypatch) -> None:
    """TTL 过期：条目置 need_relocate + 消息含「需重新定位」，且**零次**平台请求。"""
    with kit.isolated_db():
        kit.install_fake_ai(monkeypatch, "valid")
        note = kit.make_note("note_offline_1", "正文一")
        collector = kit.fake_collector(records=[note])
        counter = kit.install_fake_collector(monkeypatch, collector)
        kit.fast_gate(monkeypatch)
        # 让写入的 locator 立刻过期（TTL 为负）——模拟"搜索时的定位串已超时"
        monkeypatch.setattr(social_pipeline, "LOCATOR_TTL_SECONDS", -30, raising=True)

        pipe = social_pipeline.SocialPipeline()
        run_id = kit.with_run()
        asyncio.run(pipe._worker(run_id))

        assert counter.count("detail") == 0, \
            f"定位已过期却仍发了平台请求：{counter.kinds}"
        # 搜索本身是合法的（它负责发现命中）；被禁的是"拿过期定位去读正文/评论"
        assert counter.count("comments") == 0, counter.kinds
        assert set(counter.kinds) <= {"search"}, counter.kinds

        rows = store.query_all(
            "SELECT detail_state, last_error_code FROM social_task_item WHERE run_id=?",
            (run_id,))
        assert rows, "队列为空，用例空跑"
        assert all(r["detail_state"] == social_pipeline.QUEUE_NEED_RELOCATE
                   for r in rows), rows
        assert all(r["last_error_code"] == "LOCATOR_EXPIRED" for r in rows), rows

        message = store.query_one("SELECT user_message FROM research_run WHERE id=?",
                                  (run_id,))["user_message"] or ""
        assert "需重新定位" in message, message
        # 材料不丢：已保存的搜索摘要仍作为材料进入结果集
        items = store.query_all(
            "SELECT DISTINCT item_id FROM social_observation WHERE run_id=?", (run_id,))
        assert len(items) == 1, items
        assert len(store.query_all("SELECT id FROM social_snapshot")) >= 1


def test_item_endpoint_never_exposes_token_url_or_secret_text(clean_source_state) -> None:
    """读取边界：条目响应体不含 xsec_token；库内历史行**不被改写**（D4）。"""
    now = store.utcnow_iso()
    store.execute(
        "INSERT INTO social_item (id, platform, source_id, kind, canonical_url, author_key, "
        "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
        ("sit_t15_1", "xiaohongshu", "note_t15_1", "xhs_note", XHS_TOKEN_URL,
         "plain_author_t15", now, now))
    store.execute(
        "INSERT INTO social_snapshot (id, item_id, content_hash, identity_hash, title, "
        "evidence_text, detail_status, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
        ("ssn_t15_1", "sit_t15_1", "ch", "ih", "标题",
         f"历史正文 ct0={SENT_CT0}，联系方式无关的部分应当保留。", "complete", now))

    body = app_module.social_item_detail("sit_t15_1")
    assert body["ok"] is True, body
    data = body["data"]
    assert data["canonical_url"] == "https://www.xiaohongshu.com/explore/note_t15_1", \
        data["canonical_url"]
    blob = _blob(body)
    assert "xsec_token" not in blob, "读取边界仍带 xsec_token"
    assert SENT_XSEC not in blob, "读取边界泄漏了 xsec_token 值"
    assert SENT_CT0 not in blob, "读取边界泄漏了正文里的凭据"
    assert "无关的部分应当保留" in data["evidence_text"], "脱敏误伤了正常正文"

    # D4：只读边界，禁止批量改写历史数据
    raw = store.query_one("SELECT canonical_url FROM social_item WHERE id='sit_t15_1'")
    assert SENT_XSEC in raw["canonical_url"], "读取边界不得改写历史数据"


def test_error_summaries_pass_through_redact(clean_source_state) -> None:
    """落库的错误摘要必须已经脱敏（来源状态记录层）。"""
    sss.record_status(sss.SourceStatus(
        platform=sss.PLATFORM_X, dependency_state=sss.DEP_READY,
        last_error_code="CONTRACT_CHANGED",
        last_error_summary=f"twitter 返回异常 ct0={SENT_CT0} auth_token={SENT_AUTH}",
        checked_at=store.utcnow_iso(), check_source=sss.CHECK_PROBE))
    got = sss.get_status(sss.PLATFORM_X)
    summary = got.last_error_summary or ""
    assert SENT_CT0 not in summary and SENT_AUTH not in summary, summary
    assert "[REDACTED]" in summary, summary
    # 对外视图同样不得回显
    assert SENT_CT0 not in _blob(got.as_view())
    assert SENT_AUTH not in _blob(got.as_view())


def test_pipeline_error_summary_is_redacted(monkeypatch) -> None:
    """队列行的错误摘要同样必须脱敏（正文读取失败路径）。"""
    with kit.isolated_db():
        kit.install_fake_ai(monkeypatch, "valid")
        note = kit.make_note("note_offline_1", "正文一")
        collector = kit.fake_collector(records=[note]).plan_detail(
            CollectorError("TIMEOUT", f"采集超时 ct0={SENT_CT0}"))
        kit.install_fake_collector(monkeypatch, collector)
        kit.fast_gate(monkeypatch)

        pipe = social_pipeline.SocialPipeline()
        run_id = kit.with_run()
        asyncio.run(pipe._worker(run_id))

        rows = store.query_all(
            "SELECT last_error_summary FROM social_task_item WHERE run_id=?", (run_id,))
        assert rows, "队列为空，用例空跑"
        summaries = [r["last_error_summary"] or "" for r in rows]
        joined = " ".join(summaries)
        assert SENT_CT0 not in joined, f"错误摘要泄漏了凭据：{joined}"
        assert any("[REDACTED]" in s for s in summaries), summaries
