"""P1.4/P1.5 离线单测：news 去重与过滤（内存库）、macro 分位计算边界。不依赖网络。"""
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from data.fetcher import DDL  # noqa: E402
from data.macro import pct_rank, rolling_price_pct  # noqa: E402
from data.news import _df_to_rows, _fmt_dt, _save_news, get_recent_news  # noqa: E402


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    return conn


def _row(code="600519", title="标题A", content="内容A", published_at="2026-09-10 10:00:00"):
    return {"code": code, "title": title, "content": content, "source": "测试源",
            "url": "http://example.com/a", "published_at": published_at}


def test_save_news_dedup():
    """UNIQUE(code,title,published_at) 去重：第二次相同数据新增 0 行。"""
    conn = _mem_conn()
    assert _save_news(conn, [_row(), _row(title="标题B", published_at="2026-09-11 09:00:00")]) == 2
    # 完全相同再存一遍 -> 0
    assert _save_news(conn, [_row(), _row(title="标题B", published_at="2026-09-11 09:00:00")]) == 0
    # 同 title 同 code 同时间、url 不同 -> 仍被唯一键忽略 -> 0
    dup = _row()
    dup["url"] = "http://other.com"
    assert _save_news(conn, [dup]) == 0
    # 同 title 不同时间 -> 新增 1
    assert _save_news(conn, [_row(published_at="2026-09-12 08:00:00")]) == 1
    n = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
    assert n == 3, n
    conn.close()


def test_save_news_since_filter():
    """--since：published_at 为空或早于该日的旧闻不入库。"""
    conn = _mem_conn()
    rows = [_row(title="旧闻", published_at="2026-08-01 10:00:00"),
            _row(title="当日", published_at="2026-09-01 23:59:59"),
            _row(title="无时间", published_at="")]
    assert _save_news(conn, rows, since="2026-09-01") == 1
    titles = [r[0] for r in conn.execute("SELECT title FROM news").fetchall()]
    assert titles == ["当日"], titles
    # 不带 since 时全部入库
    assert _save_news(conn, rows) == 2
    conn.close()


def test_get_recent_news_filter():
    """get_recent_news 按 code + 近N天过滤，倒序返回。"""
    conn = _mem_conn()
    now = datetime.now()
    new = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    old = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    _save_news(conn, [
        _row(code="600519", title="新1", published_at=new),
        _row(code="600519", title="旧", published_at=old),
        _row(code="", title="市场1", published_at=new),
    ])
    got = get_recent_news(conn, code="600519", days=3)
    assert [g["title"] for g in got] == ["新1"], got
    assert set(got[0]) == {"title", "content", "source", "published_at"}
    # days 拉长可取到旧闻
    got = get_recent_news(conn, code="600519", days=30)
    assert sorted(g["title"] for g in got) == ["新1", "旧"]
    # 市场级 code=''
    got = get_recent_news(conn, code="", days=3)
    assert [g["title"] for g in got] == ["市场1"]
    conn.close()


def test_fmt_dt():
    """发布时间归一 '%Y-%m-%d %H:%M:%S'，解析失败存空串。"""
    assert _fmt_dt("2026-09-12 16:50:29") == "2026-09-12 16:50:29"
    assert _fmt_dt("2026/09/12 10:00") == "2026-09-12 10:00:00"
    assert _fmt_dt("2026-08-31") == "2026-08-31 00:00:00"
    assert _fmt_dt(pd.Timestamp("2026-09-12 08:30:00")) == "2026-09-12 08:30:00"
    assert _fmt_dt("不是时间") == ""
    assert _fmt_dt(None) == ""
    assert _fmt_dt(float("nan")) == ""


def test_df_to_rows_em_style():
    """东财市场快讯列名（标题/摘要/发布时间/链接）归一为 news 行。"""
    df = pd.DataFrame({
        "标题": ["快讯一", "快讯二"],
        "摘要": ["内容一", "内容二"],
        "发布时间": ["2026-09-12 16:50:29", "2026-09-12 09:00:00"],
        "链接": ["http://e.com/1", "http://e.com/2"],
    })
    rows = _df_to_rows(df, "")
    assert len(rows) == 2
    assert rows[0]["title"] == "快讯一" and rows[0]["content"] == "内容一"
    assert rows[0]["url"] == "http://e.com/1"
    assert rows[0]["published_at"] == "2026-09-12 16:50:29"
    # 无标题列时用内容前40字兜底（新浪风格 时间/内容）
    df2 = pd.DataFrame({"时间": ["2026-09-12 10:00:00"], "内容": ["x" * 50]})
    rows2 = _df_to_rows(df2, "")
    assert rows2[0]["title"] == "x" * 40 and len(rows2[0]["content"]) == 50


def test_pct_rank_edges():
    """PE 百分位边界：常数序列全部同分位、长度1为1.0、NaN 不参与。"""
    s = pct_rank(pd.Series([3.0, 3.0, 3.0, 3.0, 3.0]))
    assert s.tolist() == [0.6] * 5, s.tolist()  # 平均秩 (5+1)/2 / 5
    s1 = pct_rank(pd.Series([7.0]))
    assert s1.tolist() == [1.0]
    s2 = pct_rank(pd.Series([1.0, float("nan"), 3.0]))
    assert pd.isna(s2.iloc[1])
    # pct 分母为非 NaN 个数 2：1.0 -> 1/2, 3.0 -> 2/2
    assert abs(s2.iloc[0] - 0.5) < 1e-9 and abs(s2.iloc[2] - 1.0) < 1e-9


def test_rolling_price_pct_edges():
    """价格分位兜底口径边界：常数序列全 1.0、长度1为1.0、缺失 close 返回 None。"""
    dates = [f"2026-01-{d:02d}" for d in range(1, 11)]
    pcts = rolling_price_pct(dates, [100.0] * 10, years=5)
    assert pcts == [1.0] * 10, pcts
    assert rolling_price_pct(["2026-01-01"], [88.0], years=5) == [1.0]
    pcts = rolling_price_pct(["2026-01-01", "2026-01-02", "2026-01-03"],
                             [100.0, None, 100.0], years=5)
    assert len(pcts) == 3 and pcts[0] == 1.0 and pcts[2] == 1.0 and pcts[1] is None
    # 递增序列：最后一个点为窗口最大 -> 1.0，第一个点 -> 1.0（窗口内仅自身）
    pcts = rolling_price_pct(["2026-01-01", "2026-01-02", "2026-01-03"],
                             [10.0, 20.0, 30.0], years=5)
    assert pcts == [1.0, 1.0, 1.0]
    # 空输入
    assert rolling_price_pct([], []) == []


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"== {len(fns) - failed}/{len(fns)} passed ==")
    sys.exit(1 if failed else 0)
