"""缠论 R3 批次 2b/3 —— 因果信号 gate 实跑脚本（预注册判据，先定后跑）。

施工方案：docs/缠论R3施工方案-2026-09-19.md。运行：
    .venv/bin/python3 -m signals.chan_research
退出码：0=pass（三 gate 全过），1=fail（任一 gate 不过即归档，供 CI 挂门）。
只读 market.db（sqlite URI mode=ro），不写任何库表；不动生产面。
gate 只认本脚本实跑输出；禁止扫参数、禁止对 gate 数字做事后调整（§3.5 归档红线：
任一 gate fail → 归档结题、不进生产；再评估须新预注册批次立项）。

因果引擎与重画率定义见 signals/chan_causal.py（前缀重算 O(N²) 可证明无前视）；
组合回测引擎与基线腿见 signals/chan_backtest.py；结构引擎见 signals/chanlib.py；
数据层见 signals/chan_data.py。§3.6 口径钉死补遗（与 §3 同冻结）由上述模块实现，
要点：畸变日 pct_change 差分严格 >1pp；分型确认 = 第 3 根合并K i_end 原始 bar 收盘；
严格笔中间根合并K索引差 ≥4；中枢延伸闭区间无上限；突破 = 向上笔整体高于中枢、
回抽端点 > ZG 单次尝试；5 交易日去重生成层先行；clean 60 交易日窗含确认日 +
形成窗无复牌日；出场 D0 计数停牌顺延；基线腿 Top5 细节；MDD 差 = 信号腿 − 基线腿。

==============================================================================
以下为施工方案 §3 预注册口径原文（批次 0 commit 后不可变，完整抄入）：
==============================================================================

## 3. 预注册口径（批次 0 commit 后不可变；原文须完整抄入 signals/chan_research.py docstring）

### 3.1 数据
- 样本：config.json `watchlist_core`（51 只）；价格列 high_qfq / low_qfq / close_qfq；
  open_qfq = open + (close_qfq − close)（加法型复权当日全 OHLC 常数偏移，逐 bar 构造）。
- 畸变日：|ret_qfq − ret_raw| > 1pp 的交易日（单日复权阶梯）。
  clean 信号 = 确认日前 60 个交易日窗口内无畸变日（覆盖 EMA26 衰减 ~34 根）且形成窗无断链。
- 断链：按 trade_calendar，相邻 bar 间隔 >1 交易日即断链——不跨链连笔、不跨链比较。
- 每票 warmup 120 根有效 bar 后才允许产生信号；测试窗 2024-07-01 起。

### 3.2 结构定义（缠论机制员冻结清单）
- 包含处理：方向沿用前一合并K相对其前一根的方向（首根默认向下）；向上合并取
  max(高)/max(低)，向下取 min(高)/min(低)，递归至无包含。
- 分型：合并后相邻三根，中根高低点同为极值；端点=中根高低点；顶底分型不共用合并K。
- 成笔：主口径 = 严格笔（顶底分型间 ≥5 根合并K、不共用元素）；辅口径 = czsc 宽松笔
  （≥4 根）仅作敏感性披露，不参与 gate。
- 笔中枢：连续三笔重叠，ZG=min(三笔高点)、ZD=max(三笔低点)、须 ZG>ZD；后续笔与
  [ZD,ZG] 有重叠即延伸；不做中枢升级/扩展合并。
- 三买：中枢最后一个延伸笔之后的**第一根**向上笔笔顶 > ZG（突破），其后的**第一根**
  向下笔回抽，判据 = 回抽笔底分型端点价 > ZG（禁用收盘价替代）。
- 因果确认：三买确认日 = 回抽笔底分型第 3 根合并K收盘日；入场 = 确认日次日开盘价
  （open_qfq）；次日停牌顺延至复牌开盘。
- 出场：顶分型第 3 根合并K收盘确认 → 次日开盘出；或自入场起第 20 个交易日收盘出；
  先到者。成本：进/出各 0.15%（双边 0.3%）。
- MACD 顶背驰（close_qfq，EMA12/26、DEA9，柱面积同向前后笔段比较）：**只记录、不作
  主出场**，仅披露检出率与诊断。

### 3.3 组合构造
- 信号腿：三买确认次日开盘等权买入；持仓中同票重复信号忽略；同票 5 交易日内重复信号
  只取第一次；最大同时持仓 5 只，超额信号按确认时间先到先得、其余丢弃；
  **空仓期 = 现金**（主口径，写死；不持有 momentum 替代）。
- 基线腿（受控 momentum）：同一引擎、同一池、同持仓上限与成本；每月末换持
  "截至上月末 20 日动量最强 Top5"等权。**表述红线**：此为受控对照腿，非生产
  momentum profile，结题结论不得混称。

### 3.4 Gate（先定后跑，逐项过）
- **Gate 1 样本分辨率**：clean 信号 ≥80（同票 5 交易日去重后计）且信号月桶中位 ≥3；
  不足 → 判"无分辨率"直接 FAIL，不做超额归因。
- **Gate 2 因果性**：重画率 ≤15%。重画率 = 1 −（因果引擎实发信号中，最终结构下同票同
  确认日仍构成三买的比例）。
- **Gate 3 walk-forward**（对照基线腿，测试窗内净口径，三项全过）：
  ① 扣成本年化超额 ≥ +5%；
  ② 2025H1 / 2025H2 / 2026 三段超额 ≥2 段为正；
  ③ 信号腿 MDD 劣于基线 ≤2pp。
- 诊断（非 gate，如实披露）：事件层 fwd20 vs 池等权；MACD 背驰检出率；两种出场贡献
  拆分；分段收益。事件层 fwd20 对池等权 <+1% 时结题报告单列"入场几何增量薄"提示。

### 3.5 归档红线
- 任一 gate fail → 归档结题、不进生产；**禁止**改 ZG/中枢延伸/成笔口径/出场规则重跑；
  再评估须新预注册批次立项。脚本退出码 0=pass / 1=fail，供 CI 挂门。

==============================================================================
抄入结束。以下为实现层说明。
==============================================================================

流程：只读加载 core 51 + trade_calendar → 因果扫描（实发原始/去重两计数）→
Gate 1（clean ≥80 且月桶中位（含零月）≥3；fail 则 Gate 2/3 标"未跑（Gate 1 fail
就地归档）"并 exit 1）→ Gate 2（重画率 ≤15%；fail 则 Gate 3 标"未跑（实现不可信
就地归档）"并 exit 1）→ Gate 3（信号腿 vs 受控 momentum 基线腿，同引擎同窗同成本，
三项全过）→ 诊断（fwd20 vs 池等权 / MACD 背驰检出率 / 出场贡献拆分 / 分段收益）→
三 gate 逐项数字与总 VERDICT。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from signals.chan_backtest import momentum_baseline_leg, run_portfolio_backtest  # noqa: E402
from signals.chan_causal import (  # noqa: E402
    build_instructions, causal_pipeline_code, clean_check, evaluate_gates,
    judge_gate1, judge_gate2, judge_gate3, month_bucket_stats,
    plan_signal_trade, repaint_report, select_portfolio)
from signals.chan_data import (TEST_START, calendar_index, chain_breaks,  # noqa: E402
                               distortion_days, load_core_bars)
from signals.chanlib import compute_structure, macd_divergence, split_segments  # noqa: E402
from signals.rotation import metrics, seg_total  # noqa: E402


def _fmt_pct(x) -> str:
    return f"{x:+.2%}"


def _diagnostics(bars_by_code, calendar, scan, emitted, accepted) -> dict:
    """非 gate 诊断（§3.4：如实披露）：fwd20 vs 池等权 / MACD 背驰检出率 /
    出场贡献拆分 / 分段收益在 Gate 3 打印。"""
    out = {}
    # ---- 事件层 fwd20 vs 池等权（入场次日开盘 → +20 票内 bar 收盘，不扣成本）
    pool_open, pool_close = {}, {}
    for c, df in bars_by_code.items():
        pool_open[c] = dict(zip(df["trade_date"], df["open_qfq"].astype(float)))
        pool_close[c] = dict(zip(df["trade_date"], df["close_qfq"].astype(float)))
    sig_rets, pool_rets = [], []
    for s in emitted:
        info = scan[s["code"]]
        i = info["pos"][s["confirm_date"]] + 1          # 入场 bar（次日）
        j = i + 20                                       # +20 票内 bar
        if j >= len(info["dates"]):
            continue
        d_in, d_out = info["dates"][i], info["dates"][j]
        try:
            r = pool_close[s["code"]][d_out] / pool_open[s["code"]][d_in] - 1.0
        except KeyError:
            continue
        pr = [pool_close[c][d_out] / pool_open[c][d_in] - 1.0
              for c in pool_close
              if d_in in pool_open[c] and d_out in pool_close[c]]
        if not pr:
            continue
        sig_rets.append(r)
        pool_rets.append(float(np.mean(pr)))
    m_sig = float(np.mean(sig_rets)) if sig_rets else 0.0
    m_pool = float(np.mean(pool_rets)) if pool_rets else 0.0
    out["fwd20"] = {"n": len(sig_rets), "signal": m_sig, "pool": m_pool,
                    "excess": m_sig - m_pool}

    # ---- MACD 背驰检出率：全史笔级 + 信号持仓窗口内（chanlib.macd_divergence）
    tot_stk = div_stk = win_stk = win_div = 0
    sig_with_div = 0
    strokes_global = {}          # code -> [(g_start, g_end, divergence)]
    for code in sorted(bars_by_code):
        info = scan[code]
        segs = split_segments(bars_by_code[code], calendar)
        per = []
        for seg_id, seg in enumerate(segs):
            offset = info["seg_meta"][seg_id]["offset"]
            st = compute_structure(seg)
            divs = macd_divergence(seg["close_qfq"].astype(float), st["strokes"])
            for d in divs:
                per.append((offset + d["start_bar"], offset + d["end_bar"],
                            d["divergence"]))
                tot_stk += 1
                div_stk += 1 if d["divergence"] else 0
        strokes_global[code] = per
    for a in accepted:
        info = scan[a["code"]]
        i0 = info["pos"][a["entry_date"]]
        i1 = info["pos"][a["exit_date"]] if a["exit_date"] else len(info["dates"]) - 1
        hit_any = False
        for g0, g1, dv in strokes_global[a["code"]]:
            if g1 >= i0 and g0 <= i1:
                win_stk += 1
                win_div += 1 if dv else 0
                hit_any = hit_any or dv
        sig_with_div += 1 if hit_any else 0
    out["macd"] = {"strokes": tot_stk, "divergent": div_stk,
                   "rate": div_stk / tot_stk if tot_stk else 0.0,
                   "win_strokes": win_stk, "win_divergent": win_div,
                   "win_rate": win_div / win_stk if win_stk else 0.0,
                   "sig_with_div": sig_with_div, "sig_total": len(accepted)}
    return out


def main() -> int:
    t0 = time.time()
    bars_by_code, calendar = load_core_bars()          # 只读（sqlite URI mode=ro）
    cal_pos = calendar_index(calendar)
    last_bar = max(df["trade_date"].iloc[-1] for df in bars_by_code.values())
    months = sorted({d[:7] for d in calendar if TEST_START <= d <= last_bar})
    n_bars = sum(len(v) for v in bars_by_code.values())
    print("=== 缠论 R3 批次 2b —— 因果信号 gate 实跑（只读，§3.4 预注册判据） ===")
    print(f"数据: {calendar[0]} ~ {calendar[-1]}（{len(calendar)} 交易日，末根 {last_bar}），"
          f"core {len(bars_by_code)} 票 {n_bars} bar；测试窗 {TEST_START} 起，"
          f"月桶 {len(months)} 个月（{months[0]}~{months[-1]}，含零月）")

    # ---- [1] 因果扫描（前缀重算 O(N²)，逐票逐断链段）
    print(f"\n[1] 因果扫描（逐票前缀重算，确认日=i_end 收盘；{time.strftime('%H:%M:%S')}）")
    scan = {}
    for code in sorted(bars_by_code):
        info = causal_pipeline_code(bars_by_code[code], calendar, code)
        scan[code] = info
        print(f"  {code}: 段={len(info['seg_meta'])} 原始={len(info['raw'])} "
              f"去重={len(info['emitted'])} 终态={len(info['terminal'])}")
    raw_all = [s for c in sorted(scan) for s in scan[c]["raw"]]
    emitted = [s for c in sorted(scan) for s in scan[c]["emitted"]]
    terminal_raw = [s for c in sorted(scan) for s in scan[c]["terminal_raw"]]
    terminal = [s for c in sorted(scan) for s in scan[c]["terminal"]]
    pos_by_code = {c: scan[c]["pos"] for c in scan}
    print(f"  实发信号：原始 {len(raw_all)}（含末根合并K延长再发）/ 去重 {len(emitted)}"
          f"（Gate 2 分母）；终态结构：原始 {len(terminal_raw)} / 去重 {len(terminal)}")
    print(f"  扫描耗时 {time.time() - t0:.0f}s")

    # ---- [2] Gate 1 样本分辨率（clean 判定在去重后集合上，§3.6-7）
    print(f"\n[2] Gate 1 样本分辨率（clean ≥{80} 且月桶中位（含零月）≥{3}）")
    distortion = {c: distortion_days(bars_by_code[c]) for c in bars_by_code}
    breaks = {c: chain_breaks(list(bars_by_code[c]["trade_date"]), calendar)
              for c in bars_by_code}
    clean_signals, clean_reject = [], {"warmup": 0, "distortion": 0, "resume": 0}
    for s in emitted:
        ok, why = clean_check(
            s, warmup_start=scan[s["code"]]["warmup_start"],
            distortion_set=distortion[s["code"]], breaks=breaks[s["code"]],
            calendar=calendar, cal_pos=cal_pos)
        if ok:
            clean_signals.append(s)
        else:
            clean_reject[why] += 1
    mb = month_bucket_stats(clean_signals, months)
    n_covered_ge3 = sum(1 for v in mb["counts"].values() if v >= 3)
    print(f"  去重后实发 {len(emitted)} → clean {len(clean_signals)}"
          f"（剔除：warmup {clean_reject['warmup']} / 畸变窗 {clean_reject['distortion']}"
          f" / 形成窗复牌 {clean_reject['resume']}）")
    print("  月桶（clean 信号，含零月）: " + "  ".join(
        f"{m}:{n}" for m, n in mb["counts"].items()))
    print(f"  月桶中位：含零月 {mb['median_all']:g}（gate 口径，"
          f"{mb['n_months']} 月/{mb['n_covered']} 月覆盖、月≥3 的月 {n_covered_ge3} 个）"
          f"  仅覆盖月 {mb['median_covered']:g}（对照，不参与 gate）")
    gate1 = judge_gate1(len(clean_signals), mb["median_all"])
    print(f"  Gate 1: clean {len(clean_signals)}（≥80? "
          f"{'PASS' if len(clean_signals) >= 80 else 'FAIL'}）× "
          f"月桶中位 {mb['median_all']:g}（≥3? "
          f"{'PASS' if mb['median_all'] >= 3 else 'FAIL'}）→ "
          f"{'PASS' if gate1 else 'FAIL'}")
    if not gate1:
        print("\n  Gate 2/3：未跑（Gate 1 fail 就地归档，§3.5）")
        print(f"\nVERDICT: FAIL（Gate 1）  耗时 {time.time() - t0:.0f}s")
        return 1

    # ---- [3] Gate 2 因果性（重画率，去重后实发集 vs 终态集）
    print(f"\n[3] Gate 2 因果性（重画率 ≤15%？；§3.4 定义引用见 chan_causal docstring）")
    rep = repaint_report(emitted, terminal, pos_by_code)
    rep_raw = repaint_report(raw_all, terminal_raw, pos_by_code)
    gate2 = judge_gate2(rep["repaint"])
    print(f"  gate 口径（去重后集）：实发 {rep['n_emitted']}，终态仍构成三买 "
          f"{rep['n_hit']} → 重画率 {rep['repaint']:.2%} → "
          f"{'PASS' if gate2 else 'FAIL'}")
    print(f"  诊断口径（原始全集，非 gate）：实发 {rep_raw['n_emitted']}，命中 "
          f"{rep_raw['n_hit']} → 重画率 {rep_raw['repaint']:.2%}")
    print(f"  重画归因：确认日移位 {rep['n_shift']}（末根合并K延长/结构改写后移，"
          f"近距 ≤{8} 根有终态信号）/ 信号消失 {rep['n_vanish']}（结构改写后该枢"
          f"不再构成三买）")
    ext_dup = sum(1 for k in {(s["code"], s["trigger_pi"]) for s in raw_all}
                  if sum(1 for s in raw_all
                         if (s["code"], s["trigger_pi"]) == k) > 1)
    print(f"  延长再发事件（同触发分型多根原始发射，去重合并）：{ext_dup} 组")
    for s in rep["missed"][:8]:
        p = pos_by_code[s["code"]][s["confirm_date"]]
        near = [t["confirm_date"] for t in terminal if t["code"] == s["code"]
                and abs(pos_by_code[t["code"]][t["confirm_date"]] - p) <= 8]
        tag = f"→ 移位至 {near}" if near else "→ 消失"
        print(f"    重画例: {s['code']} 确认 {s['confirm_date']} "
              f"(ZG={s['zg']:.2f}) {tag}")
    if not gate2:
        print("\n  Gate 3：未跑（实现不可信就地归档，§4 批次 2b 验收）")
        print(f"\nVERDICT: FAIL（Gate 2 重画率）  耗时 {time.time() - t0:.0f}s")
        return 1

    # ---- [4] Gate 3 walk-forward（信号腿 vs 受控 momentum 基线腿，§3.6-13）
    print(f"\n[4] Gate 3 walk-forward（对照受控 momentum 基线腿，测试窗净口径）")
    plans = {}
    for s in emitted:
        plans[(s["code"], s["confirm_date"])] = plan_signal_trade(
            s, scan[s["code"]]["dates"], scan[s["code"]]["pos"],
            scan[s["code"]]["top_event_dates"])
    data_end_idx = cal_pos[last_bar]
    sel = select_portfolio(emitted, plans, cal_pos, data_end_idx)
    instructions = build_instructions(sel["accepted"])
    print(f"  组合层选信号（§3.3，生成层）：候选 {len(emitted)} → 接受 "
          f"{len(sel['accepted'])}（不可交易 {sel['n_untradable']} / 持仓中同票忽略 "
          f"{sel['n_held_ignored']} / 5 只上限丢弃 {sel['n_cap_dropped']}），"
          f"指令 {len(instructions)} 条")
    leg_s = run_portfolio_backtest(bars_by_code, calendar, instructions)
    leg_b = momentum_baseline_leg(bars_by_code, calendar)
    tot_s, ann_s, mdd_s = metrics(leg_s["returns"])
    tot_b, ann_b, mdd_b = metrics(leg_b["returns"])
    segs_s = seg_total(leg_s["returns"])
    segs_b = seg_total(leg_b["returns"])
    seg_exc = {k: segs_s.get(k, 0.0) - segs_b.get(k, 0.0) for k in segs_b}
    ann_exc = ann_s - ann_b
    mdd_gap = mdd_s - mdd_b
    n_buy = sum(1 for t in leg_s["trades"] if t["side"] == "buy")
    print(f"  {'腿':<10}{'累计':>10}{'年化':>10}{'MDD':>9}{'成交':>7}")
    print(f"  {'信号腿':<10}{tot_s:>9.1%}{ann_s:>9.1%}{mdd_s:>8.1%}{n_buy:>7}")
    print(f"  {'基线腿':<10}{tot_b:>9.1%}{ann_b:>9.1%}{mdd_b:>8.1%}"
          f"{leg_b['switches']:>7}")
    print(f"  （信号腿 skipped={len(leg_s['skipped'])} noops={len(leg_s['noops'])} "
          f"最大持仓 {leg_s['n_positions_max']}/5；基线腿 skipped="
          f"{len(leg_b['skipped'])}）")
    for k in sorted(seg_exc):
        print(f"  段 {k}: 信号 {segs_s.get(k, 0.0):+.2%} vs 基线 "
              f"{segs_b.get(k, 0.0):+.2%} → 超额 {seg_exc[k]:+.2%}")
    g3 = judge_gate3(ann_exc, seg_exc, mdd_gap)
    print(f"  ① 扣成本年化超额 {ann_exc:+.2%}（≥ +5%? "
          f"{'PASS' if g3['c1'] else 'FAIL'}）")
    print(f"  ② 三段超额为正 {g3['n_seg_pos']}/3（≥2 段? "
          f"{'PASS' if g3['c2'] else 'FAIL'}）")
    print(f"  ③ MDD 差（信号 − 基线，§3.6-13）{mdd_gap:+.2%}（劣于基线 ≤2pp? "
          f"{'PASS' if g3['c3'] else 'FAIL'}）")
    gate3 = g3["pass"]

    # ---- [5] 诊断（非 gate，如实披露）
    print(f"\n[5] 诊断（非 gate，§3.4）")
    diag = _diagnostics(bars_by_code, calendar, scan, emitted, sel["accepted"])
    d20 = diag["fwd20"]
    print(f"  事件层 fwd20：n={d20['n']} 信号均值 {d20['signal']:+.2%}  "
          f"池等权均值 {d20['pool']:+.2%}  超额 {d20['excess']:+.2%}"
          f"{'  ← 入场几何增量薄（<+1%，结题报告单列提示）' if d20['excess'] < 0.01 else ''}")
    dm = diag["macd"]
    print(f"  MACD 背驰检出率：全史笔级 {dm['divergent']}/{dm['strokes']}"
          f"（{dm['rate']:.1%}）；信号持仓窗内笔级 {dm['win_divergent']}/"
          f"{dm['win_strokes']}（{dm['win_rate']:.1%}），"
          f"持仓窗含背驰的信号 {dm['sig_with_div']}/{dm['sig_total']}")
    kind_n, kind_ret = {}, {}
    by_code_buy = {t["code"]: t for t in leg_s["trades"] if t["side"] == "buy"}
    by_code_sell = {t["code"]: t for t in leg_s["trades"] if t["side"] == "sell"}
    hold_end = 0
    for a in sel["accepted"]:
        if a["exit"] is None:
            hold_end += 1
            continue
        b, sl = by_code_buy[a["code"]], by_code_sell[a["code"]]
        r = (sl["gross"] - sl["cost"]) / (b["gross"] + b["cost"]) - 1.0
        k = a["exit"]["kind"]
        kind_n[k] = kind_n.get(k, 0) + 1
        kind_ret.setdefault(k, []).append(r)
    parts = []
    for k, label in [("top_fractal", "顶分型"), ("day20", "20日")]:
        if kind_n.get(k):
            rs = kind_ret[k]
            parts.append(f"{label} n={len(rs)} 平均 {float(np.mean(rs)):+.2%}")
    parts.append(f"持有至末 n={hold_end}")
    print("  出场贡献拆分（净口径单笔收益）: " + "；".join(parts))

    # ---- 总结：三 gate 逐项数字与总 VERDICT
    gates = evaluate_gates(clean_count=len(clean_signals),
                           month_median=mb["median_all"],
                           repaint=rep["repaint"],
                           ann_excess=ann_exc, seg_excesses=seg_exc,
                           mdd_diff=mdd_gap)
    print(f"\n=== 三 gate 逐项（§3.4 先定后跑） ===")
    print(f"Gate 1 样本分辨率: clean {len(clean_signals)}/80, 月桶中位(含零月) "
          f"{mb['median_all']:g}/3 → {'PASS' if gates['gate1'] else 'FAIL'}")
    print(f"Gate 2 因果性: 重画率 {rep['repaint']:.2%}/15% → "
          f"{'PASS' if gates['gate2'] else 'FAIL'}")
    if gates["gate3"] is None:
        print("Gate 3 walk-forward: 未跑")
    else:
        d = gates["gate3_detail"]
        print(f"Gate 3 walk-forward: ① {ann_exc:+.2%} ② {d['n_seg_pos']}/3 段为正 "
              f"③ MDD 差 {mdd_gap:+.2%} → {'PASS' if gates['gate3'] else 'FAIL'}")
    print(f"\nVERDICT: {gates['verdict']}  耗时 {time.time() - t0:.0f}s")
    return 0 if gates["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
