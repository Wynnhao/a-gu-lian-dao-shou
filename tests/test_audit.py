"""data/audit 停板口径与 pct_out_of_range 三类豁免测试（2026-09-15 人工复核后落地）。

check_db 只查 daily_bar，用内联最小建表避免连带 import fetcher（akshare 重依赖）。
直跑：python3 tests/test_audit.py
"""
import sqlite3
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.audit import _limit_pct, check_db     # noqa: E402

DDL = """
CREATE TABLE daily_bar (
    code TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL, pct_chg REAL, turnover REAL,
    source TEXT, close_qfq REAL, high_qfq REAL, low_qfq REAL,
    PRIMARY KEY (code, trade_date)
);
"""


def _mem() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    return conn


def _add(conn, code, td, close, pct, cq=None):
    """加一行自洽数据（量纲/OHLC 合法，避免混入无关告警）。"""
    volume = 1e6
    conn.execute(
        "INSERT INTO daily_bar (code, trade_date, open, high, low, close,"
        " volume, amount, pct_chg, close_qfq) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (code, td, close, close * 1.01, close * 0.99, close,
         volume, close * volume * 100, pct, cq))


def _pct_flags(conn):
    issues, total = check_db(conn)
    return [i for i in issues if i["kind"] == "pct_out_of_range"], total


def test_limit_pct_board_bands():
    assert _limit_pct("600519") == 10.5
    assert _limit_pct("300750") == 20.5
    assert _limit_pct("302132") == 20.5    # 创业板新代码段（此前误按 10.5）
    assert _limit_pct("688801") == 20.5
    assert _limit_pct("830799") == 30.5
    assert _limit_pct("920002") == 30.5


def test_302_band_no_longer_flagged():
    conn = _mem()
    _add(conn, "302132", "2026-05-20", 10.0, None)
    _add(conn, "302132", "2026-05-21", 12.0, 19.5, cq=12.0)
    flags, _ = _pct_flags(conn)
    assert flags == [], flags


def test_new_stock_first5days_exempt():
    conn = _mem()
    # 主板新股第2个交易日 -24.76%（无涨跌幅限制）→ 豁免
    _add(conn, "001221", "2026-05-20", 30.0, None)
    _add(conn, "001221", "2026-05-21", 22.6, -24.76)
    flags, _ = _pct_flags(conn)
    assert flags == [], flags
    # 第7个交易日（row_no=6）超停板 → 仍要报
    for i in range(2, 7):
        _add(conn, "001221", f"2026-05-{20 + i}", 22.6, 0.0)
    _add(conn, "001221", "2026-05-27", 25.3, 12.0)
    flags, _ = _pct_flags(conn)
    assert len(flags) == 1 and flags[0]["code"] == "001221", flags


def test_exdiv_exempt_but_real_crash_still_flagged():
    conn = _mem()
    # 除权假跌：原始 -25%，前复权 +1%（10.0→10.1）→ 豁免（垫 7 行历史走出新股窗口）
    for i in range(7):
        _add(conn, "000999", "2026-05-%02d" % (10 + i), 10.0, 0.0, cq=10.0)
    _add(conn, "000999", "2026-05-17", 7.5, -25.0, cq=10.1)
    flags, _ = _pct_flags(conn)
    assert flags == [], flags
    # 真崩盘：复权后同样 -25% → 必须照报
    for i in range(7):
        _add(conn, "000001", "2026-05-%02d" % (10 + i), 10.0, 0.0, cq=10.0)
    _add(conn, "000001", "2026-05-17", 7.5, -25.0, cq=7.5)
    flags, _ = _pct_flags(conn)
    assert len(flags) == 1 and flags[0]["code"] == "000001", flags


def test_qfq_missing_conservative_flag():
    conn = _mem()
    # qfq 未回填时无法判定除权 → 保守照报（保持旧行为；垫 7 行走出新股窗口）
    for i in range(7):
        _add(conn, "600519", "2026-05-%02d" % (10 + i), 10.0, 0.0)
    _add(conn, "600519", "2026-05-17", 11.3, 13.0)
    flags, _ = _pct_flags(conn)
    assert len(flags) == 1 and flags[0]["code"] == "600519", flags


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print("PASS %s" % name)
        except Exception:
            failed += 1
            print("FAIL %s" % name)
            traceback.print_exc()
    print("\n%d/%d tests passed" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
