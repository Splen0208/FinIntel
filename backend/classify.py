"""finintel classify：岗位分类显式规则（任务书 3.3）。

先看工作内容，后看标题；不允许因为标题没有"消费金融"就直接删除岗位。
每条分类必须给出理由。此模块是确定性规则，不调用任何模型。
"""
from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# 关键词组
# ---------------------------------------------------------------------------

SALES_PATTERNS = [
    "电销", "电话销售", "地推", "线下推广", "客户贷款推广", "推广贷款",
    "获客指标", "拉新", "电话营销", "销售任务", "业绩指标",
]
PRODUCT_PATTERNS = [
    "产品经理", "产品总监", "产品专家", "产品规划", "产品设计", "需求设计",
    "用户旅程", "产品化", "产品侧", "需求文档", "prd",
]
ENGINEERING_PATTERNS = [
    "java", "研发", "开发", "工程师", "架构师", "后端", "接口改造", "系统设计",
    "服务架构", "技术方案", "微服务",
]
ALGO_PATTERNS = [
    "算法", "数据研发", "数据开发", "机器学习", "大模型", "nlp", "数据仓库",
]
DIRECT_CREDIT_PATTERNS = [
    "信贷", "贷款", "授信", "额度", "定价", "风控", "反欺诈", "资金方", "放款",
    "账单", "还款", "催收", "贷后", "用信", "复借", "消费金融", "现金贷", "账务",
    "清结算", "对账", "分单", "借款",
]
IRRELEVANT_PATTERNS = ["证券交易", "保险营销", "购物车", "商品推荐", "电商运营"]
ADJACENT_PATTERNS = ["证券", "保险", "基金", "理财", "支付通道", "电商"]


def _hit(text: str, patterns: list[str]) -> list[str]:
    low = text.lower()
    return [p for p in patterns if p in low]


def classify_job(title: str, responsibilities: str, requirements: str,
                 company_industry: str = "") -> dict:
    """返回 role_class / relevance / reason。

    规则顺序（任务书 3.3）：
    1. 正文含销售特征 → sales（即使标题含"产品"）
    2. 职责含产品工作特征 → product
    3. 职责含研发特征 → engineering / algorithm_data
    4. 信息不足 → unknown
    相关性独立判定：职责含信贷业务词 → direct；正文为空 → unknown；
    明确其他行业且无信贷词 → irrelevant / adjacent。
    """
    body = f"{responsibilities}\n{requirements}"
    reasons: list[str] = []

    sales_hits = _hit(body, SALES_PATTERNS)
    product_hits = _hit(f"{title}\n{responsibilities}", PRODUCT_PATTERNS)
    eng_hits = _hit(f"{title}\n{responsibilities}", ENGINEERING_PATTERNS)
    algo_hits = _hit(f"{title}\n{responsibilities}", ALGO_PATTERNS)
    credit_hits = _hit(body, DIRECT_CREDIT_PATTERNS)
    irrelevant_hits = _hit(body, IRRELEVANT_PATTERNS)
    adjacent_hits = _hit(body, ADJACENT_PATTERNS)

    if not body.strip():
        return {
            "role_class": "unknown",
            "relevance": "unknown",
            "reason": "职责与任职要求正文均为空，信息不足，不推断岗位业务（AC-08）",
        }

    # 角色分类：先看内容
    if sales_hits and not credit_hits:
        role, role_reason = "sales", f"正文包含销售特征词 {sales_hits[:3]}，即使标题含产品也归为销售（AC-07）"
    elif sales_hits and credit_hits and len(sales_hits) >= 2 and not product_hits and not eng_hits:
        role, role_reason = "sales", f"正文以销售内容为主（{sales_hits[:3]}），信贷词仅作背景"
    elif algo_hits and eng_hits:
        role, role_reason = "algorithm_data", f"职责同时包含算法与工程特征（{algo_hits[:2]}）"
    elif algo_hits:
        role, role_reason = "algorithm_data", f"职责包含算法/数据特征（{algo_hits[:2]}）"
    elif product_hits and eng_hits:
        # 产品岗描述系统协作但主体是产品
        role, role_reason = "product", f"职责包含产品设计特征（{product_hits[:2]}），研发词为协作背景"
    elif product_hits:
        role, role_reason = "product", f"职责包含产品设计特征（{product_hits[:2]}）"
    elif eng_hits:
        role, role_reason = "engineering", f"职责包含研发特征（{eng_hits[:2]}）"
    else:
        role, role_reason = "unknown", "标题与职责均无明确产品/研发/算法特征，标记待判断，不自动补全（AC-06）"
    reasons.append(role_reason)

    # 相关性
    if credit_hits:
        rel, rel_reason = "direct", f"职责/要求包含信贷业务词（{credit_hits[:3]}）"
    elif irrelevant_hits:
        rel, rel_reason = "irrelevant", f"职责为非信贷业务（{irrelevant_hits[:2]}）且无信贷关联"
    elif adjacent_hits:
        rel, rel_reason = "adjacent", f"职责与金融科技相邻（{adjacent_hits[:2]}），无明确信贷业务对象"
    else:
        rel, rel_reason = "unknown", "无明确业务领域信息，相关性待判断"
    reasons.append(rel_reason)

    return {"role_class": role, "relevance": rel, "reason": "；".join(reasons)}


def prescreen_keep(role_class: str, relevance: str) -> tuple[bool, str]:
    """第一阶段预筛（任务书 3.3）：明显不相关可排除；有歧义则读取详情再判断。"""
    if role_class == "sales" and relevance == "irrelevant":
        return False, "销售岗且与消费信贷无关，预筛排除"
    if relevance == "irrelevant":
        return False, "与消费信贷不相关，预筛排除"
    return True, "进入详情读取队列"


# ---------------------------------------------------------------------------
# 经验层级（设计 D11 / C4）：确定性规则，供路径一过滤与标注
# ---------------------------------------------------------------------------

_EXPERT_PATTERNS = ["10年", "十年", "专家", "资深专家", "负责人", "总监", "首席", "vp", "cto"]
_SENIOR_PATTERNS = ["5年", "五年", "8年", "高级", "资深", "leader", "团队负责", "带团队"]
_MID_PATTERNS = ["3年", "三年", "中级", "3-5年", "3到5年"]
_JUNIOR_PATTERNS = ["1年", "一年", "2年", "初级", "助理", "1-3年", "1到3年"]
_INTERN_PATTERNS = ["实习", "应届", "校招", "管培生"]


def classify_experience_level(title: str, requirements: str) -> tuple[str, str]:
    """按标题+任职要求推断经验层级。

    返回 ("intern"|"junior"|"mid"|"senior"|"expert"|"unknown", reason)。
    规则确定性，不调用模型；无法判断返回 unknown。
    """
    text = f"{title or ''}\n{requirements or ''}".lower()

    def hit(patterns: list[str]) -> list[str]:
        return [p for p in patterns if p in text]

    if hit(_INTERN_PATTERNS):
        return "intern", "标题/要求含实习、应届、校招等词"
    if hit(_EXPERT_PATTERNS):
        return "expert", "标题/要求含专家、负责人、总监或 10 年以上经验等词"
    if hit(_SENIOR_PATTERNS):
        return "senior", "标题/要求含高级、资深或 5 年以上经验等词"
    if hit(_MID_PATTERNS):
        return "mid", "标题/要求含中级或 3 年以上经验等词"
    if hit(_JUNIOR_PATTERNS):
        return "junior", "标题/要求含初级、助理或 1~2 年经验等词"
    return "unknown", "标题与要求未明确经验层级，不做推断"
