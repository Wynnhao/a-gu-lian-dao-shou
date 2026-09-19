"""缠论 R3 批次 2b —— bar-by-bar 因果信号引擎 + 重画率度量（Gate 2）+ 信号腿组合层。

== 因果实现语义（前缀重算，最简且可证明无前视） ==
逐票、逐断链段（chanlib.split_segments，§3.6-14d 不跨链连笔）；对段内每根 bar t 的
收盘时点，用 chanlib 对 bars[0..t] 重算 包含→分型→笔→中枢→三买 全管线，收集
"确认 bar == t" 的三买信号（确认日 = 回抽笔终点底分型第 3 根合并K的 i_end 原始
bar 交易日，§3.6-14a）与前缀时点确认的顶分型（出场事件，与入场同一次前缀扫描记录）。
前缀重算复杂度 O(N²)（每票 ~659 bar、core 51 总量分钟级；实测超 10 分钟才允许做
"只在分型边界重算"的等价优化并写明论证，本批未触发）。末根合并K可被后续 bar 包含
并入而延长 i_end → 确认日因此后移：同一形态会在 t 与 t+k 各发一次原始事件，而终态
结构只含最终确认日——这正是重画率要度量的现象，如实计入（生成层 <5 交易日去重只
保留第一次，实发与终态之差即重画）。

== Gate 2 定义（施工方案 §3.4 原文引用） ==
"重画率 ≤15%。重画率 = 1 −（因果引擎实发信号中，最终结构下同票同确认日仍构成
三买的比例）。"
实现口径：分母 = 去重后的实发信号集（§3.6-7"去重属信号生成层、与因果引擎一致"，
先于 clean 判定执行）；分子 = 其中 (code, confirm_date) 亦出现在全史终态信号集
（对每段用 chanlib 跑全样本 + 同一去重/warmup/测试窗规则）者。去重前的原始全集
重画率另报作诊断（非 gate）。

== 生成层（§3.6-7 顺序：去重 → warmup → 测试窗，先于 clean 判定与组合层） ==
1) 同票 <5 交易日去重：间隔按该票实际有 bar 交易日计数；锚点 = 最近一次"保留"
   信号（贪心链）；多枢同触发分型由 chanlib.third_buys 按触发分型（合并K索引）
   去重（同探针），因果层不重复做；
2) warmup：确认日 ≥ 第 120 根有效 bar 交易日（§3.6-14c，chan_data.warmup_start_date）；
3) 测试窗：确认日 ≥ TEST_START。

== 组合层（§3.3；选信号在生成层完成 ≤5 并发——事件 buy 越限引擎直接 assert，
组合层选信号是引擎不变式的前置保证） ==
- 入场 = 确认日次一有 bar 交易日开盘 buy@open（停牌顺延由引擎自处理）；
- 出场（因果流）：顶分型出场 = 入场执行日后首个顶分型确认日 → 次一有 bar 交易日
  开盘 sell@open；20 日出场 = 入场日为第 1 根（按票内 bar 序）第 20 根收盘
  sell@close；先到日期生效，同日按顶分型口径（§3.6-8）；
- 选信号：确认时间先到先得贪心（同确认日按 code 升序）；持仓中同票重复信号忽略
  （候选入场日落在同票已接受持仓 [入场执行日, 出场执行日) 内）；最大同时持仓 5 只，
  确定性槽位模拟：槽位按 trade_calendar 逐日计数，占用区间 = [入场执行日,
  出场执行日)、出场执行日释放（同日先释放后可再入场——对应引擎"卖出相先于买入相"
  的执行次序）；候选生命周期内任一交易日并发将超 5 → 丢弃。产出 Instruction 列表
  交 chan_backtest.run_portfolio_backtest（事件指令模型）。空仓期 = 现金（写死）。

== 实现层新钉死（§3.6-14 同例，提请批次 3 结题确认冻结） ==
a. <5 交易日去重的贪心锚点 = 最近一次"保留"信号（非最近一次"出现"信号）——
   §3.6-7"只取确认时间第一次"对链式信号的传递语义取标准贪心读法。
b. 顶分型出场事件流 = 前缀时点检出的全部顶分型确认（§3.2"分型"字面，不限成笔
   端点分型）；同一顶分型因末根合并K延长会多次确认，出场取"入场执行日后首个"，
   该重复对出场无影响。
c. 交易腿信号集 = 去重后实发全集（§3.3 字面未含 clean 过滤；clean 是 §3.1/Gate 1
   的样本分辨率判据，不进入 §3.3 组合构造）。
d. 出场/入场指令仅当执行 bar 存在时生成：20 日出场越数据末尾 → 不出 20 日指令；
   顶分型确认日无次一 bar 同理；两规则均不可得 → 持有至数据末尾（exit=None，
   槽位占用至末，引擎按末收盘盯市）。
e. 同确认日多信号的处理次序键 = (confirm_date, code)，确定性。
f. 不可交易信号（确认日为该票末根 bar，无次日）不进组合层，单独计数披露。
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from signals.chan_backtest import Instruction  # noqa: E402
from signals.chan_data import (TEST_START, warmup_start_date,  # noqa: E402
                               window_before)
from signals.chanlib import (build_pivots, build_strokes, find_fractals,  # noqa: E402
                             merge_inclusion, split_segments, third_buys)

MIN_SIGNAL_GAP_BARS = 5        # §3.6-7 同票去重间隔（按票内有 bar 交易日计）
EXIT_HOLD_BARS = 20            # §3.6-8 20 日出场（入场日为第 1 根）
CLEAN_WINDOW_DAYS = 60         # §3.6-3 畸变窗（含确认日，按 trade_calendar）
NEAR_SHIFT_BARS = 8            # 重画归因（诊断）：确认日近距移位判定阈值
WARMUP_AUTO = "auto"           # scan 时由 chan_data.warmup_start_date 推断
HOLD_END_SENTINEL = "9999-12-31"   # 持有至数据末尾的槽位上界哨兵（ISO 日期序）


# ---------------------------------------------------------------- 因果前缀扫描

def _segment_signals(hs, ls, sd):
    """段内全样本结构 → 终态三买信号列表（chanlib 冻结语义，一次跑全段）。"""
    merged = merge_inclusion(hs, ls)
    frs = find_fractals(merged)
    _, strokes, _ = build_strokes(frs)
    pivots = build_pivots(strokes)
    return third_buys(strokes, pivots, sd)["signals"], frs


def causal_pipeline_code(df, calendar, code, *, warmup_start=WARMUP_AUTO,
                         test_start=TEST_START,
                         high_col="high_qfq", low_col="low_qfq") -> dict:
    """单票因果管线：前缀重算扫描 + 终态结构 + 生成层过滤。

    warmup_start=WARMUP_AUTO 时用 chan_data.warmup_start_date(df)（§3.6-14c）；
    传 None 表示不限制（合成测试用）。test_start=None 同理。
    返回 dict：
      raw          原始实发（生成层 warmup/测试窗过滤、未 <5 去重）
      emitted      去重后实发（Gate 2 分母、Gate 1 clean 判定与组合层输入）
      terminal_raw 终态结构原始信号（同过滤，未去重）
      terminal     终态结构去重后信号（Gate 2 分子匹配集）
      top_events   前缀时点确认的顶分型事件（出场流，全史、无 warmup/测试窗过滤）
      dates / pos  该票全部 bar 交易日与 {日期: 下标}
      warmup_start / seg_meta（段起全局偏移，供 MACD 窗口诊断映射）
    """
    dates_all = list(df["trade_date"])
    pos_all = {d: i for i, d in enumerate(dates_all)}
    if warmup_start == WARMUP_AUTO:
        warmup_start = warmup_start_date(df)
    segs = split_segments(df, calendar)

    raw_signals, top_events, terminal_pool, seg_meta = [], [], [], []
    for seg_id, seg in enumerate(segs):
        sd = list(seg["trade_date"])
        hs = [float(x) for x in seg[high_col]]
        ls = [float(x) for x in seg[low_col]]
        seg_meta.append({"seg_id": seg_id, "offset": pos_all[sd[0]], "dates": sd})

        # 终态（全样本）结构信号——Gate 2 分子的来源
        tb_full, _ = _segment_signals(hs, ls, sd)
        for s in tb_full:
            s2 = dict(s)
            s2["seg_id"] = seg_id
            s2["formation_start_date"] = sd[s["formation_start_bar"]]
            terminal_pool.append(s2)

        # 因果前缀扫描：对每根 bar t 收盘时点重算 bars[0..t]（O(N²)，无前视）
        for t in range(len(sd)):
            merged = merge_inclusion(hs[:t + 1], ls[:t + 1])
            frs = find_fractals(merged)
            _, strokes, _ = build_strokes(frs)
            pivots = build_pivots(strokes)
            tb = third_buys(strokes, pivots, sd[:t + 1])
            for s in tb["signals"]:
                if s["confirm_bar"] == t:      # 本根收盘时点新确认的三买
                    s2 = dict(s)
                    s2["seg_id"] = seg_id
                    s2["formation_start_date"] = sd[s["formation_start_bar"]]
                    raw_signals.append(s2)
            for f in frs:                      # 顶分型确认事件（出场流）
                if f["type"] == "T" and f["conf_bar"] == t:
                    top_events.append({"date": sd[t], "pi": f["pi"]})

    for pool in (raw_signals, terminal_pool):
        pool.sort(key=lambda s: pos_all[s["confirm_date"]])
    for s in raw_signals + terminal_pool:
        s["code"] = code
    raw_f = _apply_window(raw_signals, warmup_start, test_start)
    terminal_raw = _apply_window(terminal_pool, warmup_start, test_start)
    emitted = _apply_window(dedup_signals(raw_signals, pos_all),
                            warmup_start, test_start)
    terminal = _apply_window(dedup_signals(terminal_pool, pos_all),
                             warmup_start, test_start)
    return {"code": code, "raw": raw_f, "emitted": emitted,
            "terminal_raw": terminal_raw, "terminal": terminal,
            "top_events": top_events,
            "top_event_dates": sorted(e["date"] for e in top_events),
            "dates": dates_all, "pos": pos_all,
            "warmup_start": warmup_start, "seg_meta": seg_meta}


def run_causal_scan(bars_by_code, calendar, *, test_start=TEST_START,
                    warmup_start=WARMUP_AUTO,
                    high_col="high_qfq", low_col="low_qfq", progress=None) -> dict:
    """core 池全量因果扫描：{code: causal_pipeline_code 结果}（按代码升序）。

    warmup_start=WARMUP_AUTO 时逐票用 chan_data.warmup_start_date 推断（§3.6-14c）；
    传 None 表示不限制（合成测试用）。"""
    out = {}
    for code in sorted(bars_by_code):
        out[code] = causal_pipeline_code(
            bars_by_code[code], calendar, code, test_start=test_start,
            warmup_start=warmup_start, high_col=high_col, low_col=low_col)
        if progress is not None:
            progress(out[code])
    return out


# ---------------------------------------------------------------- 生成层规则

def _apply_window(signals, warmup_start, test_start):
    """warmup（§3.6-14c）与测试窗过滤（§3.6-7 顺序：去重先行，此处后置过滤）。"""
    out = signals
    if warmup_start is not None:
        out = [s for s in out if s["confirm_date"] >= warmup_start]
    if test_start is not None:
        out = [s for s in out if s["confirm_date"] >= test_start]
    return out


def dedup_signals(signals, code_pos, min_gap: int = MIN_SIGNAL_GAP_BARS) -> list:
    """§3.6-7：同票两信号确认日间隔（按该票实际有 bar 交易日计）<min_gap 只取
    确认时间第一次；贪心锚点 = 最近一次"保留"信号（实现层钉死 a）。"""
    kept, last = [], None
    for s in sorted(signals, key=lambda s: code_pos[s["confirm_date"]]):
        p = code_pos[s["confirm_date"]]
        if last is not None and p - last < min_gap:
            continue
        kept.append(s)
        last = p
    return kept


# ---------------------------------------------------------------- 交易计划

def plan_signal_trade(sig, code_dates, code_pos, top_event_dates, *,
                      hold_bars: int = EXIT_HOLD_BARS):
    """信号 → 交易计划（§3.2/§3.6-8）。

    入场 = 确认日次一有 bar 交易日（开盘 buy@open 由指令层固定）；
    出场 = 顶分型（入场执行日后首个顶分型确认日 → 次一有 bar 交易日开盘）与
    20 日（入场日第 1 根、第 hold_bars 根收盘）先到日期，同日按顶分型口径。
    确认日无次日 → None（不可交易）；某规则执行 bar 不存在 → 该规则缺省；
    两规则均缺省 → exit=None（持有至数据末尾）。
    """
    i = code_pos[sig["confirm_date"]]
    if i + 1 >= len(code_dates):
        return None
    entry = code_dates[i + 1]
    top_sell = None
    for d in top_event_dates:              # 升序（扫描序），取首个 > 入场执行日
        if d > entry:
            k = code_pos[d]
            if k + 1 < len(code_dates):
                top_sell = {"date": code_dates[k + 1], "price_type": "open",
                            "kind": "top_fractal"}
            break
    day_sell = None
    if i + 1 + hold_bars - 1 < len(code_dates):
        day_sell = {"date": code_dates[i + 1 + hold_bars - 1],
                    "price_type": "close", "kind": "day20"}
    if top_sell is not None and (day_sell is None
                                 or top_sell["date"] <= day_sell["date"]):
        exit_ = top_sell                   # 同日按顶分型口径（§3.6-8）
    else:
        exit_ = day_sell
    return {"entry_date": entry, "exit": exit_,
            "exit_date": exit_["date"] if exit_ else None}


def select_portfolio(signals, plans, calendar_pos, data_end_idx) -> dict:
    """§3.3 生成层选信号（确定性槽位模拟，事件 buy 越限 assert 的前置保证）。

    - signals 按 (confirm_date, code) 先到先得（内部排序，同日 code 升序）；
    - 持仓中同票重复信号忽略：候选入场日 ∈ 同票已接受持仓 [entry, exit)；
    - 最大同时持仓 5 只：槽位按 trade_calendar 逐日计数，占用 = [entry, exit)、
      出场执行日释放（同日先释放后可再入场）；候选生命周期 [entry, exit) 内
      任一交易日已有 5 只持仓 → 丢弃；exit=None（持有至末）占用至 data_end_idx。
    返回 {"accepted", "n_untradable", "n_held_ignored", "n_cap_dropped"}。
    """
    accepted = []
    n_untr = n_held = n_cap = 0
    occ: dict = {}                          # calendar idx -> 已占槽位数
    for s in sorted(signals, key=lambda x: (x["confirm_date"], x["code"])):
        plan = plans.get((s["code"], s["confirm_date"]))
        if plan is None:
            n_untr += 1
            continue
        e = plan["entry_date"]
        x_end = plan["exit_date"] or HOLD_END_SENTINEL
        if any(a["code"] == s["code"] and a["entry_date"] <= e < a["exit_end"]
               for a in accepted):
            n_held += 1
            continue
        e_idx = calendar_pos[e]
        hi = calendar_pos[plan["exit_date"]] if plan["exit_date"] \
            else data_end_idx + 1          # 出场执行日释放（半开区间 [e, x)）
        if any(occ.get(i, 0) >= 5 for i in range(e_idx, hi)):
            n_cap += 1
            continue
        accepted.append({"code": s["code"], "confirm_date": s["confirm_date"],
                         "entry_date": e, "exit": plan["exit"],
                         "exit_date": plan["exit_date"], "exit_end": x_end})
        for i in range(e_idx, hi):
            occ[i] = occ.get(i, 0) + 1
    return {"accepted": accepted, "n_untradable": n_untr,
            "n_held_ignored": n_held, "n_cap_dropped": n_cap}


def build_instructions(accepted) -> list:
    """接受信号 → 事件指令：入场 buy@open；顶分型出场 sell@open / 20 日 sell@close；
    exit=None 不出卖出指令（持有至末）。"""
    instrs = []
    for a in accepted:
        instrs.append(Instruction(a["entry_date"], a["code"], "buy", "open"))
        if a["exit"] is not None:
            instrs.append(Instruction(a["exit"]["date"], a["code"], "sell",
                                      a["exit"]["price_type"]))
    return instrs


# ---------------------------------------------------------------- clean / 月桶 / gate 判定

def clean_check(sig, *, warmup_start, distortion_set, breaks, calendar, cal_pos,
                clean_window_days: int = CLEAN_WINDOW_DAYS) -> tuple:
    """§3.6-3 + §3.6-14c clean 判定，返回 (ok, reason)。

    - warmup：确认日 ≥ 第 120 根有效 bar 交易日；
    - 畸变窗：确认日前 clean_window_days 个交易日（按 trade_calendar、含确认日）
      内无该票畸变日；
    - 形成窗：形成窗起点（枢首笔起点分型极值所在合并K首根原始 bar 交易日）至
      确认日之间无复牌日（起点当日复牌不算——结构整体位于断链后单段内）。
    """
    cd = sig["confirm_date"]
    if warmup_start is not None and cd < warmup_start:
        return False, "warmup"
    win = set(window_before(calendar, cd, clean_window_days, pos=cal_pos))
    if distortion_set & win:
        return False, "distortion"
    start = sig["formation_start_date"]
    for _d1, resume, _n in breaks:
        if start < resume <= cd:
            return False, "resume"
    return True, ""


def month_bucket_stats(signals, months) -> dict:
    """§3.6-1 月桶：测试窗全部自然月含零月计数 + 中位（含零月 gate 口径 /
    仅覆盖月对照口径）。信号归属月 = 确认日所在自然月（§3.6-14a）。"""
    counts = {m: 0 for m in months}
    for s in signals:
        m = s["confirm_date"][:7]
        if m in counts:
            counts[m] += 1
    vals = [counts[m] for m in months]
    cov = [v for v in vals if v > 0]
    return {"counts": counts,
            "median_all": float(statistics.median(vals)) if vals else 0.0,
            "median_covered": float(statistics.median(cov)) if cov else 0.0,
            "n_months": len(months), "n_covered": len(cov)}


MIN_CLEAN = 80              # Gate 1：clean 信号下限（§3.4）
MIN_MONTH_MEDIAN = 3        # Gate 1：月桶中位下限（§3.4/§3.6-1）
REPAINT_LIMIT = 0.15        # Gate 2：重画率上限（§3.4）
ANN_EXCESS_MIN = 0.05       # Gate 3 ①：扣成本年化超额下限（§3.4）
SEG_POS_MIN = 2             # Gate 3 ②：三段超额为正段数下限（§3.4）
MDD_GAP_MAX = 0.02          # Gate 3 ③：MDD 差上限（§3.4/§3.6-13）


def judge_gate1(clean_count, month_median) -> bool:
    return clean_count >= MIN_CLEAN and month_median >= MIN_MONTH_MEDIAN


def judge_gate2(repaint) -> bool:
    return repaint <= REPAINT_LIMIT


def judge_gate3(ann_excess, seg_excesses, mdd_diff) -> dict:
    c1 = ann_excess >= ANN_EXCESS_MIN
    n_pos = sum(1 for v in seg_excesses.values() if v > 0)
    c2 = n_pos >= SEG_POS_MIN
    c3 = mdd_diff <= MDD_GAP_MAX
    return {"c1": c1, "c2": c2, "c3": c3, "n_seg_pos": n_pos,
            "pass": bool(c1 and c2 and c3)}


def evaluate_gates(*, clean_count, month_median, repaint,
                   ann_excess, seg_excesses, mdd_diff) -> dict:
    """三 gate 短路评估（§3.4/§3.5：Gate 1 fail → 后续未跑；Gate 2 fail → Gate 3
    未跑"实现不可信就地归档"）。供注入假数字直接单测。"""
    g1 = judge_gate1(clean_count, month_median)
    if not g1:
        return {"gate1": False, "gate2": None, "gate3": None,
                "gate3_detail": None, "verdict": "FAIL"}
    g2 = judge_gate2(repaint)
    if not g2:
        return {"gate1": True, "gate2": False, "gate3": None,
                "gate3_detail": None, "verdict": "FAIL"}
    detail = judge_gate3(ann_excess, seg_excesses, mdd_diff)
    return {"gate1": True, "gate2": True, "gate3": detail["pass"],
            "gate3_detail": detail,
            "verdict": "PASS" if detail["pass"] else "FAIL"}


# ---------------------------------------------------------------- 重画率度量

def repaint_report(emitted, terminal, pos_by_code) -> dict:
    """Gate 2 重画率：1 − |实发 ∩ 终态| / |实发|（按 (code, confirm_date) 匹配），
    附归因拆分（诊断）：确认日近距移位（≤NEAR_SHIFT_BARS 根内存在同票终态信号，
    末根合并K延长/结构改写后移）vs 信号消失（结构改写后该枢不再构成三买）。"""
    hit = {(s["code"], s["confirm_date"]) for s in terminal}
    n_ok = sum(1 for s in emitted if (s["code"], s["confirm_date"]) in hit)
    missed = [s for s in emitted if (s["code"], s["confirm_date"]) not in hit]
    term_by_code = {}
    for s in terminal:
        term_by_code.setdefault(s["code"], []).append(s)
    n_shift = n_vanish = 0
    for s in missed:
        p = pos_by_code[s["code"]][s["confirm_date"]]
        near = any(abs(pos_by_code[s["code"]][t["confirm_date"]] - p)
                   <= NEAR_SHIFT_BARS for t in term_by_code.get(s["code"], []))
        n_shift += 1 if near else 0
        n_vanish += 0 if near else 1
    rate = (1.0 - n_ok / len(emitted)) if emitted else 0.0
    return {"n_emitted": len(emitted), "n_hit": n_ok, "repaint": rate,
            "n_shift": n_shift, "n_vanish": n_vanish, "missed": missed}
