"""批次 1（全量打包批）—— 对冲配对尾盘口径单测（纯合成数据，绝不连 DB）。

施工方案：docs/全量打包施工方案-2026-09-20.md §3.2（预注册口径）。
纪律：零 DB 访问（连只读也不允许）；全部用手工构造的收益矩阵 / 价格面板驱动
signals/hedge_pair_research.py 的纯函数层（白名单三条件、触发双向与 ISO 周去重、
受控 momentum 选票、触发日目标缩放、收盘执行组合引擎的成本/扣减/停牌顺延、
Gate 谓词），逐项核对 docstring 消歧 1~11 的实现语义。
"""
import os
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

os.environ["AGSICKLE_DISABLE_LIVE_QUOTES"] = "1"  # 测试保持离线

from signals import hedge_pair_research as hp  # noqa: E402


def make_panel(data: dict) -> pd.DataFrame:
    """{code: {date: close(或 None)}} → date×code 收盘面板（缺 bar = NaN）。"""
    dates = sorted({d for series in data.values() for d in series})
    px = pd.DataFrame(index=dates, columns=sorted(data), dtype=float)
    for c, series in data.items():
        for d, v in series.items():
            if v is not None:
                px.at[d, c] = float(v)
    return px


def neg_corr_returns(n: int, seed: int = 7) -> pd.DataFrame:
    """两列强负相关收益（B = −A + 微噪声）。"""
    rng = np.random.default_rng(seed)
    a = rng.normal(0, 0.01, n)
    b = -a + rng.normal(0, 0.0002, n)
    idx = pd.bdate_range("2024-01-01", periods=n).strftime("%Y-%m-%d")
    return pd.DataFrame({"AAA": a, "BBB": b}, index=idx)


# ---------------- 白名单（§3.2 三条件 + warmup + 当期清单） ----------------

class TestWhitelist(unittest.TestCase):

    def test_negative_pair_selected(self):
        win = neg_corr_returns(260)
        self.assertEqual(hp.estimate_pair_whitelist(win), [("AAA", "BBB")])

    def test_positive_pair_excluded(self):
        win = neg_corr_returns(260, seed=11)
        win["BBB"] = -win["BBB"]          # 变正 相关
        self.assertEqual(hp.estimate_pair_whitelist(win), [])

    def test_back_half_positive_excluded(self):
        """整体 ρ250 < −0.30 但后半窗正相关 → 剔除。"""
        n, half = 260, 130
        rng = np.random.default_rng(3)
        z1, z2, e = (rng.normal(0, 1, half) for _ in range(3))
        a = np.concatenate([1.5 * z1, z2])
        b = np.concatenate([-1.5 * z1, 0.9 * z2 + 0.05 * e])
        idx = pd.bdate_range("2024-01-01", periods=n).strftime("%Y-%m-%d")
        win = pd.DataFrame({"AAA": a, "BBB": b}, index=idx)
        self.assertLess(win.corr().loc["AAA", "BBB"], -0.30)          # 进 ρ250 门槛
        self.assertGreater(win.iloc[half:].corr().loc["AAA", "BBB"], 0)  # 后半窗正
        self.assertEqual(hp.estimate_pair_whitelist(win), [])

    def test_rho60_positive_excluded(self):
        """整体 ρ250 < −0.30、前后半窗均负，但末 60 日正相关 → 剔除。"""
        rng = np.random.default_rng(5)
        z = rng.normal(0, 1, 200)
        w = rng.normal(0, 1, 60)
        e = rng.normal(0, 0.05, 60)
        a = np.concatenate([z, w])
        b = np.concatenate([-z, w + e])
        idx = pd.bdate_range("2024-01-01", periods=260).strftime("%Y-%m-%d")
        win = pd.DataFrame({"AAA": a, "BBB": b}, index=idx)
        self.assertLess(win.corr().loc["AAA", "BBB"], -0.30)
        self.assertLess(win.iloc[:130].corr().loc["AAA", "BBB"], 0)
        self.assertLess(win.iloc[130:].corr().loc["AAA", "BBB"], 0)
        self.assertGreater(win.tail(60).corr().loc["AAA", "BBB"], 0)
        self.assertEqual(hp.estimate_pair_whitelist(win), [])

    def test_insufficient_window_empty(self):
        self.assertEqual(hp.estimate_pair_whitelist(neg_corr_returns(100)), [])

    def test_monthly_warmup(self):
        """warmup：前置收益不足 250 行的月份不入白名单/测试窗。"""
        ret = neg_corr_returns(300)
        wls = hp.monthly_pair_whitelists(ret)
        self.assertTrue(wls)
        months = sorted({d[:7] for d in ret.index})
        for m in wls:                     # 每个生效月前置行数必 >=250
            prior = [d for d in ret.index if d[:7] < m]
            self.assertGreaterEqual(len(prior), 250)
        first_valid = next(m for m in months
                           if len([d for d in ret.index if d[:7] < m]) >= 250)
        self.assertEqual(min(wls), first_valid)
        self.assertEqual(wls[first_valid], [("AAA", "BBB")])

    def test_stable_pairs_current(self):
        ret = neg_corr_returns(300)
        months = sorted({d[:7] for d in ret.index})
        cur = hp.stable_pairs_current(ret, asof_month=months[-1])
        self.assertEqual(cur["effective_month"], months[-1])
        self.assertEqual(cur["pairs"], [("AAA", "BBB")])
        prev_days = [d for d in ret.index if d[:7] < months[-1]]
        self.assertEqual(cur["estimated_at"], prev_days[-1])   # 上月末重估
        self.assertEqual(cur["window_days"], 250)
        # 窗口不满 → None
        self.assertIsNone(hp.stable_pairs_current(neg_corr_returns(100)))


# ---------------- 触发（阈值严格性 / 双向 / ISO 周去重） ----------------

class TestTriggers(unittest.TestCase):

    def test_rules(self):
        idx = ["2025-01-06", "2025-01-07", "2025-01-08", "2025-01-13"]
        ret = pd.DataFrame({"AAA": [-0.02, -0.03, 0.01, -0.015],
                            "BBB": [0.01, 0.005, -0.02, 0.006]}, index=idx)
        wl = {"2025-01": [("AAA", "BBB")]}
        trigs = hp.generate_triggers(ret, wl)
        # 01-06 触发(A跌买B)；01-07 同对同 ISO 周 → 去重；01-08 反向(B跌买A)为不同
        # 有序对 → 放行；01-13 新周 → 放行
        self.assertEqual(trigs, [hp.Trigger("2025-01-06", "AAA", "BBB"),
                                 hp.Trigger("2025-01-08", "BBB", "AAA"),
                                 hp.Trigger("2025-01-13", "AAA", "BBB")])

    def test_strict_thresholds_and_nan(self):
        ret = pd.DataFrame({"AAA": [-0.010, -0.02, -0.02],
                            "BBB": [0.001, 0.0, np.nan]},
                           index=["2025-01-06", "2025-01-07", "2025-01-08"])
        wl = {"2025-01": [("AAA", "BBB")]}
        self.assertEqual(hp.generate_triggers(ret, wl), [])   # 全部不满足严格不等式

    def test_whitelist_month_gate(self):
        ret = pd.DataFrame({"AAA": [-0.02], "BBB": [0.01]}, index=["2024-12-30"])
        self.assertEqual(hp.generate_triggers(ret, {"2025-01": [("AAA", "BBB")]}), [])


# ---------------- 底仓（受控 momentum Top5） ----------------

class TestPicks(unittest.TestCase):

    def _panel(self):
        jan = pd.bdate_range("2025-01-01", "2025-01-31").strftime("%Y-%m-%d").tolist()
        feb = pd.bdate_range("2025-02-01", "2025-02-28").strftime("%Y-%m-%d").tolist()
        data = {}
        for code, end in [("A", 120.0), ("B", 115.0), ("C", 110.0),
                          ("D", 105.0), ("E", 102.0), ("F", 101.0)]:
            ramp = np.linspace(100.0, end, len(jan))
            data[code] = {**{d: float(v) for d, v in zip(jan, ramp)}}
        for code in ["A", "B", "C", "D", "E"]:            # 2 月持平
            data[code].update({d: data[code][jan[-1]] for d in feb})
        data["F"].update({d: v for d, v in                    # F 二月拉升
                          zip(feb, np.linspace(101.0, 300.0, len(feb)))})
        g = {d: float(v) for d, v in                        # G 仅 20 根（不足 21）
             zip(jan[:20], np.linspace(100.0, 200.0, 20))}
        data["G"] = g
        return make_panel(data), jan, feb

    def test_top5_and_insufficient_history(self):
        px, jan, feb = self._panel()
        picks = hp.momentum_month_picks(px, ["2025-02", "2025-03"])
        self.assertEqual(picks["2025-02"], ["A", "B", "C", "D", "E"])  # G 不足 21 根剔除
        self.assertEqual(picks["2025-03"][0], "F")                     # 只用 2 月末前数据
        self.assertTrue(set(picks["2025-03"]) <= {"A", "B", "C", "D", "E", "F"})

    def test_month_end_set_and_base_targets(self):
        px, jan, feb = self._panel()
        days = jan + feb
        me = hp.month_end_set(days)
        self.assertEqual(me, {jan[-1]})               # 数据末日（2 月末）不算月末
        picks = {"2025-01": ["O"], "2025-02": ["A", "B"]}
        tgt = hp.build_base_targets(days, picks, me, ["2025-01", "2025-02"])
        self.assertEqual(tgt[jan[-2]], {"O": 0.20})           # 月中持当月
        self.assertEqual(tgt[jan[-1]], {"A": 0.20, "B": 0.20})  # 月末收盘切下月
        self.assertEqual(tgt[feb[0]], {"A": 0.20, "B": 0.20})
        self.assertEqual(tgt[feb[-1]], {"A": 0.20, "B": 0.20})  # 数据末日不算月末


# ---------------- 触发日目标（30% 上调 / 等比缩 / 现金优先 / T+1） ----------------

class TestTargets(unittest.TestCase):

    def test_override_b_in_base(self):
        base = {c: 0.20 for c in "ABCDE"}
        out = hp.apply_hedge_override(base, ["C"])
        self.assertAlmostEqual(out["C"], 0.30)
        for c in "ABDE":
            self.assertAlmostEqual(out[c], 0.175)
        self.assertAlmostEqual(sum(out.values()), 1.0)

    def test_override_b_outside_base(self):
        base = {c: 0.20 for c in "ABCDE"}
        out = hp.apply_hedge_override(base, ["F"])
        self.assertAlmostEqual(out["F"], 0.30)
        for c in "ABCDE":
            self.assertAlmostEqual(out[c], 0.14)
        self.assertAlmostEqual(sum(out.values()), 1.0)

    def test_override_cash_first(self):
        base = {"A": 0.20, "B": 0.20, "D": 0.20}     # 底仓 3 只 + 40% 现金
        out = hp.apply_hedge_override(base, ["F"])
        self.assertAlmostEqual(out["F"], 0.30)
        self.assertAlmostEqual(out["A"], 0.20)        # Σ=0.9 ≤1 → 现金容纳，不缩
        self.assertAlmostEqual(sum(out.values()), 0.90)

    def test_override_two_bs_and_extreme(self):
        base = {c: 0.20 for c in "ABCDE"}
        out = hp.apply_hedge_override(base, ["C", "F"])
        self.assertAlmostEqual(out["C"], 0.30)
        self.assertAlmostEqual(out["F"], 0.30)
        self.assertAlmostEqual(sum(out.values()), 1.0)
        out4 = hp.apply_hedge_override(base, ["C", "F", "A", "B"])   # 4×30%>100%
        self.assertAlmostEqual(sum(out4.values()), 1.0)
        self.assertAlmostEqual(out4["C"], 0.25)

    def test_signal_targets_revert_and_newest(self):
        d = pd.bdate_range("2025-03-03", periods=5).strftime("%Y-%m-%d").tolist()
        base = {x: {"A": 0.20, "B": 0.20} for x in d}
        di = {x: i for i, x in enumerate(d)}
        t1 = hp.Trigger(d[2], "CCC", "B")
        sig, by = hp.build_signal_targets(d, base, [t1], di, 0)
        self.assertEqual(sig[d[2]], {"A": 0.20, "B": 0.30})
        self.assertEqual(sig[d[3]], base[d[3]])                    # 次日回落
        self.assertEqual(sig[d[1]], base[d[1]])
        self.assertEqual(by[d[2]], [t1])
        # 窗口重叠最新优先：回落日新触发 → 新目标直接生效
        t2 = hp.Trigger(d[3], "CCC", "B")
        sig2, _ = hp.build_signal_targets(d, base, [t1, t2], di, 0)
        self.assertEqual(sig2[d[3]], {"A": 0.20, "B": 0.30})

    def test_t1_arm_shift_and_drop(self):
        d = pd.bdate_range("2025-03-03", periods=5).strftime("%Y-%m-%d").tolist()
        base = {x: {"A": 0.20, "B": 0.20} for x in d}
        di = {x: i for i, x in enumerate(d)}
        t = hp.Trigger(d[2], "CCC", "B")
        t1, _ = hp.build_signal_targets(d, base, [t], di, 1)
        self.assertEqual(t1[d[2]], base[d[2]])                     # 触发日不动
        self.assertEqual(t1[d[3]], {"A": 0.20, "B": 0.30})         # 次日执行
        t_last = hp.Trigger(d[4], "CCC", "B")
        t1b, _ = hp.build_signal_targets(d, base, [t_last], di, 1)
        self.assertEqual(t1b[d[4]], base[d[4]])                    # 越出末尾 → 丢弃


# ---------------- 组合引擎（收盘执行 / 只计变动 / 停牌顺延） ----------------

class TestEngine(unittest.TestCase):

    def test_basic_accounting(self):
        d = pd.bdate_range("2025-01-06", periods=4).strftime("%Y-%m-%d").tolist()
        px = make_panel({"X": dict(zip(d, [100.0, 110.0, 100.0, 100.0])),
                         "Y": dict(zip(d, [100.0, 100.0, 100.0, 100.0]))})
        tgt = {x: {"X": 0.5, "Y": 0.5} for x in d}
        res = hp.run_weight_portfolio(px.ffill(), px.notna(), tgt, d)
        nav0 = 1.0 - 0.0015 - 0.003                     # 建仓成本+尾盘扣减
        self.assertAlmostEqual(res["nav"][d[0]], nav0, places=12)
        self.assertEqual(res["returns"][d[0]], 0.0)
        nav1 = -0.0045 + 0.005 * 110 + 0.005 * 100
        self.assertAlmostEqual(res["nav"][d[1]], nav1, places=12)
        self.assertAlmostEqual(res["returns"][d[1]], nav1 / nav0 - 1, places=12)
        self.assertAlmostEqual(res["nav"][d[2]], nav0, places=12)
        self.assertEqual([e["date"] for e in res["events"]], [d[0]])   # 目标不变不交易
        self.assertAlmostEqual(res["events"][0]["drag"], 0.003, places=15)
        self.assertAlmostEqual(res["events"][0]["cost"], 0.0015, places=15)
        self.assertAlmostEqual(res["turnover_total"], 1.0, places=12)

    def test_target_change_costs(self):
        d = pd.bdate_range("2025-01-06", periods=4).strftime("%Y-%m-%d").tolist()
        px = make_panel({"X": dict(zip(d, [100.0, 110.0, 100.0, 100.0])),
                         "Y": dict(zip(d, [100.0, 100.0, 100.0, 100.0]))})
        w0, w1 = {"X": 0.5, "Y": 0.5}, {"X": 0.25, "Y": 0.75}
        tgt = {d[0]: w0, d[1]: w0, d[2]: w1, d[3]: w1}
        res = hp.run_weight_portfolio(px.ffill(), px.notna(), tgt, d)
        nav_ref = -0.0045 + 0.005 * 100 + 0.005 * 100    # D2 盯市后（X 回落 100）
        wx, wy = 0.25 * nav_ref, 0.75 * nav_ref
        turn = abs(wx - 0.5) + abs(wy - 0.5)
        self.assertAlmostEqual(res["events"][-1]["turnover"], turn, places=12)
        self.assertAlmostEqual(res["events"][-1]["cost"], 0.0015 * turn, places=15)
        self.assertAlmostEqual(res["events"][-1]["drag"], 0.003 * turn, places=15)
        self.assertAlmostEqual(res["nav"][d[2]], nav_ref - 0.0045 * turn, places=12)
        self.assertAlmostEqual(res["nav"][d[3]], res["nav"][d[2]], places=12)

    def test_halt_carry_and_deferred_sell(self):
        d = pd.bdate_range("2025-01-06", periods=5).strftime("%Y-%m-%d").tolist()
        z = {d[0]: 100.0, d[1]: 100.0, d[2]: None, d[3]: 100.0, d[4]: 100.0}
        px = make_panel({"X": {x: 100.0 for x in d}, "Z": z})
        tgt = {d[0]: {"Z": 1.0}, d[1]: {"Z": 1.0}}
        tgt.update({x: {"X": 1.0} for x in d[2:]})      # 停牌日切目标
        res = hp.run_weight_portfolio(px.ffill(), px.notna(), tgt, d)
        self.assertEqual([e["date"] for e in res["events"]], [d[0], d[2], d[3]])
        # d2：Z 停牌盯市沿用（该日零收益贡献），卖出顺延；X 买入照常
        nav2 = (-0.0045 - 0.9955 - 0.0015 * 0.9955 - 0.003 * 0.9955
                + 0.9955 + 0.01 * 100.0)
        self.assertAlmostEqual(res["nav"][d[2]], nav2, places=12)
        # d3：Z 复牌，顺延卖出成交（换手 = 全部 Z 市值）；NAV 只减卖出费用+扣减
        self.assertAlmostEqual(res["events"][2]["turnover"], 1.0, places=12)
        nav3 = nav2 - 0.0015 - 0.003
        self.assertAlmostEqual(res["nav"][d[3]], nav3, places=12)
        self.assertAlmostEqual(res["nav"][d[4]], nav3, places=12)

    def test_deferred_buy_on_first_bar(self):
        d = pd.bdate_range("2025-01-06", periods=3).strftime("%Y-%m-%d").tolist()
        px = make_panel({"X": {x: 100.0 for x in d},
                         "W": {d[0]: None, d[1]: 200.0, d[2]: 200.0}})
        tgt = {x: {"X": 0.5, "W": 0.5} for x in d}
        res = hp.run_weight_portfolio(px.ffill(), px.notna(), tgt, d)
        self.assertEqual([e["date"] for e in res["events"]], [d[0], d[1]])
        self.assertAlmostEqual(res["events"][0]["turnover"], 0.5, places=12)  # 只买 X


# ---------------- Gate 谓词 ----------------

class TestGates(unittest.TestCase):

    def test_h0_median_with_zero_months(self):
        months = ["2025-01", "2025-02", "2025-03", "2025-04"]
        few = [hp.Trigger("2025-01-0%d" % (i + 1), "A", "B") for i in range(2)]
        r = hp.gate_h0(few, months)
        self.assertFalse(r["ok"])
        self.assertEqual(r["median"], 0.0)
        self.assertEqual(r["counts"], {"2025-01": 2, "2025-02": 0,
                                       "2025-03": 0, "2025-04": 0})
        many = few + [hp.Trigger("2025-04-01", "A", "B") for _ in range(70)]
        self.assertFalse(hp.gate_h0(many, months)["ok"])       # 72 个但中位 1
        spread = ([hp.Trigger("2025-0%d-01" % m, "A", "B") for m in (1, 2, 3)
                   for _ in range(3)]
                  + [hp.Trigger("2025-04-01", "A", "B") for _ in range(61)])
        r2 = hp.gate_h0(spread, months)
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["median"], 3.0)

    def test_excess_numbers(self):
        idx = ["2025-01-02", "2025-01-03"]
        rs = pd.Series([0.01, 0.02], index=idx)
        rb = pd.Series([0.001, 0.002], index=idx)
        ex = hp.gate_excess(rs, rb)
        ts, tb = 1.01 * 1.02 - 1, 1.001 * 1.002 - 1
        ann_s, ann_b = (1 + ts) ** 126 - 1, (1 + tb) ** 126 - 1
        self.assertAlmostEqual(ex["exc_ann"], ann_s - ann_b, places=12)
        self.assertAlmostEqual(ex["seg_exc"]["2025H1"], ts - tb, places=12)
        self.assertEqual(ex["n_pos_segs"], 1)
        self.assertAlmostEqual(ex["mdd_gap"], 0.0, places=12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
