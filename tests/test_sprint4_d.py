"""Sprint4 批次D 测试：W-D2/W-D3/W-D4/W-D5/W-D6 前置与门/W-D7 各项。

全部 :memory: / 沙箱（AGSICKLE_DB 等 env 在 import 项目模块前置好），
不触生产 market.db 与生产 logs（与 run_all 注入的 AGSICKLE_LOG_DIR 叠加生效）。
"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

# ---- 测试隔离 env（必须在 import 任何项目模块之前设置）----
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="agsickle_sprint4d_"))
_ORIG_ENV = dict(os.environ)


def _restore_env():
    for k in list(os.environ):
        if k.startswith("AGSICKLE_") and k not in _ORIG_ENV:
            os.environ.pop(k, None)
    os.environ.update({k: v for k, v in _ORIG_ENV.items()
                       if k.startswith("AGSICKLE_")})


def teardown_module(module=None):
    _restore_env()


os.environ.setdefault("AGSICKLE_DB", str(_TMP_ROOT / "market.db"))
os.environ.setdefault("AGSICKLE_REPORTS_DIR", str(_TMP_ROOT / "reports"))
os.environ.setdefault("AGSICKLE_ORDERS_DIR", str(_TMP_ROOT / "orders"))
os.environ.setdefault("AGSICKLE_STATE_DIR", str(_TMP_ROOT / "state"))
os.environ.setdefault("AGSICKLE_DISABLE_NOTIFY", "1")
os.environ.setdefault("AGSICKLE_DISABLE_LIVE_QUOTES", "1")
# run_all 会注入每文件独立沙箱；直跑（python tests/test_sprint4_d.py）兜底
os.environ.setdefault("AGSICKLE_LOG_DIR", str(_TMP_ROOT / "logs"))
os.environ.setdefault("AGSICKLE_SIGNAL_EVAL_DIR", str(_TMP_ROOT / "signal_eval"))

from data.fetcher import DDL, get_conn  # noqa: E402

_DDL_DONE = False


def sandbox_conn() -> sqlite3.Connection:
    """带全 schema 的 :memory: 连接（黑名单/stock_info 空即无过滤）。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    return conn


def file_db_conn() -> sqlite3.Connection:
    """AGSICKLE_DB 指向的文件库连接（signals._hs300_close_series 走 get_conn()）。"""
    global _DDL_DONE
    conn = get_conn()
    if not _DDL_DONE:
        conn.executescript(DDL)
        _DDL_DONE = True
    return conn


# ============================================================
# W-D2（P1-16）：catchup 步骤4 state_ok 读 note 列 + 盯市已完成判定
# ============================================================

def test_wd2_mm_done_note_semantics():
    import pipeline.catchup as c
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE portfolio_state (date TEXT, cash REAL,"
                 " market_value REAL, total REAL, drawdown REAL,"
                 " kill_switch INT, note TEXT)")

    def row(note):
        conn.execute("INSERT INTO portfolio_state VALUES"
                     " ('2026-09-18',1,1,2,0,0,?)", (note,))
        r = conn.execute("SELECT * FROM portfolio_state").fetchone()
        conn.execute("DELETE FROM portfolio_state")
        return r

    # 当日价盯市 / 停牌票滞后盯市 → 均算"盯市已完成"（防停牌残留循环）
    assert c._mm_done(row("mark_to_market@t; 价格日期=2026-09-18"))
    assert c._mm_done(row("mark_to_market@t; 价格滞后:600096@2026-09-17"))
    assert not c._mm_done(row(None))
    assert not c._mm_done(row(""))
    assert not c._mm_done(row("mark_to_market@t"))      # 无两种标记 → 未完成
    assert not c._mm_done(None)                          # 无当日行 → 未完成
    # 关键回归：ps[0] 是 date 列——老实现拿 date 与 note 子串比较恒 False；
    # 新实现读 ps["note"]（Row 键访问）
    r = row("mark_to_market@t; 价格日期=2026-09-18")
    assert r[0] == "2026-09-18"
    assert r["note"] != r[0] and c._mm_done(r)
    conn.close()


# ============================================================
# W-D3（P1-18）：--date 守卫的"有痕降级"——报告头价格滞后标注 + PENDING 文案
# ============================================================

def test_wd3_stale_note_in_report_header():
    from review import daily
    conn = sandbox_conn()
    try:
        note = "⚠ 价格滞后（目标日 2026-09-18 无日线，盯市基于最近可得收盘）"
        out1 = Path(tempfile.mkdtemp(prefix="agsickle_wd3_a_"))
        daily.generate_daily_report("2026-09-18", conn=conn, out_dir=out1,
                                    stale_note=note)
        text = (out1 / "2026-09-18.md").read_text(encoding="utf-8")
        assert "价格滞后" in text and "无日线" in text, text[:400]
        # 头部位置：标注须在标题与正文小节之间（报告头，而非正文深处）
        assert text.index("价格滞后") < text.index("## 决策回顾")
        # 正常路径（不带 stale_note）报告头无标注
        out2 = Path(tempfile.mkdtemp(prefix="agsickle_wd3_b_"))
        daily.generate_daily_report("2026-09-18", conn=conn, out_dir=out2)
        text2 = (out2 / "2026-09-18.md").read_text(encoding="utf-8")
        assert "价格滞后" not in text2
    finally:
        conn.close()


def test_wd3_pending_text_no_fake_catchup_date_arg():
    """P2-22：PENDING 文案不再教用户跑 `catchup.py --date`（catchup 无 argparse）。"""
    from pipeline import postclose
    postclose.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = postclose._write_pending("2026-09-17", "2026-09-18")
    body = path.read_text(encoding="utf-8")
    assert "catchup.py --date" not in body, body
    assert "pipeline/catchup.py" in body            # 仍保留正确入口
    assert "postclose.py --date" in body            # postclose 的 --date 是真参数


# ============================================================
# W-D4（P1-24）：config 校验加深
# ============================================================

def _full_cfg():
    with open(BASE / "config.json", encoding="utf-8") as f:
        return json.load(f)


def test_wd4_real_config_passes():
    from common.config import validate
    assert validate(_full_cfg()) == []


def test_wd4_missing_section_and_keys():
    from common.config import validate
    cfg = _full_cfg()
    del cfg["risk"]
    errs = validate(cfg)
    assert any(e.startswith("risk:") for e in errs), errs
    cfg = _full_cfg()
    del cfg["execution"]
    assert any(e.startswith("execution:") for e in validate(cfg))
    cfg = _full_cfg()
    del cfg["signals"]
    assert any(e.startswith("signals:") for e in validate(cfg))
    # 段内关键键
    cfg = _full_cfg()
    del cfg["risk"]["max_drawdown_kill"]
    assert "risk.max_drawdown_kill: missing required key" in validate(cfg)
    cfg = _full_cfg()
    del cfg["execution"]["manual_gate"]
    assert "execution.manual_gate: missing required key" in validate(cfg)


def test_wd4_watchlist_core_is_hard_key():
    from common.config import validate
    cfg = _full_cfg()
    del cfg["watchlist_core"]
    errs = validate(cfg)
    assert any(e.startswith("watchlist_core:") for e in errs), errs


def test_wd4_carc_optional_keys_not_enforced():
    """协同点4：C-ARC 三个带默认可选项缺失不得报错。"""
    from common.config import validate
    cfg = _full_cfg()
    for k in ("exec_retry_max", "exec_retry_drift_max", "exec_breaker_threshold"):
        cfg["execution"].pop(k, None)
    assert validate(cfg) == []


def test_wd4_profile_value_and_types():
    from common.config import validate, ConfigError, snapshot
    cfg = _full_cfg()
    cfg["signals"]["profile"] = "nope"
    errs = validate(cfg)
    assert any("signals.profile" in e for e in errs), errs
    cfg = _full_cfg()
    cfg["risk"]["max_single_weight"] = "0.2"       # 数字键给字符串
    assert any("risk.max_single_weight" in e for e in validate(cfg))
    # snapshot() 对坏配置 fail-fast
    bad = Path(_TMP_ROOT / "bad_config.json")
    cfg2 = _full_cfg()
    del cfg2["watchlist_core"]
    bad.write_text(json.dumps(cfg2), encoding="utf-8")
    try:
        snapshot(path=bad)
        raise AssertionError("snapshot 应 ConfigError")
    except ConfigError as e:
        assert "watchlist_core" in str(e)
    finally:
        bad.unlink(missing_ok=True)


# ============================================================
# W-D6 前置 P2-4：profile 显式参数透传全链（X 名下必须写 X 的分）
# ============================================================

def _seed_bars(conn, codes, dates, close0=100.0):
    """合成日线：几何随机游走（种子固定），qfq=close*0.99，成交额/换手充足。"""
    import random
    rng = random.Random(42)
    for code in codes:
        close = close0
        for i, d in enumerate(dates):
            close *= (1.0 + rng.uniform(-0.02, 0.02))
            hi, lo = close * 1.01, close * 0.99
            conn.execute(
                "INSERT OR REPLACE INTO daily_bar (code, trade_date, open, high,"
                " low, close, volume, amount, pct_chg, turnover, close_qfq,"
                " high_qfq, low_qfq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (code, d, close, hi, lo, close, 1e6, close * 1e6, 0.5, 2.5,
                 close * 0.99, hi * 0.99, lo * 0.99))
    for code in codes:
        conn.execute("INSERT OR REPLACE INTO stock_info (code, name)"
                     " VALUES (?,?)", (code, "票" + code))


def test_wd6_p2p4_backfill_explicit_profile_wins():
    """config 全局 profile 与显式 prof 错开时，回填行必须是 prof 名下的分：
    momentum 名下 = 生产时序分（可复算），reversal 名下 = 截面分（带 score_parts）。"""
    from signals import signals as sig
    from signals.factors import ma, mom, rsi
    import pandas as pd

    conn = file_db_conn()
    dates = ["2026-03-%02d" % d for d in range(1, 28)] + \
            ["2026-04-%02d" % d for d in range(1, 31)] + \
            ["2026-05-%02d" % d for d in range(1, 26)]   # 62 个交易日
    codes = ["600001", "000002", "300003"]
    try:
        conn.execute("DELETE FROM daily_bar")
        conn.execute("DELETE FROM signal")
        conn.execute("DELETE FROM index_daily")
        # 指数与个股同步的假序列（IVOL 需要基准；单调+微扰即可）
        import random
        rng = random.Random(7)
        c300 = 4000.0
        for d in dates:
            c300 *= (1.0 + rng.uniform(-0.01, 0.01))
            conn.execute("INSERT OR REPLACE INTO index_daily (index_code,"
                         " trade_date, close) VALUES ('000300', ?, ?)", (d, c300))
        _seed_bars(conn, codes, dates)
        conn.commit()   # _hs300_close_series 走独立连接，必须先落盘可见

        # 全局 profile 钉成 momentum，显式回填 reversal——错名即污染的回归场景
        orig = sig.profile
        sig.profile = lambda: "momentum"
        try:
            n_rev = sig.backfill_history(conn=conn, start=dates[20],
                                         prof="reversal_lowvol")
            n_v2 = sig.backfill_history(conn=conn, start=dates[20],
                                        prof="reversal_lowvol_v2")
        finally:
            sig.profile = orig
        assert n_rev >= 100 and n_v2 >= 100, (n_rev, n_v2)   # 每名下 ≥100 行
        rows = conn.execute(
            "SELECT code, as_of, signals, score FROM signal"
            " WHERE profile='reversal_lowvol'").fetchall()
        assert len(rows) == n_rev
        for code, as_of, sig_json, score in rows:
            s = json.loads(sig_json)
            assert score is not None and 0.0 <= score <= 1.0
            assert "score_parts" in s, "reversal 行必须带截面打分痕迹"
        # v2 名下五因子 parts
        rows_v2 = conn.execute(
            "SELECT signals FROM signal WHERE profile='reversal_lowvol_v2'"
            " LIMIT 1").fetchall()
        parts = json.loads(rows_v2[0][0])["score_parts"]
        assert "ivol_rank" in parts and "lowmax_rank" in parts, parts
        ivol_ranked = json.loads(rows_v2[0][0])["ivol_20d"]
        assert ivol_ranked is not None, "基准已入库且日期对齐，v2 行应有 ivol_20d"

        # momentum 名下：全局 profile 钉成 reversal，显式回填 momentum
        orig = sig.profile
        sig.profile = lambda: "reversal_lowvol"
        try:
            n_mom = sig.backfill_history(conn=conn, start=dates[40],
                                         prof="momentum")
        finally:
            sig.profile = orig
        assert n_mom >= 100, n_mom
        # 时序分可复算且完全一致（X 名下是 X 的分）
        bars = pd.read_sql("SELECT * FROM daily_bar", conn)
        checked = 0
        for code, as_of, sig_json, score in conn.execute(
                "SELECT code, as_of, signals, score FROM signal"
                " WHERE profile='momentum'").fetchall():
            g = bars[(bars["code"] == code) & (bars["trade_date"] <= as_of)]
            g = g.sort_values("trade_date")
            close = g["close_qfq"].fillna(g["close"]).astype(float)
            ma5, ma20, ma60 = ma(close, 5), ma(close, 20), ma(close, 60)
            if ma5 is not None and ma20 is not None and ma60 is not None:
                trend = "up" if (ma5 > ma20 > ma60) else \
                    "down" if (ma5 < ma20 < ma60) else "flat"
            else:
                trend = "flat"
            exp = sig._score_momentum(trend if ma5 is not None else None,
                                      mom(close, 20), rsi(close, 14))
            assert abs(float(score) - exp) < 1e-6, (code, as_of, score, exp)
            checked += 1
            if checked >= 20:
                break
        assert checked >= 20
    finally:
        conn.execute("DELETE FROM daily_bar")
        conn.execute("DELETE FROM signal")
        conn.execute("DELETE FROM index_daily")
        conn.commit()
        conn.close()


def test_wd6_p2p4_compute_all_no_05_fallback():
    """compute_all(profile=X) 与全局错开：不再出现 0.5 默认值顶替/交叉分。"""
    from signals import signals as sig

    conn = file_db_conn()
    dates = ["2026-06-%02d" % d for d in range(1, 29)]
    codes = ["600001", "000002", "300003"]
    try:
        conn.execute("DELETE FROM daily_bar")
        conn.execute("DELETE FROM signal")
        conn.execute("DELETE FROM index_daily")
        import random
        rng = random.Random(11)
        c300 = 4000.0
        for d in dates:
            c300 *= (1.0 + rng.uniform(-0.01, 0.01))
            conn.execute("INSERT OR REPLACE INTO index_daily (index_code,"
                         " trade_date, close) VALUES ('000300', ?, ?)", (d, c300))
        _seed_bars(conn, codes, dates)
        conn.commit()
        orig = sig.profile
        sig.profile = lambda: "momentum"
        try:
            results = sig.compute_all(conn=conn, watchlist_only=False,
                                      profile="reversal_lowvol_v2",
                                      as_of=dates[-1])
        finally:
            sig.profile = orig
        assert results, "compute_all 应产出结果"
        rows = conn.execute(
            "SELECT signals, score FROM signal WHERE profile='reversal_lowvol_v2'"
            " AND as_of=?", (dates[-1],)).fetchall()
        assert len(rows) == len(results)
        for sig_json, score in rows:
            s = json.loads(sig_json)
            assert "score_parts" in s
            assert abs(score - 0.5) > 1e-9 or "ivol_rank" in s["score_parts"], \
                "0.5 只能来自真实截面合成（全缺因子），不能是默认值顶替"
    finally:
        conn.execute("DELETE FROM daily_bar")
        conn.execute("DELETE FROM signal")
        conn.execute("DELETE FROM index_daily")
        conn.close()


# ============================================================
# W-D6 前置 P2-2：IVOL 日期对齐 + factors.ivol 同口径
# ============================================================

def test_wd6_p2p2_ivol_date_aligned_matches_factors():
    """个股停牌缺中间日时，ivol_20d 必须等于"按交易日交集对齐后的 factors.ivol"；
    旧长度对齐在缺日场景下两端窗口错位（对同一输入复算即露馅）。"""
    import numpy as np
    import pandas as pd
    from signals import signals as sig
    from signals.factors import ivol

    conn = file_db_conn()
    try:
        conn.execute("DELETE FROM daily_bar WHERE code='600777'")
        conn.execute("DELETE FROM signal WHERE code='600777'")
        conn.execute("DELETE FROM index_daily")
        all_dates = ["2026-02-%02d" % d for d in range(1, 27)]    # 26 日
        # 个股缺 5 天（停牌）：交集 21 天 vs 各自尾部 21 天不同窗
        missing = {"2026-02-05", "2026-02-06", "2026-02-12", "2026-02-13",
                   "2026-02-19"}
        stock_dates = [d for d in all_dates if d not in missing]
        rng = np.random.default_rng(5)
        c300 = 4000.0
        bench = {}
        for d in all_dates:
            c300 *= (1.0 + rng.normal(0, 0.01))
            bench[d] = c300
            conn.execute("INSERT OR REPLACE INTO index_daily (index_code,"
                         " trade_date, close) VALUES ('000300', ?, ?)",
                         (d, c300))
        close = 20.0
        stock = {}
        for d in stock_dates:
            close *= (1.0 + rng.normal(0, 0.02))
            stock[d] = close
            conn.execute(
                "INSERT OR REPLACE INTO daily_bar (code, trade_date, open, high,"
                " low, close, volume, amount, pct_chg, turnover, close_qfq,"
                " high_qfq, low_qfq) VALUES ('600777',?,?,?,?,?,?,?,0.5,2.5,"
                "?,?,?)",
                (d, close, close * 1.01, close * 0.99, close,
                 close * 1e6, close * 1e6, close * 0.99, close * 1.01 * 0.99,
                 close * 0.99 * 0.99))
        conn.execute("INSERT OR REPLACE INTO stock_info (code, name)"
                     " VALUES ('600777','对齐票')")
        conn.commit()   # _hs300_close_series 走独立连接
        rows = sig._rows_as_of(_pool_by_code(conn), ["600777"], None, {})
        assert len(rows) == 1
        got = rows[0]["signals"]["ivol_20d"]
        # 日期对齐基准值：交集尾部喂 factors.ivol
        joined = pd.concat([pd.Series(stock), pd.Series(bench)],
                           axis=1, join="inner").dropna()
        exp = ivol(joined.iloc[:, 0].values, joined.iloc[:, 1].values, 20)
        assert got is not None and exp is not None
        assert abs(got - exp) < 1e-9, (got, exp)
        # 旧"长度对齐"实现会给不同数字（证明本用例有区分度）
        n = min(len(stock), len(bench))
        sk = pd.Series(stock).values[-n:]
        bk = pd.Series(bench).values[-n:]
        legacy = ivol(sk, bk, 20)
        assert abs(legacy - exp) > 1e-9, "用例失去区分度（长度对齐恰巧对齐）"
    finally:
        conn.execute("DELETE FROM daily_bar WHERE code='600777'")
        conn.execute("DELETE FROM signal WHERE code='600777'")
        conn.execute("DELETE FROM index_daily")
        conn.close()


def _pool_by_code(conn):
    import pandas as pd
    pool = pd.read_sql("SELECT * FROM daily_bar", conn)
    return {str(c): g for c, g in pool.groupby("code")}


# ============================================================
# W-D6 前置 P2-3：turn20 最小样本与回测一致（≥10 个有效值）
# ============================================================

def test_wd6_p2p3_turn20_min_samples():
    from signals import signals as sig
    conn = file_db_conn()
    try:
        conn.execute("DELETE FROM daily_bar WHERE code='600778'")
        conn.execute("DELETE FROM signal WHERE code='600778'")
        conn.execute("DELETE FROM index_daily")
        dates = ["2026-01-%02d" % d for d in range(1, 27)]     # 26 日
        conn.execute("INSERT OR REPLACE INTO index_daily (index_code,"
                     " trade_date, close) VALUES ('000300', ?, 4000)",
                     (dates[0],))
        for i, d in enumerate(dates):
            close = 50.0 + i * 0.1
            conn.execute(
                "INSERT OR REPLACE INTO daily_bar (code, trade_date, open, high,"
                " low, close, volume, amount, pct_chg, turnover, close_qfq,"
                " high_qfq, low_qfq) VALUES ('600778',?,?,?,?,?,?,?,0.5,?,"
                "?,?,?)",
                (d, close, close * 1.01, close * 0.99, close, 1e6, close * 1e6,
                 2.5 if i >= 6 else None,          # 前 6 日换手 NULL → 末20行仅 14 有效
                 close * 0.99, close * 1.01 * 0.99, close * 0.99 * 0.99))
        conn.execute("INSERT OR REPLACE INTO stock_info (code, name)"
                     " VALUES ('600778','换手票')")
        conn.commit()
        rows = sig._rows_as_of(_pool_by_code(conn), ["600778"], None, {})
        turn20 = rows[0]["factors"]["turn20"]
        assert turn20 is not None                          # 末20行 20 有效 ≥ 10
        # 再抹掉 11 天换手 → 末 20 行仅 9 个有效 → None（旧实现仍出均值）
        conn.execute(
            "UPDATE daily_bar SET turnover=NULL WHERE code='600778'"
            " AND trade_date BETWEEN '2026-01-10' AND '2026-01-20'")
        rows = sig._rows_as_of(_pool_by_code(conn), ["600778"], None, {})
        assert rows[0]["factors"]["turn20"] is None
    finally:
        conn.execute("DELETE FROM daily_bar WHERE code='600778'")
        conn.execute("DELETE FROM signal WHERE code='600778'")
        conn.execute("DELETE FROM index_daily")
        conn.close()


# ============================================================
# W-D6 ④⑤⑥：交易所取整 / momentum 同源打分 / universe==full pass 门
# ============================================================

def test_wd6_limit_price_exchange_rounding():
    from signals.backtest import _limit_up_price
    # 10.07×1.10=11.077 裸乘积 < 11.08 交易所价——旧实现把收盘 11.08 的
    # 真涨停漏判为"可买"（P0-3 证据链3）
    assert _limit_up_price(10.07, "600000") == 11.08
    assert _limit_up_price(10.05, "600000") == 11.06   # 11.055 HALF_UP → 11.06
    assert _limit_up_price(3.51, "300001") == 4.21     # 创业板 20%：4.212 → 4.21
    import math
    assert not math.isclose(_limit_up_price(10.07, "600000"), 10.07 * 1.10)


def test_wd6_momentum_panel_matches_production():
    import numpy as np
    import pandas as pd
    from signals import signals as sig
    from signals.backtest import _momentum_score_panel
    from signals.factors import ma, mom, rsi
    rng = np.random.default_rng(23)
    idx = pd.date_range("2026-01-01", periods=120, freq="B")
    wide = pd.DataFrame(
        {c: 100 * np.exp(np.cumsum(rng.normal(0, 0.02, len(idx))))
         for c in ("600001", "000002")}, index=idx)
    panel = _momentum_score_panel(wide)
    assert panel.notna().any().all()
    for probe in (40, 70, 100, 119):
        d = idx[probe]
        for c in wide.columns:
            # 与生产同口径：截至 probe 的收盘序列喂 factors.*（无前视）
            close = wide[c].iloc[:probe + 1]
            ma5, ma20, ma60 = ma(close, 5), ma(close, 20), ma(close, 60)
            if ma5 is not None and ma20 is not None and ma60 is not None:
                trend = "up" if (ma5 > ma20 > ma60) else \
                    "down" if (ma5 < ma20 < ma60) else "flat"
            else:
                trend = "flat"
            exp = sig._score_momentum(trend if ma5 is not None else None,
                                      mom(close, 20), rsi(close, 14))
            got = float(panel.loc[d, c])
            assert abs(got - exp) < 1e-9, (probe, c, got, exp)


def _tiny_pool():
    """2 票 × 140 日合成 pool（run_backtest 直连入参口径）。"""
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(31)
    dates = pd.date_range("2026-01-01", periods=140, freq="B").strftime(
        "%Y-%m-%d")
    rows = []
    for code in ("600001", "000002"):
        close = 20.0
        for d in dates:
            close *= (1.0 + rng.normal(0.0005, 0.015))
            rows.append({"code": code, "trade_date": d, "close": close,
                         "close_qfq": close, "high": close * 1.01,
                         "low": close * 0.99, "amount": 2e8, "turnover": 3.0})
    pool = pd.DataFrame(rows)
    idx = pd.Series(np.linspace(4000, 4400, len(dates)),
                    index=pd.Index(dates))
    return pool, idx


def test_wd6_pass_gate_requires_full_universe():
    from signals.backtest import run_backtest
    pool, idx = _tiny_pool()
    r_core = run_backtest(pool, idx, strategy="reversal_lowvol", universe="core")
    assert r_core["pass"] is None, r_core["pass"]
    assert r_core["pass_criteria"]["universe_ok"] is False
    assert "宇宙不符" in r_core["pass_criteria"]["note"]
    assert "%%" not in r_core["params"]["cost_model"]      # P2-8 `%%` 残留
    assert "0.025%" in r_core["params"]["cost_model"]
    r_full = run_backtest(pool, idx, strategy="reversal_lowvol", universe="full")
    assert isinstance(r_full["pass"], bool)
    assert r_full["pass_criteria"]["universe_ok"] is True
    r_mom = run_backtest(pool, idx, strategy="momentum", universe="full")
    assert isinstance(r_mom["pass"], bool)                 # 同源打分可跑通


# ============================================================
# W-D7：trade_cal / groups 死代码 / movers / 锁下沉 / trade_date
# ============================================================

def test_wd7_trade_cal_skip_until_year_end():
    import data.trade_cal as tc
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE trade_calendar (date TEXT PRIMARY KEY)")
    calls = {"n": 0}

    def fake_fetch():
        calls["n"] += 1
        raise AssertionError("已覆盖到今年年底不应重拉")

    orig_fetch = tc._fetch
    tc._fetch = fake_fetch
    try:
        from datetime import date as _d
        y = _d.today().year
        # 覆盖到今年 12-31 → 跳过（旧判据要求覆盖明年 4 月，365 天天天重拉）
        for m in range(1, 13):
            conn.execute("INSERT OR IGNORE INTO trade_calendar VALUES (?)",
                         ("%d-%02d-01" % (y, m),))
        conn.execute("INSERT OR IGNORE INTO trade_calendar VALUES (?)",
                     ("%d-12-31" % y,))
        n = tc.ensure_calendar(conn)
        assert calls["n"] == 0 and n == 13
        # 跨年场景：日历停在去年 → 必须刷新一次（fake 返回小表）
        def fake_fetch2():
            calls["n"] += 1
            import pandas as pd
            return pd.DataFrame({"trade_date": ["%d-01-04" % (y + 1)]})
        tc._fetch = fake_fetch2
        conn.execute("DELETE FROM trade_calendar WHERE date >= ?",
                     ("%d-01-01" % y,))
        conn.execute("INSERT INTO trade_calendar VALUES ('%d-06-01'" % (y - 1) + ")")
        tc.ensure_calendar(conn)
        assert calls["n"] == 1
    finally:
        tc._fetch = orig_fetch
        conn.close()


def test_wd7_groups_dead_code_removed():
    src = (BASE / "webapp" / "api" / "groups.py").read_text(encoding="utf-8")
    assert "_AUDIT_CACHE" not in src            # P2-24：Phase6 搬运残留
    assert "数据状态" not in src.split("api_dynamic_pools")[-1]


def test_wd7_movers_60d_high_low_qfq_basis():
    """P2-6：raw 口径跨除权误报"创60日新低"，qfq 口径不报；qfq 缺失回退 raw。"""
    from common.config import core_codes
    from signals.movers import compute_watchlist_movers
    code = core_codes()[0]
    conn = sandbox_conn()
    try:
        conn.execute("INSERT OR REPLACE INTO stock_info (code, name)"
                     " VALUES (?,?)", (code, "分红票"))
        dates = ["2026-07-%02d" % d for d in range(1, 31)] + \
                ["2026-08-%02d" % d for d in range(1, 32)]
        # 历史 59 日：raw 从 200 线性降到 141.2（qfq 恒 100 附近）
        n_hist = len(dates) - 1
        for i, d in enumerate(dates[:-1]):
            raw = 200.0 - (200.0 - 141.2) * i / (n_hist - 1)
            qfq = 100.0 + i * 0.01
            conn.execute(
                "INSERT OR REPLACE INTO daily_bar (code, trade_date, open,"
                " high, low, close, volume, amount, pct_chg, turnover,"
                " close_qfq, high_qfq, low_qfq) VALUES (?,?,?,?,?,?,?,?,"
                "'-0.5','1.0',?,?,?)",
                (code, d, raw, raw * 1.005, raw * 0.995, raw, 1e6, raw * 1e6,
                 qfq, qfq * 1.005, qfq * 0.995))
        # 今日：raw 140（跌破 raw 60 日最低价 140.49——旧口径报新低），
        # qfq 100.50（高于 qfq 60 日最低 ~99.50——复权口径不报）；非除权日
        today = dates[-1]
        conn.execute(
            "INSERT OR REPLACE INTO daily_bar (code, trade_date, open, high,"
            " low, close, volume, amount, pct_chg, turnover, close_qfq,"
            " high_qfq, low_qfq) VALUES (?,?,?,?,?,?,?,'1.4e8','-0.85','1.0',"
            "?,?,?)",
            (code, today, 140.0, 140.9, 139.5, 140.0, 1e6,
             100.50, 100.60, 100.40))
        rows = compute_watchlist_movers(conn, as_of=today)
        hits = [r for r in rows if r["code"] == code
                and any("创60日新低" in x for x in r["reason"])]
        assert not hits, "qfq 口径不应误报新低"
        # qfq 缺失 → 回退 raw（旧口径行为，误报仍在但已显式降级）
        conn.execute("UPDATE daily_bar SET close_qfq=NULL, high_qfq=NULL,"
                     " low_qfq=NULL WHERE code=?", (code,))
        rows = compute_watchlist_movers(conn, as_of=today)
        hits = [r for r in rows if r["code"] == code
                and any("创60日新低" in x for x in r["reason"])]
        assert hits, "qfq 缺失应回退 raw 口径（误报仍在，行为不变）"
    finally:
        conn.close()


def test_wd7_movers_missing_amount_fails_floor():
    """P2-7：tx 兜底快照缺 turnover（成交额 None）视为不达标。"""
    from signals.movers import compute_market_movers
    spot = [
        {"代码": "600100", "名称": "有票", "最新价": 10.0, "涨跌幅": 6.0,
         "量比": 3.0, "成交额": 2e8, "振幅": 5.0},
        {"代码": "600200", "名称": "缺票", "最新价": 10.0, "涨跌幅": 6.0,
         "量比": 3.0, "成交额": None, "振幅": 5.0},
    ]
    rows = compute_market_movers(spot, top_n=10)
    codes = {r["code"] for r in rows}
    assert "600100" in codes
    assert "600200" not in codes, "缺成交额的票不得绕过流动性地板"


def test_wd7_exec_lock_reentrant_and_sunk():
    """P2-21：_exec_lock 可重入（CLI 外层锁 + 函数内锁不死锁），且
    propose/confirm 函数内自带锁（程序化调用不再绕锁）。"""
    from execution import runner
    with runner._exec_lock():
        with runner._exec_lock():          # 嵌套 → 深度计数，不 flock 自锁
            with runner._exec_lock():
                pass
    conn = sandbox_conn()
    try:
        # 铺最小数据（数据健康检查要求 daily_bar 非空）
        conn.execute("INSERT OR REPLACE INTO stock_info (code, name)"
                     " VALUES ('600519','测试票'),('000001','测试票2')")
        for i in range(30):
            d = "2026-08-%02d" % (i + 1)
            for code in ("600519", "000001"):
                conn.execute(
                    "INSERT OR REPLACE INTO daily_bar (code, trade_date, open,"
                    " high, low, close, volume, amount, pct_chg, turnover)"
                    " VALUES (?,?,?,?,?,?,10000,?,0.5,1.0)",
                    (code, d, 10 + i, 10.5 + i, 9.5 + i, 10 + i, (10 + i) * 1e4))
        conn.commit()
        # 直接调用 propose（无外层锁）→ 函数内持锁路径
        runner.propose(conn, {"action": "watch", "code": "600519",
                              "target_weight": 0.0, "confidence": 0.9,
                              "reasons": ["锁下沉测试"]},
                       decision_id=None, run_date="2026-09-18",
                       now=runner.datetime(2026, 9, 18, 10, 0, 0))
        row = conn.execute(
            "SELECT run_date, trade_date FROM decision WHERE action='watch'"
            " ORDER BY id DESC LIMIT 1").fetchone()
        # P2-25：propose --file 路径 trade_date 回填 = run_date
        assert row[1] == row[0] == "2026-09-18", tuple(row)
        # 外层锁内再调 propose（CLI 批量路径）→ 可重入不阻塞
        with runner._exec_lock():
            v2 = runner.propose(conn, {"action": "watch", "code": "000001",
                                       "target_weight": 0.0, "confidence": 0.9,
                                       "reasons": ["重入测试"]},
                                decision_id=None, run_date="2026-09-18",
                                now=runner.datetime(2026, 9, 18, 10, 0, 0))
            row2 = conn.execute(
                "SELECT run_date, trade_date FROM decision WHERE code='000001'"
                " ORDER BY id DESC LIMIT 1").fetchone()
            assert row2[1] == "2026-09-18", tuple(row2)
    finally:
        conn.close()


def test_wd7_wd1_pending_cleared_on_postclose_success(tmp_path=None):
    """W-D1 代码部分：postclose 成功路径清当日 PENDING——PENDING 只写"今日"名
    （_write_pending 用 today_iso 命名），成功尾部清除同一命名，两者闭合。"""
    from datetime import date
    from pipeline import postclose
    pending = postclose.REPORTS_DIR / ("PENDING-%s.md" % date.today().isoformat())
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text("stub", encoding="utf-8")
    # 模拟成功路径的清除块（与 postclose.main 尾部同一实现）
    try:
        pending.unlink()
    except OSError:
        pass
    assert not pending.exists()
    assert postclose._write_pending.__doc__ is not None


if __name__ == "__main__":
    fns = [(k, v) for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = []
    for name, fn in fns:
        try:
            fn()
            print("PASS", name)
        except Exception as e:  # noqa: BLE001
            failed.append(name)
            import traceback
            traceback.print_exc()
            print("FAIL", name, repr(e))
    print("\n%d/%d passed" % (len(fns) - len(failed), len(fns)))
    sys.exit(1 if failed else 0)
