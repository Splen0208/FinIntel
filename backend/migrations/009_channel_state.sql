-- finintel 009_channel_state.sql
-- 采集通道状态：把「账号曾验证成功」与「当前采集通道可用」两个事实分开存。
-- 依据 docs/channel-consistency-design.md §2.3 / §2.7。
--
-- 背景（真机已确认，不要推翻）：
--   * `status --live` 只读本地凭据 + httpx，验证不了浏览器通道，所以顶栏原来那个
--     「已连接」只是账号事实；搜索走的是另一条通道（browser_source / cdp_url）。
--   * 换通道后旧通道的成功不能给新通道放行，所以就绪状态以**通道指纹**为主键。
--
-- 只新增表与列，不改任何既有数据；默认 state='unknown'（初始值，不是放行）。
PRAGMA foreign_keys = ON;

-- 1) 通道就绪状态表：主键 = 通道指纹，任一维度变化 = 新行 state='unknown'
CREATE TABLE IF NOT EXISTS channel_state (
  fingerprint        TEXT PRIMARY KEY,
  browser_source     TEXT NOT NULL DEFAULT '',        -- ''/'auto'=自动；existing-browser；stored-cookie
  cdp_url            TEXT NOT NULL DEFAULT '',        -- '' = 未显式指定（上游自动探测）
  data_dir           TEXT NOT NULL DEFAULT '',        -- 绝对路径，不是凭据
  cli_path           TEXT NOT NULL DEFAULT '',        -- 绝对路径，不是凭据
  config_version     INTEGER NOT NULL DEFAULT 1,      -- 通道配置版本号，变更即整体失效
  state              TEXT NOT NULL DEFAULT 'unknown',  -- unknown|ready|not_ready
  checked_at         TEXT,
  check_source       TEXT,                            -- doctor|collect_ok|collect_failed
  last_collect_ok_at TEXT,
  last_collect_cards INTEGER NOT NULL DEFAULT 0,
  last_error_code    TEXT,
  last_error_summary TEXT,                            -- 必须过 redact()，绝不落任何凭据
  updated_at         TEXT
);

-- 2) 任务真实到达的阶段（只增不减）：失败/暂停时用它判断哪些阶段从未执行
ALTER TABLE research_run ADD COLUMN stage_reached TEXT;

-- 3) 通道配置版本号：变更通道相关设置时递增，旧指纹行的 ready 一律不再生效
INSERT OR IGNORE INTO app_setting (key, value) VALUES ('channel_config_version', '1');
