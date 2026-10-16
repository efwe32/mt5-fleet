"""v1.4.5：取消浮亏警报。
python tests/test_panel145.py
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
from fleet import __version__  # noqa: E402

fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {info}" if info else ""))
    if not cond:
        fails.append(name)


check("版本 1.4.5", __version__ == "1.4.5", __version__)
check("无 alerts 模块", not (ROOT / "fleet" / "alerts.py").exists())

js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
check("无警报栏 HTML", "alert-board" not in html and "setAlert" not in html)
check("无警报前端逻辑", "renderAlerts" not in js and "alert-view" not in js and "alert_loss" not in js)
check("无警报 CSS", "alert-board" not in css and "alert-chip" not in css)
check("分发弹出仍 3 分钟", "const POPUP_MS = 180000" in js)
check("banner 仍用 POPUP_MS", "POPUP_MS" in js and "banner-x" in js)

mgr = (ROOT / "fleet" / "manager.py").read_text(encoding="utf-8")
srv = (ROOT / "fleet" / "server.py").read_text(encoding="utf-8")
check("无后台警报监控", "_apply_alerts" not in mgr and "view_alert" not in mgr)
check("无警报 API", "/api/alerts" not in srv and "alert_loss" not in srv.split("pop")[0])

port = 18785
data = Path("/tmp/fleet145")
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
            time.sleep(0.3)
    check("mock 起来", bool(token))
    c = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"X-Fleet-Token": token}, timeout=60)
    check("ping", c.get("/ping").json().get("version") == "1.4.5")
    st = c.get("/api/state").json()
    check("状态无 alerts", "alerts" not in st and "alertPopup" not in st and "popupMs" not in st)
    check("设置无 alert_loss", "alert_loss" not in st["settings"])
    r = c.post("/api/alerts/view", json={"id": "x"})
    check("警报接口已移除", r.status_code in (404, 405, 422) or (r.status_code == 404), r.status_code)
    # 旧设置里的 alert_loss 保存时会被清掉
    r = c.post("/api/settings", json={"max_lots": st["settings"]["max_lots"]}).json()
    st2 = c.get("/api/state").json()
    check("保存后仍无 alert_loss", "alert_loss" not in st2["settings"])
    # 页面无警报文案
    check("首页 HTML 无警报板", "alert-board" not in page and "浮亏警报" not in page)
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
