"""v1.3.4：去掉金十；远程端口必须密码，连错会锁定；本机入口不变。
python tests/test_remote.py
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fleet.remote import check_password, hash_password  # noqa: E402

fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {info}" if info else ""))
    if not cond:
        fails.append(name)


stored = hash_password("secret-pass")
check("密码摘要能核对", check_password("secret-pass", stored) and not check_password("nope", stored))
check("摘要里没有明文", "secret-pass" not in stored)

port = 18774
data = Path("/tmp/fleet134")
if data.exists():
    import shutil
    shutil.rmtree(data)
env = os.environ.copy()
env["FLEET_MOCK_AUTOTRADE"] = "0"
env["FLEET_REMOTE_NO_TUNNEL"] = "1"
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
                page = html
                break
        except Exception:
            time.sleep(0.3)
    check("本机页面能打开", bool(token))
    if not token:
        raise SystemExit(1)
    check("页面上没有金十", "金十" not in page and "jin10" not in page.lower() and "rili.jin10" not in page)
    c = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"X-Fleet-Token": token}, timeout=30)
    st = c.get("/api/state").json()
    check("状态里没有日历", "calendar" not in st)
    check("默认远程是关的", st["remote"]["enabled"] is False and st["remote"]["online"] is False, st["remote"])
    check("密码摘要不出现在状态里", "remote_pass_hash" not in str(st) and "scrypt$" not in str(st))
    r = c.get("/api/calendar")
    check("日历接口已删除", r.status_code == 404, r.status_code)
    r = c.post("/api/trade/order", json={"ids": ["a1"], "order": {"symbol": "XAUUSD", "kind": "buy", "lots": 0.01}})
    check("本机带令牌不会被远程密码拦住", r.status_code != 401, r.status_code)
    bad = c.post("/api/settings", json={"remote_enabled": True})
    check("没设密码不能打开", bad.status_code == 400, bad.text[:80])
    ok = c.post("/api/settings", json={"remote_password": "phone-pass-1", "remote_enabled": True})
    check("设好密码可以打开", ok.status_code == 200 and "phone-pass-1" not in ok.text and "scrypt$" not in ok.text, ok.status_code)
    st = c.get("/api/state").json()
    rp = st["remote"]["port"]
    check("远程端口已就绪", rp and rp != port, rp)
    rc = httpx.Client(base_url=f"http://127.0.0.1:{rp}", timeout=30)
    page = rc.get("/")
    check("手机入口先要密码", page.status_code == 200 and "FLEET_TOKEN" not in page.text and "远程密码" in page.text)
    trade = rc.post("/api/trade/order", json={"ids": ["a1"], "order": {"symbol": "XAUUSD", "kind": "buy", "lots": 0.01}})
    check("没密码不能下单", trade.status_code == 401, trade.status_code)
    state = rc.get("/api/state", headers={"X-Fleet-Token": token})
    check("只拿本机令牌也不能看远程状态", state.status_code == 401, state.status_code)
    good = rc.post("/api/remote/login", json={"password": "phone-pass-1"})
    sid = good.cookies.get("fleet_remote") or ""
    if not sid:
        m = re.search(r"fleet_remote=([^;]+)", good.headers.get("set-cookie", ""))
        sid = m.group(1) if m else ""
    check("密码正确发给会话", good.status_code == 200 and bool(sid), good.status_code)
    seen = rc.get("/api/state", headers={"X-Fleet-Token": token, "Cookie": f"fleet_remote={sid}"})
    check("密码通过后能看面板", seen.status_code == 200 and "accounts" in seen.json(), seen.status_code)
    check("账户密码不在远程状态里", all("password" not in a for a in seen.json()["accounts"]))
    for i in range(4):
        w = rc.post("/api/remote/login", json={"password": "wrong"})
        check(f"第 {i+1} 次错误仍是密码错误", w.status_code == 401, w.status_code) if i == 0 else None
    fifth = rc.post("/api/remote/login", json={"password": "wrong"})
    check("第 5 次错误后锁定", fifth.status_code == 429, fifth.status_code)
    locked = rc.post("/api/remote/login", json={"password": "phone-pass-1"})
    check("锁定期间正确密码也不放行", locked.status_code == 429, locked.text[:80])
    local = c.get("/api/state")
    check("锁定不影响本机", local.status_code == 200)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()

print("FAILS", len(fails))
sys.exit(1 if fails else 0)
