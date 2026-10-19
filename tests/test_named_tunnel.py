"""v1.5.2：Cloudflare 命名隧道（固定链接令牌）。
python tests/test_named_tunnel.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fleet import __version__  # noqa: E402
from fleet.config import DEFAULT_SETTINGS, Store  # noqa: E402
from fleet.remote import RemoteGate  # noqa: E402
from fleet import secrets as sec  # noqa: E402

fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {info}" if info else ""))
    if not cond:
        fails.append(name)


check("版本 ≥1.5.2", tuple(int(x) for x in __version__.split(".")[:3]) >= (1, 5, 2), __version__)
check("默认有 remote_listen_port", DEFAULT_SETTINGS.get("remote_listen_port") == 18765)
check("默认令牌为空", DEFAULT_SETTINGS.get("remote_tunnel_token_enc") == "")

# 单元：令牌加解密 + public 形态
td = Path(tempfile.mkdtemp(prefix="fleet-nt-"))
try:
    (td / "settings.json").write_text("{}", encoding="utf-8")
    (td / "accounts.json").write_text('{"accounts":[]}', encoding="utf-8")
    store = Store(td)
    g = RemoteGate(store)
    g.port = 18765
    check("无令牌不是 named", not g.has_token() and g.public()["mode"] in ("off", "quick"))
    fake = "eyJhIjoiYWJjZGVmZ2hpamsnbG1ub3BxcnN0dXZ3eHl6MDEyMzQ1Njc4OSJ9"  # 足够长
    g.set_tunnel_token(fake)
    store.save_settings()
    check("令牌已加密保存", store.settings["remote_tunnel_token_enc"].startswith(("dpapi:", "plain:")))
    check("解密还原", g.token_plain() == fake)
    check("has_token", g.has_token())
    store.settings["remote_enabled"] = True
    store.settings["remote_pass_hash"] = "scrypt$00$00"  # 仅占位，enabled 还需 hash；public 不校验
    # enabled() 需要真 hash——用 set_password
    g.set_password("phone-pass-1")
    store.settings["remote_enabled"] = True
    g.online = True
    store.settings["remote_fixed_url"] = "https://phone.example.com"
    pub = g.public()
    check("named 模式", pub["mode"] == "named" and pub["tokenSaved"] is True, pub)
    check("显示固定 URL", pub["url"] == "https://phone.example.com" and pub["online"] is True, pub)
    store.settings["remote_fixed_url"] = ""
    pub2 = g.public()
    check("无固定 URL 时有提示", pub2["online"] and not pub2["url"] and "127.0.0.1:18765" in (pub2.get("hint") or ""), pub2)
    g.set_tunnel_token(None, clear=True)
    check("可清除令牌", not g.has_token())
finally:
    shutil.rmtree(td, ignore_errors=True)

# 静态文案
html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
aj = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
notes = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
check("设置页有令牌输入", "setRemoteTok" in html and "固定链接令牌" in html)
check("设置页有固定地址与端口", "setRemoteFixed" in html and "setRemotePort" in html and "18765" in html)
check("app.js 保存令牌字段", "remote_tunnel_token" in aj and "remote_tunnel_token_clear" in aj)
check("更新说明", "## v1.5.2" in notes and "命名隧道" in notes)

# 面板：保存令牌不回传原文；FLEET_REMOTE_NO_TUNNEL 下 named 标 online
port = 18790
data = Path("/tmp/fleet152-nt")
shutil.rmtree(data, ignore_errors=True)
env = {**os.environ, "FLEET_MOCK_AUTOTRADE": "0", "FLEET_REMOTE_NO_TUNNEL": "1"}
proc = subprocess.Popen(
    [sys.executable, "app.py", "--mock", "--no-browser", "--port", str(port), "--data-dir", str(data)],
    cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)
try:
    token = ""
    for _ in range(40):
        try:
            page = httpx.get(f"http://127.0.0.1:{port}/", timeout=2).text
            m = re.search(r'FLEET_TOKEN = "([^"]+)"', page)
            if m:
                token = m.group(1)
                break
        except Exception:
            time.sleep(0.3)
    check("本机页面打开", bool(token))
    c = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"X-Fleet-Token": token}, timeout=30)
    fake = "eyJ0b2tlbiI6Im5hbWVkLXR1bm5lbC10ZXN0LXRva2VuLTEyMzQ1Njc4OTAifQ"
    r = c.post("/api/settings", json={
        "remote_password": "phone-pass-9",
        "remote_enabled": True,
        "remote_tunnel_token": fake,
        "remote_fixed_url": "https://mt5-phone.example.com",
        "remote_listen_port": 18765,
    }).json()
    check("保存不回传令牌", "eyJ0b2tlbi" not in str(r) and r.get("remote_tunnel_token_set") is True, {k: r.get(k) for k in ("remote_tunnel_token_set", "remote_fixed_url", "remote_listen_port")})
    check("状态不含密文令牌", "remote_tunnel_token_enc" not in str(c.get("/api/state").json()))
    st = c.get("/api/state").json()
    rem = st["remote"]
    check("remote 为 named 且 online", rem.get("mode") == "named" and rem.get("tokenSaved") and rem.get("online"), rem)
    check("显示固定访问地址", rem.get("url") == "https://mt5-phone.example.com", rem)
    check("本机端口提示", rem.get("listenPort") in (18765, rem.get("port")), rem)
    # 远程口仍可用
    rp = rem["port"]
    rc = httpx.Client(base_url=f"http://127.0.0.1:{rp}", timeout=30)
    good = rc.post("/api/remote/login", json={"password": "phone-pass-9"})
    check("命名模式下远程密码仍可用", good.status_code == 200, good.status_code)
    # 清除令牌
    r2 = c.post("/api/settings", json={"remote_tunnel_token_clear": True, "remote_enabled": True}).json()
    check("清除后 token_set 为假", r2.get("remote_tunnel_token_set") is False)
    st2 = c.get("/api/state").json()["remote"]
    check("清除后不再是 named", st2.get("mode") != "named" or not st2.get("tokenSaved"), st2)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()

print("FAILS", len(fails))
sys.exit(1 if fails else 0)
