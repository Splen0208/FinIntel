# -*- coding: utf-8 -*-
"""来源状态与凭据入口（指令 G 第一条；增量设计 T15）。

解决的问题
----------
旧 ``/api/sources`` 把三行状态**硬编码**成 ``unknown`` / ``configured`` / ``unverified``：
既不能反映真实依赖与登录态，也没有 ``checked_at``，用户看到的绿灯/红灯与事实无关。
本模块提供一套**可落库、可体检**的状态模型：

``dependency_state`` × ``connection_state`` × ``auth_state``（每项都带 ``checked_at``）

铁律
----
1. **未检查就是 unknown**：三层各自独立取证，拿不到证据就如实 ``unknown``，
   绝不"因为配置看起来齐了"就报 ready（防假绿灯）。
2. 体检**不发起采集**：只做依赖发现 + 凭据存在性（只回答有/无）+ 通道连接状态；
   真正的 ready 只能来自真实取数/预检回执（``check_source='collect_ok'``）。
3. 凭据只在 ``app_setting`` / 进程环境之间流转，**绝不回显**：对外视图里只有
   ``has_value`` 布尔与脱敏摘要，任何路径都不得吐出凭据值。
4. ``operator_action`` 必须指向**实际存在**的入口（例如 ``/api/settings`` 的
   ``x_auth_token`` / ``x_ct0``），否则等于把用户支使到一个不存在的页面。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any, Optional

import store
import social_redact

__all__ = [
    "PLATFORM_BOSS", "PLATFORM_X", "PLATFORM_XHS", "PLATFORMS",
    "DEP_UNKNOWN", "DEP_READY", "DEP_MISSING", "DEP_VERSION_UNSUPPORTED",
    "CONN_UNKNOWN", "CONN_READY", "CONN_NOT_READY",
    "AUTH_UNKNOWN", "AUTH_AUTHENTICATED", "AUTH_REQUIRED", "AUTH_EXPIRED",
    "CHECK_PROBE", "CHECK_COLLECT_OK", "CHECK_COLLECT_FAILED", "CHECK_HEALTH_CACHE",
    "X_CREDENTIAL_KEYS", "X_ENV_BY_KEY",
    "SourceStatus", "get_status", "all_statuses", "check_platform", "check_all",
    "credential_present", "x_credentials_state", "apply_x_credentials_to_env",
    "load_credentials_from_settings", "sources_view",
]

# ---------------------------------------------------------------------------
# 平台 / 状态取值
# ---------------------------------------------------------------------------
PLATFORM_BOSS = "zhipin"
PLATFORM_X = "x"
PLATFORM_XHS = "xiaohongshu"
PLATFORMS = (PLATFORM_BOSS, PLATFORM_X, PLATFORM_XHS)

DEP_UNKNOWN = "unknown"
DEP_READY = "ready"
DEP_MISSING = "missing"
DEP_VERSION_UNSUPPORTED = "version_unsupported"

CONN_UNKNOWN = "unknown"
CONN_READY = "ready"
CONN_NOT_READY = "not_ready"

AUTH_UNKNOWN = "unknown"
AUTH_AUTHENTICATED = "authenticated"
AUTH_REQUIRED = "auth_required"
AUTH_EXPIRED = "expired"

CHECK_PROBE = "probe"
CHECK_COLLECT_OK = "collect_ok"
CHECK_COLLECT_FAILED = "collect_failed"
CHECK_HEALTH_CACHE = "health_cache"

# X 凭据入口（G 第一条：X 只读进程环境变量 = 让用户去一个不存在的入口）。
X_CREDENTIAL_KEYS = ("x_auth_token", "x_ct0")
X_ENV_BY_KEY = {"x_auth_token": "TWITTER_AUTH_TOKEN", "x_ct0": "TWITTER_CT0"}
X_CREDENTIAL_ACTION = (
    "在「设置 → X 海外热点凭据」里填写 auth_token 与 ct0（保存后采集子进程即可读到；"
    "本接口不会回显它们的值）"
)
X_SECRET_KEYS = ("x_auth_token", "x_ct0")

_LABELS = {
    PLATFORM_BOSS: ("BOSS 岗位", "boss_research",
                    "BOSS 直聘岗位搜索与 JD；状态来自健康检查记录，未检查即 unknown"),
    PLATFORM_X: ("X 海外热点", "x_topics",
                 "消费信贷/消费金融/金融科技海外热点"),
    PLATFORM_XHS: ("小红书岗位讨论", "xhs_career",
                   "岗位/公司相关笔记与评论讨论"),
}


@dataclass(frozen=True)
class SourceStatus:
    """一个来源的**事实**状态（三层互不混淆，每层都可为 unknown）。"""

    platform: str
    dependency_state: str = DEP_UNKNOWN
    connection_state: str = CONN_UNKNOWN
    auth_state: str = AUTH_UNKNOWN
    capabilities: dict[str, Any] = field(default_factory=dict)
    provider_version: Optional[str] = None
    checked_at: Optional[str] = None
    check_source: Optional[str] = None
    last_error_code: Optional[str] = None
    last_error_summary: Optional[str] = None
    operator_action: str = ""
    note: str = ""
    # 诊断用（路径不是凭据，但普通视图默认不展开）
    exe_path: Optional[str] = None
    node_path: Optional[str] = None

    # -- 派生 -------------------------------------------------------------
    @property
    def label(self) -> str:
        return _LABELS.get(self.platform, (self.platform, "", ""))[0]

    @property
    def task_type(self) -> str:
        return _LABELS.get(self.platform, ("", "", ""))[1]

    @property
    def status(self) -> str:
        """粗粒度状态（**由三层派生**，不是第四个真相源）。

        依赖缺失 → ``dependency_missing``；连接未就绪 → ``not_ready``；
        未登录 → ``auth_required``；三层全 ready → ``ready``；任一 unknown → ``unknown``。
        """
        if self.dependency_state == DEP_MISSING:
            return "dependency_missing"
        if self.dependency_state == DEP_VERSION_UNSUPPORTED:
            return "version_unsupported"
        if self.dependency_state != DEP_READY:
            return "unknown"
        if self.connection_state == CONN_NOT_READY:
            return "not_ready"
        if self.auth_state in (AUTH_REQUIRED, AUTH_EXPIRED):
            return "auth_required"
        if (self.connection_state == CONN_READY and self.auth_state == AUTH_AUTHENTICATED):
            return "ready"
        return "unknown"

    def as_view(self) -> dict[str, Any]:
        """对外普通视图：只有事实与一行脱敏摘要，**不含凭据、不含路径**。"""
        return {
            "id": self.platform,
            "label": self.label,
            "task_type": self.task_type,
            "status": self.status,
            "dependency_state": self.dependency_state,
            "connection_state": self.connection_state,
            "auth_state": self.auth_state,
            "capabilities": dict(self.capabilities or {}),
            "provider_version": self.provider_version,
            "checked_at": self.checked_at,
            "check_source": self.check_source,
            "last_error_code": self.last_error_code,
            "detail": self.last_error_summary,
            "operator_action": self.operator_action,
            "note": self.note or self.label,
        }

    def as_diagnostic_view(self) -> dict[str, Any]:
        """诊断视图：额外给出依赖路径（路径不是凭据）。"""
        view = self.as_view()
        view.update({"exe_path": self.exe_path, "node_path": self.node_path})
        return view


# ---------------------------------------------------------------------------
# 凭据：只回答"有/无"，并把设置项桥接到子进程环境变量
# ---------------------------------------------------------------------------

def _setting(key: str) -> Optional[str]:
    try:
        row = store.query_one("SELECT value FROM app_setting WHERE key=?", (key,))
    except Exception:  # noqa: BLE001 - 库未就绪时按"没有配置"处理
        return None
    return row["value"] if row else None


def credential_present(key: str) -> bool:
    """凭据是否已配置（app_setting 或对应环境变量），**只回布尔，不回值**。"""
    if str(_setting(key) or "").strip():
        return True
    env_name = X_ENV_BY_KEY.get(key)
    return bool(env_name and str(os.environ.get(env_name) or "").strip())


def x_credentials_state() -> tuple[str, list[str]]:
    """X 凭据状态 → ``(auth_state, 缺失的配置项名)``（不回显任何值）。"""
    missing = [key for key in X_CREDENTIAL_KEYS if not credential_present(key)]
    if missing:
        return (AUTH_REQUIRED, missing)
    return (AUTH_AUTHENTICATED, [])


def apply_x_credentials_to_env(values: Optional[dict[str, str]] = None, *,
                               clear_missing: bool = False) -> dict[str, bool]:
    """把 X 凭据写进**本进程环境**，供采集子进程继承。

    为什么需要这一步：现有 X 适配器只读 ``TWITTER_AUTH_TOKEN`` / ``TWITTER_CT0``
    环境变量。设置页写了 ``app_setting`` 而进程环境没有变 → 用户以为配好了，
    采集子进程却读不到（指令 G 第一条"把用户支使到不存在的入口"的反面）。

    ``clear_missing`` 的语义很关键：
    - ``False``（默认，启动路径）：值为空就**跳过**，绝不清除外部的环境变量——
      否则会把用户在 shell / 部署环境里配好的凭据误删；
    - ``True``（用户显式清空设置项）：清掉对应环境变量，避免旧值继续冒充"已配置"。

    返回值只有"哪些键已注入"的布尔，**不含值**。
    """
    if values is None:
        values = {key: (_setting(key) or "") for key in X_CREDENTIAL_KEYS}
    result: dict[str, bool] = {}
    for key, value in values.items():
        if key not in X_ENV_BY_KEY:
            continue
        env_name = X_ENV_BY_KEY[key]
        text = str(value or "").strip()
        if text:
            os.environ[env_name] = text
            result[key] = True
        elif clear_missing:
            os.environ.pop(env_name, None)
            result[key] = False
        else:
            result[key] = bool(str(os.environ.get(env_name) or "").strip())
    return result


def load_credentials_from_settings() -> dict[str, bool]:
    """启动时把已保存的 X 凭据灌进进程环境（幂等，且绝不清除外来变量）。"""
    return apply_x_credentials_to_env(clear_missing=False)


# ---------------------------------------------------------------------------
# 依赖发现（只读，不发起采集）
# ---------------------------------------------------------------------------

def _handle_for(platform: str):
    """解析依赖启动组合；失败返回 ``(None, error_code, summary)``。

    只用 ``collectors.provider`` 的公开入口（T08 口径），失败一律 fail-closed——
    宁可报 missing/unknown，也不猜一个"应该没问题"。
    """
    try:
        from collectors import provider
    except Exception as exc:  # noqa: BLE001 - provider 尚未就绪 → 依赖判定为 missing
        return None, "DEPENDENCY_MISSING", social_redact.redact_exception(exc)
    try:
        if platform == PLATFORM_X:
            return provider.resolve_twitter_launch(), None, None
        if platform == PLATFORM_XHS:
            return provider.resolve_opencli_launch(), None, None
    except Exception as exc:  # noqa: BLE001 - CollectorError / OSError 都按依赖缺失处理
        code = str(getattr(exc, "code", "") or "DEPENDENCY_MISSING")
        summary = social_redact.redact(
            f"{type(exc).__name__}: {getattr(exc, 'message', exc)}")
        return None, code, summary[:200]
    return None, "DEPENDENCY_MISSING", "未知平台"


def _declared_capabilities(platform: str, handle) -> dict[str, Any]:
    """适配器**声明**的能力集（不是可用性证据，命名上明确标出）。"""
    try:
        if platform == PLATFORM_X:
            from collectors.x_source import XSource

            caps = XSource(str(handle.exe_path)).capabilities()
        elif platform == PLATFORM_XHS:
            from collectors.xhs_source import XhsSource

            caps = XhsSource(str(handle.exe_path)).capabilities()
        else:
            return {}
    except Exception:  # noqa: BLE001 - 声明读不到就不声明
        return {}
    view = {
        "search": bool(getattr(caps, "search", False)),
        "detail": bool(getattr(caps, "detail", False)),
        "comments": bool(getattr(caps, "comments", False)),
        "origin": "adapter_declared",
    }
    return view


# ---------------------------------------------------------------------------
# 落库 / 读取
# ---------------------------------------------------------------------------

_COLUMNS = (
    "platform", "session_key", "dependency_state", "connection_state", "auth_state",
    "capability_json", "provider_version", "exe_path", "node_path", "checked_at",
    "check_source", "last_error_code", "last_error_summary", "updated_at",
)


def record_status(status: SourceStatus, *, session_key: str = "default") -> None:
    """落库（按 (platform, session_key) upsert），摘要强制脱敏。"""
    conn = store.get_conn()
    conn.execute(
        """INSERT INTO social_source_status (id, platform, session_key, dependency_state,
             connection_state, auth_state, capability_json, provider_version, exe_path,
             node_path, checked_at, check_source, last_error_code, last_error_summary,
             updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(platform, session_key) DO UPDATE SET
             dependency_state=excluded.dependency_state,
             connection_state=excluded.connection_state,
             auth_state=excluded.auth_state,
             capability_json=excluded.capability_json,
             provider_version=excluded.provider_version,
             exe_path=excluded.exe_path,
             node_path=excluded.node_path,
             checked_at=excluded.checked_at,
             check_source=excluded.check_source,
             last_error_code=excluded.last_error_code,
             last_error_summary=excluded.last_error_summary,
             updated_at=excluded.updated_at""",
        (
            f"sss_{status.platform}_{session_key}", status.platform, session_key,
            status.dependency_state, status.connection_state, status.auth_state,
            store.jdumps(status.capabilities or {}), status.provider_version,
            status.exe_path, status.node_path, status.checked_at, status.check_source,
            status.last_error_code,
            social_redact.redact(status.last_error_summary)[:300]
            if status.last_error_summary else None,
            store.utcnow_iso(),
        ),
    )
    conn.commit()


def get_status(platform: str, *, session_key: str = "default") -> SourceStatus:
    """读已记录状态；**没有记录就是三层 unknown + checked_at=None**。"""
    row = store.query_one(
        "SELECT * FROM social_source_status WHERE platform=? AND session_key=?",
        (platform, session_key))
    if not row:
        return SourceStatus(platform=platform)
    return SourceStatus(
        platform=platform,
        dependency_state=str(row["dependency_state"] or DEP_UNKNOWN),
        connection_state=str(row["connection_state"] or CONN_UNKNOWN),
        auth_state=str(row["auth_state"] or AUTH_UNKNOWN),
        capabilities=store.jloads(row["capability_json"], {}) or {},
        provider_version=row["provider_version"],
        checked_at=row["checked_at"],
        check_source=row["check_source"],
        last_error_code=row["last_error_code"],
        last_error_summary=row["last_error_summary"],
        exe_path=row["exe_path"],
        node_path=row["node_path"],
    )


# ---------------------------------------------------------------------------
# 体检（只读探测：不发起采集）
# ---------------------------------------------------------------------------

def _xhs_channel_states() -> tuple[str, str, Optional[str], Optional[str]]:
    """小红书连接/认证状态 ← 采集通道的**真实记录**（只读 DB，不拉子进程）。

    返回 ``(connection_state, auth_state, error_code, summary)``。
    """
    status = get_status(PLATFORM_XHS)
    return (status.connection_state, status.auth_state,
            status.last_error_code, status.last_error_summary)


def record_collection(platform: str, *, error_code: Optional[str] = None,
                      message: str = "", operator_action: str = "") -> None:
    """来源状态只跟随本平台真实采集回执，不借用其它平台的认证记录。"""
    status = get_status(platform)
    auth = status.auth_state
    if error_code in ("AUTH_REQUIRED", "AUTH_EXPIRED"):
        auth = AUTH_REQUIRED if error_code == "AUTH_REQUIRED" else AUTH_EXPIRED
    elif error_code is None:
        auth = AUTH_AUTHENTICATED
    record_status(replace(status, dependency_state=DEP_MISSING if error_code == "DEPENDENCY_MISSING" else DEP_READY,
        connection_state=CONN_NOT_READY if error_code else CONN_READY,
        auth_state=auth, checked_at=store.utcnow_iso(),
        check_source=CHECK_COLLECT_FAILED if error_code else CHECK_COLLECT_OK,
        last_error_code=error_code, last_error_summary=message or None,
        operator_action=operator_action))


def _boss_status() -> SourceStatus:
    """BOSS 来源状态 ← 健康检查**已记录**的事实（只读，不触发任何子进程）。"""
    label, task_type, note = _LABELS[PLATFORM_BOSS]
    import channel_state
    channel = channel_state.channel_view()
    if (channel.get("state") == CONN_READY and channel.get("check_source") == CHECK_COLLECT_OK
            and channel_state.RUNTIME._collect_fresh(channel)):
        return SourceStatus(platform=PLATFORM_BOSS, dependency_state=DEP_READY,
            connection_state=CONN_READY, auth_state=AUTH_AUTHENTICATED,
            capabilities={"search":True,"detail":True,"origin":"collect_ok"},
            checked_at=channel.get("checked_at"), check_source=CHECK_COLLECT_OK,
            note="岗位搜索已实测；正文完整性按每份材料单独标注")
    if channel.get('browser_source') == 'existing-browser':
        # A stored-cookie health cache describes another channel; it cannot diagnose this profile.
        code = channel.get('last_error_code')
        return SourceStatus(platform=PLATFORM_BOSS,
            connection_state=channel.get('state') if channel.get('state') in (CONN_READY, CONN_NOT_READY) else CONN_UNKNOWN,
            auth_state=AUTH_REQUIRED if code == 'AUTH_REQUIRED' else AUTH_EXPIRED if code == 'AUTH_EXPIRED' else AUTH_UNKNOWN,
            checked_at=channel.get('checked_at'), check_source=channel.get('check_source'),
            last_error_code=code, last_error_summary=channel.get('last_error_summary'),
            note='专用浏览器连接状态；登录与搜索能力以本人确认和实际采集为准，未实测项保持 unknown')
    try:
        import health as health_mod

        layers = health_mod.cached_layer_view(
            ["cli_path", "local_credential", "online_auth", "recent_search"])
    except Exception:  # noqa: BLE001
        return SourceStatus(platform=PLATFORM_BOSS, note=note)

    dep = layers["cli_path"]
    cred = layers["local_credential"]
    online = layers["online_auth"]
    recent = layers["recent_search"]

    dependency_state = DEP_UNKNOWN
    if dep.status == "ok":
        dependency_state = DEP_READY
    elif dep.error_code == "DEPENDENCY_MISSING" or dep.status in ("error", "unavailable"):
        dependency_state = DEP_MISSING

    auth_state = AUTH_UNKNOWN
    cred_code = cred.error_code
    if cred.status == "ok" or online.status == "ok":
        auth_state = AUTH_AUTHENTICATED
    elif cred_code == "AUTH_REQUIRED":
        auth_state = AUTH_REQUIRED
    elif cred_code == "AUTH_EXPIRED":
        auth_state = AUTH_EXPIRED

    connection_state = CONN_READY if recent.status == "ok" else CONN_UNKNOWN
    checked_at = dep.checked_at or cred.checked_at or online.checked_at
    return SourceStatus(
        platform=PLATFORM_BOSS,
        dependency_state=dependency_state,
        connection_state=connection_state,
        auth_state=auth_state,
        checked_at=checked_at,
        check_source=CHECK_HEALTH_CACHE,
        last_error_code=(dep.error_code or cred_code),
        last_error_summary=(dep.error_message or cred.error_message),
        operator_action=(dep.operator_actions[0] if dep.operator_actions else ""),
        note=note,
        exe_path=(dep.evidence or {}).get("cli_path"),
    )


def check_platform(platform: str, *, session_key: str = "default") -> SourceStatus:
    """做一次**只读体检**并落库；返回体检后的状态。

    - 依赖：``collectors.provider`` 解析（不发子进程、不采集）；
    - 认证：X → 凭据是否已配置（只回答有/无）；小红书 → 通道记录；
    - 连接：X → 无浏览器通道证据，如实 unknown；小红书 → 通道状态。
    """
    note = _LABELS.get(platform, ("", "", ""))[2]
    if platform == PLATFORM_BOSS:
        return _boss_status()

    handle, error_code, summary = _handle_for(platform)
    if handle is None:
        status = SourceStatus(
            platform=platform, dependency_state=DEP_MISSING, connection_state=CONN_UNKNOWN,
            auth_state=AUTH_UNKNOWN, checked_at=store.utcnow_iso(),
            check_source=CHECK_PROBE, last_error_code=error_code,
            last_error_summary=summary, operator_action="检查采集依赖是否安装",
            note=note)
        record_status(status, session_key=session_key)
        return status

    capabilities = _declared_capabilities(platform, handle)
    if platform == PLATFORM_X:
        auth_state, missing = x_credentials_state()
        prior = get_status(platform, session_key=session_key)
        status = SourceStatus(
            platform=platform, dependency_state=DEP_READY,
            connection_state=CONN_UNKNOWN if missing else prior.connection_state,
            auth_state=auth_state, capabilities=capabilities,
            provider_version=handle.version, checked_at=store.utcnow_iso(),
            check_source=CHECK_PROBE,
            last_error_code=(prior.last_error_code if not missing else "AUTH_REQUIRED"),
            last_error_summary=(prior.last_error_summary if not missing
                                else "X 凭据尚未配置：" + "、".join(X_CREDENTIAL_KEYS)),
            operator_action=("" if not missing else X_CREDENTIAL_ACTION),
            note=note, exe_path=handle.exe_path, node_path=handle.node_path)
    else:
        connection_state, auth_state, code, summary = _xhs_channel_states()
        status = SourceStatus(
            platform=platform, dependency_state=DEP_READY,
            connection_state=connection_state, auth_state=auth_state,
            capabilities=capabilities, provider_version=handle.version,
            checked_at=store.utcnow_iso(), check_source=CHECK_PROBE,
            last_error_code=code, last_error_summary=summary,
            operator_action=("" if connection_state == CONN_READY
                             else "在已授权的浏览器里连接小红书采集通道后重试"),
            note=note, exe_path=handle.exe_path, node_path=handle.node_path)
    record_status(status, session_key=session_key)
    return status


def check_all(*, session_key: str = "default") -> list[SourceStatus]:
    return [check_platform(p, session_key=session_key) for p in PLATFORMS]


def all_statuses(*, refresh: bool = False, session_key: str = "default"
                 ) -> list[SourceStatus]:
    """三个来源的当前状态；``refresh=True`` 时先做一次只读体检。"""
    if refresh:
        return check_all(session_key=session_key)
    return [_boss_status()] + [get_status(p, session_key=session_key)
                               for p in (PLATFORM_X, PLATFORM_XHS)]


def sources_view(*, refresh: bool = False) -> dict[str, Any]:
    """/api/sources 的响应体（脱敏；凭据只以 has_value 出现，绝不带值）。"""
    return {
        "sources": [s.as_view() for s in all_statuses(refresh=refresh)],
        "checked": refresh,
        "credential_flags": {key: credential_present(key) for key in X_CREDENTIAL_KEYS},
        "redaction_rules": list(social_redact.REDACTION_RULES),
    }
