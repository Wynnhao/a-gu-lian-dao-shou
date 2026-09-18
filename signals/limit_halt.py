"""Fix-4（D1）：规则 21 跌停应急主动扫描器 + confirm 优先超时兜底。

复审 TOP 5 #1：规则 21 只在 LLM 主动提交跌停价卖单时被动响应，LLM 不主动卖
时连续跌停死锁依然成立。本模块主动对 position 表逐票判定三条件（跌停价 /
封单比 / 浮亏≥止损线，复用 risk.engine._limit_halt_condition），命中即生成
emergency sell decision（price=跌停价, shares=avail_shares）走 propose 入库。

执行路径（用户决策 D1 + 审查补丁批 Fix F）：
- 盘后扫描生成应急单（run_date=次日）→ 当晚 macOS 通知 → 睡前 confirm 则次日
  09:15 自动执行；
- 未 confirm → 次日盘前 09:00 再通知一次；
- 09:14 仍未 confirm：execution.emergency_direct_exec=true 时
  runner.confirm(confirmed_by="emergency_timeout_failsafe") 自动执行（confirm
  内置风控复跑与 ±2% 价格校验）；默认 false 时只再提醒一次人工处理，不自动
  成交（自动兜底跟随总开关，与规则21 直写同一政策）。

stuck 联动：limit_halt_stuck 表同票首日 insert、后续 +1（不再命中即解除删除）；
5 日 → risk_event('limit_halt_stuck') + 通知人工；同日 ≥3 只 stuck → 触发
规则 5 kill_switch（risk.engine.apply_kill_switch，既有人工 resume 链）。

审计链：emergency_scan（扫描生成）→ emergency_timeout_failsafe（超时自动）/
人工（confirm 时填真人名）→ trade 表可完整归因。
"""
import logging
import logging.handlers
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from data import repo  # noqa: E402  （TRADE_EFFECTIVE_SQL：有效成交唯一定义）

log = logging.getLogger("signals.limit_halt")
if not log.handlers:
    from common.logsetup import rotating_handler  # AGSICKLE_LOG_DIR 逃生门（Sprint4 W0-1）
    log.addHandler(rotating_handler("signal.log"))
    log.addHandler(logging.StreamHandler())
    log.setLevel(logging.INFO)
log.propagate = False

STUCK_EVENT_DAYS = 5      # 连续 stuck 达 5 日 → risk_event + 通知
STUCK_KILL_COUNT = 3      # 同日 ≥3 只 stuck → kill_switch
FAILSAFE_TIME = (9, 14)   # 盘前 09:14 超时兜底线


def scan_positions(conn: sqlite3.Connection, ctx_inputs: Optional[dict] = None,
                   cfg: Optional[dict] = None,
                   now: Optional[datetime] = None) -> List[dict]:
    """对 position 表每只票判规则 21 三条件，命中 → emergency sell decision dict。

    ctx_inputs 缺省字段现读（positions/prev_close 取 PaperBroker.portfolio +
    daily_bar 最新收盘；atr_pct 取 regime.latest_atr_pct；live_quotes 盘后无
    实时行情 → 空 dict，条件②按"缺数据不阻断"处理）。
    返回 decision dict 列表（含 code/order/emergency_scan 标记）。
    """
    from execution import runner as _runner
    from risk.engine import RiskContext, _limit_halt_condition
    from risk.regime import latest_atr_pct

    now = now or datetime.now()
    cfg = cfg if cfg is not None else _runner.CFG.get("risk", {})
    ctx_inputs = dict(ctx_inputs or {})
    positions = ctx_inputs.get("positions")
    broker = None
    if positions is None:
        broker = _runner.PaperBroker()
        _, positions, _, _ = broker.portfolio(conn)
    codes = sorted(positions)
    if not codes:
        return []
    prev_close = ctx_inputs.get("prev_close")
    latest_prices = ctx_inputs.get("latest_prices")
    live_quotes = ctx_inputs.get("live_quotes") or {}
    atr_pct = ctx_inputs.get("atr_pct")
    if atr_pct is None:
        atr_pct = latest_atr_pct(conn, codes)
    # 调用方显式给了现价（如盘中传入实时快照）→ 缺价票不再回退日线收盘补齐：
    # 盘中日线最新 bar 是昨日收盘，回退补齐会把"缺实时价"伪装成"昨收价"，
    # 条件①退化为检测昨日跌停（审查补丁批 Fix B）。缺价票走 lp=None 保守跳过。
    lp_from_input = latest_prices is not None
    if prev_close is None or latest_prices is None:
        # 现读：昨收=前一日收盘、现价=最新收盘（盘后/禁实时行情口径）
        broker = broker or _runner.PaperBroker()
        prev_close = dict(prev_close or {})
        latest_prices = dict(latest_prices or {})
        for code in codes:
            if code not in prev_close:
                pc = broker.prev_close(conn, code,
                                       on_date=(now or datetime.now()).strftime("%Y-%m-%d"))
                if pc is not None:
                    prev_close[code] = float(pc)
            if not lp_from_input and code not in latest_prices:
                lp = broker.latest_price(conn, code, live=False)
                if lp is not None:
                    latest_prices[code] = float(lp)
    ctx = RiskContext(
        now=now, positions=positions,
        cash=0.0, total_equity=0.0,
        latest_prices=latest_prices, prev_close=prev_close,
        atr_pct=atr_pct, live_quotes=live_quotes)
    out: List[dict] = []
    for code in codes:
        pos = positions.get(code) or {}
        avail = int(pos.get("avail_shares", 0) or 0)
        if avail <= 0:
            continue  # T+1 不可卖，应急单无意义
        hit, info = _limit_halt_condition(code, ctx, cfg)
        if not hit:
            continue
        down = info["down"]
        # P0-1：条件①——当日确实跌停才生成应急单。_limit_halt_condition 只算 down
        # 不比价（共用函数注释明示"由调用方比对 order.price == down"），而扫描器
        # 自造 order.price=down 会让该比对恒真、条件①形同虚设，三条件实际只剩
        # "浮亏≥止损线"。此处显式校验现价：缺失或高于跌停价 0.5% 以上（未跌停）
        # → 跳过，避免把任意破线持仓误判成连续跌停应急单（原缺陷会进而误触
        # stuck 计数与全账户 kill_switch）。容差保底一分钱（down<2 元时 0.5%
        # 不足 1 分，覆盖不了数据源 1 分钱精度误差）。
        lp = latest_prices.get(code)
        tol = max(down * 0.005, 0.01)
        if lp is None or lp > down + tol:
            continue
        out.append({
            "action": "sell",
            "code": code,
            "target_weight": 0.0,
            "confidence": 1.0,
            "reasons": [
                "规则21主动扫描：跌停封死且浮亏 %.1f%% ≥ 止损线 %.1f%%"
                % (info["loss"] * 100, info["stop_line"] * 100),
                ("封单比 %.2f%%（W-A4②：条件②按实时快照判定）"
                 % (info["seal_ratio"] * 100))
                if info.get("seal_ratio") is not None
                else "条件②缺数据（快照无 ask1_vol/float_mv），按'不阻断'处理",
                "连续跌停应急：次日 09:15 集合竞价挂跌停价卖出",
            ],
            "risk_notes": [
                "emergency_scan 自动生成（D1：confirm 优先，09:14 未确认自动执行）",
            ],
            "order": {"side": "sell", "price": float(down), "shares": avail},
            "emergency_scan": True,
        })
    return out


def _dedupe_emergency(conn: sqlite3.Connection, code: str, run_date: str) -> bool:
    """同票同 run_date 已有 emergency_scan sell 单 → True（幂等，不重复生成）。

    W-A5③（P1-4）：判重范围收紧为 status IN ('proposed','approved') 或已有
    有效成交——此前 `status != 'rejected'` 把 expired 也算"已存在"，应急单被
    跨日闸门作废后同 run_date 永不再生（链路死锁的第三环）。
    """
    n = conn.execute(
        "SELECT COUNT(*) FROM decision WHERE code=? AND action='sell'"
        " AND emergency_scan=1 AND run_date=? AND (status IN ('proposed','approved')"
        " OR EXISTS (SELECT 1 FROM trade t WHERE t.decision_id=decision.id AND "
        + repo.TRADE_EFFECTIVE_SQL + "))",
        (code, run_date)).fetchone()[0]
    return n > 0


def run_postclose_scan(conn: Optional[sqlite3.Connection] = None,
                       now: Optional[datetime] = None,
                       run_date: Optional[str] = None) -> dict:
    """盘后入口（postclose 步骤 3.5）：扫描 → propose 入库（run_date=次日）→ 通知。

    propose 跑风控：approved → pending 落盘等人工 confirm；被拒/重复跳过留痕。
    返回 {"scanned": N, "proposed": [decision_id...], "skipped": [code...]}。
    """
    from execution import runner as _runner
    from risk.notify import notify

    own = conn is None
    c = conn
    if own:
        from data.fetcher import get_conn
        c = get_conn()
    now = now or datetime.now()
    # run_date = 预期执行日（次日交易日近似取自然日次日；跨日闸门按 run_date==today 放行，
    # 节假日场景由 premarket 兜底的"run_date=今天才执行"语义自然顺延）
    run_date = run_date or (now + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        # W-A4②：盘后自拉实时快照（腾讯/东财盘后仍可取，postclose 无现成快照可复用）
        # ——live_quotes 供条件②封单比；price/prev_close 供条件①现价校验与跌停价
        # 基准（当日跌停价的正确基准是快照自带昨收）。拉取失败/被禁用 → 空注入，
        # scan_positions 回退日线收盘口径（与旧行为一致，缺价票保守跳过）。
        ctx_inputs: Optional[dict] = None
        try:
            import os as _os
            if _os.environ.get("AGSICKLE_DISABLE_LIVE_QUOTES") != "1":
                from data.quotes import get_live_prices as _glp
                _broker = _runner.PaperBroker()
                _, _positions0, _, _ = _broker.portfolio(c)
                _codes0 = sorted(_positions0)
                live0: Dict[str, dict] = _glp(_codes0, force=True) if _codes0 else {}
                if live0:
                    ctx_inputs = {"live_quotes": live0}
                    _lp = {str(k): float(v["price"]) for k, v in live0.items()
                           if v.get("price") is not None}
                    _pc = {str(k): float(v["prev_close"]) for k, v in live0.items()
                           if v.get("prev_close") is not None}
                    if _lp:
                        ctx_inputs["latest_prices"] = _lp
                    if _pc:
                        ctx_inputs["prev_close"] = _pc
        except Exception as e:  # noqa: BLE001
            log.warning("盘后实时快照拉取失败（回退日线收盘口径）: %r", e)
            ctx_inputs = None
        decisions = scan_positions(c, ctx_inputs=ctx_inputs, now=now)
        proposed, skipped = [], []
        for d in decisions:
            code = d["code"]
            if _dedupe_emergency(c, code, run_date):
                skipped.append(code)
                continue
            v = _runner.propose(c, d, decision_id=None, run_date=run_date, now=now)
            if v.approved:
                proposed.append(code)
                log.warning("跌停应急扫描单已生成：%s（decision run_date=%s，"
                            "confirm 优先%s）",
                            code, run_date,
                            "，%02d:%02d 未确认自动执行" % FAILSAFE_TIME
                            if _runner.CFG.get("execution", {}).get(
                                "emergency_direct_exec", False)
                            else "（自动兜底停用，需人工确认）")
            else:
                skipped.append(code)
        c.commit()
        if proposed:
            notify("跌停应急扫描：发现 %d 只连续跌停票" % len(proposed),
                   "；".join(proposed)
                   + "。已生成应急卖单（sleep 前 confirm 可次日 09:15 执行；"
                     + ("否则次日 %02d:%02d 自动执行" % FAILSAFE_TIME
                        if _runner.CFG.get("execution", {}).get(
                            "emergency_direct_exec", False)
                        else "自动兜底已停用，请人工确认）"))
        return {"scanned": len(decisions), "proposed": proposed,
                "skipped": skipped}
    finally:
        if own:
            c.close()


def update_stuck(conn: sqlite3.Connection, hit_codes: List[str],
                 today: str) -> dict:
    """更新 limit_halt_stuck：命中票首日 insert / 后续 +1；未命中票解除删除。

    返回 {"hit": [...], "removed": [...], "days": {code: stuck_days}}。
    """
    rows = {r[0]: int(r[1] or 0) for r in conn.execute(
        "SELECT code, stuck_days FROM limit_halt_stuck").fetchall()}
    days: Dict[str, int] = {}
    for code in hit_codes:
        if code in rows:
            conn.execute(
                "UPDATE limit_halt_stuck SET stuck_days=stuck_days+1,"
                " last_attempt=? WHERE code=?", (today, code))
            days[code] = rows[code] + 1
        else:
            conn.execute(
                "INSERT OR REPLACE INTO limit_halt_stuck"
                " (code, first_stuck_date, stuck_days, last_attempt)"
                " VALUES (?,?,1,?)", (code, today, today))
            days[code] = 1
    removed = [c for c in rows if c not in set(hit_codes)]
    for code in removed:
        conn.execute("DELETE FROM limit_halt_stuck WHERE code=?", (code,))
    conn.commit()
    return {"hit": list(hit_codes), "removed": removed, "days": days}


def enforce_stuck_rules(conn: sqlite3.Connection,
                        now: Optional[datetime] = None) -> dict:
    """stuck 联动：单票 5 日 → risk_event + 通知；同日 ≥3 只 → kill_switch。

    返回 {"events": [code...], "kill": bool}。
    """
    from risk.engine import apply_kill_switch, record_event
    from risk.notify import notify

    now = now or datetime.now()   # kill_until 计算需要；此前 now=None 且 ≥3 只时 TypeError
    # 去重按真实自然日（risk_event.ts 由 record_event 用系统时间写），
    # 不用调用方注入的 now——回放/测试时间与落库时间不一致会击穿去重
    real_today = datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT code, first_stuck_date, stuck_days FROM limit_halt_stuck"
        " ORDER BY stuck_days DESC").fetchall()
    events, kill = [], False
    for code, first, days in rows:
        if int(days or 0) >= STUCK_EVENT_DAYS:
            n = conn.execute(
                "SELECT COUNT(*) FROM risk_event WHERE rule='limit_halt_stuck'"
                " AND ts LIKE ? AND detail LIKE ?", (real_today + "%",
                                                     f"%{code}%")).fetchone()[0]
            if n == 0:
                record_event(conn, "limit_halt_stuck",
                             "%s 连续跌停 stuck %s 日（自 %s），请人工介入"
                             % (code, days, first))
                events.append(code)
    # W-A4③（P1-7）：首日触板（first_stuck_date==今日）不计入 kill 聚集判定——
    # "连续跌停死封"要求至少进入第二个 stuck 日；此前 len(rows)>=3 未过滤首日行，
    # 叠加旧字段错位时"触碰跌停即 stuck"，3 只同日首触即假触发 72h 全账户 kill。
    kill_rows = [r for r in rows if str(r[1] or "") != real_today]
    if len(kill_rows) >= STUCK_KILL_COUNT:
        kill_until = now + timedelta(hours=72)
        apply_kill_switch(conn, kill_until,
                          note="跌停应急扫描：同日 %d 只 stuck（%s），触发规则5 kill"
                               % (len(kill_rows), ",".join(r[0] for r in kill_rows)))
        kill = True
        notify("跌停应急扫描触发 kill",
               "同日 %d 只票 stuck（%s），已停机 72h（人工 resume）"
               % (len(kill_rows), ",".join(r[0] for r in kill_rows)))
    elif events:
        notify("连续跌停预警",
               "；".join("%s 已 stuck 5 日" % c for c in events) + "，请人工介入")
    conn.commit()
    return {"events": events, "kill": kill}


def run_intraday_scan(conn: sqlite3.Connection,
                      now: Optional[datetime] = None,
                      latest_prices: Optional[Dict[str, float]] = None,
                      prev_close: Optional[Dict[str, float]] = None,
                      live_quotes: Optional[Dict[str, dict]] = None) -> dict:
    """盘中入口（intraday_check 14:50）：扫描 → stuck 计数 → 联动规则。

    盘中不生成新应急单（当日可卖窗口太小、且 premarket 兜底链路只在盘前），
    只做 stuck 计数与 kill/预警联动。

    Fix B：latest_prices 传当日实时快照（intraday_check 已拉取），prev_close
    传快照自带昨收——当日跌停价基准是**昨日收盘**，盘中日线最新 bar 恰好就是
    昨日收盘，若让 scan_positions 走 DB 回退会取 offset=1 的**前日**收盘，跌停价
    基准错位一天。两图缺省时回退日线收盘口径（= 检测昨日的跌停，仅供回放/测试）；
    快照整体为空（行情失败）时本轮跳过 stuck 更新——既不计数也不清除
    （宁可漏计一日，不可假 kill / 假解除）。
    W-A4②：live_quotes 由 intraday_check 传入同一实时快照——规则21 条件②
    封单比（ask1_vol/float_mv）在扫描器路径此前恒缺数据。
    """
    if latest_prices is None or prev_close is None:
        log.warning("run_intraday_scan 未传实时快照，回退日线收盘口径"
                    "（盘中=昨日跌停检测，仅供回放/测试）")
        hit = [d["code"] for d in scan_positions(conn, now=now)]
        st = update_stuck(conn, hit, (now or datetime.now()).strftime("%Y-%m-%d"))
    elif not latest_prices:
        log.warning("实时快照为空（行情失败？），本轮跳过 stuck 更新（不计数也不清除）")
        st = {"hit": [], "removed": [], "days": {}}
        hit = []
    else:
        ctx_inputs = {"latest_prices": dict(latest_prices),
                      "prev_close": dict(prev_close),
                      "live_quotes": dict(live_quotes or {})}
        hit = [d["code"] for d in scan_positions(conn, ctx_inputs=ctx_inputs,
                                                 now=now)]
        st = update_stuck(conn, hit, (now or datetime.now()).strftime("%Y-%m-%d"))
    ef = enforce_stuck_rules(conn, now=now)
    return {"hit": hit, "stuck": st, "enforce": ef}


def premarket_failsafe(conn: Optional[sqlite3.Connection] = None,
                       now: Optional[datetime] = None,
                       do_exec: Optional[bool] = None) -> dict:
    """盘前步骤 0.5（D1 核心）：昨日未 confirm 的应急单超时兜底。

    查 run_date=今天 的 emergency_scan 单且 status 仍 approved：
    - 09:14 之前（约 09:00 premarket 触发）：仅 macOS 通知"N 笔待确认"；
    - 09:14 及之后：仍未 confirm → execution.emergency_direct_exec=true 时
      runner.confirm(confirmed_by="emergency_timeout_failsafe") 自动执行
      （confirm 内置风控复跑与价格校验）；开关默认 false → 只再提醒一次
      人工 confirm/reject，不自动成交（Fix F：自动兜底跟随总开关，与规则21
      直写同一政策，恪守"绝不自动成交"）。
    do_exec：显式覆盖时间窗判定（测试用）；缺省按时间窗判断。开关独立于
    do_exec——false 时无论如何不自动执行。
    """
    from execution import runner as _runner
    from risk.notify import notify

    own = conn is None
    c = conn
    if own:
        from data.fetcher import get_conn
        c = get_conn()
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    try:
        rows = c.execute(
            "SELECT id, code FROM decision WHERE emergency_scan=1"
            " AND status='approved' AND run_date=? ORDER BY id",
            (today,)).fetchall()
        out = {"pending": [str(r[1]) for r in rows],
               "executed": [], "notified": False}
        if not rows:
            return out
        direct_exec = bool(
            _runner.CFG.get("execution", {}).get("emergency_direct_exec", False))
        if do_exec is None:
            do_exec = now.time() >= datetime.strptime(
                "%02d:%02d" % FAILSAFE_TIME, "%H:%M").time()
        if do_exec and not direct_exec:
            notify("跌停应急单待人工确认",
                   "%d 笔（%s）已过 %02d:%02d：自动兜底已停用"
                   "（execution.emergency_direct_exec=false），请人工 confirm/reject"
                   % (len(rows), "、".join(out["pending"]), *FAILSAFE_TIME))
            log.warning("应急单超时兜底降级为只通知（emergency_direct_exec=false）")
            out["notified"] = True
            return out
        if do_exec:
            for did, code in rows:
                res = _runner.confirm(c, int(did),
                                      confirmed_by="emergency_timeout_failsafe",
                                      now=now)
                if res is not None:
                    out["executed"].append(code)
                    log.warning("跌停应急单超时兜底执行：decision#%s %s"
                                "（confirmed_by=emergency_timeout_failsafe）",
                                did, code)
                else:
                    log.warning("跌停应急单超时兜底未成交：decision#%s %s"
                                "（风控复跑未通过，人工需跟进）", did, code)
        else:
            notify("跌停应急单待确认",
                   "%d 笔（%s）%s，请尽快人工确认"
                   % (len(rows), "、".join(out["pending"]),
                      ("将于 %02d:%02d 自动执行" % FAILSAFE_TIME) if direct_exec
                      else "需人工 confirm（自动兜底已停用）"))
            out["notified"] = True
        return out
    finally:
        if own:
            c.close()
