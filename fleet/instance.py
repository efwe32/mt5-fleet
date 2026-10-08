"""启动时清理「上次没退出干净的本程序后台」。

只会结束同时满足下面条件的进程：
  1. 是 Python 进程（或打包后的 MT5Fleet.exe）；
  2. 主进程：命令行里的 app.py 解析后就是本程序的 app.py（同一个文件，不是别的文件夹里的另一份，
     例如 D:\\15\\新建文件夹\\MT5批量终端 里的便携版和 D:\\15 互不影响）；
     工作进程：工作目录就是本程序目录，或者它的父进程就是上面找到的主进程；
  3. 命令行是本程序的入口（app.py / MT5Fleet.exe）或它派生的 multiprocessing 工作进程；
  4. 不是当前进程，也不是当前进程的父进程（.venv 的 python.exe / pythonw.exe 启动器）。
不会结束任何 terminal64.exe（受管终端继续运行，新程序启动后会重新接管它们），也不会碰其它 Python 程序。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p)) if p else ""


def _same_dir(a: str, b: Path) -> bool:
    try:
        return _norm(a) == _norm(str(b))
    except Exception:
        return False


def _my_lineage() -> set[int]:
    out = {os.getpid()}
    if psutil is None:
        return out
    try:
        p = psutil.Process()
        for par in p.parents():
            out.add(par.pid)
    except Exception:
        pass
    return out


def _is_app_cmdline(args: list[str], cwd: str, root: Path) -> bool:
    """命令行是不是本程序：python app.py ... / MT5Fleet.exe / multiprocessing 工作进程。"""
    if not args:
        return False
    low = [a.lower() for a in args]
    exe0 = Path(args[0]).name.lower()
    if exe0 == "mt5fleet.exe" and _norm(str(Path(args[0]).parent)) == _norm(str(root)):
        return True
    for a in args[1:]:
        if a.lower().endswith("app.py"):
            full = a if os.path.isabs(a) else os.path.join(cwd or "", a)
            if _norm(full) == _norm(str(root / "app.py")):
                return True
    if any("multiprocessing" in a for a in low) and (any("spawn_main" in a for a in low) or "--multiprocessing-fork" in low):
        return True
    return False


def _data_dir_of(args: list[str], cwd: str, root: Path) -> str:
    """主进程命令行对应的数据目录（--data-dir，默认 程序目录\\data 或 data_mock）。"""
    d = ""
    for i, a in enumerate(args):
        if a == "--data-dir" and i + 1 < len(args):
            d = args[i + 1]
        elif a.startswith("--data-dir="):
            d = a.split("=", 1)[1]
    if not d:
        d = str(root / ("data_mock" if "--mock" in args else "data"))
    elif not os.path.isabs(d):
        d = os.path.join(cwd or str(root), d)
    return _norm(d)


def _live_python_parent(ppid: int, child) -> bool:
    """父进程是否是一个还在运行的 Python 进程。父进程已结束（孤儿被系统接管、或 Windows 上 PID 被别的程序复用）返回 False。"""
    try:
        par = psutil.Process(ppid)
        if par.create_time() > child.create_time() + 0.5:
            return False          # PID 复用：这个“父进程”比子进程还晚启动
        name = (par.name() or "").lower()
        return name.startswith("python") or name == "mt5fleet.exe"
    except Exception:
        return False


def find_stale(root: Path, data_dir: Path | None = None) -> list:
    """找出上次运行留下的本程序进程（主进程 + 工作进程），按“子进程在前”排序。
    data_dir 给定时，主进程只算使用同一个数据目录的那些（别的数据目录的实例不碰）。"""
    if psutil is None:
        return []
    mine = _my_lineage()
    found = {}
    for p in psutil.process_iter(["pid", "name", "cmdline", "cwd", "ppid"]):
        try:
            if p.pid in mine:
                continue
            name = (p.info.get("name") or "").lower()
            if not (name.startswith("python") or name == "mt5fleet.exe"):
                continue
            args = p.info.get("cmdline") or []
            cwd = p.info.get("cwd") or ""
            if not _is_app_cmdline(args, cwd, root):
                continue
            is_worker = any("multiprocessing" in a for a in args)
            # 工作进程的命令行里没有 app.py，只能看工作目录（multiprocessing 子进程继承主进程的工作目录）；
            # 工作目录读不到的，后面再看父进程是不是本程序的主进程
            if is_worker and cwd and not _same_dir(cwd, root):
                continue
            if data_dir is not None and not is_worker:
                if _data_dir_of(args, cwd, root) != _norm(str(data_dir)):
                    continue
            found[p.pid] = p
        except Exception:
            continue
    # multiprocessing 工作进程：还要求它的父进程是上面找到的本程序进程，或父进程已经不在了（孤儿）
    out = []
    for pid, p in found.items():
        args = p.info.get("cmdline") or []
        if any("multiprocessing" in a for a in args):
            ppid = p.info.get("ppid") or 0
            if not p.info.get("cwd") and ppid not in found:
                continue      # 工作目录读不到、父进程也不是本程序：不确定是谁的，不碰
            if ppid and ppid not in found and _live_python_parent(ppid, p):
                # 父进程是另一个还在运行的 Python 程序（别的程序或别的数据目录的实例）：不碰
                continue
        out.append(p)
    # 子进程先结束，主进程后结束
    out.sort(key=lambda p: 0 if any("multiprocessing" in a for a in (p.info.get("cmdline") or [])) else 1)
    return out


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if os.name != "nt":
            # 和 uvicorn 一样：刚关闭的连接（TIME_WAIT）不算占用。Windows 上不设，避免误判
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def cleanup_stale(root: Path, data_dir: Path | None = None, port: int | None = None, timeout: float = 8.0) -> list[str]:
    """结束上次残留的本程序进程（不碰 MT5 终端）。返回被结束进程的说明。"""
    procs = find_stale(root, data_dir)
    done = []
    for p in procs:
        try:
            desc = f"PID {p.pid}（{' '.join((p.info.get('cmdline') or [])[:3])[:80]}）"
            # 直接结束（不走正常退出流程）：正常退出会按设置关闭 MT5 终端，而这里要让终端和 EA 继续运行
            p.kill()
            done.append(desc)
        except Exception:
            continue
    if procs:
        gone, alive = psutil.wait_procs(procs, timeout=timeout)
        for p in alive:
            try:
                p.kill()
            except Exception:
                pass
        if alive:
            psutil.wait_procs(alive, timeout=3)
    if port and done:
        end = time.time() + 5
        while port_in_use(port) and time.time() < end:
            time.sleep(0.2)
    return done


class InstanceLock:
    """数据目录\\app.lock：记录当前运行的本程序（PID、启动时间、端口），正常退出时删除。"""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "app.lock"

    def read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def write(self, port: int, version: str = ""):
        info = {"pid": os.getpid(), "port": port, "at": time.time(), "exe": sys.executable, "version": version}
        try:
            if psutil is not None:
                info["create_time"] = psutil.Process().create_time()
            self.path.write_text(json.dumps(info), encoding="utf-8")
        except Exception:
            pass

    def release(self):
        try:
            if self.read().get("pid") == os.getpid():
                self.path.unlink()
        except Exception:
            pass


# ---------------- 已在运行的实例（一键启动时直接打开网页 / 停止后台） ----------------
def running_instance(root: Path, data_dir: Path) -> dict | None:
    """数据目录里登记的、仍在运行的本程序主进程（PID + 启动时间 + 命令行都对得上）。"""
    info = InstanceLock(data_dir).read()
    if not info or psutil is None:
        return None
    try:
        p = psutil.Process(int(info["pid"]))
        if info.get("create_time") and abs(p.create_time() - float(info["create_time"])) > 1.0:
            return None
        if p.status() == psutil.STATUS_ZOMBIE:
            return None
        if not _is_app_cmdline(p.cmdline(), p.cwd(), root):
            return None
        return info
    except Exception:
        return None


def probe(port: int, timeout: float = 3.0) -> str | None:
    """网页服务是否正常响应；返回页面里的访问令牌。"""
    import re
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/", timeout=timeout) as r:
            html = r.read(200000).decode("utf-8", "ignore")
        m = re.search(r'FLEET_TOKEN = "([^"]+)"', html)
        return m.group(1) if m else None
    except Exception:
        return None


def request_exit(port: int, token: str, timeout: float = 5.0) -> bool:
    """让正在运行的程序正常退出（和网页里点「退出程序」一样）。"""
    import urllib.request
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{int(port)}/api/exit", data=b"{}", method="POST",
                                     headers={"X-Fleet-Token": token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def wait_exit(pid: int, timeout: float) -> bool:
    if psutil is None:
        return False
    end = time.time() + timeout
    while time.time() < end:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:   # 已退出、只是还没被父进程回收
                return True
        except psutil.NoSuchProcess:
            return True
        except Exception:
            return False
        time.sleep(0.3)
    return False
