"""USDJPY 报价。优先用已登录终端的报价；没有终端时用公开行情（Yahoo USDJPY=X）。"""
from __future__ import annotations

import json
import urllib.request

UA = "Mozilla/5.0"


def public_usdjpy(timeout: float = 8) -> dict:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/USDJPY=X?interval=1d&range=5d"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    meta = data["chart"]["result"][0]["meta"]
    price = float(meta.get("regularMarketPrice") or 0)
    prev = float(meta.get("chartPreviousClose") or meta.get("previousClose") or 0)
    if price <= 0:
        raise RuntimeError("公开行情没有 USDJPY 价格")
    pct = (price - prev) / prev * 100 if prev else 0.0
    return {
        "symbol": "USDJPY",
        "price": price,
        "prev": prev,
        "change": round(pct, 3),
        "source": "公开行情",
        "digits": 3,
    }


def from_terminal(q: dict | None) -> dict | None:
    """把终端快照里的 usdjpy 字段换成面板用的结构。"""
    if not q or not q.get("bid"):
        return None
    price = float(q["bid"])
    prev = float(q.get("prev") or 0)
    pct = (price - prev) / prev * 100 if prev else 0.0
    return {
        "symbol": q.get("symbol") or "USDJPY",
        "price": price,
        "prev": prev,
        "change": round(pct, 3),
        "source": "MT5",
        "digits": int(q.get("digits") or 3),
    }
