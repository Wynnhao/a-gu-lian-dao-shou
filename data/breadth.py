"""Sprint 2 任务 3（P1-1）：市场宽度/情绪模块数据采集。

数据源三档兜底：
  1. em  (ak.stock_zt_pool_em / ak.stock_zh_a_gdhs)        ← 主源
  2. tx  (腾讯批量报价 ask1_price==limit_up_price 扫描)  ← 兜底
  3. sina (finance.sina.com.cn 网页爬虫)                  ← 最后兜底（需 BeautifulSoup）

写表：breadth_daily(date PRIMARY KEY, limit_up_count, limit_up_seal_rate,
       limit_down_count, advance_decline_ratio, new_high_minus_new_low,
       breadth_composite, source)
"""
import json
import logging
import logging.handlers
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from data.fetcher import call_ak  # noqa: E402  (Sprint 2 任务 3：em 源走 call_ak 熔断)

log = logging.getLogger("breadth")
if not log.handlers:
    log.addHandler(logging.handlers.RotatingFileHandler(
        BASE / "logs" / "breadth.log", encoding="utf-8",
        maxBytes=5_000_000, backupCount=3))
    log.addHandler(logging.StreamHandler())
    log.setLevel(logging.INFO)
log.propagate = False


# ---------------------------------------------------------------- 数据源

def _fetch_em_breadth(date_str: str) -> dict:
    """em 源：拉涨停股池+跌停股池+涨跌平家数。

    akshare 接口：
      - stock_zt_pool_em(date='YYYYMMDD')  → 涨停明细（含炸板）
      - stock_zt_pool_dtgc_em / stock_zt_pool_zbgc_em  → 炸板/跌停
      - stock_zh_a_gdhs(symbol='szsh', date=...)  → 涨跌家数

    返回 dict 含 limit_up_count / limit_up_seal_rate / limit_down_count /
    advance_decline_ratio / new_high_minus_new_low / source='em'。
    任意失败抛错由调用方走兜底链。
    """
    from data.fetcher import call_ak
    import akshare as ak
    out = {"source": "em"}
    # 涨停明细
    df_zt = call_ak("zt_pool_em", ak.stock_zt_pool_em, date=date_str)
    if df_zt is not None and not df_zt.empty:
        out["limit_up_count"] = int(len(df_zt))
    else:
        out["limit_up_count"] = 0
    # 封板率（Fix-1）：涨停数 / (涨停数 + 炸板数)，炸板池 stock_zt_pool_zbgc_em；
    # 炸板池失败 → None（不硬编码 1.0）
    out["limit_up_seal_rate"] = None
    try:
        df_zb = call_ak("zbgc_em", ak.stock_zt_pool_zbgc_em, date=date_str)
        zb = int(len(df_zb)) if df_zb is not None else 0
        denom = out["limit_up_count"] + zb
        if denom > 0:
            out["limit_up_seal_rate"] = round(out["limit_up_count"] / denom, 4)
    except Exception as e:
        log.warning("zbgc_em FAIL: %s", repr(e)[:140])
    # 跌停股池
    try:
        df_dt = call_ak("dt_pool_em", ak.stock_zt_pool_dtgc_em, date=date_str)
        out["limit_down_count"] = int(len(df_dt)) if df_dt is not None else 0
    except Exception as e:
        log.warning("dt_pool_em FAIL: %s", repr(e)[:140])
        out["limit_down_count"] = 0
    # 涨跌平家数
    try:
        df_gd = call_ak("gdhs_em", ak.stock_zh_a_gdhs)
        if df_gd is not None and not df_gd.empty:
            # 列名适配：up_count / down_count / flat_count
            up_col = next((c for c in df_gd.columns if "上涨" in c), None)
            dn_col = next((c for c in df_gd.columns if "下跌" in c), None)
            if up_col and dn_col:
                up = float(df_gd[up_col].iloc[0])
                dn = float(df_gd[dn_col].iloc[0])
                out["advance_decline_ratio"] = round(up / dn, 4) if dn > 0 else None
            else:
                out["advance_decline_ratio"] = None
        else:
            out["advance_decline_ratio"] = None
    except Exception as e:
        log.warning("gdhs_em FAIL: %s", repr(e)[:140])
        out["advance_decline_ratio"] = None
    # 新高新低（em 端点可能缺失，设为 None 占位）
    out["new_high_minus_new_low"] = None
    return out


def _fetch_tx_breadth(date_str: str, codes: list = None) -> dict:
    """tx 源：遍历 codes 列表，按 ask1_price==limit_up_price 计数。

    codes 缺省从 config.json watchlist 取前 86 只 + universe800 部分（共 ~200 只）。
    注：tx 源不能拿全市场 5000+ 只，因此只是近似；实际生产建议先 em 拿全的。
    """
    from data.fetcher import get_conn
    from data.quotes import get_live_prices
    from common.market import limit_pct, limit_price
    out = {"source": "tx"}
    codes = codes or []
    if not codes:
        try:
            conn = get_conn()
            rows = conn.execute(
                "SELECT code FROM watchlist").fetchall() if False else \
                conn.execute("SELECT code FROM daily_bar GROUP BY code").fetchall()
            codes = [r[0] for r in rows][:200]
            conn.close()
        except Exception:
            codes = []
    if not codes:
        # 无可用代码清单 → 视为失败（让兜底链继续走 sina / 全失败路径），
        # 绝不落一行全 0 的假数据
        raise RuntimeError("tx breadth: 无可用代码清单")
    quotes = get_live_prices(codes, force=True)
    if not quotes:
        # 行情全挂（网络/闭市）→ 视为失败，同样不落 0 值行
        raise RuntimeError("tx breadth: 实时行情不可用")
    zt = dt = 0
    for code, q in quotes.items():
        prev = q.get("prev_close") or 0
        pct = limit_pct(code)
        if not prev or pct <= 0:
            continue
        up = limit_price(float(prev), pct, up=True)
        dn = limit_price(float(prev), pct, up=False)
        if q.get("ask1_price") == up:
            zt += 1
        elif q.get("ask1_price") == dn:
            dt += 1
    out["limit_up_count"] = zt
    out["limit_down_count"] = dt
    out["limit_up_seal_rate"] = None  # tx 源无炸板数据，不硬编码（Fix-1）
    out["advance_decline_ratio"] = None
    out["new_high_minus_new_low"] = None
    return out


def _fetch_sina_breadth(date_str: str) -> dict:
    """sina 兜底：拉 finance.sina.com.cn 涨跌停家数页面。

    用 BeautifulSoup 解析 HTML 中的统计数字。失败抛错。
    """
    out = {"source": "sina"}
    from bs4 import BeautifulSoup
    url = "https://finance.sina.com.cn/stock/go.php/pFLKZSData/index/p/1"
    try:
        resp = requests.get(url, timeout=5)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text()
        # 简化解析：抓 "涨停:" 与 "跌停:" 后面的数字
        m_zt = re.search(r"涨停[:：]\s*(\d+)", text)
        m_dt = re.search(r"跌停[:：]\s*(\d+)", text)
        out["limit_up_count"] = int(m_zt.group(1)) if m_zt else 0
        out["limit_down_count"] = int(m_dt.group(1)) if m_dt else 0
        out["limit_up_seal_rate"] = None
        out["advance_decline_ratio"] = None
        out["new_high_minus_new_low"] = None
        return out
    except Exception as e:
        log.warning("sina breadth FAIL: %s", repr(e)[:140])
        raise


# ---------------------------------------------------------------- 主入口

def _fetch_breadth(date_str: str = None, codes: list = None) -> dict:
    """三档兜底：em → tx → sina。返回 dict（至少含 source）。

    源函数通过模块属性延迟查找（便于测试 monkey-patch 单档源）。
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    # 紧凑日期（akshare stock_zt_pool_em 用 YYYYMMDD）
    date_compact = date_str.replace("-", "")
    this = sys.modules[__name__]
    for name in ("_fetch_em_breadth", "_fetch_tx_breadth", "_fetch_sina_breadth"):
        try:
            if name == "_fetch_tx_breadth":
                out = this._fetch_tx_breadth(date_compact, codes)
            else:
                out = getattr(this, name)(date_compact)
            if out.get("limit_up_count") is not None or out.get("limit_down_count"):
                log.info("fetch_breadth OK source=%s", out.get("source"))
                return out
        except Exception as e:
            log.warning("fetch_breadth source=%s FAIL: %s", name, repr(e)[:140])
    return {"source": None, "limit_up_count": 0, "limit_down_count": 0,
            "limit_up_seal_rate": None, "advance_decline_ratio": None,
            "new_high_minus_new_low": None}


# ---------------------------------------------------------------- 指标计算（Fix-1）

# composite 加权 z 系数：(列名, 权重, 方向)。量级与 signals/breadth.py 的
# threshold=-2 匹配（真 z-score，不再是原始值算术平均）。
_Z_WEIGHTS = (
    ("limit_up_count", 0.30, +1.0),
    ("limit_down_count", 0.30, -1.0),
    ("advance_decline_ratio", 0.20, +1.0),
    ("new_high_minus_new_low", 0.20, +1.0),
)
Z_WINDOW = 250          # z-score 历史窗口
Z_MIN_SAMPLES = 60      # 样本 <60 → z 不可信返回 None


def _rolling_zscore(conn: sqlite3.Connection, col: str, cur: float,
                    date_str: str, window: int = Z_WINDOW,
                    min_samples: int = Z_MIN_SAMPLES):
    """breadth_daily 近 window 日 col 列历史（不含当日）+ 当日值 cur 的 z-score。

    返回 (z, actual_window)；样本 < min_samples 或 std=0 → (None, None)。
    冷启动 60~249 样本用实际窗口，actual_window 供 source 字段标注。
    col 必须来自 _Z_WEIGHTS 白名单（防注入）。
    """
    assert col in tuple(w[0] for w in _Z_WEIGHTS)
    try:
        rows = conn.execute(
            "SELECT %s FROM breadth_daily WHERE date < ? AND %s IS NOT NULL"
            " ORDER BY date DESC LIMIT ?" % (col, col),
            (date_str, window)).fetchall()
    except sqlite3.OperationalError:
        return None, None
    vals = [float(cur)] + [float(r[0]) for r in rows]
    n = len(vals)
    if n < min_samples:
        return None, None
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    std = var ** 0.5
    if std <= 1e-12:
        return None, None
    return (vals[0] - mean) / std, n


def _composite_from_z(conn: sqlite3.Connection, out: dict, date_str: str):
    """加权 z composite：任一因子 z 缺失 → 权重重归一化后加权；全缺失 → None。

    返回 (composite, z_window_actual)：z_window_actual 为参与因子的最小实际样本数
    （< 250 表示冷启动），供 source 标注。
    """
    comp = 0.0
    total_w = 0.0
    min_window = None
    for col, w, sign in _Z_WEIGHTS:
        v = out.get(col)
        if v is None:
            continue
        z, actual = _rolling_zscore(conn, col, float(v), date_str)
        if actual is not None and (min_window is None or actual < min_window):
            min_window = actual
        if z is None:
            continue
        comp += w * sign * z
        total_w += w
    if total_w <= 0:
        return None, min_window
    return round(comp / total_w, 4), min_window


def _count_new_high_low(conn: sqlite3.Connection, window: int = 60):
    """创 60 日新高家数 − 创新低家数（收盘价口径，qfq 优先）。

    从 daily_bar 全表扫；表空/缺列 → None。样本不足 window 的票跳过。
    """
    try:
        rows = conn.execute(
            "SELECT code, close, close_qfq FROM daily_bar ORDER BY code, trade_date"
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows:
        return None
    series: dict = {}
    for code, close, close_qfq in rows:
        px = close_qfq if close_qfq is not None else close
        if px is None:
            continue
        series.setdefault(code, []).append(float(px))
    nh = nl = 0
    for pxs in series.values():
        if len(pxs) < window:
            continue
        w = pxs[-window:]
        cur = w[-1]
        if cur >= max(w):
            nh += 1
        elif cur <= min(w):
            nl += 1
    return nh - nl


def fetch_breadth_daily(date_str: str = None, conn: sqlite3.Connection = None) -> dict:
    """采集 + 写 breadth_daily 表，返回 dict（带 source 与各指标）。"""
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    own = conn is None
    c = conn
    if own:
        from data.fetcher import get_conn
        c = get_conn()
    try:
        out = _fetch_breadth(date_str)
        if out.get("source") is None:
            log.warning("fetch_breadth_daily: 三档全失败")
            return out
        # new_high_minus_new_low：daily_bar 全表扫（源无关，Fix-1）
        try:
            out["new_high_minus_new_low"] = _count_new_high_low(c)
        except Exception as e:  # noqa: BLE001
            log.warning("new_high_minus_new_low FAIL: %s", repr(e)[:140])
            out["new_high_minus_new_low"] = None
        # composite：加权 z-score（历史不足 60 日 → None，极端避险档不会被假数据误触）
        try:
            composite, z_window = _composite_from_z(c, out, date_str)
        except Exception as e:  # noqa: BLE001
            log.warning("breadth composite FAIL: %s", repr(e)[:140])
            composite, z_window = None, None
        out["breadth_composite"] = composite
        source = out.get("source")
        if source and z_window is not None and z_window < Z_WINDOW:
            source = f"{source}|z_window={z_window}"
        out["source"] = source
        c.execute(
            "INSERT OR REPLACE INTO breadth_daily"
            " (date, limit_up_count, limit_up_seal_rate, limit_down_count,"
            "  advance_decline_ratio, new_high_minus_new_low, breadth_composite, source)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (date_str, out.get("limit_up_count"),
             out.get("limit_up_seal_rate"), out.get("limit_down_count"),
             out.get("advance_decline_ratio"), out.get("new_high_minus_new_low"),
             out.get("breadth_composite"), out.get("source")))
        c.commit()
        log.info("fetch_breadth_daily 写入 %s source=%s composite=%s",
                 date_str, out.get("source"), composite)
    finally:
        if own:
            c.close()
    return out


if __name__ == "__main__":
    print("== Sprint 2 任务 3：市场宽度 ==")
    print(json.dumps(fetch_breadth_daily(), ensure_ascii=False, indent=2, default=str))
