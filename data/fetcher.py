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
- daily_bar.open_qfq（批次0 Gate 0-B，2026-09-20）：与 high/low_qfq 同一等比
  口径 open × (close_qfq/close)，round4 舍入单调 ⇒ raw low≤open≤high 成立时
  导出值不破坏不等式。增量写路径（backfill_qfq/rebrush_qfq_full）在检测到
  daily_bar 已有 open_qfq 列时同步写该列；列不存在则跳过（老库未跑过 Gate 0-B
  回填时行为零变化，不自动迁移 schema——列由批次0 gate0b-apply 单事务创建）；
- index_daily 存指数 close + high/low（RSRS 需要高低价回归）；
- daily_bar.source / fetch_log.detail 记录实际命中数据源，降级口径可审计；
- SQLite 连接统一 WAL + busy_timeout，webapp/catchup/postclose 三方并发写不再撞锁。
"""
import json
import logging
import logging.handlers
import os
import sqlite3
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

import akshare as ak
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))  # 本脚本被 catchup 当子进程直接跑，需能找到 common/

from common import market as _market  # noqa: E402
from common.config import snapshot  # noqa: E402
from data import repo  # noqa: E402

CFG = snapshot()  # 统一配置层：import 期冻结 + 硬键校验 fail-fast

log = logging.getLogger("fetcher")
log.setLevel(logging.INFO)
if not log.handlers:
    from common.logsetup import rotating_handler  # AGSICKLE_LOG_DIR 逃生门（Sprint4 W0-1）
    log.addHandler(rotating_handler("fetch.log"))
    log.addHandler(logging.StreamHandler())
log.propagate = False

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
-- P2 信号：signals 为因子 JSON，score 为综合分；profile 标识打分口径
-- （Fix-5：v1/v2 双 profile 行并存，signal_eval 按 config.signals.profile 过滤）
CREATE TABLE IF NOT EXISTS signal (
    code TEXT, as_of TEXT, signals TEXT, score REAL,
    profile TEXT DEFAULT 'reversal_lowvol',
    PRIMARY KEY (code, as_of, profile)
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
    t1_ret REAL, direction_hit INT, review TEXT,
    emergency_scan INT DEFAULT 0
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
-- Fix-4（D1）：规则 21 跌停应急扫描——连续跌停 stuck 计数（code 主键，
-- 首次命中 insert、后续扫描 +1；不再命中即解除删除）
CREATE TABLE IF NOT EXISTS limit_halt_stuck (
    code TEXT PRIMARY KEY, first_stuck_date TEXT,
    stuck_days INT DEFAULT 1, last_attempt TEXT
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

-- Sprint 2 任务 1（P1-3）：10Y 国债收益率与 ETF 份额旁路
CREATE TABLE IF NOT EXISTS index_bond_yield (
    index_code TEXT, trade_date TEXT, yield REAL,
    delta_20d_bp REAL, source TEXT,
    PRIMARY KEY (index_code, trade_date)
);
CREATE TABLE IF NOT EXISTS index_etf_share (
    etf_code TEXT, trade_date TEXT, share REAL,
    pct_chg_1d REAL, source TEXT,
    PRIMARY KEY (etf_code, trade_date)
);

-- Sprint 2 任务 2（P1-4）：业绩预告关键词事件
CREATE TABLE IF NOT EXISTS news_earnings (
    code TEXT, date TEXT, kind TEXT, count INT, samples TEXT,
    PRIMARY KEY (code, date, kind)
);

-- Sprint 2 任务 3（P1-1）：市场宽度/情绪每日指标
CREATE TABLE IF NOT EXISTS breadth_daily (
    date TEXT PRIMARY KEY,
    limit_up_count INT, limit_up_seal_rate REAL,
    limit_down_count INT, advance_decline_ratio REAL,
    new_high_minus_new_low INT, breadth_composite REAL,
    source TEXT
);

-- C-ARC-3b（ADR-A3 对「不引新表」的显式豁免落点，docs/架构借鉴-cloddsbot-2026-09-19.md）：
-- 盘中 5 分钟栅格快照，供盘中时点回放/回测（尾盘决策、止损触线、limit_halt 应急的
-- 验证基础设施）。ts 为 5 分钟栅格 ISO 串；INSERT OR REPLACE 天然幂等
-- （launchd 合并触发/手动补跑无冲突）。录制器绝不写 daily_bar。
CREATE TABLE IF NOT EXISTS minute_snapshot (
    code TEXT NOT NULL, ts TEXT NOT NULL,
    price REAL, volume REAL, amount REAL, source TEXT,
    PRIMARY KEY (code, ts)
);
CREATE INDEX IF NOT EXISTS idx_minute_snapshot_ts ON minute_snapshot(ts);
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
    # Fix-4：decision 行是否为跌停应急扫描单（超时兜底查询用）
    ("decision", "emergency_scan",
     "ALTER TABLE decision ADD COLUMN emergency_scan INT DEFAULT 0"),
    # Sprint 2 增量迁移（Sprint 1 任务 3 quotes 字段扩放在 quote_snapshot，DDL 由 data/quotes.py 维护）
]


def _migrate_signal_profile(conn: sqlite3.Connection) -> None:
    """Fix-5：老库 signal 表主键 (code, as_of) → (code, as_of, profile)。

    ALTER 无法改主键，走重建：旧行全部归入 'reversal_lowvol'（历史均为 v1 口径）。
    幂等：signal 表已有 profile 列则跳过；残留 signal_mig（上次迁移中断）先清。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(signal)")}
    if not cols or "profile" in cols:
        return
    conn.executescript("""
        DROP TABLE IF EXISTS signal_mig;
        CREATE TABLE signal_mig (
            code TEXT, as_of TEXT, signals TEXT, score REAL,
            profile TEXT DEFAULT 'reversal_lowvol',
            PRIMARY KEY (code, as_of, profile)
        );
        INSERT INTO signal_mig (code, as_of, signals, score, profile)
            SELECT code, as_of, signals, score, 'reversal_lowvol' FROM signal;
        DROP TABLE signal;
        ALTER TABLE signal_mig RENAME TO signal;
    """)


_MIGRATED_FOR: Optional[str] = None  # 进程级：该库路径已完成 DDL+迁移（换库自动重跑）


def init_db(conn: sqlite3.Connection) -> None:
    """建表 + 增量迁移（幂等）。get_conn 进程内首连自动执行，通常无需手动调用。"""
    conn.executescript(DDL)
    for table, col, sql in _MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and col not in cols:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # 并发下另一进程已加列
    _migrate_signal_profile(conn)
    conn.commit()


def dedup_signal_table(conn: sqlite3.Connection) -> int:
    """Fix-5 后续清理：signal 表按 (code, as_of, profile) 主键去重，保留 rowid 最大行。

    由于 INSERT OR REPLACE 行为正确（主键含 profile），重复行来自早期迁移中断或
    旧版未带 profile 的写路径。人工触发、幂等、可重复执行。
    """
    cur = conn.execute("""
        DELETE FROM signal
        WHERE rowid NOT IN (
            SELECT MAX(rowid) FROM signal GROUP BY code, as_of, profile
        )
    """)
    n = cur.rowcount
    conn.commit()
    log.info("signal 去重：删除 %d 行重复", n)
    return n


def get_conn() -> sqlite3.Connection:
    # AGSICKLE_DB：测试逃生门，子进程黑盒测试用它把 DB 隔离到临时库
    db_file = os.environ.get("AGSICKLE_DB") or BASE / CFG["db_path"]
    conn = sqlite3.connect(db_file, timeout=15)
    # Row 同时支持 row[0]/row["name"]——repo 层与 webapp 风格统一，对既有 tuple
    # 索引风格向后兼容（Phase 4 连接层统一）
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=NORMAL")
    # DDL 从「每连接重跑」改为进程内首连一次（此前短命 pipeline 每连接跑 15 条 DDL）
    global _MIGRATED_FOR
    db_key = str(db_file)
    if _MIGRATED_FOR != db_key:
        init_db(conn)
        _MIGRATED_FOR = db_key
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


# ---------------------------------------------------------------- qfq 列族写路径助手

def _qfq_has_open_col(conn: sqlite3.Connection) -> bool:
    """daily_bar 是否已有 open_qfq 列（批次0 Gate 0-B 增列后增量路径同步写它；
    未增列的老库保持三列写入，行为零变化——schema 迁移权在 gate0b-apply）。"""
    return bool({r[1] for r in conn.execute("PRAGMA table_info(daily_bar)")}
                & {"open_qfq"})


def _write_qfq_rows(conn: sqlite3.Connection, rows: list,
                    with_open: bool) -> None:
    """qfq 列族 UPDATE（close_qfq/high_qfq/low_qfq[+open_qfq]，等比同行导出）。

    rows 元素 = (cq, hq, lq[, oq], code, trade_date)；with_open 由调用方经
    _qfq_has_open_col 判定。open_qfq = round(open × cq / close, 4)，与
    high/low_qfq 完全同一口径（Gate 0-B 增量一致性）。不 commit（事务归属调用方）。
    """
    if with_open:
        conn.executemany(
            "UPDATE daily_bar SET close_qfq=?, high_qfq=?, low_qfq=?, open_qfq=? "
            "WHERE code=? AND trade_date=?", rows)
    else:
        conn.executemany(
            "UPDATE daily_bar SET close_qfq=?, high_qfq=?, low_qfq=? "
            "WHERE code=? AND trade_date=?",
            [(cq, hq, lq, cd, d) for cq, hq, lq, _oq, cd, d in rows])


# ---------------------------------------------------------------- 量纲归一

def _norm_volume(volume, amount, close):
    """把任意源的成交量归一为「手」；判别核心在 common/market.py。

    腾讯源文档称 volume 为股、实测部分票按手返回，逐行判定比按源判定可靠；
    判定失败（含非法输入）原值返回，交由 data/audit.py 兜底（红线4 的回退差异）。
    """
    try:
        v = float(volume)
    except (TypeError, ValueError):
        return volume
    if _market.volume_unit_is_lots(v, amount, close):
        return v
    return round(v / 100.0, 2)   # 原值是「股」→ 换成手


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


def _before_morning_open(now=None) -> bool:
    """W-B9（P2-10）：交易日 9:15 前的早盘时段（当日 bar 尚未生成）。

    此时增量窗口若包含"今天"，双源必然返回空 → 误报 empty_today
    （09-16/17 实测 164/86 条）。与 _market_data_window 一样按 end 截到昨日处理。
    """
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return hm < 915


# W-B9（P2-11）：本次运行内已写 empty_today 的 {code: run_at}——run() 落 fetch_log
# "ok" 行前检查，rows=0 且已写 empty_today 时不再补一条矛盾的 ok 行
# （09-18 实测同一时刻 344 条 empty_today + 344 条 ok 并存）。
_EMPTY_TODAY_WRITTEN: dict = {}


def fetch_daily(code: str, conn: sqlite3.Connection) -> int:
    """增量拉取单只股票日K（东财优先，腾讯兜底），返回新增行数。"""
    last = repo.latest_bar_date(conn, code)
    start = START_DATE
    if last:
        start = (pd.Timestamp(last) + pd.Timedelta(days=1)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    # 采集保护窗内东财会返回当日未走完的部分 bar，且增量机制（start=last+1）
    # 导致该半根 bar 永不被重取覆盖——窗口内一律截到昨日。
    # W-B9（P2-10）：交易日 9:15 前同理——当日 bar 还没生成，窗口含"今天"必空，
    # 会把正常早盘拉取误报成 empty_today。
    if _market_data_window() or _before_morning_open():
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
            _EMPTY_TODAY_WRITTEN[code] = datetime.now().isoformat(timespec="seconds")
            try:
                conn.execute(
                    "INSERT INTO fetch_log VALUES (?,?,?,?,?)",
                    (code, _EMPTY_TODAY_WRITTEN[code],
                     "empty_today", 0, "em+tx both empty"))
            except Exception as e:  # noqa: BLE001
                log.warning("fetch_log empty_today 写盘失败: %s", repr(e)[:120])
        return 0

    # 首行 pct_chg：东财官方列已带；腾讯源由库内前收盘推算（除息日口径也正确），
    # 无前收（历史首行）才退化为窗口内自算并记 0。
    if "pct_chg" not in df.columns or df["pct_chg"].isna().any():
        prev = repo.latest_close(conn, code)
        computed = df["close"].pct_change() * 100
        if prev:
            first_pct = (float(df["close"].iloc[0]) / float(prev) - 1) * 100
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


def recalc_tx_pct(conn: sqlite3.Connection, code: Optional[str] = None,
                  tol_pp: float = 0.1) -> tuple:
    """W-B3（P1-10）：tx 源 pct_chg 除权修复——除权跳变日行 pct 用 qfq 环比。

    腾讯源 pct 由不复权 close 逐行差分（fetch_daily 首选 em 官方涨跌幅列，
    tx 兜底行在除权日呈假暴跌/假暴涨，000001 2024-06-14 存 -5.74% 实为 qfq
    口径 +1.13%）。前复权环比≈官方除息口径，qfq 在手时按它重算。

    扫描范围：source='tx' 及 source IS NULL（source 列上线前的老行——生产实测
    000001 的 tx 时代行 source 全为 NULL，同样带除权假跌签名；签名门槛保证
    em 官方口径行不被误改）。

    检测口径（计划 W-B3）：d(t)=close−close_qfq 跳变日（|Δd|>0.01，即除权
    事件）且 |现存 pct − qfq 环比| > tol_pp 才重算。**不能**只看背离：tx 加法型
    复权下，除权段内的正常日 qfq 环比与 raw 环比天然不同（低价高 adj 票可达
    数 pp），全量按 qfq 环比重写会污染正常行——实测全库仅 ~7k 行是真除权错行。

    - 行自身或其前一行无 close_qfq → 跳过并计数（调用方汇报）；
    - code=None 时扫全表（存量重算 / audit --fix），指定 code 时只处理该票
      （backfill_qfq 每次增量后调用，"未来行有 qfq 即用 qfq 环比"的落地路径）。

    返回 (fixed, skipped_no_qfq)。不 commit（事务归属调用方）。
    """
    where, args = ("WHERE code=? AND (source='tx' OR source IS NULL)", (str(code),)) if code \
        else ("WHERE source='tx' OR source IS NULL", ())
    rows = conn.execute(
        f"SELECT code, trade_date, pct_chg, close, close_qfq FROM daily_bar {where} "
        "ORDER BY code, trade_date", args).fetchall()
    fixed = skipped = 0
    prev = {}
    updates = []
    for cd, td, pct, c, cq in rows:
        pc, pcq = prev.get(cd, (None, None))
        prev[cd] = (c, cq)
        if cq is None or pcq is None or pct is None or c is None or pc is None:
            skipped += 1
            continue
        d_jump = abs((c - cq) - (pc - pcq))
        qfq_pct = (float(cq) / float(pcq) - 1.0) * 100.0
        if d_jump > 0.01 and abs(float(pct) - qfq_pct) > tol_pp:
            updates.append((round(qfq_pct, 4), cd, td))
    if updates:
        conn.executemany(
            "UPDATE daily_bar SET pct_chg=? WHERE code=? AND trade_date=?",
            updates)
        fixed = len(updates)
        log.info("tx pct 重算（除权跳变日 qfq 环比口径）: %d 行%s", fixed,
                 f"（code={code}）" if code else "")
    return fixed, skipped


def rebrush_qfq_full(code: str, conn: sqlite3.Connection) -> tuple:
    """W-B4（P1-11）：单票 qfq 全史整段重刷（close_qfq/high_qfq/low_qfq）。

    - 整段单源：em_qfq 优先（只给收盘，high/low_qfq 按 close 比例同行导出），
      失败整票换 tx_qfq（自带前复权 OHLC）——一票之内绝不混源；
    - 覆盖范围 = 该票 daily_bar 全史（MIN(trade_date) 起），qfq 全历史重锚后
      与 raw 行按日期对齐 UPDATE，fetch_log 留源标记（status='qfq_full_rebrush'）；
    - 返回 (rows_written, source)；两源全挂返回 (0, "")，由调用方重试/汇报。
    """
    row = conn.execute(
        "SELECT MIN(trade_date), MAX(trade_date) FROM daily_bar WHERE code=?",
        (code,)).fetchone()
    if not row or not row[0]:
        return 0, ""
    start8 = str(row[0]).replace("-", "")
    end8 = date.today().strftime("%Y%m%d")
    if _market_data_window():
        end8 = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    df, src = None, ""
    for src_name, fn in (("em_qfq", _hist_em_qfq), ("tx_qfq", _hist_tx_qfq)):
        try:
            got = fn(code, start8, end8)
        except Exception as e:  # noqa: BLE001
            log.info("%s qfq 全史 via %s fail: %s", code, src_name, repr(e)[:80])
            continue
        if got is not None and not got.empty:
            df, src = got, src_name
            break
    if df is None or df.empty:
        log.error("%s qfq 全史重刷：两源均不可用", code)
        return 0, ""
    raw = {d: (o, h, l, c) for d, o, h, l, c in conn.execute(
        "SELECT trade_date, open, high, low, close FROM daily_bar WHERE code=?",
        (code,))}
    rows = []
    for _, r in df.iterrows():
        d = r["date"].strftime("%Y-%m-%d")
        cq = r["close_qfq"]
        if pd.isna(cq) or d not in raw:
            continue
        hq = r.get("high_qfq") if "high_qfq" in df.columns else None
        lq = r.get("low_qfq") if "low_qfq" in df.columns else None
        ro, rh, rl, rc = raw[d]
        if (hq is None or pd.isna(hq)) and rc and rh is not None and not pd.isna(rh):
            hq = float(rh) * float(cq) / float(rc)
        if (lq is None or pd.isna(lq)) and rc and rl is not None and not pd.isna(rl):
            lq = float(rl) * float(cq) / float(rc)
        oq = (round(float(ro) * float(cq) / float(rc), 4)
              if rc and ro is not None and not pd.isna(ro) else None)
        rows.append((round(float(cq), 4),
                     round(float(hq), 4) if hq is not None and not pd.isna(hq) else None,
                     round(float(lq), 4) if lq is not None and not pd.isna(lq) else None,
                     oq,
                     code, d))
    if not rows:
        return 0, ""
    _write_qfq_rows(conn, rows, _qfq_has_open_col(conn))
    conn.execute(
        "INSERT INTO fetch_log VALUES (?,?,?,?,?)",
        (code, datetime.now().isoformat(timespec="seconds"),
         "qfq_full_rebrush", len(rows), f"source={src}"))
    conn.commit()
    log.info("%s qfq 全史重刷 via %s: %d rows", code, src, len(rows))
    return len(rows), src


def backfill_qfq(code: str, conn: sqlite3.Connection) -> int:
    """回填前复权 OHLC 列（close_qfq/high_qfq/low_qfq；除权除息不再污染动量/均线/ATR）。

    东财 em_qfq 优先（仅收盘，high/low_qfq 由 close_qfq/close 比例同行导出），
    腾讯 tx_qfq 兜底（自带前复权 OHLC）。失败静默跳过：因子层自动回退不复权
    close，audit 会提示补跑。

    W-B4（P1-11）：7 天增量窗的结构缺陷——前复权全历史重锚，只回刷 7 天会在
    除权事件后留下永久伪跳变。修复：写窗前先比对重叠行的库内 qfq 与新拉 qfq，
    锚点漂移（某行差 > max(0.01, 0.1%)）即判定发生除权重锚 → 该票自动全史重刷；
    写窗后再对窗边界做一次库内不变量校验（qfq 环比深于 raw 环比 >0.3pp 非法）
    兜底，命中同样触发全史重刷。
    """
    last_qfq = repo.latest_bar_date(conn, code, qfq_only=True)
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

    # 锚点漂移检测：重叠行（库内已有 qfq 且本次也拉到）比值不一致 → 源端重锚
    stored_qfq = {d: cq for d, cq in conn.execute(
        "SELECT trade_date, close_qfq FROM daily_bar WHERE code=? "
        "AND close_qfq IS NOT NULL", (code,))}
    drifted = False
    for _, r in df.iterrows():
        d = r["date"].strftime("%Y-%m-%d")
        cq = r["close_qfq"]
        if pd.isna(cq) or d not in stored_qfq:
            continue
        old = float(stored_qfq[d])
        if old > 0 and abs(float(cq) - old) > max(0.01, old * 0.001):
            drifted = True
            break
    if drifted:
        log.warning("%s qfq 锚点漂移（增量窗内除权重锚），转全史重刷", code)
        n, real_src = rebrush_qfq_full(code, conn)
        if n:
            recalc_tx_pct(conn, code=code)
            conn.commit()
            return n
        log.error("%s 全史重刷失败（两源不可用），本次增量放弃写入（防伪跳变入库）", code)
        return 0

    raw = {d: (o, h, l, c) for d, o, h, l, c in conn.execute(
        "SELECT trade_date, open, high, low, close FROM daily_bar WHERE code=?",
        (code,))}
    rows = []
    for _, r in df.iterrows():
        d = r["date"].strftime("%Y-%m-%d")
        cq = r["close_qfq"]
        if pd.isna(cq) or d not in raw:
            continue
        hq = r.get("high_qfq") if "high_qfq" in df.columns else None
        lq = r.get("low_qfq") if "low_qfq" in df.columns else None
        ro, rh, rl, rc = raw[d]
        # em 源只给收盘：复权是逐行线性缩放，high/low_qfq 按 close 比例同行导出
        # （open_qfq 同一比例——批次0 Gate 0-B 增量一致性）
        if (hq is None or pd.isna(hq)) and rc and rh is not None and not pd.isna(rh):
            hq = float(rh) * float(cq) / float(rc)
        if (lq is None or pd.isna(lq)) and rc and rl is not None and not pd.isna(rl):
            lq = float(rl) * float(cq) / float(rc)
        oq = (round(float(ro) * float(cq) / float(rc), 4)
              if rc and ro is not None and not pd.isna(ro) else None)
        rows.append((round(float(cq), 4),
                     round(float(hq), 4) if hq is not None and not pd.isna(hq) else None,
                     round(float(lq), 4) if lq is not None and not pd.isna(lq) else None,
                     oq,
                     code, d))
    if not rows:
        return 0
    _write_qfq_rows(conn, rows, _qfq_has_open_col(conn))
    # 窗边界不变量校验：任一相邻对 qfq 环比深于 raw 环比 >0.3pp → 伪跳变，全史重刷
    # （含窗前一日：伪跳变恰出现在"旧锚末行 → 新锚首行"的边界对上）
    got_dates = [r[0] for r in conn.execute(
        "SELECT trade_date FROM daily_bar WHERE code=? AND close_qfq IS NOT NULL "
        "AND trade_date >= ? ORDER BY trade_date", (code, start))]
    edge = conn.execute(
        "SELECT MAX(trade_date) FROM daily_bar WHERE code=? AND trade_date < ? "
        "AND close_qfq IS NOT NULL", (code, start)).fetchone()[0]
    if _qfq_invariant_violated(conn, code, ([edge] if edge else []) + got_dates):
        log.warning("%s 增量窗边界伪跳变（qfq 环比深于 raw >0.3pp），转全史重刷", code)
        n, real_src = rebrush_qfq_full(code, conn)
        if n:
            recalc_tx_pct(conn, code=code)
            conn.commit()
            return n
    recalc_tx_pct(conn, code=code)
    conn.commit()
    log.info("%s qfq via %s: %d rows", code, src, len(rows))
    return len(rows)


def _qfq_invariant_violated(conn: sqlite3.Connection, code: str,
                            dates: list, pp: float = 0.3) -> bool:
    """库内不变量（源无关）：同一票相邻交易日，qfq 环比跌幅深于 raw 环比 >pp 个
    百分点 → 前复权序列非法（除权重锚不完整）。dates 为升序待检日期集
    （含窗边界各取前一日）。"""
    if len(dates) < 2:
        return False
    ph = ",".join("?" * len(dates))
    rows = conn.execute(
        f"SELECT trade_date, close, close_qfq FROM daily_bar "
        f"WHERE code=? AND trade_date IN ({ph}) ORDER BY trade_date",
        (code, *dates)).fetchall()
    by_d = {d: (c, cq) for d, c, cq in rows}
    ds = sorted(by_d)
    for i in range(1, len(ds)):
        prev_d, d = ds[i - 1], ds[i]
        pc, pcq = by_d[prev_d]
        c, cq = by_d[d]
        if None in (pc, pcq, c, cq) or pc <= 0 or pcq <= 0:
            continue
        raw_ret = float(c) / float(pc) - 1.0
        qfq_ret = float(cq) / float(pcq) - 1.0
        if qfq_ret < raw_ret - pp / 100.0:
            return True
    return False


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
    # 测试逃生门：短路整个日线采集网络面（调用时读 env，沿用 AGSICKLE_DISABLE_* 模式）
    if os.environ.get("AGSICKLE_DISABLE_FETCHER") == "1":
        log.info("AGSICKLE_DISABLE_FETCHER=1，跳过日线采集")
        return
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
            # W-B9（P2-11）：rows=0 且本轮已写 empty_today → 不再补矛盾的 ok 行
            if not (n == 0 and code in _EMPTY_TODAY_WRITTEN):
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
