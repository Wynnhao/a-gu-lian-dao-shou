"""pipeline 集成测试 · P0 行为级场景（结构性重构 Phase 1a，docs/结构性重构实施方案.md）。

场景（全走公开 API，不耦合实现细节）：
1. pending 15:05 TTL 边界 + 跨日 expired 全链路：decision 置 expired/rejected、
   pending 文件清理、risk_event(pending_expired) 落痕；
2. postclose 无当日日线 → PENDING + exit 2（net_guard 黑盒真脚本）→ 日线到位后
   catchup 15:10 闭环（进程内注入 now + stub _run_script 断言调用清单）；
3. postclose 幂等双跑：state 不重复、报告不重复、两次 rc=0。

零流量验收（tcpdump 的无 root 等效方案）：全部黑盒场景经 tests/net_guard.py 跑，
audit hook 拦截 socket/urllib 事件，违规清单断言为空；进程内网络面（movers 全市场
快照/hot 板块榜）一律 patch 掉，同进程绝不发起真实 HTTP。

mock 层次：进程内场景用 :memory: 库（DDL 来自 data.fetcher，不碰真实库）+ 时间注入
（catch_up(now=) / runner.confirm(now=)）；黑盒场景用 AGSICKLE_DB 临时文件库走真脚本；
输出目录全走 AGSICKLE_* env 隔离。
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
from typing import List, Optional, Tuple

# ---- 测试隔离 env（必须在 import 任何项目模块之前设置）----
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="agsickle_pipeline_"))
os.environ.setdefault("AGSICKLE_DISABLE_LIVE_QUOTES", "1")  # 实时行情保持离线
os.environ.setdefault("AGSICKLE_DISABLE_NOTIFY", "1")       # 不弹系统通知
os.environ.setdefault("AGSICKLE_DISABLE_SLIPPAGE", "1")
os.environ.setdefault("AGSICKLE_DISABLE_FETCHER", "1")      # 日线采集短路
os.environ.setdefault("AGSICKLE_DISABLE_NEWS", "1")
os.environ.setdefault("AGSICKLE_DISABLE_MACRO", "1")
os.environ.setdefault("AGSICKLE_DISABLE_SPOT", "1")         # 全市场快照/板块榜短路（movers/hot）
os.environ.setdefault("AGSICKLE_STATE_DIR", str(_TMP_ROOT / "state"))
os.environ.setdefault("AGSICKLE_ORDERS_DIR", str(_TMP_ROOT / "state"))
os.environ.setdefault("AGSICKLE_BACKUP_DIR", str(_TMP_ROOT / "backup"))
# signal_eval 沙箱：compute_all/premarket 会写 factor_crowding.json，缺此隔离时
# 直跑（run_all 之外）会把合成拥挤状态写进**生产** logs/signal_eval/
os.environ.setdefault("AGSICKLE_SIGNAL_EVAL_DIR", str(_TMP_ROOT / "signal_eval"))

_MOCK_QUOTES_FILE = _TMP_ROOT / "mock_quotes.json"
_MOCK_QUOTES_FILE.write_text(json.dumps({
    "600519": {"price": 1500.0, "prev_close": 1490.0, "open": 1495.0, "high": 1510.0,
               "low": 1488.0, "name": "贵州茅台", "source": "mock",
               "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
    "000001": {"price": 11.0, "prev_close": 10.9, "open": 10.95, "high": 11.1,
               "low": 10.88, "name": "平安银行", "source": "mock",
               "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
}), encoding="utf-8")

from data.fetcher import DDL
from test_execution import fresh_conn, seed_market, mk_decision, NOW10, _BASE
from execution import runner
import pipeline.catchup as catchup
import review.daily as daily_mod
import review.weekly as weekly_mod
import signals.hot as hot_mod
import signals.movers as movers_mod


# ---------------------------------------------------------------- 基础设施

def _fresh_env(tag: str) -> Path:
    """每个场景独立的一套临时目录 + 库。

    get_conn 的 AGSICKLE_DB 与报告目录的 AGSICKLE_REPORTS_DIR 都是调用时读 env，
    子进程继承当前进程 env，因此这里改完 env 后进程内/子进程两侧同时生效。
    """
    d = Path(tempfile.mkdtemp(prefix="agsickle_pipe_%s_" % tag))
    os.environ["AGSICKLE_DB"] = str(d / "market.db")
    os.environ["AGSICKLE_REPORTS_DIR"] = str(d / "reports")
    os.environ["AGSICKLE_SESSION_DIR"] = str(d / "session")
    os.environ["AGSICKLE_STATE_DIR"] = str(d / "state")
    os.environ["AGSICKLE_ORDERS_DIR"] = str(d / "state")
    os.environ["AGSICKLE_BACKUP_DIR"] = str(d / "backup")
    os.environ["AGSICKLE_MOCK_QUOTES"] = str(_MOCK_QUOTES_FILE)
    (d / "reports").mkdir(parents=True, exist_ok=True)
    (d / "session").mkdir(exist_ok=True)
    (d / "state").mkdir(exist_ok=True)
    (d / "backup").mkdir(exist_ok=True)
    return d


def _seed_db(with_today: bool) -> str:
    """临时文件库：DDL + stock_info + daily_bar（昨日[+今日]）+ trade_calendar。

    日历 seed「今天前后各 200 天每天」，保证任何自然日跑测试时 catchup 的
    is_trading_day 都把今天判为交易日（周末跑测试不秒退）。
    """
    conn = sqlite3.connect(os.environ["AGSICKLE_DB"])
    conn.row_factory = sqlite3.Row  # 与生产 fetcher.get_conn 一致
    conn.executescript(DDL)
    today = date.today()
    today_str = today.isoformat()
    yday = (today - timedelta(days=1)).isoformat()  # postclose 只做字符串比较
    for code, name in (("600519", "贵州茅台"), ("000001", "平安银行")):
        conn.execute("INSERT INTO stock_info VALUES (?,?,?,?)",
                     (code, name, "2024-01-02",
                      datetime.now().isoformat(timespec="seconds")))
        for td in ([yday] if not with_today else [yday, today_str]):
            conn.execute(
                "INSERT INTO daily_bar (code, trade_date, open, high, low, close,"
                " volume, amount, pct_chg, turnover) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (code, td, 10.0, 10.0, 10.0, 10.0, 1000, 1000000.0, 0.0, 1.0))
    base = today - timedelta(days=200)
    conn.executemany("INSERT OR IGNORE INTO trade_calendar VALUES (?)",
                     [((base + timedelta(days=i)).isoformat(),) for i in range(400)])
    conn.commit()
    conn.close()
    return today_str


def _run_guarded(script_rel: str, *args: str, timeout: int = 300) -> Tuple[int, List[str]]:
    """net_guard 包裹跑项目脚本（子进程黑盒），返回 (退出码, 网络违规清单)。"""
    vlog = Path(os.environ["AGSICKLE_STATE_DIR"]) / "net_violations.txt"
    cmd = [sys.executable, str(BASE / "tests" / "net_guard.py"), str(vlog),
           str(BASE / script_rel)] + list(args)
    proc = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True,
                          timeout=timeout)
    violations = vlog.read_text(encoding="utf-8").splitlines() if vlog.is_file() \
        else ["(violations log missing)"]
    return proc.returncode, violations


class _CatchupPatches:
    """进程内跑 catch_up 的注入集合：目录常量 + 子进程脚本 + 网络面全 stub。"""

    def __init__(self, record: List[str]):
        self.record = record
        self._orig: list = []

    def __enter__(self):
        self._orig = [
            (catchup, "REPORTS_DIR", catchup.REPORTS_DIR),
            (catchup, "SESSION_DIR", catchup.SESSION_DIR),
            (catchup, "STATE_DIR", catchup.STATE_DIR),
            (catchup, "HEARTBEAT_FILE", catchup.HEARTBEAT_FILE),
            (catchup, "_run_script", catchup._run_script),
            (catchup, "subprocess", catchup.subprocess),
            (daily_mod, "generate_daily_report", daily_mod.generate_daily_report),
            (weekly_mod, "weekly_report", weekly_mod.weekly_report),
            (movers_mod, "refresh", movers_mod.refresh),
            (hot_mod, "refresh", hot_mod.refresh),
        ]
        # 目录常量改指隔离 env（import 期固化的修正）
        reports = Path(os.environ["AGSICKLE_REPORTS_DIR"])
        catchup.REPORTS_DIR = reports
        catchup.SESSION_DIR = Path(os.environ["AGSICKLE_SESSION_DIR"])
        catchup.STATE_DIR = Path(os.environ["AGSICKLE_STATE_DIR"])
        catchup.HEARTBEAT_FILE = catchup.STATE_DIR / "catchup_heartbeat"

        def fake_run_script(rel, timeout=900, extra=None):
            self.record.append(rel)
            return True

        class _FakeProc:
            returncode = 0
            stdout = ""
            stderr = ""

        class _FakeSubprocess:
            TimeoutExpired = subprocess.TimeoutExpired

            @staticmethod
            def run(*a, **k):
                self.record.append("subprocess:%s" % (a[0] if a else "?"))
                return _FakeProc()

        catchup._run_script = fake_run_script
        catchup.subprocess = _FakeSubprocess()

        # 日报/周报/动态池：写隔离目录的确定性 stub
        # （generate_daily_report 对 trade_date<today 一律改写 PENDING 的兜底会把
        #  catchup 补历史日报拦成 PENDING 文件，行为存疑，测试不依赖它——见交付纪要）
        def fake_generate_daily_report(trade_date=None, conn=None, out_dir=None):
            self.record.append("report:%s" % trade_date)
            p = Path(os.environ["AGSICKLE_REPORTS_DIR"]) / ("%s.md" % trade_date)
            p.write_text("# fake report %s" % trade_date, encoding="utf-8")
            return p

        def fake_weekly(trade_date=None, *a, **k):
            self.record.append("weekly:%s" % trade_date)
            return Path(os.environ["AGSICKLE_REPORTS_DIR"]) / "fake-weekly.md"

        daily_mod.generate_daily_report = fake_generate_daily_report
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


# ---------------- 场景 1：pending TTL 边界 + 跨日 expired 全链路 ----------------

def test_pending_ttl_boundary_and_crossday_full_chain():
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(tempfile.mkdtemp(prefix="pipe_test_orders_"))
    try:
        n_expired_events = lambda: conn.execute(
            "SELECT COUNT(*) FROM risk_event WHERE rule='pending_expired'").fetchone()[0]

        # 同 conn 四条决策必须参数互异（_dedupe_check 会跳过同日同参数决策）
        # #1 对照组：盘中有效期内确认 → 正常执行
        v1 = runner.propose(conn, mk_decision("buy", "000001", 11.0, 100),
                            now=NOW10, orders_dir=orders)
        assert v1.approved and len(runner.list_pending(orders)) == 1
        res = runner.confirm(conn, 1, confirmed_by="场景1", now=NOW10, orders_dir=orders)
        assert res is not None and res["ok"]
        assert conn.execute("SELECT status FROM decision WHERE id=1").fetchone()[0] \
            == "executed"
        assert runner.list_pending(orders) == []       # pending 文件清理

        # #2 TTL 边界内 15:05:00（边界含）：不产生 expired，但重跑风控按非时段拒绝
        v2 = runner.propose(conn, mk_decision("buy", "000001", 11.0, 200),
                            now=NOW10, orders_dir=orders)
        assert v2.approved
        before = n_expired_events()
        late_edge = datetime.combine(_BASE, time(15, 5, 0))
        res2 = runner.confirm(conn, 2, confirmed_by="场景1", now=late_edge,
                              orders_dir=orders)
        assert res2 is None
        st2 = conn.execute("SELECT status FROM decision WHERE id=2").fetchone()[0]
        assert st2 != "expired", st2                   # 边界内不置 expired
        assert n_expired_events() == before            # 且无 pending_expired 落痕
        assert runner.list_pending(orders) == []       # pending 仍被清理

        # #3 TTL 过 15:05:01：expired + pending 清理 + risk_event 落痕
        v3 = runner.propose(conn, mk_decision("buy", "000001", 11.0, 300),
                            now=NOW10, orders_dir=orders)
        assert v3.approved
        late = datetime.combine(_BASE, time(15, 5, 1))
        res3 = runner.confirm(conn, 3, confirmed_by="场景1", now=late, orders_dir=orders)
        assert res3 is None
        st3 = conn.execute("SELECT status FROM decision WHERE id=3").fetchone()[0]
        assert st3 == "expired", st3
        assert runner.list_pending(orders) == []
        ev = conn.execute(
            "SELECT detail FROM risk_event WHERE rule='pending_expired' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        assert ev is not None and "decision#3" in ev[0] and "15:05" in ev[0]

        # #4 跨日：昨日决策今日确认 → expired（决策依据已失效）
        v4 = runner.propose(conn, mk_decision("buy", "000001", 11.0, 400),
                            now=NOW10, orders_dir=orders)
        assert v4.approved
        next_day = datetime.combine(_BASE + timedelta(days=1), time(10, 0))
        res4 = runner.confirm(conn, 4, confirmed_by="场景1", now=next_day,
                              orders_dir=orders)
        assert res4 is None
        st4 = conn.execute("SELECT status FROM decision WHERE id=4").fetchone()[0]
        assert st4 == "expired", st4
        assert runner.list_pending(orders) == []
        ev4 = conn.execute(
            "SELECT detail FROM risk_event WHERE rule='pending_expired' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        assert ev4 is not None and "decision#4" in ev4[0] and "跨日" in ev4[0]
    finally:
        shutil.rmtree(orders, ignore_errors=True)


# ------------- 场景 2：postclose PENDING exit 2 → catchup 15:10 闭环 -------------

def test_postclose_pending_exit2_then_catchup_closes_loop():
    _fresh_env("loop")
    today_str = _seed_db(with_today=False)          # 库内只有昨日日线
    pending = Path(os.environ["AGSICKLE_REPORTS_DIR"]) / ("PENDING-%s.md" % today_str)

    # 2a 黑盒 postclose：无当日日线 → PENDING 兜底 + exit 2（零流量守卫）
    rc, violations = _run_guarded("pipeline/postclose.py")
    assert rc == 2, "postclose 应 exit 2, got %d\nstderr见运行日志" % rc
    assert pending.is_file(), "PENDING 兜底文件未写出"
    assert violations == [], "黑盒 postclose 泄漏真实网络: %s" % violations
    conn = sqlite3.connect(os.environ["AGSICKLE_DB"])
    n_state = conn.execute(
        "SELECT COUNT(*) FROM portfolio_state WHERE date=?", (today_str,)).fetchone()[0]
    conn.close()
    assert n_state == 0, "PENDING 退出路径不得写当日 state"

    # 2b 日线到位 → catchup（注入 15:20）触发步骤4 重跑 postclose 闭环
    conn = sqlite3.connect(os.environ["AGSICKLE_DB"])
    for code in ("600519", "000001"):
        conn.execute(
            "INSERT INTO daily_bar (code, trade_date, open, high, low, close,"
            " volume, amount, pct_chg, turnover) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (code, today_str, 10.2, 10.2, 10.2, 10.2, 1000, 1000000.0, 2.0, 1.0))
    conn.commit()
    conn.close()

    calls: List[str] = []
    with _CatchupPatches(calls):
        rc2 = catchup.catch_up(now=datetime.combine(date.today(), time(15, 20)))
    assert rc2 == 0, "catchup 闭环应无失败项, rc=%d, calls=%s" % (rc2, calls)
    assert "pipeline/postclose.py" in calls, "步骤4 未重跑 postclose: %s" % calls
    assert not pending.exists(), "闭环后 PENDING 文件未被清除"

    # 2c 闭环后半段语义：postclose 真跑时 mark_to_market 写出的 note 含「价格日期=今天」
    st = daily_mod.mark_to_market(today_str)
    assert "价格日期=%s" % today_str in st["note"], st["note"]
    conn = sqlite3.connect(os.environ["AGSICKLE_DB"])
    row = conn.execute(
        "SELECT note FROM portfolio_state WHERE date=?", (today_str,)).fetchone()
    conn.close()
    assert row is not None and "价格日期=%s" % today_str in row[0]


# ----------------------- 场景 3：postclose 幂等双跑 -----------------------

def test_postclose_idempotent_double_run():
    _fresh_env("idem")
    today_str = _seed_db(with_today=True)           # 当日日线已入库 → 正常路径
    reports = Path(os.environ["AGSICKLE_REPORTS_DIR"])

    rc1, v1 = _run_guarded("pipeline/postclose.py")
    rc2, v2 = _run_guarded("pipeline/postclose.py")
    assert rc1 == 0 and rc2 == 0, "两次 postclose 均 rc=0, got %d/%d" % (rc1, rc2)
    assert v1 == [] and v2 == [], "黑盒 postclose 泄漏真实网络: %s / %s" % (v1, v2)

    conn = sqlite3.connect(os.environ["AGSICKLE_DB"])
    n_state = conn.execute(
        "SELECT COUNT(*) FROM portfolio_state WHERE date=?", (today_str,)).fetchone()[0]
    conn.close()
    assert n_state == 1, "INSERT OR REPLACE 语义：当日 state 恰 1 行, got %d" % n_state

    daily_report = reports / ("%s.md" % today_str)
    assert daily_report.is_file(), "日报未生成"
    # 第二跑后仍只有一份当日日报（覆盖写，不产生副本/编号文件）
    dupes = [p for p in reports.glob("*.md")
             if p.name.startswith(today_str) and p.name != today_str + ".md"]
    assert dupes == [], "双跑产生重复报告: %s" % dupes
    # 通知已由 AGSICKLE_DISABLE_NOTIFY=1 短路：两次跑均无系统通知副作用（无法断言，语义见 docstring）


# ----------------------- C-ARC-3b/T6：盘中分钟快照录制器 -----------------------

def _patch_fetch_snapshot(snap_fn):
    """进程内 stub 录制取数面（fetch_snapshot 不走 MOCK_QUOTES 逃生门，直 patch）。"""
    import pipeline.recorder as rec
    orig = rec.quotes.fetch_snapshot
    rec.quotes.fetch_snapshot = snap_fn
    return orig


def _patch_recorder_conn(conn):
    """main() 内部走 fetcher.get_conn()——patch 到调用方的 :memory: 连接，
    绝不让测试摸到 AGSICKLE_DB/生产库（C-TEST-4）。main() 退出时会 close，
    用 no-close 代理保护真实连接。"""
    import pipeline.recorder as rec

    class _NoCloseConn:
        def __init__(self, c):
            self._c = c

        def close(self):
            pass

        def __getattr__(self, name):
            return getattr(self._c, name)

    orig = rec.fetcher.get_conn
    rec.fetcher.get_conn = lambda: _NoCloseConn(conn)
    return orig


def test_recorder_grid_ts():
    """5 分钟栅格归整：向下取整到桶边界，秒/微秒清零。"""
    import pipeline.recorder as rec
    assert rec.grid_ts(datetime(2026, 9, 18, 12, 7, 43)) == "2026-09-18T12:05:00"
    assert rec.grid_ts(datetime(2026, 9, 18, 9, 30, 0)) == "2026-09-18T09:30:00"
    assert rec.grid_ts(datetime(2026, 9, 18, 15, 0, 59)) == "2026-09-18T15:00:00"
    assert rec.grid_ts(datetime(2026, 9, 18, 10, 4, 59)) == "2026-09-18T10:00:00"


def test_recorder_writes_idempotent_and_fetch_log_ok():
    """入库幂等：同一 5 分钟栅格重跑行数不增（INSERT OR REPLACE）；
    fetch_log 落 status='ok' 市场级行（code=minute_snapshot）。"""
    import pipeline.recorder as rec
    conn = fresh_conn()
    seed_market(conn)
    snap = {"600519": {"price": 1500.0, "volume": 100000.0, "amount": 1.5e9,
                       "prev_close": 1490.0, "time": "t", "source": "tencent_snapshot"},
            "000001": {"price": 11.0, "volume": 200000.0, "amount": 2.2e9,
                       "prev_close": 10.9, "time": "t", "source": "tencent_snapshot"},
            "000300": {"price": 3900.0, "volume": None, "amount": None,
                       "prev_close": 3890.0, "time": "t", "source": "tencent_snapshot"}}
    orig = _patch_fetch_snapshot(lambda codes, index_codes=None: dict(snap))
    try:
        now = datetime.combine(_BASE, time(10, 2))
        wanted, n, miss = rec.record_once(conn, now)
        assert (wanted, n) == (8, 3) and miss == 5      # seed 5 票 + 3 指数
        ts = rec.grid_ts(now)
        rows = conn.execute("SELECT code, price, source FROM minute_snapshot"
                            " WHERE ts=?", (ts,)).fetchall()
        assert {r[0] for r in rows} == {"600519", "000001", "000300"}
        # 重跑同栅格：行数不增（幂等）
        assert rec.record_once(conn, now)[1] == 3
        assert conn.execute("SELECT COUNT(*) FROM minute_snapshot WHERE ts=?",
                            (ts,)).fetchone()[0] == 3
        # 不同栅格：新增行
        rec.record_once(conn, datetime.combine(_BASE, time(10, 7)))
        assert conn.execute("SELECT COUNT(*) FROM minute_snapshot").fetchone()[0] == 6
        fl = conn.execute("SELECT status, rows FROM fetch_log WHERE"
                          " code='minute_snapshot'").fetchall()
        assert len(fl) == 3 and all(r[0] == "ok" and r[1] == 3 for r in fl), fl
    finally:
        rec.quotes.fetch_snapshot = orig
        conn.close()


def test_recorder_gates_holiday_and_lunch():
    """双 gate：假日（日历覆盖当年但当日不在）与午休直接秒退，零写库零 fetch_log。"""
    import pipeline.recorder as rec
    conn = fresh_conn()
    seed_market(conn)
    orig_snap = _patch_fetch_snapshot(
        lambda codes, index_codes=None: (_ for _ in ()).throw(AssertionError("不应取数")))
    orig_gc = _patch_recorder_conn(conn)
    try:
        # 假日 gate：日历覆盖当年但不含 _BASE
        conn.execute("INSERT INTO trade_calendar VALUES (?)",
                     ((_BASE + timedelta(days=7)).isoformat(),))
        conn.commit()
        assert rec.main(["--now", _BASE.isoformat()]) == 0
        # 午休 gate：清日历恢复 weekday 语义，12:00 非连续竞价
        conn.execute("DELETE FROM trade_calendar")
        conn.commit()
        lunch = datetime.combine(_BASE, time(12, 0))
        assert rec.main(["--now", lunch.isoformat()]) == 0
        assert conn.execute("SELECT COUNT(*) FROM minute_snapshot").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM fetch_log WHERE"
                            " code='minute_snapshot'").fetchone()[0] == 0
    finally:
        rec.quotes.fetch_snapshot = orig_snap
        rec.fetcher.get_conn = orig_gc
        conn.close()


def test_recorder_total_failure_is_fail_loud():
    """整轮失败 fail-loud：快照 0 行 → rc=1 + fetch_log status='fail'（缺测率验收口径）。"""
    import pipeline.recorder as rec
    conn = fresh_conn()
    seed_market(conn)
    orig_snap = _patch_fetch_snapshot(lambda codes, index_codes=None: {})
    orig_gc = _patch_recorder_conn(conn)
    try:
        rc = rec.main(["--now", datetime.combine(_BASE, time(10, 2)).isoformat()])
        assert rc == 1
        fl = conn.execute("SELECT status, rows, detail FROM fetch_log WHERE"
                          " code='minute_snapshot'").fetchone()
        assert fl is not None and fl[0] == "fail" and fl[1] == 0
        assert "miss" in fl[2]
    finally:
        rec.quotes.fetch_snapshot = orig_snap
        rec.fetcher.get_conn = orig_gc
        conn.close()


def test_recorder_wal_concurrent_writer():
    """并发写与 WAL 兼容：另一连接先行写入的行不被录制器覆盖/丢失（AGSICKLE_DB 文件库）。"""
    import pipeline.recorder as rec
    from data.fetcher import get_conn
    d = Path(tempfile.mkdtemp(prefix="agsickle_rec_wal_"))
    old_db = os.environ.get("AGSICKLE_DB")
    os.environ["AGSICKLE_DB"] = str(d / "market.db")
    try:
        conn = get_conn()
        seed_market(conn)
        conn.close()
        # 连接 A：预写一行（另一进程语义）
        a = get_conn()
        a.execute("INSERT OR REPLACE INTO minute_snapshot VALUES (?,?,?,?,?,?)",
                  ("999999", "2026-09-18T10:05:00", 1.0, None, None, "other"))
        a.commit()
        orig = _patch_fetch_snapshot(lambda codes, index_codes=None: {
            "600519": {"price": 1500.0, "volume": None, "amount": None,
                       "prev_close": 1490.0, "time": "t", "source": "tencent_snapshot"}})
        try:
            assert rec.main(["--now", datetime.combine(_BASE, time(10, 7)).isoformat()]) == 0
        finally:
            rec.quotes.fetch_snapshot = orig
        b = get_conn()
        rows = {r[0] for r in b.execute(
            "SELECT code FROM minute_snapshot WHERE ts LIKE '2026-09-18T10:%'")}
        assert "999999" in rows and "600519" in rows   # 两连接的行都健在
        b.close()
        a.close()
    finally:
        if old_db is None:
            os.environ.pop("AGSICKLE_DB", None)
        else:
            os.environ["AGSICKLE_DB"] = old_db
        shutil.rmtree(d, ignore_errors=True)


# ----------------------- 直接运行入口 -----------------------

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
