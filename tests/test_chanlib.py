"""缠论 R3 批次 1 —— chanlib 结构引擎单测（纯合成数据，绝不连 DB）。

施工方案：docs/缠论R3施工方案-2026-09-19.md §3.2 + §3.6；黄金用例期望值见
tests/chan_fixtures.py（批次 0 手推、批次 1 逐一断言）。本文件零 DB 访问（连只读
也不允许）；数据层用例（open_qfq/畸变/断链/warmup/60日窗）由 tests/test_chan_data.py
覆盖，本文件只测 signals/chanlib.py 的结构语义。

补充手工用例（批次 1 新增，全部合成）：
- 宽松笔 min_gap=3 敏感性（§3.2 辅口径 vs 主口径差异可复现）；
- 断链切段后结构不跨链（§3.6-14d）；
- MACD 柱面积手工核对 + 背驰检出/不检出构造用例（§3.6-9，数值已预先验证）；
- 悬浮笔→突破一步收敛（§3.6-6③）与回抽失败后该枢单次尝试不再重扫。
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

from signals.chanlib import (LOOSE_MIN_GAP, STRICT_MIN_GAP, build_pivots,
                             build_strokes, compute_structure, find_fractals,
                             macd_divergence, merge_inclusion, split_segments,
                             third_buys)
from tests.chan_fixtures import (INCLUSION_CASES, FRACTAL_CASES, PIVOT_CASES,
                                 STROKE_CASE, THIRDBUY_CASES, bars_to_ohlc,
                                 make_dates)

_TESTS = []


def test(fn):
    _TESTS.append(fn)
    return fn


test.__test__ = False  # pytest 不要把装饰器本身当测试收集


def _struct(bars_hl, min_gap=STRICT_MIN_GAP):
    """(high, low) 列表 → (DataFrame, 结构 dict)。合成列名 high/low。"""
    df = bars_to_ohlc(bars_hl)
    st = compute_structure(df, min_gap=min_gap, high_col="high", low_col="low")
    return df, st


def _mkstroke(dir_, lo, hi, s_pi, e_pi, s_bar, e_bar, ef_pi, ef_conf_bar):
    """构造笔级测试输入（third_buys/build_pivots 所需最小字段）。

    端点分型价按笔方向机械推导：up 笔 sf=B(lo)/ef=T(hi)，down 笔 sf=T(hi)/ef=B(lo)。
    """
    sf = {"type": "B" if dir_ == "up" else "T", "p": lo if dir_ == "up" else hi,
          "pi": s_pi, "conf_bar": s_bar, "mid_i_start": s_bar, "mid_i_end": s_bar}
    ef = {"type": "T" if dir_ == "up" else "B", "p": hi if dir_ == "up" else lo,
          "pi": ef_pi, "conf_bar": ef_conf_bar, "mid_i_start": e_bar,
          "mid_i_end": e_bar}
    return {"dir": dir_, "hi": hi, "lo": lo, "start_pi": s_pi, "end_pi": e_pi,
            "sf": sf, "ef": ef, "start_bar": s_bar, "end_bar": e_bar}


# ---------------- 1. 包含处理（chan_fixtures.INCLUSION_CASES 全部 4 例） ----------------

@test
def test_inclusion_fixtures():
    for case in INCLUSION_CASES:
        bars = case["bars"]
        got = [(k["h"], k["l"], k["i_start"], k["i_end"])
               for k in merge_inclusion([b[0] for b in bars],
                                        [b[1] for b in bars])]
        expect = [tuple(e) for e in case["expect"]]
        assert got == expect, (case["name"], got, expect)


# ---------------- 2. 分型（FRACTAL_CASES 全部 2 例，含确认信息 §3.6-14a） ----------------

@test
def test_fractal_fixtures():
    for case in FRACTAL_CASES:
        df = bars_to_ohlc(case["bars"])
        merged = merge_inclusion(df["high"], df["low"])
        got = [(f["type"], f["pi"], f["p"], f["conf_bar"]) for f in find_fractals(merged)]
        expect = [tuple(e) for e in case["expect"]]
        assert got == expect, (case["name"], got, expect)
        # conf_pi = 第 3 根合并K索引 = pi+1；确认原始 bar = 该合并K i_end
        for f in find_fractals(merged):
            assert f["conf_pi"] == f["pi"] + 1
            assert f["conf_bar"] == merged[f["conf_pi"]]["i_end"]


# ---------------- 3. 严格笔（STROKE_CASE：三种丢弃原因齐全） ----------------

@test
def test_strict_stroke_fixture():
    case = STROKE_CASE
    _, st = _struct(case["bars"])
    kept = [(f["type"], f["pi"], round(f["p"], 6)) for f in st["kept_fractals"]]
    expect_kept = [(t, p, round(v, 6)) for t, p, v in case["expect"]["kept_fractals"]]
    assert kept == expect_kept, kept
    assert st["dropped"] == case["expect"]["dropped"], st["dropped"]
    got = [{"dir": s["dir"], "hi": s["hi"], "lo": s["lo"],
            "start_pi": s["start_pi"], "end_pi": s["end_pi"]}
           for s in st["strokes"]]
    assert got == case["expect"]["strokes"], got


# ---------------- 4. 同型等价：相等取后到者（§3.6-5 手工用例） ----------------

@test
def test_stroke_equal_price_takes_later():
    # B@2(p=9.0) 与 B@7(p=9.0) 等价 → 取后到者，旧者记 same_type_more_extreme；
    # T@4 与 B@2 距离 2<4 → 丢后者（opposite_type_too_close）
    hl = [(12, 11), (11, 10), (10, 9),      # B@2 p=9.0
          (10.4, 9.4), (10.8, 9.8), (10.5, 9.6),  # T@4 p=10.8
          (10.4, 9.4), (10, 9), (10.2, 9.2)]      # B@7 p=9.0
    df = bars_to_ohlc(hl)
    frs = find_fractals(merge_inclusion(df["high"], df["low"]))
    assert [(f["type"], f["pi"], f["p"]) for f in frs] == [
        ("B", 2, 9.0), ("T", 4, 10.8), ("B", 7, 9.0)]
    kept, strokes, dropped = build_strokes(frs, min_gap=STRICT_MIN_GAP)
    assert [f["pi"] for f in kept] == [7]
    assert dropped == {2: "same_type_more_extreme",
                       4: "opposite_type_too_close"}
    assert strokes == []


# ---------------- 5. 笔中枢与延伸（PIVOT_CASES）+ ZG<=ZD 不成枢 ----------------

@test
def test_pivot_fixture():
    case = PIVOT_CASES[0]
    pvs = build_pivots(case["strokes"])
    got = [{"start_stroke_idx": p["start_stroke_idx"], "zg": p["zg"],
            "zd": p["zd"], "ext_stroke_idxs": p["ext_stroke_idxs"]}
           for p in pvs]
    assert got == case["expect"], (got, case["expect"])
    # 无重叠三笔 → ZG<=ZD 不成枢
    no_pivot = [{"dir": "up", "lo": 1.0, "hi": 2.0},
                {"dir": "down", "lo": 10.0, "hi": 20.0},
                {"dir": "up", "lo": 30.0, "hi": 40.0}]
    assert build_pivots(no_pivot) == []
    # 扫描起点 = 最后延伸笔之后第一根笔
    assert pvs[0]["scan_start_idx"] == pvs[0]["ext_stroke_idxs"][-1] + 1
    assert pvs[2]["scan_start_idx"] == 5  # 无延伸 → 第三笔（idx 2+2=4）之后


# ---------------- 6. 三买三用例（成功 / 回抽失败 / 第一根突破约束） ----------------

@test
def test_thirdbuy_fixtures():
    for case in THIRDBUY_CASES:
        df, st = _struct(case["bars_hl"])
        dates = df["trade_date"].tolist()
        exp = case["expect"]
        # 前置：合成 bar 1:1 映射为合并K（用例推导前提）
        assert len(st["merged"]) == len(case["bars_hl"]), case["name"]
        kept = [(f["type"], f["pi"], round(f["p"], 6)) for f in st["kept_fractals"]]
        expect_kept = [(t, p, round(v, 6)) for t, p, v in exp["kept_fractals"]]
        assert kept == expect_kept, (case["name"], kept)
        assert len(st["strokes"]) == exp["n_strokes"], case["name"]
        tb = third_buys(st["strokes"], st["pivots"], dates)
        assert tb["raw_flags"] == exp["raw_flags"], (case["name"], tb["raw_flags"])
        sigs = tb["signals"]
        assert len(sigs) == len(exp["signals"]), case["name"]
        for got, want in zip(sigs, exp["signals"]):
            for k, v in want.items():
                assert got[k] == v, (case["name"], k, got[k], v)
            # 确认日 = 确认 bar（回抽底分型第 3 根合并K i_end）所指原始 bar 交易日
            assert got["confirm_date"] == dates[got["confirm_bar"]]
            assert dates[got["confirm_bar"]] == df.loc[got["confirm_bar"], "trade_date"]


# ---------------- 7. 宽松笔 min_gap=3 敏感性（§3.2 辅口径） ----------------

@test
def test_loose_stroke_min_gap_sensitivity():
    # B@2(9.0) —T@5(11.5)— B@10(8.0)：异性距离恰 3 → 主口径丢、辅口径留
    hl = [(12, 11), (11, 10), (10, 9),
          (10.5, 9.5), (11, 10), (11.5, 10.5),
          (11, 10), (10.5, 9.5), (10, 9), (9.5, 8.5), (9, 8), (9.2, 8.2)]
    df = bars_to_ohlc(hl)
    frs = find_fractals(merge_inclusion(df["high"], df["low"]))
    assert [(f["type"], f["pi"], f["p"]) for f in frs] == [
        ("B", 2, 9.0), ("T", 5, 11.5), ("B", 10, 8.0)]
    kept4, stks4, drop4 = build_strokes(frs, min_gap=STRICT_MIN_GAP)
    kept3, stks3, drop3 = build_strokes(frs, min_gap=LOOSE_MIN_GAP)
    assert [f["pi"] for f in kept4] == [10]
    assert drop4 == {2: "same_type_more_extreme", 5: "opposite_type_too_close"}
    assert stks4 == []
    assert [f["pi"] for f in kept3] == [2, 5, 10]
    assert [(s["dir"], s["hi"], s["lo"]) for s in stks3] == [
        ("up", 11.5, 9.0), ("down", 11.5, 8.0)]
    # 对照：STROKE_CASE 下宽松笔不改变保留分型（该用例对 min_gap 不敏感）
    df2 = bars_to_ohlc(STROKE_CASE["bars"])
    frs2 = find_fractals(merge_inclusion(df2["high"], df2["low"]))
    kept4b, _, _ = build_strokes(frs2, min_gap=STRICT_MIN_GAP)
    kept3b, _, _ = build_strokes(frs2, min_gap=LOOSE_MIN_GAP)
    assert [f["pi"] for f in kept4b] == [f["pi"] for f in kept3b] == [2, 7, 14, 21]


# ---------------- 8. 悬浮笔→突破一步收敛（§3.6-6③ 手工用例） ----------------

@test
def test_thirdbuy_suspended_then_breakout():
    strokes = [
        _mkstroke("up", 10.0, 13.0, 0, 4, 0, 6, ef_pi=4, ef_conf_bar=6),
        _mkstroke("down", 11.0, 13.0, 4, 10, 6, 12, ef_pi=10, ef_conf_bar=12),
        _mkstroke("up", 11.0, 12.5, 10, 14, 12, 20, ef_pi=14, ef_conf_bar=20),
        # 悬浮笔：向下、与 [11.0,12.5] 无交集（lo=12.8>ZG）且非 hi<ZD → 继续扫描
        _mkstroke("down", 12.8, 14.0, 14, 22, 20, 30, ef_pi=22, ef_conf_bar=30),
        # 突破：up 且 lo=12.8>ZG=12.5（悬浮笔的下一笔，一步收敛）
        _mkstroke("up", 12.8, 14.5, 22, 30, 30, 44, ef_pi=30, ef_conf_bar=44),
        # 回抽：端点 13.2 > ZG → 三买成立
        _mkstroke("down", 13.2, 14.5, 30, 38, 44, 50, ef_pi=38, ef_conf_bar=50),
    ]
    pvs = build_pivots(strokes)
    p0 = [p for p in pvs if p["start_stroke_idx"] == 0][0]
    assert (p0["zg"], p0["zd"], p0["ext_stroke_idxs"]) == (12.5, 11.0, [])
    dates = make_dates(60)
    tb = third_buys(strokes, pvs, dates)
    assert tb["raw_flags"] == [0]
    sig = tb["signals"][0]
    assert sig["breakout_stroke_idx"] == 4 and sig["pullback_stroke_idx"] == 5
    assert sig["trigger_price"] == 13.2
    assert sig["confirm_date"] == dates[50]


# ---------------- 9. 回抽失败 → 该枢单次尝试不再重扫（§3.6-6） ----------------

@test
def test_thirdbuy_single_attempt_no_rescan():
    strokes = [
        _mkstroke("up", 10.0, 13.0, 0, 4, 0, 6, ef_pi=4, ef_conf_bar=6),
        _mkstroke("down", 11.0, 13.0, 4, 10, 6, 12, ef_pi=10, ef_conf_bar=12),
        _mkstroke("up", 11.0, 12.5, 10, 14, 12, 20, ef_pi=14, ef_conf_bar=20),
        _mkstroke("down", 12.8, 14.0, 14, 22, 20, 30, ef_pi=22, ef_conf_bar=30),
        _mkstroke("up", 12.8, 14.5, 22, 30, 30, 44, ef_pi=30, ef_conf_bar=44),
        # 回抽端点 12.0 ≤ ZG=12.5 → 该枢失败
        _mkstroke("down", 12.0, 14.5, 30, 38, 44, 50, ef_pi=38, ef_conf_bar=50),
        # 之后再次突破+本应成功的回抽——不得从同枢重扫
        _mkstroke("up", 13.0, 15.0, 38, 46, 50, 60, ef_pi=46, ef_conf_bar=60),
        _mkstroke("down", 13.5, 15.0, 46, 54, 60, 70, ef_pi=54, ef_conf_bar=70),
    ]
    pvs = build_pivots(strokes)
    dates = make_dates(80)
    tb = third_buys(strokes, pvs, dates)
    assert tb["raw_flags"] == [], tb["raw_flags"]
    assert tb["signals"] == []


# ---------------- 10. 断链切段：结构不跨链（§3.6-14d） ----------------

@test
def test_split_segments_no_cross_chain():
    cal = [d.strftime("%Y-%m-%d")
           for d in pd.bdate_range("2024-01-02", periods=12)]
    seg1_dates, seg2_dates = cal[0:3], cal[5:12]   # 缺 cal[3]、cal[4] 两交易日
    hl = [(10, 9), (11, 10), (12, 11),             # 段1 终于峰（12,11）
          (11.5, 10.5), (11, 10), (10.5, 9.5), (10, 9), (9.5, 8.5),
          (9, 8), (8.5, 8)]                        # 段1 峰后段2 直接回落
    df = bars_to_ohlc(hl, dates=seg1_dates + seg2_dates)
    segs = split_segments(df, cal)
    assert len(segs) == 2
    assert segs[0]["trade_date"].tolist() == seg1_dates
    assert segs[1]["trade_date"].tolist() == seg2_dates
    assert list(segs[0]["high"]) == [b[0] for b in hl[:3]]
    # 不切段：跨缺口把段2 首根并入 → 在合并K2 检出跨链顶分型
    frs_full = find_fractals(merge_inclusion(df["high"], df["low"]))
    assert ("T", 2) in [(f["type"], f["pi"]) for f in frs_full]
    # 切段后：段内独立计算，段1（终于峰）无分型、跨链分型消失
    st0 = compute_structure(segs[0], high_col="high", low_col="low")
    assert st0["fractals"] == []
    for seg in segs:
        st = compute_structure(seg, high_col="high", low_col="low")
        for s in st["strokes"]:   # 笔的原始 bar 区间不越段界
            assert 0 <= s["start_bar"] <= s["end_bar"] < len(seg)
    # 无断链 → 单段原样返回
    segs1 = split_segments(bars_to_ohlc([(10, 9), (11, 10), (12, 11)]), cal[:3] + cal[6:])
    assert len(segs1) == 1 and len(segs1[0]) == 3
    # 票内日期不在日历 → 复用 chain_breaks 的从严报错
    try:
        split_segments(bars_to_ohlc([(10, 9), (11, 10)],
                                    dates=["2024-01-02", "2099-01-01"]), cal)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


# ---------------- 11. MACD 柱面积手工核对（§3.6-9 标准递归） ----------------

@test
def test_macd_hist_hand_check():
    # close=[10,11]：EMA12[1]=10+2/13·1，EMA26[1]=10+2/27·1，DIF[0]=0，
    # DEA[1]=(2/10)·DIF[1]+(8/10)·DIF[0] → 柱[1]=2·(DIF[1]−DEA[1])=1.6·DIF[1]
    res = macd_divergence([10.0, 11.0],
                          [{"dir": "up", "hi": 11.0, "lo": 10.0,
                            "start_bar": 0, "end_bar": 1}])
    dif1 = (10.0 + 2.0 / 13.0) - (10.0 + 2.0 / 27.0)
    expect_hist1 = 2.0 * (dif1 - 0.2 * dif1)
    assert abs(res[0]["red_area"] - expect_hist1) < 1e-9
    assert res[0]["green_area"] == 0.0 and res[0]["divergence"] is False
    # 常数序列 → DIF=DEA=0，柱恒 0，无背驰
    res2 = macd_divergence([5.0] * 20,
                           [{"dir": "up", "hi": 5.0, "lo": 4.0,
                             "start_bar": 0, "end_bar": 19}])
    assert res2[0]["red_area"] == 0.0 and res2[0]["green_area"] == 0.0


# ---------------- 12/13. MACD 背驰检出 / 不检出（构造用例，数值预验证） ----------------

@test
def test_macd_divergence_up():
    # 检出：前笔强攻 10→30（红柱大），后笔仅缓涨创新高 30→31（红柱小）→ 背驰
    c1 = list(np.linspace(10, 30, 31)) + list(np.linspace(30.0, 31.0, 25))
    stks1 = [{"dir": "up", "hi": 30.0, "lo": 10.0, "start_bar": 0, "end_bar": 30},
             {"dir": "up", "hi": 31.0, "lo": 29.0, "start_bar": 30, "end_bar": 55}]
    r1 = macd_divergence(c1, stks1)
    assert [x["divergence"] for x in r1] == [False, True]
    assert r1[1]["red_area"] < r1[0]["red_area"]
    assert stks1[1]["hi"] > stks1[0]["hi"]  # 后笔价格极值更极端
    # 不检出：前笔缓涨 10→12（红柱小），后笔陡涨创新高 12→30（红柱更大）
    c2 = list(np.linspace(10, 12, 31)) + list(np.linspace(12.0, 30.0, 25))
    stks2 = [{"dir": "up", "hi": 12.0, "lo": 10.0, "start_bar": 0, "end_bar": 30},
             {"dir": "up", "hi": 30.0, "lo": 11.5, "start_bar": 30, "end_bar": 55}]
    r2 = macd_divergence(c2, stks2)
    assert [x["divergence"] for x in r2] == [False, False]
    assert r2[1]["red_area"] > r2[0]["red_area"]


@test
def test_macd_divergence_down():
    # 检出：前笔深跌 30→12（绿柱大），后笔阴跌新低 12→11（绿柱小）→ 背驰
    c1 = list(np.linspace(30, 12, 31)) + list(np.linspace(12.0, 11.0, 25))
    stks1 = [{"dir": "down", "hi": 30.0, "lo": 12.0, "start_bar": 0, "end_bar": 30},
             {"dir": "down", "hi": 13.0, "lo": 11.0, "start_bar": 30, "end_bar": 55}]
    r1 = macd_divergence(c1, stks1)
    assert [x["divergence"] for x in r1] == [False, True]
    assert r1[1]["green_area"] < r1[0]["green_area"]
    assert stks1[1]["lo"] < stks1[0]["lo"]  # 后笔价格极值更极端
    # 不检出：前笔阴跌 20→19（绿柱小），后笔暴跌新低 19→5（绿柱更大）
    c2 = list(np.linspace(20, 19, 31)) + list(np.linspace(19.0, 5.0, 25))
    stks2 = [{"dir": "down", "hi": 20.0, "lo": 19.0, "start_bar": 0, "end_bar": 30},
             {"dir": "down", "hi": 19.5, "lo": 5.0, "start_bar": 30, "end_bar": 55}]
    r2 = macd_divergence(c2, stks2)
    assert [x["divergence"] for x in r2] == [False, False]
    assert r2[1]["green_area"] > r2[0]["green_area"]


# ---------------- 14. 确认 bar 越界从严（dates 与 bar 序不一致即报错） ----------------

@test
def test_thirdbuy_confirm_bar_guard():
    strokes = [
        _mkstroke("up", 10.0, 13.0, 0, 4, 0, 6, ef_pi=4, ef_conf_bar=6),
        _mkstroke("down", 11.0, 13.0, 4, 10, 6, 12, ef_pi=10, ef_conf_bar=12),
        _mkstroke("up", 11.0, 12.5, 10, 14, 12, 20, ef_pi=14, ef_conf_bar=20),
        _mkstroke("down", 12.8, 14.0, 14, 22, 20, 30, ef_pi=22, ef_conf_bar=30),
        _mkstroke("up", 12.8, 14.5, 22, 30, 30, 44, ef_pi=30, ef_conf_bar=44),
        _mkstroke("down", 13.2, 14.5, 30, 38, 44, 50, ef_pi=38, ef_conf_bar=50),
    ]
    pvs = build_pivots(strokes)
    try:
        third_buys(strokes, pvs, make_dates(5))  # dates 短于确认 bar 50
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


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
