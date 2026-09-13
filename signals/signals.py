"""信号层：按技术方案 §3 P2.3 输出个股技术信号 JSON 并入库；score 为 0~1 加权合成。

score 权重（写死）：趋势方向 0.35（up=1.0 / flat=0.5 / down=0.0，要求 MA5/20/60 齐全）；
20日动量 0.40（0.5 + mom_20d/0.30 截断到 [0,1]，即 ±15% 动量映射到满/零分，含符号与幅度）；
RSI 健康 0.25（30~70 区间内 =1.0，区间外 =0.0，二值）。
任一因子缺失时按剩余因子权重重归一化；全部缺失时 score 取中性 0.5。
"""
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import argparse
import json
import logging
import math
import time
from typing import Optional

import pandas as pd

from data.fetcher import get_conn  # noqa: E402
from risk.blacklist import check_blacklist  # noqa: E402
from signals.factors import atr, ma, mom, rsi, turnover_pct  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("signals")
log.addHandler(logging.FileHandler(BASE / "logs" / "signal.log", encoding="utf-8"))
log.propagate = False  # 只写 signal.log，不串到 fetcher 的 root handler

# 权重与参数（与模块 docstring 一致，勿随手改）
W_TREND, W_MOM, W_RSI = 0.35, 0.40, 0.25
MOM_SPAN = 0.30          # mom 映射斜率：score = clip(0.5 + mom/MOM_SPAN, 0, 1)
RSI_HEALTH = (30.0, 70.0)
TURNOVER_WINDOW = 250


def _f(x) -> Optional[float]:
    """转 JSON 安全 float：None/NaN → None，否则 float。"""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def _signal_from_bars(bars: pd.DataFrame, code: str, as_of: Optional[str]) -> dict:
    """对单只票的截至 as_of 日线序列计算信号 dict（bars 需按 trade_date 升序）。"""
    if as_of is not None:
        bars = bars[bars["trade_date"] <= as_of]
    bars = bars.sort_values("trade_date")
    if bars.empty:
        raise ValueError(f"{code} 在 as_of={as_of} 前无行情数据")

    close = bars["close"].reset_index(drop=True)
    high = bars["high"].reset_index(drop=True)
    low = bars["low"].reset_index(drop=True)
    to = bars["turnover"].reset_index(drop=True)

    ma5, ma20, ma60 = ma(close, 5), ma(close, 20), ma(close, 60)
    if ma5 is not None and ma20 is not None and ma60 is not None:
        trend = "up" if (ma5 > ma20 > ma60) else "down" if (ma5 < ma20 < ma60) else "flat"
    else:
        trend = "flat"  # MA 不全时无法判向，输出 schema 要求恒为字符串
    r = rsi(close, 14)
    a = atr(high, low, close, 14)
    m = mom(close, 20)
    tp = turnover_pct(to, TURNOVER_WINDOW)
    last = bars.iloc[-1]

    signals = {
        "ma_trend": trend,
        "ma5": _f(ma5), "ma20": _f(ma20), "ma60": _f(ma60),
        "rsi_14": _f(r), "atr_14": _f(a),
        "mom_20d": _f(m), "turnover_pct": _f(tp),
        "above_ma60": bool(_f(ma60) is not None and float(last["close"]) > _f(ma60)),
        "close": _f(last["close"]), "pct_chg": _f(last["pct_chg"]),
    }

    # score：三因子加权，缺谁去掉谁的权重（重归一化）
    num, den = 0.0, 0.0
    if ma5 is not None and ma20 is not None and ma60 is not None:
        num += W_TREND * {"up": 1.0, "flat": 0.5, "down": 0.0}[trend]
        den += W_TREND
    if m is not None:
        num += W_MOM * min(max(0.5 + m / MOM_SPAN, 0.0), 1.0)
        den += W_MOM
    if r is not None:
        num += W_RSI * (1.0 if RSI_HEALTH[0] <= r <= RSI_HEALTH[1] else 0.0)
        den += W_RSI
    score = (num / den) if den > 0 else 0.5  # 全缺时中性

    return {"code": str(code), "signals": signals,
            "score": round(float(score), 4),
            "as_of": str(last["trade_date"])}


def compute_signal(code: str, as_of: Optional[str] = None,
                   conn=None, pool: Optional[pd.DataFrame] = None) -> dict:
    """计算单票信号。pool 为已读入的 daily_bar DataFrame（compute_all 复用，避免重复读表）。"""
    own = conn is None and pool is None
    if pool is None:
        c = conn or get_conn()
        pool = pd.read_sql("SELECT * FROM daily_bar WHERE code=?", c, params=(code,))
        if own:
            c.close()
    bars = pool[pool["code"] == str(code)]
    return _signal_from_bars(bars, code, as_of)


def compute_all(as_of: Optional[str] = None, conn=None) -> list:
    """全池信号：读一次表，黑名单过滤后逐票计算并 INSERT OR REPLACE 入 signal 表。"""
    own = conn is None
    c = conn or get_conn()
    t0 = time.time()
    pool = pd.read_sql("SELECT * FROM daily_bar", c)
    codes = [r[0] for r in c.execute("SELECT code FROM stock_info ORDER BY code").fetchall()]
    bl = check_blacklist(c)

    results = []
    for code in codes:
        ok, reason = bl.get(code, (True, "-"))
        if not ok:
            log.info("skip %s 黑名单: %s", code, reason)
            continue
        try:
            res = compute_signal(code, as_of=as_of, conn=c, pool=pool)
        except ValueError as e:
            log.warning("skip %s: %s", code, e)
            continue
        c.execute("INSERT OR REPLACE INTO signal VALUES (?,?,?,?)",
                  (res["code"], res["as_of"],
                   json.dumps(res["signals"], ensure_ascii=False), res["score"]))
        results.append(res)
    c.commit()
    if own:
        c.close()
    log.info("compute_all %s 票，耗时 %.2fs", len(results), time.time() - t0)
    return results


def main():
    ap = argparse.ArgumentParser(description="计算全池技术信号并入库")
    ap.add_argument("--as_of", default=None, help="截至日期 YYYY-MM-DD（默认最新交易日）")
    ap.add_argument("--json", action="store_true", help="stdout 输出 JSON 数组（与表内容一致）")
    args = ap.parse_args()

    results = compute_all(as_of=args.as_of)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            s = r["signals"]
            log.info("%s as_of=%s trend=%s score=%.3f mom=%.3f rsi=%.1f close=%.2f",
                     r["code"], r["as_of"], s["ma_trend"], r["score"],
                     s["mom_20d"] if s["mom_20d"] is not None else float("nan"),
                     s["rsi_14"] if s["rsi_14"] is not None else float("nan"),
                     s["close"] if s["close"] is not None else float("nan"))


if __name__ == "__main__":
    main()
