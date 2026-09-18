"""异动池：规则量化筛近期异动票（涨幅/放量/振幅/加速/新高新低），全自动进出池。

候选集分层：
1. 全市场模式：akshare 东财全市场快照（ak.stock_zh_a_spot_em，含量比/涨跌幅），
   按量比/涨幅/成交额门槛初筛取 Top N——接口挂了自动降级；
2. 自选池模式（兜底，必有）：daily_bar 日线口径计算五类异动规则。

所有阈值来自 config.pools.movers。异动票仅"纳入评估"（watch 资格），
buy/sell 仍限自选池（config.watchlist）——晋升自选池必须人工改 config。
"""
import json
import logging
import logging.handlers
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from common.config import load  # noqa: E402
from data import repo  # noqa: E402
from signals import dynpool  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.handlers.RotatingFileHandler(BASE / "logs" / "signal.log", encoding="utf-8", maxBytes=5_000_000, backupCount=3),
              logging.StreamHandler()],
)
log = logging.getLogger("movers")


def _cfg() -> dict:
    return load().get("pools", {}).get("movers", {})


def _wl_codes() -> List[str]:
    """可交易池（watchlist_core）代码——异动池"自选池口径"的判定基准。"""
    from common.config import core_codes
    return core_codes()


def _limit_band(code: str) -> float:
    """停板幅度%（与 risk.engine/data.audit 口径一致）：创业/科创 20，北交所 30，主板 10。"""
    if code.startswith(("300", "301", "302", "688", "689")):
        return 20.0
    if code.startswith(("43", "83", "87", "88", "92")):
        return 30.0
    return 10.0


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
    latest_map = repo.latest_dates_by_code(conn)
    if not latest_map:
        return []
    as_of = as_of or max(latest_map.values())
    out: List[dict] = []
    for code, td in latest_map.items():
        if td != as_of:
            continue  # 只看最新交易日有数据的票
        hist = conn.execute(
            "SELECT trade_date, close, high, low, volume, pct_chg, close_qfq FROM daily_bar "
            "WHERE code=? ORDER BY trade_date DESC LIMIT 61", (code,)).fetchall()
        if len(hist) < 6:
            continue
        today = hist[0]
        _, close, high, low, volume, pct_chg, cq = today
        prev_close = hist[1][1]
        prev_cq = hist[1][6]
        if not close or not prev_close:
            continue
        name_row = conn.execute(
            "SELECT name FROM stock_info WHERE code=?", (code,)).fetchone()
        name = name_row[0] if name_row else code
        # 除权除息日判定：腾讯源 pct_chg 按未复权昨收自算，送转/分红日呈假暴跌/假新低。
        # 原始涨跌幅与前复权涨跌幅显著背离、且复权后在停板内 → 除权日，跨除权日的
        # ①涨幅/③振幅/④动量/⑤新高低全部失真，当日跳过（②放量与价格复权无关，保留）。
        # qfq 缺失（未回补）时按非除权日处理，行为与旧版一致。
        exdiv = False
        if cq and prev_cq and pct_chg is not None:
            qfq_pct = (cq / prev_cq - 1) * 100
            exdiv = (abs(pct_chg) - abs(qfq_pct) > 1.0
                     and abs(qfq_pct) <= _limit_band(code))
        reasons: List[str] = []
        strength = 0.0
        # ① 涨幅异动
        if pct_chg is not None and not exdiv and abs(pct_chg) >= c.get("pct_chg", 5.0):
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
        if not exdiv and high and low and prev_close and (high - low) / prev_close * 100 >= c.get("amplitude", 7.0):
            reasons.append("振幅%.1f%%" % ((high - low) / prev_close * 100))
            strength += 1.0
        # ④ 5日加速
        if not exdiv:
            m5 = sum(h[5] or 0 for h in hist[:5])
            if m5 >= c.get("mom5_up", 12.0):
                reasons.append("5日+%+.1f%%" % m5)
                strength += min(m5 / 20.0, 1.5)
            elif m5 <= c.get("mom5_down", -10.0):
                reasons.append("5日%+.1f%%（急跌）" % m5)
                strength += min(abs(m5) / 20.0, 1.5)
        # ⑤ 60日新高/新低（不含今日）
        if not exdiv:
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
    """全市场快照：东财主源 → 腾讯兜底。失败返回 None（调用方降级自选池模式）。

    P1 修复 1:加指数退避 30s/60s/120s 三次重试。
    P1 修复 2:em 持续 RemoteDisconnected 时，腾讯 stock_zh_a_spot_tx 可兜底返回
    约 5500+ 只候选（字段为英文 code/name/zdf/zf/zxj/turnover；列名与 em 不一致，
    调用方 compute_market_movers 需做兼容）。腾讯快照也是"上一交易日盘后"口径。
    """
    # 测试逃生门：短路全市场快照网络面（调用时读 env），调用方降级自选池口径
    import os
    if os.environ.get("AGSICKLE_DISABLE_SPOT") == "1":
        log.info("AGSICKLE_DISABLE_SPOT=1，跳过全市场快照")
        return None
    import time
    import akshare as ak

    def _norm_tx(rows: List[dict]) -> List[dict]:
        """腾讯 spot (英文字段) → em 快照 (中文字段) 列名映射，喂给 compute_market_movers。

        单位换算：腾讯 turnover 为「万元」，em 成交额为「元」（amount_floor=5e7 按
        元口径），不换算会把 17.5亿 误当 17.5万 全部筛掉（2026-09-15 实测入池 0 只）。
        """
        out = []
        for r in rows:
            code = str(r.get("code") or "")
            if code.startswith(("sh", "sz", "bj")):
                code = code[2:]
            turnover = _to_float(r.get("turnover"))
            out.append({
                "代码": code,
                "名称": r.get("name") or "",
                "最新价": _to_float(r.get("zxj")),
                "涨跌幅": _to_float(r.get("zdf")),
                "量比": _to_float(r.get("lb")),
                "成交额": turnover * 10000.0 if turnover is not None else None,
                "振幅": _to_float(r.get("zf")),
            })
        return out

    def _to_float(v) -> Optional[float]:
        try:
            return float(v) if v not in (None, "", "-") else None
        except (TypeError, ValueError):
            return None

    # ---- 主源：东财 stock_zh_a_spot_em（中文列名）----
    delays = [0, 30, 60, 120]
    em_need = {"代码", "名称", "最新价", "涨跌幅", "量比", "成交额"}
    last_err = None
    for i, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            df = ak.stock_zh_a_spot_em()
            if df is None or df.empty or not em_need.issubset(set(df.columns)):
                last_err = ValueError("em empty or missing columns")
                log.warning("全市场快照(em)第 %d 次返回空/缺列", i + 1)
                continue
            if i > 0:
                log.info("全市场快照(em)第 %d 次重试成功", i + 1)
            return df.to_dict("records")
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("全市场快照(em)第 %d 次失败: %s", i + 1, repr(e)[:120])
            continue

    # ---- 兜底：腾讯 stock_zh_a_spot_tx（英文字段，~5500 行，约 8s）----
    log.warning("全市场快照(em)全失败（%d 次），降级到腾讯 spot_tx: %s",
                len(delays), repr(last_err)[:120])
    try:
        df = ak.stock_zh_a_spot_tx()
        if df is None or df.empty:
            log.warning("全市场快照(tx) 返回空，降级自选池口径")
            return None
        rows = _norm_tx(df.to_dict("records"))
        log.info("全市场快照(tx) 兜底成功 %d 行", len(rows))
        return rows
    except Exception as e:  # noqa: BLE001
        log.warning("全市场快照(tx) 也失败: %s", repr(e)[:120])
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
        as_of = repo.latest_trade_date(conn) or datetime.now().strftime("%Y-%m-%d")
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
        top_n = int(_cfg().get("top_n", 20))
        if len(rows) > top_n:
            rows = rows[:top_n]  # 兜底口径同样受池子容量约束，与全市场口径一致
        mode = "watchlist"
    n = dynpool.upsert_pool_rows(conn, "movers", rows, day, mode=mode)
    log.info("异动池刷新 mode=%s 入池 %d 只（as_of=%s）", mode, n, day)
    return {"mode": mode, "count": n, "rows": rows}
