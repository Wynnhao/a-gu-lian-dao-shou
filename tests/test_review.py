"""复盘层单元测试：pytest 风格，亦可直接 `python3 tests/test_review.py` 运行。

不连真实 market.db：全部用 sqlite3.connect(":memory:") + data.fetcher.DDL 建表后插合成数据
（注意 get_conn() 固定连真实库，本测试不使用它）。期初资金取自真实 config.json（1000000）。
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from pathlib import Path as _Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

# ---- 测试隔离 env（W-D5 / P1-29/30：对照 test_pipeline_full 的正确模式）----
# 本文件曾以 backup/restore 直写生产 logs/backtest_result.json（非原子，测试
# 中途被 kill 会把合成 verdict 永久留在生产位）。现 profile_verdict 走 bt_path
# 注入，不再碰生产位；本 env 快照兜底防 AGSICKLE_* 泄漏到后续测试文件。
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


from data.fetcher import DDL
from review import daily, weekly

START = daily.START_CASH  # 1,000,000.0


def _offline_fetcher(start8, end8):
    raise ConnectionError("测试离线环境，不应发起真实抓取")


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    return conn


def add_bar(conn, code, d, close, pct=0.0, prev_close=None):
    conn.execute(
        "INSERT OR REPLACE INTO daily_bar (code, trade_date, open, high, low, close,"
        " volume, amount, pct_chg, turnover) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (code, d, prev_close if prev_close is not None else close,
         close, close, close, 10000, close * 10000, pct, 1.0),
    )


def add_pos(conn, code, name, shares, cost):
    conn.execute(
        "INSERT OR REPLACE INTO position VALUES (?,?,?,?,?,?)",
        (code, name, shares, shares, cost, "2099-01-01T00:00:00"),
    )


def add_trade(conn, d, code, name, side, price, shares):
    conn.execute(
        "INSERT INTO trade VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (d, code, name, side, price, shares, price * shares, "ord1", "filled",
         1, "", "human", f"{d}T09:35:00"),
    )


def add_state(conn, d, total, cash=None):
    mv = 0.0 if cash is None else total - cash
    conn.execute(
        "INSERT OR REPLACE INTO portfolio_state VALUES (?,?,?,?,?,?,?)",
        (d, cash if cash is not None else total, mv, total, 0.0, 0, "test"),
    )


def approx(a, b, tol=1e-6):
    assert abs(a - b) <= tol, f"{a} != {b} (tol={tol})"


# ---------------------------------------------------------------- mark_to_market

def test_mark_to_market_basic_and_drawdown_peak_update():
    conn = make_conn()
    add_bar(conn, "600519", "2099-01-06", 800.0, pct=0.0)
    add_bar(conn, "600519", "2099-01-07", 880.0, pct=10.0)
    add_bar(conn, "600519", "2099-01-08", 792.0, pct=-10.0)
    add_pos(conn, "600519", "贵州茅台", 1000, 800.0)
    conn.commit()

    # 第1天：cash = 1,000,000 - 800*1000 = 200,000；mv = 800,000；total = 1,000,000
    st1 = daily.mark_to_market("2099-01-06", conn)
    approx(st1["cash"], 200000.0)
    approx(st1["market_value"], 800000.0)
    approx(st1["total"], 1000000.0)
    approx(st1["drawdown"], 0.0)

    # 第2天创新高：回撤应为 0，且历史 peak 更新为 1,080,000
    st2 = daily.mark_to_market("2099-01-07", conn)
    approx(st2["total"], 1080000.0)
    approx(st2["drawdown"], 0.0)
    approx(st2["peak"], 1000000.0)  # 计算当日回撤时的历史峰值（未含当日）

    # 第3天下跌：回撤 = 1 - 992000/1080000 = 0.0814815（peak 取自第2天落库行 -> 验证 peak 更新）
    st3 = daily.mark_to_market("2099-01-08", conn)
    approx(st3["total"], 992000.0)
    approx(st3["drawdown"], 1 - 992000.0 / 1080000.0, tol=1e-9)
    conn.close()


def test_mark_to_market_empty_position_table():
    conn = make_conn()
    st = daily.mark_to_market("2099-01-06", conn)
    approx(st["cash"], START)
    approx(st["market_value"], 0.0)
    approx(st["total"], START)
    approx(st["drawdown"], 0.0)
    assert st["kill_switch"] == 0
    # 已落库
    row = conn.execute("SELECT cash, market_value, total, drawdown FROM portfolio_state WHERE date='2099-01-06'").fetchone()
    assert row is not None
    approx(row[2], START)
    conn.close()


def test_mark_to_market_stale_price_noted():
    conn = make_conn()
    add_bar(conn, "000001", "2099-01-06", 10.0)
    add_pos(conn, "000001", "平安银行", 10000, 9.0)
    conn.commit()
    st = daily.mark_to_market("2099-01-10", conn)  # 当日无K线 -> 用 01-06 收盘并注明
    approx(st["market_value"], 100000.0)
    assert st["positions"][0]["bar_date"] == "2099-01-06"
    assert st["positions"][0]["stale"] is True
    assert any("000001" in s for s in st["stale_codes"])
    assert "000001" in st["note"]
    conn.close()


def test_mark_to_market_cash_from_trade_flows():
    conn = make_conn()
    add_bar(conn, "600519", "2099-01-07", 100.0)
    add_pos(conn, "600519", "贵州茅台", 1000, 90.0)
    add_trade(conn, "2099-01-07", "600519", "贵州茅台", "buy", 100.0, 1000)  # amount=100,000
    # 已撤单成交不影响资金
    conn.execute(
        "INSERT INTO trade VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("2099-01-07", "000001", "平安银行", "buy", 10.0, 100, 1000.0, "ord2",
         "cancelled", 2, "", "human", "2099-01-07T09:35:00"),
    )
    conn.commit()
    st = daily.mark_to_market("2099-01-07", conn)
    approx(st["cash"], START - 100000.0)
    approx(st["total"], START - 100000.0 + 100000.0)
    conn.close()


# ---------------------------------------------------------------- portfolio_pnl

def test_portfolio_pnl_state_diff():
    conn = make_conn()
    add_state(conn, "2099-01-06", 1000000.0, cash=1000000.0)
    add_state(conn, "2099-01-07", 1010000.0, cash=1010000.0)
    conn.commit()
    r = daily.portfolio_pnl("2099-01-07", conn)
    assert r["method"] == "portfolio_state_diff"
    approx(r["day_pnl"], 10000.0)
    assert r["prev_date"] == "2099-01-06"
    approx(r["prev_total"], 1000000.0)
    conn.close()


def test_portfolio_pnl_reconstruct_buy_today():
    """当日买入的影响要剔除：期初持股 = 当前1000 - 当日买200 = 800 -> 800×(110-100)=8000"""
    conn = make_conn()
    add_bar(conn, "600519", "2099-01-06", 100.0)
    add_bar(conn, "600519", "2099-01-07", 110.0, pct=10.0)
    add_pos(conn, "600519", "贵州茅台", 1000, 90.0)
    add_trade(conn, "2099-01-07", "600519", "贵州茅台", "buy", 110.0, 200)
    conn.commit()
    r = daily.portfolio_pnl("2099-01-07", conn)
    assert r["method"] == "reconstructed_from_prev_close"
    assert r["prev_total"] is None
    approx(r["day_pnl"], 8000.0)
    approx(r["per_code"]["600519"], 8000.0)
    conn.close()


def test_portfolio_pnl_reconstruct_sell_today():
    """当日卖出：期初持股 = 当前800 + 当日卖200 = 1000 -> 1000×(110-100)=10000"""
    conn = make_conn()
    add_bar(conn, "600519", "2099-01-06", 100.0)
    add_bar(conn, "600519", "2099-01-07", 110.0, pct=10.0)
    add_pos(conn, "600519", "贵州茅台", 800, 90.0)
    add_trade(conn, "2099-01-07", "600519", "贵州茅台", "sell", 110.0, 200)
    conn.commit()
    r = daily.portfolio_pnl("2099-01-07", conn)
    approx(r["day_pnl"], 10000.0)
    conn.close()


# ---------------------------------------------------------------- 空表 / 降级

def test_empty_tables_no_crash():
    conn = make_conn()
    r = daily.portfolio_pnl("2099-01-06", conn)
    approx(r["day_pnl"], 0.0)
    approx(r["total"], START)

    out = _Path(tempfile.mkdtemp())
    p = daily.generate_daily_report("2099-01-06", conn, out_dir=out)
    text = p.read_text(encoding="utf-8")
    for head in ["## 决策回顾", "## 成交明细", "## 持仓与当日盈亏", "## 基准对比", "## AI自我评估"]:
        assert head in text, f"缺少小节 {head}"
    assert "暂无数据" in text
    assert "当日无决策" in text
    assert "基准数据缺失" in text

    pw = weekly.weekly_report("2099-01-10", conn, out_dir=out, fetcher=_offline_fetcher)
    tw = pw.read_text(encoding="utf-8")
    for head in ["## 组合表现", "## 基准对比（沪深300 000300）", "## 简单归因（简化口径）", "## 周内成交"]:
        assert head in tw, f"周报缺少小节 {head}"
    assert "暂无数据" in tw
    conn.close()


# ---------------------------------------------------------------- 每日报告内容与数值

def test_daily_report_content_and_numbers():
    conn = make_conn()
    add_bar(conn, "600519", "2099-01-06", 100.0)
    add_bar(conn, "600519", "2099-01-07", 110.0, pct=10.0)
    add_pos(conn, "600519", "贵州茅台", 1000, 90.0)
    add_trade(conn, "2099-01-07", "600519", "贵州茅台", "buy", 110.0, 200)
    conn.execute(
        "INSERT INTO decision (run_date, code, action, target_weight, confidence, reasons, risk_notes, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("2099-01-07", "600519", "buy", 0.10, 0.7,
         '["理由A: MA5上穿MA20", "理由B: 放量突破"]', '["风险: 短线追高"]', "executed",
         "2099-01-07T09:31:00"),
    )
    conn.execute("INSERT OR REPLACE INTO index_daily (index_code, trade_date, close) VALUES ('000300','2099-01-06',4000.0)")
    conn.execute("INSERT OR REPLACE INTO index_daily (index_code, trade_date, close) VALUES ('000300','2099-01-07',4040.0)")  # +1%
    conn.commit()

    out = _Path(tempfile.mkdtemp())
    p = daily.generate_daily_report("2099-01-07", conn, out_dir=out)
    text = p.read_text(encoding="utf-8")

    # 决策回顾：原文引用
    assert "理由A: MA5上穿MA20" in text and "理由B: 放量突破" in text
    assert "executed" in text
    # AI自我评估：risk_notes 原文
    assert "风险: 短线追高" in text
    # 成交明细
    assert "110.00" in text and "买入" in text and "human" in text
    # 持仓数值：市值=1000×110=110,000；浮动盈亏=1000×(110-90)=20,000
    assert "110,000.00" in text and "20,000.00" in text
    # 当日涨跌幅
    assert "+10.00%" in text
    # 组合：cash=1,000,000-22,000=978,000；total=978,000+110,000=1,088,000 -> 累计 +8.80%
    assert "978,000.00" in text and "+8.80%" in text
    # 当日盈亏（还原口径）：800×(110-100)=8,000
    assert "8,000.00" in text
    # 基准：+1.00%，组合日收益 8000/1,000,000? 无上日状态 -> prev_total None。
    # 这里已先跑过 mark_to_market（state 有 01-07 行），无 01-06 行 -> prev 为 None，
    # 但 reconstruct 分支给 day_pnl=8000，prev_total=None -> 组合日收益率 n/a，仍应有基准 +1.00%
    assert "+1.00%" in text

    # 若补上 01-06 状态，则应输出相对收益
    add_state(conn, "2099-01-06", 1000000.0, cash=1000000.0)
    conn.commit()
    p2 = daily.generate_daily_report("2099-01-07", conn, out_dir=out)
    text2 = p2.read_text(encoding="utf-8")
    assert "+1.00%" in text2  # 基准
    conn.close()


def test_daily_report_benchmark_missing_then_present():
    conn = make_conn()
    add_bar(conn, "600519", "2099-01-07", 110.0)
    add_pos(conn, "600519", "贵州茅台", 100, 100.0)
    conn.commit()
    out = _Path(tempfile.mkdtemp())
    t1 = daily.generate_daily_report("2099-01-07", conn, out_dir=out).read_text(encoding="utf-8")
    assert "基准数据缺失" in t1
    conn.execute("INSERT OR REPLACE INTO index_daily (index_code, trade_date, close) VALUES ('000300','2099-01-06',4000.0)")
    conn.execute("INSERT OR REPLACE INTO index_daily (index_code, trade_date, close) VALUES ('000300','2099-01-07',4000.0)")
    conn.commit()
    t2 = daily.generate_daily_report("2099-01-07", conn, out_dir=out).read_text(encoding="utf-8")
    assert "基准数据缺失" not in t2
    assert "沪深300收盘" in t2
    conn.close()


# ---------------- 批次4a（全量批 P2 清债·风控执行域）2026-09-20 ----------------

def test_mark_to_market_replay_before_dd_base_not_zeroed():
    """P2①：补跑 dd_base_date **之前**的历史日报——旧口径 effective_peak 的
    after=dd_base_date 与 before=回放日 组合恒空窗 → 回撤记 0，且 mark_to_market
    的 INSERT OR REPLACE 把该历史日 drawdown 列覆写为 0。修复后回放日早于重置日
    → 回退 250 行窗口：回撤按回放日之前的窗口峰值计算（1 - 100万/200万 = 50%）。"""
    import json as _json
    from execution import runner as _runner

    conn = make_conn()
    add_state(conn, "2099-01-05", 2_000_000.0)
    add_state(conn, "2099-01-06", 1_000_000.0)
    conn.commit()
    ks_dir = tempfile.mkdtemp(prefix="agsickle_review_ddbase_")
    ks_file = Path(ks_dir) / "kill.json"
    orig_ks = _runner.KILL_STATE_FILE
    _runner.KILL_STATE_FILE = ks_file
    try:
        ks_file.write_text(_json.dumps(
            {"active": False, "until": None, "note": "", "dd_base": 900_000.0,
             "dd_base_date": "2099-01-10", "kill_count": 1}, ensure_ascii=False),
            encoding="utf-8")
        st = daily.mark_to_market("2099-01-06", conn)  # 空持仓 → total=START_CASH
        approx(st["total"], START)
        approx(st["drawdown"], 1 - START / 2_000_000.0)   # 0.5，不是 0
        # 覆写守卫：补跑后该历史日 drawdown 列保留窗口口径值（旧口径被覆写为 0）
        row = conn.execute(
            "SELECT drawdown FROM portfolio_state WHERE date='2099-01-06'").fetchone()
        assert row is not None
        approx(row[0], 1 - START / 2_000_000.0)
    finally:
        _runner.KILL_STATE_FILE = orig_ks
        conn.close()


def test_daily_report_kill_history_observation_line():
    """P2⑤（ADR-S4-1 对冲观测接线）：kill.json 的 kill_count/lifetime_peak 此前
    只写不读（无消费方）——分段 8% 口径下熔断史静默。日报必须输出熔断史累计
    观测行；kill.json 缺累计键/缺失时无此行（行为不变）。"""
    import json as _json
    from execution import runner as _runner

    conn = make_conn()
    add_bar(conn, "600519", "2099-01-06", 800.0)
    add_pos(conn, "600519", "贵州茅台", 1000, 800.0)
    conn.commit()
    ks_dir = tempfile.mkdtemp(prefix="agsickle_review_killks_")
    ks_file = Path(ks_dir) / "kill.json"
    orig_ks = _runner.KILL_STATE_FILE
    _runner.KILL_STATE_FILE = ks_file
    out = _Path(tempfile.mkdtemp())
    try:
        # 负例：kill.json 存在但无累计键 → 不输出熔断史行
        ks_file.write_text(_json.dumps(
            {"active": False, "until": None, "note": ""}, ensure_ascii=False),
            encoding="utf-8")
        t1 = daily.generate_daily_report("2099-01-06", conn, out_dir=out).read_text(encoding="utf-8")
        assert "熔断史" not in t1
        # 正例：kill_count=2 / lifetime_peak=160 万 → 熔断史行 + 全局回撤
        # （total=200,000+800,000=1,000,000 → 全局回撤 1-100万/160万 = -37.50%）
        ks_file.write_text(_json.dumps(
            {"active": False, "until": None, "note": "", "kill_count": 2,
             "lifetime_peak": 1_600_000.0}, ensure_ascii=False), encoding="utf-8")
        t2 = daily.generate_daily_report("2099-01-06", conn, out_dir=out).read_text(encoding="utf-8")
        assert "熔断史" in t2 and "kill_count=2" in t2
        assert "1,600,000.00" in t2 and "-37.50%" in t2
    finally:
        _runner.KILL_STATE_FILE = orig_ks
        conn.close()


# ---------------------------------------------------------------- 周报数值断言（手算样例）

def seed_week(conn, end_close, week_ret_target_total):
    """2024-01-08(周一)~2024-01-12(周五)；基准日 2024-01-05(上周五)。"""
    add_bar(conn, "600519", "2024-01-05", 500.0)
    add_bar(conn, "600519", "2024-01-12", end_close)
    conn.execute("INSERT OR REPLACE INTO index_daily (index_code, trade_date, close) VALUES ('000300','2024-01-05',4000.0)")
    conn.execute("INSERT OR REPLACE INTO index_daily (index_code, trade_date, close) VALUES ('000300','2024-01-12',4080.0)")  # 基准 +2%
    add_pos(conn, "600519", "贵州茅台", 1000, 480.0)
    add_state(conn, "2024-01-05", 1000000.0, cash=500000.0)          # 期初：市值50万+现金50万
    add_state(conn, "2024-01-12", week_ret_target_total, cash=500000.0)
    conn.commit()


def test_weekly_report_numbers():
    conn = make_conn()
    # 场景1：个票 +2% 与基准持平 -> 选股贡献0，组合+2%全来自择时贡献
    seed_week(conn, 510.0, 1020000.0)
    out = _Path(tempfile.mkdtemp())
    p = weekly.weekly_report("2024-01-12", conn, out_dir=out)
    assert p.name == "2024-W02.md"
    t = p.read_text(encoding="utf-8")
    assert "+2.00%" in t                     # 组合周收益 与 基准周收益
    assert "+0.00%" in t                     # 超额 与 选股贡献
    assert "+2.00%" in t and "择时贡献" in t  # 择时贡献 = 2%
    assert "50.00%" in t                     # 期初总仓位

    # 场景2：个票 +4% -> 选股贡献 = 0.5×4% - 0.5×2% = 1%；择时 = 2% - 1% = 1%
    conn2 = make_conn()
    seed_week(conn2, 520.0, 1020000.0)
    p2 = weekly.weekly_report("2024-01-12", conn2, out_dir=out)
    t2 = p2.read_text(encoding="utf-8")
    assert "+1.00%" in t2 and "选股贡献" in t2
    conn.close()
    conn2.close()


def test_weekly_benchmark_missing_degrades():
    conn = make_conn()
    add_state(conn, "2024-01-05", 1000000.0, cash=1000000.0)
    add_state(conn, "2024-01-12", 1010000.0, cash=1010000.0)
    conn.commit()
    out = _Path(tempfile.mkdtemp())
    t = weekly.weekly_report("2024-01-12", conn, out_dir=out, fetcher=_offline_fetcher).read_text(encoding="utf-8")
    assert "+1.00%" in t              # 组合周收益仍可算
    assert "基准数据缺失" in t
    conn.close()


# ---------------------------------------------------------------- ensure_benchmark

def test_ensure_benchmark_existing_skips_fetch():
    conn = make_conn()
    conn.execute("INSERT OR REPLACE INTO index_daily (index_code, trade_date, close) VALUES ('000300','2024-01-12',4000.0)")
    conn.commit()

    def _should_not_run(start8, end8):
        raise AssertionError("已有截至 end_date 的数据时不应发起抓取")

    r = weekly.ensure_benchmark("2024-01-12", conn, fetcher=_should_not_run)
    assert r["ok"] is True and r["source"] == "existing" and r["rows"] == 0
    conn.close()


def test_ensure_benchmark_stale_row_triggers_fetch():
    """W-B2（P0-5）：兜底条件 COUNT>0 → MAX(trade_date)>=end_date——窗口内只有
    陈旧行（停更于 01-10 < end 01-12）必须触发补数，不再"有历史行即跳过"。"""
    conn = make_conn()
    conn.execute("INSERT OR REPLACE INTO index_daily (index_code, trade_date, close) VALUES ('000300','2024-01-10',4000.0)")
    conn.commit()
    import pandas as pd

    def fake_fetch(start8, end8):
        return pd.DataFrame({"date": ["2024-01-11", "2024-01-12"], "close": [4010.0, 4020.0]})

    r = weekly.ensure_benchmark("2024-01-12", conn, fetcher=fake_fetch)
    assert r["ok"] is True and r["rows"] == 2 and r["source"] == "fake_fetch", r
    latest = conn.execute(
        "SELECT MAX(trade_date) FROM index_daily WHERE index_code='000300'").fetchone()[0]
    assert latest == "2024-01-12"
    conn.close()


def test_ensure_benchmark_fetch_and_insert():
    conn = make_conn()
    import pandas as pd

    def fake_fetch(start8, end8):
        assert start8 == "20231213" and end8 == "20240112"
        return pd.DataFrame({"date": ["2024-01-08", "2024-01-09"], "close": [3900.0, 3910.0]})

    r = weekly.ensure_benchmark("2024-01-12", conn, fetcher=fake_fetch)
    assert r["ok"] is True and r["rows"] == 2 and r["source"] == "fake_fetch"
    n = conn.execute("SELECT COUNT(*) FROM index_daily WHERE index_code='000300'").fetchone()[0]
    assert n == 2
    conn.close()


def test_ensure_benchmark_fetch_fail_degrades():
    conn = make_conn()

    def bad_fetch(start8, end8):
        raise ConnectionError("网络不通")

    r = weekly.ensure_benchmark("2024-01-12", conn, fetcher=bad_fetch)
    assert r["ok"] is False and "ConnectionError" in (r["error"] or "")
    conn.close()


# ---------------------------------------------------------------- 直接运行入口

def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:  # noqa: BLE001
            failed.append(name)
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed"
          + ("" if not failed else f"; failed: {', '.join(failed)}"))
    return 1 if failed else 0


# ============================================================
# Sprint 1 任务 1 + 任务 2：signal_eval 落盘 + profile 投票
# ============================================================

def _write_bt(tmp_path, cur_ann=-0.0801, alt_ann=0.3554,
              cur_mdd=-0.3615, alt_mdd=-0.3224):
    """写一份临时 backtest_result.json，返回路径供 profile_verdict(bt_path=...) 注入。

    W-D5（P1-29）：测试不再 backup/restore 生产 logs/backtest_result.json——
    那条路非原子，测试中途被 kill 会把合成 verdict 永久留在生产位，premarket
    与周报会基于假数据出 switch 建议甚至写红线 risk_event。

    与 verdict 逻辑对齐：当前 profile = momentum（config 当前值），备选 = reversal_lowvol/v2。
    测试场景的 cur_ann/cur_mdd 视为当前 profile 的数字，alt_* 视为备选（verdict 取最优 alt）。
    """
    import json as _json
    p = tmp_path / "backtest_result.json"
    payload = {
        # W-D6 元数据契约的最小形态（verdict 应把版本信息透出给 bundle）
        "generated_at": "2026-09-19 12:00:00",
        "universe": {"mode": "full", "codes": 816},
        "data_version": "test-fixture",
        "profiles": {
            # 当前 profile（verdict 取这里做 B/C_cur）
            "momentum": {
                "strategy_perf": {"annual_return": cur_ann, "max_drawdown": cur_mdd},
                "benchmark_hs300": {"annual_return": 0.1169, "max_drawdown": -0.1566},
            },
            # 备选（verdict 取年化超额最高者做 B_alt，取 MDD 最接近 0 者做 C_alt）
            "reversal_lowvol": {
                "strategy_perf": {"annual_return": alt_ann, "max_drawdown": alt_mdd},
                "benchmark_hs300": {"annual_return": 0.1169, "max_drawdown": -0.1566},
            },
            "reversal_lowvol_v2": {
                # v2 给一个介于 cur/alt 之间的备份，让 B/C 各取极值时仍能挑出 alt
                "strategy_perf": {"annual_return": (cur_ann + alt_ann) / 2,
                                   "max_drawdown": (cur_mdd + alt_mdd) / 2},
                "benchmark_hs300": {"annual_return": 0.1169, "max_drawdown": -0.1566},
            },
        },
    }
    p.write_text(_json.dumps(payload), encoding="utf-8")
    return p


def test_signal_eval_persists_latest(tmp_path=None):
    """任务 1：evaluate() 落盘到 AGSICKLE_SIGNAL_EVAL_DIR 隔离目录（K3：不得碰生产
    logs/signal_eval/）。"""
    import json as _json
    from review import signal_eval
    sandbox = tempfile.mkdtemp(prefix="agsickle_signal_eval_test_")
    old_env = os.environ.get("AGSICKLE_SIGNAL_EVAL_DIR")
    os.environ["AGSICKLE_SIGNAL_EVAL_DIR"] = sandbox
    conn = make_conn()
    try:
        payload = signal_eval.evaluate(conn)
        persist = signal_eval._persist_latest(payload)
        assert persist.get("error") is None, persist
        p = _Path(persist["path"])
        latest = _Path(persist["latest_path"])
        assert p.exists() and latest.exists()
        # 落盘目录确为沙箱，生产目录未被触碰
        assert str(p).startswith(sandbox)
        assert not (BASE / "logs" / "signal_eval" / latest.name).exists() or \
            _json.loads((BASE / "logs" / "signal_eval" / latest.name)
                        .read_text(encoding="utf-8")) != payload
        # latest.json 可被解析回 dict
        loaded = _json.loads(latest.read_text(encoding="utf-8"))
        assert "profile_verdict" in loaded
        assert "factor_ic" in loaded
        # P2-14：落盘后不留 tmp 半写文件（原子写 tmp+os.replace）
        assert not (_Path(sandbox) / "latest.json.tmp").exists()
    finally:
        conn.close()
        _restore_env()


def test_p2g12_signal_frame_excludes_null_profile_rows():
    """P2-⑫ 负向（预注册要求）：_signal_frame 的 `OR profile IS NULL` 兼容条款
    已删——NULL 行不得进入 IC 评估（潜伏混算闸）；只取当前 profile 行。"""
    from review import signal_eval
    old_prof = os.environ.get("AGSICKLE_SIGNALS_PROFILE")
    os.environ["AGSICKLE_SIGNALS_PROFILE"] = "reversal_lowvol"
    conn = __import__("sqlite3").connect(":memory:")
    conn.executescript(DDL)
    try:
        conn.executemany(
            "INSERT INTO signal (code, as_of, signals, score, profile)"
            " VALUES (?,?,?,?,?)",
            [("600519", "2026-09-18", "{}", 1.5, "reversal_lowvol"),
             ("600519", "2026-09-17", "{}", 2.5, "momentum"),
             ("600519", "2026-09-16", "{}", 3.5, None)])
        conn.commit()
        df = signal_eval._signal_frame(conn)
        rows = {(r["code"], r["as_of"]) for _, r in df.iterrows()}
        assert rows == {("600519", "2026-09-18")}, rows
        # 全 NULL → 空 frame（不混入任何口径）
        conn.execute("UPDATE signal SET profile=NULL")
        conn.commit()
        df2 = signal_eval._signal_frame(conn)
        assert df2.empty, "profile 全 NULL 时 _signal_frame 必须为空"
    finally:
        conn.close()
        if old_prof is None:
            os.environ.pop("AGSICKLE_SIGNALS_PROFILE", None)
        else:
            os.environ["AGSICKLE_SIGNALS_PROFILE"] = old_prof
        _restore_env()


def test_profile_verdict_both_lost_switch(tmp_path=None):
    """投票 case1：B+C 双劣 → switch（v1.7 verdict 顶层结构：votes/b_switch/c_switch/red_line_triggered）。

    W-D5：合成回测产物经 bt_path 注入，全程不触碰生产 backtest_result.json。"""
    from review import signal_eval
    bt = _write_bt(_Path(tempfile.mkdtemp()),
                   cur_ann=-0.10, alt_ann=0.30,    # B_cur − B_alt = -0.40 → 投切换
                   cur_mdd=-0.10, alt_mdd=-0.03)   # C_cur − C_alt = -0.07 → 投切换
    conn = make_conn()
    try:
        v = signal_eval.profile_verdict(conn, bt_path=bt)
        assert v["B"]["gap"] is not None and v["B"]["gap"] < -0.10, v["B"]
        assert v["C"]["gap"] is not None and v["C"]["gap"] < -0.05, v["C"]
        assert v["red_line_triggered"] is False  # MDD -10% > -30%
        assert v["verdict"] == "switch", v
        assert v["confidence"] == "high"  # 2/2 → high
        # v1.7：suggest_profile 切换到 B 维较优者（"alt"按 Fix-5 取年化超额最高）
        assert v["suggest_profile"] != v["profile"]
        # W-D6 元数据透出（bundle 引用红线数字必须带版本）
        assert v["backtest_generated_at"] == "2026-09-19 12:00:00", v
        assert v["backtest_universe"] == "full", v
        assert str(bt) == v["backtest_path"], v
    finally:
        conn.close()


def test_profile_verdict_only_c_vote_switch_low_confidence(tmp_path=None):
    """Fix-6 投票 case2（v1.5）：仅 C 投切换（B 不够差）→ switch + confidence=low。"""
    from review import signal_eval
    bt = _write_bt(_Path(tempfile.mkdtemp()),
                   cur_ann=0.10, alt_ann=0.15,    # B gap=-5pp > -10pp → 不投
                   cur_mdd=-0.20, alt_mdd=-0.10)  # C gap=-10pp < -5pp → 投
    conn = make_conn()
    try:
        v = signal_eval.profile_verdict(conn, bt_path=bt)
        assert v["votes"]["b_switch"] is False, v["votes"]
        assert v["votes"]["c_switch"] is True, v["votes"]
        assert v["red_line_triggered"] is False  # -20% > -30%
        assert v["verdict"] == "switch", v
        assert v["confidence"] == "low", v
        # suggest_profile 切到 B 维较优备选（Fix-5 取最高年化超额）
        assert v["suggest_profile"] != v["profile"], v
    finally:
        conn.close()


def test_profile_verdict_env_injection(tmp_path=None):
    """W-D5：bt_path 缺省时读 AGSICKLE_BACKTEST_RESULT env（测试注入点第二落点）。"""
    from review import signal_eval
    bt = _write_bt(_Path(tempfile.mkdtemp()),
                   cur_ann=-0.10, alt_ann=0.30,
                   cur_mdd=-0.10, alt_mdd=-0.03)
    old = os.environ.get("AGSICKLE_BACKTEST_RESULT")
    os.environ["AGSICKLE_BACKTEST_RESULT"] = str(bt)
    conn = make_conn()
    try:
        v = signal_eval.profile_verdict(conn)
        assert v["backtest_missing"] is None, v
        assert v["B"]["current"] is not None, v
        assert v["backtest_path"] == str(bt), v
    finally:
        conn.close()
        if old is None:
            os.environ.pop("AGSICKLE_BACKTEST_RESULT", None)
        else:
            os.environ["AGSICKLE_BACKTEST_RESULT"] = old


def test_profile_verdict_red_line_overrides(tmp_path=None):
    """投票 case3：MDD 破 -30% → 无条件 hold，suggest_profile 保持现状（v1.7 不再自动建议切换）。"""
    from review import signal_eval
    bt = _write_bt(_Path(tempfile.mkdtemp()),
                   cur_ann=-0.10, alt_ann=0.30,    # B 投切换
                   cur_mdd=-0.40, alt_mdd=-0.10)  # C 也投切换 + 触发红线
    conn0 = make_conn()
    try:
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        conn0.execute(
            "DELETE FROM risk_event WHERE rule='profile_verdict_red_line'"
            " AND ts LIKE ?", (today + "%",))
        conn0.commit()
    finally:
        conn0.close()
    conn = make_conn()
    try:
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        v = signal_eval.profile_verdict(conn, bt_path=bt)
        assert v["red_line_triggered"] is True, v
        assert v["verdict"] == "hold", v
        # v1.7 红线只 hold，不切：suggest_profile = 当前 profile
        assert v["suggest_profile"] == v["profile"], v
        n = conn.execute(
            "SELECT COUNT(*) FROM risk_event WHERE rule='profile_verdict_red_line'"
            " AND ts LIKE ?", (today + "%",)).fetchone()[0]
        assert n >= 1, "红线 override 必须写 risk_event"
        # 红线事件必须带回测版本号（P1-25/W-D6：引用带版本）
        row = conn.execute(
            "SELECT detail FROM risk_event WHERE rule='profile_verdict_red_line'"
            " AND ts LIKE ? LIMIT 1", (today + "%",)).fetchone()
        assert "2026-09-19 12:00:00" in (row[0] or ""), row
    finally:
        conn.close()


def test_profile_verdict_three_profile_alts_pick_best():
    """Fix-5：三 profile 备选显式枚举——v1 当前时备选 = momentum + v2，
    B/C 各取备选较优者对比（B 从 momentum 取、C 从 v2 取）。"""
    import json as _json
    from review import signal_eval
    import signals.signals as sig_mod
    orig_profile = sig_mod.profile
    sig_mod.profile = lambda: "reversal_lowvol"
    bt = _Path(tempfile.mkdtemp()) / "backtest_result.json"
    payload = {
        "profiles": {
            "reversal_lowvol": {
                "strategy_perf": {"annual_return": 0.60, "max_drawdown": -0.15},
                "benchmark_hs300": {"annual_return": 0.10, "max_drawdown": -0.15},
            },
            "momentum": {  # B 较优（超额 +20pp）
                "strategy_perf": {"annual_return": 0.30, "max_drawdown": -0.20},
                "benchmark_hs300": {"annual_return": 0.10, "max_drawdown": -0.15},
            },
            "reversal_lowvol_v2": {  # C 较优（MDD 最浅）
                "strategy_perf": {"annual_return": 0.12, "max_drawdown": -0.08},
                "benchmark_hs300": {"annual_return": 0.10, "max_drawdown": -0.15},
            },
        },
    }
    bt.write_text(_json.dumps(payload), encoding="utf-8")
    conn = make_conn()
    try:
        v = signal_eval.profile_verdict(conn, bt_path=bt)
        assert v["other_profiles"] == ["momentum", "reversal_lowvol_v2"]
        assert v["alt_best"]["b_from"] == "momentum"
        assert v["alt_best"]["c_from"] == "reversal_lowvol_v2"
        # B_alt=+0.20（momentum 超额）；C_alt=-0.08（v2 的 MDD）
        assert abs(v["B"]["alt"] - 0.20) < 1e-9
        assert abs(v["C"]["alt"] - (-0.08)) < 1e-9
        # B gap=+0.30 远优于备选 → 不投；C gap=-0.07 投 → 1 票（Fix-6 ≥1 票即建议）
        assert v["votes"]["votes_switch"] == 1
        assert v["verdict"] == "switch"
        assert v["confidence"] == "low"
        # switch 目标 = B 维较优备选 momentum
        assert v["suggest_profile"] == "momentum"
    finally:
        conn.close()
        sig_mod.profile = orig_profile


def test_profile_verdict_abstain_when_bt_missing(tmp_path=None):
    """任务 2 投票 case4：backtest_result.json 缺失 → B/C 全弃权 → hold。"""
    from review import signal_eval
    missing = _Path(tempfile.mkdtemp()) / "nope.json"
    conn = make_conn()
    try:
        v = signal_eval.profile_verdict(conn, bt_path=missing)
        assert "B (年化超额 vs HS300)" in v["abstains"]
        assert "C (MDD)" in v["abstains"]
        assert v["verdict"] == "hold"
        assert v["backtest_missing"] is not None
    finally:
        conn.close()


def test_profile_verdict_bt_file_not_touched():
    """W-D5 回归护栏：verdict 全路径（含红线 override）不得写/删注入的 bt 文件，
    更不得触碰生产 backtest_result.json 位。"""
    from review import signal_eval
    bt = _write_bt(_Path(tempfile.mkdtemp()),
                   cur_ann=-0.10, alt_ann=0.30,
                   cur_mdd=-0.40, alt_mdd=-0.10)  # 触发红线 override
    before = bt.read_bytes()
    prod = BASE / "logs" / "backtest_result.json"
    prod_before = prod.read_bytes() if prod.exists() else None
    conn = make_conn()
    try:
        v = signal_eval.profile_verdict(conn, bt_path=bt)
        assert v["red_line_triggered"] is True
    finally:
        conn.close()
    assert bt.read_bytes() == before, "注入的 bt 文件被改动"
    assert (prod.read_bytes() if prod.exists() else None) == prod_before, \
        "生产 backtest_result.json 被测试触碰"


def test_profile_verdict_compat_with_weekly():
    """v1.4 verdict dict 兼容 v1.3 周报消费方（保留旧字段 score_ic_h5）。"""
    from review import signal_eval
    conn = make_conn()
    try:
        v = signal_eval.profile_verdict(conn)
        # v1.3 周报要读的旧字段必须仍在
        assert "score_ic_h5" in v
        assert "ic_thresholds_v13" in v
        assert v["ic_thresholds_v13"]["keep_ge"] == 0.02
    finally:
        conn.close()


def test_profile_verdict_abstain_reason_corrected_2c():
    """2c（2026-09-21 P0-B 复核）：A/D 弃权理由不得再引用已失实的「momentum 无 signal
    表 score 行」——momentum 在 signal 表有 score 行（生产只读实测 55,418 行），维持
    弃权的真实原因 = v1.4「B+C 两维」框架设计。理由文本必须带更正标注，且本更正不改
    投票逻辑（verdict/votes 字段语义与既有用例一致）。"""
    from review import signal_eval
    bt = _write_bt(_Path(tempfile.mkdtemp()),
                   cur_ann=0.30, alt_ann=0.30,     # B gap=0 → 不投（基准 0.1169）
                   cur_mdd=-0.10, alt_mdd=-0.25)   # C 维 v2 取中位 -0.175 → gap=+7.5pp 不投；红线不触发
    conn = make_conn()
    old = os.environ.get("AGSICKLE_SIGNALS_PROFILE")
    os.environ["AGSICKLE_SIGNALS_PROFILE"] = "momentum"
    try:
        # 种 momentum score 行（生产形态：momentum 有 score 行 → 旧理由失实）
        for i in range(5):
            conn.execute(
                "INSERT OR REPLACE INTO signal (code, as_of, signals, score, profile)"
                " VALUES (?,?,?,?,?)",
                (f"60000{i}", f"2026-09-0{i + 1}", "{}", 0.5 + i, "momentum"))
        conn.commit()
        v = signal_eval.profile_verdict(conn, bt_path=bt)
        # 旧失实文案必须消失
        assert "无 signal 表 score 行，A/D 仅 current 可算" not in v["abstain_reason"], \
            v["abstain_reason"]
        assert "A/D 因 momentum 无 signal 行弃权" not in v["note"], v["note"]
        # 新文案：框架设计定性 + 动态 score 行数 + 更正标注
        assert "框架设计，非数据缺失" in v["abstain_reason"], v["abstain_reason"]
        assert "score 行 5 行" in v["abstain_reason"], v["abstain_reason"]
        assert "2026-09-21 复核更正" in v["abstain_reason"], v["abstain_reason"]
        assert "已于 2026-09-21 复核证伪" in v["note"], v["note"]
        # 投票逻辑零变化：B/C 双不投 → keep、无弃权维度
        assert v["verdict"] == "keep", v["verdict"]
        assert v["abstains"] == [], v["abstains"]
        assert v["votes"]["votes_switch"] == 0, v["votes"]
    finally:
        conn.close()
        if old is None:
            os.environ.pop("AGSICKLE_SIGNALS_PROFILE", None)
        else:
            os.environ["AGSICKLE_SIGNALS_PROFILE"] = old
        _restore_env()


if __name__ == "__main__":
    raise SystemExit(_main())
