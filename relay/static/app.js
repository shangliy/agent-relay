"use strict";
// CSP: script-src 'self' — no inline handlers. Clicks go through data-act / data-go delegation.
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const rp = m => (m / 1000).toLocaleString(undefined, {maximumFractionDigits: 3}) + " RP";
const num = n => Number(n).toLocaleString();
const ago = t => {
  if (!t) return "";
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return "刚刚";
  if (s < 3600) return Math.floor(s / 60) + " 分钟前";
  if (s < 86400) return Math.floor(s / 3600) + " 小时前";
  return new Date(t * 1000).toLocaleDateString();
};
const INSTALL = 'pip install "git+https://github.com/shangliy/agent-relay"';
const STATUS = {open: ["待接单", ""], claimed: ["执行中", "ok"], stalled: ["失联待处理", "warn"], done: ["已完成", "ok"],
                failed: ["失败·已按用量结算", "warn"], cancelled: ["已取消", ""]};
const RUN_STATE = {assigned: "已派单，等待电脑启动", running: "电脑正在执行", uploading: "上传结果中", validating: "检查中",
                   settled: "已结算", expired: "电脑失联，租约过期", failed: "失败"};
const CAT = {
  blog: ["Blog 文章", "标题、正文、摘要、待核实清单"],
  research: ["研究简报", "基于材料的结论 + 来源 + 分歧"],
  paper_idea: ["论文想法", "可证伪假设、基线、最小实验"],
  paper_review: ["论文评审", "仅限公开论文的结构化评审"],
  product: ["产品优化", "诊断、优先级、实验与指标"],
  video: ["视频策划", "分镜、口播稿、字幕文本"],
};
const LOGIN_ERRORS = {INVITE_REQUIRED: "首次加入需要有效的邀请码。", STATE_MISMATCH: "登录已过期，请重试。", OAUTH_FAILED: "GitHub 登录失败，请重试。"};

let meta = null, me = null, timer = null, pairWatch = null;
const draft = {category: "blog"};

// ------------------------------------------------------------------ plumbing

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "show" + (err ? " err" : "");
  clearTimeout(t._h); t._h = setTimeout(() => t.className = "", err ? 5000 : 2500);
}

const FRIENDLY = {
  NO_DEVICE: "还没有连接电脑。去「我的 → 设备」连接一台电脑。",
  DEVICE_NOT_READY: "你的电脑不在线。请在电脑上运行 agent-relay-worker run。",
  CAPABILITY_MISMATCH: "你在线的电脑没有这个任务需要的执行端或模型。",
  TASK_ALREADY_CLAIMED: "手慢了，这个任务已被别人接走。",
  INSUFFICIENT_CREDIT: "可用积分不足。先帮别人完成任务赚取积分，或降低预算。",
};

async function api(method, path, body) {
  const r = await fetch(path, {method, credentials: "same-origin", headers: {"Content-Type": "application/json"},
                               body: body === undefined ? undefined : JSON.stringify(body)});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = data.error || {code: String(r.status), message: r.statusText};
    if (r.status === 401) { me = null; if (path !== "/v2/me") route(); }  // /v2/me 401 is handled by route()
    else toast(FRIENDLY[e.code] || `${e.message}${e.hint ? "（" + e.hint + "）" : ""}`, true);
    throw e;
  }
  return data;
}

async function loadMe() {
  me = await api("GET", "/v2/me");
  const a = me.account;
  const bal = $("#bal");
  bal.textContent = `可用 ${rp(a.available_milli)}`;
  bal.classList.remove("hidden");
  return me;
}

const online = () => me.devices.filter(d => d.online);

// Mirrors the server's capability rule so the lobby can show "can I take this?"
function canRun(task) {
  for (const d of online()) {
    const info = (d.capabilities.adapters || {})[task.adapter];
    if (info && info.usage === "known" && ((info.models || []).includes("*") || (info.models || []).includes(task.model))) return true;
  }
  return false;
}

function setView(html) { $("#view").innerHTML = html; window.scrollTo(0, 0); }

function every(ms, fn) { clearInterval(timer); timer = setInterval(() => { if (!document.hidden) fn().catch(() => {}); }, ms); }

// ------------------------------------------------------------------ router

async function route() {
  clearInterval(timer);
  if (!me) {
    try { await loadMe(); } catch { return renderLogin(); }
  }
  $("#tabs").classList.remove("hidden");
  const [name, arg] = (location.hash.slice(1) || "home").split("/");
  document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.go === (name === "task" ? "mine" : name)));
  const views = {home: renderHome, lobby: renderLobby, publish: renderPublish, mine: renderMine, community: renderCommunity, task: renderTask};
  await (views[name] || renderHome)(arg);
}
window.addEventListener("hashchange", route);
const go = h => { if (location.hash === "#" + h) route(); else location.hash = h; };

// ------------------------------------------------------------------ login

function renderLogin() {
  $("#tabs").classList.add("hidden");
  $("#bal").classList.add("hidden");
  setView(`
    <h2>网页发布任务，手机接单，电脑执行</h2>
    <div class="card">
      <p style="margin-top:0">把任务和积分预算固定下来发布；别人用<b>自己电脑上</b>的 Agent 帮你完成，按实际 token 用量结算积分。你也可以用手机接别人的任务赚积分。积分<b>不能提现</b>，只用来请求帮助。</p>
      <ul class="muted" style="padding-left:20px">
        <li>任务输入和结果都是<b>公开</b>的，不要提交私人凭据、保密审稿材料或未授权数据。</li>
        <li>执行者能看到任务明文，也能修改自己的客户端；这不是保密计算。</li>
        <li>只能使用你被允许用于他人任务的模型资源（例如自己的 API key）。</li>
        <li>邀请制 Alpha 试点：可能有故障、规则调整或数据重置。</li>
      </ul>
      <label class="check"><input type="checkbox" id="ack"> <span>我已阅读并同意以上说明</span></label>
      ${meta.login.github ? `
        ${meta.login.invite_only ? `<label class="f">邀请码（首次加入需要，老用户可留空）</label><input id="invite" placeholder="XXXXX-XXXXX" autocomplete="off" autocapitalize="characters">` : ""}
        <p><button class="block" data-act="github">用 GitHub 登录</button></p>` : ""}
      ${meta.login.dev ? `
        <label class="f">开发登录（仅本地环境）</label>
        <div class="row"><input id="handle" class="grow" placeholder="用户名，例如 alice" autocapitalize="none"><button data-act="devlogin">进入</button></div>` : ""}
      ${!meta.login.github && !meta.login.dev ? `<p class="muted">服务器尚未配置登录方式。</p>` : ""}
    </div>
    <p class="muted center small">开源：<a href="https://github.com/shangliy/agent-relay" rel="noopener" target="_blank">github.com/shangliy/agent-relay</a></p>`);
}

// ------------------------------------------------------------------ home

async function renderHome() {
  await loadMe();
  const a = me.account;
  const hasDevice = me.devices.some(d => !d.revoked);
  const isOnline = online().length > 0;
  const worked = me.working.some(r => r.state === "settled");
  const published = me.published.length > 0;
  const activePub = me.published.filter(t => ["open", "claimed", "stalled"].includes(t.status));
  const activeWork = me.working.filter(r => ["assigned", "running", "uploading", "validating"].includes(r.state));
  const step = (done, n, title, sub, action) => `<li class="${done ? "done" : ""}"><span class="n">${done ? "✓" : n}</span>
      <div class="grow"><b>${title}</b><div class="muted small">${sub}</div></div>${done ? "" : action}</li>`;
  setView(`
    <div class="card">
      <div class="muted small">你好，${esc(a.handle)}</div>
      <div class="row" style="align-items:flex-end"><div class="grow"><div class="big">${rp(a.available_milli)}</div><div class="muted small">可用于发布 · 余额 ${rp(a.balance_milli)} · 预留 ${rp(a.reserved_milli)} · 信用额度 ${rp(a.credit_milli)}</div></div></div>
      ${a.balance_milli < 0 ? `<p class="small" style="color:var(--warn)">余额为负，暂时不能发布。帮别人完成任务赚回积分即可。</p>` : ""}
      <div class="row" style="margin-top:12px"><button class="grow" data-go="publish">发布任务</button><button class="grow ghost" data-go="lobby">去接单</button></div>
    </div>
    ${!(worked && published) ? `<h3>三步上手</h3><div class="card"><ol class="steps">
      ${step(hasDevice && isOnline, 1, "连接你的电脑", hasDevice ? (isOnline ? "已在线" : "电脑离线：在电脑上运行 agent-relay-worker run") : "接单需要一台运行客户端的电脑", `<button class="ghost" data-go="mine/devices">连接</button>`)}
      ${step(worked, 2, "用手机接第一单", "电脑自动执行、检查、回传，积分到账", `<button class="ghost" data-go="lobby">看看</button>`)}
      ${step(published, 3, "发布你的第一个任务", "新账户有 20 RP 信用额度", `<button class="ghost" data-go="publish">发布</button>`)}
    </ol></div>` : ""}
    ${activeWork.length ? `<h3>我的电脑正在做</h3>${activeWork.map(r => `<div class="card row"><span class="pulse"></span><div class="grow"><b>${esc(r.title)}</b><div class="muted small">${esc(RUN_STATE[r.state] || r.state)}</div></div><button class="ghost" data-go="task/${esc(r.task_id)}">查看</button></div>`).join("")}` : ""}
    ${activePub.length ? `<h3>我发布的进行中任务</h3>${activePub.map(t => taskCard(t)).join("")}` : ""}
  `);
  if (activeWork.length || activePub.length) every(8000, renderHome);
}

// ------------------------------------------------------------------ lobby

let lobbyAll = false;

function taskCard(t, {claim = false} = {}) {
  const [label, cls] = STATUS[t.status] || [t.status, ""];
  const mine = me && t.publisher_id === me.account.id;
  const able = claim && !mine && canRun(t);
  return `<div class="card">
    <div class="row"><span class="tag">${esc((CAT[t.category] || [t.category])[0])}</span><span class="tag ${cls}">${esc(label)}</span><span class="muted small" style="margin-left:auto">${ago(t.created_at)}</span></div>
    <div style="margin:8px 0 2px"><b>${esc(t.title)}</b></div>
    <div class="muted small">最多 ${rp(t.budget_milli)} · ${esc(t.adapter)} / ${esc(t.model)}</div>
    <div class="row" style="margin-top:10px">
      <button class="ghost grow" data-go="task/${esc(t.id)}">详情</button>
      ${claim && !mine ? `<button class="grow" data-act="claim" data-id="${esc(t.id)}" ${able ? "" : "disabled"}>接单</button>` : ""}
    </div>
    ${claim && !mine && !able ? `<div class="muted small" style="margin-top:6px">${online().length ? "你在线的电脑不支持这个执行端/模型" : "电脑不在线，无法接单"}</div>` : ""}
  </div>`;
}

async function renderLobby(before) {
  await loadMe();
  const d = await api("GET", "/v2/tasks?status=open&limit=30" + (before ? "&before=" + encodeURIComponent(before) : ""));
  const others = d.tasks.filter(t => t.publisher_id !== me.account.id);
  const list = lobbyAll ? others : others.filter(canRun);
  const devs = online();
  setView(`
    <div class="row"><h2 class="grow">接单</h2><button class="ghost" data-act="refresh">刷新</button></div>
    <div class="card row small">${devs.length
      ? `<span class="pulse"></span><span class="grow">${devs.length} 台电脑在线：${esc(devs.map(x => x.name).join("、"))}。接单后自动派给合适的电脑。</span>`
      : `<span class="grow">你的电脑不在线。接单需要电脑运行客户端。</span><button class="ghost" data-go="mine/devices">连接电脑</button>`}</div>
    <div class="seg"><button class="${lobbyAll ? "" : "on"}" data-act="lobbyMine">我能接的</button><button class="${lobbyAll ? "on" : ""}" data-act="lobbyAll">全部待接（${others.length}）</button></div>
    ${list.map(t => taskCard(t, {claim: true})).join("") || `<div class="empty">${lobbyAll || !others.length ? "暂时没有待接任务。" : "没有你的电脑能接的任务。<br>切到「全部待接」看看需要什么执行端。"}</div>`}
    ${d.next_cursor ? `<button class="ghost block" data-act="page" data-id="${esc(d.next_cursor)}">加载更多</button>` : ""}
  `);
  every(15000, () => renderLobby(before));
}

// ------------------------------------------------------------------ publish

async function renderPublish() {
  await loadMe();
  const supply = (await api("GET", "/v2/supply")).options.filter(o => meta.test_adapters || o.adapter !== "echo");
  const tpl = meta.templates.find(t => t.category === draft.category);
  const opts = supply.map(o => `<option value="${esc(o.adapter + "|" + o.model)}">${esc(o.model)} · ${esc(o.adapter)}（${o.devices} 台在线）</option>`).join("");
  const a = me.account;
  setView(`
    <h2>发布任务</h2>
    ${a.balance_milli < 0 ? `<div class="card small" style="color:var(--warn)">余额为负，暂时不能发布。先接单赚回积分。</div>` : ""}
    <div class="card">
      <label class="f" style="margin-top:0">1. 任务类型</label>
      <div class="grid">${meta.templates.map(t => `<button class="pick ${t.category === draft.category ? "on" : ""}" data-act="cat" data-id="${esc(t.category)}"><b>${esc((CAT[t.category] || [t.label])[0])}</b><span>${esc((CAT[t.category] || ["", ""])[1])}</span></button>`).join("")}</div>

      <label class="f">2. 标题</label>
      <input id="p_title" maxlength="120" placeholder="一句话说明你要什么" value="${esc(draft.title || "")}">
      <label class="f">材料（公开）</label>
      <textarea id="p_input" placeholder="粘贴你有权公开的材料：文章、笔记、论文摘要、产品数据……">${esc(draft.input || "")}</textarea>

      <label class="f">3. 由谁的模型执行</label>
      ${supply.length ? `<select id="p_model">${opts}<option value="custom">其他（手动填写）</option></select>`
                      : `<div class="muted small">现在没有在线电脑。你仍可发布，填写需要的执行端和模型，等有人上线接单。</div>`}
      <div id="custom" class="row ${supply.length ? "hidden" : ""}" style="margin-top:8px">
        <select id="p_adapter" class="grow">${["claude_code", "codex", "http"].concat(meta.test_adapters ? ["echo"] : []).map(x => `<option>${x}</option>`).join("")}</select>
        <input id="p_custom_model" class="grow" placeholder="模型，例如 claude-sonnet-5-5">
      </div>

      <label class="f">4. 积分预算上限：<span id="b_val">${draft.budget || 5}</span> RP</label>
      <input id="p_budget" type="range" min="1" max="${Math.max(1, Math.min(100, Math.floor(a.available_milli / 1000)))}" value="${Math.min(draft.budget || 5, Math.max(1, Math.floor(a.available_milli / 1000)))}">
      <div class="muted small" id="b_hint"></div>

      <details><summary>高级：修改 prompt 与交付要求</summary>
        <label class="f">Prompt（发布后固定，接单者无法修改）</label><textarea id="p_prompt">${esc(tpl.prompt)}</textarea>
        <label class="f">交付要求</label><input id="p_deliv" value="${esc(tpl.deliverable)}">
        <div class="muted small" style="margin-top:8px">自动检查：<span id="p_check">${esc(tpl.validator.id)}</span>（格式通过 ≠ 内容正确）</div>
      </details>

      <label class="check"><input type="checkbox" id="p_consent"> <span>我有权公开以上材料和结果，其中不含私人凭据或保密内容。</span></label>
      <div class="muted small">按实际用量结算：输入 token × 1 + 输出 token × 3 milli RP，不超过上限，剩余退回。失败但用量已知照常结算；用量未知按 0 结算。</div>
    </div>
    <div class="sticky-cta"><button class="block" data-act="publish" ${a.balance_milli < 0 ? "disabled" : ""}>发布并预留积分</button></div>
  `);
  budgetHint();
}

function budgetHint() {
  const b = +$("#p_budget").value;
  $("#b_val").textContent = b;
  $("#b_hint").textContent = `上限 ${b} RP ≈ 例如 ${num(b * 500)} 输入 + ${num(Math.floor(b * 500 / 3))} 输出 token。`;
}

function saveDraft() {
  if (!$("#p_title")) return;
  draft.title = $("#p_title").value; draft.input = $("#p_input").value; draft.budget = +$("#p_budget").value;
}

// ------------------------------------------------------------------ mine

let mineSeg = "published";

async function renderMine(seg) {
  if (seg) mineSeg = seg;
  await loadMe();
  const segs = [["published", "我发布的"], ["working", "我执行的"], ["devices", "设备"], ["ledger", "积分流水"], ["profile", "名片"]];
  let body = "";
  if (mineSeg === "published") {
    body = me.published.map(t => taskCard(t)).join("") || `<div class="empty">还没有发布任务。<br><br><button data-go="publish">发布第一个任务</button></div>`;
  } else if (mineSeg === "working") {
    body = me.working.map(r => `<div class="card row"><div class="grow"><b>${esc(r.title)}</b><div class="muted small">${esc(RUN_STATE[r.state] || r.state)}${r.outcome ? " · " + (r.outcome === "succeeded" ? "成功" : "失败") : ""}${r.fee_milli != null ? " · +" + rp(r.fee_milli) : ""}</div></div><button class="ghost" data-go="task/${esc(r.task_id)}">查看</button></div>`).join("")
      || `<div class="empty">还没有执行过任务。<br><br><button data-go="lobby">去接单</button></div>`;
  } else if (mineSeg === "devices") {
    body = renderDevices();
  } else if (mineSeg === "ledger") {
    body = `<div class="card">${me.ledger.map(l => `<div class="row" style="padding:6px 0;border-bottom:1px solid var(--line)"><span class="grow small">${l.kind === "settle_credit" ? "完成任务收入" : "任务支出"} · ${ago(l.created_at)}</span><b style="color:${l.amount_milli > 0 ? "var(--ok)" : "var(--ink)"}">${l.amount_milli > 0 ? "+" : ""}${rp(l.amount_milli)}</b></div>`).join("") || '<span class="muted">暂无流水</span>'}</div>`;
  } else {
    const p = me.profile;
    body = `<div class="card">
      <p class="muted small" style="margin-top:0">累计收入进入前三、且开启公开时，首页榜单会展示你的作品。</p>
      <label class="f">展示名称</label><input id="f_name" maxlength="40" value="${esc(p.display_name)}">
      <label class="f">简介（≤180 字）</label><textarea id="f_bio" maxlength="180">${esc(p.bio)}</textarea>
      <label class="f">作品名称</label><input id="f_wt" maxlength="80" value="${esc(p.work_title)}">
      <label class="f">作品链接（https）</label><input id="f_wu" type="url" value="${esc(p.work_url)}" placeholder="https://">
      <label class="check"><input type="checkbox" id="f_pub" ${p.public ? "checked" : ""}> <span>公开名片</span></label>
      <button class="block" data-act="profile">保存</button></div>
      <p class="center"><button class="ghost" data-act="logout">退出登录</button></p>`;
  }
  setView(`<h2>我的</h2><div class="seg">${segs.map(([k, v]) => `<button class="${k === mineSeg ? "on" : ""}" data-go="mine/${k}">${v}</button>`).join("")}</div>${body}`);
  if (mineSeg === "devices" && pairWatch) showPairCmd();
}

function renderDevices() {
  const list = me.devices.filter(d => !d.revoked).map(d => {
    const engines = Object.entries(d.capabilities.adapters || {}).map(([k, v]) => `${k}: ${(v.models || []).join(", ")}`);
    return `<div class="card"><div class="row"><b class="grow">${esc(d.name)}</b><span class="tag ${d.online ? "ok" : ""}">${d.online ? "在线" : "离线"}</span></div>
      <div class="muted small">${engines.length ? esc(engines.join(" · ")) : "没有启用执行端（在电脑上运行 agent-relay-worker doctor 查看）"}</div>
      <div class="row" style="margin-top:8px"><span class="muted small grow">最后活跃 ${ago(d.last_seen_at)}</span><button class="danger" data-act="revoke" data-id="${esc(d.id)}">移除</button></div></div>`;
  }).join("");
  return `<div class="card">
      <b>连接一台电脑</b>
      <p class="muted small">手机只负责接单；推理在你的电脑上运行，模型密钥留在电脑上。需要 Python 3.10+。</p>
      <button class="block" data-act="paircode">生成连接命令</button>
      <div id="paircmd"></div>
    </div>${list}`;
}

function showPairCmd() {
  const el = $("#paircmd");
  if (!el || !pairWatch) return;
  const left = Math.max(0, Math.round((pairWatch.expires - Date.now()) / 1000));
  el.innerHTML = pairWatch.done
    ? `<p style="color:var(--ok)"><b>✓ 已连接 ${esc(pairWatch.done)}</b>。保持电脑上的窗口打开，就可以用手机接单了。</p>`
    : `<p class="small" style="margin-bottom:4px">在电脑终端里粘贴运行（${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} 内有效）：</p>
       <pre id="cmdtext">${esc(pairWatch.cmd)}</pre>
       <div class="row"><button class="ghost grow" data-act="copy">复制命令</button></div>
       <p class="muted small"><span class="pulse"></span>等待电脑连接…　启用模型：设置 <code>ANTHROPIC_API_KEY</code>（Claude Code）或运行 <code>agent-relay-worker doctor</code> 查看。</p>`;
}

// ------------------------------------------------------------------ task detail

async function renderTask(id) {
  const t = await api("GET", "/v2/tasks/" + encodeURIComponent(id));
  await loadMe();
  const [label, cls] = STATUS[t.status] || [t.status, ""];
  const mine = t.publisher_id === me.account.id;
  const runs = t.runs.slice().reverse().map(r => {
    const active = ["assigned", "running", "uploading", "validating"].includes(r.state);
    const v = r.validation;
    return `<div class="card">
      <div class="row">${active ? '<span class="pulse"></span>' : ""}<b class="grow">第 ${r.attempt} 次执行 · ${esc(RUN_STATE[r.state] || r.state)}</b>${r.outcome ? `<span class="tag ${r.outcome === "succeeded" ? "ok" : "warn"}">${r.outcome === "succeeded" ? "成功" : "失败"}</span>` : ""}</div>
      ${r.state === "settled" ? `<div class="muted small" style="margin-top:6px">${r.usage_status === "known" ? `用量：输入 ${num(r.input_tokens)} · 输出 ${num(r.output_tokens)} token` : "用量未知（按 0 结算）"} · 结算 ${rp(r.fee_milli)}${r.error_code ? " · " + esc(r.error_code) : ""}</div>` : ""}
      ${v ? `<div class="muted small">格式检查：${v.passed ? "通过" : "未通过"}${v.passed ? "" : " — " + esc(v.checks.filter(c => !c.passed).map(c => c.detail || c.name).join("; "))}（格式通过不代表内容正确）</div>` : ""}
      ${active && (mine || r.worker_id === me.account.id) ? `<button class="danger" style="margin-top:8px" data-act="stop" data-id="${esc(r.id)}" data-task="${esc(id)}">请求停止</button>` : ""}
      ${r.output_text ? `<pre>${esc(r.output_text)}</pre><button class="ghost" data-act="copyout" data-id="${esc(r.id)}">复制结果</button>` : ""}
    </div>`;
  }).join("");
  const canClaim = t.status === "open" && !mine;
  window._outputs = Object.fromEntries(t.runs.map(r => [r.id, r.output_text || ""]));
  setView(`
    <button class="ghost" data-act="back">‹ 返回</button>
    <div class="card">
      <div class="row"><span class="tag">${esc((CAT[t.category] || [t.category])[0])}</span><span class="tag ${cls}">${esc(label)}</span>${mine ? '<span class="tag">我发布的</span>' : ""}</div>
      <h2 style="margin:10px 0 4px">${esc(t.title)}</h2>
      <div class="muted small">预算上限 ${rp(t.budget_milli)} · ${esc(t.adapter)} / ${esc(t.model)} · ${ago(t.created_at)}</div>
      ${canClaim ? `<p><button class="block" data-act="claim" data-id="${esc(t.id)}" ${canRun(t) ? "" : "disabled"}>接单</button></p>${canRun(t) ? "" : `<div class="muted small">${online().length ? "你在线的电脑不支持这个执行端/模型" : "电脑不在线，无法接单"}</div>`}` : ""}
      ${mine && ["open", "stalled"].includes(t.status) ? `<div class="row" style="margin-top:10px">${t.status === "stalled" ? `<button class="grow" data-act="reopen" data-id="${esc(t.id)}">重新开放</button>` : ""}<button class="danger grow" data-act="cancel" data-id="${esc(t.id)}">取消并退回预算</button></div>` : ""}
      ${t.status === "stalled" && mine ? `<p class="muted small">执行电脑失联。重新开放会让别人再次接单，旧运行可能仍在消耗，可能产生重复费用。</p>` : ""}
    </div>
    ${runs || (t.status === "open" ? `<div class="empty">等待有人接单…</div>` : "")}
    <details class="card"><summary>任务内容（固定，spec ${esc(t.spec_hash.slice(0, 12))}）</summary>
      <label class="f">Prompt</label><pre>${esc(t.spec.prompt)}</pre>
      ${t.spec.input_manifest.map(i => `<label class="f">材料：${esc(i.name)}</label><pre>${esc(i.text)}</pre>`).join("")}
      <label class="f">交付要求</label><pre>${esc(t.spec.deliverable)}</pre>
    </details>
  `);
  if (["open", "claimed"].includes(t.status)) every(5000, () => renderTask(id));
}

// ------------------------------------------------------------------ community

async function renderCommunity() {
  const c = await api("GET", "/v2/community");
  setView(`
    <h2>贡献榜</h2>
    <p class="muted small">按累计赚取积分排名；前三且公开名片者展示作品。消费不影响历史排名；排名不代表平台背书。</p>
    ${c.leaderboard.map(e => `<div class="card">
      <div class="row"><span class="big" style="font-size:22px;width:44px">${e.rank <= 3 ? ["🥇", "🥈", "🥉"][e.rank - 1] : "#" + e.rank}</span>
        <div class="grow"><b>${esc(e.name)}</b><div class="muted small">累计 ${rp(e.earned_milli)} · ${e.tasks} 个任务</div></div></div>
      ${e.showcase ? `<div style="margin-top:8px">${esc(e.showcase.bio)}</div>
        <div class="row" style="margin-top:6px">${e.showcase.work_url ? `<a class="grow" href="${esc(e.showcase.work_url)}" rel="noopener nofollow ugc" target="_blank">${esc(e.showcase.work_title || e.showcase.work_url)} ↗</a>` : '<span class="grow"></span>'}
        ${e.account_id !== me.account.id ? `<button class="ghost small" data-act="report" data-id="${esc(e.account_id)}">举报</button>` : ""}</div>` : ""}
    </div>`).join("") || `<div class="empty">还没有结算记录。完成第一单就能上榜。</div>`}
    <p class="center"><button class="ghost" data-go="mine/profile">编辑我的名片</button></p>
  `);
}

// ------------------------------------------------------------------ actions

const ACTIONS = {
  github() {
    if (!$("#ack").checked) return toast("请先阅读并勾选说明", true);
    const inv = $("#invite") ? $("#invite").value.trim() : "";
    location.href = "/v2/auth/github/start?invite=" + encodeURIComponent(inv);
  },
  async devlogin() {
    if (!$("#ack").checked) return toast("请先阅读并勾选说明", true);
    await api("POST", "/v2/auth/dev-login", {handle: $("#handle").value});
    me = null; go("home");
  },
  async logout() { await api("POST", "/v2/auth/logout"); me = null; location.hash = ""; route(); },
  refresh: () => route(),
  back() { if (history.length > 1) history.back(); else go("home"); },
  page: el => renderLobby(el.dataset.id),
  lobbyMine() { lobbyAll = false; renderLobby(); },
  lobbyAll() { lobbyAll = true; renderLobby(); },
  async claim(el) {
    el.disabled = true;
    try {
      const r = await api("POST", `/v2/tasks/${encodeURIComponent(el.dataset.id)}/claim`, {});
      toast(`已接单，派给「${r.device_name}」执行`);
      go("task/" + el.dataset.id);
    } catch { el.disabled = false; }
  },
  async cancel(el) {
    if (!confirm("取消任务并退回预留积分？")) return;
    await api("POST", `/v2/tasks/${encodeURIComponent(el.dataset.id)}/cancel`); toast("已取消，预算已退回"); route();
  },
  async reopen(el) {
    if (!confirm("旧运行可能仍在供应商端执行，重新开放可能产生重复费用。确认？")) return;
    await api("POST", `/v2/tasks/${encodeURIComponent(el.dataset.id)}/reopen`, {acknowledge_duplicate_cost: true}); route();
  },
  async stop(el) {
    if (!confirm("请求停止？已经发生的用量仍会结算。")) return;
    await api("POST", `/v2/runs/${encodeURIComponent(el.dataset.id)}/stop`); toast("已发送停止请求"); route();
  },
  cat(el) {
    // Update in place: re-rendering would race with typing and wipe the form.
    draft.category = el.dataset.id;
    const tpl = meta.templates.find(t => t.category === draft.category);
    document.querySelectorAll("[data-act=cat]").forEach(b => b.classList.toggle("on", b === el));
    $("#p_prompt").value = tpl.prompt; $("#p_deliv").value = tpl.deliverable;
    $("#p_check").textContent = tpl.validator.id;
  },
  async publish(el) {
    const title = $("#p_title").value.trim();
    if (!title) return toast("请填写标题", true);
    if (!$("#p_consent").checked) return toast("请确认材料可以公开", true);
    let adapter, model;
    const sel = $("#p_model");
    if (sel && sel.value !== "custom") [adapter, model] = sel.value.split("|");
    else { adapter = $("#p_adapter").value; model = $("#p_custom_model").value.trim(); }
    if (!model) return toast("请选择或填写模型", true);
    const input = $("#p_input").value.trim();
    el.disabled = true;
    try {
      const d = await api("POST", "/v2/tasks", {
        category: draft.category, title, prompt: $("#p_prompt").value, deliverable: $("#p_deliv").value,
        inputs: input ? [{name: "材料", text: input}] : [], adapter, model,
        budget_milli: +$("#p_budget").value * 1000, public_consent: true,
      });
      Object.assign(draft, {title: "", input: ""});
      toast("已发布，等待接单");
      go("task/" + d.task_id);
    } catch { el.disabled = false; }
  },
  async paircode() {
    const d = await api("POST", "/v2/devices/pair-codes");
    const known = new Set(me.devices.map(x => x.id));
    pairWatch = {cmd: `${INSTALL}\nagent-relay-worker connect ${d.code} --server ${location.origin}`, expires: d.expires_at * 1000, known, done: null};
    showPairCmd();
    every(3000, async () => {
      if (!pairWatch || Date.now() > pairWatch.expires) { pairWatch = null; clearInterval(timer); return; }
      await loadMe();
      const fresh = me.devices.find(x => !pairWatch.known.has(x.id) && x.online);
      if (fresh) { pairWatch.done = fresh.name; clearInterval(timer); toast("电脑已连接"); renderMine("devices"); setTimeout(() => { pairWatch = null; }, 60000); }
      else showPairCmd();
    });
  },
  async copy() {
    try { await navigator.clipboard.writeText(pairWatch.cmd); toast("已复制"); }
    catch { const r = document.createRange(); r.selectNode($("#cmdtext")); getSelection().removeAllRanges(); getSelection().addRange(r); toast("请长按复制"); }
  },
  async copyout(el) {
    try { await navigator.clipboard.writeText(window._outputs[el.dataset.id]); toast("已复制结果"); } catch { toast("复制失败，请手动选择", true); }
  },
  async revoke(el) {
    if (!confirm("移除后这台电脑无法继续接单，需要重新连接。")) return;
    await api("POST", `/v2/devices/${encodeURIComponent(el.dataset.id)}/revoke`); renderMine("devices");
  },
  async report(el) {
    const reason = prompt("举报原因（例如：链接失效、垃圾推广、违规内容）");
    if (!reason) return;
    await api("POST", "/v2/reports", {target_account_id: el.dataset.id, reason});
    toast("已提交，管理员会尽快处理");
  },
  async profile() {
    await api("PUT", "/v2/me/profile", {display_name: $("#f_name").value, bio: $("#f_bio").value, work_title: $("#f_wt").value,
                                         work_url: $("#f_wu").value, public: $("#f_pub").checked});
    toast("已保存");
  },
};

document.addEventListener("click", e => {
  const nav = e.target.closest("[data-go]");
  if (nav) { saveDraft(); return go(nav.dataset.go); }
  const el = e.target.closest("[data-act]");
  if (el && ACTIONS[el.dataset.act]) { e.preventDefault(); Promise.resolve(ACTIONS[el.dataset.act](el)).catch(() => {}); }
});
document.addEventListener("input", e => { if (e.target.id === "p_budget") budgetHint(); });
document.addEventListener("change", e => {
  if (e.target.id === "p_model") $("#custom").classList.toggle("hidden", e.target.value !== "custom");
});

(async function boot() {
  meta = await api("GET", "/v2/meta");
  const params = new URLSearchParams(location.search);
  if (params.get("login_error")) {
    toast(LOGIN_ERRORS[params.get("login_error")] || "登录失败：" + params.get("login_error"), true);
    history.replaceState(null, "", "/");
  }
  route();
})();
