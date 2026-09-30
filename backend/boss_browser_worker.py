# -*- coding: utf-8 -*-
"""BOSS 详情浏览器 worker：在装有 boss-agent-cli 的解释器内，读取同一浏览器内的完整职位描述；兼容旧卡片调用。

背景（任务书 §4.2 / 任务书 §1 问题 1）：
  finintel 的 ``source_boss.detail()`` 过去传 ``--job-id`` 给 ``boss detail``，
  上游在该参数下走 **httpx 快速通道**（``job_detail(encryptJobId)``），而搜索走浏览器 CDP。
  两条通道拿到的正文不一致：httpx 的 ``jobDetail`` 字段经常为空/不完整，
  于是出现「搜索能用、详情不能用」的通道不一致。

带公开 job-id 时，worker 读取已冻结浏览器中的详情页，校验岗位 ID、落地 URL 与未裁剪描述区。
没有 job-id 的旧调用仍走 **浏览器通道**：用 boss-agent-cli 公开 SDK 的
``BossClient.job_card_browser(security_id, lid)``（源码注释明确「跳过 httpx，直接走浏览器通道」），
读取 ``jobCard.postDescription``。

完整性口径（**不得夸大**）：拿到卡片正文只能证明「这张卡片有文字」，
**不能**证明它就是完整 JD——卡片本身可能只是摘要。因此成功信封里一律带
``_detail_status="partial"``、``_detail_source="boss_browser_card"``，
由 ``source_boss`` / ``upstream_contract.map_detail`` 决定最终展示口径；
空正文才是 ``missing``。

设计（任务书 §3.2 / §3.1 boss_browser_worker.py）：
  - 只通过固定 argv 接收参数，禁止 shell=True，关键词只作为数据。
  - stdout 只输出**一条 JSON 信封**（与 boss CLI 的 {ok,schema_version,command,data,pagination,error,hints} 同构，
    便于 source_boss 复用 parse_envelope）；stderr 只输出脱敏诊断。
  - 风控/认证错误升格为结构化 error，绝不静默回退 httpx 或 headless。

运行方式（由 backend 以 argv 数组 spawn，不走 shell）：
  <python> boss_browser_worker.py --data-dir <dir> --cdp-url <url> \
      --browser-source existing-browser -- security_id lid
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
COMMAND = "detail"

# 与 boss CLI 一致的风控错误码（源码 api/zhipin_errors / api/client.py）
_CODE_ACCOUNT_RISK = 36
_CODE_STOKEN_EXPIRED = 37
# BOSS（zhipin）的「业务成功」判据在 SDK 里只有一处：``BossPlatform.is_success``
# 要求 ``code == 0``。本 worker 用同一条判据，构建不出 platform 时按同语义回退。
_BUSINESS_SUCCESS_CODE = 0
_CARD_ID_FIELD = "encryptJobId"
# 与 ``boss_agent_cli.platforms.zhipin._ERROR_CODE_MAP``（2.0.0 实测为
# ``{9: RATE_LIMITED, 36: ACCOUNT_RISK}``）对齐；只有当 SDK 模块级映射取不到时才用它。
_FALLBACK_CODE_MAP: dict[int, str] = {9: "RATE_LIMITED", 36: "ACCOUNT_RISK"}
# 业务错误里允许「可重试」的少数码；风控一律不可重试。
_RECOVERABLE_BUSINESS_CODES: frozenset[str] = frozenset({
    "RATE_LIMITED", "NETWORK_ERROR", "UPSTREAM_NETWORK",
})
_ACTION_BY_CODE: dict[str, str] = {
    "ACCOUNT_RISK": "停止自动重试；请在 BOSS 官网或 App 确认账号状态后手动重试一次",
    "ENVIRONMENT_RISK": "当前访问环境被平台拦截；保留浏览器并在官方页面检查，不自动重试",
    "AUTH_EXPIRED": "登录态已过期，请在当前生效 data-dir 下重新登录",
    "TOKEN_REFRESH_FAILED": "登录态刷新失败，请在当前生效 data-dir 下重新登录",
    "RATE_LIMITED": "等待一段时间后再手动重试；不要连续重试",
    "NETWORK_ERROR": "检查本机网络后再手动重试",
}
_DEFAULT_BUSINESS_ACTION = "查看上游状态后手动重试"
# 环境风险的**有界**文案补充：只认已经在本机响应里见过的原文片段，不泛化。
_ENVIRONMENT_MARKERS: tuple[str, ...] = (
    "访问环境风控", "访问环境存在异常", "环境存在异常", "IP地址存在异常", "IP 地址存在异常",
)
_TOKEN_MARKERS: tuple[str, ...] = (
    "__zp_stoken__", "stoken", "登录态过期", "登录状态过期", "登录状态已失效",
    "登录已过期", "凭证过期", "认证过期",
)


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _ok(data: dict[str, Any]) -> None:
    _emit({
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "command": COMMAND,
        "data": data,
        "pagination": None,
        "error": None,
        "hints": None,
    })


def _fail(code: str, message: str, *, recoverable: bool = False,
          recovery_action: str = "", details: Any = None) -> None:
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "recoverable": recoverable,
    }
    if recovery_action:
        error["recovery_action"] = recovery_action
    if details is not None:
        error["details"] = details
    _emit({
        "ok": False,
        "schema_version": SCHEMA_VERSION,
        "command": COMMAND,
        "data": None,
        "pagination": None,
        "error": error,
        "hints": None,
    })


def _build_job_from_card(card: dict[str, Any], *, security_id: str) -> dict[str, Any]:
    """把 jobCard 响应映射为与 boss CLI detail 一致的字段结构（commands/detail.py build_job_from_card）。"""
    raw_job_type = card.get("jobType")
    employment_type = ""
    if isinstance(raw_job_type, int):
        # 与 upstream 端 employment_type_from_raw 对齐：0/1 为全职类，其余原样字符串化。
        # 这里只做最保守的映射，避免错误标注。
        employment_type = str(raw_job_type)
    return {
        "job_id": card.get(_CARD_ID_FIELD, ""),
        "title": card.get("jobName", ""),
        "company": card.get("brandName", ""),
        "salary": card.get("salaryDesc", ""),
        "city": card.get("cityName", ""),
        "experience": card.get("experienceName", ""),
        "education": card.get("degreeName", ""),
        "description": card.get("postDescription", ""),
        "address": card.get("address", ""),
        "skills": card.get("jobLabels", []) or [],
        "boss_name": card.get("bossName", ""),
        "boss_title": card.get("bossTitle", ""),
        "boss_active": card.get("activeTimeDesc", "离线"),
        "security_id": security_id,
        "raw_job_type": raw_job_type,
        "employment_type": employment_type,
        "days_per_week": card.get("daysPerWeekDesc", ""),
        "least_month": card.get("leastMonthDesc", ""),
        "pay_type": card.get("payTypeDesc", ""),
        "greeted": card.get("greeted", False),
    }


def _unwrap_card(raw: Any, platform: Any) -> dict[str, Any] | None:
    """从浏览器通道响应里取 jobCard：优先平台公开 unwrap（BOSS → zpData）。

    响应未必是 dict（异常路径下 SDK 可能给出 None / 非对象），此时返回 None，
    由调用方的业务码校验去处理——本 worker 的 stdout 契约是「永远只有一条信封」，
    任何分支都不允许把裸异常抛到 stdout。
    """
    if not isinstance(raw, dict):
        return None
    if platform is not None:
        try:
            platform_data = platform.unwrap_data(raw)
        except Exception:  # noqa: BLE001
            platform_data = None
        if isinstance(platform_data, dict) and isinstance(platform_data.get("jobCard"), dict):
            return platform_data["jobCard"]
    # 回退：常见结构（zpData / data / 顶层）。
    for layer in (raw.get("zpData"), raw.get("data"), raw):
        if isinstance(layer, dict) and isinstance(layer.get("jobCard"), dict):
            return layer["jobCard"]
    return None


def _sdk_error_classes() -> dict[str, type[BaseException]]:
    """取本机 SDK 里**真实存在**的异常类型；取不到就返回空表（走有界文案兜底）。

    之所以运行时取而不是写死名字：worker 在**服务解释器**里跑，.class 类型必须与
    该解释器里的 boss-agent-cli 完全一致，否则 isinstance 判定会整个失效并悄悄
    退化成「什么都不匹配」。
    """
    classes: dict[str, type[BaseException]] = {}
    for module_name, names in (
        ("boss_agent_cli.api.client", ("AuthError", "PlatformRiskError",
                                       "AccountRiskError", "EnvironmentRiskError")),
        ("boss_agent_cli.api.browser_source", ("BrowserSourceUnavailable",
                                               "BrowserSourceUnsupported")),
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001
            continue
        for name in names:
            target = getattr(module, name, None)
            if isinstance(target, type) and issubclass(target, BaseException):
                classes[name] = target
    return classes


def _is_instance(exc: Exception, classes: dict[str, type[BaseException]],
                 name: str) -> bool:
    """``isinstance`` 的安全包装：子类（如 AccountRiskError ⊂ PlatformRiskError）也算命中。"""
    target = classes.get(name)
    return target is not None and isinstance(exc, target)


def _classify_error(exc: Exception) -> tuple[str, bool, str]:
    """把 boss SDK 异常映射为稳定的 finintel 上游错误码（对齐 upstream_envelope.UPSTREAM_ERROR_MAP）。

    判定顺序：**结构化证据优先，文案只作有界补充**（任务书 F4）。

    1. SDK 的真实异常类型（含子类）——``EnvironmentRiskError`` / ``AccountRiskError``
       都继承 ``PlatformRiskError``，各自还带类属性 ``code``；
    2. 异常自带的 ``code``（``BrowserSourceUnavailable`` 把 policy 的失败码挂在异常上）；
    3. 最后才是消息 markers，且只认本机响应里见过的原文片段。

    两条**不得**犯的错：
      - 不得把任何 ``code 37`` 一律判成环境风险：37 在 SDK 里还可能是 token 刷新失败，
        二者由 ``classify_code_37`` 的证据裁决（见 ``_classify_raw_error``）；
      - 不得把明确的环境异常解释成单纯 token 过期：环境 markers 在 token markers
        **之前**判定。
    """
    classes = _sdk_error_classes()
    if any(_is_instance(exc, classes, name)
           for name in ("BrowserSourceUnavailable", "BrowserSourceUnsupported")):
        # 连不上用户浏览器 / 浏览器里没有可用会话：归因到「通道连接」，不归因到「未登录」。
        # 结构化码优先：policy 的 failure_code 就是 CDP_UNAVAILABLE /
        # BROWSER_SESSION_NOT_FOUND，由上游 envelope 层映射成 OWN_BROWSER_*。
        structured = str(getattr(exc, "code", "") or "").strip()
        code = structured if structured in {"CDP_UNAVAILABLE", "BROWSER_SESSION_NOT_FOUND"} \
            else "BROWSER_SESSION_NOT_FOUND"
        return (code, True,
                "没有连上目标浏览器的采集会话：确认浏览器已以可连接的方式启动且 BOSS 直聘页面已打开后重试采集")
    if _is_instance(exc, classes, "EnvironmentRiskError"):
        return "ENVIRONMENT_RISK", False, _ACTION_BY_CODE["ENVIRONMENT_RISK"]
    if _is_instance(exc, classes, "AccountRiskError"):
        return "ACCOUNT_RISK", False, _ACTION_BY_CODE["ACCOUNT_RISK"]
    if _is_instance(exc, classes, "PlatformRiskError"):
        # 已知子类都已判过；这里是**未被识别的风控子类**。必须先保证「不自动重试、
        # 且落到风控家族」（与 _state_for_error 的 risk 口径一致），再谈细分：
        # 因此用 STOPPED_BY_SAFETY（意为“被平台安全策略停止”，子类细分未知），
        # 并把真实类型与原始消息放进 details，绝不冒充某个具体风控码。
        return "STOPPED_BY_SAFETY", False, _ACTION_BY_CODE["ACCOUNT_RISK"]
    if _is_instance(exc, classes, "AuthError"):
        return "AUTH_REQUIRED", False, "请在当前生效 data-dir 下完成人工登录后重试"
    return _classify_error_by_message(exc)


def _classify_error_by_message(exc: Exception) -> tuple[str, bool, str]:
    """文案兜底（layer 3）：只在拿不到结构化类型时用，且 marker 集合刻意很小。"""
    name = type(exc).__name__
    msg = str(exc)
    lowered = msg.lower()
    if name in ("BrowserSourceUnavailable", "BrowserSourceUnsupported") or \
            "未发现可复用的现有浏览器会话" in msg or "cdp" in lowered and "不可" in msg:
        return ("BROWSER_SESSION_NOT_FOUND", True,
                "没有连上目标浏览器的采集会话：确认浏览器已以可连接的方式启动且 BOSS 直聘页面已打开后重试采集")
    if any(marker in msg for marker in _ENVIRONMENT_MARKERS):
        return "ENVIRONMENT_RISK", False, _ACTION_BY_CODE["ENVIRONMENT_RISK"]
    if f"code {_CODE_ACCOUNT_RISK}" in msg or "账户存在异常" in msg:
        return "ACCOUNT_RISK", False, _ACTION_BY_CODE["ACCOUNT_RISK"]
    if any(marker in lowered for marker in _TOKEN_MARKERS):
        return "AUTH_EXPIRED", False, _ACTION_BY_CODE["AUTH_EXPIRED"]
    if "timeout" in lowered:
        return "UPSTREAM_NETWORK", True, "检查本机网络后再手动重试"
    return "UPSTREAM_PROCESS_FAILED", True, "运行 doctor/status 查看上游状态后手动重试"


def _build_platform(client: Any) -> tuple[Any, str]:
    """构造做业务码校验用的 ``BossPlatform``。

    返回 ``(platform, reason)``。构造失败**不代表可以跳过业务码校验**——调用方必须
    拿 ``reason`` 去做诊断，并用同源回退判定继续校验。
    """
    try:
        from boss_agent_cli.platforms.zhipin import BossPlatform

        return BossPlatform(client), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def _is_business_success(raw: Any, platform: Any) -> bool:
    """业务是否成功。``platform`` 构建失败时按**同一语义**（BOSS: ``code == 0``）回退。"""
    if platform is not None:
        try:
            return bool(platform.is_success(raw))
        except Exception:  # noqa: BLE001
            pass
    return isinstance(raw, dict) and raw.get("code") == _BUSINESS_SUCCESS_CODE


def _classify_raw_error(raw: Any) -> tuple[str, str]:
    """没有 ``BossPlatform`` 实例时，仍按 **SDK 同源判据**给业务响应分类。

    优先复用 SDK 的**模块级**函数（``response_message`` / ``classify_code_37`` /
    ``_ERROR_CODE_MAP``——它们只需要响应体，不需要实例）；拿不到时才用本模块的同义兜底。

    ``code 37`` 的处理口径与 SDK 一致且**不扩大**：evidence 指向 token 才算
    ``TOKEN_REFRESH_FAILED``，其余保守归为 ``ENVIRONMENT_RISK``；连分类器都取不到时
    如实返回 ``UNKNOWN``（上游 ``refine_risk_code`` 仍可用同一份消息证据归一），
    绝不凭 code 数值脑补。
    """
    if not isinstance(raw, dict):
        # 非对象响应（None / str / list）：既无法读 code 也无从谈 37，如实未知。
        return "UNKNOWN", "职位详情获取失败（上游返回的不是对象响应）"
    message = str(raw.get("message") or raw.get("msg") or raw.get("error") or "")
    code_map: dict[int, str] = dict(_FALLBACK_CODE_MAP)
    classify_code_37: Any = None
    try:
        from boss_agent_cli.platforms import zhipin as zhipin_module

        message = str(zhipin_module.response_message(raw) or message)
        classify_code_37 = getattr(zhipin_module, "classify_code_37", None)
        sdk_map = getattr(zhipin_module, "_ERROR_CODE_MAP", None)
        if isinstance(sdk_map, dict) and sdk_map:
            code_map = dict(sdk_map)
    except Exception:  # noqa: BLE001
        pass
    code = raw.get("code")
    if isinstance(code, int) and code in code_map:
        return code_map[code], message or "职位详情获取失败"
    if code == _CODE_STOKEN_EXPIRED:
        if callable(classify_code_37):
            try:
                return ("TOKEN_REFRESH_FAILED" if classify_code_37(raw) == "token_expired"
                        else "ENVIRONMENT_RISK"), message or "职位详情获取失败"
            except Exception:  # noqa: BLE001
                pass
        return "UNKNOWN", message or "职位详情获取失败"
    if isinstance(code, int) and 500 <= code < 600:
        return "NETWORK_ERROR", message or "职位详情获取失败"
    return "UNKNOWN", message or "职位详情获取失败"


def _business_error(raw: Any, platform: Any) -> tuple[str, str]:
    """取业务失败码；``platform`` 不可用时退回 ``_classify_raw_error``。"""
    if platform is not None and isinstance(raw, dict):
        try:
            code, message = platform.parse_error(raw)
            return str(code or "UNKNOWN"), str(message or "")
        except Exception:  # noqa: BLE001
            pass
    return _classify_raw_error(raw)


def main() -> int:
    parser = argparse.ArgumentParser(description="BOSS 详情浏览器 worker")
    parser.add_argument("--data-dir", required=True, help="boss-agent-cli 的 data_dir")
    parser.add_argument("--cdp-url", default=None, help="CDP 调试端点，如 http://127.0.0.1:9222")
    parser.add_argument("--browser-source", default="existing-browser",
                        choices=["existing-browser"],
                        help="浏览器采集来源")
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--job-id", default=None, help="公开岗位 ID；提供时读取已渲染的完整详情页")
    parser.add_argument("security_id", help="搜索结果里的 securityId")
    parser.add_argument("lid", nargs="?", default="", help="搜索结果里的 lid（可选）")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).expanduser()
    if not data_dir.is_dir():
        _fail("DATA_DIR_MISMATCH", f"data-dir 不存在：{data_dir}",
              recovery_action="确认登录时使用的 data-dir 与采集时一致")
        return 1

    started = time.time()
    if args.job_id:
        from boss_rendered_detail import read_rendered_job, RenderedDetailError
        try:
            data = read_rendered_job(args.cdp_url, args.job_id, args.timeout_seconds)
            data["_elapsed_ms"] = int((time.time()-started)*1000)
            _ok(data)
            return 0
        except RenderedDetailError as exc:
            _fail(exc.code, str(exc), recovery_action="保留当前浏览器；处理平台验证后手动重试")
            return 1
        except Exception as exc:
            _fail("NETWORK_ERROR", "完整详情读取未完成："+type(exc).__name__,
                  recoverable=True, recovery_action="检查现有浏览器连接后手动重试")
            return 1
    try:
        from boss_agent_cli.auth.manager import AuthManager
        from boss_agent_cli.api.client import BossClient, AuthError, PlatformRiskError
    except Exception as exc:  # noqa: BLE001
        _fail("DEPENDENCY_MISSING",
              f"当前解释器无法加载 boss-agent-cli SDK：{type(exc).__name__}",
              recovery_action="在装有 boss-agent-cli==2.0.0 的解释器内运行本 worker")
        return 1

    raw: Any = None
    card: dict[str, Any] | None = None
    platform: Any = None
    platform_error = ""
    try:
        auth = AuthManager(data_dir, platform="zhipin")
        with BossClient(
            auth,
            cdp_url=args.cdp_url,
            browser_source=args.browser_source,
        ) as client:
            # 强制浏览器通道读职位卡片（不走 httpx），取 JD 正文。
            raw = client.job_card_browser(args.security_id, args.lid)
            # 业务码校验对象要在 client 存活时构造（持有 client 引用）。
            # 构造失败**不等于**可以不做校验 —— platform_error 会进 details 诊断。
            platform, platform_error = _build_platform(client)
            card = _unwrap_card(raw, platform)
    except (AuthError, PlatformRiskError) as exc:
        code, recoverable, action = _classify_error(exc)
        _fail(code, str(exc) or "职位详情获取失败", recoverable=recoverable,
              recovery_action=action)
        return 1
    except Exception as exc:  # noqa: BLE001
        code, recoverable, action = _classify_error(exc)
        _fail(code, f"浏览器通道读取详情失败：{type(exc).__name__}: {exc}",
              recoverable=recoverable, recovery_action=action)
        return 1

    # ---- 业务码校验：无论 platform 是否构建成功都要走这一步（任务书 F3） ----
    # 老实现写成 ``if platform is not None and not platform.is_success(raw)``：
    # BossPlatform 一旦构建失败（依赖漂移 / 构造签名变化）就直接跳过校验，
    # 平台返回的非成功响应会被当成成功继续往下走。
    if not _is_business_success(raw, platform):
        upstream_code, upstream_message = _business_error(raw, platform)
        details: dict[str, Any] = {"business_code": raw.get("code") if isinstance(raw, dict) else None}
        if platform is None:
            details["platform_probe"] = platform_error or "BossPlatform 构建失败"
            details["validation"] = "fallback_code_zero"
        _fail(upstream_code, upstream_message or "职位详情获取失败",
              recoverable=upstream_code in _RECOVERABLE_BUSINESS_CODES,
              recovery_action=_ACTION_BY_CODE.get(upstream_code, _DEFAULT_BUSINESS_ACTION),
              details=details)
        return 1

    if not card:
        # 没有任何可读卡片：可能是岗位下架或通道返回空。
        _fail("JOB_NOT_FOUND", "职位不存在或已下架，或浏览器通道未返回卡片",
              recovery_action="跳过该岗位并继续处理其他岗位")
        return 1

    # ---- 岗位身份（F5）：ID 只能来自 jobCard.encryptJobId，没有别名兜底 ----
    # 拿不到 ID 就无法证明这张卡片属于请求的目标岗位，因此**不产出成功信封**，
    # 由调用方拒绝保存，而不是塞一个空 ID 让下游误存。
    if not str(card.get(_CARD_ID_FIELD) or "").strip():
        _fail("CARD_JOB_ID_MISSING",
              f"职位卡片缺少 {_CARD_ID_FIELD}，无法确认它与请求的目标岗位一致",
              recovery_action="核对 boss-agent-cli 返回的 jobCard 字段；"
                              "不同卡片版本若换了 ID 字段名，先同步契约再放行",
              details={"card_keys": sorted(card.keys())[:20],
                       "identity_field": _CARD_ID_FIELD})
        return 1

    data = _build_job_from_card(card, security_id=args.security_id)
    description = data.get("description") or ""
    if not description.strip():
        # 卡片存在但正文为空：如实标注，不冒充完整 JD。
        data["_detail_status"] = "missing"
        data["_detail_source"] = "boss_browser_card"
        data["_description_incomplete"] = True
    else:
        # 卡片正文：有文字不等于完整 JD，标记为卡片来源的 partial，由 source_boss 决定最终 status。
        data["_detail_status"] = "partial"
        data["_detail_source"] = "boss_browser_card"
        data["_description_incomplete"] = False

    data["_elapsed_ms"] = int((time.time() - started) * 1000)
    _ok(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
