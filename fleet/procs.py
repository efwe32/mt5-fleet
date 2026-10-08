"""记录「本程序自己启动的」MT5 终端进程（PID + 启动时间 + 路径）。

只有登记在这里、且 PID/启动时间/exe 路径都对得上的进程，本程序才会去关闭或重启。
桌面上用户自己打开的 terminal64.exe 永远不会被碰。
记录保存在 数据目录\\launched.json，程序重启后仍能认出之前自己启动、还在运行的终端。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None


def norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p)) if p else ""


class Registry:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "launched.json"
        self.lock = threading.RLock()
        try:
            self.items: dict = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self.items = {}
        self.prune()

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def alive(entry: dict) -> bool:
        if psutil is None or not entry:
            return False
        try:
            p = psutil.Process(int(entry["pid"]))
            if abs(p.create_time() - float(entry["create_time"])) > 1.0:
                return False  # PID 被系统复用了，不是我们那个进程
            exe = p.exe()
            return norm(exe) == norm(entry["path"])
        except Exception:
            return False

    def prune(self):
        with self.lock:
            dead = [k for k, v in self.items.items() if not self.alive(v)]
            for k in dead:
                self.items.pop(k, None)
            self._save()

    def add(self, path: str, pid: int, create_time: float):
        with self.lock:
            self.items[norm(path)] = {"path": path, "pid": int(pid), "create_time": float(create_time)}
            self._save()

    def remove(self, path: str):
        with self.lock:
            self.items.pop(norm(path), None)
            self._save()

    def get(self, path: str) -> dict | None:
        with self.lock:
            e = self.items.get(norm(path))
            return e if e and self.alive(e) else None

    def all_alive(self) -> list[dict]:
        with self.lock:
            return [v for v in self.items.values() if self.alive(v)]
