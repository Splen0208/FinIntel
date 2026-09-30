/* 「环境被拦截」动作的执行级契约：在**本项目的专用浏览器**里打开官方页面，
 * **绝不**触发重新登录，也**绝不**在「日常浏览器」里开新窗口。
 *
 * 用法： ``node backend/tests/frontend_risk_action_contract.mjs <path-to-simple.js>``
 * 输出： 一行 JSON 到 stdout：``{"ok": bool, "failed": [ ... ], "detail": { ... }}``
 * 退出码：全部通过 0，否则 1。
 *
 * 真机缺陷（H2）：`kind==='risk' && environment_blocked` 的空态按钮文案写「查看 BOSS 官方
 * 页面」，onclick 却是 `sfStartLogin()` → 点一下会 POST ``/api/auth/login``（重新登录），
 * 与文案自相矛盾，也违背上游「不刷新 Token、不重新登录、不自动重试」的要求。
 *
 * 二轮修正（#2）：纯前端 ``window.open`` 开在日常浏览器里毫无作用——被标记的是**专用
 * profile 那个会话**，必须在那台浏览器里打开官方页面。正确做法是 POST 后端专用端点
 * ``/api/channel/open-official``（后端只发 CDP 浏览器级命令）。这里断言该动作命中**专用
 * 端点**、**零业务请求**（不是 login/search/detail）、且**不**在日常浏览器里开窗口。
 *
 * 这里把**真实发布**的 ``frontend/simple.js`` 装进 Node 的 ``vm`` 最小沙箱，直接调用真正的
 * ``SF_ACTION_BTN`` / ``sfEmptyByKind()`` / ``sfOpenOfficialPage()``，并记录 ``api`` / ``window.open``
 * 调用，证明该动作**不碰** ``/api/auth/login``，只经专用端点让专用浏览器打开官方页面。
 */
import fs from "node:fs";
import vm from "node:vm";

const simplePath = process.argv[2];
if (!simplePath) {
  console.error("usage: node frontend_risk_action_contract.mjs <path-to-simple.js>");
  process.exit(2);
}
const src = fs.readFileSync(simplePath, "utf8");

const noop = () => {};
const stubEl = () => ({
  innerHTML: "", textContent: "", value: "", hidden: false, style: {},
  addEventListener: noop, appendChild: noop, setAttribute: noop,
  getAttribute: () => null, classList: { add: noop, remove: noop },
  querySelector: () => null, querySelectorAll: () => [],
});

const API_CALLS = [];
const OPENED = [];

const sandbox = {
  console, __API_CALLS: API_CALLS, __OPENED: OPENED, __RESULT: null, __DONE: null,
  window: { addEventListener: noop, location: { hash: "" }, open: (...a) => { OPENED.push(a); } },
  document: {
    addEventListener: noop, getElementById: () => null,
    querySelector: () => null, querySelectorAll: () => [],
    createElement: stubEl, body: stubEl(), head: stubEl(),
  },
  location: { hash: "", pathname: "/" },
  RENDER: {}, route: { query: {} },
  localStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, data: {} }) }),
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval: noop,
  requestAnimationFrame: noop, alert: noop, confirm: () => true,
  api: () => Promise.resolve({ ok: true, data: {} }),
  toast: noop, esc: (s) => String(s == null ? "" : s), fmtTime: (s) => String(s || ""),
  $: () => null,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

const epilogue = `
  globalThis.__DONE = (async () => {
    const out = {};
    api = (path) => { __API_CALLS.push(path); return Promise.resolve({ ok: true, data: {} }); };

    out.check_official_html = SF_ACTION_BTN.check_official("在本项目的专用浏览器中打开 BOSS 官方页面");
    out.open_verify_html = SF_ACTION_BTN.open_verify("打开官方验证页面");
    out.risk_env_empty_html = sfEmptyByKind("run_env", "risk",
      { environment_blocked: true, empty_reason: "" });
    out.risk_verify_empty_html = sfEmptyByKind("run_v", "risk",
      { environment_blocked: false, empty_reason: "" });
    out.risk_saved_html = sfEmptyByKind("run_saved", "risk",
      { environment_blocked: true, summary: { collected: 30 } });
    out.official_endpoint = (typeof SF_OFFICIAL_ENDPOINT === "string") ? SF_OFFICIAL_ENDPOINT : null;

    __API_CALLS.length = 0;
    await sfOpenOfficialPage();
    out.api_calls_after_open = __API_CALLS.slice();
    out.opened = __OPENED.slice();
    __RESULT = out;
  })();
`;

vm.runInContext(`${src}\n;\n${epilogue}`, sandbox, { filename: "simple.js" });

await sandbox.__DONE;
const out = sandbox.__RESULT || {};
const s = (v) => String(v == null ? "" : v);
const calls = out.api_calls_after_open || [];
const isBusiness = (p) => /\/api\/auth\/login|\/search|\/detail/.test(s(p));

const checks = [
  ["详情被拦截时如实保留已取得岗位数量",
    s(out.risk_saved_html).includes("已保存 30 个岗位") && !s(out.risk_saved_html).includes("本次尚未取得岗位")],
  ["check_official 动作映射到 sfOpenOfficialPage()",
    s(out.check_official_html).includes("sfOpenOfficialPage()")],
  ["check_official 动作**不含** sfStartLogin（不重新登录）",
    !s(out.check_official_html).includes("sfStartLogin")],
  ["check_official 动作不含 /api/auth/login",
    !s(out.check_official_html).includes("/api/auth/login")],
  ["check_official 文案如实说明「专用浏览器」",
    s(out.check_official_html).includes("专用浏览器")],
  ["环境被拦截空态按钮走 sfOpenOfficialPage()",
    s(out.risk_env_empty_html).includes("sfOpenOfficialPage()")],
  ["环境被拦截空态不含 sfStartLogin",
    !s(out.risk_env_empty_html).includes("sfStartLogin")],
  ["环境被拦截空态文案如实说明「专用浏览器」",
    s(out.risk_env_empty_html).includes("专用浏览器")],
  /* 反向对照：真正的验证码空态仍走登录（证明没有把整组动作一起改坏） */
  ["对照：验证码空态仍走 sfStartLogin（未误伤）",
    s(out.risk_verify_empty_html).includes("sfStartLogin")],
  /* 打开官方页面：恰好一次、命中**专用端点**，且**不**在日常浏览器里开窗口 */
  ["打开官方页面恰好命中一次专用端点 /api/channel/open-official",
    calls.length === 1 && calls[0] === "/api/channel/open-official"],
  ["专用端点常量指向 /api/channel/open-official",
    out.official_endpoint === "/api/channel/open-official"],
  ["打开官方页面**零业务请求**（不是 login/search/detail）",
    calls.every((p) => !isBusiness(p))],
  ["打开官方页面**不**在日常浏览器里 window.open",
    (out.opened || []).length === 0],
];

const failed = checks.filter(([, pass]) => !pass).map(([name]) => name);
process.stdout.write(`${JSON.stringify({
  ok: failed.length === 0, failed,
  detail: { official_endpoint: out.official_endpoint, opened: out.opened,
            api_calls_after_open: out.api_calls_after_open },
})}\n`);
process.exit(failed.length === 0 ? 0 : 1);
