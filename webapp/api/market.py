"""webapp/api 拆分模块（结构性重构 Phase 6，纯结构搬移——不改 SQL 语义与响应 JSON 形状）。"""
"""行情/信号/宏观/总览类只读接口。"""
import bisect
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from webapp.api.common import (bench_close_at, blacklist_of, clamp_int, fnum,
                               health_issues_of, latest_bar, parse_json_field, q_all, q_one)

INDEX_NAMES = {"000300": "沪深300", "000905": "中证500", "000001": "上证指数"}


def api_overview(conn: sqlite3.Connection, qs: dict) -> dict:
    from webapp import server as _srv
    cfg = _srv.load_config()
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

