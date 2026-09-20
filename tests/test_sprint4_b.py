"""Sprint4 批次B（数据正确性）专项测试：W-B1/B2/B3/B4①/B6/B9。

全部离线（:memory: / monkey-patch），不连真实 market.db、不出网。
直跑：python3 tests/test_sprint4_b.py
"""
import os
import sqlite3
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

# 沙箱：signal_eval 目录（factor_crowding.json）与日志目录，不碰生产
os.environ.setdefault("AGSICKLE_SIGNAL_EVAL_DIR",
                      tempfile.mkdtemp(prefix="agsickle_b_se_"))
os.environ.setdefault("AGSICKLE_LOG_DIR",
                      tempfile.mkdtemp(prefix="agsickle_b_logs_"))
os.environ.setdefault("AGSICKLE_DISABLE_LIVE_QUOTES", "1")

from data.fetcher import DDL, init_db  # noqa: E402
from review import daily  # noqa: E402
from signals import signals as sig  # noqa: E402

_TESTS = []


def test(fn):
    _TESTS.append(fn)
    return fn


test.__test__ = False


def _mem() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    return conn


def _sandbox_signal_eval():
    old = os.environ.get("AGSICKLE_SIGNAL_EVAL_DIR")
    os.environ["AGSICKLE_SIGNAL_EVAL_DIR"] = tempfile.mkdtemp(prefix="agsickle_b_se_case_")
    return old


def _restore_signal_eval(old):
    if old is None:
        os.environ.pop("AGSICKLE_SIGNAL_EVAL_DIR", None)
    else:
        os.environ["AGSICKLE_SIGNAL_EVAL_DIR"] = old


# ============================================================
# W-B1：拥挤度 IC 只取当前 profile（P0-2）
# ============================================================

def _seed_ic_env(conn, momentum_scores, reversal_scores):
    """三 profile 混库：momentum 分与 fwd5 完全正相关、reversal 完全负相关、
    v2 噪声。日收益按 code 序号单调 → fwd5 排序恒定 → 设计分即 ±1 RankIC。"""
    codes = ["600001", "600002", "600003", "600004", "600005", "600006"]
    months = ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07"]
    for ci, code in enumerate(codes):
        px = 100.0
        for m in months:
            for d in range(1, 29):
                dt = f"{m}-{d:02d}"
                conn.execute(
                    "INSERT INTO daily_bar (code, trade_date, open, high, low, close,"
                    " volume, amount, pct_chg, turnover) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (code, dt, px, px * 1.01, px * 0.99, px, 10000, px * 1e6, 0.1, 1.0))
                px *= 1.0 + 0.001 * (ci + 1)  # 漂移随 ci 单调 → fwd5 排序恒定
            # 三 profile 并存（旧脏数据形态）
            for prof, scores in (("momentum", momentum_scores),
                                 ("reversal_lowvol", reversal_scores)):
                conn.execute(
                    "INSERT INTO signal (code, as_of, signals, score, profile)"
                    " VALUES (?,?,?,?,?)", (code, f"{m}-01", "{}", scores[ci], prof))
            conn.execute(
                "INSERT INTO signal (code, as_of, signals, score, profile)"
                " VALUES (?,?,?,?,?)", (code, f"{m}-01", "{}", 0.5,
                                        "reversal_lowvol_v2"))
    conn.commit()


@test
def test_wb1_crowding_ic_profile_filtered_momentum():
    """W-B1（P0-2）：profile=momentum 时 IC 只用 momentum 分——与 fwd5 完全正相关
    → mu60≈+1；若混入 reversal（完美负相关）则 mu60 被拖向 0（修复前行为）。"""
    old = _sandbox_signal_eval()
    old_prof = os.environ.get("AGSICKLE_SIGNALS_PROFILE")
    os.environ["AGSICKLE_SIGNALS_PROFILE"] = "momentum"
    conn = _mem()
    try:
        _seed_ic_env(conn,
                     momentum_scores=[0.1 * i for i in range(6)],
                     reversal_scores=[0.1 * (5 - i) for i in range(6)])
        out = sig._write_factor_crowding(conn)
        assert out["mu60"] is not None and out["mu60"] > 0.95, out
        assert out["n_buckets"] >= 4
    finally:
        conn.close()
        _restore_signal_eval(old)
        if old_prof is None:
            os.environ.pop("AGSICKLE_SIGNALS_PROFILE", None)
        else:
            os.environ["AGSICKLE_SIGNALS_PROFILE"] = old_prof


@test
def test_wb1_crowding_ic_profile_filtered_reversal():
    """W-B1：profile=reversal_lowvol 时同一混库给出 ≈−1 的 IC（证明过滤随
    当前 profile 切换，而不是恒取某一种）。"""
    old = _sandbox_signal_eval()
    old_prof = os.environ.get("AGSICKLE_SIGNALS_PROFILE")
    os.environ["AGSICKLE_SIGNALS_PROFILE"] = "reversal_lowvol"
    conn = _mem()
    try:
        _seed_ic_env(conn,
                     momentum_scores=[0.1 * i for i in range(6)],
                     reversal_scores=[0.1 * (5 - i) for i in range(6)])
        out = sig._write_factor_crowding(conn)
        assert out["mu60"] is not None and out["mu60"] < -0.95, out
    finally:
        conn.close()
        _restore_signal_eval(old)
        if old_prof is None:
            os.environ.pop("AGSICKLE_SIGNALS_PROFILE", None)
        else:
            os.environ["AGSICKLE_SIGNALS_PROFILE"] = old_prof


@test
def test_wb1_crowding_state_event_note():
    """W-B1：event_note 透传 state 迁移 risk_event detail（数据修复中间态标注）。"""
    old = _sandbox_signal_eval()
    old_prof = os.environ.get("AGSICKLE_SIGNALS_PROFILE")
    os.environ["AGSICKLE_SIGNALS_PROFILE"] = "momentum"
    conn = _mem()
    try:
        # 预置 prev state=active（旧生产形态），构造强 IC → 触发 active→cooling 迁移
        sig._persist_factor_crowding({"state": "active", "active_since": "2026-09-01",
                                      "cooling_count": 0})
        _seed_ic_env(conn,
                     momentum_scores=[0.1 * i for i in range(6)],
                     reversal_scores=[0.1 * (5 - i) for i in range(6)])
        out = sig._write_factor_crowding(
            conn, event_note="数据修复中间态：qfq 全量重刷后重算")
        assert out["state"] == "cooling", out
        row = conn.execute(
            "SELECT detail FROM risk_event WHERE rule='factor_crowding_state'"
            " ORDER BY id DESC LIMIT 1").fetchone()
        assert row and "数据修复中间态" in row[0], row
    finally:
        conn.close()
        _restore_signal_eval(old)
        if old_prof is None:
            os.environ.pop("AGSICKLE_SIGNALS_PROFILE", None)
        else:
            os.environ["AGSICKLE_SIGNALS_PROFILE"] = old_prof


# ============================================================
# W-B4①：above_ma60 同口径（P2-1）
# ============================================================

@test
def test_wb4_above_ma60_uses_qfq_close():
    """W-B4①：除权票 raw 收盘远低于 qfq 均线、但 qfq 收盘在均线上方 →
    above_ma60 必须按 qfq 收盘比较（修复前 raw 76 < raw 均线口径恒 False）。"""
    import pandas as pd
    rows = []
    for i in range(70):
        if i < 65:
            raw, cq = 100.0, 100.0 + i * 0.01
        else:
            # 除权：raw 跳到 76，qfq 平滑延续
            raw, cq = 76.0 + (i - 65) * 0.1, 100.65 + (i - 65) * 0.1
        rows.append({"trade_date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}",
                     "open": raw, "high": raw * 1.01, "low": raw * 0.99,
                     "close": raw, "volume": 10000.0, "amount": raw * 1e6,
                     "pct_chg": 0.1, "turnover": 1.0, "close_qfq": cq})
    bars = pd.DataFrame(rows)
    r = sig._factor_from_bars(bars, "600519", None)
    assert r is not None
    s = r["signals"]
    assert s["price_basis"] == "qfq"
    assert s["above_ma60"] is True, (s["above_ma60"], s["close"], s["ma60"])
    assert s["close"] == 76.4  # close 字段仍是不复权口径（展示用）；qfq 口径为 101.05


# ============================================================
# W-B3：tx pct_chg 除权修复（P1-10）
# ============================================================

def _seed_tx_rows(conn):
    """000001（tx 源）5 行：末行除权 raw -25% 但 qfq 环比 +1%；另有 1 行无 qfq。"""
    rows = [
        ("000001", "2026-05-10", 10.0, 10.0, 0.0, "tx"),
        ("000001", "2026-05-11", 10.0, 10.0, 0.0, "tx"),
        ("000001", "2026-05-12", 10.0, None, 0.0, "tx"),   # 无 qfq → skip
        ("000001", "2026-05-13", 10.0, 10.0, 0.0, "tx"),
        ("000001", "2026-05-14", 7.5, 10.1, -25.0, "tx"),  # 假暴跌（真值 +1%）
        ("000001", "2026-05-15", 7.6, 10.2, 1.33, "em"),   # em 行不动
    ]
    for code, td, c, cq, pct, src in rows:
        conn.execute(
            "INSERT INTO daily_bar (code, trade_date, open, high, low, close,"
            " volume, amount, pct_chg, turnover, source, close_qfq)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, td, c, c * 1.01, c * 0.99, c, 10000.0, c * 1e6, pct, 1.0,
             src, cq))
    conn.commit()


@test
def test_wb3_recalc_tx_pct_fixes_exdiv_rows():
    """W-B3：tx 行 pct 与 qfq 环比背离 >0.1pp → 重算为 qfq 环比；无 qfq 行跳过
    并计数；em 行不动。"""
    from data.fetcher import recalc_tx_pct
    conn = _mem()
    try:
        _seed_tx_rows(conn)
        fixed, skipped = recalc_tx_pct(conn)
        assert fixed == 1, (fixed, skipped)
        assert skipped >= 2, skipped  # 首行无前驱 + 05-12 无 qfq
        pct = conn.execute(
            "SELECT pct_chg FROM daily_bar WHERE code='000001'"
            " AND trade_date='2026-05-14'").fetchone()[0]
        assert abs(pct - 1.0) < 1e-6, pct  # (10.1/10.0-1)*100
        em_pct = conn.execute(
            "SELECT pct_chg FROM daily_bar WHERE trade_date='2026-05-15'").fetchone()[0]
        assert abs(em_pct - 1.33) < 1e-9  # em 行不被触碰
        # 幂等：再跑一遍 fixed=0
        fixed2, _ = recalc_tx_pct(conn)
        assert fixed2 == 0
    finally:
        conn.close()


@test
def test_wb3_audit_flags_tx_pct_divergent():
    """W-B3：check_db 新增 tx_pct_divergent 类目（超停板豁免的除权假跌也要报）。"""
    from data.audit import check_db
    conn = _mem()
    try:
        _seed_tx_rows(conn)
        issues, total = check_db(conn)
        kinds = [i["kind"] for i in issues]
        assert "tx_pct_divergent" in kinds, kinds
        hit = [i for i in issues if i["kind"] == "tx_pct_divergent"]
        assert hit[0]["code"] == "000001" and hit[0]["date"] == "2026-05-14"
        assert "pct_out_of_range" not in kinds  # 停板豁免仍生效，不重复报
    finally:
        conn.close()


@test
def test_wb3_audit_fix_incages_recalc():
    """W-B3：audit.run(fix=True) 收纳 tx pct 修复（mock get_conn 注入 :memory:，
    close 代理为 no-op 以便事后断言）。"""
    import data.audit as audit_mod
    import data.fetcher as fetcher_mod
    conn = _mem()
    _seed_tx_rows(conn)

    class _NoCloseConn:
        """代理：audit.run 的 finally close() 不作用在 :memory: 连接上。"""

        def __getattr__(self, name):
            return getattr(conn, name)

        def close(self):
            pass

    orig_conn = fetcher_mod.get_conn
    fetcher_mod.get_conn = lambda: _NoCloseConn()
    try:
        r = audit_mod.run(fix=True, backup=False)
        assert "tx_pct_divergent" not in r["by_kind"], r["by_kind"]
        pct = conn.execute(
            "SELECT pct_chg FROM daily_bar WHERE code='000001'"
            " AND trade_date='2026-05-14'").fetchone()[0]
        assert abs(pct - 1.0) < 1e-6
    finally:
        fetcher_mod.get_conn = orig_conn
        conn.close()


# ============================================================
# W-B6：breadth 新鲜度闸门 / legu 兜底 / sina raise / 盘前空池（P1-13 + P2-5）
# ============================================================

@test
def test_wb6_breadth_stale_composite_gated():
    """W-B6（P2-5）：breadth 最新行早于最近 2 个交易日 → composite 按 None 处理
    + reason 注明，极端避险档不被陈旧状态误触。"""
    from signals.breadth import compute_breadth_factor
    conn = _mem()
    try:
        for d in ("2099-01-01", "2099-01-02"):
            conn.execute(
                "INSERT INTO daily_bar (code, trade_date, close) VALUES ('600519',?,10.0)",
                (d,))
        conn.execute(
            "INSERT INTO breadth_daily VALUES"
            " ('2026-09-01', 5, 0.3, 80, 0.1, -50, -3.0, 'em')")  # 陈旧强避险信号
        conn.commit()
        bf = compute_breadth_factor(conn)
        assert bf["composite"] is None and bf["override_cap"] is None, bf
        assert "陈旧" in bf["reason"], bf
        # 新鲜行（>= 地板 2099-01-01）→ 正常生效
        conn.execute(
            "INSERT INTO breadth_daily VALUES"
            " ('2099-01-02', 5, 0.3, 80, 0.1, -50, -3.0, 'em')")
        conn.commit()
        bf2 = compute_breadth_factor(conn)
        assert bf2["composite"] == -3.0 and bf2["override_cap"] == 0.1, bf2
    finally:
        conn.close()


@test
def test_wb6_legu_fallback_source():
    """W-B6（P1-13①）：legu 活跃度接口独立兜底档——上涨/下跌/涨停/跌停家数
    → advance_decline_ratio 不再恒 NULL。"""
    import pandas as pd
    from data import breadth as breadth_mod
    orig = breadth_mod.call_ak

    def _mock(source, fn, *a, **kw):
        assert source == "legu"
        return pd.DataFrame({
            "item": ["上涨", "涨停", "下跌", "跌停", "平盘", "统计日期"],
            "value": [3937.0, 79.0, 1109.0, 1.0, 163.0, "2026-09-18 15:00:00"],
        })

    breadth_mod.call_ak = _mock
    try:
        out = breadth_mod._fetch_legu_breadth("20260918")
        assert out["source"] == "legu"
        assert out["limit_up_count"] == 79
        assert out["limit_down_count"] == 1
        assert abs(out["advance_decline_ratio"] - 3.5509) < 1e-3
    finally:
        breadth_mod.call_ak = orig


@test
def test_wb6_em_source_uses_legu_for_adr():
    """W-B6：em 主源内的涨跌家数改走 legu（原 stock_zh_a_gdhs 股东户数接口
    无涨跌家数列）→ source 标注 em+legu、ratio 有值。"""
    import pandas as pd
    from data import breadth as breadth_mod
    import data.fetcher as fetcher_mod
    orig_b = breadth_mod.call_ak
    orig_f = fetcher_mod.call_ak

    def _mock(source, fn, *a, **kw):
        if source == "zt_pool_em":
            return pd.DataFrame({"代码": ["1", "2"], "名称": ["a", "b"]})
        if source == "zbgc_em":
            return pd.DataFrame({"代码": ["9"]})
        if source == "dt_pool_em":
            return pd.DataFrame({"代码": ["8"]})
        if source == "legu":
            # P2-⑪ 后接口契约含统计日期行（akshare stock_market_activity_legu
            # 恒附 item='统计日期'）；快照日期≠目标日时 em 档不采用其涨跌家数
            return pd.DataFrame({"item": ["上涨", "下跌", "统计日期"],
                                 "value": [3000.0, 1500.0, "2026-09-18 15:00:00"]})
        return None

    # _fetch_em_breadth 函数内 `from data.fetcher import call_ak` 重导入 → 须 patch
    # data.fetcher 侧；_fetch_legu_breadth 用模块全局 → 须 patch breadth 侧
    breadth_mod.call_ak = _mock
    fetcher_mod.call_ak = _mock
    try:
        out = breadth_mod._fetch_em_breadth("20260918")
        assert out["source"] == "em+legu", out
        assert abs(out["advance_decline_ratio"] - 2.0) < 1e-9
        assert out["limit_up_count"] == 2
        assert abs(out["limit_up_seal_rate"] - 2 / 3) < 1e-3
    finally:
        breadth_mod.call_ak = orig_b
        fetcher_mod.call_ak = orig_f


@test
def test_wb6_sina_parse_failure_raises():
    """W-B6（P1-13③）：sina 兜底解析失败必须 raise，不再静默写 0 涨停。"""
    from data import breadth as breadth_mod

    class _FakeResp:
        text = "<html>无法解析的页面，无涨停字样</html>"
        encoding = "utf-8"

    orig_get = breadth_mod.requests.get
    breadth_mod.requests.get = lambda url, timeout: _FakeResp()
    try:
        raised = False
        try:
            breadth_mod._fetch_sina_breadth("20260918")
        except Exception:
            raised = True
        assert raised, "解析失败应 raise 而非静默 0 值"
    finally:
        breadth_mod.requests.get = orig_get


@test
def test_wb6_premarket_empty_pool_not_written():
    """W-B6（P1-13②）：盘前空池（<9:25 且计数全 0）不落 0 值行。"""
    from data import breadth as breadth_mod
    orig_fetch = breadth_mod._fetch_breadth
    orig_guard = breadth_mod._premarket_empty_pool
    breadth_mod._fetch_breadth = lambda d=None: {
        "source": "em", "limit_up_count": 0, "limit_down_count": 0,
        "limit_up_seal_rate": None, "advance_decline_ratio": None,
        "new_high_minus_new_low": None}
    breadth_mod._premarket_empty_pool = lambda d: True
    conn = _mem()
    try:
        out = breadth_mod.fetch_breadth_daily("2099-01-01", conn=conn)
        n = conn.execute("SELECT COUNT(*) FROM breadth_daily").fetchone()[0]
        assert n == 0, "盘前空池不应落库"
        assert out.get("source") == "em"
        # 收盘后时段（守卫 False）→ 正常落库
        breadth_mod._premarket_empty_pool = lambda d: False
        breadth_mod.fetch_breadth_daily("2099-01-01", conn=conn)
        n2 = conn.execute("SELECT COUNT(*) FROM breadth_daily").fetchone()[0]
        assert n2 == 1
    finally:
        breadth_mod._fetch_breadth = orig_fetch
        breadth_mod._premarket_empty_pool = orig_guard
        conn.close()


@test
def test_wb6_all_sources_fail_returns_none_counts():
    """W-B6：四档全失败 → limit_up_count=None（不再造 0 值假行），不落库。"""
    from data import breadth as breadth_mod
    import data.fetcher as fetcher_mod
    import data.quotes as quotes_mod
    orig_b = breadth_mod.call_ak
    orig_f = fetcher_mod.call_ak

    def _throw(source, fn, *a, **kw):
        raise ConnectionError("全源冷却")

    breadth_mod.call_ak = _throw
    fetcher_mod.call_ak = _throw  # _fetch_em_breadth 函数内重导入走这里
    orig_sina = breadth_mod._fetch_sina_breadth
    breadth_mod._fetch_sina_breadth = lambda d: (_ for _ in ()).throw(
        ConnectionError("sina 挂"))
    orig_quotes = quotes_mod.get_live_prices
    quotes_mod.get_live_prices = lambda codes, force=False: {}
    conn = _mem()
    try:
        out = breadth_mod.fetch_breadth_daily("2026-09-16", conn=conn)
        assert out.get("limit_up_count") is None, out
        assert out.get("source") is None
        n = conn.execute("SELECT COUNT(*) FROM breadth_daily").fetchone()[0]
        assert n == 0
    finally:
        breadth_mod.call_ak = orig_b
        fetcher_mod.call_ak = orig_f
        breadth_mod._fetch_sina_breadth = orig_sina
        quotes_mod.get_live_prices = orig_quotes
        conn.close()


# ============================================================
# W-B2：日报基准滞后守卫（P0-5）
# ============================================================

@test
def test_wb2_sec_benchmark_lagging_guard():
    """W-B2（P0-5）：index_daily 缺当日行 → 标注"基准滞后"，相对收益 n/a，
    不再渲染"当日 +0.00%"。"""
    conn = _mem()
    try:
        conn.execute("INSERT INTO index_daily (index_code, trade_date, close)"
                     " VALUES ('000300','2099-01-06',4000.0)")
        conn.execute("INSERT INTO index_daily (index_code, trade_date, close)"
                     " VALUES ('000300','2099-01-07',4020.0)")
        conn.commit()
        text = daily._sec_benchmark(conn, "2099-01-08", 1000.0, 1000000.0)
        assert "基准滞后" in text, text
        assert "2099-01-07" in text
        assert "n/a" in text
        # 基准当日到位 → 正常输出相对收益
        conn.execute("INSERT INTO index_daily (index_code, trade_date, close)"
                     " VALUES ('000300','2099-01-08',4040.0)")
        conn.commit()
        text2 = daily._sec_benchmark(conn, "2099-01-08", 1000.0, 1000000.0)
        assert "沪深300收盘" in text2 and "相对收益" in text2
        assert "+0.00%（" not in text2.split("当日")[1] if "当日" in text2 else True
    finally:
        conn.close()


# ============================================================
# W-B9：早盘误报守卫（P2-10）
# ============================================================

@test
def test_wb9_before_morning_open_window():
    """W-B9：交易日 9:15 前 → True（end 截到昨日防 empty_today 误报）；
    9:15 起 False；周末恒 False。"""
    from data.fetcher import _before_morning_open
    assert _before_morning_open(datetime(2026, 9, 21, 8, 0)) is True   # 周一 08:00
    assert _before_morning_open(datetime(2026, 9, 21, 9, 14)) is True
    assert _before_morning_open(datetime(2026, 9, 21, 9, 15)) is False
    assert _before_morning_open(datetime(2026, 9, 21, 10, 30)) is False
    assert _before_morning_open(datetime(2026, 9, 19, 8, 0)) is False  # 周六


# ============================================================
# W-B8：国债 tx 兜底窗口 + 新鲜度（P1-15）
# ============================================================

@test
def test_wb8_bond_tx_window_and_staleness():
    """W-B8（P1-15）：tx 兜底必须传最近窗口参数（默认窗口 2020-2021 死数据），
    且末行早于今日-7 天 → 整体弃用返回空。"""
    import pandas as pd
    from data import macro
    orig = macro.call_ak
    captured = {}

    def _stale_route(source, fn, *a, **kw):
        captured["source"] = source
        captured["args"] = kw or a
        return pd.DataFrame({"日期": ["2020-03-02", "2020-03-03"],
                             "10年": [2.6, 2.61]})

    macro.call_ak = _stale_route
    try:
        rows = macro._fetch_bond_yield_one("tx")
        assert rows == [], "两年前死数据应整体弃用"
        assert captured["source"] == "bond_tx"
        assert "start_date" in captured["args"] and "end_date" in captured["args"]
    finally:
        macro.call_ak = orig

    def _fresh_route(source, fn, *a, **kw):
        from datetime import date, timedelta
        today = date.today()
        return pd.DataFrame({
            "日期": [(today - timedelta(days=2)).isoformat(),
                     (today - timedelta(days=1)).isoformat()],
            "10年": [2.6, 2.61]})

    macro.call_ak = _fresh_route
    try:
        rows = macro._fetch_bond_yield_one("tx")
        assert len(rows) == 2 and rows[-1][2] == "tx"
    finally:
        macro.call_ak = orig


def main() -> int:
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
