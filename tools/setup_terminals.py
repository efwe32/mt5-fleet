"""创建终端副本：为每个账户准备一份独立的便携 MT5（程序目录\\terminals\\<账号>）。

  python tools/setup_terminals.py                 自动找模板 + 读取 data\\accounts.json 里的账号
  python tools/setup_terminals.py 51002811 88011203   指定账号
  python tools/setup_terminals.py --src "D:\\15\\MetaTrader 5"  指定模板目录
  python tools/setup_terminals.py --open 51002811  以便携模式打开某个副本（第一次手动登录/搜索券商服务器用）

只读取模板目录，不修改它；不会碰 %APPDATA%\\MetaQuotes，也不会碰桌面上正在运行的 MT5。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fleet.config import terminals_root  # noqa: E402
from fleet.terminal import clone_terminal, is_app_copy, is_running  # noqa: E402
from fleet.terminal import find_templates as _find_templates  # noqa: E402

SKIP_SEARCH = {".venv", "venv", "data", "data_mock", "build", "dist", "__pycache__", "static", "fleet", "tools"}


def find_templates(root: Path, troot: Path) -> list[Path]:
    # 和程序里的自动查找一致：跳过 python 运行环境、便携版文件夹（如 新建文件夹\\MT5批量终端）等本程序的其它副本
    return _find_templates(root, troot)


def find_installers(root: Path) -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        if len(d.relative_to(root).parts) > 2:
            dirnames[:] = []
            continue
        dirnames[:] = [n for n in dirnames if n.lower() not in SKIP_SEARCH and n.lower() != "terminals"
                       and not n.lower().startswith("python") and not is_app_copy(d / n)]
        for f in filenames:
            fl = f.lower()
            if fl.endswith(".exe") and fl != "terminal64.exe" and ("mt5" in fl or "metatrader" in fl or "setup" in fl):
                out.append(d / f)
    return out


def logins_from_accounts() -> list[str]:
    for name in ("data", "data_mock"):
        p = ROOT / name / "accounts.json"
        if p.exists() and name == "data":
            try:
                return [a["login"] for a in json.loads(p.read_text(encoding="utf-8")).get("accounts", []) if a.get("login")]
            except Exception:
                pass
    return []


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def main():
    ap = argparse.ArgumentParser(description="为每个账户创建独立的便携 MT5 终端副本")
    ap.add_argument("logins", nargs="*", help="账号（不填则读取 data\\accounts.json，或手动输入）")
    ap.add_argument("--src", default="", help="模板 MT5 目录（里面有 terminal64.exe）")
    ap.add_argument("--open", default="", help="以便携模式打开 terminals\\<账号> 这个副本")
    args = ap.parse_args()

    troot = terminals_root()
    troot.mkdir(parents=True, exist_ok=True)
    print("=" * 64)
    print(f" 程序目录：{ROOT}")
    print(f" 受管终端目录：{troot}")
    print("=" * 64)

    if args.open:
        exe = troot / args.open / "terminal64.exe"
        if not exe.exists():
            print(f"[错误] 找不到 {exe}")
            return 1
        if is_running(str(exe)):
            print("这个副本已经在运行。")
            return 0
        subprocess.Popen([str(exe), "/portable"], cwd=str(exe.parent))
        print(f"已用便携模式打开：{exe}")
        print("第一次使用：在 MT5 里 文件 → 登录交易账户（找不到服务器就用『开新账户』里搜索券商名称），")
        print("勾选保存密码，打开工具栏的『Algo Trading / 算法交易』，然后关闭这个 MT5 窗口。")
        return 0

    # 1. 模板
    src = Path(args.src) if args.src else None
    if src is None:
        cands = find_templates(ROOT, troot)
        if len(cands) == 1:
            src = cands[0]
        elif len(cands) > 1:
            print("找到多份 MT5：")
            for i, c in enumerate(cands, 1):
                print(f"  {i}. {c}")
            pick = ask("用哪一份做模板？输入序号（默认 1）：") or "1"
            try:
                src = cands[int(pick) - 1]
            except Exception:
                print("[错误] 序号不对")
                return 1
    if src is None or not (src / "terminal64.exe").exists():
        inst = find_installers(ROOT)
        print("\n没有在程序目录里找到已安装的 MT5（terminal64.exe）。")
        if inst:
            print("找到这些可能是 MT5 安装包的文件：")
            for p in inst:
                print(f"  - {p}")
        base = troot / "base"
        print(f"""
请这样安装一份「模板」MT5（不会影响你电脑上已有的 MT5）：
  1. 双击 MT5 安装包（例如 mt5setup.exe）。
  2. 在安装界面点「设置 / Settings」，把安装文件夹改成：
       {base}
     （不要用默认的 C:\\Program Files\\...，那可能是你桌面上正在用的那份）
  3. 安装完成后如果 MT5 自动打开了，直接关掉它。
  4. 再双击一次「创建终端副本.bat」。
""")
        if inst and sys.platform == "win32":
            if ask("现在打开第一个安装包吗？[y/N]：").lower() == "y":
                os.startfile(str(inst[0]))  # type: ignore[attr-defined]
        return 2
    print(f"模板：{src}")
    if is_running(str(src / "terminal64.exe")):
        print("[提示] 模板 MT5 正在运行。复制只读取文件，不会关闭或修改它；如果复制失败，请先手动关掉它再试。")

    # 2. 账号
    logins = args.logins or logins_from_accounts()
    if not logins:
        raw = ask("输入要创建终端的账号，多个用逗号分隔：")
        logins = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
    logins = [x for x in logins if x.isdigit()]
    if not logins:
        print("没有账号，结束。")
        return 1

    # 3. 复制
    ok_n = 0
    for lg in logins:
        dst = troot / lg
        try:
            ok, msg = clone_terminal(str(src), str(dst), troot)
        except Exception as e:
            ok, msg = False, f"复制失败：{e}"
        print(("  [好] " if ok else "  [失败] ") + f"{lg}：{msg}")
        ok_n += ok
    print(f"\n完成 {ok_n}/{len(logins)}。每个账户的终端路径：{troot}\\<账号>\\terminal64.exe")
    print("提示：现在推荐直接在面板「账号 → 添加并登录」里操作，副本会自动创建，不必运行本脚本。")
    print("在 MT5 批量终端里添加账户时，终端路径留空即可自动使用上面的路径。")
    print("如果某个账户登录时报『授权失败/找不到服务器』，先在面板「设置 → 从已有 MT5 导入服务器列表」；还不行再运行：")
    print("  创建终端副本.bat --open <账号>   手动登录一次（搜索券商服务器、开启「算法交易」），再关掉。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
