/* 交易页「快捷交易面板」：仿 MT5 EA 面板的一键开仓 / 平仓，作用于面板里自选的账户（与账号页勾选无关）。
   数据：/api/state（app.js 轮询后传进来，账户列表）+ /api/quick/summary（所选账户在当前品种上的汇总，1 秒刷新）。 */
"use strict";
(function () {
  const byId = (id) => document.getElementById(id);
  const h = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const nf = (n, d = 2) => (Number(n) || 0).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  const sg = (n, d = 2) => (n > 0 ? "+" : n < 0 ? "-" : "") + nf(Math.abs(n), d);
  const cls = (n) => (n > 0 ? "q-up" : n < 0 ? "q-dn" : "");
  const pad = (n) => String(n).padStart(2, "0");
  const hms = (ms) => { const d = new Date(ms); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
  const LS = { accs: "fleet.quickAccs", lots: "fleet.quickLots", sym: "fleet.quickSym", mode: "fleet.quickMode", ref: "fleet.quickRef", only: "fleet.quickOnly" };
  const ACT = {
    buy: "一键开多", sell: "一键开空", pair: "一键开单", close_long: "平全部多", close_short: "平全部空",
    close_profit: "平所有盈利", close_loss: "平所有亏损", nuke: "核弹级全平",
  };
  const MODE = { fixed: "固定手数", multiplier: "按账户倍数", equity: "按净值比例" };

  const Q = { st: null, sel: new Set(), sum: null, active: false, timer: 0, inflight: false, gen: 0, busy: "", popOpen: false, built: false, res: null };
  window.__quick = Q;
  try { const v = JSON.parse(localStorage.getItem(LS.accs) || "[]"); if (Array.isArray(v)) Q.sel = new Set(v.map(String)); } catch (e) { Q.sel = new Set(); }

  const accs = () => (Q.st && Q.st.accounts) || [];
  function ids() { const all = new Set(accs().map((a) => a.id)); return [...Q.sel].filter((i) => all.has(i)); }
  function selAccs() { const s = new Set(ids()); return accs().filter((a) => s.has(a.id)); }
  function symbol() { return (byId("qSym").value || "").trim(); }
  function setSel(list) {
    Q.sel = new Set(list);
    try { localStorage.setItem(LS.accs, JSON.stringify([...Q.sel])); } catch (e) { /* 隐私模式 */ }
    Q.gen++; Q.sum = null; renderPick(); renderAll(); refresh();
  }

  // ---------------- 结构 ----------------
  function build() {
    if (Q.built || !byId("qp")) return;
    Q.built = true;
    const s = (Q.st && Q.st.settings) || {};
    byId("qSym").value = localStorage.getItem(LS.sym) || s.default_symbol || "USDJPYc";
    byId("qLots").value = localStorage.getItem(LS.lots) || "0.01";
    byId("qMode").value = MODE[localStorage.getItem(LS.mode)] ? localStorage.getItem(LS.mode) : "fixed";
    byId("qRef").value = localStorage.getItem(LS.ref) || "10000";
    byId("qOnly").checked = localStorage.getItem(LS.only) !== "0";
    byId("qRefWrap").classList.toggle("hidden", byId("qMode").value !== "equity");
    byId("qSym").addEventListener("change", () => {
      const v = symbol();
      if (!/^[A-Za-z0-9._#&+\-]{1,32}$/.test(v)) { toast("品种格式不对（例如 USDJPYc）", true); return; }
      localStorage.setItem(LS.sym, v); Q.gen++; Q.sum = null; renderAll(); refresh();
    });
    byId("qSym").addEventListener("keydown", (e) => { if (e.key === "Enter") e.target.blur(); });
    byId("qLots").addEventListener("change", () => { const v = Number(byId("qLots").value); if (v > 0) localStorage.setItem(LS.lots, String(v)); });
    byId("qMode").addEventListener("change", () => { localStorage.setItem(LS.mode, byId("qMode").value); byId("qRefWrap").classList.toggle("hidden", byId("qMode").value !== "equity"); });
    byId("qRef").addEventListener("change", () => localStorage.setItem(LS.ref, byId("qRef").value));
    byId("qOnly").addEventListener("change", () => { localStorage.setItem(LS.only, byId("qOnly").checked ? "1" : "0"); renderAll(); });
    byId("qp").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-q]"); if (b) return onAction(b.dataset.q);
      if (e.target.closest("#qArm")) return onArm();
    });
    initPick();
  }

  // ---------------- 选择账户（与收益页「选择账户」同款） ----------------
  function pickLabel() {
    const n = accs().length, s = selAccs();
    if (!s.length) return "选择账户：未选 ▾";
    if (s.length === n) return `选择账户：全部（${n}）▾`;
    return `选择账户：${s.length === 1 ? s[0].alias || s[0].login : `${s.length} / ${n}`} ▾`;
  }
  function renderPick() {
    const btn = byId("qpPick"); if (!btn) return;
    const s = selAccs();
    btn.textContent = pickLabel();
    btn.title = s.length ? "操作这些账户：" + s.map((a) => `${a.alias}（${a.login}）`).join("、") : "还没有选择账户";
    btn.classList.toggle("on", s.length > 0);
    btn.setAttribute("aria-expanded", Q.popOpen ? "true" : "false");
    const pop = byId("qpPop");
    pop.classList.toggle("hidden", !Q.popOpen);
    if (!Q.popOpen) return;
    const list = accs(), sel = new Set(ids());
    const groups = [...new Set(list.map((a) => a.group || "未分组"))];
    const key = JSON.stringify(list.map((a) => [a.id, a.alias, a.login, a.group, a.link]));
    const fl = (a) => `<span class="fl ${cls(a.floating)}">${a.link === "online" ? sg(a.floating) : "—"}</span>`;
    if (pop._k === key) {    // 账户没变：只同步勾选和浮动盈亏，不重建（保持焦点和滚动位置）
      pop.querySelectorAll("input[data-qpid]").forEach((cb) => { const v = sel.has(cb.dataset.qpid); if (cb.checked !== v) cb.checked = v; });
      list.forEach((a) => { const el = pop.querySelector(`.fl[data-f="${CSS.escape(a.id)}"]`); if (el) { const t = a.link === "online" ? sg(a.floating) : "—"; if (el.textContent !== t) el.textContent = t; el.className = "fl " + cls(a.floating); el.dataset.f = a.id; } });
      const foot = pop.querySelector(".pp-foot span"); if (foot && !foot._hint) foot.textContent = `已选 ${sel.size} / ${list.length} · 自动保存`;
      if (foot) foot._hint = false;
      return;
    }
    pop._k = key;
    const top = pop.querySelector(".pp-list") ? pop.querySelector(".pp-list").scrollTop : 0;
    pop.innerHTML = !list.length ? `<div class="pp-lab">还没有账户 · 先在账号页添加并登录</div>` : `
      <div class="pp-row"><button type="button" data-qp="all">全选</button><button type="button" data-qp="inv">反选</button><button type="button" data-qp="online">只看在线</button><button type="button" data-qp="none">清空</button></div>
      ${groups.length > 1 ? `<div class="pp-lab">按分组</div><div class="pp-row">${groups.map((g) => `<button type="button" data-qp="g" data-g="${h(g)}">${h(g)}</button>`).join("")}</div>` : ""}
      <div class="pp-list">${list.map((a) => `<label><input type="checkbox" data-qpid="${h(a.id)}"${sel.has(a.id) ? " checked" : ""}><span class="nm">${h(a.alias || a.login)}</span><span class="lg">${h(a.login)}</span><span class="st${a.link === "online" ? " on" : ""}">${a.link === "online" ? "在线" : a.link === "error" ? "出错" : "离线"}</span>${fl(a).replace('class="fl', `data-f="${h(a.id)}" class="fl`)}</label>`).join("")}</div>
      <div class="pp-foot"><span>已选 ${sel.size} / ${list.length} · 自动保存</span><button type="button" data-qp="close">完成</button></div>`;
    const l2 = pop.querySelector(".pp-list"); if (l2) l2.scrollTop = top;
  }
  function pickHint(msg) { const f = document.querySelector("#qpPop .pp-foot span"); if (f) { f.textContent = msg; f._hint = true; f.style.color = "#ff6b6b"; setTimeout(() => { f.style.color = ""; }, 2500); } }
  function initPick() {
    const btn = byId("qpPick"), pop = byId("qpPop");
    btn.addEventListener("click", (e) => { e.stopPropagation(); Q.popOpen = !Q.popOpen; pop._k = ""; renderPick(); });
    pop.addEventListener("click", (e) => {
      e.stopPropagation();
      const b = e.target.closest("button[data-qp]"); if (!b) return;
      const list = accs(), cur = new Set(ids()), act = b.dataset.qp;
      if (act === "close") { Q.popOpen = false; return renderPick(); }
      let next = null;
      if (act === "all") next = list.map((a) => a.id);
      else if (act === "none") next = [];
      else if (act === "inv") next = list.filter((a) => !cur.has(a.id)).map((a) => a.id);
      else if (act === "online") next = list.filter((a) => a.link === "online").map((a) => a.id);
      else if (act === "g") next = list.filter((a) => (a.group || "未分组") === b.dataset.g).map((a) => a.id);
      if (next === null) return;
      if (act === "online" && !next.length) return pickHint("现在没有在线账户");
      setSel(next);
    });
    pop.addEventListener("change", (e) => {
      const cb = e.target.closest("input[data-qpid]"); if (!cb) return;
      const cur = new Set(ids());
      if (cb.checked) cur.add(cb.dataset.qpid); else cur.delete(cb.dataset.qpid);
      setSel(accs().map((a) => a.id).filter((i) => cur.has(i)));
    });
    document.addEventListener("click", (e) => { if (Q.popOpen && !e.target.closest("#qpPickWrap")) { Q.popOpen = false; renderPick(); } });
    document.addEventListener("keydown", (e) => { if (Q.popOpen && e.key === "Escape") { Q.popOpen = false; renderPick(); btn.focus(); } });
  }

  // ---------------- 数据 ----------------
  async function refresh() {
    clearTimeout(Q.timer);
    if (!Q.active) return;
    if (!document.hidden && !Q.inflight && Q.st) {
      Q.inflight = true;
      const gen = Q.gen;
      try {
        const q = `/api/quick/summary?symbol=${encodeURIComponent(symbol())}&accounts=${encodeURIComponent(ids().join(","))}`;
        const r = await fetch(q, { headers: { "X-Fleet-Token": window.FLEET_TOKEN } });
        if (r.ok) { const j = await r.json(); Q.old = false; if (gen === Q.gen) { Q.sum = j; renderAll(); } }
        else if (r.status === 404 && !Q.old) { Q.old = true; renderAll(); }   // 后台还是旧版本
      } catch (e) { /* 断线提示由 app.js 负责 */ }
      Q.inflight = false;
    }
    Q.timer = setTimeout(refresh, 1000);
  }

  // ---------------- 显示 ----------------
  function set(id, html) { const el = byId(id); if (el && el._h !== html) { el._h = html; el.innerHTML = html; } }
  function layers(side, d) {
    const L = side.layers || [];
    if (!L.length) return "—";
    const shown = L.slice(0, 10).map((x, i) => `L${i + 1}:${Number(x[0]).toFixed(d)}${x[1] > 1 ? `×${x[1]}` : ""}`).join(" ");
    return shown + (L.length > 10 ? ` …共 ${L.length} 层` : "");
  }
  function renderAll() {
    if (!Q.built) return;
    const s = Q.sum, sel = selAccs();
    const d = (s && s.quote && s.quote.digits) || 3;
    const q = s && s.quote;
    set("qQuote", q ? `${Number(q.bid).toFixed(d)} / ${Number(q.ask).toFixed(d)}${s.quotesDiffer ? ` <span class="q-note" title="不同券商报价不同，这里显示 ${h(q.from)} 的报价">（${h(q.from)}）</span>` : ""}` : (sel.length ? `<span class="q-note">${s ? "所选在线账户里取不到这个品种的报价" : "读取中…"}</span>` : "— / —"));
    set("qToday", s ? `<span>${sg(s.today)}</span>` : "—");
    set("qHist", s ? `<span>${sg(s.hist)}</span>` : "—");
    set("qDD", s ? `${nf(s.drawdown)}%` : "—");
    const lo = (s && s.long) || { n: 0, lots: 0, vwap: 0, profit: 0, layers: [] }, sh = (s && s.short) || { n: 0, lots: 0, vwap: 0, profit: 0, layers: [] };
    set("qPos", `多 L${lo.n} ${nf(lo.lots)}手 盈亏价 ${lo.lots ? Number(lo.vwap).toFixed(d) : "—"} | 空 L${sh.n} ${nf(sh.lots)}手 盈亏价 ${sh.lots ? Number(sh.vwap).toFixed(d) : "—"}`);
    set("qPnl", `持仓盈亏: 多 ${sg(lo.profit)} | 空 ${sg(sh.profit)} | 净 ${sg(s ? s.net : 0)}`);
    set("qLayL", `多层级价位: ${layers(lo, d)}`);
    set("qLayS", `空层级价位: ${layers(sh, d)}`);
    // 按钮
    const online = s ? s.online : sel.filter((a) => a.link === "online").length;
    byId("qp").querySelectorAll("button[data-q]").forEach((b) => {
      b.disabled = !!Q.busy || !sel.length || !online || !!Q.old;
      b.textContent = Q.busy === b.dataset.q ? "执行中…" : ACT[b.dataset.q];
    });
    // 自动全平
    const au = s && s.auto, inp = byId("qAuto"), arm = byId("qArm");
    if (au) {
      if (document.activeElement !== inp) inp.value = nf(au.amount).replace(/,/g, "");
      inp.disabled = true; arm.textContent = "取消自动"; arm.classList.add("on");
      arm.title = "取消自动全平";
    } else {
      inp.disabled = false; arm.textContent = "启用"; arm.classList.remove("on");
      arm.title = "合计浮动盈亏达到这个金额时自动核弹级全平（正数=止盈，负数=止损，0=不启用）";
    }
    arm.disabled = !au && (!sel.length || !!Q.busy);
    // 结果 / 状态
    renderRes();
    const last = s && s.last;
    const autoTxt = au ? `<span class="q-armed">自动全平已启用：${au.amount > 0 ? "≥" : "≤"} ${sg(au.amount)}（当前 ${sg(au.net)}，${au.ids.length} 个账户${au.online < au.ids.length ? `，在线 ${au.online}` : ""}）</span>`
      : (s && s.autoLast ? `自动全平：已于 ${hms(s.autoLast.at)} 触发（${sg(s.autoLast.net)}），成功 ${s.autoLast.ok} / 失败 ${s.autoLast.fail}` : "自动全平：未启用");
    set("qStatus", `状态: 在线 ${online}/${sel.length} | ${last ? `上次 ${h(last.title)} ${h(last.symbol || "")} 成功 ${last.ok} / 失败 ${last.fail}（${hms(last.at)}）` : "还没有操作"} | ${autoTxt}`);
    renderSide();
  }
  function renderRes() {
    const sel = selAccs(), s = Q.sum;
    if (Q.old) return set("qRes", `<div class="q-hint">后台还是旧版本：请双击一次「一键启动.vbs」换成新版本（MT5 终端和 EA 不受影响），然后刷新网页。</div>`);
    if (!sel.length) return set("qRes", `<div class="q-hint">先点上方「选择账户」勾选要操作的账户（可以全选、按分组选）。</div>`);
    if (s && !s.online) return set("qRes", `<div class="q-hint">所选账户都不在线：先在账号页登录。</div>`);
    const r = Q.res;
    if (!r) return set("qRes", "");
    const bad = r.results.filter((x) => !x.ok), good = r.results.filter((x) => x.ok);
    set("qRes", `<div class="q-rh"><b>${h(r.title)}</b>：<span class="q-up">成功 ${r.ok}</span> / <span class="${r.fail ? "q-dn" : ""}">失败 ${r.fail}</span><span class="q-note"> · ${(r.ms / 1000).toFixed(1)} 秒 · ${hms(r.at)}</span><button type="button" class="q-x" onclick="window.__quick.res=null;window.__quick.render()">×</button></div>
      <div class="q-rl">${bad.map((x) => `<div class="q-dn">✗ ${h(x.alias)}：${h(x.message)}</div>`).join("")}${good.map((x) => `<div class="q-ok">✓ ${h(x.alias)}：${h(x.message)}</div>`).join("")}</div>`);
  }
  function renderSide() {
    const s = Q.sum, sel = selAccs();
    if (!sel.length) return set("qSide", `<h3>已选账户</h3><p class="q-note">还没有选择账户。</p>`);
    const rows = (s && s.accounts) || sel.map((a) => ({ id: a.id, alias: a.alias, login: a.login, link: a.link, floating: a.floating, longLots: 0, shortLots: 0, symProfit: 0, symbol: "", error: "" }));
    const tot = rows.reduce((t, r) => t + (r.link === "online" ? Number(r.floating) || 0 : 0), 0);
    set("qSide", `<h3>已选账户（${rows.length}）<span class="q-note"> · 合计浮动 <b class="${cls(tot)}">${sg(tot)}</b> · 本品种 ${h(symbol())}</span></h3>
      <div class="q-tw"><table><thead><tr><th>账户</th><th>账号</th><th>状态</th><th>浮动</th><th>多</th><th>空</th><th>本品种盈亏</th></tr></thead><tbody>
      ${rows.map((r) => `<tr><td>${h(r.alias)}${r.error ? `<div class="q-dn q-sm">${h(r.error)}</div>` : r.note ? `<div class="q-note q-sm">${h(r.note)}</div>` : ""}</td><td class="q-num">${h(r.login)}</td>
        <td class="${r.link === "online" ? "q-up" : r.link === "error" ? "q-dn" : "q-note"}">${r.link === "online" ? "在线" : r.link === "error" ? "出错" : r.link === "connecting" ? "连接中" : "离线"}</td>
        <td class="q-num ${cls(r.floating)}">${r.link === "online" ? sg(r.floating) : "—"}</td><td class="q-num">${r.longLots ? nf(r.longLots) : "—"}</td><td class="q-num">${r.shortLots ? nf(r.shortLots) : "—"}</td>
        <td class="q-num ${cls(r.symProfit)}">${r.longLots || r.shortLots ? sg(r.symProfit) : "—"}</td></tr>`).join("")}
      </tbody></table></div>`);
  }

  // ---------------- 操作 ----------------
  function matchPos(mode, onlySym) {
    const rows = new Map(((Q.sum && Q.sum.accounts) || []).map((r) => [r.id, r]));
    const sym = symbol();
    const inSym = (a, p) => { const r = rows.get(a.id); return p.symbol === sym || (r && r.symbol && p.symbol === r.symbol) || (p.symbol.startsWith(sym) && p.symbol.length - sym.length <= 2); };
    const hit = (p) => mode === "long" ? p.side === "buy" : mode === "short" ? p.side === "sell" : mode === "profit" ? p.profit > 0 : mode === "loss" ? p.profit < 0 : true;
    const out = [];
    selAccs().filter((a) => a.link === "online").forEach((a) => (a.positions || []).forEach((p) => { if (hit(p) && (!onlySym || inSym(a, p))) out.push(p); }));
    return out;
  }
  async function send(action, body) {
    Q.busy = action; renderAll();
    const t0 = Date.now();
    try {
      const r = await api("/api/quick/action", { action, ids: ids(), symbol: symbol(), ...body });
      Q.res = { ...r, at: Date.now(), ms: r.ms || Date.now() - t0 };
      const bad = r.results.filter((x) => !x.ok);
      bad.length ? toast(`${r.title}：成功 ${r.ok}，失败 ${r.fail}。${bad[0].alias}：${bad[0].message}`, true) : toast(`${r.title}：成功 ${r.ok}`);
    } catch (e) { toast(e.message, true); }
    finally { Q.busy = ""; renderAll(); if (typeof poll === "function") poll(); refresh(); }
  }
  async function onAction(action) {
    if (Q.busy) return toast("上一个快捷操作还在执行", true);
    const sel = selAccs(); if (!sel.length) return toast("先选择账户", true);
    const sym = symbol(), on = sel.filter((a) => a.enabled && a.link === "online");
    const noConfirm = !!(Q.st && Q.st.settings && Q.st.settings.quick_no_confirm);
    const off = sel.length - on.length, offTxt = off ? `<div class="small muted">${off} 个未登录/停用账户会记为失败。</div>` : "";
    if (["buy", "sell", "pair"].includes(action)) {
      const lots = Number(byId("qLots").value), mode = byId("qMode").value, ref = Number(byId("qRef").value);
      if (!/^[A-Za-z0-9._#&+\-]{1,32}$/.test(sym)) return toast("请填写品种", true);
      if (!(lots > 0)) return toast("开单手数需大于 0", true);
      if (mode === "equity" && !(ref > 0)) return toast("按净值比例需要填写基准净值", true);
      localStorage.setItem(LS.lots, String(lots));
      const body = { lots, scale_mode: mode, ref_equity: ref };
      if (noConfirm) return send(action, body);
      let pv = null;
      try { pv = await api("/api/trade/preview", { ids: ids(), order: { symbol: sym, lots, scale_mode: mode, ref_equity: ref, scale_groups: [] } }); } catch (e) { return toast(e.message, true); }
      const k = action === "pair" ? 2 : 1, total = Math.round(pv.total * k * 100) / 100;
      const per = mode === "fixed" ? `每户 <b>${lots}</b> 手` : `${MODE[mode]}：` + pv.rows.filter((r) => r.online).slice(0, 12).map((r) => `${h(r.alias)} ${r.lots}`).join("、") + (pv.rows.length > 12 ? " …" : "");
      const warn = pv.rows.filter((r) => r.warn && r.online).map((r) => `${h(r.alias)}：${h(r.warn)}`).slice(0, 4).join("；");
      const blocked = pv.max_total_lots && total > pv.max_total_lots;
      return confirmBox({
        title: `${ACT[action]} ${sym}`,
        desc: `<div><b>${on.length}</b> 个在线账户 · ${action === "pair" ? "同时开 1 多 + 1 空" : action === "buy" ? "买入（多）" : "卖出（空）"} · ${per} · 合计约 <b>${total}</b> 手</div>${warn ? `<div class="small down-t">${warn}</div>` : ""}${blocked ? `<div class="small down-t">合计超过批量上限 ${pv.max_total_lots} 手，会被整批拦截</div>` : ""}${offTxt}`,
        label: `确认${ACT[action]}`, tone: action === "buy" ? "up" : action === "sell" ? "danger" : "primary",
        run: () => { send(action, body); },
      });
    }
    if (action === "nuke") {
      const ps = matchPos("all", false), ords = on.reduce((t, a) => t + (a.orders || []).length, 0);
      const pnl = ps.reduce((t, p) => t + p.profit, 0);
      return confirmBox({
        title: "核弹级全平",
        desc: `<div>平掉 <b>${on.length}</b> 个在线账户的<b>全部持仓（所有品种，共 ${ps.length} 笔，浮动 ${sg(pnl)}）</b>，并撤销全部挂单（${ords} 个）。</div>${offTxt}`,
        label: "确认全平", tone: "danger", run: () => { send("nuke", {}); },
      });
    }
    const mode = { close_long: "long", close_short: "short", close_profit: "profit", close_loss: "loss" }[action];
    const only = byId("qOnly").checked;
    if (only && !/^[A-Za-z0-9._#&+\-]{1,32}$/.test(sym)) return toast("请填写品种，或取消「仅当前品种」", true);
    const ps = matchPos(mode, only);
    if (!ps.length) return toast(`所选在线账户里没有符合条件的持仓（${only ? sym : "全部品种"}）`, true);
    const body = { only_symbol: only };
    if (noConfirm) return send(action, body);
    const pnl = ps.reduce((t, p) => t + p.profit, 0);
    confirmBox({
      title: `${ACT[action]}${only ? ` ${sym}` : "（全部品种）"}`,
      desc: `<div><b>${on.length}</b> 个在线账户 · 平掉 <b>${ps.length}</b> 笔 · ${ps.reduce((t, p) => t + p.volume, 0).toFixed(2)} 手 · 当前浮动 ${sg(pnl)}</div>${offTxt}`,
      label: "确认平仓", tone: "danger", run: () => { send(action, body); },
    });
  }
  async function onArm() {
    const s = Q.sum;
    if (s && s.auto) {
      try { await api("/api/quick/auto", { amount: 0 }); toast("已取消自动全平"); } catch (e) { toast(e.message, true); }
      return refresh();
    }
    const amt = Math.round(Number(byId("qAuto").value) * 100) / 100;
    if (!isFinite(amt)) return toast("金额需为数字", true);
    if (!amt) return toast("填一个金额：正数 = 合计盈利到这个数自动全平（止盈），负数 = 亏到这个数自动全平（止损）", true);
    const sel = selAccs(); if (!sel.length) return toast("先选择账户", true);
    const cur = sel.reduce((t, a) => t + (a.link === "online" ? Number(a.floating) || 0 : 0), 0);
    confirmBox({
      title: `启用自动全平（${amt > 0 ? "止盈" : "止损"}）`,
      desc: `<div>当这 <b>${sel.length}</b> 个账户的<b>合计浮动盈亏 ${amt > 0 ? "≥" : "≤"} ${sg(amt)}</b> 时，自动核弹级全平（所有品种的持仓 + 撤销挂单）。</div><div class="small muted">当前合计 ${sg(cur)}。由后台监控（关掉网页也有效，重启程序后继续监控），只触发一次，触发后自动取消并记入日志。</div>`,
      label: "启用", tone: "danger",
      run: async () => {
        try { await api("/api/quick/auto", { amount: amt, ids: ids() }); toast("自动全平已启用"); refresh(); }
        catch (e) { toast(e.message, true); return true; }
      },
    });
  }

  window.Quick = {
    onState(st) {
      Q.st = st; build();
      renderPick();
      if (!Q.sum) renderAll(); else renderSide();
    },
    start() { if (Q.active) return; Q.active = true; refresh(); },
    stop() { Q.active = false; clearTimeout(Q.timer); Q.popOpen = false; },
    ids, selAccs,
  };
  Q.render = renderAll;
  document.addEventListener("visibilitychange", () => { if (!document.hidden && Q.active) refresh(); });
})();
