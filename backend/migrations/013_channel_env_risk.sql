-- finintel 013_channel_env_risk.sql
-- 修 012 的**同一件事实**在另一处仍被塌缩：012 只修正了 research_run.stop_reason，
-- 没同步修正 channel_state.last_error_code。于是同一次「访问环境风控」在
--   任务/结果页：stop_reason='ENVIRONMENT_RISK'  → 「环境被拦截」
--   顶栏：channel_state.last_error_code='ACCOUNT_RISK' + state='not_ready' → 「连接尚未就绪」
-- 讲成两个互相矛盾的故事（真机已确认：run_f05ef3d9e1 与 fingerprint=e184b8dfb334）。
--
-- 用与 012 **同一份证据、同一判据**（上游 message 里的 `访问环境风控` 签名）把被旧映射
-- 塌缩成 ACCOUNT_RISK 的**派生态**通道错误码还原为 ENVIRONMENT_RISK。
-- 判据落在通道行自己的 last_error_summary 上（与 last_error_code 同源、同行，无跨行歧义）；
-- **不修改** upstream_call 原始调用记录（与 012 一致）。
-- 幂等：再跑一次不会命中（值已不是 ACCOUNT_RISK）。
UPDATE channel_state SET last_error_code='ENVIRONMENT_RISK'
WHERE last_error_code='ACCOUNT_RISK'
  AND last_error_summary LIKE '%访问环境风控%';
