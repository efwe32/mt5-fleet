"""v1.5.12：交易页独立「预订单」面板（挂多/挂空 + SL/TP）。
python tests/test_panel1512.py
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


check("版本 1.5.12", __version__ == "1.5.12", __version__)

notes = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
readme = (ROOT / "README.md").read_text(encoding="utf-8")
html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
pend = (ROOT / "static" / "pending.js").read_text(encoding="utf-8")
app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
css = (ROOT / "static" / "quick.css").read_text(encoding="utf-8")

check("更新说明 v1.5.12", "## v1.5.12" in notes and "预订单" in notes)
check("README 本次更新 1.5.12", "v1.5.12" in readme and "预订单" in readme)

check("HTML 有预订单标题", "预订单" in html and 'id="pendPanel"' in html)
check("HTML 挂多/挂空", 'id="pHangLong"' in html and 'id="pHangShort"' in html and ">挂多<" in html and ">挂空<" in html)
check("HTML SL/TP 字段", 'id="pSL"' in html and 'id="pTP"' in html and 'id="pPrice"' in html and 'id="pUnit"' in html)
check("HTML 撤销本品种挂单", "撤销本品种挂单" in html and 'id="pCancelSym"' in html)
check("HTML 引入 pending.js", "pending.js" in html)
check("预订单在快捷交易与持仓列表之间", html.find("qp-wrap") < html.find("pendPanel") < html.find("qPosPanel"))

check("pending.js 自动限价/止损逻辑", "buy_limit" in pend and "buy_stop" in pend and "sell_limit" in pend and "sell_stop" in pend)
check("pending.js pickLongKind 价≤ask", "pickLongKind" in pend and "ask" in pend)
check("pending.js pickShortKind 价≥bid", "pickShortKind" in pend and "bid" in pend)
check("pending.js 无报价回退限价", "warn" in pend and 'kind: "buy_limit"' in pend and 'kind: "sell_limit"' in pend)
check("pending.js magic/comment", "880000" in pend and "fleet pending" in pend)
check("pending.js 确认后 order API", "/api/trade/order" in pend and "/api/trade/preview" in pend)
check("pending.js 撤单 API", "/api/trade/cancel" in pend)
check("pending.js 滚动安全", "_k === structKey" in pend or "structKey" in pend)

check("HTML 预订单独立选择账户", 'id="pPick"' in html and 'id="pPop"' in html and 'id="pPickWrap"' in html)
check("pending.js 独立多选 localStorage", "fleet.pendAccs" in pend and "setSel" in pend)
check("pending.js 默认同步快捷可改", "maybeSyncFromQuick" in pend and "同步快捷" in pend)
check("pending.js hang 用本面板选户", "selAccs()" in pend and "先选择账户" in pend)
check("pending.js 不绑死 Quick.ids 下单", "ids: idList" in pend or "ids: ids()" in pend)

check("app.js pendingOrder", "pendingOrder" in app and "FleetTrade" in app)
check("app.js FleetPending hook", "FleetPending" in app)
check("quick.css .pend-panel", ".pend-panel{" in css.replace(" ", "") or ".pend-panel {" in css)

# 市价一键按钮仍在
check("市价一键开多仍在", 'data-q="buy"' in html and "一键开多" in html)
check("市价一键开空仍在", 'data-q="sell"' in html and "一键开空" in html)

# 单元：limit/stop 选择（在 Node 外用简单正则断言逻辑存在）
check("挂多逻辑注释或比较", re.search(r"price\s*<=\s*.*ask|Number\(price\)\s*<=\s*Number\(q\.ask\)", pend) is not None)
check("挂空逻辑注释或比较", re.search(r"price\s*>=\s*.*bid|Number\(price\)\s*>=\s*Number\(q\.bid\)", pend) is not None)


desk = (ROOT / "static" / "desk.js").read_text(encoding="utf-8")
dcss = (ROOT / "static" / "desk.css").read_text(encoding="utf-8")
check("头像详情预订单 HTML", "预订单（仅本账户）" in desk and "pdAvHangLong" in desk and "pdAvHangShort" in desk)
check("头像详情挂单价/SL/TP", "pdAvPendPrice" in desk and "pdAvPendSL" in desk and "pdAvPendTP" in desk and "pdAvPendUnit" in desk)
check("头像详情 placeAvPending", "placeAvPending" in desk and "fleet pending" in desk)
check("头像详情仍保留开多开空", 'id="pdAvBuy"' in desk and 'id="pdAvSell"' in desk and "一键全部清仓" in desk)
check("desk.css .pd-avpend", ".pd-avpend" in dcss)
check("更新说明含头像预订单", "头像" in notes and "预订单" in notes)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
sys.exit(1 if fails else 0)
