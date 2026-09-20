"""宏观比值（铜油比/油金比）周期先验 —— M1 研究版（gate 批，先定后跑）。

施工方案：docs/概念轮动与周期因子施工方案-2026-09-19.md §3.M1。
纯离线：akshare 拉三源（不写库）+ 只读 market.db（concept_returns 内部 mode=ro）。
运行：.venv/bin/python3 -m signals.macro_ratio_research
退出码（2026-09-21 三态出口，D-3 增补）：0=pass，1=fail（有分辨率但判据为负），
3=INSUFFICIENT-DATA（Gate0 有效事件不足——无分辨率未裁决，挂起，不进 PASS/FAIL
二元；检验① |Spearman|>=0.2 与检验② |corr|>0.7 判据数字零改动）。

预注册口径（跑后不许挪）：
- 比值：铜油比 = 沪铜主力CU0收盘 / 布伦特OIL收盘；油金比 = 布伦特OIL / 黄金收盘。
  黄金优先 COMEX GC（与油同美元计价），不可得则上海金 SGE（CNY/g，含汇率漂移 caveat）。
  铜以 CNY 计价含 USDCNY 漂移 caveat（M2 若落地可换 LME 美元铜复核）。
  绝对水平禁用，一律 60/250 日滚动分位（防供给冲击漂移与换月跳空）。
- 检验①（分辨率）：比值 250 日滚动分位四分桶 × 未来 20/60 日收益。
  组合：成长 = 半导体+AI+新能源+苹果+政策军工 组收益等权；
       防御 = 消费+生物医药+厄尔尼诺 等权；另列 HS300 参考。
  预注册 pass 线：>=1 个比值满足 |Spearman(分位桶, 成长-防御未来20日价差)| >= 0.2
  且 60 日 horizon 同号。
- 检验②（共线性，硬闸）：max(|corr(比值 20/60 日变动, HS300 20/60 日动量)|) > 0.7
  → 判冗余，fail 不落地（铜与大盘周期共线是主要死因风险）。
- 附表：铜油比×油金比 分位中位数二分交叉四象限 × 未来20日收益（M2 映射参考，非 gate）。
  已知 caveat：fwd 窗口重叠 + 分位慢变 → 事件自相关，Spearman 显著性偏高，仅作 screen。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from signals.rotation import (INSUFFICIENT_DATA, EXIT_INSUFFICIENT,  # noqa: E402
                              concept_returns)

BASE = Path(__file__).resolve().parent.parent

GROWTH = ["半导体", "AI", "新能源", "苹果", "政策军工"]
DEFENSE = ["消费", "生物医药", "厄尔尼诺"]
GATE_COLLINEAR = 0.7
GATE_SPEARMAN = 0.2

# ---- Gate0 分辨率前置（三态出口；2026-09-21 修复批 D-3 增补，不在 2026-09-19 预注册内）----
# 依据：docs/多agent全库审查报告-2026-09-21.md P0-C + docs/修复施工方案-2026-09-21.md
# D-3。原 M1 预注册无样本量下限——三源交集/有效事件不足时 250 日滚动分位无从计算或
# 仅由窗口边沿决定，属"测不了"而非"FAIL"。有效事件数低于下限 → 结论 INSUFFICIENT-DATA
# （无分辨率未裁决，挂起并登记数据条件），退出码 3；检验①/② 判据数字零改动。
# 下限取保守值 250（≈一个完整 250 日滚动分位窗的日事件量），【待预注册复核】。
GATE0_MIN_EVENTS = 250      # 单比值单 horizon 有效事件数下限（保守值，待预注册复核）


def gate0_resolution(events: dict) -> dict:
    """Gate0（M1 版，2026-09-21 D-3 增补）：events = {比值名: {"n20": n, "n60": n}}。

    任一比值在 20/60 两个 horizon 的有效事件数均达 GATE0_MIN_EVENTS 才可进检验①
    PASS/FAIL 裁决；否则 ok=False → INSUFFICIENT-DATA / 退出码 3（挂起）。只做挂起
    判定，不改任何预注册判据。"""
    per = {name: (v.get("n20", 0) >= GATE0_MIN_EVENTS
                  and v.get("n60", 0) >= GATE0_MIN_EVENTS)
           for name, v in events.items()}
    return {"ok": any(per.values()), "per_ratio": per, "min_events": GATE0_MIN_EVENTS}


def _fetch_cu() -> pd.Series:
    import akshare as ak
    df = ak.futures_main_sina(symbol="CU0", start_date="20100101",
                              end_date=date.today().strftime("%Y%m%d"))
    s = pd.Series(pd.to_numeric(df["收盘价"], errors="coerce").values,
                  index=pd.to_datetime(df["日期"]).dt.strftime("%Y-%m-%d"))
    return s.dropna().sort_index()


def _fetch_foreign(symbol: str) -> pd.Series:
    import akshare as ak
    df = ak.futures_foreign_hist(symbol=symbol)
    s = pd.Series(pd.to_numeric(df["close"], errors="coerce").values,
                  index=pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d"))
    return s.dropna().sort_index()


def _fetch_au() -> pd.Series:
    """黄金：优先 COMEX GC（美元），失败回退上海金 SGE（CNY）。"""
    try:
        s = _fetch_foreign("GC")
        print(f"黄金源: COMEX GC（美元计价），{s.index[0]}~{s.index[-1]}，{len(s)} 行")
        return s
    except Exception as e:
        print(f"COMEX GC 不可得（{repr(e)[:80]}），回退上海金 SGE（CNY 计价 caveat）")
        import akshare as ak
        df = ak.spot_hist_sge()
        s = pd.Series(pd.to_numeric(df["close"], errors="coerce").values,
                      index=pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d"))
        return s.dropna().sort_index()


def _hs300_close() -> pd.Series:
    import sqlite3
    conn = sqlite3.connect(f"file:{BASE / 'data' / 'market.db'}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT trade_date, close FROM index_daily WHERE index_code='000300'"
            " ORDER BY trade_date").fetchall()
    finally:
        conn.close()
    return pd.Series({d: c for d, c in rows}, dtype=float).sort_index()


def roll_pct(s: pd.Series, w: int) -> pd.Series:
    """滚动窗口内末值分位（0~1）。"""
    return s.rolling(w).apply(lambda x: (x <= x[-1]).mean(), raw=True)


def fwd_ret(c: pd.Series, n: int) -> pd.Series:
    """t 之后 n 日累计收益（t+1..t+n），log 域。"""
    return np.expm1(np.log1p(c).rolling(n).sum().shift(-n))


def main() -> int:
    print("== 拉取商品数据（akshare，不写库）==")
    cu = _fetch_cu()
    oil = _fetch_foreign("OIL")
    au = _fetch_au()
    print(f"铜 CU0: {cu.index[0]}~{cu.index[-1]} {len(cu)} 行 | "
          f"油 OIL: {oil.index[0]}~{oil.index[-1]} {len(oil)} 行")

    j = pd.concat({"cu": cu, "oil": oil, "au": au}, axis=1, join="inner").sort_index()
    ratios = {"铜油比": j["cu"] / j["oil"], "油金比": j["oil"] / j["au"]}
    print(f"三源交集: {len(j)} 天（{j.index[0]}~{j.index[-1]}）")

    hs300 = _hs300_close()
    cret = concept_returns()
    growth = cret[GROWTH].mean(axis=1)
    defense = cret[DEFENSE].mean(axis=1)

    print("\n== 检验② 共线性（硬闸: |corr|>0.7 判冗余 fail）==")
    max_abs = 0.0
    for rname, series in ratios.items():
        for n in (20, 60):
            rchg = series.pct_change(n)
            hs = hs300.pct_change(n)
            both = pd.concat([rchg, hs], axis=1, join="inner").dropna()
            c = float(both.corr().iloc[0, 1]) if len(both) > 30 else float("nan")
            max_abs = max(max_abs, abs(c)) if pd.notna(c) else max_abs
            print(f"  {rname} {n}日变动 vs HS300 {n}日动量: corr={c:+.3f}（n={len(both)}）")
    c2_ok = max_abs <= GATE_COLLINEAR
    print(f"  → 共线性闸: max|corr|={max_abs:.3f} "
          f"{'PASS' if c2_ok else 'FAIL（冗余，不落地）'}")

    print("\n== 检验① 分辨率（比值 250 日滚动分位四分桶 × 未来收益）==")
    spear = {}
    ev_counts = {}   # Gate0：每比值每 horizon 的有效事件数（D-3 增补）
    for rname, series in ratios.items():
        pct = roll_pct(series, 250).reindex(cret.index, method="ffill")
        for n in (20, 60):
            ev = pd.DataFrame({
                "pct": pct,
                "g": fwd_ret(growth, n),
                "d": fwd_ret(defense, n),
            }).dropna()
            ev_counts.setdefault(rname, {})[f"n{n}"] = int(len(ev))
            ev["bucket"] = pd.cut(ev["pct"], [0, 0.25, 0.5, 0.75, 1.0], labels=False)
            ev["spread"] = ev["g"] - ev["d"]
            gmean = ev.groupby("bucket")["spread"].mean()
            # Spearman = 秩 Pearson（不引入 scipy 依赖）
            rho = float(ev["bucket"].rank().corr(ev["spread"].rank()))
            spear[(rname, n)] = rho
            print(f"  {rname} fwd{n}d 成长-防御价差 分桶均值: "
                  + " ".join(f"Q{int(b)}={v:+.2%}" for b, v in gmean.items())
                  + f"  Spearman={rho:+.2f}")
    c1_ok = False
    for rname in ratios:
        r20 = spear.get((rname, 20), float("nan"))
        r60 = spear.get((rname, 60), float("nan"))
        if pd.notna(r20) and pd.notna(r60) and abs(r20) >= GATE_SPEARMAN \
                and np.sign(r20) == np.sign(r60):
            c1_ok = True
            print(f"  → {rname} 达预注册 pass 线（|ρ20|>=0.2 且 60d 同号）")
    if not c1_ok:
        print("  → 无比值达预注册 pass 线")

    print("\n== 附表: 四象限 × 未来20日收益（M2 映射参考，非 gate）==")
    q = pd.DataFrame({
        "cu_oil": roll_pct(ratios["铜油比"], 250).reindex(cret.index, method="ffill"),
        "oil_au": roll_pct(ratios["油金比"], 250).reindex(cret.index, method="ffill"),
        "g": fwd_ret(growth, 20),
        "d": fwd_ret(defense, 20),
    }).dropna()
    for cu_hi in (True, False):
        for au_hi in (True, False):
            m = ((q["cu_oil"] > 0.5) == cu_hi) & ((q["oil_au"] > 0.5) == au_hi)
            if m.sum() < 10:
                continue
            tag = ("铜油比" + ("高" if cu_hi else "低")) + "/" + \
                  ("油金比" + ("高" if au_hi else "低"))
            print(f"  {tag:<18} n={int(m.sum()):>4}  成长 {q.loc[m,'g'].mean():+.2%}  "
                  f"防御 {q.loc[m,'d'].mean():+.2%}  "
                  f"价差 {q.loc[m,'g'].mean() - q.loc[m,'d'].mean():+.2%}")

    print("\n== Gate 汇总 ==")
    # ---- Gate0 分辨率前置（2026-09-21 D-3 增补；未过 → 挂起，不进 PASS/FAIL 二元）----
    g0 = gate0_resolution(ev_counts)
    print(f"  Gate0 分辨率前置（有效事件/比值/horizon >= {GATE0_MIN_EVENTS}）: "
          + ("PASS" if g0["ok"] else
             f"未过 {ev_counts} → INSUFFICIENT-DATA（挂起）"))
    print(f"  检验① 分辨率: {'PASS' if c1_ok else 'FAIL'}")
    print(f"  检验② 共线性: {'PASS' if c2_ok else 'FAIL'}")
    if not g0["ok"]:
        print(f"\nVERDICT: {INSUFFICIENT_DATA}（无分辨率未裁决，挂起——登记数据条件："
              f"三源交集/有效事件需每比值每 horizon >= {GATE0_MIN_EVENTS}；"
              f"检验①/② 判据数字零改动）")
        return EXIT_INSUFFICIENT
    ok = c1_ok and c2_ok
    print(f"\nVERDICT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
