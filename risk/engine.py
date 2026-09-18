"""风控引擎：所有下单必经的硬规则裁决模块（纯本地规则、无网络；动态闸/ATR 止损
所需的 pandas 依赖经 risk.regime 懒加载，仅本地计算）。"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

# 市场口径唯一权威实现（common/market.py，Phase 2 收敛）：limit_pct/limit_price/
# in_trading_session 本体已移入，此处 re-export 保持 paper/tests 的 import 路径不破
from common.market import (SESSION_AM, SESSION_PM, in_trading_session,  # noqa: F401
                           is_trading_time, limit_pct, limit_price)

# P0-5：sell 单豁免"仅业绩预告负面"黑名单拦截的判定口径（单一权威在 risk.blacklist）
from risk.blacklist import is_earnings_only

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
    # ---- 2026-09 风控补强新增（缺省为空 = 对应规则自动跳过，向后兼容旧调用方） ----
    day_amount: Dict[str, float] = field(default_factory=dict)    # {code: 最新日线成交额}
    code_concepts: Dict[str, List[str]] = field(default_factory=dict)  # {code: [概念]}
    today_sold_codes: set = field(default_factory=set)            # 当日已卖出代码
    watchlist_codes: set = field(default_factory=set)             # 自选池全集（晋升判断用）
    # ---- 2026-09-14 市场环境总闸（risk/regime.py，策略库 Top2/Top3） ----
    position_cap: Optional[float] = None            # 动态总仓位上限（绝对值）；None=无附加约束
    atr_pct: Dict[str, float] = field(default_factory=dict)  # {code: ATR占比}（ATR 自适应止损）
    # ---- Sprint 1 任务 4：实时行情扩展（规则 21 条件② 死封判定） ----
    live_quotes: Dict[str, dict] = field(default_factory=dict)  # {code: quote_dict 含 ask1_vol/float_mv}
    # ---- Sprint4 W-A9（P1-5）：latest_prices 的口径标记（build_context 填写） ----
    # "live"=实时快照价；"stale_close"=实时缺失回退的日线收盘（昨收冒充实价）。
    # 缺省空 dict = 旧调用方未标注 → 规则9 按原行为比对（向后兼容）。
    price_source: Dict[str, str] = field(default_factory=dict)
    # ---- C-ARC-4（T2）：build_context 内 fail-open 的留痕便签 ----
    # 只追加、不参与裁决；调用方在 flush_events 时转成 failopen_regime_cap 事件落库。
    ctx_notes: List[str] = field(default_factory=list)


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
    kill_pending: List[str] = field(default_factory=list)  # T+1 不可卖、需次日补清算的代码
    kill_until: Optional[datetime] = None
    # Sprint 1 任务 4：跌停应急事件（规则 21），由 check() 调用方写 risk_event
    emergency_pending: Optional[dict] = None
    # P0-4：check() 期间产生的待落库 risk_event（{"rule","detail","once_today_prefix"}）。
    # check() 自身不再 get_conn() 直写生产库（测试会污染 risk_event），统一由
    # 调用方（runner.propose/confirm，持有 conn）经 flush_events 落库。
    events: List[dict] = field(default_factory=list)

    def brief(self) -> str:
        parts = ["approved=%s" % self.approved,
                 "report_only=%s" % self.report_only,
                 "kill_trigger=%s" % self.kill_trigger]
        if self.adjusted_order is not None:
            parts.append("adjusted_shares=%s" % self.adjusted_order.get("shares"))
        if self.kill_until is not None:
            parts.append("kill_until=%s" % self.kill_until.strftime("%Y-%m-%d %H:%M"))
        if self.kill_pending:
            parts.append("kill_pending=%s" % ",".join(self.kill_pending))
        if self.emergency_pending:
            parts.append("emergency_pending=%s" % self.emergency_pending.get("code"))
        return " ".join(parts)


# ---------------- 工具函数 ----------------
# （SESSION_AM/PM、in_trading_session、limit_pct、limit_price 已收敛至 common/market.py，
#  模块顶部 re-export——本文件不再有本地定义）

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


def _build_kill_orders(ctx: RiskContext) -> Tuple[List[dict], List[str]]:
    """对全部持仓按可卖数量生成市价卖单。

    avail_shares<=0（当日买入 T+1 不可卖）的票无法当日清仓，返回递延名单——
    执行层落 risk_event(kill_liquidation_pending)，次日解锁后由补清算流程卖出。
    此前直接跳过且停机期禁卖，残仓至少锁死 72 小时（kill 清仓承诺与能力不一致）。
    """
    orders, deferred = [], []
    for code, pos in (ctx.positions or {}).items():
        avail = int(pos.get("avail_shares", 0) or 0)
        if avail <= 0:
            if int(pos.get("shares", 0) or 0) > 0:
                deferred.append(code)
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
            "kill_liquidation": True,
        })
    return orders, deferred


# ---------------- 规则（逐条函数化） ----------------


def rule_blacklist(decision: dict, ctx: RiskContext, cfg: dict, v: Verdict) -> None:
    """规则1：黑名单中 ok=False 的标的拒绝。

    P0-5：sell 单豁免"仅业绩预告负面"拦截——止损卖出（含规则21 应急单）不应被
    业绩预告焊死出口（连续跌停+负面预告正是规则21 的典型场景）；含 ST/次新/
    上市天数等其他拦截理由时不豁免，buy 单一律不豁免。与规则20"sell 不挡"同理。
    """
    code = str(decision.get("code") or "")
    item = (ctx.blacklist or {}).get(code)
    if item and not item[0]:
        reason = item[1]
        # 归一化比较（与 check() 主流程同口径）："SELL"/" Sell " 等非常规输入
        # 不豁免 → fail-closed 走拦截分支
        if str(decision.get("action") or "").strip().lower() == "sell" \
                and is_earnings_only(reason):
            v.warnings.append(
                "黑名单豁免（sell）：%s 仅因业绩预告负面拦截，止损卖单放行" % code)
            return
        v.violations.append("黑名单：%s 被拦截（%s）" % (code, reason))


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
    """规则3：数据健康检查异常 → 降级为只出报告（不 approve）。

    W-A2③：kill_liquidation 补清算单降级为**留痕放行**——kill 高发恰在坏数据日，
    report_only 会让补清算在数据异常日永无成交机会（与规则5"强平最高优先级"
    同一口径）；豁免本身落 risk_event 可查。
    """
    if ctx.health_issues:
        if decision.get("kill_liquidation"):
            v.warnings.append(
                "规则3豁免：kill 补清算单在数据异常日留痕放行（%s）"
                % "; ".join(ctx.health_issues))
            v.events.append({
                "rule": "kill_liquidation_health_exempt",
                "detail": "kill_liquidation_health_exempt: 数据异常日（%s）补清算单 %s %s "
                          "留痕放行（强平优先，不受 report_only 降级）"
                          % ("; ".join(ctx.health_issues), decision.get("action"),
                             decision.get("code")),
                "once_today_prefix": "kill_liquidation_health_exempt: %s "
                                     % decision.get("code")})
            return
        v.report_only = True
        v.violations.append("数据健康异常：" + "; ".join(ctx.health_issues) + "，降级为只出报告")


def rule_trading_session(decision: dict, ctx: RiskContext, cfg: dict, v: Verdict) -> None:
    """规则4：非交易时段拒绝买卖（仅对 buy/sell 调用；hold/watch 永远放行）。

    豁免：Fix-4 跌停应急扫描单（emergency_scan=True）——盘后 propose 只是入库
    挂 pending、盘前 09:14 兜底 confirm 才真正执行，两次动作都不在连续竞价时段，
    若按普通单拒掉 D1 链路即失效；执行价仍受 confirm 内置价格类风控约束。
    W-A2①：kill_liquidation 补清算单同款豁免——resolve_liquidations 在 09:00
    premarket 调 propose，非交易时段必被拒 → 递延补清算 dead-on-arrival（P1-2）；
    强平最高优先级（CONSTRAINTS §3.3），与规则5 的清算单豁免同口径。
    """
    if not in_trading_session(ctx.now):
        if decision.get("emergency_scan"):
            v.warnings.append(
                "规则4豁免：跌停应急扫描单允许非交易时段入库/兜底确认（执行价二次校验保留）")
            return
        if decision.get("kill_liquidation") and decision.get("action") == "sell":
            v.warnings.append(
                "规则4豁免：kill 补清算单允许非交易时段 propose/确认（强平最高优先级，"
                "执行价二次校验保留）")
            return
        v.violations.append(
            "非交易时段：%s 不在 周一~周五 09:30-11:30/13:00-15:00，拒绝买卖"
            % ctx.now.strftime("%Y-%m-%d %H:%M:%S"))


def rule_kill_switch(decision: dict, ctx: RiskContext, cfg: dict, v: Verdict) -> None:
    """规则5：停机期内拒绝买入与普通卖出；kill_liquidation 清算卖单放行（补清仓）；
    回撤达阈值且未停机 → 触发清仓+停机。"""
    action = decision.get("action")
    in_stop = ctx.kill_switch_until is not None and ctx.now < ctx.kill_switch_until
    if in_stop and action == "buy":
        v.violations.append(
            "kill switch 停机期（至 %s），拒绝买入"
            % ctx.kill_switch_until.strftime("%Y-%m-%d %H:%M"))
    elif in_stop and action == "sell" and not decision.get("kill_liquidation"):
        v.violations.append(
            "kill switch 停机期（至 %s），拒绝普通卖出（清算单除外）"
            % ctx.kill_switch_until.strftime("%Y-%m-%d %H:%M"))
    peak = float(ctx.peak_equity or 0)
    equity = float(ctx.total_equity or 0)
    if peak > 0 and equity > 0:
        dd = 1.0 - equity / peak
        cap = float(cfg.get("max_drawdown_kill", 0.08))
        if dd >= cap and not in_stop:
            v.kill_trigger = True
            v.kill_until = ctx.now + timedelta(hours=float(cfg.get("kill_stop_hours", 72)))
            orders, deferred = _build_kill_orders(ctx)
            v.kill_orders = orders
            v.kill_pending = deferred
            msg = "kill switch 触发：回撤 %.1f%% ≥ %.0f%%，清仓并停机 %s 小时" % (
                dd * 100, cap * 100, cfg.get("kill_stop_hours", 72))
            if deferred:
                msg += "；%s 当日买入 T+1 不可卖，已列入次日补清算" % ",".join(deferred)
            v.violations.append(msg)


def stop_loss_line(ctx: RiskContext, cfg: dict, code: str) -> float:
    """单票有效止损线（浮亏口径）：max(stop_loss_pct, atr_stop_mult × 该票 ATR占比)。

    ATR 自适应（海龟 2N 逻辑，risk/regime.stop_loss_line）：高波票止损更宽防噪声
    扫损，低波票维持基础线；ATR 数据缺失 → 基础线（行为与旧版一致）。
    """
    from risk.regime import stop_loss_line as _line
    return _line(float(cfg.get("stop_loss_pct", 0.08) or 0),
                 (ctx.atr_pct or {}).get(code),
                 float(cfg.get("atr_stop_mult", 2.0) or 2.0))


def stop_loss_breaches(ctx: RiskContext, cfg: dict) -> List[Tuple[str, float]]:
    """单票浮亏超止损线的持仓 [(code, 浮亏%)]（供盘中扫描生成止损卖出提示）。

    止损线为 ATR 自适应口径（见 stop_loss_line），不再固定 8%。
    """
    cap = float(cfg.get("stop_loss_pct", 0.08) or 0)
    if cap <= 0:
        return []
    out = []
    for code, pos in (ctx.positions or {}).items():
        cost = float(pos.get("cost", 0) or 0)
        price = ctx.latest_prices.get(code)
        if cost <= 0 or price is None or price <= 0:
            continue
        loss = 1.0 - float(price) / cost
        if loss >= stop_loss_line(ctx, cfg, code) - 1e-12:
            out.append((code, loss))
    return sorted(out, key=lambda x: -x[1])


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
    """规则7：总仓位（现有持仓市值+本次买入金额)/总权益 ≤ 上限（仅 buy）。

    上限 = min(静态 max_total_weight, ctx.position_cap 动态闸)——动态闸由
    risk/regime.py（RSRS+二八三档、波动率目标）计算并经 build_context 注入；
    None 表示无附加约束（regime 故障 fail-open，静态上限仍生效）。
    """
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
    dyn = ctx.position_cap
    if dyn is not None:
        cap = min(cap, float(dyn))
        if w > cap + 1e-9:
            v.violations.append(
                "总仓位 %.1f%% > 动态闸上限 %.1f%%（市场环境总闸，静态 %.0f%%；"
                "降仓或等环境转暖，卖出不受限）" % (w * 100, cap * 100,
                                          float(cfg.get("max_total_weight", 0.80)) * 100))
            return
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
    """规则9：委托价偏离实时价超阈值拒绝。

    W-A9（P1-5）陈旧价口径：ctx.price_source 标记基准为 stale_close（实时缺失
    回退昨收）时，"昨收比昨收"式的自动比价不可靠——不再产生违规/放行的伪结论，
    降为 warning + risk_event 留痕（stale_price_guard）；真正的硬闸在执行定价层
    （confirm 不自动以昨收成交，见 runner.confirm 的 stale_price_halt /
    stale_price_exec）。缺 price_source 的旧调用方按原行为比对（向后兼容）。
    """
    order = decision.get("order") or {}
    code = str(decision.get("code") or "")
    ref = (ctx.latest_prices or {}).get(code)
    if not ref or ref <= 0:
        v.violations.append("价格保护：缺少 %s 的有效实时价，无法校验委托价" % code)
        return
    price = float(order.get("price", 0) or 0)
    if (ctx.price_source or {}).get(code) == "stale_close":
        v.warnings.append(
            "价格保护口径：%s 基准为陈旧昨收 %.2f（实时价缺失），自动比价跳过并留痕"
            "（执行定价由 confirm 口径闸把关）" % (code, float(ref)))
        v.events.append({
            "rule": "stale_price_guard",
            "detail": "stale_price_guard: %s 委托价 %.2f 的比价基准为陈旧昨收 %.2f"
                      "（实时价缺失），自动比价不可靠已留痕" % (code, price, float(ref)),
            "once_today_prefix": "stale_price_guard: %s " % code})
        return
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
    """规则14：涨跌停保护——买价达到涨停拒买，卖价达到跌停拒卖。

    豁免：decision['emergency_pending_skip']=True 且 risk_event 已有
    'limit_halt_emergency' 记录 → 仅记 warning 放行（让应急单穿透规则 14）。
    """
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
        up = limit_price(float(pc), pct, up=True)
        if price >= up:
            v.violations.append(
                "涨停保护：委托买价 %.2f ≥ 涨停价 %.2f（昨收 %.2f，±%.0f%%），拒买"
                % (price, up, float(pc), pct * 100))
    elif action == "sell":
        down = limit_price(float(pc), pct, up=False)
        if price <= down:
            # Sprint 1 任务 4：规则 21 跌停应急路径豁免
            if decision.get("emergency_pending_skip"):
                v.warnings.append(
                    "跌停保护豁免：limit_halt_emergency 应急单放行"
                    "（卖价 %.2f = 跌停价 %.2f）" % (price, down))
                return
            v.violations.append(
                "跌停保护：委托卖价 %.2f ≤ 跌停价 %.2f（昨收 %.2f，±%.0f%%），拒卖"
                % (price, down, float(pc), pct * 100))


def _limit_halt_condition(code: str, ctx: RiskContext, cfg: dict) -> Tuple[bool, dict]:
    """规则 21 三条件判定核心（Fix-4：engine 与 signals/limit_halt 扫描器共用）。

    ① 死封候选：跌停价（由调用方比对 order.price == down，此处只算出 down）
    ② 死封（可选）：quote.ask1_vol × 100 / (quote.float_mv / quote.price) > 3%
       —— 任一字段缺失视为 True（不阻断）
    ③ 浮亏 ≥ stop_loss_line（复用规则 16 现成实现 stop_loss_breaches）

    返回 (hit, info)：hit=False 时 info["reason"] 说明；hit=True 时 info 含
    down/loss/stop_line/seal_ratio（可能 None）。
    """
    pc = (ctx.prev_close or {}).get(code)
    if not pc or pc <= 0:
        return False, {"reason": "缺昨收价"}
    pct = limit_pct(code)
    down = limit_price(float(pc), pct, up=False)
    breach = {c: loss for c, loss in stop_loss_breaches(ctx, cfg)}
    loss = breach.get(code)
    if loss is None or loss < stop_loss_line(ctx, cfg, code) - 1e-12:
        return False, {"reason": "浮亏未破止损线", "down": down}
    quote = (ctx.live_quotes or {}).get(code) or {}
    ask1_vol = quote.get("ask1_vol")
    float_mv = quote.get("float_mv")
    cur_price = quote.get("price")
    seal_ratio = None
    if ask1_vol is not None and float_mv is not None and cur_price and cur_price > 0:
        float_shares = float(float_mv) / float(cur_price)
        if float_shares > 0:
            seal_ratio = (float(ask1_vol) * 100) / float_shares
            if seal_ratio < 0.03:
                return False, {"reason": "封单比不足 3%", "down": down,
                               "seal_ratio": seal_ratio}
    return True, {"down": down, "loss": loss,
                  "stop_line": stop_loss_line(ctx, cfg, code),
                  "seal_ratio": seal_ratio}


def rule_limit_halt_emergency(decision: dict, ctx: RiskContext, cfg: dict,
                              v: Verdict) -> None:
    """规则21：跌停封单应急——A股 T+1 + 涨跌停 + 隔夜跳空三重制度下，规则14
    '卖价=跌停价拒卖'会让连续跌停持仓票卡死。三条件 AND（条件② 缺数据静默跳过）：

    ① 卖单：action=='sell' 且 order.price == limit_price(prev_close, limit_pct, up=False)
    ② 死封（可选）：quote.ask1_vol × 100 / (quote.float_mv / quote.price) > 3%
       —— 任一字段缺失视为 True（不阻断，让条件③ 单独也能触发应急路径）
    ③ 浮亏 ≥ stop_loss_line（复用规则 16 现成实现 stop_loss_breaches）

    触发动作：写 risk_event(rule='limit_halt_emergency') 一条；不直接下单，
    走 confirm 闸门由用户放行（plan 4.3 默认 A 路径）。规则 14 通过
    decision['emergency_pending_skip']=True 同步豁免。

    Fix-4：emergency_scan=True（主动扫描器生成的待确认单）不再设置
    skip_gate 直写——D1 决策是 confirm 优先 + 09:14 超时兜底，扫描单必须
    停在 pending 等人工/兜底确认。
    """
    if decision.get("action") != "sell":
        return
    code = str(decision.get("code") or "")
    order = decision.get("order") or {}
    hit, info = _limit_halt_condition(code, ctx, cfg)
    if not hit:
        return
    down = info["down"]
    loss = info["loss"]
    seal_ratio = info.get("seal_ratio")
    price = float(order.get("price", 0) or 0)
    # 条件①：卖价 = 跌停价（与规则 14 同口径）
    if price > down:
        return  # 不是跌停价，不触发
    # 三条件全满足（② 缺数据时按"通过"处理）→ 写事件 + 豁免规则 14
    seal_note = (f"封单比 {seal_ratio:.2%}" if seal_ratio is not None
                 else "条件②缺数据，按'不阻断'处理")
    detail = (f"{code} 跌停应急: 卖价={price:.2f}={down:.2f}（跌停价）；"
              f"浮亏={loss:.2%}（止损线 {info['stop_line']:.2%}）；"
              f"{seal_note}；建议次日 09:15 集合竞价挂跌停价")
    try:
        # ctx 不一定带 conn，从决策层无法直接写库 → 挂到 v 上由 check() 调用方
        # 经 flush_events 写库（P0-4：不再 get_conn() 直写生产库）
        v.emergency_pending = {
            "rule": "limit_halt_emergency",
            "code": code,
            "detail": detail,
        }
        v.events.append({"rule": "limit_halt_emergency", "detail": detail,
                         "once_today_prefix": code + " "})
        # 同步给 decision 加豁免 flag，规则 14 检查这个 flag 决定放行
        decision["emergency_pending_skip"] = True
        if decision.get("emergency_scan"):
            # Fix-4：扫描单走 confirm 闸门；09:14 超时兜底是否自动执行由
            # execution.emergency_direct_exec 决定（Fix F，默认 false 只提醒）
            v.warnings.append("跌停应急扫描单：%s（confirm 优先；09:14 超时兜底"
                              "视 emergency_direct_exec 开关）" % detail)
            return
        # P1-6：skip_gate 仅标记"这是规则21 应急单"；是否直写成交由
        # execution.emergency_direct_exec 开关决定（默认 false → runner 仍走
        # confirm 人工闸门，恪守"绝不自动成交"总原则）。
        decision["skip_gate"] = True
        decision["confirmed_by"] = "emergency_rule21"
        v.warnings.append("跌停应急路径激活：%s（直写需 execution.emergency_direct_exec=true，"
                          "否则走 confirm 人工闸门）" % detail)
    except Exception:
        pass


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


def rule_stop_loss(decision: dict, ctx: RiskContext, cfg: dict, v: Verdict) -> None:
    """规则16：单票浮亏 ≥ 有效止损线（ATR 自适应，见 stop_loss_line）的票禁止加仓
    （浮亏更深只会放大暴露），应走止损卖出（盘中扫描据此生成提示）。"""
    if decision.get("action") != "buy":
        return
    code = str(decision.get("code") or "")
    for c, loss in stop_loss_breaches(ctx, cfg):
        if c == code:
            v.violations.append(
                "单票止损：%s 浮亏 %.1f%% ≥ 止损线 %.0f%%，禁止加仓（应止损卖出）"
                % (c, loss * 100, stop_loss_line(ctx, cfg, code) * 100))


def rule_concept_concentration(decision: dict, ctx: RiskContext, cfg: dict, v: Verdict) -> None:
    """规则17：同概念持仓市值+本次买入 ≤ max_concept_weight（概念齐涨齐跌，
    此前 5 只持仓可全押同一题材）。代码无概念标签时跳过。"""
    if decision.get("action") != "buy":
        return
    cap = float(cfg.get("max_concept_weight", 0.45) or 0)
    if cap <= 0:
        return
    code = str(decision.get("code") or "")
    concepts = ctx.code_concepts.get(code) or []
    if not concepts:
        return
    order = decision.get("order") or {}
    amount = float(order.get("price", 0) or 0) * _effective_shares(order, v)
    equity = float(ctx.total_equity or 0)
    if equity <= 0:
        return
    for concept in concepts:
        mv = sum(_position_mv(ctx, c) for c, pos in (ctx.positions or {}).items()
                 if c != code and concept in (ctx.code_concepts.get(c) or []))
        w = (mv + amount) / equity
        if w > cap + 1e-9:
            v.violations.append(
                "概念集中度：概念「%s」持仓+本次 %.1f%% > 上限 %.0f%%"
                % (concept, w * 100, cap * 100))


def rule_liquidity(decision: dict, ctx: RiskContext, cfg: dict, v: Verdict) -> None:
    """规则18：本次下单金额 ≤ 最新日线成交额 × max_amount_share（paper 资金对
    小成交额票会吃掉大量盘口，成交价假设失真）。无成交额数据时跳过。

    豁免：Fix-4 跌停应急扫描单（emergency_scan=True）——连续跌停日成交额萎缩
    是常态，全仓逃命单几乎必然超 1% 参与率，若照拒则 D1 应急链路形同虚设；
    应急单以跌停价集合竞价排队、不构成盘中砸盘冲击，且经人工 confirm/超时闸门。
    W-A2③：kill_liquidation 补清算卖单同款豁免（全仓清仓额几乎必然超 1%）。
    """
    order = decision.get("order") or {}
    if not (order and decision.get("action") in ("buy", "sell")):
        return
    cap = float(cfg.get("max_amount_share", 0.01) or 0)
    if cap <= 0:
        return
    code = str(decision.get("code") or "")
    amt = float(ctx.day_amount.get(code) or 0)
    if amt <= 0:
        return
    gross = float(order.get("price", 0) or 0) * _effective_shares(order, v)
    if gross > amt * cap + 1e-6:
        if decision.get("emergency_scan"):
            v.warnings.append(
                "流动性豁免：跌停应急扫描单不受 %.1f%% 参与率约束"
                "（跌停价排队逃命单，%.0f 元 > 成交额 %.0f × %.1f%%）"
                % (cap * 100, gross, amt, cap * 100))
            return
        if decision.get("kill_liquidation"):
            v.warnings.append(
                "流动性豁免：kill 补清算卖单不受 %.1f%% 参与率约束"
                "（强平优先，%.0f 元 > 成交额 %.0f × %.1f%%）"
                % (cap * 100, gross, amt, cap * 100))
            return
        v.violations.append(
            "流动性约束：下单 %.0f 元 > 最新成交额 %.0f × %.1f%%（=%.0f），拒绝"
            % (gross, amt, cap * 100, amt * cap))


def rule_round_trip(decision: dict, ctx: RiskContext, cfg: dict, v: Verdict) -> None:
    """规则19：当日已卖出的票禁止再买回（防止同日高频往返刷交易次数）。"""
    if decision.get("action") != "buy":
        return
    code = str(decision.get("code") or "")
    if code in (ctx.today_sold_codes or set()):
        v.violations.append("同票往返：%s 当日已卖出，禁止再买回" % code)


def rule_factor_crowding(decision: dict, ctx: RiskContext, cfg: dict,
                         v: Verdict) -> None:
    """规则20：因子拥挤度熔断（Sprint 1 任务 5）—— read_factor_crowding() 返回
    crowded=True 时，buy 单 target_weight > 5% 自动压回 5%（按当前 equity 重算股数）。
    sell 单不挡（拥挤熔断不该堵止损）。
    """
    if decision.get("action") != "buy":
        return
    try:
        from signals.signals import read_factor_crowding
        fc = read_factor_crowding()
    except Exception as e:  # noqa: BLE001
        # C-ARC-4（T2）：fail-open 也要留痕（此前仅静默放行，拥挤熔断失效无迹可查）。
        # detail 必须以 once_today_prefix 开头（ADR-0 §3），否则去重永不命中会刷表。
        v.events.append({
            "rule": "failopen_factor_crowding",
            "detail": "failopen_factor_crowding: 规则20 拥挤度读取失败，fail-open 放行 %s"
                      % repr(e)[:120],
            "once_today_prefix": "failopen_factor_crowding",
        })
        return
    if not fc.get("crowded"):
        return
    tw = decision.get("target_weight")
    if tw is None:
        return
    try:
        tw = float(tw)
    except (TypeError, ValueError):
        return
    cap = float(fc.get("crowded_max_weight", 0.05))
    if tw <= cap + 1e-12:
        return  # 本来就在 5% 以下，放行
    # 按 5% 等价股数压回（需要知道当前实时价与现金）
    code = str(decision.get("code") or "")
    order = decision.get("order") or {}
    price = float(order.get("price", 0) or 0)
    if price <= 0:
        return
    equity = float(ctx.total_equity or 0)
    if equity <= 0:
        return
    target_amount = equity * cap
    new_shares = int((target_amount // price) // 100 * 100)  # 整手规整
    if new_shares <= 0:
        v.violations.append(
            "因子拥挤熔断：%s target_weight %.1f%% > 5%% 上限，"
            "5%% 等价股数不足一手（%d 元/股），拒绝"
            % (code, tw * 100, int(price)))
        return
    adj = dict(order)
    adj["shares"] = new_shares
    v.adjusted_order = adj
    v.warnings.append(
        "因子拥挤熔断：%s target_weight %.1f%% → 5%% 上限，"
        "股数 %s → %d" % (code, tw * 100, order.get("shares"), new_shares))
    # 写 risk_event（同日去重）——P0-4：挂到 v.events 由调用方落库，不再
    # get_conn() 直写生产库（该路径在测试里污染了 risk_event 表）
    v.events.append({
        "rule": "factor_crowding_active",
        "detail": f"因子拥挤熔断生效: μ={fc.get('mu60')}, σ={fc.get('sigma60')}; "
                  f"target_weight {tw:.1%} → {cap:.0%} 上限",
        "once_today_prefix": "因子拥挤熔断生效"})


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
    rule_limit_halt_emergency(decision, ctx, cfg, v)   # 规则21（Sprint 1，必须在规则14之前：先写豁免 flag）
    rule_price_limit(decision, ctx, cfg, v)      # 规则14（被 21 触发的 emergency_pending_skip 豁免）
    rule_t_plus_1(decision, ctx, cfg, v)         # 规则12
    rule_lot_size(decision, ctx, cfg, v)         # 规则13（先规整，后续金额按规整后数量）
    rule_single_weight(decision, ctx, cfg, v)    # 规则6
    rule_total_weight(decision, ctx, cfg, v)     # 规则7
    rule_max_positions(decision, ctx, cfg, v)    # 规则8
    rule_daily_trades(decision, ctx, cfg, v)     # 规则10
    rule_weekly_turnover(decision, ctx, cfg, v)  # 规则11
    rule_stop_loss(decision, ctx, cfg, v)        # 规则16
    rule_concept_concentration(decision, ctx, cfg, v)  # 规则17
    rule_liquidity(decision, ctx, cfg, v)        # 规则18
    rule_round_trip(decision, ctx, cfg, v)       # 规则19
    rule_factor_crowding(decision, ctx, cfg, v)  # 规则20（Sprint 1 任务 5）

    # 规则 21 等事件已挂 v.events（P0-4）——check() 不再直写生产库，
    # 由调用方（runner.propose/confirm）经 flush_events(conn, v, decision_id) 落库

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


def flush_events(conn: sqlite3.Connection, v: Verdict,
                 decision_id: Optional[int] = None,
                 warn_prefix: Optional[str] = None) -> None:
    """把 check() 挂到 Verdict 上的 risk_event 写库（P0-4：engine 不再 get_conn 直写）。

    once_today_prefix 非空的事件按 rule+当日+detail 前缀去重（同票同日只留一条，
    规则21 反复 check 不再刷屏——此前无去重直写积累了大量重复行）。

    warn_prefix（C-ARC-4，T2）：非 None 时把 v.warnings 逐条转成 rule='warn' 事件
    落库，detail='<warn_prefix> <原文>'、once_today_prefix='<warn_prefix> '——
    复用同一去重三元组，worst case ≤1 行/票/日（调用方传决策 code）。
    detail 必须以前缀开头，否则 LIKE 永不命中、结构性 warning 会涓流刷表（ADR-0 §3）。
    """
    events = list(v.events)
    if warn_prefix:
        events = events + [{"rule": "warn", "detail": "%s %s" % (warn_prefix, w),
                            "once_today_prefix": "%s " % warn_prefix}
                           for w in (v.warnings or [])]
    for ev in events:
        try:
            prefix = ev.get("once_today_prefix")
            if prefix:
                today = datetime.now().strftime("%Y-%m-%d")
                n = conn.execute(
                    "SELECT COUNT(*) FROM risk_event WHERE rule=? AND ts LIKE ?"
                    " AND detail LIKE ?",
                    (ev["rule"], today + "%", prefix + "%")).fetchone()[0]
                if n:
                    continue
            record_event(conn, ev["rule"], ev["detail"], decision_id)
        except Exception as e:  # noqa: BLE001
            import logging as _lg
            _lg.getLogger("risk.engine").warning(
                "flush_events 落库失败（不阻断）: %s", repr(e))


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
    """读取配置的 risk 段（只读，不修改；CLI demo 用）。"""
    from common.config import snapshot
    return snapshot(path=Path(path) if path else None)["risk"]


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
        ("动态总仓位闸（regime cap=50% < 静态 80%，拒买）",
         _demo_dec("buy", "000001", 10.0, 3000),
         _demo_ctx(positions={"600519": {"name": "贵州茅台", "shares": 350,
                                         "avail_shares": 350, "cost": 1400.0}},
                   position_cap=0.50)),
        ("ATR 自适应止损：高波票 2ATR=14% 线，浮亏 10% 未破线（放行）",
         _demo_dec("buy", "300750", 12.0, 100),
         _demo_ctx(positions={"300750": {"name": "宁德时代", "shares": 100,
                                         "avail_shares": 100, "cost": 13.33}},
                   atr_pct={"300750": 0.07})),
        ("ATR 自适应止损：低波票仍按 8% 基础线，浮亏 10% 破线（拒买）",
         _demo_dec("buy", "300750", 12.0, 100),
         _demo_ctx(positions={"300750": {"name": "宁德时代", "shares": 100,
                                         "avail_shares": 100, "cost": 13.33}},
                   atr_pct={"300750": 0.03})),
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
