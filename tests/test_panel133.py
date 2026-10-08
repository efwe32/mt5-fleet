"""v1.3.3：浮亏警报、首页弹出 3 分钟、金十 4 星过滤。
可单独跑（会自己拉起 --mock）：python tests/test_panel133.py
"""
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet.alerts import scan, POPUP_MS  # noqa: E402
from fleet.calendar import filter_stars, upcoming  # noqa: E402

fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {info}" if info else ""))
    if not cond:
        fails.append(name)


rows = [
    {"id": "1", "star": 5, "pub_time": "2026-10-08 21:30:00", "country": "美国", "name": "非农", "previous": "10", "consensus": "12", "actual": "", "unit": "万人"},
    {"id": "2", "star": 4, "pub_time": "2026-10-09 02:00:00", "country": "英国", "name": "利率", "previous": "4", "consensus": "4", "actual": None},
    {"id": "3", "star": 3, "pub_time": "2026-10-08 15:00:00", "country": "日本", "name": "三星数据", "previous": "1"},
    {"id": "4", "star": 5, "country": "美国", "name": "没有时间"},
]
kept = filter_stars(rows)
check("日历只留 4 星及以上且有时间", [r["title"] for r in kept] == ["非农", "利率"] and all(r["star"] >= 4 for r in kept), [r["title"] for r in kept])
check("前值预期公布", kept[0]["previous"] == "10" and kept[0]["forecast"] == "12" and kept[0]["actual"] == "")
win = upcoming(kept, datetime(2026, 10, 8, 12, 0), 1)
check("只留今天和明天", [r["title"] for r in win] == ["非农", "利率"], [r["title"] for r in win])

fresh, armed = scan([{"id": "a", "alias": "甲", "login": "1", "floating": -2999, "currency": "USD"}], 3000, {})
check("没到 3000 不响", fresh == [] and armed == {})
fresh, armed = scan([{"id": "a", "alias": "甲", "login": "1", "floating": -3000, "currency": "USD"}], 3000, {})
check("到达 3000 响一次", len(fresh) == 1 and fresh[0]["floating"] == -3000 and armed.get("a"))
fresh2, armed = scan([{"id": "a", "alias": "甲", "login": "1", "floating": -4000, "currency": "USD"}], 3000, armed)
check("还在阈值下不再响", fresh2 == [] and armed.get("a"))
fresh3, armed = scan([{"id": "a", "alias": "甲", "login": "1", "floating": -100, "currency": "USD"}], 3000, armed)
check("回到阈值以上才重新武装", fresh3 == [] and "a" not in armed)
fresh4, armed = scan([{"id": "a", "alias": "甲", "login": "1", "floating": -3000, "currency": "USD"}], 3000, armed)
check("再次跌破会再响", len(fresh4) == 1)
check("弹出停留 3 分钟", POPUP_MS == 180000)
js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
check("前端弹出也是 3 分钟", "const POPUP_MS = 180000" in js and "POPUP_MS" in js)

# ---- mock 服务 ----
import httpx  # noqa: E402

port = 18771
data = Path("/tmp/fleet133")
if data.exists():
    import shutil
    shutil.rmtree(data)
env = os.environ.copy()
env["FLEET_MOCK_AUTOTRADE"] = "0"
proc = subprocess.Popen(
    [sys.executable, "app.py", "--mock", "--no-browser", "--port", str(port), "--data-dir", str(data)],
    cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)
try:
    token = ""
    for _ in range(40):
        try:
            html = httpx.get(f"http://127.0.0.1:{port}/", timeout=2).text
            m = re.search(r'FLEET_TOKEN = "([^"]+)"', html)
            if m:
                token = m.group(1)
                break
        except Exception:
            time.sleep(0.4)
    check("mock 服务起来", bool(token))
    if not token:
        raise SystemExit(1)
    c = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"X-Fleet-Token": token}, timeout=120)
    st = c.get("/api/state").json()
    check("默认警报金额 3000", st["settings"].get("alert_loss") == 3000, st["settings"].get("alert_loss"))
    check("状态里带上停留时间", st.get("popupMs") == 180000)
    aid = st["accounts"][0]["id"]
    r = c.post("/api/login", json={"ids": [aid]}).json()
    check("登录一个账户", r["ok"] == 1, r)
    online = None
    for _ in range(20):
        st = c.get("/api/state").json()
        online = next((a for a in st["accounts"] if a["id"] == aid and a["link"] == "online"), None)
        if online:
            break
        time.sleep(0.4)
    check("账户在线", online is not None, None if online else st["accounts"][0]["link"])
    c.post("/api/mock/floating", json={"id": aid, "floating": -3000})
    hit = None
    for _ in range(20):
        st = c.get("/api/state").json()
        if st.get("alerts"):
            hit = st
            break
        time.sleep(0.4)
    check("浮亏到 3000 出现警报", bool(hit), (hit or st).get("alerts"))
    if hit:
        al = hit["alerts"][0]
        check("警报带账户、金额、编号", al["n"] == 1 and al["floating"] == -3000 and al["accountId"] == aid, al)
        check("首页弹出这条警报", (hit.get("alertPopup") or {}).get("id") == al["id"])
        c.post("/api/alerts/view", json={"id": al["id"]})
        st = c.get("/api/state").json()
        check("查看后取消", st["alerts"] == [] and not st.get("alertPopup"))
        c.post("/api/mock/floating", json={"id": aid, "floating": -4500})
        time.sleep(2.2)
        st = c.get("/api/state").json()
        check("还在阈值下查看后不再响", st["alerts"] == [], st["alerts"])
        c.post("/api/mock/floating", json={"id": aid, "floating": -100})
        time.sleep(2.2)
        c.post("/api/mock/floating", json={"id": aid, "floating": -3200})
        again = None
        for _ in range(20):
            st = c.get("/api/state").json()
            if st.get("alerts"):
                again = st
                break
            time.sleep(0.4)
        check("回到阈值以上后再次跌破会再响", bool(again) and again["alerts"][0]["n"] == 2, (again or st).get("alerts"))
    q = None
    for _ in range(15):
        st = c.get("/api/state").json()
        if (st.get("quote") or {}).get("price"):
            q = st["quote"]
            break
        time.sleep(0.5)
    check("USDJPY 有价格和涨跌", bool(q) and "change" in q, q)
    cal = None
    for _ in range(25):
        st = c.get("/api/state").json()
        cal = st.get("calendar") or {}
        if cal.get("events") or cal.get("error"):
            break
        time.sleep(0.5)
    ev = cal.get("events") or []
    check("日历事件都是 4 星及以上", all(e.get("star", 0) >= 4 for e in ev), [e.get("star") for e in ev[:8]])
    if not ev:
        check("金十未授权时有说明", "未授权" in (cal.get("error") or "") or "金十" in (cal.get("error") or ""), cal.get("error"))
    else:
        check("金十返回了日历", True, len(ev))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()

print("FAILS", len(fails))
sys.exit(1 if fails else 0)
