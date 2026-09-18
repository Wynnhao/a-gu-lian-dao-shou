"""Sprint 2 任务 2（P1-4）：业绩预告关键词事件通道。

数据源：news 表（已接 ak.stock_news_em + ak.stock_notice_report，1358 行 + 公告源已就位）。
逻辑：扫描近 N 天个股新闻标题+内容，正/负面关键词命中数相减得 net_score；
     samples 保留前 3 条标题供 LLM 引用。
"""
import json
import logging
import logging.handlers
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

log = logging.getLogger("signals.earnings")
if not log.handlers:
    from common.logsetup import rotating_handler  # AGSICKLE_LOG_DIR 逃生门（Sprint4 W0-1）
    log.addHandler(rotating_handler("signal.log"))
    log.addHandler(logging.StreamHandler())
    log.setLevel(logging.INFO)
log.propagate = False

# 关键词表（审查报告 P1-4 内置；人工维护，扩数据时不破格式）
# 注意子串碰撞：正面词不得是负面词的前缀（"净利润同比"曾命中"净利润同比下滑"造成假正面）
EARNINGS_POS = ["预增", "扭亏", "超预期", "上修", "业绩快报良好",
                "净利润同比增长", "净利润同比预增", "同比增长", "业绩亮眼", "高增长", "业绩预盈"]
EARNINGS_NEG = ["预减", "首亏", "续亏", "商誉减值", "下修", "业绩变脸",
                "不及预期", "同比下滑", "净利润同比下滑", "净利润同比下降",
                "亏损扩大", "业绩预亏"]

_POS_RE = [re.compile(re.escape(k)) for k in EARNINGS_POS]
_NEG_RE = [re.compile(re.escape(k)) for k in EARNINGS_NEG]


def _scan_text(text: str) -> tuple:
    """单文本扫描 → (positive_hits, negative_hits, matched_samples_pos, matched_samples_neg)."""
    if not text:
        return 0, 0, [], []
    pos = sum(1 for r in _POS_RE if r.search(text))
    neg = sum(1 for r in _NEG_RE if r.search(text))
    return pos, neg, [], []


def compute_earnings_events(conn: sqlite3.Connection, days: int = 3,
                             min_net_score: int = 2) -> dict:
    """对近 N 天 news 表按 code 扫描，写入 news_earnings 表。

    返回 {code: {"positive": int, "negative": int, "net": int,
                   "samples_pos": [...], "samples_neg": [...]}}
    min_net_score 仅用于过滤低噪音写入（实际返回里保留所有命中数据）。
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT code, title, content, published_at FROM news"
        " WHERE published_at >= ? AND code != ''"
        " ORDER BY published_at DESC", (cutoff,)).fetchall()
    out: dict = {}
    for code, title, content, pub in rows:
        text = (title or "") + " " + (content or "")
        pos, neg, _, _ = _scan_text(text)
        if pos == 0 and neg == 0:
            continue
        if code not in out:
            out[code] = {"positive": 0, "negative": 0, "net": 0,
                         "samples_pos": [], "samples_neg": []}
        out[code]["positive"] += pos
        out[code]["negative"] += neg
        if pos > 0 and len(out[code]["samples_pos"]) < 3:
            out[code]["samples_pos"].append((title or "")[:60])
        if neg > 0 and len(out[code]["samples_neg"]) < 3:
            out[code]["samples_neg"].append((title or "")[:60])
    for c in out:
        out[c]["net"] = out[c]["positive"] - out[c]["negative"]
    # 写入 news_earnings 表（同日覆盖式）
    today = datetime.now().strftime("%Y-%m-%d")
    n = 0
    for code, d in out.items():
        if abs(d["net"]) < min_net_score:
            continue
        for kind in ("positive", "negative"):
            if d[kind] > 0:
                conn.execute(
                    "INSERT OR REPLACE INTO news_earnings"
                    " (code, date, kind, count, samples) VALUES (?,?,?,?,?)",
                    (code, today, kind, d[kind],
                     json.dumps(d.get("samples_" + kind, []), ensure_ascii=False)))
                n += 1
    conn.commit()
    log.info("compute_earnings_events: %d 行（days=%d, min_net=%d）",
             n, days, min_net_score)
    return out


def refresh(conn: sqlite3.Connection = None,
            days: int = 3, min_net_score: int = 2) -> dict:
    """premarket 步骤 6.6 入口：跑 compute_earnings_events 并返回结果。

    conn=None 时用 data.fetcher.get_conn()（生产）。
    """
    own = conn is None
    c = conn
    if own:
        from data.fetcher import get_conn
        c = get_conn()
    try:
        return compute_earnings_events(c, days=days, min_net_score=min_net_score)
    finally:
        if own:
            c.close()


if __name__ == "__main__":
    print("== Sprint 2 任务 2：业绩预告关键词事件 ==")
    print(json.dumps(refresh(), ensure_ascii=False, indent=2))
