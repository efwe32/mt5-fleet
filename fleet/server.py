"""本地网页界面 + API（只监听 127.0.0.1）。"""
from __future__ import annotations

import csv
import io
import os
import re
import secrets as pysecrets
import threading
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import app_root, SYMBOLS, TIMEFRAMES, default_terminal_path, static_dir, terminals_root
from .manager import QUICK_ACTIONS, Fleet, QuickBusy
from . import terminal as T
from . import __version__
from .updater import UpdateError
from .remote import check_password as _check_remote_password

EXAMPLE_CSV = ("alias,login,password,server,group,terminal_path,symbol_suffix,lot_multiplier\r\n"
               "主仓 · 黄金,51002811,你的密码,ICMarketsSC-MT5,主仓,,,1\r\n"
               "跟单 A,88011203,你的密码,Pepperstone-Edge-Live,跟单,,.r,0.5\r\n")
# terminal_path 留空 = 程序目录\terminals\<账号>\terminal64.exe


def parse_csv(text: str) -> list[dict]:
    text = text.lstrip("\ufeff")
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        return []
    head = [c.strip().lower() for c in rows[0]]
    has_head = "login" in head or "账号" in head
    cols = ["alias", "login", "password", "server", "group", "terminal_path", "symbol_suffix", "lot_multiplier"]
    if has_head:
        zh = {"备注": "alias", "账号": "login", "密码": "password", "服务器": "server", "分组": "group",
              "终端路径": "terminal_path", "后缀": "symbol_suffix", "倍数": "lot_multiplier"}
        cols = [zh.get(h, h) for h in head]
        rows = rows[1:]
    out = []
    for r in rows:
        d = {cols[i]: c.strip() for i, c in enumerate(r) if i < len(cols)}
        if d.get("login") and d.get("server"):
            d["alias"] = d.get("alias") or d["login"]
            d["group"] = d.get("group") or "未分组"
            out.append(d)
    return out


def parse_quick(text: str) -> tuple[list[dict], list[str]]:
    """批量粘贴：每行 账号,密码,服务器[,分组,名称]（逗号 / 中文逗号 / Tab 分隔）。"""
    rows, errors = [], []
    for n, line in enumerate((text or "").lstrip("\ufeff").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in re.split(r"[,，\t]", line)]
        if len(parts) < 3:
            errors.append(f"第 {n} 行：至少需要 账号,密码,服务器")
            continue
        if n == 1 and not parts[0].isdigit() and parts[0] in ("账号", "login", "帐号"):
            continue
        rows.append({"login": parts[0], "password": parts[1], "server": parts[2],
                     "group": parts[3] if len(parts) > 3 else "", "alias": parts[4] if len(parts) > 4 else ""})
    return rows, errors


REMOTE_LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>远程访问</title>
<style>
body{margin:0;background:#12140f;color:#f3f0e4;font-family:"Microsoft YaHei","PingFang SC",sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center}
form{width:min(420px,calc(100% - 32px));background:#1b1e16;border:1px solid #3a3f32;border-radius:12px;padding:20px}
h1{font-size:18px;margin:0 0 8px}p{color:#b4b09a;font-size:13px;line-height:1.5}
input{width:100%;box-sizing:border-box;height:48px;border-radius:8px;border:1px solid #3a3f32;background:#12140f;color:#f3f0e4;padding:0 12px;font-size:16px}
button{width:100%;height:48px;margin-top:12px;border:0;border-radius:8px;background:#e0b15a;color:#1c1408;font-weight:700;font-size:16px}
.err{color:#ef8b74;min-height:1.2em;font-size:13px}
</style></head><body>
<form id="f"><h1>MT5 批量终端</h1><p>这是加密的远程入口。输入这台电脑上设置的远程密码。交易账户的密码不会出现在这个页面里。</p>
<input id="pw" type="password" autocomplete="current-password" placeholder="远程密码" autofocus>
<div class="err" id="e"></div><button type="submit">进入</button></form>
<script>
document.getElementById("f").onsubmit=async function(ev){ev.preventDefault();
 const e=document.getElementById("e"); e.textContent="正在核对…";
 try{const r=await fetch("/api/remote/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password:document.getElementById("pw").value})});
  const d=await r.json().catch(()=>({}));
  if(!r.ok){e.textContent=d.detail||"没有通过";return;}
  location.replace("/");
 }catch(err){e.textContent="网络中断";}};
</script></body></html>"""

def create_app(fleet: Fleet, port: int, on_exit=None, updater=None) -> FastAPI:
    app = FastAPI(title="MT5 Fleet", docs_url=None, redoc_url=None, openapi_url=None)
    token = pysecrets.token_urlsafe(24)
    app.state.token = token
    app.state.remote_port = 0
    busy = threading.Lock()
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost"}

    def _client_ip(request: Request) -> str:
        ip = (request.headers.get("cf-connecting-ip") or "").strip()
        if not ip:
            ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if not ip and request.client:
            ip = request.client.host or ""
        return ip or "remote"

    def _remote_port(request: Request) -> bool:
        srv = request.scope.get("server") or ("", 0)
        rp = int(getattr(app.state, "remote_port", 0) or 0)
        try:
            return rp and int(srv[1]) == rp
        except (TypeError, ValueError):
            return False

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # 防 DNS 重绑定 + 防其它网页跨站调用（需要页面里的随机令牌）
        if _remote_port(request):
            if not fleet.remote.enabled():
                return PlainTextResponse("远程访问已关闭", status_code=403)
            path = request.url.path
            if path == "/api/remote/login":
                return await call_next(request)
            if path.startswith("/static/"):
                return await call_next(request)
            if not fleet.remote.valid_session(request.cookies.get("fleet_remote")):
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "请先输入远程密码"}, status_code=401)
                if path in ("/", ""):
                    return HTMLResponse(REMOTE_LOGIN_HTML, headers={"Cache-Control": "no-store"})
                return PlainTextResponse("请先输入远程密码", status_code=401)
            if path.startswith("/api/"):
                got = request.headers.get("x-fleet-token") or request.query_params.get("token")
                if got != token:
                    return JSONResponse({"detail": "令牌无效，请刷新页面"}, status_code=401)
            return await call_next(request)
        host = request.headers.get("host", "")
        if host not in allowed_hosts:
            return PlainTextResponse("forbidden host", status_code=403)
        if request.url.path.startswith("/api/"):
            got = request.headers.get("x-fleet-token") or request.query_params.get("token")
            if got != token:
                return JSONResponse({"detail": "令牌无效，请刷新页面"}, status_code=401)
        return await call_next(request)

    def run_exclusive(fn, *a, **kw):
        if not busy.acquire(blocking=False):
            raise HTTPException(409, "上一项还在执行")
        try:
            return fn(*a, **kw)
        finally:
            busy.release()

    def ids_of(body: dict) -> list[str]:
        ids = body.get("ids") or []
        if not isinstance(ids, list) or not ids:
            raise HTTPException(400, "请先勾选账户")
        return [str(i) for i in ids]

    sd = static_dir()

    def busy_reason() -> str:
        if busy.locked() or fleet.busy.locked() or fleet.qlock.locked():
            return "有批量操作（登录 / 下单 / 分发…）正在执行，请等它完成后再更新"
        if fleet.prog and not fleet.prog.get("done", True):
            return "有批量操作正在执行，请等它完成后再更新"
        return ""

    if updater is not None:
        updater.busy_check = busy_reason

    @app.get("/", response_class=HTMLResponse)
    def index():
        html = (sd / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__FLEET_TOKEN__", token), headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=str(sd)), name="static")

    # ---------- 状态 ----------
    @app.get("/api/state")
    def state():
        st = fleet.state()
        st["symbols"], st["timeframes"] = SYMBOLS, TIMEFRAMES
        st["busy"] = busy.locked()
        return st

    @app.post("/api/alerts/view")
    def view_alert(body: dict):
        return {"ok": fleet.view_alert(str(body.get("id") or ""))}

    @app.post("/api/mock/floating")
    def mock_floating(body: dict):
        if not fleet.mock:
            raise HTTPException(404, "没有这个接口")
        try:
            floating = float(body.get("floating"))
        except (TypeError, ValueError):
            raise HTTPException(400, "浮亏金额不对")
        ok, msg = fleet.set_mock_floating(str(body.get("id") or ""), floating)
        if not ok:
            raise HTTPException(400, msg)
        return {"ok": True, "message": msg}

    @app.post("/api/remote/login")
    def remote_login(request: Request, body: dict):
        if not _remote_port(request):
            raise HTTPException(404, "没有这个接口")
        if not fleet.remote.enabled():
            raise HTTPException(403, "远程访问已关闭")
        ip = _client_ip(request)
        if fleet.remote.locked(ip):
            left = max(1, fleet.remote.lock_left(ip) // 60)
            raise HTTPException(429, f"密码错误次数过多，请 {left} 分钟后再试")
        if not fleet.remote.has_password() or not _check_remote_password(str(body.get("password") or ""), fleet.store.settings.get("remote_pass_hash") or ""):
            locked = fleet.remote.fail(ip)
            if locked:
                raise HTTPException(429, "密码错误次数过多，请 10 分钟后再试")
            raise HTTPException(401, "远程密码错误")
        fleet.remote.succeed(ip)
        sid = fleet.remote.new_session()
        resp = JSONResponse({"ok": True})
        resp.set_cookie("fleet_remote", sid, max_age=12 * 3600, httponly=True, secure=True, samesite="lax", path="/")
        return resp

    # ---------- 账户 ----------
    @app.post("/api/accounts")
    def add_account(body: dict):
        err = fleet.store.validate(body)
        if err:
            raise HTTPException(400, err)
        a = fleet.store.add(body)
        fleet.log(a["id"], "添加账户", True, "已加入台账" + ("" if body.get("password") else "，没有密码，暂时不能登录"))
        return fleet.store.public(a)

    @app.put("/api/accounts/{acc_id}")
    def edit_account(acc_id: str, body: dict):
        if not fleet.store.get(acc_id):
            raise HTTPException(404, "账户不存在")
        err = fleet.store.validate(body, acc_id)
        if err:
            raise HTTPException(400, err)
        a = fleet.store.update(acc_id, body)
        fleet.account_changed(acc_id, body.get("password") or None)
        fleet.log(acc_id, "修改账户", True, "已更新账户资料" + ("（含密码）" if body.get("password") else ""))
        return fleet.store.public(a)

    @app.post("/api/accounts/{acc_id}/toggle")
    def toggle(acc_id: str):
        a = run_exclusive(fleet.toggle_enabled, acc_id)
        if not a:
            raise HTTPException(404, "账户不存在")
        return {"enabled": a["enabled"]}

    @app.post("/api/accounts/delete")
    def delete(body: dict):
        n = run_exclusive(fleet.remove, ids_of(body))
        fleet.banner = {"title": "删除", "ok": n, "fail": 0}
        return {"removed": n}

    @app.post("/api/accounts/import")
    def import_csv(body: dict):
        rows = parse_csv(body.get("text") or "")
        added, errors = 0, []
        for r in rows:
            err = fleet.store.validate(r)
            if err:
                errors.append(f"{r.get('login')}: {err}")
                continue
            a = fleet.store.add(r)
            added += 1
            fleet.log(a["id"], "导入", True, "已从 CSV 导入" + ("，密码已加密保存" if r.get("password") else "，没有密码，暂时不能登录"))
        return {"added": added, "errors": errors}

    @app.post("/api/accounts/quick")
    def quick_add(body: dict):
        """添加并登录：单个（rows=[{...}]）或批量粘贴（text）。"""
        rows = body.get("rows")
        perr: list[str] = []
        if not isinstance(rows, list):
            rows, perr = parse_quick(str(body.get("text") or ""))
        rows = [r for r in rows if isinstance(r, dict)][:200]
        if not rows:
            raise HTTPException(400, "；".join(perr) or "没有识别到账户，请按 账号,密码,服务器 填写")
        out = run_exclusive(fleet.quick_add, rows)
        out["errors"] = perr + out.get("errors", [])
        return out

    @app.get("/api/accounts/example.csv")
    def example_csv():
        return Response(("\ufeff" + EXAMPLE_CSV).encode("utf-8"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=fleet-accounts-example.csv"})

    # ---------- 连接 ----------
    @app.post("/api/login")
    def login(body: dict):
        return run_exclusive(fleet.login, ids_of(body))

    @app.post("/api/disconnect")
    def disconnect(body: dict):
        return run_exclusive(fleet.disconnect, ids_of(body))

    @app.post("/api/refresh")
    def refresh(body: dict):
        return run_exclusive(fleet.refresh, ids_of(body))

    # ---------- 交易 ----------
    def order_params(body: dict) -> dict:
        p = dict(body.get("order") or {})
        sym = str(p.get("symbol") or "").strip()
        if not sym:
            raise HTTPException(400, "请填写品种")
        try:
            p["lots"] = float(p.get("lots"))
        except (TypeError, ValueError):
            raise HTTPException(400, "手数需为数字")
        if p["lots"] <= 0:
            raise HTTPException(400, "手数需大于 0")
        for k in ("sl", "tp", "price", "ref_equity"):
            try:
                p[k] = float(p.get(k) or 0)
            except ValueError:
                raise HTTPException(400, f"{k} 需为数字")
        try:
            p["magic"] = int(p.get("magic") or 0)
        except ValueError:
            raise HTTPException(400, "魔术号需为整数")
        p["symbol"] = sym
        return p

    @app.post("/api/trade/preview")
    def preview(body: dict):
        return fleet.preview_order(ids_of(body), order_params(body))

    @app.post("/api/trade/order")
    def order(body: dict):
        return run_exclusive(fleet.order, ids_of(body), order_params(body))

    @app.post("/api/trade/close")
    def close(body: dict):
        f = body.get("filter") or {"mode": "all"}
        if f.get("mode") == "magic":
            try:
                f["magic"] = int(f.get("magic"))
            except (TypeError, ValueError):
                raise HTTPException(400, "魔术号需为整数")
        title = {"all": "全平", "long": "平多", "short": "平空", "symbol": "按品种平", "magic": "按魔术号平",
                 "profit": "平盈利单", "loss": "平亏损单", "ticket": "平单笔"}.get(f.get("mode"), "平仓")
        return run_exclusive(fleet.run_batch, title, "close", ids_of(body), f, 90)

    @app.post("/api/trade/modify")
    def modify(body: dict):
        p = body.get("modify") or {}
        return run_exclusive(fleet.run_batch, "改止损止盈", "modify", ids_of(body), p, 60)

    @app.post("/api/trade/cancel")
    def cancel(body: dict):
        return run_exclusive(fleet.run_batch, "撤挂单", "cancel_orders", ids_of(body), {"symbol": body.get("symbol") or ""}, 60)

    # ---------- 策略 ----------
    # ---------- 快捷交易面板（交易页顶部） ----------
    SYM_RE = re.compile(r"[A-Za-z0-9._#&+\-]{1,32}")

    def quick_ids(text: str) -> list[str]:
        return [x.strip() for x in text.split(",") if x.strip()][:200]

    @app.get("/api/quick/summary")
    def quick_summary(symbol: str = "", accounts: str = ""):
        """快捷面板实时数据：所选账户在这个品种上的报价、多空层数/手数/均价、今日/历史平仓盈亏、回撤、自动全平状态（只读）。"""
        sym = symbol.strip() or str(fleet.store.settings.get("default_symbol") or "USDJPYc")
        if not SYM_RE.fullmatch(sym):
            raise HTTPException(400, "品种格式不对（例如 USDJPYc）")
        return fleet.quick_summary(sym, quick_ids(accounts))

    @app.post("/api/quick/action")
    def quick_action(body: dict):
        action = str(body.get("action") or "")
        if action not in QUICK_ACTIONS:
            raise HTTPException(400, "未知的快捷操作")
        ids = ids_of(body)
        sym = str(body.get("symbol") or "").strip()
        p: dict = {"symbol": sym, "only_symbol": bool(body.get("only_symbol"))}
        if action in ("buy", "sell", "pair") or (action != "nuke" and p["only_symbol"]):
            if not SYM_RE.fullmatch(sym):
                raise HTTPException(400, "请填写正确的品种（例如 USDJPYc）")
        if action in ("buy", "sell", "pair"):
            try:
                p["lots"] = float(body.get("lots"))
            except (TypeError, ValueError):
                raise HTTPException(400, "手数需为数字")
            if not 0 < p["lots"] <= 1000:
                raise HTTPException(400, "手数需大于 0")
            mode = str(body.get("scale_mode") or "fixed")
            if mode not in ("fixed", "multiplier", "equity"):
                raise HTTPException(400, "手数模式不对")
            p["scale_mode"] = mode
            try:
                p["ref_equity"] = float(body.get("ref_equity") or 0)
            except (TypeError, ValueError):
                raise HTTPException(400, "基准净值需为数字")
            if mode == "equity" and p["ref_equity"] <= 0:
                raise HTTPException(400, "按净值比例需要填写基准净值")
        try:
            return fleet.quick_action(action, ids, p)
        except QuickBusy as e:
            raise HTTPException(409, str(e))

    @app.post("/api/quick/auto")
    def quick_auto(body: dict):
        """自动全平：amount > 0 = 合计浮动盈亏达到该金额（止盈）、< 0 = 亏到该金额（止损）时核弹级全平；0 = 取消。"""
        try:
            amount = round(float(body.get("amount") or 0), 2)
        except (TypeError, ValueError):
            raise HTTPException(400, "金额需为数字")
        ids = [str(i) for i in (body.get("ids") or [])][:200]
        try:
            return fleet.arm_auto(amount, ids)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/strategy/upload")
    async def upload(file: UploadFile = File(...)):
        name = os.path.basename(file.filename or "")
        if not re.fullmatch(r"[^\\/:*?\"<>|]+\.(ex5|set)", name, re.I):
            raise HTTPException(400, "只接受 .ex5 或 .set 文件")
        data = await file.read()
        if len(data) > 50 * 1024 * 1024:
            raise HTTPException(400, "文件太大")
        dst = fleet.store.data_dir / "ea_library" / name
        dst.write_bytes(data)
        fleet.log("", "上传策略文件", True, f"{name}（{len(data)} 字节）已存入 EA 库", "本机")
        return {"name": name, "size": len(data)}

    @app.post("/api/strategy/deploy")
    def deploy(body: dict):
        s = body.get("strategy") or {}
        fn = str(s.get("fileName") or "").strip()
        if not fn.lower().endswith(".ex5"):
            raise HTTPException(400, "请填写 .ex5 文件名")
        try:
            magic = int(s.get("magic"))
        except (TypeError, ValueError):
            raise HTTPException(400, "魔术号需为整数")
        if s.get("timeframe") not in TIMEFRAMES:
            raise HTTPException(400, "周期不对")
        st = {"fileName": fn, "symbol": str(s.get("symbol") or "").strip(), "timeframe": s["timeframe"],
              "magic": magic, "preset": str(s.get("preset") or "").strip()}
        if not st["symbol"]:
            raise HTTPException(400, "请选择品种")
        return run_exclusive(fleet.deploy, ids_of(body), st)

    @app.post("/api/strategy/distribute")
    def distribute(body: dict):
        s = body.get("strategy") or {}
        fn = str(s.get("fileName") or "").strip()
        if not fn.lower().endswith(".ex5"):
            raise HTTPException(400, "请选择 .ex5 文件")
        if not fleet.mock and fn not in fleet.library():
            raise HTTPException(400, "EA 库里没有这个文件，请先上传")
        try:
            magic = int(s.get("magic"))
            step = int(body.get("magic_step") or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "魔术号需为整数")
        if not 0 <= magic <= 2 ** 63 - 1 or not 0 <= step <= 100000:
            raise HTTPException(400, "魔术号超出范围")
        if s.get("timeframe") not in TIMEFRAMES:
            raise HTTPException(400, "周期不对")
        sym = str(s.get("symbol") or "").strip()
        if not sym:
            raise HTTPException(400, "请选择品种")
        lib = set(fleet.library())
        preset = str(s.get("preset") or "").strip()
        gp = {str(k): str(v).strip() for k, v in (body.get("group_presets") or {}).items() if str(v).strip()}
        for x in [preset, *gp.values()]:
            if x and not fleet.mock and x not in lib:
                raise HTTPException(400, f"EA 库里没有参数文件 {x}")
        mp = str(body.get("magic_param") or "").strip()
        if mp and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", mp):
            raise HTTPException(400, "魔术号参数名只能是字母、数字、下划线（和 EA 输入参数名一致）")
        st = {"fileName": fn, "symbol": sym, "timeframe": s["timeframe"], "magic": magic, "preset": preset}
        if body.get("concurrency") not in (None, ""):
            try:
                fleet.store.settings["dist_concurrency"] = max(1, min(4, int(body["concurrency"])))
                fleet.store.save_settings()
            except (TypeError, ValueError):
                pass
        opts = {"magic_step": step, "group_presets": gp, "magic_param": mp, "replace": body.get("replace", True) is not False}
        return run_exclusive(fleet.distribute, ids_of(body), st, opts)

    @app.post("/api/strategy/stop")
    def stop(body: dict):
        return run_exclusive(fleet.stop_strategies, ids_of(body))

    @app.post("/api/strategy/strip")
    def strip(body: dict):
        return run_exclusive(fleet.strip_strategies, ids_of(body))

    @app.post("/api/strategy/{acc_id}/{sid}/{action}")
    def strategy_action(acc_id: str, sid: str, action: str):
        if action not in ("start", "stop", "remove"):
            raise HTTPException(400, "未知操作")
        return run_exclusive(fleet.strategy_action, acc_id, sid, action)

    # ---------- 收益 ----------
    @app.post("/api/history")
    def history(body: dict):
        ids = body.get("ids") or [a["id"] for a in fleet.store.accounts]
        return fleet.history([str(i) for i in ids])

    @app.get("/api/desk")
    def desk(since: int = 0, eq_since: int = 0, deals_v: int = -1, accounts: str = ""):
        """收益台：净值曲线采样、活动流、平仓记录缓存（只读）。accounts=ID1,ID2 只汇总这些账户（空 = 全部）。"""
        ids = [x.strip() for x in accounts.split(",") if x.strip()][:200]
        return fleet.desk(since, eq_since, deals_v, ids or None)

    # ---------- 日志 ----------
    @app.get("/api/logs")
    def logs():
        return list(fleet.logs)

    @app.get("/api/logs.csv")
    def logs_csv():
        return Response(fleet.logs_csv().encode("utf-8"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=fleet-log.csv"})

    @app.post("/api/logs/clear")
    def clear_logs():
        fleet.logs.clear()
        return {"ok": True}

    # ---------- 设置 ----------
    @app.post("/api/settings")
    def save_settings(body: dict):
        s = fleet.store.settings
        for k, lo, hi in (("max_lots", 0.01, 1000), ("max_total_lots", 0.01, 10000), ("poll_interval", 0.5, 30),
                          ("deviation", 0, 1000), ("alert_loss", 1, 100000000)):
            if k in body and body[k] not in (None, ""):
                try:
                    v = float(body[k])
                except ValueError:
                    raise HTTPException(400, f"{k} 需为数字")
                if not lo <= v <= hi:
                    raise HTTPException(400, f"{k} 需在 {lo}~{hi} 之间")
                s[k] = int(v) if k in ("deviation", "alert_loss") else v
        if "scale_groups" in body:
            g = body["scale_groups"]
            s["scale_groups"] = [x.strip() for x in (g.split(",") if isinstance(g, str) else g) if x.strip()]
        if isinstance(body.get("ini_encoding"), str) and body["ini_encoding"] in ("utf-16", "utf-8", "mbcs"):
            s["ini_encoding"] = body["ini_encoding"]
        if "default_symbol" in body:
            ds = str(body.get("default_symbol") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9._#&+\-]{1,32}", ds):
                raise HTTPException(400, "默认品种格式不对（例如 USDJPYc）")
            s["default_symbol"] = ds
        if "default_timeframe" in body:
            tf = str(body.get("default_timeframe") or "").strip().upper()
            if tf not in TIMEFRAMES:
                raise HTTPException(400, f"默认周期只能是 {'/'.join(TIMEFRAMES)}")
            s["default_timeframe"] = tf
        for k in ("close_terminal_on_disconnect", "close_terminals_on_exit", "auto_enable_algo", "allow_dll_import",
                  "single_chart", "auto_reattach", "quick_no_confirm"):
            if k in body:
                s[k] = bool(body[k])
        for k, lo, hi in (("login_concurrency", 1, 8), ("dist_concurrency", 1, 4)):
            if k in body and body[k] not in (None, ""):
                try:
                    v = int(body[k])
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{k} 需为整数")
                s[k] = max(lo, min(hi, v))
        if "update_repo" in body:
            r = str(body.get("update_repo") or "").strip().strip("/")
            r = re.sub(r"^https?://github\.com/", "", r)
            if r and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", r):
                raise HTTPException(400, "GitHub 仓库格式：用户名/仓库名，例如 efwe32/mt5-fleet")
            s["update_repo"] = r
        if "update_url" in body:
            u = str(body.get("update_url") or "").strip()
            if u and not re.fullmatch(r"https?://\S+", u):
                raise HTTPException(400, "自定义更新地址需要以 http:// 或 https:// 开头")
            s["update_url"] = u
        for k in ("update_mirror", "update_auto_check"):
            if k in body:
                s[k] = bool(body[k])
        if "template_dir" in body:
            t = str(body.get("template_dir") or "").strip().strip('"')
            t_abs = t if (not t or os.path.isabs(t)) else os.path.normpath(os.path.join(str(app_root()), t))
            if t and not os.path.isfile(os.path.join(t_abs, "terminal64.exe")):
                raise HTTPException(400, f"这个文件夹里没有 terminal64.exe：{t}")
            if t and T.is_under(os.path.join(t_abs, "terminal64.exe"), terminals_root()) and \
                    os.path.normcase(os.path.abspath(t_abs)) != os.path.normcase(str(terminals_root() / "base")):
                raise HTTPException(400, "模板不能用某个账户的终端副本，请填原始 MT5 目录或 terminals\\base")
            s["template_dir"] = t
            fleet.template_info(force=True)
        pw = body.get("remote_password")
        if isinstance(pw, str) and pw != "":
            if len(pw) < 6:
                raise HTTPException(400, "远程密码至少 6 位")
            fleet.remote.set_password(pw)
        if "remote_enabled" in body:
            on = bool(body.get("remote_enabled"))
            if on and not fleet.remote.has_password():
                raise HTTPException(400, "请先设置远程密码，再打开远程访问")
            s["remote_enabled"] = on
        fleet.store.first_launch = False
        fleet.store.save_settings()
        if s.get("remote_enabled") and fleet.remote.has_password():
            fleet.remote.open()
        else:
            fleet.remote.stop()
        return {k: v for k, v in s.items() if k != "remote_pass_hash"}

    # ---------- 工具 ----------
    @app.post("/api/tools/check_path")
    def check_path(body: dict):
        p = str(body.get("path") or "").strip() or default_terminal_path(str(body.get("login") or "账号"))
        err = T.check_managed_path(p, terminals_root())
        exists = os.path.isfile(p)
        running = exists and T.is_running(p)
        owned = bool(fleet.registry.get(p))
        return {"path": p, "error": err, "exists": exists, "portableData": exists and (Path(p).parent / "MQL5").exists(),
                "running": running, "owned": owned, "root": str(terminals_root())}

    @app.get("/api/tools/mt5_sources")
    def mt5_sources():
        """（只读）找电脑上已有的 MT5，用来导入服务器列表。"""
        return {"items": T.scan_mt5_installs(troot=terminals_root())}

    @app.post("/api/tools/servers_check")
    def servers_check(body: dict):
        return {"items": T.servers_dat_candidates(str(body.get("path") or "").strip().strip('"'))}

    @app.post("/api/tools/import_servers")
    def import_servers(body: dict):
        r = fleet.import_servers(str(body.get("path") or ""))
        if not r.get("ok"):
            raise HTTPException(400, r.get("message", "导入失败"))
        return r

    @app.post("/api/tools/fix_servers")
    def fix_servers(body: dict):
        r = fleet.fix_servers()
        if not r.get("ok"):
            raise HTTPException(400, r.get("message", "修复失败"))
        return r

    @app.post("/api/tools/template")
    def template(body: dict):
        return fleet.template_info(force=True)

    @app.post("/api/tools/clone")
    def clone(body: dict):
        src = str(body.get("src") or "").strip() or fleet.template_info()["path"] or str(terminals_root() / "base")
        ids = body.get("ids") or []
        root = terminals_root()
        results = []
        for i in ids:
            a = fleet.store.get(i)
            if not a:
                continue
            dst = root / a["login"]
            try:
                ok, msg = T.clone_terminal(src, str(dst), root)
            except Exception as e:
                ok, msg = False, f"复制失败：{e}"
            if ok:
                fleet.store.update(i, {"terminal_path": str(dst / "terminal64.exe")})
            fleet.log(i, "复制终端", ok, msg)
            results.append({"id": i, "ok": ok, "message": msg})
        return {"results": results}

    @app.post("/api/tools/close_owned")
    def close_owned():
        ids = [i for i in list(fleet.workers)]
        if ids:
            run_exclusive(fleet.disconnect, ids)
        msgs = fleet.close_owned_terminals()
        fleet.log("", "关闭终端", True, f"已关闭本程序启动的 {len(msgs)} 个终端（其它 MT5 未受影响）", "本机")
        return {"closed": len(msgs), "messages": msgs}

    # ---------- 在线更新 ----------
    @app.get("/ping")
    def ping():
        # 不需要令牌：只给出版本号和进程号（更新重启时网页用它判断新后台是否已经起来）
        return {"app": "mt5-fleet", "version": __version__, "pid": os.getpid()}

    def need_updater():
        if updater is None:
            raise HTTPException(400, "当前运行方式不支持在线更新")
        return updater

    @app.get("/api/update/status")
    def update_status():
        return need_updater().public_status()

    @app.get("/api/update/check")
    def update_check(force: int = 0):
        return need_updater().check(force=bool(force))

    @app.post("/api/update/install")
    def update_install():
        u = need_updater()
        try:
            st = u.start_install()
        except UpdateError as e:
            raise HTTPException(409, str(e))
        fleet.log("", "在线更新", True, "开始检查并安装新版本", "本机")
        return st

    @app.post("/api/update/rollback")
    def update_rollback(body: dict):
        u = need_updater()
        try:
            st = u.start_rollback(str(body.get("name") or ""))
        except UpdateError as e:
            raise HTTPException(409, str(e))
        fleet.log("", "在线更新", True, f"恢复备份 {body.get('name') or '最近一份'}", "本机")
        return st

    @app.post("/api/exit")
    def exit_app():
        fleet.log("", "退出", True, "从网页退出程序", "本机")
        if on_exit:
            threading.Timer(0.5, on_exit).start()
        return {"ok": True}

    return app
