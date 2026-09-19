"""X 批次解冻重测 —— X1 尾盘口径轴 / X2 行业粒度轴（gate 批，单变量原则，判据照抄 R1）。

施工方案：docs/全量打包施工方案-2026-09-20.md §3.4（预注册原文逐字抄录于下）；
R1 原文：docs/概念轮动与周期因子施工方案-2026-09-19.md §3.R1（X 冻结条款见其 §3.X，
2026-09-20 全量打包批用户裁决解冻）。market.db 一律 uri mode=ro 只读，不写任何库表；
**不改 signals/rotation.py（R1 冻结脚本）**，白名单/组收益/触发信号/动量选组逻辑全部
import 复用。X2 申万映射经 akshare 在线取、只进内存（不落库、不 Mock；失败即阻断上报）。
运行：.venv/bin/python3 -m signals.rotation_x
退出码：0 = X1/X2 两轴全过；1 = 任一轴 fail（含 X2 申万映射在线取数被阻断——阻断即
如实上报并停止该轴，不换非申万源替代、不用硬编码映射）。

【§3.4 预注册原文（逐字）】
- X1 尾盘口径轴：R1 白名单规则（ρ_250d < -0.15 且前后半窗同负 且 ρ_60d < -0.20、月度
  重估）与触发阈值（A 组日收益 < -0.8%、同对每 ISO 周最多 1 次）逐字不变；唯一变更 =
  执行口径：触发当日尾盘收盘买入 B 组、T+1 尾盘收盘回落（原 R1 为 T+1..T+3 接力窗口）；
  成本换腿 0.1% + 尾盘扣减 0.3%（同 3.2 主口径）；T+2/T+3 持有变体只披露不作 gate。
- X2 行业粒度轴：白名单规则与执行口径（T+1..T+3 接力）与 R1 逐字相同；唯一变更 =
  组成员从概念组换为申万一级行业（映射经 akshare 在线取、只进内存、失败即阻断上报）；
  组日收益等权、组内 ≥6 只有数据才计（照 R1 MIN_MEMBERS）。
- Gate（两轴各自独立判定，判据与 R1 rotation.py 三件套逐字相同）：① 扣成本年化超额
  ≥ +5%；② 三段 ≥2 正；③ MDD 劣化 ≤2pp（均对照各自口径的 momentum 基线组）。fail 即归档
  该轴；禁止调参。
- 测试窗与 warmup 沿用 R1 口径（首个满足 250 日重估窗的月份起）。

【实现口径钉死（跑后不许挪）】
- X1 执行时序：触发日 t0 观测到 A 组深跌后于当日尾盘收盘价买入 B（建仓 close(t0)）、
  T+1 尾盘收盘回落（平仓 close(t0+1)）——即 B 组日收益只计入 t0+1 一日
  （close(t0)→close(t0+1)）；T+2/T+3 持有变体 = B 分别计入 t0+1..t0+2 / t0+1..t0+3
  （只披露不作 gate）。R1 对照口径 = B 计入 t0+1..t0+3（rotation.WINDOW_DAYS=3）。
- X1 成本框架：每次换腿 0.1% + 尾盘乐观偏差扣减 0.3% = 0.4%/次换腿（0.3% 同 §3.2
  主口径，14:50 观测≈收盘价的乐观偏差预注册扣减；进/出两次换腿各计一次）。
  X1 momentum 基线组与信号腿**同执行价假设、同成本框架，只差信号**（基线换腿同计
  0.4%/次）。换腿判定沿用 R1 惯例：当日目标组名相对前一交易日变化即计一次换腿；
  信号目标组与当月动量持有组同名 → 无实际交易、不计成本（不虚构无交易的成本）。
- X2：白名单/触发/执行（T+1..T+3 接力、换腿 0.1%）与 R1 逐字相同——直接 import
  rotation.build_desired / rotation.leg_returns；组成员 = 申万一级行业（akshare
  sw_index_first_info + index_component_sw 逐行业在线取，只进内存）；组日收益等权、
  组内 >= MIN_MEMBERS(6) 只有数据才计；对照基线 = 行业组口径 momentum 基线组
  （R1 成本 0.1%/次换腿，与信号腿同框架）。
- 白名单/信号/动量逻辑零本地复刻：X1 与 X2 共用 rotation.monthly_whitelists /
  generate_signals / momentum_picks（组收益 DataFrame 换源，规则逐字不变）。
  X2 组收益引擎 group_returns_from_px 与 rotation.concept_returns 同式，实跑前做
  逐位一致性自检（喂 R1 概念成员必须复现 R1 组收益，防复刻走样）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

from signals import rotation as r1  # R1 冻结脚本：只 import 复用，绝不修改

BASE = r1.BASE

COST_TAIL_BIAS = 0.003                      # 尾盘乐观偏差扣减（§3.2 主口径 0.3%）
COST_SWITCH_X1 = r1.COST_PER_SWITCH + COST_TAIL_BIAS   # 0.1% + 0.3% = 0.4%/次换腿
SW_SLEEP_S = 0.2                            # 申万成分逐行业取数的礼貌间隔


# ---------------------------------------------------------------- X1 尾盘口径轴

def build_desired_x1(cret: pd.DataFrame, picks: dict, sigs: list, test_start: str,
                     hold_days: int = 1) -> dict:
    """X1 每日目标腿。hold_days=1 主口径：B 只计入 t0+1 一日（t0 尾盘收盘买入、
    t0+hold_days 尾盘收盘回落）；hold_days=2/3 为披露变体。同日多信号后触发者覆盖
    （沿用 R1 后触发覆盖先触发惯例）；其余日 = 当月动量持有组。"""
    cover = {}
    for t0, _a, b in sigs:
        pos = cret.index.get_loc(t0)
        for i in range(1, hold_days + 1):
            if pos + i < len(cret.index):
                cover[cret.index[pos + i]] = b
    desired = {}
    for d in cret.index:
        if d < test_start:
            continue
        desired[d] = cover.get(d, picks.get(d[:7]))
    return desired


def leg_returns_cost(cret: pd.DataFrame, desired: dict, equal_ret: pd.Series,
                     cost_per_switch: float) -> tuple:
    """R1 rotation.leg_returns 的成本参数化复刻（冻结文件不可改，逻辑逐行同源：
    None=CASH 计 0；'EQUAL' 取池内等权；组日收益缺失按 0 计；换腿判据 = 目标组名
    相对前一交易日变化）。唯一差异 = 每次换腿成本由参数给出（X1 框架 0.4%）。
    返回 (日收益Series, 换腿次数)。"""
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
            r -= cost_per_switch
            switches += 1
        rets[d] = r
        prev = desired[d]
    return pd.Series(rets).sort_index(), switches


def evaluate_gate(strat: pd.Series, mom: pd.Series) -> dict:
    """R1 三件套逐字判据（两轴共用）：① 年化超额 >= +5%；② 三段 >=2 正；③ MDD 劣化
    <=2pp。均扣成本口径（strat/mom 已各自含换腿成本的日收益序列）。"""
    _ts, ann_s, mdd_s = r1.metrics(strat)
    _tm, ann_m, mdd_m = r1.metrics(mom)
    seg_s = r1.seg_total(strat)
    seg_m = r1.seg_total(mom)
    seg_exc = {k: seg_s[k] - seg_m[k] for k in seg_s if k in seg_m}
    n_pos = sum(1 for v in seg_exc.values() if v > 0)
    out = {
        "ann_strat": ann_s, "ann_mom": ann_m, "exc_ann": ann_s - ann_m,
        "mdd_strat": mdd_s, "mdd_mom": mdd_m, "mdd_gap": mdd_s - mdd_m,
        "seg_exc": seg_exc, "n_pos": n_pos,
        "c1": (ann_s - ann_m) >= 0.05,
        "c2": n_pos >= 2,
        "c3": (mdd_s - mdd_m) <= 0.02,
    }
    out["ok"] = out["c1"] and out["c2"] and out["c3"]
    return out


def _print_gate(g: dict) -> None:
    print("\n== Gate（对照本轴 momentum 基线，三项须全过，判据照抄 R1）==")
    print(f"  ① 年化超额 {g['exc_ann']:+.2%}  （>= +5%? {'PASS' if g['c1'] else 'FAIL'}）")
    for k, v in g["seg_exc"].items():
        print(f"     段 {k}: 超额 {v:+.2%}")
    print(f"  ② 三段为正 {g['n_pos']}/3   （>=2? {'PASS' if g['c2'] else 'FAIL'}）")
    print(f"  ③ MDD 差 {g['mdd_gap']:+.2%}  （劣于基线 <=2pp? {'PASS' if g['c3'] else 'FAIL'}）")


def run_x1() -> bool:
    """X1 尾盘口径轴：R1 概念组数据/白名单/信号逐字复用，唯一变更 = 执行与成本框架。"""
    cret = r1.concept_returns()
    whitelists = r1.monthly_whitelists(cret)
    if not whitelists:
        print("X1 FAIL: 无任何月份满足 250 日白名单重估窗口")
        return False
    test_start = min(whitelists) + "-01"
    picks = r1.momentum_picks(cret)
    sigs = r1.generate_signals(cret, whitelists)
    equal_ret = cret.mean(axis=1)

    print("[X1 尾盘口径轴] 概念组口径与 R1 逐字相同（组收益/白名单/信号 import 复用）")
    print(f"数据: {cret.index[0]} ~ {cret.index[-1]}（{len(cret)} 交易日）")
    print(f"测试窗: {test_start} 起（首个满足 250 日白名单窗口的月份）")
    wl_months = [(m, wl) for m, wl in sorted(whitelists.items()) if wl]
    print(f"白名单: {len(wl_months)}/{len(whitelists)} 个月非空")
    for m, wl in wl_months:
        print(f"  {m}: {['%s→%s' % p for p in wl]}")
    print(f"信号数: {len(sigs)}")
    for t0, a, b in sigs:
        print(f"  {t0}  {a} 跌 → {b}")
    print(f"成本框架: 每次换腿 {COST_SWITCH_X1:.1%}（换腿 0.1% + 尾盘乐观偏差 0.3%），"
          f"基线同框架")

    desired_mom = {d: picks.get(d[:7]) for d in cret.index if d >= test_start}
    legs = {}
    info = {}
    for label, desired in [
        ("CASH", {d: None for d in desired_mom}),
        ("EQUAL", {d: "EQUAL" for d in desired_mom}),
        ("MOM(基线)", desired_mom),
        ("STRAT(信号腿)", build_desired_x1(cret, picks, sigs, test_start, hold_days=1)),
    ]:
        r, sw = leg_returns_cost(cret, desired, equal_ret, COST_SWITCH_X1)
        legs[label] = r
        info[label] = (r1.metrics(r), sw)

    print("\n== 全期指标（测试窗内，净口径）==")
    print(f"{'腿':<14}{'累计':>9}{'年化':>9}{'MDD':>8}{'换腿':>6}")
    for label in ["CASH", "EQUAL", "MOM(基线)", "STRAT(信号腿)"]:
        (total, ann, mdd), sw = info[label]
        print(f"{label:<14}{total:>8.1%}{ann:>8.1%}{mdd:>7.1%}{sw:>6}")

    g = evaluate_gate(legs["STRAT(信号腿)"], legs["MOM(基线)"])
    _print_gate(g)
    print(f"\nX1 VERDICT: {'PASS' if g['ok'] else 'FAIL'}")

    # T+2/T+3 持有变体：只披露，不作 gate（同一 X1 成本框架、同一 X1 基线）。
    print("\n== X1 持有期变体（披露，非 gate）==")
    print(f"{'变体':<10}{'累计':>9}{'年化':>9}{'MDD':>8}{'年化超额':>9}{'换腿':>6}")
    for hold in (1, 2, 3):
        d_var = build_desired_x1(cret, picks, sigs, test_start, hold_days=hold)
        r_var, sw = leg_returns_cost(cret, d_var, equal_ret, COST_SWITCH_X1)
        total, ann, mdd = r1.metrics(r_var)
        gv = evaluate_gate(r_var, legs["MOM(基线)"])
        tag = "主口径" if hold == 1 else "变体"
        print(f"T+{hold} 持有({tag}){total:>10.1%}{ann:>9.1%}{mdd:>8.1%}"
              f"{gv['exc_ann']:>9.2%}{sw:>6}")
    return g["ok"]


# ---------------------------------------------------------------- X2 行业粒度轴

def load_close_qfq_pivot() -> pd.DataFrame:
    """全库 close_qfq 透视表（mode=ro 只读；与 rotation.concept_returns 同查询）。"""
    conn = r1._connect_ro()
    try:
        df = pd.read_sql_query(
            "SELECT code, trade_date, close_qfq FROM daily_bar ORDER BY trade_date", conn)
    finally:
        conn.close()
    return df.pivot(index="trade_date", columns="code", values="close_qfq").sort_index()


def group_returns_from_px(px: pd.DataFrame, members: dict) -> pd.DataFrame:
    """组日收益（等权、组内 >=MIN_MEMBERS 只有数据才计，否则 NaN）——与
    rotation.concept_returns 同式，但组成员由参数给出（X2 = 申万一级行业）。"""
    ret = px.pct_change()
    cret = pd.DataFrame(index=ret.index)
    for g, codes in members.items():
        sub = ret[[c for c in codes if c in ret.columns]]
        cret[g] = sub.mean(axis=1).where(sub.notna().sum(axis=1) >= r1.MIN_MEMBERS)
    return cret.dropna(how="all")


def fetch_sw_members(codes=None, sleep_s: float = SW_SLEEP_S) -> tuple:
    """申万一级行业 → watchlist 成员组。akshare 在线取（sw_index_first_info 列 31 个
    一级行业 → index_component_sw 逐行业取成分），只进内存；任一接口失败上抛
    RuntimeError（main 捕获后阻断上报 X2：不重试、不换源、不落库）。
    返回 (members dict, stats dict)。"""
    import time

    import akshare as ak  # 延迟 import：X1 路径与单测零网络依赖

    if codes is None:
        cfg = json.loads(r1.CONFIG_PATH.read_text())
        codes = [e["code"] for e in cfg["watchlist"]]
    codes = [str(c).zfill(6) for c in codes]
    try:
        first = ak.sw_index_first_info()
        code2ind = {}
        for _, row in first.iterrows():
            ind_code = str(row["行业代码"]).split(".")[0]
            cons = ak.index_component_sw(symbol=ind_code)
            for c in cons["证券代码"].astype(str).str.zfill(6):
                code2ind[c] = str(row["行业名称"])
            time.sleep(sleep_s)
    except Exception as e:  # 阻断上报：失败不 Mock、不落库、不换源
        raise RuntimeError(
            f"申万一级行业映射在线取数失败（akshare {ak.__version__}）: "
            f"{type(e).__name__}: {e}") from e
    members: dict = {}
    covered, missing = [], []
    for c in codes:
        ind = code2ind.get(c)
        if ind is None:
            missing.append(c)
        else:
            members.setdefault(ind, []).append(c)
            covered.append(c)
    stats = {
        "industries": int(len(first)),
        "watchlist_codes": len(codes),
        "covered": len(covered),
        "missing": missing,
        "group_sizes": {g: len(v) for g, v in sorted(members.items(),
                                                     key=lambda kv: -len(kv[1]))},
    }
    return members, stats


def run_x2() -> bool:
    """X2 行业粒度轴：白名单/触发/执行与 R1 逐字相同，唯一变更 = 组成员换申万一级行业。"""
    print("\n[X2 行业粒度轴] 申万映射在线取数（akshare，只进内存）……")
    members, stats = fetch_sw_members()
    eligible = {g: v for g, v in members.items() if len(v) >= r1.MIN_MEMBERS}
    print(f"映射: {stats['industries']} 个申万一级行业，watchlist 覆盖 "
          f"{stats['covered']}/{stats['watchlist_codes']}"
          + (f"，缺映射 {stats['missing']}" if stats["missing"] else ""))
    print(f"行业组规模: {stats['group_sizes']}")
    print(f"入池组（>= {r1.MIN_MEMBERS} 只才计日收益）: "
          f"{ {g: len(v) for g, v in eligible.items()} }")

    # 引擎一致性自检：X2 组收益引擎喂 R1 概念成员必须逐位复现 R1 组收益。
    px = load_close_qfq_pivot()
    mine = group_returns_from_px(px, r1.load_groups())
    ref = r1.concept_returns()
    if not mine.equals(ref):
        print("X2 阻断: 组收益引擎与 R1 不一致（复刻走样），gate 前必须修复")
        return False
    print("组收益引擎一致性自检: 与 R1 concept_returns 逐位一致")

    cret = group_returns_from_px(px, members)
    whitelists = r1.monthly_whitelists(cret)
    if not whitelists:
        print("X2 FAIL: 无任何月份满足 250 日白名单重估窗口")
        return False
    test_start = min(whitelists) + "-01"
    picks = r1.momentum_picks(cret)
    sigs = r1.generate_signals(cret, whitelists)
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
    print(f"成本框架: 每次换腿 {r1.COST_PER_SWITCH:.1%}（与 R1 逐字相同，接力窗口 "
          f"T+1..T+{r1.WINDOW_DAYS}）")

    legs = {}
    info = {}
    for mode, label in [("cash", "CASH"), ("equal", "EQUAL(基线②)"),
                        ("mom", "MOM(基线③)"), ("strat", "STRAT(信号腿)")]:
        desired = r1.build_desired(cret, picks, sigs, test_start, mode)
        r, sw = r1.leg_returns(cret, desired, equal_ret)
        legs[label] = r
        info[label] = (r1.metrics(r), sw)

    print("\n== 全期指标（测试窗内，净口径）==")
    print(f"{'腿':<14}{'累计':>9}{'年化':>9}{'MDD':>8}{'换腿':>6}")
    for label in ["CASH", "EQUAL(基线②)", "MOM(基线③)", "STRAT(信号腿)"]:
        (total, ann, mdd), sw = info[label]
        print(f"{label:<14}{total:>8.1%}{ann:>8.1%}{mdd:>7.1%}{sw:>6}")

    g = evaluate_gate(legs["STRAT(信号腿)"], legs["MOM(基线③)"])
    _print_gate(g)
    print(f"\nX2 VERDICT: {'PASS' if g['ok'] else 'FAIL'}")
    return g["ok"]


def main() -> int:
    ok1 = run_x1()
    try:
        ok2 = run_x2()
    except RuntimeError as e:
        print(f"\nX2 阻断上报: {e}")
        print("按 §5-6 红线如实报告阻断点并停止该轴（不换非申万源、不硬编码、不落库）")
        ok2 = False
    print(f"\n总 VERDICT: X1={'PASS' if ok1 else 'FAIL'}  X2={'PASS' if ok2 else 'FAIL'}")
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    sys.exit(main())
