"""概念轮动（跷跷板）信号 —— R1 研究版（gate 批，先定后跑）。

施工方案：docs/概念轮动与周期因子施工方案-2026-09-19.md §3.R1。
纯离线研究：只读 market.db（uri mode=ro），不写任何库表；本脚本 gate pass
才允许进 R2 生产化，fail 即归档结题。运行：.venv/bin/python3 -m signals.rotation
退出码：0=pass，1=fail（供未来 CI 挂门）。

预注册口径（与 2026-09-19 实测一致，跑后不许挪）：
- 概念日收益：组内 close_qfq 日收益等权平均；当日组内 >=6 只有数据才计，否则 NaN。
- 白名单：每月末用截至当日的 250 日窗重估一次、次月全月生效；入选需
  ρ_250d < -0.15 且 前后半窗（各125日）ρ 同为负 且 ρ_60d < -0.20
  （红线：重估频率不得高于每月，见施工方案 §4.2）。
- 触发：A 组日收益 < -0.8% → (A,B) 于 T+1..T+3 为接力窗口；同对每 ISO 周最多
  触发 1 次；窗口重叠时最新触发者优先。
- 回测：默认腿 = 动量基线（每月换持"截至上月末 20 日动量最强"概念组）；
  信号窗口改持 B 组；每次换腿扣 0.1%（进/出各一次 = 双边 0.2%）。
  窗口内 B 组收益缺失按 0 计（罕见，不额外计费）。
- Pass（三条件同时满足，均对照动量基线③）：
  ① 扣成本年化超额 >= +5%；
  ② 2025H1 / 2025H2 / 2026 三段超额 >=2 段为正（段内简单收益差）；
  ③ 信号腿 MDD 劣于基线不超过 2pp。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
DB_PATH = BASE / "data" / "market.db"
CONFIG_PATH = BASE / "config.json"

TRIGGER_THR = -0.008        # A 组单日深跌阈值
WINDOW_DAYS = 3             # 接力窗口交易日数
RHO_250_THR = -0.15         # 白名单 250 日相关阈值
RHO_60_THR = -0.20          # 白名单 60 日相关阈值
MIN_HIST = 250              # 白名单重估所需最少历史
MIN_MEMBERS = 6             # 概念日收益最少有效成员数
COST_PER_SWITCH = 0.001     # 每次换腿 0.1%
SEG_DEFS = {
    "2025H1": ("2025-01-01", "2025-06-30"),
    "2025H2": ("2025-07-01", "2025-12-31"),
    "2026": ("2026-01-01", "2026-12-31"),
}


def _connect_ro() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def load_groups() -> dict:
    """config.json watchlist 首概念标签 → 组成员代码。"""
    cfg = json.loads(CONFIG_PATH.read_text())
    members = {}
    for e in cfg["watchlist"]:
        cs = e.get("concepts") or []
        if cs:
            members.setdefault(cs[0], []).append(e["code"])
    return members


def concept_returns() -> pd.DataFrame:
    """概念日收益（等权、组内 >=6 只有数据才计）。M1 复用本函数保证同口径。"""
    members = load_groups()
    conn = _connect_ro()
    try:
        df = pd.read_sql_query(
            "SELECT code, trade_date, close_qfq FROM daily_bar ORDER BY trade_date", conn)
    finally:
        conn.close()
    px = df.pivot(index="trade_date", columns="code", values="close_qfq").sort_index()
    ret = px.pct_change()
    cret = pd.DataFrame(index=ret.index)
    for g, codes in members.items():
        sub = ret[codes]
        cret[g] = sub.mean(axis=1).where(sub.notna().sum(axis=1) >= MIN_MEMBERS)
    return cret.dropna(how="all")


def estimate_whitelist(win: pd.DataFrame) -> list:
    """对给定窗口按三项预注册标准输出白名单对（不足 250 日返回空）。"""
    if len(win) < MIN_HIST:
        return []
    corr250 = win.corr(min_periods=150)
    half = len(win) // 2
    corr_f = win.iloc[:half].corr(min_periods=60)
    corr_b = win.iloc[half:].corr(min_periods=60)
    corr60 = win.tail(60).corr(min_periods=40)
    out = []
    for a, b in combinations(win.columns, 2):
        r250 = corr250.loc[a, b]
        r60 = corr60.loc[a, b]
        if pd.isna(r250) or pd.isna(r60):
            continue
        rf, rb = corr_f.loc[a, b], corr_b.loc[a, b]
        if r250 < RHO_250_THR and r60 < RHO_60_THR and rf < 0 and rb < 0:
            out.append((a, b))
    return out


def monthly_whitelists(cret: pd.DataFrame) -> dict:
    """{生效月: 白名单}，只收录窗口已满 250 日的月份（此前为 warmup，不入测试）。"""
    out = {}
    for m in sorted({d[:7] for d in cret.index}):
        prev = [d for d in cret.index if d[:7] < m]
        if not prev:
            continue
        win = cret.loc[: prev[-1]].tail(MIN_HIST)
        if len(win) >= MIN_HIST:
            out[m] = estimate_whitelist(win)
    return out


def generate_signals(cret: pd.DataFrame, whitelists: dict) -> list:
    """[(trigger_date, trigger组, 目标组)]；同对每 ISO 周最多一次。"""
    sigs = []
    seen_week = set()
    for t in cret.index:
        wl = whitelists.get(t[:7], [])
        if not wl:
            continue
        iso = pd.Timestamp(t).isocalendar()
        wkey = (iso[0], iso[1])
        for a, b in wl:
            r = cret.at[t, a]
            if pd.isna(r) or r >= TRIGGER_THR:
                continue
            if (wkey, a, b) in seen_week:
                continue
            seen_week.add((wkey, a, b))
            sigs.append((t, a, b))
    return sigs


def momentum_picks(cret: pd.DataFrame) -> dict:
    """{生效月: 默认持有概念组}，按截至上月末 20 日动量最强（全 NaN 则当月持币）。"""
    mom20 = (1 + cret).rolling(20).apply(np.prod, raw=True) - 1
    picks = {}
    for m in sorted({d[:7] for d in cret.index}):
        prev = [d for d in cret.index if d[:7] < m]
        if not prev:
            continue
        row = mom20.loc[prev[-1]]
        picks[m] = None if row.isna().all() else row.idxmax()
    return picks


def build_desired(cret: pd.DataFrame, picks: dict, sigs: list, test_start: str,
                  mode: str) -> dict:
    """每日目标腿。mode: cash / equal / mom / strat。信号窗口最新触发者优先。"""
    cover = {}
    if mode == "strat":
        for t0, _a, b in sigs:
            pos = cret.index.get_loc(t0)
            for i in range(1, WINDOW_DAYS + 1):
                if pos + i < len(cret.index):
                    cover[cret.index[pos + i]] = b  # 后触发覆盖先触发
    desired = {}
    for d in cret.index:
        if d < test_start:
            continue
        if mode == "cash":
            desired[d] = None
        elif mode == "equal":
            desired[d] = "EQUAL"
        elif mode == "mom":
            desired[d] = picks.get(d[:7])
        else:
            desired[d] = cover.get(d, picks.get(d[:7]))
    return desired


def leg_returns(cret: pd.DataFrame, desired: dict, equal_ret: pd.Series) -> tuple:
    """每日净收益（换腿扣 0.1%）。返回 (日收益Series, 换腿次数)。"""
    rets = {}
    switches = 0
    prev = None
    for d in sorted(desired):
        asset = desired[d]
        if asset is None:
            r = 0.0
        elif asset == "EQUAL":
            r = float(equal_ret.get(d, 0.0))
            if pd.isna(r):
                r = 0.0
        else:
            r = cret.at[d, asset]
            r = 0.0 if pd.isna(r) else float(r)
        if prev is not None and desired[d] != prev:
            r -= COST_PER_SWITCH
            switches += 1
        rets[d] = r
        prev = desired[d]
    return pd.Series(rets).sort_index(), switches


def metrics(r: pd.Series) -> tuple:
    """(累计收益, 年化, 正值MDD)。"""
    total = float((1 + r).prod() - 1)
    n = len(r)
    ann = (1 + total) ** (252 / n) - 1 if n else 0.0
    eq = (1 + r).cumprod()
    mdd = float((1 - eq / eq.cummax()).max())
    return total, ann, mdd


def seg_total(r: pd.Series) -> dict:
    out = {}
    for name, (s, e) in SEG_DEFS.items():
        sub = r[(r.index >= s) & (r.index <= e)]
        if len(sub):
            out[name] = float((1 + sub).prod() - 1)
    return out


def main() -> int:
    cret = concept_returns()
    whitelists = monthly_whitelists(cret)
    if not whitelists:
        print("FAIL: 无任何月份满足 250 日白名单重估窗口")
        return 1
    test_start = min(whitelists) + "-01"
    picks = momentum_picks(cret)
    sigs = generate_signals(cret, whitelists)
    equal_ret = cret.mean(axis=1)

    print(f"数据: {cret.index[0]} ~ {cret.index[-1]}（{len(cret)} 交易日）")
    print(f"测试窗: {test_start} 起（首个满足 250 日白名单窗口的月份）")
    wl_months = [(m, wl) for m, wl in sorted(whitelists.items()) if wl]
    print(f"白名单: {len(wl_months)}/{len(whitelists)} 个月非空")
    for m, wl in wl_months:
        print(f"  {m}: {['%s→%s' % p for p in wl]}")
    print(f"信号数: {len(sigs)}")
    for t0, a, b in sigs:
        print(f"  {t0}  {a} 跌 → {b}")

    legs = {}
    info = {}
    for mode, label in [("cash", "CASH"), ("equal", "EQUAL(基线②)"),
                        ("mom", "MOM(基线③)"), ("strat", "STRAT(信号腿)")]:
        desired = build_desired(cret, picks, sigs, test_start, mode)
        r, sw = leg_returns(cret, desired, equal_ret)
        legs[label] = r
        info[label] = (metrics(r), sw)

    print("\n== 全期指标（测试窗内，净口径）==")
    print(f"{'腿':<14}{'累计':>9}{'年化':>9}{'MDD':>8}{'换腿':>6}")
    for label in ["CASH", "EQUAL(基线②)", "MOM(基线③)", "STRAT(信号腿)"]:
        (total, ann, mdd), sw = info[label]
        print(f"{label:<14}{total:>8.1%}{ann:>8.1%}{mdd:>7.1%}{sw:>6}")

    (ts, as_, mds), _ = info["STRAT(信号腿)"]
    (tm, am, mdm), _ = info["MOM(基线③)"]
    exc_ann = as_ - am
    segs = seg_total(legs["STRAT(信号腿)"])
    segm = seg_total(legs["MOM(基线③)"])
    seg_exc = {k: segs[k] - segm[k] for k in segs if k in segm}
    n_pos = sum(1 for v in seg_exc.values() if v > 0)
    mdd_gap = mds - mdm

    print("\n== Gate（对照 MOM 基线③，三项须全过）==")
    c1 = exc_ann >= 0.05
    print(f"  ① 年化超额 {exc_ann:+.2%}  （>= +5%? {'PASS' if c1 else 'FAIL'}）")
    for k, v in seg_exc.items():
        print(f"     段 {k}: 超额 {v:+.2%}")
    c2 = n_pos >= 2
    print(f"  ② 三段为正 {n_pos}/3   （>=2? {'PASS' if c2 else 'FAIL'}）")
    c3 = mdd_gap <= 0.02
    print(f"  ③ MDD 差 {mdd_gap:+.2%}  （劣于基线 <=2pp? {'PASS' if c3 else 'FAIL'}）")

    ok = c1 and c2 and c3
    print(f"\nVERDICT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
