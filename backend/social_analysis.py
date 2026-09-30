# -*- coding: utf-8 -*-
"""社交 AI 分析（任务书 §10 / 指令 E4）：X 议题 + 小红书主题，分批、可追溯、证据可校验。

设计要点（E4：AI 输入预算和批次）
---------------------------------
1. **有界批次**：按字符预算 :data:`MAX_BATCH_CHARS` 与件数上限
   :data:`MAX_ITEMS_PER_BATCH` 把输入切成多批。单条超长内容**单独成批并被截断并标注**，
   不会因为"一条过长"就让整批失败。
2. **批次回执**：每一批在 ``social_analysis_batch`` 落一行，记录
   输入 hash / 模型 / prompt 版本 / 结果 hash / 错误码 / 耗时 / 批次序号 / 覆盖条数 /
   输入字符数。回调缺失会让人无法判断"AI 到底跑了什么"。
3. **单批失败不牵连其他批**：一批失败只标记该批；其余批次的结论保留。
   任务整体按 ``partial`` 语义收口（写入 ``ai_batch_failed`` 计数，见
   :func:`_record_partial_failure`）。
4. **引用只允许指向本批次输入**：用
   :func:`social_store.save_evidence` 的 ``allowed_snapshot_ids`` /
   ``allowed_comment_snapshot_ids`` 白名单强制，跨批次引用一律被拒绝（进隔离区）。
5. **缓存 hash 严谨**：``input_hash`` 覆盖**决定语义的输入与版本**——
   prompt 文件内容 hash、schema 版本、模型标签、批内每条 ``(kind,id,identity)``
   与批次序号。只拼接正文的 hash 会在 prompt 改写后错误命中旧结论。
6. **结构合法 ≠ 语义成立**：模型自述的作者数 / 提及数不作统计事实；
   ``independent_sources`` 由服务端按引用快照去重得出。
7. **结论版本口径**：每次分析留一行审计，用 :func:`social_store.set_current_analysis`
   明确任务采用哪个版本；旧版本保留但不与当前版本混成一份结果。

所有模型交互由调用方注入（``ai_provider.chat_json``），测试一律离线打桩。
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Optional

import ai_provider
import social_store
from store import execute, jdumps, jloads, new_id, query_all, query_one, utcnow_iso

PROMPT_X = "social_x_v1"
PROMPT_XHS = "social_xhs_v1"
SCHEMA_VERSION = "1.0"

#: 单个批次允许的最大字符数（含块头）。超出即切批。
MAX_BATCH_CHARS = 12000
#: 单个批次最多容纳的输入条数（快照 + 评论快照）。
MAX_ITEMS_PER_BATCH = 12
#: 单条超长内容被截断时附加的标注（明确告知模型与审计者"这不是完整原文"）。
TRUNCATION_MARK = "\n…[本批已按字符预算截断；完整原文以落库快照为准]"

#: 渲染块之间的分隔符（与假模型解析器约定一致）。
_BLOCK_SEP = "\n\n"


# ---------------------------------------------------------------------------
# prompt / hash 工具
# ---------------------------------------------------------------------------

def _prompt(name: str) -> str:
    """读取 prompt 文件正文（缓存 hash 与实际调用都必须用同一份内容）。"""
    p = Path(__file__).resolve().parent / "prompts" / f"{name}.md"
    return p.read_text(encoding="utf-8")


def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        jdumps(value).encode("utf-8")).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _model_label() -> str:
    cfg = ai_provider.ai_config()
    return f"{cfg['provider']}/{cfg['model']}"


# ---------------------------------------------------------------------------
# 输入读取（只取本任务的"当前"快照）
# ---------------------------------------------------------------------------

def _run_snapshots(run_id: str) -> list[dict[str, Any]]:
    """本任务的**当前**快照（只取 ``is_current=1`` 的观察，T03 遗留项）。

    同一 item 被多次命中时，``social_observation`` 会保留多行历史，
    只有当前行指向的快照才允许进入 AI 输入，避免把新旧正文一起发模型。
    """
    return query_all(
        """SELECT DISTINCT ss.id AS snapshot_id, ss.evidence_text, ss.identity_hash,
                  ss.published_at, ss.detail_status,
                  si.platform, si.source_id, si.author_key
           FROM social_observation so
           JOIN social_snapshot ss ON ss.id = so.snapshot_id
           JOIN social_item si ON si.id = ss.item_id
           WHERE so.run_id=? AND so.is_current=1
           ORDER BY ss.rowid""",
        (run_id,),
    )


def _run_comments(run_id: str) -> list[dict[str, Any]]:
    """本任务实际读取过的评论快照（带 content_hash 供缓存键使用）。"""
    return query_all(
        """SELECT scs.id AS comment_snapshot_id, scs.evidence_text, scs.content_hash,
                  scs.published_at, si.source_id, si.platform
           FROM run_social_comment rsc
           JOIN social_comment_snapshot scs ON scs.id = rsc.comment_snapshot_id
           JOIN social_comment sc ON sc.id = scs.comment_id
           JOIN social_item si ON si.id = sc.item_id
           WHERE rsc.run_id=? ORDER BY scs.rowid""",
        (run_id,),
    )


# ---------------------------------------------------------------------------
# 分批（build_batches）
# ---------------------------------------------------------------------------

def _unit_from_snapshot(s: dict[str, Any]) -> dict[str, Any]:
    header = (f"[snapshot_id={s['snapshot_id']}] platform={s['platform']} "
              f"source={s['source_id']} published={s.get('published_at')} "
              f"status={s['detail_status']}\n")
    return {"kind": "snapshot", "id": s["snapshot_id"],
            "identity": s.get("identity_hash") or "", "header": header,
            "text": s.get("evidence_text") or ""}


def _unit_from_comment(c: dict[str, Any]) -> dict[str, Any]:
    header = (f"[comment_snapshot_id={c['comment_snapshot_id']}] "
              f"source={c['source_id']} published={c.get('published_at')}\n")
    return {"kind": "comment", "id": c["comment_snapshot_id"],
            "identity": c.get("content_hash") or "", "header": header,
            "text": c.get("evidence_text") or ""}


def _empty_batch(index: int) -> dict[str, Any]:
    return {"index": index, "blocks": [], "char_count": 0, "item_count": 0,
            "snapshot_ids": [], "comment_snapshot_ids": [], "units": [],
            "truncated": False}


def _add_unit_full(batch: dict[str, Any], unit: dict[str, Any]) -> None:
    block = unit["header"] + unit["text"]
    if batch["blocks"]:
        batch["char_count"] += len(_BLOCK_SEP)
    batch["char_count"] += len(block)
    batch["blocks"].append(block)
    batch["item_count"] += 1
    batch["units"].append(unit)
    if unit["kind"] == "snapshot":
        batch["snapshot_ids"].append(unit["id"])
    else:
        batch["comment_snapshot_ids"].append(unit["id"])


def _add_unit_truncated(batch: dict[str, Any], unit: dict[str, Any]) -> None:
    """单条超长内容：独立成批，正文截断并显式标注。"""
    header = unit["header"]
    budget = MAX_BATCH_CHARS - len(header) - len(TRUNCATION_MARK)
    if budget < 0:
        budget = 0
    block = header + unit["text"][:budget] + TRUNCATION_MARK
    batch["char_count"] = len(block)
    batch["blocks"].append(block)
    batch["item_count"] = 1
    batch["units"].append(unit)
    batch["truncated"] = True
    if unit["kind"] == "snapshot":
        batch["snapshot_ids"].append(unit["id"])
    else:
        batch["comment_snapshot_ids"].append(unit["id"])


def build_batches(snapshots: list[dict[str, Any]],
                  comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把输入切成有界批次。

    规则（贪心）：
    - 逐条加入当前批；当再加入会使 ``char_count`` 超过 :data:`MAX_BATCH_CHARS`
      或件数达到 :data:`MAX_ITEMS_PER_BATCH` 时，封批并开新批。
    - 单条自身就超过预算 → **独占一批**，正文截断并附 :data:`TRUNCATION_MARK`。

    返回的每个批次含 ``index / content(字符串) / char_count / item_count /
    snapshot_ids / comment_snapshot_ids / units / truncated``。
    """
    units = ([_unit_from_snapshot(s) for s in snapshots]
             + [_unit_from_comment(c) for c in comments])
    batches: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None
    for unit in units:
        block_len = len(unit["header"]) + len(unit["text"])
        if cur is None:
            cur = _empty_batch(len(batches))
        sep = len(_BLOCK_SEP) if cur["blocks"] else 0
        if cur["blocks"] and (cur["item_count"] >= MAX_ITEMS_PER_BATCH
                              or cur["char_count"] + sep + block_len > MAX_BATCH_CHARS):
            batches.append(cur)
            cur = _empty_batch(len(batches))
            sep = 0
        if block_len + sep > MAX_BATCH_CHARS:
            # 单条超预算 → 独占一批（不因一条超长而整批失败）
            _add_unit_truncated(cur, unit)
            batches.append(cur)
            cur = None
            continue
        _add_unit_full(cur, unit)
    if cur is not None:
        batches.append(cur)
    for b in batches:
        b["content"] = _BLOCK_SEP.join(b["blocks"])
    return batches


# ---------------------------------------------------------------------------
# 证据校验
# ---------------------------------------------------------------------------

def _validate_evidence_refs(refs: list[Any], *, text_by_snapshot: dict[str, str],
                            text_by_comment: dict[str, str]) -> list[dict[str, Any]]:
    """服务端校验 quote 是否真是原文子串，并补 start/end（Unicode code point 偏移）。

    只做"结构 + 引用对得上原文"的校验；**批次范围**约束交给
    :func:`social_store.save_evidence` 的白名单参数执行。
    """
    valid: list[dict[str, Any]] = []
    for ref in refs if isinstance(refs, list) else []:
        if not isinstance(ref, dict):
            continue
        quote = str(ref.get("quote") or "")
        sid = ref.get("snapshot_id")
        cid = ref.get("comment_snapshot_id")
        stance = ref.get("stance") or "support"
        if bool(sid) == bool(cid) or stance not in ("support", "contradict", "context"):
            continue
        text = None
        if sid and sid in text_by_snapshot:
            text = text_by_snapshot[sid]
        elif cid and cid in text_by_comment:
            text = text_by_comment[cid]
        if text is None or not quote:
            continue
        start = text.find(quote)
        if start < 0:
            continue  # 引用对不上原文，丢弃（不产出无据结论）
        valid.append({
            "snapshot_id": sid, "comment_snapshot_id": cid,
            "quote": quote, "start": start, "end": start + len(quote),
            "stance": stance,
        })
    return valid


def _ref_in_batch(ref: dict[str, Any], allowed_snapshot: set[str],
                  allowed_comment: set[str]) -> bool:
    """该引用是否落在本批次允许的输入集合内。"""
    sid = ref.get("snapshot_id")
    cid = ref.get("comment_snapshot_id")
    if sid:
        return sid in allowed_snapshot
    if cid:
        return cid in allowed_comment
    return False


# ---------------------------------------------------------------------------
# 批次回执 / 缓存
# ---------------------------------------------------------------------------

def _analysis_input_hash(batch_hashes: list[str]) -> str:
    """整次分析的输入 hash：覆盖每一批的 hash（内含 prompt/schema/model/批内清单）。

    prompt 文件内容一变 → 批 hash 变 → 整次 hash 变 → 缓存失效（重新分析）。
    """
    return _sha256_json({"batches": batch_hashes})


def _batch_input_hash(batch: dict[str, Any], *, prompt_hash: str,
                      model_label: str) -> str:
    """单批输入 hash：批序号 + prompt 内容 hash + schema + 模型 + 批内清单（去重键）。

    ``identity`` 是该条材料决定语义的身份（快照 identity_hash / 评论 content_hash）：
    正文相同但状态不同 → 身份不同 → 缓存不命中。
    """
    payload = {
        "batch_index": batch["index"],
        "prompt_hash": prompt_hash,
        "schema_version": SCHEMA_VERSION,
        "model_label": model_label,
        "items": [[u["kind"], u["id"], u["identity"]] for u in batch["units"]],
    }
    return _sha256_json(payload)


def _save_batch_receipt(analysis_id: str, batch: dict[str, Any], *, input_hash: str,
                        model_label: str, prompt_version: str, status: str,
                        error_code: Optional[str], elapsed_ms: Optional[int],
                        result_hash: Optional[str]) -> None:
    """每批落一行回执（``social_analysis_batch``）。"""
    execute(
        "INSERT OR REPLACE INTO social_analysis_batch (id, analysis_id, batch_index, "
        "input_hash, input_char_count, item_count, model_label, prompt_version, status, "
        "error_code, elapsed_ms, result_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (new_id("sab"), analysis_id, batch["index"], input_hash, batch["char_count"],
         batch["item_count"], model_label, prompt_version, status, error_code,
         elapsed_ms, result_hash, utcnow_iso()),
    )


def _find_cached_batch(input_hash: str) -> Optional[dict[str, Any]]:
    """按输入 hash 复用**此前成功**的批次（不重复调用模型）。"""
    return query_one(
        """SELECT sab.analysis_id AS analysis_id, sab.result_hash AS result_hash
           FROM social_analysis_batch sab
           JOIN social_analysis sa ON sa.id = sab.analysis_id
           WHERE sab.input_hash=? AND sab.status='ok' AND sa.status='ok'
           ORDER BY sab.rowid DESC LIMIT 1""",
        (input_hash,),
    )


def _copy_findings_from_cache(src_analysis_id: str, dst_analysis_id: str,
                              run_id: str, batch: dict[str, Any]) -> int:
    """把缓存批次的分析结论原样复制到本次分析（新的 finding/evidence 行）。

    只复制"证据全部落在本批次输入内"的结论，避免把跨批次内容错绑到本批。
    """
    allowed_s = set(batch["snapshot_ids"])
    allowed_c = set(batch["comment_snapshot_ids"])
    count = 0
    for f in query_all(
            "SELECT id, kind, title, summary_zh, limitations_json FROM social_finding "
            "WHERE analysis_id=?", (src_analysis_id,)):
        ev = query_all(
            "SELECT snapshot_id, comment_snapshot_id, quote, start, end, stance "
            "FROM social_evidence WHERE finding_id=?", (f["id"],))
        if not ev:
            continue
        if not all((e["snapshot_id"] in allowed_s if e["snapshot_id"]
                    else e["comment_snapshot_id"] in allowed_c) for e in ev):
            continue
        if not any(e["stance"] == "support" for e in ev):
            continue
        new_fid = social_store.save_finding(
            dst_analysis_id, kind=f["kind"], title=f["title"],
            summary_zh=f["summary_zh"], limitations=jloads(f["limitations_json"], {}))
        for e in ev:
            social_store.save_evidence(
                new_fid, quote=e["quote"], start=e["start"], end=e["end"],
                stance=e["stance"], snapshot_id=e["snapshot_id"],
                comment_snapshot_id=e["comment_snapshot_id"], run_id=run_id,
                allowed_snapshot_ids=allowed_s, allowed_comment_snapshot_ids=allowed_c)
        count += 1
    return count


# ---------------------------------------------------------------------------
# 结论落库
# ---------------------------------------------------------------------------

def _save_topics(analysis_id: str, run_id: str, result: dict[str, Any], *,
                 batch: dict[str, Any], authors: dict[str, str],
                 gtexts: dict[str, str], gctexts: dict[str, str]) -> tuple[int, int]:
    """把一批模型输出里的合法主题落库，返回 ``(保存的结论数, 被拒引用数)``。

    批次范围铁律：证据只用本批次的快照 id（白名单）；
    跨批次引用即使 quote 对得上原文，也会被 :func:`social_store.save_evidence` 拒绝。
    """
    allowed_s = set(batch["snapshot_ids"])
    allowed_c = set(batch["comment_snapshot_ids"])
    saved_topics = 0
    rejected_refs = 0
    for topic in (result.get("topics") if isinstance(result, dict) else None) or []:
        if not isinstance(topic, dict):
            continue
        refs_all = _validate_evidence_refs(
            topic.get("evidence_refs"), text_by_snapshot=gtexts, text_by_comment=gctexts)
        allowed_refs = [r for r in refs_all if _ref_in_batch(r, allowed_s, allowed_c)]
        rejected_refs += len(refs_all) - len(allowed_refs)
        # 没有可用 support 证据 → 不产出结论（无据结论不入库）
        if not allowed_refs or not any(r["stance"] == "support" for r in allowed_refs):
            continue
        ids = {r["snapshot_id"] for r in allowed_refs if r["snapshot_id"]}
        independent = len({authors[i] for i in ids if authors.get(i)})
        limitations = {
            "sample_limitations": topic.get("limitations") or ["仅代表本次采集样本"],
            "independent_sources": independent,
            "evidence_items": len(ids),
            "evidence_comments": len({r["comment_snapshot_id"] for r in allowed_refs
                                      if r["comment_snapshot_id"]}),
            "disclaimer": "independent_sources 由服务端按引用快照去重得出；"
                          "模型自述的 mention_counts / independent_sources 不作统计事实",
        }
        fid = social_store.save_finding(
            analysis_id,
            kind=("topic" if batch["comment_snapshot_ids"]
                  or (len(ids) >= 3 and independent >= 2) else "lead"),
            title=str(topic.get("title") or "未命名主题"),
            summary_zh=str(topic.get("summary_zh") or ""), limitations=limitations)
        saved_refs = 0
        for ref in refs_all:
            try:
                social_store.save_evidence(
                    fid, run_id=run_id, allowed_snapshot_ids=allowed_s,
                    allowed_comment_snapshot_ids=allowed_c, **ref)
                saved_refs += 1
            except ValueError:
                # 跨批次 / 非法引用被白名单拒绝（隔离区已记录），丢弃该条引用。
                continue
        if saved_refs == 0:
            # 保守兜底：理论不可达（allowed_refs 非空）。不保留无证据结论。
            continue
        saved_topics += 1
    return saved_topics, rejected_refs


def _run_batch(analysis_id: str, run_id: str, prompt_text: str, prompt_hash: str,
               model_label: str, batch: dict[str, Any], *, authors: dict[str, str],
               gtexts: dict[str, str], gctexts: dict[str, str],
               ) -> dict[str, Any]:
    """执行单批：先查缓存，未命中才调用模型，随后落库结论。

    返回 ``{status, findings, rejected_refs, result_hash, elapsed_ms, reused}``。
    失败时**抛出**异常（由调用方统一记为 failed 回执），不影响其他批。
    """
    bhash = _batch_input_hash(batch, prompt_hash=prompt_hash, model_label=model_label)
    cached = _find_cached_batch(bhash)
    if cached:
        count = _copy_findings_from_cache(
            cached["analysis_id"], analysis_id, run_id, batch)
        return {"status": "ok" if count else "insufficient_evidence",
                "findings": count, "rejected_refs": 0,
                "result_hash": cached["result_hash"], "elapsed_ms": 0, "reused": True}

    started = _now_ms()
    result = ai_provider.chat_json(prompt_text, batch["content"])
    elapsed = _now_ms() - started
    if not isinstance(result, dict) or not isinstance(result.get("topics"), list):
        raise ai_provider.AIError("AI_OUTPUT_INVALID", "模型缺少 topics 列表")
    count, rejected = _save_topics(
        analysis_id, run_id, result, batch=batch, authors=authors,
        gtexts=gtexts, gctexts=gctexts)
    return {"status": "ok" if count else "insufficient_evidence",
            "findings": count, "rejected_refs": rejected,
            "result_hash": _sha256_json(result.get("topics") or []),
            "elapsed_ms": elapsed, "reused": False}


# ---------------------------------------------------------------------------
# 任务收口：批次失败计数（让 _finish 判 partial）
# ---------------------------------------------------------------------------

def _record_partial_failure(run_id: str, failed_batches: int) -> None:
    """把失败批次数写进任务计数，使终态按 ``partial`` 语义收口。

    调用方（``social_pipeline._analyze``）会在调用前重置 ``ai_batch_failed``，
    而 ``_finish`` 依据该计数判定 partial；这里在失败批次存在时补正，
    保证"一批失败、其余成功"时任务状态是 partial 而不是 completed。
    """
    row = query_one("SELECT counters FROM research_run WHERE id=?", (run_id,))
    if not row:
        return
    counters = jloads(row["counters"], {})
    counters["ai_batch_failed"] = int(failed_batches)
    execute("UPDATE research_run SET counters=? WHERE id=?", (jdumps(counters), run_id))


def _result_dict(analysis_id: str, status: str, *, reused: bool) -> dict[str, Any]:
    """汇总一次分析的对外结果（批次口径 + 当前结论数）。"""
    stats = query_one(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok,
                  SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                  SUM(item_count) AS items
           FROM social_analysis_batch WHERE analysis_id=?""", (analysis_id,))
    findings = query_all(
        "SELECT id FROM social_finding WHERE analysis_id=?", (analysis_id,))
    total = int(stats["total"] or 0) if stats else 0
    return {
        "status": status,
        "analysis_id": analysis_id,
        "reused": reused,
        "batches_total": total,
        "batches_ok": int(stats["ok"] or 0) if stats else 0,
        "batches_failed": int(stats["failed"] or 0) if stats else 0,
        "input_item_count": int(stats["items"] or 0) if stats else 0,
        "findings": len(findings),
    }


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def analyze_x(run_id: str) -> dict[str, Any]:
    return _analyze(run_id, PROMPT_X, include_comments=False)


def analyze_xhs(run_id: str) -> dict[str, Any]:
    return _analyze(run_id, PROMPT_XHS, include_comments=True)


def _analyze(run_id: str, prompt: str, *, include_comments: bool) -> dict[str, Any]:
    """分析入口：分批 → 逐批执行（失败隔离）→ 明确当前版本 → 收口计数。"""
    snapshots = [s for s in _run_snapshots(run_id)
                 if (s.get("evidence_text") or "").strip()
                 and s["detail_status"] in ("complete", "partial")]
    comments = [c for c in (_run_comments(run_id) if include_comments else [])
                if (c.get("evidence_text") or "").strip()]
    model = _model_label()
    prompt_text = _prompt(prompt)
    prompt_hash = _sha256_text(prompt_text)

    batches = build_batches(snapshots, comments)
    batch_hashes = [_batch_input_hash(b, prompt_hash=prompt_hash, model_label=model)
                    for b in batches]
    input_hash = _analysis_input_hash(batch_hashes)
    input_item_count = sum(b["item_count"] for b in batches)

    # 顶层的同输入缓存：相同输入 + 相同 prompt 内容 + 相同模型 + 相同 schema → 复用
    cached = query_one(
        """SELECT id, status FROM social_analysis WHERE run_id=? AND input_hash=?
           AND prompt_version=? AND schema_version=? AND model_label=?
           AND status IN ('ok','insufficient_evidence') ORDER BY rowid DESC LIMIT 1""",
        (run_id, input_hash, prompt, SCHEMA_VERSION, model))
    if cached:
        social_store.set_current_analysis(run_id, cached["id"])
        return _result_dict(cached["id"], cached["status"], reused=True)

    analysis_id = social_store.save_analysis(
        run_id, input_hash=input_hash, schema_version=SCHEMA_VERSION,
        prompt_version=prompt, model_label=model, status="pending",
        is_current=False, batch_count=len(batches), input_item_count=input_item_count)

    if not batches:
        execute("UPDATE social_analysis SET status='insufficient_evidence', result_json=? "
                "WHERE id=?",
                (jdumps({"status": "insufficient_evidence", "required": "no_input",
                         "item_count": 0, "validated_findings": 0}), analysis_id))
        social_store.set_current_analysis(run_id, analysis_id)
        return _result_dict(analysis_id, "insufficient_evidence", reused=False)

    # 全任务范围的文本映射：quote 校验用全局（跨批次引用由 save_evidence 白名单拒绝）
    gtexts = {s["snapshot_id"]: s["evidence_text"] for s in snapshots}
    gctexts = {c["comment_snapshot_id"]: c["evidence_text"] for c in comments}
    authors = {s["snapshot_id"]: s["author_key"] for s in snapshots}

    ok_batches = 0
    failed_batches = 0
    total_findings = 0
    total_chars = 0
    first_error: Optional[BaseException] = None
    for batch in batches:
        total_chars += batch["char_count"]
        try:
            outcome = _run_batch(analysis_id, run_id, prompt_text, prompt_hash, model,
                                 batch, authors=authors, gtexts=gtexts, gctexts=gctexts)
        except Exception as exc:  # noqa: BLE001 - 单批失败隔离，继续其他批
            failed_batches += 1
            if first_error is None:
                first_error = exc
            _save_batch_receipt(
                analysis_id, batch, input_hash=batch_hashes[batch["index"]],
                model_label=model, prompt_version=prompt, status="failed",
                error_code=getattr(exc, "code", "AI_BATCH_FAILED"),
                elapsed_ms=None, result_hash=None)
            continue
        if outcome["status"] == "ok":
            ok_batches += 1
            total_findings += outcome["findings"]
        _save_batch_receipt(
            analysis_id, batch, input_hash=batch_hashes[batch["index"]],
            model_label=model, prompt_version=prompt, status=outcome["status"],
            error_code=None, elapsed_ms=outcome["elapsed_ms"],
            result_hash=outcome["result_hash"])

    if ok_batches:
        status = "ok"
    elif failed_batches:
        status = "failed"
    else:
        status = "insufficient_evidence"

    execute("UPDATE social_analysis SET status=?, result_json=? WHERE id=?",
            (status, jdumps({
                "status": status,
                "input_char_count": total_chars,
                "item_count": input_item_count,
                "validated_findings": total_findings,
                "batches_total": len(batches),
                "batches_ok": ok_batches,
                "batches_failed": failed_batches,
                "truncated_batches": sum(1 for b in batches if b["truncated"]),
            }), analysis_id))
    social_store.set_current_analysis(run_id, analysis_id)

    if failed_batches:
        _record_partial_failure(run_id, failed_batches)

    # 全部批次失败 → 抛出（调用方记 failed 回执，材料保留）；部分失败不影响其他批。
    if status == "failed" and first_error is not None:
        raise first_error

    return _result_dict(analysis_id, status, reused=False)


__all__ = [
    "PROMPT_X", "PROMPT_XHS", "SCHEMA_VERSION",
    "MAX_BATCH_CHARS", "MAX_ITEMS_PER_BATCH", "TRUNCATION_MARK",
    "build_batches", "analyze_x", "analyze_xhs",
]
