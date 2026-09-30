# -*- coding: utf-8 -*-
"""迁移 017 离线测试：空库全量应用 / 真实库副本应用 / 重复启动幂等。

**离线**：全程不发起网络请求，不调用任何真实平台采集器。
测试只在 ``tempfile`` 下的临时库里写数据；业务库 ``.local/data/research.sqlite3``
**只以只读方式打开并用 SQLite backup API 拷贝到临时文件**，绝不在其上执行任何写语句。

覆盖三场景（对应《增量设计》T02 验收）
  a. 空库从头应用 001→017：新表齐备、加列到位、CHECK 生效、016 索引全在；
  b. 真实库副本应用 017：原有记录与索引保留，非法证据进 quarantine 不静默删除；
  c. 重复启动：``apply_migrations()`` 连调两次不报错、结构不变、version 不重复。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import store  # noqa: E402

# ---------------------------------------------------------------------------
# 常量：迁移 016 建的 7 个显式索引 / 017 新增的 7 张表
# ---------------------------------------------------------------------------

INDEXES_016 = {
    "idx_social_item_platform",
    "idx_social_snapshot_item",
    "idx_social_snapshot_published",
    "idx_social_comment_item",
    "idx_social_observation_run",
    "idx_social_finding_analysis",
    "idx_social_evidence_finding",
}

TABLES_017 = {
    "social_source_status",
    "social_rate_gate",
    "social_task_item",
    "social_locator",
    "social_run_checkpoint",
    "social_analysis_batch",
    "social_evidence_quarantine",
}

NEW_SNAPSHOT_COLUMNS = {"identity_hash", "published_at_raw", "published_at_quality", "redacted"}
NEW_COMMENT_SNAPSHOT_COLUMNS = {"published_at_raw", "published_at_quality"}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

@contextmanager
def use_db(path: Path) -> Iterator[sqlite3.Connection]:
    """把 store 的连接切到指定临时库；退出时关闭并恢复原环境。"""
    prev_env = os.environ.get("FININTEL_DB")
    prev_conn = getattr(store._local, "conn", None)

    def _drop() -> None:
        conn = getattr(store._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - 清理阶段不因连接异常中断
                pass
        store._local.conn = None

    def _is_open(conn: Any) -> bool:
        """连接是否仍可用：已关闭的连接不能回填，否则后续模块会在关闭的库上操作。"""
        if conn is None:
            return False
        try:
            conn.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            return False
        return True

    try:
        _drop()
        os.environ["FININTEL_DB"] = str(path)
        yield store.get_conn()
    finally:
        _drop()
        # 只回填仍然可用的连接；否则置空，让 get_conn() 按当前 FININTEL_DB 重新打开
        store._local.conn = prev_conn if _is_open(prev_conn) else None
        if prev_env is None:
            os.environ.pop("FININTEL_DB", None)
        else:
            os.environ["FININTEL_DB"] = prev_env


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def index_names(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def columns_of(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def schema_signature(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    # 注意：store.get_conn() 设置了 row_factory=Row，Row 之间不可比较，需转成元组
    return sorted((r[0], r[1], r[2]) for r in conn.execute(
        "SELECT type, name, IFNULL(sql,'') FROM sqlite_master ORDER BY type, name").fetchall())


def migration_rows(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return sorted((r[0], r[1]) for r in
                  conn.execute("SELECT version, applied_at FROM schema_migrations").fetchall())


def row_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def new_temp_db(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    if path.exists():
        path.unlink()
    return path


# ---------------------------------------------------------------------------
# 场景 a：空库从头应用 001→017
# ---------------------------------------------------------------------------

def test_empty_db_applies_all_migrations(tmp_path: Path) -> None:
    """离线：空库从 001 一路应用到 017，新表/新列/CHECK 全部到位。"""
    with use_db(new_temp_db(tmp_path, "empty.sqlite3")) as conn:
        applied = store.apply_migrations()
        assert "017_social_runtime" in applied, f"017 未应用：{applied}"
        assert applied == sorted(applied), "迁移应按版本号顺序应用"

        names = table_names(conn)
        missing = TABLES_017 - names
        assert not missing, f"017 新表缺失：{missing}"

        snap_cols = columns_of(conn, "social_snapshot")
        assert NEW_SNAPSHOT_COLUMNS <= snap_cols, f"social_snapshot 缺列：{snap_cols}"
        cs_cols = columns_of(conn, "social_comment_snapshot")
        assert NEW_COMMENT_SNAPSHOT_COLUMNS <= cs_cols, f"social_comment_snapshot 缺列：{cs_cols}"

        obs_cols = columns_of(conn, "social_observation")
        assert "is_current" in obs_cols
        ana_cols = columns_of(conn, "social_analysis")
        assert {"is_current", "batch_count", "input_item_count"} <= ana_cols

        assert INDEXES_016 <= index_names(conn), "016 建的索引在 017 后丢失"
        assert "017_social_runtime" in {v for v, _ in migration_rows(conn)}
        # 外键一致性（017 重建过 4 张表，重建后不应留下悬空引用）
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_evidence_check_constraints(tmp_path: Path) -> None:
    """离线：两个快照 FK 全空 / 全非空 → SQLite CHECK 拒绝。"""
    with use_db(new_temp_db(tmp_path, "check.sqlite3")) as conn:
        store.apply_migrations()
        conn.execute("PRAGMA foreign_keys=OFF")  # 只验 CHECK，不引入 FK 依赖
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO social_evidence (id, finding_id, quote, start, end, stance) "
                    "VALUES ('sev_both_null','sfd_x','q',0,1,'support')")
                conn.commit()
            conn.rollback()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO social_evidence (id, finding_id, snapshot_id, "
                    "comment_snapshot_id, quote, start, end, stance) "
                    "VALUES ('sev_both_set','sfd_x','ssn_a','scs_a','q',0,1,'support')")
                conn.commit()
            conn.rollback()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO social_evidence (id, finding_id, snapshot_id, quote, "
                    "start, end, stance) VALUES ('sev_bad_stance','sfd_x','ssn_a','q',0,1,'maybe')")
                conn.commit()
            conn.rollback()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO social_evidence (id, finding_id, snapshot_id, quote, "
                    "start, end, stance) VALUES ('sev_bad_offset','sfd_x','ssn_a','q',5,3,'support')")
                conn.commit()
            conn.rollback()
        finally:
            conn.execute("PRAGMA foreign_keys=ON")


# ---------------------------------------------------------------------------
# 场景 b：真实库副本应用 017（原记录与索引必须保留）
# ---------------------------------------------------------------------------

def _seed_legacy_social(conn: sqlite3.Connection) -> dict[str, Any]:
    """在真实库副本上按 016 形状播种社交数据（含 3 条非法证据）。

    播种用的是 016 的列清单，因此也能顺带证明 016 → 017 的重建不会丢数据。
    """
    row = conn.execute("SELECT id FROM research_run WHERE origin='social' LIMIT 1").fetchone()
    if row:
        run_id = row[0]
    else:
        run_id = "run_migration_fixture"
        conn.execute(
            "INSERT INTO research_run (id, name, config_snapshot, state, started_at, counters, "
            "limits, query_text, filters, origin, stage, task_type) "
            "VALUES (?,'017 迁移测试','{}','paused','2026-09-28T00:00:00Z','{}','{}',"
                    "'离线','{}','social','paused','xhs_career')", (run_id,))
        conn.commit()

    now = "2026-09-28T00:00:00Z"
    conn.execute(
        "INSERT INTO social_item (id, platform, source_id, kind, canonical_url, author_key, "
        "first_seen_at, last_seen_at) VALUES ('sit_seed1','xiaohongshu','note_seed1','xhs_note',"
        "'https://www.xiaohongshu.com/explore/note_seed1','anon_seed',?,?)", (now, now))
    conn.execute(
        "INSERT INTO social_snapshot (id, item_id, content_hash, title, evidence_text, language, "
        "published_at, fetched_at, detail_status, collector_version, source_url_verified, "
        "identity_quality) VALUES ('ssn_seed1','sit_seed1','h1','离线标题','离线正文 一',"
        "'zh',?,?,'complete','opencli',0,'platform')", (now, now))
    conn.execute(
        "INSERT INTO social_snapshot (id, item_id, content_hash, title, evidence_text, language, "
        "published_at, fetched_at, detail_status, collector_version, source_url_verified, "
        "identity_quality) VALUES ('ssn_seed2','sit_seed1','h2','离线标题','离线正文 二',"
        "'zh',?,?,'partial','opencli',0,'platform')", (now, now))
    conn.execute(
        "INSERT INTO social_observation (id, run_id, item_id, snapshot_id, query_text, "
        "retrieval_mode, observed_at, metrics_json, coverage_json) "
        "VALUES ('sob_seed1',?,'sit_seed1','ssn_seed1','离线','keyword_search',?,'{}','{}')",
        (run_id, now))
    conn.execute(
        "INSERT INTO social_analysis (id, run_id, input_hash, schema_version, prompt_version, "
        "model_label, status, result_json, created_at) VALUES ('san_seed1',?,'ih','1.0',"
        "'social_xhs_v1','offline/model','ok','{}',?)", (run_id, now))
    conn.execute(
        "INSERT INTO social_finding (id, analysis_id, kind, title, summary_zh, "
        "limitations_json, review_status) VALUES ('sfd_seed1','san_seed1','topic','离线主题',"
        "'离线摘要','{}','auto')")
    # 评论链：run_social_comment 是 social_observation 的子表，
    # 用来验证"重建父表"时子表记录不丢（同时反证 PRAGMA foreign_keys=OFF 生效）
    conn.execute(
        "INSERT INTO social_comment (id, item_id, source_comment_id, parent_source_id, "
        "root_source_id, author_key, first_seen_at, parent_missing) "
        "VALUES ('scm_seed1','sit_seed1','c_seed1',NULL,NULL,'anon_seed',?,0)", (now,))
    conn.execute(
        "INSERT INTO social_comment_snapshot (id, comment_id, content_hash, evidence_text, "
        "published_at, fetched_at, content_status, identity_quality) "
        "VALUES ('scs_seed1','scm_seed1','hc1','离线评论',?,?,'complete','platform')",
        (now, now))
    conn.execute(
        "INSERT INTO run_social_comment (run_id, comment_snapshot_id, observation_id) "
        "VALUES (?,'scs_seed1','sob_seed1')", (run_id,))
    # 合法证据
    conn.execute(
        "INSERT INTO social_evidence (id, finding_id, snapshot_id, quote, start, end, stance) "
        "VALUES ('sev_seed_ok','sfd_seed1','ssn_seed1','离线正文',0,4,'support')")
    # 三条非法证据：FK 全空 / stance 非法 / 偏移非法
    conn.execute(
        "INSERT INTO social_evidence (id, finding_id, quote, start, end, stance) "
        "VALUES ('sev_seed_bad_fk','sfd_seed1','q',0,1,'support')")
    conn.execute(
        "INSERT INTO social_evidence (id, finding_id, snapshot_id, quote, start, end, stance) "
        "VALUES ('sev_seed_bad_stance','sfd_seed1','ssn_seed1','q',0,1,'maybe')")
    conn.execute(
        "INSERT INTO social_evidence (id, finding_id, snapshot_id, quote, start, end, stance) "
        "VALUES ('sev_seed_bad_offset','sfd_seed1','ssn_seed1','q',5,3,'support')")
    conn.commit()
    return {"run_id": run_id}


def test_real_db_copy_keeps_records_and_indexes(tmp_path: Path) -> None:
    """离线：真实库副本应用 017 → 原记录与索引一条不少、一个不丢。

    业务库当前可能已是 017（本任务已对真实库执行过一次），也可能仍是 016；
    两种起始状态都必须成立：未应用就应用，已应用则为空操作。
    """
    business = store.DEFAULT_DB_PATH
    if not business.exists():
        pytest.skip(f"业务库不存在，跳过真实库副本场景：{business}")

    before_stat = business.stat()
    copy_path = new_temp_db(tmp_path, "real_copy.sqlite3")
    # 只读打开业务库 + backup API（含 WAL 合并）→ 全程不对业务库执行写语句
    src = sqlite3.connect(f"file:{business.as_posix()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(copy_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    assert business.stat().st_size == before_stat.st_size, "业务库文件大小被改动，测试不得写业务库"

    with use_db(copy_path) as conn:
        baseline = {t: row_count(conn, t) for t in sorted(table_names(conn))}
        idx_before = index_names(conn)
        already = "017_social_runtime" in {
            r[0] for r in conn.execute("SELECT version FROM schema_migrations")}

        applied = store.apply_migrations()
        if already:
            assert applied == [], f"017 已应用时不应重复执行：{applied}"
        else:
            assert applied == ["017_social_runtime"], f"副本上 017 未应用：{applied}"

        # 1) 017 结构到位
        assert TABLES_017 <= table_names(conn)
        assert NEW_SNAPSHOT_COLUMNS <= columns_of(conn, "social_snapshot")
        assert NEW_COMMENT_SNAPSHOT_COLUMNS <= columns_of(conn, "social_comment_snapshot")
        assert "is_current" in columns_of(conn, "social_observation")

        # 2) 原有记录一条不少（schema_migrations 仅在首次应用时 +1）
        for table, count in baseline.items():
            if table in TABLES_017:
                continue
            now_count = row_count(conn, table)
            if table == "schema_migrations":
                assert now_count in (count, count + 1), f"{table} 行数异常：{count} → {now_count}"
            else:
                assert now_count == count, f"{table} 行数变化：{count} → {now_count}"

        # 3) 016 建的索引全部还在（集合比对，只增不减）
        assert INDEXES_016 <= index_names(conn)
        assert idx_before - index_names(conn) == set(), f"索引丢失：{idx_before - index_names(conn)}"

        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def _migrations_upto(tmp_path: Path, last_version: str) -> Path:
    """把 <= last_version 的迁移脚本复制到临时目录，用于构造"旧版本库"。"""
    import shutil

    target = tmp_path / f"migrations_{last_version}"
    target.mkdir(parents=True, exist_ok=True)
    for f in sorted((BACKEND_DIR / "migrations").glob("*.sql")):
        if f.stem <= last_version:
            shutil.copy2(f, target / f.name)
    return target


def test_legacy_016_state_upgrade_quarantines_invalid_evidence(tmp_path: Path,
                                                               monkeypatch: Any) -> None:
    """离线：016 状态的库升级到 017 → 合法记录保留，非法证据进 quarantine 不静默删除。

    不依赖业务库当前处于哪个版本：先把迁移目录限制到 016 建成旧库，再放开 017。
    """
    with use_db(new_temp_db(tmp_path, "legacy.sqlite3")) as conn:
        monkeypatch.setattr(store, "MIGRATIONS_DIR",
                            _migrations_upto(tmp_path, "016_social_sources"))
        pre = store.apply_migrations()
        assert "016_social_sources" in pre and "017_social_runtime" not in pre

        seeded = _seed_legacy_social(conn)
        baseline = {t: row_count(conn, t) for t in sorted(table_names(conn))}

        monkeypatch.setattr(store, "MIGRATIONS_DIR", _migrations_upto(tmp_path, "017_social_runtime"))
        applied = store.apply_migrations()
        assert applied == ["017_social_runtime"], f"017 未应用：{applied}"

        # 1) 播种的合法记录全部保留
        assert row_count(conn, "social_item") == baseline["social_item"]
        assert row_count(conn, "social_snapshot") == baseline["social_snapshot"]
        assert row_count(conn, "social_observation") == baseline["social_observation"]
        assert row_count(conn, "social_analysis") == baseline["social_analysis"]
        assert row_count(conn, "social_finding") == baseline["social_finding"]
        assert conn.execute(
            "SELECT COUNT(*) FROM social_snapshot WHERE id IN ('ssn_seed1','ssn_seed2')"
        ).fetchone()[0] == 2
        # observation 重建后 is_current 默认 1
        assert conn.execute(
            "SELECT is_current FROM social_observation WHERE id='sob_seed1'").fetchone()[0] == 1
        # 父表 social_observation 被重建，子表 run_social_comment 的记录必须保留
        assert row_count(conn, "run_social_comment") == baseline["run_social_comment"]
        child = conn.execute(
            "SELECT run_id, comment_snapshot_id, observation_id FROM run_social_comment "
            "WHERE comment_snapshot_id='scs_seed1'").fetchone()
        assert child is not None, "重建父表后子表记录丢失"
        assert (child[0], child[1], child[2]) == (seeded["run_id"], "scs_seed1", "sob_seed1")

        # 2) 合法证据保留；非法证据进 quarantine，不静默删除
        assert conn.execute(
            "SELECT COUNT(*) FROM social_evidence WHERE id='sev_seed_ok'").fetchone()[0] == 1
        quarantined = {r[0]: r[1] for r in conn.execute(
            "SELECT id, reason FROM social_evidence_quarantine").fetchall()}
        assert quarantined.get("sev_seed_bad_fk") == "FK_PAIR_INVALID"
        assert quarantined.get("sev_seed_bad_stance") == "STANCE_INVALID"
        assert quarantined.get("sev_seed_bad_offset") == "OFFSET_INVALID"
        assert conn.execute(
            "SELECT COUNT(*) FROM social_evidence WHERE id LIKE 'sev_seed_bad%'"
        ).fetchone()[0] == 0, "非法证据不应留在正式表里"

        # 3) 旧行 identity_hash 退化为 content_hash（保证旧证据偏移可解析）
        assert conn.execute(
            "SELECT COUNT(*) FROM social_snapshot WHERE identity_hash=content_hash "
            "AND id LIKE 'ssn_seed%'").fetchone()[0] == 2

        # 4) 索引与外键
        assert INDEXES_016 <= index_names(conn)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# ---------------------------------------------------------------------------
# 场景 c：重复启动
# ---------------------------------------------------------------------------

def test_apply_migrations_twice_is_idempotent(tmp_path: Path) -> None:
    """离线：连续两次 apply_migrations() → 不报错、结构不变、version 不重复。"""
    with use_db(new_temp_db(tmp_path, "twice.sqlite3")) as conn:
        first = store.apply_migrations()
        assert "017_social_runtime" in first
        sig_first = schema_signature(conn)
        mig_first = migration_rows(conn)
        counts_first = {t: row_count(conn, t) for t in sorted(table_names(conn))}

        second = store.apply_migrations()
        assert second == [], f"第二次调用不应再应用任何迁移：{second}"

        assert schema_signature(conn) == sig_first, "表/索引结构发生变化"
        assert migration_rows(conn) == mig_first, "schema_migrations 记录发生变化"
        assert [v for v, _ in migration_rows(conn)].count("017_social_runtime") == 1
        assert {t: row_count(conn, t) for t in sorted(table_names(conn))} == counts_first

        # 第三次的显式幂等（连续启动是真实场景：服务重启就会走这条路）
        third = store.apply_migrations()
        assert third == []
        assert schema_signature(conn) == sig_first


def test_migration_never_touches_business_db() -> None:
    """离线：测试期间不得把 FININTEL_DB 指向业务库（防误写断言）。"""
    assert os.environ.get("FININTEL_DB") != str(store.DEFAULT_DB_PATH), \
        "FININTEL_DB 指向了业务库，测试隔离失效"


def test_backup_source_is_readonly(tmp_path: Path) -> None:
    """离线：业务库以只读方式打开，任何写语句都必须失败（隔离的硬保证）。"""
    business = store.DEFAULT_DB_PATH
    if not business.exists():
        pytest.skip("业务库不存在")
    conn = sqlite3.connect(f"file:{business.as_posix()}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE zz_should_not_exist (id TEXT)")
    finally:
        conn.close()
