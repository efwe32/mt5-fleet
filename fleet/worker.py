"""单账户工作进程。

MetaTrader5 Python 包一个进程只能连接一个终端，所以每个账户跑一个独立进程，
各自指向自己的 terminal64.exe。进程内：初始化/登录 -> 定时轮询账户和持仓 ->
执行主进程发来的命令（下单、平仓、改止损止盈、撤挂单、部署/停止 EA、查历史）。
"""
from __future__ import annotations

import math
import os
import queue
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .retcodes import OK_RETCODES, error_msg, trade_msg

# 官方常量（mock 和真包取值一致，这里做兜底）
C = dict(ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1, ORDER_TYPE_BUY_LIMIT=2, ORDER_TYPE_SELL_LIMIT=3,
         ORDER_TYPE_BUY_STOP=4, ORDER_TYPE_SELL_STOP=5, TRADE_ACTION_DEAL=1, TRADE_ACTION_PENDING=5,
         TRADE_ACTION_SLTP=6, TRADE_ACTION_REMOVE=8, ORDER_TIME_GTC=0, ORDER_FILLING_FOK=0,
         ORDER_FILLING_IOC=1, ORDER_FILLING_RETURN=2, DEAL_ENTRY_OUT=1, DEAL_ENTRY_INOUT=2, DEAL_ENTRY_OUT_BY=3,
         DEAL_TYPE_BUY=0, DEAL_TYPE_SELL=1)

ORDER_KINDS = {
    "buy": ("ORDER_TYPE_BUY", "市价买入"), "sell": ("ORDER_TYPE_SELL", "市价卖出"),
    "buy_limit": ("ORDER_TYPE_BUY_LIMIT", "买入限价"), "sell_limit": ("ORDER_TYPE_SELL_LIMIT", "卖出限价"),
    "buy_stop": ("ORDER_TYPE_BUY_STOP", "买入止损"), "sell_stop": ("ORDER_TYPE_SELL_STOP", "卖出止损"),
}
CLOSE_LABEL = {"all": "全平", "long": "平多", "short": "平空", "symbol": "按品种平", "magic": "按魔术号平",
               "profit": "平盈利单", "loss": "平亏损单", "ticket": "平单笔"}


def load_mt5(mock: bool):
    if mock:
        from .mock_mt5 import MockMT5
        return MockMT5(), None
    try:
        import MetaTrader5 as mt5  # type: ignore
        return mt5, None
    except Exception as e:  # 没装 / 不是 Windows / 32 位 Python
        return None, f"无法导入 MetaTrader5 包：{e}。请在 Windows 64 位 Python 3.10+ 下运行「安装依赖.bat」"


class Worker:
    def __init__(self, acc: dict, password: str, settings: dict, cmd_q, evt_q, mock: bool, data_dir: str,
                 own: dict | None = None, root: str = ""):
        self.acc = acc
        self.password = password
        self.settings = settings
        self.cmd_q = cmd_q
        self.evt_q = evt_q
        self.mock = mock
        self.data_dir = Path(data_dir)
        self.mt5 = None
        self.link = "offline"
        self.link_error = ""
        self.running = True
        self.fail_polls = 0
        self.last_reconnect = 0.0
        self.was_online = False
        self.own = own            # 本程序启动的终端进程 {path,pid,create_time}
        self.root = Path(root) if root else None
        self._ini = None          # 本次启动用的一次性配置文件（含密码），登录后删除
        self.quiet = False        # 为 True 时不发进度（例如部署过程中的重新登录）
        self.reconnect_tries = 0
        # 快捷交易面板关注的品种（标准名）→ 每轮轮询附带报价；解析结果缓存
        self.watch: list[str] = [str(x) for x in (settings.get("_watch") or [])][:4]
        self._sym_cache: dict[str, tuple] = {}
        # 这次登录是用户点的（登录 / 分发）：算法交易没开就帮他打开。后台重连、接管不算
        self._explicit = bool(acc.pop("_explicit", False))

    # ---------------- 工具 ----------------
    def c(self, name):
        return getattr(self.mt5, name, C.get(name))

    def emit(self, **kw):
        kw.setdefault("id", self.acc["id"])
        try:
            self.evt_q.put(kw)
        except Exception:
            pass

    def status(self, snapshot=None):
        self.emit(type="status", link=self.link, linkError=self.link_error, snapshot=snapshot, ts=time.time())

    def err(self):
        try:
            return error_msg(self.mt5.last_error())
        except Exception:
            return "未知错误"

    # ---------------- 进度（网页上每个账户的步骤条） ----------------
    def progress(self, key: str, state: str, msg: str = ""):
        """state: run / ok / warn / fail / skip"""
        if self.quiet:
            return
        self.emit(type="progress", key=key, state=state, msg=msg[:300])

    # ---------------- 连接 ----------------
    def _fail(self, msg: str, key: str = "login"):
        self.link, self.link_error = "error", msg
        self.status()
        self.progress(key, "fail", msg)
        return False, msg

    def connect(self) -> tuple[bool, str]:
        self.link, self.link_error = "connecting", ""
        self.status()
        mt5, imp_err = load_mt5(self.mock)
        if mt5 is None:
            return self._fail(imp_err, "copy")
        self.mt5 = mt5
        path = self.acc.get("terminal_path") or ""
        try:
            login = int(self.acc["login"])
        except ValueError:
            return self._fail("账号不是数字", "copy")
        if not self.password:
            return self._fail("没有保存密码，请在「编辑」里填写密码", "copy")
        launched_before = self.own
        try:
            ok, msg = self.ensure_terminal(path)
            if not ok:
                self.link, self.link_error = "error", msg
                self.status()
                return False, msg
            just_launched = self.own is not None and self.own is not launched_before
            self.progress("login", "run", f"正在登录 {login} @ {self.acc['server']}")
            # 永远带上明确的终端路径 + 便携模式，绝不让 MetaTrader5 自己去找默认终端
            kwargs = dict(path=path, login=login, password=self.password, server=self.acc["server"],
                          timeout=60000, portable=True)
            ok = mt5.initialize(**kwargs)
            if not ok:
                first = self.err()
                code = self._err_code()
                if code in (-6, -10005, -10003, -10004, -1):
                    # 新副本可能还不认识这个券商服务器：终端会自己按服务器名搜索，等一会儿再试一次
                    self.progress("login", "run", f"第一次登录失败（{first}），终端可能正在搜索服务器，稍后重试一次…")
                    try:
                        mt5.shutdown()
                    except Exception:
                        pass
                    time.sleep(1.0 if self.mock else float(self.settings.get("login_retry_delay", 12)))
                    ok = mt5.initialize(**kwargs)
            if not ok:
                msg = "初始化/登录失败：" + self.err()
                if self._err_code() == -6:
                    msg += "。请核对账号、交易密码和服务器名；如果服务器名没错，可能是这个终端副本还不认识该券商的服务器，" \
                           "请在设置页「导入服务器列表」后再登录"
                try:
                    mt5.shutdown()
                except Exception:
                    pass
                if just_launched and not self.was_online:
                    self.close_own_terminal()  # 刚才为这次登录启动的终端，登录失败就关掉，不留空窗口
                return self._fail(msg)
            info = mt5.account_info()
            if info is None or int(info.login) != login:
                if not mt5.login(login, password=self.password, server=self.acc["server"], timeout=60000):
                    return self._fail("登录失败：" + self.err())
        finally:
            self.wipe_ini()
        # 终端刚启动 / 刚重启时，等它真正连上券商、能读到账户，再开始轮询
        snap = None
        deadline = time.time() + (2 if self.mock else 30)
        while True:
            try:
                snap = self.snapshot()
                if snap["terminal"]["connected"] or time.time() >= deadline:
                    break
            except Exception:
                if time.time() >= deadline:
                    break
            time.sleep(0.5)
        if snap is None:
            return self._fail("已登录但读取不到账户信息：" + self.err())
        self.link, self.link_error = "online", ""
        self.fail_polls = 0
        self.reconnect_tries = 0
        self.was_online = True
        self.status(snap)
        acc = snap.get("account") or {}
        self.progress("login", "ok", f"登录成功 · {acc.get('company', '')} · {acc.get('server', '')}")
        snap, pref_note = self.apply_algo_pref(snap)
        algo_ok, algo_msg = self.check_algo(snap)
        if pref_note:
            algo_msg = pref_note if algo_ok else f"{pref_note}；{algo_msg}"
        sym_note = self.select_default_symbol()
        self.progress("algo", "ok" if algo_ok else "warn", algo_msg + (f"；{sym_note}" if sym_note else ""))
        warn = "" if algo_ok else f"（注意：{algo_msg}）"
        if sym_note:
            warn += f"（{sym_note}）"
        return True, f"登录成功 · {acc.get('company', '')}{warn}"

    def default_chart(self) -> tuple[str, str]:
        sym = self.resolve_symbol(str(self.settings.get("default_symbol") or "USDJPYc"))
        tf = str(self.settings.get("default_timeframe") or "M15").upper()
        return sym, tf

    def select_default_symbol(self) -> str:
        """把默认品种加进市场报价（登录后默认图表、EA 用）；这个账户没有该品种时给出建议。"""
        sym, _ = self.default_chart()
        try:
            if self.mt5.symbol_select(sym, True) and self.mt5.symbol_info(sym) is not None:
                return ""
            sug = self.suggest(sym)
            return f"默认品种 {sym} 在这个账户里不存在" + (f"，可用：{'、'.join(sug)}" if sug else "")
        except Exception:
            return ""

    def available_symbols(self) -> list[str]:
        try:
            return [s.name for s in (self.mt5.symbols_get() or ())]
        except Exception:
            return []

    def suggest(self, sym: str) -> list[str]:
        from . import terminal as T
        return T.suggest_symbols(sym, self.available_symbols())

    def check_symbol(self, symbol: str) -> tuple[bool, str, str]:
        """确认品种在这个账户里存在并加入市场报价。返回 (ok, 实际品种, 说明)。
        不存在但只有一个同品种的写法（例如 USDJPY → USDJPYc）时自动改用它并说明。"""
        mt5 = self.mt5
        try:
            if mt5.symbol_info(symbol) is not None:
                mt5.symbol_select(symbol, True)
                return True, symbol, ""
        except Exception:
            pass
        sug = self.suggest(symbol)
        if len(sug) == 1:
            alt = sug[0]
            mt5.symbol_select(alt, True)
            return True, alt, f"这个账户没有 {symbol}，已自动改用 {alt}"
        if sug:
            return False, symbol, f"这个账户没有品种 {symbol}，可选：{'、'.join(sug)}（请在分发窗口改品种，或在账户里设置品种后缀/映射）"
        return False, symbol, f"这个账户没有品种 {symbol}（检查品种名和后缀，例如 USDJPYc）"

    def _err_code(self) -> int:
        try:
            return int(self.mt5.last_error()[0])
        except Exception:
            return 0

    # ---------------- 算法交易按钮 ----------------
    def _algo_now(self):
        try:
            ti = self.mt5.terminal_info()
        except Exception:
            ti = None
        return None if ti is None else bool(ti.trade_allowed)

    def set_algo(self, on: bool) -> tuple[bool, str, dict]:
        """低层：点工具栏「算法交易」按钮并回读 terminal_info().trade_allowed。
        v1.5.15：生产路径只允许在按钮当前是关时调用 set_algo(True)（ensure_toolbar_algo_on / 分发）。
        禁止把关掉工具栏当作关面板算法或交易收尾。"""
        want = "开启" if on else "关闭"
        cur = self._algo_now()
        if cur is None:
            return False, "读取不到终端状态：" + self.err(), {}
        if cur == on:
            return True, f"算法交易本来就是{'开' if on else '关'}", {"trade_allowed": cur, "changed": False}
        hwnd = 0
        if not self.mock:
            from . import terminal as T
            proc = T.proc_matches(self.own)
            if not self.own or proc is None:
                return False, "这个终端不是本程序启动的，不会去点它的按钮（请在那个终端窗口里手动点「算法交易」）", \
                    {"trade_allowed": cur}
            wins = T.main_windows(proc.pid)
            if not wins:
                return False, "找不到这个终端的主窗口（可能还在启动），请稍后再试", {"trade_allowed": cur}
            hwnd = wins[0]
        for attempt in (1, 2):
            if self.mock:
                if not getattr(self.mt5, "_algo_stuck", False):
                    self.mt5._algo = on
            else:
                from . import terminal as T
                if not T.post_algo_toggle(hwnd):
                    return False, "向终端发送切换命令失败", {"trade_allowed": cur}
            deadline = time.time() + (0.5 if self.mock else 5)
            while time.time() < deadline:
                time.sleep(0.05 if self.mock else 0.25)
                now = self._algo_now()
                if now == on:
                    tail = "" if on else "：EA 不再下单，持仓保留"
                    return True, f"算法交易已{want}{tail}" + ("（第二次才生效）" if attempt == 2 else ""), \
                        {"trade_allowed": on, "changed": True}
            if self._algo_now() == on:
                break
        now = self._algo_now()
        return False, f"已发送{want}命令，但终端状态没有变化（现在仍是{'开' if now else '关'}）。请在该终端窗口里手动点「算法交易」按钮", \
            {"trade_allowed": bool(now)}

    def ensure_toolbar_algo_on(self) -> tuple[bool, str, dict]:
        """保证工具栏算法交易为开，以便 Python API 能下单。
        仅当按钮当前是关时才点开；已经是开则不动、绝不因此关掉。
        若面板标记了 algo_off（禁止图表 EA），先静音 EA（不因交易重启终端），再打开工具栏。"""
        cur = self._algo_now()
        if cur is True:
            return True, "工具栏算法交易已开", {"trade_allowed": True, "changed": False}
        if cur is None:
            return False, "读取不到终端状态：" + self.err(), {}
        if self.acc.get("algo_off"):
            self.apply_ea_live(False, may_relaunch=False)
        return self.set_algo(True)

    def with_algo_for_trade(self, fn, params: dict, guard_experts: bool = False):
        """快捷 / 批量交易：只确保工具栏为开，然后执行下单/平仓。
        不改 algo_off，不做完再关工具栏，绝不因交易重启 / 重开 MT5，也不卸图表 EA。
        guard_experts 仅保留兼容旧调用，已忽略。"""
        _ = guard_experts
        ok, msg, _ = self.ensure_toolbar_algo_on()
        if not ok:
            return False, f"无法打开工具栏算法交易（下单/平仓需要）：{msg}", {}
        return fn(params)

    def _ea_already_in_state(self, allow: bool) -> bool:
        if getattr(self, "_ea_trade_locked", None) == (not allow):
            if self.mock:
                return True
        if self.mock:
            return False
        path = self.acc.get("terminal_path") or ""
        if not path:
            return getattr(self, "_ea_trade_locked", None) == (not allow)
        from . import terminal as T
        try:
            return not T.chart_experts_need_live_change(path, allow)
        except Exception:
            return getattr(self, "_ea_trade_locked", None) == (not allow)

    def apply_ea_live(self, allow: bool, may_relaunch: bool = False) -> tuple[bool, str, bool]:
        """按面板开关设置图表 EA 的自动交易权限（expertmode bit 0）。
        已是目标状态则跳过。实时生效通常需要关终端 → 改文件 → 再打开；
        只允许在登录/重连 apply_algo_pref、面板 /api/algo、分发/停 EA 时重启。
        交易路径 must 传 may_relaunch=False。返回 (ok, msg, relaunched)。"""
        allow = bool(allow)
        if self._ea_already_in_state(allow):
            self._ea_trade_locked = not allow
            return True, "图表 EA 自动交易权限已是目标状态", False
        if self.mock:
            self._ea_trade_locked = not allow
            return True, "图表 EA 已允许自动交易" if allow else "图表 EA 已禁止自动交易", False
        from . import terminal as T
        path = self.acc.get("terminal_path") or ""
        if not path:
            self._ea_trade_locked = not allow
            return True, "没有终端路径，仅记住偏好", False
        running = False
        try:
            running = bool(T.find_processes(path))
        except Exception:
            running = False
        if not running or not may_relaunch:
            try:
                if not running:
                    T.set_chart_experts_live(path, allow)
                self._ea_trade_locked = not allow
            except Exception as e:
                return False, f"改写图表 EA 权限失败：{e}", False
            if running:
                return True, "已记住图表 EA 权限，将在下次连接时生效（交易路径不重启终端）", False
            return True, "图表 EA 已允许自动交易" if allow else "图表 EA 已禁止自动交易", False
        # 终端在跑且允许重启：关 → 改文件 → 再开（只这一次）
        if getattr(self, "_applying_ea_bounce", False):
            self._ea_trade_locked = not allow
            return True, "图表 EA 权限处理中", False
        self._applying_ea_bounce = True
        try:
            ok, msg = self._restart()
            if not ok:
                return False, f"关闭终端以更新图表 EA 权限失败：{msg}", False
            try:
                T.set_chart_experts_live(path, allow)
            except Exception as e:
                return False, f"改写图表 EA 权限失败：{e}", False
            self._ea_trade_locked = not allow
            ok, msg = self._relaunch(None)
            if not ok:
                return False, f"已改写图表 EA 权限，但重新打开终端失败：{msg}", True
            return True, ("已重启终端并允许图表 EA 自动交易" if allow else "已重启终端并禁止图表 EA 自动交易"), True
        finally:
            self._applying_ea_bounce = False

    def apply_algo_pref(self, snap: dict) -> tuple[dict, str]:
        """登录 / 重连后：面板开关管图表 EA，工具栏保持开给面板接口。
        - 默认：标记 algo_off、静音图表 EA，并确保工具栏为开（不再关掉工具栏）。
        - 设置「登录后默认打开」且用户点的登录、且未标记关闭：恢复 EA 并保证工具栏开。
        - 分发 EA（_deploy_algo）：恢复 EA 并保证工具栏开。
        静音状态需要改图表且终端已在运行时，可关终端改文件再打开一次。"""
        if getattr(self, "_applying_ea_bounce", False):
            # 重启后再登录：文件已改好，只保证工具栏开，避免再次重启
            ok, msg, _ = self.ensure_toolbar_algo_on()
            note = "工具栏算法交易已开（图表 EA 权限已按面板开关生效）" if ok else "打开工具栏算法交易失败：" + msg
            if self.acc.get("algo_off"):
                note = ("图表 EA 已禁止自动交易；" + note) if ok else note
            try:
                snap = self.snapshot()
                self.status(snap)
            except Exception:
                pass
            return snap, note

        explicit, self._explicit = self._explicit, False
        deploy = bool(getattr(self, "_deploy_algo", False))
        self._deploy_algo = False
        auto = bool(self.settings.get("auto_enable_algo", False))
        note = ""
        changed = False
        relaunched = False

        if deploy:
            if self.acc.get("algo_off"):
                self.acc["algo_off"] = False
                self.emit(type="algo_off", algo_off=False)
            ok_m, msg_m, relaunched = self.apply_ea_live(True, may_relaunch=True)
            if not ok_m:
                note = "恢复图表 EA 自动交易失败：" + msg_m
            else:
                note = "算法交易已开启（分发）" if not relaunched else msg_m
            changed = True
        elif explicit and auto and not self.acc.get("algo_off"):
            ok_m, msg_m, relaunched = self.apply_ea_live(True, may_relaunch=True)
            note = ("已允许图表 EA 自动交易" if ok_m else "恢复图表 EA 自动交易失败：" + msg_m)
            changed = True
        else:
            if not self.acc.get("algo_off"):
                self.acc["algo_off"] = True
                self.emit(type="algo_off", algo_off=True)
            ok_m, msg_m, relaunched = self.apply_ea_live(False, may_relaunch=True)
            if not ok_m:
                note = "禁止图表 EA 自动交易失败：" + msg_m
            else:
                note = "图表 EA 已禁止自动交易（默认；工具栏为面板保持开，开平仓仍可用）"
                if relaunched:
                    note = "图表 EA 已禁止自动交易（已重启终端使权限生效；工具栏为面板保持开，开平仓仍可用）"
            changed = True

        if relaunched:
            try:
                snap = self.snapshot()
                self.status(snap)
            except Exception:
                pass
        # 静音之后再开工具栏（绝不能先开再静音）
        ok, msg, det = self.ensure_toolbar_algo_on()
        if not ok:
            extra = "打开工具栏算法交易失败：" + msg
            note = f"{note}；{extra}" if note else extra
            self.emit(type="log", action="算法交易", ok=False, message=extra)
        elif det.get("changed"):
            self.emit(type="log", action="算法交易", ok=True, message="已打开工具栏算法交易（供面板下单，不等于 EA 可下单）")
            changed = True
        if note:
            self.emit(type="log", action="算法交易", ok=ok or not note.startswith("禁止") and "失败" not in note, message=note)

        if changed or note:
            try:
                snap = self.snapshot()
                self.status(snap)
            except Exception:
                pass
        return snap, note

    def cmd_algo(self, p: dict):
        """面板「算法交易」开关：只管理图表 EA 自动交易，不关工具栏。"""
        on = bool(p.get("on"))
        if on:
            self.acc["algo_off"] = False
            self.emit(type="algo_off", algo_off=False)
            ok_m, msg_m, relaunched = self.apply_ea_live(True, may_relaunch=True)
            ok_t, msg_t, det = self.ensure_toolbar_algo_on()
            ok = ok_m and ok_t
            parts = []
            if ok_m:
                parts.append("EA 已允许自动交易")
            else:
                parts.append("允许图表 EA 自动交易失败：" + msg_m)
            if relaunched and ok_m:
                parts.append("已重启终端使权限生效")
            if ok_t:
                parts.append("工具栏已开")
            else:
                parts.append("打开工具栏失败：" + msg_t)
            detail = {"trade_allowed": (det or {}).get("trade_allowed", self._algo_now()),
                      "ea_trading": True, "algoEnabled": True, "relaunched": relaunched}
            return ok, "，".join(parts), detail
        # 关闭：先静音 EA，再保证工具栏开
        self.acc["algo_off"] = True
        self.emit(type="algo_off", algo_off=True)
        ok_m, msg_m, relaunched = self.apply_ea_live(False, may_relaunch=True)
        ok_t, msg_t, det = self.ensure_toolbar_algo_on()
        ok = ok_m and ok_t
        if ok:
            msg = "EA 已禁止自动交易，持仓保留；工具栏为面板保持开，开平仓仍可用"
            if relaunched:
                msg += "（已重启终端使权限生效）"
        else:
            bits = []
            if not ok_m:
                bits.append("禁止图表 EA 失败：" + msg_m)
            if not ok_t:
                bits.append("打开工具栏失败：" + msg_t)
            msg = "；".join(bits) or msg_m
        detail = {"trade_allowed": (det or {}).get("trade_allowed", self._algo_now()),
                  "ea_trading": False, "algoEnabled": False, "relaunched": relaunched}
        return ok, msg, detail

    def check_algo(self, snap: dict) -> tuple[bool, str]:
        ti = snap.get("terminal") or {}
        acc = snap.get("account") or {}
        if not ti.get("trade_allowed", False):
            return False, ("终端工具栏的「算法交易」没有打开：面板下单需要它保持开。"
                           "本程序会在需要时打开它；如果仍是关闭，请在这个终端窗口的工具栏点一下「算法交易」按钮（变绿），"
                           "并检查 工具 → 选项 → 智能交易系统 里「允许算法交易」已勾选")
        if not acc.get("trade_allowed", True):
            return False, "这个账户不允许交易：可能是用投资者（只读）密码登录的，或账户被券商禁止交易，请改用交易密码"
        if not acc.get("trade_expert", True):
            return False, "券商禁止这个账户使用 EA（智能交易）自动交易，EA 不会下单；Python 手动批量下单仍可尝试"
        if self.acc.get("algo_off"):
            return True, "图表 EA 已禁止自动交易；工具栏为面板保持开，开平仓仍可用"
        return True, "图表 EA 允许自动交易，工具栏已开"

    def wipe_ini(self):
        from . import terminal as T
        if self._ini:
            T.wipe_file(self._ini)
            self._ini = None

    def ensure_terminal(self, path: str, startup: dict | None = None) -> tuple[bool, str]:
        """确认终端路径合格；没有副本就从模板复制一份；没运行就由本程序以 /portable 启动并登记 PID。
        启动时用一次性的 /config 配置文件写入登录信息并打开算法交易，登录后立即删除该文件。"""
        from . import terminal as T
        if not path:
            return self._fail_step("copy", "没有设置终端路径（本程序不会去连接默认/桌面上的 MT5）")
        if self.root is not None:
            err = T.check_managed_path(path, self.root)
            if err:
                return self._fail_step("copy", err)
        if self.mock:   # 仅自动化测试：没有真实文件
            self.progress("copy", "ok", "终端副本已就绪")
            self.progress("launch", "ok", "终端已启动")
            return True, ""
        # 1. 副本
        if not os.path.isfile(path):
            tpl = self.settings.get("_template") or ""
            if not tpl:
                return self._fail_step("copy", "还没有这个账户的终端副本，也没有找到模板 MT5。请在设置页填写「模板 MT5 目录」"
                                               "（例如 D:\\15\\MetaTrader 5），或把 MT5 安装到 terminals\\base")
            self.progress("copy", "run", f"正在从模板复制：{tpl}")
            try:
                ok, msg = T.clone_terminal(tpl, str(Path(path).parent), self.root)
            except Exception as e:
                ok, msg = False, f"复制失败：{e}"
            if not ok:
                return self._fail_step("copy", msg)
            self.emit(type="log", action="创建副本", ok=True, message=msg)
            self.progress("copy", "ok", msg)
        else:
            self.progress("copy", "ok", "终端副本已存在")
        # 2. 是否已在运行
        running = T.find_processes(path)
        if running:
            mine = T.proc_matches(self.own)
            if mine is not None and mine.pid in [p.pid for p in running]:
                self.progress("launch", "ok", "终端已在运行（本程序启动）")
                return True, ""
            ours = [p for p in running if T.launched_by_fleet(p, path)]
            if ours:
                # 本程序启动过、之后由 MT5 自动更新（LiveUpdate）用原参数重新启动的终端：接管
                if self._adopt(ours[0], path, "接管终端", "MT5 自动更新后重新启动了终端，已重新接管（PID {pid}）"):
                    self.progress("launch", "ok", "终端已在运行（由本程序启动的终端自动更新后重启，已接管）")
                    return True, ""
            return self._fail_step("launch", "这个终端文件夹里的 MT5 已经在运行，但不是本程序启动的。为避免冲突不会接管它，"
                                             "请先手动关闭这个窗口再登录")
        # 3. 服务器列表 + 图表整理 + 启动
        T.wipe_stale_inis(Path(path).parent)
        if startup is None and self.settings.get("single_chart", True):
            try:   # 只留一个默认品种/周期的图表（挂着 EA 的图表保留）
                sym, tf = self.default_chart()
                T.prepare_profile(path, sym, tf, None, None, bool(self.settings.get("allow_dll_import", False)))
            except Exception:
                pass
        lib = self.data_dir / "servers" / "servers.dat"
        note = ""
        try:   # 原先能正常登录的账户不动它的服务器列表；新账户 / 登录失败过的才放入
            note = "" if self.acc.get("_worked") else T.apply_servers_dat(path, lib)
        except Exception as e:
            note = f"放入服务器列表失败：{e}"
        creds = {"login": self.acc["login"], "password": self.password, "server": self.acc["server"]} if self.password else None
        enc = self.settings.get("ini_encoding", "utf-16")
        dll = bool(self.settings.get("allow_dll_import", False))
        ini = None
        # v1.5.15：先按面板开关改好图表 EA 的 live 位，再以 Enabled=1 启动（工具栏开给面板）。
        # 未改好图表时先 Enabled=0，登录后再静音并打开工具栏，避免 EA 在启动瞬间下单。
        if startup is not None:
            want_ea = True
        else:
            want_ea = bool(self.settings.get("auto_enable_algo", False)) and not self.acc.get("algo_off")
        charts_ready = True
        try:
            T.set_chart_experts_live(path, want_ea)
        except Exception:
            charts_ready = False
        want_algo = True if (want_ea or charts_ready) else False
        if creds or startup or want_algo or not want_ea:
            try:
                ini = T.write_config_ini(path, creds, startup, dll, enc, algo=want_algo)
            except Exception as e:
                return self._fail_step("launch", f"写启动配置失败：{e}")
        self._ini = ini
        self.progress("launch", "run", "正在以便携模式启动终端" + (f"（{note}）" if note else ""))
        try:
            pid, ct = T.launch_terminal(path, ini)
        except Exception as e:
            self.wipe_ini()
            return self._fail_step("launch", f"启动终端失败：{e}")
        marks = T.log_marks(path)
        self.own = {"path": path, "pid": pid, "create_time": ct}
        self.emit(type="launched", path=path, pid=pid, create_time=ct)
        time.sleep(3)
        if not T.proc_matches(self.own):
            # 常见原因：MT5 启动时发现新版本，先退出、由更新程序升级后用同样的参数重新启动（LiveUpdate）。
            # 这时启动配置（含登录信息）先不删，等更新后的终端起来读它，然后接管这个新进程。
            self.progress("launch", "run", "终端启动后退出了，可能是 MT5 正在自动更新，等待它重新启动…")
            wait = 5 if self.mock else float(self.settings.get("update_wait", 120))
            deadline = time.time() + wait
            while time.time() < deadline:
                ours = T.adoptable_processes(path)
                if ours:
                    upd = T.liveupdate_lines(path, marks)
                    why = "MT5 自动更新" if upd else "终端自己重新启动"
                    if self._adopt(ours[0], path, "接管终端", why + "后已接管新进程（PID {pid}）"):
                        self.progress("launch", "ok", f"{why}完成，已接管（PID {self.own['pid']}）")
                        return True, ""
                time.sleep(1)
            self.wipe_ini()
            upd = T.liveupdate_lines(path, marks)
            if upd:
                return self._fail_step("launch", "MT5 正在自动更新，更新后没有在 " + f"{int(wait)} 秒内重新启动，请稍后再登录。日志：{upd[-1]}")
            return self._fail_step("launch", "终端启动后马上退出了（检查 terminal64.exe 是否完整，或被杀毒软件拦截）")
        self.progress("launch", "ok", f"终端已启动（PID {pid}）" + (f"，{note}" if note else ""))
        return True, ""

    def _adopt(self, proc, path: str, action: str, msg: str) -> bool:
        from . import terminal as T
        entry = T.proc_entry(proc, path)
        if not entry:
            return False
        self.own = entry
        self.emit(type="launched", path=path, pid=entry["pid"], create_time=entry["create_time"], adopted=True)
        self.emit(type="log", action=action, ok=True, message=msg.format(pid=entry["pid"]))
        return True

    def _fail_step(self, key: str, msg: str):
        self.progress(key, "fail", msg)
        return False, msg

    def close_own_terminal(self) -> tuple[bool, str]:
        from . import terminal as T
        if self.mock or not self.own:
            return True, "终端不是本程序启动的，保持运行"
        ok, msg = T.close_owned(self.own)
        if ok:
            self.emit(type="closed", path=self.own.get("path"))
            self.own = None
        return ok, msg

    def disconnect(self):
        if self.mt5 is not None:
            try:
                self.mt5.shutdown()
            except Exception:
                pass
        self.link, self.link_error = "offline", ""
        self.status()

    # ---------------- 轮询 ----------------

    def usdjpy_quote(self) -> dict | None:
        """已登录终端上的 USDJPYc / USDJPY。15 秒缓存，失败不影响账户轮询。"""
        now = time.time()
        cached = getattr(self, "_uj_cache", None)
        if cached and now - cached[0] < 15:
            return cached[1]
        q = None
        try:
            q = self._usdjpy_once()
        except Exception:
            q = None
        self._uj_cache = (now, q)
        return q

    def _usdjpy_once(self) -> dict | None:
        mt5 = self.mt5
        if mt5 is None:
            return None
        for name in ("USDJPYc", "USDJPY"):
            try:
                if not mt5.symbol_select(name, True):
                    continue
                tick = mt5.symbol_info_tick(name)
                if tick is None or not getattr(tick, "bid", 0):
                    continue
                info = mt5.symbol_info(name)
                prev = 0.0
                tf = getattr(mt5, "TIMEFRAME_H1", 16385)
                rates = None
                fn = getattr(mt5, "copy_rates_from_pos", None)
                if fn:
                    rates = fn(name, tf, 0, 25)
                if rates is not None and len(rates) >= 2:
                    bar = rates[0]
                    try:
                        prev = float(bar["close"])
                    except Exception:
                        prev = float(bar[4])
                return {"symbol": name, "bid": float(tick.bid), "prev": prev,
                        "digits": int(getattr(info, "digits", 3) or 3)}
            except Exception:
                continue
        return None

    def _server_time(self, quotes: dict | None = None) -> int:
        """券商服务器时间（秒，和 MT5 终端右下角一致）。优先用关注品种 / USDJPY 的 tick.time。"""
        mt5 = self.mt5
        if mt5 is None:
            return 0
        names = []
        for q in (quotes or {}).values():
            if isinstance(q, dict) and q.get("symbol") and not q.get("error"):
                names.append(q["symbol"])
        for n in ("USDJPYc", "USDJPY", "EURUSD", "XAUUSD"):
            if n not in names:
                names.append(n)
        for name in names:
            try:
                tick = mt5.symbol_info_tick(name)
                if tick is not None and getattr(tick, "time", 0):
                    return int(tick.time)
            except Exception:
                continue
        try:  # 任意有报价的品种
            for s in (mt5.symbols_get() or ())[:30]:
                tick = mt5.symbol_info_tick(s.name)
                if tick is not None and getattr(tick, "time", 0):
                    return int(tick.time)
        except Exception:
            pass
        return 0

    def snapshot(self) -> dict:
        mt5 = self.mt5
        info = mt5.account_info()
        if info is None:
            raise RuntimeError("读取账户失败：" + self.err())
        positions = mt5.positions_get() or ()
        orders = mt5.orders_get() or ()
        ti = mt5.terminal_info()
        quotes = {}
        for sym in self.watch:
            try:
                ok, actual, note = self.quick_symbol(sym)
                if not ok:
                    quotes[sym] = {"symbol": actual, "error": note}
                    continue
                t = mt5.symbol_info_tick(actual)
                si = mt5.symbol_info(actual)
                if t is None:
                    quotes[sym] = {"symbol": actual, "error": f"取不到 {actual} 的报价"}
                    continue
                quotes[sym] = {"symbol": actual, "bid": t.bid, "ask": t.ask, "digits": int(getattr(si, "digits", 5) or 5),
                               "note": note}
            except Exception as e:  # 报价失败不影响账户轮询
                quotes[sym] = {"symbol": sym, "error": f"报价出错：{e}"}
        sym_meta = {}
        def _pos_meta(sym: str):
            if sym not in sym_meta:
                try:
                    si = mt5.symbol_info(sym)
                except Exception:
                    si = None
                digits = int(getattr(si, "digits", 5) or 5) if si is not None else 5
                point = float(getattr(si, "point", 0) or 0) if si is not None else 0.0
                if not point:
                    point = 10 ** -digits
                sym_meta[sym] = (digits, point)
            return sym_meta[sym]
        pos_rows = []
        for p in positions:
            digits, point = _pos_meta(p.symbol)
            pos_rows.append({
                "ticket": p.ticket, "symbol": p.symbol, "side": "buy" if p.type == 0 else "sell",
                "volume": p.volume, "openPrice": p.price_open, "price": p.price_current, "sl": p.sl, "tp": p.tp,
                "digits": digits, "point": point,
                "profit": round(p.profit + getattr(p, "swap", 0.0), 2), "swap": getattr(p, "swap", 0.0),
                "magic": p.magic, "comment": p.comment, "time": p.time,
            })
        return {
            "account": {
                "login": info.login, "balance": round(info.balance, 2), "equity": round(info.equity, 2),
                "margin": round(info.margin, 2), "margin_free": round(info.margin_free, 2),
                "margin_level": round(info.margin_level or 0, 2), "profit": round(info.profit, 2),
                "currency": info.currency, "leverage": info.leverage, "name": info.name, "server": info.server,
                "company": info.company, "trade_allowed": bool(info.trade_allowed),
                "trade_expert": bool(getattr(info, "trade_expert", True)),
            },
            "positions": pos_rows,
            "orders": [{
                "ticket": o.ticket, "symbol": o.symbol, "type": int(o.type), "volume": o.volume_current,
                "price": o.price_open, "sl": o.sl, "tp": o.tp, "magic": o.magic, "comment": o.comment,
            } for o in orders],
            "terminal": {
                "connected": bool(ti.connected) if ti else False,
                "trade_allowed": bool(ti.trade_allowed) if ti else False,
                "ea_trading": not bool(self.acc.get("algo_off")),
                "algoEnabled": not bool(self.acc.get("algo_off")),
                "build": getattr(ti, "build", 0) if ti else 0,
                "ping_ms": round(getattr(ti, "ping_last", 0) / 1000, 1) if ti else 0,
            },
            "quotes": quotes,
            "usdjpy": self.usdjpy_quote(),
            "server_time": self._server_time(quotes),
        }

    def terminal_gone(self) -> bool:
        """本程序启动的终端进程已经不在了（被关闭、崩溃、或 MT5 自动更新时退出）。"""
        if self.mock or not self.own:
            return False
        from . import terminal as T
        return T.proc_matches(self.own) is None

    def poll(self):
        if self.link != "online" or self.mt5 is None:
            return
        if self.terminal_gone():
            # 终端换了进程（例如自动更新后重启）：旧连接已失效，马上重新初始化，不等 3 次失败
            self.link, self.link_error = "error", "终端已重启，正在重新连接"
            self.status()
            self.last_reconnect = 0.0
            return
        try:
            snap = self.snapshot()
            self.fail_polls = 0
            if not snap["terminal"]["connected"]:
                self.link_error = "终端与券商服务器断开，等待重连"
            else:
                self.link_error = ""
            self.status(snap)
        except Exception as e:
            self.fail_polls += 1
            if self.fail_polls >= 3:
                self.link, self.link_error = "error", f"{e}（将自动重连）"
                self.status()

    def maybe_reconnect(self):
        # 只在「曾经在线、后来断开」时自动重连；密码错误不会反复重试，避免账户被锁
        gap = 5 if self.reconnect_tries < 6 else 15
        if self.link == "error" and self.was_online and self.mt5 is not None and time.time() - self.last_reconnect > gap:
            self.last_reconnect = time.time()
            self.reconnect_tries += 1
            try:
                self.mt5.shutdown()
            except Exception:
                pass
            self.quiet = True
            try:
                ok, msg = self.connect()
            finally:
                self.quiet = False
            if ok or self.reconnect_tries <= 1 or self.reconnect_tries % 6 == 0:
                self.emit(type="log", action="自动重连", ok=ok, message=msg)

    # ---------------- 交易工具 ----------------
    def resolve_symbol(self, sym: str) -> str:
        mapping = {}
        for part in (self.acc.get("symbol_map") or "").replace("；", ";").replace("，", ",").replace(",", ";").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                mapping[k.strip().upper()] = v.strip()
        if sym.upper() in mapping:
            return mapping[sym.upper()]
        suffix = self.acc.get("symbol_suffix") or ""
        if suffix and not sym.endswith(suffix):
            return sym + suffix
        return sym

    def quick_symbol(self, sym: str) -> tuple[bool, str, str]:
        """快捷面板用：先按账户的后缀/映射换算，再确认存在；只有一个同品种写法时自动改用（例如 USDJPY → USDJPYc）。结果缓存。"""
        hit = self._sym_cache.get(sym)
        if hit is not None and (hit[0][0] or time.time() - hit[1] < 30):   # 找不到的品种 30 秒后再查
            return hit[0]
        res = self.check_symbol(self.resolve_symbol(sym))
        self._sym_cache[sym] = (res, time.time())
        return res

    def symbol_names(self, sym: str) -> set[str]:
        """平仓「仅当前品种」时，哪些持仓品种名算同一个品种。"""
        names = {sym, self.resolve_symbol(sym)}
        try:
            ok, actual, _ = self.quick_symbol(sym)
            if ok:
                names.add(actual)
        except Exception:
            pass
        return names

    def symbol_ready(self, symbol: str):
        mt5 = self.mt5
        if not mt5.symbol_select(symbol, True):
            sug = self.suggest(symbol)
            raise ValueError(f"品种 {symbol} 在这个账户不存在" + (f"，可用：{'、'.join(sug)}" if sug else "（检查后缀设置）"))
        info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if info is None or tick is None:
            raise ValueError(f"取不到 {symbol} 的报价：{self.err()}")
        return info, tick

    @staticmethod
    def norm_volume(v: float, info) -> float:
        step = info.volume_step or 0.01
        v = math.floor(v / step + 1e-9) * step
        dec = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
        return round(v, dec)

    def fillings(self, info):
        mode = int(getattr(info, "filling_mode", 0) or 0)
        order = []
        if mode & 1:
            order.append(self.c("ORDER_FILLING_FOK"))
        if mode & 2:
            order.append(self.c("ORDER_FILLING_IOC"))
        order.append(self.c("ORDER_FILLING_RETURN"))
        for f in (self.c("ORDER_FILLING_FOK"), self.c("ORDER_FILLING_IOC")):
            if f not in order:
                order.append(f)
        return order

    def send(self, req: dict, info=None):
        """发单；成交模式不被支持（10030）时自动换下一种。"""
        fills = self.fillings(info) if info is not None and req.get("action") == self.c("TRADE_ACTION_DEAL") else [req.get("type_filling")]
        res = None
        for f in fills:
            if f is not None:
                req["type_filling"] = f
            res = self.mt5.order_send(req)
            if res is None:
                return False, "发单无返回：" + self.err(), None
            if res.retcode != 10030:
                break
        ok = res.retcode in OK_RETCODES
        return ok, trade_msg(res.retcode, getattr(res, "comment", "")), res

    def check(self, req: dict, info=None):
        if info is not None and req.get("action") == self.c("TRADE_ACTION_DEAL"):
            req["type_filling"] = self.fillings(info)[0]
        fn = getattr(self.mt5, "order_check", None)
        if fn is None:
            return True, "（未做服务器检查）"
        res = fn(req)
        if res is None:
            return False, "检查失败：" + self.err()
        ok = res.retcode in (0, 10009, 10008)
        extra = f"，预计占用保证金 {res.margin:.2f}，剩余可用 {res.margin_free:.2f}" if ok else ""
        return ok, trade_msg(res.retcode, getattr(res, "comment", "")) + extra

    # ---------------- 命令 ----------------
    def cmd_order(self, p: dict):
        mt5, acc = self.mt5, self.acc
        kind = p.get("kind", "buy")
        if kind not in ORDER_KINDS:
            return False, f"未知下单类型 {kind}", {}
        type_name, label = ORDER_KINDS[kind]
        symbol = self.resolve_symbol(p["symbol"])
        sym_note = ""
        if p.get("auto_symbol"):          # 快捷面板：只有一个同品种写法时自动改用
            ok_s, actual, sym_note = self.quick_symbol(p["symbol"])
            if not ok_s:
                return False, sym_note, {}
            symbol = actual
        info, tick = self.symbol_ready(symbol)
        ainfo = mt5.account_info()
        base = float(p["lots"])
        mode = p.get("scale_mode", "fixed")
        scale_groups = p.get("scale_groups") or []
        scaled = (not scale_groups) or (acc.get("group") in scale_groups)
        factor = 1.0
        if scaled and mode == "multiplier":
            factor = float(acc.get("lot_multiplier") or 1.0)
        elif scaled and mode == "equity":
            ref = float(p.get("ref_equity") or 0)
            if ref <= 0:
                return False, "按净值比例需要填写基准净值", {}
            factor = ainfo.equity / ref
        raw = base * factor
        lots = self.norm_volume(raw, info)
        if lots < info.volume_min:
            return False, f"计算手数 {raw:.4f} 小于该品种最小手数 {info.volume_min}", {"lots": lots}
        if lots > info.volume_max:
            return False, f"计算手数 {lots} 超过该品种最大手数 {info.volume_max}", {"lots": lots}
        max_lots = float(p.get("max_lots") or 0)
        if max_lots and lots > max_lots + 1e-9:
            return False, f"手数保护：{lots} 手 > 单笔上限 {max_lots} 手，已拦截", {"lots": lots}
        otype = self.c(type_name)
        is_market = kind in ("buy", "sell")
        is_buy = kind.startswith("buy")
        if is_market:
            price = tick.ask if is_buy else tick.bid
        else:
            price = float(p.get("price") or 0)
            if price <= 0:
                return False, "挂单需要填写价格", {}
        point = info.point or 10 ** -info.digits
        sl = float(p.get("sl") or 0)
        tp = float(p.get("tp") or 0)
        if p.get("sltp_mode") == "points":
            sl = (price - sl * point if is_buy else price + sl * point) if sl else 0.0
            tp = (price + tp * point if is_buy else price - tp * point) if tp else 0.0
        sl, tp = round(sl, info.digits), round(tp, info.digits)
        req = {
            "action": self.c("TRADE_ACTION_DEAL") if is_market else self.c("TRADE_ACTION_PENDING"),
            "symbol": symbol, "volume": lots, "type": otype, "price": round(price, info.digits),
            "sl": sl, "tp": tp, "deviation": int(p.get("deviation") or 20), "magic": int(p.get("magic") or 0),
            "comment": (p.get("comment") or "fleet")[:31], "type_time": self.c("ORDER_TIME_GTC"),
        }
        if not is_market:
            req["type_filling"] = self.c("ORDER_FILLING_RETURN")
        desc = f"{label} {symbol} {lots} 手" + (f"（{base}×{factor:.3f}）" if factor != 1 else "") + \
               (f" @{req['price']}" if not is_market else "") + (f" SL {sl}" if sl else "") + (f" TP {tp}" if tp else "")
        ok, msg, res = self.send(req, info if is_market else None)
        detail = {"lots": lots}
        if res is not None:
            detail.update(order=getattr(res, "order", 0), deal=getattr(res, "deal", 0), price=getattr(res, "price", 0))
        tail = f"，单号 {detail.get('order')}，成交价 {detail.get('price')}" if ok and is_market else (f"，挂单号 {detail.get('order')}" if ok else "")
        if sym_note:
            tail += f"（{sym_note}）"
        return ok, f"{desc}：{msg}{tail}", detail

    def cmd_order_pair(self, p: dict):
        """一键开单：同一品种同时开一张多单和一张空单（对冲）。多单因为手数/品种问题没发出去时，空单也不发。"""
        ok1, m1, d1 = self.cmd_order({**p, "kind": "buy"})
        if not ok1 and not d1.get("order"):
            return False, f"开多失败，空单未发送：{m1}", {"buy": d1}
        ok2, m2, d2 = self.cmd_order({**p, "kind": "sell"})
        return ok1 and ok2, f"多：{m1}；空：{m2}", {"buy": d1, "sell": d2, "lots": d1.get("lots", 0)}

    def cmd_lock(self, p: dict):
        """一键锁仓：按当前品种补齐多空手数差（空多→买，多多→卖），然后禁止图表 EA 自动交易并记 algo_off。不关工具栏。"""
        symbol = p.get("symbol") or ""
        sym_note = ""
        if p.get("auto_symbol"):
            ok_s, actual, sym_note = self.quick_symbol(symbol)
            if not ok_s:
                return False, sym_note, {}
            symbol = actual
        else:
            symbol = self.resolve_symbol(symbol)
        info, _tick = self.symbol_ready(symbol)
        positions = list(self.mt5.positions_get(symbol=symbol) or [])
        buy_t = self.c("ORDER_TYPE_BUY")
        sell_t = self.c("ORDER_TYPE_SELL")
        long_lots = round(sum(float(x.volume) for x in positions if int(x.type) == buy_t), 8)
        short_lots = round(sum(float(x.volume) for x in positions if int(x.type) == sell_t), 8)
        step = float(getattr(info, "volume_step", 0) or 0.01)
        diff = abs(long_lots - short_lots)
        detail = {"longLots": long_lots, "shortLots": short_lots, "symbol": symbol}

        def _mute_eas():
            self.acc["algo_off"] = True
            self.emit(type="algo_off", algo_off=True)
            ok_a, msg_a, _rel = self.apply_ea_live(False, may_relaunch=False)
            if ok_a:
                msg_a = "已禁止图表 EA 自动交易（工具栏保持开，开平仓仍可用）"
            return ok_a, msg_a

        if diff < step / 2:
            ok_a, msg_a = _mute_eas()
            tail = "；" + msg_a if ok_a else f"；禁止图表 EA 失败：{msg_a}"
            note = f"（{sym_note}）" if sym_note else ""
            return True, f"{symbol} 已锁仓或无净持仓（多 {long_lots:g} / 空 {short_lots:g}）{note}{tail}", {**detail, "skipped": True}

        if short_lots > long_lots:
            kind, raw = "buy", short_lots - long_lots
            side_cn = "补多"
        else:
            kind, raw = "sell", long_lots - short_lots
            side_cn = "补空"
        lots = self.norm_volume(raw, info)
        if lots < float(info.volume_min or 0):
            ok_a, msg_a = _mute_eas()
            return False, f"锁仓需 {raw:g} 手，小于最小手数 {info.volume_min}", detail
        ok, msg, d = self.cmd_order({
            "symbol": symbol, "kind": kind, "lots": lots, "scale_mode": "fixed",
            "magic": int(p.get("magic") or 880000), "comment": (p.get("comment") or "fleet lock")[:31],
            "deviation": int(p.get("deviation") or 20), "max_lots": float(p.get("max_lots") or 0),
            "auto_symbol": False, "sl": 0, "tp": 0,
        })
        detail.update(d or {}, kind=kind, lots=lots)
        ok_a, msg_a = _mute_eas()
        algo_tail = "；" + msg_a if ok_a else f"；禁止图表 EA 失败：{msg_a}"
        note = f"（{sym_note}）" if sym_note else ""
        return ok, f"一键锁仓 {side_cn} {lots:g} 手：{msg}{note}{algo_tail}", detail


    def _match(self, pos, f: dict) -> bool:
        mode = f.get("mode", "all")
        if mode == "long":
            return pos.type == 0
        if mode == "short":
            return pos.type == 1
        if mode == "symbol":
            want = f.get("symbol", "")
            return pos.symbol == want or pos.symbol == self.resolve_symbol(want)
        if mode == "magic":
            return int(pos.magic) == int(f.get("magic", -1))
        if mode == "profit":
            return pos.profit + getattr(pos, "swap", 0) > 0
        if mode == "loss":
            return pos.profit + getattr(pos, "swap", 0) < 0
        if mode == "ticket":
            return int(pos.ticket) == int(f.get("ticket", -1))
        return True

    def cmd_close(self, p: dict):
        mt5 = self.mt5
        positions = [x for x in (mt5.positions_get() or ()) if self._match(x, p)]
        label = CLOSE_LABEL.get(p.get("mode", "all"), "平仓")
        only = str(p.get("only_symbol") or "")
        if only:                           # 快捷面板「仅当前品种」
            names = self.symbol_names(only)
            positions = [x for x in positions if x.symbol in names]
            label += f"（{only}）"
        if not positions:
            return True, f"{label}：无匹配持仓", {"closed": 0}
        total = round(sum(x.profit + getattr(x, "swap", 0) for x in positions), 2)
        ok_n, fails, realized = 0, [], 0.0
        for x in positions:
            info = mt5.symbol_info(x.symbol)
            tick = mt5.symbol_info_tick(x.symbol)
            if info is None or tick is None:
                fails.append(f"#{x.ticket} 取不到报价")
                continue
            close_buy = x.type == 1
            req = {
                "action": self.c("TRADE_ACTION_DEAL"), "symbol": x.symbol, "volume": x.volume,
                "type": self.c("ORDER_TYPE_BUY") if close_buy else self.c("ORDER_TYPE_SELL"),
                "position": x.ticket, "price": tick.ask if close_buy else tick.bid,
                "deviation": int(p.get("deviation") or 20), "magic": x.magic, "comment": "fleet close",
                "type_time": self.c("ORDER_TIME_GTC"),
            }
            ok, msg, _ = self.send(req, info)
            if ok:
                ok_n += 1
                realized += x.profit + getattr(x, "swap", 0)
            else:
                fails.append(f"#{x.ticket} {msg}")
        msg = f"{label}：成功平掉 {ok_n}/{len(positions)} 笔，约实现 {realized:+.2f}"
        if fails:
            msg += "；失败：" + "；".join(fails[:5])
        return not fails, msg, {"closed": ok_n}

    def cmd_modify(self, p: dict):
        mt5 = self.mt5
        f = {"mode": p.get("mode", "symbol"), "symbol": p.get("symbol", ""), "magic": p.get("magic", -1),
             "ticket": p.get("ticket", -1)}
        positions = [x for x in (mt5.positions_get() or ()) if self._match(x, f)]
        if not positions:
            return True, "改止损止盈：无匹配持仓", {}
        n_ok, fails, plan = 0, [], []
        for x in positions:
            info = mt5.symbol_info(x.symbol)
            if info is None:
                fails.append(f"#{x.ticket} 取不到品种信息")
                continue
            point = info.point or 10 ** -info.digits
            sl_in, tp_in = p.get("sl"), p.get("tp")
            sl, tp = x.sl, x.tp
            if p.get("sltp_mode") == "points":
                base = x.price_open
                if sl_in not in (None, ""):
                    v = float(sl_in)
                    sl = 0.0 if v == 0 else (base - v * point if x.type == 0 else base + v * point)
                if tp_in not in (None, ""):
                    v = float(tp_in)
                    tp = 0.0 if v == 0 else (base + v * point if x.type == 0 else base - v * point)
            else:
                if sl_in not in (None, ""):
                    sl = float(sl_in)
                if tp_in not in (None, ""):
                    tp = float(tp_in)
            sl, tp = round(sl, info.digits), round(tp, info.digits)
            plan.append(f"#{x.ticket} {x.symbol} SL {sl} TP {tp}")
            req = {"action": self.c("TRADE_ACTION_SLTP"), "symbol": x.symbol, "position": x.ticket, "sl": sl, "tp": tp,
                   "magic": x.magic}
            ok, msg, _ = self.send(req)
            if ok:
                n_ok += 1
            else:
                fails.append(f"#{x.ticket} {msg}")
        msg = f"改止损止盈：成功 {n_ok}/{len(positions)} 笔"
        if fails:
            msg += "；失败：" + "；".join(fails[:5])
        return not fails, msg, {}

    def cmd_cancel_orders(self, p: dict):
        mt5 = self.mt5
        sym = p.get("symbol") or ""
        orders = [o for o in (mt5.orders_get() or ()) if not sym or o.symbol in (sym, self.resolve_symbol(sym))]
        if not orders:
            return True, "撤挂单：没有挂单", {}
        n_ok, fails = 0, []
        for o in orders:
            ok, msg, _ = self.send({"action": self.c("TRADE_ACTION_REMOVE"), "order": o.ticket})
            if ok:
                n_ok += 1
            else:
                fails.append(f"#{o.ticket} {msg}")
        msg = f"撤挂单：成功 {n_ok}/{len(orders)} 个"
        if fails:
            msg += "；失败：" + "；".join(fails[:5])
        return not fails, msg, {}

    def cmd_nuke(self, p: dict):
        """核弹级全平：平掉全部持仓（所有品种）并撤销全部挂单。"""
        ok1, m1, d1 = self.cmd_close({"mode": "all", "deviation": p.get("deviation")})
        ok2, m2, _ = self.cmd_cancel_orders({})
        left = len(self.mt5.positions_get() or ())
        msg = f"{m1}；{m2}" + (f"；仍有 {left} 笔持仓未平" if left else "")
        return ok1 and ok2 and not left, msg, {"closed": d1.get("closed", 0), "left": left}

    def cmd_history(self, p: dict):
        mt5 = self.mt5
        now = datetime.now(timezone.utc)
        start = datetime(now.year, 1, 1, tzinfo=timezone.utc) if not p.get("days") else now - timedelta(days=int(p["days"]))
        deals = mt5.history_deals_get(start, now + timedelta(days=1))
        if deals is None:
            return False, "读取历史失败：" + self.err(), {"deals": []}
        outs = (self.c("DEAL_ENTRY_OUT"), self.c("DEAL_ENTRY_INOUT"), self.c("DEAL_ENTRY_OUT_BY"))
        rows = []
        for d in deals:
            if d.entry in outs and d.type in (self.c("DEAL_TYPE_BUY"), self.c("DEAL_TYPE_SELL")):
                net = d.profit + d.commission + d.swap + getattr(d, "fee", 0.0)
                rows.append({"time": int(d.time), "profit": round(net, 2), "symbol": d.symbol, "magic": d.magic})
        return True, f"读取 {len(rows)} 笔平仓记录", {"deals": rows}

    # ---- EA ----
    def _restart(self):
        """断开 Python 连接并正常关闭（本程序启动的）终端。"""
        from . import terminal as T
        try:
            self.mt5.shutdown()
        except Exception:
            pass
        path = self.acc.get("terminal_path")
        if T.find_processes(path) and T.proc_matches(self.own) is None:
            return False, "这个终端不是本程序启动的，不会去关闭它"
        self.link, self.link_error = "connecting", "正在重启终端"
        self.status()
        return self.close_own_terminal()

    def _relaunch(self, startup: dict | None = None):
        path = self.acc.get("terminal_path")
        self.quiet = True     # 重新登录的细节不刷到进度条上
        try:
            ok, msg = self.ensure_terminal(path, startup)
            if not ok:
                return False, msg
            time.sleep(1)
            last = ""
            for _ in range(6):
                ok, msg = self.connect()
                if ok:
                    return True, msg
                last = msg
                time.sleep(5)
            return False, "终端已重启但重新连接失败：" + last
        finally:
            self.quiet = False
            self.wipe_ini()

    def _account_preset(self, p: dict, lib: Path | None) -> str:
        """有「魔术号参数名」时，为这个账户生成专用 .set（魔术号写进去）。返回要用的参数文件名。"""
        from . import terminal as T
        preset = p.get("preset") or ""
        param = (p.get("magic_param") or "").strip()
        if not param:
            return preset
        base = (lib / preset) if (lib is not None and preset) else None
        return T.make_account_preset(self.acc["terminal_path"], base, Path(p["fileName"]).stem, self.acc["login"],
                                     {param: int(p.get("magic") or 0)})

    def cmd_deploy(self, p: dict):
        """分发 / 部署 EA：检查品种 → 复制文件 → 关闭终端 → 把 EA 写进图表配置 → 重新打开并登录 → 校验。
        EA 写在图表文件里（和手动拖到图表上一样），终端以后任何一次重启（包括 MT5 自动更新）都会自动重新加载。"""
        from . import terminal as T
        fname, symbol, tf = p["fileName"], self.resolve_symbol(p["symbol"]), p["timeframe"]
        preset = p.get("preset") or ""
        magic = int(p.get("magic") or 0)
        stem = Path(fname).stem
        # 0. 品种必须在这个账户里存在（例如 Exness 美分账户是 USDJPYc，不是 USDJPY）
        ok, real, note = self.check_symbol(symbol)
        if not ok:
            return self._step_fail("files", note)
        symbol = real
        desc = f"{fname} · {symbol} {tf} · 魔术号 {magic}" + (f" · {preset}" if preset else "")
        if self.mock:   # 仅自动化测试
            for key, txt in (("files", "已复制 EA 文件" + (f"（{note}）" if note else "")), ("restart", "终端已重启"),
                             ("attach", f"已挂到 {symbol} {tf} 图表"), ("verify", "EA 已加载，算法交易已开启")):
                self.progress(key, "run")
                time.sleep(0.25)
                self.progress(key, "ok", txt)
            return True, f"已部署并启动 {desc}" + (f"（{note}）" if note else ""), \
                {"running": True, "magic": magic, "preset": preset, "symbol": symbol}
        path = self.acc.get("terminal_path")
        if not path:
            return self._step_fail("files", "这个账户没有设置终端路径，无法部署 EA")
        lib = self.data_dir / "ea_library"
        src = lib / fname
        if not src.exists():
            return self._step_fail("files", f"EA 库里没有 {fname}，请先在策略页上传 .ex5 文件")
        preset_src = (lib / preset) if preset else None
        if preset_src is not None and not preset_src.exists():
            return self._step_fail("files", f"EA 库里没有参数文件 {preset}，请先上传")
        self.progress("files", "run")
        try:
            T.install_files(path, src, preset_src)
            use_preset = self._account_preset(p, lib)
        except Exception as e:
            return self._step_fail("files", f"复制文件失败：{e}")
        self.progress("files", "ok", f"已复制到 MQL5\\Experts\\{T.FLEET_SUBDIR}" + (f"，参数 {use_preset}" if use_preset else "")
                      + (f"；{note}" if note else ""))
        self.progress("restart", "run", "正在关闭终端（会先保存图表）")
        ok, msg = self._restart()
        if not ok:
            return self._step_fail("restart", f"关闭终端失败：{msg}")
        self.progress("restart", "ok", msg)
        # 1. 把 EA 写进图表配置（终端已关闭）：卸下旧策略（分发清全部 / 或仅同名）再挂新 EA，去掉多余空图表
        dll = bool(self.settings.get("allow_dll_import", False))
        preset_file = (T.mql5_dir(path) / "Presets" / use_preset) if use_preset else None
        drop = []
        if p.get("clear_prior") or p.get("clear_files"):
            drop = [Path(n).stem for n in (p.get("clear_files") or []) if n]
        if p.get("replace", True) and stem not in drop:
            drop.append(stem)
        # 旧版本写在其它图表段落里的同名 EA 也去掉
        for n in (p.get("clear_files") or []):
            try:
                T.remove_expert_from_charts(path, n)
            except Exception:
                pass
        if p.get("replace", True):
            try:
                T.remove_expert_from_charts(path, fname)
            except Exception:
                pass
        self.progress("attach", "run", f"正在把 EA 挂到 {symbol} {tf} 图表并重新打开终端")
        try:
            prof = T.prepare_profile(path, symbol, tf, {"stem": stem, "inputs": T.preset_inputs(preset_file)},
                                     drop, dll)
        except Exception as e:
            prof = None
            perr = str(e)
        marks = T.log_marks(path)
        self._explicit = True       # 分发是用户点的
        self._deploy_algo = True    # 分发后务必打开算法交易（EA 要跑），不受「登录默认关闭」影响
        if prof is None:
            # 写图表文件失败：退回 MT5 启动配置挂载（这种方式终端重启后 EA 不会自动恢复）
            ok, msg = self._relaunch({"fileName": fname, "symbol": symbol, "timeframe": tf, "preset": use_preset})
            mode_note = f"（图表配置写入失败：{perr}，已改用启动配置挂载，终端重启后需重新分发）"
        else:
            ok, msg = self._relaunch(None)
            mode_note = ""
        if not ok:
            return self._step_fail("attach", msg)
        self.progress("attach", "ok", "终端已重新打开并登录" + (f"，已替换 {prof['removed']} 个旧图表" if prof and prof["removed"] else ""))
        # 2. 校验：终端日志里的 EA 加载记录 + 算法交易
        found, line = self._verify_ea(path, fname, marks)
        if found == "none" and prof is not None:
            # 极少数 MT5 版本不认手写的图表文件：退回官方启动配置再挂一次
            self.progress("verify", "run", "日志里没看到 EA 加载记录，改用启动配置再挂一次")
            ok, msg = self._restart()
            if ok:
                try:
                    T.prepare_profile(path, symbol, tf, None, [stem], dll)
                except Exception:
                    pass
                marks = T.log_marks(path)
                self._explicit = True
                self._deploy_algo = True
                ok, msg = self._relaunch({"fileName": fname, "symbol": symbol, "timeframe": tf, "preset": use_preset})
                if not ok:
                    return self._step_fail("verify", msg)
                found, line = self._verify_ea(path, fname, marks)
                mode_note = "（已改用启动配置挂载：终端重启后 EA 不会自动恢复，需要重新分发）"
        snap = self.snapshot()
        self.status(snap)
        algo_ok, algo_msg = self.check_algo(snap)
        extra = {"running": True, "magic": magic, "preset": use_preset, "symbol": symbol}
        if found == "fail":
            self.progress("verify", "fail", f"终端日志显示 EA 没有加载成功：{line}")
            return False, f"EA 加载失败：{line}", {"running": False}
        tail = (f"（{note}）" if note else "") + mode_note
        if not algo_ok:
            self.progress("verify", "warn", algo_msg)
            return True, f"已部署 {desc}，但{algo_msg}{tail}", {**extra, "warn": algo_msg}
        if found == "ok":
            self.progress("verify", "ok", "EA 已加载到图表，算法交易已开启" + tail)
            return True, f"已部署并启动 {desc}{tail}", extra
        tip = "终端日志里还没看到 EA 的加载记录，请在该终端里看一眼图表右上角是否有 EA 名称和笑脸/帽子图标"
        self.progress("verify", "warn", tip)
        return True, f"已部署 {desc}（{tip}）{tail}", {**extra, "warn": tip}

    def _verify_ea(self, path: str, fname: str, marks: dict) -> tuple[str, str]:
        from . import terminal as T
        found, line = "none", ""
        deadline = time.time() + float(self.settings.get("verify_seconds", 20))
        while time.time() < deadline:
            found, line = T.find_ea_in_logs(path, fname, marks)
            if found in ("ok", "fail"):
                break
            time.sleep(1)
        return found, line

    def _step_fail(self, key: str, msg: str):
        self.progress(key, "fail", msg)
        return False, msg, {}

    def cmd_stop_ea(self, p: dict):
        from . import terminal as T
        names = p.get("fileNames") or []
        if not names:
            return True, "没有正在运行的策略", {}
        if self.mock:   # 仅自动化测试
            for key in ("restart", "remove", "attach"):
                self.progress(key, "run")
                time.sleep(0.2)
                self.progress(key, "ok")
            return True, f"已停止 {'、'.join(names)}", {}
        path = self.acc.get("terminal_path")
        if not path:
            return self._step_fail("restart", "这个账户没有设置终端路径")
        self.progress("restart", "run", "正在关闭终端（会先保存图表）")
        ok, msg = self._restart()
        if not ok:
            return self._step_fail("restart", f"关闭终端失败：{msg}")
        self.progress("restart", "ok", msg)
        stems = [Path(n).stem for n in names]
        n = 0
        for name in names:   # 旧版本写在其它图表里的同名 EA 段落也去掉
            n += T.remove_expert_from_charts(path, name)
        sym, tf = self.default_chart()
        try:
            prof = T.prepare_profile(path, sym, tf, None, stems, bool(self.settings.get("allow_dll_import", False)))
            n = max(n, prof["removed"])
        except Exception:
            pass
        self.progress("remove", "ok", f"已从 {n} 个图表移除 {'、'.join(names)}")
        self.progress("attach", "run", "正在重新打开终端")
        ok, msg = self._relaunch(None)
        if not ok:
            return self._step_fail("attach", msg)
        self.progress("attach", "ok", "终端已重新打开并登录")
        return True, f"已从 {n} 个图表移除 {'、'.join(names)} 并重启终端", {}

    # ---------------- 主循环 ----------------
    def handle(self, cmd: dict):
        name, req, params = cmd.get("cmd"), cmd.get("req"), cmd.get("params") or {}
        if name == "update_account":
            self.acc.update(cmd.get("account") or {})
            self._sym_cache = {}          # 后缀 / 品种映射可能改了
            if cmd.get("password"):
                self.password = cmd["password"]
            return
        if name == "connect":
            if params.get("explicit"):
                self._explicit = True
            if self.link != "online":
                ok, msg = self.connect()
            else:
                ok, msg = True, "已在线"
                if self._explicit:
                    try:
                        _, note = self.apply_algo_pref(self.snapshot())
                    except Exception as e:
                        note = f"读取终端状态失败：{e}"
                    if note:
                        msg += "，" + note
            self._explicit = False
            self.emit(type="result", req=req, ok=ok, message=msg)
            return
        if name == "disconnect":
            self.disconnect()
            if params.get("close_terminal") and not self.mock and self.own:
                ok, msg = self.close_own_terminal()
                self.emit(type="result", req=req, ok=ok, message="已断开并" + msg)
            else:
                self.emit(type="result", req=req, ok=True, message="已断开（终端保持运行）")
            self.running = False
            return
        if name == "refresh":
            if self.link != "online":
                self.emit(type="result", req=req, ok=False, message="终端未登录")
            else:
                self.poll()
                self.emit(type="result", req=req, ok=True, message="已刷新")
            return
        if name == "watch":
            syms = [str(x) for x in (params.get("symbols") or []) if str(x).strip()][:4]
            if syms != self.watch:
                self.watch = syms
                self._sym_cache = {k: v for k, v in self._sym_cache.items() if k in syms}
                if self.link == "online":
                    self.poll()
            self.emit(type="result", req=req, ok=True, message="已更新关注品种")
            return
        handlers = {"order": self.cmd_order, "close": self.cmd_close, "modify": self.cmd_modify,
                    "cancel_orders": self.cmd_cancel_orders, "history": self.cmd_history,
                    "deploy": self.cmd_deploy, "stop_ea": self.cmd_stop_ea,
                    "order_pair": self.cmd_order_pair, "nuke": self.cmd_nuke, "algo": self.cmd_algo,
                    "lock": self.cmd_lock}
        fn = handlers.get(name)
        if fn is None:
            self.emit(type="result", req=req, ok=False, message=f"未知命令 {name}")
            return
        if self.link != "online" and name not in ("deploy", "stop_ea"):
            self.emit(type="result", req=req, ok=False, message="终端未登录")
            return
        if self.link != "online":
            self.emit(type="result", req=req, ok=False, message="终端未登录，先批量登录再部署/停止策略")
            return
        try:
            # 下单/平仓等只需工具栏为开；面板算法关着也能做，且不会再关工具栏、不改 algo_off
            # 交易路径绝不重启终端 / 不卸图表 EA（v1.5.14 起）
            if name in ("close", "modify", "cancel_orders", "nuke", "lock", "order", "order_pair"):
                ok, msg, detail = self.with_algo_for_trade(fn, params, guard_experts=False)
            else:
                ok, msg, detail = fn(params)
        except Exception as e:
            ok, msg, detail = False, f"执行出错：{e}", {"trace": traceback.format_exc()[-800:]}
        self.emit(type="result", req=req, ok=ok, message=msg, detail=detail)
        if name in ("order", "close", "modify", "cancel_orders", "order_pair", "nuke", "algo", "lock"):
            self.poll()

    def run(self, init_req=None):
        ok, msg = self.connect()
        self.emit(type="result", req=init_req, ok=ok, message=msg)
        if not ok:
            self.emit(type="exit", link="error", linkError=msg)
            return
        interval = float(self.settings.get("poll_interval", 1.5))
        next_poll = time.time() + interval
        while self.running:
            timeout = max(0.05, next_poll - time.time())
            try:
                cmd = self.cmd_q.get(timeout=timeout)
            except queue.Empty:
                cmd = None
            except (EOFError, OSError):
                break
            if cmd is not None:
                if cmd.get("cmd") == "exit":
                    self.disconnect()
                    break
                try:
                    self.handle(cmd)
                except Exception as e:
                    self.emit(type="result", req=cmd.get("req"), ok=False, message=f"内部错误：{e}")
                continue
            if time.time() >= next_poll:
                next_poll = time.time() + interval
                if self.link == "online":
                    self.poll()
                else:
                    self.maybe_reconnect()
        self.wipe_ini()
        self.emit(type="exit")


def worker_main(acc, password, settings, cmd_q, evt_q, mock, data_dir, init_req=None, own=None, root=""):
    w = Worker(acc, password, settings, cmd_q, evt_q, mock, data_dir, own, root)
    try:
        w.run(init_req)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        w.emit(type="status", link="error", linkError=f"工作进程崩溃：{e}", snapshot=None, ts=time.time())
        w.emit(type="exit")
