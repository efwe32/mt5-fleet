"""v1.5.11：收益台真人化头像（10 套静态 PNG）。
python tests/test_panel1511.py
"""
import struct
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


check("版本 1.5.11", __version__ == "1.5.11", __version__)

notes = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
readme = (ROOT / "README.md").read_text(encoding="utf-8")
desk = (ROOT / "static" / "desk.js").read_text(encoding="utf-8")
dcss = (ROOT / "static" / "desk.css").read_text(encoding="utf-8")

check("更新说明 v1.5.11", "## v1.5.11" in notes and ("头像" in notes or "真人" in notes))
check("README 本次更新 1.5.11", "v1.5.11" in readme and ("头像" in readme or "真人" in readme))
check("仍提及 1.5.10 清仓", "1.5.10" in notes and ("清仓" in notes or "平全部" in notes))

avatars = ROOT / "static" / "avatars"
# CI / 仅提交了 .b64 时先解码出 PNG
import base64
for b64p in sorted(avatars.glob("face-*.png.b64")):
    png = Path(str(b64p)[:-4])
    raw = base64.b64decode("".join(b64p.read_text(encoding="ascii").split()))
    if not png.is_file() or png.read_bytes() != raw:
        png.write_bytes(raw)

for i in range(10):
    p = avatars / f"face-{i}.png"
    ok_exist = p.is_file() and p.stat().st_size > 1000
    check(f"face-{i}.png 存在", ok_exist, str(p))
    if not ok_exist:
        continue
    data = p.read_bytes()
    check(f"face-{i}.png 可读 PNG", data[:8] == b"\x89PNG\r\n\x1a\n", data[:8])
    # IHDR width/height ~192
    w = h = None
    if data[12:16] == b"IHDR" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
    check(f"face-{i}.png 约 192px", w == 192 and h == 192, f"{w}x{h}")

check("desk.js FACE_SRC", "FACE_SRC" in desk and "/static/avatars/face-" in desk)
# .pd-av canvas rule: border-radius 50% and image-rendering auto, not pixelated
import re
m = re.search(r"\.pd-av\s+canvas\s*\{([^}]+)\}", dcss)
block = m.group(1) if m else ""
check("desk.css .pd-av canvas border-radius:50%", "border-radius:50%" in block.replace(" ", ""), block[:120])
check("desk.css .pd-av canvas image-rendering:auto", "image-rendering:auto" in block.replace(" ", "") or "image-rendering: auto" in block, block[:120])
check("desk.css .pd-av canvas 非 pixelated", "pixelated" not in block, block[:120])

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
sys.exit(1 if fails else 0)
