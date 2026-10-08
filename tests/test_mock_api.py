"""测试模式（--mock 假终端）端到端测试：python tests/test_mock_api.py [端口]
需要先运行（关掉随机成交，持仓数才可预期）：set FLEET_MOCK_AUTOTRADE=0 然后 python app.py --mock --no-browser --port 8765
"""
import re
import sys
import time

import httpx

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
BASE = f"http://127.0.0.1:{PORT}"
html = httpx.get(BASE + "/").text
TOKEN = re.search(r'FLEET_TOKEN = "([^"]+)"', html).group(1)
c = httpx.Client(base_url=BASE, headers={"X-Fleet-Token": TOKEN}, timeout=300)
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {info}" if info else ""))
    if not cond:
        fails.append(name)


# 安全
check("无令牌拒绝", httpx.get(BASE + "/api/state").status_code == 401)
check("错误 Host 拒绝", httpx.get(BASE + "/api/state", headers={"Host": "evil.com", "X-Fleet-Token": TOKEN}).status_code == 403)

st = c.get("/api/state").json()
ids = [a["id"] for a in st["accounts"]]
check("6 个示例账户", len(ids) == 6, len(ids))
check("只有实盘：设置里没有 dry_run", "dry_run" not in st["settings"])
c.post("/api/settings", json={"dry_run": True})
check("旧客户端发 dry_run 被忽略", "dry_run" not in c.get("/api/state").json()["settings"])
check("路径在 terminals 下", all("terminals" in a["terminal_path"] for a in st["accounts"]))

# 路径隔离
r = c.post("/api/accounts", json={"alias": "外部", "login": "123456", "server": "X", "password": "p", "terminal_path": "C:\\Program Files\\MetaTrader 5\\terminal64.exe"})
check("拒绝 terminals 以外的终端路径", r.status_code == 400, r.json().get("detail"))
r = c.post("/api/accounts", json={"alias": "外部2", "login": "123457", "server": "X", "password": "p", "terminal_path": "/opt/mt5/terminal64.exe"})
check("拒绝 terminals 以外的终端路径(2)", r.status_code == 400 and "terminals" in r.json().get("detail", ""), r.json().get("detail"))
r = c.post("/api/accounts", json={"alias": "新号", "login": "30001234", "server": "Broker-Demo", "password": "wrong", "group": "测试"})
check("添加账户", r.status_code == 200)
new_id = r.json()["id"]
check("默认路径 terminals/<账号>", r.json()["terminal_path"].endswith("30001234/terminal64.exe") or r.json()["terminal_path"].endswith("30001234\\terminal64.exe"), r.json()["terminal_path"])
check("密码不回传", "password" not in r.json())

# 登录
t = time.time()
r = c.post("/api/login", json={"ids": ids + [new_id]}).json()
check("批量登录 6 成功 1 失败（密码错误）", r["ok"] == 6 and r["fail"] == 1, f"{r['ok']}/{r['fail']} {time.time()-t:.1f}s")
time.sleep(2)
st = c.get("/api/state").json()
online = [a for a in st["accounts"] if a["link"] == "online"]
check("在线 6", len(online) == 6)
check("持仓已读取", sum(len(a["positions"]) for a in online) == 10, sum(len(a["positions"]) for a in online))

# 下单预览
order = {"symbol": "XAUUSD", "kind": "buy", "lots": 0.1, "scale_mode": "multiplier", "scale_groups": ["跟单"], "sl": 500, "tp": 800, "sltp_mode": "points", "magic": 990001}
pv = c.post("/api/trade/preview", json={"ids": ids, "order": order}).json()
lots = {r_["alias"]: r_["lots"] for r_ in pv["rows"]}
check("跟单 B 倍数 0.5 生效", lots["跟单 B"] == 0.05 and lots["主仓 · 黄金"] == 0.1, lots)
before = sum(len(a["positions"]) for a in online)

# 手数保护
big = dict(order, lots=5, scale_mode="fixed")
r = c.post("/api/trade/order", json={"ids": ids[:1], "order": big}).json()
check("单笔上限拦截", r["fail"] == 1 and "手数保护" in r["results"][0]["message"], r["results"][0]["message"])

# 实盘下单（测试用假终端）
r = c.post("/api/trade/order", json={"ids": ids, "order": order}).json()
check("实盘下单成功 6", r["ok"] == 6, r["results"][0]["message"])
time.sleep(1.5)
st = c.get("/api/state").json()
after = sum(len(a["positions"]) for a in st["accounts"] if a["link"] == "online")
check("新增 6 笔持仓", after == before + 6, f"{before}->{after}")

# 收益台：按账户筛选（净值曲线按每个账户的采样重新汇总、事件和平仓记录只给选中的账户）
time.sleep(6)
dk = c.get("/api/desk").json()
check("收益台净值采样（全部）", dk["equity"] and all(len(r) == 5 for r in dk["equity"]) and dk["equity"][-1][4] == 6, dk["equity"][-1:] if dk["equity"] else dk)
on_ids = [a["id"] for a in st["accounts"] if a["link"] == "online"]
pick = on_ids[:2]
dk2 = c.get("/api/desk", params={"accounts": ",".join(pick)}).json()
last_all, last2 = dk["equity"][-1], dk2["equity"][-1] if dk2["equity"] else None
eq_pick = sum(a["equity"] for a in st["accounts"] if a["id"] in pick)
check("只看 2 个账户：净值按这 2 个账户汇总", last2 is not None and last2[4] == 2 and last2[1] < last_all[1]
      and abs(last2[1] - eq_pick) <= max(5, eq_pick * 0.05), (last2, round(eq_pick, 2), last_all[1]))
check("只看 2 个账户：事件只含这 2 个账户（或系统）", all((not e.get("accountId")) or e["accountId"] in pick for e in dk2["events"]) and dk2["accounts"] == sorted(pick),
      len(dk2["events"]))
check("只看 2 个账户：平仓记录只给这 2 个账户", set((dk2.get("deals") or {}).keys()) <= set(pick), list((dk2.get("deals") or {}).keys()))
dk3 = c.get("/api/desk", params={"accounts": "不存在的账户"}).json()
check("选了不存在的账户：没有数据也不报错", dk3["equity"] == [] and dk3["events"] == [])
acc0 = next(a for a in st["accounts"] if a["id"] == ids[0])
p = next(x for x in acc0["positions"] if x["magic"] == 990001)
check("SL/TP 按点数设置", p["sl"] > 0 and p["tp"] > p["sl"], f"sl={p['sl']} tp={p['tp']}")

# 挂单 + 撤单
r = c.post("/api/trade/order", json={"ids": ids[:2], "order": {"symbol": "EURUSD", "kind": "buy_limit", "lots": 0.01, "price": 1.0, "magic": 1}}).json()
check("挂单成功", r["ok"] == 2, r["results"][0]["message"])
r = c.post("/api/trade/cancel", json={"ids": ids[:2]}).json()
check("撤挂单成功", r["ok"] == 2, r["results"][0]["message"])

# 改止损止盈
r = c.post("/api/trade/modify", json={"ids": ids, "modify": {"mode": "symbol", "symbol": "XAUUSD", "sl": 0, "tp": None, "sltp_mode": "price"}}).json()
check("批量清除 XAUUSD 止损", r["ok"] == 6, r["results"][0]["message"])

# 按魔术号平
r = c.post("/api/trade/close", json={"ids": ids, "filter": {"mode": "magic", "magic": 990001}}).json()
check("按魔术号平仓", r["ok"] == 6 and "成功平掉 1/1" in r["results"][0]["message"], r["results"][0]["message"])
time.sleep(1.5)
st = c.get("/api/state").json()
check("平仓后持仓数恢复", sum(len(a["positions"]) for a in st["accounts"] if a["link"] == "online") == before)

# 平空 / 平盈利
r = c.post("/api/trade/close", json={"ids": ids, "filter": {"mode": "short"}}).json()
check("平空", r["ok"] == 6, r["results"][1]["message"])
r = c.post("/api/trade/close", json={"ids": ids, "filter": {"mode": "symbol", "symbol": "US30"}}).json()
check("按品种平 US30", r["ok"] == 6, r["results"][5]["message"])

# ---------------- 交易页快捷面板 ----------------
def qsum(sym, accs, wait_quote=True):
    for _ in range(20):
        q = c.get("/api/quick/summary", params={"symbol": sym, "accounts": ",".join(accs)}).json()
        if q["quote"] or not wait_quote:
            return q
        time.sleep(0.4)
    return q


def qact(action, accs, **kw):
    return c.post("/api/quick/action", json={"action": action, "ids": accs, **kw}).json()


A, B = ids[1], ids[3]          # 51002844（EURUSD 多 0.5 @1.0812）、88011290（EURUSD 多 0.2 @1.0861）
q = qsum("EURUSD", [A, B])
check("快捷：报价 + 两个账户的多单层数/手数/均价", q["quote"] and q["quote"]["bid"] > 0 and q["long"]["n"] == 2 and q["long"]["lots"] == 0.7
      and abs(q["long"]["vwap"] - round((0.5 * 1.0812 + 0.2 * 1.0861) / 0.7, 5)) < 1e-9 and q["short"]["n"] == 0 and q["online"] == 2 and len(q["accounts"]) == 2,
      {k: q[k] for k in ("quote", "long", "short", "online")})
check("快捷：今日/历史平仓盈亏、回撤字段", isinstance(q["today"], (int, float)) and isinstance(q["hist"], (int, float)) and q["hist"] != 0 and q["drawdown"] >= 0, (q["today"], q["hist"], q["drawdown"]))
check("快捷：层级价位", [x[0] for x in q["long"]["layers"]] == [1.0812, 1.0861], q["long"]["layers"])
r = qact("buy", [A, B], symbol="EURUSD", lots=0.01, scale_mode="fixed")
check("快捷：一键开多 2 个账户", r["ok"] == 2 and r["title"] == "一键开多" and "市价买入 EURUSD 0.01" in r["results"][0]["message"], r["results"][0]["message"])
r = qact("sell", [A, B], symbol="EURUSD", lots=0.01, scale_mode="fixed")
check("快捷：一键开空", r["ok"] == 2 and "市价卖出" in r["results"][0]["message"], r["results"][0]["message"])
r = qact("pair", [A, B], symbol="EURUSD", lots=0.01, scale_mode="fixed")
check("快捷：一键开单（同时一多一空）", r["ok"] == 2 and "多：" in r["results"][0]["message"] and "空：" in r["results"][0]["message"], r["results"][0]["message"][:120])
q = qsum("EURUSD", [A, B])
check("快捷：开仓后 多 6 层 / 空 4 层", q["long"]["n"] == 6 and q["short"]["n"] == 4 and q["long"]["lots"] == 0.74, (q["long"]["n"], q["short"]["n"], q["long"]["lots"]))
q1 = qsum("EURUSD", [A])
check("快捷：只选 1 个账户时只统计它", q1["long"]["n"] == 3 and q1["short"]["n"] == 2 and len(q1["accounts"]) == 1, (q1["long"]["n"], q1["short"]["n"]))
check("快捷：记录上次操作", q["last"] and q["last"]["title"] == "一键开单" and q["last"]["ok"] == 2, q["last"])
r = qact("buy", [A], symbol="EURUSD", lots=0.01, scale_mode="multiplier")
check("快捷：按账户倍数（主仓倍数 1）", r["ok"] == 1 and "0.01 手" in r["results"][0]["message"], r["results"][0]["message"])
r = qact("buy", [B], symbol="EURUSD", lots=0.04, scale_mode="multiplier")
check("快捷：按账户倍数对所有选中账户生效（跟单 B 倍数 0.5 → 0.02 手，不受缩放分组限制）", r["ok"] == 1 and "0.02 手" in r["results"][0]["message"], r["results"][0]["message"])
r = qact("buy", [A], symbol="GBPUSD", lots=0.01, scale_mode="fixed")
check("快捷：GBPUSD 开多", r["ok"] == 1, r["results"][0]["message"])
r = qact("close_long", [A], symbol="EURUSD", only_symbol=True)
st = c.get("/api/state").json(); accA = next(a for a in st["accounts"] if a["id"] == A)
check("快捷：平全部多（仅当前品种）只平 EURUSD 多单", r["ok"] == 1 and "平多（EURUSD）" in r["results"][0]["message"]
      and not [p for p in accA["positions"] if p["symbol"] == "EURUSD" and p["side"] == "buy"]
      and [p for p in accA["positions"] if p["symbol"] == "GBPUSD" and p["side"] == "buy"]
      and len([p for p in accA["positions"] if p["side"] == "sell"]) == 2, r["results"][0]["message"])
q = qsum("EURUSD", [B])
check("快捷：另一个账户没被平（账户子集）", q["long"]["n"] == 4 and q["short"]["n"] == 2, (q["long"]["n"], q["short"]["n"]))
r = qact("close_long", [A], symbol="EURUSD", only_symbol=False)
st = c.get("/api/state").json(); accA = next(a for a in st["accounts"] if a["id"] == A)
check("快捷：平全部多（全部品种）连 GBPUSD 一起平", r["ok"] == 1 and not [p for p in accA["positions"] if p["side"] == "buy"], r["results"][0]["message"])
r = qact("close_short", [A], symbol="EURUSD", only_symbol=True)
q = qsum("EURUSD", [A])
check("快捷：平全部空", r["ok"] == 1 and "平空（EURUSD）" in r["results"][0]["message"] and q["short"]["n"] == 0, r["results"][0]["message"])
r = qact("close_profit", [B], symbol="EURUSD", only_symbol=True)
check("快捷：平所有盈利（按品种）", r["ok"] == 1 and "平盈利单（EURUSD）" in r["results"][0]["message"], r["results"][0]["message"])
r = qact("close_loss", [B], symbol="EURUSD", only_symbol=True)
check("快捷：平所有亏损（按品种）", r["ok"] == 1 and "平亏损单（EURUSD）" in r["results"][0]["message"], r["results"][0]["message"])
r = qact("buy", ids[:1], symbol="EURUSD", lots=5, scale_mode="fixed")
check("快捷：单笔手数保护", r["fail"] == 1 and "手数保护" in r["results"][0]["message"], r["results"][0]["message"])
r = qact("pair", ids, symbol="EURUSD", lots=1, scale_mode="fixed")
check("快捷：一键开单按两倍手数计算批量上限，整批拦截", r["fail"] == 6 and "超过批量上限" in r["results"][0]["message"], r["results"][0]["message"])
r = c.post("/api/quick/action", json={"action": "buy", "ids": [A], "symbol": "EUR USD", "lots": 0.01})
check("快捷：品种格式校验", r.status_code == 400)
r = c.post("/api/quick/action", json={"action": "boom", "ids": [A]})
check("快捷：未知操作被拒绝", r.status_code == 400)
r = c.post("/api/quick/action", json={"action": "buy", "ids": [], "symbol": "EURUSD", "lots": 0.01})
check("快捷：没选账户被拒绝", r.status_code == 400)
# 核弹级全平：持仓（所有品种）+ 挂单
c.post("/api/trade/order", json={"ids": [B], "order": {"symbol": "EURUSD", "kind": "buy_limit", "lots": 0.01, "price": 1.0, "magic": 1}})
qact("buy", [B], symbol="GBPUSD", lots=0.01, scale_mode="fixed")
time.sleep(1.6)
st = c.get("/api/state").json(); accB = next(a for a in st["accounts"] if a["id"] == B)
check("核弹前：有持仓和挂单", accB["positions"] and accB["orders"], (len(accB["positions"]), len(accB["orders"])))
r = qact("nuke", [B])
st = c.get("/api/state").json(); accB = next(a for a in st["accounts"] if a["id"] == B); accA = next(a for a in st["accounts"] if a["id"] == ids[0])
check("快捷：核弹级全平（全部品种 + 撤挂单）", r["ok"] == 1 and not accB["positions"] and not accB["orders"] and "撤挂单" in r["results"][0]["message"], r["results"][0]["message"])
check("快捷：核弹只影响选中的账户", len(accA["positions"]) > 0, len(accA["positions"]))
# 自动全平：对冲单的合计浮动 = -2×点差（与价格走势无关），用它做确定的触发测试
r = c.post("/api/quick/auto", json={"amount": -5, "ids": [B]}).json()
check("自动全平：启用（-5 止损）", r["auto"] and r["auto"]["amount"] == -5 and r["auto"]["ids"] == [B], r)
import os as _os
DATA = _os.environ.get("FLEET_TEST_DATA", "")
if DATA:
    check("自动全平：状态保存到数据目录", _os.path.exists(_os.path.join(DATA, "quick_auto.json")))
q = qsum("EURUSD", [B], wait_quote=False)
check("自动全平：面板显示已启用", q["auto"] and q["auto"]["amount"] == -5 and q["auto"]["online"] == 1, q["auto"])
r = qact("pair", [B], symbol="XAUUSD", lots=0.5, scale_mode="fixed")
check("自动全平：开对冲单（合计浮动约 -21.6）", r["ok"] == 1, r["results"][0]["message"][:100])
for _ in range(30):
    q = qsum("EURUSD", [B], wait_quote=False)
    if not q["auto"] and q["autoLast"]:
        break
    time.sleep(0.5)
st = c.get("/api/state").json(); accB = next(a for a in st["accounts"] if a["id"] == B)
check("自动全平：达到金额后触发一次并取消", not q["auto"] and q["autoLast"] and q["autoLast"]["ok"] == 1 and q["autoLast"]["net"] <= -5, (q["auto"], q["autoLast"]))
check("自动全平：触发后持仓已全平", not accB["positions"], len(accB["positions"]))
logs_ = c.get("/api/logs").json()
check("自动全平：日志有触发记录", any(x["action"] == "自动全平" and "触发" in x["message"] for x in logs_) and any(x["action"] == "自动全平（核弹级）" for x in logs_))
if DATA:
    check("自动全平：触发后删除保存的状态", not _os.path.exists(_os.path.join(DATA, "quick_auto.json")))
qact("pair", [B], symbol="XAUUSD", lots=0.5, scale_mode="fixed")
time.sleep(1.6)
r = c.post("/api/quick/auto", json={"amount": -1, "ids": [B]})
check("自动全平：已经达到金额时拒绝启用（避免立刻全平）", r.status_code == 400 and "已经达到" in r.json()["detail"], r.text[:120])
r = c.post("/api/quick/auto", json={"amount": 99999, "ids": [B]}).json()
check("自动全平：止盈方向启用", r["auto"]["amount"] == 99999)
r = c.post("/api/quick/auto", json={"amount": 0}).json()
check("自动全平：取消", r["auto"] is None and not c.get("/api/quick/summary", params={"accounts": B}).json()["auto"])
qact("nuke", [B])
r = c.post("/api/settings", json={"quick_no_confirm": True}).json()
check("设置：快捷面板免确认（默认关）", r["quick_no_confirm"] is True)
c.post("/api/settings", json={"quick_no_confirm": False})
html3 = c.get("/").text
check("交易页有快捷面板和高级折叠区", all(k in html3 for k in ("快捷交易面板", "一键开多", "一键开单", "核弹级全平", "仅当前品种", 'id="tradeAdv"', "setQuickNC", "/static/quick.js")))

# 未登录账户
r = c.post("/api/trade/close", json={"ids": [new_id], "filter": {"mode": "all"}}).json()
check("未登录账户记为失败", r["fail"] == 1 and r["results"][0]["message"] == "终端未登录")

# 策略
r = c.post("/api/strategy/deploy", json={"ids": ids[:2], "strategy": {"fileName": "Test.ex5", "symbol": "XAUUSD", "timeframe": "H1", "magic": 5, "preset": ""}}).json()
check("部署策略", r["ok"] == 2, r["results"][0]["message"])
st = c.get("/api/state").json()
byid = {a["id"]: a for a in st["accounts"]}
check("策略已记录为运行", any(s["fileName"] == "Test.ex5" and s["running"] for s in byid[ids[0]]["strategies"]))
r = c.post("/api/strategy/stop", json={"ids": ids[:2]}).json()
check("停止策略", r["ok"] == 2, r["results"][0]["message"])
r = c.post("/api/strategy/strip", json={"ids": ids[1:2]}).json()
st = c.get("/api/state").json()
check("移除策略", {a["id"]: a for a in st["accounts"]}[ids[1]]["strategies"] == [])
files = {"file": ("Demo.ex5", b"\x00\x01fake", "application/octet-stream")}
r = c.post("/api/strategy/upload", files=files)
check("上传 EX5", r.status_code == 200 and "Demo.ex5" in c.get("/api/state").json()["library"])
r = c.post("/api/strategy/upload", files={"file": ("x.exe", b"1", "application/octet-stream")})
check("拒绝非 ex5/set", r.status_code == 400)

# 收益
h = c.post("/api/history", json={}).json()
check("读取历史", h["accounts"] == 6 and len(h["deals"]) > 50, f"{len(h['deals'])} deals")

# 收益台
for _ in range(40):
    dk = c.get("/api/desk").json()
    if dk["equity"] and dk.get("deals") and len(dk["deals"]) == 6:
        break
    time.sleep(0.5)
check("收益台：净值采样", len(dk["equity"]) >= 1 and dk["equity"][-1][4] == 6, dk["equity"][-1:] if dk["equity"] else "无")
check("收益台：平仓缓存", len(dk.get("deals") or {}) == 6 and sum(len(v) for v in dk["deals"].values()) > 50)
check("收益台：活动流", dk["eventsTotal"] > 10 and any(e["kind"] == "open" for e in dk["events"]), dk["eventsTotal"])
dk2 = c.get(f"/api/desk?since={dk['eventSeq']}&eq_since={dk['equity'][-1][0]}&deals_v={dk['dealsVersion']}").json()
check("收益台：增量（不重复发送）", "deals" not in dk2 and all(e["seq"] > dk["eventSeq"] for e in dk2["events"]))
check("收益台：无令牌拒绝", httpx.get(BASE + "/api/desk").status_code == 401)

# CSV 导入
csv_text = "alias,login,password,server,group\n导入一,40001111,pw,Broker-Demo,测试\n坏行,abc,pw,X,测试\n"
r = c.post("/api/accounts/import", json={"text": csv_text}).json()
check("CSV 导入 1 个，坏行报错", r["added"] == 1 and len(r["errors"]) == 1, r)

# 日志
logs = c.get("/api/logs").json()
check("日志有记录", len(logs) > 20, len(logs))
csv_ = c.get("/api/logs.csv")
check("日志 CSV 导出带 BOM", csv_.status_code == 200 and csv_.content.startswith("\ufeff".encode()))

# 停用 / 删除 / 断开
r = c.post(f"/api/accounts/{ids[5]}/toggle").json()
check("停用账户", r["enabled"] is False)
r = c.post("/api/trade/close", json={"ids": [ids[5]], "filter": {"mode": "all"}}).json()
check("停用账户操作失败", r["fail"] == 1 and r["results"][0]["message"] == "已停用")
c.post(f"/api/accounts/{ids[5]}/toggle")
r = c.post("/api/disconnect", json={"ids": ids}).json()
check("批量断开", r["fail"] == 0)
time.sleep(1)
st = c.get("/api/state").json()
check("全部离线", all(a["link"] != "online" for a in st["accounts"]))
r = c.post("/api/accounts/delete", json={"ids": [new_id]}).json()
check("删除账户", r["removed"] == 1)
# 添加并登录（一键登录）
r = c.post("/api/accounts/quick", json={"text": "9001001,pw1,Quick-Server,快测,一号\n9001002,wrong,Quick-Server,快测\nabc,1,2\n9001003,noalgo,Quick-Server"}).json()
check("添加并登录：2 成功 1 失败", r["ok"] == 2 and r["fail"] == 1 and r["added"] == 3, f"{r['ok']}/{r['fail']} {r['added']}")
check("添加并登录：坏行报错", any("abc" in e for e in r["errors"]), r["errors"])
st = c.get("/api/state").json()
prog = st["progress"]
check("添加并登录：进度四步", prog["title"] == "添加并登录" and [k for k, _ in prog["steps"]] == ["copy", "launch", "login", "algo"] and prog["done"])
items = {prog["items"][i]["login"]: prog["items"][i] for i in prog["order"]}
check("添加并登录：失败停在登录并有中文说明", items["9001002"]["steps"]["login"]["s"] == "fail" and "授权失败" in items["9001002"]["message"])
check("添加并登录：算法交易没开时自动打开", items["9001003"]["steps"]["algo"]["s"] == "ok" and "已自动打开算法交易" in items["9001003"]["steps"]["algo"]["m"], items["9001003"]["steps"]["algo"])
check("服务器名进入自动补全（坏行不算）", "Quick-Server" in st["servers"] and "2" not in st["servers"])
r = c.post("/api/accounts/quick", json={"rows": [{"login": "9001002", "password": "good", "server": "Quick-Server"}]}).json()
check("已在列表的账户：只更新密码并登录", r["ok"] == 1 and r["updated"] == 1 and r["added"] == 0, r["results"][0]["message"])
r = c.post("/api/accounts/quick", json={"text": "就一列"})
check("批量粘贴格式错误被拒绝", r.status_code == 400)

# 一键分发
c.post("/api/strategy/upload", files={"file": ("Pulse.ex5", b"EX5" * 40)})
c.post("/api/strategy/upload", files={"file": ("small.set", b"Lots=0.01\r\n")})
st = c.get("/api/state").json()
qids = [a["id"] for a in st["accounts"] if a["group"] == "快测"] + [a["id"] for a in st["accounts"] if a["login"] == "9001003"]
c.post("/api/disconnect", json={"ids": qids[:1]})
r = c.post("/api/strategy/distribute", json={"ids": qids, "strategy": {"fileName": "Pulse.ex5", "symbol": "XAUUSD", "timeframe": "H1", "magic": 700, "preset": ""},
                                             "magic_step": 1, "group_presets": {"快测": "small.set"}, "concurrency": 2}).json()
check("分发到 3 个账户（含 1 个先自动登录）", r["ok"] == 3 and r["fail"] == 0, [x["message"] for x in r["results"]])
st = c.get("/api/state").json()
by = {a["id"]: a for a in st["accounts"]}
mag = [[(x["magic"], x["preset"]) for x in by[i]["strategies"] if x["fileName"] == "Pulse.ex5"] for i in qids]
check("魔术号自增 + 按分组参数文件", mag == [[(700, "small.set")], [(701, "small.set")], [(702, "")]], mag)
prog = st["progress"]
check("分发进度五步全绿", [k for k, _ in prog["steps"]] == ["login", "files", "restart", "attach", "verify"]
      and all(all(v["s"] == "ok" for v in prog["items"][i]["steps"].values()) for i in qids))
r = c.post("/api/strategy/distribute", json={"ids": qids[:1], "strategy": {"fileName": "Pulse.ex5", "symbol": "EURUSD", "timeframe": "M15", "magic": 800}}).json()
st = c.get("/api/state").json()
check("再次分发替换同名 EA（不重复记录）", [(x["symbol"], x["magic"]) for x in next(a for a in st["accounts"] if a["id"] == qids[0])["strategies"]] == [("EURUSD", 800)])
r = c.post("/api/strategy/distribute", json={"ids": qids, "strategy": {"fileName": "Pulse.ex5", "symbol": "XAUUSD", "timeframe": "H9", "magic": 1}})
check("分发参数校验（周期）", r.status_code == 400)
r = c.post("/api/strategy/distribute", json={"ids": qids, "strategy": {"fileName": "Pulse.ex5", "symbol": "XAUUSD", "timeframe": "H1", "magic": 1}, "magic_param": "bad name"})
check("分发参数校验（参数名）", r.status_code == 400)
r = c.post("/api/strategy/stop", json={"ids": qids}).json()
check("一键停止", r["ok"] == 3 and c.get("/api/state").json()["progress"]["title"] == "停止策略", [x["message"] for x in r["results"]])

# 默认品种 / 周期（设置里可改）
ss = c.get("/api/state").json()["settings"]
check("默认品种 USDJPYc、周期 M15、只开一个图表", ss.get("default_symbol") == "USDJPYc" and ss.get("default_timeframe") == "M15"
      and ss.get("single_chart") is True and ss.get("auto_reattach") is True, {k: ss.get(k) for k in ("default_symbol", "default_timeframe", "single_chart")})
r = c.post("/api/settings", json={"default_timeframe": "M7"})
check("默认周期校验", r.status_code == 400 and "默认周期" in r.json().get("detail", ""), r.text[:80])
r = c.post("/api/settings", json={"default_symbol": "USD JPY"})
check("默认品种校验", r.status_code == 400, r.text[:80])
r = c.post("/api/settings", json={"default_symbol": "EURUSDc", "default_timeframe": "h1"})
check("修改默认品种和周期", r.status_code == 200 and r.json()["default_symbol"] == "EURUSDc" and r.json()["default_timeframe"] == "H1")
c.post("/api/settings", json={"default_symbol": "USDJPYc", "default_timeframe": "M15"})
html2 = c.get("/").text
check("设置页有默认品种和周期", all(k in html2 for k in ("setDefSym", "setDefTf", "默认品种")))

# 品种后缀检查：美分账户只有带 c 的品种
r = c.post("/api/accounts/quick", json={"text": "9002001,pw,Exness-MT5Cent,美分\n"}).json()
check("美分账户登录", r["ok"] == 1, r["results"][0]["message"])
cent = next(a["id"] for a in c.get("/api/state").json()["accounts"] if a["login"] == "9002001")
r = c.post("/api/strategy/distribute", json={"ids": [cent], "strategy": {"fileName": "Pulse.ex5", "symbol": "USDJPY", "timeframe": "M15", "magic": 9}}).json()
check("USDJPY 自动改用 USDJPYc", r["ok"] == 1 and "已自动改用 USDJPYc" in r["results"][0]["message"], r["results"][0]["message"])
st = c.get("/api/state").json()
check("台账记录实际品种 USDJPYc", [x["symbol"] for x in next(a for a in st["accounts"] if a["id"] == cent)["strategies"]] == ["USDJPYc"])
r = c.post("/api/strategy/distribute", json={"ids": [cent], "strategy": {"fileName": "Pulse.ex5", "symbol": "GOLD", "timeframe": "M15", "magic": 9}}).json()
check("没有的品种分发失败并说明", r["fail"] == 1 and "没有品种 GOLD" in r["results"][0]["message"], r["results"][0]["message"])
r = c.post("/api/trade/order", json={"ids": [cent], "order": {"symbol": "XAUUSD", "kind": "buy", "lots": 0.01, "magic": 9}}).json()
check("下单品种不存在时给出带后缀的建议", r["fail"] == 1 and "XAUUSDc" in r["results"][0]["message"], r["results"][0]["message"])
r = c.post("/api/trade/order", json={"ids": [cent], "order": {"symbol": "USDJPYc", "kind": "buy", "lots": 0.01, "magic": 9}}).json()
check("EA 运行中手动下单正常", r["ok"] == 1, r["results"][0]["message"])
r = c.post("/api/quick/action", json={"action": "buy", "ids": [cent], "symbol": "USDJPY", "lots": 0.01}).json()
check("快捷面板：美分账户 USDJPY 自动改用 USDJPYc", r["ok"] == 1 and "已自动改用 USDJPYc" in r["results"][0]["message"], r["results"][0]["message"])
for _ in range(20):
    q = c.get("/api/quick/summary", params={"symbol": "USDJPY", "accounts": cent}).json()
    if q["quote"]:
        break
    time.sleep(0.4)
check("快捷面板：美分账户报价和持仓按 USDJPYc 统计", q["quote"] and q["quote"]["symbol"] == "USDJPYc" and q["accounts"][0]["symbol"] == "USDJPYc"
      and q["long"]["lots"] >= 0.02, (q["quote"], q["long"]))
r = c.post("/api/quick/action", json={"action": "close_long", "ids": [cent], "symbol": "USDJPY", "only_symbol": True}).json()
check("快捷面板：仅当前品种平仓也认 USDJPYc", r["ok"] == 1 and "成功平掉 2/2" in r["results"][0]["message"], r["results"][0]["message"])

r = c.post("/api/accounts/delete", json={"ids": qids + [cent]}).json()
check("清理测试账户", r["removed"] == 4)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
sys.exit(1 if fails else 0)
