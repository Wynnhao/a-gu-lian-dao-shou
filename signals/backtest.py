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

已知限制：回测宇宙仍是 daily_bar 中的票（当前=自选池 30 只，事后人工挑选、
幸存者偏差大）；中证 800 宇宙回补待东财接口解封后执行（见 docs/优化修复纪要.md）。
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
from typing import List, Tuple

import akshare as ak
import pandas as pd

from data.fetcher import get_conn

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("backtest")
log.addHandler(logging.handlers.RotatingFileHandler(BASE / "logs" / "signal.log", encoding="utf-8", maxBytes=5_000_000, backupCount=3))
log.propagate = False

START = "2024-01-02"
ANN_DAYS = 252
COMMISSION = 0.00025     # 双边佣金（与 config.execution 一致）
STAMP_TAX = 0.0005       # 卖出印花税
SLIPPAGE = 0.001         # 滑点 10bps（默认档，敏感性 ±50%）
AMOUNT_FLOOR = 5e7       # 流动性地板：成交额 < 5000 万不入选
MAX_STALE_DAYS = 5       # 停牌（ffill）超过 5 日的票不入选
TOP_N = 5                # 等权持仓数


def _fetch_index_em(conn) -> int:
    """东财源（技术方案指定接口），中文列名。返回入库行数。"""
    end = time.strftime("%Y%m%d")
    df = ak.index_zh_a_hist(symbol="000300", period="daily",
                            start_date="20240101", end_date=end)
    if df is None or df.empty:
        return 0
    rows = [("000300", pd.Timestamp(d).strftime("%Y-%m-%d"), float(c))
            for d, c in zip(df["日期"], df["收盘"])]
    conn.executemany("INSERT OR REPLACE INTO index_daily VALUES (?,?,?)", rows)
    return len(rows)


def _fetch_index_tx(conn) -> int:
    """腾讯源兜底（东财接口在本机持续 ConnectionError 时的备选，偏离已在报告注明）。"""
    df = ak.stock_zh_index_daily_tx(symbol="sh000300")
    if df is None or df.empty:
        return 0
    rows = [("000300", pd.Timestamp(d).strftime("%Y-%m-%d"), float(c))
            for d, c in zip(df["date"], df["close"])
            if pd.Timestamp(d) >= pd.Timestamp("2024-01-01")]
    conn.executemany("INSERT OR REPLACE INTO index_daily VALUES (?,?,?)", rows)
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
    pct = 0.20 if str(code).startswith(("30", "68")) else 0.10
    return float(prev_close) * (1 + pct)


def _per_year(nav: pd.Series) -> dict:
    """分年度收益拆解（index 为 YYYY-MM-DD 字符串）。"""
    out = {}
    for y, seg in nav.groupby([str(x)[:4] for x in nav.index]):
        if len(seg) < 2:
            continue
        out[str(y)] = round(float(seg.iloc[-1] / seg.iloc[0] - 1.0), 4)
    return out


def run_backtest(pool: pd.DataFrame, idx_close: pd.Series, strategy: str = "reversal_lowvol",
                 top_n: int = TOP_N, slippage: float = SLIPPAGE) -> dict:
    """周调仓 Top-N 等权策略（含可成交口径），空仓持币合法，权重期内随价格漂移。

    strategy: momentum = 20日动量 TopN（正向）；reversal_lowvol = 5日反转 + 低波动
    综合分 TopN（生产 reversal_lowvol score 的日线代理口径）。
    """
    wide = pool.pivot(index="trade_date", columns="code", values="close").sort_index()
    valid = wide.notna()                       # 原始非缺失掩码（判停牌 ffill 陈旧度）
    wide = wide.ffill()
    amt = (pool.pivot(index="trade_date", columns="code", values="amount")
           .sort_index().reindex(wide.index).ffill())
    wide = wide.loc[(wide.index >= START) & (wide.index <= idx_close.index.max())]
    valid = valid.reindex(wide.index).fillna(False)
    amt = amt.reindex(wide.index)
    rets = wide.pct_change(fill_method=None)
    win = 20 if strategy == "momentum" else 5
    mom_w = wide / wide.shift(win) - 1.0
    vol20 = rets.rolling(20).std()

    dates = list(wide.index)
    tmp = pd.DataFrame({"d": dates, "w": pd.PeriodIndex(dates, freq="W").astype(str)})
    rebal_dates = [d for d in tmp.groupby("w")["d"].max()
                   if bool(mom_w.loc[d].notna().any())]

    def tradable(d, code) -> bool:
        """可成交口径：未停牌超限、成交额达地板、未封死涨停。"""
        loc = wide.index.get_loc(d)
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
        prev = float(wide.iloc[loc - 1][code])
        cur = float(wide.loc[d, code])
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
            m = mom_w.loc[d].dropna()
            if strategy == "momentum":
                ranked = m[m > 0.0].sort_values(ascending=False)
            else:
                # 反转+低波综合分：5日跌幅越深分越高，同等跌幅下波动越低越优
                rev = -m
                vol = vol20.loc[d].reindex(rev.index)
                score = rev.rank(pct=True) + (1.0 - vol.rank(pct=True, na_option="bottom"))
                ranked = score.sort_values(ascending=False)
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

    return {
        "strategy": strategy,
        "params": {"top_n": top_n, "cost_model": "commission 0.025%% + stamp 0.05%%(sell)"
                                                   " + slippage %.0fbps" % (slippage * 1e4),
                   "amount_floor": AMOUNT_FLOOR, "max_stale_days": MAX_STALE_DAYS,
                   "tradable_filter": True},
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
        "pass": bool(st_ann > b_ann and st_mdd > -0.25),
    }


def _safe(rets: pd.DataFrame, d, c) -> float:
    """单票单日收益，停牌/缺失按 0 处理。"""
    x = float(rets.loc[d, c])
    return 0.0 if math.isnan(x) else x


def main():
    conn = get_conn()
    ensure_benchmark(conn)
    pool = pd.read_sql("SELECT code, trade_date, close, amount FROM daily_bar", conn)
    idx = pd.read_sql("SELECT trade_date, close FROM index_daily WHERE index_code='000300' "
                      "ORDER BY trade_date", conn)
    conn.close()
    pool["close"] = pool["close"].astype(float)
    pool["amount"] = pd.to_numeric(pool["amount"], errors="coerce")
    idx_close = idx.set_index("trade_date")["close"].astype(float)

    results = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "profiles": {}, "notes": [
                   "回测宇宙仍为 daily_bar 内票池（幸存者偏差），中证800 宇宙回补待数据源解封",
                   "momentum 与 reversal_lowvol 均为样本内结果，置信度打折看（策略库 §9）"]}
    for strat in ("momentum", "reversal_lowvol"):
        r = run_backtest(pool, idx_close, strategy=strat)
        results["profiles"][strat] = r
        log.info("回测 %s: ann=%.2f%% mdd=%.2f%% pass=%s",
                 strat, r["strategy_perf"]["annual_return"] * 100,
                 r["strategy_perf"]["max_drawdown"] * 100, r["pass"])
    # 滑点敏感性（生产默认 profile ±50%）
    base = results["profiles"]["reversal_lowvol"]
    sens = {}
    for s in (SLIPPAGE * 0.5, SLIPPAGE, SLIPPAGE * 1.5):
        r = run_backtest(pool, idx_close, strategy="reversal_lowvol", slippage=s)
        sens["%.0fbps" % (s * 1e4)] = r["strategy_perf"]["annual_return"]
    base["slippage_sensitivity_annual"] = sens

    try:
        cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
        results["selected_profile"] = cfg.get("signals", {}).get("profile",
                                                                 "reversal_lowvol")
    except Exception:  # noqa: BLE001
        results["selected_profile"] = "reversal_lowvol"

    out = json.dumps(results, ensure_ascii=False, indent=2)
    (BASE / "logs" / "backtest_result.json").write_text(out, encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
