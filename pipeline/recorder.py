"""盘中分钟快照录制器（C-ARC-3b/T6）：每 5 分钟对自选池+指数做一次批量快照落库。

借鉴 CloddsBot run-tick-recorder（架构层借鉴 C-ARC-3，docs/架构借鉴-cloddsbot-2026-09-19.md）：
日线数据无法回答「止损线盘中何时触到、触后怎么走」类问题；尾盘 14:50 决策、
止损触线与 limit_halt 应急策略的回测校准需要盘中时点数据。先攒数据与缺测率
观察（连续 5 交易日 <5%，fetch_log 口径），回测接入不在本 sprint 范围。

- 双 gate：trade_cal.is_trading_day + common.market.in_trading_session，
  节假日/周末/午休/收盘直接秒退（launchd StartInterval=300 常驻触发）；
- 取数走 quotes.fetch_snapshot（T5：显式指数符号表+量纲+UA+独立冷却），
  本模块不自写网络；
- minute_snapshot 幂等：INSERT OR REPLACE (code, ts 5分钟栅格)，
  launchd 合并触发/手动补跑无冲突；
- 整轮失败 fail-loud 落 fetch_log（status='fail'，照 empty_today 风格）；
- 心跳文件供看门狗体检（照 catchup 心跳模式）；
- 红线：绝不写 daily_bar（盘中部分 bar 会污染日线历史）。

退出码：0 正常/非交易时段跳过；1 录制失败。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import argparse
import json
import logging
import logging.handlers
import os
from datetime import datetime
from typing import List, Optional, Tuple

from common.config import snapshot as _cfg_snapshot          # noqa: E402
from common.market import in_trading_session                  # noqa: E402
from data import fetcher, quotes, repo                        # noqa: E402
from data.trade_cal import is_trading_day                     # noqa: E402

log = logging.getLogger("pipeline.recorder")
log.setLevel(logging.INFO)
if not log.handlers:
    # AGSICKLE_LOG_DIR 逃生门（批次0 W0-1 同模式，Sprint4 落地核验②）：
    # 测试/隔离环境把日志导出去，绝不写生产 logs/
    _LOG_DIR = Path(os.environ.get("AGSICKLE_LOG_DIR") or (BASE / "logs"))
    _fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    _fh = logging.handlers.RotatingFileHandler(_LOG_DIR / "pipeline.log",
                                               encoding="utf-8",
                                               maxBytes=5_000_000, backupCount=3)
    _fh.setFormatter(_fmt)
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    log.addHandler(_fh)
    log.addHandler(_sh)
log.propagate = False

LOG_TAG = "[recorder]"

# 缺省录制的宽基指数（与 fetcher 指数日线口径一致）；可用 config.recorder.index_codes 覆盖
DEFAULT_INDEX_CODES = ["000001", "000300", "000905"]   # 上证指数 / 沪深300 / 中证500


def _state_dir() -> Path:
    """调用时读 env（测试隔离，与 runner.STATE_DIR 同模式）。"""
    return Path(os.environ.get("AGSICKLE_STATE_DIR") or (BASE / "logs" / "state"))


def _heartbeat() -> None:
    try:
        _state_dir().mkdir(parents=True, exist_ok=True)
        (_state_dir() / "recorder_heartbeat").touch()
    except OSError:
        pass


def _index_codes() -> List[str]:
    try:
        return list(_cfg_snapshot().get("recorder", {}).get("index_codes")
                    or DEFAULT_INDEX_CODES)
    except Exception:  # noqa: BLE001  — 配置读取失败回退缺省（容错分级保留）
        return list(DEFAULT_INDEX_CODES)


def grid_ts(now: datetime) -> str:
    """5 分钟栅格 ISO 串：向下取整到 5 分钟桶（12:07:43 → ...T12:05:00）。"""
    return now.replace(minute=(now.minute // 5) * 5, second=0,
                       microsecond=0).isoformat(timespec="seconds")


def record_once(conn, now: datetime) -> Tuple[int, int, int]:
    """录一轮快照：取数 → 批量 upsert → fetch_log 留痕。返回 (请求数, 入库数, 缺测数)。"""
    codes = [str(c) for c in repo.all_codes(conn)]
    idx = _index_codes()
    wanted = len(set(codes)) + len(idx)
    ts = grid_ts(now)
    try:
        snap = quotes.fetch_snapshot(codes, index_codes=idx)
    except Exception as e:  # noqa: BLE001
        log.error("录制快照 FAIL: %s", repr(e)[:200])
        fetcher_log_row(conn, "fail", 0, ts, str(e)[:200])
        raise
    rows = [(str(code), ts, q.get("price"), q.get("volume"), q.get("amount"),
             q.get("source")) for code, q in (snap or {}).items()]
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO minute_snapshot (code, ts, price, volume,"
            " amount, source) VALUES (?,?,?,?,?,?)", rows)
        conn.commit()
    miss = wanted - len(rows)
    fetcher_log_row(conn, "ok" if rows else "fail", len(rows), ts,
                    json.dumps({"total": wanted, "miss": miss, "ts": ts},
                               ensure_ascii=False))
    return wanted, len(rows), miss


def fetcher_log_row(conn, status: str, n: int, ts: str, detail: str) -> None:
    """fetch_log 留痕（code=minute_snapshot 市场级行；整轮失败 fail-loud 不静默）。"""
    try:
        conn.execute("INSERT INTO fetch_log VALUES (?,?,?,?,?)",
                     ("minute_snapshot", datetime.now().isoformat(timespec="seconds"),
                      status, n, detail))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("fetch_log 写盘失败: %s", repr(e)[:120])


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="盘中分钟快照录制器（5 分钟栅格）")
    ap.add_argument("--now", default=None, help="覆盖当前时间 ISO（回放/测试）")
    args = ap.parse_args(argv)
    now = datetime.fromisoformat(args.now) if args.now else datetime.now()

    conn = fetcher.get_conn()
    try:
        # 双 gate：交易日 + 连续竞价时段（节假日/午休/收盘秒退，不算失败）
        if not is_trading_day(conn, now.date()):
            print("%s %s 非交易日，跳过" % (LOG_TAG, now.date()))
            return 0
        if not in_trading_session(now):
            print("%s %s 非连续竞价时段，跳过" % (LOG_TAG, now.strftime("%H:%M")))
            return 0
        _heartbeat()
        wanted, n, miss = record_once(conn, now)
        if wanted and not n:
            log.error("录制快照整轮失败：请求 %d、入库 0（fetch_log 已留痕，"
                      "缺测率验收受影响）", wanted)
            return 1
        print("%s %s 入库 %d/%d（缺测 %d）" % (LOG_TAG, grid_ts(now), n, wanted, miss))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
