"""风控：黑名单过滤 + 数据健康检查。"""
import json
import logging
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from common.config import snapshot  # noqa: E402
from data import repo  # noqa: E402

CFG = snapshot()

log = logging.getLogger("risk.blacklist")

# Fix-5：业绩预告负面硬拦截（此前只有 LLM 软提示，无代码级拦截）
EARNINGS_NEG_NET_LIMIT = -2
EARNINGS_LOOKBACK_DAYS = 3

# P0-5：业绩预告负面拦截只针对"买入"——持仓票出负面预告后仍需能止损卖出，
# 否则连续跌停 + 负面预告（规则21 的典型场景）会被自己的黑名单焊死出口。
# 该前缀是 check_blacklist 生成 earnings 子原因与下游 sell 豁免识别的唯一权威口径。
EARNINGS_BLOCK_PREFIX = "业绩预告负面"


def split_blacklist_reason(reason: str) -> list:
    """把 check_blacklist 聚合的 reason 字符串拆回子原因列表（'; ' 连接，'-' 视为空）。"""
    return [r for r in str(reason or "").split("; ") if r and r != "-"]


def is_earnings_only(reason: str) -> bool:
    """该拦截是否**仅**由业绩预告负面构成（无 ST/次新/上市天数等其他原因）。

    仅当全部子原因都以 EARNINGS_BLOCK_PREFIX 开头才返回 True——含任何其他
    拦截理由（如 ST）时返回 False，sell 豁免不生效，仍按原样拦截。
    """
    parts = split_blacklist_reason(reason)
    return bool(parts) and all(p.startswith(EARNINGS_BLOCK_PREFIX) for p in parts)


def _earnings_negative_net(conn: sqlite3.Connection,
                           days: int = EARNINGS_LOOKBACK_DAYS) -> dict:
    """近 N 日 news_earnings 净分 {code: net}（positive 计正、negative 计负）。

    kind 取值以实际 schema 为准（'positive'/'negative'）；表缺失/空 → {}。
    """
    try:
        rows = conn.execute(
            "SELECT code, SUM(CASE WHEN kind='positive' THEN count"
            " ELSE -count END) AS net FROM news_earnings"
            " WHERE date >= ? GROUP BY code",
            ((date.today() - timedelta(days=days)).isoformat(),)).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r[0]: int(r[1] or 0) for r in rows}


def check_blacklist(conn: sqlite3.Connection) -> dict:
    """返回 {code: (是否通过, 原因)}。规则：上市不满N日 / ST / 次新高价股 /
    业绩预告负面（net ≤ −2 硬拦截）。"""
    rules = CFG["blacklist_rules"]
    today = date.today()
    earn_net = _earnings_negative_net(conn)
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
        net = earn_net.get(code)
        if net is not None and net <= EARNINGS_NEG_NET_LIMIT:
            reasons.append(f"{EARNINGS_BLOCK_PREFIX}（net={net}）")
        result[code] = (len(reasons) == 0, "; ".join(reasons) or "-")
    return result


def health_check(conn: sqlite3.Connection) -> list:
    """数据健康检查：最新数据是否为最近一个交易日、行数是否异常。"""
    latest = repo.latest_trade_date(conn)
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
