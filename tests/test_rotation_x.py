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
  RuntimeError 阻断（不重试不换源）。
"""
import os
import sys
import types
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from signals import rotation as r1  # noqa: E402
from signals import rotation_x as rx  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
