"""执行编排：决策落库 → 风控裁决 → 人工闸门 → paper 成交 → 回读校验 → 状态落库。

流程：
- build_context：从 position/portfolio_state/trade/daily_bar/黑名单/健康检查组装 RiskContext；
- propose：跑 risk check，decision.status → approved/rejected/report_only；manual_gate 开启时
  不直接成交，写 logs/orders/<date>/pending_<decision_id>.json 等待人工确认；
- confirm：重跑风控后 PaperBroker.buy/sell 成交 + readback 回读，status → executed；
- reject：人工否决；kill_trigger 联动：kill_orders 逐条 paper 卖出 + apply_kill_switch。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import argparse
import contextlib
import fcntl
import json
import logging
import logging.handlers
import os
import re
import sqlite3
from dataclasses import asdict
from datetime import date, datetime, timedelta, time as dtime
from typing import Any, Dict, List, Optional, Tuple

from common.config import snapshot
from common.market import in_trading_session
from data import repo
from data.fetcher import get_conn
from risk.blacklist import check_blacklist, health_check
from risk.engine import (RiskContext, Verdict, check, record_event, apply_kill_switch,
                         flush_events)
from risk.notify import notify
from execution.paper import PaperBroker, compute_fees

CFG = snapshot()  # 统一配置层：import 期冻结（set_gate 原地 mutate 的测试手法保持可用）
ORDERS_DIR = Path(os.environ.get("AGSICKLE_ORDERS_DIR") or (BASE / "logs" / "orders"))
STATE_DIR = Path(os.environ.get("AGSICKLE_STATE_DIR") or (BASE / "logs" / "state"))
KILL_STATE_FILE = STATE_DIR / "kill.json"

log = logging.getLogger("exec.runner")
log.setLevel(logging.INFO)
log.propagate = False
if not log.handlers:
    from common.logsetup import rotating_handler  # AGSICKLE_LOG_DIR 逃生门（Sprint4 W0-1）
    _fh = rotating_handler("exec.log")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_fh)


# ---------------------------------------------------------------- 模式守卫与并发锁

def _assert_paper_mode() -> None:
    """mode=ui 硬拒绝：UI 自动化的成交回执补录/截图回写/账本对账三件事都还没实现，
    切过去会让本地账本与同花顺账户脱节、风控基于错误账本裁决——代码层直接挡住，
    不靠 runbook 文字约束。确需演练时设 AGSICKLE_ALLOW_UI=1。"""
    if CFG.get("execution", {}).get("mode") == "ui" \
            and os.environ.get("AGSICKLE_ALLOW_UI") != "1":
        raise SystemExit(
            "[runner] execution.mode=ui 未就绪（缺 UI 成交回执补录/截图回写/账本对账），"
            "已硬性拒绝。演练请设 AGSICKLE_ALLOW_UI=1 并按 execution/runbook_ths.md 执行。")


# P2-21（W-D7）：进程内持锁深度——flock 对同一进程的不同 fd 会自锁（LOCK_EX
# 阻塞等待自己），锁下沉到函数内后，CLI 外层锁 + 函数内锁必须可重入。
_EXEC_LOCK_DEPTH = {"n": 0}


@contextlib.contextmanager
def _exec_lock():
    """执行入口互斥锁（P2-21 改造：**可重入**，并下沉到 propose/confirm/_do_kill
    函数内——此前锁只在 CLI main() 外层，limit_halt failsafe / intraday_check
    程序化调用 propose/confirm 完全绕锁，trade 计数→成交之间存在 TOCTOU 窗口）。

    跨进程互斥语义不变：cron/catchup/人工/看板四方并发时阻塞等待（SQLite 写
    并发另有 busy_timeout 兜底，但计数→成交的原子性只能靠进程锁）。
    重入语义：同进程已持锁（深度>0，如 CLI 外层已锁、一批决策逐条 propose）
    时降级为深度计数，不再 flock；非重入实现会让函数内加锁在 CLI 路径自锁。
    """
    if _EXEC_LOCK_DEPTH["n"] > 0:
        _EXEC_LOCK_DEPTH["n"] += 1
        try:
            yield
        finally:
            _EXEC_LOCK_DEPTH["n"] -= 1
        return
    ORDERS_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = ORDERS_DIR / ".lock"
    with open(lock_path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        _EXEC_LOCK_DEPTH["n"] = 1
        try:
            yield
        finally:
            _EXEC_LOCK_DEPTH["n"] = 0
            fcntl.flock(f, fcntl.LOCK_UN)


# ---------------------------------------------------------------- kill 状态文件

def read_kill_state() -> Optional[dict]:
    """logs/state/kill.json：{"active": bool, "until": iso, "note": str}。

    存在时为权威状态（支持人工 resume/extend）；不存在时回退 risk_event 推导
    （兼容旧库）。此前靠 rule LIKE '%kill%' 推导停机期，任何含 kill 的新规则名
    都会改变停机推导，且无法人工提前恢复。
    """
    try:
        return json.loads(KILL_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_kill_state(until: Optional[datetime], note: str = "",
                     dd_base_equity: Optional[float] = None,
                     clear_dd_base: bool = False,
                     extra: Optional[dict] = None,
                     now: Optional[datetime] = None) -> None:
    """合并式写 kill.json（Sprint4 W-A3①）：只覆盖本函数拥有的键
    （active/until/note/updated_at），未知键（dd_base/dd_base_date/lifetime_peak/
    kill_count 等）原样保留——此前整文件覆写会把 dd_base 抹掉，下一次
    resume/extend 后回撤基准复位即失效（P1-1 kill 回撤死锁）。

    - dd_base_equity：写入/覆盖 dd_base + dd_base_date（now 当日）——kill 全部
      清仓完成后按清仓后权益重置回撤基准（"分段 8%" 语义，用户已拍板）；
    - clear_dd_base：移除 dd_base 键（递延场景下旧基准已失效，
      待 resolve_liquidations 补写）；
    - extra：额外合并键（lifetime_peak/kill_count 等累计观测口径）；
    - now：dd_base_date/updated_at 的时钟基准（回放/测试可注入，缺省墙钟）。
    原子写：tmp + rename（半写文件不再可能被 read_kill_state 读到）。
    """
    now = now or datetime.now()
    try:
        existing = json.loads(KILL_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            existing = {}
    except (OSError, ValueError):
        existing = {}
    payload = dict(existing)
    payload.update({"active": until is not None,
                    "until": until.isoformat(timespec="seconds") if until else None,
                    "note": note,
                    "updated_at": now.isoformat(timespec="seconds")})
    if dd_base_equity is not None:
        payload["dd_base"] = round(float(dd_base_equity), 2)
        payload["dd_base_date"] = now.strftime("%Y-%m-%d")
    if clear_dd_base:
        payload.pop("dd_base", None)
        payload.pop("dd_base_date", None)
    if extra:
        payload.update(extra)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.chmod(0o755)
    tmp = KILL_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(KILL_STATE_FILE)


def _update_kill_state_extras(extra: dict, clear_dd_base: bool = False,
                              now: Optional[datetime] = None) -> None:
    """不改停机语义（active/until/note 沿用现值）地合并 extra 键——
    resolve_liquidations 递延补写 dd_base 用（W-A3③）。"""
    ks = read_kill_state() or {}
    until: Optional[datetime] = None
    if ks.get("active") and ks.get("until"):
        try:
            until = datetime.fromisoformat(str(ks["until"]))
        except (TypeError, ValueError):
            until = None
    write_kill_state(until, str(ks.get("note") or ""), clear_dd_base=clear_dd_base,
                     extra=extra, now=now)


def effective_peak(conn: sqlite3.Connection, before: Optional[str] = None,
                   window: int = 250) -> Optional[float]:
    """回撤峰值的唯一定径（W-A3⑤ 统一口径）：
    - kill.json 带 dd_base_date（分段 8% 语义）：只看 dd_base_date（含）之后的
      portfolio_state 峰值——清仓重置前的历史峰值不再参与回撤判定；
    - 否则：最近 window 行窗口峰值（一条坏数据不永久抬高峰值）；
    - before 供日报回放口径（mark_to_market 只看该日之前）。
    clamp（max 当前权益）由调用方做——峰值至少不低于当前权益。"""
    ks = read_kill_state() or {}
    base = ks.get("dd_base_date")
    if base:
        return repo.peak_total(conn, after=str(base), before=before)
    return repo.peak_total(conn, window=window, before=before)


def kill_resume(conn: sqlite3.Connection, reason: str = "") -> dict:
    """人工提前解除停机（写 active=false 覆盖事件推导），留痕 risk_event。"""
    write_kill_state(None, "resume: %s" % (reason or "人工解除"))
    record_event(conn, "kill_resume", "人工解除停机：%s" % (reason or "未填写理由"))
    log.info("kill 停机已人工解除: %s", reason)
    return read_kill_state() or {}


def kill_extend(conn: sqlite3.Connection, hours: float, reason: str = "") -> dict:
    """人工延长停机。"""
    cur = read_kill_state() or {}
    base = cur.get("until")
    new_until = datetime.now() + timedelta(hours=hours)
    if base:
        try:
            new_until = max(new_until, datetime.fromisoformat(str(base)))
        except ValueError:
            pass
    write_kill_state(new_until, "extend: %s" % (reason or "人工延长"))
    record_event(conn, "kill_extend", "人工延长停机 %s 小时（至 %s）：%s"
                 % (hours, new_until.strftime("%Y-%m-%d %H:%M"), reason))
    return read_kill_state() or {}


# ---------------------------------------------------------------- 上下文组装

def build_context(conn: sqlite3.Connection, now: datetime,
                  live_quotes_override: Optional[Dict[str, dict]] = None) -> RiskContext:
    """组装风控上下文。

    - today_trades = trade 表当日 filled+submitted 计数；
    - week_turnover = 近5交易日（daily_bar 最近5个交易日）有效成交额合计 / total_equity；
    - peak_equity = max(回撤窗口峰值, 当前 total_equity)；窗口为 dd_base 感知
      （W-A3⑤：kill.json 带 dd_base_date 时只看该日之后，否则 250 行窗口）；
    - kill_switch_until = kill.json（权威）；文件缺失回退 risk_event 推导；
    - blacklist / health_issues 来自 risk.blacklist；
    - live_quotes_override（W-A7）：调用方自带实时快照（midday force 拉取）——
      跳过交易时段门控直接采用，并连带重算 total_equity（只改 latest_prices
      不改 equity 则回撤照样失真）；
    - price_source（W-A9）：每票价格口径 "live"/"stale_close"，规则9 与
      confirm 定价据此识别"昨收冒充实价"。
    """
    broker = PaperBroker()
    cash, positions, total_equity, prev_closes = broker.portfolio(conn)

    codes = set(positions)
    for c in repo.all_codes(conn):
        codes.add(str(c))
    # 实时行情：交易时段批量拉一次（闭市/禁用/失败自动回退日线收盘）
    live_quotes: Dict[str, dict] = {}
    import os
    if live_quotes_override is not None:
        live_quotes = {str(k): v for k, v in live_quotes_override.items()
                       if isinstance(v, dict)}
    elif (os.environ.get("AGSICKLE_DISABLE_LIVE_QUOTES") != "1"
            and CFG.get("execution", {}).get("use_live_prices", True)):
        try:
            from data.quotes import get_live_prices, is_trading_time
            if is_trading_time(now):
                live_quotes = get_live_prices(sorted(codes))
        except Exception as e:  # noqa: BLE001
            # 盯市退化必须有痕可查：否则止损/回撤类风控基于昨收价失真时无任何线索
            log.warning("实时行情获取失败，风控盯市退回昨收价: %s", e)
            live_quotes = {}
    latest_prices: Dict[str, float] = {}
    price_source: Dict[str, str] = {}   # W-A9：live / stale_close
    for code in sorted(codes):
        q = live_quotes.get(code)
        if q and q.get("price") is not None:
            latest_prices[code] = float(q["price"])
            price_source[code] = "live"
        else:
            lp = broker.latest_price(conn, code, live=False)
            if lp is not None:
                latest_prices[code] = lp
                price_source[code] = "stale_close"
    for code, q in live_quotes.items():  # 实时昨收补齐（新股仅1根bar时日线推不出）
        if code not in prev_closes and q.get("prev_close") is not None:
            prev_closes[code] = float(q["prev_close"])
    if live_quotes_override is not None:
        # W-A7：override 连带重算 total_equity——持仓价以 override 为权威，
        # 缺价票退回成本价（与 paper.portfolio 同款回退）
        total_equity = cash
        for code, p in positions.items():
            lp = latest_prices.get(code)
            total_equity += p["shares"] * float(lp if lp is not None else p["cost"])
        total_equity = round(total_equity, 2)

    today = now.strftime("%Y-%m-%d")
    today_trades = int(conn.execute(
        "SELECT COUNT(*) FROM trade WHERE trade_date=? AND status IN ('filled','submitted')",
        (today,)).fetchone()[0])
    today_sold = {r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM trade WHERE trade_date=? AND side='sell' AND "
        + repo.TRADE_EFFECTIVE_SQL, (today,)).fetchall()}

    # 近5交易日换手：daily_bar 最近5日 ∪ 今天（当日 bar 盘后才入库，
    # 漏掉今天会低估当日换手、放行超限交易）
    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM daily_bar ORDER BY trade_date DESC LIMIT 5").fetchall()]
    if today not in days:
        days.append(today)
    week_amount = 0.0
    if days:
        ph = ",".join("?" * len(days))
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0.0) FROM trade WHERE side IN ('buy','sell')"
            " AND status='filled' AND trade_date IN (%s)" % ph, days).fetchone()
        week_amount = float(row[0] or 0.0)
    week_turnover = (week_amount / total_equity) if total_equity > 0 else 0.0

    # 峰值（W-A3⑤ 统一口径）：dd_base 感知窗口 + clamp——kill 清仓重置后走
    # "分段 8%"（dd_base 之后窗口），否则最近 250 行；一条坏数据/测试 seed 行
    # 不再永久抬高峰值导致误 kill 或回撤锁死
    peak_equity = max(float(effective_peak(conn) or 0.0), float(total_equity))

    # kill 停机期：logs/state/kill.json 为权威（支持人工 resume/extend），
    # 文件缺失时回退 risk_event 白名单推导（kill_switch/kill_manual/kill_extend）
    kill_until: Optional[datetime] = None
    hours = float(CFG.get("risk", {}).get("kill_stop_hours", 72))
    ks = read_kill_state()
    if ks is not None:
        if ks.get("active") and ks.get("until"):
            try:
                kill_until = datetime.fromisoformat(str(ks["until"]))
            except ValueError:
                log.warning("kill 状态文件时间无法解析: %r", ks.get("until"))
    else:
        ev = conn.execute(
            "SELECT ts FROM risk_event WHERE rule IN "
            "('kill_switch','kill_manual','kill_extend') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if ev and ev[0]:
            try:
                kill_until = datetime.fromisoformat(str(ev[0])) + timedelta(hours=hours)
            except ValueError:
                log.warning("kill 事件时间戳无法解析: %r", ev[0])

    # 新规则上下文：最新日线成交额（流动性约束）与概念标签（集中度约束）
    day_amount: Dict[str, float] = {str(c): float(a or 0) for c, a in conn.execute(
        "SELECT code, amount FROM daily_bar WHERE trade_date IN "
        "(SELECT MAX(trade_date) FROM daily_bar GROUP BY code) AND amount IS NOT NULL"
    ).fetchall()}
    code_concepts: Dict[str, List[str]] = {}
    wl_codes = set()
    for w in CFG.get("watchlist", []):
        wl_codes.add(str(w["code"]))
        for t in (w.get("concepts") or []):
            code_concepts.setdefault(str(w["code"]), []).append(str(t))

    # 市场环境总闸（regime/波动率目标 → 动态总仓位上限）与持仓 ATR（ATR 自适应止损）。
    # 两者 fail-open：计算失败不阻断交易，engine 退回静态上限/固定止损线。
    position_cap: Optional[float] = None
    atr_pct: Dict[str, float] = {}
    ctx_notes: List[str] = []
    try:
        from risk import regime as _regime
        cap_info = _regime.position_cap(conn, CFG)
        position_cap = cap_info.get("cap")
        if positions:
            atr_pct = _regime.latest_atr_pct(conn, positions.keys())
    except Exception as e:  # noqa: BLE001
        # C-ARC-4（T2）：fail-open 照旧不阻断，但留痕便签挂 ctx——由 propose/confirm
        # 在 flush_events 时转成 failopen_regime_cap 事件落库（此前完全无迹可查）
        note = "regime/vol cap 计算失败，fail-open 无动态闸: %s" % repr(e)[:120]
        log.warning("%s", note)
        ctx_notes.append(note)

    return RiskContext(
        now=now,
        positions=positions,
        cash=cash,
        total_equity=total_equity,
        latest_prices=latest_prices,
        prev_close=prev_closes,
        today_trades=today_trades,
        week_turnover=week_turnover,
        peak_equity=peak_equity,
        kill_switch_until=kill_until,
        blacklist=check_blacklist(conn),
        health_issues=health_check(conn),
        day_amount=day_amount,
        code_concepts=code_concepts,
        today_sold_codes=today_sold,
        watchlist_codes=wl_codes,
        position_cap=position_cap,
        atr_pct=atr_pct,
        ctx_notes=ctx_notes,
        live_quotes=live_quotes,   # W-A4②：此前恒为空 dict，规则21 条件②从未算过
        price_source=price_source,  # W-A9：昨收冒充实价的口径标记
    )


# ---------------------------------------------------------------- decision 行工具

def _insert_decision(conn: sqlite3.Connection, decision: dict, run_date: str,
                     trade_date: Optional[str] = None) -> int:
    """决策落库（status=proposed），input_snapshot 存决策 JSON 全文，返回新 id。

    P2-25（W-D7）：trade_date 缺省取 run_date（预期执行日，与 ai/decide.py
    "trade_date=预期执行日" 口径一致）——此前 propose --file 路径恒写 NULL 且
    永不回填，backfill_decision_outcomes 的 `trade_date IS NOT NULL` 过滤把
    这些决策永久排除在决策→结果闭环统计外。
    """
    decision_id = repo.insert_decision(conn, decision, run_date,
                                       trade_date=trade_date or run_date)
    conn.commit()
    return decision_id


def _parse_json_list(raw: Any) -> List[str]:
    return repo.parse_json_list(raw)


def _decision_from_row(row: tuple) -> dict:
    """decision 行 → 决策 dict。order 优先取 input_snapshot 中的 JSON（ai.decide 落库时
    input_snapshot 可能是 bundle 全文，故按 code+action 匹配查找）。

    W-A5①：行扩为 11 列（末列 emergency_scan），应急单标志以 **DB 列为准**——
    input_snapshot 缺 flag 的历史行/手工行也能在 confirm 重建 dict 时恢复，
    规则4/14/18 的豁免判定不再依赖快照 JSON 是否完整。
    """
    (did, run_date, code, action, tw, conf, reasons, risk_notes, snapshot, status,
     emergency_scan) = row
    d: Dict[str, Any] = {
        "action": action, "code": code, "target_weight": tw, "confidence": conf,
        "reasons": _parse_json_list(reasons), "risk_notes": _parse_json_list(risk_notes),
    }
    if emergency_scan:
        d["emergency_scan"] = True
    order = None
    if snapshot:
        try:
            snap = json.loads(snapshot)
        except (TypeError, ValueError):
            snap = None
        candidates: List[dict] = []
        if isinstance(snap, dict):
            if "decisions" in snap and isinstance(snap["decisions"], list):
                candidates = [x for x in snap["decisions"] if isinstance(x, dict)]
            elif "action" in snap:
                candidates = [snap]
        elif isinstance(snap, list):
            candidates = [x for x in snap if isinstance(x, dict)]
        for c in candidates:
            if str(c.get("code")) == str(code) and str(c.get("action")) == str(action) \
                    and isinstance(c.get("order"), dict):
                order = c["order"]
                # Fix-4：带回应急单标志——规则 4/14/18 的 emergency_scan 豁免判定
                # 依赖这些键，丢失会让 09:14 兜底 confirm 在非交易时段被拒
                # （emergency_scan 现以 DB 列为准，快照缺 flag 不影响）
                for _flag in ("emergency_scan", "emergency_pending_skip"):
                    if c.get(_flag) is not None:
                        d[_flag] = c[_flag]
                # C-ARC 补修1（docs/Sprint4-全库审查修复计划-2026-09-19.md 落地核验①，
                # 即 W-A2②）：kill_liquidation 只随卖单恢复（收紧口径）——
                # resolve_liquidations 构造的补清算单 flag 此前在 confirm 重建决策
                # dict 时丢失，规则5 停机豁免与 T4 熔断豁免在 confirm 侧双双失效
                # （propose 侧直接读输入 dict 不受影响）
                if action == "sell" and c.get("kill_liquidation") is not None:
                    d["kill_liquidation"] = c["kill_liquidation"]
                # 审查补丁批 Fix A：skip_gate/confirmed_by 一律不从输入恢复——
                # snapshot 存的是 LLM 原始 JSON，恢复它们等于允许决策输入自带
                # "免闸门直写"标志（含 buy 也能直写、审计字段可伪造）。合法应急单
                # 的 skip_gate 由规则 21 在 propose 的 check() 内重新置位。
                if c.get("skip_gate") is not None or c.get("confirmed_by") is not None:
                    log.warning("决策#%s 输入携带 skip_gate/confirmed_by，已剥离"
                                "（仅规则21 可设置，防注入）", did)
                break
    if order is not None:
        d["order"] = order
    return d


def _get_decision(conn: sqlite3.Connection, decision_id: int) -> Optional[Tuple[tuple, dict]]:
    row = repo.get_decision_row(conn, decision_id)
    if not row:
        return None
    return row, _decision_from_row(row)


def _set_status(conn: sqlite3.Connection, decision_id: int, status: str) -> None:
    conn.execute("UPDATE decision SET status=? WHERE id=?", (status, decision_id))
    conn.commit()


def _code_name(conn: sqlite3.Connection, code: str, decision: dict) -> str:
    if decision.get("name"):
        return str(decision["name"])
    row = conn.execute("SELECT name FROM stock_info WHERE code=?", (code,)).fetchone()
    if row and row[0]:
        return str(row[0])
    row = conn.execute("SELECT name FROM position WHERE code=?", (code,)).fetchone()
    return str(row[0]) if row and row[0] else code


# ---------------------------------------------------------------- 人工闸门文件

def _verdict_dict(v: Verdict) -> dict:
    d = asdict(v)
    if d.get("kill_until") is not None:
        d["kill_until"] = d["kill_until"].isoformat(timespec="seconds")
    return d


def _pending_path(date_str: str, decision_id: int,
                  orders_dir: Optional[Path] = None) -> Path:
    base = Path(orders_dir) if orders_dir else ORDERS_DIR
    return base / date_str / ("pending_%d.json" % decision_id)


def _write_pending(conn: sqlite3.Connection, decision_id: int, decision: dict, v: Verdict,
                   now: datetime, orders_dir: Optional[Path] = None,
                   run_date: Optional[str] = None) -> Path:
    """闸门开启时把待确认单写 logs/orders/<date>/pending_<decision_id>.json。

    valid_until=**run_date 当日** 15:05（W-A5②：与 confirm 的 TTL 闸门同一口径——
    应急单 run_date=次日，文件不再写创建日导致"生成即过期"的字面歧义）。
    """
    order = v.adjusted_order if v.adjusted_order is not None else (decision.get("order") or {})
    valid_until = (run_date or now.strftime("%Y-%m-%d")) + "T15:05:00"
    payload = {
        "decision_id": decision_id,
        "created_at": now.isoformat(timespec="seconds"),
        "valid_until": valid_until,
        "status": "pending",
        "decision": decision,
        "verdict": _verdict_dict(v),
        "submit_price": order.get("price"),
        "suggest_shares": order.get("shares"),
        "confirm_hint": "python3 execution/runner.py confirm --decision-id %d [--by 姓名] [--price 价格]" % decision_id,
        "reject_hint": "python3 execution/runner.py reject --decision-id %d --reason \"...\"" % decision_id,
    }
    path = _pending_path(now.strftime("%Y-%m-%d"), decision_id, orders_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("pending 单已写入 %s（有效至 %s）", path, valid_until)
    notify("待确认单 decision#%d" % decision_id,
           "%s %s %s @%s x%s，有效至 15:05"
           % (decision.get("action"), decision.get("code"), decision.get("name", ""),
              order.get("price"), order.get("shares")))
    return path


def _remove_pending(decision_id: int, orders_dir: Optional[Path] = None) -> int:
    """confirm/reject 后清掉对应 pending 文件，返回删除数。"""
    base = Path(orders_dir) if orders_dir else ORDERS_DIR
    n = 0
    if base.exists():
        for p in base.glob("*/pending_%d.json" % decision_id):
            p.unlink()
            n += 1
    return n


def list_pending(orders_dir: Optional[Path] = None) -> List[Path]:
    base = Path(orders_dir) if orders_dir else ORDERS_DIR
    if not base.exists():
        return []
    return sorted(base.glob("*/pending_*.json"))


# ---------------------------------------------------------------- kill 联动

def _do_kill(conn: sqlite3.Connection, v: Verdict, decision_id: Optional[int],
             now: datetime) -> None:
    """kill 触发入口（P2-21：函数内持执行锁，可重入——propose/confirm 路径
    已在锁内时降级为计数）。"""
    with _exec_lock():
        _do_kill_locked(conn, v, decision_id, now)


def _do_kill_locked(conn: sqlite3.Connection, v: Verdict, decision_id: Optional[int],
                    now: datetime) -> None:
    """kill 触发：kill_orders 逐条走 PaperBroker.sell + apply_kill_switch，如实打印。

    卖出失败的单此前只留痕不重试、T+1 不可卖的票直接被跳过——残仓在停机期被锁死。
    现在：失败单与 T+1 递延票统一写 risk_event(kill_liquidation_pending)，
    盘前流水线调 resolve_liquidations() 自动补清算。
    """
    broker = PaperBroker()
    td = now.strftime("%Y-%m-%d")
    print("[KILL] kill switch 触发：清仓 %d 笔 + 停机至 %s"
          % (len(v.kill_orders),
             v.kill_until.strftime("%Y-%m-%d %H:%M") if v.kill_until else "-"))
    deferred = list(v.kill_pending or [])
    trade_ids: List[int] = []
    for ko in v.kill_orders:
        # W-A1（P0-4）：每笔清仓单 decision_id=None——多笔共用同一 id 会被
        # trade(decision_id) 唯一索引（idx_trade_decision_uniq）在第 2 笔 INSERT 时
        # 抛 IntegrityError（不在 _pre_trade_guards 的 try 内→kill 中途崩溃、
        # kill.json 未写）；partial unique index 对 NULL 不生效。血缘由
        # confirmed_by="kill_switch" + 下方 kill_executed 汇总事件（C-ARC H2：
        # detail 含根 decision_id + trade_id 列表，为血缘唯一载体）保留。
        res = broker.sell(conn, str(ko["code"]), str(ko.get("name") or ko["code"]),
                          float(ko.get("price") or 0), int(ko.get("shares") or 0),
                          decision_id=None, confirmed_by="kill_switch",
                          trade_date=td)
        if res is None:
            msg = "kill 清仓失败：%s" % json.dumps(ko, ensure_ascii=False)
            record_event(conn, "kill_sell_failed", msg, decision_id)
            print("[KILL] 清仓失败：%s" % msg)
            deferred.append(str(ko["code"]))
            continue
        trade_ids.append(int(res["trade_id"]))
        rb = broker.readback(conn, res["trade_id"])
        print("[KILL] 已清仓 %s %s x%d @%.2f 金额=%.2f 回读ok=%s"
              % (res["code"], res["name"], res["shares"], res["price"],
                 res["amount"], rb["ok"]))
    for code in dict.fromkeys(deferred):  # 去重保序
        pos = conn.execute(
            "SELECT name, shares FROM position WHERE code=?", (code,)).fetchone()
        if not pos:
            continue  # 已无持仓（例如同批 kill 单已清掉）
        record_event(conn, "kill_liquidation_pending",
                     json.dumps({"code": code, "name": pos[0], "shares": int(pos[1])},
                                ensure_ascii=False),
                     decision_id)
        print("[KILL] %s T+1/失败递延，已列入次日补清算（%d 股）" % (code, pos[1]))
    # W-A3③④：回撤基准重置 + 累计观测口径。
    # 全部清仓完成（无失败/递延，position 表已空）→ 按清仓后权益写 dd_base
    # （分段 8%：resume 后不再被清仓前历史峰值立即重新 kill）；
    # 有递延 → 清掉旧 dd_base（基准失效），由 resolve_liquidations 补清仓完成后补写。
    # lifetime_peak/kill_count 持续累计（日报/risk_event 观测全史口径，
    # 不随分段重置丢失熔断史——用户已拍板接受"分段 8%"语义）。
    ks_prev = read_kill_state() or {}
    extra = {
        "kill_count": int(ks_prev.get("kill_count") or 0) + 1,
        "lifetime_peak": round(max(float(ks_prev.get("lifetime_peak") or 0.0),
                                   float(repo.peak_total(conn) or 0.0)),
                               2),
    }
    if deferred:
        write_kill_state(v.kill_until, "decision#%s 触发" % decision_id,
                         clear_dd_base=True, extra=extra, now=now)
    else:
        post_equity = broker.portfolio(conn)[2]
        write_kill_state(v.kill_until, "decision#%s 触发" % decision_id,
                         dd_base_equity=post_equity, extra=extra, now=now)
        record_event(conn, "kill_dd_base_reset",
                     "kill 清仓完成，回撤基准重置为清仓后权益 %.2f（dd_base_date=%s，"
                     "分段 8%% 口径）" % (post_equity, td), decision_id)
    # C-ARC-4（T2）：kill 执行汇总留痕（一条；逐笔与 trade 表 100% 冗余——
    # trade 行天然带 decision_id + confirmed_by='kill_switch'，ADR-0 §2）。
    # H2（sprint4-carc-conflicts）：W-A1 后 trade.decision_id 可能为 NULL，
    # 本事件升级为 kill 血缘唯一载体——detail 必须显式含触发决策 id + trade_id 列表。
    record_event(conn, "kill_executed",
                 json.dumps({"trigger_decision_id": decision_id,
                             "sells": len(trade_ids), "trade_ids": trade_ids,
                             "deferred": len(dict.fromkeys(deferred))},
                            ensure_ascii=False),
                 decision_id)
    apply_kill_switch(conn, v.kill_until, note="decision#%s 触发" % decision_id)
    # kill.json 已在上方 dd_base 分支合并式写入（active/until/note + dd_base/累计键）
    notify("⚠️ KILL SWITCH 触发", "回撤熔断：清仓 %d 笔，停机至 %s%s"
           % (len(v.kill_orders),
              v.kill_until.strftime("%m-%d %H:%M") if v.kill_until else "-",
              "，递延补清算 %d 只" % len(deferred) if deferred else ""))
    print("[KILL] kill switch 已落库（portfolio_state.kill_switch=1 + kill.json + 留痕）")


def resolve_liquidations(conn: sqlite3.Connection, now: Optional[datetime] = None,
                         orders_dir: Optional[Path] = None) -> int:
    """补清算：扫描未完成的 kill_liquidation_pending 事件，对仍有持仓的票
    自动 propose 一条 kill_liquidation 卖单（走正常风控，停机期也放行）。

    判定"未完成"：事件之后该票没有足额的 filled 卖出流水。返回补清算单数。
    """
    now = now or datetime.now()
    rows = conn.execute(
        "SELECT id, ts, detail FROM risk_event WHERE rule='kill_liquidation_pending' "
        "ORDER BY id").fetchall()
    n = 0
    cleared = 0      # 本轮扫描发现"已清偿"的事件数（事件行永存，不删）
    for eid, ts, detail in rows:
        try:
            info = json.loads(detail)
        except (TypeError, ValueError):
            continue
        code = str(info.get("code") or "")
        shares = int(info.get("shares") or 0)
        if not code or shares <= 0:
            continue
        sold = conn.execute(
            "SELECT COALESCE(SUM(shares),0) FROM trade WHERE code=? AND side='sell' "
            "AND status='filled' AND created_at > ?", (code, ts)).fetchone()[0]
        pos = conn.execute(
            "SELECT name, shares, cost FROM position WHERE code=?", (code,)).fetchone()
        if not pos or int(pos[1]) <= 0 or sold >= shares:
            cleared += 1
            continue  # 已清仓完成
        # W-A9 定价同规则：盘前实时价不可用 → 显式取日线最新收盘（陈旧口径）
        # 作为委托价——该价随 decision 落快照为"设计执行价"，confirm 侧按
        # kill_liquidation 显式价路径执行并留痕，不会出现"自动以昨收成交"的
        # 静默回退（execution 定价口径见 confirm 内 stale_price_* 事件）。
        price = PaperBroker().latest_price(conn, code, live=False) or 0.0
        if price <= 0:
            price = float(pos[2] or 0)
        if price <= 0:
            continue
        decision = {
            "action": "sell", "code": code, "name": pos[0],
            "target_weight": 0.0, "confidence": 1.0,
            "reasons": ["kill 递延补清算（T+1 解锁/上次卖出失败）"],
            "risk_notes": ["kill_switch 自动清算单",
                           "定价口径：日线最新收盘（盘前实时价不可用，陈旧基准已留痕）"],
            "kill_liquidation": True,
            "order": {"side": "sell", "price": price, "shares": int(pos[1])},
        }
        print("[liquidation] %s %s 补清算 %d 股 @%.2f" % (code, pos[0], int(pos[1]), price))
        propose(conn, decision, run_date=now.strftime("%Y-%m-%d"), now=now,
                orders_dir=orders_dir)
        n += 1
    # W-A3③：递延场景的 dd_base 补写——kill_liquidation_pending 事件在本轮全部
    # 清偿（有 cleared 或本轮 propose 的 n）、position 表已空，且 kill.json 处于
    # "kill 已发生但 dd_base 未重置"的待重置态 → 按当前权益重置回撤基准。
    # 覆盖两条路径：本轮 gate-off 直接成交（n>0），以及上一轮 pending 清算单此后
    # 被 confirm 成交（cleared>0、n=0）。gate-on 落 pending 时持仓未空 → 不写。
    # 幂等：写入后 dd_base_date 存在，后续 resolve 不再触发（仅日期更新才覆盖）。
    pos_left = conn.execute("SELECT COUNT(*) FROM position WHERE shares>0").fetchone()[0]
    ks = read_kill_state() or {}
    pending_reset = bool(ks.get("kill_count")) and not ks.get("dd_base_date")
    if (n or cleared) and not pos_left and pending_reset:
        eq = PaperBroker().portfolio(conn)[2]
        _update_kill_state_extras({"dd_base": round(eq, 2),
                                   "dd_base_date": now.strftime("%Y-%m-%d")},
                                  now=now)
        record_event(conn, "kill_dd_base_reset",
                     "kill 递延补清算全部完成，回撤基准重置为权益 %.2f"
                     "（dd_base_date=%s，分段 8%% 口径）"
                     % (eq, now.strftime("%Y-%m-%d")))
    return n


# ---------------------------------------------------------------- 执行级有限重挂（C-ARC-1/T3）与熔断判定（C-ARC-2/T4）
#
# taxonomy 依据 docs/ADR-0-执行失败taxonomy-F1a-F1b-F2.md：
# - F1a（confirm 重跑风控被拒，status=rejected、pending 已删）→ 本节自动重挂对象
# - F1b（执行价二次校验被拒，pending 保留）与 F2（broker 拒单，pending 保留）→ 不重挂
# - 熔断输入 = F1a ∪ F2 按根决策链归并

_RETRY_MARK = "exec_retry_of="   # reasons 标记：exec_retry_of=<根id>;attempt=<n>


def _retry_root(conn: sqlite3.Connection, decision_id: int) -> int:
    """重挂链根决策 id：本决策带 exec_retry_of 标记则归并到根，否则自身即根。"""
    row = conn.execute("SELECT reasons FROM decision WHERE id=?", (decision_id,)).fetchone()
    if row and row[0]:
        m = re.search(_RETRY_MARK + r"(\d+);attempt=", str(row[0]))
        if m:
            return int(m.group(1))
    return int(decision_id)


def _retry_attempts(conn: sqlite3.Connection, root: int, run_date: str) -> int:
    """当日链上既有重挂次数（reasons 含 exec_retry_of=<root>;attempt= 的决策数）。
    LIKE 用 ';attempt=' 锚定，root=5 不会误配 55。"""
    n = conn.execute(
        "SELECT COUNT(*) FROM decision WHERE run_date=? AND reasons LIKE ?",
        (run_date, "%" + _RETRY_MARK + "%d;attempt=%%" % root)).fetchone()[0]
    return int(n)


def exec_breaker_tripped(conn: sqlite3.Connection, today: Optional[str] = None,
                         threshold: Optional[int] = None) -> bool:
    """C-ARC-2/T4：执行失败熔断判定——当日 F1a ∪ F2 的**根决策**数 ≥ 阈值。

    不加状态文件：risk_event 落库推导，DB 天然跨进程、当日自动过期、可重放。
    根决策归并：F2 直接取 decision_id；F1a 经重挂链的按 exec_retry_of 回溯根 id。
    """
    today = today or datetime.now().strftime("%Y-%m-%d")
    if threshold is None:
        threshold = int(CFG.get("execution", {}).get("exec_breaker_threshold", 3))
    rows = conn.execute(
        "SELECT DISTINCT decision_id FROM risk_event WHERE rule IN "
        "('risk_check_reconfirm','execution_failed') AND ts LIKE ? "
        "AND decision_id IS NOT NULL", (today + "%",)).fetchall()
    roots = {_retry_root(conn, int(did)) for (did,) in rows}
    return len(roots) >= max(1, int(threshold))


def _record_once_today(conn: sqlite3.Connection, rule: str, detail: str,
                       prefix: Optional[str] = None,
                       decision_id: Optional[int] = None) -> bool:
    """同日同前缀只落一条（照 limit_halt real_today 口径：去重窗口按真实自然日，
    防回放注入时间击穿去重）。detail 必须以 prefix 开头（ADR-0 §3）。
    返回是否本次真正落库（调用方据此决定是否 notify，避免重复打扰）。"""
    prefix = prefix or detail
    real_today = datetime.now().strftime("%Y-%m-%d")
    n = conn.execute(
        "SELECT COUNT(*) FROM risk_event WHERE rule=? AND ts LIKE ? AND detail LIKE ?",
        (rule, real_today + "%", prefix + "%")).fetchone()[0]
    if not n:
        record_event(conn, rule, detail, decision_id)
        return True
    return False


def requeue_price_rejects(conn: sqlite3.Connection, now: Optional[datetime] = None,
                          orders_dir: Optional[Path] = None) -> int:
    """C-ARC-1/T3：当日 F1a「价格漂移型」拒单 → 按实时价重插决策、走完整 propose
    （风控 + pending），等人工 confirm。与 09-16 人工路径同构（#28 被价格保护拒
    → 人工按实时价重提 #31）：不自动成交、不继承原单确认状态、不覆盖 F1b/F2。

    判定式（施工方案 §2.T3）：rejected + risk_check_reconfirm + 无更晚同票同向决策
    + 实时价漂移 > 0.5×price_guard_pct + attempt < exec_retry_max + 累计漂移 <
    exec_retry_drift_max + 交易时段内且 14:55 前 + 交易日 + 熔断未触发。

    返回重挂数。由 intraday_check 步骤 3.7（14:50 cron 与 catchup 30 分钟扫描）调起。
    """
    now = now or datetime.now()
    exec_cfg = CFG.get("execution", {})
    max_retry = int(exec_cfg.get("exec_retry_max", 3))
    drift_max = float(exec_cfg.get("exec_retry_drift_max", 0.05))
    guard_pct = float(CFG.get("risk", {}).get("price_guard_pct", 0.02))
    today = now.strftime("%Y-%m-%d")

    # 时段闸：连续竞价内 + 14:55 前（给人工留 10 分钟；15:05 TTL 兜底）
    if not in_trading_session(now) or now.time() >= dtime(14, 55):
        return 0
    from data.trade_cal import is_trading_day
    if not is_trading_day(conn, now.date()):
        return 0
    if exec_breaker_tripped(conn, today):   # 熔断期不重挂（C-ARC-2）
        return 0

    broker = PaperBroker()
    # emergency_scan=0：规则21 应急单不参与重挂——limit_halt 有自己的 stuck 计数与
    # 次日再生成链，重挂掺和会干扰（sprint4-carc-conflicts 排查协同点②）
    rows = conn.execute(
        "SELECT d.id FROM decision d WHERE d.run_date=? AND d.status='rejected' "
        "AND d.action IN ('buy','sell') AND COALESCE(d.emergency_scan,0)=0 AND EXISTS ("
        "  SELECT 1 FROM risk_event e WHERE e.rule='risk_check_reconfirm' "
        "  AND e.decision_id=d.id) ORDER BY d.id", (today,)).fetchall()

    n = 0
    for (did,) in rows:
        got = _get_decision(conn, did)
        if not got:
            continue
        _row, decision = got
        code = str(decision.get("code") or "")
        action = str(decision.get("action") or "")
        order = decision.get("order") or {}
        try:
            old_price = float(order.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if not code or old_price <= 0:
            continue
        # 人工已重提（存在更晚的同 code+action 决策）→ 不掺和（09-16 #31 场景）
        later = conn.execute(
            "SELECT 1 FROM decision WHERE run_date=? AND code=? AND action=? "
            "AND id>? LIMIT 1", (today, code, action, did)).fetchone()
        if later:
            continue
        live = broker.latest_price(conn, code)
        if not live or live <= 0:
            continue
        drift = abs(live - old_price) / old_price
        if drift <= guard_pct * 0.5:
            continue   # 非价格漂移型拒单：重提无意义（原价仍贴市价）
        root = _retry_root(conn, did)
        attempts = _retry_attempts(conn, root, today)
        if attempts >= max_retry:
            # 链耗尽：止损卖单仍未成交必须响铃（并入 stop_loss 事件流，同日同票一条）
            if action == "sell":
                _record_once_today(
                    conn, "stop_loss_unfilled",
                    "stop_loss_unfilled: %s 止损卖单重挂链耗尽（attempt=%d ≥ %d，"
                    "根决策 #%d），仍未成交，请人工处置" % (code, attempts, max_retry, root),
                    prefix="stop_loss_unfilled: %s " % code, decision_id=did)
                notify("止损卖单未成交", "%s 重挂 %d 次仍未成交（根决策 #%d），请人工处置"
                       % (code, attempts, root))
            continue
        # 追价护栏：新价相对根决策原价累计漂移 ≥ exec_retry_drift_max → 放弃
        root_price = old_price
        if root != did:
            rg = _get_decision(conn, root)
            if rg:
                try:
                    root_price = float((rg[1].get("order") or {}).get("price") or old_price)
                except (TypeError, ValueError):
                    root_price = old_price
        cum = abs(live - root_price) / root_price if root_price > 0 else 0.0
        if cum >= drift_max:
            _record_once_today(
                conn, "exec_retry_skip",
                "exec_retry_skip: %s 累计漂移 %.2f%% ≥ 上限 %.0f%%（根 #%d 原 %.2f → "
                "现 %.2f），放弃追价" % (code, cum * 100, drift_max * 100,
                                 root, root_price, live),
                prefix="exec_retry_skip: %s " % code, decision_id=did)
            continue

        new_decision = dict(decision)
        new_order = dict(order)
        new_order["price"] = float(live)
        new_decision["order"] = new_order
        new_decision["reasons"] = list(decision.get("reasons") or []) + [
            "%s%d;attempt=%d（执行级重挂：原价 %.2f → 实时价 %.2f）"
            % (_RETRY_MARK, root, attempts + 1, old_price, live)]
        new_id = _insert_decision(conn, new_decision, today)
        v2 = propose(conn, new_decision, decision_id=new_id, now=now,
                     orders_dir=orders_dir)
        record_event(conn, "exec_retry",
                     json.dumps({"root": root, "attempt": attempts + 1,
                                 "old_price": old_price, "new_price": float(live),
                                 "new_decision_id": new_id,
                                 "approved": bool(v2.approved)},
                                ensure_ascii=False),
                     new_id)
        print("[requeue] decision#%d 已按实时价 %.2f 重提（根 #%d，attempt %d/%d）"
              % (new_id, live, root, attempts + 1, max_retry))
        n += 1
    return n


# ---------------------------------------------------------------- 核心编排

def _print_decision(d: dict, decision_id: Optional[int]) -> None:
    print("[propose] decision#%s %s %s %s target_weight=%s confidence=%s"
          % (decision_id, d.get("code"), d.get("action"), d.get("name", ""),
             d.get("target_weight"), d.get("confidence")))
    if isinstance(d.get("order"), dict):
        print("[propose] order: side=%s price=%s shares=%s"
              % (d["order"].get("side"), d["order"].get("price"), d["order"].get("shares")))
    for r in d.get("reasons", []) or []:
        print("[propose]   理由: %s" % r)


def _print_verdict(v: Verdict) -> None:
    print("[risk] %s" % v.brief())
    for x in v.violations:
        print("[risk]   [违规] %s" % x)
    for w in v.warnings:
        print("[risk]   [警告] %s" % w)


def _ctx_failopen_events(ctx: RiskContext) -> List[dict]:
    """build_context 的 fail-open 便签 → 待落库事件（C-ARC-4/T2）。
    detail 必须以 once_today_prefix 开头（ADR-0 §3），once_today 去重才命中。"""
    return [{"rule": "failopen_regime_cap",
             "detail": "failopen_regime_cap: %s" % note,
             "once_today_prefix": "failopen_regime_cap"}
            for note in (getattr(ctx, "ctx_notes", None) or [])]


def _dedupe_check(conn: sqlite3.Connection, decision: dict, run_date: str) -> bool:
    """propose --file 重跑幂等：同 run_date 同 code+action 且订单参数一致、
    状态未被否决的决策已存在 → 跳过（此前 catchup/人工重跑会重复入库并重复 propose）。"""
    order = decision.get("order") or {}
    rows = conn.execute(
        "SELECT id, input_snapshot, status FROM decision WHERE run_date=? AND code=? "
        "AND action=? AND status NOT IN ('rejected','expired')",
        (run_date, decision.get("code"), decision.get("action"))).fetchall()
    for _id, snap, _st in rows:
        try:
            old = json.loads(snap) if snap else {}
        except (TypeError, ValueError):
            continue
        old_order = old.get("order") if isinstance(old, dict) else None
        if isinstance(old_order, dict) and \
                float(old_order.get("price") or 0) == float(order.get("price") or 0) and \
                int(old_order.get("shares") or 0) == int(order.get("shares") or 0):
            return True
    return False


def propose(conn: sqlite3.Connection, decision: dict, decision_id: Optional[int] = None,
            run_date: Optional[str] = None, now: Optional[datetime] = None,
            orders_dir: Optional[Path] = None) -> Verdict:
    """跑风控并落库裁决结论；approved 且闸门开启 → 写 pending 等人工确认。

    P2-21（W-D7）：函数内持执行锁（可重入）——此前程序化调用（limit_halt 应急
    单 / intraday_check kill 安全网）绕过 CLI 外层锁，存在 TOCTOU 窗口。

    - decision_id 为 None 时先插入 decision 行（status=proposed，重复文件内容跳过）；
    - kill_trigger=True 时立即执行清仓 + apply_kill_switch（不受人工闸门约束）；
    - report_only / rejected 均不进入闸门。
    """
    with _exec_lock():
        return _propose_locked(conn, decision, decision_id=decision_id,
                               run_date=run_date, now=now, orders_dir=orders_dir)


def _propose_locked(conn: sqlite3.Connection, decision: dict,
                    decision_id: Optional[int] = None,
                    run_date: Optional[str] = None, now: Optional[datetime] = None,
                    orders_dir: Optional[Path] = None) -> Verdict:
    now = now or datetime.now()
    _assert_paper_mode()
    exec_cfg = CFG.get("execution", {})
    # C-ARC-2/T4：执行失败熔断挡板——当日 F1a∪F2 根决策数达阈值后暂停新 propose。
    # 豁免 kill 递延补清算（强平 > 熔断，CONSTRAINTS §3.3 强序）；补修1 收紧口径：
    # 只认卖单（resolve_liquidations 构造恒为 sell，畸形 buy 不得借 flag 绕熔断）。
    if not (decision.get("kill_liquidation") and decision.get("action") == "sell") \
            and exec_breaker_tripped(conn, now.strftime("%Y-%m-%d")):
        first = _record_once_today(
            conn, "exec_circuit_breaker",
            "exec_circuit_breaker: 当日执行失败根决策数达阈值 %d，暂停新 propose"
            "（kill 补清算豁免）" % int(exec_cfg.get("exec_breaker_threshold", 3)),
            prefix="exec_circuit_breaker")
        if first:
            notify("执行失败熔断生效",
                   "当日执行失败（拒单/成交失败）根决策数达阈值，已暂停新 propose；"
                   "请人工检查行情与数据后处理积压单")
        print("[propose] 执行失败熔断生效：暂停新 propose（risk_event 已留痕）")
        return Verdict(approved=False, warnings=["exec breaker"])
    # 审查补丁批 Fix A：skip_gate/confirmed_by 只能由规则 21 在下方 check() 内设置；
    # 任何调用方传入的这两个键一律剥离（防决策输入注入"免闸门直写"标志——
    # 否则 emergency_direct_exec=true 时自带 skip_gate 的 buy 也能直写成交）。
    for _k in ("skip_gate", "confirmed_by"):
        if decision.pop(_k, None) is not None:
            log.warning("propose 收到的决策携带 %s，已剥离（仅规则21 可设置）", _k)
    if decision_id is None:
        run_date = run_date or now.strftime("%Y-%m-%d")
        if _dedupe_check(conn, decision, run_date):
            print("[propose] 跳过重复决策：%s %s %s（同日同参数已存在）"
                  % (decision.get("code"), decision.get("action"),
                     (decision.get("order") or {}).get("price")))
            return Verdict(approved=False, warnings=["duplicate skipped"])
        decision_id = _insert_decision(conn, decision, run_date)
    ctx = build_context(conn, now)
    v = check(decision, ctx, CFG.get("risk", {}))

    _print_decision(decision, decision_id)
    _print_verdict(v)
    for x in v.violations:
        record_event(conn, "risk_check", x, decision_id)
    v.events.extend(_ctx_failopen_events(ctx))
    # P0-4：规则21/因子拥挤事件由调用方落库；C-ARC-4：warnings 以 code 前缀同日落库
    flush_events(conn, v, decision_id,
                 warn_prefix=str(decision.get("code") or ""))

    if v.kill_trigger:
        _do_kill(conn, v, decision_id, now)
        _set_status(conn, decision_id, "rejected")
        record_event(conn, "decision_killed", "决策因 kill 触发作废", decision_id)
        print("[propose] decision#%d 状态 -> rejected（kill 触发）" % decision_id)
        return v

    if v.approved:
        # P1-6：skip_gate（规则21 应急单）是否允许直写成交，由
        # execution.emergency_direct_exec 开关控制（默认 false → 仍走人工闸门，
        # 恪守"绝不自动成交"总原则）。manual_gate=false 的闸门全关模式保持原直写语义。
        gate_off = not exec_cfg.get("manual_gate", True)
        emergency_direct = bool(decision.get("skip_gate")) and \
            decision.get("action") == "sell" and \
            exec_cfg.get("emergency_direct_exec", False)
        direct_exec = gate_off or emergency_direct
        if decision.get("action") in ("hold", "watch"):
            _set_status(conn, decision_id, "approved")
            print("[propose] decision#%d 状态 -> approved（%s 无交易动作，无需确认）"
                  % (decision_id, decision.get("action")))
        elif not direct_exec:
            path = _write_pending(conn, decision_id, decision, v, now, orders_dir,
                                  run_date=run_date)
            _set_status(conn, decision_id, "approved")
            print("[gate] 人工闸门开启：待确认单 %s" % path)
            print("[gate] 等待人工确认 -> python3 execution/runner.py confirm"
                  " --decision-id %d" % decision_id)
        else:
            order = v.adjusted_order if v.adjusted_order is not None else decision["order"]
            if decision.get("skip_gate"):
                # P1-6：仅 emergency_direct_exec=true 时才走到这里（直写成交）
                print("[gate] 直写执行（emergency_direct_exec=true，skip_gate，rule=%s）：%s %s x%s"
                      % (decision.get("confirmed_by") or "emergency_rule21",
                         decision.get("code"), decision.get("action"),
                         (order or {}).get("shares")))
                confirmed_by = decision.get("confirmed_by") or "emergency_rule21"
            else:
                print("[gate] 人工闸门关闭：直接执行")
                confirmed_by = "auto"
            # gate-off / skip_gate 自动模式按实时价成交（此前直接用决策价，决策价与
            # 市价的偏差完全不被记录）
            price = PaperBroker().latest_price(conn, str(decision.get("code")))
            if price is None:
                price = float(order["price"])
            else:
                ref = float(order["price"])
                if ref > 0 and abs(price - ref) / ref > 0.01:
                    record_event(conn, "auto_price_drift",
                                 "decision#%d 决策价 %.2f → 自动成交价 %.2f（偏离 %.1f%%）"
                                 % (decision_id, ref, price, abs(price - ref) / ref * 100),
                                 decision_id)
            _execute(conn, decision_id, decision, float(price),
                     int(order["shares"]), confirmed_by=confirmed_by, now=now)
    else:
        status = "report_only" if v.report_only else "rejected"
        _set_status(conn, decision_id, status)
        print("[propose] decision#%d 状态 -> %s" % (decision_id, status))
    return v


def _execute(conn: sqlite3.Connection, decision_id: int, decision: dict, price: float,
             shares: int, confirmed_by: str, now: datetime) -> Optional[dict]:
    """PaperBroker 成交 + readback 回读 + decision.status=executed。

    成交失败 → decision 保持 approved 可重试；回读不一致 → status=executed_unverified
    （不可再 confirm，等人工核查；此前保持 approved 且 pending 已删，重跑 confirm
    会二次成交）。
    """
    broker = PaperBroker()
    code = str(decision.get("code"))
    name = _code_name(conn, code, decision)
    action = str(decision.get("action"))
    td = now.strftime("%Y-%m-%d")
    if action == "buy":
        res = broker.buy(conn, code, name, price, shares, decision_id=decision_id,
                         confirmed_by=confirmed_by, trade_date=td)
    else:
        res = broker.sell(conn, code, name, price, shares, decision_id=decision_id,
                          confirmed_by=confirmed_by, trade_date=td)
    if res is None:
        msg = "成交失败 decision#%d %s %s x%d @%.2f（现金不足/可卖不足/前置防线/参数非法）" % (
            decision_id, action, code, shares, price)
        record_event(conn, "execution_failed", msg, decision_id)
        notify("成交失败 decision#%d" % decision_id, msg)
        print("[exec] 失败：%s" % msg)
        return None
    rb = broker.readback(conn, res["trade_id"])
    print("[exec] 成交 trade#%d %s %s %s x%d @%.2f 金额=%.2f（佣金 %.2f 印花税 %.2f）"
          % (res["trade_id"], code, name, action, res["shares"], res["price"],
             res["amount"], res["commission"], res["stamp_tax"]))
    print("[readback] ok=%s %s" % (rb["ok"], rb["detail"]))
    if rb["ok"]:
        _set_status(conn, decision_id, "executed")
        print("[propose] decision#%d 状态 -> executed" % decision_id)
    else:
        _set_status(conn, decision_id, "executed_unverified")
        notify("回读不一致 decision#%d" % decision_id,
               "trade#%s 成交但账本回读失败，请人工核查 risk_event" % res["trade_id"])
        print("[readback] 不一致！decision#%d 状态 -> executed_unverified（不可再确认）"
              % decision_id)
    return res


def confirm(conn: sqlite3.Connection, decision_id: int, confirmed_by: str = "human",
            price_override: Optional[float] = None, now: Optional[datetime] = None,
            orders_dir: Optional[Path] = None) -> Optional[dict]:
    """人工确认执行单条 approved 决策：重跑风控 → 成交（price_override 或最新价）→ 回读。

    P2-21（W-D7）：函数内持执行锁（可重入）——limit_halt 09:14 failsafe 的
    程序化 confirm 此前完全绕锁。

    - 决策 run_date 与今天不一致 → 置 expired（emergency_scan 单且 run_date≥今日
      豁免——T 晚生成次日执行，W-A5②）；TTL 按 run_date 当日 15:05（非墙钟当日）；
    - 最终成交价（override/实时价）确定后，再对**执行价**重跑价格保护与涨跌停
      校验——此前重跑风控用的是决策原始价，--price 覆盖价可绕过全部价格类风控；
    - 实时价缺失时（W-A9）不得自动以昨收成交：应急/补清算单按其显式设计价执行
      并留痕（stale_price_exec），普通单挂起保留 approved 等 --price 显式确认。
    """
    with _exec_lock():
        return _confirm_locked(conn, decision_id, confirmed_by=confirmed_by,
                               price_override=price_override, now=now,
                               orders_dir=orders_dir)


def _confirm_locked(conn: sqlite3.Connection, decision_id: int,
                    confirmed_by: str = "human",
                    price_override: Optional[float] = None,
                    now: Optional[datetime] = None,
                    orders_dir: Optional[Path] = None) -> Optional[dict]:
    now = now or datetime.now()
    _assert_paper_mode()
    got = _get_decision(conn, decision_id)
    if not got:
        print("[confirm] decision#%d 不存在" % decision_id)
        return None
    row, decision = got
    if row[9] != "approved":
        print("[confirm] decision#%d 当前状态 %s，仅 approved 可确认" % (decision_id, row[9]))
        return None
    # 过期闸门（W-A5②）：决策是为某个交易日做的，跨日决策依据已失效——
    # 豁免：emergency_scan 单（run_date=预期执行日=次日，T 晚生成后允许跨日
    # 确认），但 run_date 已过（<今日）的陈旧应急单照常作废（可重新扫描生成）
    run_date = str(row[1] or "")
    is_emergency = bool(row[10]) or bool(decision.get("emergency_scan"))
    if run_date and run_date != now.strftime("%Y-%m-%d"):
        if not (is_emergency and run_date >= now.strftime("%Y-%m-%d")):
            _set_status(conn, decision_id, "expired")
            _remove_pending(decision_id, orders_dir)
            record_event(conn, "pending_expired",
                         "decision#%d run_date=%s 跨日确认被拒（今日 %s）"
                         % (decision_id, run_date, now.strftime("%Y-%m-%d")), decision_id)
            print("[confirm] decision#%d 状态 -> expired（决策日 %s ≠ 今日，需重新决策）"
                  % (decision_id, run_date))
            return None
        record_event(conn, "pending_crossday_allowed",
                     "decision#%d emergency_scan 单跨日放行（run_date=%s ≥ 今日 %s）"
                     % (decision_id, run_date, now.strftime("%Y-%m-%d")), decision_id)
        print("[confirm] decision#%d 应急单跨日放行（run_date=%s）" % (decision_id, run_date))
    # TTL 闸门（W-A5②）：有效期按 **run_date 当日 15:05**（而非墙钟当日）——
    # 应急单 run_date=次日，T 晚 22:00 confirm 仍在次日 15:05 之前 → 放行；
    # 此前按墙钟当日 15:05 判，T 晚 confirm 必被杀（P1-4 "睡前 confirm 即作废"）
    if run_date:
        try:
            deadline = datetime.combine(date.fromisoformat(run_date), dtime(15, 5))
        except ValueError:
            deadline = None
        if deadline is not None and now > deadline:
            _set_status(conn, decision_id, "expired")
            _remove_pending(decision_id, orders_dir)
            record_event(conn, "pending_expired",
                         "decision#%d 超 run_date(%s) 当日 15:05 TTL 确认被拒（now=%s）"
                         % (decision_id, run_date, now.isoformat(timespec="seconds")),
                         decision_id)
            print("[confirm] decision#%d 状态 -> expired（已过 %s 15:05 有效期，需重新决策）"
                  % (decision_id, run_date))
            return None
    ctx = build_context(conn, now)
    v = check(decision, ctx, CFG.get("risk", {}))
    print("[confirm] decision#%d 重跑风控：%s" % (decision_id, v.brief()))
    for x in v.violations:
        print("[risk]   [违规] %s" % x)
        record_event(conn, "risk_check_reconfirm", x, decision_id)
    for w in v.warnings:                    # C-ARC-4：confirm 此前连 warnings 都不打印
        print("[risk]   [警告] %s" % w)
    v.events.extend(_ctx_failopen_events(ctx))
    flush_events(conn, v, decision_id,      # P0-4 + C-ARC-4（同 propose）
                 warn_prefix=str(decision.get("code") or ""))
    if v.kill_trigger:
        _do_kill(conn, v, decision_id, now)
        _set_status(conn, decision_id, "rejected")
        print("[confirm] decision#%d 状态 -> rejected（kill 触发）" % decision_id)
        return None
    if not v.approved:
        _set_status(conn, decision_id, "report_only" if v.report_only else "rejected")
        _remove_pending(decision_id, orders_dir)
        print("[confirm] decision#%d 状态 -> %s（重跑风控未通过，待确认单已清理）"
              % (decision_id, "report_only" if v.report_only else "rejected"))
        return None

    broker = PaperBroker()
    code = str(decision.get("code"))
    order = v.adjusted_order if v.adjusted_order is not None else decision.get("order") or {}
    explicit_price = price_override is not None
    if explicit_price:
        price = float(price_override)
    else:
        # W-A9（P1-5）：定价必须带口径——live=实时价可用；stale_close=实时缺失
        # 回退昨收。陈旧昨收**不得自动成交**：应急/补清算单走其显式设计价
        # （跌停价/盘前收盘，构造时即定价并留痕）；普通单挂起等 --price。
        price, psource = broker.latest_price_with_source(conn, code)
        if price is None:
            price = float(order.get("price") or 0)
            print("[confirm] 警告：无最新收盘，退回决策价 %.2f" % price)
        elif psource == "stale_close":
            if decision.get("emergency_scan") or decision.get("kill_liquidation"):
                dp = float(order.get("price") or 0)
                if dp <= 0:
                    print("[confirm] 挂起：实时价缺失且决策无显式价，不自动以昨收成交")
                    return None
                stale_ref = price
                price = dp
                explicit_price = True   # 决策设计价 ≠ 自动昨收
                record_event(conn, "stale_price_exec",
                             "decision#%d %s 实时价缺失，按决策显式设计价 %.2f 执行"
                             "（昨收 %.2f 不作为成交价）" % (decision_id, code, dp, stale_ref),
                             decision_id)
                print("[confirm] %s 应急/清算单按显式设计价 %.2f 执行（实时价缺失已留痕）"
                      % ("emergency_scan" if decision.get("emergency_scan")
                         else "kill_liquidation", dp))
            else:
                record_event(conn, "stale_price_halt",
                             "decision#%d %s 实时价缺失（仅剩昨收 %.2f），拒绝自动以"
                             "昨收成交，保留 approved 等待 --price 显式确认"
                             % (decision_id, code, price), decision_id)
                print("[confirm] 挂起：实时价缺失（仅剩昨收 %.2f），不自动以昨收成交；"
                      "请稍后重试或 --price 显式确认（事件 stale_price_halt 已留痕）" % price)
                return None
    shares = int(order.get("shares") or 0)
    if shares <= 0 or price <= 0:
        print("[confirm] 价格/数量非法，放弃执行")
        return None

    # 执行价二次风控：price_guard + price_limit 用最终成交价重跑
    recheck = Verdict()
    recheck_decision = dict(decision)
    recheck_decision["order"] = dict(order)
    recheck_decision["order"]["price"] = price
    if explicit_price:
        # W-A9：显式给价（--price / 应急设计价）→ 规则9 按显式口径留痕放行
        recheck_decision["_explicit_price"] = True
    from risk import engine as _eng
    _eng.rule_price_guard(recheck_decision, ctx, CFG.get("risk", {}), recheck)
    _eng.rule_price_limit(recheck_decision, ctx, CFG.get("risk", {}), recheck)
    if recheck.violations:
        for x in recheck.violations:
            record_event(conn, "price_recheck", x, decision_id)
            print("[confirm] [执行价校验] %s" % x)
        print("[confirm] 执行价 %.2f 未通过二次校验，保留待确认单（可用更接近市价的"
              " --price 重试或 reject）" % price)
        return None

    res = _execute(conn, decision_id, decision, price, shares, confirmed_by, now)
    if res is not None:
        _remove_pending(decision_id, orders_dir)
    return res


def reject(conn: sqlite3.Connection, decision_id: int, by: str = "human",
           reason: str = "", orders_dir: Optional[Path] = None) -> bool:
    """人工否决：status=rejected + risk_event 留痕 + 清理 pending 文件。

    W-A6（P1-6）：executed/executed_unverified 一律拒绝 reject——已成交决策被置回
    rejected 会让账实审计链断裂（trade 行还在、决策却显示否决）。
    """
    got = _get_decision(conn, decision_id)
    if not got:
        print("[reject] decision#%d 不存在" % decision_id)
        return False
    cur_status = str(got[0][9] or "")
    if cur_status in ("executed", "executed_unverified"):
        record_event(conn, "manual_reject_rejected",
                     "decision#%d 已成交（status=%s），拒绝 reject（审计链保护）"
                     % (decision_id, cur_status), decision_id)
        print("[reject] decision#%d 已成交（status=%s），不可否决（审计链保护）"
              % (decision_id, cur_status))
        return False
    _set_status(conn, decision_id, "rejected")
    record_event(conn, "manual_reject", "%s: %s" % (by, reason or "未填写理由"), decision_id)
    _remove_pending(decision_id, orders_dir)
    log.info("decision#%d 被 %s 否决：%s", decision_id, by, reason)
    print("[reject] decision#%d 状态 -> rejected（%s: %s）" % (decision_id, by, reason))
    return True


def propose_db(conn: sqlite3.Connection, run_date: Optional[str] = None,
               now: Optional[datetime] = None) -> int:
    """对 ai/decide.py 已入库的 proposed 决策逐条跑风控（不重复插行）。

    盘前 9:00 生成决策时未开盘，交易时段规则会拒买卖，故 propose 拆到
    开盘后（如 9:35）由本命令执行；执行仍需人工 confirm。
    """
    now = now or datetime.now()
    if run_date is None:
        run_date = now.strftime("%Y-%m-%d")  # 决策口径统一：今天=预期执行日
    # C-ARC-2/T4：熔断生效时批量风控入口直接短路（逐条 propose 内挡板同样兜底）
    if exec_breaker_tripped(conn, now.strftime("%Y-%m-%d")):
        _record_once_today(
            conn, "exec_circuit_breaker",
            "exec_circuit_breaker: 当日执行失败根决策数达阈值 %d，propose-db 短路"
            % int(CFG.get("execution", {}).get("exec_breaker_threshold", 3)),
            prefix="exec_circuit_breaker")
        print("[propose-db] 执行失败熔断生效：跳过 run_date=%s 全部 propose" % run_date)
        return 0
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM decision WHERE run_date=? AND status='proposed' ORDER BY id",
        (run_date,)).fetchall()]
    if not ids:
        print("[propose-db] run_date=%s 无待风控的 proposed 决策" % run_date)
        return 0
    for did in ids:
        got = _get_decision(conn, did)
        if got:
            propose(conn, got[1], decision_id=did, now=now)
    return len(ids)


# ---------------------------------------------------------------- status 一览

def status(conn: sqlite3.Connection, date_str: Optional[str] = None) -> dict:
    """当日决策/成交/现金/持仓/待确认单一览（stdout 打印并返回 dict）。"""
    broker = PaperBroker()
    day = date_str or datetime.now().strftime("%Y-%m-%d")
    print("==== 执行状态 %s ====" % day)

    print("-- 决策 --")
    rows = conn.execute(
        "SELECT id, code, action, target_weight, confidence, status, created_at"
        " FROM decision WHERE run_date=? ORDER BY id", (day,)).fetchall()
    if not rows:
        print("  无")
    for (did, code, action, tw, conf, st, created) in rows:
        print("  #%s %s %s tw=%s conf=%s 状态=%s (%s)"
              % (did, code, action, tw, conf, st, created))

    print("-- 成交 --")
    rows = conn.execute(
        "SELECT id, code, name, side, price, shares, amount, status, confirmed_by"
        " FROM trade WHERE trade_date=? ORDER BY id", (day,)).fetchall()
    if not rows:
        print("  无")
    for (tid, code, name, side, price, shares, amount, st, by) in rows:
        print("  trade#%s %s %s %s x%d @%.2f 金额=%.2f [%s] 确认人=%s"
              % (tid, code, name or "", side, shares, price or 0, amount or 0, st, by or ""))

    cash, positions, total, prev_closes = broker.portfolio(conn)
    print("-- 账户 --")
    print("  现金=%.2f 总权益=%.2f" % (cash, total))
    if not positions:
        print("  持仓：无")
    for code, p in sorted(positions.items()):
        lp = broker.latest_price(conn, code)
        print("  %s %s 持股=%d 可卖=%d 成本=%.2f 现价=%s 市值=%.2f"
              % (code, p["name"], p["shares"], p["avail_shares"], p["cost"],
                 ("%.2f" % lp) if lp else "n/a", p["shares"] * (lp or p["cost"])))

    pend = list_pending()
    print("-- 待人工确认（pending 文件）--")
    if not pend:
        print("  无")
    for p in pend:
        print("  %s" % p)
    ks = repo.peak_total(conn)
    print("  （portfolio_state 历史峰值 total=%.2f）" % float(ks or 0))
    return {"date": day, "cash": cash, "total": total, "positions": positions,
            "pending": [str(p) for p in pend]}


# ---------------------------------------------------------------- 决策文件解析

def load_decision_file(path: str) -> List[dict]:
    """读取 AI 决策文件：支持 单对象 / 数组 / {"decisions": [...]} 三种形态。

    Fix A：文件内容属外部输入，skip_gate/confirmed_by 一律剥离（propose 入口
    也会再剥一次，此处先拦掉防止落库 snapshot 带入）。
    """
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(obj, dict) and isinstance(obj.get("decisions"), list):
        out = [x for x in obj["decisions"] if isinstance(x, dict)]
    elif isinstance(obj, list):
        out = [x for x in obj if isinstance(x, dict)]
    elif isinstance(obj, dict):
        out = [obj]
    else:
        raise ValueError("决策文件须为 JSON 对象/数组/{decisions:[...]}，实际: %s"
                         % type(obj).__name__)
    for d in out:
        for _k in ("skip_gate", "confirmed_by"):
            if d.pop(_k, None) is not None:
                log.warning("决策文件 %s 携带 %s，已剥离（仅规则21 可设置）", path, _k)
    return out


# ---------------------------------------------------------------- CLI

def _parse_now(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="A股AI模拟交易 · 执行编排（paper 模拟盘）")
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--now", default=None,
                       help="覆盖当前时间 ISO 格式（如 '2026-09-09 10:00:00'，回放/测试用）")

    p = sub.add_parser("propose", help="读取 AI 决策文件逐条 propose")
    p.add_argument("--file", required=True, help="决策 JSON 文件路径")
    p.add_argument("--date", default=None, help="run_date YYYY-MM-DD（默认今天）")
    add_common(p)

    p = sub.add_parser("propose-db", help="对当日已入库的 proposed 决策逐条跑风控（配合 ai/decide.py，不重复插行）")
    p.add_argument("--date", default=None, help="run_date（默认最新交易日）")
    add_common(p)

    p = sub.add_parser("confirm", help="人工确认执行 approved 决策")
    p.add_argument("--decision-id", type=int, required=True)
    p.add_argument("--by", default="human", help="确认人")
    p.add_argument("--price", type=float, default=None, help="成交价覆盖（默认最新收盘）")
    add_common(p)

    p = sub.add_parser("reject", help="人工否决决策")
    p.add_argument("--decision-id", type=int, required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--by", default="human")
    add_common(p)

    p = sub.add_parser("kill", help="kill 停机状态管理（resume 解除 / extend 延长）")
    p.add_argument("action", choices=["resume", "extend", "show"])
    p.add_argument("--hours", type=float, default=24.0, help="extend 延长小时数")
    p.add_argument("--reason", default="", help="操作理由（留痕）")
    add_common(p)

    p = sub.add_parser("status", help="当日决策/成交/现金/持仓一览")
    p.add_argument("--date", default=None)
    add_common(p)

    args = ap.parse_args(argv)
    now = _parse_now(args.now)

    _assert_paper_mode()
    conn = get_conn()
    try:
        if args.cmd == "propose":
            decisions = load_decision_file(args.file)
            print("[propose] 读取 %s 共 %d 条决策" % (args.file, len(decisions)))
            with _exec_lock():
                for d in decisions:
                    propose(conn, d, run_date=args.date, now=now)
        elif args.cmd == "propose-db":
            with _exec_lock():
                n = propose_db(conn, run_date=args.date, now=now)
            if n:
                status(conn, args.date)
        elif args.cmd == "confirm":
            with _exec_lock():
                confirm(conn, args.decision_id, confirmed_by=args.by,
                        price_override=args.price, now=now)
        elif args.cmd == "reject":
            with _exec_lock():
                reject(conn, args.decision_id, by=args.by, reason=args.reason)
        elif args.cmd == "kill":
            if args.action == "resume":
                st = kill_resume(conn, args.reason)
                print("[kill] 停机已解除：%s" % json.dumps(st, ensure_ascii=False))
            elif args.action == "extend":
                st = kill_extend(conn, args.hours, args.reason)
                print("[kill] 停机已延长：%s" % json.dumps(st, ensure_ascii=False))
            else:
                print("[kill] 当前状态：%s" % json.dumps(read_kill_state(), ensure_ascii=False))
        elif args.cmd == "status":
            status(conn, args.date)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
