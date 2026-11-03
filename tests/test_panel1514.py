"""v1.5.14：交易不重启 MT5、头像止盈点位、策略只删库。
python tests/test_panel1514.py
"""
import queue
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fleet import __version__
from fleet.worker import Worker

fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {info}" if info else ""))
    if not cond:
        fails.append(name)


check("版本 1.5.14", __version__ == "1.5.14", __version__)

notes = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
readme = (ROOT / "README.md").read_text(encoding="utf-8")
desk = (ROOT / "static" / "desk.js").read_text(encoding="utf-8")
css = (ROOT / "static" / "desk.css").read_text(encoding="utf-8")
qj = (ROOT / "static" / "quick.js").read_text(encoding="utf-8")
app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
worker = (ROOT / "fleet" / "worker.py").read_text(encoding="utf-8")
mgr = (ROOT / "fleet" / "manager.py").read_text(encoding="utf-8")
srv = (ROOT / "fleet" / "server.py").read_text(encoding="utf-8")

check("更新说明 v1.5.14", "## v1.5.14" in notes and "不重启" in notes and "止盈点位" in notes and "只删" in notes)
check("README 本次更新 1.5.14", "v1.5.14" in readme and "不重启" in readme)

# 1) 交易路径绝不重启
check("已移除 _experts_freeze_for_trade", "_experts_freeze_for_trade" not in worker)
check("已移除 _experts_restore_after_trade", "_experts_restore_after_trade" not in worker)
check("with_algo 文档写明不重启", "绝不因交易而重启" in worker or "绝不因交易而重启" in worker)
check("dispatch 交易统一 with_algo guard=False",
      'name in ("close", "modify", "cancel_orders", "nuke", "lock", "order", "order_pair")' in worker
      and "guard_experts=False" in worker
      and "guard_experts=True" not in worker)
# _restart/_relaunch 仅应出现在 deploy/stop 路径附近，不应在 with_algo 内
algo_fn = worker[worker.find("def with_algo_for_trade"):worker.find("def apply_algo_pref")]
check("with_algo 体内无 _restart/_relaunch", "_restart" not in algo_fn and "_relaunch" not in algo_fn)
check("锁仓文案不再写卸 EA 重启", "暂时卸下图表 EA" not in desk and "暂时卸下图表 EA" not in qj)
check("锁仓文案写明不重启", "不重启" in desk and "不重启" in qj)

# runtime spy：algo 关着做 lock/close，不得调用 restart/relaunch
def make_worker():
    a = {"id": "x", "login": "123456", "server": "S", "terminal_path": "/tmp/x/terminal64.exe", "algo_off": True}
    w = Worker(a, "mock-pass", {"auto_enable_algo": False}, queue.Queue(), queue.Queue(), True, "/tmp/fleet1514w")
    ok, _ = w.connect()
    assert ok and w._algo_now() is False
    return w

w = make_worker()
calls = {"restart": 0, "relaunch": 0}
w._restart = lambda *a, **k: (calls.__setitem__("restart", calls["restart"] + 1) or (False, "should-not-run"))
w._relaunch = lambda *a, **k: (calls.__setitem__("relaunch", calls["relaunch"] + 1) or (False, "should-not-run"))

# seed a position for close/lock
mt5 = w.mt5
# mock positions via existing API if any
ok, msg, d = w.with_algo_for_trade(w.cmd_order, {
    "symbol": "XAUUSD", "kind": "buy", "lots": 0.01, "scale_mode": "fixed",
    "auto_symbol": True, "deviation": 20, "magic": 880000, "max_lots": 10,
}, guard_experts=True)  # even if caller passes True, must not restart
check("order(guard=True) 成功且未重启", ok and calls["restart"] == 0 and calls["relaunch"] == 0, (ok, msg, calls))
check("order 后算法关回", w._algo_now() is False)

calls["restart"] = calls["relaunch"] = 0
# close all
ok, msg, d = w.with_algo_for_trade(w.cmd_close, {"mode": "all", "deviation": 20}, guard_experts=True)
check("close(guard=True) 未调用 restart/relaunch", calls["restart"] == 0 and calls["relaunch"] == 0, (ok, msg, calls))

w2 = make_worker()
calls2 = {"restart": 0, "relaunch": 0}
w2._restart = lambda *a, **k: (calls2.__setitem__("restart", calls2["restart"] + 1) or (False, "no"))
w2._relaunch = lambda *a, **k: (calls2.__setitem__("relaunch", calls2["relaunch"] + 1) or (False, "no"))
# open then lock
w2.with_algo_for_trade(w2.cmd_order, {
    "symbol": "XAUUSD", "kind": "buy", "lots": 0.02, "scale_mode": "fixed",
    "auto_symbol": True, "deviation": 20, "magic": 880000, "max_lots": 10,
})
calls2["restart"] = calls2["relaunch"] = 0
ok, msg, d = w2.with_algo_for_trade(w2.cmd_lock, {
    "symbol": "XAUUSD", "auto_symbol": True, "deviation": 20, "magic": 880000, "max_lots": 10,
}, guard_experts=True)
check("lock(guard=True) 未调用 restart/relaunch", calls2["restart"] == 0 and calls2["relaunch"] == 0, (ok, msg, calls2))

# handle() 路径：ensure trade cmds go through with_algo without restart
w3 = make_worker()
spy = {"n": 0}
real = w3.with_algo_for_trade
def wrap(fn, params, guard_experts=False):
    spy["n"] += 1
    spy["guard"] = guard_experts
    # wrap restart
    w3._restart = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("restart-called"))
    w3._relaunch = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("relaunch-called"))
    return real(fn, params, guard_experts=guard_experts)
w3.with_algo_for_trade = wrap
# use handle via cmd queue style - call the internal branch by invoking handlers
ok, msg, detail = w3.with_algo_for_trade(w3.cmd_order, {
    "symbol": "XAUUSD", "kind": "sell", "lots": 0.01, "scale_mode": "fixed",
    "auto_symbol": True, "deviation": 20, "magic": 1, "max_lots": 10,
}, guard_experts=False)
check("handle 风格下单无重启异常", ok, msg)

# 2) 头像持仓列
check("持仓表含止损止盈止盈点位列", "<th>止损</th>" in desk and "<th>止盈</th>" in desk and "<th>止盈点位</th>" in desk)
check("盈亏列仍在", "<th>盈亏</th>" in desk and 'data-live="pl"' in desk)
check("tpPointsOf / fmtPriceLv / 队列汇总", "function tpPointsOf" in desk and "function fmtPriceLv" in desk and "posQueueSummary" in desk and 'data-av="posSum"' in desk)
check("patchAvLive 更新 sl/tp/tpPts", 'data-live=sl' in desk and 'data-live=tp' in desk and 'data-live=tpPts' in desk)
check("队列汇总样式", ".pd-avpossum" in css)
check("snapshot 带 digits/point", '"digits": digits, "point": point' in worker)

# 3) delete_library 只删库+台账
dl = mgr[mgr.find("def delete_library"):mgr.find("def shutdown")]
check("delete_library 不调用 stop_strategies", "stop_strategies" not in dl)
check("delete_library 不重启字样", "不会重启终端" in dl or "不重启" in dl)
check("确认框文案只删库", "只删终端 EA 库" in app and "也不会重启终端" in app)
check("server 文档不卸图表", "不卸图表 EA" in srv)
check("不再出现删除前停止策略", "删除库文件前停止策略" not in mgr)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
sys.exit(1 if fails else 0)
