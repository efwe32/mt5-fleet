"""在线更新端到端测试（Linux：假的 terminal64.exe + 假的 MetaTrader5 包，走“实盘”代码路径）。
用法：python tests/test_updater.py /tmp/fake/terminal64.exe [端口]

模拟一台已经装好 v当前版本 的电脑（临时文件夹，带 data\\、terminals\\、python\\、.venv\\ 等），
本地 HTTP 服务器假装 GitHub（releases/latest + 资源下载），依次验证：
  1. 检查更新（GitHub 接口 + sha256）→ 立即更新 → 后台自动重启成新版本，MT5 终端进程号不变、自动接管、能下单；
     只替换程序文件，data / terminals / python / .venv 不动，有备份，删除了新版本去掉的文件。
  2. 新版本自检通过但启动失败 → 重启助手自动恢复上一版本并重新启动。
  3. 新版本自检失败（导入出错）→ 当场恢复，后台不重启。
  4. 压缩包被篡改（sha256 不一致）→ 拒绝，什么都不改。
  5. GitHub 不可用时用自定义更新地址（latest.json）。
  6. 手动「恢复上一个版本」。
  7. 清单里有 data/… 之类的路径 → 拒绝。
"""
import hashlib
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import psutil

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
sys.path.insert(0, str(SRC))
from fleet import __version__ as CUR  # noqa: E402
from fleet.updater import _check_manifest, current_app_files, is_app_path  # noqa: E402

fake = Path(sys.argv[1])
port = int(sys.argv[2]) if len(sys.argv) > 2 else 8930
tmp = Path(tempfile.mkdtemp(prefix="fleet-upd-"))
inst = tmp / "install"            # 模拟用户电脑上的程序目录
data = inst / "data"
troot = inst / "terminals"
www = tmp / "www"                 # 假 GitHub
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {str(info)[:300]}" if info not in ("", None) else ""), flush=True)
    if not cond:
        fails.append(name)


def vbump(v, n):
    a, b, c = map(int, v.split("."))
    return f"{a}.{b}.{c + n}"


V1, V2, V3, V4 = (vbump(CUR, i) for i in (1, 2, 3, 4))


# ---------------- 假 GitHub ----------------
class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(www), **kw)

    def log_message(self, *a):
        pass


class TS(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


www.mkdir(parents=True)
httpd = TS(("127.0.0.1", 0), H)
WPORT = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
WBASE = f"http://127.0.0.1:{WPORT}"
api_latest = www / "repos" / "efwe32" / "mt5-fleet" / "releases" / "latest"
api_latest.parent.mkdir(parents=True)


def copy_app(dst: Path):
    for rel in current_app_files(SRC):
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SRC / rel, dst / rel)


def publish(ver: str, mutate=None, as_latest=True) -> dict:
    src = tmp / f"src-{ver}"
    copy_app(src)
    (src / "fleet" / "__init__.py").write_text(f'"""MT5 Fleet"""\n__version__ = "{ver}"\n', encoding="utf-8")
    notes = (src / "RELEASE_NOTES.md").read_text(encoding="utf-8") + f"\n## v{ver}\n- 测试版本 {ver}\n"
    (src / "RELEASE_NOTES.md").write_text(notes, encoding="utf-8")
    if mutate:
        mutate(src)
    out = www / "download" / f"v{ver}"
    r = subprocess.run([sys.executable, str(src / "tools" / "make_release.py"), "--out", str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    m = json.loads((out / "mt5-fleet-manifest.json").read_text(encoding="utf-8"))
    assets = []
    for name in (m["asset"], "mt5-fleet-manifest.json", "latest.json"):
        b = (out / name).read_bytes()
        assets.append({"name": name, "browser_download_url": f"{WBASE}/download/v{ver}/{name}",
                       "digest": "sha256:" + hashlib.sha256(b).hexdigest(), "size": len(b)})
    if as_latest:
        api_latest.write_text(json.dumps({"tag_name": f"v{ver}", "html_url": f"{WBASE}/release/v{ver}",
                                          "published_at": "2026-10-08T04:00:00Z", "assets": assets}), encoding="utf-8")
    return m


def h(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def procs_of(exe: Path):
    out = []
    for p in psutil.process_iter(["exe"]):
        try:
            if p.info["exe"] and os.path.samefile(p.info["exe"], exe) and p.status() != psutil.STATUS_ZOMBIE:
                out.append(p)
        except Exception:
            pass
    return out


def term_pids():
    return {lg: sorted(p.pid for p in procs_of(troot / lg / "terminal64.exe")) for lg in ("70001",)}


def ping():
    try:
        return httpx.get(f"http://127.0.0.1:{port}/ping", timeout=3).json()
    except Exception:
        return None


def client():
    for _ in range(120):
        try:
            html = httpx.get(f"http://127.0.0.1:{port}/", timeout=3).text
            t = re.search(r'FLEET_TOKEN = "([^"]+)"', html).group(1)
            return httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"X-Fleet-Token": t}, timeout=300)
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("程序没有启动")


def wait_status(c, states, sec=120):
    end = time.time() + sec
    st = {}
    while time.time() < end:
        try:
            st = c.get("/api/update/status").json()
            if st["state"] in states:
                return st
        except Exception:
            pass
        time.sleep(0.4)
    return st


def wait_new_backend(old_pid, version, sec=150):
    end = time.time() + sec
    while time.time() < end:
        p = ping()
        if p and p["pid"] != old_pid and p["version"] == version:
            return p
        time.sleep(0.5)
    return ping()


def wait_online(c, sec=60):
    end = time.time() + sec
    a = {}
    while time.time() < end:
        a = {x["login"]: x for x in c.get("/api/state").json()["accounts"]}
        if a.get("70001", {}).get("link") == "online":
            return a
        time.sleep(1)
    return a


def app_hashes():
    return {rel: h(inst / rel) for rel in current_app_files(inst)}


def protected_snapshot():
    snap = {}
    for base in (troot, inst / "python", inst / ".venv", inst / "MT5模板"):
        for f in sorted(base.rglob("*")):
            if f.is_file():
                snap[str(f.relative_to(inst))] = (h(f), f.stat().st_mtime_ns)
    for rel in ("ea_library/My.ex5", "sentinel.txt", "servers/servers.dat"):
        f = data / rel
        snap["data/" + rel] = (h(f), f.stat().st_mtime_ns)
    return snap


def accounts_core():
    j = json.loads((data / "accounts.json").read_text(encoding="utf-8"))
    return [(a["id"], a["login"], a["server"], a["password"], a["alias"]) for a in j["accounts"]]


# ---------------- 准备“用户电脑” ----------------
copy_app(inst)
tpl = inst / "MT5模板"
(tpl / "Config").mkdir(parents=True)
(tpl / "MQL5" / "Experts").mkdir(parents=True)
shutil.copy2(fake, tpl / "terminal64.exe")
(tpl / "Config" / "servers.dat").write_bytes(b"TEMPLATE-SERVERS" * 8)
(inst / "python").mkdir()
(inst / "python" / "说明.txt").write_text("便携版运行环境（测试占位，没有 python.exe）", encoding="utf-8")
(inst / ".venv").mkdir()
(inst / ".venv" / "pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
(data / "ea_library").mkdir(parents=True)
(data / "ea_library" / "My.ex5").write_bytes(b"EX5-BINARY" * 100)
(data / "servers").mkdir()
(data / "servers" / "servers.dat").write_bytes(b"SRV" * 50)
(data / "sentinel.txt").write_text("不要动我", encoding="utf-8")
(data / "settings.json").write_text(json.dumps({
    "template_dir": str(tpl), "login_retry_delay": 1, "verify_seconds": 6, "update_wait": 30,
    "update_repo": "efwe32/mt5-fleet", "update_url": "", "update_mirror": True}), encoding="utf-8")
troot.mkdir()
(troot / "说明.txt").write_text("受管终端", encoding="utf-8")
env = {**os.environ, "FLEET_TERMINALS_ROOT": str(troot), "PYTHONPATH": str(HERE / "fake_mt5"),
       "FLEET_MOCK_AUTOTRADE": "0", "PYTHONUNBUFFERED": "1", "FLEET_GITHUB_API": WBASE, "FLEET_UPDATE_TIMEOUT": "60"}

# 安全：路径白名单
check("白名单：程序文件允许", all(is_app_path(x) for x in ("app.py", "fleet/server.py", "static/app.js", "tools/x.py", "一键启动.vbs", "停止后台.bat", "README.md")))
check("白名单：数据 / 终端 / Python / 越界路径拒绝", not any(is_app_path(x) for x in (
    "data/accounts.json", "data/settings.json", "terminals/1/terminal64.exe", "python/python.exe", ".venv/x", "MT5模板/terminal64.exe",
    "../app.py", "fleet/../data/x", "/etc/passwd", "C:/x.py", "fleet\\x.py", "tests/a.py", "x.exe", "fleet/__pycache__/a.pyc")))
try:
    _check_manifest({"name": "mt5-fleet", "version": "9.9.9", "sha256": "0" * 64,
                     "files": {"app.py": "0", "fleet/__init__.py": "0", "data/accounts.json": "0"}})
    check("清单里带 data/ 路径被拒绝", False)
except ValueError as e:
    check("清单里带 data/ 路径被拒绝", "不允许" in str(e), e)

srv = subprocess.Popen([sys.executable, str(inst / "app.py"), "--no-browser", "--hidden", "--port", str(port)],
                       cwd=str(inst), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    c = client()
    p0 = ping()
    check(f"旧版本 v{CUR} 已启动", p0 and p0["version"] == CUR, p0)
    r = c.post("/api/accounts/quick", json={"text": "70001,pw1,Real-Server,主仓,甲\n"}).json()
    check("添加并登录（假终端）", r["ok"] == 1, r)
    pids0 = term_pids()
    check("终端在运行", len(pids0["70001"]) == 1, pids0)
    st = c.get("/api/update/status").json()
    check("状态：识别安装方式", st["current"] == CUR and st["layout"], st.get("layout"))

    # ---------- 没有发布时 ----------
    r = c.get("/api/update/check?force=1").json()
    check("还没有发布：提示失败但不崩", r["ok"] is False and r["errors"], r["errors"])

    # ---------- 1. 正常更新 ----------
    def mut1(d: Path):
        (d / "static" / "app.css").write_text((d / "static" / "app.css").read_text(encoding="utf-8") + f"\n/* {V1} */\n", encoding="utf-8")
        (d / "fleet" / "extra_mod.py").write_text("X = 1\n", encoding="utf-8")
        (d / "tools" / "setup_terminals.py").unlink()
    m1 = publish(V1, mut1)
    r = c.get("/api/update/check?force=1").json()
    check("检查更新：发现新版本", r["ok"] and r["newer"] and r["latest"] == V1 and r["source"].startswith("GitHub"), r)
    check("检查更新：带更新说明", f"测试版本 {V1}" in r.get("notes", ""), r.get("notes"))
    check("检查更新：依赖没变化", r.get("requirements_changed") is False, r.get("requirements_changed"))
    prot0 = protected_snapshot()
    acc0 = accounts_core()
    old_pid = p0["pid"]
    r = c.post("/api/update/install")
    check("开始安装", r.status_code == 200, r.text)
    st = wait_status(c, ("restarting", "error", "uptodate"))
    check("安装到“重启中”", st.get("state") == "restarting", st.get("log"))
    p1 = wait_new_backend(old_pid, V1)
    check(f"后台自动重启为 v{V1}", p1 and p1["version"] == V1 and p1["pid"] != old_pid, p1)
    check("旧后台进程已退出", not psutil.pid_exists(old_pid) or psutil.Process(old_pid).status() == psutil.STATUS_ZOMBIE)
    check("MT5 终端没有被关掉（进程号不变）", term_pids() == pids0, (term_pids(), pids0))
    c = client()
    accs = wait_online(c)
    check("新版本自动接管终端并在线", accs.get("70001", {}).get("link") == "online", accs.get("70001", {}).get("linkError"))
    r = c.post("/api/trade/order", json={"ids": [accs["70001"]["id"]], "order": {"symbol": "EURUSD", "kind": "buy", "lots": 0.01, "magic": 7}}).json()
    check("更新后下单正常", r.get("ok") == 1, r)
    check("程序文件已换成新版本", (inst / "fleet" / "extra_mod.py").exists() and f"/* {V1} */" in (inst / "static" / "app.css").read_text(encoding="utf-8"))
    check("新版本去掉的文件已删除", not (inst / "tools" / "setup_terminals.py").exists())
    check("每个程序文件和发布清单一致", all(h(inst / k) == v for k, v in m1["files"].items()))
    check("data / terminals / python / .venv / MT5模板 没有被改动", protected_snapshot() == prot0)
    check("账户台账没变（账号、密码密文）", accounts_core() == acc0)
    bks = sorted((data / "backups").iterdir())
    check("已备份旧版本", len(bks) == 1 and bks[0].name.startswith(CUR + "-") and (bks[0] / "tools" / "setup_terminals.py").exists(), [b.name for b in bks])
    check("备份里没有数据文件", not any("data" == p.relative_to(bks[0]).parts[0] for p in bks[0].rglob("*")))
    lu = json.loads((data / "updates" / "last_update.json").read_text(encoding="utf-8"))
    check("更新记录：成功", lu["status"] == "ok" and lu["from"] == CUR and lu["to"] == V1, lu)
    hl = (data / "logs" / "update-helper.log").read_text(encoding="utf-8")
    check("重启助手日志", "运行正常" in hl, hl[-300:])
    st = c.get("/api/update/status").json()
    check("网页能拿到更新结果和备份列表", st["last_update"]["status"] == "ok" and st["backups"], st["last_update"])
    r = c.get("/api/update/check?force=1").json()
    check("再检查：已经是最新", r["ok"] and not r["newer"], r)
    r = c.post("/api/update/install")
    st = wait_status(c, ("uptodate", "error", "restarting"), 30)
    check("已是最新时点更新：不重启", st.get("state") == "uptodate" and ping()["pid"] == p1["pid"], st.get("message"))
    files_v1 = app_hashes()

    # ---------- 2. 新版本启动失败 → 自动恢复 ----------
    def mut2(d: Path):
        s = (d / "app.py").read_text(encoding="utf-8")
        s = s.replace("def _run(args, data_dir: Path, hidden: bool, log_path):\n",
                      "def _run(args, data_dir: Path, hidden: bool, log_path):\n    if not args.stop and not args.check_update:\n        raise RuntimeError('测试：启动失败')\n", 1)
        (d / "app.py").write_text(s, encoding="utf-8")
    publish(V2, mut2)
    r = c.get("/api/update/check?force=1").json()
    check(f"发现 v{V2}", r.get("latest") == V2 and r["newer"], r)
    pid_b = ping()["pid"]
    c.post("/api/update/install")
    st = wait_status(c, ("restarting", "error"))
    check(f"v{V2} 自检通过、开始重启", st.get("state") == "restarting", st.get("log"))
    t0 = time.time()
    p2 = wait_new_backend(pid_b, V1, 120)
    check(f"新版本起不来 → 自动恢复 v{V1} 并重新启动（{time.time() - t0:.0f} 秒）", p2 and p2["version"] == V1 and p2["pid"] != pid_b, p2)
    check("恢复后程序文件和恢复前一样", app_hashes() == files_v1)
    lu = json.loads((data / "updates" / "last_update.json").read_text(encoding="utf-8"))
    check("更新记录：已自动恢复", lu["status"] == "rolled_back" and V2 in lu.get("error", ""), lu)
    check("终端仍然没被关（进程号不变）", term_pids() == pids0, term_pids())
    c = client()
    accs = wait_online(c)
    check("恢复后重新接管终端并在线", accs.get("70001", {}).get("link") == "online")
    check("数据仍然没被改动", protected_snapshot() == prot0 and accounts_core() == acc0)

    # ---------- 3. 新版本自检失败 → 当场恢复，不重启 ----------
    def mut3(d: Path):
        p = d / "fleet" / "server.py"
        p.write_text(p.read_text(encoding="utf-8").replace("from __future__ import annotations\n", "from __future__ import annotations\nimport fleet_module_that_does_not_exist  # noqa\n", 1), encoding="utf-8")
    publish(V3, mut3)
    c.get("/api/update/check?force=1")
    pid_c = ping()["pid"]
    c.post("/api/update/install")
    st = wait_status(c, ("error", "restarting"))
    check("自检失败 → 报错并恢复", st.get("state") == "error" and "自检失败" in st.get("message", ""), st.get("message"))
    check("自检失败时后台没有重启", ping()["pid"] == pid_c)
    check("自检失败后程序文件已恢复", app_hashes() == files_v1)

    # ---------- 4. 压缩包被篡改 ----------
    m4 = publish(V4)
    z = www / "download" / f"v{V4}" / m4["asset"]
    good = z.read_bytes()
    z.write_bytes(good[:-10] + b"0123456789")
    c.get("/api/update/check?force=1")
    c.post("/api/update/install")
    st = wait_status(c, ("error", "restarting"))
    check("篡改的压缩包被拒绝（sha256）", st.get("state") == "error" and "sha256" in st.get("message", ""), st.get("message"))
    check("篡改时什么都没改", app_hashes() == files_v1 and ping()["pid"] == pid_c)
    z.write_bytes(good)

    # ---------- 5. 自定义更新地址 ----------
    c.post("/api/settings", json={"update_repo": "nobody/nothing", "update_url": f"{WBASE}/download/v{V4}/latest.json"})
    r = c.get("/api/update/check?force=1").json()
    check("GitHub 不可用 → 自定义地址找到新版本", r["ok"] and r["latest"] == V4 and r["source"] == "自定义地址" and any("GitHub" in e for e in r["errors"]), r)
    r = c.post("/api/settings", json={"update_url": "ftp://x"})
    check("自定义地址格式检查", r.status_code == 400)
    r = c.post("/api/settings", json={"update_repo": "bad repo"})
    check("仓库格式检查", r.status_code == 400)
    c.post("/api/settings", json={"update_repo": "efwe32/mt5-fleet", "update_url": ""})

    # ---------- 6. 手动恢复上一个版本 ----------
    st = c.get("/api/update/status").json()
    b0 = next(b for b in st["backups"] if b["version"] == CUR)
    pid_d = ping()["pid"]
    r = c.post("/api/update/rollback", json={"name": b0["name"]})
    check("开始手动恢复", r.status_code == 200, r.text)
    p6 = wait_new_backend(pid_d, CUR, 120)
    check(f"手动恢复到 v{CUR} 并重启", p6 and p6["version"] == CUR, p6)
    check("恢复后 setup_terminals.py 回来了、extra_mod.py 没了", (inst / "tools" / "setup_terminals.py").exists() and not (inst / "fleet" / "extra_mod.py").exists())
    check("手动恢复后终端仍在（进程号不变）", term_pids() == pids0)
    check("手动恢复后数据没被改动", protected_snapshot() == prot0 and accounts_core() == acc0)

    # ---------- 命令行检查更新（只读） ----------
    settings_before = (data / "settings.json").read_bytes()
    r = subprocess.run([sys.executable, str(inst / "app.py"), "--check-update"], cwd=str(inst), env=env, capture_output=True, text=True, timeout=60)
    check("命令行 --check-update 输出新版本", r.returncode == 0 and f'"latest": "{V4}"' in r.stdout, r.stdout[-300:] + r.stderr[-300:])
    check("命令行检查不写 settings.json", (data / "settings.json").read_bytes() == settings_before)
finally:
    lk = {}
    try:
        lk = json.loads((data / "app.lock").read_text(encoding="utf-8"))
    except Exception:
        pass
    subprocess.run([sys.executable, str(inst / "app.py"), "--stop"], cwd=str(inst), env=env, capture_output=True, timeout=180)
    for p in psutil.process_iter(["exe", "cmdline"]):
        try:
            cl = " ".join(p.info.get("cmdline") or [])
            if (p.info["exe"] and str(tmp) in p.info["exe"]) or str(inst) in cl:
                p.kill()
        except Exception:
            pass
    if srv.poll() is None:
        srv.kill()
    if fails:
        for f in sorted((data / "logs").glob("*.log")):
            print(f"---- {f.name}\n" + f.read_text(encoding="utf-8", errors="ignore")[-2500:])
    httpd.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
sys.exit(1 if fails else 0)
