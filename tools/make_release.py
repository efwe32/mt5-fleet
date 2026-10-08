"""生成在线更新用的发布文件（只含程序文件，不含任何账户 / 数据 / MT5 / Python）。

用法（在程序目录）：
  python tools/make_release.py                 生成到 dist\\
  python tools/make_release.py --out D:\\发布   指定输出目录
生成：
  mt5-fleet-app-<版本>.zip      程序文件（app.py、fleet、static、tools、README、requirements、启动脚本）
  mt5-fleet-manifest.json       清单：版本、更新说明、压缩包 sha256 / 大小、每个文件的 sha256
  latest.json                   和清单相同（自定义更新地址用：把它和 zip 放到同一个网盘 / 服务器目录，
                                在面板「设置 → 在线更新 → 自定义更新地址」填 latest.json 的直链）
  notes.md                      本版本的更新说明（发布页正文）

发布到 GitHub：把版本号（fleet/__init__.py）和 RELEASE_NOTES.md 改好后推送到 main，
仓库里的 GitHub Actions（.github/workflows/release.yml）会自动运行本脚本并创建 v<版本> 发布。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from fleet import __version__  # noqa: E402
from fleet.updater import APP_NAME, MANIFEST_ASSET, _req_norm, current_app_files, is_app_path  # noqa: E402

ZIP_DATE = (2026, 1, 1, 0, 0, 0)     # 固定时间戳：同样的文件生成同样的 zip


def notes_for(version: str) -> str:
    p = ROOT / "RELEASE_NOTES.md"
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8")
    m = re.search(rf"^##\s*v?{re.escape(version)}\b[^\n]*\n(.*?)(?=^##\s|\Z)", text, re.S | re.M)
    return (m.group(1) if m else "").strip()


def content_of(rel: str) -> bytes:
    """.bat / .vbs 统一用 Windows 换行（CRLF）：git / 网页传输可能把换行改成 LF，cmd 读 LF 的中文批处理会出错。"""
    b = (ROOT / rel).read_bytes()
    if rel.lower().endswith((".bat", ".cmd", ".vbs")):
        b = b.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    return b


def collect() -> list[str]:
    files = current_app_files(ROOT)
    bad = [f for f in files if not is_app_path(f)]
    if bad:
        raise SystemExit(f"[错误] 不允许发布的文件：{bad}")
    for f in files:
        b = (ROOT / f).read_bytes()
        if f.lower().endswith(".vbs") and any(x > 127 for x in b):
            raise SystemExit(f"[错误] {f} 里有非 ASCII 字符（VBS 不支持 UTF-8）。中文请用 ChrW(…) 写。")
        low = f.lower()
        if low.endswith((".ex5", ".ex4", ".dat", ".lock")) or "accounts" in low or "password" in low:
            raise SystemExit(f"[错误] 看起来是数据文件，不能发布：{f}")
    return files


def build(out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    files = collect()
    blobs = {f: content_of(f) for f in files}
    hashes = {f: hashlib.sha256(b).hexdigest() for f, b in blobs.items()}
    asset = f"{APP_NAME}-app-{__version__}.zip"
    zpath = out / asset
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in files:
            zi = zipfile.ZipInfo(f, ZIP_DATE)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            zi.flag_bits |= 0x800          # 文件名 UTF-8
            z.writestr(zi, blobs[f])
    data = zpath.read_bytes()
    notes = notes_for(__version__)
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8") if (ROOT / "requirements.txt").exists() else ""
    manifest = {
        "name": APP_NAME, "version": __version__, "released": time.strftime("%Y-%m-%d"),
        "notes": notes, "asset": asset, "zip_url": asset,
        "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
        "requirements": _req_norm(req), "files": hashes,
    }
    bundled = ROOT / "tools" / "servers" / "mt5-servers.b64"
    if bundled.is_file():
        import base64
        from fleet.terminal import servers_dat_count_bytes
        raw = base64.b64decode("".join(bundled.read_text(encoding="ascii").split()))
        cnt = servers_dat_count_bytes(raw)
        if cnt < 0:
            raise SystemExit("[错误] tools/servers/mt5-servers.b64 不是有效的 servers.dat")
        for bad in (b"password", b"Password", "密码".encode()):
            if bad in raw:
                raise SystemExit(f"[错误] 服务器列表里出现了不该有的内容：{bad!r}")
        (out / "mt5-servers.dat").write_bytes(raw)
        manifest["servers"] = {"asset": "mt5-servers.dat", "sha256": hashlib.sha256(raw).hexdigest(),
                               "size": len(raw), "count": cnt,
                               "note": "只有券商服务器列表（加密保存），没有账号、密码、历史"}
    txt = json.dumps(manifest, ensure_ascii=False, indent=1)
    (out / MANIFEST_ASSET).write_text(txt, encoding="utf-8")
    (out / "latest.json").write_text(txt, encoding="utf-8")
    (out / "notes.md").write_text((notes or f"v{__version__}") + "\n\n---\n面板里「设置 → 在线更新 → 检查更新 / 立即更新」即可一键更新（不需要登录 GitHub）。\n"
                                  f"手动安装：下载 {asset}，解压覆盖到程序目录（不要覆盖 data、terminals 文件夹）。\n", encoding="utf-8")
    return manifest


def main():
    ap = argparse.ArgumentParser(description="生成在线更新发布文件")
    ap.add_argument("--out", default=str(ROOT / "dist"))
    args = ap.parse_args()
    m = build(Path(args.out))
    print(f"v{m['version']}：{len(m['files'])} 个文件，{m['asset']} {m['size'] / 1024:.0f} KB，sha256 {m['sha256']}")
    if not m["notes"]:
        print("[提示] RELEASE_NOTES.md 里没有这个版本的说明（## v版本号）")
    print(f"输出：{Path(args.out).resolve()}")


if __name__ == "__main__":
    main()
