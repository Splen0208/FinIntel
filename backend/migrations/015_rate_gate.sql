-- 二轮·C：应用层**持久化**频率闸门（跨进程、跨重启）。
--
-- 背景：上游 CLI 的节流是进程内的；本项目每次搜索都是新起一个 CLI 进程，进程内节流失效。
-- 真正能约束「同一账号短时间反复访问平台」的状态必须落在应用层、且重启后仍在。
--
-- 单行表（id=1）：
--   lease_run_id / lease_until   账号级串行：同一时刻只允许一个采集任务持有租约（带 TTL）
--   last_list_at                 上次列表请求时刻 → 最小间隔 ≥60s
--   window_start / window_count  滑动窗口 → 任意 10 分钟内列表请求 ≤20 次
--   cooldown_until / cooldown_reason  ENVIRONMENT_RISK / ACCOUNT_RISK 后的硬冷却（30 分钟）
--   manual_required / manual_credits  冷却结束后：自动路径一律停，只允许一次人工请求
CREATE TABLE IF NOT EXISTS rate_gate (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    window_start     REAL,
    window_count     INTEGER NOT NULL DEFAULT 0,
    last_list_at     REAL,
    cooldown_until   REAL,
    cooldown_reason  TEXT,
    manual_required  INTEGER NOT NULL DEFAULT 0,
    manual_credits   INTEGER NOT NULL DEFAULT 0,
    lease_run_id     TEXT,
    lease_until      REAL,
    updated_at       TEXT
);
