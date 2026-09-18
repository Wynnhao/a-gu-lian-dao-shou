"""市场宽度/情绪模块测试：em/tx/sina 三档兜底 + 落盘 + regime override（任务 3）。"""
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

os.environ["AGSICKLE_DISABLE_LIVE_QUOTES"] = "1"  # 测试保持离线（禁实时行情出网）

import sqlite3

from data import breadth as breadth_mod
from data.breadth import _fetch_em_breadth, _fetch_sina_breadth, _fetch_tx_breadth, fetch_breadth_daily
from data.fetcher import DDL, init_db, get_conn
from signals.breadth import compute_breadth_factor, read_breadth


_TESTS = []


def test(fn):
    _TESTS.append(fn)
    return fn


test.__test__ = False  # pytest 不要把装饰器本身当测试收集


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    return conn


# ---------------- read_breadth + compute_breadth_factor ----------------

@test
def test_read_breadth_empty_db_returns_reason():
    """空表 → 各指标 None + reason 写明降级路径。"""
    conn = _mem_conn()
    try:
        d = read_breadth(conn)
        assert d["limit_up_count"] is None
        assert d["limit_down_count"] is None
        assert d["breadth_composite"] is None
        assert "空" in d["reason"]
    finally:
        conn.close()


@test
def test_read_breadth_returns_latest_row():
    """填一行 → 返回最新一行的所有字段。"""
    conn = _mem_conn()
    try:
        conn.execute(
            "INSERT INTO breadth_daily VALUES"
            " ('2026-09-16', 35, 0.85, 12, 2.5, -3, -1.5, 'em')")
        conn.commit()
        d = read_breadth(conn)
        assert d["date"] == "2026-09-16"
        assert d["limit_up_count"] == 35
        assert d["limit_up_seal_rate"] == 0.85
        assert d["limit_down_count"] == 12
        assert d["advance_decline_ratio"] == 2.5
        assert d["new_high_minus_new_low"] == -3
        assert d["breadth_composite"] == -1.5
        assert d["source"] == "em"
    finally:
        conn.close()


@test
def test_compute_breadth_factor_no_override_above_threshold():
    """composite >= -2 → override_cap=None（不触发极端避险）。"""
    conn = _mem_conn()
    try:
        conn.execute(
            "INSERT INTO breadth_daily VALUES"
            " ('2026-09-16', 50, 1.0, 5, 3.0, 10, -0.5, 'em')")
        conn.commit()
        bf = compute_breadth_factor(conn, threshold=-2.0, override_cap=0.1)
        assert bf["composite"] == -0.5
        assert bf["override_cap"] is None
    finally:
        conn.close()


@test
def test_compute_breadth_factor_override_below_threshold():
    """composite < -2 → override_cap=0.1（极端避险，cap 压到 10%）。"""
    conn = _mem_conn()
    try:
        conn.execute(
            "INSERT INTO breadth_daily VALUES"
            " ('2026-09-16', 5, 0.3, 80, 0.1, -50, -3.0, 'em')")
        conn.commit()
        bf = compute_breadth_factor(conn, threshold=-2.0, override_cap=0.1)
        assert bf["composite"] == -3.0
        assert bf["override_cap"] == 0.1
        assert "极端避险" in bf["reason"]
    finally:
        conn.close()


# ---------------- _fetch_em_breadth + fetch_breadth_daily mock 路径 ----------------

@test
def test_fetch_breadth_daily_em_source_writes_table():
    """em 主源成功 → 写 breadth_daily。"""
    conn = _mem_conn()
    try:
        # mock call_ak 让 em 返回合成涨停股池
        # 必须 mock data.fetcher.call_ak（breadth 模块从那里 import 的）
        import pandas as pd
        from data import fetcher as fetcher_mod
        orig = fetcher_mod.call_ak
        def _mock(source, fn, *a, **kw):
            if source == "zt_pool_em":
                return pd.DataFrame({"代码": ["000001", "600519", "300750"],
                                     "名称": ["平安银行", "贵州茅台", "宁德时代"],
                                     "封板状态": [1, 1, 1]})
            if source == "dt_pool_em":
                return pd.DataFrame({"代码": ["600036"], "名称": ["招商银行"],
                                     "封板状态": [1]})
            if source == "gdhs_em":
                return pd.DataFrame({"上涨家数": [3000], "下跌家数": [1500]})
            return None
        fetcher_mod.call_ak = _mock
        try:
            out = fetch_breadth_daily("2026-09-16", conn=conn)
        finally:
            fetcher_mod.call_ak = orig
        assert out["source"] == "em", "expected source=em got %s" % out.get("source")
        assert out["limit_up_count"] == 3
        assert out["limit_down_count"] == 1
        # 入库
        row = conn.execute(
            "SELECT limit_up_count, limit_down_count, source FROM breadth_daily"
            " WHERE date='2026-09-16'").fetchone()
        assert row == (3, 1, "em")
    finally:
        conn.close()


@test
def test_fetch_breadth_daily_three_sources_all_fail():
    """em + legu + sina 都抛错；tx 拿不到行情（mock 空）；最终不落数据。"""
    conn = _mem_conn()
    try:
        from data import fetcher as fetcher_mod
        orig = fetcher_mod.call_ak
        fetcher_mod.call_ak = lambda source, fn, *a, **kw: (_ for _ in ()).throw(
            ConnectionError("em 冷却"))
        # W-B6：legu 兜底档走 breadth 模块全局 call_ak，同样置障（否则真出网）
        orig_legu_call = breadth_mod.call_ak
        breadth_mod.call_ak = lambda source, fn, *a, **kw: (_ for _ in ()).throw(
            ConnectionError("legu 冷却"))
        orig_sina = breadth_mod._fetch_sina_breadth
        breadth_mod._fetch_sina_breadth = lambda d: (_ for _ in ()).throw(
            ConnectionError("sina 也挂"))
        # tx 源依赖实时行情 → mock 返回空（模拟行情全挂/离线）
        import data.quotes as quotes_mod
        orig_quotes = quotes_mod.get_live_prices
        quotes_mod.get_live_prices = lambda codes, force=False: {}
        try:
            out = fetch_breadth_daily("2026-09-16", conn=conn)
        finally:
            fetcher_mod.call_ak = orig
            breadth_mod.call_ak = orig_legu_call
            breadth_mod._fetch_sina_breadth = orig_sina
            quotes_mod.get_live_prices = orig_quotes
        # 四档全失败 → 不写库，且不造 0 值假行（W-B6）
        n = conn.execute("SELECT COUNT(*) FROM breadth_daily").fetchone()[0]
        assert n == 0, f"四档全失败不应落库，实得 {n} 行（source={out.get('source')}）"
        assert out.get("limit_up_count") is None, out
    finally:
        conn.close()


# ---------------- Fix-1：真 z-score composite / 冷启动 / 新高新低 / 封板率 ----------------

@test
def test_rolling_zscore_matches_manual_formula():
    """250 日历史 + 当日值 → z 与 numpy 总体标准差手算一致；actual=251。"""
    import numpy as np
    conn = _mem_conn()
    try:
        hist = [40 + (i * 7) % 11 for i in range(250)]
        for i, v in enumerate(hist):
            conn.execute(
                "INSERT INTO breadth_daily (date, limit_up_count) VALUES (?,?)",
                (f"d{i:04d}", v))
        conn.commit()
        cur = 45.0
        z, actual = breadth_mod._rolling_zscore(conn, "limit_up_count", cur, "d0250")
        full = np.array([cur] + hist, dtype=float)
        expected = (cur - full.mean()) / full.std()  # ddof=0 总体标准差
        assert actual == 251
        assert z is not None and abs(z - expected) < 1e-9
    finally:
        conn.close()


@test
def test_rolling_zscore_std_zero_or_insufficient_returns_none():
    """恒定历史（std=0）→ None；样本 <60 → (None, None)。"""
    conn = _mem_conn()
    try:
        for i in range(250):
            conn.execute(
                "INSERT INTO breadth_daily (date, limit_up_count) VALUES (?,?)",
                (f"d{i:04d}", 50))
        conn.commit()
        z, actual = breadth_mod._rolling_zscore(conn, "limit_up_count", 50.0, "d0250")
        assert z is None and actual is None
        # 样本不足：只有 30 行历史 → 31 < 60 → None
        conn2 = _mem_conn()
        try:
            for i in range(30):
                conn2.execute(
                    "INSERT INTO breadth_daily (date, limit_down_count) VALUES (?,?)",
                    (f"d{i:04d}", 5 + i))
            conn2.commit()
            z2, _ = breadth_mod._rolling_zscore(conn2, "limit_down_count", 40.0, "d030")
            assert z2 is None
        finally:
            conn2.close()
    finally:
        conn.close()


@test
def test_composite_cold_start_under_60_is_none():
    """冷启动 <60 样本：composite=None（极端避险档不被假数据误触）。"""
    conn = _mem_conn()
    try:
        for i in range(58):
            conn.execute(
                "INSERT INTO breadth_daily VALUES"
                " (?, 30, 0.7, 10, 1.5, 0, NULL, 'em')", (f"d{i:04d}",))
        conn.commit()
        composite, z_window = breadth_mod._composite_from_z(
            conn, {"limit_up_count": 3, "limit_down_count": 1,
                   "advance_decline_ratio": 2.0}, "d0058")
        assert composite is None
    finally:
        conn.close()


@test
def test_composite_warmup_uses_actual_window_and_weights_renorm():
    """60~249 样本冷启动：用实际窗口算 z；daily_bar 空 → nhl 缺列权重重归一化
    （0.3+0.3+0.2)/0.8，composite 与手算一致；source 标注 z_window。"""
    import numpy as np
    conn = _mem_conn()
    try:
        n_hist = 100
        hist_up = [30 + (i * 5) % 9 for i in range(n_hist)]
        hist_dn = [8 + (i * 3) % 7 for i in range(n_hist)]
        hist_adr = [1.0 + (i % 5) * 0.2 for i in range(n_hist)]
        for i in range(n_hist):
            conn.execute(
                "INSERT INTO breadth_daily VALUES"
                " (?, ?, 0.8, ?, ?, 0, NULL, 'em')",
                (f"d{i:04d}", hist_up[i], hist_dn[i], hist_adr[i]))
        conn.commit()
        cur = {"limit_up_count": 3.0, "limit_down_count": 1.0,
               "advance_decline_ratio": 2.0}  # nhl=None（daily_bar 空）
        composite, z_window = breadth_mod._composite_from_z(conn, cur, "d0100")
        assert z_window == n_hist + 1  # 100 历史 + 当日
        # 手算（重归一化：(0.3*z_up - 0.3*z_dn + 0.2*z_adr)/0.8）

        def _z(vals, cur_v):
            full = np.array([cur_v] + vals, dtype=float)
            return (cur_v - full.mean()) / full.std()

        expected = (0.3 * _z(hist_up, 3.0) - 0.3 * _z(hist_dn, 1.0)
                    + 0.2 * _z(hist_adr, 2.0)) / 0.8
        assert composite is not None
        assert abs(composite - round(expected, 4)) < 1e-6
        # 全流程：source 带 z_window 标注
        import pandas as pd
        from data import fetcher as fetcher_mod
        orig = fetcher_mod.call_ak

        def _mock(source, fn, *a, **kw):
            if source == "zt_pool_em":
                return pd.DataFrame({"代码": ["1", "2", "3"]})
            if source == "dt_pool_em":
                return pd.DataFrame({"代码": ["8"]})
            if source == "gdhs_em":
                return pd.DataFrame({"上涨家数": [200], "下跌家数": [100]})
            return None
        fetcher_mod.call_ak = _mock
        try:
            out = fetch_breadth_daily("d0100", conn=conn)
        finally:
            fetcher_mod.call_ak = orig
        assert out["source"] == "em|z_window=101", out["source"]
        assert out["breadth_composite"] is not None
    finally:
        conn.close()


@test
def test_count_new_high_low_known_data():
    """构造已知新高/新低：A 递增(+1)、B 递减(-1)、C 中间(0)、D 样本不足跳过、
    E 递增(+1) → nhl = +1。"""
    conn = _mem_conn()
    try:
        w = 60
        dates = [f"d{i:04d}" for i in range(w)]
        series = {
            "600001": [10.0 + i for i in range(w)],        # 递增 → 新高
            "600002": [50.0 - i for i in range(w)],        # 递减 → 新低
            "600003": ([20.0 + i * 0.5 for i in range(30)]     # 先涨后跌
                       + [40.0 - i * 0.5 for i in range(30)]),  # 末端 25.5 非极值
            "600005": [5.0 + i * 0.1 for i in range(w)],   # 递增 → 新高
        }
        for code, closes in series.items():
            for d, cl in zip(dates, closes):
                conn.execute(
                    "INSERT INTO daily_bar (code, trade_date, close) VALUES (?,?,?)",
                    (code, d, cl))
        for d in dates[:30]:  # D 只有 30 行 → 不足 60 跳过
            conn.execute(
                "INSERT INTO daily_bar (code, trade_date, close) VALUES (?,?,?)",
                ("600004", d, 8.0))
        conn.commit()
        nhl = breadth_mod._count_new_high_low(conn, window=60)
        assert nhl == 1, nhl
    finally:
        conn.close()


@test
def test_em_seal_rate_from_zhaban_pool():
    """封板率 = 涨停数 / (涨停数 + 炸板数)：3 涨停 + 1 炸板 → 0.75。"""
    conn = _mem_conn()
    try:
        import pandas as pd
        from data import fetcher as fetcher_mod
        orig = fetcher_mod.call_ak

        def _mock(source, fn, *a, **kw):
            if source == "zt_pool_em":
                return pd.DataFrame({"代码": ["1", "2", "3"]})
            if source == "zbgc_em":
                return pd.DataFrame({"代码": ["9"]})
            if source == "dt_pool_em":
                return pd.DataFrame({"代码": ["8"]})
            if source == "gdhs_em":
                return pd.DataFrame({"上涨家数": [100], "下跌家数": [100]})
            return None
        fetcher_mod.call_ak = _mock
        try:
            out = fetch_breadth_daily("2026-09-16", conn=conn)
        finally:
            fetcher_mod.call_ak = orig
        assert out["limit_up_count"] == 3
        assert out["limit_up_seal_rate"] == 0.75, out["limit_up_seal_rate"]
    finally:
        conn.close()


@test
def test_em_seal_rate_none_when_zhaban_pool_fails():
    """炸板池接口失败 → seal_rate=None（不硬编码 1.0）。"""
    conn = _mem_conn()
    try:
        import pandas as pd
        from data import fetcher as fetcher_mod
        orig = fetcher_mod.call_ak

        def _mock(source, fn, *a, **kw):
            if source == "zt_pool_em":
                return pd.DataFrame({"代码": ["1", "2", "3"]})
            if source == "zbgc_em":
                raise ConnectionError("炸板池接口冷却")
            if source == "dt_pool_em":
                return pd.DataFrame({"代码": ["8"]})
            if source == "gdhs_em":
                return pd.DataFrame({"上涨家数": [100], "下跌家数": [100]})
            return None
        fetcher_mod.call_ak = _mock
        try:
            out = fetch_breadth_daily("2026-09-16", conn=conn)
        finally:
            fetcher_mod.call_ak = orig
        assert out["limit_up_count"] == 3
        assert out["limit_up_seal_rate"] is None
    finally:
        conn.close()


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
