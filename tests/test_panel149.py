"""v1.4.9：手机选账户底部遮罩 + 静态资源版本号防缓存。
python tests/test_panel149.py
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

check("版本 1.4.9", __version__ == "1.4.9", __version__)

css = (ROOT / "static" / "quick.css").read_text(encoding="utf-8")
desk = (ROOT / "static" / "desk.js").read_text(encoding="utf-8")
aj = (ROOT / "static" / "algo.js").read_text(encoding="utf-8")
qj = (ROOT / "static" / "quick.js").read_text(encoding="utf-8")
html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
server = (ROOT / "fleet" / "server.py").read_text(encoding="utf-8")
notes = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")

check("有遮罩样式 fleet-pick-mask", "fleet-pick-mask" in css and "z-index:200" in css)
check("有底部 sheet 样式", "fleet-sheet" in css and "safe-area-inset-bottom" in css and "75vh" in css)
check("FleetMobileSheet 助手", "window.FleetMobileSheet" in desk and "isMobile" in desk and "fleetPickMask" in desk)
check("algo 用 sheet + 防误触", "syncSheet" in aj and "openedAt" in aj and "350" in aj and "FleetMobileSheet" in aj)
check("quick 用 sheet + 防误触", "syncSheet" in qj and "openedAt" in qj and "350" in qj and "FleetMobileSheet" in qj)
check("index 静态资源带 __FLEET_VER__", html.count("?v=__FLEET_VER__") >= 7, str(html.count("?v=__FLEET_VER__")))
check("server 注入版本号", 'replace("__FLEET_VER__", __version__)' in server)
check("更新说明", "## v1.4.9" in notes and "遮罩" in notes and "缓存" in notes)

# 模拟 index 替换逻辑
sample = 'href="/static/app.css?v=__FLEET_VER__"'
out = sample.replace("__FLEET_VER__", __version__)
check("版本注入结果含 1.4.9", "v=1.4.9" in out, out)

# 兼容：1.4.8 钉底仍在（兜底）
check("保留 al-panel 钉底兜底", ".al-panel .pd-pop" in css and "bottom:" in css)

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    sys.exit(1)
print("ALL PASS")
