"""webapp/api 拆分模块（结构性重构 Phase 6，纯结构搬移——不改 SQL 语义与响应 JSON 形状）。"""
"""跨域共享查询/格式化 helper（无 server 模块依赖，可独立测试）。"""
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


def q_all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> List[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def q_one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Optional[dict]:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def parse_json_field(raw: Any, fallback: Any) -> Any:
    """表里的 JSON 串 → 对象；空/坏值回退。"""
    if raw is None or str(raw).strip() == "":
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def safe_join(base: Path, rel: str) -> Optional[Path]:
    """路径白名单：resolve 后必须仍在 base 之内，否则 None（防目录穿越）。"""
    try:
        base_real = os.path.realpath(str(base))
        target = os.path.realpath(os.path.join(base_real, rel))
        if os.path.commonpath([base_real, target]) != base_real:
            return None
        return Path(target)
    except (ValueError, OSError):
        return None


def clamp_int(val: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(val)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def fnum(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 风控快照（复用 risk/blacklist.py，单一事实源——此前双维护存在口径漂移风险）

def health_issues_of(conn: sqlite3.Connection) -> List[str]:
    from risk.blacklist import health_check
    return health_check(conn)


def blacklist_of(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    from risk.blacklist import check_blacklist
    names = {r["code"]: r["name"] for r in
             q_all(conn, "SELECT code, name FROM stock_info")}
    out: List[Dict[str, Any]] = []
    for code, (ok, reason) in sorted(check_blacklist(conn).items()):
        out.append({"code": code, "name": names.get(code, code),
                    "ok": bool(ok), "reason": reason or "-"})
    return out


def latest_bar(conn: sqlite3.Connection, code: str) -> Optional[dict]:
    return q_one(conn, "SELECT trade_date, close FROM daily_bar WHERE code=? "
                       "ORDER BY trade_date DESC LIMIT 1", (code,))


def bench_close_at(conn: sqlite3.Connection, date_str: str) -> Optional[float]:
    """index_daily '000300' 在 date_str 当日或之前最近一个交易日的收盘。"""
    row = q_one(conn, "SELECT close FROM index_daily WHERE index_code='000300' "
                      "AND trade_date<=? ORDER BY trade_date DESC LIMIT 1", (date_str,))
    return fnum(row["close"]) if row and row["close"] is not None else None


# ---------------------------------------------------------------- API 实现
