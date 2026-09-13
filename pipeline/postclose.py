"""盘后流水线（15:30 触发）：补齐当日日线 -> 盯市 -> 每日复盘报告（周五或 --weekly 加跑周报）。"""
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import argparse
import logging
from datetime import date
from typing import Optional

from data import fetcher
from review import daily, weekly

log = logging.getLogger("pipeline.postclose")
log.setLevel(logging.INFO)
if not log.handlers:
    _fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    _fh = logging.FileHandler(BASE / "logs" / "pipeline.log", encoding="utf-8")
    _fh.setFormatter(_fmt)
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    log.addHandler(_fh)
    log.addHandler(_sh)
log.propagate = False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="盘后流水线：盯市 + 每日复盘报告（周五/指定时含周报）")
    ap.add_argument("--date", default=None, dest="trade_date",
                    help="交易日 YYYY-MM-DD（默认 daily_bar 最新交易日）")
    ap.add_argument("--weekly", action="store_true", help="强制加跑周度复盘报告")
    args = ap.parse_args(argv)
    log.info("==== postclose start ====")

    # 1. 补齐当日日线
    try:
        fetcher.run()
    except Exception as e:
        log.error("步骤1 fetcher.run FAIL（继续用已有数据复盘）: %s", repr(e))

    # 1.5 动态池刷新（收盘正式口径：异动规则基于当日完整日线）
    try:
        conn0 = fetcher.get_conn()
        try:
            from signals import movers, hot
            m = movers.refresh(conn0)
            h = hot.refresh(conn0)
            log.info("步骤1.5 动态池：异动 %d 只（%s口径）、热门题材 %d/个股 %d",
                     m["count"], m["mode"], len(h["themes"]), len(h["stocks"]))
        finally:
            conn0.close()
    except Exception as e:
        log.error("步骤1.5 动态池 FAIL（继续）: %s", repr(e))

    # 2. 盯市 + 每日复盘报告
    conn = fetcher.get_conn()
    try:
        trade_date = args.trade_date or daily.latest_trade_date(conn)
    finally:
        conn.close()

    try:
        st = daily.mark_to_market(trade_date)
        log.info("步骤2 盯市完成 %s: total=%.2f drawdown=%.2f%% kill_switch=%s",
                 st["trade_date"], st["total"], st["drawdown"] * 100, st["kill_switch"])
    except Exception as e:
        st = {}
        log.error("步骤2 mark_to_market FAIL: %s", repr(e))
    try:
        report_path = daily.generate_daily_report(trade_date)
    except Exception as e:
        log.error("步骤2 generate_daily_report FAIL: %s", repr(e))
        print("[postclose] 每日报告生成失败：%r" % e)
        return 1

    # 3. 周五（或 --weekly）加跑周报
    weekly_path: Optional[Path] = None
    if date.today().weekday() == 4 or args.weekly:
        try:
            weekly_path = weekly.weekly_report(trade_date)
            log.info("步骤3 周报完成: %s", weekly_path)
        except Exception as e:
            log.error("步骤3 weekly_report FAIL（不影响日报）: %s", repr(e))
    else:
        log.info("步骤3 今天非周五且未指定 --weekly，跳过周报")

    # 4. 输出
    print("[postclose] ===== 盘后流程完成 trade_date=%s =====" % trade_date)
    if st:
        print("[postclose] 盯市: 现金=%.2f 市值=%.2f 总资产=%.2f 回撤=%.2f%% kill_switch=%s"
              % (st.get("cash", 0.0), st.get("market_value", 0.0), st.get("total", 0.0),
                 st.get("drawdown", 0.0) * 100, st.get("kill_switch", 0)))
    print("[postclose] 每日报告: %s" % report_path)
    if weekly_path is not None:
        print("[postclose] 周度报告: %s" % weekly_path)
    log.info("==== postclose done exit=0 ====")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
