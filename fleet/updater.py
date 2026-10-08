"""在线更新：检查新版本、下载、校验、备份、只替换程序文件、重启后台（MT5 终端和 EA 继续运行）。

更新源（按顺序尝试）：
  1. GitHub 公开仓库的最新发布（不需要登录 GitHub）：
     https://api.github.com/repos/<仓库>/releases/latest → 资源 mt5-fleet-manifest.json + mt5-fleet-app-<版本>.zip
     github.com 下载慢或打不开时，压缩包（和清单）改走加速镜像；内容用 GitHub 公布的 sha256 校验，镜像改不了内容。
  2. 设置里的「自定义更新地址」：一个 latest.json（和 make_release.py 生成的清单同格式）的直链，
     里面的 zip_url 可以是相对地址（和 latest.json 放在同一个目录即可，例如网盘 / 自己的服务器）。

只会改动程序文件：app.py、README.md、requirements.txt、根目录的 .bat / .vbs、fleet\\、static\\、tools\\。
永远不碰：data\\（账户、设置、日志、EA 库……）、terminals\\、MT5 模板、python\\、.venv\\。
替换前把当前程序文件备份到 data\\backups\\<版本>-<时间>\\；新版本启动失败会自动恢复备份并重新启动旧版本。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import __version__

DEFAULT_REPO = "efwe32/mt5-fleet"
MANIFEST_ASSET = "mt5-fleet-manifest.json"
APP_NAME = "mt5-fleet"
# github.com 下载加速镜像（只用于下载；内容用 GitHub API 公布的 sha256 校验）
MIRRORS = ["https://ghfast.top/", "https://gh-proxy.com/"]
GITHUB_HOSTS = {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com",
                "raw.githubusercontent.com", "codeload.github.com"}
APP_DIRS = ("fleet", "static", "tools")
APP_ROOT_FILES = ("app.py", "README.md", "requirements.txt", "RELEASE_NOTES.md")
# 开发用的脚本：本机原来没有就不装（便携版里本来就没有这几个）
OPTIONAL_ROOT = {"安装依赖.bat", "打包便携版.bat", "build_exe.bat"}
NEVER = {"data", "data_mock", "terminals", "python", ".venv", "venv", "build", "mt5模板", "新建文件夹", ".git", "tests"}
MAX_ZIP = 50 * 1024 * 1024
CHECK_TTL = 1800
KEEP_BACKUPS = 5
GITHUB_API = os.environ.get("FLEET_GITHUB_API", "https://api.github.com")   # 环境变量只供自动化测试使用
RESTART_TIMEOUT = float(os.environ.get("FLEET_UPDATE_TIMEOUT", "90"))


# ---------------- 小工具 ----------------
def vtuple(v: str) -> tuple:
    out = []
    for p in re.split(r"[.\-+]", str(v or "").strip().lstrip("vV")):
        out.append(int(p) if p.isdigit() else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:4])


def newer(a: str, b: str) -> bool:
    """a 比 b 新？"""
    return vtuple(a) > vtuple(b)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def is_app_path(rel: str) -> bool:
    """压缩包 / 清单里的路径是不是允许替换的程序文件。"""
    if not rel or "\\" in rel or rel.startswith("/") or ":" in rel:
        return False
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    if parts[0].lower() in NEVER or "__pycache__" in parts:
        return False
    if len(parts) == 1:
        low = parts[0].lower()
        return parts[0] in APP_ROOT_FILES or low.endswith(".bat") or low.endswith(".vbs")
    return parts[0] in APP_DIRS


def current_app_files(root: Path) -> list[str]:
    """本机现在的程序文件（相对路径，/ 分隔）。"""
    out = []
    for f in sorted(root.iterdir()):
        if f.is_file() and is_app_path(f.name):
            out.append(f.name)
    for d in APP_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(x for x in dirnames if x != "__pycache__")
            for fn in sorted(filenames):
                if fn.endswith((".pyc", ".pyo")):
                    continue
                rel = Path(dirpath, fn).relative_to(root).as_posix()
                if is_app_path(rel):
                    out.append(rel)
    return out


def _req_norm(text: str) -> list[str]:
    return sorted(x.split("#", 1)[0].strip().replace(" ", "") for x in text.splitlines() if x.split("#", 1)[0].strip())


def layout(root: Path) -> dict:
    """便携版（python\\）还是源码版（.venv\\）。"""
    if (root / "python" / "python.exe").exists() or (root / "python" / "pythonw.exe").exists():
        return {"kind": "便携版", "python": root / "python" / "python.exe", "pythonw": root / "python" / "pythonw.exe"}
    if (root / ".venv" / "Scripts" / "python.exe").exists():
        return {"kind": "源码版", "python": root / ".venv" / "Scripts" / "python.exe",
                "pythonw": root / ".venv" / "Scripts" / "pythonw.exe"}
    exe = Path(sys.executable)
    py = exe.with_name(exe.name.replace("pythonw", "python"))
    pyw = exe.with_name(exe.name.replace("pythonw", "python").replace("python", "pythonw", 1)) if os.name == "nt" else exe
    return {"kind": "其它", "python": py if py.exists() else exe, "pythonw": pyw if pyw.exists() else exe}


# ---------------- 网络 ----------------
def _req(url: str, timeout: float, accept: str = "") -> urllib.request.Request:
    h = {"User-Agent": f"MT5Fleet-Updater/{__version__}"}
    if accept:
        h["Accept"] = accept
    return urllib.request.Request(url, headers=h)


def http_get(url: str, timeout: float = 15, limit: int = 2 * 1024 * 1024, accept: str = "") -> bytes:
    with urllib.request.urlopen(_req(url, timeout, accept), timeout=timeout) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("内容太大")
    return data


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def with_mirrors(url: str, use_mirror: bool) -> list[str]:
    urls = [url]
    if use_mirror and _host(url) in GITHUB_HOSTS:
        urls += [m + url for m in MIRRORS]
    return urls


def _err(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 404:
            return "404（还没有发布版本，或地址不对）"
        if e.code == 403:
            return "403（GitHub 访问次数限制，过一会儿再试）"
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"连不上（{e.reason}）"
    if isinstance(e, TimeoutError) or "timed out" in str(e):
        return "超时"
    return str(e) or e.__class__.__name__


def _short(url: str) -> str:
    h = _host(url)
    return h + ("（镜像）" if any(url.startswith(m) for m in MIRRORS) else "")


# ---------------- 检查更新 ----------------
def _check_manifest(m: dict) -> dict:
    if not isinstance(m, dict) or m.get("name", APP_NAME) != APP_NAME:
        raise ValueError("清单不是本程序的")
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(m.get("version", ""))):
        raise ValueError("清单里的版本号不对")
    if not re.fullmatch(r"[0-9a-f]{64}", str(m.get("sha256", ""))):
        raise ValueError("清单里缺少 sha256")
    files = m.get("files")
    if not isinstance(files, dict) or "app.py" not in files or "fleet/__init__.py" not in files:
        raise ValueError("清单里缺少文件列表")
    bad = [p for p in files if not is_app_path(p)]
    if bad:
        raise ValueError(f"清单里有不允许替换的文件：{bad[:3]}")
    return m


def from_github(repo: str, use_mirror: bool = True, timeout: float = 12) -> dict:
    t0 = time.time()
    api = f"{GITHUB_API}/repos/{repo}/releases/latest"
    rel = json.loads(http_get(api, timeout, accept="application/vnd.github+json").decode("utf-8"))
    api_ms = int((time.time() - t0) * 1000)
    assets = {a.get("name"): a for a in rel.get("assets") or []}
    ma = assets.get(MANIFEST_ASSET)
    if not ma:
        raise ValueError(f"最新发布（{rel.get('tag_name')}）里没有 {MANIFEST_ASSET}")
    mdig = str(ma.get("digest") or "")
    mdig = mdig.split(":", 1)[1] if mdig.startswith("sha256:") else ""
    # 清单：没有 GitHub 公布的 sha256 时只从 github.com 直接下载（不走镜像）
    errs, raw, used = [], None, ""
    for u in with_mirrors(ma["browser_download_url"], use_mirror and bool(mdig)):
        try:
            b = http_get(u, timeout)
            if mdig and sha256_bytes(b) != mdig:
                raise ValueError("清单校验不一致")
            raw, used = b, u
            break
        except Exception as e:
            errs.append(f"{_short(u)}：{_err(e)}")
    if raw is None:
        raise ValueError("下载清单失败：" + "；".join(errs))
    m = _check_manifest(json.loads(raw.decode("utf-8")))
    za = assets.get(m.get("asset"))
    if not za:
        raise ValueError(f"发布里缺少压缩包 {m.get('asset')}")
    zdig = str(za.get("digest") or "")
    if zdig.startswith("sha256:") and zdig.split(":", 1)[1] != m["sha256"]:
        raise ValueError("清单和 GitHub 公布的压缩包 sha256 不一致，已停止")
    return {"manifest": m, "zip_urls": with_mirrors(za["browser_download_url"], use_mirror),
            "source": f"GitHub {repo}", "release_url": rel.get("html_url", ""),
            "published": rel.get("published_at", ""), "api_ms": api_ms, "manifest_via": _short(used)}


def from_custom(url: str, use_mirror: bool = True, timeout: float = 15) -> dict:
    t0 = time.time()
    m = _check_manifest(json.loads(http_get(url, timeout).decode("utf-8-sig")))
    zu = urllib.parse.urljoin(url, str(m.get("zip_url") or m.get("asset") or ""))
    if not zu.lower().startswith(("http://", "https://")):
        raise ValueError("清单里的 zip_url 不对")
    return {"manifest": m, "zip_urls": with_mirrors(zu, use_mirror), "source": "自定义地址",
            "release_url": "", "published": m.get("released", ""), "api_ms": int((time.time() - t0) * 1000),
            "manifest_via": _host(url)}


def check(settings: dict, timeout: float = 12) -> dict:
    """返回 {current, latest, newer, notes, source, errors…}；网络失败时 ok=False。"""
    repo = (settings.get("update_repo") or DEFAULT_REPO).strip()
    custom = (settings.get("update_url") or "").strip()
    mirror = settings.get("update_mirror", True) is not False
    errors, found = [], None
    if repo:
        try:
            found = from_github(repo, mirror, timeout)
        except Exception as e:
            errors.append(f"GitHub：{_err(e) if not isinstance(e, ValueError) else e}")
    if found is None and custom:
        try:
            found = from_custom(custom, mirror, timeout)
        except Exception as e:
            errors.append(f"自定义地址：{_err(e) if not isinstance(e, ValueError) else e}")
    out = {"ok": found is not None, "current": __version__, "checked_at": time.time(), "errors": errors,
           "repo": repo, "custom": custom}
    if found:
        m = found["manifest"]
        out.update({"latest": m["version"], "newer": newer(m["version"], __version__), "notes": m.get("notes", ""),
                    "released": m.get("released", ""), "size": m.get("size", 0), "source": found["source"],
                    "release_url": found["release_url"], "api_ms": found["api_ms"], "manifest_via": found["manifest_via"],
                    "requirements_changed": None})
        out["_found"] = found
    elif not errors:
        out["errors"] = ["没有设置更新源"]
    return out


# ---------------- 安装 ----------------
class UpdateError(Exception):
    pass


class Updater:
    """一次只允许一个更新任务；状态给网页轮询。"""

    def __init__(self, root: Path, data_dir: Path, settings_getter, busy_check=None, before_exit=None,
                 restart_args: list[str] | None = None, port: int = 8765):
        self.root = Path(root)
        self.data_dir = Path(data_dir)
        self.settings = settings_getter
        self.busy_check = busy_check or (lambda: "")
        self.before_exit = before_exit or (lambda: None)
        self.restart_args = list(restart_args or [])
        self.port = port
        self.lock = threading.Lock()
        self.last_check: dict | None = None
        self.status = {"state": "idle", "message": "", "progress": 0, "log": []}
        self.exit_fn = lambda: os._exit(0)          # 测试可以替换
        self.updates_dir = self.data_dir / "updates"
        self.backups_dir = self.data_dir / "backups"

    # ---- 状态 ----
    def _set(self, state=None, message=None, progress=None):
        if state is not None:
            self.status["state"] = state
        if message is not None:
            self.status["message"] = message
            self.status["log"].append(f"{time.strftime('%H:%M:%S')} {message}")
            self.status["log"] = self.status["log"][-60:]
            print(f"[更新] {message}", flush=True)
        if progress is not None:
            self.status["progress"] = progress

    def last_update(self) -> dict:
        try:
            return json.loads((self.updates_dir / "last_update.json").read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_last(self, info: dict):
        self.updates_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.updates_dir / "last_update.json.tmp"
        tmp.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.updates_dir / "last_update.json")

    def mark_started(self):
        """新版本启动后调用：确认更新成功。"""
        lu = self.last_update()
        if lu.get("status") == "restarting" and lu.get("to") == __version__:
            lu.update(status="ok", finished_at=time.time())
            self._write_last(lu)

    def backups(self) -> list[dict]:
        out = []
        if self.backups_dir.is_dir():
            for d in sorted(self.backups_dir.iterdir(), key=lambda p: p.name, reverse=True):
                try:
                    info = json.loads((d / "backup_info.json").read_text(encoding="utf-8"))
                    out.append({"name": d.name, "version": info.get("version"), "at": info.get("at"),
                                "files": len(info.get("files") or [])})
                except Exception:
                    continue
        return out

    def public_status(self) -> dict:
        st = dict(self.status)
        st["current"] = __version__
        st["layout"] = layout(self.root)["kind"]
        st["last_update"] = self.last_update()
        st["backups"] = self.backups()[:KEEP_BACKUPS]
        if self.last_check:
            st["check"] = {k: v for k, v in self.last_check.items() if not k.startswith("_")}
        return st

    # ---- 检查 ----
    def check(self, force: bool = False) -> dict:
        lc = self.last_check
        if not force and lc and lc.get("ok") and time.time() - lc.get("checked_at", 0) < CHECK_TTL:
            return {k: v for k, v in lc.items() if not k.startswith("_")}
        res = check(self.settings())
        if res.get("ok"):
            m = res["_found"]["manifest"]
            try:
                cur = (self.root / "requirements.txt").read_text(encoding="utf-8")
                if isinstance(m.get("requirements"), list):
                    res["requirements_changed"] = _req_norm(cur) != sorted(m["requirements"])
            except Exception:
                res["requirements_changed"] = None
        self.last_check = res
        return {k: v for k, v in res.items() if not k.startswith("_")}

    # ---- 安装 ----
    def start_install(self) -> dict:
        if self.status["state"] in ("checking", "downloading", "verifying", "deps", "backup", "installing", "testing", "restarting"):
            raise UpdateError("更新正在进行")
        why = self.busy_check()
        if why:
            raise UpdateError(why)
        self.status = {"state": "checking", "message": "", "progress": 0, "log": []}
        threading.Thread(target=self._install_thread, daemon=True).start()
        return self.public_status()

    def start_rollback(self, name: str = "") -> dict:
        if self.status["state"] not in ("idle", "done", "error", "uptodate"):
            raise UpdateError("更新正在进行")
        why = self.busy_check()
        if why:
            raise UpdateError(why)
        bks = self.backups()
        b = next((x for x in bks if x["name"] == name), None) if name else (bks[0] if bks else None)
        if not b:
            raise UpdateError("没有可恢复的备份")
        self.status = {"state": "installing", "message": "", "progress": 0, "log": []}
        threading.Thread(target=self._rollback_thread, args=(b["name"],), daemon=True).start()
        return self.public_status()

    def _install_thread(self):
        try:
            self._set("checking", "检查最新版本…", 2)
            res = check(self.settings())
            if not res.get("ok"):
                raise UpdateError("检查更新失败：" + "；".join(res.get("errors") or []))
            self.last_check = res
            found = res["_found"]
            m = found["manifest"]
            if not newer(m["version"], __version__):
                self._set("uptodate", f"已经是最新版本 v{__version__}", 100)
                return
            self.install(found)
        except UpdateError as e:
            self._set("error", str(e))
        except Exception as e:  # pragma: no cover
            import traceback
            traceback.print_exc()
            self._set("error", f"更新出错：{e}")

    def _download(self, urls: list[str], m: dict) -> bytes:
        errs = []
        size = int(m.get("size") or 0)
        for u in urls:
            try:
                self._set("downloading", f"下载 v{m['version']}（{_short(u)}）…", 5)
                t0 = time.time()
                with urllib.request.urlopen(_req(u, 30), timeout=30) as r:
                    total = int(r.headers.get("Content-Length") or size or 0)
                    buf = bytearray()
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        buf += chunk
                        if len(buf) > MAX_ZIP:
                            raise UpdateError("压缩包太大")
                        if total:
                            self._set(progress=5 + int(55 * min(1.0, len(buf) / total)))
                data = bytes(buf)
                if size and len(data) != size:
                    raise ValueError(f"大小不对（{len(data)} ≠ {size}）")
                if sha256_bytes(data) != m["sha256"]:
                    raise ValueError("sha256 校验不一致")
                dt = max(0.001, time.time() - t0)
                self._set(message=f"下载完成 {len(data) / 1024:.0f} KB，用时 {dt:.1f} 秒，sha256 校验通过", progress=60)
                return data
            except UpdateError:
                raise
            except Exception as e:
                errs.append(f"{_short(u)}：{_err(e)}")
                self._set(message=f"  {_short(u)} 失败：{_err(e)}")
        raise UpdateError("下载失败：" + "；".join(errs))

    def _extract(self, data: bytes, m: dict) -> Path:
        files: dict = m["files"]
        stage = self.updates_dir / f"staging-{m['version']}"
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        import io
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            extra = [n for n in names if n not in files and n != MANIFEST_ASSET]
            if extra:
                raise UpdateError(f"压缩包里有清单以外的文件：{extra[:3]}")
            missing = [p for p in files if p not in names]
            if missing:
                raise UpdateError(f"压缩包缺少文件：{missing[:3]}")
            for rel, h in files.items():
                if not is_app_path(rel):
                    raise UpdateError(f"不允许替换的路径：{rel}")
                b = z.read(rel)
                if sha256_bytes(b) != h:
                    raise UpdateError(f"文件校验不一致：{rel}")
                dst = stage / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(b)
        ver = re.search(r'__version__\s*=\s*"([^"]+)"', (stage / "fleet" / "__init__.py").read_text(encoding="utf-8"))
        if not ver or ver.group(1) != m["version"]:
            raise UpdateError("压缩包里的版本号和清单不一致")
        for p in stage.rglob("*.py"):
            try:
                compile(p.read_text(encoding="utf-8"), str(p), "exec")
            except SyntaxError as e:
                raise UpdateError(f"新版本文件有语法错误：{p.relative_to(stage)}：{e}")
        return stage

    def _pip(self, stage: Path) -> str:
        """requirements.txt 有变化时安装依赖；返回说明。失败抛 UpdateError（还没替换任何文件）。"""
        new = (stage / "requirements.txt").read_text(encoding="utf-8") if (stage / "requirements.txt").exists() else ""
        try:
            cur = (self.root / "requirements.txt").read_text(encoding="utf-8")
        except Exception:
            cur = ""
        if _req_norm(new) == _req_norm(cur):
            return "依赖没有变化，跳过安装"
        lay = layout(self.root)
        py = str(lay["python"])
        self._set("deps", f"依赖有变化，正在用本机 Python 安装（{lay['kind']}）…", 64)
        flags = 0x08000000 if os.name == "nt" else 0       # CREATE_NO_WINDOW
        if subprocess.run([py, "-m", "pip", "--version"], capture_output=True, creationflags=flags).returncode != 0:
            if lay["kind"] == "便携版":
                cmd0 = None
            else:
                raise UpdateError("新版本需要安装新的依赖，但本机 Python 没有 pip。请双击「安装依赖.bat」后再更新。")
        else:
            cmd0 = [py, "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location", "-r", str(stage / "requirements.txt")]
        if cmd0 is None:
            # 便携版的嵌入式 Python 没有 pip：用打包时的方式不可行，明确提示
            raise UpdateError("新版本需要新的依赖，便携版自带的 Python 没有 pip，无法在线安装。请下载新的完整便携版（MT5批量终端.zip）。")
        last = ""
        for idx in (None, "https://pypi.tuna.tsinghua.edu.cn/simple", "https://mirrors.aliyun.com/pypi/simple/"):
            cmd = cmd0 + (["-i", idx] if idx else [])
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               creationflags=flags, timeout=900)
            if r.returncode == 0:
                return "依赖安装完成" + (f"（镜像 {idx}）" if idx else "")
            last = (r.stderr or r.stdout or "")[-400:]
        raise UpdateError("安装新依赖失败（程序文件没有改动）：" + last)

    def backup(self) -> Path:
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        dst = self.backups_dir / f"{__version__}-{time.strftime('%Y%m%d-%H%M%S')}"
        n = 1
        while dst.exists():
            n += 1
            dst = self.backups_dir / f"{__version__}-{time.strftime('%Y%m%d-%H%M%S')}-{n}"
        files = current_app_files(self.root)
        for rel in files:
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.root / rel, dst / rel)
        (dst / "backup_info.json").write_text(json.dumps({"version": __version__, "at": time.time(), "files": files},
                                                         ensure_ascii=False, indent=1), encoding="utf-8")
        # 只保留最近几份备份（只删 data\backups 里本程序自己建的备份文件夹）
        olds = sorted([d for d in self.backups_dir.iterdir() if d.is_dir() and (d / "backup_info.json").exists()],
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for d in olds[KEEP_BACKUPS:]:
            shutil.rmtree(d, ignore_errors=True)
        return dst

    def _apply(self, src: Path, new_files: list[str], old_files: list[str]):
        """把 src 里的 new_files 复制到程序目录，删掉旧版本有、新版本没有的程序文件。"""
        for rel in new_files:
            if not is_app_path(rel):
                continue
            if rel in OPTIONAL_ROOT and not (self.root / rel).exists() and rel not in old_files:
                continue
            dst = self.root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".upd-tmp")
            shutil.copy2(src / rel, tmp)
            _replace_retry(tmp, dst)
        keep = set(new_files)
        for rel in old_files:
            if rel not in keep and is_app_path(rel) and "/" in rel:     # 根目录的启动脚本不删
                try:
                    (self.root / rel).unlink()
                except FileNotFoundError:
                    pass
        for d in APP_DIRS:
            pc = self.root / d / "__pycache__"
            shutil.rmtree(pc, ignore_errors=True)

    def restore(self, bdir: Path) -> int:
        info = json.loads((bdir / "backup_info.json").read_text(encoding="utf-8"))
        files = info.get("files") or []
        self._apply(bdir, files, current_app_files(self.root))
        return len(files)

    def _selftest(self, expect: str) -> tuple[bool, str]:
        lay = layout(self.root)
        flags = 0x08000000 if os.name == "nt" else 0
        try:
            r = subprocess.run([str(lay["python"]), str(self.root / "app.py"), "--self-test"], cwd=str(self.root),
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                               creationflags=flags)
        except Exception as e:
            return False, str(e)
        out = (r.stdout or "") + (r.stderr or "")
        ok = r.returncode == 0 and f"SELFTEST OK {expect}" in out
        lines = [x for x in out.strip().splitlines() if x.strip()]
        return ok, " | ".join(lines[-3:])[-600:]

    def install(self, found: dict, restart: bool = True):
        m = found["manifest"]
        data = self._download(found["zip_urls"], m)
        self._set("verifying", "解压并逐个校验文件…", 62)
        stage = self._extract(data, m)
        self._set(message=f"校验通过：{len(m['files'])} 个文件，版本 v{m['version']}")
        self._set(message=self._pip(stage), progress=70)
        why = self.busy_check()
        if why:
            raise UpdateError(why)
        self._set("backup", "备份当前程序文件…", 74)
        old_files = current_app_files(self.root)
        bdir = self.backup()
        self._set(message=f"已备份到 data\\backups\\{bdir.name}（{len(old_files)} 个文件）")
        self._set("installing", "替换程序文件（data、terminals、MT5、Python 不动）…", 80)
        try:
            self._apply(stage, list(m["files"]), old_files)
        except Exception as e:
            self.restore(bdir)
            raise UpdateError(f"替换文件失败，已恢复原来的文件：{e}")
        self._set("testing", "检查新版本能否正常启动…", 88)
        ok, out = self._selftest(m["version"])
        if not ok:
            n = self.restore(bdir)
            raise UpdateError(f"新版本自检失败，已恢复 v{__version__}（{n} 个文件）。详情：{out}")
        shutil.rmtree(stage, ignore_errors=True)
        info = {"from": __version__, "to": m["version"], "backup": bdir.name, "at": time.time(),
                "source": found.get("source", ""), "status": "restarting"}
        self._write_last(info)
        if restart:
            self._set("restarting", f"已安装 v{m['version']}，正在重启后台（MT5 终端和 EA 继续运行）…", 95)
            self.restart(m["version"], bdir.name)

    def _rollback_thread(self, name: str):
        try:
            bdir = self.backups_dir / name
            info = json.loads((bdir / "backup_info.json").read_text(encoding="utf-8"))
            self._set("installing", f"恢复备份 {name}（v{info.get('version')}）…", 30)
            cur_bdir = self.backup()
            self._set(message=f"当前版本先备份到 data\\backups\\{cur_bdir.name}")
            n = self.restore(bdir)
            ok, out = self._selftest(str(info.get("version")))
            if not ok:
                self.restore(cur_bdir)
                raise UpdateError(f"恢复后的版本自检失败，已还原：{out}")
            self._set(message=f"已恢复 {n} 个文件")
            self._write_last({"from": __version__, "to": info.get("version"), "backup": cur_bdir.name, "at": time.time(),
                              "source": "恢复备份", "status": "restarting"})
            self._set("restarting", f"已恢复到 v{info.get('version')}，正在重启后台…", 95)
            self.restart(str(info.get("version")), cur_bdir.name)
        except UpdateError as e:
            self._set("error", str(e))
        except Exception as e:  # pragma: no cover
            self._set("error", f"恢复出错：{e}")

    # ---- 重启 ----
    def restart(self, target: str, backup_name: str):
        """启动一个独立的小助手：等本进程退出 → 启动新版本 → 新版本 90 秒内没起来就恢复备份、重新启动旧版本。"""
        self.updates_dir.mkdir(parents=True, exist_ok=True)
        helper = self.updates_dir / "restart_helper.py"
        helper.write_text(HELPER_SRC, encoding="utf-8")
        lay = layout(self.root)
        job = {"root": str(self.root), "data_dir": str(self.data_dir), "pid": os.getpid(),
               "create_time": _create_time(), "target": target, "old": __version__, "backup": backup_name,
               "pythonw": str(lay["pythonw"]), "args": self.restart_args, "port": self.port, "timeout": RESTART_TIMEOUT}
        jf = self.updates_dir / "restart_job.json"
        jf.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        hp = layout(self.root)["pythonw"]
        hexe = str(hp) if Path(hp).exists() else sys.executable
        spawn_detached([hexe, str(helper), str(jf)], cwd=str(self.root))

        def _later():
            time.sleep(1.5)                 # 让网页先收到“正在重启”
            try:
                self.before_exit()          # 保存台账、结束本进程的工作进程（MT5 终端不关）
            except Exception as e:
                print("[更新] 退出前处理出错", e, flush=True)
            print("[更新] 后台退出，交给新版本接管终端", flush=True)
            try:
                sys.stdout.flush()
            except Exception:
                pass
            self.exit_fn()
        threading.Thread(target=_later, daemon=True).start()


def _replace_retry(tmp: Path, dst: Path, tries: int = 40):
    """Windows 上文件可能被短暂占用（网页正在读取、杀毒软件扫描）：重试几秒。"""
    for i in range(tries):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            if i == tries - 1:
                try:
                    tmp.unlink()
                except Exception:
                    pass
                raise
            time.sleep(0.25)


def _create_time() -> float:
    try:
        import psutil
        return psutil.Process().create_time()
    except Exception:
        return 0.0


def spawn_detached(cmd: list[str], cwd: str):
    """启动一个和本进程无关的独立进程（本进程退出后它继续运行，不带黑色窗口）。"""
    kw = {"cwd": cwd, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        DETACHED, NEW_GROUP, BREAKAWAY, NO_WINDOW = 0x00000008, 0x00000200, 0x01000000, 0x08000000
        try:
            return subprocess.Popen(cmd, creationflags=DETACHED | NEW_GROUP | BREAKAWAY | NO_WINDOW, **kw)
        except OSError:
            return subprocess.Popen(cmd, creationflags=DETACHED | NEW_GROUP | NO_WINDOW, **kw)
    return subprocess.Popen(cmd, start_new_session=True, **kw)


# 重启助手：独立脚本（只用标准库 + psutil），写到 data\updates\restart_helper.py 后运行。
# 它不属于新版本或旧版本的程序文件，所以新版本有问题时也能把旧版本恢复回来。
HELPER_SRC = r'''
import json, os, shutil, subprocess, sys, time, urllib.request
from pathlib import Path

job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
root, data = Path(job["root"]), Path(job["data_dir"])
logf = data / "logs" / "update-helper.log"
logf.parent.mkdir(parents=True, exist_ok=True)

def log(msg):
    with open(logf, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")

try:
    import psutil
except Exception:
    psutil = None

def alive(pid, ct=0.0):
    if psutil is None:
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False
    try:
        p = psutil.Process(pid)
        if ct and abs(p.create_time() - ct) > 1.0:
            return False
        return p.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False

def spawn(cmd):
    kw = dict(cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        try:
            return subprocess.Popen(cmd, creationflags=0x00000008 | 0x00000200 | 0x01000000 | 0x08000000, **kw)
        except OSError:
            return subprocess.Popen(cmd, creationflags=0x00000008 | 0x00000200 | 0x08000000, **kw)
    return subprocess.Popen(cmd, start_new_session=True, **kw)

def lock():
    try:
        return json.loads((data / "app.lock").read_text(encoding="utf-8"))
    except Exception:
        return {}

def healthy(version, timeout, proc=None):
    end = time.time() + timeout
    dead_at = None
    while time.time() < end:
        time.sleep(1)
        if proc is not None and proc.poll() is not None:
            # 启动的进程已经退出（启动出错）：再等 3 秒确认，不用等到超时
            dead_at = dead_at or time.time()
            if time.time() - dead_at > 3:
                return None
        lk = lock()
        if lk.get("version") == version and lk.get("pid") and alive(int(lk["pid"])):
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/" % int(lk["port"]), timeout=3) as r:
                    if b"FLEET_TOKEN" in r.read(300000):
                        return lk
            except Exception:
                pass
    return None

def start():
    py = job["pythonw"] if Path(job["pythonw"]).exists() else sys.executable
    args = [a for a in job.get("args", []) if a not in ("--reuse", "--hidden", "--no-browser", "--stop")]
    return spawn([py, str(root / "app.py")] + args + ["--hidden", "--no-browser"])

def kill_tree(p):
    """只结束 Python 进程（新版本的后台和它的工作进程），MT5 终端不碰。"""
    if psutil is None:
        try:
            p.kill()
        except Exception:
            pass
        return
    try:
        top = psutil.Process(p.pid)
        procs = [top] + top.children(recursive=True)
    except Exception:
        return
    lk = lock()
    if lk.get("pid") and lk.get("version") == job["target"]:
        try:
            mp = psutil.Process(int(lk["pid"]))
            if not lk.get("create_time") or abs(mp.create_time() - float(lk["create_time"])) < 1.0:
                procs += [mp] + mp.children(recursive=True)
        except Exception:
            pass
    for x in procs:
        try:
            n = (x.name() or "").lower()
            if n.startswith("python"):
                x.kill()
        except Exception:
            pass

def restore(bdir):
    info = json.loads((bdir / "backup_info.json").read_text(encoding="utf-8"))
    files = info.get("files") or []
    keep = set(files)
    for d in ("fleet", "static", "tools"):
        base = root / d
        if base.is_dir():
            for f in list(base.rglob("*")):
                rel = f.relative_to(root).as_posix()
                if f.is_file() and "__pycache__" not in rel and rel not in keep:
                    f.unlink()
            shutil.rmtree(base / "__pycache__", ignore_errors=True)
    for rel in files:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        for i in range(40):
            try:
                shutil.copy2(bdir / rel, root / rel)
                break
            except PermissionError:
                if i == 39:
                    raise
                time.sleep(0.25)
    return info.get("version")

def write_last(**kw):
    p = data / "updates" / "last_update.json"
    try:
        cur = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        cur = {}
    cur.update(kw)
    p.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")

log("重启助手启动：v%s → v%s，等待旧后台（PID %s）退出" % (job["old"], job["target"], job["pid"]))
end = time.time() + 30
while alive(int(job["pid"]), float(job.get("create_time") or 0)) and time.time() < end:
    time.sleep(0.3)
if alive(int(job["pid"]), float(job.get("create_time") or 0)) and psutil is not None:
    log("旧后台 30 秒没有退出，强制结束（只结束这个 Python 进程）")
    try:
        psutil.Process(int(job["pid"])).kill()
    except Exception:
        pass
    time.sleep(1)
p = start()
log("已启动新版本后台（PID %s）" % p.pid)
lk = healthy(job["target"], float(job.get("timeout", 90)), p)
if lk:
    log("新版本 v%s 运行正常（PID %s，端口 %s）" % (job["target"], lk.get("pid"), lk.get("port")))
    write_last(status="ok", finished_at=time.time(), port=lk.get("port"))
    sys.exit(0)
log("新版本没有在规定时间内正常启动，恢复备份 %s" % job["backup"])
kill_tree(p)
time.sleep(1.5)
try:
    ver = restore(data / "backups" / job["backup"])
except Exception as e:
    log("恢复备份失败：%s" % e)
    write_last(status="failed", error="新版本启动失败，恢复备份也失败：%s" % e, finished_at=time.time())
    sys.exit(1)
write_last(status="rolled_back", error="新版本 v%s 没有正常启动，已自动恢复 v%s" % (job["target"], ver), finished_at=time.time())
p = start()
lk = healthy(ver, float(job.get("timeout", 90)), p)
log("已恢复并重新启动 v%s：%s" % (ver, "正常" if lk else "仍未启动，请双击「一键启动.vbs」"))
'''
