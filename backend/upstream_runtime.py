"""finintel upstream_runtime：运行实例自证（写入并读取 .local/run/server.json）。

目的（整改指令阶段 A.2）：解决"文档写 8765、实际跑 8766"、"改了代码页面没变"这类误判。
做法：进程启动时把自己是谁（解释器绝对路径、入口、cwd、pid、端口、启动时间、代码 mtime）
写进一个固定位置的文件；`/api/health` 对外暴露该内容。

`code_mtime` 取 **backend/ 下所有已加载后端模块 .py 的最大 mtime**（不是只看入口
app.py）：只看入口时，改了 pipeline.py 这类子模块而没动 app.py，自证文件仍显示"代码未变"，
会把"实例跑的是旧代码"误判成"代码没生效"，曾因此白排查一轮。

巡检顺序固定为：**先读 server.json 确认实例，再打开页面**。

不引入任何网络或平台访问，纯本地文件读写。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent
RUN_DIR = PROJECT_DIR / ".local" / "run"
SERVER_JSON = RUN_DIR / "server.json"


def _detect_port() -> Optional[int]:
    """从常见来源推断监听端口：环境变量 > uvicorn 命令行参数。"""
    env = os.environ.get("FININTEL_PORT")
    if env and env.isdigit():
        return int(env)
    argv = list(sys.argv)
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            try:
                return int(argv[i + 1])
            except ValueError:
                return None
        if a.startswith("--port="):
            try:
                return int(a.split("=", 1)[1])
            except ValueError:
                return None
    return None


# 扫描时排除：缓存目录、测试目录、临时/调试脚本（下划线前缀）
_CODE_SCAN_EXCLUDE_DIRS = {"__pycache__", "tests", ".pytest_cache", "migrations"}


def _code_mtime() -> tuple[Optional[int], Optional[str]]:
    """backend/ 下所有后端模块 .py 的最大 mtime（不递归、排除缓存与测试）。

    返回 ``(mtime, 最新文件名)``。只看入口文件会漏掉"只改子模块"的情况，故取最大值。
    """
    newest: Optional[int] = None
    newest_name: Optional[str] = None
    try:
        for p in sorted(BACKEND_DIR.glob("*.py")):
            if p.name.startswith("_"):
                continue
            try:
                m = int(p.stat().st_mtime)
            except OSError:
                continue
            if newest is None or m > newest:
                newest, newest_name = m, p.name
    except OSError:
        return None, None
    return newest, newest_name


def write_server_json(entry: str = "backend/app.py") -> dict[str, Any]:
    """写入运行实例自证文件，返回写入内容。

    **只有能解析出监听端口的真实服务进程才写**：测试或脚本 ``import app`` 同样会走到
    这里，若无条件写入，就会用「测试进程 pid + port=None」覆盖掉正在运行实例的记录，
    使 ``/api/health`` 误报 ``stale``（多实例假象）。解析不到端口时保留既有记录。
    """
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    port = _detect_port()
    payload: dict[str, Any] = {
        "python": sys.executable,
        "entry": entry,
        "cwd": os.getcwd(),
        "werkzeug_backend_dir": str(BACKEND_DIR),
        "pid": os.getpid(),
        "port": port,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    # 全部后端模块（backend/*.py）的最大 mtime —— 代表"该实例加载的是哪一版代码"
    mt, mt_file = _code_mtime()
    payload["code_mtime"] = mt
    payload["code_mtime_file"] = mt_file
    payload["code_mtime_iso"] = (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mt)) if mt else None)
    if port is None:
        return payload
    try:
        SERVER_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return payload


def read_server_json() -> Optional[dict[str, Any]]:
    """读取当前/最近一次运行实例自证；不存在或损坏返回 None。"""
    try:
        return json.loads(SERVER_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def runtime_block(current_pid: Optional[int] = None) -> dict[str, Any]:
    """给 /api/health 使用的 runtime 块；标注该文件是否属于当前进程。"""
    if current_pid is None:
        current_pid = os.getpid()
    data = read_server_json()
    if not data:
        return {"status": "unknown",
                "detail": "尚无运行实例自证文件（服务未通过本项目入口启动？）",
                "server_json": str(SERVER_JSON)}
    same = (current_pid is None) or (data.get("pid") == current_pid)
    return {
        "status": "ok" if same else "stale",
        "detail": ("本进程自证" if same else
                   "server.json 记录的 pid 与当前进程不一致，可能存在多个实例"),
        "server_json": str(SERVER_JSON),
        "python": data.get("python"),
        "entry": data.get("entry"),
        "cwd": data.get("cwd"),
        "pid": data.get("pid"),
        "port": data.get("port"),
        "started_at": data.get("started_at"),
        "code_mtime": data.get("code_mtime"),
        "code_mtime_iso": data.get("code_mtime_iso"),
        # 最新那个 .py 的文件名，便于定位"到底哪个模块比实例新"
        "code_mtime_file": data.get("code_mtime_file"),
    }
