"""交易日历：新浪历史交易日表缓存到 trade_calendar，查询失败退化为 weekday 判断。

此前节假日全靠 weekday 硬判断：国庆/春节的周一会照常跑流水线、用陈旧价出决策。
接入日历后 premarket/catchup 的交易日判断引用本模块（风控时段判断用的是
risk.engine 的秒级时段窗，与本模块无关）；日历拉取失败不阻塞主流程
（降级为 weekday，与旧行为一致，audit/health 会标注日历未覆盖）。
"""
import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

log = logging.getLogger("calendar")


def ensure_calendar(conn: sqlite3.Connection, horizon_days: int = 200) -> int:
    """拉取并缓存交易日历，返回日历总行数；已有覆盖到今年年底的数据则跳过。

    best-effort：网络失败返回当前行数（可能为 0，调用方退化为 weekday 判断）。
    """
    try:
        have = conn.execute("SELECT MAX(date) FROM trade_calendar").fetchone()[0]
        need_until = (date.today() + timedelta(days=horizon_days)).strftime("%Y-%m-%d")
        if have and str(have) >= need_until:
            return conn.execute("SELECT COUNT(*) FROM trade_calendar").fetchone()[0]
        df = _fetch()
        if df is None or df.empty:
            return conn.execute("SELECT COUNT(*) FROM trade_calendar").fetchone()[0]
        rows = []
        for v in df.iloc[:, 0]:  # 新浪接口单列 trade_date
            try:
                rows.append((pd.Timestamp(v).strftime("%Y-%m-%d"),))
            except Exception:  # noqa: BLE001
                continue
        conn.executemany("INSERT OR IGNORE INTO trade_calendar VALUES (?)", rows)
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM trade_calendar").fetchone()[0]
        log.info("交易日历入库: %d 行（覆盖至 %s）", n, rows[-1][0] if rows else "?")
        return n
    except Exception as e:  # noqa: BLE001
        log.warning("交易日历拉取失败（退化 weekday 判断）: %s", repr(e)[:120])
        return conn.execute("SELECT COUNT(*) FROM trade_calendar").fetchone()[0]


def _fetch():
    import akshare as ak
    return ak.tool_trade_date_hist_sina()


def is_trading_day(conn: sqlite3.Connection, d: date = None) -> bool:
    """d 是否交易日：日历有数据按日历，否则退化为周一~五（旧行为）。"""
    d = d or date.today()
    key = d.strftime("%Y-%m-%d")
    covered = conn.execute(
        "SELECT 1 FROM trade_calendar WHERE date BETWEEN ? AND ? LIMIT 1",
        (f"{d.year}-01-01", f"{d.year}-12-31")).fetchone()
    if covered:
        return conn.execute(
            "SELECT 1 FROM trade_calendar WHERE date=? LIMIT 1", (key,)).fetchone() is not None
    return d.weekday() < 5


def recent_trade_days(conn: sqlite3.Connection, n: int, end: str = None) -> list:
    """截至 end（含）的最近 n 个交易日（YYYY-MM-DD 升序）；无日历时按工作日近似。"""
    end = end or date.today().strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT date FROM trade_calendar WHERE date<=? ORDER BY date DESC LIMIT ?",
        (end, n)).fetchall()
    if rows:
        return sorted(r[0] for r in rows)
    # 退化：往前找 n 个工作日
    out, cur = [], datetime.strptime(end, "%Y-%m-%d").date()
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.strftime("%Y-%m-%d"))
        cur -= timedelta(days=1)
    return sorted(out)
