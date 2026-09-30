-- finintel 001_initial.sql
-- 消费信贷招聘情报工具 - 初始结构（对应任务书 2.4 工程数据结构）
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS company (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    identity_status TEXT NOT NULL DEFAULT 'provisional',  -- verified / provisional
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_alias (
    id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES company(id),
    source TEXT NOT NULL,
    alias TEXT NOT NULL,
    source_company_id TEXT,
    basis TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_run (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    config_snapshot TEXT NOT NULL,             -- JSON
    state TEXT NOT NULL DEFAULT 'planned',
    started_at TEXT NOT NULL,
    collection_ended_at TEXT,
    ended_at TEXT,
    counters TEXT NOT NULL DEFAULT '{}',       -- JSON
    limits TEXT NOT NULL DEFAULT '{}',         -- JSON
    stop_reason TEXT
);

CREATE TABLE IF NOT EXISTS search_observation (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES research_run(id),
    query_id TEXT NOT NULL,
    page INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    source_job_ids TEXT NOT NULL DEFAULT '[]'  -- JSON
);

CREATE TABLE IF NOT EXISTS job (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_job_id TEXT,
    identity_key TEXT NOT NULL,
    company_id TEXT REFERENCES company(id),
    company_name_raw TEXT NOT NULL,
    title TEXT NOT NULL,
    city TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(source, identity_key)
);

CREATE TABLE IF NOT EXISTS job_snapshot (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES job(id),
    content_hash TEXT NOT NULL,
    source_url TEXT,
    fetched_at TEXT NOT NULL,
    published_at TEXT,
    published_at_raw TEXT,
    source_status TEXT NOT NULL DEFAULT 'unknown',   -- open/closed/unknown
    detail_status TEXT NOT NULL,                     -- complete/missing/partial/failed/too_large
    evidence_text TEXT NOT NULL DEFAULT '',
    responsibilities_text TEXT,
    requirements_text TEXT,
    mapping_version TEXT NOT NULL,
    UNIQUE(job_id, content_hash)
);

CREATE TABLE IF NOT EXISTS run_job (
    run_id TEXT NOT NULL REFERENCES research_run(id),
    job_id TEXT NOT NULL REFERENCES job(id),
    snapshot_id TEXT REFERENCES job_snapshot(id),
    detail_outcome TEXT NOT NULL DEFAULT 'pending',  -- pending/reused/complete/missing/partial/failed/too_large
    last_error TEXT,
    job_ref TEXT NOT NULL,                            -- 服务生成的不透明本地标识
    PRIMARY KEY (run_id, job_id)
);

CREATE TABLE IF NOT EXISTS job_locator (
    job_id TEXT PRIMARY KEY REFERENCES job(id),
    security_id TEXT,
    lid TEXT,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS job_pair (
    id TEXT PRIMARY KEY,
    left_snapshot_id TEXT NOT NULL REFERENCES job_snapshot(id),
    right_snapshot_id TEXT NOT NULL REFERENCES job_snapshot(id),
    similarity REAL,
    relation TEXT NOT NULL,                          -- suspected/duplicate/distinct
    basis TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    review_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(left_snapshot_id, right_snapshot_id)
);

CREATE TABLE IF NOT EXISTS analysis (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES job_snapshot(id),
    schema_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    taxonomy_version TEXT NOT NULL,
    model_label TEXT NOT NULL,
    role_class TEXT NOT NULL,
    relevance TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL,                            -- pending/partial/validated/analysis_failed
    result_status TEXT NOT NULL,                     -- findings_found/insufficient_information/excluded/needs_review
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    review_version INTEGER NOT NULL DEFAULT 0,
    submission_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS finding (
    id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES analysis(id),
    statement TEXT NOT NULL,
    business_domain TEXT NOT NULL,
    action_type TEXT NOT NULL,
    audience TEXT,
    market TEXT,
    product_names TEXT NOT NULL DEFAULT '[]',        -- JSON
    interpretation TEXT,
    limitations TEXT NOT NULL DEFAULT '[]',          -- JSON
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    review_version INTEGER NOT NULL DEFAULT 0,
    merged_key TEXT,
    support_level TEXT NOT NULL DEFAULT 'single',    -- single/multi/unverified
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    finding_id TEXT NOT NULL REFERENCES finding(id),
    snapshot_id TEXT NOT NULL REFERENCES job_snapshot(id),
    section TEXT NOT NULL,                           -- responsibility/requirement/background/unknown
    quote TEXT NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS review_event (
    id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,                       -- analysis/finding/company_alias/job_pair
    target_id TEXT NOT NULL,
    expected_version INTEGER NOT NULL,
    action TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    reason TEXT,
    actor TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report (
    id TEXT PRIMARY KEY,
    run_ids TEXT NOT NULL,                           -- JSON
    filters TEXT NOT NULL DEFAULT '{}',              -- JSON
    analysis_ids TEXT NOT NULL DEFAULT '[]',         -- JSON
    review_revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    output_paths TEXT NOT NULL DEFAULT '{}',         -- JSON
    status TEXT NOT NULL DEFAULT 'generated'
);

CREATE TABLE IF NOT EXISTS analysis_batch (
    token TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    snapshot_ids TEXT NOT NULL,                      -- JSON
    prompt_version TEXT NOT NULL,
    taxonomy_version TEXT NOT NULL,
    chunks TEXT NOT NULL,                            -- JSON: {snapshot_id: [{start,end}]}
    used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS submission_log (
    submission_id TEXT PRIMARY KEY,
    batch_token TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_setting (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_job_company ON job(company_id);
CREATE INDEX IF NOT EXISTS idx_snapshot_job ON job_snapshot(job_id);
CREATE INDEX IF NOT EXISTS idx_finding_analysis ON finding(analysis_id);
CREATE INDEX IF NOT EXISTS idx_evidence_finding ON evidence(finding_id);
CREATE INDEX IF NOT EXISTS idx_analysis_snapshot ON analysis(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_run_job_run ON run_job(run_id);
CREATE INDEX IF NOT EXISTS idx_search_obs_run ON search_observation(run_id);
