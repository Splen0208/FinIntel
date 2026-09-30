"""持久化编排器：一次点击把搜索→详情→去重→AI 抽取→方向整理全部跑完。

设计依据：`docs/simple-flow-design.md` §7（持久化编排）与 §8（结果物化）。

关键语义
--------
- **复用 `research_run`**，不新建第二套任务表；进度写 `stage`，断点写 `checkpoint`。
- worker 是服务端 daemon 线程：浏览器刷新不中断任务，轮询接口只读 DB。
- 服务重启 `recover_pending()` 只把未完成任务置 `paused`，**不自动重新采集**。
- 取消只在「上游调用之间」生效：不再发起新请求，但已取得数据全部保留。
- 内部上限（≤2 个搜索页、≤20 个去重岗位、详情串行）是后端常量，不进业务表单。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import ai_provider
import analysis
import channel_state
import dedupe
import login_bridge
import rate_gate
import runs as runs_mod
import schemas
import user_view
from store import get_conn, jdumps, jloads, new_id, query_all, query_one, utcnow_iso

SIMPLE_ORIGIN = "simple"
SIMPLE_SOURCE = "zhipin"
PENDING_INTENT_TTL_SECONDS = 30 * 60

MAX_SEARCH_PAGES = 2          # 内部上限：单次任务最多 2 个搜索页
MAX_JOBS = 20                 # 内部上限：最多整理 20 个去重岗位
DETAILS_PER_CALL = 3          # 详情串行
ANALYSIS_BATCH_SIZE = 5
CALL_TIMEOUT_SECONDS = 120
# 岗位类型（需求 §3：默认关注产品和研发岗位）。JOB_TYPE_ORDER 的顺序即展示顺序。
JOB_TYPE_ORDER: tuple[str, ...] = ("product", "engineering", "product_engineering", "all")
JOB_TYPES = frozenset(JOB_TYPE_ORDER)

# 读时展示口径（主理人裁定 = 方案 A）：
# job_type **只过滤结果视图的 directions[]**，不影响采集、不影响
# counters(collected/organized/failed)、不影响预算上限。理由是需求 §3 明确
# 「不能仅凭公司行业字段做硬过滤导致漏掉信贷岗位」——采集层过滤会真丢岗位。
# None 表示不按岗位类型过滤（all）。
# algorithm_data 的中文标签是「算法/数据研发」——它本身就是研发，故并入
# product_engineering（需求 §3「默认关注产品和研发岗位」，且警告过度过滤会漏岗位，
# 消费信贷的算法/风控建模岗正是核心求职方向）。只排除真正的非研发角色。
ROLE_CLASSES_BY_JOB_TYPE: dict[str, Optional[frozenset[str]]] = {
    "product": frozenset({"product"}),
    "engineering": frozenset({"engineering"}),
    "product_engineering": frozenset({"product", "engineering", "algorithm_data"}),
    "all": None,
}
DEFAULT_DISPLAY_JOB_TYPE = "product_engineering"
JOB_TYPE_APPLIES_TO = "display"

ACTIVE_RUN_STAGES = frozenset({
    "checking_env", "creating", "searching", "fetching_details",
    "deduping", "analyzing", "organizing",
})
TERMINAL_STAGES = frozenset({"completed", "cancelled", "failed", "partial"})
RESUMABLE_STAGES = frozenset({"paused", "auth_expired", "pending_login"})
# 「搜索已成功、进入详情及之后」的阶段集合（依据只增不减的 stage_reached 判定）。
# 用于区分**详情阶段**的失败与**搜索阶段**的失败。
# 通道口径（详情改走 browser worker 之后）：详情与搜索用的是**同一份冻结通道**
# （同一个已有浏览器会话），不再是「另一套本地 HTTP 凭据」。因此详情阶段的认证类
# 失败意味着**这条浏览器会话的登录态在读详情时不可用**——它和「搜索阶段压根没登录」
# 必须分开讲，但**不能再**解释成「详情的独立 HTTP 凭据失效」。
DETAIL_PHASES = frozenset({"fetching_details", "deduping", "analyzing", "organizing", "completed"})
RETRY_ORGANIZE_STAGES = frozenset({"paused", "failed", "partial", "analyzing", "organizing"})

# 阶段条的顺序（也是「真实到达点」的比较基准）
STAGE_ORDER: tuple[str, ...] = (
    "searching", "fetching_details", "deduping", "analyzing", "organizing", "completed",
)
STAGE_LABELS: dict[str, str] = {
    "searching": "搜索真实职位",
    "fetching_details": "读取职位详情",
    "deduping": "去重并保留原文",
    "analyzing": "提取有依据的工作事项",
    "organizing": "按公司和方向整理",
    "completed": "保存并显示结果",
}
# 终止态：真实到达点之前 done、到达点 failed、之后 skipped（**不再是全部 done**）
TERMINATED_STAGES = frozenset({"paused", "cancelled", "failed"})
# 「搜索连接未就绪」的落点码：走 user_view 的通道类文案
CHANNEL_NOT_READY_CODE = "CHANNEL_NOT_READY"

# run 级标记：本次恢复是否已经 re-arm 过通道（保证「同一次恢复只 re-arm 一次」）。
# 真机故障（run_d7207cd19ee74c4b）：同一次恢复里 re-arm 被触发了两次，第二次把本轮刚
# 写下的 ``not_ready / check_source=collect_failed`` 擦成 ``unknown``；而
# ``collect_gate()`` 对 auto+unknown 的规则是「放行一次探测」，于是**刚失败过的通道**
# 在顶栏又显示成绿的（绿点）——这正是要杜绝的「失败结论被探测许可覆盖」。
CHANNEL_REARM_MARK = "channel_rearmed_for_this_resume"


def _later_stage(current: Optional[str], candidate: str) -> Optional[str]:
    """取 ``STAGE_ORDER`` 里更靠后的那个阶段（到达点只增不减）。

    不在顺序表里的阶段（creating / paused / cancelled …）不推进到达点——
    它们不是「真实执行到的采集阶段」。
    """
    if candidate not in STAGE_ORDER:
        return current
    if current not in STAGE_ORDER:
        return candidate
    return current if STAGE_ORDER.index(current) >= STAGE_ORDER.index(candidate) else candidate


def _account_connected(status: Optional[dict]) -> bool:
    """账号事实：``connected`` / ``connected_degraded``（凭证不完整也算已连接）。"""
    import login_bridge

    return str((status or {}).get("state") or "") in login_bridge.CONNECTED_STATUSES


class PipelineError(Exception):
    """稳定的编排层错误（与 runs.RunError 同形）。"""

    def __init__(self, code: str, message: str, retryable: bool = False,
                 operator_action: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.operator_action = operator_action


class Pipeline:
    """持久化编排器（进程内单例使用）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}

    # ------------------------------------------------------------------
    # 对外：起始 / 查询 / 控制
    # ------------------------------------------------------------------

    def start_task(
        self,
        query: str,
        *,
        city: Optional[str] = None,
        job_type: Optional[str] = None,
        reuse_run_id: Optional[str] = None,
        source: str = SIMPLE_SOURCE,
        force_new: bool = False,
    ) -> dict:
        """发起一次「搜索并整理」。立即返回；真正采集在后台 worker 里跑。

        ``source`` 是**内部/离线演示入口**：HTTP 层固定 `'zhipin'`，正常流程不改数据来源。
        """
        text = (query or "").strip()
        if not text:
            raise PipelineError("INVALID_ANALYSIS", "请输入关键词后再搜索", False,
                                "关键词是唯一必填项，例如“消费信贷 产品经理”")
        if job_type is not None and job_type not in JOB_TYPES:
            raise PipelineError("INVALID_ANALYSIS", f"岗位类型 {job_type} 不受支持", False,
                                f"岗位类型取 {'/'.join(JOB_TYPE_ORDER)}")

        if reuse_run_id:
            try:
                run = runs_mod.get_run(reuse_run_id)
            except KeyError:
                raise PipelineError("INVALID_ANALYSIS", f"任务 {reuse_run_id} 不存在")
            # 显式继续既有任务：保留该 run 已存的展示口径（用户是在"继续"，不是重新设定筛选；
            # 展示口径仍可由 GET /api/tasks/{id}/result?job_type= 覆盖）。
            return self._search_payload(run["id"])

        existing = None if force_new else self._find_same_query_run(text)
        if existing:
            # 复用同一 run（job_type 不进复用键 → 改筛选不重复采集），
            # 但展示口径要刷成最新的，否则结果页仍按旧口径渲染。
            self._sync_display_scope(existing, job_type)
            # 同 query 的「待登录」意图：现在已连接就让它直接跑起来，
            # 不能因为复用了旧 run 又把已登录的会话退回 pending_login。
            self._resume_intent_if_connected(existing)
            return self._search_payload(existing["id"])

        self._assert_env_ready(source)

        name = f"{text} · {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        try:
            run = runs_mod.create_run(name, {
                "source": source,
                "queries": [text],
                "cities": [city] if city else [],
                "max_search_calls": MAX_SEARCH_PAGES,
                "max_detail_calls": MAX_JOBS,
                "details_per_call": DETAILS_PER_CALL,
                "analysis_batch_size": ANALYSIS_BATCH_SIZE,
                "call_timeout_seconds": CALL_TIMEOUT_SECONDS,
                # 用户从 /api/search 点「搜索并整理」= 显式动作；频率闸门据此把这类采集放行
                # （自动续跑/后台重试不带此标记，见 resume_task）。
                "user_initiated": True,
            })
        except runs_mod.RunError as exc:
            raise PipelineError(exc.code, exc.message, exc.retryable, exc.operator_action) from exc

        login_state = self._login_state()
        # 登录只在真实采集链路上是前置条件；离线演示入口（source='demo'，仅供开发/测试）
        # 不使用平台会话，不参与普通用户流程。
        # 判定用「采集门禁」而不是「账号曾验证成功」：
        #   collect_gate = 账号事实(connected/connected_degraded) + 当前采集通道是否就绪。
        #   两者合起来才是「能不能真的去采集」——顶栏与搜索前置检查共用同一个函数。
        account_ok = _account_connected(login_state)
        connected = self._login_collectable() or source != SIMPLE_SOURCE
        stage = "creating" if connected else "pending_login"
        checkpoint = {
            "intent": "search",
            "intent_at": utcnow_iso(),
            "pages_done": {},
            "detail_done_refs": [],
            "snapshot_ids_ready": [],
            "analyzed_snapshot_ids": [],
            "directions_built": False,
        }
        connection = get_conn()
        connection.execute(
            """UPDATE research_run SET origin=?, stage=?, stage_updated_at=?, query_text=?,
                 filters=?, checkpoint=?, user_message=? WHERE id=?""",
            (
                SIMPLE_ORIGIN, stage, utcnow_iso(), text,
                jdumps({"city": city, "job_type": job_type}),
                jdumps(checkpoint),
                user_view.PENDING_LOGIN_MESSAGE if not connected else "正在准备搜索",
                run["id"],
            ),
        )
        connection.commit()
        if connected:
            self._spawn_worker(run["id"], force_refresh=force_new)
        elif account_ok and source == SIMPLE_SOURCE:
            # 账号已验证、但**当前搜索通道未就绪**：如实报「连接未就绪」并给恢复动作，
            # 绝不退回 pending_login（那会误导成「去登录就好了」），
            # 也绝不放行采集（放行只会换来一次必然失败的搜索与误导文案）。
            self._settle_failure(run["id"], CHANNEL_NOT_READY_CODE)
        return self._search_payload(run["id"], login_state)

    def _search_payload(self, run_id: str, login_state: Optional[dict] = None) -> dict:
        """``POST /api/search`` 的统一返回形状：``task`` + 顶层 ``boss``。

        三条返回路径（首次创建 / 同 query 复用 / 显式 reuse_run_id）必须形状一致——
        否则前端无条件读 ``data.boss`` 时会在某些路径上拿到 undefined。
        """
        return {
            **self.task_view(run_id),
            # 同一份 boss 块（唯一实现）；调用方已取到的 status 直接复用
            "boss": login_bridge.boss_block(login_state),
        }

    def task_view(self, run_id: str) -> dict:
        """只读 DB 的任务视图，轮询可安全高频调用（绝不触发在线探测）。"""
        run = self._require_run(run_id)
        counters = run["counters"]
        collected = int(counters.get("jobs_unique", 0) or 0)
        organized = int(run.get("organized_count") or 0)
        failed = int(run.get("failed_count") or 0)
        accounting = self._accounting(run_id)
        detail_read = accounting["details_complete"] + accounting["details_partial"]
        mapped = user_view.user_view(
            run.get("stage"), run.get("state"), self._error_code(run),
            {"collected": collected, "organized": organized, "failed": failed,
             "detail_read": detail_read},
        )
        if self._error_code(run) in user_view.FAILURE_GATE_CODES:
            # 频率闸门：消息里的「还要等多久」按**当下**剩余时间算，别显示写死的旧数字
            mapped = {**mapped,
                      **user_view.rate_gate_view(rate_gate.remaining_wait_seconds())}
        stage = run.get("stage") or "creating"
        if stage == "searching" and (run.get("user_message") or "").startswith("正在等待搜索间隔"):
            mapped["user_message"] = run["user_message"]
        cancelled = bool(run.get("cancel_requested"))
        stage_reached = run.get("stage_reached")
        if self._error_code(run) == "ENVIRONMENT_RISK" and stage_reached == "fetching_details":
            mapped["user_message"] = f"已取得 {collected} 个岗位，但 BOSS 拦截了详情读取；已保留进度"
        # 详情阶段的认证失败：详情与搜索共用**同一条冻结通道**（同一个浏览器会话），
        # 所以这不是「另一套本地 HTTP 凭据坏了」，而是这条会话在读详情时登录态已不可用。
        # 仍必须与「搜索阶段压根没登录」分开讲：否则页面会一键导向「重新登录并继续」，
        # 而重登后详情定位符往往已过期，用户会在登录循环里打转。
        detail_auth = (self._error_code(run) in user_view.FAILURE_AUTH_CODES
                       and self._detail_phase(run) and not cancelled)
        if detail_auth:
            mapped = {**mapped, **user_view.detail_auth_view(collected)}
        # 定位符过期是**事实**（不是从 stop_reason 猜的）：还有待读岗位的定位符过期时，
        # 「能不能继续」由刷新额度决定，文案与按钮都照实说，绝不给假按钮。
        refresh_left = runs_mod.locator_refresh_left(run["id"])
        # 与 ``result_view`` 共用**同一个**事实判定 ``_stale_locator_state``：任务页与结果页
        # 必须讲同一个故事（旧实现各自看 stop_reason/码 → 同一任务两页两说）。
        in_stale_state = self._stale_locator_state(run, stage)
        if in_stale_state and not cancelled:
            mapped = {**mapped, **user_view.stale_locator_view(can_refresh=refresh_left)}
            can_resume = refresh_left
        else:
            can_resume = (stage in RESUMABLE_STAGES and not cancelled
                          and not detail_auth and not self._resume_blocked(run))
        if can_resume and self._error_code(run) == rate_gate.RATE_MIN_INTERVAL:
            mapped["actions"] = [{"id": "resume", "label": "继续搜索并整理"}]
        task = {
            "run_id": run["id"],
            "query": run.get("query_text") or self._query_of(run),
            "city": (jloads(run.get("filters"), {}) or {}).get("city"),
            "stage": stage,
            "stage_reached": stage_reached,
            "state": run["state"],
            "user_stage": mapped["user_stage"],
            "user_message": mapped["user_message"],
            "actions": mapped["actions"],
            "counters": {
                **accounting,
                "collected": collected,
                "organized": organized,
                "failed": failed,
                "detail_read": detail_read,
                "search_calls": int(counters.get("search_calls", 0) or 0),
                "detail_calls": int(counters.get("detail_calls", 0) or 0),
                # 独立计数：为刷新过期定位符额外用掉的搜索次数（不占 max_search_calls）。
                # 用 runs 里的常量而不是字面量：口径只有一个来源，改名不会悄悄漂成 0。
                "locator_refresh_calls": int(
                    counters.get(runs_mod.LOCATOR_REFRESH_COUNTER, 0) or 0),
            },
            # 阶段条按「真实到达点」生成：失败/暂停后未执行的阶段是 skipped，不是 done
            "stages": self._stage_list(stage, stage_reached, run.get("state")),
            # 前端空态的唯一依据（channel/auth/risk/stale 一律给恢复动作，不给「换个关键词」）
            "failure_kind": self._failure_kind(
                run, {"collected": collected, "organized": organized, "failed": failed}, stage),
            "can": {
                "cancel": stage in ACTIVE_RUN_STAGES and not cancelled,
                "resume": can_resume,
                "retry_organize": stage in RETRY_ORGANIZE_STAGES and not cancelled,
                "view_result": collected > 0 or stage in TERMINAL_STAGES,
                "export": collected > 0,
                # 「获取最新结果」：只对已出结果、且非进行中的任务放行（点了会真联网重取页 1）
                "refresh": self._can_refresh(run),
            },
            "cancel_requested": cancelled,
            "started_at": run.get("started_at"),
            "stage_updated_at": run.get("stage_updated_at"),
            # 与 /api/home、/api/auth/status 同一份 boss 块（唯一实现）：
            # 任务页轮询也会 sfSetBoss，缺字段会让顶栏退化（真机缺陷即此处同族）
            "boss": login_bridge.boss_block(),
        }
        return {"task": task}

    @staticmethod
    def _can_refresh(run: dict[str, Any]) -> bool:
        """能否「获取最新结果」：非进行中、非待登录、未取消、且确已取得岗位。

        「获取最新结果」会真正联网重取第 1 页，属于**用户显式**的打扰平台动作；因此只在
        用户已经看着一份结果想刷新时才放行，进行中的任务一律拒绝（避免与采集并发打架）。
        """
        stage = run.get("stage") or ""
        return (not run.get("cancel_requested")
                and stage not in ACTIVE_RUN_STAGES
                and stage != "pending_login"
                and int(run["counters"].get("jobs_unique", 0) or 0) > 0)

    def cancel_task(self, run_id: str, reason: str = "用户主动取消") -> dict:
        run = self._require_run(run_id)
        stage = run.get("stage")
        if stage not in ACTIVE_RUN_STAGES and stage not in {"pending_login"}:
            raise PipelineError(
                "TOOL_NOT_ALLOWED", f"任务阶段 {stage} 不可取消", False,
                "只有进行中的任务可以取消；已结束的结果会完整保留",
            )
        connection = get_conn()
        connection.execute(
            """UPDATE research_run SET cancel_requested=1, state='stopped', stage='cancelled',
                 stage_updated_at=?, user_message=?, stop_reason=? WHERE id=?""",
            (utcnow_iso(), user_view.CANCELLED_MESSAGE, reason, run_id),
        )
        connection.commit()
        return self.task_view(run_id)

    def resume_task(self, run_id: str, *, manual: bool = False) -> dict:
        run = self._require_run(run_id)
        if run.get("cancel_requested"):
            raise PipelineError("TOOL_NOT_ALLOWED", "任务已取消，不能继续", False,
                                "如需继续请重新发起搜索（已取得的数据仍可查看）")
        if (run.get("stage") or "") not in RESUMABLE_STAGES:
            raise PipelineError("TOOL_NOT_ALLOWED", f"任务阶段 {run.get('stage')} 不可继续", False,
                                "只有暂停或待登录的任务可以继续")
        # 用户显式点「继续」= 他已经处理过连接问题：允许再探测一次（not_ready → unknown）。
        # 只在这里放宽，绝不用于自动重试；探测成败仍会回写 channel_state。
        # 关键：**同一次恢复里只 re-arm 一次**（run 级标记幂等），否则后续阶段会把本轮
        # 刚写下的失败结论（not_ready/collect_failed）擦成 unknown，顶栏就会「失败后仍显绿」。
        rearmed_here = self._rearm_channel_for_resume(run_id)
        try:
            if (rearmed_here and
                    channel_state.current_policy().source in (
                        channel_state.SOURCE_STORED_COOKIE, channel_state.SOURCE_EXISTING_BROWSER)):
                # Explicit CDP needs a bounded connection check after a user-handled verification.
                channel_state.precheck_current(reason="explicit_resume")
            status = self._login_state()
            if _account_connected(status) and not self._login_collectable():
                raise PipelineError(
                    CHANNEL_NOT_READY_CODE,
                    user_view.ACCOUNT_OK_CHANNEL_NOT_READY,
                    True,
                    "打开设置页查看连接步骤；连接恢复后再点「继续」，关键词与已取得的数据都保留",
                )
            self._ensure_collectable(run_id)
        except PipelineError:
            # 这次并没有真的开跑（前置检查没过）：把**本次**占用的一次性探测额度还回去，
            # 用户下次点「继续」仍可再探一次。只还自己占的那次，绝不误删在跑那次的标记。
            if rearmed_here:
                self._clear_channel_rearm_mark(run_id)
            raise
        self._set_stage(run_id, "creating", "正在继续上次的搜索")
        # HTTP 的「继续」是人工动作；登录回调的自动恢复保留默认 manual=False。
        # 当前仍在冷却时，两种路径都会被频率闸门拒绝。
        self._spawn_worker(run_id, manual=manual)
        return self.task_view(run_id)

    def retry_organize(self, run_id: str) -> dict:
        """只重跑「分析→整理」，绝不重复搜索/详情，也不动 search/detail 计数。"""
        run = self._require_run(run_id)
        if run.get("cancel_requested"):
            raise PipelineError("TOOL_NOT_ALLOWED", "任务已取消，不能重试整理")
        if (run.get("stage") or "") not in RETRY_ORGANIZE_STAGES:
            raise PipelineError("TOOL_NOT_ALLOWED", f"任务阶段 {run.get('stage')} 不可重试整理",
                                False, "仅在整理未完成时可用")
        if not self._ai_ready(run):
            raise PipelineError(
                "AI_NOT_CONFIGURED",
                "AI 服务未配置，无法整理岗位正文",
                False,
                "由部署方配置模型后重试；已取得岗位与正文均已保留，不回退为规则结果",
            )
        self._set_stage(run_id, "analyzing", "正在重新整理公司方向")
        self._spawn_worker(run_id, organize_only=True)
        return self.task_view(run_id)

    def refresh_task(self, run_id: str) -> dict:
        """用户显式「获取最新结果」：重跑采集，**首个页签跳过上游缓存**，真正联网取一次。

        与「继续」共用同一套前置门禁（账号/通道），绝不绕过检查直接联网。只允许对**已有结果**
        的任务使用——用户看着结果想刷新时才用它。清掉搜索检查点让搜索阶段从第 1 页重跑；
        详情检查点保留（正文没必要重复读）。首个页签带 ``--no-cache``，后续页仍允许命中缓存，
        既拿到最新列表，又不过度打扰平台。
        """
        run = self._require_run(run_id)
        if not self._can_refresh(run):
            if run.get("cancel_requested"):
                raise PipelineError("TOOL_NOT_ALLOWED", "任务已取消，不能获取最新结果", False,
                                    "如需继续请重新发起搜索（已取得的数据仍可查看）")
            stage = run.get("stage") or ""
            if stage in ACTIVE_RUN_STAGES or stage == "pending_login":
                raise PipelineError("TOOL_NOT_ALLOWED", f"任务阶段 {stage} 正在进行中", False,
                                    "请等本次采集结束后再获取最新结果")
            raise PipelineError("TOOL_NOT_ALLOWED", "本次还没有取得岗位，无法获取最新结果", False,
                                "请先发起一次搜索")
        status = self._login_state()
        if _account_connected(status) and not self._login_collectable():
            raise PipelineError(
                CHANNEL_NOT_READY_CODE,
                user_view.ACCOUNT_OK_CHANNEL_NOT_READY,
                True,
                "打开设置页查看连接步骤；连接恢复后再点「获取最新结果」，关键词与已取得的数据都保留",
            )
        if int(run["counters"].get("search_calls",0)) >= int(run["limits"]["max_search_calls"]):
            # A refresh needs a new budget receipt; preserve the completed historical run.
            filters = jloads(run.get("filters"),{}) or {}
            return self.start_task(run.get("query_text") or run["config_snapshot"]["queries"][0]["text"],
                city=filters.get("city"),job_type=filters.get("job_type"),
                source=run["config_snapshot"]["source"],force_new=True)
        checkpoint = self._checkpoint(run_id)
        checkpoint["pages_done"] = {}
        self._save_checkpoint(run_id, checkpoint)
        self._set_stage(run_id, "creating", "正在获取最新结果")
        self._spawn_worker(run_id, force_refresh=True)
        return self.task_view(run_id)

    # ------------------------------------------------------------------
    # 结果 / 历史
    # ------------------------------------------------------------------

    def result_view(self, run_id: str, *, job_type: Optional[str] = None) -> dict:
        """结果视图。``job_type`` 是**读时展示口径**：只过滤 directions[]。

        生效顺序：显式入参 > run 的 ``filters.job_type`` > ``DEFAULT_DISPLAY_JOB_TYPE``
        （默认 product_engineering，对齐需求 §3「默认关注产品和研发岗位」）。
        采集结果与 counters 一律不变，仅展示口径变化。
        """
        run = self._require_run(run_id)
        counters = run["counters"]
        collected = int(counters.get("jobs_unique", 0) or 0)
        organized = int(run.get("organized_count") or 0)
        failed = int(run.get("failed_count") or 0)
        filters = jloads(run.get("filters"), {}) or {}
        effective = job_type or filters.get("job_type") or DEFAULT_DISPLAY_JOB_TYPE
        if effective not in JOB_TYPES:
            raise PipelineError("INVALID_ANALYSIS", f"岗位类型 {effective} 不受支持", False,
                                f"岗位类型取 {'/'.join(JOB_TYPE_ORDER)}")
        allowed = ROLE_CLASSES_BY_JOB_TYPE[effective]

        rows = query_all(
            """SELECT d.*, c.display_name, c.identity_status
               FROM run_direction d JOIN company c ON c.id=d.company_id
               WHERE d.run_id=?
               ORDER BY CASE d.support_level WHEN 'multi' THEN 0 WHEN 'unverified' THEN 1
                         ELSE 2 END, d.latest_fetched_at DESC, d.statement""",
            (run_id,),
        )
        roles_by_direction = self._direction_role_classes(rows)
        directions = []
        hidden = 0
        for row in rows:
            if allowed is not None and not (
                    roles_by_direction.get(row["id"], frozenset()) & allowed):
                hidden += 1
                continue
            finding_ids = jloads(row["finding_ids"], [])
            metadata = query_all(
                """SELECT a.relevance, s.detail_status, f.limitations
                   FROM finding f JOIN analysis a ON a.id=f.analysis_id
                   JOIN job_snapshot s ON s.id=a.snapshot_id WHERE f.id IN ("""
                + ",".join("?" for _ in finding_ids) + ")", tuple(finding_ids)) if finding_ids else []
            relevances = {m["relevance"] for m in metadata}
            relevance = next(iter(relevances)) if len(relevances) == 1 else "unknown"
            partial_body = any(m["detail_status"] != "complete" for m in metadata)
            directions.append({
                "relevance": relevance,
                "relevance_label": schemas.RELEVANCE_LABELS.get(relevance, "待确认"),
                "partial_body": partial_body,
                "limitations": sorted({s for m in metadata for s in jloads(m["limitations"], [])}),
                "company": row["company_id"],
                "display_name": row["display_name"],
                "identity_status": row["identity_status"],
                "business_domain": row["business_domain"],
                "business_domain_label": schemas.TAXONOMY.get(row["business_domain"],
                                                              row["business_domain"]),
                "statement": row["statement"],
                "action_type": row["action_type"],
                "action_type_label": schemas.ACTION_TYPE_LABELS.get(row["action_type"],
                                                                    row["action_type"]),
                "support_level": row["support_level"],
                "support_level_label": schemas.SUPPORT_LEVEL_LABELS.get(row["support_level"],
                                                                        row["support_level"]),
                "review_status": row["review_status"],
                "review_status_label": schemas.REVIEW_STATUS_LABELS.get(row["review_status"],
                                                                       row["review_status"]),
                "job_count": row["job_count"],
                "evidence_count": row["evidence_count"],
                "latest_fetched_at": row["latest_fetched_at"],
                "finding_ids": jloads(row["finding_ids"], []),
                "job_ids": jloads(row["job_ids"], []),
            })
        # 空态理由只在「真实成功返回的空列表」时才说「没有匹配岗位」；
        # 通道/认证/风控/定位符过期一律不出现这句话（前端同时按 failure_kind 分流）。
        # 用 _failure_kind 而不是直接 user_view.failure_kind：迁移前的老任务即使
        # stop_reason 还是上游口径（UPSTREAM_RESULT_LIMIT），只要确实还有过期定位符，
        # 也能按**事实**如实归到 stale，绝不把「定位符过期」说成「没找到岗位」。
        failure_kind = self._failure_kind(
            run, {"collected": collected, "organized": organized, "failed": failed},
            run.get("stage") or "",
        )
        empty_reason = None
        if failure_kind == "empty":
            # 只有「真实成功返回的空列表」才允许给结论性文案「没有找到匹配岗位」
            empty_reason = user_view.EMPTY_RESULT_MESSAGE
        elif failure_kind == "stale":
            # 定位符过期、详情**根本没取到**：给一句与 stale 一致的事实描述，
            # 绝不套「未发现方向」——那是把「失败」说成「无结果」（用户指令 §4.1）
            empty_reason = user_view.STALE_DETAILS_PENDING_MESSAGE
        elif failure_kind == "gated":
            # 本地频率闸门：如实说清为什么在等、大概还要等多久（与任务页同一句）
            empty_reason = user_view.rate_gate_message(rate_gate.remaining_wait_seconds())
        elif failure_kind == "none" and not rows:
            # 只有在**没有任何失败信号**（failure_kind == none）时，才敢下「没有足以
            # 判断的业务方向」这个结论。channel/auth/risk/partial 是失败，不能下结论。
            # 注意：按「过滤后为空」判定理由会让人误以为丢了数据，
            # 所以这里只看未过滤的方向，过滤的隐藏量单独放在 hidden_by_job_type。
            empty_reason = "本次未发现足以判断的业务方向"
        return {
            "summary": {
                **self._accounting(run_id),
                "query": run.get("query_text") or self._query_of(run),
                "scope": {
                    "city": filters.get("city"),
                    "job_type": effective,
                    "job_type_applies_to": JOB_TYPE_APPLIES_TO,
                    "hidden_by_job_type": hidden,
                },
                "searched_at": run.get("started_at"),
                "collected": collected,
                "organized": organized,
                "failed": failed,
            },
            "directions": directions,
            "empty_reason": empty_reason,
            "environment_blocked": self._error_code(run) == "ENVIRONMENT_RISK",
            "failure_kind": failure_kind,
            # 空态按钮文案据此分流：stale 且额度仍在 → 给「继续」（真能成功）
            "can_resume": bool(failure_kind == "stale"
                               and runs_mod.locator_refresh_left(run_id)),
            "stage": run.get("stage"),
            "state": run["state"],
            # 如实告知「这份结果是不是来自本地缓存、多旧」——绝不沉默地端上旧数据
            "cache": self._result_cache(run_id),
            "can": {"refresh": self._can_refresh(run)},
        }

    @staticmethod
    def _result_cache(run_id: str) -> dict:
        """结果页的缓存标注：命中上游缓存时给出年龄与一句人话；否则 ``hit=False`` 且无文案。"""
        age = runs_mod.latest_search_cache_age(run_id)
        return {
            "hit": age is not None,
            "age_seconds": age,
            "text": user_view.cache_served_message(age) if age is not None else None,
        }

    def _direction_role_classes(self, rows) -> dict[str, frozenset[str]]:
        """方向 → 其证据链上的 role_class 集合（读时展示口径的过滤依据）。

        一条方向可能由多个岗位的 finding 聚合而来，故取并集：只要有一条证据属于
        允许的角色，该方向就保留——这正是 product_engineering == product ∪ engineering
        得以成立的原因。
        """
        finding_ids = [fid for row in rows for fid in jloads(row["finding_ids"], [])]
        if not finding_ids:
            return {}
        placeholders = ",".join("?" * len(finding_ids))
        pairs = query_all(
            f"""SELECT f.id AS finding_id, a.role_class
                FROM finding f JOIN analysis a ON a.id=f.analysis_id
                WHERE f.id IN ({placeholders})""",
            tuple(finding_ids),
        )
        role_of_finding = {p["finding_id"]: p["role_class"] for p in pairs}
        return {
            row["id"]: frozenset(
                role_of_finding[fid] for fid in jloads(row["finding_ids"], [])
                if role_of_finding.get(fid)
            )
            for row in rows
        }

    def history_list(self, limit: int = 20) -> list[dict]:
        """历史记录：只列 simple 任务，演示数据不混入（需求 UX-13）。"""
        rows = query_all(
            """SELECT * FROM research_run WHERE origin=?
               ORDER BY started_at DESC LIMIT ?""",
            (SIMPLE_ORIGIN, max(1, min(int(limit or 20), 200))),
        )
        result = []
        for run in rows:
            filters = jloads(run.get("filters"), {}) or {}
            result.append({
                "run_id": run["id"],
                "query": run.get("query_text") or self._query_of(run),
                "origin": run.get("origin"),
                "city": filters.get("city"),
                "searched_at": run.get("started_at"),
                "collected": int((jloads(run.get("counters"), {}) or {}).get("jobs_unique", 0) or 0),
                "organized": int(run.get("organized_count") or 0),
                "failed": int(run.get("failed_count") or 0),
                "stage": run.get("stage"),
                "state": run.get("state"),
            })
        return result

    def home_view(self) -> dict:
        status = self._login_state()
        recent = [
            {
                "run_id": item["run_id"],
                "query": item["query"],
                "searched_at": item["searched_at"],
                "collected": item["collected"],
                "organized": item["organized"],
            }
            for item in self.history_list(limit=5)
        ]
        return {
            # boss 块唯一实现在 login_bridge.boss_block()（app.py 与 /api/tasks 共用同一份）：
            # 复用本请求已取到的 status，保证三处接口读到的是同一时刻的同一份事实。
            "boss": login_bridge.boss_block(status),
            "recent": recent,
            "suggestions": list(schemas.SUGGESTED_QUERIES),
            "scope": {"city": self._default_city(), "label": self._default_city_label()},
        }

    # ------------------------------------------------------------------
    # 启动 / 重启恢复
    # ------------------------------------------------------------------

    def recover_pending(self) -> list[str]:
        """服务重启：把未完成的 simple 任务置 paused，**不**自动重新采集。"""
        rows = query_all(
            """SELECT id FROM research_run
               WHERE origin=? AND cancel_requested=0 AND stage IS NOT NULL
                 AND stage NOT IN ('completed','cancelled','failed','partial','pending_login',
                                   'paused')""",
            (SIMPLE_ORIGIN,),
        )
        if not rows:
            return []
        now = utcnow_iso()
        connection = get_conn()
        for row in rows:
            connection.execute(
                """UPDATE research_run SET stage='paused', stage_updated_at=?, user_message=?
                   WHERE id=?""",
                (now, user_view.PAUSED_MESSAGE, row["id"]),
            )
        connection.commit()
        return [row["id"] for row in rows]

    def on_login_connected(self) -> Optional[str]:
        """登录成功后自动继续「未登录先搜索」的那一次请求（一次性、有 TTL 护栏）。"""
        cutoff = (datetime.now(timezone.utc) -
                  timedelta(seconds=PENDING_INTENT_TTL_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = query_one(
            """SELECT * FROM research_run
               WHERE origin=? AND stage='pending_login' AND cancel_requested=0
               ORDER BY started_at DESC, rowid DESC LIMIT 1""",
            (SIMPLE_ORIGIN,),
        )
        if not row:
            return None
        checkpoint = jloads(row.get("checkpoint"), {}) or {}
        intent_at = checkpoint.get("intent_at")
        if not intent_at or intent_at < cutoff:
            return None
        self._bind_unstarted_channel(row["id"])
        self.resume_task(row["id"])
        return row["id"]

    def _bind_unstarted_channel(self, run_id: str) -> None:
        """A pending login intent may adopt the newly opened browser before any collection."""
        run = self._require_run(run_id)
        if (run.get("stage") != "pending_login" or self._channel_attempt_in_flight(run_id)
                or run["counters"].get("search_calls", 0)
                or run["counters"].get("jobs_unique", 0)
                or run["config_snapshot"].get("source") != SIMPLE_SOURCE):
            return
        config = run["config_snapshot"]
        config["channel"] = channel_state.freeze_channel()
        connection = get_conn()
        connection.execute("UPDATE research_run SET config_snapshot=? WHERE id=?",
                           (jdumps(config), run_id))
        connection.commit()

    # ------------------------------------------------------------------
    # worker
    # ------------------------------------------------------------------

    def _rearm_channel_for_resume(self, run_id: str) -> bool:
        """显式恢复时的通道 re-arm：**同一次恢复里最多一次**（返回是否真的 re-arm 了）。

        为什么要幂等：``not_ready → unknown`` 是唯一能让 ``collect_gate()`` 对
        auto 通道重新放行一次探测的动作。同一次恢复里被触发两次时，第二次会把
        「本轮刚写下」的 ``not_ready/collect_failed`` 擦掉，顶栏就从「失败」变回绿点
        （失败结论被覆盖）。标记在 worker 收尾时清除，所以**用户再次显式点「继续」**
        仍然是一次新的恢复，可以再探一次——放宽只对显式动作生效，绝不自动重试。
        """
        checkpoint = self._checkpoint(run_id)
        if checkpoint.get(CHANNEL_REARM_MARK):
            return False
        if self._channel_attempt_in_flight(run_id):
            # 本次采集还在跑（worker 存活）：此刻的 not_ready 只可能是**本轮**刚写下的，
            # 再 re-arm 就是拿「探测许可」覆盖「失败结论」——硬约束，一律拒绝。
            return False
        checkpoint[CHANNEL_REARM_MARK] = utcnow_iso()
        self._save_channel_rearm_mark(run_id, checkpoint)
        channel_state.rearm_current()
        return True

    def _channel_attempt_in_flight(self, run_id: str) -> bool:
        """本次采集是否还在跑（worker 线程存活）——与 ``_spawn_worker`` 同一套事实。"""
        with self._lock:
            thread = self._threads.get(run_id)
            return bool(thread and thread.is_alive())

    def _clear_channel_rearm_mark(self, run_id: str) -> None:
        """撤销「本次恢复已 re-arm」标记：本次恢复收尾（或未真正开跑）时调用。"""
        checkpoint = self._checkpoint(run_id)
        if checkpoint.pop(CHANNEL_REARM_MARK, None) is None:
            return
        self._save_channel_rearm_mark(run_id, checkpoint)

    @staticmethod
    def _save_channel_rearm_mark(run_id: str, checkpoint: dict[str, Any]) -> None:
        """只写 checkpoint 列：re-arm 标记不是阶段推进，不该改 ``stage_updated_at``。

        否则 worker 收尾时的清理会把「最后更新时间」顶到当下，UI 上的时间就假了。
        """
        connection = get_conn()
        connection.execute("UPDATE research_run SET checkpoint=? WHERE id=?",
                           (jdumps(checkpoint), run_id))
        connection.commit()

    def _spawn_worker(self, run_id: str, *, organize_only: bool = False,
                      force_refresh: bool = False, manual: Optional[bool] = None) -> None:
        with self._lock:
            if run_id in self._threads and self._threads[run_id].is_alive():
                return
            thread = threading.Thread(
                target=self._worker, args=(run_id, organize_only, force_refresh, manual), daemon=True,
                name=f"pipeline-{run_id}",
            )
            self._threads[run_id] = thread
        thread.start()

    def _worker(self, run_id: str, organize_only: bool, force_refresh: bool = False,
                manual: Optional[bool] = None) -> None:
        lease_held = False
        try:
            if organize_only:
                self._organize_phase(run_id)
            else:
                # 账号级串行：同一时刻只允许一个采集任务持有采集租约（跨进程、跨重启，落库 + TTL）。
                granted, wait_seconds = rate_gate.begin_collection(run_id)
                if not granted:
                    raise PipelineError(
                        rate_gate.RATE_BUSY,
                        f"已有另一个采集任务正在进行，约 {max(1, wait_seconds // 60)} 分钟后可再试",
                        True, "等当前采集任务结束后再继续",
                    )
                lease_held = True
                self._collect_phase(run_id, force_refresh=force_refresh, manual=manual)
                if self._cancelled(run_id):
                    return
                self._organize_phase(run_id)
        except _Cancelled:
            self._mark_cancelled(run_id)
        except PipelineError as exc:
            self._settle_failure(run_id, exc.code)
        except runs_mod.RunError as exc:
            self._settle_failure(run_id, exc.code)
        except analysis.AnalysisError as exc:
            self._settle_organize_failure(run_id, exc.code)
        except Exception as exc:  # 兜底：任何异常都要落一个可恢复状态，不能静默卡住
            self._settle_failure(run_id, f"UNEXPECTED_{type(exc).__name__}".upper())
        finally:
            # 本次恢复已经收尾：撤掉一次性标记，用户下一次显式点「继续」= 新的一次恢复。
            # 收尾清理绝不允许影响 worker 退出，任何异常都吞掉（通道标记只是诊断辅助）。
            try:
                if lease_held:
                    rate_gate.end_collection(run_id)
            except Exception:  # noqa: BLE001
                pass
            try:
                self._clear_channel_rearm_mark(run_id)
            except Exception:  # noqa: BLE001
                pass
            with self._lock:
                self._threads.pop(run_id, None)

    def _wait_for_list_slot(self, run_id: str, *, manual: Optional[bool] = None,
                            force_refresh: bool = False) -> None:
        """Only the local minimum interval is scheduled; other gates remain stops."""
        run = self._require_run(run_id)
        if run["config_snapshot"].get("source") != "zhipin":
            return
        if manual is None:
            manual = force_refresh or bool(run["config_snapshot"].get("user_initiated"))
        deadline = time.monotonic() + rate_gate.MIN_LIST_INTERVAL_SECONDS + 2
        waited = False
        while True:
            self._raise_if_cancelled(run_id)
            decision = rate_gate.check_list_request(run_id, manual=manual)
            if decision["allowed"]:
                if waited:
                    self._set_stage(run_id, "searching", "正在搜索岗位")
                return
            if decision["code"] != rate_gate.RATE_MIN_INTERVAL or time.monotonic() >= deadline:
                raise runs_mod.RunError(decision["code"], decision["message"], True,
                                        decision["operator_action"])
            waited = True
            self._set_stage(run_id, "searching",
                            f"正在等待搜索间隔，约 {decision['wait_seconds']} 秒后自动继续；已取得的岗位已保存")
            time.sleep(1)

    def _collect_phase(self, run_id: str, *, force_refresh: bool = False,
                       manual: Optional[bool] = None) -> None:
        run = self._require_run(run_id)
        checkpoint = self._checkpoint(run_id)
        query_id = (run["config_snapshot"].get("queries") or [{}])[0].get("query_id", "q1")

        self._set_stage(run_id, "searching", "正在搜索岗位")
        pages_done = list(checkpoint.get("pages_done", {}).get(query_id, []))
        # 老任务可能是「搜索预算打满」留下的 budget_stopped：只要有详情可做就先恢复，
        # 否则下面的 do_search / fetch_details 两道门禁都会把它挡在门外。
        # 注意：这里**只**恢复预算态，绝不 re-arm 通道（re-arm 只归 resume_task 那一个入口，
        # 且一次恢复只一次），否则会把搜索阶段刚写下的 not_ready 擦成 unknown。
        self._ensure_details_callable(run_id)
        # 「获取最新结果」只对**首个**页签显式跳过上游缓存（少打扰平台：后续页仍允许命中缓存）。
        refresh_first = force_refresh
        for page in range(1, MAX_SEARCH_PAGES + 1):
            self._raise_if_cancelled(run_id)
            if page in pages_done:
                continue
            if not refresh_first and self._require_run(run_id)["counters"].get("jobs_unique", 0) >= MAX_JOBS:
                break
            try:
                self._wait_for_list_slot(run_id, manual=manual, force_refresh=refresh_first)
                result = runs_mod.do_search(run_id, query_id, page, force_refresh=refresh_first,
                                            manual=manual)
            except runs_mod.RunError as exc:
                if exc.code == "BUDGET_EXHAUSTED":
                    # 记下是**哪个**预算打满：state 里不区分，checkpoint 里要留痕迹，
                    # 详情阶段照常往下走（详情预算与之独立）。
                    checkpoint["search_budget_exhausted"] = True
                    self._save_checkpoint(run_id, checkpoint)
                    break
                raise
            refresh_first = False
            pages_done.append(page)
            checkpoint.setdefault("pages_done", {})[query_id] = pages_done
            self._save_checkpoint(run_id, checkpoint)
            counters = runs_mod.get_run(run_id)["counters"]
            checkpoint["snapshot_ids_ready"] = self._snapshot_ids(run_id)
            if not result.get("has_more") or counters["jobs_unique"] >= MAX_JOBS:
                break
        checkpoint["snapshot_ids_ready"] = self._snapshot_ids(run_id)
        self._save_checkpoint(run_id, checkpoint)

        self._set_stage(run_id, "fetching_details", "正在读取岗位详情，已读取 0 个")
        done = list(checkpoint.get("detail_done_refs", []))
        pending = [
            row["job_ref"]
            for row in query_all(
                """SELECT job_ref FROM run_job WHERE run_id=? AND detail_outcome='pending'
                   ORDER BY rowid""",
                (run_id,),
            )
            if row["job_ref"] not in done
        ][:MAX_JOBS]
        # 搜索预算耗尽不该连详情一起判死：仍有 pending 岗位就显式恢复到可调用态。
        # 同上：这里不 re-arm 通道。本次恢复的搜索若已如实失败，该失败结论必须保留下来
        # （详情阶段再 re-arm 会把它擦成 unknown → 顶栏「失败后仍显绿」）。
        self._ensure_details_callable(run_id, pending)
        for index in range(0, len(pending), DETAILS_PER_CALL):
            self._raise_if_cancelled(run_id)
            batch = pending[index:index + DETAILS_PER_CALL]
            try:
                runs_mod.fetch_details(run_id, batch)
            except runs_mod.RunError as exc:
                if exc.code != "BUDGET_EXHAUSTED":
                    raise
                # 详情预算打满：保留已取到的详情继续整理，不把整轮判死
                checkpoint["detail_budget_exhausted"] = True
                self._save_checkpoint(run_id, checkpoint)
                break
            done.extend(batch)
            checkpoint["detail_done_refs"] = done
            self._save_checkpoint(run_id, checkpoint)
            counters = runs_mod.get_run(run_id)["counters"]
            self._set_stage(
                run_id, "fetching_details",
                f"正在读取岗位详情，已读取 {counters['snapshots_complete'] + counters['snapshots_reused'] + counters.get('snapshots_partial', 0)} 个",
            )

        self._set_stage(run_id, "deduping", "正在去重并保留原文")
        checkpoint["snapshot_ids_ready"] = self._snapshot_ids(run_id)
        self._save_checkpoint(run_id, checkpoint)
        state = self._require_run(run_id)["state"]
        if state in ("planned", "collecting"):
            try:
                runs_mod.finish_collection(run_id)
            except runs_mod.RunError:
                pass
        dedupe.update_support_levels(run_id)

    @staticmethod
    def _pending_detail_refs(run_id: str) -> list[str]:
        return [row["job_ref"] for row in query_all(
            """SELECT job_ref FROM run_job WHERE run_id=? AND detail_outcome='pending'
               ORDER BY rowid""",
            (run_id,),
        )]

    def _ensure_details_callable(self, run_id: str,
                                 pending: Optional[list[str]] = None) -> None:
        """把「被搜索预算误伤」的状态显式恢复到可调用态。

        门禁本身**保留不动**（``fetch_details`` / ``do_search`` 仍只认
        planned/collecting），这里只是在**确实还有详情可做**时走既有的
        ``resume_collection`` 显式恢复——绝不靠放宽门禁来放行。
        """
        run = self._require_run(run_id)
        if run["state"] in ("planned", "collecting"):
            return
        if run["state"] != "budget_stopped":
            return          # 认证/风控/失败态各有各的恢复入口，不在这里代劳
        refs = self._pending_detail_refs(run_id) if pending is None else pending
        if not refs:
            return          # 没有待读详情：budget_stopped 是如实结论，保持不动
        try:
            runs_mod.resume_collection(run_id)
        except runs_mod.RunError:
            pass            # 确实没预算了：保持 budget_stopped，由文案给出人话说明

    def _organize_phase(self, run_id: str) -> None:
        run = self._require_run(run_id)
        if run["state"] in ("planned", "collecting"):
            try:
                runs_mod.finish_collection(run_id)
            except runs_mod.RunError:
                pass
        checkpoint = self._checkpoint(run_id)
        ready = self._snapshot_ids(run_id)
        # DB receipts, not a legacy checkpoint, prove that an analysis was actually saved.
        analyzed = [row["snapshot_id"] for row in query_all(
            """SELECT rj.snapshot_id FROM run_job rj JOIN analysis a ON a.id=rj.analysis_id
               AND a.snapshot_id=rj.snapshot_id WHERE rj.run_id=?""", (run_id,))]
        checkpoint["snapshot_ids_ready"] = ready
        checkpoint["analyzed_snapshot_ids"] = analyzed
        self._save_checkpoint(run_id, checkpoint)
        todo = [snapshot_id for snapshot_id in ready if snapshot_id not in analyzed]

        if todo:
            self._set_stage(run_id, "analyzing", "正在整理公司方向，已完成 0 个")
        for index in range(0, len(todo), ANALYSIS_BATCH_SIZE):
            self._raise_if_cancelled(run_id)
            batch = todo[index:index + ANALYSIS_BATCH_SIZE]
            analysis.auto_analyze(run_id, batch)  # AI_NOT_CONFIGURED 等直接抛出
            saved = {row["snapshot_id"] for row in query_all(
                """SELECT rj.snapshot_id FROM run_job rj JOIN analysis a ON a.id=rj.analysis_id
                   AND a.snapshot_id=rj.snapshot_id WHERE rj.run_id=?""", (run_id,))}
            connection = get_conn()
            for sid in batch:
                if sid in saved:
                    analyzed.append(sid)
                else:
                    connection.execute(
                        """UPDATE run_job SET analysis_error=COALESCE(analysis_error, ?)
                           WHERE run_id=? AND snapshot_id=?""",
                        (jdumps([{"code": "AI_OUTPUT_MISSING", "message": "分析未成功保存，可重试整理"}]), run_id, sid))
            connection.commit()
            checkpoint["analyzed_snapshot_ids"] = analyzed
            self._save_checkpoint(run_id, checkpoint)
            self._set_stage(run_id, "analyzing",
                            f"正在整理公司方向，已完成 {self._organized_count(run_id)} 个")

        self._raise_if_cancelled(run_id)
        self._set_stage(run_id, "organizing", "正在按公司和方向整理")
        self._build_directions(run_id)
        checkpoint["directions_built"] = True
        self._save_checkpoint(run_id, checkpoint)

        organized = self._recount(run_id)
        failed = self._failed_count(run_id)
        state = "partial" if failed else "completed"
        stage = "partial" if failed else "completed"
        mapped = user_view.user_view(stage, state, None,
                                    {"collected": self._collected_count(run_id),
                                     "organized": organized, "failed": failed})
        connection = get_conn()
        connection.execute(
            """UPDATE research_run SET stage=?, state=?, stage_reached=?, stage_updated_at=?,
                 user_message=?, organized_count=?, failed_count=?,
                 collection_ended_at=COALESCE(collection_ended_at, ?)
               WHERE id=?""",
            (stage, state, "completed", utcnow_iso(), mapped["user_message"], organized, failed,
             utcnow_iso(), run_id),
        )
        connection.commit()

    # ------------------------------------------------------------------
    # 结果物化
    # ------------------------------------------------------------------

    def _build_directions(self, run_id: str) -> int:
        """按 company_id + business_domain + support_level 聚合证据化结论。"""
        rows = query_all(
            """SELECT f.id AS finding_id, f.statement, f.business_domain, f.action_type,
                      f.support_level, f.review_status, f.created_at,
                      j.company_id AS company_id, j.id AS job_id, s.fetched_at,
                      (SELECT COUNT(*) FROM evidence e WHERE e.finding_id=f.id) AS evidence_count
               FROM finding f
               JOIN analysis a ON f.analysis_id=a.id
               JOIN job_snapshot s ON a.snapshot_id=s.id
               JOIN job j ON s.job_id=j.id
               JOIN run_job rj ON rj.analysis_id=a.id AND rj.snapshot_id=s.id
               WHERE rj.run_id=?
                 AND j.company_id IS NOT NULL
                 AND f.review_status != 'excluded'
                 AND EXISTS (SELECT 1 FROM evidence e WHERE e.finding_id=f.id)
               ORDER BY s.fetched_at, f.created_at""",
            (run_id,),
        )
        groups: dict[tuple, dict[str, Any]] = {}
        for row in rows:
            if analysis.is_generic_statement(row["statement"]):
                continue
            key = (row["company_id"], row["business_domain"], row["statement"])
            group = groups.setdefault(key, {
                "statement": row["statement"], "statement_weight": -1,
                "action_type": row["action_type"], "review_status": row["review_status"],
                "finding_ids": [], "job_ids": set(), "evidence_count": 0, "latest": "",
                "unverified": False,
            })
            weight = int(row["evidence_count"] or 0)
            if weight > group["statement_weight"]:
                group["statement"] = row["statement"]
                group["statement_weight"] = weight
                group["action_type"] = row["action_type"]
            group["unverified"] |= row["support_level"] == "unverified"
            group["finding_ids"].append(row["finding_id"])
            group["job_ids"].add(row["job_id"])
            group["evidence_count"] += int(row["evidence_count"] or 0)
            if (row["fetched_at"] or "") > group["latest"]:
                group["latest"] = row["fetched_at"] or ""
            if row["review_status"] != "confirmed":
                group["review_status"] = "unreviewed"

        connection = get_conn()
        with connection:
            now = utcnow_iso()
            written = 0
            retained_ids = []
            for (company_id, domain, _statement), group in groups.items():
                support_level = "unverified" if group["unverified"] else "single"
                existing = query_one(
                    """SELECT id FROM run_direction WHERE run_id=? AND company_id=?
                       AND business_domain=? AND statement=?""",
                    (run_id, company_id, domain, group["statement"]),
                )
                direction_id = existing["id"] if existing else new_id("dir")
                payload = (
                    jdumps(sorted(set(group["finding_ids"]))),
                    jdumps(sorted(group["job_ids"])),
                    len(group["job_ids"]),
                    group["evidence_count"],
                    group["latest"] or None,
                    group["action_type"],
                    support_level,
                    group["review_status"],
                    now,
                )
                if existing:
                    connection.execute(
                        """UPDATE run_direction SET finding_ids=?, job_ids=?, job_count=?,
                             evidence_count=?, latest_fetched_at=?, action_type=?, support_level=?,
                             review_status=?, updated_at=? WHERE id=?""",
                        (*payload, direction_id),
                    )
                else:
                    connection.execute(
                        """INSERT INTO run_direction
                           (id, run_id, company_id, business_domain, statement, action_type,
                            support_level, review_status, job_count, evidence_count,
                            latest_fetched_at, finding_ids, job_ids, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (direction_id, run_id, company_id, domain, group["statement"],
                         group["action_type"], support_level, group["review_status"],
                         len(group["job_ids"]), group["evidence_count"], group["latest"] or None,
                         jdumps(sorted(set(group["finding_ids"]))), jdumps(sorted(group["job_ids"])),
                         now, now),
                    )
                retained_ids.append(direction_id)
                written += 1
            if retained_ids:
                connection.execute(
                    "DELETE FROM run_direction WHERE run_id=? AND id NOT IN (" + ",".join("?" for _ in retained_ids) + ")",
                    (run_id, *retained_ids))
            else:
                connection.execute("DELETE FROM run_direction WHERE run_id=?", (run_id,))
        return written

    # ------------------------------------------------------------------
    # 内部小工具
    # ------------------------------------------------------------------

    def _require_run(self, run_id: str) -> dict[str, Any]:
        try:
            return runs_mod.get_run(run_id)
        except KeyError:
            raise PipelineError("INVALID_ANALYSIS", f"任务 {run_id} 不存在") from None

    @staticmethod
    def _query_of(run: dict[str, Any]) -> str:
        queries = (run.get("config_snapshot") or {}).get("queries") or []
        return queries[0].get("text", "") if queries else ""

    @staticmethod
    def _resume_blocked(run: dict[str, Any]) -> bool:
        """不给假按钮：定位符过期且刷新额度已用尽 → 点「继续」必然同样失败。

        ``stop_reason`` 会在成功恢复时被清空，所以这个判定不会误伤恢复后的正常流程。
        """
        return (run.get("stop_reason") == runs_mod.STALE_LOCATOR_CODE
                and not runs_mod.locator_refresh_left(run["id"]))

    @staticmethod
    def _detail_phase(run: dict[str, Any]) -> bool:
        """失败是否发生在**详情阶段**（搜索已成功取得岗位之后）。

        事实依据是 ``stage_reached``（只增不减的「真实到达的阶段」）：只要到达过
        ``fetching_details`` 及之后，就说明搜索这一步已经成功。

        为什么要这一步：上游 CLI 的**搜索走浏览器会话**，**详情走独立的 HTTP 通道**
        （优先用本地保存的登录凭证）。所以详情阶段的认证失败与搜索阶段的「确实没登录」
        是两回事——前者浏览器会话往往是好的，把它渲染成「重新登录」会把用户引进
        反复登录的循环（CDP 登录常只提取到部分凭证）。
        """
        return str(run.get("stage_reached") or "") in DETAIL_PHASES

    @staticmethod
    def _stale_locator_state(run: dict[str, Any], stage: str) -> bool:
        """用户当下面对的**主问题**是不是「定位符过期」——两处 UI 共用的唯一口径。

        **事实判定**，只看现有事实（不引入新表/新列）：

        1. ``auth`` / ``risk`` 优先级更高：没登录、风控中时，刷新定位符也读不了详情，
           用户必须先解决账号/风控问题 → 返回 False，让 auth/risk 文案与动作生效；
        2. 否则，只要还有「待读（``detail_outcome='pending'``）且定位符已过期」的岗位，
           或 ``stop_reason`` 就是 ``STALE_LOCATOR`` → 就是 stale；
        3. 仍要求处于可恢复阶段（``RESUMABLE_STAGES``），避免给终态任务贴「可继续」暗示。

        **刻意不看「最近一次失败的码」**：``stop_reason`` 只记**最后一次**失败——真机里
        一次失败的刷新搜索把它写成了 ``UPSTREAM_NETWORK``，而岗位确实因定位符过期读不了。
        据此分流会让任务页（走 stale 视图）与结果页（走 channel 空态）各说各话，这正是
        本次整改要消灭的。任务页与结果页都调用本方法，保证永远同源。
        """
        if stage not in RESUMABLE_STAGES:
            return False
        state = str(run.get("state") or "")
        if state in ("auth_required", "risk_stopped"):
            return False
        code = (Pipeline._error_code(run) or "").upper()
        if (code in user_view.FAILURE_AUTH_CODES or code in user_view.FAILURE_RISK_CODES
                or code in user_view.FAILURE_GATE_CODES):
            return False
        if run.get("stop_reason") == runs_mod.STALE_LOCATOR_CODE:
            return True
        return runs_mod.stale_locator_pending_count(run["id"]) > 0

    @staticmethod
    def _failure_kind(run: dict[str, Any], counters: dict[str, int], stage: str) -> str:
        """失败分类：账号/风控优先，其次用**事实**判「定位符过期」，最后才按码。

        优先级规则（与 ``task_view`` 共用 ``_stale_locator_state``，两边必然一致）：

        1. ``auth`` / ``risk``（``user_view.failure_kind`` 按码/状态判出的）最优先：
           它们是用户必须先处理的阻塞，``_stale_locator_state`` 也会因此返回 False；
        2. 存在 stale 待读岗位（事实）→ ``stale``，**压过最近一次失败的码**
           （``channel`` 等）——否则任务页说「定位符过期」、结果页说「连接失败」；
        3. 其余按 ``user_view.failure_kind`` 的结果（含 ``partial`` / ``empty`` / ``none``）。
        """
        kind = user_view.failure_kind(
            stage, run.get("state"), Pipeline._error_code(run), counters,
            last_search_ok=runs_mod._last_search_ok(run["id"]),
            detail_phase=Pipeline._detail_phase(run),
        )
        if kind in ("auth", "risk", "gated"):
            return kind
        if Pipeline._stale_locator_state(run, stage):
            return "stale"
        return kind

    @staticmethod
    def _error_code(run: dict[str, Any]) -> Optional[str]:
        reason = (run.get("stop_reason") or "").strip()
        return reason if reason and reason.isascii() and reason.isupper() else None

    def _find_same_query_run(self, query: str) -> Optional[dict[str, Any]]:
        row = query_one(
            """SELECT * FROM research_run
               WHERE origin=? AND query_text=? AND cancel_requested=0
                 AND stage NOT IN ('completed','cancelled','failed','partial')
               ORDER BY started_at DESC LIMIT 1""",
            (SIMPLE_ORIGIN, query),
        )
        if row and row.get("stage") == "paused":
            counters = jloads(row.get("counters"), {}) or {}
            limits = jloads(row.get("limits"), {}) or {}
            config = jloads(row.get("config_snapshot"), {}) or {}
            if not counters.get("jobs_unique"):
                exhausted = counters.get("search_calls", 0) >= limits.get("max_search_calls", MAX_SEARCH_PAGES)
                changed = (config.get("source") == SIMPLE_SOURCE and
                           (config.get("channel") or {}).get("fingerprint") !=
                           channel_state.freeze_channel().get("fingerprint"))
                if exhausted or changed:
                    return None
        return row

    def _sync_display_scope(self, run: dict, job_type: Optional[str]) -> None:
        """复用既有 run 时把展示口径更新为最新 job_type。

        显式未传 job_type（None）时保留原口径，避免把用户此前的选择清空。
        """
        if job_type is None:
            return
        filters = jloads(run.get("filters"), {}) or {}
        if filters.get("job_type") == job_type:
            return
        filters["job_type"] = job_type
        connection = get_conn()
        connection.execute("UPDATE research_run SET filters=? WHERE id=?",
                           (jdumps(filters), run["id"]))
        connection.commit()

    def _assert_env_ready(self, source: str) -> None:
        if source == SIMPLE_SOURCE:
            try:
                runs_mod._upstream()
            except Exception as exc:  # UpstreamError
                raise PipelineError(getattr(exc, "code", "DEPENDENCY_MISSING"),
                                    getattr(exc, "message", str(exc)),
                                    getattr(exc, "retryable", False),
                                    getattr(exc, "operator_action", "")) from exc
        if not self._ai_ready({"config_snapshot": {"source": source}}):
            raise PipelineError(
                "AI_NOT_CONFIGURED",
                "AI 服务未配置，无法整理岗位正文（已在发起采集前拦截）",
                False,
                "由部署方配置模型后重试；不会回退为规则结果，也不会先发起平台采集",
            )

    @staticmethod
    def _ai_ready(run: dict[str, Any]) -> bool:
        """整理阶段必须有真实模型；显式 demo_mode 才允许规则分析器（仅演示）。"""
        if ai_provider.demo_enabled():
            return True
        return bool(ai_provider.ai_config()["configured"])

    def _login_state(self) -> dict[str, Any]:
        """原始 ``status_view()``（账号事实 + 通道事实）。顶层已 import ``login_bridge``。"""
        return login_bridge.controller.status_view()

    def _login_collectable(self) -> bool:
        """当前能不能真的去采集（只读 DB，绝不触发在线探测）。

        走 ``channel_state.collect_gate()``：账号事实（connected / connected_degraded）
        **加上**当前采集通道是否就绪。顶栏与搜索前置检查共用这一个判定，
        不再用「账号曾验证成功」顶替「通道可用」（设计 §0.3）。
        """
        status = self._login_state()
        gate = status.get("collect_ready")
        if gate is None:   # 老口径兜底：无通道段时只看账号事实
            return _account_connected(status)
        return bool(gate)

    def _resume_intent_if_connected(self, run: dict[str, Any]) -> None:
        """重复点同一个搜索：若「待登录」的意图现在可以执行了，就地放行。

        `_find_same_query_run` 会复用未终态的同 query run，其中包含 `pending_login`
        的那一次；不处理的话用户明明已登录，点搜索却永远退回 pending_login。
        「能执行」= 会话已连接（含降级连接）+ 该 run 有未执行的 search 意图且未被取消。
        TTL 护栏只对自动继续生效，这里用户是显式点击，不受 TTL 限制。
        """
        if (run.get("stage") or "") != "pending_login" or run.get("cancel_requested"):
            return
        if not self._login_collectable():
            return
        checkpoint = jloads(run.get("checkpoint"), {}) or {}
        if checkpoint.get("intent") != "search" or not checkpoint.get("intent_at"):
            return
        self._bind_unstarted_channel(run["id"])
        self._ensure_collectable(run["id"])
        self._set_stage(run["id"], "creating", "正在继续上次的搜索")
        self._spawn_worker(run["id"])

    def _ensure_collectable(self, run_id: str) -> None:
        """把受控暂停的 state 拉回可采集态（不重置任何计数）。"""
        state = self._require_run(run_id)["state"]
        # budget_stopped 也走 resume：只有「还有详情可做」时才放行（见 runs.resume_collection）
        if state in ("auth_required", "risk_stopped", "rate_limited", "stopped", "failed",
                     "budget_stopped"):
            try:
                runs_mod.resume_collection(run_id)
            except runs_mod.RunError as exc:
                raise PipelineError(exc.code, exc.message, exc.retryable,
                                    exc.operator_action) from exc

    @staticmethod
    def _default_city() -> Optional[str]:
        row = query_one(
            "SELECT filters FROM research_run WHERE origin=? ORDER BY started_at DESC LIMIT 1",
            (SIMPLE_ORIGIN,),
        )
        if not row:
            return None
        return (jloads(row["filters"], {}) or {}).get("city")

    @staticmethod
    def _default_city_label() -> str:
        return "来源默认范围"

    def _checkpoint(self, run_id: str) -> dict[str, Any]:
        checkpoint = jloads(self._require_run(run_id).get("checkpoint"), {}) or {}
        checkpoint.setdefault("pages_done", {})
        checkpoint.setdefault("detail_done_refs", [])
        checkpoint.setdefault("snapshot_ids_ready", [])
        checkpoint.setdefault("analyzed_snapshot_ids", [])
        checkpoint.setdefault("directions_built", False)
        return checkpoint

    def _save_checkpoint(self, run_id: str, checkpoint: dict[str, Any]) -> None:
        connection = get_conn()
        connection.execute(
            "UPDATE research_run SET checkpoint=?, stage_updated_at=? WHERE id=?",
            (jdumps(checkpoint), utcnow_iso(), run_id),
        )
        connection.commit()

    def _set_stage(self, run_id: str, stage: str, message: Optional[str] = None) -> None:
        """写当前阶段，并同步维护「真实到达的阶段」``stage_reached``（只增不减）。

        ``stage_reached`` 是失败/暂停时判断「哪些阶段从未执行」的唯一依据：
        它只由真实执行到的阶段推进，绝不由终止态反推。
        """
        row = query_one("SELECT stage_reached FROM research_run WHERE id=?", (run_id,))
        reached = _later_stage((row or {}).get("stage_reached"), stage)
        connection = get_conn()
        if message is None:
            connection.execute(
                """UPDATE research_run SET stage=?, stage_reached=?, stage_updated_at=?
                   WHERE id=?""",
                (stage, reached, utcnow_iso(), run_id),
            )
        else:
            connection.execute(
                """UPDATE research_run SET stage=?, stage_reached=?, stage_updated_at=?,
                     user_message=? WHERE id=?""",
                (stage, reached, utcnow_iso(), message, run_id),
            )
        if stage in ACTIVE_RUN_STAGES:
            connection.execute("UPDATE research_run SET stop_reason=NULL WHERE id=?", (run_id,))
        connection.commit()

    def _cancelled(self, run_id: str) -> bool:
        row = query_one("SELECT cancel_requested, stage FROM research_run WHERE id=?", (run_id,))
        return bool(row) and (bool(row["cancel_requested"]) or row["stage"] == "cancelled")

    def _raise_if_cancelled(self, run_id: str) -> None:
        if self._cancelled(run_id):
            raise _Cancelled()

    def _mark_cancelled(self, run_id: str) -> None:
        run = query_one("SELECT stage FROM research_run WHERE id=?", (run_id,))
        if run and run["stage"] == "cancelled":
            return
        connection = get_conn()
        connection.execute(
            """UPDATE research_run SET stage='cancelled', state='stopped', stage_updated_at=?,
                 user_message=? WHERE id=?""",
            (utcnow_iso(), user_view.CANCELLED_MESSAGE, run_id),
        )
        connection.commit()

    def _settle_failure(self, run_id: str, code: str) -> None:
        if self._cancelled(run_id):
            self._mark_cancelled(run_id)
            return
        mapped = user_view.user_view("paused", None, code, self._counter_bundle(run_id))
        connection = get_conn()
        connection.execute(
            """UPDATE research_run SET stage='paused', stage_updated_at=?, user_message=?,
                 stop_reason=? WHERE id=?""",
            (utcnow_iso(), mapped["user_message"], code, run_id),
        )
        connection.commit()

    def _settle_organize_failure(self, run_id: str, code: str) -> None:
        if self._cancelled(run_id):
            self._mark_cancelled(run_id)
            return
        mapped = user_view.user_view("failed", "failed", code, self._counter_bundle(run_id))
        connection = get_conn()
        connection.execute(
            """UPDATE research_run SET stage='failed', stage_updated_at=?, user_message=?,
                 stop_reason=?, organized_count=?, failed_count=? WHERE id=?""",
            (utcnow_iso(), mapped["user_message"], code, self._organized_count(run_id),
             self._failed_count(run_id), run_id),
        )
        connection.commit()

    def _counter_bundle(self, run_id: str) -> dict[str, int]:
        try:
            run = self._require_run(run_id)
        except PipelineError:
            return {}
        return {
            "collected": int(run["counters"].get("jobs_unique", 0) or 0),
            "organized": self._organized_count(run_id),
            "failed": self._failed_count(run_id),
            "detail_read": int(run["counters"].get("snapshots_complete", 0) or 0),
        }

    @staticmethod
    def _snapshot_ids(run_id: str) -> list[str]:
        rows = query_all(
            """SELECT s.id FROM run_job rj JOIN job_snapshot s ON s.id=rj.snapshot_id
               WHERE rj.run_id=? AND rj.snapshot_id IS NOT NULL
                 AND s.detail_status IN ('complete','partial')
               ORDER BY rj.rowid""",
            (run_id,),
        )
        return [row["id"] for row in rows]

    @staticmethod
    def _collected_count(run_id: str) -> int:
        row = query_one("SELECT COUNT(*) AS n FROM run_job WHERE run_id=?", (run_id,))
        return int(row["n"] or 0)

    @staticmethod
    def _accounting(run_id: str) -> dict:
        rows = query_all(
            """SELECT s.detail_status, rj.analysis_error, a.result_status
               FROM run_job rj LEFT JOIN job_snapshot s ON s.id=rj.snapshot_id
               LEFT JOIN analysis a ON a.id=rj.analysis_id AND a.snapshot_id=rj.snapshot_id
               WHERE rj.run_id=?""", (run_id,))
        return {
            "details_complete": sum(r["detail_status"] == "complete" for r in rows),
            "details_partial": sum(r["detail_status"] == "partial" for r in rows),
            "details_missing": sum(r["detail_status"] in ("missing", "failed", "too_large") for r in rows),
            "analysis_succeeded": sum(bool(r["result_status"]) for r in rows),
            "analysis_failed": sum(bool(r["analysis_error"]) for r in rows),
            "excluded": sum(r["result_status"] == "excluded" for r in rows),
            "insufficient_information": sum(r["result_status"] == "insufficient_information" for r in rows),
            "analysis_pending": sum(r["detail_status"] in ("complete", "partial") and not r["result_status"]
                                    and not r["analysis_error"] for r in rows),
        }

    @staticmethod
    def _organized_count(run_id: str) -> int:
        """有可读分析的岗位数（去重 job）。"""
        row = query_one(
            """SELECT COUNT(DISTINCT rj.job_id) AS n FROM run_job rj
               JOIN analysis a ON a.id=rj.analysis_id AND a.snapshot_id=rj.snapshot_id
               WHERE rj.run_id=? AND a.result_status IN ('findings_found','insufficient_information',
                                                         'needs_review')""",
            (run_id,),
        )
        return int(row["n"] or 0)

    @staticmethod
    def _failed_count(run_id: str) -> int:
        row = query_one(
            """SELECT COUNT(*) AS n FROM run_job WHERE run_id=?
               AND (detail_outcome IN ('failed','missing','too_large') OR analysis_error IS NOT NULL)""",
            (run_id,),
        )
        return int(row["n"] or 0)

    def _recount(self, run_id: str) -> int:
        connection = get_conn()
        connection.execute(
            "UPDATE research_run SET organized_count=?, failed_count=? WHERE id=?",
            (self._organized_count(run_id), self._failed_count(run_id), run_id),
        )
        connection.commit()
        return self._organized_count(run_id)

    @staticmethod
    def _stage_list(current: str, stage_reached: Optional[str] = None,
                    state: Optional[str] = None) -> list[dict[str, str]]:
        """按真实到达点生成阶段条（五态：done/current/failed/skipped/pending）。

        - 终止态（paused/cancelled/failed）：到达点之前 ``done``、到达点 ``failed``、
          之后 ``skipped``——**不再是全部 done**；
        - ``partial``：跑完的部分保留 ``done``（部分失败 ≠ 阶段失败）；
        - ``completed``：全部 ``done``；
        - 进行中：之前 ``done``、当前 ``current``、之后 ``pending``。
        """
        order = STAGE_ORDER
        reached_index = order.index(stage_reached) if stage_reached in order else -1
        result: list[dict[str, str]] = []
        for position, stage in enumerate(order):
            if current in TERMINATED_STAGES:
                if position < reached_index:
                    status = "done"
                elif position == reached_index:
                    status = "failed"
                else:
                    status = "skipped"
            elif current == "partial":
                status = "done" if position <= reached_index else "skipped"
            elif current == "completed":
                status = "done"
            else:
                index = order.index(current) if current in order else 0
                if position < index:
                    status = "done"
                elif position == index:
                    status = "current"
                else:
                    status = "pending"
            result.append({"stage": stage, "label": STAGE_LABELS[stage], "status": status})
        return result


class _Cancelled(Exception):
    pass


# 进程内唯一编排器（app.py 挂到 login_bridge.controller.on_connected）
pipeline = Pipeline()
