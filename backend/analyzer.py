"""finintel analyzer：内置规则分析器（演示模式）。

定位（任务书 3.5 / 9.3）：P1 正式路径是"宿主模型按分析包提取事项"；
本模块是离线演示与校准用的确定性规则实现，模拟宿主提交，
所有偏移均由程序对实际证据文本重新计算，绝不编造原文（任务书 3.5）。
输出结构与 SubmitPayload.analyses 中的 AnalysisItem 一致。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from classify import classify_job

# ---------------------------------------------------------------------------
# 证据文本组装（与 store 中快照保存格式保持一致）
# ---------------------------------------------------------------------------

RESP_HEADER = "【工作职责】"
REQ_HEADER = "【任职要求】"


def build_evidence_text(responsibilities: Optional[str], requirements: Optional[str]) -> tuple[str, dict[str, Any]]:
    """组装证据文本，返回 (evidence_text, 区间信息 section_ranges)。

    固定格式：职责在前、要求在后，各自带段落头。区间为半开 [start, end)，
    按 Python code point 计数（任务书 2.4 evidence 表约束）。
    """
    resp = (responsibilities or "").strip()
    req = (requirements or "").strip()
    parts: list[str] = []
    ranges: dict[str, Any] = {"responsibility": None, "requirement": None}
    pos = 0
    if resp:
        header = RESP_HEADER + "\n"
        parts.append(header)
        ranges["responsibility"] = [pos + len(header), pos + len(header) + len(resp)]
        parts.append(resp)
        pos += len(header) + len(resp)
        if req:
            parts.append("\n\n")
            pos += 2
    if req:
        header = REQ_HEADER + "\n"
        parts.append(header)
        ranges["requirement"] = [pos + len(header), pos + len(header) + len(req)]
        parts.append(req)
    return "".join(parts), ranges


# ---------------------------------------------------------------------------
# 分句与过滤
# ---------------------------------------------------------------------------

BOILERPLATE_PATTERNS = [
    r"^推动(产品)?创新", r"^促进业务发展", r"责任心", r"执行力", r"沟通能力",
    r"团队协作", r"其他(交办)?任务", r"上级交办", r"^配合团队", r"抗压能力",
    r"^参与技术方案评审$", r"^编写.*文档", r"^指导初级工程师", r"本科以上",
    r"^英语流利", r"数据敏感", r"^良好", r"熟悉.*(优先)?$", r"有.*经验优先",
    r"^了解.*业务$", r"^10\s*年以上", r"^跟进.*落地$", r"^参与制定",
]


def _is_boilerplate(sentence: str) -> bool:
    s = sentence.strip()
    if not s:
        return True
    for pat in BOILERPLATE_PATTERNS:
        if re.search(pat, s):
            return True
    # 无业务对象的短句（<6 字）视为无信息
    if len(s) < 6:
        return True
    return False


def split_sentences(text: str) -> list[tuple[int, str]]:
    """按换行/分号/句号分句，返回 [(句子在 text 中的起始偏移, 句子)]。"""
    results: list[tuple[int, str]] = []
    pattern = re.compile(r"[；;。\n]+")
    pos = 0
    for m in pattern.finditer(text):
        seg = text[pos:m.start()]
        if seg.strip():
            results.append((pos, seg.strip()))
        pos = m.end()
    seg = text[pos:]
    if seg.strip():
        results.append((pos, seg.strip()))
    return results


# ---------------------------------------------------------------------------
# 动作与领域映射
# ---------------------------------------------------------------------------

ACTION_PATTERNS: list[tuple[str, list[str]]] = [
    ("explore", ["探索", "研究", "预研"]),
    ("maintain", ["维护", "保障现有", "排查", "稳定性保障"]),
    ("build", ["搭建", "从零", "建设", "设计", "开发", "负责", "构建", "建立"]),
    ("optimize", ["优化", "提升", "改进", "降低", "提高"]),
    ("operate", ["运营", "日常", "跟进", "管理"]),
]

DOMAIN_PATTERNS: list[tuple[str, list[str]]] = [
    ("funding_settlement", ["资金", "对账", "清结算", "分单", "放款路由", "账务"]),
    ("credit_risk_pricing", ["授信", "额度管理", "定价", "反欺诈", "风控决策", "风控规则", "准入"]),
    ("borrowing_conversion", ["借款流程", "用信", "复借", "借款申请", "额度使用"]),
    ("billing_repayment", ["账单", "还款", "分期", "扣款", "代收代付"]),
    ("postloan_service", ["催收", "贷后", "客服", "投诉", "质检"]),
    ("acquisition_channel", ["获客", "渠道", "流量", "申请入口", "场景合作"]),
    ("b2b_technology", ["saas", "售前", "实施方案", "对外提供", "标准化产品文档", "输出实施"]),
    ("overseas_credit", ["海外", "东南亚"]),
    ("data_platform", ["用户标签", "实验平台", "数据服务", "客群分析"]),
    ("credit_risk_pricing", ["风控"]),
]

AUX_TAG_PATTERNS: list[tuple[str, list[str]]] = [
    ("AI", ["大模型", "智能", "算法", "nlp"]),
    ("反欺诈", ["反欺诈"]),
    ("信贷核心", ["核心系统", "信贷核心", "贷款核心"]),
]

DEFAULT_LIMITATIONS = ["无法确认实际项目进度", "招聘文本不能证明业务已上线"]


def _match_first(text: str, table: list[tuple[str, list[str]]]) -> Optional[str]:
    low = text.lower()
    for value, pats in table:
        for p in pats:
            if p in low:
                return value
    return None


def _aux_tags(sentence: str) -> list[str]:
    tags = []
    for tag, pats in AUX_TAG_PATTERNS:
        low = sentence.lower()
        if any(p in low for p in pats):
            tags.append(tag)
    return tags


def _norm_statement(sentence: str) -> str:
    """轻度压缩：去职责前缀与标点尾巴，不改写语义（任务书 2.4）。"""
    s = re.sub(r"^(负责|参与|推动|跟进|配合|优先)?", "", sentence.strip())
    s = s.rstrip("。；;，, ")
    return s[:300]


# ---------------------------------------------------------------------------
# 主入口：生成符合严格 Schema 的分析结果
# ---------------------------------------------------------------------------

def analyze_snapshot(snapshot: dict[str, Any], model_label: str = "rule-analyzer-demo-v1") -> dict[str, Any]:
    """对单个快照生成 AnalysisItem 结构（dict 形式，提交时再走严格校验）。"""
    evidence_text = snapshot["evidence_text"]
    resp_text = snapshot.get("responsibilities_text") or ""
    req_text = snapshot.get("requirements_text") or ""
    snapshot_id = snapshot.get("id") or snapshot.get("snapshot_id")

    cls = classify_job(snapshot.get("title", ""), resp_text, req_text,
                       snapshot.get("company_industry", ""))
    findings: list[dict[str, Any]] = []

    if resp_text.strip():
        for offset, sentence in split_sentences(resp_text):
            if _is_boilerplate(sentence):
                continue
            action = _match_first(sentence, ACTION_PATTERNS) or "unknown"
            domain = _match_first(sentence, DOMAIN_PATTERNS)
            if domain is None:
                # 有动作与对象但无法映射字典时归 other，不强选最接近分类
                domain = "other"
            # 证据定位：句子在 evidence_text 中的精确偏移（程序计算，非猜测）
            local_start = offset
            quote = sentence
            start_in_evidence = evidence_text.find(quote)
            if start_in_evidence < 0:
                # 句子经过 strip 后无法直接定位时，跳过而不编造偏移
                continue
            findings.append({
                "statement": _norm_statement(sentence),
                "business_domain": domain,
                "action_type": action,
                "audience": None,
                "market": "海外" if domain == "overseas_credit" else None,
                "product_names": [],
                "interpretation": "岗位职责文本提及该方向的工作（规则分析器生成，语义待人工复核）",
                "limitations": list(DEFAULT_LIMITATIONS),
                "evidence": [{
                    "section": "responsibility",
                    "quote": quote,
                    "start": start_in_evidence,
                    "end": start_in_evidence + len(quote),
                }],
            })

    # 任职要求中的"经验优先"不得生成事项（AC-11）；这里天然不会处理要求区
    # 销售/不相关岗位不生成事项，保留排除理由即可（任务书 3.5 第 5 条）
    if cls["role_class"] == "sales" or cls["relevance"] == "irrelevant":
        result_status = "excluded"
        findings = []
    elif findings:
        result_status = "findings_found"
    else:
        result_status = "insufficient_information"

    return {
        "snapshot_id": snapshot_id,
        "role_class": cls["role_class"],
        "relevance": cls["relevance"],
        "classification_reason": cls["reason"],
        "result_status": result_status,
        "findings": findings,
    }
