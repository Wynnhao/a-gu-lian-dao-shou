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
os.environ.setdefault("AGSICKLE_DISABLE_NOTIFY", "1")       # 测试不弹系统通知
os.environ.setdefault("AGSICKLE_DISABLE_SLIPPAGE", "1")     # 测试金额断言不含滑点
_TMP_STATE = tempfile.mkdtemp(prefix="agsickle_state_")
os.environ.setdefault("AGSICKLE_STATE_DIR", _TMP_STATE)     # kill.json 隔离到临时目录
os.environ.setdefault("AGSICKLE_ORDERS_DIR", _TMP_STATE)    # 执行锁文件隔离

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
    conn.row_factory = sqlite3.Row  # 与生产 fetcher.get_conn 一致（Phase 4 连接层统一）
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
                # amount 取 1e6 股口径（真实票日成交额亿级），避免流动性约束规则误伤合成数据
                (code, td, close, close, close, close, 1000, close * 1000000, 0.0, 1.0))
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


def set_emergency_direct(enabled: bool):
    """临时切换 execution.emergency_direct_exec（P1-6：skip_gate 直写总开关）。"""
    class _ED:
        def __enter__(self):
            self.old = runner.CFG["execution"].get("emergency_direct_exec", False)
            runner.CFG["execution"]["emergency_direct_exec"] = enabled
            return self

        def __exit__(self, *exc):
            runner.CFG["execution"]["emergency_direct_exec"] = self.old
            return False
    return _ED()


def set_live_quotes(quotes: Dict[str, dict]):
    """W-A9：临时注入 AGSICKLE_MOCK_QUOTES 实时快照并解除 DISABLE_LIVE_QUOTES——
    build_context 在交易时段会拉到该快照，price_source 标记 live（比价口径生效）。"""
    import json as _json

    class _LQ:
        def __enter__(self):
            self._tmp = tempfile.mkdtemp(prefix="exec_mock_quotes_")
            self._f = Path(self._tmp) / "quotes.json"
            self._f.write_text(_json.dumps(quotes, ensure_ascii=False), encoding="utf-8")
            self._old_mock = os.environ.get("AGSICKLE_MOCK_QUOTES")
            self._old_dis = os.environ.pop("AGSICKLE_DISABLE_LIVE_QUOTES", None)
            os.environ["AGSICKLE_MOCK_QUOTES"] = str(self._f)
            return self

        def __exit__(self, *exc):
            if self._old_mock is None:
                os.environ.pop("AGSICKLE_MOCK_QUOTES", None)
            else:
                os.environ["AGSICKLE_MOCK_QUOTES"] = self._old_mock
            if self._old_dis is not None:
                os.environ["AGSICKLE_DISABLE_LIVE_QUOTES"] = self._old_dis
            shutil.rmtree(self._tmp, ignore_errors=True)
            return False
    return _LQ()


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
    # 委托价 12.0 会超合成数据的涨停价 11.99（昨收 10.9×1.1）→ 停板模拟拒绝，
    # 故用 11.5 验证加权成本
    b.buy(conn, "000001", "平安银行", 11.5, 100)
    sh, avail, cost = conn.execute(
        "SELECT shares, avail_shares, cost FROM position WHERE code='000001'").fetchone()
    assert (sh, avail, cost) == (200, 0, 11.25)       # 加权成交价 (1100+1150)/200（成本不含费用）
    assert b.cash(conn) == 1000000.0 - 1105.0 - 1155.0


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
    # trade_date/as_of 全部显式锚定合成日：周六运行时 NEXT_DAY == 真实今天，
    # 缺省 date.today() 会让「次日解锁」变空操作（2026-09-19 周末实测红）
    b.buy(conn, "600519", "贵州茅台", 1500.0, 100, trade_date=NOW_DATE)
    sh, avail = conn.execute(
        "SELECT shares, avail_shares FROM position WHERE code='600519'").fetchone()
    assert (sh, avail) == (100, 0)                    # T+1：当日新买不计入 avail
    assert b.sell(conn, "600519", "贵州茅台", 1500.0, 1, trade_date=NOW_DATE) is None  # 当日不可卖
    b.unlock_t_plus_1(conn, as_of=NOW_DATE)           # 当日补跑盘前：当日买入不得提前解锁
    _, avail = conn.execute(
        "SELECT shares, avail_shares FROM position WHERE code='600519'").fetchone()
    assert avail == 0
    b.unlock_t_plus_1(conn, as_of=NEXT_DAY)           # 次日盘前解锁后才可卖
    assert b.sell(conn, "600519", "贵州茅台", 1500.0, 1, trade_date=NEXT_DAY)["ok"]


def test_t_plus_1_unlock_keeps_yesterday_buy_sellable():
    """常规路径：昨日买入在今日盘前解锁后全部可卖（当日买入扣除只作用于当天买单）。"""
    conn = fresh_conn()
    seed_market(conn)
    b = PaperBroker(EXEC_CFG)
    b.buy(conn, "000001", "平安银行", 11.0, 100, trade_date=YDAY)
    b.buy(conn, "000001", "平安银行", 11.2, 100)      # 当日再买 100 股
    b.unlock_t_plus_1(conn)
    sh, avail = conn.execute(
        "SELECT shares, avail_shares FROM position WHERE code='000001'").fetchone()
    assert (sh, avail) == (200, 100)                  # 只有昨日 100 股解锁


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
    # 买入显式锚定合成日（周六运行时 NEXT_DAY==真实今天，缺省 date.today()
    # 会让下方 as_of=NEXT_DAY 解锁变空操作，avail 恒为 0）
    b.buy(conn, "000001", "平安银行", 11.0, 100, trade_date=NOW_DATE)
    assert b.sell(conn, "000001", "平安银行", 11.0, 1) is None        # avail=0
    b.unlock_t_plus_1(conn, as_of=NEXT_DAY)                           # 次日盘前解锁
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
    assert tuple(row) == (1000000.0, 1000000.0, 0)  # Row → tuple 比较（row_factory 统一后）
    assert b.ensure_account(conn) is False            # 当日已有行，不重复初始化


def test_portfolio_snapshot():
    conn = fresh_conn()
    seed_market(conn)
    b = PaperBroker(EXEC_CFG)
    b.buy(conn, "000001", "平安银行", 11.0, 100)
    b.buy(conn, "600519", "贵州茅台", 1500.0, 100)
    cash, positions, total, prev = b.portfolio(conn, today=NOW_DATE)
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

        res = runner.confirm(conn, 1, confirmed_by="张三", now=NOW10, orders_dir=orders,
                             price_override=11.0)   # W-A9：离线无实时价，显式价确认
        assert res["ok"] and res["amount"] == 1105.0
        row = trade_rows(conn)[0]
        assert row[3] == "buy" and row[8] == "filled" and row[10] == "张三"
        st = conn.execute("SELECT status FROM decision WHERE id=1").fetchone()[0]
        assert st == "executed"
        assert runner.list_pending(orders) == []       # pending 文件已清理
        assert runner.confirm(conn, 1, now=NOW10, orders_dir=orders) is None  # 重复确认拒绝
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def _seed_rule21_market(conn) -> None:
    """真实规则21 触发数据：000001 昨收 10.90 → 跌停价 9.81；今收=9.81（跌停）；
    持仓成本 12.0 → 浮亏 18.25% ≥ 基础止损线 8%（signal 表为空 → ATR 缺省回基础线）。"""
    seed_market(conn, prices={"000001": (9.81, 10.90)})
    conn.execute(
        "INSERT INTO position (code, name, shares, avail_shares, cost, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("000001", "平安银行", 200, 200, 12.0, NOW_DATE + "T09:00:00"))
    conn.commit()


def test_propose_skip_gate_defaults_to_pending_gate():
    """P1-6：真实规则21 触发（跌停价卖单+浮亏破线，skip_gate 由引擎在 check 内
    设置）+ emergency_direct_exec 默认 false → **不得**直写成交，必须落 pending
    人工闸门（恪守"绝不自动成交"总原则）。P0-4：limit_halt_emergency 事件由
    flush_events 带 decision_id 落库（engine 不再直写生产库）。"""
    conn = fresh_conn()
    _seed_rule21_market(conn)
    orders = Path(_tmp_dir())
    try:
        d = mk_decision("sell", "000001", 9.81, 200)
        with set_emergency_direct(False):
            v = runner.propose(conn, d, now=NOW10, orders_dir=orders)
        assert v.approved, v.violations
        assert d.get("skip_gate") is True, "规则21 应在 check 内设置 skip_gate"
        # 默认开关关：skip_gate 单仍走人工闸门 → 写 pending、不成交
        pend = runner.list_pending(orders)
        assert len(pend) == 1, "emergency_direct_exec=false 时 skip_gate 单必须落 pending"
        assert len(trade_rows(conn)) == 0, "默认不得自动成交"
        st = conn.execute("SELECT status FROM decision WHERE id=1").fetchone()[0]
        assert st == "approved", f"应停在 approved 等人工确认，实得 {st}"
        # P0-4：事件落库走调用方 flush_events，且带 decision_id（可归因）
        ev = conn.execute(
            "SELECT detail, decision_id FROM risk_event"
            " WHERE rule='limit_halt_emergency'").fetchall()
        assert len(ev) == 1 and ev[0][1] == 1, ev
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_propose_skip_gate_direct_exec_requires_switch():
    """P1-6：仅当 execution.emergency_direct_exec=true 显式开启时，规则21 应急单
    才绕过 pending 闸门直写成交，confirmed_by=emergency_rule21 落审计。"""
    conn = fresh_conn()
    _seed_rule21_market(conn)
    orders = Path(_tmp_dir())
    try:
        d = mk_decision("sell", "000001", 9.81, 200)
        with set_emergency_direct(True):
            v = runner.propose(conn, d, now=NOW10, orders_dir=orders)
        assert v.approved, v.violations
        assert d.get("skip_gate") is True
        # 开关开：skip_gate=True → 直接成交，不写 pending
        pend = runner.list_pending(orders)
        assert len(pend) == 0, "emergency_direct_exec=true 时不应写 pending"
        assert len(trade_rows(conn)) == 1
        row = trade_rows(conn)[0]
        assert row[3] == "sell" and row[8] == "filled"
        assert row[10] == "emergency_rule21", \
            "confirmed_by 必须为 emergency_rule21 落审计"
        st = conn.execute("SELECT status FROM decision WHERE id=1").fetchone()[0]
        assert st in ("executed", "executed_unverified"), \
            f"skip_gate 直写路径 status 必须 executed 系列，实得 {st}"
        ev = conn.execute(
            "SELECT decision_id FROM risk_event"
            " WHERE rule='limit_halt_emergency'").fetchall()
        assert len(ev) == 1 and ev[0][0] == 1, ev
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_propose_injected_skip_gate_never_direct_writes():
    """审查补丁批 Fix A：决策输入携带 skip_gate/confirmed_by 一律剥离——即使
    emergency_direct_exec=true 也不得凭注入直写（snapshot 存 LLM 原始 JSON，
    恢复这些键等于允许输入自带"免闸门"标志，buy 也能直写、审计可伪造）。"""
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        d = mk_decision("sell", "000001", 11.0, 100,
                        skip_gate=True, confirmed_by="hacker")
        conn.execute(
            "INSERT INTO position (code, name, shares, avail_shares, cost, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            ("000001", "平安银行", 1000, 1000, 11.0, NOW_DATE + "T09:00:00"))
        conn.commit()
        with set_emergency_direct(True):
            v = runner.propose(conn, d, now=NOW10, orders_dir=orders)
        assert v.approved, v.violations
        assert not d.get("skip_gate"), "propose 必须剥离注入的 skip_gate"
        assert d.get("confirmed_by") is None, "propose 必须剥离注入的 confirmed_by"
        assert len(runner.list_pending(orders)) == 1, "注入单必须走人工闸门"
        assert len(trade_rows(conn)) == 0, "注入不得自动成交"
        # 落库 snapshot 不得携带注入键（_decision_from_row 恢复面保持干净）
        snap = conn.execute(
            "SELECT input_snapshot FROM decision WHERE id=1").fetchone()[0]
        assert "skip_gate" not in snap and "confirmed_by" not in snap, snap
        # buy 同理：注入 + 开关开 → 仍走闸门（直写仅限规则21 的 sell）
        d2 = mk_decision("buy", "600519", 1500.0, 100,
                         skip_gate=True, confirmed_by="hacker")
        with set_emergency_direct(True):
            v2 = runner.propose(conn, d2, now=NOW10, orders_dir=orders)
        assert v2.approved and len(trade_rows(conn)) == 0
        assert len(runner.list_pending(orders)) == 2
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_confirm_same_day_after_ttl_expired():
    """当日 15:05 TTL（pending valid_until 的执行端）：收盘后确认当日单 -> expired。

    此前 valid_until 只写不读，收盘后确认只能靠重跑风控的「非交易时段」规则兜底。
    """
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        d = mk_decision("buy", "000001", 11.0, 100)
        v = runner.propose(conn, d, now=NOW10, orders_dir=orders)
        assert v.approved and v.violations == []
        late = datetime.combine(_BASE, time(15, 30))   # 当日 15:30（TTL 已过）
        res = runner.confirm(conn, 1, confirmed_by="测试", now=late, orders_dir=orders)
        assert res is None
        st = conn.execute("SELECT status FROM decision WHERE id=1").fetchone()[0]
        assert st == "expired"
        assert runner.list_pending(orders) == []       # pending 已清理
        ev = conn.execute("SELECT COUNT(*) FROM risk_event WHERE "
                          "rule='pending_expired'").fetchone()[0]
        assert ev >= 1
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
        assert res is None
        # 跨日确认（周三决策周六确认）先命中过期闸门：昨日的决策今天不允许执行
        st = conn.execute("SELECT status FROM decision WHERE id=1").fetchone()[0]
        assert st == "expired", st
        assert len(trade_rows(conn)) == 0
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_reject_path_and_manual_reject():
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        # 风控拒绝：委托价偏离实时价 6.67% > 2%（W-A9：注入 mock 实时快照，
        # price_source=live 时规则9 的偏离比对才生效——陈旧昨收基准下自动比价
        # 已改为留痕警告，硬拒发生在执行定价层）
        with set_live_quotes({"600519": {"price": 1500.0, "prev_close": 1490.0,
                                         "source": "mock", "time": "t"}}):
            d = mk_decision("buy", "600519", 1600.0, 100)
            v = runner.propose(conn, d, now=NOW10, orders_dir=orders)
        assert not v.approved and any("价格保护" in x for x in v.violations), v.brief()
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
        # kill 状态文件是进程级持久态，清掉避免泄漏到同进程后续测试
        if runner.KILL_STATE_FILE.exists():
            runner.KILL_STATE_FILE.unlink()
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


# ---------------- 2026-09 风控补强：递延清算 / 执行价二次校验 / 幂等 / 滑点 ----------------

def test_kill_deferred_t1_position_then_resolve_next_day():
    """kill 时当日买入（T+1 不可卖）→ 递延留痕；次日解锁后 resolve_liquidations 补清算。"""
    conn = fresh_conn()
    seed_market(conn, prices={"600519": (1290.0, 1280.0), "000001": (11.0, 10.9)})
    now_iso = datetime.now().isoformat(timespec="seconds")
    # 600519 当日买入：avail=0（T+1 不可卖）
    conn.execute("INSERT INTO position VALUES ('600519','贵州茅台',1000,0,1300.0,?)",
                 (now_iso,))
    conn.execute("INSERT INTO portfolio_state VALUES (?,?,?,?,?,?,?)",
                 (YDAY, 0.0, 0.0, 2500000.0, 0.0, 0, "seed peak"))
    conn.commit()
    orders = Path(_tmp_dir())
    try:
        with set_gate(False):
            hold = {"action": "hold", "code": "600519", "target_weight": 0.0,
                    "confidence": 0.9, "reasons": ["r1", "r2"], "risk_notes": []}
            v = runner.propose(conn, hold, now=NOW10, orders_dir=orders)
        assert v.kill_trigger and v.kill_pending == ["600519"]
        # 清仓失败不假完成：持仓仍在 + 递延事件留痕
        assert conn.execute("SELECT COUNT(*) FROM position").fetchone()[0] == 1
        ev = conn.execute("SELECT COUNT(*) FROM risk_event WHERE "
                          "rule='kill_liquidation_pending'").fetchone()[0]
        assert ev == 1
        # 次日：T+1 解锁 + 补清算（关闸门让清算单直接执行）
        conn.execute("UPDATE position SET avail_shares=shares WHERE code='600519'")
        conn.commit()
        with set_gate(False):
            n = runner.resolve_liquidations(conn, now=NOW10, orders_dir=orders)
        assert n == 1
        rows = trade_rows(conn, side="sell")
        assert len(rows) == 1 and rows[0][2] == "600519" and rows[0][5] == 1000
        # 再次 resolve 不会重复清算
        assert runner.resolve_liquidations(conn, now=NOW10, orders_dir=orders) == 0
    finally:
        if runner.KILL_STATE_FILE.exists():
            runner.KILL_STATE_FILE.unlink()
        shutil.rmtree(orders, ignore_errors=True)


def test_confirm_price_override_blocked_by_recheck():
    """--price 覆盖价偏离市价超阈值 → 执行价二次校验拦截，保留 pending 可重试。"""
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        d = mk_decision("buy", "000001", 11.0, 100)
        runner.propose(conn, d, now=NOW10, orders_dir=orders)
        res = runner.confirm(conn, 1, confirmed_by="试探", price_override=12.0,
                             now=NOW10, orders_dir=orders)  # 偏离实时价 9% > 2%
        assert res is None
        st = conn.execute("SELECT status FROM decision WHERE id=1").fetchone()[0]
        assert st == "approved"                            # 不执行也不作废
        assert len(trade_rows(conn)) == 0
        assert runner.list_pending(orders)                 # pending 保留可修正重试
        ev = conn.execute("SELECT COUNT(*) FROM risk_event WHERE "
                          "rule='price_recheck'").fetchone()[0]
        assert ev >= 1
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_duplicate_decision_id_trade_rejected():
    """同 decision_id 二次成交被账本幂等拒绝（readback 失败重跑 confirm 防线）。"""
    conn = fresh_conn()
    seed_market(conn)
    b = PaperBroker(EXEC_CFG)
    r1 = b.buy(conn, "000001", "平安银行", 11.0, 100, decision_id=42)
    assert r1 and r1["ok"]
    r2 = b.buy(conn, "000001", "平安银行", 11.0, 100, decision_id=42)
    assert r2 is None                                      # 幂等拒绝
    assert len(trade_rows(conn)) == 1
    n = conn.execute("SELECT COUNT(*) FROM risk_event WHERE "
                     "rule='duplicate_decision'").fetchone()[0]
    assert n == 1


def test_load_decision_file_strips_privilege_flags():
    """完工审查 P1：load_decision_file（外部文件输入）须剥离 kill_liquidation/
    emergency_scan 提权键（豁免规则3/4/18、跨日/TTL 闸、设计价执行）——合法
    注入点仅在服务端 limit_halt/resolve_liquidations。"""
    d = Path(tempfile.mkdtemp(prefix="agsickle_exec_priv_"))
    f = d / "decision.json"
    f.write_text(json.dumps([{"action": "sell", "code": "600519",
                              "target_weight": 0.0, "confidence": 0.9,
                              "reasons": ["r1"], "risk_notes": [],
                              "kill_liquidation": True, "emergency_scan": True}]),
                 encoding="utf-8")
    out = runner.load_decision_file(str(f))
    assert out and "kill_liquidation" not in out[0] and "emergency_scan" not in out[0]


def test_prev_close_uses_last_bar_before_today_intraday():
    """完工审查 P1：盘中（daily_bar 最新=昨日）涨跌停基准应取昨日收盘；
    旧 offset=1 会取到前日（基准错位一天，合法止损卖单被误拒）。"""
    conn = fresh_conn()
    conn.execute("INSERT INTO stock_info VALUES ('000001','平安银行','2024-01-02','x')")
    d3 = (date.today() - timedelta(days=2)).isoformat()
    d2 = (date.today() - timedelta(days=1)).isoformat()
    for td, cl in ((d3, 10.0), (d2, 10.9)):   # 盘中：无今日 bar
        conn.execute("INSERT INTO daily_bar (code, trade_date, open, high, low, close,"
                     " volume, amount, pct_chg, turnover) VALUES"
                     " ('000001',?,?,?,?,?,1000,1e7,0.0,1.0)", (td, cl, cl, cl, cl))
    conn.commit()
    b = PaperBroker(EXEC_CFG)
    assert b.prev_close(conn, "000001",
                        on_date=date.today().isoformat()) == 10.9


def test_slippage_and_limit_halt_sim():
    """滑点模型 + 停板模拟：enabled 时买入按上滑价成交、超涨停价拒单。"""
    old = os.environ.pop("AGSICKLE_DISABLE_SLIPPAGE", None)
    try:
        conn = fresh_conn()
        seed_market(conn)
        cfg = dict(EXEC_CFG, slippage_bps=100)             # 100bps = 1%
        b = PaperBroker(cfg)
        res = b.buy(conn, "000001", "平安银行", 11.0, 100, trade_date=NOW_DATE)
        assert res["price"] == 11.11 and res["requested_price"] == 11.0
        assert res["amount"] == 1116.0                     # gross 1111 + 最低佣金 5
        # 委托价超涨停（10.9×1.1=11.99）→ 停板模拟拒绝
        res2 = b.buy(conn, "600519", "贵州茅台", 1500.0, 100, trade_date=NOW_DATE)  # 1500 < 1639 正常
        assert res2 and res2["ok"]
        up_reject = b.buy(conn, "000001", "平安银行", 12.0, 100, trade_date=NOW_DATE)
        assert up_reject is None                           # 12.0 > 涨停 11.99
    finally:
        if old is not None:
            os.environ["AGSICKLE_DISABLE_SLIPPAGE"] = old


# ---------------- C-ARC-4（T2）：warn/failopen/kill_executed 留痕 ----------------

def test_kill_executed_summary_event():
    """kill 清仓后落一条 kill_executed 汇总（卖出笔数/trade_ids/递延笔数）。"""
    conn = fresh_conn()
    seed_market(conn, prices={"600519": (1290.0, 1280.0), "000001": (11.0, 10.9)})
    conn.execute("INSERT INTO position VALUES ('600519','贵州茅台',1000,1000,1300.0,?)",
                 (datetime.now().isoformat(timespec="seconds"),))
    conn.execute("INSERT INTO portfolio_state VALUES (?,?,?,?,?,?,?)",
                 (YDAY, 0.0, 0.0, 2500000.0, 0.0, 0, "seed peak"))
    conn.commit()
    orders = Path(_tmp_dir())
    try:
        hold = {"action": "hold", "code": "600519", "target_weight": 0.0,
                "confidence": 0.9, "reasons": ["r1", "r2"], "risk_notes": []}
        v = runner.propose(conn, hold, now=NOW10, orders_dir=orders)
        assert v.kill_trigger
        rows = conn.execute("SELECT detail, decision_id FROM risk_event WHERE"
                            " rule='kill_executed'").fetchall()
        assert len(rows) == 1, rows
        info = json.loads(rows[0][0])
        # H2：血缘唯一载体——触发决策 id 必须在 detail 里（W-A1 后 trade 侧可能为 NULL）
        assert info["trigger_decision_id"] == 1
        assert info["sells"] == 1 and info["deferred"] == 0
        assert len(info["trade_ids"]) == 1
        assert rows[0][1] == 1                      # 归因到触发 kill 的决策
    finally:
        if runner.KILL_STATE_FILE.exists():
            runner.KILL_STATE_FILE.unlink()
        shutil.rmtree(orders, ignore_errors=True)


def test_regime_failopen_and_warn_events_propose_confirm():
    """propose/confirm 的 warnings 以 rule='warn'、code 前缀落库且跨阶段去重；
    build_context regime fail-open 经 ctx_notes 转成 failopen_regime_cap 事件；
    confirm 打印 warnings（此前只打 violations）。"""
    import io
    import contextlib
    from risk import regime as _regime

    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    orig_cap = _regime.position_cap

    def _boom(conn_, cfg_):
        raise RuntimeError("rsrs 数据缺失")
    _regime.position_cap = _boom
    try:
        d = mk_decision("buy", "600519", 1500.0, 150)   # 150 股 → 手数规整 warning
        v = runner.propose(conn, d, now=NOW10, orders_dir=orders)
        assert v.approved and v.warnings, (v.brief(), v.warnings)
        # W-A9：离线回退昨收 → rule9 stale 口径 warning 排在首位（warn_prefix 同前缀
        # 同日去重，仅落第一条）
        warn_rows = conn.execute("SELECT detail FROM risk_event WHERE rule='warn'"
                                 ).fetchall()
        assert len(warn_rows) == 1 and warn_rows[0][0].startswith("600519 "), warn_rows
        assert "陈旧昨收" in warn_rows[0][0], warn_rows
        assert conn.execute("SELECT COUNT(*) FROM risk_event WHERE"
                            " rule='failopen_regime_cap'").fetchone()[0] == 1

        # confirm：重跑风控再次产生同一 warning → 去重不刷行；stdout 必须打印 [警告]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            res = runner.confirm(conn, 1, confirmed_by="测试", now=NOW10,
                                 orders_dir=orders, price_override=1500.0)  # W-A9 显式价
        assert res is not None and res["ok"]
        assert "[警告]" in out.getvalue(), out.getvalue()[-500:]
        assert conn.execute("SELECT COUNT(*) FROM risk_event WHERE rule='warn'"
                            ).fetchone()[0] == 1
        # confirm 阶段 failopen 事件去重（同日同名前缀只一条）
        assert conn.execute("SELECT COUNT(*) FROM risk_event WHERE"
                            " rule='failopen_regime_cap'").fetchone()[0] == 1
    finally:
        _regime.position_cap = orig_cap
        shutil.rmtree(orders, ignore_errors=True)


# ---------------- C-ARC-1/T3：执行级有限重挂（F1a 价格漂移型） ----------------

def _make_f1a(conn, orders, action="buy", code="000001", price=11.15):
    """生产路径构造 F1a：10:00 propose（pending）→ 12:00 午休 confirm 重跑风控被拒
    （规则4）→ status=rejected + risk_check_reconfirm + pending 已删（09-16 场景同构）。"""
    d = mk_decision(action, code, price, 100)
    if action == "sell":
        conn.execute(
            "INSERT INTO position (code, name, shares, avail_shares, cost, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (code, NAMES[code], 100, 100, 12.0, YDAY + "T09:00:00"))
        conn.commit()
    v = runner.propose(conn, d, now=NOW10, orders_dir=orders)
    assert v.approved, v.violations
    lunch = datetime.combine(_BASE, time(12, 0))
    res = runner.confirm(conn, 1, confirmed_by="试探", now=lunch, orders_dir=orders)
    assert res is None
    st = conn.execute("SELECT status FROM decision WHERE id=1").fetchone()[0]
    assert st == "rejected" and st is not None
    n = conn.execute("SELECT COUNT(*) FROM risk_event WHERE "
                     "rule='risk_check_reconfirm' AND decision_id=1").fetchone()[0]
    assert n >= 1
    return d


def _seed_retry_chain(conn, root_id, run_date, attempts):
    """预置链上 attempt 决策行（reasons 带 exec_retry_of=<root>;attempt=<n> 标记）。"""
    for i in range(1, attempts + 1):
        conn.execute(
            "INSERT INTO decision (run_date, code, action, target_weight, confidence,"
            " reasons, risk_notes, input_snapshot, status, created_at)"
            " VALUES (?,?,'buy',0.05,0.8,?,?,?,'rejected',?)",
            (run_date, "000001",
             json.dumps(["%s%d;attempt=%d" % (runner._RETRY_MARK, root_id, i)]),
             "[]", "{}", NOW_DATE + "T13:00:00"))
    conn.commit()


def test_requeue_hits_price_drift_reject():
    """命中重挂：F1a 价格漂移型拒单 → 新决策按实时价重提、完整 propose 落 pending
    等人工 confirm（不自动成交），exec_retry 留痕。"""
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        _make_f1a(conn, orders, price=11.15)     # 委托 11.15，实时 11.0，漂移 1.35% > 1%
        assert len(runner.list_pending(orders)) == 0
        n = runner.requeue_price_rejects(conn, now=NOW10, orders_dir=orders)
        assert n == 1
        row = conn.execute(
            "SELECT id, status, reasons FROM decision WHERE id=2").fetchone()
        assert row is not None and row[1] == "approved"     # 等待 confirm，非 executed
        assert ("exec_retry_of=1;attempt=1" in row[2]), row[2]
        pend = runner.list_pending(orders)
        assert len(pend) == 1, "重挂单必须落 pending 人工闸门"
        payload = json.loads(pend[0].read_text(encoding="utf-8"))
        assert payload["decision"]["order"]["price"] == 11.0   # 按实时价重挂
        assert len(trade_rows(conn)) == 0                       # 绝不自动成交
        ev = conn.execute("SELECT detail, decision_id FROM risk_event WHERE"
                          " rule='exec_retry'").fetchone()
        assert ev and ev[1] == 2
        info = json.loads(ev[0])
        assert info["root"] == 1 and info["attempt"] == 1
        assert abs(info["old_price"] - 11.15) < 1e-9 and abs(info["new_price"] - 11.0) < 1e-9
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_requeue_skips_when_manual_requeue_exists():
    """已有人工更晚同票同向决策 → 不掺和（09-16 人工重提 #31 场景）。"""
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        _make_f1a(conn, orders, price=11.15)
        conn.execute(
            "INSERT INTO decision (run_date, code, action, target_weight, confidence,"
            " reasons, risk_notes, input_snapshot, status, created_at)"
            " VALUES (?,?,'buy',0.05,0.8,'[]','[]','{}','approved',?)",
            (NOW_DATE, "000001", NOW_DATE + "T13:00:00"))
        conn.commit()
        assert runner.requeue_price_rejects(conn, now=NOW10, orders_dir=orders) == 0
        assert conn.execute("SELECT COUNT(*) FROM decision").fetchone()[0] == 2
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_requeue_skips_non_drift_reject():
    """漂移不足（≤ 0.5×price_guard_pct）的非漂移型拒单不重挂。"""
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        _make_f1a(conn, orders, price=11.05)     # 漂移 0.45% ≤ 1%
        assert runner.requeue_price_rejects(conn, now=NOW10, orders_dir=orders) == 0
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_requeue_stops_after_attempt_exhausted():
    """attempt 耗尽（≥ exec_retry_max=3）停止重挂；止损卖单落 stop_loss_unfilled。"""
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        _make_f1a(conn, orders, action="sell", code="000001", price=11.15)
        _seed_retry_chain(conn, root_id=1, run_date=NOW_DATE, attempts=3)
        n = runner.requeue_price_rejects(conn, now=NOW10, orders_dir=orders)
        assert n == 0
        ev = conn.execute("SELECT detail FROM risk_event WHERE"
                          " rule='stop_loss_unfilled'").fetchone()
        assert ev and ev[0].startswith("stop_loss_unfilled: 000001 "), ev
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_requeue_quiet_after_1455():
    """14:55 后不触发（给人工留 10 分钟；15:05 TTL 兜底）。"""
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        _make_f1a(conn, orders, price=11.15)
        late = datetime.combine(_BASE, time(14, 56))
        assert runner.requeue_price_rejects(conn, now=late, orders_dir=orders) == 0
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_requeue_blocked_by_exec_breaker():
    """熔断期不触发：当日已有 ≥3 个根决策的 F1a/F2 事件。"""
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        _make_f1a(conn, orders, price=11.15)
        # 伪造凑满阈值 3 的 F1a 事件（ts 必须用 requeue 口径的合成日 NOW_DATE；
        # _make_f1a 经生产 confirm 产生的真实事件 ts 是真实系统日，另补一条合成日事件）。
        # did 90/91 无 decision 行：_retry_root 回退为 id 本身，仍计为独立根。
        for did in (1, 90, 91):
            conn.execute(
                "INSERT INTO risk_event (ts, rule, detail, decision_id) VALUES "
                "(?,?,?,?)", (NOW_DATE + "T13:00:00", "risk_check_reconfirm",
                              "演练 F1a #%d" % did, did))
        conn.commit()
        assert runner.exec_breaker_tripped(conn, NOW_DATE) is True
        assert runner.requeue_price_rejects(conn, now=NOW10, orders_dir=orders) == 0
        # 次日自动解除（ts LIKE 前缀换日即清零，无状态文件）
        assert runner.exec_breaker_tripped(conn, "2099-01-01") is False
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_requeue_skips_non_trading_day():
    """节假日不触发：trade_calendar 覆盖当年但当日不在其中。"""
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        _make_f1a(conn, orders, price=11.15)
        # 日历覆盖到当年（有行即视为"已覆盖"），但不含 _BASE → 视为假日
        conn.execute("INSERT INTO trade_calendar VALUES (?)",
                     ((_BASE + timedelta(days=7)).isoformat(),))
        conn.commit()
        assert runner.requeue_price_rejects(conn, now=NOW10, orders_dir=orders) == 0
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_requeue_cum_drift_guard_abandons_chase():
    """追价护栏：实时价相对根决策原价累计漂移 ≥ 5% → 放弃并落 exec_retry_skip。"""
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        _make_f1a(conn, orders, price=11.05)     # 根决策原价 11.05
        # 盘中行情下移：11.05 → 10.45（累计漂移 5.4% ≥ 5%）
        conn.execute("UPDATE daily_bar SET close=10.45 WHERE code='000001'"
                     " AND trade_date=?", (NOW_DATE,))
        conn.commit()
        n = runner.requeue_price_rejects(conn, now=NOW10, orders_dir=orders)
        assert n == 0
        ev = conn.execute("SELECT detail FROM risk_event WHERE"
                          " rule='exec_retry_skip'").fetchone()
        assert ev and ev[0].startswith("exec_retry_skip: 000001 "), ev
    finally:
        shutil.rmtree(orders, ignore_errors=True)


# ---------------- C-ARC-2/T4：执行失败熔断挡板 ----------------

def _seed_f1a_event(conn, decision_id, ts):
    """直插一条 F1a 事件（ts 显式给定，与查询口径的「当日」对齐）。"""
    conn.execute(
        "INSERT INTO risk_event (ts, rule, detail, decision_id) VALUES (?,?,?,?)",
        (ts, "risk_check_reconfirm", "演练 F1a #%s" % decision_id, decision_id))
    conn.commit()


def test_exec_breaker_blocks_propose_and_exempts_liquidation():
    """熔断：3 根决策执行失败 → propose 拒新单不插行 + 事件当日去重 + notify 一次；
    kill_liquidation 补清算豁免（强平 > 熔断，CONSTRAINTS §3.3）。"""
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        for did in (11, 12, 13):
            _seed_f1a_event(conn, did, NOW_DATE + "T13:0%d:00" % (did % 10))
        assert runner.exec_breaker_tripped(conn, NOW_DATE) is True
        before = conn.execute("SELECT COUNT(*) FROM decision").fetchone()[0]
        ev_before = conn.execute("SELECT COUNT(*) FROM risk_event WHERE"
                                 " rule='exec_circuit_breaker'").fetchone()[0]
        v = runner.propose(conn, mk_decision("buy", "000001", 11.0, 100),
                           now=NOW10, orders_dir=orders)
        assert not v.approved and "exec breaker" in v.warnings
        # 挡板在插行之前：不产生新决策行；事件当日只一条（notify 同步只一次）
        assert conn.execute("SELECT COUNT(*) FROM decision").fetchone()[0] == before
        ev = conn.execute("SELECT COUNT(*) FROM risk_event WHERE"
                          " rule='exec_circuit_breaker'").fetchone()[0]
        assert ev == ev_before + 1
        runner.propose(conn, mk_decision("buy", "000001", 11.0, 100),
                       now=NOW10, orders_dir=orders)
        assert conn.execute("SELECT COUNT(*) FROM risk_event WHERE"
                            " rule='exec_circuit_breaker'").fetchone()[0] == ev
        # kill_liquidation 卖单豁免：正常走风控并落 pending（等人工 confirm）
        conn.execute(
            "INSERT INTO position (code, name, shares, avail_shares, cost, updated_at)"
            " VALUES ('000001','平安银行',100,100,12.0,?)", (YDAY + "T09:00:00",))
        conn.commit()
        liq = mk_decision("sell", "000001", 11.0, 100, kill_liquidation=True)
        v2 = runner.propose(conn, liq, now=NOW10, orders_dir=orders)
        assert v2.approved, v2.violations
        assert len(runner.list_pending(orders)) == 1
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_exec_breaker_same_chain_counts_one_root():
    """同链归并：根决策 + 同根重挂 3 代的 F1a 事件只计 1 个根，不触发熔断；
    再加 1 个 F2（execution_failed）根共 2 个仍不触发；第 3 个根触发。"""
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        # 根决策 1（无标记）+ 三代重挂（exec_retry_of=1;attempt=n），各带 F1a 事件
        conn.execute(
            "INSERT INTO decision (run_date, code, action, target_weight, confidence,"
            " reasons, risk_notes, input_snapshot, status, created_at)"
            " VALUES (?,?,'buy',0.05,0.8,'[]','[]','{}','rejected',?)",
            (NOW_DATE, "000001", NOW_DATE + "T10:00:00"))
        for i in (1, 2, 3):
            conn.execute(
                "INSERT INTO decision (run_date, code, action, target_weight,"
                " confidence, reasons, risk_notes, input_snapshot, status, created_at)"
                " VALUES (?,?,'buy',0.05,0.8,?,?,'{}','rejected',?)",
                (NOW_DATE, "000001",
                 json.dumps(["%s1;attempt=%d" % (runner._RETRY_MARK, i)]),
                 "[]", NOW_DATE + "T13:0%d:00" % i))
            _seed_f1a_event(conn, 1 + i, NOW_DATE + "T13:0%d:00" % i)
        _seed_f1a_event(conn, 1, NOW_DATE + "T10:30:00")
        assert runner.exec_breaker_tripped(conn, NOW_DATE) is False   # 4 事件 1 根
        # 第 2 个根：F2 execution_failed
        conn.execute(
            "INSERT INTO risk_event (ts, rule, detail, decision_id) VALUES "
            "(?,?,?,?)", (NOW_DATE + "T14:00:00", "execution_failed",
                          "成交失败 decision#50", 50))
        conn.commit()
        assert runner.exec_breaker_tripped(conn, NOW_DATE) is False   # 2 根 < 3
        # 第 3 个根 → 触发
        _seed_f1a_event(conn, 77, NOW_DATE + "T14:30:00")
        assert runner.exec_breaker_tripped(conn, NOW_DATE) is True
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_exec_breaker_propose_db_short_circuit_and_next_day_release():
    """propose-db 熔断短路返回 0；次一交易日（阈值查询换日）自动解除，propose 恢复。"""
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        for did in (21, 22, 23):
            _seed_f1a_event(conn, did, NOW_DATE + "T13:00:00")
        # 预插一条 proposed 决策（若无熔断会被 propose_db 逐条处理）
        conn.execute(
            "INSERT INTO decision (run_date, code, action, target_weight, confidence,"
            " reasons, risk_notes, input_snapshot, status, created_at)"
            " VALUES (?,?,'buy',0.05,0.8,'[]','[]','{}','proposed',?)",
            (NOW_DATE, "000001", NOW_DATE + "T09:00:00"))
        conn.commit()
        n = runner.propose_db(conn, run_date=NOW_DATE, now=NOW10)
        assert n == 0
        st = conn.execute("SELECT status FROM decision WHERE id=1").fetchone()[0]
        assert st == "proposed"                       # 未被处理，原状保留
        # 次一交易日自动解除：同一决策 propose 恢复正常走风控（闸门开启 → pending）。
        # 推进 3 天（周末安全落到下周一~四），并为全池补当日 bar 防 health 降级。
        later_dt = datetime.combine(_BASE + timedelta(days=3), time(10, 0))
        later = later_dt.strftime("%Y-%m-%d")
        for code, (latest, _prev) in DEFAULT_PRICES.items():
            conn.execute(
                "INSERT INTO daily_bar (code, trade_date, open, high, low, close,"
                " volume, amount, pct_chg, turnover) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (code, later, latest, latest, latest, latest, 1000,
                 latest * 1000000, 0.0, 1.0))
        conn.commit()
        assert runner.exec_breaker_tripped(conn, later) is False
        v = runner.propose(conn, mk_decision("buy", "000001", 11.0, 100),
                           now=later_dt, orders_dir=orders)
        assert v.approved, (v.brief(), v.violations)
        assert len(runner.list_pending(orders)) == 1
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_exec_breaker_red_alert_payload():
    """看板红警：api_data_status 输出 exec_breaker 字段（当日存在即 tripped_today；
    查询口径是真实系统日，事件 ts 也用真实系统日）。"""
    conn = fresh_conn()
    seed_market(conn)
    real_today = datetime.now().strftime("%Y-%m-%d")
    _seed_f1a_event(conn, 31, real_today + "T13:00:00")
    conn.execute(
        "INSERT INTO risk_event (ts, rule, detail, decision_id) VALUES "
        "(?,?,?,?)", (real_today + "T14:00:00", "exec_circuit_breaker",
                      "exec_circuit_breaker: 演练", 31))
    conn.commit()
    from webapp.api.data_status import api_data_status
    payload = api_data_status(conn, {})
    assert payload["exec_breaker"]["tripped_today"] is True
    assert payload["exec_breaker"]["since"] == real_today + "T14:00:00"


def test_requeue_skips_emergency_scan_singles():
    """协同点②（sprint4-carc-conflicts）：规则21 应急扫描单（emergency_scan=1）
    不参与重挂——limit_halt 有自己的 stuck 计数与次日再生成链。"""
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        # 候选满足全部重挂条件（rejected + F1a 事件 + 委托价 11.15 对实时 11.0 漂移 1.35%），
        # 唯独 emergency_scan=1 → 必须在 SQL 层被排除
        conn.execute(
            "INSERT INTO decision (run_date, code, action, target_weight, confidence,"
            " reasons, risk_notes, input_snapshot, status, created_at, emergency_scan)"
            " VALUES (?,?,'buy',0.05,0.8,'[]','[]',?,'rejected',?,1)",
            (NOW_DATE, "000001",
             json.dumps(mk_decision("buy", "000001", 11.15, 100)),
             NOW_DATE + "T10:00:00"))
        _seed_f1a_event(conn, 1, NOW_DATE + "T13:00:00")
        conn.commit()
        assert runner.requeue_price_rejects(conn, now=NOW10, orders_dir=orders) == 0
        assert conn.execute("SELECT COUNT(*) FROM decision").fetchone()[0] == 1
    finally:
        shutil.rmtree(orders, ignore_errors=True)


# ---------------- C-ARC 补修1（Sprint4 落地核验①/W-A2②）：补清算 flag 跨 confirm 存活 ----------------

def test_confirm_liquidation_survives_stop_period_and_breaker():
    """kill 后次日的补清算单（input_snapshot 含 kill_liquidation: true）：
    修复前 _decision_from_row 不恢复该 flag → confirm 重建决策 dict 时丢失，
    规则5 停机豁免失效必拒；修复后停机期 + 熔断已触发两条件下 confirm 均不被拦。"""
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        # 条件一：停机期（kill.json 权威状态，STATE_DIR 已沙箱）
        runner.write_kill_state(NOW10 + timedelta(hours=2), "演练停机")
        # 条件二：熔断已触发（3 个根决策 F1a 事件，ts 与 confirm 当日口径对齐）
        for did in (61, 62, 63):
            _seed_f1a_event(conn, did, NOW_DATE + "T09:00:00")
        assert runner.exec_breaker_tripped(conn, NOW_DATE) is True
        # resolve_liquidations 同构的补清算单（昨日买入已解锁）
        conn.execute(
            "INSERT INTO position (code, name, shares, avail_shares, cost, updated_at)"
            " VALUES ('000001','平安银行',100,100,12.0,?)", (YDAY + "T09:00:00",))
        conn.commit()
        liq = mk_decision("sell", "000001", 11.0, 100, kill_liquidation=True)
        v = runner.propose(conn, liq, now=NOW10, orders_dir=orders)
        assert v.approved, v.violations          # propose 侧两豁免（T4+规则5）本就活着
        did = conn.execute("SELECT MAX(id) FROM decision").fetchone()[0]
        snap = conn.execute("SELECT input_snapshot FROM decision WHERE id=?",
                            (did,)).fetchone()[0]
        assert "kill_liquidation" in snap
        # confirm 侧：修复前 flag 丢失 → 规则5「停机期拒普通卖出」必拦
        res = runner.confirm(conn, did, confirmed_by="测试", now=NOW10, orders_dir=orders)
        assert res is not None and res["ok"], "补清算单在停机期+熔断下必须可 confirm 成交"
        st = conn.execute("SELECT status FROM decision WHERE id=?", (did,)).fetchone()[0]
        assert st == "executed" and runner.list_pending(orders) == []
        # 修复口径：恢复只随卖单（买入重建 dict 不得带 kill_liquidation）
        buy_d = mk_decision("buy", "600519", 1500.0, 100, kill_liquidation=True)
        v2 = runner.propose(conn, buy_d, now=NOW10, orders_dir=orders)   # 熔断豁免只认卖单
        assert not v2.approved and "exec breaker" in v2.warnings
    finally:
        if runner.KILL_STATE_FILE.exists():
            runner.KILL_STATE_FILE.unlink()
        shutil.rmtree(orders, ignore_errors=True)


# ---------------- Sprint4 批次A：W-A1 / W-A3 / W-A5 / W-A6 / W-A9 ----------------

def test_kill_two_positions_decision_id_null_and_dd_base():
    """W-A1+W-A3：两持仓 kill 全清——两笔 trade decision_id 均 NULL（绕
    trade(decision_id) 唯一索引，P0-4 不再中途崩）、confirmed_by=kill_switch；
    kill.json 含 dd_base/dd_base_date/kill_count/lifetime_peak；resume 后
    build_context 回撤归零（分段 8%），再 check 不触发。"""
    conn = fresh_conn()
    seed_market(conn, prices={"600519": (1290.0, 1280.0), "000001": (11.0, 10.9)})
    now_iso = datetime.now().isoformat(timespec="seconds")
    conn.execute("INSERT INTO position VALUES ('600519','贵州茅台',1000,1000,1300.0,?)",
                 (now_iso,))
    conn.execute("INSERT INTO position VALUES ('000001','平安银行',2000,2000,12.0,?)",
                 (now_iso,))
    peak_date = (NOW10 - timedelta(days=1)).strftime("%Y-%m-%d")
    conn.execute("INSERT INTO portfolio_state VALUES (?,?,?,?,?,?,?)",
                 (peak_date, 0.0, 0.0, 2600000.0, 0.0, 0, "seed peak"))
    conn.commit()
    orders = Path(_tmp_dir())
    try:
        hold = {"action": "hold", "code": "600519", "target_weight": 0.0,
                "confidence": 0.9, "reasons": ["继续持有", "趋势未破"], "risk_notes": []}
        v = runner.propose(conn, hold, now=NOW10, orders_dir=orders)
        # 权益≈2.312M vs 峰值 2.6M → 回撤 11.1% ≥ 8% 触发
        assert v.kill_trigger
        # W-A1：两笔清仓各自落账，decision_id 均 NULL、确认人 kill_switch
        rows = trade_rows(conn, side="sell")
        assert len(rows) == 2, "两持仓必须全部清仓（P0-4：此前第 2 笔被幂等防线拒）"
        assert conn.execute("SELECT COUNT(*) FROM position").fetchone()[0] == 0
        # W-A1：唯一索引实测——若仍共用同一非 NULL decision_id，第 2 笔 INSERT 会
        # 抛 IntegrityError 使 kill 中途崩溃（回归锚：idx_trade_decision_uniq）
        assert conn.execute(
            "SELECT COUNT(DISTINCT decision_id) FROM trade WHERE side='sell'"
            " AND decision_id IS NOT NULL").fetchone()[0] == 0
        # W-A3：kill.json 含 dd_base（清仓后权益）+ 累计观测键
        ks = runner.read_kill_state()
        assert ks and ks.get("active") and ks.get("dd_base") is not None
        assert ks.get("dd_base_date") == NOW_DATE
        assert ks.get("kill_count") == 1
        assert ks.get("lifetime_peak") == 2600000.0
        ev = conn.execute("SELECT COUNT(*) FROM risk_event WHERE"
                          " rule='kill_dd_base_reset'").fetchone()[0]
        assert ev == 1
        # W-A3：resume 后 dd_base 感知峰值 → 回撤归零，再 check 不触发
        runner.kill_resume(conn, "测试恢复")
        ctx = runner.build_context(conn, NOW10)
        assert ctx.kill_switch_until is None
        dd = 1 - ctx.total_equity / ctx.peak_equity if ctx.peak_equity else 0.0
        assert dd < 0.08, "清仓重置后回撤必须脱离 8% 死锁（实得 %.2f%%）" % (dd * 100)
        from risk.engine import check
        v2 = check({"action": "hold", "code": "600519", "target_weight": 0.0,
                    "confidence": 0.9, "reasons": ["r1", "r2"], "risk_notes": []},
                   ctx, runner.CFG["risk"])
        assert not v2.kill_trigger
    finally:
        if runner.KILL_STATE_FILE.exists():
            runner.KILL_STATE_FILE.unlink()
        shutil.rmtree(orders, ignore_errors=True)


def test_kill_deferred_clears_dd_base_then_resolve_rewrites():
    """W-A3③ 递延场景：kill 有 T+1 递延 → kill.json 不含 dd_base（旧基准清除）；
    resolve_liquidations 补清算全部完成 → 补写 dd_base；重复 resolve 幂等
    （同日不覆盖）。"""
    conn = fresh_conn()
    seed_market(conn, prices={"600519": (1290.0, 1280.0), "000001": (11.0, 10.9)})
    now_iso = datetime.now().isoformat(timespec="seconds")
    conn.execute("INSERT INTO position VALUES ('600519','贵州茅台',1000,0,1300.0,?)",
                 (now_iso,))   # T+1 不可卖 → 递延
    conn.execute("INSERT INTO portfolio_state VALUES (?,?,?,?,?,?,?)",
                 (YDAY, 0.0, 0.0, 2500000.0, 0.0, 0, "seed peak"))
    conn.commit()
    orders = Path(_tmp_dir())
    try:
        hold = {"action": "hold", "code": "600519", "target_weight": 0.0,
                "confidence": 0.9, "reasons": ["r1", "r2"], "risk_notes": []}
        with set_gate(False):
            v = runner.propose(conn, hold, now=NOW10, orders_dir=orders)
        assert v.kill_trigger and v.kill_pending == ["600519"]
        ks = runner.read_kill_state()
        assert ks and "dd_base" not in ks, "递延场景不得写 dd_base"
        assert "dd_base" not in ks
        # 次日解锁 → 补清算 → dd_base 补写
        conn.execute("UPDATE position SET avail_shares=shares WHERE code='600519'")
        conn.commit()
        with set_gate(False):
            assert runner.resolve_liquidations(conn, now=NOW10, orders_dir=orders) == 1
        ks2 = runner.read_kill_state()
        assert ks2.get("dd_base") is not None and ks2.get("dd_base_date") == NOW_DATE
        dd_date = ks2["dd_base_date"]
        # 幂等：同日再 resolve（无新清算）不覆盖 dd_base
        with set_gate(False):
            runner.resolve_liquidations(conn, now=NOW10, orders_dir=orders)
        ks3 = runner.read_kill_state()
        assert ks3.get("dd_base_date") == dd_date
        # W-A3⑤：build_context 走 dd_base 感知窗口
        ctx = runner.build_context(conn, NOW10)
        assert ctx.peak_equity <= ctx.total_equity + 1e-6
    finally:
        if runner.KILL_STATE_FILE.exists():
            runner.KILL_STATE_FILE.unlink()
        shutil.rmtree(orders, ignore_errors=True)


def test_confirm_emergency_crossday_gate():
    """W-A5②：应急单（run_date=次日）T 晚 confirm 放行（跨日闸门豁免 + TTL 按
    run_date 当日 15:05）；普通单跨日照旧 expired；过期 run_date 的应急单也作废。"""
    conn = fresh_conn()
    _seed_rule21_market(conn)
    orders = Path(_tmp_dir())
    try:
        d = mk_decision("sell", "000001", 9.81, 200, emergency_scan=True)
        now_propose = datetime.combine(_BASE, time(15, 40))   # T 晚盘后
        v = runner.propose(conn, d, run_date=NEXT_DAY, now=now_propose,
                           orders_dir=orders)
        assert v.approved, v.violations
        did = conn.execute("SELECT MAX(id) FROM decision").fetchone()[0]
        # T 晚 22:00 confirm：跨日 + TTL 两闸均按新语义放行 → 应急设计价成交
        now_confirm = datetime.combine(_BASE, time(22, 0))
        res = runner.confirm(conn, did, confirmed_by="睡前人工", now=now_confirm,
                             orders_dir=orders)
        assert res is not None and res["ok"], "应急单 T 晚 confirm 必须可成交（P1-4）"
        assert conn.execute("SELECT status FROM decision WHERE id=?",
                            (did,)).fetchone()[0] == "executed"
        n = conn.execute("SELECT COUNT(*) FROM risk_event WHERE"
                         " rule='pending_crossday_allowed'").fetchone()[0]
        assert n >= 1
        # 负例1：普通单（非应急）run_date=次日 → 今日 confirm → expired
        # （DB 直造 approved 行，隔离规则4 的时段拒绝，单验闸门语义）
        conn.execute(
            "INSERT INTO decision (run_date, code, action, target_weight, confidence,"
            " reasons, risk_notes, input_snapshot, status, created_at, emergency_scan)"
            " VALUES (?,?,?,?,?,?,?,?,?, ?, 0)",
            (NEXT_DAY, "600519", "buy", 0.05, 0.8, '["r1","r2"]', "[]",
             json.dumps(mk_decision("buy", "600519", 1500.0, 100), ensure_ascii=False),
             "approved", datetime.now().isoformat(timespec="seconds")))
        did2 = conn.execute("SELECT MAX(id) FROM decision").fetchone()[0]
        res2 = runner.confirm(conn, did2, confirmed_by="x", now=now_confirm,
                              orders_dir=orders)
        assert res2 is None
        assert conn.execute("SELECT status FROM decision WHERE id=?",
                            (did2,)).fetchone()[0] == "expired"
        # 负例2：应急单但 run_date 已过（<今日）→ 作废（可重新扫描生成）
        past = YDAY
        conn.execute(
            "INSERT INTO decision (run_date, code, action, target_weight, confidence,"
            " reasons, risk_notes, input_snapshot, status, created_at, emergency_scan)"
            " VALUES (?,?,?,?,?,?,?,?,?, ?, 1)",
            (past, "000001", "sell", 0.0, 1.0, '["r1","r2"]', "[]",
             json.dumps(mk_decision("sell", "000001", 9.81, 200, emergency_scan=True),
                        ensure_ascii=False),
             "approved", datetime.now().isoformat(timespec="seconds")))
        did3 = conn.execute("SELECT MAX(id) FROM decision").fetchone()[0]
        res3 = runner.confirm(conn, did3, confirmed_by="x", now=now_confirm,
                              orders_dir=orders)
        assert res3 is None
        assert conn.execute("SELECT status FROM decision WHERE id=?",
                            (did3,)).fetchone()[0] == "expired"
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_reject_executed_guard():
    """W-A6：executed/executed_unverified 拒绝 reject，审计链保住。"""
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        d = mk_decision("buy", "000001", 11.0, 100)
        runner.propose(conn, d, now=NOW10, orders_dir=orders)
        res = runner.confirm(conn, 1, confirmed_by="测试", now=NOW10,
                             orders_dir=orders, price_override=11.0)
        assert res is not None and res["ok"]
        assert runner.reject(conn, 1, by="误操作", reason="手滑", orders_dir=orders) is False
        assert conn.execute("SELECT status FROM decision WHERE id=1").fetchone()[0] \
            == "executed", "已成交决策不得被置回 rejected"
        ev = conn.execute("SELECT COUNT(*) FROM risk_event WHERE"
                          " rule='manual_reject_rejected'").fetchone()[0]
        assert ev == 1
        # executed_unverified 同样被拒
        conn.execute("UPDATE decision SET status='executed_unverified' WHERE id=1")
        conn.commit()
        assert runner.reject(conn, 1, by="误操作", reason="again", orders_dir=orders) is False
        assert conn.execute("SELECT status FROM decision WHERE id=1").fetchone()[0] \
            == "executed_unverified"
        # 对照：approved 单仍可 reject
        runner.propose(conn, mk_decision("buy", "601318", 54.83, 100),
                       now=NOW10, orders_dir=orders)
        assert runner.reject(conn, 2, by="人工", reason="不要", orders_dir=orders) is True
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_confirm_stale_price_halt_and_explicit():
    """W-A9：实时价缺失时 confirm 不得自动以昨收成交——无 --price 挂起（status
    保持 approved、pending 保留、stale_price_halt 留痕）；带 --price 放行成交。"""
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        runner.propose(conn, mk_decision("buy", "000001", 11.0, 100),
                       now=NOW10, orders_dir=orders)
        # 无 --price：挂起，不成交
        res = runner.confirm(conn, 1, confirmed_by="测试", now=NOW10, orders_dir=orders)
        assert res is None
        assert conn.execute("SELECT status FROM decision WHERE id=1").fetchone()[0] \
            == "approved"
        assert len(runner.list_pending(orders)) == 1, "挂起必须保留 pending"
        assert len(trade_rows(conn)) == 0
        n_halt = conn.execute("SELECT COUNT(*) FROM risk_event WHERE"
                              " rule='stale_price_halt'").fetchone()[0]
        assert n_halt == 1
        # 带 --price：显式确认放行，风控仍跑（成交）
        res2 = runner.confirm(conn, 1, confirmed_by="显式价", now=NOW10,
                              orders_dir=orders, price_override=11.0)
        assert res2 is not None and res2["ok"]
        assert conn.execute("SELECT status FROM decision WHERE id=1").fetchone()[0] \
            == "executed"
        # W-A9：broker 口径版定价源标记
        b = PaperBroker(EXEC_CFG)
        p, src = b.latest_price_with_source(conn, "000001")
        assert src == "stale_close" and p == 11.0   # 离线 → 昨收（陈旧）口径
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_confirm_emergency_stale_uses_design_price():
    """W-A9：应急/补清算单实时价缺失时按**决策显式设计价**执行（不自动以昨收成交），
    stale_price_exec 留痕；设计价缺失则挂起。"""
    conn = fresh_conn()
    _seed_rule21_market(conn)
    orders = Path(_tmp_dir())
    try:
        d = mk_decision("sell", "000001", 9.81, 200, emergency_scan=True)
        v = runner.propose(conn, d, now=NOW10, orders_dir=orders)
        assert v.approved, v.violations
        res = runner.confirm(conn, 1, confirmed_by="failsafe", now=NOW10,
                             orders_dir=orders)   # 无 --price
        assert res is not None and res["ok"], "应急单必须按设计价（跌停价）成交"
        row = trade_rows(conn)[0]
        assert row[4] == 9.81, "成交价必须是显式设计价 9.81（昨收 9.81 恰好相等也不得混淆口径）"
        ev = conn.execute("SELECT COUNT(*) FROM risk_event WHERE"
                          " rule='stale_price_exec'").fetchone()[0]
        assert ev == 1
    finally:
        shutil.rmtree(orders, ignore_errors=True)


# ---------------- 批次4a（全量批 P2 清债·风控执行域）2026-09-20 ----------------

def test_effective_peak_replay_before_dd_base_falls_back_to_window():
    """P2①：回放/补跑 dd_base_date **之前**的历史日——旧口径 after=dd_base_date 与
    before=回放日 组合恒空窗 → peak=None → 回撤记 0（且 mark_to_market 的
    INSERT OR REPLACE 覆写该日 drawdown 列）。修复后回放日严格早于重置日 →
    回退 250 行窗口口径（与该日当年实时写入值一致）；before==base 当日维持
    分段语义（kill 当日 dd=0）。"""
    conn = fresh_conn()
    for d, total in (("2026-08-01", 2_000_000.0), ("2026-08-02", 1_700_000.0),
                     ("2026-08-06", 1_100_000.0)):
        conn.execute("INSERT INTO portfolio_state VALUES (?,?,?,?,?,?,?)",
                     (d, 0.0, 0.0, total, 0.0, 0, "seed"))
    conn.commit()
    try:
        runner.write_kill_state(None, "P2①回放测试", dd_base_equity=1_000_000.0,
                                now=datetime(2026, 8, 5, 16, 0))
        # 回放 08-02（严格早于 dd_base_date=08-05）→ 窗口峰值 200 万（旧口径 None）
        assert runner.effective_peak(conn, before="2026-08-02") == 2_000_000.0
        # before 晚于 base → 分段窗口照常
        assert runner.effective_peak(conn, before="2026-08-07") == 1_100_000.0
        # before==base（kill 重置当日）→ 维持分段语义：空窗 None（当日 dd 记 0）
        assert runner.effective_peak(conn, before="2026-08-05") is None
    finally:
        if runner.KILL_STATE_FILE.exists():
            runner.KILL_STATE_FILE.unlink()


def test_effective_peak_dd_base_branch_windowed_250():
    """P2⑥：dd_base 分支同样受 250 行窗口约束——重置日久远（段内 >250 行）时，
    段内最旧一条坏数据行（total 999 万）不再永久抬高分段峰值。"""
    conn = fresh_conn()
    d0 = date(2025, 1, 1)
    for i in range(260):
        conn.execute("INSERT INTO portfolio_state VALUES (?,?,?,?,?,?,?)",
                     ((d0 + timedelta(days=i)).isoformat(), 0.0, 0.0,
                      9_999_999.0 if i == 0 else 1_000_000.0, 0.0, 0, "seed"))
    conn.commit()
    try:
        runner.write_kill_state(None, "P2⑥窗口测试", dd_base_equity=1_000_000.0,
                                now=datetime(2025, 1, 1, 16, 0))
        # 段内 260 行，窗口 250 → 基准日当日那条 999 万（i=0，最旧）滑出窗口
        assert runner.effective_peak(conn) == 1_000_000.0
    finally:
        if runner.KILL_STATE_FILE.exists():
            runner.KILL_STATE_FILE.unlink()


def test_confirm_emergency_next_day_stale_uses_execution_day_limit_down():
    """P2②：应急设计价=T 日跌停价；T+1 执行日（run_date=T+1）实时价缺失路径按
    **执行日跌停价**成交（昨收=T 收盘=设计价 9.81 → 执行日跌停=round(9.81×0.9)
    =8.83）。旧口径按 T 日跌停价 9.81 成交=幻影价：T+1 继续死封时高出真实可排队
    价 ~11%（10% 板）。同日 confirm（既有用例）不受影响——设计价即当日跌停价。"""
    conn = fresh_conn()
    _seed_rule21_market(conn)   # 昨收10.90/今收9.81（T 跌停）、持仓200股成本12
    orders = Path(_tmp_dir())
    try:
        d = mk_decision("sell", "000001", 9.81, 200, emergency_scan=True)
        v = runner.propose(conn, d, run_date=NEXT_DAY, now=NOW10, orders_dir=orders)
        assert v.approved, v.violations
        did = conn.execute("SELECT MAX(id) FROM decision").fetchone()[0]
        # T+1 盘前 09:14 failsafe 同款路径：实时价缺失（stale_close）
        now_t1 = datetime.combine(_BASE + timedelta(days=1), time(9, 14))
        res = runner.confirm(conn, did, confirmed_by="emergency_timeout_failsafe",
                             now=now_t1, orders_dir=orders)
        assert res is not None and res["ok"], "T+1 应急单必须可成交"
        row = trade_rows(conn)[0]
        assert row[4] == 8.83, ("成交价必须是执行日跌停价 round(9.81*0.9)=8.83，"
                                "不得按 T 日跌停价 9.81 幻影成交（实得 %.2f）" % row[4])
        ev = conn.execute("SELECT COUNT(*) FROM risk_event WHERE"
                          " rule='stale_price_exec'").fetchone()[0]
        assert ev == 1
        # 执行日跌停价挂单卖出 → 规则14 跌停拒卖经规则21 豁免，不得二次校验拦截
        n_rej = conn.execute("SELECT COUNT(*) FROM risk_event WHERE"
                             " rule='price_recheck'").fetchone()[0]
        assert n_rej == 0, "应急单按执行日跌停价排队不得被价格二次校验拦截"
    finally:
        shutil.rmtree(orders, ignore_errors=True)


def test_confirm_hang_paths_notify():
    """P2④：挂起类分支（实时价缺失 stale_price_halt / 执行价二次校验未过）此前
    只 stdout+risk_event、无主动通知——人工需"碰巧在看"才知道单子挂起等处理。
    两分支现在都必须 notify。"""
    calls = []
    _orig_notify = runner.notify
    runner.notify = lambda title, body="": (calls.append((title, body)) or {})
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        # 场景1：普通单实时价缺失 → stale_price_halt 挂起 + 主动通知
        runner.propose(conn, mk_decision("buy", "000001", 11.0, 100),
                       now=NOW10, orders_dir=orders)
        assert runner.confirm(conn, 1, confirmed_by="t", now=NOW10,
                              orders_dir=orders) is None
        assert any(t.startswith("挂起") for t, _ in calls), calls
        # 场景2：--price 12.0 超涨停 11.99 → 执行价二次校验拦截 + 主动通知
        n0 = len(calls)
        res = runner.confirm(conn, 1, confirmed_by="t", now=NOW10,
                             orders_dir=orders, price_override=12.0)
        assert res is None
        assert any("二次校验" in t for t, _ in calls[n0:]), calls[n0:]
        # pending 仍保留（挂起语义不变）
        assert len(runner.list_pending(orders)) == 1
    finally:
        runner.notify = _orig_notify
        shutil.rmtree(orders, ignore_errors=True)
        conn.close()


def test_explicit_price_marker_removed_and_recheck_still_enforced():
    """P2④（死标记处置验证）：_explicit_price 死标记已删除——显式 --price 仍必须过
    执行价二次校验（F1b 语义不放松），且注入该键不得产生任何豁免效果。"""
    conn = fresh_conn()
    seed_market(conn)
    orders = Path(_tmp_dir())
    try:
        d = mk_decision("buy", "000001", 11.0, 100, _explicit_price=True)
        assert d.get("_explicit_price") is True   # 外部输入可携带（无剥离面：无消费者）
        runner.propose(conn, d, now=NOW10, orders_dir=orders)
        did = conn.execute("SELECT MAX(id) FROM decision").fetchone()[0]
        # --price 12.0 超涨停 → 二次校验必须拦截（不得因显式价放行）
        res = runner.confirm(conn, did, confirmed_by="t", now=NOW10,
                             orders_dir=orders, price_override=12.0)
        assert res is None
        assert conn.execute("SELECT COUNT(*) FROM risk_event WHERE"
                            " rule='price_recheck'").fetchone()[0] >= 1
        assert conn.execute("SELECT status FROM decision WHERE id=?",
                            (did,)).fetchone()[0] == "approved"
        # 合法显式价（=昨收 11.0 未超涨停）照常成交——校验拦错价不拦对价
        res2 = runner.confirm(conn, did, confirmed_by="t", now=NOW10,
                              orders_dir=orders, price_override=11.0)
        assert res2 is not None and res2["ok"]
    finally:
        shutil.rmtree(orders, ignore_errors=True)


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
