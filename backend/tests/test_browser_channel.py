# -*- coding: utf-8 -*-
"""采集通道（用用户自己已登录的浏览器）契约测试：不需要 BOSS 账号与网络。

§⑤ 是本次增量（T03 采集链路通道冻结与失败回写）：任务一旦创建就把通道**冻结**
进 ``config_snapshot.channel``，采集时只认冻结值，绝不回落全局设置。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# 与其它测试模块同口径：导入 store 之前把库指向临时文件，绝不污染真实库
_TMP_DIR = Path(tempfile.mkdtemp(prefix="finintel_browser_channel_"))
os.environ["FININTEL_DB"] = str(_TMP_DIR / "browser_channel.sqlite3")

import store  # noqa: E402
from store import query_one  # noqa: E402

store.apply_migrations()

import channel_state  # noqa: E402
import source_boss  # noqa: E402
import upstream_config  # noqa: E402
import user_view  # noqa: E402
from upstream_config import (  # noqa: E402
    RawInvocation,
    build_argv,
    build_login_argv,
    channel_options_from_snapshot,
    resolve_browser_options,
    validate_browser_option,
)
from upstream_envelope import UpstreamError, parse_envelope, require_success  # noqa: E402

DATA_DIR = Path("C:/tmp/upstream")
OWN_BROWSER = {"--browser-source": "existing-browser", "--cdp-url": "http://127.0.0.1:9222"}


def invocation(payload: object, command: str = "search") -> RawInvocation:
    argv = ["boss", "--data-dir", str(DATA_DIR), "--json", command]
    return RawInvocation(
        argv=argv,
        argv_redacted=list(argv),
        returncode=1,
        stdout=json.dumps(payload, ensure_ascii=False),
        stderr="",
        elapsed_ms=9,
        timed_out=False,
        data_dir=str(DATA_DIR),
    )


def failed_envelope(code: str, command: str = "search"):
    payload = {
        "ok": False,
        "schema_version": "1.0",
        "command": command,
        "data": None,
        "pagination": None,
        "error": {"code": code, "message": "browser session not found",
                  "recoverable": True, "recovery_action": None},
        "hints": None,
    }
    return parse_envelope(invocation(payload, command))


# ---------------------------------------------------------------------------
# ① argv 形状：顶层选项必须在子命令之前
# ---------------------------------------------------------------------------

def test_top_level_options_are_placed_before_subcommand() -> None:
    argv = build_argv(
        "boss.exe", "search",
        data_dir=DATA_DIR,
        positional=("消费金融 产品经理",),
        options={"--page": 1, "--no-cache": True},
        top_level_options=OWN_BROWSER,
    )
    assert argv == [
        "boss.exe", "--data-dir", str(DATA_DIR.resolve()), "--json",
        "--browser-source", "existing-browser", "--cdp-url", "http://127.0.0.1:9222",
        "search", "消费金融 产品经理", "--page", "1", "--no-cache",
    ]
    # 子命令在最后一个顶层选项之后
    assert argv.index("search") > argv.index("--cdp-url")


def test_top_level_options_apply_to_detail_too() -> None:
    argv = build_argv(
        "boss.exe", "detail",
        data_dir=DATA_DIR,
        positional=("security-123",),
        options={"--job-id": "job-1"},
        top_level_options=OWN_BROWSER,
    )
    assert argv == [
        "boss.exe", "--data-dir", str(DATA_DIR.resolve()), "--json",
        "--browser-source", "existing-browser", "--cdp-url", "http://127.0.0.1:9222",
        "detail", "security-123", "--job-id", "job-1",
    ]


def test_argv_without_top_level_options_is_unchanged() -> None:
    argv = build_argv(
        "boss.exe", "search",
        data_dir=DATA_DIR,
        positional=("消费金融 产品经理",),
        options={"--page": 2, "--city": "北京"},
    )
    assert argv == [
        "boss.exe", "--data-dir", str(DATA_DIR.resolve()), "--json", "search",
        "消费金融 产品经理", "--page", "2", "--city", "北京",
    ]


def test_empty_top_level_options_behave_like_none() -> None:
    base = build_argv("boss.exe", "status", data_dir=DATA_DIR)
    assert build_argv("boss.exe", "status", data_dir=DATA_DIR, top_level_options={}) == base
    assert build_argv("boss.exe", "status", data_dir=DATA_DIR,
                      top_level_options={"--cdp-url": ""}) == base
    assert build_argv("boss.exe", "status", data_dir=DATA_DIR,
                      top_level_options={"--browser-source": "   "}) == base


# ---------------------------------------------------------------------------
# ② 白名单与回环地址校验
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source", ["auto", "stored-cookie", "existing-browser"])
def test_allowed_browser_sources(source: str) -> None:
    argv = build_argv("boss.exe", "search", data_dir=DATA_DIR, positional=("q",),
                      top_level_options={"--browser-source": source})
    assert argv[4:6] == ["--browser-source", source]


@pytest.mark.parametrize("source", ["headful", "Existing-Browser", "own-chrome", "--json"])
def test_unknown_browser_source_is_rejected(source: str) -> None:
    with pytest.raises(UpstreamError) as raised:
        build_argv("boss.exe", "search", data_dir=DATA_DIR, positional=("q",),
                   top_level_options={"--browser-source": source})
    assert raised.value.code == "TOOL_NOT_ALLOWED"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:9222",
    "http://localhost:9222",
    "https://127.0.0.1:9222",
    "http://localhost:9222/",
    "http://[::1]:9222",
])
def test_loopback_cdp_urls_are_accepted(url: str) -> None:
    argv = build_argv("boss.exe", "search", data_dir=DATA_DIR, positional=("q",),
                      top_level_options={"--cdp-url": url})
    assert argv[4:6] == ["--cdp-url", url]


@pytest.mark.parametrize("url", [
    "http://192.168.1.9:9222",          # 局域网其他机器
    "http://evil.example.com:9222",     # 外部主机
    "http://2130706433:9222",           # 十进制写法的 127.0.0.1，一律拒绝
    "http://127.0.0.1.evil.com:9222",   # 看起来像回环的 hostname
    "ftp://127.0.0.1:9222",             # 非 http(s)
    "127.0.0.1:9222",                   # 缺 scheme
    "http://user:pw@127.0.0.1:9222",    # 带凭据
    "http://",                          # 缺主机
])
def test_non_loopback_or_malformed_cdp_url_is_rejected(url: str) -> None:
    with pytest.raises(UpstreamError) as raised:
        build_argv("boss.exe", "search", data_dir=DATA_DIR, positional=("q",),
                   top_level_options={"--cdp-url": url})
    assert raised.value.code == "TOOL_NOT_ALLOWED"
    assert "本机" in raised.value.operator_action or "http" in raised.value.message


def test_other_top_level_options_are_still_rejected() -> None:
    """顶层白名单只放开两个键；其余（含 --delay / --data-dir）一律拒绝。"""
    for name in ("--delay", "--log-level", "--platform", "--role", "--json"):
        with pytest.raises(UpstreamError) as raised:
            build_argv("boss.exe", "search", data_dir=DATA_DIR, positional=("q",),
                       top_level_options={name: "1"})
        assert raised.value.code == "TOOL_NOT_ALLOWED"


def test_top_level_option_in_subcommand_options_is_still_rejected() -> None:
    """旧的 options 通道依然不允许顶层选项（保持既有防线）。"""
    with pytest.raises(UpstreamError) as raised:
        build_argv("boss.exe", "search", data_dir=DATA_DIR, positional=("q",),
                   options={"--browser-source": "existing-browser"})
    assert raised.value.code == "TOOL_NOT_ALLOWED"


def test_login_argv_still_forbids_browser_channel_options() -> None:
    """登录只允许 --timeout：CDP / browser-source 一律不放开。"""
    with pytest.raises(UpstreamError) as raised:
        build_login_argv("boss.exe", DATA_DIR, options={"--cdp-url": "http://127.0.0.1:9222"})
    assert raised.value.code == "TOOL_NOT_ALLOWED"
    with pytest.raises(UpstreamError):
        build_login_argv("boss.exe", DATA_DIR, options={"--browser-source": "existing-browser"})
    argv = build_login_argv("boss.exe", DATA_DIR)
    assert "--cdp-url" not in argv and "--browser-source" not in argv


# ---------------------------------------------------------------------------
# ③ 设置读取：留空即不启用
# ---------------------------------------------------------------------------

def test_resolve_browser_options_defaults_to_disabled() -> None:
    assert resolve_browser_options(setting_getter=lambda _key: None) == {}
    assert resolve_browser_options(setting_getter=lambda _key: "") == {}


def test_resolve_browser_options_reads_both_settings() -> None:
    values = {"boss_browser_source": "existing-browser", "boss_cdp_url": "http://127.0.0.1:9222"}
    assert resolve_browser_options(setting_getter=lambda key: values.get(key)) == {
        "--browser-source": "existing-browser", "--cdp-url": "http://127.0.0.1:9222",
    }


def test_resolve_browser_options_without_cdp_url_only_sends_source() -> None:
    values = {"boss_browser_source": " existing-browser "}
    assert resolve_browser_options(setting_getter=lambda key: values.get(key)) == {
        "--browser-source": "existing-browser",
    }


def test_resolve_browser_options_ignores_cdp_url_without_source() -> None:
    """未启用通道时不下发调试地址，避免把 auto 通道改到依赖本地凭据的路径。"""
    values = {"boss_cdp_url": "http://127.0.0.1:9222"}
    assert resolve_browser_options(setting_getter=lambda key: values.get(key)) == {}


def test_validate_browser_option_accepts_empty_and_rejects_bad_values() -> None:
    validate_browser_option("boss_browser_source", "")      # 关闭通道
    validate_browser_option("boss_cdp_url", "")
    validate_browser_option("boss_browser_source", "auto")
    validate_browser_option("boss_cdp_url", "http://localhost:9222")
    with pytest.raises(UpstreamError):
        validate_browser_option("boss_browser_source", "headful")
    with pytest.raises(UpstreamError):
        validate_browser_option("boss_cdp_url", "http://10.0.0.5:9222")
    with pytest.raises(UpstreamError):
        validate_browser_option("boss_cli_path", "C:/tmp/boss.exe")


def test_settings_keys_are_declared_in_module() -> None:
    assert upstream_config.BROWSER_SOURCE_SETTING_KEY == "boss_browser_source"
    assert upstream_config.CDP_URL_SETTING_KEY == "boss_cdp_url"
    assert upstream_config.DEFAULT_CDP_URL == "http://127.0.0.1:9222"


# ---------------------------------------------------------------------------
# ④ 两个新失败码 → 人话文案
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("upstream_code, finintel_code", [
    ("CDP_UNAVAILABLE", "OWN_BROWSER_NOT_READY"),
    ("BROWSER_SESSION_NOT_FOUND", "OWN_BROWSER_NO_SESSION"),
])
def test_browser_channel_codes_map_to_human_view(upstream_code: str, finintel_code: str) -> None:
    envelope = failed_envelope(upstream_code)
    with pytest.raises(UpstreamError) as raised:
        require_success(envelope, command="search")
    error = raised.value
    assert error.code == finintel_code

    view = user_view.user_view("failed", "failed", error.code, {})
    assert view["user_stage"] == "env_not_ready"
    assert view["user_message"] == user_view.BROWSER_CHANNEL_MESSAGES[finintel_code]
    # 用户看到的是中文人话，绝不出现英文错误码
    assert finintel_code not in view["user_message"]
    assert upstream_code not in view["user_message"]
    assert not view["user_message"].isascii()
    # 给得出可执行的下一步
    assert view["can"]["open_diag"] is True
    assert view["can"]["resume"] is True
    assert any(action["id"] == "open_diag" for action in view["actions"])


def test_browser_channel_view_on_login_side() -> None:
    view = user_view.user_view("env_not_ready", "disconnected", "OWN_BROWSER_NOT_READY", {})
    assert view["user_message"] == user_view.BROWSER_CHANNEL_MESSAGES["OWN_BROWSER_NOT_READY"]
    assert [action["id"] for action in view["actions"]] == ["open_diag"]


def test_operator_actions_are_human_readable() -> None:
    for code in ("CDP_UNAVAILABLE", "BROWSER_SESSION_NOT_FOUND"):
        envelope = failed_envelope(code)
        with pytest.raises(UpstreamError) as raised:
            require_success(envelope, command="search")
        action = raised.value.operator_action
        assert "--" not in action            # 诊断页也不外泄命令行开关
        assert code not in action
        assert "BOSS" in action


# ---------------------------------------------------------------------------
# ⑤ T03：任务内通道冻结 —— 只认冻结值，绝不回落全局设置
# ---------------------------------------------------------------------------

FROZEN_OWN_BROWSER = {
    "browser_source": "existing-browser",
    "cdp_url": "http://127.0.0.1:9222",
    "data_dir": "C:/tmp/upstream",
    "cli_path": "C:/tmp/venv/Scripts/boss.exe",
    "config_version": 1,
    "fingerprint": "0123456789abcdef",
}
FROZEN_AUTO = {"browser_source": "auto", "cdp_url": "", "fingerprint": "fedcba9876543210"}


def _set(key: str, value: str) -> None:
    store.execute("INSERT INTO app_setting (key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _get(key: str) -> str | None:
    row = query_one("SELECT value FROM app_setting WHERE key=?", (key,))
    return row["value"] if row else None


@pytest.fixture(autouse=True)
def _clean_channel_rows():
    saved = {key: _get(key) for key in ("boss_browser_source", "boss_cdp_url")}
    store.execute("DELETE FROM channel_state")
    yield
    store.execute("DELETE FROM channel_state")
    for key, value in saved.items():
        if value is None:
            store.execute("DELETE FROM app_setting WHERE key=?", (key,))
        else:
            _set(key, value)


@pytest.fixture
def captured_argv(monkeypatch) -> dict[str, Any]:
    """替换掉真实子进程调用，只捕获最终 argv（离线、不发起任何真实搜索）。"""
    captured: dict[str, Any] = {"argv": [], "payload": None}

    async def fake_run_upstream(cli_path: str, command: str, *, data_dir, positional=(),
                                options=None, top_level_options=None,
                                timeout_seconds: int = 120) -> RawInvocation:
        argv = build_argv(cli_path, command, data_dir=data_dir, positional=positional,
                          options=options, top_level_options=top_level_options or {})
        captured["argv"] = argv
        payload = captured["payload"] or {
            "ok": False, "schema_version": "1.0", "command": command, "data": None,
            "pagination": None,
            "error": {"code": "BROWSER_SESSION_NOT_FOUND", "message": "no session",
                      "recoverable": True, "recovery_action": None},
            "hints": None,
        }
        return RawInvocation(
            argv=argv, argv_redacted=list(argv), returncode=1,
            stdout=json.dumps(payload, ensure_ascii=False), stderr="", elapsed_ms=3,
            timed_out=False, data_dir=str(data_dir),
        )

    monkeypatch.setattr(source_boss, "run_upstream", fake_run_upstream)
    return captured


def test_search_without_frozen_channel_is_rejected() -> None:
    """search 漏传 top_level_options → 契约错，而不是悄悄回落全局设置。"""
    with pytest.raises(UpstreamError) as raised:
        asyncio.run(source_boss.search("boss.exe", "消费金融 产品经理", None, 1, DATA_DIR, 10))
    assert raised.value.code == "CHANNEL_NOT_FROZEN"


def test_detail_without_frozen_channel_is_rejected() -> None:
    with pytest.raises(UpstreamError) as raised:
        asyncio.run(source_boss.detail("boss.exe", "security-1", None, None, DATA_DIR, 10))
    assert raised.value.code == "CHANNEL_NOT_FROZEN"


def test_frozen_channel_reaches_search_argv(captured_argv: dict[str, Any]) -> None:
    options = channel_options_from_snapshot(FROZEN_OWN_BROWSER)
    with pytest.raises(UpstreamError):
        asyncio.run(source_boss.search("boss.exe", "消费金融 产品经理", None, 1, DATA_DIR, 10,
                                       top_level_options=options))
    argv = captured_argv["argv"]
    assert argv[4:8] == ["--browser-source", "existing-browser",
                         "--cdp-url", "http://127.0.0.1:9222"]
    assert argv.index("--cdp-url") < argv.index("search")      # 顶层选项在子命令之前


def test_frozen_channel_reaches_detail_argv(monkeypatch) -> None:
    """同一任务的 detail 走同一份冻结通道（detail 现在走浏览器 worker，通道身份必须一致）。"""
    captured: dict[str, Any] = {"argv": []}

    async def fake_run_worker(*, worker_py, python, data_dir, positional=(),
                             cdp_url=None, browser_source=None,
                             rendered_job_id=None,
                             timeout_seconds=120) -> RawInvocation:
        captured["argv"] = [worker_py, cdp_url, browser_source, list(positional)]
        captured['job_id']=rendered_job_id
        payload = {
            "ok": False, "schema_version": "1.0", "command": "detail", "data": None,
            "pagination": None,
            "error": {"code": "BROWSER_SESSION_NOT_FOUND", "message": "no session",
                      "recoverable": True, "recovery_action": None},
            "hints": None,
        }
        return RawInvocation(
            argv=[], argv_redacted=[], returncode=1,
            stdout=json.dumps(payload, ensure_ascii=False), stderr="", elapsed_ms=3,
            timed_out=False, data_dir=str(data_dir),
        )

    monkeypatch.setattr(source_boss, "run_worker", fake_run_worker)
    options = channel_options_from_snapshot(FROZEN_OWN_BROWSER)
    with pytest.raises(UpstreamError):
        asyncio.run(source_boss.detail("boss.exe", "security-1", "job-1", "lid-1", DATA_DIR, 10,
                                       top_level_options=options))
    worker_py, cdp_url, browser_source, positional = captured["argv"]
    assert worker_py == "boss_browser_worker.py"
    assert cdp_url == "http://127.0.0.1:9222"
    assert browser_source == "existing-browser"
    assert positional == ["security-1", "lid-1"]
    assert captured['job_id']=='job-1'



def test_frozen_channel_ignores_later_global_setting_changes(
        captured_argv: dict[str, Any]) -> None:
    """任务创建后改设置：本次采集仍用冻结值（否则就是同一任务中途偷换通道）。"""
    frozen_auto = channel_options_from_snapshot(FROZEN_AUTO)     # 冻结为 auto
    _set("boss_browser_source", "existing-browser")              # 之后改全局设置
    _set("boss_cdp_url", "http://127.0.0.1:9222")
    assert frozen_auto == {}                                     # auto 不下发
    with pytest.raises(UpstreamError):
        asyncio.run(source_boss.search("boss.exe", "消费金融 产品经理", None, 1, DATA_DIR, 10,
                                       top_level_options=frozen_auto))
    argv = captured_argv["argv"]
    assert "--browser-source" not in argv and "--cdp-url" not in argv


def test_global_settings_changes_make_a_new_channel_that_starts_unknown() -> None:
    """换通道：新通道是全新一行 unknown，旧通道的成功不为它放行。"""
    auto_fp = channel_state.ChannelFingerprint(browser_source="auto", data_dir="C:/tmp/upstream",
                                               cli_path="C:/tmp/venv/Scripts/boss.exe")
    channel_state.record_collect_ok(auto_fp, 9)
    assert channel_state.STORE.view(auto_fp)["state"] == "ready"

    own_channel = channel_state.freeze_channel(browser_source="existing-browser",
                                               cdp_url="http://127.0.0.1:9222",
                                               data_dir="C:/tmp/upstream",
                                               cli_path="C:/tmp/venv/Scripts/boss.exe")
    own_fp = channel_state.fingerprint_from_channel(own_channel)
    assert own_fp.digest() != auto_fp.digest()
    assert channel_state.STORE.view(own_fp)["state"] == "unknown"
    policy = channel_state.ChannelPolicy.for_source("existing-browser")
    assert channel_state.collect_gate("connected", channel_state.STORE.view(own_fp),
                                      policy) is False


def test_collect_failure_writes_back_and_blocks_the_next_attempt() -> None:
    """真实采集失败 → 该通道 not_ready → 门禁不再放行（失败必须回写）。"""
    fingerprint = channel_state.fingerprint_from_channel(FROZEN_OWN_BROWSER)
    assert channel_state.STORE.view(fingerprint)["state"] == "unknown"
    channel_state.record_collect_failed(fingerprint, "OWN_BROWSER_NO_SESSION",
                                        "cookie=wt2SecretValue; token abcdef0123456789")
    view = channel_state.STORE.view(fingerprint)
    assert view["state"] == "not_ready"
    assert view["last_error_code"] == "OWN_BROWSER_NO_SESSION"

    policy = channel_state.ChannelPolicy.for_source("existing-browser")
    assert channel_state.collect_gate("connected", view, policy) is False
    # 失败摘要过 redact：凭据值不得落库
    row = query_one("SELECT last_error_summary FROM channel_state WHERE fingerprint=?",
                    (fingerprint.digest(),))
    assert "wt2SecretValue" not in (row["last_error_summary"] or "")


def test_collect_success_makes_the_channel_ready() -> None:
    fingerprint = channel_state.fingerprint_from_channel(FROZEN_OWN_BROWSER)
    channel_state.record_collect_ok(fingerprint, 4)
    policy = channel_state.ChannelPolicy.for_source("existing-browser")
    # 本地账号未 connected 也放行：该通道的凭据只在浏览器里
    assert channel_state.collect_gate("disconnected", channel_state.STORE.view(fingerprint),
                                      policy) is True


def test_channel_not_ready_copy_is_used_for_channel_failures() -> None:
    """通道类失败的用户文案：不许出现「你没登录」，且必须给得出恢复动作。"""
    view = user_view.user_view("paused", "failed", "OWN_BROWSER_NO_SESSION", {})
    assert view["user_message"] == user_view.CHANNEL_NOT_READY_MESSAGE
    assert "没有登录" not in view["user_message"] and "未登录" not in view["user_message"]
    assert view["can"]["open_diag"] is True
