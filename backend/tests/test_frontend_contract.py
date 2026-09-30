# -*- coding: utf-8 -*-
"""前端顶栏「账号 × 通道」两维文案的**执行级**契约（无 JS 构建步骤，故用 Node 跑真代码）。

真机缺陷（用户指令 §0.3）：账号已验证、但当前搜索通道未就绪时，``status_view()`` 会把
``user_stage`` 打扮成 ``env_not_ready``。顶栏旧实现直接读 ``user_stage`` → 显示
「搜索服务未就绪」，与后端如实说的「账号登录已验证，当前搜索连接尚未就绪。」两处两说。

修法：顶栏按**未被打扮的账号事实** ``account_state`` × ``channel.state`` 两维判定，
绿点只认 ``channel_ready``（= 通道确凿就绪）。本用例把**真实发布**的 ``frontend/simple.js``
装进 Node 的 ``vm`` 最小沙箱，直接调用真正的 ``sfSetBoss()`` / ``sfPillText()`` /
``sfChannelReady()``，覆盖真机形状——测的是卖出去的代码，不是它的复制品。

另有 ``frontend_topbar_contract.mjs``（同一目录）可直接手工运行：
``node backend/tests/frontend_topbar_contract.mjs frontend/simple.js``
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parents[1]
SIMPLE_JS = PROJECT_ROOT / "frontend" / "simple.js"
APP_JS = PROJECT_ROOT / "frontend" / "app.js"
HARNESS = TESTS_DIR / "frontend_topbar_contract.mjs"
SETTINGS_HARNESS = TESTS_DIR / "frontend_app_settings_contract.mjs"
RISK_HARNESS = TESTS_DIR / "frontend_risk_action_contract.mjs"


def _node() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("未找到 node，跳过前端执行级契约（前端无构建步骤，需 Node 跑真代码）")
    return node


def _run_harness(node: str, harness: Path, script: Path) -> dict:
    completed = subprocess.run(
        [node, str(harness), str(script)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    stdout = (completed.stdout or "").strip()
    assert stdout, f"契约脚本无输出（stderr={completed.stderr!r}）"
    return json.loads(stdout.splitlines()[-1])


def test_topbar_copy_is_two_dimensional_account_x_channel() -> None:
    """顶栏文案与绿点按「账号事实 × 通道状态」判定，绝不退回「搜索服务未就绪」。"""
    assert SIMPLE_JS.is_file(), f"前端入口缺失：{SIMPLE_JS}"
    assert HARNESS.is_file(), f"契约脚本缺失：{HARNESS}"
    node = _node()

    completed = subprocess.run(
        [node, str(HARNESS), str(SIMPLE_JS)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    stdout = (completed.stdout or "").strip()
    assert stdout, f"契约脚本无输出（stderr={completed.stderr!r}）"
    payload = json.loads(stdout.splitlines()[-1])
    cases = {case["name"]: case for case in payload["cases"]}

    # 真机形状：账号已验证 + 通道未就绪 —— 顶栏必须说「账号侧 + 通道未就绪」，
    # 而不是把账号说成「未登录」，也不是把通道说成「搜索服务未就绪」。
    not_ready = cases["connected+not_ready"]
    assert not_ready["account_state"] == "connected"
    assert not_ready["user_stage"] == "env_not_ready"          # 前提：账号事实已被通道打扮
    assert not_ready["pill"] == "账号登录已验证，当前搜索连接尚未就绪。"
    assert not_ready["pill"] != "搜索服务未就绪"
    assert not_ready["pill"] != "BOSS：未登录"
    assert not_ready["ready"] is False                          # 通道未就绪 → 绝不画绿点

    # 通道状态尚未确认（unknown）：中性说法，不宣称已就绪、也不画绿点
    unknown = cases["connected+unknown"]
    assert unknown["pill"] == "BOSS 已连接，正在确认搜索通道"
    assert unknown["ready"] is False                            # unknown ≠ 就绪（不放行探测）
    assert unknown["pill"] != "搜索服务未就绪"

    # 通道确凿就绪：才是唯一画绿点、说「已连接」的情形
    ready = cases["connected+ready"]
    assert ready["pill"] == "BOSS 已连接"
    assert ready["ready"] is True
    # 老响应没有显式 channel_ready 字段时，channel.state === 'ready' 仍须判绿（向后兼容）
    assert cases["connected+ready_without_explicit_field"]["ready"] is True

    # 真的没登录：才是「未登录」，且不画绿点
    disconnected = cases["disconnected"]
    assert disconnected["pill"] == "BOSS：未登录"
    assert disconnected["ready"] is False

    assert payload["ok"] is True, payload
    assert completed.returncode == 0, completed.stderr


def test_frontend_green_dot_and_pill_share_one_account_fact() -> None:
    """源码级钉子：文案与绿点必须共用同一个账号事实 ``sfAccountConnected()``。

    上面那条执行级用例已能抓住行为回归；这一条额外钉住**实现同源**——避免有人把
    文案改回 ``SF_BOSS.state`` 而绿点仍走账号事实（或反之），造成「同一个点、两套口径」。
    """
    source = SIMPLE_JS.read_text(encoding="utf-8")
    assert "function sfAccountConnected" in source
    # 账号事实优先取后端 account_state（未被打扮），仅在老响应缺字段时回退
    assert "SF_BOSS.account_state" in source
    # 绿点唯一依据：账号已验证 + 通道确凿就绪
    assert "function sfChannelReady" in source
    assert "SF_BOSS.channel_ready === true" in source
    # 顶栏文案与登录块都走同一个判定，不各写一套
    assert source.count("sfChannelReady()") >= 2


def test_frontend_channel_copy_matches_backend_user_view() -> None:
    """前端通道/过期文案必须与后端 ``user_view`` **逐字一致**（同一句，不许各写一套）。

    后端是文案的唯一来源：前端只是把它渲染出来。两处各写一份，迟早会「同一状态两处两说」。
    """
    import sys

    sys.path.insert(0, str(TESTS_DIR.parent))   # backend/
    import user_view  # noqa: E402

    source = SIMPLE_JS.read_text(encoding="utf-8")
    for copy in (user_view.LOGIN_MESSAGES["connected"],
                 user_view.ACCOUNT_OK_CHANNEL_NOT_READY,
                 user_view.ACCOUNT_OK_CHANNEL_CONFIRMING,
                 # 环境风控的同一句核心话：顶栏/任务页/结果页共用 ENVIRONMENT_BLOCKED_COPY，
                 # 前端（simple.js）必须逐字持有同一句，禁止各写一份
                 user_view.ENVIRONMENT_BLOCKED_COPY,
                 user_view.STALE_LOCATOR_MESSAGE,
                 user_view.STALE_LOCATOR_RESUMABLE_MESSAGE):
        assert copy in source, f"前端缺少后端文案（后端是唯一来源）：{copy}"


def test_settings_page_can_represent_stored_cookie_and_preserves_untouched_values() -> None:
    """H1：设置页下拉必须能**如实表示** stored-cookie，保存不得把未改动的通道值静默降级。

    真机缺陷：下拉只有 auto/own 两项，``stored-cookie`` 落进「不是 existing-browser 就选
    auto」的兜底 → 页面把专用浏览器通道显示成「自动」，保存时写回 auto 并触发
    ``channel_state.invalidate_all()``（正在就绪的通道被无谓清空）。
    """
    assert APP_JS.is_file(), f"前端入口缺失：{APP_JS}"
    assert SETTINGS_HARNESS.is_file(), f"契约脚本缺失：{SETTINGS_HARNESS}"
    node = _node()

    payload = _run_harness(node, SETTINGS_HARNESS, APP_JS)
    assert payload["ok"] is True, payload
    assert payload["failed"] == [], payload["failed"]

    # 未改动保存：一个通道键都不许写（写了就会 invalidate_all，等于把通道就绪清空）
    assert "boss_browser_source" not in payload["detail"]["postsUnchanged"]
    assert "boss_cdp_url" not in payload["detail"]["postsUnchanged"]
    # 显式改动才写
    assert "boss_browser_source" in payload["detail"]["postsChanged"]
    # 无法表示的原值：保留，不覆盖
    assert "boss_browser_source" not in payload["detail"]["postsUnknown"]


def test_environment_blocked_action_never_triggers_relogin() -> None:
    """H2（+ 二轮 #2）：环境被拦截的动作只能让**专用浏览器**打开官方页面，**绝不** POST
    ``/api/auth/login``，也**绝不**在日常浏览器里 ``window.open``。

    真机缺陷：文案写「查看 BOSS 官方页面」，onclick 却是 ``sfStartLogin()``（重新登录），
    与文案矛盾，也违背上游「不刷新 Token、不重新登录、不自动重试」的要求。
    二轮修正：纯前端 ``window.open`` 会把页面开在日常浏览器里——被标记的是**专用 profile 那个
    会话**，只有经专用端点 ``/api/channel/open-official``（后端只发 CDP 浏览器级命令）让**专用
    浏览器**打开才有意义。故断言：恰好命中一次专用端点、零业务请求、不在日常浏览器开窗口。
    """
    assert SIMPLE_JS.is_file(), f"前端入口缺失：{SIMPLE_JS}"
    assert RISK_HARNESS.is_file(), f"契约脚本缺失：{RISK_HARNESS}"
    node = _node()

    payload = _run_harness(node, RISK_HARNESS, SIMPLE_JS)
    assert payload["ok"] is True, payload
    assert payload["failed"] == [], payload["failed"]
    detail = payload["detail"]
    # 恰好一次、命中的是**专用端点**（不是日常浏览器的 window.open）
    assert detail["api_calls_after_open"] == ["/api/channel/open-official"]
    assert detail["official_endpoint"] == "/api/channel/open-official"
    # 零业务请求：不是 /api/auth/login，也不是 search/detail
    assert all(not __import__("re").search(r"/api/auth/login|/search|/detail", c)
               for c in detail["api_calls_after_open"])
    # 不在日常浏览器里开新窗口
    assert detail["opened"] == []
