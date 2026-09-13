"""P2 信号层测试：因子函数合成数据边界（长度不足/恒定序列/除零/分位边界）+ 真实 DB compute_all 输出合规。"""
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import json
import math

import numpy as np
import pandas as pd

from data.fetcher import get_conn  # noqa: F401  (保留给外部工具，测试已改用 :memory:)
from signals.factors import atr, limit_pct, ma, mom, rsi, turnover_pct
from signals.signals import compute_all


# ---------- ma ----------
def test_ma_insufficient_returns_none():
    assert ma([1.0, 2.0, 3.0], 5) is None


def test_ma_ok():
    assert ma(list(range(1, 21)), 20) == 10.5
    assert ma(pd.Series([1.0] * 30), 20) == 1.0


# ---------- rsi ----------
def test_rsi_insufficient_returns_none():
    assert rsi(list(range(1, 15)), 14) is None          # 14 点 < period+1
    assert rsi([1.0, 2.0], 14) is None


def test_rsi_constant_series():
    # 恒定序列：无涨无跌 → 约定中性 50
    v = rsi([5.0] * 60, 14)
    assert v is not None and abs(v - 50.0) < 1e-9


def test_rsi_monotonic_up_is_100():
    v = rsi(np.arange(1.0, 61.0), 14)                   # 单边上涨
    assert v == 100.0


def test_rsi_range():
    v = rsi(np.linspace(100, 10, 60), 14)               # 单边下跌
    assert 0.0 <= v <= 100.0


# ---------- atr ----------
def test_atr_insufficient_returns_none():
    n = 14
    assert atr([1.0] * n, [1.0] * n, [1.0] * n, 14) is None   # 需要 period+1
    assert atr([1.0] * 3, [1.0] * 3, [1.0] * 3, 14) is None


def test_atr_constant_series_is_zero():
    v = atr([10.0] * 30, [10.0] * 30, [10.0] * 30, 14)        # 恒定 → TR=0，除零不炸
    assert v == 0.0


def test_atr_positive_value():
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 1, 100))
    high, low = close + 1.0, close - 1.0
    v = atr(high, low, close, 14)
    assert v is not None and 0.5 < v < 3.0


# ---------- mom ----------
def test_mom_insufficient_returns_none():
    assert mom([1.0] * 20, 20) is None                  # 需要 n+1=21 点
    assert mom([1.0] * 5, 20) is None


def test_mom_ok():
    close = [10.0] * 20 + [11.0]                        # len=21
    assert abs(mom(close, 20) - 0.10) < 1e-9


def test_mom_zero_denominator_returns_none():
    close = [0.0] + [5.0] * 20                          # len=21，分母 close[-1-20]=close[0]=0
    assert mom(close, 20) is None


# ---------- turnover_pct ----------
def test_turnover_pct_empty_returns_none():
    assert turnover_pct([], 250) is None


def test_turnover_pct_single_is_one():
    assert turnover_pct([3.3], 250) == 1.0


def test_turnover_pct_bounds():
    arr = list(np.linspace(1.0, 100.0, 300))            # 300 点，window=250
    assert turnover_pct(arr + [1000.0], 250) == 1.0     # 追加最大值 → 分位 1
    v = turnover_pct(arr + [0.5], 250)                  # 追加最小值 → 1/250
    assert abs(v - 1.0 / 250) < 1e-9


def test_turnover_pct_midpoint():
    v = turnover_pct(list(range(1, 11)) + [5], 250)     # 11 点中 <=5 的有 6 个 → 6/11
    assert abs(v - 6.0 / 11.0) < 1e-9


# ---------- limit_pct ----------
def test_limit_pct_by_board():
    assert limit_pct("300750") == 0.20
    assert limit_pct("688801") == 0.20
    assert limit_pct("600519") == 0.10
    assert limit_pct("000001") == 0.10


# ---------- score profiles（reversal_lowvol 默认 / momentum 可选） ----------

def _synth_bars(closes, days=None):
    """由收盘序列构造升序 daily_bar DataFrame（high/low ±1%）。"""
    from datetime import date, timedelta
    d0 = date(2025, 1, 1)
    n = len(closes)
    return pd.DataFrame({
        "code": "600519",
        "trade_date": [(d0 + timedelta(days=i)).isoformat() for i in range(n)],
        "open": [c * 0.999 for c in closes], "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes], "close": closes,
        "volume": [1000.0] * n, "amount": [c * 1000 for c in closes],
        "pct_chg": [0.0] * n, "turnover": [1.0] * n,
    })


def test_score_reversal_default_prefers_dip_over_surged():
    """默认 profile：短期暴跌票 score 应高于短期暴涨票（反转因子）。"""
    import signals.signals as sig
    assert sig.profile() == "reversal_lowvol"
    base = [100.0] * 70
    up = sig.compute_signal("600519", pool=_synth_bars(base + [115.0] * 5))
    down = sig.compute_signal("600519", pool=_synth_bars(base + [85.0] * 5))
    assert down["score"] > up["score"], (down["score"], up["score"])


def test_score_momentum_profile_prefers_surged():
    """momentum profile：暴涨票 score 应高于暴跌票（与反转相反）。"""
    import signals.signals as sig
    orig = sig.profile
    sig.profile = lambda: "momentum"
    try:
        base = [100.0] * 70
        up = sig.compute_signal("600519", pool=_synth_bars(base + [115.0] * 5))
        down = sig.compute_signal("600519", pool=_synth_bars(base + [85.0] * 5))
        assert up["score"] > down["score"], (up["score"], down["score"])
    finally:
        sig.profile = orig


def test_factor_close_prefers_qfq():
    """close_qfq 存在时因子用前复权价：除权日的 -50% 假暴跌不再污染动量。"""
    bars = _synth_bars([100.0] * 30)
    bars["close_qfq"] = bars["close"]  # 未除权：两列相同
    bars.loc[bars.index[-1], "close"] = 50.0   # 模拟 10送10 除权后的不复权价
    bars.loc[bars.index[-1], "close_qfq"] = 100.0  # 复权口径无跳变
    import signals.signals as sig
    m_raw = sig.mom(bars["close"], 5)
    m_qfq = sig.mom(sig._factor_close(bars), 5)
    assert abs(m_raw + 0.5) < 1e-6             # 不复权：假暴跌 -50%
    assert abs(m_qfq) < 1e-6                   # 前复权：动量为 0


# ---------- compute_all（:memory: 合成库，不触碰生产 market.db——审查 P2-7 修复） ----------

def test_compute_all_synthetic_db():
    import sqlite3
    from data.fetcher import DDL
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    n = 0
    for code, closes in (("600519", [100.0 + i for i in range(70)]),
                         ("000001", [20.0 - i * 0.05 for i in range(70)])):
        bars = _synth_bars(closes).copy()
        bars["code"] = code
        for _, r in bars.iterrows():
            conn.execute(
                "INSERT INTO daily_bar (code, trade_date, open, high, low, close,"
                " volume, amount, pct_chg, turnover) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (r["code"], r["trade_date"], r["open"], r["high"], r["low"], r["close"],
                 r["volume"], r["amount"], r["pct_chg"], r["turnover"]))
        # 上市 200 天（远离 60 日次新黑名单线）
        from datetime import date, timedelta
        conn.execute("INSERT INTO stock_info VALUES (?,?,?,?)",
                     (code, code, (date(2025, 1, 1) - timedelta(days=200)).isoformat(), "x"))
        n += 1
    conn.commit()
    results = compute_all(conn=conn)
    assert isinstance(results, list) and len(results) == n
    assert {r["code"] for r in results} == {"600519", "000001"}
    for res in results:
        # JSON 可序列化且禁止 NaN/Inf 混入（allow_nan=False 会拒绝 NaN）
        s = json.dumps(res, ensure_ascii=False, allow_nan=False)
        assert "NaN" not in s and "Infinity" not in s
        assert 0.0 <= res["score"] <= 1.0
        for k, v in res["signals"].items():
            if isinstance(v, float):
                assert not math.isnan(v), f"{res['code']}.{k} 是 NaN"
            assert v is None or isinstance(v, (int, float, str, bool))
        assert res["signals"]["ma_trend"] in ("up", "down", "flat")
        assert isinstance(res["signals"]["above_ma60"], bool)
        assert type(res["score"]) is float
    # 与 signal 表内容一致
    got = conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0]
    assert got == len(results)
    conn.close()


if __name__ == "__main__":
    # pytest 未安装时的直跑入口：逐个执行 test_* 函数
    import traceback
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
