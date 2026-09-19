"""缠论 R3 批次 1 —— 结构引擎纯函数库（冻结语义实现）。

施工方案：docs/缠论R3施工方案-2026-09-19.md §3.2（结构定义）+ §3.6（口径钉死补遗，
与 §3 同冻结）。本模块是 §3.2 + §3.6 冻结语义的结构引擎实现，供批次 2b 因果引擎
（chan_research.py）复用。**禁止在本库调参**——任何口径/参数/阈值变更须按 §3.5
新预注册批次立项；本文件内不存在可调参数（min_gap 的两个取值 4/3 均为 §3.2 预注册
口径本身：主口径严格笔与辅口径 czsc 宽松笔）。探针（/tmp/chan_probe_r3*.py）与本库
语义冲突处一律以 §3.6 为准（已知探针工件：首根包含方向默认向上、延伸笔数 ext≤3、
突破判"up 且 hi>ZG"先于交集检查——三者均不采，见各函数注释）。

纯函数纪律：无 DB 访问、无 config、无环境变量、无全局状态；输入 bar 数组/DataFrame
+ 日期，输出结构。唯一允许的仓库内 import 是 signals.chan_data（同为纯研究模块）：
split_segments 复用其断链判定 chain_breaks（§3.6-14d）。

语义索引（实现处注释均回引条款）：
- merge_inclusion 包含处理：闭区间判定（h/l 相等即包含）；首根合并K方向默认**向下**
  （§3.6-4，含首根即发生包含的情形；探针默认向上，不采）；后续方向 = 前一合并K相对
  其前一根的方向（按合并K高点比较，h 相等视为向上）；向上合并取 max(高)/max(低)、
  向下取 min(高)/min(低)；逐 bar 顺序处理即"递归至无包含"（合并只取 max/max 或
  min/min，数学上保证合并后的合并K不会与其前一根产生新包含，无需回溯）。
- find_fractals 分型：合并后相邻三根，中根 h、l 同时严格大于（顶）/严格小于（底）
  两侧（§3.6-4"严格不等"，相等不成型）；同一合并K不可能既严格高于又严格低于同两侧，
  "顶底分型不共用合并K"由严格性自动保证；确认信息 = 第 3 根合并K索引及其 i_end
  原始 bar 索引（§3.6-14a：确认时点 = 第 3 根合并K最后一根原始 bar 收盘）。
- build_strokes 成笔：主口径严格笔 min_gap=4——顶/底分型**中间根合并K索引差 ≥4**
  （§3.6-5，自动满足不共用元素）；相邻同型分型取更极端者（顶后者 ≥ 前者取后者、
  底后者 ≤ 前者取后者，**相等取后到者**）、异性距离不足丢**后者**（先到者优先）；
  顺序贪心，每个新分型只与最近保留分型比较。辅口径 min_gap=3（czsc 宽松笔，≥4 根
  合并K，§3.2）仅作敏感性披露，不参与 gate。
- build_pivots 笔中枢：连续三笔 ZG=min(三笔高)、ZD=max(三笔低)、须 ZG>ZD；延伸 =
  笔区间 [lo,hi] 与 [ZD,ZG] 闭区间有交集（lo ≤ ZG 且 hi ≥ ZD），延伸笔数无上限
  （§3.6-6；探针 ext≤3 为探针工件，不进口径）；不做中枢升级/扩展合并（§2 红线）。
- third_buys 三买：§3.6-6 扫描语义逐字实现——中枢最后一个延伸笔之后逐笔扫描：
  ① 向下笔无交集且 hi<ZD → 中枢终结、无三买；② 向上笔 lo>ZG → 突破笔（**与中枢
  有交集的向上笔哪怕笔顶>ZG 是延伸、不是突破**——§3.2"第一根突破"约束的实现）；
  ③ 悬浮笔（向下整体高于枢 lo>ZG / 向上整体低于枢 hi<ZD）既非延伸亦非终结，按字面
  继续扫描（由笔交替性，下一笔必落入②或①，一步收敛）。突破笔之后第一根向下笔为
  回抽笔（字面语义向前等待）；回抽笔终点底分型端点价（**禁用收盘价替代**）> ZG →
  三买成立，确认日 = 该底分型第 3 根合并K i_end 所指原始 bar 交易日；回抽端点 ≤ ZG
  → 该枢不产生三买（单次尝试，不再从同枢重扫）。多枢同触发分型按触发分型合并K索引
  去重、扫描序先到者优先（§3.6-7）；同触发多枢的形成窗取扫描序最先枢。
- macd_divergence MACD 面积与背驰（仅诊断，§3.6-9）：close_qfq 上 EMA12/26、DEA9
  标准递归（首值种子），柱 = 2×(DIF−DEA)；向上笔比红柱面积和、向下笔比绿柱面积
  绝对值和（相邻同向笔段比较）；后笔价格极值更极端而面积绝对值更小 → 记背驰一次。
  **无阈值参数**；只统计，不参与 gate 与出场。
- split_segments 断链切段：复用 chan_data.chain_breaks（§3.6-14d 断链判定 + 日历
  校验），把逐票 bar 切成连续段；结构计算在段内独立进行（每段重新跑包含→分型→笔
  →中枢→三买），不跨链连笔、不跨链比较（§3.1/§3.6-14d）。

实现约定（诊断性、非 gate、不影响任何预注册判据）：MACD 笔段原始 bar 范围取
[首分型中根 i_start, 末分型中根 i_end]（笔共享转折合并K，相邻笔面积在该处重叠，
对同向前后笔比较为同偏置）；合并K的 dir 字段为标注性信息（用最终 h 与前一根比较），
包含合并的方向判定在合并时点独立重算，不读该字段。
"""
from __future__ import annotations

from pathlib import Path
import sys

import numpy as np

try:
    from signals.chan_data import chain_breaks
except ImportError:  # 允许直接以文件运行：python3 signals/chanlib.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from signals.chan_data import chain_breaks

STRICT_MIN_GAP = 4   # 主口径严格笔：顶底分型中间根合并K索引差 ≥4（§3.6-5）
LOOSE_MIN_GAP = 3    # 辅口径 czsc 宽松笔：≥4 根合并K（§3.2，仅敏感性披露，不参与 gate）


# ---------------------------------------------------------------- 包含处理

def merge_inclusion(highs, lows) -> list:
    """包含处理（§3.2 + §3.6-4）：原始 bar 高低点序列 → 合并K列表。

    每根合并K：{h, l, i_start, i_end, dir}——h/l 为合并后高低点，i_start/i_end 为
    覆盖的原始 bar 闭区间（索引），dir 为标注性方向（'up'/'down'，与前一合并K按
    最终 h 比较，h 相等视为向上；K0 默认 'down'，§3.6-4）。

    语义：
    - 包含判定用闭区间：(h1≥h0 且 l1≤l0) 或 (h1≤h0 且 l1≥l0)，相等即包含；
    - 合并方向在合并时点重算：仅 1 根合并K时（含首根即发生包含）用默认**向下**；
      否则 = 前一合并K相对其前一根（h 比较，相等视为向上）；
    - 向上合并 h=max/l=max，向下合并 h=min/l=min；逐 bar 顺序处理即递归至无包含。
    """
    hs = [float(x) for x in highs]
    ls = [float(x) for x in lows]
    if len(hs) != len(ls):
        raise ValueError("highs/lows 长度不一致")
    proc: list = []
    for i, (h, l) in enumerate(zip(hs, ls)):
        if proc:
            last = proc[-1]
            if ((h >= last["h"] and l <= last["l"])
                    or (h <= last["h"] and l >= last["l"])):
                # §3.6-4：方向=前一合并K相对其前一根（h 比较，相等视为向上）；
                # 仅一根合并K时无"前一相对其前"可用 → 首根默认向下。
                if len(proc) >= 2:
                    up = last["h"] >= proc[-2]["h"]
                else:
                    up = False  # 首根默认向下（探针默认向上，以 §3.6-4 为准）
                if up:
                    last["h"] = max(last["h"], h)
                    last["l"] = max(last["l"], l)
                else:
                    last["h"] = min(last["h"], h)
                    last["l"] = min(last["l"], l)
                last["i_end"] = i
                continue
        proc.append({"h": h, "l": l, "i_start": i, "i_end": i, "dir": None})
    # dir 标注（非判定路径）：K0 默认向下；K_j 相对 K_{j-1} 按最终 h 比较，相等向上。
    for j, k in enumerate(proc):
        k["dir"] = "down" if j == 0 else ("up" if k["h"] >= proc[j - 1]["h"] else "down")
    return proc


# ---------------------------------------------------------------- 分型

def find_fractals(merged: list) -> list:
    """分型（§3.2 + §3.6-4/14a）：合并K序列 → 顶/底分型列表。

    每个分型：{type: 'T'|'B', p: 端点价（顶=中根 h / 底=中根 l）, pi: 中间根合并K索引,
    conf_pi: 第 3 根合并K索引（= pi+1）, conf_bar: 第 3 根合并K i_end 原始 bar 索引,
    mid_i_start / mid_i_end: 中间根合并K覆盖的原始 bar 区间}。
    严格不等：中根 h、l 须同时严格大于（顶）/严格小于（底）两侧，相等不成型。
    确认时点 = 第 3 根合并K最后一根原始 bar 收盘（§3.6-14a）。
    """
    frs = []
    for i in range(1, len(merged) - 1):
        a, b, c = merged[i - 1], merged[i], merged[i + 1]
        if b["h"] > a["h"] and b["h"] > c["h"] and b["l"] > a["l"] and b["l"] > c["l"]:
            t, p = "T", b["h"]
        elif b["h"] < a["h"] and b["h"] < c["h"] and b["l"] < a["l"] and b["l"] < c["l"]:
            t, p = "B", b["l"]
        else:
            continue
        frs.append({
            "type": t, "p": p, "pi": i,
            "conf_pi": i + 1,
            "conf_bar": c["i_end"],
            "mid_i_start": b["i_start"],
            "mid_i_end": b["i_end"],
        })
    return frs


# ---------------------------------------------------------------- 成笔

def build_strokes(fractals: list, min_gap: int = STRICT_MIN_GAP) -> tuple:
    """成笔（§3.2 + §3.6-5）：分型列表 → (保留分型, 笔列表, 丢弃集)。

    主口径 min_gap=4（严格笔）；辅口径 min_gap=3（czsc 宽松笔，仅敏感性披露）。
    顺序贪心，每个新分型只与最近保留分型比较：
    - 同型：更极端者留（顶后者 ≥ 前者 / 底后者 ≤ 前者，相等取后到者）——被替换的
      旧分型记 dropped[旧pi]="same_type_more_extreme"，更不极端的新分型记
      dropped[新pi]="same_type_less_extreme"；
    - 异性：中间根合并K索引差 ≥ min_gap 才成笔，否则丢后者
      （dropped[pi]="opposite_type_too_close"，先到者优先）。

    每根笔：{dir: 'up'|'down', hi, lo, start_pi, end_pi（分型中间根合并K索引）,
    sf / ef（起点/终点分型 dict）, start_bar / end_bar（原始 bar 区间 =
    sf 中根 i_start ~ ef 中根 i_end，供 MACD 笔段用）}。
    """
    kept: list = []
    dropped: dict = {}
    for f in fractals:
        if not kept:
            kept.append(dict(f))
            continue
        lastk = kept[-1]
        if f["type"] == lastk["type"]:
            more_extreme = (f["p"] >= lastk["p"]) if f["type"] == "T" \
                else (f["p"] <= lastk["p"])
            if more_extreme:  # 相等取后到者（§3.6-5）
                dropped[lastk["pi"]] = "same_type_more_extreme"
                kept[-1] = dict(f)
            else:
                dropped[f["pi"]] = "same_type_less_extreme"
        else:
            if f["pi"] - lastk["pi"] >= min_gap:
                kept.append(dict(f))
            else:  # 异性距离不足丢后者（先到者优先）
                dropped[f["pi"]] = "opposite_type_too_close"
    strokes = []
    for a, b in zip(kept, kept[1:]):
        strokes.append({
            "dir": "up" if a["type"] == "B" else "down",
            "hi": max(a["p"], b["p"]),
            "lo": min(a["p"], b["p"]),
            "start_pi": a["pi"],
            "end_pi": b["pi"],
            "sf": a,
            "ef": b,
            "start_bar": a["mid_i_start"],
            "end_bar": b["mid_i_end"],
        })
    return kept, strokes, dropped


# ---------------------------------------------------------------- 笔中枢

def build_pivots(strokes: list) -> list:
    """笔中枢（§3.2 + §3.6-6）：笔列表 → 中枢列表。

    对每个连续三笔起点 i：ZG=min(三笔 hi)、ZD=max(三笔 lo)，须 ZG>ZD；
    延伸 = 后续笔 [lo,hi] 与 [ZD,ZG] 闭区间有交集（lo ≤ ZG 且 hi ≥ ZD），
    依次扫描至第一根无交集笔为止，延伸笔数无上限（探针 ext≤3 不进口径）。

    每个中枢：{start_stroke_idx（三笔首笔索引）, zg, zd, ext_stroke_idxs（延伸笔
    索引列表，≥ start+3）, last_stroke_idx（最后一个延伸笔 = 无延伸时为第三笔）,
    scan_start_idx（三买扫描起点 = 最后延伸笔之后第一根笔）}。
    """
    pivots = []
    n = len(strokes)
    for i in range(n - 2):
        s0, s1, s2 = strokes[i], strokes[i + 1], strokes[i + 2]
        zg = min(s0["hi"], s1["hi"], s2["hi"])
        zd = max(s0["lo"], s1["lo"], s2["lo"])
        if not zg > zd:
            continue
        ext = []
        j = i + 3
        while j < n:
            s = strokes[j]
            if s["lo"] <= zg and s["hi"] >= zd:  # 闭区间交集（§3.6-6）
                ext.append(j)
                j += 1
            else:
                break
        last_ext = ext[-1] if ext else i + 2
        pivots.append({
            "start_stroke_idx": i,
            "zg": zg,
            "zd": zd,
            "ext_stroke_idxs": ext,
            "last_stroke_idx": last_ext,
            "scan_start_idx": last_ext + 1,
        })
    return pivots


# ---------------------------------------------------------------- 三买

def third_buys(strokes: list, pivots: list, dates: list) -> dict:
    """三买候选信号（§3.2 + §3.6-6/7）：笔+中枢+段内日期 → 候选信号。

    dates = 段内原始 bar 交易日列表（与包含处理时的 bar 序一致）；确认 bar 索引
    即其下标。扫描语义（§3.6-6 逐字）：
      ① 向下笔与中枢无交集且 hi < ZD → 中枢终结、无三买；
      ② 向上笔 lo > ZG → 突破笔（与中枢有交集的向上笔是延伸、不是突破）；
      ③ 悬浮笔按字面继续扫描（笔交替性保证一步收敛）；
      扫描中若再现与中枢有交集的笔按延伸处理（防御分支，正常数据由 build_pivots
      已耗尽连续交集笔、到不了此处）。
    突破笔之后第一根向下笔为回抽笔（向前字面等待）；回抽笔终点底分型端点价 > ZG
    → 三买；≤ ZG → 该枢不产生三买（单次尝试，不再从同枢重扫）。

    返回 {"signals": 去重后信号列表, "raw_flags": 命中中枢的 start_stroke_idx
    扫描序列表, "raw_signals": 去重前全量命中}。每个信号：
    {zg, zd, pivot_first_stroke_idx, breakout_stroke_idx, pullback_stroke_idx,
    trigger_pi（回抽底分型中间根合并K索引）, trigger_price（底分型端点价）,
    confirm_bar / confirm_date（§3.6-14a 确认原始 bar 索引/交易日）,
    formation_start_bar（§3.6-3 形成窗起点 = 枢首笔起点分型极值所在合并K首根
    原始 bar 索引；多枢同触发取扫描序最先枢）}。
    多枢同触发分型按 trigger_pi 去重、扫描序先到者优先（§3.6-7）。
    """
    n = len(strokes)
    raw_flags: list = []
    raw: list = []
    for pv in pivots:
        zg, zd = pv["zg"], pv["zd"]
        breakout_idx = None
        j = pv["scan_start_idx"]
        while j < n:
            s = strokes[j]
            if s["lo"] <= zg and s["hi"] >= zd:
                j += 1          # 延伸、不是突破（防御分支，见 docstring）
                continue
            if s["dir"] == "down" and s["hi"] < zd:
                break           # ① 中枢终结、无三买
            if s["dir"] == "up" and s["lo"] > zg:
                breakout_idx = j  # ② 突破笔
                break
            j += 1              # ③ 悬浮笔：继续扫描
        if breakout_idx is None:
            continue
        pull_idx = None
        k = breakout_idx + 1
        while k < n:            # 突破后第一根向下笔（字面等待，笔交替性下即 k+1）
            if strokes[k]["dir"] == "down":
                pull_idx = k
                break
            k += 1
        if pull_idx is None:
            continue
        ef = strokes[pull_idx]["ef"]
        if not ef["p"] > zg:
            continue            # 回抽端点 ≤ ZG → 单次尝试，不再重扫（§3.6-6）
        raw_flags.append(pv["start_stroke_idx"])
        raw.append({
            "zg": zg,
            "zd": zd,
            "pivot_first_stroke_idx": pv["start_stroke_idx"],
            "breakout_stroke_idx": breakout_idx,
            "pullback_stroke_idx": pull_idx,
            "trigger_pi": ef["pi"],
            "trigger_price": ef["p"],
            "confirm_bar": ef["conf_bar"],
            "confirm_date": None,  # 填充于下（带越界防御）
            "formation_start_bar":
                strokes[pv["start_stroke_idx"]]["sf"]["mid_i_start"],
        })
    for s in raw:
        cb = s["confirm_bar"]
        if cb >= len(dates):
            raise ValueError(
                f"confirm_bar {cb} 超出 dates 长度 {len(dates)}——"
                "dates 须与包含处理时的原始 bar 序一致")
        s["confirm_date"] = dates[cb]
    seen: set = set()
    signals = []
    for s in raw:               # §3.6-7：同触发分型去重，扫描序先到者优先
        if s["trigger_pi"] in seen:
            continue
        seen.add(s["trigger_pi"])
        signals.append(s)
    return {"signals": signals, "raw_flags": raw_flags, "raw_signals": raw}


# ---------------------------------------------------------------- MACD 面积与背驰（仅诊断）

def _ema(xs: np.ndarray, n: int) -> np.ndarray:
    """标准 EMA 递归：ema[0]=x[0]（首值种子），ema[t]=α·x[t]+(1−α)·ema[t−1]，α=2/(n+1)。"""
    a = 2.0 / (n + 1)
    out = np.empty(xs.size, dtype=float)
    prev = xs[0]
    out[0] = prev
    for i in range(1, xs.size):
        prev = a * xs[i] + (1.0 - a) * prev
        out[i] = prev
    return out


def macd_divergence(close_qfq, strokes: list) -> list:
    """MACD 柱面积与同向笔背驰诊断（§3.6-9，仅诊断、无阈值参数）。

    close_qfq = 段内原始 bar 收盘价序列（与 strokes 的 start_bar/end_bar 同索引系）。
    EMA12/26、DEA9 标准递归（首值种子），柱 = 2×(DIF−DEA)；每笔柱面积取该笔
    [start_bar, end_bar] 闭区间的红柱面积和（柱>0）与绿柱面积绝对值和（柱<0）。
    背驰：相邻同向笔比较——向上笔 hi 更高且红柱面积更小 / 向下笔 lo 更低且绿柱
    面积绝对值更小 → divergence=True。

    返回每笔一条：{stroke_idx, dir, start_bar, end_bar, red_area, green_area,
    divergence}。
    """
    c = np.asarray(close_qfq, dtype=float)
    out = []
    if c.size == 0:
        return out
    dif = _ema(c, 12) - _ema(c, 26)
    dea = _ema(dif, 9)
    hist = 2.0 * (dif - dea)
    prev_same_dir: dict = {}
    for idx, s in enumerate(strokes):
        a = max(0, min(int(s["start_bar"]), c.size - 1))
        b = max(0, min(int(s["end_bar"]), c.size - 1))
        seg = hist[a:b + 1] if b >= a else hist[:0]
        red = float(seg[seg > 0].sum()) if seg.size else 0.0
        green = float(-seg[seg < 0].sum()) if seg.size else 0.0
        d = s["dir"]
        div = False
        prev = prev_same_dir.get(d)
        if prev is not None:
            if d == "up":
                div = (s["hi"] > prev["hi"]) and (red < prev["red"])
            else:
                div = (s["lo"] < prev["lo"]) and (green < prev["green"])
        prev_same_dir[d] = {"hi": s["hi"], "lo": s["lo"],
                            "red": red, "green": green}
        out.append({"stroke_idx": idx, "dir": d, "start_bar": a, "end_bar": b,
                    "red_area": red, "green_area": green, "divergence": div})
    return out


# ---------------------------------------------------------------- 断链切段

def split_segments(bars_df, calendar_dates) -> list:
    """断链切段（§3.1/§3.6-14d）：逐票 bar DataFrame → 连续段 DataFrame 列表。

    复用 chan_data.chain_breaks（含"票内日期须在 trade_calendar 内"的从严校验）。
    bars_df 须按 trade_date 升序、含 trade_date 列；返回各段（copy、重置索引、
    保序）。结构计算在段内独立进行，不跨链连笔、不跨链比较。
    """
    dates = list(bars_df["trade_date"])
    breaks = chain_breaks(dates, list(calendar_dates))
    if not breaks:
        return [bars_df.reset_index(drop=True).copy()]
    pos = {d: i for i, d in enumerate(dates)}
    bounds = [0]
    for d1, d2, _ in breaks:
        bounds.append(pos[d1] + 1)
        bounds.append(pos[d2])
    bounds.append(len(bars_df))
    return [bars_df.iloc[a:b].reset_index(drop=True).copy()
            for a, b in zip(bounds[0::2], bounds[1::2])]


# ---------------------------------------------------------------- 便捷管线

def compute_structure(bars_df, min_gap: int = STRICT_MIN_GAP,
                      high_col: str = "high_qfq", low_col: str = "low_qfq") -> dict:
    """段内结构一步计算：包含→分型→笔→中枢。

    真实数据用缺省列 high_qfq/low_qfq（§3.1 价格列）；合成测试数据传
    high_col="high", low_col="low"。列名是数据形态适配、非口径参数——结构语义
    只取决于传入的高低点序列。三买需日期轴，由调用方以
    third_buys(strokes, pivots, dates) 单独调用。
    """
    merged = merge_inclusion(bars_df[high_col], bars_df[low_col])
    frs = find_fractals(merged)
    kept, strokes, dropped = build_strokes(frs, min_gap=min_gap)
    pivots = build_pivots(strokes)
    return {"merged": merged, "fractals": frs, "kept_fractals": kept,
            "strokes": strokes, "dropped": dropped, "pivots": pivots}


# ---------------------------------------------------------------- __main__：core 51 全史结构计数（只读）

def _run_core51() -> int:
    """只读跑 core 51 全史结构计数（主口径 + 宽松笔三买数），打印报告，exit 0。

    性质：结构密度对账（与探针 /tmp/chan_probe_r3.py 全池 179 个三买同数量级对照），
    非 gate 裁决——gate 由批次 3 signals/chan_research.py 按预注册判据跑。
    """
    from signals.chan_data import load_core_bars

    bars_by_code, calendar = load_core_bars()
    t_fr = t_stroke = t_pivot = t_pivot_disjoint = t_tb = t_tb_loose = t_div = 0
    n_seg = 0
    stroke_lens = []
    per_code_tb = {}
    for code in sorted(bars_by_code):
        df = bars_by_code[code]
        segs = split_segments(df, calendar)
        n_seg += len(segs)
        tb_here = 0
        for seg in segs:
            st = compute_structure(seg)
            dates = seg["trade_date"].tolist()
            tb = third_buys(st["strokes"], st["pivots"], dates)
            # 宽松笔辅口径（§3.2：仅敏感性披露）
            _, stks3, _ = build_strokes(st["fractals"], min_gap=LOOSE_MIN_GAP)
            pv3 = build_pivots(stks3)
            tb3 = third_buys(stks3, pv3, dates)
            divs = macd_divergence(seg["close_qfq"], st["strokes"])
            t_fr += len(st["fractals"])
            t_stroke += len(st["strokes"])
            t_pivot += len(st["pivots"])
            # 不相交（去重叠窗口）枢数：滑动三笔窗口天然重叠（PIVOT_CASES 钉死
            # 该语义），"中枢个数"的密度度量按不相交枢计（起点须>上一枢最后延伸笔）
            last = -1
            for p in st["pivots"]:
                if p["start_stroke_idx"] > last:
                    t_pivot_disjoint += 1
                    last = p["last_stroke_idx"]
            t_tb += len(tb["signals"])
            t_tb_loose += len(tb3["signals"])
            t_div += sum(1 for d in divs if d["divergence"])
            stroke_lens.extend(s["end_pi"] - s["start_pi"] for s in st["strokes"])
            tb_here += len(tb["signals"])
        per_code_tb[code] = tb_here

    n_bar = sum(len(v) for v in bars_by_code.values())
    avg_len = (sum(stroke_lens) / len(stroke_lens)) if stroke_lens else 0.0
    print("=== 缠论 R3 批次 1 结构引擎 —— core 51 全史结构计数（主口径 §3.6，只读） ===")
    print(f"数据: {n_bar} 行, 票数={len(bars_by_code)}, 断链切段后段数={n_seg}")
    print(f"[结构计数·主口径] 分型(检出)={t_fr}, 笔={t_stroke}, "
          f"笔中枢={t_pivot}(滑窗)/{t_pivot_disjoint}(不相交), 三买={t_tb}")
    print(f"[辅助披露] 宽松笔(min_gap=3)三买={t_tb_loose}, MACD 背驰笔数(诊断)={t_div}")
    print(f"[密度] 笔均长={avg_len:.2f} 合并K（合理带 5~15）, "
          f"不相交中枢/笔={t_pivot_disjoint / t_stroke if t_stroke else 0:.2f}"
          f"（合理 < 1/3 量级）, "
          f"有信号票数={sum(1 for v in per_code_tb.values() if v)}/{len(per_code_tb)}")
    top5 = sorted(per_code_tb.items(), key=lambda kv: -kv[1])[:5]
    print(f"[三买 top5 票] " + ", ".join(f"{c}={v}" for c, v in top5))
    print("对照: 探针粗版全池三买=179（探针口径：首根向上/ext<=3/hi>ZG 突破先行）；"
          "本计数按 §3.6 冻结语义（首根向下/延伸无上限/突破=lo>ZG 整体高于中枢），"
          "个体信号存在移位与增减，数量级一致即可。")
    print("注意: 本计数为全史结构密度（无 warmup/测试窗/clean 过滤），非 Gate 1 样本；"
          "gate 以批次 3 chan_research.py 为准。")
    print("done, exit 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_core51())
