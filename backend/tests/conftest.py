# -*- coding: utf-8 -*-
"""测试套件统一数据库绑定（QA 收口）。

## 为什么需要它

多个测试文件在 **import 期** 执行 ``os.environ["FININTEL_DB"] = ...``，但
``store._local.conn`` 是惰性单例：首个被 import 的模块建好连接后，后续模块的
``apply_migrations()`` 全部落在**同一个库**上。于是形成"env 指向 DB_X、活连接
指向 DB_Y"的**名义隔离**——各文件看似各自隔离，实际共用一库。

后果：一旦某个 helper 中途关闭这条共享连接，``get_conn()`` 会按 env 重开一个
从未迁移的库，表现为成片 ``no such table``（曾一次 46 个 E）。

## 本文件做什么

autouse fixture，在**每个测试边界**强制"env 指向的库"与"活连接的库"一致：

- 测试前：若两者脱钩、或连接已不可用 → 丢弃（只解绑、不 close）后按 env 重建；
  若重建出来的库是**空库** → 补应用全量迁移。
- 测试后：只关闭本 fixture 自己创建的连接；**借来的连接原样归还、绝不关闭**。

## 安全边界（故意不做的事）

1. ``FININTEL_DB`` 未设置 → 什么都不做，绝不创建连接（避免打开真实业务库）。
2. env 指向真实业务库 ``store.DEFAULT_DB_PATH`` → 什么都不做。
3. 库**非空**时绝不补迁移 —— 把迁移控制权留给用例本身
   （例如 ``test_migration_017.py`` 的"016 状态升级"测试需要自己控制版本）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import store  # noqa: E402


def _norm(path) -> str:
    """路径归一（Windows 大小写不敏感 + 绝对路径）。"""
    return os.path.normcase(os.path.abspath(str(path)))


def _conn_file(conn) -> str | None:
    """连接实际打开的数据库文件；不可用返回 None。"""
    if conn is None:
        return None
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except Exception:  # noqa: BLE001 - 关闭/损坏的连接视为不可用
        return None
    if row is None:
        return None
    return _norm(row[2])


def _alive(conn) -> bool:
    if conn is None:
        return False
    try:
        conn.execute("SELECT 1").fetchone()
        return True
    except Exception:  # noqa: BLE001
        return False


def _is_empty(conn) -> bool:
    """库里是否一张表都没有（空库才允许我们补迁移）。"""
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table'").fetchone()
    except Exception:  # noqa: BLE001
        return False
    return int(row[0]) == 0


@pytest.fixture(autouse=True)
def _db_binding():
    """强制 env 与 store._local.conn 指向同一个库。"""
    env = os.environ.get("FININTEL_DB")
    # FININTEL_QA_DB_BINDING=0 可关闭本绑定，用于做有/无 conftest 的 A/B 对照。
    if os.environ.get("FININTEL_QA_DB_BINDING", "1") == "0":
        yield
        return

    # 借来的连接：全程不关闭，退出时原样归还（它是别处 import 期建的）
    borrowed = getattr(store._local, "conn", None)
    own = None

    if env and _norm(Path(env)) != _norm(store.DEFAULT_DB_PATH):
        want = _norm(Path(env))
        if not _alive(borrowed) or _conn_file(borrowed) != want:
            # 脱钩或连接已失效 → 只解绑，不 close 借来的连接
            store._local.conn = None
            own = store.get_conn()
            if _is_empty(own):
                store.apply_migrations()

    try:
        yield
    finally:
        if own is not None:
            try:
                own.close()
            except Exception:  # noqa: BLE001
                pass
        store._local.conn = borrowed if _alive(borrowed) else None


__all__ = ["_db_binding"]
