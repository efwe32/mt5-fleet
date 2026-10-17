"""v1.4.8：手机远程算法交易开关可选账户。
python tests/test_panel148.py
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

check("版本 ≥1.4.8", tuple(int(x) for x in __version__.split(".")[:3]) >= (1, 4, 8), __version__)
css = (ROOT / "static" / "quick.css").read_text(encoding="utf-8")
js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
aj = (ROOT / "static" / "algo.js").read_text(encoding="utf-8")
check("算法弹层钉在屏幕底部", "bottom:16px" in css and ".al-panel .pd-pop" in css)
check("不再用会飞出视口的 top:auto 单独定位", not re.search(r"\.al-panel \.pd-pop\{[^}]*top:auto;width", css) or "bottom:16px" in css)
check("确认框 z-index 高于弹层", "z-index:100" in (ROOT / "static" / "app.css").read_text(encoding="utf-8"))
check("遮罩用 click 且防误触", "Date.now() - openedAt < 400" in js and 'e.target !== ov' in js)
check("开确认前先收起选账户", "if (A.popOpen)" in aj and "确认开启" in aj)
notes = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
check("更新说明含 1.4.8", "## v1.4.8" in notes and "算法交易" in notes and "手机" in notes)

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    sys.exit(1)
print("ALL PASS")
