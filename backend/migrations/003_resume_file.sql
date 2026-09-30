-- finintel 003_resume_file.sql
-- 阶段 1：文件真实读取强化（对照《简历解析实施指令》§5/§6）
-- 向后兼容：只加列 + 建新表；幂等由 schema_migrations 保证
PRAGMA foreign_keys = ON;

-- ==========================================================================
-- 一、resume 表加列（文件元数据 + 归属 + 提取器版本）
-- ==========================================================================

-- 单机版先固定 owner='local'；多用户接入现有认证后按用户 ID 隔离（R-18）
ALTER TABLE resume ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE resume ADD COLUMN file_hash TEXT;              -- sha256(原始文件字节)，用于识别重复上传
ALTER TABLE resume ADD COLUMN mime TEXT;                  -- application/pdf / docx 等
ALTER TABLE resume ADD COLUMN size INTEGER;               -- 原始文件字节数
ALTER TABLE resume ADD COLUMN extractor_version TEXT;      -- 提取器版本，如 'pdfplumber-0.11.10' / 'python-docx'
ALTER TABLE resume ADD COLUMN needs_ocr INTEGER NOT NULL DEFAULT 0;  -- 1 = 无文本层，需 OCR/粘贴文本

-- ==========================================================================
-- 二、块级原文表（不可变版本；每块有 block_id、page_no、全局偏移）
-- ==========================================================================

CREATE TABLE IF NOT EXISTS resume_text (
    id               TEXT PRIMARY KEY,
    resume_id        TEXT NOT NULL REFERENCES resume(id) ON DELETE CASCADE,
    text_version     TEXT NOT NULL DEFAULT 'v1',   -- 文本版本；同一 document 可多版本
    block_id         TEXT NOT NULL,                -- 稳定块标识，如 'p1' / 'd3'
    page_no          INTEGER,                      -- PDF 页号；DOCX 无稳定页码时为 NULL（不编造）
    start            INTEGER NOT NULL,             -- 相对 resume.evidence_text 的全局 code point 偏移（半开）
    end              INTEGER NOT NULL,
    text             TEXT NOT NULL,                -- 该块原文（未脱敏前即为证据文本切片）
    completeness     TEXT NOT NULL DEFAULT 'complete',  -- complete/partial/missing
    warnings         TEXT NOT NULL DEFAULT '[]',   -- JSON array[string]
    seq              INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rtext_resume ON resume_text(resume_id, text_version, seq);
CREATE INDEX IF NOT EXISTS idx_resume_owner ON resume(owner_id);
CREATE INDEX IF NOT EXISTS idx_resume_hash ON resume(file_hash);

-- ==========================================================================
-- 三、异步任务表（阶段 2/3 使用；此处先建表）
-- ==========================================================================

CREATE TABLE IF NOT EXISTS analysis_task (
    id               TEXT PRIMARY KEY,
    type             TEXT NOT NULL,                -- extract_facts / match / prep
    ref_id           TEXT NOT NULL,                -- resume_id 或 jobseeker_session.id
    input_versions   TEXT NOT NULL DEFAULT '{}',   -- JSON，固定输入版本（text_version/revision 等）
    idempotency_key  TEXT,                         -- 同一输入版本复用任务
    status           TEXT NOT NULL DEFAULT 'queued', -- queued/running/completed/failed/cancelled/needs_input
    stage            TEXT NOT NULL DEFAULT '',     -- 阶段：读取文件/整理经历/核对证据/分析岗位
    model            TEXT,                         -- 实际模型名
    provider         TEXT,                         -- 实际 provider
    prompt_version   TEXT,
    schema_version   TEXT,
    error_code       TEXT,
    error_message    TEXT,
    result_ref       TEXT,                         -- 指向结果表/报告 id
    started_at       TEXT,
    ended_at         TEXT,
    elapsed_ms       INTEGER,
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_analysis_task_ref ON analysis_task(ref_id, type);
CREATE INDEX IF NOT EXISTS idx_analysis_task_status ON analysis_task(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_analysis_task_idem ON analysis_task(type, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- ==========================================================================
-- 四、设置项默认值（LLM 配置示例不带密钥）
-- ==========================================================================

INSERT OR IGNORE INTO app_setting (key, value) VALUES ('llm_provider', 'openai_compat');
INSERT OR IGNORE INTO app_setting (key, value) VALUES ('llm_api_base', '');
INSERT OR IGNORE INTO app_setting (key, value) VALUES ('llm_api_key', '');
INSERT OR IGNORE INTO app_setting (key, value) VALUES ('llm_model', '');
INSERT OR IGNORE INTO app_setting (key, value) VALUES ('llm_timeout_seconds', '120');
INSERT OR IGNORE INTO app_setting (key, value) VALUES ('demo_mode', '0');   -- 默认关闭演示模式
