"""Sprint 3.5 §-1 前置快速回测——验证借鉴 CloddsBot 4 项是否有效。

基线：复用 signals/backtest.py::run_backtest（已 fix 前视/可成交口径）。
T1-T4：在 backtest 上下文里走钩子，**不动 risk/engine.py / signals/signals.py / signals/factors.py 生产路径**。

启动条件：v1.4 momentum profile 跑出 ≥2 周数据 → 自动跑本脚本：

    .venv/bin/python3 signals/backtest_cloddsbot_validate.py

状态：占位脚本——baseline 已可跑（立刻出 v1.4 reversal_lowvol 真实数字），
T1-T4 函数签名齐全、内部 TODO 等 Sprint 3.5 §1-§4 实施后填实逻辑。

引用：docs/Sprint3.5-借鉴验证实施计划-2026-09-17.md §-1
"""
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import json
import logging
import logging.handlers
import time
from typing import Tuple

import pandas as pd

from data.fetcher import get_conn
from signals.backtest import ensure_benchmark, run_backtest

log = logging.getLogger("backtest_cloddsbot_validate")
log.setLevel(logging.INFO)
if not log.handlers:
    from common.logsetup import rotating_handler  # AGSICKLE_LOG_DIR 逃生门（Sprint4 W0-1）
    log.addHandler(rotating_handler("signal.log"))
log.propagate = False

# 占位常量（实施时由 Sprint 3.5 §1-§4 任务默认值定义同步）
# §3 任务 1
TRAILING_ATR_MULT_DEFAULT = 2.0
TRAILING_ATR_ACTIVATION_N = 2
# §2 任务 5a
BOARD_OPEN_COUNT_WEIGHT = 0.15
BOARD_OPEN_LOOKBACK = 20
# §1 任务 7
VOL_REGIME_WEIGHTS = {"low": 0.7, "mid": 1.0, "high": 1.3}
# §4 任务 8b
SOFT_DD_TRIGGER = -0.15
SOFT_DD_FACTOR = 0.7


def _load_pool_and_index(universe: str = "core") -> Tuple[pd.DataFrame, pd.Series, list]:
    """复用 signals/backtest.py::main 的 SQL 加载逻辑（不重写）。

    默认 universe=core（config.watchlist_core 策略真实池子），与 Sprint 1/2/3
    backtest 保持一致；universe=full 走 daily_bar 全库作 sanity check。
    """
    conn = get_conn()
    ensure_benchmark(conn)
    from common.config import core_codes as _core_codes  # 单一事实源
    if universe == "core":
        _u_codes = _core_codes()
    else:
        _u_codes = [str(r[0]) for r in
                    conn.execute("SELECT DISTINCT code FROM daily_bar").fetchall()]
    if _u_codes:
        _ph = ",".join("?" * len(_u_codes))
        pool = pd.read_sql(
            "SELECT code, trade_date, close, close_qfq, high, low, amount, turnover "
            "FROM daily_bar WHERE code IN (%s)" % _ph, conn, params=_u_codes)
    else:
        pool = pd.read_sql(
            "SELECT code, trade_date, close, close_qfq, high, low, amount, turnover "
            "FROM daily_bar", conn)
    idx = pd.read_sql("SELECT trade_date, close FROM index_daily "
                      "WHERE index_code='000300' ORDER BY trade_date", conn)
    conn.close()
    for col in ("close", "close_qfq", "high", "low", "amount", "turnover"):
        pool[col] = pd.to_numeric(pool[col], errors="coerce")
    idx_close = idx.set_index("trade_date")["close"].astype(float)
    return pool, idx_close, _u_codes


def run_baseline(pool: pd.DataFrame, idx_close: pd.Series) -> dict:
    """基线：v1.4 reversal_lowvol（不动生产路径）。

    通过判据：与 Sprint 3 修复后数字一致（年化 +31.81% / MDD -18.21% / Calmar 1.74 量级），
    否则 backtest 路径污染。复用 run_backtest，不重写。
    """
    log.info("跑基线 reversal_lowvol...")
    r = run_backtest(pool, idx_close, strategy="reversal_lowvol")
    sp = r.get("strategy_perf", {})
    log.info("  年化=%.2f%% MDD=%.2f%% pass=%s",
             sp.get("annual_return", 0) * 100,
             sp.get("max_drawdown", 0) * 100, r.get("pass"))
    return r


def run_t1_trailing_atr(pool: pd.DataFrame, idx_close: pd.Series,
                        mult: float = TRAILING_ATR_MULT_DEFAULT,
                        activation_n: int = TRAILING_ATR_ACTIVATION_N) -> dict:
    """T1 借鉴 #1 追踪止盈——量化 v1.4 reversal_lowvol MDD -41% 根因假设。

    占位状态：函数签名齐全，内部 TODO。
    实施时（Sprint 3.5 §3 任务 1）需要：
    1. 调用 signals/factors.py::trailing_atr(close, atr, mult) 草案实现
       （trailing peak 维护 + 浮盈 >activation_n × atr 激活 + close<trailing_stop 卖出）
    2. 在 run_backtest 的调仓逻辑里加 trailing_stop 触发模拟卖出
       （必须在不修改 signals/backtest.py 生产路径的前提下，复制 run_backtest
        函数体到一个仅供本脚本调用的 _run_backtest_with_trailing，参考 Sprint 3
        Fix-5 的 limit_halt 钩子模式）
    3. **不动 risk/engine.py::rule_22_take_profit**（生产路径隔离）

    验收（§-1.3）：v1.4 reversal_lowvol MDD 改善 ≥3 个百分点（-18.21% → ≤-15%）
                   或 Calmar 提升 ≥0.1。
    """
    log.info("T1 trailing_atr mult=%.2f activation_n=%d (占位，未实现)",
             mult, activation_n)
    return {
        "status": "TODO",
        "passed": None,
        "test_date": time.strftime("%Y-%m-%d"),
        "params": {"mult": mult, "activation_n": activation_n},
        "note": "Sprint 3.5 §3 任务 1 实施后填实逻辑",
        "acceptance": "MDD 改善 ≥3pp 或 Calmar 提升 ≥0.1",
    }


def run_t2_board_open_count(pool: pd.DataFrame, idx_close: pd.Series,
                            weight: float = BOARD_OPEN_COUNT_WEIGHT,
                            lookback: int = BOARD_OPEN_LOOKBACK) -> dict:
    """T2 借鉴 #5a 开板次数量化——看涨停"触板→开板→回封"因子是否有预测力。

    占位状态：函数签名齐全，内部 TODO。
    实施时（Sprint 3.5 §2 任务 5a）需要：
    1. 从 daily_bar OHLC 算每个交易日每只票的 board_open_count
       （涨停判定 high==limit_price + 开板判定 close<limit_price）
    2. 把 board_open_count 当作第 4 因子加入 score_reversal_lowvol_xs
       （权重 0.15，等权起步）
    3. **不动 signals/signals.py 生产路径**（在 backtest 上下文里走钩子）

    验收（§-1.3）：加入池子后 RankIC ≥ 0.02 或年化提升 ≥ 1 个百分点。
                   RankIC ≈ 0 → 任务从 Sprint 3.5 砍掉。
    """
    log.info("T2 board_open_count weight=%.2f lookback=%d (占位，未实现)",
             weight, lookback)
    return {
        "status": "TODO",
        "passed": None,
        "test_date": time.strftime("%Y-%m-%d"),
        "params": {"weight": weight, "lookback": lookback},
        "note": "Sprint 3.5 §2 任务 5a 实施后填实逻辑",
        "acceptance": "RankIC ≥ 0.02 或 年化提升 ≥ 1pp",
    }


def run_t3_vol_regime_weight(pool: pd.DataFrame, idx_close: pd.Series,
                             weights: dict = VOL_REGIME_WEIGHTS) -> dict:
    """T3 借鉴 #7 波动率自适应入场阈值——高波动环境是否要拉高 vol 因子权重。

    占位状态：函数签名齐全，内部 TODO。
    实施时（Sprint 3.5 §1 任务 7）需要：
    1. 从池子截面 ATR 分位数判 vol_regime=low/mid/high
       （截面 ATR 均值与历史中位数对比，或简单按截断分位）
    2. 在 _score_cross_section 等价逻辑里按 regime 切换 weights
       （backtest 上下文钩子，不动 signals/signals.py 生产路径）
    3. **约束**：仅 regime=normal 启用（D3 决策），shock/off 档禁用
       （避免与拥挤降权 + vol_target 三层耦合，审视者点出的"反馈振荡器"风险）

    验收（§-1.3）：高波动环境下 MDD 改善 ≥ 1 个百分点或 Calmar 提升 ≥ 0.05。
                   无改善 → high 默认值 1.3 下调到 1.15。
    """
    log.info("T3 vol_regime_weight=%s (占位，未实现)", weights)
    return {
        "status": "TODO",
        "passed": None,
        "test_date": time.strftime("%Y-%m-%d"),
        "params": {"weights": weights},
        "note": "Sprint 3.5 §1 任务 7 实施后填实逻辑；约束：仅 normal 启用",
        "acceptance": "高波动环境 MDD 改善 ≥ 1pp 或 Calmar 提升 ≥ 0.05",
    }


def run_t4_soft_dd_tier(pool: pd.DataFrame, idx_close: pd.Series,
                        trigger: float = SOFT_DD_TRIGGER,
                        factor: float = SOFT_DD_FACTOR) -> dict:
    """T4 借鉴 #8b 软减仓 15% 预警层——平滑回撤曲线 vs 纯 8% kill 跳变。

    占位状态：函数签名齐全，内部 TODO。
    实施时（Sprint 3.5 §4 任务 8b）需要：
    1. 在 portfolio_state 模拟路径加 drawdown 跟踪（基于 nav 序列）
    2. drawdown ≤ trigger 时把 target_weight 整体 ×factor
       （注意：这是仓位**收缩**而非**平仓**——区别于规则 5 8% kill）
    3. **不调 risk/engine.py::apply_kill_switch**（与规则 5 边界 D2 决策）
    4. **不动 risk/engine.py 生产路径**
    5. 联动规则 6：触发时 max_single_weight 从 0.20 临时降到 0.15
       （防集中度放大，见 Sprint 3.5 §4 改造点）

    验收（§-1.3）：回撤曲线峰值不劣于纯 kill（最大回撤幅度不增 +
                   回撤后净值恢复更快）。否则 factor 从 0.7 下调到 0.85，或暂缓实现。
    """
    log.info("T4 soft_dd_tier trigger=%.2f factor=%.2f (占位，未实现)",
             trigger, factor)
    return {
        "status": "TODO",
        "passed": None,
        "test_date": time.strftime("%Y-%m-%d"),
        "params": {"trigger": trigger, "factor": factor},
        "note": "Sprint 3.5 §4 任务 8b 实施后填实逻辑；联动规则 6 临时降至 0.15",
        "acceptance": "回撤峰值不劣于纯 kill + 净值恢复更快",
    }


def aggregate_report(baseline: dict, t1: dict, t2: dict,
                     t3: dict, t4: dict) -> str:
    """汇总 4 项 + 基线 → 输出 Markdown 报告。"""
    today = time.strftime("%Y-%m-%d")
    lines = [
        f"# CloddsBot 借鉴验证快速回测 · {today}",
        "",
        "> 引用：docs/Sprint3.5-借鉴验证实施计划-2026-09-17.md §-1",
        "",
        "## 基线（v1.4 reversal_lowvol）",
    ]
    if baseline and baseline.get("status") != "TODO":
        sp = baseline.get("strategy_perf", {})
        ann = sp.get("annual_return", 0) * 100
        mdd = sp.get("max_drawdown", 0) * 100
        calmar = abs(ann / mdd) if mdd != 0 else 0.0
        lines += [
            f"- 年化: {ann:.2f}%",
            f"- MDD: {mdd:.2f}%",
            f"- Calmar: {calmar:.2f}（年化/|MDD|）",
            f"- Pass: {baseline.get('pass', 'N/A')}",
            "",
            f"- 与 Sprint 3 修复后数字一致性校验：期望年化 31.81% / MDD -18.21%；"
            f"实测 {ann:.2f}% / {mdd:.2f}%（差异 >1pp 提示 backtest 路径污染）",
        ]
    else:
        lines.append("- 未跑（占位状态）")

    for name, t in [("T1 trailing_atr", t1),
                    ("T2 board_open_count", t2),
                    ("T3 vol_regime_weight", t3),
                    ("T4 soft_dd_tier", t4)]:
        lines += ["", f"## {name}"]
        if t.get("status") == "TODO":
            lines += [
                f"- 状态: 占位（Sprint 3.5 实施后填实）",
                f"- 参数: {t.get('params', {})}",
                f"- 验收口径: {t.get('acceptance', '')}",
                f"- 实施位置: {t.get('note', '')}",
            ]
        else:
            lines += [
                f"- 测试条件: {t}",
                f"- 结果: 待回测后填",
                f"- 与基线对比: 待回测后填",
                f"- 结论: 采纳 P1 / 降级 P2 / 砍掉",
            ]

    lines += [
        "",
        "## Sprint 3.5 任务调整建议",
        "- §1-§5 默认值更新: 待回测后填",
        "- 优先级重排: 待回测后填",
        "- 是否砍项: 待回测后填",
        "",
        "## 后续动作",
        "- 通过项 → §1-§5 默认值用回测校准值",
        "- 不通过项 → 移到 Sprint 4 阶段 2 重审",
        "- §-1 报告作为 Sprint 3.5 §1-§5 启动条件（未跑过不进入实施）",
    ]
    return "\n".join(lines) + "\n"


def main():
    """主入口：跑 baseline + T1-T4 + 汇总报告。

    占位脚本启动命令（profile 跑满 2 周后跑）：

        .venv/bin/python3 signals/backtest_cloddsbot_validate.py

    或指定 universe：

        .venv/bin/python3 signals/backtest_cloddsbot_validate.py --universe=core
        .venv/bin/python3 signals/backtest_cloddsbot_validate.py --universe=full
    """
    import argparse as _ap
    _ap_inst = _ap.ArgumentParser(add_help=False)
    _ap_inst.add_argument("--universe", choices=("core", "full"), default="core")
    _args, _ = _ap_inst.parse_known_args()

    log.info("=" * 60)
    log.info("Sprint 3.5 §-1 前置快速回测启动")
    log.info("=" * 60)

    # 1. 加载 universe（不复写 backtest.py::main 逻辑）
    pool, idx_close, codes = _load_pool_and_index(universe=_args.universe)
    log.info("加载池子: %d 只票（universe=%s）", len(codes), _args.universe)

    # 2. 基线（已可跑，立刻出 v1.4 reversal_lowvol 真实数字）
    baseline = run_baseline(pool, idx_close)

    # 3. 4 个子测试（占位状态——Sprint 3.5 §1-§4 实施后填实逻辑）
    t1 = run_t1_trailing_atr(pool, idx_close)
    t2 = run_t2_board_open_count(pool, idx_close)
    t3 = run_t3_vol_regime_weight(pool, idx_close)
    t4 = run_t4_soft_dd_tier(pool, idx_close)

    # 4. 汇总报告（Markdown 可读）
    today = time.strftime("%Y-%m-%d")
    report_md = aggregate_report(baseline, t1, t2, t3, t4)
    out_dir = BASE / "logs" / "backtest"
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"cloddsbot_validate_{today}.md"
    md_path.write_text(report_md, encoding="utf-8")
    log.info("Markdown 报告落盘: %s", md_path)

    # 5. JSON 副产物（机器可读，方便 §1-§5 实施时调默认值）
    json_out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "universe": _args.universe,
        "n_codes": len(codes),
        "baseline": baseline,
        "T1_trailing_atr": t1,
        "T2_board_open_count": t2,
        "T3_vol_regime_weight": t3,
        "T4_soft_dd_tier": t4,
        "todo": [
            "T1: 实现 trailing_atr 钩子（Sprint 3.5 §3 任务 1）",
            "T2: 实现 board_open_count 因子（Sprint 3.5 §2 任务 5a）",
            "T3: 实现 vol_regime_weight 分支（Sprint 3.5 §1 任务 7）",
            "T4: 实现 soft_dd_tier 钩子（Sprint 3.5 §4 任务 8b）",
        ],
    }
    json_path = out_dir / f"cloddsbot_validate_{today}.json"
    json_path.write_text(json.dumps(json_out, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    log.info("JSON 副产物落盘: %s", json_path)


if __name__ == "__main__":
    main()