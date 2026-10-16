"""v1.4.7：取消锁单冷却；持仓按账户选择；收益浮赢金额。
python tests/test_panel147.py
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


check("版本 1.4.7", __version__ == "1.4.7", __version__)
check("无 lock_cool 模块", not (ROOT / "fleet" / "lock_cool.py").exists())

js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
qj = (ROOT / "static" / "quick.js").read_text(encoding="utf-8")
aj = (ROOT / "static" / "algo.js").read_text(encoding="utf-8")
dj = (ROOT / "static" / "desk.js").read_text(encoding="utf-8")
html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
mgr = (ROOT / "fleet" / "manager.py").read_text(encoding="utf-8")
cfg = (ROOT / "fleet" / "config.py").read_text(encoding="utf-8")

check("前端无冷却文案", "冷却" not in js and "冷却" not in qj and "冷却" not in aj)
check("前端无 lockCooldown", "lockCooldown" not in js and "lockCooldown" not in qj and "lockCooldown" not in aj)
check("manager 无 open_blocked/锁单冷却", "open_blocked" not in mgr and "lock_cool" not in mgr and "_sync_lock" not in mgr)
check("save 清掉冷却字段", "lock_cooldown" in cfg and "pop" in cfg)
check("持仓账户下拉", 'id="qPosAcc"' in html and "syncPosAccPick" in qj and "qPosSum" in qj)
check("持仓摘要有余额浮盈", "余额" in qj and "浮赢" in qj and "浮亏" in qj)
check("收益浮赢金额", 'id="pdFlWin"' in html and "浮赢金额" in dj and "flWin" in dj)
check("收益浮亏金额仍在", 'id="pdFlLoss"' in html and "浮亏金额" in dj)
check("无 tradeCool 横幅", "tradeCool" not in html and "tradeCool" not in js)

notes = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
check("更新说明有 1.4.7", "## v1.4.7" in notes and "锁单冷却" in notes and "浮赢金额" in notes)

port = 18787
data = Path("/tmp/fleet147")
if data.exists():
    shutil.rmtree(data)
# seed old cooldown fields to confirm they get stripped on save
data.mkdir(parents=True)
(data / "accounts.json").write_text(
    '''{"version":1,"accounts":[{"id":"a1","alias":"测","login":"1001","server":"Demo","group":"未分组",
    "password":"","terminal_path":"","enabled":true,"lot_multiplier":1,"symbol_suffix":"",
    "symbol_map":"","strategies":[],"last":{},"algo_off":false,
    "lock_cooldown":true,"lock_symbols":["USDJPYc"],"lock_cooldown_at":123}]}''',
    encoding="utf-8",
)
(data / "settings.json").write_text("{}", encoding="utf-8")

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
    check("ping", c.get("/ping").json().get("version") == "1.4.7")
    st = c.get("/api/state").json()
    accs = st.get("accounts") or []
    check("状态无 lockCooldown", all(not a.get("lockCooldown") and "lockCooldownMsg" not in a for a in accs), accs[:1])
    # 启动时应已清掉磁盘冷却字段
    import json
    disk = json.loads((data / "accounts.json").read_text(encoding="utf-8"))
    for a in disk.get("accounts") or []:
        check("磁盘无 lock_cooldown", "lock_cooldown" not in a and "lock_symbols" not in a and "lock_cooldown_at" not in a, a)
    # 有对锁持仓时仍应能预览/开仓（mock）
    if accs:
        aid = accs[0]["id"]
        r = c.post("/api/trade/preview", json={"ids": [aid], "order": {"symbol": "USDJPYc", "lots": 0.01, "scale_mode": "fixed", "ref_equity": 10000, "scale_groups": []}}).json()
        check("有锁单仍可预览开仓", "total" in r or "rows" in r, r)
        r = c.post("/api/quick/action", json={"action": "buy", "ids": [aid], "symbol": "USDJPYc", "lots": 0.01, "scale_mode": "fixed", "ref_equity": 10000}).json()
        # mock 可能失败因未登录，但不应因「锁单/冷却」拒绝
        msg = " ".join(x.get("message", "") for x in (r.get("results") or []))
        check("快捷开仓不被冷却拒绝", "锁单" not in msg and "冷却" not in msg, (r.get("ok"), msg[:120]))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    sys.exit(1)
print("ALL PASS")
