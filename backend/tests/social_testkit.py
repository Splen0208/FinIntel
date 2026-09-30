# -*- coding: utf-8 -*-
"""社交测试隔离脚手架（social_testkit）——**全部为离线 fixture**。

用途：给后续所有社交相关测试提供可复用的隔离环境与假实现，使测试可以
离线判定流水线逻辑（持久化 → AI → 证据校验 → 结果口径），而不需要真实平台。

硬性约定
--------
1. **绝不触碰业务库** ``.local/data/research.sqlite3``：所有数据库操作都发生在
   ``isolated_db()`` 创建的临时目录里，退出即清理。
2. **不发起任何真实网络请求**：采集器用内存 ``FakeCollector``，OpenCLI 用
   ``fake_opencli()`` 打桩子进程，模型用 ``fake_ai()`` 打桩。
3. **假数据可被识别**：所有 source_id / 作者键 / 正文都带 ``offline`` 或
   ``__OFFLINE_FIXTURE__`` 标记，不会被误当作真实采集结果。
4. 引用本模块的测试，在报告中**必须标注「离线」**，不得当作平台可用性的证据。

能力清单
--------
- ``isolated_db()``            临时 SQLite 库 + 全量迁移 + store 连接重定向
- ``with_run()``               在隔离库里插入一条 origin='social' 的 research_run
- ``seed_note()``              直接播种一条笔记材料（item+snapshot+observation）
- ``fake_collector()``         内存假采集器（search/detail/comments 可编程异常）
- ``platform_call_counter()``  统计"真实调用采集器"的次数
- ``snapshot_counter()``       统计隔离库里各类材料行数
- ``fake_ai()`` / ``install_fake_ai()``  可注入假模型（7 种模式）
- ``fake_opencli()``           打桩 OpenCLI 子进程（捕获 argv、返回固定 JSON）
- ``fast_gate()``              跳过 5 秒串行冷却（B3 临时实现），保留取消检查
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import store  # noqa: E402
import social_store  # noqa: E402
import social_pipeline  # noqa: E402
import ai_provider  # noqa: E402
from collectors.contracts import (  # noqa: E402
    CapabilitySet, CollectorError, CommentBatch, CommentRecord, ContentRecord,
    SearchBatch,
)
from collectors.xhs_source import XhsSource  # noqa: E402

# 离线 fixture 统一标记：任何落库/上报的字符串都应带上它，便于一眼识别。
OFFLINE_TAG = "offline"
OFFLINE_MARK = "__OFFLINE_FIXTURE__"

# 真实业务库（只读常量，用于断言"没写进去"）
BUSINESS_DB = store.DEFAULT_DB_PATH


# ---------------------------------------------------------------------------
# 1. 隔离数据库
# ---------------------------------------------------------------------------

@contextmanager
def isolated_db():
    """创建一个临时目录里的全新 SQLite 库并应用全部迁移（001→016）。

    进入时：备份并替换 ``FININTEL_DB``、断开并丢弃 ``store._local`` 上的旧连接。
    退出时：关闭连接、清理临时目录、恢复环境变量。

    业务库 ``.local/data/research.sqlite3`` 全程不被打开、写入或删除。
    """
    prev_env = os.environ.get("FININTEL_DB")
    prev_conn = getattr(store._local, "conn", None)
    tmp = Path(tempfile.mkdtemp(prefix="finintel_isolated_"))
    db_path = tmp / "isolated.sqlite3"

    def _close_own(conn) -> None:
        """只关闭「本上下文自己创建」的连接（否则 Windows 上无法删除临时目录）。"""
        if conn is None:
            return
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - 清理阶段不因连接异常中断
            pass

    def _alive(conn) -> bool:
        """连接是否仍可用。

        ``store._local.conn`` 是线程局部的共享对象：其它测试文件在 **import 期**
        就建好连接并依赖它一路保持打开。若在 teardown 时把已 close 的连接回填，
        后续文件会拿到死连接报 ``Cannot operate on a closed database``；
        若直接置 None 又会让 ``get_conn()`` 重开一个尚未迁移的库（``no such table``）。
        因此：借用期间只解绑不关闭，原样还原；仅在它进入前就已失效时才置 None。
        """
        if conn is None:
            return False
        try:
            conn.execute("SELECT 1").fetchone()
            return True
        except Exception:  # noqa: BLE001 - 探测失败即视为不可用
            return False

    try:
        # 关键：借用期间只把连接解绑，绝不 close 它 —— 它是别的测试模块的。
        store._local.conn = None
        os.environ["FININTEL_DB"] = str(db_path)
        applied = store.apply_migrations()
        assert "016_social_sources" in applied, f"迁移未完整应用：{applied}"
        yield db_path
    finally:
        # 只关闭本上下文创建的临时库连接
        _close_own(getattr(store._local, "conn", None))
        # 原样还原借来的连接；进入前就已失效则置 None，让 get_conn() 重开
        store._local.conn = prev_conn if _alive(prev_conn) else None
        if prev_env is None:
            os.environ.pop("FININTEL_DB", None)
        else:
            os.environ["FININTEL_DB"] = prev_env
        shutil.rmtree(tmp, ignore_errors=True)


def with_run(task_type: str = social_pipeline.TASK_TYPE_XHS,
             query: str = f"{OFFLINE_TAG}关键词",
             source: str = "xiaohongshu",
             *, run_id: Optional[str] = None,
             state: str = "queued") -> str:
    """在隔离库里插入一条 origin='social' 的 research_run，返回 run_id。"""
    from store import execute, jdumps, new_id, utcnow_iso

    run_id = run_id or new_id("run")
    config = {
        "source": source,
        "task_type": task_type,
        "queries": [{"query_id": "q1", "text": query}],
        "filters": {"time_window": "7d"},
        "name": f"{OFFLINE_TAG}:{query}",
        "fixture": True,
    }
    execute(
        """INSERT INTO research_run (id, name, config_snapshot, state, started_at,
           counters, limits, query_text, filters, origin, stage, task_type)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, config["name"], jdumps(config), state, utcnow_iso(),
         jdumps(social_pipeline.SocialPipeline()._empty_counters()), jdumps({}),
         query, jdumps(config["filters"]), "social", state, task_type),
    )
    return run_id


def seed_note(run_id: str, source_id: str, text: str, *,
              title: Optional[str] = None,
              platform: str = "xiaohongshu",
              author_key: str = f"{OFFLINE_TAG}_author",
              detail_status: str = "complete",
              published_at: Optional[str] = "2026-09-20T10:00:00Z",
              canonical_url: Optional[str] = None) -> dict[str, str]:
    """播种一条笔记材料：item + snapshot + observation，返回 id 字典。

    用于"只测 AI 与证据校验"的场景，避免依赖采集器。
    """
    item_id = social_store.upsert_item(
        platform, source_id, "xhs_note",
        canonical_url=canonical_url, author_key=author_key)
    snap_id = social_store.insert_snapshot(
        item_id, title=title, evidence_text=text, language="zh",
        published_at=published_at, detail_status=detail_status,
        collector_version=f"{OFFLINE_TAG}-fixture")
    social_store.insert_observation(
        run_id, item_id, snap_id, query_text=f"{OFFLINE_TAG}",
        retrieval_mode="keyword_search", metrics={},
        provenance={"collector": f"{OFFLINE_TAG}-fixture"})
    return {"item_id": item_id, "snapshot_id": snap_id}


def seed_comment(run_id: str, item_id: str, text: str, *,
                 source_comment_id: Optional[str] = None,
                 author_key: str = f"{OFFLINE_TAG}_user") -> dict[str, str]:
    """播种一条评论材料：comment + comment_snapshot + run 关联。"""
    cid = source_comment_id or ("synthetic_" + hashlib.sha256(
        f"{item_id}|{author_key}|{text}".encode("utf-8")).hexdigest()[:24])
    comment_id = social_store.upsert_comment(
        item_id, cid, parent_source_id=None, root_source_id=None,
        author_key=author_key, parent_missing=False)
    scs_id = social_store.insert_comment_snapshot(
        comment_id, evidence_text=text, published_at="2026-09-20T11:00:00Z",
        content_status="complete")
    social_store.link_run_comment(run_id, scs_id, None)
    return {"comment_id": comment_id, "comment_snapshot_id": scs_id}


def snapshot_counter() -> dict[str, int]:
    """统计当前隔离库里各类社交材料行数（离线口径）。

    用于断言"原始材料未丢""未重复落库""重试不产生第二份快照"。
    """
    def n(table: str) -> int:
        row = store.query_one(f"SELECT COUNT(*) AS n FROM {table}")
        return int(row["n"]) if row else 0

    return {
        "items": n("social_item"),
        "snapshots": n("social_snapshot"),
        "comments": n("social_comment"),
        "comment_snapshots": n("social_comment_snapshot"),
        "observations": n("social_observation"),
        "analyses": n("social_analysis"),
        "findings": n("social_finding"),
        "evidence": n("social_evidence"),
    }


# ---------------------------------------------------------------------------
# 2. 假采集器 + 调用计数
# ---------------------------------------------------------------------------

@dataclass
class CallCounter:
    """记录"真实调用采集器"的次数，用于验证 AI-only 重试不再发起平台请求。"""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, kind: str, **detail: Any) -> None:
        with self.lock:
            self.calls.append((kind, detail))

    def count(self, kind: str) -> int:
        return sum(1 for k, _ in self.calls if k == kind)

    @property
    def total(self) -> int:
        return len(self.calls)

    @property
    def kinds(self) -> list[str]:
        return [k for k, _ in self.calls]

    def reset(self) -> None:
        with self.lock:
            self.calls.clear()


def platform_call_counter() -> CallCounter:
    """新建一个平台调用计数器（离线 fixture，不反映任何真实平台请求）。"""
    return CallCounter()


class FakeCollector:
    """内存假采集器：**离线 fixture，不是真实采集能力**。

    实现 Collector 的 search / detail / comments / capabilities。
    ``detail_plan`` / ``comments_plan`` 是按调用顺序消费的计划队列，元素可以是
    结果对象，也可以是要抛出的 ``CollectorError``（用于模拟风险中断）。
    """

    platform = "xiaohongshu"

    def __init__(self, records: Optional[Sequence[ContentRecord]] = None,
                 comments: Optional[Sequence[CommentRecord]] = None,
                 *, counter: Optional[CallCounter] = None):
        self.records: list[ContentRecord] = list(records or [])
        self.default_comments: list[CommentRecord] = list(comments or [])
        self.detail_plan: list[Any] = []
        self.comments_plan: list[Any] = []
        self.counter = counter or platform_call_counter()

    # -- 计划编排 ---------------------------------------------------------
    def plan_detail(self, *items: Any) -> "FakeCollector":
        """按调用顺序排布 detail 的返回/异常。"""
        self.detail_plan.extend(items)
        return self

    def plan_comments(self, *items: Any) -> "FakeCollector":
        self.comments_plan.extend(items)
        return self

    # -- 采集接口 ---------------------------------------------------------
    def capabilities(self) -> CapabilitySet:
        return CapabilitySet(platform=self.platform, search=True, detail=True,
                             comments=True, replies=True, verified_at=None)

    async def search(self, query: str, *, limit: int = 5, **kwargs: Any) -> SearchBatch:
        self.counter.record("search", query=query)
        return SearchBatch(records=list(self.records),
                           completeness="full" if self.records else "summary_only",
                           coverage={"retrieval_mode": "keyword_search",
                                     "fixture": True})

    async def detail(self, url: str, **kwargs: Any) -> ContentRecord:
        self.counter.record("detail", url=url)
        if self.detail_plan:
            item = self.detail_plan.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        # 默认：把搜索摘要升级为正文完整版（保持 source_id 与 URL 一致）
        match = next((r for r in self.records if r.canonical_url == url), None)
        body = f"{OFFLINE_MARK} 正文：{(match.evidence_text if match else '')}"
        return ContentRecord(
            platform=self.platform,
            source_id=(match.source_id if match else url),
            canonical_url=url,
            title=(match.title if match else None),
            evidence_text=body,
            language="zh",
            published_at=(match.published_at if match else None),
            fetched_at="",
            detail_status="complete",
            author_key=(match.author_key if match else f"{OFFLINE_TAG}_author"),
            metrics={"likes": 1},
        )

    async def comments(self, url: str, *, limit: int = 20,
                       with_replies: bool = True, **kwargs: Any) -> CommentBatch:
        self.counter.record("comments", url=url)
        if self.comments_plan:
            item = self.comments_plan.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return CommentBatch(comments=list(self.default_comments)[:limit],
                            reported_total=len(self.default_comments),
                            root_count=len(self.default_comments),
                            stop_reason="fixture_exhausted")


def fake_collector(records: Optional[Sequence[ContentRecord]] = None,
                   comments: Optional[Sequence[CommentRecord]] = None,
                   *, counter: Optional[CallCounter] = None) -> FakeCollector:
    """构造内存假采集器（离线 fixture）。"""
    return FakeCollector(records=records, comments=comments, counter=counter)


def make_note(source_id: str, text: str, *, title: Optional[str] = None,
              detail_status: str = "summary",
              author_key: str = f"{OFFLINE_TAG}_author",
              url: Optional[str] = None) -> ContentRecord:
    """构造一条离线笔记搜索结果。"""
    return ContentRecord(
        platform="xiaohongshu",
        source_id=source_id,
        canonical_url=url or f"https://www.xiaohongshu.com/explore/{source_id}",
        title=title or f"{OFFLINE_TAG}标题-{source_id}",
        evidence_text=text,
        language="zh",
        published_at="2026-09-20T10:00:00Z",
        fetched_at="",
        detail_status=detail_status,
        author_key=author_key,
        metrics={"likes": 1},
    )


def make_comment(text: str, *, author: str = f"{OFFLINE_TAG}_user",
                 user_id: str = "u1", cid: Optional[str] = None,
                 is_reply: bool = False, reply_to: Optional[str] = None,
                 likes: Optional[int] = 0,
                 time: str = "2026-09-20") -> dict[str, Any]:
    """构造一条原始评论字典（模拟 OpenCLI comments 输出，无可靠 comment_id）。"""
    payload: dict[str, Any] = {
        "author": author, "userId": user_id, "text": text,
        "likes": likes, "time": time,
    }
    if cid:
        payload["comment_id"] = cid
    if is_reply:
        payload["is_reply"] = True
    if reply_to:
        payload["reply_to"] = reply_to   # 只是展示名，不是评论 ID
    return payload


def install_fake_collector(monkeypatch, collector: FakeCollector, *,
                           platform: str = "xhs") -> CallCounter:
    """把假采集器注入 social_pipeline（替换 XhsSource / XSource 与 CLI 定位）。"""
    target = "XhsSource" if platform == "xhs" else "XSource"
    monkeypatch.setattr(social_pipeline, target, lambda exe: collector, raising=True)
    monkeypatch.setattr(social_pipeline, "_resolve_exe",
                        lambda kind: f"C:/{OFFLINE_TAG}/opencli.exe", raising=True)
    return collector.counter


def fast_gate(monkeypatch) -> None:
    """跳过 ``_before_request`` 的 5 秒串行冷却（B3 的临时实现），保留取消检查。

    冷却计时不是本批测试的被测行为，保留它会让用例慢 20s 以上；取消语义保留。
    """
    async def _gate(self, run_id):  # noqa: ANN001
        self._check_cancel(run_id)

    monkeypatch.setattr(social_pipeline.SocialPipeline, "_before_request", _gate,
                        raising=True)


# ---------------------------------------------------------------------------
# 3. 假模型
# ---------------------------------------------------------------------------

_REF_RE = re.compile(
    r"\[(?P<kind>snapshot_id|comment_snapshot_id)=(?P<rid>[^\]]+)\][^\n]*\n"
    r"(?P<text>.*?)(?=\n\n\[|\Z)", re.S)

# 假模型支持的模式
AI_MODES = ("valid", "missing_quote", "empty_topics", "raise",
            "both_ids", "bad_stance", "foreign_id", "context_only")


def _parse_ai_input(user_content: str) -> list[dict[str, Any]]:
    """解析 social_analysis._build_user_content 产出的输入，取回 ref id 与原文。"""
    out: list[dict[str, Any]] = []
    for m in _REF_RE.finditer(user_content):
        out.append({"kind": m.group("kind"), "rid": m.group("rid"),
                    "text": m.group("text")})
    return out


def _quote_of(text: str, n: int = 12) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:n]
    return text.strip()[:n]


def fake_ai(mode: str = "valid"):
    """返回一个可注入的假模型函数（**离线 fixture**）。

    模式：
    - ``valid``         引用真实落在原文内 → 应产出 finding
    - ``missing_quote`` 引文不在原文内 → 应被证据校验拒绝
    - ``empty_topics``  返回空 topics
    - ``raise``         模型调用抛 AIError
    - ``both_ids``      同时给 snapshot_id 与 comment_snapshot_id
    - ``bad_stance``    stance 非法
    - ``foreign_id``    引用不属于本任务的快照 ID
    - ``context_only``  只有 context 证据，没有 support

    返回值带 ``.calls`` 列表，记录每次调用，便于断言"重试是否再次调用模型"。
    """
    if mode not in AI_MODES:
        raise ValueError(f"未知 fake_ai 模式：{mode}（可选：{AI_MODES}）")

    def _chat_json(system_prompt: str, user_content: str, **kwargs: Any) -> Any:
        _chat_json.calls.append({"mode": mode, "user_content": user_content})
        if mode == "raise":
            raise ai_provider.AIError("AI_TIMEOUT", "离线假模型：模拟调用失败",
                                      True, "稍后重试")
        refs_src = _parse_ai_input(user_content)
        if mode == "empty_topics" or not refs_src:
            return {"topics": []}
        refs: list[dict[str, Any]] = []
        for item in refs_src:
            if mode == "both_ids":
                refs.append({"snapshot_id": item["rid"],
                             "comment_snapshot_id": "scs_does_not_exist",
                             "quote": _quote_of(item["text"]), "stance": "support"})
                continue
            if mode == "foreign_id":
                refs.append({"snapshot_id": "ssn_foreign_from_another_run",
                             "quote": _quote_of(item["text"]), "stance": "support"})
                continue
            if mode == "bad_stance":
                refs.append({"snapshot_id": item["rid"],
                             "quote": _quote_of(item["text"]), "stance": "maybe"})
                continue
            if mode == "context_only":
                refs.append({"snapshot_id": item["rid"],
                             "quote": _quote_of(item["text"]), "stance": "context"})
                continue
            quote = _quote_of(item["text"]) if mode == "valid" \
                else "__这段引文根本不在原文里__"
            ref = {"quote": quote, "stance": "support"}
            ref[item["kind"]] = item["rid"]
            refs.append(ref)
        return {
            "topics": [{
                "title": f"{OFFLINE_TAG}主题",
                "summary_zh": f"{OFFLINE_MARK} 离线 fixture 摘要",
                "evidence_refs": refs,
                "limitations": ["仅代表本次采集样本（离线 fixture）"],
            }]
        }

    _chat_json.calls = []  # type: ignore[attr-defined]
    _chat_json.mode = mode  # type: ignore[attr-defined]
    return _chat_json


def install_fake_ai(monkeypatch, mode: str = "valid"):
    """把假模型注入 ai_provider.chat_json，返回假函数（带 .calls）。"""
    fn = fake_ai(mode)
    monkeypatch.setattr(ai_provider, "chat_json", fn, raising=True)
    return fn


# ---------------------------------------------------------------------------
# 4. OpenCLI 子进程打桩
# ---------------------------------------------------------------------------

class _FakeProcess:
    """最小 subprocess 替身：只提供 run_command 用到的 communicate/kill。"""

    def __init__(self, stdout: str, stderr: str, returncode: int):
        self._out = stdout.encode("utf-8")
        self._err = stderr.encode("utf-8")
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return (self._out, self._err)

    def kill(self) -> None:
        self.returncode = -9


class SubprocessRecorder:
    """记录打桩子进程收到的 argv 与已消费的响应数。"""

    def __init__(self):
        self.argv_list: list[list[str]] = []
        self.responses: list[Any] = []

    @property
    def count(self) -> int:
        return len(self.argv_list)

    @property
    def last_argv(self) -> list[str]:
        return self.argv_list[-1]


def ok_json(payload: Any, *, returncode: int = 0, stderr: str = "") -> dict[str, Any]:
    """构造一个"命令成功 + stdout 是给定 JSON"的打桩响应。"""
    return {"stdout": json.dumps(payload, ensure_ascii=False),
            "stderr": stderr, "returncode": returncode}


def fake_opencli(monkeypatch, responses: Iterable[Any]) -> SubprocessRecorder:
    """打桩 ``asyncio.create_subprocess_exec``，**不启动任何真实进程**。

    ``responses`` 是按调用顺序消费的队列，元素可以是：
    - ``{"stdout":..., "stderr":..., "returncode":...}``
    - 或要抛出的异常实例（如 ``FileNotFoundError``）
    """
    rec = SubprocessRecorder()
    queue = list(responses)

    async def _create_subprocess_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
        rec.argv_list.append(list(argv))
        if not queue:
            raise AssertionError(f"打桩响应已耗尽，多余的 argv={list(argv)}")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return _FakeProcess(item.get("stdout", ""), item.get("stderr", ""),
                            int(item.get("returncode", 0)))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess_exec,
                        raising=True)
    return rec


def fake_cli_path(tmp_path) -> str:
    """造一个存在的假可执行文件，供 run_command 的 Path.resolve() 使用。"""
    exe = tmp_path / "opencli.exe"
    exe.write_text("", encoding="utf-8")
    return str(exe)


def xhs_source(exe: str) -> XhsSource:
    """构造小红书采集源（配合 fake_opencli 使用，全程离线）。"""
    return XhsSource(exe)


__all__ = [
    "OFFLINE_TAG", "OFFLINE_MARK", "BUSINESS_DB",
    "isolated_db", "with_run", "seed_note", "seed_comment", "snapshot_counter",
    "CallCounter", "platform_call_counter", "FakeCollector", "fake_collector",
    "make_note", "make_comment", "install_fake_collector", "fast_gate",
    "AI_MODES", "fake_ai", "install_fake_ai",
    "SubprocessRecorder", "ok_json", "fake_opencli", "fake_cli_path", "xhs_source",
]
