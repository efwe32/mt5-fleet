/* MT5 批量终端 收益台（desk）：浅色实时看板。纯原生 JS + canvas + requestAnimationFrame，离线可用。
   数据：/api/state（app.js 轮询后传进来）+ /api/desk（净值采样、活动流、平仓记录缓存）。 */
"use strict";
(function () {
  const PAL = ["#e8782a", "#5a9e2c", "#a5a01a", "#2f6fd6", "#3a62d0", "#7c4dcc", "#9b4dcc", "#c8203f", "#e0457b", "#14a08a"];
  const SYMPAL = ["#2f5fd0", "#14a08a", "#9b4dcc", "#a5a01a", "#e8782a", "#c8203f", "#5a9e2c", "#e0457b", "#3a62d0", "#7c4dcc"];
  const C = { ink: "#1b1b1d", mut: "#8b8b86", faint: "#b9b9b3", line: "#ededea", g: "#2a7a57", crim: "#c8203f", pink: "#e0457b", blue: "#2f5fd0", purple: "#3b2150", teal: "#1aa37a" };
  const MONO = 'Consolas,"Cascadia Mono","JetBrains Mono",Menlo,"Microsoft YaHei","PingFang SC","Noto Sans CJK SC","Noto Sans SC",monospace';
  const SANS = '"Segoe UI",Arial,"Microsoft YaHei","PingFang SC","Noto Sans CJK SC","Noto Sans SC",sans-serif';
  const DIMC = ["#2bb673", "#e0457b", "#f0a030", "#8e6fd8", "#3a62d0"];
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pad = (n) => String(n).padStart(2, "0");
  // 服务器时间：D.srvSkew = MT5 offset(ms)；UTC 取位避免再被北京时区加一层
  const hm = (ms) => { const d = new Date((Number(ms) || Date.now()) + (D.srvSkew || 0)); return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`; };
  const nf = (n, d = 2) => (Number(n) || 0).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  const sgn = (n, d = 2) => (n >= 0 ? "+" : "-") + nf(Math.abs(n), d);
  const usd = (n, d = 2) => (n >= 0 ? "+$" : "-$") + nf(Math.abs(n), d);
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const lerp = (a, b, k) => a + (b - a) * k;
  const rgba = (hex, a) => { const n = parseInt(hex.slice(1), 16); return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`; };

  const D = {
    active: false, raf: 0, last: 0, t: 0, fps: 0, frames: 0, fpsT: 0, built: false,
    st: null, events: [], seq: 0, total: 0, equity: [], eqLast: 0, deals: {}, dealsV: -1, started: 0, srvSkew: 0,
    histMode: "eq", agg: null, aggKey: "", pollTimer: 0, inflight: false, tw: {}, k: {}, txt: new WeakMap(),
    lit: {}, lastFloat: {}, cycleT: 0, boost: 0, reveal: 0, particles: [], sats: {}, links: [], chordT: 0, avKey: "", logInit: false,
    filter: null, gen: 0, popOpen: false,
  };
  window.__desk = D;

  // ---------------- 小工具 ----------------
  function setText(el, s) { if (el && D.txt.get(el) !== s) { D.txt.set(el, s); el.textContent = s; } }
  function setHTML(el, s) { if (el && D.txt.get(el) !== s) { D.txt.set(el, s); el.innerHTML = s; } }
  function setK(key, s, cls) { const el = D.k[key]; if (!el) return; setText(el, s); if (cls !== undefined && el.className !== cls) el.className = cls; }
  function tw(key, target, rate = 5) {
    let o = D.tw[key];
    if (!o) { o = D.tw[key] = { v: target, t: target }; }
    o.t = target; return o;
  }
  function stepTweens(dt) {
    for (const k in D.tw) { const o = D.tw[k]; const d = o.t - o.v; o.v = Math.abs(d) < 1e-6 ? o.t : o.v + d * Math.min(1, dt * 4.2); }
  }
  const tv = (key) => (D.tw[key] ? D.tw[key].v : 0);
  const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver((es) => es.forEach((e) => { if (e.target._cv) e.target._cv.dirty = true; })) : null;
  function cv(id) { const el = $(id); const o = { el, ctx: el.getContext("2d"), w: 0, h: 0, dpr: 1, dirty: true }; el._cv = o; ro && ro.observe(el); return o; }
  function prep(o) {
    if (o.dirty || !ro) {
      const w = o.el.clientWidth, h = o.el.clientHeight;
      if (w !== o.w || h !== o.h || o.dirty) {
        o.dpr = Math.min(2, window.devicePixelRatio || 1); o.w = Math.max(10, w); o.h = Math.max(10, h);
        o.el.width = Math.round(o.w * o.dpr); o.el.height = Math.round(o.h * o.dpr);
      }
      o.dirty = false;
    }
    const c = o.ctx; c.setTransform(o.dpr, 0, 0, o.dpr, 0, 0); c.clearRect(0, 0, o.w, o.h); return c;
  }
  function hash(s) { let h = 2166136261; for (const ch of String(s)) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619); } return h >>> 0; }
  function rng(seed) { let s = seed || 1; return () => { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return s / 4294967296; }; }
  function roundRect(c, x, y, w, h, r) { c.beginPath(); c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + h, r); c.arcTo(x + w, y + h, x, y + h, r); c.arcTo(x, y + h, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath(); }
  function tagOf(a) {
    let s = String(a.alias || "");
    if (/[·•|]/.test(s)) s = s.split(/[·•|]/).pop();
    s = s.replace(/\s+/g, "");
    if (!s) s = String(a.login || "").slice(-4);
    let w = 0, out = "";
    for (const ch of s) { const cw = /[\u2e80-\uffff]/.test(ch) ? 2 : 1; if (w + cw > 7) break; w += cw; out += ch; }
    return out.toUpperCase();
  }

  // ---------------- 范围 / 账户（顶部「选择账户」，保存在本机浏览器） ----------------
  const FKEY = "fleet.deskAccs";
  try { const v = JSON.parse(localStorage.getItem(FKEY) || "null"); if (Array.isArray(v) && v.length) D.filter = new Set(v.map(String)); } catch (e) { D.filter = null; }
  function allAccs() { return (D.st && D.st.accounts) || []; }
  // 当前生效的账户 ID（null = 全部）。已删除的账户自动忽略；一个都对不上时按全部处理
  function filterIds() {
    if (!D.filter) return null;
    const ids = allAccs().filter((a) => D.filter.has(a.id)).map((a) => a.id);
    if (!D.st) return [...D.filter];
    return ids.length && ids.length < allAccs().length ? ids : null;
  }
  function scopeAccs() {
    const accs = allAccs(), f = filterIds();
    return f ? accs.filter((a) => f.includes(a.id)) : accs;
  }
  function colorOf(id) { const i = allAccs().findIndex((a) => a.id === id); return i < 0 ? C.mut : PAL[i % PAL.length]; }
  function accById(id) { return allAccs().find((a) => a.id === id); }

  // ---------------- 汇总（平仓记录） ----------------
  function computeAgg() {
    const sc = scopeAccs();
    const key = [D.dealsV, sc.map((a) => a.id).join(","), new Date().toDateString()].join("|");
    if (key === D.aggKey && D.agg) return D.agg;
    D.aggKey = key;
    const deals = [];
    sc.forEach((a) => (D.deals[a.id] || []).forEach((d) => deals.push([d[0], d[1], d[2], a.id])));
    deals.sort((x, y) => x[0] - y[0]);
    const sn = new Date(Date.now() + (D.srvSkew || 0)); const day0 = Date.UTC(sn.getUTCFullYear(), sn.getUTCMonth(), sn.getUTCDate()) / 1000;
    const g = { n: deals.length, todayClosed: 0, todayBy: {}, wins: 0, losses: 0, gp: 0, gl: 0, best: 0, sum: 0 };
    deals.forEach((d) => {
      const p = d[1]; g.sum += p;
      if (p > 0) { g.wins++; g.gp += p; } else if (p < 0) { g.losses++; g.gl -= p; }
      if (p > g.best) g.best = p;
      if (d[0] >= day0) { g.todayClosed += p; g.todayBy[d[3]] = (g.todayBy[d[3]] || 0) + p; }
    });
    g.avg = g.n ? g.sum / g.n : 0;
    g.avgWin = g.wins ? g.gp / g.wins : 0; g.avgLoss = g.losses ? g.gl / g.losses : 0;
    g.winRate = g.n ? g.wins / g.n : 0; g.pf = g.gl ? g.gp / g.gl : g.gp ? Infinity : 0;
    // 均值 / 标准差
    const mu = g.avg; let v = 0; deals.forEach((d) => { v += (d[1] - mu) ** 2; });
    const sd = g.n > 1 ? Math.sqrt(v / (g.n - 1)) : 1;
    g.mu = mu; g.sd = sd || 1;
    const zs = deals.map((d) => (d[1] - mu) / g.sd);
    g.tailMass = g.n ? zs.filter((z) => z > 1.5).length / g.n : 0;
    // 山脊：滚动窗口的核密度
    const M = 26, G = 140, Z0 = -2.6, Z1 = 2.6, dz = (Z1 - Z0) / (G - 1);
    const ker = []; const bw = 0.3 / dz; for (let i = -Math.ceil(bw * 3); i <= Math.ceil(bw * 3); i++) ker.push([i, Math.exp(-0.5 * (i / bw) ** 2)]);
    const ridge = [];
    g.synthetic = g.n < 8;
    for (let k = 0; k < M; k++) {
      const arr = new Float32Array(G);
      if (g.synthetic) {
        for (let i = 0; i < G; i++) { const z = Z0 + i * dz; arr[i] = Math.exp(-0.5 * ((z - 0.15 + Math.sin(k * 0.7) * 0.15) / (0.75 + 0.1 * Math.cos(k))) ** 2); }
      } else {
        const win = Math.max(8, Math.round(g.n * 0.4)), end = Math.round(win + (g.n - win) * (k / (M - 1)));
        const bins = new Float32Array(G);
        for (let j = Math.max(0, end - win); j < end; j++) { const z = clamp(zs[j], Z0, Z1); bins[Math.round((z - Z0) / dz)] += 1; }
        for (let i = 0; i < G; i++) { let s = 0; for (const [o, w] of ker) { const b = i + o; if (b >= 0 && b < G) s += bins[b] * w; } arr[i] = s; }
      }
      let mx = 0; for (let i = 0; i < G; i++) mx = Math.max(mx, arr[i]); for (let i = 0; i < G; i++) arr[i] /= mx || 1;
      ridge.push(arr);
    }
    g.ridge = ridge; g.M = M; g.G = G; g.Z0 = Z0; g.Z1 = Z1;
    // 每日盈亏、最大回撤、平仓累计曲线
    const byDay = new Map();
    deals.forEach((d) => { const t = new Date(d[0] * 1000); const k = Date.UTC(t.getUTCFullYear(), t.getUTCMonth(), t.getUTCDate()); byDay.set(k, (byDay.get(k) || 0) + d[1]); });
    const days = [...byDay.entries()].sort((a, b) => a[0] - b[0]);
    let cum = 0, peak = 0, dd = 0; deals.forEach((d) => { cum += d[1]; peak = Math.max(peak, cum); dd = Math.max(dd, peak - cum); });
    g.maxDD = dd;
    const last = days.slice(-60).map((d) => d[1]);
    const NB = 14; const bins = new Array(NB).fill(0); let bmin = 0, bmax = 0;
    if (last.length) {
      bmin = Math.min(...last); bmax = Math.max(...last); if (bmax - bmin < 1e-9) { bmin -= 1; bmax += 1; }
      last.forEach((v) => { bins[Math.min(NB - 1, Math.floor(((v - bmin) / (bmax - bmin)) * NB))]++; });
    }
    g.bins = bins; g.bmin = bmin; g.bmax = bmax; g.days = days.length;
    const t30 = Date.now() / 1000 + (D.srvSkew || 0) / 1000 - 30 * 86400; let c2 = 0; g.closedSeries = [];
    deals.forEach((d) => { c2 += d[1]; if (d[0] >= t30) g.closedSeries.push([d[0], c2]); });
    // 最近 7 天的 账户→品种 成交次数（没有持仓时用于弦图）
    const t7 = Date.now() / 1000 + (D.srvSkew || 0) / 1000 - 7 * 86400; g.recentFlows = {};
    deals.forEach((d) => { if (d[0] >= t7 && d[2]) { const k = d[3] + "|" + d[2]; g.recentFlows[k] = (g.recentFlows[k] || 0) + 1; } });
    D.agg = g;
    return g;
  }

  // ---------------- 结构（只建一次） ----------------
  const kv = (key, label, cls = "") => `<div class="pd-kv"><span>${label}</span><b data-k="${key}" class="${cls}">—</b></div>`;
  function build() {
    if (D.built) return;
    D.built = true;
    $("pdRidgeS").innerHTML = `<div class="pd-sh">尾部扫描 / 实时</div>${kv("r.n", "平仓笔数")}${kv("r.tail", "尾部占比", "pd-g")}${kv("r.mult", "盈亏比")}${kv("r.avg", "单笔均值")}${kv("r.best", "最大单笔", "pd-g")}<div class="pd-sf">看清尾部，守好出场。</div>`;
    $("pdChordS").className = "pd-stats lines";
    $("pdChordS").innerHTML = `${kv("c.pos", "持仓笔数")}${kv("c.rej", "失败操作", "pd-r")}${kv("c.links", "每账户连线")}${kv("c.mode", "交易状态", "pd-r")}`;
    $("pdLatS").className = "pd-stats lines";
    $("pdLatS").innerHTML = `${kv("l.acc", "在线账户")}${kv("l.pos", "持仓笔数")}${kv("l.sym", "品种数")}${kv("l.ea", "运行策略")}${kv("l.rot", "旋转角度", "pd-r")}`;
    $("pdGraphL").innerHTML = `<div class="pd-sh">节点类型</div><div class="pd-legend"><span><i style="background:${C.pink}"></i>亏损持仓</span><span><i style="background:${C.blue}"></i>盈利持仓</span><span><i style="background:${C.ink}"></i>跟单路径</span><span><i style="background:${C.teal}"></i>分组枢纽</span></div>
      ${kv("g.loss", "亏损笔数", "pd-p")}${kv("g.win", "盈利笔数", "pd-b")}${kv("g.n", "平仓总数")}${kv("g.fol", "跟单账户", "pd-g")}
      <div class="pd-prog" id="pdProg">${"<i></i>".repeat(24)}</div><div class="pd-kv" style="padding:2px 0"><span>今日趋势</span><b data-k="g.trend" class="pd-b">—</b></div>`;
    $("pdGraphR").className = "pd-stats lines";
    $("pdGraphR").innerHTML = `${kv("g.pl", "今日盈亏", "pd-b")}${kv("g.dd", "最大回撤", "pd-p")}${kv("g.pf", "盈利因子", "pd-g")}${kv("g.wr", "胜率")}<div class="pd-sh" style="margin:10px 0 0">每日盈亏分布 / 近 60 天</div><div class="pd-mini pd-cv"><canvas id="pdHistoC"></canvas></div>`;
    document.querySelectorAll("#pdesk [data-k]").forEach((el) => { D.k[el.dataset.k] = el; });
    D.segs = $("pdSegs"); D.prog = [...$("pdProg").children];
    D.cv = { hist: cv("pdHistC"), ridge: cv("pdRidgeC"), chord: cv("pdChordC"), lat: cv("pdLatC"), graph: cv("pdGraphC"), histo: cv("pdHistoC") };
    $("pdHistTog").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-hist]"); if (!b) return;
      D.histMode = b.dataset.hist; D.reveal = 0;
      [...$("pdHistTog").children].forEach((x) => x.classList.toggle("on", x === b));
    });
    const det = $("pdClassic");
    if (localStorage.getItem("fleet.classic") === "1") det.open = true;
    det.addEventListener("toggle", () => localStorage.setItem("fleet.classic", det.open ? "1" : "0"));
    initLattice();
    initPick();
  }

  // ---------------- 数据轮询 ----------------
  async function pollDesk() {
    clearTimeout(D.pollTimer);
    if (!D.active) return;
    if (!document.hidden && !D.inflight && D.st) {
      D.inflight = true;
      try {
        const f = filterIds(), gen = D.gen;
        const q = `/api/desk?since=${D.seq}&eq_since=${D.eqLast}&deals_v=${D.dealsV}` + (f ? `&accounts=${encodeURIComponent(f.join(","))}` : "");
        const r = await fetch(q, { headers: { "X-Fleet-Token": window.FLEET_TOKEN } });
        if (r.ok) { const j = await r.json(); if (gen === D.gen) merge(j); }
      } catch (e) { /* 断线时由 app.js 提示 */ }
      D.inflight = false;
    }
    D.pollTimer = setTimeout(pollDesk, D.st ? 2000 : 300);
  }
  function merge(r) {
    if (D.started && r.started !== D.started) { // 服务重启：全部重来
      D.started = r.started; D.seq = 0; D.eqLast = 0; D.dealsV = -1; D.events = []; D.equity = []; D.logInit = false; $("pdRows").innerHTML = "";
      return pollDesk();
    }
    D.started = r.started; if (!D._clockFromState) D.srvSkew = r.now - Date.now(); D.total = filterIds() ? null : r.eventsTotal;
    if (r.equity && r.equity.length) { D.equity.push(...r.equity); D.eqLast = D.equity[D.equity.length - 1][0]; if (D.equity.length > 9500) D.equity.splice(0, D.equity.length - 9500); }
    if (r.deals) { D.deals = r.deals; D.dealsV = r.dealsVersion; D.aggKey = ""; }
    const fresh = r.events || [];
    if (fresh.length) {
      D.seq = fresh[fresh.length - 1].seq;
      D.events.push(...fresh); if (D.events.length > 300) D.events.splice(0, D.events.length - 300);
      addRows(fresh, D.logInit);
      if (D.logInit) fresh.forEach(onEvent);
    }
    D.logInit = true;
  }
  // 切换账户范围：清空曲线/日志/汇总，按新范围重新拉取
  function resetScope() {
    D.gen++; D.seq = 0; D.eqLast = 0; D.dealsV = -1; D.deals = {}; D.aggKey = ""; D.agg = null; D.events = []; D.equity = []; D.fine = [];
    D.logInit = false; D.avKey = ""; D.chordKey = ""; D.sats = {}; D.lit = {}; D.lastFloat = {}; D.reveal = 0; D.ridgeDisp = null;
    ["liveEq", "hLo", "hHi", "bal", "pnl", "from", "fl", "pct"].forEach((k) => { delete D.tw[k]; });
    const rows = $("pdRows"); if (rows) rows.innerHTML = "";
    const top = $("pdTop"); if (top) { top.innerHTML = ""; top.style.background = ""; }
    D.inflight = false;
    if (D.active) pollDesk();
    if (D.st && D.built) renderAvatars(scopeAccs());
    if (typeof window.onDeskScope === "function") { try { window.onDeskScope(); } catch (e) { /* 经典统计自己处理 */ } }
  }
  function setFilter(ids) {
    const all = allAccs().map((a) => a.id);
    const keep = ids ? ids.filter((i) => all.includes(i)) : null;
    D.filter = keep && keep.length && keep.length < all.length ? new Set(keep) : null;
    try { if (D.filter) localStorage.setItem(FKEY, JSON.stringify([...D.filter])); else localStorage.removeItem(FKEY); } catch (e) { /* 隐私模式 */ }
    resetScope(); renderPick();
  }

  // ---------------- 选择账户（下拉多选） ----------------
  function pickLabel() {
    const f = filterIds(), n = allAccs().length;
    if (!f) return `选择账户：全部（${n}）▾`;
    const names = scopeAccs().map((a) => a.alias || a.login);
    return `选择账户：${f.length === 1 ? names[0] : `${f.length} / ${n}`} ▾`;
  }
  function renderPick() {
    const btn = $("pdPick"); if (!btn) return;
    const f = filterIds();
    setText(btn, pickLabel());
    btn.title = f ? "正在查看：" + scopeAccs().map((a) => `${a.alias}（${a.login}）`).join("、") : "正在查看全部账户";
    btn.classList.toggle("on", !!f);
    btn.setAttribute("aria-expanded", D.popOpen ? "true" : "false");
    const pop = $("pdPop");
    pop.classList.toggle("hidden", !D.popOpen);
    if (!D.popOpen) return;
    const accs = allAccs(), sel = new Set(f || accs.map((a) => a.id));
    const groups = (D.st && D.st.groups && D.st.groups.length) ? D.st.groups.slice() : [...new Set(accs.map((a) => a.group || "未分组"))];
    const key = JSON.stringify([groups, accs.map((a) => [a.id, a.alias, a.login, a.group, a.link])]);
    if (pop._k === key) {   // 账户列表没变：只同步勾选状态，不重建（保持焦点和滚动位置）
      pop.querySelectorAll("input[data-ppid]").forEach((cb) => { const v = sel.has(cb.dataset.ppid); if (cb.checked !== v) cb.checked = v; });
      const foot = pop.querySelector(".pp-foot span"); if (foot && !foot._hint) { foot.textContent = `已选 ${sel.size} / ${accs.length} · 自动保存`; foot.style.color = ""; }
      if (foot) foot._hint = false;
      return;
    }
    pop._k = key;
    const scrollTop = pop.querySelector(".pp-list") ? pop.querySelector(".pp-list").scrollTop : 0;
    pop.innerHTML = !accs.length ? `<div class="pp-lab">还没有账户 · 先在账号页添加</div>` : `
      <div class="pp-row"><button type="button" data-pp="all">全选</button><button type="button" data-pp="inv">反选</button><button type="button" data-pp="online">只看在线</button></div>
      ${groups.length > 1 ? `<div class="pp-lab">按分组</div><div class="pp-row">${groups.map((g) => `<button type="button" data-pp="g" data-g="${esc(g)}">${esc(g)}</button>`).join("")}</div>` : ""}
      <div class="pp-list">${accs.map((a) => `<label><input type="checkbox" data-ppid="${esc(a.id)}"${sel.has(a.id) ? " checked" : ""}><i style="background:${colorOf(a.id)}"></i><span class="nm">${esc(a.alias || a.login)}</span><span class="lg">${esc(a.login)}</span><span class="st${a.link === "online" ? " on" : ""}">${a.link === "online" ? "在线" : a.link === "error" ? "出错" : "离线"}</span></label>`).join("")}</div>
      <div class="pp-foot"><span>已选 ${sel.size} / ${accs.length} · 自动保存</span><button type="button" data-pp="close">完成</button></div>`;
    const list = pop.querySelector(".pp-list"); if (list) list.scrollTop = scrollTop;
  }
  function pickHint(msg) {
    const foot = document.querySelector("#pdPop .pp-foot span"); if (foot) { foot.textContent = msg; foot.style.color = "#c8203f"; foot._hint = true; }
  }
  function initPick() {
    const btn = $("pdPick"), pop = $("pdPop"); if (!btn || !pop) return;
    btn.addEventListener("click", (e) => { e.stopPropagation(); D.popOpen = !D.popOpen; $("pdPop")._k = ""; renderPick(); });
    pop.addEventListener("click", (e) => {
      e.stopPropagation();
      const b = e.target.closest("button[data-pp]"); if (!b) return;
      const accs = allAccs(), cur = new Set(filterIds() || accs.map((a) => a.id));
      const act = b.dataset.pp;
      if (act === "close") { D.popOpen = false; return renderPick(); }
      let next = null;
      if (act === "all") next = accs.map((a) => a.id);
      else if (act === "inv") next = accs.filter((a) => !cur.has(a.id)).map((a) => a.id);
      else if (act === "online") next = accs.filter((a) => a.link === "online").map((a) => a.id);
      else if (act === "g") next = accs.filter((a) => (a.group || "未分组") === b.dataset.g).map((a) => a.id);
      if (!next || !next.length) return pickHint(act === "online" ? "现在没有在线账户" : "至少要选一个账户");
      setFilter(next);
    });
    pop.addEventListener("change", (e) => {
      const cb = e.target.closest("input[data-ppid]"); if (!cb) return;
      const accs = allAccs(), cur = new Set(filterIds() || accs.map((a) => a.id));
      if (cb.checked) cur.add(cb.dataset.ppid); else cur.delete(cb.dataset.ppid);
      if (!cur.size) { cb.checked = true; return pickHint("至少要选一个账户"); }
      setFilter(accs.map((a) => a.id).filter((i) => cur.has(i)));
    });
    document.addEventListener("click", (e) => { if (D.popOpen && !e.target.closest("#pdPickWrap")) { D.popOpen = false; renderPick(); } });
    document.addEventListener("keydown", (e) => { if (D.popOpen && e.key === "Escape") { D.popOpen = false; renderPick(); btn.focus(); } });
  }

  function onEvent(e) {
    D.boost = Math.min(2.5, D.boost + 0.6);
    const col = e.kind === "open" ? C.blue : e.kind === "close" ? (e.ok ? C.g : C.pink) : e.ok ? "#7c4dcc" : C.crim;
    if (e.accountId) light(e.accountId, col, 3200);
    // 复制流粒子
    const a = accById(e.accountId);
    if (a && (e.kind === "open" || e.kind === "close")) for (let i = 0; i < 5; i++) spawn(e.kind === "close" && !e.ok ? C.pink : C.blue, i * 0.035);
  }

  // ---------------- 活动日志 ----------------
  function rowHTML(e) {
    const a = accById(e.accountId), col = e.accountId ? colorOf(e.accountId) : C.mut;
    const tag = a ? tagOf(a) : (e.alias && e.alias !== "—" ? e.alias : "系统").slice(0, 3);
    return `<span class="t">${hm(e.at)}</span><span class="g" style="color:${col}">${esc(tag)}</span><span class="x">${esc(e.text)}</span><i class="d ${e.ok ? (e.kind === "op" ? "" : "ok") : "bad"}"></i>`;
  }
  function addRows(list, animate) {
    const box = $("pdRows");
    let inner = box.firstElementChild;
    if (!inner) { inner = document.createElement("div"); inner.className = "in"; box.appendChild(inner); }
    const take = animate ? list : list.slice(-14);
    if (!take.length) return;
    const frag = document.createDocumentFragment();
    take.forEach((e) => { const d = document.createElement("div"); d.className = "pd-row" + (animate ? " new" : ""); d.innerHTML = rowHTML(e); frag.appendChild(d); });
    if (animate) {
      inner.style.transition = "none"; inner.style.transform = `translateY(${21 * take.length}px)`;
      inner.appendChild(frag); void inner.offsetHeight;
      inner.style.transition = ""; inner.style.transform = "translateY(0)";
    } else inner.appendChild(frag);
    while (inner.children.length > 40) inner.removeChild(inner.firstElementChild);
    const e = list[list.length - 1], col = e.accountId ? colorOf(e.accountId) : C.ink;
    const top = $("pdTop");
    const a = accById(e.accountId);
    top.style.background = rgba(col, 0.09);
    top.innerHTML = `<b style="color:${col}">${hm(e.at)} ${esc(a ? a.alias : e.alias || "系统")}</b><span style="color:${col}">${esc(e.text)}</span>`;
    if (animate) { top.classList.remove("flash"); void top.offsetWidth; top.classList.add("flash"); }
  }

  // ---------------- 头像 ----------------
  const SKIN = ["#f3dcc4", "#efd2b2", "#e8c39e", "#f6e3d0", "#e2b893"];
  const HAIR = ["#1d1d1f", "#2b211b", "#3b2a20", "#d6a93a", "#6b3a1e", "#1d1d1f"];
  function drawFace(canvas, seed) {
    const c = canvas.getContext("2d"); const R = rng(seed); const P = (x, y, w, h, col) => { c.fillStyle = col; c.fillRect(x, y, w, h); };
    c.clearRect(0, 0, 16, 16);
    const skin = SKIN[Math.floor(R() * SKIN.length)], hairC = HAIR[Math.floor(R() * HAIR.length)], style = Math.floor(R() * 6);
    const red = "#d0283f", redD = "#9e1830", dark = "#1d1d1f";
    // 衣服 + 领口
    P(3, 13, 10, 3, red); P(2, 14, 12, 2, red); P(7, 13, 2, 2, dark); P(5, 13, 1, 1, redD); P(10, 13, 1, 1, redD);
    // 脖子 + 脸
    P(7, 12, 2, 1, skin); P(4, 4, 8, 8, skin); P(5, 12, 6, 1, skin); P(4, 11, 1, 1, "rgba(0,0,0,0)");
    c.clearRect(4, 11, 1, 1); c.clearRect(11, 11, 1, 1); P(5, 11, 6, 1, skin);
    // 耳机
    P(2, 6, 2, 4, red); P(12, 6, 2, 4, red); P(2, 6, 1, 4, redD); P(13, 6, 1, 4, redD);
    // 发型
    if (style === 0) { P(4, 2, 8, 2, hairC); P(4, 4, 1, 2, hairC); P(11, 4, 1, 2, hairC); P(5, 4, 3, 1, hairC); }
    else if (style === 1) { P(4, 3, 8, 1, hairC); for (let x = 4; x < 12; x += 2) P(x, 2, 1, 1, hairC); P(4, 4, 1, 1, hairC); P(11, 4, 1, 1, hairC); }
    else if (style === 2) { P(4, 2, 8, 2, hairC); P(3, 3, 1, 8, hairC); P(12, 3, 1, 8, hairC); P(4, 4, 2, 2, hairC); P(10, 4, 2, 1, hairC); }
    else if (style === 3) { for (let x = 3; x < 13; x++) P(x, 2 + ((x * 7) % 3 === 0 ? 0 : 1), 1, 2, hairC); P(4, 1, 2, 1, hairC); P(7, 1, 3, 1, hairC); P(3, 4, 1, 3, hairC); P(12, 4, 1, 3, hairC); }
    else if (style === 4) { P(5, 3, 6, 1, hairC); P(4, 9, 8, 3, hairC); P(6, 9, 4, 1, skin); }
    else { P(4, 2, 8, 2, hairC); P(6, 0, 4, 2, hairC); P(4, 4, 1, 3, hairC); P(11, 4, 1, 3, hairC); }
    // 五官
    const glasses = R() < 0.25, brow = R() < 0.6, smile = R();
    if (brow) { P(5, 6, 2, 1, dark); P(9, 6, 2, 1, dark); }
    P(6, 7, 1, 1, dark); P(9, 7, 1, 1, dark);
    if (glasses) { c.strokeStyle = dark; c.lineWidth = 1; c.strokeRect(4.5, 6.5, 3, 2); c.strokeRect(8.5, 6.5, 3, 2); P(7, 7, 2, 1, dark); }
    if (style !== 4) { if (smile < 0.5) { P(6, 10, 4, 1, dark); P(5, 9, 1, 1, dark); P(10, 9, 1, 1, dark); } else if (smile < 0.8) P(6, 10, 4, 1, dark); else { P(6, 9, 4, 2, dark); P(7, 9, 2, 1, "#fff"); } }
    else P(6, 10, 4, 1, dark);
    if (R() < 0.2 && style !== 4) P(6, 9, 4, 1, hairC);
    P(7, 8, 1, 1, "rgba(0,0,0,.18)");
  }
  function renderAvatars(sc) {
    const key = sc.map((a) => a.id + a.alias + a.group + a.login).join("|");
    const row = $("pdAvs");
    if (key !== D.avKey) {
      D.avKey = key;
      if (!sc.length) { row.innerHTML = `<div class="pd-empty-av">还没有账户 · 在账号页添加后，每个账户会在这里显示一张卡片</div>`; return; }
      row.innerHTML = sc.map((a, i) => `<div class="pd-av" data-av="${esc(a.id)}" style="--c:${colorOf(a.id)}" title="${esc(a.alias)} · ${esc(a.login)}">
        <span class="n">${pad(i + 1)}</span><span class="s"></span><span class="sig">${[3, 5, 7, 9].map((h) => `<i style="height:${h - 1}px"></i>`).join("")}</span>
        <canvas width="16" height="16"></canvas><div class="nm">${esc(a.alias)}</div><div class="rl">${esc((a.group || "未分组").toUpperCase())}</div><div class="pl"></div></div>`).join("");
      row.querySelectorAll(".pd-av").forEach((el) => drawFace(el.querySelector("canvas"), hash(accById(el.dataset.av)?.login || el.dataset.av)));
      D.avEls = {}; row.querySelectorAll(".pd-av").forEach((el) => { D.avEls[el.dataset.av] = { el, s: el.querySelector(".s"), pl: el.querySelector(".pl"), sig: [...el.querySelectorAll(".sig i")] }; });
    }
    sc.forEach((a) => {
      const o = D.avEls && D.avEls[a.id]; if (!o) return;
      const on = a.link === "online";
      const sc2 = "s" + (on ? " on" : a.link === "error" ? " err" : "");
      if (o.s.className !== sc2) o.s.className = sc2;
      setText(o.pl, on ? sgn(a.floating) : a.link === "error" ? "出错" : "离线");
      o.pl.style.color = on ? (a.floating >= 0 ? C.g : C.crim) : "";
      const bars = on ? 1 + Math.min(3, a.positionsCount) : 0;
      o.sig.forEach((b, i) => b.classList.toggle("on", i < bars));
      // 浮盈变化 → 点亮
      if (on) {
        const prev = D.lastFloat[a.id];
        const th = Math.max(0.5, Math.abs(a.equity) * 0.00025);
        if (prev === undefined) D.lastFloat[a.id] = a.floating;
        else if (Math.abs(a.floating - prev) > th) { light(a.id, a.floating > prev ? C.g : C.pink, 2400); D.lastFloat[a.id] = a.floating; }
      }
    });
  }
  function light(id, col, ms) { D.lit[id] = { col, until: performance.now() + ms }; }
  function updateLit(now) {
    if (now > D.cycleT) { // 像视频一样循环点亮
      D.cycleT = now + 1300;
      const on = scopeAccs().filter((a) => a.link === "online");
      const pool = on.length ? on : scopeAccs();
      const litN = Object.values(D.lit).filter((x) => x.until > now).length;
      if (pool.length && litN < Math.min(3, Math.ceil(pool.length / 2))) {
        const a = pool[Math.floor(Math.random() * pool.length)];
        const col = a.link !== "online" ? "#7c4dcc" : a.floating >= 0 ? [C.g, C.blue, "#7c4dcc"][Math.floor(Math.random() * 3)] : C.pink;
        light(a.id, col, 2600);
      }
    }
    if (!D.avEls) return;
    for (const id in D.avEls) {
      const L = D.lit[id], el = D.avEls[id].el, on = !!(L && L.until > now);
      if (on) { if (el._lc !== L.col) { el._lc = L.col; el.style.setProperty("--lc", L.col); } }
      if (el.classList.contains("lit") !== on) el.classList.toggle("lit", on);
    }
  }

  // ---------------- 指标条 / 统计列（每帧，只写变化） ----------------
  function updateTexts(now) {
    const st = D.st; if (!st) return;
    const sc = scopeAccs(), agg = computeAgg();
    let eq = 0, bal = 0, fl = 0, on = 0, err = 0, pos = 0, ea = 0, eaRun = 0; const syms = new Set();
    sc.forEach((a) => {
      if (a.link === "online") { on++; eq += a.equity || 0; bal += a.balance || 0; fl += a.floating || 0; }
      if (a.link === "error") err++;
      pos += a.link === "online" ? (a.positions || []).length : 0;
      (a.positions || []).forEach((p) => syms.add(p.symbol));
      (a.strategies || []).forEach((s) => { ea++; if (s.running) eaRun++; });
    });
    if (!on) sc.forEach((a) => { eq += a.equity || 0; bal += a.balance || 0; });
    const ccy = (sc.find((a) => a.currency) || {}).currency || "USD";
    const today = agg.todayClosed;
    const pnl = today;            // 总盈亏 = 今日已平仓盈亏（含手续费、隔夜利息），持仓浮动单独显示
    // 起点：今天第一条净值采样（按当前选择的账户汇总；没有就用 余额 − 今日平仓）
    const d0 = new Date(); d0.setHours(0, 0, 0, 0);
    const first = D.equity.find((r) => r[0] * 1000 >= d0.getTime());
    const from = on && first ? first[1] : bal - today;
    tw("bal", eq); tw("pnl", pnl); tw("from", from); tw("fl", fl);
    const pct = from ? (pnl / Math.abs(from)) * 100 : 0; tw("pct", pct);
    setHTML($("pdBal"), `${nf(tv("bal"))}<small>${esc(ccy)}</small>`);
    setText($("pdCcy"), ccy);
    setText($("pdFrom"), `起点 ${nf(tv("from"))} ${ccy} · 余额 ${nf(bal)}`);
    const pv = tv("pnl"), pe = $("pdPnl");
    setText(pe, usd(pv)); const pc = "pd-big " + (pv >= 0 ? "pd-g" : "pd-r"); if (pe.className !== pc) pe.className = pc;
    const pce = $("pdPnlPct"); setText(pce, `${sgn(tv("pct"))}% · 浮亏金额 ${sgn(tv("fl"))}`);
    const pcc = "pd-cap " + (pv >= 0 ? "pd-g" : "pd-r"); if (pce.className !== pcc) pce.className = pcc;
    // 运行时长
    const up = Math.max(0, (Date.now() - (D.started || Date.now())) / 1000);
    const hh = Math.floor(up / 3600), mm = Math.floor((up % 3600) / 60), ss = Math.floor(up % 60);
    setText($("pdMission"), hh ? `${hh}:${pad(mm)}:${pad(ss)}` : `${pad(mm)}:${pad(ss)}`);
    setText($("pdMissionCap"), `${hm(D.started || Date.now())} → ${hm(Date.now())} / ${hh ? hh + " 小时" : Math.max(1, mm) + " 分钟"}`);
    // 头部（服务器时间）
    const allOn = allAccs().filter((a) => a.link === "online").length;
    setText($("pdClock"), `${hm(Date.now())} / 13 小时`);
    setText($("pdSub"), `${allOn}/${allAccs().length} 个终端在线`);
    if (D.k._pick !== pickLabel()) { D.k._pick = pickLabel(); renderPick(); }
    const live = $("pdLive"), lc = "pd-live" + (allOn ? "" : err ? " err" : " off");
    if (live.className !== lc) live.className = lc;
    setText($("pdLiveT"), allOn ? "实时" : err ? "出错" : "空闲");
    // 闸门
    const gate = document.querySelector(".pd-gate"), gc = "pd-m pd-gate" + (on ? "" : " live");
    if (gate.className !== gc) gate.className = gc;
    setText($("pdGate"), "实盘");
    setText($("pdGateTag"), on ? "已连接" : err ? "连接出错" : "未连接");
    const n = Math.max(sc.length, 1), segKey = sc.map((a) => a.link).join(",");
    if (D.segs._k !== segKey) {
      D.segs._k = segKey;
      D.segs.innerHTML = (sc.length ? sc : [{}]).slice(0, 16).map((a) => `<i class="${a.link === "online" ? "on" : a.link === "error" ? "err" : ""}"></i>`).join("");
    }
    setText($("pdGateCap"), `在线 ${on}/${sc.length} · 单笔上限 ${st.settings.max_lots} 手 · 批量上限 ${st.settings.max_total_lots} 手`);
    void n;
    // 净值曲线标题
    const scopeTxt = filterIds() ? `已选 ${sc.length} 个账户` : "全部在线";
    setText($("pdHistR"), D.histMode === "eq" ? `${ccy} / ${scopeTxt} / ${spanLabel(D.histSpan || 600)}` : `${ccy} / ${filterIds() ? `已选 ${sc.length} 个账户 / ` : ""}已平仓累计 / 近 30 天`);
    setText($("pdEvN"), `${D.total == null ? D.events.length : D.total} 条事件`);
    // 山脊统计
    setK("r.n", String(agg.n));
    setK("r.tail", `${(agg.tailMass * 100).toFixed(2)}%`);
    setK("r.mult", agg.avgLoss ? `×${(agg.avgWin / agg.avgLoss).toFixed(2)}` : "—");
    setK("r.avg", agg.n ? usd(agg.avg) : "—", agg.avg >= 0 ? "" : "pd-r");
    setK("r.best", agg.n ? usd(agg.best, 1) : "—");
    // 弦图统计
    const rej = D.events.filter((e) => !e.ok && e.kind === "op").length;
    setK("c.pos", String(pos)); setK("c.rej", String(rej));
    setK("c.links", sc.length ? (D.links.length / sc.length).toFixed(1) : "0");
    setK("c.mode", on ? "实盘 · 已连接" : "实盘 · 未连接");
    setText($("pdChordN"), `${(D.chordNodes || []).length} 个节点`);
    // 晶格统计
    setK("l.acc", `${on} / ${sc.length}`); setK("l.pos", String(pos)); setK("l.sym", String(syms.size)); setK("l.ea", `${eaRun} / ${ea}`);
    setK("l.rot", `${Math.round(((D.lat.rot * 180) / Math.PI) % 360)}°`);
    // 关系图统计
    setK("g.loss", String(agg.losses)); setK("g.win", String(agg.wins)); setK("g.n", String(agg.n));
    setK("g.fol", String(D.groups ? D.groups.fol.length : 0));
    const wr = agg.winRate, segOn = Math.round(wr * 24);
    if (D.prog._n !== segOn) { D.prog._n = segOn; D.prog.forEach((el, i) => el.classList.toggle("on", i < segOn)); }
    setK("g.trend", pnl >= 0 ? "▲ 上涨" : "▼ 下跌", pnl >= 0 ? "pd-b" : "pd-p");
    setK("g.pl", usd(tv("pnl")), pv >= 0 ? "pd-b" : "pd-p");
    setK("g.dd", agg.maxDD ? "-$" + nf(agg.maxDD) : "$0.00");
    setK("g.pf", agg.pf === Infinity ? "∞" : agg.pf.toFixed(2));
    setK("g.wr", `${(wr * 100).toFixed(1)}%`);
    void now;
  }
  function spanLabel(s) { return s >= 3600 ? `${Math.round(s / 3600)} 小时` : `${Math.max(1, Math.round(s / 60))} 分钟`; }

  // ---------------- 1) 净值曲线 ----------------
  function drawHist(dt) {
    const o = D.cv.hist, c = prep(o), w = o.w, h = o.h;
    c.font = `10px ${MONO}`;
    const L = Math.max(44, (D.histLW || 40) + 10), R = 14, T = 16, B = 24, pw = w - L - R, ph = h - T - B;
    D.reveal = Math.min(1, D.reveal + dt / 1.4);
    let pts = [];
    const nowS = Date.now() / 1000;
    if (D.histMode === "eq") {
      pts = D.equity.map((r) => [r[0], r[1]]);
      const lastS = pts.length ? pts[pts.length - 1][0] : 0;
      (D.fine || []).forEach((f) => { if (f[0] > lastS) pts.push(f); });
      const on = scopeAccs().filter((a) => a.link === "online");
      if (on.length) { const live = on.reduce((s, a) => s + (a.equity || 0), 0); pts.push([nowS, tw("liveEq", live).v]); }
    } else pts = (D.agg ? D.agg.closedSeries : []).slice();
    c.font = `10px ${MONO}`; c.textBaseline = "middle";
    if (pts.length < 2) {
      c.strokeStyle = C.line; c.lineWidth = 1;
      for (let i = 0; i < 3; i++) { const y = T + (ph * i) / 2; c.beginPath(); c.moveTo(L, y); c.lineTo(w - R, y); c.stroke(); }
      c.fillStyle = C.mut; c.textAlign = "center";
      c.fillText(D.histMode === "eq" ? "暂无在线账户 · 在账号页批量登录后开始记录净值" : "近 30 天没有平仓记录", L + pw / 2, T + ph / 2);
      return;
    }
    const t0 = pts[0][0], tl = pts[pts.length - 1][0];
    const span = D.histMode === "eq" ? clamp((tl - t0) / 0.9, 90, 13 * 3600) : Math.max(60, (tl - t0) / 0.92);
    D.histSpan = lerp(D.histSpan || span, span, Math.min(1, dt * 2));
    const tEnd = Math.max(tl, t0 + D.histSpan * (D.histMode === "eq" ? 1 : 1));
    const tStart = tEnd - D.histSpan;
    let lo = Infinity, hi = -Infinity;
    pts.forEach((p) => { if (p[0] >= tStart) { lo = Math.min(lo, p[1]); hi = Math.max(hi, p[1]); } });
    if (!isFinite(lo)) { lo = pts[pts.length - 1][1]; hi = lo; }
    const rg = Math.max(hi - lo, Math.abs(hi) * 0.0004, 1);
    const yLo = tw("hLo", lo - rg * 0.18).v, yHi = tw("hHi", hi + rg * 0.32).v;
    const X = (t) => L + ((t - tStart) / D.histSpan) * pw, Y = (v) => T + ph - ((v - yLo) / (yHi - yLo || 1)) * ph;
    // 网格 + 坐标
    c.strokeStyle = C.line; c.lineWidth = 1; c.fillStyle = C.mut;
    const fmtY = (v) => Math.abs(v) >= 1e5 ? nf(v, 0) : Math.abs(v) >= 100 ? nf(v, 1) : nf(v, 2);
    for (let i = 0; i < 3; i++) {
      const v = yLo + ((yHi - yLo) * (2 - i)) / 2, y = T + (ph * i) / 2;
      c.beginPath(); c.moveTo(L, y + 0.5); c.lineTo(w - R, y + 0.5); c.stroke();
      c.textAlign = "left"; c.fillText(fmtY(v), 2, y);
      D.histLW = Math.max(i ? D.histLW : 0, c.measureText(fmtY(v)).width);
    }
    c.beginPath(); c.moveTo(L + pw / 2 + 0.5, T); c.lineTo(L + pw / 2 + 0.5, T + ph); c.stroke();
    c.textAlign = "left"; c.fillText(hm(tStart * 1000), L, h - 9);
    c.textAlign = "center"; c.fillText(hm((tStart + D.histSpan / 2) * 1000), L + pw / 2, h - 9);
    c.textAlign = "right"; c.fillText(hm(tEnd * 1000), w - R, h - 9);
    // 下采样到像素列
    const path = []; let lastX = -9;
    for (const p of pts) { if (p[0] < tStart) continue; const x = X(p[0]); if (x - lastX < 1 && path.length) path[path.length - 1] = [x, Y(p[1])]; else { path.push([x, Y(p[1])]); lastX = x; } }
    if (path.length < 2) return;
    const clipX = L + (path[path.length - 1][0] - L) * (D.reveal < 1 ? 1 - Math.pow(1 - D.reveal, 3) : 1);
    c.save(); c.beginPath(); c.rect(0, 0, clipX + 1, h); c.clip();
    const grd = c.createLinearGradient(0, T, 0, T + ph); grd.addColorStop(0, "rgba(42,122,87,.16)"); grd.addColorStop(1, "rgba(42,122,87,.02)");
    c.beginPath(); c.moveTo(path[0][0], T + ph); path.forEach((p) => c.lineTo(p[0], p[1])); c.lineTo(path[path.length - 1][0], T + ph); c.closePath(); c.fillStyle = grd; c.fill();
    c.beginPath(); path.forEach((p, i) => (i ? c.lineTo(p[0], p[1]) : c.moveTo(p[0], p[1]))); c.strokeStyle = C.ink; c.lineWidth = 1.6; c.lineJoin = "round"; c.stroke();
    c.restore();
    // 领头点
    let [lx, ly] = path[path.length - 1];
    if (clipX < lx) { lx = clipX; let k = path.findIndex((p) => p[0] >= clipX); ly = path[Math.max(0, k)][1]; }
    c.strokeStyle = "rgba(224,69,123,.28)"; c.beginPath(); c.moveTo(lx + 0.5, ly); c.lineTo(lx + 0.5, T + ph); c.stroke();
    const ph2 = (D.t * 1.3) % 1;
    c.beginPath(); c.arc(lx, ly, 4 + ph2 * 10, 0, Math.PI * 2); c.fillStyle = rgba(C.crim, 0.28 * (1 - ph2)); c.fill();
    c.beginPath(); c.arc(lx, ly, 4.2, 0, Math.PI * 2); c.fillStyle = C.crim; c.fill();
    const val = pts[pts.length - 1][1];
    const label = D.histMode === "eq" ? nf(val) : sgn(val);
    c.font = `600 11px ${MONO}`; const tw2 = c.measureText(label).width + 12;
    let bx = clamp(lx - tw2 / 2, L, w - R - tw2), by = ly - 30; if (by < 2) by = ly + 12;
    c.fillStyle = "#fff5f7"; c.strokeStyle = C.pink; c.lineWidth = 1; roundRect(c, bx + 0.5, by + 0.5, tw2, 18, 2); c.fill(); c.stroke();
    c.fillStyle = C.crim; c.textAlign = "center"; c.fillText(label, bx + tw2 / 2, by + 10);
  }

  // ---------------- 2) 尾部概率山脊 ----------------
  function drawRidge() {
    const o = D.cv.ridge, c = prep(o), w = o.w, h = o.h, g = D.agg; if (!g) return;
    const M = g.M, G = g.G, Z0 = g.Z0, Z1 = g.Z1;
    if (!D.ridgeDisp || D.ridgeDisp.length !== M) D.ridgeDisp = g.ridge.map((a) => Float32Array.from(a));
    const xL = 10, xR = w - 70, base = h - 30, A = h * 0.42, depthY = h * 0.34, depthX = w * 0.075;
    const zS = 1.5, t = D.t;
    c.font = `9.5px ${MONO}`; c.fillStyle = C.mut; c.textAlign = "left"; c.textBaseline = "middle";
    c.fillText(g.synthetic ? "盈亏密度 / 等待平仓记录" : "盈亏密度 / 滚动窗口", 8, 10);
    const xOf = (i, d) => { const xs = xL + (i / (G - 1)) * (xR - xL); const s = 1 - d * 0.1; return (xL + xR) / 2 + (xs - (xL + xR) / 2) * s + d * depthX; };
    const iS = Math.round(((zS - Z0) / (Z1 - Z0)) * (G - 1));
    const front = [];
    const strikePts = [];
    for (let k = 0; k < M; k++) {
      const tgt = g.ridge[k], disp = D.ridgeDisp[k];
      for (let i = 0; i < G; i++) disp[i] += (tgt[i] - disp[i]) * 0.06;
      const d = (M - 1 - k) / (M - 1), by = base - d * depthY, amp = A * (1 - d * 0.28);
      const ys = new Float32Array(G);
      for (let i = 0; i < G; i++) {
        const u = i / (G - 1);
        const wob = 1 + 0.07 * Math.sin(t * 1.15 + k * 0.42 + u * 6.3) + 0.04 * Math.sin(t * 0.6 - k * 0.3 + u * 11);
        ys[i] = by - amp * disp[i] * wob;
      }
      // 遮挡填充
      c.beginPath(); c.moveTo(xOf(0, d), by);
      for (let i = 0; i < G; i++) c.lineTo(xOf(i, d), ys[i]);
      c.lineTo(xOf(G - 1, d), by); c.closePath();
      if (k === M - 1) {
        const gr = c.createLinearGradient(0, by - amp, 0, by); gr.addColorStop(0, "rgba(40,40,40,.30)"); gr.addColorStop(1, "rgba(40,40,40,.02)");
        c.fillStyle = "#fff"; c.fill(); c.fillStyle = gr; c.fill();
      } else { c.fillStyle = "rgba(255,255,255,.78)"; c.fill(); }
      // 尾部（粉色）
      c.beginPath(); c.moveTo(xOf(iS, d), by);
      for (let i = iS; i < G; i++) c.lineTo(xOf(i, d), ys[i]);
      c.lineTo(xOf(G - 1, d), by); c.closePath(); c.fillStyle = rgba(C.pink, k === M - 1 ? 0.22 : 0.07); c.fill();
      // 线
      c.beginPath(); for (let i = 0; i <= iS; i++) (i ? c.lineTo(xOf(i, d), ys[i]) : c.moveTo(xOf(i, d), ys[i]));
      c.strokeStyle = k === M - 1 ? "#1f1f1f" : `rgba(70,70,70,${0.18 + 0.5 * (1 - d)})`; c.lineWidth = k === M - 1 ? 1.5 : 0.8; c.stroke();
      c.beginPath(); for (let i = iS; i < G; i++) (i > iS ? c.lineTo(xOf(i, d), ys[i]) : c.moveTo(xOf(i, d), ys[i]));
      c.strokeStyle = k === M - 1 ? C.crim : rgba(C.pink, 0.3 + 0.45 * (1 - d)); c.stroke();
      strikePts.push([xOf(iS, d), ys[iS]]);
      if (k === M - 1) for (let i = 0; i < G; i++) front.push([xOf(i, 0), ys[i]]);
    }
    // 行权线（虚线）
    c.setLineDash([3, 3]); c.strokeStyle = "rgba(30,30,30,.75)"; c.lineWidth = 1; c.beginPath();
    strikePts.forEach((p, i) => (i ? c.lineTo(p[0], p[1]) : c.moveTo(p[0], p[1]))); c.stroke(); c.setLineDash([]);
    c.beginPath(); c.moveTo(front[iS][0], front[iS][1]); c.lineTo(front[iS][0], base); c.stroke();
    // STRIKE / EXIT 标签
    const sp = strikePts[0]; c.font = `600 9.5px ${MONO}`;
    const lab = "止盈线 / 出场", lw = c.measureText(lab).width + 12, lx = clamp(sp[0] + 18, 0, w - lw - 4), ly = Math.max(4, sp[1] - 34);
    c.fillStyle = "#1b1b1d"; roundRect(c, lx, ly, lw, 16, 2); c.fill(); c.fillStyle = "#fff"; c.textAlign = "left"; c.fillText(lab, lx + 6, ly + 8.5);
    // x 轴
    c.font = `9.5px ${MONO}`; c.fillStyle = C.mut; c.textAlign = "center";
    [-2, -1, 0, 1, 2].forEach((z) => { const i = ((z - Z0) / (Z1 - Z0)) * (G - 1); c.fillText(z === 0 ? "0" : `${z > 0 ? "+" : "−"}${Math.abs(z)}σ`, xOf(i, 0), h - 10); });
    c.strokeStyle = C.line; c.beginPath(); c.moveTo(xL, base + 0.5); c.lineTo(xR, base + 0.5); c.stroke();
    // 追踪峰值的提示框
    let pi = 0; for (let i = 1; i < front.length; i++) if (front[i][1] < front[pi][1]) pi = i;
    const tx = tw("rpx", front[pi][0]).v; let ti = 0; for (let i = 1; i < front.length; i++) if (Math.abs(front[i][0] - tx) < Math.abs(front[ti][0] - tx)) ti = i;
    const [px, py] = front[ti];
    c.beginPath(); c.arc(px, py, 3.6, 0, Math.PI * 2); c.fillStyle = C.crim; c.fill();
    const l1 = `超过 +1.5σ 的概率 ${(g.tailMass * 100).toFixed(2)}%`, l2 = g.avgLoss ? `平均盈利 ${usd(g.avgWin)} · 盈亏比 ${(g.avgWin / g.avgLoss).toFixed(2)}` : g.synthetic ? "还没有平仓记录" : `单笔均值 ${usd(g.avg)}`, l3 = `平仓 ${g.n} 笔 · σ $${nf(g.sd)}`;
    c.font = `600 10px ${MONO}`; const bw = Math.max(c.measureText(l1).width, c.measureText(l2).width, c.measureText(l3).width) + 18;
    const bx = clamp(px + 14, 4, w - bw - 4), byy = clamp(py - 58, 16, h - 70);
    c.shadowColor = "rgba(0,0,0,.10)"; c.shadowBlur = 10; c.shadowOffsetY = 2; c.fillStyle = "#fff"; roundRect(c, bx, byy, bw, 50, 3); c.fill();
    c.shadowColor = "transparent"; c.strokeStyle = "#dcdcd8"; c.lineWidth = 1; c.stroke();
    c.textAlign = "left"; c.fillStyle = C.ink; c.fillText(l1, bx + 9, byy + 12);
    c.fillStyle = C.crim; c.fillText(l2, bx + 9, byy + 26); c.font = `9px ${MONO}`; c.fillStyle = C.mut; c.fillText(l3, bx + 9, byy + 39);
  }

  // ---------------- 3) 弦图 ----------------
  function chordData() {
    const sc = scopeAccs(), agg = D.agg || {};
    const w = {};
    sc.forEach((a) => (a.link === "online" ? a.positions || [] : []).forEach((p) => { const k = a.id + "|" + p.symbol; w[k] = (w[k] || 0) + 1 + p.volume * 4; }));
    if (!Object.keys(w).length) Object.entries(agg.recentFlows || {}).forEach(([k, v]) => { if (sc.some((a) => k.startsWith(a.id + "|"))) w[k] = v; });
    const key = JSON.stringify(w) + sc.map((a) => a.id).join(",");
    if (key === D.chordKey) return;
    D.chordKey = key;
    const symW = {}; Object.entries(w).forEach(([k, v]) => { const s = k.split("|")[1]; symW[s] = (symW[s] || 0) + v; });
    const syms = Object.entries(symW).sort((a, b) => b[1] - a[1]).slice(0, 8).map((x) => x[0]);
    const accs = sc.slice(0, 12);
    // 账户和品种交错排列，弦会穿过圆心，像视频里一样
    const nodes = []; const na = accs.length, ns = syms.length;
    let ia = 0, is = 0;
    while (ia < na || is < ns) {
      if (ia < na) { const a = accs[ia++]; nodes.push({ id: a.id, label: tagOf(a), col: colorOf(a.id), acc: true }); }
      if (is < ns && (ia * ns >= is * na || ia >= na)) { const s = syms[is]; nodes.push({ id: "s:" + s, label: s.replace(/\..*$/, "").slice(0, 7), col: SYMPAL[is % SYMPAL.length], acc: false }); is++; }
    }
    const prev = {}; (D.chordNodes || []).forEach((n) => (prev[n.id] = n));
    nodes.forEach((n, i) => { n.ta = -Math.PI / 2 + (i / nodes.length) * Math.PI * 2; n.a = prev[n.id] ? prev[n.id].a : n.ta; });
    D.chordNodes = nodes;
    const old = {}; D.links.forEach((l) => (old[l.k] = l));
    const mx = Math.max(1, ...Object.values(w));
    D.links = Object.entries(w).filter(([k]) => nodes.some((n) => "s:" + k.split("|")[1] === n.id) && nodes.some((n) => n.id === k.split("|")[0])).map(([k, v]) => {
      const [aid, s] = k.split("|");
      const l = old[k] || { k, al: 0, ta: 0.7, u1: Math.random() - 0.5, u2: Math.random() - 0.5, p: Math.random(), q: Math.random() };
      l.a = aid; l.s = "s:" + s; l.wt = v / mx; return l;
    });
  }
  function drawChord(dt) {
    chordData();
    const o = D.cv.chord, c = prep(o), w = o.w, h = o.h;
    const cx = w / 2, cy = h / 2 + 2, R = Math.min(w, h) / 2 - 30;
    const nodes = D.chordNodes || []; const N = nodes.length || 1;
    const span = ((Math.PI * 2) / N) * 0.62;
    nodes.forEach((n) => { n.a += (n.ta - n.a) * Math.min(1, dt * 3); });
    // 刻度圆
    c.strokeStyle = "#ececea"; c.lineWidth = 1; c.beginPath(); c.arc(cx, cy, R, 0, Math.PI * 2); c.stroke();
    // 每 ~1.6s 让一部分弦淡出、换位，再淡入
    if (D.t > D.chordT) {
      D.chordT = D.t + 1.6;
      D.links.forEach((l) => { l.ta = Math.random() < 0.3 ? 0.08 : 0.35 + 0.55 * Math.random(); });
    }
    const byId = {}; nodes.forEach((n) => (byId[n.id] = n));
    const P = (n, u) => { const a = n.a + u * span; return [cx + Math.cos(a) * (R - 3), cy + Math.sin(a) * (R - 3)]; };
    D.links.forEach((l) => {
      const A = byId[l.a], B = byId[l.s]; if (!A || !B) return;
      l.al += (l.ta - l.al) * Math.min(1, dt * 2.2);
      if (l.al < 0.1 && l.ta < 0.1) { l.u1 = (Math.random() - 0.5) * 0.9; l.u2 = (Math.random() - 0.5) * 0.9; }
      const [x1, y1] = P(A, l.u1), [x2, y2] = P(B, l.u2);
      const qx = cx + (((x1 + x2) / 2 - cx) * 0.18), qy = cy + (((y1 + y2) / 2 - cy) * 0.18);
      c.beginPath(); c.moveTo(x1, y1); c.quadraticCurveTo(qx, qy, x2, y2);
      c.strokeStyle = rgba(A.col, 0.12 + 0.6 * l.al * (0.4 + 0.6 * l.wt)); c.lineWidth = 0.7 + 2.2 * l.wt; c.stroke();
      // 沿弦移动的点
      l.p = (l.p + dt * (0.12 + 0.25 * l.wt)) % 1; l.q = (l.q + dt * 0.09) % 1;
      [l.p, l.q].forEach((tt, j) => {
        const u = 1 - tt, x = u * u * x1 + 2 * u * tt * qx + tt * tt * x2, y = u * u * y1 + 2 * u * tt * qy + tt * tt * y2;
        c.beginPath(); c.arc(x, y, j ? 1.3 : 1.9, 0, Math.PI * 2); c.fillStyle = j ? "rgba(90,90,90,.45)" : rgba(A.col, 0.55 + 0.4 * l.al); c.fill();
      });
    });
    // 节点弧段 + 标签
    c.font = `700 9.5px ${MONO}`; c.textBaseline = "middle";
    nodes.forEach((n) => {
      c.beginPath(); c.arc(cx, cy, R, n.a - span / 2, n.a + span / 2); c.strokeStyle = n.col; c.lineWidth = 3; c.lineCap = "round"; c.stroke(); c.lineCap = "butt";
      const lx = cx + Math.cos(n.a) * (R + 15), ly = cy + Math.sin(n.a) * (R + 13);
      c.fillStyle = n.col; const ca = Math.cos(n.a); c.textAlign = Math.abs(ca) < 0.3 ? "center" : ca > 0 ? "left" : "right"; c.fillText(n.label, lx, ly);
    });
    if (!D.links.length) { c.font = `9.5px ${MONO}`; c.fillStyle = C.mut; c.textAlign = "center"; c.fillText(nodes.length ? "暂无持仓" : "暂无账户", cx, cy); }
  }

  // ---------------- 4) 5D 晶格 ----------------
  function initLattice() {
    const V = []; for (let i = 0; i < 32; i++) V.push([0, 1, 2, 3, 4].map((b) => ((i >> b) & 1 ? 1 : -1)));
    const E = []; for (let i = 0; i < 32; i++) for (let b = 0; b < 5; b++) { const j = i ^ (1 << b); if (j > i) E.push([i, j, b]); }
    const F = []; for (let a = 0; a < 5; a++) for (let b = a + 1; b < 5; b++) for (let i = 0; i < 32; i++) if (!((i >> a) & 1) && !((i >> b) & 1)) F.push([i, i | (1 << a), i | (1 << a) | (1 << b), i | (1 << b), a, b]);
    D.lat = { V, E, F, rot: 0.3, ang: [0.3, 0.9, 0.5, 1.7, 0.2] };
  }
  function drawLattice(dt) {
    const o = D.cv.lat, c = prep(o), w = o.w, h = o.h, L = D.lat;
    const sp = 0.32 + D.boost * 0.25; D.boost = Math.max(0, D.boost - dt * 0.5);
    L.rot += dt * sp; L.ang[0] += dt * sp; L.ang[1] += dt * sp * 0.61; L.ang[2] += dt * sp * 0.43; L.ang[3] += dt * sp * 0.29; L.ang[4] += dt * sp * 0.77;
    const planes = [[0, 1], [2, 3], [1, 4], [0, 3], [2, 4]];
    const cs = L.ang.map(Math.cos), sn = L.ang.map(Math.sin);
    const cx = w / 2, cy = h / 2 - 8, S = Math.min(w * 0.7, h) * 0.4;
    const P = L.V.map((v0) => {
      const v = v0.slice();
      planes.forEach(([a, b], k) => { const x = v[a], y = v[b]; v[a] = x * cs[k] - y * sn[k]; v[b] = x * sn[k] + y * cs[k]; });
      let x = v[0], y = v[1], z = v[2], q = v[3], r = v[4];
      const k5 = 7 / (7 - r); x *= k5; y *= k5; z *= k5; q *= k5;
      const k4 = 7 / (7 - q); x *= k4; y *= k4; z *= k4;
      const k3 = 6 / (6 - z); return [cx + x * k3 * S * 0.46, cy + y * k3 * S * 0.46, z];
    });
    const glow = c.createRadialGradient(cx, cy, 4, cx, cy, S * 2.2); glow.addColorStop(0, "rgba(224,69,123,.10)"); glow.addColorStop(0.6, "rgba(58,98,208,.05)"); glow.addColorStop(1, "rgba(255,255,255,0)");
    c.fillStyle = glow; c.fillRect(0, 0, w, h);
    L.F.forEach((f, i) => {
      if (i % 2) return;
      c.beginPath(); c.moveTo(P[f[0]][0], P[f[0]][1]); c.lineTo(P[f[1]][0], P[f[1]][1]); c.lineTo(P[f[2]][0], P[f[2]][1]); c.lineTo(P[f[3]][0], P[f[3]][1]); c.closePath();
      c.fillStyle = rgba(DIMC[(f[4] + f[5]) % 5], 0.028); c.fill();
    });
    c.lineWidth = 1;
    L.E.forEach(([i, j, b]) => { const dz = (P[i][2] + P[j][2]) / 2; c.strokeStyle = rgba(DIMC[b], 0.35 + 0.35 * clamp(dz + 0.5, 0, 1)); c.beginPath(); c.moveTo(P[i][0], P[i][1]); c.lineTo(P[j][0], P[j][1]); c.stroke(); });
    P.forEach((p, i) => { c.beginPath(); c.arc(p[0], p[1], 2.1, 0, Math.PI * 2); c.fillStyle = DIMC[(i * 7) % 5]; c.fill(); });
    c.font = `9px ${MONO}`; c.textBaseline = "middle"; c.textAlign = "left";
    const lw = (w - 20) / 5;
    DIMC.forEach((col, i) => { const x = 10 + i * lw; c.beginPath(); c.arc(x + 3, h - 9, 3, 0, Math.PI * 2); c.fillStyle = col; c.fill(); c.fillStyle = C.ink; c.fillText(`维度${"一二三四五"[i]}`, x + 10, h - 9); });
  }

  // ---------------- 5) 关系图（主仓 → 跟单） ----------------
  function groups() {
    const sc = scopeAccs();
    const gs = [...new Set(sc.map((a) => a.group || "未分组"))];
    const master = gs.find((g) => /主|master|main/i.test(g)) || gs[0];
    const fol = gs.find((g) => g !== master && /跟|copy|follow/i.test(g)) || gs.find((g) => g !== master);
    const res = { master, fol: sc.filter((a) => (a.group || "未分组") === fol), folName: fol, mas: sc.filter((a) => (a.group || "未分组") === master), other: sc.filter((a) => ![master, fol].includes(a.group || "未分组")) };
    const og = [...new Set(res.other.map((a) => a.group || "未分组"))]; res.otherName = og.length === 1 ? og[0] : og.length ? "其他" : "空";
    D.groups = res; return res;
  }
  function satellites(hubKey, accs, now) {
    const key = accs.map((a) => a.id + ":" + (a.link === "online" ? (a.positions || []).map((p) => p.ticket + (p.profit >= 0 ? "+" : "-")).join(".") : "x")).join("|");
    let S = D.sats[hubKey];
    if (!S || S.key !== key) {
      const prev = {}; (S ? S.list : []).forEach((s) => (prev[s.id] = s));
      const list = [];
      accs.forEach((a, ai) => {
        const ang0 = (ai / Math.max(1, accs.length)) * Math.PI * 2 - Math.PI / 2 + (hubKey === "f" ? 0.4 : hubKey === "o" ? 1.1 : -0.6);
        list.push(prev[a.id] || { id: a.id, ang: ang0, r: 44 + (ai % 2) * 14, ox: 0, oy: 0, vx: 0, vy: 0, acc: a.id });
        (a.link === "online" ? a.positions || [] : []).slice(0, 10).forEach((p, pi) => {
          const id = a.id + ":" + p.ticket;
          list.push(prev[id] || { id, ang: ang0 + (pi - 2) * 0.32 + Math.random() * 0.2, r: 26 + Math.random() * 40, ox: 0, oy: 0, vx: 0, vy: 0, pos: p.ticket, acc: a.id });
        });
      });
      // 账户少时补几颗装饰点，保持画面
      const dc = [C.blue, C.pink, C.teal, "#9a9a94", "#9a9a94"];
      for (let i = list.length; i < 14; i++) list.push(prev["d" + hubKey + i] || { id: "d" + hubKey + i, ang: i * 2.39, r: 16 + ((i * 37) % 56), ox: 0, oy: 0, vx: 0, vy: 0, deco: true, col: dc[i % dc.length], sz: 1.4 + (i % 3) * 0.6 });
      S = D.sats[hubKey] = { key, list };
    }
    void now; return S.list;
  }
  function spawn(col, delay = 0) { if (D.particles.length < 160) D.particles.push({ s: -delay, v: 0.16 + Math.random() * 0.12, col, j: (Math.random() - 0.5) * 6, path: Math.random() < 0.8 ? 0 : 1 }); }
  function bez(p, t) { const u = 1 - t; return [u * u * u * p[0][0] + 3 * u * u * t * p[1][0] + 3 * u * t * t * p[2][0] + t * t * t * p[3][0], u * u * u * p[0][1] + 3 * u * u * t * p[1][1] + 3 * u * t * t * p[2][1] + t * t * t * p[3][1]]; }
  function drawGraph(dt) {
    const o = D.cv.graph, c = prep(o), w = o.w, h = o.h, G = groups(), now = performance.now();
    const hubs = [
      { k: "m", x: w * 0.25, y: h * 0.40, col: C.pink, ring: false, label: `主仓 · ${G.master || "主仓"}`, accs: G.mas },
      { k: "f", x: w * 0.76, y: h * 0.38, col: C.teal, ring: true, label: `跟单 · ${G.folName || "跟单"}`, accs: G.fol },
      { k: "o", x: w * 0.52, y: h * 0.74, col: C.purple, ring: false, label: `其他 · ${G.otherName}`, accs: G.other },
    ];
    const paths = [
      [[w * 0.01, h * 0.50], [w * 0.30, h * 0.66], [w * 0.62, h * 0.66], [w * 0.985, h * 0.50]],
      [[hubs[0].x, hubs[0].y], [hubs[0].x + 30, h * 0.80], [hubs[2].x - 80, hubs[2].y + 10], [hubs[2].x - 14, hubs[2].y]],
    ];
    // 卫星（布朗抖动）
    let nodes = 3, edges = 2;
    hubs.forEach((hb) => {
      const list = satellites(hb.k, hb.accs, now);
      list.forEach((s) => {
        s.vx += (Math.random() - 0.5) * 0.9; s.vy += (Math.random() - 0.5) * 0.9; s.vx *= 0.86; s.vy *= 0.86; s.ox += s.vx * dt * 6; s.oy += s.vy * dt * 6; s.ox *= 0.985; s.oy *= 0.985;
        s.ang += dt * 0.03;
        const sx = Math.max(1, w / 700), x = hb.x + Math.cos(s.ang) * s.r * 1.5 * sx + s.ox, y = hb.y + Math.sin(s.ang) * s.r * 0.95 + s.oy;
        s.x = x; s.y = y;
        c.strokeStyle = "rgba(30,30,30,.13)"; c.lineWidth = 0.7; c.beginPath(); c.moveTo(hb.x, hb.y); c.lineTo(x, y); c.stroke();
        nodes++; edges++;
      });
      hb.list = list;
    });
    // 主路径（虚线流动）+ 箭头
    D.dash = ((D.dash || 0) - dt * 18) % 1000;
    c.setLineDash([5, 4]); c.lineDashOffset = D.dash; c.strokeStyle = "rgba(30,30,40,.8)"; c.lineWidth = 1.4;
    const p0 = paths[0]; c.beginPath(); c.moveTo(...p0[0]); c.bezierCurveTo(...p0[1], ...p0[2], ...p0[3]); c.stroke();
    c.strokeStyle = "rgba(59,33,80,.35)"; c.lineWidth = 1; const p1 = paths[1]; c.beginPath(); c.moveTo(...p1[0]); c.bezierCurveTo(...p1[1], ...p1[2], ...p1[3]); c.stroke();
    c.setLineDash([]);
    const [ax, ay] = p0[3], [bx2, by2] = bez(p0, 0.97), an = Math.atan2(ay - by2, ax - bx2);
    c.fillStyle = C.ink; c.beginPath(); c.moveTo(ax, ay); c.lineTo(ax - 8 * Math.cos(an - 0.4), ay - 8 * Math.sin(an - 0.4)); c.lineTo(ax - 8 * Math.cos(an + 0.4), ay - 8 * Math.sin(an + 0.4)); c.closePath(); c.fill();
    // 粒子
    if (Math.random() < dt * (0.9 + D.boost)) spawn(Math.random() < (D.agg ? D.agg.winRate || 0.5 : 0.5) ? C.blue : C.pink);
    D.particles = D.particles.filter((p) => p.s < 1);
    D.particles.forEach((p) => {
      p.s += dt * p.v; if (p.s < 0) return;
      const [x, y] = bez(paths[p.path], p.s);
      const [x0, y0] = bez(paths[p.path], Math.max(0, p.s - 0.025));
      c.strokeStyle = rgba(p.col, 0.25); c.lineWidth = 2; c.beginPath(); c.moveTo(x0, y0 + p.j * 0.5); c.lineTo(x, y + p.j); c.stroke();
      c.beginPath(); c.arc(x, y + p.j, 2.3, 0, Math.PI * 2); c.fillStyle = p.col; c.fill();
    });
    // 卫星点
    c.font = `8.5px ${MONO}`; c.textBaseline = "middle"; c.textAlign = "left";
    hubs.forEach((hb) => hb.list.forEach((s) => {
      if (s.deco) { c.beginPath(); c.arc(s.x, s.y, s.sz || 1.8, 0, Math.PI * 2); c.fillStyle = rgba(s.col || "#9a9a94", 0.75); c.fill(); return; }
      const a = accById(s.acc); if (!a) return;
      if (s.pos) {
        const p = (a.positions || []).find((x) => x.ticket === s.pos); const col = p && p.profit < 0 ? C.pink : C.blue;
        c.beginPath(); c.arc(s.x, s.y, 2.4, 0, Math.PI * 2); c.fillStyle = col; c.fill();
      } else {
        const col = colorOf(a.id);
        c.beginPath(); c.arc(s.x, s.y, 3.6, 0, Math.PI * 2); c.fillStyle = col; c.fill();
        c.fillStyle = "rgba(60,60,60,.85)"; c.fillText(`${tagOf(a)} ${a.link === "online" ? sgn(a.floating, 0) : "离线"}`, s.x + 6, s.y - 1);
      }
    }));
    // 枢纽
    hubs.forEach((hb, i) => {
      const pulse = 1 + 0.06 * Math.sin(D.t * 2 + i);
      c.beginPath(); c.arc(hb.x, hb.y, 19 * pulse, 0, Math.PI * 2); c.strokeStyle = rgba(hb.col, 0.35); c.lineWidth = 1.5; c.stroke();
      c.beginPath(); c.arc(hb.x, hb.y, 13, 0, Math.PI * 2);
      if (hb.ring) { c.fillStyle = "#fff"; c.fill(); c.lineWidth = 4; c.strokeStyle = hb.col; c.stroke(); c.beginPath(); c.arc(hb.x, hb.y, 7, 0, Math.PI * 2); c.fillStyle = hb.col; c.fill(); }
      else { c.fillStyle = hb.col; c.fill(); }
      c.font = `600 9px ${MONO}`; const tl = hb.label + (hb.accs.length ? ` (${hb.accs.length})` : ""); const lw = c.measureText(tl).width + 12;
      const lx = hb.x - lw / 2, ly = hb.y + 24;
      c.fillStyle = "#fff"; c.strokeStyle = "#cfcfca"; c.lineWidth = 1; roundRect(c, lx + 0.5, ly + 0.5, lw, 16, 2); c.fill(); c.stroke();
      c.fillStyle = C.ink; c.textAlign = "center"; c.fillText(tl, hb.x, ly + 8.5);
    });
    // 药丸标签
    const pill = "实盘 / 跟单流向", pc = C.blue;
    c.font = `700 9px ${MONO}`; const pw = c.measureText(pill).width + 14, px = w * 0.43 - pw / 2, py = h * 0.17;
    c.fillStyle = pc; roundRect(c, px, py, pw, 16, 2); c.fill(); c.fillStyle = "#fff"; c.textAlign = "center"; c.fillText(pill, px + pw / 2, py + 8.5);
    setText($("pdGraphN"), `${nodes} 个节点 · ${edges + D.particles.length} 条连线`);
  }

  // ---------------- 6) 每日盈亏分布（小直方图） ----------------
  function drawHisto(dt) {
    const o = D.cv.histo, c = prep(o), w = o.w, h = o.h, g = D.agg; if (!g) return;
    const n = g.bins.length, mx = Math.max(1, ...g.bins);
    if (!D.hb || D.hb.length !== n) D.hb = new Array(n).fill(0);
    if (!D.hbT || D.t > D.hbT) { D.hbT = D.t + 0.7; D.hj = g.bins.map((v) => (v ? 1 + (Math.random() - 0.5) * 0.18 : 0)); }
    const bw = (w - 8) / n;
    let zi = g.bmax > g.bmin ? Math.floor(((0 - g.bmin) / (g.bmax - g.bmin)) * n) : -1;
    for (let i = 0; i < n; i++) {
      const tgt = (g.bins[i] / mx) * (h - 14) * (D.hj ? D.hj[i] : 1);
      D.hb[i] += (tgt - D.hb[i]) * Math.min(1, dt * 8);
      const center = g.bmin + ((i + 0.5) / n) * (g.bmax - g.bmin);
      const col = center < 0 ? C.pink : i - zi <= 3 ? C.teal : C.blue;
      c.fillStyle = col; c.fillRect(4 + i * bw + 1, h - 10 - D.hb[i], bw - 2, D.hb[i]);
    }
    c.strokeStyle = C.line; c.beginPath(); c.moveTo(0, h - 9.5); c.lineTo(w, h - 9.5); c.stroke();
    if (!g.days) { c.font = `9px ${MONO}`; c.fillStyle = C.mut; c.textAlign = "center"; c.textBaseline = "middle"; c.fillText("暂无每日盈亏", w / 2, h / 2); }
  }

  // ---------------- 主循环 ----------------
  function frame(ts) {
    D.raf = 0;
    if (!D.active) return;
    if (document.hidden) { D.last = 0; return; }   // 页面隐藏时暂停；visibilitychange 再恢复
    const dt = D.last ? Math.min(0.1, (ts - D.last) / 1000) : 0.016; D.last = ts; D.t += dt;
    D.frames++; if (ts - D.fpsT > 1000) { D.fps = Math.round((D.frames * 1000) / (ts - D.fpsT)); D.frames = 0; D.fpsT = ts; }
    try {
      stepTweens(dt);
      updateTexts(ts);
      updateLit(ts);
      drawHist(dt); drawRidge(dt); drawChord(dt); drawLattice(dt); drawGraph(dt); drawHisto(dt);
    } catch (e) { console.error("desk", e); }
    D.raf = requestAnimationFrame(frame);
  }
  function kick() { if (D.active && !D.raf && !document.hidden) { D.last = 0; D.raf = requestAnimationFrame(frame); } }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { kick(); pollDesk(); } });

  window.Desk = {
    start() {
      build();
      if (!D.active) { D.active = true; D.reveal = 0; pollDesk(); }
      kick();
    },
    stop() { D.active = false; clearTimeout(D.pollTimer); if (D.raf) cancelAnimationFrame(D.raf); D.raf = 0; },
    onState(st) {
      D.st = st;
      if (st && st.clock && (st.clock.unix || st.clock.offset)) {
        D.srvSkew = (Number(st.clock.offset) || 0) * 1000; D._clockFromState = true;
      }
      // 净值补点用本机 unix；标签通过 hm 加偏移显示服务器时间
      const on = scopeAccs().filter((a) => a.link === "online");
      if (on.length) {
        D.fine = D.fine || []; const t = Date.now() / 1000;
        D.fine.push([t, on.reduce((s2, a) => s2 + (a.equity || 0), 0)]);
        const cut = D.eqLast || 0; while (D.fine.length && (D.fine[0][0] <= cut - 1 || D.fine.length > 400)) D.fine.shift();
      }
      if (!D.built) return;
      renderAvatars(scopeAccs());
      if (D.popOpen) renderPick();
    },
    filterIds() { return filterIds(); },
    scopeAccs() { return scopeAccs(); },
  };
})();
