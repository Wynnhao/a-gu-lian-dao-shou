"""执行层测试：PaperBroker 买卖/费用/T+1/现金不足/回读一致性 + runner 上下文/闸门/拒绝/kill 联动。

纯 :memory: 库（DDL 来自 data.fetcher，不触碰真实 market.db）；风控时段用最近一个周三盘中。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import json
import os
import shutil
import sqlite3
import tempfile
import traceback
from datetime import date, datetime, time, timedelta
from typing import Dict, Optional

os.environ.setdefault("AGSICKLE_DISABLE_LIVE_QUOTES", "1")  # 测试保持离线确定价

from data.fetcher import DDL
from execution.paper import PaperBroker, compute_fees
from execution import runner

# 与 config.json 一致的纯内存配置（不读真实文件）
EXEC_CFG = {"mode": "paper", "manual_gate": True, "paper_start_cash": 1000000.0,
            "commission_rate": 0.00025, "min_commission": 5.0, "stamp_tax_rate": 0.0005}

DEFAULT_PRICES = {  # code: (最新收盘, 前一收盘)
    "600519": (1500.0, 1490.0), "000001": (11.0, 10.9), "601318": (54.83, 54.30),
    "300750": (330.51, 325.0), "688801": (50.0, 49.5),
}
NAMES = {"600519": "贵州茅台", "000001": "平安银行", "601318": "中国平安",
         "300750": "宁德时代", "688801": "N燧原-U"}


# 人造“当日”时钟：工作日用今天、周末回退到最近周五——保证 daily_bar 相对真实今天的
# 滞后 ≤2 天（health_check 通过），且 in_trading_session 把它视为交易日盘中。
_BASE = date.today() if date.today().weekday() < 5 else \
    date.today() - timedelta(days=date.today().weekday() - 4)
NOW10 = datetime.combine(_BASE, time(10, 0))                 # 交易盘中
SAT10 = NOW10 + timedelta(days=(5 - _BASE.weekday()) % 7)    # 其后的周六（非交易时段）
NOW_DATE = _BASE.isoformat()
YDAY = (_BASE - timedelta(days=1)).isoformat()               # 昨日（跨日 T+1 场景用）
NEXT_DAY = (_BASE + timedelta(days=1)).isoformat()           # 次日（解锁后卖出用）


def fresh_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    return conn


def seed_market(conn: sqlite3.Connection, prices: Optional[Dict[str, tuple]] = None) -> None:
    """seed stock_info + daily_bar（今日/昨日两根，health_check 与黑名单均可控）。"""
    prices = prices if prices is not None else DEFAULT_PRICES
    today = NOW_DATE
    yest = YDAY
    for code, (latest, prev) in prices.items():
        first = "2024-01-02" if code != "688801" else (date.today() - timedelta(days=3)).isoformat()
        conn.execute("INSERT INTO stock_info VALUES (?,?,?,?)",
                     (code, NAMES[code], first, datetime.now().isoformat(timespec="seconds")))
        for td, close in ((yest, prev), (today, latest)):
            conn.execute(
                "INSERT INTO daily_bar (code, trade_date, open, high, low, close,"
                " volume, amount, pct_chg, turnover) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (code, td, close, close, close, close, 1000, close * 1000, 0.0, 1.0))
    conn.commit()


def mk_decision(action: str, code: str, price: float, shares: int, **over) -> dict:
    d = {"action": action, "code": code, "target_weight": 0.05, "confidence": 0.8,
         "reasons": ["理由一", "理由二"], "risk_notes": [],
         "order": {"side": action, "price": price, "shares": shares}}
    d.update(over)
    return d


def set_gate(enabled: bool):
    """临时切换 runner 的人工闸门开关（用完必须 close()）。"""
    class _Gate:
        def __enter__(self):
            self.old = runner.CFG["execution"].get("manual_gate", True)
            runner.CFG["execution"]["manual_gate"] = enabled
            return self

        def __exit__(self, *exc):
            runner.CFG["execution"]["manual_gate"] = self.old
            return False
    return _Gate()


def trade_rows(conn, **cond):
    sql = "SELECT id, trade_date, code, side, price, shares, amount, order_id, status," \
          " shots, confirmed_by FROM trade"
    args = ()
    if cond:
        sql += " WHERE " + " AND ".join("%s=?" % k for k in cond)
        args = tuple(cond.values())
    return conn.execute(sql + " ORDER BY id", args).fetchall()


# ---------------- 费用计算 ----------------

def test_compute_fees_min_commission_and_normal():
    cfg = dict(EXEC_CFG)
    f = compute_fees("buy", 10.0, 100, cfg)          # gross 1000 → 0.25 < 5 → 最低佣金
    assert f["gross"] == 1000.0 and f["commission"] == 5.0
    assert f["stamp_tax"] == 0.0 and f["amount"] == 1005.0
    f = compute_fees("sell", 10.0, 100, cfg)          # 卖出另收印花税 0.5
    assert f["commission"] == 5.0 and f["stamp_tax"] == 0.5 and f["amount"] == 994.5
    f = compute_fees("buy", 1500.0, 100, cfg)         # gross 15万 → 佣金 37.5（超下限）
    assert f["commission"] == 37.5 and f["amount"] == 150037.5


# ---------------- buy / sell 断言 ----------------

def test_buy_cash_position_trade_row():
    conn = fresh_conn()
    seed_market(conn)
    b = PaperBroker(EXEC_CFG)
    b.ensure_account(conn)
    res = b.buy(conn, "000001", "平安银行", 11.0, 100, decision_id=None, confirmed_by="tester")
    assert res["ok"] and res["amount"] == 1105.0 and res["commission"] == 5.0
    assert res["cash_before"] == 1000000.0 and res["cash_after"] == 998895.0
    assert b.cash(conn) == 998895.0
    tid, _, code, side, price, shares, amount, oid, status, shots, by = trade_rows(
        conn, code="000001")[0]
    assert (code, side, price, shares, amount, status) == ("000001", "buy", 11.0, 100, 1105.0, "filled")
    assert oid.startswith("PAPER-") and shots == "[]" and by == "tester"
    code_, name, sh, avail, cost, _ = conn.execute(
        "SELECT code,name,shares,avail_shares,cost,updated_at FROM position").fetchone()
    assert (code_, name, sh, cost) == ("000001", "平安银行", 100, 11.0)


def test_buy_weighted_cost_and_cash():
    conn = fresh_conn()
    seed_market(conn)
    b = PaperBroker(EXEC_CFG)
    b.buy(conn, "000001", "平安银行", 11.0, 100)
    b.buy(conn, "000001", "平安银行", 12.0, 100)
    sh, avail, cost = conn.execute(
        "SELECT shares, avail_shares, cost FROM position WHERE code='000001'").fetchone()
    assert (sh, avail, cost) == (200, 0, 11.5)        # 加权成本 (1100+1200)/200
    assert b.cash(conn) == 1000000.0 - 1105.0 - 1205.0


def test_sell_stamp_tax_cash_and_position():
    conn = fresh_conn()
    seed_market(conn)
    b = PaperBroker(EXEC_CFG)
    b.buy(conn, "000001", "平安银行", 11.0, 100, trade_date=YDAY)   # 昨日买入
    b.unlock_t_plus_1(conn)
    res = b.sell(conn, "000001", "平安银行", 11.0, 40, confirmed_by="tester", trade_date=NOW_DATE)
    assert res["ok"]
    assert res["gross"] == 440.0 and res["commission"] == 5.0 and res["stamp_tax"] == 0.22
    assert res["amount"] == 434.78                    # 440 - 5 - 0.22
    assert b.cash(conn) == 1000000.0 - 1105.0 + 434.78
    sh, avail = conn.execute(
        "SELECT shares, avail_shares FROM position WHERE code='000001'").fetchone()
    assert (sh, avail) == (60, 60)
    row = trade_rows(conn, side="sell")[0]
    assert row[6] == 434.78 and row[8] == "filled" and row[9] == "[]"


def test_sell_clears_position_row():
    conn = fresh_conn()
    seed_market(conn)
    b = PaperBroker(EXEC_CFG)
    b.buy(conn, "000001", "平安银行", 11.0, 100, trade_date=YDAY)
    b.unlock_t_plus_1(conn)
    assert b.sell(conn, "000001", "平安银行", 11.0, 100, trade_date=NOW_DATE)["ok"]
    assert conn.execute("SELECT COUNT(*) FROM position").fetchone()[0] == 0


def test_t_plus_1_buy_today_avail_unchanged():
    conn = fresh_conn()
    seed_market(conn)
    b = PaperBroker(EXEC_CFG)
    b.buy(conn, "600519", "贵州茅台", 1500.0, 100)
    sh, avail = conn.execute(
        "SELECT shares, avail_shares FROM position WHERE code='600519'").fetchone()
    assert (sh, avail) == (100, 0)                    # T+1：当日新买不计入 avail
    assert b.sell(conn, "600519", "贵州茅台", 1500.0, 1) is None  # 当日不可卖
    b.unlock_t_plus_1(conn)
    assert b.sell(conn, "600519", "贵州茅台", 1500.0, 1, trade_date=NEXT_DAY)["ok"]  # 解锁后可卖


def test_buy_insufficient_cash_rejected():
    conn = fresh_conn()
    seed_market(conn)
    b = PaperBroker(EXEC_CFG)
    res = b.buy(conn, "600519", "贵州茅台", 1500.0, 700)   # 105万 + 佣金 > 100万
    assert res is None
    assert conn.execute("SELECT COUNT(*) FROM trade").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM position").fetchone()[0] == 0
    assert b.cash(conn) == 1000000.0                  # 现金未动
    # 贴边可用：990,247.5 ≤ 100万，应成功
    res2 = b.buy(conn, "600519", "贵州茅台", 1500.0, 660)
    assert res2["ok"] and res2["amount"] == 990247.5


def test_sell_over_avail_rejected():
    conn = fresh_conn()
    seed_market(conn)
    b = PaperBroker(EXEC_CFG)
    b.buy(conn, "000001", "平安银行", 11.0, 100)
    assert b.sell(conn, "000001", "平安银行", 11.0, 1) is None        # avail=0
    b.unlock_t_plus_1(conn)
    assert b.sell(conn, "000001", "平安银行", 11.0, 101) is None      # 超 avail
    assert b.sell(conn, "601318", "中国平安", 54.83, 100) is None     # 无持仓
    assert len(trade_rows(conn)) == 1                                 # 只有买入那笔
    sh, avail = conn.execute(
        "SELECT shares, avail_shares FROM position WHERE code='000001'").fetchone()
    assert (sh, avail) == (100, 100)


def test_ensure_account_initializes_once():
    conn = fresh_conn()
    seed_market(conn)
    b = PaperBroker(EXEC_CFG)
    assert b.ensure_account(conn) is True
    today = date.today().isoformat()
    row = conn.execute("SELECT cash, total, kill_switch FROM portfolio_state WHERE date=?",
                       (today,)).fetchone()
    assert row == (1000000.0, 1000000.0, 0)
    assert b.ensure_account(conn) is False            # 当日已有行，不重复初始化


def test_portfolio_snapshot():
    conn = fresh_conn()
    seed_market(conn)
    b = PaperBroker(EXEC_CFG)
    b.buy(conn, "000001", "平安银行", 11.0, 100)
    b.buy(conn, "600519", "贵州茅台", 1500.0, 100)
    cash, positions, total, prev = b.portfolio(conn)
    assert cash == 1000000.0 - 1105.0 - 150037.5
    assert set(positions) == {"000001", "600519"}
    assert positions["600519"]["shares"] == 100 and positions["600519"]["cost"] == 1500.0
    mv = 100 * 11.0 + 100 * 1500.0
    assert abs(total - (cash + mv)) < 0.01
    assert prev["000001"] == 10.9 and prev["600519"] == 1490.0     # 前收


# ---------------- readback 回读校验 ----------------

def test_readback_consistent():
    conn = fresh_conn()
    seed_market(conn)
    b = PaperBroker(EXEC_CFG)
    r1 = b.buy(conn, "000001", "平安银行", 11.0, 100, trade_date=YDAY)
    b.unlock_t_plus_1(conn)
    r2 = b.sell(conn, "000001", "平安银行", 11.0, 40, trade_date=NOW_DATE)
    rb1, rb2 = b.readback(conn, r1["trade_id"]), b.readback(conn, r2["trade_id"])
    assert rb1["ok"] and rb2["ok"], (rb1, rb2)
    assert "回读一致" in rb1["detail"] and rb2["cash"] == b.cash(conn)


def test_readback_detects_tampered_amount():
    conn = fresh_conn()
    seed_market(conn)
    b = PaperBroker(EXEC_CFG)
    r = b.buy(conn, "000001", "平安银行", 11.0, 100)
    assert b.readback(conn, r["trade_id"])["ok"]
    conn.execute("UPDATE trade SET amount=amount+50 WHERE id=?", (r["trade_id"],))
    conn.commit()
    rb = b.readback(conn, r["trade_id"])
    assert not rb["ok"] and "不一致" in rb["detail"]
    n = conn.execute("SELECT COUNT(*) FROM risk_event WHERE rule='readback'").fetchone()[0]
    assert n >= 1                                     # 不一致留痕 risk_event


def test_readback_missing_trade():
    conn = fresh_conn()
    seed_market(conn)
    b = PaperBroker(EXEC_CFG)
    rb = b.readback(conn, 999)
    assert not rb["ok"] and "不存在" in rb["detail"]


# ---------------- build_context 组装 ----------------

def test_build_context_fields():
    conn = fresh_conn()
    seed_market(conn)
    b = PaperBroker(EXEC_CFG)
    peak_date = YDAY
    conn.execute("INSERT INTO portfolio_state VALUES (?,?,?,?,?,?,?)",
                 (peak_date, 0.0, 0.0, 1100000.0, 0.0, 0, "seed peak"))
    b.buy(conn, "000001", "平安银行", 11.0, 100, trade_date=NOW_DATE)   # filled，当日
    conn.execute(
        "INSERT INTO trade (trade_date, code, name, side, price, shares, amount,"
        " order_id, status, decision_id, shots, confirmed_by, created_at)"
        " VALUES (?, '000001', '平安银行', 'buy', 11.0, 100, 1105.0, 'PAPER-X',"
        " 'submitted', NULL, '[]', 'x', 'x')", (NOW_DATE,))
    conn.commit()
    from risk.engine import record_event
    record_event(conn, "kill_switch", "演练停机")                       # ts=真实当前时间

    ctx = runner.build_context(conn, NOW10)
    assert ctx.now == NOW10
    assert ctx.cash == 997790.0     # 100万 − 两笔 amount（filled 1105 + submitted 1105，与复盘口径一致）
    assert set(ctx.positions) == {"000001"} and ctx.positions["000001"]["shares"] == 100
    assert ctx.latest_prices["000001"] == 11.0
    assert ctx.prev_close["000001"] == 10.9 and ctx.prev_close["600519"] == 1490.0
    assert abs(ctx.total_equity - (997790.0 + 1100.0)) < 0.01
    assert ctx.today_trades == 2                       # filled + submitted
    assert abs(ctx.week_turnover - 1105.0 / ctx.total_equity) < 1e-9  # 近5交易日 filled 成交额占比
    assert ctx.peak_equity == 1100000.0
    assert ctx.kill_switch_until is not None
    ks_ts = conn.execute("SELECT ts FROM risk_event WHERE rule='kill_switch'").fetchone()[0]
    expect_until = datetime.fromisoformat(ks_ts) + timedelta(hours=72)
    assert abs((ctx.kill_switch_until - expect_until).total_seconds()) < 5
    assert ctx.blacklist["688801"][0] is False         # N 燧原-U 被拉黑
    assert ctx.blacklist["000001"][0] is True
    assert ctx.health_issues == []                     # 今日两根K线齐全


# ---------------- propose → 闸门 → confirm 全流程 ----------------

def _tmp_dir():
    return tempfile.mkdtemp(prefix="exec_test_orders_")


def test_propose_gate_writes_pending_then_confirm_executes():
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        d = mk_decision("buy", "000001", 11.0, 100)
        v = runner.propose(conn, d, now=NOW10, orders_dir=orders)
        assert v.approved and v.violations == []
        st = conn.execute("SELECT status FROM decision WHERE id=1").fetchone()[0]
        assert st == "approved"                        # 闸门开启：approved 后等待
        assert len(trade_rows(conn)) == 0              # 未直接成交
        pend = runner.list_pending(orders)
        assert len(pend) == 1 and pend[0].name == "pending_1.json"
        payload = json.loads(pend[0].read_text(encoding="utf-8"))
        assert payload["decision_id"] == 1
        assert payload["decision"]["code"] == "000001"
        assert payload["verdict"]["approved"] is True
        assert payload["submit_price"] == 11.0 and payload["suggest_shares"] == 100

        res = runner.confirm(conn, 1, confirmed_by="张三", now=NOW10, orders_dir=orders)
        assert res["ok"] and res["amount"] == 1105.0
        row = trade_rows(conn)[0]
        assert row[3] == "buy" and row[8] == "filled" and row[10] == "张三"
        st = conn.execute("SELECT status FROM decision WHERE id=1").fetchone()[0]
        assert st == "executed"
        assert runner.list_pending(orders) == []       # pending 文件已清理
        assert runner.confirm(conn, 1, now=NOW10, orders_dir=orders) is None  # 重复确认拒绝
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_confirm_price_override_and_lot_adjust():
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        d = mk_decision("buy", "600519", 1500.0, 150)  # 150 股 → 风控规整 100
        v = runner.propose(conn, d, now=NOW10, orders_dir=orders)
        assert v.approved and v.adjusted_order["shares"] == 100
        payload = json.loads(runner.list_pending(orders)[0].read_text(encoding="utf-8"))
        assert payload["suggest_shares"] == 100        # 建议数量用规整后
        res = runner.confirm(conn, 1, confirmed_by="李四", price_override=1500.5,
                             now=NOW10, orders_dir=orders)
        assert res["ok"] and res["shares"] == 100 and res["price"] == 1500.5
        row = trade_rows(conn)[0]
        assert row[4] == 1500.5 and row[5] == 100
        assert row[6] == 150087.51     # gross 150050 + 佣金 37.51（150050×0.00025）
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_gate_off_executes_immediately():
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        with set_gate(False):
            d = mk_decision("buy", "000001", 11.0, 100)
            v = runner.propose(conn, d, now=NOW10, orders_dir=orders)
            assert v.approved
        assert len(trade_rows(conn)) == 1              # 闸门关闭：直接成交
        st = conn.execute("SELECT status FROM decision WHERE id=1").fetchone()[0]
        assert st == "executed"
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_confirm_rerun_risk_rejects_non_session():
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        d = mk_decision("buy", "000001", 11.0, 100)
        runner.propose(conn, d, now=NOW10, orders_dir=orders)
        assert conn.execute("SELECT status FROM decision WHERE id=1").fetchone()[0] == "approved"
        res = runner.confirm(conn, 1, confirmed_by="王五", now=SAT10, orders_dir=orders)
        assert res is None                             # 周六重跑风控不过
        st = conn.execute("SELECT status FROM decision WHERE id=1").fetchone()[0]
        assert st == "rejected"
        assert len(trade_rows(conn)) == 0
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_reject_path_and_manual_reject():
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        # 风控拒绝：委托价偏离实时价 6.67% > 2%
        d = mk_decision("buy", "600519", 1600.0, 100)
        v = runner.propose(conn, d, now=NOW10, orders_dir=orders)
        assert not v.approved and any("价格保护" in x for x in v.violations)
        st = conn.execute("SELECT status FROM decision WHERE id=1").fetchone()[0]
        assert st == "rejected"
        n = conn.execute("SELECT COUNT(*) FROM risk_event WHERE decision_id=1").fetchone()[0]
        assert n >= 1

        # report_only：低置信度
        d2 = mk_decision("buy", "000001", 11.0, 100, confidence=0.5)
        runner.propose(conn, d2, now=NOW10, orders_dir=orders)
        st2 = conn.execute("SELECT status FROM decision WHERE id=2").fetchone()[0]
        assert st2 == "report_only"

        # 人工 reject 已 approved 的单
        d3 = mk_decision("buy", "601318", 54.83, 100)
        runner.propose(conn, d3, now=NOW10, orders_dir=orders)
        assert runner.list_pending(orders)
        assert runner.reject(conn, 3, by="老赵", reason="不想买保险", orders_dir=orders)
        st3 = conn.execute("SELECT status FROM decision WHERE id=3").fetchone()[0]
        assert st3 == "rejected" and runner.list_pending(orders) == []
        ev = conn.execute("SELECT rule, detail FROM risk_event WHERE decision_id=3"
                          " AND rule='manual_reject'").fetchone()
        assert ev and "老赵" in ev[1] and "不想买保险" in ev[1]
    finally:
        shutil.rmtree(orders, ignore_errors=True)


# ---------------- kill trigger 联动清仓 ----------------

def test_kill_trigger_liquidates_and_marks_switch():
    conn = fresh_conn()
    # 1000股@1290，历史峰值 250万 → 回撤 8.4% ≥ 8% 触发
    seed_market(conn, prices={"600519": (1290.0, 1280.0), "000001": (11.0, 10.9)})
    now_iso = datetime.now().isoformat(timespec="seconds")
    conn.execute("INSERT INTO position VALUES ('600519','贵州茅台',1000,1000,1300.0,?)",
                 (now_iso,))
    peak_date = (NOW10 - timedelta(days=1)).strftime("%Y-%m-%d")
    conn.execute("INSERT INTO portfolio_state VALUES (?,?,?,?,?,?,?)",
                 (peak_date, 0.0, 0.0, 2500000.0, 0.0, 0, "seed peak"))
    conn.commit()
    orders = Path(_tmp_dir())
    try:
        hold = {"action": "hold", "code": "600519", "target_weight": 0.0,
                "confidence": 0.9, "reasons": ["继续持有", "趋势未破"], "risk_notes": []}
        v = runner.propose(conn, hold, now=NOW10, orders_dir=orders)
        assert v.kill_trigger and not v.approved
        assert len(v.kill_orders) == 1 and v.kill_orders[0]["shares"] == 1000
        # 清仓已执行
        assert conn.execute("SELECT COUNT(*) FROM position").fetchone()[0] == 0
        row = trade_rows(conn)[0]
        assert (row[2], row[3], row[5]) == ("600519", "sell", 1000)
        assert row[6] == 1289032.5                     # 1,290,000 − 佣金322.5 − 印花税645
        assert row[10] == "kill_switch"
        # 停机留痕
        ks = conn.execute("SELECT kill_switch FROM portfolio_state"
                          " WHERE kill_switch=1").fetchone()
        assert ks is not None
        n = conn.execute("SELECT COUNT(*) FROM risk_event WHERE rule='kill_switch'").fetchone()[0]
        assert n >= 1
        st = conn.execute("SELECT status FROM decision WHERE id=1").fetchone()[0]
        assert st == "rejected"                        # 触发 kill 的决策作废
        # 停机期内再买卖被拒（build_context 推出 kill_switch_until）
        ctx = runner.build_context(conn, NOW10)
        assert ctx.kill_switch_until is not None and NOW10 < ctx.kill_switch_until
        from risk.engine import check
        v2 = check(mk_decision("buy", "000001", 11.0, 100), ctx, runner.CFG["risk"])
        assert not v2.approved and any("kill switch" in x for x in v2.violations)
    finally:
        shutil.rmtree(orders, ignore_errors=True)


# ---------------- 决策文件解析 ----------------

def test_load_decision_file_forms():
    import os
    d = Path(_tmp_dir())
    try:
        single = d / "single.json"
        single.write_text(json.dumps(mk_decision("buy", "000001", 11.0, 100)),
                          encoding="utf-8")
        assert len(runner.load_decision_file(str(single))) == 1
        arr = d / "arr.json"
        arr.write_text(json.dumps([mk_decision("buy", "000001", 11.0, 100),
                                   mk_decision("hold", "600519", 0, 0, order=None)]),
                       encoding="utf-8")
        assert len(runner.load_decision_file(str(arr))) == 2
        wrapped = d / "wrapped.json"
        wrapped.write_text(json.dumps({"decisions": [mk_decision("buy", "000001", 11.0, 100)]}),
                           encoding="utf-8")
        assert len(runner.load_decision_file(str(wrapped))) == 1
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------- 直接运行入口 ----------------

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
