"""回测宇宙回补：中证800 成分股日线入 daily_bar（不进 stock_info）。

用途：自选池 30 只是事后人工挑选的上涨票，其上任何选股类 alpha 回测都被
幸存者偏差污染（2026-09-13 双 profile 实测已证实）。中证800 是规则化指数，
用它的成分日线做回测宇宙可大幅缓解（仍有"现时点成分回溯"的成分变动偏差，
backtest notes 已注明）。宇宙票不进 stock_info——否则会混入自选池信号计算
与决策 bundle；成员快照留痕 universe_member 表。

数据源：腾讯（东财封禁期间的唯一可用源），每票两次调用：
- raw（adjust=""）→ daily_bar 基础列（OHLC/量额/换手/pct_chg 由 raw 收盘推算）；
- qfq（adjust="qfq"）→ close_qfq/high_qfq/low_qfq（复权因子列）。

用法：
  python3 -m data.universe800              # 全量/增量回补（幂等，可断点续跑）
  python3 -m data.universe800 --limit 20   # 只回补前 20 只（试跑）
  python3 -m data.universe800 --force      # 忽略"已最新"跳过，全部重拉
增量判定：某票 MAX(trade_date) 已达全库最新交易日且行数充足 → 跳过。

限速与熔断：每票间隔 0.5s；连续 10 票失败自动中止（重跑续传）——
腾讯同样可能封禁，宁可持续多日跑完也不要把源打挂。
"""
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import argparse
import json
import logging
import logging.handlers
from datetime import date, timedelta

import pandas as pd

from data import repo
from data.fetcher import (_market_data_window, _norm_volume, call_ak, get_conn)

log = logging.getLogger("universe800")
log.setLevel(logging.INFO)
if not log.handlers:
    from common.logsetup import rotating_handler  # AGSICKLE_LOG_DIR 逃生门（Sprint4 W0-1）
    log.addHandler(rotating_handler("fetch.log"))
log.propagate = False

UNIVERSE = "csi800"
SLEEP_PER_CODE = 0.5
MAX_CONSEC_FAIL = 10
MIN_ROWS_KEEP = 300      # 行数低于此视为数据不全，不因"已最新"跳过


def _tx_symbol(code: str) -> str:
    return ("sh" if code.startswith(("6", "9")) else
            "sz" if code.startswith(("0", "3")) else "bj") + code


def _hist_tx(code: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    import akshare as ak
    df = call_ak("tx", ak.stock_zh_a_hist_tx, symbol=_tx_symbol(code),
                 start_date=start, end_date=end, adjust=adjust)
    if df is None or df.empty:
        raise ValueError(f"{code} tx(adjust={adjust}) 空返回")
    return df


def constituents() -> list:
    """中证800 成分（中证官网）：[(code, name)]。"""
    import akshare as ak
    df = call_ak("csi", ak.index_stock_cons_csindex, symbol="000906")
    if df is None or df.empty:
        raise RuntimeError("中证800 成分获取失败")
    out = []
    for _, r in df.iterrows():
        code = str(r.get("成分券代码") or "").zfill(6)
        name = str(r.get("成分券名称") or "")
        if len(code) == 6 and code.isdigit():
            out.append((code, name))
    return out


def upsert_code(conn, code: str, start: str, end: str) -> int:
    """单票回补：raw + qfq 两次调用合并写入 daily_bar。返回写入行数。"""
    raw = _hist_tx(code, start, end, adjust="")
    time.sleep(0.3)
    qfq = _hist_tx(code, start, end, adjust="qfq")
    hcol = qfq["high"] if "high" in qfq.columns else pd.Series([None] * len(qfq))
    lcol = qfq["low"] if "low" in qfq.columns else pd.Series([None] * len(qfq))
    qfq_close = {pd.Timestamp(d).strftime("%Y-%m-%d"): (c, h, l)
                 for d, c, h, l in zip(qfq["date"], qfq["close"], hcol, lcol)}
    prev = repo.latest_close(conn, code)
    pct = raw["close"].pct_change() * 100
    if prev:
        pct.iloc[0] = (float(raw["close"].iloc[0]) / float(prev) - 1) * 100
    rows = []
    for _, r in raw.iterrows():
        d = pd.Timestamp(r["date"]).strftime("%Y-%m-%d")
        cq, hq, lq = qfq_close.get(d, (None, None, None))
        close = float(r["close"])
        rows.append((
            code, d,
            float(r["open"]), float(r["high"]), float(r["low"]), close,
            float(_norm_volume(r["volume"], r["amount"], r["close"])),
            float(r["amount"]) if pd.notna(r.get("amount")) else 0.0,
            float(pct.loc[r.name]) if pd.notna(pct.loc[r.name]) else 0.0,
            float(r["turnover"]) * 100 if pd.notna(r.get("turnover")) else 0.0,
            "tx", cq, hq, lq,
        ))
    conn.executemany(
        "INSERT OR REPLACE INTO daily_bar (code, trade_date, open, high, low, close, "
        "volume, amount, pct_chg, turnover, source, close_qfq, high_qfq, low_qfq) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="中证800 回测宇宙回补（腾讯源，幂等可续跑）")
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 只（试跑）")
    ap.add_argument("--force", action="store_true", help="忽略增量判定全部重拉")
    args = ap.parse_args(argv)

    conn = get_conn()
    try:
        cons = constituents()
        as_of = date.today().isoformat()
        conn.executemany(
            "INSERT OR REPLACE INTO universe_member (universe, code, name, as_of) "
            "VALUES (?,?,?,?)", [(UNIVERSE, c, n, as_of) for c, n in cons])
        conn.commit()
        log.info("中证800 成分 %d 只（快照 %s）", len(cons), as_of)
        if args.limit:
            cons = cons[:args.limit]

        global_max = repo.latest_trade_date(conn)
        end = date.today().strftime("%Y%m%d")
        if _market_data_window():
            end = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
        start = "20240101"

        done = fail = consec_fail = 0
        for i, (code, name) in enumerate(cons, 1):
            row = conn.execute(
                "SELECT MAX(trade_date), COUNT(*) FROM daily_bar WHERE code=?",
                (code,)).fetchone()
            if (not args.force and global_max and row and row[0] == global_max
                    and row[1] >= MIN_ROWS_KEEP):
                done += 1
                continue
            try:
                n = upsert_code(conn, code, start, end)
                done += 1
                consec_fail = 0
                log.info("[%d/%d] %s %s: +%d rows", i, len(cons), code, name, n)
            except Exception as e:  # noqa: BLE001
                fail += 1
                consec_fail += 1
                log.warning("[%d/%d] %s %s FAIL: %s", i, len(cons), code, name,
                            repr(e)[:100])
                if consec_fail >= MAX_CONSEC_FAIL:
                    log.error("连续 %d 票失败，中止（重跑本命令续传）", consec_fail)
                    break
            time.sleep(SLEEP_PER_CODE)

        n_uni = conn.execute(
            "SELECT COUNT(DISTINCT code) FROM daily_bar WHERE code IN "
            "(SELECT code FROM universe_member WHERE universe=?)", (UNIVERSE,)).fetchone()[0]
        print(json.dumps({"constituents": len(cons), "ok": done, "fail": fail,
                          "universe_codes_in_daily_bar": int(n_uni)},
                         ensure_ascii=False))
        return 0 if fail < len(cons) else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
