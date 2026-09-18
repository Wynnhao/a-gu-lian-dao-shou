"""Sprint4 批次C（LLM 上下文与评估）专项测试：W-C1~W-C8。

覆盖（对应批次C验收门 1）：
- W-C1 预算降级次序（先砍滞后票墙再砍新闻正文）+ data_quality core 过滤；
- W-C2 realized_pnl 移动平均成本配比（空仓/持仓两口径）；
- W-C3 提示词红线语义存在性 + 红线缺失场景 + bundle"当前可行动空间"；
- W-C4 watch/hold 回填（t1_ret 必填、direction_hit 仅 buy/sell）+ 周报 rejected 不进分母；
- W-C5 周报基准端日期错配标注 + 超额 n/a；
- W-C6 decide 对合法 `[]` exit 0；
- W-C7 日报补跑落盘（守卫改文件存在性）+ catchup 产物校验；
- W-C8 catchup 2a 11:00 门 + bundle.HHMM.md 版本化（保留 10 份）。

全部离线（:memory: / 临时文件库 / monkey-patch），不连生产 market.db、不出网、
不写生产 logs/*.log 与 logs/session（AGSICKLE_* env 沙箱在 import 前设置）。
直跑：python3 tests/test_sprint4_c.py
"""
import contextlib
import io
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from datetime import date, datetime, time, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

# ---- 沙箱（必须在 import 任何项目模块之前设置）----
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="agsickle_c_"))
for _sub in ("signal_eval", "logs", "session"):
    (_TMP_ROOT / _sub).mkdir(parents=True, exist_ok=True)
os.environ.setdefault("AGSICKLE_SIGNAL_EVAL_DIR", str(_TMP_ROOT / "signal_eval"))
os.environ.setdefault("AGSICKLE_LOG_DIR", str(_TMP_ROOT / "logs"))
os.environ.setdefault("AGSICKLE_SESSION_DIR", str(_TMP_ROOT / "session"))
os.environ.setdefault("AGSICKLE_DISABLE_LIVE_QUOTES", "1")
os.environ.setdefault("AGSICKLE_DISABLE_NOTIFY", "1")
os.environ.setdefault("AGSICKLE_DISABLE_FETCHER", "1")
os.environ.setdefault("AGSICKLE_DISABLE_NEWS", "1")
os.environ.setdefault("AGSICKLE_DISABLE_MACRO", "1")
os.environ.setdefault("AGSICKLE_DISABLE_SPOT", "1")

from common.config import core_codes  # noqa: E402
from data.fetcher import DDL  # noqa: E402
from ai import bundle as ai_bundle  # noqa: E402
from ai import decide  # noqa: E402
from review import daily as daily_mod  # noqa: E402
from review import weekly as weekly_mod  # noqa: E402
import pipeline.catchup as catchup  # noqa: E402
import signals.hot as hot_mod  # noqa: E402
import signals.movers as movers_mod  # noqa: E402

_TESTS = []


def test(fn):
    _TESTS.append(fn)
    return fn


test.__test__ = False


def _mem() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    return conn


def _add_bar(conn, code, d, close, qfq=None):
    conn.execute(
        "INSERT OR REPLACE INTO daily_bar (code, trade_date, open, high, low, close,"
        " volume, amount, pct_chg, turnover, close_qfq) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (code, d, close, close, close, close, 10000, close * 1e4, 0.0, 1.0, qfq))


def _add_trade(conn, d, code, side, price, shares, amount):
    conn.execute(
        "INSERT INTO trade (trade_date, code, name, side, price, shares, amount,"
        " order_id, status, decision_id, shots, confirmed_by, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (d, code, "票" + code, side, price, shares, amount, "ord", "filled",
         None, "[]", "test", d + "T09:35:00"))


def _add_decision(conn, run_date, code, action, status, trade_date,
                  t1_ret=None, hit=None, conf=0.7):
    conn.execute(
        "INSERT INTO decision (run_date, code, action, target_weight, confidence,"
        " reasons, risk_notes, input_snapshot, status, created_at, trade_date,"
        " model, prompt_version, t1_ret, direction_hit)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_date, code, action, 0.1 if action in ("buy", "sell") else 0.0, conf,
         '["r1", "r2"]', "[]", "{}", status, run_date + "T09:35:00", trade_date,
         "test", "t", t1_ret, hit))


# ============================================================
# W-C1：data_quality 仅 core + 非核心折叠 + 预算降级次序
# ============================================================

@test
def test_wc1_data_quality_core_only_and_noncore_fold():
    """W-C1（P1-19）：data_quality 只统计 core_codes 的滞后票；非 core 滞后折叠为
    一行计数。730 只滞后票墙不再进入 bundle。"""
    conn = _mem()
    try:
        core = core_codes()
        assert len(core) >= 2
        d1 = date.today().isoformat()          # 证据日（最新交易日）
        d0 = (date.today() - timedelta(days=1)).isoformat()
        _add_bar(conn, core[0], d1, 100.0)     # core 新鲜
        _add_bar(conn, core[1], d0, 100.0)     # core 滞后
        _add_bar(conn, "888801", d0, 50.0)     # 非 core 滞后
        _add_bar(conn, "888802", d0, 50.0)     # 非 core 滞后
        conn.commit()
        b = ai_bundle.build_bundle(conn=conn)
        assert b["evidence_date"] == d1
        assert set(b["data_quality"].keys()) == {core[0], core[1]}, b["data_quality"]
        assert b["data_quality_noncore_lag"] == 2
        assert b["data_quality_noncore_total"] == 2
        md = ai_bundle.bundle_to_markdown(b)
        assert "另有 2 只非核心票滞后" in md
        assert f"{core[1]}({d0})" in md            # core 滞后仍逐票列
        assert "888801" not in md and "888802" not in md  # 非 core 不进正文
    finally:
        conn.close()


@test
def test_wc1_degrade_wall_first_then_news():
    """W-C1：预算降级次序——超预算先折叠滞后票墙（明细→计数），新闻正文保持
    120 字；仍超才降正文 60→0。"""
    conn = _mem()
    session_root = Path(tempfile.mkdtemp(prefix="agsickle_c_wc1_"))
    old_build = ai_bundle.build_bundle
    try:
        b = ai_bundle.build_bundle(conn=conn)  # 空表底座
        # 人为塞出"滞后票墙"：400 只核心滞后（逐票明细远超新闻正文体量）
        d0 = (date.today() - timedelta(days=1)).isoformat()
        b["data_quality"] = {f"6{i:05d}": d0 for i in range(400)}
        b["data_quality_noncore_lag"] = 0
        b["data_quality_noncore_total"] = 0
        b["news"] = {"market": [{"title": "T", "source": "S",
                                 "published_at": "P", "content": "X" * 120}]}
        for w in b.get("watchlist", []):
            b["news"][w["code"]] = []
        ai_bundle.build_bundle = lambda run_date=None, conn=None: b

        md_full = ai_bundle.bundle_to_markdown(b)
        md_fold = ai_bundle.bundle_to_markdown(b, lag_detail=False)
        assert "60000(" not in md_fold and "60399(" not in md_fold
        assert "预算降级）核心池" in md_fold
        assert "X" * 120 in md_fold, "折墙后新闻正文必须保持 120 字"

        # 阶梯1：预算卡在 full 与 folded 之间 → 只折墙、正文 120 字保留
        ai_bundle.MD_BUDGET = (len(md_full) + len(md_fold)) // 2
        os.environ["AGSICKLE_SESSION_DIR"] = str(session_root)
        _, md_path = ai_bundle.write_bundle("2026-09-19")
        md1 = md_path.read_text(encoding="utf-8")
        assert "X" * 120 in md1, "第一级降级必须先砍墙、新闻正文不动"
        assert "预算降级）核心池" in md1

        # 阶梯2：folded 仍超 → 正文 60 字（60 字正文的缩减量必须足以落入预算）
        ai_bundle.MD_BUDGET = max(1, len(md_fold) - 30)
        _, md_path = ai_bundle.write_bundle("2026-09-19")
        md2 = md_path.read_text(encoding="utf-8")
        assert "X" * 61 not in md2 and "X" * 60 in md2, md2[:200]

        # 阶梯3：还超 → 正文置空
        ai_bundle.MD_BUDGET = 100
        _, md_path = ai_bundle.write_bundle("2026-09-19")
        md3 = md_path.read_text(encoding="utf-8")
        assert "X" * 10 not in md3
    finally:
        ai_bundle.build_bundle = old_build
        ai_bundle.MD_BUDGET = 45000
        shutil.rmtree(session_root, ignore_errors=True)
        conn.close()


# ============================================================
# W-C2：realized_pnl 移动平均成本配比
# ============================================================

@test
def test_wc2_realized_pnl_moving_average_open_position():
    """W-C2（P1-20）：持仓未平仓时 realized_pnl 只结转已平仓部分
    （此前 Σ卖出−Σ买入 会把未平仓买入算成假巨亏）；净投入现金为副口径。"""
    conn = _mem()
    try:
        _add_trade(conn, "2026-09-10", "600001", "buy", 10.0, 100, 1000.0)
        _add_trade(conn, "2026-09-10", "600001", "sell", 12.0, 50, 600.0)
        conn.commit()
        b = ai_bundle.build_bundle(conn=conn)
        # realized = 600 − 50×移动成本10 = 100；净投入 = 600 − 1000 = −400
        assert abs(b["realized_pnl"] - 100.0) < 1e-6, b["realized_pnl"]
        assert abs(b["net_cash_outlay"] - (-400.0)) < 1e-6
        md = ai_bundle.bundle_to_markdown(b)
        assert "累计已实现盈亏（已平仓口径）" in md
        assert "净投入现金" in md
    finally:
        conn.close()


@test
def test_wc2_closed_position_two_agreements_match():
    """W-C2：全部平仓后两口径一致（移动成本全部结转完）。"""
    conn = _mem()
    try:
        _add_trade(conn, "2026-09-10", "600001", "buy", 10.0, 100, 1000.0)
        _add_trade(conn, "2026-09-11", "600001", "buy", 20.0, 100, 2000.0)
        _add_trade(conn, "2026-09-12", "600001", "sell", 25.0, 200, 5000.0)
        conn.commit()
        b = ai_bundle.build_bundle(conn=conn)
        assert abs(b["realized_pnl"] - 2000.0) < 1e-6
        assert abs(b["realized_pnl"] - b["net_cash_outlay"]) < 1e-6, \
            "空仓时两口径必须一致"
    finally:
        conn.close()


@test
def test_wc2_no_trades_zero_and_equal():
    """W-C2：无任何成交（空仓）→ 两口径均为 0 且一致。"""
    conn = _mem()
    try:
        b = ai_bundle.build_bundle(conn=conn)
        assert b["realized_pnl"] == 0.0 and b["net_cash_outlay"] == 0.0
    finally:
        conn.close()


# ============================================================
# W-C3：红线语义提示词 + 红线缺失场景 + 当前可行动空间
# ============================================================

@test
def test_wc3_prompt_redline_semantics_present():
    """W-C3（P1-26）：提示词必须明确红线=评估信号（降置信+更强证据+小仓位试探）、
    不是交易禁令；红线缺失场景按无红线信息处理、不得臆造数字、引用带版本。"""
    rules = ai_bundle._OUTPUT_RULES
    assert "不是交易禁令" in rules
    assert "评估信号" in rules
    assert "小仓位试探" in rules
    assert "不得因红线无条件空仓" in rules
    assert "红线数据缺失" in rules
    assert "严禁臆造" in rules
    assert "数据版本未知" in rules
    # 渲染层不再出现旧的"无条件 hold"语义
    conn = _mem()
    try:
        b = ai_bundle.build_bundle(conn=conn)
        md = ai_bundle.bundle_to_markdown(b)
        assert "无条件 hold" not in md
        # 红线缺失场景：沙箱无 latest.json → profile_verdict_missing → 渲染守则
        assert bundle_missing_verdict_renders_guidance(b)
    finally:
        conn.close()


def bundle_missing_verdict_renders_guidance(b: dict) -> bool:
    md = ai_bundle.bundle_to_markdown(b)
    return ("红线数据缺失" in md) and ("不得臆造" in md) and \
           (b.get("profile_verdict_missing") is not None)


@test
def test_wc3_redline_triggered_render_is_signal_not_ban():
    """W-C3：红线触发渲染=评估信号措辞 + 数据版本要求；携带版本时必须显示。"""
    b = {"run_date": "2026-09-19", "evidence_date": "2026-09-18"}
    v = {"profile": "momentum", "other_profile": "reversal_lowvol",
         "verdict": "hold", "suggest_profile": "momentum",
         "red_line_triggered": True,
         "thresholds": {"mdd_red_line": -0.30},
         "C": {"current": -0.41, "alt": None, "gap": None},
         "B": {"current": None, "alt": None, "gap": None},
         "votes": {"b_switch": None, "c_switch": None, "votes_switch": 0,
                   "votes_total": 2},
         "abstains": ["B (年化超额 vs HS300)"]}
    b["profile_verdict_latest"] = v
    md = ai_bundle.bundle_to_markdown(b)
    assert "不是交易禁令" in md
    assert "评估信号" in md
    assert "数据版本未知" in md, "无元数据时必须标注版本未知"
    v["backtest_generated_at"] = "2026-10-01T12:00:00"
    md2 = ai_bundle.bundle_to_markdown(b)
    assert "2026-10-01T12:00:00" in md2, "携带版本时红线引用必须带版本"
    # backtest 缺失字段（批次B退役 backtest_result.json 后的常态）→ 明示缺失守则
    v2 = dict(v, red_line_triggered=False, backtest_missing="backtest_result.json 不存在")
    b["profile_verdict_latest"] = v2
    md3 = ai_bundle.bundle_to_markdown(b)
    assert "红线数据缺失" in md3 and "不得臆造回测数字" in md3


@test
def test_wc3_actionable_space_in_bundle_and_md():
    """W-C3：bundle 增"当前可行动空间"——按 regime/拥挤/黑名单算出最大单票权重、
    总仓位上限、剩余可买入预算与黑名单禁交易名单。"""
    conn = _mem()
    se_dir = Path(os.environ["AGSICKLE_SIGNAL_EVAL_DIR"])
    crowding_file = se_dir / "factor_crowding.json"
    try:
        d1 = date.today().isoformat()
        conn.execute(
            "INSERT INTO portfolio_state VALUES (?,?,?,?,?,?,?)",
            (d1, 850000.0, 150000.0, 1000000.0, 0.0, 0, "t"))
        conn.execute(
            "INSERT INTO position VALUES ('600519','贵州茅台',1000,1000,120.0,?)",
            (d1 + "T00:00:00",))
        _add_bar(conn, "600519", d1, 150.0)     # mv=150000 → 权重 15%
        # 核心池内黑名单票：ST 名（600519 在 watchlist_core）
        conn.execute("INSERT INTO stock_info VALUES ('600519','ST测试','2024-01-02',?)",
                     (d1 + "T00:00:00",))
        conn.commit()
        b = ai_bundle.build_bundle(conn=conn)
        act = b["actionable_space"]
        assert act.get("total_weight_cap") == ai_bundle.MAX_TOTAL_WEIGHT
        assert abs(act["current_invested_weight"] - 0.15) < 1e-6, act
        assert abs(act["remaining_buy_budget"] - 0.65) < 1e-6
        assert act["single_weight_cap"] == ai_bundle.MAX_SINGLE_WEIGHT
        assert "600519" in act["blacklist_blocked_core"]
        md = ai_bundle.bundle_to_markdown(b)
        assert "当前可行动空间" in md and "总仓位上限" in md and "单票权重上限" in md

        # 拥挤熔断态 → 单票上限压到 5%
        crowding_file.write_text('{"crowded": true, "mu60": 1.2, "sigma60": 0.5,'
                                 ' "n_buckets": 10}', encoding="utf-8")
        b2 = ai_bundle.build_bundle(conn=conn)
        assert b2["actionable_space"]["single_weight_cap"] == 0.05
        assert b2["actionable_space"]["crowding_capped_to_5pct"] is True
    finally:
        if crowding_file.exists():
            crowding_file.unlink()
        conn.close()


# ============================================================
# W-C4：watch/hold 回填 + 周报分组与 rejected 不进分母
# ============================================================

@test
def test_wc4_backfill_covers_watch_hold():
    """W-C4（P1-27）：回填扩展到 watch/hold——t1_ret 必填；direction_hit 仅对
    buy/sell 打分，watch/hold 保持 NULL。"""
    conn = _mem()
    try:
        _add_bar(conn, "600001", "2026-09-14", 100.0)
        _add_bar(conn, "600001", "2026-09-15", 110.0)   # 决策日收盘（base）
        _add_bar(conn, "600001", "2026-09-16", 121.0)   # 次日（nxt）→ ret=+10%
        for action in ("buy", "sell", "watch", "hold"):
            _add_decision(conn, "2026-09-15", "600001", action, "proposed",
                          "2026-09-15")
        conn.commit()
        n = daily_mod.backfill_decision_outcomes(conn, as_of="2026-09-17")
        assert n == 4, n
        rows = conn.execute(
            "SELECT action, t1_ret, direction_hit FROM decision ORDER BY id").fetchall()
        by_action = {r[0]: (r[1], r[2]) for r in rows}
        for action in ("buy", "sell", "watch", "hold"):
            ret, hit = by_action[action]
            assert ret is not None and abs(ret - 0.1) < 1e-6, (action, ret)
        assert by_action["buy"][1] == 1
        assert by_action["sell"][1] == 0
        assert by_action["watch"][1] is None, "watch 不打方向分"
        assert by_action["hold"][1] is None, "hold 不打方向分"
    finally:
        conn.close()


@test
def test_wc4_weekly_quality_groups_and_rejected_excluded():
    """W-C4：周报决策质量节——executed/未执行分组；被风控 rejected 的决策不进
    胜率分母；watch/hold 只看 t1_ret 事后分布。"""
    conn = _mem()
    try:
        _add_decision(conn, "2026-09-15", "600001", "buy", "executed",
                      "2026-09-15", t1_ret=0.02, hit=1, conf=0.8)
        _add_decision(conn, "2026-09-15", "600002", "buy", "rejected",
                      "2026-09-15", t1_ret=0.05, hit=1, conf=0.9)
        _add_decision(conn, "2026-09-15", "600003", "sell", "proposed",
                      "2026-09-15", t1_ret=0.03, hit=0, conf=0.7)
        _add_decision(conn, "2026-09-15", "600004", "watch", "proposed",
                      "2026-09-15", t1_ret=0.02, hit=None)
        _add_decision(conn, "2026-09-15", "600005", "hold", "proposed",
                      "2026-09-15", t1_ret=-0.01, hit=None)
        conn.commit()
        body = weekly_mod._sec_decision_quality(conn, "2026-09-14", "2026-09-18")
        assert "已执行 buy/sell 方向命中：1/1（胜率 100%）" in body, body
        assert "风控拒绝决策 1 条" in body and "不进胜率分母" in body
        assert "2/2" not in body, "rejected 不得混入胜率分母"
        assert "未执行 buy/sell" in body and "0/1" in body
        assert "watch/hold" in body and "2 条" in body
        assert "+0.50%" in body  # avg(2%, -1%) = 0.5%
    finally:
        conn.close()


# ============================================================
# W-C5：周报基准窗口对齐
# ============================================================

def _seed_weekly(conn, index_end: str):
    for d, total in [("2026-09-11", 1000000.0), ("2026-09-14", 1001000.0),
                     ("2026-09-15", 1002000.0), ("2026-09-16", 1003000.0),
                     ("2026-09-17", 1004000.0), ("2026-09-18", 1005000.0)]:
        conn.execute("INSERT OR REPLACE INTO portfolio_state VALUES (?,?,?,?,?,?,?)",
                     (d, total, 0.0, total, 0.0, 0, "t"))
    for d, close in [("2026-09-11", 4000.0), ("2026-09-17", 4020.0),
                     ("2026-09-18", 4040.0)]:
        if d <= index_end:
            conn.execute("INSERT OR REPLACE INTO index_daily VALUES ('000300',?,?,NULL,NULL)",
                         (d, close))
    conn.commit()


def _offline_fetcher(start8, end8):
    raise ConnectionError("测试离线环境，不应发起真实抓取")


@test
def test_wc5_benchmark_aligned_excess_shown():
    conn = _mem()
    out = Path(tempfile.mkdtemp(prefix="agsickle_c_wc5a_"))
    try:
        _seed_weekly(conn, index_end="2026-09-18")
        p = weekly_mod.weekly_report("2026-09-18", conn=conn, out_dir=out,
                                     fetcher=_offline_fetcher)
        text = p.read_text(encoding="utf-8")
        assert "超额收益（组合-基准）" in text
        assert "超额收益：n/a" not in text
        assert "基准窗口止于" not in text
    finally:
        conn.close()
        shutil.rmtree(out, ignore_errors=True)


@test
def test_wc5_benchmark_mismatch_annotated_and_na():
    """W-C5（P1-28）：基准止于 09-17、组合端期末 09-18 → 显式标注滞后 1 日，
    超额收益标 n/a（此前静默跨日相减出假超额 +0.33%）。"""
    conn = _mem()
    out = Path(tempfile.mkdtemp(prefix="agsickle_c_wc5b_"))
    try:
        _seed_weekly(conn, index_end="2026-09-17")
        p = weekly_mod.weekly_report("2026-09-18", conn=conn, out_dir=out,
                                     fetcher=_offline_fetcher)
        text = p.read_text(encoding="utf-8")
        assert "基准窗口止于 2026-09-17（数据滞后 1 日；组合端期末 2026-09-18）" in text
        assert "超额收益：n/a" in text
        assert "基准窗口错配" in text            # 附注 + 归因降级说明
        # 周收益（组合端）仍在；基准跨日假收益不再以"超额"形式出现
        assert "周收益率" in text
    finally:
        conn.close()
        shutil.rmtree(out, ignore_errors=True)


# ============================================================
# W-C6：decide 合法空决策 exit 0
# ============================================================

@test
def test_wc6_legal_empty_decision_exit_zero():
    """W-C6（P1-23）：LLM 输出 `[]` 且校验零失败 → 打印"当日合法空决策"且
    main exit 0（此前 exit 1 + "校验未全部通过" 误报触发自动化补跑）。"""
    f = Path(tempfile.mkdtemp(prefix="agsickle_c_wc6_")) / "empty.json"
    f.write_text("[]", encoding="utf-8")
    info: dict = {}
    ids = decide.load_and_save(str(f), "2026-09-19", result=info)
    assert ids == [] and info.get("legal_empty") is True
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = decide.main(["--file", str(f), "--date", "2026-09-19"])
    assert rc == 0, rc
    assert "当日合法空决策" in buf.getvalue()


@test
def test_wc6_real_failure_still_exit_one():
    """W-C6 负向：真校验失败仍 exit 1（_dump_raw 已 stub，不落任何盘）。"""
    f = Path(tempfile.mkdtemp(prefix="agsickle_c_wc6b_")) / "bad.json"
    f.write_text('[{"action":"nope","code":"1"}]', encoding="utf-8")
    orig_dump = decide._dump_raw
    decide._dump_raw = lambda *a, **k: None
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = decide.main(["--file", str(f), "--date", "2026-09-19"])
        assert rc == 1, rc
        assert "校验未全部通过" in buf.getvalue()
        assert "当日合法空决策" not in buf.getvalue()
    finally:
        decide._dump_raw = orig_dump


# ============================================================
# W-C7：日报补跑落盘 + catchup 产物校验
# ============================================================

@test
def test_wc7_daily_report_backfill_writes_and_protects_existing():
    """W-C7（P1-21）：守卫改"目标文件已存在才拒写"——历史日产物缺失时真正落盘
    （catchup 补跑场景）；产物已在时拒写保护审计链；当日允许覆盖重写。"""
    conn = _mem()
    out = Path(tempfile.mkdtemp(prefix="agsickle_c_wc7_"))
    try:
        p = daily_mod.generate_daily_report("2026-09-10", conn=conn, out_dir=out)
        assert p.is_file() and p.name == "2026-09-10.md", "历史缺失日报必须真正落盘"
        assert "PENDING" not in p.name
        # 已存在 → 拒写（内容原样保护）
        p.write_text("SENTINEL", encoding="utf-8")
        p2 = daily_mod.generate_daily_report("2026-09-10", conn=conn, out_dir=out)
        assert p2 == p and p2.read_text(encoding="utf-8") == "SENTINEL"
        # 当日 → 允许覆盖重写（幂等双跑语义不变）
        today = date.today().isoformat()
        pt = out / (today + ".md")
        pt.write_text("SENTINEL", encoding="utf-8")
        daily_mod.generate_daily_report(today, conn=conn, out_dir=out)
        assert pt.read_text(encoding="utf-8") != "SENTINEL"
    finally:
        conn.close()
        shutil.rmtree(out, ignore_errors=True)


class _CatchupSandbox:
    """catchup 进程内注入：目录常量 + 子进程脚本 + 动态池/周报 stub（同 test_pipeline 模式）。"""

    def __init__(self, db_path: str, reports: Path, session: Path, state: Path):
        self.db_path = db_path
        self.reports = reports
        self.session = session
        self.state = state
        self.calls = []
        self._orig = []

    def __enter__(self):
        self._orig = [
            (catchup, "REPORTS_DIR", catchup.REPORTS_DIR),
            (catchup, "SESSION_DIR", catchup.SESSION_DIR),
            (catchup, "STATE_DIR", catchup.STATE_DIR),
            (catchup, "HEARTBEAT_FILE", catchup.HEARTBEAT_FILE),
            (catchup, "_run_script", catchup._run_script),
            (catchup, "subprocess", catchup.subprocess),
            (weekly_mod, "weekly_report", weekly_mod.weekly_report),
            (movers_mod, "refresh", movers_mod.refresh),
            (hot_mod, "refresh", hot_mod.refresh),
        ]
        catchup.REPORTS_DIR = self.reports
        catchup.SESSION_DIR = self.session
        catchup.STATE_DIR = self.state
        catchup.HEARTBEAT_FILE = self.state / "catchup_heartbeat"

        stub = self

        def fake_run_script(rel, timeout=900, extra=None):
            stub.calls.append(rel)
            return True

        class _FakeProc:
            returncode = 0
            stdout = ""
            stderr = ""

        class _FakeSubprocess:
            TimeoutExpired = subprocess.TimeoutExpired

            @staticmethod
            def run(*a, **k):
                stub.calls.append("subprocess:%s" % (a[0] if a else "?"))
                return _FakeProc()

        catchup._run_script = fake_run_script
        catchup.subprocess = _FakeSubprocess()
        weekly_mod.weekly_report = lambda trade_date=None, *a, **k: \
            self.reports / "fake-weekly.md"
        movers_mod.refresh = lambda conn, as_of=None, market_mode=True: {
            "mode": "watchlist", "count": 0, "rows": []}
        hot_mod.refresh = lambda conn, as_of=None: {
            "themes": [], "stocks": [], "boards": [], "count": 0}
        return self

    def __exit__(self, *exc):
        for mod, attr, val in reversed(self._orig):
            setattr(mod, attr, val)
        return False


def _seed_catchup_db(db_path: str, bar_dates: list) -> str:
    conn = sqlite3.connect(db_path)
    conn.executescript(DDL)
    today = date.today()
    for code in ("600519", "000001"):
        conn.execute("INSERT INTO stock_info VALUES (?,?,?,?)",
                     (code, "票" + code, "2024-01-02",
                      datetime.now().isoformat(timespec="seconds")))
        for td in bar_dates:
            _add_bar(conn, code, td, 100.0)
    base = today - timedelta(days=200)
    conn.executemany("INSERT OR IGNORE INTO trade_calendar VALUES (?)",
                     [((base + timedelta(days=i)).isoformat(),) for i in range(400)])
    conn.commit()
    conn.close()
    return today.isoformat()


@test
def test_wc7_catchup_backfill_writes_report_and_verifies_product():
    """W-C7：catchup 步骤1 补跑缺失日 → td.md 真正落盘（旧守卫下永远写不出）；
    产物校验：日报写不出时计入 failures（rc=1），不再静默"成功"。"""
    d = Path(tempfile.mkdtemp(prefix="agsickle_c_wc7c_"))
    db_path = str(d / "market.db")
    reports, session, state = d / "reports", d / "session", d / "state"
    for p in (reports, session, state):
        p.mkdir(parents=True, exist_ok=True)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    today_str = _seed_catchup_db(db_path, [yesterday])
    old_db = os.environ.get("AGSICKLE_DB")
    old_reports_env = os.environ.get("AGSICKLE_REPORTS_DIR")
    os.environ["AGSICKLE_DB"] = db_path
    os.environ["AGSICKLE_REPORTS_DIR"] = str(reports)  # generate_daily_report 读 env
    try:
        # 正常链：补跑落盘 + 产物校验通过 → rc=0
        with _CatchupSandbox(db_path, reports, session, state):
            rc = catchup.catch_up(now=datetime.combine(date.today(), time(15, 20)))
        assert rc == 0, "补跑应成功, rc=%d" % rc
        assert (reports / (yesterday + ".md")).is_file(), "补跑日报必须真正落盘"

        # 失败注入：generate_daily_report 写不出（旧守卫行为）→ 产物校验计 failures
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM portfolio_state WHERE date=?", (yesterday,))
        conn.commit()
        conn.close()
        (reports / (yesterday + ".md")).unlink()
        orig_report = daily_mod.generate_daily_report

        def swallow(trade_date=None, conn=None, out_dir=None):
            return Path("/nonexistent")  # 静默不落盘（模拟旧 PENDING 吞写）

        daily_mod.generate_daily_report = swallow
        try:
            with _CatchupSandbox(db_path, reports, session, state):
                rc2 = catchup.catch_up(now=datetime.combine(date.today(), time(15, 21)))
            assert rc2 == 1, "日报产物缺失必须计入 failures（rc=1），got %d" % rc2
            assert not (reports / (yesterday + ".md")).exists()
        finally:
            daily_mod.generate_daily_report = orig_report
    finally:
        if old_db is None:
            os.environ.pop("AGSICKLE_DB", None)
        else:
            os.environ["AGSICKLE_DB"] = old_db
        if old_reports_env is None:
            os.environ.pop("AGSICKLE_REPORTS_DIR", None)
        else:
            os.environ["AGSICKLE_REPORTS_DIR"] = old_reports_env
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# W-C8：catchup 2a 11:00 门 + bundle.HHMM.md 版本化
# ============================================================

@test
def test_wc8_premarket_gate_before_and_after_1100():
    """W-C8（P1-22）：signal 未对齐时——11:00 前允许重跑 premarket（早晨修复
    信号缺失）；11:00 后且当日 bundle 在 → 不再整套重跑（防傍晚覆盖早晨证据）；
    bundle 缺失任何时候都补。"""
    d = Path(tempfile.mkdtemp(prefix="agsickle_c_wc8_"))
    db_path = str(d / "market.db")
    reports, session, state = d / "reports", d / "session", d / "state"
    for p in (reports, session, state):
        p.mkdir(parents=True, exist_ok=True)
    today_str = _seed_catchup_db(db_path, [date.today().isoformat()])
    # signal 停在昨日 → 恒"未对齐"（09-17/18 傍晚覆盖场景的根源条件）
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO signal (code, as_of, signals, score, profile) VALUES"
                 " ('600519', ?, '{}', 0.5, 'reversal_lowvol')",
                 ((date.today() - timedelta(days=1)).isoformat(),))
    conn.commit()
    conn.close()
    old_db = os.environ.get("AGSICKLE_DB")
    os.environ["AGSICKLE_DB"] = db_path
    try:
        # ① 11:00 前 + bundle 在 + 信号未对齐 → 重跑（允许）
        (session / today_str).mkdir(parents=True, exist_ok=True)
        (session / today_str / "bundle.md").write_text("# morning", encoding="utf-8")
        with _CatchupSandbox(db_path, reports, session, state) as sb:
            rc = catchup.catch_up(now=datetime.combine(date.today(), time(10, 40)))
        assert rc == 0
        assert "pipeline/premarket.py" in sb.calls, "11:00 前信号未对齐应重跑"

        # ② 11:00 后 + bundle 在 + 信号未对齐 → 不重跑（保护早晨 bundle）
        with _CatchupSandbox(db_path, reports, session, state) as sb:
            rc = catchup.catch_up(now=datetime.combine(date.today(), time(14, 0)))
        assert rc == 0
        assert "pipeline/premarket.py" not in sb.calls, \
            "11:00 后不得覆盖早晨 bundle: %s" % sb.calls

        # ③ 11:00 后 + bundle 缺失 → 仍补跑
        (session / today_str / "bundle.md").unlink()
        with _CatchupSandbox(db_path, reports, session, state) as sb:
            rc = catchup.catch_up(now=datetime.combine(date.today(), time(14, 5)))
        assert rc == 0
        assert "pipeline/premarket.py" in sb.calls, "bundle 缺失任何时候都应补跑"
    finally:
        if old_db is None:
            os.environ.pop("AGSICKLE_DB", None)
        else:
            os.environ["AGSICKLE_DB"] = old_db
        shutil.rmtree(d, ignore_errors=True)


@test
def test_wc8_bundle_stamp_versions_retention():
    """W-C8：write_bundle 同时落 bundle.HHMM.md 带时间戳副本；目录只保留最近
    10 份（防审计链无限增长）。时间冻结到 12:00 保证与历史副本的排序确定。"""
    session_root = Path(tempfile.mkdtemp(prefix="agsickle_c_wc8b_"))
    old_build = ai_bundle.build_bundle
    old_dt = ai_bundle.datetime

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 19, 12, 0)

    try:
        b = {"run_date": "2026-09-19", "evidence_date": "2026-09-18",
             "generated_at": "2026-09-19T09:00:00", "prompt_version": "t"}
        ai_bundle.build_bundle = lambda run_date=None, conn=None: b
        ai_bundle.datetime = _FrozenDateTime
        day_dir = session_root / "2026-09-19"
        day_dir.mkdir(parents=True, exist_ok=True)
        for hh in range(800, 800 + 12):     # 12 份历史副本（0800..0811，均早于冻结时刻）
            (day_dir / ("bundle.%04d.md" % hh)).write_text("old", encoding="utf-8")
        os.environ["AGSICKLE_SESSION_DIR"] = str(session_root)
        _, md_path = ai_bundle.write_bundle("2026-09-19")
        stamped = sorted(day_dir.glob("bundle.????.md"))
        assert len(stamped) == ai_bundle.BUNDLE_STAMP_KEEP, stamped
        assert all(re.fullmatch(r"bundle\.\d{4}\.md", p.name) for p in stamped)
        assert not (day_dir / "bundle.0800.md").exists(), "最旧的副本应被清理"
        assert not (day_dir / "bundle.0801.md").exists()
        assert not (day_dir / "bundle.0802.md").exists()
        assert stamped[-1].name == "bundle.1200.md", stamped[-1].name
        assert md_path.read_text(encoding="utf-8") == \
            stamped[-1].read_text(encoding="utf-8"), \
            "时间戳副本内容必须与 bundle.md 一致"
    finally:
        ai_bundle.build_bundle = old_build
        ai_bundle.datetime = old_dt
        shutil.rmtree(session_root, ignore_errors=True)


# ---------------------------------------------------------------- 直接运行入口

def _main() -> int:
    failed = []
    for fn in _TESTS:
        try:
            fn()
            print("PASS %s" % fn.__name__)
        except Exception as e:  # noqa: BLE001
            failed.append(fn.__name__)
            print("FAIL %s: %s: %s" % (fn.__name__, type(e).__name__, e))
            traceback.print_exc()
    print("\n%d/%d passed" % (len(_TESTS) - len(failed), len(_TESTS))
          + ("" if not failed else "; failed: %s" % ", ".join(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
