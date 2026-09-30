-- finintel 008_browser_channel.sql
-- 采集通道设置：允许把「搜索/详情」改走用户自己已登录的浏览器（CDP）。
-- 场景：上游 headless 回退路径会注入本地 cookie 后再打开首页，该 context 在本机稳定打不开
-- 首页（Page.goto 超时）。绕开办法是显式指定 --browser-source existing-browser：
-- 不读本地凭据、不注入 cookie，直接用用户自己浏览器里已登录的会话采集。
-- 只写 app_setting，不改任何既有表与数据；默认两条都留空 = 不启用，老用户行为完全不变。
PRAGMA foreign_keys = ON;

-- 1) 采集浏览器来源：留空 = 自动（历史行为）；existing-browser = 用用户自己已登录的浏览器
INSERT OR IGNORE INTO app_setting (key, value) VALUES ('boss_browser_source', '');

-- 2) 调试地址：留空 = 让上游自动探测本机调试端口；填写时只允许 localhost / 127.0.0.1
INSERT OR IGNORE INTO app_setting (key, value) VALUES ('boss_cdp_url', '');
