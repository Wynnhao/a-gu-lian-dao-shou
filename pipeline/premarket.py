"""盘前流水线（9:00 触发）：行情->资讯->估值->体检->T+1解锁->信号->决策输入包，失败降级不中断。"""
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import argparse
import logging
from datetime import date, datetime

from data import fetcher, news
from data import macro as macro_mod
from risk.blacklist import check_blacklist, health_check
from signals.signals import compute_all
from ai import bundle as ai_bundle

log = logging.getLogger("pipeline.premarket")
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
    ap = argparse.ArgumentParser(description="盘前流水线：数据准备 + 决策输入包落盘")
    ap.add_argument("--date", default=None, dest="run_date",
                    help="运行日期 YYYY-MM-DD（默认 daily_bar 最新交易日）")
    args = ap.parse_args(argv)
    log.info("==== premarket start ====")

    # 1. 增量行情
    try:
        fetcher.run()
    except Exception as e:
        log.error("步骤1 fetcher.run FAIL（继续）: %s", repr(e))

    conn = fetcher.get_conn()
    try:
        latest = conn.execute("SELECT MAX(trade_date) FROM daily_bar").fetchone()[0]
        if not latest:
            msg = "行情全失败：daily_bar 无任何数据，放弃盘前流程"
            log.error(msg)
            print("[premarket] " + msg)
            return 1
        run_date = args.run_date or latest

        # 2. 资讯
        try:
            news_stats = news.fetch_all()
            log.info("步骤2 资讯完成: %s", news_stats)
        except Exception as e:
            news_stats = {}
            log.error("步骤2 news.fetch_all FAIL（继续）: %s", repr(e))

        # 3. 指数估值：最新日期距今 >3 天才刷新（省时）
        try:
            row = conn.execute("SELECT MAX(trade_date) FROM index_valuation").fetchone()[0]
            lag = ((date.today() - datetime.strptime(str(row)[:10], "%Y-%m-%d").date()).days
                   if row else 9999)
            if lag > 3:
                log.info("步骤3 index_valuation 最新 %s 滞后 %d 天 >3，刷新估值", row, lag)
                macro_mod.fetch_index_valuation()
            else:
                log.info("步骤3 index_valuation 最新 %s（滞后 %d 天 ≤3），跳过", row, lag)
        except Exception as e:
            log.error("步骤3 估值刷新 FAIL（继续）: %s", repr(e))

        # 4. 黑名单 + 数据健康
        try:
            bl = check_blacklist(conn)
        except Exception as e:
            bl = {}
            log.error("步骤4 check_blacklist FAIL（继续）: %s", repr(e))
        try:
            issues = health_check(conn)
        except Exception as e:
            issues = ["health_check 自身失败: %s" % repr(e)]
            log.error("步骤4 health_check FAIL（继续）: %s", repr(e))
        if issues:
            log.warning("【今日只出报告不下单】数据健康异常: %s", "；".join(issues))

        # 5. T+1 解锁：隔夜后昨日买入全部可卖
        try:
            cur = conn.execute("UPDATE position SET avail_shares=shares")
            conn.commit()
            log.info("步骤5 T+1 解锁完成，更新 %d 行", cur.rowcount if cur.rowcount else 0)
        except Exception as e:
            log.error("步骤5 T+1 解锁 FAIL（继续）: %s", repr(e))

        # 6. 信号
        try:
            sigs = compute_all()
            log.info("步骤6 信号计算完成: %d 票", len(sigs))
        except Exception as e:
            sigs = []
            log.error("步骤6 signals.compute_all FAIL（继续）: %s", repr(e))

        # 6.5 动态池（异动/热门）：供输入包与看板，LLM 可对池内票输出 watch 观察
        try:
            from signals import movers, hot
            m = movers.refresh(conn)
            h = hot.refresh(conn)
            log.info("步骤6.5 动态池：异动 %d 只（%s口径）、热门题材 %d/个股 %d",
                     m["count"], m["mode"], len(h["themes"]), len(h["stocks"]))
        except Exception as e:
            log.error("步骤6.5 动态池 FAIL（继续）: %s", repr(e))

        # 7. 决策输入包
        try:
            json_path, md_path = ai_bundle.write_bundle(run_date)
        except Exception as e:
            log.error("步骤7 write_bundle FAIL: %s", repr(e))
            print("[premarket] 决策输入包落盘失败：%r" % e)
            return 1
    finally:
        conn.close()

    # 8. 总结
    print("[premarket] ===== 盘前流程完成 run_date=%s =====" % run_date)
    print("[premarket] 数据状态: 最新交易日=%s" % latest)
    print("[premarket] 资讯新增: " + (", ".join(f"{k}: +{v}" for k, v in news_stats.items())
                                       if news_stats else "（本轮无统计）"))
    print("[premarket] 黑名单:")
    for code in sorted(bl):
        ok, reason = bl[code]
        print("    %s %s (%s)" % (code, "PASS" if ok else "BLOCK", reason))
    print("[premarket] 数据健康: " + ("；".join(issues) if issues else "OK"))
    if issues:
        print("[premarket] >>> 【今日只出报告不下单】<<<")
    print("[premarket] 信号摘要（%d 票）:" % len(sigs))
    print("    %-8s %-6s %-8s %-8s %-10s %s" % ("code", "trend", "mom20d", "rsi14", "close", "score"))
    for s in sigs:
        g = s.get("signals") or {}
        print("    %-8s %-6s %-8s %-8s %-10s %.3f" % (
            s.get("code"), g.get("ma_trend"),
            "n/a" if g.get("mom_20d") is None else f"{g['mom_20d']:+.3f}",
            "n/a" if g.get("rsi_14") is None else f"{g['rsi_14']:.1f}",
            "n/a" if g.get("close") is None else f"{g['close']:.2f}",
            s.get("score", 0.0)))
    print("[premarket] 决策输入包:")
    print("    %s" % json_path)
    print("    %s" % md_path)
    log.info("==== premarket done exit=0 ====")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
