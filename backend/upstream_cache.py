"""只读读取上游 CLI 自己的搜索缓存，用来**如实**告知「这份结果是不是来自本地缓存、多久了」。

为什么要直接读上游缓存库：上游 CLI 把「跳过缓存」交给调用方（``--no-cache``）。本项目过去
**写死** ``--no-cache=True``，等于每次都强制联网；一旦去掉这个写死项，就必须能如实说明
「这一次到底是不是命中了本地缓存、缓存有多旧」——否则用户会以为看到的是刚抓回来的实时结果，
而实际可能是几小时前的旧数据（「沉默地端上旧数据」正是不许犯的错）。

上游缓存位置固定：``<data_dir>/cache/boss_agent.db`` 的 ``search_cache`` 表
（``cache_key`` / ``response`` / ``created_at``）。``cache_key`` 由上游
``CacheStore._make_search_key`` 用 ``sha256(json.dumps(params, sort_keys=True,
ensure_ascii=False))`` 算出；``params`` 的形状在与本模块的 ``search_cache_key`` 里逐一镜像，
并由测试与上游实现逐字对照（见 ``tests/test_cache_refresh.py``）。

铁律：**读不到 / 无表 / 无行 / 已过期 → 一律返回 None**（宁可不说，绝不虚报）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

# 与上游 boss_agent_cli.commands.search 的常量保持一致
SEARCH_CACHE_SCHEMA = 2
SEARCH_CACHE_TTL_SECONDS = 86400  # 24h，与上游 CacheStore._SEARCH_TTL 一致
_CACHE_REL = Path("cache") / "boss_agent.db"


def search_cache_key(
    query: Optional[str],
    city: Optional[str],
    page: int,
    *,
    url: Optional[str] = None,
    raw_params: Optional[dict[str, Any]] = None,
) -> str:
    """复算上游搜索缓存的键（与 ``CacheStore._make_search_key`` 逐字一致）。

    本项目对上游 search 只传 ``query`` / ``--city`` / ``--page``（详情见 ``source_boss.search``），
    因此其余筛选维度固定为 ``None``、``raw_params`` 固定为 ``{}``——与上游构造的
    ``search_params`` 形状完全相同。
    """
    params = {
        "cache_schema": SEARCH_CACHE_SCHEMA,
        "query": query or "",
        "city": city,
        "salary": None,
        "experience": None,
        "education": None,
        "industry": None,
        "scale": None,
        "stage": None,
        "job_type": None,
        "url": url,
        "raw_params": raw_params or {},
        "page": int(page),
    }
    raw = json.dumps(params, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def cache_db_path(data_dir: object) -> Path:
    """上游搜索缓存库的绝对路径（不检查存在性）。"""
    return Path(str(data_dir)) / _CACHE_REL


def cache_age_seconds(
    data_dir: object,
    query: Optional[str],
    city: Optional[str],
    page: int,
    *,
    ttl_seconds: int = SEARCH_CACHE_TTL_SECONDS,
    now: Optional[float] = None,
) -> Optional[int]:
    """本次搜索**将会**命中的本地缓存的年龄（整秒）；不是命中则返回 ``None``。

    上游的缓存门禁是 ``not welfare_conditions and not no_cache and not with_score``；本项目
    从不使用 ``--welfare`` / ``--with-score``，所以在「未传 ``--no-cache``」时，命中与否**恰好**
    等于「库里有一行未过期的该键」。过期的行上游会当作未命中（并顺带删除），这里同样返回
    ``None``。任何读取异常（库不存在、只读打开失败、表缺失）都返回 ``None``——宁可不说。
    """
    db = cache_db_path(data_dir)
    if not db.is_file():
        return None
    key = search_cache_key(query, city, page)
    try:
        # ``mode=ro`` 只读打开：绝不写入、绝不建表，不与正在写缓存的上游进程抢写锁。
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        try:
            row = conn.execute(
                "SELECT created_at FROM search_cache WHERE cache_key=?", (key,)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    created = float(row[0])
    age = (now if now is not None else time.time()) - created
    if age < 0 or age > ttl_seconds:
        return None
    return int(age)
