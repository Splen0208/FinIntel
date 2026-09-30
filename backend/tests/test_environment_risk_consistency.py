# -*- coding: utf-8 -*-
"""访问环境风控（code 37）三处修复的**独立单测**（H3 归一 / H4 中性缺码 / H5 CDP 注入）。

与 ``test_pipeline.py`` 里的三接口一致性用例互补：这里测的是**纯函数层**——
上游错误归一（``refine_risk_code``）、缺码风控的中性表述、以及登录 CDP 注入的
唯一显式入口，全部离线，不访问平台。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="finintel_env_risk_"))
os.environ["FININTEL_DB"] = str(_TMP_DIR / "env_risk.sqlite3")

import managed_browser  # noqa: E402
import store  # noqa: E402
import upstream_config  # noqa: E402
import upstream_envelope  # noqa: E402
import user_view  # noqa: E402
from upstream_envelope import UpstreamError  # noqa: E402

store._local = threading.local()
store.apply_migrations()

_SIGNATURE_MESSAGE = ("BOSS 直聘访问环境风控 (code 37): 您的环境存在异常.。"
                      "当前浏览器模式: CDP。已停止且未刷新或重试。")


def _envelope(code: str, message: str) -> SimpleNamespace:
    return SimpleNamespace(
        error={"code": code, "message": message}, operator_actions=[], layer="business",
        raw_snippet="", argv_redacted=[], data_dir="", elapsed_ms=1, command="search",
        returncode=1)


# ---------------------------------------------------------------------------
# H3：同一份证据（上游 message 的 ``访问环境风控`` 签名）把被塌缩的风控码归一
# ---------------------------------------------------------------------------

def test_refine_risk_code_uses_one_shared_signature() -> None:
    """归一判据只有一处（签名），正放/反放都要对，绝不臆测、绝不一律改判。"""
    assert upstream_envelope.ENVIRONMENT_RISK_SIGNATURE == "访问环境风控"

    # 旧映射把「访问环境风控」塌缩成了 ACCOUNT_RISK → 用同一份证据还原
    assert upstream_envelope.refine_risk_code(
        "ACCOUNT_RISK", _SIGNATURE_MESSAGE) == "ENVIRONMENT_RISK"
    # 本就是 ENVIRONMENT_RISK：保持
    assert upstream_envelope.refine_risk_code(
        "ENVIRONMENT_RISK", "访问环境存在异常") == "ENVIRONMENT_RISK"
    # 无签名的账号风控：原样保留（不误判）
    assert upstream_envelope.refine_risk_code("ACCOUNT_RISK", "账号风控拦截") == "ACCOUNT_RISK"
    # 非风控码即便恰好带签名也不改判
    assert upstream_envelope.refine_risk_code("UPSTREAM_NETWORK", _SIGNATURE_MESSAGE) == \
        "UPSTREAM_NETWORK"


def test_map_upstream_error_normalizes_code_for_the_real_path() -> None:
    """走真实映射路径（``map_upstream_error``）复核同源归一与``retryable``口径。"""
    normalized = upstream_envelope.map_upstream_error(_envelope("ACCOUNT_RISK", _SIGNATURE_MESSAGE))
    assert normalized.code == "ENVIRONMENT_RISK"
    assert normalized.retryable is False                      # 环境风控不可自动重试

    account = upstream_envelope.map_upstream_error(_envelope("ACCOUNT_RISK", "账号风控拦截"))
    assert account.code == "ACCOUNT_RISK"

    already = upstream_envelope.map_upstream_error(_envelope("ENVIRONMENT_RISK", "环境异常"))
    assert already.code == "ENVIRONMENT_RISK"


# ---------------------------------------------------------------------------
# H4：缺码的风控不得默认成某个具体风险码
# ---------------------------------------------------------------------------

def test_risk_without_code_is_neutral_not_account_risk() -> None:
    """``state=risk_stopped`` 但无具体码：中性表述 + 诚实指引，绝不冒充账号风控。"""
    view = user_view.user_view("paused", "risk_stopped", None)
    assert view["user_message"] == user_view.RISK_PAUSED_MESSAGE
    assert "平台已暂停本次搜索" in view["user_message"]
    assert "账号" not in view["user_message"]                  # 不再默认成账号风控
    assert view["can"]["check_official"] is True               # 打开 BOSS 官方页面
    assert view["can"]["login"] is False                       # 绝不给重新登录
    assert view["can"]["resume"] is False                      # 也不给「继续」的假按钮
    assert any(a["id"] == "check_official" for a in view["actions"])


def test_environment_risk_surfaces_share_one_core_sentence() -> None:
    """登录块 / 任务页 / 顶栏对**同一次**环境风控共用唯一核心句，且都不触发重新登录。"""
    login = user_view._login_view("connected", "connected", "ENVIRONMENT_RISK")
    task = user_view.user_view("paused", "risk_stopped", "ENVIRONMENT_RISK")
    topbar = user_view.environment_blocked_view()

    for view in (login, task, topbar):
        assert user_view.ENVIRONMENT_BLOCKED_COPY in view["user_message"]
        assert any(a["id"] == "check_official" for a in view["actions"])
        # 环境风控一律不得出现「重新登录 / 重新登录并继续 / 打开官方验证页面（走登录）」
        assert not any(a["id"] in ("login", "login_and_resume", "open_verify")
                       for a in view["actions"])
        assert view["can"]["login"] is False


# ---------------------------------------------------------------------------
# H5：登录 CDP 注入必须是唯一、已校验的显式入口（回环 + 专用 profile 匹配）
# ---------------------------------------------------------------------------

def test_login_cdp_injection_requires_loopback_and_dedicated_profile(
        monkeypatch, tmp_path) -> None:
    """``build_login_argv_with_cdp``：非回环 / 与专用 profile 不一致 / 无活动端点一律拒绝。"""
    base = upstream_config.build_login_argv("C:/x/boss.exe", tmp_path, timeout_seconds=180)
    assert "--cdp-url" not in base and "--cdp" not in base     # 基础 argv 干净

    # 1) 非回环地址：直接拒绝
    monkeypatch.setattr(managed_browser, "endpoint_for_profile",
                        lambda profile: "http://127.0.0.1:9231")
    with pytest.raises(UpstreamError):
        upstream_config.build_login_argv_with_cdp(base, "http://10.0.0.5:9222")

    # 2) 回环但与本项目专用 profile 的活动端点不一致：拒绝
    with pytest.raises(UpstreamError):
        upstream_config.build_login_argv_with_cdp(base, "http://127.0.0.1:2222")

    # 3) 专用 profile 没有活动端点（None）：拒绝
    monkeypatch.setattr(managed_browser, "endpoint_for_profile", lambda profile: None)
    with pytest.raises(UpstreamError):
        upstream_config.build_login_argv_with_cdp(base, "http://127.0.0.1:9231")

    # 4) 匹配专用 profile 的回环端点：放行并注入
    monkeypatch.setattr(managed_browser, "endpoint_for_profile",
                        lambda profile: "http://127.0.0.1:9231")
    argv = upstream_config.build_login_argv_with_cdp(base, "http://127.0.0.1:9231")
    assert argv[argv.index("--cdp-url") + 1] == "http://127.0.0.1:9231"
    assert argv.index("--cdp-url") < argv.index("login") < argv.index("--cdp")

    # 5) login 子命令缺失：拒绝注入
    with pytest.raises(UpstreamError):
        upstream_config.build_login_argv_with_cdp(
            ["boss.exe", "--data-dir", "x", "--json"], "http://127.0.0.1:9231")


def test_login_guard_stays_the_single_decision_point() -> None:
    """注入不再绕开守卫：严格模式仍拒 ``--cdp-url/--cdp``，放行只认**恰好匹配的那一个端点**。"""
    # 默认（严格）模式：登录命令仍不许出现 CDP 选项
    with pytest.raises(UpstreamError):
        upstream_config._assert_no_forbidden_login_options(["--cdp-url", "http://127.0.0.1:9231"])
    with pytest.raises(UpstreamError):
        upstream_config._assert_no_forbidden_login_options(["--cdp"])
    # 显式放行的端点不匹配 → 仍拒绝（不是「有 allow 就全放」）
    with pytest.raises(UpstreamError):
        upstream_config._assert_no_forbidden_login_options(
            ["--cdp-url", "http://127.0.0.1:9999"], allow_cdp_url="http://127.0.0.1:9231")
    # 其它能力放大选项即便在放行模式下也仍被拒
    with pytest.raises(UpstreamError):
        upstream_config._assert_no_forbidden_login_options(
            ["--force"], allow_cdp_url="http://127.0.0.1:9231")
    # 恰好匹配才放行（不抛）
    upstream_config._assert_no_forbidden_login_options(
        ["--cdp-url", "http://127.0.0.1:9231", "--cdp"], allow_cdp_url="http://127.0.0.1:9231")
