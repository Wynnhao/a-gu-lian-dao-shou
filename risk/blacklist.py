"""风控：黑名单过滤 + 数据健康检查。"""
import json
import logging
import sqlite3
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))

log = logging.getLogger("risk.blacklist")


def check_blacklist(conn: sqlite3.Connection) -> dict:
    """返回 {code: (是否通过, 原因)}。规则：上市不满N日 / ST / 次新高价股。"""
    rules = CFG["blacklist_rules"]
    today = date.today()
    result = {}
    rows = conn.execute("SELECT code, name, first_trade_date FROM stock_info").fetchall()
    for code, name, first in rows:
        reasons = []
        if first:
            # 脏日期兜底：此前 fromisoformat 直接崩，且 confirm（人工闸门）与
            # signals 计算链路调用本函数无 try/except，一条脏数据即全局硬崩
            try:
                days = (today - date.fromisoformat(str(first).strip())).days
            except ValueError:
                log.warning("stock_info %s first_trade_date=%r 非法，跳过上市天数判断",
                            code, first)
                days = None
            if days is not None and days < rules["min_listed_days"]:
                reasons.append(f"上市仅{days}天 < {rules['min_listed_days']}天")
        if rules["exclude_st"] and ("ST" in name or "st" in name):
            reasons.append("ST标的")
        if rules["exclude_new_high_price_stocks"] and name.startswith("N"):
            reasons.append("次新股(首日/无涨跌幅限制)")
        result[code] = (len(reasons) == 0, "; ".join(reasons) or "-")
    return result


def health_check(conn: sqlite3.Connection) -> list:
    """数据健康检查：最新数据是否为最近一个交易日、行数是否异常。"""
    latest = conn.execute(
        "SELECT MAX(trade_date) FROM daily_bar"
    ).fetchone()[0]
    issues = []
    if not latest:
        return ["daily_bar 为空"]
    # 周末容差：最近3个自然日内应有数据
    from datetime import datetime, timedelta
    lag = (datetime.today() - datetime.fromisoformat(latest)).days
    if lag > 3:
        issues.append(f"数据滞后 {lag} 天（最新 {latest}）")
    for code, name, *_ in conn.execute(
            "SELECT code,name FROM stock_info WHERE code NOT IN "
            "(SELECT code FROM daily_bar WHERE trade_date=?)", (latest,)):
        issues.append(f"{code} {name} 缺少 {latest} 的数据")
    return issues


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(BASE / "data"))
    from fetcher import get_conn
    conn = get_conn()
    print("== 黑名单检查 ==")
    for code, (ok, why) in check_blacklist(conn).items():
        print(f"  {code}: {'PASS' if ok else 'BLOCK'} ({why})")
    print("== 数据健康 ==")
    for i in health_check(conn) or ["OK"]:
        print(f"  {i}")
