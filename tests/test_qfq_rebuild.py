"""批次0 qfq 数据层单测（全量打包施工方案 §3.1 / 2026-09-20）。

覆盖（S10 预注册清单）：
- open_qfq 等比导出正确性（含除权日缩放因子）；
- low≤open≤high 校验逻辑（容差边界、原始违例识别、加法型混用违例回归）；
- Gate 0-A 匹配逻辑（事件日 / 其后首个交易日 / ±1 边界，合成数据）；
- fetcher 增量路径写 open_qfq（临时库；列存在才写、缺失零行为变化）；
- 无除权区间不变量校验函数（乘法型成立 / 加法型检出 / 事件跨区间剔除）；
- Gate 0-A 重拉事务语义（复检过→kept 提交；复检不过→rolled_back 库值还原）；
- Gate 0-C 抽样确定性（重灾票全入 + 固定种子补足）。

隔离：全部用临时目录 / 临时库 + AGSICKLE_* 环境变量，绝不触生产库表。
直接运行：.venv/bin/python3 tests/test_qfq_rebuild.py
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# 隔离先行：日志 handler / events 缓存 / 备份目录全部指向临时沙箱
# （必须在被测模块 import 前生效——run_all 惯例）
_TMP = tempfile.mkdtemp(prefix="qfq_rebuild_test_")
os.environ["AGSICKLE_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["AGSICKLE_BACKUP_DIR"] = os.path.join(_TMP, "backup")
os.makedirs(os.environ["AGSICKLE_LOG_DIR"], exist_ok=True)

import pandas as pd  # noqa: E402

from data import qfq_rebuild as qr  # noqa: E402

# daily_bar 审计所需最小 schema（与 fetcher.DDL 的列语义一致，测试专用精简版）
_TEST_DDL = """
CREATE TABLE daily_bar (
    code TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL, pct_chg REAL, turnover REAL,
    source TEXT, close_qfq REAL, high_qfq REAL, low_qfq REAL,
    PRIMARY KEY (code, trade_date)
);
CREATE TABLE fetch_log (code TEXT, run_at TEXT, status TEXT, rows INT, detail TEXT);
"""


def make_db(with_open_qfq: bool = False) -> tuple:
    """临时库 + 连接（返回 path, conn）。"""
    tmp = tempfile.mkdtemp(prefix="qfq_rebuild_db_")
    p = Path(tmp) / "t.db"
    conn = sqlite3.connect(p)
    conn.executescript(_TEST_DDL)
    if with_open_qfq:
        conn.execute("ALTER TABLE daily_bar ADD COLUMN open_qfq REAL")
    conn.commit()
    return p, conn


def bars_of(*rows) -> pd.DataFrame:
    """(code, date, open, high, low, close, close_qfq[, high_qfq, low_qfq]) →
    qfq_rebuild.load_bars 同形 DataFrame；high/low_qfq 缺省按等比导出（round4）。"""
    recs = []
    for r in rows:
        cd, d, o, h, l, c, cq = r[:7]
        hq = r[7] if len(r) > 7 else round(h * cq / c, 4)
        lq = r[8] if len(r) > 8 else round(l * cq / c, 4)
        recs.append({"code": cd, "trade_date": d, "open": o, "high": h, "low": l,
                     "close": c, "close_qfq": cq, "high_qfq": hq, "low_qfq": lq})
    return pd.DataFrame(recs)


class TestDeriveOpenQfq(unittest.TestCase):
    """S10-1：open_qfq 等比导出正确性（含除权日缩放因子）。"""

    def test_plain_scaling(self):
        self.assertEqual(qr.derive_open_qfq(10.0, 20.0, 15.0), 7.5)

    def test_exdiv_scaling_factor(self):
        # 除权日：raw close 因送转腰斩、qfq 连续——因子 = cq/close
        o, c, cq = 21.0, 20.2, 11.1
        self.assertEqual(qr.derive_open_qfq(o, c, cq), round(o * cq / c, 4))

    def test_invalid_close(self):
        self.assertIsNone(qr.derive_open_qfq(10.0, 0.0, 5.0))
        self.assertIsNone(qr.derive_open_qfq(10.0, None, 5.0))
        self.assertIsNone(qr.derive_open_qfq(None, 10.0, 5.0))

    def test_rounding_monotone_preserves_bounds(self):
        """同一行等比 + round4：raw low≤open≤high ⇒ 导出值保序（舍入单调）。"""
        k_cases = [0.5886, 0.9123, 0.3333, 1.7]
        for k in k_cases:
            for (o, h, l, c) in [(13.21, 13.52, 13.16, 13.37), (5.01, 5.01, 4.98, 4.99),
                                 (100.02, 101.0, 100.01, 100.5)]:
                cq = round(c * k, 4)
                self.assertLessEqual(qr.derive_open_qfq(o, c, cq),
                                     round(h * cq / c, 4) + 1e-12)
                self.assertGreaterEqual(qr.derive_open_qfq(o, c, cq),
                                        round(l * cq / c, 4) - 1e-12)


class TestGate0BPrecheck(unittest.TestCase):
    """S10-2：low≤open≤high 校验逻辑（容差边界、原始违例识别）。"""

    def test_consistent_multiplicative_zero_violation(self):
        bars = bars_of(("600000", "2024-01-02", 10.0, 10.5, 9.8, 10.2, 10.2),
                       ("600000", "2024-01-03", 10.3, 10.8, 10.1, 10.6, 10.6),
                       ("600000", "2024-01-04", 10.5, 10.9, 10.2, 10.3, 10.3))
        r = qr.gate0b_precheck(bars)
        self.assertEqual(r["new_violation_rows"], 0)
        self.assertEqual(r["raw_ohlc_broken_rows"], 0)

    def test_additive_hybrid_violation_detected(self):
        """加法型 low/high_qfq（tx 源）× 等比 open_qfq 混用 → 检出（批次0 实况回归）。"""
        # 600066 型：offset=-5.5，下行日 open==low>close → open_qfq < low_qfq
        o, h, l, c, cq = 13.5, 13.5, 13.5, 13.37, 7.87
        bars = bars_of(("600066", "2024-01-02", o, h, l, c, cq,
                        round(h - 5.5, 4), round(l - 5.5, 4)))
        r = qr.gate0b_precheck(bars)
        self.assertEqual(r["new_violation_rows"], 1)
        self.assertEqual(r["new_violation_stocks"], 1)

    def test_raw_broken_separated_from_new(self):
        """raw 层 open<low 的原始违例行单列，不算等比导出新违例。"""
        bars = bars_of(("600001", "2024-01-02", 9.5, 10.5, 9.8, 10.2, 10.2))
        r = qr.gate0b_precheck(bars)
        self.assertEqual(r["raw_ohlc_broken_rows"], 1)
        self.assertEqual(r["new_violation_rows"], 0)

    def test_tolerance_boundary(self):
        """相对容差 1e-6 × 价格：P=200 绝对带 2e-4 ≥ round4 步长 1e-4 → 一步差不报；
        P=50 绝对带 5e-5 < 1e-4 → 一步差即报。"""
        def row(price, low_qfq, open_val):
            c, cq = price, price  # open_qfq = round(open*cq/c,4) = round(open,4)
            return bars_of(("600002", "2024-01-02", open_val, price * 1.02,
                            price * 0.97, c, cq,
                            round(price * 1.02, 4), low_qfq))
        # P=200：open=199.9999 → open_qfq=199.9999，low_qfq=200.0，diff=1e-4 < 2e-4 带内
        self.assertEqual(
            qr.gate0b_precheck(row(200.0, 200.0, 199.9999))["new_violation_rows"], 0)
        # P=50：diff=1e-4 > 5e-5 带外 → 报
        self.assertEqual(
            qr.gate0b_precheck(row(50.0, 50.0, 49.9999))["new_violation_rows"], 1)


class TestGate0AMatch(unittest.TestCase):
    """S10-3：Gate 0-A 匹配逻辑（事件日 / 其后首个交易日 / ±1 边界）。"""

    CAL = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08",
           "2024-01-09", "2024-01-10"]

    def _dist(self, *dates):
        return pd.DataFrame({"code": ["000001"] * len(dates),
                             "trade_date": list(dates),
                             "ret_raw": [0.01] * len(dates),
                             "ret_qfq": [0.03] * len(dates)})

    def test_event_day_itself(self):
        m = qr.explained_mask(self._dist("2024-01-03"),
                              {"000001": ["2024-01-03"]}, self.CAL)
        self.assertTrue(bool(m.iloc[0]))

    def test_event_on_nontrading_day_snaps_next_td(self):
        # 事件落在周六 01-06 → 吸附到 01-08；畸变日在 01-08 可解释
        m = qr.explained_mask(self._dist("2024-01-08"),
                              {"000001": ["2024-01-06"]}, self.CAL)
        self.assertTrue(bool(m.iloc[0]))

    def test_boundary_plus_minus_one(self):
        ev = {"000001": ["2024-01-04"]}
        for d, want in (("2024-01-03", True), ("2024-01-05", True),   # ±1 边界
                        ("2024-01-02", False), ("2024-01-08", False)):  # ±2 越界
            m = qr.explained_mask(self._dist(d), ev, self.CAL)
            self.assertEqual(bool(m.iloc[0]), want, msg=f"{d} 应为 {want}")

    def test_other_stock_event_not_counted(self):
        m = qr.explained_mask(self._dist("2024-01-04"),
                              {"600000": ["2024-01-04"]}, self.CAL)
        self.assertFalse(bool(m.iloc[0]))


class TestDistortionDays(unittest.TestCase):

    def test_known_values_and_threshold(self):
        # ret_raw: +2%, ret_qfq: +2.02% → 差 2bp 不畸变；下一对差 1.5pp 畸变
        bars = bars_of(("000002", "2024-01-02", 10, 10, 10, 10.0, 10.0),
                       ("000002", "2024-01-03", 10, 10, 10, 10.2, 10.202),
                       ("000002", "2024-01-04", 10, 10, 10, 10.3, 10.4547))
        d = qr.distortion_days(bars)
        self.assertEqual(len(d), 1)
        self.assertEqual(d.iloc[0]["trade_date"], "2024-01-04")
        # 首行无前值：不参与（pct_change NaN）

    def test_first_row_excluded(self):
        bars = bars_of(("000003", "2024-01-02", 10, 10, 10, 10.0, 5.0))
        self.assertEqual(len(qr.distortion_days(bars)), 0)


class TestGate0D(unittest.TestCase):
    """S10-5：无除权区间不变量校验函数。"""

    CAL = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]

    def _bars_multiplicative(self, rounded: bool):
        rows, c = [], 20.0
        for i, d in enumerate(self.CAL):
            c = c * (1.0 + 0.01 * (i % 3 - 1))  # -1%/0/+1%
            cq = round(c * 0.9, 2) if rounded else c * 0.9  # k=0.9 恒定
            rows.append(("600009", d, c * 1.01, c * 1.02, c * 0.99, c, cq))
        return bars_of(*rows)

    def test_exact_multiplicative_clean(self):
        r = qr.gate0d_check(self._bars_multiplicative(rounded=False), {}, self.CAL)
        self.assertEqual(r["violations"], 0)
        self.assertEqual(r["no_event_pairs"], 4)

    def test_source_rounded_multiplicative_violates_1e9(self):
        """2 位小数舍入的源（em 形态）在 1e-9 容差下必违例——0-D 判据物理上
        只对无舍入的精确等比成立（批次0 报告的结构性发现）。"""
        r = qr.gate0d_check(self._bars_multiplicative(rounded=True), {}, self.CAL)
        self.assertGreater(r["violations"], 0)

    def test_additive_offset_detected_with_structure(self):
        rows, c = [], 20.0
        for i, d in enumerate(self.CAL):
            c = c * (1.0 + 0.02 * ((i % 3) - 1))
            rows.append(("600010", d, c * 1.01, c * 1.02, c * 0.99, c,
                         round(c - 5.5, 4)))  # 加法型：offset 5.5/20 ≈ 27.5%
        r = qr.gate0d_check(bars_of(*rows), {}, self.CAL)
        self.assertGreater(r["violations"], 0)
        self.assertGreater(
            r["structure_by_offset_bucket"][">=1e-2"]["violations"], 0)

    def test_event_spanning_suspension_gap_excluded(self):
        """停牌跳日对 (prev, cur] 区间含事件 → 该对剔除（不参与不变量）。"""
        rows = [("600011", "2024-01-02", 10, 10, 10, 10.0, 10.0),
                ("600011", "2024-01-08", 10, 10, 10, 11.0, 11.0)]  # 跳过 01-03~05
        r = qr.gate0d_check(bars_of(*rows), {"600011": ["2024-01-04"]}, self.CAL)
        self.assertEqual(r["no_event_pairs"], 0)  # 唯一一对被事件剔除


class TestRepullTransaction(unittest.TestCase):
    """S10 附加：Gate 0-A 重拉的事务语义（合成 em 源，离线）。"""

    def setUp(self):
        import data.fetcher as fetcher
        self.fetcher = fetcher
        self._tmp = tempfile.mkdtemp(prefix="qfq_repair_test_")
        qr.WORK_DIR = Path(self._tmp)  # events 缓存重定向
        self.db = Path(self._tmp) / "t.db"
        conn = sqlite3.connect(self.db)
        conn.executescript(_TEST_DDL)
        # 加法型脏数据：offset 5.5（≈27%），无事件的 2024-01-03 有 1.5pp 畸变
        conn.executemany(
            "INSERT INTO daily_bar (code, trade_date, open, high, low, close, "
            "volume, amount, pct_chg, turnover, source, close_qfq, high_qfq, low_qfq)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [("600066", "2024-01-02", 13.21, 13.52, 13.16, 13.37, 1, 1, 0, 0, "tx",
              7.87, 8.02, 7.66),
             ("600066", "2024-01-03", 13.49, 13.96, 13.36, 13.77, 1, 1, 3, 0, "tx",
              8.27, 8.46, 7.86),
             ("600066", "2024-01-04", 13.77, 13.83, 13.42, 13.48, 1, 1, -2, 0, "tx",
              7.98, 8.33, 7.92)])
        conn.commit()
        self.events = {"600066": []}
        self.calendar = ["2024-01-02", "2024-01-03", "2024-01-04"]

    def _fake_em(self, closes):
        """合成 em qfq 源：纯乘法 k=0.6 恒定（无畸变）。"""
        df = pd.DataFrame({
            "date": pd.to_datetime(self.calendar),
            "close_qfq": [round(c * 0.6, 4) for c in closes],
        })
        return lambda code, start, end: df

    def test_kept_when_clean(self):
        old = self.fetcher._hist_em_qfq
        self.fetcher._hist_em_qfq = self._fake_em([13.37, 13.77, 13.48])
        try:
            conn = sqlite3.connect(self.db)
            r = qr.repull_em_qfq("600066", conn, self.events, self.calendar)
            self.assertEqual(r["status"], "kept")
            self.assertEqual(r["rows"], 3)
            v = conn.execute("SELECT close_qfq, high_qfq, low_qfq FROM daily_bar "
                             "WHERE trade_date='2024-01-04'").fetchone()
            self.assertAlmostEqual(v[0], round(13.48 * 0.6, 4))
            self.assertAlmostEqual(v[1], round(13.83 * 0.6, 4))
            self.assertAlmostEqual(v[2], round(13.42 * 0.6, 4))
            conn.close()
        finally:
            self.fetcher._hist_em_qfq = old

    def test_rolled_back_when_still_unexplained(self):
        """复检仍无解释 → ROLLBACK：库值还原为重拉前（单事务语义）。"""
        before = sqlite3.connect(self.db).execute(
            "SELECT close_qfq FROM daily_bar WHERE trade_date='2024-01-03'"
        ).fetchone()[0]
        bad = pd.DataFrame({"date": pd.to_datetime(self.calendar),
                            "close_qfq": [8.0, 8.5, 7.0]})  # 01-04 暴跌假畸变
        old = self.fetcher._hist_em_qfq
        self.fetcher._hist_em_qfq = lambda code, start, end: bad
        try:
            conn = sqlite3.connect(self.db)
            r = qr.repull_em_qfq("600066", conn, self.events, self.calendar)
            self.assertEqual(r["status"], "rolled_back")
            self.assertTrue(r["unexplained"])
            after = conn.execute("SELECT close_qfq FROM daily_bar "
                                 "WHERE trade_date='2024-01-03'").fetchone()[0]
            self.assertEqual(after, before)  # 事务已回滚
            conn.close()
        finally:
            self.fetcher._hist_em_qfq = old

    def test_connection_error_propagates(self):
        """网络级失败（ConnectionError 家族）向上抛 → 调用方整批阻断。"""

        def boom(code, start, end):
            raise ConnectionError("ProxyError: push2his 不可达")

        old = self.fetcher._hist_em_qfq
        self.fetcher._hist_em_qfq = boom
        try:
            conn = sqlite3.connect(self.db)
            with self.assertRaises(ConnectionError):
                qr.repull_em_qfq("600066", conn, self.events, self.calendar)
            conn.close()
        finally:
            self.fetcher._hist_em_qfq = old


class TestFetcherIncrementalOpenQfq(unittest.TestCase):
    """S10-4：fetcher 增量路径写 open_qfq（临时库；列存在才写）。"""

    def setUp(self):
        import data.fetcher as fetcher
        self.fetcher = fetcher

    def _seed(self, conn):
        conn.executemany(
            "INSERT INTO daily_bar (code, trade_date, open, high, low, close, "
            "volume, amount, pct_chg, turnover, source, close_qfq, high_qfq, low_qfq)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [("600519", "2024-01-02", 10.0, 10.2, 9.9, 10.1, 1, 1, 1.0, 0, "em",
              None, None, None),
             ("600519", "2024-01-03", 10.3, 10.8, 10.1, 10.6, 1, 1, 4.95, 0, "em",
              None, None, None)])
        conn.commit()

    def _fake_em(self):
        df = pd.DataFrame({"date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
                           "close_qfq": [10.1, 10.35]})
        return lambda code, start, end: df

    def test_writes_open_qfq_when_column_exists(self):
        _p, conn = make_db(with_open_qfq=True)
        self._seed(conn)
        old = self.fetcher._hist_em_qfq
        self.fetcher._hist_em_qfq = self._fake_em()
        try:
            n = self.fetcher.backfill_qfq("600519", conn)
            self.assertGreaterEqual(n, 1)
            rows = conn.execute(
                "SELECT trade_date, open, close, close_qfq, high_qfq, low_qfq, "
                "open_qfq FROM daily_bar ORDER BY trade_date").fetchall()
            for td, o, c, cq, hq, lq, oq in rows:
                if cq is None:
                    self.assertIsNone(oq)
                    continue
                self.assertEqual(oq, round(o * cq / c, 4))
                self.assertLessEqual(lq, oq)
                self.assertLessEqual(oq, hq)
        finally:
            self.fetcher._hist_em_qfq = old
            conn.close()

    def test_noop_when_column_absent(self):
        """老库（未跑 Gate 0-B）：三列照写、无 open_qfq 列也不报错。"""
        _p, conn = make_db(with_open_qfq=False)
        self._seed(conn)
        old = self.fetcher._hist_em_qfq
        self.fetcher._hist_em_qfq = self._fake_em()
        try:
            n = self.fetcher.backfill_qfq("600519", conn)
            self.assertGreaterEqual(n, 1)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_bar)")}
            self.assertNotIn("open_qfq", cols)
            self.assertIsNotNone(conn.execute(
                "SELECT close_qfq FROM daily_bar WHERE trade_date='2024-01-03'"
            ).fetchone()[0])
        finally:
            self.fetcher._hist_em_qfq = old
            conn.close()


class _FakeConn:
    """gate0c_sample 只用 conn.execute(...).fetchall() 拉全码表——离线替身。"""

    def __init__(self, codes):
        self._codes = codes

    def execute(self, *a, **kw):
        return _Rows([(c,) for c in self._codes])


class _Rows(list):
    def fetchall(self):
        return list(self)


class TestGate0C(unittest.TestCase):

    def test_sample_heavy_and_deterministic(self):
        codes = [f"{i:06d}" for i in range(1, 101)]
        heavy = codes[:60]
        # 畸变日只出现在 heavy 60 票（其余 40 票零畸变）
        dist = pd.DataFrame([
            {"code": c, "trade_date": f"2024-02-{i:02d}", "ret_raw": 0.01,
             "ret_qfq": 0.05} for c in heavy for i in (1, 2, 3)])

        s1 = qr.gate0c_sample(_FakeConn(codes), dist, n=50)
        self.assertEqual(len(s1), 60)  # 畸变票 60 > 50 → 全入不截断
        self.assertEqual(set(s1), set(heavy))
        s2 = qr.gate0c_sample(_FakeConn(codes), dist, n=50)
        self.assertEqual(s1, s2)  # 固定种子可复现

    def test_sample_fills_to_n_with_rest(self):
        codes = [f"{i:06d}" for i in range(1, 31)]
        dist = pd.DataFrame([{"code": codes[0], "trade_date": "2024-01-02",
                              "ret_raw": 0.01, "ret_qfq": 0.05}])
        s = qr.gate0c_sample(_FakeConn(codes), dist, n=10)
        self.assertEqual(len(s), 10)
        self.assertIn(codes[0], s)


class TestGate0BApply(unittest.TestCase):

    def test_apply_on_temp_db(self):
        p, conn = make_db()
        conn.executemany(
            "INSERT INTO daily_bar (code, trade_date, open, high, low, close, "
            "volume, amount, pct_chg, turnover, source, close_qfq, high_qfq, low_qfq)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [("600000", "2024-01-02", 10.0, 10.5, 9.8, 10.2, 1, 1, 2.0, 0, "em",
              10.2, 10.5, 9.8),
             ("600000", "2024-01-03", 10.3, 10.8, 10.1, 10.6, 1, 1, 3.9, 0, "em",
              10.6, 10.8, 10.1)])
        conn.commit()
        conn.close()
        r = qr.gate0b_apply(db=p)
        self.assertTrue(r["added_column"])
        self.assertEqual(r["rows_backfilled"], 2)
        self.assertEqual(r["post"]["new_violation_rows"], 0)
        conn = sqlite3.connect(p)
        oq = conn.execute("SELECT open_qfq FROM daily_bar WHERE trade_date="
                          "'2024-01-03'").fetchone()[0]
        self.assertEqual(oq, round(10.3 * 10.6 / 10.6, 4))
        conn.close()

    def test_apply_skips_bad_close(self):
        p, conn = make_db()
        conn.executemany(
            "INSERT INTO daily_bar (code, trade_date, open, high, low, close, "
            "volume, amount, pct_chg, turnover, source, close_qfq, high_qfq, low_qfq)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [("600000", "2024-01-02", 10.0, 10.5, 9.8, 10.2, 1, 1, 2.0, 0, "em",
              10.2, 10.5, 9.8),
             ("600000", "2024-01-03", 10.3, 10.8, 10.1, 0.0, 1, 1, -100.0, 0, "em",
              0.0, 10.8, 10.1)])
        conn.commit()
        conn.close()
        r = qr.gate0b_apply(db=p)
        self.assertEqual(r["skipped_close_zero_or_null"], 1)
        conn = sqlite3.connect(p)
        self.assertIsNone(conn.execute("SELECT open_qfq FROM daily_bar WHERE "
                                       "trade_date='2024-01-03'").fetchone()[0])
        conn.close()


class TestP2Batch4b(unittest.TestCase):
    """批次 4b（P2 数据域清债）回归：⑦recalc 前行=上一日历行 / ⑧重刷失败
    弃窗 / ⑨重刷冷却窗 / ⑩ audit --fix 收敛混源残存（临时库合成验证，
    生产库执行留档待授权——见 commit 与批次报告）。"""

    def setUp(self):
        import data.fetcher as fetcher
        self.fetcher = fetcher

    def _seed(self, conn, code, td, close, cq, pct, src):
        conn.execute(
            "INSERT INTO daily_bar (code, trade_date, open, high, low, close, "
            "volume, amount, pct_chg, turnover, source, close_qfq)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, td, close, close * 1.01, close * 0.99, close,
             1e6, close * 1e8, pct, 0.0, src, cq))

    # ---------------- ⑦ recalc_tx_pct：前一行 = 上一日历行 ----------------

    def test_p2g7_recalc_uses_prev_calendar_row_across_mixed_gap(self):
        """混源缺口（tx 之间夹 em 行）+ 缺口内除权：
        - 错行（pct 存 0.0）→ 按上一日历行(em) 的 qfq 日环比重算（旧口径会把
          过滤集内跨缺口的多日收益 -3.43% 写进去）；
        - 正确行（pct 已= qfq 日环比）→ 不动（旧口径会误改，证明负向也收敛）。"""
        _p, conn = make_db()
        try:
            qfq_daily = round((9.56 / 9.68 - 1) * 100, 4)     # 日环比 -1.2397
            qfq_multiday = round((9.56 / 9.90 - 1) * 100, 4)  # 跨缺口多日 -3.4343
            self.assertNotAlmostEqual(qfq_daily, qfq_multiday, places=2)
            # 票 A（错行）：d1(tx) --gap-- d2(em) --除权-- d3(tx, pct=0)
            self._seed(conn, "000034", "2024-01-02", 10.0, 9.90, 0.0, "tx")
            self._seed(conn, "000034", "2024-01-10", 9.80, 9.68, -2.02, "em")
            self._seed(conn, "000034", "2024-01-11", 9.70, 9.56, 0.0, "tx")
            # 票 B（正确行）：同结构但 d3 的 pct 已是 qfq 日环比
            self._seed(conn, "600517", "2024-01-02", 10.0, 9.90, 0.0, "tx")
            self._seed(conn, "600517", "2024-01-10", 9.80, 9.68, -2.02, "em")
            self._seed(conn, "600517", "2024-01-11", 9.70, 9.56, qfq_daily, "tx")
            conn.commit()
            fixed, _skipped = self.fetcher.recalc_tx_pct(conn)
            self.assertEqual(fixed, 1, "只有票 A 的错行应被重算")
            got_a = conn.execute(
                "SELECT pct_chg FROM daily_bar WHERE code='000034'"
                " AND trade_date='2024-01-11'").fetchone()[0]
            self.assertAlmostEqual(got_a, qfq_daily, places=6,
                                   msg="应写日环比而非跨缺口多日收益")
            got_b = conn.execute(
                "SELECT pct_chg FROM daily_bar WHERE code='600517'"
                " AND trade_date='2024-01-11'").fetchone()[0]
            self.assertAlmostEqual(got_b, qfq_daily, places=6,
                                   msg="已正确的行不得被改写")
            # 幂等
            fixed2, _ = self.fetcher.recalc_tx_pct(conn)
            self.assertEqual(fixed2, 0)
        finally:
            conn.close()

    def test_p2g7_recalc_code_scoped_uses_calendar_prev(self):
        """code= 模式（backfill_qfq 增量路径）同样取上一日历行。"""
        _p, conn = make_db()
        try:
            self._seed(conn, "300394", "2024-01-02", 10.0, 9.90, 0.0, "tx")
            self._seed(conn, "300394", "2024-01-10", 9.80, 9.68, 0.0, "em")
            self._seed(conn, "300394", "2024-01-11", 9.70, 9.56, 0.0, "tx")
            # 另一票不受影响
            self._seed(conn, "688778", "2024-01-11", 9.70, 9.56, 0.0, "tx")
            conn.commit()
            fixed, _ = self.fetcher.recalc_tx_pct(conn, code="300394")
            self.assertEqual(fixed, 1)
            other = conn.execute(
                "SELECT pct_chg FROM daily_bar WHERE code='688778'").fetchone()[0]
            self.assertEqual(other, 0.0)
        finally:
            conn.close()

    # -------- ⑧⑨ backfill_qfq 不变量分支：弃窗 / 冷却窗 --------

    def _seed_invariant_fixture(self, conn, code="600519"):
        """edge 行（旧锚 cq=10.0）+ 无 qfq 行；假 em_qfq 给新锚 9.0 →
        边界对 qfq 环比 -10% 深于 raw 0% >0.3pp → 不变量必命中。"""
        self._seed(conn, code, "2024-01-02", 10.0, 10.0, 0.0, "em")
        self._seed(conn, code, "2024-01-03", 10.0, None, 0.0, "em")
        conn.commit()
        df = pd.DataFrame({"date": pd.to_datetime(["2024-01-03"]),
                           "close_qfq": [9.0]})
        return lambda c, s, e: df

    def test_p2g8_invariant_hit_rebrush_fail_discards_window(self):
        """⑧：不变量命中且重刷失败 → 窗写入整体回滚（close_qfq 保持 NULL），
        返回 0——与漂移路径"放弃写入"对称（此前坏窗照样 commit）。"""
        _p, conn = make_db()
        old_qfq, old_rebrush = (self.fetcher._hist_em_qfq,
                                self.fetcher.rebrush_qfq_full)
        self.fetcher._hist_em_qfq = self._seed_invariant_fixture(conn)
        self.fetcher.rebrush_qfq_full = lambda code, conn: (0, "")
        try:
            n = self.fetcher.backfill_qfq("600519", conn)
            self.assertEqual(n, 0)
            cq = conn.execute("SELECT close_qfq FROM daily_bar WHERE "
                              "trade_date='2024-01-03'").fetchone()[0]
            self.assertIsNone(cq, "重刷失败时增量窗写入必须被回滚")
            edge = conn.execute("SELECT close_qfq FROM daily_bar WHERE "
                                "trade_date='2024-01-02'").fetchone()[0]
            self.assertEqual(edge, 10.0, "已提交的旧行不受影响")
            # 连接仍可继续正常写入（SAVEPOINT 已清理）
            conn.execute("INSERT INTO fetch_log VALUES ('x','t','ok',0,'')")
            conn.commit()
        finally:
            self.fetcher._hist_em_qfq = old_qfq
            self.fetcher.rebrush_qfq_full = old_rebrush
            conn.close()

    def test_p2g8_invariant_hit_rebrush_ok_commits(self):
        """⑧ 对照：重刷成功 → 返回重刷行数且窗写入一并提交。"""
        _p, conn = make_db()
        old_qfq, old_rebrush = (self.fetcher._hist_em_qfq,
                                self.fetcher.rebrush_qfq_full)
        self.fetcher._hist_em_qfq = self._seed_invariant_fixture(conn)
        self.fetcher.rebrush_qfq_full = lambda code, conn: (7, "tx_qfq")
        try:
            n = self.fetcher.backfill_qfq("600519", conn)
            self.assertEqual(n, 7)
            cq = conn.execute("SELECT close_qfq FROM daily_bar WHERE "
                              "trade_date='2024-01-03'").fetchone()[0]
            self.assertEqual(cq, 9.0)
        finally:
            self.fetcher._hist_em_qfq = old_qfq
            self.fetcher.rebrush_qfq_full = old_rebrush
            conn.close()

    def test_p2g9_cooldown_skips_rebrush_keeps_window(self):
        """⑨：冷却窗内已有 qfq_full_rebrush 留痕 → 不变量命中不再重刷
        （加法型不可收敛，防每 30 分钟全史重刷），增量窗保留。"""
        from datetime import datetime
        _p, conn = make_db()
        old_qfq, old_rebrush = (self.fetcher._hist_em_qfq,
                                self.fetcher.rebrush_qfq_full)

        def _must_not_rebrush(code, conn):
            raise AssertionError("冷却窗内不应触发全史重刷")

        self.fetcher._hist_em_qfq = self._seed_invariant_fixture(conn)
        self.fetcher.rebrush_qfq_full = _must_not_rebrush
        conn.execute(
            "INSERT INTO fetch_log VALUES (?,?, 'qfq_full_rebrush', 655, "
            "'source=tx_qfq')",
            ("600519", datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        try:
            n = self.fetcher.backfill_qfq("600519", conn)
            self.assertGreaterEqual(n, 1)
            cq = conn.execute("SELECT close_qfq FROM daily_bar WHERE "
                              "trade_date='2024-01-03'").fetchone()[0]
            self.assertEqual(cq, 9.0, "冷却窗分支应保留增量窗")
        finally:
            self.fetcher._hist_em_qfq = old_qfq
            self.fetcher.rebrush_qfq_full = old_rebrush
            conn.close()

    def test_p2g9_cooldown_expires_then_rebrush_attempted(self):
        """⑨ 负向：留痕早于冷却窗（REBRUSH_COOLDOWN_DAYS+1 天前）→ 重刷照常
        触发（重刷失败路径回归：返回 0 且窗被弃）。"""
        from datetime import datetime, timedelta
        _p, conn = make_db()
        old_qfq, old_rebrush = (self.fetcher._hist_em_qfq,
                                self.fetcher.rebrush_qfq_full)
        self.fetcher._hist_em_qfq = self._seed_invariant_fixture(conn)
        self.fetcher.rebrush_qfq_full = lambda code, conn: (0, "")
        stale = (datetime.now() - timedelta(
            days=self.fetcher.REBRUSH_COOLDOWN_DAYS + 1)
        ).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO fetch_log VALUES (?,?, 'qfq_full_rebrush', 655, "
            "'source=tx_qfq')", ("600519", stale))
        conn.commit()
        try:
            n = self.fetcher.backfill_qfq("600519", conn)
            self.assertEqual(n, 0)
            cq = conn.execute("SELECT close_qfq FROM daily_bar WHERE "
                              "trade_date='2024-01-03'").fetchone()[0]
            self.assertIsNone(cq)
        finally:
            self.fetcher._hist_em_qfq = old_qfq
            self.fetcher.rebrush_qfq_full = old_rebrush
            conn.close()

    # -------- ⑩ audit --fix 收敛混源残存（临时库合成验证） --------

    def test_p2g10_audit_fix_converges_residual_mixed_rows(self):
        """⑩：合成"生产 141 行/9 票"同型数据（非 watchlist 混源票、跨缺口
        除权、pct 与日环比背离>1pp）→ check_db 报 tx_pct_divergent；
        audit.fix_tx_pct（= --fix 的 W-B3 步）收敛后复检为 0。生产库执行
        留档待授权，本用例只证代码路径可收敛。"""
        from data.audit import check_db, fix_tx_pct
        _p, conn = make_db()
        try:
            for i, code in enumerate(("300394", "688778", "000034")):
                self._seed(conn, code, "2024-01-02", 10.0, 9.90, 0.0, "tx")
                self._seed(conn, code, "2024-01-10", 9.80, 9.68, -2.02, "em")
                self._seed(conn, code, f"2024-01-1{i + 1}", 9.70, 9.56,
                           0.0, "tx")
            conn.commit()
            issues, _total = check_db(conn)
            flagged = [i for i in issues if i["kind"] == "tx_pct_divergent"]
            self.assertEqual(len(flagged), 3, flagged)
            fixed, skipped = fix_tx_pct(conn)
            self.assertEqual(fixed, 3)
            self.assertEqual(skipped, 3)  # 每票首行无上一日历行 → 计入 skipped
            issues2, _ = check_db(conn)
            self.assertEqual(
                [i for i in issues2 if i["kind"] == "tx_pct_divergent"], [])
            expect = round((9.56 / 9.68 - 1) * 100, 4)
            for code in ("300394", "688778", "000034"):
                pct = conn.execute(
                    "SELECT pct_chg FROM daily_bar WHERE code=? AND "
                    "trade_date LIKE '2024-01-1_' AND source='tx' AND "
                    "close=9.70", (code,)).fetchone()[0]
                self.assertAlmostEqual(pct, expect, places=6)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
