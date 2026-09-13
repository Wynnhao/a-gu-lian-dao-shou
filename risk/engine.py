"""风控引擎：所有下单必经的硬规则裁决模块（纯规则、无网络、仅标准库）。"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Optional, Tuple

# ---------------- 数据结构 ----------------


@dataclass
class RiskContext:
    """风控上下文，由调用方在每次裁决前组装。"""
    now: datetime
    positions: Dict[str, dict]                       # {code: {name, shares, avail_shares, cost}}
    cash: float
    total_equity: float                              # 现金 + 持仓市值
    latest_prices: Dict[str, float]                  # {code: 实时价}
    prev_close: Dict[str, float]                     # {code: 昨收}
    today_trades: int = 0
    week_turnover: float = 0.0                       # 近5交易日成交额/总权益
    peak_equity: float = 0.0
    kill_switch_until: Optional[datetime] = None
    blacklist: Dict[str, Tuple[bool, str]] = field(default_factory=dict)
    health_issues: List[str] = field(default_factory=list)


@dataclass
class Verdict:
    """裁决结果：approved=False 时执行层必须放弃下单。"""
    approved: bool = False
    report_only: bool = False                        # 降级为只出报告不下单
    violations: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    adjusted_order: Optional[dict] = None            # 手数规整等修正后的 order
    kill_trigger: bool = False
    kill_orders: List[dict] = field(default_factory=list)  # 触发清仓时的 sell 指令
    kill_until: Optional[datetime] = None

    def brief(self) -> str:
        parts = ["approved=%s" % self.approved,
                 "report_only=%s" % self.report_only,
                 "kill_trigger=%s" % self.kill_trigger]
        if self.adjusted_order is not None:
            parts.append("adjusted_shares=%s" % self.adjusted_order.get("shares"))
        if self.kill_until is not None:
            parts.append("kill_until=%s" % self.kill_until.strftime("%Y-%m-%d %H:%M"))
        return " ".join(parts)


# ---------------- 工具函数 ----------------

SESSION_AM = (dtime(9, 30), dtime(11, 30))
SESSION_PM = (dtime(13, 0), dtime(15, 0))


def in_trading_session(now: datetime) -> bool:
    """是否处于 A 股连续竞价时段（周一~周五 09:30-11:30 / 13:00-15:00，边界含）。"""
    if now.weekday() >= 5:
        return False
    t = now.time()
    return SESSION_AM[0] <= t <= SESSION_AM[1] or SESSION_PM[0] <= t <= SESSION_PM[1]


def limit_pct(code: str) -> float:
    """涨跌停幅度：创业板(30)/科创板(68) 20%，其余 10%。"""
    code = str(code)
    return 0.20 if code.startswith(("30", "68")) else 0.10


def _position_mv(ctx: RiskContext, code: str) -> float:
    """单票持仓市值（优先实时价，缺实时价退回成本价）。"""
    pos = ctx.positions.get(code) or {}
    shares = float(pos.get("shares", 0) or 0)
    price = ctx.latest_prices.get(code)
    if price is None:
        price = float(pos.get("cost", 0) or 0)
    return shares * float(price)


def _effective_shares(order: dict, v: Verdict) -> int:
    """手数规整后的委托数量（未规整则取原始数量）。"""
    src = v.adjusted_order if v.adjusted_order is not None else order
    return int(round(float(src.get("shares", 0) or 0)))


def _build_kill_orders(ctx: RiskContext) -> List[dict]:
    """对全部持仓按可卖数量生成市价卖单（avail_shares<=0 的跳过）。"""
    orders = []
    for code, pos in (ctx.positions or {}).items():
        avail = int(pos.get("avail_shares", 0) or 0)
        if avail <= 0:
            continue
        price = ctx.latest_prices.get(code)
        if price is None:
            price = float(pos.get("cost", 0) or 0)
        orders.append({
            "code": code,
            "name": pos.get("name", code),
            "side": "sell",
            "type": "market",
            "price": float(price),
            "shares": avail,
            "reason": "kill switch 清仓",
        })
    return orders


# ---------------- 规则（逐条函数化） ----------------


def rule_blacklist(decision: dict, ctx: RiskContext, cfg: dict, v: Verdict) -> None:
    """规则1：黑名单中 ok=False 的标的拒绝。"""
    code = str(decision.get("code") or "")
    item = (ctx.blacklist or {}).get(code)
    if item and not item[0]:
        v.violations.append("黑名单：%s 被拦截（%s）" % (code, item[1]))


def rule_confidence(decision: dict, ctx: RiskContext, cfg: dict, v: Verdict) -> None:
    """规则2：置信度低于下限 → 降级为只出报告（不 approve）。"""
    conf = decision.get("confidence")
    try:
        conf = float(conf)
    except (TypeError, ValueError):
        conf = 0.0
    floor = float(cfg.get("min_confidence", 0.60))
    if conf < floor:
        v.report_only = True
        v.violations.append("置信度 %.2f < 下限 %.2f，降级为只出报告" % (conf, floor))


def rule_health(decision: dict, ctx: RiskContext, cfg: dict, v: Verdict) -> None:
    """规则3：数据健康检查异常 → 降级为只出报告（不 approve）。"""
    if ctx.health_issues:
        v.report_only = True
        v.violations.append("数据健康异常：" + "; ".join(ctx.health_issues) + "，降级为只出报告")


def rule_trading_session(decision: dict, ctx: RiskContext, cfg: dict, v: Verdict) -> None:
    """规则4：非交易时段拒绝买卖（仅对 buy/sell 调用；hold/watch 永远放行）。"""
    if not in_trading_session(ctx.now):
        v.violations.append(
            "非交易时段：%s 不在 周一~周五 09:30-11:30/13:00-15:00，拒绝买卖"
            % ctx.now.strftime("%Y-%m-%d %H:%M:%S"))


def rule_kill_switch(decision: dict, ctx: RiskContext, cfg: dict, v: Verdict) -> None:
    """规则5：停机期内拒绝一切买卖（buy/sell）；回撤达阈值且未停机 → 触发清仓+停机。"""
    action = decision.get("action")
    in_stop = ctx.kill_switch_until is not None and ctx.now < ctx.kill_switch_until
    if in_stop and action in ("buy", "sell"):
        v.violations.append(
            "kill switch 停机期（至 %s），拒绝一切买卖"
            % ctx.kill_switch_until.strftime("%Y-%m-%d %H:%M"))
    peak = float(ctx.peak_equity or 0)
    equity = float(ctx.total_equity or 0)
    if peak > 0 and equity > 0:
        dd = 1.0 - equity / peak
        cap = float(cfg.get("max_drawdown_kill", 0.08))
        if dd >= cap and not in_stop:
            v.kill_trigger = True
            v.kill_until = ctx.now + timedelta(hours=float(cfg.get("kill_stop_hours", 72)))
            v.kill_orders = _build_kill_orders(ctx)
            v.violations.append(
                "kill switch 触发：回撤 %.1f%% ≥ %.0f%%，清仓并停机 %s 小时"
                % (dd * 100, cap * 100, cfg.get("kill_stop_hours", 72)))


def rule_single_weight(decision: dict, ctx: RiskContext, cfg: dict, v: Verdict) -> None:
    """规则6：单票权重（该票持仓市值+本次买入金额)/总权益 ≤ 上限（仅 buy）。"""
    if decision.get("action") != "buy":
        return
    equity = float(ctx.total_equity or 0)
    if equity <= 0:
        v.violations.append("单票权重：total_equity 非法（≤0），拒绝买入")
        return
    order = decision.get("order") or {}
    amount = float(order.get("price", 0) or 0) * _effective_shares(order, v)
    w = (_position_mv(ctx, str(decision.get("code") or "")) + amount) / equity
    cap = float(cfg.get("max_single_weight", 0.20))
    if w > cap + 1e-9:
        v.violations.append("单票权重 %.1f%% > 上限 %.1f%%" % (w * 100, cap * 100))


def rule_total_weight(decision: dict, ctx: RiskContext, cfg: dict, v: Verdict) -> None:
    """规则7：总仓位（现有持仓市值+本次买入金额)/总权益 ≤ 上限（仅 buy）。"""
    if decision.get("action") != "buy":
        return
    equity = float(ctx.total_equity or 0)
    if equity <= 0:
        v.violations.append("总仓位：total_equity 非法（≤0），拒绝买入")
        return
    order = decision.get("order") or {}
    amount = float(order.get("price", 0) or 0) * _effective_shares(order, v)
    total_mv = sum(_position_mv(ctx, c) for c in (ctx.positions or {}))
    w = (total_mv + amount) / equity
    cap = float(cfg.get("max_total_weight", 0.80))
    if w > cap + 1e-9:
        v.violations.append("总仓位 %.1f%% > 上限 %.1f%%" % (w * 100, cap * 100))


def rule_max_positions(decision: dict, ctx: RiskContext, cfg: dict, v: Verdict) -> None:
    """规则8：买入后 distinct 持仓 code 数 ≤ 上限（仅 buy）。"""
    if decision.get("action") != "buy":
        return
    code = str(decision.get("code") or "")
    n = len(ctx.positions or {}) + (0 if code in (ctx.positions or {}) else 1)
    cap = int(cfg.get("max_positions", 5))
    if n > cap:
        v.violations.append("持仓数 %d > 上限 %d" % (n, cap))


def rule_price_guard(decision: dict, ctx: RiskContext, cfg: dict, v: Verdict) -> None:
    """规则9：委托价偏离实时价超阈值拒绝。"""
    order = decision.get("order") or {}
    code = str(decision.get("code") or "")
    ref = (ctx.latest_prices or {}).get(code)
    if not ref or ref <= 0:
        v.violations.append("价格保护：缺少 %s 的有效实时价，无法校验委托价" % code)
        return
    price = float(order.get("price", 0) or 0)
    dev = abs(price - ref) / float(ref)
    cap = float(cfg.get("price_guard_pct", 0.02))
    if dev > cap + 1e-12:
        v.violations.append(
            "价格保护：委托价 %.2f 偏离实时价 %.2f 达 %.2f%% > %.1f%%"
            % (price, ref, dev * 100, cap * 100))


def rule_daily_trades(decision: dict, ctx: RiskContext, cfg: dict, v: Verdict) -> None:
    """规则10：当日成交次数达到上限后拒绝。"""
    cap = int(cfg.get("max_daily_trades", 3))
    if int(ctx.today_trades or 0) >= cap:
        v.violations.append("当日交易次数 %d 已达上限 %d" % (int(ctx.today_trades or 0), cap))


def rule_weekly_turnover(decision: dict, ctx: RiskContext, cfg: dict, v: Verdict) -> None:
    """规则11：近5交易日换手 + 本次金额占比 ≤ 上限（买卖均计）。"""
    equity = float(ctx.total_equity or 0)
    if equity <= 0:
        v.violations.append("周换手：total_equity 非法（≤0），拒绝")
        return
    order = decision.get("order") or {}
    amount = float(order.get("price", 0) or 0) * _effective_shares(order, v)
    add = amount / equity
    cur = float(ctx.week_turnover or 0)
    cap = float(cfg.get("max_weekly_turnover", 2.0))
    if cur + add > cap + 1e-9:
        v.violations.append(
            "周换手 %.0f%% + 本次 %.1f%% = %.1f%% > 上限 %.0f%%"
            % (cur * 100, add * 100, (cur + add) * 100, cap * 100))


def rule_t_plus_1(decision: dict, ctx: RiskContext, cfg: dict, v: Verdict) -> None:
    """规则12：卖出数量不得超过可卖数量 avail_shares（T+1，仅 sell）。"""
    if decision.get("action") != "sell":
        return
    code = str(decision.get("code") or "")
    pos = (ctx.positions or {}).get(code) or {}
    avail = int(pos.get("avail_shares", 0) or 0)
    order = decision.get("order") or {}
    shares = int(round(float(order.get("shares", 0) or 0)))
    if shares > avail:
        v.violations.append("T+1：委托卖出 %d 股 > 可卖 %d 股" % (shares, avail))


def rule_lot_size(decision: dict, ctx: RiskContext, cfg: dict, v: Verdict) -> None:
    """规则13：买入数量规整为整手，规整后为0拒绝；卖出允许非整手（仅 buy）。"""
    if decision.get("action") != "buy":
        return
    lot = int(cfg.get("lot_size", 100))
    order = decision.get("order") or {}
    shares = int(round(float(order.get("shares", 0) or 0)))
    if shares <= 0:
        v.violations.append("手数：买入数量非法（≤0）")
        return
    if shares % lot != 0:
        adj = (shares // lot) * lot
        if adj <= 0:
            v.violations.append("手数：%d 股不足一手（%d），规整后为 0，拒绝" % (shares, lot))
            return
        adjusted = dict(order)
        adjusted["shares"] = adj
        v.adjusted_order = adjusted
        v.warnings.append("手数规整：%d → %d 股（%d 的整数倍）" % (shares, adj, lot))


def rule_price_limit(decision: dict, ctx: RiskContext, cfg: dict, v: Verdict) -> None:
    """规则14：涨跌停保护——买价达到涨停拒买，卖价达到跌停拒卖。"""
    order = decision.get("order") or {}
    code = str(decision.get("code") or "")
    action = decision.get("action")
    pc = (ctx.prev_close or {}).get(code)
    if not pc or pc <= 0:
        v.warnings.append("涨跌停保护：缺少 %s 昨收价，跳过校验" % code)
        return
    pct = limit_pct(code)
    price = float(order.get("price", 0) or 0)
    if action == "buy":
        up = round(float(pc) * (1 + pct), 2)
        if price >= up:
            v.violations.append(
                "涨停保护：委托买价 %.2f ≥ 涨停价 %.2f（昨收 %.2f，±%.0f%%），拒买"
                % (price, up, float(pc), pct * 100))
    elif action == "sell":
        down = round(float(pc) * (1 - pct), 2)
        if price <= down:
            v.violations.append(
                "跌停保护：委托卖价 %.2f ≤ 跌停价 %.2f（昨收 %.2f，±%.0f%%），拒卖"
                % (price, down, float(pc), pct * 100))


def rule_target_weight(decision: dict, ctx: RiskContext, cfg: dict, v: Verdict) -> None:
    """规则15：目标权重越界（<0 或 > 单票上限）拒绝。"""
    tw = decision.get("target_weight")
    if tw is None:
        return
    try:
        tw = float(tw)
    except (TypeError, ValueError):
        v.violations.append("目标权重非法：%r" % (decision.get("target_weight"),))
        return
    cap = float(cfg.get("max_single_weight", 0.20))
    if tw < 0 or tw > cap + 1e-9:
        v.violations.append("目标权重 %.1f%% 越界（合法范围 0 ~ %.0f%%）" % (tw * 100, cap * 100))


# ---------------- 结构校验与主入口 ----------------


def _check_order_struct(decision: dict, v: Verdict) -> Optional[dict]:
    """buy/sell 的 order 结构校验（方向一致、价格数量为正数）。非法时返回 None。"""
    action = str(decision.get("action") or "").strip().lower()
    code = str(decision.get("code") or "").strip()
    order = decision.get("order")
    problems = []
    if action not in ("buy", "sell"):
        problems.append("action 非法：%r" % (decision.get("action"),))
    if not (len(code) == 6 and code.isdigit()):
        problems.append("code 非法：%r" % (decision.get("code"),))
    if not isinstance(order, dict):
        problems.append("缺少 order")
        order = {}
    else:
        if order.get("side") != action:
            problems.append("order.side=%r 与 action=%r 不一致" % (order.get("side"), action))
        try:
            if float(order.get("price")) <= 0:
                problems.append("order.price 必须 > 0")
        except (TypeError, ValueError):
            problems.append("order.price 非法：%r" % (order.get("price"),))
        try:
            if float(order.get("shares")) <= 0:
                problems.append("order.shares 必须 > 0")
        except (TypeError, ValueError):
            problems.append("order.shares 非法：%r" % (order.get("shares"),))
    if problems:
        v.violations.append("下单结构非法：" + "；".join(problems))
        return None
    return {"side": action, "price": float(order["price"]), "shares": float(order["shares"])}


def check(decision: dict, ctx: RiskContext, cfg: dict) -> Verdict:
    """风控主入口：对一条 AI 决策做全部硬规则裁决，返回 Verdict。"""
    v = Verdict()
    decision = decision or {}
    action = str(decision.get("action") or "").strip().lower()

    # 全局规则（对任意 action 生效）：置信度/健康降级、目标权重越界、回撤 kill 触发
    rule_confidence(decision, ctx, cfg, v)   # 规则2
    rule_health(decision, ctx, cfg, v)       # 规则3
    rule_target_weight(decision, ctx, cfg, v)  # 规则15
    rule_kill_switch(decision, ctx, cfg, v)  # 规则5（触发清仓对任意 action 生效）

    if action in ("hold", "watch"):
        # hold/watch 为无交易动作：除上述全局规则外永远放行
        v.approved = (not v.violations) and (not v.report_only) and (not v.kill_trigger)
        return v

    if action not in ("buy", "sell"):
        v.violations.append("未知 action：%r，拒绝" % (decision.get("action"),))
        return v

    if _check_order_struct(decision, v) is None:
        return v

    rule_trading_session(decision, ctx, cfg, v)  # 规则4
    rule_blacklist(decision, ctx, cfg, v)        # 规则1
    rule_price_guard(decision, ctx, cfg, v)      # 规则9
    rule_price_limit(decision, ctx, cfg, v)      # 规则14
    rule_t_plus_1(decision, ctx, cfg, v)         # 规则12
    rule_lot_size(decision, ctx, cfg, v)         # 规则13（先规整，后续金额按规整后数量）
    rule_single_weight(decision, ctx, cfg, v)    # 规则6
    rule_total_weight(decision, ctx, cfg, v)     # 规则7
    rule_max_positions(decision, ctx, cfg, v)    # 规则8
    rule_daily_trades(decision, ctx, cfg, v)     # 规则10
    rule_weekly_turnover(decision, ctx, cfg, v)  # 规则11

    v.approved = (not v.violations) and (not v.report_only) and (not v.kill_trigger)
    return v


# ---------------- 留痕（SQLite） ----------------


def record_event(conn: sqlite3.Connection, rule: str, detail: str,
                 decision_id: Optional[int] = None) -> None:
    """写 risk_event 留痕（ts=now iso）。"""
    conn.execute(
        "INSERT INTO risk_event (ts, rule, detail, decision_id) VALUES (?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), rule, detail, decision_id),
    )
    conn.commit()


def apply_kill_switch(conn: sqlite3.Connection, kill_until: Optional[datetime],
                      note: str = "") -> None:
    """在 portfolio_state 最新日期行置 kill_switch=1（空表则插入今日行），并写 risk_event 留痕。"""
    msg = "kill switch 至 %s" % (kill_until.strftime("%Y-%m-%d %H:%M") if kill_until else "-")
    if note:
        msg += "（%s）" % note
    row = conn.execute(
        "SELECT date FROM portfolio_state ORDER BY date DESC LIMIT 1").fetchone()
    if row:
        conn.execute("UPDATE portfolio_state SET kill_switch=1, note=? WHERE date=?",
                     (msg, row[0]))
    else:
        conn.execute(
            "INSERT INTO portfolio_state (date, cash, market_value, total, drawdown,"
            " kill_switch, note) VALUES (?,?,?,?,?,?,?)",
            (datetime.now().strftime("%Y-%m-%d"), 0.0, 0.0, 0.0, None, 1, msg),
        )
    record_event(conn, "kill_switch", msg)


# ---------------- CLI demo ----------------


def load_cfg(path: Optional[str] = None) -> dict:
    """读取 config.json 的 risk 段（只读，不修改）。"""
    p = Path(path) if path else BASE / "config.json"
    return json.loads(p.read_text(encoding="utf-8"))["risk"]


def _demo_ctx(**over) -> RiskContext:
    base = dict(
        now=datetime(2026, 9, 9, 10, 0, 0),   # 周三盘中
        positions={},
        cash=1000000.0,
        total_equity=1000000.0,
        latest_prices={"600519": 1500.0, "000001": 10.0, "300750": 12.0, "688801": 50.0},
        prev_close={"600519": 1490.0, "000001": 10.0, "300750": 10.0, "688801": 50.0},
        today_trades=0,
        week_turnover=0.0,
        peak_equity=1000000.0,
        kill_switch_until=None,
        blacklist={},
        health_issues=[],
    )
    base.update(over)
    return RiskContext(**base)


def _demo_dec(action: str, code: str, price: float, shares: int, **over) -> dict:
    d = {
        "action": action, "code": code, "target_weight": 0.10,
        "confidence": 0.8, "reasons": ["demo理由"], "risk_notes": [],
        "order": {"side": action, "price": price, "shares": shares},
    }
    d.update(over)
    return d


def demo() -> None:
    """人工冒烟入口：构造典型违规/合法场景逐个跑 check 并打印结果。"""
    cfg = load_cfg()
    kill_pos = {
        "600519": {"name": "贵州茅台", "shares": 1000, "avail_shares": 1000, "cost": 1050.0},
        "000001": {"name": "平安银行", "shares": 2000, "avail_shares": 0, "cost": 10.0},
    }
    scenarios = [
        ("合法买入（整手，应通过）",
         _demo_dec("buy", "600519", 1500.0, 100), _demo_ctx()),
        ("手数规整（150→100，应通过并给 adjusted）",
         _demo_dec("buy", "600519", 1500.0, 150), _demo_ctx()),
        ("不足一手（50股，规整后为0，拒绝）",
         _demo_dec("buy", "600519", 1500.0, 50), _demo_ctx()),
        ("hold 放行", _demo_dec("hold", "600519", 0, 0,
                               order={"side": "hold", "price": 0, "shares": 0}),
         _demo_ctx()),
        ("watch 放行", _demo_dec("watch", "000001", 0, 0,
                                 order={"side": "watch", "price": 0, "shares": 0}),
         _demo_ctx()),
        ("超单票权重（30% > 20%）",
         _demo_dec("buy", "600519", 1500.0, 200), _demo_ctx()),
        ("超总仓位（84.9% > 80%）",
         _demo_dec("buy", "000001", 10.0, 15000),
         _demo_ctx(positions={"600519": {"name": "贵州茅台", "shares": 466,
                                         "avail_shares": 466, "cost": 1400.0}})),
        ("超持仓数（第6只）",
         _demo_dec("buy", "688801", 50.0, 100),
         _demo_ctx(positions={c: {"name": c, "shares": 100, "avail_shares": 100, "cost": 10.0}
                              for c in ("600519", "000001", "300750", "601318", "688981")})),
        ("价格保护（委托价偏离实时价 6.67% > 2%）",
         _demo_dec("buy", "600519", 1600.0, 100), _demo_ctx()),
        ("黑名单拦截",
         _demo_dec("buy", "688801", 50.0, 100),
         _demo_ctx(blacklist={"688801": (False, "次新股(首日/无涨跌幅限制)")})),
        ("非交易时段（周六）",
         _demo_dec("buy", "600519", 1500.0, 100),
         _demo_ctx(now=datetime(2026, 9, 12, 10, 0, 0))),
        ("非交易时段（午休 12:00）",
         _demo_dec("sell", "600519", 1500.0, 100),
         _demo_ctx(now=datetime(2026, 9, 9, 12, 0, 0),
                   positions={"600519": {"name": "贵州茅台", "shares": 500,
                                         "avail_shares": 500, "cost": 1400.0}})),
        ("超当日次数（3/3）",
         _demo_dec("buy", "600519", 1500.0, 100), _demo_ctx(today_trades=3)),
        ("超周换手（195%+15% > 200%）",
         _demo_dec("buy", "600519", 1500.0, 100), _demo_ctx(week_turnover=1.95)),
        ("T+1 超卖（1000 > 可卖400）",
         _demo_dec("sell", "600519", 1500.0, 1000),
         _demo_ctx(positions={"600519": {"name": "贵州茅台", "shares": 1000,
                                         "avail_shares": 400, "cost": 1400.0}})),
        ("涨停价买入（主板 10% 边界 11.00）",
         _demo_dec("buy", "000001", 11.0, 1000),
         _demo_ctx(latest_prices={"600519": 1500.0, "000001": 11.0,
                                  "300750": 12.0, "688801": 50.0})),
        ("创业板 20% 涨停边界：12.00 拒",
         _demo_dec("buy", "300750", 12.0, 100), _demo_ctx()),
        ("创业板 20% 涨停边界：11.99 过",
         _demo_dec("buy", "300750", 11.99, 100), _demo_ctx()),
        ("跌停价卖出（9.00 ≤ 跌停价 9.00）",
         _demo_dec("sell", "000001", 9.0, 100),
         _demo_ctx(latest_prices={"600519": 1500.0, "000001": 9.0,
                                  "300750": 12.0, "688801": 50.0},
                   positions={"000001": {"name": "平安银行", "shares": 1000,
                                         "avail_shares": 1000, "cost": 12.0}})),
        ("低置信度（0.5 < 0.6，只出报告）",
         _demo_dec("buy", "600519", 1500.0, 100, confidence=0.5), _demo_ctx()),
        ("数据健康异常（只出报告）",
         _demo_dec("buy", "600519", 1500.0, 100),
         _demo_ctx(health_issues=["数据滞后 4 天（最新 2026-09-05）"])),
        ("目标权重越界（30% > 20%）",
         _demo_dec("buy", "600519", 1500.0, 100, target_weight=0.30), _demo_ctx()),
        ("kill switch 触发（回撤 8.5% ≥ 8%，清仓+停机72h）",
         _demo_dec("hold", "600519", 0, 0,
                   order={"side": "hold", "price": 0, "shares": 0}),
         _demo_ctx(positions=kill_pos, total_equity=915000.0)),
        ("kill switch 停机期内买卖拒绝（hold 仍放行）",
         _demo_dec("buy", "600519", 1500.0, 100),
         _demo_ctx(kill_switch_until=datetime(2026, 9, 9, 15, 30, 0))),
        ("kill switch 到期恢复（昨日期限已过+回撤1%，通过）",
         _demo_dec("buy", "600519", 1500.0, 100),
         _demo_ctx(kill_switch_until=datetime(2026, 9, 9, 9, 0, 0),
                   total_equity=990000.0)),
    ]

    print("==== P4.1 风控引擎 demo（cfg=%s）====" % json.dumps(cfg, ensure_ascii=False))
    for i, (name, dec, ctx) in enumerate(scenarios, 1):
        v = check(dec, ctx, cfg)
        print("\n[%02d] %s" % (i, name))
        print("     decision: %s" % json.dumps(dec, ensure_ascii=False))
        print("     -> %s" % v.brief())
        if v.violations:
            for x in v.violations:
                print("        [违规] %s" % x)
        if v.warnings:
            for x in v.warnings:
                print("        [警告] %s" % x)
        if v.kill_orders:
            print("        [清仓指令] %s" % json.dumps(v.kill_orders, ensure_ascii=False))
    print("\n==== demo 结束 ====")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()
    else:
        print("用法: python3 risk/engine.py demo")
        sys.exit(1)
