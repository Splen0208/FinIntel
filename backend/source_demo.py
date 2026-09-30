"""finintel 合成演示数据源。

明确标注 synthetic：仅用于离线演示从岗位文本到报告的完整处理链路（任务书 8.3），
不代表任何真实公司动态，不能冒充真实采集结果。
数据覆盖任务书 8.2 建议的样本类型：真实产品、业务研发、伪产品销售、无价值套话、
只有任职要求的线索、同名公司、重复 JD、探索性工作、维护性工作、长文本。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "synthetic_jobs.json"

_CACHE: Optional[list[dict[str, Any]]] = None


def _load() -> list[dict[str, Any]]:
    global _CACHE
    if _CACHE is None:
        with open(FIXTURE_PATH, encoding="utf-8") as f:
            _CACHE = json.load(f)
    return _CACHE


def search(query: str, city: Optional[str], page: int) -> dict[str, Any]:
    """按查询词过滤合成岗位，每页 10 条。空列表也是成功响应（AC-02）。"""
    jobs = _load()
    keywords = [k.strip() for k in query.replace("　", " ").split(" ") if k.strip()]
    matched: list[dict[str, Any]] = []
    for j in jobs:
        if city and j.get("city") and j["city"] != city:
            continue
        hay = " ".join([
            j["title"], j["company_name"], j.get("responsibilities", "") or "",
            j.get("requirements", "") or "", " ".join(j.get("tags", [])),
        ])
        if keywords and not any(k.lower() in hay.lower() for k in keywords):
            continue
        matched.append(j)
    page_size = 10
    start = (page - 1) * page_size
    items = matched[start:start + page_size]
    return {
        "cards": [
            {
                "source_job_id": j["source_job_id"],
                "security_id": j["security_id"],
                "title": j["title"],
                "company_name": j["company_name"],
                "company_industry": j.get("company_industry", ""),
                "city": j.get("city"),
                "salary": j.get("salary", ""),
                "experience": j.get("experience", ""),
                "job_type": j.get("job_type", ""),
                "active_time": j.get("active_time", "刚刚活跃"),
            }
            for j in items
        ],
        "total": len(matched),
        "page": page,
        "page_size": page_size,
        "has_more": start + page_size < len(matched),
    }


def detail(security_id: str) -> dict[str, Any]:
    """按 security_id 读取合成详情。岗位失效时返回结构化错误。"""
    for j in _load():
        if j["security_id"] == security_id:
            return {
                "source_job_id": j["source_job_id"],
                "title": j["title"],
                "company_name": j["company_name"],
                "city": j.get("city"),
                "salary": j.get("salary", ""),
                "job_type": j.get("job_type", ""),
                "published_at_raw": j.get("published_at_raw"),
                "source_status": j.get("source_status", "unknown"),
                "responsibilities": j.get("responsibilities") or "",
                "requirements": j.get("requirements") or "",
                "company_intro": j.get("company_intro") or "",
                "source_url": j.get("source_url"),
            }
    raise KeyError(f"security_id 不存在或已失效: {security_id}")
