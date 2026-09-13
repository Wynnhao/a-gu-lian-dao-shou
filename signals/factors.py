"""因子库：MA/RSI(Wilder)/ATR(Wilder)/动量/换手率分位/涨跌停幅度的纯函数，输入 pd.Series 或 numpy array，输出 float，数据长度不足返回 None。"""
import math
from typing import Optional

import numpy as np
import pandas as pd


def _arr(x) -> np.ndarray:
    """统一转一维 float 数组并丢弃 NaN，避免上游缺失值污染计算。"""
    a = np.asarray(x, dtype=float).ravel()
    return a[~np.isnan(a)]


def ma(arr, n: int) -> Optional[float]:
    """简单均线：最近 n 期均值；长度不足返回 None。"""
    a = _arr(arr)
    if len(a) < n:
        return None
    return float(a[-n:].mean())


def rsi(close, period: int = 14) -> Optional[float]:
    """Wilder 平滑 RSI；至少需要 period+1 个点，否则 None。
    约定：涨跌均为 0（恒定序列）时返回 50.0；仅跌为 0 返回 100.0；仅涨为 0 返回 0.0。"""
    a = _arr(close)
    if len(a) < period + 1:
        return None
    delta = np.diff(a)
    gain = np.clip(delta, 0.0, None)
    loss = -np.clip(delta, None, 0.0)
    avg_gain = float(gain[:period].mean())
    avg_loss = float(loss[:period].mean())
    for i in range(period, len(delta)):
        avg_gain = (avg_gain * (period - 1) + float(gain[i])) / period
        avg_loss = (avg_loss * (period - 1) + float(loss[i])) / period
    if avg_gain == 0.0 and avg_loss == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    if avg_gain == 0.0:
        return 0.0
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1.0 + rs))


def atr_series(high, low, close, period: int = 14) -> Optional[pd.Series]:
    """Wilder 平滑 ATR 全序列（与 atr() 同一种子与递推，供回测面板逐日取值）。

    输入等长序列；返回与输入等长的 Series（前 period 个点为 NaN）。序列过短
    （< period+1 个点）返回 None。
    """
    h, l, c = _arr(high), _arr(low), _arr(close)
    if not (len(h) == len(l) == len(c)):
        raise ValueError("high/low/close 长度不一致")
    if len(c) < period + 1:
        return None
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    out = np.full(len(tr), np.nan)
    val = float(tr[1:period + 1].mean())  # 用前 period 个完整 TR 作种子
    out[period] = val
    for i in range(period + 1, len(tr)):
        val = (val * (period - 1) + float(tr[i])) / period
        out[i] = val
    return pd.Series(out, index=pd.RangeIndex(len(tr)))


def atr(high, low, close, period: int = 14) -> Optional[float]:
    """Wilder 平滑 ATR（最新值）；至少需要 period+1 个点（首日 TR 用 high-low），否则 None。"""
    s = atr_series(high, low, close, period)
    if s is None:
        return None
    v = float(s.iloc[-1])
    return None if math.isnan(v) else v


def mom(close, n: int = 20) -> Optional[float]:
    """n 日动量：close[-1]/close[-1-n] - 1；长度不足 n+1 或分母为 0 返回 None。"""
    a = _arr(close)
    if len(a) < n + 1:
        return None
    denom = float(a[-1 - n])
    if denom == 0.0:
        return None
    return float(a[-1]) / denom - 1.0


def turnover_pct(turnover, window: int = 250) -> Optional[float]:
    """当前换手率在近 window 期（含当日）内的分位，0~1；无数据返回 None。
    序列含当前值：最大值 → 1.0，最小值 → 1/len(window切片)。"""
    a = _arr(turnover)
    if len(a) == 0:
        return None
    w = a[-window:]
    cur = float(w[-1])
    return float((w <= cur).mean())


def limit_pct(code: str) -> float:
    """涨跌停幅度：创业板(30)/科创板(68) 0.20，其余 0.10。"""
    return 0.20 if str(code).startswith(("30", "68")) else 0.10
