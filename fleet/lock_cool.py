"""锁单冷却（lock cooldown）。

「锁单」= 同一品种上同时存在多头和空头持仓（对锁 / 锁仓）。
典型来源：亏损锁仓 EA（如 USDJPY_Sim_V2_锁3333）在单边浮亏达标后开反向单。

规则：
- 一旦检测到锁单，账户进入「冷却」：禁止新开仓，直到该账户完全清仓（无任何持仓）。
- 冷却标志写入 accounts.json（lock_cooldown），面板 / 终端重启后仍有效；
  上线时若仍有持仓且已标记冷却、或再次看到锁单，继续冷却，并保持算法交易关闭，
  避免 EA 因「算法交易重新打开」清掉内部的「锁仓停建仓」标志后继续加仓。
- 冷却中允许：平仓 / 核弹级全平 / 改单 / 撤挂单；禁止：快捷开多开空开单、批量下单、分发/挂载会开新仓的路径、手动一键开算法。
"""
from __future__ import annotations


def lock_symbols(positions: list | None) -> list[str]:
    """返回同时有多、空持仓的品种（按名字排序）。"""
    buy, sell = {}, {}
    for p in positions or []:
        sym = str(p.get("symbol") or "")
        if not sym:
            continue
        side = p.get("side")
        vol = float(p.get("volume") or 0)
        if vol <= 0:
            continue
        if side == "buy":
            buy[sym] = buy.get(sym, 0) + vol
        elif side == "sell":
            sell[sym] = sell.get(sym, 0) + vol
    return sorted(s for s in buy if sell.get(s, 0) > 0)


def is_flat(positions: list | None) -> bool:
    return not any(float(p.get("volume") or 0) > 0 for p in (positions or []))


def cooldown_message(a: dict, symbols: list[str] | None = None) -> str:
    syms = symbols if symbols is not None else list(a.get("lock_symbols") or [])
    if syms:
        return f"冷却中：有锁单（{'、'.join(syms[:4])}{'…' if len(syms) > 4 else ''}），禁止新开仓，清仓后自动恢复"
    return "冷却中：有锁单禁止新开仓，清仓后自动恢复"


def sync(account: dict, positions: list | None, now: int | None = None) -> tuple[bool, str]:
    """根据持仓更新 account 的 lock_cooldown / lock_symbols / lock_cooldown_at。

    返回 (是否有字段变化, 事件说明；无变化时说明为空)。
    """
    import time
    now = int(now if now is not None else time.time())
    syms = lock_symbols(positions)
    flat = is_flat(positions)
    was = bool(account.get("lock_cooldown"))
    old_syms = list(account.get("lock_symbols") or [])
    changed = False
    note = ""

    if syms:
        # 检测到锁单 → 进入 / 保持冷却
        if not was:
            account["lock_cooldown"] = True
            account["lock_cooldown_at"] = now
            changed = True
            note = f"检测到锁单（{'、'.join(syms)}），进入冷却：禁止新开仓直到清仓"
        if syms != old_syms:
            account["lock_symbols"] = syms
            changed = True
        elif not account.get("lock_symbols"):
            account["lock_symbols"] = syms
            changed = True
    elif was and flat:
        account["lock_cooldown"] = False
        account["lock_symbols"] = []
        account["lock_cooldown_at"] = 0
        changed = True
        note = "已清仓，锁单冷却解除，允许重新开仓"
    elif was and not flat:
        # 冷却中但仍有持仓（可能已不是对锁，仍要等清仓）
        if old_syms and not account.get("lock_symbols"):
            account["lock_symbols"] = old_syms
            changed = True
    else:
        # 未冷却且无锁单
        if account.get("lock_symbols"):
            account["lock_symbols"] = []
            changed = True

    return changed, note


def public_fields(account: dict) -> dict:
    on = bool(account.get("lock_cooldown"))
    syms = list(account.get("lock_symbols") or [])
    return {
        "lockCooldown": on,
        "lockSymbols": syms,
        "lockCooldownAt": int(account.get("lock_cooldown_at") or 0),
        "lockCooldownMsg": cooldown_message(account, syms) if on else "",
    }
