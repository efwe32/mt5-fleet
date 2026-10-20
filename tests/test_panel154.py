"""v1.5.4：策略库直接删除 + 分发清旧策略。
python tests/test_panel154.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fleet import __version__

fails = []

def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {info}" if info else ""))
    if not cond:
        fails.append(name)

check("版本 1.5.4", __version__ == "1.5.4", __version__)

notes = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
readme = (ROOT / "README.md").read_text(encoding="utf-8")
app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
mgr = (ROOT / "fleet" / "manager.py").read_text(encoding="utf-8")
wkr = (ROOT / "fleet" / "worker.py").read_text(encoding="utf-8")
srv = (ROOT / "fleet" / "server.py").read_text(encoding="utf-8")

check("更新说明 v1.5.4", "## v1.5.4" in notes and "直接删除" in notes and "清旧策略" in notes)
check("README 本次更新", "v1.5.4" in readme and "直接「删除」" in readme)
check("删除确认不再拒绝", "会拒绝删除" not in app and "自动从账户台账清掉同名策略" in app)
check("分发文案含清除旧策略", "清除旧策略" in app and "会先清除该账户上旧策略" in app)
check("分发无同名替换勾选", 'id="dReplace"' not in app)
check("delete_library 不再拒绝运行中", "请先停止或「移除」这些账户上的策略，再删除库文件" not in mgr)
check("delete_library 会 stop + 清台账", "删除库文件前停止策略" in mgr and "a[\"strategies\"] = [s for s in" in mgr)
check("_record_deploy clear_all", "clear_all: bool = False" in mgr and "clear_all=True" in mgr)
check("distribute 传 clear_prior", '"clear_prior": True' in mgr or "'clear_prior': True" in mgr)
check("worker 卸旧策略", "clear_prior" in wkr and "clear_files" in wkr)
check("server 删库说明", "自动清台账" in srv)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
sys.exit(1 if fails else 0)
