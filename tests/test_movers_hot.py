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


test.__test__ = False  # pytest 不要把装饰器本身当测试收集


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
    # （W-B5 后自选池口径只出 core 票，样本票改用 600519）
    conn2 = _mem()
    from datetime import date, timedelta
    d0 = date(2026, 5, 1)
    rows = []
    close = 100.0
    for i in range(70):
        rows.append(("600519", (d0 + timedelta(days=i)).isoformat(),
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
                  ("600519", "贵州茅台", d0.isoformat(), "x"))
    hits = [r for r in mv.compute_watchlist_movers(conn2) if r["code"] == "600519"]
    assert hits, "急跌应命中"
    text = ";".join(hits[0]["reason"])
    assert "5日" in text and ("新低" in text or "急跌" in text), hits[0]["reason"]
    conn.close()
    conn2.close()


@test
def test_movers_watchlist_filter_excludes_non_core():
    """W-B5（P1-12）：自选池口径只出 watchlist_core 票——池外票（000001 不在
    config.watchlist_core）即使触发五规则也不入池，不再全库 816 票扫池。"""
    conn = _mem()
    from datetime import date, timedelta
    d0 = date(2026, 5, 1)
    rows = []
    close = 100.0
    for i in range(70):
        rows.append(("000001", (d0 + timedelta(days=i)).isoformat(),
                     close * 0.999, close * 1.01, close * 0.99, close, 1000, 1e6, 0.0, 1.0))
        close *= 0.995
    for j, p in enumerate([-3.0, -3.0, -3.0, -3.0, -3.0]):
        code, td, o, h, l, c, v, a, _, t = rows[-5 + j]
        c2 = rows[-6 + j][5] * (1 + p / 100)
        rows[-5 + j] = (code, td, c2 * 0.999, c2 * 1.005, c2 * 0.985, c2, 2000, a, p, t)
    conn.executemany("INSERT INTO daily_bar (code, trade_date, open, high, low, close,"
                     " volume, amount, pct_chg, turnover) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     rows)
    conn.execute("INSERT INTO stock_info VALUES (?,?,?,?)",
                 ("000001", "平安银行", d0.isoformat(), "x"))
    conn.commit()
    out = mv.compute_watchlist_movers(conn)
    assert not out, "池外票不应进自选池异动: %s" % out
    conn.close()


@test
def test_hot_stock_watchlist_filter_excludes_non_core():
    """W-B5（P1-12）：热门个股只出 watchlist_core 票。000001 有新闻突增但池外 →
    不入池；对照 600519（池内）同量新闻可入。"""
    conn = _mem()
    now = datetime.now()
    from datetime import timedelta
    for code in ("000001", "600519"):
        conn.execute("INSERT INTO stock_info VALUES (?,?,?,?)",
                     (code, "X", "x", "x"))
        for i in range(3):
            ts = (now - timedelta(hours=2 + i)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("INSERT INTO news (code,title,content,source,url,published_at,fetched_at)"
                         " VALUES (?,?,'','t','',?,'x')", (code, "新闻%d" % i, ts))
    conn.commit()
    out = hot.compute_hot_stocks(conn)
    codes = {s["code"] for s in out}
    assert "000001" not in codes, "池外票不应进热门个股: %s" % codes
    assert "600519" in codes, "池内票应正常入池: %s" % codes
    conn.close()


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


# ============================================================
# 批次3b · 任务6：候选 pct 自洽预检（审查 P2"600016 已进 movers 池缺预检"）
# ============================================================

def _seed_two_bars(conn, code, prev_close, last_close, last_pct,
                   prev_cq=None, last_cq=None):
    """两根自洽日线（前一根 prev_close、后一根 last_close/last_pct）。"""
    from datetime import date, timedelta
    d0 = date(2026, 9, 1)
    for i, (c, cq) in enumerate([(prev_close, prev_cq), (last_close, last_cq)]):
        conn.execute(
            "INSERT INTO daily_bar (code, trade_date, open, high, low, close,"
            " volume, amount, pct_chg, turnover, close_qfq) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (code, (d0 + timedelta(days=i)).isoformat(), c, c * 1.01, c * 0.99,
             c, 1e6, 6e7, last_pct if i == 1 else 0.0, 1.0, cq))
    conn.commit()


@test
def test_task6_market_movers_dirty_pct_dropped():
    """正用例（600016 场景）：快照价锚定库内 close（±0.5%）但 pct 与价格反推
    偏差 >2pp（脏快照签名）→ 候选丢弃并计数留痕，不入池。"""
    conn = _mem()
    _seed_two_bars(conn, "600016", prev_close=4.00, last_close=4.00, last_pct=0.0)
    spot = [{"代码": "600016", "名称": "民生银行", "最新价": 4.00, "涨跌幅": 9.9,
             "量比": 5.0, "成交额": 6e7, "振幅": 3.0}]
    rows = mv.compute_market_movers(spot, top_n=10, conn=conn)
    assert rows == [], "脏 pct 候选不得入池: %s" % rows
    assert mv.PCT_PRECHECK_LAST["dropped"] == 1, mv.PCT_PRECHECK_LAST
    assert mv.PCT_PRECHECK_LAST["kept"] == 0
    conn.close()


@test
def test_task6_market_movers_consistent_pct_kept():
    """反用例：pct 与价格反推自洽（4.00→4.24 = +6%）→ 正常入池（对照组）。"""
    conn = _mem()
    _seed_two_bars(conn, "600016", prev_close=4.00, last_close=4.24, last_pct=6.0)
    spot = [{"代码": "600016", "名称": "民生银行", "最新价": 4.24, "涨跌幅": 6.0,
             "量比": 3.0, "成交额": 6e7, "振幅": 3.0}]
    rows = mv.compute_market_movers(spot, top_n=10, conn=conn)
    assert len(rows) == 1 and rows[0]["code"] == "600016", rows
    assert any("涨幅" in r for r in rows[0]["reason"])
    assert mv.PCT_PRECHECK_LAST["dropped"] == 0
    conn.close()


@test
def test_task6_market_movers_band_violation_dropped_even_without_conn():
    """正用例（停板带）：|pct| 超板幅+0.5pp → 单日不可能，conn=None（无法锚价）
    也丢弃——同口径覆盖 test_sprint4_d 既有"无 conn 保留正常票"的语义。"""
    spot = [{"代码": "600016", "名称": "民生银行", "最新价": 4.00, "涨跌幅": 12.0,
             "量比": 5.0, "成交额": 6e7, "振幅": 3.0}]
    rows = mv.compute_market_movers(spot, top_n=10, conn=None)
    assert rows == [], rows
    assert mv.PCT_PRECHECK_LAST["dropped"] == 1


@test
def test_task6_market_movers_unanchored_price_not_overdropped():
    """反用例（防误杀）：快照价锚不上库内 close（盘中实时价/数据滞后）→ 只做
    停板带检查，pct 合法即放行（预检不得把正常候选清空）。"""
    conn = _mem()
    _seed_two_bars(conn, "600016", prev_close=4.00, last_close=4.24, last_pct=6.0)
    spot = [{"代码": "600016", "名称": "民生银行", "最新价": 4.77, "涨跌幅": 6.0,
             "量比": 3.0, "成交额": 6e7, "振幅": 3.0}]
    rows = mv.compute_market_movers(spot, top_n=10, conn=conn)
    assert len(rows) == 1 and rows[0]["code"] == "600016", rows
    conn.close()


@test
def test_task6_hot_stock_dirty_bar_pct_dropped():
    """正用例：热门个股候选（新闻阈值已过）latest bar pct 与日线价格反推偏差
    >2pp（脏 bar 签名）→ 丢弃并计数；对照自洽 bar 正常入池。"""
    conn = _mem()
    conn.execute("INSERT INTO stock_info VALUES (?,?,?,?)", ("600519", "贵州茅台", "x", "x"))
    now = datetime.now()
    from datetime import timedelta
    for i in range(3):
        ts = (now - timedelta(hours=2 + i)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT INTO news (code,title,content,source,url,published_at,fetched_at)"
                     " VALUES ('600519',?,'','t','',?,'x')", ("新闻%d" % i, ts))
    # 脏 bar：价格自岿然不动，pct 却 +9%
    _seed_two_bars(conn, "600519", prev_close=10.0, last_close=10.0, last_pct=9.0,
                   prev_cq=10.0, last_cq=10.0)
    conn.commit()
    out = hot.compute_hot_stocks(conn)
    assert not any(s["code"] == "600519" for s in out), out
    assert hot._LAST_PCT_DROPPED == 1, hot._LAST_PCT_DROPPED
    # 对照：pct 与价格自洽 → 正常入池
    conn.execute("DELETE FROM daily_bar WHERE code='600519'")
    _seed_two_bars(conn, "600519", prev_close=10.0, last_close=10.6, last_pct=6.0,
                   prev_cq=10.0, last_cq=10.6)
    conn.commit()
    out2 = hot.compute_hot_stocks(conn)
    assert any(s["code"] == "600519" for s in out2), out2
    assert hot._LAST_PCT_DROPPED == 0
    conn.close()


@test
def test_task6_hot_stock_exday_bar_conservative_keep():
    """反用例（保守放行）：除权事件日（d(t) 跳变）raw pct 与价格反推天然背离
    → 不做预检丢弃（除权日跳过校验，与 audit/movers 保守语义一致）。"""
    conn = _mem()
    conn.execute("INSERT INTO stock_info VALUES (?,?,?,?)", ("600519", "贵州茅台", "x", "x"))
    now = datetime.now()
    from datetime import timedelta
    for i in range(3):
        ts = (now - timedelta(hours=2 + i)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT INTO news (code,title,content,source,url,published_at,fetched_at)"
                     " VALUES ('600519',?,'','t','',?,'x')", ("新闻%d" % i, ts))
    # 除权日：raw -30% 假跌、qfq 连续（d_jump=3.5 > 0.01 除权签名）
    _seed_two_bars(conn, "600519", prev_close=10.0, last_close=7.0, last_pct=-30.0,
                   prev_cq=10.0, last_cq=10.5)
    conn.commit()
    out = hot.compute_hot_stocks(conn)
    assert any(s["code"] == "600519" for s in out), out
    assert hot._LAST_PCT_DROPPED == 0
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
