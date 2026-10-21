"""v1.5.10：头像详情一键清仓（完整包）一键全部清仓 / 平全部空 / 平全部多。
python tests/test_panel159.py
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

check("版本 1.5.10", __version__ == "1.5.10", __version__)

notes = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
readme = (ROOT / "README.md").read_text(encoding="utf-8")
app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
desk = (ROOT / "static" / "desk.js").read_text(encoding="utf-8")
dcss = (ROOT / "static" / "desk.css").read_text(encoding="utf-8")

check("更新说明 v1.5.10", "## v1.5.10" in notes and ("清仓" in notes or "平全部" in notes))
check("README 本次更新", "v1.5.10" in readme and ("清仓" in readme or "平全部" in readme))

check("FleetTrade.closeByModes", "function closeByModes" in app and "closeByModes" in app.split("window.FleetTrade")[1][:200])
check("closeByModes 单户 ids", 'ids: [accId]' in app and 'filter: { mode }' in app)
check("closeByModes 模式", '"all"' in app and '"long"' in app and '"short"' in app)

check("desk 批量按钮文案", "一键全部清仓" in desk and "平全部空" in desk and "平全部多" in desk)
check("desk data-avbatch", 'data-avbatch="all"' in desk and 'data-avbatch="short"' in desk and 'data-avbatch="long"' in desk)
check("desk 调用 closeByModes", "FT.closeByModes" in desk)
check("desk canLong/canShort", "canLong" in desk and "canShort" in desk and "canAll" in desk)
check("desk patchAvLive 批量禁用", 'data-avbatch' in desk and "nBuy" in desk and "nSell" in desk)
check("desk 仍用 structKey/patchAvLive", "structKey" in desk and "patchAvLive" in desk and "_avKey" in desk)

check("CSS pd-avbatch", ".pd-avbatch" in dcss and ".pd-avsec-row" in dcss)
check("CSS 手机可点", "min-height:46px" in dcss and "data-avbatch=all" in dcss)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
sys.exit(1 if fails else 0)
