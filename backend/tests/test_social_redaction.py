# -*- coding: utf-8 -*-
"""T14 脱敏契约（**离线**）：统一入口、匿名键与「先脱敏 → 再算 hash → 再算偏移」。

**本文件全部是离线测试**：
- 数据库 = 临时目录里的全新 SQLite，与 ``.local/data/research.sqlite3`` 完全无关；
- 采集器 / 模型 = ``social_testkit`` 的内存替身（不是真实采集能力）；
- 凭据 = ``SENTINEL_...`` 哨兵字符串，是**脱敏形状样本，非真实数据**，绝不使用真实 token。

判定原则：脱敏只允许替换联系方式/凭据，**正文其余部分逐字保留**（脱敏 ≠ 删除证据），
证据偏移永远以保存后的不可变文本为准。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(BACKEND_DIR), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import social_redact  # noqa: E402
import social_store  # noqa: E402
import store  # noqa: E402

SENTINEL_AUTH = "SENTINEL_AUTH_TOKEN_9f3c7b21"
SENTINEL_CT0 = "SENTINEL_CT0_5ab9c0de"
SENTINEL_XSEC = "SENTINEL_XSEC_TOKEN_zz9"
SENTINEL_EMAIL = "SENTINEL_HR_MAILBOX@example.com"
SENTINEL_WECHAT = "wxid_SENTINELWECHAT001"
SENTINEL_SCREEN_NAME = "SENTINEL_SCREEN_NAME_kevin"
SENTINEL_PHONE = "13812345678"

BODY = (
    "消费金融风控岗位最近招聘需求明显增加。\n"
    f"联系方式：微信：{SENTINEL_WECHAT} 邮箱 {SENTINEL_EMAIL} 手机 {SENTINEL_PHONE}\n"
    f"采集参数：xsec_token={SENTINEL_XSEC} auth_token={SENTINEL_AUTH} ct0={SENTINEL_CT0}\n"
    "结论：岗位要求以风控建模与数据分析为主。"
)

# 正文里必须**逐字保留**的部分（证明脱敏不是删除证据）。
KEEP_LINES = (
    "消费金融风控岗位最近招聘需求明显增加。",
    "结论：岗位要求以风控建模与数据分析为主。",
)


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """临时库 + 全量迁移（绝不触碰业务库）。"""
    monkeypatch.setenv("FININTEL_DB", str(tmp_path / "redact.sqlite3"))
    monkeypatch.setattr(store._local, "conn", None, raising=False)
    applied = store.apply_migrations()
    assert "017_social_runtime" in applied, f"迁移未完整应用：{applied}"
    yield
    conn = getattr(store._local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    store._local.conn = None


def _texts_of(body: str) -> str:
    return json.dumps(body, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 1) 统一入口：幂等 + 覆盖范围 + 不误伤正文
# ---------------------------------------------------------------------------

def test_redact_covers_credentials_and_contacts() -> None:
    """哨兵凭据/联系方式必须全部被替换，且不留任何哨兵片段。"""
    out = social_redact.redact(BODY)
    for sentinel in (SENTINEL_AUTH, SENTINEL_CT0, SENTINEL_XSEC,
                     SENTINEL_EMAIL, SENTINEL_WECHAT, SENTINEL_PHONE):
        assert sentinel not in out, f"{sentinel} 未被脱敏：{out}"
    assert not social_redact.contains_secret(out), out


def test_redact_keeps_evidence_verbatim() -> None:
    """脱敏 ≠ 删除证据：与联系方式无关的正文必须逐字保留。"""
    out = social_redact.redact(BODY)
    for line in KEEP_LINES:
        assert line in out, f"正文被误改：{line!r} 不在 {out!r}"


def test_redact_is_idempotent() -> None:
    """幂等：重复脱敏不再产生二次改写（保证同文本同 hash）。"""
    once = social_redact.redact(BODY)
    twice = social_redact.redact(once)
    assert once == twice


def test_redact_handles_none_and_empty() -> None:
    assert social_redact.redact(None) is None
    assert social_redact.redact("") == ""


def test_safe_token_error_never_echoes_the_value() -> None:
    """T14 明确要求：参数校验失败**不得**回显原参数值（它可能正是凭据）。

    注意：合法 token 形状的值必须**正常通过**（调用方就是要把它拼进 argv），
    只有「空值 / 选项前缀 / 控制字符」才拒绝，且拒绝时不得把值带进错误消息。
    """
    # 空值：拒绝，但错误消息里没有任何值可回显
    with pytest.raises(ValueError):
        social_redact.safe_token("")
    with pytest.raises(ValueError):
        social_redact.safe_token(None)
    # 选项前缀：错误消息绝不能把被拒绝的值（本身就是凭据）带出来
    prefixed = "-" + SENTINEL_AUTH
    with pytest.raises(ValueError) as exc:
        social_redact.safe_token(prefixed)
    assert SENTINEL_AUTH not in str(exc.value), str(exc.value)
    assert prefixed not in str(exc.value), str(exc.value)
    # 控制字符（注入形态）：同样不得回显
    ctrl = SENTINEL_CT0 + "\n--force"
    with pytest.raises(ValueError) as exc2:
        social_redact.safe_token(ctrl)
    assert SENTINEL_CT0 not in str(exc2.value), str(exc2.value)
    # 合法值原样返回（不误伤正常参数，包括 token 形状）
    assert social_redact.safe_token("风控岗位") == "风控岗位"
    assert social_redact.safe_token(SENTINEL_AUTH) == SENTINEL_AUTH


def test_contains_secret_is_false_after_redaction() -> None:
    """不变量：``redact`` 的输出**不得**再被判为敏感（占位符不算命中）。"""
    out = social_redact.redact(BODY)
    assert social_redact.contains_secret(out) is False, out
    # 反向对照：未脱敏原文必须判为敏感（证明探测本身有效，不是恒 False）
    assert social_redact.contains_secret(BODY) is True


def test_public_url_drops_query_and_rejects_foreign_hosts() -> None:
    """D4 读取边界：对外 URL 只留 scheme://host/path。"""
    url = f"https://www.xiaohongshu.com/explore/note_1?xsec_token={SENTINEL_XSEC}&xsec_source=pc_search"
    out = social_redact.public_url(url)
    assert out == "https://www.xiaohongshu.com/explore/note_1", out
    assert social_redact.public_url("https://evil.example.com/note?token=x") is None
    assert social_redact.public_url(None) is None


# ---------------------------------------------------------------------------
# 2) 落库顺序：先脱敏 → 再算 hash → 再算偏移
# ---------------------------------------------------------------------------

def _seed_item_with_secret() -> tuple[str, str]:
    item_id = social_store.upsert_item(
        "xiaohongshu", "note_sentinel_1", "xhs_note",
        canonical_url=f"https://www.xiaohongshu.com/explore/note_sentinel_1?xsec_token={SENTINEL_XSEC}",
        author_key=SENTINEL_SCREEN_NAME)
    snap_id = social_store.insert_snapshot(
        item_id, title=f"标题 {SENTINEL_EMAIL}", evidence_text=BODY, language="zh",
        published_at="2026-09-20T10:00:00Z", detail_status="complete",
        collector_version="offline-fixture")
    return item_id, snap_id


def test_snapshot_hash_is_computed_on_redacted_text(isolated) -> None:
    """content_hash / identity_hash 必须基于**脱敏后**的文本（顺序铁律）。"""
    item_id, snap_id = _seed_item_with_secret()
    row = store.query_one(
        "SELECT content_hash, identity_hash, evidence_text, title, redacted "
        "FROM social_snapshot WHERE id=?", (snap_id,))
    stored = row["evidence_text"]
    for sentinel in (SENTINEL_AUTH, SENTINEL_CT0, SENTINEL_XSEC,
                     SENTINEL_EMAIL, SENTINEL_WECHAT, SENTINEL_PHONE):
        assert sentinel not in stored, stored
    # hash 与「落库文本」自洽 → 说明脱敏发生在算 hash 之前
    assert row["content_hash"] == social_store.content_hash(stored)
    assert row["identity_hash"] == social_store.identity_hash(
        platform="xiaohongshu", source_id="note_sentinel_1", kind="xhs_note",
        title=row["title"], evidence_text=stored, detail_status="complete",
        language="zh", published_at="2026-09-20T10:00:00Z",
        collector_version="offline-fixture")
    assert row["redacted"] == 1, "被改写过必须留审计标记 redacted=1"
    assert SENTINEL_EMAIL not in (row["title"] or "")
    # 去重键也建立在脱敏文本上：同一条再写一次 → 复用同一快照（不产生第二份）
    again = social_store.insert_snapshot(
        item_id, title=f"标题 {SENTINEL_EMAIL}", evidence_text=BODY, language="zh",
        published_at="2026-09-20T10:00:00Z", detail_status="complete",
        collector_version="offline-fixture")
    assert again == snap_id


def test_evidence_offsets_align_with_stored_text(isolated) -> None:
    """证据偏移以保存后的文本为准：quote 用落库文本算偏移 → 校验通过且落库无哨兵。"""
    item_id, snap_id = _seed_item_with_secret()
    stored = store.query_one("SELECT evidence_text FROM social_snapshot WHERE id=?",
                             (snap_id,))["evidence_text"]
    quote = "风控建模与数据分析"
    start = stored.index(quote)
    end = start + len(quote)
    run_id = "run_redact_fixture"
    store.execute(
        "INSERT INTO research_run (id, name, config_snapshot, started_at) VALUES (?,?,?,?)",
        (run_id, "离线脱敏用例", "{}", "2026-09-28T00:00:00Z"))
    social_store.insert_observation(run_id, item_id, snap_id, query_text="离线",
                                    retrieval_mode="keyword_search")
    analysis_id = social_store.save_analysis(
        run_id, input_hash="h", schema_version="1", prompt_version="p",
        model_label="offline", status="ok")
    finding_id = social_store.save_finding(
        analysis_id, kind="topic", title="离线主题", summary_zh="离线摘要")
    evidence_id = social_store.save_evidence(
        finding_id, quote=quote, start=start, end=end, stance="support",
        snapshot_id=snap_id, run_id=run_id, item_id=item_id)
    rows = social_store.evidence_by_finding(finding_id)
    assert len(rows) == 1, rows
    assert rows[0]["start"] == start and rows[0]["end"] == end
    assert rows[0]["quote"] in rows[0]["source_text"]
    assert rows[0]["id"] == evidence_id
    # 含哨兵的 quote 会被脱敏后落库，且不会因为脱敏而与原文对不上
    evidence2 = social_store.save_evidence(
        finding_id, quote=f"xsec_token={SENTINEL_XSEC}",
        start=None, end=None, stance="context", snapshot_id=snap_id,
        run_id=run_id, item_id=item_id, verify_quote=False)
    stored_quote = store.query_one("SELECT quote FROM social_evidence WHERE id=?",
                                   (evidence2,))["quote"]
    assert SENTINEL_XSEC not in stored_quote, stored_quote


# ---------------------------------------------------------------------------
# 3) 作者键匿名化
# ---------------------------------------------------------------------------

def test_author_key_is_stable_anon_never_plaintext(isolated) -> None:
    item_id, _ = _seed_item_with_secret()
    row = store.query_one("SELECT author_key FROM social_item WHERE id=?", (item_id,))
    key = row["author_key"]
    assert key and key.startswith("anon_"), key
    assert len(key) == len("anon_") + 16, key
    assert SENTINEL_SCREEN_NAME not in key

    same = social_store.upsert_item("xiaohongshu", "note_sentinel_1", "xhs_note",
                                    canonical_url=None, author_key=SENTINEL_SCREEN_NAME)
    assert store.query_one("SELECT author_key FROM social_item WHERE id=?",
                           (same,))["author_key"] == key
    other = social_store.upsert_item("x", "note_sentinel_1", "x_post",
                                     canonical_url=None, author_key=SENTINEL_SCREEN_NAME)
    assert store.query_one("SELECT author_key FROM social_item WHERE id=?",
                           (other,))["author_key"] != key


def test_comment_author_and_text_are_redacted(isolated) -> None:
    item_id, _ = _seed_item_with_secret()
    cid = social_store.upsert_comment(
        item_id, "c_sentinel_1", parent_source_id=None, root_source_id=None,
        author_key=SENTINEL_SCREEN_NAME, parent_missing=False)
    scs = social_store.insert_comment_snapshot(
        cid, evidence_text=f"评论正文，联系 {SENTINEL_EMAIL} ct0={SENTINEL_CT0}",
        published_at="2026-09-20T11:00:00Z", content_status="complete")
    row = store.query_one(
        "SELECT evidence_text FROM social_comment_snapshot WHERE id=?", (scs,))
    assert SENTINEL_EMAIL not in row["evidence_text"]
    assert SENTINEL_CT0 not in row["evidence_text"]
    comment_author = store.query_one(
        "SELECT author_key FROM social_comment WHERE id=?", (cid,))["author_key"]
    assert comment_author.startswith("anon_")
    assert SENTINEL_SCREEN_NAME not in comment_author


# ---------------------------------------------------------------------------
# 4) D4：旧数据只读审计（只报条数/主键，不回显值，不改写）
# ---------------------------------------------------------------------------

def test_audit_reports_legacy_rows_without_revealing_values(isolated) -> None:
    """模拟 T14 之前写入的历史行（含 token URL / 含凭据文本）→ 只读审计必须只报条数。"""
    now = store.utcnow_iso()
    store.execute(
        "INSERT INTO social_item (id, platform, source_id, kind, canonical_url, author_key, "
        "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
        ("sit_legacy_1", "xiaohongshu", "note_legacy_1", "xhs_note",
         f"https://www.xiaohongshu.com/explore/note_legacy_1?xsec_token={SENTINEL_XSEC}",
         "plain_legacy_author", now, now))
    store.execute(
        "INSERT INTO social_snapshot (id, item_id, content_hash, identity_hash, title, "
        "evidence_text, detail_status, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
        ("ssn_legacy_1", "sit_legacy_1", "ch", "ih", None,
         f"历史正文 auth_token={SENTINEL_AUTH}", "complete", now))

    report = social_redact.audit_legacy_sensitive_urls()
    assert report["dry_run"] is True and report["rewritten"] == 0, report
    assert report["items_with_query"] >= 1, report
    assert report["snapshots_with_secret_shape"] >= 1, report
    blob = json.dumps(report, ensure_ascii=False)
    for sentinel in (SENTINEL_AUTH, SENTINEL_XSEC):
        assert sentinel not in blob, "审计报告本身泄漏了凭据"
    # 只读：历史行没有被改写
    row = store.query_one("SELECT canonical_url FROM social_item WHERE id='sit_legacy_1'")
    assert SENTINEL_XSEC in row["canonical_url"], "审计不得改写历史数据"
