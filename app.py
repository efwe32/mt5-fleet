"""MT5 Fleet 启动入口。

用法：
  python app.py              正常模式（需要 Windows + MetaTrader5 包 + 每个账户一个 MT5 终端）
  python app.py --mock       仅供开发者自动化测试（内置假终端）
  python app.py --port 8765 --no-browser --data-dir D:\\15\\data
受管终端固定放在 程序目录\\terminals\\<账号>\\terminal64.exe（模板：terminals\\base）。
"""
from __future__ import annotations

import argparse
import multiprocessing
import os
import signal
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if os.name != "nt":
            # 和 uvicorn 一样：刚关闭的连接（TIME_WAIT）不算占用。Windows 上不设，避免误判
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _redirect_output(data_dir: Path) -> Path | None:
    """隐藏模式（pythonw，没有黑色窗口）：把输出写到 data\\logs\\app-日期.log。"""
    try:
        logs = data_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / f"app-{time.strftime('%Y%m%d')}.log"
        f = open(path, "a", encoding="utf-8", buffering=1, errors="replace")
        f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动（隐藏模式，PID {os.getpid()}） =====\n")
        sys.stdout = sys.stderr = f
        return path
    except Exception:
        return None


def _message_box(text: str, error: bool = True):
    """隐藏模式下出错时弹一个 Windows 提示框（否则用户什么都看不到）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, "MT5 批量终端", 0x10 if error else 0x40)
    except Exception:
        pass


def _open_browser(url: str, delay: float = 1.2):
    def _open():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_open, daemon=True).start()


def stop_running(root: Path, data_dir: Path) -> int:
    """停止后台.bat：让正在运行的本程序正常退出（和网页里点「退出程序」一样），超时再强制结束本程序自己的进程。"""
    from fleet.instance import cleanup_stale, probe, request_exit, running_instance, wait_exit
    inst = running_instance(root, data_dir)
    stopped = False
    if inst:
        tok = probe(inst.get("port", 0))
        if tok and request_exit(inst["port"], tok):
            print(f"已通知程序退出（PID {inst['pid']}），正在断开终端连接……")
            stopped = wait_exit(int(inst["pid"]), 120)
            print("程序已正常退出。" if stopped else "等待超时，改为强制结束。")
    left = cleanup_stale(root, data_dir)
    if left:
        print(f"已结束 {len(left)} 个本程序的后台进程（MT5 终端不受影响）：")
        for x in left:
            print("  " + x)
    if not inst and not left:
        print("后台没有在运行。")
    return 0


def self_test() -> int:
    """在线更新替换文件后，用新文件跑一次：能导入全部模块、网页文件齐全，才算通过。"""
    from fleet import __version__
    import importlib
    for mod in ("fleet.config", "fleet.secrets", "fleet.procs", "fleet.terminal", "fleet.instance", "fleet.retcodes",
                "fleet.worker", "fleet.manager", "fleet.updater", "fleet.server", "fastapi", "uvicorn", "psutil"):
        importlib.import_module(mod)
    from fleet.config import static_dir
    for f in ("index.html", "app.js", "app.css", "desk.js", "quick.js"):
        if not (static_dir() / f).is_file():
            print(f"SELFTEST FAIL 缺少 static/{f}")
            return 1
    print(f"SELFTEST OK {__version__}")
    return 0


def check_update_cli(data_dir: Path) -> int:
    """只读检查：读 data\\settings.json（不写），访问更新源，打印结果。"""
    import json
    from fleet import __version__
    from fleet.config import DEFAULT_SETTINGS
    from fleet import updater
    try:
        s = {**DEFAULT_SETTINGS, **json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))}
    except Exception:
        s = dict(DEFAULT_SETTINGS)
    t0 = time.time()
    r = updater.check(s)
    r.pop("_found", None)
    r["elapsed_ms"] = int((time.time() - t0) * 1000)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    if r.get("ok"):
        print(f"当前 v{__version__}，最新 v{r['latest']}：" + ("有新版本" if r["newer"] else "已经是最新版本"))
    return 0 if r.get("ok") else 2


def main():
    multiprocessing.freeze_support()
    if sys.platform == "win32" and sys.stdout is not None:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    from fleet.config import app_root

    ap = argparse.ArgumentParser(description="MT5 批量终端（多账户）")
    ap.add_argument("--mock", action="store_true", help="仅供自动化测试：使用内置假终端")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    ap.add_argument("--data-dir", default="", help="数据目录（默认：程序目录下 data 或 data_mock）")
    ap.add_argument("--no-cleanup", action="store_true", help="启动时不清理上次未退出的本程序后台")
    ap.add_argument("--hidden", action="store_true", help="隐藏模式：不显示窗口，输出写到 data\\logs\\app-日期.log")
    ap.add_argument("--reuse", action="store_true", help="程序已在运行（同一版本、能正常访问）时只打开网页，不重启")
    ap.add_argument("--stop", action="store_true", help="停止正在运行的本程序（停止后台.bat 用）")
    ap.add_argument("--self-test", action="store_true", help="在线更新用：检查程序文件能否正常加载（不启动、不改动任何数据）")
    ap.add_argument("--check-update", action="store_true", help="检查有没有新版本（只读，不安装）")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    data_dir = Path(args.data_dir) if args.data_dir else app_root() / ("data_mock" if args.mock else "data")
    data_dir = Path(os.path.abspath(data_dir))
    hidden = args.hidden or sys.stdout is None
    log_path = _redirect_output(data_dir) if hidden else None
    if hidden and log_path is None:      # 连日志都写不了：至少不要因为 print 崩溃
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    try:
        return _run(args, data_dir, hidden, log_path)
    except SystemExit:
        raise
    except BaseException as e:
        import traceback
        traceback.print_exc()
        if hidden:
            _message_box(f"MT5 批量终端启动失败：{e}\n\n详细信息在：{log_path or '（无法写日志）'}\n可以双击「调试启动（显示窗口）.bat」查看。")
        raise


def _run(args, data_dir: Path, hidden: bool, log_path):
    from fleet.config import Store, app_root, mock_seed_accounts, terminals_root
    from fleet import __version__
    from fleet.instance import InstanceLock, cleanup_stale, probe, running_instance

    if args.stop:
        return stop_running(app_root(), data_dir)
    if args.check_update:
        return check_update_cli(data_dir)
    if args.reuse:
        inst = running_instance(app_root(), data_dir)
        if inst and inst.get("version") == __version__ and probe(inst.get("port", 0)):
            url = f"http://127.0.0.1:{inst['port']}/"
            print(f"[提示] 程序已经在运行（PID {inst['pid']}），直接打开 {url}")
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        if inst:
            print("[提示] 正在运行的是旧版本或没有响应，重新启动（MT5 终端保持运行）")
    if not args.no_cleanup:
        # 上次的程序没退出干净（窗口被直接关掉、卡住等）：只结束本程序自己的 Python 后台进程，
        # 受管 MT5 终端不会被关闭，新程序启动后会重新接管它们（EA 继续运行）。
        try:
            killed = cleanup_stale(app_root(), data_dir, args.port)
        except Exception as e:
            killed = []
            print(f"[提示] 检查上次的后台时出错：{e}")
        if killed:
            print(f"[提示] 已清理上次未退出的后台（{len(killed)} 个进程），MT5 终端保持运行，稍后自动重新接管", flush=True)
    store = Store(data_dir)
    if args.mock and not store.accounts:
        store.accounts = [store._normalize(a) for a in mock_seed_accounts()]
        store.save_accounts()

    port = args.port
    if not _port_free(port):
        for p in range(port + 1, port + 20):
            if _port_free(p):
                print(f"[提示] 端口 {port} 被占用，改用 {p}")
                port = p
                break

    from fleet.manager import Fleet
    from fleet.server import create_app
    import uvicorn

    terminals_root().mkdir(parents=True, exist_ok=True)
    fleet = Fleet(store, mock=args.mock)
    holder = {}

    def request_exit():
        srv = holder.get("server")
        if srv is not None:
            srv.should_exit = True

    from fleet.updater import Updater

    def before_update_exit():
        # 在线更新重启：保存台账、结束本进程的 MT5 连接工作进程；MT5 终端不关（新版本启动后重新接管）
        try:
            fleet.store.save_accounts()
        except Exception:
            pass
        for w in list(fleet.workers.values()):
            try:
                w["proc"].kill()
            except Exception:
                pass
        lock.release()

    restart_args = (["--mock"] if args.mock else []) + (["--data-dir", str(data_dir)] if args.data_dir else []) + ["--port", str(port)]
    updater = Updater(app_root(), data_dir, lambda: store.settings, before_exit=before_update_exit,
                      restart_args=restart_args, port=port)
    app = create_app(fleet, port, on_exit=request_exit, updater=updater)
    lock = InstanceLock(data_dir)
    lock.write(port, __version__)
    url = f"http://127.0.0.1:{port}/"
    mode = "测试模式（--mock，内置假终端）" if args.mock else "实盘"
    print("=" * 60)
    print(f" MT5 批量终端 v{__version__}  ·  {mode}")
    print(f" 数据目录：{data_dir}")
    print(f" 受管终端目录：{terminals_root()}  （只使用这里面的 MT5，桌面上的 MT5 不受影响）")
    print(f" 打开浏览器访问：{url}")
    print(" 【实盘】批量下单 / 平仓 / 改单 / 部署策略都会真实发送到券商，执行前网页里会弹出确认。")
    if fleet.mt5_import_error:
        print(" [警告] " + fleet.mt5_import_error)
    if hidden:
        print(f" 隐藏模式：没有窗口。退出请在网页左下角点「退出程序」，或双击「停止后台.bat」。日志：{log_path}")
    else:
        print(" 退出：网页里点「退出程序」或在本窗口按 Ctrl+C。")
    print(" 退出时" + ("会关闭本程序启动的终端" if store.settings.get("close_terminals_on_exit") else "不会关闭任何终端")
          + "；桌面上你自己打开的 MT5 永远不会被关闭。")
    print("=" * 60)

    if not args.no_browser:
        _open_browser(url)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    holder["server"] = server

    def _mark_started():
        for _ in range(600):
            if server.started:
                updater.mark_started()
                return
            time.sleep(0.2)
    threading.Thread(target=_mark_started, daemon=True).start()
    if hasattr(signal, "SIGBREAK"):  # Windows：关闭控制台窗口 / Ctrl+Break
        try:
            signal.signal(signal.SIGBREAK, lambda *_: request_exit())
        except Exception:
            pass
    try:
        server.run()
    finally:
        print("正在断开所有终端连接（只会关闭本程序自己启动的终端）……")
        try:
            fleet.shutdown()
        finally:
            lock.release()


if __name__ == "__main__":
    sys.exit(main() or 0)
