"""批次 0 · qfq 数据层审计与修复（全量打包施工方案 §3.1，预注册口径逐字冻结）。

预注册口径原文（docs/全量打包施工方案-2026-09-20.md §3.1，批次 0 commit 后不可变）：

    先审计后修复，重刷仅由审计结果驱动。

    - 审计 A1（只读）：全库畸变日清单（|ret_qfq − ret_raw| > 1pp，ret_raw=close.pct_change，
      ret_qfq=close_qfq.pct_change）+ 除权除息事件清单（akshare em 除权数据在线取，只进内存
      与报告，不入库）。
    - Gate 0-A（畸变解释完备性）：每个畸变日必须落在该票除权事件清单上（事件日或其后
      首个交易日，容忍除权净额归零导致的边界 ±1 日）；存在无解释畸变日的票 → 该票以 em 单源
      重拉全史 qfq（事务内 UPDATE 该票 close_qfq/high_qfq/low_qfq）后复检；复检仍无解释 →
      停下上报，不得硬修。
    - Gate 0-B（open_qfq 等比补列）：open_qfq ≡ open × (close_qfq / close)，与现行
      high_qfq/low_qfq 同等比口径（fetcher.py L8-10 语义），全库 ALTER TABLE 增列 + 回填；
      fetcher 增量写路径同步该式（增量一致性）；逐日校验 low_qfq ≤ open_qfq ≤ high_qfq
      违例 = 0（浮点容差 1e-6 相对）。
    - Gate 0-C（双源对账）：抽样 ≥50 票（含全部畸变重灾票）与 tx_qfq 逐日收益对账：
      收益差中位 ≤ 0.05%，|差|>1% 的点逐票 ≤ 2 处（源间口径差容忍；超限票单独列报告）。
    - Gate 0-D（无除权区间不变量）：无除权事件的相邻交易日，qfq 收益 = raw 收益
      （相对容差 1e-9）逐日成立。
    - 生产安全红线：执行前备份 market.db；全部写操作单事务且仅限 daily_bar 的
      close_qfq/high_qfq/low_qfq/open_qfq 列族（绝不触其他任何表）；写后跑全库数据体检
      （audit 路径）+ 回滚预案写入批次报告。

实现事实（2026-09-20 施工时经验测定，供报告与复检解读，不构成口径变更）：
- 库内 qfq 列族 96.6% 行来自腾讯源（tx/NULL source），其复权为交易所精确公式的
  逐段实现——纯现金分红段呈「加法型」（close_qfq − close 段内恒定），送转段乘法；
  因此无事件日的 ret_qfq 与 ret_raw 天然存在与分红净额/价格水平成比例的背离。
- akshare em 除权清单接口 = ak.stock_fhps_em(date=报告期)（datacenter-web 域，与
  push2his K线域相互独立）；em K线（重拉源）= ak.stock_zh_a_hist(adjust="qfq")。
- 本模块所有审计路径一律 mode=ro 连库；写路径仅 gate0a-repair / gate0b-apply 两个
  子命令，且各自单事务、只触 daily_bar 的 qfq 列族。

CLI 子命令（.venv/bin/python3 -m data.qfq_rebuild <cmd>）：
    events          在线拉除权除息事件清单 → logs/state/qfq_batch0/events.json
    audit-a1        只读畸变日清单与统计 → logs/state/qfq_batch0/a1_distortion.json
    gate0a          畸变/事件匹配（只读）→ logs/state/qfq_batch0/gate0a.json
    gate0a-repair   对无解释畸变日票 em 单源重拉（单票单事务）+ 复检 → gate0a_repair.json
    gate0b-precheck open_qfq 等比假想违例（写前只读预检）→ gate0b_precheck.json
    gate0b-apply    ALTER TABLE + 回填 open_qfq（单事务）→ gate0b_apply.json
    gate0c          双源对账（在线 tx qfq，≥50 票）→ gate0c.json
    gate0d          无除权区间不变量（只读）→ gate0d.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

WORK_DIR = Path(BASE / "logs" / "state" / "qfq_batch0")  # gitignored 中间产物

DIST_THRESH = 0.01       # 畸变日阈值：|ret_qfq − ret_raw| > 1pp（预注册）
OHLC_TOL = 1e-6          # Gate 0-B low≤open≤high 相对容差（预注册）
INV_TOL = 1e-9           # Gate 0-D 无除权区间 ret 相等相对容差（预注册）
GATE0C_N = 50            # 双源对账抽样下限（预注册 ≥50）
GATE0C_SEED = 20260920   # 抽样固定随机种子（可复现）
MATCH_WINDOW_TD = 1      # 事件匹配容忍：事件日（吸附到其后首个交易日后）±1 交易日

# 分红送配报告期：覆盖库内 2024-01-02 起的全部除权除息日（含 2023 各期特殊分红
# 落在 2024 年初的除权日，与 2026 最新实施分配）
FHPS_PERIODS = [
    "20230331", "20230630", "20230930", "20231231",
    "20240331", "20240630", "20240930", "20241231",
    "20250331", "20250630", "20250930", "20251231",
    "20260331", "20260630",
]


# ---------------------------------------------------------------- 连接

def db_path() -> Path:
    import os
    return Path(os.environ.get("AGSICKLE_DB") or BASE / "data" / "market.db")


def ro_conn(db: Optional[Path] = None) -> sqlite3.Connection:
    """只读连接（审计路径统一入口；uri mode=ro 保证绝不写）。"""
    p = db or db_path()
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=15)
    return conn


def rw_conn(db: Optional[Path] = None) -> sqlite3.Connection:
    """读写连接（仅 gate0a-repair / gate0b-apply）。"""
    p = db or db_path()
    conn = sqlite3.connect(p, timeout=15)
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


# ---------------------------------------------------------------- 审计 A1

def load_bars(conn: sqlite3.Connection, code: Optional[str] = None) -> pd.DataFrame:
    """daily_bar 的 qfq 审计所需列（code, trade_date 升序）。"""
    sql = ("SELECT code, trade_date, open, high, low, close, "
           "close_qfq, high_qfq, low_qfq FROM daily_bar")
    args: tuple = ()
    if code:
        sql += " WHERE code=?"
        args = (code,)
    sql += " ORDER BY code, trade_date"
    return pd.read_sql(sql, conn, params=args)


def distortion_days(bars: pd.DataFrame, thresh: float = DIST_THRESH) -> pd.DataFrame:
    """畸变日清单：|ret_qfq − ret_raw| > thresh 的 (code, trade_date, ret_raw, ret_qfq)。

    ret=组内 pct_change（pandas 3.x 无 fill_method 参数，缺失前值自然为 NaN）。
    """
    g = bars.sort_values(["code", "trade_date"])
    ret_raw = g.groupby("code")["close"].pct_change()
    ret_qfq = g.groupby("code")["close_qfq"].pct_change()
    out = pd.DataFrame({
        "code": g["code"].values,
        "trade_date": g["trade_date"].values,
        "ret_raw": ret_raw.values,
        "ret_qfq": ret_qfq.values,
    })
    return out[out["ret_raw"].notna() & out["ret_qfq"].notna()
               & ((out["ret_qfq"] - out["ret_raw"]).abs() > thresh)].reset_index(drop=True)


# ---------------------------------------------------------------- 除权事件清单（在线）

def fetch_ex_events(periods: Sequence[str] = FHPS_PERIODS,
                    codes: Optional[set] = None) -> Dict[str, List[str]]:
    """akshare em 分红送配 → 每票除权除息日清单（只进内存与落盘 JSON，绝不入库）。

    任一报告期在线取数异常、或全部期次合并后无任何有效事件 → RuntimeError（阻断，
    不 Mock）。codes 给定时只保留库内票。
    """
    import akshare as ak
    events: Dict[str, set] = {}
    failures: List[str] = []
    for period in periods:
        df = None
        last_err = None
        for attempt in range(3):  # 网络退避（代理环境偶发断连）
            try:
                df = ak.stock_fhps_em(date=period)
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        if df is None:
            failures.append(f"{period}: {last_err!r}")
            continue
        if "除权除息日" not in df.columns or "代码" not in df.columns:
            failures.append(f"{period}: 列缺失 {list(df.columns)[:6]}")
            continue
        for cd, ex in zip(df["代码"].astype(str), df["除权除息日"].astype(str)):
            if not isinstance(ex, str) or len(ex) != 10 or ex[4] != "-":
                continue  # "-" / 预案未实施
            if codes is not None and cd not in codes:
                continue
            events.setdefault(cd, set()).add(ex)
    if failures:
        raise RuntimeError(f"fhps 在线取数失败（阻断，不 Mock）: {failures}")
    if not events:
        raise RuntimeError("fhps 全部报告期无任何有效除权除息日（异常，阻断）")
    return {cd: sorted(ds) for cd, ds in events.items()}


def load_or_fetch_events(path: Optional[Path] = None,
                         codes: Optional[set] = None) -> Tuple[Dict[str, List[str]], str]:
    """事件清单复用：优先读已落盘 JSON（同批多次子命令免重拉），否则在线取。"""
    p = path or (WORK_DIR / "events.json")
    if p.is_file():
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw["events"], "cache"
    events = fetch_ex_events(codes=codes)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"fetched_at": date.today().isoformat(),
                             "periods": list(FHPS_PERIODS),
                             "events": events}, ensure_ascii=False),
                 encoding="utf-8")
    return events, "online"


# ---------------------------------------------------------------- 匹配（Gate 0-A）

def build_calendar(bars: pd.DataFrame) -> List[str]:
    return sorted(bars["trade_date"].unique().tolist())


def _snap_next_td(event_date: str, cal_index: Dict[str, int], calendar: List[str]) -> Optional[int]:
    """事件日吸附到其当日或其后首个交易日（事件日为非交易日时），返回交易日序号。"""
    import bisect
    i = bisect.bisect_left(calendar, event_date)
    return i if i < len(calendar) else None


def explained_mask(dist: pd.DataFrame, events: Dict[str, List[str]],
                   calendar: List[str]) -> pd.Series:
    """每个畸变日是否可被该票除权事件解释（事件日或其后首个交易日，±1 交易日边界）。

    判定：畸变日 d 可解释 ⟺ 存在事件 e，|idx(d) − idx(snap(e))| ≤ MATCH_WINDOW_TD。
    """
    cal_index = {d: i for i, d in enumerate(calendar)}
    snapped: Dict[str, Dict[int, None]] = {}
    for cd, ds in events.items():
        idxs = {}
        for e in ds:
            i = _snap_next_td(e, cal_index, calendar)
            if i is not None:
                idxs[i] = None
        if idxs:
            snapped[cd] = idxs
    flags = []
    for cd, td in zip(dist["code"].values, dist["trade_date"].values):
        ok = False
        ev_idx = snapped.get(cd)
        if ev_idx:
            di = cal_index.get(td)
            if di is not None:
                for ei in ev_idx:
                    if abs(di - ei) <= MATCH_WINDOW_TD:
                        ok = True
                        break
        flags.append(ok)
    return pd.Series(flags, index=dist.index)


# ---------------------------------------------------------------- Gate 0-A 修复（写路径·单票单事务）

def repull_em_qfq(code: str, conn: sqlite3.Connection,
                  events: Dict[str, List[str]], calendar: List[str]) -> dict:
    """em 单源重拉单票全史 qfq：单事务内 UPDATE 三列 + 复检，复检不过即 ROLLBACK。

    - 与 fetcher.backfill_qfq 的 em 分支同等比口径：high/low_qfq = raw × (close_qfq/close)
      （round 4，舍入单调 ⇒ 不破坏 low≤x≤high 传递性）；
    - 复检 = 重算该票畸变日并对事件清单匹配（同连接读得到本事务未提交写入）；
      存在无解释畸变日 → ROLLBACK（预注册：不得硬修）；
    - 绝不触其他表/其他列/其他票；
    - 返回 {status: kept|rolled_back|fetch_fail, rows, unexplained: [...]}。
      网络级失败（ConnectionError，含代理断连/熔断冷却）向上抛出由调用方整批阻断。
    """
    from data.fetcher import _hist_em_qfq  # 复用 em 拉取（call_ak 自带重试与熔断）
    row = conn.execute(
        "SELECT MIN(trade_date), MAX(trade_date) FROM daily_bar WHERE code=?",
        (code,)).fetchone()
    if not row or not row[0]:
        return {"status": "fetch_fail", "rows": 0, "unexplained": [],
                "error": "no bars"}
    start8 = str(row[0]).replace("-", "")
    end8 = date.today().strftime("%Y%m%d")
    df = _hist_em_qfq(code, start8, end8)  # em 单源，失败抛异常（不降级 tx）
    if df is None or df.empty:
        return {"status": "fetch_fail", "rows": 0, "unexplained": [],
                "error": "em qfq empty"}
    raw = {d: (h, l, c) for d, h, l, c in conn.execute(
        "SELECT trade_date, high, low, close FROM daily_bar WHERE code=?", (code,))}
    rows = []
    for _, r in df.iterrows():
        d = r["date"].strftime("%Y-%m-%d")
        cq = r["close_qfq"]
        if pd.isna(cq) or d not in raw:
            continue
        rh, rl, rc = raw[d]
        if not rc or rh is None or rl is None:
            continue
        hq = round(float(rh) * float(cq) / float(rc), 4)
        lq = round(float(rl) * float(cq) / float(rc), 4)
        rows.append((round(float(cq), 4), hq, lq, code, d))
    if not rows:
        return {"status": "fetch_fail", "rows": 0, "unexplained": [],
                "error": "no date overlap"}
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            "UPDATE daily_bar SET close_qfq=?, high_qfq=?, low_qfq=? "
            "WHERE code=? AND trade_date=?", rows)
        dist = distortion_days(load_bars(conn, code))
        unexp = dist[~explained_mask(dist, events, calendar)]
        if len(unexp):
            conn.rollback()  # 预注册：复检仍无解释 → ROLLBACK，不硬修
            return {"status": "rolled_back", "rows": 0,
                    "unexplained": unexp[["code", "trade_date"]]
                                     .to_dict("records")[:50]}
        conn.commit()
        return {"status": "kept", "rows": len(rows), "unexplained": []}
    except Exception:
        conn.rollback()
        raise


def gate0a_repair(unexplained_codes: Sequence[str], db: Optional[Path] = None) -> dict:
    """对无解释畸变日票逐票 em 单源重拉 + 事务内复检（写路径，单票单事务）。

    网络级失败（ConnectionError 家族：代理断连/熔断冷却）→ 立即整批阻断（向上抛，
    不 Mock、不带病推进）；单票数据级失败（em 空返回等）→ 记 failed 继续。
    返回 {repulled, failed, rows_written}。
    """
    events, _src = load_or_fetch_events()
    conn = rw_conn(db)
    try:
        bars0 = load_bars(conn)
        calendar = build_calendar(bars0)
        repulled, failed, written = [], [], {}
        for code in unexplained_codes:
            try:
                r = repull_em_qfq(code, conn, events, calendar)
            except ConnectionError:
                conn.rollback()
                raise  # 整批阻断（在线取数失败，预注册红线）
            except Exception as e:  # noqa: BLE001 —— 单票数据级失败：如实记录
                failed.append({"code": code, "error": repr(e)[:200]})
                continue
            if r["status"] == "kept":
                repulled.append(code)
                written[code] = r["rows"]
            else:
                failed.append({"code": code, "status": r["status"],
                               "error": r.get("error", ""),
                               "unexplained": r["unexplained"]})
        return {"repulled": repulled, "failed": failed, "rows_written": written}
    finally:
        conn.close()


# ---------------------------------------------------------------- Gate 0-B

def derive_open_qfq(open_: float, close: float, close_qfq: float) -> Optional[float]:
    """open_qfq ≡ round(open × (close_qfq / close), 4)（预注册公式；round4 与兄弟列一致，
    舍入单调 ⇒ raw 层 low≤open≤high 成立时导出值不破坏不等式）。close 0/NULL → None。"""
    if close is None or close_qfq is None or not close or open_ is None:
        return None
    return round(float(open_) * float(close_qfq) / float(close), 4)


def gate0b_precheck(bars: pd.DataFrame) -> dict:
    """写前只读预检：按预注册公式假想 open_qfq，统计违例并区分原始/新引入。"""
    df = bars.dropna(subset=["open", "high", "low", "close", "close_qfq",
                             "high_qfq", "low_qfq"])
    df = df[(df.close > 0) & (df.close_qfq > 0) & (df.open > 0)
            & (df.high > 0) & (df.low > 0)]
    oq = (df["open"] * df["close_qfq"] / df["close"]).round(4)
    lo_bad = oq < df["low_qfq"] - _rel_vec(df["low_qfq"])
    hi_bad = oq > df["high_qfq"] + _rel_vec(df["high_qfq"])
    viol = lo_bad | hi_bad
    raw_bad = (df["open"] < df["low"]) | (df["open"] > df["high"])
    zero_close = bars[(bars.close.isna()) | (bars.close <= 0)]
    return {
        "rows_checked": int(len(df)),
        "raw_ohlc_broken_rows": int(raw_bad.sum()),
        "raw_ohlc_broken_stocks": int(df.code[raw_bad].nunique()) if raw_bad.any() else 0,
        "new_violation_rows": int((viol & ~raw_bad).sum()),
        "new_violation_stocks": int(df.code[viol & ~raw_bad].nunique())
        if (viol & ~raw_bad).any() else 0,
        "violation_rows_incl_raw": int(viol.sum()),
        "close_zero_or_null_rows": int(len(zero_close)),
        "detail": df.loc[viol & ~raw_bad,
                         ["code", "trade_date", "open", "low_qfq", "high_qfq",
                          "close", "close_qfq"]].assign(open_qfq=oq[viol & ~raw_bad])
                       .to_dict("records")[:200],
    }


def _rel_vec(s: pd.Series) -> pd.Series:
    return (OHLC_TOL * s.abs()).clip(lower=OHLC_TOL)


def gate0b_apply(db: Optional[Path] = None) -> dict:
    """写路径：单事务 ALTER TABLE 增列 + 全库回填 open_qfq + 写后校验。

    close 为 0/NULL 的行不回填（如实报数）；任何异常 → ROLLBACK（列增改随事务回退）。
    """
    conn = rw_conn(db)
    try:
        pre = gate0b_precheck(load_bars(conn))
        conn.execute("BEGIN IMMEDIATE")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_bar)")}
        added = "open_qfq" not in cols
        if added:
            conn.execute("ALTER TABLE daily_bar ADD COLUMN open_qfq REAL")
        n_close_bad = conn.execute(
            "SELECT COUNT(*) FROM daily_bar WHERE close IS NULL OR close <= 0"
        ).fetchone()[0]
        n_qfq_null = conn.execute(
            "SELECT COUNT(*) FROM daily_bar WHERE close_qfq IS NULL").fetchone()[0]
        n_open_null = conn.execute(
            "SELECT COUNT(*) FROM daily_bar WHERE open IS NULL").fetchone()[0]
        rows = conn.execute(
            "SELECT rowid, open, close, close_qfq FROM daily_bar "
            "WHERE close IS NOT NULL AND close > 0 AND close_qfq IS NOT NULL "
            "AND open IS NOT NULL").fetchall()
        upd = [(derive_open_qfq(o, c, cq), rid) for rid, o, c, cq in rows]
        conn.executemany(
            "UPDATE daily_bar SET open_qfq=? WHERE rowid=?",
            [(v, rid) for v, rid in upd if v is not None])
        conn.commit()
        # 写后校验（全量重读，gate0b_precheck 同一判据）
        post = gate0b_precheck(load_bars(conn))
        return {"added_column": added, "rows_backfilled": len(upd),
                "skipped_close_zero_or_null": int(n_close_bad),
                "skipped_close_qfq_null": int(n_qfq_null),
                "skipped_open_null": int(n_open_null),
                "pre": pre, "post": post}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- Gate 0-C（在线）

def gate0c_sample(conn: sqlite3.Connection, dist: pd.DataFrame,
                  n: int = GATE0C_N, seed: int = GATE0C_SEED) -> List[str]:
    """抽样（§3.1「含全部畸变重灾票」）：全部畸变票按畸变次数稳定降序全入 +
    其余票固定种子随机补足至 ≥n（畸变票已超 n 时全入，不截断）。"""
    counts = dist.groupby("code").size().sort_values(ascending=False, kind="stable")
    heavy = [str(c) for c in counts.index]
    all_codes = [str(r[0]) for r in conn.execute(
        "SELECT DISTINCT code FROM daily_bar ORDER BY code").fetchall()]
    rest = [c for c in all_codes if c not in set(heavy)]
    if len(heavy) >= n:
        return heavy
    import random
    rng = random.Random(seed)
    rest_sorted = sorted(rest)
    rng.shuffle(rest_sorted)
    return heavy + rest_sorted[:n - len(heavy)]


def gate0c_reconcile(conn: sqlite3.Connection, codes: Sequence[str]) -> dict:
    """每票在线拉腾讯 qfq 全史，与库内 close_qfq 逐日收益对账（只读库，不写）。

    判定（预注册）：收益差中位 ≤0.05% 且 |差|>1% 的点逐票 ≤2 处（超限票披露不阻断）；
    任一票在线失败 → 该票记入 failed（整体失败时由调用方阻断上报）。
    """
    from data.fetcher import _hist_tx_qfq
    per_stock, all_diffs, failed = [], [], []
    for code in codes:
        row = conn.execute(
            "SELECT MIN(trade_date), MAX(trade_date) FROM daily_bar WHERE code=?",
            (code,)).fetchone()
        if not row or not row[0]:
            continue
        start8, end8 = str(row[0]).replace("-", ""), str(row[1]).replace("-", "")
        try:
            df = _hist_tx_qfq(code, start8, end8)
        except Exception as e:  # noqa: BLE001
            failed.append({"code": code, "error": repr(e)[:150]})
            continue
        if df is None or df.empty:
            failed.append({"code": code, "error": "tx qfq empty"})
            continue
        bars = load_bars(conn, code)[["trade_date", "close_qfq"]]
        m = bars.merge(df[["date", "close_qfq"]].rename(
            columns={"date": "trade_date", "close_qfq": "tx_qfq"}),
            on="trade_date", how="inner")
        m = m.sort_values("trade_date")
        ret_db = m["close_qfq"].pct_change()
        ret_tx = m["tx_qfq"].pct_change()
        d = (ret_db - ret_tx).abs().dropna()
        if len(d) == 0:
            failed.append({"code": code, "error": "对账无重叠日"})
            continue
        over = int((d > 0.01).sum())
        per_stock.append({"code": code, "days": int(len(d)),
                          "median_diff": float(d.median()),
                          "over_1pct_points": over})
        all_diffs.append(d)
    pooled = pd.concat(all_diffs) if all_diffs else pd.Series(dtype=float)
    over_stocks = [s for s in per_stock if s["over_1pct_points"] > 2]
    return {
        "n_sampled": len(per_stock), "n_failed": len(failed), "failed": failed,
        "overall_median_diff": float(pooled.median()) if len(pooled) else None,
        "overall_median_pass_0p05pct": bool(len(pooled) and pooled.median() <= 0.0005),
        "over_limit_stocks": over_stocks,
        "per_stock": per_stock,
    }


# ---------------------------------------------------------------- Gate 0-D（只读）

def gate0d_check(bars: pd.DataFrame, events: Dict[str, List[str]],
                 calendar: List[str]) -> dict:
    """无除权事件相邻交易日：ret_qfq = ret_raw（相对容差 1e-9）逐日成立性 + 违例结构。

    - 「相邻两日之间无该票除权事件」：事件吸附序号不在 (idx(prev), idx(cur)] 区间
      （股票停牌跳日时区间可能横跨多个日历交易日，两指针 searchsorted 向量化判定）；
    - 违例 = |Δret| > 1e-9 且相对 |Δret|/|ret_raw| > 1e-9（双尺度，防 ret_raw≈0 假信号）；
    - 结构统计（S6 预注册要求）：违例按 |close_qfq−close|/close 分桶（加法型特征：
      大净额桶违例密集）与违例幅度分位数，判别系统性 vs 个别。
    """
    import bisect
    import numpy as np
    cal_index = {d: i for i, d in enumerate(calendar)}
    snapped_events: Dict[str, List[int]] = {}
    for cd, ds in events.items():
        idxs = []
        for e in ds:
            i = bisect.bisect_left(calendar, e)
            if i < len(calendar):
                idxs.append(i)
        if idxs:
            snapped_events[cd] = sorted(idxs)
    g = bars.sort_values(["code", "trade_date"]).copy()
    g["_idx"] = g["trade_date"].map(cal_index).astype("Int64")
    # 每行：事件序号 ≤ 自身序号的累计数（组内 diff ≠ 0 ⇒ (prev, cur] 区间含事件）。
    # 必须在 notna/正值行过滤【之前】对全序计算——否则被滤行会断开 diff 链，
    # 停牌后首对的事件判定会错（首行 prev 缺失被滤后 diff 无前值可减）。
    cnt_le = np.zeros(len(g), dtype=np.int64)
    idx_arr = g["_idx"].astype("float64").to_numpy()
    codes_arr = g["code"].to_numpy()
    for i, (cd, di) in enumerate(zip(codes_arr, idx_arr)):
        ev = snapped_events.get(cd)
        if ev and pd.notna(di):
            cnt_le[i] = bisect.bisect_right(ev, int(di))
    has_event_in_span = pd.Series(cnt_le, index=g.index).groupby(
        codes_arr, sort=False).diff().fillna(0).ne(0)
    g["prev_close"] = g.groupby("code")["close"].shift(1)
    g["prev_close_qfq"] = g.groupby("code")["close_qfq"].shift(1)
    g = g[g["prev_close"].notna() & g["prev_close_qfq"].notna()]
    g = g[(g["prev_close"] > 0) & (g["prev_close_qfq"] > 0) & (g["close"] > 0)
          & (g["close_qfq"] > 0)]
    gg = g[~has_event_in_span.loc[g.index]]
    ret_raw = gg["close"] / gg["prev_close"] - 1.0
    ret_qfq = gg["close_qfq"] / gg["prev_close_qfq"] - 1.0
    diff = (ret_qfq - ret_raw).abs()
    rel = diff / ret_raw.abs().clip(lower=1e-12)
    viol = (diff > INV_TOL) & (rel > INV_TOL)
    off = (gg["close_qfq"] - gg["close"]).abs() / gg["close"]
    buckets = pd.cut(off, [-0.001, 1e-6, 1e-4, 1e-3, 1e-2, 1.0],
                     labels=["0", "<1e-4", "<1e-3", "<1e-2", ">=1e-2"])
    struct = pd.DataFrame({"viol": viol.values, "bucket": buckets.values}).groupby(
        "bucket", observed=False).agg(pairs=("viol", "size"),
                                      violations=("viol", "sum"))
    return {
        "no_event_pairs": int(len(gg)),
        "violations": int(viol.sum()),
        "violating_stocks": int(gg.code[viol].nunique()),
        "diff_percentiles": {lbl: float(diff.quantile(qv))
                             for lbl, qv in (("50%", .5), ("90%", .9),
                                             ("99%", .99), ("max", 1.0))},
        "structure_by_offset_bucket": {
            str(k): {"pairs": int(v["pairs"]), "violations": int(v["violations"])}
            for k, v in struct.to_dict("index").items()},
        "sample_violations": gg.loc[viol, ["code", "trade_date", "close", "close_qfq"]]
                                  .head(30).to_dict("records"),
    }


# ---------------------------------------------------------------- CLI

def _dump(name: str, obj) -> Path:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    p = WORK_DIR / name
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str),
                 encoding="utf-8")
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description="批次0 qfq 数据层审计与修复（§3.1 预注册口径）")
    ap.add_argument("cmd", choices=["events", "audit-a1", "gate0a", "gate0a-repair",
                                    "gate0b-precheck", "gate0b-apply",
                                    "gate0c", "gate0d"])
    args = ap.parse_args()
    conn = ro_conn()
    try:
        codes = {str(r[0]) for r in conn.execute(
            "SELECT DISTINCT code FROM daily_bar").fetchall()}
        if args.cmd == "events":
            events = fetch_ex_events(codes=codes)
            _dump("events.json", {"fetched_at": date.today().isoformat(),
                                  "periods": list(FHPS_PERIODS), "events": events})
            print(f"events: {len(events)} 票，落盘 logs/state/qfq_batch0/events.json")
            return 0
        bars = load_bars(conn)
        calendar = build_calendar(bars)
        dist = distortion_days(bars)
        if args.cmd == "audit-a1":
            obj = {
                "rows": len(bars), "stocks": int(bars.code.nunique()),
                "distortion_days": int(len(dist)),
                "distortion_stocks": int(dist.code.nunique()),
                "top_by_count": dist.groupby("code").size()
                                    .sort_values(ascending=False).head(40)
                                    .to_dict(),
                "rows_detail": dist.assign(
                    gap=(dist.ret_qfq - dist.ret_raw)).to_dict("records"),
            }
            _dump("a1_distortion.json", obj)
            print(f"A1: 畸变日 {len(dist)} / 票 {dist.code.nunique()}")
            return 0
        events, src = load_or_fetch_events(codes=codes)
        if args.cmd == "gate0a":
            exp = explained_mask(dist, events, calendar)
            unexp = dist[~exp]
            obj = {
                "events_source": src, "event_stocks": len(events),
                "distortion_days": int(len(dist)),
                "explained": int(exp.sum()), "unexplained": int(len(unexp)),
                "unexplained_stocks": int(unexp.code.nunique()),
                "unexplained_top": unexp.groupby("code").size()
                                        .sort_values(ascending=False).head(60).to_dict(),
                "unexplained_detail": unexp[["code", "trade_date"]].to_dict("records"),
            }
            _dump("gate0a.json", obj)
            print(f"Gate0-A 匹配: 解释 {exp.sum()} / 未解释 {len(unexp)}"
                  f"（票 {unexp.code.nunique()}）")
            return 0
        if args.cmd == "gate0a-repair":
            exp = explained_mask(dist, events, calendar)
            unexp_codes = sorted(dist[~exp].code.unique().tolist())
            obj = gate0a_repair(unexp_codes)
            _dump("gate0a_repair.json", obj)
            print(f"重拉成功 {len(obj['repulled'])} 票；复检失败 {len(obj['failed'])} 票")
            return 0
        if args.cmd == "gate0b-precheck":
            obj = gate0b_precheck(bars)
            _dump("gate0b_precheck.json", obj)
            print(f"Gate0-B 预检: 新违例 {obj['new_violation_rows']} 行"
                  f"（{obj['new_violation_stocks']} 票），raw 原始违例 "
                  f"{obj['raw_ohlc_broken_rows']} 行")
            return 0
        if args.cmd == "gate0b-apply":
            obj = gate0b_apply()
            _dump("gate0b_apply.json", obj)
            print(f"Gate0-B 写入: backfill {obj['rows_backfilled']} 行，"
                  f"post 新违例 {obj['post']['new_violation_rows']}")
            return 0
        if args.cmd == "gate0c":
            sample = gate0c_sample(conn, dist)
            obj = gate0c_reconcile(conn, sample)
            _dump("gate0c.json", obj)
            print(f"Gate0-C: 抽样 {obj['n_sampled']} 票，整体中位 "
                  f"{obj['overall_median_diff']}, 超限票 {len(obj['over_limit_stocks'])},"
                  f" 在线失败 {obj['n_failed']}")
            return 0
        if args.cmd == "gate0d":
            obj = gate0d_check(bars, events, calendar)
            _dump("gate0d.json", obj)
            print(f"Gate0-D: 无事件对 {obj['no_event_pairs']}，违例 {obj['violations']}"
                  f"（票 {obj['violating_stocks']}）")
            return 0
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
