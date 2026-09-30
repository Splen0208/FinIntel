"""Research-run orchestration with strict real/demo source isolation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import ModuleType
from typing import Any, Optional

import analyzer
import channel_state
import company as company_mod
import rate_gate
import schemas
import store
import upstream_cache
import user_view
from store import get_conn, jloads, jdumps, new_id, query_all, query_one, utcnow_iso
from upstream_config import (
    DataDirResolution,
    channel_options_from_snapshot,
    install_command_hint,
    resolve_cli_path,
    resolve_upstream_data_dir,
)
from upstream_contract import SECURITY_ID_TTL_SECONDS
from upstream_envelope import FailureLayer, UpstreamError

# 详情定位符（security_id/lid）只有 ``SECURITY_ID_TTL_SECONDS``（30 分钟）有效。
# 「搜完过一会儿再回来点继续」必然整批过期，而**一次**无缓存搜索就能把整批定位符
# 全部刷新（搜索返回的卡片里有全部 security_id）。所以给**显式恢复**一个一次性、
# 独立计数的小额度：既不阻断正常路径，也绝不放宽 ``max_search_calls`` 的采集语义。
MAX_LOCATOR_REFRESH_CALLS = 1
LOCATOR_REFRESH_GRANT = "locator_refresh_grant"      # 一次性授权标记（只有显式恢复才发）
LOCATOR_REFRESH_COUNTER = "locator_refresh_calls"    # 已发起的刷新搜索次数（纯诊断，成败都算）
# 「已**成功**兑现过一次刷新」的计数：只有它才让一次性额度真正用尽。
# **失败不算**——真机缺陷（run_d7207cd19ee74c4b）：一次刷新搜索撞上上游网络抖动
# （UPSTREAM_NETWORK）就把 ``locator_refresh_calls`` 打满，旧口径据此判「额度用尽」，
# 15 个岗位被永久封死在「重新搜索」上。额度必须**成功才销账**，失败原样保留，
# 用户再点一次「继续」= 新的一次恢复，仍能拿到一次尝试。
LOCATOR_REFRESH_CONSUMED = "locator_refresh_consumed"
# 本地「定位符过期且刷新额度用尽」的专用码。
# **刻意不复用** ``UPSTREAM_RESULT_LIMIT``：那个码对应上游 ``RESULT_LIMIT_REACHED``
# （搜索范围过大），两件事的补救措施完全不同，共用一个码会让文案说谎。
STALE_LOCATOR_CODE = "STALE_LOCATOR"


class RunError(Exception):
    """Stable run-layer error that preserves upstream diagnostic identity."""

    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool,
        operator_action: str,
        *,
        upstream_code: Optional[str] = None,
        layer: Optional[str] = None,
        evidence: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.operator_action = operator_action
        self.upstream_code = upstream_code
        self.layer = layer
        self.evidence = evidence or {}


def _validate_budget(config: dict[str, Any]) -> dict[str, int]:
    limits = {
        key: config.get(key, schemas.DEFAULT_BUDGET[key])
        for key in (
            "max_search_calls",
            "max_detail_calls",
            "call_timeout_seconds",
            "max_transient_retries",
            "details_per_call",
            "analysis_batch_size",
        )
    }
    for key, value in limits.items():
        upper = schemas.BUDGET_UPPER_BOUNDS[key]
        if not isinstance(value, int) or value < 1 or value > upper:
            raise RunError(
                "INVALID_ANALYSIS",
                f"预算 {key}={value} 超出允许范围 1~{upper}",
                False,
                "调整运行配置中的预算值",
            )
    return limits


def _get_setting(key: str) -> Optional[str]:
    row = query_one("SELECT value FROM app_setting WHERE key=?", (key,))
    return row["value"] if row else None


def _source_adapter(source: str) -> ModuleType:
    """Return exactly one adapter; the real branch never imports demo fixtures."""
    if source == "zhipin":
        import source_boss

        return source_boss
    if source == "demo":
        import source_demo

        return source_demo
    raise RunError(
        "INVALID_ANALYSIS",
        f"未知数据源 {source}，仅支持 demo/zhipin",
        False,
        "正常采集请选择 zhipin；演示数据必须主动选择 demo",
    )


def _upstream(channel: Optional[dict[str, Any]] = None) -> tuple[str, DataDirResolution]:
    """Resolve the real adapter's CLI and the single effective data directory.

    传入 ``channel``（任务冻结的通道快照）时**一律使用冻结值**：同一任务的
    search 与 detail 必须共用同一份 ``(data_dir, cli_path)``，中途改全局设置
    不得偷换。未传入时才按当前设置解析（创建任务 / 环境预检）。
    """
    if channel and channel.get("cli_path") and channel.get("data_dir"):
        from pathlib import Path

        return str(channel["cli_path"]), DataDirResolution(
            path=Path(str(channel["data_dir"])), source="frozen",
            raw=str(channel["data_dir"]), created=False,
        )
    cli_path = resolve_cli_path(_get_setting("boss_cli_path"))
    data_dir = resolve_upstream_data_dir()
    if not cli_path:
        raise UpstreamError(
            "DEPENDENCY_MISSING",
            "真实模式需要 boss-agent-cli==2.0.0，但未找到 boss CLI",
            False,
            install_command_hint(),
            layer=FailureLayer.TRANSPORT,
            evidence={"data_dir": str(data_dir.path), "upstream_command": "resolve"},
        )
    return cli_path, data_dir


def _channel_for(run: dict[str, Any]) -> dict[str, Any]:
    """取该任务冻结的采集通道；老任务（迁移 009 之前）缺快照时就地冻结并回写。

    冻结后同一任务的 search / detail 共用同一份通道，绝不回落全局设置。
    """
    snapshot = run.get("config_snapshot") or {}
    channel = snapshot.get("channel")
    if isinstance(channel, dict) and channel.get("fingerprint"):
        return channel
    channel = channel_state.freeze_channel()
    snapshot = dict(snapshot)
    snapshot["channel"] = channel
    run["config_snapshot"] = snapshot
    connection = get_conn()
    connection.execute("UPDATE research_run SET config_snapshot=? WHERE id=?",
                       (jdumps(snapshot), run["id"]))
    connection.commit()
    return channel


def _last_search_ok(run_id: str) -> Optional[bool]:
    """最后一次 search 是否**真实成功**（信封 ok）：区分「空列表」与「调用失败」。

    search_observation 的 outcome 由 do_search 写入：成功为 ``success``，
    失败为 ``error:<code>``。没有记录时返回 None（不下结论）。
    """
    row = query_one(
        "SELECT outcome FROM search_observation WHERE run_id=? "
        "ORDER BY observed_at DESC, rowid DESC LIMIT 1",
        (run_id,),
    )
    if not row:
        return None
    return str(row["outcome"]) == "success"


def _record_channel_result(channel: Optional[dict[str, Any]], *, ok: bool,
                           cards: int = 0, code: str = "", summary: str = "") -> None:
    """把真实采集结果回写 channel_state；写库失败绝不影响采集主流程。"""
    if not channel:
        return
    try:
        if ok:
            channel_state.record_collect_ok(channel, cards)
        else:
            channel_state.record_collect_failed(channel, code, summary)
    except Exception as exc:  # 通道状态只是诊断事实，回写失败不得中断任务
        print(f"[finintel] channel_state 回写失败: {type(exc).__name__}", flush=True)


def _as_run_error(error: UpstreamError) -> RunError:
    return RunError(
        error.code,
        error.message,
        error.retryable,
        error.operator_action,
        upstream_code=error.upstream_code,
        layer=error.layer,
        evidence=error.evidence,
    )


def create_run(name: str, config: dict[str, Any]) -> dict[str, Any]:
    """Create an immutable run configuration; real BOSS is the default source."""
    queries = config.get("queries") or []
    if not queries:
        raise RunError("INVALID_ANALYSIS", "至少需要一个显式查询词", False, "在创建运行时填写查询词")
    if len(queries) > 6:
        raise RunError("INVALID_ANALYSIS", "查询词不超过 6 个，不做笛卡尔积", False, "减少查询词数量")
    source = config.get("source", "zhipin")
    _source_adapter(source)
    cli_path: Optional[str] = None
    data_dir: Optional[DataDirResolution] = None
    if source == "zhipin":
        try:
            cli_path, data_dir = _upstream()
        except UpstreamError as error:
            raise _as_run_error(error) from error

    limits = _validate_budget(config)
    run_id = new_id("run")
    config_snapshot = {
        "source": source,
        "queries": [{"query_id": f"q{index + 1}", "text": query} for index, query in enumerate(queries)],
        "cities": config.get("cities") or [],
        "company_watchlist": config.get("company_watchlist") or [],
        "name": name,
        # 「本次采集源自用户的显式动作」：用户从 /api/search 发起时为 True。
        # 频率闸门据此把用户点「搜索并整理」与自动续跑/后台重试区分开（详见 rate_gate）。
        "user_initiated": bool(config.get("user_initiated", False)),
        # 任务内通道冻结：search/detail 一律用它，之后改全局设置不再影响本任务
        "channel": (channel_state.freeze_channel(data_dir=str(data_dir.path), cli_path=cli_path)
                    if source == "zhipin" and data_dir is not None else None),
    }
    connection = get_conn()
    connection.execute(
        """INSERT INTO research_run (id, name, config_snapshot, state, started_at, counters, limits)
           VALUES (?,?,?,?,?,?,?)""",
        (
            run_id,
            name,
            jdumps(config_snapshot),
            "planned",
            utcnow_iso(),
            jdumps({
                "search_calls": 0,
                "detail_calls": 0,
                "cards_seen": 0,
                "jobs_unique": 0,
                "snapshots_complete": 0,
                "snapshots_missing": 0,
                "snapshots_failed": 0,
                "snapshots_reused": 0,
            }),
            jdumps(limits),
        ),
    )
    connection.commit()
    return get_run(run_id)


def get_run(run_id: str) -> dict[str, Any]:
    """Return a run with decoded immutable configuration and counters."""
    run = query_one("SELECT * FROM research_run WHERE id=?", (run_id,))
    if not run:
        raise KeyError(f"运行 {run_id} 不存在")
    run["config_snapshot"] = jloads(run["config_snapshot"], {})
    run["counters"] = jloads(run["counters"], {})
    run["limits"] = jloads(run["limits"], {})
    # 失败分类（前端空态的唯一依据）与真实到达的阶段
    run["failure_kind"] = user_view.failure_kind(
        run.get("stage"), run.get("state"), _stop_code(run),
        {"collected": int(run["counters"].get("jobs_unique", 0) or 0),
         "organized": int(run.get("organized_count") or 0),
         "failed": int(run.get("failed_count") or 0)},
        last_search_ok=_last_search_ok(run_id),
    )
    return run


def _stop_code(run: dict[str, Any]) -> Optional[str]:
    """``stop_reason`` 里的工程码（与 pipeline._error_code 同口径）。"""
    reason = str(run.get("stop_reason") or "").strip()
    return reason if reason and reason.isascii() and reason.isupper() else None


def set_run_channel(run_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """诊断页显式换通道：校验 → 重新冻结 → 预检 → 回写快照（同一任务不隐式切换）。"""
    get_run(run_id)   # 不存在即抛 KeyError
    result = channel_state.RUNTIME.change_run_channel(run_id, patch or {})
    return {"run_id": run_id, "channel": result["channel"], "policy": result["policy"],
            "state": result["state"], "channel_changed_at": utcnow_iso()}


def list_runs() -> list[dict[str, Any]]:
    """List historical runs without changing their original source labels."""
    rows = query_all("SELECT * FROM research_run ORDER BY started_at DESC")
    for row in rows:
        row["config_snapshot"] = jloads(row["config_snapshot"], {})
        row["counters"] = jloads(row["counters"], {})
        row["limits"] = jloads(row["limits"], {})
    return rows


def _set_counter(run_id: str, counter: str, delta: int = 1, absolute: Optional[int] = None) -> None:
    run = get_run(run_id)
    counters = run["counters"]
    counters[counter] = counters.get(counter, 0) + delta if absolute is None else absolute
    connection = get_conn()
    connection.execute("UPDATE research_run SET counters=? WHERE id=?", (jdumps(counters), run_id))
    connection.commit()


def _set_state(run_id: str, state: str, stop_reason: Optional[str] = None) -> None:
    connection = get_conn()
    connection.execute(
        "UPDATE research_run SET state=?, stop_reason=? WHERE id=?",
        (state, stop_reason, run_id),
    )
    connection.commit()


def _state_for_error(code: str) -> str:
    if code in {"AUTH_REQUIRED", "AUTH_EXPIRED"}:
        return "auth_required"
    if code in {"ACCOUNT_RISK", "ENVIRONMENT_RISK"}:
        return "risk_stopped"
    if code == "RATE_LIMITED":
        return "rate_limited"
    return "failed"


def pending_detail_count(run_id: str) -> int:
    """还有多少岗位等着读详情（``detail_outcome='pending'``）。"""
    return int(query_one(
        "SELECT COUNT(*) AS n FROM run_job WHERE run_id=? AND detail_outcome='pending'",
        (run_id,),
    )["n"])


def has_detail_work_left(run_id: str) -> bool:
    """详情阶段是否**确实还有事可做**：有 pending 岗位 **且** 详情预算未耗尽。

    两个预算彼此独立（``max_search_calls`` / ``max_detail_calls``）：搜索打满
    **不等于**详情不能取。这个函数是所有「能不能继续取详情」判定的唯一依据，
    ``state`` 里不记录是哪个预算耗尽，靠 state 判断本身就是脆的。
    """
    if pending_detail_count(run_id) <= 0:
        return False
    run = get_run(run_id)
    return int(run["counters"].get("detail_calls", 0) or 0) < int(
        run["limits"]["max_detail_calls"])


def _settle_search_budget(run_id: str) -> None:
    """搜索预算耗尽的落点：**只在详情也没得做时**才把整条链路置 ``budget_stopped``。

    真机故障（已复现）：搜索预算在循环中途打满 → 置 ``budget_stopped`` →
    ``fetch_details`` 的状态门禁（只认 planned/collecting）把所有详情调用拒之门外
    → 15 个已搜到的岗位永远取不到详情，任务还不算失败。
    """
    if has_detail_work_left(run_id):
        return
    _set_state(run_id, "budget_stopped", "搜索调用预算耗尽")


def locator_refresh_count(run_id: str) -> int:
    """本任务**已发起**的刷新搜索次数（独立于 search_calls，成功失败都算，纯诊断）。"""
    return int(get_run(run_id)["counters"].get(LOCATOR_REFRESH_COUNTER, 0) or 0)


def locator_refresh_consumed(run_id: str) -> int:
    """本任务**成功兑现**过的刷新次数（失败不计入）。"""
    return int(get_run(run_id)["counters"].get(LOCATOR_REFRESH_CONSUMED, 0) or 0)


def locator_refresh_left(run_id: str) -> bool:
    """还有没有「点一次继续再试一把」的机会。

    口径修正（真机缺陷）：**不再看累计计数器** ``locator_refresh_calls``——一次
    **失败**的刷新搜索会把它打满，旧口径据此判「额度用尽」→ 任务被永久封死
    （真机 run_d7207cd19ee74c4b：15 个岗位停在「重新搜索」，用户连重试入口都没有）。

    新口径只看**是否已经成功兑现过一次**：只要还没成功兑现过，就还有机会——
    要么授权额度正握在手里（本次恢复尚未成功），要么随时可以用一次**显式恢复**
    换到一份新的（``_grant_locator_refresh`` 每次恢复都发）。失败不罚没。
    """
    return locator_refresh_consumed(run_id) < MAX_LOCATOR_REFRESH_CALLS


def locator_refresh_granted(run_id: str) -> bool:
    """是否持有一次性刷新授权——**只有显式恢复（resume）才会发放**。"""
    return int(get_run(run_id)["counters"].get(LOCATOR_REFRESH_GRANT, 0) or 0) > 0


def _grant_locator_refresh(run_id: str) -> None:
    """显式恢复时发放一次性刷新额度。

    每次**显式**恢复都发一份新的（不再受累计计数器约束）：失败不罚没额度，用户再
    点一次「继续」= 新的一次恢复，仍能再试。**不会无限续命**——一次恢复只允许试
    一次，由授权标记在**尝试开始**即被消费来保证（见 ``_revalidate_locator``）；
    续命必须由用户亲手再点一次「继续」。
    """
    _set_counter(run_id, LOCATOR_REFRESH_GRANT, absolute=1)


def stale_locator_pending_count(run_id: str) -> int:
    """还有多少**待读**岗位的定位符已经过期（用来如实描述当下能不能继续）。

    这是关于任务的事实，不是从 ``stop_reason`` 猜出来的：老任务（迁移前落的是
    上游口径 ``UPSTREAM_RESULT_LIMIT``）也能被正确描述。
    """
    now = utcnow_iso()
    rows = query_all(
        """SELECT l.expires_at AS expires_at
           FROM run_job rj JOIN job_locator l ON l.job_id=rj.job_id
           WHERE rj.run_id=? AND rj.detail_outcome='pending'""",
        (run_id,),
    )
    return sum(1 for row in rows if row["expires_at"] and str(row["expires_at"]) <= now)


def _refresh_run_locators(run_id: str, cards: list[dict[str, Any]]) -> int:
    """用**一次**搜索的卡片，把本任务所有已装岗位的定位符一次性刷新。

    只更新本任务已有的岗位（绝不借刷新偷偷扩采），返回刷新条数。
    """
    by_source_id = {
        str(card["source_job_id"]): card
        for card in cards
        if card.get("source_job_id") and card.get("security_id")
    }
    if not by_source_id:
        return 0
    rows = query_all(
        """SELECT j.id AS job_id, j.source_job_id AS source_job_id
           FROM run_job rj JOIN job j ON j.id=rj.job_id WHERE rj.run_id=?""",
        (run_id,),
    )
    refreshed = 0
    for row in rows:
        card = by_source_id.get(str(row["source_job_id"] or ""))
        if card:
            _refresh_locator(row["job_id"], card)
            refreshed += 1
    return refreshed


def _record_upstream_call(
    run_id: str,
    command: str,
    trace: Optional[dict[str, Any]],
    *,
    result_summary: str,
    ref: Optional[str] = None,
    error: Optional[UpstreamError] = None,
) -> None:
    """Persist one credential-redacted upstream call for audit and reproduction."""
    evidence = error.evidence if error else (trace or {})
    argv = evidence.get("argv_redacted", []) if isinstance(evidence, dict) else []
    data_dir = evidence.get("data_dir", "") if isinstance(evidence, dict) else ""
    elapsed = evidence.get("elapsed_ms") if isinstance(evidence, dict) else None
    layer = error.layer if error else (trace or {}).get("layer", "ok")
    error_code = error.code if error else (trace or {}).get("error_code")
    ok_value = 0 if error else 1
    connection = get_conn()
    connection.execute(
        """INSERT INTO upstream_call
           (id,run_id,command,argv_redacted,data_dir,started_at,elapsed_ms,layer,ok,
            error_code,result_summary,ref)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            new_id("call"),
            run_id,
            command,
            jdumps(argv),
            str(data_dir),
            utcnow_iso(),
            elapsed,
            layer,
            ok_value,
            error_code,
            result_summary[:500],
            ref,
        ),
    )
    connection.commit()


def list_upstream_calls(run_id: str) -> list[dict[str, Any]]:
    """List sanitized upstream call evidence for one run."""
    rows = query_all("SELECT * FROM upstream_call WHERE run_id=? ORDER BY started_at", (run_id,))
    for row in rows:
        row["argv_redacted"] = jloads(row["argv_redacted"], [])
        row["ok"] = bool(row["ok"])
    return rows


def do_search(run_id: str, query_id: str, page: int, *,
              force_refresh: bool = False, manual: Optional[bool] = None) -> dict[str, Any]:
    """Execute one bounded source search and persist exact-source observations.

    ``force_refresh`` 是用户显式「获取最新结果」的唯一出口：为真时**绕过**本任务已成功
    的搜索观测（应用层复用）**并**给上游传 ``--no-cache``（跳过上游本地缓存），从而真正联网
    取一次最新结果。默认 ``False`` = 允许复用 + 允许上游命中缓存（少打扰平台）。

    ``manual`` = 「本次采集是否由用户的**显式动作**触发」（用户点「搜索并整理」或「获取最新结果」），
    用于让频率闸门区分「人做的决定」与「自动续跑/后台重试」。传 ``None`` 时按
    「本任务由用户显式发起（``config_snapshot.user_initiated``）**或** 本次是显式刷新」判定——
    **不**再看单一的 ``force_refresh``（否则用户自己点搜索会被误判为自动路径而拒之门外）。
    注意：``manual`` 只影响"是否允许这一次"，**最小间隔与 10 分钟窗口上限对 manual 请求同样生效**。
    """
    run = get_run(run_id)
    if run["state"] not in ("planned", "collecting"):
        raise RunError(
            "TOOL_NOT_ALLOWED",
            f"运行状态为 {run['state']}，不能发起新的平台调用",
            False,
            "受控停止后需显式 resume_collection，或新建有明确预算的新批次",
        )
    queries = run["config_snapshot"]["queries"]
    if not any(query["query_id"] == query_id for query in queries):
        raise RunError(
            "TOOL_NOT_ALLOWED",
            f"query_id {query_id} 不属于该运行的查询计划",
            False,
            "使用返回的查询计划中的 query_id",
        )
    if page < 1:
        raise RunError("INVALID_ANALYSIS", "page 必须为正整数", False, "检查分页参数")
    counters = run["counters"]
    limits = run["limits"]
    if counters["search_calls"] >= limits["max_search_calls"]:
        # 搜索打满 ≠ 详情不能取：只在详情也没得做时才置 budget_stopped
        _settle_search_budget(run_id)
        raise RunError(
            "BUDGET_EXHAUSTED",
            f"搜索调用已达预算上限 {limits['max_search_calls']}（含失败）",
            False,
            "已采集素材仍可分析；如需继续采集请新建有明确预算的新批次",
        )

    existing = query_one(
        "SELECT * FROM search_observation WHERE run_id=? AND query_id=? AND page=? AND outcome='success'",
        (run_id, query_id, page),
    )
    if existing and not force_refresh:
        jobs = query_all(
            """SELECT j.*, rj.job_ref, rj.detail_outcome FROM run_job rj
               JOIN job j ON j.id=rj.job_id WHERE rj.run_id=?""",
            (run_id,),
        )
        return {
            "reused": True,
            "cards": _cards_from_jobs(jobs),
            "jobs": jobs,
            "counters": get_run(run_id)["counters"],
            "observation_id": existing["id"],
            "result_code": "EMPTY_RESULT" if not jobs else "OK",
            # 应用层复用：既没联网也没读上游缓存，如实标为空
            "cache_age_seconds": None,
            "cache_hit": False,
        }

    query_text = next(query["text"] for query in queries if query["query_id"] == query_id)
    city = run["config_snapshot"]["cities"][0] if run["config_snapshot"]["cities"] else None
    source = run["config_snapshot"]["source"]
    # 应用层持久化频率闸门：只在真正要发 list 请求（zhipin）前拦。上面「应用层复用」已提前返回，
    # 命中复用的那一次**不**算打扰平台，故不在此门禁范围。被拦时**绝不**发任何请求。
    # manual（用户显式发起）= 本任务由用户显式创建 或 本次是显式刷新；自动续跑/重试一律不算。
    if manual is None:
        manual = force_refresh or bool(run["config_snapshot"].get("user_initiated"))
    if source == "zhipin":
        decision = rate_gate.check_list_request(run_id, manual=manual)
        if not decision["allowed"]:
            raise RunError(
                decision["code"], decision["message"], True, decision["operator_action"],
            )
    adapter = _source_adapter(source)
    channel: Optional[dict[str, Any]] = None
    cache_age: Optional[int] = None
    try:
        if source == "zhipin":
            channel = _channel_for(run)
            cli_path, data_dir = _upstream(channel)
            # 取数**前**探测：库里若已有未过期的同条件缓存，则这一次不会联网（命中上游缓存），
            # 其年龄如实记录；没有则说明会真正联网，age 保持 None。缓存探测失败一律当作「未知」。
            if not force_refresh:
                cache_age = upstream_cache.cache_age_seconds(
                    getattr(data_dir, "path", data_dir), query_text, city, page)
            # 保守登记：即使这次调用随后失败，也算「已尝试过一次 list 请求」（宁可少发不发多）
            rate_gate.record_list_request(run_id)
            result = _run_async(
                adapter.search(
                    cli_path,
                    query_text,
                    city,
                    page,
                    data_dir,
                    limits["call_timeout_seconds"],
                    # 默认 False → 允许上游命中缓存；仅「获取最新结果」显式传 True 跳过缓存
                    force_refresh,
                    # 通道必传且来自任务冻结值：search 与 detail 共用同一份通道
                    top_level_options=channel_options_from_snapshot(channel),
                )
            )
            _record_channel_result(channel, ok=True, cards=len(result.get("cards") or []))
            _record_upstream_call(
                run_id,
                "search",
                result["trace"],
                result_summary=(
                    "EMPTY_RESULT" if result["empty_result"] else f"cards={len(result['cards'])}, total={result['total']}"
                ),
                ref=f"{query_id}:{page}",
            )
        else:
            result = adapter.search(query_text, city, page)
    except UpstreamError as error:
        _record_observation(run_id, query_id, page, f"error:{error.code}", [])
        _set_counter(run_id, "search_calls")
        if source == "zhipin":
            # 风险码（环境/账号风控）：立即进入应用层硬冷却，冷却期内任何路径都不得再发请求
            if rate_gate.is_risk_code(error.code):
                rate_gate.note_risk_stop(error.code)
            # 真实取数失败 = 该通道未自证可用；search 是唯一走浏览器通道的调用
            _record_channel_result(channel or _channel_for(run), ok=False,
                                   code=error.code, summary=error.message)
        _record_upstream_call(
            run_id,
            "search",
            None,
            result_summary=error.message,
            ref=f"{query_id}:{page}",
            error=error,
        )
        _set_state(run_id, _state_for_error(error.code), error.code)
        raise _as_run_error(error) from error

    _set_counter(run_id, "search_calls")
    job_refs: list[dict[str, Any]] = []
    source_job_ids: list[str] = []
    for card in result["cards"]:
        if not card.get("title") or not card.get("company_name"):
            if source == "zhipin":
                raise RunError(
                    "UPSTREAM_CONTRACT_CHANGED",
                    "真实搜索卡片缺少标题或公司，已停止入库",
                    False,
                    "核对上游字段契约",
                    layer=FailureLayer.CONTRACT,
                )
            continue
        stable_id = card.get("source_job_id")
        if stable_id:
            source_job_ids.append(stable_id)
        job = _upsert_job(run_id, source, card)
        job_refs.append({
            "job_ref": job["job_ref"],
            "job_id": job["id"],
            "title": job["title"],
            "company": job["company_name_raw"],
            "city": job["city"],
        })
    observation_id = _record_observation(run_id, query_id, page, "success", source_job_ids,
                                         cache_age_seconds=cache_age)
    _set_counter(run_id, "cards_seen", len(result["cards"]))
    if run["state"] == "planned":
        _set_state(run_id, "collecting")
    return {
        "reused": False,
        "query": query_text,
        "query_id": query_id,
        "page": result["page"],
        "total": result["total"],
        "has_more": result["has_more"],
        "cards": [_card_view(card) for card in result["cards"]],
        "job_refs": job_refs,
        "counters": get_run(run_id)["counters"],
        "observation_id": observation_id,
        "result_code": result.get("result_code", "EMPTY_RESULT" if not result["cards"] else "OK"),
        # 「结果是否来自本地缓存 + 多旧」如实回传，供结果页如实标注，绝不沉默地端上旧数据
        "cache_hit": cache_age is not None,
        "cache_age_seconds": cache_age,
    }


def _run_async(coroutine: Any) -> Any:
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, coroutine).result(timeout=360)
    return asyncio.run(coroutine)


def _card_view(card: dict[str, Any]) -> dict[str, Any]:
    return {
        key: card.get(key)
        for key in (
            "source_job_id",
            "title",
            "company_name",
            "company_industry",
            "city",
            "salary",
            "experience",
        )
    }


def _cards_from_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source_job_id": job["source_job_id"],
            "title": job["title"],
            "company_name": job["company_name_raw"],
            "city": job["city"],
        }
        for job in jobs
    ]


def _record_observation(
    run_id: str,
    query_id: str,
    page: int,
    outcome: str,
    source_job_ids: list[str],
    *,
    cache_age_seconds: Optional[int] = None,
) -> str:
    observation_id = new_id("obs")
    connection = get_conn()
    connection.execute(
        """INSERT INTO search_observation
               (id, run_id, query_id, page, observed_at, outcome, source_job_ids, cache_age_seconds)
           VALUES (?,?,?,?,?,?,?,?)""",
        (observation_id, run_id, query_id, page, utcnow_iso(), outcome, jdumps(source_job_ids),
         cache_age_seconds),
    )
    connection.commit()
    return observation_id


def latest_search_cache_age(run_id: str) -> Optional[int]:
    """本任务**最近一次成功搜索**命中的上游缓存年龄（秒）；联网抓取或从未命中则为 None。

    只认成功观测里 ``cache_age_seconds`` 非空的行。结果页据此如实标注「来自本地缓存」，
    绝不凭猜测声称命中缓存。
    """
    row = query_one(
        """SELECT cache_age_seconds FROM search_observation
           WHERE run_id=? AND outcome='success' AND cache_age_seconds IS NOT NULL
           ORDER BY observed_at DESC, rowid DESC LIMIT 1""",
        (run_id,),
    )
    return int(row["cache_age_seconds"]) if row and row["cache_age_seconds"] is not None else None


def _locator_expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=SECURITY_ID_TTL_SECONDS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _upsert_job(run_id: str, source: str, card: dict[str, Any]) -> dict[str, Any]:
    connection = get_conn()
    company = company_mod.ensure_company(card["company_name"], source)
    stable_id = card.get("source_job_id")
    if source == "zhipin" and stable_id:
        identity_key = f"jid:zhipin:{stable_id}"
    elif stable_id:
        identity_key = f"sid:{stable_id}"
    else:
        identity_key = schemas.identity_key_for(
            source,
            None,
            card["company_name"],
            card["title"],
            card.get("city"),
            "pending",
        )
    existing = query_one("SELECT * FROM job WHERE source=? AND identity_key=?", (source, identity_key))
    now = utcnow_iso()
    if existing:
        connection.execute("UPDATE job SET last_seen_at=? WHERE id=?", (now, existing["id"]))
        job_id = existing["id"]
    else:
        job_id = new_id("job")
        connection.execute(
            """INSERT INTO job (id, source, source_job_id, identity_key, company_id,
               company_name_raw, title, city, first_seen_at, last_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                job_id,
                source,
                stable_id,
                identity_key,
                company["id"],
                card["company_name"],
                card["title"],
                card.get("city"),
                now,
                now,
            ),
        )
    if card.get("security_id"):
        expires_at = _locator_expiry() if source == "zhipin" else None
        connection.execute(
            """INSERT INTO job_locator (job_id, security_id, lid, expires_at) VALUES (?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET security_id=excluded.security_id,
                 lid=excluded.lid, expires_at=excluded.expires_at""",
            (job_id, card["security_id"], card.get("lid"), expires_at),
        )
    relation = query_one("SELECT * FROM run_job WHERE run_id=? AND job_id=?", (run_id, job_id))
    if not relation:
        job_ref = new_id("jr")
        connection.execute(
            "INSERT INTO run_job (run_id, job_id, detail_outcome, job_ref) VALUES (?,?,?,?)",
            (run_id, job_id, "pending", job_ref),
        )
    else:
        job_ref = relation["job_ref"]
    connection.commit()
    total = query_one("SELECT COUNT(*) AS n FROM run_job WHERE run_id=?", (run_id,))["n"]
    _set_counter(run_id, "jobs_unique", absolute=total)
    return {
        "id": job_id,
        "job_ref": job_ref,
        "title": card["title"],
        "company_name_raw": card["company_name"],
        "city": card.get("city"),
    }


def _locator_is_fresh(locator: Optional[dict[str, Any]]) -> bool:
    if not locator or not locator.get("security_id"):
        return False
    expires_at = locator.get("expires_at")
    return not expires_at or expires_at > utcnow_iso()


def _refresh_locator(job_id: str, card: dict[str, Any]) -> dict[str, Any]:
    connection = get_conn()
    connection.execute(
        """INSERT INTO job_locator (job_id,security_id,lid,expires_at) VALUES (?,?,?,?)
           ON CONFLICT(job_id) DO UPDATE SET security_id=excluded.security_id,
             lid=excluded.lid, expires_at=excluded.expires_at""",
        (job_id, card["security_id"], card.get("lid"), _locator_expiry()),
    )
    connection.commit()
    return query_one("SELECT * FROM job_locator WHERE job_id=?", (job_id,))


def _detail_lid(locator: Optional[dict[str, Any]]) -> Optional[str]:
    """取详情页用的 lid：缺失/空白一律返回 ``None``（**不是**空字符串）。

    ``lid`` 在上游是**可选**的列表上下文（``map_search_card`` 把它列为 optional，
    SDK ``job_card_browser(security_id, lid="")`` 也允许缺省），所以「没有 lid」是
    合法状态；``source_boss.detail()`` 收到 ``None`` 会**正确省略**该位置参数，
    而收到空白串会报**点名 lid** 的契约错。这里把两者分清，避免把「本来就没有列表
    上下文」误报成调用方违约。
    """
    raw = (locator or {}).get("lid")
    if raw is None:
        return None
    return str(raw).strip() or None


def _revalidate_locator(run: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """Refresh an expired temporary locator with one no-cache search, within budget."""
    locator = query_one("SELECT * FROM job_locator WHERE job_id=?", (job["id"],))
    if _locator_is_fresh(locator):
        return locator
    counters = get_run(run["id"])["counters"]
    limits = run["limits"]
    search_left = counters["search_calls"] < limits["max_search_calls"]
    # 这一次恢复的额度是否还握在手里——**只看授权标记**，与「还剩没剩额度」的对外口径
    # （``locator_refresh_left``）分开：对外那个负责「还能不能点继续」，这里负责
    # 「本次恢复的那一次试过没有」。授权在尝试时即消费 → 同一次恢复只试一次。
    grant_left = locator_refresh_granted(run["id"])
    if not search_left and not grant_left:
        raise UpstreamError(
            STALE_LOCATOR_CODE,
            "本批岗位的定位信息已过期（约 30 分钟有效），刷新定位后才能继续读取详情",
            False,
            "点一次「继续」会先刷新定位再接着读取（关键词与已取得的内容都会保留）",
            layer=FailureLayer.BUSINESS,
        )
    # 只有搜索预算确实不够时才动刷新额度：用额度时**不动 search_calls**，
    # 保证任务声明的 max_search_calls 在正常路径上仍被严格遵守。
    use_grant = not search_left
    observations = query_all(
        "SELECT * FROM search_observation WHERE run_id=? AND outcome='success' ORDER BY observed_at",
        (run["id"],),
    )
    selected = None
    for observation in observations:
        if job["source_job_id"] in jloads(observation["source_job_ids"], []):
            selected = observation
            break
    if selected is None:
        raise UpstreamError(
            "JOB_NOT_FOUND",
            "找不到用于刷新详情定位符的原始搜索记录",
            False,
            "重新执行该运行的原始查询后再读取详情",
            upstream_code="JOB_NOT_FOUND",
        )
    query = next(
        query
        for query in run["config_snapshot"]["queries"]
        if query["query_id"] == selected["query_id"]
    )
    city = run["config_snapshot"]["cities"][0] if run["config_snapshot"]["cities"] else None
    adapter = _source_adapter("zhipin")
    channel = _channel_for(run)
    cli_path, data_dir = _upstream(channel)
    if use_grant:
        # 诊断计数：本次确实发起了一次「为刷新定位」的搜索（不管成败）。
        _set_counter(run["id"], LOCATOR_REFRESH_COUNTER)
        # 授权标记在**尝试开始**即消费：同一次恢复只允许试一次（绝不无限重试）。
        # 但额度**成功才真正销账**（见下方成功分支）——失败的刷新不罚没，用户下次
        # 显式「继续」= 新的一次恢复，仍能拿到一次尝试（真机缺陷修正）。
        _set_counter(run["id"], LOCATOR_REFRESH_GRANT, absolute=0)
    else:
        _set_counter(run["id"], "search_calls")
    # 审计用的可区分 ref：诊断页能一眼看出这次搜索是「为刷新定位」花的
    refresh_ref = f"{query['query_id']}:locator-refresh"
    try:
        result = _run_async(
            adapter.search(
                cli_path,
                query["text"],
                city,
                selected["page"],
                data_dir,
                limits["call_timeout_seconds"],
                True,
                top_level_options=channel_options_from_snapshot(channel),
            )
        )
        _record_channel_result(channel, ok=True, cards=len(result.get("cards") or []))
        _record_upstream_call(
            run["id"],
            "search",
            result["trace"],
            result_summary=(
                f"locator_refresh cards={len(result['cards'])}"
                f"{' (额外额度)' if use_grant else ''}"
            ),
            ref=refresh_ref,
        )
    except UpstreamError as error:
        _record_channel_result(channel, ok=False, code=error.code, summary=error.message)
        _record_upstream_call(
            run["id"],
            "search",
            None,
            result_summary=f"locator_refresh failed: {error.message}",
            ref=refresh_ref,
            error=error,
        )
        raise
    # 成功兑现：一次性额度此刻才真正销账（失败走上面 except 原样保留）。
    if use_grant:
        _set_counter(run["id"], LOCATOR_REFRESH_CONSUMED)
    # 一次搜索的卡片就能覆盖整批：本任务**所有**已装岗位的定位符一起刷新，
    # 后续岗位在 fetch_details 里会直接命中「新鲜」判定，不再重复花额度。
    _refresh_run_locators(run["id"], list(result.get("cards") or []))
    locator = query_one("SELECT * FROM job_locator WHERE job_id=?", (job["id"],))
    if not _locator_is_fresh(locator):
        raise UpstreamError(
            "JOB_NOT_FOUND",
            "刷新搜索后未找到原岗位，岗位可能已下架",
            False,
            "跳过该岗位并继续处理其他岗位",
            upstream_code="JOB_NOT_FOUND",
        )
    return locator


def fetch_details(run_id: str, job_refs: list[str]) -> dict[str, Any]:
    """Read bounded details; real failures never fall back to demo fixtures."""
    run = get_run(run_id)
    if run["state"] not in ("planned", "collecting"):
        raise RunError(
            "TOOL_NOT_ALLOWED",
            f"运行状态为 {run['state']}，不能发起新的平台调用",
            False,
            "受控停止后需显式 resume_collection",
        )
    limits = run["limits"]
    if get_run(run_id)["counters"]["detail_calls"] >= limits["max_detail_calls"]:
        # 详情预算耗尽：这条链路确实无事可做了，才置 budget_stopped
        _set_state(run_id, "budget_stopped", "详情调用预算耗尽")
        raise RunError(
            "BUDGET_EXHAUSTED",
            f"详情调用已达预算上限 {limits['max_detail_calls']}（含失败）",
            False,
            "已采集素材仍可分析与出报告",
        )
    if not job_refs or len(job_refs) > limits["details_per_call"]:
        raise RunError(
            "INVALID_ANALYSIS",
            f"job_refs 数量需为 1~{limits['details_per_call']}",
            False,
            "减少每次读取的岗位数",
        )

    connection = get_conn()
    outcomes: list[dict[str, Any]] = []
    source = run["config_snapshot"]["source"]
    adapter = _source_adapter(source)
    for job_ref in job_refs:
        relation = query_one("SELECT * FROM run_job WHERE run_id=? AND job_ref=?", (run_id, job_ref))
        if not relation:
            outcomes.append({"job_ref": job_ref, "outcome": "not_found", "error": "job_ref 不属于该运行"})
            continue
        job = query_one("SELECT * FROM job WHERE id=?", (relation["job_id"],))
        if not job:
            outcomes.append({"job_ref": job_ref, "outcome": "not_found", "error": "岗位不存在"})
            continue
        existing_snapshot = query_one(
            """SELECT * FROM job_snapshot WHERE job_id=? AND id=? AND detail_status IN ('complete','partial')
               ORDER BY fetched_at DESC LIMIT 1""",
            (job["id"], relation["snapshot_id"]),
        )
        if existing_snapshot:
            connection.execute(
                "UPDATE run_job SET snapshot_id=?, detail_outcome='reused' WHERE run_id=? AND job_id=?",
                (existing_snapshot["id"], run_id, job["id"]),
            )
            connection.commit()
            _set_counter(run_id, "snapshots_reused")
            outcomes.append({
                "job_ref": job_ref,
                "outcome": "reused",
                "snapshot_id": existing_snapshot["id"],
                "title": job["title"],
            })
            continue
        if get_run(run_id)["counters"]["detail_calls"] >= limits["max_detail_calls"]:
            outcomes.append({
                "job_ref": job_ref,
                "outcome": "pending",
                "error": "本轮调用预算已耗尽，岗位保留待处理，不会静默漏项",
            })
            break

        try:
            if source == "demo":
                locator = query_one("SELECT * FROM job_locator WHERE job_id=?", (job["id"],))
                if not locator or not locator["security_id"]:
                    raise KeyError("缺少演示详情定位符")
                _set_counter(run_id, "detail_calls")
                detail_data = adapter.detail(locator["security_id"])
            else:
                # Detail requests share account risk stops; list-only spacing does not apply.
                decision = rate_gate.check_list_request(run_id, manual=False)
                if not decision["allowed"] and decision["code"] not in {
                    rate_gate.RATE_MIN_INTERVAL, rate_gate.RATE_WINDOW_LIMIT,
                }:
                    raise RunError(decision["code"], decision["message"], True,
                                   decision["operator_action"])
                if job.get("source_job_id") and getattr(adapter,"STABLE_DETAIL_ID",False):
                    # A public detail URL does not depend on a short-lived card securityId.
                    locator = query_one("SELECT * FROM job_locator WHERE job_id=?",(job["id"],)) or {}
                    locator = {**locator,"security_id":locator.get("security_id") or job["source_job_id"]}
                else:
                    locator = _revalidate_locator(run, job)
                channel = _channel_for(run)
                cli_path, data_dir = _upstream(channel)
                channel_options = channel_options_from_snapshot(channel)
                _set_counter(run_id, "detail_calls")
                try:
                    detail_data = _run_async(
                        adapter.detail(
                            cli_path,
                            locator["security_id"],
                            job["source_job_id"],
                            _detail_lid(locator),
                            data_dir,
                            limits["call_timeout_seconds"],
                            # 与本次 search 同一份冻结通道：详情现在走**浏览器 worker**，
                            # 与 search 共用同一份 (data_dir, cli_path, browser_source, cdp_url)。
                            # 旧实现「detail 走 httpx + 本地凭据」的通道已废弃，勿复其说。
                            top_level_options=channel_options,
                        )
                    )
                    _record_upstream_call(
                        run_id,
                        "detail",
                        detail_data["trace"],
                        result_summary=(
                            f"detail_status={detail_data['detail_status']}, "
                            f"responsibility_chars={len(detail_data['responsibilities'])}"
                        ),
                        ref=job_ref,
                    )
                except UpstreamError as first_error:
                    _record_upstream_call(
                        run_id,
                        "detail",
                        None,
                        result_summary=first_error.message,
                        ref=job_ref,
                        error=first_error,
                    )
                    # 浏览器通道的两类新失败码与 UPSTREAM_BROWSER_UNAVAILABLE 同口径：
                    # 允许用刷新的定位符重试一次，不把整批详情直接判死。
                    if first_error.code not in {
                        "JOB_NOT_FOUND", "UPSTREAM_BROWSER_UNAVAILABLE",
                        "OWN_BROWSER_NOT_READY", "OWN_BROWSER_NO_SESSION",
                    }:
                        raise
                    refreshed = _revalidate_locator(run, job)
                    if get_run(run_id)["counters"]["detail_calls"] >= limits["max_detail_calls"]:
                        raise
                    _set_counter(run_id, "detail_calls")
                    detail_data = _run_async(
                        adapter.detail(
                            cli_path,
                            refreshed["security_id"],
                            job["source_job_id"],
                            _detail_lid(refreshed),
                            data_dir,
                            limits["call_timeout_seconds"],
                            top_level_options=channel_options,
                        )
                    )
                    _record_upstream_call(
                        run_id,
                        "detail",
                        detail_data["trace"],
                        result_summary=f"retry detail_status={detail_data['detail_status']}",
                        ref=job_ref,
                    )
        except KeyError as error:
            connection.execute(
                "UPDATE run_job SET detail_outcome='failed', last_error=? WHERE run_id=? AND job_id=?",
                (str(error)[:200], run_id, job["id"]),
            )
            connection.commit()
            _set_counter(run_id, "snapshots_failed")
            outcomes.append({"job_ref": job_ref, "outcome": "failed", "error": f"岗位详情读取失败: {error}"})
            continue
        except UpstreamError as error:
            if error.code == STALE_LOCATOR_CODE:
                # 定位符过期且刷新额度用尽：岗位本身没坏，只是当下取不到，
                # **不标失败**、整批停下并如实报错（由 UI 引导「重新搜索」）。
                outcomes.append({"job_ref": job_ref, "outcome": "pending", "error": error.message})
                _set_state(run_id, "failed", error.code)
                raise _as_run_error(error) from error
            if source == "zhipin" and rate_gate.is_risk_code(error.code):
                rate_gate.note_risk_stop(error.code)
                _record_channel_result(_channel_for(run), ok=False,
                                       code=error.code, summary=error.message)
                connection.execute(
                    "UPDATE run_job SET last_error=? WHERE run_id=? AND job_id=?",
                    (f"{error.code}: {error.message}"[:500], run_id, job["id"]),
                )
                connection.commit()
                _set_state(run_id, _state_for_error(error.code), error.code)
                raise _as_run_error(error) from error
            connection.execute(
                "UPDATE run_job SET detail_outcome='failed', last_error=? WHERE run_id=? AND job_id=?",
                (f"{error.code}: {error.message}"[:500], run_id, job["id"]),
            )
            connection.commit()
            _set_counter(run_id, "snapshots_failed")
            _set_state(run_id, _state_for_error(error.code), error.code)
            if source == "zhipin" and error.code.upper() in user_view.FAILURE_CHANNEL_CODES:
                # 详情侧也证明该通道不可用（与 search 同一份冻结通道，结论同样有效）
                _record_channel_result(_channel_for(run), ok=False,
                                       code=error.code, summary=error.message)
            if error.code == "JOB_NOT_FOUND":
                outcomes.append({"job_ref": job_ref, "outcome": "failed", "error": error.message})
                continue
            raise _as_run_error(error) from error

        responsibilities = schemas.normalize_evidence_text(detail_data.get("responsibilities") or "")
        requirements = schemas.normalize_evidence_text(detail_data.get("requirements") or "")
        raw_length = len((detail_data.get("responsibilities") or "") + (detail_data.get("requirements") or ""))
        if raw_length > schemas.MAX_BUSINESS_TEXT_CHARS:
            detail_status = "too_large"
        elif not responsibilities:
            detail_status = "missing"
        else:
            detail_status = detail_data.get("detail_status") or ("partial" if source == "zhipin" else "complete")
            if detail_status not in ("complete", "partial"):
                detail_status = "partial"
        evidence_text, _ranges = analyzer.build_evidence_text(responsibilities, requirements)
        content_hash = schemas.content_hash_fields(
            detail_data.get("title") or job["title"],
            detail_data.get("company_name") or job["company_name_raw"],
            detail_data.get("city") or job["city"],
            responsibilities,
            requirements,
        )
        snapshot = query_one(
            "SELECT * FROM job_snapshot WHERE job_id=? AND content_hash=?",
            (job["id"], content_hash),
        )
        metadata = {"detail_status":detail_status,
            "detail_source":detail_data.get("detail_source") or ("demo" if source=="demo" else "unknown"),
            "mapping_warning":detail_data.get("mapping_warning"),
            "source_url_verified":1 if detail_data.get("source_url_verified") else 0}
        if snapshot and any(snapshot.get(k)!=v for k,v in metadata.items()):
            # Equal text with a new completeness receipt is a new immutable snapshot.
            import hashlib
            content_hash = hashlib.sha256((content_hash+jdumps(metadata)).encode('utf-8')).hexdigest()
            snapshot = query_one("SELECT * FROM job_snapshot WHERE job_id=? AND content_hash=?",
                                 (job["id"],content_hash))
            if snapshot and any(snapshot.get(k)!=v for k,v in metadata.items()):
                # Older versions could change metadata in place. Never reuse that receipt.
                content_hash = hashlib.sha256((content_hash+new_id("receipt")).encode('utf-8')).hexdigest()
                snapshot = None
        if not snapshot:
            snapshot_id = new_id("snap")
            connection.execute(
                """INSERT INTO job_snapshot
                   (id,job_id,content_hash,source_url,fetched_at,published_at,published_at_raw,
                    source_status,detail_status,evidence_text,responsibilities_text,
                    requirements_text,mapping_version,source_platform,source_job_id,
                    source_url_verified)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot_id,
                    job["id"],
                    content_hash,
                    _strip_credential_params(detail_data.get("source_url")),
                    utcnow_iso(),
                    None,
                    detail_data.get("published_at_raw"),
                    detail_data.get("source_status") or "unknown",
                    detail_status,
                    evidence_text,
                    responsibilities or None,
                    requirements or None,
                    schemas.MAPPING_VERSION,
                    detail_data.get("source_platform") or source,
                    detail_data.get("source_job_id") or job["source_job_id"],
                    1 if detail_data.get("source_url_verified") else 0,
                ),
            )
            connection.execute(
                "UPDATE job_snapshot SET detail_source=?, mapping_warning=? WHERE id=?",
                (metadata["detail_source"],metadata["mapping_warning"],snapshot_id))
        else:
            snapshot_id = snapshot["id"]
        counter_map = {
            "partial": "snapshots_partial",
            "complete": "snapshots_complete",
            "missing": "snapshots_missing",
            "too_large": "snapshots_complete",
        }
        connection.execute(
            "UPDATE run_job SET snapshot_id=?, detail_outcome=? WHERE run_id=? AND job_id=?",
            (snapshot_id, detail_status, run_id, job["id"]),
        )
        connection.commit()
        _set_counter(run_id, counter_map[detail_status])
        outcomes.append({
            "job_ref": job_ref,
            "outcome": detail_status,
            "snapshot_id": snapshot_id,
            "title": job["title"],
            "detail_status": detail_status,
        })

    return {
        "outcomes": outcomes,
        "counters": get_run(run_id)["counters"],
        "completeness": {
            "total": len(job_refs),
            "done": sum(1 for outcome in outcomes if outcome["outcome"] != "pending"),
        },
    }


def _strip_credential_params(url: Optional[str]) -> Optional[str]:
    """Remove credential-shaped query parameters from a public source URL."""
    if not url:
        return None
    import re

    return re.sub(r"([?&])(securityId|lid|token|key)=[^&]*", r"\1[removed]", url)


def finish_collection(run_id: str) -> dict[str, Any]:
    run = get_run(run_id)
    if run["state"] not in ("planned", "collecting", "budget_stopped"):
        raise RunError("TOOL_NOT_ALLOWED", f"状态 {run['state']} 下不能结束采集", False, "")
    failures = query_one(
        "SELECT COUNT(*) AS n FROM run_job WHERE run_id=? AND detail_outcome IN ('failed','not_found')",
        (run_id,),
    )["n"]
    new_state = "partial" if failures else "ready_for_analysis"
    connection = get_conn()
    connection.execute(
        "UPDATE research_run SET state=?, collection_ended_at=? WHERE id=?",
        (new_state, utcnow_iso(), run_id),
    )
    connection.commit()
    return get_run(run_id)


def stop_run(run_id: str, reason: str = "用户手动停止") -> dict[str, Any]:
    _set_state(run_id, "stopped", reason)
    return get_run(run_id)


def resume_collection(run_id: str) -> dict[str, Any]:
    """把受控暂停的任务拉回可采集态（**不重置任何已消耗计数**）。

    ``budget_stopped`` 也允许恢复，但**仅限**「还有 pending 岗位且详情预算未耗尽」：
    搜索预算打满不该连详情一起判死（两个预算彼此独立）。真的没得做时照旧拒绝，
    不给「点了继续却什么也没发生」的假象。
    """
    run = get_run(run_id)
    state = run["state"]
    if state not in ("auth_required", "risk_stopped", "rate_limited", "stopped", "failed",
                     "budget_stopped"):
        raise RunError(
            "TOOL_NOT_ALLOWED",
            f"状态 {run['state']} 不属于受控暂停，不能 resume",
            False,
            "受控停止后需显式 resume_collection，或新建有明确预算的新批次",
        )
    if run.get("stop_reason") == STALE_LOCATOR_CODE and not locator_refresh_left(run_id):
        # 本任务**已经成功刷新过一次**、定位符如今又过期：这一次「继续」不会再拿到新额度
        # → 如实拒绝，别给假按钮。（关键区别：一次**失败**的刷新不算「已刷新」，额度原样
        # 保留，「继续」仍然可用——真机缺陷修正，见 locator_refresh_left 的说明。）
        raise RunError(
            STALE_LOCATOR_CODE,
            "本批岗位的定位信息已过期（约 30 分钟有效），本任务已刷新过一次，继续也不会成功",
            False,
            "重新搜索一次（关键词与已取得的内容都会保留）",
        )
    counters, limits = run["counters"], run["limits"]
    if counters["search_calls"] >= limits["max_search_calls"] and counters["detail_calls"] >= limits["max_detail_calls"]:
        raise RunError(
            "BUDGET_EXHAUSTED",
            "预算已耗尽，resume 不能重置已消耗调用次数",
            False,
            "请新建有明确预算的新批次",
        )
    if state == "budget_stopped" and not has_detail_work_left(run_id):
        raise RunError(
            "BUDGET_EXHAUSTED",
            "搜索预算已用完，且没有待读取详情的岗位，继续也不会新增采集",
            False,
            "请新建有明确预算的新批次；已取得的岗位与正文均已保留",
        )
    # 显式恢复才发放一次性定位符刷新额度（正常采集路径没有这个额度）
    if state == 'auth_required':
        # The user's resume authorizes retrying the blocked job, without refunding calls.
        store.execute("""UPDATE run_job SET detail_outcome='pending', last_error=NULL
                   WHERE run_id=? AND detail_outcome='failed' AND last_error LIKE 'AUTH_REQUIRED:%'""",
                (run_id,))
    elif state == 'failed' and run.get('stop_reason') in ('UPSTREAM_CONTRACT_CHANGED','UPSTREAM_NETWORK'):
        store.execute("""UPDATE run_job SET detail_outcome='pending', last_error=NULL
                   WHERE run_id=? AND detail_outcome='failed' AND last_error LIKE ?""",
                (run_id,run['stop_reason']+':%'))
    _grant_locator_refresh(run_id)
    _set_state(run_id, "collecting", None)
    return get_run(run_id)


def run_status(run_id: str, cursor: Optional[str] = None, limit: int = 50) -> dict[str, Any]:
    run = get_run(run_id)
    pending_analysis = query_all(
        """SELECT rj.job_ref, j.title, j.company_name_raw, rj.detail_outcome
           FROM run_job rj JOIN job j ON j.id=rj.job_id
           WHERE rj.run_id=? AND rj.detail_outcome='complete'
             AND NOT EXISTS (SELECT 1 FROM analysis a WHERE a.snapshot_id=rj.snapshot_id)
           ORDER BY j.company_name_raw LIMIT ? OFFSET ?""",
        (run_id, limit, _cursor_offset(cursor)),
    )
    failures = query_all(
        """SELECT rj.job_ref, j.title, j.company_name_raw, rj.detail_outcome, rj.last_error
           FROM run_job rj JOIN job j ON j.id=rj.job_id
           WHERE rj.run_id=? AND rj.detail_outcome IN ('failed','missing','pending')
           ORDER BY j.company_name_raw LIMIT ? OFFSET ?""",
        (run_id, limit, _cursor_offset(cursor)),
    )
    observations = query_all(
        "SELECT * FROM search_observation WHERE run_id=? ORDER BY observed_at", (run_id,)
    )
    return {
        "run": run,
        "pending_analysis": pending_analysis,
        "failures": failures,
        "observations": [
            dict(observation, source_job_ids=jloads(observation["source_job_ids"], []))
            for observation in observations
        ],
        # 诊断页用：本次为刷新定位符**额外**用掉的搜索次数（独立计数，不占采集预算）
        "locator_refresh": {
            "used": locator_refresh_count(run_id),
            "limit": MAX_LOCATOR_REFRESH_CALLS,
            "left": locator_refresh_left(run_id),
        },
    }


def _cursor_offset(cursor: Optional[str]) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(cursor))
    except ValueError:
        return 0
