-- 017_social_runtime.sql：社交多源运行时表 + 存量表约束加固。
--
-- 目标（对应《增量设计》2.2 / 2.3 与《指令》E1/E2/E3）：
--   B1 队列 / B2 断点+locator / B3 原子租约预算 / E2 当前口径 / E3 约束 / E4 批次回执 / G 来源状态
--
-- 铁律
--   1. 不覆盖已在本机应用的 016_social_sources.sql（016 于 2026-09-28T07:57:06Z 应用）。
--      本脚本是纯增量：016 建的表/索引/数据一律保留。
--   2. 存量表需要加 CHECK / FK / 改唯一键时，走「五步重建」：
--        新建 __new 表 → INSERT SELECT 复制原记录 → DROP 旧表 → RENAME → 重建索引。
--      索引必须逐个重建（DROP TABLE 会连带删除其索引）。
--   3. 非法证据行进 social_evidence_quarantine，不静默丢弃、不静默删除。
--   4. 幂等：由 store.apply_migrations() 的 schema_migrations 版本登记保证
--      （同一 version 只会执行一次）。新增表/索引一律带 IF NOT EXISTS；
--      存量表的 ALTER ADD COLUMN 与五步重建**不是**语句级幂等，
--      因此**不要绕过 apply_migrations() 直接重复 executescript 本文件**。
--   5. PRAGMA foreign_keys 必须在事务外执行；store.apply_migrations() 用
--      executescript，会先 COMMIT，故本文件开头的 PRAGMA 能生效。
--   6. 不写入、不记录任何凭据：locator_value 只存采集路径内部使用的定位串。

PRAGMA foreign_keys = OFF;

-- ===========================================================================
-- 1) 来源状态（平台 + 会话维度，取代 /api/sources 的硬编码 unknown/unverified）
-- ===========================================================================
CREATE TABLE IF NOT EXISTS social_source_status (
    id                 TEXT PRIMARY KEY,
    platform           TEXT NOT NULL,                       -- x / xiaohongshu
    session_key        TEXT NOT NULL DEFAULT 'default',
    dependency_state   TEXT NOT NULL DEFAULT 'unknown',     -- unknown|ready|missing|version_unsupported
    connection_state   TEXT NOT NULL DEFAULT 'unknown',     -- unknown|ready|not_ready
    auth_state         TEXT NOT NULL DEFAULT 'unknown',     -- unknown|authenticated|auth_required|expired
    capability_json    TEXT NOT NULL DEFAULT '{}',
    provider_version   TEXT,
    exe_path           TEXT,                                -- 冻结的可执行程序（路径，不是凭据）
    node_path          TEXT,
    checked_at         TEXT,
    check_source       TEXT,                                -- probe|collect_ok|collect_failed
    last_error_code    TEXT,
    last_error_summary TEXT,                                -- 必须过 redact()，绝不落凭据
    updated_at         TEXT,
    UNIQUE(platform, session_key)
);
CREATE INDEX IF NOT EXISTS idx_social_source_status_platform
    ON social_source_status(platform, session_key);

-- ===========================================================================
-- 2) locator：token locator 与公开 URL 分离存储，带 TTL。
--    先建 locator，再建引用它的 social_task_item，避免悬空外键。
-- ===========================================================================
CREATE TABLE IF NOT EXISTS social_locator (
    id            TEXT PRIMARY KEY,
    item_id       TEXT NOT NULL REFERENCES social_item(id),
    platform      TEXT NOT NULL,
    kind          TEXT NOT NULL,                            -- xhs_token_url / x_tweet_id
    public_url    TEXT,                                     -- 已去查询参数，可对外
    locator_value TEXT NOT NULL,                            -- 敏感：仅采集路径内部读取，禁止进任何通用 API
    url_verified  INTEGER NOT NULL DEFAULT 0,
    expires_at    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE(item_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_social_locator_expiry ON social_locator(expires_at);

-- ===========================================================================
-- 3) 任务条目队列（B1：搜索命中先全量入库并建立队列）
-- ===========================================================================
CREATE TABLE IF NOT EXISTS social_task_item (
    id                 TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL REFERENCES research_run(id) ON DELETE CASCADE,
    item_id            TEXT NOT NULL REFERENCES social_item(id),
    seq                INTEGER NOT NULL,
    detail_state       TEXT NOT NULL DEFAULT 'pending',     -- pending|running|done|skipped|failed|need_relocate
    comment_state      TEXT NOT NULL DEFAULT 'pending',     -- pending|running|done|skipped|failed|not_applicable
    detail_attempts    INTEGER NOT NULL DEFAULT 0,
    comment_attempts   INTEGER NOT NULL DEFAULT 0,
    detail_status      TEXT,                                -- 最近一次正文状态
    last_error_code    TEXT,
    last_error_summary TEXT,                                -- 必须过 redact()
    locator_id         TEXT REFERENCES social_locator(id),
    updated_at         TEXT,
    UNIQUE(run_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_social_task_item_detail_queue
    ON social_task_item(run_id, detail_state, seq);
CREATE INDEX IF NOT EXISTS idx_social_task_item_comment_queue
    ON social_task_item(run_id, comment_state, seq);

-- ===========================================================================
-- 4) 租约 / 预算 / 冷却（按平台+会话分行，不是全局；AI 走独立行不占平台预算）
-- ===========================================================================
CREATE TABLE IF NOT EXISTS social_rate_gate (
    platform        TEXT NOT NULL,
    session_key     TEXT NOT NULL DEFAULT 'default',
    lease_run_id    TEXT,
    lease_kind      TEXT,                                   -- platform|ai|browser
    lease_until     REAL,
    window_start    REAL,
    window_count    INTEGER NOT NULL DEFAULT 0,
    last_request_at REAL,
    cooldown_until  REAL,
    cooldown_reason TEXT,
    updated_at      TEXT,
    PRIMARY KEY (platform, session_key)
);

-- ===========================================================================
-- 5) 任务断点（B2）
-- ===========================================================================
CREATE TABLE IF NOT EXISTS social_run_checkpoint (
    run_id                TEXT PRIMARY KEY REFERENCES research_run(id) ON DELETE CASCADE,
    stage                 TEXT NOT NULL DEFAULT 'queued',
    query_text            TEXT,
    cursor                TEXT,
    budget_json           TEXT NOT NULL DEFAULT '{}',
    counters_json         TEXT NOT NULL DEFAULT '{}',
    last_error_code       TEXT,
    last_error_summary    TEXT,
    analysis_batches_done INTEGER NOT NULL DEFAULT 0,
    updated_at            TEXT
);

-- ===========================================================================
-- 6) AI 批次回执（E4）
-- ===========================================================================
CREATE TABLE IF NOT EXISTS social_analysis_batch (
    id               TEXT PRIMARY KEY,
    analysis_id      TEXT NOT NULL REFERENCES social_analysis(id),
    batch_index      INTEGER NOT NULL,
    input_hash       TEXT NOT NULL,
    input_char_count INTEGER NOT NULL DEFAULT 0,
    item_count       INTEGER NOT NULL DEFAULT 0,
    model_label      TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    status           TEXT NOT NULL,                         -- ok|failed|insufficient_evidence
    error_code       TEXT,
    elapsed_ms       INTEGER,
    result_hash      TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE(analysis_id, batch_index)
);
CREATE INDEX IF NOT EXISTS idx_social_analysis_batch_analysis
    ON social_analysis_batch(analysis_id, batch_index);

-- ===========================================================================
-- 7) 非法证据隔离区（E3 重建时兜底，也承接 save_evidence 的校验失败）
-- ===========================================================================
CREATE TABLE IF NOT EXISTS social_evidence_quarantine (
    id                  TEXT PRIMARY KEY,
    finding_id          TEXT,
    snapshot_id         TEXT,
    comment_snapshot_id TEXT,
    quote               TEXT,
    start               INTEGER,
    end                 INTEGER,
    stance              TEXT,
    reason              TEXT NOT NULL,
    quarantined_at      TEXT NOT NULL
);

-- ===========================================================================
-- 8) social_comment_snapshot：加列（纯增量，无约束变化 → ALTER 即可）
-- ===========================================================================
ALTER TABLE social_comment_snapshot ADD COLUMN published_at_raw TEXT;
ALTER TABLE social_comment_snapshot ADD COLUMN published_at_quality TEXT NOT NULL DEFAULT 'unknown';

-- ===========================================================================
-- 9) social_snapshot：加 identity_hash 等列，并把唯一键从
--    UNIQUE(item_id, content_hash) 换成 UNIQUE(item_id, identity_hash)。
--    必须重建：016 的 (item_id, content_hash) 唯一键会阻止
--    「正文相同但 title/detail_status 变化」产生新版本快照（E1）。
-- ===========================================================================
ALTER TABLE social_snapshot ADD COLUMN identity_hash TEXT;
ALTER TABLE social_snapshot ADD COLUMN published_at_raw TEXT;
ALTER TABLE social_snapshot ADD COLUMN published_at_quality TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE social_snapshot ADD COLUMN redacted INTEGER NOT NULL DEFAULT 0;

-- 旧行回填：identity_hash 退化为 content_hash，保证已被引用的证据偏移依然可解析。
UPDATE social_snapshot SET identity_hash = content_hash WHERE identity_hash IS NULL;

DROP TABLE IF EXISTS social_snapshot__new;
CREATE TABLE social_snapshot__new (
    id                  TEXT PRIMARY KEY,
    item_id             TEXT NOT NULL REFERENCES social_item(id),
    content_hash        TEXT NOT NULL,
    identity_hash       TEXT,                               -- 去重键（见 §5.4）
    title               TEXT,
    evidence_text       TEXT NOT NULL,                      -- 不可变原文（脱敏后）
    language            TEXT,
    published_at        TEXT,
    published_at_raw    TEXT,                               -- 平台原始时间串（可能是相对时间）
    published_at_quality TEXT NOT NULL DEFAULT 'unknown',   -- absolute|relative_unparsed|unknown
    fetched_at          TEXT NOT NULL,
    detail_status       TEXT NOT NULL,                      -- complete / partial / summary / missing
    collector_version   TEXT,
    source_url_verified INTEGER NOT NULL DEFAULT 0,
    identity_quality    TEXT NOT NULL DEFAULT 'platform',   -- platform / synthetic
    redacted            INTEGER NOT NULL DEFAULT 0,
    UNIQUE(item_id, identity_hash)
);
INSERT INTO social_snapshot__new
    (id, item_id, content_hash, identity_hash, title, evidence_text, language,
     published_at, published_at_raw, published_at_quality, fetched_at, detail_status,
     collector_version, source_url_verified, identity_quality, redacted)
SELECT id, item_id, content_hash, identity_hash, title, evidence_text, language,
       published_at, published_at_raw, published_at_quality, fetched_at, detail_status,
       collector_version, source_url_verified, identity_quality, redacted
  FROM social_snapshot;
DROP TABLE social_snapshot;
ALTER TABLE social_snapshot__new RENAME TO social_snapshot;
CREATE INDEX IF NOT EXISTS idx_social_snapshot_item      ON social_snapshot(item_id, fetched_at);
CREATE INDEX IF NOT EXISTS idx_social_snapshot_published ON social_snapshot(published_at);
CREATE INDEX IF NOT EXISTS idx_social_snapshot_identity  ON social_snapshot(identity_hash);

-- ===========================================================================
-- 10) social_evidence 重建：补 CHECK（两 FK 恰有一个非空 / stance 合法 / 偏移合法）
--     合法行原样保留；非法行进 quarantine，不静默删除。
-- ===========================================================================
INSERT OR IGNORE INTO social_evidence_quarantine
    (id, finding_id, snapshot_id, comment_snapshot_id, quote, start, end, stance,
     reason, quarantined_at)
SELECT id, finding_id, snapshot_id, comment_snapshot_id, quote, start, end, stance,
       'FK_PAIR_INVALID', 'MIGRATION_017'
  FROM social_evidence
 WHERE (snapshot_id IS NULL) = (comment_snapshot_id IS NULL);

INSERT OR IGNORE INTO social_evidence_quarantine
    (id, finding_id, snapshot_id, comment_snapshot_id, quote, start, end, stance,
     reason, quarantined_at)
SELECT id, finding_id, snapshot_id, comment_snapshot_id, quote, start, end, stance,
       'STANCE_INVALID', 'MIGRATION_017'
  FROM social_evidence
 WHERE (snapshot_id IS NULL) != (comment_snapshot_id IS NULL)
   AND (stance IS NULL OR stance NOT IN ('support', 'contradict', 'context'));

INSERT OR IGNORE INTO social_evidence_quarantine
    (id, finding_id, snapshot_id, comment_snapshot_id, quote, start, end, stance,
     reason, quarantined_at)
SELECT id, finding_id, snapshot_id, comment_snapshot_id, quote, start, end, stance,
       'OFFSET_INVALID', 'MIGRATION_017'
  FROM social_evidence
 WHERE (snapshot_id IS NULL) != (comment_snapshot_id IS NULL)
   AND (stance IN ('support', 'contradict', 'context'))
   AND start IS NOT NULL
   AND NOT (start >= 0 AND end > start);

DROP TABLE IF EXISTS social_evidence__new;
CREATE TABLE social_evidence__new (
    id                  TEXT PRIMARY KEY,
    finding_id          TEXT NOT NULL REFERENCES social_finding(id),
    snapshot_id         TEXT REFERENCES social_snapshot(id),
    comment_snapshot_id TEXT REFERENCES social_comment_snapshot(id),
    quote               TEXT NOT NULL,
    start               INTEGER,
    end                 INTEGER,
    stance              TEXT NOT NULL,
    CHECK ((snapshot_id IS NULL) != (comment_snapshot_id IS NULL)),
    CHECK (stance IN ('support', 'contradict', 'context')),
    CHECK (start IS NULL OR (start >= 0 AND end > start))
);
INSERT INTO social_evidence__new
    (id, finding_id, snapshot_id, comment_snapshot_id, quote, start, end, stance)
SELECT id, finding_id, snapshot_id, comment_snapshot_id, quote, start, end, stance
  FROM social_evidence
 WHERE (snapshot_id IS NULL) != (comment_snapshot_id IS NULL)
   AND stance IN ('support', 'contradict', 'context')
   AND (start IS NULL OR (start >= 0 AND end > start));
DROP TABLE social_evidence;
ALTER TABLE social_evidence__new RENAME TO social_evidence;
CREATE INDEX IF NOT EXISTS idx_social_evidence_finding  ON social_evidence(finding_id);
CREATE INDEX IF NOT EXISTS idx_social_evidence_snapshot ON social_evidence(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_social_evidence_comment  ON social_evidence(comment_snapshot_id);

-- ===========================================================================
-- 11) social_observation 重建：run_id 加外键 + is_current 口径（E2）
--     「每次命中」都留记录（审计），但同 (run_id, item_id) 只有一条 is_current=1。
-- ===========================================================================
DROP TABLE IF EXISTS social_observation__new;
CREATE TABLE social_observation__new (
    id               TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL REFERENCES research_run(id) ON DELETE CASCADE,
    item_id          TEXT NOT NULL REFERENCES social_item(id),
    snapshot_id      TEXT REFERENCES social_snapshot(id),
    query_text       TEXT,
    retrieval_mode   TEXT NOT NULL,
    observed_at      TEXT NOT NULL,
    metrics_json     TEXT NOT NULL DEFAULT '{}',
    coverage_json    TEXT NOT NULL DEFAULT '{}',
    ingestion_method TEXT,
    provenance_json  TEXT,
    is_current       INTEGER NOT NULL DEFAULT 1
);
INSERT INTO social_observation__new
    (id, run_id, item_id, snapshot_id, query_text, retrieval_mode, observed_at,
     metrics_json, coverage_json, ingestion_method, provenance_json, is_current)
SELECT id, run_id, item_id, snapshot_id, query_text, retrieval_mode, observed_at,
       metrics_json, coverage_json, ingestion_method, provenance_json, 1
  FROM social_observation;
DROP TABLE social_observation;
ALTER TABLE social_observation__new RENAME TO social_observation;
CREATE INDEX IF NOT EXISTS idx_social_observation_run      ON social_observation(run_id);
CREATE INDEX IF NOT EXISTS idx_social_observation_run_item ON social_observation(run_id, item_id, is_current);

-- ===========================================================================
-- 12) social_analysis 重建：run_id 加外键 + 当前版本口径（E2）
-- ===========================================================================
DROP TABLE IF EXISTS social_analysis__new;
CREATE TABLE social_analysis__new (
    id               TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL REFERENCES research_run(id) ON DELETE CASCADE,
    input_hash       TEXT NOT NULL,
    schema_version   TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    model_label      TEXT NOT NULL,
    status           TEXT NOT NULL,
    result_json      TEXT,
    created_at       TEXT NOT NULL,
    is_current       INTEGER NOT NULL DEFAULT 0,
    batch_count      INTEGER NOT NULL DEFAULT 0,
    input_item_count INTEGER NOT NULL DEFAULT 0
);
INSERT INTO social_analysis__new
    (id, run_id, input_hash, schema_version, prompt_version, model_label,
     status, result_json, created_at, is_current, batch_count, input_item_count)
SELECT id, run_id, input_hash, schema_version, prompt_version, model_label,
       status, result_json, created_at, 0, 0, 0
  FROM social_analysis;
DROP TABLE social_analysis;
ALTER TABLE social_analysis__new RENAME TO social_analysis;
CREATE INDEX IF NOT EXISTS idx_social_analysis_run_current ON social_analysis(run_id, is_current);

-- ===========================================================================
-- 13) run_social_comment 重建：run_id 加外键
-- ===========================================================================
DROP TABLE IF EXISTS run_social_comment__new;
CREATE TABLE run_social_comment__new (
    run_id              TEXT NOT NULL REFERENCES research_run(id) ON DELETE CASCADE,
    comment_snapshot_id TEXT NOT NULL REFERENCES social_comment_snapshot(id),
    observation_id      TEXT REFERENCES social_observation(id),
    UNIQUE(run_id, comment_snapshot_id)
);
INSERT OR IGNORE INTO run_social_comment__new
    (run_id, comment_snapshot_id, observation_id)
SELECT run_id, comment_snapshot_id, observation_id FROM run_social_comment;
DROP TABLE run_social_comment;
ALTER TABLE run_social_comment__new RENAME TO run_social_comment;

PRAGMA foreign_keys = ON;
