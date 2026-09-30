-- finintel 005_upstream_reality.sql
-- Persist layered upstream health, per-call audit evidence, and real source identity.
CREATE TABLE IF NOT EXISTS health_check (
    key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    checked_at TEXT,
    elapsed_ms INTEGER,
    detail TEXT,
    error_code TEXT,
    error_message TEXT,
    operator_actions TEXT NOT NULL DEFAULT '[]',
    evidence TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS upstream_call (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    command TEXT NOT NULL,
    argv_redacted TEXT NOT NULL,
    data_dir TEXT NOT NULL,
    started_at TEXT NOT NULL,
    elapsed_ms INTEGER,
    layer TEXT,
    ok INTEGER,
    error_code TEXT,
    result_summary TEXT,
    ref TEXT,
    FOREIGN KEY(run_id) REFERENCES research_run(id)
);

ALTER TABLE job_snapshot ADD COLUMN source_platform TEXT;
ALTER TABLE job_snapshot ADD COLUMN source_job_id TEXT;
ALTER TABLE job_snapshot ADD COLUMN source_url_verified INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_job_source ON job(source);
CREATE INDEX IF NOT EXISTS idx_upstream_call_run ON upstream_call(run_id, started_at);
