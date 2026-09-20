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
    """Fix-2 步骤 3.5 → W-B7 更新：premarket.refresh_bond_etf 只调国债 fetcher
    （ETF 份额源不存在已显式摘除），失败不抛（pipeline 降级继续）。"""
    from pipeline import premarket
    called = []
    orig_by, orig_es = macro.fetch_bond_yield, macro.fetch_etf_share
    try:
        macro.fetch_bond_yield = lambda *a, **kw: called.append("bond")
        macro.fetch_etf_share = lambda *a, **kw: called.append("etf")
        premarket.refresh_bond_etf()
        assert called == ["bond"], "W-B7 后步骤3.5不应再调 fetch_etf_share: %s" % called
        # 失败不抛
        def _boom(*a, **kw):
            raise ConnectionError("接口冷却")
        macro.fetch_bond_yield = _boom
        premarket.refresh_bond_etf()
    finally:
        macro.fetch_bond_yield = orig_by
        macro.fetch_etf_share = orig_es


# ============================================================
# 批次3b · P1-9：index_valuation 加 valuation_mode 列（临时库验证）
# 生产库不执行迁移（待授权清单①）——本节全部用 :memory:/临时库。
# ============================================================

_OLD_VALUATION_DDL = """
CREATE TABLE IF NOT EXISTS index_valuation (
    index_code TEXT, trade_date TEXT,
    pe REAL, pe_pct REAL, pb REAL, pb_pct REAL, close REAL,
    PRIMARY KEY (index_code, trade_date)
);
"""


def _old_schema_conn() -> sqlite3.Connection:
    """模拟未授权加列的老库：手工建 7 列 index_valuation + 其余表走 DDL。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    conn.execute("DROP TABLE index_valuation")
    conn.executescript(_OLD_VALUATION_DDL)
    conn.commit()
    return conn


@test
def test_p1c9_old_schema_not_auto_migrated_and_ensure_is_idempotent():
    """正+反（迁移）：init_db 对老库**不**自动加列（生产加列待授权的机制保证）；
    ensure_valuation_mode_column 首调加列、再调跳过（幂等），存量数据保留。"""
    from data.fetcher import ensure_valuation_mode_column
    conn = _old_schema_conn()
    try:
        conn.execute("INSERT INTO index_valuation VALUES "
                     "('000300','2026-09-11',12,0.5,1.3,0.6,4000)")
        conn.commit()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(index_valuation)")}
        assert "valuation_mode" not in cols, "init_db 不得对老库自动加列"
        assert ensure_valuation_mode_column(conn) is True, "首调应实际加列"
        cols2 = {r[1] for r in conn.execute("PRAGMA table_info(index_valuation)")}
        assert "valuation_mode" in cols2
        row = conn.execute("SELECT pe, close FROM index_valuation"
                           " WHERE index_code='000300'").fetchone()
        assert row == (12.0, 4000.0), "存量数据必须保留"
        assert ensure_valuation_mode_column(conn) is False, "二调必须跳过（幂等）"
        # 新 DDL 库（已带列）→ ensure 直接 False
        conn2 = _mem_conn()
        try:
            assert ensure_valuation_mode_column(conn2) is False
        finally:
            conn2.close()
    finally:
        conn.close()


@test
def test_p1c9_write_valuation_marks_mode_fallback_and_real():
    """正用例（写入口径标注）：价格分位兜底行 valuation_mode='price_fallback'
    （pe/pb NULL）；真实口径行 'real'。"""
    conn = _mem_conn()  # 新 DDL：已带 valuation_mode 列
    try:
        rows = [("2026-01-05", 3200.0), ("2026-01-06", 3250.0)]
        conn.executemany(
            "INSERT OR REPLACE INTO index_daily VALUES ('000300',?,3200,3200,3200)",
            [(d, ) for d, _ in rows])
        conn.commit()
        n = macro._price_percentile_fallback("000300", conn, years=5)
        assert n == 2
        r = conn.execute(
            "SELECT pe, pe_pct, pb, pb_pct, valuation_mode FROM index_valuation"
            " ORDER BY trade_date").fetchall()
        assert all(x[0] is None and x[2] is None for x in r), "兜底行 pe/pb 必须为 NULL"
        assert all(x[1] is not None for x in r), "pe_pct=价格分位"
        assert {x[4] for x in r} == {macro.VALUATION_MODE_FALLBACK}
        # 真实口径
        macro._write_valuation("000905", conn, {"2026-01-06": (25.0, 0.4, 2.1, 0.3, 5100.0)},
                               mode=macro.VALUATION_MODE_REAL)
        got = conn.execute("SELECT pe, valuation_mode FROM index_valuation"
                           " WHERE index_code='000905'").fetchone()
        assert got == (25.0, macro.VALUATION_MODE_REAL), got
    finally:
        conn.close()


@test
def test_p1c9_write_valuation_old_schema_compat():
    """反用例（兼容）：老库（未加列）写路径自动退回 7 列模式，零行为变化。"""
    conn = _old_schema_conn()
    try:
        n = macro._write_valuation("000300", conn,
                                   {"2026-01-06": (25.0, 0.4, 2.1, 0.3, 5100.0)})
        assert n == 1
        row = conn.execute("SELECT pe, pe_pct, pb, pb_pct, close FROM index_valuation"
                           ).fetchone()
        assert row == (25.0, 0.4, 2.1, 0.3, 5100.0), row
    finally:
        conn.close()


@test
def test_p1c9_bundle_macro_fallback_disclosure():
    """正用例（bundle 降级披露）：兜底行透出 valuation_mode='price_fallback' 并
    生成 macro_fallback 披露；markdown 数值旁标「价格分位兜底」。真实口径行不受
    影响；老库（无 mode 列）读取不崩、不产出兜底披露。"""
    from ai.bundle import bundle_to_markdown, build_bundle
    conn = _mem_conn()
    try:
        macro._write_valuation("000300", conn,
                               {"2026-09-11": (None, 0.62, None, None, 4000.0)},
                               mode=macro.VALUATION_MODE_FALLBACK)
        macro._write_valuation("000905", conn,
                               {"2026-09-11": (25.0, 0.4, 2.1, 0.3, 5100.0)},
                               mode=macro.VALUATION_MODE_REAL)
        conn.commit()
        b = build_bundle(run_date="2026-09-11", conn=conn)
        assert b["macro"]["000300"]["valuation_mode"] == "price_fallback"
        assert b["macro"]["000905"]["valuation_mode"] == "real"
        assert b["macro_fallback"]["indices"] == ["000300"], b.get("macro_fallback")
        assert "兜底口径" in b["macro_fallback"]["note"]
        md = bundle_to_markdown(b)
        assert "价格分位兜底" in md and "兜底（价格分位）" in md
        assert "真实（PE历史分位）" in md
    finally:
        conn.close()
    # 老库（无 mode 列）：读取降级不崩、无兜底披露
    old = _old_schema_conn()
    try:
        old.execute("INSERT INTO index_valuation VALUES "
                    "('000300','2026-09-11',NULL,0.62,NULL,NULL,4000)")
        old.commit()
        b2 = build_bundle(run_date="2026-09-11", conn=old)
        assert "valuation_mode" not in b2["macro"]["000300"]
        assert "macro_fallback" not in b2
        md2 = bundle_to_markdown(b2)
        assert "兜底（价格分位）" not in md2
    finally:
        old.close()


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
