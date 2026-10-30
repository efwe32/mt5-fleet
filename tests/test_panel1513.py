"""v1.5.13：浮赢/浮亏、一键锁仓、平仓防 EA 补单。
python tests/test_panel1513.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fleet import __version__

fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {info}" if info else ""))
    if not cond:
        fails.append(name)


check("版本 1.5.13", __version__ == "1.5.13", __version__)

notes = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
readme = (ROOT / "README.md").read_text(encoding="utf-8")
html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
qj = (ROOT / "static" / "quick.js").read_text(encoding="utf-8")
desk = (ROOT / "static" / "desk.js").read_text(encoding="utf-8")
css = (ROOT / "static" / "quick.css").read_text(encoding="utf-8")
worker = (ROOT / "fleet" / "worker.py").read_text(encoding="utf-8")
manager = (ROOT / "fleet" / "manager.py").read_text(encoding="utf-8")
terminal = (ROOT / "fleet" / "terminal.py").read_text(encoding="utf-8")

check("更新说明 v1.5.13", "## v1.5.13" in notes and "一键锁仓" in notes and "补单" in notes)
check("README 本次更新 1.5.13", "v1.5.13" in readme and "一键锁仓" in readme)

# 浮赢/浮亏 UI
check("qSide 浮赢浮亏列", "浮赢" in qj and "浮亏" in qj and "floatParts" in qj)
check("qSide 表头含浮赢浮亏", "<th>浮赢</th>" in qj and "<th>浮亏</th>" in qj)
check("manager floatWin/floatLoss", "floatWin" in manager and "floatLoss" in manager)
check("头像详情浮赢浮亏", 'data-avm="fwin"' in desk and 'data-avm="floss"' in desk and ">浮赢<" in desk and ">浮亏<" in desk)
check("patchAvLive 更新浮赢浮亏", "[data-avm=fwin]" in desk and "[data-avm=floss]" in desk)

# 一键锁仓
check("HTML 一键锁仓按钮", 'data-q="lock"' in html and "一键锁仓" in html)
check("quick.js ACT lock", 'lock: "一键锁仓"' in qj and 'action === "lock"' in qj)
check("quick.css .qb-lock", ".qb-lock" in css)
check("QUICK_ACTIONS lock", '"lock": ("一键锁仓"' in manager)
check("manager _quick_run lock", 'cmd == "lock"' in manager and "fleet lock" in manager)
check("worker cmd_lock", "def cmd_lock" in worker and "fleet lock" in worker)
check("worker handlers lock", '"lock": self.cmd_lock' in worker)

# 平仓防 EA：guard_experts
check("with_algo_for_trade 有 guard_experts", "def with_algo_for_trade" in worker and "guard_experts" in worker)
check("close/nuke/lock 走 guard_experts=True",
      'name in ("close", "modify", "cancel_orders", "nuke", "lock")' in worker
      and "guard_experts=True" in worker)
check("order/pair 不护 EA",
      'name in ("order", "order_pair")' in worker
      and "guard_experts=False" in worker)
check("锁仓确认提及短暂开算法", "短暂打开" in qj and 'action === "lock"' in qj)
check("terminal freeze/restore", "def freeze_chart_experts" in terminal and "def restore_chart_experts" in terminal)
check("worker 调用 freeze/restore", "_experts_freeze_for_trade" in worker and "_experts_restore_after_trade" in worker)
check("更新说明写明平仓防补单", "卸下" in notes and ("图表" in notes or "EA" in notes))

# busy 防双击仍在
check("Q.busy 防双击", "if (Q.busy)" in qj)


desk = (ROOT / "static" / "desk.js").read_text(encoding="utf-8")
css = (ROOT / "static" / "desk.css").read_text(encoding="utf-8")
check("头像详情一键锁仓按钮", 'id="pdAvLock"' in desk and "一键锁仓" in desk)
check("lockAvSymbol 实现", "async function lockAvSymbol" in desk and '/api/quick/action' in desk and 'action: "lock"' in desk)
check("头像锁仓确认提及短暂开算法", "短暂打开" in desk and "lockAvSymbol" in desk)
check("头像锁仓按钮样式", ".pd-avbtn.lock" in css)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
sys.exit(1 if fails else 0)
