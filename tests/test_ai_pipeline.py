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
from datetime import datetime, timedelta
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
    # daily_bar 日期取"昨天"：health_check 的滞后容差是自然日 3 天，
    # 写死日期会让本用例每周二以后必挂（lag>3 误报数据滞后）
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
