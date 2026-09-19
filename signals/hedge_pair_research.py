"""对冲配对尾盘口径研究批 —— 批次 1（全量打包批，预注册 gate，先定后跑）。

施工方案：docs/全量打包施工方案-2026-09-20.md §3.2（预注册口径，批次 0 commit 后
不可变，原文逐字抄入下方）。纯离线研究：market.db 一律
sqlite3.connect("file:...market.db?mode=ro", uri=True) 只读，不写任何库表；
gate fail 即归档该线，禁止调参/换口径重跑，再评估须另立新预注册批次。
运行：.venv/bin/python3 -m signals.hedge_pair_research
退出码：0=pass，1=fail（供 CI 挂门）。

================================================================================
§3.2 批次 1 · 对冲配对尾盘口径研究批（预注册 gate）—— 原文逐字冻结
================================================================================

脚本 `signals/hedge_pair_research.py`（mode=ro、预注册 docstring、退出码 0=pass/1=fail）。

- **配对池**：config.json `watchlist_core`（51 只，可交易性约束；extended 观察票不得作为
  配对腿）。
- **配对白名单（月度重估，照 R1 月频红线）**：每月末用截至当日 250 日窗重估、次月全月生
  效；入选对需 ρ_250d < −0.30 且前后半窗（各 125 日）ρ 同为负 且 ρ_60d < 0。日收益用
  close_qfq。
- **触发（尾盘口径，全部主口径参数冻结）**：生效白名单内配对 (A,B)，A 当日收益 < −1.0%
  且 B 当日收益 > 0 → 当日尾盘执行：B 权重上调至 30%（底仓其余票等比缩）；**次日尾盘回落
  等权**（持有 1 日）。同对每 ISO 周最多触发 1 次；窗口重叠最新优先。
- **组合构造**：底仓 = 受控 momentum Top5 等权各 20%（每月末换持，照 R3 §3.3/§3.6-10 冻结
  细节；**表述红线：受控对照腿，非生产 momentum profile**）；B 不在底仓时买入（挤出现金或
  等比缩其余仓）；成本每边 0.15% + **尾盘乐观偏差扣减 0.3%/次调仓**（14:50 观测≈收盘价
  的乐观偏差预注册扣减；敏感性 0/0.3%/0.5% 三档只披露，主口径 0.3%）。
- **Gate（先定后跑，逐项过；fail 即归档）**：
  - Gate H0 分辨率：去重后触发 ≥60 且 触发月桶中位（含零月）≥3；不足 → 判"无分辨率"直接
    FAIL，不做超额归因。
  - Gate H1：扣成本年化超额（信号腿 − momentum 基线腿，算术差）≥ +5%。
  - Gate H2：2025H1 / 2025H2 / 2026 三段超额 ≥2 段为正（段内简单收益差）。
  - Gate H3：信号腿 MDD 劣于基线 ≤2pp。
- **诊断（非 gate，如实披露）**：分对贡献拆分；**T+1 执行对照组**（同信号改次日执行——
  预期超额消失，作为"跷跷板同日性"机制验证）；尾盘扣减三档敏感性；两种配对腿方向
  （A 跌买 B / B 跌买 A）检出数。
- **无论 gate 结果必须产出**：稳定配对清单生成函数（月度重估规则实现 + 当期清单），供
  批次 6 证据字段复用——**证据合入不依赖 gate**（用户裁决②）。

================================================================================
实现语义消歧（原文未钉死处的本批钉死实现，随本 docstring 冻结；不触碰上述任何
预注册数字）
================================================================================

1. 收益矩阵：core 51 票 close_qfq 按 trade_date pivot 后 pct_change()（与 R1
   signals/rotation.py 同一调用、同一 venv pandas 3 语义：不 pad，停牌日及其次日收益为
   NaN）。NaN 不满足任何触发不等式，也不进相关矩阵（pairwise complete + min_periods）。
2. 相关窗 min_periods 照 R1 rotation.estimate_whitelist 惯例：ρ_250d→150、前后半窗
   （各 125 日）→60、ρ_60d→40。
3. 月末与生效：月 = panel 月份（panel = 全部有票有 bar 的交易日）；月末 = 该月最后一个
   panel 交易日；重估窗 = 截至上月末的收益 tail(250)，满 250 行才有当月白名单；测试窗 =
   首个满足 250 日重估窗的月份起（照 R1 惯例，warmup 月不入测试）。
4. 执行价：一切调仓按执行日当日收盘价 close_qfq 成交（尾盘口径 14:50≈收盘）。月末换持
   同理：动量用截至月末收盘的 20 日收益（票内 bar 序 close[p−1]/close[p−21]−1，照 R3
   §3.6-10 定义/Top5/等权/不足 21 根剔除/不足 5 只现金兜底），同日收盘换持——与 R3
   "次月开盘调仓"的差异仅执行时点，源于本批全框架为收盘执行；同框架因果性 = 14:50 用
   近收盘数据计算并成交，其乐观偏差由两条腿共用的尾盘扣减覆盖。
5. 组合引擎（双腿同框架，只差信号）：日循环先盯市（当日有 bar 用当日 close_qfq，无 bar
   沿用最近有 bar 收盘 = 该日零收益）得 NAV_ref，再按目标调仓。目标权重仅在变化日调仓，
   目标不变期间持仓随价格漂移（买入持有，不产生交易与成本）。停牌票的目标交易顺延至其
   下一有 bar 交易日成交（每日重试；目标已再变则以最新目标为准 = 后到取消先到）；至数据
   末尾仍无 bar → 保持持仓（盯市沿用最近收盘）。
6. 触发日目标：当日全部去重后触发（不同对同日触发同一 B 合并）中每个 distinct B 目标权重
   30%；其余底仓票保持底仓权重（各 20%），若 Σ > 100% 则其余票等比缩至 100% − 30%×k；
   30%×k > 100% 时触发票自身等比缩至合计 100%（其余为 0）；底仓不足 5 只有现金时优先由
   现金容纳（不强制满仓，Σ < 100% 时其余票权重不动）。
7. 回落：触发后下一 panel 交易日收盘目标回落为该日底仓等权目标（持有 1 日）；该日若有
   新触发则新触发目标直接生效（窗口重叠最新优先的自然实现）。
8. 成本：每次调仓事件对实际变动名义 Σ|Δmv| 计——双边各 0.15%（每笔 |Δ| × 0.15%）＋
   尾盘扣减 0.3% × Σ|Δmv|（"0.3%/次调仓从该次交易收益中扣除"的实现语义 = 该次交易的
   名义 × 0.3% 在调仓当日以成本扣除，一阶等价于该次交易收益扣减 0.3pp）。变动为零不成交
   不计费（照 R3"只计变动"）。允许负现金（零息融资，照 R3 引擎消歧 b）。基线腿与信号腿
   共用同一成本函数，各自交易各自计费（保证超额归因只差信号）。
9. T+1 对照组：同一触发集合（去重/周限不变），执行日 = 触发日下一 panel 交易日收盘、
   持有至再下一日收盘回落；执行日 B 无 bar → 该次触发跳过交易（预期超额消失 = 跷跷板
   同日性机制验证）。
10. Gate 口径：H0 月桶 = 测试窗全部自然月含零月，中位数 ≥3 且去重后触发总数 ≥60；
    H1 = 年化(信号) − 年化(基线)（算术差，rotation.metrics 年化式）；H2 = SEG_DEFS
    （2025H1/2025H2/2026）段内简单收益差（段取与测试窗交集），≥2 段为正；H3 = MDD(信号)
    − MDD(基线) ≤ 2pp。H0 不过 → 直接 FAIL，不做 H1~H3 超额归因裁决（腿指标与诊断照披露）。
11. 分对贡献（诊断，线性近似）：每触发事件 Σ_code Δw(code) × r(code, 次一日)，Δw = 触发
    日信号腿目标 − 底仓目标，r 用 ffill 价格收益（停牌 0）；不含成本与漂移二阶项。
12. 当期稳定配对清单：生效月 = 数据末日所在月，其白名单 = 截至上月末 250 日窗重估结果；
    落盘 logs/reports/hedge_pairs_current.json（运行时产物，不入仓）+ 报告全文披露。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from bisect import bisect_right
from collections import namedtuple
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from signals.rotation import SEG_DEFS, metrics, seg_total  # noqa: E402  同源指标

DB_PATH = BASE / "data" / "market.db"
CONFIG_PATH = BASE / "config.json"
REPORT_JSON = BASE / "logs" / "reports" / "hedge_pairs_current.json"

# ---------------- 预注册参数（§3.2 冻结，跑后不许挪） ----------------
MIN_HIST = 250              # 白名单重估窗
RHO_250_THR = -0.30         # ρ_250d < −0.30
TRIGGER_THR = -0.010        # A 当日收益 < −1.0%
HEDGE_WEIGHT = 0.30         # B 上调至 30%
COST_RATE = 0.0015          # 每边 0.15%
TAIL_DRAG = 0.003           # 尾盘乐观偏差扣减 主口径 0.3%
TAIL_DRAG_SENS = (0.0, 0.003, 0.005)   # 敏感性三档（只披露）
TOP_N = 5                   # 受控 momentum Top5（R3 §3.6-10）
MOM_WINDOW = 20             # 20 日动量
BASE_WEIGHT = 0.20          # 底仓每票 20%
GATE_H0_MIN_TRIGGERS = 60   # 去重后触发 ≥60
GATE_H0_MIN_MEDIAN = 3      # 月桶中位（含零月）≥3
GATE_H1_MIN_EXCESS = 0.05   # 年化超额 ≥ +5%
GATE_H2_MIN_POS_SEGS = 2    # 三段 ≥2 正
GATE_H3_MAX_MDD_GAP = 0.02  # MDD 劣于基线 ≤2pp

Trigger = namedtuple("Trigger", ["date", "fall", "rise"])


# ---------------------------------------------------------------- 数据层（只读）

def _connect_ro(db_path: Path = DB_PATH) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)


def load_close_panel(db_path: Path = DB_PATH, config_path: Path = CONFIG_PATH) -> tuple:
    """core 51 收盘面板。返回 (px: DataFrame[date×code]=close_qfq, names: {code: 名称})。

    只读（sqlite URI mode=ro），不写任何库表；extended 观察票不进池（§3.2 配对池红线）。
    """
    cfg = json.loads(Path(config_path).read_text())
    core = cfg["watchlist_core"]
    codes = [e["code"] for e in core]
    names = {e["code"]: e.get("name", e["code"]) for e in core}
    ph = ",".join("?" * len(codes))
    conn = _connect_ro(db_path)
    try:
        df = pd.read_sql_query(
            f"SELECT code, trade_date, close_qfq FROM daily_bar "
            f"WHERE code IN ({ph}) ORDER BY trade_date", conn, params=codes)
    finally:
        conn.close()
    px = df.pivot(index="trade_date", columns="code", values="close_qfq").sort_index()
    return px[sorted(px.columns)], names


# ---------------------------------------------------------------- 白名单（R1 同款）

def _whitelist_rows(ret_win: pd.DataFrame) -> list:
    """按三项预注册标准返回白名单明细行（不足 250 日返回空）。

    min_periods 照 R1 rotation.estimate_whitelist：250→150、半窗→60、60→40。
    """
    if len(ret_win) < MIN_HIST:
        return []
    corr250 = ret_win.corr(min_periods=150)
    half = len(ret_win) // 2
    corr_f = ret_win.iloc[:half].corr(min_periods=60)
    corr_b = ret_win.iloc[half:].corr(min_periods=60)
    corr60 = ret_win.tail(60).corr(min_periods=40)
    rows = []
    for a, b in combinations(ret_win.columns, 2):
        r250 = corr250.loc[a, b]
        r60 = corr60.loc[a, b]
        if pd.isna(r250) or pd.isna(r60):
            continue
        rf, rb = corr_f.loc[a, b], corr_b.loc[a, b]
        if pd.isna(rf) or pd.isna(rb):
            continue
        if r250 < RHO_250_THR and r60 < 0.0 and rf < 0.0 and rb < 0.0:
            rows.append({"a": a, "b": b, "rho_250d": float(r250),
                         "rho_front": float(rf), "rho_back": float(rb),
                         "rho_60d": float(r60)})
    return rows


def estimate_pair_whitelist(ret_win: pd.DataFrame) -> list:
    """白名单配对列表 [(a, b), ...]（a<b 列序；条件见 _whitelist_rows）。"""
    return [(r["a"], r["b"]) for r in _whitelist_rows(ret_win)]


def monthly_pair_whitelists(ret: pd.DataFrame) -> dict:
    """{生效月: [(a,b), ...]}，只收录重估窗已满 250 日的月份（此前为 warmup）。

    每月末（= 该月最后一个有收益数据的交易日）用截至当日 tail(250) 重估、次月全月生效
    （R1 rotation.monthly_whitelists 同款遍历）。
    """
    out = {}
    for m in sorted({d[:7] for d in ret.index}):
        prev = [d for d in ret.index if d[:7] < m]
        if not prev:
            continue
        win = ret.loc[: prev[-1]].tail(MIN_HIST)
        if len(win) >= MIN_HIST:
            out[m] = estimate_pair_whitelist(win)
    return out


def stable_pairs_current(ret: pd.DataFrame, asof_month: str = None) -> dict:
    """当期生效稳定配对清单（月度重估规则实现，供批次 6 证据字段复用）。

    生效月 = asof_month 或数据末日所在月；其白名单 = 截至上月末 250 日窗重估。
    返回 {"effective_month", "estimated_at", "window_days", "pairs": [(a,b)...],
    "detail": [...]}；窗口不满 250 日 → None。
    """
    m = asof_month or ret.index[-1][:7]
    prev = [d for d in ret.index if d[:7] < m]
    if not prev:
        return None
    win = ret.loc[: prev[-1]].tail(MIN_HIST)
    if len(win) < MIN_HIST:
        return None
    return {"effective_month": m, "estimated_at": prev[-1], "window_days": len(win),
            "pairs": estimate_pair_whitelist(win), "detail": _whitelist_rows(win)}


# ---------------------------------------------------------------- 触发

def generate_triggers(ret: pd.DataFrame, whitelists: dict) -> list:
    """去重后触发 [Trigger(date, fall, rise)]；双向检查（A 跌买 B / B 跌买 A）。

    条件（严格不等）：fall 当日收益 < −1.0% 且 rise 当日收益 > 0；NaN 不触发。
    同对（有序 fall→rise）每 ISO 周最多 1 次（R1 同款 seen_week）。
    """
    sigs = []
    seen_week = set()
    for t in ret.index:
        wl = whitelists.get(t[:7])
        if not wl:
            continue
        iso = pd.Timestamp(t).isocalendar()
        wk = (iso[0], iso[1])
        for a, b in wl:
            ra, rb = ret.at[t, a], ret.at[t, b]
            if pd.isna(ra) or pd.isna(rb):
                continue
            if ra < TRIGGER_THR and rb > 0.0:
                if (wk, a, b) not in seen_week:
                    seen_week.add((wk, a, b))
                    sigs.append(Trigger(t, a, b))
            if rb < TRIGGER_THR and ra > 0.0:
                if (wk, b, a) not in seen_week:
                    seen_week.add((wk, b, a))
                    sigs.append(Trigger(t, b, a))
    return sigs


# ---------------------------------------------------------------- 底仓（受控 momentum）

def momentum_month_picks(px: pd.DataFrame, months: list) -> dict:
    """{生效月: Top5 代码}——截至上月末 20 日动量（票内 bar 序 close[p−1]/close[p−21]−1，
    月末前不足 21 根 bar 剔除；平值按代码序；不足 5 只现金兜底=列表更短）。

    照 R3 §3.6-10（受控对照腿，非生产 momentum profile）。
    """
    bar_dates = {c: list(px[c].dropna().index) for c in px.columns}
    bar_closes = {c: [float(v) for v in px[c].dropna().values] for c in px.columns}
    panel_days = list(px.index)
    picks = {}
    for m in months:
        prev = [d for d in panel_days if d[:7] < m]
        if not prev:
            continue
        pe = prev[-1]
        scored = []
        for c in px.columns:
            ds, cs = bar_dates[c], bar_closes[c]
            p = bisect_right(ds, pe)
            if p < MOM_WINDOW + 1:
                continue
            scored.append((-(cs[p - 1] / cs[p - 1 - MOM_WINDOW] - 1.0), c))
        scored.sort()
        picks[m] = [c for _neg, c in scored[:TOP_N]]
    return picks


def month_end_set(panel_days: list) -> set:
    """panel 月末集合：下一 panel 日跨月的当日（数据末日不算月末）。"""
    return {d for d, nxt in zip(panel_days, panel_days[1:]) if d[:7] != nxt[:7]}


def build_base_targets(days: list, picks: dict, month_ends: set,
                       months_sorted: list) -> dict:
    """底仓逐日目标 {date: {code: 0.20}}。月末当日收盘切换为下月 picks（14:50 计算+
    收盘成交，见 docstring 消歧 4）；其余日持当月 picks；无 picks 的日为空仓目标。"""
    targets = {}
    for d in days:
        m = d[:7]
        key = m
        if d in month_ends:
            j = months_sorted.index(m)
            if j + 1 < len(months_sorted):
                key = months_sorted[j + 1]
        targets[d] = {c: BASE_WEIGHT for c in picks.get(key, [])}
    return targets


# ---------------------------------------------------------------- 信号腿目标

def apply_hedge_override(base: dict, b_codes: list) -> dict:
    """触发日目标：每个 distinct B 至 30%，其余保底仓权重、Σ>100% 时等比缩，
    现金优先容纳；30%×k>100% 时触发票自身等比缩至合计 100%（docstring 消歧 6）。"""
    bset = set(b_codes)
    others = {c: w for c, w in base.items() if c not in bset}
    s = sum(others.values())
    tw = HEDGE_WEIGHT * len(bset)
    if tw + s > 1.0 + 1e-12:
        room = max(0.0, 1.0 - tw)
        if s > 0.0:
            others = {c: w * room / s for c, w in others.items()}
        bw = HEDGE_WEIGHT if tw <= 1.0 else 1.0 / len(bset)
    else:
        bw = HEDGE_WEIGHT
    out = dict(others)
    for b in bset:
        out[b] = bw
    return out


def build_signal_targets(days: list, base_targets: dict, triggers: list,
                         day_index: dict, exec_shift: int = 0) -> tuple:
    """信号腿逐日目标（exec_shift=0 主口径当日尾盘 / 1 为 T+1 对照组）。

    返回 (targets, by_day)：by_day = {执行日: [Trigger,...]}（诊断用）。
    执行日越出数据末尾的触发丢弃（无交易）。"""
    by_day = {}
    for tg in triggers:
        i = day_index[tg.date] + exec_shift
        if 0 <= i < len(days):
            by_day.setdefault(days[i], []).append(tg)
    targets = {}
    for d in days:
        tgs = by_day.get(d)
        if tgs:
            targets[d] = apply_hedge_override(base_targets[d],
                                              sorted({tg.rise for tg in tgs}))
        else:
            targets[d] = base_targets[d]
    return targets, by_day


# ---------------------------------------------------------------- 组合引擎（双腿同框架）

def run_weight_portfolio(px_close_ffill: pd.DataFrame, has_bar: pd.DataFrame,
                         targets: dict, days: list, *,
                         cost_rate: float = COST_RATE,
                         tail_drag: float = TAIL_DRAG,
                         initial_nav: float = 1.0,
                         nav_tol: float = 1e-9) -> dict:
    """目标权重组合引擎（收盘执行；docstring 消歧 5/8）。

    返回 {"returns", "nav": 日频 Series（含首日 setup，收益记 0）；
    "events": 调仓事件；"turnover_total", "cost_total", "drag_total"}。
    恒等式：cash + Σ shares×last_px = nav 逐日精确成立；先盯市后调仓（反前视）。
    """
    shares = {}
    cash = float(initial_nav)
    last_px = {}
    cur_target = None
    pending = set()
    rets, navs = {}, {}
    events = []
    nav_prev = None

    for d in days:
        row = px_close_ffill.loc[d]
        hb = has_bar.loc[d]
        # 1) 盯市：当日有 bar 的持仓票更新最近收盘
        for c in list(shares):
            if bool(hb.get(c)):
                last_px[c] = float(row[c])
        nav_ref = cash + sum(sh * last_px[c] for c, sh in shares.items())
        # 2) 目标变化 → 刷新 pending（最新目标优先，后到取消先到）
        tgt = targets.get(d, {})
        if tgt != cur_target:
            cur_target = tgt
            for c in set(tgt) | set(shares):
                cur_mv = shares.get(c, 0.0) * last_px.get(c, 0.0)
                want = tgt.get(c, 0.0) * nav_ref
                if abs(want - cur_mv) > nav_tol:
                    pending.add(c)
        # 3) 执行 pending 中当日有 bar 的票（停牌顺延：留在 pending 每日重试）
        turnover = 0.0
        cost_sum = 0.0
        for c in sorted(pending):
            if not bool(hb.get(c)) or not np.isfinite(float(row[c])):
                continue
            px = float(row[c])
            cur_mv = shares.get(c, 0.0) * px
            want = cur_target.get(c, 0.0) * nav_ref
            delta = want - cur_mv
            if abs(delta) <= nav_tol:
                pending.discard(c)
                continue
            fee = cost_rate * abs(delta)
            cash -= delta + fee
            turnover += abs(delta)
            cost_sum += fee
            new_sh = want / px
            if new_sh <= 1e-15:
                shares.pop(c, None)
                last_px.pop(c, None)
            else:
                shares[c] = new_sh
                last_px[c] = px
            pending.discard(c)
        drag = tail_drag * turnover
        cash -= drag
        if turnover > 0.0:
            events.append({"date": d, "turnover": turnover, "cost": cost_sum,
                           "drag": drag})
        # 4) 收盘 NAV 与日收益
        nav = cash + sum(sh * last_px[c] for c, sh in shares.items())
        rets[d] = (nav / nav_prev - 1.0) if nav_prev is not None else 0.0
        navs[d] = nav
        nav_prev = nav

    return {"returns": pd.Series(rets), "nav": pd.Series(navs), "events": events,
            "turnover_total": sum(e["turnover"] for e in events),
            "cost_total": sum(e["cost"] for e in events),
            "drag_total": sum(e["drag"] for e in events)}


# ---------------------------------------------------------------- Gate 裁决（纯函数）

def gate_h0(triggers: list, test_months: list) -> dict:
    """H0 分辨率：月桶（含零月）计数、中位数、判定。"""
    counts = {m: 0 for m in test_months}
    for tg in triggers:
        counts[tg.date[:7]] = counts.get(tg.date[:7], 0) + 1
    vals = [counts[m] for m in test_months]
    med = float(np.median(vals)) if vals else 0.0
    ok = len(triggers) >= GATE_H0_MIN_TRIGGERS and med >= GATE_H0_MIN_MEDIAN
    return {"ok": ok, "n_triggers": len(triggers), "median": med, "counts": counts}


def gate_excess(r_sig: pd.Series, r_base: pd.Series) -> dict:
    """H1/H2/H3 数字：年化超额（算术差）、三段简单收益差、MDD 差。"""
    _, ann_s, mdd_s = metrics(r_sig)
    _, ann_b, mdd_b = metrics(r_base)
    segs_s, segs_b = seg_total(r_sig), seg_total(r_base)
    seg_exc = {k: segs_s[k] - segs_b[k] for k in segs_s if k in segs_b}
    return {"exc_ann": ann_s - ann_b, "ann_sig": ann_s, "ann_base": ann_b,
            "mdd_sig": mdd_s, "mdd_base": mdd_b, "mdd_gap": mdd_s - mdd_b,
            "seg_exc": seg_exc,
            "n_pos_segs": sum(1 for v in seg_exc.values() if v > 0)}


# ---------------------------------------------------------------- 诊断

def trigger_attribution(triggers: list, base_targets: dict, sig_targets: dict,
                        ret_ffill: pd.DataFrame, day_index: dict,
                        days: list) -> dict:
    """分对贡献（线性近似，docstring 消歧 11）：{（fall,rise): {"n", "contrib"}}。"""
    per = {}
    for tg in triggers:
        j = day_index[tg.date] + 1
        if j >= len(days):
            continue
        d, d2 = days[day_index[tg.date]], days[j]
        sig_t, base_t = sig_targets[d], base_targets[d]
        contrib = 0.0
        for c in set(sig_t) | set(base_t):
            dw = sig_t.get(c, 0.0) - base_t.get(c, 0.0)
            if abs(dw) < 1e-12:
                continue
            r = ret_ffill.at[d2, c]
            if pd.isna(r):
                continue
            contrib += dw * float(r)
        agg = per.setdefault((tg.fall, tg.rise), {"n": 0, "contrib": 0.0})
        agg["n"] += 1
        agg["contrib"] += contrib
    return per


def direction_counts(triggers: list) -> dict:
    """两种配对腿方向检出数：{无序对: [n(a跌买b), n(b跌买a)]}（a<b 列序）。"""
    dirs = {}
    for tg in triggers:
        key = tuple(sorted((tg.fall, tg.rise)))
        agg = dirs.setdefault(key, [0, 0])
        agg[0 if tg.fall == key[0] else 1] += 1
    return dirs


# ---------------------------------------------------------------- 主流程

def main() -> int:
    px, names = load_close_panel()
    ret = px.pct_change()
    pxf = px.ffill()
    has_bar = px.notna()
    ret_ffill = pxf.pct_change()
    panel_days = list(px.index)

    whitelists = monthly_pair_whitelists(ret)
    if not whitelists:
        print("FAIL: 无任何月份满足 250 日配对白名单重估窗口")
        return 1
    test_month0 = min(whitelists)
    test_start = test_month0 + "-01"
    test_days = [d for d in panel_days if d >= test_start]
    init_day = max(d for d in panel_days if d < test_start)
    days = [init_day] + test_days
    months_sorted = sorted({d[:7] for d in panel_days})
    test_months = [m for m in months_sorted if m >= test_month0]
    month_ends = month_end_set(panel_days)

    picks = momentum_month_picks(px, test_months)
    base_targets = build_base_targets(days, picks, month_ends, months_sorted)
    triggers = generate_triggers(ret, whitelists)
    day_index = {d: i for i, d in enumerate(days)}
    sig_targets, _ = build_signal_targets(days, base_targets, triggers, day_index, 0)
    t1_targets, _ = build_signal_targets(days, base_targets, triggers, day_index, 1)

    print(f"数据: {panel_days[0]} ~ {panel_days[-1]}（{len(panel_days)} 个 panel 交易日，"
          f"core {len(px.columns)} 票）")
    print(f"测试窗: {test_start} 起（首个满足 250 日白名单重估窗的月份），"
          f"{test_days[0]} ~ {test_days[-1]}（{len(test_days)} 交易日，"
          f"{len(test_months)} 个自然月）")
    wl_months = [(m, wl) for m, wl in sorted(whitelists.items()) if wl]
    print(f"白名单: {len(wl_months)}/{len(whitelists)} 个月非空")
    for m, wl in wl_months:
        print(f"  {m}: {['%s(%s)→%s(%s)' % (a, names.get(a, a), b, names.get(b, b)) for a, b in wl]}")

    # ---- 腿回测（主口径 tail_drag=0.3%）----
    base_res = run_weight_portfolio(pxf, has_bar, base_targets, days)
    sig_res = run_weight_portfolio(pxf, has_bar, sig_targets, days)
    t1_res = run_weight_portfolio(pxf, has_bar, t1_targets, days)
    r_base = base_res["returns"].loc[lambda s: s.index >= test_start]
    r_sig = sig_res["returns"].loc[lambda s: s.index >= test_start]
    r_t1 = t1_res["returns"].loc[lambda s: s.index >= test_start]

    print(f"\n== 腿指标（测试窗内，净口径：双边 0.15% + 尾盘扣减 0.3%）==")
    print(f"{'腿':<12}{'累计':>10}{'年化':>9}{'MDD':>8}{'调仓次数':>7}{'换手合计':>9}")
    for label, res, r in [("MOM(基线)", base_res, r_base),
                          ("STRAT(信号)", sig_res, r_sig),
                          ("T+1(对照)", t1_res, r_t1)]:
        total, ann, mdd = metrics(r)
        print(f"{label:<12}{total:>10.2%}{ann:>9.2%}{mdd:>8.2%}"
              f"{len(res['events']):>7}{res['turnover_total']:>9.2f}")

    # ---- Gate H0（分辨率，先行短路）----
    h0 = gate_h0(triggers, test_months)
    print(f"\n== Gate H0 分辨率 ==")
    print(f"  去重后触发 {h0['n_triggers']}（>=60? "
          f"{'PASS' if h0['n_triggers'] >= GATE_H0_MIN_TRIGGERS else 'FAIL'}）；"
          f"月桶中位（含零月 {len(test_months)} 个月）{h0['median']:.1f}"
          f"（>=3? {'PASS' if h0['median'] >= GATE_H0_MIN_MEDIAN else 'FAIL'}）")
    nz = {m: c for m, c in h0["counts"].items() if c}
    print(f"  非零月桶: {nz}")

    gates = {"H0": h0["ok"]}
    if not h0["ok"]:
        print("  → 判「无分辨率」直接 FAIL，不做超额归因（H1~H3 未裁决）")
    else:
        ex = gate_excess(r_sig, r_base)
        print(f"\n== Gate H1~H3（对照受控 momentum 基线腿）==")
        c1 = ex["exc_ann"] >= GATE_H1_MIN_EXCESS
        gates["H1"] = c1
        print(f"  H1 扣成本年化超额 {ex['exc_ann']:+.2%}"
              f"（信号 {ex['ann_sig']:+.2%} − 基线 {ex['ann_base']:+.2%}；"
              f">= +5%? {'PASS' if c1 else 'FAIL'}）")
        for k, v in ex["seg_exc"].items():
            print(f"     段 {k}: 超额 {v:+.2%}")
        c2 = ex["n_pos_segs"] >= GATE_H2_MIN_POS_SEGS
        gates["H2"] = c2
        print(f"  H2 三段为正 {ex['n_pos_segs']}/3（>=2? "
              f"{'PASS' if c2 else 'FAIL'}）")
        c3 = ex["mdd_gap"] <= GATE_H3_MAX_MDD_GAP
        gates["H3"] = c3
        print(f"  H3 MDD 差 {ex['mdd_gap']:+.2%}（信号 {ex['mdd_sig']:.2%} − "
              f"基线 {ex['mdd_base']:.2%}；劣于基线 <=2pp? "
              f"{'PASS' if c3 else 'FAIL'}）")

    # ---- 诊断（非 gate，如实披露）----
    print(f"\n== 诊断（非 gate）==")
    dirs = direction_counts(triggers)
    n_first = sum(v[0] for v in dirs.values())
    n_second = sum(v[1] for v in dirs.values())
    print(f"  两种配对腿方向: 无序对标注（代码小者=a）a跌买b {n_first} 次 / "
          f"b跌买a {n_second} 次")
    for key, v in sorted(dirs.items()):
        print(f"    {key[0]}({names.get(key[0], key[0])})↔{key[1]}"
              f"({names.get(key[1], key[1])}): a跌买b {v[0]} / b跌买a {v[1]}")
    n_b_in_base = sum(1 for tg in triggers if tg.rise in base_targets[tg.date])
    print(f"  触发时 B 已在底仓: {n_b_in_base}/{len(triggers)}；"
          f"B 不在底仓（买入挤现金/等比缩）: {len(triggers) - n_b_in_base}")

    attrib = trigger_attribution(triggers, base_targets, sig_targets,
                                 ret_ffill, day_index, days)
    print(f"  分对贡献（线性近似，未扣成本）: 合计 "
          f"{sum(v['contrib'] for v in attrib.values()):+.4%}")
    for key, v in sorted(attrib.items(), key=lambda kv: -kv[1]["contrib"]):
        print(f"    {key[0]}({names.get(key[0], key[0])})跌→买{key[1]}"
              f"({names.get(key[1], key[1])}): n={v['n']} 贡献 {v['contrib']:+.4%}")

    ex_t1 = gate_excess(r_t1, r_base)
    print(f"  T+1 对照组（同信号改次日执行）: 年化超额 {ex_t1['exc_ann']:+.2%}"
          f"（主口径 {gate_excess(r_sig, r_base)['exc_ann']:+.2%}；预期消失 = 跷跷板"
          f"同日性机制验证）")
    for k, v in ex_t1["seg_exc"].items():
        print(f"     段 {k}: T+1 超额 {v:+.2%}")

    print(f"  尾盘扣减三档敏感性（只披露）:")
    for drag in TAIL_DRAG_SENS:
        b = run_weight_portfolio(pxf, has_bar, base_targets, days, tail_drag=drag)
        s = run_weight_portfolio(pxf, has_bar, sig_targets, days, tail_drag=drag)
        rb = b["returns"].loc[lambda x: x.index >= test_start]
        rs = s["returns"].loc[lambda x: x.index >= test_start]
        _, ann_s, _ = metrics(rs)
        _, ann_b, _ = metrics(rb)
        print(f"    drag={drag:.1%}: 信号年化 {ann_s:+.2%} 基线年化 {ann_b:+.2%} "
              f"年化超额 {ann_s - ann_b:+.2%}")

    print(f"  成本披露: 基线 佣金 {base_res['cost_total']:.4f} 扣减 "
          f"{base_res['drag_total']:.4f}；信号 佣金 {sig_res['cost_total']:.4f} "
          f"扣减 {sig_res['drag_total']:.4f}")

    # ---- 当期稳定配对清单（无论 gate 结果必产出）----
    cur = stable_pairs_current(ret)
    print(f"\n== 当期稳定配对清单（生效月 {cur['effective_month']}，"
          f"截至 {cur['estimated_at']} 250 日窗重估）==")
    if cur["pairs"]:
        for row in cur["detail"]:
            print(f"  {row['a']}({names.get(row['a'], row['a'])}) ↔ "
                  f"{row['b']}({names.get(row['b'], row['b'])}): "
                  f"ρ250={row['rho_250d']:+.3f} 前{row['rho_front']:+.3f} "
                  f"后{row['rho_back']:+.3f} ρ60={row['rho_60d']:+.3f}")
    else:
        print("  （空）")
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(
        {"effective_month": cur["effective_month"], "estimated_at": cur["estimated_at"],
         "window_days": cur["window_days"], "generated_by": "signals/hedge_pair_research.py",
         "pairs": [{"a": r["a"], "b": r["b"], "name_a": names.get(r["a"], r["a"]),
                    "name_b": names.get(r["b"], r["b"]), "rho_250d": r["rho_250d"],
                    "rho_front": r["rho_front"], "rho_back": r["rho_back"],
                    "rho_60d": r["rho_60d"]} for r in cur["detail"]]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  落盘: {REPORT_JSON}")

    ok = all(gates.values())
    print(f"\nGate: {gates}")
    print(f"VERDICT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
