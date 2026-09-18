"""PaperBroker：paper 模拟成交引擎（现金/持仓/佣金/印花税/T+1 可卖/下单回读校验，纯 SQLite 无网络）。

口径约定（与 review/daily.py 的现金还原保持一致）：
- trade.amount = 现金净流：买入 = price×shares + 佣金；卖出 = price×shares − 佣金 − 印花税。
- 账户现金 = paper_start_cash − Σ买入amount + Σ卖出amount（仅有效成交，
  status 不属于 rejected/cancelled/canceled/pending）；review/daily.mark_to_market 同口径还原。
- T+1：买入当日 avail_shares 不增加，由盘前流水线调用 unlock_t_plus_1() 把 avail 同步为 shares。
- position.cost = 成交价加权平均（不含费用，费用只影响现金）。
- paper 模式 trade.shots 固定写 "[]"（UI 模式由 runbook 截图路径填充）。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import json
import logging
import logging.handlers
import sqlite3
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

# limit_price 为 common/market.py 唯一口径（engine re-export）；「超板才拒(>)」的
# 调用点策略留在本模块（红线2，与 engine「到板即拒(≥)」不同）
from common.config import snapshot
from data import repo
from risk.engine import limit_pct, limit_price, record_event

FULL_CFG = snapshot()  # 统一配置层：import 期冻结（test 注入仍可原地 mutate）
EXEC_CFG_DEFAULT = dict(FULL_CFG.get("execution", {}))



def _exec_logger(name: str) -> logging.Logger:
    """exec.log 专用 logger（不向 root 传播，避免混入 fetch.log/stdout）。"""
    lg = logging.getLogger(name)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    if not lg.handlers:
        from common.logsetup import rotating_handler  # AGSICKLE_LOG_DIR 逃生门（Sprint4 W0-1）
        fh = rotating_handler("exec.log")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        lg.addHandler(fh)
    return lg


log = _exec_logger("exec.paper")


class _Reject(Exception):
    """成交前置防线拒绝（内部信号，buy/sell 捕获后转为返回 None）。"""


def _r2(x: float) -> float:
    """金额保留两位小数（修正 -0.0）。"""
    return round(float(x) + 0.0, 2)


def _slippage_side(exec_cfg: dict, side: str, price: float) -> float:
    """滑点调整后的成交价：买入上滑、卖出下滑（bps，默认 0=关闭）。

    环境开关 AGSICKLE_DISABLE_SLIPPAGE=1（测试用）强制关闭——否则读取真实
    config.json 的 PaperBroker 会让既有金额断言全部漂移。
    """
    import os
    if os.environ.get("AGSICKLE_DISABLE_SLIPPAGE") == "1":
        return price
    bps = float(exec_cfg.get("slippage_bps", 0) or 0)
    if bps <= 0:
        return price
    adj = 1.0 + (bps / 10000.0 if side == "buy" else -bps / 10000.0)
    return float(price) * adj


def compute_fees(side: str, price: float, shares: int, exec_cfg: dict) -> dict:
    """按 execution 费率重算单笔费用。

    返回 {gross, commission, stamp_tax, amount}：
    - gross = price × shares；
    - commission = max(gross × commission_rate, min_commission)；
    - stamp_tax 仅卖出计收；
    - amount = 现金净流 = gross + commission − stamp_tax（买入为总支出，卖出为净入账）。
    price 应传入滑点调整后的实际成交价（由 PaperBroker 负责），本函数不做滑点。
    """
    gross = _r2(float(price) * int(shares))
    commission = max(_r2(gross * float(exec_cfg.get("commission_rate", 0.00025))),
                     float(exec_cfg.get("min_commission", 5.0)))
    stamp = _r2(gross * float(exec_cfg.get("stamp_tax_rate", 0.0005))) if side == "sell" else 0.0
    sign = 1.0 if side == "buy" else -1.0             # 买入佣金加到支出，卖出从入账扣
    amount = _r2(gross + sign * commission - stamp)
    return {"gross": gross, "commission": commission, "stamp_tax": stamp, "amount": amount}


class PaperBroker:
    """模拟盘经纪商：现金/持仓账本以 trade 流水为唯一事实源（重放可完整还原）。"""

    def __init__(self, exec_cfg: Optional[dict] = None):
        self.cfg = dict(EXEC_CFG_DEFAULT)
        if exec_cfg:
            self.cfg.update(exec_cfg)
        import os
        if os.environ.get("AGSICKLE_DISABLE_SLIPPAGE") == "1":
            self.cfg["slippage_bps"] = 0
            self.cfg["volume_participation_cap"] = 0

    # ------------------------------------------------------------ 成交前置防线

    def _pre_trade_guards(self, conn: sqlite3.Connection, code: str, side: str,
                          price: float, shares: int,
                          decision_id: Optional[int]) -> None:
        """成交前置防线（任一触发直接拒绝成交，返回 None 由调用方处理）：

        1. 幂等：同 decision_id 已有有效成交 → 拒绝（readback 失败后重跑 confirm
           曾会造成同一决策二次成交）；
        2. 停板模拟：买价超涨停/卖价超跌停 → 拒绝（现实中根本无法成交，
           此前 paper 只查 price>0，涨停价也能"成交"）；
        3. 流动性：下单金额 > 最新日线成交额 × volume_participation_cap → 拒绝。
        """
        if decision_id is not None:
            dup = repo.has_effective_trade(conn, decision_id)
            if dup:
                record_event(conn, "duplicate_decision",
                             "decision#%s 已有成交，拒绝重复执行（%s %s x%d）"
                             % (decision_id, side, code, shares), decision_id)
                raise _Reject("decision#%s 已有成交（幂等拒绝）" % decision_id)
        if self.cfg.get("sim_limit_halt", True) and price > 0:
            pc = self.prev_close(conn, code)
            if pc:
                pct = limit_pct(code)  # 停板幅度统一走风控引擎口径（含北交所30%）
                if side == "buy" and price > limit_price(pc, pct, True) + 1e-9:
                    raise _Reject("买价 %.2f 超涨停价，涨停无法成交" % price)
                if side == "sell" and price < limit_price(pc, pct, False) - 1e-9:
                    raise _Reject("卖价 %.2f 低于跌停价，跌停无法成交" % price)
        cap = float(self.cfg.get("volume_participation_cap", 0) or 0)
        if cap > 0:
            row = conn.execute(
                "SELECT amount FROM daily_bar WHERE code=? ORDER BY trade_date DESC LIMIT 1",
                (code,)).fetchone()
            amt = float(row[0] or 0) if row else 0.0
            if amt > 0 and price * shares > amt * cap + 1e-6:
                raise _Reject("下单 %.0f 元 > 最新成交额 %.0f × %.1f%%（流动性约束）"
                              % (price * shares, amt, cap * 100))

    def _exec_price(self, side: str, price: float) -> float:
        return _slippage_side(self.cfg, side, price)

    # ------------------------------------------------------------ 账户基础

    @property
    def start_cash(self) -> float:
        return float(self.cfg.get("paper_start_cash", 1000000.0))

    def cash(self, conn: sqlite3.Connection) -> float:
        """当前现金：期初资金 − Σ买入amount + Σ卖出amount（仅有效成交，与复盘口径一致）。"""
        flow = repo.cash_flows(conn)
        return _r2(self.start_cash - flow.get("buy", 0.0) + flow.get("sell", 0.0))

    def ensure_account(self, conn: sqlite3.Connection) -> bool:
        """position 表与 portfolio_state 当日行都为空时初始化现金=paper_start_cash。"""
        n_pos = conn.execute("SELECT COUNT(*) FROM position").fetchone()[0]
        today = date.today().isoformat()
        has_today = repo.has_state(conn, today)
        if n_pos == 0 and not has_today:
            conn.execute(
                "INSERT INTO portfolio_state (date, cash, market_value, total, drawdown,"
                " kill_switch, note) VALUES (?,?,?,?,?,?,?)",
                (today, self.start_cash, 0.0, self.start_cash, 0.0, 0, "paper 初始化"))
            conn.commit()
            log.info("ensure_account: 初始化 paper 账户 现金=%.2f", self.start_cash)
            return True
        return False

    def unlock_t_plus_1(self, conn: sqlite3.Connection,
                        as_of: Optional[str] = None) -> int:
        """T+1 解锁：avail_shares 同步为 shares，但 as_of 当日买入的部分除外。

        盘前流水线在每个交易日开盘前调用。显式扣除当日买入，防止盘中补跑
        盘前流水线把当天刚买入的仓位提前解锁（破坏 T+1）。
        """
        d = as_of or datetime.now().strftime("%Y-%m-%d")
        cur = conn.execute(
            "UPDATE position SET avail_shares = MAX(shares - COALESCE("
            "(SELECT SUM(t.shares) FROM trade t WHERE t.code = position.code "
            "AND t.side = 'buy' AND t.trade_date = ?), 0), 0)", (d,))
        conn.commit()
        if cur.rowcount:
            log.info("unlock_t_plus_1: %d 只持仓可卖数量已同步（扣除 %s 当日买入）",
                     cur.rowcount, d)
        return cur.rowcount

    # ------------------------------------------------------------ 行情

    def latest_price(self, conn: sqlite3.Connection, code: str,
                     live: bool = True) -> Optional[float]:
        """该票"当前价"：交易时段内优先实时行情（腾讯/东财），闭市、被禁用或
        行情不可用时回退 daily_bar 最新收盘。无任何数据返回 None。"""
        close = repo.latest_close(conn, code)
        if not live:
            return close
        p = self._live_quote_price(code)
        return p if p is not None else close

    @staticmethod
    def _live_quote_price(code: str) -> Optional[float]:
        """实时价提取；环境开关/配置关闭/非交易时段/网络失败一律 None。"""
        import os
        if os.environ.get("AGSICKLE_DISABLE_LIVE_QUOTES") == "1":
            return None
        if not EXEC_CFG_DEFAULT.get("use_live_prices", True):
            return None
        try:
            from data.quotes import get_live_prices, is_trading_time
            if not is_trading_time():
                return None
            q = get_live_prices([str(code)])
            return (q.get(str(code)) or {}).get("price")
        except Exception as e:  # noqa: BLE001
            log.warning("实时价获取失败（回退日线收盘）: %s", repr(e)[:120])
            return None

    def prev_close(self, conn: sqlite3.Connection, code: str) -> Optional[float]:
        """前一交易日收盘（涨跌停基准用）。"""
        return repo.latest_close(conn, code, offset=1)

    # ------------------------------------------------------------ 买卖

    def buy(self, conn: sqlite3.Connection, code: str, name: str, price: float, shares: int,
            decision_id: Optional[int] = None, confirmed_by: Optional[str] = None,
            trade_date: Optional[str] = None) -> Optional[dict]:
        """买入：前置防线 → 现金充足校验 → position upsert（avail 不变，T+1）→ 写 trade。

        成交价 = 委托价 ×（1+滑点bps）；现金不足/前置防线拒绝/参数非法返回 None。
        """
        shares = int(shares)
        price = float(price)
        if shares <= 0 or price <= 0:
            log.warning("buy 拒绝：非法参数 code=%s price=%s shares=%s", code, price, shares)
            return None
        try:
            self._pre_trade_guards(conn, code, "buy", price, shares, decision_id)
        except _Reject as e:
            log.warning("buy 拒绝：%s code=%s", e, code)
            return None
        exec_price = self._exec_price("buy", price)
        fees = compute_fees("buy", exec_price, shares, self.cfg)
        cash_before = self.cash(conn)
        if cash_before < fees["amount"] - 1e-6:
            log.warning("buy 拒绝：现金不足 code=%s 需要 %.2f（含佣金）仅 %.2f",
                        code, fees["amount"], cash_before)
            return None

        row = conn.execute(
            "SELECT shares, cost FROM position WHERE code=?", (code,)).fetchone()
        now_iso = datetime.now().isoformat(timespec="seconds")
        if row:
            old_sh, old_cost = int(row[0]), float(row[1] or 0.0)
            new_sh = old_sh + shares
            new_cost = _r2((old_cost * old_sh + fees["gross"]) / new_sh)
            conn.execute(
                "UPDATE position SET shares=?, cost=?, updated_at=? WHERE code=?",
                (new_sh, new_cost, now_iso, code))
        else:
            new_sh, new_cost = shares, _r2(fees["gross"] / shares)
            conn.execute(
                "INSERT INTO position (code, name, shares, avail_shares, cost, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (code, name, new_sh, 0, new_cost, now_iso))  # T+1：当日新买 avail=0

        order_id = "PAPER-%s" % datetime.now().strftime("%Y%m%d%H%M%S%f")
        td = trade_date or date.today().isoformat()
        tid = repo.insert_trade(
            conn, trade_date=td, code=code, name=name, side="buy", price=exec_price,
            shares=shares, amount=fees["amount"], order_id=order_id,
            decision_id=decision_id, confirmed_by=confirmed_by, created_at=now_iso)
        conn.commit()
        res = {"ok": True, "trade_id": tid, "order_id": order_id,
               "code": code, "name": name, "side": "buy", "price": exec_price,
               "requested_price": price,
               "slippage_bps": float(self.cfg.get("slippage_bps", 0) or 0),
               "shares": shares, "trade_date": td,
               "cash_before": cash_before, "cash_after": _r2(cash_before - fees["amount"]),
               "gross": fees["gross"], "commission": fees["commission"],
               "stamp_tax": 0.0, "amount": fees["amount"]}
        log.info("buy 成交 trade#%s %s %s x%d @%.2f（委托 %.2f）金额=%.2f 佣金=%.2f"
                 " 现金 %.2f→%.2f",
                 res["trade_id"], code, name, shares, exec_price, price,
                 fees["amount"], fees["commission"], cash_before, res["cash_after"])
        return res

    def sell(self, conn: sqlite3.Connection, code: str, name: str, price: float, shares: int,
             decision_id: Optional[int] = None, confirmed_by: Optional[str] = None,
             trade_date: Optional[str] = None) -> Optional[dict]:
        """卖出：前置防线 → 校验 avail_shares >= shares（T+1）→ position 减持 → 写 trade。

        成交价 = 委托价 ×（1−滑点bps）；拒绝时返回 None。
        """
        shares = int(shares)
        price = float(price)
        if shares <= 0 or price <= 0:
            log.warning("sell 拒绝：非法参数 code=%s price=%s shares=%s", code, price, shares)
            return None
        try:
            self._pre_trade_guards(conn, code, "sell", price, shares, decision_id)
        except _Reject as e:
            log.warning("sell 拒绝：%s code=%s", e, code)
            return None
        row = conn.execute(
            "SELECT name, shares, avail_shares FROM position WHERE code=?", (code,)).fetchone()
        if not row:
            log.warning("sell 拒绝：%s 无持仓", code)
            return None
        total, avail = int(row[1]), int(row[2])
        if shares > avail:
            log.warning("sell 拒绝：%s 委托卖出 %d 股 > 可卖 %d 股（T+1）", code, shares, avail)
            return None
        if shares > total:
            log.warning("sell 拒绝：%s 委托卖出 %d 股 > 持股 %d（数据异常）", code, shares, total)
            return None
        exec_price = self._exec_price("sell", price)
        fees = compute_fees("sell", exec_price, shares, self.cfg)
        if fees["amount"] <= 0:
            log.warning("sell 拒绝：%s 净入账 %.2f 非正，放弃", code, fees["amount"])
            return None

        cash_before = self.cash(conn)
        remaining, remain_avail = total - shares, avail - shares
        now_iso = datetime.now().isoformat(timespec="seconds")
        if remaining > 0:
            conn.execute(
                "UPDATE position SET shares=?, avail_shares=?, updated_at=? WHERE code=?",
                (remaining, remain_avail, now_iso, code))
        else:
            conn.execute("DELETE FROM position WHERE code=?", (code,))

        order_id = "PAPER-%s" % datetime.now().strftime("%Y%m%d%H%M%S%f")
        td = trade_date or date.today().isoformat()
        tid = repo.insert_trade(
            conn, trade_date=td, code=code, name=name, side="sell", price=exec_price,
            shares=shares, amount=fees["amount"], order_id=order_id,
            decision_id=decision_id, confirmed_by=confirmed_by, created_at=now_iso)
        conn.commit()
        res = {"ok": True, "trade_id": tid, "order_id": order_id,
               "code": code, "name": name or row[0], "side": "sell", "price": exec_price,
               "requested_price": price,
               "slippage_bps": float(self.cfg.get("slippage_bps", 0) or 0),
               "shares": shares, "trade_date": td,
               "cash_before": cash_before, "cash_after": _r2(cash_before + fees["amount"]),
               "gross": fees["gross"], "commission": fees["commission"],
               "stamp_tax": fees["stamp_tax"], "amount": fees["amount"]}
        log.info("sell 成交 trade#%s %s %s x%d @%.2f（委托 %.2f）金额=%.2f 佣金=%.2f"
                 " 印花税=%.2f 现金 %.2f→%.2f", res["trade_id"], code, res["name"],
                 shares, exec_price, price, fees["amount"], fees["commission"],
                 fees["stamp_tax"], cash_before, res["cash_after"])
        return res

    # ------------------------------------------------------------ 回读校验

    def readback(self, conn: sqlite3.Connection, trade_id: int) -> dict:
        """下单回读：重读 trade 行并重算费用，重放全部有效流水核对现金与持仓。

        - 金额断言：amount == 重算（price×shares ± 佣金/印花税），误差 ≤0.01；
        - 现金断言：按重算金额重放的现金 == 按落库金额重放的现金（捕获任何 amount 篡改）；
        - 持仓断言：重放持股 == position 行；avail == 持股 − 最近活动日买入股数（T+1）；
          成本 == Σ买入gross/Σ买入股数。
        不一致时写 risk_event(rule='readback') 并返回 {ok: False, detail}。
        """
        row = conn.execute(
            "SELECT id, trade_date, code, name, side, price, shares, amount, order_id,"
            " status, decision_id FROM trade WHERE id=?", (int(trade_id),)).fetchone()
        if not row:
            msg = "readback 失败：trade#%s 不存在" % trade_id
            log.error(msg)
            return {"ok": False, "detail": msg}
        tid, tdate, code, name, side, price, shares, amount, order_id, status, did = row
        shares = int(shares)
        problems: List[str] = []
        if status != "filled":
            problems.append("status=%s 非 filled" % status)
        if side not in ("buy", "sell"):
            problems.append("side=%s 非法" % side)
        if price is None or float(price) <= 0 or shares <= 0:
            problems.append("价格/数量非法 price=%s shares=%s" % (price, shares))
            return self._readback_fail(conn, tid, did, problems)

        fees = compute_fees(side, float(price), shares, self.cfg)
        if abs(fees["amount"] - float(amount)) > 0.01:
            problems.append("金额不一致：落库 %.2f ≠ 重算 %.2f（佣金 %.2f 印花税 %.2f）"
                            % (float(amount), fees["amount"], fees["commission"],
                               fees["stamp_tax"]))

        # ---- 全量重放：现金 + 该票持仓 ----
        replay_cash = self.start_cash
        ledger_cash = self.start_cash
        n_eff = 0
        buy_sh = buy_gross = 0
        last_day: Optional[str] = None
        same_day_buy = 0
        for (r_tdate, r_side, r_price, r_shares, r_amount, r_status) in \
                repo.effective_trades(conn, code):
            n_eff += 1
            f = compute_fees(r_side, float(r_price), int(r_shares), self.cfg)
            if r_side == "buy":
                replay_cash -= f["amount"]
                ledger_cash -= float(r_amount)
                buy_sh += int(r_shares)
                buy_gross += f["gross"]
            else:
                replay_cash += f["amount"]
                ledger_cash += float(r_amount)
            if last_day is None or str(r_tdate) >= last_day:
                if last_day is not None and str(r_tdate) != last_day:
                    same_day_buy = 0
                last_day = str(r_tdate)
                if r_side == "buy":
                    same_day_buy += int(r_shares)
        if abs(replay_cash - ledger_cash) > 0.01 * max(1, n_eff):
            problems.append("现金流水不一致：按价格/数量重放 %.2f ≠ 按落库金额重放 %.2f"
                            % (replay_cash, ledger_cash))

        # ---- 持仓行核对 ----
        exp_shares = buy_sh - self._sold_shares(conn, code)
        pos = conn.execute(
            "SELECT shares, avail_shares, cost FROM position WHERE code=?", (code,)).fetchone()
        if exp_shares <= 0:
            if pos is not None:
                problems.append("持仓应已清空但仍存在：shares=%s" % (pos[0],))
        else:
            if pos is None:
                problems.append("应有持仓 %d 股但 position 无行" % exp_shares)
            else:
                if int(pos[0]) != exp_shares:
                    problems.append("持股不一致：position=%s ≠ 流水重放 %d"
                                    % (pos[0], exp_shares))
                exp_avail = exp_shares - same_day_buy
                if int(pos[1]) != exp_avail:
                    problems.append("可卖不一致：position=%s ≠ %d（T+1 扣除当日买入 %d）"
                                    % (pos[1], exp_avail, same_day_buy))
                exp_cost = buy_gross / buy_sh if buy_sh else 0.0
                if pos[2] is None or abs(float(pos[2]) - exp_cost) > 0.01:
                    problems.append("成本不一致：position=%s ≠ 重放 %.4f"
                                    % (pos[2], exp_cost))

        if problems:
            return self._readback_fail(conn, tid, did, problems)

        detail = ("trade#%s 回读一致：%s %s %s x%d @%.2f 金额=%.2f（佣金 %.2f 印花税 %.2f），"
                  "现金=%.2f，持仓 %d 股（可卖 %d，成本 %.2f）"
                  % (tid, tdate, side, code, shares, float(price), fees["amount"],
                     fees["commission"], fees["stamp_tax"], ledger_cash,
                     exp_shares if exp_shares > 0 else 0,
                     (exp_shares - same_day_buy) if exp_shares > 0 else 0,
                     (buy_gross / buy_sh if buy_sh else 0.0)))
        log.info(detail)
        return {"ok": True, "detail": detail, "trade_id": int(tid),
                "cash": _r2(ledger_cash), "expected": fees}

    def _sold_shares(self, conn: sqlite3.Connection, code: str) -> int:
        return repo.sold_shares(conn, code)

    def _readback_fail(self, conn: sqlite3.Connection, trade_id: int,
                       decision_id: Optional[int], problems: List[str]) -> dict:
        detail = "readback 失败 trade#%s：%s" % (trade_id, "; ".join(problems))
        log.error(detail)
        record_event(conn, "readback", detail, decision_id)
        return {"ok": False, "detail": detail, "trade_id": int(trade_id)}

    # ------------------------------------------------------------ 组合快照

    def portfolio(self, conn: sqlite3.Connection
                  ) -> Tuple[float, Dict[str, dict], float, Dict[str, float]]:
        """返回 (现金, 持仓dict, 总权益, 昨收盘dict)，供风控上下文组装。

        - 持仓 dict：{code: {name, shares, avail_shares, cost}}（shares>0）；
        - 总权益 = 现金 + Σ shares×最新收盘（无行情退回成本价）；
        - 昨收盘：stock_info ∪ 持仓 的每个 code 取“最新前一交易日”收盘。
        """
        cash = self.cash(conn)
        positions: Dict[str, dict] = {}
        for (code, name, shares, avail, cost) in conn.execute(
                "SELECT code, name, shares, avail_shares, cost FROM position"
                " WHERE shares > 0 ORDER BY code").fetchall():
            positions[code] = {"name": name or code, "shares": int(shares),
                               "avail_shares": int(avail), "cost": float(cost or 0.0)}
        codes = set(positions)
        codes.update(repo.all_codes(conn))
        prev_closes: Dict[str, float] = {}
        total_equity = cash
        for code in sorted(codes):
            pc = self.prev_close(conn, code)
            if pc is not None:
                prev_closes[code] = pc
            if code in positions:
                lp = self.latest_price(conn, code)
                if lp is None:
                    lp = positions[code]["cost"]
                total_equity += positions[code]["shares"] * float(lp)
        return _r2(cash), positions, _r2(total_equity), prev_closes
