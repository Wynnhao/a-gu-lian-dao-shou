"""pipeline 集成测试 · 全套场景（结构性重构 Phase 1b，docs/结构性重构实施方案.md）。

在 Phase 4（repo/config 收敛）后的新接缝上补齐：
- P1 catchup 缺日补跑（盯市+日报+周五周报）+ 退出码 0/1；_run_script 调用清单断言；
- P1 尾盘 kill 链：intraday_check 真子进程 rc==2 → catchup 步骤2c 传播 exit 2
  （子进程契约：AGSICKLE_DB/MOCK_QUOTES 等 env 隔离，kill 落库全部进临时库）；
- P2 premarket 降级与信号对齐（signal as_of 滞后触发步骤2a；空库 exit 1）；
- P2 midday --now 回放（真子进程 net_guard）：产物落 session/test/、绝不写 daily_bar、
  pending 漂移表、零流量；
- P3 webapp /api/confirm|/api/reject → monkeypatch run_runner 的 e2e。

已知测试陷阱（绕开）：postclose PENDING 分支只能在真实今天语义下测；midday run_date
恒为真实今天（--now 只影响内容与产物目录）；catchup flock 要求测试避免并发（本文件串行）。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
if str(BASE / "tests") not in sys.path:
    sys.path.insert(0, str(BASE / "tests"))

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import traceback
from datetime import date, datetime, time, timedelta
from typing import List

# ---- 测试隔离 env（必须在 import 任何项目模块之前设置）----
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="agsickle_pipefull_"))
# 本文件的 env 修改从不逐用例恢复（整文件隔离设计）；teardown_module 恢复，
# 避免与其它测试文件同进程跑（pytest tests/*.py）时泄漏 AGSICKLE_DB 等到后续文件
_ORIG_ENV = dict(os.environ)


def _restore_env():
    # 只动 AGSICKLE_ 前缀——碰其它键（如 PYTEST_CURRENT_TEST）会破坏 pytest 自身
    for k in list(os.environ):
        if k.startswith("AGSICKLE_") and k not in _ORIG_ENV:
            os.environ.pop(k, None)
    os.environ.update({k: v for k, v in _ORIG_ENV.items()
                       if k.startswith("AGSICKLE_")})


def teardown_module(module=None):
    _restore_env()


os.environ.setdefault("AGSICKLE_DISABLE_LIVE_QUOTES", "1")
os.environ.setdefault("AGSICKLE_DISABLE_NOTIFY", "1")
os.environ.setdefault("AGSICKLE_DISABLE_SLIPPAGE", "1")
os.environ.setdefault("AGSICKLE_DISABLE_FETCHER", "1")
os.environ.setdefault("AGSICKLE_DISABLE_NEWS", "1")
os.environ.setdefault("AGSICKLE_DISABLE_MACRO", "1")
os.environ.setdefault("AGSICKLE_DISABLE_SPOT", "1")
os.environ.setdefault("AGSICKLE_STATE_DIR", str(_TMP_ROOT / "state"))
os.environ.setdefault("AGSICKLE_ORDERS_DIR", str(_TMP_ROOT / "state"))
os.environ.setdefault("AGSICKLE_BACKUP_DIR", str(_TMP_ROOT / "backup"))
# signal_eval 沙箱：compute_all/premarket 会写 factor_crowding.json，缺此隔离时
# 直跑（run_all 之外）会把合成拥挤状态写进**生产** logs/signal_eval/
os.environ.setdefault("AGSICKLE_SIGNAL_EVAL_DIR", str(_TMP_ROOT / "signal_eval"))

_MOCK_QUOTES_FILE = _TMP_ROOT / "mock_quotes.json"

from data.fetcher import DDL
from data import repo
import pipeline.catchup as catchup
import review.daily as daily_mod
import review.weekly as weekly_mod
import signals.hot as hot_mod
import signals.movers as movers_mod


def _fresh_env(tag: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix="agsickle_pfull_%s_" % tag))
    os.environ["AGSICKLE_DB"] = str(d / "market.db")
    os.environ["AGSICKLE_REPORTS_DIR"] = str(d / "reports")
    os.environ["AGSICKLE_SESSION_DIR"] = str(d / "session")
    os.environ["AGSICKLE_STATE_DIR"] = str(d / "state")
    os.environ["AGSICKLE_ORDERS_DIR"] = str(d / "orders")
    os.environ["AGSICKLE_BACKUP_DIR"] = str(d / "backup")
    for sub in ("reports", "session", "state", "orders", "backup"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def _seed_db(bars: bool = True) -> str:
    """临时文件库：DDL + stock_info + daily_bar（昨日[+今日]）+ trade_calendar 全覆盖。"""
    conn = sqlite3.connect(os.environ["AGSICKLE_DB"])
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    today = date.today()
    today_str = today.isoformat()
    yday = (today - timedelta(days=1)).isoformat()
    if bars:
        for code in ("600519", "000001"):
            conn.execute("INSERT INTO stock_info VALUES (?,?,?,?)",
                         (code, "票" + code, "2024-01-02",
                          datetime.now().isoformat(timespec="seconds")))
            for td in [yday, today_str]:
                conn.execute(
                    "INSERT INTO daily_bar (code, trade_date, open, high, low, close,"
                    " volume, amount, pct_chg, turnover) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (code, td, 100.0, 100.0, 100.0, 100.0, 1000, 1e7, 0.0, 1.0))
    base = today - timedelta(days=200)
    conn.executemany("INSERT OR IGNORE INTO trade_calendar VALUES (?)",
                     [((base + timedelta(days=i)).isoformat(),) for i in range(400)])
    conn.commit()
    conn.close()
    return today_str


def _write_mock_quotes(payload: dict) -> None:
    _MOCK_QUOTES_FILE.write_text(json.dumps(payload), encoding="utf-8")
    os.environ["AGSICKLE_MOCK_QUOTES"] = str(_MOCK_QUOTES_FILE)


def _mock_quote(code: str, price: float, prev_close: float) -> dict:
    return {"code": code, "price": price, "prev_close": prev_close, "open": prev_close,
            "high": max(price, prev_close), "low": min(price, prev_close),
            "name": "票" + code, "source": "mock",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


class _ScriptStub:
    """catchup._run_script 注入：记录调用、可设返回值；subprocess.run 可选真跑。"""

    def __init__(self, ok: bool = True, real_subprocess: bool = False):
        self.calls: List[str] = []
        self.ok = ok
        self.real_subprocess = real_subprocess
        self._orig = {}

    def __enter__(self):
        self._orig = [(catchup, k, getattr(catchup, k)) for k in
                      ("REPORTS_DIR", "SESSION_DIR", "STATE_DIR", "HEARTBEAT_FILE",
                       "_run_script", "subprocess")]
        reports = Path(os.environ["AGSICKLE_REPORTS_DIR"])
        catchup.REPORTS_DIR = reports
        catchup.SESSION_DIR = Path(os.environ["AGSICKLE_SESSION_DIR"])
        catchup.STATE_DIR = Path(os.environ["AGSICKLE_STATE_DIR"])
        catchup.HEARTBEAT_FILE = catchup.STATE_DIR / "catchup_heartbeat"

        stub = self

        def fake_run_script(rel, timeout=900, extra=None):
            stub.calls.append(rel)
            return stub.ok

        catchup._run_script = fake_run_script
        if not self.real_subprocess:
            class _FakeProc:
                returncode = 0
                stdout = ""
                stderr = ""

            class _FakeSubprocess:
                TimeoutExpired = subprocess.TimeoutExpired

                @staticmethod
                def run(*a, **k):
                    stub.calls.append("subprocess:%s" % (a[0] if a else "?"))
                    return _FakeProc()

            catchup.subprocess = _FakeSubprocess()
        else:
            # 真跑模式：记录调用的包装器（子进程真实执行——子进程契约验证）
            import subprocess as _sp
            real_run = _sp.run

            def recording_run(*a, **k):
                stub.calls.append("subprocess:%s" % (a[0] if a else "?"))
                return real_run(*a, **k)

            class _RecordingSubprocess:
                TimeoutExpired = _sp.TimeoutExpired
                run = staticmethod(recording_run)

            catchup.subprocess = _RecordingSubprocess()

        # 日报/周报/动态池 stub（同 Phase 1a）
        self._orig += [(daily_mod, "generate_daily_report", daily_mod.generate_daily_report),
                       (weekly_mod, "weekly_report", weekly_mod.weekly_report),
                       (movers_mod, "refresh", movers_mod.refresh),
                       (hot_mod, "refresh", hot_mod.refresh)]

        def fake_report(trade_date=None, conn=None, out_dir=None):
            stub.calls.append("report:%s" % trade_date)
            p = reports / ("%s.md" % trade_date)
            p.write_text("# fake report %s" % trade_date, encoding="utf-8")
            return p

        def fake_weekly(trade_date=None, *a, **k):
            stub.calls.append("weekly:%s" % trade_date)
            return reports / "fake-weekly.md"

        daily_mod.generate_daily_report = fake_report
        weekly_mod.weekly_report = fake_weekly
        movers_mod.refresh = lambda conn, as_of=None, market_mode=True: {
            "mode": "watchlist", "count": 0, "rows": []}
        hot_mod.refresh = lambda conn, as_of=None: {
            "themes": [], "stocks": [], "boards": [], "count": 0}
        return self

    def __exit__(self, *exc):
        for mod, attr, val in reversed(self._orig):
            setattr(mod, attr, val)
        return False


# ---------------- P1: catchup 缺日补跑 + 退出码 0/1 ----------------

def test_catchup_backfill_missing_days_rc0_and_rc1():
    _fresh_env("backfill")
    today_str = _seed_db(bars=True)
    today = date.fromisoformat(today_str)
    # T-1 有 bar 无 state/report → 步骤1 应补跑（周五再加跑周报）
    with _ScriptStub(ok=True) as stub:
        rc = catchup.catch_up(now=datetime.combine(today, time(11, 35)))
    assert rc == 0, "全部脚本 stub 成功 → rc=0, calls=%s" % stub.calls
    yday = (today - timedelta(days=1)).isoformat()
    assert "report:%s" % yday in stub.calls, "步骤1 补了 T-1 日报: %s" % stub.calls
    conn = sqlite3.connect(os.environ["AGSICKLE_DB"])
    n = conn.execute("SELECT COUNT(*) FROM portfolio_state WHERE date=?",
                     (yday,)).fetchone()[0]
    conn.close()
    assert n == 1, "步骤1 补了 T-1 盯市行"
    # 盘中 11:35（<15:10）→ 不触发步骤4 的 postclose 重跑
    assert "pipeline/postclose.py" not in stub.calls
    # 调用清单：盘中兜底全开
    assert "data/fetcher.py" in stub.calls and "pipeline/premarket.py" in stub.calls \
        and "pipeline/midday.py" in stub.calls
    # 周五场景：T-1 是周五 → weekly 也应被调
    if date.fromisoformat(yday).weekday() == 4:
        assert any(c.startswith("weekly:") for c in stub.calls), stub.calls

    # ---- 失败注入：_run_script 全部失败 → rc=1 ----
    with _ScriptStub(ok=False) as stub:
        rc1 = catchup.catch_up(now=datetime.combine(today, time(11, 36)))
    assert rc1 == 1, "脚本失败 → rc=1（部分失败），got %d" % rc1


# ---------------- P1: 尾盘 kill 链（真子进程契约） ----------------

def test_kill_chain_real_subprocess_propagates_rc2():
    _fresh_env("killchain")
    today_str = _seed_db(bars=True)
    today = date.fromisoformat(today_str)
    conn = sqlite3.connect(os.environ["AGSICKLE_DB"])
    # 今日日线收盘即暴跌价：total_equity 走收盘价口径（broker.portfolio 的实时价依赖
    # 真实时钟处于盘中——测试时间语义陷阱），故用日线表达暴跌，不依赖实时价
    conn.execute(
        "UPDATE daily_bar SET close=91.0, high=91.0, low=91.0, open=91.0 "
        "WHERE code='600519' AND trade_date=?", (today_str,))
    # 持仓 9000 股 @cost 100：portfolio 现金走 trade 流水口径，必须补昨日买入流水
    # （amount=90 万）账本才自洽：现金 = 1000000 - 900000 = 100000。
    # trade_date=昨日（今日买入会 T+1 不可卖，kill 清仓将递延而非成交）
    conn.execute("INSERT INTO position VALUES ('600519','票600519',9000,9000,100.0,?)",
                 (datetime.now().isoformat(timespec="seconds"),))
    conn.execute(
        "INSERT INTO trade (trade_date, code, name, side, price, shares, amount,"
        " order_id, status, decision_id, shots, confirmed_by, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ((today - timedelta(days=1)).isoformat(), "600519", "票600519", "buy", 100.0,
         9000, 900000.0, "PAPER-seed", "filled", None, "[]", "seed",
         datetime.now().isoformat(timespec="seconds")))
    # 峰值 100 万（昨日行）→ 权益 100000 + 9000×91 = 919000 → 回撤 8.1% ≥ 8%
    # 卖出价 91 高于跌停 90（昨收 100×0.9）→ 清仓可成交
    conn.execute("INSERT INTO portfolio_state VALUES (?,?,?,?,?,?,?)",
                 ((today - timedelta(days=1)).isoformat(), 100000.0, 900000.0,
                  1000000.0, 0.0, 0, "峰值行"))
    conn.commit()
    conn.close()
    _write_mock_quotes({"600519": _mock_quote("600519", 91.0, 100.0),
                        "000001": _mock_quote("000001", 100.0, 100.0)})

    # _run_script stub（premarket/midday/fetcher 不真跑），但 subprocess.run 真跑——
    # 步骤2c 以真子进程执行 intraday_check.py（子进程契约）
    with _ScriptStub(ok=True, real_subprocess=True) as stub:
        rc = catchup.catch_up(now=datetime.combine(today, time(14, 55)))
    assert rc == 2, "kill 链应传播 exit 2, got %d, calls=%s" % (rc, stub.calls)
    assert any(c.startswith("subprocess:") and "intraday_check.py" in c
               for c in stub.calls), "步骤2c 真子进程执行: %s" % stub.calls
    # kill 后遗症全部落在隔离库/隔离目录
    conn = sqlite3.connect(os.environ["AGSICKLE_DB"])
    ks = conn.execute("SELECT kill_switch FROM portfolio_state "
                      "ORDER BY date DESC LIMIT 1").fetchone()[0]
    sells = conn.execute("SELECT COUNT(*) FROM trade WHERE side='sell' AND status='filled'"
                         ).fetchone()[0]
    ev = conn.execute("SELECT COUNT(*) FROM risk_event WHERE rule IN "
                      "('kill_switch','kill_liquidation_pending')").fetchone()[0]
    conn.close()
    assert int(ks) == 1, "portfolio_state 落 kill_switch=1"
    assert sells >= 1, "kill 清仓卖出成交"
    assert ev >= 1, "risk_event 留痕"
    kill_file = Path(os.environ["AGSICKLE_STATE_DIR"]) / "kill.json"
    assert kill_file.is_file(), "kill.json 落盘（72h 停机权威）"


# ---------------- P2: premarket 降级与信号对齐 ----------------

def test_catchup_signal_misalign_triggers_premarket():
    """bundle 当日新鲜但 signal as_of 滞后 → 步骤2a 仍触发（此前只看 mtime 的缺陷）。"""
    _fresh_env("sigalign")
    today_str = _seed_db(bars=True)
    today = date.fromisoformat(today_str)
    conn = sqlite3.connect(os.environ["AGSICKLE_DB"])
    conn.execute("INSERT INTO signal (code, as_of, signals, score, profile) VALUES "
                 "('600519', ?, '{}', 0.5, 'reversal_lowvol')", ((today - timedelta(days=1)).isoformat(),))
    conn.commit()
    conn.close()
    # 当日新鲜 bundle（mtime=今天）
    bundle_dir = Path(os.environ["AGSICKLE_SESSION_DIR"]) / today_str
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "bundle.md").write_text("# fresh bundle", encoding="utf-8")

    with _ScriptStub(ok=True) as stub:
        rc = catchup.catch_up(now=datetime.combine(today, time(10, 40)))
    assert rc == 0
    assert "pipeline/premarket.py" in stub.calls, "signal 滞后应触发步骤2a: %s" % stub.calls


def test_premarket_empty_db_exit1_real_subprocess():
    """空库（daily_bar 无任何数据）→ premarket 真子进程 exit 1。"""
    _fresh_env("pempty")
    _seed_db(bars=False)  # DDL 初始化但无行情
    _write_mock_quotes({})
    proc = subprocess.run(
        [sys.executable, str(BASE / "pipeline" / "premarket.py")],
        cwd=str(BASE), capture_output=True, text=True, timeout=180)
    assert proc.returncode == 1, "空库应 exit 1, got %d\n%s" % (
        proc.returncode, (proc.stdout + proc.stderr)[-400:])


# ---------------- P2: midday --now 回放（真子进程 + net_guard） ----------------

def _run_guarded(script_rel, *args, timeout=300):
    vlog = Path(os.environ["AGSICKLE_STATE_DIR"]) / "net_violations.txt"
    cmd = [sys.executable, str(BASE / "tests" / "net_guard.py"), str(vlog),
           str(BASE / script_rel)] + list(args)
    proc = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True,
                          timeout=timeout)
    violations = vlog.read_text(encoding="utf-8").splitlines() if vlog.is_file() \
        else ["(violations log missing)"]
    return proc.returncode, violations, proc.stdout + proc.stderr


def test_midday_replay_now_isolation():
    """midday --now 回放：产物落 session/test/、daily_bar 零写入、含 pending 漂移表、零流量。"""
    _fresh_env("midday")
    today_str = _seed_db(bars=True)
    # 持仓 + pending 单（漂移表素材）：委托价 95 vs mock 实时 100 → 漂移 5% > 2% 阈值的 75%
    conn = sqlite3.connect(os.environ["AGSICKLE_DB"])
    conn.execute("INSERT INTO position VALUES ('600519','票600519',100,0,100.0,?)",
                 (datetime.now().isoformat(timespec="seconds"),))
    conn.execute("INSERT INTO decision (run_date, code, action, target_weight, confidence,"
                 " reasons, risk_notes, input_snapshot, status, created_at)"
                 " VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (today_str, "600519", "buy", 0.05, 0.8, '["r"]', '[]', '{}',
                  "approved", datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()
    orders = Path(os.environ["AGSICKLE_ORDERS_DIR"]) / today_str
    orders.mkdir(parents=True, exist_ok=True)
    (orders / "pending_1.json").write_text(json.dumps({
        "decision_id": 1,
        "decision": {"code": "600519", "action": "buy",
                     "order": {"side": "buy", "price": 95.0, "shares": 100}},
        "submit_price": 95.0, "suggest_shares": 100,
        "valid_until": today_str + "T15:05:00"}), encoding="utf-8")
    _write_mock_quotes({"600519": _mock_quote("600519", 100.0, 100.0),
                        "000001": _mock_quote("000001", 100.0, 100.0)})

    conn = sqlite3.connect(os.environ["AGSICKLE_DB"])
    bars_before = conn.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0]
    conn.close()

    rc, violations, out = _run_guarded("pipeline/midday.py",
                                       "--now", "%sT11:35:00" % today_str)
    assert rc == 0, "midday 回放 rc=0, got %d\n%s" % (rc, out[-500:])
    assert violations == [], "midday 回放泄漏真实网络: %s" % violations

    test_dir = Path(os.environ["AGSICKLE_SESSION_DIR"]) / "test"
    assert (test_dir / "midday_bundle.md").is_file(), "产物落 session/test/"
    bundle = (test_dir / "midday_bundle.md").read_text(encoding="utf-8")
    assert "pending" in bundle.lower() and "漂移" in bundle, "bundle 含 pending 漂移表"
    assert "回放" in bundle, "bundle 标注 --now 回放口径"

    conn = sqlite3.connect(os.environ["AGSICKLE_DB"])
    bars_after = conn.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0]
    conn.close()
    assert bars_after == bars_before, "回放绝不写 daily_bar（%d → %d）" % (
        bars_before, bars_after)
    # 正式 session 目录不被污染
    assert not (Path(os.environ["AGSICKLE_SESSION_DIR"]) / today_str / "midday_bundle.md").is_file()


# ---------------- P3: webapp /api/confirm|reject e2e（monkeypatch run_runner） ----------------

def test_webapp_confirm_reject_e2e():
    """/api/confirm|/api/reject 走 HTTP 层，run_runner 打桩断言参数透传与响应形状。"""
    import importlib
    import threading
    import urllib.request
    from webapp import server as srv_mod
    srv_mod = importlib.reload(srv_mod)

    tmp = Path(tempfile.mkdtemp(prefix="agsickle_confirme2e_"))
    srv_mod.CONFIG_PATH = tmp / "config.json"
    srv_mod.CONFIG_PATH.write_text(json.dumps({"db_path": str(tmp / "m.db")}),
                                   encoding="utf-8")
    srv_mod._CONFIG_CACHE = {"mtime": None, "cfg": {}}

    calls: List[List[str]] = []

    def fake_run_runner(args, timeout=60):
        calls.append(list(args))
        return 0, "模拟 runner 输出 decision#%s" % (args[1] if len(args) > 1 else "?")

    orig = srv_mod.run_runner
    srv_mod.run_runner = fake_run_runner
    try:
        httpd = srv_mod.ThreadingHTTPServer(("127.0.0.1", 0), srv_mod.DashboardHandler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            def post(path, body):
                req = urllib.request.Request(
                    "http://127.0.0.1:%d%s" % (port, path),
                    data=json.dumps(body).encode("utf-8"),
                    headers={"Content-Type": "application/json",
                             "Host": "127.0.0.1",
                             "Origin": "http://127.0.0.1:%d" % port},
                    method="POST")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, json.loads(resp.read())
            st, body = post("/api/confirm", {"decision_id": 7, "by": "许文昊"})
            assert st == 200 and body["ok"] is True and "模拟 runner" in body["output"]
            assert calls[-1] == ["confirm", "--decision-id", "7", "--by", "许文昊"]
            st, body = post("/api/reject", {"decision_id": 9, "reason": "逻辑不符"})
            assert st == 200 and body["ok"] is True
            assert calls[-1][0] == "reject" and "--decision-id" in calls[-1]
            assert any("逻辑不符" in a for a in calls[-1])
        finally:
            httpd.shutdown()
            httpd.server_close()
    finally:
        srv_mod.run_runner = orig
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print("PASS %s" % name)
        except Exception:
            failed += 1
            print("FAIL %s" % name)
            traceback.print_exc()
    print("\n%d/%d tests passed" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
