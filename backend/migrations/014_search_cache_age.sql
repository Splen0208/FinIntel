-- 二轮·B：如实标注「本次结果来自上游本地缓存」及其年龄。
--
-- 上游 CLI 自带搜索缓存（<data_dir>/cache/boss_agent.db）。本项目过去把 --no-cache 写死，
-- 等于永远联网；去掉写死后，需要在结果页如实说明「这一次是不是命中缓存、缓存多久了」，
-- 否则用户会把旧数据当成刚抓的实时结果。
--
-- 在单次 search 观测上落一条 cache_age_seconds（命中缓存时的年龄；联网抓取为 NULL）：
-- 该列只描述**这一次取数**的事实，不引入新表，也不改变既有列语义。
ALTER TABLE search_observation ADD COLUMN cache_age_seconds INTEGER;
