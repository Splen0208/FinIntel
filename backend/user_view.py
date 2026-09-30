"""状态 → 普通用户文案 / 可用操作映射（设计 D8 与 §6）。

普通 UI **只**消费 ``user_stage`` / ``user_message`` / ``actions`` / ``can``；
``AUTH_REQUIRED``、``AI_NOT_CONFIGURED``、``MCP_PROTOCOL_ERROR`` 等工程码只进技术诊断页，
不删除错误处理，只是不上普通页面（需求 §7）。

``user_view(stage, state, error_code, counters)`` 同时服务两类组件：

- **登录组件**（§6 前 8 态）：``stage`` = ``login_task.status``，``state`` = boss 连接态
  （``connected`` / ``connecting`` / ``disconnected``）。
- **任务组件**（§6 第 9 态与需求 §7 进度文案）：``stage`` = ``research_run.stage``，
  ``state`` = ``research_run.state``（后者取值落在 ``schemas.RUN_STATE_LABELS`` 内，
  这是两类调用唯一的判别依据）。
"""
from __future__ import annotations

import schemas

# ---------------------------------------------------------------------------
# 文案（逐条对齐设计 §6 表 / 需求 §7 表）
# ---------------------------------------------------------------------------

LOGIN_MESSAGES = {
    "disconnected": "登录 BOSS 后即可搜索真实岗位",
    "starting": "正在打开 BOSS 登录页面",
    "waiting_user": "请在打开的 BOSS 页面完成登录，并保留此窗口供搜索使用",
    "verifying": "登录已提交，正在确认连接",
    "connected": "BOSS 已连接",
    # 已连接但凭证不完整：照常允许采集，只如实提示「可能撑不到长时间任务」并给可行动作
    "connected_degraded": "BOSS 已连接，但登录信息不完整，长时间搜索可能中断，建议重新登录一次",
    "timeout_or_cancelled": "本次登录未完成，关键词已保留",
    "platform_verification": "BOSS 需要你完成验证",
    "env_not_ready": "搜索服务尚未准备好",
    "auth_expired": "BOSS 登录已失效，已保留当前进度",
}

EMPTY_RESULT_MESSAGE = "没有找到匹配岗位，保留搜索词"
CANCELLED_MESSAGE = "已停止，保留已获取内容"
PAUSED_MESSAGE = "任务已暂停，可继续"
# 预算耗尽：只有「还能继续读详情」才给「继续」，否则「可继续」是误导
BUDGET_STOPPED_MESSAGE = "本轮采集预算已用完，已保留已获取的岗位"
BUDGET_STOPPED_DETAIL_PENDING_MESSAGE = "本轮搜索预算已用完，岗位已保留，可继续读取详情"
# 定位符过期（30 分钟 TTL）且刷新额度已用尽：只能重新搜索，不能再给「继续」
STALE_LOCATOR_MESSAGE = "本批岗位的定位信息已过期（约 30 分钟有效），无法继续读取详情，请重新搜索。"
# 同样过期，但显式恢复的一次性刷新额度还在：此时「继续」是**真能成功**的
STALE_LOCATOR_RESUMABLE_MESSAGE = (
    "本批岗位的定位信息已过期（约 30 分钟有效）；点「继续」会自动刷新定位后继续读取详情。")
# result_view 的 empty_reason：定位符过期时给**事实描述**，绝不套「未发现方向」这类
# 结论性文案（用户指令 §4.1：不许把「详情没取到」说成「没有找到」）。
STALE_DETAILS_PENDING_MESSAGE = "这批岗位的详情尚未取得，无法判断业务方向"
PENDING_LOGIN_MESSAGE = "已保留本次搜索，登录 BOSS 后将自动继续"
AI_FAILED_MESSAGE = "岗位已保存，暂未完成整理"
NETWORK_FAILED_MESSAGE = "当前连接失败，进度已保留"
PLATFORM_PAUSED_MESSAGE = "BOSS 需要你完成验证，搜索已暂停"
# 「访问环境被平台拦截」是**同一件事实**：顶栏 / 任务页 / 结果页必须讲同一句核心话。
# 三处共用 ENVIRONMENT_BLOCKED_COPY（前端 simple.js 亦逐字持有同一句），禁止各写一份——
# 本项目反复出现过「同一事实多份实现」的缺陷，这条是刻意收口。
ENVIRONMENT_BLOCKED_COPY = "BOSS 拦截了当前访问环境"
ENVIRONMENT_PAUSED_MESSAGE = f"登录已成功，但 {ENVIRONMENT_BLOCKED_COPY}，搜索已暂停"
ENVIRONMENT_TOPBAR_MESSAGE = f"登录已成功，但 {ENVIRONMENT_BLOCKED_COPY}"
# 缺码的风控（state=risk_stopped 但未带具体码）：**绝不**冒充某个具体风险码（如账号风控），
# 只给中性表述 + 「去官方页面确认」的诚实指引。
RISK_PAUSED_MESSAGE = "平台已暂停本次搜索，请在 BOSS 官方页面确认访问恢复后再继续"

# 「这份结果来自上游本地缓存」的诚实标注前缀。缓存年龄由上游缓存行的 ``created_at`` 算出，
# 绝不猜、也不沉默地端上旧数据（用户指令：不许说谎）。前端 simple.js 亦逐字持有同一前缀。
CACHE_SERVED_PREFIX = "结果来自本地缓存"


def cache_served_message(age_seconds: int) -> str:
    """把缓存年龄渲染成一句人话：``结果来自本地缓存（X 分钟前）``。

    ≥1 小时按小时显示，否则按分钟（不足 1 分钟显示「刚刚」）。始终带括号里的相对时间，
    让用户一眼看出这不是刚抓回来的实时数据。
    """
    total = max(0, int(age_seconds))
    minutes = total // 60
    if minutes < 1:
        when = "刚刚"
    elif minutes < 60:
        when = f"{minutes} 分钟前"
    else:
        when = f"{minutes // 60} 小时前"
    return f"{CACHE_SERVED_PREFIX}（{when}）"

# 上游 `error.code` → 用户态（设计 §6 第 7/8 行）
PLATFORM_VERIFICATION_CODES = frozenset({
    "PLATFORM_VERIFICATION_REQUIRED", "LOGIN_RISK_CONTROL",
    "ENVIRONMENT_RISK", "ACCOUNT_RISK", "STOPPED_BY_SAFETY",
})
ENV_NOT_READY_CODES = frozenset({
    "DEPENDENCY_MISSING", "BROWSER_KERNEL_MISSING", "CDP_UNAVAILABLE",
    "LOGIN_CREDENTIAL_EXTRACTION_FAILED", "UPSTREAM_BROWSER_UNAVAILABLE",
})
AUTH_CODES = frozenset({"AUTH_REQUIRED", "AUTH_EXPIRED"})
AI_CODES = frozenset({"AI_NOT_CONFIGURED", "AI_TIMEOUT", "AI_OUTPUT_INVALID"})
NETWORK_CODES = frozenset({
    "UPSTREAM_NETWORK", "UPSTREAM_TIMEOUT", "UPSTREAM_PROCESS_FAILED",
    "RATE_LIMITED",
})
# 采集改走「用户自己的浏览器」后的两类失败（上游 CDP_UNAVAILABLE / BROWSER_SESSION_NOT_FOUND 的落点）
# 以及搜索前置检查判定的「通道未就绪」：一律只说「当前搜索连接」，
# **绝不**说成「你没有登录」——账号事实与通道事实是两回事（设计 §0.1 / §0.3）。
CHANNEL_NOT_READY_MESSAGE = "当前搜索未能连接到可用的 BOSS 浏览器会话，搜索已暂停，关键词已保留。"
ACCOUNT_OK_CHANNEL_NOT_READY = "账号登录已验证，当前搜索连接尚未就绪。"
# 账号已验证、但通道状态**尚未确认**（unknown）：如实说「正在确认」，不宣称已就绪。
# 与前端顶栏 ``connected_confirming`` 同一句，保证后端首页与顶栏口径一致。
ACCOUNT_OK_CHANNEL_CONFIRMING = "BOSS 已连接，正在确认搜索通道"
BROWSER_CHANNEL_MESSAGES = {
    "OWN_BROWSER_NOT_READY": CHANNEL_NOT_READY_MESSAGE,
    "OWN_BROWSER_NO_SESSION": CHANNEL_NOT_READY_MESSAGE,
    "CHANNEL_NOT_READY": CHANNEL_NOT_READY_MESSAGE,
    "CHANNEL_NOT_FROZEN": CHANNEL_NOT_READY_MESSAGE,
    "CHANNEL_CHANGED": CHANNEL_NOT_READY_MESSAGE,
}
BROWSER_CHANNEL_CODES = frozenset(BROWSER_CHANNEL_MESSAGES)

LOGIN_ONLY_STATUSES = frozenset({
    "starting", "waiting_user", "verifying", "connected", "connected_degraded", "timeout",
    "platform_verification", "env_not_ready",
})
TASK_STAGES = frozenset({
    "checking_env", "pending_login", "creating", "searching", "fetching_details",
    "deduping", "analyzing", "organizing", "completed",
    "paused", "cancelled", "failed", "partial",
})
TASK_ONLY_STAGES = TASK_STAGES - {"failed", "cancelled"}
RUN_STATES = frozenset(schemas.RUN_STATE_LABELS)

# ---------------------------------------------------------------------------
# 可用操作
# ---------------------------------------------------------------------------

A_LOGIN = {"id": "login", "label": "登录 BOSS"}
A_CANCEL = {"id": "cancel", "label": "取消"}
A_REOPEN = {"id": "reopen", "label": "重新打开登录页面"}
A_WAIT = {"id": "wait", "label": "等待"}
A_SEARCH = {"id": "search", "label": "开始搜索"}
A_RELOGIN = {"id": "login", "label": "重新登录"}
A_OPEN_VERIFY = {"id": "open_verify", "label": "打开官方验证页面"}
A_REPAIR = {"id": "open_diag", "label": "查看修复说明"}
# 环境风控/缺码风控的诚实动作：在**本项目的专用浏览器**里打开 BOSS 官方页面人工确认。
# 必须开在专用 profile（被标记的就是那个会话）——前端映射为 POST /api/channel/open-official
# （纯 CDP 浏览器动作，**不触发重新登录**，也不发任何业务请求）。
A_CHECK_OFFICIAL = {"id": "check_official", "label": "在本项目的专用浏览器中打开 BOSS 官方页面"}
# 同一个 open_diag 动作，但给「浏览器通道」一个更具体的按钮文案
A_BROWSER_GUIDE = {"id": "open_diag", "label": "查看连接步骤"}
A_LOGIN_AND_RESUME = {"id": "login_and_resume", "label": "重新登录并继续原任务"}
# 详情阶段认证失败专用：搜索走浏览器会话、详情走**独立的 HTTP 通道**（用本地凭证），
# 所以「列表能搜到」不代表详情可读。这里必须与「搜索阶段需要登录」区分开，
# 否则用户会被反复引导重新登录（而 CDP 登录常只提取到部分凭证，重登往往修不好）。
A_RELOGIN_TRY_FIX = {"id": "login", "label": "重新登录（尝试修复）"}
A_RESUME = {"id": "resume", "label": "继续"}
A_SEARCH_AGAIN = {"id": "search_again", "label": "重新搜索"}
A_RETRY_ORGANIZE = {"id": "retry_organize", "label": "重试整理"}
A_VIEW_RESULT = {"id": "view_result", "label": "查看已完成部分"}
# 结果页唯一的「越过缓存重新联网」出口：点了才会带 --no-cache 真正获取最新列表
A_REFRESH = {"id": "refresh", "label": "获取最新结果"}

# ---------------------------------------------------------------------------
# 失败分类：前端空态的唯一依据（设计 §2.6）
# ---------------------------------------------------------------------------

# 通道连接类：一律给「连接恢复」动作，**不得**出现「换个关键词再搜」
FAILURE_CHANNEL_CODES = frozenset({
    "OWN_BROWSER_NOT_READY", "OWN_BROWSER_NO_SESSION", "CHANNEL_NOT_READY",
    "CHANNEL_NOT_FROZEN", "CHANNEL_CHANGED", "CHANNEL_PRECHECK_FAILED",
    "CDP_UNAVAILABLE", "CDP_URL_REQUIRED", "BROWSER_KERNEL_MISSING",
    "UPSTREAM_BROWSER_UNAVAILABLE", "UPSTREAM_TIMEOUT", "UPSTREAM_NETWORK",
    "UPSTREAM_PROCESS_FAILED", "DATA_DIR_MISMATCH",
}) | BROWSER_CHANNEL_CODES
# 账号认证类：重新登录并继续原任务
FAILURE_AUTH_CODES = frozenset(AUTH_CODES)
# 平台风控类：打开官方验证页面
FAILURE_RISK_CODES = frozenset(PLATFORM_VERIFICATION_CODES | {"ACCOUNT_RISK"})
# 定位符过期类（30 分钟 TTL）且刷新额度已用尽：只能重新搜索，**不是**「继续」。
# 注意：**不**包含上游的 ``UPSTREAM_RESULT_LIMIT``（那是「搜索范围过大」，
# 补救方式是缩小范围，与定位符过期是两回事，共用一个码会让文案说谎）。
FAILURE_STALE_CODES = frozenset({"STALE_LOCATOR"})
FAILURE_KINDS = frozenset({"channel", "auth", "risk", "partial", "empty", "none", "stale",
                           "gated"})

# 应用层频率闸门（二轮·C）拒绝时抛出的码。与 ``rate_gate`` 的原因码逐字一致；
# 「为什么在等 / 大概还要等多久」由 ``rate_gate_message`` 如实给出，绝不无声卡住。
FAILURE_GATE_CODES = frozenset({
    "RATE_COOLDOWN", "RATE_MANUAL_REQUIRED", "RATE_BUSY",
    "RATE_MIN_INTERVAL", "RATE_WINDOW_LIMIT",
})


def rate_gate_message(wait_seconds: Optional[int] = None) -> str:
    """频率闸门暂停采集时的**如实**说明：为什么在等 + 建议等多久。"""
    if wait_seconds and wait_seconds > 0:
        minutes = max(1, int(round(wait_seconds / 60)))
        return (f"本地频率保护已暂停本次采集：为减少对平台的访问，建议约 {minutes} 分钟后"
                f"再重试（重新搜索，或点「获取最新结果」）。")
    return "本地频率保护已暂停本次采集：请稍后再重试（重新搜索，或点「获取最新结果」）。"


def rate_gate_view(wait_seconds: Optional[int] = None) -> dict:
    """闸门暂停的任务视图：只给「获取最新结果」（自动继续已被闸门挡住，给了也是假按钮）。"""
    return _view("paused", rate_gate_message(wait_seconds), [A_REFRESH])


def stale_locator_view(*, can_refresh: bool) -> dict:
    """定位符过期时的用户视图：能不能继续，由「刷新额度」说了算，绝不猜。

    - ``can_refresh`` 为真 → 给「继续」（点了真的会刷新定位并继续读取详情）；
    - 为假 → 只给「重新搜索」与「查看已完成部分」，**不给**点了一定失败的假按钮。
    """
    if can_refresh:
        return _view("paused", STALE_LOCATOR_RESUMABLE_MESSAGE, [A_RESUME, A_VIEW_RESULT])
    return _view("paused", STALE_LOCATOR_MESSAGE, [A_SEARCH_AGAIN, A_VIEW_RESULT])


def detail_auth_message(collected: int) -> str:
    """详情阶段认证失败的诚实文案（**同一条通道**口径）。

    详情读取走 ``boss_browser_worker``：它与搜索用的是**同一份冻结通道**、同一个
    已登录的浏览器会话（不再使用独立 HTTP 通道 + 本地保存的凭证）。因此「搜索成功、
    详情认证失败」的真实含义是：**这条浏览器会话在读取详情时登录态已不可用**
    （会话过期 / 页面被踢出 / 当前页面已不在 BOSS 直聘），而不是另一套凭据失效。

    重登后能不能接着读，取决于详情定位符是否还在有效期内（securityId/lid 有 TTL），
    所以这里如实说明「重登后可以重试，但不保证恢复」，既不给假保证，也不误导归因。
    """
    return (
        f"已搜索到 {max(0, int(collected))} 个岗位，但读取详情失败："
        "详情与搜索用的是同一个浏览器会话，但读取详情时该会话的登录态已不可用；"
        "重新登录后可以重试，但详情定位信息可能已过期，不保证恢复。"
    )


def detail_auth_view(collected: int) -> dict:
    """详情阶段认证失败的用户视图（任务页与结果页共用同一句文案）。"""
    return _view("paused", detail_auth_message(collected), [A_RELOGIN_TRY_FIX, A_VIEW_RESULT])


def failure_kind(
    stage: Optional[str] = None,
    state: Optional[str] = None,
    error_code: Optional[str] = None,
    counters: Optional[dict] = None,
    *,
    last_search_ok: Optional[bool] = None,
    detail_phase: bool = False,
) -> str:
    """判定这次「没有结果」到底属于哪一类（前端按它分流空态）。

    ``last_search_ok`` 是**唯一**能区分「真实成功返回的空列表」与「调用失败」的证据：

    - ``True``  = 最后一次 search 信封 ok（真实成功，可能确实是空列表）；
    - ``False`` = search 调用失败（无论什么原因）；
    - ``None``  = 没有 search 记录，不下结论。

    铁律：只有 ``True`` 才允许判 ``empty``。``False`` / ``None`` 一律不得渲染成
    「没有找到匹配岗位 / 换个关键词再搜」。
    """
    code = (error_code or "").strip().upper()
    stage = (stage or "").strip()
    state = (state or "").strip()

    if code in FAILURE_CHANNEL_CODES:
        return "channel"
    if code in FAILURE_STALE_CODES:
        return "stale"
    # 认证失败的**阶段**决定该说什么：搜索阶段失败 = 确实需要登录；详情阶段失败 = 搜索
    # 已经在同一个浏览器会话里成功了，坏的是**这条会话在读取详情时的登录态**。
    # 两者必须分开，否则会把用户反复引向「重新登录」。
    # 口径更正：详情已改走**浏览器 worker**（与搜索同一份冻结通道），不再使用
    # 「独立的 HTTP 通道 + 本地保存凭证」，旧解释会给出错误归因。
    if (code in FAILURE_AUTH_CODES or state == "auth_required") and detail_phase:
        return "detail_auth"
    if code in FAILURE_AUTH_CODES or state == "auth_required":
        return "auth"
    if code in FAILURE_GATE_CODES:
        return "gated"
    if code in FAILURE_RISK_CODES or state == "risk_stopped":
        return "risk"

    counts = _counters_view(counters)
    if stage == "partial" or (stage == "completed" and counts["failed"] > 0):
        return "partial"
    if stage == "completed" and counts["collected"] == 0:
        return "empty" if last_search_ok is True else "none"
    return "none"


def channel_not_ready_view() -> dict:
    """账号已验证、但当前搜索通道未就绪：顶栏/任务页的统一文案与动作。"""
    return _view("env_not_ready", ACCOUNT_OK_CHANNEL_NOT_READY, [A_RESUME, A_BROWSER_GUIDE])


def environment_blocked_view() -> dict:
    """账号已验证、但当前搜索**因访问环境被平台拦截**而未就绪：顶栏/登录块用。

    与任务页 (``ENVIRONMENT_PAUSED_MESSAGE``) / 结果页讲同一句话（``ENVIRONMENT_BLOCKED_COPY``）。
    动作只给「打开 BOSS 官方页面」——环境风控**不得**触发重新登录（上游明确要求）。
    """
    return _view("env_not_ready", ENVIRONMENT_TOPBAR_MESSAGE, [A_CHECK_OFFICIAL])


def channel_confirming_view() -> dict:
    """账号已验证、但通道状态**尚未确认**（``unknown``）：如实说「正在确认」。

    ``unknown`` 只表示「后台允许放行**一次**探测」，**不等于**「已经就绪」。顶栏的绿点
    与这句文案都必须照此：不宣称已就绪，也不误说成「没有登录 / 连接未就绪」。
    """
    return _view("connected", ACCOUNT_OK_CHANNEL_CONFIRMING, [A_SEARCH])


def _can(actions: list[dict]) -> dict[str, bool]:
    ids = {action["id"] for action in actions}
    return {
        "login": bool({"login", "login_and_resume"} & ids),
        "cancel": "cancel" in ids,
        "reopen": "reopen" in ids,
        "search": "search" in ids,
        "resume": bool({"resume", "login_and_resume"} & ids),
        "retry_organize": "retry_organize" in ids,
        "open_diag": "open_diag" in ids,
        "open_verify": "open_verify" in ids,
        "check_official": "check_official" in ids,
        "refresh": "refresh" in ids,
    }


def _view(user_stage: str, user_message: str, actions: list[dict]) -> dict:
    return {"user_stage": user_stage, "user_message": user_message,
            "actions": actions, "can": _can(actions)}


def _counters_view(counters: dict | None) -> dict[str, int]:
    counters = dict(counters or {})

    def pick(*keys: str) -> int:
        for key in keys:
            value = counters.get(key)
            if isinstance(value, int) and value > 0:
                return value
        for key in keys:
            value = counters.get(key)
            if isinstance(value, int):
                return value
        return 0

    return {
        "collected": pick("collected", "jobs_unique"),
        "organized": pick("organized", "organized_count"),
        "failed": pick("failed", "failed_count"),
        "detail_read": pick("detail_read", "snapshots_complete", "detail_calls"),
    }


# ---------------------------------------------------------------------------
# 登录侧（设计 §6 第 1~8 行）
# ---------------------------------------------------------------------------

def _login_view(stage: str, state: str, code: str) -> dict:
    if code == "ENVIRONMENT_RISK":
        # 环境风控优先于通用「平台验证」分支：**绝不**给 sfStartLogin 触发的 open_verify
        # （那会重新登录，与文案矛盾）。统一走 environment_blocked_view 的诚实出口。
        return environment_blocked_view()
    if code in PLATFORM_VERIFICATION_CODES:
        return _view("platform_verification", LOGIN_MESSAGES["platform_verification"],
                     [A_OPEN_VERIFY, A_CANCEL])
    if code in BROWSER_CHANNEL_CODES:
        return _view("env_not_ready", BROWSER_CHANNEL_MESSAGES[code], [A_BROWSER_GUIDE])
    if code in ENV_NOT_READY_CODES:
        return _view("env_not_ready", LOGIN_MESSAGES["env_not_ready"], [A_REPAIR])
    if stage == "connected":
        return _view("connected", LOGIN_MESSAGES["connected"], [A_SEARCH])
    if stage == "connected_degraded":
        # 仍是「已连接」：搜索照常可用；多给一个重新登录的出口让用户补齐凭证
        return _view("connected", LOGIN_MESSAGES["connected_degraded"], [A_SEARCH, A_RELOGIN])
    if stage == "starting":
        return _view("starting", LOGIN_MESSAGES["starting"], [A_CANCEL])
    if stage == "waiting_user":
        return _view("waiting_user", LOGIN_MESSAGES["waiting_user"], [A_REOPEN, A_CANCEL])
    if stage == "verifying":
        return _view("verifying", LOGIN_MESSAGES["verifying"], [A_WAIT, A_CANCEL])
    if stage == "platform_verification":
        return _view("platform_verification", LOGIN_MESSAGES["platform_verification"],
                     [A_OPEN_VERIFY, A_CANCEL])
    if stage == "env_not_ready":
        return _view("env_not_ready", LOGIN_MESSAGES["env_not_ready"], [A_REPAIR])
    if stage in {"timeout", "cancelled", "failed"}:
        return _view("timeout_or_cancelled", LOGIN_MESSAGES["timeout_or_cancelled"], [A_RELOGIN])
    return _view("disconnected", LOGIN_MESSAGES["disconnected"], [A_LOGIN])


# ---------------------------------------------------------------------------
# 任务侧（设计 §6 第 9 行 + 需求 §7 进度文案）
# ---------------------------------------------------------------------------

def _task_view(stage: str, state: str, code: str, counters: dict | None) -> dict:
    counts = _counters_view(counters)
    collected, organized, failed = counts["collected"], counts["organized"], counts["failed"]

    if code in AUTH_CODES:
        return _view("auth_expired", LOGIN_MESSAGES["auth_expired"], [A_LOGIN_AND_RESUME])
    if code in FAILURE_GATE_CODES:
        # 本地频率闸门：自动路径已被挡住，只给诚实的等待说明 + 人工出口（见 rate_gate_view）
        return rate_gate_view()
    if code == "ENVIRONMENT_RISK":
        # 环境风控：**不触发重新登录**（上游要求「不刷新 Token、不重新登录、不自动重试」）。
        # 只给诚实的「打开 BOSS 官方页面人工确认」，与结果页同一句话。
        return _view("platform_verification", ENVIRONMENT_PAUSED_MESSAGE, [A_CHECK_OFFICIAL])
    if code in PLATFORM_VERIFICATION_CODES:
        return _view("platform_verification", PLATFORM_PAUSED_MESSAGE, [A_OPEN_VERIFY, A_RESUME])
    if code in BROWSER_CHANNEL_CODES:
        return _view("env_not_ready", BROWSER_CHANNEL_MESSAGES[code], [A_RESUME, A_BROWSER_GUIDE])
    if code in ENV_NOT_READY_CODES:
        return _view("env_not_ready", LOGIN_MESSAGES["env_not_ready"], [A_REPAIR, A_RESUME])
    if code in FAILURE_STALE_CODES:
        # 定位符过期且刷新额度已用尽：**不给「继续」**（点了必然同样失败），
        # 只给「重新搜索」（关键词保留）与「查看已完成部分」。
        return _view("paused", STALE_LOCATOR_MESSAGE, [A_SEARCH_AGAIN, A_VIEW_RESULT])
    if code in AI_CODES or stage == "failed":
        return _view("failed", AI_FAILED_MESSAGE, [A_RETRY_ORGANIZE, A_VIEW_RESULT])
    if code in NETWORK_CODES or state == "rate_limited":
        return _view("paused", NETWORK_FAILED_MESSAGE, [A_RESUME])

    # 预算耗尽：搜索/详情是两个独立预算，得说得清「还能不能继续」
    if state == "budget_stopped":
        if counts["detail_read"] < counts["collected"]:
            return _view("paused", BUDGET_STOPPED_DETAIL_PENDING_MESSAGE,
                         [A_RESUME, A_VIEW_RESULT])
        return _view("paused", BUDGET_STOPPED_MESSAGE, [A_VIEW_RESULT])

    if stage == "cancelled":
        return _view("cancelled", CANCELLED_MESSAGE, [A_VIEW_RESULT])
    if state == "risk_stopped":
        # 风控停止：``state`` 是错误落点的权威镜像（``runs._state_for_error`` 同步写
        # ``stop_reason``）。这一条**必须**先于通用 ``stage == "paused"`` 判定：
        # 否则「任务已暂停，可继续」会盖掉风控事实，还给出点了会再次撞风控的「继续」假按钮。
        # 具体风控码（环境/账号等）已在上面的码分支处理，走到这里即**缺码**风控——
        # 只给中性表述 + 诚实指引，绝不冒充某个具体风险码。
        return _view("platform_verification", RISK_PAUSED_MESSAGE, [A_CHECK_OFFICIAL])
    if stage == "paused":
        return _view("paused", PAUSED_MESSAGE, [A_RESUME])
    if stage == "pending_login":
        return _view("pending_login", PENDING_LOGIN_MESSAGE, [A_LOGIN])
    if stage == "checking_env" or stage == "creating":
        return _view("creating", "正在准备搜索", [A_CANCEL])
    if stage == "searching":
        return _view("searching", "正在搜索岗位", [A_CANCEL])
    if stage == "fetching_details":
        return _view("fetching_details",
                     f"正在读取岗位详情，已读取 {counts['detail_read']} 个", [A_CANCEL])
    if stage == "deduping":
        return _view("deduping", "正在去重并保留原文", [A_CANCEL])
    if stage in {"analyzing", "organizing"}:
        return _view(stage, f"正在整理公司方向，已完成 {organized} 个", [A_CANCEL])
    if stage == "partial":
        return _view("partial", f"已整理 {organized} 个，{failed} 个未成功；结论仅覆盖成功部分",
                     [A_VIEW_RESULT, A_RETRY_ORGANIZE])
    if stage == "completed":
        if collected == 0:
            return _view("empty", EMPTY_RESULT_MESSAGE, [A_SEARCH])
        if failed > 0:
            return _view("partial", f"已整理 {organized} 个，{failed} 个未成功；结论仅覆盖成功部分",
                         [A_VIEW_RESULT, A_RETRY_ORGANIZE])
        return _view("completed", "整理完成，直接展示结果", [A_VIEW_RESULT])
    return _view(stage or "paused", PAUSED_MESSAGE, [A_RESUME])


# ---------------------------------------------------------------------------
# 唯一入口
# ---------------------------------------------------------------------------

def user_view(
    stage: str | None = None,
    state: str | None = None,
    error_code: str | None = None,
    counters: dict | None = None,
) -> dict:
    """把内部 stage/state/error_code 映射为普通用户可读文案与操作。"""
    stage = (stage or "").strip()
    state = (state or "").strip()
    code = (error_code or "").strip().upper()
    if not code:
        # run.state 本身就是错误落点的镜像（runs._state_for_error 同步写 stop_reason）
        if state == "auth_required":
            code = "AUTH_REQUIRED"
        # 注意：``state == "risk_stopped"`` 时**刻意不注入** ``ACCOUNT_RISK``。
        # 历史缺陷：这里曾把「缺码的风控」默认成账号风控，导致未带具体码时也冒充某个
        # 具体风险码。现在留空，由 ``_task_view`` 的 ``state == "risk_stopped"`` 分支
        # 给中性表述（``RISK_PAUSED_MESSAGE``）+ 诚实指引（打开 BOSS 官方页面）。
    if stage in LOGIN_ONLY_STATUSES:
        return _login_view(stage, state, code)
    if state in RUN_STATES or (not state and stage in TASK_ONLY_STAGES):
        return _task_view(stage, state, code, counters)
    return _login_view(stage, state, code)
