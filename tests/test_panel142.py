"""v1.4.2：经典统计修复 + 交易页选中账户持仓列表一键平仓。
python tests/test_panel142.py
"""
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {info}" if info else ""))
    if not cond:
        fails.append(name)


js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
# once：必须先跑 fn 再记 lastRender，否则抛错后永远不重试
m = re.search(r"function once\(key, value, fn\) \{([\s\S]*?)\n\}", js)
body = m.group(1) if m else ""
check("once 先执行再缓存", "fn();" in body and body.find("fn();") < body.find("ui.lastRender[key] = s"))
check("经典统计用服务器 UTC 年", "sn.getUTCFullYear()" in js and "now.getFullYear()" not in js.split("function renderStats")[1].split("function ")[0])
check("经典统计按账户范围过滤成交", "scopeIds.includes(d.accountId)" in js)

qj = (ROOT / "static" / "quick.js").read_text(encoding="utf-8")
html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
css = (ROOT / "static" / "quick.css").read_text(encoding="utf-8")
check("持仓列表面板 HTML", 'id="qPosPanel"' in html and 'id="qPosList"' in html)
check("持仓列表渲染", "function renderPosList" in qj and "data-qclose" in qj)
check("单笔平仓确认走 /api/trade/close ticket", 'filter: { mode: "ticket"' in qj and "askCloseOne" in qj)
check("持仓列表 CSS", ".q-pospanel" in css and ".q-px" in css)
check("中文列头", "魔术号" in qj and "浮盈" in qj and "开仓价" in qj)

from fleet import __version__
check("版本 1.4.2", __version__ == "1.4.2", __version__)

# ---- mock 服务 ----
port = 18782
data = Path("/tmp/fleet142")
if data.exists():
    shutil.rmtree(data)
env = os.environ.copy()
env["FLEET_MOCK_AUTOTRADE"] = "0"
proc = subprocess.Popen(
    [sys.executable, "app.py", "--mock", "--no-browser", "--port", str(port), "--data-dir", str(data)],
    cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)
try:
    token = ""
    for _ in range(50):
        try:
            page = httpx.get(f"http://127.0.0.1:{port}/", timeout=2).text
            m = re.search(r'FLEET_TOKEN = "([^"]+)"', page)
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
    ver = c.get("/ping").json()
    check("版本接口", ver.get("version") == "1.4.2", ver)
    ids = [a["id"] for a in st["accounts"]]
    r = c.post("/api/login", json={"ids": ids}).json()
    check("批量登录", r["ok"] >= 1, r)
    online = []
    for _ in range(25):
        st = c.get("/api/state").json()
        online = [a for a in st["accounts"] if a["link"] == "online"]
        if online:
            break
        time.sleep(0.4)
    check("有在线账户", len(online) >= 1, len(online))
    hist = c.post("/api/history", json={}).json()
    check("历史成交可读", "deals" in hist and isinstance(hist["deals"], list), list(hist.keys())[:8])
    # 经典统计依赖 deals；mock 至少应返回列表（可为空，但不能报错）
    check("历史无 error 字段挡板", not hist.get("error"), hist.get("error"))

    # 找一笔持仓做 ticket 平仓
    st = c.get("/api/state").json()
    target = None
    for a in st["accounts"]:
        if a["link"] == "online" and a.get("positions"):
            target = (a["id"], a["positions"][0])
            break
    if not target:
        # 开一笔
        aid = online[0]["id"]
        order = {"symbol": "XAUUSD", "kind": "buy", "lots": 0.01, "scale_mode": "fixed", "magic": 0, "comment": "fleet142"}
        r = c.post("/api/trade/order", json={"ids": [aid], "order": order}).json()
        check("开仓造持仓", r["ok"] == 1, r)
        time.sleep(1)
        st = c.get("/api/state").json()
        for a in st["accounts"]:
            if a["id"] == aid and a.get("positions"):
                target = (a["id"], a["positions"][0])
                break
    check("有持仓可平", target is not None)
    if target:
        aid, pos = target
        before = sum(len(a.get("positions") or []) for a in c.get("/api/state").json()["accounts"] if a["id"] == aid)
        r = c.post("/api/trade/close", json={"ids": [aid], "filter": {"mode": "ticket", "ticket": pos["ticket"]}}).json()
        check("ticket 平仓成功", r["ok"] == 1, r)
        time.sleep(0.8)
        after = sum(len(a.get("positions") or []) for a in c.get("/api/state").json()["accounts"] if a["id"] == aid)
        check("平仓后持仓减少", after == before - 1, f"{before}->{after}")
        # magic 0 也能平（上面可能是 0）
        check("持仓含 magic 字段", "magic" in pos)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()

if fails:
    print("FAILED:", ", ".join(fails))
    sys.exit(1)
print("ALL PASS")
