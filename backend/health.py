"""Layered, cached health state for the real BOSS integration."""
from __future__ import annotations

import asyncio
import glob
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

import source_boss
import store
from upstream_config import (
    DataDirResolution,
    PROJECT_DIR,
    install_command_hint,
    login_command_hint,
    resolve_cli_path,
)
from upstream_envelope import UpstreamError

LAYER_KEYS = (
    "cli_path",
    "mcp_protocol",
    "local_credential",
    "online_auth",
    "recent_search",
    "recent_detail",
)
LAYER_TTL_SECONDS: dict[str, Optional[int]] = {
    "cli_path": 3600,
    "mcp_protocol": None,
    "local_credential": 3600,
    "online_auth": 24 * 3600,
    "recent_search": 7 * 24 * 3600,
    "recent_detail": 7 * 24 * 3600,
}
PROBE_THROTTLE_SECONDS = 60
_PROBE_LOCK = asyncio.Lock()
_LAST_PROBED: dict[str, float] = {}


@dataclass(frozen=True)
class LayerState:
    """One independently verifiable integration capability."""

    key: str
    status: str
    checked_at: Optional[str]
    elapsed_ms: Optional[int]
    detail: str
    error_code: Optional[str]
    error_message: Optional[str]
    operator_actions: list[str]
    evidence: dict[str, Any]
    stale: bool

    def as_view(self) -> dict[str, Any]:
        """Return the JSON-safe API representation."""
        return asdict(self)


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _is_stale(key: str, checked_at: Optional[str]) -> bool:
    ttl = LAYER_TTL_SECONDS[key]
    checked = _parse_time(checked_at)
    if ttl is None:
        return False
    if checked is None:
        return True
    return (datetime.now(timezone.utc) - checked).total_seconds() > ttl


def _unknown(key: str, detail: str) -> LayerState:
    return LayerState(
        key=key,
        status="unknown",
        checked_at=None,
        elapsed_ms=None,
        detail=detail,
        error_code=None,
        error_message=None,
        operator_actions=[],
        evidence={},
        stale=True,
    )


def _load_recorded(key: str) -> Optional[LayerState]:
    try:
        row = store.query_one("SELECT * FROM health_check WHERE key=?", (key,))
    except Exception:
        return None
    if not row:
        return None
    return LayerState(
        key=key,
        status="stale" if _is_stale(key, row["checked_at"]) else row["status"],
        checked_at=row["checked_at"],
        elapsed_ms=row["elapsed_ms"],
        detail=row["detail"] or "",
        error_code=row["error_code"],
        error_message=row["error_message"],
        operator_actions=store.jloads(row["operator_actions"], []),
        evidence=store.jloads(row["evidence"], {}),
        stale=_is_stale(key, row["checked_at"]),
    )


def _mcp_layer() -> LayerState:
    candidates = sorted(
        glob.glob(str(PROJECT_DIR / ".local" / "probe" / "mcp_handshake_*.json")),
        reverse=True,
    )
    if not candidates:
        return _unknown("mcp_protocol", "尚无独立 MCP 握手验证记录；网站采集链路使用 CLI")
    path = Path(candidates[0])
    checked_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        ok = payload.get("ok") is True and payload.get("used_by_website") is False
        detail = "MCP 独立握手已验证；不参与网站采集链路" if ok else "MCP 握手记录未通过"
        return LayerState(
            key="mcp_protocol",
            status="ok" if ok else "error",
            checked_at=checked_at,
            elapsed_ms=payload.get("elapsed_ms") if isinstance(payload.get("elapsed_ms"), int) else None,
            detail=detail,
            error_code=None if ok else "MCP_PROTOCOL_ERROR",
            error_message=None if ok else "独立 MCP 握手记录未通过",
            operator_actions=[],
            evidence={"path": str(path), "used_by_website": False},
            stale=False,
        )
    except (OSError, json.JSONDecodeError) as exc:
        return LayerState(
            key="mcp_protocol",
            status="error",
            checked_at=checked_at,
            elapsed_ms=None,
            detail="MCP 握手记录无法读取；网站采集链路仍为 CLI",
            error_code="MCP_PROTOCOL_ERROR",
            error_message=str(exc),
            operator_actions=[],
            evidence={"path": str(path), "used_by_website": False},
            stale=False,
        )


def _recent_search_layer() -> LayerState:
    try:
        rows = store.query_all(
            """SELECT o.*, r.config_snapshot FROM search_observation o
               JOIN research_run r ON r.id=o.run_id
               WHERE o.outcome='success' ORDER BY o.observed_at DESC"""
        )
    except Exception:
        rows = []
    for row in rows:
        config = store.jloads(row["config_snapshot"], {})
        if config.get("source") != "zhipin":
            continue
        count = len(store.jloads(row["source_job_ids"], []))
        return LayerState(
            key="recent_search",
            status="stale" if _is_stale("recent_search", row["observed_at"]) else "ok",
            checked_at=row["observed_at"],
            elapsed_ms=None,
            detail=f"最近真实搜索返回 {count} 个岗位标识",
            error_code=None,
            error_message=None,
            operator_actions=[],
            evidence={"run_id": row["run_id"], "count": count, "query_id": row["query_id"]},
            stale=_is_stale("recent_search", row["observed_at"]),
        )
    return _unknown("recent_search", "尚无真实 BOSS 搜索成功记录")


def _recent_detail_layer() -> LayerState:
    try:
        row = store.query_one(
            """SELECT s.id, s.fetched_at, s.detail_status, s.responsibilities_text,
                      s.source_platform, s.source_job_id, j.id AS job_id
               FROM job_snapshot s JOIN job j ON j.id=s.job_id
               WHERE j.source='zhipin' AND s.detail_status='complete'
                 AND LENGTH(TRIM(COALESCE(s.responsibilities_text,''))) > 0
               ORDER BY s.fetched_at DESC LIMIT 1"""
        )
    except Exception:
        row = None
    if not row:
        return _unknown("recent_detail", "尚无职责正文非空的真实 BOSS 详情记录")
    stale = _is_stale("recent_detail", row["fetched_at"])
    return LayerState(
        key="recent_detail",
        status="stale" if stale else "ok",
        checked_at=row["fetched_at"],
        elapsed_ms=None,
        detail=f"最近真实详情正文 {len(row['responsibilities_text'])} 字",
        error_code=None,
        error_message=None,
        operator_actions=[],
        evidence={
            "snapshot_id": row["id"],
            "job_id": row["job_id"],
            "source_platform": row["source_platform"],
            "source_job_id": row["source_job_id"],
        },
        stale=stale,
    )


def cached_layer_view(include: Sequence[str] = LAYER_KEYS) -> dict[str, LayerState]:
    """Return cached/local health only; this function never starts a subprocess."""
    invalid = set(include) - set(LAYER_KEYS)
    if invalid:
        raise ValueError(f"未知健康层：{sorted(invalid)}")
    result: dict[str, LayerState] = {}
    for key in include:
        if key == "mcp_protocol":
            result[key] = _mcp_layer()
        elif key == "recent_search":
            result[key] = _recent_search_layer()
        elif key == "recent_detail":
            result[key] = _recent_detail_layer()
        elif key == "cli_path":
            recorded = _load_recorded(key)
            cli_path = resolve_cli_path(_setting("boss_cli_path"))
            if cli_path:
                result[key] = recorded or LayerState(
                    key=key,
                    status="ok",
                    checked_at=None,
                    elapsed_ms=None,
                    detail=f"已解析 CLI 路径（尚未执行探针）：{cli_path}",
                    error_code=None,
                    error_message=None,
                    operator_actions=[],
                    evidence={"cli_path": cli_path},
                    stale=True,
                )
            else:
                result[key] = LayerState(
                    key=key,
                    status="unavailable",
                    checked_at=recorded.checked_at if recorded else None,
                    elapsed_ms=None,
                    detail="未找到 boss CLI",
                    error_code="DEPENDENCY_MISSING",
                    error_message="未安装或未配置 boss-agent-cli==2.0.0",
                    operator_actions=[install_command_hint()],
                    evidence={},
                    stale=True,
                )
        else:
            result[key] = _load_recorded(key) or _unknown(key, "尚未执行该层检查")
    return result


def _setting(key: str) -> Optional[str]:
    try:
        row = store.query_one("SELECT value FROM app_setting WHERE key=?", (key,))
    except Exception:
        return None
    return row["value"] if row else None


def boot_steps(cli_path: Optional[str], data_dir: DataDirResolution) -> list[str]:
    """Return concrete human login steps without exposing any credentials."""
    executable = cli_path or str(Path("boss.exe" if __import__("os").name == "nt" else "boss"))
    return [
        f"打开本机终端并切到项目目录：cd {PROJECT_DIR}",
        f"确认 CLI 版本：{executable} --version（应输出 boss, version 2.0.0）",
        f"执行人工登录：{login_command_hint(executable, data_dir.path)}",
        "按终端提示在平台侧完成扫码或验证；不要向任何人提供 Cookie、令牌或验证码",
        f"确认本地登录态：{executable} --data-dir \"{data_dir.path}\" --json status",
        "回到设置页点击“重新检查”",
    ]


def _state_from_error(key: str, exc: UpstreamError, data_dir: DataDirResolution) -> LayerState:
    actions = [exc.operator_action] if exc.operator_action else []
    if exc.code in {"AUTH_REQUIRED", "AUTH_EXPIRED"}:
        actions.extend(boot_steps(resolve_cli_path(_setting("boss_cli_path")), data_dir))
    return LayerState(
        key=key,
        status="unavailable" if exc.code in {"AUTH_REQUIRED", "AUTH_EXPIRED", "DEPENDENCY_MISSING"} else "error",
        checked_at=store.utcnow_iso(),
        elapsed_ms=exc.evidence.get("elapsed_ms") if isinstance(exc.evidence, dict) else None,
        detail=exc.message,
        error_code=exc.code,
        error_message=exc.message,
        operator_actions=list(dict.fromkeys(actions)),
        evidence=exc.evidence,
        stale=False,
    )


async def probe(
    cli_path: Optional[str],
    data_dir: DataDirResolution,
    layers: Sequence[str],
    timeout_seconds: int = 60,
) -> dict[str, LayerState]:
    """Run explicitly requested local/online probes; never call search or detail."""
    requested = tuple(dict.fromkeys(layers))
    invalid = set(requested) - {"cli_path", "local_credential", "online_auth"}
    if invalid:
        raise ValueError(f"这些健康层不允许主动探测：{sorted(invalid)}")
    resolved_cli = resolve_cli_path(cli_path)
    now_mono = asyncio.get_running_loop().time()
    async with _PROBE_LOCK:
        states: dict[str, LayerState] = {}
        for key in requested:
            last = _LAST_PROBED.get(key)
            recorded = _load_recorded(key)
            if last is not None and now_mono - last < PROBE_THROTTLE_SECONDS and recorded:
                evidence = dict(recorded.evidence)
                evidence["throttled"] = True
                states[key] = LayerState(**{**recorded.as_view(), "evidence": evidence})
                continue
            if not resolved_cli:
                state = LayerState(
                    key=key,
                    status="unavailable",
                    checked_at=store.utcnow_iso(),
                    elapsed_ms=None,
                    detail="未找到 boss CLI",
                    error_code="DEPENDENCY_MISSING",
                    error_message="未安装或未配置 boss-agent-cli==2.0.0",
                    operator_actions=[install_command_hint()],
                    evidence={},
                    stale=False,
                )
            else:
                try:
                    if key == "cli_path":
                        result = await source_boss.ping(resolved_cli, data_dir, timeout_seconds)
                        state = LayerState(
                            key=key,
                            status="ok",
                            checked_at=store.utcnow_iso(),
                            elapsed_ms=result["trace"]["elapsed_ms"],
                            detail="boss CLI doctor 可执行，且 data-dir 回显一致",
                            error_code=None,
                            error_message=None,
                            operator_actions=result["operator_actions"],
                            evidence={
                                "cli_path": resolved_cli,
                                "data_dir_echo": result["data_dir_echo"],
                                "trace": result["trace"],
                            },
                            stale=False,
                        )
                    else:
                        live = key == "online_auth"
                        result = await source_boss.auth_state(
                            resolved_cli, data_dir, live=live, timeout_seconds=timeout_seconds
                        )
                        state = LayerState(
                            key=key,
                            status="ok",
                            checked_at=store.utcnow_iso(),
                            elapsed_ms=result["trace"]["elapsed_ms"],
                            detail="在线认证有效" if live else "本地认证材料通过上游检查",
                            error_code=None,
                            error_message=None,
                            operator_actions=result["operator_actions"],
                            evidence={
                                "auth_state": result["auth_state"],
                                "data_dir_echo": result["data_dir_echo"],
                                "trace": result["trace"],
                            },
                            stale=False,
                        )
                except UpstreamError as exc:
                    state = _state_from_error(key, exc, data_dir)
            states[key] = state
            _LAST_PROBED[key] = now_mono
        record_layers(states)
        return states


def record_layers(states: dict[str, LayerState]) -> None:
    """Upsert sanitized health states."""
    connection = store.get_conn()
    for key, state in states.items():
        connection.execute(
            """INSERT INTO health_check
               (key,status,checked_at,elapsed_ms,detail,error_code,error_message,
                operator_actions,evidence,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 status=excluded.status, checked_at=excluded.checked_at,
                 elapsed_ms=excluded.elapsed_ms, detail=excluded.detail,
                 error_code=excluded.error_code, error_message=excluded.error_message,
                 operator_actions=excluded.operator_actions, evidence=excluded.evidence,
                 updated_at=excluded.updated_at""",
            (
                key,
                state.status,
                state.checked_at,
                state.elapsed_ms,
                state.detail,
                state.error_code,
                state.error_message,
                store.jdumps(state.operator_actions),
                store.jdumps(state.evidence),
                store.utcnow_iso(),
            ),
        )
    connection.commit()


def website_ready() -> bool:
    """网站采集链路是否就绪：只看 CLI 路径与本地登录材料（设计 D9 / UX-14）。

    独立 MCP 握手失败**不**阻断已验证的 CLI 网站功能；``integration_state()``
    原样保留供技术诊断页使用，首页改读本函数。
    """
    layers = cached_layer_view(["cli_path", "local_credential"])
    cli_ok = layers["cli_path"].status in {"ok", "stale"}
    credential_ok = layers["local_credential"].status == "ok"
    return cli_ok and credential_ok


def integration_state() -> tuple[str, list[str]]:
    """Derive readiness solely from persisted/cached real capability evidence."""
    layers = cached_layer_view()
    if layers["cli_path"].status not in {"ok", "stale"}:
        return "cli_unverified", ["cli_path"]
    if layers["local_credential"].status != "ok":
        return "auth_missing", ["local_credential", "online_auth"]
    if layers["mcp_protocol"].status != "ok":
        return "mcp_unverified", ["mcp_protocol"]
    if layers["recent_search"].status not in {"ok", "stale"}:
        return "search_unverified", ["recent_search"]
    if layers["recent_detail"].status not in {"ok", "stale"}:
        return "detail_unverified", ["recent_detail"]
    return "ready", []
