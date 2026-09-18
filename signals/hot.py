"""热门池：事件驱动的热门题材与热门个股（规则计数为主，LLM 研判为辅）。

三类信号：
1. hot_theme 热门题材：市场级新闻按内置题材关键词计数，频次突增（≥min_hits 且
   明显高于基线）判定为热；能映射到自选池概念标签的联动给出代表票；
2. hot_stock 热门个股：自选池票当日新闻条数 ≥min_news 且 ≥stock_vs_avg×近7日日均；
3. board_hot 概念板块榜：东财概念板块涨幅榜 Top（接口可用时），纯增强展示。

阈值来自 config.pools.hot。题材关键词表内置起步版，可按需在代码中扩充。
"""
import json
import logging
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from common.config import load  # noqa: E402
from data import repo  # noqa: E402
from signals import dynpool  # noqa: E402

log = logging.getLogger("hot")

# 题材关键词表（起步版；值为 (关键词列表, 对应自选池概念标签或None)）
THEMES: Dict[str, tuple] = {
    "AI/算力": (["算力", "GPU", "光模块", "CPO", "大模型", "AIGC", "AI+", "人工智能",
                "智算", "英伟达"], "AI"),
    "机器人": (["机器人", "人形机器人", "具身智能"], "AI"),
    "固态电池": (["固态电池", "钠电池", "锂电"], "新能源"),
    "光伏储能": (["光伏", "储能", "硅片", "组件", "逆变器"], "新能源"),
    "消费电子/苹果链": (["苹果", "iPhone", "消费电子", "MR", "Vision"], "苹果"),
    "白酒食品": (["白酒", "食品", "饮料", "调味品"], "消费"),
    "免税文旅": (["免税", "文旅", "出行", "旅游"], "消费"),
    "农业气象": (["厄尔尼诺", "极端天气", "干旱", "粮食安全", "种业", "防汛"], "厄尔尼诺"),
    "稀土小金属": (["稀土", "小金属", "钨", "锑"], None),
    "黄金贵金属": (["黄金", "贵金属", "金价"], None),
    "低空经济": (["低空经济", "eVTOL", "通航"], None),
    "军工": (["军工", "国防", "导弹"], None),
    "医药创新药": (["创新药", "医药", "CXO", "减肥药"], None),
    "半导体设备": (["半导体", "光刻", "晶圆", "国产替代"], "AI"),
}


def _cfg() -> dict:
    return load().get("pools", {}).get("hot", {})


def _concept_map(conn: sqlite3.Connection) -> Dict[str, List[str]]:
    """概念标签 -> 该组自选票列表（来自 config watchlist[].concepts）。"""
    m: Dict[str, List[str]] = {}
    for w in load().get("watchlist", []):
        for t in w.get("concepts") or []:
            m.setdefault(t, []).append(str(w["code"]))
    return m


def compute_hot_themes(conn: sqlite3.Connection, window_days: int = 2) -> List[dict]:
    """市场级新闻关键词计数 → 热门题材（含代表票与样例标题）。

    判热条件（与 docstring 承诺一致）：近 window_days 天命中 ≥min_hits 且
    明显高于基线——≥theme_vs_avg × 此前 5 日日均（突增而非常态热度）；
    此前窗口无数据时只按 min_hits 判定。
    """
    cfg = _cfg()
    min_hits = int(cfg.get("theme_min_hits", 5))
    vs_avg = float(cfg.get("theme_vs_avg", 2.0))
    since = (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    base_since = (datetime.now() - timedelta(days=window_days + 5)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT title, content, published_at FROM news WHERE code='' "
        "AND COALESCE(published_at,'') >= ? ORDER BY published_at DESC", (since,)).fetchall()
    base_rows = conn.execute(
        "SELECT title, content FROM news WHERE code='' "
        "AND COALESCE(published_at,'') >= ? "
        "AND COALESCE(published_at,'') < ?", (base_since, since)).fetchall()
    cmap = _concept_map(conn)
    info = conn.execute("SELECT code, name FROM stock_info").fetchall()
    name_of = {c: n for c, n in info}
    out: List[dict] = []
    for theme, (keywords, concept_tag) in THEMES.items():
        hits, samples = 0, []
        for title, content, pub in rows:
            text = (title or "") + " " + (content or "")
            if any(re.search(re.escape(kw), text, re.IGNORECASE) for kw in keywords):
                hits += 1
                if len(samples) < 3:
                    samples.append((title or "")[:60])
        if hits < min_hits:
            continue
        base_hits = sum(
            1 for title, content in base_rows
            if any(re.search(re.escape(kw), (title or "") + " " + (content or ""),
                             re.IGNORECASE) for kw in keywords))
        baseline = base_hits / 5.0  # 此前 5 日日均
        surge = baseline <= 0 or hits >= vs_avg * baseline
        if not surge:
            continue
        related = [f"{c} {name_of.get(c, '')}" for c in cmap.get(concept_tag or "", [])]
        out.append({"theme": theme, "hits": hits, "hot": True,
                    "baseline": round(baseline, 2), "samples": samples,
                    "concept_tag": concept_tag, "related_stocks": related})
    out.sort(key=lambda r: -r["hits"])
    return out


def compute_hot_stocks(conn: sqlite3.Connection) -> List[dict]:
    """自选池个股新闻突增：近24小时条数 ≥min_news 且 ≥stock_vs_avg×前7日日均。

    W-B5（Sprint4，P1-12）：候选集由 stock_info 全表（all_codes，816 票）收紧为
    watchlist_core——不可交易票的新闻热度不该进"自选池热门个股"。
    """
    cfg = _cfg()
    min_news = int(cfg.get("stock_min_news", 3))
    vs_avg = float(cfg.get("stock_vs_avg", 2.0))
    now = datetime.now()
    day_start = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    week_start = (now - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
    cutoff = now.strftime("%Y-%m-%d %H:%M:%S")
    out: List[dict] = []
    try:
        from risk.blacklist import check_blacklist
        bl_ok = {c: ok for c, (ok, _) in check_blacklist(conn).items()}
    except Exception:
        bl_ok = {}
    try:
        from common.config import core_codes
        core = set(core_codes())
    except Exception:  # noqa: BLE001
        core = set(repo.all_codes(conn))  # 兜底：配置异常时退回全表（原行为）
    codes = [c for c in repo.all_codes(conn) if c in core]
    for code in codes:
        if bl_ok.get(code, True) is False:
            continue  # 黑名单票不进热门池（N/ST/次新）
        today_n = conn.execute(
            "SELECT COUNT(*) FROM news WHERE code=? AND COALESCE(published_at,'')>=? "
            "AND COALESCE(published_at,'')<=?",
            (code, day_start, cutoff)).fetchone()[0]
        if today_n < min_news:
            continue
        week_n = conn.execute(
            "SELECT COUNT(*) FROM news WHERE code=? AND COALESCE(published_at,'')>=? "
            "AND COALESCE(published_at,'')<?", (code, week_start, day_start)).fetchone()[0]
        avg = week_n / 7.0
        if avg > 0 and today_n < vs_avg * avg:
            continue
        name_row = conn.execute("SELECT name FROM stock_info WHERE code=?",
                                (code,)).fetchone()
        samples = [r[0] for r in conn.execute(
            "SELECT title FROM news WHERE code=? AND COALESCE(published_at,'')>=? "
            "AND COALESCE(published_at,'')<=? ORDER BY published_at DESC LIMIT 3",
            (code, day_start, cutoff))]
        out.append({"code": code, "name": name_row[0] if name_row else code,
                    "reason": ["近24小时新闻 %d 条（前7日日均 %.1f）" % (today_n, avg)]
                    + ["《%s》" % s[:40] for s in samples],
                    "strength": round(today_n / max(avg, 1.0), 2),
                    "today_news": today_n})
    out.sort(key=lambda r: -r["strength"])
    return out


_board_cache: dict = {"ts": 0.0, "rows": []}
_BOARD_TTL = 1800.0  # 审查 P2-3：板块榜 30 分钟缓存，避免看板每次刷新都打东财（实测单次 15s）


def board_hot(top_n: int = 8, force: bool = False) -> List[dict]:
    """东财概念板块涨幅榜 Top N（纯增强，接口不可用返回空）。

    审查 P2-3：按"尝试时间"缓存——成功与失败都计入 TTL，避免接口宕机时
    每次看板刷新都付 ~15s 的连接超时代价。
    """
    import time as _time
    if not force and (_time.monotonic() - _board_cache["ts"]) < _BOARD_TTL:
        return _board_cache["rows"]
    _board_cache["ts"] = _time.monotonic()
    # 测试逃生门：短路板块榜网络面（调用时读 env）
    import os
    if os.environ.get("AGSICKLE_DISABLE_SPOT") == "1":
        return _board_cache["rows"]
    try:
        import akshare as ak
        df = ak.stock_board_concept_name_em()
        if df is None or df.empty:
            return _board_cache["rows"]
        name_c = next((c for c in df.columns if "板块名称" in c), None)
        chg_c = next((c for c in df.columns if "涨跌幅" in c), None)
        if not name_c or not chg_c:
            return _board_cache["rows"]
        df = df.sort_values(chg_c, ascending=False).head(top_n)
        rows = [{"board": str(r[name_c]), "pct_chg": float(r[chg_c])}
                for _, r in df.iterrows()]
        _board_cache["rows"] = rows
        return rows
    except Exception as e:  # noqa: BLE001
        log.warning("概念板块榜不可用（跳过）: %s", repr(e)[:120])
        return _board_cache["rows"]


def refresh(conn: sqlite3.Connection, as_of: Optional[str] = None) -> dict:
    """刷新热门池：题材 + 个股两条线入库留痕。

    as_of 默认取 daily_bar 最新交易日（审查P2：与 movers 锚点一致，周末不产生错位脏数据）。
    """
    if as_of is None:
        day = repo.latest_trade_date(conn) or datetime.now().strftime("%Y-%m-%d")
    else:
        day = as_of
    themes = compute_hot_themes(conn)
    stocks = compute_hot_stocks(conn)
    window_days = 2
    n1 = dynpool.upsert_pool_rows(conn, "hot_theme", [
        {"code": "THEME:%s" % t["theme"], "name": t["theme"],
         "reason": ["近%d天命中 %d 次（基线 %.1f/日）" % (window_days, t["hits"],
                                                       t.get("baseline", 0.0))] + t["samples"],
         "strength": t["hits"]} for t in themes], day, mode="watchlist")
    n2 = dynpool.upsert_pool_rows(conn, "hot_stock", stocks, day, mode="watchlist")
    log.info("热门池刷新：题材 %d、个股 %d（as_of=%s）", n1, n2, day)
    boards = board_hot()
    return {"themes": themes, "stocks": stocks, "boards": boards,
            "count": n1 + n2}
