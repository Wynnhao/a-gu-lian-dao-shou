"""缠论 LLM 证据版（全量打包批 · 批次 2）—— 确认口径因果化重钉 + 逐日证据标签流。

施工方案：docs/全量打包施工方案-2026-09-20.md §3.3（预注册，批次 0 commit 后不可变，
逐字抄录于下）。运行：.venv/bin/python3 -m signals.chan_evidence
（market.db 只读 mode=ro，经 chan_data.load_core_bars URI 只读连接；标签流只在
内存/stdout，不入库；退出码恒 0——本批**无 pass/fail gate**，四项强制披露如实回填）。

复用资产（全部只读零修改）：chanlib（结构引擎 §3.2/§3.6 冻结语义）、chan_causal
（前缀重算因果引擎 + <5 交易日去重 + §3.6-8 出场计划 plan_signal_trade + 重画诊断
阈值 NEAR_SHIFT_BARS）、chan_data（数据层 + warmup）。R3 背景：Gate2 重画率 61.46%
FAIL 归档，机械线不复活不调参；本批标签作为软证据进 LLM 上下文，天然规避可交易性
要求，唯一前置 = 定义层重钉"首发确认即锁定"因果口径（R3 方案 §6.3）。

==============================================================================
以下为施工方案 §3.3 预注册口径原文（批次 0 commit 后不可变，完整抄入）：
==============================================================================

### 3.3 批次 2 · 缠论 LLM 证据版（无 pass/fail gate；口径重钉冻结 + 强制披露）

脚本 `signals/chan_evidence.py`（mode=ro，复用 chanlib/chan_causal/chan_data 资产）。

- **确认口径因果化重钉（本批冻结，替代 R3 机械信号语义）**：
  1. 标签流由 chan_causal 前缀重算逐日生成；**历史标签永不回改**（首发确认即锁定）。
  2. 三买状态标签：首发确认日锁定为"三买持仓态"，在因果流中出场条件触发（顶分型确认
     或 20 日先到，照 R3 §3.6-8 出场计数）后转为"三买结束"；此后同枢再确认按新事件记录。
  3. 结构上下文标签逐日更新但只增不改历史：当前笔方向、最近顶/底分型类型与距今交易日数、
     最近中枢 [ZD,ZG] 与现价相对位置（上/内/下）。
- **强制披露（结题如实回填，不设阈值 gate）**：①锁定三买标签 T+20 终态相容率（与
  chanlib 全史终态结构不矛盾的比例）；②标签日变更频率/票；③覆盖率（warmup 后有标签
  票数/50）；④与 R3 机械信号流（288 实发）的关系说明。
- **产出**：逐票逐日标签流（内存/临时产物，不入库）+ bundle 字段规格（每票一行结构摘要，
  token 上限照滞后票墙惯例，具体文案批次 6 定）+ 结题附 3 票样例供用户人工验收。
- **验收**：单测绿（锁定语义/不回改/出场解锁/标签域）+ core 51 全池实跑统计产出。

==============================================================================
抄入结束。以下为实现层钉死（与 R3 §3.6 同性质：只消歧义钉死唯一语义，不新增可调
参数；冻结于本批 commit）。供历史标签因果性的完整钉死清单 a~j：
==============================================================================

a. **标签行范围**：warmup 起始日（chan_data.warmup_start_date，§3.6-14c 第 120 根有效
   bar 交易日）之后该票全部有 bar 交易日，每日期恰一行，只增不改。R3 测试窗
   TEST_START=2024-07-01 **不适用**（那是 R3 回测窗概念；证据标签为全史 warmup 后口径，
   测试窗对照只在披露④做）。warmup 起始日为 None（有效 bar 不足 120，无信号资格票，
   如 core 中 6 bar 票 688801）→ 该票标签流为空（不生成任何标签行）。合成测试显式传
   warmup_start=None 表示"不设 warmup 限制"，与"无资格"区分。
b. **事件输入** = chan_causal 因果实发流 emitted（原始发射经 <5 交易日去重 + warmup
   过滤，test_start=None 即全史 warmup 后）——与 R3 生成层同源同实现。
c. **出场日** = plan_signal_trade（R3 §3.6-8 冻结实现逐字复用：顶分型确认日次一有 bar
   交易日开盘 / 入场日起第 20 根有 bar 交易日收盘，先到者，同日顶分型口径）。三买状态
   在**出场执行日**翻转为「三买结束」；两规则均不可得（数据末尾）→ 持仓态保持至数据
   末（事件级标注 exit_kind=数据末，日级状态不出现该原因）。
d. **持仓中忽略**：新信号确认日落在既有事件 [首发确认日, 出场执行日) 内 → 不开新事件
   （计入披露）；确认日 == 出场执行日 → 该日既有事件已结束，开新事件（与 R3 组合层
   "同日先释放后入场"次序一致）。此即"此后同枢再确认按新事件记录"的实现语义：出场后
   任何再确认（含同枢、含新枢）均按事件序号递增记录为新事件。
e. **不可交易信号**（确认日为该票末根 bar，plan=None）仍开事件：三买状态标签是结构
   状态而非持仓（exit=数据末，单独计数披露）。
f. **上下文标签定义**：当前笔方向 = 最近保留分型起正在形成的笔（底分型→up / 顶分型→
   down / 无保留分型→None）；最近分型 = 前缀全部检出分型（find_fractals，不限成笔
   端点）中确认 bar 最大者，距今交易日数 = t − conf_bar（票内有 bar 序）；最近中枢 =
   前缀中枢中 (last_stroke_idx, start_stroke_idx) 字典序最大者，现价 = 当日收盘
   （真实数据 close_qfq / 合成 close），位置 = close>ZG 上 / close<ZD 下 / 其余内。
   每行由该日前缀结构决定，写入后永不回改（延长/改写只影响后缀行——前缀稳定性）。
g. **披露①实现**：相容 ⟺ 同票 chanlib 全史终态三买信号（causal_pipeline 的 terminal，
   同 warmup 过滤、test_start=None）中存在确认日距该事件首发确认日 |Δ| ≤
   NEAR_SHIFT_BARS=8（chan_causal 冻结诊断阈值，移位类容差；R3 §6.2 归因：移位 90
   例为 i_end 延长所致，非形态否认）者；同日命中另计（诊断）。不相容 = 消失类（终态
   结构否认该处存在三买）。"T+20"命名的含义 = 以事件 20 日出场窗口为考察期的终态
   对照（全史终态结构已含 T+20 及以后全部信息）。
h. **披露②实现**：离散字段 = {三买状态, 当前笔方向, 最近分型类型, 中枢相对位置}
   （"距今 N 日"计数器不计变更）；变更日 = 与前一标签行任一离散字段不同；
   频率 = 变更日 / (标签行数 − 1)，逐票计算后报池均值/中位。
i. **披露④实现**：E_r3 = emitted 中确认日 ≥ TEST_START 的子集（与 R3 chan_research
   同引擎同数据 → 可复现实跑 288）；证据事件首发确认集与 E_r3 按 (code, confirm_date)
   对照；恒等式 |E_r3| = 窗口内事件数 + 窗口内持仓中忽略数（状态机处理 emitted 全序列，
   每个信号非忽略即开事件）。差异归因即持仓中忽略（R3 在组合层按入场日忽略，本批在
   标签层按确认日/标签状态忽略——两忽略口径的差 = 出场执行日当日的边界信号）。
j. **样例票确定性选取**：A = 事件数最多票（并列取代码序最小）；B = 000333（R3 方案
   §6.2 Gate2 归因点名票：移位 + 消失链典型；若不在池内则回退中位票）；C = 事件数
   中位票（按 (事件数, 代码) 排序取下中位元素）。三者去重后不足 3 票按序补齐。

标签行 schema（bundle 消费口径，字段名冻结）：
{code, date,
 tb_state: "无"|"三买持仓"|"三买结束",
 tb_exit_kind: None|"顶分型"|"20日"（结束态当期事件原因；持仓中忽略不改历史），
 tb_confirm: 当期事件首发确认日, tb_days_since_confirm: 距首发确认日票内 bar 数,
 tb_event_seq: 当期事件序号（1 起）,
 stroke_dir: "up"|"down"|None, last_fractal: "T"|"B"|None, last_fractal_days: int|None,
 pivot_zd/pivot_zg: float|None, pivot_pos: "上"|"内"|"下"|None}
"""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from signals.chan_causal import (EXIT_HOLD_BARS, NEAR_SHIFT_BARS,  # noqa: E402
                                 WARMUP_AUTO, causal_pipeline_code,
                                 plan_signal_trade)
from signals.chan_data import TEST_START, load_core_bars, warmup_start_date  # noqa: E402
from signals.chanlib import (build_pivots, build_strokes, find_fractals,  # noqa: E402
                             merge_inclusion, split_segments)

# ---------------------------------------------------------------- 标签域（冻结）

TB_NONE = "无"            # 尚无三买事件（事件一旦出现，状态不再回到"无"）
TB_HOLD = "三买持仓"      # 首发确认锁定，出场条件未触发
TB_END = "三买结束"       # 出场执行日翻转；保持至下一事件首发确认日
TB_STATE_DOMAIN = (TB_NONE, TB_HOLD, TB_END)

EXIT_TOP = "顶分型"       # §3.6-8 顶分型出场（确认日次一有 bar 交易日开盘）
EXIT_DAY20 = "20日"       # §3.6-8 20 日出场（入场日起第 20 根收盘）
EXIT_HOLD_END = "数据末"  # 仅事件级标注：数据末未出场；日级状态不出现该原因
EXIT_KIND_DOMAIN = (EXIT_TOP, EXIT_DAY20, EXIT_HOLD_END)

POS_ABOVE, POS_INSIDE, POS_BELOW = "上", "内", "下"

# 披露②的离散字段集（钉死 h；计数器类字段不计变更）
DISCRETE_FIELDS = ("tb_state", "stroke_dir", "last_fractal", "pivot_pos")


# ---------------------------------------------------------------- 上下文标签（§3.3-3，钉死 f）

def context_labels(df, calendar, *, warmup_start=None,
                   high_col="high_qfq", low_col="low_qfq",
                   close_col="close_qfq") -> dict:
    """逐日结构上下文标签（段内前缀重算，只增不改）：{date: row}。

    对每个断链段（chanlib.split_segments，§3.6-14d 不跨链）的每根 bar t（收盘时点），
    用 chanlib 冻结语义对 bars[0..t] 重算 包含→分型→严格笔→中枢（与 chan_causal
    前缀重算同一套纯函数），提取三件上下文：当前笔方向 / 最近分型与距今 / 最近中枢
    与现价相对位置。行的取值只依赖该日前缀，天然满足"只增不改历史"。
    """
    out: dict = {}
    for seg in split_segments(df, calendar):
        sd = [str(d) for d in seg["trade_date"]]
        hs = [float(x) for x in seg[high_col]]
        ls = [float(x) for x in seg[low_col]]
        cl = [float(x) for x in seg[close_col]]
        for t in range(len(sd)):
            d = sd[t]
            if warmup_start is not None and d < warmup_start:
                continue
            merged = merge_inclusion(hs[:t + 1], ls[:t + 1])
            frs = find_fractals(merged)
            kept, strokes, _ = build_strokes(frs)
            pivots = build_pivots(strokes)
            if kept:
                stroke_dir = "up" if kept[-1]["type"] == "B" else "down"
            else:
                stroke_dir = None
            if frs:
                fr = max(frs, key=lambda f: f["conf_bar"])
                last_fractal, last_fractal_days = fr["type"], t - fr["conf_bar"]
            else:
                last_fractal, last_fractal_days = None, None
            if pivots:
                pv = max(pivots, key=lambda p: (p["last_stroke_idx"],
                                                p["start_stroke_idx"]))
                zd, zg, c = float(pv["zd"]), float(pv["zg"]), cl[t]
                if c > zg:
                    pos = POS_ABOVE
                elif c < zd:
                    pos = POS_BELOW
                else:
                    pos = POS_INSIDE
            else:
                zd = zg = pos = None
            out[d] = {"stroke_dir": stroke_dir, "last_fractal": last_fractal,
                      "last_fractal_days": last_fractal_days,
                      "pivot_zd": zd, "pivot_zg": zg, "pivot_pos": pos}
    return out


# ---------------------------------------------------------------- 三买事件状态机（§3.3-2，钉死 b~e）

def _pivot_key(sig) -> tuple:
    """同枢身份（诊断性归因键）：(段 id, 枢首笔索引, ZG, ZD)。"""
    return (sig.get("seg_id"), sig.get("pivot_first_stroke_idx"),
            round(float(sig["zg"]), 6), round(float(sig["zd"]), 6))


def tb_events(emitted, dates, pos, top_event_dates,
              *, hold_bars: int = EXIT_HOLD_BARS) -> dict:
    """三买事件状态机：chan_causal 去重后实发流 → 事件序列（§3.3-2 + 钉死 c/d/e）。

    输入 emitted 升序（按确认日）；对每个信号：
    - 持仓中忽略（钉死 d）：确认日 ∈ 既有事件 [首发确认日, 出场执行日) → 不开新事件，
      计 n_held_ignored；
    - 否则开新事件（序号递增）：出场 = plan_signal_trade（R3 §3.6-8 冻结实现）；
      plan=None（确认日为末根 bar，钉死 e）→ entry/exit 均 None、exit_kind=数据末，
      计 n_untradable。
    返回 {"events": [...], "n_held_ignored", "n_untradable",
    "n_same_pivot_reconfirm"}（同枢再确认 = pivot_key 在更早事件中出现过的当前事件数）。
    """
    events: list = []
    n_held = n_untr = 0
    cur = None
    for sig in sorted(emitted, key=lambda s: pos[s["confirm_date"]]):
        cd = sig["confirm_date"]
        if cur is not None and (cur["exit_date"] is None or cd < cur["exit_date"]):
            n_held += 1                       # 出场前再确认：不开新事件（钉死 d）
            continue
        plan = plan_signal_trade(sig, dates, pos, top_event_dates,
                                 hold_bars=hold_bars)
        if plan is None:
            n_untr += 1
            entry = exit_date = None
            exit_kind = EXIT_HOLD_END
        else:
            entry = plan["entry_date"]
            exit_date = plan["exit_date"]
            if plan["exit"] is None:
                exit_kind = EXIT_HOLD_END
            else:
                exit_kind = (EXIT_TOP if plan["exit"]["kind"] == "top_fractal"
                             else EXIT_DAY20)
        cur = {"seq": len(events) + 1, "confirm_date": cd, "entry_date": entry,
               "exit_date": exit_date, "exit_kind": exit_kind,
               "zg": sig["zg"], "zd": sig["zd"], "trigger_pi": sig["trigger_pi"],
               "trigger_price": sig["trigger_price"], "pivot_key": _pivot_key(sig)}
        events.append(cur)
    seen_keys = set()
    n_same_pivot = 0
    for e in events:
        if e["pivot_key"] in seen_keys:
            n_same_pivot += 1
        seen_keys.add(e["pivot_key"])
    return {"events": events, "n_held_ignored": n_held, "n_untradable": n_untr,
            "n_same_pivot_reconfirm": n_same_pivot}


# ---------------------------------------------------------------- 逐日标签流组装

def assemble_labels(code, dates, pos, ctx, events, warmup_start) -> list:
    """逐日标签行（warmup 后全部有 bar 交易日，每日期恰一行，只增不改）。

    三买状态取"确认日 ≤ d 的最新事件"：d ∈ [首发确认日, 出场执行日) → 三买持仓；
    d ≥ 出场执行日 → 三买结束（原因 = 当期事件出场类型）；出场执行日为 None →
    持仓保持至数据末。所有字段在日期 d 只依赖 ≤ d 的信息（因果）。
    """
    labels: list = []
    n = len(events)
    ei = -1                                  # 最新 confirm_date ≤ d 的事件下标
    for d in dates:
        if warmup_start is not None and d < warmup_start:
            continue
        row_ctx = ctx.get(d)
        if row_ctx is None:                  # 防御：每个 bar 日期必属某段，不应发生
            continue
        while ei + 1 < n and events[ei + 1]["confirm_date"] <= d:
            ei += 1
        if ei < 0:
            state, kind, conf, days, seq = TB_NONE, None, None, None, None
        else:
            ev = events[ei]
            if ev["exit_date"] is None or d < ev["exit_date"]:
                state, kind = TB_HOLD, None
            else:
                state, kind = TB_END, ev["exit_kind"]
            conf, seq = ev["confirm_date"], ev["seq"]
            days = pos[d] - pos[conf]
        labels.append({"code": code, "date": d, "tb_state": state,
                       "tb_exit_kind": kind, "tb_confirm": conf,
                       "tb_days_since_confirm": days, "tb_event_seq": seq,
                       **row_ctx})
    return labels


def evidence_pipeline(df, calendar, code, *, warmup_start=WARMUP_AUTO,
                      high_col="high_qfq", low_col="low_qfq",
                      close_col="close_qfq") -> dict:
    """单票证据管线：chan_causal 因果实发 → 事件状态机 → 逐日标签流。

    warmup_start=WARMUP_AUTO（默认）时用 chan_data.warmup_start_date 推断（§3.6-14c）；
    有效 bar 不足 120 → 无信号资格票：标签流为空（钉死 a）。显式传 None（合成测试）
    = 不设 warmup 限制。causal_pipeline 以 test_start=None 调用 = 全史 warmup 后口径
    （钉死 a/b）；终态结构 terminal 同口径返回，供披露①对照。
    """
    auto = warmup_start == WARMUP_AUTO
    if auto:
        warmup_start = warmup_start_date(df)
    info = causal_pipeline_code(df, calendar, code, warmup_start=warmup_start,
                                test_start=None, high_col=high_col,
                                low_col=low_col)
    if auto and warmup_start is None:
        # 无信号资格票（<120 根有效 bar）：不生成标签（钉死 a）
        return {"code": code, "labels": [], "events": [],
                "n_held_ignored": 0, "n_untradable": 0,
                "n_same_pivot_reconfirm": 0, "terminal": info["terminal"],
                "emitted": [], "raw": info["raw"], "pos": info["pos"],
                "dates": info["dates"], "warmup_start": None}
    ev = tb_events(info["emitted"], info["dates"], info["pos"],
                   info["top_event_dates"])
    ctx = context_labels(df, calendar, warmup_start=warmup_start,
                         high_col=high_col, low_col=low_col, close_col=close_col)
    labels = assemble_labels(code, info["dates"], info["pos"], ctx,
                             ev["events"], warmup_start)
    return {"code": code, "labels": labels, "events": ev["events"],
            "n_held_ignored": ev["n_held_ignored"],
            "n_untradable": ev["n_untradable"],
            "n_same_pivot_reconfirm": ev["n_same_pivot_reconfirm"],
            "terminal": info["terminal"], "emitted": info["emitted"],
            "raw": info["raw"], "pos": info["pos"], "dates": info["dates"],
            "warmup_start": warmup_start}


# ---------------------------------------------------------------- 强制披露（§3.3，无 gate）

def terminal_compatibility(events, terminal_signals, pos,
                           near_bars: int = NEAR_SHIFT_BARS) -> dict:
    """披露①：锁定三买标签 T+20 终态相容率（钉死 g）。

    events = tb_events 事件序列；terminal_signals = 同票 chanlib 全史终态三买信号
    （causal_pipeline.terminal，warmup 过滤、test_start=None）。相容 = 终态存在
    确认日距 ≤ near_bars 的信号（移位容差）；同日命中另计；差 = 消失类。
    """
    tp = [pos[s["confirm_date"]] for s in terminal_signals]
    n_compat = n_exact = 0
    vanished: list = []
    for e in events:
        p = pos[e["confirm_date"]]
        if p in tp:
            n_compat += 1
            n_exact += 1
        elif any(abs(p - q) <= near_bars for q in tp):
            n_compat += 1
        else:
            vanished.append(e)
    n = len(events)
    return {"n_events": n, "n_compat": n_compat, "n_exact": n_exact,
            "n_vanish": n - n_compat,
            "compat_rate": (n_compat / n) if n else None,
            "exact_rate": (n_exact / n) if n else None,
            "vanished": vanished}


def change_frequency(labels) -> dict:
    """披露②：标签日变更频率（钉死 h；分母 = 标签行数 − 1）。

    离散字段 = DISCRETE_FIELDS；变更日 = 任一离散字段与前一标签行不同。
    另报三买状态单独变更（口径更窄的对照）。行数 <2 → 频率 None。
    """
    m = len(labels) - 1
    if m < 1:
        return {"n_days": len(labels), "n_change": 0, "freq": None,
                "n_change_tb": 0, "freq_tb": None}
    n_chg = sum(1 for a, b in zip(labels, labels[1:])
                if any(a[f] != b[f] for f in DISCRETE_FIELDS))
    n_tb = sum(1 for a, b in zip(labels, labels[1:])
               if a["tb_state"] != b["tb_state"])
    return {"n_days": len(labels), "n_change": n_chg, "freq": n_chg / m,
            "n_change_tb": n_tb, "freq_tb": n_tb / m}


def r3_relationship(results, *, test_start: str = TEST_START) -> dict:
    """披露④：与 R3 机械信号流的关系（钉死 i）。

    E_r3 = 各票 emitted 中确认日 ≥ test_start 的子集（与 R3 chan_research 同引擎
    同数据，R3 实跑 288）；证据事件窗内首发集与之按 (code, confirm_date) 对照。
    恒等式：|E_r3| = 窗口内证据事件数 + 窗口内持仓中忽略数。
    """
    e_r3: list = []
    ev_keys: set = set()
    for code in sorted(results):
        r = results[code]
        for s in r["emitted"]:
            if s["confirm_date"] >= test_start:
                e_r3.append((code, s["confirm_date"]))
        for e in r["events"]:
            if e["confirm_date"] >= test_start:
                ev_keys.add((code, e["confirm_date"]))
    n_became = sum(1 for k in e_r3 if k in ev_keys)
    return {"test_start": test_start, "n_r3_emitted": len(e_r3),
            "n_evidence_events_in_window": len(ev_keys),
            "n_r3_became_event": n_became,
            "n_r3_held_ignored": len(e_r3) - n_became}


# ---------------------------------------------------------------- 样例（钉死 j）

def pick_samples(results) -> list:
    """3 票样例确定性选取（钉死 j）：最多事件 / 000333（R3 归因点名）/ 中位事件。"""
    by_n = sorted(results, key=lambda c: (len(results[c]["events"]), c))
    cands = []
    if by_n:
        cands.append(by_n[-1])
    cands.append("000333" if "000333" in results else by_n[len(by_n) // 2])
    cands.append(by_n[len(by_n) // 2])
    out: list = []
    for c in cands:
        if c not in out:
            out.append(c)
    return out[:3]


def state_timeline(labels, cap: int = 14) -> list:
    """标签流状态时间线（连续同 (状态, 事件序, 原因) 压缩为区间；超 cap 截断标 …）。

    元素 = [key, 起, 止]，key = (状态, 事件序, 出场原因)；截断标记行 key=("…", None, None)。
    """
    segs: list = []
    for row in labels:
        key = (row["tb_state"], row["tb_event_seq"], row["tb_exit_kind"])
        if segs and segs[-1][0] == key:
            segs[-1][2] = row["date"]
        else:
            segs.append([key, row["date"], row["date"]])
    if len(segs) > cap:
        half = cap // 2
        return (segs[:half]
                + [(("…", None, None), f"（略 {len(segs) - cap} 段）", None)]
                + segs[-(cap - half):])
    return segs


def _tl_seg(s) -> str:
    """时间线元素 → 一段文案（供 main 与报告样例；截断行只印说明）。"""
    key, a, b = s
    if key[0] == "…":
        return f"[{a}]"
    kind = f"({key[2]})" if key[2] else ""
    return f"[{a}~{b} {key[0]}#{key[1] if key[1] is not None else '-'}{kind}]"


# ---------------------------------------------------------------- core 全池实跑（只读，无 gate）

def _fmt_ev(e) -> str:
    return (f"#{e['seq']} 首发{e['confirm_date']} 入场"
            f"{e['entry_date'] or '-'} 出场{e['exit_date'] or '-'}"
            f"({e['exit_kind']}) ZG={e['zg']:.2f} ZD={e['zd']:.2f} "
            f"触发价={e['trigger_price']:.2f}")


def main() -> int:
    t0 = time.time()
    bars_by_code, calendar = load_core_bars()          # 只读（sqlite URI mode=ro）
    n_bars = sum(len(v) for v in bars_by_code.values())
    print("=== 缠论 LLM 证据版（全量打包批 · 批次 2）—— core 全池实跑（只读，无 gate） ===")
    print(f"数据: core {len(bars_by_code)} 票 {n_bars} bar，日历 {calendar[0]}~"
          f"{calendar[-1]}；口径 = 施工方案 §3.3（冻结）+ 本模块 docstring 钉死 a~j")

    results: dict = {}
    for code in sorted(bars_by_code):
        results[code] = evidence_pipeline(bars_by_code[code], calendar, code)
        r = results[code]
        print(f"  {code}: 标签行 {len(r['labels'])} 事件 {len(r['events'])}"
              f"（同枢再确认 {r['n_same_pivot_reconfirm']} / 持仓中忽略 "
              f"{r['n_held_ignored']} / 末根不可交易 {r['n_untradable']}）"
              f"  warmup={r['warmup_start'] or '无资格'}")
    print(f"  双前缀扫描（因果信号 + 上下文标签）耗时 {time.time() - t0:.0f}s")

    # ---- 汇总底数
    all_events = [e for c in sorted(results) for e in results[c]["events"]]
    n_labels = sum(len(r["labels"]) for r in results.values())
    kind_n = {k: sum(1 for e in all_events if e["exit_kind"] == k)
              for k in EXIT_KIND_DOMAIN}
    n_same_pivot = sum(r["n_same_pivot_reconfirm"] for r in results.values())
    n_held = sum(r["n_held_ignored"] for r in results.values())
    n_untr = sum(r["n_untradable"] for r in results.values())
    print(f"\n[底数] 事件总数 {len(all_events)}（出场：顶分型 {kind_n[EXIT_TOP]} / "
          f"20日 {kind_n[EXIT_DAY20]} / 数据末 {kind_n[EXIT_HOLD_END]}；同枢再确认 "
          f"{n_same_pivot}；全程持仓中忽略 {n_held}；末根不可交易 {n_untr}）；"
          f"标签行总数 {n_labels}")

    # ---- 披露①：T+20 终态相容率（逐票汇总 + 窗口内对照）
    per_compat = {c: terminal_compatibility(results[c]["events"],
                                            results[c]["terminal"],
                                            results[c]["pos"])
                  for c in sorted(results)}
    tot = sum(v["n_events"] for v in per_compat.values())
    compat = sum(v["n_compat"] for v in per_compat.values())
    exact = sum(v["n_exact"] for v in per_compat.values())
    vanish = sum(v["n_vanish"] for v in per_compat.values())
    inwin_compat = inwin_n = 0
    for c in sorted(results):
        pos = results[c]["pos"]
        tp = [pos[s["confirm_date"]] for s in results[c]["terminal"]]
        for e in results[c]["events"]:
            if e["confirm_date"] < TEST_START:
                continue
            inwin_n += 1
            p = pos[e["confirm_date"]]
            if p in tp or any(abs(p - q) <= NEAR_SHIFT_BARS for q in tp):
                inwin_compat += 1
    print(f"\n[披露①·锁定三买标签 T+20 终态相容率] 全史（warmup 后）: 相容 "
          f"{compat}/{tot} = {compat / tot:.2%}（同日命中 {exact} = "
          f"{exact / tot:.2%}；消失类 {vanish} = {vanish / tot:.2%}）")
    if inwin_n:
        print(f"  窗口内对照（首发确认 ≥ {TEST_START}，与 R3 Gate2 重画率同窗可比）: "
              f"相容 {inwin_compat}/{inwin_n} = {inwin_compat / inwin_n:.2%}")
    n_vanish_tickets = sorted(c for c in per_compat if per_compat[c]["n_vanish"])
    if n_vanish_tickets:
        print(f"  存在消失类事件票 ({len(n_vanish_tickets)}): "
              + ", ".join(f"{c}({per_compat[c]['n_vanish']})"
                          for c in n_vanish_tickets[:12])
              + ("…" if len(n_vanish_tickets) > 12 else ""))

    # ---- 披露②：标签日变更频率/票
    per_freq = {c: change_frequency(results[c]["labels"]) for c in sorted(results)}
    freqs = [v["freq"] for v in per_freq.values() if v["freq"] is not None]
    freqs_tb = [v["freq_tb"] for v in per_freq.values() if v["freq_tb"] is not None]
    tot_chg = sum(v["n_change"] for v in per_freq.values())
    tot_cmp = sum(v["n_days"] - 1 for v in per_freq.values())
    tot_chg_tb = sum(v["n_change_tb"] for v in per_freq.values())
    print(f"\n[披露②·标签日变更频率/票] 离散字段 {DISCRETE_FIELDS}；"
          f"票数（有 ≥2 标签行）{len(freqs)}")
    print(f"  池聚合: {tot_chg}/{tot_cmp} = {tot_chg / tot_cmp:.4f}（票均值 "
          f"{statistics.mean(freqs):.4f} / 中位 {statistics.median(freqs):.4f} / "
          f"min {min(freqs):.4f} / max {max(freqs):.4f}）")
    print(f"  三买状态单独变更: 池 {tot_chg_tb}/{tot_cmp} = "
          f"{tot_chg_tb / tot_cmp:.4f}（票均值 {statistics.mean(freqs_tb):.4f}）")

    # ---- 披露③：覆盖率（预注册分母 50 逐字照抄；core 实际 51 只如实注明）
    n_any = sum(1 for r in results.values() if r["labels"])
    n_ev = sum(1 for r in results.values() if r["events"])
    n_qual = sum(1 for r in results.values() if r["warmup_start"] is not None)
    print(f"\n[披露③·覆盖率] 预注册口径 = warmup 后有标签票数/50（分母 50 逐字照抄；"
          f"**core 实际 51 只**，其中 warmup 资格 {n_qual} 只）")
    print(f"  warmup 后有标签行票数: {n_any}/50"
          f"（实际 {n_any}/{len(results)}）")
    print(f"  warmup 后有三买事件票数: {n_ev}/50（实际 {n_ev}/{len(results)}）")

    # ---- 披露④：与 R3 机械信号流（288 实发）的关系
    rel = r3_relationship(results)
    print(f"\n[披露④·与 R3 机械信号流的关系] 同源 = chan_causal 前缀重算 + <5 交易日"
          f"去重 + warmup（同引擎同数据零改动复用）")
    print(f"  E_r3（emitted 中确认日 ≥ {TEST_START}，R3 chan_research 口径）= "
          f"{rel['n_r3_emitted']}（R3 实跑 288，同数据应复现）")
    print(f"  窗口内证据事件 {rel['n_evidence_events_in_window']} = E_r3 成为事件 "
          f"{rel['n_r3_became_event']} + 持仓中忽略 {rel['n_r3_held_ignored']}"
          f"（恒等式钉死 i）")
    print(f"  差异归因: ①证据版无测试窗（全史 warmup 后），窗口外事件 "
          f"{len(all_events) - inwin_n} 个；②持仓中忽略发生在标签层（按确认日标签状态，"
          f"钉死 d），R3 发生在组合层（按入场日，§3.3 组合构造）——两口径差 = 出场执行日"
          f"当日边界信号；③R3 组合层另有 5 只上限丢弃与不可交易剔除，证据版均无（无组合层）。")

    # ---- 3 票样例（钉死 j）
    print(f"\n[3 票样例·人工验收]（选取规则钉死 j：最多事件 / 000333 / 中位事件）")
    for code in pick_samples(results):
        r = results[code]
        print(f"\n  --- {code} ---  warmup={r['warmup_start']} 标签行 "
              f"{len(r['labels'])} 事件 {len(r['events'])}（持仓中忽略 "
              f"{r['n_held_ignored']}）")
        for e in r["events"][:10]:
            print(f"    {_fmt_ev(e)}")
        if len(r["events"]) > 10:
            print(f"    …（另 {len(r['events']) - 10} 事件略）")
        print("    状态时间线: " + "  ".join(
            _tl_seg(s) for s in state_timeline(r["labels"])))
        if r["labels"]:
            for row in (r["labels"][0], r["labels"][len(r["labels"]) // 2],
                        r["labels"][-1]):
                print(f"    样例行 {row['date']}: tb={row['tb_state']}"
                      f"{('(' + row['tb_exit_kind'] + ')') if row['tb_exit_kind'] else ''}"
                      f" 确认={row['tb_confirm']} 第{row['tb_days_since_confirm']}日"
                      f" 笔={row['stroke_dir']} 分型={row['last_fractal']}"
                      f"{row['last_fractal_days']}日前"
                      f" 中枢=[{row['pivot_zd']},{row['pivot_zg']}]"
                      f"{row['pivot_pos'] if row['pivot_pos'] else '-'}")

    print(f"\ndone（无 gate，披露如实），耗时 {time.time() - t0:.0f}s，exit 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
