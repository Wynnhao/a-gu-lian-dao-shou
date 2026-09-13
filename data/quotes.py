"""盘中实时行情：腾讯批量主源 + 东财单票兜底，带 TTL 缓存与闭市回退。

约定：
- 只读接口，绝不写 daily_bar（日线历史只归盘后正式入库管）；
- 快照审计写 logs/quotes/<日期>.jsonl，供事后核对盘中价口径；
- 网络失败一律返回 None 由调用方回退日线收盘价，绝不抛异常中断流程。
"""
import json
import logging
import logging.handlers
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import requests

BASE = Path(__file__).resolve().parent.parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.handlers.RotatingFileHandler(BASE / "logs" / "quotes.log", encoding="utf-8", maxBytes=5_000_000, backupCount=3),
              logging.StreamHandler()],
)
log = logging.getLogger("quotes")

QUOTES_DIR = BASE / "logs" / "quotes"
TTL_SECONDS = 30
_TIMEOUT = 5

_cache: Dict[str, tuple] = {}  # code -> (quote_dict, monotonic_ts)
_last_fetch: Optional[float] = None  # 批量请求时间（整个批次共享 TTL）


# ---------------------------------------------------------------- 基础工具

def _exchange_prefix(code: str) -> Optional[str]:
    """600/601/603/605/688 → sh；000/001/002/003/300/301 → sz；北交所不覆盖。"""
    if code.startswith(("6", "9")):
        return "sh"
    if code.startswith(("0", "3")):
        return "sz"
    return None


def is_trading_time(now: Optional[datetime] = None) -> bool:
    """A股连续竞价时段：周一~五 9:30-11:30 / 13:00-15:00（与风控规则口径一致）。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)


def _audit_snapshot(quotes: Dict[str, dict]) -> None:
    """追加写当日快照审计文件（best-effort，失败不影响主流程）。"""
    try:
        QUOTES_DIR.mkdir(parents=True, exist_ok=True)
        path = QUOTES_DIR / (datetime.now().strftime("%Y-%m-%d") + ".jsonl")
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "quotes": quotes}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        log.warning("快照审计写入失败（忽略）: %s", repr(e))


# ---------------------------------------------------------------- 数据源

def _fetch_tencent(codes: List[str]) -> Dict[str, dict]:
    """腾讯批量行情 qt.gtimg.cn，一次请求覆盖全部代码，GBK 编码。

    返回 {code: {price, prev_close, open, high, low, time, source}}。
    """
    symbols = []
    for c in codes:
        pfx = _exchange_prefix(c)
        if pfx:
            symbols.append(pfx + c)
    if not symbols:
        return {}
    url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
    resp = requests.get(url, timeout=_TIMEOUT)
    resp.encoding = "gbk"
    out: Dict[str, dict] = {}
    now_iso = datetime.now().isoformat(timespec="seconds")
    for m in re.finditer(r'v_(?:sh|sz)(\d{6})="([^"]*)"', resp.text):
        code, payload = m.group(1), m.group(2)
        f = payload.split("~")
        if len(f) < 6:
            continue
        try:
            if float(f[3]) <= 0:  # 审查P2：停牌"0.00"不得作为有效实时价
                continue
        except ValueError:
            continue
        try:
            out[code] = {
                "price": float(f[3]),
                "prev_close": float(f[4]) if f[4] else None,
                "open": float(f[5]) if f[5] else None,
                "high": float(f[33]) if len(f) > 33 and f[33] else None,
                "low": float(f[34]) if len(f) > 34 and f[34] else None,
                "time": f[30] if len(f) > 30 else now_iso,
                "name": f[1] if len(f) > 1 else "",
                "source": "tencent",
            }
        except (ValueError, IndexError):
            continue
    return out


def _fetch_eastmoney_one(code: str) -> Optional[dict]:
    """东财单票兜底 push2 API；secid：sh=1.xxxx，sz=0.xxxx。"""
    pfx = _exchange_prefix(code)
    if pfx is None:
        return None
    secid = ("1." if pfx == "sh" else "0.") + code
    url = ("https://push2.eastmoney.com/api/qt/stock/get?secid=" + secid
           + "&fields=f43,f44,f45,f46,f60,f58,f124,f59")
    resp = requests.get(url, timeout=_TIMEOUT)
    data = resp.json().get("data") or {}
    if not data.get("f43"):
        return None
    scale = 10 ** int(data.get("f59") or 2)
    return {
        "price": data["f43"] / scale,
        "prev_close": (data["f60"] / scale) if data.get("f60") else None,
        "open": (data["f46"] / scale) if data.get("f46") else None,
        "high": (data["f44"] / scale) if data.get("f44") else None,
        "low": (data["f45"] / scale) if data.get("f45") else None,
        "time": str(data.get("f124") or ""),
        "name": data.get("f58") or "",
        "source": "eastmoney",
    }


# ---------------------------------------------------------------- 对外接口

def get_live_prices(codes: List[str], force: bool = False) -> Dict[str, dict]:
    """批量获取实时行情（带 TTL 缓存）。失败返回 {}，调用方自行回退收盘价。

    返回 {code: {price, prev_close, open, high, low, time, name, source}}。
    """
    global _last_fetch
    codes = [str(c) for c in codes]
    now = time.monotonic()
    if not force and _last_fetch is not None and (now - _last_fetch) < TTL_SECONDS:
        hit = {c: _cache[c][0] for c in codes if c in _cache}
        if len(hit) == len(codes):
            return hit            # 全命中才直接返回（审查P2：部分命中不丢码）
    quotes: Dict[str, dict] = {}
    try:
        quotes = _fetch_tencent(codes)
    except Exception as e:  # noqa: BLE001
        log.warning("腾讯批量行情失败: %s", repr(e)[:120])
    missing = [c for c in codes if c not in quotes]
    for c in missing:
        try:
            q = _fetch_eastmoney_one(c)
            if q:
                quotes[c] = q
                time.sleep(0.3)
        except Exception as e:  # noqa: BLE001
            log.warning("%s 东财兜底失败: %s", c, repr(e)[:120])
    if quotes:
        ts = time.monotonic()
        _last_fetch = ts
        for c, q in quotes.items():
            _cache[c] = (q, ts)
        if is_trading_time():
            _audit_snapshot(quotes)
        log.info("实时行情 %d/%d 票（腾讯主源+东财兜底）", len(quotes), len(codes))
    return quotes


def get_live_price(codes: List[str], code: str) -> Optional[float]:
    """单票实时价，取不到返回 None。"""
    return get_live_prices(codes).get(str(code), {}).get("price")


def freshness_note(q: Optional[dict]) -> str:
    """给调用方生成口径注：该报价的时间与来源。"""
    if not q:
        return "日线收盘价（实时行情不可用）"
    return "实时价 %s（%s）" % (q.get("time"), q.get("source"))


def clear_cache() -> None:
    """测试用：清空进程内缓存。"""
    global _last_fetch
    _cache.clear()
    _last_fetch = None
