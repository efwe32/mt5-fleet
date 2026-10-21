"""v1.5.8：头像详情弹层自带 --pd-*，白底文字清晰。
python tests/test_panel158.py
"""
import re
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

check("版本 1.5.8", __version__ == "1.5.8", __version__)

notes = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
readme = (ROOT / "README.md").read_text(encoding="utf-8")
dcss = (ROOT / "static" / "desk.css").read_text(encoding="utf-8")
djs = (ROOT / "static" / "desk.js").read_text(encoding="utf-8")

check("更新说明 v1.5.8", "## v1.5.8" in notes and ("发虚" in notes or "发白" in notes or "--pd-" in notes))
check("README 本次更新", "v1.5.8" in readme)

# .pd-avmask block must define --pd-ink before any child uses it
m = re.search(r"\.pd-avmask\s*\{([^}]*)\}", dcss)
check(".pd-avmask 规则存在", bool(m), "missing .pd-avmask{...}")
block = m.group(1) if m else ""
check(".pd-avmask 定义 --pd-ink", "--pd-ink:" in block or "--pd-ink :" in block, block[:120])
check(".pd-avmask 定义 --pd-mut", "--pd-mut:" in block or "--pd-mut :" in block)
check(".pd-avmask 定义 --pd-mono", "--pd-mono:" in block or "--pd-mono :" in block)
check(".pd-avmask 定义 --pd-sans", "--pd-sans:" in block or "--pd-sans :" in block)
check(".pd-avmask 定义 --pd-line", "--pd-line:" in block or "--pd-line :" in block)
check(".pd-avmask 定义 --pd-g", "--pd-g:" in block or "--pd-g :" in block)
check(".pd-avmask 定义 --pd-crim", "--pd-crim:" in block or "--pd-crim :" in block)
check(".pd-avmask color:var(--pd-ink)", "color:var(--pd-ink)" in block or "color: var(--pd-ink)" in block)

check(".pd-avsheet 仍用 var(--pd-ink)", ".pd-avsheet" in dcss and "color:var(--pd-ink)" in dcss)
check("ensureAvSheet 仍挂 body", 'className = "pd-avmask"' in djs and "document.body.appendChild" in djs)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
sys.exit(1 if fails else 0)
