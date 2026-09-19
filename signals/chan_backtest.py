"""缠论 R3 批次 2a —— 组合回测引擎 + 受控 momentum 基线腿（§3.3 受控对照腿）。

表述红线（§3.3 原文）：本模块的 momentum 腿是**受控对照腿**，不是生产 momentum
profile（signals/backtest.py 的生产同源打分策略），结题结论不得混称。

冻结口径出处（docs/缠论R3施工方案-2026-09-19.md，批次 0 commit 后不可变）：
- §3.6-8  出场计数：D0 = 入场日（确认日次日开盘买入、停牌顺延至下一有 bar 交易日
          开盘）；20 日出场按票内有 bar 交易日计数、第 20 根收盘卖出；停牌期间不
          计数不交易、恢复后从停牌前状态继续。
- §3.6-10 基线腿：动量 = 截至上月末最后交易日 close_qfq 的 20 日收益
          （close_qfq[t]/close_qfq[t−20] − 1）；每月最后交易日收盘计算信号、次月
          首个有 bar 交易日开盘调仓；Top5 等权、每笔目标权重 20% 总资产；已持有票
          不重复计费（只对变动部分扣 0.15%/边）；池内 20 日历史不足的票剔除；
          不足 5 只时余下为现金。
- §3.6-11 信号腿资金：每笔目标权重 20% 总资产、按执行日开盘价（open_qfq）成交；
          不做 100 股取整（连续份额）；现金不计收益；持仓盯市用当日 close_qfq，
          当日无 bar 沿用最近有 bar 收盘。
- §3.6-12 反前视：沿用 signals/backtest.py run_backtest 日循环模式——每个交易日
          先结算旧持仓当日收益并更新 NAV，再执行当日调仓；信号次日开盘 / 基线次月
          开盘执行，构造上无前视。
- §3.6-13 Gate 3 的 MDD 差 = 信号腿 MDD − 基线腿 MDD（日频 NAV、测试窗、净口径），
          本引擎为双腿提供同源日收益/NAV 序列（批次 3 裁决用）。
- 成本：进/出各 0.15%（§3.2），只对实际成交名义计。

交易指令模型（两类腿共用本引擎；批次 2b 信号腿以"事件指令"接入，不须改引擎）：
    Instruction(date, code, side, price_type, target=False)
    side ∈ {"buy","sell"}；price_type ∈ {"open","close"}（执行价 = open_qfq/close_qfq）。
    - 事件指令（信号腿，target=False）：buy = 新开/加仓，名义 = 20% × NAV_ref；
      sell = 全仓退出。信号腿的入场（确认次日开盘 buy@open）、顶分型出场
      （确认次日开盘 sell@open）、20 日出场（第 20 根收盘 sell@close）都由生成层
      翻译成本模型指令；sell 无持仓时为无操作（no-op）——同一位置的双出场指令
      先到者成交、后到者无害。
    - 月末调仓指令（基线腿，target=True）：引擎按 |目标市值 − 当前市值| 结算变动，
      变动为零不成交、不计费（§3.6-10"已持有票不重复计费"的引擎语义）；buy 的
      目标市值 = 20% × NAV_ref（留存票据此自动增/减持回 20%），sell 的目标市值 = 0。
    - 停牌顺延：指令在票内首个 ≥ date 的 bar 成交（open = 该 bar 开盘、close = 该
      bar 收盘），与 §3.6-8 顺延语义一致；直至数据末尾仍无 bar → 记入 skipped。
    - 同票 target 指令"后到取消先到"：早指令到其成交日仍未成交（停牌跨月）而同票
      已有更晚日期的 target 指令时，取消早指令（月末目标组合以最新调仓月意愿为准，
      防"上月卖出顺延误杀本月重新入选"）。事件指令永不取消。
    - 换仓重叠（掉出票停牌、其卖出顺延未成交）时，target 买入遇 5 只上限 → 顺延至
      该票下一 bar 重试（槽位由顺延卖出释放，§3.6-8 顺延精神）；若同票已存在日期
      晚于该指令、且不晚于本次重试日的指令（较新意愿已到决定时点）→ 取消；更晚
      指令仍在未来 → 先到先得、继续顺延；直至数据末尾仍无空位 → skipped。
      事件买入遇上限 → assert 抛错（信号腿"最大同时持仓 5 只、超额丢弃"由生成层
      保证，引擎越限即不变式违规）。

日循环记账（§3.6-12 反前视，逐日）：
    1) 先结算：卖出按其价格类型成交入现金；留存持仓按当日 close_qfq 盯市（当日无
       bar 沿用最近有 bar 收盘、零收益）；
    2) 再执行买入：名义基准 NAV_ref = 上一结算日 NAV（= 执行时点前最近可得总资产）；
    3) NAV = 现金 + Σ(股数 × 盯市价)；日收益 = NAV_T/NAV_{T−1} − 1；现金零收益。
    恒等式（§3.6-12 冻结语义的逐项体现）：入场日该票 PnL = 名义 × (close_T/open_T − 1)、
    现金支出 = 名义 × (1+0.0015)；sell@open 日该票当日无敞口、现金收入 = 成交市值 ×
    (1−0.0015)；sell@close 日先计全天收益、再按 close_T × (1−0.0015) 结算现金。

引擎层消歧（实现中新钉死，§3.6-14 同例，提请批次 3 结题时确认冻结）：
    a. 名义基准 NAV_ref = 上一结算日 NAV（§3.6-10/11 未钉死"总资产"的基准时点；
       执行日开盘时点的最近可得总资产，无前视）。
    b. 允许负现金：5×20% = 100% 叠加双边成本使负现金成为冻结权重的必然结果；
       负余额同样零收益（隐含零利率融资，研究口径）。
    c. 换仓次数 = 有实际成交的调仓月数。
    d. 动量按票内 bar 序取 close_qfq[t]/close_qfq[t−20] − 1（§3.6-14b"票内日期
       序列"精神；跨停牌的票其 20 日窗跨更长的日历段）。
    e. 月末/次月边界按 trade_calendar 自然月；首月调仓的上月末信号可早于测试窗
       （信号时点的已知信息，非前视）。
    f. 事件 buy 对已持仓票 = 加仓语义（上限只数不同代码计）；是否重复入场由信号
       生成层（§3.3"持仓中同票重复信号忽略"）保证。
    g. 换仓重叠顺延：掉出票停牌时其卖出顺延、槽位未即时释放，新入票的 target 买入
       顺延至该票下一 bar 重试（§3.6-10 未预见此重叠；顺延而非丢弃，与 §3.6-8
       "停牌顺延"精神一致，保证 Top5 目标组合最终完整入账）。

指标同源复用 signals.rotation 的 metrics / seg_total（§4 指定）；数据加载唯一入口
signals.chan_data.load_core_bars（sqlite URI mode=ro，只读）。本批不做信号生成
（批次 2b）、不做 gate 裁决（批次 3）、不做参数扫描。

运行：.venv/bin/python3 -m signals.chan_backtest
      只读跑基线腿测试窗（2024-07-01 起）全窗回测，打印累计/年化/MDD/换仓/逐段
      收益，并重复跑第二遍断言日收益序列逐位一致（可复现性验收），exit 0。
"""
from __future__ import annotations

import bisect
import sys
from collections import namedtuple
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from signals.chan_data import TEST_START, load_core_bars  # noqa: E402
from signals.rotation import metrics, seg_total  # noqa: E402

COST_RATE = 0.0015        # 进/出各 0.15%（§3.2；§3.6-10 只对变动部分计）
TARGET_WEIGHT = 0.20      # 每笔目标权重 20% 总资产（§3.6-10/11）
MAX_POSITIONS = 5         # 持仓上限 5 只（§3.3；越限 assert 抛错）
TOP_N = 5                 # 基线腿 Top5（§3.6-10）
MOM_WINDOW = 20           # 动量窗口 20 日（§3.6-10）
INITIAL_NAV = 1.0

VALID_SIDES = ("buy", "sell")
VALID_PRICE_TYPES = ("open", "close")

Instruction = namedtuple("Instruction",
                         ["date", "code", "side", "price_type", "target"],
                         defaults=(False,))


# ---------------------------------------------------------------- 校验

def _validate_bars(bars_by_code, calendar):
    """从严校验并展开为 {code: {dates, open, close, idx}}（引擎只用 open_qfq/close_qfq）。"""
    cal_list = list(calendar)
    if cal_list != sorted(cal_list):
        raise ValueError("trade_calendar 未按升序给出")
    cal_set = set(cal_list)
    if len(cal_set) != len(cal_list):
        raise ValueError("trade_calendar 含重复日期")
    series = {}
    for code in sorted(bars_by_code):
        df = bars_by_code[code]
        for col in ("trade_date", "open_qfq", "close_qfq"):
            if col not in df.columns:
                raise ValueError(f"{code} 缺少列 {col}")
        dates = list(df["trade_date"])
        if dates != sorted(dates):
            raise ValueError(f"{code} bar 未按 trade_date 升序")
        missing = [d for d in dates if d not in cal_set]
        if missing:
            raise ValueError(f"{code} 存在不在 trade_calendar 中的 bar 日期: {missing[:5]}")
        opens = df["open_qfq"].astype(float)
        closes = df["close_qfq"].astype(float)
        if bool(opens.isna().any()) or bool(closes.isna().any()):
            raise ValueError(f"{code} open_qfq/close_qfq 含空值，引擎从严拒绝")
        series[code] = {"dates": dates,
                        "open": [float(x) for x in opens],
                        "close": [float(x) for x in closes],
                        "idx": {d: i for i, d in enumerate(dates)}}
    if not series:
        raise ValueError("bars_by_code 为空")
    return series


def _check_instruction(ins):
    if ins.side not in VALID_SIDES:
        raise ValueError(f"非法 side: {ins.side!r}（{ins}）")
    if ins.price_type not in VALID_PRICE_TYPES:
        raise ValueError(f"非法 price_type: {ins.price_type!r}（{ins}）")


# ---------------------------------------------------------------- 引擎

def run_portfolio_backtest(bars_by_code, calendar, instructions, *,
                           test_start=TEST_START, initial_nav=INITIAL_NAV,
                           cost_rate=COST_RATE, target_weight=TARGET_WEIGHT,
                           max_positions=MAX_POSITIONS) -> dict:
    """组合回测引擎：接受事件/月末两类指令列表，日频记账（§3.6-11/12）。

    返回 {"returns", "nav", "cash", "mv": 日频 Series；"trades": 成交日志；
    "skipped": 无法成交指令；"noops": 无操作卖出；"n_positions_max": 最大持仓数}。
    """
    series = _validate_bars(bars_by_code, calendar)
    last_bar = max(s["dates"][-1] for s in series.values())
    days = [d for d in calendar if test_start <= d <= last_bar]
    if not days:
        raise ValueError(f"测试窗 [{test_start}~] 与数据（末根 {last_bar}）无交集")

    # ---- 指令解析：票内首个 >= 指令日的 bar 成交（停牌顺延，§3.6-8 语义）
    per_code = {}
    for order, ins in enumerate(instructions):
        _check_instruction(ins)
        if ins.code not in series:
            raise ValueError(f"指令代码不在池内: {ins.code}（{ins}）")
        per_code.setdefault(ins.code, []).append((order, ins))
    exec_groups = {}
    skipped = []
    instr_dates_by_code = {}            # 存活指令日期登记（顺延取消判定用）
    for code in sorted(per_code):
        info = series[code]
        resolved = []
        for order, ins in per_code[code]:
            i = bisect.bisect_left(info["dates"], ins.date)
            if i >= len(info["dates"]):
                skipped.append({"date": ins.date, "code": code, "side": ins.side,
                                "price_type": ins.price_type, "target": ins.target,
                                "reason": "no_bar_on_or_after"})
                continue
            resolved.append((ins.date, order, ins, i))
        # target 指令"后到取消先到"：早指令到成交日仍未执行（停牌跨月）且同票已有
        # 更晚日期的 target 指令 → 取消早指令；事件指令永不取消。
        resolved.sort(key=lambda t: (t[0], t[1]))
        dead = set()
        for j in range(len(resolved) - 1):
            d1, _o1, ins1, i1 = resolved[j]
            d2, _o2, ins2, _i2 = resolved[j + 1]
            if ins1.target and ins2.target and info["dates"][i1] >= d2:
                dead.add(id(ins1))
        for _d, order, ins, i in resolved:
            if id(ins) in dead:
                skipped.append({"date": ins.date, "code": code, "side": ins.side,
                                "price_type": ins.price_type, "target": ins.target,
                                "reason": "superseded_by_later_target"})
                continue
            exec_groups.setdefault(info["dates"][i], []).append((order, code, ins, i))
            instr_dates_by_code.setdefault(code, []).append(ins.date)
    for d in exec_groups:
        exec_groups[d].sort(key=lambda t: t[0])

    # ---- 日循环（§3.6-12：先结算旧持仓当日收益，再执行当日指令）
    cash = float(initial_nav)
    shares = {}        # code -> 股数（连续份额，§3.6-11 不做 100 股取整）
    last_close = {}    # code -> 最近有 bar 收盘（盯市价，停牌沿用）
    nav_prev = float(initial_nav)
    nav_d, ret_d, cash_d, mv_d = {}, {}, {}, {}
    trades, noops = [], []
    deferred = []      # cap 顺延中的 target 买入 [(order, code, ins, next_bar_date)]
    cap_defers = []
    max_pos_seen = 0

    for d in days:
        entries = list(exec_groups.get(d, []))
        due = sorted([x for x in deferred if x[3] == d], key=lambda x: x[0])
        if due:
            deferred = [x for x in deferred if x[3] != d]
            # 顺延买入排在当日静态指令之后重试（晚到者后议，确定性次序）
            entries.extend((o, c, ins, series[c]["idx"][d])
                           for o, c, ins, _nd in due)

        # 卖出相（先结算）：事件/target sell = 全仓退出；无持仓为无操作
        for _order, code, ins, i in entries:
            if ins.side != "sell":
                continue
            held = shares.get(code, 0.0)
            if held <= 0.0:
                noops.append({"date": d, "instr_date": ins.date, "code": code,
                              "side": ins.side, "price_type": ins.price_type,
                              "target": ins.target, "reason": "no_position"})
                continue
            info = series[code]
            px = info["open"][i] if ins.price_type == "open" else info["close"][i]
            gross = held * px
            cost = cost_rate * gross
            cash += gross - cost
            del shares[code]
            trades.append({"exec_date": d, "instr_date": ins.date, "code": code,
                           "side": "sell", "price_type": ins.price_type,
                           "target": ins.target, "px": px, "shares": held,
                           "gross": gross, "cost": cost})

        # target buy 的减持部分（目标市值 < 当前市值 → 卖出差额，只对变动计费）
        for _order, code, ins, i in entries:
            if not (ins.side == "buy" and ins.target):
                continue
            held = shares.get(code, 0.0)
            cur_mv = held * last_close.get(code, 0.0)
            delta = target_weight * nav_prev - cur_mv
            if delta >= 0.0:
                continue            # 增持/零变动 → 买入相处理；变动为零不成交不计费
            info = series[code]
            px = info["open"][i] if ins.price_type == "open" else info["close"][i]
            n_sh = min((-delta) / last_close[code], held)
            if n_sh >= held * (1.0 - 1e-12):
                n_sh = held         # 全退浮点尘埃归并
            gross = n_sh * px
            cost = cost_rate * gross
            cash += gross - cost
            shares[code] = held - n_sh
            if shares[code] <= 1e-12:
                del shares[code]
            trades.append({"exec_date": d, "instr_date": ins.date, "code": code,
                           "side": "sell", "price_type": ins.price_type,
                           "target": ins.target, "px": px, "shares": n_sh,
                           "gross": gross, "cost": cost})

        # 买入相（后切换）：事件 buy = 20%×NAV_ref；target buy = 增持至目标市值
        for order, code, ins, i in entries:
            if ins.side != "buy":
                continue
            if ins.target:
                cur_mv = shares.get(code, 0.0) * last_close.get(code, 0.0)
                notional = target_weight * nav_prev - cur_mv
                if notional <= 0.0:
                    continue        # 减持已在卖出相执行；零变动不成交不计费
            else:
                notional = target_weight * nav_prev
            if code not in shares and len(shares) >= max_positions:
                if not ins.target:
                    raise AssertionError(
                        f"持仓上限 {max_positions} 越限: {d} 买入 {code}"
                        f"（当前持仓 {sorted(shares)}）")
                # 换仓重叠：掉出票停牌致槽位未释放 → target 买入顺延至下一 bar 重试；
                # 同票若已有"晚于本指令、且不晚于本次重试日"的指令（较新意愿已到
                # 决定时点）→ 取消；更晚指令仍在未来 → 先到先得，继续顺延
                if any(ins.date < dd <= d
                       for dd in instr_dates_by_code.get(code, [])):
                    skipped.append({"date": ins.date, "code": code, "side": ins.side,
                                    "price_type": ins.price_type, "target": ins.target,
                                    "reason": "superseded_by_later_target"})
                    continue
                info = series[code]
                k = info["idx"][d]
                if k + 1 < len(info["dates"]):
                    nxt = info["dates"][k + 1]
                    deferred.append((order, code, ins, nxt))
                    cap_defers.append({"date": d, "code": code, "until": nxt})
                    continue
                skipped.append({"date": ins.date, "code": code, "side": ins.side,
                                "price_type": ins.price_type, "target": ins.target,
                                "reason": "cap_deferred_no_bar_left"})
                continue
            info = series[code]
            px = info["open"][i] if ins.price_type == "open" else info["close"][i]
            cost = cost_rate * notional
            cash -= notional + cost
            shares[code] = shares.get(code, 0.0) + notional / px
            trades.append({"exec_date": d, "instr_date": ins.date, "code": code,
                           "side": "buy", "price_type": ins.price_type,
                           "target": ins.target, "px": px, "shares": notional / px,
                           "gross": notional, "cost": cost})

        # 盯市（§3.6-11：当日 close_qfq；当日无 bar 沿用最近有 bar 收盘、零收益）
        for code in shares:
            j = series[code]["idx"].get(d)
            if j is not None:
                last_close[code] = series[code]["close"][j]
        mv = 0.0
        for code in shares:
            mv += shares[code] * last_close[code]
        nav = cash + mv
        if len(shares) > max_positions:
            raise AssertionError(f"持仓上限 {max_positions} 越限（日终 {d}）")
        nav_d[d] = nav
        ret_d[d] = nav / nav_prev - 1.0
        cash_d[d] = cash
        mv_d[d] = mv
        if len(shares) > max_pos_seen:
            max_pos_seen = len(shares)
        nav_prev = nav

    return {"returns": pd.Series(ret_d), "nav": pd.Series(nav_d),
            "cash": pd.Series(cash_d), "mv": pd.Series(mv_d),
            "trades": trades, "skipped": skipped, "noops": noops,
            "cap_defers": cap_defers, "n_positions_max": max_pos_seen}


# ---------------------------------------------------------------- 基线腿

def momentum_baseline_leg(bars_by_code, calendar, *, test_start=TEST_START,
                          initial_nav=INITIAL_NAV) -> dict:
    """受控 momentum 基线腿（§3.6-10 逐字；受控对照腿，非生产 momentum profile）。

    动量 = 截至上月末最后交易日 close_qfq 的 20 日收益；每月最后交易日收盘计算、
    次月首个有 bar 交易日开盘调仓（月末信号 → 月末调仓指令，引擎按票顺延）；Top5
    等权、每笔目标权重 20% 总资产；池内 20 日历史不足（月末前不足 21 根 bar）剔除；
    不足 5 只余下现金；已持有票不重复计费（target 指令只对变动部分成交计费）。
    返回引擎结果 + picks（{月: Top5}）+ rebalance_dates + switches（换仓次数）。
    """
    series = _validate_bars(bars_by_code, calendar)
    last_bar = max(s["dates"][-1] for s in series.values())
    cal_list = list(calendar)
    cal_months = sorted({d[:7] for d in cal_list})
    months = [m for m in cal_months if test_start[:7] <= m <= last_bar[:7]]

    instructions = []
    picks, reb_dates = {}, {}
    prev_top = []
    for m in months:
        prev_keys = [k for k in cal_months if k < m]
        if not prev_keys:
            continue                    # 无上月（窗口首月防御，不产生调仓）
        prev_end = max(d for d in cal_list if d[:7] == prev_keys[-1])
        scored = []
        for code in sorted(series):
            info = series[code]
            p = bisect.bisect_right(info["dates"], prev_end)
            if p < MOM_WINDOW + 1:
                continue                # 20 日历史不足 → 剔除（§3.6-10）
            mom = info["close"][p - 1] / info["close"][p - 1 - MOM_WINDOW] - 1.0
            scored.append((-mom, code))
        scored.sort()
        top = [c for _neg, c in scored[:TOP_N]]
        reb = min(d for d in cal_list if d[:7] == m and d >= test_start)
        for c in top:
            instructions.append(Instruction(reb, c, "buy", "open", target=True))
        for c in prev_top:
            if c not in top:
                instructions.append(Instruction(reb, c, "sell", "open", target=True))
        picks[m] = top
        reb_dates[m] = reb
        prev_top = top

    res = run_portfolio_backtest(bars_by_code, calendar, instructions,
                                 test_start=test_start, initial_nav=initial_nav)
    traded_months = {t["instr_date"][:7] for t in res["trades"]}
    switches = sum(1 for m in sorted(picks) if m in traded_months)
    res.update({"picks": picks, "rebalance_dates": reb_dates,
                "switches": switches, "months": months})
    return res


# ---------------------------------------------------------------- 实跑验收

def main() -> int:
    bars_by_code, calendar = load_core_bars()          # 只读（sqlite URI mode=ro）
    r1 = momentum_baseline_leg(bars_by_code, calendar)
    r2 = momentum_baseline_leg(bars_by_code, calendar)
    if not (list(r1["returns"].index) == list(r2["returns"].index)
            and bool((r1["returns"].values == r2["returns"].values).all())
            and bool((r1["nav"].values == r2["nav"].values).all())):
        print("FAIL: 基线腿两遍日收益/NAV 序列不一致（可复现性验收失败）")
        return 1

    total, ann, mdd = metrics(r1["returns"])
    segs = seg_total(r1["returns"])
    ret = r1["returns"]
    print(f"数据: {calendar[0]} ~ {calendar[-1]}；测试窗 {ret.index[0]} ~ "
          f"{ret.index[-1]}（{len(ret)} 个交易日）")
    print("== 受控 momentum 基线腿（§3.6-10；受控对照腿，非生产 momentum profile）==")
    print(f"累计 {total:+.2%}  年化 {ann:+.2%}  MDD {mdd:.2%}  "
          f"换仓 {r1['switches']}/{len(r1['picks'])} 月  "
          f"最大持仓 {r1['n_positions_max']}/{MAX_POSITIONS}  "
          f"跳过指令 {len(r1['skipped'])}  无操作卖出 {len(r1['noops'])}  "
          f"换仓重叠顺延 {len(r1['cap_defers'])}")
    for k, v in segs.items():
        print(f"  段 {k}: {v:+.2%}")

    ok = True
    if r1["switches"] < 24:
        print(f"FAIL: 换仓 {r1['switches']} < 24（27 个月验收下限）")
        ok = False
    if r1["n_positions_max"] > MAX_POSITIONS:
        print(f"FAIL: 最大持仓 {r1['n_positions_max']} 越上限 {MAX_POSITIONS}")
        ok = False
    if r1["skipped"]:
        print(f"FAIL: {len(r1['skipped'])} 条指令无法成交（基线腿应无）")
        ok = False
    if ok:
        print("验收: 两遍日收益序列逐位一致 / 换仓>=24 / 持仓上限未越限 —— PASS")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
