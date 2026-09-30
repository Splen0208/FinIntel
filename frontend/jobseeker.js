/* finintel 求职者模式前端（T05）
 *
 * 复用 app.js 中的全局函数：$ / $$ / esc / api / toast / handleErr / badge / lbl /
 * fmtTime / fmtShort / renderEvidence / highlightEvidence / openDrawer / openModal /
 * closeDrawer / closeModal / mdToHtml / LABELS / TAXONOMY / RENDER / PAGES / route。
 * 本文件不重复实现以上能力；新枚举中文标签一律从 LABELS（/api/health 下发）读取。
 */
"use strict";

/* ============================ 模式管理 ============================ */

function currentMode() {
  const m = localStorage.getItem("fin.mode");
  return m === "jobseeker" ? "jobseeker" : "research";
}

function setMode(mode) {
  if (mode !== "research" && mode !== "jobseeker") return;
  localStorage.setItem("fin.mode", mode);
  renderModeSwitch();
  // 切换模式不清除任何记录，仅跳转到对应模式首页。
  if (mode === "jobseeker") {
    if (!route.page || route.page.indexOf("js-") !== 0) location.hash = "#/js-sessions";
  } else {
    if (route.page && route.page.indexOf("js-") === 0) location.hash = "#/dashboard";
  }
}

function renderModeSwitch() {
  const el = $("#mode-switch");
  if (!el) return;
  const mode = currentMode();
  el.innerHTML = `
    <div class="mode-switch-label">当前模式（切换不清除记录）</div>
    <div class="mode-switch-btns">
      <button class="mode-btn ${mode === "research" ? "active" : ""}" onclick="setMode('research')">研究者</button>
      <button class="mode-btn ${mode === "jobseeker" ? "active" : ""}" onclick="setMode('jobseeker')">求职者</button>
    </div>`;
}

/* ============================ 中文标签兜底（仅限 /api/health 未下发的字典） ============================ */

// resume_item.kind 未纳入 /api/health.labels，此为本文件唯一一处本地中文标签。
const RESUME_ITEM_KIND_LABELS = {
  education: "教育经历", experience: "工作/实习经历", project: "项目经历",
  skill: "技能", summary: "自我评价", other: "其他",
};

function jsLabels(kind) {
  return (LABELS && LABELS[kind]) || {};
}

/* ============================ 状态配色 ============================ */

const SESSION_BADGE = { draft: "b-gray", jd_ready: "b-blue", resume_ready: "b-blue", matched: "b-green", focus_ready: "b-purple", plan_ready: "b-green", failed: "b-red" };
const MATCH_BADGE = { direct_evidence: "b-green", transferable: "b-orange", not_in_resume: "b-orange", confirmed_gap: "b-red", insufficient: "b-gray" };
const REQ_CAT_BADGE = { core_task: "b-blue", must_have: "b-purple", nice_to_have: "b-teal", scenario_experience: "b-purple", ambiguous: "b-orange" };
const PARSE_BADGE = { pending: "b-gray", parsed: "b-green", partial: "b-orange", failed: "b-red", too_large: "b-red", needs_ocr: "b-orange" };
const LEARN_BADGE = { short_term_learnable: "b-teal", needs_expression_practice: "b-blue", real_project_required: "b-red" };
const PREP_BADGE = { planned: "b-gray", in_progress: "b-blue", done: "b-green" };
const FOCUS_BADGE = { business_scenario: "b-teal", project_contribution: "b-blue", decision_tradeoff: "b-purple", result_metric: "b-green", failure_improvement: "b-orange", transfer_ability: "b-blue" };
const RESUME_ITEM_KIND_BADGE = { education: "b-blue", experience: "b-green", project: "b-purple", skill: "b-teal", summary: "b-gray", other: "b-gray" };

/* ============================ 通用工具 ============================ */

function demoStrip() {
  return `<div class="warn-strip">⚠ 结果由内置规则匹配器生成（rule-matcher-demo-v1），语义需人工确认，不代表宿主模型质量。</div>`;
}

function failureArea(session) {
  return `<div class="err-box"><b>处理失败</b>：${esc(session.failure_message || session.failure_code || "未知错误")}
    <div class="btn-row mt8"><button class="btn sm" onclick="retryJsSession('${esc(session.id)}')">重置失败态重试</button></div>
    <div class="mt8">下一步：根据错误信息修正后重新分析，或重新导入 JD / 简历。</div></div>`;
}

function jsEmpty(msg) {
  return `<div class="page-head"><div><h1>求职者模式</h1></div></div>
    <div class="card card-pad empty"><div class="big">◇</div>${esc(msg)}</div>`;
}

function renderEvidenceBox(id, text) {
  return `<div class="evidence-box" id="${esc(id)}">${renderEvidence(text || "")}</div>`;
}

// 给主区内的 evidence-box 绑定 dataset.text，供 highlightEvidence 定位使用（复用 app.js 逻辑）。
function bindEvidence(root) {
  $$(".evidence-box", root || document).forEach(box => {
    const src = box.querySelector(".ev-src");
    if (src) src.dataset.text = src.textContent;
  });
}

function isOutcomeOk(data) {
  const outs = data && data.outcomes;
  return !!(outs && outs.length && outs.every(o => o.ok));
}

function renderMatchOutcome(data) {
  const el = $("#js-match-result");
  if (!el) return;
  const outs = (data && data.outcomes) || [];
  el.innerHTML = outs.map(o => o.ok
    ? `<div class="ok-box">✓ 校验通过，已入库 ${(data.row_ids || []).length} 行。</div>`
    : `<div class="err-box">✗ 校验失败，未入库（字段级错误）：<br>${(o.errors || []).map(e => `<b>${esc(e.field)}</b> [${esc(e.code)}] ${esc(e.message)}`).join("<br>")}</div>`).join("");
}

function renderPrepOutcome(data) {
  const el = $("#js-focus-result");
  if (!el) return;
  const outs = (data && data.outcomes) || [];
  el.innerHTML = outs.map(o => o.ok
    ? `<div class="ok-box">✓ 已生成 ${(data.focus_ids || []).length} 个考察重点、${(data.task_ids || []).length} 个准备任务。</div>`
    : `<div class="err-box">✗ 校验失败，未入库（字段级错误）：<br>${(o.errors || []).map(e => `<b>${esc(e.field)}</b> [${esc(e.code)}] ${esc(e.message)}`).join("<br>")}</div>`).join("");
}

/* ============================ 页面：会话列表 ============================ */

function sessionDest(s) {
  if (["matched", "focus_ready", "plan_ready"].includes(s.status)) return `#/js-match/${s.id}`;
  return `#/js-wizard/${s.id}`;
}

async function renderJsSessions(main) {
  const r = await api("/api/js/sessions");
  if (!handleErr(r)) return;
  const sessions = r.data.sessions;
  main.innerHTML = `
  <div class="page-head">
    <div><h1>面试准备</h1><div class="sub">把搜索到的目标岗位、X／小红书背景研究与自己的经历连起来。</div></div>
    <button class="btn primary" onclick="createJsSession()">＋ 新建求职准备</button>
  </div>
  <div class="card card-pad">
    ${sessions.length ? `<table class="tbl">
      <tr><th>目标岗位</th><th>状态</th><th>简历</th><th>更新时间</th><th></th></tr>
      ${sessions.map(s => `<tr class="rowlink" onclick="location.hash='${sessionDest(s)}'">
        <td><b>${esc(s.title || s.role_direction || "未命名准备")}</b>${s.company_display ? `<br><span class="small muted">${esc(s.company_display)}</span>` : ""}</td>
        <td>${badge(SESSION_BADGE, s.status, LABELS.session_status)}${s.route === "explore" ? ' <span class="badge b-purple">行业需求</span>' : ""}</td>
        <td class="muted">${esc(s.resume_label || "—")}</td>
        <td class="small muted">${fmtTime(s.updated_at)}</td>
        <td><a class="btn sm" href="${sessionDest(s)}" onclick="event.stopPropagation()">打开</a></td>
      </tr>`).join("")}</table>` : `<div class="empty"><div class="big">＋</div>还没有求职准备会话。点击右上角"新建求职准备"开始。</div>`}
  </div>`;
}

async function createJsSession() {
  const r = await api("/api/js/sessions", { method: "POST", body: { route: "match" } });
  if (!handleErr(r)) return;
  // 后端 create_session 返回 get_session() 的结构：data.session = { session, job_target, resume, counts }。
  const s = r.data.session;
  const id = (s && s.session && s.session.id) || (s && s.id);
  if (!id) { toast("创建会话失败：响应缺少会话 ID", "err"); return; }
  location.hash = "#/js-wizard/" + id;
}

/* ============================ 页面：向导 ============================ */

function jsStepIdx(status) {
  return { draft: 1, jd_ready: 2, resume_ready: 3, matched: 4, focus_ready: 5, plan_ready: 6, failed: 0 }[status] ?? 1;
}

function renderJsSteps(idx) {
  const steps = [["目标 JD"], ["简历导入"], ["条目校对"], ["匹配矩阵"], ["面试重点"], ["准备清单"]];
  return `<div class="card card-pad"><div class="steps">${steps.map((s, i) => `
    <div class="step ${i + 1 < idx ? "done" : ""} ${i + 1 === idx ? "cur" : ""}">
      <div class="dot">${i + 1 < idx ? "✓" : i + 1}</div><div class="stxt">${s[0]}</div>
    </div>${i < steps.length - 1 ? `<div class="step-join ${i + 1 < idx ? "done" : ""}"></div>` : ""}`).join("")}</div></div>`;
}

function toggleJsSection(id) {
  const el = $("#" + id);
  if (el) el.style.display = el.style.display === "none" ? "block" : "none";
}

function switchJdTab(t) {
  $$("#js-jd-tabs .tab").forEach(x => x.classList.toggle("active", x.dataset.t === t));
  $("#js-jd-paste").style.display = t === "paste" ? "block" : "none";
  $("#js-jd-library").style.display = t === "library" ? "block" : "none";
}

function switchResumeTab(t) {
  $$("#js-resume-tabs .tab").forEach(x => x.classList.toggle("active", x.dataset.t === t));
  $("#js-resume-text").style.display = t === "text" ? "block" : "none";
  $("#js-resume-file").style.display = t === "file" ? "block" : "none";
  $("#js-resume-existing").style.display = t === "existing" ? "block" : "none";
}

function jdFormHtml(sessionId, jobs) {
  return `
    <div class="tabs" id="js-jd-tabs">
      <div class="tab active" data-t="paste" onclick="switchJdTab('paste')">粘贴 JD</div>
      <div class="tab" data-t="library" onclick="switchJdTab('library')">从岗位库选择</div>
    </div>
    <div id="js-jd-paste">
      <div class="grid c2">
        <div class="field"><label class="f">岗位名称</label><input type="text" id="jd-title" placeholder="例如：消费信贷产品经理"></div>
        <div class="field"><label class="f">公司（可选）</label><input type="text" id="jd-company" placeholder="例如：某消费金融公司"></div>
      </div>
      <div class="field"><label class="f">工作职责</label><textarea id="jd-resp" rows="5" placeholder="粘贴岗位职责正文"></textarea></div>
      <div class="field"><label class="f">任职要求</label><textarea id="jd-req" rows="5" placeholder="粘贴任职要求正文"></textarea></div>
      <button class="btn primary" onclick="submitPasteTarget('${esc(sessionId)}')">保存目标 JD</button>
    </div>
    <div id="js-jd-library" style="display:none">
      ${jobs.length ? `
        <div class="field"><label class="f">选择一个已采集详情的岗位（${jobs.length}）</label>
          <select id="jd-job-sel">${jobs.map(j => `<option value="${esc(j.id)}">${esc(j.title)} · ${esc(j.company_name_raw)}</option>`).join("")}</select></div>
        <button class="btn primary" onclick="submitLibraryTarget('${esc(sessionId)}')">使用该岗位作为目标 JD</button>
        <div class="f-help mt8">岗位库中无岗位时，请先在研究者模式完成一次采集运行。</div>` : `
        <div class="empty">岗位库暂无可用岗位（需正文完整）。可直接在左侧粘贴 JD。</div>`}
    </div>`;
}

function resumeFormHtml(sessionId, resumes) {
  return `
    <div class="tabs" id="js-resume-tabs">
      <div class="tab active" data-t="text" onclick="switchResumeTab('text')">粘贴文本</div>
      <div class="tab" data-t="file" onclick="switchResumeTab('file')">上传文件</div>
      <div class="tab" data-t="existing" onclick="switchResumeTab('existing')">选择已有</div>
    </div>
    <div id="js-resume-text">
      <div class="field"><label class="f">简历名称</label><input type="text" id="rv-label" placeholder="例如：我的简历 2026"></div>
      <div class="field"><label class="f">简历正文</label><textarea id="rv-text" rows="12" placeholder="粘贴简历文本；导入时自动移除手机号/邮箱"></textarea></div>
      <button class="btn primary" onclick="importResumeText('${esc(sessionId)}')">导入文本简历</button>
    </div>
    <div id="js-resume-file" style="display:none">
      <div class="field"><label class="f">简历名称</label><input type="text" id="rv-file-label" placeholder="例如：我的简历 2026"></div>
      <div class="field"><label class="f">选择文件（PDF / DOCX）</label><input type="file" id="rv-file" accept=".pdf,.docx,.doc"></div>
      <div class="f-help">扫描件无文本层将判解析失败，请改用粘贴文本。</div>
      <button class="btn primary" onclick="importResumeFile('${esc(sessionId)}')">上传并解析</button>
    </div>
    <div id="js-resume-existing" style="display:none">
      ${resumes.length ? `
        <div class="field"><label class="f">选择已有简历</label>
          <select id="rv-existing-sel">${resumes.map(r => `<option value="${esc(r.id)}">${esc(r.label)}（${esc(r.parse_status)}）</option>`).join("")}</select></div>
        <button class="btn primary" onclick="bindExistingResume('${esc(sessionId)}')">绑定到本会话</button>` : `
        <div class="empty">暂无已解析的简历。</div>`}
    </div>`;
}

async function renderJsWizard(main) {
  const pageSeq = __routeSeq;
  const sessionId = route.arg;
  if (!sessionId) {
    if (pageSeq !== __routeSeq) return;
  main.innerHTML = `<div class="page-head"><div><h1>新建求职准备</h1><div class="sub">从搜索结果选定岗位，结合背景研究与简历生成准备计划。</div></div></div>
    <div class="card card-pad empty"><div class="big">＋</div>创建一次求职准备会话后开始录入。
      <div class="btn-row mt14" style="justify-content:center"><button class="btn primary" onclick="createJsSession()">＋ 创建会话</button></div></div>`;
    return;
  }
  const sres = await api(`/api/js/sessions/${sessionId}`);
  if (!handleErr(sres)) return;
  const { session, job_target, resume } = sres.data;
  const jobsRes = await api("/api/jobs");
  const jobs = (jobsRes.ok ? jobsRes.data.jobs : []).filter(j => ["complete", "partial"].includes(j.detail_status));
  const resumesRes = await api("/api/resumes");
  const resumes = (resumesRes.ok ? resumesRes.data.resumes : []).filter(r => !r.deleted_at && ["parsed", "partial"].includes(r.parse_status));

  if (pageSeq !== __routeSeq) return;
  main.innerHTML = `
  <div class="page-head">
    <div><h1>面试准备工作区</h1><div class="sub">${fmtShort(session.id)} · ${badge(SESSION_BADGE, session.status, LABELS.session_status)}</div></div>
    <div class="btn-row">
      <a class="btn" href="#/js-sessions">← 返回列表</a>
      <a class="btn primary" href="#/js-match/${esc(sessionId)}">进入匹配矩阵 →</a>
    </div>
  </div>
  ${renderJsSteps(jsStepIdx(session.status))}
  ${session.status === "failed" ? failureArea(session) : ""}

  <div class="card card-pad">
    <h3>① 目标 JD <span class="hint">粘贴或从岗位库选择，直接开始</span></h3>
    ${job_target ? `
      <div class="ok-box">已选择：<b>${esc(job_target.title || "")}</b>${job_target.company_display ? " · " + esc(job_target.company_display) : ""}（${esc(job_target.source_kind)}，${job_target.char_count} 字符）</div>
      <div class="btn-row mt8"><button class="btn sm" onclick="toggleJsSection('js-jd-form')">更换 JD</button></div>
      <div id="js-jd-form" style="display:none">${jdFormHtml(sessionId, jobs)}</div>` : `
      <div id="js-jd-form">${jdFormHtml(sessionId, jobs)}</div>`}
  </div>

  <div class="card card-pad">
    <h3>③ 简历 <span class="hint">粘贴文本 / 上传 PDF·DOCX / 选择已有简历</span></h3>
    ${resume ? `
      <div class="ok-box">已绑定：<b>${esc(resume.label)}</b> · ${badge(PARSE_BADGE, resume.parse_status, LABELS.parse_status)}（${resume.char_count} 字符）</div>
      <div class="btn-row mt8"><button class="btn sm" onclick="toggleJsSection('js-resume-form')">更换简历</button></div>
      <div id="js-resume-form" style="display:none">${resumeFormHtml(sessionId, resumes)}</div>` : `
      <div id="js-resume-form">${resumeFormHtml(sessionId, resumes)}</div>`}
  </div>

  <div class="card card-pad">
    <h3>④ 校对简历条目 <span class="hint">逐项修正 / 新增；user 条目不会被重新解析覆盖</span></h3>
    <div id="js-items-area"></div>
  </div>

  <div class="card card-pad">
    <h3>⑤ 匹配分析 → ⑥ 面试重点与准备清单</h3>
    <div class="btn-row">
      <a class="btn primary" href="#/js-match/${esc(sessionId)}">进入匹配矩阵</a>
      ${["matched", "focus_ready", "plan_ready"].includes(session.status) ? `<a class="btn" href="#/js-focus/${esc(sessionId)}">面试重点</a>` : ""}
      ${session.status === "plan_ready" ? `<a class="btn" href="#/js-prep/${esc(sessionId)}">准备清单</a>` : ""}
    </div>
    <div class="f-help mt8">先用模型分析 JD 与简历的匹配，再结合所选背景材料生成面试重点与准备计划。演示模式使用标明的合成数据与规则。</div>
  </div>`;

  if (resume && ["parsed", "partial"].includes(resume.parse_status)) {
    await renderWizardItems(resume.id);
  } else if (resume) {
    $("#js-items-area").innerHTML = `<div class="err-box">简历解析状态为 ${esc(resume.parse_status)}，无法校对。请更换简历（推荐粘贴文本版）。</div>`;
  } else {
    $("#js-items-area").innerHTML = `<div class="empty">先完成③简历导入后再校对。</div>`;
  }
}

async function submitPasteTarget(sessionId) {
  const title = $("#jd-title").value.trim();
  const company_raw = $("#jd-company").value.trim();
  const responsibilities_text = $("#jd-resp").value;
  const requirements_text = $("#jd-req").value;
  if (!title) return toast("请填写岗位名称", "err");
  if (!responsibilities_text.trim() && !requirements_text.trim()) return toast("请粘贴职责或任职要求至少其一", "err");
  const r = await api("/api/job-targets", { method: "POST", body: { session_id: sessionId, source_kind: "paste", title, company_raw, responsibilities_text, requirements_text } });
  if (!handleErr(r)) return;
  toast("目标 JD 已保存", "ok");
  routeTo();
}

async function submitLibraryTarget(sessionId) {
  const jobId = $("#jd-job-sel").value;
  if (!jobId) return toast("请选择岗位", "err");
  const r = await api("/api/job-targets", { method: "POST", body: { session_id: sessionId, source_kind: "library", job_id: jobId } });
  if (!handleErr(r)) return;
  toast("已从岗位库选择目标 JD", "ok");
  routeTo();
}

function handleResumeImport(r, sessionId) {
  if (!handleErr(r)) return;
  const resume = r.data.resume;
  if (resume.parse_status === "failed" || resume.parse_status === "too_large") {
    toast(`无法使用该简历：${resume.failure_message || resume.parse_status}`, "err");
    return;
  }
  if (resume.parse_status === "needs_ocr") {
    toast("该文件无可提取文本层（疑似扫描件/图片），请粘贴文本版简历或上传可复制的 PDF", "err");
    return;
  }
  bindResumeToSession(sessionId, resume.id);
}

async function importResumeText(sessionId) {
  const label = $("#rv-label").value.trim();
  const text = $("#rv-text").value;
  if (!label) return toast("请填写简历名称", "err");
  if (!text.trim()) return toast("请粘贴简历正文", "err");
  const r = await api("/api/resumes", { method: "POST", body: { label, text } });
  handleResumeImport(r, sessionId);
}

async function importResumeFile(sessionId) {
  const input = $("#rv-file");
  const file = input && input.files && input.files[0];
  const label = $("#rv-file-label").value.trim();
  if (!file) return toast("请选择文件", "err");
  if (!label) return toast("请填写简历名称", "err");
  const fd = new FormData();
  fd.append("file", file);
  fd.append("label", label);
  toast("正在解析文件…");
  const res = await fetch("/api/resumes/file", { method: "POST", body: fd });
  const r = await res.json().catch(() => ({ ok: false, error: { code: "PARSE", message: "响应解析失败" } }));
  handleResumeImport(r, sessionId);
}

async function bindResumeToSession(sessionId, resumeId) {
  const r = await api(`/api/js/sessions/${sessionId}`, { method: "POST", body: { resume_id: resumeId } });
  if (!handleErr(r)) return;
  toast("简历已绑定", "ok");
  routeTo();
}

async function bindExistingResume(sessionId) {
  const resumeId = $("#rv-existing-sel").value;
  if (!resumeId) return toast("请选择简历", "err");
  bindResumeToSession(sessionId, resumeId);
}

async function retryJsSession(sessionId) {
  const r = await api(`/api/js/sessions/${sessionId}/retry`, { method: "POST", body: {} });
  if (!handleErr(r)) return;
  toast("已重置失败态", "ok");
  routeTo();
}

/* ---------- 简历条目校对 ---------- */

async function renderWizardItems(resumeId) {
  const r = await api(`/api/resumes/${resumeId}`);
  if (!handleErr(r)) return;
  window.__jsResumeId = resumeId;
  window.__jsResumeItems = r.data.items.map(it => ({
    id: it.id, kind: it.kind, title: it.title, org: it.org,
    period_raw: it.period_raw, start: it.start, end: it.end,
  }));
  const el = $("#js-items-area");
  if (!el) return;
  el.innerHTML = `
    <div class="field"><label class="f">结构化条目（${r.data.items.length}）</label>
      <div id="js-items-list"></div>
      <div class="btn-row mt8">
        <button class="btn sm" onclick="addResumeItemRow()">＋ 新增条目</button>
        <button class="btn sm primary" onclick="saveResumeItems()">保存校对</button>
      </div>
      <div class="f-help">保存需填写原因（留痕）；修改后 source=user，不会被重新解析覆盖。</div></div>`;
  paintResumeItems();
}

function kindOptions(sel) {
  return Object.entries(RESUME_ITEM_KIND_LABELS).map(([k, v]) => `<option value="${k}" ${k === sel ? "selected" : ""}>${esc(v)}</option>`).join("");
}

function paintResumeItems() {
  const el = $("#js-items-list");
  if (!el) return;
  const items = window.__jsResumeItems || [];
  el.innerHTML = items.map((it, i) => `
    <div class="item-row" data-i="${i}">
      <select data-f="kind">${kindOptions(it.kind)}</select>
      <input data-f="title" value="${esc(it.title || "")}" placeholder="标题">
      <input data-f="org" value="${esc(it.org || "")}" placeholder="组织">
      <input data-f="period" value="${esc(it.period_raw || "")}" placeholder="时间段">
      <button class="btn sm danger" onclick="removeResumeItemRow(${i})">✕</button>
    </div>`).join("");
}

function addResumeItemRow() {
  window.__jsResumeItems = window.__jsResumeItems || [];
  window.__jsResumeItems.push({ id: null, kind: "experience", title: "", org: "", period_raw: "", start: null, end: null });
  paintResumeItems();
}

function removeResumeItemRow(i) {
  window.__jsResumeItems.splice(i, 1);
  paintResumeItems();
}

async function saveResumeItems() {
  const reason = prompt("请填写校对原因（留痕要求）：");
  if (reason === null) return;
  if (!reason.trim()) return toast("必须填写原因", "err");
  const items = [];
  $$("#js-items-list .item-row").forEach(rowEl => {
    const i = +rowEl.dataset.i;
    const src = window.__jsResumeItems[i];
    const get = f => rowEl.querySelector(`[data-f="${f}"]`).value.trim();
    items.push({
      id: src.id || undefined,
      kind: rowEl.querySelector('[data-f="kind"]').value,
      title: get("title"),
      org: get("org") || null,
      period_raw: get("period") || null,
      start: src.start != null ? src.start : null,
      end: src.end != null ? src.end : null,
      confidence: 1.0,
    });
  });
  if (!items.length) return toast("至少保留一条条目", "err");
  const r = await api(`/api/resumes/${window.__jsResumeId}/items`, { method: "POST", body: { items, reason: reason.trim() } });
  if (!handleErr(r)) return;
  toast("校对已保存", "ok");
  routeTo();
}

/* ============================ 页面：匹配矩阵 ============================ */

function quoteChip(q, boxId) {
  const sideLabel = jsLabels("evidence_side")[q.side] || q.side;
  return `<div class="quote-chip" onclick="highlightEvidence('${boxId}', ${q.start}, ${q.end})" title="点击高亮定位 [${q.start},${q.end})">
    <span class="badge ${q.side === "jd" ? "b-purple" : "b-teal"}">${esc(sideLabel)}</span>
    “${esc(q.quote && q.quote.length > 160 ? q.quote.slice(0, 160) + "…" : (q.quote || ""))}”
    <span class="mono muted">[${q.start},${q.end})</span>
  </div>`;
}

function renderMatrix(rows) {
  if (!rows || !rows.length) return `<div class="empty"><div class="big">⌘</div>尚未生成匹配矩阵</div>`;
  return rows.map(r => {
    const req = r.requirement || {};
    const jdQuotes = (r.quotes || []).filter(q => q.side === "jd");
    const resumeQuotes = (r.quotes || []).filter(q => q.side === "resume");
    const uncertainties = r.uncertainties || [];
    return `
    <div class="matrix-row matrix-status-${esc(r.match_status)}">
      <div class="matrix-head">
        <div class="matrix-req">${badge(REQ_CAT_BADGE, req.category, LABELS.requirement_category)}<span class="matrix-text">${esc(req.text || "—")}</span></div>
        <div class="matrix-status">${badge(MATCH_BADGE, r.match_status, LABELS.match_status)}${r.needs_rejudge ? ' <span class="badge b-orange">可重新分析</span>' : ""}</div>
      </div>
      <div class="matrix-notes">
        ${r.jd_note ? `<div class="matrix-note"><b>JD 侧：</b>${esc(r.jd_note)}</div>` : ""}
        ${r.resume_note ? `<div class="matrix-note"><b>简历侧：</b>${esc(r.resume_note)}</div>` : ""}
      </div>
      ${(jdQuotes.length || resumeQuotes.length) ? `
      <details class="matrix-ev">
        <summary>展开原文依据（JD ${jdQuotes.length} 条 / 简历 ${resumeQuotes.length} 条）</summary>
        ${jdQuotes.map(q => quoteChip(q, "js-jd-ev")).join("")}
        ${resumeQuotes.map(q => quoteChip(q, "js-resume-ev")).join("")}
      </details>` : ""}
      ${r.reasoning ? `<div class="matrix-reason"><b>判定说明：</b>${esc(r.reasoning)}</div>` : ""}
      ${uncertainties.length ? `<div class="matrix-unct"><b>仍不确定：</b>${uncertainties.map(esc).join("；")}</div>` : ""}
      ${r.prep_advice ? `<div class="matrix-advice"><b>准备建议：</b>${esc(r.prep_advice)}</div>` : ""}
      ${r.user_supplement ? `<div class="matrix-supp"><b>本人补充：</b>${esc(r.user_supplement)}</div>` : ""}
      <div class="btn-row mt8">
        <button class="btn sm" onclick="openSupplementModal('${esc(r.id)}')">＋ 补充本人经历</button>
        <button class="btn sm" onclick="openCorrectModalJs('match_row','${esc(r.id)}',${r.review_version || 0})">✎ 修正状态</button>
      </div>
    </div>`;
  }).join("");
}

async function renderJsMatch(main) {
  const pageSeq = __routeSeq;
  const sessionId = route.arg;
  if (!sessionId) { main.innerHTML = jsEmpty("缺少会话参数"); return; }
  const sres = await api(`/api/js/sessions/${sessionId}`);
  if (!handleErr(sres)) return;
  const session = sres.data.session;
  const jt = sres.data.job_target;
  const rv = sres.data.resume;
  const mres = await api(`/api/js/sessions/${sessionId}/match`);
  if (!handleErr(mres)) return;
  const mv = mres.data;

  let jdText = "", resumeText = "";
  if (jt && jt.id) {
    const t = await api(`/api/job-targets/${jt.id}`);
    if (t.ok) jdText = t.data.job_target.evidence_text || "";
  }
  if (rv && rv.id) {
    const r2 = await api(`/api/resumes/${rv.id}`);
    if (r2.ok) resumeText = r2.data.resume.evidence_text || "";
  }

  if (pageSeq !== __routeSeq) return;
  window.__jsRowsById = {};
  (mv.rows || []).forEach(row => { window.__jsRowsById[row.id] = row; });

  const readyForMatch = ["resume_ready", "matched", "focus_ready", "plan_ready", "failed"].includes(session.status);
  const matched = ["matched", "focus_ready", "plan_ready"].includes(session.status);

  if (pageSeq !== __routeSeq) return;
  main.innerHTML = `
  <div class="page-head">
    <div><h1>匹配矩阵</h1><div class="sub">${esc(jt ? jt.title : "未选择目标 JD")}${jt && jt.company_display ? " · " + esc(jt.company_display) : ""} · ${badge(SESSION_BADGE, session.status, LABELS.session_status)}</div></div>
    <div class="btn-row">
      <a class="btn" href="#/js-wizard/${esc(sessionId)}">← 向导</a>
      ${readyForMatch ? `<button class="btn" onclick="runAutoMatch('${esc(sessionId)}')">⚙ 用 AI 分析</button>` : ""}
      ${matched ? `<a class="btn primary" href="#/js-focus/${esc(sessionId)}">生成面试重点 →</a>` : ""}
    </div>
  </div>
  ${aiStatusBanner()}
  ${mv.demo ? demoStrip() : ""}
  ${session.status === "failed" ? failureArea(session) : ""}
  <div id="js-match-result"></div>

  ${!matched && !mv.rows.length ? `
    <div class="card card-pad">
      <h3>匹配分析</h3>
      ${readyForMatch ? `
        <div class="f-help mb8">前置要求：目标 JD 已录入、简历已解析。点击上方"用 AI 分析"（真实模型，语义结论可追溯到原文），或手动粘贴宿主模型输出。</div>
        <div class="btn-row">
          <button class="btn primary" onclick="runAutoMatch('${esc(sessionId)}')">⚙ 生成岗位与简历匹配</button>
          <button class="btn" onclick="showManualMatch()">✎ 手动提交模型输出 JSON</button>
        </div>
        <div id="js-manual-match" class="mt8" style="display:none">
          <label class="f">宿主模型输出 JSON（须符合 MatchSubmitPayload：batch_token / submission_id / model_label / requirements / match_rows / clarifications）</label>
          <textarea id="js-manual-json" rows="12" placeholder='粘贴符合 Schema 的 JSON'></textarea>
          <div class="btn-row mt8"><button class="btn primary" onclick="manualSubmitMatch('${esc(sessionId)}')">提交校验</button></div>
        </div>` : `
        <div class="empty">请先在向导中完成目标 JD 录入与简历解析（当前状态：${esc(LABELS.session_status[session.status] || session.status)}）。</div>`}
    </div>` : ""}

  ${jdText || resumeText ? `
    <div class="grid c2">
      <div class="card card-pad">
        <h3 style="margin:0 0 8px">JD 原文 <span class="hint">点击引用高亮定位</span></h3>
        ${renderEvidenceBox("js-jd-ev", jdText)}
      </div>
      <div class="card card-pad">
        <h3 style="margin:0 0 8px">简历原文 <span class="hint">脱敏后正文，点击引用高亮定位</span></h3>
        ${renderEvidenceBox("js-resume-ev", resumeText)}
      </div>
    </div>` : ""}

  ${mv.rows.length ? `<div class="card card-pad"><h3>匹配矩阵（${mv.rows.length} 行）</h3>${renderMatrix(mv.rows)}</div>` : ""}

  ${mv.clarifications && mv.clarifications.length ? `
    <div class="card card-pad">
      <h3>待本人确认与补充提问（${mv.clarifications.length}）</h3>
      ${mv.clarifications.map(c => `
        <div class="finding">
          <div class="st">${esc(c.question)}</div>
          <div class="small muted">为什么问：${esc(c.why_asked || "")}</div>
          ${c.answer_text ? `<div class="matrix-supp mt8"><b>已回答：</b>${esc(c.answer_text)}</div>` : `<div class="btn-row mt8"><button class="btn sm" onclick="answerClarification('${esc(c.id)}')">回答</button></div>`}
        </div>`).join("")}
      <div class="small muted">回答不会自动改矩阵状态；可重新分析以更新判定。</div>
    </div>` : ""}
  `;
  setTimeout(() => bindEvidence(main), 0);
}

async function runAutoMatch(sessionId) {
  toast("正在调用 AI 分析（未配置 AI 服务时会返回错误，不会回退为演示结果）…");
  const r = await api(`/api/js/sessions/${sessionId}/auto-match`, { method: "POST", body: {} });
  if (!handleErr(r)) return;
  if (isOutcomeOk(r.data)) { toast("匹配完成", "ok"); routeTo(); }
  else renderMatchOutcome(r.data);
}

function showManualMatch() {
  const el = $("#js-manual-match");
  if (el) el.style.display = "block";
}

async function manualSubmitMatch(sessionId) {
  let payload;
  try { payload = JSON.parse($("#js-manual-json").value); }
  catch (e) { return toast("JSON 解析失败：" + e.message, "err"); }
  const r = await api(`/api/js/sessions/${sessionId}/submit-match`, { method: "POST", body: payload });
  if (!handleErr(r)) return;
  if (isOutcomeOk(r.data)) { toast("提交校验通过", "ok"); routeTo(); }
  else renderMatchOutcome(r.data);
}

function openSupplementModal(rowId) {
  const row = window.__jsRowsById && window.__jsRowsById[rowId];
  const current = (row && row.user_supplement) ? row.user_supplement : "";
  openModal(`
    <h3>补充本人经历</h3>
    <div class="field"><label class="f">本人补充（可粘贴相关经历，视为本人确认来源）</label>
      <textarea id="js-supp-text" rows="6">${esc(current)}</textarea></div>
    <div class="field"><label class="f">可选：确认 / 修正匹配状态</label>
      <select id="js-supp-status">
        <option value="">不修改状态（仅补充，提示可重新分析）</option>
        ${Object.entries(jsLabels("match_status")).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}
      </select></div>
    <div class="field"><label class="f">原因（必填，留痕）</label>
      <input type="text" id="js-supp-reason" placeholder="例如：本人补充真实项目经历"></div>
    <div class="btn-row" style="justify-content:flex-end">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn primary" onclick="submitSupplement('${esc(rowId)}')">提交</button>
    </div>`);
}

async function submitSupplement(rowId) {
  const user_supplement = $("#js-supp-text").value.trim();
  const reason = $("#js-supp-reason").value.trim();
  const match_status = $("#js-supp-status").value || null;
  if (!user_supplement) return toast("补充内容不能为空", "err");
  if (!reason) return toast("请填写原因", "err");
  const body = { user_supplement, reason };
  if (match_status) body.match_status = match_status;
  const r = await api(`/api/js/match-rows/${rowId}/supplement`, { method: "POST", body });
  if (!handleErr(r)) return;
  closeModal();
  toast(match_status ? "已补充并确认状态" : "已补充；可重新分析以更新判定", "ok");
  routeTo();
}

function answerClarification(clarId) {
  openModal(`
    <h3>回答补充提问</h3>
    <div class="field"><label class="f">您的回答</label><textarea id="jc-answer" rows="4"></textarea></div>
    <div class="field"><label class="f">原因（留痕）</label><input type="text" id="jc-reason" placeholder="本人据实回答"></div>
    <div class="btn-row" style="justify-content:flex-end">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn primary" onclick="submitAnswerClarification('${esc(clarId)}')">提交</button>
    </div>`);
}

async function submitAnswerClarification(clarId) {
  const answer = $("#jc-answer").value.trim();
  const reason = $("#jc-reason").value.trim();
  if (!answer) return toast("回答不能为空", "err");
  if (!reason) return toast("请填写原因", "err");
  const r = await api(`/api/js/clarifications/${clarId}/answer`, { method: "POST", body: { answer_text: answer, reason } });
  if (!handleErr(r)) return;
  closeModal();
  toast("已回答；可重新分析以更新判定", "ok");
  routeTo();
}

/* ---------- 修正（复用 /api/review） ---------- */

const CORRECT_CONFIGS = {
  match_row: {
    title: "修正匹配行",
    fields: [
      { f: "match_status", label: "匹配状态", type: "select", options: () => jsLabels("match_status") },
      { f: "jd_note", label: "JD 侧说明", type: "text" },
      { f: "resume_note", label: "简历侧说明", type: "text" },
      { f: "user_supplement", label: "本人补充", type: "text" },
      { f: "reasoning", label: "判定说明", type: "textarea" },
      { f: "uncertainties", label: "仍不确定（每行一条）", type: "lines" },
      { f: "prep_advice", label: "准备建议", type: "text" },
    ],
  },
  jd_requirement: {
    title: "修正 JD 要求",
    fields: [
      { f: "category", label: "类别", type: "select", options: () => jsLabels("requirement_category") },
      { f: "text", label: "要求文本", type: "textarea" },
      { f: "judgement_note", label: "判断说明", type: "textarea" },
    ],
  },
  interview_focus: {
    title: "修正面试考察重点",
    fields: [
      { f: "category", label: "类别", type: "select", options: () => jsLabels("focus_category") },
      { f: "title", label: "标题", type: "text" },
      { f: "why_asked", label: "为什么可能被问", type: "textarea" },
      { f: "questions", label: "可能问题（JSON 数组）", type: "json" },
      { f: "materials", label: "应准备材料（每行一条）", type: "lines" },
      { f: "uncertainties", label: "仍不确定（每行一条）", type: "lines" },
    ],
  },
  prep_task: {
    title: "修正准备任务",
    fields: [
      { f: "priority", label: "优先级", type: "number" },
      { f: "purpose", label: "准备目的", type: "text" },
      { f: "actions", label: "行动（每行一条）", type: "lines" },
      { f: "suggested_hours", label: "建议投入（小时）", type: "number" },
      { f: "deliverable", label: "完成产物", type: "text" },
      { f: "self_check", label: "自检标准", type: "text" },
      { f: "learnability", label: "能力属性", type: "select", options: () => jsLabels("learnability") },
      { f: "is_practice_project", label: "练习项目（非真实经历）", type: "check" },
    ],
  },
  resume_item: {
    title: "修正简历条目",
    fields: [
      { f: "kind", label: "类别", type: "select", options: () => RESUME_ITEM_KIND_LABELS },
      { f: "title", label: "标题", type: "text" },
      { f: "org", label: "组织", type: "text" },
      { f: "period_raw", label: "时间段", type: "text" },
    ],
  },
};

function jsPrefill(targetType, id) {
  const maps = {
    match_row: window.__jsRowsById,
    jd_requirement: window.__jsReqsById,
    interview_focus: window.__jsFocusById,
    prep_task: window.__jsTasksById,
    resume_item: window.__jsItemsById,
  };
  const m = maps[targetType];
  return (m && m[id]) ? m[id] : {};
}

function renderCorrectField(f, prefill) {
  const val = prefill[f.f];
  const id = "jc-" + f.f;
  if (f.type === "select") {
    const options = f.options ? f.options() : {};
    return `<div class="field"><label class="f">${esc(f.label)}</label>
      <select id="${id}">${Object.entries(options).map(([k, v]) => `<option value="${k}" ${val === k ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></div>`;
  }
  if (f.type === "textarea") {
    return `<div class="field"><label class="f">${esc(f.label)}</label><textarea id="${id}" rows="3">${esc(val == null ? "" : val)}</textarea></div>`;
  }
  if (f.type === "lines") {
    const text = Array.isArray(val) ? val.join("\n") : "";
    return `<div class="field"><label class="f">${esc(f.label)}</label><textarea id="${id}" rows="3">${esc(text)}</textarea></div>`;
  }
  if (f.type === "json") {
    const text = (val == null) ? "" : JSON.stringify(val);
    return `<div class="field"><label class="f">${esc(f.label)}</label><textarea id="${id}" rows="3">${esc(text)}</textarea></div>`;
  }
  if (f.type === "number") {
    return `<div class="field"><label class="f">${esc(f.label)}</label><input type="number" id="${id}" value="${esc(val == null ? "" : val)}"></div>`;
  }
  if (f.type === "check") {
    return `<div class="checkline"><input type="checkbox" id="${id}" ${val ? "checked" : ""}><span>${esc(f.label)}</span></div>`;
  }
  return `<div class="field"><label class="f">${esc(f.label)}</label><input type="text" id="${id}" value="${esc(val == null ? "" : val)}"></div>`;
}

function openCorrectModalJs(targetType, id, version) {
  const cfg = CORRECT_CONFIGS[targetType];
  if (!cfg) { toast("不支持的修正类型：" + targetType, "err"); return; }
  const prefill = jsPrefill(targetType, id);
  const fieldsHtml = cfg.fields.map(f => renderCorrectField(f, prefill)).join("");
  openModal(`
    <h3>${cfg.title}</h3>
    <div class="field muted small">对象：<span class="mono">${esc(id)}</span> · 当前版本 ${version}</div>
    ${fieldsHtml}
    <div class="field"><label class="f">修改原因（必填，留痕）</label>
      <input type="text" id="jc-reason" placeholder="例如：本人确认该经历真实存在"></div>
    <div class="btn-row" style="justify-content:flex-end">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn primary" onclick="submitCorrectJs('${esc(targetType)}','${esc(id)}',${version})">提交修正</button>
    </div>`);
  window.__jsCorrectCfg = cfg;
}

async function submitCorrectJs(targetType, id, version) {
  const cfg = window.__jsCorrectCfg;
  const reason = $("#jc-reason").value.trim();
  if (!reason) return toast("请填写修改原因", "err");
  const patch = {};
  for (const f of cfg.fields) {
    const el = $("#jc-" + f.f);
    if (!el) continue;
    if (f.type === "check") { patch[f.f] = el.checked; continue; }
    if (f.type === "json") {
      const v = el.value.trim();
      if (!v) continue;
      try { patch[f.f] = JSON.parse(v); }
      catch (e) { return toast(`${f.label} JSON 解析失败：${e.message}`, "err"); }
      continue;
    }
    if (f.type === "lines") {
      const arr = el.value.split("\n").map(s => s.trim()).filter(Boolean);
      if (arr.length) patch[f.f] = arr;
      continue;
    }
    if (f.type === "number") { const v = el.value; if (v === "") continue; patch[f.f] = parseFloat(v); continue; }
    const v = el.value;
    if (v === "") continue;
    patch[f.f] = v;
  }
  const r = await api("/api/review", { method: "POST", body: { target_type: targetType, target_id: id, action: "correct", patch, reason, expected_version: version } });
  if (!handleErr(r)) return;
  closeModal();
  toast("已修正并留痕", "ok");
  routeTo();
}

/* ============================ 页面：面试重点 ============================ */

function renderFocusCard(f) {
  const questions = f.questions || [];
  const materials = f.materials || [];
  const uncertainties = f.uncertainties || [];
  const links = f.links || [];
  return `
  <div class="card card-pad focus-card">
    <div class="spread">
      <h3 style="margin:0">${badge(FOCUS_BADGE, f.category, LABELS.focus_category)}<span>${esc(f.title)}</span></h3>
      <button class="btn sm" onclick="openCorrectModalJs('interview_focus','${esc(f.id)}',${f.review_version || 0})">✎ 修正</button>
    </div>
    <div class="focus-why"><b>为什么可能被问：</b>${esc(f.why_asked || "")}</div>
    ${questions.length ? `<div class="focus-q"><b>可能问题：</b><ul>${questions.map(q => `<li>${esc(q.question || "")}${(q.followups || []).length ? `<ul>${q.followups.map(fu => `<li class="muted">${esc(fu)}</li>`).join("")}</ul>` : ""}</li>`).join("")}</ul></div>` : ""}
    ${materials.length ? `<div class="focus-m"><b>应准备材料：</b>${materials.map(esc).join("、")}</div>` : ""}
    ${uncertainties.length ? `<div class="focus-u"><b>仍不确定：</b>${uncertainties.map(esc).join("；")}</div>` : ""}
    ${links.length ? `<div class="focus-l"><b>依据：</b>${links.map(l => `<span class="badge b-gray">${esc(jsLabels("evidence_side")[l.side] || l.side)}</span> ${esc(l.note || l.ref_id || "")}`).join(" ")}</div>` : ""}
  </div>`;
}

async function renderJsFocus(main) {
  const pageSeq = __routeSeq;
  const sessionId = route.arg;
  if (!sessionId) { main.innerHTML = jsEmpty("缺少会话参数"); return; }
  const sres = await api(`/api/js/sessions/${sessionId}`);
  if (!handleErr(sres)) return;
  const session = sres.data.session;
  const fres = await api(`/api/js/sessions/${sessionId}/focus`);
  if (!handleErr(fres)) return;
  const fv = fres.data;
  if (pageSeq !== __routeSeq) return;
  window.__jsFocusById = {};
  (fv.focus || []).forEach(f => { window.__jsFocusById[f.id] = f; });

  if (pageSeq !== __routeSeq) return;
  main.innerHTML = `
  <div class="page-head">
    <div><h1>面试考察重点</h1><div class="sub">措辞限"可能考察 / 常见会被追问"，不含真题类断言；每条重点均标注不确定性。</div></div>
    <div class="btn-row">
      <a class="btn" href="#/js-match/${esc(sessionId)}">← 匹配矩阵</a>
      <button class="btn" onclick="runAutoPrep('${esc(sessionId)}')">⚙ 生成 / 重新生成</button>
      <a class="btn primary" href="#/js-prep/${esc(sessionId)}">准备清单 →</a>
    </div>
  </div>
  ${fv.demo ? demoStrip() : ""}
  ${session.status === "failed" ? failureArea(session) : ""}
  <div id="js-focus-result"></div>
  ${fv.focus && fv.focus.length ? fv.focus.map(renderFocusCard).join("") : `
    <div class="card card-pad empty"><div class="big">⌘</div>${["matched", "focus_ready", "plan_ready"].includes(session.status) ? "尚未生成面试重点，点击上方“生成 / 重新生成”。" : "请先完成匹配分析。"}</div>`}`;
}

async function runAutoPrep(sessionId) {
  toast("正在生成面试重点与准备清单…");
  const r = await api(`/api/js/sessions/${sessionId}/auto-prep`, { method: "POST", body: {} });
  if (!handleErr(r)) return;
  if (isOutcomeOk(r.data)) { toast("已生成", "ok"); routeTo(); }
  else renderPrepOutcome(r.data);
}

/* ============================ 页面：准备清单 ============================ */

function renderTaskCard(t) {
  const statusOpts = Object.entries(jsLabels("prep_status")).map(([k, v]) => `<option value="${k}" ${t.status === k ? "selected" : ""}>${esc(v)}</option>`).join("");
  return `
  <div class="card card-pad task-card ${t.is_practice_project ? "task-practice" : ""}">
    <div class="spread">
      <div><b>${t.priority}. ${esc(t.purpose || "")}</b>${t.is_practice_project ? ' <span class="practice-flag">练习项目（非真实经历）</span>' : ""}</div>
      <div class="flex">${badge(LEARN_BADGE, t.learnability, LABELS.learnability)}${badge(PREP_BADGE, t.status, LABELS.prep_status)}</div>
    </div>
    <div class="task-meta small muted mt8">建议投入 ${t.suggested_hours}h · 完成产物：${esc(t.deliverable || "")}</div>
    ${(t.actions || []).length ? `<div class="task-actions mt8"><b>行动：</b><ul>${t.actions.map(a => `<li>${esc(a)}</li>`).join("")}</ul></div>` : ""}
    <div class="task-check mt8"><b>自检标准：</b>${esc(t.self_check || "")}</div>
    ${t.resources_note ? `<div class="small muted mt8">${esc(t.resources_note)}</div>` : ""}
    <div class="btn-row mt8">
      <select class="sm" onchange="setTaskStatus('${esc(t.id)}', this.value)">${statusOpts}</select>
      <button class="btn sm" onclick="openCorrectModalJs('prep_task','${esc(t.id)}',${t.review_version || 0})">✎ 修正</button>
    </div>
  </div>`;
}

async function renderJsPrep(main) {
  const pageSeq = __routeSeq;
  const sessionId = route.arg;
  if (!sessionId) { main.innerHTML = jsEmpty("缺少会话参数"); return; }
  const pres = await api(`/api/js/sessions/${sessionId}/prep`);
  if (!handleErr(pres)) return;
  const pv = pres.data;
  const sres = await api(`/api/js/sessions/${sessionId}`);
  const session = sres.ok ? sres.data.session : {};
  const demo = session.model_label === "rule-matcher-demo-v1";
  if (pageSeq !== __routeSeq) return;
  window.__jsTasksById = {};
  (pv.tasks || []).forEach(t => { window.__jsTasksById[t.id] = t; });

  if (pageSeq !== __routeSeq) return;
  main.innerHTML = `
  <div class="page-head">
    <div><h1>准备清单</h1><div class="sub">按优先级排序；练习项目强制标注“非真实经历”，不得作为真实经历陈述。</div></div>
    <div class="btn-row">
      <a class="btn" href="#/js-focus/${esc(sessionId)}">← 面试重点</a>
      <button class="btn primary" onclick="exportPrep('${esc(sessionId)}')">导出 MD 报告</button>
    </div>
  </div>
  ${aiStatusBanner()}
  ${demo ? demoStrip() : ""}
  <div class="card card-pad">
    <h3>投入概览</h3>
    <div class="flex">
      <span class="badge b-gray">总建议 ${pv.summary.total_hours}h</span>
      <span class="badge b-green">已完成 ${pv.summary.done_hours}h</span>
      <span class="badge b-orange">剩余 ${pv.summary.remaining_hours}h</span>
      ${Object.entries(pv.summary.by_learnability || {}).map(([k, v]) => `<span class="badge b-gray">${esc(jsLabels("learnability")[k] || k)} ${v}h</span>`).join("")}
    </div>
  </div>
  <div id="js-prep-report" class="card card-pad" style="display:none"></div>
  ${pv.tasks && pv.tasks.length ? pv.tasks.map(renderTaskCard).join("") : `<div class="card card-pad empty"><div class="big">◇</div>尚未生成准备清单，请先在面试重点页生成。</div>`}`;
}

async function setTaskStatus(taskId, status) {
  const r = await api(`/api/js/prep-tasks/${taskId}/status`, { method: "POST", body: { status } });
  if (!handleErr(r)) return;
  toast("已更新状态", "ok");
  routeTo();
}

async function exportPrep(sessionId) {
  const r = await api(`/api/js/sessions/${sessionId}/report`, { method: "POST", body: { formats: ["md"] } });
  if (!handleErr(r)) return;
  const d = r.data;
  toast("报告已生成：" + d.paths.md, "ok");
  const box = $("#js-prep-report");
  if (box) {
    box.style.display = "block";
    box.innerHTML = `<h3>报告预览</h3>
      <div class="report-md">${mdToHtml(d.markdown || "")}</div>
      <div class="btn-row mt8"><a class="btn sm" href="/api/reports/${d.report_id}/file?fmt=md" download>下载 MD</a></div>`;
  }
}

/* ============================ 页面：我的资料 ============================ */

async function renderJsProfile(main) {
  const [sres, rres] = await Promise.all([api("/api/resumes/storage-info"), api("/api/resumes")]);
  if (!handleErr(sres) || !handleErr(rres)) return;
  const info = sres.data;
  const resumes = rres.data.resumes;
  main.innerHTML = `
  <div class="page-head"><div><h1>我的资料</h1><div class="sub">简历保存在本机，不云同步；删除需二次确认，删除后不可恢复。</div></div></div>
  <div class="card card-pad">
    <h3>存储位置与处理方式</h3>
    <dl class="kv">
      <dt>数据库</dt><dd class="mono">${esc(info.db_path)}</dd>
      <dt>简历文件目录</dt><dd class="mono">${esc(info.resume_store_dir)}</dd>
      <dt>报告输出</dt><dd class="mono">${esc(info.outputs_dir)}</dd>
    </dl>
    <div class="small mt8">${esc(info.processing_note)}</div>
    <div class="small muted mt8">${esc(info.redaction_note)}</div>
    <div class="small muted mt8">${esc(info.retention_note)}</div>
    <div class="small muted mt8">${esc(info.delete_note)}</div>
  </div>
  <div class="card card-pad">
    <div class="spread mb8">
      <h3 style="margin:0">我的简历（${resumes.length}）</h3>
      <button class="btn danger" onclick="deleteAllResumes()">删除全部简历</button>
    </div>
    ${resumes.length ? `<table class="tbl">
      <tr><th>名称</th><th>来源</th><th>解析状态</th><th>字符数</th><th>创建时间</th><th>状态</th><th>操作</th></tr>
      ${resumes.map(rs => `<tr>
        <td><b>${esc(rs.label)}</b><br><span class="mono">${esc(String(rs.id).slice(0, 14))}…</span></td>
        <td class="muted">${esc(rs.source_type)}</td>
        <td>${badge(PARSE_BADGE, rs.parse_status, LABELS.parse_status)}</td>
        <td>${rs.char_count}</td>
        <td class="small muted">${fmtTime(rs.created_at)}</td>
        <td>${rs.deleted_at ? '<span class="badge b-red">已删除</span>' : '<span class="badge b-green">正常</span>'}</td>
        <td><div class="btn-row">
          ${rs.deleted_at ? "" : `<button class="btn sm" onclick="openResumeDrawer('${esc(rs.id)}')">查看/校对</button>`}
          ${rs.deleted_at ? "" : `<button class="btn sm danger" onclick="deleteResume('${esc(rs.id)}')">删除</button>`}
        </div></td>
      </tr>`).join("")}</table>` : `<div class="empty"><div class="big">◇</div>还没有简历，可到“新建准备”中导入。</div>`}
  </div>`;
}

async function openResumeDrawer(resumeId) {
  const r = await api(`/api/resumes/${resumeId}`);
  if (!handleErr(r)) return;
  const { resume, items } = r.data;
  window.__jsItemsById = {};
  items.forEach(it => { window.__jsItemsById[it.id] = it; });
  openDrawer(`
    <span class="close" onclick="closeDrawer()">✕</span>
    <h2>${esc(resume.label)}</h2>
    <div class="muted small mb8">${badge(PARSE_BADGE, resume.parse_status, LABELS.parse_status)} · ${resume.char_count} 字符 · 来源 ${esc(resume.source_type)}</div>
    ${resume.redaction_note ? `<div class="small muted mb8">${esc(resume.redaction_note)}</div>` : ""}
    <h3 style="font-size:14px;margin:12px 0 8px">简历正文（脱敏后，偏移基准）</h3>
    <div class="evidence-box" id="js-resume-ev">${renderEvidence(resume.evidence_text || "")}</div>
    <h3 style="font-size:14px;margin:16px 0 8px">结构化条目（${items.length}）</h3>
    ${items.length ? items.map(it => `<div class="finding">
      <div class="st">${badge(RESUME_ITEM_KIND_BADGE, it.kind, RESUME_ITEM_KIND_LABELS)} ${esc(it.title)}</div>
      <div class="small muted">${esc(it.org || "")}${it.period_raw ? " · " + esc(it.period_raw) : ""} · 来源 ${esc(it.source)}</div>
      ${it.start != null ? `<span class="mono muted">[${it.start},${it.end})</span>` : ""}
      <div class="btn-row mt8"><button class="btn sm" onclick="openCorrectModalJs('resume_item','${esc(it.id)}',${it.review_version || 0})">✎ 修正</button></div>
    </div>`).join("") : `<div class="empty">未解析出条目</div>`}
  `);
}

async function deleteResume(resumeId) {
  if (!confirm("确认删除该简历？删除后正文与文件一并清除，不可恢复。")) return;
  const r = await api(`/api/resumes/${resumeId}`, { method: "DELETE", body: { confirm: true } });
  if (!handleErr(r)) return;
  toast("已删除", "ok");
  routeTo();
}

async function deleteAllResumes() {
  if (!confirm("确认删除全部简历？该操作不可恢复。")) return;
  const r = await api("/api/resumes", { method: "DELETE", body: { confirm: true } });
  if (!handleErr(r)) return;
  toast(`已删除 ${r.data.deleted} 份简历`, "ok");
  routeTo();
}

/* ============================ 注册与启动 ============================ */

function initJobseeker() {
  PAGES.jobseeker.push(
    { id: "js-sessions", ico: "☰", name: "面试准备" },
    { id: "js-wizard", ico: "＋", name: "新建准备" },
    { id: "js-profile", ico: "▦", name: "我的资料" },
  );
  RENDER["js-sessions"] = renderJsSessions;
  RENDER["js-wizard"] = renderJsWizard;
  RENDER["js-match"] = renderJsMatch;
  RENDER["js-focus"] = renderJsFocus;
  RENDER["js-prep"] = renderJsPrep;
  RENDER["js-profile"] = renderJsProfile;
  // 阶段二（P1）占位，尚未开放，避免直接访问报错。
  RENDER["js-demand"] = async function (main) {
    main.innerHTML = jsEmpty("行业需求概览为阶段二功能，尚未开放。");
  };
  renderModeSwitch();
  loadAiStatus();
}

// AI 服务配置状态（供匹配/面试页面提示；未配置时分析会返回 AI_NOT_CONFIGURED，不回退 mock）
async function loadAiStatus() {
  try {
    const r = await api("/api/ai/status");
    if (r.ok) window.__aiStatus = r.data;
  } catch (e) { /* 静默 */ }
}

function aiStatusBanner() {
  const s = window.__aiStatus;
  if (s && !s.configured) {
    return `<div class="warn-strip">⚠ AI 服务未配置：匹配分析与面试准备不可用（不会回退为演示结果）。请在「我的资料 → 设置」配置模型后重试；文件解析结果不受影响。</div>`;
  }
  if (s && s.configured) {
    return `<div class="hint">AI 分析将由真实模型执行：${esc(s.provider || "")} / ${esc(s.model || "")}。所有结论可追溯到 JD 与简历原文。</div>`;
  }
  return "";
}

initJobseeker();
