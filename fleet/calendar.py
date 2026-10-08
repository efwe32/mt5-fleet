"""金十财经日历。

页面 https://rili.jin10.com/ 的日数据走快讯长连接（wss://wss-flash-2.jin10.com/），
请求码 2006、类型 0（宏观数据）、日期 YYYY-MM-DD。访客登录能连上，但对方现在对未登录用户返回 401
（页面上也是「请登录后查看」）。旧的 cdn-rili.jin10.com/web_data/.../economics.json 域名已经不存在。
这里仍按页面用的协议去取；取到事件就只留 4 星及以上。取不到就给出原因，不编造数据。
"""
from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import threading
import time
import zlib
from datetime import datetime, timedelta

MIN_STAR = 4
HOST = "wss-flash-2.jin10.com"
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "events": [], "error": "", "source": ""}


def filter_stars(rows, minimum: int = MIN_STAR) -> list[dict]:
    """只留星级 >= minimum，并整理成面板用的字段。没有时间的条目（指标名录）丢掉。"""
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        try:
            star = int(r.get("star") or 0)
        except (TypeError, ValueError):
            continue
        if star < minimum:
            continue
        when = str(r.get("pub_time") or r.get("publish_time") or r.get("time") or r.get("date") or "").strip()
        if len(when) < 8:  # 没有公布时间就不是一条日历
            continue
        out.append({
            "id": r.get("id") or r.get("data_id") or "",
            "time": when,
            "country": str(r.get("country") or r.get("country_name") or r.get("currency") or ""),
            "title": str(r.get("name") or r.get("title") or r.get("indicator_name") or ""),
            "previous": _txt(r.get("previous")),
            "forecast": _txt(r.get("consensus") if r.get("consensus") is not None else r.get("forecast")),
            "actual": _txt(r.get("actual")),
            "star": star,
            "unit": str(r.get("unit") or ""),
        })
    out.sort(key=lambda x: x["time"])
    return out


def _txt(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s in ("None", "null") else s


def upcoming(rows: list[dict], now: datetime | None = None, days: int = 3) -> list[dict]:
    """今天和之后 days 天内（含今天已经公布的）。"""
    now = now or datetime.now()
    start = now.strftime("%Y-%m-%d")
    end = (now + timedelta(days=days)).strftime("%Y-%m-%d 23:59")
    return [r for r in rows if start <= r["time"][:10] and r["time"][:16] <= end]


def _xor(data: bytes, key: str) -> bytes:
    a = ord(key[0])
    out = bytearray(data)
    for i in range(len(out)):
        out[i] ^= ord(key[(i + a) % len(key)])
    return bytes(out)


class _W:
    def __init__(self):
        self.b = bytearray()

    def w16(self, v):
        self.b += struct.pack("<h", int(v))

    def w32(self, v):
        self.b += struct.pack("<i", int(v))

    def wu32(self, v):
        self.b += struct.pack("<I", int(v) & 0xFFFFFFFF)

    def wstr(self, s: str):
        raw = s.encode("utf-8")
        self.b += struct.pack("<H", len(raw)) + raw


class _R:
    def __init__(self, data: bytes):
        self.d = data
        self.p = 0

    def ru32(self):
        v = struct.unpack_from("<I", self.d, self.p)[0]
        self.p += 4
        return v

    def r16(self):
        v = struct.unpack_from("<h", self.d, self.p)[0]
        self.p += 2
        return v

    def rstr(self):
        n = struct.unpack_from("<H", self.d, self.p)[0]
        self.p += 2
        s = self.d[self.p:self.p + n].decode("utf-8", "replace")
        self.p += n
        return s


def _readn(ss, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        c = ss.recv(n - len(buf))
        if not c:
            raise EOFError("连接已关闭")
        buf += c
    return buf


def _connect():
    ctx = ssl.create_default_context()
    raw = socket.create_connection((HOST, 443), 12)
    ss = ctx.wrap_socket(raw, server_hostname=HOST)
    key = base64.b64encode(os.urandom(16)).decode()
    ss.sendall((
        f"GET / HTTP/1.1\r\nHost: {HOST}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
        "Origin: https://rili.jin10.com\r\nUser-Agent: Mozilla/5.0\r\n\r\n"
    ).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = ss.recv(4096)
        if not chunk:
            raise EOFError("握手失败")
        buf += chunk
    if b"101" not in buf.split(b"\r\n", 1)[0]:
        raise RuntimeError("金十长连接握手失败")
    return ss


def _send(ss, data: bytes, opcode: int = 2):
    mask = os.urandom(4)
    ln = len(data)
    hdr = bytearray([0x80 | opcode])
    if ln < 126:
        hdr.append(0x80 | ln)
    elif ln < 65536:
        hdr.append(0x80 | 126)
        hdr += struct.pack(">H", ln)
    else:
        hdr.append(0x80 | 127)
        hdr += struct.pack(">Q", ln)
    ss.sendall(bytes(hdr) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def _recv(ss):
    b1, b2 = _readn(ss, 2)
    opcode = b1 & 0x0F
    ln = b2 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", _readn(ss, 2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", _readn(ss, 8))[0]
    return opcode, (_readn(ss, ln) if ln else b"")


def _decode_blob(data: str):
    if not data:
        return []
    blob = base64.b64decode(data)
    try:
        txt = zlib.decompress(blob)
    except zlib.error:
        txt = zlib.decompress(blob, -15)
    arr = json.loads(txt)
    return arr if isinstance(arr, list) else []


def fetch_day(date: str, timeout: float = 12) -> tuple[list, str]:
    """取某一天的宏观数据。返回 (原始列表, 错误说明)。"""
    ss = _connect()
    ss.settimeout(timeout)
    try:
        _op, data = _recv(ss)
        r = _R(data)
        r.ru32()
        i = r.ru32()
        s = r.ru32()
        key = f"{s}.{i}"
        login = _W()
        login.w16(4002)
        login.w32(0)
        login.wstr("")
        login.wstr("chrome")
        login.w32(1)
        login.wstr("web")
        _send(ss, _xor(bytes(login.b), key))
        req = _W()
        req.w16(2006)
        req.wu32(0)  # 宏观数据
        req.wstr(date)
        req.wu32(7)
        _send(ss, _xor(bytes(req.b), key))
        for _ in range(16):
            op, data = _recv(ss)
            if op == 9:
                _send(ss, data, 10)
                continue
            if op != 2:
                continue
            rr = _R(_xor(data, key))
            code = rr.r16()
            if code == 1201:
                _send(ss, b"", 1)
                continue
            if code != 2006:
                continue
            rr.ru32()
            rr.rstr()
            rr.ru32()
            body = json.loads(rr.rstr() or "{}")
            status = int(body.get("status") or 0)
            if status == 401:
                return [], "金十财经日历现在需要登录后才能查看（公开接口返回未授权）。页面：https://rili.jin10.com/"
            if status not in (200, 101):
                return [], f"金十返回状态 {status} {body.get('message') or ''}".strip()
            return _decode_blob(body.get("data") or ""), ""
        return [], "金十没有返回这一天的日历"
    finally:
        try:
            ss.close()
        except Exception:
            pass


def load(force: bool = False, days: int = 3) -> dict:
    """今天起 days 天、4 星及以上。结果缓存 10 分钟。"""
    with _lock:
        if not force and _cache["at"] and time.time() - _cache["at"] < 600 and (_cache["events"] or _cache["error"]):
            return dict(_cache)
    events: list[dict] = []
    error = ""
    now = datetime.now()
    try:
        for i in range(days + 1):
            day = (now + timedelta(days=i)).strftime("%Y-%m-%d")
            rows, err = fetch_day(day)
            if err and not rows:
                error = err
                if "未授权" in err or "401" in err:
                    break
                continue
            events.extend(filter_stars(rows))
        events = upcoming(events, now, days)
    except Exception as e:
        error = f"读取金十日历失败：{e}"
    with _lock:
        # 失败时保留上一份成功的数据
        if events or not _cache["events"]:
            _cache["events"] = events
        if error and _cache["events"]:
            error = ""  # 还有旧数据就先显示旧的
        _cache["error"] = error
        _cache["source"] = "jin10"
        _cache["at"] = time.time()
        return dict(_cache)
