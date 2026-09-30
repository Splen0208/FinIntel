"""Field contract for the locked boss-agent-cli 2.0.0 release."""
from __future__ import annotations

from typing import Any, Iterable, Optional

from upstream_envelope import FailureLayer, UpstreamError

UPSTREAM_PACKAGE = "boss-agent-cli"
UPSTREAM_VERSION = "2.0.0"
EXPECTED_SCHEMA_VERSION = "1.0"
URL_TEMPLATE_DEFAULT = "https://www.zhipin.com/job_detail/{job_id}.html"
SECURITY_ID_TTL_SECONDS = 1800
MIN_RESPONSIBILITY_CHARS = 1
SUSPICIOUS_RESPONSIBILITY_CHARS = 20

SEARCH_REQUIRED_FIELDS = frozenset({"job_id", "security_id", "title", "company"})
SEARCH_OPTIONAL_FIELDS = frozenset({
    "city", "district", "salary", "experience", "education", "skills", "welfare",
    "industry", "scale", "stage", "boss_name", "boss_title", "boss_active", "lid",
    "raw_job_type", "employment_type", "days_per_week", "least_month", "job_labels",
    "greeted", "match_score", "welfare_match",
})
PAGINATION_REQUIRED_FIELDS = frozenset({"page", "has_more", "total"})
DETAIL_REQUIRED_FIELDS = frozenset({"job_id", "title", "company", "description"})
DETAIL_OPTIONAL_FIELDS = frozenset({
    "salary", "city", "experience", "education", "address", "skills", "boss_name",
    "boss_title", "boss_active", "security_id", "raw_job_type", "employment_type",
    "days_per_week", "least_month", "pay_type", "greeted",
})


class ContractViolation(UpstreamError):
    """Raised when a successful upstream payload violates the locked contract."""


def contract_error(
    missing: Iterable[str], command: str, sample_keys: Iterable[str]
) -> ContractViolation:
    """Build a deterministic contract error without treating data as empty."""
    missing_fields = sorted(set(missing))
    available_fields = sorted(set(sample_keys))
    return ContractViolation(
        "UPSTREAM_CONTRACT_CHANGED",
        f"{command} 返回缺少必填字段 {missing_fields}；可用字段为 {available_fields}",
        False,
        f"停止将该响应入库，固定 {UPSTREAM_PACKAGE}=={UPSTREAM_VERSION} 并更新字段映射",
        layer=FailureLayer.CONTRACT,
        evidence={
            "upstream_command": command,
            "missing_fields": missing_fields,
            "sample_keys": available_fields,
        },
    )


def _require_mapping(value: object, command: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractViolation(
            "UPSTREAM_CONTRACT_CHANGED",
            f"{command} 返回条目不是对象",
            False,
            "停止入库并核对上游字段契约",
            layer=FailureLayer.CONTRACT,
        )
    return value


def _optional(data: dict[str, Any], key: str, default: Any = None) -> Any:
    return data[key] if key in data else default


def map_search_card(item: dict[str, Any]) -> dict[str, Any]:
    """Map one normalized snake_case search item without alias fallbacks."""
    data = _require_mapping(item, "search")
    missing = SEARCH_REQUIRED_FIELDS - data.keys()
    if missing:
        raise contract_error(missing, "search", data.keys())
    for field in SEARCH_REQUIRED_FIELDS:
        if data[field] is None or (isinstance(data[field], str) and not data[field].strip()):
            raise contract_error({field}, "search", data.keys())
    return {
        "source_job_id": str(data["job_id"]),
        "security_id": str(data["security_id"]),
        "lid": _optional(data, "lid"),
        "title": str(data["title"]),
        "company_name": str(data["company"]),
        "company_industry": _optional(data, "industry", "") or "",
        "city": _optional(data, "city"),
        "district": _optional(data, "district"),
        "salary": _optional(data, "salary", "") or "",
        "experience": _optional(data, "experience", "") or "",
        "education": _optional(data, "education", "") or "",
        "job_type": _optional(data, "employment_type", "") or "",
        "skills": _optional(data, "skills", []) or [],
        "welfare": _optional(data, "welfare", []) or [],
        "boss_active": _optional(data, "boss_active"),
        "match_score": _optional(data, "match_score"),
    }


def map_detail(
    data: dict[str, Any],
    *,
    security_id: str,
    url_template: str = URL_TEMPLATE_DEFAULT,
    url_verified: bool = False,
) -> dict[str, Any]:
    """Map a detail payload and preserve the source identity and evidence body."""
    item = _require_mapping(data, "detail")
    missing = DETAIL_REQUIRED_FIELDS - item.keys()
    if missing:
        raise contract_error(missing, "detail", item.keys())
    for field in ("job_id", "title", "company"):
        if item[field] is None or not str(item[field]).strip():
            raise contract_error({field}, "detail", item.keys())
    description = item["description"]
    if not isinstance(description, str):
        raise contract_error({"description(string)"}, "detail", item.keys())
    responsibilities = description.strip()
    source_job_id = str(item["job_id"])
    source_url = url_template.format(job_id=source_job_id) if source_job_id else None
    warning = "上游未提供全文完整性证明，当前正文可能为职位卡片摘要" if responsibilities else None
    if 0 < len(responsibilities) < SUSPICIOUS_RESPONSIBILITY_CHARS:
        warning = "职责正文长度低于 20 字，请人工核对完整性"
    return {
        "source_job_id": source_job_id,
        "security_id": security_id,
        "title": str(item["title"]),
        "company_name": str(item["company"]),
        "city": _optional(item, "city"),
        "salary": _optional(item, "salary", "") or "",
        "experience": _optional(item, "experience", "") or "",
        "education": _optional(item, "education", "") or "",
        "job_type": _optional(item, "employment_type", "") or "",
        "published_at_raw": None,
        "source_status": "unknown",
        "responsibilities": responsibilities,
        "requirements": "",
        "company_intro": "",
        "source_url": source_url,
        "source_platform": "zhipin",
        "source_url_verified": bool(url_verified),
        "address": _optional(item, "address"),
        "skills": _optional(item, "skills", []) or [],
        "boss_name": _optional(item, "boss_name"),
        "boss_title": _optional(item, "boss_title"),
        "boss_active": _optional(item, "boss_active"),
        "mapping_warning": warning,
        "detail_source": "cli_detail_unverified",
        "detail_status": "partial" if responsibilities else "missing",
    }


def check_contract_drift(schema_payload: dict[str, Any]) -> list[str]:
    """Compare a schema response with the locked positional/option contract."""
    issues: list[str] = []
    if not isinstance(schema_payload, dict):
        return ["schema payload 不是对象"]
    if schema_payload.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        issues.append(
            f"schema_version={schema_payload.get('schema_version')!r}，期望 {EXPECTED_SCHEMA_VERSION!r}"
        )
    data = schema_payload.get("data")
    if not isinstance(data, dict):
        issues.append("schema.data 不是对象")
        return issues
    commands = data.get("commands")
    if not isinstance(commands, dict):
        issues.append("schema.data.commands 不是对象")
        return issues
    expectations = {
        "search": ({"--page", "--city", "--no-cache"}, 1),
        "detail": ({"--job-id", "--lid"}, 1),
    }
    for name, (required_options, positional_count) in expectations.items():
        command = commands.get(name)
        if not isinstance(command, dict):
            issues.append(f"缺少命令 {name}")
            continue
        args = command.get("args")
        if not isinstance(args, list) or len(args) != positional_count:
            issues.append(f"{name}.args 数量变化：{args!r}")
        options = command.get("options")
        if not isinstance(options, dict):
            issues.append(f"{name}.options 不是对象")
            continue
        missing_options = required_options - options.keys()
        if missing_options:
            issues.append(f"{name} 缺少选项 {sorted(missing_options)}")
    return issues
