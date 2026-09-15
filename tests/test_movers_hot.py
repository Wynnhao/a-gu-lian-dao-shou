"""动态池测试：异动五规则（合成日线）+ 题材关键词计数 + 池读写留痕。全部离线。

直跑：python3 tests/test_movers_hot.py
"""
import os
import sqlite3
import sys
import traceback
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

os.environ.setdefault("AGSICKLE_DISABLE_LIVE_QUOTES", "1")

from data.fetcher import DDL     # noqa: E402
from signals import dynpool      # noqa: E402
from signals import hot          # noqa: E402
from signals import movers as mv  # noqa: E402

_TESTS = []


def test(fn):
    _TESTS.append(fn)
    return fn


def _mem():
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    return conn


def _seed_stock(conn, code="600519", name="贵州茅台", days=70,
                last_close=100.0, last_pct=0.0, last_vol=1000,
                last_high=None, last_low=None, vol_profile=None,
                pct_profile=None):
    """合成日线：从早到晚 days 行，最后一行为 last_* 指定的当日。"""
    base = last_close / (1 + (last_pct or 0) / 100)
    rows = []
    from datetime import date, timedelta
    d0 = date(2026, 5, 1)
    vol = vol_profile or [1000] * (days - 1)
    pct = pct_profile or [0.0] * (days - 1)
    close = base
    for i in range(days - 1):
        close = close / (1 + pct[i] / 100)
        rows.append((code, (d0 + timedelta(days=i)).isoformat(),
                     close, close * 1.02, close * 0.98, close, vol[i], 1e6, pct[i], 1.0))
    rows.reverse()  # 递推从最早开始，需要正序
    # 重新按正序递推
    rows = []
    closes = [base]
    for i in range(days - 1):
        closes.append(closes[-1] * (1 + pct[i] / 100))
    for i, c in enumerate(closes):
        is_last = i == days - 1
        hi = last_high if (is_last and last_high) else c * 1.02
        lo = last_low if (is_last and last_low) else c * 0.98
        v = last_vol if is_last else vol[i]
        p = last_pct if is_last else pct[i]
        rows.append((code, (d0 + timedelta(days=i)).isoformat(),
                     c * 0.999, hi, lo, c, v, 1e6, p, 1.0))
    conn.executemany("INSERT INTO daily_bar (code, trade_date, open, high, low, close,"
                     " volume, amount, pct_chg, turnover) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     rows)
    conn.execute("INSERT INTO stock_info VALUES (?,?,?,?)",
                 (code, name, (d0).isoformat(), "x"))
    conn.commit()


@test
def test_movers_volume_spike_and_gain():
    conn = _mem()
    # 近20日均量1000，当日放量3倍+涨6% → 命中涨幅+放量
    _seed_stock(conn, "600519", "贵州茅台", last_close=106.0, last_pct=6.0,
                last_vol=3000)
    rows = mv.compute_watchlist_movers(conn)
    hit = [r for r in rows if r["code"] == "600519"]
    assert hit, "应命中异动"
    text = ";".join(hit[0]["reason"])
    assert "涨幅" in text and "放量" in text, hit[0]["reason"]
    conn.close()


@test
def test_movers_new_low_and_crash():
    conn = _mem()
    # 5日连跌共 -15% → 急跌；且跌破前60日低点 → 新低
    conn2 = _mem()
    from datetime import date, timedelta
    d0 = date(2026, 5, 1)
    rows = []
    close = 100.0
    for i in range(70):
        rows.append(("000001", (d0 + timedelta(days=i)).isoformat(),
                     close * 0.999, close * 1.01, close * 0.99, close, 1000, 1e6, 0.0, 1.0))
        close *= 0.995
    # 最后5天急跌
    for j, p in enumerate([-3.0, -3.0, -3.0, -3.0, -3.0]):
        code, td, o, h, l, c, v, a, _, t = rows[-5 + j]
        c2 = rows[-6 + j][5] * (1 + p / 100)
        rows[-5 + j] = (code, td, c2 * 0.999, c2 * 1.005, c2 * 0.985, c2, 2000, a, p, t)
    conn2.executemany("INSERT INTO daily_bar (code, trade_date, open, high, low, close,"
                      " volume, amount, pct_chg, turnover) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      rows)
    conn2.execute("INSERT INTO stock_info VALUES (?,?,?,?)",
                  ("000001", "平安银行", d0.isoformat(), "x"))
    hits = [r for r in mv.compute_watchlist_movers(conn2) if r["code"] == "000001"]
    assert hits, "急跌应命中"
    text = ";".join(hits[0]["reason"])
    assert "5日" in text and ("新低" in text or "急跌" in text), hits[0]["reason"]
    conn.close()
    conn2.close()


@test
def test_movers_quiet_stock_not_selected():
    conn = _mem()
    _seed_stock(conn, "600519", "贵州茅台", last_close=100.0, last_pct=0.5, last_vol=1000)
    rows = [r for r in mv.compute_watchlist_movers(conn) if r["code"] == "600519"]
    assert not rows, "温和走势不应入池"
    conn.close()


@test
def test_movers_exdiv_day_no_fake_alerts():
    """除权日（原始 -25% vs 复权 +1%）：涨幅/急跌/新低不再假异动（2026-09-15 复核修复）。"""
    conn = _mem()
    _seed_stock(conn, "600519", "贵州茅台", days=70,
                last_close=75.0, last_pct=-25.0, last_vol=1000)
    # 给最后两行补 close_qfq：除权因子使复权涨幅仅 +1%（真实走势平稳）
    tds = [r[0] for r in conn.execute(
        "SELECT trade_date FROM daily_bar WHERE code='600519' ORDER BY trade_date")]
    conn.execute("UPDATE daily_bar SET close_qfq=100.0 WHERE code='600519' AND trade_date=?",
                 (tds[-2],))
    conn.execute("UPDATE daily_bar SET close_qfq=101.0 WHERE code='600519' AND trade_date=?",
                 (tds[-1],))
    conn.commit()
    hits = [r for r in mv.compute_watchlist_movers(conn) if r["code"] == "600519"]
    assert not hits, "除权日不应因假跌/假新低入池: %s" % (hits and hits[0]["reason"])
    conn.close()


@test
def test_hot_theme_keyword_count():
    conn = _mem()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    news = [("GPU 订单大增 算力需求爆发", "", now), ("光模块龙头业绩超预期 AI 算力扩产", "", now),
            ("大模型竞赛推高算力成本", "", now), ("智算中心落地", "", now),
            ("算力租赁价格上行", "", now)]
    for t, c, ts in news:
        conn.execute("INSERT INTO news (code,title,content,source,url,published_at,fetched_at)"
                     " VALUES ('',?,?,'t','',?,'x')", (t, c, ts))
    # 干扰项：不相关新闻
    conn.execute("INSERT INTO news (code,title,content,source,url,published_at,fetched_at)"
                 " VALUES ('','白酒提价','','t','',?,'x')", (now,))
    conn.commit()
    out = hot.compute_hot_themes(conn)
    ai = [t for t in out if t["theme"] == "AI/算力"]
    assert ai and ai[0]["hits"] >= 5 and ai[0]["hot"], out
    assert ai[0]["related_stocks"], "AI 题材应联动自选池 AI 组"
    conn.close()


@test
def test_hot_stock_news_surge():
    conn = _mem()
    conn.execute("INSERT INTO stock_info VALUES (?,?,?,?)", ("600519", "贵州茅台", "x", "x"))
    now = datetime.now()
    from datetime import timedelta
    # 近24小时3条（≥min_news=3），前7天日均 0.14（仅1条）
    for i in range(3):
        ts = (now - timedelta(hours=2 + i)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT INTO news (code,title,content,source,url,published_at,fetched_at)"
                     " VALUES ('600519',?,'','t','',?,'x')", ("新闻%d" % i, ts))
    ts_old = (now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("INSERT INTO news (code,title,content,source,url,published_at,fetched_at)"
                 " VALUES ('600519','旧闻','','t','',?,'x')", (ts_old,))
    conn.commit()
    out = hot.compute_hot_stocks(conn)
    assert any(s["code"] == "600519" for s in out), out
    conn.close()


@test
def test_pool_io_roundtrip_and_current():
    conn = _mem()
    dynpool.upsert_pool_rows(conn, "movers", [
        {"code": "600519", "name": "贵州茅台", "reason": ["涨幅6%"], "strength": 1.5}],
        "2026-09-10")
    dynpool.upsert_pool_rows(conn, "movers", [
        {"code": "000001", "name": "平安银行", "reason": ["放量"], "strength": 1.0}],
        "2026-09-11")
    cur = dynpool.current_pool(conn, "movers")
    assert [r["code"] for r in cur] == ["000001"], "当前成员应只含最新刷新日"
    assert dynpool.pool_dates(conn, "movers") == ["2026-09-11", "2026-09-10"]
    conn.close()


def main() -> int:
    failed = 0
    for fn in _TESTS:
        try:
            fn()
            print("PASS %s" % fn.__name__)
        except Exception:  # noqa: BLE001
            failed += 1
            print("FAIL %s" % fn.__name__)
            traceback.print_exc()
    print("%d/%d tests passed" % (len(_TESTS) - failed, len(_TESTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
