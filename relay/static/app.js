"use strict";
// CSP: script-src 'self' — no inline handlers. All clicks go through data-act delegation.
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const rp = m => (m / 1000).toLocaleString(undefined, {maximumFractionDigits: 3}) + " RP";
const when = t => t ? new Date(t * 1000).toLocaleString() : "";
const STATUS = {open: "待接单", claimed: "执行中", stalled: "失联待处理", done: "完成", failed: "失败（已按用量结算）", cancelled: "已取消"};
const LOGIN_ERRORS = {INVITE_REQUIRED: "首次加入需要有效的邀请码。", STATE_MISMATCH: "登录已过期，请重试。", OAUTH_FAILED: "GitHub 登录失败，请重试。"};
let meta = null, me = null, tab = "lobby";

async function api(method, path, body) {
  $("#err").textContent = "";
  const r = await fetch(path, {method, credentials: "same-origin", headers: {"Content-Type": "application/json"},
                               body: body === undefined ? undefined : JSON.stringify(body)});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = data.error || {code: r.status, message: r.statusText};
    if (r.status === 401) { me = null; showLogin(); }
    else $("#err").textContent = `${e.code}: ${e.message}${e.hint ? " — " + e.hint : ""}`;
    throw e;
  }
  return data;
}

function needAck() {
  if ($("#ack").checked) return true;
  $("#err").textContent = "请先阅读并勾选说明。";
  return false;
}

function showLogin() {
  $("#login").classList.remove("hidden"); $("#app").classList.add("hidden"); $("#bal").textContent = "";
  $("#gh-login").classList.toggle("hidden", !meta.login.github);
  $("#dev-login").classList.toggle("hidden", !meta.login.dev);
}

async function boot() {
  meta = await api("GET", "/v2/meta");
  const params = new URLSearchParams(location.search);
  if (params.get("login_error")) {
    $("#err").textContent = LOGIN_ERRORS[params.get("login_error")] || "登录失败：" + params.get("login_error");
    history.replaceState(null, "", "/");
  }
  try { await refresh(); } catch { return; }
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
}

async function refresh() {
  me = await api("GET", "/v2/me");
  const a = me.account;
  $("#bal").innerHTML = `${esc(a.handle)} · 余额 ${rp(a.balance_milli)} · 预留 ${rp(a.reserved_milli)} · 可用 ${rp(a.available_milli)} <button class="ghost" data-act="logout">退出</button>`;
  render();
}

function show(t) {
  tab = t;
  document.querySelectorAll("nav button").forEach(b => b.classList.toggle("on", b.dataset.tab === t));
  document.querySelectorAll("[id^=tab-]").forEach(el => el.classList.toggle("hidden", el.id !== "tab-" + t));
  $("#detail").innerHTML = "";
  render();
}

function render() {
  return ({lobby: renderLobby, publish: renderPublish, mine: renderMine, devices: renderDevices, community: renderCommunity}[tab])();
}

function taskCard(t, actions = "") {
  return `<div class="card"><div class="row"><b>${esc(t.title)}</b><span class="tag">${esc(t.category)}</span><span class="tag">${esc(STATUS[t.status] || t.status)}</span></div>
    <div class="muted">${esc(t.adapter)} / ${esc(t.model)} · 预算上限 ${rp(t.budget_milli)} · ${when(t.created_at)}</div>
    <div class="row" style="margin-top:6px"><button class="ghost" data-act="open" data-id="${esc(t.id)}">详情</button>${actions}</div></div>`;
}

async function renderLobby(before) {
  const d = await api("GET", "/v2/tasks?status=open&limit=20" + (before ? "&before=" + encodeURIComponent(before) : ""));
  const devs = me.devices.filter(x => x.online);
  const sel = devs.length ? `<select id="claimdev" style="width:auto">${devs.map(x => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join("")}</select>`
                          : `<span class="muted">没有在线设备：先在「设备」页配对并启动本地客户端。</span>`;
  $("#tab-lobby").innerHTML = `<div class="card row"><span>接单设备</span>${sel}<button class="ghost" data-act="refresh">刷新</button></div>` +
    (d.tasks.map(t => taskCard(t, t.publisher_id !== me.account.id && devs.length ? `<button data-act="claim" data-id="${esc(t.id)}">接单</button>` : "")).join("")
      || `<p class="muted">暂时没有待接任务。</p>`) +
    (d.next_cursor ? `<button class="ghost" data-act="page" data-id="${esc(d.next_cursor)}">下一页</button>` : "");
}

function renderPublish() {
  const opts = meta.templates.map(t => `<option value="${esc(t.category)}">${esc(t.label)}</option>`).join("");
  const adapters = ["claude_code", "codex", "http"].concat(meta.test_adapters ? ["echo"] : []).map(a => `<option>${a}</option>`).join("");
  $("#tab-publish").innerHTML = `<div class="card">
    <label>任务类型</label><select id="p_cat">${opts}</select>
    <label>标题</label><input id="p_title" maxlength="120">
    <label>Prompt（发布后固定，接单者无法修改）</label><textarea id="p_prompt"></textarea>
    <label>输入材料（公开）</label><textarea id="p_input" placeholder="粘贴你有权公开的材料"></textarea>
    <label>交付要求</label><input id="p_deliv">
    <div class="row"><div style="flex:1"><label>执行端</label><select id="p_adapter">${adapters}</select></div>
      <div style="flex:1"><label>模型</label><input id="p_model" placeholder="例如 claude-sonnet-5-5"></div>
      <div style="flex:1"><label>预算上限（1–100 RP）</label><input id="p_budget" type="number" min="1" max="100" value="5"></div></div>
    <p class="muted" id="p_rules"></p>
    <label><input type="checkbox" id="p_consent" style="width:auto"> 我确认有权公开以上输入与结果，且不包含私人凭据或保密材料。</label>
    <p class="muted">计费：输入 token × 1 + 输出 token × 3 milli RP，不超过预算。失败但用量已知的任务仍按规则结算；用量未知按 0 结算。</p>
    <button data-act="publish">发布并预留积分</button></div>`;
  fillTemplate();
}

function fillTemplate() {
  const t = meta.templates.find(x => x.category === $("#p_cat").value);
  $("#p_prompt").value = t.prompt; $("#p_deliv").value = t.deliverable;
  $("#p_rules").textContent = `自动检查：${t.validator.id} ${JSON.stringify(t.validator.params).slice(0, 80)}（格式通过 ≠ 内容正确）`;
}

function renderMine() {
  const pub = me.published.map(t => taskCard(t,
      (["open", "stalled"].includes(t.status) ? `<button class="danger" data-act="cancel" data-id="${esc(t.id)}">取消</button>` : "") +
      (t.status === "stalled" ? `<button class="ghost" data-act="reopen" data-id="${esc(t.id)}">重新开放</button>` : ""))).join("");
  const work = me.working.map(r => `<div class="card row"><b>${esc(r.title)}</b><span class="tag">${esc(r.state)}</span>
      ${r.outcome ? `<span class="tag">${esc(r.outcome)}</span>` : ""}<span class="muted">${r.fee_milli != null ? rp(r.fee_milli) : ""}</span>
      <button class="ghost" data-act="open" data-id="${esc(r.task_id)}">详情</button></div>`).join("");
  const led = me.ledger.map(l => `<div class="muted">${when(l.created_at)} · ${l.amount_milli > 0 ? "+" : ""}${rp(l.amount_milli)} · ${esc(l.kind)} · ${esc(l.task_id)}</div>`).join("");
  $("#tab-mine").innerHTML = `<h3>我发布的</h3>${pub || '<p class="muted">无</p>'}<h3>我执行的</h3>${work || '<p class="muted">无</p>'}
    <h3>积分流水</h3><div class="card">${led || '<span class="muted">无</span>'}</div>`;
}

async function openTask(id) {
  const t = await api("GET", "/v2/tasks/" + encodeURIComponent(id));
  const runs = t.runs.map(r => `<div class="card"><div class="row"><b>第 ${r.attempt} 次</b><span class="tag">${esc(r.state)}</span>
      ${r.outcome ? `<span class="tag">${esc(r.outcome)}</span>` : ""}
      ${["assigned", "running"].includes(r.state) ? `<button class="danger" data-act="stop" data-id="${esc(r.id)}" data-task="${esc(id)}">请求停止</button>` : ""}</div>
      <div class="muted">用量 ${esc(r.usage_status || "—")} ${r.usage_status === "known" ? `· 输入 ${r.input_tokens} · 输出 ${r.output_tokens}` : ""} · 结算 ${r.fee_milli != null ? rp(r.fee_milli) : "—"} ${r.error_code ? "· " + esc(r.error_code) : ""}</div>
      ${r.validation ? `<div class="muted">格式检查：${r.validation.passed ? "通过" : "未通过"} ${esc(r.validation.checks.map(c => c.name + (c.passed ? "✓" : "✗ " + c.detail)).join("; "))}</div>` : ""}
      ${r.output_text ? `<pre>${esc(r.output_text)}</pre>` : ""}</div>`).join("");
  $("#detail").innerHTML = `<h3>任务详情</h3><div class="card"><b>${esc(t.title)}</b> <span class="tag">${esc(STATUS[t.status] || t.status)}</span>
    <div class="muted">spec_hash ${esc(t.spec_hash.slice(0, 16))}… · 检查器 ${esc(t.spec.validator_id)} v${esc(t.spec.validator_version)}</div>
    <pre>${esc(t.spec.prompt)}</pre>${t.spec.input_manifest.map(i => `<div class="muted">${esc(i.name)}</div><pre>${esc(i.text)}</pre>`).join("")}</div>${runs}`;
  $("#detail").scrollIntoView({behavior: "smooth"});
}

function renderDevices() {
  const list = me.devices.map(d => `<div class="card"><div class="row"><b>${esc(d.name)}</b><span class="tag">${d.revoked ? "已撤销" : d.online ? "在线" : "离线"}</span>
      ${d.revoked ? "" : `<button class="danger" data-act="revoke" data-id="${esc(d.id)}">撤销</button>`}</div>
      <div class="muted">执行端：${esc(Object.entries(d.capabilities.adapters || {}).map(([k, v]) => `${k}(${(v.models || []).join(",")}; usage ${v.usage})`).join(" · ") || "无")}</div>
      <div class="muted">客户端 ${esc(d.client_version)} · 最后活跃 ${when(d.last_seen_at)}</div></div>`).join("");
  $("#tab-devices").innerHTML = `<div class="card"><p>1. 在电脑上安装客户端（Python 3.10+，仅依赖标准库）：</p>
    <pre>pip install "git+https://github.com/shangliy/agent-relay"</pre>
    <p>2. 生成一次性配对码（10 分钟有效），在电脑上运行显示的命令：</p>
    <button data-act="paircode">生成配对码</button><pre id="paircmd" class="hidden"></pre>
    <p class="muted">手机只负责接单；推理在你的电脑上运行。模型凭据留在本机，不上传平台。运行 <code>agent-relay-worker doctor</code> 可查看本机可用的执行端。</p></div>${list}`;
}

async function renderCommunity() {
  const c = await api("GET", "/v2/community");
  const p = me.profile;
  const board = c.leaderboard.map(e => `<div class="card"><div class="row"><b>#${e.rank} ${esc(e.name)}</b><span class="muted">累计 ${rp(e.earned_milli)} · ${e.tasks} 个任务</span>
      ${e.showcase && e.account_id !== me.account.id ? `<button class="ghost" data-act="report" data-id="${esc(e.account_id)}">举报</button>` : ""}</div>
      ${e.showcase ? `<div>${esc(e.showcase.bio)}</div>${e.showcase.work_url ? `<a href="${esc(e.showcase.work_url)}" rel="noopener nofollow ugc" target="_blank">${esc(e.showcase.work_title || e.showcase.work_url)}</a>` : ""}` : ""}</div>`).join("");
  $("#tab-community").innerHTML = `<p class="muted">累计赚取积分前十；前三且公开名片者展示作品。消费不影响历史排名；排名不代表平台背书，也不保证曝光。</p>
    ${board || '<p class="muted">还没有结算记录。</p>'}
    <h3>我的公开名片</h3><div class="card">
      <label>展示名称</label><input id="f_name" maxlength="40" value="${esc(p.display_name)}">
      <label>简介（≤180 字）</label><textarea id="f_bio" maxlength="180">${esc(p.bio)}</textarea>
      <label>作品名称</label><input id="f_wt" maxlength="80" value="${esc(p.work_title)}">
      <label>作品链接（仅 https）</label><input id="f_wu" value="${esc(p.work_url)}">
      <label><input type="checkbox" id="f_pub" style="width:auto" ${p.public ? "checked" : ""}> 公开名片</label>
      <button data-act="profile">保存</button></div>`;
}

const ACTIONS = {
  async github() {
    if (!needAck()) return;
    location.href = "/v2/auth/github/start?invite=" + encodeURIComponent($("#invite").value.trim());
  },
  async devlogin() {
    if (!needAck()) return;
    await api("POST", "/v2/auth/dev-login", {handle: $("#handle").value});
    boot();
  },
  async logout() { await api("POST", "/v2/auth/logout"); me = null; showLogin(); },
  refresh: () => refresh(),
  page: el => renderLobby(el.dataset.id),
  open: el => openTask(el.dataset.id),
  async claim(el) { await api("POST", `/v2/tasks/${encodeURIComponent(el.dataset.id)}/claim`, {device_id: $("#claimdev").value}); await refresh(); },
  async cancel(el) { await api("POST", `/v2/tasks/${encodeURIComponent(el.dataset.id)}/cancel`); await refresh(); },
  async reopen(el) {
    if (!confirm("旧运行可能仍在供应商端执行，重新开放可能产生重复费用。确认？")) return;
    await api("POST", `/v2/tasks/${encodeURIComponent(el.dataset.id)}/reopen`, {acknowledge_duplicate_cost: true}); await refresh();
  },
  async stop(el) { await api("POST", `/v2/runs/${encodeURIComponent(el.dataset.id)}/stop`); openTask(el.dataset.task); },
  async publish() {
    const input = $("#p_input").value.trim();
    const d = await api("POST", "/v2/tasks", {
      category: $("#p_cat").value, title: $("#p_title").value, prompt: $("#p_prompt").value,
      inputs: input ? [{name: "material", text: input}] : [], deliverable: $("#p_deliv").value,
      adapter: $("#p_adapter").value, model: $("#p_model").value, budget_milli: Math.round(+$("#p_budget").value * 1000),
      public_consent: $("#p_consent").checked,
    });
    await refresh(); show("mine"); openTask(d.task_id);
  },
  async paircode() {
    const d = await api("POST", "/v2/devices/pair-codes");
    const el = $("#paircmd"); el.classList.remove("hidden");
    el.textContent = `agent-relay-worker pair ${d.code} --server ${location.origin}\nagent-relay-worker run`;
  },
  async revoke(el) { if (confirm("撤销后该设备无法继续提交。")) { await api("POST", `/v2/devices/${encodeURIComponent(el.dataset.id)}/revoke`); await refresh(); } },
  async report(el) {
    const reason = prompt("举报原因（例如：链接失效、垃圾推广、违规内容）");
    if (!reason) return;
    await api("POST", "/v2/reports", {target_account_id: el.dataset.id, reason});
    alert("已提交，管理员会尽快处理。");
  },
  async profile() {
    await api("PUT", "/v2/me/profile", {display_name: $("#f_name").value, bio: $("#f_bio").value, work_title: $("#f_wt").value,
                                         work_url: $("#f_wu").value, public: $("#f_pub").checked});
    await refresh();
  },
};

document.addEventListener("click", e => {
  const nav = e.target.closest("nav button[data-tab]");
  if (nav) return show(nav.dataset.tab);
  const el = e.target.closest("[data-act]");
  if (el && ACTIONS[el.dataset.act]) Promise.resolve(ACTIONS[el.dataset.act](el)).catch(() => {});
});
document.addEventListener("change", e => { if (e.target.id === "p_cat") fillTemplate(); });

setInterval(() => { if (me && !document.hidden && ["lobby", "mine"].includes(tab)) refresh().catch(() => {}); }, 15000);
boot();
