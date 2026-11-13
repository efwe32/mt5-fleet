"""v1.5.16：登录/重连不改算法状态；面板开关管 EA；交易不点工具栏。
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


# ---- 启动配置：可显式写 Enabled；algo=None 时不写，避免改工具栏 ----
check("启动配置可显式打开算法交易", "Enabled=1" in T._ini_lines(None, None, False, algo=True))
check("显式关闭时启动配置 Enabled=0", "Enabled=0" in T._ini_lines(None, None, False, algo=False))
check("不干预时启动配置不写 Enabled", "Enabled=" not in "\n".join(T._ini_lines(None, None, False, algo=None)))
check("非 Windows 不会去发窗口消息", T.main_windows(1) == [] and T.post_algo_toggle(1) is False)


# ---- 工作进程：登录/重连不改工具栏、不改 algo_off；分发仍可打开 ----
def worker(pw, settings=None, **acc):
    a = {"id": "x", "login": "123456", "server": "S", "terminal_path": "/tmp/x/terminal64.exe", **acc}
    w = Worker(a, pw, settings or {"auto_enable_algo": False}, queue.Queue(), queue.Queue(), True, "/tmp/fleet150w")
    ok, _ = w.connect()
    return w, ok


w, ok = worker("mock-pass", algo_off=True)
check("手动禁止 EA 的账户：重连后仍禁止且工具栏保持开", ok and w._algo_now() is True and w.acc.get("algo_off") is True, (w._algo_now(), w.acc.get("algo_off")))

w, ok = worker("noalgo")
check("后台重连：工具栏原本关保持关，不改 algo_off", ok and w._algo_now() is False and w.acc.get("algo_off") is not True, (w._algo_now(), w.acc.get("algo_off")))

w, ok = worker("mock-pass")  # mock 默认 algo 开着；未标记 algo_off
check("后台重连：本来开着保持开，不擅自禁止 EA", ok and w._algo_now() is True and w.acc.get("algo_off") is not True, (w._algo_now(), w.acc.get("algo_off")))

w, ok = worker("noalgo", _explicit=True)
check("用户点登录：工具栏关着也不打开，不改 algo_off", ok and w._algo_now() is False and w.acc.get("algo_off") is not True, (w._algo_now(), w.acc.get("algo_off")))

w, ok = worker("noalgo", {"auto_enable_algo": True}, _explicit=True)
check("即使用户开了旧设置，登录也不自动打开工具栏", ok and w._algo_now() is False, w._algo_now())

w, ok = worker("noalgo", {"auto_enable_algo": False}, _explicit=True)
w._deploy_algo = True
w._explicit = True
snap, note = w.apply_algo_pref(w.snapshot())
check("分发路径：会打开算法交易", w._algo_now() is True and w.acc.get("algo_off") is False, (w._algo_now(), note, w.acc.get("algo_off")))

ok, msg, d = w.set_algo(False)
check("关闭后回读终端确实是关", ok and d["trade_allowed"] is False and "持仓保留" in msg, msg)
ok, msg, d = w.set_algo(False)
check("已经是关时不重复切换", ok and d.get("changed") is False, msg)
ok, msg, d = w.set_algo(True)
check("再打开回读是开", ok and d["trade_allowed"] is True, msg)

w, ok = worker("algostuck", {"auto_enable_algo": True}, _explicit=True)
ok2, msg, d = w.set_algo(True)
check("按钮没反应时报失败并说明", ok and not ok2 and "状态没有变化" in msg and d["trade_allowed"] is False, msg)

# 快捷交易：面板 algo_off 时工具栏应已开，下单后不再关回
w, ok = worker("mock-pass", algo_off=True)
check("准备：面板禁止 EA，工具栏开", ok and w._algo_now() is True and w.acc.get("algo_off") is True)
ok, msg, d = w.with_algo_for_trade(w.cmd_order, {"symbol": "XAUUSD", "kind": "buy", "lots": 0.01, "scale_mode": "fixed", "auto_symbol": True, "deviation": 20, "magic": 1, "max_lots": 10})
check("algo_off 时也能下单", ok, msg)
check("下单后工具栏保持开且 algo_off 保留", w._algo_now() is True and w.acc.get("algo_off") is True, (w._algo_now(), w.acc.get("algo_off")))
w2, ok = worker("mock-pass", {"auto_enable_algo": True}, _explicit=True)
assert w2._algo_now() is True
ok, msg, d = w2.with_algo_for_trade(w2.cmd_order, {"symbol": "XAUUSD", "kind": "buy", "lots": 0.01, "scale_mode": "fixed", "auto_symbol": True, "deviation": 20, "magic": 1, "max_lots": 10})
check("本来就是开：下单成功且保持开", ok and w2._algo_now() is True, (ok, msg, w2._algo_now()))

# ---- 面板接口（本机 + 手机远程口） ----
port = 18785
data = Path("/tmp/fleet150")
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
    check("设置文案含不干预", "不干预" in page or "登录后默认允许图表 EA 自动交易" in page or "登录后默认打开算法交易" in page)
    c = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"X-Fleet-Token": token}, timeout=60)
    check("algo.js 能加载", c.get("/static/algo.js").status_code == 200)
    st0 = c.get("/api/state").json()
    check("默认设置 auto_enable_algo 为否", st0["settings"].get("auto_enable_algo") is False, st0["settings"].get("auto_enable_algo"))
    r = c.post("/api/login", json={"ids": ["a1", "a2", "a3"]}).json()
    check("登录 3 个账户", r["ok"] == 3, r)

    def acc_of(i):
        return next(x for x in c.get("/api/state").json()["accounts"] if x["id"] == i)

    def algo_of(i):
        a = acc_of(i)
        tb = (a.get("terminal") or {}).get("trade_allowed")
        return tb, a.get("algo_off"), a.get("ea_trading")

    check("登录后不改工具栏、不标记 algo_off", all(algo_of(i)[0] is True and algo_of(i)[1] is not True and algo_of(i)[2] is not False for i in ("a1", "a2", "a3")), [algo_of(i) for i in ("a1", "a2", "a3")])
    check("必须说明开还是关", c.post("/api/algo", json={"ids": ["a1"]}).status_code == 400)
    r = c.post("/api/algo", json={"ids": ["a1", "a2", "a5"], "on": True}).json()
    res = {x["id"]: x for x in r["results"]}
    check("一键开启：在线的两个成功", res["a1"]["ok"] and res["a2"]["ok"] and res["a1"]["trade_allowed"] is True, r)
    check("一键开启：未登录的报失败并说明原因", not res["a5"]["ok"] and "未登录" in res["a5"]["message"], res["a5"])
    time.sleep(2)
    check("开启后状态是开且不再标记关闭", algo_of("a1") == (True, False, True) and algo_of("a2") == (True, False, True), (algo_of("a1"), algo_of("a2")))
    check("没选的账户保持登录时状态（未禁止）", algo_of("a3") == (True, False, True), algo_of("a3"))
    c.post("/api/algo", json={"ids": ["a3"], "on": False})
    time.sleep(1)
    check("随后单独关闭 a3", algo_of("a3") == (True, True, False), algo_of("a3"))
    r = c.post("/api/algo", json={"ids": ["a1", "a2"], "on": False}).json()
    res = {x["id"]: x for x in r["results"]}
    check("一键关闭：成功且持仓保留", res["a1"]["ok"] and "持仓保留" in res["a1"]["message"], res["a1"])
    time.sleep(2)
    check("状态里实时显示已禁止 EA，工具栏仍开", algo_of("a1") == (True, True, False) and algo_of("a2") == (True, True, False), (algo_of("a1"), algo_of("a2")))
    c.post("/api/refresh", json={"ids": ["a1"]})
    time.sleep(2)
    check("后台刷新不会把 EA 打开", algo_of("a1")[1] is True and algo_of("a1")[2] is False, algo_of("a1"))
    r = c.post("/api/algo", json={"ids": ["a1"], "on": True}).json()
    check("一键开启单个账户", r["ok"] == 1 and r["results"][0]["trade_allowed"] is True, r)
    time.sleep(2)
    check("开启后状态是开且不再标记关闭(单)", algo_of("a1") == (True, False, True), algo_of("a1"))
    # 再登录：默认不应重新打开
    c.post("/api/algo", json={"ids": ["a2"], "on": False})
    time.sleep(1)
    r = c.post("/api/login", json={"ids": ["a2"]}).json()
    time.sleep(2)
    check("用户点登录不会重新允许 EA", r["ok"] == 1 and algo_of("a2") == (True, True, False), (r["results"][0]["message"], algo_of("a2")))
    logs = c.get("/api/logs").json() if c.get("/api/logs").status_code == 200 else []
    text = str(logs)
    check("日志里有逐个账户的结果", "一键关闭算法交易" in text and "一键开启算法交易" in text)

    # 手机远程口
    ok = c.post("/api/settings", json={"remote_password": "phone-pass-1", "remote_enabled": True})
    rp = c.get("/api/state").json()["remote"]["port"]
    rc = httpx.Client(base_url=f"http://127.0.0.1:{rp}", timeout=30)
    bad = rc.post("/api/algo", json={"ids": ["a3"], "on": True}, headers={"X-Fleet-Token": token})
    check("手机远程口没有密码不能开关", bad.status_code == 401, bad.status_code)
    check("没有密码的请求没有改动终端", algo_of("a3")[1] is True and algo_of("a3")[2] is False, algo_of("a3"))
    good = rc.post("/api/remote/login", json={"password": "phone-pass-1"})
    m = re.search(r"fleet_remote=([^;]+)", good.headers.get("set-cookie", ""))
    hdr = {"X-Fleet-Token": token, "Cookie": f"fleet_remote={m.group(1) if m else ''}"}
    page = rc.get("/", headers={"Cookie": hdr["Cookie"]}).text
    check("手机页面也有算法交易开关", "算法交易开关" in page and "/static/algo.js" in page)
    r = rc.post("/api/algo", json={"ids": ["a3"], "on": True}, headers=hdr).json()
    check("手机输入密码后可以一键开启", r["ok"] == 1 and r["results"][0]["trade_allowed"] is True, r)
    r = rc.post("/api/algo", json={"ids": ["a3"], "on": False}, headers=hdr).json()
    check("手机一键关闭", r["ok"] == 1 and r["results"][0].get("ea_trading") is False, r)

    # 快捷交易在算法关闭时仍可下单，并恢复关闭
    c.post("/api/algo", json={"ids": ["a1"], "on": False})
    time.sleep(1.2)
    check("快捷测前 EA 禁止、工具栏开", algo_of("a1") == (True, True, False), algo_of("a1"))
    r = c.post("/api/quick/action", json={"action": "buy", "ids": ["a1"], "symbol": "XAUUSD", "lots": 0.01}).json()
    check("算法关着快捷开多成功", r.get("ok") == 1, r)
    time.sleep(1.5)
    check("快捷开多后工具栏仍开且 algo_off 保留", algo_of("a1") == (True, True, False), algo_of("a1"))
    c.post("/api/algo", json={"ids": ["a1"], "on": True})
    time.sleep(1.2)
    r = c.post("/api/quick/action", json={"action": "buy", "ids": ["a1"], "symbol": "XAUUSD", "lots": 0.01}).json()
    check("算法开着快捷开多成功", r.get("ok") == 1, r)
    time.sleep(1.2)
    check("算法开着下单后仍保持开", algo_of("a1") == (True, False, True), algo_of("a1"))
    js = c.get("/static/algo.js").text
    check("关闭确认文案：登录不再自动打开", "一键开启" in js and "登录或分发" not in js and "分发 EA" in js)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()

print("FAILS", len(fails))
sys.exit(1 if fails else 0)
