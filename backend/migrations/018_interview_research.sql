-- Explicitly selected research evidence for a personal interview session.
ALTER TABLE jobseeker_session ADD COLUMN origin_run_id TEXT;
ALTER TABLE jobseeker_session ADD COLUMN research_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobseeker_session ADD COLUMN prep_research_version INTEGER NOT NULL DEFAULT 0;
CREATE TABLE interview_material (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES jobseeker_session(id) ON DELETE CASCADE,
    platform TEXT NOT NULL CHECK(platform IN ('x','xiaohongshu')),
    source_snapshot_id TEXT REFERENCES social_snapshot(id),
    comment_snapshot_id TEXT REFERENCES social_comment_snapshot(id),
    source_run_id TEXT NOT NULL REFERENCES research_run(id),
    title TEXT NOT NULL,
    source_url TEXT,
    quote TEXT NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    CHECK ((source_snapshot_id IS NULL) != (comment_snapshot_id IS NULL)),
    CHECK (start >= 0 AND end > start)
);
CREATE UNIQUE INDEX interview_material_identity ON interview_material
    (session_id,COALESCE(source_snapshot_id,''),COALESCE(comment_snapshot_id,''),start,end);
CREATE TABLE interview_research_question (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES jobseeker_session(id) ON DELETE CASCADE,
    material_id TEXT NOT NULL REFERENCES interview_material(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    why_relevant TEXT NOT NULL,
    quote TEXT NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    limitations TEXT NOT NULL,
    research_version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
