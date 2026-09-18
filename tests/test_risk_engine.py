"""风控引擎测试：§5.4 十组违规拦截 + 合法通过 + kill switch + report_only + 留痕落库。"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

# 规则20 经 signals.read_factor_crowding() 读 logs/signal_eval/factor_crowding.json；
# 不隔离时会读到**生产**拥挤状态——2026-09-17 盘后生产激活 crowded=true 后，
# 本文件 12 个买入用例被真实状态压到 5% 上限而批量失败。指向空沙箱目录，
# 缺文件 → crowded=False（与 test_signals 的 K3 沙箱同模式）。
os.environ.setdefault("AGSICKLE_SIGNAL_EVAL_DIR",
                      tempfile.mkdtemp(prefix="agsickle_re_se_"))

from risk.engine import (RiskContext, Verdict, check, record_event, apply_kill_switch,
                         flush_events, limit_pct, in_trading_session)

# 与 config.json risk 段一致的纯内存配置（不读真实文件/数据库）
CFG = {
    "max_single_weight": 0.20,
    "max_total_weight": 0.80,
    "max_positions": 5,
    "price_guard_pct": 0.02,
    "max_daily_trades": 3,
    "max_weekly_turnover": 2.0,
    "max_drawdown_kill": 0.08,
    "kill_stop_hours": 72,
    "min_confidence": 0.60,
    "lot_size": 100,
}

WED = datetime(2026, 9, 9, 10, 0, 0)      # 周三盘中
SAT = datetime(2026, 9, 12, 10, 0, 0)     # 周六


def mk_ctx(**over) -> RiskContext:
    # 规则20 全局读 factor_crowding.json：同进程更早执行的 pipeline 类测试会经
    # compute_all 向共享沙箱目录落盘 crowded=true 的合成状态，污染本文件与拥挤度
    # 无关的规则用例。逐用例重置为非拥挤。env 守卫：绝不写生产 logs/signal_eval/。
    if os.environ.get("AGSICKLE_SIGNAL_EVAL_DIR"):
        import json as _json
        from signals.signals import _factor_crowding_path
        p = _factor_crowding_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps({"crowded": False, "reason": "test_risk_engine 重置"}),
                     encoding="utf-8")
    base = dict(
        now=WED,
        positions={},
        cash=1000000.0,
        total_equity=1000000.0,
        latest_prices={"600519": 1500.0, "000001": 11.0, "300750": 12.0},
        prev_close={"600519": 1490.0, "000001": 10.0, "300750": 10.0},
        today_trades=0,
        week_turnover=0.0,
        peak_equity=1000000.0,
        kill_switch_until=None,
        blacklist={},
        health_issues=[],
    )
    base.update(over)
    return RiskContext(**base)


def mk_dec(action: str, code: str, price: float, shares: int, **over) -> dict:
    d = {
        "action": action, "code": code, "target_weight": 0.10,
        "confidence": 0.8, "reasons": ["r1", "r2"], "risk_notes": [],
        "order": {"side": action, "price": price, "shares": shares},
    }
    d.update(over)
    return d


def hit(v: Verdict, keyword: str) -> bool:
    return any(keyword in s for s in v.violations)


# ---------------- §5.4 十组违规（全部 approved=False 且命中规则关键词） ----------------


def test_violation_single_weight():
    """超单票仓位：30% > 20%。"""
    v = check(mk_dec("buy", "600519", 1500.0, 200), mk_ctx(), CFG)
    assert not v.approved
    assert hit(v, "单票权重")
    assert len(v.violations) == 1


def test_violation_price_guard():
    """超价格偏离：委托价偏离实时价 6.67% > 2%。"""
    v = check(mk_dec("buy", "600519", 1600.0, 100), mk_ctx(), CFG)
    assert not v.approved
    assert hit(v, "价格保护")
    assert len(v.violations) == 1


def test_violation_blacklist():
    """黑名单 ok=False 拒绝。"""
    ctx = mk_ctx(blacklist={"600519": (False, "ST标的")})
    v = check(mk_dec("buy", "600519", 1500.0, 100), ctx, CFG)
    assert not v.approved
    assert hit(v, "黑名单")
    assert len(v.violations) == 1


def test_violation_non_trading_session():
    """非交易时段（周六）拒绝买卖。"""
    v = check(mk_dec("buy", "600519", 1500.0, 100), mk_ctx(now=SAT), CFG)
    assert not v.approved
    assert hit(v, "非交易时段")
    assert len(v.violations) == 1


def test_violation_daily_trades():
    """超当日次数：today_trades 已达上限 3。"""
    v = check(mk_dec("buy", "600519", 1500.0, 100), mk_ctx(today_trades=3), CFG)
    assert not v.approved
    assert hit(v, "当日交易次数")
    assert len(v.violations) == 1


def test_violation_weekly_turnover():
    """超周换手：195% + 15% > 200%。"""
    v = check(mk_dec("buy", "600519", 1500.0, 100), mk_ctx(week_turnover=1.95), CFG)
    assert not v.approved
    assert hit(v, "周换手")
    assert len(v.violations) == 1


def test_violation_t_plus_1():
    """T+1 超卖：卖 1000 > 可卖 400。"""
    ctx = mk_ctx(positions={"600519": {"name": "贵州茅台", "shares": 1000,
                                       "avail_shares": 400, "cost": 1400.0}})
    v = check(mk_dec("sell", "600519", 1500.0, 1000), ctx, CFG)
    assert not v.approved
    assert hit(v, "T+1")
    assert len(v.violations) == 1


def test_violation_limit_up_buy():
    """涨停价买入：主板昨收10.00，涨停11.00，买价11.00 ≥ 涨停价 → 拒买。"""
    v = check(mk_dec("buy", "000001", 11.0, 1000), mk_ctx(), CFG)
    assert not v.approved
    assert hit(v, "涨停")
    assert len(v.violations) == 1


def test_violation_limit_down_sell():
    """跌停价卖出（浮亏不破止损线）：昨收10.00，跌停9.00，卖价9.00 → 拒卖。

    Sprint 1 任务 4 起：浮亏破止损线 + 跌停价 → 规则 21 触发并豁免规则 14。
    本 case 用成本 9.5（浮亏仅 5.3% < 8% 止损基础线）让规则 21 不触发，
    保留"规则 14 拒卖"的原始语义。
    """
    ctx = mk_ctx(latest_prices={"600519": 1500.0, "000001": 9.0, "300750": 12.0},
                 positions={"000001": {"name": "平安银行", "shares": 1000,
                                       "avail_shares": 1000, "cost": 9.5}})
    v = check(mk_dec("sell", "000001", 9.0, 100), ctx, CFG)
    assert not v.approved
    assert hit(v, "跌停")
    assert len(v.violations) == 1
    assert v.emergency_pending is None  # 浮亏未破线 → 规则 21 不应触发


def test_violation_low_confidence_report_only():
    """低置信度：0.50 < 0.60 → report_only 且不 approve。"""
    v = check(mk_dec("buy", "600519", 1500.0, 100, confidence=0.5), mk_ctx(), CFG)
    assert not v.approved
    assert v.report_only
    assert hit(v, "置信度")
    assert len(v.violations) == 1


# ---------------- 额外违规（规则15/7/8/13 与结构） ----------------


def test_violation_target_weight_out_of_range():
    """目标权重越界：0.30 > 0.20。"""
    v = check(mk_dec("buy", "600519", 1500.0, 100, target_weight=0.30), mk_ctx(), CFG)
    assert not v.approved
    assert hit(v, "目标权重")


def test_violation_total_weight():
    """超总仓位：69.9% + 16.4% ≈ 86% > 80%。"""
    ctx = mk_ctx(positions={"600519": {"name": "贵州茅台", "shares": 466,
                                       "avail_shares": 466, "cost": 1400.0}})
    # 466×1500=69.9万 + 15000×10.9=16.35万 → 86.25% > 80%；单票 16.35% < 20% 不触发规则6
    v = check(mk_dec("buy", "000001", 10.9, 15000), ctx, CFG)
    assert not v.approved
    assert hit(v, "总仓位")
    assert len(v.violations) == 1


def test_violation_max_positions():
    """持仓数：已有5只再买入第6只（新代码 000002）。"""
    ctx = mk_ctx(positions={c: {"name": c, "shares": 100, "avail_shares": 100, "cost": 10.0}
                            for c in ("600519", "000001", "300750", "601318", "688981")})
    ctx.latest_prices["000002"] = 20.0
    ctx.prev_close["000002"] = 19.8
    v = check(mk_dec("buy", "000002", 20.0, 100), ctx, CFG)
    assert not v.approved
    assert hit(v, "持仓数")
    assert len(v.violations) == 1


def test_violation_lot_rounds_to_zero():
    """手数：买入 50 股规整后为 0，拒绝。"""
    v = check(mk_dec("buy", "600519", 1500.0, 50), mk_ctx(), CFG)
    assert not v.approved
    assert hit(v, "手数")
    assert v.adjusted_order is None


def test_violation_bad_order_struct():
    """order 结构非法：side 与 action 不一致。"""
    d = mk_dec("buy", "600519", 1500.0, 100)
    d["order"]["side"] = "sell"
    v = check(d, mk_ctx(), CFG)
    assert not v.approved
    assert hit(v, "下单结构非法")


# ---------------- 合法通过用例（≥4 组） ----------------


def test_legal_buy_passes_clean():
    """合法买入：无违规、无调整、直接通过。"""
    v = check(mk_dec("buy", "600519", 1500.0, 100), mk_ctx(), CFG)
    assert v.approved
    assert v.violations == []
    assert v.report_only is False
    assert v.adjusted_order is None


def test_legal_lot_adjust():
    """手数规整：买 150 股 → adjusted_order.shares = 100，仍通过。"""
    v = check(mk_dec("buy", "600519", 1500.0, 150), mk_ctx(), CFG)
    assert v.approved
    assert v.adjusted_order is not None
    assert v.adjusted_order["shares"] == 100
    assert any("手数规整" in w for w in v.warnings)


def test_legal_sell_partial():
    """合法卖出：卖 400 ≤ 可卖 1000，非整手约束不适用。"""
    ctx = mk_ctx(positions={"600519": {"name": "贵州茅台", "shares": 1000,
                                       "avail_shares": 1000, "cost": 1400.0}})
    v = check(mk_dec("sell", "600519", 1500.0, 400), ctx, CFG)
    assert v.approved
    assert v.adjusted_order is None
    assert v.violations == []


def test_legal_hold_and_watch_pass():
    """hold/watch 永远放行（含周末、kill 停机期）。"""
    for act in ("hold", "watch"):
        d = mk_dec(act, "600519", 0, 0, order={"side": act, "price": 0, "shares": 0})
        for ctx in (mk_ctx(now=SAT),
                    mk_ctx(kill_switch_until=WED + timedelta(hours=2))):
            v = check(d, ctx, CFG)
            assert v.approved, (act, ctx.now, v.violations)


def test_legal_board_20pct_limit_boundary():
    """创业板 20% 涨跌停边界：昨收 10.00 → 涨停 12.00；11.99 过、12.00 拒。"""
    assert limit_pct("300750") == 0.20
    assert limit_pct("688801") == 0.20
    assert limit_pct("689009") == 0.20   # 科创CDR：68 前缀覆盖
    assert limit_pct("600519") == 0.10
    assert limit_pct("430047") == 0.30   # 北交所（2026-09-15 审查修复补齐）
    assert limit_pct("830799") == 0.30
    assert limit_pct("920002") == 0.30
    # 11.99（涨停价下方 1 分）且偏离实时价 12.00 仅 0.083% → 通过
    v = check(mk_dec("buy", "300750", 11.99, 100), mk_ctx(), CFG)
    assert v.approved, v.violations
    # 12.00 = 涨停价 → 拒绝
    v = check(mk_dec("buy", "300750", 12.0, 100), mk_ctx(), CFG)
    assert not v.approved
    assert hit(v, "涨停")


def test_trading_session_boundaries():
    """交易时段边界：09:30/11:30/13:00/15:00 含边界，午休与收盘后不在。"""
    for hm, expect in [((9, 30), True), ((9, 29), False), ((11, 30), True),
                       ((11, 31), False), ((13, 0), True), ((15, 0), True),
                       ((15, 1), False), ((12, 0), False)]:
        assert in_trading_session(WED.replace(hour=hm[0], minute=hm[1])) is expect, hm
    assert in_trading_session(SAT) is False


# ---------------- kill switch：触发 / 停机拒绝 / 到期恢复 ----------------


def test_kill_switch_trigger_generates_liquidation():
    """回撤 8.5% ≥ 8%：生成清仓指令（按 avail_shares）+ kill_until=now+72h。"""
    ctx = mk_ctx(
        positions={
            "600519": {"name": "贵州茅台", "shares": 1000, "avail_shares": 1000, "cost": 1400.0},
            "000001": {"name": "平安银行", "shares": 2000, "avail_shares": 0, "cost": 12.0},
        },
        total_equity=915000.0,
    )
    v = check(mk_dec("buy", "300750", 12.0, 100), ctx, CFG)
    assert v.kill_trigger
    assert not v.approved
    assert hit(v, "kill switch")
    assert v.kill_until == WED + timedelta(hours=CFG["kill_stop_hours"])
    assert len(v.kill_orders) == 1  # 000001 avail=0 跳过
    ko = v.kill_orders[0]
    assert ko["code"] == "600519" and ko["side"] == "sell"
    assert ko["shares"] == 1000


def test_kill_switch_active_rejects_trades():
    """停机期内买卖拒绝（不再重复触发）；hold 放行。"""
    ctx = mk_ctx(kill_switch_until=WED + timedelta(hours=2))
    v = check(mk_dec("buy", "600519", 1500.0, 100), ctx, CFG)
    assert not v.approved
    assert hit(v, "kill switch")
    assert not v.kill_trigger and v.kill_orders == []
    d = mk_dec("hold", "600519", 0, 0, order={"side": "hold", "price": 0, "shares": 0})
    assert check(d, ctx, CFG).approved


def test_kill_switch_expiry_recovers():
    """到期恢复：停机期限已过且回撤仅 1% → 重新放行、不再触发。"""
    ctx = mk_ctx(kill_switch_until=WED - timedelta(hours=1), total_equity=990000.0)
    v = check(mk_dec("buy", "600519", 1500.0, 100), ctx, CFG)
    assert v.approved
    assert not v.kill_trigger
    assert v.kill_until is None
    assert v.violations == []


# ---------------- report_only 两条路径 ----------------


def test_report_only_health_issues():
    """数据健康检查异常 → report_only=True（不 approve），对 hold 同样生效。"""
    ctx = mk_ctx(health_issues=["数据滞后 4 天（最新 2026-09-05）"])
    v = check(mk_dec("buy", "600519", 1500.0, 100), ctx, CFG)
    assert v.report_only and not v.approved
    assert hit(v, "数据健康")
    d = mk_dec("hold", "600519", 0, 0, order={"side": "hold", "price": 0, "shares": 0})
    v2 = check(d, ctx, CFG)
    assert v2.report_only and not v2.approved


# 低置信度路径见 test_violation_low_confidence_report_only


# ---------------- 留痕：record_event / apply_kill_switch（内存库，不碰真实 market.db） ----------------


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE risk_event (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " ts TEXT, rule TEXT, detail TEXT, decision_id INT)")
    conn.execute("CREATE TABLE portfolio_state (date TEXT PRIMARY KEY, cash REAL,"
                 " market_value REAL, total REAL, drawdown REAL, kill_switch INT, note TEXT)")
    return conn


def test_record_event_writes_row():
    conn = _mem_conn()
    record_event(conn, "测试规则", "单票权重 25.0% > 上限 20.0%", decision_id=7)
    ts, rule, detail, did = conn.execute(
        "SELECT ts, rule, detail, decision_id FROM risk_event").fetchone()
    assert rule == "测试规则" and detail.startswith("单票权重") and did == 7
    assert "T" in ts  # iso 时间戳
    record_event(conn, "无决策规则", "x")  # decision_id 缺省 None
    assert conn.execute("SELECT decision_id FROM risk_event WHERE rule='无决策规则'"
                        ).fetchone()[0] is None


def test_apply_kill_switch_updates_latest_row():
    conn = _mem_conn()
    conn.execute("INSERT INTO portfolio_state VALUES"
                 " ('2026-09-11', 100000.0, 900000.0, 1000000.0, 0.0, 0, '')")
    conn.execute("INSERT INTO portfolio_state VALUES"
                 " ('2026-09-10', 100000.0, 850000.0, 950000.0, 0.0, 0, '')")
    apply_kill_switch(conn, datetime(2026, 9, 15, 10, 0), note="演练")
    date_, ks, note = conn.execute(
        "SELECT date, kill_switch, note FROM portfolio_state"
        " WHERE kill_switch=1").fetchone()
    assert date_ == "2026-09-11" and ks == 1       # 更新的是最新日期行
    assert "kill switch" in note and "2026-09-15 10:00" in note and "演练" in note
    n = conn.execute("SELECT COUNT(*) FROM risk_event WHERE rule='kill_switch'"
                     ).fetchone()[0]
    assert n == 1


def test_apply_kill_switch_empty_table_inserts_today():
    conn = _mem_conn()
    apply_kill_switch(conn, datetime(2026, 9, 15, 10, 0), note="")
    today = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute("SELECT date, kill_switch, note FROM portfolio_state").fetchone()
    assert row[0] == today and row[1] == 1 and "kill switch" in row[2]


# ---------------- 规则16-19（2026-09 风控补强） ----------------


def test_rule16_stop_loss_blocks_averaging_down():
    """单票止损：浮亏 20% ≥ 8% 的票禁止加仓。"""
    ctx = mk_ctx(positions={"600519": {"name": "贵州茅台", "shares": 100,
                                       "avail_shares": 100, "cost": 1500.0}},
                 latest_prices={"600519": 1200.0, "000001": 11.0, "300750": 12.0},
                 prev_close={"600519": 1230.0, "000001": 10.0, "300750": 10.0})
    v = check(mk_dec("buy", "600519", 1200.0, 100), ctx, CFG)
    assert not v.approved and hit(v, "单票止损")
    # 卖出不受止损规则限制（止损就是靠卖出执行）
    v2 = check(mk_dec("sell", "600519", 1200.0, 100), ctx, CFG)
    assert v2.approved, v2.violations


def test_rule17_concept_concentration():
    """概念集中度：同概念持仓+本次 > 45% 拒绝。"""
    cfg = dict(CFG, max_concept_weight=0.45)
    ctx = mk_ctx(
        positions={"002230": {"name": "科大讯飞", "shares": 20000, "avail_shares": 20000,
                              "cost": 20.0}},
        latest_prices={"002230": 20.0, "000977": 50.0, "600519": 1500.0},
        code_concepts={"002230": ["AI"], "000977": ["AI"]})
    # 已持仓 40万 + 本次 15万 = 55% > 45%
    v = check(mk_dec("buy", "000977", 50.0, 3000), ctx, cfg)
    assert not v.approved and hit(v, "概念集中度")
    # 无概念标签的票不受限
    v2 = check(mk_dec("buy", "600519", 1500.0, 100), ctx, cfg)
    assert v2.approved, v2.violations


def test_rule18_liquidity_cap():
    """流动性约束：下单金额 > 最新成交额 × 1% 拒绝；无成交额数据跳过。"""
    ctx = mk_ctx(day_amount={"600519": 5000000.0})   # 1% 上限 5 万
    v = check(mk_dec("buy", "600519", 1500.0, 100), ctx, CFG)  # 15 万 > 5 万
    assert not v.approved and hit(v, "流动性约束")
    v2 = check(mk_dec("buy", "600519", 1500.0, 100), mk_ctx(), CFG)  # 无数据 → 跳过
    assert v2.approved and not hit(v2, "流动性")


def test_rule19_round_trip():
    """同票往返：当日已卖出 600519 再买回拒绝。"""
    ctx = mk_ctx(today_sold_codes={"600519"})
    v = check(mk_dec("buy", "600519", 1500.0, 100), ctx, CFG)
    assert not v.approved and hit(v, "同票往返")


def test_kill_liquidation_sell_allowed_during_stop():
    """停机期内普通卖单拒绝，但 kill_liquidation 清算卖单放行（补清仓通道）。"""
    ctx = mk_ctx(kill_switch_until=WED + timedelta(hours=2),
                 positions={"600519": {"name": "贵州茅台", "shares": 1000,
                                       "avail_shares": 1000, "cost": 1400.0}})
    v = check(mk_dec("sell", "600519", 1500.0, 100), ctx, CFG)
    assert not v.approved and hit(v, "kill switch")
    d2 = mk_dec("sell", "600519", 1500.0, 1000, kill_liquidation=True)
    v2 = check(d2, ctx, CFG)
    assert v2.approved, v2.violations


def test_kill_deferred_positions_reported():
    """kill 触发时 T+1 不可卖的票进 kill_pending 递延名单（不再被静默跳过）。"""
    ctx = mk_ctx(
        positions={
            "600519": {"name": "贵州茅台", "shares": 1000, "avail_shares": 1000, "cost": 1400.0},
            "000001": {"name": "平安银行", "shares": 2000, "avail_shares": 0, "cost": 12.0},
        },
        total_equity=915000.0)
    v = check(mk_dec("buy", "300750", 12.0, 100), ctx, CFG)
    assert v.kill_trigger
    assert v.kill_pending == ["000001"]
    assert len(v.kill_orders) == 1


# ---------------- 2026-09-14 市场环境总闸 + ATR 自适应止损 ----------------


def test_dynamic_position_cap_rejects_buy():
    """regime 动态闸（cap=50%）< 静态 80%：买入后总仓位 52.9% 被拒，且提示动态闸。"""
    ctx = mk_ctx(positions={"600519": {"name": "贵州茅台", "shares": 350,
                                       "avail_shares": 350, "cost": 1400.0}},
                 position_cap=0.50)
    v = check(mk_dec("buy", "000001", 10.0, 3000), ctx, CFG)
    assert not v.approved and hit(v, "动态闸")
    # 卖出永远不受总仓位闸限制
    v2 = check(mk_dec("sell", "600519", 1500.0, 100), ctx, CFG)
    assert v2.approved, v2.violations


def test_dynamic_position_cap_none_is_noop():
    """position_cap=None（regime 故障 fail-open）→ 行为与旧版完全一致。"""
    ctx = mk_ctx(positions={"600519": {"name": "贵州茅台", "shares": 466,
                                       "avail_shares": 466, "cost": 1400.0}})
    v = check(mk_dec("buy", "000001", 10.0, 15000), ctx, CFG)
    assert not v.approved and hit(v, "总仓位") and not hit(v, "动态闸")


def test_atr_stop_line_widens_for_high_vol():
    """ATR 自适应止损线：高波票 2ATR=14% > 基础 8%；低波票仍 8%；缺失回退基础线。"""
    from risk.engine import stop_loss_line
    ctx = mk_ctx(atr_pct={"300750": 0.07, "600519": 0.03})
    assert abs(stop_loss_line(ctx, CFG, "300750") - 0.14) < 1e-9
    assert abs(stop_loss_line(ctx, CFG, "600519") - 0.08) < 1e-9
    assert abs(stop_loss_line(mk_ctx(), CFG, "600519") - 0.08) < 1e-9


def test_rule16_atr_aware_no_premature_block():
    """高波票浮亏 10%（< 2ATR=14%）不触发止损拦截；同幅亏损低波票仍拦截。"""
    pos = {"300750": {"name": "宁德时代", "shares": 100, "avail_shares": 100,
                      "cost": 13.33}}
    prices = {"300750": 12.0, "000001": 11.0, "600519": 1500.0}
    prevs = {"300750": 12.1, "000001": 10.0, "600519": 1490.0}
    wide = mk_ctx(positions=pos, latest_prices=prices, prev_close=prevs,
                  atr_pct={"300750": 0.07})
    v = check(mk_dec("buy", "300750", 12.0, 100), wide, CFG)
    assert v.approved, v.violations
    narrow = mk_ctx(positions=pos, latest_prices=prices, prev_close=prevs,
                    atr_pct={"300750": 0.03})
    v2 = check(mk_dec("buy", "300750", 12.0, 100), narrow, CFG)
    assert not v2.approved and hit(v2, "单票止损")


def test_stop_loss_breaches_uses_atr_line():
    """盘中扫描口径：浮亏超自适应线才算破线。"""
    from risk.engine import stop_loss_breaches
    ctx = mk_ctx(positions={"300750": {"name": "宁德时代", "shares": 100,
                                       "avail_shares": 100, "cost": 13.33}},
                 latest_prices={"300750": 12.0, "000001": 11.0, "600519": 1500.0},
                 atr_pct={"300750": 0.07})
    assert stop_loss_breaches(ctx, CFG) == []          # 10% < 14%（2ATR）
    ctx_narrow = mk_ctx(positions=ctx.positions,
                        latest_prices=ctx.latest_prices,
                        atr_pct={"300750": 0.03})
    assert [c for c, _ in stop_loss_breaches(ctx_narrow, CFG)] == ["300750"]


# ---------------- Sprint 1 任务 4：规则 21 跌停封单应急 ----------------

def test_rule21_limit_halt_emergency_trigger_with_seal():
    """条件①②③ 全满足：卖价=跌停价 + 封单比 5% + 浮亏破止损线 → 触发，写 risk_event。"""
    # 600519 主板 ±10%，昨收 1490，跌停价 = 1490 * 0.9 = 1341.00
    # 持仓成本 1500（> 跌停价，浮亏约 10.6% > 8% 止损基础线）
    ctx = mk_ctx(
        positions={"600519": {"name": "贵州茅台", "shares": 100,
                              "avail_shares": 100, "cost": 1500.0}},
        latest_prices={"600519": 1341.0},
        prev_close={"600519": 1490.0},
        # 流通市值 1.64 万亿，ask1_vol 50000 手 → 5000000 股
        # 5000000 / (1.64e12/1341) ≈ 0.41% < 3% 不构成死封
        # 改：ask1_vol=3000000 手 = 3e8 股，3e8/1.22e9 ≈ 24.6% 强封单
        live_quotes={"600519": {"price": 1341.0, "ask1_vol": 3000000,
                                "float_mv": 1.64e12}},
    )
    d = mk_dec("sell", "600519", 1341.0, 100)
    v = check(d, ctx, CFG)
    assert v.emergency_pending is not None, "规则 21 必须触发"
    assert v.emergency_pending["code"] == "600519"
    assert v.emergency_pending["rule"] == "limit_halt_emergency"
    # 规则 14 必须放行（豁免生效），不应有 "跌停保护：委托卖价" violation
    assert not hit(v, "跌停保护：委托卖价"), \
        "emergency_pending_skip 必须豁免规则 14，violations=%s" % v.violations
    # warnings 应有"跌停应急路径激活"
    assert any("跌停应急路径激活" in w for w in v.warnings), v.warnings
    # decision 加了 emergency_pending_skip
    assert d.get("emergency_pending_skip") is True


def test_rule21_seal_missing_data_still_triggers():
    """条件② 缺数据时静默跳过（按"通过"处理），仅条件①+③ 也触发应急。"""
    ctx = mk_ctx(
        positions={"600519": {"name": "贵州茅台", "shares": 100,
                              "avail_shares": 100, "cost": 1500.0}},
        latest_prices={"600519": 1341.0},
        prev_close={"600519": 1490.0},
        # live_quotes 不给 600519 → 条件② 缺失
    )
    d = mk_dec("sell", "600519", 1341.0, 100)
    v = check(d, ctx, CFG)
    assert v.emergency_pending is not None, "条件② 缺数据时仍应触发"
    # warnings 应注明"条件②缺数据"
    assert any("条件②缺数据" in w for w in v.warnings), v.warnings


def test_rule21_seal_ratio_below_threshold_blocks():
    """条件② 封单比 < 3% → 不构成死封 → 不触发。"""
    ctx = mk_ctx(
        positions={"600519": {"name": "贵州茅台", "shares": 100,
                              "avail_shares": 100, "cost": 1500.0}},
        latest_prices={"600519": 1341.0},
        prev_close={"600519": 1490.0},
        # ask1_vol=10000 手 = 1e6 股 / 1.22e9 ≈ 0.08% < 3%
        live_quotes={"600519": {"price": 1341.0, "ask1_vol": 10000,
                                "float_mv": 1.64e12}},
    )
    d = mk_dec("sell", "600519", 1341.0, 100)
    v = check(d, ctx, CFG)
    assert v.emergency_pending is None, "封单比不足 3% 时不应触发"


def test_rule21_regular_sell_below_limit_still_rejected():
    """未带 emergency_pending_skip 标记的普通 sell 单，跌停价仍被规则 14 拒卖。"""
    # 用一个不触发规则 21 的场景：cost=1400，浮亏只有 4.2% < 8% → 条件③ 不满足
    # 但卖价=跌停价 → 规则 14 仍应拒卖
    ctx = mk_ctx(
        positions={"600519": {"name": "贵州茅台", "shares": 100,
                              "avail_shares": 100, "cost": 1400.0}},
        latest_prices={"600519": 1341.0},
        prev_close={"600519": 1490.0},
        # 故意不传 live_quotes，且浮亏不破止损线 → 规则 21 不触发
    )
    d = mk_dec("sell", "600519", 1341.0, 100)
    v = check(d, ctx, CFG)
    # 规则 21 未触发，无豁免
    assert v.emergency_pending is None
    # 规则 14 仍应拒卖
    assert hit(v, "跌停保护：委托卖价")
    assert d.get("emergency_pending_skip") is None or \
           d.get("emergency_pending_skip") is False


# ---------------- Sprint 2 任务 4：skip_gate 标志 ----------------

def test_rule21_sets_skip_gate_flag():
    """规则 21 触发时同步给 decision 加 skip_gate / confirmed_by flag（让 runner 走 B 路径）。"""
    ctx = mk_ctx(
        positions={"600519": {"name": "贵州茅台", "shares": 100,
                              "avail_shares": 100, "cost": 1500.0}},
        latest_prices={"600519": 1341.0},
        prev_close={"600519": 1490.0},
        live_quotes={"600519": {"price": 1341.0, "ask1_vol": 3000000,
                                "float_mv": 1.64e12}},
    )
    d = mk_dec("sell", "600519", 1341.0, 100)
    v = check(d, ctx, CFG)
    assert v.emergency_pending is not None
    # Sprint 2 新增：skip_gate + confirmed_by 必须被规则 21 写入
    assert d.get("skip_gate") is True, "规则 21 必须写 skip_gate=True"
    assert d.get("confirmed_by") == "emergency_rule21"
    # 旧的 emergency_pending_skip 仍保留（向后兼容规则 14 豁免）
    assert d.get("emergency_pending_skip") is True


def test_regular_sell_does_not_set_skip_gate():
    """普通 sell 单（非跌停应急）不应有 skip_gate 标志。"""
    # 成本 9.5，浮亏仅 5.3%，不触发规则 21
    ctx = mk_ctx(
        positions={"000001": {"name": "平安银行", "shares": 1000,
                              "avail_shares": 1000, "cost": 9.5}},
        latest_prices={"000001": 9.0},
        prev_close={"000001": 10.0},
    )
    d = mk_dec("sell", "000001", 9.5, 100)
    v = check(d, ctx, CFG)
    # 普通 sell 单：规则 21 未触发，无 skip_gate
    assert v.emergency_pending is None
    assert not d.get("skip_gate"), "普通 sell 单不应有 skip_gate flag"


# ---------------- P0-5：sell 单豁免"仅业绩预告负面"黑名单拦截 ----------------
# 注意：本节必须位于 __main__ 块之前——run_all.py 以 subprocess 直接执行本文件，
# __main__ 末尾 sys.exit() 会使其后定义的测试永不执行（Fix-5 节即因此仅 pytest 可见）。


def test_is_earnings_only_helper():
    """is_earnings_only：仅业绩预告负面→True；含 ST 等→False；空/'-'→False。"""
    from risk.blacklist import is_earnings_only
    assert is_earnings_only("业绩预告负面（net=-3）") is True
    assert is_earnings_only("业绩预告负面（net=-2）; 业绩预告负面（net=-3）") is True
    assert is_earnings_only("ST标的") is False
    assert is_earnings_only("业绩预告负面（net=-3）; ST标的") is False
    assert is_earnings_only("上市仅30天 < 60天") is False
    assert is_earnings_only("-") is False
    assert is_earnings_only("") is False


def test_rule_blacklist_sell_exempts_earnings_only():
    """P0-5：sell + 仅业绩预告负面 → 豁免放行（无 violation，有豁免 warning）。"""
    from risk.engine import rule_blacklist
    ctx = mk_ctx(blacklist={"600519": (False, "业绩预告负面（net=-3）")})
    v = Verdict()
    rule_blacklist(mk_dec("sell", "600519", 1400.0, 100), ctx, CFG, v)
    assert v.violations == [], f"sell 应豁免业绩预告拦截，实得 {v.violations}"
    assert any("黑名单豁免" in w for w in v.warnings), v.warnings


def test_rule_blacklist_sell_still_blocks_st():
    """P0-5：sell + ST（非业绩预告）→ 仍拦截，豁免不生效。"""
    from risk.engine import rule_blacklist
    ctx = mk_ctx(blacklist={"600519": (False, "ST标的")})
    v = Verdict()
    rule_blacklist(mk_dec("sell", "600519", 1400.0, 100), ctx, CFG, v)
    assert len(v.violations) == 1 and "黑名单" in v.violations[0]


def test_rule_blacklist_sell_mixed_reasons_still_blocks():
    """P0-5：sell + 业绩预告负面 & ST 混合 → 仍拦截（含非豁免理由）。"""
    from risk.engine import rule_blacklist
    ctx = mk_ctx(blacklist={"600519": (False, "业绩预告负面（net=-3）; ST标的")})
    v = Verdict()
    rule_blacklist(mk_dec("sell", "600519", 1400.0, 100), ctx, CFG, v)
    assert len(v.violations) == 1 and "黑名单" in v.violations[0]


def test_rule_blacklist_buy_never_exempts_earnings():
    """P0-5：buy + 仅业绩预告负面 → 仍拦截（豁免只给 sell 止损出口）。"""
    from risk.engine import rule_blacklist
    ctx = mk_ctx(blacklist={"600519": (False, "业绩预告负面（net=-3）")})
    v = Verdict()
    rule_blacklist(mk_dec("buy", "600519", 1500.0, 100), ctx, CFG, v)
    assert len(v.violations) == 1 and "黑名单" in v.violations[0]


# ---------------- Fix-5：blacklist 业绩预告负面硬拦截 ----------------
# （原位于 __main__ 块 sys.exit() 之后，run_all.py 直接执行时永不运行——补丁批
#  Fix D 前移到此处使其真正被执行；pytest 下位置无差）

def test_blacklist_earnings_negative_net_blocks():
    """news_earnings 近 3 日 net ≤ −2 → ok=False；net=0 / 无数据 / 过期 → ok=True。"""
    from datetime import date as _date
    from data.fetcher import DDL
    from risk.blacklist import check_blacklist
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    today = _date.today().isoformat()
    try:
        conn.execute("INSERT INTO stock_info VALUES ('600001','正常票','2020-01-01','x')")
        conn.execute("INSERT INTO stock_info VALUES ('600002','负面票','2020-01-01','x')")
        conn.execute("INSERT INTO stock_info VALUES ('600003','抵消票','2020-01-01','x')")
        # 600002：纯负面 net=-3 → 拦
        conn.execute("INSERT INTO news_earnings VALUES ('600002',?, 'negative', 3, '[]')",
                     (today,))
        # 600003：正负抵消 net=0 → 放行
        conn.execute("INSERT INTO news_earnings VALUES ('600003',?, 'positive', 2, '[]')",
                     (today,))
        conn.execute("INSERT INTO news_earnings VALUES ('600003',?, 'negative', 2, '[]')",
                     (today,))
        conn.commit()
        bl = check_blacklist(conn)
        assert bl["600001"][0] is True
        assert bl["600002"][0] is False and "业绩预告负面" in bl["600002"][1], bl["600002"]
        assert bl["600003"][0] is True
        # 老日期（>3 天前）不参与
        conn.execute("DELETE FROM news_earnings")
        conn.execute("INSERT INTO news_earnings VALUES ('600002', '2020-01-01',"
                     " 'negative', 9, '[]')")
        conn.commit()
        bl2 = check_blacklist(conn)
        assert bl2["600002"][0] is True
    finally:
        conn.close()


# ---------------- C-ARC-4（T2）：warn/failopen 留痕 ----------------

def test_flush_events_warn_prefix_dedupe():
    """warn_prefix：warnings 以 rule='warn'、detail='<code> <原文>' 落库；
    同票同日去重 ≤1 行；不同票各得一行（ADR-0 §2/§3 契约）。"""
    conn = _mem_conn()
    v = Verdict(approved=True, warnings=["手数规整：150 → 100 股（100 的整数倍）"])
    flush_events(conn, v, decision_id=1, warn_prefix="600519")
    rows = conn.execute("SELECT detail FROM risk_event WHERE rule='warn'").fetchall()
    assert len(rows) == 1 and rows[0][0].startswith("600519 "), rows
    # 同票同日第二次 flush：去重不刷行
    flush_events(conn, Verdict(approved=True, warnings=["另一条警告"]), 1,
                 warn_prefix="600519")
    assert conn.execute("SELECT COUNT(*) FROM risk_event WHERE rule='warn'"
                        ).fetchone()[0] == 1
    # 不同票：各得一行（前缀隔离）
    flush_events(conn, Verdict(approved=True, warnings=["缺昨收价，跳过校验"]), 2,
                 warn_prefix="000001")
    codes = {r[0].split(" ", 1)[0] for r in conn.execute(
        "SELECT detail FROM risk_event WHERE rule='warn'").fetchall()}
    assert codes == {"600519", "000001"}


def test_failopen_factor_crowding_event():
    """规则20 拥挤度读取失败 → fail-open 放行 + failopen_factor_crowding 事件挂
    v.events（调用方 flush 落库），detail 以 once_today_prefix 开头（去重可命中）。"""
    import signals.signals as _ss
    orig = _ss.read_factor_crowding
    def _boom():
        raise RuntimeError("json decode fail")
    _ss.read_factor_crowding = _boom
    try:
        v = check(mk_dec("buy", "600519", 1500.0, 100), mk_ctx(), CFG)
    finally:
        _ss.read_factor_crowding = orig
    assert v.approved, v.violations           # fail-open：读取失败不挡正常买入
    ev = [e for e in v.events if e["rule"] == "failopen_factor_crowding"]
    assert len(ev) == 1, v.events
    assert ev[0]["detail"].startswith(ev[0]["once_today_prefix"])
    # 落库端到端：flush 后 risk_event 可查
    conn = _mem_conn()
    flush_events(conn, v, decision_id=9)
    assert conn.execute("SELECT COUNT(*) FROM risk_event WHERE"
                        " rule='failopen_factor_crowding'").fetchone()[0] == 1


# ---------------- Sprint4 批次A：W-A2（规则3/4/18 强平豁免）+ W-A9（规则9 陈旧口径） ----------------

def test_rule4_kill_liquidation_exempt_non_session():
    """W-A2①（P1-2）：09:00 premarket 的 kill 补清算单过规则4（非交易时段豁免，
    与 emergency_scan 同口径）；普通卖单照拒。"""
    pos = {"600519": {"name": "贵州茅台", "shares": 1000,
                      "avail_shares": 1000, "cost": 1400.0}}
    ctx = mk_ctx(now=SAT, positions=pos)
    d = mk_dec("sell", "600519", 1500.0, 100, kill_liquidation=True)
    v = check(d, ctx, CFG)
    assert v.approved, v.violations
    assert any("规则4豁免" in w for w in v.warnings)
    # 普通卖单非时段照拒
    v2 = check(mk_dec("sell", "600519", 1500.0, 100), ctx, CFG)
    assert not v2.approved and hit(v2, "非交易时段")


def test_rule3_health_kill_liquidation_exempt():
    """W-A2③：数据健康异常日 kill 补清算单降级为留痕放行（不再 report_only）。"""
    ctx = mk_ctx(health_issues=["数据滞后 4 天（最新 2026-09-05）"],
                 positions={"600519": {"name": "贵州茅台", "shares": 1000,
                                       "avail_shares": 1000, "cost": 1400.0}})
    d = mk_dec("sell", "600519", 1500.0, 100, kill_liquidation=True)
    v = check(d, ctx, CFG)
    assert v.approved and not v.report_only, v.brief()
    assert any("规则3豁免" in w for w in v.warnings)
    assert any(e["rule"] == "kill_liquidation_health_exempt" for e in v.events)
    # 普通单照旧 report_only
    v2 = check(mk_dec("buy", "600519", 1500.0, 100), ctx, CFG)
    assert v2.report_only and not v2.approved


def test_rule18_kill_liquidation_exempt():
    """W-A2③：kill 补清算卖单豁免 1% 参与率上限（与 emergency_scan 同款）。"""
    ctx = mk_ctx(day_amount={"600519": 5000000.0},
                 positions={"600519": {"name": "贵州茅台", "shares": 1000,
                                       "avail_shares": 1000, "cost": 1400.0}})
    d = mk_dec("sell", "600519", 1500.0, 1000, kill_liquidation=True)  # 150 万 > 5 万
    v = check(d, ctx, CFG)
    assert v.approved, v.violations
    assert any("流动性豁免" in w for w in v.warnings)


def test_rule9_stale_price_source_warning_not_violation():
    """W-A9：price_source=stale_close（昨收冒充实价）时规则9 降为留痕警告——
    既不产生伪违规，也不静默放行（stale_price_guard 事件可查）；live 口径照旧比对。"""
    stale_ctx = mk_ctx(price_source={"600519": "stale_close", "000001": "stale_close"})
    # 偏离 6.67%（stale 基准下不再违规，改警告 + 事件）
    v = check(mk_dec("buy", "600519", 1600.0, 100), stale_ctx, CFG)
    assert not hit(v, "价格保护：委托价")
    assert any("陈旧昨收" in w for w in v.warnings)
    assert any(e["rule"] == "stale_price_guard" for e in v.events)
    # 未偏离的 stale 基准单同样留痕（不静默）
    v2 = check(mk_dec("buy", "600519", 1500.0, 100), stale_ctx, CFG)
    assert v2.approved
    assert any(e["rule"] == "stale_price_guard" for e in v2.events)
    # live 口径照旧：偏离 → 违规
    live_ctx = mk_ctx(price_source={"600519": "live"})
    v3 = check(mk_dec("buy", "600519", 1600.0, 100), live_ctx, CFG)
    assert not v3.approved and hit(v3, "价格保护")


# ---------------- 直接运行入口 ----------------

if __name__ == "__main__":
    import traceback
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print("PASS %s" % name)
        except Exception:
            failed += 1
            print("FAIL %s" % name)
            traceback.print_exc()
    print("\n%d/%d tests passed" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
