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
import os
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from data.fetcher import get_conn
from risk.blacklist import check_blacklist, health_check
from risk.engine import (RiskContext, Verdict, check, record_event, apply_kill_switch,
                         limit_pct, limit_price)
from risk.notify import notify
from execution.paper import PaperBroker, compute_fees

CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
ORDERS_DIR = Path(os.environ.get("AGSICKLE_ORDERS_DIR") or (BASE / "logs" / "orders"))
STATE_DIR = Path(os.environ.get("AGSICKLE_STATE_DIR") or (BASE / "logs" / "state"))
KILL_STATE_FILE = STATE_DIR / "kill.json"

log = logging.getLogger("exec.runner")
log.setLevel(logging.INFO)
log.propagate = False
if not log.handlers:
    _fh = logging.FileHandler(BASE / "logs" / "exec.log", encoding="utf-8")
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


@contextlib.contextmanager
def _exec_lock():
    """执行入口互斥锁：cron/catchup/人工/看板四方可能同时触发 confirm/propose，
    SQLite 并发写有 busy_timeout 兜底，但 trade 计数→成交之间的 TOCTOU 只能靠进程锁。"""
    ORDERS_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = ORDERS_DIR / ".lock"
    with open(lock_path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
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


def write_kill_state(until: Optional[datetime], note: str = "") -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.chmod(0o755)
    payload = {"active": until is not None,
               "until": until.isoformat(timespec="seconds") if until else None,
               "note": note,
               "updated_at": datetime.now().isoformat(timespec="seconds")}
    KILL_STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                               encoding="utf-8")


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

def build_context(conn: sqlite3.Connection, now: datetime) -> RiskContext:
    """组装风控上下文。

    - today_trades = trade 表当日 filled+submitted 计数；
    - week_turnover = 近5交易日（daily_bar 最近5个交易日）有效成交额合计 / total_equity；
    - peak_equity = max(portfolio_state 历史 MAX(total), 当前 total_equity)；
    - kill_switch_until = risk_event 最近一条 rule 含 'kill' 的事件 ts + kill_stop_hours；
    - blacklist / health_issues 来自 risk.blacklist。
    """
    broker = PaperBroker()
    cash, positions, total_equity, prev_closes = broker.portfolio(conn)

    codes = set(positions)
    for (c,) in conn.execute("SELECT code FROM stock_info").fetchall():
        codes.add(str(c))
    # 实时行情：交易时段批量拉一次（闭市/禁用/失败自动回退日线收盘）
    live_quotes: Dict[str, dict] = {}
    import os
    if (os.environ.get("AGSICKLE_DISABLE_LIVE_QUOTES") != "1"
            and CFG.get("execution", {}).get("use_live_prices", True)):
        try:
            from data.quotes import get_live_prices, is_trading_time
            if is_trading_time(now):
                live_quotes = get_live_prices(sorted(codes))
        except Exception:  # noqa: BLE001
            live_quotes = {}
    latest_prices: Dict[str, float] = {}
    for code in sorted(codes):
        q = live_quotes.get(code)
        if q and q.get("price") is not None:
            latest_prices[code] = float(q["price"])
        else:
            lp = broker.latest_price(conn, code, live=False)
            if lp is not None:
                latest_prices[code] = lp
    for code, q in live_quotes.items():  # 实时昨收补齐（新股仅1根bar时日线推不出）
        if code not in prev_closes and q.get("prev_close") is not None:
            prev_closes[code] = float(q["prev_close"])

    today = now.strftime("%Y-%m-%d")
    today_trades = int(conn.execute(
        "SELECT COUNT(*) FROM trade WHERE trade_date=? AND status IN ('filled','submitted')",
        (today,)).fetchone()[0])
    today_sold = {r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM trade WHERE trade_date=? AND side='sell' AND " 
        "(status IS NULL OR status NOT IN ('rejected','cancelled','canceled','pending'))",
        (today,)).fetchall()}

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

    # 峰值只取最近 250 行：一条坏数据/测试 seed 行此前会永久抬高峰值，
    # 导致误 kill 或回撤永远 ≥8% 而锁死
    peak_row = conn.execute(
        "SELECT MAX(total) FROM (SELECT total FROM portfolio_state "
        "ORDER BY date DESC LIMIT 250)").fetchone()
    peak_equity = max(float(peak_row[0] or 0.0), float(total_equity))

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
    )


# ---------------------------------------------------------------- decision 行工具

def _insert_decision(conn: sqlite3.Connection, decision: dict, run_date: str) -> int:
    """决策落库（status=proposed），input_snapshot 存决策 JSON 全文，返回新 id。"""
    cur = conn.execute(
        "INSERT INTO decision (run_date, code, action, target_weight, confidence,"
        " reasons, risk_notes, input_snapshot, status, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_date, decision.get("code"), decision.get("action"),
         decision.get("target_weight"), decision.get("confidence"),
         json.dumps(decision.get("reasons", []), ensure_ascii=False),
         json.dumps(decision.get("risk_notes", []), ensure_ascii=False),
         json.dumps(decision, ensure_ascii=False), "proposed",
         datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return int(cur.lastrowid)


def _parse_json_list(raw: Any) -> List[str]:
    if raw is None or str(raw).strip() == "":
        return []
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return [str(raw)]
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def _decision_from_row(row: tuple) -> dict:
    """decision 行 → 决策 dict。order 优先取 input_snapshot 中的 JSON（ai.decide 落库时
    input_snapshot 可能是 bundle 全文，故按 code+action 匹配查找）。"""
    (did, run_date, code, action, tw, conf, reasons, risk_notes, snapshot, status) = row
    d: Dict[str, Any] = {
        "action": action, "code": code, "target_weight": tw, "confidence": conf,
        "reasons": _parse_json_list(reasons), "risk_notes": _parse_json_list(risk_notes),
    }
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
                break
    if order is not None:
        d["order"] = order
    return d


def _get_decision(conn: sqlite3.Connection, decision_id: int) -> Optional[Tuple[tuple, dict]]:
    row = conn.execute(
        "SELECT id, run_date, code, action, target_weight, confidence, reasons, risk_notes,"
        " input_snapshot, status FROM decision WHERE id=?", (decision_id,)).fetchone()
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
                   now: datetime, orders_dir: Optional[Path] = None) -> Path:
    """闸门开启时把待确认单写 logs/orders/<date>/pending_<decision_id>.json。

    valid_until=当日 15:05（此前 pending 无 TTL，昨日决策今天仍可按今日价成交，
    决策依据早已失效）。
    """
    order = v.adjusted_order if v.adjusted_order is not None else (decision.get("order") or {})
    valid_until = now.strftime("%Y-%m-%d") + "T15:05:00"
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
    for ko in v.kill_orders:
        res = broker.sell(conn, str(ko["code"]), str(ko.get("name") or ko["code"]),
                          float(ko.get("price") or 0), int(ko.get("shares") or 0),
                          decision_id=decision_id, confirmed_by="kill_switch",
                          trade_date=td)
        if res is None:
            msg = "kill 清仓失败：%s" % json.dumps(ko, ensure_ascii=False)
            record_event(conn, "kill_sell_failed", msg, decision_id)
            print("[KILL] 清仓失败：%s" % msg)
            deferred.append(str(ko["code"]))
            continue
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
    apply_kill_switch(conn, v.kill_until, note="decision#%s 触发" % decision_id)
    write_kill_state(v.kill_until, "decision#%s 触发" % decision_id)
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
            continue  # 已清仓完成
        price = PaperBroker().latest_price(conn, code, live=False) or 0.0
        if price <= 0:
            price = float(pos[2] or 0)
        if price <= 0:
            continue
        decision = {
            "action": "sell", "code": code, "name": pos[0],
            "target_weight": 0.0, "confidence": 1.0,
            "reasons": ["kill 递延补清算（T+1 解锁/上次卖出失败）"],
            "risk_notes": ["kill_switch 自动清算单"],
            "kill_liquidation": True,
            "order": {"side": "sell", "price": price, "shares": int(pos[1])},
        }
        print("[liquidation] %s %s 补清算 %d 股 @%.2f" % (code, pos[0], int(pos[1]), price))
        propose(conn, decision, run_date=now.strftime("%Y-%m-%d"), now=now,
                orders_dir=orders_dir)
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

    - decision_id 为 None 时先插入 decision 行（status=proposed，重复文件内容跳过）；
    - kill_trigger=True 时立即执行清仓 + apply_kill_switch（不受人工闸门约束）；
    - report_only / rejected 均不进入闸门。
    """
    now = now or datetime.now()
    _assert_paper_mode()
    exec_cfg = CFG.get("execution", {})
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

    if v.kill_trigger:
        _do_kill(conn, v, decision_id, now)
        _set_status(conn, decision_id, "rejected")
        record_event(conn, "decision_killed", "决策因 kill 触发作废", decision_id)
        print("[propose] decision#%d 状态 -> rejected（kill 触发）" % decision_id)
        return v

    if v.approved:
        if decision.get("action") in ("hold", "watch"):
            _set_status(conn, decision_id, "approved")
            print("[propose] decision#%d 状态 -> approved（%s 无交易动作，无需确认）"
                  % (decision_id, decision.get("action")))
        elif exec_cfg.get("manual_gate", True):
            path = _write_pending(conn, decision_id, decision, v, now, orders_dir)
            _set_status(conn, decision_id, "approved")
            print("[gate] 人工闸门开启：待确认单 %s" % path)
            print("[gate] 等待人工确认 -> python3 execution/runner.py confirm"
                  " --decision-id %d" % decision_id)
        else:
            order = v.adjusted_order if v.adjusted_order is not None else decision["order"]
            print("[gate] 人工闸门关闭：直接执行")
            # gate-off 自动模式按实时价成交（此前直接用决策价，决策价与市价的
            # 偏差完全不被记录）
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
                     int(order["shares"]), confirmed_by="auto", now=now)
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

    - 决策 run_date 与今天不一致（或 pending 过期）→ 置 expired，拒绝执行昨天的决策；
    - 最终成交价（override/实时价）确定后，再对**执行价**重跑价格保护与涨跌停
      校验——此前重跑风控用的是决策原始价，--price 覆盖价可绕过全部价格类风控。
    """
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
    # 过期闸门：决策是为某个交易日做的，跨日（或超当日 15:05）决策依据已失效
    run_date = str(row[1] or "")
    if run_date and run_date != now.strftime("%Y-%m-%d"):
        _set_status(conn, decision_id, "expired")
        _remove_pending(decision_id, orders_dir)
        record_event(conn, "pending_expired",
                     "decision#%d run_date=%s 跨日确认被拒（今日 %s）"
                     % (decision_id, run_date, now.strftime("%Y-%m-%d")), decision_id)
        print("[confirm] decision#%d 状态 -> expired（决策日 %s ≠ 今日，需重新决策）"
              % (decision_id, run_date))
        return None
    ctx = build_context(conn, now)
    v = check(decision, ctx, CFG.get("risk", {}))
    print("[confirm] decision#%d 重跑风控：%s" % (decision_id, v.brief()))
    for x in v.violations:
        print("[risk]   [违规] %s" % x)
        record_event(conn, "risk_check_reconfirm", x, decision_id)
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
    if price_override is not None:
        price = float(price_override)
    else:
        price = broker.latest_price(conn, code)
        if price is None:
            price = float((decision.get("order") or {}).get("price") or 0)
            print("[confirm] 警告：无最新收盘，退回决策价 %.2f" % price)
    order = v.adjusted_order if v.adjusted_order is not None else decision.get("order") or {}
    shares = int(order.get("shares") or 0)
    if shares <= 0 or price <= 0:
        print("[confirm] 价格/数量非法，放弃执行")
        return None

    # 执行价二次风控：price_guard + price_limit 用最终成交价重跑
    recheck = Verdict()
    recheck_decision = dict(decision)
    recheck_decision["order"] = dict(order)
    recheck_decision["order"]["price"] = price
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
    """人工否决：status=rejected + risk_event 留痕 + 清理 pending 文件。"""
    got = _get_decision(conn, decision_id)
    if not got:
        print("[reject] decision#%d 不存在" % decision_id)
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
        row = conn.execute("SELECT MAX(trade_date) FROM daily_bar").fetchone()
        run_date = row[0] if row and row[0] else now.strftime("%Y-%m-%d")
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
    ks = conn.execute("SELECT MAX(total) FROM portfolio_state").fetchone()[0]
    print("  （portfolio_state 历史峰值 total=%.2f）" % float(ks or 0))
    return {"date": day, "cash": cash, "total": total, "positions": positions,
            "pending": [str(p) for p in pend]}


# ---------------------------------------------------------------- 决策文件解析

def load_decision_file(path: str) -> List[dict]:
    """读取 AI 决策文件：支持 单对象 / 数组 / {"decisions": [...]} 三种形态。"""
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(obj, dict) and isinstance(obj.get("decisions"), list):
        return [x for x in obj["decisions"] if isinstance(x, dict)]
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        return [obj]
    raise ValueError("决策文件须为 JSON 对象/数组/{decisions:[...]}，实际: %s" % type(obj).__name__)


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
