"""数据状态总览：新鲜度/数据源用量/体检摘要（看板数据状态面板）。"""
import sqlite3
from datetime import datetime, timedelta
from typing import List

from webapp.api.common import health_issues_of, q_all

_AUDIT_CACHE: dict = {"at": 0.0, "summary": None}   # 体检全表扫描有成本，进程内缓存


def api_health(conn: sqlite3.Connection, qs: dict) -> dict:
    fetch_log = q_all(conn, "SELECT code, run_at, status, rows, detail FROM fetch_log "
                            "ORDER BY run_at DESC LIMIT 20")
    codes = q_all(conn, "SELECT si.code AS code, si.name AS name, MAX(db.trade_date) AS latest_bar_date,"
                        " COUNT(db.trade_date) AS rows FROM stock_info si "
                        "LEFT JOIN daily_bar db ON db.code=si.code "
                        "GROUP BY si.code, si.name ORDER BY si.code")
    for c in codes:
        c["rows"] = int(c["rows"] or 0)
    return {"health_issues": health_issues_of(conn), "fetch_log": fetch_log, "codes": codes}


def _srv():
    from webapp import server
    return server


def api_data_status(conn: sqlite3.Connection, qs: dict) -> dict:
    """数据状态总览：各层数据新鲜度 + 数据源用量 + 体检摘要（看板「数据状态」面板）。

    口径：一切"新鲜"以 daily_bar 最新交易日为锚；失败明细截断保输出轻量。
    """
    import time as _time

    wl = _srv().load_config().get("watchlist", [])
    wl_codes = [str(w["code"]) for w in wl]
    latest = conn.execute("SELECT MAX(trade_date) FROM daily_bar").fetchone()[0]

    # 1) 自选池日线滞后票（最新 bar 落后全局最新交易日）
    lagging = []
    if wl_codes and latest:
        ph = ",".join("?" * len(wl_codes))
        name_of = {str(w["code"]): str(w.get("name") or w["code"]) for w in wl}
        for code, td in conn.execute(
                f"SELECT code, MAX(trade_date) FROM daily_bar "
                f"WHERE code IN ({ph}) GROUP BY code", wl_codes):
            if td != latest:
                lagging.append({"code": code, "name": name_of.get(code, code),
                                "latest_bar_date": td})

    # 2) 指数日线新鲜度（缺行会导致日报基准/regime 失真——自检已知坑）
    indexes = q_all(conn, "SELECT index_code, MAX(trade_date) AS latest_date "
                          "FROM index_daily GROUP BY index_code ORDER BY index_code")

    # 3) 动态池新鲜度（各池最新一次刷新日期/口径/数量）
    pools = []
    for p in ("movers", "hot_theme", "hot_stock"):
        row = conn.execute(
            "SELECT added_date, mode, COUNT(*) FROM dynamic_pool WHERE pool=? "
            "AND added_date=(SELECT MAX(added_date) FROM dynamic_pool WHERE pool=?)",
            (p, p)).fetchone()
        pools.append({"pool": p, "added_date": row[0] if row else None,
                      "mode": (row[1] or "") if row else "",
                      "count": int(row[2] or 0) if row else 0})

    # 4) 信号/决策新鲜度
    from common.config import active_profile
    sig = conn.execute("SELECT MAX(as_of), COUNT(*) FROM signal WHERE profile=?",
                       (active_profile(),)).fetchone()
    dec = conn.execute(
        "SELECT run_date, COUNT(*) FROM decision WHERE run_date="
        "(SELECT MAX(run_date) FROM decision) GROUP BY run_date").fetchone()

    # 5) 数据源用量（近 30 天 daily_bar 行数按来源——如实反映 em/腾讯兜底占比）
    since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    sources = q_all(conn, "SELECT source, COUNT(*) AS rows, MAX(trade_date) AS latest_date "
                          "FROM daily_bar WHERE trade_date>=? "
                          "GROUP BY source ORDER BY rows DESC", (since,))
    for s in sources:
        s["rows"] = int(s["rows"] or 0)

    # 6) 近期采集失败（fetch_log detail 含数据源名与原因；empty_today=当日全源
    #    失败——盯市/决策将沿用昨日，需与健康面板红警同级展示）
    fails = q_all(conn, "SELECT code, run_at, detail FROM fetch_log "
                        "WHERE status IN ('fail','empty_today') "
                        "ORDER BY run_at DESC LIMIT 8")

    # 7) 数据体检摘要（进程内缓存 5 分钟）
    now_t = _time.monotonic()
    if _AUDIT_CACHE["summary"] is None or now_t - _AUDIT_CACHE["at"] > 300:
        try:
            from collections import Counter
            from data.audit import check_db
            issues, total = check_db(conn, limit=500)
            _AUDIT_CACHE["summary"] = {
                "total": int(total or 0),
                "kinds": dict(Counter(i["kind"] for i in issues)),
                "sample": issues[:3],
                "checked_at": datetime.now().isoformat(timespec="seconds"),
            }
        except Exception as e:  # noqa: BLE001
            _AUDIT_CACHE["summary"] = {"error": "%s: %s" % (type(e).__name__, e)}
        _AUDIT_CACHE["at"] = now_t
    audit = dict(_AUDIT_CACHE["summary"] or {})

    # 8) 最新决策会话（bundle/decision 文件时效）
    session: dict = {"run_date": None, "bundle_mtime": None, "decision": False}
    try:
        SESSION_DIR = _srv().SESSION_DIR
        if SESSION_DIR.is_dir():
            for d in sorted((x for x in SESSION_DIR.iterdir() if x.is_dir()),
                            reverse=True):
                b = d / "bundle.md"
                if b.is_file():
                    session = {
                        "run_date": d.name,
                        "bundle_mtime": datetime.fromtimestamp(
                            b.stat().st_mtime).isoformat(timespec="seconds"),
                        "decision": (d / "decision.json").is_file(),
                    }
                    break
    except OSError:
        pass

    # 9) 盘中快照审计时效（logs/quotes 最新文件）
    quotes: dict = {"latest_file": None, "age_min": None}
    try:
        qdir = _srv().LOGS_DIR / "quotes"
        files = [f for f in qdir.iterdir() if f.is_file()] if qdir.is_dir() else []
        if files:
            newest = max(files, key=lambda f: f.stat().st_mtime)
            age = (datetime.now().timestamp() - newest.stat().st_mtime) / 60
            quotes = {"latest_file": newest.name, "age_min": round(age, 1)}
    except OSError:
        pass

    return {"generated_at": datetime.now().isoformat(timespec="seconds"),
            "latest_bar_date": latest,
            "watchlist_total": len(wl_codes),
            "watchlist_lagging": lagging,
            "indexes": indexes,
            "pools": pools,
            "signal": {"as_of": sig[0] if sig else None,
                       "rows": int(sig[1] or 0) if sig else 0},
            "decision": {"run_date": dec[0] if dec else None,
                         "rows_latest": int(dec[1] or 0) if dec else 0},
            "sources_30d": sources,
            "recent_fails": fails,
            "audit": audit,
            "session": session,
            "quotes_audit": quotes}
