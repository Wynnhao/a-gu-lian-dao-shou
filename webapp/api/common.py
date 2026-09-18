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

def health_issues_of(conn: sqlite3.Connection, scope: str = "watchlist") -> List[str]:
    """数据健康告警。

    scope="watchlist" 只看自选池（日常看板口径）；scope="all" 看全 universe。
    列表里同时包含"当日缺失"（今天该有却没有）和"长期滞后"（最新 bar 落后
    全局最新日 1 天以上）——后者用「(停 N 天)」后缀标注，便于 UI 折叠。
    """
    from datetime import datetime
    from risk.blacklist import health_check
    if scope == "all":
        return health_check(conn)

    wl = _watchlist_codes()
    if not wl:
        return []
    ph = ",".join("?" * len(wl))
    rows = q_all(conn, f"SELECT code, name, MAX(trade_date) AS latest "
                       f"FROM stock_info LEFT JOIN daily_bar USING(code) "
                       f"WHERE code IN ({ph}) GROUP BY code, name", wl)
    issues: List[str] = []
    global_latest = q_one(conn, "SELECT MAX(trade_date) AS d FROM daily_bar")
    global_latest = global_latest["d"] if global_latest else None
    today = datetime.now().date()
    for r in rows:
        latest = r["latest"]
        if latest is None:
            issues.append(f"{r['code']} {r['name']} 缺少全部日线")
            continue
        try:
            ld = datetime.fromisoformat(str(latest)).date()
        except ValueError:
            continue
        if global_latest and ld.isoformat() != global_latest:
            lag = (today - ld).days
            issues.append(f"{r['code']} {r['name']} 停在 {latest}（滞后 {lag} 天）")
    return issues


def blacklist_of(conn: sqlite3.Connection, scope: str = "watchlist") -> List[Dict[str, Any]]:
    """黑名单快照。

    scope="watchlist" 只看自选池（日常看板口径，PASS/BLOCK 比例直观）；
    scope="all" 看全 universe（含历史 holdings / 信号命中票）——少数场合需要。
    """
    from risk.blacklist import check_blacklist
    bl = check_blacklist(conn)
    if scope == "all":
        names = {r["code"]: r["name"] for r in
                 q_all(conn, "SELECT code, name FROM stock_info")}
    else:
        wl = set(_watchlist_codes())
        bl = {c: v for c, v in bl.items() if c in wl}
        names = {r["code"]: r["name"] for r in
                 q_all(conn, f"SELECT code, name FROM stock_info "
                             f"WHERE code IN ({','.join('?' * len(wl))})", tuple(wl))}
    out: List[Dict[str, Any]] = []
    for code, (ok, reason) in sorted(bl.items()):
        out.append({"code": code, "name": names.get(code, code),
                    "ok": bool(ok), "reason": reason or "-"})
    return out


def _watchlist_codes() -> List[str]:
    """自选池代码列表（复用 common.config.snapshot，与 risk/blacklist 一致口径）。"""
    try:
        from common.config import snapshot
        return [str(w["code"]) for w in (snapshot().get("watchlist") or [])]
    except Exception:  # noqa: BLE001
        return []


def latest_bar(conn: sqlite3.Connection, code: str) -> Optional[dict]:
    return q_one(conn, "SELECT trade_date, close FROM daily_bar WHERE code=? "
                       "ORDER BY trade_date DESC LIMIT 1", (code,))


def bench_close_at(conn: sqlite3.Connection, date_str: str) -> Optional[float]:
    """index_daily '000300' 在 date_str 当日或之前最近一个交易日的收盘。"""
    row = q_one(conn, "SELECT close FROM index_daily WHERE index_code='000300' "
                      "AND trade_date<=? ORDER BY trade_date DESC LIMIT 1", (date_str,))
    return fnum(row["close"]) if row and row["close"] is not None else None


# ---------------------------------------------------------------- API 实现
