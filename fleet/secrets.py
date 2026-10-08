"""本地密码保护。

Windows 上优先使用 DPAPI（CryptProtectData，绑定当前 Windows 用户），
不依赖 pywin32，直接走 ctypes。非 Windows 或 DPAPI 失败时退回 base64
（仅做混淆，不是加密），并在界面上给出警告。
密码只保存在本机 accounts.json，不会发往任何地方。
"""
from __future__ import annotations

import base64
import sys

_ENTROPY = b"mt5-fleet-v1"


def _dpapi_available() -> bool:
    return sys.platform == "win32"


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32

    def _to_blob(data: bytes) -> _BLOB:
        buf = ctypes.create_string_buffer(data, len(data))
        blob = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob._buf = buf  # keep alive
        return blob

    def _from_blob(blob: _BLOB) -> bytes:
        out = ctypes.string_at(blob.pbData, blob.cbData)
        _kernel32.LocalFree(blob.pbData)
        return out

    def _protect(data: bytes) -> bytes:
        inp, ent, out = _to_blob(data), _to_blob(_ENTROPY), _BLOB()
        if not _crypt32.CryptProtectData(ctypes.byref(inp), "mt5fleet", ctypes.byref(ent),
                                         None, None, 0x01, ctypes.byref(out)):
            raise OSError("CryptProtectData 失败")
        return _from_blob(out)

    def _unprotect(data: bytes) -> bytes:
        inp, ent, out = _to_blob(data), _to_blob(_ENTROPY), _BLOB()
        if not _crypt32.CryptUnprotectData(ctypes.byref(inp), None, ctypes.byref(ent),
                                           None, None, 0x01, ctypes.byref(out)):
            raise OSError("CryptUnprotectData 失败（可能换了 Windows 用户或电脑）")
        return _from_blob(out)


def encrypt(password: str) -> str:
    if not password:
        return ""
    raw = password.encode("utf-8")
    if _dpapi_available():
        try:
            return "dpapi:" + base64.b64encode(_protect(raw)).decode("ascii")
        except Exception:
            pass
    return "plain:" + base64.b64encode(raw).decode("ascii")


def decrypt(token: str) -> str:
    if not token:
        return ""
    kind, _, payload = token.partition(":")
    data = base64.b64decode(payload.encode("ascii"))
    if kind == "dpapi":
        if not _dpapi_available():
            raise OSError("这个密码是用 Windows DPAPI 加密的，只能在原来那台 Windows 电脑上解开")
        return _unprotect(data).decode("utf-8")
    if kind == "plain":
        return data.decode("utf-8")
    # 兼容手工写入的明文
    return token


def protection_kind(token: str) -> str:
    if not token:
        return "none"
    return "dpapi" if token.startswith("dpapi:") else "plain"


def dpapi_supported() -> bool:
    if not _dpapi_available():
        return False
    try:
        return decrypt(encrypt("probe")) == "probe" and encrypt("probe").startswith("dpapi:")
    except Exception:
        return False
