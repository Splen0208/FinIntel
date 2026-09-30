/* finintel 社交多源结果渲染（X 海外热点 / 小红书岗位讨论）。
 * 原生 JS，无构建步骤。由 simple.js 在 task_type 为 x_topics/xhs_career 时调用。
 * 复用全局 esc / api / toast / fmtTime（app.js 提供）。
 *
 * T16 / T17 硬化要点（对应《指令》§5.G）：
 *  1) 结果接口失败与「真的空搜索」在文案上严格区分：失败绝不伪装成空结果；
 *     失败/暂停任务显示「任务已失败/已暂停」+ 原因，而不是「无结果」。
 *  2) 轮询不再无脑重绘：数据签名不变则跳过重绘；变了也保留用户已展开的原文/评论。
 *  3) 切换任务后用「请求序号 + runId」丢弃过期响应，旧任务页面不会被覆盖。
 *  4) 终态由 simple.js 停止轮询（这里只负责 await 渲染完成）。
 *  5) 证据弹窗补正文上下文、来源、快照 ID、原帖/评论类型。
 *  6) 评论入口与「根评论限额」覆盖声明，绝不暗示「已获取全部评论」。
 *  7) X 时间范围、未知发布时间数量与采样限制如实展示。
 *  8) 局部失败（正文/评论未取到）与来源筛选状态可见。
 */

const SOCIAL_TERMINAL = ["completed", "failed", "cancelled", "partial", "paused"];
const SOCIAL_STAGE_LABEL = {
  queued: "排队中", searching: "正在采集", fetching_details: "正在读取正文",
  fetching_comments: "正在读取评论", deduping: "正在去重", analyzing: "正在 AI 整理",
  completed: "已完成", failed: "失败", cancelled: "已取消", partial: "部分完成", paused: "已暂停",
};

/* 面向用户的终态标题；主文案里绝不出现工程错误码 */
const SF_SOCIAL_STATE_TITLE = {
  failed: "任务已失败",
  paused: "任务已暂停",
  cancelled: "任务已取消",
  partial: "任务部分完成",
  completed: "任务已完成",
};

/* 正文状态 → 用户文案（complete / partial / missing 三态互斥） */
const SF_DETAIL_NOTE = {
  complete: "",
  partial: "仅取得部分正文（详情不完整）",
  missing: "正文未取得（读取失败或上游未返回）",
  failed: "正文读取失败",
  summary: "搜索摘要（尚未读取正文）",
};

/* 社交结果动作白名单：只渲染有真实后端实现的动作，绝不出现点了必失败的假按钮 */
const SF_SOCIAL_ACTION = {
  cancel: { label: "取消任务", kind: "" },
  "retry-organize": { label: "仅重新整理已保存材料", kind: "primary" },
  resume: { label: "继续本次采集", kind: "primary" },
};

/* 单页社交结果 UI 状态（按任务维度保留展开 / 评论缓存，跨轮询不丢） */
const SF_SOCIAL = {
  run: null,            // 当前正在渲染的任务 id
  req: 0,               // 请求序号：比对比以丢弃过期响应
  open: {},             // item_id -> true（已展开的原文）
  comments: {},         // item_id -> 已加载的评论 HTML
  sig: null,            // 数据签名：不变则跳过重绘
  src: "",              // 来源筛选（多平台时）
};

/* ============================ 纯工具 ============================ */

function sfNum(v) {
  const n = Number(v);
  return isFinite(n) ? n : 0;
}

/* AI 计数口径：**分析批次**，绝不是帖子数（红线文案见 sfAiNote） */
function sfAiBatches(c) {
  const x = (c || {});
  if (x.ai_batches_ok != null) return sfNum(x.ai_batches_ok);
  if (x.ai_batch_success != null) return sfNum(x.ai_batch_success);
  return sfNum(x.ai_batches_total);
}

function sfAiNote(counters, findingsCount) {
  const total = sfAiBatches(counters);
  const failed = sfNum((counters || {}).ai_batches_failed != null
    ? counters.ai_batches_failed : counters.ai_batch_failed);
  return `AI 完成 ${total} 个分析批次，产出 ${sfNum(findingsCount)} 个结论主题`
    + (failed > 0 ? `（其中 ${failed} 个批次失败，结论为其余批次产出）` : "");
}

function sfSocialSafeUrl(value) {
  try {
    const u = new URL(value);
    if (u.protocol !== "https:" || !["x.com", "www.x.com", "twitter.com", "www.xiaohongshu.com", "xiaohongshu.com"].includes(u.hostname)) return "";
    u.search = ""; u.hash = "";
    return u.href;
  } catch (_) { return ""; }
}

function sfPlatformLabel(p) { return p === "x" ? "X" : (p === "xiaohongshu" ? "小红书" : (p || "来源")); }

/* 数据签名：用于判断是否需要重绘（数据不变就不动 DOM，展开态与缓存自然保留）。
 * 必须包含 stage 与 actions：空结果在「处理中 → 已完成」时文案要变，动作按钮也会变。 */
function sfSocialSignature(items, findings, counters, stage, actions) {
  try {
    return JSON.stringify({
      i: items.map(x => [x.item_id, x.snapshot_id, x.detail_status]),
      f: findings.map(x => [x.id, x.title]),
      c: counters || {},
      s: stage || "",
      a: (actions || []).map(x => (x && x.id) || ""),
    });
  } catch (_) { return null; }
}

/* 评论覆盖声明：上游 limit 是**根评论**限制，含回复后总数可能更大 */
function sfCommentCoverageText(rootCount, total) {
  return `本次读取根评论 ${sfNum(rootCount)} 条，含回复共 ${sfNum(total)} 条。`
    + "上游按“根评论”计限额，回复可能未全部返回，不代表已获取该内容下的全部评论。";
}

/* 证据上下文：截取引文前后各 ~80 字；start/end 不可用时退回子串定位 */
function sfEvidenceContext(text, start, end, quote) {
  const full = String(text == null ? "" : text);
  let s = (typeof start === "number" && start >= 0) ? start : null;
  let e = (typeof end === "number") ? end : null;
  if (s === null || e === null || e <= s || e > full.length) {
    const q = String(quote == null ? "" : quote);
    const idx = q ? full.indexOf(q) : -1;
    if (idx >= 0) { s = idx; e = idx + q.length; }
  }
  if (s === null || s >= full.length) {
    return { before: "", mark: String(quote || full.slice(0, 160)), after: "" };
  }
  const pad = 80;
  return {
    before: full.slice(Math.max(0, s - pad), s),
    mark: full.slice(s, e),
    after: full.slice(e, Math.min(full.length, e + pad)),
  };
}

/* ============================ 顶层渲染入口 ============================ */

async function sfRenderSocial(runId, t) {
  SF_SOCIAL.run = runId;                     // 切换任务：后续过期响应据此丢弃
  window.__SF_CURRENT_TASK = t;
  const head = $("#sf-rhead");
  const isXhs = t.task_type === "xhs_career";
  const srcLabel = isXhs ? "小红书岗位讨论" : "X 海外热点";
  if (head) {
    head.innerHTML = `<div class="sf-result-top"><a class="sf-back" href="#/history">← 返回</a>
      <span class="sf-note">${esc(srcLabel)}</span></div>
      <div class="sf-result-title"><b>${esc(t.query || "")}</b></div>`;
  }
  const stage = t.stage || "";
  const terminal = SOCIAL_TERMINAL.indexOf(stage) >= 0;
  const host = $("#sf-progress");
  if (host) host.innerHTML = sfSocialProgressHtml(t, terminal);
  await sfSocialRenderResult(runId, t, { terminal: terminal });
}

/* 进度区文案：终态显示「任务已失败/已暂停」+ 原因，绝不显示「无结果」 */
function sfSocialProgressHtml(t, terminal) {
  const stage = t.stage || "";
  if (!terminal) {
    const label = t.user_message || SOCIAL_STAGE_LABEL[stage] || "处理中";
    return `<div class="sf-note">${esc(label)}…</div>`;
  }
  if (stage === "completed") {
    return `<div class="sf-note">任务已完成，以下为本次已取得并保存的内容。</div>`;
  }
  const title = SF_SOCIAL_STATE_TITLE[stage] || "任务状态";
  const reason = String(t.user_message || "").trim();
  const cls = (stage === "partial" || stage === "cancelled") ? "warn" : "err";
  return `<div class="sf-alert sf-alert-${cls}">
    <div class="sf-alert-title">${esc(title)}</div>
    ${reason && reason !== title ? `<div class="sf-alert-text">${esc(reason)}</div>` : ""}
    <div class="sf-alert-text">已保存的材料不会被丢弃；下方显示的是本次已取得的内容，这不代表没有结果。</div>
  </div>`;
}

/* ============================ 结果区 ============================ */

async function sfSocialRenderResult(runId, t, opts) {
  opts = opts || {};
  const body = $("#sf-body");
  if (!body) return;
  if (SF_SOCIAL.run !== runId) return;               // 已切换任务：不写旧页面
  const token = ++SF_SOCIAL.req;

  let data = null;
  let loadError = false;
  try {
    const r = await api(`/api/tasks/${encodeURIComponent(runId)}/result`);
    if (token !== SF_SOCIAL.req || SF_SOCIAL.run !== runId) return;   // 过期响应：丢弃
    if (!r.ok) loadError = true;
    else data = r.data || {};
  } catch (e) {
    if (token !== SF_SOCIAL.req || SF_SOCIAL.run !== runId) return;
    loadError = true;
  }

  if (loadError) {
    SF_SOCIAL.sig = null;                            // 失败后强制下一次重绘
    body.innerHTML = `<div id="sf-social-root">${sfSocialLoadFailed()}</div>`;
    sfBindSocialRetry(body, runId, t);
    return;
  }

  const items = Array.isArray(data.items) ? data.items : [];
  const findings = Array.isArray(data.findings) ? data.findings : [];
  const counters = data.counters || {};
  const isXhs = t.task_type === "xhs_career";
  const sig = sfSocialSignature(items, findings, counters, t.stage, t.actions);

  // 数据未变 → 跳过重绘（轮询不破坏展开态、不闪烁）
  if (sig !== null && sig === SF_SOCIAL.sig && $("#sf-social-root", body)) return;
  SF_SOCIAL.sig = sig;

  body.innerHTML = `<div id="sf-social-root">${sfSocialBodyHtml(runId, t, data, {
    items: items, findings: findings, counters: counters, isXhs: isXhs, terminal: !!opts.terminal,
  })}</div>`;
  sfBindSocialBody(body, runId, t, data);
}

function sfSocialLoadFailed() {
  return `<div class="sf-alert sf-alert-warn">
    <div class="sf-alert-title">结果读取失败（可重试）</div>
    <div class="sf-alert-text">这次没有读到结果数据。这不代表没有采集到内容，也不是“没有找到结果”；
      已保存的材料仍在，稍后重试即可。</div>
    <div class="btn-row mt8">
      <button class="btn primary" type="button" data-social-retry="1">重试读取</button>
      <a class="btn" href="#/history">返回历史</a>
    </div>
  </div>`;
}

/* 真的空结果 vs 任务失败/暂停：文案必须不同，绝不能把失败说成「没有内容」 */
function sfSocialEmptyNote(t) {
  const stage = t.stage || "";
  if (stage === "failed" || stage === "paused" || stage === "cancelled" || stage === "partial") {
    const title = SF_SOCIAL_STATE_TITLE[stage] || "任务未完成";
    const reason = String(t.user_message || "").trim();
    return `<div class="sf-alert sf-alert-warn">
      <div class="sf-alert-title">${esc(title)}</div>
      <div class="sf-alert-text">${esc(reason || "本次未取得任何可用材料。")}
        这不代表平台没有相关内容，只表示本次读取未完成。</div>
    </div>`;
  }
  if (stage === "completed") {
    return `<div class="sf-note">本次搜索确实没有返回可用内容（这是真实的空结果，并非数据读取异常）。可以换个关键词再试。</div>`;
  }
  return `<div class="sf-note">尚未保存内容，任务仍在处理中，请稍候。</div>`;
}

function sfSocialBodyHtml(runId, t, d, ctx) {
  const items = ctx.items || [];
  const findings = ctx.findings || [];
  const counters = ctx.counters || {};
  const isXhs = ctx.isXhs;
  const detailRead = sfNum(counters.detail_complete) + sfNum(counters.detail_partial);
  const missing = sfNum(counters.detail_missing);
  const comments = sfNum(counters.comments_collected);
  const commentFailed = sfNum(counters.comment_fetch_failed_items);

  const overview = `
  <div class="sf-social-overview">
    <div class="sf-stat"><b>${items.length}</b><span>${isXhs ? "笔记" : "帖子"}</span></div>
    <div class="sf-stat"><b>${detailRead}</b><span>已读正文（含部分）</span></div>
    <div class="sf-stat"><b>${comments}</b><span>评论</span></div>
    <div class="sf-stat"><b>${findings.length}</b><span>结论主题</span></div>
  </div>
  <div class="sf-note">${esc(sfAiNote(counters, findings.length))}</div>`;

  // 局部失败状态：正文/评论未取到必须看得见
  const partialBits = [];
  if (missing > 0) partialBits.push(`${missing} 条正文未取得`);
  if (commentFailed > 0) partialBits.push(`${commentFailed} 条内容的评论读取失败`);
  const partialNote = partialBits.length
    ? `<div class="sf-infobar" id="sf-partial-note">本次为部分完成：${esc(partialBits.join("、"))}；已取得的材料照常保留，缺失项可稍后重试。</div>`
    : "";

  // X：时间范围 / 未知发布时间 / 采样限制
  const xRange = (!isXhs) ? sfXRangeHtml(t, d, items) : "";

  // 来源筛选：仅当本次结果确实包含多个来源时才出现
  const platforms = Array.from(new Set(items.map(i => i.platform).filter(Boolean)));
  const filterHtml = platforms.length > 1 ? `
    <div class="sf-chip-row" id="sf-src-filter">
      <button class="sf-chip${SF_SOCIAL.src ? "" : " active"}" data-src="">全部来源</button>
      ${platforms.map(p => `<button class="sf-chip${SF_SOCIAL.src === p ? " active" : ""}" data-src="${esc(p)}">${esc(sfPlatformLabel(p))}</button>`).join("")}
    </div>` : "";

  const findingsHtml = findings.length ? `
  <section class="sf-social-section">
    <h3 class="sf-h3">${isXhs ? "岗位讨论主题" : "行业议题"}</h3>
    ${findings.map(f => `
      <div class="sf-social-finding">
        <div class="sf-social-finding-title">${esc(f.title || "未命名")}</div>
        <div class="sf-social-finding-summary">${esc(f.summary_zh || "")}</div>
        ${f.limitations_json ? `<div class="sf-note">局限：${esc(sfLimitations(f.limitations_json))}</div>` : ""}
        <button class="sf-link" type="button" data-finding="${esc(f.id)}">查看依据 →</button>
        <button class="btn sm" type="button" onclick="interviewChooseFinding('${esc(f.id)}',this)">加入面试准备</button>
      </div>`).join("")}
  </section>` : `<div class="sf-note">材料已保存，AI 整理尚未产出结论主题。</div>`;

  const itemsHtml = items.length ? `
  <section class="sf-social-section">
    <h3 class="sf-h3">原始材料（${items.length}）</h3>
    ${filterHtml}
    ${items.map(it => sfSocialItemHtml(it, isXhs)).join("")}
  </section>` : `<section class="sf-social-section">${sfSocialEmptyNote(t)}</section>`;

  const actions = (t.actions || []).filter(a => a && SF_SOCIAL_ACTION[a.id]);

  return overview
    + `<div class="btn-row mt8">
         ${actions.map(a => `<button class="btn ${SF_SOCIAL_ACTION[a.id].kind}" type="button" data-social-action="${esc(a.id)}">${esc(a.label || SF_SOCIAL_ACTION[a.id].label)}</button>`).join("")}
         <button class="btn" type="button" data-social-export="1">导出报告</button>
         <a class="btn" href="#/history">返回历史</a>
       </div>`
    + xRange + partialNote + findingsHtml + itemsHtml;
}

function sfLimitations(raw) {
  try {
    const v = (typeof raw === "string") ? JSON.parse(raw) : raw;
    if (Array.isArray(v)) return v.join("；");
    if (v && typeof v === "object") return Object.values(v).join("；");
    return String(v || "");
  } catch (_) { return String(raw || ""); }
}

function sfSocialItemHtml(it, isXhs) {
  const itemId = it.item_id;
  const open = !!SF_SOCIAL.open[itemId];
  const detailNote = SF_DETAIL_NOTE[it.detail_status] || "";
  const url = sfSocialSafeUrl(it.canonical_url);
  const cached = SF_SOCIAL.comments[itemId];
  const title = (it.title || it.evidence_text || "无正文").slice(0, 90);
  return `<div class="sf-social-item" data-platform="${esc(it.platform || "")}">
    <div class="sf-social-item-head">
      <span class="sf-badge">${esc(sfPlatformLabel(it.platform))}</span>
      <span class="sf-social-item-author">${esc(it.author_key || "匿名作者")}</span>
      ${it.published_at ? `<span class="sf-note">${esc(fmtTime(it.published_at))}</span>`
                        : `<span class="sf-note">发布时间未知</span>`}
      ${detailNote ? `<span class="sf-note">${esc(detailNote)}</span>` : ""}
    </div>
    <details data-item="${esc(itemId)}"${open ? " open" : ""}>
      <summary>${esc(title)} · 展开原文</summary>
      <div class="sf-social-item-text" style="white-space:pre-wrap">${esc(it.evidence_text || "尚未取得正文")}</div>
    </details>
    ${url ? `<a class="sf-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">来源 ↗</a>`
          : `<span class="sf-note">来源链接未通过校验，已隐藏</span>`}
    ${isXhs ? `<button class="sf-link" type="button" data-comments="${esc(itemId)}">查看已采评论</button>
      <div data-comment-host="${esc(itemId)}"${cached ? ' data-loaded="1"' : ""}>${cached || ""}</div>` : ""}
  </div>`;
}

/* X 时间范围 / 未知发布时间 / 采样限制如实展示 */
function sfXRangeHtml(t, d, items) {
  const win = (d && d.filters && d.filters.time_window)
    || (d && d.time_window)
    || (t && t.filters && t.filters.time_window) || "";
  const days = win ? String(win).replace("d", "") : "7";
  const unknown = items.filter(i => !i.published_at).length;
  const dated = items.map(i => i.published_at).filter(Boolean).sort();
  const span = dated.length ? `${fmtTime(dated[0])} ～ ${fmtTime(dated[dated.length - 1])}` : "";
  const parts = [`时间范围：本次请求近 ${esc(days)} 天${win ? "" : "（默认）"}。`];
  if (span) parts.push(`已取内容发布于 ${esc(span)}。`);
  parts.push(`其中 ${unknown} 条未标注发布时间，无法确认是否落在时间窗内。`);
  parts.push("平台检索为关键词采样，仅代表本次取到的样本，不代表全部讨论。");
  return `<div class="sf-infobar" id="sf-xrange">${parts.join(" ")}</div>`;
}

/* ============================ 事件绑定 ============================ */

function sfBindSocialRetry(body, runId, t) {
  $$("[data-social-retry]", body).forEach(btn => btn.addEventListener("click", () => {
    SF_SOCIAL.sig = null;
    sfSocialRenderResult(runId, t, { terminal: SOCIAL_TERMINAL.indexOf(t.stage || "") >= 0 });
  }));
}

function sfBindSocialBody(body, runId, t, d) {
  sfBindSocialRetry(body, runId, t);

  // 展开态：先登记，轮询重绘后按 SF_SOCIAL.open 还原
  $$("details[data-item]", body).forEach(det => {
    det.addEventListener("toggle", () => {
      if (det.open) SF_SOCIAL.open[det.dataset.item] = true;
      else delete SF_SOCIAL.open[det.dataset.item];
    });
  });

  // 来源筛选（多来源时）
  $$("#sf-src-filter .sf-chip", body).forEach(chip => chip.addEventListener("click", () => {
    SF_SOCIAL.src = chip.dataset.src || "";
    $$("#sf-src-filter .sf-chip", body).forEach(c => c.classList.toggle("active", c === chip));
    $$(".sf-social-item", body).forEach(row => {
      row.hidden = !!SF_SOCIAL.src && row.dataset.platform !== SF_SOCIAL.src;
    });
  }));
  if (SF_SOCIAL.src) {
    $$(".sf-social-item", body).forEach(row => { row.hidden = row.dataset.platform !== SF_SOCIAL.src; });
  }

  // 任务动作
  $$("[data-social-action]", body).forEach(btn => btn.addEventListener("click", () => {
    sfSocialAction(runId, btn.dataset.socialAction, btn);
  }));

  // 导出报告：社交报告若后端未实现 → 如实提示，绝不假装成功
  $$("[data-social-export]", body).forEach(btn => btn.addEventListener("click", () => {
    sfExportSocialReport(runId);
  }));

  // 评论入口
  $$("[data-comments]", body).forEach(btn => btn.addEventListener("click", () => {
    sfLoadSocialComments(btn, runId);
  }));

  // 证据弹窗
  const bySnapshot = {};
  (d.items || []).forEach(it => { if (it.snapshot_id) bySnapshot[it.snapshot_id] = it; });
  const ctx = { bySnapshot: bySnapshot, platformLabel: (t.task_type === "xhs_career" ? "小红书" : "X") };
  $$(".sf-social-finding button[data-finding]", body).forEach(btn => {
    btn.addEventListener("click", async () => {
      const fid = btn.dataset.finding;
      const r = await api(`/api/social/findings/${encodeURIComponent(fid)}/evidence`);
      const evs = (r.ok && r.data && r.data.evidence) || [];
      showSocialEvidence(evs, ctx);
    });
  });
}

async function sfLoadSocialComments(btn, runId) {
  const itemId = btn.dataset.comments;
  const host = btn.nextElementSibling;
  if (!host) return;
  host.textContent = "正在读取已保存评论…";
  try {
    const r = await api(`/api/social/items/${encodeURIComponent(itemId)}/comments?run_id=${encodeURIComponent(runId)}`);
    if (!r.ok) throw new Error("comments failed");
    const rows = (r.data && r.data.comments) || [];
    const html = sfCommentsHtml(rows);
    SF_SOCIAL.comments[itemId] = html;              // 缓存：轮询重绘后不丢
    host.innerHTML = html;
    host.dataset.loaded = "1";
  } catch (_) {
    host.innerHTML = `<div class="sf-note">评论读取失败（可重试）。这不代表平台没有评论。</div>`;
  }
}

function sfCommentsHtml(rows) {
  const list = Array.isArray(rows) ? rows : [];
  if (!list.length) {
    return `<div class="sf-note">本任务尚未保存该条内容的评论；可能是读取失败或上游未返回，不代表平台没有评论。</div>`;
  }
  const rootCount = list.filter(c => !c.parent_source_id).length;
  const head = `<div class="sf-note">${esc(sfCommentCoverageText(rootCount, list.length))}</div>`;
  return head + list.map(c => `<div class="sf-evi">
      <span class="sf-note">${esc(c.author_key || "匿名")}${c.parent_missing ? " · 回复（上级关系未确认）" : ""}${c.identity_quality === "synthetic" ? " · 合成键" : ""}${c.published_at ? " · " + esc(fmtTime(c.published_at)) : ""}</span>
      <div class="sf-evi-quote">${esc(c.evidence_text || "")}</div>
    </div>`).join("");
}

/* ============================ 证据弹窗 ============================ */

function showSocialEvidence(evs, ctx) {
  ctx = ctx || {};
  const root = $("#modal-root");
  if (!root) return;
  const list = Array.isArray(evs) ? evs : [];
  root.innerHTML = `<div class="sf-modal-mask"><div class="sf-modal">
    <div class="sf-modal-head"><b>结论依据</b><button class="sf-link" id="sf-evi-close">关闭</button></div>
    <div class="sf-modal-body">
      ${list.length ? list.map(e => sfEvidenceCard(e, ctx)).join("") : `<div class="sf-note">本结论暂无可用引用。</div>`}
    </div>
  </div></div>`;
  $("#sf-evi-close").addEventListener("click", () => { root.innerHTML = ""; });
  root.querySelector(".sf-modal-mask").addEventListener("click", ev => {
    if (ev.target === root.querySelector(".sf-modal-mask")) root.innerHTML = "";
  });
}

function sfEvidenceCard(e, ctx) {
  const stanceLabel = { support: "支持", contradict: "反例", context: "背景" };
  const isComment = !!e.comment_snapshot_id;
  const kindLabel = isComment ? "评论" : "原帖";
  const item = (ctx.bySnapshot && e.snapshot_id) ? ctx.bySnapshot[e.snapshot_id] : null;
  const platform = item ? sfPlatformLabel(item.platform) : (ctx.platformLabel || "");
  const when = (item && item.fetched_at) ? fmtTime(item.fetched_at) : "";
  const seg = sfEvidenceContext(e.source_text, e.start, e.end, e.quote);
  const synthetic = (isComment && e.identity_quality === "synthetic");
  const hasCtx = !!(seg.before || seg.after);
  const snapshotId = e.snapshot_id || e.comment_snapshot_id || "—";
  return `<div class="sf-evi">
    <div class="sf-evi-head">
      <span class="sf-badge">${esc(stanceLabel[e.stance] || e.stance || "引用")}</span>
      <span class="sf-note">${esc(kindLabel)}${platform ? " · " + esc(platform) : ""}${when ? " · 采集于 " + esc(when) : ""}</span>
    </div>
    <div class="sf-evi-quote">「${esc(e.quote || seg.mark || "")}」</div>
    ${hasCtx ? `<div class="sf-evi-ctx">正文上下文：…${esc(seg.before)}<mark>${esc(seg.mark)}</mark>${esc(seg.after)}…</div>`
             : `<div class="sf-note">本次未保留可展示的正文上下文。</div>`}
    <details class="sf-evi-diag"><summary class="sf-note">技术信息</summary>
      <div class="sf-note mono">类型：${esc(kindLabel)} · 快照：${esc(snapshotId)}${synthetic ? " · 合成键（平台未提供稳定评论 ID）" : ""}</div>
    </details>
  </div>`;
}

/* ============================ 任务动作 ============================ */

async function sfSocialAction(runId, id, btn) {
  const meta = SF_SOCIAL_ACTION[id];
  if (!meta) return;
  const path = id === "retry-organize" ? "retry-organize" : id;
  if (btn) btn.disabled = true;
  const r = await api(`/api/tasks/${encodeURIComponent(runId)}/${path}`, { method: "POST", body: {} });
  if (btn) btn.disabled = false;
  if (!r.ok) {
    if (r.error && r.error.code === "UNSUPPORTED_CAPABILITY") {
      toast("社交任务暂不支持这个动作；已保存的材料不受影响。", "err");
      return;
    }
    toast("操作未完成，请稍后重试", "err");
    return;
  }
  SF_SOCIAL.sig = null;
  if (typeof sfRunTask === "function") sfRunTask(runId);
  else sfSocialRenderResult(runId, window.__SF_CURRENT_TASK || {}, {});
}

async function sfExportSocialReport(runId) {
  const r = await api(`/api/tasks/${encodeURIComponent(runId)}/report`, { method: "POST", body: { formats: ["md"] } });
  if (!r.ok) {
    if (r.error && r.error.code === "UNSUPPORTED_CAPABILITY") {
      toast("社交结果报告导出暂未支持；已保存的材料不受影响。", "err");
      return;
    }
    toast("导出未完成，请稍后重试", "err");
    return;
  }
  const id = (r.data && (r.data.report_id || (r.data.report && r.data.report.id)));
  if (!id) { toast("报告已生成，可在诊断页的报告中心查看", "ok"); return; }
  window.open(`/api/reports/${encodeURIComponent(id)}/file?fmt=md`, "_blank");
}

window.SocialView = {
  renderTask: sfRenderSocial,
  /* 供契约测试直接调用（执行级断言，不是源码正则） */
  _test: {
    renderResult: sfSocialRenderResult,
    progressHtml: sfSocialProgressHtml,
    loadFailedHtml: sfSocialLoadFailed,
    emptyNote: sfSocialEmptyNote,
    commentCoverageText: sfCommentCoverageText,
    commentsHtml: sfCommentsHtml,
    evidenceContext: sfEvidenceContext,
    evidenceCard: sfEvidenceCard,
    aiNote: sfAiNote,
    state: SF_SOCIAL,
  },
};
