/* 交易页「算法交易开关」：在自选账户的 MT5 里一键打开 / 关闭工具栏的「算法交易」按钮。
   状态来自 /api/state 里每个账户的 terminal.trade_allowed（终端自己报告的，约 1.5 秒刷新一次）。
   只作用于本程序启动的终端；桌面上的 MT5 不碰。电脑和手机远程页面用的是同一个接口。 */
"use strict";
(function () {
  const byId = (id) => document.getElementById(id);
  const h = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pad = (n) => String(n).padStart(2, "0");
  const hms = (ms) => { const off = ((window.__fleetClock && window.__fleetClock.offset) || 0) * 1000; const d = new Date((Number(ms) || Date.now()) + off); return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`; };
  const LS = "fleet.algoAccs";
  const A = { st: null, sel: new Set(), popOpen: false, built: false, busy: "", res: null };
  window.__algo = A;
  try { const v = JSON.parse(localStorage.getItem(LS) || "[]"); if (Array.isArray(v)) A.sel = new Set(v.map(String)); } catch (e) { A.sel = new Set(); }

  const accs = () => (A.st && A.st.accounts) || [];
  function ids() { const all = new Set(accs().map((a) => a.id)); return [...A.sel].filter((i) => all.has(i)); }
  function selAccs() { const s = new Set(ids()); return accs().filter((a) => s.has(a.id)); }
  function setSel(list) {
    A.sel = new Set(list);
    try { localStorage.setItem(LS, JSON.stringify([...A.sel])); } catch (e) { /* 隐私模式 */ }
    renderPick(); render();
  }
  function set(id, html) { const el = byId(id); if (el && el._h !== html) { el._h = html; el.innerHTML = html; } }
  const online = (a) => a.link === "online" && a.enabled !== false;
  function algoOf(a) {   // true / false / null（不在线，读不到）
    if (!online(a) || !a.terminal) return null;
    return !!a.terminal.trade_allowed;
  }

  // ---------------- 选择账户（和快捷交易面板同款） ----------------
  function pickLabel() {
    const n = accs().length, s = selAccs();
    if (!s.length) return "选择账户：未选 ▾";
    if (s.length === n) return `选择账户：全部（${n}）▾`;
    return `选择账户：${s.length === 1 ? s[0].alias || s[0].login : `${s.length} / ${n}`} ▾`;
  }
  function renderPick() {
    const btn = byId("alPick"); if (!btn) return;
    const s = selAccs();
    btn.textContent = pickLabel();
    btn.classList.toggle("on", s.length > 0);
    btn.setAttribute("aria-expanded", A.popOpen ? "true" : "false");
    const pop = byId("alPop");
    pop.classList.toggle("hidden", !A.popOpen);
    if (!A.popOpen) return;
    const list = accs(), sel = new Set(ids());
    const groups = (A.st && A.st.groups && A.st.groups.length) ? A.st.groups.slice() : [...new Set(list.map((a) => a.group || "未分组"))];
    const stTxt = (a) => { const v = algoOf(a); return v === null ? (a.link === "error" ? "出错" : "离线") : v ? "算法 开" : "算法 关"; };
    const key = JSON.stringify([groups, list.map((a) => [a.id, a.alias, a.login, a.group, a.link, algoOf(a)])]);
    if (pop._k === key) {
      pop.querySelectorAll("input[data-alid]").forEach((cb) => { const v = sel.has(cb.dataset.alid); if (cb.checked !== v) cb.checked = v; });
      const foot = pop.querySelector(".pp-foot span"); if (foot && !foot._hint) foot.textContent = `已选 ${sel.size} / ${list.length} · 自动保存`;
      if (foot) foot._hint = false;
      return;
    }
    pop._k = key;
    const top = pop.querySelector(".pp-list") ? pop.querySelector(".pp-list").scrollTop : 0;
    pop.innerHTML = !list.length ? `<div class="pp-lab">还没有账户 · 先在账号页添加并登录</div>` : `
      <div class="pp-row"><button type="button" data-al="all">全选</button><button type="button" data-al="inv">反选</button><button type="button" data-al="online">只看在线</button><button type="button" data-al="none">清空</button></div>
      ${groups.length > 1 ? `<div class="pp-lab">按分组</div><div class="pp-row">${groups.map((g) => `<button type="button" data-al="g" data-g="${h(g)}">${h(g)}</button>`).join("")}</div>` : ""}
      <div class="pp-list">${list.map((a) => `<label><input type="checkbox" data-alid="${h(a.id)}"${sel.has(a.id) ? " checked" : ""}><span class="nm">${h(a.alias || a.login)}</span><span class="lg">${h(a.login)}</span><span class="st${algoOf(a) ? " on" : ""}">${stTxt(a)}</span></label>`).join("")}</div>
      <div class="pp-foot"><span>已选 ${sel.size} / ${list.length} · 自动保存</span><button type="button" data-al="close">完成</button></div>`;
    const l2 = pop.querySelector(".pp-list"); if (l2) l2.scrollTop = top;
  }
  function initPick() {
    const btn = byId("alPick"), pop = byId("alPop");
    btn.addEventListener("click", (e) => { e.stopPropagation(); A.popOpen = !A.popOpen; pop._k = ""; renderPick(); });
    pop.addEventListener("click", (e) => {
      e.stopPropagation();
      const b = e.target.closest("button[data-al]"); if (!b) return;
      const list = accs(), cur = new Set(ids()), act = b.dataset.al;
      if (act === "close") { A.popOpen = false; return renderPick(); }
      let next = null;
      if (act === "all") next = list.map((a) => a.id);
      else if (act === "none") next = [];
      else if (act === "inv") next = list.filter((a) => !cur.has(a.id)).map((a) => a.id);
      else if (act === "online") next = list.filter(online).map((a) => a.id);
      else if (act === "g") next = list.filter((a) => (a.group || "未分组") === b.dataset.g).map((a) => a.id);
      if (next === null) return;
      setSel(next);
    });
    pop.addEventListener("change", (e) => {
      const cb = e.target.closest("input[data-alid]"); if (!cb) return;
      const cur = new Set(ids());
      if (cb.checked) cur.add(cb.dataset.alid); else cur.delete(cb.dataset.alid);
      setSel(accs().map((a) => a.id).filter((i) => cur.has(i)));
    });
    document.addEventListener("click", (e) => { if (A.popOpen && !e.target.closest("#alPickWrap")) { A.popOpen = false; renderPick(); } });
    document.addEventListener("keydown", (e) => { if (A.popOpen && e.key === "Escape") { A.popOpen = false; renderPick(); btn.focus(); } });
  }

  // ---------------- 显示 ----------------
  function render() {
    if (!A.built) return;
    const sel = selAccs(), mock = !!(A.st && A.st.mock);
    const on = sel.filter((a) => algoOf(a) === true).length, off = sel.filter((a) => algoOf(a) === false).length;
    const live = sel.filter(online).length;
    byId("alOn").disabled = !!A.busy || !live;
    byId("alOff").disabled = !!A.busy || !live;
    byId("alOn").textContent = A.busy === "on" ? "正在开启…" : "一键开启";
    byId("alOff").textContent = A.busy === "off" ? "正在关闭…" : "一键关闭";
    set("alSum", sel.length ? `已选 ${sel.length} 个 · 在线 ${live} · <span class="up-t">开 ${on}</span> · <span class="down-t">关 ${off}</span>` : "先点「选择账户」勾选要开关的账户（可全选）");
    const r = A.res;
    set("alRes", !r ? "" : `<div class="al-rh"><b>${h(r.title)}</b>：<span class="up-t">成功 ${r.ok}</span> / <span class="${r.fail ? "down-t" : ""}">失败 ${r.fail}</span> <span class="muted small">· ${hms(r.at)}</span> <button type="button" class="al-x" data-alx="1" aria-label="关闭结果">×</button></div>
      <div class="al-rl">${r.results.filter((x) => !x.ok).map((x) => `<div class="down-t">✗ 失败 · ${h(x.alias)}：${h(x.message)}</div>`).join("")}${r.results.filter((x) => x.ok).map((x) => `<div class="up-t">✓ 成功 · ${h(x.alias)}：${h(x.message)}</div>`).join("")}</div>`);
    if (!sel.length) return set("alList", "");
    set("alList", `<div class="al-tw"><table class="al-t"><thead><tr><th>账户</th><th>账号</th><th>连接</th><th>算法交易</th><th>说明</th></tr></thead><tbody>
      ${sel.map((a) => {
        const v = algoOf(a);
        const tag = v === null ? `<span class="tag">—</span>` : v ? `<span class="tag ok">开</span>` : `<span class="tag warn">关</span>`;
        const link = a.link === "online" ? `<span class="up-t">在线</span>` : a.link === "error" ? `<span class="down-t">出错</span>` : a.link === "connecting" ? "连接中" : `<span class="muted">离线</span>`;
        const notes = [];
        if (a.algo_off) notes.push("你已手动关闭，后台重连不会自动打开");
        if (online(a) && !a.ownedTerminal && !mock) notes.push("不是本程序启动的终端，不会去点它");
        if (a.enabled === false) notes.push("已停用");
        return `<tr><td>${h(a.alias || a.login)}</td><td class="num">${h(a.login)}</td><td>${link}</td><td>${tag}</td><td class="small muted">${h(notes.join("；"))}</td></tr>`;
      }).join("")}</tbody></table></div>`);
  }

  // ---------------- 操作 ----------------
  function ask(on) {
    if (A.busy) return toast("上一次开关还在执行", true);
    const sel = selAccs(); if (!sel.length) return toast("先选择账户", true);
    const live = sel.filter(online), skip = sel.length - live.length;
    if (!live.length) return toast("所选账户都不在线：先在账号页登录", true);
    const names = live.slice(0, 8).map((a) => h(a.alias || a.login)).join("、") + (live.length > 8 ? ` 等 ${live.length} 个` : "");
    const desc = on
      ? `<div>在 <b>${live.length}</b> 个在线账户（${names}）的 MT5 里<b>打开「算法交易」</b>。打开后，这些终端里挂着的 EA 会开始自动下单。</div>`
      : `<div>在 <b>${live.length}</b> 个在线账户（${names}）的 MT5 里<b>关闭「算法交易」</b>。</div>
         <div style="margin-top:6px">关闭后：<b>EA 停止下单，已有持仓和挂单保留，不会平仓</b>。本程序在这些账户上的下单、平仓（包括核弹级全平、自动全平）也会被 MT5 拒绝，直到重新开启。</div>
         <div class="small muted" style="margin-top:6px">后台重连不会自动打开；只有你点「一键开启」、登录或分发时才会重新打开。</div>`;
    confirmBox({
      title: on ? "一键开启算法交易" : "一键关闭算法交易",
      desc: desc + `<div class="small muted" style="margin-top:6px">只操作本程序启动的终端，桌面上你自己的 MT5 不碰。每个账户会回读终端状态，确认真的变了才算成功。</div>` + (skip ? `<div class="small muted">${skip} 个未登录 / 停用的账户会记为失败。</div>` : ""),
      label: on ? "确认开启" : "确认关闭", tone: on ? "up" : "danger",
      run: () => { run(on); },
    });
  }
  async function run(on) {
    A.busy = on ? "on" : "off"; render();
    try {
      const r = await api("/api/algo", { ids: ids(), on });
      A.res = { ...r, at: Date.now() };
      const bad = r.results.filter((x) => !x.ok);
      bad.length ? toast(`${r.title}：成功 ${r.ok}，失败 ${r.fail}。${bad[0].alias}：${bad[0].message}`, true) : toast(`${r.title}：成功 ${r.ok}`);
    } catch (e) { toast(e.message, true); }
    finally { A.busy = ""; render(); if (typeof poll === "function") poll(); }
  }

  function build() {
    if (A.built || !byId("algoPanel")) return;
    A.built = true;
    initPick();
    byId("alOn").addEventListener("click", () => ask(true));
    byId("alOff").addEventListener("click", () => ask(false));
    byId("alRes").addEventListener("click", (e) => { if (e.target.closest("[data-alx]")) { A.res = null; render(); } });
  }

  window.Algo = {
    onState(st) { A.st = st; build(); renderPick(); render(); },
    ids,
  };
})();
