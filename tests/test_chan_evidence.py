"""缠论 LLM 证据版（全量打包批 · 批次 2）单测 —— 纯合成数据，绝不连 DB。

施工方案：docs/全量打包施工方案-2026-09-20.md §3.3（预注册冻结）+ signals/
chan_evidence.py docstring 实现层钉死 a~j。纪律：本文件零 DB 访问（连只读也不
允许）；全部用 tests/chan_fixtures 的合成黄金序列（THIRDBUY_CASES[0]，36 根、
三买确认 bar=35）与手工信号字典驱动。

用例清单（覆盖验收四件 + 披露函数）：
- 锁定语义：首发确认即锁定（末根合并K延长再发被 <5 交易日去重吸收，不开新事件）；
- 历史标签永不回改：前缀稳定性（labels(bars[:n]) == labels(bars[:n+k]) 前 n 行），
  两机制各验一遍（i_end 延长移位 / 深跌结构改写致终态信号消失——标签仍持仓不改）；
- 出场解锁：顶分型确认日次一 bar 翻转「三买结束(顶分型)」；20 日先到第 20 根翻转
  「三买结束(20日)」；状态机层面：持仓中忽略（[首发, 出场) 闭开区间）、出场执行日
  当日解锁开新事件（同枢再确认 = 新事件序号）、末根确认不可交易 → 持有至末；
- 标签域完整性：值域闭集 + 状态转移合法性（无→持仓→结束→持仓…，无不可逆回退）+
  行数/日期唯一性 + warmup 门（auto 模式 120 根有效 bar；不足 120 无资格空流）；
- 上下文标签：fixture 手推值（当前笔方向/最近分型与距今/最近中枢与位置）逐项对数；
- 披露函数：T+20 终态相容率（同日/近距≤8/消失三分）、标签日变更频率（离散字段）。
"""
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

os.environ["AGSICKLE_DISABLE_LIVE_QUOTES"] = "1"  # 测试保持离线

from signals.chan_evidence import (  # noqa: E402
    EXIT_DAY20, EXIT_HOLD_END, EXIT_TOP, POS_INSIDE, TB_END, TB_HOLD, TB_NONE,
    change_frequency, context_labels, evidence_pipeline, r3_relationship,
    state_timeline, tb_events, terminal_compatibility)
from tests.chan_fixtures import THIRDBUY_CASES, bars_to_ohlc, make_dates  # noqa: E402

_TESTS = []


def test(fn):
    _TESTS.append(fn)
    return fn


test.__test__ = False  # pytest 不要把装饰器本身当测试收集

SUCCESS_BARS = THIRDBUY_CASES[0]["bars_hl"]      # 36 根，三买确认 bar=35（fixture 手推）

# 追加 bar 情景（沿用 test_chan_causal 的构造约定，推见各用例注释）：
EXT_BARS = SUCCESS_BARS + [(13.5, 13.4)]                  # 37 根：bar36 ⊆ K35 → i_end 延长，确认日后移
DROP_BARS = SUCCESS_BARS + [                              # 44 根：深跌改写 → 终态信号消失
    (13.5, 13.2), (13.3, 13.0), (13.1, 12.8), (12.9, 12.6),
    (12.7, 12.4), (12.5, 12.2), (12.8, 12.5), (13.0, 12.7)]
TOP_EXIT_BARS = SUCCESS_BARS + [                          # 40 根：K36~K39 新高回撤 → 顶分型确认 bar38
    (13.8, 13.4), (14.2, 13.8), (13.9, 13.5), (13.7, 13.4)]
DAY20_BARS = (SUCCESS_BARS + [(13.8, 13.4), (13.9, 13.5)]  # 58 根：20 根严格上行 → 无顶分型，20 日出场
              + [(13.8 + 0.1 * k, 13.4 + 0.1 * k) for k in range(2, 22)])


def _run(bars_hl, code="X"):
    """合成 (h,l) 序列 → evidence_pipeline（显式 warmup=None = 不设限制，钉死 a）。"""
    n = len(bars_hl)
    df = bars_to_ohlc(bars_hl)
    cal = make_dates(n)
    return evidence_pipeline(df, cal, code, warmup_start=None,
                             high_col="high", low_col="low", close_col="close")


# ---------------- 1. 锁定语义：首发确认即锁定（延长再发不开新事件） ----------------

@test
def test_first_confirmation_locks():
    # EXT_BARS：末根合并K被 bar36 包含延长 → 原始发射 2 次（t=35/36），<5 交易日去重
    # 只保留第一次 → 事件恰 1 个、首发确认 = dates[35]（首发即锁定，钉死 b/d）
    res = _run(EXT_BARS)
    assert [s["confirm_date"] for s in res["emitted"]] == [res["dates"][35]]
    assert len(res["events"]) == 1
    ev = res["events"][0]
    assert ev["seq"] == 1 and ev["confirm_date"] == res["dates"][35]
    assert ev["entry_date"] == res["dates"][36]
    # 确认日当日标签即锁定为「三买持仓」，次日仍持仓（出场未触发）
    lab = {r["date"]: r for r in res["labels"]}
    assert lab[res["dates"][35]]["tb_state"] == TB_HOLD
    assert lab[res["dates"][36]]["tb_state"] == TB_HOLD
    assert lab[res["dates"][35]]["tb_confirm"] == res["dates"][35]
    assert lab[res["dates"][36]]["tb_days_since_confirm"] == 1


# ---------------- 2. 历史标签永不回改：前缀稳定性（重画两机制各验一遍） ----------------

@test
def test_labels_never_repaint_extension():
    # 机制一（i_end 延长移位）：36 根流的前 36 行 == 37 根流的前 36 行（逐字段）；
    # 终态确认日已移至 36，但首发标签锁死在 35，不回改
    short = _run(SUCCESS_BARS)
    long_ = _run(EXT_BARS)
    assert short["labels"] == long_["labels"][:len(short["labels"])]
    term = [s["confirm_date"] for s in long_["terminal"]]
    assert term == [long_["dates"][36]]          # 终态结构确实移位（重画事实）
    assert long_["labels"][-1]["tb_state"] == TB_HOLD   # 标签不改：仍持仓


@test
def test_labels_never_repaint_structure_rewrite():
    # 机制二（结构改写消失）：深跌序列终态无任何三买（fixture test_repaint_
    # structure_rewrite 同构造），但 d35 锁定的持仓标签保持到数据末，不回改为「无」
    res = _run(DROP_BARS)
    assert res["terminal"] == []
    assert len(res["events"]) == 1
    assert all(r["tb_state"] == TB_HOLD for r in res["labels"]
               if r["date"] >= res["dates"][35])
    short = _run(SUCCESS_BARS)
    assert short["labels"] == res["labels"][:len(short["labels"])]


# ---------------- 3. 出场解锁：顶分型确认 / 20 日先到（真实 bar 全链） ----------------

@test
def test_exit_unlock_top_fractal():
    # TOP_EXIT_BARS：K36=(13.8,13.4) K37=(14.2,13.8) K38=(13.9,13.5) 均为新合并K
    # （互不包含、不并入 K35）→ 顶分型@K37 于 bar38 确认 → 次日 bar39 开盘出（§3.6-8）；
    # 入场 d36，20 日出场日 d55 不存在 → 顶分型先到。d35~d38 持仓，d39 翻转结束。
    res = _run(TOP_EXIT_BARS)
    d = res["dates"]
    ev = res["events"][0]
    assert ev["entry_date"] == d[36]
    assert ev["exit_date"] == d[39] and ev["exit_kind"] == EXIT_TOP
    lab = {r["date"]: r for r in res["labels"]}
    for i in (35, 36, 37, 38):
        assert lab[d[i]]["tb_state"] == TB_HOLD, i
    assert lab[d[39]]["tb_state"] == TB_END
    assert lab[d[39]]["tb_exit_kind"] == EXIT_TOP
    assert lab[d[39]]["tb_days_since_confirm"] == 4
    assert lab[d[-1]]["tb_state"] == TB_END      # 出场后保持结束态至末（无新事件）


@test
def test_exit_unlock_day20():
    # DAY20_BARS：入场 d36 为第 1 根，第 20 根 = d55 收盘出（§3.6-8）；全程无顶分型
    res = _run(DAY20_BARS)
    d = res["dates"]
    ev = res["events"][0]
    assert ev["exit_date"] == d[36 + 19] and ev["exit_kind"] == EXIT_DAY20
    lab = {r["date"]: r for r in res["labels"]}
    assert all(lab[d[i]]["tb_state"] == TB_HOLD for i in range(35, 55))
    assert lab[d[55]]["tb_state"] == TB_END
    assert lab[d[55]]["tb_exit_kind"] == EXIT_DAY20
    assert lab[d[57]]["tb_state"] == TB_END


@test
def test_event_machine_ignore_unlock_and_holdend():
    # 状态机直测（手工信号字典 + 顶分型事件流，钉死 c/d/e）：
    # sig@10 → 入场 11、20 日出场 30；sig@15/sig@29 ∈ [10,30) 持仓中忽略；
    # sig@30 == 出场执行日 → 当日已结束，开新事件（同枢再确认 = 新事件序号 2）
    dates = make_dates(40)
    pos = {x: i for i, x in enumerate(dates)}

    def sig(i):
        return {"code": "X", "confirm_date": dates[i], "trigger_pi": i,
                "zg": 10.0, "zd": 9.0, "trigger_price": 10.5,
                "seg_id": 0, "pivot_first_stroke_idx": 0}

    ev = tb_events([sig(10), sig(15), sig(29), sig(30)], dates, pos, [])
    assert len(ev["events"]) == 2 and ev["n_held_ignored"] == 2
    assert ev["events"][0]["exit_date"] == dates[30]
    assert ev["events"][0]["exit_kind"] == EXIT_DAY20
    assert ev["events"][1]["seq"] == 2
    assert ev["events"][1]["confirm_date"] == dates[30]   # 出场日当日解锁
    assert ev["n_same_pivot_reconfirm"] == 1              # 同枢再确认按新事件记录
    # 顶分型出场：sig@10 入场 11，顶分型确认 14 → 卖出日 15；sig@14 忽略、sig@15 解锁
    ev2 = tb_events([sig(10), sig(14), sig(15)], dates, pos, [dates[14]])
    assert ev2["events"][0]["exit_date"] == dates[15]
    assert ev2["events"][0]["exit_kind"] == EXIT_TOP
    assert [e["confirm_date"] for e in ev2["events"]] == [dates[10], dates[15]]
    # 末根确认（不可交易）仍开事件：持仓至末，exit=None（钉死 e）
    ev3 = tb_events([sig(39)], dates, pos, [])
    assert len(ev3["events"]) == 1 and ev3["n_untradable"] == 1
    assert ev3["events"][0]["exit_date"] is None
    assert ev3["events"][0]["exit_kind"] == EXIT_HOLD_END
    # 出场为 None 的既有事件 → 其后一切信号持仓中忽略
    ev4 = tb_events([sig(39), sig(39)], dates, pos, [])
    assert len(ev4["events"]) == 1 and ev4["n_held_ignored"] == 1


# ---------------- 4. 标签域完整性 + warmup 门 ----------------

@test
def test_label_domain_and_transitions():
    res = _run(TOP_EXIT_BARS)
    labels = res["labels"]
    dates = res["dates"]
    # 行数 = 全部 bar 日期（warmup=None）；日期升序唯一
    assert len(labels) == len(dates)
    assert [r["date"] for r in labels] == list(dates)
    # 值域闭集（钉死 schema）
    for r in labels:
        assert r["tb_state"] in (TB_NONE, TB_HOLD, TB_END)
        assert r["tb_exit_kind"] in (None, EXIT_TOP, EXIT_DAY20)
        assert r["stroke_dir"] in ("up", "down", None)
        assert r["last_fractal"] in ("T", "B", None)
        assert r["pivot_pos"] in ("上", "内", "下", None)
        # 上下文字段成组出现/成组缺省
        assert (r["last_fractal"] is None) == (r["last_fractal_days"] is None)
        assert (r["pivot_zd"] is None) == (r["pivot_zg"] is None) \
            == (r["pivot_pos"] is None)
        # 三买字段一致性
        if r["tb_state"] == TB_NONE:
            assert r["tb_confirm"] is None and r["tb_event_seq"] is None \
                and r["tb_days_since_confirm"] is None
        else:
            assert r["tb_confirm"] is not None and r["tb_event_seq"] >= 1
            assert r["tb_days_since_confirm"] == \
                res["pos"][r["date"]] - res["pos"][r["tb_confirm"]]
            if r["tb_state"] == TB_HOLD:
                assert r["tb_exit_kind"] is None
            else:
                assert r["tb_exit_kind"] in (EXIT_TOP, EXIT_DAY20)
    # 状态转移合法：无 只作前缀；离开后不回；持仓 → 持仓/结束；结束 → 结束/持仓
    states = [r["tb_state"] for r in labels]
    seen_event = False
    for a, b in zip(states, states[1:]):
        if a == TB_NONE:
            assert not seen_event
            assert b in (TB_NONE, TB_HOLD)
        else:
            seen_event = True
            assert b in (TB_HOLD, TB_END)      # 一旦有事件，不再回「无」
    assert TB_HOLD in states and TB_END in states and TB_NONE in states


@test
def test_warmup_gate_and_unqualified():
    # auto warmup：130 根全有效（qfq 列补齐）→ 标签自第 120 根有效 bar 日起（11 行）；
    # 100 根 < 120 → 无信号资格票：标签流为空（钉死 a）
    def full_df(bars_hl):
        df = bars_to_ohlc(bars_hl)
        for col, src in (("close_qfq", "close"), ("high_qfq", "high"),
                         ("low_qfq", "low")):
            df[col] = df[src]
        return df

    down130 = [(100.0 - i, 99.0 - i) for i in range(130)]   # 严格下行：无包含无分型
    cal = make_dates(130)
    res = evidence_pipeline(full_df(down130), cal, "W")
    assert res["warmup_start"] == cal[119]
    assert len(res["labels"]) == 11
    assert all(r["date"] >= cal[119] for r in res["labels"])
    assert all(r["tb_state"] == TB_NONE for r in res["labels"])
    assert all(r["stroke_dir"] is None and r["last_fractal"] is None
               and r["pivot_pos"] is None for r in res["labels"])
    down100 = [(100.0 - i, 99.0 - i) for i in range(100)]
    res2 = evidence_pipeline(full_df(down100), make_dates(100), "W")
    assert res2["warmup_start"] is None
    assert res2["labels"] == [] and res2["events"] == []   # 无资格：空流


# ---------------- 5. 上下文标签：fixture 手推值逐项对数（钉死 f） ----------------

@test
def test_context_labels_hand_derived():
    # d35（确认日，前缀=全 36 根）：保留分型末位 B@34 → 形成笔向上；最近确认分型
    # = B4@34（conf_bar=35）→ B、距今 0；最近中枢 = i=5 枢（s5,s6,s7，last=7/start=5
    # 字典序最大）[ZD=13.0,ZG=14.0]，现价 13.7（bar35 上行 close=high）→ 内
    res = _run(SUCCESS_BARS)
    lab = {r["date"]: r for r in res["labels"]}
    r35 = lab[res["dates"][35]]
    assert r35["stroke_dir"] == "up"
    assert r35["last_fractal"] == "B" and r35["last_fractal_days"] == 0
    assert r35["pivot_zd"] == 13.0 and r35["pivot_zg"] == 14.0
    assert r35["pivot_pos"] == POS_INSIDE
    # d34（B4 尚未确认——其第 3 根合并K=bar35 不在前缀内）：最近确认分型 = T@30
    # （conf_bar=31）→ T、距今 3；保留分型末位 T@30 → 形成笔向下；最近中枢 =
    # i=4 枢（s4,s5,s6）[12.8,14.0]，现价 13.0（bar34 下行 close=low）→ 内
    r34 = lab[res["dates"][34]]
    assert r34["stroke_dir"] == "down"
    assert r34["last_fractal"] == "T" and r34["last_fractal_days"] == 3
    assert abs(r34["pivot_zd"] - 12.8) < 1e-9 and r34["pivot_zg"] == 14.0
    assert r34["pivot_pos"] == POS_INSIDE


# ---------------- 6. 披露函数：①相容率 / ②变更频率 / ④R3 关系 ----------------

@test
def test_terminal_compatibility_metric():
    dates = make_dates(60)
    pos = {x: i for i, x in enumerate(dates)}

    def ev(i):
        return {"confirm_date": dates[i]}

    terminal = [{"confirm_date": dates[10]}, {"confirm_date": dates[17]}]
    # 事件 @10（同日）、@20（距终态 17 差 7 ≤8 近距）、@50（距最近终态 33 >8 消失）
    r = terminal_compatibility([ev(10), ev(20), ev(50)], terminal, pos)
    assert r["n_events"] == 3 and r["n_compat"] == 2 and r["n_exact"] == 1
    assert r["n_vanish"] == 1 and abs(r["compat_rate"] - 2 / 3) < 1e-12
    assert r["vanished"] == [{"confirm_date": dates[50]}]
    # 无事件票：比率 None（不计分母）， vanished 空
    r0 = terminal_compatibility([], terminal, pos)
    assert r0["compat_rate"] is None and r0["n_events"] == 0


@test
def test_change_frequency_metric():
    def row(state, fr, pos_):
        return {"tb_state": state, "stroke_dir": "up", "last_fractal": fr,
                "pivot_pos": pos_}

    labels = [row(TB_NONE, "T", "内"), row(TB_NONE, "T", "上"),
              row(TB_HOLD, "T", "上"), row(TB_HOLD, "B", "上")]
    r = change_frequency(labels)
    assert r["n_days"] == 4 and r["n_change"] == 3      # 内→上 / 无→持仓 / T→B
    assert abs(r["freq"] - 1.0) < 1e-12
    assert r["n_change_tb"] == 1 and abs(r["freq_tb"] - 1 / 3) < 1e-12
    # 单行 / 空流：频率 None
    assert change_frequency(labels[:1])["freq"] is None
    assert change_frequency([])["freq"] is None


@test
def test_r3_relationship_identity():
    # 恒等式（钉死 i）：|E_r3| = 窗口内事件数 + 窗口内持仓中忽略数
    dates = make_dates(300)
    pos = {x: i for i, x in enumerate(dates)}

    def sig(i, code="X"):
        return {"code": code, "confirm_date": dates[i], "trigger_pi": i,
                "zg": 10.0, "zd": 9.0, "trigger_price": 10.5,
                "seg_id": 0, "pivot_first_stroke_idx": 0}

    # 票 X：窗口前后各一信号、无重叠 → 两事件；票 Y：两信号均在 X 池外独立成事件
    results = {}
    for code, sigs in (("X", [sig(150), sig(152), sig(151)]),
                       ("Y", [sig(160, "Y"), sig(161, "Y")])):
        d = dates
        ev = tb_events(sigs, d, pos, [])
        results[code] = {"emitted": sigs, "events": ev["events"],
                         "labels": [], "pos": pos}
    # X: sig@150 事件（出场 150+1+19=170，20日）；sig@152/151 ∈ [150,170) 忽略
    # Y: sig@160 事件（出场 180）；sig@161 忽略
    rel = r3_relationship(results, test_start=dates[100])
    assert rel["n_r3_emitted"] == 5
    assert rel["n_evidence_events_in_window"] == 2
    assert rel["n_r3_became_event"] == 2
    assert rel["n_r3_held_ignored"] == 3
    assert rel["n_r3_emitted"] == \
        rel["n_r3_became_event"] + rel["n_r3_held_ignored"]
    assert rel["n_evidence_events_in_window"] == rel["n_r3_became_event"]


@test
def test_state_timeline_compression():
    rows = ([{"tb_state": TB_NONE, "tb_event_seq": None, "tb_exit_kind": None,
              "date": f"2025-01-{d:02d}"} for d in range(1, 4)]
            + [{"tb_state": TB_HOLD, "tb_event_seq": 1, "tb_exit_kind": None,
                "date": f"2025-01-{d:02d}"} for d in range(4, 7)]
            + [{"tb_state": TB_END, "tb_event_seq": 1, "tb_exit_kind": "20日",
                "date": f"2025-01-{d:02d}"} for d in range(7, 10)])
    tl = state_timeline(rows)
    assert len(tl) == 3
    assert tl[0][0] == (TB_NONE, None, None)
    assert tl[0][1] == "2025-01-01" and tl[0][2] == "2025-01-03"
    assert tl[1][0] == (TB_HOLD, 1, None)
    assert tl[2][0] == (TB_END, 1, "20日") and tl[2][2] == "2025-01-09"
    # 截断（cap）：标记行 key=("…", None, None)，只印说明、不含日期区间
    tl2 = state_timeline(rows, cap=2)
    assert len(tl2) == 3
    assert tl2[1][0] == ("…", None, None) and tl2[1][1] == "（略 1 段）"
    assert tl2[0][0] == (TB_NONE, None, None)
    assert tl2[2][0] == (TB_END, 1, "20日")


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
