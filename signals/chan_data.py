"""缠论 R3 批次 0 —— 纯研究数据层（只读）。

施工方案：docs/缠论R3施工方案-2026-09-19.md §3.1（数据）+ §3.6（口径钉死补遗）。
本模块只依赖 numpy / pandas / 标准库，不 import 任何生产模块；除 load_core_bars()
外全部为纯函数（接受 DataFrame / 日历列表参数，可单测、测试零 DB）。

口径要点（§3.1 + §3.6 冻结，批次 0 commit 后不可变）：
- open_qfq = open + (close_qfq − close)，逐 bar 构造（§3.1）。
- 畸变日：|ret_qfq − ret_raw| > 1pp；ret_raw = close 的 pct_change，ret_qfq =
  close_qfq 的 pct_change，不用 pct_chg 列（§3.6 第 2 条）。
- 断链：按 trade_calendar，票内相邻 bar 间隔 >1 交易日即断链；缺失日历交易日数 =
  间隔 −1（§3.6 第 14d 条）。结构引擎按断链切段独立计算，不跨链连笔。
- warmup：每票全史前 120 根有效 bar（OHLC+三复权列全非空）之后才允许产生信号；
  信号允许起始日 = 第 120 根有效 bar 的交易日（§3.6 第 14c 条）。
- 测试窗 TEST_START = 2024-07-01（§3.1）。

只读纪律：DB 一律 sqlite3.connect("file:...market.db?mode=ro", uri=True)，
不写任何库表。运行方（chan_probe_estimate / 批次 2b chan_research）自行负责
退出码语义；本模块不做 gate 裁决。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
DB_PATH = BASE / "data" / "market.db"
CONFIG_PATH = BASE / "config.json"

TEST_START = "2024-07-01"      # 测试窗起点（§3.1）
WARMUP_BARS = 120              # 有效 bar warmup（§3.1）
DISTORTION_THRESHOLD = 0.01    # |ret_qfq − ret_raw| > 1pp（§3.1/§3.6-2）

BAR_COLUMNS = ["trade_date", "open", "high", "low", "close",
               "close_qfq", "high_qfq", "low_qfq"]
VALID_COLUMNS = ["open", "high", "low", "close", "close_qfq", "high_qfq", "low_qfq"]


# ---------------------------------------------------------------- 纯函数层

def construct_open_qfq(df: pd.DataFrame) -> pd.Series:
    """open_qfq = open + (close_qfq − close)，逐 bar（§3.1 冻结口径）。

    要求 df 含 open / close / close_qfq 三列；返回与 df 等长的 Series。
    """
    return df["open"] + (df["close_qfq"] - df["close"])


def with_open_qfq(df: pd.DataFrame) -> pd.DataFrame:
    """返回带 open_qfq 列的副本（不改动入参）。"""
    out = df.copy()
    out["open_qfq"] = construct_open_qfq(out)
    return out


def distortion_days(df: pd.DataFrame) -> set:
    """畸变日集合：|ret_qfq − ret_raw| > 1pp 的 trade_date（§3.6 第 2 条）。

    df 须按 trade_date 升序、含 trade_date / close / close_qfq 列。
    ret_raw = close.pct_change()，ret_qfq = close_qfq.pct_change()（票内 bar 序，
    不用 pct_chg 列）；首根 NaN 不判；判据严格大于（=1pp 不算）。
    """
    ret_raw = df["close"].pct_change()
    ret_qfq = df["close_qfq"].pct_change()
    mask = (ret_qfq - ret_raw).abs() > DISTORTION_THRESHOLD
    return set(df.loc[mask.fillna(False), "trade_date"])


def chain_breaks(dates: list, calendar: list) -> list:
    """断链清单：[(缺口前交易日, 缺口后交易日, 缺失日历交易日数), ...]（§3.1/§3.6-14d）。

    dates = 该票升序 trade_date 列表；calendar = 全市场交易日历升序列表。
    票内相邻两根 bar 的日历间隔 >1 个交易日即断链；缺失数 = 间隔 − 1。
    任一票内日期不在日历中 → ValueError（数据层从严，不静默跳过）。
    """
    pos = {d: i for i, d in enumerate(calendar)}
    missing_dates = [d for d in dates if d not in pos]
    if missing_dates:
        raise ValueError(f"票内 bar 日期不在 trade_calendar 中: {missing_dates[:5]}")
    out = []
    for d1, d2 in zip(dates, dates[1:]):
        gap = pos[d2] - pos[d1]
        if gap > 1:
            out.append((d1, d2, gap - 1))
    return out


def valid_mask(df: pd.DataFrame) -> pd.Series:
    """有效 bar 掩码：OHLC + 三复权列全非空（§3.6 第 14c 条）。"""
    return df[VALID_COLUMNS].notna().all(axis=1)


def warmup_start_date(df: pd.DataFrame):
    """信号允许起始日 = 第 120 根有效 bar 的交易日；有效 bar 不足 120 → None。"""
    ok = df.loc[valid_mask(df), "trade_date"]
    if len(ok) < WARMUP_BARS:
        return None
    return ok.iloc[WARMUP_BARS - 1]


def calendar_index(calendar: list) -> dict:
    """{trade_date: 日历序号}，供 60 交易日窗口等按 trade_calendar 计数的规则用。"""
    return {d: i for i, d in enumerate(calendar)}


def window_before(calendar: list, anchor: str, n: int, pos: dict = None) -> list:
    """以 anchor 为末根、按 trade_calendar 往前取 n 个交易日（含 anchor 本身，
    §3.6 第 3 条）。anchor 须在日历内；历史不足 n 时返回可得的截断窗口。
    pos 可传入预构建的 calendar_index() 复用。"""
    if pos is None:
        pos = calendar_index(calendar)
    if anchor not in pos:
        raise ValueError(f"anchor {anchor} 不在 trade_calendar 中")
    i = pos[anchor]
    return calendar[max(0, i - n + 1): i + 1]


# ---------------------------------------------------------------- DB 加载（唯一入口）

def core_codes(config_path: Path = CONFIG_PATH) -> list:
    """config.json watchlist_core 的 51 个代码（§3.1 样本）。"""
    cfg = json.loads(Path(config_path).read_text())
    return [e["code"] for e in cfg["watchlist_core"]]


def load_core_bars(db_path: Path = DB_PATH, config_path: Path = CONFIG_PATH
                   ) -> tuple:
    """加载 core 51 逐票 DataFrame 与交易日历（只读）。

    返回 (bars_by_code, calendar)：
    - bars_by_code: {code: DataFrame(trade_date, open, high, low, close,
      close_qfq, high_qfq, low_qfq, open_qfq)}，按 trade_date 升序；
    - calendar: trade_calendar 全表日期升序列表。
    纪律：sqlite URI mode=ro，不写任何库表。
    """
    codes = core_codes(config_path)
    ph = ",".join("?" * len(codes))
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    try:
        bars = pd.read_sql_query(
            f"SELECT code, trade_date, open, high, low, close, close_qfq, "
            f"high_qfq, low_qfq FROM daily_bar WHERE code IN ({ph}) "
            f"ORDER BY code, trade_date", conn, params=codes)
        cal = pd.read_sql_query(
            "SELECT date FROM trade_calendar ORDER BY date", conn)
    finally:
        conn.close()
    calendar = cal["date"].tolist()
    bars_by_code = {}
    for code, g in bars.groupby("code", sort=True):
        g = g.drop(columns=["code"]).reset_index(drop=True)
        bars_by_code[code] = with_open_qfq(g)
    return bars_by_code, calendar
