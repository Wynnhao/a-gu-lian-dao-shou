"""data/audit 停板口径与 pct_out_of_range 三类豁免测试（2026-09-15 人工复核后落地）。

check_db 只查 daily_bar，用内联最小建表避免连带 import fetcher（akshare 重依赖）。
直跑：python3 tests/test_audit.py

批次3b（2026-09-21）新增（数据域清债批）：
- P1-7：audit 豁免判据与 recalc 写入口径对齐（除权日 qfq 环比总回报超板豁免）；
- 任务5：audit 与 recalc 的 tx_pct 跳变/背离阈值统一（单一常量源）；
- P1-8：profile_verdict 宇宙守卫（INSUFFICIENT-UNIVERSE，signal_eval 侧）；
- 任务7：冷却窗命中首次 notify（fetcher 侧，幂等标记）。
"""
import os
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.audit import (TX_PCT_DIVERGE_TOL_PP, TX_PCT_EXDAY_JUMP,  # noqa: E402
                        _limit_pct, check_db)

DDL = """
CREATE TABLE daily_bar (
    code TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL, pct_chg REAL, turnover REAL,
    source TEXT, close_qfq REAL, high_qfq REAL, low_qfq REAL,
    PRIMARY KEY (code, trade_date)
);
CREATE TABLE signal (
    code TEXT, as_of TEXT, signals TEXT, score REAL,
    profile TEXT DEFAULT 'reversal_lowvol',
    PRIMARY KEY (code, as_of, profile)
);
CREATE TABLE fetch_log (code TEXT, run_at TEXT, status TEXT, rows INT, detail TEXT);
CREATE TABLE risk_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, rule TEXT, detail TEXT,
    decision_id INT
);
"""


def _mem() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    return conn


def _add(conn, code, td, close, pct, cq=None, source=None):
    """加一行自洽数据（量纲/OHLC 合法，避免混入无关告警）。"""
    volume = 1e6
    conn.execute(
        "INSERT INTO daily_bar (code, trade_date, open, high, low, close,"
        " volume, amount, pct_chg, close_qfq, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (code, td, close, close * 1.01, close * 0.99, close,
         volume, close * volume * 100, pct, cq, source))


def _pct_flags(conn):
    issues, total = check_db(conn)
    return [i for i in issues if i["kind"] == "pct_out_of_range"], total


def test_limit_pct_board_bands():
    assert _limit_pct("600519") == 10.5
    assert _limit_pct("300750") == 20.5
    assert _limit_pct("302132") == 20.5    # 创业板新代码段（此前误按 10.5）
    assert _limit_pct("688801") == 20.5
    assert _limit_pct("830799") == 30.5
    assert _limit_pct("920002") == 30.5


def test_302_band_no_longer_flagged():
    conn = _mem()
    _add(conn, "302132", "2026-05-20", 10.0, None)
    _add(conn, "302132", "2026-05-21", 12.0, 19.5, cq=12.0)
    flags, _ = _pct_flags(conn)
    assert flags == [], flags


def test_new_stock_first5days_exempt():
    conn = _mem()
    # 主板新股第2个交易日 -24.76%（无涨跌幅限制）→ 豁免
    _add(conn, "001221", "2026-05-20", 30.0, None)
    _add(conn, "001221", "2026-05-21", 22.6, -24.76)
    flags, _ = _pct_flags(conn)
    assert flags == [], flags
    # 第7个交易日（row_no=6）超停板 → 仍要报
    for i in range(2, 7):
        _add(conn, "001221", f"2026-05-{20 + i}", 22.6, 0.0)
    _add(conn, "001221", "2026-05-27", 25.3, 12.0)
    flags, _ = _pct_flags(conn)
    assert len(flags) == 1 and flags[0]["code"] == "001221", flags


def test_exdiv_exempt_but_real_crash_still_flagged():
    conn = _mem()
    # 除权假跌：原始 -25%，前复权 +1%（10.0→10.1）→ 豁免（垫 7 行历史走出新股窗口）
    for i in range(7):
        _add(conn, "000999", "2026-05-%02d" % (10 + i), 10.0, 0.0, cq=10.0)
    _add(conn, "000999", "2026-05-17", 7.5, -25.0, cq=10.1)
    flags, _ = _pct_flags(conn)
    assert flags == [], flags
    # 真崩盘：复权后同样 -25% → 必须照报
    for i in range(7):
        _add(conn, "000001", "2026-05-%02d" % (10 + i), 10.0, 0.0, cq=10.0)
    _add(conn, "000001", "2026-05-17", 7.5, -25.0, cq=7.5)
    flags, _ = _pct_flags(conn)
    assert len(flags) == 1 and flags[0]["code"] == "000001", flags


def test_qfq_missing_conservative_flag():
    conn = _mem()
    # qfq 未回填时无法判定除权 → 保守照报（保持旧行为；垫 7 行走出新股窗口）
    for i in range(7):
        _add(conn, "600519", "2026-05-%02d" % (10 + i), 10.0, 0.0)
    _add(conn, "600519", "2026-05-17", 11.3, 13.0)
    flags, _ = _pct_flags(conn)
    assert len(flags) == 1 and flags[0]["code"] == "600519", flags


# ============================================================
# 批次3b · P1-7：audit 豁免判据与 recalc 写入口径对齐
# （40 行/16 码 pct_out_of_range 永久报警的根因消除）
# ============================================================

def _seed_exday_recalc_row(conn, code, pct, cq, source="tx"):
    """垫 7 行走出新股窗口 + 一行除权事件日行：d(t) 跳变 1.3（>0.01 除权签名）、
    qfq 环比 +13.0%（recalc 会写入的总回报口径，超 10.5 板幅——002271 同构）。"""
    for i in range(7):
        _add(conn, code, "2026-05-%02d" % (10 + i), 10.0, 0.0, cq=10.0,
             source=source)
    _add(conn, code, "2026-05-17", 10.0, pct, cq=cq, source=source)
    # d_jump = |(c−cq) − (pc−pcq)| = |(10−cq) − 0| → cq=11.3 时 =1.3


def test_p1c7_exday_recalc_pct_exempt():
    """正用例（40 行类）：除权事件日 pct = qfq 环比总回报（+13% 超板）→ 豁免。
    生产实证：40/40 行均属此口径（recalc 写入），对齐后 pct_out_of_range 归零。"""
    conn = _mem()
    _seed_exday_recalc_row(conn, "002271", pct=13.0, cq=11.3)
    flags, total = _pct_flags(conn)
    assert flags == [], flags
    assert total == 0, total
    conn.close()


def test_p1c7_exday_pct_off_recalc_still_flagged():
    """反用例（真异常不豁免）：除权日 pct 与 qfq 环比背离 >1pp → pct_out_of_range
    与 tx_pct_divergent 双报（豁免只认 recalc 写入口径，不放过任意超板值）。"""
    conn = _mem()
    _seed_exday_recalc_row(conn, "002271", pct=13.0, cq=11.1)  # qfq环比 +11%，dev 2pp
    flags, _total = _pct_flags(conn)
    assert len(flags) == 1 and flags[0]["code"] == "002271", flags
    issues, _ = check_db(conn)
    kinds = {i["kind"] for i in issues}
    assert "tx_pct_divergent" in kinds, kinds
    conn.close()


def test_p1c7_exday_band_within_qfq_takes_old_exemption_first():
    """反用例（豁免次序不变）：除权签名但 qfq 环比在板内 → 仍走原「除权假跌」
    豁免（新口径档只兜 qfq 环比超板的 recalc 残渣，不改变既有豁免优先级）。"""
    conn = _mem()
    for i in range(7):
        _add(conn, "600519", "2026-05-%02d" % (10 + i), 10.0, 0.0, cq=10.0)
    _add(conn, "600519", "2026-05-17", 11.3, 13.0, cq=10.3)  # qfq环比 +3% 在板内
    flags, total = _pct_flags(conn)
    assert flags == [] and total == 0, (flags, total)
    conn.close()


# ============================================================
# 批次3b · 任务5：audit 与 recalc 的 tx_pct 阈值统一（单一常量源）
# ============================================================

def _seed_tx_divergence(conn, code, pct, cq=11.3):
    """除权签名行（d_jump=1.3）+ 指定 pct（与 qfq 环比 +13% 形成可控背离）。"""
    for i in range(7):
        _add(conn, code, "2026-06-%02d" % (1 + i), 10.0, 0.0, cq=10.0, source="tx")
    _add(conn, code, "2026-06-08", 10.0, pct, cq=cq, source="tx")


def test_task5_thresholds_single_source_and_divergent_flag():
    """常量单一来源：audit 报警与 fetcher.recalc 重写共用 TX_PCT_* 常量；
    背离 1.5pp（>统一容差）→ audit 报 tx_pct_divergent。"""
    from data import audit as _audit
    assert _audit.TX_PCT_DIVERGE_TOL_PP == 1.0
    assert _audit.TX_PCT_EXDAY_JUMP == 0.01
    conn = _mem()
    _seed_tx_divergence(conn, "000001", pct=14.5)   # dev |14.5−13| = 1.5 > 1.0
    issues, _ = check_db(conn)
    div = [i for i in issues if i["kind"] == "tx_pct_divergent"]
    assert len(div) == 1, issues
    conn.close()


def test_task5_recalc_uses_unified_thresholds():
    """行为对齐：背离 0.8pp（旧 recalc 0.1pp 会改写、audit 1.0pp 不报）→
    统一后 recalc 不再改写（与 audit 不报同门）；背离 1.5pp → 重写为 qfq 环比。"""
    from data import fetcher  # 惰性导入（akshare 重依赖，仅本用例承担）
    conn = _mem()
    _seed_tx_divergence(conn, "000001", pct=13.8)   # dev 0.8 ∈ (0.1, 1.0]
    fixed, _skipped = fetcher.recalc_tx_pct(conn)
    assert fixed == 0, "统一容差下 0.8pp 背离不得改写（与 audit 不报同门）"
    got = conn.execute("SELECT pct_chg FROM daily_bar WHERE trade_date='2026-06-08'"
                       ).fetchone()[0]
    assert got == 13.8, got
    # 对照：1.5pp 背离 → 重写为 qfq 环比（--fix 收敛 audit 报警集）
    _seed_tx_divergence(conn, "000002", pct=14.5)
    fixed2, _ = fetcher.recalc_tx_pct(conn, code="000002")
    assert fixed2 == 1, "1.5pp 背离应被重算"
    got2 = conn.execute("SELECT pct_chg FROM daily_bar WHERE code='000002'"
                        " AND trade_date='2026-06-08'").fetchone()[0]
    assert abs(got2 - 13.0) < 1e-6, got2
    conn.close()


# ============================================================
# 批次3b · P1-8：profile_verdict 宇宙守卫（INSUFFICIENT-UNIVERSE）
# ============================================================

def _write_bt_fixture(directory, cur_ann=-0.10, alt_ann=0.30,
                      cur_mdd=-0.10, alt_mdd=-0.03):
    """最小 backtest_result.json（momentum 为当前 profile，双备选）。"""
    import json
    p = Path(directory) / "backtest_result.json"
    payload = {
        "generated_at": "2026-09-19 12:00:00",
        "universe": {"mode": "full", "codes": 816},
        "data_version": "test-fixture",
        "profiles": {
            "momentum": {
                "strategy_perf": {"annual_return": cur_ann, "max_drawdown": cur_mdd},
                "benchmark_hs300": {"annual_return": 0.1169, "max_drawdown": -0.1566}},
            "reversal_lowvol": {
                "strategy_perf": {"annual_return": alt_ann, "max_drawdown": alt_mdd},
                "benchmark_hs300": {"annual_return": 0.1169, "max_drawdown": -0.1566}},
            "reversal_lowvol_v2": {
                "strategy_perf": {"annual_return": (cur_ann + alt_ann) / 2,
                                  "max_drawdown": (cur_mdd + alt_mdd) / 2},
                "benchmark_hs300": {"annual_return": 0.1169, "max_drawdown": -0.1566}},
        },
    }
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _seed_universe(conn, n_codes, max_date="2026-09-18"):
    """n_codes 只票在最新交易日有 bar（宇宙新鲜度度量口径）。"""
    for i in range(n_codes):
        _add(conn, "%06d" % (600000 + i), max_date, 10.0, 0.0, cq=10.0)
    conn.commit()


def _run_verdict(conn, bt_path):
    from review import signal_eval
    return signal_eval.profile_verdict(conn, bt_path=bt_path)


def test_p1c8_universe_guard_abstains_when_starved():
    """正用例：最新交易日仅 5 票（断粮宇宙，生产实测 86 票同构）→ B/C 判据弃权：
    verdict=hold（不产 switch/keep）、abstains 带 INSUFFICIENT-UNIVERSE、
    投票字段置 None；B/C 数字照常透出供人工参考。"""
    old_prof = os.environ.get("AGSICKLE_SIGNALS_PROFILE")
    os.environ["AGSICKLE_SIGNALS_PROFILE"] = "momentum"
    conn = _mem()
    try:
        _seed_universe(conn, 5)
        bt = _write_bt_fixture(tempfile.mkdtemp(prefix="bt_guard_"))
        v = _run_verdict(conn, bt)
        assert v["verdict"] == "hold", v["verdict"]
        assert v["universe"]["insufficient"] is True, v["universe"]
        assert v["universe"]["codes_on_latest"] == 5, v["universe"]
        assert any("INSUFFICIENT-UNIVERSE" in a for a in v["abstains"]), v["abstains"]
        assert v["votes"]["b_switch"] is None and v["votes"]["c_switch"] is None, v["votes"]
        assert v["B"]["current"] is not None, "B/C 数字应照常透出"
    finally:
        conn.close()
        if old_prof is None:
            os.environ.pop("AGSICKLE_SIGNALS_PROFILE", None)
        else:
            os.environ["AGSICKLE_SIGNALS_PROFILE"] = old_prof


def test_p1c8_universe_guard_passes_when_sufficient():
    """反用例：最新交易日 301 票 ≥ 守卫阈值 → 原路径（B/C 双劣 → switch）。"""
    old_prof = os.environ.get("AGSICKLE_SIGNALS_PROFILE")
    os.environ["AGSICKLE_SIGNALS_PROFILE"] = "momentum"
    conn = _mem()
    try:
        _seed_universe(conn, 301)
        bt = _write_bt_fixture(tempfile.mkdtemp(prefix="bt_ok_"))
        v = _run_verdict(conn, bt)
        assert v["verdict"] == "switch", v["verdict"]
        assert v["universe"]["insufficient"] is False, v["universe"]
        assert v["votes"]["b_switch"] is True, v["votes"]
        assert not any("universe" in a for a in v["abstains"]), v["abstains"]
    finally:
        conn.close()
        if old_prof is None:
            os.environ.pop("AGSICKLE_SIGNALS_PROFILE", None)
        else:
            os.environ["AGSICKLE_SIGNALS_PROFILE"] = old_prof


def test_p1c8_universe_guard_empty_db_legacy_path():
    """反用例（边界语义）：空库（0 票）不触发守卫，走原路径——空库场景由
    MIN_SAMPLES 等既有"样本不足"标注兜底（也保证既有空 conn 单测语义不变）。"""
    old_prof = os.environ.get("AGSICKLE_SIGNALS_PROFILE")
    os.environ["AGSICKLE_SIGNALS_PROFILE"] = "momentum"
    conn = _mem()
    try:
        bt = _write_bt_fixture(tempfile.mkdtemp(prefix="bt_empty_"))
        v = _run_verdict(conn, bt)
        assert v["verdict"] == "switch", v["verdict"]
        assert v["universe"]["codes_on_latest"] == 0
        assert v["universe"]["insufficient"] is False
    finally:
        conn.close()
        if old_prof is None:
            os.environ.pop("AGSICKLE_SIGNALS_PROFILE", None)
        else:
            os.environ["AGSICKLE_SIGNALS_PROFILE"] = old_prof


# ============================================================
# 批次3b · 任务7：冷却窗命中首次 notify（幂等标记，跨进程限一次）
# ============================================================

class _EnvSandbox:
    """临时 state 目录 + 通知短路解除（直接赋值非 setdefault，防 shell 残留）。"""

    def __enter__(self):
        self._old = {k: os.environ.get(k) for k in
                     ("AGSICKLE_STATE_DIR", "AGSICKLE_DISABLE_NOTIFY",
                      "AGSICKLE_LOG_DIR")}
        self._tmp = tempfile.mkdtemp(prefix="cooldown_notify_test_")
        os.environ["AGSICKLE_STATE_DIR"] = self._tmp
        os.environ.pop("AGSICKLE_DISABLE_NOTIFY", None)
        os.environ.pop("AGSICKLE_LOG_DIR", None)   # 生产形态：逃生门未设
        return self._tmp

    def __exit__(self, *exc):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def _patch_channel(recorder):
    import risk.notify as _rn
    orig = _rn.notify
    _rn.notify = lambda t, b: recorder((t, b))
    return orig


def test_task7_cooldown_notify_first_hit_notifies_then_suppressed():
    """正+反：首次命中 → notified 且通道恰一次；冷却窗内重复命中 → suppressed
    （幂等标记落盘，跨进程防刷屏）。"""
    from data import fetcher
    calls = []
    with _EnvSandbox():
        orig = _patch_channel(calls.append)
        try:
            s1 = fetcher._notify_cooldown_hit("qfq_rebrush:600519", "t1", "b1",
                                              window_days=7)
            assert s1 == "notified", s1
            assert len(calls) == 1 and calls[0][0] == "t1"
            marker = Path(fetcher._cooldown_notify_marker_path())
            assert marker.is_file(), "幂等标记必须落盘"
            s2 = fetcher._notify_cooldown_hit("qfq_rebrush:600519", "t2", "b2",
                                              window_days=7)
            assert s2 == "suppressed", s2
            assert len(calls) == 1, "冷却窗内不得二次 notify"
            # 不同 key 各自独立限次
            s3 = fetcher._notify_cooldown_hit("qfq_rebrush:000001", "t3", "b3",
                                              window_days=7)
            assert s3 == "notified" and len(calls) == 2
        finally:
            import risk.notify as _rn
            _rn.notify = orig


def test_task7_cooldown_notify_rearm_after_window():
    """正用例（窗过期再武装）：标记时间戳早于冷却窗 → 再次 notify。"""
    import json as _json
    import time as _time
    from data import fetcher
    calls = []
    with _EnvSandbox():
        orig = _patch_channel(calls.append)
        try:
            marker = fetcher._cooldown_notify_marker_path()
            marker.parent.mkdir(parents=True, exist_ok=True)
            stale = _time.time() - 8 * 86400.0   # 8 天前（> 7 天冷却窗）
            marker.write_text(_json.dumps({"qfq_rebrush:600519": stale}),
                              encoding="utf-8")
            s = fetcher._notify_cooldown_hit("qfq_rebrush:600519", "t", "b",
                                             window_days=7)
            assert s == "notified" and len(calls) == 1, (s, calls)
        finally:
            import risk.notify as _rn
            _rn.notify = orig


def test_task7_cooldown_notify_test_env_shortcircuit():
    """反用例（测试环境双保险）：AGSICKLE_DISABLE_NOTIFY=1 或 AGSICKLE_LOG_DIR
    已设（测试逃生门）→ 短路不触通道、不落标记（既有未设该变量的测试用例
    ——如 test_qfq_rebuild ⑨ 冷却窗用例——不得弹系统通知）。"""
    from data import fetcher
    calls = []
    orig_log = os.environ.get("AGSICKLE_LOG_DIR")
    orig_dis = os.environ.get("AGSICKLE_DISABLE_NOTIFY")
    # 场景1：仅逃生门（强制清空 DISABLE_NOTIFY，防同进程其他测试文件的
    # setdefault 残留——直接赋值非 setdefault）
    os.environ["AGSICKLE_LOG_DIR"] = tempfile.mkdtemp(prefix="notify_gate_")
    os.environ.pop("AGSICKLE_DISABLE_NOTIFY", None)
    try:
        s = fetcher._notify_cooldown_hit("k", "t", "b")
        assert s == "test_env_suppressed", s
        # 场景2：显式禁用
        os.environ.pop("AGSICKLE_LOG_DIR", None)
        os.environ["AGSICKLE_DISABLE_NOTIFY"] = "1"
        s2 = fetcher._notify_cooldown_hit("k", "t", "b")
        assert s2 == "disabled", s2
        assert calls == []
    finally:
        if orig_log is None:
            os.environ.pop("AGSICKLE_LOG_DIR", None)
        else:
            os.environ["AGSICKLE_LOG_DIR"] = orig_log
        if orig_dis is None:
            os.environ.pop("AGSICKLE_DISABLE_NOTIFY", None)
        else:
            os.environ["AGSICKLE_DISABLE_NOTIFY"] = orig_dis


def test_task7_cooldown_notify_wired_into_backfill_rebrush_branch():
    """接线用例：backfill_qfq 冷却窗（保留窗）分支调用 _notify_cooldown_hit
    （key=qfq_rebrush:<code>）；无冷却留痕的重刷路径不触发。"""
    import pandas as pd
    from data import fetcher
    conn = _mem()
    calls = []
    old_qfq = fetcher._hist_em_qfq
    old_rebrush = fetcher.rebrush_qfq_full
    old_hook = fetcher._notify_cooldown_hit
    try:
        from datetime import datetime
        # 边界对：旧锚 cq=10.0 + 新窗 cq=9.0 → 不变量命中（qfq 环比 -10% 深于 raw 0%）
        for td, cq in (("2024-01-02", 10.0), ("2024-01-03", None)):
            conn.execute(
                "INSERT INTO daily_bar (code, trade_date, open, high, low, close,"
                " volume, amount, pct_chg, close_qfq, source) VALUES"
                " ('600519',?,10,10.1,9.9,10,1e6,1e8,0,?,'em')", (td, cq))
        df = pd.DataFrame({"date": pd.to_datetime(["2024-01-03"]),
                           "close_qfq": [9.0]})
        fetcher._hist_em_qfq = lambda c, s, e: df
        fetcher.rebrush_qfq_full = lambda code, conn: (_m for _m in ()).throw(
            AssertionError("冷却窗内不应触发全史重刷"))
        fetcher._notify_cooldown_hit = lambda key, title, body, **kw: \
            calls.append(key) or "notified"
        # 冷却留痕：新鲜 qfq_full_rebrush → 命中保留窗分支
        conn.execute(
            "INSERT INTO fetch_log VALUES ('600519',?, 'qfq_full_rebrush', 1, 'x')",
            (datetime.now().isoformat(timespec="seconds"),))
        conn.commit()
        n = fetcher.backfill_qfq("600519", conn)
        assert n >= 1
        assert calls == ["qfq_rebrush:600519"], calls
        # 对照（反）：无冷却留痕 → 走重刷路径，不触发 notify
        calls.clear()
        conn.execute("DELETE FROM fetch_log")
        conn.commit()
        fetcher.rebrush_qfq_full = lambda code, conn: (7, "tx_qfq")
        n2 = fetcher.backfill_qfq("600519", conn)
        assert n2 == 7
        assert calls == [], "非冷却路径不得 notify"
    finally:
        fetcher._hist_em_qfq = old_qfq
        fetcher.rebrush_qfq_full = old_rebrush
        fetcher._notify_cooldown_hit = old_hook
        conn.close()


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print("PASS %s" % name)
        except Exception:
            failed += 1
            print("FAIL %s" % name)
            traceback.print_exc()
    print("\n%d/%d tests passed" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
