"""浮亏警报：到阈值响一次，回到阈值以上才重新武装。"""
from __future__ import annotations

POPUP_MS = 180_000  # 首页弹出信息停留 3 分钟


def loss_threshold(settings: dict) -> float:
    try:
        v = float(settings.get("alert_loss", 3000))
    except (TypeError, ValueError):
        return 3000.0
    return v if v > 0 else 3000.0


def scan(rows: list[dict], threshold: float, armed: dict) -> tuple[list[dict], dict]:
    """rows: 在线账户 {id, alias, login, floating, currency}。

    armed[id] 为真表示这次跌破已经报过，要等浮亏回到阈值以上（floating > -threshold）才清除。
    用户查看并取消警报不会重新武装：还在阈值以下时不会再响。
    离线账户不参与，已武装的状态保留，避免断线重连又响一次。
    """
    armed = dict(armed)
    fresh = []
    for a in rows:
        aid = str(a.get("id") or "")
        if not aid:
            continue
        try:
            fl = float(a.get("floating") or 0)
        except (TypeError, ValueError):
            continue
        if fl > -threshold:
            armed.pop(aid, None)
            continue
        if armed.get(aid):
            continue
        armed[aid] = True
        fresh.append({
            "accountId": aid,
            "alias": a.get("alias") or aid,
            "login": str(a.get("login") or ""),
            "floating": round(fl, 2),
            "currency": a.get("currency") or "",
        })
    return fresh, armed
