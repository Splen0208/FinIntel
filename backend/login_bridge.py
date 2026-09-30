"""受控登录桥接：把上游真实支持的交互登录封装成一个网页可引导的流程。

设计依据：`docs/simple-flow-design.md` §5（登录桥接）与 §6（状态机）。

铁律
----
1. 登录命令唯一出口是 `upstream_config.build_login_argv()`，形状硬编码。
2. 子进程退出码 0 **不算**已连接；必须再跑一次 `status --live`，且以
   `logged_in && live` 为准；`auth_state` 只描述凭证质量（`partial` → 已连接但降级）。
   窗口关闭 ≠ 成功，凭证不完整 ≠ 未连接。
3. 单次登录流程最多 2 次在线探测、间隔 ≥5s；任务轮询接口永不触发在线探测。
4. 并发唯一：`login_task(active) WHERE active=1` 唯一索引；重复点击返回同一流程。
   等待循环一律非阻塞 `poll()`：子进程一旦退出立即收敛，禁止只靠总超时转换状态；
   收尾按「退出码 + 凭证是否已落盘」两条线索决定去向，退出码必须落库以便诊断。
5. 凭据红线：cookie/stoken/user_agent 绝不入库、不返回前端、不写日志；
   stderr 每行都过 `redact()` 且整段定长截断（≤12 行 / ≤2000 字符）。
6. `boss.exe` 子进程用 `CREATE_NO_WINDOW` 隐藏控制台；patchright 拉起的 Chromium 是
   独立 GUI 进程，**必须可见**，不得被任何隐藏窗口的包装包住。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import user_view
import store
import channel_state
from store import get_conn, new_id, query_one, utcnow_iso
from upstream_config import (
    LOGIN_DEFAULT_TIMEOUT_SECONDS,
    LOGIN_TIMEOUT_BOUNDS,
    PROJECT_DIR,
    build_login_argv,
    redact,
    resolve_cli_path,
    resolve_upstream_data_dir,
    run_upstream,
    sanitized_child_env,
)
from upstream_envelope import UpstreamError, parse_envelope

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

LOGIN_STDERR_MAX_LINES = 12
LOGIN_STDERR_MAX_CHARS = 2000
LOGIN_WALL_CLOCK_GRACE_SECONDS = 60
LOGIN_LIVE_PROBE_ATTEMPTS = 2
LOGIN_LIVE_PROBE_INTERVAL_SECONDS = 5
LOGIN_LIVE_PROBE_TIMEOUT_SECONDS = 60
# 等待子进程退出的轮询间隔：非阻塞 poll，绝不整段阻塞到总超时
LOGIN_POLL_INTERVAL_SECONDS = 0.2
# 杀进程后给内核回收句柄的兜底等待（正常情况下第一次 poll 就返回）
LOGIN_REAP_TIMEOUT_SECONDS = 5
# stdout 读取线程的最长等待：宁可放弃 stdout，也不让收尾被句柄挂住
LOGIN_STREAM_JOIN_SECONDS = 5

ACTIVE_STATUSES = frozenset({"starting", "waiting_user", "verifying"})
# 「已连接」的两种落地状态：凭证完整 / 凭证不完整但仍能真实取数（partial）
CONNECTED_STATUSES = frozenset({"connected", "connected_degraded"})
TERMINAL_STATUSES = frozenset({
    "connected", "connected_degraded", "timeout", "cancelled", "platform_verification",
    "env_not_ready", "failed",
})

# 在线复核的三类判定（只描述结果，不决定文案；文案一律由 user_view 出）
PROBE_CONNECTED = "connected"
PROBE_CONNECTED_DEGRADED = "connected_degraded"
PROBE_NOT_CONNECTED = "not_connected"

# `status --live` 的凭证质量标签：主判据永远是 logged_in && live
AUTH_STATE_COMPLETE = "complete"
AUTH_STATE_PARTIAL = "partial"
# 这两个才是真正的「没连上」：凭证缺失/损坏
NOT_CONNECTED_AUTH_STATES = frozenset({"missing", "broken"})
DEGRADED_AUTH_STATES = frozenset({AUTH_STATE_PARTIAL})
DEGRADED_AUTH_SUMMARIES = frozenset({"degraded"})

# 判断是否已落盘的上游凭证文件：只看文件名与大小，绝不读取内容
CREDENTIAL_FILE_NAMES = ("session.enc", "salt")
CREDENTIAL_SUBDIR = "auth"

# 上游 error.code → 登录终态（设计 §6 第 6/7/8 行）
PLATFORM_VERIFICATION_CODES = frozenset({
    "PLATFORM_VERIFICATION_REQUIRED", "LOGIN_RISK_CONTROL",
    "ENVIRONMENT_RISK", "ACCOUNT_RISK", "STOPPED_BY_SAFETY",
})
ENV_NOT_READY_CODES = frozenset({
    "DEPENDENCY_MISSING", "BROWSER_KERNEL_MISSING", "CDP_UNAVAILABLE",
    "LOGIN_CREDENTIAL_EXTRACTION_FAILED", "UPSTREAM_BROWSER_UNAVAILABLE",
    "DATA_DIR_MISMATCH",
})
LOGIN_TIMEOUT_CODES = frozenset({"LOGIN_TIMEOUT", "UPSTREAM_LOGIN_TIMEOUT", "UPSTREAM_TIMEOUT"})

# stderr 提示性推进（设计 §5.3，绝不用它判定「已连接」）
_STDERR_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("正在启动", "launching", "尝试从本地浏览器提取 Cookie"), "starting"),
    (("已打开 BOSS 直聘登录页", "请扫码或手机号登录", "请在 Chrome 中扫码登录，等待中",
      "[boss] 等待扫码中", "请在已打开的智联页签中完成登录"), "waiting_user"),
    (("[boss] 已扫码，等待确认", "[boss] 扫码确认成功", "检测到登录成功，正在提取凭证"), "verifying"),
    (("扫码登录超时", "CDP 扫码登录超时", "TimeoutError", "timeout"), "timeout"),
)
_STAGE_BY_STATUS = {
    "starting": "launching",
    "waiting_user": "opened",
    "verifying": "verifying",
    "timeout": "timeout",
}


class LoginError(Exception):
    """稳定的登录层错误（与 runs.RunError 同形，便于 app.py 统一翻译）。"""

    def __init__(self, code: str, message: str, retryable: bool = False,
                 operator_action: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.operator_action = operator_action


def classify_stderr_line(line: str) -> Optional[str]:
    """只做提示性状态推进；返回状态名或 None。"""
    text = line or ""
    for patterns, status in _STDERR_RULES:
        if any(pattern in text for pattern in patterns):
            return status
    return None


def classify_error(code: Optional[str]) -> tuple[str, Optional[str]]:
    """上游错误码 → (login_task.status, error_code)。"""
    upstream_code = (code or "").upper()
    if upstream_code in PLATFORM_VERIFICATION_CODES:
        return "platform_verification", upstream_code
    if upstream_code in ENV_NOT_READY_CODES:
        return "env_not_ready", upstream_code
    if upstream_code in LOGIN_TIMEOUT_CODES:
        return "timeout", upstream_code
    return "failed", upstream_code or None


def credential_settled(data_dir: Optional[str]) -> bool:
    """上游凭证是否已落盘：只看 auth 目录下的文件名与大小，**绝不读取内容**。

    这是「进程不在了，但用户其实已经登录成功」的唯一本地证据。
    """
    if not data_dir:
        return False
    auth_dir = Path(str(data_dir)) / CREDENTIAL_SUBDIR
    try:
        return all(
            (auth_dir / name).is_file() and (auth_dir / name).stat().st_size > 0
            for name in CREDENTIAL_FILE_NAMES
        )
    except OSError:
        return False


def classify_login_probe(probe: dict) -> str:
    """按「能不能真的拿到数据」判定一次 `status --live` 的结果。

    返回 `PROBE_CONNECTED` / `PROBE_CONNECTED_DEGRADED` / `PROBE_NOT_CONNECTED`：

    - `logged_in` 与 `live` 同时成立 = 真的能取数 = 已连接（主判据）；
    - `auth_state` 只描述凭证质量：`partial`/`summary=degraded` → 已连接但要降级提示；
      `missing`/`broken` → 未连接；
    - 探针本身失败或拿不到在线事实 → 未连接（绝不因为「没报错」就判成功）。
    """
    if not isinstance(probe, dict) or not probe.get("ok"):
        return PROBE_NOT_CONNECTED
    if "logged_in" in probe or "live" in probe:
        logged_in = bool(probe.get("logged_in"))
        live = bool(probe.get("live"))
    else:
        # 旧形探针只带 auth_state：仍用 auth_state 兜底，保持既有判定口径不变
        logged_in = live = str(probe.get("auth_state") or "") == AUTH_STATE_COMPLETE
    if not (logged_in and live):
        return PROBE_NOT_CONNECTED
    auth_state = str(probe.get("auth_state") or "")
    summary = str(probe.get("auth_summary") or "").strip().lower()
    if auth_state in NOT_CONNECTED_AUTH_STATES:
        return PROBE_NOT_CONNECTED
    if auth_state in DEGRADED_AUTH_STATES or summary in DEGRADED_AUTH_SUMMARIES:
        return PROBE_CONNECTED_DEGRADED
    return PROBE_CONNECTED


def _probe_digest(probe: Optional[dict]) -> str:
    """给诊断页用的一行事实摘要（脱敏，绝不含任何凭证值）。"""
    probe = probe or {}
    return redact(
        f"logged_in={probe.get('logged_in')} live={probe.get('live')} "
        f"auth_state={probe.get('auth_state')} auth_summary={probe.get('auth_summary')}"
    )[:300]


# ---------------------------------------------------------------------------
# 控制器
# ---------------------------------------------------------------------------

class LoginController:
    """受控登录子进程 + 持久化登录状态机。"""

    def __init__(
        self,
        *,
        cli_path_provider: Optional[Callable[[], Optional[str]]] = None,
        data_dir_provider: Optional[Callable[[], Any]] = None,
        spawn: Optional[Callable[[list[str]], Any]] = None,
        live_probe: Optional[Callable[[str, Any, int], dict[str, Any]]] = None,
        grace_seconds: int = LOGIN_WALL_CLOCK_GRACE_SECONDS,
        on_connected: Optional[Callable[[], None]] = None,
    ) -> None:
        self._cli_path_provider = cli_path_provider or _default_cli_path
        self._data_dir_provider = data_dir_provider or resolve_upstream_data_dir
        self._spawn = spawn or _default_spawn
        self._live_probe = live_probe or _default_live_probe
        self._grace_seconds = grace_seconds
        self.on_connected = on_connected
        self.browser_provider = None  # Enabled by the real Windows server, never by offline tests.
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._processes: dict[str, Any] = {}

    # -- 查询 ---------------------------------------------------------------

    def active(self) -> Optional[dict]:
        """当前活跃登录流程（只读 DB）。"""
        return query_one(
            "SELECT * FROM login_task WHERE active=1 ORDER BY started_at DESC LIMIT 1"
        )

    def latest(self) -> Optional[dict]:
        return query_one("SELECT * FROM login_task ORDER BY started_at DESC LIMIT 1")

    def get(self, login_id: str) -> dict:
        row = query_one("SELECT * FROM login_task WHERE id=?", (login_id,))
        if not row:
            raise LoginError("INVALID_ANALYSIS", f"登录流程 {login_id} 不存在")
        return row

    def view(self, row: dict) -> dict:
        """普通 UI 视图：只含文案与操作，不含任何凭据。"""
        mapped = user_view.user_view(row["status"], self._boss_state(row), row.get("error_code"))
        return {
            "login_id": row["id"],
            "status": row["status"],
            "stage": row.get("stage"),
            "user_stage": mapped["user_stage"],
            "user_message": mapped["user_message"],
            "actions": mapped["actions"],
            "can": mapped["can"],
            "live_verified_at": row.get("live_verified_at"),
            "started_at": row.get("started_at"),
            "updated_at": row.get("updated_at"),
            "finished_at": row.get("finished_at"),
        }

    def diagnostic_view(self, login_id: str) -> dict:
        """技术诊断页视图：含脱敏后的 stderr 尾部与内部错误码。"""
        row = self.get(login_id)
        return {
            "login_id": row["id"],
            "status": row["status"],
            "stage": row.get("stage"),
            "error_code": row.get("error_code"),
            "message": row.get("message"),
            "stderr_tail": row.get("stderr_tail") or "",
            "data_dir": row.get("data_dir"),
            "cli_path": row.get("cli_path"),
            "pid": row.get("pid"),
            "exit_code": row.get("exit_code"),
            "timeout_seconds": row.get("timeout_seconds"),
        }

    def status_view(self) -> dict:
        """首页顶栏用的一个状态（只读 DB，绝不触发在线探测）。

        把两个事实**合起来读**（设计 §2.4/§2.5）：

        - ``state``：账号事实（语义不变，仍是「账号曾验证成功」）；
        - ``account_state``：**未被打扮过的账号事实**（``connected`` / ``connecting`` /
          ``disconnected``）。顶栏要做「账号 × 通道」两维判定时只能用它——``user_stage``
          会被通道未就绪的打扮覆盖成 ``env_not_ready``，拿它当账号事实会得出错误结论；
        - ``channel``：当前采集通道的就绪状态（未知/就绪/未就绪）；
        - ``collect_ready``：``channel_state.collect_gate(...)``——搜索前置检查用的
          判定，不再用「曾登录成功」顶替「通道可用」；
        - ``channel_ready``：``channel.state == 'ready'``——顶栏绿点**唯一**依据。
          ``collect_ready`` 对 ``unknown``（放行一次探测）与 ``existing-browser``
          的 24h 兜底都可能为真，**不等于**「已经就绪」，顶栏绝不能拿它画绿点。
        """
        import channel_state

        channel = channel_state.channel_view()
        policy = channel_state.current_policy()
        row = self.latest()
        if not row:
            mapped = user_view.user_view(None, "disconnected", None)
            return {
                "state": "disconnected", "login": None, **mapped,
                "channel": channel, "collect_ready": False, "channel_ready": False,
                "account_state": "disconnected",
                "live_verified_at": None,
            }
        with_active = self.active() or row
        boss_state = self._boss_state(with_active)
        collect_ready = channel_state.collect_gate(boss_state, channel, policy)
        channel_ready = channel.get("state") == channel_state.STATE_READY
        mapped = user_view.user_view(
            with_active["status"], boss_state, with_active.get("error_code")
        )
        if (policy.source == channel_state.SOURCE_EXISTING_BROWSER and collect_ready
                and channel.get("check_source") == channel_state.CHECK_COLLECT_OK
                and channel_state.RUNTIME._collect_fresh(channel)):
            mapped = {"user_stage":"connected", "user_message":"BOSS 已连接，可搜索真实岗位",
                      "actions":[{"id":"search", "label":"开始搜索"}]}
        if boss_state == "connected" and not channel_ready:
            # 通道**没有确凿就绪**就不能显示成「已连接可用」。
            # 三处（顶栏 / 任务页 / 结果页）对**同一次风控**必须讲同一句话：若通道最近
            # 一次真实失败是「访问环境风控」，就不能只说成「连接尚未就绪 / 正在确认」——
            # 那是通道口径，会把**真正的原因（环境被拦截）**说没了。
            #
            # ``channel_state.last_error_code`` 与 ``research_run.stop_reason`` 同源同口径：
            # 都取自 ``upstream_envelope.map_upstream_error`` 归一后的 ``error.code``
            # （见 ``refine_risk_code``），因此这里按 ``last_error_code`` 判定即可，
            # 不会出现「任务页说环境风控、顶栏说连接未就绪」的两说。
            last_error = str(channel.get("last_error_code") or "")
            if last_error == "ENVIRONMENT_RISK":
                mapped = user_view.environment_blocked_view()
            elif channel.get("state") == channel_state.STATE_NOT_READY:
                # ``not_ready`` → 如实说「连接尚未就绪」；``unknown``（尚未确认）→
                # 如实说「正在确认搜索通道」。绝不说成「你没有登录」。
                mapped = user_view.channel_not_ready_view()
            else:
                mapped = user_view.channel_confirming_view()
        return {
            "state": self._boss_state(row),
            "account_state": boss_state,
            "login": self.view(row) if row["status"] in ACTIVE_STATUSES else None,
            **mapped,
            "channel": channel,
            "collect_ready": bool(collect_ready),
            "channel_ready": bool(channel_ready),
            "live_verified_at": row.get("live_verified_at"),
        }

    # -- 启动 / 取消 / 重开 --------------------------------------------------

    def recover_pending(self) -> None:
        """A restarted server cannot keep claiming its old login worker is running."""
        row = self.active()
        if not row:
            return
        with self._lock:
            thread = self._threads.get(row["id"])
            if thread and thread.is_alive():
                return
        # Never kill an old stored PID: Windows may already have reused it.
        self._finalize(row["id"], "cancelled", "LOGIN_INTERRUPTED",
                       "服务已重启，请再次点击登录 BOSS；专用浏览器中的登录状态会保留")

    def start(self, timeout_seconds: int = LOGIN_DEFAULT_TIMEOUT_SECONDS) -> dict:
        """启动一次登录流程；已存在活跃流程时原样返回同一流程。"""
        existing = self.active()
        if existing:
            return self.view(existing)

        cli_path = self._cli_path_provider()
        if not cli_path:
            raise LoginError(
                "DEPENDENCY_MISSING",
                "未找到 boss CLI，无法启动官方登录页面",
                False,
                "由开发或部署方安装 boss-agent-cli==2.0.0 后重试",
            )
        data_dir = self._data_dir_provider()
        try:
            argv = build_login_argv(cli_path, data_dir.path, timeout_seconds=timeout_seconds)
        except UpstreamError as exc:
            raise LoginError(exc.code, exc.message, exc.retryable, exc.operator_action) from exc

        login_id = new_id("login")
        now = utcnow_iso()
        connection = get_conn()
        try:
            connection.execute(
                """INSERT INTO login_task
                   (id,status,stage,data_dir,cli_path,pid,timeout_seconds,message,stderr_tail,
                    error_code,user_message,active,started_at,updated_at,finished_at,live_verified_at)
                   VALUES (?,?,?,?,?,NULL,?,NULL,'',NULL,?,1,?,?,NULL,NULL)""",
                (login_id, "starting", "launching", str(data_dir.path), cli_path,
                 int(timeout_seconds), user_view.LOGIN_MESSAGES["starting"], now, now),
            )
            connection.commit()
        except Exception:
            # 并发唯一索引冲突：另一请求已建流程，返回既有的那一行
            existing = self.active()
            if existing:
                return self.view(existing)
            raise

        thread = threading.Thread(
            target=self._run, args=(login_id, argv, int(timeout_seconds)),
            name=f"login-{login_id}", daemon=True,
        )
        with self._lock:
            self._threads[login_id] = thread
        thread.start()
        return self.view(self.get(login_id))

    def cancel(self, login_id: str, reason: str = "用户取消登录") -> dict:
        row = self.get(login_id)
        if row["status"] in TERMINAL_STATUSES and row["status"] != "connected":
            raise LoginError("TOOL_NOT_ALLOWED", f"登录流程已处于终态 {row['status']}，无需取消")
        if row["status"] == "connected":
            raise LoginError("TOOL_NOT_ALLOWED", "登录已连接，无需取消")
        self._kill(login_id, "cancelled", None, reason)
        return self.view(self.get(login_id))

    def reopen(self, login_id: str) -> dict:
        """取消并重开 = 新流程（窗口被用户关掉后的唯一出口）。"""
        self.get(login_id)
        self._kill(login_id, "cancelled", None, "用户重新打开登录页面")
        return self.start()

    # -- 内部：进程与状态 ----------------------------------------------------

    def _run(self, login_id: str, argv: list[str], timeout_seconds: int) -> None:
        try:
            if self.browser_provider is not None:
                import managed_browser
                endpoint = self.browser_provider()
                argv = managed_browser.login_argv(argv, endpoint)
                channel = channel_state.freeze_channel()
                self._update(login_id, channel_snapshot=store.jdumps(channel))
                # Cancelling while Chrome was starting must not spawn a login worker afterward.
                if self.get(login_id)["status"] not in ACTIVE_STATUSES:
                    return
            process = self._spawn(argv)
        except (OSError, ValueError) as exc:
            self._finalize(login_id, "env_not_ready", "DEPENDENCY_MISSING",
                           redact(f"{type(exc).__name__}: {exc}")[:500])
            return
        with self._lock:
            self._processes[login_id] = process
        self._update(login_id, pid=process.pid)
        pumper = threading.Thread(
            target=self._consume, args=(login_id, process), daemon=True,
            name=f"login-stderr-{login_id}",
        )
        pumper.start()
        # stdout 也用独立线程边跑边读：句柄被子孙进程持有、迟迟收不到 EOF 时，
        # 主线程也不会被 `read()` 永久挂住，收尾永远是「有界」的
        stdout_chunks: list[bytes] = []
        reader = threading.Thread(
            target=self._read_stream, args=(process.stdout, stdout_chunks), daemon=True,
            name=f"login-stdout-{login_id}",
        )
        reader.start()

        # 非阻塞轮询：子进程一退出就立刻收敛，绝不靠总超时才转状态
        exited = self._await_exit(
            process, time.monotonic() + max(1, timeout_seconds) + max(0, self._grace_seconds)
        )
        if not exited:
            _kill_tree(process)
            # 回收后再补一次短轮询拿到退出码（正常会在第一次 poll 就返回）
            self._await_exit(process, time.monotonic() + LOGIN_REAP_TIMEOUT_SECONDS)

        with self._lock:
            # 进程已不在运行：登记表里不再保留它的句柄（登记表只装「还活着」的流程）
            self._processes.pop(login_id, None)
            self._threads.pop(login_id, None)

        reader.join(timeout=LOGIN_STREAM_JOIN_SECONDS)
        pumper.join(timeout=2)
        stdout_raw = b"".join(stdout_chunks)

        returncode = process.returncode if isinstance(process.returncode, int) else -1
        self._record_exit(login_id, returncode)

        row = query_one("SELECT status, data_dir FROM login_task WHERE id=?", (login_id,))
        if not row or row["status"] not in ACTIVE_STATUSES:
            return  # 已被取消或在终态，不再覆盖

        if not exited:
            # 墙钟到点、进程已被回收：凭证若已落盘仍要以在线事实为准，不能直接判失败
            if credential_settled(row["data_dir"]):
                self._update(login_id, status="verifying", stage="verifying",
                             message="登录窗口等待到时，检测到凭证已落盘，正在确认连接")
                self._verify_live(
                    login_id,
                    fallback=("timeout", "UPSTREAM_LOGIN_TIMEOUT", "登录等待超过时限"),
                )
                return
            self._finalize(login_id, "timeout", "UPSTREAM_LOGIN_TIMEOUT", "登录等待超过时限")
            return
        self._finish_from_envelope(login_id, returncode, stdout_raw, row["data_dir"])

    def _await_exit(self, process: Any, deadline: float) -> bool:
        """非阻塞轮询子进程是否退出；返回 True 表示已退出（拿到退出码）。

        不用 `process.wait(timeout=...)`：那会把整段等待交给一次系统调用，
        一旦子进程被环境打断或句柄回收异常，任务就会卡在 verifying 直到总超时。
        """
        while True:
            if process.poll() is not None:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(LOGIN_POLL_INTERVAL_SECONDS, remaining))

    def _record_exit(self, login_id: str, returncode: int) -> None:
        """把上游登录进程的真实退出码落库（诊断页唯一能区分「没登录」与「进程死了」的字段）。"""
        connection = get_conn()
        connection.execute(
            "UPDATE login_task SET exit_code=?, updated_at=? WHERE id=?",
            (int(returncode), utcnow_iso(), login_id),
        )
        connection.commit()

    @staticmethod
    def _read_stream(stream: Any, sink: list[bytes]) -> None:
        """后台线程读 stdout：句柄被子孙进程持有时也不能拖住主流程的收尾。"""
        if stream is None:
            return
        try:
            for chunk in iter(lambda: stream.read(4096), b""):
                sink.append(chunk)
        except (OSError, ValueError):
            return

    def _consume(self, login_id: str, process: Any) -> None:
        """逐行读取 stderr：脱敏 + 定长截断 + 提示性状态推进。"""
        stream = process.stderr
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                self._record_line(login_id, redact(line)[:400])
        except (OSError, ValueError):
            return

    def _record_line(self, login_id: str, safe_line: str) -> None:
        """stderr 只作提示：写入脱敏后的定长尾巴，并在活跃态内推进 stage/status。"""
        connection = get_conn()
        row = connection.execute(
            "SELECT stderr_tail FROM login_task WHERE id=?", (login_id,)
        ).fetchone()
        if not row:
            return
        tail = _append_tail(row["stderr_tail"] or "", safe_line)
        status = classify_stderr_line(safe_line)
        now = utcnow_iso()
        if status in {"starting", "waiting_user", "verifying"}:
            connection.execute(
                """UPDATE login_task SET status=?, stage=?, message=?, user_message=?,
                     stderr_tail=?, updated_at=?
                   WHERE id=? AND status IN ('starting','waiting_user','verifying')""",
                (status, _STAGE_BY_STATUS.get(status), safe_line,
                 user_view.LOGIN_MESSAGES.get(status), tail, now, login_id),
            )
        else:
            connection.execute(
                """UPDATE login_task SET message=?, stderr_tail=?, updated_at=?
                   WHERE id=? AND status IN ('starting','waiting_user','verifying')""",
                (safe_line, tail, now, login_id),
            )
        connection.commit()

    def _finish_from_envelope(self, login_id: str, returncode: int, stdout_raw: bytes,
                              data_dir: str) -> None:
        """按「退出码 + 凭证是否已落盘」两条线索给登录流程收尾。

        凭证已落盘 → 立即做在线复核，以 `logged_in && live` 的事实为准；
        没落盘 → 明确失败态，退出码/上游错误码/stderr 尾部都留在 login_task 里可查。
        """
        stdout = stdout_raw.decode("utf-8", errors="replace") if stdout_raw else ""
        invocation = _RawInvocationLike(stdout=stdout, returncode=returncode,
                                        stderr="", data_dir=data_dir)
        envelope = None
        upstream_code = ""
        detail = ""
        try:
            envelope = parse_envelope(invocation)
        except UpstreamError as exc:
            upstream_code, detail = exc.code, exc.message
        if envelope is not None:
            if envelope.ok:
                self._update(login_id, status="verifying", stage="verifying",
                             message="登录已提交，正在确认连接")
                self._verify_live(login_id)
                return
            upstream_code = str((envelope.error or {}).get("code") or upstream_code or "")
            detail = redact(str((envelope.error or {}).get("message") or detail or upstream_code))

        settled = credential_settled(data_dir)
        # 风控/验证类与环境缺失类不抢跑在线探测：前者要人来介入，后者再探测也没有意义
        no_probe_codes = PLATFORM_VERIFICATION_CODES | ENV_NOT_READY_CODES
        if settled and upstream_code.upper() not in no_probe_codes:
            # 进程已退出（可能是被环境打断），但凭证确实落盘了：
            # 不要替用户下结论，立刻用在线事实判断（≤2 次探测）。
            self._update(login_id, status="verifying", stage="verifying",
                         message=f"登录进程已退出（退出码 {returncode}），凭证已落盘，正在确认连接")
            self._verify_live(
                login_id,
                fallback=classify_error(upstream_code or f"EXIT_{returncode}"),
            )
            return
        status, error_code = classify_error(upstream_code or f"EXIT_{returncode}")
        reason = detail or upstream_code or "上游登录进程未返回可用结果"
        self._finalize(login_id, status, error_code,
                       f"{reason}（退出码 {returncode}，未见凭证落盘；详见本页 stderr 尾部）")

    def _verify_live(self, login_id: str,
                     fallback: Optional[tuple[str, Optional[str], str]] = None) -> None:
        """在线复核：最多 2 次、间隔 ≥5s；只有 `logged_in && live` 才算已连接。

        调用时机只在「子进程已退出之后」（含被墙钟回收），既不提前也不延后；
        `fallback` 是登录层已经拿到的失败结论，在线复核同样失败时用它在用户面前
        说清「为什么没登上」，但绝不改写在线事实。
        """
        row = query_one("SELECT cli_path, data_dir, status, channel_snapshot FROM login_task WHERE id=?",
                        (login_id,))
        if not row or row["status"] not in ACTIVE_STATUSES:
            return
        cli_path = row["cli_path"]
        data_dir = row["data_dir"]
        last: Optional[dict[str, Any]] = None
        for attempt in range(LOGIN_LIVE_PROBE_ATTEMPTS):
            if attempt:
                time.sleep(LOGIN_LIVE_PROBE_INTERVAL_SECONDS)
            try:
                if self._live_probe is _default_live_probe:
                    channel = store.jloads(row.get("channel_snapshot"), {})
                    options = channel_state.channel_options(channel) if channel else {}
                    last = self._live_probe(cli_path, data_dir, LOGIN_LIVE_PROBE_TIMEOUT_SECONDS,
                                            top_level_options=options)
                else:
                    last = self._live_probe(cli_path, data_dir, LOGIN_LIVE_PROBE_TIMEOUT_SECONDS)
            except Exception as exc:  # 探测失败只作诊断，不改写上游事实
                last = {"ok": False, "error_code": "UPSTREAM_PROCESS_FAILED",
                        "detail": redact(f"{type(exc).__name__}: {exc}")[:300]}
            verdict = classify_login_probe(last or {})
            if verdict != PROBE_NOT_CONNECTED:
                self._mark_connected(login_id, verdict, last or {})
                return
        probe_error = str((last or {}).get("error_code") or "").strip()
        detail = str((last or {}).get("detail") or "")[:300]
        probe_fact = _probe_digest(last)
        if fallback is not None and probe_error in ("", "AUTH_REQUIRED"):
            # 在线事实只是「没登上」：用登录层已拿到的结论说清原因
            status, error_code = fallback[0], fallback[1]
        elif probe_error in ENV_NOT_READY_CODES:
            status, error_code = "env_not_ready", probe_error
        else:
            status, error_code = "failed", probe_error or "AUTH_REQUIRED"
        prefix = f"{fallback[2]}；" if fallback is not None else ""
        self._finalize(login_id, status, error_code,
                       f"{prefix}在线复核未通过认证检查（{probe_fact}）：{detail}")

    def _mark_connected(self, login_id: str, verdict: str = PROBE_CONNECTED,
                        probe: Optional[dict] = None) -> None:
        """落「已连接」终态；凭证不完整时记为 `connected_degraded` 并附降级说明。"""
        degraded = verdict == PROBE_CONNECTED_DEGRADED
        status = "connected_degraded" if degraded else "connected"
        if degraded:
            message = ("账号在线复核通过，但凭证不完整（%s）；"
                       "搜索是否可用需由实际取数确认" % _probe_digest(probe))
        else:
            message = "账号在线复核通过，搜索是否可用需由实际取数确认"
        mapped = user_view.user_view(status, "connected", None)
        connection = get_conn()
        now = utcnow_iso()
        cursor = connection.execute(
            """UPDATE login_task SET status=?, stage='verifying', active=0,
                 live_verified_at=?, message=?, user_message=?, updated_at=?, finished_at=?
               WHERE id=? AND status IN ('starting','waiting_user','verifying')""",
            (status, now, message, mapped["user_message"], now, now, login_id),
        )
        connection.commit()
        if not cursor.rowcount:
            return
        callback = self.on_connected
        if callable(callback):
            try:
                callback()
            except Exception as exc:  # 登录成功回调失败不得影响登录结果
                print(f"[finintel] on_login_connected 回调失败: {type(exc).__name__}",
                      file=sys.stderr)

    def _update(self, login_id: str, **fields: Any) -> None:
        fields["updated_at"] = utcnow_iso()
        keys = ", ".join(f"{key}=?" for key in fields)
        connection = get_conn()
        connection.execute(
            f"UPDATE login_task SET {keys} WHERE id=?", (*fields.values(), login_id)
        )
        connection.commit()

    def _finalize(self, login_id: str, status: str, error_code: Optional[str],
                  message: str) -> None:
        row = query_one("SELECT status FROM login_task WHERE id=?", (login_id,))
        if not row:
            return  # 记录已被清理，进程收尾时静默退出
        if row["status"] in TERMINAL_STATUSES:
            return
        mapped = user_view.user_view(status, "disconnected", error_code)
        connection = get_conn()
        now = utcnow_iso()
        connection.execute(
            """UPDATE login_task SET status=?, active=0, error_code=?, message=?,
                 user_message=?, updated_at=?, finished_at=? WHERE id=?""",
            (status, error_code, redact(message)[:500], mapped["user_message"], now, now, login_id),
        )
        connection.commit()

    def _kill(self, login_id: str, status: str, error_code: Optional[str],
              message: str) -> None:
        with self._lock:
            process = self._processes.get(login_id)
        self._finalize(login_id, status, error_code, message)
        if process is not None:
            _kill_tree(process)

    # -- 内部：辅助 ----------------------------------------------------------

    @staticmethod
    def _boss_state(row: dict) -> str:
        # 凭证不完整（partial）也是「已连接」：能否真实取数由在线复核结果决定
        if row["status"] in CONNECTED_STATUSES:
            return "connected"
        if row["status"] in ACTIVE_STATUSES:
            return "connecting"
        return "disconnected"


def _default_cli_path() -> Optional[str]:
    row = query_one("SELECT value FROM app_setting WHERE key='boss_cli_path'")
    return resolve_cli_path(row["value"] if row else None)


def _default_spawn(argv: list[str]) -> Any:
    # 与 run_upstream 同一套环境净化：登录子进程同样不能继承回环代理，否则登录/搜索
    # 走一个出口、在线复核走另一个出口，同一会话出现两个出口 IP（code 37 诱因）。
    environment = sanitized_child_env()
    environment["PYTHONIOENCODING"] = "utf-8"
    return subprocess.Popen(
        argv,
        cwd=str(PROJECT_DIR),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # 只隐藏 boss.exe 自己的控制台；patchright 的 Chromium 是独立 GUI 进程，保持可见
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _default_live_probe(cli_path: str, data_dir: str, timeout_seconds: int,
                        top_level_options: Optional[dict] = None) -> dict[str, Any]:
    """跑一次只读的 `status --live`，只取「能不能真的取数」的事实（事实 3）。

    返回体只有布尔事实与凭证质量标签，绝不含 cookie / token / user_agent 等任何凭证值。
    """
    import asyncio

    from pathlib import Path

    invocation = asyncio.run(run_upstream(
        cli_path, "status", data_dir=Path(data_dir),
        options={"--live": True}, timeout_seconds=timeout_seconds,
        top_level_options=top_level_options or {},
    ))
    envelope = parse_envelope(invocation)
    if not envelope.ok:
        error = envelope.error or {}
        return {"ok": False, "error_code": error.get("code") or "UPSTREAM_PROCESS_FAILED",
                "auth_state": None, "auth_summary": "", "logged_in": False, "live": False,
                "data_dir_echo": envelope.data_dir,
                "detail": redact(str(error.get("message") or ""))[:300]}
    data = envelope.data if isinstance(envelope.data, dict) else {}
    health = data.get("auth_health") if isinstance(data.get("auth_health"), dict) else {}
    auth_state = data.get("auth_state") or health.get("auth_state")
    return {
        "ok": True,
        "auth_state": str(auth_state) if auth_state else None,
        "auth_summary": str(data.get("auth_summary") or health.get("summary") or ""),
        "logged_in": bool(data.get("logged_in")),
        "live": bool(data.get("live")),
        "data_dir_echo": data.get("data_dir"),
        "detail": "",
    }


class _RawInvocationLike:
    """`parse_envelope` 只需要这几个字段；避免为收盘解析复制 RawInvocation。"""

    def __init__(self, *, stdout: str, returncode: int, stderr: str, data_dir: str) -> None:
        self.argv: list[str] = []
        self.argv_redacted: list[str] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.elapsed_ms = 0
        self.timed_out = False
        self.data_dir = data_dir


def _append_tail(tail: str, line: str) -> str:
    lines = [item for item in (tail or "").split("\n") if item]
    lines.append(line)
    lines = lines[-LOGIN_STDERR_MAX_LINES:]
    text = "\n".join(lines)
    return text[-LOGIN_STDERR_MAX_CHARS:]


def _kill_tree(process: Any) -> None:
    """回收整个进程树：连 patchright 拉起的 Chromium 子进程一起杀掉。"""
    pid = getattr(process, "pid", None)
    if os.name == "nt" and pid:
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                process.wait(timeout=2)
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return


def timeout_bounds_error(timeout_seconds: int) -> Optional[str]:
    """API 层校验用户可请求的超时范围（设计 §5.1）。"""
    low, high = LOGIN_TIMEOUT_BOUNDS
    if not isinstance(timeout_seconds, int) or not low <= timeout_seconds <= high:
        return f"登录超时需在 {low}~{high} 秒之间"
    return None


# ---------------------------------------------------------------------------
# 顶栏 / 首页的 boss 块：**唯一**实现
#
# ``/api/home``、``/api/auth/status``、``/api/tasks/{id}`` 三处都会往响应里放一个
# ``boss`` 块，前端 ``sfSetBoss()`` 无条件用它覆盖顶栏状态。真机缺陷就是其中一处
# 手写字面量漏了 ``channel`` → 覆盖后 ``SF_BOSS.channel`` 变成 null → 顶栏从
# 「账号登录已验证，当前搜索连接尚未就绪。」退化成「正在确认搜索通道」。
# 因此收敛到这一个函数：谁都不许再手写第二份，任何一处漏字段都会立刻被
# ``test_pipeline.test_every_boss_block_is_the_same_shape_and_same_facts`` 抓住。
#
# 依赖方向：本模块只依赖 ``user_view`` / ``store`` / ``upstream_*``，**不得**反向
# import ``app`` / ``pipeline``；``app`` 与 ``pipeline`` 都单向依赖本模块。
# ---------------------------------------------------------------------------

BOSS_BLOCK_FIELDS: tuple[str, ...] = (
    "state", "account_state", "user_stage", "user_message", "actions",
    "live_verified_at", "login", "channel", "channel_ready", "collect_ready",
)


def boss_block(status: Optional[dict] = None) -> dict:
    """把 ``status_view()`` 收敛成顶栏/首页消费的 ``boss`` 块（三处接口共用）。

    入参可复用调用方**已经取到**的 ``status_view()``（同一请求内不重复取，保证三处
    拿到的是同一时刻的同一份事实）；缺省时自行取一次。

    字段口径（设计 §2.4/§2.5）：

    - ``state``：账号事实的旧字段，保留兼容；
    - ``account_state``：**未被打扮**的账号事实——``user_stage`` 会被「通道未就绪」
      覆盖成 ``env_not_ready``，顶栏做「账号 × 通道」两维判定必须用它；
    - ``channel`` / ``channel_ready``：通道事实；``channel_ready`` = 通道确凿就绪
      （``channel.state == 'ready'``），顶栏绿点的**唯一**依据；
    - ``collect_ready``：采集门禁（``collect_gate()``）；``unknown`` / ``existing-browser``
      的 24h 兜底也可能为真，那是「允许试一次」，**不等于**「已经就绪」；
    - ``login``：活跃登录流程的 UI 视图（无任何凭据），``/api/tasks/{id}`` 原本就带，
      统一后三处一致、字段不缩水。
    """
    payload = status if status is not None else controller.status_view()
    return {
        "state": payload["state"],
        "account_state": payload.get("account_state") or payload["state"],
        "user_stage": payload["user_stage"],
        "user_message": payload["user_message"],
        "actions": payload["actions"],
        "live_verified_at": payload.get("live_verified_at"),
        "login": payload.get("login"),
        "channel": payload.get("channel"),
        "channel_ready": bool(payload.get("channel_ready")),
        "collect_ready": bool(payload.get("collect_ready")),
    }


__all__ = [
    "ACTIVE_STATUSES",
    "BOSS_BLOCK_FIELDS",
    "CONNECTED_STATUSES",
    "TERMINAL_STATUSES",
    "LoginController",
    "LoginError",
    "boss_block",
    "classify_error",
    "classify_login_probe",
    "classify_stderr_line",
    "controller",
    "credential_settled",
    "timeout_bounds_error",
]

# 进程内唯一控制器（app.py 挂 on_login_connected 回调）
controller = LoginController()
