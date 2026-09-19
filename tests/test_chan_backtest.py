"""缠论 R3 批次 2a —— 组合回测引擎 + 受控 momentum 基线腿单测（纯合成数据，绝不连 DB）。

施工方案：docs/缠论R3施工方案-2026-09-19.md §3.3 + §3.6-8/10/11/12/13。
纪律：本文件零 DB 访问（连只读也不允许）；全部用手工构造的 2~5 票合成日线驱动
signals/chan_backtest.py 的引擎与基线腿，逐项核对 §3.6-12 记账恒等式与 §3.6-10
基线选票规则。open_qfq 构造复用 signals/chan_data.with_open_qfq（§3.1 单一来源）。
"""
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

os.environ["AGSICKLE_DISABLE_LIVE_QUOTES"] = "1"  # 测试保持离线

from signals.chan_backtest import (Instruction, MAX_POSITIONS, TARGET_WEIGHT,
                                   momentum_baseline_leg,
                                   run_portfolio_backtest)
from signals.chan_data import with_open_qfq

_TESTS = []


def test(fn):
    _TESTS.append(fn)
    return fn


test.__test__ = False  # pytest 不要把装饰器本身当测试收集

CAL5 = ["2024-07-01", "2024-07-02", "2024-07-03", "2024-07-04", "2024-07-05"]


def make_bars(dates, opens, closes):
    """合成日线（close_qfq = close，open_qfq = open，§3.1 构造式经 with_open_qfq）。"""
    o = np.asarray(opens, dtype=float)
    c = np.asarray(closes, dtype=float)
    df = pd.DataFrame({
        "trade_date": list(dates),
        "open": o,
        "high": np.maximum(o, c) + 0.1,
        "low": np.minimum(o, c) - 0.1,
        "close": c,
        "close_qfq": c,
        "high_qfq": np.maximum(o, c) + 0.1,
        "low_qfq": np.minimum(o, c) - 0.1,
    })
    return with_open_qfq(df)


# ---------------- 1. 入场日记账（§3.6-11/12：名义×(close/open−1) − 0.15% 成本） ----------------

@test
def test_entry_day_accounting():
    bars = {"A": make_bars(CAL5[:3], [10.0, 11.0, 12.0], [10.5, 11.5, 12.5])}
    res = run_portfolio_backtest(bars, CAL5[:3],
                                 [Instruction("2024-07-01", "A", "buy", "open")])
    notional = TARGET_WEIGHT * 1.0                      # NAV_ref = 初始 1.0
    expect_n0 = 1.0 + notional * (10.5 / 10.0 - 1.0) - notional * 0.0015
    assert abs(res["nav"].iloc[0] - expect_n0) < 1e-12
    assert abs(res["returns"].iloc[0] - (expect_n0 - 1.0)) < 1e-12
    # 次日持仓盯市：现金不变，市值按 close_qfq 重估
    expect_n1 = (1.0 - notional * 1.0015) + (notional / 10.0) * 11.5
    assert abs(res["nav"].iloc[1] - expect_n1) < 1e-12
    t = res["trades"][0]
    assert t["side"] == "buy" and t["price_type"] == "open" and t["px"] == 10.0
    assert abs(t["gross"] - 0.2) < 1e-15
    assert abs(t["cost"] - 0.2 * 0.0015) < 1e-18        # 买入成本 = 名义 × 0.15%
    assert abs(t["shares"] - 0.02) < 1e-15
    assert abs(res["cash"].iloc[0] - (1.0 - 0.2 * 1.0015)) < 1e-12  # 支出 = 名义×(1+0.0015)


# ---------------- 2. 持仓日盯市 + 停牌顺延（§3.6-11：无 bar 沿用最近收盘、零收益） ----------------

@test
def test_mark_to_market_and_suspension_carry():
    dates = ["2024-07-01", "2024-07-02", "2024-07-04", "2024-07-05"]  # 07-03 停牌
    bars = {"A": make_bars(dates, [10.0, 11.0, 12.0, 13.0],
                           [10.5, 11.5, 12.5, 13.5])}
    res = run_portfolio_backtest(bars, CAL5,
                                 [Instruction("2024-07-01", "A", "buy", "open")])
    nav = res["nav"]
    notional = 0.2
    n0 = 1.0 + notional * (10.5 / 10.0 - 1.0) - notional * 0.0015
    assert abs(nav["2024-07-01"] - n0) < 1e-12
    n1 = (1.0 - notional * 1.0015) + (notional / 10.0) * 11.5
    assert abs(nav["2024-07-02"] - n1) < 1e-12
    # 停牌日：沿用旧收盘 → NAV 逐位不变、日收益恰为 0
    assert nav["2024-07-03"] == nav["2024-07-02"]
    assert res["returns"]["2024-07-03"] == 0.0
    # 复牌日恢复盯市
    n2 = (1.0 - notional * 1.0015) + (notional / 10.0) * 12.5
    assert abs(nav["2024-07-04"] - n2) < 1e-12


# ---------------- 3a. sell@open：当日无敞口，现金 = 成交市值×(1−0.0015) ----------------

@test
def test_sell_open_no_intraday_exposure():
    bars = {"A": make_bars(CAL5[:3], [10.0, 11.0, 100.0], [10.5, 5.0, 100.0])}
    res = run_portfolio_backtest(bars, CAL5[:3],
                                 [Instruction("2024-07-01", "A", "buy", "open"),
                                  Instruction("2024-07-02", "A", "sell", "open")])
    n1 = 1.0 + 0.2 * (10.5 / 10.0 - 1.0) - 0.2 * 0.0015
    assert abs(res["nav"]["2024-07-01"] - n1) < 1e-12
    # 07-02 收盘暴跌至 5.0 不得进 NAV（开盘即无敞口）
    proceeds = (0.2 / 10.0) * 11.0 * (1.0 - 0.0015)
    expect = (1.0 - 0.2 * 1.0015) + proceeds
    assert abs(res["nav"]["2024-07-02"] - expect) < 1e-12
    # 隔夜段进 NAV：ΔNAV = 股数×(open−prev_close) − 卖出成本
    assert abs((res["nav"]["2024-07-02"] - res["nav"]["2024-07-01"])
               - ((0.2 / 10.0) * (11.0 - 10.5)
                  - (0.2 / 10.0) * 11.0 * 0.0015)) < 1e-12
    t = res["trades"][-1]
    assert t["side"] == "sell" and t["px"] == 11.0
    assert abs(t["cost"] - (0.2 / 10.0) * 11.0 * 0.0015) < 1e-18
    # 此后全现金
    assert res["nav"]["2024-07-03"] == res["nav"]["2024-07-02"]


# ---------------- 3b. sell@close：全天敞口后按 close×(1−0.0015) 结算现金 ----------------

@test
def test_sell_close_full_day_exposure():
    bars = {"A": make_bars(CAL5[:3], [10.0, 5.0, 100.0], [10.5, 11.0, 100.0])}
    res = run_portfolio_backtest(bars, CAL5[:3],
                                 [Instruction("2024-07-01", "A", "buy", "open"),
                                  Instruction("2024-07-02", "A", "sell", "close")])
    # 07-02 开盘暴跌至 5.0 不得影响结算价；但全天 close−prev_close 波动须进 NAV
    expect = (1.0 - 0.2 * 1.0015) + (0.2 / 10.0) * 11.0 * (1.0 - 0.0015)
    assert abs(res["nav"]["2024-07-02"] - expect) < 1e-12
    assert abs((res["nav"]["2024-07-02"] - res["nav"]["2024-07-01"])
               - ((0.2 / 10.0) * (11.0 - 10.5)
                  - (0.2 / 10.0) * 11.0 * 0.0015)) < 1e-12
    t = res["trades"][-1]
    assert t["side"] == "sell" and t["price_type"] == "close" and t["px"] == 11.0


# ---------------- 4. 反前视（§3.6-12）：调仓日旧仓 sell@open、新仓 buy@open→close ----------------

@test
def test_anti_lookahead_rebalance_day_ordering():
    """月末信号 → 次日开盘调仓。次序错的实现会得出不同数字：旧仓若误按全天 close
    结算会多算 open→close 段 +0.02×(9.5−9.0)；新仓名义若误用含当日盯市的 NAV 亦不同。"""
    dates = ["2024-07-01", "2024-07-02", "2024-07-03", "2024-07-04"]
    old = make_bars(dates, [10.0, 10.6, 9.0, 9.0], [10.5, 10.4, 9.5, 9.5])
    new = make_bars(dates, [20.0, 19.5, 20.0, 20.0], [20.5, 19.5, 22.0, 22.0])
    instrs = [Instruction("2024-07-01", "OLD", "buy", "open"),
              Instruction("2024-07-03", "OLD", "sell", "open"),
              Instruction("2024-07-03", "NEW", "buy", "open")]
    res = run_portfolio_backtest({"OLD": old, "NEW": new}, dates, instrs)
    nav = res["nav"]
    n0 = 1.0 + 0.2 * (10.5 / 10.0 - 1.0) - 0.2 * 0.0015
    n1 = (1.0 - 0.2 * 1.0015) + (0.2 / 10.0) * 10.4   # 07-02 仅旧仓盯市
    assert abs(nav["2024-07-01"] - n0) < 1e-12
    assert abs(nav["2024-07-02"] - n1) < 1e-12
    # 调仓日（07-03）手算：旧仓隔夜 (9.0−10.4)−卖出成本；新仓 (22/20−1)×名义−买入成本
    notional_new = 0.2 * n1                            # NAV_ref = 上一结算日 NAV
    expect_n2 = n1 \
        + (0.02 * (9.0 - 10.4) - 0.02 * 9.0 * 0.0015) \
        + (notional_new * (22.0 / 20.0 - 1.0) - notional_new * 0.0015)
    assert abs(nav["2024-07-03"] - expect_n2) < 1e-12
    t_sell = [t for t in res["trades"] if t["side"] == "sell"][0]
    t_buy = [t for t in res["trades"] if t["side"] == "buy" and t["code"] == "NEW"][0]
    assert t_sell["exec_date"] == "2024-07-03" and t_sell["px"] == 9.0
    assert t_buy["exec_date"] == "2024-07-03" and t_buy["px"] == 20.0
    assert abs(t_buy["gross"] - notional_new) < 1e-12
    assert nav["2024-07-04"] == nav["2024-07-03"]      # 次日无交易：NEW 收盘持平


# ---------------- 5. 成本精确性 / 无操作卖出 / 指令校验 ----------------

@test
def test_costs_idempotence_and_instruction_validation():
    dates = CAL5[:3]
    bars_a = make_bars(dates, [10.0, 11.0, 12.0], [10.5, 11.5, 12.5])
    # 同日重复事件卖出：首笔成交、次笔无操作，现金与单笔完全一致
    r_double = run_portfolio_backtest(
        {"A": bars_a}, dates,
        [Instruction("2024-07-01", "A", "buy", "open"),
         Instruction("2024-07-02", "A", "sell", "open"),
         Instruction("2024-07-02", "A", "sell", "open")])
    r_single = run_portfolio_backtest(
        {"A": bars_a}, dates,
        [Instruction("2024-07-01", "A", "buy", "open"),
         Instruction("2024-07-02", "A", "sell", "open")])
    assert len([t for t in r_double["trades"] if t["side"] == "sell"]) == 1
    assert len(r_double["noops"]) == 1
    assert r_double["nav"]["2024-07-02"] == r_single["nav"]["2024-07-02"]
    # target sell 对从未持有的票：变动为零 → 不成交、不计费、现金不动
    r_t = run_portfolio_backtest(
        {"A": bars_a}, dates,
        [Instruction("2024-07-01", "A", "sell", "open", target=True)])
    assert r_t["trades"] == []
    assert r_t["cash"]["2024-07-01"] == 1.0
    assert r_t["nav"]["2024-07-01"] == 1.0
    # 数据末尾之后无 bar 的指令 → skipped（不静默丢弃）
    r_s = run_portfolio_backtest(
        {"A": bars_a}, dates,
        [Instruction("2024-07-03", "A", "sell", "open"),
         Instruction("2024-07-05", "A", "buy", "open")])
    assert len(r_s["skipped"]) == 1
    assert r_s["skipped"][0]["reason"] == "no_bar_on_or_after"
    # 非法指令从严报错
    for bad in [Instruction("2024-07-01", "A", "hold", "open"),
                Instruction("2024-07-01", "A", "buy", "vwap"),
                Instruction("2024-07-01", "ZZZZ", "buy", "open")]:
        try:
            run_portfolio_backtest({"A": bars_a}, dates, [bad])
            raise AssertionError("expected ValueError: %s" % (bad,))
        except ValueError:
            pass


# ---------------- 6. 持仓上限 5 只 assert + 现金守恒（现金+市值=NAV 逐日核对） ----------------

@test
def test_position_cap_assert_and_cash_conservation():
    dates = CAL5[:3]
    codes = ["S1", "S2", "S3", "S4", "S5", "S6"]
    flat = {c: make_bars(dates, [10.0] * 3, [10.0] * 3) for c in codes}
    buys6 = [Instruction("2024-07-01", c, "buy", "open") for c in codes]
    try:
        run_portfolio_backtest(flat, dates, buys6)
        raise AssertionError("expected AssertionError: 持仓上限 5 只")
    except AssertionError:
        pass
    res5 = run_portfolio_backtest(flat, dates, buys6[:5])
    assert res5["n_positions_max"] == 5 <= MAX_POSITIONS
    # 5×20% 全投 + 成本 → 负现金为冻结权重的必然（引擎消歧 b，如实入账）
    expect_nav = 5 * 0.2 * (10.0 / 10.0) + (1.0 - 5 * 0.2 * 1.0015)
    assert abs(res5["nav"].iloc[-1] - expect_nav) < 1e-12

    # 守恒场景：3 票 + 停牌 + target 调仓 + sell@close，独立记账复算逐日 NAV
    dates6 = ["2024-07-01", "2024-07-02", "2024-07-03",
              "2024-07-04", "2024-07-05", "2024-07-08"]
    bars = {
        "A": make_bars(["2024-07-01", "2024-07-02", "2024-07-03",
                        "2024-07-05", "2024-07-08"],        # 07-04 停牌
                       [10.0, 11.0, 12.0, 13.0, 14.0],
                       [10.5, 11.2, 11.8, 12.9, 13.6]),
        "B": make_bars(dates6, [20.0, 20.5, 21.0, 20.0, 19.5, 20.2],
                       [20.4, 20.8, 20.6, 19.6, 20.0, 20.9]),
        "C": make_bars(dates6, [5.0, 5.1, 5.2, 5.3, 5.4, 5.5],
                       [5.05, 5.15, 5.25, 5.35, 5.45, 5.55]),
    }
    instrs = [Instruction("2024-07-01", "A", "buy", "open"),
              Instruction("2024-07-01", "B", "buy", "open"),
              Instruction("2024-07-01", "C", "buy", "open"),
              Instruction("2024-07-04", "B", "buy", "open", target=True),
              Instruction("2024-07-05", "C", "sell", "close")]
    res = run_portfolio_backtest(bars, dates6, instrs)
    # 引擎自身恒等式：现金 + 市值 = NAV 逐日
    for d in dates6:
        assert abs(res["cash"][d] + res["mv"][d] - res["nav"][d]) < 1e-9
    # 独立复算：由成交日志重放现金与股数（不依赖引擎内部状态）
    cashx, sharesx, lastc = 1.0, {}, {}
    tlog = sorted(res["trades"], key=lambda t: (t["exec_date"], t["code"]))
    ti = 0
    for d in dates6:
        while ti < len(tlog) and tlog[ti]["exec_date"] == d:
            t = tlog[ti]
            if t["side"] == "buy":
                cashx -= t["gross"] + t["cost"]
                sharesx[t["code"]] = sharesx.get(t["code"], 0.0) + t["shares"]
            else:
                cashx += t["gross"] - t["cost"]
                sharesx[t["code"]] = sharesx.get(t["code"], 0.0) - t["shares"]
                assert sharesx[t["code"]] > -1e-12
            ti += 1
        for c in sharesx:
            df = bars[c]
            hit = df.loc[df["trade_date"] == d, "close_qfq"]
            if len(hit):
                lastc[c] = float(hit.iloc[0])
        navx = cashx + sum(sharesx[c] * lastc[c] for c in sharesx)
        assert abs(navx - res["nav"][d]) < 1e-9
        assert abs(cashx - res["cash"][d]) < 1e-9
    # 日收益序列与 NAV 逐日自洽
    nav = res["nav"]
    prev = nav.shift(1).fillna(1.0)
    assert np.allclose(res["returns"].values, (nav / prev - 1.0).values, atol=1e-12)


# ---------------- 7. 基线腿选票（§3.6-10）：Top5/历史不足剔除/现金兜底/次月首日执行 ----------------

@test
def test_baseline_leg_picks_and_execution():
    cal = [d.strftime("%Y-%m-%d")
           for d in pd.bdate_range("2024-01-02", "2024-03-05")]
    n = len(cal)                                   # 1月 22 根(idx0..21)、2月 21 根(22..42)、3月 3 根(43..45)

    def seg(c_jan, s_jan, c_feb0, s_feb, i):
        if i <= 21:
            return c_jan + s_jan * i
        return c_feb0 + s_feb * (i - 22)

    closes = {}
    for c, (cj, sj, cf, sf) in {
            "A": (10.0, 0.10, 12.1, -0.30),   # 1月最陡 +，2月崩塌
            "B": (10.0, 0.05, 11.05, 0.05),
            "C": (10.0, 0.00, 10.0, 0.00),
            "D": (10.0, -0.05, 8.95, 0.08),   # 2月反转向上
    }.items():
        closes[c] = [seg(cj, sj, cf, sf, i) for i in range(n)]
    closes["E"] = [10.0 + 0.5 * i for i in range(n - 14)]   # 01-22 上市：1月末仅 8 根
    closes["F"] = [10.0 + 0.4 * i for i in range(20)]       # 1月末恰 20 根（<21 → 剔除）
    closes["G"] = [10.0 + 0.05 * i for i in range(n - 22)]  # 02-01 起：2月末恰 21 根
    spans = {"A": (0, n), "B": (0, n), "C": (0, n), "D": (0, n),
             "E": (14, n), "F": (2, 22), "G": (22, n)}
    bars = {}
    for c, (a, b) in spans.items():
        ks = [k for k in range(a, b) if not (c == "D" and k == 22)]  # D 02-01 停牌
        cl = [closes[c][k - a] for k in ks]
        ops = [cl[0]] + cl[:-1]                    # open = 前收
        bars[c] = make_bars([cal[k] for k in ks], ops, cl)

    res = momentum_baseline_leg(bars, cal, test_start="2024-01-02")
    # 信号月无调仓（2024-01 无上月）→ 全 1 月 NAV 恒为 1.0
    assert all(res["nav"][d] == 1.0 for d in cal[:22])
    # 动量 Top 独立复算（与规则同式、独立实现；len<21 剔除即 off-by-one 探针）
    def expected_rank(prev_end):
        scored = []
        for c, df in bars.items():
            sub = df.loc[df["trade_date"] <= prev_end, "close_qfq"].tolist()
            if len(sub) < 21:
                continue
            scored.append((-(sub[-1] / sub[-21] - 1.0), c))
        scored.sort()
        return [c for _neg, c in scored[:5]]

    assert expected_rank("2024-01-31") == res["picks"]["2024-02"] == ["A", "B", "C", "D"]
    assert expected_rank("2024-02-29") == res["picks"]["2024-03"] == ["E", "D", "G", "B", "C"]
    # 20 日历史不足剔除（E 虽动量巨大、F 恰 20 根，均不得入选 2024-02）
    assert "E" not in res["picks"]["2024-02"] and "F" not in res["picks"]["2024-02"]
    assert "A" not in res["picks"]["2024-03"]       # 2月崩塌掉出

    tr = res["trades"]
    feb1 = [t for t in tr if t["instr_date"] == "2024-02-01" and t["side"] == "buy"]
    assert {t["code"] for t in feb1} == {"A", "B", "C", "D"}
    feb1_by_code = {t["code"]: t for t in feb1}
    exec_dates = {t["code"]: t["exec_date"] for t in feb1}
    # 次月首交易日执行（2024-02-01）；D 停牌顺延至 02-02（§3.6-8 顺延语义）
    assert exec_dates["A"] == "2024-02-01" and exec_dates["D"] == "2024-02-02"
    for t in feb1:
        assert t["target"] is True and t["price_type"] == "open"
    for t in feb1:
        if t["exec_date"] == "2024-02-01":
            assert abs(t["gross"] - 0.2) < 1e-12    # 首执行日 NAV=1 → 每笔 20%
    # D 顺延至 02-02 成交：名义 = 20% × 上一结算日（02-01 收盘）NAV（引擎消歧 a）
    assert abs(feb1_by_code["D"]["gross"] - 0.2 * res["nav"]["2024-02-01"]) < 1e-12
    # 不足 5 只 → 2 月仅 4×20% + 现金兜底；3 月持满 5 只不越限
    assert res["n_positions_max"] == 5 <= MAX_POSITIONS
    # 02-01 三票以 open（=前收）成交、收盘持平 → NAV 精确可算；
    # D 顺延至 02-02 以 20%×NAV(02-01) 定尺买入（引擎消歧 a），现金兜底逐位核对
    cash_0201 = 1.0 - 3 * 0.2 * 1.0015
    nav_0201 = cash_0201 + 3 * 0.2
    assert abs(res["nav"]["2024-02-01"] - nav_0201) < 1e-12
    assert abs(res["cash"]["2024-02-02"]
               - (cash_0201 - 0.2 * nav_0201 * 1.0015)) < 1e-12

    mar1 = {t["code"]: t for t in tr if t["instr_date"] == "2024-03-01"}
    assert set(mar1) == {"A", "B", "C", "D", "E", "G"}
    assert mar1["A"]["side"] == "sell"              # 掉出 → 全退
    assert mar1["E"]["side"] == "buy" and mar1["G"]["side"] == "buy"
    nav_feb_end = res["nav"]["2024-02-29"]
    # 新入票名义 = 20% × 上一结算日 NAV（引擎消歧 a）
    assert abs(mar1["E"]["gross"] - 0.2 * nav_feb_end) < 1e-9
    # 留存票只交易变动部分（小修小补，非整仓 20%；"已持有票不重复计费"）
    for c in ("B", "C", "D"):
        assert mar1[c]["side"] in ("buy", "sell")
        assert 0.0 < mar1[c]["gross"] < 0.05 * nav_feb_end
    for t in tr:
        assert abs(t["cost"] - 0.0015 * t["gross"]) < 1e-15
    assert res["switches"] == 2                     # 2024-02 / 2024-03 两个月有成交


# ---------------- 8. 换仓重叠顺延（引擎消歧 g）：掉出票停牌占槽 → 新入票顺延重试 ----------------

@test
def test_target_buy_defers_when_slot_blocked_by_suspended_exit():
    """掉出票停牌→卖出顺延、槽位未释放：新入票 target 买入遇 5 只上限顺延至下一 bar
    重试，复牌日先卖后买、持仓从未越限；事件买入越限仍 assert（见 cap 用例）。"""
    cal = [d.strftime("%Y-%m-%d")
           for d in pd.bdate_range("2024-01-02", "2024-03-08")]
    n = len(cal)

    def cl_two_phase(c_jan, s_jan, s_feb, i):
        return c_jan + s_jan * i if i <= 21 else c_jan + s_jan * 21 + s_feb * (i - 22)

    spec = {"H1": (10.0, 0.02, 0.02), "H2": (10.0, 0.03, 0.03),
            "H3": (10.0, 0.04, 0.04), "H4": (10.0, 0.05, 0.05),
            "X": (10.0, 0.10, -0.30), "N": (10.0, -0.01, 0.20)}
    bars = {}
    for c, (cj, sj, sf) in spec.items():
        cl = [cl_two_phase(cj, sj, sf, i) for i in range(n)]
        if c == "X":
            ks = [i for i in range(n) if not (43 <= i <= 45)]   # 03-01~03-05 停牌
            ds = [cal[i] for i in ks]
            cl = [cl[i] for i in ks]
        else:
            ds = cal
        ops = [cl[0]] + cl[:-1]
        bars[c] = make_bars(ds, ops, cl)

    res = momentum_baseline_leg(bars, cal, test_start="2024-01-02")
    assert res["picks"]["2024-02"] == ["X", "H4", "H3", "H2", "H1"]
    assert res["picks"]["2024-03"] == ["N", "H4", "H3", "H2", "H1"]   # X 2月崩塌掉出
    assert res["n_positions_max"] == 5 <= MAX_POSITIONS
    assert res["skipped"] == []
    tr = res["trades"]
    x_sell = [t for t in tr if t["code"] == "X" and t["side"] == "sell"]
    n_buy = [t for t in tr if t["code"] == "N" and t["side"] == "buy"]
    assert len(x_sell) == 1 and x_sell[0]["exec_date"] == "2024-03-06"  # 卖出顺延至复牌
    assert len(n_buy) == 1 and n_buy[0]["exec_date"] == "2024-03-06"    # 买入顺延至槽位释放
    assert [(x["date"], x["until"]) for x in res["cap_defers"]] == [
        ("2024-03-01", "2024-03-04"), ("2024-03-04", "2024-03-05"),
        ("2024-03-05", "2024-03-06")]


# ---------------- 9. 顺延买入的时点感知取消：较新指令已到决定时点才杀，未来指令不杀 ----------------

@test
def test_deferred_buy_killed_only_when_later_wish_arrives():
    """顺延中的 target 买入：同票更晚指令日期 <= 重试日（较新意愿已生效）→ 取消；
    更晚指令仍在未来 → 先到先得、继续顺延。用引擎直做（手工指令，不依赖基线生成）。"""
    cal = [d.strftime("%Y-%m-%d")
           for d in pd.bdate_range("2024-07-01", "2024-08-09")]   # 30 个交易日
    flat = list(range(30))
    bars = {}
    for c in ["SLOT", "K1", "K2", "K3", "K4"]:
        ks, cl = flat, [10.0] * 30
        if c == "SLOT":                     # 07-09~07-30 停牌（长停占槽）
            ks = [i for i in flat if not (6 <= i <= 21)]
            cl = [10.0 + 0.01 * i for i in ks]
        else:
            cl = [10.0 + 0.01 * i for i in ks]
        ds = [cal[i] for i in ks]
        ops = [cl[0]] + cl[:-1]
        bars[c] = make_bars(ds, ops, cl)
    ncl = [10.0 + 0.02 * i for i in flat]   # N 全程有 bar
    bars["N"] = make_bars(cal, [ncl[0]] + ncl[:-1], ncl)

    instrs = [
        Instruction("2024-07-01", "SLOT", "buy", "open", target=True),
        Instruction("2024-07-01", "K1", "buy", "open", target=True),
        Instruction("2024-07-01", "K2", "buy", "open", target=True),
        Instruction("2024-07-01", "K3", "buy", "open", target=True),
        Instruction("2024-07-01", "K4", "buy", "open", target=True),
        Instruction("2024-07-09", "SLOT", "sell", "open", target=True),  # 顺延至 07-31
        Instruction("2024-07-10", "N", "buy", "open", target=True),      # 顶格 → 顺延
        Instruction("2024-07-16", "N", "sell", "open", target=True),     # 较新意愿
    ]
    res = run_portfolio_backtest(bars, cal, instrs)
    assert res["n_positions_max"] == 5 <= MAX_POSITIONS
    # N 买入顺延链：07-10→11→12→15→16；07-16 较新卖出意愿已生效 → 取消
    assert [(x["date"], x["until"]) for x in res["cap_defers"]] == [
        ("2024-07-10", "2024-07-11"), ("2024-07-11", "2024-07-12"),
        ("2024-07-12", "2024-07-15"), ("2024-07-15", "2024-07-16")]
    assert not any(t["code"] == "N" and t["side"] == "buy" for t in res["trades"])
    assert any(s["code"] == "N" and s["reason"] == "superseded_by_later_target"
               for s in res["skipped"])
    # SLOT 卖出顺延至复牌日成交；N 的卖出因无持仓为无操作
    slot_sell = [t for t in res["trades"] if t["code"] == "SLOT" and t["side"] == "sell"]
    assert len(slot_sell) == 1 and slot_sell[0]["exec_date"] == "2024-07-31"
    assert any(x["code"] == "N" and x["reason"] == "no_position"
               for x in res["noops"])


def main() -> int:
    import traceback
    failed = 0
    for fn in _TESTS:
        try:
            fn()
            print("PASS %s" % fn.__name__)
        except Exception:  # noqa: BLE001
            failed += 1
            print("FAIL %s" % fn.__name__)
            traceback.print_exc()
    print("%d/%d tests passed" % (len(_TESTS) - failed, len(_TESTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
