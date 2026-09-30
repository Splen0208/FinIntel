BEGIN;
-- Each run selects an explicit analysis version; errors remain retryable.
ALTER TABLE run_job ADD COLUMN analysis_id TEXT REFERENCES analysis(id);
ALTER TABLE run_job ADD COLUMN analysis_error TEXT;
ALTER TABLE job_snapshot ADD COLUMN detail_source TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE job_snapshot ADD COLUMN mapping_warning TEXT;

-- Historical receipts are recovered from successful submissions of this run,
-- never from arbitrary analyses of the same job.
UPDATE run_job SET analysis_id = (
    SELECT a.id FROM submission_log l
    JOIN analysis_batch b ON b.token=l.batch_token
    JOIN json_each(l.result_json, '$.outcomes') outcome
    JOIN analysis a ON a.id=json_extract(outcome.value, '$.analysis_id')
    WHERE b.run_id=run_job.run_id AND a.snapshot_id=run_job.snapshot_id
      AND json_extract(outcome.value, '$.ok')=1
    ORDER BY l.created_at DESC, l.rowid DESC LIMIT 1
);
UPDATE run_job SET analysis_error='[{"code":"AI_OUTPUT_MISSING","message":"原任务未保存成功分析，请重试整理"}]'
WHERE snapshot_id IS NOT NULL AND analysis_id IS NULL
  AND run_id IN (SELECT id FROM research_run WHERE origin='simple' AND stage IN ('completed','partial'))
  AND snapshot_id IN (SELECT id FROM job_snapshot WHERE detail_status IN ('complete','partial'));

-- CLI 2.0 did not expose whether description came from a full JD or a job card.
UPDATE job_snapshot SET detail_status='partial', detail_source='cli_detail_unverified',
  mapping_warning='上游未提供全文完整性证明，当前正文可能为职位卡片摘要'
WHERE source_platform='zhipin' AND detail_status IN ('complete','partial');
UPDATE run_job SET detail_outcome='partial'
WHERE detail_outcome IN ('complete','reused')
  AND snapshot_id IN (SELECT id FROM job_snapshot WHERE detail_status='partial');

UPDATE research_run SET failed_count=(
  SELECT COUNT(*) FROM run_job WHERE run_id=research_run.id
    AND (detail_outcome IN ('failed','missing','too_large') OR analysis_error IS NOT NULL)
);
UPDATE research_run SET stage='partial', state='partial',
  user_message='部分岗位分析未完成，已保留结果，可重试整理'
WHERE origin='simple' AND stage='completed' AND failed_count>0;

COMMIT;
