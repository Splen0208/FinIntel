-- Restore the distinction lost by the old ENVIRONMENT_RISK -> ACCOUNT_RISK mapping.
-- Keep original call receipts unchanged; only correct the derived paused-task reason.
UPDATE research_run SET stop_reason='ENVIRONMENT_RISK'
WHERE state='risk_stopped' AND stop_reason='ACCOUNT_RISK'
  AND (SELECT result_summary FROM upstream_call
       WHERE upstream_call.run_id=research_run.id
       ORDER BY started_at DESC, rowid DESC LIMIT 1) LIKE '%访问环境风控%';
