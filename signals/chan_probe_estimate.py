"""缠论 R3 批次 0 —— 数据侧预估（一次性，非 gate 脚本）。

**预注册 gate 脚本是 signals/chan_research.py（批次 2b 交付）**；本脚本只是批次 0
的数据地基预估，用于"开工 / 提前归档"决策（施工方案 §4 批次 0 验收行），
其输出不构成 gate 裁决、不回填 §6 数字。
- 结构实现沿用探针粗版（/tmp/chan_probe_r3.py，一次性口径，不进仓库当口径）：
  包含 → 分型 → 严格笔 → 中枢 → 三买；与冻结 §3.6-6 语义的两处已知偏差
  （延伸笔数上限 ext≤3、突破判"up 且 hi>ZG 先于交集检查"），故本预估只是
  冻结口径信包的近似带。
- 唯一按 §3.6 修正：包含首根合并K方向默认**向下**（探针原为向上）。
- 对每个原始信号套用**冻结 clean 规则**（§3.1 + §3.6）：
  ① 同票 5 交易日去重（生成层，先 clean 前去重，先到者优先，§3.6-7）；
  ② warmup：确认 bar 为该票第 ≥120 根有效 bar；
  ③ 测试窗：确认日 ≥ 2024-07-01（TEST_START）；
  ④ 畸变：确认日前 60 个交易日（按 trade_calendar、含确认日，§3.6-3）窗口内
    无 |ret_qfq − ret_raw|>1pp 畸变日（pct_change 口径，§3.6-2）；
  ⑤ 形成窗无断链：中枢首笔起点（首笔起点分型所在合并K首根原始 bar）至确认日
    之间无复牌日（§3.6-3；多枢同触发取扫描序最先枢）。
只读纪律：sqlite URI mode=ro，不写任何库表；退出码恒 0（非 gate）。
运行：.venv/bin/python3 -m signals.chan_probe_estimate
"""
from __future__ import annotations

import statistics as st
import sys
from collections import defaultdict

import pandas as pd

from signals.chan_data import (TEST_START, WARMUP_BARS, calendar_index,
                               chain_breaks, distortion_days, load_core_bars,
                               valid_mask, window_before)

CAL_WINDOW = 60          # 确认日前 60 个交易日畸变窗（§3.6-3）
DEDUP_GAP = 5            # 同票信号去重间隔（交易日，§3.3/§3.6-7）
PROBE_RAW_179 = 179      # §1 对照：探针粗版全池信号（首根向上）
PROBE_CLEAN_157 = 157    # §1 对照：探针畸变剔除后（无 warmup/断链/去重）


# ---- 探针粗版结构（一次性口径；仅包含首根方向按 §3.6-4 改为向下） ----

def inclusion(bars):
    """bars: [(h,l)] → 合并K；首根默认向下（§3.6-4），后续按前K相对前前K高点。"""
    proc = []
    for i, (h, l) in enumerate(bars):
        if proc:
            last = proc[-1]
            if (h >= last["h"] and l <= last["l"]) or (h <= last["h"] and l >= last["l"]):
                up = (proc[-1]["h"] >= proc[-2]["h"]) if len(proc) >= 2 else False
                if up:
                    last["h"] = max(last["h"], h); last["l"] = max(last["l"], l)
                else:
                    last["h"] = min(last["h"], h); last["l"] = min(last["l"], l)
                last["i_end"] = i
                continue
        proc.append({"h": h, "l": l, "i_start": i, "i_end": i})
    return proc


def fractals(proc):
    frs = []
    for i in range(1, len(proc) - 1):
        a, b, c = proc[i - 1], proc[i], proc[i + 1]
        if b["h"] > a["h"] and b["h"] > c["h"] and b["l"] > a["l"] and b["l"] > c["l"]:
            frs.append({"t": "T", "p": b["h"], "pi": i, "conf": c["i_end"]})
        elif b["h"] < a["h"] and b["h"] < c["h"] and b["l"] < a["l"] and b["l"] < c["l"]:
            frs.append({"t": "B", "p": b["l"], "pi": i, "conf": c["i_end"]})
    return frs


def build_strokes(frs):
    kept = []
    for f in frs:
        if not kept:
            kept.append(f); continue
        lastk = kept[-1]
        if f["t"] == lastk["t"]:
            if ((f["t"] == "T" and f["p"] >= lastk["p"])
                    or (f["t"] == "B" and f["p"] <= lastk["p"])):
                kept[-1] = f
        else:
            if f["pi"] - lastk["pi"] >= 4:
                kept.append(f)
    stks = [{"dir": "up" if a["t"] == "B" else "down",
             "hi": max(a["p"], b["p"]), "lo": min(a["p"], b["p"]),
             "sf": a, "ef": b} for a, b in zip(kept, kept[1:])]
    return kept, stks


def third_buys(stks):
    """探针粗版三买（breakout=up&hi>ZG 先于交集检查、ext≤3 上限——与冻结语义
    的两处已知偏差见 docstring）；同触发分型去重并记录枢首笔索引。"""
    sigs, n = [], len(stks)
    for i in range(n - 2):
        s0, s1, s2 = stks[i], stks[i + 1], stks[i + 2]
        zg = min(s0["hi"], s1["hi"], s2["hi"])
        zd = max(s0["lo"], s1["lo"], s2["lo"])
        if zg <= zd:
            continue
        j, ext, leave = i + 3, 0, None
        while j < n and ext <= 3:
            s = stks[j]
            if s["dir"] == "up" and s["hi"] > zg:
                leave = s; break
            if s["lo"] <= zg and s["hi"] >= zd:
                ext += 1; j += 1
            else:
                break
        if leave is None or j + 1 >= n:
            continue
        pull = stks[j + 1]
        if pull["dir"] == "down" and pull["lo"] > zg:
            sigs.append({"zg": zg, "zd": zd, "pivot_i": i,
                         "trig": pull["ef"], "top": leave["ef"]})
    seen, out = set(), []
    for s in sigs:
        k = s["trig"]["pi"]
        if k not in seen:
            seen.add(k); out.append(s)
    return out


def main() -> int:
    bars_by_code, calendar = load_core_bars()
    cal_pos = calendar_index(calendar)
    print(f"=== R3 批次 0 数据侧预估（core 51，探针粗版结构+首根向下，冻结 clean 规则） ===")
    print(f"日历: {calendar[0]} ~ {calendar[-1]}（{len(calendar)} 交易日）；"
          f"测试窗 {TEST_START} 起；warmup {WARMUP_BARS} 根有效 bar")

    n_raw = n_dedup = n_clean = 0
    stocks_with_clean = []
    per_stock = {}
    mon_all = defaultdict(int)          # 确认日月桶（含零月另行补 0）
    clean_rows = []
    for code, df in bars_by_code.items():
        dates = df["trade_date"].tolist()
        dset = distortion_days(df)
        vmask = valid_mask(df)
        vrank = {}                      # bar idx → 有效 bar 序数（1-based）
        r = 0
        for i, ok in enumerate(vmask.tolist()):
            if ok:
                r += 1
            vrank[i] = r
        breaks = chain_breaks(dates, calendar)
        resume = [b[1] for b in breaks]
        warm_start = warmup_date = None
        okv = df.loc[vmask, "trade_date"].tolist()
        warmup_date = okv[WARMUP_BARS - 1] if len(okv) >= WARMUP_BARS else None

        bars = list(zip(df["high_qfq"], df["low_qfq"]))
        proc = inclusion(bars)
        frs = fractals(proc)
        kept, stks = build_strokes(frs)
        sigs = third_buys(stks)
        n_raw += len(sigs)

        # ① 同票 5 交易日去重（生成层，先于 clean；third_buys 已按触发序升序）
        kept_sigs, last_conf = [], None
        for s in sigs:
            if last_conf is None or s["trig"]["conf"] - last_conf >= DEDUP_GAP:
                kept_sigs.append(s)
                last_conf = s["trig"]["conf"]
        n_dedup += len(kept_sigs)

        n_ok = 0
        for s in kept_sigs:
            t = s["trig"]["conf"]
            d = dates[t]
            if warmup_date is None or vrank.get(t, 0) < WARMUP_BARS:
                continue                                    # ② warmup
            if d < TEST_START:
                continue                                    # ③ 测试窗
            win = set(window_before(calendar, d, CAL_WINDOW, cal_pos))
            if win & dset:
                continue                                    # ④ 畸变窗
            fstart = proc[stks[s["pivot_i"]]["sf"]["pi"]]["i_start"]
            fdate = dates[fstart]
            if any(fdate < rd <= d for rd in resume):
                continue                                    # ⑤ 形成窗断链
            n_ok += 1
            mon_all[d[:7]] += 1
            clean_rows.append((code, d, s["zg"]))
        n_clean += n_ok
        per_stock[code] = (len(sigs), len(kept_sigs), n_ok)
        if n_ok:
            stocks_with_clean.append((code, n_ok))

    # 月桶：测试窗内全部自然月（含零信号月）；以数据实际末日为界（trade_calendar
    # 表含未来交易日，2026-10 之后尚未发生，不计入——§3.6-1"测试窗内"字面语义）
    last_data = max(df["trade_date"].iloc[-1] for df in bars_by_code.values())
    months = pd.period_range(TEST_START, last_data, freq="M").astype(str).tolist()
    mv = [mon_all.get(m, 0) for m in months]
    covered = [v for v in mv if v > 0]
    med_all = st.median(mv) if mv else 0
    med_cov = st.median(covered) if covered else 0

    print(f"\n[对照 §1 探针] 本预估原始信号 {n_raw}（探针 179，差异=首根方向向下+数据层"
          f"微差）；探针 clean 157 为畸变单口径、未做 warmup/断链/去重")
    print(f"[去重] 同票 5 交易日去重后（生成层，先于 clean）: {n_dedup}")
    print(f"[clean 信号预估] {n_clean}（warmup+测试窗+畸变窗60td+形成窗断链 全冻结口径）")
    print(f"[有信号票数] {len(stocks_with_clean)}/51"
          f"（top5: {sorted(stocks_with_clean, key=lambda x: -x[1])[:5]}）")

    print(f"\n[月桶] 测试窗 {months[0]}~{months[-1]} 共 {len(months)} 个自然月（含零信号月）")
    for m, v in zip(months, mv):
        print(f"  {m}: {'·' if v == 0 else v}{'（零）' if v == 0 else ''}")
    print(f"  月桶中位（含零月，Gate 1 口径）: {med_all}")
    print(f"  月桶中位（仅覆盖月，对照口径）: {med_cov}；覆盖 {len(covered)}/{len(months)} 月；"
          f"月≥3 的月数 {sum(1 for v in mv if v >= 3)}；零信号月 {sum(1 for v in mv if v == 0)}")

    g1a = n_clean >= 80
    g1b = med_all >= 3
    print(f"\n[Gate 1 前瞻] clean ≥80? {n_clean} → {'PASS' if g1a else 'FAIL'}；"
          f"月桶中位(含零月) ≥3? {med_all} → {'PASS' if g1b else 'FAIL'}")
    print(f"[结论] 预估 {'≥80：支持 PROCEED（最终以批次 2b chan_research.py 冻结引擎为准）' if g1a else '<80：EARLY-ARCHIVE'}")
    print("\nestimate done, exit 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
