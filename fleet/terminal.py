"""MT5 终端进程控制与 EA 部署（Windows）。

隔离原则（不影响桌面上已经在用的 MT5）：
  1. 只使用程序目录下 terminals\\<账号>\\terminal64.exe 这些「受管终端」，别的路径一律拒绝。
  2. 受管终端总是用 /portable 启动：数据、配置、图表、EA 都留在它自己的文件夹里，
     不读写 %APPDATA%\\MetaQuotes，也不碰桌面那份 MT5 的设置。
  3. 只关闭/重启本程序自己启动并登记过 PID 的进程（见 procs.py），
     绝不按进程名去结束其它 terminal64.exe。

MetaTrader5 Python 接口不能把 EA 挂到图表上，所以部署 EA 用 MT5 官方启动配置：
  terminal64.exe /portable /config:fleet_start.ini
  [StartUp] Expert=Fleet\\xxx  Symbol=XAUUSD  Period=H1  ExpertParameters=xxx.set
停止 EA：正常关闭（我们自己启动的）终端，让它保存图表，再从
MQL5\\Profiles\\Charts\\*\\*.chr 里删掉对应 <expert> 段落，然后重新启动。
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None

from .procs import norm

FLEET_SUBDIR = "Fleet"


# ---------------- 路径隔离 ----------------
def is_under(path: str, root: Path) -> bool:
    try:
        p = Path(os.path.abspath(path))
        r = Path(os.path.abspath(str(root)))
        return os.path.normcase(str(p)).startswith(os.path.normcase(str(r)) + os.sep)
    except Exception:
        return False


def check_managed_path(path: str, root: Path) -> str | None:
    """返回错误信息；None 表示路径合格。"""
    if not path:
        return "没有设置终端路径"
    if Path(path).name.lower() != "terminal64.exe":
        return "终端路径必须指向 terminal64.exe"
    if not is_under(path, root):
        return f"终端路径必须在本程序的终端目录里：{root}{os.sep}<账号>{os.sep}terminal64.exe（为了不影响桌面上正在用的 MT5）"
    if os.path.normcase(os.path.abspath(str(Path(path).parent))) == os.path.normcase(os.path.abspath(str(root / "base"))):
        return "terminals\\base 是模板，不能直接登录账户，请先用「创建终端副本」复制一份"
    return None


def terminal_dir(terminal_path: str) -> Path:
    return Path(terminal_path).parent


def mql5_dir(terminal_path: str) -> Path:
    """受管终端一律便携模式，MQL5 数据目录就在终端目录里。"""
    return terminal_dir(terminal_path) / "MQL5"


# ---------------- 进程 ----------------
def find_processes(terminal_path: str):
    if psutil is None or not terminal_path:
        return []
    target = norm(terminal_path)
    out = []
    for p in psutil.process_iter(["pid", "exe"]):
        try:
            exe = p.info.get("exe")
            if exe and norm(exe) == target and not _zombie(p):
                out.append(p)
        except Exception:
            continue
    return out


def is_running(terminal_path: str) -> bool:
    return bool(find_processes(terminal_path))


def _zombie(p) -> bool:
    """已退出、只是还没被父进程回收的进程（Linux 下的僵尸进程）当作不存在。"""
    try:
        return p.status() == psutil.STATUS_ZOMBIE
    except Exception:
        return True


def proc_matches(entry: dict | None) -> "psutil.Process | None":
    if not entry or psutil is None:
        return None
    try:
        p = psutil.Process(int(entry["pid"]))
        if abs(p.create_time() - float(entry["create_time"])) > 1.0:
            return None
        if norm(p.exe()) != norm(entry["path"]) or _zombie(p):
            return None
        return p
    except Exception:
        return None


def close_owned(entry: dict | None, timeout: float = 30.0) -> tuple[bool, str]:
    """只关闭登记过的那一个进程。先发正常关闭（WM_CLOSE，终端会保存图表），超时再结束这一个 PID。"""
    p = proc_matches(entry)
    if p is None:
        return True, "终端未运行"
    pid = p.pid
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        p.terminate()
    try:
        p.wait(timeout=timeout)
        return True, "终端已关闭"
    except Exception:
        pass
    try:
        p.kill()  # 只结束这一个 PID（是我们自己启动的）
        p.wait(timeout=5)
    except Exception:
        pass
    return proc_matches(entry) is None, "终端未响应关闭，已结束该进程（本次图表改动可能没保存）"


def launch_terminal(terminal_path: str, config_ini: Path | None = None) -> tuple[int, float]:
    """以便携模式启动受管终端，返回 (pid, create_time)。"""
    args = [terminal_path, "/portable"]
    if config_ini:
        args.append(f"/config:{config_ini}")
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(args, cwd=str(terminal_dir(terminal_path)), creationflags=flags,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    ct = time.time()
    if psutil is not None:
        try:
            ct = psutil.Process(proc.pid).create_time()
        except Exception:
            pass
    return proc.pid, ct


# ---------------- EA 文件 ----------------
def install_files(terminal_path: str, ex5_src: Path, preset_src: Path | None) -> tuple[Path, Path | None]:
    m = mql5_dir(terminal_path)
    experts = m / "Experts" / FLEET_SUBDIR
    experts.mkdir(parents=True, exist_ok=True)
    dst = experts / ex5_src.name
    shutil.copy2(ex5_src, dst)
    pdst = None
    if preset_src and preset_src.exists():
        presets = m / "Presets"
        presets.mkdir(parents=True, exist_ok=True)
        pdst = presets / preset_src.name
        shutil.copy2(preset_src, pdst)
    return dst, pdst


def _ini_lines(creds: dict | None, startup: dict | None, allow_dll: bool) -> list[str]:
    lines: list[str] = []
    if creds:
        # 登录信息只写进这个临时文件，终端启动、登录完成后立刻抹掉删除
        lines += ["[Common]", f"Login={creds['login']}", f"Password={creds['password']}", f"Server={creds['server']}",
                  "KeepPrivate=1", "NewsEnable=0", "CertInstall=0", ""]
    lines += ["[Experts]", "AllowLiveTrading=1", f"AllowDllImport={1 if allow_dll else 0}", "Enabled=1",
              "Account=0", "Profile=0"]
    if startup:
        stem = Path(startup["fileName"]).stem
        lines += ["", "[StartUp]", f"Expert={FLEET_SUBDIR}\\{stem}", f"Symbol={startup['symbol']}",
                  f"Period={startup['timeframe']}"]
        if startup.get("preset"):
            lines.append(f"ExpertParameters={startup['preset']}")
    return lines


def write_config_ini(terminal_path: str, creds: dict | None = None, startup: dict | None = None,
                     allow_dll: bool = False, encoding: str = "utf-16") -> Path:
    """写一次性的启动配置（/config:xxx.ini）到该终端自己的目录，文件名随机。
    creds={login,password,server} 时包含登录信息，调用方必须在终端启动后用 wipe_file() 删除。"""
    ini = terminal_dir(terminal_path) / f"fleet_{secrets.token_hex(6)}.ini"
    ini.write_text("\r\n".join(_ini_lines(creds, startup, allow_dll)) + "\r\n", encoding=encoding)
    return ini


def write_start_ini(terminal_path: str, ex5_name: str, symbol: str, timeframe: str, preset: str,
                    encoding: str = "utf-16", creds: dict | None = None, allow_dll: bool = False) -> Path:
    """部署 EA 用的启动配置：[StartUp] 把 EA 挂到指定品种/周期的图表上，并打开算法交易。"""
    return write_config_ini(terminal_path, creds, {"fileName": ex5_name, "symbol": symbol, "timeframe": timeframe,
                                                   "preset": preset}, allow_dll, encoding)


def write_base_ini(terminal_path: str, encoding: str = "utf-16", creds: dict | None = None,
                   allow_dll: bool = False) -> Path:
    """普通启动用的配置：打开该终端的算法交易（Python 发单需要），可附带登录信息。"""
    return write_config_ini(terminal_path, creds, None, allow_dll, encoding)


def wipe_file(path) -> bool:
    """先用 0 覆盖再删除（启动配置里有密码）。"""
    try:
        p = Path(path)
        if not p.exists():
            return True
        try:
            n = p.stat().st_size
            with open(p, "r+b") as f:
                f.write(b"\0" * n)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            pass
        p.unlink()
        return True
    except Exception:
        return False


def wipe_stale_inis(folder) -> int:
    """删除某个受管终端目录（或整个 terminals 目录下各终端）里遗留的 fleet_*.ini。"""
    n = 0
    folder = Path(folder)
    if not folder.exists():
        return 0
    cands = list(folder.glob("fleet_*.ini")) + list(folder.glob("*/fleet_*.ini"))
    for f in cands:
        n += wipe_file(f)
    return n


# ---------------- 校验：EA 是否已加载（读终端日志，只读） ----------------
def log_marks(terminal_path: str) -> dict:
    """记录终端日志当前长度，之后只看新增部分。"""
    d = terminal_dir(terminal_path)
    marks = {}
    for sub in (d / "Logs", d / "MQL5" / "Logs"):
        if sub.exists():
            for f in sub.glob("*.log"):
                try:
                    marks[str(f)] = f.stat().st_size
                except OSError:
                    pass
    return marks


def _decode_log(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe"):
        raw = raw[2:]
    if len(raw) > 1 and raw[1:2] == b"\x00":
        return raw[: len(raw) // 2 * 2].decode("utf-16-le", "ignore")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("mbcs" if sys.platform == "win32" else "latin-1", "ignore")


OK_WORDS = ("loaded successfully", "加载成功", "成功加载", "已加载")
BAD_WORDS = ("failed", "cannot", "removed", "失败", "无法", "已删除", "已移除")


def find_ea_in_logs(terminal_path: str, ex5_name: str, marks: dict) -> tuple[str, str]:
    """在启动之后新写入的终端日志里找 EA 加载记录。返回 (ok|fail|seen|none, 那一行)。"""
    stem = Path(ex5_name).stem.lower()
    d = terminal_dir(terminal_path)
    seen = ""
    for sub in (d / "Logs", d / "MQL5" / "Logs"):
        if not sub.exists():
            continue
        for f in sub.glob("*.log"):
            try:
                start = marks.get(str(f), 0)
                with open(f, "rb") as fh:
                    fh.seek(start - (start % 2))
                    text = _decode_log(fh.read())
            except OSError:
                continue
            for line in text.splitlines():
                low = line.lower()
                if stem not in low:
                    continue
                clean = " ".join(line.split())[:200]
                if any(w in low for w in OK_WORDS):
                    return "ok", clean
                if any(w in low for w in BAD_WORDS):
                    return "fail", clean
                seen = seen or clean
    return ("seen", seen) if seen else ("none", "")


_EXPERT_RE = re.compile(r"<expert>.*?</expert>\s*", re.S | re.I)


def _read_text(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16"), "utf-16"
    if raw[1:2] == b"\x00":
        return raw.decode("utf-16-le"), "utf-16-le"
    try:
        return raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        enc = "mbcs" if sys.platform == "win32" else "latin-1"
        return raw.decode(enc), enc


def remove_expert_from_charts(terminal_path: str, ex5_name: str) -> int:
    """在（我们自己的）终端关闭状态下，从它的图表文件里删除指定 EA。返回改动的图表数。"""
    stem = Path(ex5_name).stem.lower()
    charts_root = mql5_dir(terminal_path) / "Profiles" / "Charts"
    if not charts_root.exists():
        return 0
    changed = 0
    for chr_file in charts_root.rglob("*.chr"):
        try:
            text, enc = _read_text(chr_file)
        except Exception:
            continue

        def repl(m):
            block = m.group(0)
            name = re.search(r"^\s*name=(.*)$", block, re.M | re.I)
            path = re.search(r"^\s*path=(.*)$", block, re.M | re.I)
            n = name.group(1).strip().lower() if name else ""
            pth = path.group(1).strip().lower() if path else ""
            if n == stem or pth.endswith(f"{stem}.ex5") or pth.endswith(f"\\{stem}"):
                return ""
            return block

        new = _EXPERT_RE.sub(repl, text)
        if new != text:
            shutil.copy2(chr_file, chr_file.with_suffix(".chr.fleetbak"))
            if enc.startswith("utf-16"):
                chr_file.write_bytes(b"\xff\xfe" + new.encode("utf-16-le"))
            else:
                chr_file.write_text(new, encoding=enc)
            changed += 1
    return changed


# ---------------- 复制终端 ----------------
SKIP_DIRS = {"bases", "logs", "tester"}
SKIP_CONFIG_FILES = {"accounts.dat"}      # 模板里保存的其它账户登录信息不复制


def clone_terminal(src_dir: str, dst_dir: str, root: Path) -> tuple[bool, str]:
    """把一份 MT5 程序文件夹复制成新的便携终端。
    只读取源目录（不会改动它，哪怕它正在运行）；跳过历史数据、日志、模板里保存的账户。
    先复制到 <账号>.copying，完成后再改名，避免半截副本被当成可用终端。"""
    src, dst = Path(src_dir), Path(dst_dir)
    if not (src / "terminal64.exe").exists():
        return False, f"模板目录里没有 terminal64.exe：{src}"
    if not is_under(str(dst / "terminal64.exe"), root):
        return False, f"目标必须在 {root} 里面"
    if (dst / "terminal64.exe").exists():
        return True, f"已存在，跳过：{dst}"
    if is_under(str(src / "terminal64.exe"), dst) or os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
        return False, "模板和目标是同一个文件夹"
    stage = dst.with_name(dst.name + ".copying")
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    errors = []
    for dirpath, dirnames, filenames in os.walk(src):
        rel = Path(dirpath).relative_to(src)
        if not rel.parts:
            dirnames[:] = [n for n in dirnames if n.lower() not in SKIP_DIRS]
            filenames = [f for f in filenames if not (f.lower().startswith("fleet_") and f.lower().endswith(".ini"))]
        else:
            dirnames[:] = [n for n in dirnames if n.lower() != "logs"]
            if rel.parts[0].lower() == "config":
                filenames = [f for f in filenames if f.lower() not in SKIP_CONFIG_FILES]
        (stage / rel).mkdir(parents=True, exist_ok=True)
        for f in filenames:
            try:
                shutil.copy2(Path(dirpath) / f, stage / rel / f)
            except OSError as e:   # 模板正在运行时个别文件可能被占用，跳过即可
                errors.append(f"{rel / f}：{e.strerror or e}")
    if not (stage / "terminal64.exe").exists():
        shutil.rmtree(stage, ignore_errors=True)
        return False, "复制 terminal64.exe 失败：" + ("；".join(errors[:3]) or "未知原因")
    if dst.exists():
        # 目标文件夹已存在但没有 terminal64.exe（例如只放了 MQL5）：合并进去
        shutil.copytree(stage, dst, dirs_exist_ok=True)
        shutil.rmtree(stage, ignore_errors=True)
    else:
        os.replace(stage, dst)
    tail = f"（{len(errors)} 个被占用的文件已跳过）" if errors else ""
    return True, f"已复制到 {dst}{tail}"


# ---------------- 模板 MT5 ----------------
TEMPLATE_SKIP = {".venv", "venv", "data", "data_mock", "build", "dist", "__pycache__", "static", "fleet", "tools",
                 "tests", "$recycle.bin", "system volume information"}


PORTABLE_MARKER = ".mt5fleet-portable"     # 便携版文件夹里的标记文件
PREFERRED_TEMPLATE = "MT5模板"             # 程序目录里优先使用的模板文件夹名（便携版也用这个名字）


def is_app_copy(d: Path) -> bool:
    """这个文件夹是不是本程序的另一份（便携版 / 复制出来的备份）。找模板时不进去，避免把别处的 MT5 副本当模板。"""
    try:
        return (d / PORTABLE_MARKER).exists() or ((d / "app.py").is_file() and (d / "fleet").is_dir())
    except OSError:
        return False


def _template_dir_ok(parent: Path, name: str) -> bool:
    low = name.lower()
    if low in TEMPLATE_SKIP or low.endswith(".copying") or low.endswith(".building") or low.startswith("python"):
        return False
    return not is_app_copy(parent / name)


def find_templates(app_dir: Path, troot: Path, max_depth: int = 4) -> list[Path]:
    """在程序目录里找可以当模板的 MT5（有 terminal64.exe 的文件夹）。terminals\\base 优先；
    terminals 里其它账户的副本不算模板。"""
    found: list[Path] = []
    base = troot / "base"
    if (base / "terminal64.exe").exists():
        found.append(base)
    if not app_dir.exists():
        return found
    for dirpath, dirnames, filenames in os.walk(app_dir):
        d = Path(dirpath)
        depth = len(d.relative_to(app_dir).parts)
        dirnames[:] = [n for n in dirnames if depth < max_depth and _template_dir_ok(d, n)
                       and not (os.path.normcase(str(d)) == os.path.normcase(str(troot)) and n.lower() != "base")]
        if "terminal64.exe" in [f.lower() for f in filenames] and d not in found:
            found.append(d)
    head = [x for x in found if x == base]
    rest = [x for x in found if x != base]

    def rank(d: Path):
        try:
            mt = (d / "terminal64.exe").stat().st_mtime
        except OSError:
            mt = 0
        # 「MT5模板」优先（新版模板放这里），其余按 terminal64.exe 新旧排序（新的优先）
        return (0 if d.name == PREFERRED_TEMPLATE else 1, -mt)
    return head + sorted(rest, key=rank)


def exe_version(path) -> str:
    """（只读）exe 的文件版本，例如 5.0.0.6246；读不到返回空。"""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(str(path), 0, size, buf):
            return ""
        ptr, ln = ctypes.c_void_p(), wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(ln)) or not ptr.value:
            return ""

        class _VS(ctypes.Structure):
            _fields_ = [("sig", wintypes.DWORD), ("sv", wintypes.DWORD), ("ms", wintypes.DWORD), ("ls", wintypes.DWORD)]
        vs = ctypes.cast(ptr, ctypes.POINTER(_VS)).contents
        return f"{vs.ms >> 16}.{vs.ms & 0xFFFF}.{vs.ls >> 16}.{vs.ls & 0xFFFF}"
    except Exception:
        return ""


# ---------------- 服务器列表 servers.dat（只读导入） ----------------
def _read_origin(d: Path) -> str:
    o = d / "origin.txt"
    try:
        raw = o.read_bytes()
    except OSError:
        return ""
    for enc in ("utf-16", "utf-8-sig", "mbcs" if sys.platform == "win32" else "latin-1"):
        try:
            return raw.decode(enc).strip().strip("\x00")
        except Exception:
            continue
    return ""


def appdata_data_dirs() -> list[tuple[Path, str]]:
    """（只读）列出 %APPDATA%\\MetaQuotes\\Terminal\\<编号> 数据目录和它对应的安装目录。
    只在用户点「导入服务器列表」时读取，不会写入。"""
    base = os.environ.get("APPDATA")
    if not base:
        return []
    tdir = Path(base) / "MetaQuotes" / "Terminal"
    out = []
    try:
        for d in tdir.iterdir():
            if d.is_dir() and (d / "Config").exists():
                out.append((d, _read_origin(d)))
    except OSError:
        pass
    return out


def servers_dat_count(path) -> int:
    """servers.dat 里的服务器条目数（只读文件头；-1 = 不是 servers.dat 或读不出来）。"""
    try:
        with open(path, "rb") as f:
            h = f.read(0xB0)
    except OSError:
        return -1
    if len(h) < 0xB0 or h[4:8] != "Co".encode("utf-16-le") or "Servers".encode("utf-16-le") not in h[0x80:0xA4]:
        return -1
    n = int.from_bytes(h[0xAC:0xB0], "little")
    return n if 0 <= n < 100000 else -1


def servers_dat_candidates(folder: str) -> list[dict]:
    """某个 MT5 文件夹（安装目录或数据目录）对应的 servers.dat，可能有多份，按修改时间新→旧。"""
    out = []
    if not folder:
        return out
    f = Path(folder)
    for c in (f / "Config" / "servers.dat", f / "servers.dat"):
        if c.is_file():
            out.append(c)
    norm_f = os.path.normcase(os.path.abspath(str(f))).rstrip("\\/")
    for d, origin in appdata_data_dirs():
        if origin and os.path.normcase(os.path.abspath(origin)).rstrip("\\/") == norm_f:
            c = d / "Config" / "servers.dat"
            if c.is_file():
                out.append(c)
    rows = []
    for c in out:
        try:
            st = c.stat()
            rows.append({"path": str(c), "size": st.st_size, "mtime": int(st.st_mtime), "count": servers_dat_count(c)})
        except OSError:
            pass
    rows.sort(key=lambda r: (-r["count"], -r["mtime"]))   # 服务器最多的优先，其次最新
    return rows


def scan_mt5_installs(extra_roots: list[str] | None = None, budget: float = 4.0, troot: Path | None = None) -> list[dict]:
    """（只读）找电脑上已有的 MT5：各盘根目录往下 3 层、Program Files、APPDATA 数据目录。"""
    t0 = time.time()
    seen: dict[str, dict] = {}

    def add(folder: Path, kind: str):
        key = os.path.normcase(os.path.abspath(str(folder)))
        if troot is not None and (is_under(str(folder / "x"), troot) and not key.endswith(os.sep + "base")):
            return
        if key in seen:
            return
        cands = servers_dat_candidates(str(folder))
        seen[key] = {"path": str(folder), "kind": kind, "servers": cands[0] if cands else None,
                     "running": is_running(str(folder / "terminal64.exe")) if kind == "安装目录" else False}

    roots: list[Path] = [Path(r) for r in (extra_roots or []) if r]
    if sys.platform == "win32":
        for letter in "CDEFGHIJ":
            r = Path(f"{letter}:\\")
            if r.exists():
                roots.append(r)
        for env in ("ProgramFiles", "ProgramFiles(x86)"):
            if os.environ.get(env):
                roots.insert(0, Path(os.environ[env]))
    skip = {"windows", "$recycle.bin", "system volume information", "programdata", "users", "recovery",
            "perflogs", "node_modules", ".git", "winsxs", "msocache", "intel", "amd", "nvidia"}
    for r in roots:
        stack = [(r, 0)]
        while stack and time.time() - t0 < budget:
            d, depth = stack.pop()
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            if any(e.name.lower() == "terminal64.exe" for e in entries if e.is_file()):
                add(d, "安装目录")
                continue
            if depth >= 3:
                continue
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False) and e.name.lower() not in skip and not e.name.startswith("."):
                        stack.append((Path(e.path), depth + 1))
                except OSError:
                    pass
    for d, origin in appdata_data_dirs():
        if (d / "Config" / "servers.dat").is_file():
            key = os.path.normcase(os.path.abspath(origin)) if origin else ""
            if key and key in seen and not seen[key]["servers"]:
                continue
            seen.setdefault(os.path.normcase(str(d)), {
                "path": str(d), "kind": "数据目录", "origin": origin,
                "servers": (servers_dat_candidates(str(d)) or [None])[0], "running": False})
    return sorted(seen.values(), key=lambda x: (x["servers"] is None, -((x["servers"] or {}).get("count") or 0), x["path"].lower()))


def import_servers_dat(src_file: str, lib_dir: Path) -> tuple[bool, str, dict]:
    """把别的 MT5 的 servers.dat 只读复制到本程序的数据目录（lib_dir\\servers.dat）。"""
    src = Path(src_file)
    if src.name.lower() != "servers.dat" or not src.is_file():
        return False, f"不是有效的 servers.dat：{src}", {}
    size = src.stat().st_size
    if size < 64 or size > 20 * 1024 * 1024:
        return False, f"servers.dat 大小异常（{size} 字节）", {}
    lib_dir.mkdir(parents=True, exist_ok=True)
    dst = lib_dir / "servers.dat"
    if dst.exists():
        shutil.copy2(dst, lib_dir / "servers.dat.prev")
    with open(src, "rb") as f:   # 只读打开源文件
        data = f.read()
    tmp = dst.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dst)
    digest = hashlib.sha1(data).hexdigest()[:16]
    cnt = servers_dat_count(dst)
    what = f"{cnt} 个服务器" if cnt >= 0 else f"{size} 字节"
    return True, f"已导入服务器列表（{what}）", {"source": str(src), "size": size, "hash": digest, "at": int(time.time()), "count": cnt}


def apply_servers_dat(terminal_path: str, lib_file: Path) -> str:
    """把导入的 servers.dat 放进（未运行的）受管终端的 Config。每份导入只放一次，
    之后终端自己搜索到的新服务器不会被覆盖。返回说明文字（空 = 没动）。"""
    if not lib_file.is_file():
        return ""
    data = lib_file.read_bytes()
    digest = hashlib.sha1(data).hexdigest()[:16]
    cfg = terminal_dir(terminal_path) / "Config"
    mark = cfg / "servers.dat.fleet"
    try:
        if mark.exists() and mark.read_text(encoding="utf-8").strip() == digest:
            return ""
    except OSError:
        pass
    cfg.mkdir(parents=True, exist_ok=True)
    dst = cfg / "servers.dat"
    have, new_n = (servers_dat_count(dst) if dst.exists() else -1), servers_dat_count(lib_file)
    if have >= 0 and new_n >= 0 and have > new_n:     # 这个终端自己的列表更全：不替换
        return ""
    bak = cfg / "servers.dat.fleetbak"
    if dst.exists() and not bak.exists():
        shutil.copy2(dst, bak)
    dst.write_bytes(data)
    mark.write_text(digest, encoding="utf-8")
    return "已放入导入的服务器列表"


# ---------------- 每账户参数文件（魔术号） ----------------
def make_account_preset(terminal_path: str, base_preset: Path | None, stem: str, login: str,
                        overrides: dict) -> str:
    """基于 .set 生成这个账户专用的参数文件（例如每个账户不同的魔术号），放到该终端 MQL5\\Presets。"""
    text, enc = ("", "utf-16")
    if base_preset is not None and base_preset.exists():
        text, enc = _read_text(base_preset)
    lines = text.splitlines()
    for k, v in overrides.items():
        pat = re.compile(rf"^\s*{re.escape(k)}\s*=", re.I)
        hit = False
        for i, ln in enumerate(lines):
            if pat.match(ln):
                rest = ln.split("=", 1)[1]
                tail = rest[rest.index("||"):] if "||" in rest else ""
                lines[i] = f"{k}={v}{tail}"
                hit = True
        if not hit:
            lines.append(f"{k}={v}")
    name = f"fleet_{stem}_{login}.set"
    dst_dir = mql5_dir(terminal_path) / "Presets"
    dst_dir.mkdir(parents=True, exist_ok=True)
    body = "\r\n".join(lines) + "\r\n"
    if enc.startswith("utf-16"):
        (dst_dir / name).write_bytes(b"\xff\xfe" + body.encode("utf-16-le"))
    else:
        (dst_dir / name).write_text(body, encoding=enc)
    return name


# ---------------- 接管：MT5 自动更新后自己重启的终端 ----------------
def _cmdline(p) -> list[str]:
    try:
        return [str(x) for x in (p.cmdline() or [])]
    except Exception:
        return []


def launched_by_fleet(p, terminal_path: str) -> bool:
    """这个进程是不是本程序发起的启动（包括 MT5 LiveUpdate 更新后用原参数重新启动的那一个）。
    判断依据：exe 就是这个受管终端；命令行带 /portable；/config 指向本终端目录里我们生成的 fleet_*.ini。
    用户自己双击打开的终端没有这个配置参数，不会被接管。"""
    try:
        if norm(p.exe()) != norm(terminal_path):
            return False
    except Exception:
        return False
    args = _cmdline(p)
    joined = " ".join(args)
    if "/portable" not in joined.lower():
        return False
    tdir = os.path.normcase(os.path.abspath(str(terminal_dir(terminal_path))))
    for a in args:
        m = re.search(r"/config:\"?(.+?)\"?$", a, re.I)
        if not m:
            continue
        cfg = m.group(1).strip().strip('"')
        name = os.path.basename(cfg).lower()
        parent = os.path.normcase(os.path.abspath(os.path.dirname(cfg) or tdir))
        if name.startswith("fleet_") and name.endswith(".ini") and parent == tdir:
            return True
    return False


def adoptable_processes(terminal_path: str) -> list:
    return [p for p in find_processes(terminal_path) if launched_by_fleet(p, terminal_path)]


def proc_entry(p, terminal_path: str) -> dict | None:
    try:
        return {"path": terminal_path, "pid": int(p.pid), "create_time": float(p.create_time())}
    except Exception:
        return None


def liveupdate_lines(terminal_path: str, marks: dict | None = None) -> list[str]:
    """终端日志里 MT5 自动更新（LiveUpdate）的记录，用来向用户解释“终端为什么自己重启了”。"""
    d = terminal_dir(terminal_path) / "Logs"
    out = []
    if not d.exists():
        return out
    for f in sorted(d.glob("*.log"))[-2:]:
        try:
            start = (marks or {}).get(str(f), 0)
            with open(f, "rb") as fh:
                fh.seek(start - (start % 2))
                text = _decode_log(fh.read())
        except OSError:
            continue
        out += [" ".join(ln.split())[:200] for ln in text.splitlines() if "liveupdate" in ln.lower()]
    return out


# ---------------- 图表配置（profile）：单图表 + 把 EA 写进图表文件 ----------------
# MT5 用 [StartUp] 挂上的 EA 不会保存进图表配置：终端一重启（包括 MT5 自动更新后的重启）EA 就没了，
# 而且会留下一个空的图表（windows_total=0）。所以分发时改为在终端关闭状态下直接写一个带 <expert> 段的图表文件，
# 和用户在 MT5 里手动把 EA 拖到图表上后终端自己保存的格式一样，以后每次启动都会自动加载。
PERIOD_CODES = {"M1": (0, 1), "M2": (0, 2), "M3": (0, 3), "M4": (0, 4), "M5": (0, 5), "M6": (0, 6), "M10": (0, 10),
                "M12": (0, 12), "M15": (0, 15), "M20": (0, 20), "M30": (0, 30), "H1": (1, 1), "H2": (1, 2), "H3": (1, 3),
                "H4": (1, 4), "H6": (1, 6), "H8": (1, 8), "H12": (1, 12), "D1": (2, 1), "W1": (3, 1), "MN1": (4, 1)}

_CHART_HEAD = """<chart>
id={id}
symbol={symbol}
description=
period_type={ptype}
period_size={psize}
digits=5
tick_size=0.000000
position_time=0
scale_fix=0
scale_fixed_min=0.000000
scale_fixed_max=0.000000
scale_fix11=0
scale_bar=0
scale_bar_val=1.000000
scale=8
mode=1
fore=0
grid=1
volume=0
scroll=1
shift=1
shift_size=20.000000
fixed_pos=0.000000
ticker=1
ohlc=0
one_click=0
one_click_btn=1
bidline=1
askline=0
lastline=0
days=0
descriptions=0
tradelines=1
tradehistory=1
window_left=0
window_top=0
window_right=0
window_bottom=0
window_type=3
floating=0
floating_left=0
floating_top=0
floating_right=0
floating_bottom=0
floating_type=1
floating_toolbar=1
floating_tbstate=
background_color=0
foreground_color=16777215
barup_color=65280
bardown_color=65280
bullcandle_color=0
bearcandle_color=16777215
chartline_color=65280
volumes_color=3329330
grid_color=10061943
bidline_color=10061943
askline_color=255
lastline_color=49152
stops_color=255
windows_total=1
"""

_CHART_WINDOW = """
<window>
height=100.000000
objects=0

<indicator>
name=Main
path=
apply=1
show_data=1
scale_inherit=0
scale_line=0
scale_line_percent=50
scale_line_value=0.000000
scale_fix_min=0
scale_fix_min_val=0.000000
scale_fix_max=0
scale_fix_max_val=0.000000
expertmode=0
fixed_height=-1
</indicator>
</window>
</chart>
"""


def preset_inputs(preset_path: Path | None) -> list[tuple[str, str]]:
    """把 .set 参数文件转成图表文件 <inputs> 里的 name=value（去掉优化用的 ||start||step||stop||Y 尾巴）。"""
    if preset_path is None or not Path(preset_path).exists():
        return []
    text, _ = _read_text(Path(preset_path))
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith(";") or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        if "||" in v:
            v = v[: v.index("||")]
        out.append((k.strip(), v.strip()))
    return out


def expert_block(stem: str, inputs: list[tuple[str, str]] | None = None, allow_dll: bool = False) -> str:
    lines = ["<expert>", f"name={stem}", f"path=Experts\\{FLEET_SUBDIR}\\{stem}.ex5", f"expertmode={5 if allow_dll else 1}"]
    if inputs:
        lines.append("<inputs>")
        lines += [f"{k}={v}" for k, v in inputs]
        lines.append("</inputs>")
    lines.append("</expert>")
    return "\n".join(lines) + "\n"


def chart_text(symbol: str, timeframe: str, expert: str = "") -> str:
    ptype, psize = PERIOD_CODES.get(timeframe.upper(), (0, 15))
    head = _CHART_HEAD.format(id=134000000000000000 + secrets.randbelow(10 ** 15), symbol=symbol, ptype=ptype, psize=psize)
    return head + (("\n" + expert) if expert else "") + _CHART_WINDOW


def profile_name(terminal_path: str) -> str:
    """当前使用的图表配置名（Config\\common.ini 的 [Charts] ProfileLast），默认 Default。"""
    f = terminal_dir(terminal_path) / "Config" / "common.ini"
    try:
        text, _ = _read_text(f)
        m = re.search(r"^\s*ProfileLast\s*=\s*(.+?)\s*$", text, re.M | re.I)
        if m and m.group(1) and not re.search(r"[\\/:*?\"<>|]|\.\.", m.group(1)):
            return m.group(1)
    except Exception:
        pass
    return "Default"


def profile_dir(terminal_path: str) -> Path:
    return mql5_dir(terminal_path) / "Profiles" / "Charts" / profile_name(terminal_path)


def _chart_info(path: Path) -> dict:
    text, _ = _read_text(path)
    experts = []
    for m in _EXPERT_RE.finditer(text):
        nm = re.search(r"^\s*name=(.*)$", m.group(0), re.M | re.I)
        pth = re.search(r"^\s*path=(.*)$", m.group(0), re.M | re.I)
        experts.append(((nm.group(1).strip() if nm else ""), (pth.group(1).strip() if pth else "")))
    sym = re.search(r"^\s*symbol=(.*)$", text, re.M | re.I)
    pt = re.search(r"^\s*period_type=(\d+)", text, re.M | re.I)
    ps = re.search(r"^\s*period_size=(\d+)", text, re.M | re.I)
    wt = re.search(r"^\s*windows_total=(\d+)", text, re.M | re.I)
    return {"symbol": sym.group(1).strip() if sym else "", "period": (int(pt.group(1)) if pt else -1, int(ps.group(1)) if ps else -1),
            "windows": int(wt.group(1)) if wt else 0, "experts": experts}


def _expert_matches(ex: tuple[str, str], stem: str) -> bool:
    n, pth = ex[0].lower(), ex[1].lower()
    stem = stem.lower()
    return n == stem or pth.endswith(f"\\{stem}.ex5") or pth.endswith(f"\\{stem}") or pth == f"{stem}.ex5"


def _write_chart(path: Path, text: str):
    path.write_bytes(b"\xff\xfe" + text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-16-le"))


def _free_chart_name(folder: Path) -> Path:
    for i in range(1, 1000):
        p = folder / f"chart{i:02d}.chr"
        if not p.exists():
            return p
    return folder / f"chart_{secrets.token_hex(3)}.chr"


def list_charts(terminal_path: str) -> list[dict]:
    d = profile_dir(terminal_path)
    out = []
    for f in sorted(d.glob("*.chr")) if d.exists() else []:
        try:
            info = _chart_info(f)
        except Exception:
            continue
        info["file"] = f.name
        out.append(info)
    return out


def prepare_profile(terminal_path: str, symbol: str, timeframe: str, expert: dict | None = None,
                    drop_experts: list[str] | None = None, allow_dll: bool = False) -> dict:
    """在（本程序的、已关闭的）终端里整理图表配置。
    - 没有 EA 的普通图表全部移走（MT5 模板自带的 5~6 个 XAUUSD 图表、[StartUp] 留下的空图表）；
    - drop_experts 里的同名 EA 所在的图表移走（替换旧版本 / 停止 EA）；
    - 挂着其它 EA 的图表保留；
    - expert={stem, inputs} 时新建一个挂着该 EA 的 symbol/timeframe 图表；
    - 最后一个图表都没有时，新建一个 symbol/timeframe 的普通图表。
    移走的文件备份到 终端目录\\fleet_backup\\charts-时间\\。已经是目标状态时不做任何改动。"""
    d = profile_dir(terminal_path)
    d.mkdir(parents=True, exist_ok=True)
    want = PERIOD_CODES.get(timeframe.upper(), (0, 15))
    drop = [x for x in (drop_experts or [])]
    charts = []
    for f in sorted(d.glob("*.chr")):
        try:
            charts.append((f, _chart_info(f)))
        except Exception:
            continue
    remove, keep_ea = [], []
    for f, info in charts:
        if not info["experts"]:
            remove.append(f)
        elif any(_expert_matches(e, s) for e in info["experts"] for s in drop):
            remove.append(f)
        else:
            keep_ea.append(f)
    create_plain = expert is None and not keep_ea
    # 已经是目标状态：只有一个普通图表，品种周期一致
    if create_plain and len(charts) == 1 and len(remove) == 1:
        info = charts[0][1]
        if info["symbol"] == symbol and info["period"] == want and info["windows"] >= 1:
            return {"changed": False, "removed": 0, "created": "", "backup": "", "kept": 0}
    backup = ""
    if remove:
        bdir = terminal_dir(terminal_path) / "fleet_backup" / f"charts-{time.strftime('%Y%m%d-%H%M%S')}"
        bdir.mkdir(parents=True, exist_ok=True)
        for f in remove:
            try:
                shutil.copy2(f, bdir / f.name)
                f.unlink()
            except OSError:
                pass
        backup = str(bdir)
    created = ""
    if expert is not None:
        p = _free_chart_name(d)
        _write_chart(p, chart_text(symbol, timeframe, expert_block(expert["stem"], expert.get("inputs"), allow_dll)))
        created = p.name
    elif create_plain:
        p = _free_chart_name(d)
        _write_chart(p, chart_text(symbol, timeframe))
        created = p.name
    return {"changed": bool(remove or created), "removed": len(remove), "created": created, "backup": backup,
            "kept": len(keep_ea)}


def charts_with_expert(terminal_path: str, stem: str) -> list[str]:
    return [c["file"] for c in list_charts(terminal_path) if any(_expert_matches(e, stem) for e in c["experts"])]


# ---------------- 品种名检查（券商后缀） ----------------
def symbol_core(name: str) -> str:
    """去掉券商后缀后的“核心名”：USDJPYc / USDJPYm / USDJPY.r / usdjpy → USDJPY。"""
    s = (name or "").strip()
    s = re.split(r"[._#-]", s, 1)[0] or s
    if any(ch.isupper() for ch in s):
        m = re.match(r"^(.*?[A-Z0-9])[a-z]+$", s)
        if m:
            s = m.group(1)
    return s.upper()


def suggest_symbols(name: str, available: list[str], limit: int = 5) -> list[str]:
    """name 在这个账户里不存在时，找同一个品种的其它写法（例如 USDJPY → USDJPYc）。"""
    core = symbol_core(name)
    if not core:
        return []
    exact = [s for s in available if symbol_core(s) == core and s != name]
    if exact:
        return sorted(exact, key=lambda s: (len(s), s))[:limit]
    up = name.upper()
    loose = [s for s in available if s.upper().startswith(up) or up.startswith(s.upper())]
    return sorted(loose, key=lambda s: (len(s), s))[:limit]
