"""Fix-4（D1）：规则 21 跌停应急主动扫描器测试。

覆盖：扫描器三条件命中/不命中、propose 入库幂等、stuck 计数递增 + 5 日
risk_event、同日 ≥3 只 stuck → kill、盘前 09:14 超时兜底自动执行。

全离线：AGSICKLE_DISABLE_LIVE_QUOTES/NOTIFY=1；:memory: 库 + ctx_inputs 注入，
不触生产 DB 与实时行情。
"""
import json
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

os.environ.setdefault("AGSICKLE_DISABLE_LIVE_QUOTES", "1")
os.environ.setdefault("AGSICKLE_DISABLE_NOTIFY", "1")

from data.fetcher import DDL, init_db  # noqa: E402
from signals import limit_halt  # noqa: E402

CFG = {"stop_loss_pct": 0.08, "atr_stop_mult": 2.0}


def set_emergency_direct(enabled: bool):
    """临时切换 execution.emergency_direct_exec（Fix F：兜底/直写总开关）。"""
    from execution import runner as _runner
    if "execution" not in _runner.CFG:
        _runner.CFG["execution"] = {}

    class _ED:
        def __enter__(self):
            self.old = _runner.CFG["execution"].get("emergency_direct_exec", False)
            _runner.CFG["execution"]["emergency_direct_exec"] = enabled
            return self

        def __exit__(self, *exc):
            _runner.CFG["execution"]["emergency_direct_exec"] = self.old
            return False
    return _ED()


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    return conn


def _seed_hit(conn, code: str = "600519", prev: float = 100.0, last: float = 90.0,
              cost: float = 110.0, avail: int = 200):
    """构造满足三条件的库内数据：昨收 prev、今收 last（跌停）、浮亏破 8% 线。"""
    d1 = (date.today() - timedelta(days=1)).isoformat()
    d2 = date.today().isoformat()
    conn.execute("INSERT OR REPLACE INTO stock_info VALUES (?,?,?,?)",
                 (code, "测试票", "2020-01-01", "x"))
    for d, cl in ((d1, prev), (d2, last)):
        conn.execute(
            "INSERT OR REPLACE INTO daily_bar (code, trade_date, open, high, low,"
            " close, volume, amount, pct_chg, turnover) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (code, d, cl, cl, cl, cl, 10_000_000, cl * 10_000_000, 0.0, 1.0))
    conn.execute(
        "INSERT OR REPLACE INTO position (code, name, shares, avail_shares, cost,"
        " updated_at) VALUES (?,?,?,?,?,?)",
        (code, "测试票", avail, avail, cost, d2))
    conn.commit()


def _hit_ctx_inputs(code: str = "600519", prev: float = 100.0, last: float = 90.0,
                    cost: float = 110.0, avail: int = 200) -> dict:
    return {
        "positions": {code: {"name": "测试票", "shares": avail,
                             "avail_shares": avail, "cost": cost}},
        "prev_close": {code: prev},
        "latest_prices": {code: last},
        "atr_pct": {},
        "live_quotes": {},
    }


# ---------------- 1. 扫描器三条件 ----------------

def test_scan_hit_and_miss():
    """命中：跌停价 + 浮亏破线 + ②缺数据不阻断 → 生成 emergency sell；
    不命中：浮亏未破线 / avail_shares=0（T+1）→ 不生成。"""
    now = datetime(2026, 9, 17, 15, 30, 0)
    ds = limit_halt.scan_positions(_mem_conn(), ctx_inputs=_hit_ctx_inputs(),
                                   cfg=CFG, now=now)
    assert len(ds) == 1
    d = ds[0]
    assert d["code"] == "600519" and d["action"] == "sell"
    assert abs(d["order"]["price"] - 90.0) < 1e-9   # 跌停价
    assert d["order"]["shares"] == 200              # avail_shares
    assert d["emergency_scan"] is True
    # 浮亏未破线（cost=95, price=90 → 5.26% < 8%）
    ds2 = limit_halt.scan_positions(
        _mem_conn(), ctx_inputs=_hit_ctx_inputs(cost=95.0), cfg=CFG, now=now)
    assert ds2 == []
    # T+1 不可卖
    ds3 = limit_halt.scan_positions(
        _mem_conn(), ctx_inputs=_hit_ctx_inputs(avail=0), cfg=CFG, now=now)
    assert ds3 == []


def test_scan_miss_when_not_limit_down():
    """P0-1：现价未跌停（即使浮亏破线）→ 不生成应急单。

    昨收100/今收103(+3%，远离跌停90)/成本130(浮亏20.8%破线)：原缺陷会误判
    "跌停封死"生成 sell@90 应急单并谎报连续跌停，修复后条件①（现价≈跌停价）拦住。
    """
    now = datetime(2026, 9, 17, 15, 30, 0)
    ctx = _hit_ctx_inputs(prev=100.0, last=103.0, cost=130.0)
    ds = limit_halt.scan_positions(_mem_conn(), ctx_inputs=ctx, cfg=CFG, now=now)
    assert ds == [], f"未跌停的破线持仓不应生成应急单，实得 {ds}"


def test_scan_miss_when_latest_price_missing():
    """P0-1：现价缺失（latest_prices 无该票）→ 保守跳过，不生成应急单。"""
    now = datetime(2026, 9, 17, 15, 30, 0)
    ctx = _hit_ctx_inputs(prev=100.0, last=90.0, cost=110.0)
    ctx["latest_prices"] = {}   # 抹掉现价 → lp is None → 跳过
    ds = limit_halt.scan_positions(_mem_conn(), ctx_inputs=ctx, cfg=CFG, now=now)
    assert ds == [], f"现价缺失应保守跳过，实得 {ds}"


def _seed_intraday(conn, code: str = "600519", d0: float = 100.0, d1: float = 90.0,
                   cost: float = 130.0, avail: int = 200):
    """盘中场景：只有前日(d0)/昨日(d1)两根 bar（当日 bar 盘中未写入），昨收=d1。

    昨日 d1=90 恰为前日 100 的跌停价（模拟"昨日跌停、今日走势待判"的开局）。
    """
    t = date.today()
    conn.execute("INSERT OR REPLACE INTO stock_info VALUES (?,?,?,?)",
                 (code, "测试票", "2020-01-01", "x"))
    for off, cl in ((2, d0), (1, d1)):
        conn.execute(
            "INSERT OR REPLACE INTO daily_bar (code, trade_date, open, high, low,"
            " close, volume, amount, pct_chg, turnover) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (code, (t - timedelta(days=off)).isoformat(), cl, cl, cl, cl,
             10_000_000, cl * 10_000_000, 0.0, 1.0))
    conn.execute(
        "INSERT OR REPLACE INTO position (code, name, shares, avail_shares, cost,"
        " updated_at) VALUES (?,?,?,?,?,?)",
        (code, "测试票", avail, avail, cost, t.isoformat()))
    conn.commit()


# ---------------- 1b. Fix B：盘中 stuck 计数用当日实时快照 ----------------

def test_intraday_scan_live_limit_down_counted():
    """Fix B：当日真跌停（实时价=跌停价）→ 当日即计数，不再滞后一天。"""
    conn = _mem_conn()
    _seed_intraday(conn)                      # 前日100/昨日90（昨收90）
    now = datetime(2026, 9, 17, 14, 50, 0)
    r = limit_halt.run_intraday_scan(
        conn, now=now, latest_prices={"600519": 81.0},   # 今日实时=90×0.9=81
        prev_close={"600519": 90.0})
    assert r["hit"] == ["600519"], r
    assert r["stuck"]["days"] == {"600519": 1}


def test_intraday_scan_rebound_not_counted():
    """Fix B：昨日跌停、今日反弹（实时 99 ≫ 跌停 81）→ 不计数。

    原缺陷：盘中回退日线收盘（=昨收 90）比"前日基准跌停价 90"恒命中 →
    反弹票照样计数，3 只即假触发 72h kill。"""
    conn = _mem_conn()
    _seed_intraday(conn)
    now = datetime(2026, 9, 17, 14, 50, 0)
    r = limit_halt.run_intraday_scan(
        conn, now=now, latest_prices={"600519": 99.0},
        prev_close={"600519": 90.0})
    assert r["hit"] == [], r
    assert r["stuck"]["days"] == {}


def test_intraday_scan_quotes_outage_preserves_stuck():
    """Fix B：实时快照整体缺失（行情失败）→ 本轮不计数也**不清除**既有 stuck
    （宁可漏计一日，不可因断网把 5 日预警的计数清零）。"""
    conn = _mem_conn()
    _seed_intraday(conn)
    limit_halt.update_stuck(conn, ["600519"], "2026-09-16")   # 既有计数 1
    now = datetime(2026, 9, 17, 14, 50, 0)
    r = limit_halt.run_intraday_scan(conn, now=now, latest_prices={}, prev_close={})
    assert r["hit"] == [] and r["stuck"]["days"] == {}
    row = conn.execute("SELECT stuck_days FROM limit_halt_stuck"
                       " WHERE code='600519'").fetchone()
    assert row is not None and row[0] == 1, "断网轮不得清除既有 stuck 计数"


# ---------------- 2. propose 入库 + 幂等 ----------------

def test_postclose_scan_proposes_and_dedupes(monkeypatch=None):
    """run_postclose_scan：propose 入库（run_date=次日、emergency_scan=1、
    status=approved、pending 落盘）；重复扫描不重复生成（幂等）。"""
    from execution import runner as _runner
    conn = _mem_conn()
    _seed_hit(conn)
    orders_dir = Path(tempfile.mkdtemp(prefix="agsickle_lh_orders_"))
    orig_orders_dir = _runner.ORDERS_DIR
    _runner.ORDERS_DIR = orders_dir
    try:
        now = datetime(2026, 9, 17, 15, 30, 0)  # 盘后（规则4 豁免）
        r1 = limit_halt.run_postclose_scan(conn, now=now)
        assert r1["proposed"] == ["600519"], r1
        row = conn.execute(
            "SELECT run_date, code, action, status, emergency_scan FROM decision"
            " WHERE emergency_scan=1").fetchone()
        assert row[0] == "2026-09-18"          # run_date=预期执行日（次日）
        assert row[1] == "600519" and row[2] == "sell"
        assert row[3] == "approved"            # pending 等人工 confirm
        assert row[4] == 1
        # 幂等：同票同 run_date 重复扫描 → skipped
        r2 = limit_halt.run_postclose_scan(conn, now=now)
        assert r2["proposed"] == [] and r2["skipped"] == ["600519"]
        n = conn.execute("SELECT COUNT(*) FROM decision WHERE"
                         " emergency_scan=1").fetchone()[0]
        assert n == 1
    finally:
        _runner.ORDERS_DIR = orig_orders_dir
        conn.close()


# ---------------- 3. stuck 计数 + 5 日事件 ----------------

def test_stuck_increment_and_five_day_event():
    """stuck 递增；5 日写 risk_event（同日同票去重）；不再命中即解除删除。"""
    conn = _mem_conn()
    try:
        d1 = "2026-09-10"
        st1 = limit_halt.update_stuck(conn, ["600519"], d1)
        assert st1["days"] == {"600519": 1}
        st2 = limit_halt.update_stuck(conn, ["600519"], "2026-09-11")
        assert st2["days"] == {"600519": 2}
        # 解除：未命中 → 删除
        st3 = limit_halt.update_stuck(conn, [], "2026-09-12")
        assert st3["removed"] == ["600519"]
        # 5 日事件
        limit_halt.update_stuck(conn, ["600519"], "2026-09-09")
        for i in range(10, 14):
            limit_halt.update_stuck(conn, ["600519"], f"2026-09-{i}")
        now = datetime(2026, 9, 13, 14, 50, 0)
        ef = limit_halt.enforce_stuck_rules(conn, now=now)
        assert ef["events"] == ["600519"] and ef["kill"] is False
        n = conn.execute("SELECT COUNT(*) FROM risk_event WHERE"
                         " rule='limit_halt_stuck'").fetchone()[0]
        assert n == 1
        # 同日重复 enforce → 去重
        limit_halt.enforce_stuck_rules(conn, now=now)
        n2 = conn.execute("SELECT COUNT(*) FROM risk_event WHERE"
                          " rule='limit_halt_stuck'").fetchone()[0]
        assert n2 == 1
    finally:
        conn.close()


def test_stuck_three_codes_trigger_kill():
    """同日 ≥3 只 stuck → apply_kill_switch（portfolio_state.kill_switch=1）。"""
    conn = _mem_conn()
    try:
        today = "2026-09-17"
        limit_halt.update_stuck(conn, ["600519", "000001", "600036"], today)
        now = datetime(2026, 9, 17, 14, 50, 0)
        ef = limit_halt.enforce_stuck_rules(conn, now=now)
        assert ef["kill"] is True
        row = conn.execute(
            "SELECT kill_switch FROM portfolio_state ORDER BY date DESC"
            " LIMIT 1").fetchone()
        assert row is not None and row[0] == 1
    finally:
        conn.close()


# ---------------- 4. 盘前 09:14 超时兜底 ----------------

def _seed_pending_emergency(conn, run_date: str) -> int:
    """插入一笔 approved 的 emergency_scan sell 决策（模拟昨日盘后扫描产物）。"""
    decision = {
        "action": "sell", "code": "600519", "target_weight": 0.0,
        "confidence": 1.0,
        "reasons": ["规则21主动扫描：跌停封死且浮亏 18.2% ≥ 止损线 8.0%",
                    "连续跌停应急：次日 09:15 集合竞价挂跌停价卖出"],
        "risk_notes": ["emergency_scan 自动生成（D1：confirm 优先，09:14 未确认自动执行）"],
        "order": {"side": "sell", "price": 90.0, "shares": 200},
        "emergency_scan": True,
    }
    cur = conn.execute(
        "INSERT INTO decision (run_date, code, action, target_weight, confidence,"
        " reasons, risk_notes, input_snapshot, status, created_at, emergency_scan)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_date, "600519", "sell", 0.0, 1.0,
         json.dumps(decision["reasons"], ensure_ascii=False),
         json.dumps(decision["risk_notes"], ensure_ascii=False),
         json.dumps(decision, ensure_ascii=False), "approved",
         datetime.now().isoformat(timespec="seconds"), 1))
    conn.commit()
    return int(cur.lastrowid)


def test_failsafe_executes_after_0914():
    """09:14+ 仍未 confirm 且 emergency_direct_exec=true → 自动 confirm 执行，
    trade.confirmed_by=emergency_timeout_failsafe（审计链）。"""
    conn = _mem_conn()
    _seed_hit(conn)
    today = date.today().isoformat()
    did = _seed_pending_emergency(conn, run_date=today)
    from execution import runner as _runner
    orders_dir = Path(tempfile.mkdtemp(prefix="agsickle_lh_orders_"))
    orig_orders_dir = _runner.ORDERS_DIR
    _runner.ORDERS_DIR = orders_dir
    try:
        now = datetime.now().replace(hour=9, minute=14, second=30)
        with set_emergency_direct(True):
            out = limit_halt.premarket_failsafe(conn, now=now)
        assert out["pending"] == ["600519"]
        assert out["executed"] == ["600519"], out
        trow = conn.execute(
            "SELECT side, shares, confirmed_by, decision_id FROM trade"
            " WHERE decision_id=?", (did,)).fetchone()
        assert trow is not None, "应有成交记录"
        assert trow[0] == "sell" and trow[1] == 200
        assert trow[2] == "emergency_timeout_failsafe"
        status = conn.execute("SELECT status FROM decision WHERE id=?",
                              (did,)).fetchone()[0]
        assert status == "executed"
    finally:
        _runner.ORDERS_DIR = orig_orders_dir
        conn.close()


def test_failsafe_switch_off_notifies_only():
    """Fix F：emergency_direct_exec=false（默认）时，09:14+ 只提醒人工处理，
    不自动成交——自动兜底跟随总开关，与规则21 直写同一政策。"""
    conn = _mem_conn()
    _seed_hit(conn)
    today = date.today().isoformat()
    did = _seed_pending_emergency(conn, run_date=today)
    now = datetime.now().replace(hour=9, minute=14, second=30)
    with set_emergency_direct(False):
        out = limit_halt.premarket_failsafe(conn, now=now)
    assert out["executed"] == [] and out["notified"] is True, out
    status = conn.execute("SELECT status FROM decision WHERE id=?",
                          (did,)).fetchone()[0]
    assert status == "approved"
    assert conn.execute("SELECT COUNT(*) FROM trade").fetchone()[0] == 0
    conn.close()


def test_failsafe_before_0914_only_notifies():
    """09:14 之前（如 09:00）：只通知不执行，decision 保持 approved。"""
    conn = _mem_conn()
    _seed_hit(conn)
    today = date.today().isoformat()
    did = _seed_pending_emergency(conn, run_date=today)
    now = datetime.now().replace(hour=9, minute=0, second=0)
    with set_emergency_direct(True):   # 开关开也不得在 09:14 前执行
        out = limit_halt.premarket_failsafe(conn, now=now)
    assert out["executed"] == [] and out["notified"] is True
    status = conn.execute("SELECT status FROM decision WHERE id=?",
                          (did,)).fetchone()[0]
    assert status == "approved"
    n = conn.execute("SELECT COUNT(*) FROM trade").fetchone()[0]
    assert n == 0
    conn.close()


def test_failsafe_no_pending_is_noop():
    """无待确认应急单 → 空转。"""
    conn = _mem_conn()
    now = datetime.now().replace(hour=9, minute=14, second=30)
    out = limit_halt.premarket_failsafe(conn, now=now)
    assert out == {"pending": [], "executed": [], "notified": False}
    conn.close()


# ---------------- Sprint4 批次A：W-A5③ / W-A4③ / W-A4② ----------------

def test_dedupe_emergency_allows_regeneration_after_expired():
    """W-A5③：应急单被作废（expired）后同 run_date 可重新生成；proposed/approved
    或已有有效成交仍然挡重生。"""
    conn = _mem_conn()
    did = _seed_pending_emergency(conn, run_date="2026-09-18")
    assert limit_halt._dedupe_emergency(conn, "600519", "2026-09-18") is True
    # expired（旧跨日闸门产物）→ 不再挡重生（P1-4 链路死锁第三环）
    conn.execute("UPDATE decision SET status='expired' WHERE id=?", (did,))
    conn.commit()
    assert limit_halt._dedupe_emergency(conn, "600519", "2026-09-18") is False
    # rejected 同样不挡
    conn.execute("UPDATE decision SET status='rejected' WHERE id=?", (did,))
    conn.commit()
    assert limit_halt._dedupe_emergency(conn, "600519", "2026-09-18") is False
    # 已有有效成交（executed）→ 挡重生（已卖过，不再重复生成）
    conn.execute("UPDATE decision SET status='executed' WHERE id=?", (did,))
    conn.execute(
        "INSERT INTO trade (trade_date, code, name, side, price, shares, amount,"
        " order_id, status, decision_id, shots, confirmed_by, created_at)"
        " VALUES ('2026-09-18','600519','测试票','sell',90.0,200,17000.0,"
        " 'PAPER-X','filled',?, '[]','t','2026-09-18T09:15:00')", (did,))
    conn.commit()
    assert limit_halt._dedupe_emergency(conn, "600519", "2026-09-18") is True
    conn.close()


def test_stuck_first_day_not_counted_for_kill():
    """W-A4③（P1-7）：首日触板（first_stuck_date==今日）不计入"同日 ≥3 只 kill"
    聚集判定；次日仍在 stuck 才计入。"""
    conn = _mem_conn()
    try:
        real_today = datetime.now().strftime("%Y-%m-%d")
        # 三只票今日首触：不得 72h kill（"触碰跌停即 kill"已禁止）
        limit_halt.update_stuck(conn, ["600519", "000001", "600036"], real_today)
        now = datetime.combine(datetime.now().date(), datetime.strptime(
            "14:50", "%H:%M").time())
        ef = limit_halt.enforce_stuck_rules(conn, now=now)
        assert ef["kill"] is False, "首日触板不得触发全账户 kill"
        # 次日仍 stuck（first_stuck_date≠今日）→ 计入 → kill
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        conn.execute("DELETE FROM limit_halt_stuck")
        limit_halt.update_stuck(conn, ["600519", "000001", "600036"], yesterday)
        ef2 = limit_halt.enforce_stuck_rules(conn, now=now)
        assert ef2["kill"] is True
    finally:
        conn.close()


def test_postclose_scan_wires_live_quotes_seal():
    """W-A4②：盘后扫描自拉实时快照（AGSICKLE_MOCK_QUOTES 按实测协议注入）——
    规则21 条件②封单比真正参与**生成判定**：真死封（≥3%）生成应急单且理由留痕
    封单比；触板未封死（<3%）不判死封、不生成。"""
    from execution import runner as _runner

    def _run_with_mock(mock_quote: dict) -> dict:
        conn = _mem_conn()
        _seed_hit(conn)   # 昨收100/今收90（跌停）、成本110、avail 200
        mock = {"600519": dict({"price": 90.0, "prev_close": 100.0, "open": 90.0,
                                "high": 90.0, "low": 90.0, "time": "20260918150000",
                                "name": "测试票", "source": "tencent",
                                "limit_up": 110.0, "limit_down": 90.0},
                               **mock_quote)}
        tmp = tempfile.mkdtemp(prefix="lh_mock_quotes_")
        qf = Path(tmp) / "quotes.json"
        qf.write_text(json.dumps(mock, ensure_ascii=False), encoding="utf-8")
        old_mock = os.environ.get("AGSICKLE_MOCK_QUOTES")
        old_dis = os.environ.pop("AGSICKLE_DISABLE_LIVE_QUOTES", None)
        os.environ["AGSICKLE_MOCK_QUOTES"] = str(qf)
        orders_dir = Path(tempfile.mkdtemp(prefix="agsickle_lh_orders_"))
        orig_orders_dir = _runner.ORDERS_DIR
        _runner.ORDERS_DIR = orders_dir
        try:
            return limit_halt.run_postclose_scan(conn, now=datetime(2026, 9, 17, 15, 30))
        finally:
            if old_mock is None:
                os.environ.pop("AGSICKLE_MOCK_QUOTES", None)
            else:
                os.environ["AGSICKLE_MOCK_QUOTES"] = old_mock
            if old_dis is not None:
                os.environ["AGSICKLE_DISABLE_LIVE_QUOTES"] = old_dis
            _runner.ORDERS_DIR = orig_orders_dir
            conn.close()
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(orders_dir, ignore_errors=True)

    # 真死封：5000手×100 / (1.5e8/90) ≈ 30% ≥ 3% → 生成，理由含封单比
    r = _run_with_mock({"ask1_price": 90.0, "ask1_vol": 5000.0, "float_mv": 1.5e8})
    assert r["proposed"] == ["600519"], r

    # 触板未封死：封单比 100×100/(1.5e8/90) ≈ 0.6% < 3% → 不生成（W-A4 核心负例，
    # scanned=0 表示扫描器在生成前就按条件②拦下）
    r2 = _run_with_mock({"ask1_price": 90.0, "ask1_vol": 100.0, "float_mv": 1.5e8})
    assert r2["proposed"] == [] and r2["scanned"] == 0, r2


# ---------------- 直接运行入口 ----------------

if __name__ == "__main__":
    import traceback
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
