-- finintel 002_jobseeker.sql
-- 求职者模式增量：向后兼容（只加列 + 建新表），幂等由 schema_migrations 保证
PRAGMA foreign_keys = ON;

-- ==========================================================================
-- 一、现有表加列
-- ==========================================================================

-- C3：报告类型分流
ALTER TABLE report ADD COLUMN kind TEXT NOT NULL DEFAULT 'company_intel';
--   company_intel    既有公司情报报告（既有行默认落此值）
--   jobseeker_prep   求职准备报告（ref_id = jobseeker_session.id）
--   industry_demand  行业需求报告（ref_id = jobseeker_session.id，阶段二）
ALTER TABLE report ADD COLUMN ref_id TEXT;

-- C1：分析包类型；ref_id 承载 jobseeker_session.id（research 侧为空）
ALTER TABLE analysis_batch ADD COLUMN payload_kind TEXT NOT NULL DEFAULT 'company_finding';
--   company_finding / jd_match / prep_plan
ALTER TABLE analysis_batch ADD COLUMN ref_id TEXT;

-- C4：经验层级（路径一过滤与标注；classify.classify_experience_level 写入）
ALTER TABLE job_snapshot ADD COLUMN experience_level TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE job            ADD COLUMN experience_level TEXT NOT NULL DEFAULT 'unknown';

-- ==========================================================================
-- 二、新增表（P0：阶段一）
-- ==========================================================================

-- 简历主表（不存原始姓名/手机/邮箱原文；evidence_text 为偏移基准，生成后不可改）
CREATE TABLE IF NOT EXISTS resume (
    id                TEXT PRIMARY KEY,
    label             TEXT NOT NULL,
    source_type       TEXT NOT NULL DEFAULT 'text',   -- text/pdf/docx
    file_name         TEXT,                            -- 已脱敏文件名
    file_path         TEXT,                            -- resume_store_dir 下绝对路径；text 来源为空
    evidence_text     TEXT NOT NULL DEFAULT '',        -- normalize_evidence_text 后正文；删除时清空
    char_count        INTEGER NOT NULL DEFAULT 0,
    parse_status      TEXT NOT NULL DEFAULT 'pending', -- pending/parsed/partial/failed/too_large
    parse_warnings    TEXT NOT NULL DEFAULT '[]',      -- JSON array[string]
    failure_code      TEXT,                            -- RESUME_UNPARSEABLE / RESUME_TOO_LARGE / DEPENDENCY_MISSING
    failure_message   TEXT,
    section_ranges    TEXT NOT NULL DEFAULT '{}',      -- JSON {"education":[s,e],"experience":[s,e],...}
    mapping_version   TEXT NOT NULL DEFAULT 'evidence_map_v1',
    redaction_note    TEXT,                            -- 脱敏说明（固定文案，UI 直接展示）
    deleted_at        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- 简历结构化条目（start/end 相对 resume.evidence_text）
CREATE TABLE IF NOT EXISTS resume_item (
    id             TEXT PRIMARY KEY,
    resume_id      TEXT NOT NULL REFERENCES resume(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL DEFAULT 'other',   -- education/experience/project/skill/summary/other
    title          TEXT NOT NULL DEFAULT '',
    org            TEXT,
    period_raw     TEXT,
    start          INTEGER,                          -- 半开区间起点（可为 NULL：用户手工新增项）
    end            INTEGER,
    confidence     REAL NOT NULL DEFAULT 0.5,        -- 0~1，parser 产出；user 来源固定 1.0
    source         TEXT NOT NULL DEFAULT 'parser',   -- parser/user（user 项不得被重新解析覆盖）
    review_status  TEXT NOT NULL DEFAULT 'unreviewed',
    review_version INTEGER NOT NULL DEFAULT 0,
    seq            INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- 一次求职准备会话
CREATE TABLE IF NOT EXISTS jobseeker_session (
    id                 TEXT PRIMARY KEY,
    mode               TEXT NOT NULL DEFAULT 'jobseeker',   -- 固定 jobseeker（保留字段便于审计）
    route              TEXT NOT NULL DEFAULT 'match',       -- match(路径二) / explore(路径一)
    industry           TEXT,
    role_direction     TEXT,
    experience_level   TEXT NOT NULL DEFAULT 'unknown',     -- intern/junior/mid/senior/expert/unknown
    target_company_ids TEXT NOT NULL DEFAULT '[]',          -- JSON array[company_id]
    background_note    TEXT,                                -- 个人背景简介（可空；空则只给通用建议）
    job_target_id      TEXT,
    resume_id          TEXT,
    status             TEXT NOT NULL DEFAULT 'draft',
    --   draft/jd_ready/resume_ready/matched/focus_ready/plan_ready/failed
    failure_code       TEXT,
    failure_message    TEXT,
    model_label        TEXT,
    prompt_version     TEXT,
    schema_version     TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

-- 目标 JD 统一表示（library 来源只存快照引用，不复制正文）
CREATE TABLE IF NOT EXISTS job_target (
    id                    TEXT PRIMARY KEY,
    session_id            TEXT REFERENCES jobseeker_session(id) ON DELETE SET NULL,
    source_kind           TEXT NOT NULL,          -- library/paste
    snapshot_id           TEXT,                   -- library 来源；paste 为空；不强外键（PRD §10）
    job_id                TEXT,
    pasted_title          TEXT,
    pasted_company_raw    TEXT,
    title                 TEXT NOT NULL DEFAULT '',
    company_display       TEXT,
    evidence_text         TEXT NOT NULL DEFAULT '',
    responsibilities_text TEXT,
    requirements_text     TEXT,
    section_ranges        TEXT NOT NULL DEFAULT '{}',  -- JSON {"responsibility":[s,e],"requirement":[s,e]}
    mapping_version       TEXT NOT NULL DEFAULT 'evidence_map_v1',
    created_at            TEXT NOT NULL
);

-- JD 要求条目（F-05 五类）
CREATE TABLE IF NOT EXISTS jd_requirement (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    job_target_id   TEXT NOT NULL,
    category        TEXT NOT NULL,
    --   core_task/must_have/nice_to_have/scenario_experience/ambiguous
    text            TEXT NOT NULL,
    judgement_note  TEXT NOT NULL DEFAULT '',   -- ambiguous 必填"为什么模糊"
    source_ref      TEXT,                       -- 包内临时 ref（如 "R1"），仅一次提交内关联用
    review_status   TEXT NOT NULL DEFAULT 'unreviewed',
    review_version  INTEGER NOT NULL DEFAULT 0,
    seq             INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

-- 匹配矩阵行（六列由 UI 拼装；数据侧存状态 + 两侧说明 + 建议 + 不确定性）
CREATE TABLE IF NOT EXISTS match_row (
    id               TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    requirement_id   TEXT NOT NULL,
    match_status     TEXT NOT NULL,
    --   direct_evidence/transferable/not_in_resume/confirmed_gap/insufficient
    jd_note          TEXT NOT NULL DEFAULT '',
    resume_note      TEXT NOT NULL DEFAULT '',
    user_supplement  TEXT,                       -- 用户补充的个人经历（本人提供，视为本人确认来源）
    reasoning        TEXT NOT NULL DEFAULT '',
    uncertainties    TEXT NOT NULL DEFAULT '[]', -- JSON array[string]，不得为空数组的场景见校验
    prep_advice      TEXT NOT NULL DEFAULT '',
    needs_rejudge    INTEGER NOT NULL DEFAULT 0, -- 补充了经历但未改状态 → 提示"可重新分析"
    review_status    TEXT NOT NULL DEFAULT 'unreviewed',
    review_version   INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- 通用引用表（JD 与简历共用；不复用 evidence 表）
CREATE TABLE IF NOT EXISTS match_quote (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    owner_type  TEXT NOT NULL,
    --   jd_requirement/match_row/interview_focus/resume_item/capability_item/prep_task
    owner_id    TEXT NOT NULL,
    side        TEXT NOT NULL,               -- jd/resume
    ref_type    TEXT NOT NULL DEFAULT 'target',  -- target(job_target)/resume/resume_item/snapshot
    ref_id      TEXT,
    section     TEXT NOT NULL DEFAULT '',
    --   JD: responsibility/requirement/background/unknown
    --   简历: education/experience/project/skill/summary/other
    quote       TEXT NOT NULL,               -- 必须等于 evidence_text[start:end]
    start       INTEGER NOT NULL,
    end         INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);

-- 高价值补充提问（F-08，≤5 条）
CREATE TABLE IF NOT EXISTS clarification (
    id                     TEXT PRIMARY KEY,
    session_id             TEXT NOT NULL,
    question               TEXT NOT NULL,
    why_asked              TEXT NOT NULL DEFAULT '',
    target_requirement_ids TEXT NOT NULL DEFAULT '[]',  -- JSON array[jd_requirement.id]
    answer_text            TEXT,
    answered_at            TEXT,
    seq                    INTEGER NOT NULL DEFAULT 0,
    created_at             TEXT NOT NULL
);

-- 面试考察重点（F-09，6 类 5 要素）
CREATE TABLE IF NOT EXISTS interview_focus (
    id             TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL,
    category       TEXT NOT NULL,
    --   business_scenario/project_contribution/decision_tradeoff/
    --   result_metric/failure_improvement/transfer_ability
    title          TEXT NOT NULL,
    why_asked      TEXT NOT NULL DEFAULT '',
    questions      TEXT NOT NULL DEFAULT '[]',  -- JSON [{"question":str,"followups":[str]}]
    materials      TEXT NOT NULL DEFAULT '[]',  -- JSON array[string] 应准备的真实材料
    uncertainties  TEXT NOT NULL DEFAULT '[]',  -- JSON array[string]，非空（R8）
    review_status  TEXT NOT NULL DEFAULT 'unreviewed',
    review_version INTEGER NOT NULL DEFAULT 0,
    seq            INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);

-- 考察点依据关联（保证"每条都能追溯"）
CREATE TABLE IF NOT EXISTS focus_link (
    id         TEXT PRIMARY KEY,
    focus_id   TEXT NOT NULL,
    side       TEXT NOT NULL,            -- jd/resume/requirement
    ref_type   TEXT NOT NULL DEFAULT '', -- target/resume_item/requirement
    ref_id     TEXT,
    quote      TEXT,
    start      INTEGER,
    end        INTEGER,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- 准备计划任务（F-10）
CREATE TABLE IF NOT EXISTS prep_task (
    id                   TEXT PRIMARY KEY,
    session_id           TEXT NOT NULL,
    priority             INTEGER NOT NULL DEFAULT 5,     -- 1 最高
    purpose              TEXT NOT NULL,
    linked_requirement_ids TEXT NOT NULL DEFAULT '[]',   -- JSON array[jd_requirement.id]
    actions              TEXT NOT NULL DEFAULT '[]',     -- JSON array[string]
    suggested_hours      REAL NOT NULL DEFAULT 0,
    deliverable          TEXT NOT NULL DEFAULT '',
    self_check           TEXT NOT NULL DEFAULT '',
    learnability         TEXT NOT NULL DEFAULT 'needs_expression_practice',
    --   short_term_learnable/needs_expression_practice/real_project_required
    is_practice_project  INTEGER NOT NULL DEFAULT 0,     -- 1 → UI/报告强制标注"练习项目（非真实经历）"
    resources_note       TEXT,                            -- 未检索到可核验资料时的说明文本
    status               TEXT NOT NULL DEFAULT 'planned', -- planned/in_progress/done
    seq                  INTEGER NOT NULL DEFAULT 0,
    review_status        TEXT NOT NULL DEFAULT 'unreviewed',
    review_version       INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

-- ==========================================================================
-- 三、新增表（P1：阶段二 路径一）
-- ==========================================================================

-- 学习资料（F-22；只存可核验来源，检索不到时不写行，改在 prep_task.resources_note 明示）
CREATE TABLE IF NOT EXISTS prep_resource (
    id                   TEXT PRIMARY KEY,
    prep_task_id         TEXT NOT NULL,
    title                TEXT NOT NULL,
    url                  TEXT,
    publisher            TEXT,
    verified_retrievable INTEGER NOT NULL DEFAULT 0,
    note                 TEXT,
    created_at           TEXT NOT NULL
);

-- 行业需求计数快照（R17/R12：公司数/去重岗位数/来源/时间范围必须落库）
CREATE TABLE IF NOT EXISTS demand_snapshot (
    id                TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL,
    industry          TEXT,
    role_direction    TEXT,
    experience_level  TEXT NOT NULL DEFAULT 'unknown',
    company_watchlist TEXT NOT NULL DEFAULT '[]',
    source_run_ids    TEXT NOT NULL DEFAULT '[]',
    filters           TEXT NOT NULL DEFAULT '{}',
    company_count     INTEGER NOT NULL DEFAULT 0,
    unique_job_count  INTEGER NOT NULL DEFAULT 0,
    source_label      TEXT NOT NULL DEFAULT '',
    time_from         TEXT,
    time_to           TEXT,
    dedupe_basis      TEXT NOT NULL DEFAULT '职责规范化后相似度>=0.90 且 >=80 字符（复用 dedupe.MIN_SIMILARITY/MIN_CHARS）',
    counting_caveat   TEXT NOT NULL DEFAULT '计数为本样本内去重计数，不表示行业需求比例',
    created_at        TEXT NOT NULL
);

-- 能力要求映射（F-19；频率只用离散三档，禁止百分比）
CREATE TABLE IF NOT EXISTS capability_item (
    id                       TEXT PRIMARY KEY,
    demand_snapshot_id       TEXT NOT NULL,
    capability_type          TEXT NOT NULL,   -- business_knowledge/skill/project_experience
    name                     TEXT NOT NULL,
    frequency_class          TEXT NOT NULL DEFAULT 'insufficient_info',
    --   common_in_sample/specific_to_few/insufficient_info
    business_reason          TEXT NOT NULL DEFAULT '',
    supporting_finding_ids   TEXT NOT NULL DEFAULT '[]',
    supporting_job_count     INTEGER NOT NULL DEFAULT 0,
    supporting_company_count INTEGER NOT NULL DEFAULT 0,
    seq                      INTEGER NOT NULL DEFAULT 0,
    created_at               TEXT NOT NULL
);

-- ==========================================================================
-- 四、索引
-- ==========================================================================

CREATE INDEX IF NOT EXISTS idx_resume_item_resume   ON resume_item(resume_id);
CREATE INDEX IF NOT EXISTS idx_target_session       ON job_target(session_id);
CREATE INDEX IF NOT EXISTS idx_jss_status           ON jobseeker_session(status);
CREATE INDEX IF NOT EXISTS idx_req_session          ON jd_requirement(session_id);
CREATE INDEX IF NOT EXISTS idx_req_target           ON jd_requirement(job_target_id);
CREATE INDEX IF NOT EXISTS idx_mrow_session         ON match_row(session_id);
CREATE INDEX IF NOT EXISTS idx_mrow_req             ON match_row(requirement_id);
CREATE INDEX IF NOT EXISTS idx_mquote_owner         ON match_quote(owner_type, owner_id);
CREATE INDEX IF NOT EXISTS idx_mquote_session       ON match_quote(session_id);
CREATE INDEX IF NOT EXISTS idx_clar_session         ON clarification(session_id);
CREATE INDEX IF NOT EXISTS idx_focus_session        ON interview_focus(session_id);
CREATE INDEX IF NOT EXISTS idx_flink_focus          ON focus_link(focus_id);
CREATE INDEX IF NOT EXISTS idx_ptask_session        ON prep_task(session_id);
CREATE INDEX IF NOT EXISTS idx_pres_task            ON prep_resource(prep_task_id);
CREATE INDEX IF NOT EXISTS idx_demand_session       ON demand_snapshot(session_id);
CREATE INDEX IF NOT EXISTS idx_cap_demand           ON capability_item(demand_snapshot_id);
CREATE INDEX IF NOT EXISTS idx_report_kind          ON report(kind);

-- ==========================================================================
-- 五、设置项默认值（F-13）
-- ==========================================================================

INSERT OR IGNORE INTO app_setting (key, value) VALUES ('resume_store_dir', '.local/resume');
INSERT OR IGNORE INTO app_setting (key, value) VALUES ('resume_delete_confirm', '1');
INSERT OR IGNORE INTO app_setting (key, value) VALUES ('match_prompt_version', 'match_v1');
