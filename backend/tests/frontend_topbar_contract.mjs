/* 前端顶栏「账号 × 通道」两维文案的执行级契约。
 *
 * 用法： ``node backend/tests/frontend_topbar_contract.mjs <path-to-simple.js>``
 * 输出： 一行 JSON 到 stdout：``{"ok": bool, "cases": [ ... ]}``
 * 退出码：全部通过 0，否则 1。
 *
 * 为什么用 Node 的 ``vm`` 而不是正则扫源码：把**真实发布**的 ``frontend/simple.js``
 * 装进一个最小沙箱（桩掉 DOM / 网络 / 轮询），随后调用真正的
 * ``sfSetBoss()`` / ``sfPillText()`` / ``sfChannelReady()``。测的是卖出去的代码，
 * 不是它的复制品——真机缺陷（``account_state=connected`` 而 ``user_stage`` 被通道未就绪
 * 打扮成 ``env_not_ready``）正是发生在这三个函数的组合里。
 */
import fs from "node:fs";
import vm from "node:vm";

const simplePath = process.argv[2];
if (!simplePath) {
  console.error("usage: node frontend_topbar_contract.mjs <path-to-simple.js>");
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

/* 真机形状矩阵：account_state 是「未被打扮的账号事实」，user_stage 可能被通道状态
   覆盖成 env_not_ready。顶栏文案与绿点都只能按 account_state × channel.state 判。 */
const CASES = [
  {
    name: "connected+ready",
    boss: { account_state: "connected", user_stage: "connected", state: "connected",
            channel: { state: "ready" }, channel_ready: true, collect_ready: true },
    expect_pill: "BOSS 已连接", expect_ready: true,
  },
  {
    name: "connected+not_ready",
    boss: { account_state: "connected", user_stage: "env_not_ready", state: "connected",
            channel: { state: "not_ready" }, channel_ready: false, collect_ready: false },
    expect_pill: "账号登录已验证，当前搜索连接尚未就绪。", expect_ready: false,
  },
  {
    name: "connected+unknown",
    boss: { account_state: "connected", user_stage: "env_not_ready", state: "connected",
            channel: { state: "unknown" }, channel_ready: false, collect_ready: true },
    expect_pill: "BOSS 已连接，正在确认搜索通道", expect_ready: false,
  },
  {
    /* 老响应没有显式 channel_ready：只要 channel.state==='ready' 也必须判绿 */
    name: "connected+ready_without_explicit_field",
    boss: { account_state: "connected", user_stage: "connected", state: "connected",
            channel: { state: "ready" }, collect_ready: true },
    expect_pill: "BOSS 已连接", expect_ready: true,
  },
  {
    name: "disconnected",
    boss: { account_state: "disconnected", user_stage: "disconnected", state: "disconnected",
            channel: { state: "unknown" }, channel_ready: false, collect_ready: false },
    expect_pill: "BOSS：未登录", expect_ready: false,
  },
];

const sandbox = {
  console,
  __CASES: CASES,
  __RESULT: null,
  window: { addEventListener: noop, location: { hash: "" } },
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

/* 关键：加载与断言写在**同一个脚本**里，``sfRefreshBoss()`` 的微任务在同步断言结束前
   不会插入执行（脚本返回时调用栈才清空），因此读到的一定是我们刚设的状态。 */
const epilogue = `
  const __out = [];
  for (const __c of __CASES) {
    sfSetBoss(__c.boss);
    __out.push({
      name: __c.name,
      account_state: __c.boss.account_state,
      user_stage: __c.boss.user_stage,
      channel_state: (__c.boss.channel || {}).state || null,
      pill: sfPillText(),
      ready: sfChannelReady(),
      expect_pill: __c.expect_pill,
      expect_ready: __c.expect_ready,
    });
  }
  __RESULT = __out;
`;

vm.runInContext(`${src}\n;\n${epilogue}`, sandbox, { filename: "simple.js" });

const cases = (sandbox.__RESULT || []).map((c) => ({
  ...c,
  pass: c.pill === c.expect_pill && c.ready === c.expect_ready,
}));
const ok = cases.length === CASES.length && cases.every((c) => c.pass);
process.stdout.write(`${JSON.stringify({ ok, cases })}\n`);
process.exit(ok ? 0 : 1);
