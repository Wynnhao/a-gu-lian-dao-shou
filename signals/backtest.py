"""P2.4 信号回测：2024-01-02 至最新交易日，每周最后交易日收盘按 mom 动量等权调仓，双边成本合计 0.15%（买卖各 0.075%），对比沪深300。"""
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import json
import logging
import math
import time
from typing import List, Tuple

import akshare as ak
import pandas as pd

from data.fetcher import get_conn

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("backtest")
log.addHandler(logging.FileHandler(BASE / "logs" / "signal.log", encoding="utf-8"))
log.propagate = False

START = "2024-01-02"
COST_SIDE = 0.00075          # 单边 0.075%，双边合计 0.15%
DEFAULT_PARAMS = (20, "mom>0")
# 验收不达标时的备选参数（窗口 {10,30} + 过滤阈值 mom>截面中位数），合计补试 ≤3 组
ALTERNATE_PARAMS = [(10, "mom>0"), (30, "mom>0"), (20, "mom>median")]
ANN_DAYS = 252


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


def run_backtest(pool: pd.DataFrame, idx_close: pd.Series,
                 mom_window: int, filt: str) -> dict:
    """周调仓动量策略：调仓日收盘选 mom 达标者等权，空仓持币合法，权重在期内随涨跌漂移。"""
    wide = pool.pivot(index="trade_date", columns="code", values="close").sort_index()
    wide = wide.ffill()
    wide = wide.loc[(wide.index >= START) & (wide.index <= idx_close.index.max())]
    rets = wide.pct_change(fill_method=None)
    mom_w = wide / wide.shift(mom_window) - 1.0
    dates = list(wide.index)

    # 每周最后一个交易日（ISO 周分组取组内最大交易日）；动量窗口未填满的周不调仓
    tmp = pd.DataFrame({"d": dates, "w": pd.PeriodIndex(dates, freq="W").astype(str)})
    rebal_dates = [d for d in tmp.groupby("w")["d"].max()
                   if bool(mom_w.loc[d].notna().any())]

    v, w = 1.0, {}                     # 净值、持仓权重（随价格漂移）
    nav_d = {}
    for d in dates:
        # 审查 P1-2 修复：先用【旧权重】结算 d 日收益，收盘后再切换新权重——
        # 否则新组合会吃到据以选股的 d 日当日涨幅（look-ahead）。
        r = float((rets.loc[d] * pd.Series(w)).fillna(0.0).sum()) if w else 0.0
        v *= (1.0 + r)
        if w and r != 0.0:
            w = {c: wi * (1.0 + _safe(rets, d, c)) / (1.0 + r) for c, wi in w.items()}
        if d in rebal_dates:
            m = mom_w.loc[d].dropna()
            if filt == "mom>median" and len(m) >= 2:
                sel = m[m > m.median()].index.tolist()
            else:
                sel = m[m > 0.0].index.tolist()
            target = {c: 1.0 / len(sel) for c in sel}
            turnover = sum(abs(target.get(c, 0.0) - w.get(c, 0.0))
                           for c in set(target) | set(w))
            v *= (1.0 - COST_SIDE * turnover)   # 买卖双边各 0.075%
            w = target
        nav_d[d] = v
    nav = pd.Series(nav_d)

    st_total, st_ann, st_mdd = _metrics(nav)
    bench = idx_close.reindex(nav.index).dropna()
    b_total, b_ann, b_mdd = _metrics(bench / bench.iloc[0])  # 归一到净值口径再算年化

    if w:
        w_sum = sum(w.values())
        final = {"as_of": str(nav.index[-1]), "codes": sorted(w),
                 "weights": {c: round(x / w_sum, 4) for c, x in w.items()},
                 "cash_weight": 0.0}
    else:
        final = {"as_of": str(nav.index[-1]), "codes": [], "weights": {},
                 "cash_weight": 1.0}

    return {
        "params": {"mom_window": mom_window, "filter": filt,
                   "cost_round_trip_pct": COST_SIDE * 200},
        "window": {"start": str(nav.index[0]), "end": str(nav.index[-1]),
                   "trading_days": int(len(nav) - 1)},
        "strategy": {"total_return": round(st_total, 4),
                     "annual_return": round(st_ann, 4),
                     "max_drawdown": round(st_mdd, 4)},
        "benchmark_hs300": {"total_return": round(b_total, 4),
                            "annual_return": round(b_ann, 4),
                            "max_drawdown": round(b_mdd, 4)},
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
    pool = pd.read_sql("SELECT code, trade_date, close FROM daily_bar", conn)
    idx = pd.read_sql("SELECT trade_date, close FROM index_daily WHERE index_code='000300' "
                      "ORDER BY trade_date", conn)
    conn.close()
    pool["close"] = pool["close"].astype(float)
    idx_close = idx.set_index("trade_date")["close"].astype(float)

    tried: List[dict] = []
    result = run_backtest(pool, idx_close, *DEFAULT_PARAMS)
    tried.append({"params": result["params"], "pass": result["pass"],
                  "annual_return": result["strategy"]["annual_return"]})
    if not result["pass"]:  # 只允许 ≤3 组备选小范围重试
        for cand in ALTERNATE_PARAMS:
            result = run_backtest(pool, idx_close, *cand)
            tried.append({"params": result["params"], "pass": result["pass"],
                          "annual_return": result["strategy"]["annual_return"]})
            if result["pass"]:
                break
    result["tried_params"] = tried
    if not result["pass"]:
        log.warning("全部参数组均未达标，如实输出（小池子分散不足是已知限制）")

    out = json.dumps(result, ensure_ascii=False, indent=2)
    (BASE / "logs" / "backtest_result.json").write_text(out, encoding="utf-8")
    print(out)
    log.info("回测完成: params=%s pass=%s", result["params"], result["pass"])


if __name__ == "__main__":
    main()
