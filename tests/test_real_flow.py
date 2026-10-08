"""一键登录 + 一键分发 的端到端测试（Linux 上用假的 terminal64.exe 和假的 MetaTrader5 包，走“实盘”代码路径）。
用法：python tests/test_real_flow.py /tmp/fake/terminal64.exe [端口]
假终端由 tests/fake_terminal.c 编译：cc -O2 -o /tmp/fake/terminal64.exe tests/fake_terminal.c
覆盖：自动创建副本、启动配置（含登录信息）内容和删除、servers.dat 只读导入、分发到多个账户、失败说明、只关闭自己启动的终端；
默认只开一个 USDJPYc M15 图表、EA 写进图表配置（重启后还在）、MT5 自动更新后重新接管、品种后缀检查、重开软件自动清理旧后台并接管终端。
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fleet.terminal import _write_chart, chart_text, list_charts  # noqa: E402

HERE = Path(__file__).resolve().parent
APP = HERE.parent
fake = Path(sys.argv[1])
port = int(sys.argv[2]) if len(sys.argv) > 2 else 8890
tmp = Path(tempfile.mkdtemp(prefix="fleet-real-"))
troot = tmp / "terminals"
data = tmp / "data"
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {info}" if info else ""))
    if not cond:
        fails.append(name)


def u16(path: Path) -> str:
    raw = path.read_bytes()
    return raw[2:].decode("utf-16-le") if raw.startswith(b"\xff\xfe") else raw.decode("utf-8", "ignore")


def procs_of(exe: Path):
    import psutil
    out = []
    for p in psutil.process_iter(["exe"]):
        try:
            if p.info["exe"] and os.path.samefile(p.info["exe"], exe) and p.status() != psutil.STATUS_ZOMBIE:
                out.append(p)
        except Exception:
            pass
    return out


# ---- 模板 MT5（假的）：自带一份 servers.dat，还带一个不该被复制的 accounts.dat ----
tpl = tmp / "MetaTrader 5    4"
(tpl / "Config").mkdir(parents=True)
(tpl / "MQL5" / "Experts").mkdir(parents=True)
(tpl / "Bases").mkdir()
shutil.copy2(fake, tpl / "terminal64.exe")
(tpl / "Config" / "servers.dat").write_bytes(b"TEMPLATE-SERVERS" * 8)
(tpl / "Config" / "accounts.dat").write_bytes(b"secret")
prof = tpl / "MQL5" / "Profiles" / "Charts" / "Default"
prof.mkdir(parents=True)
for i, (sym, tf) in enumerate([("EURUSD", "H1"), ("GBPUSD", "H1"), ("USDJPY", "H1"), ("USDCHF", "H1"), ("XAUUSD", "M15"), ("BTCUSD", "D1")], 1):
    _write_chart(prof / f"chart{i:02d}.chr", chart_text(sym, tf))
tpl_before = {str(p): p.stat().st_mtime for p in tpl.rglob("*")}

# ---- “桌面上”的另一份 MT5：只读，用来导入服务器列表 ----
desk = tmp / "desktop-mt5"
(desk / "Config").mkdir(parents=True)
shutil.copy2(fake, desk / "terminal64.exe")
srv = desk / "Config" / "servers.dat"
srv.write_bytes(b"DESKTOP-SERVERS!" * 16)
os.chmod(srv, stat.S_IRUSR | stat.S_IRGRP)
os.chmod(desk / "Config", stat.S_IRUSR | stat.S_IXUSR)
desk_mtime = srv.stat().st_mtime

data.mkdir()
(data / "settings.json").write_text(json.dumps({"template_dir": str(tpl), "login_retry_delay": 1, "verify_seconds": 6, "close_terminal_on_disconnect": True, "update_wait": 30}), encoding="utf-8")
env = {**os.environ, "FLEET_TERMINALS_ROOT": str(troot), "PYTHONPATH": f"{APP}{os.pathsep}{HERE / 'fake_mt5'}",
       "FLEET_MOCK_AUTOTRADE": "0", "PYTHONUNBUFFERED": "1"}


def start_app(logname):
    return subprocess.Popen([sys.executable, str(APP / "app.py"), "--no-browser", "--port", str(port), "--data-dir", str(data)],
                            env=env, cwd=str(APP), stdout=open(tmp / logname, "w"), stderr=subprocess.STDOUT)


def connect_app(old_tok=None):
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            html = httpx.get(base + "/").text
            t = re.search(r'FLEET_TOKEN = "([^"]+)"', html).group(1)
            if t != old_tok:
                return httpx.Client(base_url=base, headers={"X-Fleet-Token": t}, timeout=600), t
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("程序没有启动")


def tpath(lg):
    return str(troot / lg / "terminal64.exe")


def wait_online(c, logins, sec=60, not_pid=None):
    end = time.time() + sec
    while time.time() < end:
        accs = {a["login"]: a for a in c.get("/api/state").json()["accounts"]}
        ok = all(accs[lg]["link"] == "online" for lg in logins)
        if ok and not_pid:
            ok = all(set(p.pid for p in procs_of(troot / lg / "terminal64.exe")) - {not_pid.get(lg)} for lg in logins)
        if ok:
            return accs
        time.sleep(1)
    return {a["login"]: a for a in c.get("/api/state").json()["accounts"]}


srv_proc = start_app("server.log")
srv2 = None
try:
    c, tok = connect_app()
    st = c.get("/api/state").json()
    check("实盘路径（非测试模式）", st["mock"] is False and not st["mt5Error"], st.get("mt5Error"))
    check("模板已识别", st["template"]["path"] == str(tpl), st["template"])

    # ---- 1. 添加并登录：两个正常账户 + 一个密码错误 ----
    t0 = time.time()
    r = c.post("/api/accounts/quick", json={"text": "70001,Pa;ss#1,Real-Server,主仓,甲\n70002,pw2,Real-Server,跟单\n70003,wrong,Real-Server\n"}).json()
    check("添加并登录 2 成功 1 失败", r["ok"] == 2 and r["fail"] == 1, f"{r['ok']}/{r['fail']} {time.time() - t0:.1f}s")
    bad = next(x for x in r["results"] if not x["ok"])
    check("失败有中文说明（授权失败 + 导入服务器提示）", "授权失败" in bad["message"] and "导入服务器列表" in bad["message"], bad["message"][:80])
    for lg in ("70001", "70002"):
        d = troot / lg
        check(f"{lg} 自动创建副本", (d / "terminal64.exe").exists())
        check(f"{lg} 副本不含 Bases / accounts.dat", not (d / "Bases").exists() and not (d / "Config" / "accounts.dat").exists())
        check(f"{lg} 副本带模板的 servers.dat", (d / "Config" / "servers.dat").read_bytes().startswith(b"TEMPLATE"))
        seen = sorted(d.glob("fake_seen_*.ini"))
        txt = u16(seen[0]) if seen else ""
        check(f"{lg} 启动配置含登录信息和算法交易", all(k in txt for k in (f"Login={lg}", "Password=", "Server=Real-Server", "KeepPrivate=1",
                                                                       "NewsEnable=0", "[Experts]", "AllowLiveTrading=1", "Enabled=1", "AllowDllImport=0")), txt.replace("\r\n", " | ")[:160])
        check(f"{lg} 含密码的启动配置已删除", not list(d.glob("fleet_*.ini")), [p.name for p in d.glob("fleet_*.ini")])
        ch = list_charts(tpath(lg))
        check(f"{lg} 登录后只开一个 USDJPYc M15 图表", [(x["symbol"], x["period"], x["experts"]) for x in ch] == [("USDJPYc", (0, 15), [])], ch)
        bk = list((d / "fleet_backup").glob("charts-*/*.chr"))
        check(f"{lg} 原来的 6 个图表已备份", len(bk) == 6, len(bk))
        check(f"{lg} 登录配置不用 [StartUp]", "[StartUp]" not in txt)
    check("密码里的特殊字符原样写入", "Password=Pa;ss#1" in u16(sorted((troot / "70001").glob("fake_seen_*.ini"))[0]))
    check("登录失败的终端已关闭", not procs_of(troot / "70003" / "terminal64.exe"))
    check("模板目录没有被改动", {str(p): p.stat().st_mtime for p in tpl.rglob("*")} == tpl_before)
    st = c.get("/api/state").json()
    prog = st["progress"]
    items = [prog["items"][i] for i in prog["order"]]
    check("进度：四个步骤", [k for k, _ in prog["steps"]] == ["copy", "launch", "login", "algo"])
    check("进度：成功账户四步全绿", all(s["s"] == "ok" for s in items[0]["steps"].values()), items[0]["steps"])
    check("进度：失败账户停在登录", items[2]["steps"]["login"]["s"] == "fail" and items[2]["steps"]["algo"]["s"] == "skip")
    check("服务器名进入自动补全", "Real-Server" in st["servers"])
    accs = {a["login"]: a for a in st["accounts"]}
    check("密码不回传", all("password" not in a for a in st["accounts"]))

    # ---- 2. 服务器列表：只读导入 ----
    r = c.post("/api/tools/import_servers", json={"path": str(troot / "70001")})
    check("拒绝从自己的副本导入", r.status_code == 400)
    r = c.post("/api/tools/import_servers", json={"path": str(desk)})
    check("从只读的桌面 MT5 导入", r.status_code == 200 and r.json()["ok"], r.text[:120])
    check("源文件未被改动", srv.stat().st_mtime == desk_mtime and srv.read_bytes().startswith(b"DESKTOP"))
    check("导入到数据目录", (data / "servers" / "servers.dat").read_bytes() == srv.read_bytes())
    r = c.post("/api/tools/servers_check", json={"path": str(desk)}).json()
    check("检测文件夹里的 servers.dat", len(r["items"]) == 1)

    # ---- 3. 分发：上传 EA + .set，分发到两个账户（魔术号自增、按分组参数文件、魔术号写入参数） ----
    c.post("/api/strategy/upload", files={"file": ("Pulse.ex5", b"EX5" * 50)})
    c.post("/api/strategy/upload", files={"file": ("base.set", "Lots=0.10\r\nMagicNumber=1||1||1||10||N\r\n".encode("utf-16"))})
    c.post("/api/strategy/upload", files={"file": ("small.set", b"Lots=0.01\r\n")})
    ids = [accs["70001"]["id"], accs["70002"]["id"]]
    pid_before = {lg: [p.pid for p in procs_of(troot / lg / "terminal64.exe")] for lg in ("70001", "70002")}
    t0 = time.time()
    r = c.post("/api/strategy/distribute", json={"ids": ids, "strategy": {"fileName": "Pulse.ex5", "symbol": "XAUUSD", "timeframe": "H1", "magic": 9100, "preset": "base.set"},
                                                 "magic_step": 1, "group_presets": {"跟单": "small.set"}, "magic_param": "MagicNumber", "concurrency": 2}).json()
    check("分发 2 个账户成功", r["ok"] == 2 and r["fail"] == 0, f"{[x['message'] for x in r['results']]} {time.time() - t0:.1f}s")
    for lg, magic, lots in (("70001", 9100, "0.10"), ("70002", 9101, "0.01")):
        d = troot / lg
        check(f"{lg} EA 已复制", (d / "MQL5" / "Experts" / "Fleet" / "Pulse.ex5").exists())
        ps = d / "MQL5" / "Presets" / f"fleet_Pulse_{lg}.set"
        pt = u16(ps) if ps.exists() else ""
        check(f"{lg} 账户参数文件：魔术号 {magic}、手数 {lots}", f"MagicNumber={magic}" in pt and f"Lots={lots}" in pt, pt.replace("\r\n", " | "))
        seen = sorted(d.glob("fake_seen_*.ini"), key=lambda p: int(p.stem.rsplit("_", 1)[1]))
        txt = u16(seen[-1])
        check(f"{lg} 重启时不再用 [StartUp] 挂 EA", "Expert=" not in txt, txt.replace("\r\n", " | ")[-160:])
        ch = list_charts(tpath(lg))
        check(f"{lg} EA 写进图表配置（XAUUSD H1，重启后还在）", [(x["symbol"], x["period"], x["experts"]) for x in ch]
              == [("XAUUSD", (1, 1), [("Pulse", "Experts\\Fleet\\Pulse.ex5")])], ch)
        cf = next((troot / lg / "MQL5" / "Profiles" / "Charts" / "Default").glob("*.chr"))
        ct = u16(cf)
        check(f"{lg} 图表里的 EA 参数含魔术号 {magic}、手数 {lots}", f"MagicNumber={magic}" in ct and f"Lots={lots}" in ct
              and "expertmode=1" in ct and cf.read_bytes().startswith(b"\xff\xfe"), ct[ct.find("<expert>"):ct.find("</expert>")].replace("\r\n", " | "))
        check(f"{lg} 重启后旧进程已关闭、新进程在运行", procs_of(d / "terminal64.exe") and not set(pid_before[lg]) & {p.pid for p in procs_of(d / "terminal64.exe")})
        check(f"{lg} 启动配置已删除", not list(d.glob("fleet_*.ini")))
        check(f"{lg} 导入的服务器列表已在重启时放入", (d / "Config" / "servers.dat").read_bytes().startswith(b"DESKTOP")
              and (d / "Config" / "servers.dat.fleetbak").read_bytes().startswith(b"TEMPLATE"))
    st = c.get("/api/state").json()
    prog = st["progress"]
    check("分发进度五步全绿（日志校验通过）", all(all(s["s"] == "ok" for s in prog["items"][i]["steps"].values()) for i in prog["order"]),
          [prog["items"][i]["steps"]["verify"] for i in prog["order"]])
    accs = {a["login"]: a for a in st["accounts"]}
    check("台账记录魔术号", sorted(s["magic"] for lg in ("70001", "70002") for s in accs[lg]["strategies"]) == [9100, 9101])

    # 再分发一次（替换同名 EA），台账不重复
    r = c.post("/api/strategy/distribute", json={"ids": ids[:1], "strategy": {"fileName": "Pulse.ex5", "symbol": "EURUSD", "timeframe": "M15", "magic": 9200, "preset": ""}}).json()
    accs = {a["login"]: a for a in c.get("/api/state").json()["accounts"]}
    check("再次分发替换同名 EA", r["ok"] == 1 and [(s["symbol"], s["magic"]) for s in accs["70001"]["strategies"]] == [("EURUSD", 9200)], accs["70001"]["strategies"])
    ch = list_charts(tpath("70001"))
    check("替换后只剩一个 EA 图表（EURUSD M15）", [(x["symbol"], x["period"], len(x["experts"])) for x in ch] == [("EURUSD", (0, 15), 1)], ch)

    # ---- 3b. MT5 运行中自动更新（LiveUpdate）：进程换了，程序要自动重新接管，EA 还在，能下单 ----
    old_pid = {"70001": procs_of(troot / "70001" / "terminal64.exe")[0].pid}
    (troot / "70001" / "fake_update_later").write_text("1")
    for _ in range(120):   # 等程序发现终端换了进程并接管
        lg_rows = c.get("/api/logs").json()
        if any(x["action"] == "接管终端" and x["alias"] == "甲" for x in lg_rows):
            break
        time.sleep(0.5)
    accs = wait_online(c, ["70001"], 60, not_pid=old_pid)
    newp = [p.pid for p in procs_of(troot / "70001" / "terminal64.exe")]
    check("更新后终端换了进程，程序自动重新接管并在线", accs["70001"]["link"] == "online" and newp and old_pid["70001"] not in newp,
          f"{accs['70001']['link']} {accs['70001'].get('linkError', '')} {old_pid} -> {newp}")
    for _ in range(20):   # 假终端的更新：新进程先等 2 秒再真正启动
        logtxt = "".join(u16(f) for f in (troot / "70001" / "Logs").glob("*.log"))
        if logtxt.count("expert Pulse (EURUSD,M15) loaded") >= 2:
            break
        time.sleep(0.5)
    check("更新后 EA 从图表配置自动加载", logtxt.count("expert Pulse (EURUSD,M15) loaded") >= 2, logtxt.count("expert Pulse (EURUSD,M15) loaded"))
    check("日志里有接管终端", any(x["action"] == "接管终端" and x["alias"] == "甲" for x in lg_rows),
          [(x["alias"], x["action"], x["ok"], x["message"][:90]) for x in lg_rows[:14]])
    r = c.post("/api/trade/order", json={"ids": ids[:1], "order": {"symbol": "EURUSD", "kind": "buy", "lots": 0.01, "magic": 77}}).json()
    check("EA 运行中、更新后手动下单正常", r["ok"] == 1, r["results"][0]["message"])

    # ---- 3c. 启动时就自动更新：启动配置要留到新进程读完，登录照样成功 ----
    r = c.post("/api/disconnect", json={"ids": [accs["70002"]["id"]]}).json()
    time.sleep(1)
    check("断开并关闭 70002 的终端", not procs_of(troot / "70002" / "terminal64.exe"))
    n_seen = len(list((troot / "70002").glob("fake_seen_*.ini")))
    (troot / "70002" / "fake_update_on_start").write_text("1")
    r = c.post("/api/login", json={"ids": [accs["70002"]["id"]]}).json()
    check("启动时自动更新：照样登录成功", r["ok"] == 1, r["results"][0]["message"])
    seen = sorted((troot / "70002").glob("fake_seen_*.ini"), key=lambda p: int(p.stem.rsplit("_", 1)[1]))
    check("更新后的新进程读到了启动配置（登录信息）", len(seen) == n_seen + 1 and "Login=70002" in u16(seen[-1]), len(seen))
    check("之后启动配置已删除", not list((troot / "70002").glob("fleet_*.ini")))
    check("登录后图表里的 EA 还在（不会被登录冲掉）", len(list_charts(tpath("70002"))) == 1 and list_charts(tpath("70002"))[0]["experts"], list_charts(tpath("70002")))

    # 分发到未登录的账户：先自动登录，失败要说明
    r = c.post("/api/strategy/distribute", json={"ids": [accs["70003"]["id"]], "strategy": {"fileName": "Pulse.ex5", "symbol": "XAUUSD", "timeframe": "H1", "magic": 1}}).json()
    check("未登录账户先自动登录，失败有说明", r["fail"] == 1 and r["results"][0]["message"].startswith("登录失败"), r["results"][0]["message"][:60])
    r = c.post("/api/strategy/distribute", json={"ids": ids, "strategy": {"fileName": "Nope.ex5", "symbol": "XAUUSD", "timeframe": "H1", "magic": 1}})
    check("EA 库里没有的文件被拒绝", r.status_code == 400)

    # ---- 3d. 品种后缀：Cent 账户只有带 c 的品种 ----
    r = c.post("/api/accounts/quick", json={"text": "70005,pw5,Exness-MT5Cent,美分\n"}).json()
    check("美分账户登录成功", r["ok"] == 1, r["results"][0]["message"])
    accs = {a["login"]: a for a in c.get("/api/state").json()["accounts"]}
    check("美分账户默认图表 USDJPYc", [x["symbol"] for x in list_charts(tpath("70005"))] == ["USDJPYc"])
    cid = accs["70005"]["id"]
    r = c.post("/api/strategy/distribute", json={"ids": [cid], "strategy": {"fileName": "Pulse.ex5", "symbol": "USDJPY", "timeframe": "M15", "magic": 5}}).json()
    check("品种 USDJPY 自动改成 USDJPYc", r["ok"] == 1 and "已自动改用 USDJPYc" in r["results"][0]["message"], r["results"][0]["message"])
    ch = list_charts(tpath("70005"))
    check("美分账户 EA 图表用 USDJPYc", [x["symbol"] for x in ch] == ["USDJPYc"], ch)
    pid5 = [p.pid for p in procs_of(troot / "70005" / "terminal64.exe")]
    r = c.post("/api/strategy/distribute", json={"ids": [cid], "strategy": {"fileName": "Pulse.ex5", "symbol": "GOLD", "timeframe": "M15", "magic": 5}}).json()
    check("没有的品种：分发失败并说明，不重启终端", r["fail"] == 1 and "没有品种 GOLD" in r["results"][0]["message"]
          and [p.pid for p in procs_of(troot / "70005" / "terminal64.exe")] == pid5, r["results"][0]["message"])

    # ---- 3e. 交易页快捷面板（实盘代码路径）：自选账户、自动品种后缀、一键开多/开单、按品种平仓、核弹级全平、自动全平 ----
    qids = [accs["70001"]["id"], accs["70002"]["id"], cid]

    def qsum(sym, ids_):
        q = {}
        for _ in range(25):
            q = c.get("/api/quick/summary", params={"symbol": sym, "accounts": ",".join(ids_)}).json()
            if q["quote"] and all(r["symbol"] for r in q["accounts"] if r["link"] == "online"):
                return q
            time.sleep(0.4)
        return q

    q = qsum("USDJPY", qids)
    check("快捷：3 个账户在线、各自的实际品种（美分 USDJPYc）", q["online"] == 3 and {r["login"]: r["symbol"] for r in q["accounts"]} == {"70001": "USDJPY", "70002": "USDJPY", "70005": "USDJPYc"},
          {r["login"]: (r["symbol"], r["error"]) for r in q["accounts"]})
    r = c.post("/api/quick/action", json={"action": "buy", "ids": qids, "symbol": "USDJPY", "lots": 0.01, "scale_mode": "fixed"}).json()
    check("快捷：一键开多 3 个账户（美分自动改用 USDJPYc）", r["ok"] == 3 and any("已自动改用 USDJPYc" in x["message"] for x in r["results"]), [x["message"][:70] for x in r["results"]])
    r = c.post("/api/quick/action", json={"action": "pair", "ids": qids[1:], "symbol": "USDJPY", "lots": 0.02, "scale_mode": "fixed"}).json()
    check("快捷：一键开单 2 个账户", r["ok"] == 2, [x["message"][:90] for x in r["results"]])
    time.sleep(2)
    q = qsum("USDJPY", qids)
    check("快捷：汇总 多 5 层 0.07 手 / 空 2 层 0.04 手", q["long"]["n"] == 5 and q["long"]["lots"] == 0.07 and q["short"]["n"] == 2 and q["short"]["lots"] == 0.04, (q["long"], q["short"]))
    r = c.post("/api/trade/order", json={"ids": [qids[1]], "order": {"symbol": "EURUSD", "kind": "buy", "lots": 0.01, "magic": 3}}).json()
    r = c.post("/api/quick/action", json={"action": "close_short", "ids": qids, "symbol": "USDJPY", "only_symbol": True}).json()
    q = qsum("USDJPY", qids)
    check("快捷：平全部空（仅 USDJPY）", r["ok"] == 3 and q["short"]["n"] == 0 and q["long"]["n"] == 5, [x["message"][:60] for x in r["results"]])
    r = c.post("/api/quick/action", json={"action": "close_long", "ids": qids[2:], "symbol": "USDJPY", "only_symbol": True}).json()
    check("快捷：美分账户按 USDJPY 平多也认 USDJPYc", r["ok"] == 1 and "成功平掉 2/2" in r["results"][0]["message"], r["results"][0]["message"])
    r = c.post("/api/quick/action", json={"action": "nuke", "ids": [qids[1]]}).json()
    a2 = next(a for a in c.get("/api/state").json()["accounts"] if a["id"] == qids[1])
    check("快捷：核弹级全平（含 EURUSD）", r["ok"] == 1 and not a2["positions"], r["results"][0]["message"])
    r = c.post("/api/quick/auto", json={"amount": -3, "ids": [qids[1]]}).json()
    check("自动全平：启用", r["auto"] and r["auto"]["amount"] == -3)
    check("自动全平：保存到数据目录", (data / "quick_auto.json").exists())
    c.post("/api/quick/action", json={"action": "pair", "ids": [qids[1]], "symbol": "XAUUSD", "lots": 0.5, "scale_mode": "fixed"})
    for _ in range(30):
        q = c.get("/api/quick/summary", params={"symbol": "USDJPY", "accounts": qids[1]}).json()
        if not q["auto"] and q["autoLast"]:
            break
        time.sleep(0.5)
    a2 = next(a for a in c.get("/api/state").json()["accounts"] if a["id"] == qids[1])
    check("自动全平：触发一次、全平并取消", not q["auto"] and q["autoLast"] and q["autoLast"]["ok"] == 1 and not a2["positions"] and not (data / "quick_auto.json").exists(), (q["auto"], q["autoLast"]))
    c.post("/api/quick/action", json={"action": "nuke", "ids": qids})

    # ---- 4. 一键停止 ----
    r = c.post("/api/strategy/stop", json={"ids": ids}).json()
    check("一键停止", r["ok"] == 2, [x["message"] for x in r["results"]])
    accs = {a["login"]: a for a in c.get("/api/state").json()["accounts"]}
    check("台账标记已停止", not any(s["running"] for lg in ("70001", "70002") for s in accs[lg]["strategies"]))
    for lg in ("70001", "70002"):
        ch = list_charts(tpath(lg))
        check(f"{lg} 停止后图表恢复为一个 USDJPYc M15（没有 EA）", [(x["symbol"], x["period"], x["experts"]) for x in ch] == [("USDJPYc", (0, 15), [])], ch)

    # ---- 4b. 旧窗口没关又打开软件：自动清理旧后台，终端不关，自动接管 ----
    term_pids = {lg: sorted(p.pid for p in procs_of(troot / lg / "terminal64.exe")) for lg in ("70001", "70002", "70005")}
    old_main = srv_proc.pid
    srv2 = start_app("server2.log")
    c, tok = connect_app(old_tok=tok)
    try:
        srv_proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        pass
    check("旧的后台已被结束", srv_proc.poll() is not None)
    out2 = (tmp / "server2.log").read_text(encoding="utf-8", errors="ignore")
    check("提示“已清理上次未退出的后台”", "已清理上次未退出的后台" in out2, out2[-300:])
    check("受管终端没有被关掉（进程号不变）",
          {lg: sorted(p.pid for p in procs_of(troot / lg / "terminal64.exe")) for lg in term_pids} == term_pids, term_pids)
    accs = wait_online(c, ["70001", "70002", "70005"], 60)
    check("重开后三个账户自动接管并在线", all(accs[lg]["link"] == "online" for lg in ("70001", "70002", "70005")),
          {lg: (accs[lg]["link"], accs[lg].get("linkError", "")) for lg in ("70001", "70002", "70005")})
    r = c.post("/api/trade/order", json={"ids": [accs["70002"]["id"]], "order": {"symbol": "EURUSD", "kind": "buy", "lots": 0.01, "magic": 78}}).json()
    check("重开后下单正常", r["ok"] == 1, r["results"][0]["message"])
    srv_proc = srv2

    # ---- 5. 外部进程不受影响 + 退出只关自己的 ----
    import psutil
    other_py = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], cwd=str(tmp))
    other = subprocess.Popen([str(desk / "terminal64.exe"), "/portable"], cwd=str(tmp))
    time.sleep(0.5)
    c.post("/api/exit", json={})
    srv_proc.wait(timeout=90)
    time.sleep(1)
    check("退出时关闭本程序启动的终端", not any(procs_of(troot / lg / "terminal64.exe") for lg in ("70001", "70002", "70005")))
    check("别的 Python 程序没被碰", other_py.poll() is None)
    other_py.kill()
    check("其它 MT5 进程仍在运行", other.poll() is None)
    other.kill()
    other.wait()
    check("没有遗留的启动配置", not list(troot.glob("*/fleet_*.ini")))
finally:
    for sp in (srv_proc, srv2):
        if sp is not None and sp.poll() is None:
            sp.kill()
    for p in procs_of(fake):
        pass
    import psutil
    for p in psutil.process_iter(["exe"]):
        try:
            if p.info["exe"] and str(tmp) in p.info["exe"]:
                p.kill()
        except Exception:
            pass
    try:
        os.chmod(desk / "Config", stat.S_IRWXU)
        os.chmod(srv, stat.S_IRWXU)
    except Exception:
        pass
    if fails:
        for n in ("server.log", "server2.log"):
            if (tmp / n).exists():
                print(f"---- {n}\n" + (tmp / n).read_text(encoding="utf-8", errors="ignore")[-3000:])
    shutil.rmtree(tmp, ignore_errors=True)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
sys.exit(1 if fails else 0)
