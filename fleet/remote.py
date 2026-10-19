"""手机远程访问。

默认关闭。打开后在本机另开一个只认远程密码的端口，再用 Cloudflare 隧道接到外网：
- 未填令牌：临时加密隧道（trycloudflare.com，每次重启地址会变，不用注册）
- 已填「固定链接令牌」：命名隧道（cloudflared tunnel run --token），地址由你在
  Cloudflare 控制台绑定的主机名决定，程序更新/重启后地址不变

本机 127.0.0.1 的原来入口不经过这里，也不用远程密码。
密码只存成 scrypt 摘要；隧道令牌用本机加密保存，接口里只显示「已保存」，不写进日志。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

from . import secrets as sec

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
MAX_FAILS = 5
LOCK_SEC = 10 * 60
CF_VERSION = "2026.10.0"
CF_SHA = {
    "windows": "86aee4017b26625cee8484c113558f48effa4cd47f7aa05fcf425604e5d2b23c",
    "linux": "d33ff2d14475178d2012c2c56beba87389ac5ded27649519f198a7d3134a99db",
}
DEFAULT_NAMED_PORT = 18765


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + salt.hex() + "$" + dk.hex()


def check_password(password: str, stored: str) -> bool:
    try:
        kind, salt, dig = (stored or "").split("$", 2)
        if kind != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk.hex(), dig)
    except Exception:
        return False


class RemoteGate:
    def __init__(self, store):
        self.store = store
        self.port = 0
        self.url = ""          # 临时隧道解析到的 trycloudflare 地址
        self.online = False
        self.error = ""
        self.mode = "off"      # off | quick | named
        self.sessions: set[str] = set()
        self._fails: dict[str, list] = {}
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._stop = False
        self._starting = False

    def enabled(self) -> bool:
        return bool(self.store.settings.get("remote_enabled")) and bool(self.store.settings.get("remote_pass_hash"))

    def has_password(self) -> bool:
        return bool(self.store.settings.get("remote_pass_hash"))

    def has_token(self) -> bool:
        return bool(self.store.settings.get("remote_tunnel_token_enc"))

    def token_plain(self) -> str:
        enc = self.store.settings.get("remote_tunnel_token_enc") or ""
        if not enc:
            return ""
        try:
            return sec.decrypt(enc)
        except Exception:
            return ""

    def set_password(self, password: str):
        self.store.settings["remote_pass_hash"] = hash_password(password)
        with self._lock:
            self.sessions.clear()

    def set_tunnel_token(self, token: str | None, clear: bool = False):
        """保存或清除命名隧道令牌。token 为空且 clear=False 表示不改动。"""
        if clear:
            self.store.settings["remote_tunnel_token_enc"] = ""
            return
        if token is None:
            return
        token = str(token).strip()
        if not token:
            return
        self.store.settings["remote_tunnel_token_enc"] = sec.encrypt(token)

    def fixed_url(self) -> str:
        return str(self.store.settings.get("remote_fixed_url") or "").strip()

    def listen_port_setting(self) -> int:
        try:
            p = int(self.store.settings.get("remote_listen_port") or DEFAULT_NAMED_PORT)
        except (TypeError, ValueError):
            p = DEFAULT_NAMED_PORT
        return max(1024, min(65535, p))

    def public(self) -> dict:
        named = self.has_token()
        fixed = self.fixed_url()
        if named:
            display = fixed
            online = bool(self.online and self.enabled())
            if online and display:
                url = display
            elif online:
                url = ""
            else:
                url = ""
            hint = ""
            if online and not display:
                hint = (f"已用固定链接连接（本机端口 {self.port}）。"
                        f"请在 Cloudflare 控制台把「公共服务」指到 http://127.0.0.1:{self.port}，"
                        f"或在下方填写「固定访问地址」以便这里直接显示。")
            elif not online and self.error:
                hint = ""
            mode = "named"
        else:
            url = self.url if self.online else ""
            online = bool(self.online and self.url)
            hint = ""
            mode = "quick" if self.enabled() else "off"
        return {
            "enabled": self.enabled(),
            "hasPassword": self.has_password(),
            "online": online,
            "url": url,
            "error": self.error or "",
            "port": self.port,
            "mode": mode,
            "tokenSaved": named,
            "fixedUrl": fixed,
            "listenPort": self.port or self.listen_port_setting(),
            "hint": hint,
        }

    def valid_session(self, sid: str | None) -> bool:
        return bool(sid) and sid in self.sessions and self.enabled()

    def new_session(self) -> str:
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self.sessions.add(sid)
        return sid

    def locked(self, ip: str) -> bool:
        row = self._fails.get(ip)
        if not row:
            return False
        return time.time() < float(row[1] or 0)

    def lock_left(self, ip: str) -> int:
        row = self._fails.get(ip) or [0, 0]
        return max(0, int(float(row[1] or 0) - time.time()))

    def fail(self, ip: str) -> bool:
        """记一次错误。返回现在是否已锁定。"""
        with self._lock:
            now = time.time()
            count, until = self._fails.get(ip, [0, 0.0])
            if now < float(until or 0):
                return True
            if until and now >= float(until):
                count = 0
            count = int(count) + 1
            until = 0.0
            if count >= MAX_FAILS:
                until = now + LOCK_SEC
                count = 0
            self._fails[ip] = [count, until]
            return now < float(until or 0)

    def succeed(self, ip: str):
        self._fails.pop(ip, None)

    def open(self):
        """后台拉起隧道。没设密码或开关是关的，什么都不做。已经连着就不用再开一条。"""
        if not self.enabled() or not self.port:
            return
        if os.environ.get("FLEET_REMOTE_NO_TUNNEL") == "1":
            # 测试：不拉 cloudflared；命名模式仍标成“可连”（本机端口可用）
            if self.has_token():
                self.mode = "named"
                self.online = True
                self.url = ""
                self.error = ""
            return
        if self._starting or (self._proc and self._proc.poll() is None):
            return
        self._stop = False
        self._starting = True
        threading.Thread(target=self._run, name="remote-tunnel", daemon=True).start()

    def stop(self):
        self._stop = True
        self.online = False
        self.url = ""
        self.mode = "off"
        with self._lock:
            self.sessions.clear()
        proc = self._proc
        self._proc = None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _binary(self) -> str:
        name = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
        path = self.store.data_dir / "remote" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        kind = "windows" if sys.platform == "win32" else "linux"
        asset = "cloudflared-windows-amd64.exe" if kind == "windows" else "cloudflared-linux-amd64"
        url = f"https://github.com/cloudflare/cloudflared/releases/download/{CF_VERSION}/{asset}"
        expect = CF_SHA[kind]
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expect:
            return str(path)
        self.error = "正在下载远程通道…"
        tmp = path.with_suffix(path.suffix + ".part")
        req = urllib.request.Request(url, headers={"User-Agent": "mt5-fleet"})
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
        if digest != expect:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("远程通道程序校验失败，没有启用")
        os.replace(tmp, path)
        if sys.platform != "win32":
            os.chmod(path, 0o755)
        return str(path)

    def _run(self):
        try:
            binary = self._binary()
        except Exception as e:
            self.online = False
            self.url = ""
            self.error = "打不开远程访问：" + str(e)
            self._starting = False
            return
        if self._stop or not self.enabled():
            self._starting = False
            return
        token = self.token_plain() if self.has_token() else ""
        named = bool(token)
        if named:
            self.mode = "named"
            # 令牌只进命令行参数，不写日志、不进配置文件正文
            cmd = [binary, "tunnel", "run", "--token", token, "--no-autoupdate"]
        else:
            self.mode = "quick"
            cmd = [binary, "tunnel", "--url", f"http://127.0.0.1:{self.port}",
                   "--no-autoupdate", "--protocol", "http2"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        except Exception as e:
            self.error = "远程通道没有启动：" + str(e)
            self._starting = False
            return
        self._proc = proc
        if named:
            # 命名隧道：稍等几秒，进程还活着就视为已连接（主机名在 Cloudflare 控制台）
            for _ in range(8):
                if self._stop or proc.poll() is not None:
                    break
                time.sleep(0.5)
            if proc.poll() is None and not self._stop:
                self.online = True
                self.url = ""
                self.error = ""
                self._starting = False
                if proc.stdout:
                    threading.Thread(target=self._drain_named, args=(proc,), daemon=True).start()
                return
            if not self._stop:
                self.error = ("固定链接隧道没有连上。请检查令牌是否正确、Cloudflare 控制台里"
                              f"「公共服务」是否指向 http://127.0.0.1:{self.port}，以及这台电脑能否上网。")
                self._starting = False
                self.stop()
            return
        # 临时隧道：从日志里抓 trycloudflare 地址
        deadline = time.time() + 45
        buf = ""
        while time.time() < deadline and not self._stop and proc.poll() is None:
            line = b""
            try:
                line = proc.stdout.readline() if proc.stdout else b""
            except Exception:
                break
            if not line:
                time.sleep(0.2)
                continue
            buf += line.decode("utf-8", "replace")
            m = URL_RE.search(buf)
            if m:
                self.url = m.group(0)
                self.online = True
                self.error = ""
                self._starting = False
                break
        if not self.online and not self._stop:
            self.error = "远程通道没有连上。请确认这台电脑能打开外网后再打开一次。"
            self._starting = False
            self.stop()
            return
        if proc.stdout:
            threading.Thread(target=self._drain, args=(proc,), daemon=True).start()

    def _redact(self, text: str) -> str:
        """避免令牌偶然出现在缓冲里被当错误展示。"""
        tok = self.token_plain()
        if tok and tok in text:
            text = text.replace(tok, "***")
        return text

    def _drain_named(self, proc: subprocess.Popen):
        try:
            if proc.stdout:
                for _line in proc.stdout:
                    if self._stop:
                        break
        except Exception:
            pass
        if proc.poll() is not None and not self._stop:
            self.online = False
            self.url = ""
            if self.enabled():
                self.error = "固定链接隧道已断开。把开关关掉再打开可以重连。"

    def _drain(self, proc: subprocess.Popen):
        try:
            if proc.stdout:
                for line in proc.stdout:
                    if self._stop:
                        break
                    text = self._redact(line.decode("utf-8", "replace"))
                    m = URL_RE.search(text)
                    if m and not self.url:
                        self.url = m.group(0)
                        self.online = True
        except Exception:
            pass
        if proc.poll() is not None and not self._stop:
            self.online = False
            self.url = ""
            if self.enabled():
                self.error = "远程通道已断开。把开关关掉再打开可以重连。"
