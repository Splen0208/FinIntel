"""Locked upstream CLI configuration, command construction, and execution."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlparse

UPSTREAM_SETTING_KEY = "upstream_data_dir"
UPSTREAM_ENV_VAR = "FININTEL_UPSTREAM_DATA_DIR"
DEFAULT_UPSTREAM_REL = Path(".local") / "upstream"
PROJECT_DIR = Path(__file__).resolve().parent.parent

# 上游子进程相关的运行日志出口（诊断用；不含任何凭据）
logger = logging.getLogger("finintel.upstream_config")

TOP_LEVEL_OPTIONS: frozenset[str] = frozenset({
    "--data-dir", "--json", "--delay", "--log-level", "--cdp-url",
    "--browser-source", "--platform", "--role",
})
# ---------------------------------------------------------------------------
# 采集通道：允许改走「用户自己已登录的浏览器」（CDP）
# ---------------------------------------------------------------------------
# 只有这两个顶层选项允许透传，且必须放在子命令之前；登录命令不适用
# （LOGIN_FORBIDDEN_OPTIONS 已显式封禁，登录仍走官方可见浏览器）。
BROWSER_SOURCE_OPTION = "--browser-source"
CDP_URL_OPTION = "--cdp-url"
# 登录命令的「接到专用浏览器」开关：布尔选项，与 ``--cdp-url`` 成对出现。
# 二者都在 ``LOGIN_FORBIDDEN_OPTIONS`` 内，唯一例外见 ``build_login_argv_with_cdp``。
CDP_CALL_OPTION = "--cdp"
PASSTHROUGH_TOP_LEVEL_OPTIONS: frozenset[str] = frozenset({
    BROWSER_SOURCE_OPTION, CDP_URL_OPTION,
})
# 上游 browser_source.py POLICIES 的三种来源，不允许自造取值
BROWSER_SOURCE_VALUES: frozenset[str] = frozenset({"auto", "stored-cookie", "existing-browser"})
# 调试地址只接受回环地址：禁止把本机采集会话导流到别的机器
LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})
DEFAULT_CDP_URL = "http://127.0.0.1:9222"
BROWSER_SOURCE_SETTING_KEY = "boss_browser_source"
CDP_URL_SETTING_KEY = "boss_cdp_url"
READ_ONLY_SUBCOMMANDS: frozenset[str] = frozenset({
    "schema", "doctor", "status", "cities", "platforms", "search", "detail",
})
SUBCOMMAND_POSITIONALS: dict[str, tuple[int, int]] = {
    "schema": (0, 0),
    "doctor": (0, 0),
    "status": (0, 0),
    "cities": (0, 0),
    "platforms": (0, 0),
    "search": (0, 1),
    "detail": (1, 1),
}
SUBCOMMAND_OPTIONS: dict[str, frozenset[str]] = {
    "schema": frozenset({"--format"}),
    "doctor": frozenset({"--live-probe"}),
    "status": frozenset({"--live"}),
    "cities": frozenset(),
    "platforms": frozenset({"--platform", "--capability"}),
    "search": frozenset({
        "--url", "--city", "--salary", "--experience", "--education",
        "--industry", "--scale", "--stage", "--job-type", "--welfare",
        "--page", "--with-score", "--sort", "--no-cache",
    }),
    "detail": frozenset({"--job-id", "--lid"}),
}

# ---------------------------------------------------------------------------
# 登录命令（唯一一条允许的非只读命令，设计 §5.1）
# ---------------------------------------------------------------------------

LOGIN_SUBCOMMAND = "login"
LOGIN_DEFAULT_TIMEOUT_SECONDS = 180
# 用户可见的请求范围（API 层用它校验 timeout_seconds）
LOGIN_TIMEOUT_BOUNDS: tuple[int, int] = (30, 600)
# argv 工厂的下限：允许用极短超时跑桥接自证（真实窗口 8 秒即可打开），
# 用户可请求的范围仍由 LOGIN_TIMEOUT_BOUNDS 收敛。
LOGIN_MIN_INTERNAL_TIMEOUT_SECONDS = 5
LOGIN_ALLOWED_OPTIONS: frozenset[str] = frozenset({"--timeout"})
# 一律不允许出现在登录命令里的能力放大选项（--force 只作诊断页人工动作）
LOGIN_FORBIDDEN_OPTIONS: frozenset[str] = frozenset({
    "--force", "--cdp", "--cdp-url", "--cookie-source",
    "--curl-file", "--browser-source", "--platform",
})

_CONTROL_CHAR_RE = re.compile(r"[\x00\r\n]")
# 「单 token」凭据对：这些键的值本来就只是一个 token（token=xxx / api_key: sk-xx /
# stoken=… / wt2=…）。value 组只吃一个 token 是**刻意**的：`token: not found` 这类
# 诊断文本必须原样保留，泛化成「吃到行尾」会把诊断能力修废。
_SECRET_PAIR_RE = re.compile(
    r"(?i)(token|api[_-]?key|secret|stoken|wt2)\s*([=:])\s*([^\s,;]+)"
)
# 「行级」凭据对（兜底）：值里**带空格与分号**的键，单 token 组只吃第一段，后面照旧落库。
# 真机反例（QA 实测）：`user_agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)
# AppleWebKit/537.36 …` —— 单 token 组只吃掉 `Mozilla/5.0`，后面整串仍然落地。
# 所以 set-cookie / cookie / authorization / x-auth* / user_agent 一律「键后吃到行尾」。
# 分隔符容许 JSON 形状（`"authorization": "Bearer …"`）：上游 CLI 的 stderr 里就是 JSON 行，
# 而引号会夹在键与冒号之间，不容许的话这条兜底在最常见的落库路径上会**整条失效**。
# 代价是 JSON 行的收尾引号/花括号一并被吃掉——诊断展示可接受（失败关闭）。
# 只对「值必然敏感」的键生效，**绝不泛化**：`Page.goto: Timeout 15000ms exceeded.`
# 之类的诊断文本必须逐字不变（见 tests 里的反向断言）。
_SECRET_LINE_RE = re.compile(
    r'(?im)(set-cookie|cookie|authorization|x-auth[a-z0-9_-]*|user[_-]?agent)'
    r'("?\s*[=:]\s*"?)\s*[^\r\n]*'
)
# 第二层：没有 authorization 键、只剩裸 `Bearer eyJ…` 时兜住
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
# 32 位以上十六进制串（token / session id 形状）。**刻意不用 `\b`**：`\b` 依赖
# 「词字符 / 非词字符」边界，而 CJK 与字母**也是词字符**——`会话失效<32hex>已过期`、
# `stoken<32hex>`（无 `=`/`:`）这类真实形状下 `\b` 根本不成立，整串会漏网（真机缺陷）。
# 用「前后不是十六进制字符」的定界代替：既兜住紧贴词字符的情形，也不会把更长的
# 十六进制串从中间截断脱敏（从第一个非 hex 前的位置匹配到最后一个）。
_LONG_TOKEN_RE = re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{32,}(?![0-9a-f])")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


@dataclass(frozen=True)
class DataDirResolution:
    """Resolved upstream state directory and its configuration origin."""

    path: Path
    source: str
    raw: Optional[str]
    created: bool

    def as_view(self) -> dict[str, object]:
        """Return a credential-free API representation."""
        return {
            "path": str(self.path),
            "source": self.source,
            "raw": self.raw,
            "exists": self.path.exists(),
        }


@dataclass(frozen=True)
class RawInvocation:
    """Complete result of one bounded CLI subprocess call."""

    argv: list[str]
    argv_redacted: list[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed_ms: int
    timed_out: bool
    data_dir: str
    # 输出超过上限时，本次子进程已被终止、stdout/stderr 只保留到上限（D1）。
    # 追加字段带默认值，不破坏既有构造调用。
    output_truncated: bool = False


def _default_setting_getter(key: str) -> Optional[str]:
    import store

    try:
        row = store.query_one("SELECT value FROM app_setting WHERE key=?", (key,))
    except Exception:
        return None
    return row["value"] if row else None


def resolve_upstream_data_dir(
    *,
    create: bool = True,
    setting_getter: Optional[Callable[[str], Optional[str]]] = None,
) -> DataDirResolution:
    """Resolve the single upstream data directory.

    Priority is database setting, environment variable, then the project-local
    default. Relative values are interpreted relative to the project root.
    """
    getter = setting_getter or _default_setting_getter
    setting_value = getter(UPSTREAM_SETTING_KEY)
    environment_value = os.environ.get(UPSTREAM_ENV_VAR)
    if setting_value and setting_value.strip():
        raw = setting_value.strip()
        source = "setting"
    elif environment_value and environment_value.strip():
        raw = environment_value.strip()
        source = "env"
    else:
        raw = None
        source = "default"

    candidate = Path(raw).expanduser() if raw is not None else PROJECT_DIR / DEFAULT_UPSTREAM_REL
    if not candidate.is_absolute():
        candidate = PROJECT_DIR / candidate
    path = candidate.resolve()
    existed = path.exists()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return DataDirResolution(path=path, source=source, raw=raw, created=create and not existed)


def same_path(a: str | Path, b: str | Path) -> bool:
    """Compare paths using platform-aware case and separator normalization."""
    return os.path.normcase(os.path.realpath(os.fspath(a))) == os.path.normcase(
        os.path.realpath(os.fspath(b))
    )


def assert_upstream_dir(echoed: Optional[str], expected: Path) -> None:
    """Reject a CLI response that reports a different effective data directory."""
    from upstream_envelope import FailureLayer, UpstreamError

    if not echoed or not same_path(echoed, expected):
        raise UpstreamError(
            "DATA_DIR_MISMATCH",
            "上游实际使用的数据目录与 finintel 当前生效目录不一致",
            False,
            f"请使用同一绝对目录完成登录和采集：{expected}",
            layer=FailureLayer.CONTRACT,
            evidence={"expected_data_dir": str(expected), "upstream_data_dir": echoed or ""},
        )


def resolve_cli_path(configured: Optional[str]) -> Optional[str]:
    """Resolve the configured CLI, current interpreter sibling, or PATH entry."""
    if configured and configured.strip():
        candidate = Path(configured.strip()).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        found = shutil.which(configured.strip())
        return str(Path(found).resolve()) if found else None

    import sys

    executable_name = "boss.exe" if os.name == "nt" else "boss"
    sibling = Path(sys.executable).resolve().parent / executable_name
    if sibling.is_file():
        return str(sibling)
    found = shutil.which("boss") or shutil.which("boss.exe")
    return str(Path(found).resolve()) if found else None


def _safe_token(value: object, *, label: str) -> str:
    token = str(value)
    if not token or token.startswith("-") or _CONTROL_CHAR_RE.search(token):
        from upstream_envelope import FailureLayer, UpstreamError

        raise UpstreamError(
            "TOOL_NOT_ALLOWED",
            f"上游参数 {label} 含空值、选项前缀或控制字符，已拒绝",
            False,
            "检查调用参数；位置参数和选项值不得以连字符开头，也不得包含换行",
            layer=FailureLayer.CONTRACT,
        )
    return token


def _validate_browser_source(value: object) -> str:
    """校验 ``--browser-source``：只允许上游已定义的三种采集来源。"""
    from upstream_envelope import FailureLayer, UpstreamError

    token = _safe_token(value, label=BROWSER_SOURCE_OPTION)
    if token not in BROWSER_SOURCE_VALUES:
        raise UpstreamError(
            "TOOL_NOT_ALLOWED",
            f"采集浏览器来源只允许 {sorted(BROWSER_SOURCE_VALUES)}，实际为 {token}",
            False,
            "在诊断页把采集浏览器来源改回「自动」，或选择其中一种合法来源",
            layer=FailureLayer.CONTRACT,
        )
    return token


def _validate_cdp_url(value: object) -> str:
    """校验 ``--cdp-url``：必须是 http(s)，且只允许指向本机回环地址。"""
    from urllib.parse import urlparse

    from upstream_envelope import FailureLayer, UpstreamError

    token = _safe_token(value, label=CDP_URL_OPTION)
    parsed = urlparse(token)
    host = (parsed.hostname or "").strip().lower()
    if parsed.scheme not in {"http", "https"} or not host:
        raise UpstreamError(
            "TOOL_NOT_ALLOWED",
            f"采集浏览器的调试地址必须是 http:// 或 https:// 开头的完整地址，实际为 {token}",
            False,
            f"改回默认地址 {DEFAULT_CDP_URL}，或在诊断页填写本机地址",
            layer=FailureLayer.CONTRACT,
        )
    if host not in LOOPBACK_HOSTS or parsed.username or parsed.password:
        raise UpstreamError(
            "TOOL_NOT_ALLOWED",
            "采集浏览器的调试地址只允许指向本机（localhost / 127.0.0.1），不得指向其他机器",
            False,
            f"改回默认地址 {DEFAULT_CDP_URL}，或填写本机地址",
            layer=FailureLayer.CONTRACT,
        )
    return token


def browser_option_tokens(options: Optional[Mapping[str, str]] = None) -> list[str]:
    """把采集通道的两个顶层选项拼成 argv 片段（调用方负责放在子命令之前）。

    空值表示该项未启用，直接跳过；键不在白名单内一律拒绝。
    """
    from upstream_envelope import FailureLayer, UpstreamError

    tokens: list[str] = []
    for name, value in (options or {}).items():
        if name not in PASSTHROUGH_TOP_LEVEL_OPTIONS:
            raise UpstreamError(
                "TOOL_NOT_ALLOWED",
                f"不允许透传的顶层选项 {name}",
                False,
                f"只允许透传 {sorted(PASSTHROUGH_TOP_LEVEL_OPTIONS)}；其余顶层选项由 build_argv 固定生成",
                layer=FailureLayer.CONTRACT,
            )
        if value is None or str(value).strip() == "":
            continue
        if name == BROWSER_SOURCE_OPTION:
            tokens.extend([name, _validate_browser_source(value)])
        else:
            tokens.extend([name, _validate_cdp_url(value)])
    return tokens


def resolve_browser_options(
    *,
    setting_getter: Optional[Callable[[str], Optional[str]]] = None,
) -> dict[str, str]:
    """读取「用用户自己的浏览器采集」的两个设置；留空即不启用（返回空字典）。

    只有显式配置了来源才下发顶层选项；调试地址仅在来源已配置时一起下发，
    避免把默认 auto 通道意外改到依赖本地凭据的路径上。

    **收口说明（迁移 009 后）**：采集链路（search / detail）一律改用
    ``channel_options_from_snapshot()`` 读任务冻结的通道，禁止在采集时回落到
    全局设置——否则换设置就会让同一任务的 search 与 detail 中途偷换通道。
    本函数只在「创建任务时冻结通道」与「诊断页预检」这两个时机读一次。
    """
    getter = setting_getter or _default_setting_getter
    source = (getter(BROWSER_SOURCE_SETTING_KEY) or "").strip()
    if not source:
        return {}
    options = {BROWSER_SOURCE_OPTION: source}
    cdp_url = (getter(CDP_URL_SETTING_KEY) or "").strip()
    if cdp_url:
        options[CDP_URL_OPTION] = cdp_url
    return options


def channel_options_from_snapshot(channel: Optional[Mapping[str, Any]]) -> dict[str, str]:
    """把任务冻结的通道快照翻译成顶层选项；**绝不**读取全局设置。

    - ``auto``（或留空）不上报 ``--browser-source``：与历史默认 argv 逐项一致；
    - 其余来源照原样下发，调试地址仅在显式配置时下发；
    - 通道未冻结（None / 空）一律抛契约错：调用方必须先冻结通道，
      彻底堵死「漏传 → 回落全局设置 → 中途换通道」的口子。
    """
    from upstream_envelope import FailureLayer, UpstreamError

    if not isinstance(channel, Mapping) or not channel:
        raise UpstreamError(
            "CHANNEL_NOT_FROZEN",
            "采集通道未冻结，不能发起 search/detail",
            False,
            "先由任务冻结通道（config_snapshot.channel），再把冻结值显式传给 search/detail",
            layer=FailureLayer.CONTRACT,
        )
    source = str(channel.get("browser_source") or "").strip()
    cdp_url = str(channel.get("cdp_url") or "").strip()
    options: dict[str, str] = {}
    if source and source != "auto":
        options[BROWSER_SOURCE_OPTION] = _validate_browser_source(source)
    if cdp_url:
        options[CDP_URL_OPTION] = _validate_cdp_url(cdp_url)
    return options


def validate_browser_option(name: str, value: str) -> None:
    """校验设置页写入的采集通道选项；空值表示关闭该通道，一律放行。"""
    from upstream_envelope import FailureLayer, UpstreamError

    text = (value or "").strip()
    if not text:
        return
    if name == BROWSER_SOURCE_SETTING_KEY:
        _validate_browser_source(text)
        return
    if name == CDP_URL_SETTING_KEY:
        _validate_cdp_url(text)
        return
    raise UpstreamError(
        "TOOL_NOT_ALLOWED",
        f"不支持的采集通道设置项 {name}",
        False,
        f"只允许 {BROWSER_SOURCE_SETTING_KEY} / {CDP_URL_SETTING_KEY}",
        layer=FailureLayer.CONTRACT,
    )


def build_argv(
    cli_path: str,
    subcommand: str,
    *,
    data_dir: Path,
    positional: Sequence[str] = (),
    options: Optional[Mapping[str, str | int | bool | None]] = None,
    top_level_options: Optional[Mapping[str, str]] = None,
) -> list[str]:
    """Build the only allowed argv shape for read-only upstream commands.

    ``top_level_options`` 只允许 ``--browser-source`` / ``--cdp-url`` 两个键，
    必须放在子命令之前（上游 CLI 的全局选项位置要求）；不传时 argv 与历史行为
    逐项一致。
    """
    from upstream_envelope import FailureLayer, UpstreamError

    if subcommand not in READ_ONLY_SUBCOMMANDS:
        raise UpstreamError(
            "TOOL_NOT_ALLOWED",
            f"上游命令不在只读白名单内：{subcommand}",
            False,
            "本项目只允许 schema/doctor/status/cities/platforms/search/detail",
            layer=FailureLayer.CONTRACT,
        )
    if not cli_path or _CONTROL_CHAR_RE.search(cli_path):
        raise UpstreamError(
            "DEPENDENCY_MISSING",
            "boss CLI 路径为空或无效",
            False,
            install_command_hint(),
            layer=FailureLayer.TRANSPORT,
        )
    minimum, maximum = SUBCOMMAND_POSITIONALS[subcommand]
    if not minimum <= len(positional) <= maximum:
        raise UpstreamError(
            "TOOL_NOT_ALLOWED",
            f"{subcommand} 位置参数数量必须为 {minimum}~{maximum}，实际为 {len(positional)}",
            False,
            "按锁定版本 2.0.0 的命令契约修正调用",
            layer=FailureLayer.CONTRACT,
        )

    normalized_data_dir = data_dir.resolve()
    argv = [cli_path, "--data-dir", str(normalized_data_dir), "--json"]
    argv.extend(browser_option_tokens(top_level_options))
    argv.append(subcommand)
    argv.extend(_safe_token(value, label=f"{subcommand} positional") for value in positional)
    for name, value in (options or {}).items():
        if name in TOP_LEVEL_OPTIONS or name not in SUBCOMMAND_OPTIONS[subcommand]:
            raise UpstreamError(
                "TOOL_NOT_ALLOWED",
                f"{subcommand} 不允许选项 {name}",
                False,
                "顶层 --data-dir/--json 由 build_argv 固定生成；其余选项须在子命令白名单内",
                layer=FailureLayer.CONTRACT,
            )
        if value is None or value == "" or value is False:
            continue
        if value is True:
            argv.append(name)
            continue
        argv.extend([name, _safe_token(value, label=name)])
    return argv


def _validate_login_timeout(value: object) -> int:
    from upstream_envelope import FailureLayer, UpstreamError

    try:
        timeout = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise UpstreamError(
            "TOOL_NOT_ALLOWED",
            f"登录超时必须是整数，实际为 {value!r}",
            False,
            f"登录超时取 {LOGIN_MIN_INTERNAL_TIMEOUT_SECONDS}~{LOGIN_TIMEOUT_BOUNDS[1]} 秒",
            layer=FailureLayer.CONTRACT,
        ) from None
    if not LOGIN_MIN_INTERNAL_TIMEOUT_SECONDS <= timeout <= LOGIN_TIMEOUT_BOUNDS[1]:
        raise UpstreamError(
            "TOOL_NOT_ALLOWED",
            f"登录超时 {timeout} 秒超出允许范围",
            False,
            f"登录超时取 {LOGIN_MIN_INTERNAL_TIMEOUT_SECONDS}~{LOGIN_TIMEOUT_BOUNDS[1]} 秒",
            layer=FailureLayer.CONTRACT,
        )
    return timeout


def _assert_no_forbidden_login_options(tokens: Sequence[str], *,
                                       allow_cdp_url: Optional[str] = None) -> None:
    """登录 argv 的**唯一**守卫：默认封禁全部能力放大选项。

    历史上 ``managed_browser.login_argv`` 在 ``build_login_argv`` 之后**自行拼接**
    ``--cdp-url`` / ``--cdp``，把本守卫整体绕开（真机隐患）。现在只保留一个显式例外：
    ``allow_cdp_url`` 指向**已校验**的、本项目专用浏览器的回环 CDP 端点时，放行恰好一对
    ``--cdp-url <allow_cdp_url>`` 与 ``--cdp``。该参数**只由** ``build_login_argv_with_cdp``
    传入，其余任何路径（含 ``build_login_argv`` 自身）都走严格模式，
    ``--force / --cookie-source / --curl-file / --browser-source / --platform`` 一律拒绝。
    """
    from upstream_envelope import FailureLayer, UpstreamError

    for index, token in enumerate(tokens):
        parts = re.split(r"\s+", str(token))
        for offset, part in enumerate(parts):
            key = part.split("=", 1)[0]
            if key not in LOGIN_FORBIDDEN_OPTIONS:
                continue
            if allow_cdp_url and key == CDP_CALL_OPTION:
                continue
            if allow_cdp_url and key == CDP_URL_OPTION:
                if "=" in part:
                    value = part.split("=", 1)[1]
                elif offset + 1 < len(parts):
                    value = parts[offset + 1]
                else:
                    value = str(tokens[index + 1]) if index + 1 < len(tokens) else ""
                if value == allow_cdp_url:
                    continue
            raise UpstreamError(
                "TOOL_NOT_ALLOWED",
                f"登录命令不允许选项 {key}",
                False,
                "登录只允许 --timeout；--force/--cdp/--cookie-source 等一律不开放",
                layer=FailureLayer.CONTRACT,
            )


def build_login_argv(
    cli_path: str,
    data_dir: Path,
    *,
    timeout_seconds: int = LOGIN_DEFAULT_TIMEOUT_SECONDS,
    options: Optional[Mapping[str, str | int | bool | None]] = None,
) -> list[str]:
    """Build the single allowed non-read-only login command.

    形状固定、无位置参数，选项白名单只有 ``--timeout``：
        [cli_path, "--data-dir", <abs>, "--json", "login", "--timeout", <n>]

    拒绝 ``--force / --cdp / --cdp-url / --cookie-source / --curl-file /
    --browser-source / --platform``，从结构上杜绝命令注入与能力放大。
    """
    from upstream_envelope import FailureLayer, UpstreamError

    if not cli_path or _CONTROL_CHAR_RE.search(cli_path):
        raise UpstreamError(
            "DEPENDENCY_MISSING",
            "boss CLI 路径为空或无效",
            False,
            install_command_hint(),
            layer=FailureLayer.TRANSPORT,
        )
    _assert_no_forbidden_login_options([cli_path])
    resolved_timeout = _validate_login_timeout(timeout_seconds)
    argv = [cli_path, "--data-dir", str(data_dir.resolve()), "--json", LOGIN_SUBCOMMAND]
    for name, value in (options or {}).items():
        if name not in LOGIN_ALLOWED_OPTIONS:
            raise UpstreamError(
                "TOOL_NOT_ALLOWED",
                f"登录命令不允许选项 {name}",
                False,
                "登录只允许 --timeout；--force/--cdp/--cookie-source 等一律不开放",
                layer=FailureLayer.CONTRACT,
            )
        if name == "--timeout":
            resolved_timeout = _validate_login_timeout(value)
    argv.extend(["--timeout", str(resolved_timeout)])
    _assert_no_forbidden_login_options(argv)
    return argv


def build_login_argv_with_cdp(base_argv: Sequence[str], endpoint: str) -> list[str]:
    """本项目**专用浏览器**登录的**唯一** argv 出口：显式、已校验地接入该浏览器。

    ``build_login_argv`` 刻意封禁 ``--cdp-url`` / ``--cdp``（防命令注入与能力放大），
    但专用浏览器登录确实需要这两个选项把官方登录流程接到**本项目自己的**浏览器上。
    历史实现在 ``managed_browser`` 里对 argv 事后拼接、绕开了守卫，这里收口为一个显式入口。

    放行条件（三者缺一不可）：

    1. 端点必须是 http(s) 回环地址（``_validate_cdp_url``，与采集侧同一套规则）；
    2. 端点必须**就是本项目专用 profile 当前活着的 CDP 端点**——用 ``DevToolsActivePort``
       + 浏览器 UUID 校验（``managed_browser.endpoint_for_profile``）；端口对不上、或换了
       另一个浏览器，一律拒绝，杜绝把会话导流到别的机器或陈旧端口；
    3. 注入后再由 ``_assert_no_forbidden_login_options`` 复核——守卫是**唯一**判定点：
       只放行本函数显式注入的 ``--cdp-url <endpoint>`` 与 ``--cdp``。

    返回：``base_argv`` 的 ``login`` 子命令**之前**插入 ``--cdp-url <endpoint>``，
    末尾追加 ``--cdp``（符合上游 CLI 全局选项的位置要求）。
    """
    from upstream_envelope import FailureLayer, UpstreamError

    tokens = [str(token) for token in base_argv]
    if LOGIN_SUBCOMMAND not in tokens:
        raise UpstreamError(
            "TOOL_NOT_ALLOWED",
            "登录 argv 缺少 login 子命令，拒绝注入 CDP 选项",
            False,
            "先用 build_login_argv() 生成基础 argv，再交给本函数注入专用浏览器 CDP",
            layer=FailureLayer.CONTRACT,
        )
    validated = _validate_cdp_url(endpoint)

    # 延迟导入：避免 upstream_config <-> managed_browser 顶层循环依赖。
    import managed_browser

    live_endpoint = managed_browser.endpoint_for_profile(managed_browser.PROFILE)
    if not live_endpoint or str(live_endpoint).rstrip("/") != validated.rstrip("/"):
        raise UpstreamError(
            "TOOL_NOT_ALLOWED",
            "登录 CDP 端点与本项目专用浏览器的活动端点不一致，已拒绝注入",
            False,
            "只允许把登录接到本项目专用浏览器：请先打开该专用窗口再点击「登录 BOSS」",
            layer=FailureLayer.CONTRACT,
        )

    index = tokens.index(LOGIN_SUBCOMMAND)
    injected = (tokens[:index] + [CDP_URL_OPTION, validated, LOGIN_SUBCOMMAND]
                + tokens[index + 1:] + [CDP_CALL_OPTION])
    _assert_no_forbidden_login_options(injected, allow_cdp_url=validated)
    return injected


def redact(text: str) -> str:
    """Remove common credential and personal-token shapes from text.

    顺序很重要：先跑**行级**兜底（cookie / set-cookie / authorization / x-auth* /
    user_agent 的值可能带空格与分号，必须吃到行尾），再跑单 token 凭据对，最后是
    裸 Bearer、长十六进制 token 与手机号。
    """
    sanitized = _SECRET_LINE_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
    sanitized = _SECRET_PAIR_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", sanitized)
    sanitized = _BEARER_RE.sub("Bearer [REDACTED]", sanitized)
    sanitized = _LONG_TOKEN_RE.sub("[REDACTED_TOKEN]", sanitized)
    sanitized = _PHONE_RE.sub("1**********", sanitized)
    return sanitized


def redact_argv(argv: Sequence[str]) -> list[str]:
    """Return an independently redacted argv copy for persistence."""
    return [redact(str(token)) for token in argv]


def shell_quote_for_human(argv: Sequence[str]) -> str:
    """Render a copyable Windows-safe command for human diagnostics only."""
    return subprocess.list2cmdline(list(argv))


def login_command_hint(cli_path: str, data_dir: Path) -> str:
    """Return the human-only interactive login command."""
    return shell_quote_for_human([cli_path, "--data-dir", str(data_dir.resolve()), "login"])


def install_command_hint() -> str:
    """Return the pinned installation command using the active interpreter."""
    import sys

    return shell_quote_for_human([
        str(Path(sys.executable).resolve()), "-m", "pip", "install", "boss-agent-cli==2.0.0",
    ])


# ---------------------------------------------------------------------------
# 子进程环境净化：只清「回环代理」，绝不碰用户有意配置的代理
# ---------------------------------------------------------------------------
# 背景（code 37「您的环境存在异常」的根因候选）：某些运行环境（如工具沙箱）会注入
# ``http_proxy`` / ``https_proxy`` = ``http://127.0.0.1:<port>``。上游 CLI 走 httpx 的
# 通道（``status --live`` / detail / user_info）默认 ``trust_env=True``，于是**经本地代理**
# 出网；而浏览器通道（Chrome）在 Windows 上只认**系统代理**（本项目 ``ProxyEnable=0``），
# 走的是**真实网络**。同一登录会话因此出现**两个出口 IP**，与平台 code 37「环境异常」的
# 语义吻合。
#
# 取舍（**精确、不扩大**）：仅当代理变量**指向回环地址**（127.0.0.1 / localhost / ::1）时
# 才从子进程环境删除——那是运行环境自己注入的、本不该有的本地代理跳转。用户/公司**有意**
# 配置的非回环代理（如公司网关 ``http://10.x.x.x:8080``）必须**原样保留**，删掉会把用户的
# 网络弄坏。目的不是绕过平台风控，而是让**平台流量的出口与浏览器通道一致**，消除「同一会话
# 两个出口」这一异常。
PROXY_ENV_VAR_NAMES: frozenset[str] = frozenset({
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
})
# 判定为「回环」的主机名（与 ``LOOPBACK_HOSTS`` 同源，额外含去括号形式）
_PROXY_LOOPBACK_HOSTS: frozenset[str] = frozenset({
    "localhost", "127.0.0.1", "::1", "[::1]",
})
# 记录上一次净化实际删掉的变量名（诊断用，非真值来源）
_last_stripped_proxy_keys: tuple[str, ...] = ()


def _is_loopback_proxy(value: str) -> bool:
    """判断一个代理变量值是否**指向回环地址**。

    兼容 ``host:port``、``scheme://host:port`` 两种写法；``hostname`` 对 ``[::1]`` 会去掉
    方括号返回 ``::1``。**多条目**的值（如 ``no_proxy=localhost,127.0.0.1`` 这类绕过清单）
    整体解析出的 host 不会命中回环集合，因而**保守地保留**——不误伤用户有意写的绕过清单。
    """
    candidate = (value or "").strip()
    if not candidate:
        return False
    if "://" not in candidate:
        candidate = "//" + candidate
    host = (urlparse(candidate).hostname or "").lower()
    return host in _PROXY_LOOPBACK_HOSTS


def loopback_proxy_keys(env: Mapping[str, str]) -> list[str]:
    """返回 ``env`` 中**指向回环地址**的代理变量名（大小写不敏感）。

    净化与诊断共用这**唯一**判据，避免两处逻辑漂移。
    """
    return sorted(
        key for key, value in env.items()
        if key.lower() in PROXY_ENV_VAR_NAMES and _is_loopback_proxy(str(value))
    )


def env_proxy_stripped() -> bool:
    """当前进程环境里是否存在「将被从子进程删除」的回环代理（供 ``/api/health`` 诊断）。

    返回 True 表示：本运行环境确实注入了回环代理，上游子进程已（且将持续）被净化，
    出口由此与浏览器通道保持一致。
    """
    return bool(loopback_proxy_keys(os.environ))


def last_stripped_proxy_keys() -> tuple[str, ...]:
    """上一次 ``sanitized_child_env()`` 实际删除的变量名（诊断用）。"""
    return _last_stripped_proxy_keys


def sanitized_child_env(base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """返回 spawn 上游子进程用的环境副本：**只**删除指向回环地址的代理变量。

    这是所有 spawn 上游 CLI / 专用浏览器子进程的**唯一**环境来源——任何 spawn 点都不得
    再直接使用裸的 ``os.environ`` 拷贝（有源码级测试守护）。被清理时会写一条运行日志
    （仅变量名，不含值），便于诊断。
    """
    global _last_stripped_proxy_keys
    env = dict(os.environ if base is None else base)
    stripped = loopback_proxy_keys(env)
    for key in stripped:
        env.pop(key, None)
    stripped_tuple = tuple(stripped)
    if stripped_tuple and stripped_tuple != _last_stripped_proxy_keys:
        logger.warning(
            "已从上游子进程环境移除回环代理变量 %s（使平台出口与浏览器通道一致）",
            ", ".join(stripped_tuple),
        )
    _last_stripped_proxy_keys = stripped_tuple
    return env


# ---------------------------------------------------------------------------
# worker（``backend/boss_browser_worker.py``）的位置参数遮蔽
# ---------------------------------------------------------------------------
# F7：worker 的位置参数只有 **两个** —— ``securityId`` 与 ``lid``（见
# ``boss_browser_worker.main()`` 的 argparse positional）。它们是平台内部标识：
# 形状不固定（既不一定是 32 位十六进制，也不一定带 ``token=`` 之类的键），
# 因此 ``redact()`` 的**形态猜测**规则可能整条漏掉。
#
# 所以这里不用形态猜测：``--`` 之后的所有 token **一律整段替换**为占位符，
# 不保留原值的任何片段。「遮蔽哪个参数」由位置（argparse positional 的顺序）
# 决定，不受将来新增/改名影响。
WORKER_POSITIONAL_SEPARATOR = "--"
WORKER_POSITIONAL_LABELS: tuple[str, ...] = ("securityId", "lid")


def redact_worker_argv(
    argv: Sequence[str],
    *,
    labels: Sequence[str] = WORKER_POSITIONAL_LABELS,
) -> list[str]:
    """Return a redacted argv copy for worker invocations.

    与 ``redact_argv`` 的区别：``--`` 之后的位置参数被**整段替换**为
    ``[REDACTED_<label>]``（label 缺省时被降级为 ``positional{n}``），而不是
    交给 ``redact()`` 按形态猜测。
    """
    redacted = [redact(str(token)) for token in argv]
    try:
        separator_index = len(argv) - argv[::-1].index(WORKER_POSITIONAL_SEPARATOR) - 1
    except ValueError:
        return redacted
    for offset in range(separator_index + 1, len(argv)):
        position = offset - separator_index
        label = labels[position - 1] if 0 < position <= len(labels) else f"positional{position}"
        redacted[offset] = f"[REDACTED_{label}]"
    return redacted


# ---------------------------------------------------------------------------
# 有界流式读取子进程输出（D1）：不再 ``communicate()`` 读完再判大小
# ---------------------------------------------------------------------------
# 旧实现先 ``process.communicate()`` 把整段 stdout/stderr 读进内存，**再**判断是否超限。
# 那只防"解析截断的 JSON"，**挡不住**一个持续输出 8MB 的子进程把内存撑爆。
#
# 现在改成**有界并发读取**：stdout/stderr 各起一个读任务分片读取，累计**超过**上限
# 立即 ``kill()`` **本次**子进程并回收。三种竞争都要覆盖：
#   - 读超时（进程不退出）→ kill + 回收 + ``timed_out=True``；
#   - 进程先退出 → 读到 EOF → ``await process.wait()`` 取 returncode；
#   - 达到上限 → kill + 回收 + ``*_exceeded=True``。
# **只** kill 本次 ``Popen`` 对象，绝不按端口/进程名关闭浏览器或别的任务。
MAX_STDOUT_BYTES = 2 * 1024 * 1024
MAX_STDERR_BYTES = 256 * 1024
_READ_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class BoundedOutput:
    """一次有界子进程读取的结果（``*_exceeded`` 为 True 时内容已截断到上限）。"""

    stdout: bytes
    stderr: bytes
    returncode: int
    timed_out: bool
    stdout_exceeded: bool
    stderr_exceeded: bool

    @property
    def any_exceeded(self) -> bool:
        return bool(self.stdout_exceeded or self.stderr_exceeded)


def _kill_quietly(process: object) -> None:
    """杀掉**本次**子进程；已退出或对象不支持 kill 时静默。"""
    killer = getattr(process, "kill", None)
    if killer is None:
        return
    try:
        killer()
    except (ProcessLookupError, PermissionError, OSError, AttributeError, RuntimeError):
        pass


def _supports_bounded_streaming(process: object) -> bool:
    """进程对象是否暴露可异步读取的 stdout/stderr（真实 asyncio 子进程会）。"""
    return (getattr(process, "stdout", None) is not None
            and getattr(process, "stderr", None) is not None)


async def _reap(process: object, *, timeout_seconds: int = 5) -> None:
    """回收本次子进程，避免僵尸；回收不了就再杀一次。"""
    waiter = getattr(process, "wait", None)
    if waiter is None:
        return
    try:
        await asyncio.wait_for(waiter(), timeout=max(1, int(timeout_seconds)))
    except (asyncio.TimeoutError, ProcessLookupError, OSError):
        _kill_quietly(process)


def _close_pipe_transports(process: object) -> None:
    """主动关闭本次子进程的管道 transport（只影响本次 Popen）。

    终止/取消后若仍有未读完的管道，Proactor 会在 GC 时报 ``unclosed transport``。
    这里显式关掉本次进程的 stdin/stdout/stderr transport；测试替身没有 ``_transport``
    时静默跳过。
    """
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(process, name, None)
        transport = getattr(stream, "_transport", None)
        if transport is None:
            continue
        try:
            transport.close()
        except (OSError, RuntimeError, ValueError):  # pragma: no cover - 关闭竞态
            pass


async def communicate_bounded(
    process: object,
    *,
    timeout_seconds: int,
    max_stdout_bytes: int = MAX_STDOUT_BYTES,
    max_stderr_bytes: int = MAX_STDERR_BYTES,
) -> BoundedOutput:
    """有界并发读取 stdout/stderr；达到上限或超时即终止本次子进程并回收。

    对**不暴露 stdout/stderr 的对象**（测试替身 / 极简鸭子类型）退回一次
    ``communicate()``，但**仍然**做大小判定，保证语义一致。
    """
    limit = max(1, int(timeout_seconds))

    if not _supports_bounded_streaming(process):
        # 兼容路径：对象没有可分流读取的管道（例如测试替身），退回 communicate()。
        timed_out = False
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout=limit)
        except asyncio.TimeoutError:
            timed_out = True
            _kill_quietly(process)
            out, err = await process.communicate()
        out = out or b""
        err = err or b""
        returncode = getattr(process, "returncode", None)
        return BoundedOutput(
            stdout=out,
            stderr=err,
            returncode=returncode if isinstance(returncode, int) else -1,
            timed_out=timed_out,
            stdout_exceeded=len(out) > max_stdout_bytes,
            stderr_exceeded=len(err) > max_stderr_bytes,
        )

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    exceeded = {"stdout": False, "stderr": False}

    async def _pump(stream: object, chunks: list[bytes], key: str, cap: int) -> None:
        total = 0
        reader = stream.read  # type: ignore[attr-defined]
        while True:
            chunk = await reader(_READ_CHUNK_BYTES)
            if not chunk:
                return
            chunks.append(chunk)
            total += len(chunk)
            if total > cap:
                exceeded[key] = True
                _kill_quietly(process)
                return

    pumps = asyncio.gather(
        _pump(process.stdout, stdout_chunks, "stdout", max_stdout_bytes),  # type: ignore[attr-defined]
        _pump(process.stderr, stderr_chunks, "stderr", max_stderr_bytes),  # type: ignore[attr-defined]
    )
    timed_out = False
    try:
        await asyncio.wait_for(pumps, timeout=limit)
    except asyncio.TimeoutError:
        timed_out = True
        _kill_quietly(process)

    if timed_out or exceeded["stdout"] or exceeded["stderr"]:
        _kill_quietly(process)
    await _reap(process)
    _close_pipe_transports(process)

    returncode = getattr(process, "returncode", None)
    return BoundedOutput(
        stdout=b"".join(stdout_chunks),
        stderr=b"".join(stderr_chunks),
        returncode=returncode if isinstance(returncode, int) else -1,
        timed_out=timed_out,
        stdout_exceeded=exceeded["stdout"],
        stderr_exceeded=exceeded["stderr"],
    )


def _truncation_note(bounded: BoundedOutput) -> str:
    """给超限/超时补一条**脱敏后**的诊断说明（不含任何原文片段）。"""
    parts: list[str] = []
    if bounded.stdout_exceeded:
        parts.append(f"stdout 超过 {MAX_STDOUT_BYTES} 字节上限")
    if bounded.stderr_exceeded:
        parts.append(f"stderr 超过 {MAX_STDERR_BYTES} 字节上限")
    if bounded.timed_out:
        parts.append("子进程读取超时")
    return "；".join(parts)


async def run_worker(
    *,
    worker_py: str,
    python: str,
    data_dir: Path,
    positional: Sequence[str] = (),
    cdp_url: Optional[str] = None,
    browser_source: Optional[str] = None,
    rendered_job_id: Optional[str] = None,
    timeout_seconds: int = 120,
) -> RawInvocation:
    """Execute a repository worker (``backend/boss_browser_worker.py``) in the pinned interpreter.

    与 ``run_upstream`` 的区别：worker 是我们仓库内的 Python 脚本，必须在**装有 boss SDK 的
    固定解释器**内运行（任务书 §3.2），stdout 输出一条与 boss CLI 同构的 JSON 信封。
    这里同样遵守 argv 数组、白名单、超时、输出大小上限，禁止 shell=True。

    脱敏（F7）：落库/展示用的 ``argv_redacted`` 走 ``redact_worker_argv`` ——``--`` 之后的
    两个位置参数 ``securityId`` / ``lid`` 被**整段替换**为 ``[REDACTED_securityId]`` /
    ``[REDACTED_lid]``。二者形状不固定，靠 ``redact()`` 的形态猜测会整条漏掉，
    所以这里按位置遮蔽，且不保留原值任何片段。
    """
    worker_path = (PROJECT_DIR / "backend" / worker_py).resolve()
    _data_dir = Path(data_dir).expanduser().resolve()
    argv = [python, str(worker_path), "--data-dir", str(_data_dir)]
    if cdp_url:
        argv += ["--cdp-url", cdp_url]
    if browser_source:
        argv += ["--browser-source", browser_source]
    if rendered_job_id:
        argv += ["--job-id", _safe_token(rendered_job_id, label="public job ID")]
    argv += ["--timeout-seconds", str(max(1, timeout_seconds))]
    argv += ["--", *[_safe_token(v, label="worker positional") for v in positional]]

    started = time.monotonic()
    environment = sanitized_child_env()
    environment["PYTHONIOENCODING"] = "utf-8"
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=str((PROJECT_DIR / "backend").resolve()),
            env=environment,
        )
    except (FileNotFoundError, PermissionError, OSError, ValueError) as exc:
        return RawInvocation(
            argv=argv,
            argv_redacted=redact_worker_argv(argv),
            returncode=-1,
            stdout="",
            stderr=redact(f"{type(exc).__name__}: {exc}"),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            timed_out=False,
            data_dir=str(_data_dir),
        )

    bounded = await communicate_bounded(process, timeout_seconds=timeout_seconds)
    if bounded.any_exceeded:
        # 本次子进程已在读取过程中被终止；不把超限内容当正常结果。
        return RawInvocation(
            argv=argv,
            argv_redacted=redact_worker_argv(argv),
            returncode=bounded.returncode,
            stdout=bounded.stdout[:MAX_STDOUT_BYTES].decode("utf-8", errors="replace"),
            stderr=redact(_truncation_note(bounded))[:MAX_STDERR_BYTES],
            elapsed_ms=int((time.monotonic() - started) * 1000),
            timed_out=False,
            data_dir=str(_data_dir),
            output_truncated=True,
        )
    return RawInvocation(
        argv=argv,
        argv_redacted=redact_worker_argv(argv),
        returncode=bounded.returncode,
        stdout=bounded.stdout.decode("utf-8", errors="replace"),
        stderr=bounded.stderr.decode("utf-8", errors="replace"),
        elapsed_ms=int((time.monotonic() - started) * 1000),
        timed_out=bounded.timed_out,
        data_dir=str(_data_dir),
    )


async def run_upstream(
    cli_path: str,
    subcommand: str,
    *,
    data_dir: Path,
    positional: Sequence[str] = (),
    options: Optional[Mapping[str, str | int | bool | None]] = None,
    top_level_options: Optional[Mapping[str, str]] = None,
    timeout_seconds: int = 120,
    cwd: Optional[Path] = None,
) -> RawInvocation:
    """Execute one bounded, non-interactive, read-only upstream CLI call."""
    argv = build_argv(
        cli_path,
        subcommand,
        data_dir=data_dir,
        positional=positional,
        options=options,
        top_level_options=top_level_options,
    )
    started = time.monotonic()
    environment = sanitized_child_env()
    environment["PYTHONIOENCODING"] = "utf-8"
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=str((cwd or PROJECT_DIR).resolve()),
            env=environment,
        )
    except (FileNotFoundError, PermissionError, OSError, ValueError) as exc:
        return RawInvocation(
            argv=argv,
            argv_redacted=redact_argv(argv),
            returncode=-1,
            stdout="",
            stderr=redact(f"{type(exc).__name__}: {exc}"),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            timed_out=False,
            data_dir=str(data_dir.resolve()),
        )

    bounded = await communicate_bounded(process, timeout_seconds=timeout_seconds)
    if bounded.any_exceeded:
        # 本次子进程已在读取过程中被终止；不把超限内容当正常结果。
        return RawInvocation(
            argv=argv,
            argv_redacted=redact_argv(argv),
            returncode=bounded.returncode,
            stdout=bounded.stdout[:MAX_STDOUT_BYTES].decode("utf-8", errors="replace"),
            stderr=redact(_truncation_note(bounded))[:MAX_STDERR_BYTES],
            elapsed_ms=int((time.monotonic() - started) * 1000),
            timed_out=False,
            data_dir=str(data_dir.resolve()),
            output_truncated=True,
        )
    return RawInvocation(
        argv=argv,
        argv_redacted=redact_argv(argv),
        returncode=bounded.returncode,
        stdout=bounded.stdout.decode("utf-8", errors="replace"),
        stderr=bounded.stderr.decode("utf-8", errors="replace"),
        elapsed_ms=int((time.monotonic() - started) * 1000),
        timed_out=bounded.timed_out,
        data_dir=str(data_dir.resolve()),
    )
