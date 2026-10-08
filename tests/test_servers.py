"""服务器列表：条目数读取、导入后不覆盖更全的列表、模板优先级（不需要 MT5）。
用法：python tests/test_servers.py"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fleet import terminal as T  # noqa: E402


def fake_dat(path: Path, n: int, body: int = 64):
    h = bytearray(0x1AC)
    h[0:4] = (503).to_bytes(4, "little")
    c = "Copyright 2000-2026, MetaQuotes Ltd.".encode("utf-16-le")
    h[4:4 + len(c)] = c
    sv = "Servers".encode("utf-16-le")
    h[0x84:0x84 + len(sv)] = sv
    h[0xAC:0xB0] = n.to_bytes(4, "little")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(h) + os.urandom(body))


def main():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        fake_dat(td / "a" / "Config" / "servers.dat", 191)
        fake_dat(td / "b" / "Config" / "servers.dat", 56)
        (td / "x.txt").write_text("hello")
        assert T.servers_dat_count(td / "a" / "Config" / "servers.dat") == 191
        assert T.servers_dat_count(td / "b" / "Config" / "servers.dat") == 56
        assert T.servers_dat_count(td / "x.txt") == -1
        assert T.servers_dat_count(td / "nope.dat") == -1
        rows = T.servers_dat_candidates(str(td / "a"))
        assert rows and rows[0]["count"] == 191, rows

        # 导入 56 个的列表 → 不覆盖已有 191 个的终端；导入 191 个 → 覆盖只有 56 个的终端
        lib = td / "lib"
        ok, msg, meta = T.import_servers_dat(str(td / "b" / "Config" / "servers.dat"), lib)
        assert ok and meta["count"] == 56 and "56 个服务器" in msg, msg
        term = td / "terminals" / "1001"
        fake_dat(term / "Config" / "servers.dat", 191)
        (term / "terminal64.exe").write_bytes(b"x")
        assert T.apply_servers_dat(str(term / "terminal64.exe"), lib / "servers.dat") == ""
        assert T.servers_dat_count(term / "Config" / "servers.dat") == 191
        ok, msg, meta = T.import_servers_dat(str(td / "a" / "Config" / "servers.dat"), lib)
        term2 = td / "terminals" / "1002"
        fake_dat(term2 / "Config" / "servers.dat", 56)
        (term2 / "terminal64.exe").write_bytes(b"x")
        assert T.apply_servers_dat(str(term2 / "terminal64.exe"), lib / "servers.dat")
        assert T.servers_dat_count(term2 / "Config" / "servers.dat") == 191
        assert (term2 / "Config" / "servers.dat.fleetbak").exists()

        # 模板优先级：MT5模板 优先于其它文件夹（即使其它文件夹更新）
        app = td / "app"
        for name in ("MetaTrader 5    4", "MT5模板", "Zeta"):
            (app / name).mkdir(parents=True)
            (app / name / "terminal64.exe").write_bytes(b"x")
        now = time.time()
        os.utime(app / "MT5模板" / "terminal64.exe", (now - 100, now - 100))
        os.utime(app / "MetaTrader 5    4" / "terminal64.exe", (now - 1000, now - 1000))
        os.utime(app / "Zeta" / "terminal64.exe", (now, now))
        got = [p.name for p in T.find_templates(app, app / "terminals")]
        assert got == ["MT5模板", "Zeta", "MetaTrader 5    4"], got
        assert T.exe_version(app / "Zeta" / "terminal64.exe") == ""

    # 随程序发布的列表：191 个服务器，明文里没有账号、密码
    root = Path(__file__).resolve().parent.parent
    raw = T.bundled_servers_bytes(root)
    assert T.servers_dat_count_bytes(raw) == 191, T.servers_dat_count_bytes(raw)
    for bad in (b"165404037", b"165468707", b"165404029", b"277986117", b"password", b"Password", "密码".encode()):
        assert bad not in raw, bad
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        ok, msg, meta = T.import_servers_bytes(raw, td / "lib", "随程序发布的服务器列表")
        assert ok and meta["count"] == 191 and "191 个服务器" in msg, msg
        # 发布的列表不覆盖更全的终端
        term = td / "t" / "9"
        fake_dat(term / "Config" / "servers.dat", 300)
        (term / "terminal64.exe").write_bytes(b"x")
        assert T.apply_servers_dat(str(term / "terminal64.exe"), td / "lib" / "servers.dat") == ""
        assert T.servers_dat_count(term / "Config" / "servers.dat") == 300
    print("test_servers OK")


if __name__ == "__main__":
    main()
