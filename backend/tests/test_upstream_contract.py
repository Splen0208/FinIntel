# -*- coding: utf-8 -*-
"""Contract tests that do not require a BOSS account or network access."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

import upstream_config  # noqa: E402
from upstream_config import (  # noqa: E402
    RawInvocation,
    build_argv,
    redact,
    resolve_upstream_data_dir,
)
from upstream_contract import map_detail, map_search_card  # noqa: E402
from upstream_envelope import (  # noqa: E402
    FailureLayer,
    UpstreamError,
    parse_envelope,
    require_success,
    sanitize_hints,
)


def invocation(payload: object, *, returncode: int = 0, raw: bool = False) -> RawInvocation:
    stdout = str(payload) if raw else json.dumps(payload, ensure_ascii=False)
    return RawInvocation(
        argv=["boss", "--data-dir", "C:/tmp/upstream", "--json", "search"],
        argv_redacted=["boss", "--data-dir", "C:/tmp/upstream", "--json", "search"],
        returncode=returncode,
        stdout=stdout,
        stderr="",
        elapsed_ms=12,
        timed_out=False,
        data_dir="C:/tmp/upstream",
    )


def test_build_search_argv_has_top_options_before_positional() -> None:
    data_dir = (upstream_config.PROJECT_DIR / ".local" / "test-upstream").resolve()
    argv = build_argv(
        "boss.exe",
        "search",
        data_dir=data_dir,
        positional=("消费金融 产品经理",),
        options={"--page": 1, "--no-cache": True},
    )
    assert argv == [
        "boss.exe", "--data-dir", str(data_dir), "--json", "search",
        "消费金融 产品经理", "--page", "1", "--no-cache",
    ]


def test_build_detail_uses_security_id_as_positional() -> None:
    argv = build_argv(
        "boss.exe",
        "detail",
        data_dir=Path("C:/tmp/upstream"),
        positional=("security-123",),
        options={"--job-id": "job-123", "--lid": "lid-123"},
    )
    assert argv[4:7] == ["detail", "security-123", "--job-id"]
    assert "--security-id" not in argv


@pytest.mark.parametrize("options", [{"--security-id": "x"}, {"--json": True}])
def test_build_argv_rejects_invalid_subcommand_options(options: dict[str, object]) -> None:
    with pytest.raises(UpstreamError) as raised:
        build_argv(
            "boss.exe",
            "detail",
            data_dir=Path("C:/tmp/upstream"),
            positional=("security-123",),
            options=options,
        )
    assert raised.value.code == "TOOL_NOT_ALLOWED"


def test_build_argv_rejects_write_commands_and_option_injection() -> None:
    with pytest.raises(UpstreamError, match="只读白名单"):
        build_argv("boss.exe", "apply", data_dir=Path("C:/tmp/upstream"))
    with pytest.raises(UpstreamError) as raised:
        build_argv(
            "boss.exe", "search", data_dir=Path("C:/tmp/upstream"), positional=("--json",)
        )
    assert raised.value.code == "TOOL_NOT_ALLOWED"


def test_data_dir_resolution_priority_and_relative_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(upstream_config.UPSTREAM_ENV_VAR, "env-relative")
    setting = resolve_upstream_data_dir(create=False, setting_getter=lambda _key: "setting-relative")
    assert setting.source == "setting"
    assert setting.path == (upstream_config.PROJECT_DIR / "setting-relative").resolve()

    environment = resolve_upstream_data_dir(create=False, setting_getter=lambda _key: None)
    assert environment.source == "env"
    assert environment.path == (upstream_config.PROJECT_DIR / "env-relative").resolve()

    monkeypatch.delenv(upstream_config.UPSTREAM_ENV_VAR)
    default = resolve_upstream_data_dir(create=False, setting_getter=lambda _key: None)
    assert default.source == "default"
    assert default.path == (upstream_config.PROJECT_DIR / upstream_config.DEFAULT_UPSTREAM_REL).resolve()


@pytest.mark.parametrize(
    "payload",
    [
        "diagnostic line\n{}",
        {"ok": True, "schema_version": "1.0", "command": "search"},
        {"ok": True, "schema_version": "2.0", "command": "search", "data": []},
    ],
)
def test_dirty_missing_or_changed_envelope_is_contract_error(payload: object) -> None:
    envelope = parse_envelope(invocation(payload, raw=isinstance(payload, str)))
    assert envelope.ok is False
    assert envelope.layer in {FailureLayer.ENVELOPE, FailureLayer.CONTRACT}
    with pytest.raises(UpstreamError) as raised:
        require_success(envelope, command="search")
    assert raised.value.code == "UPSTREAM_CONTRACT_CHANGED"


def test_auth_required_preserves_recovery_action() -> None:
    payload = {
        "ok": False,
        "schema_version": "1.0",
        "command": "status",
        "data": None,
        "pagination": None,
        "error": {
            "code": "AUTH_REQUIRED",
            "message": "需要登录",
            "recoverable": False,
            "recovery_action": "请在本机终端完成人工登录",
        },
        "hints": None,
    }
    envelope = parse_envelope(invocation(payload, returncode=1))
    with pytest.raises(UpstreamError) as raised:
        require_success(envelope, command="status")
    assert raised.value.code == "AUTH_REQUIRED"
    assert "请在本机终端完成人工登录" in raised.value.operator_action


def test_sanitize_hints_removes_next_actions_and_action_commands() -> None:
    actions, safe = sanitize_hints({
        "next_actions": ["boss detail x", "boss apply x", "boss greet x"],
        "auth_health": {"status": "missing"},
        "checks": [
            {"name": "cookie_presence", "recovery_action": "请在本机终端完成人工登录"},
            {"name": "bad", "recovery_action": "boss apply x"},
        ],
    })
    assert safe is not None
    assert "next_actions" not in safe
    assert safe["auth_health"]["status"] == "missing"
    assert actions == ["请在本机终端完成人工登录"]
    assert "apply" not in json.dumps(safe, ensure_ascii=False).lower()


def test_detail_ip_rejection_is_environment_risk_not_retryable_unknown():
    payload = {"ok": False, "schema_version": "1.0", "command": "detail",
               "data": None, "pagination": None, "hints": None,
               "error": {"code": "UNKNOWN", "message": "您的IP地址存在异常行为.",
                         "recoverable": True}}
    with pytest.raises(UpstreamError) as caught:
        require_success(parse_envelope(invocation(payload, returncode=1)), command="detail")
    assert caught.value.code == "ENVIRONMENT_RISK"
    assert caught.value.retryable is False
    from upstream_envelope import refine_risk_code
    assert refine_risk_code("UPSTREAM_UNKNOWN", "未知详情错误") == "UPSTREAM_UNKNOWN"


def test_valid_empty_search_is_the_only_empty_result_path() -> None:
    payload = {
        "ok": True,
        "schema_version": "1.0",
        "command": "search",
        "data": [],
        "pagination": {"page": 1, "has_more": False, "total": 0},
        "error": None,
        "hints": None,
    }
    envelope = require_success(parse_envelope(invocation(payload)), command="search")
    assert envelope.layer == FailureLayer.OK
    assert envelope.data == []
    assert envelope.pagination == {"page": 1, "has_more": False, "total": 0}


def test_success_contract_rejects_missing_search_fields() -> None:
    payload = {
        "ok": True,
        "schema_version": "1.0",
        "command": "search",
        "data": [{"title": "产品经理", "company": "示例公司"}],
        "pagination": {"page": 1, "has_more": False, "total": 1},
        "error": None,
        "hints": None,
    }
    envelope = parse_envelope(invocation(payload))
    with pytest.raises(UpstreamError) as raised:
        require_success(envelope, command="search")
    assert raised.value.code == "UPSTREAM_CONTRACT_CHANGED"


def test_exact_snake_case_field_mapping() -> None:
    card = map_search_card({
        "job_id": "job-1",
        "security_id": "security-1",
        "title": "消费金融产品经理",
        "company": "真实公司",
        "city": "北京",
        "employment_type": "全职",
    })
    assert card["source_job_id"] == "job-1"
    assert card["company_name"] == "真实公司"
    detail = map_detail({
        "job_id": "job-1",
        "title": "消费金融产品经理",
        "company": "真实公司",
        "description": "负责消费金融产品规划与落地。",
        "employment_type": "全职",
    }, security_id="security-1")
    assert detail["responsibilities"] == "负责消费金融产品规划与落地。"
    assert detail["requirements"] == ""
    assert detail["source_platform"] == "zhipin"
    assert detail["source_url_verified"] is False


# ---------------------------------------------------------------------------
# redact()：值含空格/分号的凭据必须走**行级**规则；诊断文本必须逐字不变
#
# 真机缺口：`user_agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 …`
# 的单 token 组只能吃掉 `Mozilla/5.0`，后面的分号段原样落库。
# ---------------------------------------------------------------------------

def test_redact_line_level_credentials_swallow_spaces_and_semicolons() -> None:
    dirty = "\n".join((
        "user_agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln",
        "set-cookie: wt2=AbCdEf123456; Path=/; HttpOnly; Secure",
        "cookie: wt2AbCdEf123456; stoken=eyJhbGciOiJIUzI1NiJ9abcdef",
    ))
    clean = redact(dirty)
    for needle in (
        "Mozilla/5.0", "Windows NT 10.0", "Win64", "AppleWebKit/537.36", "Chrome/131",
        "eyJhbGciOiJIUzI1NiJ9", "wt2AbCdEf123456", "HttpOnly",
    ):
        assert needle not in clean, needle
    # 每个键各脱敏一次，计数也必须对得上（不多删不少删）
    assert clean.count("[REDACTED]") == 4, clean


def test_redact_line_level_rule_covers_json_shaped_lines() -> None:
    """上游 CLI 的 stderr 是 JSON 行：引号夹在键与冒号之间，兜底规则必须仍然生效。"""
    line = '{"code":"OWN_BROWSER_NO_SESSION","authorization":"Bearer eyJhbGciOiJIUzI1NiJ9.abc"}'
    clean = redact(f"""error: {line}""")
    assert "eyJhbGciOiJIUzI1NiJ9" not in clean
    assert "OWN_BROWSER_NO_SESSION" in clean      # 工程码不是凭据，必须留着（诊断要用）
    assert "[REDACTED]" in clean


def test_redact_bare_bearer_token_is_redacted() -> None:
    """第二层：没有 authorization 键、只剩裸 `Bearer eyJ…` 也不能漏。"""
    clean = redact("upstream said: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln")
    assert "eyJhbGciOiJIUzI1NiJ9" not in clean
    assert "Bearer [REDACTED]" in clean


def test_redact_keeps_diagnostics_verbatim() -> None:
    """反向断言：行级规则绝不能把诊断文本也吃掉（把诊断能力修废）。

    这三条都是真机 stderr / argv 里真实出现过的形状，必须逐字不变。
    """
    for line in (
        "Page.goto: Timeout 15000ms exceeded.",                     # 设计 §2.2 记录的真机现象
        "TimeoutError: Page.goto: Timeout 15000ms exceeded.",       # 信封口径 f"{type(exc)}: {exc}"
        "boss --data-dir C:/tmp/upstream --cookie-source chrome --json search",  # argv 回显
    ):
        assert redact(line) == line, repr(redact(line))


def test_redact_mixed_stderr_redacts_credentials_but_keeps_the_failure_line() -> None:
    """混合场景：同一段 stderr 里脱掉凭据，同时保留失败诊断行。"""
    blob = "\n".join((
        "user_agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln",
        "Page.goto: Timeout 15000ms exceeded.",
    ))
    clean = redact(blob)
    assert "Mozilla/5.0" not in clean and "eyJhbGciOiJIUzI1NiJ9" not in clean
    assert "Page.goto: Timeout 15000ms exceeded." in clean
    # 行数不变：脱敏不得吞行（诊断行的上下文要留着）
    assert len(clean.split("\n")) == 3


def test_redact_single_token_rule_still_works_and_keeps_prose() -> None:
    """单 token 规则仍在；且 `token: not found` 这类诊断文本不会被**整行**吃掉。

    只吃掉紧随键后的那个 token（这里是 `not`）——这是既有口径，刻意保留：
    它保证「token: <值>」形状脱敏的同时，不会把整行诊断文本抹平。
    """
    clean = redact("token: 9f8e7d6c5b4a39281706f5e4d3c2b1a0 api_key=sk-abcdef123456")
    assert "9f8e7d6c5b4a39281706f5e4d3c2b1a0" not in clean
    assert "sk-abcdef123456" not in clean
    assert redact("token: not found in cookie jar") == "token:[REDACTED] found in cookie jar"
    # 行级键（cookie 等）**没有** `=`/`:` 时不得触发：`in cookie jar` 必须原样保留
    assert redact("in cookie jar") == "in cookie jar"


# ---------------------------------------------------------------------------
# 32+ 位十六进制串：`\b` 定界在紧贴词字符（CJK / 字母）时会整串漏网
# ---------------------------------------------------------------------------

def test_redact_long_hex_tokens_are_bounded_by_non_hex_not_word_boundaries() -> None:
    """长十六进制串（token 形状）紧贴 CJK / 字母 / 无分隔键时也必须脱敏。

    真机缺口（B3）：旧规则 ``\\b[0-9a-f]{32,}\\b`` 依赖词边界，而 CJK 与字母同样是
    词字符 —— ``会话失效<32hex>已过期``、``stoken<32hex>``（无 ``=``/``:``）里 ``\\b``
    不成立 → 整串原样落库。改成「前后不是十六进制字符」的定界后必须兜住。
    """
    hex32 = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"
    assert len(hex32) == 32
    cases = (
        f"token 已失效：{hex32} 请重新登录",   # 独立出现（前后空格，旧规则本就能兜）
        f"会话失效{hex32}已过期",              # 紧贴中文（CJK 也是词字符 → 旧规则漏网）
        f"xyz{hex32}zzz",                      # 紧贴非 hex 字母（`\\b` 不成立 → 旧规则漏网）
        f"stoken{hex32}",                      # 无 `=`/`:` 分隔的 stoken（无凭据对可依赖）
        f"error code=1 session={hex32}",       # `=` 后紧贴（无空格）
    )
    for raw in cases:
        clean = redact(raw)
        assert hex32 not in clean, raw
        assert "[REDACTED_TOKEN]" in clean, raw


def test_redact_does_not_truncate_lone_short_hex_runs() -> None:
    """反向断言：不足 32 位的十六进制串不算 token，必须原样保留（别过度脱敏）。"""
    short = "9f8e7d6c5b4a39281706f5e4d3c2b1a"     # 31 位
    assert len(short) == 31
    assert redact(f"job id {short} done") == f"job id {short} done"
