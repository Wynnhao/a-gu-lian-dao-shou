"""P2 信号层测试：因子函数合成数据边界（长度不足/恒定序列/除零/分位边界）+ 真实 DB compute_all 输出合规。"""
import os
import sys
import tempfile
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

# 规则20/打分降权经 read_factor_crowding() 读 logs/signal_eval/factor_crowding.json；
# 不隔离时会读到**生产**拥挤状态（2026-09-17 起生产 crowded=true），且 compute_all
# 会把合成状态写回生产目录 → 打分/降权用例必败 + 生产文件被测试覆盖。
# 指向空沙箱目录，缺文件 → crowded=False（本文件内显式沙箱的用例会各自覆盖并恢复）。
os.environ.setdefault("AGSICKLE_SIGNAL_EVAL_DIR",
                      tempfile.mkdtemp(prefix="agsickle_signals_se_"))

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
    assert limit_pct("689009") == 0.20   # 科创CDR：68 前缀覆盖
    assert limit_pct("600519") == 0.10
    assert limit_pct("000001") == 0.10
    assert limit_pct("830799") == 0.30   # 北交所（与 risk.engine 口径对齐）
    assert limit_pct("920002") == 0.30


# ---------- score profiles（reversal_lowvol 截面 / momentum 时序） ----------

def _synth_bars(closes, days=None, code="600519", turnover=1.0):
    """由收盘序列构造升序 daily_bar DataFrame（high/low ±1%）。"""
    from datetime import date, timedelta
    d0 = date(2025, 1, 1)
    n = len(closes)
    return pd.DataFrame({
        "code": code,
        "trade_date": [(d0 + timedelta(days=i)).isoformat() for i in range(n)],
        "open": [c * 0.999 for c in closes], "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes], "close": closes,
        "volume": [1000.0] * n, "amount": [c * 1000 for c in closes],
        "pct_chg": [0.0] * n, "turnover": [turnover] * n,
    })


def test_rank01_basics():
    """min-max rank 归一：单调、并列平均秩、单元素中性 0.5。"""
    from signals.signals import _rank01
    s = pd.Series([0.10, 0.30, 0.20], index=["a", "b", "c"])
    r = _rank01(s)
    assert abs(r["a"] - 0.0) < 1e-9 and abs(r["b"] - 1.0) < 1e-9 \
        and abs(r["c"] - 0.5) < 1e-9
    # 并列取平均秩：两个最小值 → 秩 1.5 → (1.5-1)/3 = 1/6
    s2 = pd.Series([1.0, 1.0, 2.0, 3.0])
    r2 = _rank01(s2)
    assert abs(r2.iloc[0] - 1.0 / 6) < 1e-9 and abs(r2.iloc[1] - 1.0 / 6) < 1e-9
    # 单元素截面 → 中性
    r3 = _rank01(pd.Series([7.0], index=["x"]))
    assert abs(r3["x"] - 0.5) < 1e-9


def test_score_xs_reversal_prefers_dip_in_cross_section():
    """截面打分：同池中短期暴跌票 score 应高于短期暴涨票（反转因子主导）。

    与 config.signals.profile 当前值解耦：通过环境变量 AGSICKLE_SIGNALS_PROFILE
    临时强制 reversal_lowvol 走截面打分路径，测反转偏好。不持久化。
    """
    import os
    os.environ["AGSICKLE_SIGNALS_PROFILE"] = "reversal_lowvol"
    try:
        import signals.signals as sig
        assert sig.profile() == "reversal_lowvol"
        base = [100.0] * 70
        pool = pd.concat([
            _synth_bars(base + [115.0] * 5, code="600519"),
            _synth_bars(base + [85.0] * 5, code="000001"),
        ], ignore_index=True)
        up = sig.compute_signal("600519", pool=pool)
        down = sig.compute_signal("000001", pool=pool)
        assert 0.0 <= up["score"] <= 1.0 and 0.0 <= down["score"] <= 1.0
        assert down["score"] > up["score"], (down["score"], up["score"])
        # 截面 rank 已写入 JSON
    finally:
        del os.environ["AGSICKLE_SIGNALS_PROFILE"]
    assert "score_parts" in down["signals"]
    assert down["signals"]["score_parts"]["rev_rank"] > \
        up["signals"]["score_parts"]["rev_rank"]


def test_score_xs_missing_factor_renormalizes():
    """缺某因子 → 该因子权重剔除重归一化：score 等于可用因子 rank 的加权和。"""
    import signals.signals as sig
    m5 = pd.Series({"a": -0.10, "b": 0.0, "c": 0.10})
    atrp = pd.Series({"a": 0.02, "b": 0.03, "c": 0.04})
    turn20 = pd.Series({"a": None, "b": 1.0, "c": 2.0}, dtype=float)
    score, parts = sig.score_reversal_lowvol_xs(m5, atrp, turn20)
    # a 缺换手：反转 rank 1.0 + 低波 rank 1.0 → (0.40+0.35)/0.75 = 1.0
    assert abs(score["a"] - 1.0) < 1e-9
    # c：反转 rank 0、低波 rank 0、换手最高 rank 0 → 0.0
    assert abs(score["c"] - 0.0) < 1e-9
    # b：三因子全有 → 0.40×0.5 + 0.35×0.5 + 0.25×1.0（换手最低 rank 1）= 0.625
    assert abs(score["b"] - 0.625) < 1e-9


def test_score_xs_single_stock_neutral():
    """单票截面无排序信息 → 中性 0.5（旧 clip 口径在单票上会给出极端分，已修）。"""
    import signals.signals as sig
    score, _ = sig.score_reversal_lowvol_xs(
        pd.Series({"a": -0.2}), pd.Series({"a": 0.01}), pd.Series({"a": 0.5}))
    assert abs(score["a"] - 0.5) < 1e-9


def test_score_momentum_profile_prefers_surged():
    """momentum profile（时序口径）：暴涨票 score 应高于暴跌票（与反转相反）。"""
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


def test_atr_uses_full_qfq_ohlc_when_available():
    """ATR 口径：high/low/close 前复权齐全 → 全复权（atr_basis=qfq）；
    混用 raw H/L 与复权 C 的旧做法在除权日产生假 TR，已修。"""
    import signals.signals as sig
    bars = _synth_bars([100.0] * 40)
    bars["close_qfq"] = bars["close"]
    bars["high_qfq"] = bars["high"]
    bars["low_qfq"] = bars["low"]
    h, l, c, basis = sig._factor_ohlc(bars)
    assert basis == "qfq"
    # 部分缺失 → 整体回退不复权
    bars2 = bars.copy()
    bars2.loc[bars2.index[:20], "high_qfq"] = None
    h2, l2, c2, basis2 = sig._factor_ohlc(bars2)
    assert basis2 == "raw"


def test_atr_series_matches_scalar_atr():
    """atr_series 与标量 atr 同种子同递推：末值一致。"""
    from signals.factors import atr, atr_series
    rng = np.random.default_rng(11)
    close = 100 + np.cumsum(rng.normal(0, 1, 120))
    high, low = close + 1.0, close - 1.0
    s = atr_series(high, low, close, 14)
    assert s is not None and s.iloc[:14].isna().all() and not math.isnan(s.iloc[14])
    assert abs(float(s.iloc[-1]) - atr(high, low, close, 14)) < 1e-12


def test_backfill_history_synthetic_db():
    """全史回填：逐日重算并入 signal 表，行数=交易日×票数，幂等重跑覆盖。"""
    import sqlite3
    import signals.signals as sig
    from data.fetcher import DDL
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    from datetime import date, timedelta
    d0 = date(2025, 1, 1)
    dates = [(d0 + timedelta(days=i)).isoformat() for i in range(40)]
    for code, drift in (("600519", 0.05), ("000001", -0.05)):
        closes = [100.0]
        for i in range(39):
            closes.append(closes[-1] * (1 + (drift if i % 3 else -drift / 2)))
        for i, d in enumerate(dates):
            c = closes[i]
            conn.execute(
                "INSERT INTO daily_bar (code, trade_date, open, high, low, close,"
                " volume, amount, pct_chg, turnover) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (code, d, c * 0.999, c * 1.01, c * 0.99, c, 1000.0, c * 1000, 0.0, 1.0))
        conn.execute("INSERT INTO stock_info VALUES (?,?,?,?)",
                     (code, code, (d0 - timedelta(days=200)).isoformat(), "x"))
    conn.commit()
    n = sig.backfill_history(conn=conn)
    assert n == 40 * 2
    rows = conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0]
    assert rows == n
    # 幂等：重跑行数不变
    n2 = sig.backfill_history(conn=conn)
    assert n2 == n and conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0] == n
    # 每日截面分数合法
    lo, hi = conn.execute("SELECT MIN(score), MAX(score) FROM signal").fetchone()
    assert 0.0 <= lo <= 1.0 and 0.0 <= hi <= 1.0
    conn.close()


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
    # compute_all 内部会写 factor_crowding.json → K3 沙箱隔离，不碰生产目录
    old, _sandbox = _sandbox_signal_eval_dir()
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
    # 合成库用 000001（不在 config.watchlist_core 51 只核心内）；compute_all 默认
    # watchlist_only=True 会按 watchlist_core 过滤掉它 → len=1≠2。本测试验证的是
    # compute_all 机器本身（因子/打分/写库），与 watchlist 过滤无关，故显式走全 universe。
    results = compute_all(conn=conn, watchlist_only=False)
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
            assert v is None or isinstance(v, (int, float, str, bool, dict, list))
        assert res["signals"]["ma_trend"] in ("up", "down", "flat")
        assert isinstance(res["signals"]["above_ma60"], bool)
        assert type(res["score"]) is float
    # 与 signal 表内容一致
    got = conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0]
    assert got == len(results)
    conn.close()
    _restore_signal_eval_dir(old)


# ============================================================
# Sprint 1 任务 5：因子拥挤熔断（factor_crowding.json 落盘）
# ============================================================

def _sandbox_signal_eval_dir():
    """K3 测试隔离：factor_crowding/signal_eval 落盘指到临时沙箱目录，
    返回旧环境变量值供恢复。生产 logs/signal_eval/ 不得被测试触碰。"""
    import os
    import tempfile
    sandbox = tempfile.mkdtemp(prefix="agsickle_signal_eval_test_")
    old = os.environ.get("AGSICKLE_SIGNAL_EVAL_DIR")
    os.environ["AGSICKLE_SIGNAL_EVAL_DIR"] = sandbox
    return old, sandbox


def _restore_signal_eval_dir(old):
    import os
    if old is None:
        os.environ.pop("AGSICKLE_SIGNAL_EVAL_DIR", None)
    else:
        os.environ["AGSICKLE_SIGNAL_EVAL_DIR"] = old


def _synth_signal_bar_env():
    """构造 6 个月 × 5 只票的合成 signal + daily_bar（让 rolling IC 算出有意义数值）。

    关键约束：
    - 每只票每月插足够多行（30+ 行），让 pct_change(5).shift(-5) 在 group 内能算出非 NaN
    - signal.as_of 与 daily_bar.trade_date 同格式（YYYY-MM-DD），merge 用 inner join 命中
    - 每月每只票插一行 signal（as_of=该月 1 日），让 bucket 切月能拿到 5-6 个 bucket
    """
    from data.fetcher import DDL
    conn = __import__("sqlite3").connect(":memory:")
    conn.executescript(DDL)
    import random
    random.seed(42)
    codes = ["600519", "000001", "300750", "002415", "688041"]
    months = ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
    for ci, code in enumerate(codes):
        base = 10 + ci * 5
        for mi, m in enumerate(months):
            # 让 score 与未来 5 日收益挂钩（强 IC）：score 越高 → 价格越高
            score = 0.3 + ci * 0.1 + mi * 0.05  # 票间有差异
            px = base + mi * 2 + score * 10  # 分数高的票价格更高 → score 与 fwd5 正相关
            for d in range(30):
                dt = f"{m}-{d+1:02d}"
                pxd = px + d * 0.05 + random.uniform(-0.1, 0.1)
                conn.execute(
                    "INSERT INTO daily_bar (code, trade_date, open, high, low, close,"
                    " volume, amount, pct_chg, turnover) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (code, dt, pxd, pxd + 0.5, pxd - 0.5, pxd, 10000, pxd * 10000,
                     0.1, 1.0))
            conn.execute(
                "INSERT INTO signal (code, as_of, signals, score) VALUES (?,?,?,?)",
                (code, f"{m}-05", "{}", score))
    conn.commit()
    return conn


def test_factor_crowding_persists_file():
    """任务 5：_write_factor_crowding 把 μ60/σ60/crowded 写入 factor_crowding.json（沙箱目录）。"""
    from signals import signals as sig
    old, _sandbox = _sandbox_signal_eval_dir()
    # W-B1 后 IC 按 profile() 过滤；合成行未显式给 profile（列缺省
    # 'reversal_lowvol'）→ 用环境变量把当前 profile 对齐到合成数据口径
    old_prof = os.environ.get("AGSICKLE_SIGNALS_PROFILE")
    os.environ["AGSICKLE_SIGNALS_PROFILE"] = "reversal_lowvol"
    conn = _synth_signal_bar_env()
    try:
        out = sig._write_factor_crowding(conn)
        # 字段齐全
        assert "mu60" in out and "sigma60" in out and "crowded" in out
        # 文件已落盘（沙箱内）
        path = sig._factor_crowding_path()
        assert str(path).startswith(_sandbox)
        assert path.exists()
        import json as _json
        loaded = _json.loads(path.read_text(encoding="utf-8"))
        assert loaded["generated_at"]
        # 合成数据 bucket >= 4，μ60/σ60 必有数值
        assert loaded["n_buckets"] >= 4
    finally:
        conn.close()
        _restore_signal_eval_dir(old)
        if old_prof is None:
            os.environ.pop("AGSICKLE_SIGNALS_PROFILE", None)
        else:
            os.environ["AGSICKLE_SIGNALS_PROFILE"] = old_prof


def test_factor_crowding_empty_signal_returns_no_crowd():
    """信号表为空时：crowded=False + reason 说明，不抛异常。"""
    from data.fetcher import DDL
    from signals import signals as sig
    old, _sandbox = _sandbox_signal_eval_dir()
    conn = __import__("sqlite3").connect(":memory:")
    conn.executescript(DDL)
    try:
        out = sig._write_factor_crowding(conn)
        assert out["crowded"] is False
        assert "为空" in out["reason"]
        # 文件仍落盘（覆盖式，沙箱内）
        assert sig._factor_crowding_path().exists()
    finally:
        conn.close()
        _restore_signal_eval_dir(old)


def test_rule20_factor_crowding_caps_buy_weight():
    """任务 5 风控 case：factor_crowding.json 标记 crowded=True →
    buy + target_weight=0.20 → 自动压回 5%（按 equity 重算股数）。"""
    from signals import signals as sig
    old, _sandbox = _sandbox_signal_eval_dir()
    try:
        # 写一份假的 crowded 状态（沙箱内，不碰生产文件）
        sig._persist_factor_crowding({
            "generated_at": "2026-09-16T10:00:00",
            "mu60": 0.001, "sigma60": 0.05,
            "n_buckets": 12, "crowded": True,
            "reason": "测试：手工标记拥挤",
        })
        # 跑规则 20
        import risk.engine as eng
        ctx = eng.mk_ctx() if hasattr(eng, "mk_ctx") else eng.RiskContext(
            now=__import__("datetime").datetime(2026, 9, 16, 10, 0, 0),
            positions={}, cash=1000000.0, total_equity=1000000.0,
            latest_prices={"600519": 1500.0}, prev_close={"600519": 1490.0})
        d = {"action": "buy", "code": "600519", "target_weight": 0.20,
             "confidence": 0.8, "reasons": ["r1", "r2"], "risk_notes": [],
             "order": {"side": "buy", "price": 1500.0, "shares": 100}}
        v = eng.check(d, ctx, eng.load_cfg())
        # 5% 上限 = 50000 元，50000/1500=33.33 股，//100*100=0 股 → 应被拒
        # 改用更小价格让整手规整不为 0
        d2 = {"action": "buy", "code": "600519", "target_weight": 0.20,
              "confidence": 0.8, "reasons": ["r1", "r2"], "risk_notes": [],
              "order": {"side": "buy", "price": 100.0, "shares": 2000}}
        v2 = eng.check(d2, ctx, eng.load_cfg())
        # 5% 上限 = 50000 元，50000/100=500 股，//100*100=500 股 → adjusted_order
        assert v2.adjusted_order is not None
        assert v2.adjusted_order["shares"] == 500
        assert any("因子拥挤熔断" in w for w in v2.warnings), v2.warnings
        # d1 价格 1500 → 5% 等价股数=0，应 violations
        assert any("因子拥挤熔断" in s and "不足一手" in s
                   for s in v.violations), v.violations
    finally:
        # 沙箱随环境变量恢复一并弃用，无需清理生产文件
        _restore_signal_eval_dir(old)


# ============================================================
# Sprint 2 任务 2（P1-4）：业绩预告关键词事件通道
# ============================================================

def test_earnings_compute_counts_pos_neg_and_writes_table():
    """signals.earnings.compute_earnings_events：扫描 news 表正/负面命中，
    net 写入 news_earnings 表。"""
    from signals import earnings
    from data.fetcher import DDL
    conn = __import__("sqlite3").connect(":memory:")
    conn.executescript(DDL)
    today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
    # 600519 两条正面；000001 一条正面 + 一条负面；300750 仅负面
    conn.execute(
        "INSERT INTO news (code, title, content, source, url, published_at, fetched_at)"
        " VALUES (?,?,?,?,?,?,?)",
        ("600519", "贵州茅台业绩预增", "公司预计上半年净利润同比增长30%",
         "em", "u1", today + "T09:30:00", today))
    conn.execute(
        "INSERT INTO news (code, title, content, source, url, published_at, fetched_at)"
        " VALUES (?,?,?,?,?,?,?)",
        ("600519", "业绩快报良好", "营收超预期",
         "em", "u2", today + "T10:30:00", today))
    conn.execute(
        "INSERT INTO news (code, title, content, source, url, published_at, fetched_at)"
        " VALUES (?,?,?,?,?,?,?)",
        ("000001", "平安银行业绩预增", "增长稳健", "em", "u3", today + "T11:00:00", today))
    conn.execute(
        "INSERT INTO news (code, title, content, source, url, published_at, fetched_at)"
        " VALUES (?,?,?,?,?,?,?)",
        ("000001", "但市场担忧业绩变脸", "存在不确定性", "em", "u4", today + "T12:00:00", today))
    conn.execute(
        "INSERT INTO news (code, title, content, source, url, published_at, fetched_at)"
        " VALUES (?,?,?,?,?,?,?)",
        ("300750", "宁德时代业绩变脸", "下修预警", "em", "u5", today + "T13:00:00", today))
    conn.commit()
    out = earnings.compute_earnings_events(conn, days=3, min_net_score=2)
    # 600519: net=+2（预增 + 良好 - 0 = 2 + 2）但具体由关键词匹配决定
    assert "600519" in out
    assert out["600519"]["positive"] >= 2
    assert out["600519"]["negative"] == 0
    assert out["600519"]["net"] >= 2
    # 300750 净分为负
    assert "300750" in out
    assert out["300750"]["net"] <= -1
    # news_earnings 表必须写入（按 min_net_score=2 过滤，但有写入）
    n = conn.execute("SELECT COUNT(*) FROM news_earnings").fetchone()[0]
    assert n > 0
    conn.close()


def test_earnings_no_keyword_returns_empty_out():
    """news 表里全是中性新闻 → out 为空 dict，不写 news_earnings。"""
    from signals import earnings
    from data.fetcher import DDL
    conn = __import__("sqlite3").connect(":memory:")
    conn.executescript(DDL)
    today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO news (code, title, content, source, url, published_at, fetched_at)"
        " VALUES (?,?,?,?,?,?,?)",
        ("600519", "茅台召开股东大会", "常规披露",
         "em", "u1", today + "T09:30:00", today))
    conn.commit()
    out = earnings.compute_earnings_events(conn, days=3, min_net_score=2)
    assert out == {}
    n = conn.execute("SELECT COUNT(*) FROM news_earnings").fetchone()[0]
    assert n == 0
    conn.close()


def test_earnings_low_net_filtered_from_table_but_in_out():
    """净分 1（不达 min_net_score=2 阈值）→ out 仍返回，但不写 news_earnings 表。"""
    from signals import earnings
    from data.fetcher import DDL
    conn = __import__("sqlite3").connect(":memory:")
    conn.executescript(DDL)
    today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
    # 单条正/负相抵 → net=0；但 min_net_score=2 阈值会过滤写入
    conn.execute(
        "INSERT INTO news (code, title, content, source, url, published_at, fetched_at)"
        " VALUES (?,?,?,?,?,?,?)",
        ("600519", "业绩预增", "增长", "em", "u1", today + "T09:30:00", today))
    conn.execute(
        "INSERT INTO news (code, title, content, source, url, published_at, fetched_at)"
        " VALUES (?,?,?,?,?,?,?)",
        ("600519", "不及预期", "下滑", "em", "u2", today + "T10:30:00", today))
    conn.commit()
    out = earnings.compute_earnings_events(conn, days=3, min_net_score=2)
    assert "600519" in out
    assert out["600519"]["net"] == 0
    # 阈值过滤 → 表里无写入
    n = conn.execute("SELECT COUNT(*) FROM news_earnings").fetchone()[0]
    assert n == 0
    conn.close()


def test_earnings_scan_text_no_pos_substring_collision():
    """K1 子串碰撞回归：'净利润同比下滑' 不得命中正面词（旧表 '净利润同比' 是其前缀），
    应判为纯负面（pos=0 且 neg≥1）。"""
    from signals import earnings
    pos, neg, _, _ = earnings._scan_text("净利润同比下滑30%")
    assert pos == 0
    assert neg >= 1
    # 正面表述仍要命中
    pos2, neg2, _, _ = earnings._scan_text("净利润同比增长30%")
    assert pos2 >= 1
    assert neg2 == 0
    # 下降表述命中负面
    _, neg3, _, _ = earnings._scan_text("净利润同比下降，业绩预亏")
    assert neg3 >= 1


# ============================================================
# Fix-3：规则 20 权重降级 + 退出滞回状态机
# ============================================================

def test_crowding_hysteresis_state_machine():
    """滞回状态机：触发 / 滞回带不翻转 / 清零回迁 / 计满退出。"""
    from signals import signals as sig
    # off + hit（μ<0.005 且 σ>0.02）→ active
    s = sig._crowding_next_state({"state": "off"}, 0.001, 0.05, "2026-09-17")
    assert s["state"] == "active" and s["active_since"] == "2026-09-17"
    assert s["cooling_count"] == 0
    # active + μ 落滞回带 [0.005, 0.015) → 保持 active 不翻转
    s = sig._crowding_next_state(s, 0.010, 0.03, "d2")
    assert s["state"] == "active"
    # active + μ≥0.015 → cooling（count=1）
    s = sig._crowding_next_state(s, 0.020, 0.01, "d3")
    assert s["state"] == "cooling" and s["cooling_count"] == 1
    # cooling + μ 再跌破 0.005 → 清零回 active（保留原 active_since）
    s = sig._crowding_next_state(s, 0.002, 0.05, "d4")
    assert s["state"] == "active" and s["cooling_count"] == 0
    assert s["active_since"] == "2026-09-17"
    # cooling 计满 5 次 → off
    s = sig._crowding_next_state({"state": "cooling", "cooling_count": 4,
                                  "active_since": "d1"}, 0.020, 0.01, "d9")
    assert s["state"] == "off" and s["cooling_count"] == 0
    # off + μ 落滞回带 → 仍 off（滞回带不触发）
    s = sig._crowding_next_state(s, 0.010, 0.03, "d10")
    assert s["state"] == "off"


def test_crowding_weight_downgrade_changes_scores():
    """权重降级：state=active → 降权生效（score 变化、低波票上位至并列）
    + crowding_downgraded 注记；cooling 不降级。

    三票对称构造（turn 同 rank=0.5）：
    A rev=1.0/vol=0.0；B rev=0.0/vol=1.0；C 全 0.5。
    正常权 0.40/0.35/0.25 → A>C>B；降权 0.20/0.20/0.15（归一 /0.55）→ A=B=C，
    低波票 B 从末位升至并列第一。
    """
    from signals import signals as sig

    def _rows():
        return [
            {"code": "A", "factors": {"mom_5d": -0.10, "atr_pct": 0.05,
                                      "turn20": 2.0}, "signals": {}},
            {"code": "B", "factors": {"mom_5d": 0.10, "atr_pct": 0.01,
                                      "turn20": 2.0}, "signals": {}},
            {"code": "C", "factors": {"mom_5d": 0.0, "atr_pct": 0.03,
                                      "turn20": 2.0}, "signals": {}},
        ]

    orig = sig.read_factor_crowding
    try:
        # 正常权重基线（必须显式打桩非拥挤：生产 factor_crowding.json 自
        # 2026-09-17 起 crowded=true，不打桩时基线即被降权 → A=B=C 并列必败；
        # 模块级沙箱只保证目录隔离，不保证状态为空——pytest 同进程内更早的
        # 用例可能已向沙箱写入拥挤状态）
        sig.read_factor_crowding = lambda: {"crowded": False, "reason": "基线：非拥挤"}
        rows = _rows()
        sig._score_cross_section(rows)
        sc = {r["code"]: r["score"] for r in rows}
        assert sc["A"] > sc["C"] > sc["B"], sc
        assert abs(sc["A"] - sc["B"]) > 0.04
        assert all(r["signals"]["score_parts"]["crowding_downgraded"] is False
                   for r in rows)
        # active：降级 → A=B=C，低波票上位
        sig.read_factor_crowding = lambda: {"state": "active", "crowded": True}
        rows = _rows()
        sig._score_cross_section(rows)
        sc2 = {r["code"]: r["score"] for r in rows}
        assert abs(sc2["A"] - sc2["B"]) < 1e-9, sc2
        assert abs(sc2["A"] - sc2["C"]) < 1e-9, sc2
        assert all(r["signals"]["score_parts"]["crowding_downgraded"] is True
                   for r in rows)
        # cooling：crowded 仍 True（规则 20 仓位压制）但权重已恢复
        sig.read_factor_crowding = lambda: {"state": "cooling", "crowded": True}
        rows = _rows()
        sig._score_cross_section(rows)
        sc3 = {r["code"]: r["score"] for r in rows}
        assert sc3 == sc
        assert all(r["signals"]["score_parts"]["crowding_downgraded"] is False
                   for r in rows)
    finally:
        sig.read_factor_crowding = orig


def test_crowding_state_transition_writes_risk_event_dedup():
    """state 迁移写 risk_event（rule=factor_crowding_state），同日同迁移去重。"""
    from data.fetcher import DDL
    from signals import signals as sig
    conn = __import__("sqlite3").connect(":memory:")
    conn.executescript(DDL)
    try:
        sig._record_crowding_state_event(conn, "off", "active", 0.001)
        sig._record_crowding_state_event(conn, "off", "active", 0.0012)  # 去重
        n1 = conn.execute("SELECT COUNT(*) FROM risk_event WHERE"
                          " rule='factor_crowding_state'").fetchone()[0]
        assert n1 == 1
        sig._record_crowding_state_event(conn, "active", "cooling", 0.02)
        n2 = conn.execute("SELECT COUNT(*) FROM risk_event WHERE"
                          " rule='factor_crowding_state'").fetchone()[0]
        assert n2 == 2
        # 两条迁移的 detail 均在库（同秒写入 ts 相同，按内容断言）
        details = " | ".join(r[0] for r in conn.execute(
            "SELECT detail FROM risk_event WHERE rule='factor_crowding_state'"))
        assert "off→active" in details and "active→cooling" in details
    finally:
        conn.close()


def test_write_factor_crowding_keeps_state_when_data_unavailable():
    """计算不可用（bucket 不足）时保留旧 state——熔断不因一次失败意外解除。"""
    from signals import signals as sig
    old, _sandbox = _sandbox_signal_eval_dir()
    conn = __import__("sqlite3").connect(":memory:")
    from data.fetcher import DDL
    conn.executescript(DDL)
    try:
        # 预置 active 状态
        sig._persist_factor_crowding({"state": "active", "crowded": True,
                                      "cooling_count": 0,
                                      "active_since": "2026-09-01"})
        out = sig._write_factor_crowding(conn)  # 空 signal 表 → 早退
        assert out["state"] == "active"
        assert out["crowded"] is True
        assert "保留旧 state" in out["reason"]
    finally:
        conn.close()
        _restore_signal_eval_dir(old)


# ============================================================
# Fix-5（D3）：v2 五因子 profile + signal 表 profile 列隔离
# ============================================================

def test_v2_score_orders_low_ivol_low_max():
    """v2 五因子：五维各取极值的票排序符合"反转+低波+低换手+低IVOL+低MAX"预期。"""
    import pandas as pd
    from signals.signals import score_reversal_lowvol_v2_xs
    # A：全面最优（跌最深/波动最低/换手最低/IVOL 最低/MAX 最低）
    # B：全面最差；C：居中 → 序 A > C > B
    n = 5
    m5 = pd.Series({"A": -0.10, "B": 0.10, "C": 0.0, "D": 0.02, "E": -0.02})
    atrp = pd.Series({"A": 0.01, "B": 0.08, "C": 0.04, "D": 0.05, "E": 0.03})
    turn20 = pd.Series({"A": 0.5, "B": 9.0, "C": 3.0, "D": 4.0, "E": 2.0})
    ivol = pd.Series({"A": 0.005, "B": 0.05, "C": 0.02, "D": 0.03, "E": 0.015})
    max5 = pd.Series({"A": 0.01, "B": 0.09, "C": 0.04, "D": 0.06, "E": 0.02})
    score, parts = score_reversal_lowvol_v2_xs(m5, atrp, turn20, ivol, max5)
    assert score["A"] > score["C"] > score["B"]
    assert parts.loc["A", "ivol"] == 1.0 and parts.loc["B", "ivol"] == 0.0
    assert parts.loc["A", "maxr"] == 1.0 and parts.loc["B", "maxr"] == 0.0
    # 单票异象敏感性：IVOL 相同的两票由其余四因子决定相对序
    ivol2 = ivol.copy()
    ivol2["D"] = 0.02  # 与 C 并列
    score2, _ = score_reversal_lowvol_v2_xs(m5, atrp, turn20, ivol2, max5)
    assert abs(score2["D"] - score2["C"]) < 1e-9 or score2["D"] != score["D"]


def test_v2_score_missing_factor_renormalizes():
    """v2 缺因子：某票缺 IVOL/MAX → 权重剔除重归一化；全缺 → 0.5。"""
    import pandas as pd
    from signals.signals import score_reversal_lowvol_v2_xs
    m5 = pd.Series({"A": -0.10, "B": 0.10, "C": 0.0})
    atrp = pd.Series({"A": 0.01, "B": 0.08, "C": 0.04})
    turn20 = pd.Series({"A": 0.5, "B": 9.0, "C": 3.0})
    # 仅 A 有 IVOL/MAX（B 缺失）：A 的 ivol/max rank 单元素截面 → 中性 0.5
    ivol = pd.Series({"A": 0.005, "B": None}, dtype=float)
    max5 = pd.Series({"A": 0.01, "B": None}, dtype=float)
    score, parts = score_reversal_lowvol_v2_xs(m5, atrp, turn20, ivol, max5)
    assert pd.isna(parts.loc["B", "ivol"]) and pd.isna(parts.loc["B", "maxr"])
    # B: (0.30×0 + 0.20×0 + 0.15×0)/0.65 = 0
    # A: (0.30×1 + 0.20×1 + 0.15×1 + 0.20×0.5 + 0.15×0.5)/1.0 = 0.825
    assert abs(score["B"] - 0.0) < 1e-9
    assert abs(score["A"] - 0.825) < 1e-9
    # 全因子全缺的票：_rank01 dropna 后整行退出截面（与 v1 行为一致，不参与排序）
    score_all_na, _ = score_reversal_lowvol_v2_xs(
        pd.Series({"X": None, "Y": 0.0}, dtype=float),
        pd.Series({"X": None, "Y": 0.03}, dtype=float),
        pd.Series({"X": None, "Y": 2.0}, dtype=float),
        pd.Series({"X": None, "Y": 0.02}, dtype=float),
        pd.Series({"X": None, "Y": 0.04}, dtype=float))
    assert "X" not in score_all_na.index and abs(score_all_na["Y"] - 0.5) < 1e-9


def test_signal_profile_isolation_v1_v2():
    """profile 列隔离：v1/v2 行同 (code, as_of) 并存互不覆盖；读取按 profile 过滤。"""
    from data.fetcher import DDL
    from signals import signals as sig
    conn = __import__("sqlite3").connect(":memory:")
    conn.executescript(DDL)
    orig_profile = sig.profile
    sig.profile = lambda: "reversal_lowvol"
    try:
        for prof, score in (("reversal_lowvol", 0.7),
                            ("reversal_lowvol_v2", 0.3)):
            conn.execute(
                "INSERT OR REPLACE INTO signal (code, as_of, signals, score,"
                " profile) VALUES (?,?,?,?,?)",
                ("600519", "2026-09-16", "{}", score, prof))
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM signal WHERE code='600519'"
                         ).fetchone()[0]
        assert n == 2  # 并存，不互相 REPLACE
        # 过滤读取：当前 profile=v1 → 只取 v1 行
        from review.signal_eval import _signal_frame
        df_v1 = _signal_frame(conn)
        assert len(df_v1) == 1 and abs(df_v1.iloc[0]["score"] - 0.7) < 1e-9
        # 切 v2 → 只取 v2 行
        sig.profile = lambda: "reversal_lowvol_v2"
        df_v2 = _signal_frame(conn)
        assert len(df_v2) == 1 and abs(df_v2.iloc[0]["score"] - 0.3) < 1e-9
    finally:
        sig.profile = orig_profile
        conn.close()


# ============================================================
# 主入口在文件最末尾（追加新测试后必须保持在最后）
# ============================================================


# ============================================================
# Sprint 2 附任务（P1-2）：IVOL + MAX(5) 因子单元测试
# ============================================================

def test_ivol_perfect_tracking_is_zero():
    """个股与基准完全同步（同收益率序列）→ 残差 σ ≈ 0（浮点容差 1e-12）。"""
    from signals.factors import ivol
    close = [100.0, 101.0, 100.5, 102.0, 103.0, 101.5,
             103.5, 104.0, 103.0, 105.0, 106.0,
             107.0, 106.0, 108.0, 109.0, 108.5,
             110.0, 111.0, 110.5, 112.0, 113.0, 114.0]
    got = ivol(close, close, window=20)
    assert got is not None and abs(got) < 1e-12, f"残差应≈0，实得 {got}"


def test_ivol_intercept_strips_systematic_drift():
    """Fix-6：带截距回归——个股 = α + β·基准 + 白噪 → 残差 σ 与手算一致，
    且小于无截距口径（系统性日均漂移被剔除）。"""
    import numpy as np
    from signals.factors import ivol
    rng = np.random.default_rng(42)
    mr = rng.normal(0.0, 0.01, 20)                 # 基准日收益
    eps = np.array([0.001 if i % 2 == 0 else -0.001 for i in range(20)])
    sr = 0.002 + 1.5 * mr + eps                    # α=0.002, β=1.5
    s = [100.0]
    m = [1000.0]
    for r in sr:
        s.append(s[-1] * (1 + r))
    for r in mr:
        m.append(m[-1] * (1 + r))
    got = ivol(s, m, window=20)
    beta_h, alpha_h = np.polyfit(mr, sr, 1)  # polyfit 返回 [斜率, 截距]
    resid = sr - alpha_h - beta_h * mr
    want = float(np.std(resid, ddof=1))
    assert got is not None and abs(got - want) < 1e-12
    # 无截距口径会把 α 漂移并入残差，σ 更大（漂移未剔除）
    beta_nc = float(np.cov(sr, mr, ddof=1)[0, 1] / np.var(mr, ddof=1))
    want_nc = float(np.std(sr - beta_nc * mr, ddof=1))
    assert got <= want_nc + 1e-15


def test_ivol_insufficient_returns_none():
    """序列不足 window+1 → None。"""
    from signals.factors import ivol
    assert ivol([1.0, 2.0], [1.0, 2.0], window=20) is None
    assert ivol([1.0] * 25, [1.0] * 10, window=20) is None


def test_max_ret_bali_picks_top5_mean():
    """Bali MAX(5)：已知收益序列 → top5 均值精确匹配。"""
    from signals.factors import max_ret_bali
    # 构造 21 个收盘价：前 20 日收益已知
    rets = [0.01, -0.02, 0.03, 0.05, -0.01, 0.02, 0.04, -0.03,
            0.01, 0.06, -0.02, 0.02, 0.01, 0.03, -0.01, 0.04,
            0.02, -0.01, 0.05, 0.08]
    close = [100.0]
    for r in rets:
        close.append(close[-1] * (1 + r))
    got = max_ret_bali(close, window=20, top_k=5)
    top5 = sorted(rets)[-5:]           # [0.04, 0.05, 0.05, 0.06, 0.08]
    want = sum(top5) / 5
    assert abs(got - want) < 1e-9, f"got={got}, want={want}"


def test_max_ret_bali_insufficient_returns_none():
    """序列不足 → None。"""
    from signals.factors import max_ret_bali
    assert max_ret_bali([1.0, 1.1], window=20) is None


def test_compute_signal_writes_ivol_max_fields():
    """compute_signal 把 ivol_20d / max_ret_5_20d 写入 signals JSON。

    pool 传单票 DataFrame（与既有 test_score_xs 用法一致）。
    """
    from signals import signals as sig
    closes = list(np.linspace(10, 15, 40))   # 40 日上涨
    bars = _synth_bars(closes)
    res = sig.compute_signal("600519", pool=bars)
    assert res is not None
    g = res["signals"]
    # 字段必须存在（值可能为 None——真实库 HS300 与合成序列长度对齐后可算）
    assert "ivol_20d" in g
    assert "max_ret_5_20d" in g
    # max_ret_5_20d 一定可算（不依赖基准）：单调上涨 → 全正收益，top5 均值 > 0
    assert g["max_ret_5_20d"] is not None and g["max_ret_5_20d"] > 0


# ============================================================
# 主入口（真正放最后）
# ============================================================

if __name__ == "__main__":
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
