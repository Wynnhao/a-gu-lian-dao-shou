"""观察期机制全量测试（出手率打磨批批次3，2026-10-08 裁决#3/#4/#10）。

覆盖：observing 状态机（3a）、watch streak 统计（3b）、表态纪律提示词（3c）、
watch_pool upsert 幂等（3e）、日报失守标注（3d）、postclose sweeper 不误扫
observing、runner propose watch→observing 直通。

全离线：:memory: 库 + AGSICKLE_* 沙箱（强制赋值防 shell 残留击穿隔离）。
直跑：.venv/bin/python3 tests/test_observing.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import traceback
from datetime import date, datetime, time, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
if str(BASE / "tests") not in sys.path:
    sys.path.insert(0, str(BASE / "tests"))

# ---- 沙箱（import 项目模块前生效；强制赋值——批次3a 执行域教训）----
_TMP = Path(tempfile.mkdtemp(prefix="agsickle_observing_"))
os.environ["AGSICKLE_SIGNAL_EVAL_DIR"] = str(_TMP / "signal_eval")
os.environ["AGSICKLE_LOG_DIR"] = str(_TMP / "logs")
os.environ["AGSICKLE_SESSION_DIR"] = str(_TMP / "session")
os.environ["AGSICKLE_ORDERS_DIR"] = str(_TMP / "state")
os.environ["AGSICKLE_STATE_DIR"] = str(_TMP / "state")
os.environ["AGSICKLE_DISABLE_LIVE_QUOTES"] = "1"
os.environ["AGSICKLE_DISABLE_NOTIFY"] = "1"
os.environ["AGSICKLE_DISABLE_FETCHER"] = "1"
os.environ["AGSICKLE_DISABLE_NEWS"] = "1"
os.environ["AGSICKLE_DISABLE_SPOT"] = "1"

from data.fetcher import DDL, close_watch_pool, ensure_watch_pool_table, \
    init_db, upsert_watch_pool  # noqa: E402
from ai import bundle as ai_bundle  # noqa: E402
from ai import decide as ai_decide  # noqa: E402
from execution import runner  # noqa: E402
from test_execution import NOW_DATE, seed_market  # noqa: E402

_TESTS = []


def test(fn):
    _TESTS.append(fn)
    return fn


test.__test__ = False  # pytest 不要把装饰器本身当测试收集


def _mem() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    return conn


def _dec(action, code, conf=0.65, tw=0.0, reasons=None):
    d = {"action": action, "code": code, "target_weight": tw,
         "confidence": conf, "reasons": reasons or ["理由一", "理由二"],
         "risk_notes": []}
    if action in ("buy", "sell"):
        d["order"] = {"side": action, "price": 10.0, "shares": 100}
    return d


# ============================================================
# 3a：observing 状态机（save_decisions 落库口径）
# ============================================================

@test
def test_observing_status_watch_high_and_low_conf():
    """裁决#4：watch 一律 status=observing——高/低 conf 均免降级，
    不再落 report_only（watch 死状态根因之一）。"""
    conn = _mem()
    try:
        decs = [_dec("watch", "002463", conf=0.72),
                _dec("watch", "002230", conf=0.40)]
        ids = ai_decide.save_decisions(conn, decs, "{}", run_date=NOW_DATE)
        assert len(ids) == 2
        rows = dict(conn.execute(
            "SELECT code, status FROM decision WHERE id IN (?,?)",
            tuple(ids)).fetchall())
        assert rows["002463"] == "observing", rows
        assert rows["002230"] == "observing", rows  # 低 conf 也 observing
    finally:
        conn.close()


@test
def test_hold_exempt_from_report_only_downgrade():
    """裁决#4：hold 免降级 → proposed（无交易动作，propose_db 直通 approved）；
    buy 低 conf 仍 report_only（降级语义只保留在交易动作上）。"""
    conn = _mem()
    try:
        ids = ai_decide.save_decisions(
            conn, [_dec("hold", "600519", conf=0.40),
                   _dec("buy", "600108", conf=0.40, tw=0.05)],
            "{}", run_date=NOW_DATE)
        rows = dict(conn.execute(
            "SELECT code, status FROM decision WHERE id IN (?,?)",
            tuple(ids)).fetchall())
        assert rows["600519"] == "proposed", rows  # 低 conf hold 不再 report_only
        assert rows["600108"] == "report_only", rows  # buy 低 conf 保留降级
    finally:
        conn.close()


@test
def test_runner_propose_watch_goes_observing_hold_approved():
    """裁决#4：propose 直通分支——watch 终态 observing（非 approved，
    不被过期单 sweeper 波及）；hold 保持 approved。"""
    conn = _mem()
    seed_market(conn)
    import tempfile as _tf
    orders = Path(_tf.mkdtemp(prefix="agsickle_obs_orders_"))
    try:
        now = datetime.combine(date.today(), time(10, 0))
        ids = ai_decide.save_decisions(
            conn, [_dec("watch", "002463", conf=0.72),
                   _dec("hold", "600519", conf=0.72)],
            "{}", run_date=NOW_DATE)
        conn.commit()
        wid, hid = ids
        runner.propose_db(conn, run_date=NOW_DATE, now=now)
        w_st = conn.execute("SELECT status FROM decision WHERE id=?",
                            (wid,)).fetchone()[0]
        h_st = conn.execute("SELECT status FROM decision WHERE id=?",
                            (hid,)).fetchone()[0]
        assert w_st == "observing", w_st
        assert h_st == "approved", h_st
    finally:
        conn.close()


@test
def test_propose_db_and_sweeper_ignore_observing():
    """裁决#4 配套：propose_db 不处理 observing；postclose 过期单 sweeper
    （action IN buy/sell 且 status=approved）不误扫 observing——跨日 observing
    原样保留。"""
    from pipeline.postclose import _sweep_expired_decisions
    conn = _mem()
    seed_market(conn)
    try:
        yday = (date.today() - timedelta(days=1)).isoformat()
        ids = ai_decide.save_decisions(
            conn, [_dec("watch", "002463", conf=0.72)],
            "{}", run_date=yday)
        conn.execute("UPDATE decision SET status='observing' WHERE id=?",
                     (ids[0],))
        conn.commit()
        # sweeper（昨日 approved buy/sell 才扫；observing watch 不动）
        r = _sweep_expired_decisions(conn)
        assert r["expired"] == 0, r
        st = conn.execute("SELECT status FROM decision WHERE id=?",
                          (ids[0],)).fetchone()[0]
        assert st == "observing", st
    finally:
        conn.close()


# ============================================================
# 3b：watch streak 统计
# ============================================================

@test
def test_watch_streak_same_day_dedup_and_cross_day():
    """3b：streak=DISTINCT run_date（同日多条计 1）、跨日累计、按 streak 降序。"""
    conn = _mem()
    try:
        today = date.today()
        d1, d2, d3 = ((today - timedelta(days=n)).isoformat()
                      for n in (2, 1, 0))
        for rd, code, conf in ((d1, "002463", 0.68), (d2, "002463", 0.62),
                               (d2, "002463", 0.65),  # 同日第二条
                               (d2, "002230", 0.55), (d3, "002230", 0.62)):
            conn.execute(
                "INSERT INTO decision (run_date, trade_date, code, action,"
                " target_weight, confidence, reasons, risk_notes, status,"
                " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rd, rd, code, "watch", 0.0, conf, '["r1"]', "[]",
                 "observing", rd + "T09:00:00"))
        conn.commit()
        ws = {x["code"]: x for x in ai_bundle._watch_streaks(conn)}
        assert ws["002463"]["streak_days"] == 2, ws  # 同日两条计 1
        assert ws["002230"]["streak_days"] == 2
        assert ws["002463"]["first_watch"] == d1
        assert ws["002463"]["last_watch"] == d2
        assert ws["002463"]["deadline_left"] == ai_bundle.WATCH_STREAK_LINE - 2
    finally:
        conn.close()


@test
def test_watch_streak_episode_reset_after_trade():
    """3b：升级 buy（或 sell）后的 streak 重计——episode 边界=最后交易日。"""
    conn = _mem()
    try:
        today = date.today()
        d_old, d_new = (today - timedelta(days=5)).isoformat(), \
                       (today - timedelta(days=1)).isoformat()
        for rd in (d_old, d_new):
            conn.execute(
                "INSERT INTO decision (run_date, trade_date, code, action,"
                " target_weight, confidence, reasons, risk_notes, status,"
                " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rd, rd, "002463", "watch", 0.0, 0.7, '["r1"]', "[]",
                 "observing", rd + "T09:00:00"))
        conn.execute(
            "INSERT INTO decision (run_date, trade_date, code, action,"
            " target_weight, confidence, reasons, risk_notes, status,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (d_old, d_old, "002463", "buy", 0.05, 0.7, '["r1"]', "[]",
             "executed", d_old + "T09:40:00"))
        conn.commit()
        ws = {x["code"]: x for x in ai_bundle._watch_streaks(conn)}
        assert ws["002463"]["streak_days"] == 1, ws  # 只计 buy 之后的 d_new
        assert ws["002463"]["first_watch"] == d_new
    finally:
        conn.close()


# ============================================================
# 3c：表态纪律提示词
# ============================================================

@test
def test_stance_discipline_prompt_rule12():
    """3c：_OUTPUT_RULES 第 12 条常量锁定 + PROMPT_VERSION bump +
    连续性提醒补充文案。"""
    rules = ai_bundle._OUTPUT_RULES
    assert "表态纪律" in rules
    assert "移出观察" in rules
    assert "无限期观察视为放弃判断" in rules
    assert ai_bundle.PROMPT_VERSION >= "2026-10.2"
    assert ai_bundle.WATCH_STREAK_LINE == 3  # 裁决#3
    assert ai_bundle.WATCH_POOL_CAP == 20    # 裁决#10 容量上限


@test
def test_bundle_markdown_watch_sections_render():
    """3b/3e：bundle markdown 出现「观察池状态」与「当前观察池」两节，
    streak≥3 票渲染「今日须表态」。"""
    conn = _mem()
    try:
        today = date.today()
        for n in (2, 1, 0):
            rd = (today - timedelta(days=n)).isoformat()
            conn.execute(
                "INSERT INTO decision (run_date, trade_date, code, action,"
                " target_weight, confidence, reasons, risk_notes, status,"
                " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rd, rd, "002463", "watch", 0.0, 0.7, '["r1"]', "[]",
                 "observing", rd + "T09:00:00"))
        ensure_watch_pool_table(conn)
        upsert_watch_pool(conn, "002463", (today - timedelta(days=2)).isoformat(),
                          thesis="测试逻辑", catalyst="测试催化剂")
        conn.commit()
        b = ai_bundle.build_bundle(conn=conn)
        md = ai_bundle.bundle_to_markdown(b)
        assert "观察池状态" in md
        assert "当前观察池" in md
        assert "今日须表态" in md
        assert "002463" in md
    finally:
        conn.close()


# ============================================================
# 3e：watch_pool upsert 幂等 + decide 落库挂接
# ============================================================

@test
def test_watch_pool_upsert_idempotent():
    """3e：同日重复 watch 计 1（noop）；跨日 +1；buy 终结后重新开 episode；
    终态幂等。"""
    conn = _mem()
    try:
        ensure_watch_pool_table(conn)
        today = date.today().isoformat()
        yday = (date.today() - timedelta(days=1)).isoformat()
        r1 = upsert_watch_pool(conn, "002463", yday, thesis="t1",
                               catalyst="c1", deadline="2026-12-31")
        assert r1["transition"] == "new_episode" and r1["count"] == 1
        r2 = upsert_watch_pool(conn, "002463", yday)  # 同日第二条
        assert r2["transition"] == "noop_same_day" and r2["count"] == 1
        r3 = upsert_watch_pool(conn, "002463", today)
        assert r3["transition"] == "extended" and r3["count"] == 2
        # 首日 thesis 不被后续覆盖
        row = conn.execute("SELECT thesis, first_date, count FROM watch_pool"
                           " WHERE code='002463'").fetchone()
        assert row[0] == "t1" and row[1] == yday and row[2] == 2
        # buy → upgraded
        assert close_watch_pool(conn, "002463", "upgraded") is True
        assert conn.execute("SELECT status FROM watch_pool WHERE code='002463'"
                            ).fetchone()[0] == "upgraded"
        # 终态再 close → no-op False
        assert close_watch_pool(conn, "002463", "upgraded") is False
        # 新 watch → 重开 episode
        r4 = upsert_watch_pool(conn, "002463", today)
        assert r4["transition"] == "reopened" and r4["count"] == 1
    finally:
        conn.close()


@test
def test_decide_save_maintains_watch_pool():
    """3e：save_decisions watch → watch_pool 自动 upsert（thesis/催化剂/
    deadline 从 reasons 首条结构化提取）；buy → upgraded。"""
    conn = _mem()
    try:
        ids = ai_decide.save_decisions(
            conn, [_dec("watch", "002463", conf=0.72,
                        reasons=["v2 score=0.83 催化剂：CRO 订单回暖 2026-10-15 复核",
                                 "r2"])],
            "{}", run_date=NOW_DATE)
        assert len(ids) == 1
        row = conn.execute(
            "SELECT code, count, status, thesis, catalyst, deadline"
            " FROM watch_pool WHERE code='002463'").fetchone()
        assert row is not None, "watch 落库须自动 upsert watch_pool"
        assert row[1] == 1 and row[2] == "active"
        assert "催化剂" in row[3]  # thesis=首条前 80 字
        assert row[4].startswith("CRO 订单回暖")  # 催化剂提取
        assert row[5] == "2026-10-15"   # deadline 提取
        # buy → upgraded
        ai_decide.save_decisions(
            conn, [_dec("buy", "002463", conf=0.8, tw=0.05)],
            "{}", run_date=NOW_DATE)
        st = conn.execute("SELECT status FROM watch_pool WHERE code='002463'"
                          ).fetchone()[0]
        assert st == "upgraded", st
    finally:
        conn.close()


@test
def test_decide_hold_remove_observation_closes_pool():
    """3e：hold 且 reasons 首条含「移出观察」→ removed；普通 hold 不动池。"""
    conn = _mem()
    try:
        ensure_watch_pool_table(conn)
        upsert_watch_pool(conn, "002463", NOW_DATE)
        ai_decide.save_decisions(
            conn, [_dec("hold", "002463",
                        reasons=["移出观察：CRO 订单证伪，逻辑失效", "r2"])],
            "{}", run_date=NOW_DATE)
        st = conn.execute("SELECT status FROM watch_pool WHERE code='002463'"
                          ).fetchone()[0]
        assert st == "removed", st
        # 普通 hold 不动
        upsert_watch_pool(conn, "600108", NOW_DATE)
        ai_decide.save_decisions(
            conn, [_dec("hold", "600108", reasons=["估值到位", "r2"])],
            "{}", run_date=NOW_DATE)
        st2 = conn.execute("SELECT status FROM watch_pool WHERE code='600108'"
                           ).fetchone()[0]
        assert st2 == "active", st2
    finally:
        conn.close()


# ============================================================
# 3d：日报观察纪律失守标注
# ============================================================

@test
def test_daily_watch_discipline_breach_and_act():
    """3d：streak≥3 票当日未表态 → 「观察纪律失守」；有 buy 表态 → 已表态。"""
    from review.daily import _sec_watch_discipline
    conn = _mem()
    try:
        today = date.today()
        for n in (2, 1, 0):
            rd = (today - timedelta(days=n)).isoformat()
            conn.execute(
                "INSERT INTO decision (run_date, trade_date, code, action,"
                " target_weight, confidence, reasons, risk_notes, status,"
                " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rd, rd, "002463", "watch", 0.0, 0.7, '["r1"]', "[]",
                 "observing", rd + "T09:00:00"))
        conn.commit()
        sec = _sec_watch_discipline(conn, today.isoformat())
        assert "观察纪律失守" in sec, sec
        assert "002463" in sec
        # 当日 buy 表态 → 不再失守
        conn.execute(
            "INSERT INTO decision (run_date, trade_date, code, action,"
            " target_weight, confidence, reasons, risk_notes, status,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (today.isoformat(), today.isoformat(), "002463", "buy", 0.05,
             0.75, '["累计证据升级"]', "[]", "proposed",
             today.isoformat() + "T09:00:00"))
        conn.commit()
        sec2 = _sec_watch_discipline(conn, today.isoformat())
        assert "观察纪律失守" not in sec2, sec2
        # buy 落库后 streak episode 重计（due 清空 → 显示"无到期票"或"已表态"
        # 取决于统计口径），关键行为=失守标注消失
        # hold+移出观察 也算表态
        conn.execute("DELETE FROM decision WHERE action='buy' AND code='002463'")
        conn.execute(
            "INSERT INTO decision (run_date, trade_date, code, action,"
            " target_weight, confidence, reasons, risk_notes, status,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (today.isoformat(), today.isoformat(), "002463", "hold", 0.0,
             0.6, '["移出观察：逻辑失效"]', "[]", "proposed",
             today.isoformat() + "T09:00:00"))
        conn.commit()
        sec3 = _sec_watch_discipline(conn, today.isoformat())
        assert "观察纪律失守" not in sec3, sec3
    finally:
        conn.close()


def main() -> int:
    failed = 0
    for fn in _TESTS:
        try:
            fn()
            print("PASS %s" % fn.__name__)
        except Exception:  # noqa: BLE001
            failed += 1
            print("FAIL %s" % fn.__name__)
            traceback.print_exc()
    print("%d/%d tests passed" % (len(_TESTS) - failed, len(_TESTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
