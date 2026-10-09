"""主进程：管理每个账户的工作进程、汇总状态、批量命令、操作日志。"""
from __future__ import annotations

import csv
import io
import json
import os
import multiprocessing as mp
import queue
import shutil
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import secrets
from . import terminal as T
from .config import Store, app_root, terminals_root
from .procs import Registry
from . import alerts
from .remote import RemoteGate
from . import quote as fx
from .worker import worker_main

CTX = mp.get_context("spawn")
EQ_STEP = 5            # 净值采样间隔（秒）
EQ_MAX = 13 * 3600 // EQ_STEP + 10   # 保留 13 小时
DEALS_EVERY = 60       # 平仓记录刷新间隔（秒）
# 网页上每个账户的步骤条
LOGIN_STEPS = [("copy", "创建副本"), ("launch", "启动终端"), ("login", "登录"), ("algo", "算法交易已开启")]
DIST_STEPS = [("login", "登录"), ("files", "复制 EA 文件"), ("restart", "重启终端"), ("attach", "挂载 EA"), ("verify", "校验")]
STOP_STEPS = [("restart", "关闭终端"), ("remove", "从图表移除 EA"), ("attach", "重新打开终端")]
# 交易页快捷面板：动作 → (名称, 工作进程命令)
QUICK_ACTIONS = {
    "buy": ("一键开多", "order"), "sell": ("一键开空", "order"), "pair": ("一键开单", "order_pair"),
    "close_long": ("平全部多", "close"), "close_short": ("平全部空", "close"),
    "close_profit": ("平所有盈利", "close"), "close_loss": ("平所有亏损", "close"),
    "nuke": ("核弹级全平", "nuke"),
}
QUICK_CLOSE_MODE = {"close_long": "long", "close_short": "short", "close_profit": "profit", "close_loss": "loss"}
FRESH_SEC = 15         # 自动全平只统计这么多秒内刷新过的账户


class QuickBusy(RuntimeError):
    pass


class Fleet:
    def __init__(self, store: Store, mock: bool = False):
        self.store = store
        self.mock = mock
        self.evt_q = CTX.Queue()
        self.workers: dict[str, dict] = {}
        self.runtime: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}
        self.logs: deque = deque(maxlen=500)
        self.lock = threading.RLock()
        self.busy = threading.Lock()
        self.banner = None
        self.alerts: list[dict] = []
        self.alert_armed: dict = {}
        self.alert_seq = 0
        self.alert_popup = None
        self.quote: dict | None = None
        self.remote = RemoteGate(store)
        self.clock = {"unix": 0, "offset": 0.0, "source": "", "at": 0.0}  # MT5 服务器时间（来自 tick）
        self._load_alerts()
        self.registry = Registry(store.data_dir)   # 本程序启动的终端 PID
        self.root = terminals_root()
        self.mt5_import_error = None
        self._stop = False
        self._last_persist = time.time()
        # ---- 收益台（desk）数据 ----
        self.started = time.time()
        self.events: deque = deque(maxlen=800)      # 活动流：操作日志 + 开/平仓变化
        self.event_seq = 0
        self.equity: deque = deque(maxlen=EQ_MAX)   # [ts, equity, balance, floating, online, {账户ID: [净值, 余额, 浮动]}]
        self.deals: dict[str, list] = {}            # 每账户平仓记录缓存
        self.deals_version = 0
        self.deals_at = 0.0
        self._deals_due = time.time() + 3
        # ---- 一键登录 / 分发进度 ----
        self.prog: dict | None = None
        self._tpl: dict | None = None
        self._tpl_at = 0.0
        # ---- 交易页快捷面板 ----
        self.quick_watch: list[str] = [str(store.settings.get("default_symbol") or "USDJPYc")]
        self._watch_sent: dict[str, tuple] = {}
        self.quick_last: dict | None = None
        self.qlock = threading.Lock()
        self.auto: dict | None = None          # 自动全平：{"amount", "ids", "at", ...}
        self.auto_last: dict | None = None
        self._load_auto()
        if not mock:
            try:
                n = T.wipe_stale_inis(self.root)   # 上次异常退出遗留的启动配置（可能含密码）
                if n:
                    print(f"[提示] 已删除 {n} 个遗留的启动配置文件")
            except Exception:
                pass
        self._load_today_logs()
        self._load_equity()
        if not mock:
            try:
                import MetaTrader5  # noqa: F401
            except Exception as e:
                self.mt5_import_error = f"无法导入 MetaTrader5 包（{e}）。界面可以打开，但不能登录终端。请在 Windows 64 位 Python 3.10+ 上运行「安装依赖.bat」。"
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()
        threading.Thread(target=self._desk_loop, daemon=True).start()
        threading.Thread(target=self._quote_loop, daemon=True).start()
        self.reattach_ids = self.running_terminal_accounts() if not mock else []
        if self.reattach_ids and store.settings.get("auto_reattach", True):
            threading.Thread(target=self._reattach, daemon=True).start()
        if not mock:
            try:   # 随版本发布的服务器列表（含 42 及以上）作为底线；不覆盖更全的，不动正在用的账户
                self.seed_published_servers()
            except Exception as e:
                print(f"[提示] 放入发布的服务器列表时出错：{e}")

    # ---------------- 程序重启后：接管上次本程序启动、仍在运行的终端 ----------------
    def running_terminal_accounts(self) -> list[str]:
        """launched.json 里登记过（PID + 启动时间 + exe 路径都对得上）、或由我们的启动参数重新启动的终端对应的账户。"""
        ids = []
        for a in self.store.accounts:
            path = a.get("terminal_path") or ""
            if not a.get("enabled", True) or not path:
                continue
            try:
                if self.registry.get(path) or T.adoptable_processes(path):
                    ids.append(a["id"])
            except Exception:
                continue
        return ids

    def _reattach(self):
        time.sleep(0.5)
        ids = [i for i in self.reattach_ids if not self._alive(i)]
        if not ids:
            return
        print(f"[提示] 正在重新连接上次仍在运行的 {len(ids)} 个终端（不会重启它们，EA 继续运行）")
        try:
            with self.busy:
                results = self._login_ids(ids, finish=False)
        except Exception as e:  # pragma: no cover
            print("重新连接出错", e)
            return
        for i, r in results.items():
            self.log(i, "接管终端", r.get("ok", False), ("已重新连接仍在运行的终端 · " if r.get("ok") else "") + r.get("message", ""))

    # ---------------- 日志 ----------------
    def _log_file(self) -> Path:
        return self.store.data_dir / "logs" / f"fleet-{datetime.now():%Y%m%d}.jsonl"

    def _load_today_logs(self):
        try:
            with open(self._log_file(), "r", encoding="utf-8") as f:
                for line in f.readlines()[-500:]:
                    self.logs.appendleft(json.loads(line))
        except Exception:
            pass
        for e in reversed(list(self.logs)[:40]):   # 用今天的日志预填活动流
            self._event(e.get("accountId"), "op", f"{e.get('action', '')} · {e.get('message', '')}", e.get("ok", True),
                        e.get("alias"))
            self.events[-1]["at"] = e.get("at", self.events[-1]["at"])

    def log(self, acc_id: str, action: str, ok: bool, message: str, alias: str | None = None):
        if alias is None:
            a = self.store.get(acc_id) if acc_id else None
            alias = a["alias"] if a else ("批量" if not acc_id else "—")
        e = {"id": uuid.uuid4().hex[:10], "at": int(time.time() * 1000), "accountId": acc_id, "alias": alias,
             "action": action, "ok": bool(ok), "message": message,
             "mode": "实盘"}
        with self.lock:
            self.logs.appendleft(e)
            self._event(acc_id, "op", f"{action} · {message}", ok, alias)
        try:
            with open(self._log_file(), "a", encoding="utf-8") as f:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return e

    def logs_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["time", "alias", "action", "ok", "mode", "message"])
        for e in list(self.logs):
            w.writerow([datetime.fromtimestamp(e["at"] / 1000).strftime("%Y-%m-%d %H:%M:%S"), e["alias"], e["action"],
                        "ok" if e["ok"] else "fail", e.get("mode", ""), e["message"]])
        return "\ufeff" + buf.getvalue()

    # ---------------- 事件 ----------------
    def _reader(self):
        while not self._stop:
            try:
                ev = self.evt_q.get(timeout=0.5)
            except queue.Empty:
                continue
            except (EOFError, OSError):
                break
            try:
                self._on_event(ev)
            except Exception as e:  # pragma: no cover
                print("事件处理出错", e)

    def _on_event(self, ev: dict):
        t, acc_id = ev.get("type"), ev.get("id")
        self._pending_alert = None
        with self.lock:
            rt = self.runtime.setdefault(acc_id, {"link": "offline", "linkError": "", "snapshot": None, "ts": 0})
            if t == "status":
                rt["link"], rt["linkError"] = ev["link"], ev.get("linkError", "")
                if ev.get("snapshot"):
                    self._diff_positions(acc_id, rt.get("snapshot"), ev["snapshot"])
                    rt["snapshot"], rt["ts"] = ev["snapshot"], ev.get("ts", time.time())
                    self._remember(acc_id, ev["snapshot"])
                    if rt.get("link") == "online":
                        self._pending_alert = self._apply_alerts_locked()
                        stime = int((ev["snapshot"] or {}).get("server_time") or 0)
                        if stime > 0:
                            a = self.store.get(acc_id) or {}
                            src = a.get("alias") or a.get("login") or acc_id
                            # 多账户不同服务器：同一来源持续更新；来源掉线后才换别的账户
                            cur = self.clock.get("source") or ""
                            cur_online = False
                            if cur:
                                for ac in self.store.accounts:
                                    label = ac.get("alias") or ac.get("login") or ac["id"]
                                    if label == cur and (self.runtime.get(ac["id"]) or {}).get("link") == "online":
                                        cur_online = True
                                        break
                            if not cur or src == cur or not cur_online:
                                self.clock = {"unix": stime, "offset": float(stime) - time.time(),
                                              "source": src, "at": time.time()}
            elif t == "result":
                p = self.pending.get(ev.get("req"))
                if p:
                    p["result"] = ev
                    p["event"].set()
            elif t == "launched":
                self.registry.add(ev["path"], ev["pid"], ev["create_time"])
                if not ev.get("adopted"):
                    self.log(acc_id, "启动终端", True, f"以便携模式启动 {ev['path']}（PID {ev['pid']}）")
            elif t == "closed":
                self.registry.remove(ev.get("path") or "")
                self.log(acc_id, "关闭终端", True, "已关闭本程序启动的终端")
            elif t == "log":
                self.log(acc_id, ev.get("action", ""), ev.get("ok", True), ev.get("message", ""))
            elif t == "progress":
                self._prog_step(acc_id, ev.get("key", ""), ev.get("state", ""), ev.get("msg", ""))
            elif t == "exit":
                w = self.workers.pop(acc_id, None)
                if ev.get("link") == "error":
                    rt["link"], rt["linkError"] = "error", ev.get("linkError", "")
                elif rt.get("link") != "error":
                    rt["link"], rt["linkError"] = "offline", ""
                if w:
                    threading.Thread(target=self._reap, args=(w,), daemon=True).start()
        self._flush_alerts()

    @staticmethod
    def _reap(w):
        try:
            w["proc"].join(timeout=10)
        except Exception:
            pass

    def _remember(self, acc_id, snap):
        a = self.store.get(acc_id)
        if not a:
            return
        acc = snap.get("account") or {}
        a["last"] = {"balance": acc.get("balance", 0), "equity": acc.get("equity", 0), "floating": acc.get("profit", 0),
                     "positions": len(snap.get("positions") or []), "currency": acc.get("currency", ""),
                     "ts": int(time.time())}
        if time.time() - self._last_persist > 30:
            self._last_persist = time.time()
            self.store.save_accounts()

    # ---------------- 收益台：活动流 / 净值采样 / 平仓缓存 ----------------
    def _event(self, acc_id, kind, text, ok=True, alias=None):
        if alias is None:
            a = self.store.get(acc_id) if acc_id else None
            alias = a["alias"] if a else ("批量" if not acc_id else "—")
        self.event_seq += 1
        self.events.append({"seq": self.event_seq, "at": int(time.time() * 1000), "accountId": acc_id or "",
                            "alias": alias, "kind": kind, "ok": bool(ok), "text": text[:120]})

    def _diff_positions(self, acc_id, old, new):
        if not old:   # 刚上线：尽快读取它的平仓记录
            self._deals_due = min(self._deals_due, time.time() + 1.5)
            return
        before = {p["ticket"]: p for p in old.get("positions") or []}
        after = {p["ticket"]: p for p in new.get("positions") or []}
        for t, p in after.items():
            if t not in before:
                self._event(acc_id, "open", f"开仓 {p['symbol']} {'买' if p['side'] == 'buy' else '卖'} {p['volume']:g} 手 @ {p['openPrice']:g}")
            elif abs(p["volume"] - before[t]["volume"]) > 1e-9:
                self._event(acc_id, "close", f"部分平仓 {p['symbol']} #{t} {before[t]['volume']:g} → {p['volume']:g} 手")
        closed = [p for t, p in before.items() if t not in after]
        for p in closed:
            pr = p.get("profit", 0)
            self._event(acc_id, "close", f"平仓 {p['symbol']} #{p['ticket']} {'+' if pr >= 0 else ''}{pr:.2f}", pr >= 0)
        if closed:
            self._deals_due = min(self._deals_due, time.time() + 2)

    def _eq_file(self) -> Path:
        return self.store.data_dir / "logs" / f"equity-{datetime.now():%Y%m%d}.jsonl"

    def _load_equity(self):
        cutoff = time.time() - 13 * 3600
        for day in sorted({datetime.fromtimestamp(cutoff), datetime.now()}):
            try:
                with open(self.store.data_dir / "logs" / f"equity-{day:%Y%m%d}.jsonl", "r", encoding="utf-8") as f:
                    for line in f:
                        r = json.loads(line)
                        if r[0] >= cutoff and (not self.equity or r[0] > self.equity[-1][0]):
                            self.equity.append(r)
            except Exception:
                pass

    def _sample_equity(self):
        eq = bal = fl = 0.0
        n = 0
        per: dict[str, list] = {}
        with self.lock:
            for acc_id, rt in self.runtime.items():
                snap = rt.get("snapshot")
                if rt.get("link") == "online" and snap and self._alive(acc_id):
                    a = snap.get("account") or {}
                    e, b, f = float(a.get("equity", 0) or 0), float(a.get("balance", 0) or 0), float(a.get("profit", 0) or 0)
                    eq += e
                    bal += b
                    fl += f
                    n += 1
                    per[acc_id] = [round(e, 2), round(b, 2), round(f, 2)]
        if not n:
            return
        # 第 6 项：每个账户自己的净值/余额/浮动，收益台按账户筛选时用它重新汇总
        r = [int(time.time()), round(eq, 2), round(bal, 2), round(fl, 2), n, per]
        self.equity.append(r)
        try:
            with open(self._eq_file(), "a", encoding="utf-8") as f:
                f.write(json.dumps(r) + "\n")
        except Exception:
            pass

    def _refresh_deals(self):
        with self.lock:
            ids = [i for i in self.runtime if self._alive(i) and self.runtime[i].get("link") == "online"]
        if not ids:
            self._deals_due = time.time() + 5
            return
        try:
            r = self.history(ids)
        except Exception:
            return
        fresh: dict[str, list] = {}
        for d in r.get("deals", []):
            fresh.setdefault(d["accountId"], []).append([d["time"], d["profit"], d.get("symbol", "")])
        changed = False
        for i in ids:
            rows = sorted(fresh.get(i, []))
            if rows != self.deals.get(i):
                self.deals[i] = rows
                changed = True
        if changed:
            self.deals_version += 1
        self.deals_at = time.time()

    def _desk_loop(self):
        next_eq = time.time() + 2
        while not self._stop:
            time.sleep(0.5)
            now = time.time()
            if now >= next_eq:
                next_eq = now + EQ_STEP
                try:
                    self._sample_equity()
                except Exception as e:  # pragma: no cover
                    print("净值采样出错", e)
            try:
                self._check_auto()
            except Exception as e:  # pragma: no cover
                print("自动全平检查出错", e)
            if now >= self._deals_due:
                self._deals_due = now + DEALS_EVERY
                self._refresh_deals()

    @staticmethod
    def _eq_subset(r: list, ids: set) -> list | None:
        """把一条净值采样按选中的账户重新汇总；这条采样里没有这些账户（或是旧格式没有分账户数据）返回 None。"""
        per = r[5] if len(r) > 5 and isinstance(r[5], dict) else None
        if per is None:
            return None
        rows = [v for k, v in per.items() if k in ids]
        if not rows:
            return None
        return [r[0], round(sum(x[0] for x in rows), 2), round(sum(x[1] for x in rows), 2), round(sum(x[2] for x in rows), 2), len(rows)]

    def desk(self, since_seq: int = 0, eq_since: int = 0, deals_v: int = -1, accounts: list[str] | None = None) -> dict:
        ids = set(accounts or [])
        with self.lock:
            events = [e for e in self.events if e["seq"] > since_seq]
            if ids:
                # 只看选中的账户：其它账户的事件不返回（系统事件保留）
                events = [e for e in events if not e.get("accountId") or e.get("accountId") in ids]
            if not since_seq:
                events = events[-60:]
            if ids:
                eq = [x for x in (self._eq_subset(r, ids) for r in self.equity if r[0] > eq_since) if x]
            else:
                eq = [r[:5] for r in self.equity if r[0] > eq_since]
            off = float(self.clock.get("offset") or 0)
            out = {"started": int(self.started * 1000), "now": int((time.time() + off) * 1000), "eqStep": EQ_STEP,
                   "events": events, "eventSeq": self.event_seq, "eventsTotal": self.event_seq,
                   "clockOffset": round(off, 3),
                   "equity": eq, "dealsVersion": self.deals_version, "dealsAt": int(self.deals_at * 1000),
                   "accounts": sorted(ids)}
            if deals_v != self.deals_version:
                out["deals"] = {k: v for k, v in self.deals.items() if not ids or k in ids}
        return out

    def _watchdog(self):
        while not self._stop:
            time.sleep(2)
            with self.lock:
                for acc_id, w in list(self.workers.items()):
                    if not w["proc"].is_alive():
                        self.workers.pop(acc_id, None)
                        rt = self.runtime.setdefault(acc_id, {})
                        if rt.get("link") in ("online", "connecting"):
                            rt["link"], rt["linkError"] = "error", f"工作进程意外退出（代码 {w['proc'].exitcode}）"
                            self.log(acc_id, "进程", False, rt["linkError"])

    # ---------------- 工作进程 ----------------
    def _alive(self, acc_id) -> bool:
        w = self.workers.get(acc_id)
        return bool(w and w["proc"].is_alive())

    def _new_req(self):
        req = uuid.uuid4().hex
        self.pending[req] = {"event": threading.Event(), "result": None}
        return req

    def _wait(self, reqs: dict, timeout: float) -> dict:
        """reqs: {acc_id: req}. 返回 {acc_id: result_event}。"""
        deadline = time.time() + timeout
        out = {}
        for acc_id, req in reqs.items():
            p = self.pending.get(req)
            left = max(0.1, deadline - time.time())
            if p and p["event"].wait(left):
                out[acc_id] = p["result"]
            else:
                out[acc_id] = {"ok": False, "message": "等待终端响应超时"}
            self.pending.pop(req, None)
        return out

    def _spawn(self, a: dict, req: str, explicit: bool = False):
        cmd_q = CTX.Queue()
        acc = {k: v for k, v in a.items() if k not in ("password", "last")}
        acc["_explicit"] = bool(explicit)
        acc["_worked"] = bool(a.get("last"))      # 原先登录成功过：不替换它终端里的服务器列表
        try:
            pw = self.store.password_of(a)
        except Exception as e:
            return f"密码解密失败：{e}"
        own = self.registry.get(a["terminal_path"]) if a["terminal_path"] else None
        self._watch_sent[a["id"]] = tuple(self.quick_watch)
        proc = CTX.Process(target=worker_main, name=f"mt5-{a['login']}",
                           args=(acc, pw, {**self.store.settings, "_template": self.template_info()["path"],
                                           "_watch": list(self.quick_watch)}, cmd_q, self.evt_q, self.mock,
                                 str(self.store.data_dir), req, own, str(self.root)), daemon=True)
        proc.start()
        self.workers[a["id"]] = {"proc": proc, "cmd_q": cmd_q}
        self.runtime[a["id"]] = {**self.runtime.get(a["id"], {}), "link": "connecting", "linkError": ""}
        return None

    def _send(self, acc_id, cmd: str, params: dict | None = None):
        req = self._new_req()
        self.workers[acc_id]["cmd_q"].put({"cmd": cmd, "req": req, "params": params or {}})
        return req

    # ---------------- 批量操作 ----------------
    def _summary(self, title, results: dict, log_action: str | None = None, banner: bool = True):
        ok = sum(1 for r in results.values() if r.get("ok"))
        fail = len(results) - ok
        for acc_id, r in results.items():
            self.log(acc_id, log_action or title, r.get("ok", False), r.get("message", ""))
        if banner:
            self.banner = {"title": title, "ok": ok, "fail": fail, "at": int(time.time() * 1000)}
        return {"ok": ok, "fail": fail, "results": [
            {"id": k, "alias": (self.store.get(k) or {}).get("alias", k), "ok": v.get("ok", False),
             "message": v.get("message", ""), "detail": v.get("detail")} for k, v in results.items()]}

    # ---------------- 进度 ----------------
    def _prog_begin(self, title: str, ids: list[str], steps):
        items = {}
        for i in ids:
            a = self.store.get(i) or {}
            items[i] = {"alias": a.get("alias", i), "login": a.get("login", ""), "done": False, "ok": None, "message": "",
                        "steps": {k: {"s": "wait", "m": ""} for k, _ in steps}}
        with self.lock:
            self.prog = {"id": uuid.uuid4().hex[:8], "title": title, "steps": [list(x) for x in steps], "order": list(ids),
                         "items": items, "started": int(time.time() * 1000), "done": False}

    def _prog_step(self, acc_id, key, state, msg=""):
        p = self.prog
        it = p and p["items"].get(acc_id)
        if not it or it["done"] or key not in it["steps"]:
            return
        it["steps"][key] = {"s": state, "m": msg}

    def _prog_finish(self, acc_id, ok: bool, msg: str):
        p = self.prog
        it = p and p["items"].get(acc_id)
        if not it or it["done"]:
            return
        for k, st in it["steps"].items():
            if st["s"] == "run":
                st["s"] = "ok" if ok else "fail"
                if not ok and not st["m"]:
                    st["m"] = msg
            elif st["s"] == "wait":
                st["s"] = "ok" if ok else "skip"
        if not ok and not any(st["s"] == "fail" for st in it["steps"].values()):
            first = next((k for k, st in it["steps"].items() if st["s"] == "skip"), None)
            if first:
                it["steps"][first] = {"s": "fail", "m": msg}
        it.update(done=True, ok=bool(ok), message=msg)

    def _prog_end(self):
        if self.prog:
            self.prog["done"] = True
            self.prog["ended"] = int(time.time() * 1000)

    # ---------------- 模板 / 服务器 ----------------
    def template_info(self, force: bool = False) -> dict:
        now = time.time()
        if not force and self._tpl is not None and now - self._tpl_at < 30:
            return self._tpl
        conf = (self.store.settings.get("template_dir") or "").strip()
        if conf and not os.path.isabs(conf):          # 相对路径（便携版）：相对于程序目录
            conf = os.path.normpath(os.path.join(str(app_root()), conf))
        if conf:
            ok = os.path.isfile(os.path.join(conf, "terminal64.exe"))
            info = {"path": conf if ok else "", "configured": conf, "auto": False, "candidates": [],
                    "error": "" if ok else f"模板目录里没有 terminal64.exe：{conf}"}
        else:
            try:
                c = [str(x) for x in T.find_templates(app_root(), self.root)]
            except Exception:
                c = []
            info = {"path": c[0] if c else "", "configured": "", "auto": True, "candidates": c,
                    "error": "" if c else "没有在程序目录里找到 MT5（terminal64.exe）。请在设置页填写「模板 MT5 目录」，"
                                          "或把 MT5 安装到 terminals\\base"}
        info["running"] = bool(info["path"]) and not self.mock and T.is_running(os.path.join(info["path"], "terminal64.exe"))
        info["version"] = T.exe_version(os.path.join(info["path"], "terminal64.exe")) if info["path"] else ""
        info["servers"] = T.servers_dat_count(os.path.join(info["path"], "Config", "servers.dat")) if info["path"] else -1
        self._tpl, self._tpl_at = info, now
        return info

    def remember_servers(self, names):
        known = [x for x in (self.store.settings.get("known_servers") or []) if isinstance(x, str)]
        for n in names:
            n = (n or "").strip()
            if n:
                known = [n] + [x for x in known if x != n]
        self.store.settings["known_servers"] = known[:80]
        self.store.save_settings()

    def server_names(self) -> list[str]:
        out = list(self.store.settings.get("known_servers") or [])
        for a in self.store.accounts:
            if a["server"] and a["server"] not in out:
                out.append(a["server"])
        return out

    def import_servers(self, path: str) -> dict:
        """从用户指定的 MT5 文件夹（或 servers.dat 文件）只读导入服务器列表。"""
        path = (path or "").strip().strip('"')
        if not path:
            return {"ok": False, "message": "请填写 MT5 文件夹"}
        src = path if path.lower().endswith("servers.dat") else ""
        if not src:
            c = T.servers_dat_candidates(path)
            if not c:
                return {"ok": False, "message": f"这个文件夹里没有找到 Config\\servers.dat：{path}。"
                                                "可以在那个 MT5 里点 文件 → 打开数据文件夹，把打开的文件夹路径填进来"}
            src = c[0]["path"]
        if T.is_under(src, self.root):
            return {"ok": False, "message": "请选择本程序 terminals 以外的 MT5（例如你桌面上在用的那份）"}
        ok, msg, meta = T.import_servers_dat(src, self.store.data_dir / "servers")
        if not ok:
            self.log("", "导入服务器列表", False, msg, "本机")
            return {"ok": False, "message": msg}
        self.store.settings["servers_import"] = meta
        self.store.save_settings()
        applied, waiting = 0, 0
        lib = self.store.data_dir / "servers" / "servers.dat"
        if not self.mock and self.root.exists():
            for d in self.root.iterdir():
                exe = d / "terminal64.exe"
                if d.is_dir() and exe.exists():
                    if T.is_running(str(exe)):
                        waiting += 1
                        continue
                    try:
                        if T.apply_servers_dat(str(exe), lib):
                            applied += 1
                    except Exception:
                        pass
        msg = f"{msg}，来源 {src}（只读复制，未改动原文件）。已放入 {applied} 个未运行的终端副本" + \
              (f"，{waiting} 个正在运行的会在下次由本程序启动时放入" if waiting else "") + "；新建的副本会自动带上"
        self.log("", "导入服务器列表", True, msg, "本机")
        return {"ok": True, "message": msg, **meta}

    def servers_lib_count(self) -> int:
        lib = self.store.data_dir / "servers" / "servers.dat"
        return T.servers_dat_count(lib) if lib.is_file() else -1

    def failing_ids(self) -> list[str]:
        """登录失败 / 从来没登录成功过、现在也不在线的账户（「原先能用」的账户不算）。"""
        out = []
        with self.lock:
            for a in self.store.accounts:
                if not a["enabled"]:
                    continue
                rt = self.runtime.get(a["id"], {})
                if rt.get("link") == "online" and self._alive(a["id"]):
                    continue
                if rt.get("link") == "error" or not a.get("last"):
                    out.append(a["id"])
        return out

    def _published_servers(self) -> bytes:
        got = getattr(self, "_pub_cache", None)
        if got is None:
            got = b"" if self.mock else T.bundled_servers_bytes(app_root())
            self._pub_cache = got
        return got

    def seed_published_servers(self) -> dict:
        """启动时把随程序发布的服务器列表放进来（只在它比现有的更全时），并放进模板和登录失败的终端。"""
        data = self._published_servers()
        n = T.servers_dat_count_bytes(data) if data else -1
        if n > self.servers_lib_count():
            ok, msg, meta = T.import_servers_bytes(data, self.store.data_dir / "servers", "随程序发布的服务器列表")
            if ok:
                self.store.settings["servers_import"] = meta
                self.store.save_settings()
                print(f"[提示] {msg}（发布的列表，含 42 及以上的服务器）")
        return self._push_servers_lib(log=False)

    def _push_servers_lib(self, log: bool = True) -> dict:
        """把 data\\servers\\servers.dat 放进模板（比模板更全时）和登录失败 / 没登录成功的终端。正在用的账户不动。"""
        lib = self.store.data_dir / "servers" / "servers.dat"
        cur = self.servers_lib_count()
        tpl_note = ""
        tpl = "" if self.mock else (self.template_info().get("path") or "")
        if tpl and cur > 0 and not T.is_running(os.path.join(tpl, "terminal64.exe")):
            dst = Path(tpl) / "Config" / "servers.dat"
            have = T.servers_dat_count(dst) if dst.is_file() else -1
            if cur > have:
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if dst.exists():
                        shutil.copy2(dst, dst.with_suffix(".dat.bak"))
                    dst.write_bytes(lib.read_bytes())
                    tpl_note = f"模板已更新为 {cur} 个服务器"
                except OSError as e:
                    tpl_note = f"模板未更新：{e}"
        failing = self.failing_ids()
        applied, busy = 0, 0
        for i in failing:
            a = self.store.get(i)
            pth = (a or {}).get("terminal_path") or ""
            if not pth or not os.path.isfile(pth) or self._alive(i):
                continue
            if not self.mock and T.is_running(pth):
                busy += 1
                continue
            try:
                if T.apply_servers_dat(pth, lib):
                    applied += 1
            except Exception:
                pass
        return {"count": cur, "template": tpl_note, "failing": failing, "applied": applied, "busy": busy}

    def fix_servers(self) -> dict:
        """一键修复服务器列表：先用随程序发布的列表（含 42 及以上）打底，再和本机 MT5 里更全的比较，取最多的；
        放进模板和登录失败 / 没登录成功的账户终端。正常在用的账户不动。"""
        if self.mock:
            return {"ok": False, "message": "模拟模式不需要服务器列表"}
        lib = self.store.data_dir / "servers" / "servers.dat"
        cur = self.servers_lib_count()
        cands = []
        pub = self._published_servers()
        pn = T.servers_dat_count_bytes(pub) if pub else -1
        if pn > 0:
            cands.append({"bytes": pub, "count": pn, "mtime": 0, "owner": "随程序发布的服务器列表"})
        tpl = self.template_info(force=True).get("path")
        if tpl and not T.is_under(os.path.join(tpl, "x"), self.root):
            cands += [dict(r, owner=tpl) for r in T.servers_dat_candidates(tpl)]
        try:
            for it in T.scan_mt5_installs(budget=6.0, troot=self.root):
                if it.get("servers"):
                    cands.append(dict(it["servers"], owner=it.get("origin") or it["path"]))
        except Exception:
            pass
        cands = [c for c in cands if c.get("count", -1) > 0 and ("bytes" in c or not T.is_under(c.get("path", ""), self.root))]
        if not cands and cur <= 0:
            return {"ok": False, "message": "没有找到服务器列表。请先把程序更新到 1.3.2 及以上，或在下面手动选择一份 MT5 导入"}
        best = max(cands, key=lambda c: (c["count"], c.get("mtime") or 0)) if cands else None
        parts = []
        if best and best["count"] > cur:
            if "bytes" in best:
                ok, msg, meta = T.import_servers_bytes(best["bytes"], lib.parent, best["owner"])
            else:
                ok, msg, meta = T.import_servers_dat(best["path"], lib.parent)
            if not ok:
                self.log("", "修复服务器列表", False, msg, "本机")
                return {"ok": False, "message": msg}
            self.store.settings["servers_import"] = meta
            self.store.save_settings()
            parts.append(f"已采用{best['owner']}的 {best['count']} 个服务器" + ("" if "bytes" in best else "（只读复制，原来的 MT5 没有改动）"))
            cur = best["count"]
        else:
            base = f"，其中随程序发布的有 {pn} 个" if pn > 0 else ""
            parts.append(f"服务器列表已经是最全的（{cur} 个{base}）")
        pushed = self._push_servers_lib(log=False)
        if pushed.get("template"):
            parts.append(pushed["template"])
        failing, applied, busy = pushed["failing"], pushed["applied"], pushed["busy"]
        if failing:
            parts.append(f"{len(failing)} 个登录失败 / 还没登录成功的账户：已更新 {applied} 个终端的服务器列表"
                         + (f"，{busy} 个终端正在运行（下次由本程序启动时更新）" if busy else ""))
        else:
            parts.append("现在没有登录失败的账户")
        parts.append("正常在用的账户没有改动；以后新建的终端会自动带上")
        msg = "；".join(parts)
        self.log("", "修复服务器列表", True, msg, "本机")
        return {"ok": True, "message": msg, "count": cur, "retry": failing, "published": pn}

    # ---------------- 登录 ----------------
    def _login_ids(self, ids: list[str], finish: bool = True, explicit: bool = False) -> dict:
        """登录（必要时自动创建终端副本），有并发上限。返回 {id: result}。"""
        if self.mt5_import_error:
            return {i: {"ok": False, "message": self.mt5_import_error} for i in ids}
        paths, results, todo = {}, {}, []
        for i in ids:
            a = self.store.get(i)
            if not a:
                results[i] = {"ok": False, "message": "账户不存在"}
                continue
            if not a["enabled"]:
                results[i] = {"ok": False, "message": "已停用"}
                continue
            perr = T.check_managed_path(a["terminal_path"], self.root)
            if perr:
                results[i] = {"ok": False, "message": perr}
                continue
            key = os.path.normcase(os.path.abspath(a["terminal_path"]))
            other = paths.get(key)
            if other is None:
                for wid in self.workers:
                    wa = self.store.get(wid)
                    if wid != i and wa and wa["terminal_path"] and os.path.normcase(os.path.abspath(wa["terminal_path"])) == key:
                        other = wa["alias"]
            if other:
                results[i] = {"ok": False, "message": f"终端路径与「{other}」相同，一个终端只能同时登录一个账户"}
                continue
            paths[key] = a["alias"]
            if not self.mock and not self._alive(i) and T.is_running(a["terminal_path"]) and not self.registry.get(a["terminal_path"]) \
                    and not T.adoptable_processes(a["terminal_path"]):
                results[i] = {"ok": False, "message": "这个终端文件夹里的 MT5 已在运行，但不是本程序启动的。为避免冲突不会接管，请先手动关闭它"}
                continue
            todo.append(i)
        if finish:
            for i, r in results.items():
                self._prog_finish(i, False, r["message"])

        def one(i):
            a = self.store.get(i)
            with self.lock:
                if self._alive(i):
                    if self.runtime.get(i, {}).get("link") == "online":
                        for k in ("copy", "launch", "login", "algo"):
                            self._prog_step(i, k, "ok", "已在线" if k == "login" else "")
                    req = self._send(i, "connect", {"explicit": explicit})
                else:
                    req = self._new_req()
                    err = self._spawn(a, req, explicit)
                    if err:
                        self.pending.pop(req, None)
                        return {"ok": False, "message": err}
            return self._wait({i: req}, 480)[i]

        conc = max(1, min(8, int(self.store.settings.get("login_concurrency") or 3)))
        if todo:
            with ThreadPoolExecutor(max_workers=conc) as ex:
                for i, r in zip(todo, ex.map(one, todo)):
                    results[i] = r
                    if finish:
                        self._prog_finish(i, r.get("ok", False), r.get("message", ""))
        for i, r in results.items():
            if not r.get("ok"):
                rt = self.runtime.setdefault(i, {})
                if rt.get("link") != "online":
                    rt["link"], rt["linkError"] = "error", r.get("message", "")
        ok_servers = [self.store.get(i)["server"] for i, r in results.items() if r.get("ok") and self.store.get(i)]
        if ok_servers:
            self.remember_servers(ok_servers)
        return results

    def login(self, ids: list[str], title: str = "批量登录"):
        self._prog_begin(title, ids, LOGIN_STEPS)
        self._clear_algo_off(ids)      # 用户点的登录：算法交易恢复成开
        try:
            results = self._login_ids(ids, explicit=True)
        finally:
            self._prog_end()
        return self._summary(title, results, "登录")

    def quick_add(self, rows: list[dict]) -> dict:
        """添加并登录：新账户加入台账（已存在的更新密码），然后自动 创建副本 → 启动终端 → 登录 → 打开算法交易。"""
        ids, errors, added, updated, good = [], [], 0, 0, []
        for r in rows:
            login = str(r.get("login") or "").strip()
            pw = str(r.get("password") or "")
            server = str(r.get("server") or "").strip()
            group = str(r.get("group") or "").strip()
            name = str(r.get("alias") or "").strip()
            tag = login or "空账号"
            if not login.isdigit() or len(login) < 3:
                errors.append(f"{tag}：账号需为至少 3 位数字")
                continue
            if not pw:
                errors.append(f"{tag}：请填写交易密码")
                continue
            if not server:
                errors.append(f"{tag}：请填写服务器")
                continue
            good.append(server)
            ex = next((a for a in self.store.accounts if a["login"] == login and a["server"] == server), None)
            if ex:
                upd = {"password": pw}
                if group:
                    upd["group"] = group
                if name:
                    upd["alias"] = name
                self.store.update(ex["id"], upd)
                ex["enabled"] = True
                self.store.save_accounts()
                self.account_changed(ex["id"], pw)
                self.log(ex["id"], "添加并登录", True, "账户已在列表里，已更新密码" + ("和分组/名称" if (group or name) else ""))
                if ex["id"] not in ids:
                    ids.append(ex["id"])
                updated += 1
                continue
            data = {"alias": name or login, "login": login, "password": pw, "server": server, "group": group or "未分组"}
            err = self.store.validate(data)
            if err:
                errors.append(f"{tag}：{err}")
                continue
            a = self.store.add(data)
            self.log(a["id"], "添加账户", True, "已加入台账，密码已加密保存")
            ids.append(a["id"])
            added += 1
        self.remember_servers(good)
        if not ids:
            return {"ok": 0, "fail": 0, "results": [], "errors": errors, "added": 0, "updated": 0, "ids": []}
        out = self.login(ids, "添加并登录")
        out.update(errors=errors, added=added, updated=updated, ids=ids)
        return out

    def disconnect(self, ids: list[str]):
        results, reqs = {}, {}
        close_term = bool(self.store.settings.get("close_terminal_on_disconnect"))
        with self.lock:
            for i in ids:
                if self._alive(i):
                    reqs[i] = self._send(i, "disconnect", {"close_terminal": close_term})
                else:
                    rt = self.runtime.setdefault(i, {})
                    rt["link"], rt["linkError"] = "offline", ""
        results.update(self._wait(reqs, 40))
        return self._summary("断开", results)

    def refresh(self, ids: list[str]):
        reqs, results = {}, {}
        with self.lock:
            for i in ids:
                if self._alive(i):
                    reqs[i] = self._send(i, "refresh")
                else:
                    results[i] = {"ok": False, "message": "终端未登录"}
        results.update(self._wait(reqs, 15))
        return self._summary("刷新", results)

    def run_batch(self, title: str, cmd: str, ids: list[str], params: dict, timeout: float = 60,
                  per_account: dict | None = None, banner: bool = True):
        params = dict(params)
        reqs, results = {}, {}
        with self.lock:
            for i in ids:
                a = self.store.get(i)
                if not a:
                    results[i] = {"ok": False, "message": "账户不存在"}
                elif not a["enabled"]:
                    results[i] = {"ok": False, "message": "已停用"}
                elif not self._alive(i) or self.runtime.get(i, {}).get("link") != "online":
                    results[i] = {"ok": False, "message": "终端未登录"}
                else:
                    pp = {**params, **((per_account or {}).get(i) or {})}
                    reqs[i] = self._send(i, cmd, pp)
        results.update(self._wait(reqs, timeout))
        return self._summary(title, results, banner=banner)

    # ---- 下单 ----
    def preview_order(self, ids: list[str], p: dict) -> dict:
        s = self.store.settings
        rows, total = [], 0.0
        for i in ids:
            a = self.store.get(i)
            if not a:
                continue
            rt = self.runtime.get(i, {})
            online = rt.get("link") == "online"
            eq = ((rt.get("snapshot") or {}).get("account") or {}).get("equity") or (a.get("last") or {}).get("equity") or 0
            groups = p.get("scale_groups") or []
            scaled = (not groups) or a["group"] in groups
            f = 1.0
            if scaled and p.get("scale_mode") == "multiplier":
                f = float(a.get("lot_multiplier") or 1)
            elif scaled and p.get("scale_mode") == "equity":
                ref = float(p.get("ref_equity") or 0)
                f = (eq / ref) if ref > 0 else 0
            lots = round(float(p.get("lots") or 0) * f, 4)
            warn = ""
            if not a["enabled"]:
                warn = "已停用"
            elif not online:
                warn = "未登录，会记为失败"
            elif lots > float(s.get("max_lots") or 0) > 0:
                warn = f"超过单笔上限 {s['max_lots']}，会被拦截"
            if online and a["enabled"] and not (lots > float(s.get("max_lots") or 0) > 0):
                total += lots
            rows.append({"id": i, "alias": a["alias"], "group": a["group"], "equity": eq, "factor": round(f, 4),
                         "lots": lots, "online": online, "warn": warn})
        return {"rows": rows, "total": round(total, 4), "max_lots": s.get("max_lots"),
                "max_total_lots": s.get("max_total_lots"),
                "blocked": total > float(s.get("max_total_lots") or 1e9)}

    def order(self, ids: list[str], p: dict):
        pv = self.preview_order(ids, p)
        if pv["blocked"]:
            res = {i: {"ok": False, "message": f"合计手数 {pv['total']} 超过批量上限 {pv['max_total_lots']}，整批拦截"} for i in ids}
            return self._summary("批量下单", res)
        params = {k: p.get(k) for k in ("symbol", "kind", "lots", "scale_mode", "ref_equity", "scale_groups", "sl", "tp",
                                         "sltp_mode", "price", "magic", "comment")}
        params["max_lots"] = float(self.store.settings.get("max_lots") or 0)
        params["deviation"] = int(self.store.settings.get("deviation") or 20)
        return self.run_batch("批量下单", "order", ids, params, 60)

    # ---------------- 交易页快捷面板 ----------------
    def _ensure_watch(self, symbol: str):
        """让在线账户的工作进程在每轮轮询里附带这个品种的报价（最近用过的 4 个品种）。"""
        if symbol and symbol not in self.quick_watch:
            self.quick_watch = ([symbol] + [x for x in self.quick_watch if x != symbol])[:4]
        want = tuple(self.quick_watch)
        with self.lock:
            for i, w in list(self.workers.items()):
                if self._watch_sent.get(i) == want or not w["proc"].is_alive():
                    continue
                try:
                    w["cmd_q"].put({"cmd": "watch", "req": None, "params": {"symbols": list(want)}})
                    self._watch_sent[i] = want
                except Exception:
                    pass

    @staticmethod
    def _sym_names(a: dict, symbol: str, quote: dict | None) -> set[str]:
        from types import SimpleNamespace
        from .worker import Worker
        names = {symbol}
        try:
            names.add(Worker.resolve_symbol(SimpleNamespace(acc=a), symbol))
        except Exception:
            pass
        if quote and quote.get("symbol"):
            names.add(quote["symbol"])
        return names

    @staticmethod
    def _layers(rows: list, digits: int) -> list:
        """同一价位（按报价小数位）的持仓合并成一层，按开仓时间排序。"""
        out: dict[float, list] = {}
        for t, price, vol in sorted(rows):
            k = round(price, digits)
            if k not in out:
                out[k] = [k, 0, 0.0]
            out[k][1] += 1
            out[k][2] = round(out[k][2] + vol, 2)
        return list(out.values())

    def quick_summary(self, symbol: str, ids: list[str]) -> dict:
        self._ensure_watch(symbol)
        now = time.time()
        day0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        rows, longs, shorts = [], [], []
        quote, bids = None, []
        bal = eq = fl_all = 0.0
        n_on = 0
        today = hist = 0.0
        prof = {"long": 0.0, "short": 0.0}
        with self.lock:
            for i in ids:
                a = self.store.get(i)
                if not a:
                    continue
                rt = self.runtime.get(i, {})
                link = rt.get("link", "offline")
                if link in ("online", "connecting") and not self._alive(i):
                    link = "offline"
                snap = rt.get("snapshot") if link == "online" else None
                acc = (snap or {}).get("account") or {}
                q = ((snap or {}).get("quotes") or {}).get(symbol)
                names = self._sym_names(a, symbol, q)
                pos = [p for p in (snap or {}).get("positions", []) if p["symbol"] in names]
                ll = round(sum(p["volume"] for p in pos if p["side"] == "buy"), 2)
                sl = round(sum(p["volume"] for p in pos if p["side"] == "sell"), 2)
                sp = round(sum(p["profit"] for p in pos), 2)
                if snap:
                    n_on += 1
                    bal += float(acc.get("balance", 0) or 0)
                    eq += float(acc.get("equity", 0) or 0)
                    fl_all += float(acc.get("profit", 0) or 0)
                    for p in pos:
                        (longs if p["side"] == "buy" else shorts).append((p.get("time", 0), p["openPrice"], p["volume"]))
                        prof["long" if p["side"] == "buy" else "short"] += p["profit"]
                if q and q.get("bid"):
                    bids.append(q["bid"])
                    if quote is None:
                        quote = {"bid": q["bid"], "ask": q["ask"], "digits": q.get("digits", 5), "symbol": q.get("symbol"), "from": a["alias"]}
                for d in self.deals.get(i) or []:
                    if len(d) > 2 and d[2] in names:
                        hist += d[1]
                        if d[0] >= day0:
                            today += d[1]
                last = a.get("last") or {}
                rows.append({"id": i, "alias": a["alias"], "login": a["login"], "group": a["group"], "link": link,
                             "enabled": a["enabled"], "floating": acc.get("profit", last.get("floating", 0)),
                             "equity": acc.get("equity", last.get("equity", 0)), "symbol": (q or {}).get("symbol") or "",
                             "longLots": ll, "shortLots": sl, "symProfit": sp, "positions": len(pos),
                             "error": (q or {}).get("error", "") if snap else "", "note": (q or {}).get("note", "") if snap else ""})
        digits = int((quote or {}).get("digits", 5))

        def side(lst, pr):
            vol = round(sum(x[2] for x in lst), 2)
            vwap = round(sum(x[1] * x[2] for x in lst) / vol, digits) if vol else 0
            return {"n": len(lst), "lots": vol, "vwap": vwap, "profit": round(pr, 2), "layers": self._layers(lst, digits)}

        lo, sh = side(longs, prof["long"]), side(shorts, prof["short"])
        return {
            "symbol": symbol, "quote": quote, "quotesDiffer": bool(bids) and (max(bids) - min(bids)) > 10 ** -digits * 50,
            "today": round(today, 2), "hist": round(hist, 2), "balance": round(bal, 2), "equity": round(eq, 2),
            "floatAll": round(fl_all, 2), "drawdown": round(max(0.0, (bal - eq) / bal * 100), 2) if bal > 0 else 0.0,
            "long": lo, "short": sh, "net": round(lo["profit"] + sh["profit"], 2),
            "online": n_on, "selected": len(rows), "accounts": rows,
            "auto": self.auto_public(), "autoLast": self.auto_last, "last": self.quick_last,
            "noConfirm": bool(self.store.settings.get("quick_no_confirm")), "now": int(now * 1000),
        }

    def quick_action(self, action: str, ids: list[str], p: dict) -> dict:
        if action not in QUICK_ACTIONS:
            raise ValueError(f"未知操作 {action}")
        if not self.qlock.acquire(blocking=False):
            raise QuickBusy("上一个快捷操作还在执行")
        try:
            return self._quick_run(action, ids, p)
        finally:
            self.qlock.release()

    def _quick_run(self, action: str, ids: list[str], p: dict) -> dict:
        s = self.store.settings
        title, cmd = QUICK_ACTIONS[action]
        dev = int(s.get("deviation") or 20)
        sym = str(p.get("symbol") or "")
        t0 = time.time()
        if cmd in ("order", "order_pair"):
            op = {"symbol": sym, "lots": float(p["lots"]), "scale_mode": p.get("scale_mode") or "fixed",
                  "ref_equity": float(p.get("ref_equity") or 0), "scale_groups": []}
            pv = self.preview_order(ids, op)
            total = round(pv["total"] * (2 if cmd == "order_pair" else 1), 4)
            maxt = float(s.get("max_total_lots") or 0)
            if maxt and total > maxt + 1e-9:
                res = {i: {"ok": False, "message": f"合计手数 {total:g} 超过批量上限 {maxt:g}，整批拦截"} for i in ids}
                out = self._summary(f"快捷·{title}", res, banner=False)
            else:
                params = {**op, "kind": "sell" if action == "sell" else "buy", "magic": int(s.get("default_magic") or 0),
                          "comment": "fleet quick", "max_lots": float(s.get("max_lots") or 0), "deviation": dev,
                          "auto_symbol": True, "sl": 0, "tp": 0}
                out = self.run_batch(f"快捷·{title}", cmd, ids, params, 60, banner=False)
        elif cmd == "close":
            params = {"mode": QUICK_CLOSE_MODE[action], "only_symbol": sym if p.get("only_symbol") else "", "deviation": dev}
            out = self.run_batch(f"快捷·{title}", "close", ids, params, 90, banner=False)
        else:
            out = self.run_batch(f"快捷·{title}", "nuke", ids, {"deviation": dev}, 120, banner=False)
        out["title"] = title
        out["ms"] = int((time.time() - t0) * 1000)
        self._quick_remember(action, title, sym if (cmd != "nuke" and (cmd != "close" or p.get("only_symbol"))) else "全部品种", out)
        return out

    def _quick_remember(self, action, title, sym, out):
        self.quick_last = {"action": action, "title": title, "symbol": sym, "ok": out["ok"], "fail": out["fail"],
                           "at": int(time.time() * 1000), "ms": out.get("ms", 0),
                           "fails": [{"alias": r["alias"], "message": r["message"]} for r in out["results"] if not r["ok"]][:8]}

    # ---- 自动全平（核弹级）：选中账户的合计浮动盈亏达到设定金额时触发一次，然后自动取消 ----
    def _auto_file(self) -> Path:
        return self.store.data_dir / "quick_auto.json"

    def _load_auto(self):
        try:
            a = json.loads(self._auto_file().read_text(encoding="utf-8"))
            if isinstance(a, dict) and float(a.get("amount") or 0) and isinstance(a.get("ids"), list) and a["ids"]:
                self.auto = {"amount": float(a["amount"]), "ids": [str(x) for x in a["ids"]], "at": int(a.get("at") or 0)}
                print(f"[提示] 自动全平仍处于启用状态：合计浮动盈亏达到 {self.auto['amount']:+.2f} 时全平 {len(self.auto['ids'])} 个账户")
        except Exception:
            self.auto = None

    def _save_auto(self):
        try:
            if self.auto:
                self._auto_file().write_text(json.dumps({k: self.auto[k] for k in ("amount", "ids", "at")}, ensure_ascii=False), encoding="utf-8")
            elif self._auto_file().exists():
                self._auto_file().unlink()
        except Exception:
            pass

    def _net_of(self, ids: list[str]) -> tuple[float, int]:
        net, n, now = 0.0, 0, time.time()
        with self.lock:
            for i in ids:
                rt = self.runtime.get(i) or {}
                snap = rt.get("snapshot")
                if rt.get("link") == "online" and snap and self._alive(i) and now - float(rt.get("ts") or 0) <= FRESH_SEC:
                    net += float((snap.get("account") or {}).get("profit", 0) or 0)
                    n += 1
        return round(net, 2), n

    def auto_public(self) -> dict | None:
        a = self.auto
        if not a:
            return None
        net, n = self._net_of(a["ids"])
        return {"amount": a["amount"], "ids": a["ids"], "at": a["at"], "net": net, "online": n}

    def arm_auto(self, amount: float, ids: list[str]) -> dict:
        if not amount:
            if self.auto:
                self.auto = None
                self._save_auto()
                self.log("", "自动全平", True, "已取消", "快捷面板")
            return {"auto": None}
        ids = [i for i in ids if self.store.get(i)]
        if not ids:
            raise ValueError("请先选择账户")
        net, n = self._net_of(ids)
        if not n:
            raise ValueError("选中的账户都不在线，不能启用自动全平")
        if (amount > 0 and net >= amount) or (amount < 0 and net <= amount):
            raise ValueError(f"当前合计浮动盈亏 {net:+.2f} 已经达到 {amount:+.2f}，启用会立刻全平。要马上全平请直接点「核弹级全平」")
        self.auto = {"amount": float(amount), "ids": ids, "at": int(time.time() * 1000)}
        self._save_auto()
        cond = f"≥ {amount:+.2f}（止盈）" if amount > 0 else f"≤ {amount:+.2f}（止损）"
        self.log("", "自动全平", True, f"已启用：{len(ids)} 个账户合计浮动盈亏 {cond} 时全平全部持仓并撤挂单（当前 {net:+.2f}）", "快捷面板")
        return {"auto": self.auto_public()}

    def _check_auto(self):
        a = self.auto
        if not a:
            return
        net, n = self._net_of(a["ids"])
        if not n:
            return
        amt = a["amount"]
        if not ((amt > 0 and net >= amt) or (amt < 0 and net <= amt)):
            return
        self.auto = None            # 只触发一次
        self._save_auto()
        self.log("", "自动全平", True, f"触发：{n} 个在线账户合计浮动盈亏 {net:+.2f} 达到 {amt:+.2f}，开始核弹级全平 {len(a['ids'])} 个账户", "自动")
        threading.Thread(target=self._auto_fire, args=(a, net), daemon=True).start()

    def _auto_fire(self, a: dict, net: float):
        got = self.qlock.acquire(timeout=30)      # 等正在执行的快捷操作（最多 30 秒），之后照样执行
        try:
            t0 = time.time()
            out = self.run_batch("自动全平（核弹级）", "nuke", a["ids"], {"deviation": int(self.store.settings.get("deviation") or 20)}, 120)
            out["ms"] = int((time.time() - t0) * 1000)
            self._quick_remember("auto", "自动全平", "全部品种", out)
            self.auto_last = {"at": int(time.time() * 1000), "amount": a["amount"], "net": net, "ok": out["ok"], "fail": out["fail"]}
        finally:
            if got:
                self.qlock.release()

    # ---- 策略 ----
    def _upsert_strategy(self, a: dict, s: dict, running: bool):
        for x in a["strategies"]:
            if (x["fileName"], x["symbol"], x["timeframe"], int(x["magic"])) == (s["fileName"], s["symbol"], s["timeframe"], int(s["magic"])):
                x.update(running=running, preset=s.get("preset", ""))
                return x
        x = {"id": uuid.uuid4().hex[:8], "fileName": s["fileName"], "symbol": s["symbol"], "timeframe": s["timeframe"],
             "magic": int(s["magic"]), "preset": s.get("preset", ""), "running": running}
        a["strategies"].append(x)
        return x

    def _deploy_many(self, ids: list[str], per: dict, timeout: float = 420) -> dict:
        """按并发上限逐个账户部署。per: {id: 参数}。"""
        def one(i):
            with self.lock:
                if not self._alive(i) or self.runtime.get(i, {}).get("link") != "online":
                    return {"ok": False, "message": "终端未登录"}
                req = self._send(i, "deploy", per[i])
            return self._wait({i: req}, timeout)[i]

        results = {}
        conc = max(1, min(4, int(self.store.settings.get("dist_concurrency") or 2)))
        if ids:
            with ThreadPoolExecutor(max_workers=conc) as ex:
                for i, r in zip(ids, ex.map(one, ids)):
                    results[i] = r
                    self._prog_finish(i, r.get("ok", False), r.get("message", ""))
        return results

    def _record_deploy(self, results: dict, per: dict, replace: bool):
        for i, r in results.items():
            a = self.store.get(i)
            if not a or not r.get("ok"):
                continue
            pp = per[i]
            d = r.get("detail") or {}
            if replace:
                a["strategies"] = [x for x in a["strategies"] if x["fileName"] != pp["fileName"]]
            self._upsert_strategy(a, {**pp, "magic": d.get("magic", pp["magic"]), "preset": pp.get("preset", ""),
                                      "symbol": d.get("symbol") or pp.get("symbol", "")},
                                  bool(d.get("running", True)))
        self.store.save_accounts()

    def deploy(self, ids: list[str], s: dict):
        """单个策略「启动」用：部署到指定账户（不替换同名 EA）。"""
        title = "部署策略"
        self._prog_begin(title, ids, DIST_STEPS)
        self._clear_algo_off(ids)
        try:
            for i in ids:
                self._prog_step(i, "login", "ok", "已在线" if self.runtime.get(i, {}).get("link") == "online" else "")
            per = {i: dict(s) for i in ids}
            ready = [i for i in ids if self._alive(i) and self.runtime.get(i, {}).get("link") == "online"]
            results = {i: {"ok": False, "message": "终端未登录，先登录再部署"} for i in ids if i not in ready}
            for i, r in results.items():
                self._prog_finish(i, False, r["message"])
            results.update(self._deploy_many(ready, per))
        finally:
            self._prog_end()
        self._record_deploy(results, per, False)
        return self._summary(title, results)

    def distribute(self, ids: list[str], s: dict, opts: dict) -> dict:
        """一键分发：未登录的先自动登录 → 每个账户 复制 EA → 重启终端 → 挂到图表 → 校验。"""
        title = "分发策略"
        self._prog_begin(title, ids, DIST_STEPS)
        results: dict = {}
        per: dict = {}
        replace = bool(opts.get("replace", True))
        self._clear_algo_off(ids)      # 分发是用户点的：EA 要跑，算法交易恢复成开
        try:
            need = []
            for i in ids:
                if self._alive(i) and self.runtime.get(i, {}).get("link") == "online":
                    self._prog_step(i, "login", "ok", "已在线")
                else:
                    need.append(i)
            if need:
                for i in need:
                    self._prog_step(i, "login", "run", "未登录，正在自动登录")
                lr = self._login_ids(need, finish=False, explicit=True)
                for i, r in lr.items():
                    if r.get("ok"):
                        self._prog_step(i, "login", "ok", r.get("message", ""))
                    else:
                        results[i] = {"ok": False, "message": "登录失败：" + r.get("message", "")}
                        self._prog_finish(i, False, results[i]["message"])
            base = int(s["magic"])
            step = int(opts.get("magic_step") or 0)
            gp = opts.get("group_presets") or {}
            for k, i in enumerate(ids):
                a = self.store.get(i) or {}
                per[i] = {**s, "magic": base + k * step,
                          "preset": (gp.get(a.get("group", "")) or s.get("preset") or ""),
                          "magic_param": (opts.get("magic_param") or "").strip(), "replace": replace}
            ready = [i for i in ids if i not in results]
            results.update(self._deploy_many(ready, per))
        finally:
            self._prog_end()
        self._record_deploy(results, per, replace)
        out = self._summary(title, results)
        return out

    def stop_strategies(self, ids: list[str], only: dict[str, list[str]] | None = None, title="停止策略"):
        per, run_ids, results = {}, [], {}
        for i in ids:
            a = self.store.get(i)
            if not a:
                continue
            names = sorted({x["fileName"] for x in a["strategies"] if x["running"] and (not only or x["id"] in only.get(i, []))})
            if not names:
                results[i] = {"ok": True, "message": "没有正在运行的策略"}
                continue
            per[i] = {"fileNames": names}
            run_ids.append(i)
        self._prog_begin(title, [i for i in ids if self.store.get(i)], STOP_STEPS)
        try:
            for i, r in results.items():
                self._prog_finish(i, True, r["message"])
            out = self.run_batch(title, "stop_ea", run_ids, {}, timeout=240, per_account=per) if run_ids else {"ok": 0, "fail": 0, "results": []}
            for r in out["results"]:
                self._prog_finish(r["id"], r["ok"], r["message"])
        finally:
            self._prog_end()
        if results:
            extra = self._summary(title, results)
            out = {"ok": out["ok"] + extra["ok"], "fail": out["fail"] + extra["fail"], "results": out["results"] + extra["results"]}
            self.banner = {**(self.banner or {}), "title": title, "ok": out["ok"], "fail": out["fail"]}
        for r in out["results"]:
            a = self.store.get(r["id"])
            if a and r["ok"]:
                names = set(per.get(r["id"], {}).get("fileNames", []))
                for x in a["strategies"]:
                    if x["fileName"] in names:
                        x["running"] = False
        self.store.save_accounts()
        return out

    def strategy_action(self, acc_id: str, sid: str, action: str):
        a = self.store.get(acc_id)
        x = next((s for s in (a or {}).get("strategies", []) if s["id"] == sid), None)
        if not a or not x:
            return {"ok": 0, "fail": 1, "results": [{"id": acc_id, "ok": False, "message": "策略不存在"}]}
        if action == "start":
            return self.deploy([acc_id], {k: x[k] for k in ("fileName", "symbol", "timeframe", "magic", "preset")})
        if action == "stop":
            return self.stop_strategies([acc_id], {acc_id: [sid]})
        if action == "remove":
            if x["running"]:
                out = self.stop_strategies([acc_id], {acc_id: [sid]}, "移除策略")
                if not out["ok"]:
                    return out
            a["strategies"] = [s for s in a["strategies"] if s["id"] != sid]
            self.store.save_accounts()
            self.log(acc_id, "移除策略", True, f"已从台账移除 {x['fileName']}")
            return {"ok": 1, "fail": 0, "results": [{"id": acc_id, "ok": True, "message": "已移除"}]}
        return {"ok": 0, "fail": 1, "results": []}

    def strip_strategies(self, ids: list[str]):
        out = self.stop_strategies(ids, None, "移除策略")
        failed = {r["id"] for r in out["results"] if not r["ok"]}
        for i in ids:
            a = self.store.get(i)
            if a and i not in failed:
                n = len(a["strategies"])
                a["strategies"] = []
                self.log(i, "移除策略", True, f"已从台账移除 {n} 个 EX5")
        self.store.save_accounts()
        return out

    # ---- 收益 ----
    def history(self, ids: list[str]):
        reqs, results = {}, {}
        with self.lock:
            for i in ids:
                if self._alive(i) and self.runtime.get(i, {}).get("link") == "online":
                    reqs[i] = self._send(i, "history", {})
        results.update(self._wait(reqs, 60))
        out = []
        for i, r in results.items():
            a = self.store.get(i) or {}
            for d in (r.get("detail") or {}).get("deals", []):
                out.append({**d, "accountId": i, "alias": a.get("alias", i)})
        return {"deals": out, "accounts": len(reqs), "skipped": len([i for i in ids if i not in reqs])}

    # ---------------- 账户变更 ----------------
    # ---------------- 交易页：一键开关算法交易 ----------------
    def _clear_algo_off(self, ids: list[str]):
        changed = False
        for i in ids:
            a = self.store.get(i)
            if a and a.get("algo_off"):
                a["algo_off"] = False
                changed = True
                self.account_changed(i)
        if changed:
            self.store.save_accounts()

    def set_algo(self, ids: list[str], on: bool) -> dict:
        """在所选账户（本程序启动的终端）里打开 / 关闭「算法交易」按钮，并按终端回读的状态报告结果。"""
        title = "一键开启算法交易" if on else "一键关闭算法交易"
        out = self.run_batch(title, "algo", ids, {"on": bool(on)}, 30, banner=False)
        changed = False
        for r in out["results"]:
            a = self.store.get(r["id"])
            if a and r["ok"]:
                if bool(a.get("algo_off")) != (not on):
                    a["algo_off"] = not on
                    changed = True
            r["trade_allowed"] = (r.get("detail") or {}).get("trade_allowed")
        if changed:
            self.store.save_accounts()
        out["title"], out["on"] = title, bool(on)
        return out

    # ---------------- 自定义分组 ----------------
    def group_names(self) -> list[str]:
        """设置里登记的分组 + 账户上实际用到的分组（去重，未分组始终在最后可选）。"""
        raw = self.store.settings.get("account_groups") or []
        names = []
        for g in list(raw) + [a.get("group") or "" for a in self.store.accounts]:
            g = str(g or "").strip() or "未分组"
            if g not in names:
                names.append(g)
        if "未分组" in names:
            names = [g for g in names if g != "未分组"] + ["未分组"]
        return names

    def _save_group_names(self, names: list[str]):
        cleaned, seen = [], set()
        for g in names:
            g = str(g or "").strip() or "未分组"
            if g == "未分组" or g in seen:
                continue
            seen.add(g)
            cleaned.append(g)
        self.store.settings["account_groups"] = cleaned
        self.store.save_settings()

    def create_group(self, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            return {"ok": False, "message": "请填写分组名"}
        if name == "未分组":
            return {"ok": False, "message": "「未分组」是系统保留名"}
        if len(name) > 32:
            return {"ok": False, "message": "分组名不要超过 32 个字"}
        names = [g for g in self.group_names() if g != "未分组"]
        if name in names:
            return {"ok": False, "message": f"分组「{name}」已经有了"}
        names.append(name)
        self._save_group_names(names)
        self.log("", "分组", True, f"新建分组「{name}」", "本机")
        return {"ok": True, "message": f"已新建「{name}」", "groups": self.group_names()}

    def rename_group(self, old: str, new: str) -> dict:
        old, new = (old or "").strip(), (new or "").strip()
        if not old or not new:
            return {"ok": False, "message": "请填写原名和新名"}
        if old == "未分组" or new == "未分组":
            return {"ok": False, "message": "「未分组」不能改名"}
        if old == new:
            return {"ok": True, "message": "名字没变", "groups": self.group_names()}
        names = [g for g in self.group_names() if g != "未分组"]
        if old not in names and not any(a.get("group") == old for a in self.store.accounts):
            return {"ok": False, "message": f"没有分组「{old}」"}
        if new in names and new != old:
            return {"ok": False, "message": f"分组「{new}」已经有了"}
        names = [new if g == old else g for g in names]
        if new not in names:
            names.append(new)
        n = 0
        for a in self.store.accounts:
            if a.get("group") == old:
                a["group"] = new
                n += 1
                self.account_changed(a["id"])
        self._save_group_names(names)
        self.store.save_accounts()
        self.log("", "分组", True, f"分组「{old}」改名为「{new}」（{n} 个账户）", "本机")
        return {"ok": True, "message": f"已改名为「{new}」", "moved": n, "groups": self.group_names()}

    def delete_group(self, name: str) -> dict:
        name = (name or "").strip()
        if not name or name == "未分组":
            return {"ok": False, "message": "「未分组」不能删除"}
        names = [g for g in self.group_names() if g != "未分组" and g != name]
        n = 0
        for a in self.store.accounts:
            if a.get("group") == name:
                a["group"] = "未分组"
                n += 1
                self.account_changed(a["id"])
        self._save_group_names(names)
        self.store.save_accounts()
        self.log("", "分组", True, f"删除分组「{name}」，{n} 个账户回到未分组", "本机")
        return {"ok": True, "message": f"已删除「{name}」", "moved": n, "groups": self.group_names()}

    def assign_group(self, ids: list[str], group: str) -> dict:
        group = (group or "").strip() or "未分组"
        if group != "未分组":
            names = [g for g in self.group_names() if g != "未分组"]
            if group not in names:
                names.append(group)
                self._save_group_names(names)
        n = 0
        for i in ids:
            a = self.store.get(i)
            if not a:
                continue
            if a.get("group") != group:
                a["group"] = group
                n += 1
                self.account_changed(i)
        if n:
            self.store.save_accounts()
            self.log("", "分组", True, f"{n} 个账户移入「{group}」", "本机")
        return {"ok": True, "message": f"已把 {n} 个账户放到「{group}」", "moved": n, "groups": self.group_names()}

    def account_changed(self, acc_id: str, password: str | None = None):
        a = self.store.get(acc_id)
        if a and self._alive(acc_id):
            acc = {k: v for k, v in a.items() if k not in ("password", "last")}
            acc["_worked"] = bool(a.get("last"))
            self.workers[acc_id]["cmd_q"].put({"cmd": "update_account", "account": acc, "password": password})

    def toggle_enabled(self, acc_id: str):
        a = self.store.get(acc_id)
        if not a:
            return None
        a["enabled"] = not a["enabled"]
        if not a["enabled"]:
            if self._alive(acc_id):
                self.disconnect([acc_id])
        self.store.save_accounts()
        self.log(acc_id, "启用" if a["enabled"] else "停用", True,
                 "已启用" if a["enabled"] else "已停用并断开终端（不会关闭 MT5 里的 EA，需停止请用策略页）")
        return a

    def remove(self, ids: list[str]):
        live = [i for i in ids if self._alive(i)]
        if live:
            self.disconnect(live)
        gone = self.store.remove(ids)
        for a in gone:
            self.log(a["id"], "删除", True, "已从台账删除", a["alias"])
            self.runtime.pop(a["id"], None)
        return len(gone)

    # ---------------- 浮亏警报 / 报价 / 日历 ----------------
    def _alerts_path(self) -> Path:
        return self.store.data_dir / "alerts.json"

    def _load_alerts(self):
        try:
            d = json.loads(self._alerts_path().read_text(encoding="utf-8"))
        except Exception:
            d = {}
        self.alerts = list(d.get("alerts") or [])
        self.alert_armed = {str(k): True for k, v in (d.get("armed") or {}).items() if v}
        self.alert_seq = int(d.get("seq") or 0)

    def _save_alerts(self):
        try:
            self.store._write(self._alerts_path(), {
                "alerts": self.alerts, "armed": self.alert_armed, "seq": self.alert_seq,
            })
        except Exception:
            pass

    def _apply_alerts_locked(self):
        """调用方已持有 self.lock。返回 (日志, 是否变化)，由 _flush_alerts 落盘。"""
        rows = []
        for a in self.store.accounts:
            rt = self.runtime.get(a["id"], {})
            snap = rt.get("snapshot")
            if rt.get("link") != "online" or not snap:
                continue
            acc = snap.get("account") or {}
            rows.append({
                "id": a["id"], "alias": a.get("alias") or a["id"], "login": a.get("login") or "",
                "floating": acc.get("profit", 0), "currency": acc.get("currency") or "",
            })
        fresh, armed = alerts.scan(rows, alerts.loss_threshold(self.store.settings), self.alert_armed)
        changed = bool(fresh) or armed != self.alert_armed
        self.alert_armed = armed
        logs = []
        now = int(time.time() * 1000)
        for f in fresh:
            self.alert_seq += 1
            item = {**f, "n": self.alert_seq, "id": uuid.uuid4().hex[:8], "at": now}
            self.alerts.append(item)
            text = f"{item['alias']}（{item['login']}）浮亏 {item['floating']:.2f} {item['currency']}".strip()
            self.alert_popup = {"id": item["id"], "n": item["n"], "title": f"警报{item['n']}", "text": text, "at": now}
            logs.append((item["accountId"], text, item["alias"]))
        return logs, changed

    def _flush_alerts(self):
        pending = getattr(self, "_pending_alert", None)
        self._pending_alert = None
        if not pending:
            return
        logs, changed = pending
        for acc_id, text, alias in logs:
            self.log(acc_id, "浮亏警报", True, text, alias)
        if changed:
            self._save_alerts()

    def _sync_alerts(self, accounts: list[dict]):
        with self.lock:
            self._pending_alert = self._apply_alerts_locked()
        self._flush_alerts()

    def view_alert(self, alert_id: str) -> bool:
        """查看后取消这一条。不重新武装：浮亏还在阈值以下时不会再响，要等回到阈值以上。"""
        with self.lock:
            before = len(self.alerts)
            self.alerts = [a for a in self.alerts if a.get("id") != alert_id]
            if self.alert_popup and self.alert_popup.get("id") == alert_id:
                self.alert_popup = None
            gone = len(self.alerts) != before
        if gone:
            self._save_alerts()
        return gone

    def set_mock_floating(self, acc_id: str, floating: float) -> tuple[bool, str]:
        if not self.mock:
            return False, "只有测试模式可以设置浮亏"
        if not self._alive(acc_id):
            return False, "这个账户不在线"
        self._send(acc_id, "set_float", {"floating": floating})
        return True, "已发送"

    def _quote_loop(self):
        while not self._stop:
            try:
                self._refresh_quote()
            except Exception:
                pass
            for _ in range(5):
                if self._stop:
                    return
                time.sleep(1)

    def _terminal_quote(self) -> dict | None:
        with self.lock:
            for rt in self.runtime.values():
                if rt.get("link") != "online":
                    continue
                q = fx.from_terminal((rt.get("snapshot") or {}).get("usdjpy"))
                if q:
                    return q
        return None

    def _refresh_quote(self):
        term = self._terminal_quote()
        if term:
            self.quote = {**term, "at": int(time.time() * 1000)}
            return
        age = time.time() * 1000 - (self.quote or {}).get("at", 0)
        if self.quote and self.quote.get("source") == "公开行情" and self.quote.get("price") and age < 60_000:
            return
        try:
            q = fx.public_usdjpy()
            self.quote = {**q, "at": int(time.time() * 1000), "error": ""}
        except Exception as e:
            if not (self.quote and self.quote.get("price")):
                self.quote = {"symbol": "USDJPY", "price": 0, "change": 0, "source": "", "error": str(e),
                              "at": int(time.time() * 1000)}

    # ---------------- 状态 ----------------
    def state(self) -> dict:
        accounts = []
        with self.lock:
            for a in self.store.accounts:
                rt = self.runtime.get(a["id"], {})
                link = rt.get("link", "offline")
                if link in ("online", "connecting") and not self._alive(a["id"]):
                    link = "offline"
                snap = rt.get("snapshot") if link == "online" or (link == "connecting" and rt.get("snapshot")) else None
                last = a.get("last") or {}
                acc = (snap or {}).get("account") or {}
                d = self.store.public(a)
                d.update({
                    "link": link, "linkError": rt.get("linkError", ""),
                    "balance": acc.get("balance", last.get("balance", 0)),
                    "equity": acc.get("equity", last.get("equity", 0)),
                    "floating": acc.get("profit", last.get("floating", 0)),
                    "margin": acc.get("margin", 0), "marginFree": acc.get("margin_free", 0),
                    "marginLevel": acc.get("margin_level", 0), "currency": acc.get("currency", last.get("currency", "")),
                    "company": acc.get("company", ""), "leverage": acc.get("leverage", 0),
                    "positions": (snap or {}).get("positions", []), "orders": (snap or {}).get("orders", []),
                    "positionsCount": len((snap or {}).get("positions", [])) if snap else last.get("positions", 0),
                    "terminal": (snap or {}).get("terminal"), "stale": snap is None, "lastTs": last.get("ts", 0),
                    "ownedTerminal": bool(self.registry.get(a["terminal_path"])) if a["terminal_path"] else False,
                })
                accounts.append(d)
        self._sync_alerts(accounts)
        popup = self.alert_popup
        if popup and int(time.time() * 1000) - int(popup.get("at") or 0) > alerts.POPUP_MS:
            popup = None
        settings = {k: v for k, v in self.store.settings.items() if k != "remote_pass_hash"}
        return {"accounts": accounts, "settings": settings, "banner": self.banner, "mock": self.mock,
                "alerts": list(self.alerts), "alertPopup": popup, "popupMs": alerts.POPUP_MS,
                "quote": self.quote, "remote": self.remote.public(),
                "clock": {"unix": int(self.clock.get("unix") or 0),
                          "offset": round(float(self.clock.get("offset") or 0), 3),
                          "source": self.clock.get("source") or "",
                          "label": "服务器时间"},
                "groups": self.group_names(),
                "mt5Error": self.mt5_import_error, "dpapi": secrets.dpapi_supported(),
                "library": self.library(), "firstLaunch": self.store.first_launch,
                "terminalsRoot": str(self.root), "dataDir": str(self.store.data_dir), "now": int(time.time() * 1000),
                "progress": self.prog, "servers": self.server_names(), "template": self.template_info(),
                "serversImport": self.store.settings.get("servers_import"), "serversLib": self.servers_lib_count(),
                "serversPublished": T.servers_dat_count_bytes(self._published_servers())}

    def library(self):
        lib = self.store.data_dir / "ea_library"
        return sorted(p.name for p in lib.iterdir() if p.suffix.lower() in (".ex5", ".set")) if lib.exists() else []

    def shutdown(self):
        with self.lock:
            for i, w in list(self.workers.items()):
                try:
                    w["cmd_q"].put({"cmd": "exit"})
                except Exception:
                    pass
        deadline = time.time() + 5
        for w in list(self.workers.values()):
            w["proc"].join(timeout=max(0.1, deadline - time.time()))
            if w["proc"].is_alive():
                w["proc"].terminate()
        if self.store.settings.get("close_terminals_on_exit") and not self.mock:
            self.close_owned_terminals()
        self._stop = True
        try:
            self.remote.stop()
        except Exception:
            pass
        self.store.save_accounts()

    def close_owned_terminals(self) -> list[str]:
        """只关闭登记在 launched.json 里、由本程序启动的终端。"""
        msgs = []
        entries = self.registry.all_alive()
        threads = []

        def one(e):
            ok, msg = T.close_owned(e)
            if ok:
                self.registry.remove(e["path"])
            msgs.append(f"{e['path']}：{msg}")

        for e in entries:
            th = threading.Thread(target=one, args=(e,))
            th.start()
            threads.append(th)
        for th in threads:
            th.join(timeout=40)
        for m in msgs:
            print("  " + m)
        return msgs
