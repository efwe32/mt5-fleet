"""v1.5.15：面板算法只管图表 EA；关着也能开平仓，且不开关工具栏。
python tests/test_panel1515.py
"""
import inspect
import queue
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fleet import __version__
from fleet import terminal as T
from fleet.worker import Worker

fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {info}" if info else ""))
    if not cond:
        fails.append(name)


check("版本 1.5.15", __version__ == "1.5.15", __version__)

notes = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
readme = (ROOT / "README.md").read_text(encoding="utf-8")
worker = (ROOT / "fleet" / "worker.py").read_text(encoding="utf-8")
terminal = (ROOT / "fleet" / "terminal.py").read_text(encoding="utf-8")
mgr = (ROOT / "fleet" / "manager.py").read_text(encoding="utf-8")
html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
aj = (ROOT / "static" / "algo.js").read_text(encoding="utf-8")
app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
qj = (ROOT / "static" / "quick.js").read_text(encoding="utf-8")
desk = (ROOT / "static" / "desk.js").read_text(encoding="utf-8")
css = (ROOT / "static" / "desk.css").read_text(encoding="utf-8")
pend = (ROOT / "static" / "pending.js").read_text(encoding="utf-8")

check("更新说明 v1.5.15", "## v1.5.15" in notes and "图表 EA" in notes and "工具栏" in notes and "头像" in notes)
check("README 本次更新 1.5.15", "v1.5.15" in readme and "图表 EA" in readme)

# 源码门禁：交易路径不得关工具栏、不得为交易重启
check("有 ensure_toolbar_algo_on", "def ensure_toolbar_algo_on" in worker)
check("有 apply_ea_live", "def apply_ea_live" in worker)
check("terminal 有 set_chart_experts_live", "def set_chart_experts_live" in terminal)
check("worker 无 set_algo(False)", "set_algo(False)" not in worker)
check("cmd_lock 不关工具栏", "set_algo(False)" not in worker[worker.find("def cmd_lock"):worker.find("def _match")])
check("默认 apply_algo_pref 不关工具栏", "set_algo(False)" not in worker[worker.find("def apply_algo_pref"):worker.find("def cmd_algo")])
check("with_algo 无 finally 关闭", "set_algo(False)" not in worker[worker.find("def with_algo_for_trade"):worker.find("def _ea_already_in_state")])
check("cmd_algo 关闭文案", "EA 已禁止自动交易" in worker and "工具栏为面板保持开" in worker and "开平仓仍可用" in worker)

# UI：不再说短暂打开；开关不绑 trade_allowed
for name, src in (("algo.js", aj), ("quick.js", qj), ("desk.js", desk), ("app.js", app), ("index.html", html), ("pending.js", pend)):
    check(f"{name} 不再写短暂打开", "短暂打开" not in src)
check("algo.js 开关函数不读 trade_allowed", "trade_allowed" not in aj[aj.find("function algoOf"):aj.find("function pickLabel")])
check("algo.js 绑 ea_trading/algo_off", "ea_trading" in aj and "algo_off" in aj)
check("app.js termTag 绑 eaTradingOf", "function eaTradingOf" in app and "图表 EA" in app)
check("index 说明不管工具栏灯", "不是工具栏那颗灯" in html and "不等于 EA 可下单" in html)
check("设置文案是允许图表 EA", "登录后默认允许图表 EA 自动交易" in html)
check("头像有本账户算法开关", "pdAvAlgoOn" in desk and "pdAvAlgoOff" in desk and "function toggleAvAlgo" in desk)
check("头像开关绑 eaTradingOf", "function eaTradingOf" in desk and "toggleAvAlgo" in desk)
check("头像 structKey 含算法状态", "eaTradingOf(a)" in desk and "algo_off" in desk)
check("头像开关样式", ".pd-avalgo" in css and ".pd-avbtn.algo-on" in css)
check("头像确认不含短暂打开", "短暂打开" not in desk)

# expertmode 单元
chr_dir = Path("/tmp/fleet1515charts")
# build a fake terminal layout: MQL5/Profiles/Charts/Default/chart01.chr
import shutil, tempfile
td = Path(tempfile.mkdtemp(prefix="fleet1515t_"))
try:
    charts = td / "MQL5" / "Profiles" / "Charts" / "Default"
    charts.mkdir(parents=True)
    # also a common.ini so profile_name works, or default Default
    body = (
        "<chart>\nsymbol=XAUUSD\n"
        "<expert>\nname=Foo\npath=Experts\\\\Fleet\\\\Foo.ex5\nexpertmode=5\n"
        "<inputs>\nLots=0.01\n</inputs>\n</expert>\n"
        "<window>\n<indicator>\nname=Main\nexpertmode=0\n</indicator>\n</window>\n</chart>\n"
    )
    f = charts / "chart01.chr"
    f.write_text(body, encoding="utf-8")
    exe = str(td / "terminal64.exe")
    (td / "terminal64.exe").write_bytes(b"x")
    live, muted = T.chart_experts_live_counts(exe)
    check("初始 expertmode=5 计为允许", (live, muted) == (1, 0), (live, muted))
    check("需要静音", T.chart_experts_need_live_change(exe, False) is True)
    r = T.set_chart_experts_live(exe, False)
    check("静音改了 1 个 EA", r["changed_experts"] == 1 and r["changed_files"] == 1, r)
    live, muted = T.chart_experts_live_counts(exe)
    check("静音后 expertmode 清 bit0（4）", (live, muted) == (0, 1), (live, muted))
    txt = f.read_text(encoding="utf-8")
    check("指标 expertmode=0 未改、EA 变为 4", "expertmode=4" in txt and "name=Main\nexpertmode=0" in txt, txt)
    r2 = T.set_chart_experts_live(exe, False)
    check("重复静音跳过", r2["changed_experts"] == 0, r2)
    r3 = T.set_chart_experts_live(exe, True)
    check("恢复 live 位", r3["changed_experts"] == 1, r3)
    live, muted = T.chart_experts_live_counts(exe)
    check("恢复后为 5", (live, muted) == (1, 0), (live, muted))
    check("恢复后文本 expertmode=5", "expertmode=5" in f.read_text(encoding="utf-8"))
finally:
    shutil.rmtree(td, ignore_errors=True)

# runtime：algo_off 时下单/平仓/锁仓不点已开的工具栏，不重启
set_calls = []

def make_worker(algo_on=True, algo_off=True, password="mock-pass"):
    a = {"id": "x", "login": "123456", "server": "S", "terminal_path": "/tmp/x/terminal64.exe", "algo_off": algo_off}
    w = Worker(a, password, {"auto_enable_algo": False}, queue.Queue(), queue.Queue(), True, "/tmp/fleet1515w")
    ok, msg = w.connect()
    assert ok, msg
    # 登录默认会把工具栏保持/打开；若测试要模拟「已经是开」，保证它是开
    if algo_on and w._algo_now() is not True:
        w.mt5._algo = True
    return w

w = make_worker()
# wrap set_algo / post paths
real_set = w.set_algo
def spy_set(on):
    set_calls.append(on)
    return real_set(on)
w.set_algo = spy_set
w._restart = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("restart-called"))
w._relaunch = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("relaunch-called"))
set_calls.clear()
assert w._algo_now() is True
ok, msg, d = w.with_algo_for_trade(w.cmd_order, {
    "symbol": "XAUUSD", "kind": "buy", "lots": 0.01, "scale_mode": "fixed",
    "auto_symbol": True, "deviation": 20, "magic": 880000, "max_lots": 10,
})
check("algo_off 且工具栏已开：下单成功", ok, msg)
check("下单未调用 set_algo", set_calls == [], set_calls)
check("下单后工具栏仍开、algo_off 仍在", w._algo_now() is True and w.acc.get("algo_off") is True, (w._algo_now(), w.acc.get("algo_off")))

set_calls.clear()
ok, msg, d = w.with_algo_for_trade(w.cmd_close, {"mode": "all", "deviation": 20})
check("平仓未调用 set_algo、未重启", ok and set_calls == [], (ok, msg, set_calls))

# 再开多然后锁仓：不得 set_algo(False)，不得重启
w.with_algo_for_trade(w.cmd_order, {
    "symbol": "XAUUSD", "kind": "buy", "lots": 0.02, "scale_mode": "fixed",
    "auto_symbol": True, "deviation": 20, "magic": 880000, "max_lots": 10,
})
set_calls.clear()
ok, msg, d = w.with_algo_for_trade(w.cmd_lock, {
    "symbol": "XAUUSD", "auto_symbol": True, "deviation": 20, "magic": 880000, "max_lots": 10,
})
check("锁仓成功且未 set_algo", ok and set_calls == [], (ok, msg, set_calls))
check("锁仓后工具栏仍开", w._algo_now() is True)
check("锁仓后 algo_off 为 True", w.acc.get("algo_off") is True)
check("锁仓文案禁止 EA 而非关工具栏", "禁止图表 EA" in msg or "已禁止" in msg, msg)

# 默认 apply_algo_pref：mock-pass 工具栏本来开，不得关掉
w2 = make_worker(password="mock-pass", algo_off=False)
# connect 已经跑过 apply_algo_pref
check("默认登录后 algo_off=True", w2.acc.get("algo_off") is True)
check("默认登录不关工具栏（保持开）", w2._algo_now() is True, w2._algo_now())
check("默认登录静音了 EA（mock 标记）", getattr(w2, "_ea_trade_locked", None) is True)

# noalgo：登录时工具栏是关的，应当打开一次
w3_acc = {"id": "y", "login": "123456", "server": "S", "terminal_path": "/tmp/y/terminal64.exe", "algo_off": True}
w3 = Worker(w3_acc, "noalgo", {"auto_enable_algo": False}, queue.Queue(), queue.Queue(), True, "/tmp/fleet1515w3")
# spy before connect
set3 = []
real3 = Worker.set_algo
def spy3(self, on):
    set3.append(on)
    return real3(self, on)
w3.set_algo = spy3.__get__(w3, Worker)
ok, msg = w3.connect()
check("noalgo 登录成功", ok, msg)
check("工具栏原本关：登录只打开一次", set3 == [True], set3)
check("noalgo 登录后工具栏开、EA 禁止", w3._algo_now() is True and w3.acc.get("algo_off") is True)

# cmd_algo off：不关工具栏
w4 = make_worker()
assert w4._algo_now() is True
set4 = []
real4 = w4.set_algo
w4.set_algo = lambda on: (set4.append(on) or real4(on))
ok, msg, d = w4.cmd_algo({"on": False})
check("cmd_algo 关闭成功", ok, msg)
check("关闭后工具栏仍开", w4._algo_now() is True and (d.get("trade_allowed") in (True, None) or d.get("ea_trading") is False), d)
check("关闭不调用 set_algo(False)", False not in set4, set4)
check("关闭文案提到 EA 禁止和工具栏保持开", "禁止自动交易" in msg and "工具栏" in msg and "开平仓仍可用" in msg, msg)
check("关闭后 algo_off", w4.acc.get("algo_off") is True)

ok, msg, d = w4.cmd_algo({"on": True})
check("cmd_algo 开启成功", ok and w4.acc.get("algo_off") is False, msg)
check("开启后工具栏仍开", w4._algo_now() is True)
check("开启文案允许 EA", "允许自动交易" in msg, msg)

# 工具栏已开时 ensure 不点
w5 = make_worker()
set5 = []
real5 = w5.set_algo
w5.set_algo = lambda on: (set5.append(on) or real5(on))
ok, msg, d = w5.ensure_toolbar_algo_on()
check("工具栏已开：ensure 不调用 set_algo", ok and set5 == [] and d.get("changed") is False, (ok, set5, d))

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
sys.exit(1 if fails else 0)
