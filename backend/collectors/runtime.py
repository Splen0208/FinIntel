# -*- coding: utf-8 -*-
"""社交采集运行时：绝对路径命令调用、超时、**有界流式输出**、脱敏、白名单。

对应指令 §5-D（D1 输出限制、D2 环境变量范围）与增量设计 T11。

铁律
----
- 禁止 ``shell=True``；使用 argv 数组；关键词即使含引号/分号/``$()`` 也只能成为数据。
- **有界流式读取**：不再先 ``communicate()`` 读完再判大小——stdout/stderr 分片读取，
  累计**超过**上限立即 ``kill()`` **本次**子进程并回收，覆盖「超时 / 进程先退出 / 达到上限」
  三种竞争。超限内容**绝不**当正常结果返回。
- **环境隔离**（D2）：不把整个服务端环境传给采集子进程；剔除其它平台凭据、模型密钥、
  无关调试变量；保留 Windows 起进程必需的 ``PATH`` / ``SystemRoot`` 等系统变量；
  平台专用凭据**显式注入**（只经 env，不进 argv/日志/快照）。
- 只 ``kill()`` 本次 ``Popen`` 对象：**绝不**关闭用户浏览器、绝不杀别的任务。
- 输出 JSON 信封到 stdout，脱敏诊断到 stderr。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

# 只读白名单（X / 小红书采集命令）。任何写操作（点赞/评论/关注）不在此列。
X_READ_COMMANDS = frozenset({"search", "tweet", "user-posts", "status"})
XHS_READ_COMMANDS = frozenset({"search", "note", "comments", "whoami", "feed", "user"})

# 输出上限（字节）：超过即终止本次子进程并丢弃超限内容，避免超大输出撑爆内存。
MAX_STDOUT_BYTES = 2 * 1024 * 1024
MAX_STDERR_BYTES = 256 * 1024

# 脱敏正则：token/cookie/密钥类，绝不进入日志或快照。
_SECRET_RE = re.compile(
    r"(?i)(auth_token|ct0|stoken|wt2|apikey|api_key|token|secret|cookie|password)\s*[:=]\s*[^\s,;\"']+"
)

# ---------------------------------------------------------------------------
# 子进程环境隔离（D2）
# ---------------------------------------------------------------------------
# 系统/运行必需变量白名单：**无论如何都保留**（盲目清空会让 Windows 进程起不来）。
_SYSTEM_ENV_ALLOW = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "USERPROFILE", "USERNAME", "USERDOMAIN", "HOMEDRIVE", "HOMEPATH",
    "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER",
    "OS", "LANG", "LC_ALL", "TZ", "SYSTEMTIMEZONE", "PUBLIC",
    "PYTHONIOENCODING", "PYTHONUTF8",
})
# 其它平台/本服务的凭据名（默认不传给其它平台的采集子进程）。
_CREDENTIAL_ENV_NAMES = frozenset({
    "TWITTER_AUTH_TOKEN", "TWITTER_CT0", "TWITTER_COOKIE", "TWITTER_CSRF",
    "X_AUTH_TOKEN", "X_CT0", "TWITTER_PROXY",
})
# 模型密钥 / 外部服务密钥的变量名前缀（一律不传）。
_CREDENTIAL_ENV_PREFIXES = (
    "LLM_", "OPENAI", "ANTHROPIC", "DASHSCOPE", "DEEPSEEK", "MOONSHOT",
    "GEMINI", "GOOGLE_API", "AZURE_OPENAI", "HUGGINGFACE", "HF_",
    "AWS_", "GCP_", "BEDROCK_", "SILICONFLOW", "MODELSCOPE",
)
# 凭据类变量名的常见后缀（兜底）。
_CREDENTIAL_ENV_SUFFIXES = ("_API_KEY", "_APIKEY", "_TOKEN", "_SECRET", "_PASSWORD", "_COOKIE")
# 与本服务调试相关、对采集子进程无意义甚至有害的变量（一并剔除）。
_DEBUG_ENV_NAMES = frozenset({
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONBREAKPOINT",
    "PYTHONWARNINGS", "PYTHONVERBOSE", "PYTHONDEBUG", "PYTHONINSPECT",
    "FININTEL_DB", "FININTEL_PORT", "FININTEL_UPSTREAM_DATA_DIR",
})


def is_secret_env_name(key: str) -> bool:
    """变量名是否属于「不该传给其它平台采集子进程」的凭据/调试类。"""
    upper = str(key).upper()
    if upper in _SYSTEM_ENV_ALLOW:
        return False
    if upper in _CREDENTIAL_ENV_NAMES or upper in _DEBUG_ENV_NAMES:
        return True
    if upper.startswith(_CREDENTIAL_ENV_PREFIXES):
        return True
    return upper.endswith(_CREDENTIAL_ENV_SUFFIXES)


def build_child_env(platform_credentials: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """构造采集子进程环境（D2 的唯一入口）。

    基底先走 ``upstream_config.sanitized_child_env()``（去除指向回环地址的代理），
    再剔除其它平台凭据 / 模型密钥 / 无关调试变量，最后**显式注入**本次平台凭据。
    ``PATH`` / ``SystemRoot`` 等系统必需变量始终保留。
    """
    from upstream_config import sanitized_child_env

    env = sanitized_child_env()
    for key in list(env):
        if is_secret_env_name(key):
            env.pop(key, None)
    env["PYTHONIOENCODING"] = "utf-8"
    for key, value in (platform_credentials or {}).items():
        name = str(key or "").strip()
        if not name or value is None or value == "":
            continue
        env[name] = str(value)
    return env


def redact(text: str) -> str:
    """把疑似凭据替换为 [REDACTED]，返回脱敏文本。"""
    from upstream_config import redact as base_redact
    text = re.sub(r'(?i)(xsec_token|auth_token|ct0)([\"\s]*[:=][\"\s]*)[^\s,;&\"\']+', r'\1=[REDACTED]', text)
    return base_redact(_SECRET_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", text))


def _safe_token(value: str) -> str:
    """拒绝空值、选项前缀、控制字符（防注入）。"""
    if not value or value.startswith("-") or re.search(r"[\r\n\x00]", value):
        raise ValueError(f"非法参数：{value!r}")
    return value


def _argv_prefix(executable: Path) -> list[str]:
    """把「可执行文件」解析成真正的启动前缀（C5 的运行时收口）。

    - ``.js`` → ``node + 该 js``；
    - ``.cmd`` / ``.ps1`` / ``.bat`` / 无后缀 shell shim → **不得**直接当可执行文件，
      改由 ``provider`` 解析出 ``node + main.js``（或真实 PE）；
    - 其余（含 ``.exe``）→ 直接启动。
    """
    suffix = executable.suffix.lower()
    if suffix == ".js":
        node = shutil.which("node")
        if not node:
            raise FileNotFoundError("Node runtime missing")
        return [node, str(executable)]
    if suffix in (".cmd", ".ps1", ".bat"):
        from collectors.provider import resolve_opencli_launch
        return resolve_opencli_launch(str(executable)).argv_prefix()
    if suffix == "":
        # 无后缀：只有确为真实可执行体（Windows 上带 PE 魔数）才直接启动；
        # 否则按 shell shim 处理（本机 which("opencli") 命中的正是这种 shim）。
        from collectors.provider import is_real_executable, resolve_opencli_launch
        if is_real_executable(executable):
            return [str(executable)]
        return resolve_opencli_launch(str(executable)).argv_prefix()
    return [str(executable)]


async def run_command(
    exe: str,
    subcommand: str,
    *,
    positional: Sequence[str] = (),
    options: Optional[dict[str, str | int | bool | None]] = None,
    allowed_commands: frozenset[str],
    platform: Optional[str] = None,
    timeout_seconds: int = 60,
    env: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """执行一次只读采集命令，返回
    ``{stdout, stderr, returncode, elapsed_ms, timed_out, argv_redacted}``。

    - 白名单校验：``subcommand`` 必须在 ``allowed_commands`` 内，否则抛 ValueError；
    - 输出**有界流式**读取：超过 ``MAX_STDOUT_BYTES`` / ``MAX_STDERR_BYTES``
      即终止本次子进程并抛 ``CollectorError('OUTPUT_LIMIT_EXCEEDED')``；
    - 子进程环境走 :func:`build_child_env`（D2）。
    """
    if subcommand not in allowed_commands:
        raise ValueError(f"命令 {subcommand} 不在只读白名单 {sorted(allowed_commands)} 内")

    executable = Path(exe).resolve()
    argv = _argv_prefix(executable)
    if platform is not None:
        if platform not in ("xiaohongshu", "twitter"):
            raise ValueError("Unsupported platform prefix")
        argv.append(platform)
    argv.append(subcommand)
    argv += [_safe_token(v) for v in positional]
    for name, value in (options or {}).items():
        if value is None or value is False or value == "":
            continue
        if value is True:
            argv.append(name)
        else:
            argv.extend([name, _safe_token(str(value))])

    started = time.monotonic()
    # 凭据只经 env 注入（显式），不进 argv；基底不含其它平台凭据与模型密钥。
    child_env = build_child_env(env)

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            env=child_env,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return {
            "stdout": "",
            "stderr": redact(f"{type(exc).__name__}: {exc}"),
            "returncode": -1,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "timed_out": False,
            "argv_redacted": [redact(a) for a in argv],
        }

    from upstream_config import communicate_bounded

    bounded = await communicate_bounded(
        process, timeout_seconds=timeout_seconds,
        max_stdout_bytes=MAX_STDOUT_BYTES, max_stderr_bytes=MAX_STDERR_BYTES)

    if bounded.any_exceeded:
        from collectors.contracts import CollectorError
        raise CollectorError(
            "OUTPUT_LIMIT_EXCEEDED",
            "上游输出超过上限，已终止本次子进程并丢弃超限内容（未按正常结果处理）",
            retryable=False,
            operator_action="缩小查询范围或减少采集数量后重试；若持续出现，请检查上游 CLI 是否异常刷屏",
            evidence={
                "stdout_exceeded": bounded.stdout_exceeded,
                "stderr_exceeded": bounded.stderr_exceeded,
                "limit_stdout_bytes": MAX_STDOUT_BYTES,
                "limit_stderr_bytes": MAX_STDERR_BYTES,
            },
        )

    stdout = bounded.stdout[:MAX_STDOUT_BYTES].decode("utf-8", errors="replace")
    stderr = bounded.stderr[:MAX_STDERR_BYTES].decode("utf-8", errors="replace")
    return {
        "stdout": stdout,
        "stderr": redact(stderr),
        "returncode": bounded.returncode,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "timed_out": bounded.timed_out,
        "argv_redacted": [redact(a) for a in argv],
    }


def parse_json_or_none(text: str) -> Optional[Any]:
    """尝试解析 JSON；失败返回 None（不抛异常，让上层按契约错处理）。"""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


__all__ = [
    "run_command", "redact", "parse_json_or_none",
    "build_child_env", "is_secret_env_name",
    "X_READ_COMMANDS", "XHS_READ_COMMANDS",
    "MAX_STDOUT_BYTES", "MAX_STDERR_BYTES",
]
