"""测试用：冒充官方 MetaTrader5 包（内部是假终端），并检查 initialize 必须带 path + portable=True。"""
from fleet.mock_mt5 import MockMT5

_m = MockMT5()
CALLS = []


def initialize(path=None, **kw):
    if not path or kw.get("portable") is not True:
        raise AssertionError(f"initialize 必须带 path 和 portable=True，实际 path={path} kw={kw}")
    CALLS.append(path)
    return _m.initialize(path=path, **kw)


def __getattr__(name):
    return getattr(_m, name)
