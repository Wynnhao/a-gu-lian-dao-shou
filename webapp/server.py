#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A股镰刀手 · AI交易员看板 —— 本地 Web 仪表盘后端。

仅用 Python 标准库（http.server + sqlite3 + json），无任何第三方依赖：
    python3 webapp/server.py            # 默认 127.0.0.1:8317
    python3 webapp/server.py --port 9000

设计约定：
- 只读展示为主，唯一写操作是 POST /api/confirm、/api/reject，
  二者通过 subprocess 调 execution/runner.py，不直接改库；
- 每个请求独立 sqlite3 连接，用完即关；
- 文件类端点（report/logs/session/静态资源）一律做路径白名单：
  resolve 后必须仍位于对应基准目录（或项目根）之内，防目录穿越；
- 接口内任何异常返回 500 + {"error": "..."}，绝不让服务崩溃。

API 契约（全部 JSON）：
  GET  /api/overview                 总览（权益/持仓/基准/健康/黑名单）
  GET  /api/equity_curve             权益曲线（组合 vs 沪深300 归一 + 回撤%）
  GET  /api/candles?code=&days=120   K线 + MA5/20/60（后端算，头部补 null）
  GET  /api/signals                  每票最新信号，score 降序
  GET  /api/decisions?limit=50       决策流水（reasons/risk_notes 已解析为数组）
  GET  /api/trades?limit=50          成交流水
  GET  /api/risk_events?limit=50     风控事件
  GET  /api/news?limit=30            {market:[...], by_code:{code:[...]}}
  GET  /api/macro                    三指数最新估值
  GET  /api/macro_history?index=&years=5   PE / PE分位 历史（pe_pct 0~1 原值）
  GET  /api/health                   数据健康 + fetch_log 最近20条 + 各票行数
  GET  /api/reports                  logs/reports 列表（新→旧）
  GET  /api/report?file=xx.md        报告原文（文件名白名单 [A-Za-z0-9._-]）
  GET  /api/logs?name=exec&lines=200 日志尾部（name 限 7 个白名单文件）
  GET  /api/pending                  待人工确认单（扫 logs/orders/*/pending_*.json）
  GET  /api/sessions                 决策输入包日期列表
  GET  /api/session?date=&kind=      bundle_md | bundle_json | decision 文本内容
  GET  /api/workflow                十段流水线状态 + 当日决策追踪链
  GET  /api/doc?name=               docs/ 决策策略/策略库 markdown
  GET  /api/concepts                自选股概念分组（多归属+黑名单标注）
  GET  /api/dynamic_pools           异动池/热门池（?with_boards=1 加板块榜）
  GET  /api/backtest                 logs/backtest_result.json 原文（无则 {missing:true}）
  POST /api/confirm {decision_id, by}          跑 runner.py confirm
  POST /api/reject  {decision_id, reason, by}  跑 runner.py reject
"""
import argparse
import bisect
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import threading
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

BASE = Path(__file__).resolve().parent.parent          # 项目根
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))                      # 供 /api/dynamic_pools 等端点导入项目模块
_dist = Path(__file__).resolve().parent / "dist"       # 新前端构建产物优先
STATIC_DIR = _dist if (_dist / "index.html").is_file() \
    else Path(__file__).resolve().parent / "static"
CONFIG_PATH = BASE / "config.json"
LOGS_DIR = BASE / "logs"
REPORTS_DIR = LOGS_DIR / "reports"
SESSION_DIR = LOGS_DIR / "session"
ORDERS_DIR = LOGS_DIR / "orders"

INDEX_NAMES = {"000300": "沪深300", "000905": "中证500", "000001": "上证指数"}
LOG_WHITELIST = ("fetch", "news", "macro", "signal", "ai", "pipeline", "exec")
SESSION_KINDS = {"bundle_md": "bundle.md", "bundle_json": "bundle.json",
                 "decision": "decision.json"}
FILE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MIME_OVERRIDE = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}


# ---------------------------------------------------------------- 基础工具

_CONFIG_CACHE: Dict[str, Any] = {"mtime": None, "cfg": {}}


def load_config() -> dict:
    """读 config.json（按 mtime 缓存——此前每个请求多次读盘）。"""
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        return {}
    if _CONFIG_CACHE["mtime"] != mtime:
        try:
            _CONFIG_CACHE["cfg"] = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            _CONFIG_CACHE["mtime"] = mtime
        except Exception:
            return {}
    return _CONFIG_CACHE["cfg"]


def db_file() -> Path:
    cfg = load_config()
    p = Path(str(cfg.get("db_path") or "data/market.db"))
    if not p.is_absolute():
        p = BASE / p
    return p


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_file()), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def q_all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> List[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def q_one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Optional[dict]:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def parse_json_field(raw: Any, fallback: Any) -> Any:
    """表里的 JSON 串 → 对象；空/坏值回退。"""
    if raw is None or str(raw).strip() == "":
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def safe_join(base: Path, rel: str) -> Optional[Path]:
    """路径白名单：resolve 后必须仍在 base 之内，否则 None（防目录穿越）。"""
    try:
        base_real = os.path.realpath(str(base))
        target = os.path.realpath(os.path.join(base_real, rel))
        if os.path.commonpath([base_real, target]) != base_real:
            return None
        return Path(target)
    except (ValueError, OSError):
        return None


def clamp_int(val: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(val)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def fnum(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 风控快照（复用 risk/blacklist.py，单一事实源——此前双维护存在口径漂移风险）

def health_issues_of(conn: sqlite3.Connection) -> List[str]:
    from risk.blacklist import health_check
    return health_check(conn)


def blacklist_of(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    from risk.blacklist import check_blacklist
    names = {r["code"]: r["name"] for r in
             q_all(conn, "SELECT code, name FROM stock_info")}
    out: List[Dict[str, Any]] = []
    for code, (ok, reason) in sorted(check_blacklist(conn).items()):
        out.append({"code": code, "name": names.get(code, code),
                    "ok": bool(ok), "reason": reason or "-"})
    return out


def latest_bar(conn: sqlite3.Connection, code: str) -> Optional[dict]:
    return q_one(conn, "SELECT trade_date, close FROM daily_bar WHERE code=? "
                       "ORDER BY trade_date DESC LIMIT 1", (code,))


def bench_close_at(conn: sqlite3.Connection, date_str: str) -> Optional[float]:
    """index_daily '000300' 在 date_str 当日或之前最近一个交易日的收盘。"""
    row = q_one(conn, "SELECT close FROM index_daily WHERE index_code='000300' "
                      "AND trade_date<=? ORDER BY trade_date DESC LIMIT 1", (date_str,))
    return fnum(row["close"]) if row and row["close"] is not None else None


# ---------------------------------------------------------------- API 实现

def api_overview(conn: sqlite3.Connection, qs: dict) -> dict:
    cfg = load_config()
    start_cash = fnum((cfg.get("execution", {}) or {}).get("paper_start_cash"), 1000000.0)

    ps_latest = q_one(conn, "SELECT * FROM portfolio_state ORDER BY date DESC LIMIT 1")
    ps_first = q_one(conn, "SELECT MIN(date) AS d FROM portfolio_state")
    bar_latest = q_one(conn, "SELECT MAX(trade_date) AS d FROM daily_bar")

    as_of = ""
    if ps_latest:
        as_of = str(ps_latest["date"])
    elif bar_latest and bar_latest["d"]:
        as_of = str(bar_latest["d"])
    cash = fnum(ps_latest["cash"]) if ps_latest else 0.0
    market_value = fnum(ps_latest["market_value"]) if ps_latest else 0.0
    total = fnum(ps_latest["total"]) if ps_latest else 0.0
    drawdown = fnum(ps_latest["drawdown"]) if ps_latest else 0.0
    kill_switch = int(ps_latest["kill_switch"] or 0) if ps_latest else 0

    cum_return_pct = ((total / start_cash) - 1.0) * 100.0 if start_cash > 0 else 0.0

    # 基准：沪深300 与账户同起点（portfolio_state 最早日期）归一
    bench = {"name": "沪深300", "close": None, "cum_return_pct": 0.0}
    start_date = ps_first and ps_first["d"]
    bench_latest = q_one(conn, "SELECT trade_date, close FROM index_daily "
                               "WHERE index_code='000300' ORDER BY trade_date DESC LIMIT 1")
    if bench_latest and bench_latest["close"] is not None:
        bench["close"] = fnum(bench_latest["close"])
        base_close = bench_close_at(conn, str(start_date)) if start_date else None
        if base_close and base_close > 0:
            bench["cum_return_pct"] = (bench["close"] / base_close - 1.0) * 100.0
    excess_pct = round(cum_return_pct - fnum(bench["cum_return_pct"]), 4)

    positions: List[Dict[str, Any]] = []
    for p in q_all(conn, "SELECT * FROM position ORDER BY code"):
        bar = latest_bar(conn, str(p["code"]))
        lp = fnum(bar["close"]) if bar and bar["close"] is not None else None
        latest_date = str(bar["trade_date"]) if bar else ""
        mv = p["shares"] * lp if lp is not None else None
        pnl = (lp - fnum(p["cost"])) * p["shares"] if lp is not None else None
        positions.append({
            "code": p["code"], "name": p["name"], "shares": int(p["shares"]),
            "avail_shares": int(p["avail_shares"]), "cost": fnum(p["cost"]),
            "latest_price": lp, "latest_date": latest_date, "market_value": mv,
            "unrealized_pnl": pnl,
            "pct_of_total": (mv / total * 100.0) if (mv is not None and total > 0) else 0.0,
        })

    return {
        "as_of": as_of,
        "cash": cash, "market_value": market_value, "total": total,
        "drawdown": drawdown, "kill_switch": kill_switch,
        "start_cash": start_cash, "cum_return_pct": round(cum_return_pct, 4),
        "benchmark": bench, "excess_pct": excess_pct,
        "positions": positions,
        "health_issues": health_issues_of(conn),
        "blacklist": blacklist_of(conn),
        "kill_switch_active": bool(kill_switch),
    }


def api_equity_curve(conn: sqlite3.Connection, qs: dict) -> dict:
    rows = q_all(conn, "SELECT date, total, drawdown FROM portfolio_state ORDER BY date ASC")
    dates: List[str] = [str(r["date"]) for r in rows]
    total: List[Optional[float]] = [fnum(r["total"]) if r["total"] is not None else None for r in rows]
    drawdown: List[float] = [fnum(r["drawdown"]) * 100.0 for r in rows]  # 0~1 → %
    benchmark: List[Optional[float]] = []
    if dates:
        # 一次性载入基准收盘（此前逐日单查是 N+1，窗口拉长后明显变慢）
        bench_rows = q_all(conn, "SELECT trade_date, close FROM index_daily "
                                 "WHERE index_code='000300' ORDER BY trade_date ASC")
        bdates: List[str] = []
        bvals: List[float] = []
        last = None
        for r in bench_rows:
            if r["close"] is not None:
                last = fnum(r["close"])
            if last is not None:
                bdates.append(str(r["trade_date"]))
                bvals.append(last)
        closes: List[Optional[float]] = []
        for d in dates:
            i = bisect.bisect_right(bdates, d) - 1
            closes.append(bvals[i] if i >= 0 else None)
        first = next((c for c in closes if c), None)
        for c in closes:
            if c and first:
                benchmark.append(round(total[0] * c / first, 2) if total[0] else round(c / first, 4))
            else:
                benchmark.append(None)
    return {"dates": dates, "total": total, "benchmark": benchmark, "drawdown": drawdown}


def api_candles(conn: sqlite3.Connection, qs: dict) -> dict:
    code = str((qs.get("code") or [""])[0]).strip()
    days = clamp_int((qs.get("days") or ["120"])[0], 120, 10, 500)
    if not re.match(r"^\d{5,6}$", code):
        raise ValueError("非法代码 %r" % code)
    # 多取 80 根做 MA 预热，再裁掉，保证输出窗口内 MA 均有值
    need = days + 80
    rows = q_all(conn, "SELECT trade_date, open, high, low, close, volume, pct_chg "
                       "FROM daily_bar WHERE code=? ORDER BY trade_date DESC LIMIT ?",
                 (code, need))
    rows.reverse()
    if not rows:
        return {"code": code, "name": "", "dates": [], "kline": {"open": [], "high": [],
                "low": [], "close": [], "volume": []}, "ma5": [], "ma20": [], "ma60": []}
    info = q_one(conn, "SELECT name FROM stock_info WHERE code=?", (code,))

    def ma_list(closes: List[Optional[float]], n: int) -> List[Optional[float]]:
        out: List[Optional[float]] = []
        s = 0.0
        for i, c in enumerate(closes):
            if c is None:
                out.append(None)
                continue
            s += c
            if i >= n:
                prev = closes[i - n]
                if prev is not None:
                    s -= prev
            out.append(round(s / n, 4) if i >= n - 1 else None)
        return out

    closes = [fnum(r["close"]) for r in rows]
    ma5, ma20, ma60 = ma_list(closes, 5), ma_list(closes, 20), ma_list(closes, 60)
    if len(rows) > days:
        cut = len(rows) - days
        rows, ma5, ma20, ma60 = rows[cut:], ma5[cut:], ma20[cut:], ma60[cut:]
    return {
        "code": code, "name": (info or {}).get("name") or code,
        "dates": [str(r["trade_date"]) for r in rows],
        "kline": {
            "open": [fnum(r["open"]) for r in rows],
            "high": [fnum(r["high"]) for r in rows],
            "low": [fnum(r["low"]) for r in rows],
            "close": [fnum(r["close"]) for r in rows],
            "volume": [fnum(r["volume"]) for r in rows],
        },
        "pct_chg": [fnum(r["pct_chg"]) for r in rows],
        "ma5": ma5, "ma20": ma20, "ma60": ma60,
    }


def api_signals(conn: sqlite3.Connection, qs: dict) -> List[dict]:
    rows = q_all(conn,
                 "SELECT s.code, s.as_of, s.signals, s.score FROM signal s "
                 "JOIN (SELECT code, MAX(as_of) AS m FROM signal GROUP BY code) t "
                 "ON s.code=t.code AND s.as_of=t.m ORDER BY s.score DESC")
    names = {r["code"]: r["name"] for r in
             q_all(conn, "SELECT code, name FROM stock_info")}
    out: List[dict] = []
    for r in rows:
        out.append({"code": r["code"], "name": names.get(r["code"], r["code"]),
                    "as_of": str(r["as_of"]),
                    "signals": parse_json_field(r["signals"], {}),
                    "score": fnum(r["score"])})
    return out


def api_decisions(conn: sqlite3.Connection, qs: dict) -> List[dict]:
    limit = clamp_int((qs.get("limit") or ["50"])[0], 50, 1, 500)
    rows = q_all(conn, "SELECT * FROM decision ORDER BY id DESC LIMIT ?", (limit,))
    names = {r["code"]: r["name"] for r in q_all(conn, "SELECT code, name FROM stock_info")}
    out: List[dict] = []
    for r in rows:
        out.append({
            "id": int(r["id"]), "run_date": r["run_date"], "code": r["code"],
            "name": names.get(r["code"], r["code"]), "action": r["action"],
            "target_weight": fnum(r["target_weight"]), "confidence": fnum(r["confidence"]),
            "status": r["status"],
            "reasons": parse_json_field(r["reasons"], []),
            "risk_notes": parse_json_field(r["risk_notes"], []),
            "created_at": r["created_at"],
        })
    return out


def api_trades(conn: sqlite3.Connection, qs: dict) -> List[dict]:
    limit = clamp_int((qs.get("limit") or ["50"])[0], 50, 1, 500)
    rows = q_all(conn, "SELECT * FROM trade ORDER BY id DESC LIMIT ?", (limit,))
    out: List[dict] = []
    for r in rows:
        d = dict(r)
        for k in ("shares",):
            d[k] = int(d[k]) if d.get(k) is not None else None
        out.append(d)
    return out


def api_risk_events(conn: sqlite3.Connection, qs: dict) -> List[dict]:
    limit = clamp_int((qs.get("limit") or ["50"])[0], 50, 1, 500)
    rows = q_all(conn, "SELECT * FROM risk_event ORDER BY id DESC LIMIT ?", (limit,))
    out: List[dict] = []
    for r in rows:
        d = dict(r)
        d["id"] = int(d["id"])
        d["decision_id"] = int(d["decision_id"]) if d.get("decision_id") is not None else None
        out.append(d)
    return out


def api_news(conn: sqlite3.Connection, qs: dict) -> dict:
    limit = clamp_int((qs.get("limit") or ["30"])[0], 30, 1, 200)
    rows = q_all(conn, "SELECT code, title, content, source, url, published_at FROM news "
                       "ORDER BY published_at DESC, id DESC LIMIT ?", (limit,))

    def slim(r: dict) -> dict:
        content = str(r.get("content") or "")
        return {"title": r.get("title") or "", "source": r.get("source") or "",
                "published_at": r.get("published_at") or "", "url": r.get("url") or "",
                "content": content[:100]}

    market: List[dict] = []
    by_code: Dict[str, List[dict]] = {}
    for r in rows:
        code = str(r.get("code") or "")
        if code == "":
            market.append(slim(r))
        else:
            by_code.setdefault(code, []).append(slim(r))
    return {"market": market, "by_code": by_code}


def api_macro(conn: sqlite3.Connection, qs: dict) -> dict:
    indices: List[dict] = []
    for idx, name in INDEX_NAMES.items():
        r = q_one(conn, "SELECT * FROM index_valuation WHERE index_code=? "
                        "ORDER BY trade_date DESC LIMIT 1", (idx,))
        if not r:
            continue
        indices.append({"index_code": idx, "name": name, "trade_date": r["trade_date"],
                        "pe": fnum(r["pe"]), "pe_pct": fnum(r["pe_pct"]),
                        "pb": fnum(r["pb"]), "pb_pct": fnum(r["pb_pct"]),
                        "close": fnum(r["close"])})
    return {"indices": indices}


def api_macro_history(conn: sqlite3.Connection, qs: dict) -> dict:
    idx = str((qs.get("index") or ["000300"])[0])
    if idx not in INDEX_NAMES:
        raise ValueError("非法指数代码 %r" % idx)
    years = clamp_int((qs.get("years") or ["5"])[0], 5, 1, 20)
    latest = q_one(conn, "SELECT MAX(trade_date) AS d FROM index_valuation WHERE index_code=?",
                   (idx,))
    dates: List[str] = []
    pe: List[Optional[float]] = []
    pe_pct: List[Optional[float]] = []
    if latest and latest["d"]:
        start = (datetime.fromisoformat(str(latest["d"])) - timedelta(days=365 * years)
                 ).strftime("%Y-%m-%d")
        for r in q_all(conn, "SELECT trade_date, pe, pe_pct FROM index_valuation "
                             "WHERE index_code=? AND trade_date>=? ORDER BY trade_date ASC",
                       (idx, start)):
            dates.append(str(r["trade_date"]))
            pe.append(fnum(r["pe"]) if r["pe"] is not None else None)
            pe_pct.append(fnum(r["pe_pct"]) if r["pe_pct"] is not None else None)  # 0~1 原值
    return {"index": idx, "name": INDEX_NAMES[idx], "dates": dates, "pe": pe, "pe_pct": pe_pct}


def api_health(conn: sqlite3.Connection, qs: dict) -> dict:
    fetch_log = q_all(conn, "SELECT code, run_at, status, rows, detail FROM fetch_log "
                            "ORDER BY run_at DESC LIMIT 20")
    codes = q_all(conn, "SELECT si.code AS code, si.name AS name, MAX(db.trade_date) AS latest_bar_date,"
                        " COUNT(db.trade_date) AS rows FROM stock_info si "
                        "LEFT JOIN daily_bar db ON db.code=si.code "
                        "GROUP BY si.code, si.name ORDER BY si.code")
    for c in codes:
        c["rows"] = int(c["rows"] or 0)
    return {"health_issues": health_issues_of(conn), "fetch_log": fetch_log, "codes": codes}


def api_reports(qs: dict) -> List[dict]:
    out: List[dict] = []
    if REPORTS_DIR.is_dir():
        for p in REPORTS_DIR.iterdir():
            if p.is_file() and p.suffix == ".md":
                st = p.stat()
                out.append({"file": p.name, "size": int(st.st_size),
                            "mtime": int(st.st_mtime)})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def api_report(qs: dict) -> dict:
    name = str((qs.get("file") or [""])[0])
    if not FILE_RE.match(name) or ".." in name:
        raise ValueError("非法文件名")
    path = safe_join(REPORTS_DIR, name)
    if path is None or path.parent != REPORTS_DIR.resolve() or not path.is_file():
        raise FileNotFoundError("报告不存在")
    return {"name": name, "markdown": path.read_text(encoding="utf-8", errors="replace")}


def api_logs(qs: dict) -> dict:
    name = str((qs.get("name") or [""])[0])
    if name not in LOG_WHITELIST:
        raise ValueError("日志名须为 %s 之一" % ("/".join(LOG_WHITELIST),))
    lines = clamp_int((qs.get("lines") or ["200"])[0], 200, 1, 2000)
    path = safe_join(LOGS_DIR, name + ".log")
    if path is None or path.parent != LOGS_DIR.resolve():
        raise ValueError("非法日志路径")
    out: List[str] = []
    if path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace")
        out = text.splitlines()[-lines:]
    return {"name": name, "lines": out}


def api_pending(qs: dict) -> List[dict]:
    out: List[dict] = []
    if not ORDERS_DIR.is_dir():
        return out
    files = sorted(ORDERS_DIR.glob("*/pending_*.json"))
    for p in files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        out.append({
            "decision_id": data.get("decision_id"),
            "path": str(Path("logs") / "orders" / p.parent.name / p.name),
            "decision": data.get("decision") or {},
            "verdict": data.get("verdict") or {},
            "confirm_hint": data.get("confirm_hint") or "",
            "reject_hint": data.get("reject_hint") or "",
            "created_at": data.get("created_at") or "",
        })
    out.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return out


def api_sessions(qs: dict) -> List[dict]:
    out: List[dict] = []
    if not SESSION_DIR.is_dir():
        return out
    for p in SESSION_DIR.iterdir():
        if p.is_dir() and DATE_RE.match(p.name):
            out.append({"date": p.name, "has_bundle": (p / "bundle.md").is_file(),
                        "has_decision": (p / "decision.json").is_file()})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def api_session(qs: dict) -> dict:
    date = str((qs.get("date") or [""])[0])
    kind = str((qs.get("kind") or [""])[0])
    if not DATE_RE.match(date):
        raise ValueError("日期格式须为 YYYY-MM-DD")
    if kind not in SESSION_KINDS:
        raise ValueError("kind 须为 bundle_md/bundle_json/decision")
    folder = safe_join(SESSION_DIR, date)
    if folder is None or folder.parent != SESSION_DIR.resolve() or not folder.is_dir():
        raise FileNotFoundError("会话目录不存在")
    path = safe_join(folder, SESSION_KINDS[kind])
    if path is None or path.parent != folder.resolve() or not path.is_file():
        raise FileNotFoundError("文件不存在")
    return {"date": date, "kind": kind, "file": SESSION_KINDS[kind],
            "content": path.read_text(encoding="utf-8", errors="replace")}


def api_backtest(qs: dict) -> dict:
    path = safe_join(LOGS_DIR, "backtest_result.json")
    if path is None or not path.is_file():
        return {"missing": True}
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        return {"raw": data}
    except ValueError:
        return {"missing": True, "raw_text": text[:5000]}


# ---------------------------------------------------------------- 人工闸门（唯一写操作，走 runner.py CLI）

_BY_RE = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff]+")

# 审查 P2-2：串行化 runner 子进程调用，防并发 confirm 同一决策双重成交
_RUNNER_LOCK = threading.Lock()


def run_runner(args: List[str], timeout: int = 60) -> Tuple[int, str]:
    cmd = [sys.executable, "execution/runner.py"] + args
    with _RUNNER_LOCK:
        proc = subprocess.run(cmd, cwd=str(BASE), capture_output=True,
                              text=True, timeout=timeout)
    out = (proc.stdout or "")
    if proc.stderr:
        out += ("\n[stderr]\n" + proc.stderr) if out else proc.stderr
    return proc.returncode, out.strip() or "(无输出)"


def api_confirm(body: dict) -> dict:
    try:
        did = int(body.get("decision_id"))
    except (TypeError, ValueError):
        raise ValueError("decision_id 必须为整数")
    by = _BY_RE.sub("", str(body.get("by") or "")).strip()[:40] or "human"
    rc, out = run_runner(["confirm", "--decision-id", str(did), "--by", by])
    return {"ok": rc == 0, "output": out}


def api_reject(body: dict) -> dict:
    try:
        did = int(body.get("decision_id"))
    except (TypeError, ValueError):
        raise ValueError("decision_id 必须为整数")
    by = _BY_RE.sub("", str(body.get("by") or "")).strip()[:40] or "human"
    reason = re.sub(r"\s+", " ", str(body.get("reason") or "")).strip()[:500]
    if not reason:
        raise ValueError("否决必须填写理由")
    rc, out = run_runner(["reject", "--decision-id", str(did),
                          "--reason=%s" % reason, "--by", by])
    return {"ok": rc == 0, "output": out}


# ---------------------------------------------------------------- 决策工作流聚合

def _stage(sid: str, name: str, desc: str, status: str, detail: str,
           ts: Optional[str] = None) -> dict:
    return {"id": sid, "name": name, "desc": desc, "status": status,
            "detail": detail, "ts": ts}


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat(timespec="seconds") if dt else None


def api_workflow(conn: sqlite3.Connection, qs: dict) -> dict:
    """聚合十段流水线的实时状态 + 当日决策追踪链。status: ok/warn/fail/idle。"""
    row = q_one(conn, "SELECT MAX(trade_date) AS d FROM daily_bar")
    run_date = row["d"] if row else None
    stages: List[dict] = []
    issues = health_issues_of(conn)

    # ① 行情采集
    fl = q_one(conn, "SELECT MAX(run_at) AS ts FROM fetch_log")
    bars = q_all(conn, "SELECT code, MAX(trade_date) AS d FROM daily_bar GROUP BY code")
    fresh = sum(1 for b in bars if b["d"] == run_date)
    stages.append(_stage(
        "fetch", "行情采集", "东财/腾讯双源日K增量入库",
        "ok" if bars and fresh == len(bars) else ("warn" if bars else "idle"),
        "%d/%d 票数据至 %s" % (fresh, len(bars), run_date or "—"), fl["ts"]))

    # ② 资讯与估值
    nw = q_one(conn, "SELECT MAX(fetched_at) AS ts, COUNT(*) AS n FROM news")
    val = q_one(conn, "SELECT MAX(trade_date) AS d FROM index_valuation")
    val_lag = None
    if val["d"] and run_date:
        val_lag = (datetime.fromisoformat(run_date) - datetime.fromisoformat(val["d"])).days
    stages.append(_stage(
        "news", "资讯/估值", "个股+市场新闻；三指数PE/PB分位",
        "ok" if nw["n"] and (val_lag is not None and val_lag <= 3) else
        ("warn" if nw["n"] else "idle"),
        "新闻 %d 条；估值 %s（滞后 %s 天）"
        % (nw["n"], val["d"] or "—", val_lag if val_lag is not None else "—"), nw["ts"]))

    # ③ 数据体检（黑名单 + 健康）
    bl = blacklist_of(conn)
    blocked = sum(1 for b in bl if not b["ok"])
    stages.append(_stage(
        "health", "数据体检", "黑名单过滤 + 数据健康检查",
        "fail" if issues else "ok",
        ("健康告警 %d 项；" % len(issues)) if issues else "健康 OK；"
        + "黑名单拦截 %d/%d" % (blocked, len(bl))))

    # ④ 信号计算
    sig = q_one(conn, "SELECT MAX(as_of) AS d, COUNT(*) AS n FROM signal")
    sig_ok = sig["d"] == run_date and sig["n"] > 0
    stages.append(_stage(
        "signals", "信号计算", "MA/RSI/动量/换手分位 → score",
        "ok" if sig_ok else ("warn" if sig["n"] else "idle"),
        "%d 票 as_of=%s" % (sig["n"], sig["d"] or "—")))

    # ⑤ 输入包
    bpath = SESSION_DIR / str(run_date) / "bundle.md"
    bts = datetime.fromtimestamp(bpath.stat().st_mtime) if bpath.is_file() else None
    stages.append(_stage(
        "bundle", "输入包组装", "信号+新闻+宏观+账户 → bundle.md",
        "ok" if bts else "idle",
        "bundle %s" % ("生成于 %s" % bts.strftime("%m-%d %H:%M") if bts else "未生成"),
        _iso(bts)))

    # ⑥~⑧ 决策/裁决/闸门
    decs = q_all(conn, "SELECT * FROM decision WHERE run_date=? ORDER BY id", (run_date,))
    d_ids = [d["id"] for d in decs]
    st_cnt: Dict[str, int] = {}
    for d in decs:
        st_cnt[d["status"]] = st_cnt.get(d["status"], 0) + 1
    llm_ts = min((d["created_at"] for d in decs), default=None)
    stages.append(_stage(
        "llm", "LLM决策", "读bundle输出结构化建议（校验失败整包放弃）",
        "ok" if decs else ("idle" if not bts else "warn"),
        "%d 条决策 %s" % (len(decs),
                          "、".join("%s=%d" % kv for kv in sorted(st_cnt.items())) or "")
        if decs else "当日尚无决策（待盘前流程）",
        llm_ts))
    rk = q_one(conn,
               "SELECT COUNT(*) AS n, MAX(ts) AS ts FROM risk_event "
               "WHERE decision_id IN (%s)" % ",".join("?" * len(d_ids)) if d_ids
               else "SELECT 0 AS n, NULL AS ts",
               tuple(d_ids) if d_ids else ())
    stages.append(_stage(
        "risk", "规则裁决", "15条硬规则：仓位/价格/T+1/涨跌停/kill",
        "ok" if decs else "idle",
        "风控事件 %d 条；approved=%d rejected=%d report_only=%d"
        % (rk["n"], st_cnt.get("approved", 0) + st_cnt.get("executed", 0),
           st_cnt.get("rejected", 0), st_cnt.get("report_only", 0)), rk["ts"]))
    pend = sorted(ORDERS_DIR.glob(str(run_date) + "/pending_*.json")) if run_date else []
    p_ts = datetime.fromtimestamp(pend[0].stat().st_mtime) if pend else None
    stages.append(_stage(
        "gate", "人工闸门", "pending待确认单，confirm时重跑风控",
        "warn" if pend else "ok",
        "%d 单待人工确认" % len(pend) if pend else "无待确认单", _iso(p_ts)))

    # ⑨ 执行
    tr = q_one(conn, "SELECT COUNT(*) AS n, MAX(created_at) AS ts FROM trade WHERE trade_date=?",
               (run_date,))
    stages.append(_stage(
        "exec", "模拟执行", "PaperBroker成交(T+1)+回读校验",
        "ok" if tr["n"] else ("idle" if not decs else "warn"),
        "%d 笔成交" % tr["n"] if tr["n"] else "当日无成交", tr["ts"]))

    # ⑩ 复盘
    rep = REPORTS_DIR / (str(run_date) + ".md")
    rts = datetime.fromtimestamp(rep.stat().st_mtime) if rep.is_file() else None
    ps = q_one(conn, "SELECT total, drawdown FROM portfolio_state WHERE date=?", (run_date,))
    stages.append(_stage(
        "review", "盘后复盘", "盯市+日报+归因",
        "ok" if rts else "idle",
        "日报 %s；总资产 %s" % (rts.strftime("%m-%d %H:%M") if rts else "未生成",
                               fnum(ps["total"]) if ps else "—"), _iso(rts)))

    # 决策追踪链
    traces: List[dict] = []
    for d in decs:
        ev = q_all(conn, "SELECT ts, rule, detail FROM risk_event WHERE decision_id=? ORDER BY ts",
                   (d["id"],))
        t = q_one(conn, "SELECT id, side, price, shares, amount, status, confirmed_by "
                        "FROM trade WHERE decision_id=? ORDER BY id LIMIT 1", (d["id"],))
        traces.append({
            "id": d["id"], "code": d["code"], "action": d["action"],
            "target_weight": d["target_weight"], "confidence": d["confidence"],
            "status": d["status"], "created_at": d["created_at"],
            "reasons": parse_json_field(d["reasons"], []),
            "risk_events": ev,
            "trade": t,
            "pending": (ORDERS_DIR / str(run_date) / ("pending_%d.json" % d["id"])).is_file(),
        })
    ks = q_one(conn, "SELECT kill_switch, drawdown FROM portfolio_state "
                     "WHERE date=(SELECT MAX(date) FROM portfolio_state)")
    return {"run_date": run_date, "generated_at": _iso(datetime.now()),
            "stages": stages, "traces": traces,
            "kill_switch": bool(ks and ks["kill_switch"]),
            "health_issues": issues}


DOC_FILES = {
    "decision_playbook": BASE / "docs" / "决策策略与工作流.md",
    "strategy_lib": BASE / "docs" / "策略库.md",
}


def api_doc(qs: dict) -> dict:
    name = str(qs.get("name", [""])[0])
    path = DOC_FILES.get(name)
    if path is None:
        raise ValueError("未知文档: %s" % name)
    if not path.is_file():
        return {"name": name, "missing": True}
    return {"name": name, "markdown": path.read_text(encoding="utf-8", errors="replace")}


# ---------------------------------------------------------------- 自选股概念分组

def api_concepts(conn: sqlite3.Connection, qs: dict) -> dict:
    """按概念分组返回自选股，附最新收盘/涨跌幅/信号分/黑名单状态。

    分组来自 config.watchlist[].concepts（可多归属）；无标签的归入"未分组"。
    """
    wl = load_config().get("watchlist", [])
    bl = {b["code"]: b for b in blacklist_of(conn)}
    sig = {r["code"]: r for r in api_signals(conn, {})}
    bars = {}
    for code, td, close, pct in conn.execute(
            "SELECT code, trade_date, close, pct_chg FROM daily_bar "
            "WHERE (code, trade_date) IN (SELECT code, MAX(trade_date) FROM daily_bar GROUP BY code)"):
        bars[code] = {"date": td, "close": close, "pct_chg": pct}

    def stock_row(code: str, name: str) -> dict:
        s = sig.get(code) or {}
        sg = s.get("signals") or {}
        bar = bars.get(code) or {}
        b = bl.get(code) or {}
        return {"code": code, "name": name,
                "close": bar.get("close"), "pct_chg": bar.get("pct_chg"),
                "bar_date": bar.get("date"),
                "score": s.get("score"), "ma_trend": sg.get("ma_trend"),
                "rsi_14": sg.get("rsi_14"), "mom_20d": sg.get("mom_20d"),
                "blacklisted": (not b.get("ok", True)) if code in bl else False,
                "blacklist_reason": b.get("reason", "")}

    groups: Dict[str, list] = {}
    order: List[str] = []
    ungrouped: List[dict] = []
    for w in wl:
        row = stock_row(str(w.get("code")), str(w.get("name") or ""))
        tags = w.get("concepts") or []
        if not tags:
            ungrouped.append(row)
        for t in tags:
            if t not in groups:
                groups[t] = []
                order.append(t)
            groups[t].append(row)
    out = [{"name": t, "stocks": groups[t]} for t in order]
    if ungrouped:
        out.append({"name": "未分组", "stocks": ungrouped})
    return {"total": len(wl), "concepts": out}


# ---------------------------------------------------------------- 动态池（异动/热门）

def api_dynamic_pools(conn: sqlite3.Connection, qs: dict) -> dict:
    """当前异动池/热门池成员 + 最新一次刷新的板块榜（如已缓存）。"""
    from signals import dynpool, hot as hot_mod
    movers = dynpool.current_pool(conn, "movers")
    themes = dynpool.current_pool(conn, "hot_theme")
    stocks = dynpool.current_pool(conn, "hot_stock")
    # 概念板块榜是易变增强数据不入库，现算（失败为空）
    boards = []
    if qs.get("with_boards", ["0"])[0] == "1":  # 默认关：东财挂时首次调用也要 15s（审查P2-3）
        try:
            boards = hot_mod.board_hot()
        except Exception:  # noqa: BLE001
            boards = []
    dates = {p: dynpool.pool_dates(conn, p) for p in dynpool.POOLS}
    return {"movers": movers, "hot_theme": themes, "hot_stock": stocks,
            "boards": boards, "dates": dates,
            "updated_at": max(dates[p][0] for p in dynpool.POOLS
                              if dates[p]) if any(dates.values()) else None}


# ---------------------------------------------------------------- HTTP 服务

GET_ROUTES = {
    "/api/overview": lambda c, qs: api_overview(c, qs),
    "/api/equity_curve": lambda c, qs: api_equity_curve(c, qs),
    "/api/candles": lambda c, qs: api_candles(c, qs),
    "/api/signals": lambda c, qs: api_signals(c, qs),
    "/api/decisions": lambda c, qs: api_decisions(c, qs),
    "/api/trades": lambda c, qs: api_trades(c, qs),
    "/api/risk_events": lambda c, qs: api_risk_events(c, qs),
    "/api/news": lambda c, qs: api_news(c, qs),
    "/api/macro": lambda c, qs: api_macro(c, qs),
    "/api/macro_history": lambda c, qs: api_macro_history(c, qs),
    "/api/health": lambda c, qs: api_health(c, qs),
    "/api/reports": lambda c, qs: api_reports(qs),
    "/api/report": lambda c, qs: api_report(qs),
    "/api/logs": lambda c, qs: api_logs(qs),
    "/api/pending": lambda c, qs: api_pending(qs),
    "/api/sessions": lambda c, qs: api_sessions(qs),
    "/api/session": lambda c, qs: api_session(qs),
    "/api/backtest": lambda c, qs: api_backtest(qs),
    "/api/workflow": lambda c, qs: api_workflow(c, qs),
    "/api/doc": lambda c, qs: api_doc(qs),
    "/api/concepts": lambda c, qs: api_concepts(c, qs),
    "/api/dynamic_pools": lambda c, qs: api_dynamic_pools(c, qs),
}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "AgSickleDashboard/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 输出
    def _send_bytes(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _err(self, status: int, msg: str) -> None:
        self._send_json({"error": msg}, status)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write("[%s] %s %s\n" % (datetime.now().strftime("%H:%M:%S"),
                                           self.address_string(), fmt % args))

    # ---- 静态资源
    def _serve_static(self, path: str) -> None:
        rel = unquote(path).lstrip("/")
        if rel == "":
            rel = "index.html"
        target = safe_join(STATIC_DIR, rel)
        if target is None or not target.is_file():
            self._err(404, "静态资源不存在: %s" % rel)
            return
        suffix = target.suffix.lower()
        ctype = MIME_OVERRIDE.get(suffix) or mimetypes.guess_type(target.name)[0] \
            or "application/octet-stream"
        self._send_bytes(200, target.read_bytes(), ctype)

    # ---- 路由
    _ALLOWED_HOSTS = ("127.0.0.1", "localhost")

    def _host_ok(self) -> bool:
        """Host 白名单：GET/POST 统一校验，防 DNS rebinding 读走组合数据。"""
        host = (self.headers.get("Host") or "").lower()
        return any(host == h or host.startswith(h + ":") for h in self._ALLOWED_HOSTS)

    def do_GET(self) -> None:  # noqa: N802
        try:
            if not self._host_ok():
                self._err(403, "仅允许本地 Host 访问（got Host=%r）"
                          % (self.headers.get("Host"),))
                return
            parts = urlsplit(self.path)
            qs = parse_qs(parts.query)
            route = GET_ROUTES.get(parts.path)
            if route is not None:
                conn = get_conn()
                try:
                    self._send_json(route(conn, qs))
                finally:
                    conn.close()
                return
            if parts.path.startswith("/api/"):
                self._err(404, "未知接口 %s" % parts.path)
                return
            self._serve_static(parts.path)
        except FileNotFoundError as e:
            self._err(404, str(e))
        except (ValueError,) as e:
            self._err(400, str(e))
        except Exception:
            traceback.print_exc()
            self._err(500, "服务器内部错误: %s" % traceback.format_exc(limit=1).strip())

    def do_POST(self) -> None:  # noqa: N802
        try:
            parts = urlsplit(self.path)
            if parts.path not in ("/api/confirm", "/api/reject"):
                self._err(404, "未知写接口 %s" % parts.path)
                return
            # 写操作来源防护（审查 P1-1）：防 CSRF / DNS-rebinding 触发下单动作
            if not self._host_ok():
                self._err(403, "写接口仅允许本地 Host 访问（got Host=%r）"
                          % (self.headers.get("Host"),))
                return
            origin = self.headers.get("Origin")
            if origin:
                # 精确 hostname 比对（此前 startswith 前缀校验可被
                # http://127.0.0.1.evil.com 绕过）
                try:
                    oh = (urlsplit(origin).hostname or "").lower()
                except ValueError:
                    oh = ""
                if oh not in self._ALLOWED_HOSTS:
                    self._err(403, "跨源写请求已拒绝（Origin=%r）" % origin)
                    return
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._err(415, "Content-Type 须为 application/json（got %r）" % ctype)
                return
            raw_len = int(self.headers.get("Content-Length") or 0)
            if raw_len > (1 << 20):
                self._err(413, "请求体超过 1MB 上限")
                self.close_connection = True   # 审查P3-2：不消费超限body，直接断开防请求混淆
                return
            length = raw_len
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(body, dict):
                    raise ValueError("body 必须为 JSON 对象")
            except (ValueError, UnicodeDecodeError) as e:
                self._err(400, "请求体解析失败: %s" % e)
                return
            if parts.path == "/api/confirm":
                self._send_json(api_confirm(body))
            else:
                self._send_json(api_reject(body))
        except subprocess.TimeoutExpired:
            self._err(504, "runner.py 执行超时（60s）")
        except FileNotFoundError as e:
            self._err(404, str(e))
        except (ValueError,) as e:
            self._err(400, str(e))
        except Exception:
            traceback.print_exc()
            self._err(500, "服务器内部错误: %s" % traceback.format_exc(limit=1).strip())


def main() -> int:
    ap = argparse.ArgumentParser(description="A股镰刀手 · AI交易员看板（本地仪表盘）")
    ap.add_argument("--port", type=int, default=8317, help="监听端口（默认 8317）")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    url = "http://%s:%d/" % (args.host, args.port)
    print("=" * 56)
    print("  A股镰刀手 · AI交易员看板 已启动")
    print("  访问地址: %s" % url)
    print("  数据库  : %s" % db_file())
    print("  静态目录: %s" % STATIC_DIR)
    print("  按 Ctrl+C 停止")
    print("=" * 56)
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
