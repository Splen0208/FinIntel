"""finintel schemas：统一枚举、行业分类字典与严格校验模型。

对应任务书 2.2/2.3/2.4：内部枚举必须统一，各模块不得自行发明同义取值。
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# 枚举（任务书 2.4）
# ---------------------------------------------------------------------------

class RoleClass(str, Enum):
    product = "product"
    engineering = "engineering"
    algorithm_data = "algorithm_data"
    sales = "sales"
    other = "other"
    unknown = "unknown"

class Relevance(str, Enum):
    direct = "direct"
    adjacent = "adjacent"
    irrelevant = "irrelevant"
    unknown = "unknown"

class ActionType(str, Enum):
    build = "build"
    optimize = "optimize"
    operate = "operate"
    maintain = "maintain"
    explore = "explore"
    unknown = "unknown"

class ResultStatus(str, Enum):
    """模型可提交的分析结果状态。"""
    findings_found = "findings_found"
    insufficient_information = "insufficient_information"
    excluded = "excluded"
    needs_review = "needs_review"

class AnalysisStatus(str, Enum):
    """系统管理的 analysis.status，与 result_status 分离。"""
    pending = "pending"
    partial = "partial"
    validated = "validated"
    analysis_failed = "analysis_failed"

class Section(str, Enum):
    responsibility = "responsibility"
    requirement = "requirement"
    background = "background"
    unknown = "unknown"

class DetailStatus(str, Enum):
    complete = "complete"
    missing = "missing"
    partial = "partial"
    failed = "failed"
    too_large = "too_large"

class SourceStatus(str, Enum):
    open = "open"
    closed = "closed"
    unknown = "unknown"

class ReviewStatus(str, Enum):
    unreviewed = "unreviewed"
    confirmed = "confirmed"
    corrected = "corrected"
    excluded = "excluded"

class RunState(str, Enum):
    planned = "planned"
    collecting = "collecting"
    ready_for_analysis = "ready_for_analysis"
    analyzing = "analyzing"
    completed = "completed"
    partial = "partial"
    completed_partial = "completed_partial"
    auth_required = "auth_required"
    risk_stopped = "risk_stopped"
    rate_limited = "rate_limited"
    budget_stopped = "budget_stopped"
    stopped = "stopped"
    failed = "failed"

COLLECTING_STATES = {RunState.planned, RunState.collecting}
BLOCKING_STATES = {RunState.auth_required, RunState.risk_stopped, RunState.rate_limited}

# ---------------------------------------------------------------------------
# 行业分类字典 taxonomy_v1（任务书 2.3）
# ---------------------------------------------------------------------------

TAXONOMY_VERSION = "taxonomy_v1"

TAXONOMY = {
    "acquisition_channel": "获客与渠道",
    "credit_risk_pricing": "授信与定价",
    "borrowing_conversion": "借款与用信",
    "billing_repayment": "账单与还款",
    "postloan_service": "贷后与客户服务",
    "funding_settlement": "资金与清结算",
    "b2b_technology": "对外科技服务",
    "overseas_credit": "海外信贷",
    "data_platform": "业务数据与公共平台",
    "other": "其他/待分类",
}

AUXILIARY_TAGS = [
    "AI", "智能体", "支付", "反欺诈", "客户经营", "信贷核心",
    "小微客群", "存量用户", "新客户", "国内", "海外", "内部使用", "对外服务",
]

ROLE_CLASS_LABELS = {
    "product": "产品", "engineering": "研发", "algorithm_data": "算法/数据研发",
    "sales": "销售", "other": "其他", "unknown": "待判断",
}
RELEVANCE_LABELS = {
    "direct": "直接相关", "adjacent": "相邻业务", "irrelevant": "不相关", "unknown": "待判断",
}
ACTION_TYPE_LABELS = {
    "build": "建设", "optimize": "优化", "operate": "运营", "maintain": "维护",
    "explore": "研究探索", "unknown": "未明确",
}
RESULT_STATUS_LABELS = {
    "findings_found": "发现事项", "insufficient_information": "信息不足",
    "excluded": "排除", "needs_review": "待复核",
}
DETAIL_STATUS_LABELS = {
    "complete": "完整", "missing": "缺失", "partial": "部分",
    "failed": "失败", "too_large": "超长",
}
RUN_STATE_LABELS = {
    "planned": "已计划", "collecting": "采集中", "ready_for_analysis": "待分析",
    "analyzing": "分析中", "completed": "已完成", "partial": "部分完成",
    "completed_partial": "完成(部分)", "auth_required": "需登录",
    "risk_stopped": "风控停止", "rate_limited": "限流停止",
    "budget_stopped": "预算耗尽", "stopped": "已停止", "failed": "失败",
}
SUPPORT_LEVEL_LABELS = {
    "single": "单岗线索", "multi": "多岗一致", "unverified": "待核实",
}
REVIEW_STATUS_LABELS = {
    # 「待核对」与需求 §6/§11、前端 simple.js 统一（原「未复核」造成两个事实源，已收敛）
    "unreviewed": "待核对", "confirmed": "已确认", "corrected": "已修正", "excluded": "已排除",
}

# ---------------------------------------------------------------------------
# 错误码（任务书第 5 节）
# ---------------------------------------------------------------------------

ERROR_CODES = {
    "DEPENDENCY_MISSING", "MCP_PROTOCOL_ERROR", "AUTH_REQUIRED", "AUTH_EXPIRED",
    "ACCOUNT_RISK", "ENVIRONMENT_RISK", "RATE_LIMITED", "UPSTREAM_TIMEOUT",
    "UPSTREAM_CONTRACT_CHANGED", "DETAIL_MISSING", "BUDGET_EXHAUSTED",
    "INVALID_ANALYSIS", "INVALID_EVIDENCE", "VERSION_CONFLICT",
    "TOOL_NOT_ALLOWED", "OUTPUT_WRITE_FAILED",
    "RESUME_UNPARSEABLE", "RESUME_TOO_LARGE",
    # 简历文件解析（《简历解析实施指令》§7）
    "FILE_UNSUPPORTED", "FILE_TOO_LARGE", "PARSE_FAILED", "NEEDS_OCR", "TEXT_INCOMPLETE",
    # 真实模型调用（§4.1 / §7）
    "AI_NOT_CONFIGURED", "AI_TIMEOUT", "AI_OUTPUT_INVALID",
}

# ---------------------------------------------------------------------------
# 严格分析提交模型（任务书 3.5）
# ---------------------------------------------------------------------------

MAX_BUSINESS_TEXT_CHARS = 100_000
MAX_FINDINGS_PER_SNAPSHOT = 30
MAX_EVIDENCE_PER_FINDING = 5
ANALYSIS_CHUNK_CHARS = 12_000

class EvidenceItem(BaseModel):
    section: Section
    quote: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @field_validator("end")
    @classmethod
    def end_gt_start(cls, v: int, info):
        start = info.data.get("start")
        if start is not None and v <= start:
            raise ValueError("end 必须大于 start（半开区间 [start,end)）")
        return v

class FindingItem(BaseModel):
    statement: str = Field(min_length=1, max_length=300)
    business_domain: str
    action_type: ActionType
    audience: Optional[str] = Field(default=None, max_length=100)
    market: Optional[str] = Field(default=None, max_length=100)
    product_names: list[str] = Field(default_factory=list, max_length=10)
    interpretation: Optional[str] = Field(default=None, max_length=1000)
    limitations: list[str] = Field(min_length=1, max_length=10)
    evidence: list[EvidenceItem] = Field(min_length=1, max_length=MAX_EVIDENCE_PER_FINDING)

    @field_validator("business_domain")
    @classmethod
    def domain_in_taxonomy(cls, v: str):
        if v not in TAXONOMY:
            raise ValueError(f"business_domain 必须是 {TAXONOMY_VERSION} 字典中的取值：{list(TAXONOMY)}")
        return v

    @field_validator("product_names")
    @classmethod
    def names_short(cls, v: list[str]):
        for n in v:
            if not n or len(n) > 100:
                raise ValueError("product_names 每项需为 1~100 字符")
        return v

class AnalysisItem(BaseModel):
    snapshot_id: str = Field(min_length=1)
    role_class: RoleClass
    relevance: Relevance
    classification_reason: str = Field(min_length=1, max_length=2000)
    result_status: ResultStatus
    findings: list[FindingItem] = Field(default_factory=list, max_length=MAX_FINDINGS_PER_SNAPSHOT)

class SubmitPayload(BaseModel):
    batch_token: str
    submission_id: str = Field(min_length=8, max_length=64)
    model_label: str = Field(min_length=1, max_length=100)
    analyses: list[AnalysisItem] = Field(min_length=1, max_length=5)

# ---------------------------------------------------------------------------
# 运行配置模型（任务书 2.5 预算）
# ---------------------------------------------------------------------------

DEFAULT_BUDGET = {
    "source": "zhipin",              # zhipin（默认真实）| demo（主动演示）
    "queries": [],                    # 显式查询词
    "cities": [],
    "company_watchlist": [],
    "max_search_calls": 6,
    "max_detail_calls": 20,
    "call_timeout_seconds": 120,
    "max_transient_retries": 1,
    "details_per_call": 3,
    "analysis_batch_size": 5,
    "analysis_chunk_chars": ANALYSIS_CHUNK_CHARS,
}

BUDGET_UPPER_BOUNDS = {
    "max_search_calls": 20,
    "max_detail_calls": 60,
    "call_timeout_seconds": 300,
    "max_transient_retries": 2,
    "details_per_call": 3,
    "analysis_batch_size": 5,
}

SUGGESTED_QUERIES = [
    "消费金融 产品经理", "信贷 产品经理", "信贷 研发",
    "贷款系统 Java", "资金清结算 产品", "贷后 平台",
]

# ---------------------------------------------------------------------------
# 证据文本规范化（任务书 2.4：固定转换规则，生成后不可修改）
# ---------------------------------------------------------------------------

MAPPING_VERSION = "evidence_map_v1"

_PHONE_RE = re.compile(r"1[3-9]\d{9}")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_BLANK_RE = re.compile(r"[ \t]+\n")
_MULTI_NL = re.compile(r"\n{3,}")

def normalize_evidence_text(raw: str) -> str:
    """统一换行、去招聘者电话/邮箱等非业务个人联系信息。固定规则，不可修改。"""
    if not raw:
        return ""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _PHONE_RE.sub("[已移除联系方式]", text)
    text = _EMAIL_RE.sub("[已移除联系方式]", text)
    text = _BLANK_RE.sub("\n", text)
    text = _MULTI_NL.sub("\n\n", text)
    return text.strip()

def content_hash_fields(title: str, company_name_raw: str, city: Optional[str],
                        responsibilities: Optional[str], requirements: Optional[str]) -> str:
    """对选定规范化字段生成内容哈希；字段集与规范化规则版本固定。"""
    import hashlib
    payload = "\x1f".join([
        (title or "").strip(),
        (company_name_raw or "").strip(),
        (city or "").strip() or "-",
        normalize_evidence_text(responsibilities or ""),
        normalize_evidence_text(requirements or ""),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

def identity_key_for(source: str, source_job_id: Optional[str], company_name_raw: str,
                     title: str, city: Optional[str], content_hash: str) -> str:
    """无稳定岗位 ID 时的临时标识（身份不确定）。"""
    if source_job_id:
        return f"sid:{source_job_id}"
    import hashlib
    payload = "\x1f".join([
        (company_name_raw or "").strip(), (title or "").strip(),
        (city or "").strip() or "-", content_hash,
    ])
    return "fingerprint:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------------
# 求职者模式：新增枚举（设计 §4.1）
# ---------------------------------------------------------------------------

class UsageMode(str, Enum):
    research = "research"
    jobseeker = "jobseeker"


class MatchStatus(str, Enum):
    direct_evidence = "direct_evidence"
    transferable = "transferable"
    not_in_resume = "not_in_resume"
    confirmed_gap = "confirmed_gap"
    insufficient = "insufficient"


class RequirementCategory(str, Enum):
    core_task = "core_task"
    must_have = "must_have"
    nice_to_have = "nice_to_have"
    scenario_experience = "scenario_experience"
    ambiguous = "ambiguous"


class EvidenceSide(str, Enum):
    jd = "jd"
    resume = "resume"


class ParseStatus(str, Enum):
    pending = "pending"
    parsed = "parsed"
    partial = "partial"
    failed = "failed"
    too_large = "too_large"
    needs_ocr = "needs_ocr"


class ConfirmationStatus(str, Enum):
    """事实确认状态（《简历解析实施指令》§6.2）：解析成功/结构校验/本人确认是三种不同状态。"""
    unconfirmed = "unconfirmed"
    confirmed = "confirmed"
    needs_input = "needs_input"
    omitted = "omitted"


class FocusCategory(str, Enum):
    business_scenario = "business_scenario"
    project_contribution = "project_contribution"
    decision_tradeoff = "decision_tradeoff"
    result_metric = "result_metric"
    failure_improvement = "failure_improvement"
    transfer_ability = "transfer_ability"


class Learnability(str, Enum):
    short_term_learnable = "short_term_learnable"
    needs_expression_practice = "needs_expression_practice"
    real_project_required = "real_project_required"


class ExperienceLevel(str, Enum):
    intern = "intern"
    junior = "junior"
    mid = "mid"
    senior = "senior"
    expert = "expert"
    unknown = "unknown"


class CapabilityType(str, Enum):
    business_knowledge = "business_knowledge"
    skill = "skill"
    project_experience = "project_experience"


class FrequencyClass(str, Enum):
    common_in_sample = "common_in_sample"
    specific_to_few = "specific_to_few"
    insufficient_info = "insufficient_info"


class PrepStatus(str, Enum):
    planned = "planned"
    in_progress = "in_progress"
    done = "done"


class SessionStatus(str, Enum):
    draft = "draft"
    jd_ready = "jd_ready"
    resume_ready = "resume_ready"
    matched = "matched"
    focus_ready = "focus_ready"
    plan_ready = "plan_ready"
    failed = "failed"


class PayloadKind(str, Enum):
    company_finding = "company_finding"
    jd_match = "jd_match"
    prep_plan = "prep_plan"


# ---------------------------------------------------------------------------
# 求职者模式：中文标签（由 /api/health.labels 下发）
# ---------------------------------------------------------------------------

USAGE_MODE_LABELS = {"research": "行业研究者", "jobseeker": "求职者"}

MATCH_STATUS_LABELS = {
    "direct_evidence": "简历中有直接证据",
    "transferable": "经验可迁移（存在差异）",
    "not_in_resume": "简历未体现（需本人确认）",
    "confirmed_gap": "本人确认存在缺口",
    "insufficient": "信息不足无法判断",
}

REQUIREMENT_CATEGORY_LABELS = {
    "core_task": "核心工作任务", "must_have": "明确必备条件",
    "nice_to_have": "加分条件", "scenario_experience": "可能需要的业务场景经验",
    "ambiguous": "表述模糊需面试确认",
}

EVIDENCE_SIDE_LABELS = {"jd": "JD 原文", "resume": "简历原文"}

PARSE_STATUS_LABELS = {"pending": "待解析", "parsed": "已解析", "partial": "部分解析",
                       "failed": "解析失败", "too_large": "文本超长", "needs_ocr": "需 OCR（无文本层）"}

CONFIRMATION_STATUS_LABELS = {
    "unconfirmed": "待核对", "confirmed": "本人已确认",
    "needs_input": "信息缺失", "omitted": "已省略",
}

FOCUS_CATEGORY_LABELS = {
    "business_scenario": "业务场景理解", "project_contribution": "项目实际职责与个人贡献",
    "decision_tradeoff": "决策与方案取舍", "result_metric": "结果指标与验证",
    "failure_improvement": "异常失败经历与改进", "transfer_ability": "迁移能力",
}

LEARNABILITY_LABELS = {
    "short_term_learnable": "可短期学习补充",
    "needs_expression_practice": "已有经历需讲清楚",
    "real_project_required": "需真实项目经验（无法临时替代）",
}

EXPERIENCE_LEVEL_LABELS = {"intern": "实习", "junior": "初级", "mid": "中级",
                           "senior": "高级", "expert": "专家/负责人", "unknown": "未明确"}

CAPABILITY_TYPE_LABELS = {"business_knowledge": "业务知识", "skill": "技能",
                          "project_experience": "项目经验"}

FREQUENCY_CLASS_LABELS = {"common_in_sample": "样本中常见", "specific_to_few": "少数岗位特定要求",
                          "insufficient_info": "信息不足"}

PREP_STATUS_LABELS = {"planned": "待开始", "in_progress": "进行中", "done": "已完成"}

SESSION_STATUS_LABELS = {"draft": "草稿", "jd_ready": "JD 已就绪", "resume_ready": "简历已就绪",
                         "matched": "匹配完成", "focus_ready": "重点已生成",
                         "plan_ready": "计划已生成", "failed": "失败（可重试）"}

# ---------------------------------------------------------------------------
# 求职者模式：长度/数量常量
# ---------------------------------------------------------------------------

MAX_RESUME_TEXT_CHARS = 60_000      # 超限 → parse_status='too_large'
MAX_RESUME_FILE_BYTES = 10 * 1024 * 1024   # 10 MiB 上传上限
MAX_PDF_PAGES = 30                          # PDF 页数上限
MAX_DOCX_UNZIP_BYTES = 50 * 1024 * 1024     # DOCX 解压体积上限（防 zip 炸弹）
MAX_DOCX_ENTRIES = 2000                     # DOCX 解包条目数上限
ALLOWED_RESUME_EXTS = {".pdf", ".docx", ".txt", ".md"}  # 首版支持格式；不支持 .doc/图片/压缩包/宏文档
MAX_JD_TEXT_CHARS = MAX_BUSINESS_TEXT_CHARS   # 复用既有上限
MAX_REQUIREMENTS = 30
MAX_CLARIFICATIONS = 5              # F-08 硬上限
MAX_QUOTES_PER_OWNER = 6
MAX_FOCUS_ITEMS = 18
MAX_PREP_TASKS = 24
MAX_ACTIONS_PER_TASK = 10
MAX_RESOURCES_PER_TASK = 5
MATCH_PROMPT_VERSION = "match_v1"
PREP_PROMPT_VERSION = "prep_plan_v1"
MATCH_SCHEMA_VERSION = "match_schema_v1"
PREP_SCHEMA_VERSION = "prep_schema_v1"
RULE_MATCHER_LABEL = "rule-matcher-demo-v1"

# ---------------------------------------------------------------------------
# 禁用措辞（R1/R8，命中即拒绝入库）
# ---------------------------------------------------------------------------

BANNED_PHRASES = [
    r"一定会问",
    r"面试真题",
    r"该公司真实面试题",
    r"该公司真题",
    r"保证通过",
    r"录用概率",
    r"匹配度\s*\d+%",
    r"综合匹配分值",
    r"综合评分",
    r"\d+\s*分[（(]录用|匹配[)）]",
]

_BANNED_RE = re.compile("|".join(BANNED_PHRASES))


def has_banned_phrase(text: str) -> bool:
    """文本是否命中禁用措辞黑名单。"""
    if not text:
        return False
    return bool(_BANNED_RE.search(text))


# ---------------------------------------------------------------------------
# 求职者模式：新增 Pydantic 模型（设计 §4.2，本批次 T01~T03 需要）
# ---------------------------------------------------------------------------

class QuoteRef(BaseModel):
    side: EvidenceSide
    section: str = ""
    quote: str = Field(min_length=1, max_length=2000)
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @field_validator("end")
    @classmethod
    def end_gt_start(cls, v: int, info):
        start = info.data.get("start")
        if start is not None and v <= start:
            raise ValueError("end 必须大于 start（半开区间 [start,end)）")
        return v


class RequirementItem(BaseModel):
    ref: str = Field(min_length=1, max_length=32)
    category: RequirementCategory
    text: str = Field(min_length=1, max_length=500)
    judgement_note: str = Field(min_length=1, max_length=1000)
    quotes: list[QuoteRef] = Field(default_factory=list, max_length=MAX_QUOTES_PER_OWNER)


class MatchRowItem(BaseModel):
    requirement_ref: str = Field(min_length=1, max_length=32)
    match_status: MatchStatus
    jd_note: str = Field(min_length=1, max_length=1000)
    resume_note: str = Field(min_length=1, max_length=1000)
    reasoning: str = Field(min_length=1, max_length=2000)
    uncertainties: list[str] = Field(default_factory=list, max_length=10)
    prep_advice: str = Field(min_length=1, max_length=1000)
    quotes: list[QuoteRef] = Field(default_factory=list, max_length=MAX_QUOTES_PER_OWNER)


class ClarificationItem(BaseModel):
    question: str = Field(min_length=1, max_length=300)
    why_asked: str = Field(min_length=1, max_length=500)
    requirement_refs: list[str] = Field(default_factory=list, max_length=5)


class MatchSubmitPayload(BaseModel):
    batch_token: str
    submission_id: str = Field(min_length=8, max_length=64)
    model_label: str = Field(min_length=1, max_length=100)
    requirements: list[RequirementItem] = Field(min_length=1, max_length=MAX_REQUIREMENTS)
    match_rows: list[MatchRowItem] = Field(default_factory=list, max_length=MAX_REQUIREMENTS)
    clarifications: list[ClarificationItem] = Field(default_factory=list, max_length=MAX_CLARIFICATIONS)


class ResumeItemIn(BaseModel):
    id: Optional[str] = None
    kind: str = Field(pattern="^(education|experience|project|skill|summary|other)$")
    title: str = Field(min_length=1, max_length=200)
    org: Optional[str] = Field(default=None, max_length=200)
    period_raw: Optional[str] = Field(default=None, max_length=100)
    start: Optional[int] = Field(default=None, ge=0)
    end: Optional[int] = Field(default=None, ge=0)
    confidence: float = 1.0


# ---- 准备包 payload（payload_kind='prep_plan'）----

class FocusQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=300)
    followups: list[str] = Field(default_factory=list, max_length=5)


class LinkRef(BaseModel):
    side: str = Field(pattern="^(jd|resume|requirement)$")
    ref_type: str = ""                       # target / resume_item / requirement
    ref_id: Optional[str] = None
    quote: Optional[QuoteRef] = None
    note: str = ""


class FocusItem(BaseModel):
    category: FocusCategory
    title: str = Field(min_length=1, max_length=300)
    why_asked: str = Field(min_length=1, max_length=1000)      # 为什么可能被问
    questions: list[FocusQuestion] = Field(min_length=1, max_length=6)
    materials: list[str] = Field(min_length=1, max_length=10)  # 应准备的真实材料
    uncertainties: list[str] = Field(min_length=1, max_length=10)  # 非空（R8）
    links: list[LinkRef] = Field(min_length=1, max_length=6)   # 至少一条依据（G2）


class PrepResourceItem(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    url: Optional[str] = None
    publisher: Optional[str] = None
    verified_retrievable: int = 0
    note: Optional[str] = None


class PrepTaskItem(BaseModel):
    priority: int = Field(ge=1, le=99)
    purpose: str = Field(min_length=1, max_length=500)
    requirement_refs: list[str] = Field(default_factory=list, max_length=5)
    actions: list[str] = Field(min_length=1, max_length=MAX_ACTIONS_PER_TASK)
    suggested_hours: float = Field(ge=0, le=200)
    deliverable: str = Field(min_length=1, max_length=500)
    self_check: str = Field(min_length=1, max_length=500)
    learnability: Learnability
    is_practice_project: bool = False
    resources: list[PrepResourceItem] = Field(default_factory=list, max_length=MAX_RESOURCES_PER_TASK)


class PrepSubmitPayload(BaseModel):
    batch_token: str
    submission_id: str = Field(min_length=8, max_length=64)
    model_label: str = Field(min_length=1, max_length=100)
    focus_items: list[FocusItem] = Field(min_length=1, max_length=MAX_FOCUS_ITEMS)
    prep_tasks: list[PrepTaskItem] = Field(min_length=1, max_length=MAX_PREP_TASKS)
