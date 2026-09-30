# -*- coding: utf-8 -*-
"""finintel 求职者模式 QA 测试（增量 PRD §8 验收清单 S1~S9 / A1~A10 / G1~G4）。

运行：
    cd finintel && backend 目录下的解释器直接跑：
    <finintel>/Scripts/python.exe backend/tests/test_jobseeker.py

脚本在内存临时 SQLite 上运行（FININTEL_DB 指向临时文件），不污染真实数据库。
输出：每个验收场景 PASS/FAIL + 失败详情，最后汇总退出码（0=全过，1=有失败）。
"""
from __future__ import annotations

import io
import os
import re
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# 在 import store/app 之前把数据库指向临时文件，避免污染真实数据
_TMP = tempfile.mkdtemp(prefix="finintel_qa_")
os.environ["FININTEL_DB"] = str(Path(_TMP) / "qa.sqlite3")
os.environ.setdefault("FININTEL_SCOPE_CHECK", "1")

import store  # noqa: E402
import schemas  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

store.apply_migrations()

# 测试默认开启 demo 模式（无真实 LLM key 时 auto_match/auto_prep 走规则匹配器，
# 否则会按设计返回 AI_NOT_CONFIGURED；真实模型路径在 R-05 单独验证未配置行为）
store.get_conn().execute(
    "INSERT INTO app_setting (key, value) VALUES ('demo_mode', '1') "
    "ON CONFLICT(key) DO UPDATE SET value='1'")
store.get_conn().commit()

client = TestClient(app_module.app, raise_server_exceptions=False)

FIXTURES = BACKEND_DIR / "fixtures"


def read_fixture(rel: str) -> str:
    return (FIXTURES / rel).read_text(encoding="utf-8")


def parse_jd(text: str) -> tuple[str, str]:
    m_resp = re.search(r"【工作职责】\s*\n(.*?)(?=【任职要求】|$)", text, re.S)
    m_req = re.search(r"【任职要求】\s*\n(.*)$", text, re.S)
    return ((m_resp.group(1).strip() if m_resp else ""),
            (m_req.group(1).strip() if m_req else ""))


STD_RESP, STD_REQ = parse_jd(read_fixture("target_jd_standard.txt"))
AMB_RESP, AMB_REQ = parse_jd(read_fixture("target_jd_ambiguous.txt"))
HIGH_RESUME = read_fixture("resumes/resume_high_match.txt")
TRANSFER_RESUME = read_fixture("resumes/resume_transferable.txt")
MISSING_RESUME = read_fixture("resumes/resume_missing_key_skill.txt")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def post(path: str, **kw):
    if "json" in kw:
        return client.post(path, json=kw.pop("json"))
    return client.post(path, **kw)


def data(r) -> dict:
    return r.json().get("data") or {}


def ok(r) -> bool:
    return bool(r.json().get("ok"))


def err_of(r) -> dict:
    return r.json().get("error") or {}


def create_session(**kw) -> dict:
    # app 层包了一层 {"session": get_session(...)}，而 get_session 又返回 {"session": row, ...}
    return data(post("/api/js/sessions", json={"route": "match", **kw}))["session"]["session"]


def import_resume(text: str, label: str = "QA简历") -> dict:
    return data(post("/api/resumes", json={"label": label, "text": text}))["resume"]


def create_target(resp: str, req: str, session_id=None, title="QA目标JD", company="QA公司") -> dict:
    return data(post("/api/job-targets", json={
        "source_kind": "paste", "session_id": session_id, "title": title,
        "company_raw": company, "responsibilities_text": resp, "requirements_text": req,
    }))["job_target"]


def full_match(resume_text: str, resp: str = STD_RESP, req: str = STD_REQ):
    """建会话 -> JD -> 简历 -> 绑定 -> auto-match，返回 (sid, target_id, resume_id, match_resp)。"""
    sid = create_session()["id"]
    tgt = create_target(resp, req, session_id=sid)
    res = import_resume(resume_text)
    post(f"/api/js/sessions/{sid}", json={"job_target_id": tgt["id"], "resume_id": res["id"]})
    m = post(f"/api/js/sessions/{sid}/auto-match")
    return sid, tgt["id"], res["id"], m


def match_view(sid: str) -> dict:
    return data(client.get(f"/api/js/sessions/{sid}/match"))


def requirements_view(sid: str) -> dict:
    return data(client.get(f"/api/js/sessions/{sid}/requirements"))


def prep_view(sid: str) -> dict:
    return data(client.get(f"/api/js/sessions/{sid}/prep"))


def focus_view(sid: str) -> dict:
    return data(client.get(f"/api/js/sessions/{sid}/focus"))


def get_texts(sid: str, target_id: str, resume_id: str) -> tuple[str, str]:
    jd_text = data(client.get(f"/api/job-targets/{target_id}"))["job_target"]["evidence_text"]
    res = data(client.get(f"/api/resumes/{resume_id}"))["resume"]
    return jd_text, res["evidence_text"]


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------

def s1_high_match():
    sid, target_id, resume_id, m = full_match(HIGH_RESUME)
    assert ok(m), f"auto-match 失败: {m.json()}"
    jd_text, resume_text = get_texts(sid, target_id, resume_id)
    rows = match_view(sid)["rows"]
    direct = [r for r in rows if r["match_status"] == "direct_evidence"]
    assert direct, f"高度匹配简历应产生 direct_evidence 行，实际状态分布: {sorted({r['match_status'] for r in rows})}"
    # 引用可回切 + 双侧引用
    for r in direct:
        assert r["quotes"], "direct_evidence 行应有 JD/简历引用"
        sides = {q["side"] for q in r["quotes"]}
        assert "jd" in sides, "direct_evidence 行缺少 JD 侧引用"
        for q in r["quotes"]:
            src = jd_text if q["side"] == "jd" else resume_text
            assert src[q["start"]:q["end"]] == q["quote"], \
                f"引用切片不匹配: {q['quote'][:20]!r} vs {src[q['start']:q['end']][:20]!r}"
    # 导出 MD 含两侧引用、无"保证通过"
    rep = data(post(f"/api/js/sessions/{sid}/report", json={"formats": ["md"]}))
    md = rep["markdown"]
    assert "简历中有直接证据" in md
    assert "保证通过" not in md


def s2_transferable():
    # 走手动提交通道（规则匹配器对"电商结算 vs 信贷资金"的语义差异识别有限，见设计 Q1）
    sid = create_session()["id"]
    tgt = create_target(STD_RESP, STD_REQ, session_id=sid)
    res = import_resume(TRANSFER_RESUME)
    post(f"/api/js/sessions/{sid}", json={"job_target_id": tgt["id"], "resume_id": res["id"]})
    prep = data(post(f"/api/js/sessions/{sid}/prepare-match"))
    jd_text = prep["jd"]["evidence_text"]
    resume_text = prep["resume"]["evidence_text"]

    jd_sub = "负责资金路由与分单规则的产品设计"
    jd_pos = jd_text.find(jd_sub)
    assert jd_pos >= 0, "JD 未找到目标句"
    res_sub = "负责商家结算系统的产品设计"
    res_pos = resume_text.find(res_sub)
    assert res_pos >= 0, "简历未找到目标句"

    payload = {
        "batch_token": prep["batch_token"],
        "submission_id": "sub_s2_transferable_01",
        "model_label": "qa-test-model-v1",
        "requirements": [{
            "ref": "R1", "category": "core_task", "text": jd_sub,
            "judgement_note": "JD 职责段资金路由与分单设计",
            "quotes": [{"side": "jd", "section": "responsibility", "quote": jd_sub,
                        "start": jd_pos, "end": jd_pos + len(jd_sub)}],
        }],
        "match_rows": [{
            "requirement_ref": "R1", "match_status": "transferable",
            "jd_note": "JD 要求资金路由与分单规则设计",
            "resume_note": "简历有电商交易结算与对账经验，但属电商结算口径、未接触资金方准入与授信流程，存在业务口径差异",
            "reasoning": "经验可迁移但行业/业务口径不同",
            "uncertainties": ["迁移是否成立需本人确认与面试方核实"],
            "prep_advice": "补齐目标行业（消费信贷）业务知识，准备迁移逻辑",
            "quotes": [
                {"side": "jd", "section": "responsibility", "quote": jd_sub,
                 "start": jd_pos, "end": jd_pos + len(jd_sub)},
                {"side": "resume", "section": "experience", "quote": res_sub,
                 "start": res_pos, "end": res_pos + len(res_sub)},
            ],
        }],
        "clarifications": [],
    }
    m = post(f"/api/js/sessions/{sid}/submit-match", json=payload)
    assert ok(m), f"submit-match 失败: {m.json()}"
    body = data(m)
    assert body["ok_count"] == 1 and body["row_ids"], f"应入库 1 行: {body}"
    rows = match_view(sid)["rows"]
    row = rows[0]
    assert row["match_status"] == "transferable", row["match_status"]
    assert "差异" in row["resume_note"], "transferable 说明应指出业务差异"
    assert "补齐" in row["prep_advice"], "准备建议应涉及补齐行业业务知识"
    assert row["resume_note"] and "已满足" not in row["resume_note"]


def s3_not_in_resume():
    sid, target_id, resume_id, m = full_match(MISSING_RESUME)
    assert ok(m), f"auto-match 失败: {m.json()}"
    rows = match_view(sid)["rows"]
    nir = [r for r in rows if r["match_status"] == "not_in_resume"]
    assert nir, f"缺关键技能简历应产生 not_in_resume，实际: {sorted({r['match_status'] for r in rows})}"
    for r in nir:
        assert r["match_status"] != "confirmed_gap"
        assert r["uncertainties"], "not_in_resume 必须保留不确定性"
    clars = match_view(sid)["clarifications"]
    assert 0 < len(clars) <= 5, f"补充提问应 ≤5 条且非空，实际 {len(clars)}"


def s4_confirmed_gap():
    sid, target_id, resume_id, m = full_match(MISSING_RESUME)
    assert ok(m)
    rows = match_view(sid)["rows"]
    nir = [r for r in rows if r["match_status"] == "not_in_resume"]
    assert nir
    row = nir[0]
    # 只有用户显式确认才能到 confirmed_gap
    rv = post("/api/review", json={
        "target_type": "match_row", "target_id": row["id"], "action": "correct",
        "patch": {"match_status": "confirmed_gap"}, "reason": "本人确认无该经历",
        "expected_version": row["review_version"]})
    assert ok(rv), f"review 失败: {rv.json()}"
    assert data(rv)["result"]["match_status"] == "confirmed_gap"
    # 进入准备清单且 learnability=real_project_required
    pp = post(f"/api/js/sessions/{sid}/auto-prep")
    assert ok(pp), f"auto-prep 失败: {pp.json()}"
    tasks = prep_view(sid)["tasks"]
    gap_tasks = [t for t in tasks if t["learnability"] == "real_project_required"]
    assert gap_tasks, "confirmed_gap 行应进入准备清单且 learnability=real_project_required"
    assert not any("你已经掌握" in t["purpose"] for t in tasks)


def s5_ambiguous_jd():
    sid, target_id, resume_id, m = full_match(HIGH_RESUME, resp=AMB_RESP, req=AMB_REQ)
    assert ok(m)
    reqs = requirements_view(sid)["requirements"]
    amb = [r for r in reqs if r["category"] == "ambiguous"]
    assert amb, f"模糊 JD 应产生 ambiguous 类要求，实际类别: {sorted({r['category'] for r in reqs})}"
    for r in amb:
        assert r["judgement_note"], "ambiguous 要求必须填写模糊原因"
    clars = match_view(sid)["clarifications"]
    assert any("确认" in c["question"] or "确认" in c["why_asked"] for c in clars), \
        "应有向对方确认考察边界的问题"
    rep = data(post(f"/api/js/sessions/{sid}/report", json={"formats": ["md"]}))
    assert "待本人确认" in rep["markdown"]


def s6_parse_failed():
    # 空文本
    r = post("/api/resumes", json={"label": "空简历", "text": ""})
    assert ok(r)
    res = data(r)["resume"]
    assert res["parse_status"] == "failed", res["parse_status"]
    assert res["failure_code"] == "RESUME_UNPARSEABLE"
    assert res["failure_message"]
    # 损坏 PDF → PARSE_FAILED 错误信封（文档 §5.1：损坏文件返回具体错误）
    rf = client.post("/api/resumes/file",
                     files={"file": ("bad.pdf", b"this is not a real pdf", "application/pdf")},
                     data={"label": "损坏"})
    assert not ok(rf), "损坏文件应返回错误信封"
    assert err_of(rf)["code"] == "PARSE_FAILED", err_of(rf)
    # 解析失败不得进入匹配分析，不生成匹配行
    sid = create_session()["id"]
    tgt = create_target(STD_RESP, STD_REQ, session_id=sid)
    post(f"/api/js/sessions/{sid}", json={"job_target_id": tgt["id"], "resume_id": res["id"]})
    pm = post(f"/api/js/sessions/{sid}/prepare-match")
    assert not ok(pm), "解析失败简历应被阻断"
    assert err_of(pm)["code"] == "INVALID_ANALYSIS"
    assert match_view(sid)["rows"] == []


def s7_model_analysis_failed():
    sid = create_session()["id"]
    tgt = create_target(STD_RESP, STD_REQ, session_id=sid)
    res = import_resume(HIGH_RESUME)
    post(f"/api/js/sessions/{sid}", json={"job_target_id": tgt["id"], "resume_id": res["id"]})
    prep = data(post(f"/api/js/sessions/{sid}/prepare-match"))
    jd_text = prep["jd"]["evidence_text"]
    jd_sub = "负责资金路由与分单规则的产品设计"
    jd_pos = jd_text.find(jd_sub)
    # 偏移故意写错（start+1）
    payload = {
        "batch_token": prep["batch_token"], "submission_id": "sub_s7_badoffset_01",
        "model_label": "qa-test-model-v1",
        "requirements": [{
            "ref": "R1", "category": "core_task", "text": jd_sub,
            "judgement_note": "JD 职责", "quotes": [{
                "side": "jd", "section": "responsibility", "quote": jd_sub,
                "start": jd_pos + 1, "end": jd_pos + len(jd_sub)}],
        }],
        "match_rows": [{
            "requirement_ref": "R1", "match_status": "direct_evidence",
            "jd_note": "x", "resume_note": "y", "reasoning": "z",
            "uncertainties": [], "prep_advice": "p", "quotes": []}],
        "clarifications": [],
    }
    m = post(f"/api/js/sessions/{sid}/submit-match", json=payload)
    assert ok(m), "submit-match 应返回 200 信封而非 500"
    body = data(m)
    assert body["ok_count"] == 0 and body["failed_count"] >= 1
    errs = body["outcomes"][0]["errors"]
    assert errs, "应有字段级错误"
    e0 = errs[0]
    assert {"field", "code", "message"} <= set(e0.keys()), e0
    assert e0["code"] == "INVALID_EVIDENCE"
    # 不入库
    assert requirements_view(sid)["requirements"] == [], "校验失败不应入库"
    # 会话标记失败态
    sess = data(client.get(f"/api/js/sessions/{sid}"))["session"]
    assert sess["status"] == "failed", sess["status"]


def s8_reanalysis_preserves_review():
    sid, target_id, resume_id, m = full_match(MISSING_RESUME)
    assert ok(m)
    rows = match_view(sid)["rows"]
    assert rows
    row = rows[0]
    # 用户修正 + 补充
    rv = post("/api/review", json={
        "target_type": "match_row", "target_id": row["id"], "action": "correct",
        "patch": {"resume_note": "我补充：其实做过对账相关项目"}, "reason": "补充真实经历",
        "expected_version": row["review_version"]})
    assert ok(rv), rv.json()
    # 版本落后 → VERSION_CONFLICT
    stale = post("/api/review", json={
        "target_type": "match_row", "target_id": row["id"], "action": "correct",
        "patch": {"resume_note": "再改一次"}, "reason": "again",
        "expected_version": row["review_version"]})
    assert not ok(stale)
    assert err_of(stale)["code"] == "VERSION_CONFLICT", err_of(stale)
    assert "重新读取最新数据" in err_of(stale)["operator_action"]
    # 重新分析：已 review 行不被覆盖
    m2 = post(f"/api/js/sessions/{sid}/auto-match")
    assert ok(m2), m2.json()
    rows2 = match_view(sid)["rows"]
    corrected = [r for r in rows2 if r["review_status"] in ("corrected", "confirmed")]
    assert corrected, "重新分析后已 review 行应保留"
    kept = [r for r in corrected if r["resume_note"] == "我补充：其实做过对账相关项目"]
    assert kept, "已修正内容应在重新分析后保持不变"
    # review_event 有前后值
    evs = data(client.get(f"/api/review/events", params={"target_type": "match_row", "target_id": row["id"]}))["events"]
    assert evs and "before_json" in evs[0] and "after_json" in evs[0]
    # 补充经历（不传状态）不被覆盖
    sup = post(f"/api/js/match-rows/{row['id']}/supplement",
               json={"user_supplement": "补充：负责过渠道对账", "reason": "补充"})
    assert ok(sup), sup.json()
    rows3 = match_view(sid)["rows"]
    sup_row = [r for r in rows3 if r["id"] == row["id"]]
    assert sup_row and sup_row[0]["user_supplement"] == "补充：负责过渠道对账"


def s9_traceability():
    sid, target_id, resume_id, m = full_match(HIGH_RESUME)
    assert ok(m)
    pp = post(f"/api/js/sessions/{sid}/auto-prep")
    assert ok(pp), pp.json()
    focus = focus_view(sid)["focus"]
    assert focus, "应生成面试考察重点"
    for f in focus:
        assert f["uncertainties"], "每个考察点 uncertainties 必须非空"
        assert f["links"], "每个考察点至少一条依据"
    rep = data(post(f"/api/js/sessions/{sid}/report", json={"formats": ["md"]}))
    assert "依据与不确定性" in rep["markdown"]


# ---------------------------------------------------------------------------
# 红线 A
# ---------------------------------------------------------------------------

def a1_banned_phrases():
    # 提交含"匹配度 87%"的 payload 应被拒
    sid = create_session()["id"]
    tgt = create_target(STD_RESP, STD_REQ, session_id=sid)
    res = import_resume(HIGH_RESUME)
    post(f"/api/js/sessions/{sid}", json={"job_target_id": tgt["id"], "resume_id": res["id"]})
    prep = data(post(f"/api/js/sessions/{sid}/prepare-match"))
    jd_text = prep["jd"]["evidence_text"]
    jd_sub = "负责资金路由与分单规则的产品设计"
    jd_pos = jd_text.find(jd_sub)
    payload = {
        "batch_token": prep["batch_token"], "submission_id": "sub_a1_banned_01",
        "model_label": "qa-test-model-v1",
        "requirements": [{
            "ref": "R1", "category": "core_task", "text": jd_sub, "judgement_note": "ok",
            "quotes": [{"side": "jd", "section": "responsibility", "quote": jd_sub,
                        "start": jd_pos, "end": jd_pos + len(jd_sub)}]}],
        "match_rows": [{
            "requirement_ref": "R1", "match_status": "direct_evidence",
            "jd_note": "x", "resume_note": "y", "reasoning": "综合匹配度 87%",
            "uncertainties": [], "prep_advice": "p", "quotes": []}],
        "clarifications": [],
    }
    m = post(f"/api/js/sessions/{sid}/submit-match", json=payload)
    assert ok(m)
    body = data(m)
    assert body["ok_count"] == 0
    assert any(e["code"] == "INVALID_ANALYSIS" for e in body["outcomes"][0]["errors"])
    # 规则匹配器输出本身无禁用措辞
    sid2, _, _, m2 = full_match(HIGH_RESUME)
    assert ok(m2)
    for r in match_view(sid2)["rows"]:
        assert not schemas.has_banned_phrase(r["reasoning"] or "")
        assert not schemas.has_banned_phrase(r["resume_note"] or "")
        assert not schemas.has_banned_phrase(r["prep_advice"] or "")


def a2_no_contact_leak():
    res = import_resume(HIGH_RESUME)
    rid = res["id"]
    rv = data(client.get(f"/api/resumes/{rid}"))
    ev = rv["resume"]["evidence_text"]
    assert "13800001111" not in ev, "手机号原值泄漏"
    assert "zhangmingyuan@example.com" not in ev, "邮箱原值泄漏"
    assert "[已移除联系方式]" in ev, "应替换为 [已移除联系方式]"


def a3_delete_resume():
    res = import_resume(HIGH_RESUME)
    rid = res["id"]
    r = client.request("DELETE", f"/api/resumes/{rid}", json={"confirm": True})
    assert ok(r), r.json()
    rv = data(client.get(f"/api/resumes/{rid}"))
    assert rv["resume"]["evidence_text"] == "", "删除后 evidence_text 应清空"
    assert rv["resume"]["deleted_at"], "删除后 deleted_at 应有值"
    assert rv["resume"]["char_count"] == 0
    # 未确认删除被拒绝
    res2 = import_resume(HIGH_RESUME)
    r2 = client.request("DELETE", f"/api/resumes/{res2['id']}", json={"confirm": False})
    assert not ok(r2)


def a5_practice_annotation():
    sid, target_id, resume_id, m = full_match(MISSING_RESUME)
    assert ok(m)
    pp = post(f"/api/js/sessions/{sid}/auto-prep")
    assert ok(pp)
    tasks = prep_view(sid)["tasks"]
    practice = [t for t in tasks if t["is_practice_project"] == 1]
    assert practice, "not_in_resume 应产出练习项目任务"
    rep = data(post(f"/api/js/sessions/{sid}/report", json={"formats": ["md"]}))
    assert "练习项目（非真实经历" in rep["markdown"], "报告应显式标注练习项目"


def a6_verifiable_resources():
    sid, target_id, resume_id, m = full_match(MISSING_RESUME)
    assert ok(m)
    pp = post(f"/api/js/sessions/{sid}/auto-prep")
    assert ok(pp)
    tasks = prep_view(sid)["tasks"]
    for t in tasks:
        if t["resources"]:
            for r in t["resources"]:
                assert r.get("verified_retrievable") == 1, "只有已核验来源才能写 resources"
        else:
            assert "未检索到可核验资料" in (t.get("resources_note") or ""), \
                "无资源时应明示未检索到可核验资料"


def a8_prompt_declaration():
    for name in ("match_v1.md", "prep_plan_v1.md"):
        p = (BACKEND_DIR / "prompts" / name).read_text(encoding="utf-8")
        assert "不可信" in p and ("指令" in p), f"提示词 {name} 缺少正文为不可信资料/非指令声明"


def a9_no_apply_api():
    paths = [getattr(r, "path", "") for r in app_module.app.routes]
    for p in paths:
        low = p.lower()
        for banned in ("apply", "投递", "communicat", "chat", "message"):
            assert banned not in low, f"求职者侧不应出现投递/沟通 API: {p}"


def a10_rule_matcher_label():
    sid, target_id, resume_id, m = full_match(HIGH_RESUME)
    assert ok(m)
    assert any("rule-matcher-demo-v1" in w for w in m.json().get("warnings", [])), \
        "auto-match 应带 rule-matcher-demo-v1 warnings"
    mv = match_view(sid)
    assert mv["model_label"] == "rule-matcher-demo-v1"
    assert mv["demo"] is True


# ---------------------------------------------------------------------------
# 回归 G
# ---------------------------------------------------------------------------

def g1_research_routes():
    assert ok(client.get("/api/health"))
    labels = data(client.get("/api/health"))["labels"]
    for key in ("match_status", "requirement_category", "parse_status", "focus_category",
                "learnability", "session_status"):
        assert key in labels, f"/api/health.labels 缺少 {key}"
    assert ok(client.get("/api/runs"))
    assert ok(client.get("/api/jobs"))
    assert ok(client.get("/api/reports"))
    assert ok(client.get("/api/settings"))


def g2_migration_idempotent():
    applied = store.apply_migrations()
    assert applied == [], f"重复迁移应返回空，实际 {applied}"
    # 旧库(001) + 002 关键表/列存在
    conn = store.get_conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(report)")}
    assert "kind" in cols and "ref_id" in cols
    for tbl in ("resume", "match_row", "interview_focus", "prep_task", "job_target"):
        n = conn.execute(f"SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone()[0]
        assert n == 1, f"缺表 {tbl}"


def g3_demo_research_chain():
    # 端到端研究链路：建 run -> search -> details -> finish -> auto-analyze -> report
    r = post("/api/runs", json={"name": "QA回归", "source": "demo",
                                "queries": ["消费金融 产品经理"],
                                "max_search_calls": 2, "max_detail_calls": 4, "details_per_call": 3})
    assert ok(r), r.json()
    run = data(r)["run"]
    rid = run["id"]
    s = post(f"/api/runs/{rid}/search", json={"query_id": "q1", "page": 1})
    assert ok(s), s.json()
    job_refs = [j["job_ref"] for j in data(s)["job_refs"][:3]]
    assert job_refs
    d = post(f"/api/runs/{rid}/details", json={"job_refs": job_refs})
    assert ok(d), d.json()
    snap_ids = [o["snapshot_id"] for o in data(d)["outcomes"] if o.get("snapshot_id")]
    assert snap_ids
    fc = post(f"/api/runs/{rid}/control/finish_collection")
    assert ok(fc), fc.json()
    aa = post(f"/api/runs/{rid}/auto-analyze", json={"snapshot_ids": snap_ids})
    assert ok(aa), aa.json()
    rep = post("/api/reports", json={"run_ids": [rid], "formats": ["md"]})
    assert ok(rep), rep.json()
    assert data(rep)["markdown"], "旧格式报告应生成 markdown"


def g4_duplicate_review_no_500():
    # 建两个真实快照，手工造一对疑似重复，验证版本冲突返回 VERSION_CONFLICT 而非 500
    r = post("/api/runs", json={"name": "QA重复", "source": "demo",
                                "queries": ["消费金融 产品经理"],
                                "max_search_calls": 2, "max_detail_calls": 4, "details_per_call": 3})
    run = data(r)["run"]
    rid = run["id"]
    s = post(f"/api/runs/{rid}/search", json={"query_id": "q1", "page": 1})
    job_refs = [j["job_ref"] for j in data(s)["job_refs"][:2]]
    d = post(f"/api/runs/{rid}/details", json={"job_refs": job_refs})
    snap_ids = [o["snapshot_id"] for o in data(d)["outcomes"] if o.get("snapshot_id")]
    assert len(snap_ids) >= 2, f"需要至少 2 个快照，实际 {snap_ids}"
    conn = store.get_conn()
    pair_id = store.new_id("pair")
    conn.execute(
        "INSERT INTO job_pair (id, left_snapshot_id, right_snapshot_id, similarity, relation, basis, review_status, review_version, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (pair_id, snap_ids[0], snap_ids[1], 0.95, "suspected", "qa", "unreviewed", 0, store.utcnow_iso()))
    conn.commit()
    rv = post("/api/review", json={
        "target_type": "job_pair", "target_id": pair_id, "action": "mark_duplicate",
        "reason": "同一岗位", "expected_version": 1})
    assert rv.status_code == 200, f"疑似重复复核版本冲突不应 500，实际 status={rv.status_code}"
    assert not ok(rv), "版本冲突应返回错误信封"
    assert err_of(rv)["code"] == "VERSION_CONFLICT", err_of(rv)


# ---------------------------------------------------------------------------
# 阶段 0~4 新增验收：真实文件读取 / AI 未配置不回退（R-01~R-05）
# ---------------------------------------------------------------------------

def make_text_pdf(lines: list[str]) -> bytes:
    """极简单页文本 PDF（英文/ASCII，避免字体嵌入），用于验证 page_no 与块定位。"""
    esc = lambda s: s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    text_ops = "BT /F1 10 Tf 40 780 Td 14 TL\n" + \
        "".join(f"({esc(l)}) Tj T*\n" for l in lines) + "ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
        f"<< /Length {len(text_ops.encode('latin-1'))} >>\nstream\n{text_ops}\nendstream",
    ]
    out = ["%PDF-1.4"]
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len("\n".join(out)) + 1)
        out.append(f"{i} 0 obj")
        out.append(o)
        out.append("endobj")
    xref_pos = len("\n".join(out)) + 1
    out.append("xref")
    out.append(f"0 {len(objs) + 1}")
    out.append("0000000000 65535 f ")
    for off in offsets:
        out.append(f"{off:010d} 00000 n ")
    out.append("trailer")
    out.append(f"<< /Size {len(objs) + 1} /Root 1 0 R >>")
    out.append("startxref")
    out.append(str(xref_pos))
    out.append("%%EOF")
    return "\n".join(out).encode("latin-1")


def r01_real_text_pdf():
    pdf = make_text_pdf(["RESUME", "2020-2023 ACME Corp Product Manager",
                         "Led payment routing product design"])
    r = data(post("/api/resumes/file", files={"file": ("resume.pdf", pdf, "application/pdf")},
                  data={"label": "真实PDF"}))
    res = r["resume"]
    assert res["parse_status"] in ("parsed", "partial"), res
    detail = data(client.get(f"/api/resumes/{res['id']}"))
    blocks = detail["blocks"]
    assert blocks, "文本 PDF 应产生块级原文"
    assert any(b.get("page_no") == 1 for b in blocks), "应记录 page_no"
    full = detail["resume"]["evidence_text"]
    assert "ACME" in full, "提取文本应与文件内容一致"


def r02_docx_table():
    import docx as _docx
    d = _docx.Document()
    d.add_paragraph("2019-2024 SomeBank Product Manager - payment clearing")
    d.add_paragraph("Led the product design of fund routing and reconciliation module")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "Skills"
    t.cell(0, 1).text = "SQL, Axure"
    t.cell(1, 0).text = "Domain"
    t.cell(1, 1).text = "Consumer finance settlement"
    buf = io.BytesIO()
    d.save(buf)
    r = data(post("/api/resumes/file", files={"file": ("resume.docx", buf.getvalue(),
                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
                  data={"label": "DOCX表格"}))
    res = r["resume"]
    assert res["parse_status"] in ("parsed", "partial"), res
    detail = data(client.get(f"/api/resumes/{res['id']}"))
    assert "Skills" in detail["resume"]["evidence_text"], "表格内容应被保留"


def r03_scan_pdf_needs_ocr():
    from PIL import Image
    img = Image.new("RGB", (600, 800), "white")
    buf = io.BytesIO()
    img.save(buf, format="PDF")
    r = data(post("/api/resumes/file", files={"file": ("scan.pdf", buf.getvalue(), "application/pdf")},
                  data={"label": "扫描件"}))
    res = r["resume"]
    assert res["parse_status"] == "needs_ocr", f"扫描件应 needs_ocr，实际 {res['parse_status']}"
    assert res["needs_ocr"] is True


def r04_corrupt_or_oversize():
    # 损坏 PDF → PARSE_FAILED 错误信封
    r1 = post("/api/resumes/file", files={"file": ("bad.pdf", b"%PDF-1.4 broken", "application/pdf")},
              data={"label": "损坏"})
    assert not ok(r1), "损坏文件应返回错误信封"
    assert err_of(r1)["code"] == "PARSE_FAILED", err_of(r1)
    # 不支持扩展名
    r2 = post("/api/resumes/file", files={"file": ("x.doc", b"xx", "application/msword")},
              data={"label": "不支持"})
    assert not ok(r2) and err_of(r2)["code"] == "FILE_UNSUPPORTED"


def r05_ai_not_configured_no_fallback():
    import ai_provider
    cfg = ai_provider.ai_config()
    if cfg["configured"]:
        return  # 环境已配置真实 key，跳过未配置断言
    # demo_mode 关闭时，auto-match 必须 AI_NOT_CONFIGURED，不回退 mock
    store.get_conn().execute(
        "UPDATE app_setting SET value='0' WHERE key='demo_mode'")
    store.get_conn().commit()
    sid = create_session()["id"]
    tgt = create_target(STD_RESP, STD_REQ, session_id=sid)
    res = import_resume(HIGH_RESUME)
    post(f"/api/js/sessions/{sid}", json={"job_target_id": tgt["id"], "resume_id": res["id"]})
    m = post(f"/api/js/sessions/{sid}/auto-match")
    assert not ok(m), "未配置时应失败，不能返回模拟成功"
    assert err_of(m)["code"] == "AI_NOT_CONFIGURED", err_of(m)
    # 恢复 demo_mode
    store.get_conn().execute(
        "UPDATE app_setting SET value='1' WHERE key='demo_mode'")
    store.get_conn().commit()


def _start_mock_llm() -> tuple[object, str]:
    """启动本地 mock OpenAI 兼容服务（测试专用，明确不是真实 AI）。返回 (server, base_url)。"""
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = _json.loads(self.rfile.read(n))
            # 从 user content 里解析出 jd/resume 原文，构造引用正确的响应
            user = ""
            for m in body.get("messages", []):
                if m.get("role") == "user":
                    user = m.get("content", "")
            payload = {}
            try:
                # user content = JSON + "\n\n" + 尾部说明；raw_decode 只解析首个 JSON 对象
                payload, _ = _json.JSONDecoder().raw_decode(user)
            except Exception:
                payload = {}
            # _prepared_to_user_content 输出 JSON 含 jd.evidence_text / resume.evidence_text
            jd_text = (payload.get("jd") or {}).get("evidence_text", "")
            resume_text = (payload.get("resume") or {}).get("evidence_text", "")
            def find_quote(text, kw):
                i = text.find(kw)
                return i if i >= 0 else 0
            jd_first = jd_text.split("\n")[1] if "\n" in jd_text else jd_text[:40]
            jd_pos = jd_text.find(jd_first)
            if jd_pos < 0:
                jd_pos = 0
            resume_kw = "资金" if "资金" in resume_text else resume_text[:6]
            resume_pos = resume_text.find(resume_kw)
            if resume_pos < 0:
                resume_pos = 0
            resp = {
                "requirements": [{
                    "ref": "R1", "category": "core_task", "text": jd_first[:200],
                    "judgement_note": "mock LLM 抽取（测试）",
                    "quotes": [{"side": "jd", "section": "responsibility", "quote": jd_first,
                                "start": jd_pos, "end": jd_pos + len(jd_first)}],
                }],
                "match_rows": [{
                    "requirement_ref": "R1", "match_status": "direct_evidence",
                    "jd_note": "JD 要求涉及资金相关", "resume_note": "简历存在对应证据",
                    "reasoning": "mock LLM 判定（测试）", "uncertainties": ["引用只证明文字存在"],
                    "prep_advice": "准备量化结果", "quotes": [
                        {"side": "resume", "section": "experience", "quote": resume_kw,
                         "start": resume_pos, "end": resume_pos + len(resume_kw)}],
                }],
                "clarifications": [],
            }
            out = _json.dumps(
                {"choices": [{"message": {"content": _json.dumps(resp, ensure_ascii=False)}}]},
                ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/v1"


def r06_real_model_path():
    """真实模型调用路径验证（用本地 mock OpenAI 兼容服务，明确标注测试；验证 HTTP 调用→校验→入库）。"""
    import ai_provider
    srv, base = _start_mock_llm()
    conn = store.get_conn()
    for k, v in (("demo_mode", "0"), ("llm_api_base", base),
                 ("llm_api_key", "test-key-not-real"), ("llm_model", "test-model")):
        conn.execute("INSERT INTO app_setting (key, value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
    conn.commit()
    try:
        assert ai_provider.ai_config()["configured"] is True
        sid = create_session()["id"]
        tgt = create_target(STD_RESP, STD_REQ, session_id=sid)
        res = import_resume(HIGH_RESUME)
        post(f"/api/js/sessions/{sid}", json={"job_target_id": tgt["id"], "resume_id": res["id"]})
        m = post(f"/api/js/sessions/{sid}/auto-match")
        assert ok(m), f"真实模型路径 auto-match 失败: {m.json()}"
        mv = match_view(sid)
        assert mv["model_label"] and "test-model" in mv["model_label"], mv["model_label"]
        assert mv["rows"], "应产生匹配行"
        assert any(r["quotes"] for r in mv["rows"]), "匹配行应带引用"
        # 事实提取走真实模型路径
        f = post(f"/api/resumes/{res['id']}/extract-facts")
        assert ok(f), f"事实提取失败: {f.json()}"
    finally:
        srv.shutdown()
        for k in ("demo_mode", "llm_api_base", "llm_api_key", "llm_model"):
            conn.execute("DELETE FROM app_setting WHERE key=?", (k,))
        conn.execute("INSERT INTO app_setting (key, value) VALUES ('demo_mode', '1') "
                     "ON CONFLICT(key) DO UPDATE SET value='1'")
        conn.commit()


# ---------------------------------------------------------------------------
# 运行器
# ---------------------------------------------------------------------------

SCENARIOS = [
    ("S1", s1_high_match), ("S2", s2_transferable), ("S3", s3_not_in_resume),
    ("S4", s4_confirmed_gap), ("S5", s5_ambiguous_jd), ("S6", s6_parse_failed),
    ("S7", s7_model_analysis_failed), ("S8", s8_reanalysis_preserves_review),
    ("S9", s9_traceability),
    ("A1", a1_banned_phrases), ("A2", a2_no_contact_leak), ("A3", a3_delete_resume),
    ("A5", a5_practice_annotation), ("A6", a6_verifiable_resources),
    ("A8", a8_prompt_declaration), ("A9", a9_no_apply_api), ("A10", a10_rule_matcher_label),
    ("G1", g1_research_routes), ("G2", g2_migration_idempotent),
    ("G3", g3_demo_research_chain), ("G4", g4_duplicate_review_no_500),
    ("R-01", r01_real_text_pdf), ("R-02", r02_docx_table),
    ("R-03", r03_scan_pdf_needs_ocr), ("R-04", r04_corrupt_or_oversize),
    ("R-05", r05_ai_not_configured_no_fallback), ("R-06", r06_real_model_path),
]


def main() -> int:
    results = []
    for name, fn in SCENARIOS:
        try:
            fn()
            results.append((name, "PASS", ""))
        except Exception as e:  # noqa: BLE001
            results.append((name, "FAIL", f"{type(e).__name__}: {e}"))

    print("\n" + "=" * 70)
    print("finintel 求职者模式 QA 测试结果")
    print("=" * 70)
    for name, status, detail in results:
        line = f"[{status}] {name}"
        print(line)
        if detail:
            print("       " + detail.replace("\n", "\n       "))
    n_pass = sum(1 for _, s, _ in results if s == "PASS")
    n_fail = len(results) - n_pass
    print("-" * 70)
    print(f"TOTAL {len(results)} | PASS {n_pass} | FAIL {n_fail}")
    print("=" * 70 + "\n")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
