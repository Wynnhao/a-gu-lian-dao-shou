"""webapp/api 拆分模块（结构性重构 Phase 6，纯结构搬移——不改 SQL 语义与响应 JSON 形状）。"""
"""决策/成交/风控事件/资讯流水接口。"""
import sqlite3
from typing import Dict, List

from webapp.api.common import clamp_int, fnum, parse_json_field, q_all


def api_decisions(conn: sqlite3.Connection, qs: dict) -> List[dict]:
    limit = clamp_int((qs.get("limit") or ["50"])[0], 50, 1, 500)
    rows = q_all(conn, "SELECT * FROM decision ORDER BY id DESC LIMIT ?", (limit,))
    names = {r["code"]: r["name"] for r in q_all(conn, "SELECT code, name FROM stock_info")}
    out: List[dict] = []
    for r in rows:
        out.append({
            "id": int(r["id"]), "run_date": r["run_date"], "code": r["code"],
            "name": names.get(r["code"], r["code"]), "action": r["action"],
            "target_weight": fnum(r["target_weight"]), "confidence": fnum(r["confidence"]),
            "status": r["status"],
            "reasons": parse_json_field(r["reasons"], []),
            "risk_notes": parse_json_field(r["risk_notes"], []),
            "created_at": r["created_at"],
        })
    return out


def api_trades(conn: sqlite3.Connection, qs: dict) -> List[dict]:
    limit = clamp_int((qs.get("limit") or ["50"])[0], 50, 1, 500)
    rows = q_all(conn, "SELECT * FROM trade ORDER BY id DESC LIMIT ?", (limit,))
    out: List[dict] = []
    for r in rows:
        d = dict(r)
        for k in ("shares",):
            d[k] = int(d[k]) if d.get(k) is not None else None
        out.append(d)
    return out


def api_risk_events(conn: sqlite3.Connection, qs: dict) -> List[dict]:
    limit = clamp_int((qs.get("limit") or ["50"])[0], 50, 1, 500)
    rows = q_all(conn, "SELECT * FROM risk_event ORDER BY id DESC LIMIT ?", (limit,))
    out: List[dict] = []
    for r in rows:
        d = dict(r)
        d["id"] = int(d["id"])
        d["decision_id"] = int(d["decision_id"]) if d.get("decision_id") is not None else None
        out.append(d)
    return out


def api_news(conn: sqlite3.Connection, qs: dict) -> dict:
    limit = clamp_int((qs.get("limit") or ["30"])[0], 30, 1, 200)
    rows = q_all(conn, "SELECT code, title, content, source, url, published_at FROM news "
                       "ORDER BY published_at DESC, id DESC LIMIT ?", (limit,))

    def slim(r: dict) -> dict:
        content = str(r.get("content") or "")
        return {"title": r.get("title") or "", "source": r.get("source") or "",
                "published_at": r.get("published_at") or "", "url": r.get("url") or "",
                "content": content[:100]}

    market: List[dict] = []
    by_code: Dict[str, List[dict]] = {}
    for r in rows:
        code = str(r.get("code") or "")
        if code == "":
            market.append(slim(r))
        else:
            by_code.setdefault(code, []).append(slim(r))
    return {"market": market, "by_code": by_code}

