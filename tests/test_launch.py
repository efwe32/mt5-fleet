"""一键启动（隐藏模式）/ 已在运行时只打开网页 / 停止后台 的测试（--mock，临时数据目录）。
用法：python tests/test_launch.py [端口]
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
from fleet import __version__  # noqa: E402
from fleet.instance import probe  # noqa: E402

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8897
tmp = Path(tempfile.mkdtemp(prefix="fleet-launch-"))
data = tmp / "data"
fails = []
env = {**os.environ, "FLEET_TERMINALS_ROOT": str(tmp / "terminals"), "FLEET_MOCK_AUTOTRADE": "0"}


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {info}" if info else ""))
    if not cond:
        fails.append(name)


def run(*args, timeout=120):
    return subprocess.run([sys.executable, str(APP / "app.py"), "--mock", "--no-browser", "--data-dir", str(data), *args],
                          cwd=str(APP), env=env, capture_output=True, text=True, timeout=timeout)


def start(*args):
    return subprocess.Popen([sys.executable, str(APP / "app.py"), "--mock", "--no-browser", "--data-dir", str(data), "--port", str(port), *args],
                            cwd=str(APP), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_lock(pid=None, sec=40):
    end = time.time() + sec
    while time.time() < end:
        try:
            lk = json.loads((data / "app.lock").read_text(encoding="utf-8"))
            if (pid is None or lk["pid"] == pid) and probe(lk["port"]):
                return lk
        except Exception:
            pass
        time.sleep(0.4)
    return None


procs = []
try:
    p1 = start("--hidden")
    procs.append(p1)
    lk = wait_lock(p1.pid)
    check("隐藏模式启动，网页可以访问", lk is not None and lk.get("version") == __version__, lk)
    logs = list((data / "logs").glob("app-*.log"))
    txt = logs[0].read_text(encoding="utf-8") if logs else ""
    check("隐藏模式的输出写到 data/logs/app-日期.log", "隐藏模式" in txt and "打开浏览器访问" in txt, txt[-200:])

    t0 = time.time()
    r = run("--reuse", "--port", str(port), timeout=30)
    check("再次一键启动：程序在运行时只打开网页、马上返回", r.returncode == 0 and "已经在运行" in r.stdout and time.time() - t0 < 15,
          (r.returncode, r.stdout[-200:], r.stderr[-300:]))
    check("原来的程序没被重启", p1.poll() is None and wait_lock(p1.pid, 3) is not None)

    # 旧版本还在运行：一键启动要换成新版本（旧的被清理，终端保持）
    lkd = json.loads((data / "app.lock").read_text(encoding="utf-8"))
    lkd["version"] = "0.9.0"
    (data / "app.lock").write_text(json.dumps(lkd), encoding="utf-8")
    p2 = start("--hidden", "--reuse")
    procs.append(p2)
    lk2 = wait_lock(p2.pid, 60)
    try:
        p1.wait(timeout=20)
    except subprocess.TimeoutExpired:
        pass
    check("运行的是旧版本：自动清理旧后台并启动新版本", lk2 is not None and p1.poll() is not None and p2.poll() is None, (lk2, p1.poll()))
    txt = "".join(f.read_text(encoding="utf-8") for f in (data / "logs").glob("app-*.log"))
    check("日志里有“旧版本 → 重新启动”和“已清理上次未退出的后台”", "旧版本" in txt and "已清理上次未退出的后台" in txt)

    # 停止后台
    workers_before = [c.pid for c in psutil.Process(p2.pid).children(recursive=True)]
    r = run("--stop", timeout=180)
    try:
        p2.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass
    check("停止后台：正常退出", "正常退出" in r.stdout and p2.poll() is not None, (r.stdout[-300:], r.stderr[-300:]))
    time.sleep(1)
    left = [pid for pid in workers_before if psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE]
    check("停止后台：工作进程也都结束了", not left, left)
    check("停止后台：运行锁已删除", not (data / "app.lock").exists())
    r = run("--stop", timeout=60)
    check("没有在运行时停止后台给出提示", "没有在运行" in r.stdout, r.stdout[-200:])

    # 卡死的后台（网页没响应）：停止后台直接结束本程序进程
    p3 = start()
    procs.append(p3)
    lk3 = wait_lock(p3.pid, 40)
    psutil.Process(p3.pid).suspend()
    r = run("--stop", timeout=180)
    time.sleep(0.5)
    check("没响应的后台：停止后台强制结束本程序进程", p3.poll() is not None and "已结束" in r.stdout, r.stdout[-300:])
finally:
    for p in procs:
        if p.poll() is None:
            try:
                psutil.Process(p.pid).resume()
            except Exception:
                pass
            p.kill()
    shutil.rmtree(tmp, ignore_errors=True)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
sys.exit(1 if fails else 0)
