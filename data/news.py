"""P1.4 资讯采集：个股新闻（东财）+ 市场级新闻，INSERT OR IGNORE 幂等入库。"""
import argparse
import json
import logging
import logging.handlers
import math
import os
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
    log.addHandler(logging.handlers.RotatingFileHandler(BASE / "logs" / "news.log", encoding="utf-8", maxBytes=5_000_000, backupCount=3))
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
    """INSERT OR IGNORE 入库，返回真实新增行数（rowcount 累加，并发安全）。"""
    now = datetime.now().isoformat(timespec="seconds")
    added = 0
    for r in rows:
        pub = r.get("published_at") or ""
        if since and (not pub or pub < since):  # ISO 字符串可直接比较
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO news "
            "(code, title, content, source, url, published_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (r.get("code", ""), r.get("title", ""), r.get("content", ""),
             r.get("source", ""), r.get("url", ""), pub, now))
        added += max(cur.rowcount, 0)
    conn.commit()
    return added


def _latest(rows: list, limit: int) -> list:
    """按 published_at 降序取最新 limit 条，无时间的排最后。"""
    rows.sort(key=lambda r: (r["published_at"] != "", r["published_at"]), reverse=True)
    return rows[:limit]


def fetch_stock_news(code: str, conn=None, since=None, limit: int = 10) -> int:
    """单票最新新闻（ak.stock_news_em），取最新 limit 条，返回新增行数。

    带 since（补跑场景）时自动放大 limit 到 30：stock_news_em 只回最新 N 条，
    停机期间某票发布 >10 条会漏采，靠增量游标+放大窗口缓解。
    """
    own = conn is None
    if own:
        conn = get_conn()
    if since:
        limit = max(limit, 30)
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


_NOTICE_COL_CANDIDATES = {
    "code": ["代码", "证券代码"],
    "title": ["公告标题", "标题"],
    "date": ["公告日期", "日期", "发布日期"],
    "url": ["网址", "链接", "公告网址"],
}


def fetch_notices(conn=None, since=None, days: int = 5, limit: int = 8) -> int:
    """公告采集（ak.stock_notice_report）：减持/停复牌/业绩预告等对风控最关键，
    此前完全缺失。逐日拉全市场公告后按 watchlist 代码过滤，公告标题打【公告】前缀。
    """
    own = conn is None
    if own:
        conn = get_conn()
    codes = {item["code"] for item in CFG["watchlist"]}
    total = 0
    for offset in range(days):
        d = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
        try:
            df = ak.stock_notice_report(symbol="全部", date=d.replace("-", ""))
            time.sleep(0.8)
        except Exception as e:  # noqa: BLE001
            log.warning("公告 %s FAIL: %s", d, repr(e)[:120])
            continue
        if df is None or df.empty:
            continue
        c_col = _pick_col(df, _NOTICE_COL_CANDIDATES["code"])
        t_col = _pick_col(df, _NOTICE_COL_CANDIDATES["title"])
        d_col = _pick_col(df, _NOTICE_COL_CANDIDATES["date"])
        u_col = _pick_col(df, _NOTICE_COL_CANDIDATES["url"])
        if not c_col or not t_col:
            log.warning("公告接口列名不识别: %s", list(df.columns)[:8])
            break
        rows = []
        for _, r in df.iterrows():
            code = _clean(r.get(c_col))
            if code not in codes:
                continue
            title = _clean(r.get(t_col))
            if not title:
                continue
            rows.append({
                "code": code, "title": f"【公告】{title}",
                "content": title, "source": "notice_report",
                "url": _clean(r.get(u_col)) if u_col else "",
                "published_at": _fmt_dt(r.get(d_col)) if d_col else "",
            })
        rows = _latest(rows, limit)
        total += _save_news(conn, rows, since=since)
    log.info("公告采集: 新增 %d 条（近 %d 日）", total, days)
    if own:
        conn.close()
    return total


def fetch_market_news(conn=None, since=None, limit: int = 15) -> int:
    """市场级新闻（code=''）：快讯链（东财→新浪）与百度财经日历**并行采集**。

    此前"用第一个跑通的源 + break"导致东财正常时宏观日历永远不被采集；
    两类信息互补（快讯=事件流，日历=宏观数据发布表），都拿。
    """
    own = conn is None
    if own:
        conn = get_conn()
    n = 0
    for name in ("stock_info_global_em", "stock_info_global_sina"):
        try:
            df = getattr(ak, name)()
            time.sleep(0.8)
        except Exception as e:
            log.warning("市场新闻 %s FAIL: %s", name, repr(e)[:160])
            continue
        if df is None or df.empty:
            log.warning("市场新闻 %s 返回空, 尝试下一源", name)
            continue
        rows = _df_to_rows(df, "")
        if not rows:
            log.warning("市场新闻 %s 无有效行, 尝试下一源", name)
            continue
        for r in rows:
            r["source"] = name  # source 记实际接口名
        rows = _latest(rows, limit)
        n += _save_news(conn, rows, since=since)
        log.info("市场新闻 %s: 取 %d 条, 新增 %d", name, len(rows), n)
        break  # 快讯链内仍是互斥降级（两源内容同类）
    # 宏观日历独立采集，不受快讯链是否成功影响
    try:
        df = ak.news_economic_baidu()
        time.sleep(0.8)
        if df is not None and not df.empty:
            rows = _latest(_baidu_rows(df), limit)
            n += _save_news(conn, rows, since=since)
            log.info("市场新闻 news_economic_baidu: 取 %d 条, 累计新增 %d",
                     len(rows), n)
    except Exception as e:  # noqa: BLE001
        log.warning("市场新闻 news_economic_baidu FAIL: %s", repr(e)[:160])
    if own:
        conn.close()
    return n


def fetch_all(since=None) -> dict:
    """watchlist 全部 code + 市场级 + 公告，逐个 try/except 降级，返回 {来源: 新增条数}。"""
    # 测试逃生门：短路新闻/公告网络面（调用时读 env）
    if os.environ.get("AGSICKLE_DISABLE_NEWS") == "1":
        log.info("AGSICKLE_DISABLE_NEWS=1，跳过新闻采集")
        return {}
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
    try:
        result["notices"] = fetch_notices(conn=conn, since=since)
    except Exception as e:
        log.error("fetch_all notices FAIL: %s", repr(e)[:160])
        result["notices"] = 0
    conn.close()
    return result


def _title_similar(a: str, b: str) -> float:
    """标题 Jaccard 相似度（字符 bigram），用于同事件刷屏去重。"""
    if not a or not b:
        return 0.0
    ga = {a[i:i + 2] for i in range(len(a) - 1)}
    gb = {b[i:i + 2] for i in range(len(b) - 1)}
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def dedup_news(rows: list, threshold: float = 0.5) -> list:
    """同事件去重：输入须按时间倒序，标题相似度超阈值的保留最新一条。"""
    kept = []
    for r in rows:
        if any(_title_similar(r["title"], k["title"]) >= threshold for k in kept):
            continue
        kept.append(r)
    return kept


def get_recent_news(conn: sqlite3.Connection, code: str = "", days: int = 3,
                    name: str = "") -> list:
    """近 N 天新闻，供 AI 决策层读取。

    - 同事件去重（标题 bigram 相似度 ≥0.5 保留最新一条），此前同一回购公告
      5 条刷屏消耗决策包 token 与注意力；
    - relevance 标注：标题/正文是否命中本票简称（榜单类新闻往往是文中顺带
      提及，相关性低）；
    - 榜单类标题（含"N只/榜/排行"且正文含多票表格特征）降权排后。
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT title, content, source, published_at FROM news "
        "WHERE code = ? AND published_at != '' AND published_at >= ? "
        "ORDER BY published_at DESC",
        (code, cutoff)).fetchall()
    out = [{"title": t, "content": c, "source": s, "published_at": p}
           for t, c, s, p in rows]
    out = dedup_news(out)
    if name:
        for r in out:
            r["relevance"] = "high" if (name in r["title"] or name in r["content"]) \
                else "low"
        out.sort(key=lambda r: (r.get("relevance") != "high",))  # high 优先，稳定排序
    return out


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
