"""缠论 R3 批次 2b —— 因果引擎 + 重画率 + 组合层 + gate 判定单测（纯合成数据，绝不连 DB）。

施工方案：docs/缠论R3施工方案-2026-09-19.md §3.4（Gate 2 重画率定义）+ §3.6-3/7/8/14。
纪律：本文件零 DB 访问（连只读也不允许）；全部用手工构造/复用 chan_fixtures 的合成
序列驱动 signals/chan_causal.py 与 chan_research 的 gate 判定函数。

用例清单：
- 非重画合成序列：因果流信号 == 终态结构信号（前缀重算正确性，重画率 0）；
- 重画两机制：末根合并K被后续包含延长致确认日后移 / 结构被后续行情改写致信号消失；
- 混合重画率算对（2 信号 1 重画 → 50%）；
- 去重：<5 交易日间隔只取第一次（锚点=最近保留）、恰 5 间隔保留、多枢同触发单发射、
  延长再发被去重合并；
- 组合层：5 只上限超额丢弃、持仓中同票忽略、同日槽位先释放后入场、出场指令日期与
  价格类型（sell@open / sell@close、同日顶分型优先、不可交易 None）；
- 组合层 → run_portfolio_backtest 集成（不触发上限 assert、卖出价格类型正确）；
- clean 判定：60 交易日窗含/不含畸变日（窗界两侧）、形成窗复牌日（起点当日不算）、
  warmup；
- gate 判定函数注入假数字直接单测（含短路语义）与月桶统计。
"""
import os
import sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

os.environ["AGSICKLE_DISABLE_LIVE_QUOTES"] = "1"  # 测试保持离线

from signals.chan_backtest import Instruction, run_portfolio_backtest  # noqa: E402
from signals.chan_causal import (  # noqa: E402
    build_instructions, causal_pipeline_code, clean_check, dedup_signals,
    evaluate_gates, judge_gate1, judge_gate2, judge_gate3, month_bucket_stats,
    plan_signal_trade, repaint_report, run_causal_scan, select_portfolio)
from signals.chan_data import with_open_qfq  # noqa: E402
from tests.chan_fixtures import THIRDBUY_CASES, bars_to_ohlc, make_dates  # noqa: E402

_TESTS = []


def test(fn):
    _TESTS.append(fn)
    return fn


test.__test__ = False  # pytest 不要把装饰器本身当测试收集

SUCCESS_BARS = THIRDBUY_CASES[0]["bars_hl"]      # 36 根，三买确认 bar=35（fixture 手推）


def _scan(bars_hl, code="X"):
    """合成 (h,l) 序列 → causal_pipeline_code（无 warmup/测试窗限制）。"""
    n = len(bars_hl)
    df = bars_to_ohlc(bars_hl)
    cal = make_dates(n)
    return causal_pipeline_code(df, cal, code, warmup_start=None, test_start=None,
                                high_col="high", low_col="low")


# ---------------- 1. 非重画合成序列：因果流 == 终态结构（重画率 0） ----------------

@test
def test_causal_no_repaint_equals_terminal():
    # 确认 bar 35 之后追加两根明确上行 bar（新合并K、不并入 K35、不产生新分型）
    bars = SUCCESS_BARS + [(13.8, 13.4), (13.9, 13.5)]
    res = _scan(bars)
    dates = res["dates"]
    assert len(res["raw"]) == 1, res["raw"]
    assert res["raw"][0]["confirm_date"] == dates[35]
    assert len(res["emitted"]) == 1
    assert len(res["terminal"]) == 1
    assert res["terminal"][0]["confirm_date"] == dates[35]
    assert res["emitted"][0]["zg"] == THIRDBUY_CASES[0]["expect"]["signals"][0]["zg"]
    # 后续 bar 不产生新的顶分型确认事件（无出场噪声）
    assert all(e["date"] <= dates[35] for e in res["top_events"])
    rep = repaint_report(res["emitted"], res["terminal"], {"X": res["pos"]})
    assert rep["repaint"] == 0.0 and rep["n_hit"] == 1
    assert rep["n_shift"] == 0 and rep["n_vanish"] == 0


# ---------------- 2a. 重画机制一：末根合并K被后续包含延长 → 确认日后移 ----------------

@test
def test_repaint_confirm_extension():
    # bar36 (13.5,13.4) ⊆ K35(13.7,13.3) 闭区间 → 并入（向上合并 l=max）→
    # 回抽底分型第 3 根合并K i_end 由 35 延长至 36 → 确认日后移一根
    bars = SUCCESS_BARS + [(13.5, 13.4)]
    res = _scan(bars)
    dates = res["dates"]
    # 原始发射两次（t=35 与 t=36 各确认一次），生成层 <5 交易日去重只保留第一次
    assert [s["confirm_date"] for s in res["raw"]] == [dates[35], dates[36]]
    assert [s["confirm_date"] for s in res["emitted"]] == [dates[35]]
    # 终态结构确认日 = 36 → 实发(35) ∩ 终态(36) = ∅ → 重画率 100%
    assert [s["confirm_date"] for s in res["terminal"]] == [dates[36]]
    rep = repaint_report(res["emitted"], res["terminal"], {"X": res["pos"]})
    assert rep["repaint"] == 1.0 and rep["n_hit"] == 0
    assert rep["n_shift"] == 1 and rep["n_vanish"] == 0   # 近距移位归因


# ---------------- 2b. 重画机制二：结构被后续行情改写 → 信号消失 ----------------

@test
def test_repaint_structure_rewrite():
    # 确认后深跌再反弹：新低底分型 (12.2) 更极端替换回抽底分型 (13.0) →
    # 回抽笔端点 12.2 ≤ ZG=12.5 → 终态无任何三买（fixture 推导见 commit 说明）
    bars = SUCCESS_BARS + [
        (13.5, 13.2), (13.3, 13.0), (13.1, 12.8),      # 36-38 下行
        (12.9, 12.6), (12.7, 12.4), (12.5, 12.2),      # 39-41
        (12.8, 12.5), (13.0, 12.7),                    # 42-43 反弹使 B@41 可检出
    ]
    res = _scan(bars)
    dates = res["dates"]
    assert [s["confirm_date"] for s in res["raw"]] == [dates[35]]
    assert res["terminal"] == []
    rep = repaint_report(res["emitted"], res["terminal"], {"X": res["pos"]})
    assert rep["repaint"] == 1.0
    assert rep["n_shift"] == 0 and rep["n_vanish"] == 1   # 消失归因


# ---------------- 2c. 混合重画率：2 实发 1 重画 → 50% ----------------

@test
def test_repaint_mixed_rate():
    ok_bars = SUCCESS_BARS + [(13.8, 13.4), (13.9, 13.5)]          # 不重画
    rep_bars = SUCCESS_BARS + [(13.5, 13.4)]                        # 延长重画
    n = max(len(ok_bars), len(rep_bars))
    cal = make_dates(n)
    scan = run_causal_scan(
        {"A": bars_to_ohlc(ok_bars), "B": bars_to_ohlc(rep_bars)}, cal,
        test_start=None, warmup_start=None, high_col="high", low_col="low")
    emitted = [s for c in sorted(scan) for s in scan[c]["emitted"]]
    terminal = [s for c in sorted(scan) for s in scan[c]["terminal"]]
    # 终态集含 B 的移位后确认日（B 多一根延长 bar）→ 2 个；与实发交集仅 A → 重画 50%
    assert len(emitted) == 2 and len(terminal) == 2
    assert {s["confirm_date"] for s in terminal} == {make_dates(38)[35],
                                                     make_dates(38)[36]}
    pos = {c: scan[c]["pos"] for c in scan}
    rep = repaint_report(emitted, terminal, pos)
    assert abs(rep["repaint"] - 0.5) < 1e-12
    assert rep["n_hit"] == 1 and rep["n_shift"] == 1 and rep["n_vanish"] == 0


# ---------------- 3. 去重：<5 交易日只取第一次 / 恰 5 保留 / 多枢单发射 ----------------

@test
def test_dedup_gap_and_anchor():
    dates = make_dates(40)
    pos = {d: i for i, d in enumerate(dates)}

    def sig(i):
        return {"code": "X", "confirm_date": dates[i], "trigger_pi": i}

    # 间隔 3 (<5) 丢弃；间隔自最近保留信号计 5 → 保留（锚点=最近保留，钉死 a）
    kept = dedup_signals([sig(10), sig(13), sig(15)], pos)
    assert [s["confirm_date"] for s in kept] == [dates[10], dates[15]]
    # 恰 5 间隔不丢（判据为 <5）
    kept2 = dedup_signals([sig(10), sig(15)], pos)
    assert len(kept2) == 2
    # 乱序输入按确认时间排序后去重
    kept3 = dedup_signals([sig(15), sig(10)], pos)
    assert [s["confirm_date"] for s in kept3] == [dates[10], dates[15]]


@test
def test_multi_pivot_single_emission():
    # fixture third_buy_success 的 raw_flags=[0,1,2]（三枢同触发一个底分型），
    # chanlib 已按触发分型去重 → 因果层该时点只发射一次（不重复计数）
    res = _scan(SUCCESS_BARS + [(13.8, 13.4), (13.9, 13.5)])
    assert len(res["raw"]) == 1
    assert res["raw"][0]["trigger_pi"] == 34


# ---------------- 4. 交易计划：出场日期与价格类型（§3.6-8） ----------------

def _plan(confirm_i, top_idxs, n=40):
    dates = make_dates(n)
    pos = {d: i for i, d in enumerate(dates)}
    sig = {"code": "X", "confirm_date": dates[confirm_i]}
    plan = plan_signal_trade(sig, dates, pos, sorted(dates[i] for i in top_idxs))
    return plan, dates


@test
def test_plan_exit_top_fractal_and_day20():
    # 顶分型先到：确认 idx5 → 入场 idx6；顶分型确认 idx10 → 次日 idx11 sell@open
    plan, dates = _plan(5, [10])
    assert plan["entry_date"] == dates[6]
    assert plan["exit"] == {"date": dates[11], "price_type": "open",
                            "kind": "top_fractal"}
    # 20 日先到：顶分型确认 idx25（其卖出日 = idx26 晚于 20 日出场日）
    # 20 日出场 = 入场 idx6 起第 20 根 = idx25 收盘 → 先到者生效
    plan, dates = _plan(5, [25])
    assert plan["exit"] == {"date": dates[25], "price_type": "close",
                            "kind": "day20"}
    # 同日按顶分型口径：顶分型确认 idx24 → 卖出日 idx25 == 20 日出场日 idx25
    # → 同日 → 顶分型（open）生效（§3.6-8）
    plan, dates = _plan(5, [24])
    assert plan["exit"] == {"date": dates[25], "price_type": "open",
                            "kind": "top_fractal"}
    # 无顶分型事件 → 20 日出场 idx25 sell@close
    plan, dates = _plan(5, [])
    assert plan["exit"] == {"date": dates[25], "price_type": "close",
                            "kind": "day20"}
    # 顶分型确认早于入场执行日 → 忽略，落 20 日
    plan, dates = _plan(5, [3])
    assert plan["exit"]["kind"] == "day20"
    # 确认日为末根 bar → 不可交易
    plan, dates = _plan(39, [])
    assert plan is None
    # 顶分型确认于末根（无次日可卖）→ 顶分型缺省，落 20 日
    plan, dates = _plan(5, [39])
    assert plan["exit"] == {"date": dates[25], "price_type": "close",
                            "kind": "day20"}


# ---------------- 5. 组合层槽位模拟（§3.3） ----------------

def _mk_signals_plans(specs, dates):
    """specs: [(code, confirm_i, entry_i, exit_i|None)] → (signals, plans)"""
    signals, plans = [], {}
    for code, ci, ei, xi in specs:
        s = {"code": code, "confirm_date": dates[ci]}
        signals.append(s)
        plan = {"entry_date": dates[ei], "exit": None, "exit_date": None}
        if xi is not None:
            plan = {"entry_date": dates[ei],
                    "exit": {"date": dates[xi], "price_type": "close",
                             "kind": "day20"},
                    "exit_date": dates[xi]}
        plans[(code, dates[ci])] = plan
    return signals, plans


@test
def test_select_portfolio_cap_and_hold_ignore():
    dates = make_dates(40)
    cal_pos = {d: i for i, d in enumerate(dates)}
    # 6 只同日入场、20 日后退出 → 第 6 只超额丢弃（先到先得 = (confirm, code) 序）
    specs = [(c, 5, 6, 26) for c in ["F", "A", "C", "B", "E", "D"]]
    signals, plans = _mk_signals_plans(specs, dates)
    sel = select_portfolio(signals, plans, cal_pos, 39)
    assert {a["code"] for a in sel["accepted"]} == {"A", "B", "C", "D", "E"}
    assert sel["n_cap_dropped"] == 1
    assert sel["n_untradable"] == 0 and sel["n_held_ignored"] == 0
    # 持仓中同票忽略：A 第二信号入场日 idx9 落在 A 已接受持仓 [6,26) 内
    specs2 = [("A", 5, 6, 26), ("A", 8, 9, 30)]
    signals2, plans2 = _mk_signals_plans(specs2, dates)
    sel2 = select_portfolio(signals2, plans2, cal_pos, 39)
    assert [a["confirm_date"] for a in sel2["accepted"]] == [dates[5]]
    assert sel2["n_held_ignored"] == 1 and sel2["n_cap_dropped"] == 0


@test
def test_select_portfolio_same_day_release():
    dates = make_dates(40)
    cal_pos = {d: i for i, d in enumerate(dates)}
    # 4 只占位 [6,30)；X [6,10)；Y 入场恰为 X 出场日 idx10 → 同日先释放后入场
    specs = [("F1", 5, 6, 30), ("F2", 5, 6, 30), ("F3", 5, 6, 30),
             ("F4", 5, 6, 30), ("X", 5, 6, 10), ("Y", 9, 10, 20)]
    signals, plans = _mk_signals_plans(specs, dates)
    sel = select_portfolio(signals, plans, cal_pos, 39)
    assert {a["code"] for a in sel["accepted"]} == {"F1", "F2", "F3", "F4", "X", "Y"}
    assert sel["n_cap_dropped"] == 0
    # 反例：Y 入场提前一根（idx9，X 尚未释放）→ 6 只并发 → 丢弃
    specs2 = specs[:-1] + [("Y", 8, 9, 20)]
    signals2, plans2 = _mk_signals_plans(specs2, dates)
    sel2 = select_portfolio(signals2, plans2, cal_pos, 39)
    assert {a["code"] for a in sel2["accepted"]} == {"F1", "F2", "F3", "F4", "X"}
    assert sel2["n_cap_dropped"] == 1


# ---------------- 6. 指令构造 + 引擎集成（不触发 5 只 assert） ----------------

@test
def test_instructions_and_engine_integration():
    dates = make_dates(40)
    cal_pos = {d: i for i, d in enumerate(dates)}
    specs = [(c, 5, 6, 26) for c in ["A", "B", "C", "D", "E"]]
    specs += [("F", 5, 6, 26)]                     # F 应被上限丢弃
    signals, plans = _mk_signals_plans(specs, dates)
    sel = select_portfolio(signals, plans, cal_pos, 39)
    instrs = build_instructions(sel["accepted"])
    assert len(instrs) == 10                       # 5 买 + 5 卖
    buys = [x for x in instrs if x.side == "buy"]
    sells = [x for x in instrs if x.side == "sell"]
    assert all(x.date == dates[6] and x.price_type == "open" for x in buys)
    assert all(x.date == dates[26] and x.price_type == "close" for x in sells)
    assert all(x.target is False for x in instrs)
    # 引擎集成：合成 bar 全程平价，事件指令全数成交、上限不越、卖出按 close 结算
    n = len(dates)
    bars = {}
    for c in ["A", "B", "C", "D", "E", "F"]:
        df = pd.DataFrame({
            "trade_date": list(dates),
            "open": [10.0] * n, "high": [10.1] * n, "low": [9.9] * n,
            "close": [10.0] * n, "close_qfq": [10.0] * n,
            "high_qfq": [10.1] * n, "low_qfq": [9.9] * n})
        bars[c] = with_open_qfq(df)
    res = run_portfolio_backtest(bars, dates, instrs, test_start=dates[0])
    assert res["n_positions_max"] == 5
    sell_trades = [t for t in res["trades"] if t["side"] == "sell"]
    assert len(sell_trades) == 5
    assert all(t["px"] == 10.0 and t["exec_date"] == dates[26]
               for t in sell_trades)
    assert len(res["skipped"]) == 0 and len(res["noops"]) == 0


# ---------------- 7. 因果管线 → 组合层 → 引擎全链（合成三买信号真实成交） ----------------

@test
def test_end_to_end_causal_to_engine():
    bars_hl = SUCCESS_BARS + [(13.8, 13.4), (13.9, 13.5)]   # 38 根，确认 idx35
    # 再追加 20 根严格上行 bar（每根新高扩张 → 新合并K、不产生新分型、
    # 不并入 K35 → 结构不变、确认日不动），使 20 日出场 bar（idx55）存在
    bars_hl = bars_hl + [(13.8 + 0.1 * k, 13.4 + 0.1 * k) for k in range(2, 22)]
    df = bars_to_ohlc(bars_hl)
    dates = make_dates(len(bars_hl))
    res = causal_pipeline_code(df, dates, "A", warmup_start=None,
                               test_start=None, high_col="high", low_col="low")
    assert len(res["emitted"]) == 1                          # 追加 bar 不新增信号
    sig = res["emitted"][0]
    assert sig["confirm_date"] == dates[35]                  # 结构不被追加改写
    plan = plan_signal_trade(sig, res["dates"], res["pos"], res["top_event_dates"])
    assert plan["entry_date"] == dates[36]
    assert plan["exit"] == {"date": dates[36 + 19], "price_type": "close",
                            "kind": "day20"}                    # 入场第 20 根
    cal_pos = {d: i for i, d in enumerate(dates)}
    sel = select_portfolio([sig], {("A", sig["confirm_date"]): plan},
                           cal_pos, len(dates) - 1)
    assert len(sel["accepted"]) == 1
    instrs = build_instructions(sel["accepted"])
    # 引擎可执行的合成 bar（qfq 列同源）
    n = len(dates)
    o = [float(x) for x in df["open"]]
    c = [float(x) for x in df["close"]]
    full = pd.DataFrame({
        "trade_date": list(dates), "open": o,
        "high": [float(x) for x in df["high"]],
        "low": [float(x) for x in df["low"]], "close": c,
        "close_qfq": c, "high_qfq": [float(x) for x in df["high"]],
        "low_qfq": [float(x) for x in df["low"]]})
    res_bt = run_portfolio_backtest({"A": with_open_qfq(full)}, dates, instrs,
                                    test_start=dates[0])
    tbuy = [t for t in res_bt["trades"] if t["side"] == "buy"]
    tsell = [t for t in res_bt["trades"] if t["side"] == "sell"]
    assert len(tbuy) == 1 and tbuy[0]["exec_date"] == dates[36]
    assert len(tsell) == 1 and tsell[0]["exec_date"] == dates[55]
    assert tsell[0]["price_type"] == "close"


# ---------------- 8. clean 判定（§3.6-3 / §3.6-14c） ----------------

@test
def test_clean_check_windows():
    cal = make_dates(130)
    cal_pos = {d: i for i, d in enumerate(cal)}

    def sig(formation_i=50, confirm_i=100):
        return {"code": "X", "confirm_date": cal[confirm_i],
                "formation_start_date": cal[formation_i]}

    # 无畸变、无复牌 → clean
    ok, why = clean_check(sig(), warmup_start=None, distortion_set=set(),
                          breaks=[], calendar=cal, cal_pos=cal_pos)
    assert ok and why == ""
    # 畸变日在窗内（idx80 ∈ [41,100]）→ 不 clean；恰在窗外一侧（idx40 < 41）→ clean
    ok, why = clean_check(sig(), warmup_start=None,
                          distortion_set={cal[80]}, breaks=[],
                          calendar=cal, cal_pos=cal_pos)
    assert not ok and why == "distortion"
    ok, _ = clean_check(sig(), warmup_start=None, distortion_set={cal[40]},
                        breaks=[], calendar=cal, cal_pos=cal_pos)
    assert ok
    # 畸变日恰为确认日（窗含确认日，从严纳入）→ 不 clean
    ok, why = clean_check(sig(), warmup_start=None,
                          distortion_set={cal[100]}, breaks=[],
                          calendar=cal, cal_pos=cal_pos)
    assert not ok and why == "distortion"
    # 形成窗内复牌日（50 < 60 ≤ 100）→ 不 clean；复牌恰为形成窗起点（=50）→ clean
    ok, why = clean_check(sig(), warmup_start=None, distortion_set=set(),
                          breaks=[("2020-01-01", cal[60], 3)],
                          calendar=cal, cal_pos=cal_pos)
    assert not ok and why == "resume"
    ok, _ = clean_check(sig(), warmup_start=None, distortion_set=set(),
                        breaks=[("2020-01-01", cal[50], 3)],
                        calendar=cal, cal_pos=cal_pos)
    assert ok
    # 复牌日恰为确认日（≤ 确认日）→ 不 clean；断链在形成窗起点之前 → clean
    ok, why = clean_check(sig(), warmup_start=None, distortion_set=set(),
                          breaks=[("2020-01-01", cal[100], 3)],
                          calendar=cal, cal_pos=cal_pos)
    assert not ok and why == "resume"
    ok, _ = clean_check(sig(), warmup_start=None, distortion_set=set(),
                        breaks=[("2020-01-01", cal[49], 3)],
                        calendar=cal, cal_pos=cal_pos)
    assert ok
    # warmup：确认日 = 允许起始日 → clean；早一根 → 不 clean
    ok, why = clean_check(sig(), warmup_start=cal[100], distortion_set=set(),
                          breaks=[], calendar=cal, cal_pos=cal_pos)
    assert ok
    ok, why = clean_check(sig(), warmup_start=cal[101], distortion_set=set(),
                          breaks=[], calendar=cal, cal_pos=cal_pos)
    assert not ok and why == "warmup"


# ---------------- 9. gate 判定函数（注入假数字，含短路语义） ----------------

@test
def test_gate_judges_injected_numbers():
    # Gate 1 边界：恰 80/3 过；79 或 2.5 不过
    assert judge_gate1(80, 3) is True
    assert judge_gate1(79, 3) is False
    assert judge_gate1(80, 2.5) is False
    # Gate 2 边界：恰 15% 过（≤）；15.1% 不过
    assert judge_gate2(0.15) is True
    assert judge_gate2(0.151) is False
    # Gate 3 三项边界
    d = judge_gate3(0.05, {"2025H1": 0.01, "2025H2": -0.01, "2026": 0.02}, 0.02)
    assert d["pass"] is True and d["n_seg_pos"] == 2
    assert judge_gate3(0.0499, {"a": 0.1, "b": 0.1, "c": 0.1}, 0.0)["c1"] is False
    assert judge_gate3(0.10, {"a": 0.1, "b": -0.1, "c": -0.1}, 0.0)["c2"] is False
    assert judge_gate3(0.10, {"a": 0.1, "b": 0.1, "c": 0.1}, 0.0201)["c3"] is False
    # 短路：Gate 1 fail → 2/3 未跑（None）；Gate 2 fail → 3 未跑
    r = evaluate_gates(clean_count=79, month_median=3, repaint=0.5,
                       ann_excess=0.1, seg_excesses={"a": 1, "b": 1, "c": 1},
                       mdd_diff=0.0)
    assert r == {"gate1": False, "gate2": None, "gate3": None,
                 "gate3_detail": None, "verdict": "FAIL"}
    r2 = evaluate_gates(clean_count=100, month_median=4, repaint=0.16,
                        ann_excess=0.1, seg_excesses={"a": 1, "b": 1, "c": 1},
                        mdd_diff=0.0)
    assert r2["gate1"] is True and r2["gate2"] is False and r2["gate3"] is None
    assert r2["verdict"] == "FAIL"
    # 全过 → PASS
    r3 = evaluate_gates(clean_count=100, month_median=4, repaint=0.05,
                        ann_excess=0.08,
                        seg_excesses={"2025H1": 0.01, "2025H2": 0.02,
                                      "2026": -0.01},
                        mdd_diff=0.01)
    assert r3["verdict"] == "PASS" and r3["gate3"] is True
    # Gate 3 不过 → FAIL
    r4 = evaluate_gates(clean_count=100, month_median=4, repaint=0.05,
                        ann_excess=0.01, seg_excesses={"a": 1, "b": 1, "c": 1},
                        mdd_diff=0.0)
    assert r4["verdict"] == "FAIL" and r4["gate3"] is False


# ---------------- 10. 月桶统计（含零月 gate 口径 / 仅覆盖月对照） ----------------

@test
def test_month_bucket_stats():
    months = ["2024-07", "2024-08", "2024-09"]
    signals = [{"code": "X", "confirm_date": "2024-07-02"},
               {"code": "X", "confirm_date": "2024-07-15"},
               {"code": "Y", "confirm_date": "2024-09-03"},
               {"code": "Y", "confirm_date": "2024-09-04"},
               {"code": "Z", "confirm_date": "2024-09-05"},
               {"code": "Z", "confirm_date": "2024-09-18"},
               {"code": "Z", "confirm_date": "2024-09-19"},
               {"code": "Z", "confirm_date": "2024-09-20"}]
    mb = month_bucket_stats(signals, months)
    assert mb["counts"] == {"2024-07": 2, "2024-08": 0, "2024-09": 6}
    assert mb["median_all"] == 2.0          # 含零月（gate 口径）
    assert mb["median_covered"] == 4.0      # 仅覆盖月（对照）
    assert mb["n_months"] == 3 and mb["n_covered"] == 2


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
