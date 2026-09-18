"""P2.4 信号回测：周调仓等权组合 vs 沪深300，双 profile 对比。

2026-09 修复（此前回测四重虚高来源）：
1. 可成交口径——入选日封死涨停（close ≥ 涨停价）跳过、ffill 停牌超 5 日剔除、
   成交额地板 5000 万（动量入选者天然集中在涨停票上，是回测虚高的头号来源，
   策略库 §4 自己指出了但旧代码没实现）；
2. 成本模型与执行层统一——佣金 0.025% 双边 + 印花税 0.05% 卖出 + 滑点 10bps，
   旧模型单边 0.075% 且无滑点；
3. 回测对象改为生产 score 的两个 profile（momentum / reversal_lowvol）对比，
   不再自动挑参（旧逻辑"不过关就从备选挑第一组达标"是样本内过拟合）；
4. 输出分年度收益与滑点敏感性区间，不再只给单点数字。

2026-09-14 再修（口径统一）：
5. 收益/动量/波动一律用前复权价（close_qfq 缺失单元格回退不复权）——此前
   用未复权 close，除权除息直接污染动量排名与收益（高分红票每年 2~3% 假跌幅）；
   涨停价判定仍用不复权价（停板价按真实价格板）；
6. reversal_lowvol 分支直接调用生产 signals.score_reversal_lowvol_xs
   （5日反转+低波+低换手 三因子截面 rank 合成）——此前回测代理只有两因子
   且用 rank pct，与生产 score 不是同一个东西，回测证伪的对象一直是错的。

2026-09-19 W-D6（P0-3 选型重跑前置，全部合入后重出 backtest_result.json）：
7.  涨停判定改 common.market.limit_price（Decimal HALF_UP 交易所口径）——
    裸乘积约 38% 真涨停收盘漏判为"可买"（P0-3 证据链第 3 条，单独贡献约
    +14pp 年化虚高）；market.py 红线5 同步废止；
8.  momentum 分支改调生产 _score_momentum 同源打分面板（0.35 趋势 + 0.40
    mom20 + 0.25 RSI14，缺因子重归一化）——此前是纯 mom20 TopN 截面，
    "回测通过的就不是生产在跑的策略"（P0-3 证据链第 4 条）；
9.  pass 硬性要求 universe==full（W-D6⑥）：core 宇宙 = 人工挑选池，幸存者
    偏差全进年化，core 跑可以但 pass 字段必须 null 并注明宇宙不符；
    main 默认 universe 由 core 改为 full（C-TEST-5：中证800/全宇宙防前视）；
10. IVOL 面板口径与 factors.ivol 统一（P2-2：带截距 OLS + 样本矩 ddof=1 +
    全窗 min_periods）；turn20 最小样本与生产统一（P2-3：min_periods=10）。

已知限制：回测宇宙为 daily_bar 全部票（中证800 回补数据用现时点成分回溯，
仍有成分变动偏差）；momentum 与 reversal 系均为样本内结果，置信度打折看
（策略库 §9）；qfq 以腾讯加法型前复权为主（W-B4），低股价×大额累计分红票的
历史段 qfq 收益被放大（源算法固有，见产出元数据 data_version 说明）。
"""
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import json
import logging
import logging.handlers
import math
import time
from typing import Tuple

import numpy as np
import pandas as pd

from common.config import load as config_load
from common.market import limit_pct as market_limit_pct, limit_price as market_limit_price
from data.fetcher import get_conn
from signals.factors import atr_series
from signals.signals import (MOM_SPAN, W_MOM, W_RSI, W_TREND,
                             score_reversal_lowvol_xs, score_reversal_lowvol_v2_xs)

log = logging.getLogger("backtest")
log.setLevel(logging.INFO)
if not log.handlers:
    from common.logsetup import rotating_handler  # AGSICKLE_LOG_DIR 逃生门（Sprint4 W0-1）
    log.addHandler(rotating_handler("signal.log"))
log.propagate = False

START = "2024-01-02"
ANN_DAYS = 252
COMMISSION = 0.00025     # 双边佣金（与 config.execution 一致）
STAMP_TAX = 0.0005       # 卖出印花税
SLIPPAGE = 0.001         # 滑点 10bps（默认档，敏感性 ±50%）
AMOUNT_FLOOR = 5e7       # 流动性地板：成交额 < 5000 万不入选
MAX_STALE_DAYS = 5       # 停牌（ffill）超过 5 日的票不入选
TOP_N = 5                # 等权持仓数
TURN20_WINDOW = 20       # 低换手因子窗口（与生产一致）


def _fetch_index_em(conn) -> int:
    """东财源（技术方案指定接口），中文列名。返回入库行数。"""
    import akshare as ak
    end = time.strftime("%Y%m%d")
    df = ak.index_zh_a_hist(symbol="000300", period="daily",
                            start_date="20240101", end_date=end)
    if df is None or df.empty:
        return 0
    rows = [("000300", pd.Timestamp(d).strftime("%Y-%m-%d"), float(c))
            for d, c in zip(df["日期"], df["收盘"])]
    conn.executemany(
        "INSERT OR REPLACE INTO index_daily (index_code, trade_date, close) "
        "VALUES (?,?,?)", rows)
    return len(rows)


def _fetch_index_tx(conn) -> int:
    """腾讯源兜底（东财接口在本机持续 ConnectionError 时的备选，偏离已在报告注明）。"""
    import akshare as ak
    df = ak.stock_zh_index_daily_tx(symbol="sh000300")
    if df is None or df.empty:
        return 0
    rows = [("000300", pd.Timestamp(d).strftime("%Y-%m-%d"), float(c))
            for d, c in zip(df["date"], df["close"])
            if pd.Timestamp(d) >= pd.Timestamp("2024-01-01")]
    conn.executemany(
        "INSERT OR REPLACE INTO index_daily (index_code, trade_date, close) "
        "VALUES (?,?,?)", rows)
    return len(rows)


def ensure_benchmark(conn) -> None:
    """先东财、后腾讯；接口全挂则沿用 index_daily 已有数据；都没有则报错退出。"""
    n_db = conn.execute(
        "SELECT COUNT(*) FROM index_daily WHERE index_code='000300'").fetchone()[0]
    for fn in (_fetch_index_em, _fetch_index_tx):
        try:
            n = fn(conn)
            conn.commit()
            time.sleep(1)  # 温和限速
            log.info("benchmark via %s: +%d rows", fn.__name__, n)
            return
        except Exception as e:
            log.warning("benchmark via %s fail: %s", fn.__name__, repr(e)[:100])
            time.sleep(1)
    if n_db > 0:
        log.info("指数接口不可用，沿用 index_daily 已有 %d 行", n_db)
        return
    log.error("沪深300数据获取失败且 index_daily 为空，退出")
    sys.exit(1)


def _metrics(nav: pd.Series) -> Tuple[float, float, float]:
    """(总收益, 年化, 最大回撤)，nav 为日频净值序列。"""
    total = float(nav.iloc[-1] / nav.iloc[0] - 1.0)
    n = max(len(nav) - 1, 1)
    ann = float(nav.iloc[-1] ** (ANN_DAYS / n) - 1.0) if nav.iloc[-1] > 0 else -1.0
    mdd = float((nav / nav.cummax() - 1.0).min())
    return total, ann, mdd


def _limit_up_price(prev_close: float, code: str) -> float:
    # W-D6④（P0-3 证据链3）：改 common/market.limit_price——Decimal ROUND_HALF_UP
    # 到分的交易所口径。裸乘积保留了一个月后实测：约 38% 真涨停收盘被漏判为
    # "可买"（例如 prev_close=10.07 的主板票，裸乘积 11.077 < 交易所涨停价 11.08，
    # 收盘 11.08 的真涨停被当成可买），单独贡献约 +14pp 年化虚高——market.py
    # 红线5（"保留裸乘法"）已据此废止。
    return market_limit_price(float(prev_close), market_limit_pct(code), up=True)


def _per_year(nav: pd.Series) -> dict:
    """分年度收益拆解（index 为 YYYY-MM-DD 字符串）。"""
    out = {}
    for y, seg in nav.groupby([str(x)[:4] for x in nav.index]):
        if len(seg) < 2:
            continue
        out[str(y)] = round(float(seg.iloc[-1] / seg.iloc[0] - 1.0), 4)
    return out


def _atr_pct_panel(wide: pd.DataFrame, hi_qfq: pd.DataFrame, lo_qfq: pd.DataFrame,
                   hi_raw: pd.DataFrame, lo_raw: pd.DataFrame) -> pd.DataFrame:
    """逐票 ATR 占价比面板（Wilder ATR14 / 当日因子收盘，复权口径优先）。

    每列独立选择口径：qfq H/L 覆盖足够用全复权，否则整列回退不复权
    （列内混口径才产生假 TR，列间口径不同无碍截面 rank）。逐列 Wilder
    递推 653 日 × 票数，830 票约 2 秒。
    """
    out = {}
    for c in wide.columns:
        if hi_qfq.get(c) is not None and pd.Series(hi_qfq[c]).notna().mean() > 0.9:
            h, l = hi_qfq[c], lo_qfq[c]
        else:
            h, l = hi_raw[c], lo_raw[c]
        df3 = pd.concat([h, l, wide[c]], axis=1).dropna()
        if len(df3) < 16:
            continue
        s = atr_series(df3.iloc[:, 0], df3.iloc[:, 1], df3.iloc[:, 2], 14)
        if s is None:
            continue
        atrp = pd.Series(s.values / df3.iloc[:, 2].values, index=df3.index)
        out[c] = atrp.reindex(wide.index)
    return pd.DataFrame(out)


def _wilder_rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    """factors.rsi（Wilder 平滑 + SMA 种子）的全序列复刻（W-D6⑤ 同源打分用）。

    逐条语义与 factors.rsi 一致：delta=diff；种子=前 period 个 TR 的均值；
    递推 avg=(avg*(p-1)+x)/p；恒定序列→50、仅跌→100、仅涨→0。
    输入含 NaN 时按该票实际有行情的连续序列计算再回索引（与生产"按 bars 行
    逐票计算"同语义——停牌日不产生跨缺口的假 delta）。长度不足 period+1 → 全 NaN。
    """
    vals = close.dropna()
    out = pd.Series(np.nan, index=close.index)
    n = len(vals)
    if n < period + 1:
        return out
    a = vals.values.astype(float)
    delta = np.diff(a)
    gain = np.clip(delta, 0.0, None)
    loss = -np.clip(delta, None, 0.0)

    def _rsi(ag: float, al: float) -> float:
        if ag == 0.0 and al == 0.0:
            return 50.0
        if al == 0.0:
            return 100.0
        if ag == 0.0:
            return 0.0
        return 100.0 - 100.0 / (1.0 + ag / al)

    ag = float(gain[:period].mean())
    al = float(loss[:period].mean())
    rsi_vals = np.full(n, np.nan)
    rsi_vals[period] = _rsi(ag, al)
    for i in range(period, len(delta)):
        ag = (ag * (period - 1) + float(gain[i])) / period
        al = (al * (period - 1) + float(loss[i])) / period
        rsi_vals[i + 1] = _rsi(ag, al)
    out.loc[vals.index] = rsi_vals
    return out


def _momentum_score_panel(wide: pd.DataFrame) -> pd.DataFrame:
    """生产 momentum profile 同源打分面板（W-D6⑤ / P0-3 证据链4）。

    生产 _score_momentum = 0.35 趋势（ma5/20/60 三均线向：全多 1.0/全空 0.0/
    其余 flat 0.5，仅在 ma5 可算时计入）+ 0.40 mom20 正向 clip(0.5+m/0.30)
    + 0.25 RSI14 健康度 (1-|r-50|/50)；缺因子剔除权重重归一化，全缺 → 0.5。
    此前回测 momentum 分支 = 纯 mom20 TopN 截面——回测通过的根本不是生产在跑
    的策略。MA 用 rolling(min_periods=n)（= factors.ma 长度不足返回 None）；
    mom20 分母为 0 → NaN（= factors.mom 返回 None）；RSI 用 _wilder_rsi_series。
    """
    ma5 = wide.rolling(5, min_periods=5).mean()
    ma20 = wide.rolling(20, min_periods=20).mean()
    ma60 = wide.rolling(60, min_periods=60).mean()
    base20 = wide.shift(20)
    mom20 = (wide / base20 - 1.0).where(base20 != 0)
    rsi14 = wide.apply(_wilder_rsi_series, axis=0)

    up = (ma5 > ma20) & (ma20 > ma60)
    down = (ma5 < ma20) & (ma20 < ma60)
    trend_v = pd.DataFrame(
        np.where(up, 1.0, np.where(down, 0.0, 0.5)),
        index=wide.index, columns=wide.columns)
    trend_v = trend_v.where(ma5.notna())          # ma5 不可算 → 该因子缺席

    w_t = trend_v.notna() * W_TREND
    m_clip = (0.5 + mom20 / MOM_SPAN).clip(0.0, 1.0)
    w_m = mom20.notna() * W_MOM
    r_h = 1.0 - (rsi14 - 50.0).abs() / 50.0
    w_r = rsi14.notna() * W_RSI

    num = (trend_v.fillna(0.0) * w_t + m_clip.fillna(0.0) * w_m
           + r_h.fillna(0.0) * w_r)
    den = w_t + w_m + w_r
    return (num / den.where(den > 0.0, 1.0)).where(den > 0.0, 0.5).clip(0.0, 1.0)


def run_backtest(pool: pd.DataFrame, idx_close: pd.Series, strategy: str = "reversal_lowvol",
                 top_n: int = TOP_N, slippage: float = SLIPPAGE,
                 universe: str = "full") -> dict:
    """周调仓 Top-N 等权策略（含可成交口径），空仓持币合法，权重期内随价格漂移。

    strategy: momentum = 生产 momentum profile 同源打分 TopN（W-D6⑤）；
    reversal_lowvol = 生产 reversal_lowvol score（5日反转+低波+低换手 截面合成）
    TopN；reversal_lowvol_v2 = 五因子截面合成 TopN。
    收益与因子用前复权价（缺失回退不复权）；涨停判定用不复权价 + 交易所取整。

    universe（W-D6⑥）："full" 才允许输出 pass 布尔值；core 等人工挑选宇宙的
    幸存者偏差全进年化，pass 强制 null 并注明宇宙不符——core 跑可以，但结果
    只供相对比较，不得作为选型依据。
    """
    for col in ("close_qfq", "high_qfq", "low_qfq"):   # 旧调用方可能不带复权列
        if col not in pool.columns:
            pool = pool.copy()
            pool[col] = pd.NA
    wide_raw = pool.pivot(index="trade_date", columns="code", values="close").sort_index()
    wide = pool.pivot(index="trade_date", columns="code", values="close_qfq").sort_index()
    wide = wide.combine_first(wide_raw)        # 复权缺失单元格回退不复权
    valid = wide_raw.notna()                   # 原始非缺失掩码（判停牌 ffill 陈旧度）
    wide_ff = wide_raw.ffill()                 # 停牌价用 raw ffill（只做涨停判定）
    amt = (pool.pivot(index="trade_date", columns="code", values="amount")
           .sort_index().reindex(wide.index).ffill())
    turn = (pool.pivot(index="trade_date", columns="code", values="turnover")
            .sort_index().reindex(wide.index))
    hi_qfq = pool.pivot(index="trade_date", columns="code", values="high_qfq").sort_index()
    lo_qfq = pool.pivot(index="trade_date", columns="code", values="low_qfq").sort_index()
    hi_raw = pool.pivot(index="trade_date", columns="code", values="high").sort_index()
    lo_raw = pool.pivot(index="trade_date", columns="code", values="low").sort_index()

    wide = wide.loc[(wide.index >= START) & (wide.index <= idx_close.index.max())]
    valid = valid.reindex(wide.index).fillna(False)
    wide_ff = wide_ff.reindex(wide.index)
    amt = amt.reindex(wide.index)
    hi_qfq = hi_qfq.reindex(wide.index); lo_qfq = lo_qfq.reindex(wide.index)
    hi_raw = hi_raw.reindex(wide.index); lo_raw = lo_raw.reindex(wide.index)
    turn = turn.reindex(wide.index)

    rets = wide.pct_change(fill_method=None)
    mom_win = 20 if strategy == "momentum" else 5
    mom_w = wide / wide.shift(mom_win) - 1.0
    atrp = _atr_pct_panel(wide, hi_qfq, lo_qfq, hi_raw, lo_raw)
    # P2-3（W-D6 前置）：turn20 最小样本与生产统一——生产现要求末 20 行内
    # ≥10 个有效换手（低于则 None 不进截面），此处 rolling min_periods=10 同语义
    turn20 = turn.rolling(TURN20_WINDOW, min_periods=TURN20_WINDOW // 2).mean()
    is_v2 = strategy == "reversal_lowvol_v2"
    if is_v2:
        ivol_p, max5_p = _ivol_max_panels(wide, idx_close)
        ivol_p = ivol_p.reindex(wide.index)
        max5_p = max5_p.reindex(wide.index)
    mom_score = _momentum_score_panel(wide) if strategy == "momentum" else None

    dates = list(wide.index)
    tmp = pd.DataFrame({"d": dates, "w": pd.PeriodIndex(dates, freq="W").astype(str)})
    _score_probe = mom_score if strategy == "momentum" else mom_w
    rebal_dates = [d for d in tmp.groupby("w")["d"].max()
                   if bool(_score_probe.loc[d].notna().any())]

    def tradable(d, code) -> bool:
        """可成交口径：未停牌超限、成交额达地板、未封死涨停。"""
        loc = wide_ff.index.get_loc(d)
        if loc == 0:
            return False
        stale = 0
        for j in range(loc, -1, -1):
            if bool(valid.iloc[j][code]):
                break
            stale += 1
        if stale > MAX_STALE_DAYS:
            return False
        a = amt.loc[d, code] if code in amt.columns else None
        if a is None or pd.isna(a) or float(a) < AMOUNT_FLOOR:
            return False
        prev = float(wide_ff.iloc[loc - 1][code])
        cur = float(wide_ff.loc[d, code])
        if cur >= _limit_up_price(prev, code) - 1e-9:
            return False                        # 封死涨停，买不进
        return True

    c_buy = COMMISSION + slippage
    c_sell = COMMISSION + STAMP_TAX + slippage
    avg_side = (c_buy + c_sell) / 2.0

    v, w = 1.0, {}
    nav_d = {}
    for d in dates:
        r = float((rets.loc[d] * pd.Series(w)).fillna(0.0).sum()) if w else 0.0
        v *= (1.0 + r)
        if w and r != 0.0:
            w = {c: wi * (1.0 + _safe(rets, d, c)) / (1.0 + r) for c, wi in w.items()}
        if d in rebal_dates:
            if strategy == "momentum":
                # W-D6⑤（P0-3 证据链4）：生产 momentum 同源打分 TopN——
                # 此前为纯 mom20 TopN 截面（回测通过的就不是生产在跑的策略）
                m = mom_score.loc[d].dropna()
                ranked = m.sort_values(ascending=False)
            elif is_v2:
                # 生产同源：v2 五因子截面合成（signals.score_reversal_lowvol_v2_xs）
                score, _parts = score_reversal_lowvol_v2_xs(
                    mom_w.loc[d], atrp.loc[d].reindex(mom_w.columns),
                    turn20.loc[d], ivol_p.loc[d], max5_p.loc[d])
                ranked = score.dropna().sort_values(ascending=False)
            else:
                # 生产同源：5日反转 + 低波 + 低换手 截面合成（signals.score_reversal_lowvol_xs）
                score, _parts = score_reversal_lowvol_xs(
                    mom_w.loc[d], atrp.loc[d].reindex(mom_w.columns), turn20.loc[d])
                ranked = score.dropna().sort_values(ascending=False)
            sel = [c for c in ranked.index if tradable(d, c)][:top_n]
            target = {c: 1.0 / len(sel) for c in sel}
            turnover = sum(abs(target.get(c, 0.0) - w.get(c, 0.0))
                           for c in set(target) | set(w))
            v *= (1.0 - avg_side * turnover)
            w = target
        nav_d[d] = v
    nav = pd.Series(nav_d)

    st_total, st_ann, st_mdd = _metrics(nav)
    bench = idx_close.reindex(nav.index).dropna()
    b_total, b_ann, b_mdd = _metrics(bench / bench.iloc[0])

    if w:
        w_sum = sum(w.values())
        final = {"as_of": str(nav.index[-1]), "codes": sorted(w),
                 "weights": {c: round(x / w_sum, 4) for c, x in w.items()},
                 "cash_weight": 0.0}
    else:
        final = {"as_of": str(nav.index[-1]), "codes": [], "weights": {},
                 "cash_weight": 1.0}

    # W-D6⑥（P0-3②）：pass 硬性要求 universe==full。core = 人工挑选池，
    # 51 只 2026 年人工挑的概念票回溯交易 2024-2025，幸存者偏差全进年化——
    # core 宇宙产物不得再给出 pass 布尔值（强制 null + 注明宇宙不符）。
    calmar_ok = bool(st_mdd != 0.0 and (st_ann / abs(st_mdd)) > 0.5)
    beat_ok = bool(st_ann > b_ann)
    universe_ok = (universe == "full")
    pass_val: object = bool(calmar_ok and beat_ok) if universe_ok else None

    return {
        "strategy": strategy,
        "universe": universe,
        "params": {"top_n": top_n,
                   # P2-8：f-string 直书——% 格式化串里 "0.05%(sell)" 会被解析成
                   # mapping 键（原代码被迫用 %% 转义，字面歧义即审查所指残留）
                   "cost_model": f"commission 0.025% + stamp 0.05%(sell)"
                                 f" + slippage {slippage * 1e4:.0f}bps",
                   "amount_floor": AMOUNT_FLOOR, "max_stale_days": MAX_STALE_DAYS,
                   "tradable_filter": True, "price_basis": "qfq_pref",
                   "limit_rounding": "exchange"},
        "window": {"start": str(nav.index[0]), "end": str(nav.index[-1]),
                   "trading_days": int(len(nav) - 1)},
        "strategy_perf": {"total_return": round(st_total, 4),
                          "annual_return": round(st_ann, 4),
                          "max_drawdown": round(st_mdd, 4),
                          "per_year": _per_year(nav)},
        "benchmark_hs300": {"total_return": round(b_total, 4),
                            "annual_return": round(b_ann, 4),
                            "max_drawdown": round(b_mdd, 4),
                            "per_year": _per_year(bench / bench.iloc[0])},
        "rebalance_count": len(rebal_dates),
        "final_holdings": final,
        # v1.4 修正（用户拍板方案 B，2026-09-17）：
        # 原阈值 (ann>基准 and MDD>-25%) 是模拟盘 P2.5 现实约束，
        # 对全样本回测偏严——momentum/reversal 各有缺陷但都被同一阈值拒。
        # 改用 Calmar 比率（年化/|MDD|，行业标准）+ 超额 年化 作为双判据：
        # 1) calmar > 0.5（收益回撤比高于池子均值）
        # 2) 年化 > 基准（绝对超额为正）
        # 例外：阈值 0.5 与模拟盘现实仓位约束匹配
        # （组合风控本身仍有单票20%/总仓80%/kill -8% 等硬规则保底）
        "pass": pass_val,
        "pass_criteria": {
            "version": "v2-calmar",
            "min_calmar": 0.5,
            "require_benchmark_beat": True,
            "calmar_ok": calmar_ok,
            "benchmark_beat": beat_ok,
            "universe": universe,
            "universe_ok": universe_ok,
            "note": ("v1 单阈值 pass 偏严（两个 profile 全不通过），改双判据"
                     if universe_ok else
                     "宇宙不符（core=人工挑选池，幸存者偏差）——pass 强制 null，"
                     "结果仅供相对比较，不得作为选型依据（W-D6⑥/P0-3②）"),
        },
    }


def _safe(rets: pd.DataFrame, d, c) -> float:
    """单票单日收益，停牌/缺失按 0 处理。"""
    x = float(rets.loc[d, c])
    return 0.0 if math.isnan(x) else x


def _ivol_max_panels(wide: pd.DataFrame, idx_close: pd.Series,
                     window: int = 20, top_k: int = 5) -> tuple:
    """Fix-5：v2 因子面板——口径以 factors.ivol / factors.max_ret_bali 为唯一基准
    （P2-2，W-D6 前置）。

    - ivol：个股日收益对 HS300 日收益滚动 window 日 OLS 回归（**带截距**）的
      残差 σ。与 factors.ivol 逐点一致：β=cov/var、resid_var=var_i−β²·var_m
      的恒等式只在 cov/var 同为**样本矩（ddof=1）**时成立——此前 cov 走滚动均值
      （÷n 总体矩）、var 走 pandas 默认（÷n−1），混合矩使 β 与残差系统性偏差；
      且 min_periods=window//2 短于生产全窗要求（factors.ivol 任一序列
      < window+1 个点返回 None），现统一 min_periods=window；
    - max5：滚动 window 日收益 top_k 均值（Bali MAX），min_periods 同步收紧到全窗。
    """
    rets = wide.pct_change(fill_method=None)
    bench = idx_close.reindex(wide.index).pct_change(fill_method=None)
    cov = rets.rolling(window, min_periods=window).cov(bench)      # 样本协方差 ddof=1
    var_m = bench.rolling(window, min_periods=window).var()        # ddof=1
    var_i = rets.rolling(window, min_periods=window).var()         # ddof=1
    beta = cov.div(var_m, axis=0)
    resid_var = (var_i - beta * beta.mul(var_m, axis=0)).clip(lower=0.0)
    ivol = np.sqrt(resid_var)

    def _topk_mean(x):
        return float(np.sort(x[np.isfinite(x)])[-top_k:].mean())

    max5 = rets.rolling(window, min_periods=window).apply(_topk_mean, raw=True)
    return ivol, max5


def main():
    conn = get_conn()
    ensure_benchmark(conn)
    # W-D6⑥（P0-3②/C-TEST-5）：默认 universe 改为 full——中证800 回补/全库宇宙
    # 防前视；core（config.watchlist_core 人工挑选池）只作相对比较跑，pass 恒
    # null。--universe=core 保留（选型相对对照用，产物自带宇宙不符标注）。
    import argparse as _ap
    _ap_inst = _ap.ArgumentParser(add_help=False)
    _ap_inst.add_argument("--universe", choices=("core", "full"), default="full")
    _args, _ = _ap_inst.parse_known_args()
    from common.config import core_codes as _core_codes  # 单一事实源（common.config）
    if _args.universe == "core":
        _u_codes = _core_codes()
    else:
        _u_codes = [str(r[0]) for r in
                    conn.execute("SELECT DISTINCT code FROM daily_bar").fetchall()]
    if _u_codes:
        _ph = ",".join("?" * len(_u_codes))
        pool = pd.read_sql(
            "SELECT code, trade_date, close, close_qfq, high, low, amount, turnover "
            "FROM daily_bar WHERE code IN (%s)" % _ph, conn, params=_u_codes)
    else:
        pool = pd.read_sql(
            "SELECT code, trade_date, close, close_qfq, high, low, amount, turnover "
            "FROM daily_bar", conn)
    idx = pd.read_sql("SELECT trade_date, close FROM index_daily WHERE index_code='000300' "
                      "ORDER BY trade_date", conn)
    n_universe = _u_codes.__len__() if _u_codes else conn.execute(
        "SELECT COUNT(DISTINCT code) FROM daily_bar").fetchone()[0]
    _ph2 = ",".join("?" * len(_u_codes)) if _u_codes else ""
    n_qfq = conn.execute(
        "SELECT COUNT(DISTINCT code) FROM daily_bar WHERE close_qfq IS NOT NULL"
        + (" AND code IN (%s)" % _ph2 if _u_codes else ""),
        tuple(_u_codes) if _u_codes else ()).fetchone()[0]
    conn.close()
    for col in ("close", "close_qfq", "high", "low", "amount", "turnover"):
        pool[col] = pd.to_numeric(pool[col], errors="coerce")
    idx_close = idx.set_index("trade_date")["close"].astype(float)

    results_ts = time.strftime("%Y-%m-%d %H:%M:%S")
    results = {"generated_at": results_ts,
               "universe": {"mode": _args.universe, "codes": int(n_universe), "codes_with_qfq": int(n_qfq), "desc": "full=daily_bar 全库（中证800 回补，选型唯一合法宇宙，C-TEST-5）/ core=config.watchlist_core 人工挑选池（幸存者偏差，pass 恒 null 仅供相对比较）"},
               "metadata": {
                   # W-D6⑦/W-C3：选型产物的元数据契约——universe/window/
                   # generated_at/data_version/limit_rounding 齐备；红线事件与
                   # bundle 引用回测数字必须带这里的版本信息（P1-25）。
                   "universe": {"mode": _args.universe, "codes": int(n_universe),
                                "codes_with_qfq": int(n_qfq)},
                   "generated_at": results_ts,
                   "data_version": (
                       "daily_bar 截至运行时最新交易日；close_qfq 以腾讯加法型前复权为主"
                       "（W-B4 全量重刷时 em 日线端点已宕 3 天，483 票重刷 473 票用 tx_qfq）。"
                       "源算法固有风险（非管道缺陷）：低股价×大额累计分红票的历史段 qfq 收益"
                       "被放大，如 600096 2024-01 raw -5.5% vs qfq -7.28%——跨源/跨期读数须知"),
                   "limit_rounding": "exchange",
                   "limit_price_impl": "common.market.limit_price（Decimal HALF_UP 到分，交易所口径；W-D6④）",
                   "momentum_scoring": "生产 signals._score_momentum 同源（0.35 趋势+0.40 mom20+0.25 RSI14，W-D6⑤）",
                   "ivol_basis": "factors.ivol 同口径（带截距 OLS + 样本矩 ddof=1 + 全窗，P2-2）",
                   "turn20_min_periods": TURN20_WINDOW // 2,
               },
"profiles": {}, "notes": [
                   "pass 硬性要求 universe==full（W-D6⑥/P0-3②）：core 宇宙产物 pass=null+宇宙不符标注，仅供相对比较",
                   "收益/动量/波动用前复权价（缺失回退不复权）；涨停判定用不复权价+交易所 HALF_UP 取整（W-D6④）",
                   "momentum 与 reversal 系均为样本内结果，置信度打折看（策略库 §9）",
                   "pass 判据 v2-calmar（2026-09-17）：年化/|MDD| > 0.5 且 年化 > 基准；"
                   "v1 单阈值 MDD>-25% 偏严，两个 profile 全不通过"]}
    for strat in ("momentum", "reversal_lowvol", "reversal_lowvol_v2"):
        r = run_backtest(pool, idx_close, strategy=strat, universe=_args.universe)
        results["profiles"][strat] = r
        log.info("回测 %s: ann=%.2f%% mdd=%.2f%% pass=%s",
                 strat, r["strategy_perf"]["annual_return"] * 100,
                 r["strategy_perf"]["max_drawdown"] * 100, r["pass"])
    # 元数据 window：三 profile 共用同一池窗口，取首个结果的实际区间
    _first = next(iter(results["profiles"].values()))
    results["metadata"]["window"] = {"start": _first["window"]["start"],
                                     "end": _first["window"]["end"],
                                     "trading_days": _first["window"]["trading_days"]}
    results["generated_at"] = results_ts
    # 顶层平铺一份 data_version（profile_verdict/bundle 等消费方兼容两种形态）
    results["data_version"] = results["metadata"]["data_version"]
    # 滑点敏感性（生产默认 profile ±50%）
    base = results["profiles"]["reversal_lowvol"]
    sens = {}
    for s in (SLIPPAGE * 0.5, SLIPPAGE, SLIPPAGE * 1.5):
        r = run_backtest(pool, idx_close, strategy="reversal_lowvol", slippage=s,
                         universe=_args.universe)
        sens["%.0fbps" % (s * 1e4)] = r["strategy_perf"]["annual_return"]
    base["slippage_sensitivity_annual"] = sens

    try:
        results["selected_profile"] = config_load().get("signals", {}).get(
            "profile", "reversal_lowvol")
    except Exception:  # noqa: BLE001
        results["selected_profile"] = "reversal_lowvol"

    out = json.dumps(results, ensure_ascii=False, indent=2)
    (BASE / "logs" / "backtest_result.json").write_text(out, encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
