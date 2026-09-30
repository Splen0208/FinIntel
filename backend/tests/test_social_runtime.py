# -*- coding: utf-8 -*-
"""依赖发现/冻结（C5, T08）与有界流式读取 + 子进程环境隔离（D1/D2, T11）的**离线**测试。

**本文件全部是离线测试**：

- provider 发现用**虚拟文件系统**（``FakeFs``）注入，**不启动任何真实进程**；
- 有界流式读取用可控的**鸭子类型子进程替身**（暴露异步 ``stdout.read`` / ``stderr.read``）
  驱动；另有一条用**本机 Python 解释器**启动的真实子进程用例，仅验证"读取过程中达到上限即
  终止"，不访问任何平台；
- 环境隔离用 ``SENTINEL_SECRET_*`` 哨兵假值，**不使用任何真实 token**。

离线通过 ≠ 平台可用。这里证明的是"运行时对超限/超时/环境范围的处置正确"，
不是"小红书 / X 当前能采到数据"。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(BACKEND_DIR), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import store  # noqa: E402
import social_testkit as kit  # noqa: E402
import upstream_config  # noqa: E402
from collectors import provider  # noqa: E402
from collectors import runtime  # noqa: E402
from collectors.contracts import CollectorError  # noqa: E402

# 哨兵假值（明显不是真实凭据；只用于断言"是否被传进子进程环境"）
SENTINEL_TW_TOKEN = "SENTINEL_SECRET_TW_AUTH_TOKEN"
SENTINEL_CT0 = "SENTINEL_SECRET_CT0"
SENTINEL_LLM_KEY = "SENTINEL_SECRET_LLM_API_KEY"
SENTINEL_DASHSCOPE = "SENTINEL_SECRET_DASHSCOPE_KEY"


# ---------------------------------------------------------------------------
# 虚拟文件系统（不碰真实磁盘布局）
# ---------------------------------------------------------------------------

def _norm(path: object) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


class FakeFs(provider.FileSystem):
    """按文件/目录/PE 魔数集合判定的虚拟文件系统。"""

    def __init__(self, files=(), dirs=(), pe=(), unreadable=()) -> None:
        self.files = {_norm(p) for p in files}
        self.dirs = {_norm(p) for p in dirs}
        self.pe = {_norm(p) for p in pe}
        self.unreadable = {_norm(p) for p in unreadable}

    def is_file(self, path: object) -> bool:
        return _norm(path) in self.files

    def is_dir(self, path: object) -> bool:
        return _norm(path) in self.dirs

    def is_readable(self, path: object) -> bool:
        key = _norm(path)
        return key in self.files and key not in self.unreadable

    def has_windows_pe_magic(self, path: object) -> bool:
        return _norm(path) in self.pe


def _resolver(fs: FakeFs, *, which=None, layout=None, **kwargs) -> provider.ProviderResolver:
    return provider.ProviderResolver(
        fs=fs,
        which=which or (lambda name: None),
        layout_candidates=layout or (lambda kind: []),
        os_name="nt",
        **kwargs,
    )


# OpenCLI 本机 1.8.8 的三种真实文件布局元素
_BASE = Path("C:/fake/node/versions/22.22.2-3")
_SHIM_BARE = _BASE / "opencli"          # 无后缀 Unix shell shim（非 PE，不可直接执行）
_SHIM_CMD = _BASE / "opencli.cmd"       # npm 的 .cmd 包装（只作线索）
_NODE = _BASE / "node.exe"              # 真实 node（PE）
_ENTRY = _BASE / "node_modules" / "@jackwener" / "opencli" / "dist" / "src" / "main.js"


# ===========================================================================
# 1. provider 发现：三种布局（离线）
# ===========================================================================

def test_provider_only_bare_shim_is_dependency_missing() -> None:
    """离线：只有无后缀 shell shim（非 PE、无 node、无入口）→ DEPENDENCY_MISSING。"""
    fs = FakeFs(files=[_SHIM_BARE])           # shim 存在，但不是 PE
    resolver = _resolver(fs)

    with pytest.raises(CollectorError) as exc:
        resolver.resolve(provider.KIND_XHS, configured=str(_SHIM_BARE))

    assert exc.value.code == "DEPENDENCY_MISSING", exc.value.code
    assert exc.value.operator_action, "依赖缺失必须给出可执行的安装/配置指引"
    # 不能把裸 shim 当可执行文件启动
    assert "node" in exc.value.operator_action.lower() or "opencli" in exc.value.operator_action.lower()


def test_provider_node_entry_layout_resolves_and_verifies() -> None:
    """离线：node.exe + main.js 齐备 → 解析成 node_entry 组合并通过校验。"""
    fs = FakeFs(files=[_SHIM_BARE, _NODE, _ENTRY], pe=[_NODE])
    resolver = _resolver(fs)

    handle = resolver.resolve(provider.KIND_XHS, configured=str(_SHIM_BARE))

    assert handle.launch_mode == provider.LAUNCH_NODE_ENTRY
    assert handle.verified is True
    assert handle.argv_prefix() == [str(_NODE), str(_ENTRY)]
    # 裸 shim 只是线索，绝不进 argv
    assert str(_SHIM_BARE) not in handle.argv_prefix()


def test_provider_cmd_wrapper_resolves_to_node_entry() -> None:
    """离线：.cmd 只作线索，最终落到 node + main.js。"""
    fs = FakeFs(files=[_SHIM_CMD, _NODE, _ENTRY], pe=[_NODE])
    resolver = _resolver(fs)

    handle = resolver.resolve(provider.KIND_XHS, configured=str(_SHIM_CMD))

    assert handle.launch_mode == provider.LAUNCH_NODE_ENTRY
    assert handle.argv_prefix() == [str(_NODE), str(_ENTRY)]


def test_provider_node_outside_shim_dir_still_resolves() -> None:
    """离线：node 不在 shim 同目录（改用 PATH 命中）也能发现——不假设同目录必有 node。"""
    elsewhere = Path("C:/tools/nodejs/node.exe")
    fs = FakeFs(files=[_SHIM_CMD, _ENTRY, elsewhere], pe=[elsewhere])
    resolver = _resolver(fs, which=lambda name: str(elsewhere)
                         if name in ("node.exe", "node") else None)

    handle = resolver.resolve(provider.KIND_XHS, configured=str(_SHIM_CMD))

    assert handle.launch_mode == provider.LAUNCH_NODE_ENTRY
    assert handle.node_path == str(elsewhere)
    assert handle.entry_path == str(_ENTRY)


def test_provider_both_missing_is_dependency_missing() -> None:
    """离线：什么候选都没有 → DEPENDENCY_MISSING（不抛别的模糊异常）。"""
    resolver = _resolver(FakeFs())
    with pytest.raises(CollectorError) as exc:
        resolver.resolve(provider.KIND_XHS)
    assert exc.value.code == "DEPENDENCY_MISSING", exc.value.code


def test_provider_real_pe_exe_is_direct_exec() -> None:
    """离线：真实 PE（twitter.exe）→ 直接 exec。"""
    exe = Path("C:/fake/twitter.exe")
    fs = FakeFs(files=[exe], pe=[exe])
    resolver = _resolver(fs)

    handle = resolver.resolve(provider.KIND_X, configured=str(exe))

    assert handle.launch_mode == provider.LAUNCH_EXEC
    assert handle.argv_prefix() == [str(exe)]


def test_provider_exe_without_pe_is_not_launched_directly() -> None:
    """离线：非 PE 的 .exe（壳/包装器）不得直接启动；无包布局时判 DEPENDENCY_MISSING。"""
    exe = Path("C:/fake/twitter.exe")     # 存在、可读，但不在 PE 集合里
    fs = FakeFs(files=[exe])
    resolver = _resolver(fs)

    with pytest.raises(CollectorError) as exc:
        resolver.resolve(provider.KIND_X, configured=str(exe))
    assert exc.value.code == "DEPENDENCY_MISSING", exc.value.code


def test_provider_unreadable_entry_is_rejected() -> None:
    """离线：入口存在但不可读 → 校验失败，而不是"发现成功"再用时炸。"""
    fs = FakeFs(files=[_SHIM_CMD, _NODE, _ENTRY], pe=[_NODE], unreadable=[_ENTRY])
    resolver = _resolver(fs)
    with pytest.raises(CollectorError) as exc:
        resolver.resolve(provider.KIND_XHS, configured=str(_SHIM_CMD))
    assert exc.value.code == "DEPENDENCY_MISSING", exc.value.code


# ===========================================================================
# 2. 冻结：任务启动后不再重新 which（离线）
# ===========================================================================

def test_frozen_provider_is_not_requeried() -> None:
    """离线：冻结进 config_snapshot 后，外部配置改变不影响本任务。"""
    fs = FakeFs(files=[_SHIM_CMD, _NODE, _ENTRY], pe=[_NODE])
    handle = _resolver(fs).resolve(provider.KIND_XHS, configured=str(_SHIM_CMD))

    with kit.isolated_db():
        run_id = kit.with_run()
        frozen = provider.freeze_into(run_id, handle)
        assert frozen["exe_path"] == str(_SHIM_CMD)

        # 外部安装布局变化（换到另一个 node 目录）→ 冻结快照必须纹丝不动
        other_node = Path("C:/other/node.exe")
        other_fs = FakeFs(files=[_SHIM_CMD, other_node, _ENTRY], pe=[other_node])
        other = _resolver(other_fs, which=lambda name: str(other_node)
                          if name in ("node.exe", "node") else None
                          ).resolve(provider.KIND_XHS, configured=str(_SHIM_CMD))
        assert other.node_path == str(other_node), "外部布局确实已变化（对照组）"

        row = store.query_one("SELECT config_snapshot FROM research_run WHERE id=?", (run_id,))
        snapshot = store.jloads(row["config_snapshot"], {})
        again = provider.handle_for_run({"config_snapshot": snapshot})

        assert again is not None
        assert again.node_path == str(_NODE), "任务必须继续使用冻结的 node"
        assert again.fingerprint() == handle.fingerprint()
        assert snapshot["provider"]["exe_path"] == str(_SHIM_CMD)


def test_require_frozen_handle_without_freeze_raises_provider_not_frozen() -> None:
    """离线：未冻结就采集 → PROVIDER_NOT_FROZEN（不是模糊失败）。"""
    with pytest.raises(CollectorError) as exc:
        provider.require_frozen_handle({"config_snapshot": {}}, provider.KIND_XHS)
    assert exc.value.code == "PROVIDER_NOT_FROZEN", exc.value.code


# ===========================================================================
# 3. 三种失败分开报（离线）
# ===========================================================================

def test_three_failure_classes_are_distinct() -> None:
    """离线：依赖缺失 / 连接不可用 / 账号未登录必须**分开**报，不得合并。"""
    dep = provider.classify_failure(provider.KIND_XHS, dependency_ok=False)
    con = provider.classify_failure(provider.KIND_XHS, connection_ok=False)
    auth = provider.classify_failure(provider.KIND_XHS, authenticated=False)

    assert dep is not None and dep.code == "DEPENDENCY_MISSING"
    assert con is not None and con.code == "CONNECTION_UNAVAILABLE"
    assert auth is not None and auth.code == "AUTH_REQUIRED"
    # 依赖缺失优先于连接/登录（依赖没装就谈不上连接）
    assert provider.classify_failure(
        provider.KIND_XHS, dependency_ok=False, connection_ok=False,
        authenticated=False).code == "DEPENDENCY_MISSING"
    # 无问题 → None（不是"通过"的绿灯，只是"未发现明确问题"）
    assert provider.classify_failure(provider.KIND_XHS) is None


def test_provider_status_error_uses_same_classification() -> None:
    status = provider.ProviderStatus(kind=provider.KIND_X, connection_ok=False)
    error = status.error()
    assert error is not None and error.code == "CONNECTION_UNAVAILABLE"
    assert provider.ProviderStatus(kind=provider.KIND_X).error() is None


# ===========================================================================
# 4. 有界流式读取（D1，离线替身）
# ===========================================================================

class _FakeStream:
    """异步可读流替身；``stall=True`` 时永不返回（用于模拟超时）。"""

    def __init__(self, data: bytes = b"", *, chunk: int = 65536, stall: bool = False) -> None:
        self._data = data
        self._pos = 0
        self._chunk = max(1, chunk)
        self._stall = stall
        self._closed = False

    async def read(self, n: int = -1) -> bytes:
        if self._stall:
            await asyncio.sleep(3600)
        if self._closed:
            return b""
        size = self._chunk if n is None or n < 0 else n
        piece = self._data[self._pos:self._pos + size]
        self._pos += len(piece)
        return piece

    def close(self) -> None:
        self._closed = True


class _StreamProcess:
    """暴露 stdout/stderr 的鸭子类型子进程（触发有界流式路径）。"""

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"",
                 *, returncode: int = 0, stall_stdout: bool = False) -> None:
        self.stdout = _FakeStream(stdout, stall=stall_stdout)
        self.stderr = _FakeStream(stderr)
        self._final = returncode
        self.returncode: int | None = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.stdout.close()
        self.stderr.close()

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = self._final
        return self.returncode


def _exe(tmp_path: Path) -> str:
    exe = tmp_path / "opencli.exe"
    exe.write_bytes(b"")
    return str(exe)


def _env_has(env: dict, name: str) -> bool:
    """大小写不敏感的环境变量查找（Windows 的 os.environ 会把键归一为大写）。"""
    upper = name.upper()
    return any(str(key).upper() == upper for key in env)


def _env_get(env: dict, name: str):
    upper = name.upper()
    for key, value in env.items():
        if str(key).upper() == upper:
            return value
    return None


def test_run_command_kills_and_raises_when_stdout_exceeds_limit(tmp_path, monkeypatch) -> None:
    """离线：输出超过上限 → 子进程被终止 + OUTPUT_LIMIT_EXCEEDED，超限内容不当正常结果。"""
    proc = _StreamProcess(stdout=b"x" * (runtime.MAX_STDOUT_BYTES + 512 * 1024))

    async def _fake(*argv, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake)

    with pytest.raises(CollectorError) as exc:
        asyncio.run(runtime.run_command(
            _exe(tmp_path), "search", positional=("关键词",),
            allowed_commands=runtime.XHS_READ_COMMANDS, platform="xiaohongshu",
            timeout_seconds=30))

    assert exc.value.code == "OUTPUT_LIMIT_EXCEEDED", exc.value.code
    assert exc.value.evidence.get("stdout_exceeded") is True
    assert proc.killed is True, "超限必须终止**本次**子进程"


def test_run_command_stderr_exceeds_limit_also_fails(tmp_path, monkeypatch) -> None:
    """离线：stderr 超过上限同样终止并报错（不把超限诊断当正常结果）。"""
    proc = _StreamProcess(stderr=b"e" * (runtime.MAX_STDERR_BYTES + 128 * 1024))

    async def _fake(*argv, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake)

    with pytest.raises(CollectorError) as exc:
        asyncio.run(runtime.run_command(
            _exe(tmp_path), "search", positional=("q",),
            allowed_commands=runtime.XHS_READ_COMMANDS, platform="xiaohongshu",
            timeout_seconds=30))
    assert exc.value.code == "OUTPUT_LIMIT_EXCEEDED", exc.value.code
    assert exc.value.evidence.get("stderr_exceeded") is True
    assert proc.killed is True


def test_run_command_timeout_is_marked_and_process_killed(tmp_path, monkeypatch) -> None:
    """离线：进程不退出 → 超时被标记、子进程被终止（不无限等待）。"""
    proc = _StreamProcess(stall_stdout=True)

    async def _fake(*argv, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake)

    result = asyncio.run(runtime.run_command(
        _exe(tmp_path), "search", positional=("q",),
        allowed_commands=runtime.XHS_READ_COMMANDS, platform="xiaohongshu",
        timeout_seconds=1))

    assert result["timed_out"] is True
    assert proc.killed is True


def test_run_command_process_exits_early_returns_output(tmp_path, monkeypatch) -> None:
    """离线：进程先退出（EOF）→ 正常返回 stdout/returncode。"""
    proc = _StreamProcess(stdout=b'{"ok": true}', returncode=0)

    async def _fake(*argv, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake)

    result = asyncio.run(runtime.run_command(
        _exe(tmp_path), "search", positional=("q",),
        allowed_commands=runtime.XHS_READ_COMMANDS, platform="xiaohongshu",
        timeout_seconds=30))

    assert result["timed_out"] is False
    assert result["returncode"] == 0
    assert result["stdout"] == '{"ok": true}'
    assert proc.killed is False


def test_communicate_bounded_stops_real_subprocess_at_limit() -> None:
    """离线：真实子进程持续输出 → 在达到上限前被终止（本机 Python，无网络）。"""
    code = ("import sys\n"
            "buf = b'x' * 65536\n"
            "while True:\n"
            "    sys.stdout.buffer.write(buf)\n"
            "    sys.stdout.buffer.flush()\n")
    cap = 512 * 1024

    async def _main():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        return await upstream_config.communicate_bounded(
            proc, timeout_seconds=30, max_stdout_bytes=cap, max_stderr_bytes=64 * 1024)

    try:
        bounded = asyncio.run(_main())
    except OSError as exc:  # pragma: no cover - 环境异常时跳过
        pytest.skip(f"本机无法启动子进程：{exc}")

    assert bounded.stdout_exceeded is True
    assert len(bounded.stdout) <= cap + 65536, "不得把整段输出读进内存"
    assert bounded.returncode is not None, "超限后必须回收子进程"


def test_run_worker_marks_truncated_when_output_exceeds(monkeypatch, tmp_path) -> None:
    """离线：run_worker 也走有界流式；超限时置 output_truncated 且不返回完整输出。"""
    proc = _StreamProcess(stdout=b"y" * (upstream_config.MAX_STDOUT_BYTES + 256 * 1024))

    async def _fake(*argv, **kwargs):
        return proc

    monkeypatch.setattr(upstream_config.asyncio, "create_subprocess_exec", _fake)

    invocation = asyncio.run(upstream_config.run_worker(
        worker_py="boss_browser_worker.py",
        python=sys.executable,
        data_dir=tmp_path,
        positional=("sid", "lid"),
        timeout_seconds=30,
    ))

    assert invocation.output_truncated is True
    assert len(invocation.stdout) <= upstream_config.MAX_STDOUT_BYTES
    assert proc.killed is True


# ===========================================================================
# 5. 环境隔离（D2，离线，哨兵假值）
# ===========================================================================

def test_child_env_excludes_other_platform_credentials_and_model_keys(monkeypatch) -> None:
    """离线：其它平台凭据 / 模型密钥 / 无关调试变量不得传给采集子进程。"""
    monkeypatch.setenv("TWITTER_AUTH_TOKEN", SENTINEL_TW_TOKEN)
    monkeypatch.setenv("TWITTER_CT0", SENTINEL_CT0)
    monkeypatch.setenv("LLM_API_KEY", SENTINEL_LLM_KEY)
    monkeypatch.setenv("DASHSCOPE_API_KEY", SENTINEL_DASHSCOPE)
    monkeypatch.setenv("FININTEL_DB", "C:/should/not/reach/child.db")
    monkeypatch.setenv("PATH", "C:\\Windows\\System32")
    monkeypatch.setenv("SystemRoot", "C:\\Windows")

    env = runtime.build_child_env()

    for name in ("TWITTER_AUTH_TOKEN", "TWITTER_CT0", "LLM_API_KEY",
                 "DASHSCOPE_API_KEY", "FININTEL_DB"):
        assert not _env_has(env, name), f"{name} 不应传给采集子进程"
    joined = " ".join(env.values())
    for sentinel in (SENTINEL_TW_TOKEN, SENTINEL_CT0, SENTINEL_LLM_KEY, SENTINEL_DASHSCOPE):
        assert sentinel not in joined, "哨兵凭据值不得出现在子进程环境"
    # Windows 起进程必需的系统变量必须保留
    assert _env_get(env, "PATH") == "C:\\Windows\\System32"
    assert _env_get(env, "SystemRoot") == "C:\\Windows"
    assert _env_get(env, "PYTHONIOENCODING") == "utf-8"


def test_child_env_injects_platform_credentials_explicitly() -> None:
    """离线：平台专用凭据**显式注入**（只经 env，不进 argv）。"""
    env = runtime.build_child_env({
        "TWITTER_AUTH_TOKEN": SENTINEL_TW_TOKEN,
        "TWITTER_CT0": SENTINEL_CT0,
        "": "ignored",
    })
    assert _env_get(env, "TWITTER_AUTH_TOKEN") == SENTINEL_TW_TOKEN
    assert _env_get(env, "TWITTER_CT0") == SENTINEL_CT0
    assert not any(str(key).strip() == "" for key in env)


def test_run_command_child_env_carries_only_injected_credentials(tmp_path, monkeypatch) -> None:
    """离线：run_command 传给子进程的环境只含显式注入的凭据（不含模型密钥）。"""
    captured: dict = {}

    async def _fake(*argv, **kwargs):
        captured["env"] = dict(kwargs.get("env") or {})
        raise FileNotFoundError()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake)
    monkeypatch.setenv("LLM_API_KEY", SENTINEL_LLM_KEY)
    monkeypatch.setenv("PATH", "C:\\Windows\\System32")
    exe = tmp_path / "twitter.exe"
    exe.write_bytes(b"")

    asyncio.run(runtime.run_command(
        str(exe), "search", positional=("q",),
        allowed_commands=runtime.X_READ_COMMANDS,
        env={"TWITTER_AUTH_TOKEN": SENTINEL_TW_TOKEN}))

    env = captured["env"]
    assert _env_get(env, "TWITTER_AUTH_TOKEN") == SENTINEL_TW_TOKEN
    assert not _env_has(env, "LLM_API_KEY")
    assert SENTINEL_LLM_KEY not in " ".join(env.values())
    assert _env_get(env, "PATH") == "C:\\Windows\\System32"


def test_build_child_env_can_still_start_a_real_process() -> None:
    """离线：净化后的环境仍能启动真实进程（防"过度净化导致 Windows 起不来"回归）。"""

    async def _main():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "import os,sys;sys.stdout.write('OK' if os.environ.get('PATH') else 'NOPATH')",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            env=runtime.build_child_env(),
        )
        out, err = await proc.communicate()
        return proc.returncode, out.decode("utf-8", "replace").strip(), err.decode("utf-8", "replace")

    try:
        code, out, err = asyncio.run(_main())
    except OSError as exc:  # pragma: no cover - 环境异常时跳过
        pytest.skip(f"本机无法启动子进程：{exc}")
    assert code == 0, err
    assert out == "OK", f"净化后的环境缺少 PATH：{out!r} / {err!r}"


# ===========================================================================
# 6. runtime 的 shim 解析收口（C5 在运行时的落点）
# ===========================================================================

def test_run_command_cmd_shim_is_resolved_via_provider(tmp_path, monkeypatch) -> None:
    """离线：给 runtime 传 .cmd shim 时，真正启动的是 node + main.js（不启动 shim）。"""
    fs = FakeFs(files=[_SHIM_CMD, _NODE, _ENTRY], pe=[_NODE])
    monkeypatch.setattr(provider, "RESOLVER",
                        _resolver(fs))
    captured: dict = {}

    async def _fake(*argv, **kwargs):
        captured["argv"] = list(argv)
        raise FileNotFoundError()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake)

    asyncio.run(runtime.run_command(
        str(_SHIM_CMD), "search", positional=("关键词",),
        allowed_commands=runtime.XHS_READ_COMMANDS, platform="xiaohongshu"))

    argv = captured["argv"]
    assert argv[0] == str(_NODE), "必须以 node 启动"
    assert argv[1] == str(_ENTRY), "必须指向 main.js 入口"
    assert "search" in argv and "xiaohongshu" in argv
    assert str(_SHIM_CMD) not in argv, "不得把 .cmd shim 当可执行文件"
