"""数据层：行情拉取 + SQLite 入库（增量）。

数据质量约定（2026-09 多维审查后新增）：
- daily_bar.volume 统一为「手」（A股日线惯例，东财口径）；腾讯兜底源返回「股」时
  按 amount/close 隐含股数逐行归一，另由 data/audit.py 做存量体检与修复；
- daily_bar.pct_chg 优先用东财官方「涨跌幅」列（已按除息参考价计算），腾讯源由
  库内前收盘推算，首行无前收时存 0 并由 audit 标注；
- daily_bar.close_qfq 存前复权收盘（除权除息不污染动量/均线类因子），因子层
  优先取它，缺失时回退不复权 close；high_qfq/low_qfq 由 close_qfq/close 比例
  同行导出（复权是逐行线性缩放），供 ATR 全复权计算（混用 raw H/L 与 qfq C
  会在除权日产生假 TR 跳变）；
- index_daily 存指数 close + high/low（RSRS 需要高低价回归）；
- daily_bar.source / fetch_log.detail 记录实际命中数据源，降级口径可审计；
- SQLite 连接统一 WAL + busy_timeout，webapp/catchup/postclose 三方并发写不再撞锁。
"""
import json
import logging
import logging.handlers
import sqlite3
import time
from datetime import datetime, date, timedelta
from pathlib import Path

import akshare as ak
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.handlers.RotatingFileHandler(BASE / "logs" / "fetch.log", encoding="utf-8", maxBytes=5_000_000, backupCount=3),
              logging.StreamHandler()],
)
log = logging.getLogger("fetcher")

_DATA_CFG = CFG.get("data", {})
START_DATE = _DATA_CFG.get("start_date", "20240101")
SOURCE_COOLDOWN_MIN = _DATA_CFG.get("source_cooldown_min", 30)
SOURCE_MAX_FAIL = _DATA_CFG.get("source_max_fail", 5)

DDL = """
CREATE TABLE IF NOT EXISTS daily_bar (
    code TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL, pct_chg REAL, turnover REAL,
    source TEXT, close_qfq REAL, high_qfq REAL, low_qfq REAL,
    PRIMARY KEY (code, trade_date)
);
CREATE TABLE IF NOT EXISTS stock_info (
    code TEXT PRIMARY KEY, name TEXT, first_trade_date TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS fetch_log (
    code TEXT, run_at TEXT, status TEXT, rows INT, detail TEXT
);
-- 交易日历（新浪历史交易日表缓存），is_trading_day 的权威来源
CREATE TABLE IF NOT EXISTS trade_calendar (
    date TEXT PRIMARY KEY
);
-- P1.4 资讯：code='' 表示宏观/市场级新闻
CREATE TABLE IF NOT EXISTS news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT, title TEXT, content TEXT, source TEXT, url TEXT,
    published_at TEXT, fetched_at TEXT,
    UNIQUE(code, title, published_at)
);
CREATE INDEX IF NOT EXISTS idx_news_code_pub ON news(code, published_at);
-- P1.5 指数行情与估值分位（high/low 供 RSRS 阻力支撑回归）
CREATE TABLE IF NOT EXISTS index_daily (
    index_code TEXT, trade_date TEXT, close REAL, high REAL, low REAL,
    PRIMARY KEY (index_code, trade_date)
);
CREATE TABLE IF NOT EXISTS index_valuation (
    index_code TEXT, trade_date TEXT,
    pe REAL, pe_pct REAL, pb REAL, pb_pct REAL, close REAL,
    PRIMARY KEY (index_code, trade_date)
);
-- P2 信号：signals 为因子 JSON，score 为综合分
CREATE TABLE IF NOT EXISTS signal (
    code TEXT, as_of TEXT, signals TEXT, score REAL,
    PRIMARY KEY (code, as_of)
);
-- P3 决策：input_snapshot 保存完整输入快照用于归因
-- trade_date=预期执行日（决策口径修复：盘前决策 run_date 曾取 T-1 导致日报查空）；
-- 幂等不做 DB 唯一约束（午评同日同票再决策是合法的），由 decide.save_decisions
-- 按 (run_date, code, action, target_weight, confidence, reasons) 内容级查重
CREATE TABLE IF NOT EXISTS decision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT, code TEXT, action TEXT, target_weight REAL,
    confidence REAL, reasons TEXT, risk_notes TEXT,
    input_snapshot TEXT, status TEXT, created_at TEXT,
    trade_date TEXT, model TEXT, prompt_version TEXT,
    t1_ret REAL, direction_hit INT, review TEXT
);
-- P4 持仓（本地镜像，T+1 由 avail_shares 体现）与成交
CREATE TABLE IF NOT EXISTS position (
    code TEXT PRIMARY KEY, name TEXT, shares INT, avail_shares INT,
    cost REAL, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS trade (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT, code TEXT, name TEXT, side TEXT,
    price REAL, shares INT, amount REAL,
    order_id TEXT, status TEXT, decision_id INT,
    shots TEXT, confirmed_by TEXT, created_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_decision_uniq
    ON trade(decision_id) WHERE decision_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS portfolio_state (
    date TEXT PRIMARY KEY, cash REAL, market_value REAL, total REAL,
    drawdown REAL, kill_switch INT, note TEXT
);
CREATE TABLE IF NOT EXISTS risk_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, rule TEXT, detail TEXT, decision_id INT
);
-- 动态池（异动池/热门池）：按刷新日期留痕，当前成员=各票最新 added_date 行
-- mode 记录刷新口径（market=全市场快照 / watchlist=自选池），跨口径 strength 不可比
CREATE TABLE IF NOT EXISTS dynamic_pool (
    pool TEXT, code TEXT, name TEXT, reason TEXT,
    strength REAL, added_date TEXT, updated_at TEXT, mode TEXT,
    PRIMARY KEY (pool, code, added_date)
);
-- 回测宇宙成员留痕（如中证800）：宇宙票不进 stock_info（否则会混入自选池
-- 信号计算与决策 bundle），只进 daily_bar 供回测；成分表按快照日留痕
CREATE TABLE IF NOT EXISTS universe_member (
    universe TEXT, code TEXT, name TEXT, as_of TEXT,
    PRIMARY KEY (universe, code, as_of)
);
"""

# 已有库的增量迁移：DDL 只对新建库生效，老库靠 ALTER 补列
_MIGRATIONS = [
    ("daily_bar", "source", "ALTER TABLE daily_bar ADD COLUMN source TEXT"),
    ("daily_bar", "close_qfq", "ALTER TABLE daily_bar ADD COLUMN close_qfq REAL"),
    ("daily_bar", "high_qfq", "ALTER TABLE daily_bar ADD COLUMN high_qfq REAL"),
    ("daily_bar", "low_qfq", "ALTER TABLE daily_bar ADD COLUMN low_qfq REAL"),
    ("index_daily", "high", "ALTER TABLE index_daily ADD COLUMN high REAL"),
    ("index_daily", "low", "ALTER TABLE index_daily ADD COLUMN low REAL"),
    ("decision", "trade_date", "ALTER TABLE decision ADD COLUMN trade_date TEXT"),
    ("decision", "model", "ALTER TABLE decision ADD COLUMN model TEXT"),
    ("decision", "prompt_version", "ALTER TABLE decision ADD COLUMN prompt_version TEXT"),
    ("decision", "t1_ret", "ALTER TABLE decision ADD COLUMN t1_ret REAL"),
    ("decision", "direction_hit", "ALTER TABLE decision ADD COLUMN direction_hit INT"),
    ("decision", "review", "ALTER TABLE decision ADD COLUMN review TEXT"),
    ("dynamic_pool", "mode", "ALTER TABLE dynamic_pool ADD COLUMN mode TEXT"),
]


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(BASE / CFG["db_path"], timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(DDL)
    for table, col, sql in _MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and col not in cols:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # 并发下另一进程已加列
    conn.commit()
    return conn


# ---------------------------------------------------------------- 限速/熔断

_SESSION = None


def _session():
    """带 UA 的共享 Session（免费源对无 UA 请求更易封禁）。"""
    global _SESSION
    if _SESSION is None:
        import requests
        _SESSION = requests.Session()
        _SESSION.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
        })
    return _SESSION


_fail_counts: dict = {}   # source -> 连续失败次数
_blocked_until: dict = {}  # source -> 解禁时间戳（epoch 秒，墙钟；跨进程持久）

# 熔断状态落盘：fetcher 总在短命进程里跑（launchd/catchup 每30分钟拉起一次），
# 纯进程内 dict 的冷却期跨不过进程边界，死源每个周期都被重新探测 SOURCE_MAX_FAIL 次
#（09-15 em 全天封禁当日即复现）。冷却状态写 logs/state/source_health.json，
# 新进程 import 时载入未过期的冷却项；墙钟时间戳保证跨进程语义一致。
_BREAKER_FILE = BASE / "logs" / "state" / "source_health.json"


def _load_breaker() -> dict:
    """读取落盘的冷却状态，自动丢弃已过期项；文件缺失/损坏视为无冷却。"""
    try:
        raw = json.loads(_BREAKER_FILE.read_text(encoding="utf-8"))
        now = time.time()
        return {str(k): float(v) for k, v in raw.items() if float(v) > now}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _save_breaker() -> None:
    """冷却状态原子落盘（tmp+replace）；失败仅告警，不影响主流程。"""
    try:
        _BREAKER_FILE.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        live = {k: v for k, v in _blocked_until.items() if v > now}
        tmp = _BREAKER_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(live), encoding="utf-8")
        tmp.replace(_BREAKER_FILE)
    except OSError as e:
        log.warning("熔断状态落盘失败: %s", e)


_blocked_until.update(_load_breaker())  # import 期恢复上一进程留下的冷却期


def source_blocked(source: str) -> bool:
    """熔断：某数据源连续失败 SOURCE_MAX_FAIL 次后冷却 SOURCE_COOLDOWN_MIN 分钟
    （状态落盘，跨进程/跨运行周期生效）。"""
    return time.time() < _blocked_until.get(source, 0.0)


def _mark_source(source: str, ok: bool):
    if ok:
        _fail_counts[source] = 0
        return
    n = _fail_counts.get(source, 0) + 1
    _fail_counts[source] = n
    if n >= SOURCE_MAX_FAIL:
        _blocked_until[source] = time.time() + SOURCE_COOLDOWN_MIN * 60
        _save_breaker()
        log.warning("数据源 %s 连续失败 %d 次，冷却 %d 分钟（已落盘，跨进程生效）",
                    source, n, SOURCE_COOLDOWN_MIN)


def call_ak(source: str, fn, *args, retries: int = 2, **kw):
    """带退避重试与熔断的 akshare 调用；被熔断时直接抛错由降级链接手。"""
    if source_blocked(source):
        raise ConnectionError(f"source {source} cooling down")
    last = None
    for i in range(retries + 1):
        try:
            out = fn(*args, **kw)
            _mark_source(source, True)
            return out
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries:
                time.sleep(2 ** i)  # 1s/2s 退避
    _mark_source(source, False)
    raise last


# ---------------------------------------------------------------- 量纲归一

def _norm_volume(volume, amount, close):
    """把任意源的成交量归一为「手」；依据 amount/close 的隐含股数判断原单位。

    腾讯源文档称 volume 为股、实测部分票按手返回，逐行判定比按源判定可靠；
    amount/close 缺失或两者都无法区分时原值返回，交由 data/audit.py 兜底。
    """
    try:
        v, amt, c = float(volume), float(amount), float(close)
    except (TypeError, ValueError):
        return volume
    if v <= 0 or amt <= 0 or c <= 0:
        return volume
    implied = amt / c  # ≈ 成交股数
    if abs(v - implied) <= abs(v * 100 - implied):
        return round(v / 100.0, 2)   # 原值是「股」→ 换成手
    return v                          # 原值已是「手」


# ---------------------------------------------------------------- 行情源

def _hist_em(code: str, start: str, end: str) -> pd.DataFrame:
    """东财源，统一为通用列名；官方「涨跌幅」列保留为 pct_chg（已按除息口径）。"""
    df = call_ak("em", ak.stock_zh_a_hist, symbol=code, period="daily",
                 start_date=start, end_date=end, adjust="")
    if df is None or df.empty:
        return df
    out = pd.DataFrame({
        "date": pd.to_datetime(df["日期"]),
        "open": df["开盘"], "high": df["最高"], "low": df["最低"],
        "close": df["收盘"], "volume": df["成交量"], "amount": df["成交额"],
        "turnover": df["换手率"] / 100.0,
    })
    if "涨跌幅" in df.columns:  # 官方口径，除息日不被当暴跌
        out["pct_chg"] = pd.to_numeric(df["涨跌幅"], errors="coerce")
    return out


def _hist_em_qfq(code: str, start: str, end: str) -> pd.DataFrame:
    """东财前复权收盘（供 close_qfq 列），失败由调用方降级跳过。"""
    df = call_ak("em_qfq", ak.stock_zh_a_hist, symbol=code, period="daily",
                 start_date=start, end_date=end, adjust="qfq")
    if df is None or df.empty:
        return df
    return pd.DataFrame({
        "date": pd.to_datetime(df["日期"]),
        "close_qfq": pd.to_numeric(df["收盘"], errors="coerce"),
    })


def _hist_tx_qfq(code: str, start: str, end: str) -> pd.DataFrame:
    """腾讯前复权 OHLC 兜底（东财封禁期间的前复权唯一来源）。

    返回 date/close_qfq/high_qfq/low_qfq；腾讯源按分页拉全史，量大时较慢。
    """
    prefix = ("sh" if code.startswith(("6", "9")) else
              "sz" if code.startswith(("0", "3")) else "bj")
    df = call_ak("tx_qfq", ak.stock_zh_a_hist_tx, symbol=f"{prefix}{code}",
                 start_date=start, end_date=end, adjust="qfq")
    if df is None or df.empty:
        return df
    return pd.DataFrame({
        "date": pd.to_datetime(df["date"]),
        "close_qfq": pd.to_numeric(df["close"], errors="coerce"),
        "high_qfq": pd.to_numeric(df["high"], errors="coerce"),
        "low_qfq": pd.to_numeric(df["low"], errors="coerce"),
    })


def _hist_tx(code: str, start: str, end: str) -> pd.DataFrame:
    """腾讯源兜底；volume 单位不定，交由 _norm_volume 逐行归一。"""
    prefix = ("sh" if code.startswith(("6", "9")) else
              "sz" if code.startswith(("0", "3")) else "bj")
    df = call_ak("tx", ak.stock_zh_a_hist_tx, symbol=f"{prefix}{code}",
                 start_date=start, end_date=end, adjust="")
    if df is None or df.empty:
        return df
    pct = df["close"].pct_change() * 100
    return pd.DataFrame({
        "date": pd.to_datetime(df["date"]),
        "open": df["open"], "high": df["high"], "low": df["low"],
        "close": df["close"], "volume": df["volume"], "amount": df["amount"],
        "turnover": df["turnover"] if "turnover" in df else None,
        "pct_chg": pct,
    })


def _market_data_window(now=None) -> bool:
    """数据采集保护窗：交易日 9:15-15:05（含集合竞价/午休/收盘尾差）。

    盘中防半根 bar 的判断必须覆盖午休——launchd 每 30 分钟触发 catchup，
    12:00/12:30 恰落在原 is_trading_time（930-1130/1300-1500）盲区内，
    会把当日半根 bar 入库且增量机制永不覆盖。节假日由 catchup 的日历门挡。
    """
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return 915 <= hm <= 1505


def fetch_daily(code: str, conn: sqlite3.Connection) -> int:
    """增量拉取单只股票日K（东财优先，腾讯兜底），返回新增行数。"""
    last = conn.execute(
        "SELECT MAX(trade_date) FROM daily_bar WHERE code=?", (code,)
    ).fetchone()[0]
    start = START_DATE
    if last:
        start = (pd.Timestamp(last) + pd.Timedelta(days=1)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    # 采集保护窗内东财会返回当日未走完的部分 bar，且增量机制（start=last+1）
    # 导致该半根 bar 永不被重取覆盖——窗口内一律截到昨日。
    if _market_data_window():
        end = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    # 今日 bar 是否真的缺失：已入库时增量起点 last+1 > end，双源返回空是正常
    # 幂等重跑，不能当"全源失败"误报（2026-09-15 重跑实测误写 empty_today）。
    today_str = date.today().strftime("%Y-%m-%d")
    today_missing = (last or "") < today_str

    df, src = None, ""
    for src_name, fn in (("em", _hist_em), ("tx", _hist_tx)):
        try:
            df = fn(code, start, end)
        except Exception as e:
            log.warning("%s via %s fail: %s", code, src_name, repr(e)[:80])
            continue
        if df is not None and not df.empty:
            src = src_name
            break
        # P0 修复：返回空 df 也得落日志（之前 em fail 看不到 tx 是否也是空的）
        log.warning("%s via %s returned empty df (start=%s end=%s)",
                    code, src_name, start, end)
    if df is None or df.empty:
        # P0 修复：今日全源失败要 fail-loud——em+tx 都返回空时写 fetch_log.status='empty_today'
        # 供 webapp HealthPanel 红警；postclose 会沿用昨日盯市（fail-open）。
        # 仅在今日 bar 确实缺失时报；幂等重跑（今日已有 bar）静默返回 0。
        if end == today_str.replace("-", "") and today_missing:
            log.error("今日日线全源失败: %s end=%s — daily_bar 不会更新，盯市/决策将沿用昨日",
                      code, end)
            try:
                conn.execute(
                    "INSERT INTO fetch_log VALUES (?,?,?,?,?)",
                    (code, datetime.now().isoformat(timespec="seconds"),
                     "empty_today", 0, "em+tx both empty"))
            except Exception as e:  # noqa: BLE001
                log.warning("fetch_log empty_today 写盘失败: %s", repr(e)[:120])
        return 0

    # 首行 pct_chg：东财官方列已带；腾讯源由库内前收盘推算（除息日口径也正确），
    # 无前收（历史首行）才退化为窗口内自算并记 0。
    if "pct_chg" not in df.columns or df["pct_chg"].isna().any():
        prev = conn.execute(
            "SELECT close FROM daily_bar WHERE code=? ORDER BY trade_date DESC LIMIT 1",
            (code,)).fetchone()
        computed = df["close"].pct_change() * 100
        if prev:
            first_pct = (float(df["close"].iloc[0]) / float(prev[0]) - 1) * 100
            computed.iloc[0] = first_pct
        if "pct_chg" not in df.columns:
            df["pct_chg"] = computed
        else:
            df["pct_chg"] = df["pct_chg"].fillna(computed)

    rows = [
        (code, r["date"].strftime("%Y-%m-%d"), float(r["open"]), float(r["high"]),
         float(r["low"]), float(r["close"]),
         float(_norm_volume(r["volume"], r["amount"], r["close"])),
         float(r["amount"]),
         float(r["pct_chg"]) if pd.notna(r["pct_chg"]) else 0.0,
         float(r["turnover"]) * 100 if pd.notna(r.get("turnover")) else 0.0,
         src, None)
        for _, r in df.iterrows()
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO daily_bar "
        "(code, trade_date, open, high, low, close, volume, amount, pct_chg, "
        " turnover, source, close_qfq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    return len(rows)


def backfill_qfq(code: str, conn: sqlite3.Connection) -> int:
    """回填前复权 OHLC 列（close_qfq/high_qfq/low_qfq；除权除息不再污染动量/均线/ATR）。

    东财 em_qfq 优先（仅收盘，high/low_qfq 由 close_qfq/close 比例同行导出），
    腾讯 tx_qfq 兜底（自带前复权 OHLC）。失败静默跳过：因子层自动回退不复权
    close，audit 会提示补跑。
    """
    last_qfq = conn.execute(
        "SELECT MAX(trade_date) FROM daily_bar WHERE code=? AND close_qfq IS NOT NULL",
        (code,)).fetchone()[0]
    start = START_DATE
    if last_qfq:
        start = (pd.Timestamp(last_qfq) - pd.Timedelta(days=7)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    if _market_data_window():
        end = (date.today() - timedelta(days=1)).strftime("%Y%m%d")

    df, src = None, ""
    for src_name, fn in (("em_qfq", _hist_em_qfq), ("tx_qfq", _hist_tx_qfq)):
        try:
            got = fn(code, start, end)
        except Exception as e:  # noqa: BLE001
            log.info("%s qfq via %s fail: %s", code, src_name, repr(e)[:80])
            continue
        if got is not None and not got.empty:
            df, src = got, src_name
            break
    if df is None or df.empty:
        return 0

    raw = {d: (h, l, c) for d, h, l, c in conn.execute(
        "SELECT trade_date, high, low, close FROM daily_bar WHERE code=?", (code,))}
    rows = []
    for _, r in df.iterrows():
        d = r["date"].strftime("%Y-%m-%d")
        cq = r["close_qfq"]
        if pd.isna(cq) or d not in raw:
            continue
        hq = r.get("high_qfq") if "high_qfq" in df.columns else None
        lq = r.get("low_qfq") if "low_qfq" in df.columns else None
        rh, rl, rc = raw[d]
        # em 源只给收盘：复权是逐行线性缩放，high/low_qfq 按 close 比例同行导出
        if (hq is None or pd.isna(hq)) and rc and rh is not None and not pd.isna(rh):
            hq = float(rh) * float(cq) / float(rc)
        if (lq is None or pd.isna(lq)) and rc and rl is not None and not pd.isna(rl):
            lq = float(rl) * float(cq) / float(rc)
        rows.append((round(float(cq), 4),
                     round(float(hq), 4) if hq is not None and not pd.isna(hq) else None,
                     round(float(lq), 4) if lq is not None and not pd.isna(lq) else None,
                     code, d))
    if not rows:
        return 0
    conn.executemany(
        "UPDATE daily_bar SET close_qfq=?, high_qfq=?, low_qfq=? "
        "WHERE code=? AND trade_date=?", rows)
    conn.commit()
    log.info("%s qfq via %s: %d rows", code, src, len(rows))
    return len(rows)


# ---------------------------------------------------------------- 指数日线

# 宽基指数 → 腾讯 symbol 前缀映射（显式表，避免按代码推断深市前缀出错）
INDEX_TX_SYMBOL = {
    "000001": "sh000001",   # 上证指数
    "000300": "sh000300",   # 沪深300
    "000905": "sh000905",   # 中证500
    "000906": "sh000906",   # 中证800
    "399006": "sz399006",   # 创业板指
    "399330": "sz399330",   # 深证300
}


def _index_em(code: str, start: str, end: str) -> pd.DataFrame:
    """东财指数日线（收盘+最高+最低），中文列名。"""
    df = call_ak("em", ak.index_zh_a_hist, symbol=code, period="daily",
                 start_date=start, end_date=end)
    if df is None or df.empty:
        return df
    return pd.DataFrame({
        "date": pd.to_datetime(df["日期"]),
        "close": pd.to_numeric(df["收盘"], errors="coerce"),
        "high": pd.to_numeric(df["最高"], errors="coerce"),
        "low": pd.to_numeric(df["最低"], errors="coerce"),
    })


def _index_tx(code: str, start: str, end: str) -> pd.DataFrame:
    """腾讯指数日线兜底（含 OHLC，全史）。"""
    sym = INDEX_TX_SYMBOL.get(code)
    if sym is None:
        raise ValueError(f"index {code} 无腾讯 symbol 映射")
    df = call_ak("tx", ak.stock_zh_index_daily_tx, symbol=sym)
    if df is None or df.empty:
        return df
    out = pd.DataFrame({
        "date": pd.to_datetime(df["date"]),
        "close": pd.to_numeric(df["close"], errors="coerce"),
        "high": pd.to_numeric(df["high"], errors="coerce"),
        "low": pd.to_numeric(df["low"], errors="coerce"),
    })
    return out[(out["date"] >= pd.Timestamp(start)) & (out["date"] <= pd.Timestamp(end))]


def _index_sina(code: str, start: str, end: str) -> pd.DataFrame:
    """新浪指数日线（ak.stock_zh_index_daily，em/tx 双挂时的第三兜底）。

    em/tx 都在 09-15 当日返回空时，新浪源通常仍能拿到上一交易日盘后数据；
    速度约 0.2s/指数，比 em/tx 快得多。注意：列名是 date/open/high/low/close/volume。
    """
    sym = INDEX_TX_SYMBOL.get(code)
    if sym is None:
        raise ValueError(f"index {code} 无新浪 symbol 映射")
    df = call_ak("sina", ak.stock_zh_index_daily, symbol=sym)
    if df is None or df.empty:
        return df
    out = pd.DataFrame({
        "date": pd.to_datetime(df["date"]),
        "close": pd.to_numeric(df["close"], errors="coerce"),
        "high": pd.to_numeric(df["high"], errors="coerce"),
        "low": pd.to_numeric(df["low"], errors="coerce"),
    })
    return out[(out["date"] >= pd.Timestamp(start)) & (out["date"] <= pd.Timestamp(end))]


def ensure_index_daily(conn: sqlite3.Connection, code: str,
                       start: str = "20180101") -> int:
    """保障指数日线（含 high/low）最新：东财主源 → 腾讯 → 新浪三档兜底。

    regime（二八轮动/RSRS）与回测基准共用；幂等 INSERT OR REPLACE。
    P1 修复：加新浪 sina 兜底（em/tx 持续失败场景仍可拉到上一日数据）。
    """
    end = date.today().strftime("%Y%m%d")
    if _market_data_window():
        end = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    df, src = None, ""
    for src_name, fn in (("em", _index_em), ("tx", _index_tx), ("sina", _index_sina)):
        try:
            got = fn(code, start, end)
        except Exception as e:  # noqa: BLE001
            log.info("index %s via %s fail: %s", code, src_name, repr(e)[:80])
            time.sleep(1)
            continue
        if got is not None and not got.empty:
            df, src = got, src_name
            break
    if df is None or df.empty:
        log.warning("index %s 三个源均不可用，沿用库内已有数据", code)
        return 0
    rows = [(code, r["date"].strftime("%Y-%m-%d"),
             float(r["close"]) if pd.notna(r["close"]) else None,
             float(r["high"]) if pd.notna(r.get("high")) else None,
             float(r["low"]) if pd.notna(r.get("low")) else None)
            for _, r in df.iterrows()]
    conn.executemany(
        "INSERT OR REPLACE INTO index_daily (index_code, trade_date, close, high, low) "
        "VALUES (?,?,?,?,?)", rows)
    conn.commit()
    log.info("index %s via %s: %d rows", code, src, len(rows))
    return len(rows)


def upsert_info(item: dict, conn: sqlite3.Connection):
    code, name = item["code"], item["name"]
    first = conn.execute(
        "SELECT MIN(trade_date) FROM daily_bar WHERE code=?", (code,)
    ).fetchone()[0]
    conn.execute(
        "INSERT OR REPLACE INTO stock_info VALUES (?,?,?,?)",
        (code, name, first, datetime.now().isoformat(timespec="seconds")),
    )


def run():
    conn = get_conn()
    try:  # 交易日历缓存（best-effort，失败退化 weekday 判断）
        from data.trade_cal import ensure_calendar
        ensure_calendar(conn)
    except Exception as e:  # noqa: BLE001
        log.warning("交易日历初始化跳过: %s", repr(e)[:80])
    for item in CFG["watchlist"]:
        code = item["code"]
        try:
            n = fetch_daily(code, conn)
            upsert_info(item, conn)
            conn.execute("INSERT INTO fetch_log VALUES (?,?,?,?,?)",
                         (code, datetime.now().isoformat(timespec="seconds"),
                          "ok", n, ""))
            log.info("%s %s: +%d rows", code, item["name"], n)
        except Exception as e:
            conn.execute("INSERT INTO fetch_log VALUES (?,?,?,?,?)",
                         (code, datetime.now().isoformat(timespec="seconds"),
                          "fail", 0, str(e)[:200]))
            log.error("%s FAIL: %s", code, e)
        conn.commit()
        time.sleep(1)  # 温和限速
    if not (source_blocked("em_qfq") and source_blocked("tx_qfq")):
        for item in CFG["watchlist"]:
            try:
                backfill_qfq(item["code"], conn)
            except Exception as e:  # noqa: BLE001
                log.warning("%s qfq FAIL: %s", item["code"], repr(e)[:80])
            time.sleep(0.5)
    conn.close()


if __name__ == "__main__":
    run()
