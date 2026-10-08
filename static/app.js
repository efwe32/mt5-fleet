/* MT5 批量终端 前端：原生 JS，无需构建。 */
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => Math.abs(Number(n) || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const sfmt = (n) => (n > 0 ? "+" : n < 0 ? "-" : "") + fmt(n);
const money = (n, sign) => `<span class="num ${sign ? (n > 0 ? "up-t" : n < 0 ? "down-t" : "muted") : ""}">${sign ? sfmt(n) : fmt(n)}</span>`;
const pad = (n) => String(n).padStart(2, "0");
const tstr = (ms) => { const d = new Date(ms); return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
const TABS = [["stats", "收益"], ["accounts", "账号"], ["trade", "交易"], ["strategy", "策略"], ["log", "日志"], ["settings", "设置"]];
const POPUP_MS = 180000; // 首页弹出信息停留 3 分钟
const KIND_LABEL = { buy: "市价买入", sell: "市价卖出", buy_limit: "买入限价", sell_limit: "卖出限价", buy_stop: "买入止损", sell_stop: "卖出止损" };
const ORDER_TYPE = ["买", "卖", "买入限价", "卖出限价", "买入止损", "卖出止损", "Buy Stop Limit", "Sell Stop Limit"];

const ui = {
  tab: "stats",   // 首页固定是收益台
  selected: new Set(JSON.parse(localStorage.getItem("fleet.selected") || "[]")),
  search: "", group: "全部", logFail: false, statsScope: "all", busy: false,
  state: null, logs: [], hist: null, lastRender: {},
};

// ---------------- 基础 ----------------
function toast(msg, err) {
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), err ? 6000 : 3200);
}
async function api(path, body, method) {
  const opt = { method: method || (body === undefined ? "GET" : "POST"), headers: { "X-Fleet-Token": window.FLEET_TOKEN } };
  if (body instanceof FormData) opt.body = body;
  else if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch { data = null; }
  if (!r.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === "string" ? d : `请求失败（${r.status}）`);
  }
  return data;
}
function saveSel() { localStorage.setItem("fleet.selected", JSON.stringify([...ui.selected])); }
function selIds() {
  const ids = new Set((ui.state?.accounts || []).map((a) => a.id));
  return [...ui.selected].filter((i) => ids.has(i));
}
function selAccounts() { return (ui.state?.accounts || []).filter((a) => ui.selected.has(a.id)); }
function needSel(msg) { if (!selIds().length) { toast(msg || "请先勾选账户", true); return false; } return true; }

// ---------------- 弹窗 ----------------
function modal({ title, desc, body, actions, wide, onMount }) {
  const root = $("#modal");
  root.innerHTML = `<div class="overlay"><div class="dialog ${wide ? "wide" : ""}" role="dialog" aria-modal="true">
    <h2>${esc(title)}</h2>${desc ? `<div class="desc">${desc}</div>` : ""}
    <div class="body">${body || ""}</div><div class="actions"></div></div></div>`;
  const act = $(".actions", root);
  (actions || [{ label: "关闭" }]).forEach((a) => {
    const b = document.createElement("button");
    b.className = "btn " + (a.tone || "");
    b.textContent = a.label;
    b.type = "button";
    b.onclick = async () => {
      if (a.run) { const keep = await a.run(root); if (keep === true) return; }
      closeModal();
    };
    act.appendChild(b);
  });
  $(".overlay", root).addEventListener("mousedown", (e) => { if (e.target.classList.contains("overlay")) closeModal(); });
  document.addEventListener("keydown", escClose);
  onMount && onMount(root);
}
function escClose(e) { if (e.key === "Escape") closeModal(); }
function closeModal() { $("#modal").innerHTML = ""; document.removeEventListener("keydown", escClose); ui.prog = null; }
function confirmBox({ title, desc, label, tone, run, body, wide }) {
  const head = `<div class="warnbar" style="margin:0 0 8px">⚠ <b>实盘</b>：确认后会立即真实发送到券商。</div>`;
  modal({ title, desc: head + (desc || ""), body, wide, actions: [{ label: "取消" }, { label, tone: tone || "primary", run }] });
}

// 批量操作结果
async function runBatch(label, path, body) {
  if (ui.busy) { toast("上一项还在执行", true); return null; }
  ui.busy = true; renderBusy();
  try {
    const r = await api(path, body);
    if (r && r.results) {
      const fails = r.results.filter((x) => !x.ok);
      if (fails.length) toast(`${label}：成功 ${r.ok}，失败 ${r.fail}。${fails[0].alias}：${fails[0].message}`, true);
      else toast(`${label}：成功 ${r.ok}`);
    }
    return r;
  } catch (e) { toast(e.message, true); return null; }
  finally { ui.busy = false; renderBusy(); poll(); }
}
function renderBusy() {
  $$("main [data-act], main [data-close]").forEach((b) => {
    if (["add", "import", "example", "log-fail", "log-csv", "log-clear", "upload", "save-settings", "upd-check", "upd-install", "upd-rollback"].includes(b.dataset.act)) return;
    b.disabled = ui.busy;
  });
}


function renderQuote(q) {
  const html = !q || !q.price
    ? `USDJPY <span class="num muted">—</span><span class="src">${esc(q && q.error ? q.error : "等待报价")}</span>`
    : `USDJPY <span class="px ${q.change > 0 ? "up-t" : q.change < 0 ? "down-t" : ""}">${Number(q.price).toFixed(q.digits || 3)}</span><span class="chg ${q.change > 0 ? "up-t" : q.change < 0 ? "down-t" : ""}">${q.change > 0 ? "↑" : q.change < 0 ? "↓" : ""}${q.change > 0 ? "+" : ""}${Number(q.change).toFixed(2)}%</span><span class="src">24小时 · ${esc(q.source || "")}${q.symbol && q.symbol !== "USDJPY" ? " · " + esc(q.symbol) : ""}</span>`;
  $$("[data-slot=fx]").forEach((el) => { if (el.innerHTML !== html) el.innerHTML = html; });
}
function renderAlerts(list) {
  const html = `<b>警报</b>` + (!list.length
    ? `<span class="small muted">暂无</span>`
    : list.map((a) => `<span class="alert-chip"><b>警报${a.n}</b><span>${esc(a.alias)} ${esc(a.login)} 浮亏 ${Number(a.floating).toFixed(2)} ${esc(a.currency || "")} · ${tstr(a.at)}</span><button type="button" class="btn danger sm" data-act="alert-view" data-alert="${esc(a.id)}">查看</button></span>`).join(""));
  $$("[data-slot=alerts]").forEach((el) => { if (el.innerHTML !== html) el.innerHTML = html; });
}
function renderCalendar(cal) {
  const el = $("#calBoard .pd-cal-body") || $("#calBoard");
  if (!el) return;
  const box = $("#calBoard .pd-cal-body") || el;
  if (!cal) { box.innerHTML = `<span class="small muted">正在读取金十日历…</span>`; return; }
  const rows = cal.events || [];
  let html = "";
  if (cal.error) html += `<div class="note bad" style="margin-bottom:8px">${esc(cal.error)}</div>`;
  if (!rows.length && !cal.error) html += `<span class="small muted">今天起没有 4 星及以上的数据。</span>`;
  if (rows.length) {
    html += `<table><thead><tr><th>时间</th><th>国家/货币</th><th>事件</th><th>前值</th><th>预期</th><th>公布</th><th>星级</th></tr></thead><tbody>`
      + rows.map((r) => `<tr><td class="num">${esc(r.time)}</td><td>${esc(r.country)}</td><td>${esc(r.title)}${r.unit ? " (" + esc(r.unit) + ")" : ""}</td><td class="num">${esc(r.previous)}</td><td class="num">${esc(r.forecast)}</td><td class="num">${esc(r.actual)}</td><td class="stars">${"★".repeat(r.star || 0)}</td></tr>`).join("")
      + `</tbody></table>`;
  }
  if (box.innerHTML !== html) box.innerHTML = html;
}
const heardAlerts = new Set();
let beepReady = false;
function beepNew(list) {
  const ids = list.map((a) => a.id);
  if (!beepReady) { ids.forEach((id) => heardAlerts.add(id)); beepReady = true; return; }
  const fresh = ids.filter((id) => !heardAlerts.has(id));
  ids.forEach((id) => heardAlerts.add(id));
  if (fresh.length) beep();
}
function beep() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = ui.audio || (ui.audio = new Ctx());
    const tone = (freq, at) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "sine"; o.frequency.value = freq; o.connect(g); g.connect(ctx.destination);
      g.gain.setValueAtTime(0.0001, ctx.currentTime + at);
      g.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + at + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + at + 0.35);
      o.start(ctx.currentTime + at); o.stop(ctx.currentTime + at + 0.36);
    };
    tone(880, 0); tone(660, 0.18);
  } catch (e) { /* 浏览器没开声音权限时静默 */ }
}

// ---------------- 导航 ----------------
function renderNav() {
  const html = TABS.map(([id, label]) => `<button type="button" data-tab="${id}" class="${ui.tab === id ? "on" : ""}" ${ui.tab === id ? 'aria-current="page"' : ""}>${label}${id === "settings" && upd.newer ? '<span class="nav-new" title="有新版本，去设置里更新">新</span>' : ""}</button>`).join("");
  $("#nav").innerHTML = html; $("#navMobile").innerHTML = html;
  $$(".page").forEach((p) => p.classList.toggle("hidden", p.dataset.page !== ui.tab));
  document.body.classList.toggle("desk-mode", ui.tab === "stats");
  if (window.Desk) { if (ui.tab === "stats") { Desk.start(); if (ui.state) Desk.onState(ui.state); } else Desk.stop(); }
  if (window.Quick) { if (ui.tab === "trade") { if (ui.state) Quick.onState(ui.state); Quick.start(); } else Quick.stop(); }
}
function go(tab) {
  ui.tab = tab; localStorage.setItem("fleet.tab", tab); ui.lastRender = {}; renderNav(); render();
  if (tab === "log") loadLogs();
  if (tab === "stats" && !ui.hist) loadStats();
  if (tab === "settings") fillSettings();
}

// ---------------- 状态轮询 ----------------
let pollTimer = null;
async function poll() {
  clearTimeout(pollTimer);
  try {
    ui.state = await api("/api/state");
    render();
  } catch (e) {
    $("#warn").innerHTML = `<div class="warnbar">与本地服务断开：${esc(e.message)}。请确认黑色命令行窗口还开着。</div>`;
  }
  pollTimer = setTimeout(poll, 1200);
}

// 实时数字：结构不变时只更新数字，不重建 DOM（避免点击按钮时被重绘打断）
ui.live = {};
const L = (key) => `<span data-live="${esc(key)}">${ui.live[key] ?? ""}</span>`;
function applyLive() {
  $$("[data-live]").forEach((el) => { const v = ui.live[el.dataset.live]; if (v !== undefined && el.innerHTML !== v) el.innerHTML = v; });
}
function once(key, value, fn) {
  const s = JSON.stringify(value);
  if (ui.lastRender[key] === s) return;
  ui.lastRender[key] = s; fn();
}

function render() {
  const st = ui.state; if (!st) return;
  const accs = st.accounts;
  // 汇总
  let eq = 0, fl = 0, on = 0;
  accs.forEach((a) => { eq += a.equity || 0; fl += a.floating || 0; if (a.link === "online") on++; });
  $("#sumSel").textContent = selIds().length;
  $("#sumOnline").textContent = `${on}/${accs.length}`;
  $("#sumEquity").innerHTML = money(eq);
  $("#sumFloat").innerHTML = money(fl, true);
  $("#sideDot").className = "dot " + (on > 0 ? "up" : "");
  $("#sideText").textContent = `${on > 0 ? "在线" : "离线"} · 实盘`;
  $("#footText").textContent = "实盘：批量操作会真实发送到券商，每次执行前都会弹出确认。密码只加密保存在本机，不会上传。";
  // 警告条
  const warns = [];
  if (st.mt5Error) warns.push(st.mt5Error);
  if (!st.mock && !st.dpapi) warns.push("当前系统不支持 Windows DPAPI，密码只做了 base64 混淆保存（不是加密），请保护好数据目录。");
  once("warn", warns, () => { $("#warn").innerHTML = warns.map((w) => `<div class="warnbar">${esc(w)}</div>`).join(""); });
  const shownBanner = st.banner && st.banner.at !== ui.bannerDismissed && (Date.now() - st.banner.at < POPUP_MS) ? st.banner : null;
  const ap = st.alertPopup;
  const shownAlert = ap && ap.at !== ui.alertPopupHidden && (Date.now() - ap.at < POPUP_MS) ? ap : null;
  once("banner", [shownBanner, shownAlert], () => {
    const parts = [];
    if (shownAlert) parts.push(`<div class="banner" style="border-color:var(--down)">警报${shownAlert.n}：${esc(shownAlert.text)}<button class="btn danger sm" data-act="alert-view" data-alert="${esc(shownAlert.id)}" style="margin-left:auto">查看</button></div>`);
    if (shownBanner) parts.push(`<div class="banner">${esc(shownBanner.title)}：成功 <span class="num up-t">${shownBanner.ok}</span>，失败 <span class="num ${shownBanner.fail ? "down-t" : "muted"}">${shownBanner.fail}</span><button class="btn ghost sm" data-act="banner-x" style="margin-left:auto">知道了</button></div>`);
    $("#banner").innerHTML = parts.join("");
  });
  renderQuote(st.quote);
  renderAlerts(st.alerts || []);
  renderCalendar(st.calendar);
  beepNew(st.alerts || []);
  // datalists
  once("lists", [st.symbols, st.library, accs.map((a) => a.positions.map((p) => p.symbol))], () => {
    const syms = [...new Set([...st.symbols, ...accs.flatMap((a) => a.positions.map((p) => p.symbol))])];
    $("#symList").innerHTML = syms.map((s) => `<option value="${esc(s)}">`).join("");
    $("#setList").innerHTML = st.library.filter((n) => n.toLowerCase().endsWith(".set")).map((s) => `<option value="${esc(s)}">`).join("");
  });
  once("servers", [st.servers, accs.map((a) => a.group)], () => {
    $("#serverList").innerHTML = (st.servers || []).map((s) => `<option value="${esc(s)}">`).join("");
    const gs = [...new Set(["主仓", "跟单", "测试", ...accs.map((a) => a.group)])];
    $("#groupList").innerHTML = gs.map((g) => `<option value="${esc(g)}">`).join("");
  });
  if (ui.prog) renderProgress();
  if (ui.tab === "settings") renderSettingsInfo();
  if (ui.tab === "accounts") renderAccounts();
  if (ui.tab === "trade") { if (window.Quick) Quick.onState(st); renderTrade(); }
  if (ui.tab === "strategy") renderStrategy();
  if (ui.tab === "stats") { renderStats(); if (window.Desk) Desk.onState(st); }
  applyLive();
  renderBusy();
  if (st.firstLaunch && !ui.welcomed) { ui.welcomed = true; welcome(); }
}

function linkBadge(a) {
  const m = { online: ["在线", "up", "up-t"], connecting: ["连接中", "acc", "acc-t"], error: ["失败", "down", "down-t"], offline: ["离线", "", ""] }[a.link] || ["离线", "", ""];
  const tip = a.linkError ? ` title="${esc(a.linkError)}"` : "";
  return `<span class="small muted" style="display:inline-flex;gap:8px;align-items:center"${tip}><span class="dot ${m[1]}"></span><span class="${m[2]}">${m[0]}</span></span>`;
}
function termTag(a) {
  if (a.link !== "online" || !a.terminal) return a.ownedTerminal ? `<span class="tag" title="这个终端由本程序启动">本程序启动</span>` : "";
  return a.terminal.trade_allowed ? `<span class="tag ok" title="终端的算法交易已开启">算法交易 开</span>` : `<span class="tag warn" title="请在 MT5 工具栏打开 算法交易 按钮，否则发单会被拒绝">算法交易 关</span>`;
}

// ---------------- 账号页 ----------------
function filtered() {
  const q = ui.search.trim().toLowerCase();
  return ui.state.accounts.filter((a) => (ui.group === "全部" || a.group === ui.group) &&
    (!q || `${a.alias} ${a.login} ${a.server} ${a.group}`.toLowerCase().includes(q)));
}
function renderAccounts() {
  const st = ui.state;
  const groups = ["全部", ...new Set(st.accounts.map((a) => a.group))];
  if (!groups.includes(ui.group)) ui.group = "全部";
  once("groups", [groups, ui.group], () => {
    $("#groupFilter").innerHTML = groups.map((g) => `<button class="btn ${g === ui.group ? "primary" : ""}" data-group="${esc(g)}">${esc(g)}</button>`).join("");
  });
  const list = filtered();
  list.forEach((a) => { ui.live[a.id + ":bal"] = money(a.balance); ui.live[a.id + ":eq"] = money(a.equity); ui.live[a.id + ":fl"] = money(a.floating, true); ui.live[a.id + ":pos"] = String(a.positionsCount); });
  const view = list.map((a) => [a.id, a.alias, a.login, a.server, a.group, a.enabled, a.link, a.linkError,
    a.strategies.map((s) => s.running), a.terminal?.trade_allowed, a.ownedTerminal, a.has_password, ui.selected.has(a.id), a.terminal_path, a.lot_multiplier, a.symbol_suffix]);
  once("acc", [view, ui.search, ui.group], () => {
    if (!list.length) {
      $("#accList").innerHTML = `<div class="empty"><h2>${st.accounts.length ? "没有匹配的账户" : "还没有账户"}</h2><p>${st.accounts.length ? "换个关键词，或点「添加并登录」。" : "点「添加并登录」，填账号、交易密码、服务器即可：程序会自动复制一份独立的 MT5、启动、登录并打开算法交易。也可以一次粘贴多个账户。"}</p><div class="row"><button class="btn primary" data-act="quick">添加并登录</button></div></div>`;
      return;
    }
    const allOn = list.every((a) => ui.selected.has(a.id));
    const strat = (a) => `${a.strategies.filter((s) => s.running).length}/${a.strategies.length}`;
    const pw = (a) => a.has_password ? "" : ` · <span class="down-t">未存密码</span>`;
    const cards = list.map((a) => `<article class="card ${a.enabled ? "" : "off"}" style="${a.enabled ? "" : "opacity:.6"}">
      <div style="display:flex;gap:4px;align-items:flex-start"><label class="cbcell"><input type="checkbox" data-sel="${a.id}" ${ui.selected.has(a.id) ? "checked" : ""} aria-label="选择 ${esc(a.alias)}"></label>
      <div style="flex:1;min-width:0"><div style="display:flex;justify-content:space-between;gap:8px"><b>${esc(a.alias)}</b>${linkBadge(a)}</div>
      <div class="sub">${esc(a.login)} · ${esc(a.server)}</div></div></div>
      <dl><div><dt>余额</dt><dd>${L(a.id + ":bal")}</dd></div><div><dt>净值</dt><dd>${L(a.id + ":eq")}</dd></div><div><dt>浮盈</dt><dd>${L(a.id + ":fl")}</dd></div></dl>
      <p class="small faint" style="margin:8px 0 0">${esc(a.group)} · 持仓 ${L(a.id + ":pos")} · 策略 ${strat(a)}${pw(a)} ${termTag(a)}</p>
      ${a.link === "error" ? `<p class="small down-t" style="margin:6px 0 0">${esc(a.linkError)}</p>` : ""}
      <div class="row" style="margin-top:12px"><button class="btn" data-edit="${a.id}">编辑</button><button class="btn ghost" data-toggle="${a.id}">${a.enabled ? "停用" : "启用"}</button></div></article>`).join("");
    const rows = list.map((a) => `<tr class="${a.enabled ? "" : "off"}">
      <td class="cbcell"><input type="checkbox" data-sel="${a.id}" ${ui.selected.has(a.id) ? "checked" : ""} aria-label="选择 ${esc(a.alias)}"></td>
      <td><div style="font-weight:500">${esc(a.alias)}</div><div class="sub">${esc(a.login)} · ${esc(a.server)} · ${esc(a.group)}${a.lot_multiplier !== 1 ? ` · ×${a.lot_multiplier}` : ""}${a.symbol_suffix ? ` · 后缀 ${esc(a.symbol_suffix)}` : ""}${pw(a)}</div></td>
      <td>${L(a.id + ":bal")}</td><td>${L(a.id + ":eq")}</td><td>${L(a.id + ":fl")}</td>
      <td class="num">${L(a.id + ":pos")}</td><td class="num">${strat(a)}</td>
      <td style="max-width:220px"><div class="sub" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(a.terminal_path)}">${esc(a.terminal_path || "未设置")}</div>${termTag(a)}</td>
      <td>${linkBadge(a)}${a.link === "error" ? `<div class="small down-t" style="max-width:240px">${esc(a.linkError)}</div>` : ""}</td>
      <td><div class="row" style="flex-wrap:nowrap"><button class="btn" data-edit="${a.id}">编辑</button><button class="btn ghost" data-toggle="${a.id}">${a.enabled ? "停用" : "启用"}</button></div></td></tr>`).join("");
    $("#accList").innerHTML = `<div class="row" style="margin-bottom:8px"><label class="cbcell"><input type="checkbox" id="selAll" ${allOn ? "checked" : ""} aria-label="勾选当前列表"></label><span class="muted">勾选当前列表 · 已选 ${selIds().length}</span></div>
      <div class="cards">${cards}</div>
      <div class="tablewrap desk"><table><thead><tr><th class="cbcell"></th><th>账户</th><th>余额</th><th>净值</th><th>浮盈</th><th>持仓</th><th>策略</th><th>终端</th><th>状态</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  });
}

function accountForm(acc) {
  const a = acc || { alias: "", login: "", server: "", group: "测试", terminal_path: "", lot_multiplier: 1, symbol_suffix: "", symbol_map: "" };
  const root = ui.state.terminalsRoot, sep = root.includes("\\") ? "\\" : "/";
  modal({
    title: acc ? "编辑账户" : "添加账户", wide: true,
    desc: "密码会用 Windows DPAPI 加密后保存在本机数据目录，只有当前 Windows 用户能解开，不会发往任何地方。每个账户用程序目录 terminals 里一份独立的 MT5（总是 /portable 启动），不会动你桌面上的 MT5。",
    body: `<div class="grid2">
      <label class="field"><span>备注</span><input class="input" name="alias" value="${esc(a.alias)}" required></label>
      <label class="field"><span>分组</span><input class="input" name="group" list="groupList" value="${esc(a.group)}"></label>
      <label class="field"><span>账号</span><input class="input num" name="login" inputmode="numeric" value="${esc(a.login)}" required></label>
      <label class="field"><span>${acc ? "密码（留空则不改）" : "密码（交易密码）"}</span><input class="input" type="password" name="password" autocomplete="new-password"></label>
      <label class="field"><span>服务器（和 MT5 登录框里的一致）</span><input class="input" name="server" value="${esc(a.server)}" placeholder="例如 ICMarketsSC-MT5" required></label>
      <label class="field"><span>手数倍数（按账户倍数下单时用）</span><input class="input num" name="lot_multiplier" value="${esc(a.lot_multiplier)}" inputmode="decimal"></label>
    </div>
    <label class="field"><span>终端路径（必须在 ${esc(root)} 里；留空 = ${esc(root + sep)}账号${sep}terminal64.exe）</span>
      <div class="split"><input class="input" name="terminal_path" value="${esc(a.terminal_path)}" placeholder="${esc(root + sep)}${esc(a.login || "账号")}${sep}terminal64.exe"><button class="btn" type="button" data-check-path>检测</button></div></label>
    <div class="small muted" id="pathInfo"></div>
    <div class="grid2">
      <label class="field"><span>品种后缀（如 .m / .r / -ECN，自动加在品种名后）</span><input class="input" name="symbol_suffix" value="${esc(a.symbol_suffix)}"></label>
      <label class="field"><span>品种映射（可选，如 XAUUSD=GOLD; NAS100=USTEC）</span><input class="input" name="symbol_map" value="${esc(a.symbol_map)}"></label>
    </div>`,
    actions: [{ label: "取消" }, {
      label: "保存", tone: "primary", run: async (root) => {
        const f = {};
        $$("[name]", root).forEach((el) => { f[el.name] = el.type === "checkbox" ? el.checked : el.value.trim(); });
        if (!f.alias) { toast("请填写备注", true); return true; }
        if (!/^\d{3,}$/.test(f.login)) { toast("账号需为至少 3 位数字", true); return true; }
        if (!f.server) { toast("请填写服务器", true); return true; }
        try {
          if (acc) await api(`/api/accounts/${acc.id}`, f, "PUT"); else await api("/api/accounts", f);
          toast(acc ? "已保存" : "已添加"); poll();
        } catch (e) { toast(e.message, true); return true; }
      },
    }],
    onMount: (root) => {
      $("[data-check-path]", root).onclick = async () => {
        const p = $("[name=terminal_path]", root).value.trim(), login = $("[name=login]", root).value.trim();
        try {
          const r = await api("/api/tools/check_path", { path: p, login });
          $("#pathInfo").innerHTML = `<span class="num">${esc(r.path)}</span><br>` + (r.error ? `<span class="down-t">${esc(r.error)}</span>` : r.exists
            ? `<span class="up-t">找到 terminal64.exe</span>${r.portableData ? " · 已有便携数据目录 MQL5" : " · 还没有 MQL5 文件夹（首次 /portable 启动后生成）"}${r.running ? (r.owned ? " · 正在运行（本程序启动）" : ' · <span class="down-t">正在运行但不是本程序启动的，登录前请先关掉它</span>') : ""}`
            : `<span class="down-t">这个路径下还没有 MT5</span>，请用设置页或「创建终端副本.bat」复制一份。`);
        } catch (e) { toast(e.message, true); }
      };
    },
  });
}

// ---------------- 添加并登录 ----------------
const STATE_ICON = { wait: "○", run: "", ok: "✓", warn: "!", fail: "✕", skip: "–" };
function tplNote() {
  const t = ui.state.template || {};
  if (t.path) return `<div class="note">模板 MT5：<span class="num">${esc(t.path)}</span>${t.auto ? "（自动找到）" : ""}<br>没有终端副本的账户会自动复制一份到 <span class="num">${esc(ui.state.terminalsRoot)}\\账号</span>（只读取模板，不会改动它）。</div>`;
  return `<div class="note bad">${esc(t.error || "没有找到模板 MT5")}　<button class="btn sm" type="button" data-tab="settings">去设置</button></div>`;
}
function srvNote() {
  const si = ui.state.serversImport;
  return si ? `<span class="small faint">服务器列表：已从 <span class="num">${esc(si.source)}</span> 导入</span>`
    : `<span class="small faint">登录报「授权失败」、找不到服务器时，可在设置页点「一键修复服务器列表」。</span>`;
}
function quickForm() {
  let mode = "one";
  modal({
    title: "添加并登录", wide: true,
    desc: `填好账号、交易密码、服务器后，程序会自动完成：<div class="flow" style="margin-top:6px"><b>创建副本</b>→<b>启动终端</b>→<b>登录</b>→<b>算法交易已开启</b></div>`,
    body: `<div class="seg" role="tablist"><button type="button" class="on" data-qmode="one">单个账户</button><button type="button" data-qmode="many">批量粘贴</button></div>
      <div id="qOne"><div class="grid2">
        <label class="field"><span>账号</span><input class="input num" name="login" inputmode="numeric" autocomplete="off" placeholder="例如 51002811"></label>
        <label class="field"><span>交易密码</span><input class="input" type="password" name="password" autocomplete="new-password"></label>
        <label class="field"><span>服务器（和 MT5 登录框里的一致，可从下拉选择用过的）</span><input class="input" name="server" list="serverList" autocomplete="off" placeholder="例如 ICMarketsSC-MT5"></label>
        <label class="field"><span>分组</span><input class="input" name="group" list="groupList" placeholder="例如 主仓 / 跟单"></label>
        <label class="field"><span>名称（可选，默认用账号）</span><input class="input" name="alias" placeholder="例如 黄金一号"></label>
      </div></div>
      <div id="qMany" class="hidden"><label class="field"><span>每行一个账户：账号,密码,服务器[,分组,名称]（逗号、中文逗号或 Tab 分隔）</span>
        <textarea class="input num" id="qText" rows="7" spellcheck="false" placeholder="51002811,交易密码,ICMarketsSC-MT5,主仓,黄金一号&#10;88011203,交易密码,Pepperstone-Edge-Live,跟单"></textarea></label></div>
      ${tplNote()}${srvNote()}
      <p class="small muted" style="margin:0">密码用 Windows DPAPI 加密保存在本机；启动终端时写入的一次性配置文件在登录后立即删除。已在列表里的账号（账号 + 服务器相同）只更新密码并重新登录。</p>`,
    actions: [{ label: "取消" }, {
      label: "添加并登录", tone: "primary", run: async (root) => {
        let body;
        if (mode === "one") {
          const f = {}; $$("#qOne [name]", root).forEach((el) => { f[el.name] = el.value.trim(); });
          f.password = $("#qOne [name=password]", root).value;
          if (!/^\d{3,}$/.test(f.login)) { toast("账号需为至少 3 位数字", true); return true; }
          if (!f.password) { toast("请填写交易密码", true); return true; }
          if (!f.server) { toast("请填写服务器", true); return true; }
          body = { rows: [f] };
        } else {
          const text = $("#qText", root).value.trim();
          if (!text) { toast("请粘贴账户，每行一个", true); return true; }
          body = { text };
        }
        startProgress("添加并登录", "/api/accounts/quick", body);
        return true;   // 进度窗口已经替换了当前窗口
      },
    }],
    onMount: (root) => {
      $$("[data-qmode]", root).forEach((b) => b.onclick = () => {
        mode = b.dataset.qmode;
        $$("[data-qmode]", root).forEach((x) => x.classList.toggle("on", x === b));
        $("#qOne", root).classList.toggle("hidden", mode !== "one"); $("#qMany", root).classList.toggle("hidden", mode !== "many");
      });
      setTimeout(() => $("#qOne [name=login]", root)?.focus(), 30);
    },
  });
}

// ---------------- 进度窗口（登录 / 分发 / 停止） ----------------
async function startProgress(title, path, body) {
  if (ui.busy) { toast("上一项还在执行", true); return null; }
  ui.prog = { title, since: Date.now() - 1500, result: null, errors: [] };
  modal({ title, wide: true, body: `<div id="progBody"><p class="small muted"><span class="spin"></span> 正在开始…</p></div>`,
    actions: [{ label: "关闭", run: () => { ui.prog = null; } }] });
  ui.busy = true; renderBusy();
  let r = null;
  try {
    r = await api(path, body);
    if (ui.prog) { ui.prog.result = r; ui.prog.errors = r.errors || []; }
    const fails = (r.results || []).filter((x) => !x.ok);
    if (!ui.prog) fails.length ? toast(`${title}：成功 ${r.ok}，失败 ${r.fail}。${fails[0].alias}：${fails[0].message}`, true) : toast(`${title}：成功 ${r.ok}`);
  } catch (e) {
    if (ui.prog) ui.prog.errors = [e.message]; else toast(e.message, true);
  } finally { ui.busy = false; renderBusy(); await poll(); if (ui.prog) { ui.lastRender.prog = null; renderProgress(); } }
  return r;
}
function renderProgress() {
  const box = $("#progBody"); if (!box || !ui.prog) return;
  const p = ui.state?.progress;
  const cur = p && p.started >= ui.prog.since ? p : null;
  const res = ui.prog.result, errs = ui.prog.errors || [];
  once("prog", [cur, res && res.ok, errs], () => {
    const err = errs.length ? `<div class="note bad">${errs.map(esc).join("<br>")}</div>` : "";
    if (!cur) { box.innerHTML = err || `<p class="small muted"><span class="spin"></span> 正在开始…</p>`; return; }
    const items = cur.order.map((i) => [i, cur.items[i]]).filter((x) => x[1]);
    const done = items.filter(([, it]) => it.done), ok = done.filter(([, it]) => it.ok).length;
    const head = cur.done && res !== null
      ? `<div class="prog-head"><b class="${done.length - ok ? "down-t" : "up-t"}">完成</b><span>成功 <b class="up-t num">${ok}</b>，失败 <b class="num ${done.length - ok ? "down-t" : ""}">${done.length - ok}</b></span></div>`
      : `<div class="prog-head"><span class="spin"></span><span>进行中 <b class="num">${done.length}/${items.length}</b>，关闭窗口不会中断，结果会记在日志页</span></div>`;
    const rows = items.map(([, it]) => {
      const steps = cur.steps.map(([k, label]) => {
        const st = (it.steps[k] || { s: "wait" });
        const icon = st.s === "run" ? `<span class="spin"></span>` : `<i>${STATE_ICON[st.s] || "○"}</i>`;
        return `<span class="step ${st.s}" title="${esc(st.m || "")}">${icon}${esc(label)}</span>`;
      }).join("");
      const live = cur.steps.map(([k]) => it.steps[k]).filter((x) => x && x.m && x.s !== "wait");
      const warn = live.find((x) => x.s === "warn"), fail = live.find((x) => x.s === "fail"), last = live[live.length - 1];
      const msg = it.done ? (it.ok ? (warn ? warn.m : it.message) : (fail ? fail.m : it.message)) : (last ? last.m : "等待中");
      const cls = it.done && !it.ok ? "bad" : warn ? "warn" : "";
      return `<div class="prog-item ${it.done ? (it.ok ? "ok" : "fail") : ""}"><div class="prog-top"><b>${esc(it.alias)} <span class="sub num">${esc(it.login)}</span></b>
        <span class="small ${it.done ? (it.ok ? "up-t" : "down-t") : "acc-t"}">${it.done ? (it.ok ? (warn ? "成功（有提醒）" : "成功") : "失败") : "处理中"}</span></div>
        <div class="steps">${steps}</div><div class="prog-msg ${cls}">${esc(msg || "")}</div></div>`;
    }).join("");
    box.innerHTML = head + err + `<div class="prog-list" style="margin-top:10px">${rows}</div>`;
  });
}

// ---------------- 交易页 ----------------
function renderTrade() {
  const sel = selAccounts();
  $("#tradeEmpty").classList.toggle("hidden", sel.length > 0);
  $("#tradeBody").classList.toggle("hidden", sel.length === 0);
  if (!sel.length) {
    once("tradeEmpty", 1, () => { $("#tradeEmpty").innerHTML = `<div class="empty"><h2>还没有勾选账户</h2><p>这里的高级操作作用于账号页勾选的账户。日常开仓、平仓用上面的快捷交易面板（在面板里直接选账户）即可。</p><div class="row"><button class="btn primary" data-tab="accounts">去勾选账户</button></div></div>`; });
    return;
  }
  ui.lastRender.tradeEmpty = null;
  const syms = [...new Set([...ui.state.symbols, ...sel.flatMap((a) => a.positions.map((p) => p.symbol))])];
  once("tradeSyms", syms, () => {
    const keep = $("#cSymbol").value, keep2 = $("#mScope").value;
    $("#cSymbol").innerHTML = syms.map((s) => `<option>${esc(s)}</option>`).join("");
    $("#mScope").innerHTML = `<option value="">全部持仓</option>` + syms.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
    if (keep) $("#cSymbol").value = keep; if (keep2) $("#mScope").value = keep2;
  });
  // 敞口
  const net = new Map();
  sel.filter((a) => a.link === "online").forEach((a) => a.positions.forEach((p) => net.set(p.symbol, (net.get(p.symbol) || 0) + (p.side === "buy" ? p.volume : -p.volume))));
  const ex = [...net.entries()].map(([s, n]) => [s, Math.round(n * 100) / 100]).filter((x) => x[1] !== 0).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
  once("exposure", ex, () => {
    $("#exposure").innerHTML = ex.length ? `<div class="row">${ex.map(([s, n]) => `<span class="chip"><span class="muted">${esc(s)}</span><span class="num ${n > 0 ? "up-t" : "down-t"}">${n > 0 ? "净多" : "净空"} ${Math.abs(n).toFixed(2)}</span></span>`).join("")}</div>` : `<p class="small muted" style="margin:0">登录之后，这里按品种汇总净手数。</p>`;
  });
  const rows = sel.flatMap((a) => a.positions.map((p) => ({ a, p })));
  rows.forEach(({ a, p }) => { const k = `${a.id}:${p.ticket}`; ui.live[k + ":px"] = String(p.price); ui.live[k + ":pl"] = money(p.profit, true); ui.live[k + ":sl"] = p.sl ? String(p.sl) : "—"; ui.live[k + ":tp"] = p.tp ? String(p.tp) : "—"; });
  once("pos", rows.map(({ a, p }) => [a.id, p.ticket, p.volume]), () => {
    if (!rows.length) {
      $("#posList").innerHTML = `<div class="empty"><h2>已选账户没有持仓</h2><p>登录后如果策略或手工单开了仓，会出现在这里。</p></div>`;
      return;
    }
    const dg = (v) => (v ? String(v) : "—");
    $("#posList").innerHTML = `<div class="cards">${rows.map(({ a, p }) => `<article class="card"><div style="display:flex;justify-content:space-between"><div><b>${esc(a.alias)}</b><div class="sub">${esc(p.symbol)} · #${p.ticket}</div></div><span class="${p.side === "buy" ? "up-t" : "down-t"}">${p.side === "buy" ? "多" : "空"}</span></div>
      <dl><div><dt>手数</dt><dd class="num">${p.volume.toFixed(2)}</dd></div><div><dt>现价</dt><dd class="num">${L(`${a.id}:${p.ticket}:px`)}</dd></div><div><dt>盈亏</dt><dd>${L(`${a.id}:${p.ticket}:pl`)}</dd></div></dl>
      <div class="row" style="margin-top:8px;justify-content:space-between"><span class="small faint">魔术号 ${p.magic} · SL ${L(`${a.id}:${p.ticket}:sl`)} · TP ${L(`${a.id}:${p.ticket}:tp`)}</span><button class="btn sm" data-close-ticket="${a.id}:${p.ticket}">平仓</button></div></article>`).join("")}</div>
      <div class="tablewrap desk"><table><thead><tr><th>账户</th><th>单号</th><th>品种</th><th>方向</th><th>手数</th><th>开仓</th><th>现价</th><th>SL</th><th>TP</th><th>盈亏</th><th>魔术号</th><th></th></tr></thead><tbody>
      ${rows.map(({ a, p }) => `<tr><td>${esc(a.alias)}</td><td class="num">${p.ticket}</td><td>${esc(p.symbol)}</td><td class="${p.side === "buy" ? "up-t" : "down-t"}">${p.side === "buy" ? "多" : "空"}</td><td class="num">${p.volume.toFixed(2)}</td><td class="num">${p.openPrice}</td><td class="num">${L(`${a.id}:${p.ticket}:px`)}</td><td class="num muted">${L(`${a.id}:${p.ticket}:sl`)}</td><td class="num muted">${L(`${a.id}:${p.ticket}:tp`)}</td><td>${L(`${a.id}:${p.ticket}:pl`)}</td><td class="num">${p.magic}</td><td><button class="btn sm" data-close-ticket="${a.id}:${p.ticket}">平仓</button></td></tr>`).join("")}
      </tbody></table></div>`;
  });
  const ords = sel.flatMap((a) => (a.orders || []).map((o) => ({ a, o })));
  once("ords", ords.map(({ a, o }) => [a.id, o.ticket, o.price]), () => {
    $("#ordList").innerHTML = ords.length ? `<div class="panel"><h3>挂单（${ords.length}）</h3><div class="tablewrap" style="margin-top:8px"><table><thead><tr><th>账户</th><th>单号</th><th>品种</th><th>类型</th><th>手数</th><th>价格</th><th>SL</th><th>TP</th><th>魔术号</th></tr></thead><tbody>
      ${ords.map(({ a, o }) => `<tr><td>${esc(a.alias)}</td><td class="num">${o.ticket}</td><td>${esc(o.symbol)}</td><td>${ORDER_TYPE[o.type] || o.type}</td><td class="num">${o.volume}</td><td class="num">${o.price}</td><td class="num">${o.sl || "—"}</td><td class="num">${o.tp || "—"}</td><td class="num">${o.magic}</td></tr>`).join("")}</tbody></table></div></div>` : "";
  });
  $("#tradeNote").textContent = "实盘：所有操作直接发送到券商服务器。";
}

function orderForm(kindOverride) {
  const kind = kindOverride || $("#oKind").value;
  return {
    symbol: $("#oSymbol").value.trim(), kind, lots: $("#oLots").value, price: kind.includes("_") ? $("#oPrice").value : 0,
    scale_mode: $("#oScale").value, ref_equity: $("#oRefEq").value,
    scale_groups: $("#oGroups").value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
    sltp_mode: $("#oUnit").value, sl: $("#oSL").value, tp: $("#oTP").value, magic: $("#oMagic").value, comment: $("#oComment").value,
  };
}
async function placeOrder(kindOverride) {
  if (!needSel("请先在账号页勾选账户")) return;
  const order = orderForm(kindOverride);
  if (!order.symbol) return toast("请填写品种", true);
  if (!(Number(order.lots) > 0)) return toast("手数需大于 0", true);
  if (order.kind.includes("_") && !(Number(order.price) > 0)) return toast("挂单需要填写价格", true);
  let pv;
  try { pv = await api("/api/trade/preview", { ids: selIds(), order }); } catch (e) { return toast(e.message, true); }
  const table = `<div class="tablewrap"><table><thead><tr><th>账户</th><th>分组</th><th>净值</th><th>系数</th><th>手数</th><th>备注</th></tr></thead><tbody>
    ${pv.rows.map((r) => `<tr><td>${esc(r.alias)}</td><td>${esc(r.group)}</td><td class="num">${fmt(r.equity)}</td><td class="num">${r.factor}</td><td class="num">${r.lots}</td><td class="small ${r.warn ? "down-t" : "muted"}">${esc(r.warn || "手数会按品种步长向下取整")}</td></tr>`).join("")}
    </tbody></table></div><p class="small muted" style="margin:0">合计约 <b class="num">${pv.total}</b> 手 · 单笔上限 ${pv.max_lots} · 批量上限 ${pv.max_total_lots}${pv.blocked ? ' · <span class="down-t">超过批量上限，将被整批拦截</span>' : ""}</p>`;
  const sltp = (Number(order.sl) || Number(order.tp)) ? `，SL ${order.sl || 0} / TP ${order.tp || 0}（${order.sltp_mode === "points" ? "点数" : "价格"}）` : "";
  confirmBox({
    title: `批量${KIND_LABEL[order.kind]} ${order.symbol}`, wide: true,
    desc: `<div>将对 ${pv.rows.filter((r) => r.online).length} 个在线账户${KIND_LABEL[order.kind]} <b>${esc(order.symbol)}</b>${order.kind.includes("_") ? ` @ ${esc(order.price)}` : ""}${sltp}，魔术号 ${esc(order.magic)}。</div>`,
    body: table, label: "确认下单", tone: order.kind.startsWith("buy") ? "up" : "danger",
    run: () => { runBatch("批量下单", "/api/trade/order", { ids: selIds(), order }); },
  });
}
const CLOSE_TITLE = { all: "全平", long: "平多", short: "平空", symbol: "按品种平", magic: "按魔术号平", profit: "平盈利单", loss: "平亏损单" };
function closeBy(mode) {
  if (!needSel("请先在账号页勾选账户")) return;
  const f = { mode };
  if (mode === "symbol") f.symbol = $("#cSymbol").value;
  if (mode === "magic") { if (!/^\d+$/.test($("#cMagic").value)) return toast("魔术号需为整数", true); f.magic = Number($("#cMagic").value); }
  const sel = selAccounts(), online = sel.filter((a) => a.enabled && a.link === "online");
  const match = (p) => mode === "long" ? p.side === "buy" : mode === "short" ? p.side === "sell" : mode === "symbol" ? p.symbol === f.symbol || p.symbol.startsWith(f.symbol) : mode === "magic" ? p.magic === f.magic : mode === "profit" ? p.profit > 0 : mode === "loss" ? p.profit < 0 : true;
  const ps = online.flatMap((a) => a.positions.filter(match));
  if (!ps.length) return toast("在线账户里没有符合条件的持仓", true);
  const pnl = ps.reduce((s, p) => s + p.profit, 0), off = sel.length - online.length;
  confirmBox({
    title: CLOSE_TITLE[mode] + (f.symbol ? ` ${f.symbol}` : "") + (mode === "magic" ? ` #${f.magic}` : ""),
    desc: `将在 ${online.length} 个在线账户平掉 ${ps.length} 笔，当前浮盈 ${sfmt(pnl)}。${off ? `${off} 个未登录/停用账户会记为失败。` : ""}`,
    label: "确认平仓", tone: "danger",
    run: () => { runBatch(CLOSE_TITLE[mode], "/api/trade/close", { ids: selIds(), filter: f }); },
  });
}
function closeTicket(accId, ticket) {
  const a = ui.state.accounts.find((x) => x.id === accId); const p = a?.positions.find((x) => String(x.ticket) === ticket);
  if (!a || !p) return;
  confirmBox({ title: `平仓 #${ticket}`, desc: `${esc(a.alias)} · ${esc(p.symbol)} ${p.side === "buy" ? "多" : "空"} ${p.volume} 手，当前盈亏 ${sfmt(p.profit)}。`,
    label: "确认平仓", tone: "danger",
    run: () => { runBatch("平单笔", "/api/trade/close", { ids: [accId], filter: { mode: "ticket", ticket: Number(ticket) } }); } });
}
function modifyAll() {
  if (!needSel()) return;
  const scope = $("#mScope").value, sl = $("#mSL").value.trim(), tp = $("#mTP").value.trim();
  if (sl === "" && tp === "") return toast("止损和止盈至少填一个", true);
  if ((sl && isNaN(Number(sl))) || (tp && isNaN(Number(tp)))) return toast("止损/止盈需为数字", true);
  const modify = { mode: scope ? "symbol" : "all", symbol: scope, sl: sl === "" ? null : Number(sl), tp: tp === "" ? null : Number(tp), sltp_mode: $("#mUnit").value };
  confirmBox({ title: "批量改止损/止盈", desc: `范围：${scope || "全部持仓"}；SL ${sl === "" ? "不改" : sl}，TP ${tp === "" ? "不改" : tp}（${modify.sltp_mode === "points" ? "点数" : "价格"}）。`,
    label: "确认修改", run: () => { runBatch("改止损止盈", "/api/trade/modify", { ids: selIds(), modify }); } });
}

// ---------------- 策略页 ----------------
function renderStrategy() {
  const st = ui.state;
  once("lib", [st.library], () => {
    const ex5 = st.library.filter((n) => n.toLowerCase().endsWith(".ex5")), sets = st.library.filter((n) => n.toLowerCase().endsWith(".set"));
    $("#libList").innerHTML = ex5.length || sets.length
      ? ex5.map((n) => `<button class="btn" data-lib="${esc(n)}" title="分发这个 EA">${esc(n)}</button>`).join("") + sets.map((n) => `<span class="chip"><span class="muted">参数</span>${esc(n)}</span>`).join("")
      : `<span class="small muted">EA 库还是空的，先上传 .ex5 文件。</span>`;
  });
  const sel = selAccounts();
  once("strats", [sel.map((a) => [a.id, a.link, a.strategies, a.terminal?.trade_allowed])], () => {
    if (!sel.length) {
      $("#stratList").innerHTML = `<div class="empty"><h2>还没有勾选账户</h2><p>分发不需要先勾选（在分发窗口里选账户）。勾选账户后，这里会显示每个终端上的策略，可以单独停止或移除。</p><div class="row"><button class="btn primary" data-tab="accounts">去勾选账户</button></div></div>`;
      return;
    }
    $("#stratList").innerHTML = `<div style="display:flex;flex-direction:column;gap:12px">${sel.map((a) => `<article class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px"><b>${esc(a.alias)}</b><span class="row">${termTag(a)}${linkBadge(a)}</span></div>
      ${a.strategies.length ? `<div style="display:flex;flex-direction:column;gap:8px;margin-top:12px">${a.strategies.map((s) => `<div class="strat"><div style="min-width:0"><div style="font-weight:500">${esc(s.fileName)}</div>
        <div class="sub">${esc(s.symbol)} ${esc(s.timeframe)} · 魔术号 ${s.magic} · ${s.running ? (a.link === "online" ? '<span class="up-t">运行中</span>' : "待连接") : "已停止"}${s.preset ? ` · ${esc(s.preset)}` : ""}</div></div>
        <div class="row"><button class="btn" data-strat="${a.id}:${s.id}:${s.running ? "stop" : "start"}">${s.running ? "停止" : "启动"}</button><button class="btn ghost" data-strat="${a.id}:${s.id}:remove">移除</button></div></div>`).join("")}</div>`
      : `<p class="small muted" style="margin:8px 0 0">未部署</p>`}</article>`).join("")}</div>`;
  });
}
async function uploadEA(files) {
  for (const f of files) {
    const fd = new FormData(); fd.append("file", f);
    try { const r = await api("/api/strategy/upload", fd); toast(`已上传 ${r.name}`); if (r.name.toLowerCase().endsWith(".ex5")) ui.distFile = r.name; else ui.distPreset = r.name; }
    catch (e) { toast(`${f.name}：${e.message}`, true); }
  }
  ui.lastRender.lib = null; poll();
}
function distForm(file) {
  const st = ui.state, accs = st.accounts.filter((a) => a.enabled);
  const ex5 = st.library.filter((n) => n.toLowerCase().endsWith(".ex5")), sets = st.library.filter((n) => n.toLowerCase().endsWith(".set"));
  if (!ex5.length && !st.mock) return toast("EA 库还是空的，请先上传 .ex5 文件", true);
  if (!accs.length) return toast("还没有可用的账户，请先在账号页「添加并登录」", true);
  const pick = new Set(selIds().filter((i) => accs.some((a) => a.id === i)));
  if (!pick.size) accs.filter((a) => a.link === "online").forEach((a) => pick.add(a.id));
  const groups = [...new Set(accs.map((a) => a.group))];
  const fileSel = file || ui.distFile || ex5[0] || "";
  const setOpts = (v, none) => `<option value="">${none}</option>` + sets.map((n) => `<option ${n === v ? "selected" : ""}>${esc(n)}</option>`).join("");
  const s0 = st.settings;
  modal({
    title: "分发 EA", wide: true,
    desc: `<div class="warnbar" style="margin:0 0 8px">⚠ <b>实盘</b>：分发后 EA 会在所选账户上自动交易。</div>点「分发」后每个账户自动：<div class="flow" style="margin-top:6px"><b>登录</b>→<b>复制 EA 文件</b>→<b>重启终端</b>→<b>挂载 EA</b>→<b>校验</b></div>`,
    body: `<div class="grid2">
        <label class="field"><span>EA（.ex5）</span><select class="input" id="dFile">${(ex5.length ? ex5 : [fileSel]).map((n) => `<option ${n === fileSel ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></label>
        <label class="field"><span>参数文件（可选，.set）</span><select class="input" id="dPreset">${setOpts(ui.distPreset || "", "不用参数文件（EA 默认参数）")}</select></label>
        <label class="field"><span>品种（券商实际名称，如 USDJPYc）</span><input class="input num" id="dSymbol" list="symList" value="${esc(s0.default_symbol || "USDJPYc")}"></label>
        <label class="field"><span>周期</span><select class="input" id="dTf">${st.timeframes.map((t) => `<option ${t === (s0.default_timeframe || "M15") ? "selected" : ""}>${t}</option>`).join("")}</select></label>
        <label class="field"><span>魔术号（起始值）</span><input class="input num" id="dMagic" value="${esc(s0.default_magic || 880000)}" inputmode="numeric"></label>
        <label class="field"><span>魔术号参数名（可选，填了才会写进每个账户的参数文件）</span><input class="input num" id="dMagicParam" placeholder="例如 MagicNumber"></label>
      </div>
      <div class="row"><label class="check"><input type="checkbox" id="dStep" checked>每个账户魔术号自动 +1</label>
        <label class="check"><input type="checkbox" id="dReplace" checked>替换该账户上已有的同名 EA（避免重复挂载）</label>
        <label class="check">同时处理 <select class="input" id="dConc" style="width:72px;height:36px">${[1, 2, 3, 4].map((n) => `<option ${n === (s0.dist_concurrency || 2) ? "selected" : ""}>${n}</option>`).join("")}</select> 个账户</label></div>
      <div><div class="row" style="justify-content:space-between;margin-bottom:6px"><span class="small muted">账户（<b id="dCount" class="num">0</b> 个）</span>
        <span class="row"><button class="btn sm" type="button" data-dpick="online">全部在线</button><button class="btn sm" type="button" data-dpick="all">全部</button>${groups.map((g) => `<button class="btn sm" type="button" data-dpick="g:${esc(g)}">${esc(g)}</button>`).join("")}<button class="btn sm ghost" type="button" data-dpick="none">清空</button></span></div>
        <div class="pick" id="dList">${accs.map((a) => `<label><input type="checkbox" data-dacc="${a.id}" ${pick.has(a.id) ? "checked" : ""}><span class="nm">${esc(a.alias)} <span class="sub num">${esc(a.login)}</span></span><span class="sub">${esc(a.group)} · ${a.link === "online" ? '<span class="up-t">在线</span>' : "未登录，会先自动登录"}</span></label>`).join("")}</div></div>
      <div id="dGroups"></div>
      <p class="small muted" style="margin:0" id="dSummary"></p>`,
    actions: [{ label: "取消" }, {
      label: "分发", tone: "primary", run: async (root) => {
        const ids = [...pick];
        if (!ids.length) { toast("请至少选择一个账户", true); return true; }
        const strategy = { fileName: $("#dFile", root).value, preset: $("#dPreset", root).value, symbol: $("#dSymbol", root).value.trim(), timeframe: $("#dTf", root).value, magic: $("#dMagic", root).value.trim() };
        if (!strategy.fileName) { toast("请选择 EA", true); return true; }
        if (!strategy.symbol) { toast("请填写品种", true); return true; }
        if (!/^\d+$/.test(strategy.magic)) { toast("魔术号需为整数", true); return true; }
        const group_presets = {}; $$("[data-gset]", root).forEach((el) => { if (el.value) group_presets[el.dataset.gset] = el.value; });
        const body = { ids: accs.filter((a) => pick.has(a.id)).map((a) => a.id), strategy, group_presets, magic_step: $("#dStep", root).checked ? 1 : 0,
          magic_param: $("#dMagicParam", root).value.trim(), replace: $("#dReplace", root).checked, concurrency: Number($("#dConc", root).value) };
        ui.distFile = strategy.fileName; ui.distPreset = strategy.preset;
        startProgress("分发策略", "/api/strategy/distribute", body);
        return true;
      },
    }],
    onMount: (root) => {
      const sync = () => {
        $$("[data-dacc]", root).forEach((el) => { el.checked = pick.has(el.dataset.dacc); });
        const chosen = accs.filter((a) => pick.has(a.id)), off = chosen.filter((a) => a.link !== "online").length;
        $("#dCount", root).textContent = chosen.length;
        const gs = [...new Set(chosen.map((a) => a.group))];
        const keep = {}; $$("[data-gset]", root).forEach((el) => { keep[el.dataset.gset] = el.value; });
        $("#dGroups", root).innerHTML = gs.length > 1 && sets.length ? `<div class="small muted" style="margin-bottom:6px">按分组使用不同参数文件（例如不同手数），不选 = 用上面的参数文件</div><div class="grid2">${gs.map((g) => `<label class="field"><span>分组「${esc(g)}」</span><select class="input" data-gset="${esc(g)}">${setOpts(keep[g] || "", "同上")}</select></label>`).join("")}</div>` : "";
        const m = Number($("#dMagic", root).value) || 0, step = $("#dStep", root).checked;
        $("#dSummary", root).textContent = chosen.length ? `将分发到 ${chosen.length} 个账户${off ? `（其中 ${off} 个未登录，会先自动登录）` : ""}；魔术号 ${step && chosen.length > 1 ? `${m} ~ ${m + chosen.length - 1}` : m}。每个终端会关闭并重新打开一次（约 10~30 秒）。` : "还没有选择账户。";
      };
      root.addEventListener("change", (e) => { const id = e.target.dataset?.dacc; if (id) { e.target.checked ? pick.add(id) : pick.delete(id); } sync(); });
      root.addEventListener("input", (e) => { if (e.target.id === "dMagic") sync(); });
      $$("[data-dpick]", root).forEach((b) => b.onclick = () => {
        const v = b.dataset.dpick; pick.clear();
        accs.forEach((a) => { if (v === "all" || (v === "online" && a.link === "online") || (v.startsWith("g:") && a.group === v.slice(2))) pick.add(a.id); });
        sync();
      });
      sync();
    },
  });
}

// ---------------- 收益页 ----------------
function deskScopeIds() { return window.Desk && Desk.filterIds ? Desk.filterIds() : null; }
window.onDeskScope = () => { ui.lastRender.stats = null; if (ui.hist) loadStats(); else if (ui.tab === "stats") renderStats(); };
async function loadStats() {
  const ids = deskScopeIds();
  $("#statsInfo").innerHTML = `<span class="spin"></span> 正在从各终端读取今年的平仓记录…`;
  try { ui.hist = await api("/api/history", ids ? { ids } : {}); ui.histAt = Date.now(); ui.lastRender.stats = null; renderStats(); }
  catch (e) { toast(e.message, true); $("#statsInfo").textContent = ""; }
}
function renderStats() {
  const scopeIds = deskScopeIds();
  const si = $("#statsScopeInfo"); if (si) si.textContent = scopeIds ? `范围：已选 ${scopeIds.length} 个账户（顶部「选择账户」修改）` : "范围：全部账户（顶部「选择账户」可只看部分账户）";
  const h = ui.hist;
  if (!h) { once("stats", 0, () => { $("#statsBody").innerHTML = `<div class="empty"><h2>还没有读取历史</h2><p>先批量登录，再点「读取平仓历史」。数据来自各终端的成交历史（history_deals_get），按平仓日汇总，含手续费和隔夜利息。</p></div>`; }); return; }
  $("#statsInfo").textContent = `已读取 ${h.accounts} 个在线账户${h.skipped ? `，${h.skipped} 个未登录账户未计入` : ""} · ${new Date(ui.histAt).toLocaleTimeString("zh-CN", { hour12: false })}`;
  once("stats", [ui.histAt, (scopeIds || []).join(",")], () => {
    const deals = h.deals.slice().sort((a, b) => a.time - b.time);
    const now = new Date(), dayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
    const sum = (arr) => arr.reduce((s, d) => s + d.profit, 0);
    const today = sum(deals.filter((d) => d.time >= dayStart));
    const week = sum(deals.filter((d) => d.time >= dayStart - 6 * 86400));
    const monthStart = new Date(now.getFullYear(), now.getMonth(), 1).getTime() / 1000;
    const month = sum(deals.filter((d) => d.time >= monthStart));
    const year = sum(deals);
    const byDay = new Map();
    deals.forEach((d) => { const k = new Date(d.time * 1000); const key = `${k.getFullYear()}-${pad(k.getMonth() + 1)}-${pad(k.getDate())}`; byDay.set(key, (byDay.get(key) || 0) + d.profit); });
    const days = [...byDay.entries()].sort();
    const wins = deals.filter((d) => d.profit > 0), losses = deals.filter((d) => d.profit < 0);
    const gp = sum(wins), gl = -sum(losses);
    const best = days.reduce((m, d) => (d[1] > m[1] ? d : m), ["—", 0]), worst = days.reduce((m, d) => (d[1] < m[1] ? d : m), ["—", 0]);
    const months = Array.from({ length: 12 }, (_, i) => sum(deals.filter((d) => new Date(d.time * 1000).getMonth() === i && new Date(d.time * 1000).getFullYear() === now.getFullYear())));
    const byAcc = new Map(); deals.forEach((d) => byAcc.set(d.alias, (byAcc.get(d.alias) || 0) + d.profit));
    const accRows = [...byAcc.entries()].sort((a, b) => b[1] - a[1]);
    // 曲线
    let cum = 0; const pts = days.map(([k, v]) => { cum += v; return [k, cum]; });
    const W = 800, H = 200, P = 8;
    let svg = `<p class="small muted">今年还没有平仓记录。</p>`;
    if (pts.length > 1) {
      const ys = pts.map((p) => p[1]), mn = Math.min(0, ...ys), mx = Math.max(0, ...ys), rg = mx - mn || 1;
      const X = (i) => P + (i / (pts.length - 1)) * (W - 2 * P), Y = (v) => H - P - ((v - mn) / rg) * (H - 2 * P);
      const d = pts.map((p, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" ");
      const col = cum >= 0 ? "var(--up)" : "var(--down)";
      svg = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:220px;display:block" preserveAspectRatio="none"><line x1="0" x2="${W}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--line)" stroke-dasharray="4 4"/><path d="${d} L${X(pts.length - 1)},${Y(0)} L${X(0)},${Y(0)} Z" fill="${col}" opacity=".12"/><path d="${d}" fill="none" stroke="${col}" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>
        <div class="row small muted" style="justify-content:space-between"><span>${pts[0][0]}</span><span>累计 ${money(cum, true)}</span><span>${pts[pts.length - 1][0]}</span></div>`;
    }
    const mMax = Math.max(1, ...months.map(Math.abs));
    const aMax = Math.max(1, ...accRows.map((r) => Math.abs(r[1])));
    const floats = (ui.state.accounts.filter((a) => a.link === "online" && (!scopeIds || scopeIds.includes(a.id))));
    $("#statsBody").innerHTML = `
      <div class="grid4"><div class="stat"><div class="k">今日已实现</div><div class="v">${money(today, true)}</div></div><div class="stat"><div class="k">近 7 日</div><div class="v">${money(week, true)}</div></div>
      <div class="stat"><div class="k">本月</div><div class="v">${money(month, true)}</div></div><div class="stat"><div class="k">${now.getFullYear()} 年</div><div class="v">${money(year, true)}</div></div></div>
      <div class="panel"><h3>累计已实现收益曲线（按日）</h3><div style="margin-top:8px">${svg}</div></div>
      <div class="grid2">
        <div class="panel"><h3>1–12 月</h3><div class="bars">${months.map((v, i) => `<div class="b" title="${i + 1} 月 ${sfmt(v)}"><span class="num ${v > 0 ? "up-t" : v < 0 ? "down-t" : ""}" style="font-size:10px">${v ? Math.round(v) : ""}</span><i style="height:${(Math.abs(v) / mMax) * 100}%;background:${v >= 0 ? "var(--up)" : "var(--down)"}"></i>${i + 1}</div>`).join("")}</div></div>
        <div class="panel"><h3>分布</h3><div class="grid2" style="margin-top:8px">
          <div><div class="small muted">平仓笔数</div><div class="num">${deals.length}</div></div><div><div class="small muted">胜率</div><div class="num">${deals.length ? ((wins.length / deals.length) * 100).toFixed(1) + "%" : "—"}</div></div>
          <div><div class="small muted">盈亏比（总盈利/总亏损）</div><div class="num">${gl ? (gp / gl).toFixed(2) : wins.length ? "全胜" : "—"}</div></div><div><div class="small muted">交易天数</div><div class="num">${days.length}</div></div>
          <div><div class="small muted">最好日</div><div>${best[0]} ${money(best[1], true)}</div></div><div><div class="small muted">最差日</div><div>${worst[0]} ${money(worst[1], true)}</div></div></div></div>
      </div>
      <div class="grid2">
        <div class="panel"><h3>账户贡献（今年已实现）</h3>${accRows.length ? accRows.map(([n, v]) => `<div class="hbar"><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(n)}</span><span class="track"><i style="left:0;width:${(Math.abs(v) / aMax) * 100}%;background:${v >= 0 ? "var(--up)" : "var(--down)"}"></i></span>${money(v, true)}</div>`).join("") : `<p class="small muted">这个区间还没有平仓。</p>`}</div>
        <div class="panel"><h3>持仓浮动（实时）</h3>${floats.length ? floats.map((a) => `<div class="hbar"><span>${esc(a.alias)}</span><span class="small muted">${a.positionsCount} 笔持仓</span>${money(a.floating, true)}</div>`).join("") : `<p class="small muted">当前范围没有在线账户。</p>`}</div>
      </div>`;
  });
}

// ---------------- 日志页 ----------------
async function loadLogs() {
  try { ui.logs = await api("/api/logs"); } catch (e) { return toast(e.message, true); }
  const list = ui.logFail ? ui.logs.filter((e) => !e.ok) : ui.logs;
  $("#logFailBtn").textContent = ui.logFail ? "查看全部" : "只看失败";
  $("#logFailBtn").className = "btn " + (ui.logFail ? "primary" : "");
  once("logs", [list.length, list[0]?.id, ui.logFail], () => {
    $("#logList").innerHTML = !list.length ? `<div class="empty"><h2>还没有日志</h2><p>登录、下单、平仓、部署策略之后，每一步的成功和失败都会记在这里。</p></div>`
      : `<div class="cards">${list.map((e) => `<article class="card"><div class="row small muted" style="justify-content:space-between"><span class="num">${tstr(e.at)}</span><span>${esc(e.action)} · ${esc(e.mode || "")}</span></div><b>${esc(e.alias)}</b><p class="small ${e.ok ? "" : "down-t"}" style="margin:4px 0 0">${esc(e.message)}</p></article>`).join("")}</div>
      <div class="tablewrap desk"><table><thead><tr><th>时间</th><th>账户</th><th>动作</th><th>模式</th><th>结果</th></tr></thead><tbody>${list.map((e) => `<tr><td class="num small muted" style="white-space:nowrap">${tstr(e.at)}</td><td>${esc(e.alias)}</td><td style="white-space:nowrap">${esc(e.action)}</td><td class="small muted">${esc(e.mode || "")}</td><td class="${e.ok ? "" : "down-t"}">${esc(e.message)}</td></tr>`).join("")}</tbody></table></div>`;
  });
}

// ---------------- 设置页 ----------------
function fillSettings() {
  const s = ui.state?.settings; if (!s) return;
  $("#setAlert").value = s.alert_loss ?? 3000; $("#setMaxLots").value = s.max_lots; $("#setMaxTotal").value = s.max_total_lots;
  $("#setDev").value = s.deviation; $("#setPoll").value = s.poll_interval; $("#setGroups").value = (s.scale_groups || []).join(",");
  $("#setCloseTerm").value = s.close_terminal_on_disconnect ? "1" : "0"; $("#setIni").value = s.ini_encoding || "utf-16";
  $("#setCloseExit").value = s.close_terminals_on_exit ? "1" : "0"; $("#setAlgo").value = s.auto_enable_algo === false ? "0" : "1"; $("#rootPath").textContent = ui.state.terminalsRoot;
  $("#setTpl").value = s.template_dir || ""; $("#setDll").value = s.allow_dll_import ? "1" : "0";
  $("#setLoginConc").value = s.login_concurrency || 3; $("#setDistConc").value = s.dist_concurrency || 2;
  $("#setDefTf").innerHTML = (ui.state.timeframes || []).map((t) => `<option>${t}</option>`).join("");
  $("#setDefSym").value = s.default_symbol || "USDJPYc"; $("#setDefTf").value = s.default_timeframe || "M15";
  $("#setSingle").value = s.single_chart === false ? "0" : "1"; $("#setReattach").value = s.auto_reattach === false ? "0" : "1";
  $("#setQuickNC").value = s.quick_no_confirm ? "1" : "0";
  $("#setUpdRepo").value = s.update_repo ?? "efwe32/mt5-fleet"; $("#setUpdUrl").value = s.update_url || "";
  $("#setUpdMirror").value = s.update_mirror === false ? "0" : "1"; $("#setUpdAuto").value = s.update_auto_check === false ? "0" : "1";
  updRefresh();
  ui.lastRender.setinfo = null; renderSettingsInfo();
  $("#oGroups").value = (s.scale_groups || []).join(",");
  $("#pwInfo").textContent = ui.state.dpapi ? "密码保护：Windows DPAPI 加密" : "密码保护：base64 混淆（当前系统不支持 DPAPI）";
}
async function saveSettings(extra) {
  const body = extra || {
    alert_loss: $("#setAlert").value, max_lots: $("#setMaxLots").value, max_total_lots: $("#setMaxTotal").value, deviation: $("#setDev").value,
    poll_interval: $("#setPoll").value, scale_groups: $("#setGroups").value,
    close_terminal_on_disconnect: $("#setCloseTerm").value === "1", close_terminals_on_exit: $("#setCloseExit").value === "1", auto_enable_algo: $("#setAlgo").value === "1", ini_encoding: $("#setIni").value,
    template_dir: $("#setTpl").value.trim(), allow_dll_import: $("#setDll").value === "1",
    login_concurrency: $("#setLoginConc").value, dist_concurrency: $("#setDistConc").value,
    default_symbol: $("#setDefSym").value.trim(), default_timeframe: $("#setDefTf").value,
    single_chart: $("#setSingle").value === "1", auto_reattach: $("#setReattach").value === "1",
    quick_no_confirm: $("#setQuickNC").value === "1",
    update_repo: $("#setUpdRepo").value.trim(), update_url: $("#setUpdUrl").value.trim(),
    update_mirror: $("#setUpdMirror").value === "1", update_auto_check: $("#setUpdAuto").value === "1",
  };
  try { await api("/api/settings", body); toast("设置已保存"); await poll(); fillSettings(); } catch (e) { toast(e.message, true); }
}
// ---------------- 在线更新 ----------------
const upd = { newer: false, check: null, status: null, timer: null, restarting: false };
const UPD_STATE = { checking: "检查中", downloading: "下载中", verifying: "校验中", deps: "安装依赖", backup: "备份中", installing: "替换文件", testing: "自检中", restarting: "重启中", error: "失败", uptodate: "已是最新" };
function updRenderCheck() {
  const c = upd.check, el = $("#updResult"); if (!el) return;
  const st = upd.status || {};
  $("#updCur").textContent = `当前 v${st.current || ""}${st.layout ? " · " + st.layout : ""}`;
  const btn = $("#updInstallBtn");
  btn.disabled = !(c && c.ok && c.newer) || upd.restarting || (st.state && !["idle", "error", "uptodate"].includes(st.state));
  btn.textContent = c && c.ok && c.newer ? `立即更新到 v${c.latest}` : "立即更新";
  $("#updRollbackBtn").disabled = !(st.backups || []).length;
  if (!c) { el.innerHTML = '<p class="small muted" style="margin:0">点「检查更新」查看有没有新版本。</p>'; return; }
  if (c.loading) { el.innerHTML = '<p class="small muted" style="margin:0">正在检查…</p>'; return; }
  if (!c.ok) { el.innerHTML = `<div class="note bad">检查更新失败：${esc((c.errors || []).join("；"))}<br>可以稍后再试；如果 GitHub 一直打不开，可以在下面「更新源设置」里填一个自定义更新地址。</div>`; return; }
  const when = c.checked_at ? new Date(c.checked_at * 1000).toLocaleTimeString("zh-CN", { hour12: false }) : "";
  const head = c.newer ? `<b class="up-t">有新版本 v${esc(c.latest)}</b>（当前 v${esc(c.current)}）` : `<b>已经是最新版本 v${esc(c.current)}</b>`;
  const meta = [c.released ? `发布：${esc(c.released)}` : "", `来源：${esc(c.source || "")}`, c.api_ms ? `响应 ${c.api_ms} ms` : "", c.size ? `更新包 ${(c.size / 1024).toFixed(0)} KB` : "", when ? `检查于 ${when}` : ""].filter(Boolean).join(" · ");
  const req = c.newer && c.requirements_changed ? '<div class="note bad" style="margin-top:8px">这个版本需要安装新的依赖（会自动用本机 Python 安装；便携版没有 pip，需下载完整便携版）。</div>' : "";
  el.innerHTML = `<p style="margin:0">${head}</p><p class="small muted" style="margin:4px 0 0">${meta}${c.release_url ? ` · <a href="${esc(c.release_url)}" target="_blank" rel="noopener">发布页</a>` : ""}</p>`
    + (c.notes ? `<div class="small muted" style="margin-top:8px">更新内容：</div><div class="upd-notes">${esc(c.notes)}</div>` : "") + req;
}
function updRenderStatus() {
  const st = upd.status; if (!st || !$("#updProgress")) return;
  const active = st.state && !["idle", "uptodate"].includes(st.state);
  $("#updProgress").classList.toggle("hidden", !active && !(st.log || []).length);
  $("#updBar").style.width = `${st.state === "error" ? 100 : st.progress || 0}%`;
  $("#updBar").style.background = st.state === "error" ? "var(--down)" : "";
  $("#updMsg").innerHTML = `${UPD_STATE[st.state] ? `<b>${UPD_STATE[st.state]}</b> · ` : ""}${esc(st.message || "")}`;
  $("#updLog").textContent = (st.log || []).join("\n");
  $("#updLog").scrollTop = 1e6;
  updRenderCheck();
}
async function updRefresh() {
  try { upd.status = await api("/api/update/status"); if (upd.status.check && !upd.check) { upd.check = upd.status.check; upd.newer = !!(upd.check.ok && upd.check.newer); } updRenderStatus(); } catch {}
}
async function updCheck(force, quiet) {
  if (!quiet) { upd.check = { loading: true }; updRenderCheck(); }
  try {
    const c = await api(`/api/update/check${force ? "?force=1" : ""}`);
    upd.check = c; const was = upd.newer; upd.newer = !!(c.ok && c.newer);
    if (was !== upd.newer) renderNav();
    if (!quiet) { c.ok ? toast(c.newer ? `有新版本 v${c.latest}` : "已经是最新版本") : toast("检查更新失败", true); }
  } catch (e) { if (!quiet) { upd.check = { ok: false, errors: [e.message] }; toast(e.message, true); } }
  updRenderCheck();
}
function updInstall() {
  const c = upd.check; if (!(c && c.ok && c.newer)) return;
  modal({ title: `更新到 v${c.latest}？`, desc: `会下载新版本、校验、备份当前程序文件，然后只替换程序文件并<b>自动重启后台</b>（大约 10~30 秒，网页会自动刷新）。<br>账户、设置、EA 库、MT5 终端都不动，<b>MT5 终端和 EA 继续运行</b>；重启的这几十秒里面板暂时不刷新数据，「自动全平」监控在新版本启动后继续。<br>新版本启动失败会自动恢复到 v${esc(c.current)}。`,
    actions: [{ label: "取消" }, { label: "开始更新", tone: "primary", run: async () => {
      try { upd.status = await api("/api/update/install", {}); updRenderStatus(); updWatch(); } catch (e) { toast(e.message, true); }
    } }] });
}
function updRollback() {
  const b = (upd.status?.backups || [])[0]; if (!b) return toast("没有可恢复的备份", true);
  const list = (upd.status.backups || []).map((x) => `<option value="${esc(x.name)}">v${esc(x.version)} · ${esc(x.name)} · ${x.files} 个文件</option>`).join("");
  modal({ title: "恢复到以前的版本", desc: "把程序文件恢复成备份里的版本（当前版本也会先备份一份），然后自动重启后台。账户、设置、MT5 终端和 EA 都不受影响。",
    body: `<label class="field"><span>选择备份</span><select class="input" id="updBk">${list}</select></label>`,
    actions: [{ label: "取消" }, { label: "恢复并重启", tone: "danger", run: async () => {
      const name = $("#updBk").value;
      try { upd.status = await api("/api/update/rollback", { name }); updRenderStatus(); updWatch(); } catch (e) { toast(e.message, true); }
    } }] });
}
async function updWatch() {
  clearTimeout(upd.timer);
  try { upd.status = await api("/api/update/status"); updRenderStatus(); } catch {}
  const st = upd.status || {};
  if (st.state === "restarting") return updWaitRestart();
  if (st.state === "error") { toast(st.message, true); return; }
  if (st.state === "uptodate") { toast(st.message); updCheck(true, true); return; }
  if (["checking", "downloading", "verifying", "deps", "backup", "installing", "testing"].includes(st.state)) upd.timer = setTimeout(updWatch, 700);
}
async function updWaitRestart() {
  if (upd.restarting) return; upd.restarting = true;
  let oldPid = null;
  try { oldPid = (await (await fetch("/ping", { cache: "no-store" })).json()).pid; } catch {}
  const ov = document.createElement("div"); ov.className = "upd-overlay";
  ov.innerHTML = '<div><h2 style="margin:0 0 8px">正在重启后台…</h2><p class="small muted" id="updOvMsg">新版本已安装，后台正在重启（MT5 终端和 EA 继续运行）。网页会自动刷新，请不要关闭。</p></div>';
  document.body.appendChild(ov);
  const t0 = Date.now();
  const tick = async () => {
    try {
      const r = await fetch("/ping", { cache: "no-store" });
      if (r.ok) { const j = await r.json(); if (oldPid && j.pid !== oldPid) { $("#updOvMsg").textContent = `新后台已启动（v${j.version}），正在刷新网页…`; setTimeout(() => location.reload(), 800); return; } }
    } catch {}
    const sec = Math.round((Date.now() - t0) / 1000);
    if (sec > 150) { $("#updOvMsg").innerHTML = "等了很久还没连上新后台。请双击程序文件夹里的「一键启动.vbs」，或查看 data\\logs\\update-helper.log。"; return; }
    $("#updOvMsg").textContent = `新版本已安装，后台正在重启（MT5 终端和 EA 继续运行）… ${sec} 秒`;
    setTimeout(tick, 1000);
  };
  setTimeout(tick, 2000);
}
function updAfterReload() {
  const lu = upd.status?.last_update; if (!lu || !lu.at) return;
  const seen = Number(localStorage.getItem("fleet.updSeen") || 0);
  if (lu.at <= seen) return;
  if (lu.status === "ok") toast(`已更新到 v${lu.to}（MT5 终端和 EA 未中断）`);
  else if (lu.status === "rolled_back" || lu.status === "failed") toast(lu.error || "更新失败，已恢复原来的版本", true);
  else return;
  localStorage.setItem("fleet.updSeen", String(lu.at));
}
async function updStartup() {
  await updRefresh();
  updAfterReload();
  if (["checking", "downloading", "verifying", "deps", "backup", "installing", "testing"].includes(upd.status?.state)) updWatch();
  if (ui.state?.settings?.update_auto_check !== false) setTimeout(() => updCheck(false, true), 4000);
}

function renderSettingsInfo() {
  const st = ui.state; if (!st) return;
  once("setinfo", [st.template, st.serversImport, st.serversLib, st.serversPublished], () => {
    const t = st.template || {};
    const build = t.version ? ` · MT5 build ${esc(t.version.split(".").pop())}` : "";
    const tsrv = t.servers > 0 ? ` · ${t.servers} 个服务器` : "";
    $("#tplInfo").innerHTML = t.path ? `<span class="up-t">✓ 模板：</span><span class="num">${esc(t.path)}</span>${t.auto ? "（自动找到）" : ""}<span class="muted">${build}${tsrv}</span>${t.running ? '<span class="muted"> · 这份 MT5 正在运行，复制时只读取文件，不会关闭或改动它</span>' : ""}`
      : `<span class="down-t">${esc(t.error || "没有找到模板 MT5")}</span>`;
    const si = st.serversImport;
    const pub = st.serversPublished > 0 ? `随程序发布 <b>${st.serversPublished}</b> 个服务器（含 42 及以上，更新后自动放入）。` : "";
    const tplLine = t.servers > 0 ? `模板里有 <b>${t.servers}</b> 个服务器（新建的终端会带上）。` : "";
    $("#srvInfo").innerHTML = si ? `${pub}${tplLine}<span class="up-t">✓ 正在使用 ${st.serversLib > 0 ? st.serversLib + " 个服务器" : ""}</span>：<span class="num">${esc(si.source)}</span> · ${new Date(si.at * 1000).toLocaleString("zh-CN", { hour12: false })}`
      : `<span class="muted">${pub}${tplLine}找不到服务器时点「一键修复服务器列表」。</span>`;
  });
}
function importServersDialog() {
  let chosen = "";
  modal({
    title: "从已有 MT5 导入服务器列表", wide: true,
    desc: "选择一份已经登录过你券商账户的 MT5（例如桌面上在用的那份），列表按服务器数量从多到少排列。只会<b>只读复制</b>它的 servers.dat 到本程序，不会改动、关闭那份 MT5。",
    body: `<div id="srcList"><p class="small muted"><span class="spin"></span> 正在查找电脑上的 MT5…</p></div>
      <label class="field"><span>或手动填写 MT5 文件夹（安装目录，或在 MT5 里「文件 → 打开数据文件夹」打开的文件夹）</span>
        <div class="split"><input class="input" id="srcPath" placeholder="例如 D:\\MT5\\IC Markets"><button class="btn" type="button" id="srcCheck">检测</button></div></label>
      <div class="small" id="srcInfo"></div>`,
    actions: [{ label: "取消" }, {
      label: "导入", tone: "primary", run: async (root) => {
        const path = $("#srcPath", root).value.trim() || chosen;
        if (!path) { toast("请选择或填写一个 MT5 文件夹", true); return true; }
        try { const r = await api("/api/tools/import_servers", { path }); toast(r.message); poll(); }
        catch (e) { $("#srcInfo", root).innerHTML = `<span class="down-t">${esc(e.message)}</span>`; return true; }
      },
    }],
    onMount: async (root) => {
      $("#srcCheck", root).onclick = async () => {
        const path = $("#srcPath", root).value.trim(); if (!path) return;
        try { const r = await api("/api/tools/servers_check", { path });
          $("#srcInfo", root).innerHTML = r.items.length ? `<span class="up-t">找到 ${r.items.length} 份 servers.dat</span>，将导入服务器最多的一份${r.items[0].count > 0 ? `（${r.items[0].count} 个服务器）` : ""}：<span class="num">${esc(r.items[0].path)}</span>` : `<span class="down-t">这个文件夹里没有找到 Config\\servers.dat</span>`;
        } catch (e) { toast(e.message, true); }
      };
      try {
        const r = await api("/api/tools/mt5_sources");
        const box = $("#srcList", root); if (!box) return;
        const items = r.items.filter((x) => x.servers);
        box.innerHTML = items.length ? `<div class="small muted" style="margin-bottom:6px">找到 ${items.length} 份带服务器列表的 MT5：</div><div style="display:flex;flex-direction:column;gap:6px">${items.map((x, k) => `<label class="src"><input type="radio" name="src" value="${esc(x.servers.path)}" ${k ? "" : "checked"}><div style="min-width:0"><div class="p">${esc(x.path)}</div><div class="sub">${esc(x.kind)}${x.origin ? ` · 对应 ${esc(x.origin)}` : ""} · ${x.servers.count > 0 ? `<b>${x.servers.count} 个服务器</b>` : `servers.dat ${x.servers.size} 字节`} · ${new Date(x.servers.mtime * 1000).toLocaleString("zh-CN", { hour12: false })}${x.running ? " · 正在运行（只读复制，不影响它）" : ""}</div></div></label>`).join("")}</div>`
          : `<p class="small muted">没有自动找到其它 MT5，请在下面手动填写文件夹。</p>`;
        chosen = items[0]?.servers.path || "";
        box.addEventListener("change", (e) => { if (e.target.name === "src") chosen = e.target.value; });
      } catch (e) { const box = $("#srcList", root); if (box) box.innerHTML = `<p class="small down-t">${esc(e.message)}</p>`; }
    },
  });
}
async function fixServers(btn) {
  if (btn) { btn.disabled = true; btn.dataset.txt = btn.textContent; btn.textContent = "正在查找…"; }
  try {
    const r = await api("/api/tools/fix_servers", {});
    await poll(); ui.lastRender.setinfo = null; renderSettingsInfo();
    const retry = (r.retry || []).filter((i) => ui.state.accounts.some((a) => a.id === i));
    if (!retry.length) { modal({ title: "服务器列表已修复", desc: esc(r.message), actions: [{ label: "好", tone: "primary" }] }); return; }
    const names = retry.map((i) => ui.state.accounts.find((a) => a.id === i)?.alias).filter(Boolean);
    modal({
      title: "服务器列表已修复", desc: `${esc(r.message)}<p style="margin-top:10px">现在重新登录这 <b>${retry.length}</b> 个账户？<br><span class="small muted">${esc(names.join("、"))}</span></p>`,
      actions: [{ label: "稍后" }, { label: "重新登录", tone: "primary", run: () => { startProgress("重新登录", "/api/login", { ids: retry }); return true; } }],
    });
  } catch (e) { toast(e.message, true); }
  finally { if (btn) { btn.disabled = false; btn.textContent = btn.dataset.txt || "一键修复服务器列表"; } }
}
function welcome() {
  modal({ title: "欢迎使用 MT5 批量终端", desc: `<p>本程序为<b class="down-t">实盘</b>：批量下单、平仓、改单、部署策略都会真实发送到券商，每次执行前都会弹出确认，并受单笔 / 批量最大手数限制（设置页可改）。</p>
    <ol class="small muted" style="padding-left:18px;line-height:1.8"><li>账号页点「添加并登录」：填账号、交易密码、服务器（可一次粘贴多个）。程序自动为每个账户复制一份独立的 MT5 到 <span class="num">terminals\\&lt;账号&gt;</span>、启动、登录并打开算法交易；不会碰你桌面上正在运行的 MT5。</li><li>策略页上传 EA（.ex5），点「分发」，选好账户、品种、周期、魔术号，自动挂到每个账户的图表上运行。</li><li>交易页可以批量下单 / 平仓；收益页看实时收益。</li></ol>`,
    actions: [{ label: "开始使用", tone: "primary", run: () => { api("/api/settings", {}).catch(() => {}); } }] });
}

// ---------------- 事件 ----------------
document.addEventListener("click", async (e) => {
  const t = e.target.closest("button,[data-sel],#selAll"); if (!t) return;
  const d = t.dataset;
  if (d.tab) return go(d.tab);
  if (d.group !== undefined) { ui.group = d.group; ui.lastRender = {}; return render(); }
  if (d.edit) return accountForm(ui.state.accounts.find((a) => a.id === d.edit));
  if (d.toggle) {
    const a = ui.state.accounts.find((x) => x.id === d.toggle);
    const run = async () => { try { await api(`/api/accounts/${d.toggle}/toggle`, {}); poll(); } catch (er) { toast(er.message, true); } };
    if (a && a.enabled && a.link === "online") return modal({ title: `停用 ${a.alias}？`, desc: "停用会断开这个终端的连接。终端本身和里面运行的 EA 不受影响（要停 EA 请用策略页）。", actions: [{ label: "取消" }, { label: "停用", tone: "danger", run }] });
    return run();
  }
  if (d.lib) { ui.distFile = d.lib; return distForm(d.lib); }
  if (d.closeTicket) { const [a, tk] = d.closeTicket.split(":"); return closeTicket(a, tk); }
  if (d.close) return closeBy(d.close);
  if (d.scope) { ui.statsScope = d.scope; ui.lastRender.stats = null; return loadStats(); }
  if (d.strat) {
    const [acc, sid, action] = d.strat.split(":");
    const a = ui.state.accounts.find((x) => x.id === acc), s = a?.strategies.find((x) => x.id === sid);
    const label = { start: "启动策略", stop: "停止策略", remove: "移除策略" }[action];
    return confirmBox({ title: `${label} ${s?.fileName || ""}`, desc: `${esc(a?.alias)}：${action === "start" ? "会重启终端并把 EA 挂到图表上。" : action === "stop" ? "会重启终端并从图表上移除这个 EA（同名 EA 在多个图表上会一起移除）。" : "会先停止（如果在运行），再从台账删除记录。不会删除终端里的 .ex5 文件。"}`,
      label, tone: action === "start" ? "primary" : "danger", run: () => { if (action === "remove") { runBatch(label, `/api/strategy/${acc}/${sid}/${action}`, {}); return; } startProgress(label, `/api/strategy/${acc}/${sid}/${action}`, {}); return true; } });
  }
  switch (d.act) {
    case "banner-x": ui.bannerDismissed = ui.state.banner?.at; ui.lastRender.banner = null; return render();
    case "alert-view": {
      const id = t.dataset.alert; if (!id) return;
      try { await api("/api/alerts/view", { id }); ui.alertPopupHidden = ui.state?.alertPopup?.at; ui.lastRender.banner = null; await poll(); } catch (er) { toast(er.message, true); }
      return;
    }
    case "login": if (needSel()) startProgress("批量登录", "/api/login", { ids: selIds() }); return;
    case "disconnect": if (needSel()) runBatch("断开", "/api/disconnect", { ids: selIds() }); return;
    case "refresh": if (needSel()) runBatch("刷新", "/api/refresh", { ids: selIds() }); return;
    case "add": return accountForm(null);
    case "quick": return quickForm();
    case "distribute": return distForm();
    case "import-servers": return importServersDialog();
    case "fix-servers": return fixServers(t);
    case "tpl-check": {
      try { await api("/api/settings", { template_dir: $("#setTpl").value.trim() }); const t = await api("/api/tools/template", {}); await poll(); ui.lastRender.setinfo = null; renderSettingsInfo(); t.path ? toast("模板可用") : toast(t.error, true); }
      catch (er) { toast(er.message, true); }
      return;
    }
    case "import": return $("#csvFile").click();
    case "example": return (location.href = `/api/accounts/example.csv?token=${encodeURIComponent(window.FLEET_TOKEN)}`);
    case "delete":
      if (!needSel()) return;
      return modal({ title: "删除已选账户", desc: `将从本机台账删除 ${selIds().length} 个账户（含加密保存的密码）。在线的会先断开。不会删除 MT5 文件夹。`, actions: [{ label: "取消" }, { label: "删除", tone: "danger", run: async () => { await runBatch("删除", "/api/accounts/delete", { ids: selIds() }); } }] });
    case "order-buy": return placeOrder("buy");
    case "order-sell": return placeOrder("sell");
    case "order": return placeOrder();
    case "cancel-orders": {
      if (!needSel()) return;
      const n = selAccounts().reduce((s, a) => s + (a.orders || []).length, 0);
      if (!n) return toast("已选在线账户没有挂单", true);
      return confirmBox({ title: "撤销全部挂单", desc: `将撤销 ${n} 个挂单。`, label: "确认撤单", tone: "danger", run: () => { runBatch("撤挂单", "/api/trade/cancel", { ids: selIds() }); } });
    }
    case "modify": return modifyAll();
    case "upload": return $("#eaFile").click();
    case "stop-strats": if (needSel("请先在账号页勾选账户")) confirmBox({ title: "一键停止已勾选账户的策略", desc: `${selIds().length} 个账户：会重启有运行中策略的终端，并从图表上移除这些 EA。`, label: "停止", tone: "danger", run: () => { startProgress("停止策略", "/api/strategy/stop", { ids: selIds() }); return true; } }); return;
    case "strip-strats": if (needSel("请先在账号页勾选账户")) confirmBox({ title: "一键移除已勾选账户的策略", desc: `${selIds().length} 个账户：先停止运行中的 EA，再从台账删掉这些账户上的全部策略记录。不会删除终端里的 .ex5 文件。`, label: "移除", tone: "danger", run: () => { startProgress("移除策略", "/api/strategy/strip", { ids: selIds() }); return true; } }); return;
    case "stats-refresh": return loadStats();
    case "log-fail": ui.logFail = !ui.logFail; ui.lastRender.logs = null; return loadLogs();
    case "log-csv": return (location.href = `/api/logs.csv?token=${encodeURIComponent(window.FLEET_TOKEN)}`);
    case "log-clear": await api("/api/logs/clear", {}); ui.lastRender.logs = null; return loadLogs();
    case "save-settings": return saveSettings();
    case "upd-check": return updCheck(true);
    case "upd-install": return updInstall();
    case "upd-rollback": return updRollback();
    case "clone": {
      if (!needSel()) return;
      const src = $("#setTpl").value.trim() || ui.state.template?.path || "";
      return modal({ title: "创建终端副本", desc: `将把 ${esc(src || ui.state.terminalsRoot + "\\base")} 复制到 ${esc(ui.state.terminalsRoot)}\\账号\\（共 ${selIds().length} 个），已存在的跳过。只读取模板目录，不会改动它。`, actions: [{ label: "取消" }, { label: "开始复制", tone: "primary", run: async () => {
        try { const r = await api("/api/tools/clone", { src, ids: selIds() }); const bad = r.results.filter((x) => !x.ok); bad.length ? toast(bad[0].message, true) : toast(`完成 ${r.results.length} 个`); poll(); } catch (er) { toast(er.message, true); } } }] });
    }
    case "close-owned":
      return modal({ title: "关闭本程序启动的终端？", desc: "会断开所有连接，并正常关闭由本程序启动（登记了 PID）的 MT5 终端，里面的 EA 会停止。你自己在桌面打开的 MT5 不受影响。", actions: [{ label: "取消" }, { label: "关闭", tone: "danger", run: async () => { try { const r = await api("/api/tools/close_owned", {}); toast(`已关闭 ${r.closed} 个终端`); poll(); } catch (er) { toast(er.message, true); } } }] });
    case "exit-app":
      return modal({ title: "退出 MT5 批量终端？", desc: ui.state.settings.close_terminals_on_exit ? "退出时会关闭本程序启动的终端（EA 随之停止）。桌面上你自己打开的 MT5 不受影响。" : "退出后，本程序启动的终端会继续运行。", actions: [{ label: "取消" }, { label: "退出", tone: "danger", run: async () => { try { await api("/api/exit", {}); } catch {} clearTimeout(pollTimer); document.body.innerHTML = '<div class="empty" style="margin:40px"><h2>MT5 批量终端已退出</h2><p>后台已经结束，可以关闭这个网页了。需要时再双击「一键启动」。</p></div>'; } }] });
  }
});
document.addEventListener("change", (e) => {
  const t = e.target;
  if (t.dataset.sel) { t.checked ? ui.selected.add(t.dataset.sel) : ui.selected.delete(t.dataset.sel); saveSel(); ui.lastRender.acc = null; render(); }
  if (t.id === "selAll") { const ids = filtered().map((a) => a.id); ids.forEach((i) => (t.checked ? ui.selected.add(i) : ui.selected.delete(i))); saveSel(); ui.lastRender.acc = null; render(); }
  if (t.id === "csvFile" && t.files[0]) {
    t.files[0].text().then(async (text) => {
      t.value = "";
      try { const r = await api("/api/accounts/import", { text }); r.added ? toast(`已导入 ${r.added} 个账户`) : toast("没有识别到账户行", true); if (r.errors.length) toast(r.errors.slice(0, 3).join("；"), true); poll(); }
      catch (er) { toast(er.message, true); }
    });
  }
  if (t.id === "eaFile" && t.files.length) { const fs = [...t.files]; t.value = ""; uploadEA(fs); }
  if (t.id === "oKind") { const pend = t.value.includes("_"); $("#oPrice").disabled = !pend; $("#oPrice").placeholder = pend ? "挂单价格" : "市价单不用填"; }
});
$("#search").addEventListener("input", (e) => { ui.search = e.target.value; render(); });
{ const adv = $("#tradeAdv"); if (localStorage.getItem("fleet.tradeAdv") === "1") adv.open = true;
  adv.addEventListener("toggle", () => { localStorage.setItem("fleet.tradeAdv", adv.open ? "1" : "0"); ui.lastRender = {}; render(); }); }

renderNav();
poll().then(() => { updStartup(); if (ui.tab === "settings") fillSettings(); if (ui.tab === "log") loadLogs(); if (ui.tab === "stats") loadStats(); if (ui.state) { $("#oGroups").value = (ui.state.settings.scale_groups || []).join(","); if (ui.state.settings.default_symbol) $("#oSymbol").value = ui.state.settings.default_symbol; } });
setInterval(() => { if (ui.tab === "log") loadLogs(); }, 3000);
