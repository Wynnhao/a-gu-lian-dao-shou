"""P1.5 指数日线 + PE/PB 历史分位入库（幂等：日线 OR IGNORE，估值 OR REPLACE）。"""
import argparse
import logging
import logging.handlers
import os
import sys
import time
from datetime import date
from pathlib import Path

import akshare as ak
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from data.fetcher import get_conn  # noqa: E402

log = logging.getLogger("macro")
log.setLevel(logging.INFO)
if not log.handlers:  # 避免与 fetcher 的 basicConfig 重复挂 handler
    log.addHandler(logging.handlers.RotatingFileHandler(BASE / "logs" / "macro.log", encoding="utf-8", maxBytes=5_000_000, backupCount=3))
    log.addHandler(logging.StreamHandler())
log.propagate = False

INDEX_CODES = ["000300", "000905", "000001"]  # 6位码，无后缀

# 指数交易所前缀显式映射（此前 startswith(("000","8")) 的推断对深市指数 399xxx 会错判）
_INDEX_SYMBOL_PREFIX = {
    "000300": "sh", "000905": "sh", "000001": "sh",
    "399001": "sz", "399005": "sz", "399006": "sz", "399330": "sz",
}


def _index_symbol(code: str) -> str:
    return _INDEX_SYMBOL_PREFIX.get(code, "sh") + code

# 估值源按序尝试：(akshare函数名, symbol, 数值列)。legu 指数PE/PB 无"上证"，
# 上证指数(000001) 用 stock_market_pe_lg 的市场平均市盈率做备选，再不行走价格分位兜底。
_PE_CHAINS = {
    "000300": [("stock_index_pe_lg", "沪深300", "滚动市盈率")],
    "000905": [("stock_index_pe_lg", "中证500", "滚动市盈率")],
    "000001": [("stock_index_pe_lg", "上证", "滚动市盈率"),
               ("stock_market_pe_lg", "上证", "平均市盈率")],
}
_PB_CHAINS = {
    "000300": [("stock_index_pb_lg", "沪深300", "市净率")],
    "000905": [("stock_index_pb_lg", "中证500", "市净率")],
    "000001": [("stock_index_pb_lg", "上证", "市净率"),
               ("stock_market_pb_lg", "上证", "市净率")],
}


def pct_rank(series: pd.Series) -> pd.Series:
    """在全部可得历史中的百分位（0~1，pandas rank 平均法；NaN 输入保持 NaN）。"""
    return series.rank(method="average", pct=True)


def rolling_price_pct(dates: list, closes: list, years: int = 5) -> list:
    """每个交易日收盘价在过去 N 年滚动窗口内的价格分位（0~1，含自身）。

    与 dates/closes 等长返回；close 缺失（NaN/None）对应位置为 None。
    """
    s = pd.Series(pd.to_numeric(pd.Series(closes), errors="coerce").values,
                  index=pd.to_datetime(pd.Series(dates)))
    s = s.dropna()
    if s.empty:
        return [None] * len(dates)
    window = f"{int(years * 365.25)}D"
    pct = s.rolling(window, min_periods=1).apply(
        lambda w: float((w <= w.iloc[-1]).mean()), raw=False)
    lookup = {t.strftime("%Y-%m-%d"): p for t, p in pct.items()}
    out = []
    for d in dates:
        key = str(d)[:10]
        out.append(float(lookup[key]) if key in lookup else None)
    return out


def _daily_src(src: str, code: str, start: str, end: str) -> list:
    """单指数日线 -> [(index_code, 'YYYY-MM-DD', close)]，仅取 start 之后。"""
    if src == "index_zh_a_hist":  # 东财，symbol 不带后缀，中文列 日期/收盘
        df = ak.index_zh_a_hist(symbol=code, period="daily",
                                start_date=start, end_date=end)
        if df is None or df.empty:
            return []
        rows = [(code, pd.Timestamp(d).strftime("%Y-%m-%d"), float(c))
                for d, c in zip(df["日期"], df["收盘"])]
    else:  # 新浪/腾讯兜底，symbol 需带交易所前缀
        sym = _index_symbol(code)
        df = (ak.stock_zh_index_daily(symbol=sym) if src == "stock_zh_index_daily"
              else ak.stock_zh_index_daily_tx(symbol=sym))
        if df is None or df.empty:
            return []
        rows = [(code, pd.Timestamp(d).strftime("%Y-%m-%d"), float(c))
                for d, c in zip(df["date"], df["close"])]
    cutoff = pd.Timestamp(start).strftime("%Y-%m-%d")
    return [r for r in rows if r[1] >= cutoff]


def fetch_index_daily(codes=None, start: str = "20180101") -> dict:
    """各指数日线入库 index_daily，依次尝试 东财/新浪/腾讯 源，返回 {code: 新增行数}。"""
    if codes is None:
        codes = list(INDEX_CODES)
    end = date.today().strftime("%Y%m%d")
    conn = get_conn()
    out = {}
    for code in codes:
        new = 0
        for src in ("index_zh_a_hist", "stock_zh_index_daily", "stock_zh_index_daily_tx"):
            try:
                rows = _daily_src(src, code, start, end)
                time.sleep(0.8)  # 温和限速
            except Exception as e:
                log.warning("index_daily %s via %s FAIL: %s", code, src, repr(e)[:140])
                continue
            if not rows:
                log.warning("index_daily %s via %s 返回空", code, src)
                continue
            cnt_before = conn.execute(
                "SELECT COUNT(*) FROM index_daily WHERE index_code=?", (code,)
            ).fetchone()[0]
            conn.executemany("INSERT OR IGNORE INTO index_daily VALUES (?, ?, ?)", rows)
            conn.commit()
            new = conn.execute(
                "SELECT COUNT(*) FROM index_daily WHERE index_code=?", (code,)
            ).fetchone()[0] - cnt_before
            log.info("index_daily %s via %s: %d 行, 新增 %d", code, src, len(rows), new)
            break
        if not new and conn.execute(
                "SELECT 1 FROM index_daily WHERE index_code=? LIMIT 1", (code,)
        ).fetchone() is None:
            log.error("index_daily %s 全部数据源失败", code)
        out[code] = new
    conn.close()
    return out


def _fetch_series(chain: list):
    """按链尝试估值接口 -> (DataFrame(date,value,close), 实际接口名) 或 (None, None)。"""
    for fn_name, sym, val_col in chain:
        try:
            df = getattr(ak, fn_name)(symbol=sym)
            time.sleep(0.8)  # 温和限速
            if df is None or df.empty or "日期" not in df.columns or val_col not in df.columns:
                log.warning("%s(%s) 返回空或缺列 %s", fn_name, sym, val_col)
                continue
            out = pd.DataFrame({
                "date": pd.to_datetime(df["日期"]).dt.strftime("%Y-%m-%d"),
                "value": pd.to_numeric(df[val_col], errors="coerce"),
            })
            # legu 系接口自带"指数"点位列，可作 close 的次级来源；缺失列置 NaN
            out["close"] = (pd.to_numeric(df["指数"], errors="coerce")
                            if "指数" in df.columns else float("nan"))
            out = out.dropna(subset=["value"])
            log.info("%s(%s): %d 行", fn_name, sym, len(out))
            return out, fn_name
        except Exception as e:
            log.warning("%s(%s) FAIL: %s", fn_name, sym, repr(e)[:140])
            time.sleep(0.5)
    return None, None


def _write_valuation(code: str, conn, vals: dict) -> int:
    """INSERT OR REPLACE 写 index_valuation，vals: {date: (pe, pe_pct, pb, pb_pct, close)}。"""
    rows = [(code, d) + v for d, v in sorted(vals.items())]
    conn.executemany("INSERT OR REPLACE INTO index_valuation VALUES (?, ?, ?, ?, ?, ?, ?)",
                     rows)
    conn.commit()
    return len(rows)


def _price_percentile_fallback(code: str, conn, years: int) -> int:
    """估值源全部失败：用 index_daily close 算近5年滚动价格分位，pe/pb 置 NULL。"""
    rows = conn.execute(
        "SELECT trade_date, close FROM index_daily WHERE index_code=? ORDER BY trade_date",
        (code,)).fetchall()
    if not rows:
        log.error("价格分位兜底 %s 无 index_daily 数据可算", code)
        return 0
    dates = [r[0] for r in rows]
    closes = [r[1] for r in rows]
    pcts = rolling_price_pct(dates, closes, years=years)
    vals = {d: (None, p, None, None, c)
            for d, c, p in zip(dates, closes, pcts) if p is not None}
    n = _write_valuation(code, conn, vals)
    log.info("%s 价格分位口径: %d 行（pe/pb 置 NULL, pe_pct=近%d年价格分位）",
             code, n, years)
    return n


def _valuation_one(code: str, conn, years: int = 5) -> dict:
    pe_df, pe_src = _fetch_series(_PE_CHAINS.get(code, []))
    pb_df, pb_src = _fetch_series(_PB_CHAINS.get(code, []))
    daily = dict(conn.execute(
        "SELECT trade_date, close FROM index_daily WHERE index_code=?",
        (code,)).fetchall())

    if pe_df is None and pb_df is None:
        n = _price_percentile_fallback(code, conn, years)
        return {"mode": "价格分位口径", "pe_source": None, "pb_source": None, "rows": n}

    pe_val, pe_pct, pe_close = {}, {}, {}
    pb_val, pb_pct, pb_close = {}, {}, {}
    if pe_df is not None:
        pe_val = dict(zip(pe_df["date"], pe_df["value"]))
        pe_pct = dict(zip(pe_df["date"], pct_rank(pe_df["value"])))
        pe_close = dict(zip(pe_df["date"], pe_df["close"]))
    if pb_df is not None:
        pb_val = dict(zip(pb_df["date"], pb_df["value"]))
        pb_pct = dict(zip(pb_df["date"], pct_rank(pb_df["value"])))
        pb_close = dict(zip(pb_df["date"], pb_df["close"]))

    vals = {}
    for d in sorted(set(pe_val) | set(pb_val)):
        close = daily.get(d)
        if close is None or pd.isna(close):  # index_daily 没有则用估值接口自带点位
            close = pe_close.get(d)
            if close is None or pd.isna(close):
                close = pb_close.get(d)
        vals[d] = (pe_val.get(d), pe_pct.get(d),
                   pb_val.get(d), pb_pct.get(d),
                   float(close) if close is not None and pd.notna(close) else None)
    n = _write_valuation(code, conn, vals)
    log.info("index_valuation %s: %d 行 (pe源=%s, pb源=%s, PE历史分位口径)",
             code, n, pe_src, pb_src)
    return {"mode": "PE历史分位", "pe_source": pe_src, "pb_source": pb_src, "rows": n}


def fetch_index_valuation(codes=None, years: int = 5) -> dict:
    """各指数估值分位入库，单指数失败降级（价格分位口径），返回 {code: 摘要dict}。"""
    # 测试逃生门：短路指数估值网络面（调用时读 env）
    if os.environ.get("AGSICKLE_DISABLE_MACRO") == "1":
        log.info("AGSICKLE_DISABLE_MACRO=1，跳过指数估值采集")
        return {}
    if codes is None:
        codes = list(INDEX_CODES)
    conn = get_conn()
    out = {}
    for code in codes:
        try:
            out[code] = _valuation_one(code, conn, years=years)
        except Exception as e:
            log.error("index_valuation %s FAIL: %s", code, repr(e)[:200])
            out[code] = {"mode": "失败", "pe_source": None, "pb_source": None, "rows": 0}
        time.sleep(0.5)
    conn.close()
    return out


def main():
    print("== P1.5 指数日线入库 ==")
    for code, n in fetch_index_daily().items():
        print(f"  {code}: +{n} 行")
    print("== P1.5 指数估值分位入库 ==")
    for code, r in fetch_index_valuation().items():
        print(f"  {code}: {r}")


if __name__ == "__main__":
    main()
