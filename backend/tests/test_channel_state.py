# -*- coding: utf-8 -*-
"""采集通道状态单测（T01）：指纹 / 失效 / 判定分支 / 不落凭据。

全部离线：不访问平台、不发起真实搜索；doctor 预检一律用注入的 runner 替代。
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="finintel_channel_"))
os.environ["FININTEL_DB"] = str(_TMP_DIR / "channel_state.sqlite3")

import store  # noqa: E402
from store import query_all, query_one  # noqa: E402

store._local = threading.local()
store.apply_migrations()

import channel_state  # noqa: E402
import user_view  # noqa: E402
from channel_state import (  # noqa: E402
    SOURCE_AUTO,
    SOURCE_EXISTING_BROWSER,
    SOURCE_STORED_COOKIE,
    STATE_NOT_READY,
    STATE_READY,
    STATE_UNKNOWN,
    ChannelFingerprint,
    ChannelPolicy,
    ChannelProbe,
)
from upstream_config import (  # noqa: E402
    CDP_URL_OPTION,
    channel_options_from_snapshot,
)
from upstream_envelope import UpstreamError  # noqa: E402

DATA_DIR = "C:/tmp/finintel-upstream"
CLI_PATH = "C:/tmp/venv/Scripts/boss.exe"
OTHER_CLI = "C:/tmp/other/Scripts/boss.exe"
OTHER_DIR = "C:/tmp/finintel-upstream-2"


def _set(key: str, value: str) -> None:
    store.execute("INSERT INTO app_setting (key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _get(key: str) -> str | None:
    row = query_one("SELECT value FROM app_setting WHERE key=?", (key,))
    return row["value"] if row else None


@pytest.fixture(autouse=True)
def _clean_channel_env():
    """清空通道表并把通道相关设置还原，避免污染同库里的其他测试模块。"""
    saved = {key: _get(key) for key in (
        "boss_browser_source", "boss_cdp_url", "boss_cli_path",
        "upstream_data_dir", "channel_config_version")}
    store.execute("DELETE FROM channel_state")
    _set("boss_browser_source", "")
    _set("boss_cdp_url", "")
    _set("boss_cli_path", CLI_PATH)
    yield
    store.execute("DELETE FROM channel_state")
    for key, value in saved.items():
        if value is None:
            store.execute("DELETE FROM app_setting WHERE key=?", (key,))
        else:
            _set(key, value)


def _fp(source: str = SOURCE_AUTO, cdp_url: str = "", data_dir: str = DATA_DIR,
        cli_path: str = CLI_PATH, version: int = 1) -> ChannelFingerprint:
    return ChannelFingerprint(browser_source=source, cdp_url=cdp_url, data_dir=data_dir,
                              cli_path=cli_path, config_version=version)


# ---------------------------------------------------------------------------
# ① 指纹：任一维变化 = 新通道
# ---------------------------------------------------------------------------

def test_fingerprint_is_stable_and_short() -> None:
    assert _fp().digest() == _fp().digest()
    assert len(_fp().digest()) == 16


def test_fingerprint_empty_source_equals_auto() -> None:
    """``''`` 与 ``auto`` 语义完全等价，不制造多余的未知行。"""
    assert _fp(source="").digest() == _fp(source="auto").digest()


@pytest.mark.parametrize("kwargs", [
    {"source": SOURCE_EXISTING_BROWSER},
    {"cdp_url": "http://127.0.0.1:9222"},
    {"data_dir": OTHER_DIR},
    {"cli_path": OTHER_CLI},
    {"version": 2},
])
def test_any_dimension_change_makes_a_new_fingerprint(kwargs: dict) -> None:
    base = _fp()
    changed = _fp(**kwargs)
    assert base.digest() != changed.digest()


def test_switching_channel_creates_unknown_row_and_old_ready_does_not_carry_over() -> None:
    """换通道后旧通道的 ready 一律不为新通道放行（设计 §2.3 铁律 3）。"""
    auto_fp = _fp()
    channel_state.record_collect_ok(auto_fp, 7)
    assert channel_state.STORE.view(auto_fp)["state"] == STATE_READY

    own_fp = _fp(source=SOURCE_EXISTING_BROWSER)
    assert channel_state.STORE.view(own_fp)["state"] == STATE_UNKNOWN
    # 账号已连接，但新通道没有任何证据 → 不放行
    policy = ChannelPolicy.for_source(SOURCE_EXISTING_BROWSER)
    assert channel_state.collect_gate("connected", channel_state.STORE.view(own_fp), policy) is False


def test_fingerprint_of_settings_follows_browser_source_setting(tmp_path: Path) -> None:
    """指纹跟随当前生效设置；CLI 取配置的实际可执行文件（不存在则不认）。"""
    cli = tmp_path / "Scripts" / "boss.exe"
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text("", encoding="utf-8")
    data_dir = tmp_path / "upstream"
    data_dir.mkdir(parents=True, exist_ok=True)

    _set("boss_browser_source", "existing-browser")
    _set("boss_cdp_url", "http://127.0.0.1:9222")
    _set("boss_cli_path", str(cli))
    _set("upstream_data_dir", str(data_dir))

    fingerprint = channel_state.current_fingerprint()
    assert fingerprint.browser_source == SOURCE_EXISTING_BROWSER
    assert fingerprint.cdp_url == "http://127.0.0.1:9222"
    assert fingerprint.cli_path and os.path.normcase(
        os.path.abspath(fingerprint.cli_path)) == os.path.normcase(os.path.abspath(str(cli)))
    assert channel_state.channel_view()["fingerprint"] == fingerprint.digest()


# ---------------------------------------------------------------------------
# ② 只有三类事件能写 state；真实采集优先级最高
# ---------------------------------------------------------------------------

def test_collect_ok_then_collect_failed_then_ok_again() -> None:
    fingerprint = _fp()
    assert channel_state.STORE.view(fingerprint)["state"] == STATE_UNKNOWN

    channel_state.record_collect_ok(fingerprint, 12)
    view = channel_state.STORE.view(fingerprint)
    assert view["state"] == STATE_READY
    assert view["check_source"] == "collect_ok"
    assert view["last_collect_cards"] == 12
    assert view["last_collect_ok_at"]

    channel_state.record_collect_failed(fingerprint, "OWN_BROWSER_NO_SESSION", "会话不可用")
    view = channel_state.STORE.view(fingerprint)
    assert view["state"] == STATE_NOT_READY
    assert view["check_source"] == "collect_failed"
    assert view["last_error_code"] == "OWN_BROWSER_NO_SESSION"

    channel_state.record_collect_ok(fingerprint, 3)
    view = channel_state.STORE.view(fingerprint)
    assert view["state"] == STATE_READY
    assert view["last_error_code"] is None


def test_precheck_cannot_downgrade_a_channel_proven_by_real_collect() -> None:
    fingerprint = _fp(source=SOURCE_EXISTING_BROWSER)
    channel_state.record_collect_ok(fingerprint, 5)
    policy = ChannelPolicy.for_source(SOURCE_EXISTING_BROWSER)
    channel_state.STORE.mark_precheck(
        fingerprint, {"ok": False, "decisive": True, "checks": {},
                      "error_code": "CHANNEL_PRECHECK_FAILED", "summary": "没发现会话"},
        policy)
    assert channel_state.STORE.view(fingerprint)["state"] == STATE_READY


def test_precheck_decides_only_when_policy_allows() -> None:
    """auto 通道的就绪只能由真实采集证明：预检不下结论，也不写 state。"""
    fingerprint = _fp()
    policy = ChannelPolicy.for_source(SOURCE_AUTO)
    channel_state.STORE.mark_precheck(
        fingerprint, {"ok": False, "decisive": False, "checks": {}}, policy)
    assert channel_state.STORE.view(fingerprint)["state"] == STATE_UNKNOWN

    own = _fp(source=SOURCE_EXISTING_BROWSER)
    own_policy = ChannelPolicy.for_source(SOURCE_EXISTING_BROWSER)
    channel_state.STORE.mark_precheck(
        own, {"ok": True, "decisive": True, "checks": {"cdp": {"status": "ok"}}}, own_policy)
    assert channel_state.STORE.view(own)["state"] == STATE_READY

    broken = _fp(source=SOURCE_EXISTING_BROWSER, cdp_url="http://127.0.0.1:9333")
    channel_state.STORE.mark_precheck(
        broken, {"ok": False, "decisive": True, "checks": {"cdp": {"status": "fail"}},
                 "error_code": "CHANNEL_PRECHECK_FAILED", "summary": "端口没开"},
        own_policy)
    assert channel_state.STORE.view(broken)["state"] == STATE_NOT_READY


def test_check_source_is_always_one_of_three_events() -> None:
    fingerprint = _fp(source=SOURCE_EXISTING_BROWSER)
    policy = ChannelPolicy.for_source(SOURCE_EXISTING_BROWSER)
    channel_state.STORE.mark_precheck(
        fingerprint, {"ok": True, "decisive": True, "checks": {}}, policy)
    channel_state.record_collect_ok(fingerprint, 1)
    channel_state.record_collect_failed(fingerprint, "CDP_UNAVAILABLE", "x")
    row = query_one("SELECT * FROM channel_state WHERE fingerprint=?", (fingerprint.digest(),))
    assert row["check_source"] in {"doctor", "collect_ok", "collect_failed"}
    assert row["state"] in {STATE_UNKNOWN, STATE_READY, STATE_NOT_READY}


def test_rearm_only_moves_not_ready_back_to_unknown() -> None:
    fingerprint = _fp()
    assert channel_state.rearm_current(fingerprint)["state"] == STATE_UNKNOWN
    channel_state.record_collect_failed(fingerprint, "OWN_BROWSER_NO_SESSION", "boom")
    assert channel_state.STORE.view(fingerprint)["state"] == STATE_NOT_READY
    assert channel_state.rearm_current(fingerprint)["state"] == STATE_UNKNOWN
    # 已自证可用的通道不会被「重新探测」机制降级
    channel_state.record_collect_ok(fingerprint, 2)
    assert channel_state.rearm_current(fingerprint)["state"] == STATE_READY


def test_invalidate_all_resets_decisions_but_keeps_history() -> None:
    fingerprint = _fp()
    channel_state.record_collect_ok(fingerprint, 4)
    before = channel_state.STORE.view(fingerprint)
    assert channel_state.invalidate_all() >= 1
    after = channel_state.STORE.view(fingerprint)
    assert after["state"] == STATE_UNKNOWN
    assert after["check_source"] is None
    assert after["last_collect_ok_at"] == before["last_collect_ok_at"]   # 历史事实保留


# ---------------------------------------------------------------------------
# ③ 采集门禁 collect_gate（顶栏与搜索前置检查共用）
# ---------------------------------------------------------------------------

def _view_for(source: str, state: str, cdp_url: str = "", **extra) -> dict:
    """造一个通道视图；cdp_url 是指纹的一维，必须进指纹否则会串到别的通道行。"""
    fingerprint = _fp(source=source, cdp_url=cdp_url)
    if state == STATE_READY:
        channel_state.record_collect_ok(fingerprint, 3)
    elif state == STATE_NOT_READY:
        channel_state.record_collect_failed(fingerprint, "OWN_BROWSER_NO_SESSION", "x")
    view = channel_state.STORE.view(fingerprint)
    view.update(extra)
    return view


@pytest.mark.parametrize("account,state,expected", [
    ("connected", STATE_READY, True),
    ("connected_degraded", STATE_READY, True),      # 凭证不完整也算已连接
    ("connected", STATE_UNKNOWN, True),             # 放行一次有界探测性搜索
    ("connected", STATE_NOT_READY, False),          # 通道已自证不可用：不放行
    ("disconnected", STATE_READY, False),           # 账号没连上：不放行
    ("connecting", STATE_READY, False),
])
def test_collect_gate_auto_channel(account: str, state: str, expected: bool) -> None:
    policy = ChannelPolicy.for_source(SOURCE_AUTO)
    assert channel_state.collect_gate(account, _view_for(SOURCE_AUTO, state), policy) is expected


def test_collect_gate_existing_browser_does_not_require_local_account() -> None:
    """该通道凭据只在浏览器里：本地账号未 connected 也不得误阻断（验收清单第 3 条）。"""
    policy = ChannelPolicy.for_source(SOURCE_EXISTING_BROWSER)
    ready = _view_for(SOURCE_EXISTING_BROWSER, STATE_READY)
    assert channel_state.collect_gate("disconnected", ready, policy) is True
    assert channel_state.collect_gate("connected", ready, policy) is True

    not_ready = _view_for(SOURCE_EXISTING_BROWSER, STATE_NOT_READY)
    assert channel_state.collect_gate("connected", not_ready, policy) is False

    unknown = _view_for(SOURCE_EXISTING_BROWSER, STATE_UNKNOWN)
    assert channel_state.collect_gate("connected", unknown, policy) is False


def test_collect_gate_existing_browser_unknown_with_fresh_collect_passes() -> None:
    """unknown 但上次真实取数成功仍在有效期内 → 该通道已自证可用，放行。"""
    policy = ChannelPolicy.for_source(SOURCE_EXISTING_BROWSER)
    fingerprint = _fp(source=SOURCE_EXISTING_BROWSER)
    channel_state.record_collect_ok(fingerprint, 6)
    channel_state.STORE.rearm(fingerprint)
    channel_state.STORE.invalidate_all()
    view = channel_state.STORE.view(fingerprint)
    assert view["state"] == STATE_UNKNOWN
    assert view["last_collect_ok_at"]
    assert channel_state.collect_gate("disconnected", view, policy) is True


def test_collect_gate_stored_cookie_requires_explicit_cdp_url() -> None:
    policy = ChannelPolicy.for_source(SOURCE_STORED_COOKIE, "http://127.0.0.1:9222")
    # ① 通道行里没有调试地址 → 不放行（stored-cookie 必须显式填写）
    without_url = _view_for(SOURCE_STORED_COOKIE, STATE_READY)
    assert channel_state.collect_gate("connected", without_url, policy) is False

    # ② 显式填了地址且该通道已自证可用 → 放行
    store.execute("DELETE FROM channel_state")
    with_url = _view_for(SOURCE_STORED_COOKIE, STATE_READY, cdp_url="http://127.0.0.1:9222")
    assert channel_state.collect_gate("connected", with_url, policy) is True

    # ③ 有地址但通道还没被真实取数证明 → 不放行（unknown 不是放行）
    store.execute("DELETE FROM channel_state")
    unknown = _view_for(SOURCE_STORED_COOKIE, STATE_UNKNOWN, cdp_url="http://127.0.0.1:9222")
    assert unknown["state"] == STATE_UNKNOWN
    assert channel_state.collect_gate("connected", unknown, policy) is False


def test_collect_gate_never_relaxes_to_make_the_topbar_green() -> None:
    """通道不可用时如实报 False：不允许为了让顶栏变绿而放宽判定。"""
    policy = ChannelPolicy.for_source(SOURCE_AUTO)
    broken = _view_for(SOURCE_AUTO, STATE_NOT_READY)
    for account in ("connected", "connected_degraded"):
        assert channel_state.collect_gate(account, broken, policy) is False


# ---------------------------------------------------------------------------
# ④ 不落凭据（红线）
# ---------------------------------------------------------------------------

def test_no_credentials_are_persisted_in_channel_state() -> None:
    fingerprint = _fp(source=SOURCE_EXISTING_BROWSER)
    dirty = ("cookie=wt2AbCdEf123456; stoken=eyJhbGciOiJIUzI1NiJ9abcdef; "
             "token: 9f8e7d6c5b4a39281706f5e4d3c2b1a0")
    channel_state.record_collect_failed(fingerprint, "OWN_BROWSER_NO_SESSION", dirty)
    row = query_one("SELECT * FROM channel_state WHERE fingerprint=?", (fingerprint.digest(),))

    blob = " ".join(str(value) for value in row.values() if value is not None)
    for needle in ("wt2AbCdEf123456", "eyJhbGciOiJIUzI1NiJ9abcdef",
                   "9f8e7d6c5b4a39281706f5e4d3c2b1a0"):
        assert needle not in blob, needle
    assert "REDACTED" in blob
    # 32 位以上十六进制串（token 形状）不得出现
    assert not re.search(r"\b[0-9a-fA-F]{32,}\b", blob)


def test_channel_state_columns_never_include_credential_names() -> None:
    columns = {row["name"] for row in query_all("PRAGMA table_info(channel_state)")}
    forbidden = {"cookie", "token", "stoken", "wt2", "user_agent", "credential", "secret"}
    assert not (columns & forbidden), columns & forbidden
    assert {"fingerprint", "state", "check_source", "last_collect_ok_at",
            "last_error_code", "last_error_summary"} <= columns


def test_channel_view_has_no_paths_and_no_error_detail() -> None:
    """普通视图只有状态与一行脱敏摘要；路径与详细原因只进诊断页。"""
    fingerprint = _fp(source=SOURCE_EXISTING_BROWSER)
    channel_state.record_collect_failed(fingerprint, "CDP_UNAVAILABLE", "连不上")
    view = channel_state.STORE.view(fingerprint)
    assert "data_dir" not in view and "cli_path" not in view
    assert "last_error_summary" not in view
    diagnostic = channel_state.STORE.diagnostic_view(fingerprint)
    # 路径在库里是归一形态（大小写/分隔符统一），比较时也归一
    assert channel_state._normalize_path(diagnostic["data_dir"]) == channel_state._normalize_path(
        fingerprint.data_dir)
    assert channel_state._normalize_path(diagnostic["cli_path"]) == channel_state._normalize_path(
        fingerprint.cli_path)
    assert diagnostic["last_error_summary"] is not None


def test_channel_state_is_registered_as_a_research_table() -> None:
    """不登记进 RESEARCH_TABLES，research 侧的 scope 断言会拦住对它的写。"""
    assert "channel_state" in store.RESEARCH_TABLES


# ---------------------------------------------------------------------------
# ⑤ 冻结通道 → 顶层选项（永不回落全局设置）
# ---------------------------------------------------------------------------

def test_channel_options_require_a_frozen_channel() -> None:
    with pytest.raises(UpstreamError) as raised:
        channel_options_from_snapshot(None)
    assert raised.value.code == "CHANNEL_NOT_FROZEN"
    with pytest.raises(UpstreamError):
        channel_options_from_snapshot({})


def test_channel_options_auto_emits_no_top_level_option() -> None:
    assert channel_options_from_snapshot(
        {"browser_source": "auto", "cdp_url": ""}) == {}
    assert channel_options_from_snapshot({"browser_source": "", "cdp_url": ""}) == {}


def test_channel_options_frozen_values_ignore_global_settings() -> None:
    _set("boss_browser_source", "auto")
    assert channel_options_from_snapshot({
        "browser_source": "existing-browser", "cdp_url": "http://127.0.0.1:9222"}) == {
        "--browser-source": "existing-browser", "--cdp-url": "http://127.0.0.1:9222"}


def test_channel_options_rejects_unknown_values() -> None:
    with pytest.raises(UpstreamError) as raised:
        channel_options_from_snapshot({"browser_source": "headful"})
    assert raised.value.code == "TOOL_NOT_ALLOWED"
    with pytest.raises(UpstreamError):
        channel_options_from_snapshot({"cdp_url": "http://10.0.0.5:9222"})


def test_frozen_channel_snapshot_shape() -> None:
    channel = channel_state.freeze_channel()
    assert {"browser_source", "cdp_url", "data_dir", "cli_path",
            "fingerprint", "frozen_at"} <= set(channel)
    assert channel["fingerprint"] == channel_state.fingerprint_from_channel(channel).digest()


def test_runtime_detects_channel_change_within_one_run() -> None:
    fingerprint = _fp(source=SOURCE_EXISTING_BROWSER)
    channel = channel_state.RUNTIME.freeze(browser_source=SOURCE_EXISTING_BROWSER,
                                           data_dir=DATA_DIR, cli_path=CLI_PATH)
    run = {"config_snapshot": {"channel": channel}}
    channel_state.RUNTIME.assert_unchanged(run, fingerprint)      # 同一通道：放行
    run["config_snapshot"]["channel"]["browser_source"] = "auto"
    with pytest.raises(UpstreamError) as raised:
        channel_state.RUNTIME.assert_unchanged(run, fingerprint)
    assert raised.value.code == "CHANNEL_CHANGED"


# ---------------------------------------------------------------------------
# ⑥ 预检（doctor）行为
# ---------------------------------------------------------------------------

def test_doctor_gets_cdp_url_but_never_browser_source() -> None:
    """上游 doctor 不接受 --browser-source（源码确认），只接受 --cdp-url。"""
    captured: dict = {}

    def runner(cli_path, data_dir, timeout_seconds, top_level_options=None):
        captured["top_level_options"] = dict(top_level_options or {})
        return {"summary": "ok", "checks": [{"name": "cdp", "status": "ok"}],
                "safe_hints": None, "operator_actions": []}

    probe = ChannelProbe(runner=runner)
    evidence = probe.precheck(ChannelPolicy.for_source(SOURCE_STORED_COOKIE,
                                                       "http://127.0.0.1:9222"),
                              CLI_PATH, DATA_DIR, "http://127.0.0.1:9222")
    assert captured["top_level_options"] == {CDP_URL_OPTION: "http://127.0.0.1:9222"}
    assert "--browser-source" not in captured["top_level_options"]
    assert evidence["ok"] is True and evidence["decisive"] is True


def test_precheck_without_cli_is_not_decisive() -> None:
    evidence = ChannelProbe().precheck(ChannelPolicy.for_source(SOURCE_EXISTING_BROWSER),
                                       "", DATA_DIR)
    assert evidence["ok"] is False
    assert evidence["decisive"] is False
    assert evidence["error_code"] == "DEPENDENCY_MISSING"


def test_precheck_auto_is_not_decisive() -> None:
    """auto：预检不下结论，等真实采集证明（headless 兜底不依赖外部通道）。"""
    def runner(*_args, **_kwargs):
        raise AssertionError("auto 通道的预检不应拉起子进程")

    evidence = ChannelProbe(runner=runner).precheck(ChannelPolicy.for_source(SOURCE_AUTO),
                                                    CLI_PATH, DATA_DIR)
    assert evidence["decisive"] is False


def test_precheck_unknown_check_shape_is_treated_as_unavailable() -> None:
    """check 形状不认识一律判不可用：如实报告优先，不猜。"""
    def runner(*_args, **_kwargs):
        return {"summary": "ok", "checks": [{"name": "cdp", "status": "whatever"}],
                "safe_hints": None, "operator_actions": []}

    evidence = ChannelProbe(runner=runner).precheck(
        ChannelPolicy.for_source(SOURCE_EXISTING_BROWSER), CLI_PATH, DATA_DIR)
    assert evidence["ok"] is False
    assert evidence["decisive"] is True


# ---------------------------------------------------------------------------
# ⑦ 失败分类（前端空态的唯一依据）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code,expected", [
    ("OWN_BROWSER_NO_SESSION", "channel"),
    ("OWN_BROWSER_NOT_READY", "channel"),
    ("CHANNEL_NOT_READY", "channel"),
    ("CDP_UNAVAILABLE", "channel"),
    ("UPSTREAM_TIMEOUT", "channel"),
    ("UPSTREAM_NETWORK", "channel"),
    ("AUTH_REQUIRED", "auth"),
    ("AUTH_EXPIRED", "auth"),
    ("ACCOUNT_RISK", "risk"),
    ("PLATFORM_VERIFICATION_REQUIRED", "risk"),
])
def test_failure_kind_by_code(code: str, expected: str) -> None:
    assert user_view.failure_kind("paused", None, code, {}) == expected


def test_failure_kind_empty_only_when_search_really_succeeded() -> None:
    """只有「真实成功返回的空列表」才是 empty；调用失败一律不是。"""
    assert user_view.failure_kind("completed", "completed", None, {"collected": 0},
                                  last_search_ok=True) == "empty"
    assert user_view.failure_kind("completed", "completed", None, {"collected": 0},
                                  last_search_ok=False) == "none"
    assert user_view.failure_kind("completed", "completed", None, {"collected": 0},
                                  last_search_ok=None) == "none"


def test_failure_kind_partial_and_completed() -> None:
    assert user_view.failure_kind("partial", None, None, {"collected": 5, "failed": 2}) == "partial"
    assert user_view.failure_kind("completed", None, None,
                                  {"collected": 5, "failed": 2}) == "partial"
    assert user_view.failure_kind("completed", None, None,
                                  {"collected": 5, "failed": 0}) == "none"


def test_channel_failure_copy_never_says_user_is_not_logged_in() -> None:
    """不许说「你没有登录」：账号事实与通道事实是两回事。"""
    message = user_view.BROWSER_CHANNEL_MESSAGES["OWN_BROWSER_NO_SESSION"]
    assert message == "当前搜索未能连接到可用的 BOSS 浏览器会话，搜索已暂停，关键词已保留。"
    assert "没有登录" not in message and "未登录" not in message
    assert user_view.ACCOUNT_OK_CHANNEL_NOT_READY == "账号登录已验证，当前搜索连接尚未就绪。"
    view = user_view.channel_not_ready_view()
    assert view["user_message"] == user_view.ACCOUNT_OK_CHANNEL_NOT_READY
    assert view["can"]["open_diag"] is True


def test_channel_unknown_copy_is_neutral_and_never_claims_ready() -> None:
    """通道尚未确认（unknown）→ 如实说「正在确认搜索通道」，既不宣称已就绪、
    也不误说成「没有登录 / 连接未就绪」（口径与前端顶栏 ``connected_confirming`` 一致）。"""
    assert user_view.ACCOUNT_OK_CHANNEL_CONFIRMING == "BOSS 已连接，正在确认搜索通道"
    view = user_view.channel_confirming_view()
    assert view["user_message"] == user_view.ACCOUNT_OK_CHANNEL_CONFIRMING
    assert view["user_stage"] == "connected"
    assert "未登录" not in view["user_message"]
    assert user_view.ACCOUNT_OK_CHANNEL_CONFIRMING != user_view.ACCOUNT_OK_CHANNEL_NOT_READY
