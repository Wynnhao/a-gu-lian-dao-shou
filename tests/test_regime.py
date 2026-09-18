"""市场环境总闸测试：RSRS/二八/波动率目标/ATR 止损线的合成数据回归（全离线）。"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import sqlite3

import numpy as np
import pandas as pd

from data.fetcher import DDL
from risk.regime import (compute_regime, compute_vol_target, dual_momentum,
                         position_cap, rsrs_z, stop_loss_line)

CFG = {"risk": {"max_total_weight": 0.80},
       "regime": {"enabled": True, "rsrs_min_beta": 250},
       "vol_target": {"enabled": True, "min_samples": 21}}

D0 = pd.Timestamp("2023-01-02")


def _dates(n: int) -> list:
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(D0, periods=n)]


def _seed_index(conn, code: str, closes: list, highs=None, lows=None):
    dates = _dates(len(closes))
    for i, d in enumerate(dates):
        c = float(closes[i])
        h = float(highs[i]) if highs is not None else None
        l = float(lows[i]) if lows is not None else None
        conn.execute(
            "INSERT OR REPLACE INTO index_daily (index_code, trade_date, close, high, low)"
            " VALUES (?,?,?,?,?)", (code, d, c, h, l))
    conn.commit()


def _seed_portfolio(conn, totals: list):
    dates = _dates(len(totals))
    for d, t in zip(dates, totals):
        conn.execute(
            "INSERT OR REPLACE INTO portfolio_state (date, cash, market_value, total,"
            " drawdown, kill_switch, note) VALUES (?,?,?,?,?,?,?)",
            (d, 0.0, float(t), float(t), None, 0, ""))
    conn.commit()


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    return conn


# ---------------- rsrs_z / dual_momentum 纯函数 ----------------


def test_rsrs_z_insufficient_returns_none():
    n = 100
    rng = np.random.default_rng(3)
    low = pd.Series(100 + np.abs(rng.normal(0, 1, n)).cumsum())
    high = low + 1.0
    z, beta = rsrs_z(high, low, window=18, zwindow=600, min_beta=250)
    assert z is None


def test_rsrs_z_random_walk_returns_finite():
    rng = np.random.default_rng(5)
    n = 900
    low = pd.Series(100 + np.abs(rng.normal(0, 0.5, n)).cumsum())
    high = low + np.abs(rng.normal(1.0, 0.2, n))
    z, beta = rsrs_z(high, low, window=18, zwindow=600, min_beta=250)
    assert z is not None and np.isfinite(z)
    assert beta is not None and beta > 0


def test_dual_momentum_signs():
    up = pd.Series(np.linspace(100, 130, 60))
    down = pd.Series(np.linspace(130, 100, 60))
    mb, ms = dual_momentum(up, down, window=20)
    assert mb > 0 and ms < 0
    # 样本不足
    assert dual_momentum(up.iloc[:5], down.iloc[:5], window=20) == (None, None)


# ---------------- compute_regime ----------------


def test_regime_no_data_is_noop():
    conn = _mem_conn()
    out = compute_regime(conn, CFG)
    assert out["cap"] is None and out["tier"] is None
    conn.close()


def test_regime_dual_shelter_close_only():
    """二八皆弱（近20日双跌）→ 避险档 cap=0.2；无 H/L 时 RSRS 不参与。"""
    conn = _mem_conn()
    n = 120
    flat = [1000.0] * (n - 20)
    big = flat + [1000.0 * (1 - 0.01 * i) for i in range(1, 21)]   # 近20日跌
    small = flat + [800.0 * (1 - 0.015 * i) for i in range(1, 21)]
    _seed_index(conn, "000300", big)
    _seed_index(conn, "000905", small)
    out = compute_regime(conn, CFG)
    assert abs(out["cap"] - 0.20) < 1e-9
    assert out["tier"] == "避险"
    assert out["dual_mom"]["big"] < 0 and out["dual_mom"]["small"] < 0
    conn.close()


def test_regime_uptrend_no_constraint():
    """二八皆强（近20日双涨）且无 H/L → 无动态约束。"""
    conn = _mem_conn()
    n = 120
    flat = [1000.0] * (n - 20)
    big = flat + [1000.0 * (1 + 0.01 * i) for i in range(1, 21)]
    small = flat + [800.0 * (1 + 0.015 * i) for i in range(1, 21)]
    _seed_index(conn, "000300", big)
    _seed_index(conn, "000905", small)
    out = compute_regime(conn, CFG)
    assert out["cap"] is None
    conn.close()


def test_regime_with_hl_has_rsrs_detail():
    """有 H/L 数据时输出 RSRS 明细，cap 落在合法集合内。"""
    conn = _mem_conn()
    n = 800
    rng = np.random.default_rng(9)
    low = 100 + np.abs(rng.normal(0, 0.5, n)).cumsum()
    high = low + np.abs(rng.normal(1.0, 0.2, n))
    closes = (high + low) / 2.0
    _seed_index(conn, "000300", list(closes), list(high), list(low))
    _seed_index(conn, "000905", list(closes * 0.8))
    out = compute_regime(conn, CFG)
    assert out["rsrs"] is not None
    assert out["cap"] is None or out["cap"] in (0.20, 0.50, 0.80)
    conn.close()


# ---------------- 波动率目标仓位 ----------------


def test_vol_target_insufficient_samples():
    conn = _mem_conn()
    _seed_portfolio(conn, [1000000.0] * 10)
    out = compute_vol_target(conn, CFG)
    assert out["cap"] is None and "样本" in (out.get("note") or "")
    conn.close()


def test_vol_target_scales_down_in_high_vol():
    """日波动 2.2% → 年化约 35% > 目标 30% → cap = scale × 0.8 < 0.8。"""
    conn = _mem_conn()
    totals = [1000000.0]
    for i in range(24):
        totals.append(totals[-1] * (1.022 if i % 2 == 0 else 0.9782))
    _seed_portfolio(conn, totals)
    out = compute_vol_target(conn, CFG)
    assert out.get("sigma_ann", 0) > 0.30
    assert out["cap"] is not None and 0.4 < out["cap"] < 0.80
    conn.close()


def test_position_cap_combines_min():
    """组合输出 = min(regime cap, vol cap)：避险 0.2 + 波动 cap → 0.2。"""
    conn = _mem_conn()
    n = 120
    flat = [1000.0] * (n - 20)
    _seed_index(conn, "000300", flat + [1000.0 * (1 - 0.01 * i) for i in range(1, 21)])
    _seed_index(conn, "000905", flat + [800.0 * (1 - 0.015 * i) for i in range(1, 21)])
    totals = [1000000.0]
    for i in range(24):
        totals.append(totals[-1] * (1.022 if i % 2 == 0 else 0.9782))
    _seed_portfolio(conn, totals)
    out = position_cap(conn, CFG)
    assert abs(out["cap"] - 0.20) < 1e-9
    assert out["vol_target"]["cap"] is not None
    conn.close()


# ---------------- ATR 自适应止损线 ----------------


def test_stop_loss_line():
    assert abs(stop_loss_line(0.08, 0.07, 2.0) - 0.14) < 1e-9   # 高波 → 2ATR
    assert abs(stop_loss_line(0.08, 0.03, 2.0) - 0.08) < 1e-9   # 低波 → 基础线
    assert abs(stop_loss_line(0.08, None, 2.0) - 0.08) < 1e-9   # 缺失 → 基础线


# ---------------- Sprint 2 任务 1：国债乘子 + ETF 档位调整 ----------------

def _seed_bond(conn, delta_bp: float):
    """向 index_bond_yield 写一行（今日）"""
    conn.execute(
        "INSERT OR REPLACE INTO index_bond_yield"
        " (index_code, trade_date, yield, delta_20d_bp, source) VALUES (?,?,?,?,?)",
        ("10Y_CN", "2026-09-16", 2.50, delta_bp, "em"))
    conn.commit()


def _seed_etf(conn, pct_chg: float):
    """向 index_etf_share 写一行 510300 沪深300ETF（今日）"""
    conn.execute(
        "INSERT OR REPLACE INTO index_etf_share"
        " (etf_code, trade_date, share, pct_chg_1d, source) VALUES (?,?,?,?,?)",
        ("510300", "2026-09-16", 1000000.0, pct_chg, "em"))
    conn.commit()


def test_regime_bond_downside_caps_position():
    """国债 20 日变动 < -15bp → cap × 0.8（避险情绪）"""
    from risk.regime import _compute_bond_yield_modifier
    conn = _mem_conn()
    try:
        _seed_bond(conn, -20.0)
        cfg = {"delta_bp_threshold": -15.0, "downside_mult": 0.8}
        mult = _compute_bond_yield_modifier(conn, cfg)
        assert mult == 0.8
        # compute_regime 集成验证：detail.bond_yield.mult == 0.8
        r = compute_regime(conn, root=CFG)
        assert r["bond_yield"]["mult"] == 0.8
        assert "cap 压至" in r["bond_yield"].get("signal", "")
    finally:
        conn.close()


def test_regime_bond_above_threshold_no_constraint():
    """国债 20 日变动 > -15bp → 乘子 1.0，不约束"""
    from risk.regime import _compute_bond_yield_modifier
    conn = _mem_conn()
    try:
        _seed_bond(conn, -5.0)
        cfg = {"delta_bp_threshold": -15.0, "downside_mult": 0.8}
        assert _compute_bond_yield_modifier(conn, cfg) == 1.0
    finally:
        conn.close()


def test_regime_etf_share_noop_no_source():
    """W-B7（P1-14）：ETF 份额数据源不存在（akshare 无份额列接口）→
    _compute_etf_tier_shift 显式 no-op，恒 direction=None 且注明原因，
    即便表里有历史行也不再出方向信号。"""
    from risk.regime import _compute_etf_tier_shift
    conn = _mem_conn()
    try:
        _seed_etf(conn, -3.5)
        cfg = {"up_pct_threshold": 2.0, "down_pct_threshold": -2.0}
        sig = _compute_etf_tier_shift(conn, cfg)
        assert sig["direction"] is None
        assert sig["pct"] is None
        assert "no-op" in (sig.get("reason") or "") or "数据源" in (sig.get("reason") or "")
        _seed_etf(conn, +5.0)
        assert _compute_etf_tier_shift(conn, cfg)["direction"] is None
    finally:
        conn.close()


def test_regime_etf_share_no_move_returns_none():
    """ETF 510300 单日变动在 ±2% 内 → direction=None（W-B7 后恒 None）"""
    from risk.regime import _compute_etf_tier_shift
    conn = _mem_conn()
    try:
        _seed_etf(conn, 0.5)
        cfg = {"up_pct_threshold": 2.0, "down_pct_threshold": -2.0}
        assert _compute_etf_tier_shift(conn, cfg)["direction"] is None
        _seed_etf(conn, -0.3)
        assert _compute_etf_tier_shift(conn, cfg)["direction"] is None
    finally:
        conn.close()


# ---------------- Fix-2：ETF 升/降档 min-after override ----------------
# RSRS 的 OLS β 对构造方式敏感（渐变/跳变都会失真），改用确定性极强的
# 二八避险（cap 0.2）与国债乘子（0.8×0.8=0.64）作基础 cap 载体，
# ETF override 语义与载体无关。

def _seed_dual_shelter(conn):
    """近 20 日大小盘双跌 → 二八避险档 cap=0.2（无 H/L → RSRS 不参与）。"""
    n = 120
    flat = [1000.0] * (n - 20)
    big = flat + [1000.0 * (1 - 0.01 * i) for i in range(1, 21)]
    small = flat + [800.0 * (1 - 0.015 * i) for i in range(1, 21)]
    _seed_index(conn, "000300", big)
    _seed_index(conn, "000905", small)


def test_regime_etf_upgrades_shelter_to_half():
    """W-B7：ETF 档位信号显式 no-op——即便表里有 +3% 历史行，二八避险 0.2
    不再被 override 提档（原 Fix-2 min-after override 已随数据源下线删除）。"""
    conn = _mem_conn()
    try:
        _seed_dual_shelter(conn)
        _seed_etf(conn, +3.0)
        r = compute_regime(conn, root=CFG)
        assert r["dual_mom"]["big"] < 0 and r["dual_mom"]["small"] < 0
        assert r["etf_share"]["direction"] is None
        assert "no-op" in r["etf_share"]["signal"]
        assert abs(r["cap"] - 0.20) < 1e-9   # cap 不被提档
        assert r["tier"] == "避险"
    finally:
        conn.close()


def test_regime_etf_downgrades_to_half():
    """W-B7：ETF 档位信号显式 no-op——-3% 历史行不再触发降档，cap 保持
    min(caps)=0.64（国债乘子 0.8×静态 0.8）。"""
    conn = _mem_conn()
    try:
        _seed_bond(conn, -20.0)
        _seed_etf(conn, -3.0)
        r = compute_regime(conn, root=CFG)
        assert r["etf_share"]["direction"] is None
        assert abs(r["cap"] - 0.64) < 1e-6   # 不被压到 cap_half
        assert r["tier"] == "半配"
    finally:
        conn.close()


def test_regime_etf_no_signal_no_intervention():
    """ETF 变动在阈值内 → direction=None，cap 保持 min(caps)=0.64 不被干预。"""
    conn = _mem_conn()
    try:
        _seed_bond(conn, -20.0)
        _seed_etf(conn, +0.5)
        r = compute_regime(conn, root=CFG)
        assert r["etf_share"]["direction"] is None
        assert abs(r["cap"] - 0.64) < 1e-6
        assert r["tier"] == "半配"  # 0.64 ∈ [cap_half, static_cap) → 半配
    finally:
        conn.close()


if __name__ == "__main__":
    import traceback
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print("PASS %s" % name)
        except Exception:
            failed += 1
            print("FAIL %s" % name)
            traceback.print_exc()
    print("\n%d/%d tests passed" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
