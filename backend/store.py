"""finintel store：SQLite 访问层 + 版本化迁移。

对应任务书 2.4 / 4.3：SQLite、开启外键、结构变更用有版本的迁移脚本。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent
MIGRATIONS_DIR = BACKEND_DIR / "migrations"
DEFAULT_DB_PATH = PROJECT_DIR / ".local" / "data" / "research.sqlite3"
OUTPUTS_DIR = PROJECT_DIR / "outputs"

_local = threading.local()

# ---------------------------------------------------------------------------
# 应用层表隔离（设计 D3 / §7.6）：同库但写路径按 scope 强制隔离
# ---------------------------------------------------------------------------

RESUME_STORE_DEFAULT = ".local/resume"

JOBSEEKER_TABLES = frozenset({
    "resume", "resume_item", "resume_text", "resume_claim", "user_supplement",
    "jobseeker_session", "job_target", "jd_requirement",
    "match_row", "match_quote", "clarification", "interview_focus", "focus_link",
    "prep_task", "prep_resource", "demand_snapshot", "capability_item", "analysis_task",
    "interview_material", "interview_research_question",
})

RESEARCH_TABLES = frozenset({
    "company", "company_alias", "research_run", "search_observation", "job",
    "job_snapshot", "run_job", "job_locator", "job_pair", "analysis", "finding", "evidence",
    # 采集通道状态（迁移 009）：主键是通道指纹，只存路径与状态，不存任何凭据。
    # 必须登记在此，否则 research 侧的 scope 断言会拦住对它的写。
    "channel_state",
    # 社交多源（迁移 016）：X / 小红书，独立于岗位表。
    "social_item", "social_snapshot", "social_comment", "social_comment_snapshot",
    "social_observation", "run_social_comment", "social_analysis", "social_finding",
    "social_evidence",
    # 社交多源运行时（迁移 017）：来源状态 / 租约预算 / 队列 / locator / 断点 /
    # AI 批次回执 / 非法证据隔离区。同样必须登记，否则 research 侧 scope 断言会拦住合法写。
    "social_source_status", "social_rate_gate", "social_task_item", "social_locator",
    "social_run_checkpoint", "social_analysis_batch", "social_evidence_quarantine",
})


def assert_scope(sql: str, scope: str) -> None:
    """scope='jobseeker' → 写语句只允许 JOBSEEKER_TABLES；scope='research' → 只允许 RESEARCH_TABLES。

    对写语句中的求职者/研究侧表名做黑名单断言：任何一条写 SQL 只要引用了对侧表名即抛 RuntimeError。
    默认开启，环境变量 FININTEL_SCOPE_CHECK=0 关闭。
    """
    if os.environ.get("FININTEL_SCOPE_CHECK", "1") == "0":
        return
    allowed = JOBSEEKER_TABLES if scope == "jobseeker" else RESEARCH_TABLES
    forbidden = RESEARCH_TABLES if scope == "jobseeker" else JOBSEEKER_TABLES
    low = sql.lower()
    for table in forbidden:
        if re.search(r"\b" + re.escape(table) + r"\b", low):
            raise RuntimeError(f"scope 越界：{scope} 写语句不得引用表 {table}")


def get_resume_store_dir() -> Path:
    """读 app_setting.resume_store_dir，相对路径解析到 PROJECT_DIR。"""
    row = query_one("SELECT value FROM app_setting WHERE key='resume_store_dir'")
    rel = (row["value"] if row else RESUME_STORE_DEFAULT) or RESUME_STORE_DEFAULT
    p = Path(rel)
    if not p.is_absolute():
        p = PROJECT_DIR / p
    return p


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def get_db_path() -> Path:
    env = os.environ.get("FININTEL_DB")
    return Path(env) if env else DEFAULT_DB_PATH


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        path = get_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        _local.conn = conn
    return conn


def apply_migrations() -> list[str]:
    conn = get_conn()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    done: list[str] = []
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = sql_file.stem
        if version in applied:
            continue
        with open(sql_file, encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, utcnow_iso()),
        )
        conn.commit()
        done.append(version)
    return done


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def jloads(text: Optional[str], default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def jdumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def query_all(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    conn = get_conn()
    return rows_to_dicts(conn.execute(sql, params).fetchall())


def query_one(sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
    conn = get_conn()
    return row_to_dict(conn.execute(sql, params).fetchone())


def execute(sql: str, params: tuple = ()) -> None:
    conn = get_conn()
    conn.execute(sql, params)
    conn.commit()
