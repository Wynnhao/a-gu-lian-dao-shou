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

from data.fetcher import get_conn
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


# ---------- 真实 DB：compute_all ----------
def test_compute_all_real_db():
    conn = get_conn()
    results = compute_all(conn=conn)
    conn.close()
    assert isinstance(results, list) and len(results) >= 1

    for res in results:
        # JSON 可序列化且禁止 NaN/Inf 混入（allow_nan=False 会拒绝 NaN）
        s = json.dumps(res, ensure_ascii=False, allow_nan=False)
        assert "NaN" not in s and "Infinity" not in s
        assert 0.0 <= res["score"] <= 1.0
        for k, v in res["signals"].items():
            if isinstance(v, float):
                assert not math.isnan(v), f"{res['code']}.{k} 是 NaN"
            # None 因子必须是显式 null（json 里合法），不允许 numpy 类型
            assert v is None or isinstance(v, (int, float, str, bool))
        assert res["signals"]["ma_trend"] in ("up", "down", "flat")
        assert isinstance(res["signals"]["above_ma60"], bool)
        # pd.read_sql 出来的数值可能是 numpy 类型，检查已转成 Python float
        assert type(res["score"]) is float

    # 与 signal 表内容一致（每票最新 as_of 各一行）
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0]
    conn.close()
    assert n >= len(results)


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
