/* Search → frozen JD → selected social evidence → resume → interview plan. */
const INTERVIEW_BUSY = new Set();
let INTERVIEW_PICK = null;

function interviewRemember(id) { localStorage.setItem('finintel-interview-session', id); }
function interviewActive() { return localStorage.getItem('finintel-interview-session') || ''; }

async function interviewStart(jobId, runId, btn) {
  if (INTERVIEW_BUSY.has(jobId)) return;
  INTERVIEW_BUSY.add(jobId);
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/interview/start', {method:'POST', body:{job_id:jobId,run_id:runId}});
    if (!handleErr(r)) return;
    const id = r.data.session.id;
    interviewRemember(id);
    closeDrawer();
    location.hash = '#/js-wizard/' + id;
    toast('已保存本次搜索的目标 JD，接下来选择背景材料和简历', 'ok');
  } finally {
    INTERVIEW_BUSY.delete(jobId);
    if (btn) btn.disabled = false;
  }
}

async function interviewChooseFinding(findingId, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/js/sessions');
    if (!handleErr(r)) return;
    INTERVIEW_PICK = findingId;
    const sessions = r.data.sessions.filter(s => s.route === 'match');
    openModal(`<h2>加入哪一份面试准备？</h2>
      <p class="muted">只添加这条结论背后的保存原文，最多 8 条证据。公开讨论用于背景练习。</p>
      <select id="interview-session-choice" style="width:100%">${sessions.map(s => `<option value="${esc(s.id)}" ${s.id === interviewActive() ? 'selected' : ''}>${esc(s.title || '尚未选择岗位')}${s.company_display ? ' · '+esc(s.company_display) : ''} · ${esc(fmtTime(s.updated_at))}</option>`).join('')}</select>
      <div class="btn-row mt14"><button class="btn primary" onclick="interviewConfirmFinding(this)" ${sessions.length ? '' : 'disabled'}>加入并打开准备</button>
      <button class="btn" onclick="interviewNewWithFinding(this)">新建一份准备</button><button class="btn" onclick="closeModal()">取消</button></div>`);
  } finally { if (btn) btn.disabled = false; }
}

async function interviewConfirmFinding(btn) {
  const id = $('#interview-session-choice').value;
  if (!id || !INTERVIEW_PICK) return;
  btn.disabled = true;
  try {
    if (await interviewAttach(id, INTERVIEW_PICK)) {
      interviewRemember(id); closeModal(); location.hash = '#/js-wizard/' + id;
      if (route.page === 'js-wizard' && route.arg === id) routeTo();
    }
  } finally { btn.disabled = false; }
}

async function interviewNewWithFinding(btn) {
  btn.disabled = true;
  try {
    const r = await api('/api/js/sessions', {method:'POST',body:{route:'match'}});
    if (!handleErr(r)) return;
    const id = r.data.session.session.id;
    if (await interviewAttach(id, INTERVIEW_PICK)) {
      interviewRemember(id); closeModal(); location.hash = '#/js-wizard/' + id;
    }
  } finally { btn.disabled = false; }
}

async function interviewAttach(sessionId, findingId) {
  const r = await api(`/api/js/sessions/${encodeURIComponent(sessionId)}/research`, {method:'POST',body:{finding_id:findingId}});
  if (!handleErr(r)) return false;
  toast(`已选 ${r.data.materials.length} 条背景证据；生成计划时会一并使用`, 'ok');
  return true;
}

async function interviewRemove(sessionId, materialId, btn) {
  btn.disabled = true;
  try {
    const r = await api(`/api/js/sessions/${encodeURIComponent(sessionId)}/research/${encodeURIComponent(materialId)}`, {method:'DELETE'});
    if (handleErr(r)) routeTo();
  } finally { btn.disabled = false; }
}

function interviewSourceLink(row) {
  const safe = sfSocialSafeUrl(row.source_url);
  return `<a class="btn sm" href="#/result/${esc(row.source_run_id)}">回到搜索结果</a>`
    + (safe ? `<a class="btn sm" href="${esc(safe)}" target="_blank" rel="noopener noreferrer">查看公开原帖</a>` : '');
}

function interviewMaterialCard(row, sessionId) {
  return `<div class="finding"><b>${esc(row.title)}</b> <span class="badge b-gray">${esc(sfPlatformLabel(row.platform))}</span>
    <div class="small muted mt8">保存的${row.comment_snapshot_id ? '评论' : '正文'}证据 · ${row.source_status === 'complete' ? '完整正文' : '部分材料'} · [${row.start}, ${row.end})</div>
    <blockquote>${esc(row.quote)}</blockquote>
    <details><summary>查看保存的原文上下文</summary><div class="evidence-box" style="white-space:pre-wrap">${esc(row.context_text)}</div></details>
    <div class="btn-row mt8">${interviewSourceLink(row)}<button class="btn sm" onclick="interviewRemove('${esc(sessionId)}','${esc(row.id)}',this)">移除</button></div></div>`;
}

function interviewBackgroundCard(view, sessionId) {
  return `<div class="card card-pad" id="interview-background"><div class="spread"><h3>② X／小红书背景研究（${view.materials.length}/8）</h3>
    <button class="btn sm" onclick="interviewBrowse('${esc(sessionId)}')">选择已有搜索材料</button></div>
    <p class="small muted">${esc(view.boundary)}</p>
    ${view.materials.length ? view.materials.map(m => interviewMaterialCard(m,sessionId)).join('') : '<div class="empty">还未选择背景材料。可从已有结果选入，也可以先搜索再点击「加入面试准备」。</div>'}
    <div class="btn-row mt8"><button class="btn" onclick="interviewResearchSearch('${esc(sessionId)}','x')">去 X 搜索背景</button>
      <button class="btn" onclick="interviewResearchSearch('${esc(sessionId)}','xiaohongshu')">去小红书搜索讨论</button></div>
    <p class="small muted mt8">材料可选。没有材料时，计划仍基于 JD 与简历生成。</p></div>`;
}

async function interviewBrowse(sessionId) {
  const r = await api('/api/social/history?limit=50');
  if (!handleErr(r)) return;
  const runs = r.data.runs;
  openModal(`<h2>选择已有搜索材料</h2><p class="muted">先选择一次 X／小红书搜索，再选择有原文依据的结论。</p>
    ${runs.length ? `<select id="interview-run-choice" style="width:100%" onchange="interviewLoadFindings('${esc(sessionId)}',this.value)"><option value="">请选择搜索记录</option>${runs.map(r => `<option value="${esc(r.run_id || r.id)}">${esc(sfTaskTypeLabel(r.task_type))} · ${esc(r.query || r.query_text || '')} · ${esc(fmtTime(r.searched_at || r.started_at))}</option>`).join('')}</select>` : '<div class="empty">暂无保存的社交搜索结果，请先搜索。</div>'}
    <div id="interview-finding-list" class="mt14" style="max-height:45vh;overflow:auto"></div>
    <div class="btn-row mt14"><button class="btn" onclick="closeModal();routeTo()">完成选择，返回准备</button></div>`);
}

let INTERVIEW_FINDING_REQ = 0;
async function interviewLoadFindings(sessionId, runId) {
  const seq = ++INTERVIEW_FINDING_REQ;
  const host = $('#interview-finding-list');
  if (!host) return;
  if (!runId) { host.innerHTML = ''; return; }
  host.innerHTML = '<div class="muted">正在读取保存的结论…</div>';
  const r = await api(`/api/tasks/${encodeURIComponent(runId)}/result`);
  if (seq !== INTERVIEW_FINDING_REQ || !document.contains(host)) return;
  if (!r.ok) { host.innerHTML = '<div class="err-box">读取结论失败，请重新选择搜索记录。</div>'; return; }
  const findings = r.data.findings || [];
  host.innerHTML = findings.length ? findings.map(f => `<div class="finding"><b>${esc(f.title)}</b><p>${esc(f.summary_zh || '')}</p>
    <button class="btn sm" onclick="interviewAddFromPicker('${esc(sessionId)}','${esc(f.id)}',this)">选择这条依据</button></div>`).join('') : '<div class="empty">这次搜索尚无带原文依据的结论。</div>';
}

async function interviewAddFromPicker(sessionId,findingId,btn) {
  btn.disabled = true;
  if (await interviewAttach(sessionId,findingId)) btn.textContent = '已加入';
  else btn.disabled = false;
}

function interviewResearchSearch(sessionId,source) {
  interviewRemember(sessionId);
  SF_DRAFT.source = source;
  SF_DRAFT.reuse_run_id = null;
  const title = $('#interview-workspace')?.dataset.title || '';
  SF_DRAFT.query = title;
  location.hash = '#/?keyword=' + encodeURIComponent(title);
}

function interviewPlanBackground(view, sessionId) {
  if (!view) return '';
  return `<div class="card card-pad"><div class="spread"><h3>公开讨论背景练习</h3><a class="btn sm" href="#/js-wizard/${esc(sessionId)}">管理目标与背景材料</a></div>
    ${view.plan_stale ? '<div class="warn-strip">JD、简历或背景材料已改变，当前保留的计划已过期。请先完成必要的匹配，再重新生成计划。</div>' : ''}
    <p class="small muted">${esc(view.boundary)}</p>
    ${(view.questions || []).map(q => `<div class="finding"><b>${esc(q.question)}</b><p>${esc(q.why_relevant)}</p><blockquote>${esc(q.quote)}</blockquote>
      <div class="small muted">局限：${q.limitations.map(esc).join('；')}</div><div class="btn-row mt8">${interviewSourceLink(q)}</div></div>`).join('') || '<div class="muted">尚无背景练习问题。选择材料后重新生成，或仅使用 JD 与简历准备。</div>'}
    <div class="btn-row mt8"><button class="btn" onclick="runAutoPrep('${esc(sessionId)}')">重新生成准备计划</button></div></div>`;
}

const interviewOldWizard = RENDER['js-wizard'];
RENDER['js-wizard'] = async function(main) {
  const seq = __routeSeq;
  const id = route.arg;
  await interviewOldWizard(main);
  if (!id || seq !== __routeSeq || route.arg !== id || route.page !== 'js-wizard') return;
  const r = await api(`/api/js/sessions/${encodeURIComponent(id)}`);
  if (!r.ok || seq !== __routeSeq || route.arg !== id || route.page !== 'js-wizard') return;
  interviewRemember(id);
  const d = r.data;
  const cards = main.querySelectorAll('.card.card-pad');
  if (!cards.length) return;
  cards[0].insertAdjacentHTML('afterend',interviewBackgroundCard(d.research_background,id));
  const source = d.session.origin_run_id;
  main.querySelector('.page-head').insertAdjacentHTML('afterend',`<div id="interview-workspace" class="card card-pad" data-title="${esc(d.job_target?.title || '')}"><b>目标岗位 → 背景研究 → 简历 → 匹配 → 准备计划</b><div class="btn-row mt8">${source ? `<a class="btn sm" href="#/result/${esc(source)}">返回目标岗位搜索</a>` : ''}<a class="btn sm" href="#/">继续搜索</a></div>
    ${d.job_target?.detail_status === 'partial' ? '<p class="small">目标 JD 只取得部分正文，匹配与准备覆盖有限，建议补充完整职责和要求。</p>' : ''}</div>`);
};

for (const page of ['js-focus','js-prep']) {
  const old = RENDER[page];
  RENDER[page] = async function(main) {
    const seq = __routeSeq;
    const id = route.arg;
    await old(main);
    if (!id || seq !== __routeSeq || route.arg !== id || route.page !== page) return;
    const r = await api(`/api/js/sessions/${encodeURIComponent(id)}/research`);
    if (r.ok && seq === __routeSeq && route.arg === id && route.page === page) {
      main.querySelector('.page-head')?.insertAdjacentHTML('afterend',interviewPlanBackground(r.data,id));
    }
  };
}

for (const page of ['home','result']) {
  const old = RENDER[page];
  RENDER[page] = async function(main) {
    const seq = __routeSeq;
    await old(main);
    if (seq !== __routeSeq || route.page !== page) return;
    const id = interviewActive();
    if (!id) return;
    const r = await api(`/api/js/sessions/${encodeURIComponent(id)}`);
    if (!r.ok || seq !== __routeSeq || route.page !== page) return;
    main.insertAdjacentHTML('afterbegin',`<div class="card card-pad"><div class="spread"><div><b>正在为 ${esc(r.data.job_target?.title || '目标岗位')} 准备面试</b><div class="small muted">在搜索结论中点击「加入面试准备」，然后返回工作区生成计划。</div></div><a class="btn sm" href="#/js-wizard/${esc(id)}">返回面试准备 →</a></div></div>`);
  };
}
