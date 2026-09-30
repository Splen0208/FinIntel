# -*- coding: utf-8 -*-
"""社交链路统一脱敏入口（指令 D3 / D4，增量设计 T14）。

为什么必须有这一层
------------------
社交快照是**不可变证据**：一旦落库，任何 ``quote`` 的 ``start``/``end`` 偏移都以
落库文本为准（见 ``social_store.insert_snapshot`` / ``insert_comment_snapshot``）。
因此脱敏必须发生在**写库之前**，且顺序固定：

    先脱敏 → 再算 content_hash / identity_hash → 再算 quote 的 start / end

顺序一旦颠倒（先算 hash/偏移再脱敏），证据链就与保存的文本不再自洽——这正是本模块
存在的理由。调用方只要把文本交给本模块，再把**返回值**放进 ``social_store``，
顺序自然成立（``social_store`` 自己也兜一道，保证任何入口都不会漏）。

铁律
----
1. **脱敏 ≠ 删除原文证据**：只把联系方式与凭据换成占位符，正文其余部分逐字保留，
   所以「引文能在原文里定位」这条不变量在脱敏后依然成立。
2. **幂等**：``redact(redact(x)) == redact(x)``，重复脱敏绝不产生二次改写
   （这也保证了「同一段文本两次入库得到同一 hash」）。
3. **绝不回显**：任何异常 / 日志 / 诊断都不得包含被拒绝的原参数值（见 :func:`safe_token`）。
4. **作者键匿名化**：明文展示名一律换成稳定匿名键 ``anon_<hex16>``；口径的**唯一来源**
   是 ``collectors.contracts.stable_anon_key``，本模块只做转发，绝不重写一套。
5. **旧数据只做读取边界与报告**（:func:`public_url` / :func:`audit_legacy_sensitive_urls`），
   绝不批量改写历史数据——改数据需要用户单独授权。

覆盖范围
--------
- 凭据：``auth_token`` / ``ct0`` / ``stoken`` / ``wt2`` / ``token`` / ``secret`` /
  ``cookie`` / ``set-cookie`` / ``authorization`` / ``user_agent`` / 裸 ``Bearer`` /
  32 位以上十六进制 token / 通用 ``key=value`` 密钥形态；
- 社交侧新增形状：``xsec_*`` 查询参数（``xsec_token`` 等）、微信号、邮箱；
- 联系方式：手机号（由既有实现统一处理）。

执行顺序（每一步都幂等）
------------------------
1. **本模块先行**处理「键=值」凭据对：值终止符放宽到中英文标点与括号，
   避免上游 ``[^\\s,;&\"']+`` 把「逗号/句号之后的正文」当成凭据值一起吃掉
   （那等于删除证据，违反"脱敏 ≠ 删除原文"铁律）；
2. **复用**既有实现（``collectors.runtime.redact`` → ``upstream_config.redact``）
   处理 cookie / authorization / bearer / 长 hex / 手机号等，避免出现两套互相打架的规则；
3. 社交侧新增形状（``xsec_*`` / 微信号 / 邮箱）兜底。

第 1 步必须在上游之前：否则上游的 ``ct0=`` / ``auth_token=`` 规则会先吞掉正文尾部。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from collectors.contracts import stable_anon_key as _contracts_stable_anon_key
from collectors.runtime import redact as _runtime_redact

__all__ = [
    "SECRET_PLACEHOLDER",
    "EMAIL_PLACEHOLDER",
    "WECHAT_PLACEHOLDER",
    "REDACTION_RULES",
    "redact",
    "redact_text",
    "redact_optional",
    "redact_exception",
    "contains_secret",
    "stable_anon_key",
    "public_url",
    "safe_token",
    "audit_legacy_sensitive_urls",
]

SECRET_PLACEHOLDER = "[REDACTED]"
EMAIL_PLACEHOLDER = "[REDACTED_EMAIL]"
WECHAT_PLACEHOLDER = "[REDACTED_WECHAT]"

# 允许对外展示的公开域名（``public_url`` 的白名单）。非白名单主机一律返回 None：
# 宁可不给链接，也不给一个可能带 token / 跳转参数的 URL。
PUBLIC_URL_HOSTS = (
    "x.com", "www.x.com", "twitter.com", "www.twitter.com",
    "xiaohongshu.com", "www.xiaohongshu.com",
)

# 社交侧新增形状。每条规则都必须**幂等**：
# - ``xsec_*`` 用「键 + 分隔符」为组，替换回同一键名 → 二次运行时结果不变；
# - 微信号要求 id 以字母开头且长度 >= 5，避免把「威信: 高」这类正常中文误伤；
# - 邮箱整体替换成占位符，不保留任何可反推的片段。
_XSEC_RE = re.compile(r"(?i)\b(xsec_[a-z0-9_]+)(\s*[:=]\s*)[^\s,;&\"'#]+")
_WECHAT_LABEL_RE = re.compile(
    r"(?i)(微信|微信号|weixin|wechat|wx|vx)\s*[:：=]\s*([A-Za-z][A-Za-z0-9_\-.]{4,})")
_WXID_RE = re.compile(r"(?i)\bwxid_[0-9a-z]+")
_EMAIL_RE = re.compile(r"(?i)[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# 长数字串（QQ / 手机号兜底）：只用于「已是联系方式形状」的独立数字串，避免误伤指标数值。
_CONTACT_DIGITS_RE = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)")

# **必须最先执行**的「键=值」凭据对规则（含已脱敏形态，保证幂等）。
#
# 为什么需要它（这是一处**证据保全**修复）：
# 既有实现 ``collectors.runtime.redact`` 对 ``ct0`` / ``auth_token`` / ``xsec_token``
# 的值用的是 ``[^\s,;&\"']+``，**半角逗号才终止**。中文正文只要写成
# ``ct0=XXX，后续结论如下。``，后面的整句就会被当成凭据值一起吃掉——那是**删除证据**，
# 违反「脱敏 ≠ 删除原文」铁律。上游实现属冻结区，不能改；于是本模块**先**把这类
# 「键=值」整体换成占位符（值终止符放宽到中英文标点与括号），让上游规则无键可匹配。
#
# - 同时匹配**已脱敏**的值占位符 ``[REDACTED*]`` → 二次运行仍把键去掉，幂等且能修复
#   旧代码留下的 ``ct0=[REDACTED]，正文``（否则上游仍会吃掉逗号后的正文）。
# - 只收录「值有明确分隔符」的键；``cookie`` / ``authorization`` / ``set-cookie`` /
#   ``user_agent`` 的值本身不透明、**应当**整行吃到行尾，仍交给上游原规则处理。
_KEYED_SECRET_RE = re.compile(
    r"(?i)\b(xsec_[a-z0-9_]+|auth_token|ct0|stoken|wt2|api_key|token|secret)"
    r"([\"\s]*[:=][\"\s]*)"
    r"(?:\[REDACTED(?:_EMAIL|_WECHAT)?\]"
    r"|[^\s,;&\"'，。、；：！？（）()\[\]【】]+)"
)

# 「已经脱敏过」的占位符守卫：`key=[REDACTED]` 这种**已处理**形态不算敏感形状，
# 否则 ``contains_secret`` 会在脱敏结果上误报（把占位符当成了值）。
_PLACEHOLDER_GUARD = r"(?!\[REDACTED)"

# 供 ``contains_secret`` 做「是否还有敏感形状」的探测（不回显命中内容）。
_PROBE_PATTERNS = (
    re.compile(rf"(?i)\bxsec_[a-z0-9_]+\s*[:=]\s*{_PLACEHOLDER_GUARD}[^\s,;&\"'#]+"),
    re.compile(rf"(?i)\b(auth_token|ct0|stoken|wt2|cookie|authorization)\s*[:=]\s*"
               rf"{_PLACEHOLDER_GUARD}\S+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\bwxid_[0-9a-z]+"),
    re.compile(rf"(?i)(微信|weixin|wechat|vx|wx)\s*[:：=]\s*{_PLACEHOLDER_GUARD}"
               r"[A-Za-z][A-Za-z0-9_\-.]{4,}"),
    _EMAIL_RE,
    _CONTACT_DIGITS_RE,
)

REDACTION_RULES = (
    "键=值 凭据对（auth_token/ct0/stoken/wt2/token/secret/api_key/xsec_*）→ [REDACTED]"
    "（值终止符含中英文标点，正文不受牵连）",
    "凭据（cookie/authorization/user_agent/bearer/长 hex）→ [REDACTED]",
    "微信号 / wxid_* → [REDACTED] / [REDACTED_WECHAT]",
    "邮箱 → [REDACTED_EMAIL]",
    "手机号 → 1**********（由 upstream_config.redact 统一处理）",
)


# ---------------------------------------------------------------------------
# 文本脱敏
# ---------------------------------------------------------------------------

def redact(text: Any) -> Any:
    """把文本里的凭据与联系方式脱敏；``None`` 原样返回（便于直接用于可选字段）。

    顺序固定、且每一步都幂等：

    1. **本模块先行**：把「键=值」凭据对整体换成 ``[REDACTED]``（值终止符含中英文标点），
       这一步保住"逗号/句号之后的正文"不被上游规则误吞（脱敏 ≠ 删除证据）；
    2. 既有实现（cookie / set-cookie / authorization / x-auth* / user_agent /
       bearer / 长十六进制 token / 手机号）；
    3. 社交侧新增形状：``xsec_*``、微信号、邮箱（兜底，通常第 1 步已清掉）。

    幂等：``redact(redact(x)) == redact(x)``。
    """
    if text is None:
        return None
    raw = str(text)
    if not raw:
        return raw
    # 1) 先行：键=值 凭据对（含已脱敏形态），值终止符放宽到中英文标点。
    out = _KEYED_SECRET_RE.sub(SECRET_PLACEHOLDER, raw)
    # 2) 既有实现（cookie / set-cookie / authorization / user_agent / bearer /
    #    长十六进制 token / 手机号）。
    out = _runtime_redact(out)
    # 3) 社交侧新增形状兜底。
    out = _XSEC_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{SECRET_PLACEHOLDER}", out)
    out = _WECHAT_LABEL_RE.sub(lambda m: f"{m.group(1)}：{SECRET_PLACEHOLDER}", out)
    out = _WXID_RE.sub(WECHAT_PLACEHOLDER, out)
    out = _EMAIL_RE.sub(EMAIL_PLACEHOLDER, out)
    return out


def redact_text(text: Optional[str]) -> Optional[str]:
    """``redact`` 的显式别名（语义更清楚：输入/输出都是文本或 None）。"""
    return redact(text)


def redact_optional(text: Optional[str]) -> Optional[str]:
    """可选字段专用入口（与 :func:`redact` 等价，仅用于表意）。"""
    return redact(text)


def redact_exception(exc: BaseException, *, limit: int = 400) -> str:
    """把异常压成一行脱敏诊断文本（错误消息本身也可能带凭据）。"""
    text = redact(f"{type(exc).__name__}: {exc}") or ""
    return str(text)[: max(1, int(limit))]


def contains_secret(text: Optional[str]) -> bool:
    """文本里是否**仍有**敏感形状（只回布尔值，绝不回显命中内容）。

    用于 QA 断言「落库文本 / 日志里不再有敏感形状」。**已脱敏的占位符不算命中**
    （``key=[REDACTED]`` 是处理结果，不是残留凭据），因此对 ``redact(x)`` 的输出
    本函数必须返回 ``False``——这也是一条可断言的不变量。
    """
    if not text:
        return False
    raw = str(text)
    return any(pattern.search(raw) for pattern in _PROBE_PATTERNS)


def safe_token(value: Any) -> str:
    """校验会被拼进 argv 的参数：拒绝空值 / 选项前缀 / 控制字符（防注入）。

    错误消息**绝不包含原参数值**：调用方传进来的很可能就是凭据本身，
    回显即泄漏（T14 明确要求）。
    """
    text = "" if value is None else str(value)
    if not text:
        raise ValueError("参数不合法：不允许空值")
    if text.startswith("-"):
        raise ValueError("参数不合法：不允许以选项前缀 '-' 开头的值")
    if re.search(r"[\r\n\x00]", text):
        raise ValueError("参数不合法：值中包含控制字符")
    return text


# ---------------------------------------------------------------------------
# 作者键匿名化 / 公开 URL（读取边界）
# ---------------------------------------------------------------------------

def stable_anon_key(platform: str, raw: Optional[str]) -> Optional[str]:
    """作者键匿名化：``anon_<hex16>``，同一 ``(platform, raw)`` 永远同一键。

    口径唯一来源是 ``collectors.contracts.stable_anon_key``（本模块只转发），
    避免出现第二套算法导致「同一作者两个键」。
    """
    return _contracts_stable_anon_key(platform, raw)


def public_url(url: Optional[str], *, fallback: Optional[str] = None) -> Optional[str]:
    """对外可展示的公开 URL：只保留 ``scheme://host/path``，**丢弃查询串与锚点**。

    - 非 https / 非白名单主机 → 返回 ``fallback``（默认 ``None``）：
      读取边界上宁可不给链接，也不给带 token 的跳转 URL（D4）。
    - 幂等，且不修改入参。
    """
    if not url:
        return fallback
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(str(url))
    if parsed.scheme != "https" or parsed.hostname not in PUBLIC_URL_HOSTS:
        return fallback
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


# ---------------------------------------------------------------------------
# D4：旧数据只读审计（只报条数与主键，**绝不回显疑似凭据的值**）
# ---------------------------------------------------------------------------

def audit_legacy_sensitive_urls(limit: int = 500) -> dict[str, Any]:
    """历史数据审计（**只读、dry-run**）：找出带查询串/疑似 token 的 URL 与文本。

    返回结构与语义：

    - ``items_with_query``：``social_item.canonical_url`` 仍带查询串的条数（应清理；
      本函数不清理，只报告主键，改写需用户单独授权）；
    - ``snapshots_with_secret_shape`` / ``comment_snapshots_with_secret_shape``：
      旧快照文本里仍有敏感形状的条数（T14 上线**前**写入的行）；
    - ``locators``：``social_locator.locator_value`` 条数（**设计如此**：locator 就是
      为承载带 token 的定位串而生，禁止出任何对外响应）。

    绝不返回任何 URL/文本片段本身，避免审计报告本身变成泄漏源。
    """
    import store

    def _count(sql: str, params: tuple = ()) -> int:
        row = store.query_one(sql, params)
        return int(row["n"]) if row else 0

    query_like = ("canonical_url LIKE '%?%' OR canonical_url LIKE '%xsec_%' "
                  "OR canonical_url LIKE '%token=%'")
    items_with_query = _count(
        f"SELECT COUNT(*) AS n FROM social_item WHERE canonical_url IS NOT NULL AND ({query_like})")
    item_ids = [r["id"] for r in store.query_all(
        f"SELECT id FROM social_item WHERE canonical_url IS NOT NULL AND ({query_like}) "
        "ORDER BY id LIMIT ?", (max(1, int(limit)),))]
    secret_like = ("evidence_text LIKE '%xsec_%' OR evidence_text LIKE '%auth_token=%' "
                   "OR evidence_text LIKE '%ct0=%' OR evidence_text LIKE '%stoken=%' "
                   "OR evidence_text LIKE '%wt2=%'")
    snap_ids = [r["id"] for r in store.query_all(
        f"SELECT id FROM social_snapshot WHERE {secret_like} ORDER BY id LIMIT ?",
        (max(1, int(limit)),))]
    comment_ids = [r["id"] for r in store.query_all(
        f"SELECT id FROM social_comment_snapshot WHERE {secret_like} ORDER BY id LIMIT ?",
        (max(1, int(limit)),))]
    locator_count = _count("SELECT COUNT(*) AS n FROM social_locator")
    return {
        "dry_run": True,
        "rewritten": 0,
        "items_with_query": items_with_query,
        "item_ids": item_ids,
        "snapshots_with_secret_shape": len(snap_ids),
        "snapshot_ids": snap_ids,
        "comment_snapshots_with_secret_shape": len(comment_ids),
        "comment_snapshot_ids": comment_ids,
        "locators": locator_count,
        "note": ("只读审计：本函数不写库。历史数据清理需要用户单独授权后另行执行；"
                 "locator 值仅采集路径内部使用，禁止进入任何对外响应。"),
    }
