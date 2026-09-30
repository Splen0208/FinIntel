/* finintel 前端单页应用
 * 原生 JS，无构建步骤。主流程（simple.js）：#/ 搜索、#/history 历史、#/result/:id 结果。
 * 旧页面保留在诊断壳：#/diag/dashboard、#/diag/runs、#/diag/jobs、#/diag/analysis、
 * #/diag/companies、#/diag/review、#/diag/reports、#/diag/settings。
 */
"use strict";

/* ============================ 基础工具 ============================ */

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function esc(s) {
  if (s === null || s === undefined) return "";
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

async function api(path, options) {
  const opt = Object.assign({ headers: { "Content-Type": "application/json" } }, options || {});
  if (opt.body && typeof opt.body !== "string") opt.body = JSON.stringify(opt.body);
  const res = await fetch(path, opt);
  const json = await res.json().catch(() => ({ ok: false, error: { code: "PARSE", message: "响应解析失败" } }));
  return json;
}

function toast(msg, type) {
  const el = document.createElement("div");
  el.className = "toast " + (type || "");
  el.innerHTML = esc(msg);
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), type === "err" ? 6500 : 3800);
}

function handleErr(json, fallback) {
  if (json && json.ok === false && json.error) {
    let m = `[${json.error.code}] ${json.error.message}`;
    if (json.error.operator_action) m += `\n→ ${json.error.operator_action}`;
    toast(m, "err");
    return true;
  }
  if (fallback) toast(fallback, "err");
  return !!(json && json.ok);
}

/* 标签与字典 */
let LABELS = {
  role_class: {}, relevance: {}, action_type: {}, result_status: {},
  detail_status: {}, run_state: {}, support_level: {}, review_status: {},
};
let TAXONOMY = {};
const DOMAIN_BADGE = {
  acquisition_channel: "b-teal", credit_risk_pricing: "b-purple", borrowing_conversion: "b-blue",
  billing_repayment: "b-green", postloan_service: "b-orange", funding_settlement: "b-blue",
  b2b_technology: "b-purple", overseas_credit: "b-teal", data_platform: "b-gray", other: "b-gray",
};
function domainBadge(d) { return `<span class="badge ${DOMAIN_BADGE[d] || "b-gray"}">${esc(TAXONOMY[d] || d)}</span>`; }
function lbl(kind, v) {
  if (!v) return `<span class="badge b-gray">—</span>`;
  const map = { role: LABELS.role_class, rel: LABELS.relevance, act: LABELS.action_type,
    res: LABELS.result_status, det: LABELS.detail_status, run: LABELS.run_state,
    sup: LABELS.support_level, rev: LABELS.review_status };
  const t = map[kind] && map[kind][v];
  return t ? esc(t) : esc(v);
}
const ROLE_BADGE = { product: "b-blue", engineering: "b-green", algorithm_data: "b-purple", sales: "b-red", other: "b-gray", unknown: "b-gray" };
const REL_BADGE = { direct: "b-green", adjacent: "b-orange", irrelevant: "b-red", unknown: "b-gray" };
const RES_BADGE = { findings_found: "b-green", insufficient_information: "b-gray", excluded: "b-red", needs_review: "b-orange" };
const RUN_BADGE = { planned: "b-gray", collecting: "b-blue", ready_for_analysis: "b-purple", analyzing: "b-purple",
  completed: "b-green", partial: "b-orange", completed_partial: "b-orange",
  auth_required: "b-red", risk_stopped: "b-red", rate_limited: "b-red", budget_stopped: "b-orange", stopped: "b-gray", failed: "b-red" };
const SUP_BADGE = { single: "b-gray", multi: "b-green", unverified: "b-orange" };
const REV_BADGE = { unreviewed: "b-gray", confirmed: "b-green", corrected: "b-blue", excluded: "b-red" };
const DET_BADGE = { complete: "b-green", missing: "b-orange", partial: "b-orange", failed: "b-red", too_large: "b-red", reused: "b-teal", pending: "b-gray" };

function badge(map, v, labelMap) {
  if (!v) return `<span class="badge b-gray">—</span>`;
  return `<span class="badge ${map[v] || "b-gray"}">${esc((labelMap && labelMap[v]) || v)}</span>`;
}

const LAYER_STATUS_MAP = {
  ok: ["b-green", "已验证"], stale: ["b-orange", "已过期"], error: ["b-red", "异常"],
  unavailable: ["b-red", "不可用"], unknown: ["b-gray", "未验证"], demo: ["b-teal", "演示"],
};
const LAYER_NAMES = {
  cli_path: "① CLI 路径可用", mcp_protocol: "② MCP 协议状态", local_credential: "③ 本地认证材料",
  online_auth: "④ 在线认证", recent_search: "⑤ 最近真实搜索", recent_detail: "⑥ 最近真实详情",
};
function layerBadge(layer) {
  const [cls, txt] = LAYER_STATUS_MAP[layer.status] || ["b-gray", layer.status];
  return `<span class="badge ${cls}">${esc(txt)}</span>`;
}
function layerRow(key, layer) {
  const actions = (layer.operator_actions || []).slice(0, 3)
    .map(a => `<div class="small muted">→ ${esc(a)}</div>`).join("");
  const err = layer.error_code
    ? `<div class="small">错误码 <span class="mono">${esc(layer.error_code)}</span>${layer.error_message ? " · " + esc(layer.error_message) : ""}</div>`
    : "";
  return `<tr>
    <td style="width:150px">${esc(LAYER_NAMES[key] || key)}</td>
    <td style="width:110px">${layerBadge(layer)}${layer.stale ? ' <span class="badge b-gray">待验证</span>' : ""}</td>
    <td style="width:170px" class="small muted">${fmtTime(layer.checked_at)}</td>
    <td class="small">${esc(layer.detail || "")}${err}${actions}</td></tr>`;
}

function fmtTime(iso) {
  if (!iso) return "—";
  return iso.replace("T", " ").replace("Z", " UTC").slice(0, 19);
}
function fmtShort(id) { return id ? `<span class="mono">${esc(String(id).slice(0, 14))}…</span>` : "—"; }

/* ============================ 布局与路由 ============================ */

/* 主导航收敛为 3 项（搜索 / 历史 / 面试准备）；全部旧页面降级到诊断壳 #/diag/<id> */
const PAGES = {
  research: [
    { id: "home", ico: "⌕", name: "搜索" },
    { id: "history", ico: "◷", name: "历史" },
    { id: "js-sessions", ico: "☰", name: "面试准备" },
  ],
  diag: [
    { id: "dashboard", ico: "◈", name: "健康分层" },
    { id: "runs", ico: "RITE", name: "采集运行" },
    { id: "jobs", ico: "☰", name: "岗位库" },
    { id: "analysis", ico: "⌘", name: "分析工作台" },
    { id: "companies", ico: "▦", name: "公司方向" },
    { id: "review", ico: "✎", name: "人工复核", cntKey: "pendingReviews" },
    { id: "reports", ico: "▤", name: "报告中心" },
    { id: "settings", ico: "⚙", name: "设置" },
  ],
  jobseeker: [],
};

let pendingReviewCount = 0;

function navHref(p, diag) {
  if (diag) return `#/diag/${p.id}${p.id === "runs" && route.arg ? "/" + route.arg : ""}`;
  return p.id === "home" ? "#/" : `#/${p.id}`;
}

function renderNav() {
  const diag = !!route.diag;
  const pages = diag ? PAGES.diag : PAGES.research;
  const html = pages.map(p => `
    <a class="nav-item ${route.page === p.id ? "active" : ""}" href="${navHref(p, diag)}">
      <span class="ico">${p.ico}</span><span>${p.name}</span>
      ${p.cntKey === "pendingReviews" && pendingReviewCount ? `<span class="cnt">${pendingReviewCount}</span>` : ""}
    </a>`).join("");
  $("#nav").innerHTML = diag
    ? `${html}<a class="nav-item nav-back" href="#/">← 返回搜索</a>`
    : html;
}

const route = { page: "home", arg: null, query: {}, diag: false };

/* 解析 hash：先按第一个 "?" 分离 path 与 query，避免 #/runs?create=1 把 page 解析成 "runs?create=1" */
function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const qi = raw.indexOf("?");
  const path = qi >= 0 ? raw.slice(0, qi) : raw;
  const qs = qi >= 0 ? raw.slice(qi + 1) : "";
  const parts = path.split("/").filter(s => s !== "");
  route.page = parts[0] || "home";
  route.arg = parts.slice(1).join("/") || null;
  const query = {};
  try {
    new URLSearchParams(qs).forEach((v, k) => { query[k] = v; });
  } catch (e) { /* 忽略非法查询串 */ }
  route.query = query;
  route.diag = false;
}

/* 旧深链接兼容跳转表（设计 §10 C）。返回目标 hash；返回 null 表示不跳转。 */
function legacyDiagTarget(page) {
  return `#/diag/${page}${route.arg ? "/" + route.arg : ""}`;
}

function legacyRedirectTarget(page) {
  if (route.diag) return null;                       // 诊断页内部不再重定向
  if (page.startsWith("js-")) return null;           // 求职者页面保持原样
  switch (page) {
    case "dashboard": return "#/";
    case "jobs": case "analysis": case "companies":
    case "review": case "reports": case "settings":
      return legacyDiagTarget(page);
    // 首页的新语义路由：关键词带入搜索首页
    case "new":
      return "#/" + (route.query.keyword ? "?keyword=" + encodeURIComponent(route.query.keyword) : "");
    case "runs":
      if (route.query.create) return "#/diag/runs?create=1";
      if (route.arg) return null;                    // 按来源分流，见 resolveLegacyRedirect
      return "#/history";
    default: return null;
  }
}

/* #/runs/:id 需按来源分流：simple → 结果页；否则 → 诊断页运行详情 */
async function resolveLegacyRedirect() {
  const direct = legacyRedirectTarget(route.page);
  if (direct) return direct;
  if (!route.diag && route.page === "runs" && route.arg) {
    try {
      const r = await api(`/api/tasks/${encodeURIComponent(route.arg)}`);
      if (r.ok && r.data && r.data.task) return `#/result/${encodeURIComponent(route.arg)}`;
    } catch (e) { /* 接口未就绪时回落到诊断页 */ }
    return `#/diag/runs/${encodeURIComponent(route.arg)}`;
  }
  return null;
}

let __routeSeq = 0;

async function routeTo() {
  const seq = ++__routeSeq;
  parseHash();
  // #/diag/<原页面>[/<arg>] → 诊断壳 + 原页面
  if (route.page === "diag") {
    const seg = (route.arg || "").split("/").filter(s => s !== "");
    route.page = seg[0] || "dashboard";
    route.arg = seg.slice(1).join("/") || null;
    route.diag = true;
  }
  const target = await resolveLegacyRedirect();
  if (seq !== __routeSeq) return;                    // 已有更新的导航
  if (target) { location.replace(target); return; }

  renderNav();
  if (typeof renderTopbar === "function") renderTopbar();
  const main = $("#main");
  const fn = RENDER[route.page];
  if (!fn) {
    main.innerHTML = `<div class="sf-page"><div class="empty"><div class="big">🧭</div>
      没有这个页面：<span class="mono">${esc(route.page)}</span></div>
      <div class="btn-row" style="justify-content:center"><a class="btn primary" href="#/">回到搜索首页</a></div></div>`;
    return;
  }
  main.innerHTML = `<div class="empty"><div class="big">…</div>加载中</div>`;
  try {
    await fn(main);
  } catch (e) {
    if (seq !== __routeSeq) return;
    console.error(e);
    main.innerHTML = `<div class="empty"><div class="big">⚠</div>页面加载失败：${esc(e.message)}</div>`;
  }
}

window.addEventListener("hashchange", routeTo);

/* ============================ 页面：总览 ============================ */

const RENDER = {};

RENDER.dashboard = async function (main) {
  const h = await api("/api/health");
  if (!handleErr(h)) return;
  const { layers, stats, versions, taxonomy, data_dir, integration_state } = h.data;
  TAXONOMY = taxonomy;
  window.__stats = stats;
  window.__health = h.data;
  const ready = integration_state === "ready";
  const dirConsistent = data_dir.consistent === true;

  main.innerHTML = `
  <div class="page-head">
    <div><h1>总览</h1><div class="sub">分层接入状态分别记录，不合并成一个"连接成功"。演示数据源为合成数据，
      仅用于验证从岗位文本到报告的完整处理链路，不代表任何真实公司动态。</div></div>
    <div class="btn-row">
      <a class="btn primary" href="#/runs?create=1" onclick="localStorage.setItem('fin_create_run','1')">＋ 新建研究运行</a>
    </div>
  </div>

  <div class="card card-pad ${ready ? "" : "banner-warn"}">
    <div class="spread">
      <div><h3 style="margin:0">BOSS 真实接入状态：${ready ? "证据已齐全" : "未完成"}</h3>
        <div class="small muted mt8">${esc(h.data.notice || "")}</div></div>
      <a class="btn sm" href="#/settings">打开设置查看安装/登录指引 →</a>
    </div>
  </div>

  <div class="grid c4">
    <div class="card stat"><div class="num">${stats.companies}</div><div class="lbl">公司实体</div></div>
    <div class="card stat"><div class="num">${stats.jobs_real}</div><div class="lbl">真实岗位</div></div>
    <div class="card stat"><div class="num">${stats.jobs_demo}</div><div class="lbl">演示岗位（隔离）</div></div>
    <div class="card stat"><div class="num">${stats.findings}</div><div class="lbl">工作事项</div></div>
  </div>

  <div class="card card-pad mt14">
    <h3>接入状态分层 <span class="hint">各层独立记录，带上次检查时间；刷新首页不发起平台搜索/详情</span></h3>
    <table class="tbl">
      <tr><th>能力层</th><th>状态</th><th>上次检查</th><th>说明 / 待办</th></tr>
      ${Object.keys(LAYER_NAMES).map(k => layerRow(k, layers[k] || { status: "unknown" })).join("")}
    </table>
    <div class="small muted mt8">
      当前生效 data-dir：<span class="mono">${esc(data_dir.path)}</span> ·
      来源：${data_dir.source === "setting" ? "数据库设置" : data_dir.source === "env" ? "环境变量" : "默认目录"} ·
      上游回显一致：${dirConsistent ? "是" : (data_dir.upstream_echo ? "否" : "未探测")} ·
      网站链路：${esc(versions.website_channel)}${versions.mcp_role ? "（" + esc(versions.mcp_role) + "）" : ""}
    </div>
    <div class="btn-row mt8">
      <button class="btn" onclick="probeHealth(['cli_path','local_credential'], false)">重新检查（本地诊断）</button>
      <button class="btn" onclick="probeHealth(['cli_path','local_credential','online_auth'], true)">重新检查（含在线认证）</button>
    </div>
    <div class="small mt8">版本：上游 ${esc(versions.upstream)} · 分类字典 ${esc(versions.taxonomy)} · 证据映射 ${esc(versions.mapping)}</div>
  </div>

  <div class="card card-pad">
    <h3>最近运行</h3><div id="recent-runs"></div>
  </div>`;
  renderRecentRuns();
};

async function probeHealth(layers, online) {
  const body = { layers, timeout_seconds: 60 };
  if (!online) body.layers = layers.filter(l => l !== "online_auth");
  toast("正在执行本地诊断（不发起搜索/详情）…");
  const r = await api("/api/health/probe", { method: "POST", body });
  if (!handleErr(r)) return;
  toast("诊断完成，已刷新分层状态", "ok");
  routeTo();
}

async function renderRecentRuns() {
  const r = await api("/api/runs");
  if (!r.ok) return;
  const runs = r.data.runs.slice(0, 6);
  $("#recent-runs").innerHTML = runs.length ? `
    <table class="tbl">
      <tr><th>名称</th><th>状态</th><th>来源</th><th>查询</th><th>岗位</th><th>开始时间</th><th></th></tr>
      ${runs.map(u => `<tr class="rowlink" onclick="location.hash='#/runs/${u.id}'">
        <td><b>${esc(u.name)}</b><br>${fmtShort(u.id)}</td>
        <td>${badge(RUN_BADGE, u.state, LABELS.run_state)}</td>
        <td class="muted">${u.config_snapshot.source === "demo" ? "演示数据" : "BOSS(真实)"}</td>
        <td class="small muted">${(u.config_snapshot.queries || []).map(q => esc(q.text)).join("、")}</td>
        <td>${u.counters.jobs_unique || 0}</td>
        <td class="small muted">${fmtTime(u.started_at)}</td>
        <td><a class="btn sm" href="#/runs/${u.id}">打开</a></td>
      </tr>`).join("")}
    </table>` : `<div class="empty"><div class="big">◇</div>还没有运行。点击右上角"新建研究运行"开始。</div>`;
}

/* ============================ 页面：采集运行 ============================ */

RENDER.runs = async function (main) {
  if (route.arg) return renderRunDetail(main, route.arg);
  const r = await api("/api/runs");
  if (!handleErr(r)) return;
  const runs = r.data.runs;
  main.innerHTML = `
  <div class="page-head">
    <div><h1>采集运行</h1><div class="sub">每次运行使用配置快照：查询计划、预算在创建时固定，后续配置修改不反向改写历史。
      搜索/详情调用均计入预算（含失败重试）。</div></div>
    <button class="btn primary" onclick="openCreateRun()">＋ 新建运行</button>
  </div>
  <div class="card card-pad">
    ${runs.length ? `<table class="tbl">
      <tr><th>名称</th><th>状态</th><th>来源</th><th>查询计划</th><th>搜索/预算</th><th>详情/预算</th><th>事项</th><th>开始时间</th><th></th></tr>
      ${runs.map(u => `<tr class="rowlink" onclick="location.hash='#/runs/${u.id}'">
        <td><b>${esc(u.name)}</b><br>${fmtShort(u.id)}</td>
        <td>${badge(RUN_BADGE, u.state, LABELS.run_state)}${u.stop_reason ? `<div class="small muted mt8">${esc(u.stop_reason)}</div>` : ""}</td>
        <td class="muted">${u.config_snapshot.source === "demo" ? "演示数据" : "BOSS(真实)"}</td>
        <td class="small muted">${(u.config_snapshot.queries || []).map(q => esc(q.text)).join("、")}</td>
        <td>${u.counters.search_calls || 0} / ${u.limits.max_search_calls}</td>
        <td>${u.counters.detail_calls || 0} / ${u.limits.max_detail_calls}</td>
        <td>${u.counters.snapshots_complete || 0}</td>
        <td class="small muted">${fmtTime(u.started_at)}</td>
        <td><a class="btn sm" href="#/runs/${u.id}">详情</a></td>
      </tr>`).join("")}
    </table>` : `<div class="empty"><div class="big">◇</div>暂无运行</div>`}
  </div>`;
  if (localStorage.getItem("fin_create_run") === "1") {
    localStorage.removeItem("fin_create_run");
    openCreateRun();
  }
};

function openCreateRun() {
  const preset = ["消费金融 产品经理", "信贷 产品经理", "信贷 研发", "贷款系统 Java", "资金清结算 产品", "贷后 平台"];
  openModal(`
  <h3>新建研究运行</h3>
  <div class="field"><label class="f">运行名称</label>
    <input type="text" id="cr-name" value="消费信贷方向研究 ${new Date().toISOString().slice(5, 10)}"></div>
  <div class="field"><label class="f">数据来源</label>
    <select id="cr-source">
      <option value="zhipin">BOSS 直聘（真实，需已安装并登录 boss-agent-cli）</option>
      <option value="demo">演示数据源（演示 · 合成数据，离线完整链路）</option>
    </select>
    <div class="f-help">默认为真实来源。选择演示数据后，该运行全程标记为演示，不进入真实统计；真实来源失败不会回退演示。</div></div>
  <div class="field"><label class="f">查询词（不超过 6 个，不做笛卡尔积）</label>
    <div class="tag-input" id="cr-queries"></div>
    <div class="f-help">点击预设词快速添加：${preset.map(p => `<a href="javascript:void(0)" class="muted" onclick="addQueryTag('${esc(p)}')">＋${esc(p)}</a>`).join(" &nbsp; ")}</div></div>
  <div class="grid c2">
    <div class="field"><label class="f">搜索调用上限</label><input type="number" id="cr-search" value="6" min="1" max="20"></div>
    <div class="field"><label class="f">详情调用上限</label><input type="number" id="cr-detail" value="20" min="1" max="60"></div>
  </div>
  <div class="btn-row" style="justify-content:flex-end">
    <button class="btn" onclick="closeModal()">取消</button>
    <button class="btn primary" onclick="submitCreateRun()">创建运行</button>
  </div>`);
  initTagInput($("#cr-queries"), ["消费金融 产品经理", "信贷 研发"]);
}

function initTagInput(container, initial) {
  container.innerHTML = "";
  const input = document.createElement("input");
  input.placeholder = "输入后回车添加";
  container.appendChild(input);
  window.__tagState = window.__tagState || {};
  const key = container.id;
  window.__tagState[key] = (initial || []).slice();
  function render() {
    $$(".tag", container).forEach(t => t.remove());
    (window.__tagState[key] || []).forEach((val, idx) => {
      const t = document.createElement("span");
      t.className = "tag";
      t.innerHTML = `${esc(val)}<i onclick="removeQueryTag('${key}',${idx})">✕</i>`;
      container.insertBefore(t, input);
    });
  }
  input.onkeydown = (e) => {
    if (e.key === "Enter" && input.value.trim()) {
      e.preventDefault();
      const v = input.value.trim();
      if (!window.__tagState[key].includes(v)) window.__tagState[key].push(v);
      input.value = "";
      render();
    }
  };
  render();
  input.focus();
}

function addQueryTag(text) {
  const key = "cr-queries";
  if ($("#" + key) && !window.__tagState[key].includes(text)) {
    window.__tagState[key].push(text);
    const c = $("#" + key);
    $$(".tag", c).forEach(t => t.remove());
    window.__tagState[key].forEach((val, idx) => {
      const t = document.createElement("span");
      t.className = "tag";
      t.innerHTML = `${esc(val)}<i onclick="removeQueryTag('${key}',${idx})">✕</i>`;
      c.insertBefore(t, c.lastChild);
    });
  }
}
function removeQueryTag(key, idx) {
  window.__tagState[key].splice(idx, 1);
  const c = $("#" + key);
  if (!c) return;
  $$(".tag", c).forEach(t => t.remove());
  window.__tagState[key].forEach((val, i) => {
    const t = document.createElement("span");
    t.className = "tag";
    t.innerHTML = `${esc(val)}<i onclick="removeQueryTag('${key}',${i})">✕</i>`;
    c.insertBefore(t, c.lastChild);
  });
}

async function submitCreateRun() {
  const source = $("#cr-source").value;
  if (source === "demo" && !confirm("本运行使用演示 · 合成数据，结果不可用于真实公司判断；确认继续？")) return;
  const body = {
    name: $("#cr-name").value.trim(),
    source: $("#cr-source").value,
    queries: window.__tagState["cr-queries"] || [],
    max_search_calls: +$("#cr-search").value,
    max_detail_calls: +$("#cr-detail").value,
  };
  const r = await api("/api/runs", { method: "POST", body });
  if (!handleErr(r)) return;
  closeModal();
  toast("运行已创建，查询计划已固定");
  location.hash = "#/runs/" + r.data.run.id;
}

/* ---------- 运行详情 ---------- */

let RUNVIEW = null;

async function renderRunDetail(main, runId) {
  const r = await api("/api/runs/" + runId);
  if (!handleErr(r)) return;
  RUNVIEW = r.data;
  const { run, pending_analysis, failures, observations } = RUNVIEW;
  const state = run.state;
  const canCollect = ["planned", "collecting"].includes(state);
  const canFinish = ["planned", "collecting", "budget_stopped"].includes(state);
  const canAnalyze = ["ready_for_analysis", "partial", "analyzing", "completed_partial"].includes(state);

  const stepDefs = [["planned", "已计划"], ["collecting", "采集中"], ["ready_for_analysis", "待分析"],
    ["analyzing", "分析中"], ["completed", "已完成"]];
  const stateIdx = { planned: 0, collecting: 1, budget_stopped: 1, auth_required: 1, risk_stopped: 1,
    rate_limited: 1, stopped: 1, ready_for_analysis: 2, partial: 2, analyzing: 3, completed: 4,
    completed_partial: 4, failed: 1 };
  const idx = stateIdx[state] ?? 0;
  const stepsHtml = stepDefs.map((s, i) => `
    <div class="step ${i < idx ? "done" : ""} ${i === idx ? "cur" : ""}">
      <div class="dot">${i < idx ? "✓" : i + 1}</div><div class="stxt">${s[1]}</div>
    </div>${i < 4 ? `<div class="step-join ${i < idx ? "done" : ""}"></div>` : ""}`).join("");

  const counters = run.counters, limits = run.limits;
  /* 定位符刷新额度：独立计数，不占采集预算。真机复验时靠这一行证明「继续」真的刷新了。 */
  const refresh = RUNVIEW.locator_refresh || { used: 0, limit: 0 };
  const refreshLine = `本次为刷新定位额外用了 ${refresh.used || 0} 次`
    + `（上限 ${refresh.limit || 0} 次，不占搜索预算 max_search_calls）`;
  const budgetBar = (label, used, max) => `
    <div style="flex:1;min-width:220px">
      <div class="bar-label"><span>${label}</span><span>${used} / ${max}</span></div>
      <div class="bar ${used >= max ? "warn" : ""}"><i style="width:${Math.min(100, used / max * 100)}%"></i></div>
    </div>`;

  main.innerHTML = `
  <div class="page-head">
    <div><h1>${esc(run.name)}</h1>
      <div class="sub">${fmtShort(run.id)} · ${badge(RUN_BADGE, state, LABELS.run_state)}
      ${run.stop_reason ? ` · 停止原因：<b>${esc(run.stop_reason)}</b>` : ""}</div></div>
    <div class="btn-row">
      ${canCollect ? `<button class="btn" onclick="runControl('stop')">停止运行</button>` : ""}
      ${["auth_required", "risk_stopped", "rate_limited", "stopped"].includes(state) ? `<button class="btn" onclick="runControl('resume_collection')">恢复采集</button>` : ""}
      ${canFinish ? `<button class="btn primary" onclick="runControl('finish_collection')">结束采集，进入分析</button>` : ""}
      ${canAnalyze ? `<a class="btn primary" href="#/analysis/${run.id}">进入分析工作台 →</a>` : ""}
    </div>
  </div>

  <div class="card card-pad">
    <h3>运行状态机 <span class="hint">auth/risk/rate 停止后禁止新平台调用；预算耗尽不自动续采</span></h3>
    <div class="steps">${stepsHtml}</div>
    <div class="flex mt14">
      ${budgetBar("搜索调用（含失败重试）", counters.search_calls || 0, limits.max_search_calls)}
      ${budgetBar("详情调用（含失败重试）", counters.detail_calls || 0, limits.max_detail_calls)}
    </div>
    <div class="flex small muted mt8">
      <span>卡片 ${counters.cards_seen || 0}</span><span>唯一岗位 ${counters.jobs_unique || 0}</span>
      <span>完整快照 ${counters.snapshots_complete || 0}</span><span>缺失 ${counters.snapshots_missing || 0}</span>
      <span>失败 ${counters.snapshots_failed || 0}</span><span>复用 ${counters.snapshots_reused || 0}</span>
    </div>
    <div class="small muted mt8">${esc(refreshLine)}</div>
  </div>

  <div class="card card-pad">
    <h3>查询计划 <span class="hint">同 run/query/page 重复请求优先返回已有成功结果（幂等）</span></h3>
    <table class="tbl">
      <tr><th>query_id</th><th>查询词</th><th>已采集页数</th><th>最近结果</th><th>${canCollect ? "操作" : ""}</th></tr>
      ${(run.config_snapshot.queries || []).map(q => {
        const obs = observations.filter(o => o.query_id === q.query_id);
        const done = obs.filter(o => o.outcome === "success").length;
        return `<tr>
          <td class="mono">${esc(q.query_id)}</td><td><b>${esc(q.text)}</b></td>
          <td>${done} 页</td>
          <td>${obs.length ? obs.map(o => `<span class="badge ${o.outcome === "success" ? "b-green" : "b-red"}">${esc(o.outcome)}</span>`).join(" ") : '<span class="muted">未采集</span>'}</td>
          <td>${canCollect ? `<button class="btn sm primary" onclick="doSearch('${run.id}','${q.query_id}',${done + 1})">执行第 ${done + 1} 页搜索</button>` : ""}
        </tr>`;
      }).join("")}
    </table>
  </div>

  <div id="run-jobs-area"></div>
  <div id="run-calls-area"></div>

  ${failures.length ? `
  <div class="card card-pad">
    <h3>缺失 / 失败清单 <span class="hint">显示完成数、失败数、未处理数，不隐藏失败项</span></h3>
    <table class="tbl">
      <tr><th>岗位</th><th>公司</th><th>状态</th><th>错误</th></tr>
      ${failures.map(f => `<tr><td>${esc(f.title)}</td><td>${esc(f.company_name_raw)}</td>
        <td>${badge(DET_BADGE, f.detail_outcome, LABELS.detail_status)}</td>
        <td class="small muted">${esc(f.last_error || "")}</td></tr>`).join("")}
    </table>
  </div>` : ""}`;

  await renderRunJobs(run.id, canCollect);
  await renderRunCalls(run.id);
}

async function renderRunCalls(runId) {
  const r = await api("/api/runs/" + runId + "/calls");
  const el = $("#run-calls-area");
  if (!el || !r.ok) return;
  const calls = r.data.calls;
  el.innerHTML = `
  <div class="card card-pad">
    <h3>上游调用留痕（${calls.length}） <span class="hint">命令已脱敏；凭据与 next_actions 不落库</span></h3>
    ${calls.length ? `<table class="tbl">
      <tr><th>命令</th><th>结果</th><th>耗时/层</th><th>脱敏命令</th></tr>
      ${calls.map(c => `<tr>
        <td class="mono">${esc(c.command)}</td>
        <td>${c.ok ? '<span class="badge b-green">成功</span>' : '<span class="badge b-red">' + esc(c.error_code || "失败") + "</span>"}
          <div class="small muted">${esc(c.result_summary || "")}</div></td>
        <td class="small muted">${c.elapsed_ms === null || c.elapsed_ms === undefined ? "—" : c.elapsed_ms + " ms"}<br>${esc(c.layer || "")}</td>
        <td class="small mono">${esc((c.argv_redacted || []).join(" ") || "—")}</td></tr>`).join("")}
    </table>` : `<div class="empty"><div class="big">◇</div>尚无上游调用记录</div>`}
  </div>`;
}

async function renderRunJobs(runId, canCollect) {
  const r = await api("/api/jobs?run_id=" + runId + "&include_demo=true");
  if (!r.ok) return;
  const jobs = r.data.jobs;
  const el = $("#run-jobs-area");
  if (!el) return;
  window.__runJobs = jobs;
  el.innerHTML = `
  <div class="card card-pad">
    <div class="spread mb8">
      <h3 class="" style="margin:0">岗位卡片（${jobs.length}） <span class="hint">勾选后读取详情；重复岗位优先复用已有有效快照</span></h3>
      <div class="btn-row">
        <button class="btn" onclick="checkAllJobs(true)">全选</button>
        <button class="btn" onclick="checkAllJobs(false)">清空</button>
        <button class="btn primary" onclick="fetchSelectedDetails('${runId}')">读取选中详情（≤${RUNVIEW.run.limits.details_per_call}个/次）</button>
      </div>
    </div>
    ${jobs.length ? `<table class="tbl">
      <tr>${canCollect ? "<th style='width:34px'></th>" : ""}<th>岗位</th><th>公司</th><th>城市</th><th>来源标识</th><th>详情状态</th></tr>
      ${jobs.map((j, i) => `<tr>
        ${canCollect ? `<td><input type="checkbox" class="job-ck" data-i="${i}" ${j.detail_status ? "disabled" : ""}></td>` : ""}
        <td><b>${esc(j.title)}</b><br>${fmtShort(j.id)}</td>
        <td>${esc(j.company_name_raw)}${j.identity_status !== "verified" ? ` <span class="badge b-orange">归属待核实</span>` : ""}</td>
        <td class="muted">${esc(j.city || "—")}</td>
        <td class="small">${j.source === "demo" ? '<span class="badge b-teal">演示</span>' : '<span class="badge b-blue">BOSS 真实</span>'}
          <div class="mono muted">${j.source_job_id ? esc(String(j.source_job_id).slice(0, 20)) : "无稳定 job_id"}</div></td>
        <td>${j.detail_status ? badge(DET_BADGE, j.detail_status, LABELS.detail_status) : '<span class="badge b-gray">未读取</span>'}</td>
      </tr>`).join("")}
    </table>` : `<div class="empty"><div class="big">◇</div>还没有岗位，先在查询计划中执行搜索</div>`}
  </div>`;
}

function checkAllJobs(v) { $$(".job-ck").forEach(c => c.checked = v); }

async function doSearch(runId, queryId, page) {
  toast("正在执行第 " + page + " 页搜索…");
  const r = await api(`/api/runs/${runId}/search`, { method: "POST", body: { query_id: queryId, page } });
  if (!handleErr(r)) return;
  const d = r.data;
  if (d.result_code === "EMPTY_RESULT") {
    toast("真实搜索成功，但该条件下暂无岗位（EMPTY_RESULT）", "ok");
  } else {
    toast(`搜索完成：返回 ${d.cards.length} 张卡片${d.reused ? "（复用已有成功结果）" : ""}`, "ok");
  }
  routeTo();
}

async function fetchSelectedDetails(runId) {
  const checked = $$(".job-ck").filter(c => c.checked);
  if (!checked.length) return toast("请先勾选要读取详情的岗位", "err");
  const refs = checked.map(c => window.__runJobs[+c.dataset.i].job_ref).filter(Boolean);
  if (!refs.length) return toast("所选岗位缺少 job_ref", "err");
  const limit = RUNVIEW.run.limits.details_per_call;
  const batch = refs.slice(0, limit);
  toast(`正在读取 ${batch.length} 个岗位详情…`);
  const r = await api(`/api/runs/${runId}/details`, { method: "POST", body: { job_refs: batch } });
  if (!handleErr(r)) return;
  const d = r.data;
  const counts = {};
  d.outcomes.forEach(o => counts[o.outcome] = (counts[o.outcome] || 0) + 1);
  toast("详情读取完成：" + Object.entries(counts).map(([k, v]) => `${k} ${v}`).join("，"), "ok");
  routeTo();
}

async function runControl(action) {
  const runId = RUNVIEW.run.id;
  const r = await api(`/api/runs/${runId}/control/${action}`, { method: "POST", body: { reason: "用户手动停止" } });
  if (!handleErr(r)) return;
  const map = { finish_collection: "采集阶段已结束", stop: "运行已停止", resume_collection: "已恢复采集（不重置已消耗预算）" };
  toast(map[action] || "操作完成", "ok");
  routeTo();
}

/* ============================ 页面：分析工作台 ============================ */

RENDER.analysis = async function (main) {
  const runId = route.arg;
  let runsList = (await api("/api/runs")).data.runs;
  if (!runsList.length) {
    main.innerHTML = `<div class="page-head"><div><h1>分析工作台</h1></div></div>
      <div class="card card-pad empty"><div class="big">◇</div>还没有运行。请先<a href="#/runs">创建运行并采集岗位</a>。</div>`;
    return;
  }
  const rid = runId || runsList[0].id;
  const ps = await api(`/api/runs/${rid}/pending-snapshots`);
  if (!handleErr(ps)) return;
  const { pending, all } = ps.data;
  const analyzed = all.length;
  window.__analysisRun = rid;

  main.innerHTML = `
  <div class="page-head">
    <div><h1>分析工作台</h1><div class="sub">流程：生成分析包（固定提示词 + 证据文本）→ 模型/规则分析 → 提交校验
      （Schema + 原文引用偏移）→ 入库。引用正确只证明文字存在，不证明解释正确。</div></div>
    <div class="flex">
      <select id="an-run" style="width:260px" onchange="location.hash='#/analysis/'+this.value">
        ${runsList.map(u => `<option value="${u.id}" ${u.id === rid ? "selected" : ""}>${esc(u.name)}（${esc(u.state)}）</option>`).join("")}
      </select>
    </div>
  </div>

  <div class="grid c2">
    <div class="card card-pad">
      <h3>待分析快照（${pending.length}）</h3>
      <div style="max-height:340px;overflow-y:auto">
        ${pending.length ? pending.map(s => `
          <div class="checkline"><input type="checkbox" class="an-ck" value="${s.id}">
            <span><b>${esc(s.title)}</b><span class="muted"> · ${esc(s.company_name_raw)}</span>
            <br><span class="small muted">${fmtShort(s.id)} · ${esc(s.city || "")} · ${fmtTime(s.fetched_at)}</span></span>
          </div>`).join("") : `<div class="empty"><div class="big">✓</div>该运行没有待分析快照</div>`}
      </div>
      <div class="btn-row mt8">
        <button class="btn" onclick="toggleAnCk(true)">全选</button>
        <button class="btn primary" onclick="prepareBatch('${rid}')">生成分析包（≤5 个/批）</button>
      </div>
    </div>
    <div class="card card-pad">
      <h3>已分析（${analyzed}）</h3>
      <div style="max-height:390px;overflow-y:auto">
        ${analyzed ? `<table class="tbl">${all.map(a => `<tr>
          <td><b>${esc(a.title)}</b><div class="small muted">${esc(a.company_name_raw)}</div></td>
          <td>${badge(RES_BADGE, a.result_status, LABELS.result_status)}</td>
          <td class="small muted">${esc(a.model_label || "")}</td>
          <td><button class="btn sm" onclick="location.hash='#/jobs'">岗位库查看</button></td>
        </tr>`).join("")}</table>` : `<div class="empty">尚无分析结果</div>`}
      </div>
    </div>
  </div>

  ${typeof aiStatusBanner === "function" ? aiStatusBanner() : ""}
  <div class="card card-pad">
    <h3>分析包操作区 <span class="hint">已配置模型时由真实 AI 抽取工作事项（引用偏移仍严格校验）；未配置返回「AI 服务未配置」，不会回退为规则结果；亦可粘贴符合 Schema 的模型输出 JSON 手动提交</span></h3>
    <div id="batch-area"><div class="empty"><div class="big">⌘</div>先在左侧选择快照并生成分析包</div></div>
  </div>`;
};

function toggleAnCk(v) { $$(".an-ck").forEach(c => c.checked = v); }

async function prepareBatch(runId) {
  const ids = $$(".an-ck").filter(c => c.checked).map(c => c.value);
  if (!ids.length) return toast("请先选择快照", "err");
  if (ids.length > 5) return toast("每次最多选择 5 个快照（analysis_batch_size）", "err");
  const r = await api(`/api/runs/${runId}/prepare-analysis`, { method: "POST", body: { snapshot_ids: ids } });
  if (!handleErr(r)) return;
  window.__batch = r.data;
  renderBatchArea();
}

function renderBatchArea() {
  const b = window.__batch;
  const el = $("#batch-area");
  el.innerHTML = `
    <div class="ok-box">分析包已生成：<span class="mono">${esc(b.batch_token)}</span><br>
      提示词版本 ${esc(b.prompt_version)} · 字典 ${esc(b.taxonomy_version)}</div>
    ${b.snapshots.map((s, i) => `
      <details class="mt8" ${i === 0 ? "open" : ""}>
        <summary style="cursor:pointer;font-weight:600">${esc(s.title)} · ${esc(s.company_name)}
          <span class="muted small">（${esc(s.detail_status)} · 证据文本 ${s.evidence_text.length} 字符）</span></summary>
        <div class="evidence-box mt8" id="ev-${esc(s.snapshot_id)}">${renderEvidence(s.evidence_text)}</div>
      </details>`).join("")}
    <div class="btn-row mt14">
      <button class="btn primary" onclick="autoAnalyze()">⚙ 用 AI 分析并提交</button>
      <button class="btn" onclick="showManualSubmit()">✎ 手动提交模型输出 JSON</button>
    </div>
    <div id="manual-area" class="mt8" style="display:none">
      <label class="f">宿主模型输出 JSON（须符合 SubmitPayload：batch_token / submission_id / model_label / analyses）</label>
      <textarea id="manual-json" rows="12" placeholder='粘贴符合 Schema 的 JSON'></textarea>
      <div class="btn-row mt8"><button class="btn primary" onclick="manualSubmit()">提交校验</button></div>
    </div>
    <div id="submit-result" class="mt14"></div>`;
}

function renderEvidence(text) {
  const html = esc(text)
    .replace(/【工作职责】/g, '<span class="sec-head">【工作职责】</span>')
    .replace(/【任职要求】/g, '<span class="sec-head">【任职要求】</span>');
  return `<span class="ev-src">${html}</span>`;
}

async function autoAnalyze() {
  const b = window.__batch;
  const ids = b.snapshots.map(s => s.snapshot_id);
  const r = await api(`/api/runs/${window.__analysisRun}/auto-analyze`, { method: "POST", body: { snapshot_ids: ids } });
  if (!handleErr(r)) return;
  renderSubmitResult(r.data, true);
}

function showManualSubmit() { $("#manual-area").style.display = "block"; }

async function manualSubmit() {
  let payload;
  try { payload = JSON.parse($("#manual-json").value); }
  catch (e) { return toast("JSON 解析失败：" + e.message, "err"); }
  const r = await api(`/api/runs/${window.__analysisRun}/submit-analysis`, { method: "POST", body: payload });
  if (!handleErr(r)) return;
  renderSubmitResult(r.data, false);
}

function renderSubmitResult(data, isAuto) {
  const el = $("#submit-result");
  const outs = data.outcomes || (data.result && data.result.outcomes) || [];
  const errHtml = (errs) => errs.map(e => `<div class="err-box"><b>${esc(e.field)}</b> [${esc(e.code)}] ${esc(e.message)}</div>`).join("");
  el.innerHTML = `
    <h3>提交校验结果 ${isAuto ? (window.__aiStatus && window.__aiStatus.configured
      ? '<span class="badge b-green">真实 AI</span>'
      : '<span class="badge b-orange">规则分析器（演示）</span>') : ""}</h3>
    ${outs.map(o => o.ok
      ? `<div class="ok-box">✓ ${esc(o.snapshot_id.slice(0, 14))}… 校验通过，入库 ${o.finding_ids.length} 项事项（${esc(o.result_status)}）</div>`
      : `<div class="err-box">✗ ${esc(o.snapshot_id.slice(0, 14))}… 校验失败，未入库（字段级错误）：<br>${errHtml(o.errors)}</div>`).join("")}
    <div class="small muted mt8">宿主最多修正提交 1 次；仍失败标 analysis_failed。校验失败的结果不进入正式事项表。</div>`;
  refreshPendingCount();
}

/* ============================ 页面：岗位库 ============================ */

RENDER.jobs = async function (main) {
  main.innerHTML = `
  <div class="page-head">
    <div><h1>岗位库</h1><div class="sub">点击行查看证据文本快照与分析结果。正文为空的岗位保留为异常素材，
      不用列表摘要假装完整职责。</div></div>
  </div>
  <div class="card card-pad">
    <div class="flex mb8">
      <select id="jb-filter-role" style="width:160px" onchange="renderJobsTable()">
        <option value="">全部角色</option>
        ${Object.entries(LABELS.role_class).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}
      </select>
      <select id="jb-filter-rel" style="width:160px" onchange="renderJobsTable()">
        <option value="">全部相关性</option>
        ${Object.entries(LABELS.relevance).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}
      </select>
      <input type="text" id="jb-search" placeholder="搜索公司 / 岗位名…" style="width:240px" oninput="renderJobsTable()">
      <label class="checkline"><input type="checkbox" id="jb-include-demo" onchange="loadJobs()">
        <span>含演示数据（默认关闭，真实统计不混入演示）</span></label>
    </div>
    <div id="jobs-table"></div>
  </div>`;
  loadJobs();
};

async function loadJobs() {
  const include = $("#jb-include-demo") && $("#jb-include-demo").checked ? "true" : "false";
  const r = await api("/api/jobs?include_demo=" + include);
  if (!handleErr(r)) return;
  window.__jobs = r.data.jobs;
  renderJobsTable();
}

function renderJobsTable() {
  const role = $("#jb-filter-role").value, rel = $("#jb-filter-rel").value, q = $("#jb-search").value.trim().toLowerCase();
  const jobs = window.__jobs.filter(j =>
    (!role || j.role_class === role) && (!rel || j.relevance === rel) &&
    (!q || (j.title + j.company_name_raw).toLowerCase().includes(q)));
  $("#jobs-table").innerHTML = jobs.length ? `
    <table class="tbl">
      <tr><th>岗位</th><th>公司</th><th>城市</th><th>来源</th><th>角色</th><th>相关性</th><th>详情</th><th>分析结果</th><th>快照</th></tr>
      ${jobs.map(j => `<tr class="rowlink" onclick="openJobDrawer('${j.id}')">
        <td><b>${esc(j.title)}</b></td>
        <td>${esc(j.company_name_raw)}${j.identity_status !== "verified" ? ' <span class="badge b-orange">归属待核实</span>' : ""}</td>
        <td class="muted">${esc(j.city || "—")}</td>
        <td>${j.source === "demo" ? '<span class="badge b-teal">演示</span>' : '<span class="badge b-blue">BOSS 真实</span>'}${j.source_job_id ? `<div class="small mono muted">${esc(String(j.source_job_id).slice(0, 16))}</div>` : ""}</td>
        <td>${badge(ROLE_BADGE, j.role_class, LABELS.role_class)}</td>
        <td>${badge(REL_BADGE, j.relevance, LABELS.relevance)}</td>
        <td>${badge(DET_BADGE, j.detail_status, LABELS.detail_status)}</td>
        <td>${badge(RES_BADGE, j.result_status, LABELS.result_status)}</td>
        <td>${j.snapshot_count || 0}</td>
      </tr>`).join("")}
    </table>` : `<div class="empty"><div class="big">◇</div>没有匹配的岗位</div>`;
}

async function openJobDrawer(jobId) {
  const r = await api("/api/jobs/" + jobId);
  if (!handleErr(r)) return;
  const { job, company, snapshots, analyses } = r.data;
  const snap = snapshots[0];
  openDrawer(`
    <span class="close" onclick="closeDrawer()">✕</span>
    <h2>${esc(job.title)}</h2>
    <div class="muted small mb8">${esc(job.company_name_raw)} · ${esc(job.city || "—")} · ${company ? (company.identity_status === "verified" ? "归属已核实" : "归属待核实") : "归属待核实"}</div>
    <div class="flex mb8">
      ${badge(ROLE_BADGE, analyses[0] && analyses[0].role_class, LABELS.role_class)}
      ${badge(REL_BADGE, analyses[0] && analyses[0].relevance, LABELS.relevance)}
      ${badge(RES_BADGE, analyses[0] && analyses[0].result_status, LABELS.result_status)}
    </div>
    ${snap ? `
    <div class="kv card card-pad mb8" style="box-shadow:none">
      <dt>快照编号</dt><dd class="mono">${esc(snap.id)}</dd>
      <dt>内容哈希</dt><dd class="mono">${esc(snap.content_hash)}</dd>
      <dt>采集时间</dt><dd>${fmtTime(snap.fetched_at)}</dd>
      <dt>来源平台</dt><dd>${esc(snap.source_platform || job.source)}${snap.source_job_id ? ` · <span class="mono">${esc(snap.source_job_id)}</span>` : ""}</dd>
      <dt>发布时间</dt><dd>${esc(snap.published_at_raw || "未知（null）")}</dd>
      <dt>详情状态</dt><dd>${badge(DET_BADGE, snap.detail_status, LABELS.detail_status)}</dd>
      <dt>来源链接</dt><dd>${snap.source_url ? `<a href="${esc(snap.source_url)}" target="_blank" rel="noopener">${esc(snap.source_url)}</a>` : "无（报告提供快照编号）"}
        ${snap.source_url_verified ? "" : '<div class="small muted">链接按模板生成，未逐条校验</div>'}</dd>
    </div>
    <h3 style="font-size:14px;margin:12px 0 8px">证据文本 <span class="hint">程序确定性生成（统一换行、移除联系方式），不可变</span></h3>
    <div class="evidence-box" id="drawer-ev">${renderEvidence(snap.evidence_text)}</div>
    <div class="small muted mt8">点击下方事项的"原文引用"可在上方证据文本中高亮定位。</div>
    ${analyses.map(a => `
      <div class="card card-pad mt8" style="box-shadow:none">
        <div class="spread"><h3 style="margin:0;font-size:13.5px">分析结果 · ${esc(a.model_label)}</h3>
          <div class="flex">${badge(RES_BADGE, a.result_status, LABELS.result_status)}${badge(REV_BADGE, a.review_status, LABELS.review_status)}</div></div>
        <div class="small muted mt8">分类依据：${esc(a.reason || "")}</div>
        ${a.findings.map(f => renderFindingCard(f, snap.evidence_text)).join("") || '<div class="empty" style="padding:14px">零事项（信息不足或排除）</div>'}
      </div>`).join("")}
    ` : `<div class="empty"><div class="big">◇</div>该岗位没有可分析的正文快照</div>`}
  `);
}

function renderFindingCard(f, evidenceText) {
  return `
  <div class="finding">
    <div class="st">${esc(f.statement)}</div>
    <div class="meta">
      ${domainBadge(f.business_domain)}
      <span class="badge b-gray">${lbl("act", f.action_type)}</span>
      ${badge(SUP_BADGE, f.support_level, LABELS.support_level)}
      ${badge(REV_BADGE, f.review_status, LABELS.review_status)}
      ${f.market ? `<span class="badge b-teal">市场：${esc(f.market)}</span>` : ""}
    </div>
    ${(f.evidences || []).map(ev => `
      <div class="quote" onclick="highlightEvidence('drawer-ev', ${ev.start}, ${ev.end})"
        title="点击在证据文本中高亮定位 [${ev.start}, ${ev.end}）">
        “${esc(ev.quote.length > 160 ? ev.quote.slice(0, 160) + "…" : ev.quote)}”
        <span class="mono muted">[${ev.start},${ev.end})</span>
      </div>`).join("")}
    ${f.interpretation ? `<div class="small"><b>分析判断：</b>${esc(f.interpretation)}</div>` : ""}
    ${(f.limitations || []).slice(0, 3).map(l => `<div class="limit">不能确认：${esc(l)}</div>`).join("")}
    ${f.review_status === "unreviewed" ? `<div class="btn-row mt8">
      <button class="btn sm" onclick="quickReview('${f.id}','confirm')">✓ 确认</button>
      <button class="btn sm" onclick="openCorrectModal('${f.id}')">✎ 修正</button>
      <button class="btn sm danger" onclick="quickReview('${f.id}','exclude')">✕ 排除</button>
    </div>` : ""}
  </div>`;
}

function highlightEvidence(boxId, start, end) {
  const box = $("#" + boxId);
  if (!box) return;
  const src = box.querySelector(".ev-src");
  if (!src || !src.dataset.text) return;
  const text = src.dataset.text;
  const before = esc(text.slice(0, start));
  const hit = esc(text.slice(start, end));
  const after = esc(text.slice(end));
  src.innerHTML = before + "<mark class='active'>" + hit + "</mark>" + after;
  box.scrollTop = Math.max(0, box.scrollHeight * (start / text.length) - 80);
}

/* ============================ 页面：公司方向 ============================ */

RENDER.companies = async function (main) {
  const include = window.__includeDemo ? "true" : "false";
  const r = await api("/api/companies?include_demo=" + include);
  if (!handleErr(r)) return;
  const comps = r.data.companies;
  main.innerHTML = `
  <div class="page-head">
    <div><h1>公司方向</h1><div class="sub">按确定公司、主分类组织事项。支持程度由程序判定：
      多岗一致 = 至少两组非重复的不同岗位证据；单岗线索 = 一组有效职责证据；重复 JD 不加权重。
      当前口径：${include === "true" ? "含演示数据（整体标注）" : "仅真实来源（演示数据已排除）"}。</div></div>
    <label class="checkline"><input type="checkbox" id="cp-include-demo" ${include === "true" ? "checked" : ""}
      onchange="window.__includeDemo=this.checked;routeTo()"><span>含演示数据</span></label>
  </div>
  <div class="grid c2">
    ${comps.length ? comps.map(c => `
    <div class="card card-pad">
      <div class="spread">
        <div><b style="font-size:15px">${esc(c.display_name)}</b>
          ${c.identity_status === "verified" ? '<span class="badge b-green">已核实</span>' : '<span class="badge b-orange">归属待核实</span>'}</div>
        <div class="flex">
          <span class="badge b-blue">${c.finding_count} 事项</span>
          <span class="badge b-teal">${c.evidence_groups} 证据组</span>
        </div>
      </div>
      <div class="small muted mt8">岗位 ${c.job_count} 个 · 未复核事项 ${c.unverified} 个</div>
      <button class="btn sm primary mt8" onclick="openCompanyDrawer('${c.id}')">查看工作事项 →</button>
    </div>`).join("") : `<div class="card card-pad empty"><div class="big">▦</div>还没有公司数据</div>`}
  </div>`;
};

async function openCompanyDrawer(companyId) {
  const r = await api("/api/companies/" + companyId);
  if (!handleErr(r)) return;
  const { company, aliases, findings } = r.data;
  const byDomain = {};
  findings.forEach(f => { (byDomain[f.business_domain] = byDomain[f.business_domain] || []).push(f); });
  openDrawer(`
    <span class="close" onclick="closeDrawer()">✕</span>
    <h2>${esc(company.display_name)}
      ${company.identity_status === "verified" ? '<span class="badge b-green">已核实</span>' : '<span class="badge b-orange">归属待核实</span>'}</h2>
    <div class="small muted mb8">来源别名：${aliases.map(a => esc(a.alias)).join("、")}</div>
    ${findings.length ? Object.entries(byDomain).map(([domain, fs]) => `
      <h3 style="font-size:14px;margin:16px 0 8px">${domainBadge(domain)}<span style="margin-left:8px">${fs.length} 项</span></h3>
      ${fs.map(f => renderFindingCard(f, null)).join("")}
    `).join("") : `<div class="empty mt14"><div class="big">◇</div>该公司暂无有效事项</div>`}
  `);
}

/* ============================ 页面：人工复核 ============================ */

RENDER.review = async function (main) {
  const r = await api("/api/review/pending");
  if (!handleErr(r)) return;
  const { findings, analyses, pairs, aliases } = r.data;
  main.innerHTML = `
  <div class="page-head">
    <div><h1>人工复核</h1><div class="sub">修正留痕（前后值 + 原因 + 版本号）；版本冲突拒绝，前一修正不被覆盖。
      模型重新分析不覆盖人工修正。排除属软排除，报告默认不展示但可追溯。</div></div>
  </div>
  <div class="card card-pad">
    <div class="tabs" id="rv-tabs">
      <div class="tab active" data-t="findings">待复核事项（${findings.length}）</div>
      <div class="tab" data-t="analyses">待判断岗位（${analyses.length}）</div>
      <div class="tab" data-t="pairs">疑似重复（${pairs.length}）</div>
      <div class="tab" data-t="aliases">公司归属（${aliases.length}）</div>
    </div>
    <div id="rv-body"></div>
  </div>
  <div class="card card-pad">
    <h3>修改历史（review_event 追加写）</h3>
    <div id="rv-history"></div>
  </div>`;
  $$("#rv-tabs .tab").forEach(t => t.onclick = () => {
    $$("#rv-tabs .tab").forEach(x => x.classList.remove("active"));
    t.classList.add("active");
    renderReviewTab(t.dataset.t);
  });
  window.__reviewData = r.data;
  renderReviewTab("findings");
  renderReviewHistory();
};

function renderReviewTab(t) {
  const d = window.__reviewData;
  const el = $("#rv-body");
  if (t === "findings") {
    el.innerHTML = d.findings.length ? d.findings.map(f => `
      <div class="finding">
        <div class="st">${esc(f.statement)}</div>
        <div class="meta">${domainBadge(f.business_domain)}
          <span class="badge b-gray">${lbl("act", f.action_type)}</span>
          ${badge(SUP_BADGE, f.support_level, LABELS.support_level)}
          <span class="badge b-gray">${esc(f.company_name_raw)} · ${esc(f.title)}</span></div>
        <div class="btn-row">
          <button class="btn sm" onclick="quickReview('${f.id}','confirm')">✓ 确认</button>
          <button class="btn sm" onclick="openCorrectModal('${f.id}')">✎ 修正措辞/分类</button>
          <button class="btn sm danger" onclick="quickReview('${f.id}','exclude')">✕ 排除</button>
        </div>
      </div>`).join("") : `<div class="empty">没有待复核事项 ✓</div>`;
  } else if (t === "analyses") {
    el.innerHTML = d.analyses.length ? `<table class="tbl">
      <tr><th>岗位</th><th>公司</th><th>角色</th><th>相关性</th><th>结果</th><th>操作</th></tr>
      ${d.analyses.map(a => `<tr>
        <td><b>${esc(a.title)}</b></td><td>${esc(a.company_name_raw)}</td>
        <td>${badge(ROLE_BADGE, a.role_class, LABELS.role_class)}</td>
        <td>${badge(REL_BADGE, a.relevance, LABELS.relevance)}</td>
        <td>${badge(RES_BADGE, a.result_status, LABELS.result_status)}
          <div class="small muted mt8">${esc((a.reason || "").slice(0, 80))}</div></td>
        <td><div class="btn-row">
          <button class="btn sm" onclick="reviewAnalysis('${a.id}','correct')">修正分类</button>
          <button class="btn sm danger" onclick="reviewAnalysis('${a.id}','exclude')">排除</button>
        </div></td>
      </tr>`).join("")}</table>` : `<div class="empty">没有待判断岗位 ✓</div>`;
  } else if (t === "pairs") {
    el.innerHTML = d.pairs.length ? `<table class="tbl">
      <tr><th>岗位对</th><th>公司</th><th>相似度</th><th>关系</th><th>操作</th></tr>
      ${d.pairs.map(p => `<tr>
        <td class="small"><b>${esc(p.left_title)}</b><br><b>${esc(p.right_title)}</b></td>
        <td>${esc(p.company)}</td>
        <td class="mono">${(p.similarity || 0).toFixed(3)}</td>
        <td>${p.relation === "duplicate" ? '<span class="badge b-teal">内容完全相同</span>' : '<span class="badge b-orange">疑似重复</span>'}</td>
        <td><div class="btn-row">
          <button class="btn sm" onclick="reviewPair('${p.id}','mark_duplicate', ${p.review_version})">确认重复（同一证据组）</button>
          <button class="btn sm" onclick="reviewPair('${p.id}','mark_distinct', ${p.review_version})">不同岗位</button>
        </div></td>
      </tr>`).join("")}</table>` : `<div class="empty">没有疑似重复对 ✓</div>`;
  } else if (t === "aliases") {
    el.innerHTML = d.aliases.length ? `<table class="tbl">
      <tr><th>公司名（来源）</th><th>岗位数</th><th>建档依据</th><th>操作</th></tr>
      ${d.aliases.map(a => `<tr>
        <td><b>${esc(a.alias)}</b><br><span class="small muted">${esc(a.source)}</span></td>
        <td>${a.job_count}</td>
        <td class="small muted">${esc(a.basis)}</td>
        <td><button class="btn sm" onclick="toast('公司映射确认需提供目标实体 ID（P1 通过 API 调用 intel_review 完成）')">确认映射…</button></td>
      </tr>`).join("")}</table>` : `<div class="empty">没有待核实公司映射 ✓</div>`;
  }
}

async function quickReview(findingId, action) {
  const reason = action === "exclude" ? prompt("请填写排除原因（留痕要求）：") : "人工确认事项表述与分类";
  if (reason === null) return;
  if (action === "exclude" && !reason.trim()) return toast("排除必须填写原因", "err");
  const r = await api("/api/review", { method: "POST", body: {
    target_type: "finding", target_id: findingId, action, reason: reason.trim(), expected_version: 0 } });
  if (!handleErr(r)) return;
  toast(action === "confirm" ? "已确认（确认不是业务落地认证）" : "已排除（软排除，可追溯）", "ok");
  routeTo();
}

function openCorrectModal(findingId) {
  openModal(`
  <h3>修正事项</h3>
  <div class="field"><label class="f">事项表述（statement，1~300 字）</label>
    <textarea id="cf-statement" rows="3"></textarea></div>
  <div class="grid c2">
    <div class="field"><label class="f">业务环节</label><select id="cf-domain">
      ${Object.entries(TAXONOMY).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select></div>
    <div class="field"><label class="f">工作动作</label><select id="cf-action">
      ${Object.entries(LABELS.action_type).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select></div>
  </div>
  <div class="field"><label class="f">修改原因（必填，留痕）</label>
    <input type="text" id="cf-reason" placeholder="例如：原文为维护现有系统，不应写为新建"></div>
  <div class="btn-row" style="justify-content:flex-end">
    <button class="btn" onclick="closeModal()">取消</button>
    <button class="btn primary" onclick="submitCorrect('${findingId}')">提交修正</button>
  </div>`);
  api("/api/findings/" + findingId).then(r => {
    if (r.ok) {
      $("#cf-statement").value = r.data.statement;
      $("#cf-domain").value = r.data.business_domain;
      $("#cf-action").value = r.data.action_type;
      window.__cfVersion = r.data.review_version;
    }
  });
}

async function submitCorrect(findingId) {
  const reason = $("#cf-reason").value.trim();
  if (!reason) return toast("请填写修改原因", "err");
  const r = await api("/api/review", { method: "POST", body: {
    target_type: "finding", target_id: findingId, action: "correct",
    patch: { statement: $("#cf-statement").value.trim(), business_domain: $("#cf-domain").value,
             action_type: $("#cf-action").value },
    reason, expected_version: window.__cfVersion || 0 } });
  if (!handleErr(r)) return;
  closeModal();
  toast("已修正并留痕；受影响报告需重新生成", "ok");
  routeTo();
}

async function reviewAnalysis(analysisId, action) {
  const reason = action === "exclude" ? prompt("请填写排除原因：") : "人工修正岗位分类";
  if (reason === null) return;
  if (action === "exclude" && !reason.trim()) return toast("排除必须填写原因", "err");
  const patch = {};
  if (action === "correct") {
    const role = prompt("修正角色分类（product/engineering/algorithm_data/sales/other/unknown）：");
    if (!role) return;
    patch.role_class = role.trim();
  }
  const r = await api("/api/review", { method: "POST", body: {
    target_type: "analysis", target_id: analysisId, action, patch, reason: reason.trim(), expected_version: 0 } });
  if (!handleErr(r)) return;
  toast("已提交", "ok");
  routeTo();
}

async function reviewPair(pairId, action, expectedVersion) {
  const reason = action === "mark_duplicate" ? "人工确认两岗位为重复 JD（同一证据内容组）" : "人工确认两岗位为不同岗位";
  const r = await api("/api/review", { method: "POST", body: {
    target_type: "job_pair", target_id: pairId, action, reason, expected_version: expectedVersion } });
  if (!handleErr(r)) return;
  toast(action === "mark_duplicate" ? "已确认重复：重复 JD 不增加独立证据组数" : "已确认不同岗位，可参与多岗一致判定", "ok");
  routeTo();
}

async function renderReviewHistory() {
  const r = await api("/api/review/events");
  if (!r.ok) return;
  const el = $("#rv-history");
  if (!el) return;
  el.innerHTML = r.data.events.length ? `<table class="tbl">
    <tr><th>时间</th><th>对象</th><th>动作</th><th>原因</th></tr>
    ${r.data.events.slice(0, 20).map(e => `<tr>
      <td class="small muted">${fmtTime(e.created_at)}</td>
      <td class="mono small">${esc(e.target_type)}<br>${esc(e.target_id.slice(0, 14))}…</td>
      <td>${esc(e.action)}</td>
      <td class="small">${esc(e.reason || "")}</td></tr>`).join("")}</table>` : `<div class="empty">暂无修改历史</div>`;
}

/* ============================ 页面：报告中心 ============================ */

RENDER.reports = async function (main) {
  const runsList = (await api("/api/runs")).data.runs;
  const r = await api("/api/reports");
  if (!handleErr(r)) return;
  const reports = r.data.reports;
  main.innerHTML = `
  <div class="page-head">
    <div><h1>报告中心</h1><div class="sub">统计数字由程序计算；报告引用固定输入版本，后续人工修正需重新生成。
      导出仅写项目 outputs/ 目录，路径由服务生成。</div></div>
    <button class="btn primary" onclick="openGenReport()">＋ 生成报告</button>
  </div>
  <div class="card card-pad">
    ${reports.length ? `<table class="tbl">
      <tr><th>报告</th><th>运行</th><th>生成时间</th><th>文件</th><th>操作</th></tr>
      ${reports.map(rep => `<tr>
        <td class="mono">${esc(rep.id)}</td>
        <td class="small">${(rep.run_ids || []).map(id => {
          const u = runsList.find(x => x.id === id);
          return esc(u ? u.name : id.slice(0, 12)); }).join("、")}</td>
        <td class="small muted">${fmtTime(rep.created_at)}</td>
        <td class="small">${Object.keys(rep.output_paths || {}).map(f => `.${f}`).join(" ")}</td>
        <td><div class="btn-row">
          <button class="btn sm" onclick="previewReport('${rep.id}')">预览</button>
          <a class="btn sm" href="/api/reports/${rep.id}/file?fmt=md" download>MD</a>
          <a class="btn sm" href="/api/reports/${rep.id}/file?fmt=json" download>JSON</a>
        </div></td>
      </tr>`).join("")}</table>` : `<div class="empty"><div class="big">▤</div>还没有报告</div>`}
  </div>
  <div class="card card-pad" id="report-preview" style="display:none">
    <h3>报告预览 <span class="hint">Markdown 渲染（表格/列表）</span></h3>
    <div class="report-md" id="report-md"></div>
  </div>`;
};

function openGenReport() {
  api("/api/runs").then(r => {
    const runs = r.data.runs;
    if (!runs.length) return toast("请先创建运行", "err");
    openModal(`
    <h3>生成报告</h3>
    <div class="field"><label class="f">选择运行（可多选）</label>
      ${runs.map(u => `<div class="checkline"><input type="checkbox" class="rep-run" value="${u.id}">
        <span>${esc(u.name)} <span class="muted small">(${esc(u.state)} · ${u.counters.jobs_unique || 0} 岗位)</span></span></div>`).join("")}</div>
    <div class="checkline"><input type="checkbox" id="rep-pending" checked>
      <span>包含待核实区（include_pending）</span></div>
    <div class="checkline"><input type="checkbox" id="rep-demo">
      <span>包含演示数据（默认关闭；勾选后报告整体标注"含演示数据"）</span></div>
    <div class="btn-row" style="justify-content:flex-end">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn primary" onclick="submitGenReport()">生成 MD + JSON</button>
    </div>`);
  });
}

async function submitGenReport() {
  const runIds = $$(".rep-run").filter(c => c.checked).map(c => c.value);
  if (!runIds.length) return toast("请选择至少一个运行", "err");
  const r = await api("/api/reports", { method: "POST", body: {
    run_ids: runIds, include_pending: $("#rep-pending").checked,
    include_demo: $("#rep-demo").checked, formats: ["md", "json"] } });
  if (!handleErr(r)) return;
  closeModal();
  toast("报告已生成：" + r.data.paths.md, "ok");
  routeTo();
  setTimeout(() => previewReport(r.data.report_id), 400);
}

async function previewReport(reportId) {
  const res = await fetch(`/api/reports/${reportId}/file?fmt=md`);
  const md = await res.text();
  const box = $("#report-preview");
  box.style.display = "block";
  $("#report-md").innerHTML = mdToHtml(md);
  box.scrollIntoView({ behavior: "smooth" });
}

function mdToHtml(md) {
  const lines = md.split("\n");
  let html = "", inTable = false, inList = false;
  const inline = (s) => esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/`(.+?)`/g, "<code>$1</code>")
    .replace(/\[(.+?)\]\((.+?)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  for (const line of lines) {
    if (/^\|/.test(line)) {
      if (/^\|[\s:-]+\|/.test(line)) continue;
      if (!inTable) { html += "<table>"; inTable = true; }
      const cells = line.split("|").slice(1, -1).map(c => c.trim());
      html += "<tr>" + cells.map(c => `<td>${inline(c)}</td>`).join("") + "</tr>";
      continue;
    } else if (inTable) { html += "</table>"; inTable = false; }
    if (/^- /.test(line)) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${inline(line.slice(2))}</li>`;
      continue;
    } else if (inList) { html += "</ul>"; inList = false; }
    if (line.startsWith("### ")) html += `<h3>${inline(line.slice(4))}</h3>`;
    else if (line.startsWith("## ")) html += `<h2>${inline(line.slice(3))}</h2>`;
    else if (line.startsWith("# ")) html += `<h1>${inline(line.slice(2))}</h1>`;
    else if (line.trim()) html += `<p>${inline(line)}</p>`;
  }
  if (inTable) html += "</table>";
  if (inList) html += "</ul>";
  return html;
}

/* ============================ 页面：设置 ============================ */

RENDER.settings = async function (main) {
  const r = await api("/api/settings");
  if (!handleErr(r)) return;
  const s = r.data.settings;
  const cliDetected = r.data.cli_detected === true;
  const loginSteps = (r.data.login_steps || []).map((t, i) => `<li>${esc(t)}</li>`).join("");
  // 采集浏览器来源：下拉框必须能**如实表示**三种合法取值，绝不把 stored-cookie
  // 误显示成「自动」（旧实现用「不是 existing-browser 就选 auto」的兜底，会把
  // 专用浏览器通道显示成自动，保存时静默降级）。
  const rawSource = String(s.boss_browser_source || "").trim();
  const KNOWN_SOURCES = ["auto", "stored-cookie", "existing-browser"];
  // "" 与 "auto" 语义等价（后端 _normalize_source 归一），统一按 "auto" 决定选中项
  const canonSource = (rawSource === "" || rawSource === "auto") ? "auto" : rawSource;
  const unknownSourceOption = KNOWN_SOURCES.indexOf(canonSource) >= 0 ? "" :
    `<option value="${esc(canonSource)}" selected>未知来源（${esc(canonSource)}）— 保持原值不改</option>`;
  main.innerHTML = `
  <div class="page-head"><div><h1>设置</h1><div class="sub">日常使用只需登录 BOSS、输入关键词、搜索并整理。</div></div></div>
  <div class="card card-pad">
    <h3>${cliDetected ? "已检测到 BOSS 采集工具，无需重复安装" : "未找到 BOSS 采集工具"}</h3>
    <p>${cliDetected ? "网站会自动调用它，你不需要运行命令或填写路径。工具已安装不代表平台一定允许搜索和读取详情。" : "当前项目依赖这个工具获取真实岗位。请展开下方高级配置，检查路径或查看安装指引。"}</p>
    <a class="btn primary" href="#/">返回搜索</a>
  </div>
  <details class="card card-pad"${cliDetected ? "" : " open"}>
  <summary>高级配置与安装指引（排查问题时使用）</summary>
  <div class="grid c2">
    <div class="card card-pad">
      <h3>真实数据源（BOSS 直聘）</h3>
      <div class="small muted mb8">当前生效上游目录：<span class="mono">${esc(r.data.effective_data_dir)}</span><br>
        来源：${r.data.data_dir_source === "setting" ? "数据库设置" : r.data.data_dir_source === "env" ? "环境变量" : "默认目录"} ·
        网站链路：${esc(r.data.website_channel || "CLI")}（${esc(r.data.mcp_note || "")}）</div>
      <div class="field"><label class="f">boss CLI 可执行文件路径</label>
        <input type="text" id="st-cli" value="${esc(s.boss_cli_path || "")}" placeholder="例如 C:\\tools\\boss-agent-cli\\boss.exe">
        <div class="f-help">通常留空即可，系统会自动查找。${r.data.effective_cli_path ? "当前检测到：" + esc(r.data.effective_cli_path) : "只有自动查找失败时才需要手动指定。"}</div></div>
      <div class="field"><label class="f">上游专用数据目录（可选）</label>
        <input type="text" id="st-datadir" value="${esc(s.upstream_data_dir || "")}" placeholder="默认 .local/upstream"></div>
      <button class="btn primary" onclick="saveSettings()">保存</button>
    </div>
    <div class="card card-pad">
      <h3>维护人员使用：安装与终端登录</h3>
      <p class="small muted">${cliDetected ? "本机已检测到工具，无需执行下面的安装命令。" : "安装完成后刷新本页重新检测。"}日常登录请使用首页的“登录 BOSS”。</p>
      <div class="field"><label class="f">安装命令（可复制）</label>
        <textarea id="st-install" rows="2" readonly>${esc(r.data.install_hint || "")}</textarea>
        <div class="btn-row mt8"><button class="btn sm" onclick="copyText('st-install')">复制安装命令</button></div></div>
      <div class="field"><label class="f">登录步骤（在本机终端执行，不向任何人提供 Cookie/令牌/验证码）</label>
        <ol class="small" style="padding-left:18px;line-height:1.9">${loginSteps}</ol>
        <div class="btn-row"><button class="btn sm" onclick="copyText('st-login')">复制登录命令</button>
          <textarea id="st-login" rows="1" style="display:none">${esc((r.data.login_steps || []).filter(t => t.includes("--data-dir")).join("\n"))}</textarea></div></div>
    </div>
  </div>
  <div class="card card-pad">
    <h3>采集浏览器（诊断用）</h3>
    <div class="small muted mb8">默认由 finintel 自己打开浏览器采集。「用本项目的专用浏览器」会打开一个
      <b>只属于本项目</b>的独立 Chrome 窗口（登录与搜索都在这个窗口里完成），不动用你日常 Chrome 的会话；
      「用我自己已登录的 Chrome」则复用你那个已登录的浏览器（需先以调试模式启动它）。</div>
    <div class="grid c2">
      <div class="field"><label class="f">采集浏览器来源</label>
        <select id="st-browser-source" data-source="${esc(rawSource)}">
          <option value="auto"${canonSource === "auto" ? " selected" : ""}>自动（默认）</option>
          <option value="stored-cookie"${canonSource === "stored-cookie" ? " selected" : ""}>用本项目的专用浏览器（推荐）</option>
          <option value="existing-browser"${canonSource === "existing-browser" ? " selected" : ""}>用我自己已登录的 Chrome</option>
          ${unknownSourceOption}
        </select>
        <div class="f-help">「专用浏览器」登录后即可搜索，是本项目推荐的采集方式；保存时不会改动你未修改的取值。</div></div>
      <div class="field"><label class="f">调试地址</label>
        <input type="text" id="st-cdp-url" data-url="${esc(s.boss_cdp_url || "")}" value="${esc(s.boss_cdp_url || "")}"
               placeholder="http://127.0.0.1:9222">
        <div class="f-help">只接受本机地址（localhost / 127.0.0.1），不允许指向其他机器；留空则由上游自动探测本机端口。</div></div>
    </div>
    <div class="field"><label class="f">以调试模式启动浏览器（Windows，可复制）</label>
      <textarea id="st-browser-cmd" rows="2" readonly>"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222</textarea>
      <div class="btn-row mt8"><button class="btn sm" onclick="copyText('st-browser-cmd')">复制启动命令</button></div>
      <div class="f-help">Chrome 装在别处时把路径换成你自己的 chrome.exe；端口改了，就把上面的调试地址改成同一个端口。</div></div>
  </div>
  </details>
  <div class="card card-pad">
    <h3>AI 模型（简历事实提取 / 岗位匹配 / 面试准备）</h3>
    <div class="grid c2">
      <div class="field"><label class="f">Provider</label>
        <input type="text" id="st-llm-provider" value="${esc(s.llm_provider || "openai_compat")}" placeholder="openai_compat"></div>
      <div class="field"><label class="f">API Base URL</label>
        <input type="text" id="st-llm-base" value="${esc(s.llm_api_base || "")}" placeholder="https://api.example.com/v1"></div>
      <div class="field"><label class="f">模型名</label>
        <input type="text" id="st-llm-model" value="${esc(s.llm_model || "")}" placeholder="gpt-4o-mini / deepseek-chat 等"></div>
      <div class="field"><label class="f">API Key（不回显；留空表示不修改）</label>
        <input type="password" id="st-llm-key" value="" placeholder="sk-..." autocomplete="off"></div>
    </div>
    <div class="checkline mt8"><input type="checkbox" id="st-demo-mode" ${s.demo_mode === "1" ? "checked" : ""}>
      <span>演示模式（未配置模型时，匹配/面试用内置规则匹配器，仅演示）</span></div>
    <div class="f-help mt8">未配置模型时，求职者侧「用 AI 分析」会返回「AI 服务未配置」，不会回退为演示结果。密钥仅存本机数据库，不进入日志/报告/前端。</div>
    <button class="btn primary" onclick="saveSettings()">保存全部设置</button>
  </div>
  <div class="card card-pad">
    <h3>证据边界（本项目红线）</h3>
    <ul class="small" style="padding-left:18px;line-height:2">
      <li>职责是主要证据；任职要求仅辅助，不构成公司正在开展业务的证据</li>
      <li>保留"计划、探索、维护、现有"等限定词，不改写为已完成</li>
      <li>不推断营收、预算、上线日期、市场份额、战略转型</li>
      <li>招聘主体、品牌、集团不自动等同；同名企业不自动合并</li>
      <li>首次发现 ≠ 发布日期；重新抓到 ≠ 新发布；搜不到 ≠ 已下架</li>
      <li>重复 JD、同岗跨城市转载不增加独立证据组数</li>
      <li>演示数据源为合成数据，不能冒充真实采集结果</li>
    </ul>
  </div>`;
};

function copyText(id) {
  const el = $("#" + id);
  if (!el) return;
  el.select();
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(el.value).then(() => toast("已复制", "ok"), () => toast("请手动复制", "err"));
  } else {
    document.execCommand("copy");
    toast("已复制", "ok");
  }
}

async function saveSettings() {
  const items = [["boss_cli_path", $("#st-cli").value.trim()],
                 ["upstream_data_dir", $("#st-datadir").value.trim()],
                 ["llm_provider", $("#st-llm-provider").value.trim()],
                 ["llm_api_base", $("#st-llm-base").value.trim()],
                 ["llm_model", $("#st-llm-model").value.trim()],
                 ["demo_mode", $("#st-demo-mode").checked ? "1" : "0"]];
  const apiKey = $("#st-llm-key").value.trim();
  if (apiKey) items.push(["llm_api_key", apiKey]);  // 留空不覆盖旧 key
  // 采集浏览器通道：只写「用户确实改过、且新值可被下拉框表示」的项。
  // 后端对通道键的每次写入都会 invalidate_all（换通道即换指纹），因此**绝不能**
  // 每次保存都把未改动/无法表示的通道值原样回写——那会把通道就绪状态无谓清空，
  // 也是旧实现「保存 = 把 stored-cookie 静默降级成 auto」的根因。
  const browserEl = $("#st-browser-source");
  const cdpEl = $("#st-cdp-url");
  if (browserEl) {
    const original = browserEl.getAttribute("data-source") || "";
    const originalCanon = (original === "" || original === "auto") ? "auto" : original;
    const representable = ["auto", "stored-cookie", "existing-browser"].indexOf(originalCanon) >= 0;
    const chosen = browserEl.value;                 // 只能是三家之一
    const next = chosen === "auto" ? "" : chosen;
    const nextCanon = (next === "" || next === "auto") ? "auto" : next;
    if (representable && nextCanon !== originalCanon) {
      items.push(["boss_browser_source", next]);
    }
    // 原值不可表示（未知/未来取值）时**跳过**写入，保持数据库里的原值不变。
  }
  if (cdpEl) {
    const originalCdp = cdpEl.getAttribute("data-url") || "";
    const nextCdp = cdpEl.value.trim();
    if (nextCdp !== originalCdp) items.push(["boss_cdp_url", nextCdp]);
  }
  for (const [key, value] of items) {
    const r = await api("/api/settings", { method: "POST", body: { key, value } });
    if (!r.ok) return handleErr(r);
  }
  toast("设置已保存", "ok");
}

/* ============================ 抽屉 / 模态框 ============================ */

function openDrawer(html) {
  $("#drawer-root").innerHTML = `<div class="drawer-mask" onclick="if(event.target===this)closeDrawer()">
    <div class="drawer">${html}</div></div>`;
  setTimeout(() => {
    $$(".evidence-box").forEach(box => {
      const src = box.querySelector(".ev-src");
      if (src) src.dataset.text = src.textContent;
    });
  }, 0);
}
function closeDrawer() { $("#drawer-root").innerHTML = ""; }

function openModal(html) {
  $("#modal-root").innerHTML = `<div class="modal-mask" onclick="if(event.target===this)closeModal()">
    <div class="modal">${html}</div></div>`;
}
function closeModal() { $("#modal-root").innerHTML = ""; }

/* ============================ 启动 ============================ */

async function refreshPendingCount() {
  try {
    const r = await api("/api/review/pending");
    if (r.ok) {
      const d = r.data;
      pendingReviewCount = d.findings.length + d.pairs.length + d.aliases.length;
      renderNav();
    }
  } catch (e) { /* ignore */ }
}

async function boot() {
  // 默认进首页；「新建运行」属高级入口，由 LEGACY_REDIRECT 把 #/runs?create=1 分流到
  // #/diag/runs?create=1（localStorage.fin_create_run 仍是旧页弹窗的传输标记，交给 RENDER.runs 消费）。
  try {
    if (!location.hash) history.replaceState(null, "", "#/");
  } catch (e) { /* 隐私模式下忽略 */ }
  const h = await api("/api/health");
  if (h.ok) {
    LABELS = Object.assign(LABELS, h.data.labels);
    TAXONOMY = h.data.taxonomy;
  }
  await refreshPendingCount();
  routeTo();
}
document.addEventListener("DOMContentLoaded", boot);
