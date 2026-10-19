"""v1.5.0：登录默认关闭算法交易。
python tests/test_panel150.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fleet import __version__
from fleet.config import DEFAULT_SETTINGS, Store

fails = []

def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {info}" if info else ""))
    if not cond:
        fails.append(name)

check("版本 ≥1.5.0", tuple(int(x) for x in __version__.split(".")[:3]) >= (1, 5, 0), __version__)
check("默认 auto_enable_algo 为 False", DEFAULT_SETTINGS.get("auto_enable_algo") is False)

notes = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
readme = (ROOT / "README.md").read_text(encoding="utf-8")
html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
aj = (ROOT / "static" / "algo.js").read_text(encoding="utf-8")
app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
mgr = (ROOT / "fleet" / "manager.py").read_text(encoding="utf-8")
wkr = (ROOT / "fleet" / "worker.py").read_text(encoding="utf-8")
cfg = (ROOT / "fleet" / "config.py").read_text(encoding="utf-8")

check("更新说明", ("## v1.5.1" in notes or "## v1.5.0" in notes) and "默认关闭算法交易" in notes and "分发" in notes)
check("README 本次更新", ("v1.5.1" in readme or "v1.5.0" in readme) and "算法交易默认关闭" in readme)
check("设置文案", "登录后默认打开算法交易" in html)
check("登录步骤文案", "算法交易保持关闭" in mgr)
check("login 不再 clear_algo_off", "def login" in mgr and "_clear_algo_off(ids)      # 用户点的登录" not in mgr)
check("distribute 仍 clear_algo_off", "分发是用户点的" in mgr and "_clear_algo_off(ids)" in mgr)
check("worker 有 _deploy_algo", "_deploy_algo" in wkr)
check("worker 默认关闭文案", "算法交易已关闭（默认）" in wkr and "算法交易保持关闭（默认）" in wkr)
check("algo.js 不再说登录会打开", "登录或分发" not in aj and "分发 EA" in aj)
check("添加并登录流程文案", "算法交易保持关闭" in app)
check("一次性迁移标记", "_v150_algo_off_default" in cfg)

# 迁移：旧 settings 里 True 会被改成 False
import tempfile, json, shutil
td = Path(tempfile.mkdtemp())
try:
    (td / "settings.json").write_text(json.dumps({"auto_enable_algo": True}), encoding="utf-8")
    (td / "accounts.json").write_text(json.dumps({"accounts": []}), encoding="utf-8")
    s = Store(td)
    check("旧安装迁移为 False", s.settings.get("auto_enable_algo") is False and s.settings.get("_v150_algo_off_default") is True)
    # 用户改回 True 后不要再被迁移覆盖
    s.settings["auto_enable_algo"] = True
    s.save_settings()
    s2 = Store(td)
    check("迁移后用户改回 True 保留", s2.settings.get("auto_enable_algo") is True)
finally:
    shutil.rmtree(td, ignore_errors=True)

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    sys.exit(1)
print("ALL PASS")
