"""v1.5.16：更新/登录/重连不重启 MT5，不擅自开关算法。
python tests/test_panel1516.py
"""
import inspect
import queue
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fleet import __version__
from fleet import terminal as T
from fleet.manager import Fleet
from fleet.worker import Worker

fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {info}" if info else ""))
    if not cond:
        fails.append(name)


check("版本 1.5.16", __version__ == "1.5.16", __version__)

notes = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
readme = (ROOT / "README.md").read_text(encoding="utf-8")
worker = (ROOT / "fleet" / "worker.py").read_text(encoding="utf-8")
terminal = (ROOT / "fleet" / "terminal.py").read_text(encoding="utf-8")
mgr = (ROOT / "fleet" / "manager.py").read_text(encoding="utf-8")
app = (ROOT / "app.py").read_text(encoding="utf-8")
html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
aj = (ROOT / "static" / "algo.js").read_text(encoding="utf-8")
appjs = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
qj = (ROOT / "static" / "quick.js").read_text(encoding="utf-8")
desk = (ROOT / "static" / "desk.js").read_text(encoding="utf-8")
pend = (ROOT / "static" / "pending.js").read_text(encoding="utf-8")

check("更新说明 v1.5.16", "## v1.5.16" in notes and "不重启" in notes and "关着保持关" in notes and "开着保持开" in notes)
check("README 本次更新 1.5.16", "v1.5.16" in readme.split("上次更新")[0] and "不改算法" in readme.split("上次更新")[0])

# 源码门禁
pref = worker[worker.find("def apply_algo_pref"):worker.find("def cmd_algo")]
trade = worker[worker.find("def with_algo_for_trade"):worker.find("def _ea_already_in_state")]
algo_fn = worker[worker.find("def cmd_algo"):worker.find("def check_algo")]
live_fn = worker[worker.find("def apply_ea_live"):worker.find("def apply_algo_pref")]
check("apply_algo_pref 默认路径不调用 ensure_toolbar / set_algo",
      "ensure_toolbar_algo_on" not in pref.split("if deploy:")[0] and "set_algo(" not in pref.split("if deploy:")[0])
check("apply_algo_pref 不再 may_relaunch=True", "may_relaunch=True" not in pref)
check("apply_ea_live 不再重启终端", "self._restart" not in live_fn and "self._relaunch" not in live_fn and "已重启终端并" not in live_fn)
check("with_algo 不调用 ensure / set_algo", "ensure_toolbar_algo_on" not in trade and "set_algo(" not in trade)
check("with_algo 工具栏关时有中文说明", "不会擅自打开或关闭" in trade)
check("cmd_algo 不点工具栏、不重启", "ensure_toolbar_algo_on" not in algo_fn and "may_relaunch=True" not in algo_fn and "已重启终端" not in algo_fn)
check("worker 生产路径无 set_algo(False)", "set_algo(False)" not in worker)
check("shutdown 支持 keep_terminals", "def shutdown(self, keep_terminals: bool = False)" in mgr)
check("更新退出设置 keep_terminals", "_keep_terminals_on_shutdown" in app and "keep_terminals=keep" in app)
check("启动配置可省略 Enabled", "algo: bool | None" in terminal and "def read_saved_toolbar_algo" in terminal)
check("登录步骤是保持算法状态", "保持算法状态" in mgr)
check("UI 承诺不干预", "不干预" in html and "不会擅自" in html)
for name, src in (("algo.js", aj), ("quick.js", qj), ("desk.js", desk), ("app.js", appjs), ("index.html", html), ("pending.js", pend)):
    check(f"{name} 不再写短暂打开", "短暂打开" not in src)
    check(f"{name} 不承诺登录自动开工具栏", "工具栏灯会为面板接口保持开" not in src and "工具栏灯为面板接口保持开" not in src)

# ini / 读取保存的工具栏
check("algo=None 不写 Enabled", "Enabled=" not in "\n".join(T._ini_lines(None, None, False, algo=None)))
td = Path(tempfile.mkdtemp(prefix="fleet1516cfg_"))
try:
    cfg = td / "Config"
    cfg.mkdir()
    (cfg / "common.ini").write_text("[Charts]\nProfileLast=Default\n[Experts]\nEnabled=0\nAllowLiveTrading=1\n", encoding="utf-16")
    exe = str(td / "terminal64.exe")
    (td / "terminal64.exe").write_bytes(b"x")
    check("读到已保存的工具栏关闭", T.read_saved_toolbar_algo(exe) is False)
    (cfg / "common.ini").write_text("[Experts]\nEnabled=1\n", encoding="utf-16")
    check("读到已保存的工具栏开启", T.read_saved_toolbar_algo(exe) is True)
finally:
    import shutil
    shutil.rmtree(td, ignore_errors=True)


def make_worker(password="mock-pass", algo_off=False, **acc):
    a = {"id": "x", "login": "123456", "server": "S", "terminal_path": "/tmp/x/terminal64.exe", "algo_off": algo_off, **acc}
    w = Worker(a, password, {"auto_enable_algo": False}, queue.Queue(), queue.Queue(), True, "/tmp/fleet1516w")
    return w


# 登录：原先开保持开，原先关保持关；不 set_algo、不重启
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

# 已在线再点登录也不改
w = make_worker(password="noalgo", algo_off=True)
ok, msg = w.connect()
assert ok, msg
set_calls = []
w.set_algo = lambda on: (set_calls.append(on) or (False, "should-not", {}))
w._restart = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("restart-called"))
ok, msg = True, "已在线"
# 走 handle connect 的 already-online 分支
class P(dict):
    pass
w.link = "online"
w._explicit = True
_, note = w.apply_algo_pref(w.snapshot())
check("已在线再登录：不 set_algo", set_calls == [], set_calls)
check("已在线再登录：工具栏仍关、algo_off 仍真", w._algo_now() is False and w.acc.get("algo_off") is True)
check("已在线再登录说明保持关闭", "保持关闭" in (note or ""), note)

# 交易：工具栏已开不点灯；工具栏关则失败说明，不点开
w = make_worker(password="mock-pass", algo_off=True)
ok, msg = w.connect()
assert ok and w._algo_now() is True
set_calls = []
w.set_algo = lambda on: (set_calls.append(on) or (False, "no", {}))
w._restart = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("restart-called"))
ok, msg, d = w.with_algo_for_trade(w.cmd_order, {
    "symbol": "XAUUSD", "kind": "buy", "lots": 0.01, "scale_mode": "fixed",
    "auto_symbol": True, "deviation": 20, "magic": 880000, "max_lots": 10,
})
check("工具栏已开：下单成功且不 set_algo", ok and set_calls == [], (ok, msg, set_calls))
check("下单后状态不变", w._algo_now() is True and w.acc.get("algo_off") is True)

w2 = make_worker(password="noalgo", algo_off=True)
ok, msg = w2.connect()
assert ok and w2._algo_now() is False
set_calls = []
w2.set_algo = lambda on: (set_calls.append(on) or (True, "opened", {"changed": True, "trade_allowed": True}))
ok, msg, d = w2.with_algo_for_trade(w2.cmd_order, {
    "symbol": "XAUUSD", "kind": "buy", "lots": 0.01, "scale_mode": "fixed",
    "auto_symbol": True, "deviation": 20, "magic": 880000, "max_lots": 10,
})
check("工具栏已关：下单失败且不偷偷打开", (not ok) and set_calls == [] and w2._algo_now() is False, (ok, msg, set_calls, w2._algo_now()))
check("工具栏已关：中文说明让用户手动点", "工具栏" in msg and "关闭" in msg and "不会擅自" in msg, msg)

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

# shutdown(keep_terminals=True) 源码路径存在；签名可调用
check("Fleet.shutdown 接受 keep_terminals", "keep_terminals" in inspect.signature(Fleet.shutdown).parameters)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
sys.exit(1 if fails else 0)
