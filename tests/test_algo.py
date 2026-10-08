"""v1.3.5：交易页一键开关算法交易。
python tests/test_algo.py
"""
import os
import queue
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fleet import terminal as T  # noqa: E402
from fleet.worker import Worker  # noqa: E402

fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {info}" if info else ""))
    if not cond:
        fails.append(name)


# ---- 启动配置：手动关掉的账户，后台重开终端时 Enabled=0 ----
check("启动配置默认打开算法交易", "Enabled=1" in T._ini_lines(None, None, False))
check("手动关掉的账户启动配置保持关闭", "Enabled=0" in T._ini_lines(None, None, False, algo=False))
check("非 Windows 不会去发窗口消息", T.main_windows(1) == [] and T.post_algo_toggle(1) is False)


# ---- 工作进程：后台重连不打开，用户点的登录才打开 ----
def worker(pw, **acc):
    a = {"id": "x", "login": "123456", "server": "S", "terminal_path": "/tmp/x/terminal64.exe", **acc}
    w = Worker(a, pw, {"auto_enable_algo": True}, queue.Queue(), queue.Queue(), True, "/tmp/fleet135w")
    ok, _ = w.connect()
    return w, ok


w, ok = worker("mock-pass", algo_off=True)
check("手动关掉的账户：后台重连后仍是关", ok and w._algo_now() is False)
w, ok = worker("noalgo")
check("后台重连：算法交易本来关着就不去动", ok and w._algo_now() is False)
w, ok = worker("noalgo", _explicit=True)
check("用户点的登录：没开就自动打开", ok and w._algo_now() is True)
ok, msg, d = w.set_algo(False)
check("关闭后回读终端确实是关", ok and d["trade_allowed"] is False and "持仓保留" in msg, msg)
ok, msg, d = w.set_algo(False)
check("已经是关时不重复切换", ok and d.get("changed") is False, msg)
ok, msg, d = w.set_algo(True)
check("再打开回读是开", ok and d["trade_allowed"] is True, msg)

w, ok = worker("algostuck", _explicit=True)
ok2, msg, d = w.set_algo(True)
check("按钮没反应时报失败并说明", ok and not ok2 and "状态没有变化" in msg and d["trade_allowed"] is False, msg)

# ---- 面板接口（本机 + 手机远程口） ----
port = 18784
data = Path("/tmp/fleet135")
shutil.rmtree(data, ignore_errors=True)
env = {**os.environ, "FLEET_MOCK_AUTOTRADE": "0", "FLEET_REMOTE_NO_TUNNEL": "1"}
proc = subprocess.Popen([sys.executable, "app.py", "--mock", "--no-browser", "--port", str(port), "--data-dir", str(data)],
                        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    token, page = "", ""
    for _ in range(40):
        try:
            page = httpx.get(f"http://127.0.0.1:{port}/", timeout=2).text
            m = re.search(r'FLEET_TOKEN = "([^"]+)"', page)
            if m:
                token = m.group(1)
                break
        except Exception:
            time.sleep(0.3)
    check("本机页面能打开", bool(token))
    check("交易页有算法交易开关", all(x in page for x in ("算法交易开关", 'id="alOn"', 'id="alOff"', "/static/algo.js", 'id="alPick"')))
    c = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"X-Fleet-Token": token}, timeout=60)
    check("algo.js 能加载", c.get("/static/algo.js").status_code == 200)
    r = c.post("/api/login", json={"ids": ["a1", "a2", "a3"]}).json()
    check("登录 3 个账户", r["ok"] == 3, r)

    def algo_of(i):
        a = next(x for x in c.get("/api/state").json()["accounts"] if x["id"] == i)
        return (a.get("terminal") or {}).get("trade_allowed"), a.get("algo_off")

    check("登录后算法交易是开", all(algo_of(i)[0] is True for i in ("a1", "a2", "a3")))
    check("必须说明开还是关", c.post("/api/algo", json={"ids": ["a1"]}).status_code == 400)
    r = c.post("/api/algo", json={"ids": ["a1", "a2", "a5"], "on": False}).json()
    res = {x["id"]: x for x in r["results"]}
    check("一键关闭：在线的两个成功", res["a1"]["ok"] and res["a2"]["ok"] and res["a1"]["trade_allowed"] is False, r)
    check("一键关闭：未登录的报失败并说明原因", not res["a5"]["ok"] and "未登录" in res["a5"]["message"], res["a5"])
    check("关闭结果写明持仓保留", "持仓保留" in res["a1"]["message"], res["a1"]["message"])
    time.sleep(2)
    check("状态里实时显示已关闭", algo_of("a1") == (False, True) and algo_of("a2") == (False, True), (algo_of("a1"), algo_of("a2")))
    check("没选的账户不受影响", algo_of("a3")[0] is True and not algo_of("a3")[1])
    c.post("/api/refresh", json={"ids": ["a1"]})
    time.sleep(2)
    check("后台刷新不会把它打开", algo_of("a1")[0] is False)
    r = c.post("/api/algo", json={"ids": ["a1"], "on": True}).json()
    check("一键开启单个账户", r["ok"] == 1 and r["results"][0]["trade_allowed"] is True, r)
    time.sleep(2)
    check("开启后状态是开且不再标记关闭", algo_of("a1") == (True, False), algo_of("a1"))
    r = c.post("/api/login", json={"ids": ["a2"]}).json()
    time.sleep(2)
    check("用户点登录会重新打开", r["ok"] == 1 and algo_of("a2") == (True, False), (r["results"][0]["message"], algo_of("a2")))
    logs = c.get("/api/logs").json() if c.get("/api/logs").status_code == 200 else []
    text = str(logs)
    check("日志里有逐个账户的结果", "一键关闭算法交易" in text and "一键开启算法交易" in text)

    # 手机远程口：同一个接口，没有远程密码不能用
    ok = c.post("/api/settings", json={"remote_password": "phone-pass-1", "remote_enabled": True})
    rp = c.get("/api/state").json()["remote"]["port"]
    rc = httpx.Client(base_url=f"http://127.0.0.1:{rp}", timeout=30)
    bad = rc.post("/api/algo", json={"ids": ["a3"], "on": False}, headers={"X-Fleet-Token": token})
    check("手机远程口没有密码不能开关", bad.status_code == 401, bad.status_code)
    check("没有密码的请求没有改动终端", algo_of("a3")[0] is True)
    good = rc.post("/api/remote/login", json={"password": "phone-pass-1"})
    m = re.search(r"fleet_remote=([^;]+)", good.headers.get("set-cookie", ""))
    hdr = {"X-Fleet-Token": token, "Cookie": f"fleet_remote={m.group(1) if m else ''}"}
    page = rc.get("/", headers={"Cookie": hdr["Cookie"]}).text
    check("手机页面也有算法交易开关", "算法交易开关" in page and "/static/algo.js" in page)
    r = rc.post("/api/algo", json={"ids": ["a3"], "on": False}, headers=hdr).json()
    check("手机输入密码后可以一键关闭", r["ok"] == 1 and r["results"][0]["trade_allowed"] is False, r)
    r = rc.post("/api/algo", json={"ids": ["a3"], "on": True}, headers=hdr).json()
    check("手机一键开启", r["ok"] == 1 and r["results"][0]["trade_allowed"] is True, r)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()

print("FAILS", len(fails))
sys.exit(1 if fails else 0)
