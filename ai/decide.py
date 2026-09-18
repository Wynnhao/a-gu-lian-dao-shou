"""AI决策校验与落库：校验 LLM 输出的决策 JSON（全部合法才写 decision 表），含模板输出与 CLI。"""
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import argparse
import json
import logging
import logging.handlers
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Tuple

from common.config import core_codes, snapshot
from data import repo
from data.fetcher import get_conn
from risk.blacklist import check_blacklist, is_earnings_only

CFG = snapshot()  # 统一配置层（批1迁移：消除 import 冻结的裸 json.loads 坏味道）
RISK_CFG = CFG.get("risk", {})
WATCHLIST_CODES = core_codes(CFG)   # 策略可交易池（watchlist_core，51 只）；扩展观察池不可交易
MAX_SINGLE_WEIGHT = float(RISK_CFG.get("max_single_weight", 0.20))
MIN_CONFIDENCE = float(RISK_CFG.get("min_confidence", 0.60))

_ACTIONS = ("buy", "sell", "hold", "watch")

try:
    from ai.bundle import PROMPT_VERSION
except Exception:  # noqa: BLE001  避免循环依赖时退化
    PROMPT_VERSION = "unknown"

log = logging.getLogger("ai.decide")
log.setLevel(logging.INFO)
if not log.handlers:
    _fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    from common.logsetup import rotating_handler  # AGSICKLE_LOG_DIR 逃生门（Sprint4 W0-1）
    _fh = rotating_handler("ai.log")
    _fh.setFormatter(_fmt)
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    log.addHandler(_fh)
    log.addHandler(_sh)
log.propagate = False


# ---------------------------------------------------------------- 校验

def validate(obj, blacklist: Optional[dict] = None,
             earnings_events: Optional[dict] = None) -> Tuple[bool, Optional[dict], List[str]]:
    """校验单条决策 -> (ok, normalized, errors)。

    - obj 须为 dict；action/code/target_weight/confidence/reasons 必填，risk_notes 默认 []。
    - blacklist 为 {code: (ok, reason)}（可选）：传入时拦截 ok=False 的黑名单票。
    - earnings_events 为 {code: {positive, negative, net, ...}}（可选，Fix-5）：
      该票近 3 日净分 ≥ +2 → confidence = min(1.0, confidence + 0.1)（normalize 阶段）。
    - normalized 只保留白名单键（多余键剔除）；buy/sell 保留规整后的 order，hold/watch 不带 order。
    """
    errors: List[str] = []
    if not isinstance(obj, dict):
        return False, None, ["决策必须是 JSON 对象，实际: %s" % type(obj).__name__]

    action = str(obj.get("action") or "").strip().lower()
    code = str(obj.get("code") or "").strip()

    # action
    if action not in _ACTIONS:
        errors.append("action 非法: %r（须为 buy/sell/hold/watch）" % (obj.get("action"),))

    # code：6位数字字符串；buy/sell/hold 须在 watchlist 内，watch（观察）放宽为
    # 任意有效代码——动态池（异动/热门）票纳入评估用，无交易动作、不涉及资金
    if not (len(code) == 6 and code.isdigit()):
        errors.append("code 非法: %r（须为6位数字字符串）" % (obj.get("code"),))
    elif action != "watch" and code not in WATCHLIST_CODES:
        errors.append("code %s 不在 watchlist 内 %s" % (code, WATCHLIST_CODES))

    # 黑名单（传入 blacklist 时才校验；执行层风控引擎还会再拦一次）
    # P0-5：sell 单豁免"仅业绩预告负面"拦截（止损卖出不应被焊死，与 engine 同口径）
    if blacklist:
        item = blacklist.get(code)
        if item is not None and not item[0]:
            if action == "sell" and is_earnings_only(item[1]):
                pass  # 豁免：业绩预告负面不阻止止损卖出
            else:
                errors.append("code %s 在黑名单中: %s" % (code, item[1]))

    # target_weight ∈ [0, max_single_weight]
    tw_raw = obj.get("target_weight")
    tw = None
    try:
        tw = float(tw_raw)
    except (TypeError, ValueError):
        errors.append("target_weight 缺失或非法: %r" % (tw_raw,))
    else:
        if tw < 0 or tw > MAX_SINGLE_WEIGHT + 1e-9:
            errors.append("target_weight %s 越界（合法范围 [0, %s]）" % (tw_raw, MAX_SINGLE_WEIGHT))

    # confidence ∈ [0, 1]
    conf_raw = obj.get("confidence")
    conf = None
    try:
        conf = float(conf_raw)
    except (TypeError, ValueError):
        errors.append("confidence 缺失或非法: %r" % (conf_raw,))
    else:
        if conf < 0.0 or conf > 1.0:
            errors.append("confidence %s 越界（合法范围 [0, 1]）" % (conf_raw,))

    # reasons：字符串数组且 >=2 条非空
    reasons_raw = obj.get("reasons")
    reasons_clean: List[str] = []
    if not isinstance(reasons_raw, list):
        errors.append("reasons 须为字符串数组，实际: %r" % (reasons_raw,))
    else:
        if not all(isinstance(x, str) for x in reasons_raw):
            errors.append("reasons 含非字符串项: %r" % (reasons_raw,))
        else:
            reasons_clean = [x.strip() for x in reasons_raw if x.strip()]
            if len(reasons_clean) < 2:
                errors.append("reasons 至少 2 条非空，实际 %d 条" % len(reasons_clean))

    # risk_notes：字符串数组（可为空）
    rn_raw = obj.get("risk_notes")
    rn_clean: List[str] = []
    if rn_raw is None:
        rn_raw = []
    if not isinstance(rn_raw, list) or not all(isinstance(x, str) for x in rn_raw):
        errors.append("risk_notes 须为字符串数组，实际: %r" % (rn_raw,))
    else:
        rn_clean = [x.strip() for x in rn_raw if x.strip()]

    # order：buy/sell 必须有 {side, price>0, shares>0} 且 side 与 action 一致
    order_norm = None
    if action in ("buy", "sell"):
        od = obj.get("order")
        if not isinstance(od, dict):
            errors.append("action=%s 缺少 order 对象 {side, price, shares}" % action)
        else:
            side = str(od.get("side") or "").strip().lower()
            if side != action:
                errors.append("order.side=%r 与 action=%r 不一致" % (od.get("side"), action))
            price = None
            try:
                price = float(od.get("price"))
            except (TypeError, ValueError):
                errors.append("order.price 非法: %r" % (od.get("price"),))
            else:
                if price <= 0:
                    errors.append("order.price 必须 > 0，实际 %s" % (od.get("price"),))
            shares = None
            try:
                shares = float(od.get("shares"))
            except (TypeError, ValueError):
                errors.append("order.shares 非法: %r" % (od.get("shares"),))
            else:
                if shares <= 0:
                    errors.append("order.shares 必须 > 0，实际 %s" % (od.get("shares"),))
            if not any("order." in e for e in errors):
                order_norm = {"side": action, "price": price,
                              "shares": int(shares) if float(shares).is_integer() else shares}

    if errors:
        return False, None, errors

    normalized = {
        "action": action,
        "code": code,
        "target_weight": tw,
        "confidence": conf,
        "reasons": reasons_clean,
        "risk_notes": rn_clean,
    }
    # Fix-5：业绩预告正面硬加成——近 3 日净分 ≥ +2 → confidence +0.1（封顶 1.0）
    if earnings_events and conf is not None:
        ev = earnings_events.get(code) or {}
        net = ev.get("net")
        try:
            if net is not None and float(net) >= 2.0:
                normalized["confidence"] = min(1.0, conf + 0.1)
        except (TypeError, ValueError):
            pass
    if action in ("hold", "watch"):
        normalized["target_weight"] = 0.0  # 无交易动作不允许挂目标权重（此前 watch 可带 0.1）
    if order_norm is not None:
        normalized["order"] = order_norm
    return True, normalized, []


# ---------------------------------------------------------------- 落库

def _reason_anchored(reasons: List[str], bundle_text: str) -> bool:
    """引用核验（轻量）：至少一条理由命中 bundle 中的数字或 8 字以上连续片段。
    无法核验（bundle 缺失）时返回 True 不降级。"""
    if not bundle_text:
        return True
    import re
    for r in reasons:
        if re.search(r"\d+(\.\d+)?%?", r) and any(tok for tok in re.findall(
                r"[\u4e00-\u9fa5A-Za-z0-9.+\-]{6,}", r) if tok in bundle_text):
            return True
        for tok in re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{8,}", r):
            if tok in bundle_text:
                return True
    return False


def _dump_raw(run_date: str, data, failed: List[Tuple[int, List[str]]]) -> Optional[Path]:
    """校验失败时把 LLM 原始输出与原因清单落盘（此前失败现场不可复盘，
    小格式错导致当天无决策且无修复线索）。"""
    try:
        d = BASE / "logs" / "session" / str(run_date)
        d.mkdir(parents=True, exist_ok=True)
        p = d / ("decision_raw_%s.json" % datetime.now().strftime("%H%M%S"))
        p.write_text(json.dumps({
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "run_date": run_date,
            "errors": {str(i): errs for i, errs in failed},
            "raw": data,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        log.warning("校验失败原始输出已留存 %s（可修正后重跑，勿直接改判定）", p)
        return p
    except Exception as e:  # noqa: BLE001
        log.warning("失败留痕写入异常（忽略）: %s", repr(e))
        return None


def save_decisions(conn: sqlite3.Connection, decisions, input_snapshot: str,
                   run_date: str, trade_date: Optional[str] = None,
                   model: str = "", prompt_version: str = PROMPT_VERSION,
                   bundle_text: str = "",
                   earnings_events: Optional[dict] = None) -> List[int]:
    """逐条校验，全部通过才入库；任一失败整体放弃（返回 []），原始输出与原因留盘。

    - trade_date = 预期执行日（默认今天）——run_date/trade_date 口径修复；
    - 幂等：同 run_date 同 code+action 且 target_weight/confidence/reasons 完全一致的
      重复入库直接跳过（此前同一批决策被重复入库 5 次污染反馈链；不做 DB 唯一约束，
      午评同日同票的合法增量决策靠内容差异区分）；
    - 引用核验：buy/sell 理由无法锚定 bundle 内容时降级 report_only（防幻觉）；
    - status：confidence >= min_confidence -> "proposed"；否则 "report_only"。
    """
    if isinstance(decisions, dict):
        decisions = [decisions]
    if not isinstance(decisions, list):
        log.error("decisions 须为 JSON 数组，实际: %s", type(decisions).__name__)
        return []

    try:
        bl = check_blacklist(conn)
    except Exception as e:
        log.warning("黑名单读取失败，本轮跳过黑名单校验: %s", repr(e))
        bl = {}

    normalized_all: List[dict] = []
    failed: List[Tuple[int, List[str]]] = []
    for i, d in enumerate(decisions):
        ok, norm, errs = validate(d, blacklist=bl, earnings_events=earnings_events)
        if ok:
            normalized_all.append(norm)
        else:
            failed.append((i, errs))

    if failed:
        for i, errs in failed:
            log.error("决策[%d] 校验失败: %s", i, "；".join(errs))
        log.error("解析失败即放弃当日决策 run_date=%s（共 %d 条，%d 条不合法，全部不入库）",
                  run_date, len(decisions), len(failed))
        _dump_raw(run_date, decisions, failed)
        return []

    trade_date = trade_date or date.today().isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    ids: List[int] = []
    skipped = 0
    for d in normalized_all:
        # 内容级幂等查重
        dup = conn.execute(
            "SELECT 1 FROM decision WHERE run_date=? AND code=? AND action=? "
            "AND target_weight IS ? AND confidence IS ? AND reasons IS ? LIMIT 1",
            (run_date, d["code"], d["action"],
             d["target_weight"], d["confidence"],
             json.dumps(d["reasons"], ensure_ascii=False))).fetchone()
        if dup:
            skipped += 1
            log.info("decision 重复入库跳过 run_date=%s %s %s（同内容已存在）",
                     run_date, d["code"], d["action"])
            continue
        status = "proposed" if float(d["confidence"]) >= MIN_CONFIDENCE else "report_only"
        if d["action"] in ("buy", "sell") and bundle_text \
                and not _reason_anchored(d["reasons"], bundle_text):
            status = "report_only"
            log.warning("decision %s %s 理由未能锚定输入包内容，降级 report_only",
                        d["code"], d["action"])
        new_id = repo.insert_decision(
            conn, d, run_date, status=status, input_snapshot=input_snapshot,
            trade_date=trade_date, model=model, prompt_version=prompt_version,
            created_at=now)
        ids.append(new_id)
        log.info("decision#%d run_date=%s trade_date=%s %s %s weight=%s conf=%.2f "
                 "status=%s model=%s pv=%s",
                 new_id, run_date, trade_date, d["code"], d["action"],
                 d["target_weight"], float(d["confidence"]), status, model, prompt_version)
    conn.commit()
    if skipped:
        log.info("幂等跳过 %d 条重复决策 run_date=%s", skipped, run_date)
    log.info("已入库 %d 条决策 run_date=%s", len(ids), run_date)
    return ids


def load_and_save(json_path, run_date: Optional[str] = None,
                  model: str = "", result: Optional[dict] = None) -> List[int]:
    """CLI 主流程：读决策 JSON 文件（支持单对象或数组）-> validate -> save -> 打印结果。

    run_date 默认**今天**（预期执行日，与日报"决策回顾"对齐；此前取 daily_bar
    最新交易日导致盘前决策 run_date=T-1、日报按 T 查询永远查空）。
    result（可选 out 参数）：写入 {"legal_empty": bool}——LLM 输出 `[]` 且校验零失败
    属"当日合法空决策"（W-C6/P1-23），调用方（main）据此 exit 0，避免自动化把
    合法空决策误判为"决策缺失需补跑"。
    """
    p = Path(json_path)
    try:
        text = p.read_text(encoding="utf-8")
        data = json.loads(text)
    except Exception as e:
        log.error("读取/解析 %s 失败: %s", p, repr(e))
        print("[decide] 文件读取或 JSON 解析失败，放弃当日决策（详见 logs/ai.log）")
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        log.error("决策文件顶层须为 JSON 对象或数组，实际: %s", type(data).__name__)
        print("[decide] 决策文件格式非法，放弃当日决策（详见 logs/ai.log）")
        return []

    # W-C6（P1-23）：合法空决策——`[]` 无条目即校验零失败，属正常"今日不交易"
    # 语义（fail-safe 与显式不交易要区分开），提前返回且不碰数据库。
    if data == []:
        if result is not None:
            result["legal_empty"] = True
        print("[decide] 当日合法空决策（LLM 输出 []，无交易意图，正常语义）"
              "run_date=%s" % (run_date or date.today().isoformat()))
        return []

    conn = get_conn()
    try:
        run_date = run_date or date.today().isoformat()
        # input_snapshot 组合快照：bundle 全文用于归因，decisions 原文供
        # execution.runner._decision_from_row 回读 order（其按 "decisions" 键扫描）
        bundle_obj = None
        bundle_path = BASE / "logs" / "session" / str(run_date) / "bundle.json"
        if bundle_path.exists():
            try:
                bundle_obj = json.loads(bundle_path.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("解析 %s 失败，快照退化为决策文件原文: %s",
                            bundle_path, repr(e))
        else:
            log.warning("未找到 %s，input_snapshot 退化为决策文件原文", bundle_path)
        if bundle_obj is not None:
            snapshot = json.dumps({"bundle": bundle_obj, "decisions": data},
                                  ensure_ascii=False)
        else:
            snapshot = text
        # 引用核验锚定 bundle 内容（此前误传决策文件原文，理由必然命中，核验形同虚设）
        anchor_text = json.dumps(bundle_obj, ensure_ascii=False) \
            if bundle_obj is not None else ""
        # Fix-5：业绩预告正面 confidence 加成的数据源（bundle 读取失败 → None 不加成）
        earnings_events = (bundle_obj or {}).get("earnings_events_latest")
        ids = save_decisions(conn, data, snapshot, run_date,
                             trade_date=run_date, model=model,
                             prompt_version=PROMPT_VERSION,
                             bundle_text=anchor_text,
                             earnings_events=earnings_events)
        statuses = dict(conn.execute(
            "SELECT status, COUNT(*) FROM decision WHERE id IN (%s) GROUP BY status"
            % ",".join("?" * len(ids)), ids).fetchall()) if ids else {}
    finally:
        conn.close()

    if not ids:
        print("[decide] 校验未全部通过，当日决策已放弃（run_date=%s），详见 logs/ai.log" % run_date)
        return ids
    print("[decide] run_date=%s 入库 %d 条，decision_id=%s" % (run_date, len(ids), ids))
    print("[decide] 状态汇总: " + ("、".join(f"{k}={v}" for k, v in sorted(statuses.items()))
                                   if statuses else "无"))
    return ids


# ---------------------------------------------------------------- 模板与 CLI

def template() -> List[dict]:
    """合法决策 JSON 示例（可通过 validate 自检），供会话 agent 参考。"""
    return [
        {
            "action": "buy",
            "code": "600519",
            "target_weight": 0.10,
            "confidence": 0.72,
            "reasons": [
                "信号表 as_of=最新交易日 ma_trend=up 且 mom_20d=+5.2%，趋势与动量共振向上",
                "近3日新闻《贵州茅台三季报超预期》（东方财富）印证基本面改善",
            ],
            "risk_notes": [
                "rsi_14=68.5 逼近超买区，注意追高风险",
            ],
            "order": {"side": "buy", "price": 1450.0, "shares": 100},
        },
        {
            "action": "hold",
            "code": "300750",
            "target_weight": 0.0,
            "confidence": 0.65,
            "reasons": [
                "signal score=0.45 处于中性区间，多空信号矛盾",
                "近3日无新增催化剂新闻",
            ],
            "risk_notes": [],
        },
    ]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AI 决策校验与落库（decision 表）")
    ap.add_argument("--file", default=None, help="LLM 输出的决策 JSON 文件路径（单对象或数组）")
    ap.add_argument("--date", default=None, dest="run_date",
                    help="决策运行日期 YYYY-MM-DD（默认今天=预期执行日）")
    ap.add_argument("--model", default="", help="决策模型标识（落 decision.model 供归因）")
    ap.add_argument("--template", action="store_true", help="在 stdout 打印合法示例 JSON 后退出")
    args = ap.parse_args(argv)
    if args.template:
        print(json.dumps(template(), ensure_ascii=False, indent=2))
        return 0
    if not args.file:
        ap.error("需要 --file decision.json（或使用 --template 查看合法示例）")
    result: dict = {}
    ids = load_and_save(args.file, args.run_date, model=args.model, result=result)
    if not ids and result.get("legal_empty"):
        return 0  # W-C6：合法空决策不是失败
    return 0 if ids else 1


if __name__ == "__main__":
    raise SystemExit(main())
