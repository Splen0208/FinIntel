# -*- coding: utf-8 -*-
"""T01 基础层 + T02 登录桥接离线单测（不依赖网络与真实账号）。

真实登录链路冒烟（打开真实官方登录窗口、8 秒超时）默认跳过；
需要时显式运行：``FININTEL_REAL_LOGIN_SMOKE=1 pytest tests/test_login_bridge.py -q -k real``
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="finintel_login_bridge_"))
os.environ["FININTEL_DB"] = str(_TMP_DIR / "login_bridge.sqlite3")

import health as health_mod  # noqa: E402
import login_bridge  # noqa: E402
import store  # noqa: E402
import upstream_runtime  # noqa: E402
import user_view  # noqa: E402
from store import query_all, query_one  # noqa: E402
from upstream_config import build_login_argv  # noqa: E402
from upstream_envelope import UpstreamError  # noqa: E402

# 丢弃可能被其他测试模块缓存的连接，确保用上面的临时库
store._local = threading.local()
store.apply_migrations()

PYTHON = str(Path(sys.executable).resolve())
DATA_DIR = (BACKEND_DIR.parent / ".local" / "test-login-upstream").resolve()

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_login_task():
    """每条用例前后清空登录表，并还原可能被用例改写的 CLI 路径设置。

    本仓库的测试模块共用同一个临时库（``FININTEL_DB`` 在导入期被最后导入的模块覆盖），
    因此用例内改写 ``app_setting.boss_cli_path`` 后必须还原，否则 ``runs._upstream()``
    在后续模块里会解析到不存在的 CLI 而报 DEPENDENCY_MISSING。
    """
    previous = query_one("SELECT value FROM app_setting WHERE key='boss_cli_path'")
    store.execute("DELETE FROM login_task")
    yield
    store.execute("DELETE FROM login_task")
    if previous is None:
        store.execute("DELETE FROM app_setting WHERE key='boss_cli_path'")
    else:
        store.execute("INSERT INTO app_setting (key, value) VALUES ('boss_cli_path', ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (previous["value"],))


# ---------------------------------------------------------------------------
# 假 CLI：完整模拟 login / status 两个子命令（含 stderr 进度与标记文件）
# ---------------------------------------------------------------------------

_FAKE_BODY = '''
import json, sys, time
ARGS = sys.argv[1:]
if CONFIG.get("marker"):
    with open(CONFIG["marker"], "a", encoding="utf-8") as handle:
        handle.write(" ".join(ARGS) + "\\n")
for line in CONFIG.get("stderr", []):
    print(line, file=sys.stderr, flush=True)
if "status" in ARGS:
    print(json.dumps(CONFIG["status_env"], ensure_ascii=False))
    sys.exit(CONFIG.get("status_exit", 0))
if CONFIG.get("sleep"):
    time.sleep(CONFIG["sleep"])
if CONFIG.get("login_env") is not None:      # None = 进程死前来不及输出任何信封
    print(json.dumps(CONFIG["login_env"], ensure_ascii=False))
sys.exit(CONFIG.get("login_exit", 1))
'''

_OK_LOGIN = {
    "ok": True, "schema_version": "1.0", "command": "login", "data": {"logged_in": True},
    "pagination": None, "error": None, "hints": None,
}


def _envelope(payload: dict, *, command: str = "login") -> dict:
    return {"ok": True, "schema_version": "1.0", "command": command, "data": payload,
            "pagination": None, "error": None, "hints": None}


def _error_envelope(code: str, message: str, hints: dict | None = None) -> dict:
    return {"ok": False, "schema_version": "1.0", "command": "login", "data": None,
            "pagination": None,
            "error": {"code": code, "message": message, "recoverable": True,
                      "recovery_action": "请重新登录"},
            "hints": hints}


def status_payload(auth_state: str) -> dict:
    """模拟真实 `boss status --live` 的 data 形状（事实：auth_state 只是质量标签）。"""
    usable = auth_state in {"complete", "partial"}
    health = {
        "platform": "zhipin",
        "auth_state": auth_state,
        "summary": "degraded" if auth_state == "partial" else (
            "ok" if usable else "missing"),
        "token_present": usable,
    }
    return {"logged_in": usable, "live": usable, "user_name": "u", "auth_state": auth_state,
            "auth_summary": health["summary"], "auth_health": health}


def write_fake_cli(tmp: Path, *, mode: str = "ok", stderr: tuple[str, ...] = (),
                   sleep: float = 0.0, auth_state: str = "complete",
                   marker: Path | None = None, name: str = "fake_boss",
                   die: bool = False) -> str:
    """写一份假 CLI；``die=True`` 模拟「进程被打断：无信封、直接崩」的场景。"""
    if mode == "ok":
        login_env, login_exit = _OK_LOGIN, 0
    elif mode == "timeout":
        login_env = _error_envelope(
            "LOGIN_TIMEOUT", "扫码登录超时",
            hints={"next_actions": ["boss login --timeout 180", "boss-chrome"],
                   "operator_actions": ["请在 BOSS 官方页面完成登录", "重新发起登录",
                                        "建议启动 Chrome 调试端口"]})
        login_exit = 1
    elif mode == "platform":
        login_env = _error_envelope("PLATFORM_VERIFICATION_REQUIRED", "平台要求验证")
        login_exit = 1
    elif mode == "env":
        login_env = _error_envelope("BROWSER_KERNEL_MISSING", "浏览器内核缺失")
        login_exit = 1
    elif mode == "auth":
        login_env = _error_envelope("AUTH_REQUIRED", "需要登录")
        login_exit = 1
    else:
        raise ValueError(f"未知 mode={mode}")

    if die:
        login_env, login_exit = None, -1073740791      # 0xC0000409：进程被异常终止
    config = {
        "stderr": list(stderr), "sleep": sleep, "login_env": login_env,
        "login_exit": login_exit,
        "status_env": _envelope(status_payload(auth_state), command="status"),
        "status_exit": 0,
        "marker": str(marker) if marker else None,
    }
    script = tmp / f"{name}.py"
    script.write_text(
        "import json, sys, time\nCONFIG = json.loads(%r)\n" % json.dumps(config, ensure_ascii=False)
        + _FAKE_BODY,
        encoding="utf-8",
    )
    wrapper = tmp / f"{name}.cmd"
    wrapper.write_text(f'@echo off\r\n"{PYTHON}" "{script}" %*\r\n', encoding="utf-8")
    return str(wrapper)


def _wait_for(predicate, timeout: float = 20.0) -> bool:
    """等待状态推进。默认给足子进程冷启动时间（Windows 上叠加载荷会明显变慢）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _poll_login(client, login_id: str, timeout: float = 40.0) -> dict:
    """轮询登录状态直到终态（子进程冷启动 + ≥5s 探测间隔都要容忍）。"""
    deadline = time.monotonic() + timeout
    row: dict = {}
    while time.monotonic() < deadline:
        row = client.get(f"/api/auth/login/{login_id}").json()["data"]["login"]
        if row["status"] in login_bridge.TERMINAL_STATUSES:
            return row
        time.sleep(0.2)
    return row


# ---------------------------------------------------------------------------
# T01：argv / 状态映射 / health
# ---------------------------------------------------------------------------

def test_build_login_argv_shape_and_position() -> None:
    argv = build_login_argv("C:/x/boss.exe", DATA_DIR, timeout_seconds=180)
    assert argv == ["C:/x/boss.exe", "--data-dir", str(DATA_DIR), "--json",
                    "login", "--timeout", "180"]
    assert argv.index("--json") < argv.index("login")
    assert argv.index("--data-dir") < argv.index("--json")


@pytest.mark.parametrize("option", ["--force", "--cdp", "--cdp-url", "--cookie-source",
                                    "--curl-file", "--browser-source", "--platform"])
def test_build_login_argv_rejects_forbidden_options(option: str) -> None:
    with pytest.raises(UpstreamError) as raised:
        build_login_argv("C:/x/boss.exe", DATA_DIR, options={option: "x"})
    assert raised.value.code == "TOOL_NOT_ALLOWED"


def test_build_login_argv_rejects_inline_and_out_of_bounds() -> None:
    with pytest.raises(UpstreamError) as raised:
        build_login_argv("boss.exe --force", DATA_DIR)
    assert raised.value.code == "TOOL_NOT_ALLOWED"
    with pytest.raises(UpstreamError) as raised:
        build_login_argv("C:/x/boss.exe", DATA_DIR, timeout_seconds=9999)
    assert raised.value.code == "TOOL_NOT_ALLOWED"
    assert build_login_argv("C:/x/boss.exe", DATA_DIR, timeout_seconds=8)[-1] == "8"


def test_login_is_not_a_read_only_subcommand() -> None:
    from upstream_config import READ_ONLY_SUBCOMMANDS

    assert "login" not in READ_ONLY_SUBCOMMANDS


NINE_STATES = [
    ("disconnected", (None, "disconnected", None, {}), "登录 BOSS 后即可搜索真实岗位", ["login"]),
    ("starting", ("starting", "connecting", None, {}), "正在打开 BOSS 登录页面", ["cancel"]),
    ("waiting_user", ("waiting_user", "connecting", None, {}), "请在打开的 BOSS 页面完成登录，并保留此窗口供搜索使用",
     ["reopen", "cancel"]),
    ("verifying", ("verifying", "connecting", None, {}), "登录已提交，正在确认连接",
     ["wait", "cancel"]),
    ("connected", ("connected", "connected", None, {}), "BOSS 已连接", ["search"]),
    ("timeout_or_cancelled", ("timeout", "disconnected", "UPSTREAM_LOGIN_TIMEOUT", {}),
     "本次登录未完成，关键词已保留", ["login"]),
    ("platform_verification",
     ("platform_verification", "disconnected", "PLATFORM_VERIFICATION_REQUIRED", {}),
     "BOSS 需要你完成验证", ["open_verify", "cancel"]),
    ("env_not_ready", ("env_not_ready", "disconnected", "BROWSER_KERNEL_MISSING", {}),
     "搜索服务尚未准备好", ["open_diag"]),
    ("auth_expired", ("paused", "auth_required", "AUTH_REQUIRED", {}),
     "BOSS 登录已失效，已保留当前进度", ["login_and_resume"]),
]


@pytest.mark.parametrize("stage,args,message,action_ids", NINE_STATES,
                         ids=[item[0] for item in NINE_STATES])
def test_user_view_nine_states(stage, args, message, action_ids) -> None:
    view = user_view.user_view(*args)
    assert view["user_stage"] == stage
    assert view["user_message"] == message
    assert [action["id"] for action in view["actions"]] == action_ids
    assert "AUTH_REQUIRED" not in view["user_message"]
    assert "MCP_PROTOCOL_ERROR" not in view["user_message"]


def test_user_view_maps_run_states_with_real_counts() -> None:
    assert user_view.user_view("searching", "collecting")["user_message"] == "正在搜索岗位"
    assert user_view.user_view("fetching_details", "collecting", None,
                               {"detail_read": 7})["user_message"] == "正在读取岗位详情，已读取 7 个"
    assert user_view.user_view("organizing", "analyzing", None,
                               {"organized": 5})["user_message"] == "正在整理公司方向，已完成 5 个"
    assert user_view.user_view("completed", "completed", None,
                               {"collected": 1, "organized": 1})["user_message"] == "整理完成，直接展示结果"
    assert user_view.user_view("completed", "completed", None, {"collected": 0})["user_stage"] == "empty"
    assert user_view.user_view("cancelled", "stopped")["user_message"] == "已停止，保留已获取内容"
    assert user_view.user_view("failed", "failed", "AI_NOT_CONFIGURED",
                               {"collected": 4})["user_message"] == "岗位已保存，暂未完成整理"


def test_website_ready_ignores_mcp_failure(tmp_path, monkeypatch) -> None:
    # MCP 的事实来自独立握手文件，不能伪造一行普通 health_check 替代。
    probe_dir = tmp_path / '.local' / 'probe'
    probe_dir.mkdir(parents=True)
    (probe_dir / 'mcp_handshake_offline.json').write_text(
        json.dumps({'ok':False, 'used_by_website':False}), encoding='utf-8')
    monkeypatch.setattr(health_mod, 'PROJECT_DIR', tmp_path)
    now = store.utcnow_iso()
    store.execute("DELETE FROM health_check")
    for key, status in (("cli_path", "ok"), ("local_credential", "ok"),
                        ("mcp_protocol", "error")):
        store.execute(
            "INSERT INTO health_check (key,status,checked_at,elapsed_ms,detail,error_code,"
            "error_message,operator_actions,evidence,updated_at) VALUES (?,?,?,NULL,'',?,NULL,"
            "'[]','{}',?)",
            (key, status, now, "MCP_PROTOCOL_ERROR" if key == "mcp_protocol" else None, now))
    assert health_mod.cached_layer_view()["mcp_protocol"].status == "error"
    assert health_mod.integration_state()[0] == "mcp_unverified"   # 诊断口径原样保留
    assert health_mod.website_ready() is True                       # 首页口径忽略 MCP


def test_migration_006_is_idempotent() -> None:
    applied = store.apply_migrations()
    assert applied == [], applied
    columns = {row["name"] for row in query_all("PRAGMA table_info(research_run)")}
    assert {"stage", "stage_updated_at", "checkpoint", "user_message", "cancel_requested",
            "query_text", "filters", "origin", "organized_count", "failed_count"} <= columns
    assert query_one("SELECT name FROM sqlite_master WHERE type='table' AND name='login_task'")
    assert query_one("SELECT name FROM sqlite_master WHERE type='table' AND name='run_direction'")
    index = query_one("SELECT sql FROM sqlite_master WHERE name='uq_login_task_active'")
    assert "WHERE active = 1" in index["sql"]


def test_server_json_is_not_overwritten_without_port(tmp_path: Path,
                                                    monkeypatch) -> None:
    """解析不到监听端口时不写 server.json。

    测试/脚本 `import app` 也会走到 write_server_json，若无条件写入就会用
    「测试进程 pid + port=None」覆盖真实运行实例的记录，使 /api/health 误报 stale。
    """
    fake_json = tmp_path / "run" / "server.json"
    fake_json.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(upstream_runtime, "RUN_DIR", fake_json.parent)
    monkeypatch.setattr(upstream_runtime, "SERVER_JSON", fake_json)
    marker = {"pid": 424242, "port": 8766, "entry": "backend/app.py"}
    fake_json.write_text(json.dumps(marker), encoding="utf-8")

    monkeypatch.delenv("FININTEL_PORT", raising=False)
    monkeypatch.setattr(sys, "argv", ["app.py"])
    payload = upstream_runtime.write_server_json("backend/app.py")
    assert payload["port"] is None
    assert json.loads(fake_json.read_text(encoding="utf-8")) == marker   # 未被覆盖

    monkeypatch.setenv("FININTEL_PORT", "8766")
    payload = upstream_runtime.write_server_json("backend/app.py")
    assert payload["port"] == 8766
    written = json.loads(fake_json.read_text(encoding="utf-8"))
    assert written["port"] == 8766 and written["pid"] == os.getpid()


# ---------------------------------------------------------------------------
# T02：登录桥接
# ---------------------------------------------------------------------------

def test_start_reaches_waiting_user_then_cancel(tmp_path: Path) -> None:
    cli = write_fake_cli(tmp_path, stderr=("已打开 BOSS 直聘登录页。", "请扫码或手机号登录（超时 8 秒）...",),
                         sleep=30)
    controller = login_bridge.LoginController(
        cli_path_provider=lambda: cli,
        data_dir_provider=lambda: type("R", (), {"path": DATA_DIR})(),
    )
    view = controller.start(timeout_seconds=30)
    assert view["status"] == "starting"
    assert _wait_for(lambda: controller.get(view["login_id"])["status"] == "waiting_user"), \
        controller.get(view["login_id"])["status"]
    row = controller.get(view["login_id"])
    assert row["stage"] == "opened"
    assert "已打开 BOSS 直聘登录页" in (row["stderr_tail"] or "")
    # 中间态也必须同步 user_message（诊断页直读该列，不能留「正在打开…」）
    assert row["user_message"] == "请在打开的 BOSS 页面完成登录，并保留此窗口供搜索使用"
    assert row["user_message"] == controller.view(row)["user_message"]

    process = controller._processes[view["login_id"]]
    cancelled = controller.cancel(view["login_id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["user_stage"] == "timeout_or_cancelled"
    assert cancelled["user_message"] == "本次登录未完成，关键词已保留"
    assert controller.active() is None
    # 进程被真正回收（不是只在 DB 里改状态）
    assert _wait_for(lambda: process.poll() is not None, timeout=8)
    # 收尾线程发现进程已退出后必须摘掉句柄（登记表只保存仍在运行的流程）
    assert _wait_for(lambda: view["login_id"] not in controller._processes, timeout=8)


def test_timeout_envelope_maps_to_timeout_state(tmp_path: Path) -> None:
    cli = write_fake_cli(tmp_path, mode="timeout", stderr=("请扫码或手机号登录（超时 8 秒）...",))
    controller = login_bridge.LoginController(
        cli_path_provider=lambda: cli,
        data_dir_provider=lambda: type("R", (), {"path": DATA_DIR})(),
    )
    view = controller.start(timeout_seconds=8)
    assert _wait_for(lambda: controller.get(view["login_id"])["status"] == "timeout")
    row = controller.get(view["login_id"])
    assert row["error_code"] == "LOGIN_TIMEOUT"
    assert row["user_message"] == "本次登录未完成，关键词已保留"
    assert row["active"] == 0
    # `hints.next_actions` 必须被删除、不透传；operator_actions 不落库
    assert "boss login --timeout 180" not in json.dumps(row, ensure_ascii=False)
    assert "boss-chrome" not in json.dumps(row, ensure_ascii=False)
    assert "调试端口" not in (row["stderr_tail"] or "")


def test_wall_clock_limit_kills_long_login(tmp_path: Path) -> None:
    cli = write_fake_cli(tmp_path, sleep=30)
    controller = login_bridge.LoginController(
        cli_path_provider=lambda: cli,
        data_dir_provider=lambda: type("R", (), {"path": DATA_DIR})(),
        grace_seconds=0,
    )
    started = time.monotonic()
    view = controller.start(timeout_seconds=5)
    assert _wait_for(lambda: controller.get(view["login_id"])["status"] == "timeout", timeout=15)
    assert time.monotonic() - started < 25
    assert controller.get(view["login_id"])["error_code"] == "UPSTREAM_LOGIN_TIMEOUT"


def test_concurrent_start_returns_same_flow(tmp_path: Path) -> None:
    cli = write_fake_cli(tmp_path, stderr=("已打开 BOSS 直聘登录页。",), sleep=30)
    controller = login_bridge.LoginController(
        cli_path_provider=lambda: cli,
        data_dir_provider=lambda: type("R", (), {"path": DATA_DIR})(),
    )
    first = controller.start(timeout_seconds=30)
    second = controller.start(timeout_seconds=30)
    third = controller.start(timeout_seconds=30)
    assert first["login_id"] == second["login_id"] == third["login_id"]
    assert len(query_all("SELECT * FROM login_task WHERE active=1")) == 1
    assert len(query_all("SELECT * FROM login_task")) == 1
    controller.cancel(first["login_id"])


@pytest.mark.parametrize("mode,expected_status,expected_code,expected_stage", [
    ("platform", "platform_verification", "PLATFORM_VERIFICATION_REQUIRED", "platform_verification"),
    ("env", "env_not_ready", "BROWSER_KERNEL_MISSING", "env_not_ready"),
    ("auth", "failed", "AUTH_REQUIRED", "timeout_or_cancelled"),
])
def test_classify_error_terminal_states(tmp_path: Path, mode, expected_status,
                                        expected_code, expected_stage) -> None:
    cli = write_fake_cli(tmp_path, mode=mode)
    controller = login_bridge.LoginController(
        cli_path_provider=lambda: cli,
        data_dir_provider=lambda: type("R", (), {"path": DATA_DIR})(),
    )
    view = controller.start(timeout_seconds=30)
    assert _wait_for(lambda: controller.get(view["login_id"])["status"] == expected_status), \
        controller.get(view["login_id"])["status"]
    assert controller.get(view["login_id"])["error_code"] == expected_code
    assert controller.view(controller.get(view["login_id"]))["user_stage"] == expected_stage


def test_connected_requires_live_verification(tmp_path: Path) -> None:
    cli = write_fake_cli(tmp_path, mode="ok", auth_state="complete")
    calls: list[str] = []

    def probe(cli_path: str, data_dir: str, timeout_seconds: int) -> dict:
        calls.append(cli_path)
        return {"ok": True, "auth_state": "complete", "data_dir_echo": str(data_dir)}

    controller = login_bridge.LoginController(
        cli_path_provider=lambda: cli,
        data_dir_provider=lambda: type("R", (), {"path": DATA_DIR})(),
        live_probe=probe,
    )
    view = controller.start(timeout_seconds=30)
    assert _wait_for(lambda: controller.get(view["login_id"])["status"] == "connected")
    row = controller.get(view["login_id"])
    assert row["live_verified_at"]
    assert row["active"] == 0
    assert len(calls) == 1                     # 成功时只探测 1 次
    assert controller.view(row)["user_stage"] == "connected"


def test_managed_login_and_live_probe_use_same_endpoint(tmp_path, monkeypatch):
    import managed_browser
    marker = tmp_path / 'commands.txt'
    cli = write_fake_cli(tmp_path, mode='ok', auth_state='complete', marker=marker)
    frozen = {'browser_source':'stored-cookie', 'cdp_url':'http://127.0.0.1:9231'}
    monkeypatch.setattr(login_bridge.channel_state, 'freeze_channel', lambda: frozen)
    # 登录 CDP 注入现在要求端点匹配专用 profile 的活端点：打桩成同一地址。
    monkeypatch.setattr(managed_browser, 'endpoint_for_profile',
                        lambda profile: frozen['cdp_url'])
    controller = login_bridge.LoginController(cli_path_provider=lambda: cli,
        data_dir_provider=lambda: type('R', (), {'path':DATA_DIR})())
    controller.browser_provider = lambda: frozen['cdp_url']
    view = controller.start(timeout_seconds=30)
    assert _wait_for(lambda: controller.get(view['login_id'])['status'] == 'connected')
    lines = marker.read_text(encoding='utf-8').splitlines()
    assert len(lines) == 2
    for line in lines:
        assert '--cdp-url http://127.0.0.1:9231' in line
    assert 'login --timeout 30 --cdp' in lines[0]
    assert '--browser-source stored-cookie' in lines[1] and 'status --live' in lines[1]
    assert store.jloads(controller.get(view['login_id'])['channel_snapshot'], {}) == frozen


def test_restart_releases_orphan_login_without_killing_stored_pid(tmp_path, monkeypatch):
    cli = write_fake_cli(tmp_path, mode='ok', auth_state='complete')
    controller = login_bridge.LoginController(cli_path_provider=lambda: cli,
        data_dir_provider=lambda: type('R', (), {'path':DATA_DIR})())
    monkeypatch.setattr(controller, '_run', lambda *args: None)
    view = controller.start(timeout_seconds=30)
    controller._threads[view['login_id']].join(2)
    monkeypatch.setattr(controller, '_kill', lambda *args: pytest.fail('stored PID must not be killed'))
    controller.recover_pending()
    assert controller.active() is None
    row = controller.get(view['login_id'])
    assert row['status'] == 'cancelled' and row['error_code'] == 'LOGIN_INTERRUPTED'


def test_window_closed_is_not_success(tmp_path: Path) -> None:
    """子进程退出码 0 但在线复核 auth_state=broken → 绝不显示已连接。"""
    cli = write_fake_cli(tmp_path, mode="ok", auth_state="broken")
    calls: list[str] = []

    def probe(cli_path: str, data_dir: str, timeout_seconds: int) -> dict:
        calls.append(cli_path)
        return {"ok": True, "auth_state": "broken", "data_dir_echo": str(data_dir)}

    controller = login_bridge.LoginController(
        cli_path_provider=lambda: cli,
        data_dir_provider=lambda: type("R", (), {"path": DATA_DIR})(),
        live_probe=probe,
    )
    view = controller.start(timeout_seconds=30)
    assert _wait_for(lambda: controller.get(view["login_id"])["status"] == "failed", timeout=15)
    row = controller.get(view["login_id"])
    assert row["live_verified_at"] is None
    assert 1 <= len(calls) <= login_bridge.LOGIN_LIVE_PROBE_ATTEMPTS  # 最多 2 次
    assert controller.view(row)["user_stage"] != "connected"


def test_stderr_is_redacted_and_truncated(tmp_path: Path) -> None:
    secret = "cookie=SECRETCOOKIEVALUE1234567890; token=ABCDEF0123456789ABCDEF0123456789"
    noisy = tuple(f"处理中 {index} cookie=SECRETCOOKIEVALUE1234567890" for index in range(30))
    cli = write_fake_cli(tmp_path, mode="timeout", stderr=(secret, *noisy))
    controller = login_bridge.LoginController(
        cli_path_provider=lambda: cli,
        data_dir_provider=lambda: type("R", (), {"path": DATA_DIR})(),
    )
    view = controller.start(timeout_seconds=30)
    assert _wait_for(lambda: controller.get(view["login_id"])["status"] == "timeout")
    row = controller.get(view["login_id"])
    tail = row["stderr_tail"] or ""
    assert "SECRETCOOKIEVALUE1234567890" not in tail
    assert "ABCDEF0123456789ABCDEF0123456789" not in tail
    assert "[REDACTED" in tail
    assert len(tail.split("\n")) <= login_bridge.LOGIN_STDERR_MAX_LINES
    assert len(tail) <= login_bridge.LOGIN_STDERR_MAX_CHARS
    # login_task 整表无任何凭据字段值
    blob = json.dumps(query_one("SELECT * FROM login_task WHERE id=?", (view["login_id"],)),
                      ensure_ascii=False, default=str)
    assert "SECRETCOOKIEVALUE" not in blob


# ---------------------------------------------------------------------------
# 回归两则：
# 1) 子进程提前退出 → 立刻收敛（绝不等到总超时），按「退出码 + 凭证是否落盘」决定去向
# 2) auth_state=partial → 仍是「已连接」，只降级提示
# ---------------------------------------------------------------------------

def settle_credentials(root: Path, *, name: str = "upstream") -> Path:
    """造一份“凭证已落盘”的 data-dir（只写占位字节，绝不使用或生成真实凭证）。"""
    auth_dir = root / name / "auth"
    auth_dir.mkdir(parents=True, exist_ok=True)
    (auth_dir / "salt").write_bytes(b"0" * 16)
    (auth_dir / "session.enc").write_bytes(b"0" * 64)
    return root / name


def _probe_of(auth_state: str) -> dict:
    """按真实 `status --live` 形状构造探针结果（去掉 user_name 等个人信息字段）。"""
    payload = {key: value for key, value in status_payload(auth_state).items()
               if key != "user_name"}
    payload["ok"] = True
    return payload


@pytest.mark.parametrize("auth_state,expected_status", [
    ("complete", "connected"),
    ("partial", "connected_degraded"),
])
def test_process_dies_early_with_credentials_probes_live(tmp_path: Path, auth_state: str,
                                                        expected_status: str) -> None:
    """进程被环境打断（无信封、异常退出码），但凭证已落盘 → 立刻转在线复核，不等超时。"""
    data_dir = settle_credentials(tmp_path)
    cli = write_fake_cli(tmp_path, die=True, stderr=("检测到登录成功，正在提取凭证...",))
    controller = login_bridge.LoginController(
        cli_path_provider=lambda: cli,
        data_dir_provider=lambda: type("R", (), {"path": data_dir})(),
        live_probe=lambda *_args, **_kw: _probe_of(auth_state),
    )
    started = time.monotonic()
    # 超时故意设到允许上限：若仍依赖总超时才转状态，这条用例必然失败
    view = controller.start(timeout_seconds=600)
    assert _wait_for(lambda: controller.get(view["login_id"])["status"] == expected_status,
                     timeout=25), controller.get(view["login_id"])["status"]
    elapsed = time.monotonic() - started
    assert elapsed < 60, f"进程退出后仍等了 {elapsed:.1f}s 才收敛"
    row = controller.get(view["login_id"])
    assert row["live_verified_at"]
    assert row["active"] == 0
    assert row["exit_code"], "异常退出码必须落库以便诊断"
    assert row["exit_code"] != 0
    assert data_dir.joinpath("auth", "session.enc").exists()


def test_process_dies_early_without_credentials_is_explicit_failure(tmp_path: Path) -> None:
    """进程提前退出且没有任何凭证落盘 → 明确失败态，带退出码与失败原因。"""
    empty_dir = tmp_path / "no-auth"
    empty_dir.mkdir(parents=True, exist_ok=True)
    cli = write_fake_cli(tmp_path, die=True, stderr=("正在启动浏览器...",))
    calls: list[str] = []

    def probe(*_args, **_kwargs) -> dict:
        calls.append("probe")
        return {"ok": False, "error_code": "UPSTREAM_PROCESS_FAILED", "detail": "无法启动 CLI"}

    controller = login_bridge.LoginController(
        cli_path_provider=lambda: cli,
        data_dir_provider=lambda: type("R", (), {"path": empty_dir})(),
        live_probe=probe,
    )
    view = controller.start(timeout_seconds=600)
    assert _wait_for(lambda: controller.get(view["login_id"])["status"] == "failed",
                     timeout=25), controller.get(view["login_id"])["status"]
    row = controller.get(view["login_id"])
    assert row["error_code"]
    assert row["exit_code"] not in (0, None)
    assert "退出码" in (row["message"] or "")
    assert row["live_verified_at"] is None
    assert calls == [], "没有凭证落盘时不该做在线探测"
    user_text = controller.view(row)["user_message"]
    assert "auth" not in user_text.lower() and "partial" not in user_text.lower()


def test_wall_clock_timeout_with_credentials_still_probes_live(tmp_path: Path) -> None:
    """进程迟迟不退（用户已登录、进程卡住）→ 墙钟回收后同样以在线事实为准。"""
    data_dir = settle_credentials(tmp_path)
    cli = write_fake_cli(tmp_path, stderr=("检测到登录成功，正在提取凭证...",), sleep=30)
    controller = login_bridge.LoginController(
        cli_path_provider=lambda: cli,
        data_dir_provider=lambda: type("R", (), {"path": data_dir})(),
        live_probe=lambda *_a, **_k: {"ok": True, "logged_in": True, "live": True,
                                      "auth_state": "partial", "auth_summary": "degraded"},
        grace_seconds=0,
    )
    view = controller.start(timeout_seconds=5)
    assert _wait_for(lambda: controller.get(view["login_id"])["status"]
                     == "connected_degraded", timeout=20), \
        controller.get(view["login_id"])["status"]
    row = controller.get(view["login_id"])
    assert row["live_verified_at"]
    assert controller.view(row)["user_stage"] == "connected"


def test_partial_auth_state_is_connected_with_degraded_user_copy(tmp_path: Path) -> None:
    """主判据是 logged_in && live：partial → 已连接 + 降级提示 + 可行动作。"""
    cli = write_fake_cli(tmp_path, mode="ok")
    probe_payload = {"logged_in": True, "live": True, "auth_state": "partial",
                     "auth_summary": "degraded"}
    controller = login_bridge.LoginController(
        cli_path_provider=lambda: cli,
        data_dir_provider=lambda: type("R", (), {"path": DATA_DIR})(),
        live_probe=lambda *_a, **_k: {"ok": True, **probe_payload},
    )
    view = controller.start(timeout_seconds=30)
    assert _wait_for(lambda: controller.get(view["login_id"])["status"]
                     == "connected_degraded"), controller.get(view["login_id"])["status"]
    row = controller.get(view["login_id"])
    mapped = controller.view(row)
    assert mapped["user_stage"] == "connected"                  # 九态口径不变，前端无需改动
    assert "auth" not in mapped["user_message"].lower()
    assert "auth_state" not in mapped["user_message"].lower()
    assert "partial" not in mapped["user_message"].lower()
    assert "不完全" in mapped["user_message"] or "不完整" in mapped["user_message"]
    assert "重新登录" in mapped["user_message"]
    action_ids = [action["id"] for action in mapped["actions"]]
    assert action_ids == ["search", "login"]
    assert mapped["can"]["search"] is True                      # 采集照常允许
    assert controller.status_view()["state"] == "connected"      # 顶栏不再是「未连接」
    assert row["user_message"] == mapped["user_message"]


def test_logged_in_false_is_still_not_connected(tmp_path: Path) -> None:
    """logged_in=false → 未连接；live=true 也救不回来。"""
    cli = write_fake_cli(tmp_path, mode="ok")
    controller = login_bridge.LoginController(
        cli_path_provider=lambda: cli,
        data_dir_provider=lambda: type("R", (), {"path": DATA_DIR})(),
        live_probe=lambda *_a, **_k: {"ok": True, "logged_in": False, "live": True,
                                      "auth_state": "missing", "auth_summary": "missing"},
    )
    view = controller.start(timeout_seconds=30)
    assert _wait_for(lambda: controller.get(view["login_id"])["status"] == "failed",
                     timeout=15)
    row = controller.get(view["login_id"])
    assert row["live_verified_at"] is None
    assert controller.view(row)["user_stage"] != "connected"
    assert controller.status_view()["state"] != "connected"


@pytest.mark.parametrize("probe,verdict", [
    ({"ok": True, "logged_in": True, "live": True, "auth_state": "complete"}, "connected"),
    ({"ok": True, "logged_in": True, "live": True, "auth_state": "partial"},
     "connected_degraded"),
    ({"ok": True, "logged_in": True, "live": True, "auth_summary": "degraded"},
     "connected_degraded"),
    ({"ok": True, "logged_in": False, "live": True, "auth_state": "missing"}, "not_connected"),
    ({"ok": True, "logged_in": True, "live": False, "auth_state": "partial"}, "not_connected"),
    ({"ok": True, "logged_in": True, "live": True, "auth_state": "broken"}, "not_connected"),
    ({"ok": True, "auth_state": "complete"}, "connected"),            # 旧形探针兜底
    ({"ok": True, "auth_state": "partial"}, "not_connected"),          # 旧形探针拿不到在线事实
    ({"ok": False, "logged_in": True, "live": True}, "not_connected"),
    ({}, "not_connected"),
])
def test_classify_login_probe(probe: dict, verdict: str) -> None:
    assert login_bridge.classify_login_probe(probe) == verdict


def test_live_probe_timing_is_immediate_then_spaced(tmp_path: Path) -> None:
    """在线复核既不提前也不延后：首次探测立即发出，两次之间必须 ≥5s。"""
    cli = write_fake_cli(tmp_path, stderr=("请扫码或手机号登录（超时 8 秒）...",), sleep=6)
    stamps: list[float] = []

    def probing(*_args, **_kwargs) -> dict:
        stamps.append(time.monotonic())
        return {"ok": True, "logged_in": True, "live": True, "auth_state": "complete"}

    controller = login_bridge.LoginController(
        cli_path_provider=lambda: cli,
        data_dir_provider=lambda: type("R", (), {"path": DATA_DIR})(),
        live_probe=probing,
    )
    view = controller.start(timeout_seconds=30)
    assert _wait_for(lambda: controller.get(view["login_id"])["status"] == "waiting_user")
    triggered = time.monotonic()
    controller._verify_live(view["login_id"])      # 直接触发一次复核：不得有前置等待
    assert stamps, "在线复核没有真正发出探测"
    assert stamps[0] - triggered < 2.0, f"首次探测被延后了 {stamps[0] - triggered:.1f}s"
    assert controller.get(view["login_id"])["status"] == "connected"

    # 两次探测的间隔仍必须 ≥5s（失败路径才会走第二次）
    stamps.clear()
    failing = login_bridge.LoginController(
        cli_path_provider=lambda: cli,
        data_dir_provider=lambda: type("R", (), {"path": DATA_DIR})(),
        live_probe=lambda *_a, **_k: stamps.append(time.monotonic()) or {
            "ok": True, "logged_in": False, "live": False, "auth_state": "missing"},
    )
    view = failing.start(timeout_seconds=30)
    assert _wait_for(lambda: failing.get(view["login_id"])["status"] == "failed", timeout=30)
    assert len(stamps) == login_bridge.LOGIN_LIVE_PROBE_ATTEMPTS
    assert stamps[-1] - stamps[0] >= login_bridge.LOGIN_LIVE_PROBE_INTERVAL_SECONDS - 1


def test_missing_cli_raises_dependency_missing() -> None:
    controller = login_bridge.LoginController(cli_path_provider=lambda: None)
    with pytest.raises(login_bridge.LoginError) as raised:
        controller.start()
    assert raised.value.code == "DEPENDENCY_MISSING"


def test_timeout_bounds_for_api() -> None:
    assert login_bridge.timeout_bounds_error(180) is None
    assert login_bridge.timeout_bounds_error(29) is not None
    assert login_bridge.timeout_bounds_error(601) is not None


# ---------------------------------------------------------------------------
# T02：HTTP 接口层（真实子进程 + 假 CLI）
# ---------------------------------------------------------------------------

@pytest.fixture
def api_client(tmp_path: Path):
    marker = tmp_path / "invocations.log"
    cli = write_fake_cli(tmp_path, mode="ok", auth_state="complete", marker=marker)
    store.execute("INSERT INTO app_setting (key, value) VALUES ('boss_cli_path', ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (cli,))
    with TestClient(app_module.app, raise_server_exceptions=False) as client:
        yield client, marker


def test_auth_api_login_status_cancel_flow(api_client) -> None:
    client, marker = api_client
    started = client.post("/api/auth/login", json={"timeout_seconds": 180})
    assert started.json()["ok"], started.text
    payload = started.json()["data"]
    assert payload["login"]["user_stage"] in {"starting", "verifying", "connected"}
    login_id = payload["login"]["login_id"]

    status = client.get("/api/auth/status").json()["data"]["boss"]
    assert set(status) >= {"state", "user_stage", "user_message"}
    assert status["state"] in {"disconnected", "connecting", "connected"}
    assert "AUTH_REQUIRED" not in status["user_message"]

    # 轮询接口读到同一流程
    polled = client.get(f"/api/auth/login/{login_id}").json()
    assert polled["data"]["login"]["login_id"] == login_id

    # 等待在线复核完成（子进程冷启动在 Windows 上可能数秒）
    row = _poll_login(client, login_id)
    assert row["status"] == "connected"
    assert row["user_stage"] == "connected"
    assert row["live_verified_at"]

    # 重复点击返回同一流程（已连接后 active=0，会新建流程，但状态读数必须一致）
    after = client.get("/api/auth/status").json()["data"]["boss"]
    assert after["state"] == "connected"

    # 状态读取不触发任何新子进程：只有 1 次 login + 1 次 status(--live)
    invocations = [line for line in marker.read_text(encoding="utf-8").splitlines() if line]
    assert len(invocations) == 2, invocations

    # 已连接流程不能取消
    refused = client.post(f"/api/auth/login/{login_id}/cancel", json={}).json()
    assert refused["error"]["code"] == "TOOL_NOT_ALLOWED"

    missing = client.get("/api/auth/login/login_not_exist").json()
    assert missing["error"]["code"] == "INVALID_ANALYSIS"
    bad_timeout = client.post("/api/auth/login", json={"timeout_seconds": 1}).json()
    assert bad_timeout["error"]["code"] == "TOOL_NOT_ALLOWED"


def test_auth_api_no_credentials_in_any_response(api_client) -> None:
    client, marker = api_client
    payload = client.post("/api/auth/login", json={}).json()
    login_id = payload["data"]["login"]["login_id"]
    _poll_login(client, login_id)
    diagnostic = client.get(f"/api/auth/login/{login_id}?diagnostic=1").json()
    blob = json.dumps(diagnostic, ensure_ascii=False).lower()
    for forbidden in ("cookie", "stoken", "user_agent", "authorization", "api_key"):
        assert forbidden not in blob
    assert diagnostic["data"]["diagnostic"]["stderr_tail"] == ""


def test_auth_login_requires_cli() -> None:
    store.execute("INSERT INTO app_setting (key, value) VALUES ('boss_cli_path', ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (str(_TMP_DIR / "no-such-boss.exe"),))
    with TestClient(app_module.app, raise_server_exceptions=False) as client:
        response = client.post("/api/auth/login", json={}).json()
    assert response["error"]["code"] == "DEPENDENCY_MISSING"


# ---------------------------------------------------------------------------
# 真实登录链路冒烟（需显式开启；只验证桥接，不要求用户登录）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.environ.get("FININTEL_REAL_LOGIN_SMOKE") != "1",
                    reason="需要 FININTEL_REAL_LOGIN_SMOKE=1 才跑真实登录窗口")
def test_real_login_bridge_smoke() -> None:
    from upstream_config import resolve_cli_path, resolve_upstream_data_dir

    cli = resolve_cli_path(None)
    assert cli, "未找到 boss.exe"
    data_dir = resolve_upstream_data_dir()
    controller = login_bridge.LoginController(
        cli_path_provider=lambda: cli,
        data_dir_provider=lambda: data_dir,
        grace_seconds=20,
    )
    view = controller.start(timeout_seconds=8)
    login_id = view["login_id"]
    assert _wait_for(lambda: "已打开 BOSS 直聘登录页" in (controller.get(login_id)["stderr_tail"] or ""),
                     timeout=40), controller.get(login_id)["stderr_tail"]
    assert _wait_for(lambda: controller.get(login_id)["status"] in login_bridge.TERMINAL_STATUSES,
                     timeout=90), controller.get(login_id)["status"]
    row = controller.get(login_id)
    assert row["status"] == "timeout", row["status"]
    assert row["user_message"] == "本次登录未完成，关键词已保留"
    assert row["live_verified_at"] is None
    blob = json.dumps(row, ensure_ascii=False, default=str)
    assert "SECRETCOOKIE" not in blob
    assert "boss-chrome" not in blob
    assert "调试端口" not in (row["stderr_tail"] or "")
    print(f"\n[real-smoke] status={row['status']} error_code={row['error_code']}")
    print(f"[real-smoke] stderr_tail={row['stderr_tail']!r}")
