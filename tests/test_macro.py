"""宏观数据层测试：国债 + ETF 三档兜底 + call_ak 熔断 + 落盘（全离线 mock 网络）。"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import sqlite3
from datetime import datetime, timedelta

from data.fetcher import DDL, init_db, get_conn
from data import macro


_TESTS = []


def test(fn):
    _TESTS.append(fn)
    return fn


test.__test__ = False  # pytest 不要把装饰器本身当测试收集


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    return conn


def _mock_bond_zh_us_rate(n=25):
    """构造 akshare bond_zh_us_rate 返回值（DataFrame 含 10年 列）。"""
    import pandas as pd
    dates = pd.date_range(end=datetime(2026, 9, 16), periods=n, freq="B")
    rows = []
    base = 2.50
    for i, d in enumerate(dates):
        # 模拟下行：最近 20 日下降 25bp
        y = base - i * 0.0125 if i < 20 else base - 20 * 0.0125
        rows.append({"日期": d.strftime("%Y-%m-%d"), "10年": round(y, 4)})
    return pd.DataFrame(rows)


def _mock_fund_etf_fund_info_em(etf_code, n=25):
    """构造 akshare fund_etf_fund_info_em 返回值。"""
    import pandas as pd
    dates = pd.date_range(end=datetime(2026, 9, 16), periods=n, freq="B")
    rows = []
    base = 1_000_000.0 if etf_code == "510300" else 800_000.0
    for i, d in enumerate(dates):
        s = base * (1 + i * 0.001) if etf_code == "510300" else base * (1 - i * 0.002)
        rows.append({"净值日期": d.strftime("%Y-%m-%d"), "份额": round(s, 2)})
    return pd.DataFrame(rows)


# ---------------- fetch_bond_yield ----------------

@test
def test_fetch_bond_yield_writes_with_delta():
    """em 源成功 → 写入 index_bond_yield，每行 delta_20d_bp 计算正确。"""
    conn = _mem_conn()
    try:
        # mock call_ak
        orig = macro.call_ak
        macro.call_ak = lambda source, fn, *a, **kw: _mock_bond_zh_us_rate(25)
        try:
            out = macro.fetch_bond_yield(conn=conn)
        finally:
            macro.call_ak = orig
        assert "10Y_CN" in out
        n = out["10Y_CN"]
        assert n == 25
        rows = conn.execute(
            "SELECT trade_date, yield, delta_20d_bp, source"
            " FROM index_bond_yield ORDER BY trade_date").fetchall()
        assert len(rows) == 25
        # 前 20 行 delta 应为 None（窗口不足）
        assert rows[0][2] is None
        # 第 21 行开始有 delta，最近 delta 应接近 -25bp
        # （base 2.50 → base - 20*0.0125 = 2.25，差 25bp）
        last_delta = rows[-1][2]
        assert last_delta is not None and -30 <= last_delta <= -20
    finally:
        conn.close()


@test
def test_fetch_bond_yield_em_fail_fallback_to_tx():
    """em 源失败 → 尝试 tx 源；tx 也失败 → 返回空 dict 不抛。"""
    conn = _mem_conn()
    try:
        orig = macro.call_ak
        macro.call_ak = lambda source, fn, *a, **kw: (_ for _ in ()).throw(
            ConnectionError("em 冷却"))
        try:
            out = macro.fetch_bond_yield(conn=conn)
        finally:
            macro.call_ak = orig
        # tx 也无数据 → 返回 {}，不抛
        assert out == {}
        n = conn.execute("SELECT COUNT(*) FROM index_bond_yield").fetchone()[0]
        assert n == 0
    finally:
        conn.close()


# ---------------- fetch_etf_share ----------------

@test
def test_fetch_etf_share_writes_with_pct_chg():
    """em 源成功 → 写入 index_etf_share，pct_chg_1d 计算正确（首行为 None）。"""
    conn = _mem_conn()
    try:
        orig = macro.call_ak
        macro.call_ak = lambda source, fn, *a, **kw: _mock_fund_etf_fund_info_em("510300", 25)
        try:
            out = macro.fetch_etf_share(["510300"], conn=conn)
        finally:
            macro.call_ak = orig
        assert out.get("510300", 0) == 25
        rows = conn.execute(
            "SELECT trade_date, share, pct_chg_1d, source"
            " FROM index_etf_share ORDER BY trade_date").fetchall()
        assert len(rows) == 25
        # 首行 pct_chg 为 None（无前值）
        assert rows[0][2] is None
        # 第二行开始有 pct
        assert rows[1][2] is not None
        assert rows[1][2] > 0   # 510300 单调递增 → 正 pct
    finally:
        conn.close()


@test
def test_fetch_etf_share_em_fail_returns_empty():
    """em 源失败且 tx 端点不存在 → 不抛异常，返回空。"""
    conn = _mem_conn()
    try:
        orig = macro.call_ak
        macro.call_ak = lambda source, fn, *a, **kw: (_ for _ in ()).throw(
            ConnectionError("em 冷却"))
        try:
            out = macro.fetch_etf_share(["510300"], conn=conn)
        finally:
            macro.call_ak = orig
        # 510300 全源失败 → 不在结果中
        assert "510300" not in out
        n = conn.execute("SELECT COUNT(*) FROM index_etf_share").fetchone()[0]
        assert n == 0
    finally:
        conn.close()


# ---------------- _bond_etf_signals（bundle 读）----------------

@test
def test_bond_etf_signals_reads_latest():
    """_bond_etf_signals 读 index_bond_yield + index_etf_share 最新一行。"""
    conn = _mem_conn()
    try:
        conn.execute(
            "INSERT INTO index_bond_yield VALUES ('10Y_CN','2026-09-16',2.50,-20.0,'em')")
        conn.execute(
            "INSERT INTO index_etf_share VALUES ('510300','2026-09-16',1000000.0,-3.5,'em')")
        conn.execute(
            "INSERT INTO index_etf_share VALUES ('510500','2026-09-16',800000.0,1.0,'em')")
        conn.commit()
        from ai.bundle import _bond_etf_signals
        out = _bond_etf_signals(conn)
        assert out["bond_yield"]["delta_20d_bp"] == -20.0
        assert out["etf_share"]["510300"]["pct_chg_1d"] == -3.5
        assert out["etf_share"]["510500"]["pct_chg_1d"] == 1.0
    finally:
        conn.close()


@test
def test_bond_etf_signals_empty_db():
    """空表 → bond_yield/etf_share 均为空 dict，reason 有说明。"""
    conn = _mem_conn()
    try:
        from ai.bundle import _bond_etf_signals
        out = _bond_etf_signals(conn)
        assert out["bond_yield"] == {}
        assert out["etf_share"] == {}
        assert "均为空" in out.get("reason", "")
    finally:
        conn.close()


@test
def test_bond_etf_signals_only_etf_present():
    """只填 ETF 不填国债：bond_yield 为空 dict，etf_share 有数据。"""
    conn = _mem_conn()
    try:
        conn.execute(
            "INSERT INTO index_etf_share VALUES ('510300','2026-09-16',1000000.0,0.5,'em')")
        conn.commit()
        from ai.bundle import _bond_etf_signals
        out = _bond_etf_signals(conn)
        assert out["bond_yield"] == {}
        assert "510300" in out["etf_share"]
    finally:
        conn.close()


@test
def test_macro_main_calls_two_fetchers():
    """main() 调用 fetch_bond_yield + fetch_etf_share（不抛异常；mock 覆盖）。

    注：macro.main() 同时调 fetch_index_daily + fetch_index_valuation，但这两条
    路径未 mock（会出网），所以本测试只验证我们 Sprint 2 新加的两路调用。
    """
    conn = _mem_conn()
    try:
        orig_call_ak = macro.call_ak
        # 只 mock 我们关心的两个 source（bond/etf），其他 source 让其抛错也行
        def _route(source, fn, *a, **kw):
            if "bond" in source:
                return _mock_bond_zh_us_rate(25)
            if "etf" in source:
                return _mock_fund_etf_fund_info_em(a[0] if a else "510300", 25)
            return (_ for _ in ()).throw(ConnectionError("non-mock source %s" % source))
        macro.call_ak = _route
        # 直接调 Sprint 2 新加的两路（绕开 fetch_index_daily 的真实 akshare 调用）
        try:
            out_bond = macro.fetch_bond_yield(conn=conn)
            out_etf = macro.fetch_etf_share(conn=conn)
        finally:
            macro.call_ak = orig_call_ak
        assert out_bond.get("10Y_CN", 0) > 0
        assert "510300" in out_etf and out_etf["510300"] > 0
    finally:
        conn.close()


@test
def test_premarket_refresh_bond_etf_calls_both_and_never_raises():
    """Fix-2 步骤 3.5：premarket.refresh_bond_etf 依次调用两个 fetcher，
    单路失败不抛（pipeline 降级继续）。"""
    from pipeline import premarket
    called = []
    orig_by, orig_es = macro.fetch_bond_yield, macro.fetch_etf_share
    try:
        macro.fetch_bond_yield = lambda *a, **kw: called.append("bond")
        macro.fetch_etf_share = lambda *a, **kw: called.append("etf")
        premarket.refresh_bond_etf()
        assert called == ["bond", "etf"]
        # 单路失败不阻断另一路
        def _boom(*a, **kw):
            raise ConnectionError("接口冷却")
        macro.fetch_bond_yield = _boom
        called.clear()
        premarket.refresh_bond_etf()
        assert called == ["etf"]
    finally:
        macro.fetch_bond_yield = orig_by
        macro.fetch_etf_share = orig_es


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
