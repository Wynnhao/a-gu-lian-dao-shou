"""数据层：行情拉取 + SQLite 入库（增量）。"""
import json
import logging
import sqlite3
import time
from datetime import datetime, date, timedelta
from pathlib import Path

import akshare as ak
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(BASE / "logs" / "fetch.log", encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("fetcher")

DDL = """
CREATE TABLE IF NOT EXISTS daily_bar (
    code TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL, pct_chg REAL, turnover REAL,
    PRIMARY KEY (code, trade_date)
);
CREATE TABLE IF NOT EXISTS stock_info (
    code TEXT PRIMARY KEY, name TEXT, first_trade_date TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS fetch_log (
    code TEXT, run_at TEXT, status TEXT, rows INT, detail TEXT
);
-- P1.4 资讯：code='' 表示宏观/市场级新闻
CREATE TABLE IF NOT EXISTS news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT, title TEXT, content TEXT, source TEXT, url TEXT,
    published_at TEXT, fetched_at TEXT,
    UNIQUE(code, title, published_at)
);
-- P1.5 指数行情与估值分位
CREATE TABLE IF NOT EXISTS index_daily (
    index_code TEXT, trade_date TEXT, close REAL,
    PRIMARY KEY (index_code, trade_date)
);
CREATE TABLE IF NOT EXISTS index_valuation (
    index_code TEXT, trade_date TEXT,
    pe REAL, pe_pct REAL, pb REAL, pb_pct REAL, close REAL,
    PRIMARY KEY (index_code, trade_date)
);
-- P2 信号：signals 为因子 JSON，score 为综合分
CREATE TABLE IF NOT EXISTS signal (
    code TEXT, as_of TEXT, signals TEXT, score REAL,
    PRIMARY KEY (code, as_of)
);
-- P3 决策：input_snapshot 保存完整输入快照用于归因
CREATE TABLE IF NOT EXISTS decision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT, code TEXT, action TEXT, target_weight REAL,
    confidence REAL, reasons TEXT, risk_notes TEXT,
    input_snapshot TEXT, status TEXT, created_at TEXT
);
-- P4 持仓（本地镜像，T+1 由 avail_shares 体现）与成交
CREATE TABLE IF NOT EXISTS position (
    code TEXT PRIMARY KEY, name TEXT, shares INT, avail_shares INT,
    cost REAL, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS trade (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT, code TEXT, name TEXT, side TEXT,
    price REAL, shares INT, amount REAL,
    order_id TEXT, status TEXT, decision_id INT,
    shots TEXT, confirmed_by TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS portfolio_state (
    date TEXT PRIMARY KEY, cash REAL, market_value REAL, total REAL,
    drawdown REAL, kill_switch INT, note TEXT
);
CREATE TABLE IF NOT EXISTS risk_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, rule TEXT, detail TEXT, decision_id INT
);
-- 动态池（异动池/热门池）：按刷新日期留痕，当前成员=各票最新 added_date 行
CREATE TABLE IF NOT EXISTS dynamic_pool (
    pool TEXT, code TEXT, name TEXT, reason TEXT,
    strength REAL, added_date TEXT, updated_at TEXT,
    PRIMARY KEY (pool, code, added_date)
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(BASE / CFG["db_path"])
    conn.executescript(DDL)
    return conn


def _hist_em(code: str, start: str, end: str) -> pd.DataFrame:
    """东财源，统一为通用列名。"""
    df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start,
                            end_date=end, adjust="")
    if df is None or df.empty:
        return df
    return pd.DataFrame({
        "date": pd.to_datetime(df["日期"]),
        "open": df["开盘"], "high": df["最高"], "low": df["最低"],
        "close": df["收盘"], "volume": df["成交量"], "amount": df["成交额"],
        "turnover": df["换手率"] / 100.0,
    })


def _hist_tx(code: str, start: str, end: str) -> pd.DataFrame:
    """腾讯源兜底；涨跌幅由前收盘推算。"""
    prefix = ("sh" if code.startswith(("6", "9")) else
              "sz" if code.startswith(("0", "3")) else "bj")
    df = ak.stock_zh_a_hist_tx(symbol=f"{prefix}{code}", start_date=start,
                               end_date=end, adjust="")
    if df is None or df.empty:
        return df
    pct = df["close"].pct_change() * 100
    return pd.DataFrame({
        "date": pd.to_datetime(df["date"]),
        "open": df["open"], "high": df["high"], "low": df["low"],
        "close": df["close"], "volume": df["volume"], "amount": df["amount"],
        "turnover": df["turnover"] if "turnover" in df else None,
        "pct_chg": pct,
    })


def fetch_daily(code: str, conn: sqlite3.Connection) -> int:
    """增量拉取单只股票日K（东财优先，腾讯兜底），返回新增行数。"""
    last = conn.execute(
        "SELECT MAX(trade_date) FROM daily_bar WHERE code=?", (code,)
    ).fetchone()[0]
    start = "20240101"
    if last:
        start = (pd.Timestamp(last) + pd.Timedelta(days=1)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    # 审查 P1-1：交易时段内东财会返回当日未走完的部分 bar，且增量机制
    # （start=last+1）导致该半根 bar 永不被重取覆盖——盘中一律截到昨日。
    try:
        from data.quotes import is_trading_time
        if is_trading_time():
            end = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    except Exception:
        pass

    df = None
    for fn in (_hist_em, _hist_tx):
        try:
            df = fn(code, start, end)
            if df is not None and not df.empty:
                break
        except Exception as e:
            log.warning("%s via %s fail: %s", code, fn.__name__, repr(e)[:80])
    if df is None or df.empty:
        return 0
    if "pct_chg" not in df.columns:
        df["pct_chg"] = df["close"].pct_change() * 100
    rows = [
        (code, r["date"].strftime("%Y-%m-%d"), float(r["open"]), float(r["high"]),
         float(r["low"]), float(r["close"]), float(r["volume"]), float(r["amount"]),
         float(r["pct_chg"]) if pd.notna(r["pct_chg"]) else 0.0,
         float(r["turnover"]) * 100 if pd.notna(r.get("turnover")) else 0.0)
        for _, r in df.iterrows()
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO daily_bar VALUES (?,?,?,?,?,?,?,?,?,?)", rows
    )
    return len(rows)


def upsert_info(item: dict, conn: sqlite3.Connection):
    code, name = item["code"], item["name"]
    first = conn.execute(
        "SELECT MIN(trade_date) FROM daily_bar WHERE code=?", (code,)
    ).fetchone()[0]
    conn.execute(
        "INSERT OR REPLACE INTO stock_info VALUES (?,?,?,?)",
        (code, name, first, datetime.now().isoformat(timespec="seconds")),
    )


def run():
    conn = get_conn()
    for item in CFG["watchlist"]:
        code = item["code"]
        try:
            n = fetch_daily(code, conn)
            upsert_info(item, conn)
            conn.execute("INSERT INTO fetch_log VALUES (?,?,?,?,?)",
                         (code, datetime.now().isoformat(timespec="seconds"),
                          "ok", n, ""))
            log.info("%s %s: +%d rows", code, item["name"], n)
        except Exception as e:
            conn.execute("INSERT INTO fetch_log VALUES (?,?,?,?,?)",
                         (code, datetime.now().isoformat(timespec="seconds"),
                          "fail", 0, str(e)[:200]))
            log.error("%s FAIL: %s", code, e)
        conn.commit()
        time.sleep(1)  # 温和限速
    conn.close()


if __name__ == "__main__":
    run()
