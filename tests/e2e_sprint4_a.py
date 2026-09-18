"""Sprint4 批次A 端到端验收脚本（docs/Sprint4-全库审查修复计划-2026-09-19.md 批次A验收门）。

三场景（全沙箱：AGSICKLE_DB=临时库、LOGS/ORDERS/STATE/REPORTS/SESSION 全部临时目录；
绝不触生产库与生产 logs；只走 runner.propose/confirm/resolve_liquidations 与
limit_halt 入口函数——**绝不直接调用 broker.buy/sell**，C-ARC T1 守护测试纪律）：

a) 多持仓 kill 全清：建两持仓 → 回撤超 8% → propose 触发 kill → 两笔 trade 落账
   （decision_id 均 NULL，W-A1）+ kill.json 含 dd_base（W-A3）→ resume → 再 check
   不触发（分段 8% 死锁解除）；
b) 真死封应急全链：按 2026-09-19 实测协议造跌停快照（W-A4 口径字段）→ 盘后扫描 →
   应急单生成（run_date=次日）→（模拟 T 晚）confirm 成交（W-A5② 跨日闸门放行 +
   W-A9 显式价确认）；
c) failsafe 09:14：T 晚生成应急单不 confirm → 次日 09:14 premarket_failsafe
   (do_exec=True) 自动执行成交（W-A5④ 链路）。

另断言：生产 config.json 的 execution.emergency_direct_exec=false（验收门第 4 条）。

用法：AGSICKLE_LOG_DIR=<dir> .venv/bin/python3 tests/e2e_sprint4_a.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

# ---------------------------------------------------------------- 沙箱（import 项目模块前生效）
SANDBOX = Path(tempfile.mkdtemp(prefix="e2e_sprint4_a_"))
os.environ["AGSICKLE_DB"] = str(SANDBOX / "e2e.db")
os.environ["AGSICKLE_LOG_DIR"] = str(SANDBOX / "logs")
os.environ["AGSICKLE_STATE_DIR"] = str(SANDBOX / "state")
os.environ["AGSICKLE_ORDERS_DIR"] = str(SANDBOX / "state")   # 执行锁同目录隔离
os.environ["AGSICKLE_REPORTS_DIR"] = str(SANDBOX / "reports")
os.environ["AGSICKLE_SESSION_DIR"] = str(SANDBOX / "session")
os.environ["AGSICKLE_DISABLE_NOTIFY"] = "1"
os.environ["AGSICKLE_DISABLE_SLIPPAGE"] = "1"
os.environ["AGSICKLE_DISABLE_LIVE_QUOTES"] = "1"   # 默认离线；场景 b 临时换 mock 快照
for _d in ("logs", "state", "reports", "session"):
    (SANDBOX / _d).mkdir(parents=True, exist_ok=True)

from data.fetcher import DDL, init_db          # noqa: E402
from execution import runner                   # noqa: E402
from execution.paper import PaperBroker        # noqa: E402
from signals import limit_halt                 # noqa: E402
from risk.engine import check                  # noqa: E402

# 人造"当日"时钟：工作日用今天、周末回退最近周五（与 test_execution 同款）
_BASE = date.today() if date.today().weekday() < 5 else \
    date.today() - timedelta(days=date.today().weekday() - 4)
T = _BASE                                        # 场景 b/c 的 T（生成日）
T1 = _BASE + timedelta(days=1)                   # 预期执行日
NOW_DATE = T.isoformat()
NEXT_DATE = T1.isoformat()

SCENARIOS = []


def scenario(fn):
    SCENARIOS.append(fn)
    return fn


def fresh_db(tag: str) -> sqlite3.Connection:
    """场景独立临时文件库（AGSICKLE_DB 调用时读，改 env 即生效）。"""
    d = SANDBOX / ("db_" + tag)
    d.mkdir(exist_ok=True)
    os.environ["AGSICKLE_DB"] = str(d / "e2e.db")
    conn = sqlite3.connect(os.environ["AGSICKLE_DB"])
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def seed_market(conn, prices: dict) -> None:
    """stock_info + daily_bar（昨日/今日两根）。prices: {code: (今收, 昨收)}。"""
    for code, (last, prev) in prices.items():
        conn.execute("INSERT OR REPLACE INTO stock_info VALUES (?,?,?,?)",
                     (code, "票%s" % code, "2024-01-02", "x"))
        for td, cl in ((YDAY, prev), (NOW_DATE, last)):
            conn.execute(
                "INSERT OR REPLACE INTO daily_bar (code, trade_date, open, high, low,"
                " close, volume, amount, pct_chg, turnover) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (code, td, cl, cl, cl, cl, 10_000_000, cl * 10_000_000, 0.0, 1.0))
    conn.commit()


YDAY = (T - timedelta(days=1)).isoformat()


def add_position(conn, code, name, shares, cost, avail) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO position (code, name, shares, avail_shares, cost,"
        " updated_at) VALUES (?,?,?,?,?,?)",
        (code, name, shares, avail, cost,
         datetime.now().isoformat(timespec="seconds")))
    conn.commit()


def reset_kill_state() -> None:
    if runner.KILL_STATE_FILE.exists():
        runner.KILL_STATE_FILE.unlink()


def set_mock_quotes(quotes: dict) -> Path:
    """写 mock 快照文件并临时启用（AGSICKLE_MOCK_QUOTES 调用时读）。"""
    f = SANDBOX / ("mock_quotes_%s.json" % datetime.now().strftime("%H%M%S%f"))
    f.write_text(json.dumps(quotes, ensure_ascii=False), encoding="utf-8")
    os.environ["AGSICKLE_MOCK_QUOTES"] = str(f)
    os.environ.pop("AGSICKLE_DISABLE_LIVE_QUOTES", None)
    return f


def clear_mock_quotes(saved) -> None:
    os.environ.pop("AGSICKLE_MOCK_QUOTES", None)
    os.environ["AGSICKLE_DISABLE_LIVE_QUOTES"] = "1"


# ---------------------------------------------------------------- 场景 a

@scenario
def scenario_a_multi_position_kill_full_clear() -> None:
    """多持仓 kill 全清：两笔 trade decision_id 均 NULL + kill.json dd_base →
    resume → 再 check 不触发。"""
    reset_kill_state()
    conn = fresh_db("a")
    seed_market(conn, {"600519": (1290.0, 1280.0), "000001": (11.0, 10.9)})
    add_position(conn, "600519", "贵州茅台", 1000, 1300.0, avail=1000)
    add_position(conn, "000001", "平安银行", 2000, 12.0, avail=2000)
    peak_date = YDAY
    conn.execute("INSERT INTO portfolio_state VALUES (?,?,?,?,?,?,?)",
                 (peak_date, 0.0, 0.0, 2600000.0, 0.0, 0, "seed peak"))
    conn.commit()

    now = datetime.combine(T, dtime(10, 0))
    hold = {"action": "hold", "code": "600519", "target_weight": 0.0,
            "confidence": 0.9, "reasons": ["e2e r1", "e2e r2"], "risk_notes": []}
    v = runner.propose(conn, hold, now=now)
    assert v.kill_trigger, "回撤 11%% 必须触发 kill：%s" % v.brief()

    # 两笔 trade 各自落账：decision_id 均 NULL（W-A1 绕唯一索引）、kill_switch 留痕
    rows = conn.execute(
        "SELECT code, side, shares, decision_id, confirmed_by FROM trade"
        " WHERE side='sell' ORDER BY code").fetchall()
    assert len(rows) == 2, "两持仓必须全部清仓（P0-4），实得 %d 笔" % len(rows)
    assert {r[0] for r in rows} == {"600519", "000001"}
    for r in rows:
        assert r[3] is None, "kill 清仓 trade.decision_id 必须为 NULL（W-A1）"
        assert r[4] == "kill_switch"
    assert conn.execute("SELECT COUNT(*) FROM position").fetchone()[0] == 0

    # kill.json：dd_base（清仓后权益）+ dd_base_date + 累计观测键（W-A3③④）
    ks = runner.read_kill_state()
    assert ks and ks.get("active") and ks.get("until")
    assert ks.get("dd_base") is not None, "kill.json 必须含 dd_base"
    assert ks.get("dd_base_date") == NOW_DATE
    assert ks.get("kill_count") == 1
    assert ks.get("lifetime_peak") == 2600000.0
    n_reset = conn.execute("SELECT COUNT(*) FROM risk_event WHERE"
                           " rule='kill_dd_base_reset'").fetchone()[0]
    assert n_reset == 1

    # resume → 分段 8%：回撤归零，再 check 不触发
    runner.kill_resume(conn, "e2e 验收恢复")
    ctx = runner.build_context(conn, now)
    assert ctx.kill_switch_until is None
    dd = 1 - ctx.total_equity / ctx.peak_equity if ctx.peak_equity else 0.0
    assert dd < 0.08, "dd_base 重置后回撤必须 <8%%（实得 %.2f%%）" % (dd * 100)
    v2 = check(hold, ctx, runner.CFG["risk"])
    assert not v2.kill_trigger, "resume 后再 check 不得重新 kill（P1-1 死锁解除）"
    conn.close()
    print("  [a] 多持仓 kill 全清 + dd_base 重置 + resume 不复触 —— PASS")


# ---------------------------------------------------------------- 场景 b

# 按实测协议（2026-09-19 GET qt.gtimg.cn 三票核验）构造的跌停快照：
# ask1=f[19]/f[20]、float_mv=f[44]×1e8、limit=f[47]/f[48]
DEAD_SEAL_QUOTE = {
    "600519": {"price": 90.0, "prev_close": 100.0, "open": 90.0, "high": 90.0,
               "low": 90.0, "time": T.strftime("%Y%m%d") + "150000",
               "name": "贵州茅台", "source": "tencent",
               "ask1_price": 90.0, "ask1_vol": 5000.0,
               "float_mv": 1.5e8,
               "limit_up": 110.0, "limit_down": 90.0},
}


@scenario
def scenario_b_dead_seal_emergency_full_chain() -> None:
    """真死封应急全链：实测协议跌停快照 → 扫描生成应急单（run_date=次日）→
    （模拟 T 晚）confirm 成交。"""
    reset_kill_state()
    conn = fresh_db("b")
    seed_market(conn, {"600519": (90.0, 100.0)})    # T 收盘=跌停
    add_position(conn, "600519", "贵州茅台", 200, 110.0, avail=200)

    qf = set_mock_quotes(DEAD_SEAL_QUOTE)
    try:
        now_t = datetime.combine(T, dtime(15, 40))          # T 晚盘后扫描
        r = limit_halt.run_postclose_scan(conn, now=now_t)
        assert r["scanned"] == 1 and r["proposed"] == ["600519"], r
        row = conn.execute(
            "SELECT run_date, code, action, status, emergency_scan FROM decision"
            " WHERE emergency_scan=1 ORDER BY id DESC LIMIT 1").fetchone()
        assert row[0] == NEXT_DATE and row[1] == "600519" and row[2] == "sell"
        assert row[3] == "approved" and int(row[4]) == 1
        did = int(conn.execute("SELECT MAX(id) FROM decision").fetchone()[0])
        assert runner.list_pending(), "应急单必须落 pending 等确认"
        ev = conn.execute(
            "SELECT detail FROM risk_event WHERE rule='limit_halt_emergency'"
            " ORDER BY id DESC LIMIT 1").fetchone()
        assert ev and "600519" in ev[0], "规则21 事件必须留痕"

        # （模拟 T 晚 22:00）人工睡前 confirm——W-A5②：跨日闸门对 emergency_scan
        # 且 run_date≥今日 放行；TTL 按 run_date 当日 15:05（次日）不拦截；
        # W-A9：显式 --price 确认成交价（离线无实时价，不自动以昨收成交）
        now_night = datetime.combine(T, dtime(22, 0))
        res = runner.confirm(conn, did, confirmed_by="睡前人工",
                             price_override=90.0, now=now_night)
        assert res is not None and res["ok"], "T 晚 confirm 必须可成交（P1-4 主路径）"
        tr = conn.execute(
            "SELECT price, shares, confirmed_by, decision_id FROM trade"
            " WHERE decision_id=?", (did,)).fetchone()
        assert tr is not None and float(tr[0]) == 90.0 and int(tr[1]) == 200
        assert tr[2] == "睡前人工"
        assert conn.execute("SELECT status FROM decision WHERE id=?",
                            (did,)).fetchone()[0] == "executed"
        n_cross = conn.execute("SELECT COUNT(*) FROM risk_event WHERE"
                               " rule='pending_crossday_allowed'").fetchone()[0]
        assert n_cross >= 1, "跨日放行必须留痕"
        assert runner.list_pending() == [], "成交后 pending 必须清理"
    finally:
        clear_mock_quotes(None)
        conn.close()
    print("  [b] 真死封应急全链（扫描→生成→T 晚 confirm 成交） —— PASS")


# ---------------------------------------------------------------- 场景 c

@scenario
def scenario_c_failsafe_0914_auto_exec() -> None:
    """failsafe 09:14：T 晚生成应急单不 confirm → 次日 09:14
    premarket_failsafe(do_exec=True) 自动执行成交。"""
    reset_kill_state()
    conn = fresh_db("c")
    seed_market(conn, {"600519": (90.0, 100.0)})
    add_position(conn, "600519", "贵州茅台", 200, 110.0, avail=200)

    qf = set_mock_quotes(DEAD_SEAL_QUOTE)
    try:
        now_t = datetime.combine(T, dtime(15, 40))
        r = limit_halt.run_postclose_scan(conn, now=now_t)
        assert r["proposed"] == ["600519"], r
        did = int(conn.execute("SELECT MAX(id) FROM decision").fetchone()[0])
    finally:
        clear_mock_quotes(None)

    # 次日 09:14 未 confirm → failsafe 自动执行（emergency_direct_exec 运行时临时
    # 开启；生产 config.json 仍为 false，由验收门 4 单独断言）
    old_direct = runner.CFG.get("execution", {}).get("emergency_direct_exec", False)
    runner.CFG.setdefault("execution", {})["emergency_direct_exec"] = True
    try:
        now_t1 = datetime.combine(T1, dtime(9, 14, 0))
        out = limit_halt.premarket_failsafe(conn, now=now_t1, do_exec=True)
        assert out["pending"] == ["600519"], out
        assert out["executed"] == ["600519"], "09:14 兜底必须自动执行：%s" % out
        tr = conn.execute(
            "SELECT price, shares, confirmed_by FROM trade WHERE decision_id=?",
            (did,)).fetchone()
        assert tr is not None, "failsafe 必须落成交"
        assert float(tr[0]) == 90.0 and int(tr[1]) == 200
        assert tr[2] == "emergency_timeout_failsafe", "审计链确认人必须可归因"
        assert conn.execute("SELECT status FROM decision WHERE id=?",
                            (did,)).fetchone()[0] == "executed"
        # W-A9：应急单离线按显式设计价执行，不得静默以昨收成交（留痕可查）
        n_exec = conn.execute("SELECT COUNT(*) FROM risk_event WHERE"
                              " rule='stale_price_exec'").fetchone()[0]
        assert n_exec >= 1
    finally:
        runner.CFG["execution"]["emergency_direct_exec"] = old_direct
        conn.close()
    print("  [c] failsafe 09:14 自动执行成交 —— PASS")


# ---------------------------------------------------------------- 验收门 4 + 入口

def check_config_guard() -> None:
    """验收门第 4 条：生产 config.json 的 execution.emergency_direct_exec 必须为
    false（绝不自动成交总闸）。只读，不改。"""
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    val = cfg.get("execution", {}).get("emergency_direct_exec")
    assert val is False, "验收门4：execution.emergency_direct_exec 必须=false，实得 %r" % val
    assert runner.CFG.get("execution", {}).get("emergency_direct_exec") is False
    print("  [g] config execution.emergency_direct_exec=false —— PASS")


def main() -> int:
    print("==== Sprint4 批次A e2e（沙箱 %s）====" % SANDBOX)
    check_config_guard()
    failed = 0
    for fn in SCENARIOS:
        try:
            fn()
        except Exception:
            failed += 1
            print("FAIL %s" % fn.__name__)
            traceback.print_exc()
    total = len(SCENARIOS)
    print("==== %d/%d 场景通过 ====" % (total - failed, total))
    if failed == 0:
        shutil.rmtree(SANDBOX, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
