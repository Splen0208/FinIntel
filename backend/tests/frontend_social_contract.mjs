/* 社交结果页 T16/T17 前端行为的**执行级**契约（无 JS 构建步骤，用 Node 跑真代码）。
 *
 * 用法： ``node backend/tests/frontend_social_contract.mjs <frontend-dir>``
 *   （第二参数是 frontend 目录，脚本会依次加载 simple.js 与 social.js，与 index.html 一致）
 * 输出： 一行 JSON 到 stdout：``{"ok": bool, "failed": [ ... ], "detail": { ... }}``
 * 退出码：全部通过 0，否则 1。
 *
 * 为什么用 vm 而不是正则扫源码：把**真实发布**的 ``frontend/social.js`` 与 ``frontend/simple.js``
 * 装进 Node 的 ``vm`` 最小沙箱（桩掉 DOM / 网络 / 轮询），直接调用真正的
 * ``sfRenderSocial`` / ``sfSocialRenderResult`` / ``sfTickTask`` / ``sfRunTask`` / ``sfHistoryRows``，
 * 断言的是行为，不是它的复制品。覆盖《指令》§5.G 要求：
 *   1) 失败态与空态文案不同；2) 终态停止轮询；3) 切换任务丢弃过期响应；
 *   4) 评论覆盖声明；5) 按 task_type 渲染历史；6) 证据上下文/快照；7) AI 计数口径。
 */
import fs from "node:fs";
import path from "node:path";

const frontendDir = process.argv[2];
if (!frontendDir) {
  console.error("usage: node frontend_social_contract.mjs <frontend-dir>");
  process.exit(2);
}
const socialSrc = fs.readFileSync(path.join(frontendDir, "social.js"), "utf8");
const simpleSrc = fs.readFileSync(path.join(frontendDir, "simple.js"), "utf8");

const noop = () => {};
const BODY = { innerHTML: "", dataset: {}, hidden: false, style: {}, textContent: "",
  addEventListener: noop, nextElementSibling: null,
  classList: { add: noop, remove: noop, toggle: noop, contains: () => false } };
const INTERVALS = [];
const OPENED = [];

const stubEl = () => ({
  innerHTML: "", textContent: "", value: "", hidden: false, dataset: {}, style: {},
  addEventListener: noop, appendChild: noop, setAttribute: noop, getAttribute: () => null,
  classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
  querySelector: () => null, querySelectorAll: () => [], nextElementSibling: null,
});

const sandbox = {
  console, URL,
  __BODY: BODY, __INTERVALS: INTERVALS, __OPENED: OPENED, __RESULT: null, __DONE: null,
  __routeSeq: 1,
  window: { addEventListener: noop, location: { hash: "" }, open: (...a) => { OPENED.push(a); } },
  document: {
    hidden: false, addEventListener: noop, getElementById: () => null,
    querySelector: (sel) => (sel === "#sf-body" ? BODY : null),
    querySelectorAll: () => [], createElement: stubEl, body: stubEl(), head: stubEl(),
  },
  location: { hash: "#/result/run_x", pathname: "/" }, history: { replaceState: noop },
  RENDER: {}, route: { page: "result", arg: "run_x", query: {}, diag: false },
  localStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, data: {} }) }),
  setTimeout, clearTimeout,
  setInterval: (fn, ms) => { const id = INTERVALS.length + 1; INTERVALS.push({ id, ms }); return id; },
  clearInterval: noop, requestAnimationFrame: noop, alert: noop, confirm: () => true,
  api: () => Promise.resolve({ ok: false }),
  toast: noop,
  esc: (s) => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"),
  fmtTime: (s) => (s ? String(s) : "—"),
  $: (sel, root) => {
    if (sel === "#sf-body") return BODY;
    if (root && sel === "#sf-social-root") return {};          // 让“数据未变则跳过重绘”生效
    if (sel === "#sf-rhead" || sel === "#sf-progress" || sel === "#modal-root" || sel === "#toasts") return stubEl();
    return null;
  },
  $$: () => [],
};
sandbox.globalThis = sandbox;
const vm = await import("node:vm");
vm.createContext(sandbox);

const epilogue = `
  globalThis.__DONE = (async () => {
    const out = {};
    const T = window.SocialView._test;
    const body = $("#sf-body");

    // ---- 1) 结果接口失败 vs 真·空搜索 ----
    SF_SOCIAL.run = "run_x"; SF_SOCIAL.req = 0; SF_SOCIAL.sig = null; body.innerHTML = "";
    api = () => Promise.resolve({ ok: false, error: { code: "BOOM" } });
    await sfRenderSocial("run_x", { run_id: "run_x", task_type: "x_topics", stage: "failed",
      user_message: "采集失败：来源连接不可用", query: "q" });
    out.fail_html = body.innerHTML;

    SF_SOCIAL.sig = null;
    api = () => Promise.resolve({ ok: true, data: { items: [], findings: [], counters: {}, task_type: "x_topics", query: "q" } });
    await sfRenderSocial("run_x", { run_id: "run_x", task_type: "x_topics", stage: "completed", query: "q" });
    out.empty_html = body.innerHTML;

    SF_SOCIAL.sig = null;
    await sfRenderSocial("run_x", { run_id: "run_x", task_type: "xhs_career", stage: "paused",
      user_message: "采集已暂停：需要完成平台验证", query: "q" });
    out.paused_html = body.innerHTML;

    // ---- 数据未变 → 不重绘（保留展开态 / 不闪烁） ----
    SF_SOCIAL.sig = null;
    api = () => Promise.resolve({ ok: true, data: { items: [{ item_id: "it1", platform: "x", evidence_text: "正文", snapshot_id: "ssn_1", detail_status: "complete" }], findings: [], counters: {} } });
    await sfSocialRenderResult("run_x", { task_type: "x_topics", stage: "searching" }, {});
    body.innerHTML = "PRESERVED_MARKER";
    await sfSocialRenderResult("run_x", { task_type: "x_topics", stage: "searching" }, {});
    out.unchanged_html = body.innerHTML;

    // ---- 2) 终态停止轮询 / 非终态继续轮询 ----
    SF_ACTIVE_RUN = "run_t";
    SF_SOCIAL.sig = null;
    __INTERVALS.length = 0;
    api = (p) => (String(p).indexOf("/result") >= 0
      ? Promise.resolve({ ok: true, data: { items: [], findings: [], counters: {} } })
      : Promise.resolve({ ok: true, data: { task: { run_id: "run_t", task_type: "x_topics", stage: "completed", query: "q" } } }));
    await sfRunTask("run_t");
    out.intervals_after_terminal = __INTERVALS.length;

    __INTERVALS.length = 0; SF_SOCIAL.sig = null;
    api = (p) => (String(p).indexOf("/result") >= 0
      ? Promise.resolve({ ok: true, data: { items: [], findings: [], counters: {} } })
      : Promise.resolve({ ok: true, data: { task: { run_id: "run_t", task_type: "x_topics", stage: "searching", query: "q" } } }));
    await sfRunTask("run_t");
    out.intervals_after_running = __INTERVALS.length;
    sfClearPageTimers();

    // ---- 3) 切换任务丢弃过期响应 ----
    SF_SOCIAL.run = "A"; SF_SOCIAL.req = 0; SF_SOCIAL.sig = null; body.innerHTML = "";
    let resolveA;
    api = () => new Promise((res) => { resolveA = res; });
    const pA = sfSocialRenderResult("A", { task_type: "x_topics", stage: "searching" }, {});
    SF_SOCIAL.run = "B";            // 模拟用户切到另一个任务
    SF_SOCIAL.req += 1;             // 更新的请求序号
    resolveA({ ok: true, data: { items: [{ item_id: "aaa", platform: "x", evidence_text: "AAA_MARKER" }], findings: [], counters: {} } });
    await pA;
    out.stale_html = body.innerHTML;

    SF_SOCIAL.run = "C"; SF_SOCIAL.req = 0; SF_SOCIAL.sig = null; body.innerHTML = "";
    let resolveC;
    api = () => new Promise((res) => { resolveC = res; });
    const pC = sfSocialRenderResult("C", { task_type: "x_topics", stage: "searching" }, {});
    resolveC({ ok: true, data: { items: [{ item_id: "ccc", platform: "x", evidence_text: "CCC_MARKER" }], findings: [], counters: {} } });
    await pC;
    out.current_html = body.innerHTML;

    // ---- 4) 评论覆盖声明 ----
    out.coverage_text = T.commentCoverageText(3, 12);
    out.comments_empty_html = T.commentsHtml([]);
    out.comments_html = T.commentsHtml([
      { source_comment_id: "c1", parent_source_id: null, evidence_text: "根评论A", author_key: "anon_1" },
      { source_comment_id: "c2", parent_source_id: null, evidence_text: "根评论B", author_key: "anon_2" },
      { source_comment_id: "c3", parent_source_id: "c1", parent_missing: true, identity_quality: "synthetic", evidence_text: "回复C", author_key: "anon_3" },
    ]);

    // ---- 5) 历史按 task_type 渲染 ----
    out.hist_html = sfHistoryRows([
      { run_id: "r1", task_type: "x_topics", query: "海外热点", state: "completed", started_at: "2026-09-01T00:00:00Z" },
      { run_id: "r2", task_type: "xhs_career", query: "岗位讨论", state: "failed", started_at: "2026-09-02T00:00:00Z" },
      { run_id: "r3", query: "岗位", collected: 5, organized: 3, started_at: "2026-09-03T00:00:00Z" },
    ], true);

    // ---- 6) AI 计数口径与证据 ----
    out.ai_note = T.aiNote({ ai_batches_ok: 3, ai_batches_failed: 1 }, 7);
    const ctx = { bySnapshot: { "ssn_1": { platform: "x", fetched_at: "2026-09-01T00:00:00Z" } }, platformLabel: "X" };
    out.evidence_card = T.evidenceCard({ snapshot_id: "ssn_1", comment_snapshot_id: null, quote: "信贷", start: 2, end: 4, stance: "support", source_text: "消费信贷增长很快" }, ctx);
    out.ctx = T.evidenceContext("abcdefghij", 3, 5, "de");

    __RESULT = out;
  })();
`;

vm.runInContext(`${simpleSrc}\n;\n${socialSrc}\n;\n${epilogue}`, sandbox, { filename: "frontend.js" });

await sandbox.__DONE;
const o = sandbox.__RESULT || {};
const s = (v) => String(v == null ? "" : v);

const checks = [
  ["失败态：结果读取失败（可重试）且不伪装成空结果",
    s(o.fail_html).includes("结果读取失败") && s(o.fail_html).includes("可重试")
      && !s(o.fail_html).includes("没有返回可用内容")],
  ["空态：显示“确实没有返回可用内容”，且不等于读取失败",
    s(o.empty_html).includes("没有返回可用内容") && !s(o.empty_html).includes("结果读取失败")],
  ["失败/暂停任务：显示“任务已暂停”+原因，不显示“无结果”",
    s(o.paused_html).includes("任务已暂停") && s(o.paused_html).includes("需要完成平台验证")
      && !s(o.paused_html).includes("没有返回可用内容")],
  ["数据未变：轮询不重绘（保留展开态）",
    s(o.unchanged_html) === "PRESERVED_MARKER"],
  ["终态：不继续轮询（未登记新定时器）",
    Number(o.intervals_after_terminal) === 0],
  ["非终态：仍会继续轮询（登记了定时器）",
    Number(o.intervals_after_running) >= 1],
  ["切换任务：过期响应不覆盖新任务页面（不回写 AAA_MARKER）",
    !s(o.stale_html).includes("AAA_MARKER")],
  ["对照：当前任务响应正常回写（包含 CCC_MARKER）",
    s(o.current_html).includes("CCC_MARKER")],
  ["评论覆盖声明：含“根评论/含回复/不代表已获取全部”",
    s(o.coverage_text).includes("根评论 3 条") && s(o.coverage_text).includes("含回复共 12 条")
      && s(o.coverage_text).includes("不代表已获取")],
  ["评论列表头：按根评论限额如实声明，不暗示已获取全部",
    s(o.comments_html).includes("读取根评论 2 条") && s(o.comments_html).includes("含回复共 3 条")
      && s(o.comments_html).includes("不代表已获取")],
  ["Synthetic 评论标注“合成键”",
    s(o.comments_html).includes("合成键")],
  ["空评论：不暗示平台没有评论",
    s(o.comments_empty_html).includes("不代表平台没有评论")],
  ["历史按 task_type 渲染：X/小红书/BOSS 三种标签都在",
    s(o.hist_html).includes("X 议题分析") && s(o.hist_html).includes("小红书岗位讨论")
      && s(o.hist_html).includes("BOSS 岗位整理")],
  ["历史不再把社交任务显示成“已整理 0 个岗位”",
    !s(o.hist_html).includes("已整理 0 个岗位")],
  ["AI 计数口径：分析批次 + 结论主题（不写“已分析 N 条”）",
    s(o.ai_note).includes("AI 完成 3 个分析批次") && s(o.ai_note).includes("产出 7 个结论主题")
      && !/已分析/.test(s(o.ai_note))],
  ["证据弹窗：含正文上下文、快照 ID、类型",
    s(o.evidence_card).includes("正文上下文") && s(o.evidence_card).includes("ssn_1")
      && s(o.evidence_card).includes("原帖")],
  ["证据上下文：前后文与 mark 定位正确",
    (o.ctx || {}).mark === "de" && (o.ctx || {}).before === "abc" && (o.ctx || {}).after === "fghij"],
  ["源码红线：social.js 概览文案不含“已分析”",
    !/已分析/.test(socialSrc)],
];

const failed = checks.filter(([, pass]) => !pass).map(([name]) => name);
process.stdout.write(`${JSON.stringify({ ok: failed.length === 0, failed,
  detail: { intervals_terminal: o.intervals_after_terminal, intervals_running: o.intervals_after_running } })}\n`);
process.exit(failed.length === 0 ? 0 : 1);
