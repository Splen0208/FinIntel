"""采集通道状态：指纹、就绪判定、预检与采集结果回写（唯一判定入口）。

设计依据：`docs/channel-consistency-design.md` §0.1 / §0.3 / §1.3 / §2.3 / §2.4 / §2.8。

为什么需要它
------------
``status --live`` 只读本地凭据 + httpx，**验证不了浏览器通道**（上游
``commands/status.py`` 在本地无凭据时直接返回 AUTH_REQUIRED，根本不碰浏览器）。
所以「账号曾验证成功」（``login_task``）与「当前采集通道可用」（本模块）是两个
彼此独立的事实：前者语义不动，后者单独存、单独判定，顶栏与搜索前置检查**合起来读**。

三条铁律（已写进单测）
----------------------
1. **只存路径与状态**：``data_dir`` / ``cli_path`` 是路径不是凭据；cookie / token /
   stoken / wt2 / user_agent 一律不落库；``last_error_summary`` 强制过 ``redact()``。
   注意这条是**由 redact() 按形状兜住**的：``redact()`` 对
   ``cookie|set-cookie|authorization|x-auth*|user_agent`` 走行级规则（值含空格、
   分号也一并吃掉），对 ``token|api_key|secret|stoken|wt2`` 走单 token 规则。
   因此**未带键名的裸串不在保障范围内**（例如直接写进 summary 的一串裸十六进制
   长度 < 32 时不会被识别）；表结构层另有 ``test_channel_state_columns_never_include_credential_names`` 兜底。
2. **只有三类事件能写 state**：``collect_ok`` / ``collect_failed`` / ``doctor`` 预检；
   ``unknown`` 是初始值，**不是放行**。真实采集结果优先级最高，覆盖预检。
3. **指纹任一维变化 = 新通道**：``sha256(source|cdp_url|data_dir|cli_path|version)[:16]``，
   换维度即新行 ``unknown``，旧行的 ``ready`` 天然不为新通道放行。

低频有界：普通页面轮询只读表（``view()`` 不写库、不拉子进程）；预检只在
「登录成功」「设置变更」「诊断页显式切换」三个时机发生。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from store import get_conn, query_one, utcnow_iso
from upstream_config import (
    BROWSER_SOURCE_SETTING_KEY,
    CDP_URL_OPTION,
    CDP_URL_SETTING_KEY,
    redact,
    resolve_cli_path,
    resolve_upstream_data_dir,
    validate_browser_option,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SOURCE_AUTO = "auto"
SOURCE_EXISTING_BROWSER = "existing-browser"
SOURCE_STORED_COOKIE = "stored-cookie"
DEFAULT_SOURCE = SOURCE_AUTO

STATE_UNKNOWN = "unknown"
STATE_READY = "ready"
STATE_NOT_READY = "not_ready"
CHANNEL_STATES = (STATE_UNKNOWN, STATE_READY, STATE_NOT_READY)

CHECK_COLLECT_OK = "collect_ok"
CHECK_COLLECT_FAILED = "collect_failed"
CHECK_DOCTOR = "doctor"
CHECK_SOURCES = (CHECK_COLLECT_OK, CHECK_COLLECT_FAILED, CHECK_DOCTOR)

CHANNEL_CONFIG_VERSION_KEY = "channel_config_version"
DEFAULT_CONFIG_VERSION = 1
# 「上次真实取数成功」的有效期：超期不再视为该通道自证可用（凭证新鲜度门禁预留）
COLLECT_FRESHNESS_SECONDS = 24 * 60 * 60
DOCTOR_TIMEOUT_SECONDS = 20
# 修改这些设置 = 换通道：旧指纹行的 ready 一律失效
CHANNEL_SETTING_KEYS = frozenset({
    BROWSER_SOURCE_SETTING_KEY, CDP_URL_SETTING_KEY, "boss_cli_path", "upstream_data_dir",
})

EVIDENCE_BRIDGE = "bridge_extension"
EVIDENCE_CDP = "cdp"
EVIDENCE_NAMES = (EVIDENCE_BRIDGE, EVIDENCE_CDP)

CHECK_KEY = "channel"
# 任务快照里冻结通道的键名（``research_run.config_snapshot.channel``）
CHANNEL_KEY = CHECK_KEY
ERROR_SUMMARY_MAX_CHARS = 300
FINGERPRINT_LENGTH = 16

# 账号侧「已连接」的两种取值（login_bridge._boss_state 的输出口径）
CONNECTED_ACCOUNT_STATES = frozenset({"connected", "connected_degraded"})

_OK_STATUSES = frozenset({"ok", "pass", "passed", "ready", "healthy", "available", "true", "1"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(text: Optional[str]) -> Optional[datetime]:
    """解析 ``store.utcnow_iso()`` 写出的时间戳；解析不了返回 None（不猜）。"""
    if not text:
        return None
    try:
        return datetime.strptime(str(text).strip(), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _normalize_source(value: object) -> str:
    """``''`` 与未配置都归一为 ``auto``（语义完全等价，不制造多余指纹行）。"""
    text = str(value or "").strip()
    return text or SOURCE_AUTO


def _normalize_path(value: object) -> str:
    """路径归一：大小写与分隔符统一，保证同一目录只有一个指纹。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return os.path.normcase(str(Path(text).resolve()))
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(text))


def _check_ok(value: object) -> bool:
    """把 doctor 的单项 check 解析成布尔；**形状不认识一律判 False**（如实报告优先）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        for key in ("status", "state", "ok", "result"):
            if key in value:
                return _check_ok(value[key])
        return False
    if value is None:
        return False
    return str(value).strip().lower() in _OK_STATUSES


def _run_async(coroutine: Any) -> Any:
    """在同步上下文里跑一个协程；已在事件循环中时丢到独立线程（与 runs 同口径）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, coroutine).result(timeout=DOCTOR_TIMEOUT_SECONDS * 3)
    return asyncio.run(coroutine)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChannelFingerprint:
    """通道指纹的五个维度；任一维变化 = 新通道。"""

    browser_source: str = SOURCE_AUTO
    cdp_url: str = ""
    data_dir: str = ""
    cli_path: str = ""
    config_version: int = DEFAULT_CONFIG_VERSION

    def digest(self) -> str:
        """``sha256(source|cdp_url|data_dir|cli_path|version)[:16]``。"""
        payload = "|".join((
            _normalize_source(self.browser_source),
            str(self.cdp_url or "").strip(),
            _normalize_path(self.data_dir),
            _normalize_path(self.cli_path),
            str(int(self.config_version)),
        ))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]

    def as_view(self) -> dict[str, Any]:
        """通道身份（不含任何凭据；路径只进诊断视图）。"""
        return {
            "browser_source": _normalize_source(self.browser_source),
            "cdp_url": str(self.cdp_url or "").strip(),
            "config_version": int(self.config_version),
            "fingerprint": self.digest(),
        }


@dataclass(frozen=True)
class ChannelState:
    """一行 ``channel_state`` 的内存形态。"""

    fingerprint: str = ""
    browser_source: str = SOURCE_AUTO
    cdp_url: str = ""
    data_dir: str = ""
    cli_path: str = ""
    config_version: int = DEFAULT_CONFIG_VERSION
    state: str = STATE_UNKNOWN
    checked_at: Optional[str] = None
    check_source: Optional[str] = None
    last_collect_ok_at: Optional[str] = None
    last_collect_cards: int = 0
    last_error_code: Optional[str] = None
    last_error_summary: Optional[str] = None
    updated_at: Optional[str] = None

    def is_collect_fresh(self, now: Optional[datetime] = None) -> bool:
        """上次真实取数成功是否仍在有效期内（超期不再算「该通道已自证可用」）。"""
        stamp = _parse_iso(self.last_collect_ok_at)
        if stamp is None:
            return False
        return (now or _utcnow()) - stamp <= timedelta(seconds=COLLECT_FRESHNESS_SECONDS)

    def as_view(self) -> dict[str, Any]:
        """普通 UI 视图：只有状态与一行脱敏摘要，**不含路径、不含详细原因**。"""
        return {
            "browser_source": _normalize_source(self.browser_source),
            "cdp_url": str(self.cdp_url or "").strip(),
            "state": self.state,
            "checked_at": self.checked_at,
            "check_source": self.check_source,
            "last_collect_ok_at": self.last_collect_ok_at,
            "last_collect_cards": int(self.last_collect_cards or 0),
            "last_error_code": self.last_error_code,
            "detail": self.detail_line(),
            "fingerprint": self.fingerprint,
        }

    def as_diagnostic_view(self) -> dict[str, Any]:
        """诊断页视图：额外给出路径（路径不是凭据）与最近一次失败原因（已脱敏）。"""
        view = self.as_view()
        view.update({
            "data_dir": self.data_dir,
            "cli_path": self.cli_path,
            "config_version": int(self.config_version),
            "last_error_summary": self.last_error_summary,
            "collect_fresh": self.is_collect_fresh(),
        })
        return view

    def detail_line(self) -> str:
        """给顶栏用的一行事实摘要（脱敏，绝不含凭据值）。"""
        if self.last_collect_ok_at:
            return f"上次成功取数时间：{self.last_collect_ok_at}"
        return "上次成功取数时间：无"


def _row_to_state(row: Mapping[str, Any]) -> ChannelState:
    return ChannelState(
        fingerprint=str(row["fingerprint"] or ""),
        browser_source=str(row["browser_source"] or SOURCE_AUTO),
        cdp_url=str(row["cdp_url"] or ""),
        data_dir=str(row["data_dir"] or ""),
        cli_path=str(row["cli_path"] or ""),
        config_version=int(row["config_version"] or DEFAULT_CONFIG_VERSION),
        state=str(row["state"] or STATE_UNKNOWN),
        checked_at=row.get("checked_at"),
        check_source=row.get("check_source"),
        last_collect_ok_at=row.get("last_collect_ok_at"),
        last_collect_cards=int(row.get("last_collect_cards") or 0),
        last_error_code=row.get("last_error_code"),
        last_error_summary=row.get("last_error_summary"),
        updated_at=row.get("updated_at"),
    )


# ---------------------------------------------------------------------------
# 通道策略（与上游 POLICIES 对齐，不新增取值）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChannelPolicy:
    """某个 ``browser_source`` 下，预检需要什么证据、能否由预检定论。"""

    source: str = SOURCE_AUTO
    needs_bridge: bool = False
    needs_cdp: bool = False
    requires_real_collect: bool = True
    requires_explicit_cdp: bool = False

    @classmethod
    def for_source(cls, source: object, cdp_url: object = "") -> "ChannelPolicy":
        text = _normalize_source(source)
        if text == SOURCE_EXISTING_BROWSER:
            # bridge 或 cdp 任一可用即可（复用已打开的 zhipin 页签）
            return cls(source=text, needs_bridge=True, needs_cdp=True,
                       requires_real_collect=False, requires_explicit_cdp=False)
        if text == SOURCE_STORED_COOKIE:
            # 只连显式 CDP，不探测、不新建 context；必须显式填调试地址（诊断通道）
            return cls(source=text, needs_bridge=False, needs_cdp=True,
                       requires_real_collect=False, requires_explicit_cdp=True)
        # auto：bridge → cdp → headless，headless 兜底不依赖外部通道，
        # doctor 对它没有约束力 → 只能由真实采集证明。
        return cls(source=SOURCE_AUTO, needs_bridge=False, needs_cdp=False,
                   requires_real_collect=True, requires_explicit_cdp=False)

    @classmethod
    def from_settings(cls, getter: Optional[Callable[[str], Optional[str]]] = None
                      ) -> "ChannelPolicy":
        read = getter or _setting
        source = read(BROWSER_SOURCE_SETTING_KEY)
        cdp_url = read(CDP_URL_SETTING_KEY)
        return cls.for_source(source, cdp_url)

    def as_view(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "needs_bridge": self.needs_bridge,
            "needs_cdp": self.needs_cdp,
            "requires_real_collect": self.requires_real_collect,
            "requires_explicit_cdp": self.requires_explicit_cdp,
        }


# ---------------------------------------------------------------------------
# 预检（doctor，准入条件而非就绪证明）
# ---------------------------------------------------------------------------

class ChannelProbe:
    """复用 ``source_boss.ping()``（doctor）做有界预检。

    doctor **不接受** ``--browser-source``（源码确认），只接受 ``--cdp-url``；
    且 bridge 的连接布尔不证明目标页可用，所以预检只是**准入条件**，
    任何一次真实采集结果都会覆盖它。
    """

    def __init__(self, *, runner: Optional[Callable[..., dict]] = None,
                 timeout_seconds: int = DOCTOR_TIMEOUT_SECONDS) -> None:
        self._runner = runner
        self.timeout_seconds = int(timeout_seconds)

    @staticmethod
    def evidence_names() -> tuple[str, ...]:
        return EVIDENCE_NAMES

    @staticmethod
    def doctor_options(cdp_url: object) -> dict[str, str]:
        """doctor 只下发 ``--cdp-url``；``--browser-source`` 一律不下发。"""
        url = str(cdp_url or "").strip()
        return {CDP_URL_OPTION: url} if url else {}

    def run_doctor(self, cli_path: str, data_dir: object,
                   top_level_options: Optional[Mapping[str, str]] = None,
                   timeout_seconds: Optional[int] = None) -> dict[str, Any]:
        """跑一次 doctor，返回 ``{ok, checks, error_code, summary, elapsed_ms}``。"""
        import source_boss

        started = time.monotonic()
        options = dict(top_level_options or {})
        data_path = data_dir if hasattr(data_dir, "path") else Path(str(data_dir or ""))
        try:
            if self._runner is not None:
                # 注入的 runner（单测 / 受控环境）：签名与 source_boss.ping 保持一致
                result = self._runner(cli_path, data_path,
                                      int(timeout_seconds or self.timeout_seconds),
                                      top_level_options=options)
            else:
                result = _run_async(source_boss.ping(
                    cli_path,
                    data_path,
                    int(timeout_seconds or self.timeout_seconds),
                    top_level_options=options,
                ))
        except Exception as exc:  # 预检失败只作诊断，绝不抛出中断主流程
            return {
                "ok": False,
                "checks": {},
                "error_code": str(getattr(exc, "code", "") or "DOCTOR_FAILED"),
                "summary": redact(f"{type(exc).__name__}: {exc}")[:ERROR_SUMMARY_MAX_CHARS],
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
        checks: dict[str, Any] = {}
        for item in (result or {}).get("checks") or []:
            if isinstance(item, dict) and item.get("name"):
                checks[str(item["name"])] = item
        return {
            "ok": True,
            "checks": checks,
            "error_code": None,
            "summary": redact(str((result or {}).get("summary") or "doctor 完成"))
                       [:ERROR_SUMMARY_MAX_CHARS],
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }

    def precheck(self, policy: ChannelPolicy, cli_path: str, data_dir: object,
                 cdp_url: object = "",
                 timeout_seconds: Optional[int] = None) -> dict[str, Any]:
        """按策略组合 evidence；``decisive=False`` 表示预检不下结论（等真实采集）。"""
        if not cli_path:
            return {"ok": False, "decisive": False, "checks": {},
                    "error_code": "DEPENDENCY_MISSING",
                    "summary": "未找到 boss CLI，无法预检采集通道", "elapsed_ms": 0}
        if policy.requires_real_collect:
            return {"ok": False, "decisive": False, "checks": {}, "error_code": None,
                    "summary": "该通道的就绪只能由真实取数证明，预检不下结论", "elapsed_ms": 0}
        if policy.requires_explicit_cdp and not str(cdp_url or "").strip():
            return {"ok": False, "decisive": True, "checks": {},
                    "error_code": "CDP_URL_REQUIRED",
                    "summary": "该通道必须先显式填写本机调试地址", "elapsed_ms": 0}

        evidence = None
        if self._runner is None and str(cdp_url or "").strip():
            import managed_browser
            # 本项目专用窗口已有浏览器 UUID 回执，直接验证其本机连接。
            # 上游 doctor 还做其它在线检查，会在网络慢时把可用 CDP 误报为超时。
            if managed_browser.endpoint_for_profile(managed_browser.PROFILE) == str(cdp_url).strip():
                evidence = {"ok": True, "checks": {EVIDENCE_CDP: {"status": "ok"}},
                    "error_code": None, "summary": "已连接并验证本项目专用浏览器身份", "elapsed_ms": 0}
        if evidence is None:
            evidence = self.run_doctor(cli_path, data_dir, self.doctor_options(cdp_url),
                                       timeout_seconds)
        checks = evidence.get("checks") or {}
        bridge_ok = _check_ok(checks.get(EVIDENCE_BRIDGE))
        cdp_ok = _check_ok(checks.get(EVIDENCE_CDP))
        if policy.needs_bridge and policy.needs_cdp:
            decided = bridge_ok or cdp_ok
        elif policy.needs_cdp:
            decided = cdp_ok
        elif policy.needs_bridge:
            decided = bridge_ok
        else:
            decided = bool(evidence.get("ok"))
        summary = evidence.get("summary") or ""
        if not decided:
            summary = "预检未发现可用的浏览器会话（详见诊断页）"
        return {**evidence, "ok": bool(decided), "decisive": True, "summary": summary,
                "bridge_ok": bridge_ok, "cdp_ok": cdp_ok}


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------

class ChannelStore:
    """``channel_state`` 表的唯一写入口；只暴露设计里列出的六个动作。"""

    def get(self, fingerprint: ChannelFingerprint) -> Optional[ChannelState]:
        row = query_one("SELECT * FROM channel_state WHERE fingerprint=?",
                        (fingerprint.digest(),))
        return _row_to_state(row) if row else None

    def get_or_create(self, fingerprint: ChannelFingerprint) -> ChannelState:
        existing = self.get(fingerprint)
        if existing is not None:
            return existing
        connection = get_conn()
        connection.execute(
            """INSERT OR IGNORE INTO channel_state
               (fingerprint,browser_source,cdp_url,data_dir,cli_path,config_version,state,
                checked_at,check_source,last_collect_ok_at,last_collect_cards,
                last_error_code,last_error_summary,updated_at)
               VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL,0,NULL,NULL,?)""",
            (fingerprint.digest(), _normalize_source(fingerprint.browser_source),
             str(fingerprint.cdp_url or "").strip(), _normalize_path(fingerprint.data_dir),
             _normalize_path(fingerprint.cli_path), int(fingerprint.config_version),
             STATE_UNKNOWN, utcnow_iso()),
        )
        connection.commit()
        return self.get(fingerprint) or ChannelState(fingerprint=fingerprint.digest())

    def mark_collect_ok(self, fingerprint: ChannelFingerprint, cards: int = 0) -> ChannelState:
        """真实取数成功（信封 ok=true，卡片可以是 0）：通道自证可用，覆盖预检结论。"""
        self.get_or_create(fingerprint)
        now = utcnow_iso()
        connection = get_conn()
        connection.execute(
            """UPDATE channel_state SET state=?, checked_at=?, check_source=?,
                 last_collect_ok_at=?, last_collect_cards=?, last_error_code=NULL,
                 last_error_summary=NULL, updated_at=? WHERE fingerprint=?""",
            (STATE_READY, now, CHECK_COLLECT_OK, now, max(0, int(cards or 0)), now,
             fingerprint.digest()),
        )
        connection.commit()
        return self.get(fingerprint) or ChannelState()

    def mark_collect_failed(self, fingerprint: ChannelFingerprint, code: str,
                            summary: str = "") -> ChannelState:
        """真实取数失败：通道置 not_ready（摘要强制脱敏，绝不落凭据）。"""
        self.get_or_create(fingerprint)
        now = utcnow_iso()
        connection = get_conn()
        connection.execute(
            """UPDATE channel_state SET state=?, checked_at=?, check_source=?,
                 last_error_code=?, last_error_summary=?, updated_at=? WHERE fingerprint=?""",
            (STATE_NOT_READY, now, CHECK_COLLECT_FAILED, str(code or "")[:80] or None,
             redact(str(summary or ""))[:ERROR_SUMMARY_MAX_CHARS] or None, now,
             fingerprint.digest()),
        )
        connection.commit()
        return self.get(fingerprint) or ChannelState()

    def mark_precheck(self, fingerprint: ChannelFingerprint, evidence: Mapping[str, Any],
                      policy: Optional[ChannelPolicy] = None) -> ChannelState:
        """doctor 预检结论落库；**真实采集结果优先级更高**，预检不得降级已自证的通道。"""
        if policy is not None and policy.requires_real_collect:
            # auto：预检既不能证明也不能否定，保持原状态（unknown 不是放行）
            return self.get_or_create(fingerprint)
        ok = bool((evidence or {}).get("ok")) and bool((evidence or {}).get("decisive"))
        if ok:
            self.get_or_create(fingerprint)
            now = utcnow_iso()
            connection = get_conn()
            connection.execute(
                """UPDATE channel_state SET state=?, checked_at=?, check_source=?,
                     last_error_code=NULL, last_error_summary=NULL, updated_at=?
                   WHERE fingerprint=?""",
                (STATE_READY, now, CHECK_DOCTOR, now, fingerprint.digest()),
            )
            connection.commit()
            return self.get(fingerprint) or ChannelState()
        current = self.get_or_create(fingerprint)
        if current.state == STATE_READY:
            return current   # 真实取数已自证可用：预检不得把它降级
        now = utcnow_iso()
        connection = get_conn()
        connection.execute(
            """UPDATE channel_state SET state=?, checked_at=?, check_source=?,
                 last_error_code=?, last_error_summary=?, updated_at=? WHERE fingerprint=?""",
            (STATE_NOT_READY, now, CHECK_DOCTOR,
             str((evidence or {}).get("error_code") or "CHANNEL_PRECHECK_FAILED")[:80],
             redact(str((evidence or {}).get("summary") or "预检未通过"))
             [:ERROR_SUMMARY_MAX_CHARS], now, fingerprint.digest()),
        )
        connection.commit()
        return self.get(fingerprint) or ChannelState()

    def invalidate_all(self, config_version: Optional[int] = None) -> int:
        """换通道/换版本后让既有结论整体失效（state 回到 unknown，历史成功时间保留）。"""
        connection = get_conn()
        if config_version is None:
            cursor = connection.execute(
                """UPDATE channel_state SET state=?, checked_at=NULL, check_source=NULL""",
                (STATE_UNKNOWN,),
            )
        else:
            cursor = connection.execute(
                """UPDATE channel_state SET state=?, checked_at=NULL, check_source=NULL
                   WHERE config_version=?""",
                (STATE_UNKNOWN, int(config_version)),
            )
        connection.commit()
        return int(cursor.rowcount or 0)

    def rearm(self, fingerprint: ChannelFingerprint) -> ChannelState:
        """显式用户动作（点「继续」/改通道）后，允许再探测一次：not_ready → unknown。

        只允许**用户显式发起**，绝不用于自动重试；探测成败仍会回写，杜绝无限放行。
        """
        self.get_or_create(fingerprint)
        connection = get_conn()
        connection.execute(
            """UPDATE channel_state SET state=?, checked_at=NULL, check_source=NULL,
                 updated_at=? WHERE fingerprint=? AND state=?""",
            (STATE_UNKNOWN, utcnow_iso(), fingerprint.digest(), STATE_NOT_READY),
        )
        connection.commit()
        return self.get(fingerprint) or ChannelState()

    def view(self, fingerprint: ChannelFingerprint) -> dict[str, Any]:
        """只读视图：不存在时按 unknown 合成一行，**不写库、不拉子进程**。"""
        state = self.get(fingerprint)
        if state is None:
            return ChannelState(
                fingerprint=fingerprint.digest(),
                browser_source=_normalize_source(fingerprint.browser_source),
                cdp_url=str(fingerprint.cdp_url or "").strip(),
                data_dir=_normalize_path(fingerprint.data_dir),
                cli_path=_normalize_path(fingerprint.cli_path),
                config_version=int(fingerprint.config_version),
                state=STATE_UNKNOWN,
            ).as_view()
        return state.as_view()

    def diagnostic_view(self, fingerprint: ChannelFingerprint) -> dict[str, Any]:
        state = self.get(fingerprint)
        if state is None:
            return ChannelState(
                fingerprint=fingerprint.digest(),
                browser_source=_normalize_source(fingerprint.browser_source),
                cdp_url=str(fingerprint.cdp_url or "").strip(),
                data_dir=_normalize_path(fingerprint.data_dir),
                cli_path=_normalize_path(fingerprint.cli_path),
                config_version=int(fingerprint.config_version),
                state=STATE_UNKNOWN,
            ).as_diagnostic_view()
        return state.as_diagnostic_view()


# ---------------------------------------------------------------------------
# 运行时：指纹构造 / 任务冻结 / 采集门禁
# ---------------------------------------------------------------------------

class ChannelRuntime:
    """把「构造指纹 / 冻结到任务 / 校验未变 / 换通道 / 放行判定」收在一处。"""

    def __init__(self, store: Optional[ChannelStore] = None,
                 probe: Optional[ChannelProbe] = None) -> None:
        self.store = store or ChannelStore()
        self.probe = probe or ChannelProbe()

    # -- 指纹 ---------------------------------------------------------------

    def fingerprint_for_run(self, run: Mapping[str, Any]) -> Optional[ChannelFingerprint]:
        """从任务快照还原冻结通道；未冻结返回 None（调用方必须先冻结）。"""
        snapshot = (run or {}).get("config_snapshot") or {}
        channel = snapshot.get(CHANNEL_KEY) if isinstance(snapshot, Mapping) else None
        if not isinstance(channel, Mapping) or not channel.get("fingerprint"):
            return None
        return fingerprint_from_channel(channel)

    def fingerprint_for_settings(self) -> ChannelFingerprint:
        policy = current_policy()
        return ChannelFingerprint(
            browser_source=policy.source,
            cdp_url=_setting(CDP_URL_SETTING_KEY) or "",
            data_dir=str(resolve_upstream_data_dir(create=False).path),
            cli_path=resolve_cli_path(_setting("boss_cli_path")) or "",
            config_version=current_config_version(),
        )

    def freeze(self, *, browser_source: Optional[str] = None, cdp_url: Optional[str] = None,
               data_dir: Optional[str] = None, cli_path: Optional[str] = None,
               config_version: Optional[int] = None) -> dict[str, Any]:
        """冻结一份通道快照（写进 ``research_run.config_snapshot.channel``）。"""
        policy = (ChannelPolicy.for_source(browser_source) if browser_source is not None
                  else current_policy())
        resolved_data_dir = (str(data_dir) if data_dir
                             else str(resolve_upstream_data_dir(create=False).path))
        resolved_cli = str(cli_path) if cli_path else (resolve_cli_path(
            _setting("boss_cli_path")) or "")
        resolved_cdp = (str(cdp_url) if cdp_url is not None
                        else (_setting(CDP_URL_SETTING_KEY) or ""))
        fingerprint = ChannelFingerprint(
            browser_source=policy.source,
            cdp_url=str(resolved_cdp or "").strip(),
            data_dir=resolved_data_dir,
            cli_path=resolved_cli,
            config_version=int(config_version if config_version is not None
                               else current_config_version()),
        )
        return {
            "browser_source": fingerprint.browser_source,
            "cdp_url": fingerprint.cdp_url,
            "data_dir": _normalize_path(fingerprint.data_dir),
            "cli_path": _normalize_path(fingerprint.cli_path),
            "config_version": fingerprint.config_version,
            "fingerprint": fingerprint.digest(),
            "frozen_at": utcnow_iso(),
        }

    def freeze_into(self, run_id: str, fingerprint: ChannelFingerprint) -> dict[str, Any]:
        """把新通道写进任务快照（换通道恢复的唯一出口）。"""
        import runs

        run = runs.get_run(run_id)
        snapshot = dict(run.get("config_snapshot") or {})
        snapshot[CHANNEL_KEY] = self.freeze(
            browser_source=fingerprint.browser_source,
            cdp_url=fingerprint.cdp_url,
            data_dir=fingerprint.data_dir,
            cli_path=fingerprint.cli_path,
            config_version=fingerprint.config_version,
        )
        snapshot["channel_changed_at"] = utcnow_iso()
        connection = get_conn()
        connection.execute("UPDATE research_run SET config_snapshot=? WHERE id=?",
                           (json_dumps(snapshot), run_id))
        connection.commit()
        return snapshot[CHANNEL_KEY]

    def assert_unchanged(self, run: Mapping[str, Any],
                         fingerprint: ChannelFingerprint) -> None:
        """同一任务的 search 与 detail 之间不允许隐式换通道。"""
        frozen = self.fingerprint_for_run(run)
        if frozen is None:
            return
        if frozen.digest() != fingerprint.digest():
            from upstream_envelope import FailureLayer, UpstreamError

            raise UpstreamError(
                "CHANNEL_CHANGED",
                "任务内采集通道发生变更，已停止（同一任务的 search 与 detail 必须同一通道）",
                False,
                "如需换通道恢复，请显式走诊断页的通道切换，再重新验证后继续",
                layer=FailureLayer.CONTRACT,
            )

    def change_run_channel(self, run_id: str, patch: Mapping[str, Any]) -> dict[str, Any]:
        """诊断页显式换通道：校验取值 → 重新冻结 → 预检 → 回写快照。"""
        import runs

        run = runs.get_run(run_id)
        snapshot = dict(run.get("config_snapshot") or {})
        current = snapshot.get(CHANNEL_KEY) if isinstance(snapshot.get(CHANNEL_KEY), Mapping) else {}
        source = str(patch.get("browser_source", current.get("browser_source", "")) or "")
        cdp_url = str(patch.get("cdp_url", current.get("cdp_url", "")) or "")
        # 空值 = 关闭该通道项，一律放行；非空值走既有白名单校验
        validate_browser_option(BROWSER_SOURCE_SETTING_KEY, source)
        validate_browser_option(CDP_URL_SETTING_KEY, cdp_url)
        channel = self.freeze(browser_source=source, cdp_url=cdp_url,
                              data_dir=current.get("data_dir"),
                              cli_path=current.get("cli_path"))
        policy = ChannelPolicy.for_source(channel["browser_source"], channel["cdp_url"])
        evidence = self.probe.precheck(policy, channel["cli_path"], channel["data_dir"],
                                       channel["cdp_url"])
        self.store.mark_precheck(fingerprint_from_channel(channel), evidence, policy)
        snapshot[CHANNEL_KEY] = channel
        snapshot["channel_changed_at"] = utcnow_iso()
        connection = get_conn()
        connection.execute("UPDATE research_run SET config_snapshot=? WHERE id=?",
                           (json_dumps(snapshot), run_id))
        connection.commit()
        return {
            "run_id": run_id,
            "channel": channel,
            "policy": policy.as_view(),
            "evidence": evidence,
            "state": self.store.view(fingerprint_from_channel(channel))["state"],
        }

    # -- 门禁 ---------------------------------------------------------------

    def collect_gate(self, account_state: str, channel_view: Optional[Mapping[str, Any]],
                     policy: ChannelPolicy) -> bool:
        """顶栏与搜索前置检查**共用**的采集门禁（设计 §2.4）。

        - ``auto``：账号必须 connected（含降级连接），通道 ready 放行；
          ``unknown`` 只允许**一次**有界探测性搜索；``not_ready`` 不放行。
        - ``existing-browser``：**不要求本地账号 connected**（凭据只在浏览器里），
          只看该通道自己的证据：ready → 放行；not_ready → 不放行；
          unknown → 仅在「上次真实取数成功仍在有效期内」时放行。
        - ``stored-cookie``：诊断通道，需账号 connected + 显式 cdp_url + ready。

        任何一路拿不到证据就如实返回 False：**不许为了让顶栏变绿而放宽判定**。
        """
        view = dict(channel_view or {})
        state = str(view.get("state") or STATE_UNKNOWN)
        account = str(account_state or "")
        if policy.source == SOURCE_EXISTING_BROWSER:
            if state == STATE_READY:
                return True
            if state == STATE_NOT_READY:
                return False
            return self._collect_fresh(view)
        if policy.source == SOURCE_STORED_COOKIE:
            if account not in CONNECTED_ACCOUNT_STATES:
                return False
            if not str(view.get("cdp_url") or "").strip():
                return False
            return state == STATE_READY
        # auto（默认）
        if account not in CONNECTED_ACCOUNT_STATES:
            return False
        if state == STATE_READY:
            return True
        if state == STATE_NOT_READY:
            return False
        return True   # unknown：放行一次真实搜索，成败都回写，杜绝反复放行

    @staticmethod
    def _collect_fresh(view: Mapping[str, Any]) -> bool:
        stamp = _parse_iso(view.get("last_collect_ok_at"))
        if stamp is None:
            return False
        return _utcnow() - stamp <= timedelta(seconds=COLLECT_FRESHNESS_SECONDS)


def json_dumps(value: Any) -> str:
    """与 ``store.jdumps`` 同口径（此处避免与 store 形成多余耦合）。"""
    import json

    return json.dumps(value, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 模块级单例与便捷函数（供 login_bridge / pipeline / runs / app 使用）
# ---------------------------------------------------------------------------

STORE = ChannelStore()
PROBE = ChannelProbe()
RUNTIME = ChannelRuntime(store=STORE, probe=PROBE)


def _setting(key: str) -> Optional[str]:
    row = query_one("SELECT value FROM app_setting WHERE key=?", (key,))
    return row["value"] if row else None


def current_config_version() -> int:
    raw = _setting(CHANNEL_CONFIG_VERSION_KEY)
    try:
        return max(1, int(str(raw or DEFAULT_CONFIG_VERSION).strip()))
    except (TypeError, ValueError):
        return DEFAULT_CONFIG_VERSION


def bump_config_version() -> int:
    """通道配置版本号 +1：旧指纹行的 ready 一律不再生效。"""
    version = current_config_version() + 1
    connection = get_conn()
    connection.execute(
        "INSERT INTO app_setting (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (CHANNEL_CONFIG_VERSION_KEY, str(version)),
    )
    connection.commit()
    return version


def current_policy() -> ChannelPolicy:
    return ChannelPolicy.from_settings(_setting)


def current_fingerprint() -> ChannelFingerprint:
    return RUNTIME.fingerprint_for_settings()


def fingerprint_from_channel(channel: Optional[Mapping[str, Any]]) -> ChannelFingerprint:
    """从冻结快照还原指纹（缺字段即按默认归一，绝不回落全局设置）。"""
    payload = dict(channel or {})
    return ChannelFingerprint(
        browser_source=_normalize_source(payload.get("browser_source")),
        cdp_url=str(payload.get("cdp_url") or "").strip(),
        data_dir=str(payload.get("data_dir") or ""),
        cli_path=str(payload.get("cli_path") or ""),
        config_version=int(payload.get("config_version") or DEFAULT_CONFIG_VERSION),
    )


def freeze_channel(**overrides: Any) -> dict[str, Any]:
    """冻结当前生效通道，返回写进 ``config_snapshot.channel`` 的字典。"""
    return RUNTIME.freeze(**overrides)


def channel_view() -> dict[str, Any]:
    """普通 UI 视图（只读 DB，不写库、不拉子进程）。"""
    return STORE.view(current_fingerprint())


def channel_diagnostic_view() -> dict[str, Any]:
    """诊断页视图：含路径与最近一次失败原因（已脱敏）。"""
    return STORE.diagnostic_view(current_fingerprint())


def collect_gate(account_state: str, channel: Optional[Mapping[str, Any]] = None,
                 policy: Optional[ChannelPolicy] = None) -> bool:
    """顶栏与搜索前置检查共用的唯一门禁入口。"""
    view = channel if channel is not None else channel_view()
    return RUNTIME.collect_gate(account_state, view, policy or current_policy())


def _as_fingerprint(target: Any) -> ChannelFingerprint:
    if isinstance(target, ChannelFingerprint):
        return target
    if isinstance(target, Mapping):
        return fingerprint_from_channel(target)
    return current_fingerprint()


def record_collect_ok(channel: Any, cards: int = 0) -> dict[str, Any]:
    """真实取数成功（search 信封 ok=true）→ 通道 ready。"""
    fingerprint = _as_fingerprint(channel)
    STORE.mark_collect_ok(fingerprint, cards)
    return STORE.view(fingerprint)


def record_collect_failed(channel: Any, code: str, summary: str = "") -> dict[str, Any]:
    """真实取数失败 → 通道 not_ready（摘要脱敏后落库）。"""
    fingerprint = _as_fingerprint(channel)
    STORE.mark_collect_failed(fingerprint, code, summary)
    return STORE.view(fingerprint)


def rearm_current(channel: Any = None) -> dict[str, Any]:
    """用户显式「继续」后允许再探测一次（仅 not_ready → unknown）。"""
    fingerprint = _as_fingerprint(channel)
    STORE.rearm(fingerprint)
    return STORE.view(fingerprint)


def invalidate_all(config_version: Optional[int] = None) -> int:
    """换通道设置后让既有结论失效。"""
    return STORE.invalidate_all(config_version)


def precheck_current(*, reason: str = "") -> dict[str, Any]:
    """有界预检：只在登录成功 / 设置变更 / 诊断页显式切换时调用。"""
    fingerprint = current_fingerprint()
    policy = current_policy()
    evidence = PROBE.precheck(policy, fingerprint.cli_path, fingerprint.data_dir,
                              fingerprint.cdp_url)
    STORE.mark_precheck(fingerprint, evidence, policy)
    view = STORE.view(fingerprint)
    return {**evidence, "state": view["state"], "channel": view, "reason": reason}


def channel_options(channel: Optional[Mapping[str, Any]]) -> dict[str, str]:
    """任务冻结通道 → 顶层 ``--browser-source`` / ``--cdp-url``（**永不**读全局设置）。"""
    from upstream_config import channel_options_from_snapshot

    return channel_options_from_snapshot(channel)


__all__ = [
    "CHANNEL_KEY",
    "CHANNEL_SETTING_KEYS",
    "CHANNEL_STATES",
    "CHECK_COLLECT_FAILED",
    "CHECK_COLLECT_OK",
    "CHECK_DOCTOR",
    "CONNECTED_ACCOUNT_STATES",
    "ChannelFingerprint",
    "ChannelPolicy",
    "ChannelProbe",
    "ChannelRuntime",
    "ChannelState",
    "ChannelStore",
    "PROBE",
    "RUNTIME",
    "SOURCE_AUTO",
    "SOURCE_EXISTING_BROWSER",
    "SOURCE_STORED_COOKIE",
    "STATE_NOT_READY",
    "STATE_READY",
    "STATE_UNKNOWN",
    "STORE",
    "bump_config_version",
    "channel_diagnostic_view",
    "channel_options",
    "channel_view",
    "collect_gate",
    "current_config_version",
    "current_fingerprint",
    "current_policy",
    "fingerprint_from_channel",
    "freeze_channel",
    "invalidate_all",
    "precheck_current",
    "rearm_current",
    "record_collect_failed",
    "record_collect_ok",
]
