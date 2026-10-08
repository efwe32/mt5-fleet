"""打包便携版：生成一个自带 Python 的文件夹 + zip，复制到别的 Windows 电脑（不用装 Python）解压后双击「一键启动」即可。

用法（在程序目录，用已安装好依赖的 Python 3.13 运行；打包需要联网）：
  .venv\\Scripts\\python.exe tools\\build_portable.py
  .venv\\Scripts\\python.exe tools\\build_portable.py --out "D:\\15\\新建文件夹" --template "D:\\15\\MetaTrader 5    4"

生成：
  <out>\\MT5批量终端\\            便携版文件夹（python\\ 自带的 Python、MT5模板\\、程序文件、一键启动.vbs …）
  <out>\\MT5批量终端.zip          同样内容的压缩包

只读取本程序目录、模板 MT5、data\\ea_library 和 data\\servers；不会改动 data\\accounts.json、terminals\\、
正在运行的程序和终端。便携版里不带任何账户和密码。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fleet import __version__  # noqa: E402
from fleet.config import terminals_root  # noqa: E402
from fleet.terminal import PORTABLE_MARKER, find_templates  # noqa: E402

PKG_NAME = "MT5批量终端"
APP_FILES = ["app.py", "requirements.txt", "README.md", "RELEASE_NOTES.md", "一键启动.vbs", "停止后台.bat", "调试启动（显示窗口）.bat", "创建终端副本.bat"]
APP_DIRS = ["fleet", "static", "tools"]
TPL_SKIP_DIRS = {"bases", "logs", "tester"}                       # 历史数据、日志、测试器缓存
TPL_SKIP_TOP_FILES = {"metaeditor64.exe", "uninstall.exe"}         # 代码编辑器 / 卸载程序（跑 EA 不需要）
TPL_SKIP_CONFIG = {"accounts.dat", "common.ini"}                   # 保存的账户 / 上次登录信息
PY_MIRRORS = ["https://www.python.org/ftp/python/{v}/python-{v}-embed-amd64.zip",
              "https://mirrors.huaweicloud.com/python/{v}/python-{v}-embed-amd64.zip",
              "https://registry.npmmirror.com/-/binary/python/{v}/python-{v}-embed-amd64.zip"]
PIP_MIRRORS = [None, "https://pypi.tuna.tsinghua.edu.cn/simple", "https://mirrors.aliyun.com/pypi/simple/"]

USAGE = """MT5 批量终端（便携版） 使用说明
================================

【第一次使用】
1. 把整个文件夹（或 MT5批量终端.zip 解压后的文件夹）放到任意位置，例如 D:\\MT5批量终端
   （路径里有中文、空格都可以；不要在压缩包里直接双击运行）。
2. 双击「一键启动.vbs」。不会出现黑色窗口，几秒后浏览器自动打开操作面板。
   （如果被杀毒软件拦截，选择“允许”。）
3. 面板「账号」页 → 「添加并登录」：填 账号、交易密码、服务器 → 登录。
   每个账户会自动复制一份 MT5（在 terminals 文件夹里），不影响电脑上已有的 MT5。
4. 「策略」页 → 上传 EA（.ex5）→ 「分发」：选账户，默认品种 USDJPYc、周期 M15 → 点分发，
   程序自动把 EA 挂到每个账户并打开算法交易。

【日常】
- 打开：双击「一键启动.vbs」。已经在运行时只会打开网页，不会重复启动。
- 关闭：网页左下角「退出程序」，或双击「停止后台.bat」。
- 出问题想看运行信息：双击「调试启动（显示窗口）.bat」，或看 data\\logs\\app-日期.log。

【说明】
- 需要 Windows 10 / 11 64 位。自带 Python 3.13，不用另外安装 Python。
- 账户和密码不会随文件夹复制：密码用 Windows 加密，只在本电脑当前用户下有效。换电脑后在面板里重新「添加并登录」即可。
- EA 库里已经带上原电脑上传过的 EA，可以直接分发。
- MT5模板 文件夹是程序复制终端用的 MT5，不要删除；第一次登录时 MT5 可能自动升级一次，程序会自动等待并接管。
- 所有批量下单 / 平仓 / 分发都是实盘操作，执行前网页会弹出确认。

【更新】
- 面板「设置 → 在线更新」：点「检查更新」，有新版本时点「立即更新」。不需要登录 GitHub，也不需要重新复制文件夹。
- 只替换程序文件，账户、设置、EA 库、MT5 终端都不动；更新后后台自动重启，MT5 终端和 EA 继续运行，网页自动刷新。
- 替换前自动备份（data\\backups），新版本有问题会自动恢复，也可以点「恢复上一个版本」。
"""


def log(msg: str):
    print(msg, flush=True)


def download_embed(version: str, cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    dst = cache / f"python-{version}-embed-amd64.zip"
    if dst.exists() and zipfile.is_zipfile(dst):
        log(f"  使用已下载的 {dst.name}")
        return dst
    last = None
    for tpl in PY_MIRRORS:
        url = tpl.format(v=version)
        try:
            log(f"  下载 {url}")
            tmp = dst.with_suffix(".part")
            with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
            if zipfile.is_zipfile(tmp):
                os.replace(tmp, dst)
                return dst
            last = "下载的文件不是 zip"
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  失败：{e}")
    raise SystemExit(f"[错误] 下载 Python {version} 嵌入版失败：{last}。可以手动下载 python-{version}-embed-amd64.zip 放到 {cache} 再运行。")


def setup_python(pkg: Path, embed_zip: Path):
    pyd = pkg / "python"
    with zipfile.ZipFile(embed_zip) as z:
        z.extractall(pyd)
    pth = next(pyd.glob("python3*._pth"))
    zipname = next((x.name for x in pyd.glob("python3*.zip")), "")
    # 只用自带的库 + 程序目录（..）+ site-packages；打开 import site
    pth.write_text("\r\n".join([zipname, ".", "..", "Lib\\site-packages", "import site", ""]), encoding="utf-8")
    (pyd / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)
    return pyd


def pip_install(target: Path):
    req = ROOT / "requirements.txt"
    base = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location",
            "--only-binary=:all:", "--upgrade", "--target", str(target), "-r", str(req)]
    for idx in PIP_MIRRORS:
        cmd = base + (["-i", idx] if idx else [])
        log("  " + ("pip 官方源" if not idx else f"pip 镜像 {idx}"))
        if subprocess.run(cmd).returncode == 0:
            shutil.rmtree(target / "bin", ignore_errors=True)    # 命令行小工具指向打包机的 Python，便携版用不到
            return
    raise SystemExit("[错误] 安装依赖失败（检查网络）。")


def copy_app(pkg: Path):
    for f in APP_FILES:
        if (ROOT / f).exists():
            shutil.copy2(ROOT / f, pkg / f)
    for d in APP_DIRS:
        shutil.copytree(ROOT / d, pkg / d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (pkg / "terminals").mkdir(exist_ok=True)
    note = ROOT / "terminals" / "说明.txt"
    if note.exists():
        shutil.copy2(note, pkg / "terminals" / "说明.txt")


def copy_template(src: Path, dst: Path) -> tuple[int, int]:
    n = size = 0
    skipped = []
    for dirpath, dirnames, filenames in os.walk(src):
        rel = Path(dirpath).relative_to(src)
        dirnames[:] = [x for x in dirnames if x.lower() not in TPL_SKIP_DIRS]
        for f in filenames:
            low = f.lower()
            if not rel.parts and (low in TPL_SKIP_TOP_FILES or low.endswith(".lnk")):
                continue
            if rel.parts and rel.parts[0].lower() == "config" and low in TPL_SKIP_CONFIG:
                continue
            if low.startswith("fleet_") and low.endswith(".ini"):
                continue
            (dst / rel).mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(Path(dirpath) / f, dst / rel / f)
                n += 1
                size += (dst / rel / f).stat().st_size
            except OSError as e:     # 模板正在运行时个别文件被占用
                skipped.append(f"{rel / f}：{e}")
    for s in skipped:
        log(f"  跳过被占用的文件 {s}")
    if not (dst / "terminal64.exe").exists():
        raise SystemExit("[错误] 模板 terminal64.exe 复制失败")
    return n, size


def copy_data(pkg: Path):
    data = pkg / "data"
    (data / "ea_library").mkdir(parents=True, exist_ok=True)
    src = ROOT / "data"
    n = 0
    for sub in ("ea_library", "servers"):
        if (src / sub).is_dir():
            for f in (src / sub).iterdir():
                if f.is_file():
                    (data / sub).mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, data / sub / f.name)
                    n += 1
    return n


def make_zip(pkg: Path, zpath: Path) -> int:
    tmp = zpath.with_suffix(".zip.part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for dirpath, dirnames, filenames in os.walk(pkg):
            rel = Path(dirpath).relative_to(pkg)
            if not (rel.parts and rel.parts[0] == "python"):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            if not filenames and not dirnames:
                z.writestr((Path(PKG_NAME) / rel).as_posix() + "/", "")
            for f in filenames:
                z.write(Path(dirpath) / f, (Path(PKG_NAME) / rel / f).as_posix())
    os.replace(tmp, zpath)
    return zpath.stat().st_size


def selftest(pkg: Path, port: int) -> list[str]:
    """用便携版自带的 Python 检查依赖，并在临时数据目录里实际启动一次（隐藏模式）再用 --stop 停掉。
    使用 --no-cleanup 和单独的数据目录，不会影响正在运行的其它实例。"""
    out = []
    py, pyw = pkg / "python" / "python.exe", pkg / "python" / "pythonw.exe"
    r = subprocess.run([str(py), "-c", "import sys, MetaTrader5, numpy, fastapi, uvicorn, psutil, multipart, fleet.server; "
                        "print(sys.version.split()[0], MetaTrader5.__version__, numpy.__version__, fastapi.__version__, uvicorn.__version__, psutil.__version__, "
                        "sys.flags.isolated, sys.flags.no_user_site, sep='|')"],
                       cwd=str(pkg), capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit("[错误] 便携版 Python 导入依赖失败：\n" + r.stderr)
    out.append("依赖导入正常：Python|MetaTrader5|numpy|fastapi|uvicorn|psutil|isolated|no_user_site = " + r.stdout.strip())
    tmp = Path(tempfile.mkdtemp(prefix="mt5fleet-selftest-"))
    try:
        p = subprocess.Popen([str(pyw), str(pkg / "app.py"), "--hidden", "--no-browser", "--no-cleanup", "--port", str(port),
                              "--data-dir", str(tmp)], cwd=str(pkg))
        tok = None
        lock = None
        for _ in range(80):
            time.sleep(0.5)
            try:
                lock = json.loads((tmp / "app.lock").read_text(encoding="utf-8"))
                with urllib.request.urlopen(f"http://127.0.0.1:{lock['port']}/", timeout=3) as resp:
                    html = resp.read().decode("utf-8", "ignore")
                import re
                m = re.search(r'FLEET_TOKEN = "([^"]+)"', html)
                tok = m.group(1) if m else None
                if tok:
                    break
            except Exception:
                continue
        if not tok:
            logtxt = "".join(f.read_text(encoding="utf-8", errors="replace") for f in (tmp / "logs").glob("app-*.log")) if (tmp / "logs").exists() else ""
            p.kill()
            raise SystemExit("[错误] 便携版启动失败：\n" + logtxt[-3000:])
        req = urllib.request.Request(f"http://127.0.0.1:{lock['port']}/api/state", headers={"X-Fleet-Token": tok})
        with urllib.request.urlopen(req, timeout=10) as resp:
            st = json.loads(resp.read().decode("utf-8"))
        out.append(f"隐藏模式启动正常（pythonw，PID {lock['pid']}，端口 {lock['port']}）：实盘={not st.get('mock')}，"
                   f"MetaTrader5 包={'正常' if not st.get('mt5Error') else st.get('mt5Error')}，模板={st.get('template', {}).get('path')}，"
                   f"EA 库={[f.name for f in (pkg / 'data' / 'ea_library').glob('*') if f.is_file()]}")
        logs = list((tmp / "logs").glob("app-*.log"))
        out.append("隐藏模式日志：" + ("已写入 " + logs[0].name if logs else "没有找到"))
        r = subprocess.run([str(py), str(pkg / "app.py"), "--stop", "--data-dir", str(tmp)], cwd=str(pkg),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        try:
            p.wait(timeout=60)
        except subprocess.TimeoutExpired:
            p.kill()
        out.append("停止后台：" + " / ".join(x for x in r.stdout.strip().splitlines() if x.strip()) + f"（进程已退出：{p.poll() is not None}）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def main():
    ap = argparse.ArgumentParser(description="打包 MT5 批量终端便携版（自带 Python）")
    ap.add_argument("--out", default=str(ROOT / "新建文件夹"), help="输出文件夹（默认：程序目录\\新建文件夹）")
    ap.add_argument("--template", default="", help="模板 MT5 目录（默认自动找，例如 MetaTrader 5    4）")
    ap.add_argument("--python-version", default="", help="嵌入版 Python 版本（默认和当前 Python 相同）")
    ap.add_argument("--no-zip", action="store_true")
    ap.add_argument("--no-selftest", action="store_true")
    ap.add_argument("--selftest-port", type=int, default=18765)
    args = ap.parse_args()
    if sys.platform != "win32":
        raise SystemExit("只能在 Windows 上打包（MetaTrader5 只有 Windows 版）")
    ver = args.python_version or "%d.%d.%d" % sys.version_info[:3]
    if ver.rsplit(".", 1)[0] != "%d.%d" % sys.version_info[:2]:
        raise SystemExit(f"嵌入版 Python {ver} 和当前 Python {sys.version.split()[0]} 主版本不同，依赖装不上。请用 Python {ver.rsplit('.', 1)[0]} 运行本脚本。")
    out = Path(args.out).resolve()
    pkg = out / PKG_NAME
    stage = out / (PKG_NAME + ".building")
    tpl = Path(args.template) if args.template else next(iter(find_templates(ROOT, terminals_root())), None)
    if not tpl or not (Path(tpl) / "terminal64.exe").exists():
        raise SystemExit("[错误] 没找到模板 MT5（有 terminal64.exe 的文件夹），请用 --template 指定")
    tpl = Path(tpl)
    # 已经在运行的便携版不能覆盖
    if pkg.exists():
        from fleet.instance import running_instance
        if running_instance(pkg, pkg / "data"):
            raise SystemExit(f"[错误] {pkg} 里的程序正在运行，请先双击它的「停止后台.bat」再打包。")
    log(f"MT5 批量终端 v{__version__} 打包便携版")
    log(f"  输出：{pkg}")
    log(f"  模板 MT5：{tpl}")
    log(f"  Python：{ver}（嵌入版 64 位）")
    t0 = time.time()
    out.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    (stage / PORTABLE_MARKER).write_text(f"MT5 批量终端便携版 v{__version__}，生成于 {time.strftime('%Y-%m-%d %H:%M')}\n", encoding="utf-8")
    log("[1/6] 准备 Python 嵌入版")
    setup_python(stage, download_embed(ver, ROOT / "build" / "portable-cache"))
    log("[2/6] 安装依赖到 python\\Lib\\site-packages")
    pip_install(stage / "python" / "Lib" / "site-packages")
    log("[3/6] 复制程序文件")
    copy_app(stage)
    (stage / "使用说明.txt").write_bytes(b"\xef\xbb\xbf" + USAGE.replace("\n", "\r\n").encode("utf-8"))
    log("[4/6] 复制模板 MT5 → MT5模板（不含历史数据、日志、账户信息）")
    n, size = copy_template(tpl, stage / "MT5模板")
    log(f"  {n} 个文件，{size / 1048576:.1f} MB")
    log("[5/6] 准备 data（EA 库、服务器列表；不含账户和密码）")
    log(f"  {copy_data(stage)} 个文件")
    if pkg.exists():
        old = out / f"{PKG_NAME}.old-{time.strftime('%Y%m%d-%H%M%S')}"
        os.replace(pkg, old)
        shutil.rmtree(old, ignore_errors=True)
    os.replace(stage, pkg)
    lines = []
    if not args.no_selftest:
        log("[自检] 用便携版自带的 Python 启动一次（临时数据目录、不清理其它进程）")
        lines = selftest(pkg, args.selftest_port)
        for x in lines:
            log("  " + x)
        for d in [d for d in pkg.rglob("__pycache__") if "python" not in d.relative_to(pkg).parts[:1]]:
            shutil.rmtree(d, ignore_errors=True)
    if not args.no_zip:
        log("[6/6] 生成压缩包")
        zsize = make_zip(pkg, out / f"{PKG_NAME}.zip")
        log(f"  {out / (PKG_NAME + '.zip')}：{zsize / 1048576:.1f} MB")
    total = sum(f.stat().st_size for f in pkg.rglob("*") if f.is_file())
    log(f"完成：{pkg}（{total / 1048576:.1f} MB），用时 {time.time() - t0:.0f} 秒")
    log("把整个文件夹或 zip 复制到别的电脑，解压后双击「一键启动.vbs」即可。")


if __name__ == "__main__":
    main()
