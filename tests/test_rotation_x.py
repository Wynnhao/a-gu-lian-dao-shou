"""X 批次解冻（批次 3）—— rotation_x 单测（纯合成数据，绝不连 DB、绝不联网）。

施工方案：docs/全量打包施工方案-2026-09-20.md §3.4。纪律：本文件零 DB 访问（连只读
也不允许）、零网络（akshare 延迟 import 不触发；fetch_sw_members 用注入的假 akshare
模块测映射装配与阻断语义）。全部用手工构造的合成 DataFrame 驱动
signals/rotation_x.py 的纯函数。

用例清单：
- X1 desired：hold_days=1 只覆盖 t0+1 一日、hold_days=2/3 变体窗口、其余日 = 动量持有组；
- X1 同日多信号后触发者覆盖（最新优先）；信号落在最后交易日不越界；
- X1 成本框架：每次换腿 0.4%（0.1% 换腿 + 0.3% 尾盘乐观偏差），基线与信号腿同框架；
  信号目标组与动量持有组同名 → 无换腿无成本（不虚构无交易成本）；
- leg_returns_cost 与 R1 语义同源：CASH=0 / EQUAL 缺失按 0 / 组收益缺失按 0；
- evaluate_gate 三件套判据（① +5% ② 2/3 段 ③ MDD 2pp）逐条与 rotation.metrics 手算一致；
- X2 组收益引擎：等权 + MIN_MEMBERS=6 门槛（不足 6 只有数据当日 = NaN，整组全 NaN 剔除）；
- fetch_sw_members：假 akshare 装配映射（zfill/行业聚合/覆盖统计）+ 接口失败 →
  RuntimeError 阻断（不重试不换源）；
- D-3 三态出口（2026-09-21）：Gate0 分辨率前置 + INSUFFICIENT-DATA/退出码 3——
  rotation.main() 用 tmp sqlite（合成 daily_bar）+ tmp config 全合成端到端驱动，
  正例（数据足 → 原三件套判据路径不变，exit 0/1）与反例（数据不足 → exit 3）各一；
  rotation_x.aggregate_exit / macro_ratio.gate0_resolution 纯函数矩阵（零 DB 零网络）。
"""
import io
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from signals import rotation as r1  # noqa: E402
from signals import rotation_x as rx  # noqa: E402
from signals import macro_ratio_research as mr  # noqa: E402  Gate0 纯函数（D-3）

os.environ.setdefault("AGSICKLE_DISABLE_LIVE_QUOTES", "1")

# 跨两个月（2025-01 后 10 个交易日 + 2025-02 前 2 个），供月末换持用例。
DATES = [d.strftime("%Y-%m-%d")
         for d in pd.bdate_range("2025-01-20", periods=12)]
PICKS_G1 = {"2025-01": "G1", "2025-02": "G1"}


def make_cret() -> pd.DataFrame:
    """两组合成组收益：G1 交替 ±1%，G2 恒 +0.5%。"""
    return pd.DataFrame({
        "G1": [0.01, -0.01] * 6,
        "G2": [0.005] * 12,
    }, index=DATES)


class TestBuildDesiredX1(unittest.TestCase):
    def test_hold1_covers_only_next_day(self):
        cret = make_cret()
        sigs = [(DATES[3], "G1", "G2")]
        desired = rx.build_desired_x1(cret, PICKS_G1, sigs, DATES[0], hold_days=1)
        self.assertEqual(set(desired), set(DATES))
        self.assertEqual(desired[DATES[4]], "G2")          # t0+1 唯一信号持有日
        for i, d in enumerate(DATES):
            if i != 4:
                self.assertEqual(desired[d], "G1")         # 其余日 = 动量持有组

    def test_hold2_hold3_variants(self):
        cret = make_cret()
        sigs = [(DATES[3], "G1", "G2")]
        d2 = rx.build_desired_x1(cret, PICKS_G1, sigs, DATES[0], hold_days=2)
        self.assertEqual([d2[DATES[4]], d2[DATES[5]], d2[DATES[6]]],
                         ["G2", "G2", "G1"])
        d3 = rx.build_desired_x1(cret, PICKS_G1, sigs, DATES[0], hold_days=3)
        self.assertEqual([d3[DATES[4]], d3[DATES[5]], d3[DATES[6]], d3[DATES[7]]],
                         ["G2", "G2", "G2", "G1"])

    def test_same_day_latest_signal_wins(self):
        cret = make_cret()
        cret["G3"] = 0.002
        sigs = [(DATES[3], "G1", "G2"), (DATES[3], "G1", "G3")]
        desired = rx.build_desired_x1(cret, PICKS_G1, sigs, DATES[0])
        self.assertEqual(desired[DATES[4]], "G3")          # 后触发覆盖先触发

    def test_signal_on_last_day_no_overflow(self):
        cret = make_cret()
        sigs = [(DATES[-1], "G1", "G2")]
        desired = rx.build_desired_x1(cret, PICKS_G1, sigs, DATES[0])
        self.assertTrue(all(v == "G1" for v in desired.values()))

    def test_test_start_excludes_earlier_days(self):
        cret = make_cret()
        sigs = [(DATES[3], "G1", "G2")]
        desired = rx.build_desired_x1(cret, PICKS_G1, sigs, DATES[5])
        self.assertNotIn(DATES[4], desired)                # warmup 外的信号持有日不入测试窗


class TestLegReturnsCost(unittest.TestCase):
    def test_switch_cost_04_round_trip(self):
        cret = make_cret()
        sigs = [(DATES[3], "G1", "G2")]
        equal_ret = cret.mean(axis=1)
        desired = rx.build_desired_x1(cret, PICKS_G1, sigs, DATES[0], hold_days=1)
        r, sw = rx.leg_returns_cost(cret, desired, equal_ret, rx.COST_SWITCH_X1)
        self.assertEqual(sw, 2)                            # 进/出各一次换腿
        self.assertAlmostEqual(r[DATES[4]], 0.005 - 0.004, places=12)
        self.assertAlmostEqual(r[DATES[5]], -0.01 - 0.004, places=12)
        self.assertAlmostEqual(r[DATES[6]], 0.01, places=12)   # 回落日无再计费
        self.assertAlmostEqual(r[DATES[0]], 0.01, places=12)

    def test_baseline_same_cost_framework(self):
        cret = make_cret()
        picks = {"2025-01": "G1", "2025-02": "G2"}         # 月末换持
        equal_ret = cret.mean(axis=1)
        desired_mom = {d: picks.get(d[:7]) for d in cret.index}
        r, sw = rx.leg_returns_cost(cret, desired_mom, equal_ret, rx.COST_SWITCH_X1)
        self.assertEqual(sw, 1)                            # 基线月末换腿同计 0.4%
        i_feb = [i for i, d in enumerate(DATES) if d[:7] == "2025-02"][0]
        self.assertAlmostEqual(r[DATES[i_feb]], 0.005 - 0.004, places=12)

    def test_same_target_as_mom_no_fake_cost(self):
        cret = make_cret()
        picks = {"2025-01": "G2", "2025-02": "G2"}         # 动量已持有 G2 = 信号目标
        sigs = [(DATES[3], "G1", "G2")]
        equal_ret = cret.mean(axis=1)
        desired = rx.build_desired_x1(cret, picks, sigs, DATES[0])
        _r, sw = rx.leg_returns_cost(cret, desired, equal_ret, rx.COST_SWITCH_X1)
        self.assertEqual(sw, 0)                            # 同名 → 无实际交易无成本

    def test_cash_equal_and_missing_group_semantics(self):
        cret = make_cret()
        cret.iloc[2, cret.columns.get_loc("G2")] = np.nan  # 组收益缺失按 0 计
        equal_ret = cret.mean(axis=1)
        r_cash, _ = rx.leg_returns_cost(cret, {d: None for d in DATES},
                                        equal_ret, 0.004)
        self.assertEqual(float(r_cash.abs().sum()), 0.0)
        r_eq, _ = rx.leg_returns_cost(cret, {d: "EQUAL" for d in DATES},
                                      equal_ret, 0.004)
        self.assertAlmostEqual(r_eq[DATES[2]], 0.01, places=12)  # mean 跳过 NaN
        r_g2, _ = rx.leg_returns_cost(cret, {d: "G2" for d in DATES},
                                      equal_ret, 0.004)
        self.assertAlmostEqual(r_g2[DATES[2]], 0.0, places=12)


class TestEvaluateGate(unittest.TestCase):
    def test_three_conditions_match_r1_metrics(self):
        idx = pd.bdate_range("2025-01-02", periods=504).strftime("%Y-%m-%d")
        rng = np.random.default_rng(7)
        strat = pd.Series(rng.normal(0.001, 0.01, 504), index=idx)
        mom = pd.Series(rng.normal(0.0002, 0.01, 504), index=idx)
        g = rx.evaluate_gate(strat, mom)
        _ts, ann_s, mdd_s = r1.metrics(strat)
        _tm, ann_m, mdd_m = r1.metrics(mom)
        self.assertAlmostEqual(g["exc_ann"], ann_s - ann_m, places=12)
        self.assertAlmostEqual(g["mdd_gap"], mdd_s - mdd_m, places=12)
        self.assertEqual(g["c1"], (ann_s - ann_m) >= 0.05)
        self.assertEqual(g["c2"], g["n_pos"] >= 2)
        self.assertEqual(g["c3"], (mdd_s - mdd_m) <= 0.02)
        self.assertEqual(g["ok"], g["c1"] and g["c2"] and g["c3"])

    def test_identical_legs_fail_first_two(self):
        idx = pd.bdate_range("2025-01-02", periods=400).strftime("%Y-%m-%d")
        r = pd.Series(0.001, index=idx)
        g = rx.evaluate_gate(r, r)
        self.assertAlmostEqual(g["exc_ann"], 0.0, places=12)
        self.assertFalse(g["c1"])
        self.assertEqual(g["n_pos"], 0)
        self.assertFalse(g["c2"])
        self.assertTrue(g["c3"])                           # 平凡 PASS（两腿重合）
        self.assertFalse(g["ok"])


class TestGroupReturnsFromPx(unittest.TestCase):
    @staticmethod
    def _px_growth(codes):
        """每列每日精确 +2% 的价格网格。"""
        return pd.DataFrame(
            {c: 100.0 * (1.02 ** np.arange(len(DATES))) for c in codes},
            index=DATES)

    def test_equal_weight_and_min_members(self):
        big = [f"B{i}" for i in range(7)]                  # 7 只：>= MIN_MEMBERS
        small = [f"S{i}" for i in range(5)]                # 5 只：恒低于门槛
        px = self._px_growth(big + small)
        out = rx.group_returns_from_px(px, {"大组": big, "小组": small})
        self.assertIn("大组", out.columns)
        self.assertNotIn(DATES[0], out.index)              # 首日 pct_change=NaN，全 NaN 行被剔除
        self.assertAlmostEqual(out["大组"].iloc[0], 0.02, places=12)
        self.assertAlmostEqual(out["大组"].iloc[-1], 0.02, places=12)
        # 不足 6 只的组：列保留（与 R1 同为行向 dropna）但恒为 NaN → 永不计入
        self.assertIn("小组", out.columns)
        self.assertTrue(out["小组"].isna().all())

    def test_partial_day_below_threshold_is_nan(self):
        codes = [f"C{i}" for i in range(7)]
        px = self._px_growth(codes)
        px.iloc[3, :6] = np.nan                            # 当日只剩 1 只有数据
        out = rx.group_returns_from_px(px, {"G": codes})
        # 行 3（成员大量缺失）与行 4（pct_change 前值 NaN 传播）计数 <6 → 全 NaN 行被剔除
        self.assertNotIn(DATES[3], out.index)
        self.assertNotIn(DATES[4], out.index)
        self.assertAlmostEqual(out["G"].loc[DATES[5]], 0.02, places=12)


class TestFetchSwMembers(unittest.TestCase):
    """注入假 akshare 模块（零网络）验证映射装配与阻断语义。"""

    def _install_fake(self, industries, cons_by_symbol):
        mod = types.ModuleType("akshare")
        mod.__version__ = "0.0-fake"
        mod.sw_index_first_info = lambda: pd.DataFrame(industries)

        def _cons(symbol):
            if symbol == "999999":
                raise IOError("upstream down")
            return pd.DataFrame({"证券代码": cons_by_symbol[symbol]})

        mod.index_component_sw = _cons
        self._saved = sys.modules.get("akshare")
        sys.modules["akshare"] = mod
        self.addCleanup(self._restore)

    def _restore(self):
        if getattr(self, "_saved", None) is not None:
            sys.modules["akshare"] = self._saved
        else:
            sys.modules.pop("akshare", None)

    def test_assembly_zfill_and_stats(self):
        self._install_fake(
            {"行业代码": ["801010.SI", "801050.SI"],
             "行业名称": ["农林牧渔", "电子"]},
            {"801010": ["600519", "000998"],
             "801050": ["000977", "300750", "600519"]})    # 600519 归属后到者
        members, stats = rx.fetch_sw_members(
            codes=["600519", "977", "000998", "000001"], sleep_s=0.0)
        self.assertEqual(members, {"电子": ["600519", "000977"],
                                   "农林牧渔": ["000998"]})
        self.assertEqual(stats["covered"], 3)
        self.assertEqual(stats["missing"], ["000001"])     # 不在成分内 → 缺映射
        self.assertEqual(stats["industries"], 2)

    def test_fetch_failure_blocks(self):
        self._install_fake(
            {"行业代码": ["999999.SI"], "行业名称": ["X"]},
            {"999999": []})
        with self.assertRaises(RuntimeError) as cm:
            rx.fetch_sw_members(codes=["600519"], sleep_s=0.0)
        self.assertIn("在线取数失败", str(cm.exception))


# ---------------- D-3 三态出口（Gate0 分辨率前置 + INSUFFICIENT-DATA/exit 3） ----------------

def _build_rotation_fixture(tmp_dir: Path, n_days: int, switch_day: int) -> tuple:
    """合成 market.db + config.json（写入 tmp，绝不碰生产库/config）。

    两组各 6 成员（满足 MIN_MEMBERS=6），组内成员同价 → 组日收益 = 单序列：
    GA 恒 +0.5%/−1.0% 交替（< −0.8% 的日子触发信号），GB 前段与 GA 正相关、
    第 switch_day 根 bar 起反号（corr=±1），使白名单只可能出现在后段。
    返回 (db_path, cfg_path)。"""
    dates = pd.bdate_range("2024-01-01", periods=n_days).strftime("%Y-%m-%d")
    r_a = np.array([0.005 if i % 2 == 0 else -0.010 for i in range(n_days)])
    signs = np.array([+1.0 if i < switch_day else -1.0 for i in range(n_days)])
    pa = 100.0 * np.cumprod(1.0 + r_a)
    pb = 100.0 * np.cumprod(1.0 + r_a * signs)
    db = tmp_dir / "market.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE daily_bar (code TEXT, trade_date TEXT, close_qfq REAL)")
        rows = []
        for j, d in enumerate(dates):
            for k in range(6):
                rows.append((f"CA{k}", d, float(pa[j])))
                rows.append((f"CB{k}", d, float(pb[j])))
        conn.executemany("INSERT INTO daily_bar VALUES (?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()
    cfg = tmp_dir / "config.json"
    wl = ([{"code": f"CA{k}", "name": f"CA{k}", "concepts": ["GA"]} for k in range(6)]
          + [{"code": f"CB{k}", "name": f"CB{k}", "concepts": ["GB"]} for k in range(6)])
    cfg.write_text(json.dumps({"watchlist": wl}), encoding="utf-8")
    return db, cfg


class TestGate0ThreeState(unittest.TestCase):
    """D-3（P0-C）：Gate0 分辨率前置——数据不足 → INSUFFICIENT-DATA + 退出码 3，
    不进 PASS/FAIL 二元；数据足 → 原三件套判据路径不变。判据/阈值数字零改动。"""

    def _patch_rotation_paths(self, n_days: int, switch_day: int):
        tmp = Path(tempfile.mkdtemp(prefix="agsickle_gate0_test_"))
        db, cfg = _build_rotation_fixture(tmp, n_days, switch_day)
        saved = (r1.DB_PATH, r1.CONFIG_PATH)
        r1.DB_PATH, r1.CONFIG_PATH = db, cfg
        self.addCleanup(lambda: (setattr(r1, "DB_PATH", saved[0]),
                                 setattr(r1, "CONFIG_PATH", saved[1])))

    def test_gate0_helper_and_constants(self):
        """纯函数正反例 + 红线自查（三态常量与既有预注册判据数字零改动）。"""
        ok = r1.gate0_resolution(10, 43)               # 数据足（10 非空月/43 信号）
        self.assertTrue(ok["ok"])
        bad = r1.gate0_resolution(1, 2)                # 镜像归档 R1：1/20 月非空、2 信号
        self.assertFalse(bad["ok"])
        zero_sig = r1.gate0_resolution(20, 0)          # 非空月足但零信号 → 仍无分辨率
        self.assertFalse(zero_sig["ok"])
        self.assertEqual(r1.INSUFFICIENT_DATA, "INSUFFICIENT-DATA")
        self.assertEqual(r1.EXIT_INSUFFICIENT, 3)
        # 判据数字红线：Gate0 增补不得动原预注册参数
        self.assertEqual(r1.TRIGGER_THR, -0.008)
        self.assertEqual(r1.RHO_250_THR, -0.15)
        self.assertEqual(r1.RHO_60_THR, -0.20)
        self.assertEqual(r1.COST_PER_SWITCH, 0.001)

    def test_rotation_main_insufficient_exits_3(self):
        """反例（合成触发 exit 3）：白名单 1 个月非空 + 2 信号（归档 R1 同款分辨率形态）
        → INSUFFICIENT-DATA / 退出码 3，且不进三件套裁决（无 Gate 行）。"""
        self._patch_rotation_paths(n_days=400, switch_day=197)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = r1.main()
        out = buf.getvalue()
        self.assertEqual(rc, 3)
        self.assertIn("INSUFFICIENT-DATA", out)
        self.assertIn("Gate0 分辨率前置", out)
        self.assertNotIn("Gate（对照 MOM 基线③", out)   # 未进原判据路径
        # fixture 形态自检：确为 1 非空月 / 2 信号（镜像归档 R1）
        cret = r1.concept_returns()
        wl = r1.monthly_whitelists(cret)
        self.assertEqual(len([v for v in wl.values() if v]), 1)
        self.assertEqual(len(r1.generate_signals(cret, wl)), 2)

    def test_rotation_main_sufficient_keeps_original_gate_path(self):
        """正例：10 非空月 / 43 信号 → Gate0 通过 → 进原三件套判据（两腿近同 → ①0%
        <+5% FAIL）→ 退出码 1（非 3），判据路径与归档口径一致。"""
        self._patch_rotation_paths(n_days=560, switch_day=160)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = r1.main()
        out = buf.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("Gate（对照 MOM 基线③", out)      # 原判据路径照常裁决
        self.assertIn("VERDICT: FAIL", out)
        self.assertNotIn("INSUFFICIENT-DATA", out)

    def test_rotation_x_run_x1_insufficient_state(self):
        """run_x1 在分辨率不足的同一合成数据上返回 INSUFFICIENT-DATA 态（不再 FAIL）。"""
        self._patch_rotation_paths(n_days=400, switch_day=197)
        buf = io.StringIO()
        with redirect_stdout(buf):
            state = rx.run_x1()
        self.assertEqual(state, r1.INSUFFICIENT_DATA)
        self.assertIn("X1 VERDICT: INSUFFICIENT-DATA", buf.getvalue())
        self.assertIn("Gate0 分辨率前置", buf.getvalue())

    def test_aggregate_exit_matrix(self):
        """两轴三态聚合：FAIL 强于挂起（exit 1），挂起 exit 3，全 PASS exit 0。"""
        ins = r1.INSUFFICIENT_DATA
        self.assertEqual(rx.aggregate_exit(["PASS", "PASS"]), 0)
        self.assertEqual(rx.aggregate_exit(["PASS", "FAIL"]), 1)
        self.assertEqual(rx.aggregate_exit(["FAIL", ins]), 1)
        self.assertEqual(rx.aggregate_exit(["PASS", ins]), 3)
        self.assertEqual(rx.aggregate_exit([ins, ins]), 3)

    def test_rotation_x_main_exit3_via_state_injection(self):
        """端到端 exit 3：run_x1/run_x2 注入挂起态 → main() 返回 3（零 DB 零网络）。"""
        saved = (rx.run_x1, rx.run_x2)
        rx.run_x1 = lambda: r1.INSUFFICIENT_DATA
        rx.run_x2 = lambda: r1.INSUFFICIENT_DATA

        def _restore():
            rx.run_x1, rx.run_x2 = saved
        self.addCleanup(_restore)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = rx.main()
        self.assertEqual(rc, 3)
        self.assertIn(f"X1={r1.INSUFFICIENT_DATA}", buf.getvalue())
        # 对照：混入 FAIL 轴仍 exit 1（FAIL 强于挂起）
        rx.run_x1, rx.run_x2 = saved
        rx.run_x1 = lambda: "FAIL"
        rx.run_x2 = lambda: r1.INSUFFICIENT_DATA
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            self.assertEqual(rx.main(), 1)

    def test_macro_gate0_resolution(self):
        """M1 Gate0（events 版）：任一比值两 horizon 事件数均达下限才可裁决。"""
        self.assertTrue(mr.gate0_resolution(
            {"铜油比": {"n20": 300, "n60": 280}})["ok"])
        self.assertFalse(mr.gate0_resolution(
            {"铜油比": {"n20": 100, "n60": 280}})["ok"])   # n20 不足 → 挂起
        # 油金比不足但铜油比足 → 至少一个比值可裁决 → 整体 ok
        self.assertTrue(mr.gate0_resolution(
            {"铜油比": {"n20": 300, "n60": 300},
             "油金比": {"n20": 10, "n60": 10}})["ok"])
        self.assertEqual(mr.INSUFFICIENT_DATA, "INSUFFICIENT-DATA")
        self.assertEqual(mr.EXIT_INSUFFICIENT, 3)
        self.assertEqual(mr.GATE0_MIN_EVENTS, 250)          # 保守值常量在位（待预注册复核）


if __name__ == "__main__":
    unittest.main()
