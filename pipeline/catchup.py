"""兜底补跑器：机器漏开时错过的定时任务，在下次开机/唤醒后自动补齐。

触发方式（二选一或并存）：
1. launchd 看门狗（deploy/com.agsickle.catchup.plist，StartInterval=1800）——
   macOS launchd 语义：错过的 StartInterval 触发会在唤醒后合并执行一次，
   这正是"没开机"场景的兜底入口（cron/ZCode cron 均为纯跳过）。
2. 手动/任意会话运行：python3 pipeline/catchup.py（幂等，随时可跑）。

补跑内容（全部确定性脚本，不含 LLM 决策）：
- 增量行情（交易时段自动防盘中部分bar，见 fetcher P1-1 防护）；
- 历史缺失日的盯市 + 日报（+周五周报）——纯 DB/文件操作，可完全自动；
- 盘中兜底（交易日 9:00-15:00）：盘前 bundle 过期则重跑盘前流水线；
  11:00 后午间包缺失则重跑午评准备；有持仓则跑尾盘级 kill 安全网扫描；
- 动态池刷新（锚点滞后于最新交易日时）。

LLM 决策缺失的安全语义：当天没有决策 = 当天不交易（fail-safe），不自动伪造。
盘中补决策路径：补跑器把 bundle 备好后，由用户在任意 ZCode 会话说"补今天的决策"
（读 bundle → decide.py --date → propose-db → 人工闸门），与正常流程同一链路。

退出码：0=无可补或已补齐；1=部分补跑失败（详见输出）；2=盘中触发过 kill。
"""
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from data import fetcher                       # noqa: E402
from review import daily, weekly               # noqa: E402

REPORTS_DIR = BASE / "logs" / "reports"
SESSION_DIR = BASE / "logs" / "session"
LOG_TAG = "[catchup]"


def _say(msg: str) -> None:
    print("%s %s" % (LOG_TAG, msg))


def _latest_trade_date(conn) -> Optional[str]:
    row = conn.execute("SELECT MAX(trade_date) FROM daily_bar").fetchone()
    return row[0] if row and row[0] else None


def _recent_trade_dates(conn, n: int = 10) -> List[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM daily_bar ORDER BY trade_date DESC LIMIT ?",
        (n,)).fetchall()]


def _is_weekday(d: date) -> bool:
    return d.weekday() < 5


def _run_script(rel: str, timeout: int = 900, extra: Optional[List[str]] = None) -> bool:
    """运行项目内脚本（子进程，继承配置与环境），返回是否成功。"""
    cmd = [sys.executable, str(BASE / rel)] + (extra or [])
    try:
        proc = subprocess.run(cmd, cwd=str(BASE), capture_output=True,
                              text=True, timeout=timeout)
        ok = proc.returncode == 0
        tail = (proc.stdout or "").strip().splitlines()[-3:]
        _say("  ↳ %s %s%s" % (rel, "OK" if ok else "FAIL(rc=%d)" % proc.returncode,
                              ("：" + " / ".join(tail)) if not ok else ""))
        return ok
    except subprocess.TimeoutExpired:
        _say("  ↳ %s 超时(%ds)" % (rel, timeout))
        return False
    except Exception as e:  # noqa: BLE001
        _say("  ↳ %s 异常: %r" % (rel, e))
        return False


def _file_fresh_today(p: Path) -> bool:
    return p.is_file() and datetime.fromtimestamp(p.stat().st_mtime).date() == date.today()


def catch_up(now: Optional[datetime] = None) -> int:
    now = now or datetime.now()
    today = now.date()
    today_str = today.isoformat()
    failures = 0
    killed = False
    weekday = _is_weekday(today)
    in_trading_window = weekday and (now.hour, now.minute) >= (8, 30) \
        and (now.hour, now.minute) <= (15, 30)

    _say("==== 兜底补跑开始 %s（交易日=%s 盘中窗口=%s）===="
         % (now.strftime("%Y-%m-%d %H:%M:%S"), "是" if weekday else "否",
            "是" if in_trading_window else "否"))

    conn = fetcher.get_conn()
    try:
        latest_td = _latest_trade_date(conn)
        tds = _recent_trade_dates(conn)

        # ---- 0) 行情增量（仅交易日白天；其余时点数据不会更新）----
        if in_trading_window:
            _say("步骤0 增量行情（盘中自动防部分bar）")
            if not _run_script("data/fetcher.py", timeout=600):
                failures += 1
            latest_td = _latest_trade_date(conn)

        # ---- 1) 历史缺失日：盯市 + 日报（+周五周报）----
        for td in tds:
            if td >= today_str:
                continue
            has_state = conn.execute(
                "SELECT 1 FROM portfolio_state WHERE date=?", (td,)).fetchone()
            report = REPORTS_DIR / (td + ".md")
            if has_state and report.is_file():
                continue
            _say("步骤1 补跑 %s 的盯市/日报（盘后任务缺失）" % td)
            try:
                daily.mark_to_market(td)
                daily.generate_daily_report(td)
                w = date.fromisoformat(td)
                if w.weekday() == 4 and not (REPORTS_DIR / (
                        "%d-W%02d.md" % (w.isocalendar()[0], w.isocalendar()[1]))).is_file():
                    weekly.weekly_report(td)
            except Exception as e:  # noqa: BLE001
                failures += 1
                _say("  ↳ 补跑 %s 失败: %r" % (td, e))

        # ---- 2) 盘中兜底（仅交易日盘中窗口）----
        if in_trading_window and latest_td:
            # 2a) 盘前 bundle 过期（当日未跑盘前）→ 重跑盘前流水线（不含 LLM 决策）
            bundle = SESSION_DIR / latest_td / "bundle.md"
            if not _file_fresh_today(bundle):
                _say("步骤2a 当日盘前流程缺失，补跑（LLM 决策需会话补做）")
                if not _run_script("pipeline/premarket.py", timeout=900):
                    failures += 1
            # 2b) 午间包缺失（11:00 后）→ 补午评准备
            midday_bundle = SESSION_DIR / latest_td / "midday_bundle.md"
            if now.hour >= 11 and not _file_fresh_today(midday_bundle):
                _say("步骤2b 当日午间包缺失，补跑午评准备")
                if not _run_script("pipeline/midday.py", timeout=600):
                    failures += 1
            # 2c) 有持仓 → kill 安全网扫描（触发即清仓+停机，退出码2）
            pos_n = conn.execute("SELECT COUNT(*) FROM position").fetchone()[0]
            if pos_n:
                _say("步骤2c 持仓 %d 只 → 尾盘级风控安全网扫描" % pos_n)
                proc = subprocess.run(
                    [sys.executable, str(BASE / "pipeline/intraday_check.py")],
                    cwd=str(BASE), capture_output=True, text=True, timeout=600)
                if proc.returncode == 2:
                    killed = True
                    _say("  ↳ ⚠ kill switch 已触发（清仓+停机），详见输出/报告")
                elif proc.returncode != 0:
                    failures += 1
                    _say("  ↳ 扫描失败 rc=%d" % proc.returncode)

        # ---- 3) 动态池锚点滞后 → 刷新 ----
        if latest_td:
            stale = conn.execute(
                "SELECT COUNT(DISTINCT pool) FROM dynamic_pool WHERE added_date=?",
                (latest_td,)).fetchone()[0] < 2
            if stale:
                _say("步骤3 动态池锚点滞后（最新 %s），刷新" % latest_td)
                try:
                    from signals import movers, hot
                    m = movers.refresh(conn)
                    h = hot.refresh(conn)
                    _say("  ↳ 异动 %d / 题材 %d / 热门股 %d"
                         % (m["count"], len(h["themes"]), len(h["stocks"])))
                except Exception as e:  # noqa: BLE001
                    failures += 1
                    _say("  ↳ 动态池刷新失败: %r" % e)
    finally:
        conn.close()

    _say("==== 补跑结束：%s ====" % ("有失败项 %d" % failures if failures else "全部正常"))
    if killed:
        print("%s ⚠⚠ kill switch 已在本次补跑中触发，请立即查看看板/日报 ⚠⚠" % LOG_TAG)
    return 2 if killed else (1 if failures else 0)


if __name__ == "__main__":
    raise SystemExit(catch_up())
