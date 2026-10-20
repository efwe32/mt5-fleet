"""v1.5.5：收益台头像挪位 + 点进单账号详情（平仓/建仓）。
python tests/test_panel155.py
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

check("版本 1.5.5", __version__ == "1.5.5", __version__)

notes = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
readme = (ROOT / "README.md").read_text(encoding="utf-8")
html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
desk = (ROOT / "static" / "desk.js").read_text(encoding="utf-8")
css = (ROOT / "static" / "desk.css").read_text(encoding="utf-8")
app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

check("更新说明 v1.5.5", "## v1.5.5" in notes and "头像" in notes and ("平仓" in notes or "建仓" in notes))
check("README 本次更新", "v1.5.5" in readme and "头像" in readme)

# 头像在 metrics 之前
i_avs = html.find('id="pdAvs"')
i_met = html.find('class="pd-card pd-metrics"')
i_head = html.find('class="pd-card pd-head"')
check("HTML 仅一个 pdAvs", html.count('id="pdAvs"') == 1)
check("HTML 头像在 head 与 metrics 之间", i_head < i_avs < i_met and i_avs > 0, f"head={i_head} avs={i_avs} met={i_met}")
# 经典统计前不应再有头像块（已挪走）
classic = html.find('id="pdClassic"')
between = html[i_met:classic] if classic > i_met else ""
check("metrics 后无第二处头像", 'id="pdAvs"' not in between)

check("desk 有 openAvDetail", "function openAvDetail" in desk)
check("desk 有 renderAvDetail", "function renderAvDetail" in desk)
check("desk 头像可点", "openAvDetail(el.dataset.av)" in desk)
check("desk 建仓开多开空", "pdAvBuy" in desk and "pdAvSell" in desk and "placeAvOrder" in desk)
check("desk 平仓按钮", "data-avclose" in desk)
check("desk.css 详情样式", ".pd-avmask" in css and ".pd-avsheet" in css)
check("FleetTrade 封装", "window.FleetTrade" in app and "placeOrderFor" in app)
check("单账户下单", "单账户下单" in app and "fleet avatar" in desk)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
sys.exit(1 if fails else 0)
