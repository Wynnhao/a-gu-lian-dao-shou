"""P1.5 指数日线 + PE/PB 历史分位入库（幂等：日线 OR IGNORE，估值 OR REPLACE）。"""
import argparse
import logging
import logging.handlers
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import akshare as ak
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from data.fetcher import get_conn, call_ak  # noqa: E402  (Sprint 2 任务1: call_ak 熔断接入)

log = logging.getLogger("macro")
log.setLevel(logging.INFO)
if not log.handlers:  # 避免与 fetcher 的 basicConfig 重复挂 handler
    from common.logsetup import rotating_handler  # AGSICKLE_LOG_DIR 逃生门（Sprint4 W0-1）
    log.addHandler(rotating_handler("macro.log"))
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

# P1-9（批次3b）：index_valuation.valuation_mode 行级来源标记——bundle/webapp
# 据此区分"PE/PB 历史分位"与"价格分位兜底"两种 pe_pct 语义（此前 fail-open：
# 兜底行把价格分位写进 pe_pct 且库内无来源标记，消费方无法辨识）。
VALUATION_MODE_REAL = "real"                # PE/PB 历史分位（legu 系接口）
VALUATION_MODE_FALLBACK = "price_fallback"  # 估值源全挂 → 近5年滚动价格分位兜底


def _valuation_has_mode_col(conn) -> bool:
    """index_valuation 是否已有 valuation_mode 列（生产库加列待授权——授权前
    老库无该列，写路径自动退回 7 列模式，行为零变化）。"""
    return "valuation_mode" in {
        r[1] for r in conn.execute("PRAGMA table_info(index_valuation)")}


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


# W-B2（Sprint4，P1-9）：原 fetch_index_daily 死函数及其 _daily_src 助手已删除——
# 其 `INSERT OR IGNORE INTO index_daily VALUES (?,?,?)` 为 3 列 vs 表 5 列
# (index_code, trade_date, close, high, low)，EXPLAIN 实测必报错，且 OR IGNORE
# 语义弱于 fetcher.ensure_index_daily 的 OR REPLACE（无盘中截断、无三源兜底、
# 不补 high/low）。统一入口：fetcher.ensure_index_daily(conn, code) 逐码调用。


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


def _write_valuation(code: str, conn, vals: dict,
                     mode: str = VALUATION_MODE_REAL) -> int:
    """INSERT OR REPLACE 写 index_valuation，vals: {date: (pe, pe_pct, pb, pb_pct, close)}。

    P1-9（批次3b）：列名显式写入；表已有 valuation_mode 列时逐行标注来源 mode
    （real / price_fallback），老库（待授权加列）自动退回 7 列写法零行为变化。
    """
    if _valuation_has_mode_col(conn):
        rows = [(code, d) + v + (mode,) for d, v in sorted(vals.items())]
        conn.executemany(
            "INSERT OR REPLACE INTO index_valuation "
            "(index_code, trade_date, pe, pe_pct, pb, pb_pct, close, valuation_mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    else:
        rows = [(code, d) + v for d, v in sorted(vals.items())]
        conn.executemany("INSERT OR REPLACE INTO index_valuation VALUES (?, ?, ?, ?, ?, ?, ?)",
                         rows)
    conn.commit()
    return len(rows)


def _price_percentile_fallback(code: str, conn, years: int) -> int:
    """估值源全部失败：用 index_daily close 算近5年滚动价格分位，pe/pb 置 NULL。

    P1-9（批次3b）：fail-open → 至少可辨识——兜底行 valuation_mode='price_fallback'
    （pe_pct 语义是价格分位而非估值分位），且日志升 warning；bundle 据此降级披露。
    """
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
    n = _write_valuation(code, conn, vals, mode=VALUATION_MODE_FALLBACK)
    log.warning("%s 价格分位兜底口径: %d 行（pe/pb 置 NULL, pe_pct=近%d年价格分位，"
                "valuation_mode=%s）", code, n, years, VALUATION_MODE_FALLBACK)
    return n


def _valuation_one(code: str, conn, years: int = 5) -> dict:
    pe_df, pe_src = _fetch_series(_PE_CHAINS.get(code, []))
    pb_df, pb_src = _fetch_series(_PB_CHAINS.get(code, []))
    daily = dict(conn.execute(
        "SELECT trade_date, close FROM index_daily WHERE index_code=?",
        (code,)).fetchall())

    if pe_df is None and pb_df is None:
        n = _price_percentile_fallback(code, conn, years)
        return {"mode": "价格分位口径", "valuation_mode": VALUATION_MODE_FALLBACK,
                "pe_source": None, "pb_source": None, "rows": n}

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
    return {"mode": "PE历史分位", "valuation_mode": VALUATION_MODE_REAL,
            "pe_source": pe_src, "pb_source": pb_src, "rows": n}


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


# ============================================================
# Sprint 2 任务 1（P1-3）：10Y 国债收益率 + ETF 份额
# ============================================================

BOND_CODES = ["10Y_CN"]                   # 10 年期国债收益率（CGB 10Y）
ETF_CODES = ["510300", "510500"]          # 沪深300ETF + 中证500ETF


def _fetch_bond_yield_one(issuer: str) -> list:
    """单源拉 10Y 国债收益率日频。返回 [(trade_date, yield, source), ...]。

    issuer ∈ {"em", "tx"}：
    - em: ak.bond_zh_us_rate()（中国国债收益率曲线）
    - tx: 暂用 ak.bond_china_yield() 兜底（akshare 提供多接口）
    """
    rows: list = []
    try:
        if issuer == "em":
            df = call_ak("bond_em", ak.bond_zh_us_rate)
        elif issuer == "tx":
            # W-B8（Sprint4，P1-15）：不传参时 akshare 默认窗口 2020-02~2021-01，
            # em 挂时会把两年前旧收益率 INSERT OR REPLACE 伪装成"更新成功"。
            # 传最近 40 天窗口（end-start 需小于一年，40 天足够算 20 日 delta）。
            end = date.today()
            start = end - timedelta(days=40)
            df = call_ak("bond_tx", ak.bond_china_yield,
                         start_date=start.strftime("%Y%m%d"),
                         end_date=end.strftime("%Y%m%d"))
        else:
            return rows
    except Exception as e:
        log.warning("bond %s FAIL: %s", issuer, repr(e)[:140])
        return rows
    if df is None or df.empty:
        return rows
    # 列名适配：akshare 不同版本列名差异
    col_date = "日期" if "日期" in df.columns else (
        "date" if "date" in df.columns else df.columns[0])
    col_yield = None
    for cand in ("10年", "10年期", "10Y", "yield_10y", "中债国债到期收益率:10年"):
        if cand in df.columns:
            col_yield = cand
            break
    if col_yield is None:
        # 兜底：找包含"10"的数值列
        for c in df.columns:
            if c != col_date and "10" in str(c):
                col_yield = c
                break
    if col_yield is None:
        log.warning("bond %s 列名不识别: %s", issuer, list(df.columns)[:8])
        return rows
    for _, row in df.iterrows():
        try:
            d = str(row[col_date])[:10]
            y = float(row[col_yield])
            rows.append((d, y, issuer))
        except (TypeError, ValueError):
            continue
    # W-B8（P1-15）新鲜度闸门：末行早于今日-7 天 → 整体弃用（不装"更新成功"）
    if rows:
        latest = max(r[0] for r in rows)
        stale_before = (date.today() - timedelta(days=7)).isoformat()
        if latest < stale_before:
            log.warning("bond %s 数据陈旧（末行 %s < %s），弃用不写库",
                        issuer, latest, stale_before)
            return []
    return rows


def fetch_bond_yield(codes: list = None, conn: sqlite3.Connection = None) -> dict:
    """10Y 国债收益率入库。

    兜底链：bond_em → bond_tx。任一成功即写入。
    返回 {code: 行数}。conn=None 时用 get_conn()（生产），测试可注入 :memory:。
    """
    import sqlite3 as _sq3
    codes = codes or BOND_CODES
    out: dict = {}
    own = conn is None
    c = conn or get_conn()
    try:
        rows: list = []
        source = None
        for issuer in ("em", "tx"):
            sub = _fetch_bond_yield_one(issuer)
            if sub:
                rows = sub
                source = issuer
                break
        if not rows:
            log.warning("fetch_bond_yield: 全源失败")
            return out
        # 按日期升序，算 20 日 delta bp
        rows.sort(key=lambda x: x[0])
        n = 0
        for i, (d, y, src) in enumerate(rows):
            if i < 20:
                delta = None
            else:
                # 取 20 个交易日之前的值（rows 已排序，近似 20 日）
                prev_y = rows[i - 20][1]
                delta = round((y - prev_y) * 100, 2)  # 百分比→bp
            c.execute(
                "INSERT OR REPLACE INTO index_bond_yield"
                " (index_code, trade_date, yield, delta_20d_bp, source) VALUES (?,?,?,?,?)",
                ("10Y_CN", d, y, delta, source))
            n += 1
        c.commit()
        out["10Y_CN"] = n
        log.info("fetch_bond_yield: 写入 %d 行（source=%s）", n, source)
    finally:
        if own:
            c.close()
    return out


def _fetch_etf_share_one(etf_code: str, issuer: str) -> list:
    """单源单 ETF 拉份额日频。返回 [(trade_date, share, pct_chg_1d, source), ...]。

    issuer ∈ {"em", "tx"}：
    - em: ak.fund_etf_fund_info_em(fund=etf_code)
    - tx: ak.fund_etf_fund_info_tx（若端点存在）
    """
    rows: list = []
    try:
        if issuer == "em":
            df = call_ak("etf_em", ak.fund_etf_fund_info_em, fund=etf_code)
        elif issuer == "tx":
            # akshare 历史端点 fund_etf_fund_info_tx 可能不存在，容错
            fn = getattr(ak, "fund_etf_fund_info_tx", None)
            if fn is None:
                return rows
            df = call_ak("etf_tx", fn, fund=etf_code)
        else:
            return rows
    except Exception as e:
        log.warning("etf %s %s FAIL: %s", etf_code, issuer, repr(e)[:140])
        return rows
    if df is None or df.empty:
        return rows
    # 列名适配
    col_date = "净值日期" if "净值日期" in df.columns else (
        "trade_date" if "trade_date" in df.columns else df.columns[0])
    col_share = None
    for cand in ("份额", "基金份额", "total_share", "总份额"):
        if cand in df.columns:
            col_share = cand
            break
    if col_share is None:
        log.warning("etf %s 份额列不识别: %s", etf_code, list(df.columns)[:8])
        return rows
    prev = None
    for _, row in df.iterrows():
        try:
            d = str(row[col_date])[:10]
            s = float(row[col_share])
            pct = round((s - prev) / prev * 100, 2) if prev and prev > 0 else None
            rows.append((d, s, pct, issuer))
            prev = s
        except (TypeError, ValueError):
            continue
    return rows


def fetch_etf_share(codes: list = None, conn: sqlite3.Connection = None) -> dict:
    """ETF 份额入库（沪深300 + 中证500）。

    W-B7（Sprint4，P1-14）实测结论：akshare 无任何含「份额/规模」列的 ETF 接口
    （fund_etf_fund_info_em 净值列、fund_etf_fund_daily_em 净值/市价/折价率列，
    均实测无份额列）→ 本函数在生产上恒返回空 dict，index_etf_share 恒空表。
    regime 的 ETF 档位信号已随之显式 no-op（见 risk/regime.py）。函数保留供
    历史调用方/测试兼容；pipeline 已不再调度。

    兜底链：em 主 → tx 兜底（端点不存在则跳过）。
    返回 {etf_code: 行数}。conn=None 时用 get_conn()（生产），测试可注入 :memory:。
    """
    codes = codes or ETF_CODES
    out: dict = {}
    own = conn is None
    c = conn or get_conn()
    try:
        for code in codes:
            rows: list = []
            source = None
            for issuer in ("em", "tx"):
                sub = _fetch_etf_share_one(code, issuer)
                if sub:
                    rows = sub
                    source = issuer
                    break
            if not rows:
                log.warning("fetch_etf_share %s: 全源失败", code)
                continue
            n = 0
            for d, s, pct, src in rows:
                c.execute(
                    "INSERT OR REPLACE INTO index_etf_share"
                    " (etf_code, trade_date, share, pct_chg_1d, source) VALUES (?,?,?,?,?)",
                    (code, d, s, pct, source))
                n += 1
            out[code] = n
            log.info("fetch_etf_share %s: 写入 %d 行（source=%s）", code, n, source)
        c.commit()
    finally:
        if own:
            c.close()
    return out


def main():
    print("== P1.5 指数日线入库（W-B2：统一走 fetcher.ensure_index_daily）==")
    from data.fetcher import ensure_index_daily
    conn = get_conn()
    try:
        for code in INDEX_CODES:
            n = ensure_index_daily(conn, code)
            print(f"  {code}: +{n} 行")
    finally:
        conn.close()
    print("== P1.5 指数估值分位入库 ==")
    for code, r in fetch_index_valuation().items():
        print(f"  {code}: {r}")
    print("== P1.3 国债（Sprint 2；ETF 份额源不存在，W-B7 已显式停用）==")
    for code, n in fetch_bond_yield().items():
        print(f"  国债 {code}: +{n} 行")


if __name__ == "__main__":
    main()
