"""v1.3.3 原浮亏警报测试：v1.4.5 已取消警报功能，本文件改为确认模块已移除。
python tests/test_panel133.py
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

fails = []

def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {info}" if info else ""))
    if not cond:
        fails.append(name)

check("alerts 模块已删除", not (ROOT / "fleet" / "alerts.py").exists())
js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
check("分发结果弹出仍 3 分钟", "const POPUP_MS = 180000" in js)
check("无浮亏警报前端", "renderAlerts" not in js and "alert-view" not in js)

if fails:
    print("FAILED:", ", ".join(fails))
    sys.exit(1)
print("ALL PASS (alerts removed; see tests/test_panel145.py)")
