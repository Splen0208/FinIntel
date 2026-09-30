-- finintel 004_resume_facts.sql
-- 阶段 2：真实模型事实提取 + 用户核对（对照《简历解析实施指令》§6/§8.1/§8.2）
PRAGMA foreign_keys = ON;

-- 简历事实（由真实模型提取，带来源引用；确认状态与"解析成功/结构校验"分离）
CREATE TABLE IF NOT EXISTS resume_claim (
    id                   TEXT PRIMARY KEY,
    resume_id            TEXT NOT NULL REFERENCES resume(id) ON DELETE CASCADE,
    claim_type           TEXT NOT NULL,           -- education/experience/project/skill/result/other
    statement            TEXT NOT NULL,           -- 事实陈述（不润色、不补造）
    source_refs          TEXT NOT NULL DEFAULT '[]',
    -- JSON [{"block_id":str,"start":int,"end":int,"quote":str}]；start/end 相对 resume.evidence_text
    confirmation_status  TEXT NOT NULL DEFAULT 'unconfirmed',
    --   unconfirmed(待核对) / confirmed(本人已确认) / needs_input(信息缺失) / omitted(已省略)
    confirmed_by         TEXT,                    -- 本人确认时的 actor（本地为 'local'）
    confirmed_at         TEXT,
    revision             INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claim_resume ON resume_claim(resume_id);

-- 用户补充答复（与模型生成内容分开；来源是本人，不伪装成上传文件原句）
CREATE TABLE IF NOT EXISTS user_supplement (
    id          TEXT PRIMARY KEY,
    resume_id   TEXT NOT NULL REFERENCES resume(id) ON DELETE CASCADE,
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    revision    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_supplement_resume ON user_supplement(resume_id);

-- 事实提取任务元数据（复用 analysis_task 表，type='extract_facts'）
