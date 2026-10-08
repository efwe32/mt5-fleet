"""测试用的假 MetaTrader5 模块（--mock 模式用，Linux/没有 MT5 时也能测试界面和批量操作）。

接口和字段名与官方 MetaTrader5 Python 包保持一致（只实现本程序用到的部分）。
每个 worker 进程各自有一份独立的假终端状态。
"""
from __future__ import annotations

import math
import os
import random
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone

# ---- 常量（与官方包取值一致） ----
ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT = 2, 3
ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP = 4, 5
POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
TRADE_ACTION_DEAL, TRADE_ACTION_PENDING = 1, 5
TRADE_ACTION_SLTP, TRADE_ACTION_MODIFY, TRADE_ACTION_REMOVE = 6, 7, 8
ORDER_TIME_GTC = 0
ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
DEAL_TYPE_BUY, DEAL_TYPE_SELL, DEAL_TYPE_BALANCE = 0, 1, 2
DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY = 0, 1, 2, 3
TRADE_RETCODE_DONE, TRADE_RETCODE_PLACED = 10009, 10008
TIMEFRAME_H1 = 16385

AccountInfo = namedtuple("AccountInfo", "login trade_mode leverage limit_orders margin_so_mode trade_allowed trade_expert "
                         "margin_mode currency_digits fifo_close balance credit profit equity margin margin_free "
                         "margin_level margin_so_call margin_so_so margin_initial margin_maintenance assets liabilities "
                         "commission_blocked name server currency company")
TerminalInfo = namedtuple("TerminalInfo", "community_account community_connection connected dlls_allowed trade_allowed "
                          "tradeapi_disabled email_enabled ftp_enabled notifications_enabled mqid build maxbars "
                          "codepage ping_last community_balance retransmission company name language path data_path commondata_path")
TradePosition = namedtuple("TradePosition", "ticket time time_msc time_update time_update_msc type magic identifier reason "
                           "volume price_open sl tp price_current swap profit symbol comment external_id")
TradeOrder = namedtuple("TradeOrder", "ticket time_setup time_setup_msc time_done time_done_msc time_expiration type type_time "
                        "type_filling state magic position_id position_by_id reason volume_initial volume_current "
                        "price_open sl tp price_current price_stoplimit symbol comment external_id")
TradeDeal = namedtuple("TradeDeal", "ticket order time time_msc type entry magic position_id reason volume price commission "
                       "swap profit fee symbol comment external_id")
SymbolInfo = namedtuple("SymbolInfo", "name visible digits point trade_contract_size volume_min volume_max volume_step "
                        "filling_mode trade_mode bid ask trade_stops_level currency_profit")
Tick = namedtuple("Tick", "time bid ask last volume time_msc flags volume_real")
OrderSendResult = namedtuple("OrderSendResult", "retcode deal order volume price bid ask comment request_id "
                             "retcode_external request")
OrderCheckResult = namedtuple("OrderCheckResult", "retcode balance equity profit margin margin_free margin_level comment request")

SPECS = {
    "XAUUSD": dict(contract=100, digits=2, step=0.18, leverage=100, price=2654.32),
    "EURUSD": dict(contract=1e5, digits=5, step=8e-5, leverage=100, price=1.08462),
    "GBPUSD": dict(contract=1e5, digits=5, step=1e-4, leverage=100, price=1.27115),
    "USDJPY": dict(contract=1e5, digits=3, step=0.012, leverage=100, price=149.624),
    "NAS100": dict(contract=1, digits=1, step=3.2, leverage=50, price=20184.5),
    "US30": dict(contract=1, digits=1, step=4, leverage=50, price=42310.2),
    "BTCUSD": dict(contract=1, digits=1, step=28, leverage=20, price=64250.4),
}

SEED = {
    "51002811": (48200, [("XAUUSD", 0, 0.2, 2638.1, 88001, "趋势"), ("XAUUSD", 1, 0.1, 2661.4, 88001, "对冲")]),
    "51002844": (22640, [("EURUSD", 0, 0.5, 1.0812, 88002, "突破"), ("GBPUSD", 1, 0.3, 1.2748, 88002, "英镑")]),
    "88011203": (10500, [("NAS100", 0, 1.0, 20040, 33011, "跟单"), ("BTCUSD", 0, 0.05, 62800, 33011, "跟单")]),
    "88011290": (9800, [("NAS100", 1, 0.5, 20310, 33011, "跟单"), ("EURUSD", 0, 0.2, 1.0861, 33012, "手工")]),
    "100245": (10000, [("USDJPY", 0, 0.4, 149.1, 1001, "试单")]),
    "7720011": (15000, [("US30", 0, 0.5, 42100, 77001, "备用")]),
}


class MockMT5:
    def __init__(self):
        self._connected = False
        self._err = (1, "Success")
        self._login = 0
        self._server = ""
        self._path = ""
        self._balance = 10000.0
        self._prices = {k: v["price"] for k, v in SPECS.items()}
        self._last_tick = time.time()
        self._positions: dict[int, dict] = {}
        self._orders: dict[int, dict] = {}
        self._deals: list[dict] = []
        self._ticket = 900000 + random.randint(0, 9999) * 10
        self._rng = random.Random()
        # 测试模式下的“自动 EA”：随机开/平小单，让收益台有真实的数据流（测试时可用环境变量关闭）
        self._auto = os.environ.get("FLEET_MOCK_AUTOTRADE", "1") != "0"
        self._force_profit = None  # 测试：固定浮亏，用来触发警报

    # 让实例看起来像模块：常量
    def __getattr__(self, name):
        g = globals()
        if name.isupper() and name in g:
            return g[name]
        raise AttributeError(name)

    # ---------------- 生命周期 ----------------
    def initialize(self, path=None, login=None, password=None, server=None, timeout=60000, portable=False):
        time.sleep(0.3 + random.random() * 0.5)
        if password == "wrong":
            self._err = (-6, "Terminal: Authorization failed")
            return False
        self._path = path or "C:\\Program Files\\MetaTrader 5\\terminal64.exe"
        self._connected = True
        if login:
            return self.login(login, password, server)
        return True

    def login(self, login, password=None, server=None, timeout=60000):
        if password == "wrong":
            self._err = (-6, "Terminal: Authorization failed")
            return False
        self._login = int(login)
        self._algo = password != "noalgo"     # 测试用：模拟“算法交易没开”的终端
        self._server = server or "测试服务器"
        bal, seeds = SEED.get(str(login), (10000, []))
        if not self._positions and not self._deals:
            self._balance = float(bal)
            for sym, typ, vol, op, magic, cmt in seeds:
                self._ticket += 1
                self._positions[self._ticket] = dict(ticket=self._ticket, symbol=sym, type=typ, volume=vol,
                                                     price_open=op, sl=0.0, tp=0.0, magic=magic, comment=cmt,
                                                     time=int(time.time()) - 3600)
        self._err = (1, "Success")
        return True

    def shutdown(self):
        self._connected = False
        return True

    def last_error(self):
        return self._err

    def version(self):
        return (500, 4620, "mock")

    # ---------------- 行情 ----------------
    def _tick(self):
        now = time.time()
        n = min(20, int((now - self._last_tick) / 0.5))
        if n <= 0:
            return
        self._last_tick = now
        for _ in range(n):
            for s, spec in SPECS.items():
                p = self._prices[s] + (self._rng.random() - 0.5) * 2 * spec["step"]
                self._prices[s] = round(max(spec["step"], p), spec["digits"])
            if self._auto and self._login and self._rng.random() < 1 / 30:
                self._auto_trade()
        self._check_sltp()

    AUTO_MAGIC = 909090

    def _auto_trade(self):
        mine = [p for p in self._positions.values() if p["magic"] == self.AUTO_MAGIC]
        if mine and (len(mine) >= 3 or self._rng.random() < 0.45):
            p = self._rng.choice(mine)
            pr, cur = self._profit(p)
            # 给一点随机滑动，让平仓盈亏分布更像真实 EA
            pr = round(pr + (self._rng.gauss(0.15, 1.0)) * 12 * p["volume"] / 0.05, 2)
            self._balance += pr
            self._ticket += 1
            self._deals.append(dict(ticket=self._ticket, time=int(time.time()), type=1 - p["type"], entry=DEAL_ENTRY_OUT,
                                    magic=p["magic"], position=p["ticket"], volume=p["volume"], price=cur,
                                    profit=pr, symbol=p["symbol"]))
            del self._positions[p["ticket"]]
            return
        sym = self._rng.choice(["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "NAS100", "BTCUSD", "US30"])
        if sym not in SPECS:
            return
        typ = self._rng.randint(0, 1)
        bid, ask = self._bidask(sym)
        self._ticket += 1
        self._positions[self._ticket] = dict(ticket=self._ticket, symbol=sym, type=typ,
                                             volume=self._rng.choice([0.01, 0.02, 0.03, 0.05]),
                                             price_open=ask if typ == 0 else bid, sl=0.0, tp=0.0,
                                             magic=self.AUTO_MAGIC, comment="AutoEA", time=int(time.time()))

    def _cent(self) -> bool:
        """测试用：服务器名带 Cent 的账户，品种都带 c 后缀（像 Exness 美分账户 USDJPYc），没有不带后缀的写法。"""
        return "cent" in (self._server or "").lower()

    def _base(self, symbol):
        symbol = symbol or ""
        if self._cent():
            return symbol[:-1] if symbol.endswith("c") and symbol[:-1] in SPECS else None
        for s in SPECS:
            if symbol == s or symbol.startswith(s):
                return s
        return None

    def symbols_get(self, group=None):
        names = [k + "c" for k in SPECS] if self._cent() else list(SPECS)
        return tuple(SymbolInfo(n, True, SPECS[self._base(n)]["digits"], 0.0, 0, 0.01, 100.0, 0.01, 3, 4, 0.0, 0.0, 10, "USD")
                     for n in names)

    def _bidask(self, symbol):
        b = self._base(symbol)
        spec = SPECS[b]
        bid = self._prices[b]
        ask = round(bid + spec["step"] * 0.6, spec["digits"])
        return bid, ask

    def symbol_select(self, symbol, enable=True):
        return self._base(symbol) is not None

    def symbol_info(self, symbol):
        b = self._base(symbol)
        if not b:
            self._err = (-1, f"symbol {symbol} not found")
            return None
        self._tick()
        spec = SPECS[b]
        bid, ask = self._bidask(symbol)
        return SymbolInfo(symbol, True, spec["digits"], 10 ** -spec["digits"], spec["contract"], 0.01, 100.0, 0.01,
                          3, 4, bid, ask, 10, "USD")

    def symbol_info_tick(self, symbol):
        if not self._base(symbol):
            return None
        self._tick()
        bid, ask = self._bidask(symbol)
        t = int(time.time())
        return Tick(t, bid, ask, 0.0, 0, t * 1000, 6, 0.0)

    # ---------------- 账户 ----------------
    def _profit(self, p):
        b = self._base(p["symbol"])
        spec = SPECS[b]
        bid, ask = self._bidask(p["symbol"])
        cur = bid if p["type"] == 0 else ask
        d = (cur - p["price_open"]) * (1 if p["type"] == 0 else -1) * spec["contract"] * p["volume"]
        if b == "USDJPY":
            d /= max(cur, 0.001)
        return round(d, 2), cur

    def _margin(self):
        m = 0.0
        for p in self._positions.values():
            b = self._base(p["symbol"])
            spec = SPECS[b]
            px = 1.0 if b == "USDJPY" else self._prices[b]
            m += p["volume"] * spec["contract"] * px / spec["leverage"]
        return round(m, 2)

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        b = self._base(symbol)
        if not b:
            return None
        px = self._prices[b]
        now = int(time.time())
        out = []
        n = int(count)
        for i in range(n):
            age = (n - 1 - i) + int(start_pos)
            close = px if i == n - 1 else round(px * 0.998, 5)
            out.append({"time": now - age * 3600, "open": close, "high": close, "low": close,
                        "close": close, "tick_volume": 10})
        return out

    def account_info(self):
        if not self._connected:
            self._err = (-10004, "No IPC connection")
            return None
        self._tick()
        if self._force_profit is not None:
            profit = round(float(self._force_profit), 2)
        else:
            profit = round(sum(self._profit(p)[0] for p in self._positions.values()), 2)
        equity = round(self._balance + profit, 2)
        margin = self._margin()
        level = round(equity / margin * 100, 2) if margin else 0.0
        return AccountInfo(self._login, 0, 100, 200, 0, True, True, 2, 2, False, round(self._balance, 2), 0.0, profit,
                           equity, margin, round(equity - margin, 2), level, 100.0, 50.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                           "测试", self._server, "USD", "测试券商")

    def terminal_info(self):
        if not self._connected:
            return None
        return TerminalInfo(False, False, True, False, getattr(self, "_algo", True), False, False, False, False, 0, 4620, 100000, 936, 12000,
                            0.0, 0.0, "MetaQuotes Ltd.", "MetaTrader 5", "Chinese", self._path.rsplit("\\", 1)[0],
                            self._path.rsplit("\\", 1)[0], "")

    def positions_get(self, symbol=None, group=None, ticket=None):
        if not self._connected:
            return None
        self._tick()
        out = []
        for p in self._positions.values():
            if symbol and p["symbol"] != symbol:
                continue
            if ticket and p["ticket"] != ticket:
                continue
            pr, cur = self._profit(p)
            out.append(TradePosition(p["ticket"], p["time"], p["time"] * 1000, p["time"], p["time"] * 1000, p["type"],
                                     p["magic"], p["ticket"], 3, p["volume"], p["price_open"], p["sl"], p["tp"], cur,
                                     0.0, pr, p["symbol"], p["comment"], ""))
        return tuple(out)

    def orders_get(self, symbol=None, group=None, ticket=None):
        if not self._connected:
            return None
        out = []
        for o in self._orders.values():
            if symbol and o["symbol"] != symbol:
                continue
            bid, ask = self._bidask(o["symbol"])
            out.append(TradeOrder(o["ticket"], o["time"], o["time"] * 1000, 0, 0, 0, o["type"], 0, 2, 1, o["magic"], 0, 0,
                                  3, o["volume"], o["volume"], o["price"], o["sl"], o["tp"], bid, 0.0, o["symbol"],
                                  o["comment"], ""))
        return tuple(out)

    def history_deals_get(self, date_from, date_to, group=None):
        if not self._connected:
            return None
        ts_from = date_from.timestamp() if isinstance(date_from, datetime) else float(date_from)
        ts_to = date_to.timestamp() if isinstance(date_to, datetime) else float(date_to)
        seeded = []
        # 用账号做种子生成一份确定的历史平仓记录
        rnd = random.Random(self._login)
        start = datetime(datetime.now().year, 1, 1, tzinfo=timezone.utc)
        day = start
        tk = 100000
        today = datetime.now(timezone.utc)
        while day < today - timedelta(hours=1):
            if day.weekday() < 5 and rnd.random() > 0.35:
                for _ in range(rnd.randint(1, 3)):
                    tk += 1
                    prof = round((rnd.random() - 0.42) * self._balance * 0.006, 2)
                    t = int(day.timestamp()) + rnd.randint(3600, 80000)
                    sym = rnd.choice(list(SPECS))
                    seeded.append(TradeDeal(tk, tk, t, t * 1000, 1, DEAL_ENTRY_OUT, 0, tk, 0, 0.1, SPECS[sym]["price"],
                                            -0.7, 0.0, prof, 0.0, sym, "", ""))
            day += timedelta(days=1)
        live = [TradeDeal(d["ticket"], d["ticket"], d["time"], d["time"] * 1000, d["type"], d["entry"], d["magic"],
                          d["position"], 3, d["volume"], d["price"], 0.0, 0.0, d["profit"], 0.0, d["symbol"], "", "")
                for d in self._deals]
        return tuple(d for d in seeded + live if ts_from <= d.time < ts_to)

    # ---------------- 交易 ----------------
    def _result(self, retcode, comment, request, deal=0, order=0, volume=0.0, price=0.0):
        bid, ask = (0.0, 0.0)
        if request.get("symbol") and self._base(request["symbol"]):
            bid, ask = self._bidask(request["symbol"])
        return OrderSendResult(retcode, deal, order, volume, price, bid, ask, comment, 1, 0, request)

    def _validate(self, req):
        if not self._connected:
            return 10031, "no connection"
        sym = req.get("symbol")
        if req["action"] in (TRADE_ACTION_DEAL, TRADE_ACTION_PENDING):
            if not self._base(sym or ""):
                return 10013, "unknown symbol"
            vol = float(req.get("volume", 0))
            if vol < 0.01 or vol > 100 or abs(round(vol / 0.01) - vol / 0.01) > 1e-6:
                return 10014, "Invalid volume"
            if req.get("type_filling", 0) not in (0, 1, 2):
                return 10030, "Unsupported filling mode"
        return 0, "Done"

    def order_check(self, request):
        rc, cm = self._validate(request)
        acc = self.account_info()
        margin = 0.0
        if rc == 0 and request["action"] == TRADE_ACTION_DEAL and not request.get("position"):
            b = self._base(request["symbol"])
            spec = SPECS[b]
            margin = round(float(request["volume"]) * spec["contract"] * (1 if b == "USDJPY" else self._prices[b]) / spec["leverage"], 2)
            if margin > acc.margin_free:
                rc, cm = 10019, "No money"
        return OrderCheckResult(rc, acc.balance if acc else 0, acc.equity if acc else 0, acc.profit if acc else 0,
                                round((acc.margin if acc else 0) + margin, 2),
                                round((acc.margin_free if acc else 0) - margin, 2), 0.0, cm, request)

    def order_send(self, request):
        time.sleep(0.05 + random.random() * 0.15)
        self._tick()
        rc, cm = self._validate(request)
        if rc:
            return self._result(rc, cm, request)
        act = request["action"]
        if act == TRADE_ACTION_DEAL:
            bid, ask = self._bidask(request["symbol"])
            typ = int(request["type"])
            price = ask if typ == ORDER_TYPE_BUY else bid
            pos_ticket = request.get("position")
            self._ticket += 1
            if pos_ticket:
                p = self._positions.get(int(pos_ticket))
                if not p:
                    return self._result(10036, "Position doesn't exist", request)
                if float(request["volume"]) > p["volume"] + 1e-9:
                    return self._result(10038, "Close volume exceeds", request)
                pr, cur = self._profit({**p, "volume": float(request["volume"])})
                self._balance += pr
                p["volume"] = round(p["volume"] - float(request["volume"]), 2)
                if p["volume"] <= 0:
                    del self._positions[p["ticket"]]
                self._deals.append(dict(ticket=self._ticket, time=int(time.time()), type=typ, entry=DEAL_ENTRY_OUT,
                                        magic=p["magic"], position=p["ticket"], volume=float(request["volume"]),
                                        price=cur, profit=pr, symbol=p["symbol"]))
                return self._result(10009, "Request executed", request, self._ticket, self._ticket, float(request["volume"]), cur)
            self._positions[self._ticket] = dict(ticket=self._ticket, symbol=request["symbol"], type=typ,
                                                 volume=float(request["volume"]), price_open=price,
                                                 sl=float(request.get("sl") or 0), tp=float(request.get("tp") or 0),
                                                 magic=int(request.get("magic") or 0),
                                                 comment=str(request.get("comment") or ""), time=int(time.time()))
            return self._result(10009, "Request executed", request, self._ticket, self._ticket, float(request["volume"]), price)
        if act == TRADE_ACTION_SLTP:
            p = self._positions.get(int(request.get("position") or 0))
            if not p:
                return self._result(10036, "Position doesn't exist", request)
            p["sl"], p["tp"] = float(request.get("sl") or 0), float(request.get("tp") or 0)
            return self._result(10009, "Request executed", request)
        if act == TRADE_ACTION_PENDING:
            self._ticket += 1
            self._orders[self._ticket] = dict(ticket=self._ticket, symbol=request["symbol"], type=int(request["type"]),
                                              volume=float(request["volume"]), price=float(request["price"]),
                                              sl=float(request.get("sl") or 0), tp=float(request.get("tp") or 0),
                                              magic=int(request.get("magic") or 0),
                                              comment=str(request.get("comment") or ""), time=int(time.time()))
            return self._result(10008, "Request placed", request, 0, self._ticket)
        if act == TRADE_ACTION_REMOVE:
            if self._orders.pop(int(request.get("order") or 0), None) is None:
                return self._result(10013, "Invalid order", request)
            return self._result(10009, "Request executed", request)
        return self._result(10013, "Invalid request", request)

    def _check_sltp(self):
        for p in list(self._positions.values()):
            bid, ask = self._bidask(p["symbol"])
            cur = bid if p["type"] == 0 else ask
            hit = False
            if p["sl"]:
                hit = cur <= p["sl"] if p["type"] == 0 else cur >= p["sl"]
            if not hit and p["tp"]:
                hit = cur >= p["tp"] if p["type"] == 0 else cur <= p["tp"]
            if hit:
                pr, _ = self._profit(p)
                self._balance += pr
                self._ticket += 1
                self._deals.append(dict(ticket=self._ticket, time=int(time.time()), type=1 - p["type"],
                                        entry=DEAL_ENTRY_OUT, magic=p["magic"], position=p["ticket"],
                                        volume=p["volume"], price=cur, profit=pr, symbol=p["symbol"]))
                del self._positions[p["ticket"]]
