/* 交易页「预订单」：独立「选择账户」多选批量挂单；品种/手数跟快捷面板；到价挂多/挂空（自动限价或止损）+ SL/TP。 */
"use strict";
(function () {
  const byId = (id) => document.getElementById(id);
  const h = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const nf = (n, d = 2) => (Number(n) || 0).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  const sg = (n, d = 2) => (n > 0 ? "+" : n < 0 ? "-" : "") + nf(Math.abs(n), d);
  const cls = (n) => (n > 0 ? "q-up" : n < 0 ? "q-dn" : "");
  const ORDER_TYPE = ["买", "卖", "买入限价", "卖出限价", "买入止损", "卖出止损", "买入限价止损", "卖出限价止损"];
  const KIND_LABEL = { buy_limit: "买入限价", sell_limit: "卖出限价", buy_stop: "买入止损", sell_stop: "卖出止损" };
  const MODE = { fixed: "固定手数", multiplier: "按账户倍数", equity: "按净值比例" };
  const LS = { accs: "fleet.pendAccs", price: "fleet.pendPrice", sl: "fleet.pendSL", tp: "fleet.pendTP", unit: "fleet.pendUnit" };

  const P = { st: null, sel: new Set(), popOpen: false, openedAt: 0, busy: false, built: false, syncedFromQuick: false };
  window.__pending = P;
  try {
    const v = JSON.parse(localStorage.getItem(LS.accs) || "null");
    if (Array.isArray(v)) { P.sel = new Set(v.map(String)); P.syncedFromQuick = true; }
  } catch (e) { P.sel = new Set(); }

  function FT() { return window.FleetTrade || {}; }
  function toast(msg, bad) { const f = FT().toast; if (f) f(msg, bad); else console[bad ? "error" : "log"](msg); }
  function api(path, body) { return FT().api(path, body); }
  function confirmBox(opts) { return FT().confirmBox(opts); }
  function runBatch(title, path, body) { return FT().runBatch(title, path, body); }

  const accs = () => (P.st && P.st.accounts) || [];
  function ids() { const all = new Set(accs().map((a) => a.id)); return [...P.sel].filter((i) => all.has(i)); }
  function selAccs() { const s = new Set(ids()); return accs().filter((a) => s.has(a.id)); }
  function setSel(list) {
    P.sel = new Set(list);
    P.syncedFromQuick = true;
    try { localStorage.setItem(LS.accs, JSON.stringify([...P.sel])); } catch (e) { /* */ }
    renderPick(); setNote(); renderList();
  }
  function maybeSyncFromQuick() {
    if (P.syncedFromQuick) return;
    if (!(window.Quick && Quick.ids)) return;
    const q = Quick.ids();
    if (!q.length) return;
    P.sel = new Set(q);
    P.syncedFromQuick = true;
    try { localStorage.setItem(LS.accs, JSON.stringify([...P.sel])); } catch (e) { /* */ }
  }
  function symbol() { return ((byId("qSym") && byId("qSym").value) || "").trim(); }
  function lotsCfg() {
    const lots = Number(byId("qLots") && byId("qLots").value);
    const mode = (byId("qMode") && byId("qMode").value) || "fixed";
    const ref = Number(byId("qRef") && byId("qRef").value) || 10000;
    return { lots, mode, ref };
  }
  function quote() {
    const s = window.__quick && window.__quick.sum;
    return (s && s.quote && !s.quote.error) ? s.quote : null;
  }

  function pickLongKind(price, q) {
    if (!q || !(Number(q.ask) > 0)) return { kind: "buy_limit", warn: true };
    return { kind: Number(price) <= Number(q.ask) ? "buy_limit" : "buy_stop", warn: false };
  }
  function pickShortKind(price, q) {
    if (!q || !(Number(q.bid) > 0)) return { kind: "sell_limit", warn: true };
    return { kind: Number(price) >= Number(q.bid) ? "sell_limit" : "sell_stop", warn: false };
  }

  function pickLabel() {
    const n = accs().length, s = selAccs();
    if (!s.length) return "选择账户：未选 ▾";
    if (s.length === n) return `选择账户：全部（${n}）▾`;
    return `选择账户：${s.length === 1 ? s[0].alias || s[0].login : `${s.length} / ${n}`} ▾`;
  }
  function renderPick() {
    const btn = byId("pPick"); if (!btn) return;
    const s = selAccs();
    btn.textContent = pickLabel();
    btn.title = s.length ? "预订单作用于：" + s.map((a) => `${a.alias}（${a.login}）`).join("、") : "还没有选择账户";
    btn.classList.toggle("on", s.length > 0);
    btn.setAttribute("aria-expanded", P.popOpen ? "true" : "false");
    const pop = byId("pPop"); if (!pop) return;
    pop.classList.toggle("hidden", !P.popOpen);
    if (!P.popOpen) { syncSheet(); return; }
    const list = accs(), sel = new Set(ids());
    const groups = (P.st && P.st.groups && P.st.groups.length) ? P.st.groups.slice() : [...new Set(list.map((a) => a.group || "未分组"))];
    const key = JSON.stringify([groups, list.map((a) => [a.id, a.alias, a.login, a.group, a.link])]);
    const fl = (a) => `<span class="fl ${cls(a.floating)}" data-f="${h(a.id)}">${a.link === "online" ? sg(a.floating) : "—"}</span>`;
    if (pop._k === key) {
      pop.querySelectorAll("input[data-pid]").forEach((cb) => { const v = sel.has(cb.dataset.pid); if (cb.checked !== v) cb.checked = v; });
      list.forEach((a) => {
        const el = pop.querySelector(`.fl[data-f="${CSS.escape(a.id)}"]`);
        if (el) {
          const t = a.link === "online" ? sg(a.floating) : "—";
          if (el.textContent !== t) el.textContent = t;
          el.className = "fl " + cls(a.floating); el.dataset.f = a.id;
        }
      });
      const foot = pop.querySelector(".pp-foot span");
      if (foot && !foot._hint) foot.textContent = `已选 ${sel.size} / ${list.length} · 自动保存`;
      if (foot) foot._hint = false;
      syncSheet();
      return;
    }
    pop._k = key;
    const top = pop.querySelector(".pp-list") ? pop.querySelector(".pp-list").scrollTop : 0;
    pop.innerHTML = !list.length ? `<div class="pp-lab">还没有账户 · 先在账号页添加并登录</div>` : `
      <div class="pp-row"><button type="button" data-p="all">全选</button><button type="button" data-p="inv">反选</button><button type="button" data-p="online">只看在线</button><button type="button" data-p="fromq">同步快捷</button><button type="button" data-p="none">清空</button></div>
      ${groups.length > 1 ? `<div class="pp-lab">按分组</div><div class="pp-row">${groups.map((g) => `<button type="button" data-p="g" data-g="${h(g)}">${h(g)}</button>`).join("")}</div>` : ""}
      <div class="pp-list">${list.map((a) => `<label><input type="checkbox" data-pid="${h(a.id)}"${sel.has(a.id) ? " checked" : ""}><span class="nm">${h(a.alias || a.login)}</span><span class="lg">${h(a.login)}</span><span class="st${a.link === "online" ? " on" : ""}">${a.link === "online" ? "在线" : a.link === "error" ? "出错" : "离线"}</span>${fl(a)}</label>`).join("")}</div>
      <div class="pp-foot"><span>已选 ${sel.size} / ${list.length} · 自动保存</span><button type="button" data-p="close">完成</button></div>`;
    const l2 = pop.querySelector(".pp-list"); if (l2) l2.scrollTop = top;
    syncSheet();
  }
  function syncSheet() {
    const pop = byId("pPop"); if (!pop) return;
    const FS = window.FleetMobileSheet;
    if (P.popOpen && FS && FS.isMobile()) {
      FS.open(pop, { onClose: () => { P.popOpen = false; renderPick(); } });
    } else if (FS) {
      FS.close(pop);
    }
  }
  function pickHint(msg) {
    const f = document.querySelector("#pPop .pp-foot span");
    if (f) { f.textContent = msg; f._hint = true; f.style.color = "#ff6b6b"; setTimeout(() => { f.style.color = ""; }, 2500); }
  }
  function initPick() {
    const btn = byId("pPick"), pop = byId("pPop");
    if (!btn || !pop || btn._pendBound) return;
    btn._pendBound = true;
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      P.popOpen = !P.popOpen;
      if (P.popOpen) P.openedAt = Date.now();
      pop._k = "";
      renderPick();
    });
    pop.addEventListener("click", (e) => {
      e.stopPropagation();
      const b = e.target.closest("button[data-p]"); if (!b) return;
      const list = accs(), cur = new Set(ids()), act = b.dataset.p;
      if (act === "close") { P.popOpen = false; return renderPick(); }
      let next = null;
      if (act === "all") next = list.map((a) => a.id);
      else if (act === "none") next = [];
      else if (act === "inv") next = list.filter((a) => !cur.has(a.id)).map((a) => a.id);
      else if (act === "online") next = list.filter((a) => a.link === "online").map((a) => a.id);
      else if (act === "fromq") next = (window.Quick && Quick.ids) ? Quick.ids() : [];
      else if (act === "g") next = list.filter((a) => (a.group || "未分组") === b.dataset.g).map((a) => a.id);
      if (next === null) return;
      if (act === "online" && !next.length) return pickHint("现在没有在线账户");
      if (act === "fromq" && !next.length) return pickHint("快捷面板还没选账户");
      setSel(next);
    });
    pop.addEventListener("change", (e) => {
      const cb = e.target.closest("input[data-pid]"); if (!cb) return;
      const cur = new Set(ids());
      if (cb.checked) cur.add(cb.dataset.pid); else cur.delete(cb.dataset.pid);
      setSel(accs().map((a) => a.id).filter((i) => cur.has(i)));
    });
    document.addEventListener("click", (e) => {
      if (!P.popOpen) return;
      if (Date.now() - (P.openedAt || 0) < 350) return;
      if (window.FleetMobileSheet && FleetMobileSheet.isMobile()) return;
      if (e.target.closest("#pPickWrap") || e.target.closest("#pPop")) return;
      P.popOpen = false; renderPick();
    });
    document.addEventListener("keydown", (e) => {
      if (P.popOpen && e.key === "Escape") { P.popOpen = false; renderPick(); btn.focus(); }
    });
  }

  function build() {
    if (P.built || !byId("pendPanel")) return;
    P.built = true;
    const price = byId("pPrice"), sl = byId("pSL"), tp = byId("pTP"), unit = byId("pUnit");
    if (price && localStorage.getItem(LS.price)) price.value = localStorage.getItem(LS.price);
    if (sl && localStorage.getItem(LS.sl) != null) sl.value = localStorage.getItem(LS.sl);
    if (tp && localStorage.getItem(LS.tp) != null) tp.value = localStorage.getItem(LS.tp);
    if (unit) unit.value = localStorage.getItem(LS.unit) === "points" ? "points" : "price";
    [["pPrice", LS.price], ["pSL", LS.sl], ["pTP", LS.tp]].forEach(([id, k]) => {
      const el = byId(id); if (!el) return;
      el.addEventListener("change", () => { try { localStorage.setItem(k, el.value); } catch (e) { /* */ } });
    });
    if (unit) unit.addEventListener("change", () => { try { localStorage.setItem(LS.unit, unit.value); } catch (e) { /* */ } });
    const longB = byId("pHangLong"), shortB = byId("pHangShort"), cancelB = byId("pCancelSym");
    if (longB) longB.addEventListener("click", () => hang("long"));
    if (shortB) shortB.addEventListener("click", () => hang("short"));
    if (cancelB) cancelB.addEventListener("click", () => cancelSym());
    initPick();
  }

  function setNote() {
    const el = byId("pNote"); if (!el) return;
    const sym = symbol() || "—";
    const { lots, mode } = lotsCfg();
    const q = quote();
    const d = (q && q.digits) || 3;
    const qx = q ? `${Number(q.bid).toFixed(d)} / ${Number(q.ask).toFixed(d)}` : "暂无报价";
    const n = selAccs().length;
    el.textContent = `已选 ${n} 户 · 品种 ${sym} · 手数 ${lots || "—"}（${MODE[mode] || mode}）· 报价 ${qx}`;
  }

  function saveTwScroll(root) {
    const tw = root && root.querySelector && root.querySelector(".q-tw");
    return tw ? { left: tw.scrollLeft, top: tw.scrollTop } : null;
  }
  function restoreTwScroll(root, s) {
    if (!s) return;
    const tw = root && root.querySelector && root.querySelector(".q-tw");
    if (!tw) return;
    tw.scrollLeft = s.left; tw.scrollTop = s.top;
  }

  function renderList() {
    const el = byId("pendList"); if (!el) return;
    const list = selAccs();
    const sym = symbol();
    const rows = [];
    list.forEach((a) => {
      (a.orders || []).forEach((o) => {
        if (sym) {
          const ok = o.symbol === sym || String(o.symbol || "").startsWith(sym);
          if (!ok) return;
        }
        rows.push({ a, o });
      });
    });
    if (!list.length) {
      el._k = "";
      el.innerHTML = `<p class="small muted" style="margin:0">先点「选择账户」勾选要批量挂单的账户。</p>`;
      return;
    }
    if (!rows.length) {
      el._k = "";
      el.innerHTML = `<p class="small muted" style="margin:0">所选账户${sym ? `在 ${h(sym)} ` : ""}暂无挂单。</p>`;
      return;
    }
    const structKey = rows.map(({ a, o }) => [a.id, o.ticket, o.type, o.volume, o.price, o.sl, o.tp, o.symbol].join(":")).join(";");
    if (el._k === structKey && el.querySelector("tbody")) return;
    const kept = saveTwScroll(el);
    el._k = structKey;
    el.innerHTML = `<div class="q-tw pend-tw"><table class="q-pos-t"><thead><tr>
      <th>账户</th><th>单号</th><th>品种</th><th>类型</th><th>手数</th><th>价格</th><th>SL</th><th>TP</th>
    </tr></thead><tbody>
    ${rows.map(({ a, o }) => `<tr>
      <td>${h(a.alias)}</td><td class="q-num">${o.ticket}</td><td>${h(o.symbol)}</td>
      <td>${h(ORDER_TYPE[o.type] || o.type)}</td><td class="q-num">${o.volume}</td>
      <td class="q-num">${o.price}</td><td class="q-num">${o.sl || "—"}</td><td class="q-num">${o.tp || "—"}</td>
    </tr>`).join("")}
    </tbody></table></div>`;
    restoreTwScroll(el, kept);
  }

  function setBusy(on) {
    P.busy = !!on;
    ["pHangLong", "pHangShort", "pCancelSym"].forEach((id) => {
      const b = byId(id); if (b) b.disabled = !!on;
    });
  }

  async function hang(side) {
    if (P.busy) return toast("上一个预订单操作还在执行", true);
    if (!FT().api) return toast("交易接口未就绪，请刷新页面", true);
    if (P.popOpen) { P.popOpen = false; renderPick(); }
    const sel = selAccs();
    if (!sel.length) return toast("先选择账户", true);
    const on = sel.filter((a) => a.enabled && a.link === "online");
    if (!on.length) return toast("所选账户都不在线", true);
    const sym = symbol();
    if (!/^[A-Za-z0-9._#&+\-]{1,32}$/.test(sym)) return toast("请填写品种（快捷面板）", true);
    const { lots, mode, ref } = lotsCfg();
    if (!(lots > 0)) return toast("开单手数需大于 0（快捷面板）", true);
    if (mode === "equity" && !(ref > 0)) return toast("按净值比例需要填写基准净值", true);
    const price = Number(byId("pPrice") && byId("pPrice").value);
    if (!(price > 0)) return toast("请填写挂单价", true);
    const sl = Number(byId("pSL") && byId("pSL").value) || 0;
    const tp = Number(byId("pTP") && byId("pTP").value) || 0;
    const sltp_mode = (byId("pUnit") && byId("pUnit").value) === "points" ? "points" : "price";
    const q = quote();
    const pick = side === "long" ? pickLongKind(price, q) : pickShortKind(price, q);
    const kind = pick.kind;
    const order = {
      symbol: sym, kind, lots, price,
      scale_mode: mode, ref_equity: ref, scale_groups: [],
      sltp_mode, sl, tp, magic: 880000, comment: "fleet pending",
    };
    const idList = ids();
    let pv;
    try { pv = await api("/api/trade/preview", { ids: idList, order }); }
    catch (e) { return toast(e.message, true); }
    const kindLab = KIND_LABEL[kind] || kind;
    const sideLab = side === "long" ? "挂多（到价买入）" : "挂空（到价卖出）";
    const unitLab = sltp_mode === "points" ? "点数" : "价格";
    const sltp = (sl || tp) ? `，SL ${sl} / TP ${tp}（${unitLab}）` : "";
    const qWarn = pick.warn
      ? `<div class="small down-t">当前无可用报价，已按${kindLab}挂单；到价逻辑请自行确认。</div>`
      : (q ? `<div class="small muted">当前报价 买 ${Number(q.bid)} / 卖 ${Number(q.ask)} → 自动选 ${kindLab}</div>` : "");
    const table = `<div class="tablewrap"><table><thead><tr><th>账户</th><th>分组</th><th>净值</th><th>系数</th><th>手数</th><th>备注</th></tr></thead><tbody>
      ${(pv.rows || []).map((r) => `<tr><td>${h(r.alias)}</td><td>${h(r.group)}</td><td class="num">${nf(r.equity)}</td><td class="num">${r.factor}</td><td class="num">${r.lots}</td><td class="small ${r.warn ? "down-t" : "muted"}">${h(r.warn || "手数会按品种步长向下取整")}</td></tr>`).join("")}
      </tbody></table></div><p class="small muted" style="margin:0">合计约 <b class="num">${pv.total}</b> 手</p>`;
    const off = sel.length - on.length;
    const offTxt = off ? `<div class="small muted">${off} 个未登录/停用账户会记为失败。</div>` : "";
    confirmBox({
      title: `${sideLab} ${sym}`,
      wide: true,
      desc: `<div>将对 <b>${(pv.rows || []).filter((r) => r.online).length}</b> 个在线账户${kindLab} <b>${h(sym)}</b> @ <b>${price}</b>${sltp}，魔术号 880000，备注 fleet pending。</div>${qWarn}${offTxt}`,
      body: table,
      label: `确认${sideLab}`,
      tone: side === "long" ? "up" : "danger",
      run: () => {
        setBusy(true);
        try { runBatch(sideLab, "/api/trade/order", { ids: idList, order }); }
        finally { setTimeout(() => setBusy(false), 800); }
      },
    });
  }

  function cancelSym() {
    if (P.busy) return toast("上一个预订单操作还在执行", true);
    if (!FT().api) return toast("交易接口未就绪，请刷新页面", true);
    if (P.popOpen) { P.popOpen = false; renderPick(); }
    const sel = selAccs();
    if (!sel.length) return toast("先选择账户", true);
    const sym = symbol();
    if (!/^[A-Za-z0-9._#&+\-]{1,32}$/.test(sym)) return toast("请填写品种", true);
    const on = sel.filter((a) => a.enabled && a.link === "online");
    const n = on.reduce((t, a) => t + (a.orders || []).filter((o) => o.symbol === sym || String(o.symbol || "").startsWith(sym)).length, 0);
    if (!n) return toast(`所选在线账户在 ${sym} 没有挂单`, true);
    confirmBox({
      title: `撤销本品种挂单 ${sym}`,
      desc: `将撤销 <b>${on.length}</b> 个在线账户在 <b>${h(sym)}</b> 上的挂单（约 ${n} 个）。其它品种挂单不动。`,
      label: "确认撤单",
      tone: "danger",
      run: () => {
        setBusy(true);
        try { runBatch("撤本品种挂单", "/api/trade/cancel", { ids: ids(), symbol: sym }); }
        finally { setTimeout(() => setBusy(false), 800); }
      },
    });
  }

  function onState(st) {
    if (st) P.st = st;
    maybeSyncFromQuick();
    build();
    renderPick();
    setNote();
    renderList();
  }

  function hookQuick() {
    if (!window.Quick || Quick._pendHooked) return;
    const prev = Quick.onState.bind(Quick);
    Quick.onState = function (st) { prev(st); onState(st); };
    Quick._pendHooked = true;
  }

  window.FleetPending = {
    onState,
    pickLongKind,
    pickShortKind,
    hang,
    cancelSym,
    ids,
    selAccs,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => { hookQuick(); build(); onState(P.st); });
  } else {
    hookQuick();
    build();
    onState(P.st);
  }
})();
