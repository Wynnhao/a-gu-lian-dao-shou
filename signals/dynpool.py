"""动态池（异动池/热门池）读写：按刷新日期留痕，当前成员=各票最新入池行。

current 成员口径：每个 (pool, code) 取 MAX(added_date) 的那一行，且该日期
等于全池最新刷新日期——即"最近一次刷新时仍在池内"。历史进池记录永久保留供复盘。
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

BASE = Path(__file__).resolve().parent.parent

POOLS = ("movers", "hot_theme", "hot_stock")


def upsert_pool_rows(conn: sqlite3.Connection, pool: str,
                     rows: List[dict], as_of: str) -> int:
    """写入一次刷新结果（INSERT OR REPLACE，按 pool+code+added_date 幂等）。

    rows: [{code, name, reason, strength}, ...]
    """
    now = datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        "INSERT OR REPLACE INTO dynamic_pool VALUES (?,?,?,?,?,?,?)",
        [(pool, str(r["code"]), str(r.get("name") or ""),
          json.dumps(r.get("reason") or [], ensure_ascii=False) if not isinstance(
              r.get("reason"), str) else r["reason"],
          float(r.get("strength") or 0.0), as_of, now) for r in rows])
    conn.commit()
    return len(rows)


def current_pool(conn: sqlite3.Connection, pool: str,
                 as_of: Optional[str] = None) -> List[dict]:
    """当前池内成员（含 reason JSON 数组、strength），按 strength 降序。"""
    if as_of is None:
        row = conn.execute(
            "SELECT MAX(added_date) FROM dynamic_pool WHERE pool=?", (pool,)).fetchone()
        as_of = row[0] if row and row[0] else None
    if not as_of:
        return []
    rows = conn.execute(
        "SELECT code, name, reason, strength, added_date FROM dynamic_pool "
        "WHERE pool=? AND added_date=?", (pool, as_of)).fetchall()
    out = []
    for code, name, reason, strength, added in rows:
        try:
            reasons = json.loads(reason) if reason else []
        except (TypeError, ValueError):
            reasons = [reason] if reason else []
        out.append({"code": code, "name": name, "reasons": reasons,
                    "strength": strength, "added_date": added})
    return sorted(out, key=lambda r: -float(r.get("strength") or 0.0))


def pool_dates(conn: sqlite3.Connection, pool: str) -> List[str]:
    """该池所有刷新日期（新→旧），供看板回看。"""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT added_date FROM dynamic_pool WHERE pool=? "
        "ORDER BY added_date DESC", (pool,)).fetchall()]
