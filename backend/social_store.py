# -*- coding: utf-8 -*-
"""社交数据存取（任务书 §7.2）：帖子/笔记、快照、评论、观察、分析、发现、证据。

- 所有 ID 用文本；时间 UTC ISO8601；JSON 列做输入校验。
- 不可变快照：身份 hash 变化则新建版本，不覆盖旧证据（E1）。
- 幂等：按 (platform, source_id) / (item_id, identity_hash) / (item_id, source_comment_id) 去重。

两条口径（E2，必须分清）
------------------------
1. **每次命中**：``social_observation`` 每条搜索命中都留一行（审计用，不删）。
   同 ``(run_id, item_id)`` 只有一条 ``is_current=1``。
2. **当前快照**：``items_by_run`` 只取 ``is_current=1`` 的观察 → 每个 item 一行。

当前分析版本（E2）
------------------
``social_analysis`` 每次分析都留一行（审计用）。任务采用哪个版本由
``is_current=1`` 决定，用 :func:`set_current_analysis` 显式切换；
:func:`findings_by_run` 只展示当前版本的结论，不把历史版本混成一份结果。

脱敏（D3，写入口是最后一道闸）
------------------------------
本模块是**最终落库口**，因此在这里再兜一道 :mod:`social_redact`：
``title`` / ``evidence_text`` / ``quote`` 一律先脱敏再算 hash 与偏移，
``author_key`` 一律换成稳定匿名键。顺序固定为「先脱敏 → 再算 hash → 再算偏移」，
证据的 start/end 永远以**保存后**的不可变文本为准（T14）。
"""
from __future__ import annotations

import hashlib
import sqlite3
from typing import Any, Iterable, Optional

import store
import social_redact
from store import execute, jdumps, new_id, query_all, query_one, utcnow_iso

#: 合法证据立场（与迁移 017 的 CHECK 保持一致）。
VALID_STANCES: tuple[str, ...] = ("support", "contradict", "context")

#: 隔离原因（写入 social_evidence_quarantine.reason，便于事后审计）。
REASON_FK_PAIR = "FK_PAIR_INVALID"
REASON_STANCE = "STANCE_INVALID"
REASON_OFFSET = "OFFSET_INVALID"
REASON_NOT_FOUND = "SNAPSHOT_NOT_FOUND"
REASON_QUOTE = "QUOTE_NOT_IN_TEXT"
REASON_CROSS_RUN = "CROSS_RUN_REF"
REASON_ITEM_MISMATCH = "ITEM_MISMATCH"
REASON_NOT_ALLOWED = "REF_NOT_ALLOWED"

_IDENTITY_SEP = "\x1f"


# ---------------------------------------------------------------------------
# hash：content_hash 保持原义（证据偏移依赖它），identity_hash 是新的去重键
# ---------------------------------------------------------------------------

def content_hash(text: str) -> str:
    """sha256(evidence_text)：仅供旧证据偏移兼容，不再作去重键。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def normalize_evidence_text(text: Optional[str]) -> str:
    """归一化正文：统一换行、去掉首尾空白与行尾空白。

    只用于计算 identity_hash；落库的 evidence_text 仍是不可变原文，
    证据 start/end 偏移以落库文本为准。
    """
    if not text:
        return ""
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in unified.split("\n")).strip()


def identity_hash(*, platform: str, source_id: str, kind: str,
                  title: Optional[str], evidence_text: str, detail_status: str,
                  language: Optional[str] = None, published_at: Optional[str] = None,
                  collector_version: Optional[str] = None) -> str:
    """快照身份 hash（设计 §5.4）。

    输入字段：platform / source_id / kind / title / 归一化正文 / detail_status /
    language / published_at / collector_version。
    正文相同但 title 或 detail_status 变化 → 身份不同 → 产生新版本快照。
    """
    parts = [
        platform or "",
        source_id or "",
        kind or "",
        title or "",
        normalize_evidence_text(evidence_text),
        detail_status or "",
        language or "",
        published_at or "",
        collector_version or "",
    ]
    return hashlib.sha256(_IDENTITY_SEP.join(parts).encode("utf-8")).hexdigest()


def _item_identity_fields(item_id: str) -> tuple[str, str, str]:
    """取 item 的 (platform, source_id, kind)；查不到时退化为空串。"""
    row = query_one("SELECT platform, source_id, kind FROM social_item WHERE id=?", (item_id,))
    if not row:
        return ("", "", "")
    return (row["platform"] or "", row["source_id"] or "", row["kind"] or "")


# ---------------------------------------------------------------------------
# social_item / social_snapshot
# ---------------------------------------------------------------------------

def upsert_item(platform: str, source_id: str, kind: str, *,
                canonical_url: Optional[str], author_key: Optional[str]) -> str:
    """按 (platform, source_id) 幂等 upsert，返回 item_id。

    写库前两道清洗：
    - ``author_key`` 换成稳定匿名键 ``anon_<hex16>``（不号称匿名却存明文展示名）；
    - ``canonical_url`` 只保留公开形态（去查询串），token 只允许留在 locator 里。
    """
    now = utcnow_iso()
    author_key = social_redact.stable_anon_key(platform, author_key)
    canonical_url = social_redact.public_url(canonical_url)
    row = query_one(
        "SELECT id FROM social_item WHERE platform=? AND source_id=?",
        (platform, source_id),
    )
    if row:
        item_id = row["id"]
        execute(
            "UPDATE social_item SET last_seen_at=?, canonical_url=COALESCE(?, canonical_url), "
            "author_key=COALESCE(?, author_key) WHERE id=?",
            (now, canonical_url, author_key, item_id),
        )
        return item_id
    item_id = new_id("sit")
    execute(
        "INSERT INTO social_item (id, platform, source_id, kind, canonical_url, author_key, "
        "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
        (item_id, platform, source_id, kind, canonical_url, author_key, now, now),
    )
    return item_id


def insert_snapshot(item_id: str, *, title: Optional[str], evidence_text: str,
                    language: Optional[str], published_at: Optional[str],
                    detail_status: str, collector_version: Optional[str],
                    source_url_verified: bool = False,
                    identity_quality: str = "platform",
                    fetched_at: Optional[str] = None,
                    published_at_raw: Optional[str] = None,
                    published_at_quality: Optional[str] = None,
                    redacted: bool = False) -> Optional[str]:
    """不可变快照：身份 hash 相同则复用旧版本（不覆写），否则新建版本。

    去重键是 ``(item_id, identity_hash)``，不是 content_hash：
    正文相同但 title / detail_status 不同 → 产生新版本，旧版本与旧证据偏移保留。

    兼容 016 遗留行（identity_hash 退化为 content_hash）：若存在同 item、同
    content_hash、同 title、同 detail_status 的遗留行，则复用它，
    避免同一次重跑产生近似的重复行。

    顺序铁律（T14）：**先脱敏 → 再算 content_hash / identity_hash**。
    ``redacted`` 列记录「本条文本是否真的被脱敏改写过」，供事后审计。
    """
    raw_text = str(evidence_text or "")
    evidence_text = social_redact.redact(raw_text)
    title = social_redact.redact(title)
    redacted = bool(redacted) or (evidence_text != raw_text)
    platform, source_id, kind = _item_identity_fields(item_id)
    ih = identity_hash(
        platform=platform, source_id=source_id, kind=kind, title=title,
        evidence_text=evidence_text, detail_status=detail_status, language=language,
        published_at=published_at, collector_version=collector_version)
    ch = content_hash(evidence_text)

    existing = query_one(
        "SELECT id FROM social_snapshot WHERE item_id=? AND identity_hash=?",
        (item_id, ih),
    )
    if existing:
        return existing["id"]  # 已存在 → 原样复用，绝不覆写 evidence_text

    legacy = query_one(
        "SELECT id FROM social_snapshot WHERE item_id=? AND content_hash=? "
        "AND identity_hash IS NOT NULL AND identity_hash=content_hash "
        "AND detail_status=? AND IFNULL(title,'')=IFNULL(?,'')",
        (item_id, ch, detail_status, title),
    )
    if legacy:
        return legacy["id"]

    snap_id = new_id("ssn")
    execute(
        "INSERT INTO social_snapshot (id, item_id, content_hash, identity_hash, title, "
        "evidence_text, language, published_at, published_at_raw, published_at_quality, "
        "fetched_at, detail_status, collector_version, source_url_verified, "
        "identity_quality, redacted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (snap_id, item_id, ch, ih, title, evidence_text, language, published_at,
         published_at_raw, published_at_quality or "unknown",
         fetched_at or utcnow_iso(), detail_status, collector_version,
         1 if source_url_verified else 0, identity_quality, 1 if redacted else 0),
    )
    return snap_id


def snapshot_is_referenced(snapshot_id: str) -> bool:
    """该快照是否已被证据引用（被引用的旧版本永不覆写）。"""
    row = query_one(
        "SELECT 1 AS hit FROM social_evidence WHERE snapshot_id=? LIMIT 1", (snapshot_id,))
    return bool(row)


def ensure_snapshot_identity(snapshot_id: str) -> Optional[str]:
    """把 016 遗留行（identity_hash=content_hash）升级为真正的身份 hash。

    - 已被证据引用的快照**不动**（保持旧版本不可变）；
    - 与同 item 其它快照身份冲突时不强写，返回 None。
    返回升级后的 identity_hash；无需升级或跳过时返回当前值/None。
    """
    row = query_one(
        "SELECT id, item_id, content_hash, identity_hash, title, evidence_text, language, "
        "published_at, detail_status, collector_version FROM social_snapshot WHERE id=?",
        (snapshot_id,))
    if not row:
        return None
    current = row["identity_hash"]
    if current and current != row["content_hash"]:
        return current  # 已是真正的身份 hash
    if snapshot_is_referenced(snapshot_id):
        return current  # 已被引用 → 保持旧版本不变
    platform, source_id, kind = _item_identity_fields(row["item_id"])
    ih = identity_hash(
        platform=platform, source_id=source_id, kind=kind, title=row["title"],
        evidence_text=row["evidence_text"], detail_status=row["detail_status"],
        language=row["language"], published_at=row["published_at"],
        collector_version=row["collector_version"])
    try:
        execute("UPDATE social_snapshot SET identity_hash=? WHERE id=?", (ih, snapshot_id))
    except sqlite3.IntegrityError:
        return current  # 与既有版本冲突 → 不动
    return ih


# ---------------------------------------------------------------------------
# social_comment / social_comment_snapshot
# ---------------------------------------------------------------------------

def upsert_comment(item_id: str, source_comment_id: str, *, parent_source_id: Optional[str],
                   root_source_id: Optional[str], author_key: Optional[str],
                   parent_missing: bool = False) -> str:
    """按 (item_id, source_comment_id) 幂等 upsert，返回 comment_id。

    评论作者同样匿名化：按键里带上所属 item 的 platform，保证同一作者跨条目稳定。
    """
    now = utcnow_iso()
    platform = _item_identity_fields(item_id)[0]
    author_key = social_redact.stable_anon_key(platform, author_key)
    row = query_one(
        "SELECT id FROM social_comment WHERE item_id=? AND source_comment_id=?",
        (item_id, source_comment_id),
    )
    if row:
        cid = row["id"]
        execute(
            "UPDATE social_comment SET parent_source_id=COALESCE(?, parent_source_id), "
            "root_source_id=COALESCE(?, root_source_id), author_key=COALESCE(?, author_key), "
            "parent_missing=? WHERE id=?",
            (parent_source_id, root_source_id, author_key, 1 if parent_missing else 0, cid),
        )
        return cid
    cid = new_id("scm")
    execute(
        "INSERT INTO social_comment (id, item_id, source_comment_id, parent_source_id, "
        "root_source_id, author_key, first_seen_at, parent_missing) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (cid, item_id, source_comment_id, parent_source_id, root_source_id,
         author_key, now, 1 if parent_missing else 0),
    )
    return cid


def insert_comment_snapshot(comment_id: str, *, evidence_text: str,
                            published_at: Optional[str], content_status: str,
                            identity_quality: str = "platform",
                            fetched_at: Optional[str] = None,
                            published_at_raw: Optional[str] = None,
                            published_at_quality: Optional[str] = None) -> Optional[str]:
    """评论版本快照：按 (comment_id, content_hash) 去重。

    与正文快照同理：**先脱敏 → 再算 content_hash**（T14）。
    """
    evidence_text = social_redact.redact(evidence_text)
    h = content_hash(evidence_text)
    existing = query_one(
        "SELECT id FROM social_comment_snapshot WHERE comment_id=? AND content_hash=?",
        (comment_id, h),
    )
    if existing:
        return existing["id"]
    snap_id = new_id("scs")
    execute(
        "INSERT INTO social_comment_snapshot (id, comment_id, content_hash, evidence_text, "
        "published_at, published_at_raw, published_at_quality, fetched_at, content_status, "
        "identity_quality) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (snap_id, comment_id, h, evidence_text, published_at, published_at_raw,
         published_at_quality or "unknown", fetched_at or utcnow_iso(),
         content_status, identity_quality),
    )
    return snap_id


# ---------------------------------------------------------------------------
# social_observation / run_social_comment
# ---------------------------------------------------------------------------

def insert_observation(run_id: str, item_id: str, snapshot_id: Optional[str], *,
                       query_text: Optional[str], retrieval_mode: str,
                       metrics: Optional[dict[str, Any]] = None,
                       coverage: Optional[dict[str, Any]] = None,
                       ingestion_method: Optional[str] = None,
                       provenance: Optional[dict[str, Any]] = None) -> str:
    """记录一次命中；同 (run_id, item_id) 的旧行置 is_current=0（保留审计）。"""
    obs_id = new_id("sob")
    conn = store.get_conn()  # 同一事务：先失效旧行，再插当前行
    conn.execute(
        "UPDATE social_observation SET is_current=0 WHERE run_id=? AND item_id=? AND is_current=1",
        (run_id, item_id),
    )
    conn.execute(
        "INSERT INTO social_observation (id, run_id, item_id, snapshot_id, query_text, "
        "retrieval_mode, observed_at, metrics_json, coverage_json, ingestion_method, "
        "provenance_json, is_current) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
        (obs_id, run_id, item_id, snapshot_id, query_text, retrieval_mode,
         utcnow_iso(), jdumps(metrics or {}), jdumps(coverage or {}),
         ingestion_method, jdumps(provenance or {}) if provenance else None),
    )
    conn.commit()
    return obs_id


def link_run_comment(run_id: str, comment_snapshot_id: str, observation_id: Optional[str]) -> None:
    execute(
        "INSERT OR IGNORE INTO run_social_comment (run_id, comment_snapshot_id, observation_id) "
        "VALUES (?,?,?)",
        (run_id, comment_snapshot_id, observation_id),
    )


# ---------------------------------------------------------------------------
# social_analysis / social_finding / social_evidence
# ---------------------------------------------------------------------------

def save_analysis(run_id: str, *, input_hash: str, schema_version: str,
                  prompt_version: str, model_label: str, status: str,
                  result_json: Optional[dict[str, Any]] = None,
                  is_current: bool = False,
                  batch_count: int = 0,
                  input_item_count: int = 0) -> str:
    """保存一次分析回执。is_current=True 时同时把同 run 的其它版本置为非当前。"""
    aid = new_id("san")
    conn = store.get_conn()
    if is_current:
        conn.execute("UPDATE social_analysis SET is_current=0 WHERE run_id=?", (run_id,))
    conn.execute(
        "INSERT INTO social_analysis (id, run_id, input_hash, schema_version, prompt_version, "
        "model_label, status, result_json, created_at, is_current, batch_count, "
        "input_item_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (aid, run_id, input_hash, schema_version, prompt_version, model_label,
         status, jdumps(result_json) if result_json else None, utcnow_iso(),
         1 if is_current else 0, batch_count, input_item_count),
    )
    conn.commit()
    return aid


def set_current_analysis(run_id: str, analysis_id: str) -> None:
    """明确任务采用哪个分析版本；旧版本保留审计，不删除、不合并展示。"""
    conn = store.get_conn()
    conn.execute("UPDATE social_analysis SET is_current=0 WHERE run_id=? AND id<>?",
                 (run_id, analysis_id))
    conn.execute("UPDATE social_analysis SET is_current=1 WHERE run_id=? AND id=?",
                 (run_id, analysis_id))
    conn.commit()


def current_analysis(run_id: str) -> Optional[dict[str, Any]]:
    """当前采用的分析版本；没有显式指定时返回 None。"""
    return query_one(
        "SELECT id, run_id, status, model_label, prompt_version, created_at, is_current, "
        "batch_count, input_item_count FROM social_analysis WHERE run_id=? AND is_current=1 "
        "ORDER BY rowid DESC LIMIT 1", (run_id,))


def save_finding(analysis_id: str, *, kind: str, title: str, summary_zh: Optional[str],
                 limitations: Optional[dict[str, Any]] = None,
                 review_status: str = "auto") -> str:
    """保存一条结论；标题与摘要同样过脱敏（模型可能回显它读到的联系方式）。"""
    fid = new_id("sfd")
    execute(
        "INSERT INTO social_finding (id, analysis_id, kind, title, summary_zh, "
        "limitations_json, review_status) VALUES (?,?,?,?,?,?,?)",
        (fid, analysis_id, kind, social_redact.redact(title),
         social_redact.redact(summary_zh),
         jdumps(limitations or {}), review_status),
    )
    return fid


def _quarantine_evidence(*, finding_id: Optional[str], snapshot_id: Optional[str],
                         comment_snapshot_id: Optional[str], quote: Optional[str],
                         start: Optional[int], end: Optional[int], stance: Optional[str],
                         reason: str) -> None:
    """非法证据进隔离区：不静默丢弃，也不让它混进结果。

    隔离区同样要脱敏：被拒的 quote 恰恰可能是含凭据的那一段。
    """
    execute(
        "INSERT OR IGNORE INTO social_evidence_quarantine (id, finding_id, snapshot_id, "
        "comment_snapshot_id, quote, start, end, stance, reason, quarantined_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (new_id("seq"), finding_id, snapshot_id, comment_snapshot_id,
         social_redact.redact(quote), start, end, stance, reason, utcnow_iso()),
    )


def _reject(reason: str, message: str, **fields: Any) -> None:
    """记隔离区后抛 ValueError（调用方可见，DB 的 CHECK 是第二道兜底）。"""
    _quarantine_evidence(reason=reason, **fields)
    raise ValueError(message)


def save_evidence(finding_id: str, *, quote: str, start: Optional[int],
                  end: Optional[int], stance: str,
                  snapshot_id: Optional[str] = None,
                  comment_snapshot_id: Optional[str] = None,
                  run_id: Optional[str] = None,
                  item_id: Optional[str] = None,
                  allowed_snapshot_ids: Optional[Iterable[str]] = None,
                  allowed_comment_snapshot_ids: Optional[Iterable[str]] = None,
                  verify_quote: bool = True) -> str:
    """保存一条证据；任何一项校验不通过都进隔离区并抛 ValueError。

    校验项
      1. 两个快照 FK 恰有一个非空（防止"无来源证据"与"双来源歧义"）
      2. stance ∈ {support, contradict, context}
      3. 偏移合法：start>=0 且 end>start
      4. 快照/评论快照确实存在
      5. 引用属于本任务（run_id 缺省时由 finding → analysis 推导）
      6. 引用属于指定 item（防止把别的内容的正文绑到本条）
      7. quote 是落库原文的子串（防止引用对不上原文）

    脱敏：quote 先过 :func:`social_redact.redact` 再校验/落库。落库原文本身已脱敏，
    且脱敏幂等，因此「引用对得上原文」这条校验在脱敏后仍然成立。
    """
    quote = social_redact.redact(quote) or ""
    common: dict[str, Any] = {
        "finding_id": finding_id, "snapshot_id": snapshot_id,
        "comment_snapshot_id": comment_snapshot_id, "quote": quote,
        "start": start, "end": end, "stance": stance,
    }
    # 1) 两个快照 FK 恰有一个非空
    if (snapshot_id is None) == (comment_snapshot_id is None):
        _reject(REASON_FK_PAIR,
                "evidence 必须且只能绑定一个快照（snapshot_id 或 comment_snapshot_id）",
                **common)
    # 2) stance
    if stance not in VALID_STANCES:
        _reject(REASON_STANCE, f"非法 stance：{stance}（只能是 {VALID_STANCES}）", **common)
    # 3) 偏移
    if start is not None and not (isinstance(start, int) and start >= 0
                                  and isinstance(end, int) and end > start):
        _reject(REASON_OFFSET, f"非法偏移：start={start} end={end}（需 start>=0 且 end>start）",
                **common)
    # 4) 快照存在
    source_text: Optional[str] = None
    if snapshot_id:
        row = query_one("SELECT id, item_id, evidence_text FROM social_snapshot WHERE id=?",
                        (snapshot_id,))
        if not row:
            _reject(REASON_NOT_FOUND, f"快照不存在：{snapshot_id}", **common)
        source_text = row["evidence_text"]
        snap_item_id: Optional[str] = row["item_id"]
    else:
        row = query_one(
            "SELECT scs.id, sc.item_id AS item_id, scs.evidence_text FROM social_comment_snapshot scs "
            "JOIN social_comment sc ON sc.id = scs.comment_id WHERE scs.id=?",
            (comment_snapshot_id,))
        if not row:
            _reject(REASON_NOT_FOUND, f"评论快照不存在：{comment_snapshot_id}", **common)
        source_text = row["evidence_text"]
        snap_item_id = row["item_id"]
    # 5) 任务范围
    scope_run = run_id
    if scope_run is None:
        derived = query_one(
            "SELECT sa.run_id AS run_id FROM social_finding sf "
            "JOIN social_analysis sa ON sa.id = sf.analysis_id WHERE sf.id=?", (finding_id,))
        scope_run = derived["run_id"] if derived else None
    if scope_run:
        if snapshot_id:
            hit = query_one(
                "SELECT 1 AS hit FROM social_observation WHERE run_id=? AND snapshot_id=? LIMIT 1",
                (scope_run, snapshot_id))
        else:
            hit = query_one(
                "SELECT 1 AS hit FROM run_social_comment WHERE run_id=? AND comment_snapshot_id=? "
                "LIMIT 1", (scope_run, comment_snapshot_id))
        if not hit:
            _reject(REASON_CROSS_RUN,
                    f"引用不属于任务 {scope_run}（跨任务 ID 一律拒绝）", **common)
    # 6) item 范围
    if item_id and snap_item_id and snap_item_id != item_id:
        _reject(REASON_ITEM_MISMATCH,
                f"引用不属于 item {item_id}（实际属于 {snap_item_id}）", **common)
    # 6b) 批次白名单（E4：引用只允许指向本批次输入）
    if allowed_snapshot_ids is not None and snapshot_id \
            and snapshot_id not in set(allowed_snapshot_ids):
        _reject(REASON_NOT_ALLOWED, f"引用不在允许集合内：{snapshot_id}", **common)
    if allowed_comment_snapshot_ids is not None and comment_snapshot_id \
            and comment_snapshot_id not in set(allowed_comment_snapshot_ids):
        _reject(REASON_NOT_ALLOWED, f"引用不在允许集合内：{comment_snapshot_id}", **common)
    # 7) quote 必须是原文子串
    if verify_quote:
        if not quote:
            _reject(REASON_QUOTE, "quote 不能为空", **common)
        if source_text is None or quote not in source_text:
            _reject(REASON_QUOTE, "quote 不是落库原文的子串（引用对不上原文）", **common)

    eid = new_id("sev")
    try:
        execute(
            "INSERT INTO social_evidence (id, finding_id, snapshot_id, comment_snapshot_id, "
            "quote, start, end, stance) VALUES (?,?,?,?,?,?,?,?)",
            (eid, finding_id, snapshot_id, comment_snapshot_id, quote, start, end, stance),
        )
    except sqlite3.IntegrityError as exc:
        _reject("DB_CONSTRAINT", f"数据库约束拒绝该证据：{exc}", **common)
    return eid


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def encode_cursor(item_id: str, snapshot_id: Optional[str]) -> str:
    """复合游标 (item_id, snapshot_id) 编码。"""
    return f"{item_id}{_IDENTITY_SEP}{snapshot_id or ''}"


def decode_cursor(cursor: Any) -> tuple[str, str]:
    """复合游标解码；兼容字符串与 (item_id, snapshot_id) 元组。"""
    if isinstance(cursor, (tuple, list)):
        return (str(cursor[0]), str(cursor[1] or ""))
    text = str(cursor)
    sep = _IDENTITY_SEP if _IDENTITY_SEP in text else ("|" if "|" in text else "")
    if sep:
        item_id, _, snapshot_id = text.partition(sep)
        return (item_id, snapshot_id)
    return (text, "")


def items_by_run(run_id: str, *, limit: int = 100,
                 cursor: Optional[Any] = None) -> list[dict[str, Any]]:
    """本任务的「每 item 当前快照」列表：按 is_current 去重，复合游标分页。

    去重的是列表，不是来源记录：``social_observation`` 里的每次命中都保留。
    """
    sql = """SELECT si.id AS item_id, si.platform, si.source_id, si.kind, si.canonical_url,
                    si.author_key, ss.id AS snapshot_id, ss.title, ss.evidence_text,
                    ss.published_at, ss.fetched_at, ss.detail_status, ss.identity_hash,
                    so.id AS observation_id, so.query_text, so.retrieval_mode,
                    so.metrics_json, so.coverage_json
             FROM social_observation so
             JOIN social_item si ON si.id = so.item_id
             LEFT JOIN social_snapshot ss ON ss.id = so.snapshot_id
             JOIN (SELECT item_id, MAX(rowid) AS rid FROM social_observation
                   WHERE run_id=? AND is_current=1 GROUP BY item_id) cur
               ON cur.rid = so.rowid
             WHERE so.run_id=? AND so.is_current=1"""
    params: list[Any] = [run_id, run_id]
    if cursor:
        c_item, c_snap = decode_cursor(cursor)
        sql += " AND (si.id > ? OR (si.id = ? AND COALESCE(ss.id,'') > ?))"
        params.extend([c_item, c_item, c_snap])
    sql += " ORDER BY si.id, COALESCE(ss.id,'') LIMIT ?"
    params.append(limit)
    return query_all(sql, tuple(params))


def comments_by_item(item_id: str, run_id: Optional[str] = None,
                     limit: int = 100, *, require_run: bool = False,
                     ) -> list[dict[str, Any]]:
    """评论列表。

    - ``run_id`` 给出 → 只列该任务实际读取过的评论版本（结果页必须用这条路径）。
    - ``run_id`` 为空 → 跨任务浏览模式，返回的每行带 ``scope='cross_run'``，
      供 UI 明示"这不是本任务读取到的版本"。
    - ``require_run=True`` → 强制任务范围，缺 run_id 直接报错（结果页路径）。
    """
    if require_run and not run_id:
        raise ValueError("结果页读取评论必须指定 run_id（禁止跨任务混读）")
    sql = """SELECT sc.id AS comment_id, sc.source_comment_id, sc.parent_source_id,
                    sc.root_source_id, sc.author_key, sc.parent_missing,
                    scs.id AS comment_snapshot_id, scs.evidence_text, scs.published_at,
                    scs.content_status, scs.identity_quality"""
    params: list[Any] = []
    if run_id:
        sql += """,
                    'run' AS scope
             FROM social_comment sc
             JOIN social_comment_snapshot scs ON scs.comment_id = sc.id
             JOIN run_social_comment rsc ON rsc.comment_snapshot_id = scs.id
             WHERE sc.item_id=? AND rsc.run_id=?"""
        params = [item_id, run_id]
    else:
        sql += """,
                    'cross_run' AS scope
             FROM social_comment sc
             JOIN social_comment_snapshot scs ON scs.comment_id = sc.id
             WHERE sc.item_id=?"""
        params = [item_id]
    sql += " ORDER BY sc.source_comment_id LIMIT ?"
    params.append(limit)
    return query_all(sql, tuple(params))


def comments_for_result(run_id: str, item_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    """结果页专用：强制任务范围的评论读取（run_id 必填）。"""
    return comments_by_item(item_id, run_id, limit, require_run=True)


def findings_by_run(run_id: str, *, include_history: bool = False
                    ) -> list[dict[str, Any]]:
    """本任务展示用的结论：默认只取**当前分析版本**的 finding。

    - 已用 :func:`set_current_analysis` 指定版本 → 严格只展示该版本；
    - 未指定（兼容旧行为）→ 退化为"最新一条 ok 分析"，仍不把多个历史版本混在一起；
    - ``include_history=True`` → 审计用，返回该任务全部版本的结论。
    """
    sql = """SELECT sf.id, sf.analysis_id, sf.kind, sf.title, sf.summary_zh,
                    sf.limitations_json, sa.status AS analysis_status, sa.model_label,
                    sa.is_current AS analysis_is_current, sa.created_at AS analysis_created_at
             FROM social_finding sf
             JOIN social_analysis sa ON sa.id = sf.analysis_id
             WHERE sa.run_id=?"""
    params: list[Any] = [run_id]
    if not include_history:
        current = query_one(
            "SELECT id FROM social_analysis WHERE run_id=? AND is_current=1 "
            "ORDER BY rowid DESC LIMIT 1", (run_id,))
        if current:
            sql += " AND sa.id=? AND sa.status='ok'"
            params.append(current["id"])
        else:
            sql += " AND sa.status='ok'"
    sql += " ORDER BY sf.rowid"
    return query_all(sql, tuple(params))


def evidence_by_finding(finding_id: str) -> list[dict[str, Any]]:
    return query_all(
        """SELECT se.id, se.snapshot_id, se.comment_snapshot_id, se.quote, se.start, se.end,
                  se.stance,
                  COALESCE(ss.evidence_text, scs.evidence_text) AS source_text
           FROM social_evidence se
           LEFT JOIN social_snapshot ss ON ss.id = se.snapshot_id
           LEFT JOIN social_comment_snapshot scs ON scs.id = se.comment_snapshot_id
           WHERE se.finding_id=? ORDER BY se.rowid""",
        (finding_id,),
    )


def quarantined_evidence(limit: int = 100) -> list[dict[str, Any]]:
    """非法证据隔离区（审计入口，正常情况下应为空）。"""
    return query_all(
        """SELECT id, finding_id, snapshot_id, comment_snapshot_id, quote, start, end,
                  stance, reason, quarantined_at
           FROM social_evidence_quarantine ORDER BY quarantined_at DESC, rowid DESC LIMIT ?""",
        (limit,),
    )


__all__ = [
    "VALID_STANCES",
    "content_hash", "normalize_evidence_text", "identity_hash",
    "upsert_item", "insert_snapshot", "snapshot_is_referenced", "ensure_snapshot_identity",
    "upsert_comment", "insert_comment_snapshot",
    "insert_observation", "link_run_comment",
    "save_analysis", "set_current_analysis", "current_analysis",
    "save_finding", "save_evidence", "quarantined_evidence",
    "encode_cursor", "decode_cursor",
    "items_by_run", "comments_by_item", "comments_for_result",
    "findings_by_run", "evidence_by_finding",
]
