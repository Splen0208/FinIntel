-- finintel 007_login_exit_code.sql
-- 登录任务可诊断性补列：记录上游登录进程的真实退出码。
-- 场景：登录子进程被环境打断/提前退出时，旧逻辑只剩一个模糊的 timeout，
-- 无法区分「用户没登录」和「进程异常死亡」；落库退出码后诊断页可直接区分。
-- 只加列，不改写既有列与数据，向后兼容；幂等由 schema_migrations 版本表保证。
PRAGMA foreign_keys = ON;

-- 1) login_task：上游登录进程退出码（成功=0；负数在 Windows 上表示进程被异常终止）
ALTER TABLE login_task ADD COLUMN exit_code INTEGER;
