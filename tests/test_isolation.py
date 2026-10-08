"""终端隔离测试（Linux 上用一个假的 terminal64.exe 可执行文件代替）：
- 只接受 terminals 目录里的路径
- 本程序启动的终端登记 PID，能被关闭
- 别人启动的同路径终端：拒绝接管，也不会被关闭
- 复制终端副本
用法：python tests/test_isolation.py /tmp/fake/terminal64.exe
"""
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fleet import terminal as T  # noqa: E402
from fleet.procs import Registry  # noqa: E402

fake = Path(sys.argv[1])
tmp = Path(tempfile.mkdtemp())
root = tmp / "terminals"
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  -> {info}" if info else ""))
    if not cond:
        fails.append(name)


# 模板 + 复制
base = root / "base"
(base / "MQL5" / "Experts").mkdir(parents=True)
(base / "Bases").mkdir()
(base / "logs").mkdir()
shutil.copy2(fake, base / "terminal64.exe")
ok, msg = T.clone_terminal(str(base), str(root / "111111"), root)
check("复制副本", ok and (root / "111111" / "terminal64.exe").exists(), msg)
check("跳过 Bases/logs", not (root / "111111" / "Bases").exists() and not (root / "111111" / "logs").exists())
ok, msg = T.clone_terminal(str(base), str(tmp / "outside"), root)
check("拒绝复制到 terminals 外", not ok, msg)
ok, msg = T.clone_terminal(str(base), str(root / "222222"), root)

p1 = str(root / "111111" / "terminal64.exe")
p2 = str(root / "222222" / "terminal64.exe")
check("路径检查：受管路径通过", T.check_managed_path(p1, root) is None)
check("路径检查：base 不能直接用", T.check_managed_path(str(base / "terminal64.exe"), root) is not None)
check("路径检查：外部拒绝", T.check_managed_path("/usr/bin/terminal64.exe", root) is not None)

# 本程序启动 → 登记 → 关闭
reg = Registry(tmp)
pid, ct = T.launch_terminal(p1)
time.sleep(0.5)
reg.add(p1, pid, ct)
check("启动后登记", reg.get(p1) is not None and T.is_running(p1), pid)

# 别人启动的（不登记）
other = subprocess.Popen([p2, "/portable"])
time.sleep(0.5)
check("外部进程未登记", reg.get(p2) is None and T.is_running(p2))
fake_entry = {"path": p2, "pid": other.pid, "create_time": 1.0}  # create_time 对不上 → 不是我们的
ok, msg = T.close_owned(fake_entry)
check("create_time 不符不关闭", T.is_running(p2), msg)

# Worker.ensure_terminal 拒绝接管外部终端
from fleet.worker import Worker  # noqa: E402
import queue  # noqa: E402
w = Worker({"id": "x", "login": "222222", "server": "s", "terminal_path": p2}, "pw", {}, queue.Queue(), queue.Queue(), False, str(tmp), None, str(root))
ok, msg = w.ensure_terminal(p2)
check("拒绝接管外部启动的终端", not ok and "不是本程序启动" in msg, msg)
w2 = Worker({"id": "y", "login": "111111", "server": "s", "terminal_path": p1}, "pw", {}, queue.Queue(), queue.Queue(), False, str(tmp), reg.get(p1), str(root))
ok, msg = w2.ensure_terminal(p1)
check("接受自己启动的终端", ok, msg)
w3 = Worker({"id": "z", "login": "1", "server": "s", "terminal_path": ""}, "pw", {}, queue.Queue(), queue.Queue(), False, str(tmp), None, str(root))
ok, msg = w3.ensure_terminal("")
check("没有路径时拒绝（不连默认终端）", not ok, msg)

# 关闭所有登记的：只关自己的
for e in reg.all_alive():
    T.close_owned(e, timeout=5)
time.sleep(0.5)
check("自己启动的已关闭", not T.is_running(p1))
check("外部启动的仍在运行", T.is_running(p2))
other.kill()
other.wait()

# chr 编辑
charts = root / "111111" / "MQL5" / "Profiles" / "Charts" / "Default"
charts.mkdir(parents=True)
chr_text = "<chart>\r\nsymbol=XAUUSD\r\n<expert>\r\nname=GoldPulse\r\npath=Experts\\Fleet\\GoldPulse.ex5\r\n<inputs>\r\nA=1\r\n</inputs>\r\n</expert>\r\n<window>\r\n</window>\r\n</chart>\r\n"
(charts / "chart01.chr").write_bytes(b"\xff\xfe" + chr_text.encode("utf-16-le"))
(charts / "chart02.chr").write_bytes(b"\xff\xfe" + chr_text.replace("GoldPulse", "Other").encode("utf-16-le"))
n = T.remove_expert_from_charts(p1, "GoldPulse.ex5")
t1 = (charts / "chart01.chr").read_bytes().decode("utf-16")
t2 = (charts / "chart02.chr").read_bytes().decode("utf-16")
check("从图表移除指定 EA", n == 1 and "<expert>" not in t1 and "<window>" in t1 and "Other" in t2)

# ini
ini = T.write_start_ini(p1, "GoldPulse.ex5", "XAUUSD", "H1", "gold.set")
txt = ini.read_bytes().decode("utf-16")
check("启动配置文件", "Expert=Fleet\\GoldPulse" in txt and "ExpertParameters=gold.set" in txt and "Password" not in txt)

# 一次性启动配置（含密码）+ 抹除
ini = T.write_config_ini(p1, {"login": "111111", "password": "p@ss;w", "server": "S-1"}, None, False)
txt = ini.read_bytes().decode("utf-16")
check("登录配置文件", "[Common]" in txt and "Login=111111" in txt and "Password=p@ss;w" in txt and "KeepPrivate=1" in txt
      and "NewsEnable=0" in txt and "Enabled=1" in txt and "AllowLiveTrading=1" in txt and ini.name.startswith("fleet_"))
check("抹除启动配置", T.wipe_file(ini) and not ini.exists())
T.write_config_ini(p1, {"login": "1", "password": "x", "server": "s"})
check("清理遗留启动配置", T.wipe_stale_inis(root) >= 1 and not list((root / "111111").glob("fleet_*.ini")))

# 复制：半截副本不会被当成可用终端
(root / "333333.copying").mkdir()
ok, msg = T.clone_terminal(str(base), str(root / "333333"), root)
check("复制用临时目录再改名", ok and (root / "333333" / "terminal64.exe").exists() and not (root / "333333.copying").exists(), msg)
(base / "Config").mkdir(exist_ok=True)
(base / "Config" / "accounts.dat").write_bytes(b"x")
(base / "Config" / "servers.dat").write_bytes(b"S" * 100)
ok, msg = T.clone_terminal(str(base), str(root / "444444"), root)
check("复制跳过 accounts.dat、保留 servers.dat", not (root / "444444" / "Config" / "accounts.dat").exists() and (root / "444444" / "Config" / "servers.dat").exists())
check("找模板：base 优先、不把账户副本当模板", T.find_templates(tmp, root)[0] == base and all(p.parent == root and p.name == "base" or root not in p.parents for p in T.find_templates(tmp, root)))

# servers.dat 导入（只读）+ 放入副本一次
src = tmp / "desk" / "Config"
src.mkdir(parents=True)
(src / "servers.dat").write_bytes(b"D" * 200)
ok, msg, meta = T.import_servers_dat(str(src / "servers.dat"), tmp / "lib")
check("导入 servers.dat", ok and (tmp / "lib" / "servers.dat").read_bytes() == b"D" * 200, msg)
check("servers.dat 候选", T.servers_dat_candidates(str(tmp / "desk"))[0]["path"].endswith("servers.dat"))
p4 = str(root / "444444" / "terminal64.exe")
check("放入副本并备份原文件", T.apply_servers_dat(p4, tmp / "lib" / "servers.dat") and (root / "444444" / "Config" / "servers.dat.fleetbak").read_bytes() == b"S" * 100)
(root / "444444" / "Config" / "servers.dat").write_bytes(b"N" * 300)   # 终端自己搜到了新服务器
check("同一份导入不重复覆盖", T.apply_servers_dat(p4, tmp / "lib" / "servers.dat") == "" and (root / "444444" / "Config" / "servers.dat").read_bytes() == b"N" * 300)

# 校验 EA 加载：读启动之后新增的日志
logs = root / "444444" / "Logs"
logs.mkdir()
lf = logs / "20260101.log"
lf.write_bytes(b"\xff\xfe" + "KO\t0\t10:00:00\tExperts\texpert Pulse (XAUUSD,H1) loaded successfully\r\n".encode("utf-16-le"))
marks = T.log_marks(p4)
check("旧日志不算", T.find_ea_in_logs(p4, "Pulse.ex5", marks)[0] == "none")
with open(lf, "ab") as f:
    f.write("KO\t0\t10:01:00\tExperts\t专家 Pulse (XAUUSD,H1) 加载成功\r\n".encode("utf-16-le"))
check("新日志里找到加载成功（中文界面）", T.find_ea_in_logs(p4, "Pulse.ex5", marks)[0] == "ok")
with open(lf, "ab") as f:
    f.write("KO\t0\t10:02:00\tExperts\texpert Other (EURUSD,H1) loaded successfully\r\n".encode("utf-16-le"))
marks = T.log_marks(p4)
with open(lf, "ab") as f:
    f.write("KO\t0\t10:03:00\tExperts\texpert Pulse (XAUUSD,H1) failed to load\r\n".encode("utf-16-le"))
check("新日志里找到加载失败", T.find_ea_in_logs(p4, "Pulse.ex5", marks)[0] == "fail")

# 每账户参数文件
pr = T.make_account_preset(p4, None, "Pulse", "444444", {"MagicNumber": 77})
check("生成账户参数文件", "MagicNumber=77" in (root / "444444" / "MQL5" / "Presets" / pr).read_bytes().decode("utf-16"))

# ---------------- 第 5 轮：图表配置 / 接管 / 后台清理 / 品种后缀 ----------------
import os  # noqa: E402
import psutil  # noqa: E402
from fleet import instance as I  # noqa: E402

acct = root / "555555"
T.clone_terminal(str(base), str(acct), root)
prof = acct / "MQL5" / "Profiles" / "Charts" / "Default"
prof.mkdir(parents=True, exist_ok=True)
tp = str(acct / "terminal64.exe")
for i, sym in enumerate(["XAUUSDm", "XAUUSD", "XAUUSDc", "XAUUSDz", "XAUUSDr"], 1):   # 模板自带的 5 个图表
    T._write_chart(prof / f"chart{i:02d}.chr", T.chart_text(sym, "H1"))
(prof / "chart06.chr").write_bytes(b"\xff\xfe" + "<chart>\r\nid=0\r\nsymbol=USDJPYc\r\nperiod_type=0\r\nperiod_size=15\r\nwindows_total=0\r\n</chart>".encode("utf-16-le"))
r = T.prepare_profile(tp, "USDJPYc", "M15")
charts = T.list_charts(tp)
check("登录前整理成单个图表 USDJPYc M15", len(charts) == 1 and charts[0]["symbol"] == "USDJPYc" and charts[0]["period"] == (0, 15)
      and charts[0]["windows"] == 1 and not charts[0]["experts"], charts)
check("移走的图表有备份", r["removed"] == 6 and len(list(Path(r["backup"]).glob("*.chr"))) == 6, r)
r2 = T.prepare_profile(tp, "USDJPYc", "M15")
check("已经是单图表时不再改动", r2["changed"] is False and [c["file"] for c in T.list_charts(tp)] == [charts[0]["file"]], r2)
preset = tmp / "p.set"
preset.write_bytes(b"\xff\xfe" + "; comment\r\nLots=0.50||0.1||0.1||1||N\r\nMagicNumber=880001\r\n".encode("utf-16-le"))
r = T.prepare_profile(tp, "USDJPYc", "M15", {"stem": "USDJPY_Sim_V2_锁3333", "inputs": T.preset_inputs(preset)}, ["USDJPY_Sim_V2_锁3333"])
charts = T.list_charts(tp)
txt = T._read_text(prof / r["created"])[0]
check("EA 写进图表文件（重启后自动加载）", len(charts) == 1 and charts[0]["experts"] == [("USDJPY_Sim_V2_锁3333", "Experts\\Fleet\\USDJPY_Sim_V2_锁3333.ex5")]
      and "expertmode=1" in txt and "Lots=0.50\r\n" in txt and "MagicNumber=880001" in txt and txt.index("<expert>") < txt.index("<window>"), charts)
check("图表文件是 UTF-16 带 BOM、CRLF", (prof / r["created"]).read_bytes()[:2] == b"\xff\xfe" and "\r\n" in txt)
T._write_chart(prof / "chart09.chr", T.chart_text("EURUSDc", "H1", T.expert_block("Other")))
r = T.prepare_profile(tp, "USDJPYc", "M15", {"stem": "USDJPY_Sim_V2_锁3333", "inputs": []}, ["USDJPY_Sim_V2_锁3333"])
charts = T.list_charts(tp)
check("再次分发：替换同名 EA 图表、保留其它 EA", sorted(e[0][0] for e in [c["experts"] for c in charts]) == ["Other", "USDJPY_Sim_V2_锁3333"], charts)
r = T.prepare_profile(tp, "USDJPYc", "M15", None, ["USDJPY_Sim_V2_锁3333"])
check("停止 EA：只剩其它 EA 的图表", [c["experts"][0][0] for c in T.list_charts(tp)] == ["Other"], T.list_charts(tp))
r = T.prepare_profile(tp, "USDJPYc", "M15", None, ["Other"])
charts = T.list_charts(tp)
check("全部 EA 停止后恢复一个默认图表", len(charts) == 1 and charts[0]["symbol"] == "USDJPYc" and not charts[0]["experts"], charts)
check("周期编码", T.PERIOD_CODES["M15"] == (0, 15) and T.PERIOD_CODES["H1"] == (1, 1) and T.PERIOD_CODES["D1"] == (2, 1))

# 接管：只认 /portable + 本终端目录里的 fleet_*.ini 启动参数
ini = T.write_config_ini(tp, None, None)
mine = subprocess.Popen([tp, "/portable", f"/config:{ini}"], cwd=str(acct))
foreign = subprocess.Popen([tp, "/portable"], cwd=str(acct))
time.sleep(0.5)
pm, pf = psutil.Process(mine.pid), psutil.Process(foreign.pid)
check("本程序参数启动的终端可接管", T.launched_by_fleet(pm, tp))
check("用户自己打开的同目录终端不接管", not T.launched_by_fleet(pf, tp))
other_ini = tmp / "fleet_x.ini"
other_ini.write_text("x")
odd = subprocess.Popen([tp, "/portable", f"/config:{other_ini}"], cwd=str(acct))
time.sleep(0.3)
check("配置文件不在本终端目录的不接管", not T.launched_by_fleet(psutil.Process(odd.pid), tp))
check("adoptable_processes 只返回本程序的", [p.pid for p in T.adoptable_processes(tp)] == [mine.pid])
for pr in (mine, foreign, odd):
    pr.kill()
    pr.wait()
T.wipe_file(ini)

# 品种后缀建议
check("USDJPY 不存在时建议 USDJPYc", T.suggest_symbols("USDJPY", ["USDJPYc", "EURUSDc"]) == ["USDJPYc"])
check("USDJPYm 建议 USDJPYc", T.suggest_symbols("USDJPYm", ["USDJPYc", "XAUUSDc"]) == ["USDJPYc"])
check("没有同类品种时不乱建议", T.suggest_symbols("GOLD", ["XAUUSDc"]) == [])

# 启动时清理上次的后台：只认本程序目录 + 同一个数据目录的 app.py 和它的工作进程
app_dir = tmp / "app15"
app_dir.mkdir()
(app_dir / "app.py").write_text("import time\nwhile True: time.sleep(1)\n")
other_dir = tmp / "otherapp"
other_dir.mkdir()
(other_dir / "app.py").write_text("import time\nwhile True: time.sleep(1)\n")
old = subprocess.Popen([sys.executable, "app.py", "--no-browser"], cwd=str(app_dir))
old_other_data = subprocess.Popen([sys.executable, "app.py", "--data-dir", str(tmp / "d2")], cwd=str(app_dir))
unrelated = subprocess.Popen([sys.executable, "app.py"], cwd=str(other_dir))
# 孤儿工作进程（父进程已经没了）：用 sh 后台启动后 sh 立即退出
pidf = tmp / "orphan.pid"
subprocess.run(["sh", "-c", f"'{sys.executable}' -c 'import time; time.sleep(600)' 'from multiprocessing.spawn import spawn_main' "
                f"--multiprocessing-fork >/dev/null 2>&1 & echo $! > '{pidf}'"], cwd=str(app_dir))
child_of_other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", "from multiprocessing.spawn import spawn_main",
                                   "--multiprocessing-fork"], cwd=str(app_dir))   # 父进程还活着且不是本程序：不碰


class _P:
    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        return None if psutil.pid_exists(self.pid) and psutil.Process(self.pid).status() != psutil.STATUS_ZOMBIE else 0

    def kill(self):
        try:
            psutil.Process(self.pid).kill()
        except Exception:
            pass

    def wait(self):
        pass


# 便携版放在程序目录里面（D:\\15\\新建文件夹\\MT5批量终端）：两边互不清理
pkg = app_dir / "新建文件夹" / "MT5批量终端"
(pkg / "fleet").mkdir(parents=True)
(pkg / T.PORTABLE_MARKER).write_text("x")
(pkg / "app.py").write_text("import time\nwhile True: time.sleep(1)\n")
pkg_main = subprocess.Popen([sys.executable, str(pkg / "app.py"), "--hidden", "--reuse"], cwd=str(pkg))
pkg_pidf = tmp / "pkg_orphan.pid"
subprocess.run(["sh", "-c", f"'{sys.executable}' -c 'import time; time.sleep(600)' 'from multiprocessing.spawn import spawn_main' "
                f"--multiprocessing-fork >/dev/null 2>&1 & echo $! > '{pkg_pidf}'"], cwd=str(pkg))
abs_main = subprocess.Popen([sys.executable, str(app_dir / "app.py"), "--hidden"], cwd=str(tmp))   # 用绝对路径、在别的目录启动的本程序

time.sleep(0.3)
orphan = _P(int(pidf.read_text().strip()))
pkg_orphan = _P(int(pkg_pidf.read_text().strip()))
term = subprocess.Popen([tp, "/portable"], cwd=str(acct))
time.sleep(1.0)
found = {p.pid for p in I.find_stale(app_dir, app_dir / "data")}
check("找到上次的主进程和工作进程", old.pid in found and orphan.pid in found and abs_main.pid in found, found)
check("程序目录里的便携版（主进程和工作进程）不算", not {pkg_main.pid, pkg_orphan.pid} & found, found)
pkg_found = {p.pid for p in I.find_stale(pkg, pkg / "data")}
check("便携版只清理它自己的进程", pkg_found == {pkg_main.pid, pkg_orphan.pid}, (pkg_found, pkg_main.pid, pkg_orphan.pid))
check("别的数据目录 / 别的程序 / MT5 终端 / 别人的工作进程不算", not {old_other_data.pid, unrelated.pid, term.pid, child_of_other.pid} & found, found)
killed = I.cleanup_stale(app_dir, app_dir / "data")
time.sleep(0.5)
check("清理后旧后台已结束", old.poll() is not None and orphan.poll() is not None and abs_main.poll() is not None, killed)
check("便携版的进程没被 D:\\15 的清理结束", pkg_main.poll() is None and pkg_orphan.poll() is None)
check("清理不碰其它进程和终端", old_other_data.poll() is None and unrelated.poll() is None and term.poll() is None
      and child_of_other.poll() is None)
for pr in (old, old_other_data, unrelated, orphan, term, child_of_other, pkg_main, pkg_orphan, abs_main):
    if pr.poll() is None:
        pr.kill()
    pr.wait()
# 找模板：跳过 python 运行环境、便携版文件夹、本程序的其它副本
troot2 = app_dir / "terminals"
for d in ("MetaTrader 5    4", "python", "新建文件夹/MT5批量终端/MT5模板", "新建文件夹/MT5批量终端/python", "备份/fleetcopy/MT5模板"):
    (app_dir / d).mkdir(parents=True, exist_ok=True)
    (app_dir / d / "terminal64.exe").write_bytes(b"x")
(app_dir / "备份" / "fleetcopy" / "app.py").write_text("")
(app_dir / "备份" / "fleetcopy" / "fleet").mkdir()
tpls = [str(x.relative_to(app_dir)) for x in T.find_templates(app_dir, troot2)]
check("找模板只认 MetaTrader 5    4（不进便携版 / python / 程序副本）", tpls == ["MetaTrader 5    4"], tpls)
tpls2 = [x.name for x in T.find_templates(pkg, pkg / "terminals")]
check("便携版自己找到 MT5模板", tpls2 == ["MT5模板"], tpls2)
lk = I.InstanceLock(tmp)
lk.write(8765)
check("运行锁记录 PID 和端口", lk.read().get("pid") == os.getpid() and lk.read().get("port") == 8765)
lk.release()
check("退出时删除运行锁", not (tmp / "app.lock").exists())

shutil.rmtree(tmp, ignore_errors=True)
print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
sys.exit(1 if fails else 0)
