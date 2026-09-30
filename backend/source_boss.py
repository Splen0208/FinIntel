"""Read-only BOSS adapter backed by boss-agent-cli 2.0.0 subprocess calls."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from upstream_config import (
    DataDirResolution,
    RawInvocation,
    assert_upstream_dir,
    redact,
    resolve_cli_path,
    run_upstream,
    run_worker,
    sanitized_child_env,
)
from upstream_contract import map_detail, map_search_card
from upstream_envelope import FailureLayer, UpstreamEnvelope, UpstreamError, parse_envelope, require_success

STABLE_DETAIL_ID = True

# 通道冻结哨兵：search/detail 的 top_level_options **必须显式传入**，
# 缺省即抛契约错——堵死「漏传 → 回落全局设置 → 同一任务中途偷换通道」的口子。
_REQUIRED_TOP_LEVEL_OPTIONS = object()


def _trace(invocation: RawInvocation, envelope: UpstreamEnvelope) -> dict[str, Any]:
    return {
        "command": envelope.command,
        "argv_redacted": invocation.argv_redacted,
        "data_dir": invocation.data_dir,
        "elapsed_ms": invocation.elapsed_ms,
        "returncode": invocation.returncode,
        "layer": envelope.layer,
        "ok": envelope.ok,
        "error_code": envelope.error["code"] if envelope.error else None,
    }


def _browser_options(explicit: object) -> dict[str, str]:
    """确定本次调用的采集通道顶层选项。

    - 采集命令（search / detail）：``top_level_options`` **必传**，缺省即抛契约错；
      传 ``{}`` 表示本次显式不走该通道。二者都只能来自任务冻结的通道快照。
    - 预检命令（doctor）：允许不传（``None`` → ``{}``），因为 doctor 不接受
      ``--browser-source``，只有 ``stored-cookie`` 需要显式 ``--cdp-url``。
    """
    if explicit is _REQUIRED_TOP_LEVEL_OPTIONS:
        raise UpstreamError(
            "CHANNEL_NOT_FROZEN",
            "采集命令必须显式传入 top_level_options（任务内通道已冻结，不得回落全局设置）",
            False,
            "由 runs 从 config_snapshot.channel 取出冻结通道后显式传入",
            layer=FailureLayer.CONTRACT,
        )
    if explicit is None:
        return {}
    return dict(explicit)  # type: ignore[arg-type]


async def _call(
    cli_path: str,
    command: str,
    *,
    data_dir: Path,
    positional: tuple[str, ...] = (),
    options: Optional[dict[str, str | int | bool | None]] = None,
    top_level_options: Optional[Mapping[str, str]] = None,
    timeout_seconds: int = 120,
) -> tuple[RawInvocation, UpstreamEnvelope]:
    invocation = await run_upstream(
        cli_path,
        command,
        data_dir=data_dir,
        positional=positional,
        options=options,
        top_level_options=top_level_options or {},
        timeout_seconds=timeout_seconds,
    )
    envelope = parse_envelope(invocation)
    try:
        require_success(envelope, command=command)
    except UpstreamError as exc:
        if not exc.evidence:
            exc.evidence = _trace(invocation, envelope)
        raise
    return invocation, envelope


async def ping(
    cli_path: str,
    data_dir: Path | DataDirResolution,
    timeout_seconds: int = 60,
    top_level_options: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Run the local doctor probe and verify the effective upstream directory.

    注意：上游 ``doctor`` **不接受** ``--browser-source``（源码确认，只读 cdp_url），
    所以这里只允许传 ``--cdp-url``（``stored-cookie`` 探测指定端口时用）。
    """
    expected = data_dir.path if isinstance(data_dir, DataDirResolution) else data_dir
    invocation, envelope = await _call(
        cli_path,
        "doctor",
        data_dir=expected,
        top_level_options=_browser_options(top_level_options),
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(envelope.data, dict):
        from upstream_contract import contract_error

        raise contract_error({"data(object)"}, "doctor", [])
    echoed = envelope.data["data_dir"] if "data_dir" in envelope.data else None
    assert_upstream_dir(echoed, expected)
    return {
        "summary": envelope.data["summary"] if "summary" in envelope.data else "doctor 完成",
        "auth_state": envelope.data["auth_state"] if "auth_state" in envelope.data else None,
        "checks": envelope.data["checks"] if "checks" in envelope.data else [],
        "data_dir_echo": echoed,
        "safe_hints": envelope.safe_hints,
        "operator_actions": envelope.operator_actions,
        "trace": _trace(invocation, envelope),
    }


async def auth_state(
    cli_path: str,
    data_dir: Path | DataDirResolution,
    live: bool = False,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Read local authentication health, optionally performing one online check."""
    expected = data_dir.path if isinstance(data_dir, DataDirResolution) else data_dir
    options = {"--live": True} if live else None
    invocation, envelope = await _call(
        cli_path,
        "status",
        data_dir=expected,
        options=options,
        timeout_seconds=timeout_seconds,
    )
    data = envelope.data if isinstance(envelope.data, dict) else {}
    echoed = data["data_dir"] if "data_dir" in data else None
    if echoed:
        assert_upstream_dir(str(echoed), expected)
    return {
        "auth_state": data["auth_state"] if "auth_state" in data else "authenticated",
        "checks": data["checks"] if "checks" in data else [],
        "data_dir_echo": echoed,
        "safe_hints": envelope.safe_hints,
        "operator_actions": envelope.operator_actions,
        "trace": _trace(invocation, envelope),
    }


async def cities(
    cli_path: str,
    data_dir: Path | DataDirResolution,
    timeout_seconds: int = 60,
) -> list[dict[str, Any]]:
    """Return the upstream city catalog from a valid success envelope."""
    expected = data_dir.path if isinstance(data_dir, DataDirResolution) else data_dir
    _invocation, envelope = await _call(
        cli_path,
        "cities",
        data_dir=expected,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(envelope.data, list):
        from upstream_contract import contract_error

        raise contract_error({"data(list)"}, "cities", [])
    return envelope.data


async def search(
    cli_path: str,
    query: str,
    city: Optional[str],
    page: int,
    data_dir: Path | DataDirResolution,
    timeout_seconds: int,
    no_cache: bool = False,
    top_level_options: object = _REQUIRED_TOP_LEVEL_OPTIONS,
) -> dict[str, Any]:
    """Execute one real search and strictly map normalized result cards.

    默认**不**传 ``--no-cache``：让上游用自己的搜索缓存，避免「同一条件反复联网」。
    只有用户显式要求「获取最新结果」时才由调用方传 ``no_cache=True``。

    ``top_level_options`` **必传**：必须是任务冻结的通道（``config_snapshot.channel``），
    不允许留空后回落全局设置——否则同一任务的 search 与 detail 会中途偷换通道。
    """
    expected = data_dir.path if isinstance(data_dir, DataDirResolution) else data_dir
    options: dict[str, str | int | bool | None] = {"--page": page}
    if city:
        options["--city"] = city
    if no_cache:
        options["--no-cache"] = True
    invocation, envelope = await _call(
        cli_path,
        "search",
        data_dir=expected,
        positional=(query,),
        options=options,
        top_level_options=_browser_options(top_level_options),
        timeout_seconds=timeout_seconds,
    )
    cards = [map_search_card(item) for item in envelope.data]
    pagination = envelope.pagination
    if pagination is None:
        from upstream_contract import contract_error

        raise contract_error({"pagination"}, "search", [])
    return {
        "cards": cards,
        "page": int(pagination["page"]),
        "total": int(pagination["total"]),
        "has_more": bool(pagination["has_more"]),
        "empty_result": len(cards) == 0,
        "result_code": "EMPTY_RESULT" if not cards else "OK",
        "contract_violations": [],
        "elapsed_ms": invocation.elapsed_ms,
        "argv_redacted": invocation.argv_redacted,
        "trace": _trace(invocation, envelope),
    }


async def detail(
    cli_path: str,
    security_id: str,
    job_id: Optional[str],
    lid: Optional[str],
    data_dir: Path | DataDirResolution,
    timeout_seconds: int,
    top_level_options: object = _REQUIRED_TOP_LEVEL_OPTIONS,
) -> dict[str, Any]:
    """Execute one real detail read through the browser channel (``boss_browser_worker``).

    修复背景（任务书 §1 问题 1 / §4.2）：旧实现传 ``--job-id`` 给 ``boss detail``，
    上游在该参数下走 **httpx 快速通道**（``job_detail(encryptJobId)``），而搜索走浏览器 CDP，
    两条通道拿到的正文不一致——httpx 的 ``jobDetail`` 字段常为空/不完整，造成
    「搜索能用、详情不能用」。

    现在改为调用仓库内的 ``boss_browser_worker.py``，它直接用 boss SDK 的
    ``BossClient.job_card_browser(security_id, lid)`` **强制浏览器通道**（源码注释明确
    「跳过 httpx」），读取 ``jobCard.postDescription`` 作为详情正文。

    完整性口径：这是**浏览器卡片正文**，有文字也**不等于**完整 JD（卡片可能是摘要），
    所以 worker 标注 ``_detail_status="partial"`` / ``_detail_source="boss_browser_card"``；
    map_detail 之后本函数再统一补 ``detail_source="boss_browser_card"``。

    ``top_level_options`` **必传**，且必须与本次搜索用的是同一份冻结通道
    （含 ``browser_source`` / ``cdp_url``）——不允许留空后回落全局设置。
    """
    expected = data_dir.path if isinstance(data_dir, DataDirResolution) else data_dir
    # 冻结通道 → 顶层选项（含 --browser-source / --cdp-url），从中取 worker 所需参数。
    browser_options = _browser_options(top_level_options)
    cdp_url = browser_options.get("--cdp-url")
    browser_source = browser_options.get("--browser-source", "existing-browser")
    if browser_source != "existing-browser" or not cdp_url:
        raise UpstreamError("CHANNEL_NOT_READY", "详情读取需要已冻结的浏览器连接", False,
                            "请绑定可用的专用浏览器后重试")
    # F2：lid 的空值语义在这里一次性说清，绝不能落到 ``_safe_token("")`` 的通用拒绝上。
    #   - ``None`` / 不传：SDK 签名 ``job_card_browser(security_id, lid="")`` 允许缺省，
    #     worker 的 argparse 也是 ``nargs="?"`` → **正确省略**该位置参数；
    #   - 显式空白串：说明调用方手中有一个不可用的 lid（例如上游卡片给了 ``lid: ""``），
    #     这时必须**点名 lid** 报契约错，而不是返回一个讲不清缺什么的通用错误。
    if lid is None:
        positional: tuple[str, ...] = (security_id,)
    else:
        lid_value = str(lid).strip()
        if not lid_value:
            raise UpstreamError(
                "MISSING_REQUIRED_ARGUMENT",
                "detail 缺少可用的 lid：调用方传入了空白 lid（位置参数 lid 不可为空）",
                False,
                "缺失 lid 时请传 None（worker 会省略该位置参数）；"
                "若该岗位本应有列表上下文，核对 job_locator.lid 的写入来源是否把缺失写成了空串",
                layer=FailureLayer.CONTRACT,
                evidence={"field": "lid", "received": "blank"},
            )
        positional = (security_id, lid_value)
    # worker 用**已验证拥有 boss SDK** 的解释器运行（见 ``resolve_worker_python``）。
    python = await _worker_python(cli_path)

    invocation = await run_worker(
        worker_py="boss_browser_worker.py",
        python=python,
        data_dir=expected,
        positional=positional,
        cdp_url=cdp_url,
        browser_source=browser_source,
        rendered_job_id=job_id,
        timeout_seconds=timeout_seconds,
    )
    envelope = parse_envelope(invocation)
    try:
        require_success(envelope, command="detail")
    except UpstreamError as exc:
        if not exc.evidence:
            exc.evidence = _trace(invocation, envelope)
        raise
    # worker 输出与 boss CLI detail 同构，复用 map_detail 映射；再覆盖详情来源标注。
    #
    # F5：job_id 不匹配（或干脆拿不到）必须**拒绝保存**——把别个岗位的正文存到目标
    # 岗位上，比什么都不存更糟。真实字段来源已核对：``commands/detail.py`` 的
    # ``build_job_from_card`` 用 ``card["encryptJobId"]``，``cache/store.py`` 也以
    # ``encryptJobId`` 作为跨搜索稳定的岗位主键。它就是**唯一的** ID 来源，没有别名兜底。
    data = envelope.data if isinstance(envelope.data, dict) else {}
    returned_job_id = str(data.get("job_id") or "").strip()
    target_job_id = "" if job_id is None else str(job_id).strip()
    if not returned_job_id:
        raise UpstreamError(
            "UPSTREAM_CONTRACT_CHANGED",
            "详情返回的职位卡片没有 encryptJobId，无法确认它就是请求的目标岗位，已拒绝保存",
            False,
            "核对 boss-agent-cli 返回的 jobCard 字段；若上游换了 ID 字段名，先同步契约再放行",
            layer=FailureLayer.CONTRACT,
            evidence={"expected_job_id": target_job_id, "returned_job_id": "",
                      "identity_field": "encryptJobId", "reason": "missing"},
        )
    if target_job_id and returned_job_id != target_job_id:
        raise UpstreamError(
            "UPSTREAM_CONTRACT_CHANGED",
            "详情返回的岗位标识（jobCard.encryptJobId）与本次请求的目标岗位不一致，已拒绝保存",
            False,
            "保留本次结果并检查上游返回结构；不要把别个岗位的正文存到目标岗位上",
            layer=FailureLayer.CONTRACT,
            evidence={"expected_job_id": target_job_id, "returned_job_id": returned_job_id,
                      "identity_field": "encryptJobId", "reason": "mismatch"},
        )
    mapped = map_detail(envelope.data, security_id=security_id)
    # 浏览器卡片正文：有文字不等于完整 JD，如实标注 partial + boss_browser_card。
    mapped["detail_source"] = "boss_browser_card"
    mapped["source_url_verified"] = False  # 卡片通道未验证落地页 public job ID
    if data.get("_detail_source") == "boss_rendered_page":
        proof = data.get("_rendered_proof") or {}
        expected_url = f"https://www.zhipin.com/job_detail/{returned_job_id}.html"
        if (proof.get("job_id") != returned_job_id or proof.get("source_url") != expected_url
                or not isinstance(proof.get("section_count"),int) or proof["section_count"] < 1):
            raise UpstreamError("UPSTREAM_CONTRACT_CHANGED", "详情页缺少岗位身份与原文区块证明，已拒绝保存", False,
                                "核对完整详情读取器的返回契约", layer=FailureLayer.CONTRACT)
        mapped["detail_source"] = "boss_rendered_page"
        mapped["source_url_verified"] = True
        if data.get("_detail_status") == "complete" and proof.get("full_section") is True:
            mapped["detail_status"] = "complete"
            mapped["mapping_warning"] = None
        else:
            mapped["detail_status"] = "partial"
            mapped["mapping_warning"] = "详情页原文区块存在折叠或截断，完整性未确认"
    mapped["elapsed_ms"] = invocation.elapsed_ms
    mapped["argv_redacted"] = invocation.argv_redacted
    mapped["trace"] = _trace(invocation, envelope)
    return mapped


# ---------------------------------------------------------------------------
# F1：worker 的解释器必须是**验证过确实拥有 boss SDK 依赖**的那一个
# ---------------------------------------------------------------------------
# 老实现是「在 CLI 同目录找 python → 找不到就回退服务解释器」，两步都不验证：
# docs/env-facts-20260928.md §3 已实测——``boss.exe`` 是 uv tools 环境承载的 shim，
# ``.local/bin`` 下的 ``python3.12.exe`` 只是通用解释器，并没有 boss SDK
# （本轮复核实测：``import boss_agent_cli`` 直接 ModuleNotFoundError）。
# 一旦将来它旁边放上 ``python.exe``，worker 就会被静默交给一个缺依赖的解释器，
# 失败时只能看到 worker 报 DEPENDENCY_MISSING，查不到是哪个解释器的问题。
#
# 现在的规则：候选列表逐个**真跑 import 探测**，通过才锁定；全不过就明确报错，
# 并把每个候选的失败原因放进 evidence（不含任何凭据）。
WORKER_PYTHON_SETTING_KEY = "boss_worker_python"
WORKER_PYTHON_ENV_VAR = "FININTEL_BOSS_WORKER_PYTHON"
# 探测脚本：worker 真正 import 的模块 + 真正调用的方法。只读 import，不发平台请求。
WORKER_IMPORT_PROBE = (
    "import importlib;"
    "mods = ('boss_agent_cli', 'boss_agent_cli.api.client', "
    "'boss_agent_cli.auth.manager', 'boss_agent_cli.platforms.zhipin');"
    "[importlib.import_module(name) for name in mods];"
    "getattr(importlib.import_module('boss_agent_cli.api.client').BossClient, "
    "'job_card_browser');"
    "print(getattr(importlib.import_module('boss_agent_cli'), '__version__', 'unknown'))"
)
WORKER_PROBE_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class WorkerPythonProbe:
    """一次解释器探测的结果（绝不含凭据；``reason`` 已过 ``redact``）。"""

    python: str
    ok: bool
    version: str = ""
    reason: str = ""


@dataclass(frozen=True)
class WorkerPythonResolution:
    """一次「找 worker 解释器」的完整结果；失败时留着全部候选证据。"""

    python: Optional[str]
    attempts: tuple[WorkerPythonProbe, ...]


_WORKER_PYTHON_LOCK = threading.Lock()
_WORKER_PYTHON_CACHE: dict[str, WorkerPythonResolution] = {}


def reset_worker_python_cache() -> None:
    """清空已锁定的 worker 解释器缓存（重装依赖后、以及测试里必须重算时用）。"""
    with _WORKER_PYTHON_LOCK:
        _WORKER_PYTHON_CACHE.clear()


def _setting_value(key: str) -> str:
    import store

    try:
        row = store.query_one("SELECT value FROM app_setting WHERE key=?", (key,))
    except Exception:  # noqa: BLE001
        return ""
    return str(row["value"] or "") if row else ""


def _configured_worker_python(
    setting_getter: Optional[Callable[[str], str]] = None,
) -> Optional[str]:
    """显式指定的 worker 解释器（设置项优先于环境变量）——运维/特殊安装布局的逃生口。"""
    getter = setting_getter or _setting_value
    try:
        configured = (getter(WORKER_PYTHON_SETTING_KEY) or "").strip()
    except Exception:  # noqa: BLE001
        configured = ""
    if not configured:
        configured = (os.environ.get(WORKER_PYTHON_ENV_VAR) or "").strip()
    return configured or None


def _cli_python_candidates(cli_path: str) -> list[str]:
    """CLI 所在环境的解释器候选（shim 同目录 + venv/uv 的 ``Scripts`` 布局）。"""
    cli_dir = Path(cli_path).resolve().parent
    directories = [cli_dir]
    scripts_name = "Scripts" if sys.platform == "win32" else "bin"
    if cli_dir.name.lower() not in {scripts_name.lower(), "bin"}:
        directories.append(cli_dir / scripts_name)
    pattern = "python*.exe" if sys.platform == "win32" else "python*"
    preferred = ("python.exe", "python3.exe") if sys.platform == "win32" else ("python", "python3")
    found: list[str] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        for entry in sorted(directory.glob(pattern)):
            if entry.is_file():
                found.append(str(entry))
    seen: set[str] = set()
    unique: list[str] = []
    for path in found:
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return sorted(unique,
                  key=lambda path: (0 if Path(path).name.lower() in preferred else 1, path))


def worker_python_candidates(
    cli_path: str,
    *,
    setting_getter: Optional[Callable[[str], str]] = None,
) -> list[str]:
    """候选解释器的**唯一**顺序：显式配置 → CLI 同环境 → 服务解释器。

    顺序不是「谁优先命中」，而是「谁最可能是 CLI 真正使用的环境」；每个候选都要
    过 ``probe_worker_python``，只认**验证过**的那一个。
    """
    candidates: list[str] = []
    configured = _configured_worker_python(setting_getter=setting_getter)
    if configured:
        candidates.append(configured)
    candidates.extend(_cli_python_candidates(cli_path))
    candidates.append(sys.executable)
    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        key = os.path.normcase(candidate)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(candidate)
    return ordered


def probe_worker_python(python: str) -> WorkerPythonProbe:
    """在候选解释器里**真跑一次 import**，验证它确实拥有 worker 需要的 SDK。"""
    try:
        completed = subprocess.run(  # noqa: S603 - argv 数组、受控超时、不放宽白名单
            [python, "-c", WORKER_IMPORT_PROBE],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=WORKER_PROBE_TIMEOUT_SECONDS,
            env=sanitized_child_env(),
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return WorkerPythonProbe(
            python, False, reason=f"探测超时（超过 {WORKER_PROBE_TIMEOUT_SECONDS} 秒）")
    except (OSError, ValueError) as exc:
        return WorkerPythonProbe(python, False, reason=f"{type(exc).__name__}: {exc}")
    if completed.returncode == 0:
        lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
        return WorkerPythonProbe(python, True, version=lines[-1] if lines else "unknown")
    detail = redact((completed.stderr or completed.stdout or "").strip())[-300:]
    return WorkerPythonProbe(python, False,
                             reason=detail or f"退出码 {completed.returncode}")


def _worker_python_dependency_error(cli_path: str,
                                    attempts: tuple[WorkerPythonProbe, ...]) -> UpstreamError:
    """没有候选通过验证时的**可诊断**报错：绝不默默回退。"""
    return UpstreamError(
        "DEPENDENCY_MISSING",
        "没有找到装有 boss-agent-cli 的可用解释器，详情读取已拒绝启动（不做任何未验证的回退）",
        False,
        f"在服务所用解释器内安装 boss-agent-cli==2.0.0，或用设置项 "
        f"{WORKER_PYTHON_SETTING_KEY} / 环境变量 {WORKER_PYTHON_ENV_VAR} 显式指定该解释器",
        layer=FailureLayer.TRANSPORT,
        evidence={
            "cli_path": cli_path,
            "attempts": [{"python": item.python, "ok": item.ok,
                          "version": item.version, "reason": item.reason}
                         for item in attempts],
        },
    )


def resolve_worker_python(
    cli_path: str,
    *,
    setting_getter: Optional[Callable[[str], str]] = None,
) -> str:
    """定位并**锁定**实际拥有 boss SDK 依赖的解释器。

    - 逐个候选做 import 探测，**只**认通过验证的那个；
    - 结果按 ``cli_path`` 缓存（首次之后不再付 subprocess 成本）；
    - 全部失败 → 抛 ``DEPENDENCY_MISSING``，evidence 里带每个候选的原因。
    """
    key = os.path.normcase(str(cli_path))
    with _WORKER_PYTHON_LOCK:
        cached = _WORKER_PYTHON_CACHE.get(key)
    if cached is not None:
        if cached.python:
            return cached.python
        raise _worker_python_dependency_error(cli_path, cached.attempts)

    attempts: list[WorkerPythonProbe] = []
    winner: Optional[str] = None
    for candidate in worker_python_candidates(cli_path, setting_getter=setting_getter):
        probe = probe_worker_python(candidate)
        attempts.append(probe)
        if probe.ok:
            winner = candidate
            break
    resolution = WorkerPythonResolution(python=winner, attempts=tuple(attempts))
    with _WORKER_PYTHON_LOCK:
        _WORKER_PYTHON_CACHE[key] = resolution
    if winner is None:
        raise _worker_python_dependency_error(cli_path, resolution.attempts)
    return winner


async def _worker_python(cli_path: str) -> str:
    """异步入口：把 ``resolve_worker_python`` 的 subprocess 探测挪出事件循环。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, resolve_worker_python, cli_path)


__all__ = [
    "UpstreamError",
    "auth_state",
    "cities",
    "detail",
    "ping",
    "probe_worker_python",
    "reset_worker_python_cache",
    "resolve_worker_python",
    "resolve_cli_path",
    "search",
    "worker_python_candidates",
]
