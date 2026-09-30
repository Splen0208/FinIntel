# -*- coding: utf-8 -*-
"""code 37（「您的环境存在异常」）根因候选：清理 spawn 上游子进程时的**回环代理**环境变量。

为什么这是真问题：上游 CLI 走 httpx 的通道（``status --live`` / detail / user_info）默认
``trust_env=True``，会读 ``http_proxy`` / ``https_proxy`` 环境变量；而浏览器通道（Chrome）
在 Windows 上只认**系统代理**（本项目 ``ProxyEnable=0``），走**真实网络**。某些运行环境会
注入 ``http://127.0.0.1:<port>`` 的回环代理 → 登录/搜索从一个出口 IP、在线复核/详情从另一个
出口 IP → 同一登录会话出现**两个出口**，与平台 code 37「环境异常」语义吻合。

本模块只验证「净化」这一条，且**只**针对回环代理：

* 回环代理（127.0.0.1 / localhost / ::1）→ 从子进程环境删除；
* **非回环**代理（用户/公司有意配置）→ **原样保留**（删掉会把人家的网络弄坏）；
* 无代理变量 → 环境不变；
* 所有 spawn 点都走唯一 helper（源码级断言：不许再有裸 ``os.environ`` 拷贝用于 spawn）；
* 清理**不静默**：写运行日志 + ``/api/health`` 暴露 ``env_proxy_stripped``。

全程离线：只碰进程环境，零真实请求、零子进程真的启动。
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import test_pipeline as base  # noqa: F401  —— 触发后端 sys.path / 临时库 / 迁移
import login_bridge
import managed_browser as mb
import upstream_config

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _production_files() -> list[Path]:
    """后端所有生产 ``*.py``（排除 ``tests/`` 目录）——源码级断言会全量扫描它们。"""
    return sorted(
        path for path in BACKEND_DIR.glob("*.py")
        if path.is_file()
    )


def _read(name: str) -> str:
    return (BACKEND_DIR / name).read_text(encoding="utf-8")


def _loopback_env() -> dict[str, str]:
    return {
        "HTTP_PROXY": "http://127.0.0.1:61488",
        "http_proxy": "http://127.0.0.1:61488",
        "https_proxy": "http://localhost:61488",
        "ALL_PROXY": "socks5://[::1]:1080",
        "no_proxy": "127.0.0.1",
        "PATH": "/usr/bin",
        "PYTHONIOENCODING": "utf-8",
    }


@pytest.fixture(autouse=True)
def _reset_strip_memo(monkeypatch):
    """净化器对「同一组被删变量」只记一次日志；测试间重置备忘，保证日志断言稳定。"""
    monkeypatch.setattr(upstream_config, "_last_stripped_proxy_keys", ())


# ---------------------------------------------------------------------------
# 1) 回环代理 → 删除（含大小写变体）
# ---------------------------------------------------------------------------

def test_loopback_proxy_vars_are_removed_from_child_env() -> None:
    env = upstream_config.sanitized_child_env(_loopback_env())
    for key in ("HTTP_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "no_proxy"):
        assert key not in env, f"{key} 指向回环地址，必须从子进程环境移除"
    # 非代理变量原样保留
    assert env["PATH"] == "/usr/bin"
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_loopback_detector_covers_documented_host_forms() -> None:
    for value in ("http://127.0.0.1:1", "127.0.0.1:1", "http://localhost:1",
                  "socks5://[::1]:1080", "http://[::1]:1"):
        assert upstream_config._is_loopback_proxy(value) is True, value
    for value in ("http://10.0.0.5:8080", "http://proxy.corp:3128", "", "not-a-url"):
        assert upstream_config._is_loopback_proxy(value) is False, value


# ---------------------------------------------------------------------------
# 2) 非回环代理 → 原样保留（绝不扩大删除范围）
# ---------------------------------------------------------------------------

def test_non_loopback_proxy_vars_are_preserved() -> None:
    given = {
        "http_proxy": "http://10.0.0.5:8080",
        "HTTPS_PROXY": "http://proxy.corp.example:3128",
        "all_proxy": "socks5://192.168.1.9:1080",
        # 绕过清单是多条目值，不"指向回环"，保守保留：
        "no_proxy": "localhost,127.0.0.1,.internal",
    }
    assert upstream_config.sanitized_child_env(given) == given


# ---------------------------------------------------------------------------
# 3) 无代理变量 → 环境不变
# ---------------------------------------------------------------------------

def test_env_without_proxy_is_unchanged() -> None:
    given = {"PATH": "/usr/bin", "HOME": "/home/u", "CODEBUDDY_SERVICE_PROXY_URL": "http://x/y"}
    snap = dict(given)
    assert upstream_config.sanitized_child_env(given) == snap


def test_sanitizer_reads_os_environ_by_default(monkeypatch) -> None:
    # Windows 环境变量大小写不敏感：用「小写化视图」断言，跨平台稳健。
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:61488")     # 回环 → 删
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.5:8080")      # 非回环 → 留
    env = {key.lower(): value for key, value in upstream_config.sanitized_child_env().items()}
    assert "http_proxy" not in env
    assert env["https_proxy"] == "http://10.0.0.5:8080"


# ---------------------------------------------------------------------------
# 4) 不静默：写运行日志 + /api/health 暴露布尔
# ---------------------------------------------------------------------------

def test_stripping_is_logged_not_silent(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="finintel.upstream_config"):
        upstream_config.sanitized_child_env(_loopback_env())
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "回环代理" in messages, "清理回环代理必须留痕，不许静默"
    assert "HTTP_PROXY" in messages, "日志应含被清理的变量名"


def test_env_proxy_stripped_reflects_os_environ(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:61488")
    assert upstream_config.env_proxy_stripped() is True
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.setenv("http_proxy", "http://10.0.0.5:8080")
    assert upstream_config.env_proxy_stripped() is False


def test_health_exposes_env_proxy_stripped(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:61488")
    with TestClient(base.app_module.app, raise_server_exceptions=False) as client:
        body = client.get("/api/health").json()
    payload = body.get("data") if isinstance(body, dict) and "data" in body else body
    assert payload["env_proxy_stripped"] is True, "/api/health 诊断块必须如实暴露环境代理清理状态"


# ---------------------------------------------------------------------------
# 5) 所有 spawn 点都走唯一 helper（源码级断言）
# ---------------------------------------------------------------------------

def test_no_bare_os_environ_copy_used_for_spawn() -> None:
    offenders = [path.name for path in _production_files()
                 if "os.environ.copy()" in path.read_text(encoding="utf-8")]
    assert offenders == [], (
        f"这些生产文件仍在用裸 os.environ 拷贝（必须走 sanitized_child_env）：{offenders}"
    )


def test_upstream_cli_spawn_uses_helper() -> None:
    source = _read("upstream_config.py")
    assert "environment = sanitized_child_env()" in source
    assert "env=environment" in source


def test_login_spawn_uses_helper() -> None:
    source = _read("login_bridge.py")
    assert "environment = sanitized_child_env()" in source
    assert "env=environment" in source


def test_dedicated_browser_spawn_uses_helper() -> None:
    source = _read("managed_browser.py")
    assert "env=sanitized_child_env()" in source


# ---------------------------------------------------------------------------
# 6) 集成：三个 spawn 点出来的子进程环境真的不含回环代理
# ---------------------------------------------------------------------------

def test_run_upstream_passes_sanitized_env(monkeypatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:61488")
    captured: dict = {}

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        raise FileNotFoundError()      # 让 run_upstream 走异常分支并返回 RawInvocation

    monkeypatch.setattr(upstream_config.asyncio, "create_subprocess_exec", _fake_exec)
    result = asyncio.run(upstream_config.run_upstream(
        "boss.exe", "status", data_dir=BACKEND_DIR, timeout_seconds=5))
    assert captured["env"] is not None
    assert "HTTP_PROXY" not in captured["env"]
    assert result.returncode == -1


def test_login_subprocess_gets_sanitized_env(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:61488")
    captured: dict = {}
    monkeypatch.setattr(login_bridge.subprocess, "Popen",
                        lambda argv, **kwargs: captured.update(kwargs) or object())
    login_bridge._default_spawn(["boss.exe", "login"])
    assert "HTTPS_PROXY" not in captured["env"]


def test_dedicated_browser_subprocess_gets_sanitized_env(monkeypatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:61488")
    monkeypatch.setattr(mb, "endpoint_for_profile", lambda profile: None)
    monkeypatch.setattr(mb, "PROFILE", Path(tempfile.mkdtemp(prefix="finintel_env_")) / "boss-browser")
    monkeypatch.setattr(mb, "chrome_path", lambda: Path("chrome.exe"))
    captured: dict = {}

    class _Boom(RuntimeError):
        pass

    def _fake_popen(argv, **kwargs):
        captured.update(kwargs)
        raise _Boom()

    monkeypatch.setattr(mb.subprocess, "Popen", _fake_popen)
    with pytest.raises(_Boom):
        mb.ensure()
    assert "HTTP_PROXY" not in captured["env"], "专用浏览器子进程也不得继承回环代理"
