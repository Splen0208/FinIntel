-- 016_social_sources.sql：多源社交情报（X 海外热点 / 小红书岗位讨论）。
--
-- 背景（任务书 §7）：新增社交采集源，数据不进 job 表，不复用 analysis 表的岗位专属字段。
-- research_run 增加 task_type 区分 boss_research / x_topics / xhs_career。
-- 所有 ID 用文本；时间 UTC ISO8601；JSON 列做输入校验；外键启用。

-- 1) research_run 增加 task_type（默认 boss_research，兼容旧行）
ALTER TABLE research_run ADD COLUMN task_type TEXT NOT NULL DEFAULT 'boss_research';

-- 2) 社交内容条目（帖子/笔记），UNIQUE(platform, source_id)
CREATE TABLE IF NOT EXISTS social_item (
    id             TEXT PRIMARY KEY,
    platform       TEXT NOT NULL,                -- x / xiaohongshu
    source_id      TEXT NOT NULL,                -- 平台原始帖子/笔记 ID
    kind           TEXT NOT NULL,                -- x_post / xhs_note
    canonical_url  TEXT,
    author_key     TEXT,                         -- 作者匿名键
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    UNIQUE(platform, source_id)
);

-- 3) 不可变原文快照，UNIQUE(item_id, content_hash)
CREATE TABLE IF NOT EXISTS social_snapshot (
    id                  TEXT PRIMARY KEY,
    item_id             TEXT NOT NULL REFERENCES social_item(id),
    content_hash        TEXT NOT NULL,
    title               TEXT,
    evidence_text       TEXT NOT NULL,           -- 不可变原文（脱敏后）
    language            TEXT,
    published_at        TEXT,
    fetched_at          TEXT NOT NULL,
    detail_status       TEXT NOT NULL,           -- complete / partial / summary / missing
    collector_version   TEXT,
    source_url_verified INTEGER NOT NULL DEFAULT 0,
    identity_quality    TEXT NOT NULL DEFAULT 'platform',  -- platform / synthetic
    UNIQUE(item_id, content_hash)
);

-- 4) 评论，父评论可未采集（parent_source_id 可为空，parent_missing 标记）
CREATE TABLE IF NOT EXISTS social_comment (
    id               TEXT PRIMARY KEY,
    item_id          TEXT NOT NULL REFERENCES social_item(id),
    source_comment_id TEXT NOT NULL,
    parent_source_id TEXT,
    root_source_id   TEXT,
    author_key       TEXT,
    first_seen_at    TEXT NOT NULL,
    parent_missing   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(item_id, source_comment_id)
);

-- 5) 不可变评论版本
CREATE TABLE IF NOT EXISTS social_comment_snapshot (
    id            TEXT PRIMARY KEY,
    comment_id    TEXT NOT NULL REFERENCES social_comment(id),
    content_hash  TEXT NOT NULL,
    evidence_text TEXT NOT NULL,
    published_at  TEXT,
    fetched_at    TEXT NOT NULL,
    content_status TEXT NOT NULL,                -- complete / partial / truncated
    identity_quality TEXT NOT NULL DEFAULT 'platform',
    UNIQUE(comment_id, content_hash)
);

-- 6) 来源观察：同一帖子多次搜索命中留关联；互动指标随观察变化
CREATE TABLE IF NOT EXISTS social_observation (
    id             TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    item_id        TEXT NOT NULL REFERENCES social_item(id),
    snapshot_id    TEXT REFERENCES social_snapshot(id),
    query_text     TEXT,
    retrieval_mode TEXT NOT NULL,                -- keyword_search / account_watch / feed / csv_import
    observed_at    TEXT NOT NULL,
    metrics_json   TEXT NOT NULL DEFAULT '{}',   -- like/reply/repost/view/bookmark（可空值）
    coverage_json  TEXT NOT NULL DEFAULT '{}',   -- reported_total/fetched_count/has_more/stop_reason...
    ingestion_method TEXT,
    provenance_json TEXT
);

-- 7) 某任务确实读取过哪些评论版本
CREATE TABLE IF NOT EXISTS run_social_comment (
    run_id              TEXT NOT NULL,
    comment_snapshot_id TEXT NOT NULL REFERENCES social_comment_snapshot(id),
    observation_id      TEXT REFERENCES social_observation(id),
    UNIQUE(run_id, comment_snapshot_id)
);

-- 8) 社交 AI 分析回执（幂等复用）
CREATE TABLE IF NOT EXISTS social_analysis (
    id             TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    input_hash     TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model_label    TEXT NOT NULL,
    status         TEXT NOT NULL,                -- ok / failed / insufficient_evidence
    result_json    TEXT,
    created_at     TEXT NOT NULL
);

-- 9) 社交发现（主题/线索）
CREATE TABLE IF NOT EXISTS social_finding (
    id              TEXT PRIMARY KEY,
    analysis_id     TEXT NOT NULL REFERENCES social_analysis(id),
    kind            TEXT NOT NULL,               -- topic / lead
    title           TEXT NOT NULL,
    summary_zh      TEXT,
    limitations_json TEXT NOT NULL DEFAULT '{}',
    review_status   TEXT NOT NULL DEFAULT 'auto'
);

-- 10) 社交证据（支持/反例/上下文），两个快照 FK 恰有一个非空
CREATE TABLE IF NOT EXISTS social_evidence (
    id                  TEXT PRIMARY KEY,
    finding_id          TEXT NOT NULL REFERENCES social_finding(id),
    snapshot_id         TEXT REFERENCES social_snapshot(id),
    comment_snapshot_id TEXT REFERENCES social_comment_snapshot(id),
    quote               TEXT NOT NULL,
    start               INTEGER,
    end                 INTEGER,
    stance              TEXT NOT NULL             -- support / contradict / context
);

-- 索引：覆盖 platform+source_id、run_id、item_id+fetched_at、published_at、评论、finding
CREATE INDEX IF NOT EXISTS idx_social_item_platform ON social_item(platform, source_id);
CREATE INDEX IF NOT EXISTS idx_social_snapshot_item ON social_snapshot(item_id, fetched_at);
CREATE INDEX IF NOT EXISTS idx_social_snapshot_published ON social_snapshot(published_at);
CREATE INDEX IF NOT EXISTS idx_social_comment_item ON social_comment(item_id, source_comment_id);
CREATE INDEX IF NOT EXISTS idx_social_observation_run ON social_observation(run_id);
CREATE INDEX IF NOT EXISTS idx_social_finding_analysis ON social_finding(analysis_id);
CREATE INDEX IF NOT EXISTS idx_social_evidence_finding ON social_evidence(finding_id);
