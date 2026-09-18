"""因子库：MA/RSI(Wilder)/ATR(Wilder)/动量/换手率分位/涨跌停幅度的纯函数，输入 pd.Series 或 numpy array，输出 float，数据长度不足返回 None。"""
import math
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

# 涨跌停幅度唯一口径（Phase 2 收敛：生产代码已不使用，仅 tests 引用——re-export 保持路径）
from common.market import limit_pct  # noqa: F401


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


# ============================================================
# Sprint 2 附任务（P1-2）：IVOL + MAX(5) 残差类因子
# ============================================================

def ivol(stock_close, mkt_close, window: int = 20) -> Optional[float]:
    """特质波动率（IVOL）：过去 window 日个股日收益对基准日收益 OLS 回归的残差标准差。

    依据：审查报告 P1-2——"低波异象"的更精细分解；CAPM 单因子残差 σ，
    基准用 HS300（index_daily '000300'，close 列日收益）。
    Fix-6：回归带截距（alpha, beta = polyfit(mr, sr, 1)，resid = sr − α − β·mr），
    剔除个股相对基准的系统性日均漂移后再量波动。
    数据不足（任一序列 < window+1 个点）返回 None。

    注意：两个序列按"尾部对齐"（取各自最后 window+1 个点算收益），
    调用方须保证两者日期已对齐（compute_signal 侧按 trade_date merge 后传入）。
    """
    s = _arr(stock_close)
    m = _arr(mkt_close)
    if len(s) < window + 1 or len(m) < window + 1:
        return None
    sr = np.diff(s[-(window + 1):]) / s[-(window + 1):-1]
    mr = np.diff(m[-(window + 1):]) / m[-(window + 1):-1]
    # 防基准收益恒为 0（数据异常）：退化为个股收益 std
    if np.std(mr) == 0.0:
        return float(np.std(sr))
    beta, alpha = np.polyfit(mr, sr, 1)  # polyfit 返回 [斜率, 截距]：β 在前 α 在后
    resid = sr - alpha - beta * mr
    return float(np.std(resid, ddof=1))


def max_ret_bali(close, window: int = 20, top_k: int = 5) -> Optional[float]:
    """MAX(window, top_k)（Bali et al. 定义）：过去 window 日日收益中最大的 top_k 个的均值。

    依据：审查报告 P1-2——"最大日收益率异象"，MAX 与未来收益负相关（彩票偏好溢价）。
    数据不足（< window+1 个点）返回 None。
    """
    a = _arr(close)
    if len(a) < window + 1:
        return None
    rets = np.diff(a[-(window + 1):]) / a[-(window + 1):-1]
    k = min(top_k, len(rets))
    top = np.sort(rets)[-k:]
    return float(top.mean())
