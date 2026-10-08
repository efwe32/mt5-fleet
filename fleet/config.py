"""路径、设置与账户台账（accounts.json / settings.json）。"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import secrets

SYMBOLS = ["USDJPYc", "XAUUSDc", "EURUSDc", "XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "NAS100", "US30", "BTCUSD"]
TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"]


def app_root() -> Path:
    """程序所在目录（打包成 exe 时为 exe 所在目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def terminals_root() -> Path:
    """受管终端目录：程序目录\\terminals。每个账户一个子文件夹，base 是模板。
    （环境变量 FLEET_TERMINALS_ROOT 仅供自动化测试改到临时目录。）"""
    env = os.environ.get("FLEET_TERMINALS_ROOT")
    return Path(env) if env else app_root() / "terminals"


def default_terminal_path(login: str) -> str:
    return str(terminals_root() / str(login) / "terminal64.exe")


def static_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", app_root()))
    return base / "static"


DEFAULT_SETTINGS: dict[str, Any] = {
    "max_lots": 1.0,            # 单笔最大手数保护
    "max_total_lots": 10.0,     # 单次批量下单所有账户合计最大手数
    "poll_interval": 1.5,       # 秒
    "deviation": 20,            # 市价单允许滑点（点）
    "default_magic": 880000,
    "scale_groups": ["跟单"],   # 手数缩放默认只对这些分组生效
    "ini_encoding": "utf-16",   # MT5 启动配置文件编码
    "close_terminal_on_disconnect": False,  # 断开时是否关闭（本程序启动的）终端
    "close_terminals_on_exit": True,
    "auto_enable_algo": True,               # 本程序启动终端时自动打开它的算法交易
    "template_dir": "",          # 模板 MT5 目录（空 = 自动在程序目录里找）；只读取，不会改动
    "allow_dll_import": False,   # 启动配置里的「允许 DLL 导入」
    "login_concurrency": 3,      # 一键登录同时处理的账户数
    "dist_concurrency": 2,       # 分发 EA 同时处理的账户数
    "login_retry_delay": 12,     # 第一次登录失败（服务器还没搜到）后等待几秒再试一次
    "verify_seconds": 20,        # 分发后最多等几秒在终端日志里确认 EA 已加载
    "known_servers": [],         # 用过的服务器名（自动补全）
    "default_symbol": "USDJPYc", # 分发 EA / 登录后默认图表的品种（券商实际名称，含后缀）
    "default_timeframe": "M15",  # 分发 EA / 登录后默认图表的周期
    "single_chart": True,        # 启动受管终端前把图表整理成一个（默认品种/周期；有 EA 的图表保留）
    "auto_reattach": True,       # 程序启动时自动接管上次本程序启动、仍在运行的终端
    "update_wait": 120,          # 终端启动后若因 MT5 自动更新而重启，最多等几秒接管新进程
    "quick_no_confirm": False,   # 交易页快捷面板：开仓/平仓不弹确认（核弹级全平和自动全平始终确认）
    "update_repo": "efwe32/mt5-fleet",  # 在线更新：GitHub 公开仓库（不需要登录）
    "update_url": "",            # 在线更新：自定义更新地址（latest.json 直链，GitHub 打不开时用）
    "update_mirror": True,       # github.com 下载慢时改走加速镜像（内容按 sha256 校验）
    "update_auto_check": True,   # 打开网页时检查有没有新版本（只提示，不会自动安装）
}


class Store:
    """线程安全的 JSON 台账。"""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "logs").mkdir(exist_ok=True)
        (self.data_dir / "ea_library").mkdir(exist_ok=True)
        self.accounts_path = self.data_dir / "accounts.json"
        self.settings_path = self.data_dir / "settings.json"
        self._lock = threading.RLock()
        self.first_launch = not self.settings_path.exists()
        self.settings = {**DEFAULT_SETTINGS, **self._read(self.settings_path, {})}
        self.settings.pop("terminal_root", None)
        self.settings.pop("dry_run", None)   # 旧版本的 dry_run 开关已取消：所有操作都直接实盘发送
        self.accounts: list[dict] = [self._normalize(a) for a in self._read(self.accounts_path, {"accounts": []}).get("accounts", [])]
        self.save_settings()

    # ---------- io ----------
    @staticmethod
    def _read(path: Path, default):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except Exception as e:  # 损坏的文件备份后重建
            bak = path.with_suffix(path.suffix + f".broken-{int(time.time())}")
            try:
                path.rename(bak)
            except Exception:
                pass
            print(f"[警告] 读取 {path} 失败（{e}），已备份为 {bak.name}")
            return default

    @staticmethod
    def _write(path: Path, data):
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def save_settings(self):
        with self._lock:
            self._write(self.settings_path, self.settings)

    def save_accounts(self):
        with self._lock:
            self._write(self.accounts_path, {"version": 1, "accounts": self.accounts})

    # ---------- accounts ----------
    @staticmethod
    def _normalize(a: dict) -> dict:
        return {
            "id": a.get("id") or f"acct-{uuid.uuid4().hex[:8]}",
            "alias": (a.get("alias") or a.get("login") or "").strip(),
            "login": str(a.get("login") or "").strip(),
            "server": (a.get("server") or "").strip(),
            "group": (a.get("group") or "未分组").strip() or "未分组",
            "password": a.get("password") or "",
            "terminal_path": (a.get("terminal_path") or "").strip(),
            "enabled": a.get("enabled", True) is not False,
            "lot_multiplier": float(a.get("lot_multiplier") or 1.0),
            "symbol_suffix": (a.get("symbol_suffix") or "").strip(),
            "symbol_map": (a.get("symbol_map") or "").strip(),
            "strategies": list(a.get("strategies") or []),
            "last": dict(a.get("last") or {}),
        }

    def get(self, acc_id: str) -> dict | None:
        with self._lock:
            return next((a for a in self.accounts if a["id"] == acc_id), None)

    def public(self, a: dict) -> dict:
        d = {k: v for k, v in a.items() if k != "password"}
        d["has_password"] = bool(a.get("password"))
        d["password_protection"] = secrets.protection_kind(a.get("password", ""))
        return d

    def validate(self, data: dict, acc_id: str | None = None) -> str | None:
        alias = (data.get("alias") or "").strip()
        login = str(data.get("login") or "").strip()
        server = (data.get("server") or "").strip()
        if not alias:
            return "请填写备注"
        if not login.isdigit() or len(login) < 3:
            return "账号需为至少 3 位数字"
        if not server:
            return "请填写服务器"
        for a in self.accounts:
            if a["id"] == acc_id:
                continue
            if a["login"] == login and a["server"] == server:
                return "这个账号和服务器已经在列表里"
            tp = (data.get("terminal_path") or "").strip() or default_terminal_path(login)
            if a["terminal_path"] and os.path.normcase(a["terminal_path"]) == os.path.normcase(tp):
                return f"终端路径和「{a['alias']}」重复。一个 MT5 终端同时只能登录一个账户，请为每个账户复制一份独立的 MT5 文件夹"
        from .terminal import check_managed_path
        perr = check_managed_path((data.get("terminal_path") or "").strip() or default_terminal_path(login), terminals_root())
        if perr:
            return perr
        try:
            m = float(data.get("lot_multiplier") or 1)
            if m <= 0 or m > 100:
                return "手数倍数需在 0~100 之间"
        except ValueError:
            return "手数倍数需为数字"
        return None

    def add(self, data: dict) -> dict:
        with self._lock:
            a = self._normalize({**data, "id": None, "password": secrets.encrypt(data.get("password") or "")})
            if not a["terminal_path"]:
                a["terminal_path"] = default_terminal_path(a["login"])
            self.accounts.insert(0, a)
            self.save_accounts()
            return a

    def update(self, acc_id: str, data: dict) -> dict | None:
        with self._lock:
            a = self.get(acc_id)
            if not a:
                return None
            for k in ("alias", "login", "server", "group", "terminal_path", "symbol_suffix", "symbol_map"):
                if k in data and data[k] is not None:
                    a[k] = str(data[k]).strip()
            a["group"] = a["group"] or "未分组"
            if not a["terminal_path"]:
                a["terminal_path"] = default_terminal_path(a["login"])
            if data.get("lot_multiplier") not in (None, ""):
                a["lot_multiplier"] = float(data["lot_multiplier"])
            if data.get("password"):
                a["password"] = secrets.encrypt(data["password"])
            self.save_accounts()
            return a

    def remove(self, ids: list[str]) -> list[dict]:
        with self._lock:
            gone = [a for a in self.accounts if a["id"] in ids]
            self.accounts = [a for a in self.accounts if a["id"] not in ids]
            self.save_accounts()
            return gone

    def password_of(self, a: dict) -> str:
        return secrets.decrypt(a.get("password", ""))


def mock_seed_accounts() -> list[dict]:
    """--mock（仅供自动化测试）用的示例账户。"""
    rows = [
        ("主仓 · 黄金", "51002811", "ICMarketsSC-MT5", "主仓", [("GoldPulse.ex5", "XAUUSD", "H1", 88001, "gold.set", True)]),
        ("主仓 · 欧美", "51002844", "ICMarketsSC-MT5", "主仓", [("EuroBreak.ex5", "EURUSD", "M15", 88002, "euro.set", True)]),
        ("跟单 A", "88011203", "Pepperstone-Edge-Live", "跟单", [("CopyBridge.ex5", "NAS100", "M5", 33011, "", True)]),
        ("跟单 B", "88011290", "Pepperstone-Edge-Live", "跟单", []),
        ("测试 · 小号", "100245", "Exness-MT5Trial", "测试", []),
        ("备用 · 纳指", "7720011", "FTMO-Demo", "测试", [("NasdaqIdle.ex5", "US30", "H4", 77001, "", False)]),
    ]
    out = []
    for i, (alias, login, server, group, strats) in enumerate(rows, 1):
        out.append({
            "id": f"a{i}", "alias": alias, "login": login, "server": server, "group": group,
            "password": secrets.encrypt("mock-pass"),
            "terminal_path": default_terminal_path(login),
            "lot_multiplier": 0.5 if login == "88011290" else 1.0,
            "strategies": [{"id": f"s{i}{j}", "fileName": f, "symbol": s, "timeframe": t, "magic": m,
                            "preset": p, "running": r} for j, (f, s, t, m, p, r) in enumerate(strats)],
        })
    return out
