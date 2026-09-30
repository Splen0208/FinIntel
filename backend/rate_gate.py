"""应用层**持久化**频率闸门：把「少打扰平台」从上游进程内的节流，升级为跨进程、跨重启的账号级护栏。

为什么需要它：上游 CLI 的 ``api/throttle.py`` 是**进程内**的高斯延时 + 突发惩罚——但本项目
每次搜索都是**新起一个 CLI 进程**，进程内的 ``_last_request_time`` / ``_recent_times`` 每次都从零开始，
节流形同虚设。真正能约束「同一账号短时间反复访问」的，只能是**应用层**、**落库**、**跨重启仍在**的闸门。

闸门规则（对齐上游 ``config.py`` 的 crawl 默认与 ``throttle.py`` 的阈值）：

1. **账号级串行**：同一时刻只允许一个采集任务持有采集租约（``lease_run_id``，带 TTL）；
2. **列表请求最小间隔 ≥ 60s**：两次 list（search）请求之间至少间隔 60 秒；
3. **窗口上限**：任意 10 分钟内 list 请求 ≤ 20 次（对应上游 ``crawl.max_requests=20 / max_seconds=600``）；
4. **风险码硬冷却**：上游回 ``ENVIRONMENT_RISK`` / ``ACCOUNT_RISK`` 时**立即终止**并进入 30 分钟硬冷却；
   冷却期内**任何**路径（自动或人工）都不得发起请求；冷却结束后**只允许一次**用户显式人工请求。

状态全部落在单行表 ``rate_gate``（migration 015），重启后依然生效。所有判断都必须**如实告诉用户
为什么在等、大概还要等多久**——绝不无声地卡住或静默重试。
"""
from __future__ import annotations

import time
from typing import Any, Optional

# ── 规则常量（与上游 config.py / throttle.py 对齐）────────────────────────────
MIN_LIST_INTERVAL_SECONDS = 60.0     # 两次列表请求最小间隔
WINDOW_SECONDS = 600.0               # 窗口长度：10 分钟
MAX_REQUESTS_PER_WINDOW = 20         # 10 分钟内列表请求上限
COOLDOWN_SECONDS = 1800.0            # 风险码硬冷却：30 分钟（30–60 分钟区间内）
LEASE_TTL_SECONDS = 900.0            # 采集租约 TTL：worker 异常退出也能自动过期

# ── 拒绝原因码（= 抛给任务的 RunError.code，前端据此如实分流）────────────────
RATE_COOLDOWN = "RATE_COOLDOWN"
RATE_MANUAL_REQUIRED = "RATE_MANUAL_REQUIRED"
RATE_BUSY = "RATE_BUSY"
RATE_MIN_INTERVAL = "RATE_MIN_INTERVAL"
RATE_WINDOW_LIMIT = "RATE_WINDOW_LIMIT"

GATE_REASONS = frozenset({
    RATE_COOLDOWN, RATE_MANUAL_REQUIRED, RATE_BUSY, RATE_MIN_INTERVAL, RATE_WINDOW_LIMIT,
})

# 会触发硬冷却的上游风险码
RISK_CODES = frozenset({"ENVIRONMENT_RISK", "ACCOUNT_RISK"})

_DEFAULTS: dict[str, Any] = {
    "window_start": None,
    "window_count": 0,
    "last_list_at": None,
    "cooldown_until": None,
    "cooldown_reason": None,
    "manual_required": 0,
    "manual_credits": 0,
    "lease_run_id": None,
    "lease_until": None,
}


def _now() -> float:
    """可注入时钟：测试用 monkeypatch 推进时间，不用真的 sleep。"""
    return time.time()


def is_risk_code(code: Optional[str]) -> bool:
    return (code or "") in RISK_CODES


# ── 持久化 ────────────────────────────────────────────────────────────────

def _load() -> dict[str, Any]:
    import store

    row = store.query_one("SELECT * FROM rate_gate WHERE id=1")
    state = dict(_DEFAULTS)
    if row:
        for key in _DEFAULTS:
            if key in row.keys() and row[key] is not None:
                state[key] = row[key]
    return state


def _save(state: dict[str, Any]) -> None:
    import store

    connection = store.get_conn()
    connection.execute(
        """INSERT INTO rate_gate
               (id, window_start, window_count, last_list_at, cooldown_until, cooldown_reason,
                manual_required, manual_credits, lease_run_id, lease_until, updated_at)
           VALUES (1,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
                window_start=excluded.window_start,
                window_count=excluded.window_count,
                last_list_at=excluded.last_list_at,
                cooldown_until=excluded.cooldown_until,
                cooldown_reason=excluded.cooldown_reason,
                manual_required=excluded.manual_required,
                manual_credits=excluded.manual_credits,
                lease_run_id=excluded.lease_run_id,
                lease_until=excluded.lease_until,
                updated_at=excluded.updated_at""",
        (state["window_start"], state["window_count"], state["last_list_at"],
         state["cooldown_until"], state["cooldown_reason"], state["manual_required"],
         state["manual_credits"], state["lease_run_id"], state["lease_until"],
         store.utcnow_iso()),
    )
    connection.commit()


# ── 决策 ──────────────────────────────────────────────────────────────────

def _message(reason: str, wait: int) -> str:
    if reason == RATE_COOLDOWN:
        minutes = max(1, int(round(wait / 60)))
        return (f"账号/环境风控冷却中：本地已暂停一切自动访问，约 {minutes} 分钟后才允许"
                f"一次由你手动发起的采集（重新搜索，或点「获取最新结果」）。")
    if reason == RATE_MANUAL_REQUIRED:
        return ("风控冷却已结束；自动继续已暂停，请由你手动发起一次采集"
                "（重新搜索，或点「获取最新结果」）。")
    if reason == RATE_BUSY:
        return "已有另一个采集任务正在进行（同一账号同一时刻只跑一个采集），请等它结束后再继续。"
    if reason == RATE_MIN_INTERVAL:
        return (f"为减少对平台的访问，两次列表请求至少间隔 {int(MIN_LIST_INTERVAL_SECONDS)} 秒；"
                f"约 {max(1, int(wait))} 秒后可继续。")
    if reason == RATE_WINDOW_LIMIT:
        minutes = max(1, int(round(wait / 60)))
        return (f"10 分钟内列表请求已达上限 {MAX_REQUESTS_PER_WINDOW} 次；"
                f"约 {minutes} 分钟后恢复。")
    return "本地频率保护暂停了本次采集，请稍后再试。"


def _decision(allowed: bool, reason: str, wait: int) -> dict[str, Any]:
    return {
        "allowed": allowed,
        "code": reason,
        "wait_seconds": max(0, int(wait)),
        "message": "" if allowed else _message(reason, wait),
        "operator_action": "" if allowed else "稍后由你手动发起一次采集（重新搜索，或点「获取最新结果」）",
    }


def check_list_request(run_id: str, *, manual: bool = False,
                       now: Optional[float] = None) -> dict[str, Any]:
    """判定此刻能不能发一次 list（search）请求。

    ``manual=True`` 专指**用户显式**动作（「获取最新结果」）。冷却期内的任何请求都被拒绝；
    冷却结束后的自动路径被 ``RATE_MANUAL_REQUIRED`` 拒绝，只有 manual 路径放行一次。
    """
    moment = _now() if now is None else now
    state = _load()
    cooldown_until = state["cooldown_until"]
    if cooldown_until and moment < cooldown_until:
        return _decision(False, RATE_COOLDOWN, round(cooldown_until - moment))
    if state["manual_required"] and not manual:
        return _decision(False, RATE_MANUAL_REQUIRED, 0)
    lease_run = state["lease_run_id"]
    lease_until = state["lease_until"] or 0
    if lease_run and lease_run != run_id and moment < lease_until:
        return _decision(False, RATE_BUSY, round(lease_until - moment))
    last_list = state["last_list_at"]
    if last_list is not None:
        remaining = MIN_LIST_INTERVAL_SECONDS - (moment - last_list)
        if remaining > 0:
            return _decision(False, RATE_MIN_INTERVAL, round(remaining))
    window_start = state["window_start"]
    if (int(state["window_count"]) >= MAX_REQUESTS_PER_WINDOW
            and window_start is not None and moment - window_start < WINDOW_SECONDS):
        return _decision(False, RATE_WINDOW_LIMIT, round(WINDOW_SECONDS - (moment - window_start)))
    return _decision(True, "", 0)


def record_list_request(run_id: str, now: Optional[float] = None) -> None:
    """登记「刚发出一次 list 请求」。保守起见：**失败也算**（宁可少发，不发多）。"""
    moment = _now() if now is None else now
    state = _load()
    if state["window_start"] is None or moment - state["window_start"] >= WINDOW_SECONDS:
        state["window_start"] = moment
        state["window_count"] = 0
    state["window_count"] = int(state["window_count"]) + 1
    state["last_list_at"] = moment
    if state["manual_required"]:
        # 人工请求已发：销掉一次性人工额度，恢复正常闸门
        state["manual_required"] = 0
        state["manual_credits"] = max(0, int(state["manual_credits"]) - 1)
    _save(state)


def note_risk_stop(code: str, now: Optional[float] = None) -> None:
    """上游回风险码：立即进入硬冷却，并只留**一次**人工请求额度。"""
    moment = _now() if now is None else now
    state = _load()
    state["cooldown_until"] = moment + COOLDOWN_SECONDS
    state["cooldown_reason"] = code
    state["manual_required"] = 1
    state["manual_credits"] = 1
    state["lease_run_id"] = None
    state["lease_until"] = None
    _save(state)


# ── 账号级串行（采集租约）──────────────────────────────────────────────────

def begin_collection(run_id: str, now: Optional[float] = None) -> tuple[bool, int]:
    """尝试取得采集租约。已有别人持有且未过期 → 拒绝并返回大约还要等多久（秒）。"""
    moment = _now() if now is None else now
    state = _load()
    lease_run = state["lease_run_id"]
    lease_until = state["lease_until"] or 0
    if lease_run and lease_run != run_id and moment < lease_until:
        return False, max(1, round(lease_until - moment))
    state["lease_run_id"] = run_id
    state["lease_until"] = moment + LEASE_TTL_SECONDS
    _save(state)
    return True, 0


def end_collection(run_id: str, now: Optional[float] = None) -> None:
    """释放采集租约（幂等：不是自己持有的租约不动）。"""
    state = _load()
    if state["lease_run_id"] == run_id:
        state["lease_run_id"] = None
        state["lease_until"] = None
        _save(state)


# ── 供视图层如实告知「还要等多久」──────────────────────────────────────────

def remaining_wait_seconds(now: Optional[float] = None) -> int:
    """当前还剩多久才可能放行（秒）；无任何限制时返回 0。"""
    moment = _now() if now is None else now
    state = _load()
    waits: list[float] = []
    cooldown_until = state["cooldown_until"]
    if cooldown_until and moment < cooldown_until:
        waits.append(cooldown_until - moment)
    if state["manual_required"]:
        waits.append(COOLDOWN_SECONDS)   # 未知：给一个保守上限，提示去找「获取最新结果」
    last_list = state["last_list_at"]
    if last_list is not None:
        remaining = MIN_LIST_INTERVAL_SECONDS - (moment - last_list)
        if remaining > 0:
            waits.append(remaining)
    window_start = state["window_start"]
    if (int(state["window_count"]) >= MAX_REQUESTS_PER_WINDOW
            and window_start is not None and moment - window_start < WINDOW_SECONDS):
        waits.append(WINDOW_SECONDS - (moment - window_start))
    return int(round(max(waits))) if waits else 0


def snapshot(now: Optional[float] = None) -> dict[str, Any]:
    """当前闸门快照（诊断 / 测试用）。"""
    return {"state": _load(), "remaining_wait_seconds": remaining_wait_seconds(now)}


def reset() -> None:
    """清空闸门状态（运维/测试用；绝不用于常规采集路径）。"""
    _save(dict(_DEFAULTS))
