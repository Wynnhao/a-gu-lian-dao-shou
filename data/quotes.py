"""盘中实时行情：腾讯批量主源 + 东财单票兜底，带 TTL 缓存与闭市回退。

约定：
- 只读接口，绝不写 daily_bar（日线历史只归盘后正式入库管）；
- 快照审计写 logs/quotes/<日期>.jsonl，供事后核对盘中价口径；
- 网络失败一律返回 None 由调用方回退日线收盘价，绝不抛异常中断流程。
"""
import json
import logging
import logging.handlers
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import requests

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

# 分钟级行情门控时段唯一口径（Phase 2 收敛）：薄壳 re-export，paper/runner 的
# 「from data.quotes import is_trading_time」懒加载路径零改动
from common.market import is_trading_time  # noqa: F401,E402

log = logging.getLogger("quotes")
log.setLevel(logging.INFO)
if not log.handlers:
    from common.logsetup import rotating_handler  # AGSICKLE_LOG_DIR 逃生门（Sprint4 W0-1）
    log.addHandler(rotating_handler("quotes.log"))
    log.addHandler(logging.StreamHandler())
log.propagate = False

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
                # Sprint 1 任务3：规则21 跌停封单应急 + 与公式兜底交叉验证
                # 字段索引沿用本函数"市场类型 + 1"的内部偏移约定（与 f[33]/f[34] 同款）
                "ask1_price": float(f[21]) if len(f) > 21 and f[21] else None,
                "ask1_vol": float(f[22]) if len(f) > 22 and f[22] else None,  # 单位：手
                "float_mv": float(f[42]) if len(f) > 42 and f[42] else None,  # 流通市值，元
                "limit_up": float(f[45]) if len(f) > 45 and f[45] else None,
                "limit_down": float(f[46]) if len(f) > 46 and f[46] else None,
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
    # 测试逃生门：AGSICKLE_MOCK_QUOTES=<path.json> 指向 {code: quote_dict} 快照，
    # 黑盒测试用它打桩实时行情，绝不出网（调用时读 env）。
    mock_path = os.environ.get("AGSICKLE_MOCK_QUOTES")
    if mock_path:
        try:
            data = json.loads(Path(mock_path).read_text(encoding="utf-8"))
            return {c: data[c] for c in codes if c in data}
        except (OSError, ValueError, KeyError) as e:
            log.warning("MOCK_QUOTES 读取失败 %s: %s", mock_path, e)
            return {}
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


# ---------------------------------------------------------------- 录制专用快照（C-ARC-3a/T5）

# 显式指数符号表（与 fetcher.INDEX_TX_SYMBOL 对齐）。绝不经 _exchange_prefix 拼
# 指数符号——000001 会被拼成 sz000001=平安银行（审核抓出的三个硬伤之一）。
INDEX_SYMBOLS = {
    "000001": "sh000001",   # 上证指数
    "000300": "sh000300",   # 沪深300
    "000905": "sh000905",   # 中证500
    "399001": "sz399001",   # 深证成指
    "399006": "sz399006",   # 创业板指
}

# 免费源对无 UA 请求更易封禁（照 fetcher._session 的教训）
_UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
}

_RECORDER_MAX_FAIL = 5
_RECORDER_COOLDOWN_MIN = 30


def _recorder_breaker_file() -> Path:
    """录制器独立熔断状态文件；AGSICKLE_STATE_DIR 调用时读（测试隔离，C-ENG-6 语义）。"""
    state = Path(os.environ.get("AGSICKLE_STATE_DIR") or (BASE / "logs" / "state"))
    return state / "recorder_health.json"


def _recorder_health() -> dict:
    """{"fail_count": int, "blocked_until": epoch 秒}；文件缺失/损坏视为全新状态。"""
    try:
        raw = json.loads(_recorder_breaker_file().read_text(encoding="utf-8"))
        return {"fail_count": int(raw.get("fail_count") or 0),
                "blocked_until": float(raw.get("blocked_until") or 0.0)}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"fail_count": 0, "blocked_until": 0.0}


def _save_recorder_health(count: int, blocked_until: float) -> None:
    """原子落盘（tmp+replace，照 fetcher.source_health.json 模式）；失败仅告警。
    录制器是 5 分钟短命进程：fail_count 必须随文件持久化，连续失败才能跨进程累计。"""
    try:
        p = _recorder_breaker_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fail_count": count, "blocked_until": blocked_until}),
                       encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        log.warning("录制器熔断状态落盘失败: %s", e)


def recorder_blocked() -> bool:
    """冷却中：连续失败 ≥ _RECORDER_MAX_FAIL 后冷却 _RECORDER_COOLDOWN_MIN 分钟。"""
    return time.time() < _recorder_health()["blocked_until"]


def _mark_recorder(ok: bool) -> None:
    h = _recorder_health()
    if ok:
        if h["fail_count"] or h["blocked_until"]:
            _save_recorder_health(0, 0.0)
        return
    n = h["fail_count"] + 1
    if n >= _RECORDER_MAX_FAIL:
        _save_recorder_health(0, time.time() + _RECORDER_COOLDOWN_MIN * 60)
        log.warning("录制快照源连续失败 %d 次，冷却 %d 分钟（已落盘，跨进程生效）",
                    n, _RECORDER_COOLDOWN_MIN)
    else:
        _save_recorder_health(n, h["blocked_until"])


def fetch_snapshot(codes: List[str], index_codes: Optional[List[str]] = None
                   ) -> Dict[str, dict]:
    """录制专用批量快照（腾讯一次请求，个股+指数混编）。与 get_live_prices 的差异：

    - 指数走 INDEX_SYMBOLS 显式符号表（_exchange_prefix 会把 000001 拼成 sz000001）；
    - 解析 volume（f[6]，手→股 ×100）与 amount（f[37]，万元→元 ×10000），
      量纲口径与 movers 的 spot_tx 兜底换算一致；
    - 请求带 UA；
    - **绝不写 _audit_snapshot jsonl**（录制数据落点是 minute_snapshot 表，防双写）；
    - 独立冷却（recorder_health.json），不动 fetcher 的 akshare 熔断器；
    - 同样绝不写 daily_bar（本模块红线不变）。
    - 字段索引口径注记（C-ARC 补修3，Sprint4 落地核验/协同点6）：本函数自带
      f[3]/f[4]/f[6]/f[37] 解析、与 _fetch_tencent 互不改动；扩展字段（卖一档/
      流通市值/涨跌停）口径以 Sprint4 W-A4 对 _fetch_tencent 的实测重映射
      （ask1=f[19]/f[20]、float_mv=f[44]×1e8、涨跌停=f[47]/f[48]）为准，两处仅互证。

    返回 {code: {price, prev_close, volume, amount, time, source}}；整体失败/冷却
    返回 {}，由调用方按整轮失败处理。
    """
    if recorder_blocked():
        log.warning("录制快照源冷却中（recorder_health.json），本轮跳过")
        return {}
    symbols: Dict[str, str] = {}   # 腾讯 symbol -> 6 位码
    for c in codes:
        pfx = _exchange_prefix(str(c))
        if pfx:
            symbols[pfx + str(c)] = str(c)
    for c in (index_codes or []):
        sym = INDEX_SYMBOLS.get(str(c))
        if sym:
            symbols[sym] = str(c)
    if not symbols:
        return {}
    url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
    try:
        resp = requests.get(url, timeout=_TIMEOUT, headers=_UA_HEADERS)
        resp.encoding = "gbk"
    except Exception as e:  # noqa: BLE001
        _mark_recorder(False)
        log.warning("录制快照请求失败: %s", repr(e)[:120])
        return {}
    out: Dict[str, dict] = {}
    now_iso = datetime.now().isoformat(timespec="seconds")
    for m in re.finditer(r'v_(sh|sz)(\d{6})="([^"]*)"', resp.text):
        code = symbols.get(m.group(1) + m.group(2))
        if not code:
            continue
        f = m.group(3).split("~")
        if len(f) < 6:
            continue
        try:
            if float(f[3]) <= 0:  # 停牌"0.00"不得作为有效价（照 _fetch_tencent 口径）
                continue
        except ValueError:
            continue
        try:
            out[code] = {
                "price": float(f[3]),
                "prev_close": float(f[4]) if f[4] else None,
                "volume": float(f[6]) * 100.0 if f[6] else None,   # 手 → 股
                "amount": (float(f[37]) * 10000.0)
                          if len(f) > 37 and f[37] else None,      # 万元 → 元
                "time": f[30] if len(f) > 30 and f[30] else now_iso,
                "source": "tencent_snapshot",
            }
        except (ValueError, IndexError):
            continue
    _mark_recorder(bool(out))
    log.info("录制快照 %d/%d 票（含指数）", len(out), len(symbols))
    return out


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
