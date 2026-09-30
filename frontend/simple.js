/* finintel 三步主流程前端（T04）
 * 登录 BOSS → 输入关键词 → 一次点击自动整理。
 * 无构建步骤，直接 <script> 引入；复用 app.js 的工具函数与 RENDER 注册表。
 * 普通界面只消费 user_stage / user_message / actions，不出现任何工程错误码。
 */
"use strict";

/* ============================ 常量与状态 ============================ */

const SF_SUGGESTIONS = ["消费金融 产品经理", "信贷 研发", "资金清结算 产品"];
const SF_KEYWORD_KEY = "fin.simple.keyword";
/* 岗位类型：默认「产品与研发」，与首页文案「默认关注产品与研发岗位」一致 */
const SF_JOB_TYPES = [["product_engineering", "产品与研发"], ["product", "产品"],
                      ["engineering", "研发"], ["all", "全部"]];

/* 登录 9 态文案与可用操作（设计 §6 / 需求 §4 状态表）；action id 与 user_view 对齐 */
const SF_LOGIN_STATES = {
  disconnected:        { text: "登录 BOSS 后即可搜索真实岗位", actions: ["login"] },
  starting:            { text: "正在打开 BOSS 登录页面",        actions: ["cancel"] },
  waiting_user:        { text: "请在打开的 BOSS 页面完成登录，并保留此窗口供搜索使用",  actions: ["reopen", "cancel"] },
  verifying:           { text: "登录已提交，正在确认连接",      actions: ["wait", "cancel"] },
  connected:           { text: "BOSS 已连接",                   actions: ["search"] },
  timeout_or_cancelled:{ text: "本次登录未完成，关键词已保留",  actions: ["login"] },
  platform_verification:{ text: "BOSS 需要你完成验证",          actions: ["open_verify"] },
  env_not_ready:       { text: "搜索服务尚未准备好",            actions: ["open_diag"] },
  auth_expired:        { text: "BOSS 登录已失效，已保留当前进度", actions: ["login_and_resume"] },
};
const SF_LOGIN_TERMINAL = ["connected", "timeout_or_cancelled", "platform_verification", "env_not_ready"];

/* 任务阶段 → 用户文案（需求 §7） */
const SF_STAGES = ["checking_env", "pending_login", "creating", "searching",
  "fetching_details", "deduping", "analyzing", "organizing", "completed"];
const SF_STAGE_TEXT = {
  checking_env: "正在检查搜索服务",
  creating: "正在创建任务",
  searching: "正在搜索岗位",
  fetching_details: "正在读取岗位详情",
  deduping: "正在整理岗位",
  analyzing: "正在整理公司方向",
  organizing: "正在整理公司方向",
  completed: "整理完成",
};
const SF_TASK_TERMINAL = ["completed", "partial", "cancelled", "failed", "paused"];

/* 采集通道类失败的统一文案：只说「当前搜索连接」，绝不归因成「你没有登录」
   （账号已验证与浏览器会话可用是两回事；详细原因只进诊断页） */
const SF_CHANNEL_DOWN_TEXT = "当前搜索未能连接到可用的 BOSS 浏览器会话，搜索已暂停，关键词已保留。";
const SF_CHANNEL_NOT_READY_TEXT = "账号登录已验证，当前搜索连接尚未就绪。";
/* 定位符过期（30 分钟 TTL）且刷新额度已用尽：只能重新搜索，**不给「继续」** */
const SF_STALE_LOCATOR_TEXT = "本批岗位的定位信息已过期（约 30 分钟有效），无法继续读取详情，请重新搜索。";
/* 定位符过期但刷新额度仍在：给「继续」，点了会真的刷新定位后继续读取详情（不是假按钮） */
const SF_STALE_LOCATOR_RESUMABLE_TEXT = "本批岗位的定位信息已过期（约 30 分钟有效）；点「继续」会自动刷新定位后继续读取详情。";

/* 工程码 → 用户话术（普通界面绝不外泄内部码） */
const SF_ERR_TEXT = {
  AUTH_REQUIRED: "需要先登录 BOSS",
  AUTH_EXPIRED: "BOSS 登录已失效，请重新登录",
  AI_NOT_CONFIGURED: "岗位已保存，暂未完成整理",
  PLATFORM_VERIFICATION_REQUIRED: "BOSS 需要你完成验证",
  LOGIN_RISK_CONTROL: "BOSS 需要你完成验证",
  DEPENDENCY_MISSING: "搜索服务尚未准备好",
  BROWSER_KERNEL_MISSING: "搜索服务尚未准备好",
  CDP_UNAVAILABLE: SF_CHANNEL_DOWN_TEXT,
  BROWSER_SESSION_NOT_FOUND: SF_CHANNEL_DOWN_TEXT,
  OWN_BROWSER_NOT_READY: SF_CHANNEL_DOWN_TEXT,
  OWN_BROWSER_NO_SESSION: SF_CHANNEL_DOWN_TEXT,
  CHANNEL_NOT_READY: SF_CHANNEL_DOWN_TEXT,
  CHANNEL_NOT_FROZEN: SF_CHANNEL_DOWN_TEXT,
  CHANNEL_CHANGED: SF_CHANNEL_DOWN_TEXT,
  UPSTREAM_RESULT_LIMIT: "这次搜索范围过大，请缩小范围（例如限定城市）后重试",
  STALE_LOCATOR: SF_STALE_LOCATOR_TEXT,
  INVALID_ANALYSIS: "请求有误，请检查关键词",
  TOOL_NOT_ALLOWED: "当前状态不支持这个操作",
  OUTPUT_WRITE_FAILED: "报告写入失败，请稍后重试",
};

/* 采集通道：默认由 finintel 打开浏览器采集；用户可改成「用自己已登录的 Chrome」。
   普通界面只给中文步骤，不出现任何命令行字样（启动命令与调试地址只在诊断页）。 */
const SF_BROWSER_SOURCE_KEY = "boss_browser_source";
const SF_BROWSER_OWN_STEPS = [
  "先把 Chrome 完全退出（右下角托盘里的图标也要退出）",
  "用「调试模式」重新打开 Chrome（具体做法见设置页的「采集浏览器」）",
  "在这个 Chrome 里打开 BOSS 直聘，并确认已经是登录状态",
  "回到本页勾选下面这一项，再点「搜索并整理」",
];
const SF_BROWSER_STATE = { source: "" };

/* 环境风控（code 37）的人工确认入口：必须在**本项目的专用浏览器**里打开 BOSS 官方页面——
   被标记的就是专用 profile 那个会话，在日常浏览器里确认毫无作用。
   后端 /api/channel/open-official 只发 CDP 浏览器级命令，绝不发 search/detail/login，
   也**绝不**重新登录。专用浏览器未运行时后端返回明确错误，这里如实提示而不是静默失败。 */
const SF_OFFICIAL_ENDPOINT = "/api/channel/open-official";

const SF_BOSS = { state: "disconnected", account_state: "", user_message: "", login_id: null,
                  actions: [], channel: null, channel_ready: false, collect_ready: false };
/* 首页表单草稿：跨页面保留关键词与筛选；reuse_run_id 用于「继续搜索」增量任务 */
const SF_DRAFT = { query: "", city: "", job_type: "product_engineering",
  source: "zhipin",
                   reuse_run_id: null, pending_run_id: null };
let SF_CURRENT = { run_id: null, query: "" };
/* 当前正在展示的结果页任务 id：轮询/异步回调据此丢弃过期响应，切换任务不串台 */
let SF_ACTIVE_RUN = null;
/* run_id → 本次提交时用户选定的岗位类型。
   后端「继续搜索」(reuse_run_id) 有意不刷新 run 存量口径（继续≠改历史任务），
   所以结果接口要显式带 ?job_type=，否则会按旧口径渲染；历史里点开的任务无此项，走 run 自身口径。 */
const SF_SCOPE = {};
let SF_PAGE_TIMERS = [];
let SF_LOGIN_TIMER = null;
let SF_PAGE_BUSY = false;
const SF_JOB_CACHE = {};

/* ============================ 小工具 ============================ */

function sfErrText(json, fallback) {
  const e = json && json.error;
  if (!e) return fallback || "操作失败，请稍后重试";
  if (SF_ERR_TEXT[e.code]) return SF_ERR_TEXT[e.code];
  const msg = e.message ? String(e.message) : "";
  if (msg && !/[A-Z_]{4,}/.test(msg)) return msg;   // 后端已给中文文案
  return fallback || "操作失败，请稍后重试";
}

function sfLoginStateOf(obj) {
  if (!obj) return "disconnected";
  const s = obj.user_stage || obj.status || obj.state || "";
  if (SF_LOGIN_STATES[s]) return s;
  if (s === "timeout" || s === "cancelled") return "timeout_or_cancelled";
  if (s === "partial" || s === "failed") return "env_not_ready";
  return "disconnected";
}

function sfSetBoss(obj) {
  const st = sfLoginStateOf(obj);
  SF_BOSS.state = st;
  // 账号事实（后端 account_state）：user_stage 会被「通道未就绪」打扮成 env_not_ready，
  // 顶栏两维判定必须用这个**未被打扮**的账号事实，否则「已验证但通道未就绪」会被误读成
  // 「搜索服务未就绪」（真实路径 bug）。
  SF_BOSS.account_state = (obj && obj.account_state) || "";
  SF_BOSS.user_message = (obj && obj.user_message) || "";
  if (obj && obj.login_id) SF_BOSS.login_id = obj.login_id;
  SF_BOSS.actions = (obj && obj.actions) || [];
  SF_BOSS.channel = (obj && obj.channel) || null;
  // channel_ready = 通道确凿就绪（后端 channel.state === 'ready'）；与 collect_ready
  // 区分开：collect_ready 对 unknown / existing-browser 的 24h 兜底也会为真，
  // 那是「允许试一次」，不是「已经就绪」，顶栏绿点只认 channel_ready。
  SF_BOSS.channel_ready = !!(
    (obj && obj.channel_ready === true) ||
    (obj && obj.channel && obj.channel.state === "ready")
  );
  SF_BOSS.collect_ready = !!(obj && obj.collect_ready);
}

/* 账号是否**已验证**（顶栏「账号 × 通道」两维判定的第一维）。
   优先用后端 account_state；老响应没有该字段时回退到登录阶段。 */
function sfAccountConnected() {
  const s = SF_BOSS.account_state || "";
  if (s) return s === "connected";
  return SF_BOSS.state === "connected";
}

/* 仅允许安全字符进入 onclick 字符串字面量 */
function sfId(v) { return String(v === null || v === undefined ? "" : v).replace(/[^A-Za-z0-9_.-]/g, ""); }

function sfBtn(kind, onclick, text) {
  return `<button class="btn ${kind} sm" onclick="${onclick}">${esc(text)}</button>`;
}

/* action id → 按钮（文案优先用后端 label；id 对齐 user_view） */
const SF_ACTION_BTN = {
  login: l => sfBtn("primary", "sfStartLogin()", l || "登录 BOSS"),
  login_and_resume: l => sfBtn("primary", "sfStartLogin()", l || "重新登录并继续"),
  cancel: l => sfBtn("", `sfCancelLogin('${sfId(SF_BOSS.login_id)}')`, l || "取消"),
  reopen: l => sfBtn("", `sfReopenLogin('${sfId(SF_BOSS.login_id)}')`, l || "重新打开登录页面"),
  open_verify: l => sfBtn("primary", "sfStartLogin()", l || "打开官方验证页面"),
  /* 环境风控（code 37）：在**专用浏览器**里打开官方页面人工确认，**不**触发重新登录
     （后端 user_view 亦如此）。绝不能把这个动作映射到 sfStartLogin()——那会 POST
     /api/auth/login，与文案自相矛盾。文案要如实说明开在哪个浏览器。 */
  check_official: l => sfBtn("", "sfOpenOfficialPage()", l || "在专用浏览器中打开 BOSS 官方页面"),
  open_diag: l => sfBtn("", "location.hash='#/diag/settings'", l || "查看修复说明"),
  search: l => `<a class="btn sm" href="#/">${esc(l || "开始搜索")}</a>`,
  /* 定位符过期这类不可续采的情况：只能回去重新搜一次（关键词保留在首页），
     绝不渲染成「继续」——那是点了一定失败的假按钮 */
  search_again: l => `<a class="btn primary" href="#/">${esc(l || "重新搜索")}</a>`,
  resume: l => (SF_CURRENT.run_id
    ? sfBtn("primary", `sfResumeTask('${sfId(SF_CURRENT.run_id)}')`, l || "继续") : ""),
  retry_organize: l => sfBtn("", `sfRetryOrganize('${sfId(SF_CURRENT.run_id)}')`, l || "重试整理"),
  view_result: l => sfBtn("", `sfShowResult('${sfId(SF_CURRENT.run_id)}')`, l || "查看已完成部分"),
  /* 「获取最新结果」：唯一会跳过上游本地缓存、真正联网重取的动作（默认路径允许命中缓存） */
  refresh: l => (SF_CURRENT.run_id
    ? sfBtn("", `sfRefreshTask('${sfId(SF_CURRENT.run_id)}')`, l || "获取最新结果") : ""),
  wait: () => "",
};

/* 优先消费后端 actions；缺失时退回该状态的标准操作 */
function sfActionButtons(actions, fallbackIds) {
  const list = (actions && actions.length) ? actions : (fallbackIds || []).map(id => ({ id }));
  return list.map(a => {
    const id = typeof a === "string" ? a : (a && a.id);
    const label = (a && typeof a === "object" && a.label) || null;
    return SF_ACTION_BTN[id] ? SF_ACTION_BTN[id](label) : "";
  }).join("");
}

function sfClearPageTimers() {
  SF_PAGE_TIMERS.forEach(t => clearInterval(t));
  SF_PAGE_TIMERS = [];
}

function sfPageInterval(ms, fn) {
  const id = setInterval(fn, ms);
  SF_PAGE_TIMERS.push(id);
  return id;
}

function sfCandidate(j) {
  return j.title ? `${j.title}` : "岗位";
}

function sfAlert(kind, title, text, actions) {
  return `<div class="sf-alert sf-alert-${esc(kind)}">
    <div class="sf-alert-title">${esc(title)}</div>
    ${text ? `<div class="sf-alert-text">${esc(text)}</div>` : ""}
    ${actions ? `<div class="btn-row mt8">${actions}</div>` : ""}
  </div>`;
}

function sfEmpty(big, title, hint, actions) {
  return `<div class="sf-empty"><div class="sf-empty-big">${big}</div>
    <div class="sf-empty-title">${esc(title)}</div>
    ${hint ? `<div class="sf-empty-hint">${esc(hint)}</div>` : ""}
    ${actions ? `<div class="btn-row sf-empty-actions">${actions}</div>` : ""}
  </div>`;
}

/* ============================ 顶栏与登录组件 ============================ */

const SF_PILL_TEXT = {
  connected: "BOSS 已连接",
  connected_confirming: "BOSS 已连接，正在确认搜索通道",
  channel_not_ready: SF_CHANNEL_NOT_READY_TEXT,
  starting: "正在打开登录页面",
  waiting_user: "等待完成登录",
  verifying: "正在确认连接",
  timeout_or_cancelled: "本次登录未完成",
  platform_verification: "需要完成验证",
  env_not_ready: "搜索服务未就绪",
  auth_expired: "登录已失效",
  disconnected: "BOSS：未登录",
};
const SF_LOGIN_ACTIVE = ["starting", "waiting_user", "verifying"];

function sfPillText() {
  const ch = SF_BOSS.channel || {};
  // 两维判定：先用**账号事实**（未被通道打扮过）决定走「账号侧」还是「通道侧」文案，
  // 再按通道状态给通道侧文案。绝不因 user_stage 被覆盖成 env_not_ready 就说
  // 「搜索服务未就绪」——真相是「账号已验证、当前搜索连接尚未就绪」。
  if (sfAccountConnected()) {
    if (ch.last_error_code === "ENVIRONMENT_RISK") return "BOSS 登录已验证，当前采集被平台拦截";
    if (ch.state === "ready") return SF_PILL_TEXT.connected;
    if (ch.state === "not_ready") return SF_PILL_TEXT.channel_not_ready;
    // unknown（还没确认过）：中性说法，不宣称已就绪
    return SF_PILL_TEXT.connected_confirming;
  }
  return SF_PILL_TEXT[SF_BOSS.state] || SF_PILL_TEXT.disconnected;
}
function sfLoginBusy() { return SF_LOGIN_ACTIVE.indexOf(SF_BOSS.state) >= 0; }

/* 绿点的**唯一**依据：账号已验证、且当前采集通道确凿就绪（channel.state === 'ready'）。
   ``collect_ready`` 对 unknown（允许放行一次探测）与 existing-browser 的 24h 兜底
   都可能为真，那是「允许试一次」，**不是**「已经就绪」——顶栏/首页都不许据此画绿点。 */
function sfChannelReady() {
  return sfAccountConnected() && SF_BOSS.channel_ready === true;
}

function renderTopbar() {
  const el = $("#topbar-right");
  if (!el) return;
  const connected = sfAccountConnected();
  // 绿点严格 ⇔ 通道就绪：unknown 只表示「后台允许放行一次探测」，不等于「已经就绪」
  const ready = sfChannelReady();
  const showLoginBtn = !connected && !sfLoginBusy();
  el.innerHTML = `
    <div class="sf-account">
      <span class="sf-login-pill ${ready ? "is-ok" : ""}" id="sf-status-chip">
        <span class="sf-dot ${ready ? "is-ok" : ""}"></span>${esc(sfPillText())}
      </span>
      ${showLoginBtn ? `<button class="btn primary sm" id="sf-login-btn" onclick="sfStartLogin()">登录 BOSS</button>` : ""}
      <button class="sf-icon-btn" id="sf-account-btn" onclick="sfToggleMenu(event)"
              aria-haspopup="true" title="账户与设置">⋯</button>
      <div class="sf-menu" id="sf-account-menu" hidden>
        <div class="sf-menu-head">
          <div class="sf-menu-title">账户与登录</div>
          <div id="sf-menu-login"></div>
        </div>
        <a class="sf-menu-item" href="#/history">历史搜索</a>
        <a class="sf-menu-item" href="#/js-sessions">面试准备</a>
        <a class="sf-menu-item" href="#/diag/settings">设置</a>
        <a class="sf-menu-item" href="#/diag/dashboard">技术诊断</a>
        <div class="sf-menu-sep"></div>
        <div class="sf-menu-title">功能模式</div>
        <div id="mode-switch"></div>
      </div>
    </div>`;
  sfRenderLogin($("#sf-menu-login"), { always: true });
}

function sfToggleMenu(ev) {
  if (ev) ev.stopPropagation();
  const m = $("#sf-account-menu");
  if (!m) return;
  m.hidden = !m.hidden;
  if (!m.hidden && typeof renderModeSwitch === "function") renderModeSwitch();
}

/* 登录组件：9 态文案 + 操作按钮。
 * 默认只在「需要用户注意」的状态下出现（进行中/待验证/失败或过期），
 * 未登录与已连接时普通页面保持干净（顶栏已表达状态）。菜单里用 always 强制显示。 */
function sfRenderLogin(host, opts) {
  if (!host) return;
  const st = SF_BOSS.state;
  const quiet = ["disconnected", "connected"].indexOf(st) >= 0;
  if (quiet && !(opts && opts.always)) { host.innerHTML = ""; return; }
  const meta = SF_LOGIN_STATES[st] || SF_LOGIN_STATES.disconnected;
  const msg = SF_BOSS.user_message || meta.text;
  const btns = sfActionButtons((opts && opts.actions) || SF_BOSS.actions, meta.actions);
  // 首页登录块同样守「绿点 ⇔ 通道就绪」：文案若说「正在确认搜索通道 / 连接尚未就绪」，
  // 就不该配一个绿点（口径与顶栏完全一致，避免同一状态两处两说）。
  const ok = sfChannelReady();
  host.innerHTML = `<div class="sf-login ${ok ? "is-ok" : ""}">
    <div class="sf-login-text"><span class="sf-dot ${ok ? "is-ok" : ""}"></span>${esc(msg)}</div>
    ${btns ? `<div class="sf-login-actions">${btns}</div>` : ""}
  </div>`;
}

/* 供普通界面复用的登录状态渲染（首页 / 结果页都调用它） */
function sfRenderLoginAll() {
  sfRenderLogin($("#sf-login"));
  renderTopbar();
}

async function sfRefreshBoss() {
  try {
    const r = await api("/api/auth/status");
    if (r.ok && r.data && r.data.boss) {
      sfSetBoss(r.data.boss);
      sfRenderLoginAll();
      // 刷新后仍在进行中的登录流程：重新接上轮询（POST /api/auth/login 返回同一活跃流程）
      if (SF_LOGIN_ACTIVE.indexOf(SF_BOSS.state) >= 0 && !SF_LOGIN_TIMER) {
        const id = await sfEnsureLoginId();
        if (id) sfPollLogin(id);
      }
    }
  } catch (e) { /* 接口未就绪时保持默认未登录 */ }
}

/* 取得当前活跃登录流程的 login_id（页面刷新后可重连；无活跃流程时即发起登录） */
async function sfEnsureLoginId() {
  if (SF_BOSS.login_id) return SF_BOSS.login_id;
  const r = await api("/api/auth/login", { method: "POST", body: {} });
  if (r.ok && r.data && r.data.login && r.data.login.login_id) {
    sfSetBoss(Object.assign({ state: "waiting_user" }, r.data.login));
    sfRenderLoginAll();
    return SF_BOSS.login_id;
  }
  return null;
}

async function sfRepairBrowser() {
  if (sfRepairBrowser.busy) return;
  sfRepairBrowser.busy = true;
  toast("正在修复并重开本工具的专用浏览器，登录数据会保留", "ok");
  try {
    const r = await api("/api/auth/browser/repair", { method: "POST", body: {} });
    if (!r.ok) { toast(sfErrText(r, "浏览器修复未完成"), "err"); return; }
    if (r.data.boss) { sfSetBoss(r.data.boss); sfRenderLoginAll(); }
    toast(r.data.message, "ok");
  } catch (e) { toast("浏览器修复请求未完成，请检查本地服务", "err"); }
  finally { sfRepairBrowser.busy = false; }
}

async function sfStartLogin() {
  sfSetBoss({ state: "starting", user_message: "" });
  sfRenderLoginAll();
  const r = await api("/api/auth/login", { method: "POST", body: {} });
  if (!r.ok) {
    sfSetBoss({ state: "env_not_ready", user_message: sfErrText(r, "暂时无法打开登录页面，请稍后重试") });
    sfRenderLoginAll();
    return;
  }
  const lg = (r.data && r.data.login) || {};
  sfSetBoss(Object.assign({ state: "waiting_user" }, lg));
  sfRenderLoginAll();
  if (lg.login_id) sfPollLogin(lg.login_id);
}

async function sfCancelLogin(loginId) {
  const id = loginId || await sfEnsureLoginId();
  if (id) await api(`/api/auth/login/${encodeURIComponent(id)}/cancel`, { method: "POST", body: {} });
  sfStopLoginPoll();
  sfSetBoss({ state: "timeout_or_cancelled", user_message: "" });
  sfRenderLoginAll();
}

async function sfReopenLogin(loginId) {
  const id = loginId || await sfEnsureLoginId();
  if (id) await api(`/api/auth/login/${encodeURIComponent(id)}/reopen`, { method: "POST" });
  sfSetBoss({ state: "starting", user_message: "" });
  sfRenderLoginAll();
  sfPollLogin(SF_BOSS.login_id);
}

function sfStopLoginPoll() {
  if (SF_LOGIN_TIMER) { clearInterval(SF_LOGIN_TIMER); SF_LOGIN_TIMER = null; }
}

/* 环境风控的唯一出口：让**本项目的专用浏览器**打开/聚焦一个 BOSS 官方页面页签，供用户
   在该浏览器里人工确认访问是否恢复。**必须**走专用浏览器——被标记的就是专用 profile 那个
   会话，在日常浏览器里打开毫无作用。后端只发 CDP 浏览器级命令，绝不发 search/detail/login，
   也**绝不**触发重新登录。专用浏览器未运行时后端返回明确错误，这里如实提示而非静默失败。 */
async function sfOpenOfficialPage() {
  const r = await api(SF_OFFICIAL_ENDPOINT, { method: "POST", body: {} });
  if (!r.ok) {
    toast(sfErrText(r, "专用浏览器当前没有运行，请先在登录页点「登录 BOSS」打开它"), "err");
    return;
  }
  const d = (r.data) || {};
  toast(d.already_open ? "已在专用浏览器里定位到 BOSS 官方页面" : "已在专用浏览器里打开 BOSS 官方页面", "ok");
}

function sfPollLogin(loginId) {
  sfStopLoginPoll();
  if (!loginId) return;
  SF_LOGIN_TIMER = setInterval(async () => {
    if (document.hidden) return;                 // 页面隐藏时暂停轮询
    const r = await api(`/api/auth/login/${encodeURIComponent(loginId)}`);
    if (!r.ok) {
      if (r.error && r.error.code === "INVALID_ANALYSIS") { sfStopLoginPoll(); }
      return;
    }
    const lg = (r.data && r.data.login) || {};
    sfSetBoss(Object.assign({ login_id: loginId }, lg));
    sfRenderLoginAll();
    if (SF_LOGIN_TERMINAL.indexOf(SF_BOSS.state) >= 0) {
      sfStopLoginPoll();
      if (SF_BOSS.state === "connected") {
        toast("BOSS 已连接", "ok");
        sfAfterLoginConnected();
      }
    }
  }, 2000);
}

/* 登录成功：若之前已点过「搜索并整理」，回到那次任务继续（不新建搜索） */
function sfAfterLoginConnected() {
  const runId = SF_DRAFT.pending_run_id;
  if (!runId) return;
  SF_DRAFT.pending_run_id = null;
  if (location.hash === "#/result/" + runId) sfRunTask(runId);   // 已在该结果页则直接续看
  else location.hash = "#/result/" + runId;
}

/* ---------- 采集通道（用你自己登录的 Chrome 采集） ---------- */

function sfOwnBrowserOn() { return SF_BROWSER_STATE.source === "existing-browser"; }

async function sfLoadBrowserChannel() {
  try {
    const r = await api("/api/settings");
    if (r.ok && r.data && r.data.settings) {
      SF_BROWSER_STATE.source = String(r.data.settings[SF_BROWSER_SOURCE_KEY] || "");
    }
  } catch (e) { /* 接口未就绪时保持默认采集方式 */ }
}

async function sfSetBrowserChannel(on) {
  const previous = SF_BROWSER_STATE.source;
  SF_BROWSER_STATE.source = on ? "existing-browser" : "";
  const r = await api("/api/settings", {
    method: "POST", body: { key: SF_BROWSER_SOURCE_KEY, value: SF_BROWSER_STATE.source },
  });
  if (!r.ok) {
    SF_BROWSER_STATE.source = previous;          // 保存失败则回滚本地状态
    toast(sfErrText(r, "采集方式没改成功，请稍后再试"), "err");
    return false;
  }
  toast(on ? "已改用你自己登录的 Chrome 采集" : "已改回默认采集方式", "ok");
  return true;
}

function sfBrowserChannelBlock(open) {
  if (!sfOwnBrowserOn()) return `<div class="sf-note">点击「登录 BOSS」会打开专用浏览器。完成登录后保留该窗口，回到这里输入关键词即可搜索。</div>`;
  return `<div class="sf-browser">
    <button class="sf-link" type="button" id="sf-browser-toggle">
      ${open ? "▾" : "▸"} 搜索一直打不开？换用你自己登录的浏览器采集</button>
    <div id="sf-browser-panel"${open ? "" : " hidden"}>
      <div class="sf-note">按顺序做这 4 步，不用记任何命令：</div>
      <ol class="small" style="padding-left:18px;line-height:1.9">
        ${SF_BROWSER_OWN_STEPS.map(s => `<li>${esc(s)}</li>`).join("")}
      </ol>
      <div class="checkline"><input type="checkbox" id="sf-browser-own"${sfOwnBrowserOn() ? " checked" : ""}>
        <span>用你自己登录的 Chrome 采集</span></div>
      <div class="sf-note" id="sf-browser-now">当前：${sfOwnBrowserOn() ? "用你自己的 Chrome" : "默认（由 finintel 打开浏览器）"}</div>
      <div class="btn-row mt8"><a class="btn sm" href="#/diag/settings">查看启动步骤与调试地址</a></div>
    </div>
  </div>`;
}

function sfBindBrowserChannel() {
  const toggle = $("#sf-browser-toggle");
  const panel = $("#sf-browser-panel");
  const box = $("#sf-browser-own");
  if (toggle && panel) {
    toggle.addEventListener("click", () => {
      panel.hidden = !panel.hidden;
      toggle.textContent = (panel.hidden ? "▸" : "▾") + " 搜索一直打不开？换用你自己登录的浏览器采集";
    });
  }
  if (box) {
    box.addEventListener("change", async () => {
      box.disabled = true;
      const ok = await sfSetBrowserChannel(box.checked);
      box.disabled = false;
      box.checked = ok ? sfOwnBrowserOn() : !box.checked;
      const now = $("#sf-browser-now");
      if (now) now.textContent = "当前：" + (sfOwnBrowserOn() ? "用你自己的 Chrome" : "默认（由 finintel 打开浏览器）");
    });
  }
}

/* ============================ 首页 ============================ */

/* 任务类型 → 用户可读标签（历史/首页最近记录按类型渲染，不再统一显示“整理了 N 个岗位”） */
const SF_TASK_TYPE_LABEL = {
  x_topics: "X 议题分析",
  xhs_career: "小红书岗位讨论",
  boss_research: "BOSS 岗位整理",
};
function sfIsSocialType(tt) { return tt === "x_topics" || tt === "xhs_career"; }
function sfTaskTypeLabel(tt) { return SF_TASK_TYPE_LABEL[tt] || "BOSS 岗位整理"; }

async function sfLoadHome() {
  const h = await api("/api/home");
  const hi = await api("/api/history?limit=10");     // /api/home 的 recent 只含 BOSS，这里并入社交来源
  const hist = (hi.ok && hi.data && hi.data.runs) || [];
  let data;
  if (h.ok && h.data) {
    data = h.data;
  } else {
    // 接口未就绪时的降级取数：只用既有已就绪接口，不伪造任何数据
    const st = await api("/api/auth/status");
    data = {
      boss: (st.ok && st.data && st.data.boss) || { state: "disconnected", user_message: "" },
      recent: [],
      suggestions: SF_SUGGESTIONS,
      scope: {},
    };
  }
  const seen = {};
  const merged = [];
  (data.recent || []).concat(hist).forEach(r => {
    const id = r.run_id || r.id;
    if (!id || seen[id]) return;
    seen[id] = true;
    merged.push(r);
  });
  merged.sort((a, b) => String(b.searched_at || b.started_at || "")
    .localeCompare(String(a.searched_at || a.started_at || "")));
  data.recent = merged.slice(0, 8);
  return data;
}

RENDER.home = async function (main) {
  const pageSeq = __routeSeq;
  sfClearPageTimers();
  let data = {};
  try { data = await sfLoadHome(); } catch (e) { data = {}; }
  await sfLoadBrowserChannel();
  if (pageSeq !== __routeSeq) return;
  if (data.boss) sfSetBoss(data.boss);
  const suggestions = (data.suggestions && data.suggestions.length) ? data.suggestions : SF_SUGGESTIONS;
  const scope = data.scope || {};
  const scopeText = scope.city ? ` · 城市范围：${scope.city}` : (scope.label ? ` · ${scope.label}` : "");
  const prefill = route.query.keyword || SF_DRAFT.query || "";
  const recent = data.recent || [];

  main.innerHTML = `
  <div class="sf-page">
    <section class="sf-hero">
      <h1 class="sf-hero-title">了解同行正在做什么</h1>
      ${data.demo_launch ? '<div class="sf-note" role="status">演示模式：BOSS 使用合成岗位；未配置模型时使用演示规则分析。X 与小红书仍需真实接入。</div>' : ''}
      <div class="sf-login-slot" id="sf-login"></div>
      <form class="sf-search" id="sf-search" autocomplete="off">
        <div class="sf-source-tabs" id="sf-source-tabs" role="tablist">
          <button type="button" class="sf-source-tab" data-source="zhipin">BOSS 岗位</button>
          <button type="button" class="sf-source-tab" data-source="x">X 海外热点</button>
          <button type="button" class="sf-source-tab" data-source="xiaohongshu">小红书岗位讨论</button>
        </div>
        <input class="sf-search-input" id="sf-q" type="text" value="${esc(prefill)}"
               placeholder="输入关键词，例如：消费信贷 产品经理" aria-label="搜索关键词">
        <button class="sf-btn-lg" type="submit" id="sf-go">搜索并整理</button>
      </form>
      <div class="sf-hero-meta">
        <span class="sf-note" id="sf-scope">默认关注产品与研发岗位${esc(scopeText)}</span>
        <button class="sf-link" type="button" id="sf-filter-toggle">筛选条件 ▾</button>
      </div>
      <div class="sf-filters" id="sf-filters" hidden>
        <label class="sf-field">城市
          <input type="text" id="sf-city" value="${esc(SF_DRAFT.city)}" placeholder="留空使用来源默认范围">
        </label>
        <label class="sf-field">岗位类型
          <select id="sf-jt">
            ${SF_JOB_TYPES.map(([v, l]) =>
              `<option value="${esc(v)}"${v === SF_DRAFT.job_type ? " selected" : ""}>${esc(l)}</option>`).join("")}
          </select>
        </label>
      </div>
      <div class="sf-suggest">试试：
        ${suggestions.slice(0, 4).map((s, i) =>
          `<button class="sf-chip" type="button" data-sug="${i}">${esc(s)}</button>`).join("")}
      </div>
      <div id="sf-browser"></div>
    </section>
    <section class="sf-recent">
      <h2 class="sf-h2">最近搜索</h2>
      <div id="sf-recent-list"></div>
    </section>
  </div>`;

  $("#sf-jt").value = SF_DRAFT.job_type || "product_engineering";
  const slot = $("#sf-browser");
  if (slot) { slot.innerHTML = sfBrowserChannelBlock(false); sfBindBrowserChannel(); }
  sfRenderHomeLogin(recent);

  $("#sf-search").addEventListener("submit", ev => { ev.preventDefault(); sfSubmitSearch(); });
  $("#sf-filter-toggle").addEventListener("click", () => {
    const f = $("#sf-filters");
    f.hidden = !f.hidden;
    $("#sf-filter-toggle").textContent = f.hidden ? "筛选条件 ▾" : "筛选条件 ▴";
  });
  $$(".sf-suggest .sf-chip", main).forEach(btn => {
    btn.addEventListener("click", () => {
      const s = suggestions[Number(btn.dataset.sug)];
      $("#sf-q").value = s;                       // 建议词只填入，不执行
      SF_DRAFT.query = s;
      $("#sf-q").focus();
    });
  });
  $("#sf-q").addEventListener("input", () => { SF_DRAFT.query = $("#sf-q").value; });
  $("#sf-city").addEventListener("change", () => { SF_DRAFT.city = $("#sf-city").value.trim(); });
  $("#sf-jt").addEventListener("change", () => { SF_DRAFT.job_type = $("#sf-jt").value; });
  // 信息源切换：更新占位符与高亮；切换来源不触发搜索
  const SOURCE_PLACEHOLDER = {
    zhipin: "输入关键词，例如：消费信贷 产品经理",
    x: "输入主题，例如：consumer lending（默认消费信贷/消费金融/金融科技）",
    xiaohongshu: "输入岗位/公司关键词，例如：消费金融 产品经理 面试",
  };
  const sfSyncSourceTabs = () => {
    const src = SF_DRAFT.source || "zhipin";
    $$(".sf-source-tab", main).forEach(t => {
      t.classList.toggle("active", t.dataset.source === src);
    });
    const q = $("#sf-q");
    if (q) q.placeholder = SOURCE_PLACEHOLDER[src] || SOURCE_PLACEHOLDER.zhipin;
    // BOSS 专属筛选仅 BOSS 源显示
    const filters = $("#sf-filters");
    if (filters) filters.hidden = src !== "zhipin";
    const meta = $("#sf-scope");
    if (meta) meta.textContent = src === "zhipin" ? "默认关注产品与研发岗位" :
      (src === "x" ? "默认观察近 7 天海外行业热点" : "按相关性检索岗位相关讨论");
  };
  $$(".sf-source-tab", main).forEach(t => {
    t.addEventListener("click", () => {
      SF_DRAFT.source = t.dataset.source;
      sfSyncSourceTabs();
    });
  });
  sfSyncSourceTabs();
};

function sfRenderHomeLogin(recent) {
  sfRenderLogin($("#sf-login"));
  sfRenderRecent(recent);
}

function sfRenderRecent(recent) {
  const host = $("#sf-recent-list");
  if (!host) return;
  if (!recent.length) {
    host.innerHTML = `<div class="sf-note">还没有搜索记录。输入关键词，点一次「搜索并整理」即可。</div>`;
    return;
  }
  host.innerHTML = recent.map(r => `
    <a class="sf-recent-row" href="#/result/${encodeURIComponent(r.run_id || r.id)}">
      <span class="sf-recent-q">${esc(r.query || r.query_text || r.name || "未命名搜索")}</span>
      <span class="sf-recent-meta">${esc(fmtTime(r.searched_at || r.started_at))}</span>
      <span class="sf-recent-meta">${esc(sfRecentMeta(r))}</span>
      <span class="sf-recent-go">查看结果 →</span>
    </a>`).join("");
}

/* 首页最近记录按 task_type 渲染：社交任务不再显示“已整理 N 个岗位” */
function sfRecentMeta(r) {
  const tt = r.task_type;
  if (sfIsSocialType(tt)) return sfTaskTypeLabel(tt) + " · " + sfStateLabel(r.state);
  return `已整理 ${Number(r.organized || 0)} 个岗位`;
}

const SF_STATE_LABEL = {
  completed: "已完成", partial: "部分完成", failed: "已失败",
  paused: "已暂停", cancelled: "已取消", queued: "排队中", running: "进行中",
};
function sfStateLabel(state) { return SF_STATE_LABEL[state] || "已保存"; }

async function sfSubmitSearch() {
  const q = ($("#sf-q") && $("#sf-q").value || "").trim();
  if (!q) { toast("请先输入关键词", "err"); $("#sf-q").focus(); return; }
  SF_DRAFT.query = q;
  SF_DRAFT.city = ($("#sf-city") && $("#sf-city").value.trim()) || "";
  SF_DRAFT.job_type = ($("#sf-jt") && $("#sf-jt").value) || "product_engineering";
  SF_DRAFT.source = SF_DRAFT.source || "zhipin";
  try { localStorage.setItem(SF_KEYWORD_KEY, q); } catch (e) { /* ignore */ }

  // 关键词是唯一必填项；BOSS 源下发 job_type，社交源（x/xiaohongshu）下发 source
  const body = { query: q, source: SF_DRAFT.source };
  if (SF_DRAFT.source === "zhipin") {
    body.job_type = SF_DRAFT.job_type;
    if (SF_DRAFT.city) body.city = SF_DRAFT.city;
    if (SF_DRAFT.reuse_run_id) { body.reuse_run_id = SF_DRAFT.reuse_run_id; SF_DRAFT.reuse_run_id = null; }
  }

  const btn = $("#sf-go");
  if (btn) { btn.disabled = true; btn.textContent = "正在提交…"; }
  const r = await api("/api/search", { method: "POST", body });
  if (btn) { btn.disabled = false; btn.textContent = "搜索并整理"; }

  if (!r.ok) {
    const code = r.error && r.error.code;
    if (body.source === "zhipin" && (code === "AUTH_REQUIRED" || code === "AUTH_EXPIRED")) {
      sfSetBoss({ state: code === "AUTH_EXPIRED" ? "auth_expired" : "disconnected" });
      sfRenderLoginAll();
      toast("已保留关键词，登录 BOSS 后会自动继续这一次搜索", "ok");
      sfStartLogin();
      return;
    }
    if (code === "INVALID_ANALYSIS" && body.job_type) {
      toast("岗位类型筛选暂不可用，请在「筛选条件」里改为「全部」后重试", "err");
      return;
    }
    toast(sfErrText(r, "暂时无法发起搜索，请稍后重试"), "err");
    return;
  }

  const task = (r.data && r.data.task) || {};
  // 登录态：建任务/同 query 复用回 data.boss；显式 reuse_run_id（「继续搜索」）目前只在 task.boss。
  // 两处同源同形状，取任一即可，三条路径因此表现一致（下方 toast 判断不会用到过期登录态）。
  const boss = (r.data && r.data.boss) || task.boss;
  if (boss) { sfSetBoss(boss); sfRenderLoginAll(); }
  if (!task.run_id) { toast("任务已提交，但未取得任务编号，请到历史中查看", "err"); return; }
  // 记住本次选定口径：复用 run 时后端不落库，结果接口需靠它显式带回
  if (SF_DRAFT.job_type) SF_SCOPE[task.run_id] = SF_DRAFT.job_type;

  if (task.stage === "pending_login") {
    SF_DRAFT.pending_run_id = task.run_id;        // 登录成功后只执行这一次
    toast("已保留关键词，登录 BOSS 后会自动继续", "ok");
  }
  location.hash = "#/result/" + task.run_id;
}

/* ============================ 结果页 ============================ */

RENDER.result = async function (main) {
  const runId = route.arg;
  SF_ACTIVE_RUN = runId || null;                 // 进入结果页：登记当前任务，作为过期响应判据
  if (!runId) {
    main.innerHTML = `<div class="sf-page">${sfEmpty("🔎", "缺少任务编号", "请从最近搜索或历史记录进入。",
      `<a class="btn primary" href="#/">回到搜索首页</a>`)}</div>`;
    return;
  }
  main.innerHTML = `
  <div class="sf-page">
    <div class="sf-result-head" id="sf-rhead"></div>
    <div id="sf-progress"></div>
    <div id="sf-body"></div>
  </div>`;
  await sfRunTask(runId);
};

async function sfRunTask(runId) {
  sfClearPageTimers();
  SF_PAGE_BUSY = false;
  const done = await sfTickTask(runId);
  if (done) return;
  sfPageInterval(2000, async () => {
    if (document.hidden || SF_PAGE_BUSY) return;   // 页面隐藏时暂停
    SF_PAGE_BUSY = true;
    try { if (await sfTickTask(runId)) sfClearPageTimers(); }
    finally { SF_PAGE_BUSY = false; }
  });
}

async function sfTickTask(runId) {
  const r = await api(`/api/tasks/${encodeURIComponent(runId)}`);
  // 过期响应：任务已切换（或已离开结果页）时，绝不写旧任务的页面
  if (SF_ACTIVE_RUN !== runId) return true;
  if (!r.ok) {
    const head = $("#sf-rhead");
    if (head) head.innerHTML = sfBackLink();
    const host = $("#sf-progress");
    if (host) host.innerHTML = sfAlert("warn", "这次搜索暂时打不开", sfErrText(r, "任务不存在或服务尚未就绪"),
      `<a class="btn" href="#/history">查看历史</a><a class="btn primary" href="#/">重新搜索</a>`);
    return true;
  }
  const t = (r.data && r.data.task) || {};
  // 社交任务（X / 小红书）走独立渲染，不进 BOSS 岗位渲染
  if (t.task_type === "x_topics" || t.task_type === "xhs_career") {
    await window.SocialView.renderTask(runId, t);      // await 渲染完成再判终态
    if (SF_ACTIVE_RUN !== runId) return true;
    return ["completed", "failed", "partial", "paused", "cancelled"].includes(t.stage);
  }
  if (t.boss) { sfSetBoss(t.boss); sfRenderLoginAll(); }   // 轮询期间同步登录态（如登录失效）
  if (SF_ACTIVE_RUN !== runId) return true;
  sfRenderTaskHead(runId, t);
  const stage = t.stage || "";
  if (SF_TASK_TERMINAL.indexOf(stage) >= 0) { sfRenderTaskFinal(runId, t); return true; }
  sfRenderProgress(t);
  return false;
}

function sfBackLink() {
  return `<div class="sf-result-top"><a class="sf-back" href="#/history">← 返回</a>
    <span class="sf-note">整理中</span></div>`;
}

function sfRenderTaskHead(runId, t) {
  const head = $("#sf-rhead");
  if (!head) return;
  SF_CURRENT = { run_id: runId, query: t.query || SF_CURRENT.query };
  const bits = [];
  if (t.query) bits.push(`<b>${esc(t.query)}</b>`);
  if (t.city) bits.push(esc(t.city));
  if (t.searched_at) bits.push(esc(fmtTime(t.searched_at)));
  head.innerHTML = `<div class="sf-result-top"><a class="sf-back" href="#/history">← 返回</a>
    <div class="sf-result-title">${bits.join(" · ") || "搜索整理结果"}</div></div>`;
}

function sfRenderProgress(t) {
  const host = $("#sf-progress");
  if (!host) return;
  const stage = t.stage || "creating";
  if (stage === "pending_login" || (t.user_stage && t.user_stage === "disconnected")) {
    const connected = SF_BOSS.state === "connected";
    host.innerHTML = `<div class="sf-card sf-card-pad">
      <div class="sf-progress-text">${esc(t.user_message || "登录 BOSS 后即可继续这次搜索")}</div>
      <div class="sf-login-slot" id="sf-login"></div>
      <div class="sf-note">关键词已保留，登录成功后会自动继续这一次搜索。</div>
      ${connected ? `<div class="btn-row mt8"><button class="btn" onclick="sfResumeTask('${sfId(t.run_id)}')">继续这次搜索</button></div>` : ""}
    </div>`;
    $("#sf-body").innerHTML = "";
    sfRenderLogin($("#sf-login"), { always: true });
    return;
  }
  const counters = t.counters || {};
  let text = t.user_message || SF_STAGE_TEXT[stage] || "正在整理";
  if (stage === "fetching_details") {
    const read = counters.detail_read != null ? counters.detail_read : (t.collected || counters.collected || 0);
    text = `正在读取岗位详情，已读取 ${Number(read)} 个`;
  }
  if (stage === "analyzing" || stage === "organizing") {
    text = `正在整理公司方向，已完成 ${Number(t.organized || counters.organized || 0)} 个`;
  }
  const idx = Math.max(0, SF_STAGES.indexOf(stage));
  const pct = Math.round((idx / (SF_STAGES.length - 1)) * 100);
  const can = t.can || {};
  host.innerHTML = `<div class="sf-card sf-card-pad sf-progress">
    <div class="sf-progress-text">${esc(text)}</div>
    <div class="sf-progress-bar"><i style="width:${pct}%"></i></div>
    <div class="sf-progress-foot">
      <span class="sf-note">取得 ${Number(t.collected || counters.collected || 0)} 个岗位 · 已整理 ${Number(t.organized || counters.organized || 0)} 个</span>
      ${(can.cancel !== false) ? `<button class="btn sm" onclick="sfCancelTask('${sfId(t.run_id)}')">取消</button>` : ""}
    </div>
    ${sfStageStrip(t.stages)}
  </div>`;
  $("#sf-body").innerHTML = "";
}

/* 阶段条按后端给的 status 渲染：未执行的阶段如实标「未执行」，不再全部显示已完成 */
function sfStageStrip(stages) {
  if (!stages || !stages.length) return "";
  const mark = { done: "✓", current: "●", failed: "!", skipped: "–", pending: "·" };
  const text = { done: "已完成", current: "进行中", failed: "未完成", skipped: "未执行", pending: "待执行" };
  const dim = { skipped: 1, pending: 1 };
  return `<div class="sf-stages" style="display:flex;flex-wrap:wrap;gap:10px;margin-top:8px">` +
    stages.map(s => `<span class="sf-note" style="opacity:${dim[s.status] ? 0.55 : 1}">` +
      `${mark[s.status] || "·"} ${esc(s.label)}·${esc(text[s.status] || "")}</span>`).join("") +
    `</div>`;
}

async function sfRenderTaskFinal(runId, t) {
  const stage = t.stage || "";
  const host = $("#sf-progress");
  const counters = t.counters || {};
  const collected = Number(t.collected || counters.collected || 0);
  const organized = Number(t.organized || counters.organized || 0);
  const failed = Number(t.failed || counters.failed || 0);

  if (stage === "paused" || stage === "failed" || stage === "partial" || stage === "cancelled") {
    const kind = stage === "cancelled" ? "warn" : (stage === "partial" ? "warn" : "err");
    const title = stage === "cancelled" ? "已停止，保留已获取内容"
      : stage === "partial" ? `已整理 ${organized} 个，${failed} 个未成功`
        : (t.user_message || "本次搜索未完成");
    SF_CURRENT = { run_id: runId, query: t.query || SF_CURRENT.query };
    const expired = t.user_stage === "auth_expired" || SF_BOSS.state === "auth_expired";
    const fb = expired ? ["login_and_resume", "retry_organize", "view_result"]
      : (stage === "cancelled" ? ["retry_organize", "view_result"]
        : ["resume", "retry_organize", "view_result"]);
    let acts = sfActionButtons(t.actions, fb);
    if (!acts) acts = sfBtn("", `sfShowResult('${sfId(runId)}')`, "查看已完成部分");
    const detail = t.user_message && t.user_message !== title ? t.user_message : "";
    host.innerHTML = sfAlert(kind, title, detail, acts) + sfStageStrip(t.stages);
    // 采集环境类失败：就地给出「换用自己已登录的浏览器」的通道，省得用户回首页找
    if (t.user_stage === "env_not_ready") {
      await sfLoadBrowserChannel();
      host.insertAdjacentHTML("afterend", `<div id="sf-browser">${sfBrowserChannelBlock(true)}</div>`);
      sfBindBrowserChannel();
    }
  } else {
    host.innerHTML = "";
  }
  await sfShowResult(runId, { collected, organized, failed });
}

/* 「查看全部」：按 all 口径**重新拉结果视图**（不是只改本地状态），并记住该选择供后续刷新沿用 */
async function sfShowAllDirections(runId) {
  SF_SCOPE[runId] = "all";
  await sfShowResult(runId);
}

async function sfShowResult(runId, fallback) {
  const body = $("#sf-body");
  if (!body) return;
  body.innerHTML = `<div class="sf-note">正在加载整理结果…</div>`;
  // 「继续搜索」后 run 的存量口径可能落后于用户当前选择，故显式带回本次选定口径
  const jt = SF_SCOPE[runId];
  const r = await api(`/api/tasks/${encodeURIComponent(runId)}/result`
    + (jt ? `?job_type=${encodeURIComponent(jt)}` : ""));
  if (!r.ok) {
    body.innerHTML = sfAlert("warn", "结果暂时不可用", sfErrText(r, "可以稍后刷新，或查看岗位明细"),
      `<a class="btn" href="#/history">查看历史</a>`);
    return;
  }
  const d = r.data || {};
  sfRenderResultBody(runId, d, fallback);
}

function sfRenderResultBody(runId, d, fallback) {
  const body = $("#sf-body");
  if (!body) return;
  const s = d.summary || {};
  const collected = Number(s.collected != null ? s.collected : (fallback && fallback.collected) || 0);
  const organized = Number(s.organized != null ? s.organized : (fallback && fallback.organized) || 0);
  const failed = Number(s.failed != null ? s.failed : (fallback && fallback.failed) || 0);
  const dirs = d.directions || [];
  const groups = sfGroupDirections(dirs);
  const pendingIdentity = groups.filter(g => g.identity_status && g.identity_status !== "verified").length;
  /* 被岗位类型口径过滤掉的方向数：>0 时必须如实告知，否则用户会以为结果少了/丢了 */
  const hidden = Number((s.scope && s.scope.hidden_by_job_type) || 0);

  /* 底部动作会与空态动作撞车：stale 时「继续」是「刷新定位后继续读取详情」，
     底部再放一个「继续搜索」（其实是回首页重搜）会让用户以为是同一件事。
     stale 空态已经给出「继续」/「重新搜索」+「查看已完成部分」，这里就不再重复渲染。 */
  const hideContinueSearch = !groups.length && ["stale", "risk"].includes(d.failure_kind);

  /* 结果来自上游本地缓存时**如实标注**（含多久），并给「获取最新结果」出口——
     绝不沉默地把旧数据当实时结果端上。文案由后端 user_view.cache_served_message 提供。 */
  const cacheNote = (d.cache && d.cache.hit && d.cache.text)
    ? `<div class="sf-infobar" id="sf-cache-note">${esc(d.cache.text)}</div>` : "";
  const canRefresh = !!(d.can && d.can.refresh) && d.failure_kind !== "risk";

  body.innerHTML = `
    <div class="sf-metrics">
      <div class="sf-metric"><div class="sf-metric-num">${collected}</div><div class="sf-metric-lbl">取得岗位</div></div>
      <div class="sf-metric"><div class="sf-metric-num ok">${organized}</div><div class="sf-metric-lbl">成功整理</div></div>
      <div class="sf-metric"><div class="sf-metric-num ${failed ? "warn" : ""}">${failed}</div><div class="sf-metric-lbl">未成功</div></div>
    </div>
    ${cacheNote}

    <h2 class="sf-h2 mt24">同行在做什么</h2>
    <div class="sf-note">已分析 ${Number(s.analysis_succeeded || 0)} · 排除 ${Number(s.excluded || 0)} · 信息不足 ${Number(s.insufficient_information || 0)} · 分析失败 ${Number(s.analysis_failed || 0)}</div>
    ${s.details_partial ? `<div class="sf-infobar">${Number(s.details_partial)} 个岗位的正文完整性未确认，结论仅基于已取得片段。招聘信息不代表项目已上线。</div>` : ""}
    ${hidden > 0 ? `<div class="sf-infobar" id="sf-scope-note">已按岗位类型隐藏 ${hidden} 条方向
      <button class="sf-link" type="button" onclick="sfShowAllDirections('${sfId(runId)}')">查看全部</button></div>` : ""}
    ${groups.length ? groups.map(g => sfDirectionCard(runId, g)).join("")
      : sfEmptyByKind(runId, d.failure_kind, d)}
    ${pendingIdentity ? `<div class="sf-infobar">${pendingIdentity} 家公司的归属待核对，未计入上方统计</div>` : ""}

    <div class="sf-actions">
      <button class="btn" onclick="sfOpenJobs('${sfId(runId)}')">岗位明细</button>
      <button class="btn" onclick="sfExportReport('${sfId(runId)}')">导出报告</button>
      ${canRefresh ? `<button class="btn" onclick="sfRefreshTask('${sfId(runId)}')">获取最新结果</button>` : ""}
      ${hideContinueSearch ? "" : `<button class="btn" onclick="sfContinueSearch('${sfId(runId)}')">继续搜索</button>`}
    </div>`;
}

/* 空态一律按后端 failure_kind 分流：
   channel / auth / risk 只给「连接恢复」动作，**绝不出现「换个关键词再搜」**；
   只有 empty（真实成功返回的空列表）才允许提示换关键词。 */
function sfEmptyByKind(runId, kind, d) {
  const reason = (d && d.empty_reason) ? String(d.empty_reason)
    : "没有找到可直接引用原文的岗位内容，未用行业常识填充。";
  const canResume = SF_CURRENT.run_id
    ? sfBtn("primary", `sfResumeTask('${sfId(runId)}')`, "连接恢复后继续") : "";
  if (kind === "channel") {
    return sfEmpty("🔌", SF_CHANNEL_DOWN_TEXT,
      "关键词与已取得的内容都保留着；详细原因与连接步骤见设置页。",
      `<a class="btn" href="#/diag/settings">查看连接步骤</a>${canResume}`);
  }
  if (kind === "auth") {
    return sfEmpty("🔑", "BOSS 登录已失效，已保留当前进度",
      "重新登录后会自动继续这次搜索。",
      sfBtn("primary", "sfStartLogin()", "重新登录并继续"));
  }
  /* 详情阶段认证失败：详情与搜索用的是**同一个浏览器会话**（同一份冻结通道），
     不再走「独立的 HTTP 通道 + 本地保存凭证」。所以这里如实说的是「这条会话在读取
     详情时登录态不可用」，而不是「本地凭证失效」——旧的独立通道解释已被证伪。
     「重新登录」可以给，但必须说明详情定位信息可能已过期、不保证恢复，
     不给用户虚假承诺，也不让用户把它当成万能解药。 */
  if (kind === "detail_auth") {
    const saved = Number((d && d.summary && d.summary.collected) || 0);
    const lead = saved > 0 ? `已保存 ${saved} 个岗位。` : "";
    return sfEmpty("📄", "已搜索到岗位，但读取详情失败",
      lead + "详情与搜索用的是同一个浏览器会话，但读取详情时该会话的登录态已不可用；"
        + "重新登录后可以重试，但详情定位信息可能已过期，不保证恢复。",
      sfBtn("primary", "sfStartLogin()", "重新登录并重试")
        + sfBtn("", `sfOpenJobs('${sfId(runId)}')`, "查看已取得的岗位"));
  }
  if (kind === "risk") {
    if (d && d.environment_blocked) {
      const saved = Number((d.summary && d.summary.collected) || 0);
      const progress = saved > 0 ? `已保存 ${saved} 个岗位；未取得的正文不会生成结论。` : "本次尚未取得岗位。";
      return sfEmpty("🛡", "登录已成功，但 BOSS 拦截了当前访问环境",
        progress + "请先在专用浏览器检查官方页面。重开浏览器不保证解除平台限制。",
        sfBtn("primary", "sfOpenOfficialPage()", "在专用浏览器中打开 BOSS 官方页面") + sfBtn("", "sfRepairBrowser()", "修复并重开专用浏览器"));
    }
    return sfEmpty("🛡", "BOSS 需要你完成验证", "在 BOSS 官方页面完成验证后回到本页继续。",
      sfBtn("primary", "sfStartLogin()", "打开官方验证页面") + canResume);
  }
  if (kind === "gated") {
    /* 本地频率闸门：如实说清在等什么、还要等多久，只给人工「获取最新结果」（自动继续已被闸门挡住） */
    return sfEmpty("⏳", "本地频率保护已暂停本次采集", reason,
      sfBtn("", `sfRefreshTask('${sfId(runId)}')`, "获取最新结果"));
  }
  if (kind === "stale") {
    /* 定位符过期：能不能继续只看「刷新额度」（后端 can_resume 就是它）。
       额度还在 → 给「继续」，点了真的会刷新定位后继续；
       额度用尽 → 只给「重新搜索」+「查看已完成部分」。
       两种情况都**绝不**说成「换个关键词」——关键词没问题，是定位信息过期了 */
    if (d && d.can_resume) {
      return sfEmpty("⌛", SF_STALE_LOCATOR_RESUMABLE_TEXT,
        "关键词与已取得的内容都保留着；继续后会自动刷新定位并接着读取详情。",
        sfBtn("primary", `sfResumeTask('${sfId(runId)}')`, "继续")
        + sfBtn("", `sfShowResult('${sfId(runId)}')`, "查看已完成部分"));
    }
    return sfEmpty("⌛", SF_STALE_LOCATOR_TEXT,
      "关键词与已取得的内容都保留着；重新搜索会重新抓取一次这批岗位。",
      `<a class="btn primary" href="#/">重新搜索</a>`
      + sfBtn("", `sfShowResult('${sfId(runId)}')`, "查看已完成部分"));
  }
  return sfEmpty("—", "本次未发现足以判断的业务方向", reason,
    `<a class="btn" href="#/">换个关键词再搜</a>`);
}

function sfGroupName(d) { return d.display_name || d.company || "归属待核实公司"; }

function sfGroupDirections(dirs) {
  const map = new Map();
  dirs.forEach(d => {
    const key = d.company || sfGroupName(d);
    if (!map.has(key)) {
      map.set(key, { key, name: sfGroupName(d), identity_status: d.identity_status, items: [], job_count: 0, evidence_count: 0 });
    }
    const g = map.get(key);
    g.items.push(d);
    g.job_count = Math.max(g.job_count, Number(d.job_count || 0));
    g.evidence_count += Number(d.evidence_count || 0);
    if (!g.identity_status) g.identity_status = d.identity_status;
  });
  return Array.from(map.values());
}

function sfDirectionCard(runId, g) {
  const shown = g.items.slice(0, 3);
  const more = g.items.length - shown.length;
  const provisional = g.identity_status && g.identity_status !== "verified";
  return `<article class="sf-dir-card">
    <div class="sf-dir-head">
      <div class="sf-dir-co">${esc(g.name)}
        ${provisional ? `<span class="badge b-orange">归属待核实</span>` : ""}</div>
      <div class="sf-note">${g.job_count} 个岗位 · ${g.evidence_count} 条证据</div>
    </div>
    <div class="sf-dir-body">
      ${shown.map(d => sfDirectionRow(runId, g.key, d)).join("")}
      ${more > 0 ? `<div class="sf-note">另有 ${more} 条方向，可在「岗位明细」中查看</div>` : ""}
      <div class="sf-caveat">招聘反映的工作需求，不代表已上线</div>
      <button class="sf-link" type="button" data-run="${esc(runId)}" data-company="${esc(g.key)}"
              onclick="sfToggleEvidence(this)">▸ 展开岗位原文与来源</button>
      <div class="sf-evidence" hidden></div>
    </div>
  </article>`;
}

function sfDirectionRow(runId, key, d) {
  /* 复核状态文案只认后端 LABELS（boot 已从 /api/health 载入，unreviewed → 待核对），前端不再自带一份中文 */
  const rev = badge(REV_BADGE, d.review_status, LABELS.review_status);
  return `<div class="sf-direction">
    <div class="sf-direction-tags">
      ${d.business_domain ? domainBadge(d.business_domain) : ""}
      ${d.action_type ? `<span class="badge b-gray">${esc(lbl("act", d.action_type))}</span>` : ""}
      ${d.support_level ? badge(SUP_BADGE, d.support_level, LABELS.support_level) : ""}
      ${rev}
      ${d.relevance_label ? `<span class="badge b-gray">${esc(d.relevance_label)}</span>` : ""}
      ${d.partial_body ? '<span class="badge b-orange">正文完整性待确认</span>' : ""}
    </div>
    <div class="sf-direction-text">${esc(d.statement || "—")}</div>
    <div class="sf-note">涉及岗位 ${Number(d.job_count || 0)} · 证据 ${Number(d.evidence_count || 0)}${d.latest_fetched_at ? " · " + esc(fmtTime(d.latest_fetched_at)) : ""}</div>
  </div>`;
}

function sfToggleEvidence(btn) {
  const box = btn.nextElementSibling;
  if (!box) return;
  if (!box.hidden) {
    box.hidden = true;
    btn.textContent = "▸ 展开岗位原文与来源";
    return;
  }
  box.hidden = false;
  btn.textContent = "▾ 收起岗位原文与来源";
  if (!box.dataset.loaded) {
    box.dataset.loaded = "1";
    sfLoadCompanyJobs(btn.dataset.run, btn.dataset.company, box);
  }
}

async function sfRunJobs(runId) {
  if (SF_JOB_CACHE[runId]) return SF_JOB_CACHE[runId];
  let list = null;
  const r = await api(`/api/tasks/${encodeURIComponent(runId)}/jobs?page=1`);
  if (r.ok && r.data) list = r.data.jobs || r.data.items || null;
  if (!list) {                                   // 契约接口未就绪时用既有岗位接口兜底
    const r2 = await api(`/api/jobs?run_id=${encodeURIComponent(runId)}`);
    list = (r2.ok && r2.data && r2.data.jobs) || [];
  }
  SF_JOB_CACHE[runId] = list;
  return list;
}

async function sfLoadCompanyJobs(runId, companyKey, box) {
  box.innerHTML = `<div class="sf-note">正在读取岗位原文…</div>`;
  let jobs = [];
  try { jobs = await sfRunJobs(runId); } catch (e) { jobs = []; }
  const mine = jobs.filter(j => j.company_id === companyKey || j.display_name === companyKey
    || j.company_name_raw === companyKey);
  const pick = mine.slice(0, 3);
  if (!pick.length) {
    box.innerHTML = `<div class="sf-note">本次没有可展开的岗位原文，可在「岗位明细」中查看。</div>`;
    return;
  }
  box.innerHTML = pick.map(j => `
    <div class="sf-evjob">
      <div class="sf-evjob-head">
        <b>${esc(sfCandidate(j))}</b>
        <span class="sf-note">${esc(j.city || "城市未知")} · ${esc(fmtTime(j.last_seen_at || j.fetched_at))}</span>
        <button class="sf-link" type="button" data-job="${esc(j.id)}" data-snapshot="${esc(j.run_snapshot_id || '')}" onclick="sfLoadJobQuote(this)">看原文</button>
        ${['complete','partial'].includes(j.detail_status) ? `<button class="btn sm" type="button" onclick="interviewStart('${esc(j.id)}','${esc(runId)}',this)">基于该岗位准备面试</button>` : ''}
      </div>
      <div class="sf-evjob-quote" hidden></div>
    </div>`).join("");
}

async function sfLoadJobQuote(btn) {
  const jobId = btn.dataset.job;
  const wrap = btn.closest(".sf-evjob");
  if (!wrap || !jobId) return;
  const box = wrap.querySelector(".sf-evjob-quote");
  if (!box) return;
  if (!box.hidden) { box.hidden = true; btn.textContent = "看原文"; return; }
  box.hidden = false;
  btn.textContent = "收起原文";
  if (box.dataset.loaded) return;
  box.dataset.loaded = "1";
  box.innerHTML = `<div class="sf-note">正在读取原文…</div>`;
  const r = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
  if (!r.ok) { box.innerHTML = `<div class="sf-note">原文暂不可用</div>`; return; }
  const job = r.data.job || {};
  const snap = (btn.dataset.snapshot ? (r.data.snapshots || []).find(s => s.id === btn.dataset.snapshot) : (r.data.snapshots || [])[0]) || {};
  const text = (snap.evidence_text || snap.responsibilities_text || "").trim();
  const srcNote = `来源：${job.source === "demo" ? "演示数据" : (job.company_name_raw || "招聘平台")}` +
    ` · 抓取时间 ${esc(fmtTime(snap.fetched_at))}`;
  box.innerHTML = `<div class="sf-note">${srcNote}</div>
    ${text ? `<div class="evidence-box"><span class="ev-src"></span></div>` : `<div class="sf-note">本次未保留岗位原文。</div>`}`;
  if (text) {
    const evBox = box.querySelector(".evidence-box");
    evBox.innerHTML = renderEvidence(text);
    const src = evBox.querySelector(".ev-src");
    if (src) src.dataset.text = src.textContent;
  }
}

/* ---------- 任务操作 ---------- */

async function sfCancelTask(runId) {
  const r = await api(`/api/tasks/${encodeURIComponent(runId)}/cancel`, { method: "POST", body: { reason: "用户取消" } });
  if (!r.ok) { toast(sfErrText(r, "当前无法取消"), "err"); return; }
  await sfTickTask(runId);
}

async function sfResumeTask(runId) {
  const r = await api(`/api/tasks/${encodeURIComponent(runId)}/resume`, { method: "POST", body: {} });
  if (!r.ok) { toast(sfErrText(r, "暂时无法继续这次搜索"), "err"); return; }
  toast("已继续这次搜索", "ok");
  await sfRunTask(runId);
}

async function sfRetryOrganize(runId) {
  const r = await api(`/api/tasks/${encodeURIComponent(runId)}/retry-organize`, { method: "POST", body: {} });
  if (!r.ok) { toast(sfErrText(r, "暂时无法重试整理"), "err"); return; }
  toast("正在重试整理已取得的岗位", "ok");
  await sfRunTask(runId);
}

/* 「获取最新结果」：唯一会跳过上游本地缓存、真正联网重取第 1 页的动作。
   默认路径一律允许命中缓存（少打扰平台），只有用户明确点这里才带 --no-cache。 */
async function sfRefreshTask(runId) {
  const r = await api(`/api/tasks/${encodeURIComponent(runId)}/refresh`, { method: "POST", body: {} });
  if (!r.ok) { toast(sfErrText(r, "暂时无法获取最新结果"), "err"); return; }
  toast("正在获取最新结果", "ok");
  await sfRunTask(runId);
}

async function sfExportReport(runId) {
  const r = await api(`/api/tasks/${encodeURIComponent(runId)}/report`, { method: "POST", body: { formats: ["md"] } });
  if (!r.ok) { toast(sfErrText(r, "导出失败，请稍后重试"), "err"); return; }
  const id = (r.data && (r.data.report_id || (r.data.report && r.data.report.id)));
  if (!id) { toast("报告已生成，可在诊断页的报告中心查看", "ok"); return; }
  window.open(`/api/reports/${encodeURIComponent(id)}/file?fmt=md`, "_blank");
}

async function sfOpenJobs(runId) {
  openDrawer(`<div class="spread"><h2>岗位明细</h2><span class="close" onclick="closeDrawer()">×</span></div>
    <div id="sf-job-list"><div class="sf-note">正在读取岗位…</div></div>`);
  let jobs = [];
  try { jobs = await sfRunJobs(runId); } catch (e) { jobs = []; }
  const host = $("#sf-job-list");
  if (!host) return;
  if (!jobs.length) { host.innerHTML = `<div class="sf-note">本次没有已取得的岗位明细。</div>`; return; }
  host.innerHTML = `<table class="tbl">
    <tr><th>岗位</th><th>公司</th><th>城市</th><th>详情</th><th>面试准备</th></tr>
    ${jobs.map(j => `<tr><td>${esc(j.title || "—")}</td><td>${esc(j.display_name || j.company_name_raw || "—")}</td>
      <td>${esc(j.city || "—")}</td><td>${esc(lbl("det", j.detail_status))}</td><td>${['complete','partial'].includes(j.detail_status) ? `<button class="btn sm" onclick="interviewStart('${esc(j.id)}','${esc(runId)}',this)">准备此岗位</button>` : '<span class="muted">等待正文</span>'}</td></tr>`).join("")}
  </table>`;
}

function sfContinueSearch(runId) {
  SF_DRAFT.reuse_run_id = runId;
  SF_DRAFT.query = SF_CURRENT.query || SF_DRAFT.query;
  const kw = SF_DRAFT.query || route.query.keyword || "";
  location.hash = "#/" + (kw ? "?keyword=" + encodeURIComponent(kw) : "");
  toast("已回到首页：保留原结果，改动关键词后可继续搜索", "ok");
}

/* ============================ 历史 ============================ */

RENDER.history = async function (main) {
  sfClearPageTimers();
  const q = (route.query.q || "").trim();
  const r = await api("/api/history?limit=50");
  const runs = (r.ok && r.data && r.data.runs) || [];
  const list = q ? runs.filter(x => String(x.query || "").indexOf(q) >= 0) : runs;
  main.innerHTML = `<div class="sf-page">
    <div class="sf-page-head">
      <h1 class="sf-h1">搜索历史</h1>
      <form class="sf-mini-search" id="sf-hsearch">
        <input type="text" id="sf-hq" value="${esc(q)}" placeholder="按关键词筛选">
      </form>
    </div>
    <div id="sf-hist">${sfHistoryRows(list, !!r.ok)}</div>
    ${list.length ? `<div class="btn-row mt16"><a class="btn primary" href="#/">＋ 新的搜索</a></div>` : ""}
  </div>`;
  const form = $("#sf-hsearch");
  if (form) form.addEventListener("submit", ev => {
    ev.preventDefault();
    const v = $("#sf-hq").value.trim();
    location.hash = v ? "#/history?q=" + encodeURIComponent(v) : "#/history";
  });
};

function sfHistoryRows(runs, apiOk) {
  if (!runs.length) {
    return apiOk
      ? sfEmpty("🕘", "没有匹配的搜索记录", "换个关键词筛选，或直接发起新的搜索。",
        `<a class="btn primary" href="#/">去搜索</a>`)
      : sfAlert("warn", "历史记录暂时不可用", "可以先发起新的搜索，稍后再回来看历史。",
        `<a class="btn primary" href="#/">去搜索</a>`);
  }
  return runs.map(r => `
    <a class="sf-hist-row" href="#/result/${encodeURIComponent(r.run_id || r.id)}">
      <div class="sf-hist-main">
        <div class="sf-hist-q">${esc(r.query || r.query_text || r.name || "未命名搜索")}</div>
        <div class="sf-note">${esc(sfTaskTypeLabel(r.task_type))} · ${esc(fmtTime(r.searched_at || r.started_at))}</div>
      </div>
      <div class="sf-hist-nums">
        ${sfIsSocialType(r.task_type)
          ? `<span class="sf-note">${esc(sfStateLabel(r.state))}</span>`
          : `<span class="sf-note">取得 ${Number(r.collected || 0)}</span><span class="sf-note">整理 ${Number(r.organized || 0)}</span>`}
      </div>
      <span class="sf-recent-go">查看结果 →</span>
    </a>`).join("");
}

/* ============================ 启动 ============================ */

/* 离开当前页（hash 变化）时清理页面级轮询；登录轮询按终态自行停止 */
window.addEventListener("hashchange", sfClearPageTimers);

renderTopbar();
sfRefreshBoss();
