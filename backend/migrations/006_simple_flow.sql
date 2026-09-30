-- finintel 006_simple_flow.sql
-- 三步主流程（登录→搜索→自动整理）：登录任务表、run 编排阶段/检查点列、结果视图物化表。
-- 幂等约定与仓库既有迁移一致：表/索引用 IF NOT EXISTS；ALTER TABLE ADD COLUMN 由
-- store.apply_migrations() 的 schema_migrations 版本表保证只执行一次（同 005 约定）。
-- 只加列/建新表，不改写既有列与数据，向后兼容。
PRAGMA foreign_keys = ON;

-- 1) 登录任务（并发唯一：active=1 的行最多一行，落实“同时只允许一个登录流程”）
CREATE TABLE IF NOT EXISTS login_task (
    id               TEXT PRIMARY KEY,
    status           TEXT NOT NULL,            -- starting/waiting_user/verifying/connected/timeout/cancelled/platform_verification/env_not_ready/failed
    stage            TEXT,                     -- launching/opened/awaiting/verifying
    data_dir         TEXT NOT NULL,
    cli_path         TEXT,
    pid              INTEGER,
    timeout_seconds  INTEGER NOT NULL DEFAULT 180,
    message          TEXT,                     -- 脱敏后的最近进度提示
    stderr_tail      TEXT NOT NULL DEFAULT '', -- 脱敏、定长截断（仅诊断页）
    error_code       TEXT,                     -- 内部错误码（不直接给普通用户）
    user_message     TEXT,                     -- 面向普通用户的文案
    active           INTEGER NOT NULL DEFAULT 1,
    started_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    finished_at      TEXT,
    live_verified_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_login_task_active ON login_task(active) WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_login_task_started ON login_task(started_at DESC);

-- 2) research_run 编排扩展列（只加列）
ALTER TABLE research_run ADD COLUMN stage TEXT;
ALTER TABLE research_run ADD COLUMN stage_updated_at TEXT;
ALTER TABLE research_run ADD COLUMN checkpoint TEXT NOT NULL DEFAULT '{}';
ALTER TABLE research_run ADD COLUMN user_message TEXT;
ALTER TABLE research_run ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;
ALTER TABLE research_run ADD COLUMN query_text TEXT;
ALTER TABLE research_run ADD COLUMN filters TEXT NOT NULL DEFAULT '{}';
ALTER TABLE research_run ADD COLUMN origin TEXT NOT NULL DEFAULT 'advanced';   -- simple/advanced
ALTER TABLE research_run ADD COLUMN organized_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE research_run ADD COLUMN failed_count INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_run_origin ON research_run(origin, started_at DESC);

-- 3) 结果视图物化：公司方向（organizing 阶段生成，结果页直接按序读取）
CREATE TABLE IF NOT EXISTS run_direction (
    id                TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL REFERENCES research_run(id),
    company_id        TEXT NOT NULL REFERENCES company(id),
    business_domain   TEXT NOT NULL,
    statement         TEXT NOT NULL,
    action_type       TEXT NOT NULL,
    support_level     TEXT NOT NULL DEFAULT 'single',    -- single/multi/unverified
    review_status     TEXT NOT NULL DEFAULT 'unreviewed',
    job_count         INTEGER NOT NULL DEFAULT 0,
    evidence_count    INTEGER NOT NULL DEFAULT 0,
    latest_fetched_at TEXT,
    finding_ids       TEXT NOT NULL DEFAULT '[]',        -- JSON
    job_ids           TEXT NOT NULL DEFAULT '[]',        -- JSON
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE(run_id, company_id, business_domain, statement)
);
CREATE INDEX IF NOT EXISTS idx_run_direction_run
    ON run_direction(run_id, company_id, support_level);
