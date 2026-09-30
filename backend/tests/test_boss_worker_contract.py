# -*- coding: utf-8 -*-
"""BOSS 详情通道复核（任务书 F1–F7）的**离线**契约测试。

离线口径：
- 数据库指向临时目录（``FININTEL_DB``），**不碰真实业务库**；
- worker 依赖的 SDK 用**假模块**注入（语义与 boss-agent-cli 2.0.0 源码核对过），
  另有一组用例用本机**真实** SDK 的异常类型做结构化判定（装了才跑）；
- 子进程调用一律替换；唯一真实执行的命令是本地 ``import`` 探测（不发平台请求）；
- 哨兵值（``SENTINEL_*``）代替真实 securityId / lid，绝不写入真实凭据。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from typing import Any

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="finintel_boss_worker_"))
os.environ["FININTEL_DB"] = str(_TMP_DIR / "boss_worker.sqlite3")
os.environ["FININTEL_PORT"] = ""

import boss_browser_worker as worker  # noqa: E402
import source_boss  # noqa: E402
import store  # noqa: E402
import upstream_config  # noqa: E402
from upstream_config import RawInvocation, channel_options_from_snapshot  # noqa: E402
from upstream_envelope import UpstreamError  # noqa: E402

store.apply_migrations()

FROZEN_CHANNEL = {
    "browser_source": "existing-browser",
    "cdp_url": "http://127.0.0.1:9222",
    "data_dir": "C:/tmp/upstream",
    "cli_path": "C:/tmp/venv/Scripts/boss.exe",
}
CHANNEL_OPTIONS = channel_options_from_snapshot(FROZEN_CHANNEL)

# 哨兵：既不像 token（不是 32 位十六进制、不带 token 键），又能一眼看出有没有漏遮蔽
SENTINEL_SID = "SENTINEL-security-id-1a2b"
SENTINEL_LID = "SENTINEL-lid-9z9y"


def _success_card(description: str = "负责风控策略的设计与落地。",
                  *, job_id: str | None = "JOB-1") -> dict:
    card = {
        "jobName": "信贷策略产品经理",
        "brandName": "某消费金融公司",
        "postDescription": description,
        "cityName": "上海",
    }
    if job_id is not None:
        card["encryptJobId"] = job_id
    return card


def _response(code: int, message: str = "",
              card: dict[str, Any] | None = None) -> dict:
    payload: dict[str, Any] = {"code": code, "message": message}
    if card is not None:
        payload["zpData"] = {"jobCard": card}
    return payload


# ---------------------------------------------------------------------------
# 假 SDK：只替换 ``boss_browser_worker`` 真正 import 到的那几个名字。
# 语义逐条对照本机 boss-agent-cli 2.0.0 源码（platforms/zhipin.py、api/client.py）。
# ---------------------------------------------------------------------------

def install_fake_sdk(
    monkeypatch,
    *,
    response: dict[str, Any],
    platform_error: bool = False,
    request_exc: BaseException | None = None,
    zhipin_module_missing: bool = False,
) -> dict[str, Any]:
    seen: dict[str, Any] = {"calls": []}

    class BossClient:
        def __init__(self, auth, cdp_url=None, browser_source=None):
            seen["cdp_url"] = cdp_url
            seen["browser_source"] = browser_source

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def job_card_browser(self, security_id: str, lid: str = ""):
            seen["calls"].append((security_id, lid))
            if request_exc is not None:
                raise request_exc
            return response

    class AuthError(Exception):
        pass

    class PlatformRiskError(Exception):
        pass

    class AccountRiskError(PlatformRiskError):
        code = "ACCOUNT_RISK"

    class EnvironmentRiskError(PlatformRiskError):
        code = "ENVIRONMENT_RISK"

    class AuthManager:
        def __init__(self, data_dir, platform=None, logger=None):
            seen["data_dir"] = str(data_dir)

    class BossPlatform:
        """``code == 0`` —— 与真实 ``BossPlatform.is_success`` 同源语义。"""

        def __init__(self, client):
            if platform_error:
                raise RuntimeError("platform unavailable")
            self._client = client

        def is_success(self, raw):
            return raw.get("code") == 0

        def parse_error(self, raw):
            return ("UNKNOWN", raw.get("message") or "职位详情获取失败")

        def unwrap_data(self, raw):
            return raw.get("zpData")

    zhipin_module: Any = None
    if not zhipin_module_missing:
        zhipin_module = types.ModuleType("boss_agent_cli.platforms.zhipin")
        zhipin_module.BossPlatform = BossPlatform
        zhipin_module._ERROR_CODE_MAP = {9: "RATE_LIMITED", 36: "ACCOUNT_RISK"}

        def response_message(response: dict[str, Any]) -> str:
            return str(response.get("message") or response.get("msg")
                       or response.get("error") or response.get("zpData") or "")

        def classify_code_37(response: dict[str, Any]) -> str:
            message = response_message(response).lower()
            if any(marker in message for marker in
                   ("环境存在异常", "环境异常", "访问环境异常", "请求环境异常")):
                return "environment_risk"
            if any(marker in message for marker in
                   ("__zp_stoken__", "stoken", "token", "登录态过期", "凭证过期")):
                return "token_expired"
            return "environment_risk"

        zhipin_module.response_message = response_message
        zhipin_module.classify_code_37 = classify_code_37

    client_module = types.ModuleType("boss_agent_cli.api.client")
    client_module.BossClient = BossClient
    client_module.AuthError = AuthError
    client_module.PlatformRiskError = PlatformRiskError
    client_module.AccountRiskError = AccountRiskError
    client_module.EnvironmentRiskError = EnvironmentRiskError

    auth_manager_module = types.ModuleType("boss_agent_cli.auth.manager")
    auth_manager_module.AuthManager = AuthManager

    registered: dict[str, Any] = {
        "boss_agent_cli": types.ModuleType("boss_agent_cli"),
        "boss_agent_cli.api": types.ModuleType("boss_agent_cli.api"),
        "boss_agent_cli.auth": types.ModuleType("boss_agent_cli.auth"),
        "boss_agent_cli.platforms": types.ModuleType("boss_agent_cli.platforms"),
        "boss_agent_cli.api.client": client_module,
        "boss_agent_cli.auth.manager": auth_manager_module,
        # ``None`` in sys.modules == 该模块 import 失败：用来模拟「拿不到模块级分类器」。
        "boss_agent_cli.platforms.zhipin": zhipin_module,
    }
    for name, module in registered.items():
        monkeypatch.setitem(sys.modules, name, module)
    return seen


def run_worker_main(
    monkeypatch,
    capsys,
    tmp_path: Path,
    *,
    response: dict[str, Any],
    positional: tuple[str, ...] = (SENTINEL_SID, SENTINEL_LID),
    platform_error: bool = False,
    request_exc: BaseException | None = None,
    zhipin_module_missing: bool = False,
) -> tuple[int, dict[str, Any]]:
    """在进程内跑一次 ``boss_browser_worker.main()``，返回 ``(退出码, 信封)``。"""
    install_fake_sdk(monkeypatch, response=response, platform_error=platform_error,
                     request_exc=request_exc, zhipin_module_missing=zhipin_module_missing)
    argv = [
        "worker", "--data-dir", str(tmp_path),
        "--cdp-url", "http://127.0.0.1:9222", "--browser-source", "existing-browser",
        "--", *positional,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    code = worker.main()
    emitted = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    payload = json.loads(emitted[-1]) if emitted else {}
    return code, payload


# ---------------------------------------------------------------------------
# F3：业务码校验不得依赖 BossPlatform 是否构建成功
# ---------------------------------------------------------------------------

def test_success_envelope_is_partial_not_complete(monkeypatch, capsys, tmp_path) -> None:
    """拿到卡片正文只能证明「有文字」：必须是 boss_browser_card / partial，不是「完整 JD」。"""
    code, payload = run_worker_main(
        monkeypatch, capsys, tmp_path,
        response=_response(0, "Success", card=_success_card()))
    assert code == 0 and payload["ok"] is True
    assert payload["data"]["job_id"] == "JOB-1"
    assert payload["data"]["_detail_source"] == "boss_browser_card"
    assert payload["data"]["_detail_status"] == "partial"
    assert payload["data"]["_description_incomplete"] is False


def test_empty_description_is_missing_not_partial(monkeypatch, capsys, tmp_path) -> None:
    code, payload = run_worker_main(
        monkeypatch, capsys, tmp_path,
        response=_response(0, "Success", card=_success_card(description="   ")))
    assert code == 0 and payload["ok"] is True
    assert payload["data"]["_detail_status"] == "missing"
    assert payload["data"]["_description_incomplete"] is True


def test_risk_response_is_not_success_when_platform_build_fails(
        monkeypatch, capsys, tmp_path) -> None:
    """真机缺陷回归：``BossPlatform`` 构建失败时也不许跳过业务码校验。

    响应里**故意**带上可读的 jobCard —— 老实现 ``platform is None`` 会跳过
    ``is_success``，把这条风险响应当成功往下走；现在必须报错并归因到风控。
    """
    code, payload = run_worker_main(
        monkeypatch, capsys, tmp_path,
        response=_response(37, "您的环境存在异常，请稍后再试",
                           card=_success_card(description="假装是正文")),
        platform_error=True)
    assert code == 1 and payload["ok"] is False
    assert payload["error"]["code"] == "ENVIRONMENT_RISK"
    assert payload["error"]["recoverable"] is False
    # 失败必须可诊断：说清走的是回退判定，以及 platform 为什么没构建起来
    assert payload["error"]["details"]["validation"] == "fallback_code_zero"
    assert "platform unavailable" in payload["error"]["details"]["platform_probe"]
    assert payload["error"]["details"]["business_code"] == 37
    # 绝不夹带「看起来像成功」的 data
    assert payload["data"] is None


def test_platform_build_failure_still_classifies_account_risk(
        monkeypatch, capsys, tmp_path) -> None:
    code, payload = run_worker_main(
        monkeypatch, capsys, tmp_path,
        response=_response(36, "账户存在异常行为", card=_success_card()),
        platform_error=True)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "ACCOUNT_RISK"
    assert payload["error"]["recoverable"] is False


def test_rate_limited_is_recoverable_business_failure(monkeypatch, capsys, tmp_path) -> None:
    code, payload = run_worker_main(
        monkeypatch, capsys, tmp_path,
        response=_response(9, "操作太频繁", card=_success_card()),
        platform_error=True)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "RATE_LIMITED"
    assert payload["error"]["recoverable"] is True


def test_code_37_without_classifier_is_not_asserted_as_environment(
        monkeypatch, capsys, tmp_path) -> None:
    """F4：拿不到 SDK 的 ``classify_code_37`` 时，code 37 **不脑补**成环境风险。

    没有分类器就如实返回 UNKNOWN，由上游 ``refine_risk_code`` 用同一份消息证据决定
    要不要归一成环境风控；绝不「凡是 37 都是环境风险」。
    """
    code, payload = run_worker_main(
        monkeypatch, capsys, tmp_path,
        response=_response(37, "未知 37", card=_success_card()),
        platform_error=True, zhipin_module_missing=True)
    assert code == 1 and payload["ok"] is False
    assert payload["error"]["code"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# F4：结构化上游错误优先，文案仅作有界补充
# ---------------------------------------------------------------------------

def test_typed_sdk_exceptions_win_over_message_text(monkeypatch) -> None:
    """假 SDK 的异常层次：子类也算命中（AccountRiskError ⊂ PlatformRiskError）。"""
    seen = install_fake_sdk(monkeypatch, response={})
    client_module = sys.modules["boss_agent_cli.api.client"]
    assert worker._classify_error(client_module.AccountRiskError("x"))[0] == "ACCOUNT_RISK"
    assert worker._classify_error(client_module.EnvironmentRiskError("x"))[0] == "ENVIRONMENT_RISK"
    # 明确的环境异常即便文案含 token 线索，也不得解释成单纯 token 过期
    code, recoverable, _action = worker._classify_error(
        client_module.EnvironmentRiskError("访问环境风控 (code 37): stoken 已过期"))
    assert code == "ENVIRONMENT_RISK" and recoverable is False
    assert worker._classify_error(client_module.AuthError("nologin"))[0] == "AUTH_REQUIRED"
    assert seen["calls"] == []


def test_unrecognized_platform_risk_subclass_stays_in_risk_family(monkeypatch) -> None:
    """未识别的风控子类：不自动重试、落到风控家族，但**不冒充**某个具体风控码。"""
    install_fake_sdk(monkeypatch, response={})
    client_module = sys.modules["boss_agent_cli.api.client"]

    class FutureRiskError(client_module.PlatformRiskError):
        pass

    code, recoverable, action = worker._classify_error(FutureRiskError("新风控"))
    assert code == "STOPPED_BY_SAFETY"
    assert recoverable is False
    assert "停止自动重试" in action


def test_text_fallback_is_bounded_when_sdk_types_are_unavailable(monkeypatch) -> None:
    """拿不到结构化类型时，文案 markers 仍只认已知片段（不泛化、不臆测）。"""
    monkeypatch.setattr(worker, "_sdk_error_classes", lambda: {})
    assert worker._classify_error(RuntimeError("BOSS 直聘风控拦截 (code 36)"))[0] == "ACCOUNT_RISK"
    assert worker._classify_error(RuntimeError("访问环境风控"))[0] == "ENVIRONMENT_RISK"
    assert worker._classify_error(RuntimeError("stoken 无效"))[0] == "AUTH_EXPIRED"
    assert worker._classify_error(RuntimeError("未知错误"))[0] == "UPSTREAM_PROCESS_FAILED"


def test_real_sdk_classification_when_installed() -> None:
    """本机真实 SDK（离线：只做类型判定，不发请求）——结构化口径同样成立。"""
    client_module = pytest.importorskip("boss_agent_cli.api.client")
    assert worker._classify_error(client_module.AccountRiskError("x"))[0] == "ACCOUNT_RISK"
    assert worker._classify_error(
        client_module.EnvironmentRiskError("stoken 过期"))[0] == "ENVIRONMENT_RISK"
    assert worker._classify_error(client_module.AuthError("未登录"))[0] == "AUTH_REQUIRED"


# ---------------------------------------------------------------------------
# F5：岗位身份必须可核对（worker 侧）
# ---------------------------------------------------------------------------

def test_card_without_job_id_refuses_to_emit_success(monkeypatch, capsys, tmp_path) -> None:
    """卡片没有 encryptJobId → worker 直接报缺字段，不产出无人认领的成功信封。"""
    code, payload = run_worker_main(
        monkeypatch, capsys, tmp_path,
        response=_response(0, "Success", card=_success_card(job_id=None)))
    assert code == 1 and payload["ok"] is False
    assert payload["error"]["code"] == "CARD_JOB_ID_MISSING"
    assert payload["error"]["details"]["identity_field"] == "encryptJobId"
    # 证据里只放字段名清单，不放任何正文/凭据
    assert "encryptJobId" not in payload["error"]["details"]["card_keys"]
    assert "jobName" in payload["error"]["details"]["card_keys"]


# ---------------------------------------------------------------------------
# source_boss.detail()：F5 拒绝保存 / F2 lid 语义
# ---------------------------------------------------------------------------

def _detail_worker_envelope(data: dict[str, Any] | None,
                            error: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "ok": error is None,
        "schema_version": "1.0",
        "command": "detail",
        "data": data,
        "pagination": None,
        "error": error,
        "hints": None,
    }


def _default_failure() -> dict[str, Any]:
    return _detail_worker_envelope(None, {
        "code": "BROWSER_SESSION_NOT_FOUND", "message": "no session",
        "recoverable": True, "recovery_action": None,
    })


def _invocation(payload: dict[str, Any]) -> RawInvocation:
    return RawInvocation(
        argv=[], argv_redacted=[], returncode=0 if payload["ok"] else 1,
        stdout=json.dumps(payload, ensure_ascii=False), stderr="", elapsed_ms=3,
        timed_out=False, data_dir="C:/tmp/upstream",
    )


@pytest.fixture
def captured_detail(monkeypatch) -> dict[str, Any]:
    """替掉真实 worker 子进程，并记录 ``map_detail`` 有没有被调用（=有没有往下存）。"""
    captured: dict[str, Any] = {"payload": None, "map_detail_calls": 0}

    async def fake_run_worker(*, worker_py, python, data_dir, positional=(),
                              cdp_url=None, browser_source=None,
                              rendered_job_id=None,
                              timeout_seconds=120) -> RawInvocation:
        captured["worker_py"] = worker_py
        captured["python"] = python
        captured["positional"] = list(positional)
        captured["cdp_url"] = cdp_url
        captured["browser_source"] = browser_source
        captured["rendered_job_id"] = rendered_job_id
        return _invocation(captured["payload"] or _default_failure())

    original_map_detail = source_boss.map_detail

    def counting_map_detail(*args: Any, **kwargs: Any) -> Any:
        captured["map_detail_calls"] += 1
        return original_map_detail(*args, **kwargs)

    monkeypatch.setattr(source_boss, "run_worker", fake_run_worker)
    monkeypatch.setattr(source_boss, "map_detail", counting_map_detail)
    monkeypatch.setattr(source_boss, "resolve_worker_python", lambda cli_path: "FAKE-WORKER-PY")
    return captured


def _card_data(job_id: str, description: str = "负责风控策略。") -> dict[str, Any]:
    return {
        "job_id": job_id,
        "title": "信贷策略产品经理",
        "company": "某消费金融公司",
        "description": description,
        "city": "上海",
    }


def test_job_id_mismatch_is_refused_before_any_mapping(captured_detail) -> None:
    """F5：job_id 不匹配 → 拒绝，且**不会**走到 map_detail（即不会被保存）。"""
    captured_detail["payload"] = _detail_worker_envelope(_card_data("OTHER-JOB"))
    with pytest.raises(UpstreamError) as raised:
        asyncio.run(source_boss.detail("boss.exe", SENTINEL_SID, "TARGET-JOB", SENTINEL_LID,
                                       Path("C:/tmp/upstream"), 10,
                                       top_level_options=CHANNEL_OPTIONS))
    assert raised.value.code == "UPSTREAM_CONTRACT_CHANGED"
    assert raised.value.evidence["reason"] == "mismatch"
    assert raised.value.evidence["expected_job_id"] == "TARGET-JOB"
    assert raised.value.evidence["returned_job_id"] == "OTHER-JOB"
    assert captured_detail["map_detail_calls"] == 0


def test_missing_returned_job_id_is_refused(captured_detail) -> None:
    """返回没有 ID 时也不许「存了再说」——连 map_detail 都不会调用。"""
    captured_detail["payload"] = _detail_worker_envelope(_card_data(""))
    with pytest.raises(UpstreamError) as raised:
        asyncio.run(source_boss.detail("boss.exe", SENTINEL_SID, None, SENTINEL_LID,
                                       Path("C:/tmp/upstream"), 10,
                                       top_level_options=CHANNEL_OPTIONS))
    assert raised.value.code == "UPSTREAM_CONTRACT_CHANGED"
    assert raised.value.evidence["reason"] == "missing"
    assert captured_detail["map_detail_calls"] == 0


def test_matching_job_id_is_mapped_as_browser_card(captured_detail) -> None:
    captured_detail["payload"] = _detail_worker_envelope(_card_data("TARGET-JOB"))
    mapped = asyncio.run(source_boss.detail("boss.exe", SENTINEL_SID, "TARGET-JOB", SENTINEL_LID,
                                            Path("C:/tmp/upstream"), 10,
                                            top_level_options=CHANNEL_OPTIONS))
    assert captured_detail["map_detail_calls"] == 1
    assert mapped["detail_source"] == "boss_browser_card"
    assert mapped["source_job_id"] == "TARGET-JOB"
    assert mapped["source_url_verified"] is False
    assert captured_detail["worker_py"] == "boss_browser_worker.py"
    assert captured_detail["positional"] == [SENTINEL_SID, SENTINEL_LID]
    assert captured_detail["cdp_url"] == "http://127.0.0.1:9222"
    assert captured_detail["browser_source"] == "existing-browser"
    assert captured_detail["rendered_job_id"] == "TARGET-JOB"


@pytest.mark.parametrize('full', [True,False])
def test_rendered_detail_needs_verified_page_and_unfolded_section(captured_detail,full):
    data = _card_data('TARGET-JOB')
    data.update(_detail_source='boss_rendered_page',_detail_status='complete',
                _rendered_proof={'source_url':'https://www.zhipin.com/job_detail/TARGET-JOB.html',
                    'job_id':'TARGET-JOB','section_count':1,'full_section':full})
    captured_detail['payload'] = _detail_worker_envelope(data)
    mapped = asyncio.run(source_boss.detail('boss.exe',SENTINEL_SID,'TARGET-JOB',None,
        Path('C:/tmp/upstream'),10,top_level_options=CHANNEL_OPTIONS))
    assert mapped['source_url_verified'] is True
    assert mapped['detail_source']=='boss_rendered_page'
    assert mapped['detail_status']==('complete' if full else 'partial')


def test_rendered_detail_without_page_proof_is_refused(captured_detail):
    data = _card_data('TARGET-JOB')
    data.update(_detail_source='boss_rendered_page',_detail_status='complete')
    captured_detail['payload'] = _detail_worker_envelope(data)
    with pytest.raises(UpstreamError,match='区块证明'):
        asyncio.run(source_boss.detail('boss.exe',SENTINEL_SID,'TARGET-JOB',None,
            Path('C:/tmp/upstream'),10,top_level_options=CHANNEL_OPTIONS))


def test_blank_lid_raises_contract_error_naming_the_field(captured_detail) -> None:
    """``lid=""`` → 点名 lid 的契约错，**不是**通用 TOOL_NOT_ALLOWED / ValueError。"""
    with pytest.raises(UpstreamError) as raised:
        asyncio.run(source_boss.detail("boss.exe", SENTINEL_SID, "TARGET-JOB", "",
                                       Path("C:/tmp/upstream"), 10,
                                       top_level_options=CHANNEL_OPTIONS))
    assert raised.value.code == "MISSING_REQUIRED_ARGUMENT"
    assert "lid" in raised.value.message
    assert "lid" in raised.value.operator_action
    assert raised.value.evidence["field"] == "lid"
    assert captured_detail["map_detail_calls"] == 0


@pytest.mark.parametrize("blank", ["   ", "\t", ""])
def test_whitespace_lid_is_treated_as_blank(captured_detail, blank: str) -> None:
    with pytest.raises(UpstreamError) as raised:
        asyncio.run(source_boss.detail("boss.exe", SENTINEL_SID, "TARGET-JOB", blank,
                                       Path("C:/tmp/upstream"), 10,
                                       top_level_options=CHANNEL_OPTIONS))
    assert raised.value.code == "MISSING_REQUIRED_ARGUMENT"


def test_missing_lid_omits_the_positional_argument(captured_detail) -> None:
    """``lid=None`` → 正确省略（SDK 签名允许缺省），不是传空串去撞参数校验。"""
    captured_detail["payload"] = _detail_worker_envelope(_card_data("TARGET-JOB"))
    asyncio.run(source_boss.detail("boss.exe", SENTINEL_SID, "TARGET-JOB", None,
                                   Path("C:/tmp/upstream"), 10,
                                   top_level_options=CHANNEL_OPTIONS))
    assert captured_detail["positional"] == [SENTINEL_SID]


def test_locator_lid_normalization_never_yields_blank_string() -> None:
    """调用方把「没有 lid」统一成 ``None``：'' / 空白 / NULL / 缺失都不得传空串。"""
    import runs

    assert runs._detail_lid({"lid": ""}) is None
    assert runs._detail_lid({"lid": "   "}) is None
    assert runs._detail_lid({"lid": None}) is None
    assert runs._detail_lid({}) is None
    assert runs._detail_lid(None) is None
    assert runs._detail_lid({"lid": " lid-x "}) == "lid-x"


# ---------------------------------------------------------------------------
# F1：worker 的解释器必须**验证后**锁定，不允许未验证回退
# ---------------------------------------------------------------------------

def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> Any:
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def test_probe_reports_failure_reason_instead_of_returning_broken_interpreter(
        monkeypatch) -> None:
    monkeypatch.setattr(source_boss.subprocess, "run",
                        lambda *a, **kw: _completed(1, stderr="ModuleNotFoundError: boss_agent_cli"))
    probe = source_boss.probe_worker_python("C:/fake/python.exe")
    assert probe.ok is False and "ModuleNotFoundError" in probe.reason


def test_probe_timeout_is_reported(monkeypatch) -> None:
    def raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="python", timeout=1)

    monkeypatch.setattr(source_boss.subprocess, "run", raise_timeout)
    probe = source_boss.probe_worker_python("C:/fake/python.exe")
    assert probe.ok is False and "超时" in probe.reason


def test_no_candidate_verifies_raises_diagnosable_error(monkeypatch) -> None:
    """全不过 → 明确报错并列出每个候选的原因，**绝不**默默回退到服务解释器。"""
    source_boss.reset_worker_python_cache()
    monkeypatch.setattr(source_boss, "_configured_worker_python", lambda **kw: None)
    monkeypatch.setattr(source_boss, "_cli_python_candidates", lambda cli: ["C:/a/python.exe"])
    monkeypatch.setattr(source_boss.subprocess, "run",
                        lambda *a, **kw: _completed(1, stderr="no module"))
    try:
        with pytest.raises(UpstreamError) as raised:
            source_boss.resolve_worker_python("C:/x/boss.exe")
    finally:
        source_boss.reset_worker_python_cache()
    assert raised.value.code == "DEPENDENCY_MISSING"
    assert "未验证" in raised.value.message
    attempts = raised.value.evidence["attempts"]
    assert [item["python"] for item in attempts] == ["C:/a/python.exe", sys.executable]
    assert all(item["ok"] is False and item["reason"] for item in attempts)


def test_verified_interpreter_is_locked_and_cached(monkeypatch) -> None:
    source_boss.reset_worker_python_cache()
    calls: list[str] = []

    def fake_run(argv, **_kwargs):
        calls.append(argv[0])
        return _completed(0, stdout="2.0.0\n")

    monkeypatch.setattr(source_boss.subprocess, "run", fake_run)
    monkeypatch.setattr(source_boss, "_configured_worker_python",
                        lambda **kw: "C:/explicit/python.exe")
    monkeypatch.setattr(source_boss, "_cli_python_candidates", lambda cli: [])
    try:
        assert source_boss.resolve_worker_python("C:/x/boss.exe") == "C:/explicit/python.exe"
        assert source_boss.resolve_worker_python("C:/x/boss.exe") == "C:/explicit/python.exe"
    finally:
        source_boss.reset_worker_python_cache()
    assert calls == ["C:/explicit/python.exe"]        # 缓存：只探测一次


def test_unusable_sibling_interpreter_is_skipped_in_favour_of_verified_one(monkeypatch) -> None:
    """CLI 同目录解释器没有 SDK（本机实测同构场景）→ 跳过它，锁定验证通过的那个。"""
    source_boss.reset_worker_python_cache()
    probed: list[str] = []

    def fake_run(argv, **_kwargs):
        probed.append(argv[0])
        ok = argv[0] == sys.executable
        return _completed(0 if ok else 1, stdout="2.0.0\n" if ok else "",
                          stderr="" if ok else "No module named 'boss_agent_cli'")

    monkeypatch.setattr(source_boss.subprocess, "run", fake_run)
    monkeypatch.setattr(source_boss, "_configured_worker_python", lambda **kw: None)
    monkeypatch.setattr(source_boss, "_cli_python_candidates",
                        lambda cli: ["C:/cli/python3.12.exe"])
    try:
        assert source_boss.resolve_worker_python("C:/x/boss.exe") == sys.executable
    finally:
        source_boss.reset_worker_python_cache()
    assert probed == ["C:/cli/python3.12.exe", sys.executable]


def test_candidate_order_puts_explicit_config_first(monkeypatch) -> None:
    """顺序：显式配置 → CLI 同环境 → 服务解释器，去重且保序。"""
    monkeypatch.setattr(source_boss, "_configured_worker_python",
                        lambda **kw: "C:/explicit/python.exe")
    monkeypatch.setattr(source_boss, "_cli_python_candidates",
                        lambda cli: ["C:/cli/python.exe", "C:/explicit/python.exe"])
    candidates = source_boss.worker_python_candidates("C:/x/boss.exe")
    assert candidates[0] == "C:/explicit/python.exe"
    assert candidates[1] == "C:/cli/python.exe"
    assert candidates[-1] == sys.executable
    assert len(candidates) == len({os.path.normcase(item) for item in candidates})


def test_configured_worker_python_prefers_setting_over_env(monkeypatch) -> None:
    monkeypatch.setenv(source_boss.WORKER_PYTHON_ENV_VAR, "C:/env/python.exe")
    assert source_boss._configured_worker_python(
        setting_getter=lambda key: "") == "C:/env/python.exe"
    assert source_boss._configured_worker_python(
        setting_getter=lambda key: "C:/setting/python.exe") == "C:/setting/python.exe"


def test_real_service_interpreter_is_usable(monkeypatch) -> None:
    """本机实测（离线：只做 import 探测，不发平台请求）：服务解释器真的拥有 boss SDK。"""
    pytest.importorskip("boss_agent_cli")
    monkeypatch.delenv(source_boss.WORKER_PYTHON_ENV_VAR, raising=False)
    probe = source_boss.probe_worker_python(sys.executable)
    assert probe.ok is True
    assert probe.version


# ---------------------------------------------------------------------------
# F7：argv_redacted 必须遮蔽 worker 的两个位置参数
# ---------------------------------------------------------------------------

def test_argv_redacted_masks_both_worker_positionals() -> None:
    argv = [
        sys.executable, "boss_browser_worker.py",
        "--data-dir", "C:/tmp/upstream",
        "--cdp-url", "http://127.0.0.1:9222",
        "--browser-source", "existing-browser",
        "--timeout-seconds", "60",
        "--", SENTINEL_SID, SENTINEL_LID,
    ]
    redacted = upstream_config.redact_worker_argv(argv)
    assert redacted[-2:] == ["[REDACTED_securityId]", "[REDACTED_lid]"]
    joined = " | ".join(redacted)
    assert SENTINEL_SID not in joined and SENTINEL_LID not in joined
    # ``--`` 之前照常保留（可诊断），但仍过一层 redact
    assert "boss_browser_worker.py" in redacted[1]
    assert redacted[3] == "C:/tmp/upstream"


def test_argv_redacted_masks_single_positional_too() -> None:
    """lid 缺省时只剩一个位置参数：那一个也必须是占位符。"""
    argv = ["python", "w.py", "--data-dir", "D", "--", SENTINEL_SID]
    assert upstream_config.redact_worker_argv(argv)[-1] == "[REDACTED_securityId]"


def test_argv_redacted_without_separator_falls_back_to_plain_redaction() -> None:
    argv = ["boss.exe", "--data-dir", "D", "--json", "search", "消费金融"]
    assert upstream_config.redact_worker_argv(argv) == upstream_config.redact_argv(argv)


def test_extra_positionals_are_masked_with_generic_labels() -> None:
    """位置参数多于已知标签时也要遮蔽（失败关闭），不许把多出来的值漏出来。"""
    argv = ["python", "w.py", "--", SENTINEL_SID, SENTINEL_LID, "EXTRA-1"]
    redacted = upstream_config.redact_worker_argv(argv)
    assert redacted[-1] == "[REDACTED_positional3]"
    assert "EXTRA-1" not in " | ".join(redacted)


def test_run_worker_never_exposes_positionals_in_redacted_copy(monkeypatch) -> None:
    """端到端（子进程被替换）：真实 argv 带哨兵，落库/展示用的 argv_redacted 不带。"""
    captured: dict[str, Any] = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        return FakeProcess()

    monkeypatch.setattr(upstream_config.asyncio, "create_subprocess_exec",
                        fake_create_subprocess_exec)
    invocation = asyncio.run(upstream_config.run_worker(
        worker_py="boss_browser_worker.py",
        python=sys.executable,
        data_dir=Path(_TMP_DIR),
        positional=(SENTINEL_SID, SENTINEL_LID),
        cdp_url="http://127.0.0.1:9222",
        browser_source="existing-browser",
        timeout_seconds=5,
    ))
    # 真实 argv 必须有值（不然命令跑不起来）
    assert SENTINEL_SID in captured["argv"] and SENTINEL_LID in captured["argv"]
    assert SENTINEL_SID in invocation.argv and SENTINEL_LID in invocation.argv
    # 脱敏副本里必须没有，且位置明确
    joined = " | ".join(invocation.argv_redacted)
    assert SENTINEL_SID not in joined and SENTINEL_LID not in joined
    assert invocation.argv_redacted[-2:] == ["[REDACTED_securityId]", "[REDACTED_lid]"]
    assert invocation.returncode == 0
