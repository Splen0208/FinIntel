"""Strict parser for boss-agent-cli 2.0.0 JSON envelopes."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from upstream_config import RawInvocation, redact

EXPECTED_SCHEMA_VERSION = "1.0"


class FailureLayer:
    """Ordered upstream failure layers."""

    TRANSPORT = "transport"
    ENVELOPE = "envelope"
    BUSINESS = "business"
    CONTRACT = "contract"
    OK = "ok"


@dataclass(frozen=True)
class UpstreamEnvelope:
    """Sanitized representation of one upstream JSON envelope."""

    layer: str
    ok: bool
    command: str
    schema_version: Optional[str]
    data: Any
    pagination: Optional[dict[str, Any]]
    error: Optional[dict[str, Any]]
    operator_actions: list[str]
    safe_hints: Optional[dict[str, Any]]
    raw_snippet: str
    returncode: int
    elapsed_ms: int
    argv_redacted: list[str]
    data_dir: str


class UpstreamError(Exception):
    """Stable application-facing upstream error with diagnostic evidence."""

    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool,
        operator_action: str,
        *,
        upstream_code: Optional[str] = None,
        layer: str = FailureLayer.BUSINESS,
        raw_snippet: str = "",
        evidence: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.operator_action = operator_action
        self.upstream_code = upstream_code
        self.layer = layer
        self.raw_snippet = raw_snippet
        self.evidence = evidence or {}


UPSTREAM_ERROR_MAP: dict[str, tuple[str, bool, str]] = {
    "AUTH_REQUIRED": ("AUTH_REQUIRED", False, "请在当前生效 data-dir 下完成人工登录后重试"),
    "AUTH_EXPIRED": ("AUTH_EXPIRED", False, "登录态已过期，请在当前生效 data-dir 下重新登录"),
    "LOGIN_EXPIRED": ("AUTH_EXPIRED", False, "登录态已过期，请在当前生效 data-dir 下重新登录"),
    "TOKEN_REFRESH_FAILED": ("AUTH_EXPIRED", False, "登录态刷新失败，请在当前生效 data-dir 下重新登录"),
    "ENVIRONMENT_RISK": ("ENVIRONMENT_RISK", False, "当前访问环境被平台拦截；保留浏览器并在官方页面检查，不自动重试"),
    "ACCOUNT_RISK": ("ACCOUNT_RISK", False, "停止自动重试；请在 BOSS 官网或 App 确认账号状态后手动重试一次"),
    "LOGIN_RISK_CONTROL": ("ACCOUNT_RISK", False, "停止自动重试；请在 BOSS 官网或 App 确认账号状态后手动重试一次"),
    "STOPPED_BY_SAFETY": ("ACCOUNT_RISK", False, "停止自动重试；请在 BOSS 官网或 App 确认账号状态后手动重试一次"),
    "PLATFORM_VERIFICATION_REQUIRED": ("ACCOUNT_RISK", False, "请回平台官网完成人工验证后再手动重试一次"),
    "RATE_LIMITED": ("RATE_LIMITED", True, "等待一段时间后再手动重试；不要连续重试"),
    "RESULT_LIMIT_REACHED": ("UPSTREAM_RESULT_LIMIT", False, "缩小查询范围后重试，例如限定单一城市或减少查询词"),
    # 采集改走「用户自己的浏览器」后的两类失败：连不上浏览器 / 浏览器里没有可用会话。
    # 归因一律归到「通道连接」，**不归到用户未登录**——上游这条只说明没发现可用会话，
    # 并不等于账号未认证（本地凭据与浏览器会话是两回事，见设计 §0.1）。
    "CDP_UNAVAILABLE": (
        "OWN_BROWSER_NOT_READY", True,
        "没有连上目标浏览器的调试会话：确认浏览器已以可连接的方式启动且 BOSS 直聘页面已打开后重试采集",
    ),
    "BROWSER_SESSION_NOT_FOUND": (
        "OWN_BROWSER_NO_SESSION", True,
        "未能发现可用的浏览器采集会话：确认目标浏览器里已打开 BOSS 直聘页面、会话可用后重试采集",
    ),
    "BROWSER_KERNEL_MISSING": ("UPSTREAM_BROWSER_UNAVAILABLE", True, "请先运行 boss doctor 检查浏览器环境，必要时人工重新登录"),
    "LOGIN_TIMEOUT": ("UPSTREAM_LOGIN_TIMEOUT", True, "请人工重跑登录并适当放宽登录超时"),
    "NETWORK_ERROR": ("UPSTREAM_NETWORK", True, "检查本机网络后再手动重试"),
    "INVALID_PARAM": ("INVALID_UPSTREAM_PARAM", False, "请更新上游契约适配，并使用脱敏命令进行人工复现"),
    "JOB_NOT_FOUND": ("JOB_NOT_FOUND", False, "岗位可能已下架；跳过该岗位并继续处理其他岗位"),
    "NOT_SUPPORTED": ("UPSTREAM_NOT_SUPPORTED", False, "确认平台与角色配置；本项目仅使用只读候选人能力"),
    "PLATFORM_NOT_SUPPORTED": ("UPSTREAM_NOT_SUPPORTED", False, "确认平台与角色配置；本项目仅使用只读候选人能力"),
    "CONFIRMATION_REQUIRED": ("TOOL_NOT_ALLOWED", False, "本项目不执行需要确认的写操作或遍历操作"),
}

# 上游对「访问环境异常（code 37）」的**同一份证据**签名：无论上游把它报成
# ENVIRONMENT_RISK 还是（旧映射塌缩后的）ACCOUNT_RISK，message 里都带这个短语。
ENVIRONMENT_RISK_SIGNATURE = "访问环境风控"


def refine_risk_code(code: str, message: str = "") -> str:
    """用同一份证据把被合并/误报的风控码还原为 ``ENVIRONMENT_RISK``。

    真机缺陷：``research_run.stop_reason`` 与 ``channel_state.last_error_code`` 都从
    ``error.code`` 派生，但历史上 ``ENVIRONMENT_RISK`` 被旧映射塌缩成 ``ACCOUNT_RISK``，
    于是「同一次风控」在任务页与顶栏讲成两个故事。判定只依赖**同一份证据**（上游 message
    里的 ``访问环境风控`` 签名），因此改在这一个函数里收口：只要证据表明是访问环境风控，
    无论上游码是 ``ENVIRONMENT_RISK`` 还是 ``ACCOUNT_RISK``，都归一为 ``ENVIRONMENT_RISK``；
    非风控码或证据不足时原样返回（绝不臆测）。
    """
    if code == "UPSTREAM_UNKNOWN" and "您的IP地址存在异常行为" in str(message or ""):
        return "ENVIRONMENT_RISK"
    if code in {"ACCOUNT_RISK", "ENVIRONMENT_RISK"} and \
            ENVIRONMENT_RISK_SIGNATURE in str(message or ""):
        return "ENVIRONMENT_RISK"
    return code


_DISALLOWED_HINT_KEY = {"next_actions", "live_probe"}
_ACTION_TEXT_RE = re.compile(r"(?i)(?:\bgreet\b|\bapply\b|\bchat\b|投递|打招呼|沟通)")
_COMMAND_TEXT_RE = re.compile(r"(?i)^\s*boss(?:\.exe)?(?:\s|$)")


def _safe_operator_text(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = redact(value.strip())[:300]
    if not value or _COMMAND_TEXT_RE.search(value) or _ACTION_TEXT_RE.search(value):
        return None
    return value


def sanitize_hints(hints: Optional[dict[str, Any]]) -> tuple[list[str], Optional[dict[str, Any]]]:
    """Remove command/action hints while preserving safe diagnostic structure."""
    if not isinstance(hints, dict):
        return [], None
    actions: list[str] = []

    def clean(value: Any, key: Optional[str] = None) -> Any:
        if key in _DISALLOWED_HINT_KEY:
            return None
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for child_key, child_value in value.items():
                if child_key in _DISALLOWED_HINT_KEY:
                    continue
                if child_key == "recovery_action":
                    action = _safe_operator_text(child_value)
                    if action and action not in actions:
                        actions.append(action)
                    if action:
                        result[child_key] = action
                    continue
                cleaned = clean(child_value, child_key)
                if cleaned is not None:
                    result[child_key] = cleaned
            return result
        if isinstance(value, list):
            cleaned_items = []
            for item in value:
                cleaned = clean(item, key)
                if cleaned is not None:
                    cleaned_items.append(cleaned)
            return cleaned_items
        if isinstance(value, str):
            sanitized = redact(value)
            if _COMMAND_TEXT_RE.search(sanitized) or _ACTION_TEXT_RE.search(sanitized):
                return None
            return sanitized
        return value

    safe_hints = clean(hints)
    return actions, safe_hints if isinstance(safe_hints, dict) else None


def _evidence(invocation: RawInvocation, command: str) -> dict[str, Any]:
    return {
        "argv_redacted": invocation.argv_redacted,
        "data_dir": invocation.data_dir,
        "elapsed_ms": invocation.elapsed_ms,
        "upstream_command": command,
        "returncode": invocation.returncode,
    }


def _snippet(invocation: RawInvocation) -> str:
    combined = invocation.stdout
    if invocation.stderr:
        combined = f"{combined}\n{invocation.stderr}" if combined else invocation.stderr
    return redact(combined)[:2000]


def _error_envelope(
    invocation: RawInvocation,
    code: str,
    message: str,
    layer: str,
    *,
    command: str = "",
    retryable: bool = False,
    operator_action: str = "",
) -> UpstreamEnvelope:
    error = {
        "code": code,
        "message": message,
        "recoverable": retryable,
        "recovery_action": operator_action,
    }
    return UpstreamEnvelope(
        layer=layer,
        ok=False,
        command=command,
        schema_version=None,
        data=None,
        pagination=None,
        error=error,
        operator_actions=[operator_action] if operator_action else [],
        safe_hints=None,
        raw_snippet=_snippet(invocation),
        returncode=invocation.returncode,
        elapsed_ms=invocation.elapsed_ms,
        argv_redacted=invocation.argv_redacted,
        data_dir=invocation.data_dir,
    )


def parse_envelope(invocation: RawInvocation) -> UpstreamEnvelope:
    """Parse an invocation using transport, envelope, business, and contract layers."""
    snippet = _snippet(invocation)
    try:
        payload = json.loads(invocation.stdout)
    except (json.JSONDecodeError, TypeError):
        if invocation.timed_out:
            return _error_envelope(
                invocation,
                "UPSTREAM_TIMEOUT",
                "上游调用超时",
                FailureLayer.TRANSPORT,
                retryable=True,
                operator_action="可调大单次调用超时或稍后手动重试一次",
            )
        if invocation.returncode == -1:
            return _error_envelope(
                invocation,
                "DEPENDENCY_MISSING",
                "无法启动 boss CLI，可执行文件不存在或无权限",
                FailureLayer.TRANSPORT,
                operator_action="请安装 boss-agent-cli==2.0.0，并在设置中填写可执行文件路径",
            )
        return _error_envelope(
            invocation,
            "UPSTREAM_CONTRACT_CHANGED",
            "上游 stdout 不是单个有效 JSON 对象",
            FailureLayer.ENVELOPE,
            operator_action="核对 boss-agent-cli 版本并重跑契约测试",
        )

    if not isinstance(payload, dict):
        return _error_envelope(
            invocation,
            "UPSTREAM_CONTRACT_CHANGED",
            "上游 JSON 顶层不是对象",
            FailureLayer.ENVELOPE,
            operator_action="核对 boss-agent-cli 版本并重跑契约测试",
        )
    required_top = {"ok", "schema_version", "command", "data"}
    missing_top = sorted(required_top - payload.keys())
    if missing_top or payload["schema_version"] != EXPECTED_SCHEMA_VERSION:
        detail = f"缺少顶层键 {missing_top}" if missing_top else (
            f"schema_version={payload['schema_version']!r}，期望 {EXPECTED_SCHEMA_VERSION!r}"
        )
        return _error_envelope(
            invocation,
            "UPSTREAM_CONTRACT_CHANGED",
            f"上游信封契约变化：{detail}",
            FailureLayer.ENVELOPE,
            command=str(payload["command"]) if "command" in payload else "",
            operator_action="固定 boss-agent-cli==2.0.0 并更新适配器契约",
        )
    if not isinstance(payload["ok"], bool) or not isinstance(payload["command"], str):
        return _error_envelope(
            invocation,
            "UPSTREAM_CONTRACT_CHANGED",
            "上游 ok/command 字段类型不符合契约",
            FailureLayer.ENVELOPE,
            command=str(payload["command"]),
            operator_action="核对 boss-agent-cli 版本并重跑契约测试",
        )

    operator_actions, safe_hints = sanitize_hints(payload["hints"] if "hints" in payload else None)
    error_value = payload["error"] if "error" in payload else None
    if not payload["ok"]:
        if not isinstance(error_value, dict) or not isinstance(error_value.get("code"), str):
            return _error_envelope(
                invocation,
                "UPSTREAM_CONTRACT_CHANGED",
                "上游失败信封缺少结构化 error.code",
                FailureLayer.ENVELOPE,
                command=payload["command"],
                operator_action="核对 boss-agent-cli 版本并重跑契约测试",
            )
        recovery = _safe_operator_text(error_value.get("recovery_action"))
        if recovery and recovery not in operator_actions:
            operator_actions.insert(0, recovery)
        sanitized_error = {
            "code": error_value["code"],
            "message": redact(str(error_value.get("message", "上游调用失败"))),
            "recoverable": bool(error_value.get("recoverable", False)),
            "recovery_action": recovery or "",
        }
        if "details" in error_value:
            sanitized_error["details"] = redact(str(error_value["details"]))[:1000]
        return UpstreamEnvelope(
            layer=FailureLayer.BUSINESS,
            ok=False,
            command=payload["command"],
            schema_version=payload["schema_version"],
            data=payload["data"],
            pagination=payload["pagination"] if isinstance(payload.get("pagination"), dict) else None,
            error=sanitized_error,
            operator_actions=operator_actions,
            safe_hints=safe_hints,
            raw_snippet=snippet,
            returncode=invocation.returncode,
            elapsed_ms=invocation.elapsed_ms,
            argv_redacted=invocation.argv_redacted,
            data_dir=invocation.data_dir,
        )

    contract_message = _validate_success_contract(payload)
    if contract_message:
        return _error_envelope(
            invocation,
            "UPSTREAM_CONTRACT_CHANGED",
            contract_message,
            FailureLayer.CONTRACT,
            command=payload["command"],
            operator_action="上游成功信封的字段结构已变化，请停止采集并更新契约适配",
        )
    return UpstreamEnvelope(
        layer=FailureLayer.OK,
        ok=True,
        command=payload["command"],
        schema_version=payload["schema_version"],
        data=payload["data"],
        pagination=payload["pagination"] if isinstance(payload.get("pagination"), dict) else None,
        error=None,
        operator_actions=operator_actions,
        safe_hints=safe_hints,
        raw_snippet=snippet,
        returncode=invocation.returncode,
        elapsed_ms=invocation.elapsed_ms,
        argv_redacted=invocation.argv_redacted,
        data_dir=invocation.data_dir,
    )


def _validate_success_contract(payload: dict[str, Any]) -> Optional[str]:
    from upstream_contract import (
        DETAIL_REQUIRED_FIELDS,
        PAGINATION_REQUIRED_FIELDS,
        SEARCH_REQUIRED_FIELDS,
    )

    command = payload["command"]
    data = payload["data"]
    if command == "search":
        if not isinstance(data, list):
            return "search 成功信封的 data 必须是列表"
        pagination = payload.get("pagination")
        if not isinstance(pagination, dict):
            return "search 成功信封缺少 pagination 对象"
        missing_pagination = sorted(PAGINATION_REQUIRED_FIELDS - pagination.keys())
        if missing_pagination:
            return f"search pagination 缺少字段：{missing_pagination}"
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                return f"search data[{index}] 不是对象"
            missing = sorted(SEARCH_REQUIRED_FIELDS - item.keys())
            if missing:
                return f"search data[{index}] 缺少必填字段：{missing}"
    elif command == "detail":
        if not isinstance(data, dict):
            return "detail 成功信封的 data 必须是对象"
        missing = sorted(DETAIL_REQUIRED_FIELDS - data.keys())
        if missing:
            return f"detail data 缺少必填字段：{missing}"
    return None


def map_upstream_error(envelope: UpstreamEnvelope) -> UpstreamError:
    """Map a failed envelope to the stable finintel error taxonomy."""
    error = envelope.error or {}
    upstream_code = str(error.get("code") or "UPSTREAM_PROCESS_FAILED")
    if upstream_code in UPSTREAM_ERROR_MAP:
        code, default_retryable, default_action = UPSTREAM_ERROR_MAP[upstream_code]
    elif upstream_code in {
        "DEPENDENCY_MISSING", "UPSTREAM_TIMEOUT", "UPSTREAM_CONTRACT_CHANGED",
        "DATA_DIR_MISMATCH", "TOOL_NOT_ALLOWED",
    }:
        code = upstream_code
        default_retryable = upstream_code == "UPSTREAM_TIMEOUT"
        default_action = "检查上游配置与诊断信息后手动重试"
    else:
        normalized = re.sub(r"[^A-Z0-9_]+", "_", upstream_code.upper()).strip("_")
        code = f"UPSTREAM_{normalized or 'PROCESS_FAILED'}"
        default_retryable = bool(error.get("recoverable", False))
        default_action = "运行 doctor/status 查看上游状态后手动重试"
    # 同一份证据（上游 message）收口：把被合并/误报的「访问环境风控」还原为
    # ENVIRONMENT_RISK，使 research_run.stop_reason 与 channel_state.last_error_code
    # 由这一个函数统一得出（同源、同口径），不会两处两说。
    code = refine_risk_code(code, str(error.get("message") or ""))
    if code == "ENVIRONMENT_RISK":
        default_retryable = False
        default_action = UPSTREAM_ERROR_MAP[code][2]
    recovery = _safe_operator_text(error.get("recovery_action"))
    action_parts = []
    for action in [recovery, *envelope.operator_actions, default_action]:
        if action and action not in action_parts:
            action_parts.append(action)
    return UpstreamError(
        code,
        str(error.get("message") or "上游调用失败"),
        False if code == "ENVIRONMENT_RISK" else bool(error.get("recoverable", default_retryable)),
        "；".join(action_parts),
        upstream_code=upstream_code,
        layer=envelope.layer,
        raw_snippet=envelope.raw_snippet,
        evidence={
            "argv_redacted": envelope.argv_redacted,
            "data_dir": envelope.data_dir,
            "elapsed_ms": envelope.elapsed_ms,
            "upstream_command": envelope.command,
            "returncode": envelope.returncode,
        },
    )


def require_success(envelope: UpstreamEnvelope, *, command: str) -> UpstreamEnvelope:
    """Require a successful envelope for the expected command or raise."""
    if envelope.command and envelope.command != command:
        raise UpstreamError(
            "UPSTREAM_CONTRACT_CHANGED",
            f"上游返回 command={envelope.command!r}，期望 {command!r}",
            False,
            "核对上游版本与命令路由",
            layer=FailureLayer.CONTRACT,
            raw_snippet=envelope.raw_snippet,
            evidence={
                "argv_redacted": envelope.argv_redacted,
                "data_dir": envelope.data_dir,
                "elapsed_ms": envelope.elapsed_ms,
                "upstream_command": envelope.command,
                "returncode": envelope.returncode,
            },
        )
    if not envelope.ok:
        raise map_upstream_error(envelope)
    if envelope.returncode != 0:
        raise UpstreamError(
            "UPSTREAM_PROCESS_FAILED",
            f"上游成功信封对应非零退出码 {envelope.returncode}",
            True,
            "运行 doctor/status 检查上游环境后手动重试",
            layer=FailureLayer.TRANSPORT,
            raw_snippet=envelope.raw_snippet,
            evidence=_evidence_from_envelope(envelope),
        )
    return envelope


def _evidence_from_envelope(envelope: UpstreamEnvelope) -> dict[str, Any]:
    return {
        "argv_redacted": envelope.argv_redacted,
        "data_dir": envelope.data_dir,
        "elapsed_ms": envelope.elapsed_ms,
        "upstream_command": envelope.command,
        "returncode": envelope.returncode,
    }
