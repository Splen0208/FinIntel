# -*- coding: utf-8 -*-
"""社交采集流水线（任务书 §3）：X 海外热点 + 小红书岗位讨论。

- 复用 research_run 表（task_type=x_topics / xhs_career），但不复用 BOSS pipeline 的 worker
  （防止社交任务被旧 worker 当职位任务执行，任务书 §7.1）。
- 阶段：queued → searching → fetching_details → fetching_comments → analyzing → completed。
- 采集 → 脱敏 → SQLite 快照/回执 → AI 分析 → 证据校验 → 结果 API。
"""
from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
import time
from typing import Any, Optional, Sequence

import store
import social_redact
import social_store
from collectors.contracts import CollectorError, ContentRecord, SearchBatch, CommentBatch
from collectors.runtime import redact
from collectors.x_source import XSource, XBrowserSource
from collectors.xhs_source import XhsSource
from store import execute, get_conn, jdumps, new_id, query_all, query_one, utcnow_iso

# 任务类型
TASK_TYPE_BOSS = "boss_research"
TASK_TYPE_X = "x_topics"
TASK_TYPE_XHS = "xhs_career"

# 采集预算（任务书 §5.3 / §6.2）
X_MAX_QUERIES = 2
X_MAX_POSTS_PER_QUERY = 10
X_MAX_UNIQUE_POSTS = 20
XHS_MAX_NOTES = 5
XHS_MAX_COMMENTS_PER_NOTE = 20
XHS_MAX_COMMENTS_TOTAL = 100

# 默认 X 检索词（任务书 §5.1，仅建议，可覆盖）
X_DEFAULT_QUERIES = ["consumer lending", "consumer credit"]

# 停止码（增量设计 §5.1；唯一来源是 collectors.contracts.ERROR_CODES）：
# - 一票否决：出现即停止整条任务，不再对剩余命中发注定失败的请求
#   （DEPENDENCY_MISSING 由 XhsSource._error_from() 在 returncode == -1 时返回）；
# - TIMEOUT：单次超时不等价于风控，连续 TIMEOUT_STOP_AFTER 次才停止，
#   否则最坏会 5×90s + 5×120s 才收敛。
STOP_CODES = {"AUTH_REQUIRED", "AUTH_EXPIRED", "VERIFICATION_REQUIRED", "ENVIRONMENT_RISK",
              "ACCOUNT_RISK", "RATE_LIMITED", "CONNECTION_UNAVAILABLE", "CONTRACT_CHANGED",
              "DEPENDENCY_MISSING", "TIMEOUT"}
TIMEOUT_CODES = frozenset({"TIMEOUT"})
IMMEDIATE_STOP_CODES = frozenset(STOP_CODES - TIMEOUT_CODES)
TIMEOUT_STOP_AFTER = 3

# 队列状态（social_task_item，迁移 017）。pending / partial / failed 三态必须可区分：
# detail_state：pending（未读）/ running（读取中）/ done（读完，具体状态看 detail_status）
#               / skipped（无详情 URL 或停止时按摘要留存）/ failed（读取失败）/
#               need_relocate（locator 失效，待重定位）
QUEUE_PENDING = "pending"
QUEUE_RUNNING = "running"
QUEUE_DONE = "done"
QUEUE_SKIPPED = "skipped"
QUEUE_FAILED = "failed"
QUEUE_NEED_RELOCATE = "need_relocate"
QUEUE_NOT_APPLICABLE = "not_applicable"

# 详情 URL（带 xsec_token）的定位器有效期：只供采集路径内部使用，过期即视为失效。
LOCATOR_TTL_SECONDS = 1800


@contextmanager
def _tx(conn):
    """显式事务。

    ``store.execute`` / ``social_store`` 的工具函数每条语句都会自己 commit，
    因此事务块内**只允许**直接用 ``conn.execute`` 写库（B1 要求"命中全部落库"原子）。
    """
    started = False
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
        started = True
    try:
        yield conn
    except BaseException:
        if started:
            conn.rollback()
        raise
    else:
        if started:
            conn.commit()


def _public_url(url):
    """对外公开 URL（去查询串/锚点）；口径唯一来源 = ``social_redact.public_url``。

    保留本函数名是为了兼容既有调用点与测试；实现不再自己写一份规则。
    """
    return social_redact.public_url(url)


class SocialPipelineError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False,
                 operator_action: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.operator_action = operator_action


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _install_hint(kind: str) -> str:
    """依赖安装提示的**唯一来源** = ``collectors.provider.install_hint``（T08 冻结口径）。

    ``operator_action`` 只能指向**实际存在**的入口/动作，所以这里不自己编文案：
    provider 给什么就转什么，拿不到时给一句不指向幻觉入口的兜底。
    """
    try:
        from collectors.provider import install_hint

        hint = install_hint(kind)
        if hint:
            return str(hint)
    except Exception:  # noqa: BLE001 - 提示文案失败不该拖垮采集主流程
        pass
    return "在「设置」里确认采集依赖已安装，或调用 /api/sources 体检查看依赖状态后重试"


def _resolve_exe(kind: str) -> str:
    """与来源体检共用依赖解析，支持只安装了 Node 入口的 OpenCLI。"""
    from collectors.provider import resolve_provider
    handle = resolve_provider(kind)
    return str(handle.entry_path or handle.exe_path)


class SocialPipeline:
    """社交采集流水线。

    小红书路径（B1/B4）采用**队列驱动**：
    搜索返回 → 在一个事务内把全部命中落库（item + 搜索摘要快照 + locator + 队列行），
    之后详情/评论都通过「条件 UPDATE 领取」逐条从 ``social_task_item`` 取，
    队列是**唯一进度来源**（进程内 payload 仅作载荷缓存，不参与进度判定）。
    因此"页面看到的条数"与"库里的材料条数"始终一致。
    """

    def __init__(self):
        self._workers: dict[str, threading.Thread] = {}

    # ------------------------------------------------------------------
    # 任务创建
    # ------------------------------------------------------------------
    def start_task(self, source: str, keyword: str, *,
                   filters: Optional[dict[str, Any]] = None) -> dict:
        text = (keyword or "").strip()
        if not text:
            raise SocialPipelineError("INVALID_ANALYSIS", "请输入关键词后再搜索", False)
        if source not in ("x", "xiaohongshu") or len(text) > 300:
            raise SocialPipelineError("INVALID_ANALYSIS", "信息源或关键词无效")
        if filters and filters.get("time_window", "7d") not in ("1d", "7d", "30d"):
            raise SocialPipelineError("INVALID_ANALYSIS", "不支持的时间范围")
        gate = query_one("SELECT value FROM app_setting WHERE key='social_cooldown_until'")
        if gate and float(gate["value"]) > time.time():
            raise SocialPipelineError("RATE_LIMITED", "社交采集仍在冷却，已有材料可查看和整理")
        task_type = TASK_TYPE_X if source == "x" else TASK_TYPE_XHS
        run_id = new_id("run")
        name = f"{text} · {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        filters = filters or {}
        # 幂等：同 source + keyword 的未完成任务复用（避免重复点击重复采集）
        existing = self._find_active_task(source, text)
        if existing:
            return self.task_view(existing["id"])
        active = query_one("SELECT id FROM research_run WHERE origin='social' AND state IN ('queued','running') LIMIT 1")
        if active:
            raise SocialPipelineError("RATE_BUSY", "另一个社交采集任务正在运行，请先等待或取消")

        config = {
            "source": source,
            "task_type": task_type,
            "queries": [{"query_id": "q1", "text": text}],
            "filters": filters,
            "name": name,
        }
        execute(
            """INSERT INTO research_run (id, name, config_snapshot, state, started_at,
               counters, limits, query_text, filters, origin, stage, task_type)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, name, jdumps(config), "queued", utcnow_iso(),
             jdumps(self._empty_counters()), jdumps({}), text,
             jdumps(filters), "social", "queued", task_type),
        )
        self._spawn(run_id)
        return self.task_view(run_id)

    def _find_active_task(self, source: str, keyword: str) -> Optional[dict[str, Any]]:
        row = query_one(
            """SELECT id FROM research_run WHERE task_type=? AND query_text=? AND state IN
               ('queued','running','searching','fetching_details','fetching_comments','analyzing')
               ORDER BY started_at DESC LIMIT 1""",
            (TASK_TYPE_X if source == "x" else TASK_TYPE_XHS, keyword),
        )
        return {"id": row["id"]} if row else None

    def _empty_counters(self) -> dict[str, int]:
        """初始计数口径。

        AI 侧只记「分析批次」与「结论主题数」，**禁止**写成"已分析 N 条帖子"：
        - ``ai_batch_success``  AI 完成的分析批次数（一次 analyze = 一个批次）
        - ``ai_topic_count``   该批次产出的结论主题（finding）数
        - ``unique_items``     真正进入本任务结果集的命中条数（与库内材料一致）
        """
        return {
            "unique_items": 0, "detail_complete": 0, "detail_partial": 0,
            "detail_missing": 0, "comments_collected": 0,
            "comment_fetch_failed_items": 0, "ai_batch_success": 0,
            "ai_batch_failed": 0, "ai_batch_pending": 0, "ai_batch_skipped": 0,
            "ai_topic_count": 0,
        }

    def _spawn(self, run_id: str) -> None:
        t = threading.Thread(target=self._run_async, args=(run_id,), daemon=True)
        self._workers[run_id] = t
        t.start()

    def _run_async(self, run_id: str) -> None:
        try:
            asyncio.run(self._worker(run_id))
        finally:
            # worker 跑在自己的线程里，store._local 是线程局部：线程结束必须关连接，
            # 否则每次采集都会泄漏一个 SQLite 连接（WAL 文件不释放）。
            self._close_thread_conn()

    @staticmethod
    def _close_thread_conn() -> None:
        """关闭当前线程自己的 store 连接（只影响本线程，不动别的线程）。"""
        conn = getattr(store._local, "conn", None)
        if conn is None:
            return
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - 收尾阶段不因连接异常中断
            pass
        store._local.conn = None

    # ------------------------------------------------------------------
    # 采集 worker
    # ------------------------------------------------------------------
    async def _worker(self, run_id: str) -> None:
        try:
            # _require_run 移进 try：它在 try 之外抛错时 finally 不执行，
            # self._workers 会永久残留该 run_id，导致任务既不能取消也不能重启。
            run = self._require_run(run_id)
            task_type = run["task_type"]
            if task_type == TASK_TYPE_X:
                await self._collect_x(run_id, run)
            else:
                await self._collect_xhs(run_id, run)
        except CollectorError as exc:
            self._fail(run_id, exc.code, exc.message, exc.operator_action)
        except Exception as exc:  # noqa: BLE001
            self._fail(run_id, getattr(exc, "code", "INTERNAL_ERROR"), "采集未完成，请检查来源连接；已有结果已保存")
        finally:
            self._workers.pop(run_id, None)

    async def _collect_x(self, run_id: str, run: dict[str, Any]) -> None:
        self._set_stage(run_id, "searching", "正在采集 X 热点")
        keyword = run["query_text"]
        queries = [keyword]
        exe = _resolve_exe("x")
        source = XSource(exe)
        collector_version = "twitter-cli"
        seen: set[str] = set()
        all_records: list[ContentRecord] = []
        for q in queries[:X_MAX_QUERIES]:
            try:
                await self._before_request(run_id)
                days = int(run["config_snapshot"].get("filters", {}).get("time_window", "7d")[:-1])
                since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
                try:
                    batch = await source.search(q, max_count=X_MAX_POSTS_PER_QUERY, since=since)
                except CollectorError as primary_error:
                    if primary_error.code not in ("CONTRACT_CHANGED", "CONNECTION_UNAVAILABLE"):
                        raise
                    # 接口兼容/网络故障允许浏览器关键词搜索；认证、验证和限流一律停止。
                    await self._before_request(run_id)
                    source = XBrowserSource(_resolve_exe("xhs"))
                    batch = await source.search(q, max_count=X_MAX_POSTS_PER_QUERY, since=since)
                    collector_version = "opencli-browser"
                    from collectors.provider import resolve_opencli_launch
                    config = dict(run["config_snapshot"])
                    config["provider"] = resolve_opencli_launch().as_snapshot()
                    config["retrieval_mode"] = "keyword_search"
                    config["fallback_reason"] = primary_error.code
                    execute("UPDATE research_run SET config_snapshot=? WHERE id=?", (jdumps(config), run_id))
                import social_source_status
                social_source_status.record_collection("x")
            except CollectorError as exc:
                self._fail(run_id, exc.code, exc.message, exc.operator_action)
                return
            for rec in batch.records:
                if rec.source_id in seen:
                    continue
                seen.add(rec.source_id)
                all_records.append(rec)
                self._persist_x(run_id, rec, keyword=q, collector_version=collector_version)
                self._bump(run_id, "unique_items", 1)
                self._bump(run_id, "detail_" + (rec.detail_status if rec.detail_status in ("complete", "partial") else "missing"), 1)
                if len(all_records) >= X_MAX_UNIQUE_POSTS:
                    break
            if len(all_records) >= X_MAX_UNIQUE_POSTS:
                break

        self._set_stage(run_id, "deduping", "正在去重并保存")

        # AI 分析
        await self._analyze(run_id, run)
        self._finish(run_id)

    async def _collect_xhs(self, run_id: str, run: dict[str, Any]) -> None:
        """小红书采集：命中先全部入队落库，再逐条从队列领取读正文 / 评论。

        B1：搜索返回后，在**一条事务**内把全部命中落库（item + 搜索摘要快照 + locator
        + ``social_task_item`` 队列行），队列是唯一进度来源；进程内 payload 仅作载荷
        缓存，不再承担"读到哪一条"的语义。
        B4：正文状态 complete / partial / missing 三态互斥计数。
        """
        self._set_stage(run_id, "searching", "正在搜索小红书笔记")
        keyword = run["query_text"]
        exe = _resolve_exe("xhs")
        source = XhsSource(exe)
        try:
            await self._before_request(run_id)
            batch = await source.search(keyword, limit=XHS_MAX_NOTES)
            import social_source_status
            social_source_status.record_collection("xiaohongshu")
        except CollectorError as exc:
            self._fail(run_id, exc.code, exc.message, exc.operator_action)
            return

        payloads = self._enqueue_hits(run_id, batch.records, keyword=keyword)

        self._set_stage(run_id, "fetching_details", "正在读取笔记正文")
        state: dict[str, int] = {"timeouts": 0, "detail_read_ok": 0}
        await self._drain_details(run_id, source, keyword=keyword, payloads=payloads,
                                  state=state)

        self._set_stage(run_id, "fetching_comments", "正在读取笔记评论")
        await self._drain_comments(run_id, source, state=state)

        # AI 分析
        await self._analyze(run_id, run)
        self._finish(run_id)

    # ------------------------------------------------------------------
    # 队列驱动（B1）：搜索命中全部落库 → 条件领取 → 逐条更新快照与关联
    # ------------------------------------------------------------------
    def _enqueue_hits(self, run_id: str, records: Sequence[ContentRecord], *,
                      keyword: str) -> dict[str, ContentRecord]:
        """B1：搜索命中在一条事务内全部落库，返回 {队列行 id: 原始命中} 载荷缓存。

        事务内写入：social_item（幂等 upsert）→ social_snapshot（搜索摘要版本）→
        social_locator（带 token 的详情 URL，与对外公开 URL 分离）→ social_task_item。
        落库失败即整体回滚，绝不会出现"计数加了、库里没有"的中间态。
        """
        picked: list[ContentRecord] = []
        seen: set[str] = set()
        for rec in records:
            sid = rec.source_id or rec.canonical_url or ""
            if not sid or sid in seen:
                continue
            seen.add(sid)
            picked.append(rec)
            if len(picked) >= XHS_MAX_NOTES:
                break
        if not picked:
            return {}

        conn = get_conn()
        now = utcnow_iso()
        payloads: dict[str, ContentRecord] = {}
        without_url: list[str] = []
        with _tx(conn):
            for seq, rec in enumerate(picked, start=1):
                item_id = self._upsert_item_tx(
                    conn, rec.platform, rec.source_id, "xhs_note",
                    canonical_url=_public_url(rec.canonical_url),
                    author_key=rec.author_key)
                # 搜索摘要先作为一条可见材料版本落库（重启后仍能还原"命中过什么"）
                self._insert_snapshot_tx(conn, item_id, rec=rec,
                                         detail_status=rec.detail_status or "summary")
                has_url = bool(rec.canonical_url)
                locator_id = (self._upsert_locator_tx(conn, item_id, rec.canonical_url,
                                                      platform=rec.platform)
                              if has_url else None)
                row = conn.execute(
                    "SELECT id FROM social_task_item WHERE run_id=? AND item_id=?",
                    (run_id, item_id)).fetchone()
                if row:
                    queue_id = row["id"]
                else:
                    queue_id = new_id("sti")
                    conn.execute(
                        "INSERT INTO social_task_item (id, run_id, item_id, seq, detail_state, "
                        "comment_state, detail_attempts, comment_attempts, detail_status, "
                        "locator_id, updated_at) VALUES (?,?,?,?,?,?,0,0,?,?,?)",
                        (queue_id, run_id, item_id, seq,
                         QUEUE_PENDING if has_url else QUEUE_SKIPPED,
                         QUEUE_PENDING if has_url else QUEUE_NOT_APPLICABLE,
                         rec.detail_status or "summary", locator_id, now))
                payloads[queue_id] = rec
                if not has_url:
                    without_url.append(queue_id)

        # 没有详情 URL 的命中：正文无从读取，按搜索摘要直接落材料（与旧口径一致：计 missing）
        for queue_id in without_url:
            self._materialize_summary(run_id, queue_id, keyword=keyword)
            self._bump(run_id, "detail_missing", 1)
        return payloads

    def _upsert_item_tx(self, conn, platform: str, source_id: str, kind: str, *,
                        canonical_url: Optional[str], author_key: Optional[str]) -> str:
        """事务内版本的 social_store.upsert_item（口径一致，只是不自动提交）。

        与 store 层同一套清洗：作者键匿名化 + 公开 URL 去查询串（T14 / D4）。
        """
        now = utcnow_iso()
        author_key = social_redact.stable_anon_key(platform, author_key)
        canonical_url = social_redact.public_url(canonical_url)
        row = conn.execute(
            "SELECT id FROM social_item WHERE platform=? AND source_id=?",
            (platform, source_id)).fetchone()
        if row:
            item_id = row["id"]
            conn.execute(
                "UPDATE social_item SET last_seen_at=?, canonical_url=COALESCE(?, canonical_url), "
                "author_key=COALESCE(?, author_key) WHERE id=?",
                (now, canonical_url, author_key, item_id))
            return item_id
        item_id = new_id("sit")
        conn.execute(
            "INSERT INTO social_item (id, platform, source_id, kind, canonical_url, author_key, "
            "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
            (item_id, platform, source_id, kind, canonical_url, author_key, now, now))
        return item_id

    def _insert_snapshot_tx(self, conn, item_id: str, *, rec: ContentRecord,
                            detail_status: str) -> str:
        """事务内版本的不变快照写入。

        - 顺序铁律（T14）：**先脱敏 → 再算 content_hash / identity_hash**；
        - 身份 hash 复用 social_store 的纯函数，口径唯一（不在这里另写一套）。
        """
        raw_text = str(rec.evidence_text or "")
        text = social_redact.redact(raw_text)
        title = social_redact.redact(rec.title)
        was_redacted = 1 if text != raw_text else 0
        row = conn.execute(
            "SELECT platform, source_id, kind FROM social_item WHERE id=?", (item_id,)).fetchone()
        platform = row["platform"] if row else ""
        source_id = row["source_id"] if row else ""
        kind = row["kind"] if row else ""
        ih = social_store.identity_hash(
            platform=platform, source_id=source_id, kind=kind, title=title,
            evidence_text=text, detail_status=detail_status,
            language=rec.language, published_at=rec.published_at,
            collector_version="opencli")
        found = conn.execute(
            "SELECT id FROM social_snapshot WHERE item_id=? AND identity_hash=?",
            (item_id, ih)).fetchone()
        if found:
            return found["id"]  # 同身份版本已存在 → 复用，绝不覆写正文
        snap_id = new_id("ssn")
        conn.execute(
            "INSERT INTO social_snapshot (id, item_id, content_hash, identity_hash, title, "
            "evidence_text, language, published_at, published_at_raw, published_at_quality, "
            "fetched_at, detail_status, collector_version, source_url_verified, "
            "identity_quality, redacted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (snap_id, item_id, social_store.content_hash(text), ih, title,
             text, rec.language, rec.published_at, rec.published_at_raw,
             rec.published_at_quality or "unknown", utcnow_iso(), detail_status,
             "opencli", 1 if rec.source_url_verified else 0, rec.identity_quality,
             was_redacted))
        return snap_id

    def _upsert_locator_tx(self, conn, item_id: str, url: str, *, platform: str) -> str:
        """带 token 的详情 URL 存入 locator（与公开 URL 分离，带 TTL，仅采集路径内读取）。"""
        now = utcnow_iso()
        expires = (datetime.now(timezone.utc)
                   + timedelta(seconds=LOCATOR_TTL_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = conn.execute(
            "SELECT id FROM social_locator WHERE item_id=? AND kind=?",
            (item_id, "xhs_token_url")).fetchone()
        if row:
            locator_id = row["id"]
            conn.execute(
                "UPDATE social_locator SET public_url=?, locator_value=?, expires_at=?, "
                "created_at=? WHERE id=?",
                (_public_url(url), url, expires, now, locator_id))
            return locator_id
        locator_id = new_id("slc")
        conn.execute(
            "INSERT INTO social_locator (id, item_id, platform, kind, public_url, "
            "locator_value, url_verified, expires_at, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (locator_id, item_id, platform, "xhs_token_url", _public_url(url), url,
             0, expires, now))
        return locator_id

    @staticmethod
    def _locator_info(locator_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        """取 (详情 URL, 过期时间)；locator 值只在采集路径内部使用，绝不进对外返回。

        过期（或缺失）的 locator **不得**继续发平台请求：条目置 ``need_relocate``，
        由用户显式「仅重新整理」或重新搜索（禁止悄悄重跑整批搜索）。
        """
        if not locator_id:
            return None, None
        row = query_one("SELECT locator_value, expires_at FROM social_locator WHERE id=?",
                        (locator_id,))
        if not row:
            return None, None
        return row["locator_value"], row["expires_at"]

    @staticmethod
    def _locator_expired(expires_at: Optional[str]) -> bool:
        """过期判定：时间戳为 UTC ISO8601（同长度，可字典序比较）。"""
        if not expires_at:
            return True
        return str(expires_at) <= utcnow_iso()

    def _claim_next(self, run_id: str, phase: str) -> Optional[dict[str, Any]]:
        """条件 UPDATE 领取一条队列行（唯一进度来源）；已被别人领走则返回 None。

        ``phase='detail'``  领"待读正文"的命中；
        ``phase='comment'`` 领"待读评论"的命中（只领正文阶段已经结束的）。
        """
        if phase == "detail":
            sql = ("SELECT id, item_id, seq, locator_id FROM social_task_item "
                   "WHERE run_id=? AND detail_state=? ORDER BY seq LIMIT 1")
            params: tuple = (run_id, QUEUE_PENDING)
            update = ("UPDATE social_task_item SET detail_state=?, "
                      "detail_attempts=detail_attempts+1, updated_at=? "
                      "WHERE id=? AND detail_state=?")
            values = (QUEUE_RUNNING, utcnow_iso(), None, QUEUE_PENDING)
        else:
            sql = ("SELECT id, item_id, seq, locator_id FROM social_task_item "
                   "WHERE run_id=? AND comment_state=? AND detail_state IN (?,?,?,?) "
                   "ORDER BY seq LIMIT 1")
            params = (run_id, QUEUE_PENDING, QUEUE_DONE, QUEUE_FAILED, QUEUE_SKIPPED,
                      QUEUE_RUNNING)
            update = ("UPDATE social_task_item SET comment_state=?, "
                      "comment_attempts=comment_attempts+1, updated_at=? "
                      "WHERE id=? AND comment_state=?")
            values = (QUEUE_RUNNING, utcnow_iso(), None, QUEUE_PENDING)

        row = query_one(sql, params)
        if row is None:
            return None
        values = (values[0], values[1], row["id"], values[3])
        conn = get_conn()
        cur = conn.execute(update, values)
        conn.commit()
        if cur.rowcount != 1:
            return None  # 已被别的 worker 领走 → 本轮不再重复读同一条
        out = dict(row)
        value, expires_at = self._locator_info(row["locator_id"])
        out["locator_value"] = value
        out["locator_expired"] = self._locator_expired(expires_at)
        return out

    def _mark_detail(self, run_id: str, queue_id: str, state: str, *,
                     detail_status: Optional[str] = None,
                     error_code: Optional[str] = None,
                     error_summary: Optional[str] = None) -> None:
        """更新队列行的详情态（pending / running / done / skipped / failed 可区分）。

        错误列用 ``COALESCE``：**成功不抹掉上一次的失败原因**。队列行只有一组
        ``last_error_*`` 列、detail 与 comment 两阶段共用，若成功也写 NULL，
        评论阶段一旦成功就会把"正文读取失败的真实原因"冲掉（用户看不到 why）。
        """
        execute(
            "UPDATE social_task_item SET detail_state=?, "
            "detail_status=COALESCE(?, detail_status), "
            "last_error_code=COALESCE(?, last_error_code), "
            "last_error_summary=COALESCE(?, last_error_summary), updated_at=? WHERE id=?",
            (state, detail_status, error_code,
             redact(str(error_summary))[:200] if error_summary else None,
             utcnow_iso(), queue_id))

    def _mark_comment(self, run_id: str, queue_id: str, state: str, *,
                      error_code: Optional[str] = None,
                      error_summary: Optional[str] = None) -> None:
        """更新队列行的评论态；错误列同样 ``COALESCE``（不冲掉详情阶段的失败原因）。"""
        execute(
            "UPDATE social_task_item SET comment_state=?, "
            "last_error_code=COALESCE(?, last_error_code), "
            "last_error_summary=COALESCE(?, last_error_summary), updated_at=? WHERE id=?",
            (state, error_code,
             redact(str(error_summary))[:200] if error_summary else None,
             utcnow_iso(), queue_id))

    def _materialize(self, run_id: str, item_id: str, snapshot_id: Optional[str], *,
                     keyword: str, metrics: Optional[dict[str, Any]] = None) -> None:
        """把命中挂进任务结果集（写/换 observation，is_current 语义）。

        ``unique_items`` 只在"该 item 第一次进入本任务结果集"时 +1，
        确保页面计数与库内材料条数一致（B1 的缺陷正是两者不一致）。
        """
        existing = query_one(
            "SELECT 1 AS hit FROM social_observation WHERE run_id=? AND item_id=? LIMIT 1",
            (run_id, item_id))
        social_store.insert_observation(
            run_id, item_id, snapshot_id, query_text=keyword,
            retrieval_mode="keyword_search", metrics=metrics or {},
            coverage={"retrieval_mode": "keyword_search"},
            provenance={"collector": "opencli"})
        if not existing:
            self._bump(run_id, "unique_items", 1)

    def _materialize_summary(self, run_id: str, queue_id: str, *, keyword: str) -> None:
        """用已落库的搜索摘要快照把命中挂进结果集（正文无从读取时的兜底材料）。"""
        qrow = query_one("SELECT item_id FROM social_task_item WHERE id=?", (queue_id,))
        if not qrow:
            return
        snap = query_one(
            "SELECT id FROM social_snapshot WHERE item_id=? ORDER BY rowid DESC LIMIT 1",
            (qrow["item_id"],))
        self._materialize(run_id, qrow["item_id"], snap["id"] if snap else None,
                          keyword=keyword)

    def _materialize_failure(self, run_id: str, queue_id: str, item_id: str,
                             payload: Optional[ContentRecord], *, keyword: str) -> None:
        """正文读取失败：把搜索摘要按 ``missing`` 状态落为材料（不覆写已有快照）。"""
        snap_id = None
        if payload is not None:
            snap_id = social_store.insert_snapshot(
                item_id, title=payload.title, evidence_text=payload.evidence_text,
                language=payload.language, published_at=payload.published_at,
                detail_status="missing", collector_version="opencli",
                identity_quality=payload.identity_quality,
                published_at_raw=payload.published_at_raw,
                published_at_quality=payload.published_at_quality)
        else:
            snap = query_one(
                "SELECT id FROM social_snapshot WHERE item_id=? ORDER BY rowid DESC LIMIT 1",
                (item_id,))
            snap_id = snap["id"] if snap else None
        self._materialize(run_id, item_id, snap_id, keyword=keyword,
                          metrics=getattr(payload, "metrics", None) or {})

    def _flush_unread_hits(self, run_id: str, *, keyword: str) -> int:
        """B1 兜底：一条正文都没读成功就被一票否决定停时，把队列里尚未进入结果集的
        命中按搜索摘要补进结果集，保证"页面看到 N 条"与"库里 N 条"一致。

        已经读到至少一条正文时**不补**：没读过的命中不能冒充已采集材料（A-8 语义）。
        队列行保持 ``pending``，后续"仅重新整理/重试"仍可继续读。
        """
        rows = query_all(
            "SELECT id, item_id FROM social_task_item WHERE run_id=? AND detail_state IN (?,?) "
            "ORDER BY seq", (run_id, QUEUE_PENDING, QUEUE_RUNNING))
        kept = 0
        for row in rows:
            if query_one("SELECT 1 AS hit FROM social_observation WHERE run_id=? AND item_id=? "
                         "LIMIT 1", (run_id, row["item_id"])):
                continue
            snap = query_one(
                "SELECT id FROM social_snapshot WHERE item_id=? ORDER BY rowid DESC LIMIT 1",
                (row["item_id"],))
            self._materialize(run_id, row["item_id"], snap["id"] if snap else None,
                              keyword=keyword)
            kept += 1
        return kept

    @staticmethod
    def _detail_counter_key(detail_status: Optional[str]) -> str:
        """B4：complete / partial / missing 三态互斥计数，partial 不再塌缩成 missing。"""
        if detail_status == "complete":
            return "detail_complete"
        if detail_status == "partial":
            return "detail_partial"
        return "detail_missing"

    @staticmethod
    def _should_stop(code: str, consecutive_timeouts: int) -> bool:
        """停止判定：一票否决码出现即停；TIMEOUT 连续 N 次才停。"""
        if code in IMMEDIATE_STOP_CODES:
            return True
        return code in TIMEOUT_CODES and consecutive_timeouts >= TIMEOUT_STOP_AFTER

    async def _drain_details(self, run_id: str, source: Any, *, keyword: str,
                             payloads: dict[str, ContentRecord],
                             state: dict[str, int]) -> None:
        """逐条从队列领取 → 读正文 → 更新快照与任务关联（B1 / B4 / STOP_CODES）。"""
        while True:
            self._check_cancel(run_id)
            qrow = self._claim_next(run_id, "detail")
            if qrow is None:
                return
            payload = payloads.get(qrow["id"])
            if not qrow["locator_value"] or qrow.get("locator_expired"):
                # locator 缺失/过期 → 条目需重新定位：**不发任何平台请求**，
                # 已保存的搜索摘要仍是材料（不丢），并如实标出需要重新定位。
                code = "LOCATOR_EXPIRED" if qrow["locator_value"] else "LOCATOR_MISSING"
                self._materialize_summary(run_id, qrow["id"], keyword=keyword)
                self._bump(run_id, "detail_missing", 1)
                self._mark_detail(run_id, qrow["id"], QUEUE_NEED_RELOCATE,
                                  detail_status="missing", error_code=code)
                continue
            try:
                await self._before_request(run_id)
                rec = await source.detail(qrow["locator_value"])
            except CollectorError as exc:
                state["timeouts"] = (state["timeouts"] + 1 if exc.code in TIMEOUT_CODES
                                     else 0)
                # 读取失败也必须保留材料（旧口径），但不冒充"已读正文"
                self._materialize_failure(run_id, qrow["id"], qrow["item_id"], payload,
                                          keyword=keyword)
                self._bump(run_id, "detail_missing", 1)
                self._mark_detail(run_id, qrow["id"], QUEUE_FAILED, detail_status="missing",
                                  error_code=exc.code, error_summary=exc.message)
                if exc.code == "CANCELLED" or self._should_stop(exc.code, state["timeouts"]):
                    # 一票否决：立即停止，不再对剩余命中发注定失败的请求。
                    # 若一条正文都没读到，把剩余命中按搜索摘要补进结果集，避免
                    # "页面说有 N 条、库里一条都没有"（B1）。
                    if state["detail_read_ok"] == 0 and exc.code != "CANCELLED":
                        self._flush_unread_hits(run_id, keyword=keyword)
                    raise
                continue
            state["timeouts"] = 0
            status = rec.detail_status or "missing"
            snap_id = social_store.insert_snapshot(
                qrow["item_id"], title=rec.title, evidence_text=rec.evidence_text,
                language=rec.language, published_at=rec.published_at,
                detail_status=status, collector_version="opencli",
                identity_quality=rec.identity_quality,
                published_at_raw=rec.published_at_raw,
                published_at_quality=rec.published_at_quality)
            self._materialize(run_id, qrow["item_id"], snap_id, keyword=keyword,
                              metrics=rec.metrics)
            self._bump(run_id, self._detail_counter_key(status), 1)
            self._mark_detail(run_id, qrow["id"], QUEUE_DONE, detail_status=status)
            if status in ("complete", "partial"):
                state["detail_read_ok"] += 1

    async def _drain_comments(self, run_id: str, source: Any,
                              state: dict[str, int]) -> int:
        """逐条从队列领取 → 读评论 → 落库。预算内读取，条数计入 comments_collected。"""
        total = 0
        while total < XHS_MAX_COMMENTS_TOTAL:
            self._check_cancel(run_id)
            qrow = self._claim_next(run_id, "comment")
            if qrow is None:
                break
            if not qrow["locator_value"] or qrow.get("locator_expired"):
                self._mark_comment(run_id, qrow["id"], QUEUE_SKIPPED,
                                   error_code=("LOCATOR_EXPIRED" if qrow["locator_value"]
                                               else "LOCATOR_MISSING"))
                continue
            try:
                await self._before_request(run_id)
                cb = await source.comments(
                    qrow["locator_value"],
                    limit=min(XHS_MAX_COMMENTS_PER_NOTE, XHS_MAX_COMMENTS_TOTAL - total),
                    with_replies=True)
            except CollectorError as exc:
                state["timeouts"] = (state["timeouts"] + 1 if exc.code in TIMEOUT_CODES
                                     else 0)
                self._bump(run_id, "comment_fetch_failed_items", 1)
                self._mark_comment(run_id, qrow["id"], QUEUE_FAILED, error_code=exc.code,
                                   error_summary=exc.message)
                if exc.code == "CANCELLED" or self._should_stop(exc.code, state["timeouts"]):
                    raise
                continue
            state["timeouts"] = 0
            self._persist_comments(run_id, qrow["item_id"], cb)
            total += len(cb.comments)
            self._bump(run_id, "comments_collected", len(cb.comments))
            self._mark_comment(run_id, qrow["id"], QUEUE_DONE)
        return total

    # ------------------------------------------------------------------
    # 入库
    # ------------------------------------------------------------------
    def _persist_x(self, run_id: str, rec: ContentRecord, *, keyword: str,
                   collector_version: str = "twitter-cli") -> None:
        item_id = social_store.upsert_item(
            rec.platform, rec.source_id, "x_post",
            canonical_url=_public_url(rec.canonical_url), author_key=rec.author_key)
        snap_id = social_store.insert_snapshot(
            item_id, title=rec.title, evidence_text=rec.evidence_text,
            language=rec.language, published_at=rec.published_at,
            detail_status=rec.detail_status, collector_version=collector_version,
            identity_quality=rec.identity_quality)
        social_store.insert_observation(
            run_id, item_id, snap_id, query_text=keyword,
            retrieval_mode="keyword_search", metrics=rec.metrics,
            coverage={"time_filter_applied": True},
            provenance={"collector": collector_version})

    def _persist_comments(self, run_id: str, item_id: str, cb: CommentBatch) -> None:
        obs = query_one("SELECT id FROM social_observation WHERE run_id=? AND item_id=? ORDER BY rowid DESC LIMIT 1", (run_id, item_id))
        obs_id = obs["id"] if obs else None
        if obs_id:
            execute("UPDATE social_observation SET coverage_json=? WHERE id=?", (jdumps({
                "reported_total": cb.reported_total, "fetched_count": len(cb.comments),
                "has_more": cb.has_more, "stop_reason": cb.stop_reason}), obs_id))
        for c in cb.comments:
            cid = social_store.upsert_comment(
                item_id, c.source_comment_id, parent_source_id=c.parent_source_id,
                root_source_id=c.root_source_id, author_key=c.author_key,
                parent_missing=c.parent_missing)
            scs_id = social_store.insert_comment_snapshot(
                cid, evidence_text=c.evidence_text, published_at=c.published_at,
                content_status=c.content_status, identity_quality=c.identity_quality)
            social_store.link_run_comment(run_id, scs_id, obs_id)

    # ------------------------------------------------------------------
    # AI 分析
    # ------------------------------------------------------------------
    async def _analyze(self, run_id: str, run: dict[str, Any]) -> None:
        """AI 分析入口；未配置 AI 时跳过但保留内容（任务书 §2.1）。

        计数口径（第 5 处）：AI 只记「分析批次」与「结论主题数」——
        "AI 完成 N 个分析批次，产出 M 个结论主题"，**禁止**写成"已分析 N 条帖子"。
        """
        self._set_stage(run_id, "analyzing", "正在 AI 整理")
        self._check_cancel(run_id)
        import social_analysis
        self._set_counter(run_id, "ai_batch_failed", 0)
        self._set_counter(run_id, "ai_batch_success", 0)
        self._set_counter(run_id, "ai_batch_skipped", 0)
        self._set_counter(run_id, "ai_topic_count", 0)
        try:
            result = (social_analysis.analyze_x(run_id) if run["task_type"] == TASK_TYPE_X
                      else social_analysis.analyze_xhs(run_id))
            status = result.get("status")
            self._set_counter(run_id, "ai_batch_success", int(status == "ok"))
            self._set_counter(run_id, "ai_batch_skipped",
                              int(status == "insufficient_evidence"))
            # 结论主题数 = 本任务当前分析版本实际落库的 finding 数
            self._set_counter(run_id, "ai_topic_count",
                              len(social_store.findings_by_run(run_id)))
        except Exception as exc:
            self._set_counter(run_id, "ai_batch_failed", 1)
            execute("UPDATE research_run SET stop_reason=? WHERE id=?",
                    (getattr(exc, "code", "AI_OUTPUT_INVALID"), run_id))

    def _set_counter(self, run_id, key, value):
        row = query_one("SELECT counters FROM research_run WHERE id=?", (run_id,))
        counters = _jloads(row["counters"], {})
        counters[key] = value
        execute("UPDATE research_run SET counters=? WHERE id=?", (jdumps(counters), run_id))

    def _finish(self, run_id):
        self._check_cancel(run_id)
        c = _jloads(query_one("SELECT counters FROM research_run WHERE id=?", (run_id,))["counters"], {})
        partial = bool(c.get("ai_batch_failed") or c.get("detail_missing")
                       or c.get("comment_fetch_failed_items"))
        stage = "partial" if partial else "completed"
        if partial:
            message = "部分步骤未完成，已保存可用材料"
        elif c.get("ai_batch_success"):
            # 口径：AI 完成 N 个分析批次，产出 M 个结论主题（不是"已分析 N 条帖子"）
            message = (f"整理完成：AI 完成 {int(c.get('ai_batch_success') or 0)} 个分析批次，"
                       f"产出 {int(c.get('ai_topic_count') or 0)} 个结论主题")
        else:
            message = "采集完成；当前材料不足以形成有证据的结论"
        relocate = query_one(
            "SELECT COUNT(*) AS n FROM social_task_item WHERE run_id=? AND detail_state=?",
            (run_id, QUEUE_NEED_RELOCATE))
        need_relocate = int(relocate["n"]) if relocate else 0
        if need_relocate:
            message += f"；其中 {need_relocate} 条定位已失效，需重新定位后才能读取正文"
        execute("UPDATE research_run SET state=?,stage=?,user_message=?,ended_at=? WHERE id=?",
                (stage, stage, message, utcnow_iso(), run_id))

    def _check_cancel(self, run_id):
        if query_one("SELECT cancel_requested FROM research_run WHERE id=?", (run_id,))["cancel_requested"]:
            raise CollectorError("CANCELLED", "已取消，已保存取得的材料")

    async def _before_request(self, run_id):
        self._check_cancel(run_id)
        row = query_one("SELECT value FROM app_setting WHERE key='social_last_request'")
        wait = max(0, 5 - (time.time() - float(row["value"]))) if row else 0
        while wait > 0:
            await asyncio.sleep(min(wait, 0.25))
            self._check_cancel(run_id)
            wait -= 0.25
        execute("INSERT OR REPLACE INTO app_setting(key,value) VALUES('social_last_request',?)", (str(time.time()),))

    def cancel_task(self, run_id, reason="用户取消"):
        self._require_run(run_id)
        execute("UPDATE research_run SET cancel_requested=1,user_message='正在取消，已有材料保留' WHERE id=?", (run_id,))
        if run_id not in self._workers:
            self._fail(run_id, "CANCELLED", "已取消，已有材料保留")
        return self.task_view(run_id)

    def retry_organize(self, run_id):
        run = self._require_run(run_id)
        if run_id in self._workers:
            raise SocialPipelineError("RATE_BUSY", "任务正在执行")
        execute("UPDATE research_run SET cancel_requested=0 WHERE id=?", (run_id,))
        async def organize():
            try:
                await self._analyze(run_id, run)
                self._finish(run_id)
            except CollectorError as exc:
                self._fail(run_id, exc.code, exc.message)
            finally:
                self._workers.pop(run_id, None)
                self._close_thread_conn()  # 重试线程同样必须关掉自己的线程局部连接
        t = threading.Thread(target=lambda: asyncio.run(organize()), daemon=True)
        self._workers[run_id] = t
        t.start()
        return self.task_view(run_id)

    def recover_pending(self):
        execute("""UPDATE research_run SET state='paused',stage='paused',user_message='服务已重启，已有材料保留；可仅重新整理或发起新搜索'
            WHERE origin='social' AND state IN ('queued','running')""")

    # ------------------------------------------------------------------
    # 状态辅助
    # ------------------------------------------------------------------
    def _require_run(self, run_id: str) -> dict[str, Any]:
        row = query_one("SELECT * FROM research_run WHERE id=?", (run_id,))
        if not row or row.get("task_type") not in (TASK_TYPE_X, TASK_TYPE_XHS):
            raise SocialPipelineError("INVALID_ANALYSIS", f"任务 {run_id} 不存在")
        run = dict(row)
        run["config_snapshot"] = _jloads(run.get("config_snapshot"), {})
        run["task_type"] = run.get("task_type") or TASK_TYPE_BOSS
        run["query_text"] = run.get("query_text") or \
            (run["config_snapshot"].get("queries") or [{}])[0].get("text", "")
        return run

    def _set_stage(self, run_id: str, stage: str, message: str) -> None:
        execute("UPDATE research_run SET stage=?, stage_updated_at=?, user_message=?, "
                "state='running' WHERE id=?", (stage, utcnow_iso(), message, run_id))

    def _bump(self, run_id: str, key: str, delta: int = 1) -> None:
        row = query_one("SELECT counters FROM research_run WHERE id=?", (run_id,))
        counters = _jloads(row["counters"], {}) if row else {}
        counters[key] = counters.get(key, 0) + delta
        execute("UPDATE research_run SET counters=? WHERE id=?", (jdumps(counters), run_id))

    def _fail(self, run_id: str, code: str, message: str, action: str = "") -> None:
        import social_source_status
        run = self._require_run(run_id)
        if code in STOP_CODES:
            social_source_status.record_collection(run["config_snapshot"]["source"],
                error_code=code, message=message, operator_action=action)
        if code in ("ENVIRONMENT_RISK", "ACCOUNT_RISK", "VERIFICATION_REQUIRED", "RATE_LIMITED"):
            execute("INSERT OR REPLACE INTO app_setting(key,value) VALUES('social_cooldown_until',?)", (str(time.time()+1800),))
        stage = "cancelled" if code == "CANCELLED" else ("paused" if code in STOP_CODES else "failed")
        execute("UPDATE research_run SET state=?,stage=?,stop_reason=?,user_message=?,ended_at=? WHERE id=?",
                (stage, stage, code, message, utcnow_iso(), run_id))

    # ------------------------------------------------------------------
    # 查询视图
    # ------------------------------------------------------------------
    def task_view(self, run_id: str) -> dict:
        run = self._require_run(run_id)
        return {
            "task": {
                "run_id": run_id,
                "task_type": run["task_type"],
                "source": run["config_snapshot"].get("source"),
                "query": run["query_text"],
                "stage": run.get("stage"),
                "state": run.get("state"),
                "user_message": run.get("user_message"),
                "counters": _jloads(run.get("counters"), {}),
                "started_at": run.get("started_at"),
                "stop_reason": run.get("stop_reason"),
                "actions": ([{"id": "cancel", "label": "取消"}] if run.get("state") in ("queued", "running")
                            else [{"id": "retry-organize", "label": "仅重新整理已保存材料"}]),
            }
        }

    def result_view(self, run_id: str) -> dict:
        run = self._require_run(run_id)
        items = social_store.items_by_run(run_id, limit=100)
        findings = social_store.findings_by_run(run_id)
        return {
            "task_type": run["task_type"],
            "source": run["config_snapshot"].get("source"),
            "query": run["query_text"],
            "counters": _jloads(run.get("counters"), {}),
            "items": items,
            "findings": findings,
            "schema_version": "1.0",
        }

    def history_list(self, limit: int = 20) -> list[dict]:
        return query_all(
            """SELECT id AS run_id, task_type, query_text AS query, state, stage, started_at
               FROM research_run WHERE task_type IN (?,?) ORDER BY started_at DESC LIMIT ?""",
            (TASK_TYPE_X, TASK_TYPE_XHS, limit),
        )


def _jloads(text: Optional[str], default: Any) -> Any:
    if not text:
        return default
    import json
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


pipeline = SocialPipeline()


__all__ = [
    "SocialPipeline", "SocialPipelineError", "pipeline",
    "TASK_TYPE_BOSS", "TASK_TYPE_X", "TASK_TYPE_XHS",
]
