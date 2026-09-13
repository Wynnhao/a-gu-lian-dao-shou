"""P1.4 资讯采集：个股新闻（东财）+ 市场级新闻，INSERT OR IGNORE 幂等入库。"""
import argparse
import json
import logging
import math
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import akshare as ak
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from data.fetcher import get_conn  # noqa: E402

CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))

log = logging.getLogger("news")
log.setLevel(logging.INFO)
if not log.handlers:  # 避免与 fetcher 的 basicConfig 重复挂 handler
    log.addHandler(logging.FileHandler(BASE / "logs" / "news.log", encoding="utf-8"))
    log.addHandler(logging.StreamHandler())
log.propagate = False

# 各接口中文列名候选（实测：stock_news_em=新闻标题/新闻内容/发布时间/文章来源/新闻链接，
# stock_info_global_em=标题/摘要/发布时间/链接，stock_info_global_sina=时间/内容）
_COL_CANDIDATES = {
    "title": ["新闻标题", "标题"],
    "content": ["新闻内容", "内容", "摘要"],
    "published": ["发布时间", "时间"],
    "source": ["文章来源", "来源"],
    "url": ["新闻链接", "链接", "网址"],
}


def _clean(v) -> str:
    """单元格转干净字符串，NaN/None -> ''。"""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v).strip()


def _fmt_dt(v) -> str:
    """发布时间统一为 '%Y-%m-%d %H:%M:%S'，解析失败存空串。"""
    s = _clean(v)
    if not s:
        return ""
    try:
        return pd.to_datetime(s).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _pick_col(df: pd.DataFrame, names: list) -> str:
    for n in names:
        if n in df.columns:
            return n
    return ""


def _df_to_rows(df: pd.DataFrame, code: str) -> list:
    """通用列名归一 -> news 行 dict 列表；无标题时用内容前40字兜底。"""
    t_col = _pick_col(df, _COL_CANDIDATES["title"])
    c_col = _pick_col(df, _COL_CANDIDATES["content"])
    p_col = _pick_col(df, _COL_CANDIDATES["published"])
    s_col = _pick_col(df, _COL_CANDIDATES["source"])
    u_col = _pick_col(df, _COL_CANDIDATES["url"])
    rows = []
    for _, r in df.iterrows():
        content = _clean(r.get(c_col)) if c_col else ""
        title = _clean(r.get(t_col)) if t_col else content[:40]
        if not title and not content:
            continue
        rows.append({
            "code": code,
            "title": title,
            "content": content,
            "source": _clean(r.get(s_col)) if s_col else "",
            "url": _clean(r.get(u_col)) if u_col else "",
            "published_at": _fmt_dt(r.get(p_col)) if p_col else "",
        })
    return rows


def _baidu_rows(df: pd.DataFrame) -> list:
    """news_economic_baidu 是财经日历（日期/时间/地区/事件/公布/预期/前值），单独拼装。"""
    rows = []
    for _, r in df.iterrows():
        event = _clean(r.get("事件"))
        if not event:
            continue
        d, t = _clean(r.get("日期")), _clean(r.get("时间"))
        pub = _fmt_dt(f"{d} {t}" if d else t)
        content = (f"{_clean(r.get('地区'))} {event} 公布:{_clean(r.get('公布'))} "
                   f"预期:{_clean(r.get('预期'))} 前值:{_clean(r.get('前值'))}").strip()
        rows.append({"code": "", "title": event, "content": content,
                     "source": "", "url": "", "published_at": pub})
    return rows


def _save_news(conn: sqlite3.Connection, rows: list, since=None) -> int:
    """INSERT OR IGNORE 入库，返回真实新增行数；since 时早于该日/无时间的旧闻不入库。"""
    now = datetime.now().isoformat(timespec="seconds")
    before = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
    for r in rows:
        pub = r.get("published_at") or ""
        if since and (not pub or pub < since):  # ISO 字符串可直接比较
            continue
        conn.execute(
            "INSERT OR IGNORE INTO news "
            "(code, title, content, source, url, published_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (r.get("code", ""), r.get("title", ""), r.get("content", ""),
             r.get("source", ""), r.get("url", ""), pub, now))
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM news").fetchone()[0] - before


def _latest(rows: list, limit: int) -> list:
    """按 published_at 降序取最新 limit 条，无时间的排最后。"""
    rows.sort(key=lambda r: (r["published_at"] != "", r["published_at"]), reverse=True)
    return rows[:limit]


def fetch_stock_news(code: str, conn=None, since=None, limit: int = 10) -> int:
    """单票最新新闻（ak.stock_news_em），取最新 limit=10 条，返回新增行数。"""
    own = conn is None
    if own:
        conn = get_conn()
    n = 0
    try:
        df = ak.stock_news_em(symbol=code)
        time.sleep(0.8)  # 温和限速
        if df is None or df.empty:
            log.warning("%s stock_news_em 返回空", code)
        else:
            rows = _latest(_df_to_rows(df, code), limit)
            n = _save_news(conn, rows, since=since)
            log.info("%s stock_news_em: 取 %d 条, 新增 %d", code, len(rows), n)
    except Exception as e:
        log.error("%s stock_news_em FAIL: %s", code, repr(e)[:160])
    finally:
        if own:
            conn.close()
    return n


def fetch_market_news(conn=None, since=None, limit: int = 15) -> int:
    """市场级新闻（code=''），依次尝试东财全球财经快讯/新浪/百度，用第一个跑通的源。"""
    own = conn is None
    if own:
        conn = get_conn()
    n = 0
    for name in ("stock_info_global_em", "stock_info_global_sina", "news_economic_baidu"):
        try:
            df = getattr(ak, name)()
            time.sleep(0.8)
        except Exception as e:
            log.warning("市场新闻 %s FAIL: %s", name, repr(e)[:160])
            continue
        if df is None or df.empty:
            log.warning("市场新闻 %s 返回空, 尝试下一源", name)
            continue
        rows = _baidu_rows(df) if name == "news_economic_baidu" else _df_to_rows(df, "")
        if not rows:
            log.warning("市场新闻 %s 无有效行, 尝试下一源", name)
            continue
        for r in rows:
            r["source"] = name  # source 记实际接口名
        rows = _latest(rows, limit)
        n = _save_news(conn, rows, since=since)
        log.info("市场新闻 %s: 取 %d 条, 新增 %d", name, len(rows), n)
        break  # 用第一个能跑通的源
    if own:
        conn.close()
    return n


def fetch_all(since=None) -> dict:
    """watchlist 全部 code + 市场级，逐个 try/except 降级，返回 {code: 新增条数}。"""
    conn = get_conn()
    result = {}
    for item in CFG["watchlist"]:
        code = item["code"]
        try:
            result[code] = fetch_stock_news(code, conn=conn, since=since)
        except Exception as e:
            log.error("fetch_all %s FAIL: %s", code, repr(e)[:160])
            result[code] = 0
        time.sleep(0.5)
    try:
        result["market"] = fetch_market_news(conn=conn, since=since)
    except Exception as e:
        log.error("fetch_all market FAIL: %s", repr(e)[:160])
        result["market"] = 0
    conn.close()
    return result


def get_recent_news(conn: sqlite3.Connection, code: str = "", days: int = 3) -> list:
    """近 N 天新闻（按发布时间倒序），供 AI 决策层读取。"""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT title, content, source, published_at FROM news "
        "WHERE code = ? AND published_at != '' AND published_at >= ? "
        "ORDER BY published_at DESC",
        (code, cutoff)).fetchall()
    return [{"title": t, "content": c, "source": s, "published_at": p}
            for t, c, s, p in rows]


def main():
    ap = argparse.ArgumentParser(description="P1.4 资讯采集入库（个股+市场级新闻）")
    ap.add_argument("--since", default=None,
                    help="仅入库 published_at >= 该日(YYYY-MM-DD)的新闻，"
                         "无时间或早于该日的旧闻不入库")
    args = ap.parse_args()
    result = fetch_all(since=args.since)
    print("== 资讯采集完成，各来源新增条数 ==")
    for k, v in result.items():
        print(f"  {k}: +{v}")


if __name__ == "__main__":
    main()
