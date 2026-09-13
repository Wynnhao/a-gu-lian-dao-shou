"""异动池：规则量化筛近期异动票（涨幅/放量/振幅/加速/新高新低），全自动进出池。

候选集分层：
1. 全市场模式：akshare 东财全市场快照（ak.stock_zh_a_spot_em，含量比/涨跌幅），
   按量比/涨幅/成交额门槛初筛取 Top N——接口挂了自动降级；
2. 自选池模式（兜底，必有）：daily_bar 日线口径计算五类异动规则。

所有阈值来自 config.pools.movers。异动票仅"纳入评估"（watch 资格），
buy/sell 仍限自选池 30 只——晋升自选池必须人工改 config。
"""
import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from signals import dynpool  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(BASE / "logs" / "signal.log", encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("movers")


def _cfg() -> dict:
    return json.loads((BASE / "config.json").read_text(encoding="utf-8")) \
        .get("pools", {}).get("movers", {})


def _wl_codes() -> List[str]:
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    return [str(w["code"]) for w in cfg.get("watchlist", [])]


# ---------------------------------------------------------------- 自选池模式

def compute_watchlist_movers(conn: sqlite3.Connection,
                             as_of: Optional[str] = None) -> List[dict]:
    """日线口径五类异动规则，作用于自选池（含黑名单票——展示但标注 blacklist）。"""
    c = _cfg()
    wl = set(_wl_codes())
    try:
        from risk.blacklist import check_blacklist
        bl = {code: ok for code, (ok, _) in check_blacklist(conn).items()}
    except Exception:  # noqa: BLE001
        bl = {}
    rows = conn.execute(
        "SELECT code, MAX(trade_date) FROM daily_bar GROUP BY code").fetchall()
    if not rows:
        return []
    latest_map = {code: td for code, td in rows}
    as_of = as_of or max(latest_map.values())
    out: List[dict] = []
    for code, td in latest_map.items():
        if td != as_of:
            continue  # 只看最新交易日有数据的票
        hist = conn.execute(
            "SELECT trade_date, close, high, low, volume, pct_chg FROM daily_bar "
            "WHERE code=? ORDER BY trade_date DESC LIMIT 61", (code,)).fetchall()
        if len(hist) < 6:
            continue
        today = hist[0]
        _, close, high, low, volume, pct_chg = today
        prev_close = hist[1][1]
        if not close or not prev_close:
            continue
        name_row = conn.execute(
            "SELECT name FROM stock_info WHERE code=?", (code,)).fetchone()
        name = name_row[0] if name_row else code
        reasons: List[str] = []
        strength = 0.0
        # ① 涨幅异动
        if pct_chg is not None and abs(pct_chg) >= c.get("pct_chg", 5.0):
            reasons.append("涨幅%+.2f%%" % pct_chg)
            strength += min(abs(pct_chg) / 10.0, 2.0)
        # ② 放量（相对前20日均量）
        vols = [h[4] or 0 for h in hist[1:21]]
        if vols:
            avg_v = sum(vols) / len(vols)
            if avg_v > 0 and volume and volume / avg_v >= c.get("volume_ratio", 2.5):
                reasons.append("放量%.1f倍" % (volume / avg_v))
                strength += min(volume / avg_v / 5.0, 1.5)
        # ③ 振幅
        if high and low and prev_close and (high - low) / prev_close * 100 >= c.get("amplitude", 7.0):
            reasons.append("振幅%.1f%%" % ((high - low) / prev_close * 100))
            strength += 1.0
        # ④ 5日加速
        m5 = sum(h[5] or 0 for h in hist[:5])
        if m5 >= c.get("mom5_up", 12.0):
            reasons.append("5日+%+.1f%%" % m5)
            strength += min(m5 / 20.0, 1.5)
        elif m5 <= c.get("mom5_down", -10.0):
            reasons.append("5日%+.1f%%（急跌）" % m5)
            strength += min(abs(m5) / 20.0, 1.5)
        # ⑤ 60日新高/新低（不含今日）
        past_high = [h[2] for h in hist[1:60] if h[2]]
        past_low = [h[3] for h in hist[1:60] if h[3]]
        if past_high and close >= max(past_high):
            reasons.append("创60日新高")
            strength += 1.2
        elif past_low and close <= min(past_low):
            reasons.append("创60日新低")
            strength += 1.2
        if reasons:
            out.append({"code": code, "name": name,
                        "reason": reasons, "strength": round(strength, 2),
                        "close": close, "pct_chg": pct_chg,
                        "in_watchlist": code in wl,
                        "blacklist": bl.get(code, False) is True,
                        "mode": "watchlist"})
    out.sort(key=lambda r: -r["strength"])
    return out


# ---------------------------------------------------------------- 全市场模式

def fetch_all_market_spot() -> Optional[List[dict]]:
    """东财全市场快照（一次拉全部A股）。失败返回 None（调用方降级自选池模式）。"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        need = {"代码", "名称", "最新价", "涨跌幅", "量比", "成交额"}
        if df is None or df.empty or not need.issubset(set(df.columns)):
            return None
        return df.to_dict("records")
    except Exception as e:  # noqa: BLE001
        log.warning("全市场快照不可用（降级自选池口径）: %s", repr(e)[:120])
        return None


def compute_market_movers(spot: List[dict], top_n: int = 20,
                          conn: Optional[sqlite3.Connection] = None) -> List[dict]:
    """全市场异动初筛：量比/涨幅/成交额门槛，剔除 ST/北交所/退市整理/真次新。

    N/C 前缀结合 first_trade_date 判断：仅上市未满 10 日才剔除，避免误杀
    C 开头的长期正常票（原 startswith 过滤会把"C+"公司全杀掉）。
    conn 缺省时无法查上市日，N/C 前缀保守剔除（与旧行为一致）。
    """
    c = _cfg()
    amount_floor = float(c.get("amount_floor", 5e7))
    out: List[dict] = []
    from datetime import date as _date
    for r in spot:
        code = str(r.get("代码") or "")
        name = str(r.get("名称") or "")
        if not code or code.startswith(("8", "4", "9")) or "ST" in name.upper() \
                or "退" in name:
            continue
        if name.startswith(("N", "C")):
            if conn is None:
                continue
            row = conn.execute(
                "SELECT first_trade_date FROM stock_info WHERE code=?", (code,)).fetchone()
            if not row or not row[0]:
                continue
            try:
                listed = _date.fromisoformat(str(row[0])[:10])
                if (_date.today() - listed).days < 10:
                    continue
            except ValueError:
                continue
        price, pct, vr, amount = r.get("最新价"), r.get("涨跌幅"), r.get("量比"), r.get("成交额")
        if price is None or pct is None:
            continue
        reasons: List[str] = []
        strength = 0.0
        if pct is not None and abs(pct) >= c.get("pct_chg", 5.0):
            reasons.append("涨幅%+.2f%%" % pct)
            strength += min(abs(pct) / 10.0, 2.0)
        if vr is not None and vr >= c.get("volume_ratio", 2.5):
            reasons.append("量比%.1f" % vr)
            strength += min(vr / 5.0, 1.5)
        if amount is not None and amount < amount_floor:
            continue  # 流动性地板
        if reasons:
            out.append({"code": code, "name": name, "reason": reasons,
                        "strength": round(strength, 2), "close": price,
                        "pct_chg": pct, "in_watchlist": False, "mode": "all"})
    out.sort(key=lambda r: -r["strength"])
    return out[:top_n]


# ---------------------------------------------------------------- 刷新入口

def refresh(conn: sqlite3.Connection, as_of: Optional[str] = None,
            market_mode: bool = True) -> dict:
    """刷新异动池：优先全市场快照，失败降级自选池口径；结果留痕 dynamic_pool。

    as_of 默认取 daily_bar 最新交易日（周末/节假日刷新不会算空）。
    market_mode=False（盘前 9:00 场景）：跳过全市场快照——盘前快照是昨日收盘
    口径却带今日盘前量比，写成"昨日 added_date"会与昨日收盘口径行混存（口径污染），
    盘前只用日线口径算自选池五规则。

    合并口径修复：自选池命中票的 strength 一并替换为五规则口径（原实现只换
    reason 不换 strength，两口径强度上限 3.5 vs ~7.2 不可比）。
    """
    if as_of is None:
        row = conn.execute("SELECT MAX(trade_date) FROM daily_bar").fetchone()
        as_of = row[0] if row and row[0] else datetime.now().strftime("%Y-%m-%d")
    day = as_of
    spot = fetch_all_market_spot() if market_mode else None
    if spot is not None:
        rows = compute_market_movers(spot, top_n=int(_cfg().get("top_n", 20)),
                                     conn=conn)
        mode = "all"
        # 自选池内命中的票也用日线口径补算一遍（规则更全）
        wl_hits = {r["code"]: r for r in compute_watchlist_movers(conn, as_of=day)}
        for r in rows:
            if r["code"] in wl_hits:
                r["reason"] = wl_hits[r["code"]]["reason"]
                r["strength"] = wl_hits[r["code"]]["strength"]  # 两口径强度才可比
                r["in_watchlist"] = True
    else:
        rows = compute_watchlist_movers(conn, as_of=day)
        mode = "watchlist"
    n = dynpool.upsert_pool_rows(conn, "movers", rows, day, mode=mode)
    log.info("异动池刷新 mode=%s 入池 %d 只（as_of=%s）", mode, n, day)
    return {"mode": mode, "count": n, "rows": rows}
