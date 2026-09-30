/* 设置页「采集浏览器来源」下拉与保存的**执行级**契约（无 JS 构建步骤，用 Node 跑真代码）。
 *
 * 用法： ``node backend/tests/frontend_app_settings_contract.mjs <path-to-app.js>``
 * 输出： 一行 JSON 到 stdout：``{"ok": bool, "detail": { ... }}``
 * 退出码：全部通过 0，否则 1。
 *
 * 真机缺陷（H1）：下拉框只有 auto/own 两项，stored-cookie 落进「不是 existing-browser
 * 就选 auto」的兜底 → 页面把**专用浏览器**通道显示成「自动（默认）」，保存时又被静默
 * 降级回 auto，并触发 channel_state.invalidate_all()（正在就绪的通道被无谓清空）。
 *
 * 这里把**真实发布**的 ``frontend/app.js`` 装进 Node 的 ``vm`` 最小沙箱，直接调用真正的
 * ``RENDER.settings()`` 与 ``saveSettings()``，测的是卖出去的代码，不是它的复制品。
 */
import fs from "node:fs";
import vm from "node:vm";

const appPath = process.argv[2];
if (!appPath) {
  console.error("usage: node frontend_app_settings_contract.mjs <path-to-app.js>");
  process.exit(2);
}
const src = fs.readFileSync(appPath, "utf8");

const noop = () => {};
const stubEl = (extra) =>
  Object.assign({
    innerHTML: "", textContent: "", value: "", checked: false, hidden: false, style: {},
    addEventListener: noop, appendChild: noop, remove: noop, setAttribute: noop,
    getAttribute: () => null, classList: { add: noop, remove: noop, contains: () => false },
    querySelector: () => null, querySelectorAll: () => [], select: noop,
  }, extra || {});

const FAKE = {};       // selector → 元素桩（由 epilogue 按场景重建）
const DELETED = [];    // 捕获 channel_state.invalidate_all 之类的网络副作用（本处仅记录 POST）
const POSTS = [];      // 捕获 /api/settings 的 POST body
const OPENED = [];     // 捕获 window.open

const sandbox = {
  console, __FAKE: FAKE, __POSTS: POSTS, __OPENED: OPENED, __RESULT: null, __DONE: null,
  window: { addEventListener: noop, location: { hash: "" }, open: (...a) => { OPENED.push(a); } },
  document: {
    addEventListener: noop,
    querySelector: (sel) => FAKE[sel] || null,
    querySelectorAll: () => [], getElementById: () => null,
    createElement: stubEl, body: stubEl(), head: stubEl(),
  },
  location: { hash: "", pathname: "/" }, history: { replaceState: noop },
  navigator: {}, localStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, data: {} }) }),
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval: noop,
  requestAnimationFrame: noop, alert: noop, confirm: () => true,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

const epilogue = `
  const mk = (value, attrs, checked) => ({
    value: value == null ? "" : String(value), checked: !!checked,
    getAttribute: (n) => (attrs && Object.prototype.hasOwnProperty.call(attrs, n)) ? attrs[n] : null,
  });
  const buildFake = (cfg) => {
    for (const k of Object.keys(__FAKE)) delete __FAKE[k];
    Object.assign(__FAKE, {
      "#st-cli": mk(""), "#st-datadir": mk(""),
      "#st-llm-provider": mk(""), "#st-llm-base": mk(""), "#st-llm-model": mk(""),
      "#st-llm-key": mk(""), "#st-demo-mode": mk("", null, false),
      "#st-browser-source": mk(cfg.fakeSourceValue || cfg.source, { "data-source": cfg.source }),
      "#st-cdp-url": mk(cfg.cdp, { "data-url": cfg.cdp }),
    });
  };
  const settingsPayload = (cfg) => ({
    ok: true, data: {
      settings: { boss_browser_source: cfg.source, boss_cdp_url: cfg.cdp },
      login_steps: [], effective_data_dir: "D:/x", data_dir_source: "default",
      website_channel: "CLI", mcp_note: "", install_hint: "",
    },
  });

  globalThis.__DONE = (async () => {
    const out = {};
    toast = () => {};   // 通知条依赖真实 DOM，桩掉

    // 1) 渲染：stored-cookie 必须被如实表示（专用浏览器选项被选中）
    const renderCfg = { source: "stored-cookie", cdp: "http://127.0.0.1:62740" };
    api = () => Promise.resolve(settingsPayload(renderCfg));
    const main = { innerHTML: "" };
    await RENDER.settings(main);
    out.render_stored_cookie_html = main.innerHTML;

    // 2) 保存：未改动（原值 stored-cookie，界面仍是 stored-cookie）→ 一个通道键都不许写
    const runSave = async (cfg) => {
      buildFake(cfg);
      __POSTS.length = 0;
      api = (path, opts) => {
        if (opts && opts.method === "POST" && opts.body) __POSTS.push(opts.body.key);
        return Promise.resolve({ ok: true, data: {} });
      };
      await saveSettings();
      return __POSTS.slice();
    };
    out.posts_unchanged = await runSave({ source: "stored-cookie", cdp: "http://127.0.0.1:62740" });
    out.posts_changed = await runSave({ source: "stored-cookie", cdp: "http://127.0.0.1:62740",
                                        fakeSourceValue: "existing-browser" });
    out.posts_unknown = await runSave({ source: "some-future-source", cdp: "http://127.0.0.1:62740",
                                        fakeSourceValue: "auto" });
    __RESULT = out;
  })();
`;

vm.runInContext(`${src}\n;\n${epilogue}`, sandbox, { filename: "app.js" });

await sandbox.__DONE;
const out = sandbox.__RESULT || {};
const html = String(out.render_stored_cookie_html || "");
const postsUnchanged = out.posts_unchanged || [];
const postsChanged = out.posts_changed || [];
const postsUnknown = out.posts_unknown || [];

const CHANNEL_KEYS = ["boss_browser_source", "boss_cdp_url"];

const checks = [
  ["存储值 stored-cookie 时，专用浏览器选项被选中",
    html.includes('<option value="stored-cookie" selected>')],
  ["专用浏览器选项带用户可读中文文案",
    html.includes("用本项目的专用浏览器")],
  ["stored-cookie 时「自动」选项不再被误选（旧兜底已删）",
    !html.includes('<option value="auto" selected>')],
  ["select 带 data-source 原始值，供保存时比对",
    html.includes('data-source="stored-cookie"')],
  ["未改动保存：不写 boss_browser_source（保留原值，不触发 invalidate）",
    !postsUnchanged.includes("boss_browser_source")],
  ["未改动保存：不写 boss_cdp_url（不触发 invalidate）",
    !postsUnchanged.includes("boss_cdp_url")],
  ["用户显式改为 existing-browser：写一次 boss_browser_source=existing-browser",
    postsChanged.includes("boss_browser_source")],
  ["无法表示的原值（some-future-source）：保存时保持原值、不覆盖",
    !postsUnknown.includes("boss_browser_source")],
];

const failed = checks.filter(([, pass]) => !pass).map(([name]) => name);
process.stdout.write(`${JSON.stringify({
  ok: failed.length === 0, failed, detail: { postsUnchanged, postsChanged, postsUnknown },
})}\n`);
process.exit(failed.length === 0 ? 0 : 1);
