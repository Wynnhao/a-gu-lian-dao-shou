"""AI决策层与流水线单元测试：pytest 风格，亦可直接 `python3 tests/test_ai_pipeline.py` 运行。

不连真实 market.db：全部用 sqlite3.connect(":memory:") + data.fetcher.DDL 建表后插合成数据
（get_conn() 固定连真实库，本测试不使用它；validate 的黑名单用例通过显式参数注入，不查库）。
"""

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import json
from datetime import date, datetime, timedelta
import sqlite3

from data.fetcher import DDL
from ai import bundle as ai_bundle
from ai import decide


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    return conn


def base_decision(**over) -> dict:
    """一条合法的 buy 决策（600519 在 watchlist 内，权重/置信度/理由均合规）。"""
    d = {
        "action": "buy",
        "code": "600519",
        "target_weight": 0.10,
        "confidence": 0.70,
        "reasons": ["MA5>MA20>MA60 多头排列", "近3日利好新闻一则"],
        "risk_notes": ["RSI 偏高"],
        "order": {"side": "buy", "price": 1500.0, "shares": 100},
    }
    d.update(over)
    return d


# ---------------------------------------------------------------- validate：合法

def test_validate_ok_buy_and_normalization():
    raw = base_decision(mood="excited", source="llm")  # 多余键应被剔除
    ok, norm, errs = decide.validate(raw)
    assert ok, errs
    assert norm["action"] == "buy" and norm["code"] == "600519"
    assert norm["target_weight"] == 0.10 and norm["confidence"] == 0.70
    assert len(norm["reasons"]) == 2 and norm["risk_notes"] == ["RSI 偏高"]
    assert norm["order"] == {"side": "buy", "price": 1500.0, "shares": 100}
    assert "mood" not in norm and "source" not in norm


def test_validate_ok_hold_drops_order():
    raw = base_decision(action="hold", order={"side": "hold", "price": 0, "shares": 0})
    ok, norm, errs = decide.validate(raw)
    assert ok, errs
    assert "order" not in norm  # hold/watch 不应携带 order

    ok2, norm2, errs2 = decide.validate(base_decision(action="watch", code="300750",
                                                      order=None))
    assert ok2 and "order" not in norm2


def test_validate_ok_risk_notes_default_empty():
    raw = base_decision()
    del raw["risk_notes"]  # risk_notes 可缺省为 []
    ok, norm, errs = decide.validate(raw)
    assert ok, errs
    assert norm["risk_notes"] == []


# ---------------------------------------------------------------- validate：非法

def test_validate_missing_reasons():
    raw = base_decision(reasons=["只有一条理由"])
    ok, _, errs = decide.validate(raw)
    assert not ok and any("reasons" in e for e in errs)

    ok2, _, errs2 = decide.validate(base_decision(reasons=[]))
    assert not ok2 and any("reasons" in e for e in errs2)

    ok3, _, errs3 = decide.validate(base_decision(reasons=" MA5向上 "))
    assert not ok3 and any("reasons" in e for e in errs3)


def test_validate_confidence_out_of_range():
    for bad in (1.2, -0.05):
        ok, _, errs = decide.validate(base_decision(confidence=bad))
        assert not ok and any("confidence" in e for e in errs), bad


def test_validate_buy_without_order():
    raw = base_decision()
    del raw["order"]
    ok, _, errs = decide.validate(raw)
    assert not ok and any("order" in e for e in errs)

    ok2, _, errs2 = decide.validate(base_decision(order={"side": "sell", "price": 1.0,
                                                         "shares": 100}))
    assert not ok2 and any("side" in e for e in errs2)  # side 与 action 不一致


def test_validate_blacklist_code():
    bl = {"300750": (False, "测试黑名单：次新股")}
    ok, _, errs = decide.validate(base_decision(code="300750"), blacklist=bl)
    assert not ok and any("黑名单" in e for e in errs)
    # 不传 blacklist 时不做黑名单校验（执行层风控引擎兜底）
    ok2, _, errs2 = decide.validate(base_decision(code="300750"))
    assert ok2, errs2


def test_validate_sell_exempts_earnings_only_blacklist():
    """P0-5：sell 单对"仅业绩预告负面"黑名单豁免放行（止损出口不被焊死）；
    buy 单同样拦截仍拒；sell 单含 ST 等非业绩预告理由仍拒。"""
    sell_dec = base_decision(action="sell", code="600519",
                             order={"side": "sell", "price": 1400.0, "shares": 100})
    # ① sell + 仅业绩预告负面 → 豁免
    blEarn = {"600519": (False, "业绩预告负面（net=-3）")}
    ok, _, errs = decide.validate(sell_dec, blacklist=blEarn)
    assert ok, f"sell 应豁免业绩预告拦截，实得 errs={errs}"
    assert not any("黑名单" in e for e in errs), errs
    # ② buy + 仅业绩预告负面 → 仍拦
    ok2, _, errs2 = decide.validate(base_decision(code="600519"), blacklist=blEarn)
    assert not ok2 and any("黑名单" in e for e in errs2), errs2
    # ③ sell + 业绩预告负面 & ST 混合 → 仍拦
    blMixed = {"600519": (False, "业绩预告负面（net=-3）; ST标的")}
    ok3, _, errs3 = decide.validate(sell_dec, blacklist=blMixed)
    assert not ok3 and any("黑名单" in e for e in errs3), errs3


def test_validate_weight_out_of_range():
    for bad in (0.35, -0.01, "abc", None):
        ok, _, errs = decide.validate(base_decision(target_weight=bad))
        assert not ok and any("target_weight" in e for e in errs), bad


def test_validate_code_not_in_watchlist_and_bad_action():
    ok, _, errs = decide.validate(base_decision(code="000002"))  # 6位但不在 watchlist
    assert not ok and any("watchlist" in e for e in errs)
    ok2, _, errs2 = decide.validate(base_decision(code="60051"))  # 非6位
    assert not ok2 and any("code" in e for e in errs2)
    ok3, _, errs3 = decide.validate(base_decision(action="short"))
    assert not ok3 and any("action" in e for e in errs3)
    ok4, _, errs4 = decide.validate(["buy"])  # 非 dict
    assert not ok4 and errs4


# ---------------------------------------------------------------- save_decisions

def test_save_decisions_all_pass_inserts():
    conn = make_conn()
    snapshot = json.dumps({"run_date": "2026-09-11", "signals": []}, ensure_ascii=False)
    decisions = [base_decision(), base_decision(code="300750", action="watch",
                                                order={"side": "watch", "price": 0, "shares": 0})]
    ids = decide.save_decisions(conn, decisions, snapshot, "2026-09-11")
    assert len(ids) == 2 and all(isinstance(i, int) for i in ids)
    rows = conn.execute(
        "SELECT id, run_date, code, action, confidence, reasons, risk_notes, "
        "input_snapshot, status FROM decision ORDER BY id").fetchall()
    assert len(rows) == 2
    r0 = rows[0]
    assert r0[1] == "2026-09-11" and r0[2] == "600519" and r0[3] == "buy"
    assert r0[7] == snapshot  # input_snapshot 全文入库
    assert r0[8] == "proposed"  # 0.70 >= 0.60
    assert json.loads(r0[5]) == ["MA5>MA20>MA60 多头排列", "近3日利好新闻一则"]
    assert json.loads(r0[6]) == ["RSI 偏高"]
    conn.close()


def test_save_decisions_any_fail_aborts_all():
    conn = make_conn()
    decisions = [base_decision(),
                 base_decision(code="000001", confidence=1.5)]  # 第二条置信度越界
    ids = decide.save_decisions(conn, decisions, "{}", "2026-09-11")
    assert ids == []  # 整体放弃
    n = conn.execute("SELECT COUNT(*) FROM decision").fetchone()[0]
    assert n == 0  # 一条都不入库
    conn.close()


def test_save_decisions_report_only_status():
    conn = make_conn()
    ids = decide.save_decisions(conn, [base_decision(confidence=0.40)], "{}", "2026-09-11")
    assert len(ids) == 1
    status = conn.execute("SELECT status FROM decision WHERE id=?", (ids[0],)).fetchone()[0]
    assert status == "report_only"  # 0.40 < 0.60：只出报告不下单
    conn.close()


def test_save_decisions_blacklist_blocked_via_db():
    conn = make_conn()
    # 在 :memory: 库造一个黑名单票（上市仅3天），save_decisions 应经 check_blacklist 拦下整体
    conn.execute("INSERT INTO stock_info VALUES ('688801','N燧原-U','2026-09-09','now')")
    conn.commit()
    ids = decide.save_decisions(conn, [base_decision(code="688801")], "{}", "2026-09-11")
    assert ids == []
    assert conn.execute("SELECT COUNT(*) FROM decision").fetchone()[0] == 0
    conn.close()


# ---------------------------------------------------------------- build_bundle 空表降级

def test_build_bundle_empty_tables_degrades():
    conn = make_conn()
    b = ai_bundle.build_bundle(conn=conn)  # 不传 run_date -> 空表退回今天
    assert isinstance(b, dict)
    assert b["run_date"]  # 有默认日期（今天）
    assert b["health_issues"] == ["daily_bar 为空"]  # health_check 的空表提示
    assert "notice" in b and "只出报告" in b["notice"]
    assert b["signals"] == [] and "signals_missing" in b
    assert b["macro"] == {} and "macro_missing" in b
    assert b["positions"] == [] and b["portfolio_state"] is None
    assert b["recent_decisions"] == []
    assert b["data_quality"] == {} and "data_quality_missing" in b
    assert b["news"]["600519"] == [] and b["news"]["market"] == []
    assert b["paper_start_cash"] == 1000000.0
    # markdown 渲染同样不抛异常，且包含固定文案
    md = ai_bundle.bundle_to_markdown(b)
    assert "决策输出要求" in md and "JSON 数组" in md
    conn.close()


def test_build_bundle_with_data():
    # daily_bar 日期取"昨天"：health_check（修复批 D-0b 起）按交易日历判
    # "应有数据日"，本用例 trade_calendar 为空 → 降级周末口径，自然日昨天的
    # bar 在任何星期运行都不落后于 expected；写死日期会随时间漂移成滞后误报
    # （旧自然日 lag>3 容差已移除，注释随之更新）。
    bar_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    conn = make_conn()
    conn.execute("INSERT INTO daily_bar (code, trade_date, open, high, low, close,"
                 " volume, amount, pct_chg, turnover) VALUES "
                 "('600519',?,1,1,1,1,1,1,0,0)", (bar_date,))
    # signal 与日线同日：bundle 按最新 bar 日期取信号截面
    conn.execute("INSERT INTO signal (code, as_of, signals, score, profile) VALUES ('600519',?, '{\"ma_trend\":\"up\"}',0.7,'reversal_lowvol')", (bar_date,))
    conn.execute("INSERT INTO index_valuation VALUES ('000300','2026-09-11',12,0.5,1.3,0.6,4000)")
    conn.execute("INSERT INTO portfolio_state VALUES ('2026-09-11',900000,100000,1000000,0,0,'t')")
    conn.commit()
    b = ai_bundle.build_bundle(run_date="2026-09-11", conn=conn)
    assert b["run_date"] == "2026-09-11"
    assert b["health_issues"] == []
    assert "notice" not in b
    assert b["signals"][0]["code"] == "600519" and b["signals"][0]["signals"]["ma_trend"] == "up"
    assert b["macro"]["000300"]["pe"] == 12 and b["macro"]["000300"]["pe_pct"] == 0.5
    assert b["portfolio_state"]["total"] == 1000000.0
    assert b["data_quality"] == {"600519": bar_date}
    conn.close()


def test_build_bundle_injects_bond_etf_signals():
    """Sprint 2 任务 1：build_bundle 注入 bond_etf_signals（缺表时降级为 reason）。"""
    conn = make_conn()
    try:
        b = ai_bundle.build_bundle(run_date="2026-09-11", conn=conn)
        # 空表 → dict 内 reason 写明降级路径
        assert "bond_etf_signals" in b
        assert b["bond_etf_signals"]["bond_yield"] == {}
        assert b["bond_etf_signals"]["etf_share"] == {}
        assert "均为空" in b["bond_etf_signals"]["reason"]

        # 填一行国债 + ETF → 应出现在 bundle
        conn.execute(
            "INSERT INTO index_bond_yield VALUES ('10Y_CN','2026-09-11',2.55,-20.0,'em')")
        conn.execute(
            "INSERT INTO index_etf_share VALUES ('510300','2026-09-11',1000000.0,-3.5,'em')")
        conn.commit()
        b = ai_bundle.build_bundle(run_date="2026-09-11", conn=conn)
        assert b["bond_etf_signals"]["bond_yield"]["yield"] == 2.55
        assert b["bond_etf_signals"]["bond_yield"]["delta_20d_bp"] == -20.0
        assert b["bond_etf_signals"]["etf_share"]["510300"]["pct_chg_1d"] == -3.5
    finally:
        conn.close()


# ---------------------------------------------------------------- 模板自洽

def test_template_passes_validate():
    for i, item in enumerate(decide.template()):
        ok, norm, errs = decide.validate(item)
        assert ok, "template[%d] 不合法: %s" % (i, errs)
        if item["action"] in ("buy", "sell"):
            assert norm["order"]["side"] == item["action"]
        else:
            assert "order" not in norm


def test_load_and_save_rejects_bad_file():
    """load_and_save 对 JSON 解析失败应安全放弃（返回 []）；save 层用 :memory: 库验证。"""
    import tempfile
    d = Path(tempfile.mkdtemp())
    bad = d / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    assert decide.load_and_save(bad, "2026-09-11") == []

    good = d / "good.json"
    good.write_text(json.dumps([base_decision()], ensure_ascii=False), encoding="utf-8")
    # load_and_save 会连真实库，这里只验证 save_decisions 层面等价路径，不调用它
    conn = make_conn()
    ids = decide.save_decisions(conn, json.loads(good.read_text(encoding="utf-8")),
                                good.read_text(encoding="utf-8"), "2026-09-11")
    assert len(ids) == 1
    conn.close()


# ---------------------------------------------------------------- Fix-5：earnings confidence 加成

def test_validate_earnings_confidence_boost_capped():
    """Fix-5：近 3 日净分 ≥ +2 → confidence +0.1（封顶 1.0）；净分不足不加成。"""
    earn_pos = {"600519": {"positive": 3, "negative": 0, "net": 3}}
    ok, norm, errs = decide.validate(base_decision(confidence=0.70),
                                     earnings_events=earn_pos)
    assert ok, errs
    assert abs(norm["confidence"] - 0.80) < 1e-9

    # 加成封顶 1.0
    ok2, norm2, _ = decide.validate(base_decision(confidence=0.95),
                                    earnings_events=earn_pos)
    assert ok2 and norm2["confidence"] == 1.0

    # 净分 +1 不达阈值、无数据、净分为负 → 不加成
    ok3, norm3, _ = decide.validate(
        base_decision(confidence=0.70),
        earnings_events={"600519": {"positive": 1, "negative": 0, "net": 1}})
    assert ok3 and abs(norm3["confidence"] - 0.70) < 1e-9
    ok4, norm4, _ = decide.validate(base_decision(confidence=0.70),
                                    earnings_events={})
    assert ok4 and abs(norm4["confidence"] - 0.70) < 1e-9
    ok5, norm5, _ = decide.validate(
        base_decision(confidence=0.70),
        earnings_events={"600519": {"positive": 0, "negative": 5, "net": -5}})
    assert ok5 and abs(norm5["confidence"] - 0.70) < 1e-9


# ---------------------------------------------------------------- P2⑮（全量打包批 2026-09-20）：realized_pnl 遗漏用例

def _add_filled_trade(conn, d, code, side, amount, shares):
    """status='filled' 成交流水（amount=现金净流口径，买入含费用）。"""
    conn.execute(
        "INSERT INTO trade (trade_date, code, name, side, price, shares, amount,"
        " order_id, status, decision_id, shots, confirmed_by, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (d, code, "票" + code, side, amount / max(shares, 1), shares, amount,
         "ord", "filled", None, "[]", "test", d + "T09:35:00"))


def test_p2c15_realized_pnl_orphan_sell():
    """P2⑮：orphan 卖出——无持仓（或超出持仓）部分的卖出无成本可结转，
    按卖出额比例全额计入 realized_pnl 并在 note 留痕降级，不静默失真。
    部分孤儿：买 100@成本 10、卖 150——100 股按移动成本结转、50 股全额计入。"""
    conn = make_conn()
    try:
        _add_filled_trade(conn, "2026-09-10", "600001", "buy", 1000.0, 100)
        _add_filled_trade(conn, "2026-09-11", "600001", "sell", 1800.0, 150)
        conn.commit()
        b = ai_bundle.build_bundle(conn=conn)
        # matched 100 股：1800×(100/150) − 10×100 = 200；orphan 50 股：1800×(50/150)=600
        assert abs(b["realized_pnl"] - 800.0) < 1e-6, b["realized_pnl"]
        assert "1 笔无持仓卖出" in b["realized_pnl_note"], b["realized_pnl_note"]
        assert abs(b["net_cash_outlay"] - 800.0) < 1e-6
        # 完全孤儿（无任何买入）：全额计入 + 计数留痕
        conn.execute("DELETE FROM trade")
        _add_filled_trade(conn, "2026-09-12", "000002", "sell", 500.0, 50)
        conn.commit()
        b2 = ai_bundle.build_bundle(conn=conn)
        assert abs(b2["realized_pnl"] - 500.0) < 1e-6, b2["realized_pnl"]
        assert "1 笔无持仓卖出" in b2["realized_pnl_note"]
    finally:
        conn.close()


def test_p2c15_realized_pnl_mixed_lots_across_gaps():
    """P2⑮：混源跨缺口——多码交错 + 同码跨日期缺口（09-05→09-20）+ 同日
    买卖对（(trade_date, id) 排序保证 buy 先于 sell 结转）的移动平均成本
    配比逐段手算核对；全部平仓后 realized_pnl 与净投入现金副口径一致。"""
    conn = make_conn()
    try:
        # 600001：两段建仓 → 部分平仓 → 跨缺口再建仓 → 全平；000002 交错其中
        _add_filled_trade(conn, "2026-09-01", "600001", "buy", 1000.0, 100)   # lot avg 10.0
        _add_filled_trade(conn, "2026-09-02", "000002", "buy", 500.0, 50)     # lot avg 10.0
        _add_filled_trade(conn, "2026-09-02", "600001", "buy", 1200.0, 100)   # lot 200 @ 11.0
        _add_filled_trade(conn, "2026-09-05", "600001", "sell", 1875.0, 150)  # +1875−1650=225
        # —— 日期缺口（09-06..09-19 无成交）——
        _add_filled_trade(conn, "2026-09-20", "600001", "buy", 1500.0, 100)   # lot 150 @ 2050/150
        _add_filled_trade(conn, "2026-09-21", "600001", "buy", 700.0, 50)     # 同日 buy 先于 sell
        _add_filled_trade(conn, "2026-09-21", "600001", "sell", 720.0, 50)    # +720−700=20
        _add_filled_trade(conn, "2026-09-25", "600001", "sell", 2250.0, 150)  # +2250−2050=200
        _add_filled_trade(conn, "2026-09-25", "000002", "sell", 550.0, 50)    # +550−500=50
        conn.commit()
        b = ai_bundle.build_bundle(conn=conn)
        assert abs(b["realized_pnl"] - 495.0) < 1e-6, b["realized_pnl"]
        assert "无持仓卖出" not in b["realized_pnl_note"], b["realized_pnl_note"]
        # 全部平仓 → 与净投入现金副口径一致（W-C2 一致性）
        assert abs(b["realized_pnl"] - b["net_cash_outlay"]) < 1e-6, \
            (b["realized_pnl"], b["net_cash_outlay"])
    finally:
        conn.close()


# ---------------------------------------------------------------- 批次6：研究证据参考字段


def _seed_pair_series(conn, codes=("600100", "600200"), n=300):
    """构造 ρ=-1 反相关双票 close_qfq 序列；末日 a 跌 2% / b 涨 2%（触发提示）。"""
    d0 = date(2025, 1, 1)
    ca = cb = 100.0
    for i in range(n):
        d = (d0 + timedelta(days=i)).isoformat()
        ra = -0.02 if i == n - 1 else (0.01 if i % 2 == 0 else -0.01)
        rb = -ra
        ca *= (1 + ra)
        cb *= (1 + rb)
        for code, c in ((codes[0], ca), (codes[1], cb)):
            conn.execute(
                "INSERT OR REPLACE INTO daily_bar (code, trade_date, open, high,"
                " low, close, volume, amount, pct_chg, turnover, source, close_qfq)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (code, d, c, c, c, c, 1e6, c * 1e6, 0.0, 1.0, "em", c))
    conn.commit()


def test_bundle_hedge_evidence_whitelist_and_hint():
    """批次6：稳定配对清单复用批次1 月度重估（ρ=-1 合成对必入选）；末日
    A 跌 2%/B 涨 2% → 触发 watch 提示（fall=A, rise=B）。"""
    conn = make_conn()
    try:
        # 配对池=watchlist_core（§3.2）：合成对用真实 core 池前两只票
        ca, cb = sorted(ai_bundle.core_codes())[:2]
        _seed_pair_series(conn, codes=(ca, cb))
        ev = ai_bundle.hedge_evidence(conn, evidence_date="2025-10-15")
        assert len(ev["pairs"]) == 1, ev
        a, b = ev["pairs"][0]
        assert a == ca and b == cb          # a<b 列序
        assert ev["detail"][0]["rho_250d"] < -0.30
        assert ev["effective_month"] == "2025-10"
        assert len(ev["hints"]) == 1, ev["hints"]
        h = ev["hints"][0]
        assert h["fall"] == ca and h["rise"] == cb
        assert h["fall_ret"] <= -0.01 and h["rise_ret"] > 0
        assert "FAIL" in ev["note"]                      # 研究结论披露在位
    finally:
        conn.close()


def test_bundle_chan_evidence_lines_accounting():
    """批次6：缠论单行摘要——有标签行 ≤80 字符且以票代码开头；无数据票计
    no_label；行数+无标签数 = 票数（口径守恒）。"""
    from data.fetcher import DDL as _DDL
    conn = sqlite3.connect(":memory:")
    conn.executescript(_DDL)
    try:
        d0 = date(2025, 1, 1)
        for i in range(130):
            d = (d0 + timedelta(days=i)).isoformat()
            conn.execute("INSERT OR REPLACE INTO trade_calendar (date) VALUES (?)", (d,))
            c = 10.0 + (i % 3) * 0.1
            conn.execute(
                "INSERT OR REPLACE INTO daily_bar (code, trade_date, open, high,"
                " low, close, volume, amount, pct_chg, turnover, source, close_qfq)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("600300", d, c, c + 0.05, c - 0.05, c, 1e6, c * 1e6, 0.0, 1.0,
                 "em", c))
        conn.commit()
        cs = ai_bundle.chan_evidence_lines(conn, codes=["600300", "600999"])
        assert len(cs["lines"]) + cs["no_label"] == 2
        for ln in cs["lines"]:
            assert len(ln) <= 80 and ln.startswith("600300")
        assert "归档" in cs["note"]                      # 软证据披露在位
    finally:
        conn.close()


def test_bundle_markdown_evidence_sections_and_trims():
    """批次6：渲染节 + 两级降级——evidence_detail=False 折叠明细为计数；
    include_evidence=False 整节舍弃（降级链末位）。"""
    b = {"run_date": "2026-09-20", "prompt_version": ai_bundle.PROMPT_VERSION,
         "hedge_pairs": {"effective_month": "2026-09", "estimated_at": "2026-08-31",
                         "pairs": [["000895", "002463"]],
                         "detail": [{"a": "000895", "b": "002463", "rho_250d": -0.353}],
                         "hints": [{"date": "2026-09-18", "fall": "000895",
                                    "rise": "002463", "fall_ret": -0.021,
                                    "rise_ret": 0.012}],
                         "note": "对冲配对研究批 Gate H0 无分辨率 FAIL 归档"},
         "chan_structure": {"lines": ["000895 三买持仓(确认2026-06-05,3日前)｜笔:up 分型:顶2日前 中枢:[8.1,9.9]上"],
                            "no_label": 50, "note": "缠论机械线 R3 已归档"}}
    md_full = ai_bundle.bundle_to_markdown(b)
    assert "研究证据参考" in md_full and "000895↔002463" in md_full
    assert "三买持仓" in md_full and "配对触发观察" in md_full
    md_fold = ai_bundle.bundle_to_markdown(b, evidence_detail=False)
    assert "研究证据参考" in md_fold and "预算降级" in md_fold
    assert "确认2026-06-05" not in md_fold                # 明细已折叠（单行摘要不在）
    md_none = ai_bundle.bundle_to_markdown(b, include_evidence=False)
    assert "研究证据参考" not in md_none and "对冲稳定配对" not in md_none


def test_build_bundle_evidence_fields_degrade_on_empty():
    """批次6：空库 build_bundle 不抛异常——对冲节落"无数据"披露，缠论节
    全计 no_label；渲染含研究证据参考节且对冲节给出降级说明。"""
    conn = make_conn()
    try:
        b = ai_bundle.build_bundle(run_date="2026-09-20", conn=conn)
        hp = b["hedge_pairs"]
        assert "pairs" in hp and (hp.get("note") or hp.get("error"))
        cs = b["chan_structure"]
        assert "no_label" in cs or "error" in cs
        md = ai_bundle.bundle_to_markdown(b)
        assert "研究证据参考" in md
    finally:
        conn.close()


# ---------------------------------------------------------------- 直接运行入口

def _main() -> int:
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in fns:
        try:
            fn()
            print("PASS %s" % name)
        except Exception as e:  # noqa: BLE001
            failed.append(name)
            import traceback
            print("FAIL %s: %s: %s" % (name, type(e).__name__, e))
            traceback.print_exc()
    print("\n%d/%d passed" % (len(fns) - len(failed), len(fns))
          + ("" if not failed else "; failed: %s" % ", ".join(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
