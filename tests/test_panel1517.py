"""v1.5.17：工具栏关着时，批量终端仍可开平仓；更新/登录/重连不重启、不改算法。
python tests/test_panel1517.py
"""
import inspect
import queue
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fleet import __version__
from fleet.manager import Fleet
from fleet.worker import Worker

fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {info}" if info else ""))
    if not cond:
        fails.append(name)


check("版本 1.5.17", __version__ == "1.5.17", __version__)

notes = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
readme = (ROOT / "README.md").read_text(encoding="utf-8")
worker = (ROOT / "fleet" / "worker.py").read_text(encoding="utf-8")
mgr = (ROOT / "fleet" / "manager.py").read_text(encoding="utf-8")
app = (ROOT / "app.py").read_text(encoding="utf-8")
html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
aj = (ROOT / "static" / "algo.js").read_text(encoding="utf-8")
appjs = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
qj = (ROOT / "static" / "quick.js").read_text(encoding="utf-8")
desk = (ROOT / "static" / "desk.js").read_text(encoding="utf-8")
pend = (ROOT / "static" / "pending.js").read_text(encoding="utf-8")

check("更新说明 v1.5.17", "## v1.5.17" in notes and "开平仓" in notes and "keep_terminals" in notes)
check("README 本次更新 1.5.17", "v1.5.17" in readme.split("上次更新")[0] and "开平仓" in readme.split("上次更新")[0])
check("UI 不再写工具栏关则下单失败、需手动点开",
      "面板下单会失败" not in html and "请在该终端窗口手动点开" not in html
      and "工具栏需本来是开的" not in desk and "工具栏需本来是开的" not in qj)

# 源码门禁：交易路径恢复 ensure；不做完再关；不重启
trade = worker[worker.find("def with_algo_for_trade"):worker.find("def _ea_already_in_state")]
pref = worker[worker.find("def apply_algo_pref"):worker.find("def cmd_algo")]
algo_fn = worker[worker.find("def cmd_algo"):worker.find("def check_algo")]
live_fn = worker[worker.find("def apply_ea_live"):worker.find("def apply_algo_pref")]
ens_fn = worker[worker.find("def ensure_toolbar_algo_on"):worker.find("def with_algo_for_trade")]

check("with_algo 调用 ensure_toolbar_algo_on", "ensure_toolbar_algo_on" in trade)
check("with_algo 无 set_algo(False) / finally 关闭", "set_algo(False)" not in trade)
check("with_algo 文档写明不重启、不做完再关", "绝不因交易重启" in trade and "不做完再关" in trade)
check("with_algo 体内无 _restart/_relaunch", "self._restart" not in trade and "self._relaunch" not in trade)
check("ensure 关着才 set_algo(True)，已开则跳过", "return self.set_algo(True)" in ens_fn and "if cur is True" in ens_fn)
check("ensure 面板禁止 EA 时先静音", "apply_ea_live(False" in ens_fn and "algo_off" in ens_fn)
check("ensure 无关闭工具栏或重启", "set_algo(False)" not in ens_fn and "self._restart" not in ens_fn and "self._relaunch" not in ens_fn)
check("worker 生产路径无 set_algo(False)", "set_algo(False)" not in worker)
check("apply_algo_pref 默认路径不调用 ensure / set_algo",
      "ensure_toolbar_algo_on" not in pref.split("if deploy:")[0] and "set_algo(" not in pref.split("if deploy:")[0])
check("apply_algo_pref 不再 may_relaunch=True", "may_relaunch=True" not in pref)
check("apply_ea_live 不再重启终端", "self._restart" not in live_fn and "self._relaunch" not in live_fn and "已重启终端并" not in live_fn)
check("cmd_algo 不点工具栏、不重启", "ensure_toolbar_algo_on" not in algo_fn and "may_relaunch=True" not in algo_fn and "已重启终端" not in algo_fn)
check("shutdown 支持 keep_terminals", "def shutdown(self, keep_terminals: bool = False)" in mgr)
check("更新退出设置 keep_terminals", "_keep_terminals_on_shutdown" in app and "keep_terminals=keep" in app)
for name, src in (("algo.js", aj), ("quick.js", qj), ("desk.js", desk), ("app.js", appjs), ("index.html", html), ("pending.js", pend)):
    check(f"{name} 不再写短暂打开", "短暂打开" not in src)


def make_worker(password="mock-pass", algo_off=False, **acc):
    a = {"id": "x", "login": "123456", "server": "S", "terminal_path": "/tmp/x/terminal64.exe", "algo_off": algo_off, **acc}
    w = Worker(a, password, {"auto_enable_algo": False}, queue.Queue(), queue.Queue(), True, "/tmp/fleet1517w")
    return w


# 登录 / 重连：原先开保持开，原先关保持关；不 set_algo、不重启
for pw, off, want_tb, label in (
    ("mock-pass", False, True, "原先工具栏开、面板允许"),
    ("mock-pass", True, True, "原先工具栏开、面板禁止"),
    ("noalgo", False, False, "原先工具栏关、面板允许"),
    ("noalgo", True, False, "原先工具栏关、面板禁止"),
):
    w = make_worker(password=pw, algo_off=off)
    set_calls, rest = [], []
    real_set = w.set_algo
    w.set_algo = lambda on, _r=real_set: (set_calls.append(on) or _r(on))
    w._restart = lambda *a, **k: (rest.append("restart") or (_ for _ in ()).throw(RuntimeError("restart-called")))
    w._relaunch = lambda *a, **k: (rest.append("relaunch") or (_ for _ in ()).throw(RuntimeError("relaunch-called")))
    ok, msg = w.connect()
    check(f"{label}：登录成功", ok, msg)
    check(f"{label}：登录不 set_algo", set_calls == [], set_calls)
    check(f"{label}：登录不重启", rest == [], rest)
    check(f"{label}：工具栏保持 {want_tb}", w._algo_now() is want_tb, w._algo_now())
    check(f"{label}：algo_off 保持 {off}", w.acc.get("algo_off") is off, w.acc.get("algo_off"))

w = make_worker(password="noalgo", algo_off=True)
ok, msg = w.connect()
assert ok, msg
set_calls = []
w.set_algo = lambda on: (set_calls.append(on) or (False, "should-not", {}))
w._restart = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("restart-called"))
w.link = "online"
w._explicit = True
_, note = w.apply_algo_pref(w.snapshot())
check("已在线再登录：不 set_algo", set_calls == [], set_calls)
check("已在线再登录：工具栏仍关、algo_off 仍真", w._algo_now() is False and w.acc.get("algo_off") is True)
check("已在线再登录说明保持关闭", "保持关闭" in (note or ""), note)


# 工具栏已关 + 用户下单：打开一次，订单成功，EA 保持静音，不做完再关，不重启
w2 = make_worker(password="noalgo", algo_off=True)
ok, msg = w2.connect()
assert ok and w2._algo_now() is False and w2.acc.get("algo_off") is True
set_calls, live_calls, rest = [], [], []
real_set = w2.set_algo
real_live = w2.apply_ea_live

def spy_set(on):
    set_calls.append(on)
    return real_set(on)

def spy_live(allow, may_relaunch=False):
    live_calls.append((allow, may_relaunch))
    return real_live(allow, may_relaunch=may_relaunch)

w2.set_algo = spy_set
w2.apply_ea_live = spy_live
w2._restart = lambda *a, **k: (rest.append("restart") or (_ for _ in ()).throw(RuntimeError("restart-called")))
w2._relaunch = lambda *a, **k: (rest.append("relaunch") or (_ for _ in ()).throw(RuntimeError("relaunch-called")))
ok, msg, d = w2.with_algo_for_trade(w2.cmd_order, {
    "symbol": "XAUUSD", "kind": "buy", "lots": 0.01, "scale_mode": "fixed",
    "auto_symbol": True, "deviation": 20, "magic": 880000, "max_lots": 10,
})
check("工具栏已关：下单成功", ok, msg)
check("工具栏已关：只 set_algo(True) 一次", set_calls == [True], set_calls)
check("工具栏已关：下单后工具栏已开", w2._algo_now() is True, w2._algo_now())
check("工具栏已关：algo_off 仍禁止", w2.acc.get("algo_off") is True)
check("工具栏已关：先静音 EA 且未恢复",
      (False, False) in live_calls and all(c[0] is False for c in live_calls), live_calls)
check("工具栏已关：下单后无 set_algo(False)", False not in set_calls)
check("工具栏已关：下单不重启", rest == [], rest)

# 再平仓 / 锁仓：工具栏已开，不再点击
set_calls.clear(); live_calls.clear(); rest.clear()
ok, msg, d = w2.with_algo_for_trade(w2.cmd_close, {"mode": "all", "deviation": 20})
check("平仓：工具栏已开不再点", ok and set_calls == [], (ok, msg, set_calls))
check("平仓不重启、algo_off 仍禁止", rest == [] and w2.acc.get("algo_off") is True)

w2.with_algo_for_trade(w2.cmd_order, {
    "symbol": "XAUUSD", "kind": "buy", "lots": 0.02, "scale_mode": "fixed",
    "auto_symbol": True, "deviation": 20, "magic": 880000, "max_lots": 10,
})
set_calls.clear(); rest.clear()
ok, msg, d = w2.with_algo_for_trade(w2.cmd_lock, {
    "symbol": "XAUUSD", "auto_symbol": True, "deviation": 20, "magic": 880000, "max_lots": 10,
})
check("锁仓成功且不再点已开工具栏", ok and set_calls == [], (ok, msg, set_calls))
check("锁仓后工具栏仍开、algo_off 仍禁止、不重启",
      w2._algo_now() is True and w2.acc.get("algo_off") is True and rest == [], (w2._algo_now(), rest, msg))

# 工具栏已开：ensure 不点击
w = make_worker(password="mock-pass", algo_off=True)
ok, msg = w.connect()
assert ok and w._algo_now() is True
set_calls, rest = [], []
w.set_algo = lambda on: (set_calls.append(on) or (False, "no", {}))
w._restart = lambda *a, **k: (rest.append("r") or (_ for _ in ()).throw(RuntimeError("restart-called")))
ok, msg, d = w.with_algo_for_trade(w.cmd_order, {
    "symbol": "XAUUSD", "kind": "buy", "lots": 0.01, "scale_mode": "fixed",
    "auto_symbol": True, "deviation": 20, "magic": 880000, "max_lots": 10,
})
check("工具栏已开：下单成功且不 set_algo", ok and set_calls == [], (ok, msg, set_calls))
check("工具栏已开：下单后状态不变、不重启",
      w._algo_now() is True and w.acc.get("algo_off") is True and rest == [])

# 面板允许 EA、工具栏关：打开工具栏，不要静音 EA
w3 = make_worker(password="noalgo", algo_off=False)
ok, msg = w3.connect()
assert ok and w3._algo_now() is False and w3.acc.get("algo_off") is False
set_calls, live_calls, rest = [], [], []
real_set = w3.set_algo
real_live = w3.apply_ea_live
w3.set_algo = lambda on, _r=real_set: (set_calls.append(on) or _r(on))
w3.apply_ea_live = lambda allow, may_relaunch=False, _r=real_live: (live_calls.append((allow, may_relaunch)) or _r(allow, may_relaunch=may_relaunch))
w3._restart = lambda *a, **k: (rest.append("r") or (_ for _ in ()).throw(RuntimeError("restart-called")))
ok, msg, d = w3.with_algo_for_trade(w3.cmd_order, {
    "symbol": "XAUUSD", "kind": "buy", "lots": 0.01, "scale_mode": "fixed",
    "auto_symbol": True, "deviation": 20, "magic": 880000, "max_lots": 10,
})
check("面板允许且工具栏关：下单成功并打开工具栏", ok and set_calls == [True] and w3._algo_now() is True, (ok, msg, set_calls))
check("面板允许且工具栏关：未静音 EA", all(c[0] is not False for c in live_calls), live_calls)
check("面板允许且工具栏关：algo_off 仍否、不重启、不关回",
      w3.acc.get("algo_off") is False and rest == [] and False not in set_calls)

# 用户点开关：不点工具栏、不重启
w = make_worker(password="mock-pass", algo_off=False)
ok, msg = w.connect()
assert ok
set_calls, rest = [], []
w.set_algo = lambda on: (set_calls.append(on) or (True, "x", {"trade_allowed": True}))
w._restart = lambda *a, **k: (rest.append("r") or (False, "no"))
ok, msg, d = w.cmd_algo({"on": False})
check("用户关开关成功", ok and w.acc.get("algo_off") is True, msg)
check("用户关开关不 set_algo、不重启", set_calls == [] and rest == [] and d.get("relaunched") is False, (set_calls, rest, d, msg))
check("用户关开关后工具栏仍开", w._algo_now() is True)
ok, msg, d = w.cmd_algo({"on": True})
check("用户开开关成功且工具栏仍开", ok and w.acc.get("algo_off") is False and w._algo_now() is True, msg)
check("开关文案不承诺重启", "已重启" not in msg and "未改工具栏" in msg, msg)

check("Fleet.shutdown 接受 keep_terminals", "keep_terminals" in inspect.signature(Fleet.shutdown).parameters)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
sys.exit(1 if fails else 0)
