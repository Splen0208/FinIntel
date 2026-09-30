# -*- coding: utf-8 -*-
"""社交采集统一契约（任务书 §8.1）。

- ``Collector`` 薄接口：capabilities / search / detail / comments。
- 统一错误码与结构化结果；不支持的能力返回 UNSUPPORTED_CAPABILITY。
- comments 不能默认返回空数组冒充成功。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence


# 统一错误码（任务书 §8.1；增量设计 §5.1：唯一来源，pipeline 的 STOP_CODES 应从此导入）
ERROR_CODES = (
    "DEPENDENCY_MISSING",
    "CONNECTION_UNAVAILABLE",
    "AUTH_REQUIRED",
    "AUTH_EXPIRED",
    "VERIFICATION_REQUIRED",
    "ENVIRONMENT_RISK",
    "ACCOUNT_RISK",
    "RATE_LIMITED",
    "TIMEOUT",
    "CONTRACT_CHANGED",
    "DETAIL_REDIRECTED",
    "UNSUPPORTED_CAPABILITY",
    # 增量设计 §5.1 新增
    "OUTPUT_LIMIT_EXCEEDED",
    "LOCATOR_MISSING",
    "LOCATOR_EXPIRED",
    "LEASE_BUSY",
    "BUDGET_EXHAUSTED",
    "PROVIDER_NOT_FROZEN",
    "AI_BATCH_FAILED",
)


# ---------------------------------------------------------------------------
# 空列表语义（指令 C2）：空列表必须能区分三种语义，不得一律退化成 [] 冒充成功
# ---------------------------------------------------------------------------
RESULT_SEMANTICS_OK = "ok"                              # 有内容返回
RESULT_SEMANTICS_EMPTY_OK = "empty_ok"                 # 上游明示总数为 0：确实没有
RESULT_SEMANTICS_EMPTY_UNVERIFIED = "empty_unverified"  # 上游返回 0 条，但无法区分"确实没有/被限制/未渲染"
RESULT_SEMANTICS_HIDDEN = "hidden_by_platform"          # 平台明示隐藏/限制：必须报错，绝不当 0 条

# 发布时间质量（指令 C3 / 增量设计 §5.5）
PUBLISHED_ABSOLUTE = "absolute"                 # 可解析的绝对时间，已归一化为 UTC ISO8601
PUBLISHED_DATE_ONLY = "date_only"               # 只有日期（小红书搜索/评论常见），不伪造时刻
PUBLISHED_RELATIVE = "relative_unparsed"        # "3 天前" 之类相对时间：只保留 raw，published_at 置 NULL
PUBLISHED_UNPARSED = "unparsed"                 # 无法识别的时间串：只保留 raw
PUBLISHED_UNKNOWN = "unknown"                   # 上游根本没给时间


def stable_anon_key(platform: str, raw: Optional[str]) -> Optional[str]:
    """把平台上的公开作者标识映射为稳定匿名键（增量设计 §5.3）。

    **不要**号称匿名却直接存明文 screen_name。公开展示名如确需展示，
    应另存 ``author_display`` 字段（本轮适配器不落明文）。

    同一个 (platform, raw) 永远得到同一个键，因此可用于"同一作者"的去重口径；
    但注意：**匿名键不同 ≠ 独立来源不同**，去重口径是 ``(platform, source_id)``。
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    digest = hashlib.sha256((str(platform) + "\x1f" + text).encode("utf-8")).hexdigest()
    return "anon_" + digest[:16]


_RELATIVE_TIME_RE = re.compile(
    r"(刚刚|\d+\s*秒前|\d+\s*分钟前|\d+\s*小时前|\d+\s*天前|昨天|前天|"
    r"\d+\s*周前|\d+\s*个月前|\d+\s*年前)")
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TWITTER_TIME_RE = re.compile(
    r"^[A-Za-z]{3}\s+([A-Za-z]{3})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\s+([+-]\d{4})\s+(\d{4})$")
_MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
           "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}


def normalize_published_at(raw: Any) -> tuple[Optional[str], Optional[str], str]:
    """把上游时间串归一化为 ``(published_at, published_at_raw, quality)``。

    规则（指令 C3）：
    - 可解析的绝对时间 → 归一化成 UTC ISO8601（``...Z``），quality=``absolute``；
    - 只有日期（``YYYY-MM-DD``）→ 原样保留，**不伪造** 00:00:00 时刻，quality=``date_only``；
    - 相对时间（"3 天前"/"昨天"）→ ``published_at=None``，原文进 ``published_at_raw``，
      quality=``relative_unparsed``；**绝不**把"3 天前"当 UTC 时间写入 published_at；
    - 其它无法识别的串 → 只保留 raw，quality=``unparsed``。
    """
    if raw is None:
        return (None, None, PUBLISHED_UNKNOWN)
    if isinstance(raw, (int, float)):
        return (None, str(raw), PUBLISHED_UNPARSED)
    text = str(raw).strip()
    if not text:
        return (None, None, PUBLISHED_UNKNOWN)
    raw_out: str = text

    iso = _parse_iso8601(text)
    if iso is not None:
        return (iso, raw_out, PUBLISHED_ABSOLUTE)

    tw = _TWITTER_TIME_RE.match(text)
    if tw:
        try:
            month = _MONTHS[tw.group(1)]
            day = int(tw.group(2))
            hour, minute, second = int(tw.group(3)), int(tw.group(4)), int(tw.group(5))
            offset_sign = 1 if tw.group(6)[0] == "+" else -1
            offset_minutes = int(tw.group(6)[1:3]) * 60 + int(tw.group(6)[3:5])
            year = int(tw.group(7))
            moment = datetime(year, month, day, hour, minute, second,
                              tzinfo=timezone(offset_sign * timedelta(minutes=offset_minutes)))
            return (moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    raw_out, PUBLISHED_ABSOLUTE)
        except (KeyError, ValueError):
            return (None, raw_out, PUBLISHED_UNPARSED)

    if _DATE_ONLY_RE.match(text):
        return (text, raw_out, PUBLISHED_DATE_ONLY)

    if _RELATIVE_TIME_RE.search(text):
        return (None, raw_out, PUBLISHED_RELATIVE)

    return (None, raw_out, PUBLISHED_UNPARSED)


def _parse_iso8601(text: str) -> Optional[str]:
    """解析 ISO8601 绝对时间为 UTC ``YYYY-MM-DDTHH:MM:SSZ``；失败返回 None。"""
    candidate = text.strip()
    if "T" not in candidate and " " in candidate:
        candidate = candidate.replace(" ", "T", 1)
    if not re.match(r"^\d{4}-\d{2}-\d{2}[T ]", candidate):
        return None
    normalized = candidate.replace("Z", "+00:00").replace("z", "+00:00")
    if re.search(r"[+-]\d{2}:\d{2}$", normalized) is None and "+" not in normalized[10:] \
            and "-" not in normalized[10:]:
        normalized = normalized + "+00:00"
    try:
        moment = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def looks_like_manifest(payload: Any) -> bool:
    """判断上游是否把"命令清单/帮助文本"当业务响应返回。

    本机已复现的真实失败：解析到的 opencli 被执行后未识别 argv，
    直接把命令清单 JSON 打印到 stdout。即使 ``returncode == 0``，
    这种响应也不是业务结果，绝不能当"0 条"。
    """
    if isinstance(payload, dict):
        if isinstance(payload.get("commands"), list) and ("name" in payload or "version" in payload):
            return True
        if isinstance(payload.get("manifest"), (list, dict)):
            return True
        return False
    if isinstance(payload, list) and payload:
        sample = [p for p in payload if isinstance(p, dict)][:5]
        if not sample:
            return False
        return all(("site" in p and "name" in p and ("columns" in p or "modulePath" in p))
                   for p in sample)
    return False


def error_envelope_code(payload: Any) -> Optional[str]:
    """识别 ``{ok:false, error:{code,...}}`` 错误信封，返回上游错误码；否则 None。

    twitter-cli 0.8.5 的真实信封：``{"ok": false, "schema_version": "1",
    "error": {"code": "not_authenticated", "message": "..."}}``。
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("ok") is False or payload.get("success") is False:
        err = payload.get("error") or payload.get("err")
        if isinstance(err, dict):
            return str(err.get("code") or err.get("type") or "UNKNOWN")[:64]
        return str(err or "UNKNOWN")[:64]
    return None


def map_upstream_error_code(code: str, platform_label: str) -> tuple[str, str]:
    """把上游错误码映射为统一错误码与说明（结构化优先，文案只作有界补充）。

    返回 ``(统一错误码, 说明)``。
    """
    upper = str(code or "").upper()
    if "NOT_AUTHENTICATED" in upper or "AUTH_REQUIRED" in upper or upper == "AUTH":
        return ("AUTH_REQUIRED", f"{platform_label} 未登录或登录态失效")
    if "AUTH_EXPIRED" in upper or "TOKEN_EXPIRED" in upper:
        return ("AUTH_EXPIRED", f"{platform_label} 登录态已过期")
    if "SECURITY_BLOCK" in upper or "ENVIRONMENT_RISK" in upper or "RISK" in upper:
        return ("VERIFICATION_REQUIRED", f"{platform_label} 触发平台验证/风险限制，采集已暂停")
    if "RATE" in upper or upper == "429" or "TOO_MANY" in upper:
        return ("RATE_LIMITED", f"{platform_label} 接口限流")
    if "DEPENDENCY" in upper or "TOOL_NOT_ALLOWED" in upper or "NOT_FOUND" in upper:
        return ("DEPENDENCY_MISSING", f"{platform_label} 依赖不可用或调用被拒绝")
    if "TIMEOUT" in upper:
        return ("TIMEOUT", f"{platform_label} 采集超时")
    return ("CONNECTION_UNAVAILABLE", f"{platform_label} 上游返回失败：{str(code)[:64]}")


def int_or_none(value: Any) -> Optional[int]:
    """把上游计数（可能是 "1.2万" / "12,345" / "3.4k"）解析为 int。

    **缺字段/不可解析一律返回 None，绝不填 0**（指令 C2/C4：0 是"确实是 0"的断言，
    None 才是"未知"）。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else int(round(value))
    text = str(value).strip().replace(",", "").replace("，", "")
    if not text:
        return None
    match = re.match(r"^(\d+(?:\.\d+)?)\s*([wWkK千万])?\+?$", text)
    if not match:
        return None
    try:
        number = float(match.group(1))
    except ValueError:
        return None
    unit = (match.group(2) or "").lower()
    if unit in ("w", "万"):
        number *= 10000
    elif unit in ("k", "千"):
        number *= 1000
    return int(round(number))


class CollectorError(Exception):
    """稳定的采集错误，携带面向用户与诊断两层信息。"""

    def __init__(self, code: str, message: str, *,
                 retryable: bool = False,
                 operator_action: str = "",
                 upstream_code: Optional[str] = None,
                 evidence: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.operator_action = operator_action
        self.upstream_code = upstream_code
        self.evidence = evidence or {}


@dataclass
class CapabilitySet:
    """采集能力声明 + 验证时间（任务书 §8.1）。"""
    platform: str
    search: bool = False
    detail: bool = False
    comments: bool = False
    replies: bool = False
    time_filter: bool = False
    sort: bool = False
    pagination: bool = False
    verified_at: Optional[str] = None

    def as_view(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "search": self.search,
            "detail": self.detail,
            "comments": self.comments,
            "replies": self.replies,
            "time_filter": self.time_filter,
            "sort": self.sort,
            "pagination": self.pagination,
            "verified_at": self.verified_at,
        }


@dataclass
class ContentRecord:
    """一条标准化内容（帖子/笔记）。缺字段必须显式表达（None，不填 0）。"""
    platform: str
    source_id: str
    canonical_url: Optional[str]
    title: Optional[str]
    evidence_text: str
    language: Optional[str]
    published_at: Optional[str]
    fetched_at: str
    detail_status: str          # complete / partial / summary / missing
    author_key: Optional[str]
    metrics: dict[str, Any] = field(default_factory=dict)   # like/reply/repost/view/bookmark（可空）
    identity_quality: str = "platform"
    # 指令 C3：平台原始时间串（可能是"3 天前"）与时间质量，必须原样保留
    published_at_raw: Optional[str] = None
    published_at_quality: str = PUBLISHED_UNKNOWN
    # 指令 C1：正文是否已被证明属于目标条目。False 表示"未验证"，不是"已否定"
    source_url_verified: bool = False


@dataclass
class CommentRecord:
    """一条标准化评论（含父子关系）。"""
    source_comment_id: str
    parent_source_id: Optional[str]
    root_source_id: Optional[str]
    author_key: Optional[str]
    evidence_text: str
    published_at: Optional[str]
    fetched_at: str
    content_status: str          # complete / partial / truncated
    like_count: Optional[int] = None
    parent_missing: bool = False
    identity_quality: str = "platform"
    # 指令 C3：原始相对时间必须保留，不得把"3 天前"当 UTC 时间写进 published_at
    published_at_raw: Optional[str] = None
    published_at_quality: str = PUBLISHED_UNKNOWN


@dataclass
class SearchBatch:
    """一次搜索/采集的标准批量结果。"""
    records: list[ContentRecord] = field(default_factory=list)
    next_cursor: Optional[str] = None
    has_more: Optional[bool] = None
    completeness: str = "full"   # full / partial / summary_only
    warnings: list[str] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)


@dataclass
class CommentBatch:
    """一次评论读取的标准批量结果。

    ``result_semantics`` 用于表达"空列表到底是哪种空"（指令 C2）：
    - ``empty_ok``：上游明示总数 0，确实没有评论；
    - ``empty_unverified``：上游返回 0 条，但无法区分"确实没有 / 被隐藏 / 未渲染"；
    - ``hidden_by_platform``：平台明示隐藏或限制（此时适配器**抛错**，不返回空批次）。
    """
    comments: list[CommentRecord] = field(default_factory=list)
    next_cursor: Optional[str] = None
    has_more: Optional[bool] = None
    reported_total: Optional[int] = None
    root_count: Optional[int] = None
    reply_count: Optional[int] = None
    sort: Optional[str] = None
    stop_reason: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    result_semantics: Optional[str] = None
    # 上游 --limit 的语义：小红书是"根评论上限"，回复不计入该上限（B4）
    limit_semantics: Optional[str] = None


__all__ = [
    "CapabilitySet", "CollectorError", "ContentRecord", "CommentRecord",
    "SearchBatch", "CommentBatch", "ERROR_CODES",
    "stable_anon_key", "normalize_published_at", "looks_like_manifest",
    "error_envelope_code", "map_upstream_error_code", "int_or_none",
    "RESULT_SEMANTICS_OK", "RESULT_SEMANTICS_EMPTY_OK",
    "RESULT_SEMANTICS_EMPTY_UNVERIFIED", "RESULT_SEMANTICS_HIDDEN",
    "PUBLISHED_ABSOLUTE", "PUBLISHED_DATE_ONLY", "PUBLISHED_RELATIVE",
    "PUBLISHED_UNPARSED", "PUBLISHED_UNKNOWN",
]
