"""缠论 R3 批次 0 —— chan_data 数据层单测（纯合成数据，绝不连生产 DB）。

施工方案：docs/缠论R3施工方案-2026-09-19.md §3.1 + §3.6；期望值见 tests/chan_fixtures.py。
纪律：本文件零 DB 访问（连只读也不允许）；结构黄金用例（分型/笔/中枢/三买）由
批次 1 的 test_chanlib.py 消费 chan_fixtures，本文件只测 signals/chan_data.py。
"""
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

os.environ["AGSICKLE_DISABLE_LIVE_QUOTES"] = "1"  # 测试保持离线

from signals.chan_data import (TEST_START, WARMUP_BARS, calendar_index,
                               chain_breaks, construct_open_qfq,
                               distortion_days, valid_mask, warmup_start_date,
                               window_before, with_open_qfq)
from tests.chan_fixtures import (CHAIN_BREAK_CASE, DISTORTION_CASE,
                                 OPEN_QFQ_CASE, WINDOW_BEFORE_CASE)

_TESTS = []


def test(fn):
    _TESTS.append(fn)
    return fn


test.__test__ = False  # pytest 不要把装饰器本身当测试收集


def _df(rows, cols):
    return pd.DataFrame(rows, columns=cols)


# ---------------- open_qfq 构造（§3.1：open + (close_qfq − close)，逐 bar） ----------------

@test
def test_open_qfq_construction():
    rows = [(d, o, c, cq, expect) for d, o, c, cq, expect in OPEN_QFQ_CASE["rows"]]
    df = _df(rows, ["trade_date", "open", "close", "close_qfq", "expect"])
    got = construct_open_qfq(df)
    assert np.allclose(got.values, df["expect"].values), (got, df["expect"])
    # with_open_qfq 不改入参、列齐
    out = with_open_qfq(df.drop(columns=["expect"]))
    assert "open_qfq" in out.columns and "open_qfq" not in df.columns
    assert np.allclose(out["open_qfq"].values, df["expect"].values)
    # 除权阶梯日：open_qfq − close_qfq == open − close（加法偏移恒等式）
    assert np.allclose((out["open_qfq"] - out["close_qfq"]).values,
                       (out["open"] - out["close"]).values)


# ---------------- 畸变日（§3.6-2：pct_change 口径，严格 >1pp） ----------------

@test
def test_distortion_days():
    df = _df(list(zip(DISTORTION_CASE["dates"], DISTORTION_CASE["close"],
                      DISTORTION_CASE["close_qfq"])),
             ["trade_date", "close", "close_qfq"])
    got = distortion_days(df)
    assert got == DISTORTION_CASE["expect_days"], got
    # 单调同涨日（无复权阶梯）→ 空集
    df2 = _df([("2024-03-04", 10.0, 5.0), ("2024-03-05", 10.5, 5.25),
               ("2024-03-06", 11.0, 5.5)], ["trade_date", "close", "close_qfq"])
    assert distortion_days(df2) == set()
    # 首根 bar（ret NaN）不判畸变
    df3 = _df([("2024-03-04", 10.0, 5.0)], ["trade_date", "close", "close_qfq"])
    assert distortion_days(df3) == set()


# ---------------- 断链（§3.6-14d：日历间隔 >1 交易日；缺失数 = 间隔 − 1） ----------------

@test
def test_chain_breaks():
    cal = CHAIN_BREAK_CASE["calendar"]
    got = chain_breaks(CHAIN_BREAK_CASE["stock_dates"], cal)
    assert got == CHAIN_BREAK_CASE["expect_breaks"], got
    # 全覆盖 → 无断链
    assert chain_breaks(cal, cal) == []
    # 票内日期不在日历 → 从严报错（不静默）
    try:
        chain_breaks(["2024-01-02", "2099-01-01"], cal)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


WARMUP_CASE_N = 125


# ---------------- warmup（§3.6-14c：第 120 根有效 bar 的交易日；不足 → None） ----------------

@test
def test_warmup_start_date():
    dates = [d.strftime("%Y-%m-%d")
             for d in pd.bdate_range("2024-01-02", periods=WARMUP_CASE_N)]
    df = _df([(d, 10.0, 10.1, 10.2, 10.05, 10.15, 10.0) for d in dates],
             ["trade_date", "open", "high", "low", "close",
              "close_qfq", "high_qfq"])
    df["low_qfq"] = df["close_qfq"] - 0.05
    assert warmup_start_date(df) == dates[119]
    assert warmup_start_date(df.iloc[:119].reset_index(drop=True)) is None
    # 有效 bar 语义：中间一根 close_qfq 置 NaN → 有效 124 根；第 1..60 根有效 = idx 0..59，
    # 第 61 根有效 = idx 61 … 第 120 根有效 = idx 120 → 起始日 = dates[120]
    df2 = df.copy()
    df2.loc[60, "close_qfq"] = np.nan
    assert int(valid_mask(df2).sum()) == WARMUP_CASE_N - 1
    assert warmup_start_date(df2) == dates[120]


# ---------------- 60 交易日窗口（§3.6-3：以 anchor 为末根、含 anchor） ----------------

@test
def test_window_before():
    cal = [d.strftime("%Y-%m-%d")
           for d in pd.bdate_range("2024-01-02", periods=WINDOW_BEFORE_CASE["calendar_n"])]
    a = WINDOW_BEFORE_CASE["anchor_idx"]
    w = window_before(cal, cal[a], WINDOW_BEFORE_CASE["n"])
    assert len(w) == WINDOW_BEFORE_CASE["expect_len"]
    assert w[-1] == cal[WINDOW_BEFORE_CASE["expect_last_idx"]]
    assert w[0] == cal[a - WINDOW_BEFORE_CASE["n"] + 1]
    # 历史不足 → 截断
    t = WINDOW_BEFORE_CASE["truncated_anchor_idx"]
    w2 = window_before(cal, cal[t], WINDOW_BEFORE_CASE["n"])
    assert len(w2) == WINDOW_BEFORE_CASE["truncated_expect_len"]
    assert w2[-1] == cal[t] and w2[0] == cal[0]
    # anchor 不在日历 → 报错
    try:
        window_before(cal, "2099-01-01", 60)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert calendar_index(cal)[cal[7]] == 7


@test
def test_test_start_frozen():
    """测试窗起点为预注册常量，不得漂移。"""
    assert TEST_START == "2024-07-01"


def main() -> int:
    import traceback
    failed = 0
    for fn in _TESTS:
        try:
            fn()
            print("PASS %s" % fn.__name__)
        except Exception:  # noqa: BLE001
            failed += 1
            print("FAIL %s" % fn.__name__)
            traceback.print_exc()
    print("%d/%d tests passed" % (len(_TESTS) - failed, len(_TESTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
