# -*- coding: utf-8 -*-
"""社交结果页 T16/T17 前端行为的执行级契约（Node 跑真代码，见同目录 .mjs）。

覆盖《指令》§5.G 的前端部分：失败态与空态文案不同、终态停止轮询、切换任务丢弃
过期响应、评论覆盖声明、按 task_type 渲染历史、证据上下文/快照、AI 计数口径。

手工运行：
``node backend/tests/frontend_social_contract.mjs frontend``
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from test_frontend_contract import _node

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parents[1]
FRONTEND_DIR = PROJECT_ROOT / "frontend"
HARNESS = TESTS_DIR / "frontend_social_contract.mjs"


def test_social_result_page_contract() -> None:
    """社交结果页：失败/空态可区分、终态停轮询、切换任务不串台、评论覆盖、历史按类型。"""
    assert (FRONTEND_DIR / "social.js").is_file(), f"前端入口缺失：{FRONTEND_DIR / 'social.js'}"
    assert HARNESS.is_file(), f"契约脚本缺失：{HARNESS}"
    node = _node()

    completed = subprocess.run(
        [node, str(HARNESS), str(FRONTEND_DIR)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    stdout = (completed.stdout or "").strip()
    assert stdout, f"契约脚本无输出（stderr={completed.stderr!r}）"
    payload = json.loads(stdout.splitlines()[-1])

    assert payload["failed"] == [], payload["failed"]
    assert payload["ok"] is True, payload
    assert completed.returncode == 0, completed.stderr
    # 终态登记 0 个轮询定时器；非终态至少 1 个（证明“终态停止轮询”不是靠不轮询实现）
    assert payload["detail"]["intervals_terminal"] == 0, payload["detail"]
    assert payload["detail"]["intervals_running"] >= 1, payload["detail"]
