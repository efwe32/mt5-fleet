"""v1.4.3：锁单冷却跨重启保持。
python tests/test_panel143.py
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
from fleet import lock_cool  # noqa: E402
from fleet import __version__  # noqa: E402

fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {info}" if info else ""))
    if not cond:
        fails.append(name)


# ---- 纯逻辑 ----
pos_lock = [
    {"symbol": "USDJPYc", "side": "buy", "volume": 0.5},
    {"symbol": "USDJPYc", "side": "sell", "volume": 0.5},
    {"symbol": "XAUUSD", "side": "buy", "volume": 0.1},
]
check("锁单检测同品种多空", lock_cool.lock_symbols(pos_lock) == ["USDJPYc"], lock_cool.lock_symbols(pos_lock))
check("无对锁不算锁单", lock_cool.lock_symbols([{"symbol": "USDJPYc", "side": "buy", "volume": 1}]) == [])
check("空仓算清仓", lock_cool.is_flat([]))

a = {"lock_cooldown": False, "lock_symbols": [], "lock_cooldown_at": 0}
ch, note = lock_cool.sync(a, pos_lock, now=1000)
check("检测到锁单进入冷却", ch and a["lock_cooldown"] and a["lock_symbols"] == ["USDJPYc"] and "锁单" in note, (ch, note, a))

# 重启后：标志仍在、仍有持仓（哪怕已不是对锁）→ 保持
a2 = {"lock_cooldown": True, "lock_symbols": ["USDJPYc"], "lock_cooldown_at": 1000}
only_long = [{"symbol": "USDJPYc", "side": "buy", "volume": 0.5}]
ch, note = lock_cool.sync(a2, only_long, now=2000)
check("冷却中未清仓保持", a2["lock_cooldown"] and not note.startswith("已清仓"), (a2, note))

ch, note = lock_cool.sync(a2, [], now=3000)
check("清仓解除冷却", ch and not a2["lock_cooldown"] and a2["lock_symbols"] == [] and "清仓" in note, (a2, note))

# 持久化字段在 _normalize
from fleet.config import Store
check("版本 1.4.4", __version__ == "1.4.4", __version__)

js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
qj = (ROOT / "static" / "quick.js").read_text(encoding="utf-8")
check("账号页冷却标签", "冷却中" in js and "lockCooldown" in js)
check("快捷交易拦开仓", "lockCooldown" in qj and "有锁单禁止新开仓" in qj)

# ---- mock ----
port = 18783
data = Path("/tmp/fleet143")
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
    check("mock 起来", bool(token))
    if not token:
        raise SystemExit(1)
    c = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"X-Fleet-Token": token}, timeout=120)
    check("ping 版本", c.get("/ping").json().get("version") == "1.4.4")
    st = c.get("/api/state").json()
    ids = [a["id"] for a in st["accounts"]]
    r = c.post("/api/login", json={"ids": ids[:2]}).json()
    check("登录 2 户", r["ok"] == 2, r)
    for _ in range(25):
        st = c.get("/api/state").json()
        if sum(1 for a in st["accounts"] if a["id"] in ids[:2] and a["link"] == "online") == 2:
            break
        time.sleep(0.4)

    # mock 主仓·黄金默认就有 XAUUSD 多空对锁；先用它验证「上线即冷却」
    aid = ids[0]
    cool = False
    for _ in range(20):
        st = c.get("/api/state").json()
        a = next(x for x in st["accounts"] if x["id"] == aid)
        if a.get("lockCooldown"):
            cool = True
            break
        time.sleep(0.4)
    check("进入冷却", cool, a if cool else "timeout")
    check("状态带 lockCooldownMsg", bool(a.get("lockCooldownMsg")), a.get("lockCooldownMsg"))
    check("冷却后 algo_off", a.get("algo_off") is True, a.get("algo_off"))

    # 禁止新开仓
    r = c.post("/api/trade/order", json={"ids": [aid], "order": {
        "symbol": "XAUUSD", "kind": "buy", "lots": 0.01, "scale_mode": "fixed",
    }}).json()
    check("冷却禁止下单", r["ok"] == 0 and "锁单" in r["results"][0]["message"], r)

    r = c.post("/api/quick/action", json={"action": "buy", "ids": [aid], "symbol": "XAUUSD", "lots": 0.01, "scale_mode": "fixed"}).json()
    check("冷却禁止快捷开多", r["ok"] == 0 and "锁单" in (r["results"][0]["message"] if r.get("results") else ""), r)

    r = c.post("/api/algo", json={"ids": [aid], "on": True}).json()
    check("冷却禁止开算法", r["ok"] == 0 and ("锁单" in r["results"][0]["message"] or "冷却" in r["results"][0]["message"]), r)

    # 仍可平仓
    r = c.post("/api/trade/close", json={"ids": [aid], "filter": {"mode": "all"}}).json()
    check("冷却可全平", r["ok"] == 1, r)
    time.sleep(1.2)
    cleared = False
    for _ in range(20):
        st = c.get("/api/state").json()
        a = next(x for x in st["accounts"] if x["id"] == aid)
        if not a.get("lockCooldown") and not a.get("positions"):
            cleared = True
            break
        time.sleep(0.4)
    check("清仓后解除冷却", cleared, a)

    # 持久化：accounts.json 应已无冷却（清仓后）
    import json
    saved = json.loads((data / "accounts.json").read_text(encoding="utf-8"))
    sa = next(x for x in saved["accounts"] if x["id"] == aid)
    check("清仓后磁盘无冷却", not sa.get("lock_cooldown"), sa.get("lock_cooldown"))

    # 清仓后应能再开仓；开多+开空再进冷却（写磁盘）
    r = c.post("/api/trade/order", json={"ids": [aid], "order": {
        "symbol": "XAUUSD", "kind": "buy", "lots": 0.01, "scale_mode": "fixed",
    }}).json()
    check("解除后可开多", r["ok"] == 1, r)
    time.sleep(0.5)
    r = c.post("/api/trade/order", json={"ids": [aid], "order": {
        "symbol": "XAUUSD", "kind": "sell", "lots": 0.01, "scale_mode": "fixed",
    }}).json()
    check("开空形成锁单", r["ok"] == 1, r)
    time.sleep(0.5)
    for _ in range(15):
        st = c.get("/api/state").json()
        a = next(x for x in st["accounts"] if x["id"] == aid)
        if a.get("lockCooldown"):
            break
        time.sleep(0.3)
    saved = json.loads((data / "accounts.json").read_text(encoding="utf-8"))
    sa = next(x for x in saved["accounts"] if x["id"] == aid)
    check("冷却写入 accounts.json", sa.get("lock_cooldown") is True, sa)

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
