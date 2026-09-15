"""风控引擎测试：§5.4 十组违规拦截 + 合法通过 + kill switch + report_only + 留痕落库。"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import sqlite3
from datetime import datetime, timedelta

from risk.engine import (RiskContext, Verdict, check, record_event, apply_kill_switch,
                         limit_pct, in_trading_session)

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
    """跌停价卖出：昨收10.00，跌停9.00，卖价9.00 ≤ 跌停价 → 拒卖。"""
    ctx = mk_ctx(latest_prices={"600519": 1500.0, "000001": 9.0, "300750": 12.0},
                 positions={"000001": {"name": "平安银行", "shares": 1000,
                                       "avail_shares": 1000, "cost": 12.0}})
    v = check(mk_dec("sell", "000001", 9.0, 100), ctx, CFG)
    assert not v.approved
    assert hit(v, "跌停")
    assert len(v.violations) == 1


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
