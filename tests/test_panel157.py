"""v1.5.6/1.5.7：手机持仓表横滑不被轮询打回。
python tests/test_panel157.py
"""
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

check("版本 >= 1.5.6", tuple(int(x) for x in __version__.split(".")) >= (1, 5, 6), __version__)
check("版本 1.5.7", __version__ == "1.5.7", __version__)

notes = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
readme = (ROOT / "README.md").read_text(encoding="utf-8")
quick = (ROOT / "static" / "quick.js").read_text(encoding="utf-8")
desk = (ROOT / "static" / "desk.js").read_text(encoding="utf-8")
app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
algo = (ROOT / "static" / "algo.js").read_text(encoding="utf-8")
qcss = (ROOT / "static" / "quick.css").read_text(encoding="utf-8")
acss = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
dcss = (ROOT / "static" / "desk.css").read_text(encoding="utf-8")

check("更新说明 v1.5.7", "## v1.5.7" in notes and "横滑" in notes)
check("README 本次更新", "v1.5.7" in readme and "持仓" in readme)

check("quick 结构键", "structKey" in quick)
check("quick 就地更新", 'data-live="px"' in quick)
check("quick 恢复滚动", "restoreTwScroll" in quick and "saveTwScroll" in quick)
check("quick 票号稳定键", "localeCompare" in quick and "numeric: true" in quick)

check("desk 就地更新", "patchAvLive" in desk and 'data-live="px"' in desk)
check("desk 结构键", "_avKey" in desk)
check("desk 恢复滚动", "keptTw" in desk and "scrollLeft" in desk)

check("app withTableScroll", "withTableScroll" in app and 'withTableScroll("#posList"' in app)
check("algo 保留滚动", "scrollLeft" in algo and "al-tw" in algo)

check("CSS q-pos-tw touch", "-webkit-overflow-scrolling:touch" in qcss and "overscroll-behavior:contain" in qcss)
check("CSS tablewrap touch", "touch-action:pan-x pan-y" in acss)
check("CSS pd-avtw touch", "overscroll-behavior:contain" in dcss and ".pd-avtw" in dcss)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
sys.exit(1 if fails else 0)
