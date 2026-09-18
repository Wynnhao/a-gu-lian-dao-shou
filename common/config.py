"""统一配置层（结构性重构 Phase 3，docs/结构性重构实施方案.md）。

stdlib-only（json/copy/pathlib），Python 3.9，零新依赖（ADR #1/#2）。

两种读法，语义与迁移前各模块行为一一对应：
- snapshot(): 进程生命周期冻结快照（短命 pipeline 语义）。读盘 + validate
  fail-fast——把「崩在 blacklist.py:16 业务深处」的 KeyError 提前到 import 期，
  报错带精确路径。返回 deepcopy 的普通 dict，可变——测试注入手法（set_gate 原地
  mutate、派生常量）全部保持可用；各模块快照互不共享（与迁移前各自 json.loads 一致）。
- load(): mtime 缓存热读（webapp 长命进程语义）。文件改动下次调用生效；坏 JSON
  保留旧缓存下次重试；stat 失败返回 {}。**不跑 validate**——展示层降级是刻意
  设计（偏离方案字面「load 也 validate」的理由：webapp 每请求调用，validate raise
  会把页面从优雅降级变成 500；容错分级保留原则优先）。

容错分级保留（刻意设计，不一刀切 fail-fast）：notify 失败回退 {}、regime/signals
失败回退默认值——这些调用方的 try/except 原样保留。

AGSICKLE_* 运行时开关不收编（ADR #7：测试逃生门保持「调用时读 env」语义）。
"""
import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE / "config.json"

_GLOBAL_CACHE: Dict[str, Any] = {"mtime": None, "cfg": {}}


class ConfigError(Exception):
    """配置硬键缺失/类型错误（或 JSON 解析失败）。报错带精确路径。"""


# 有效 profile 枚举（与 signals.PROFILES 同清单；config 层不 import signals 防循环）
_VALID_PROFILES = ("reversal_lowvol", "reversal_lowvol_v2", "momentum")

# W-D4（P1-24）：关键段与段内关键硬键清单（2026-09-19 对照 config.json 现有键钉死）。
# 审查实证：watchlist_core 缺失曾静默回退全表（86 只全变可交易）、risk 内层键缺失
# 静默落默认——并行会话重写 config 的历史隐患此前没有任何闸。现关键段缺失或
# 段内关键键缺失一律 ConfigError（snapshot() import 期 fail-fast）。
# 例外（协同点4）：execution.exec_retry_max / exec_retry_drift_max /
# exec_breaker_threshold 是 C-ARC 带默认值的可选键，**不得**进本清单。
# 非关键段（pools/regime/notify/bond_yield/etf_share/breadth/recorder 等）各调用方
# 自带降级（容错分级保留），不在此列。
REQUIRED_SECTION_KEYS: Dict[str, tuple] = {
    "risk": (
        "max_single_weight", "max_total_weight", "max_positions",
        "price_guard_pct", "max_daily_trades", "max_weekly_turnover",
        "max_drawdown_kill", "kill_stop_hours", "min_confidence",
        "lot_size", "stop_loss_pct", "atr_stop_mult",
        "max_concept_weight", "max_amount_share",
    ),
    "execution": (
        "mode", "manual_gate", "emergency_direct_exec", "use_live_prices",
        "paper_start_cash", "commission_rate", "min_commission",
        "stamp_tax_rate", "slippage_bps", "volume_participation_cap",
        "sim_limit_halt",
    ),
    "signals": ("profile",),
}
# 类型约束（缺省不查类型，只查存在；列出的是必须挡住的量纲/类型错误）：
# 数字键 → int/float（bool 排除）；布尔键 → bool；字符串键 → 非空 str
_REQUIRED_NUMERIC = frozenset(
    list(REQUIRED_SECTION_KEYS["risk"]) + [
        "paper_start_cash", "commission_rate", "min_commission",
        "stamp_tax_rate", "slippage_bps", "volume_participation_cap"])
_REQUIRED_BOOL = frozenset(("manual_gate", "emergency_direct_exec",
                            "use_live_prices", "sim_limit_halt"))
_REQUIRED_STR = frozenset(("mode", "profile"))


def _check_section_keys(errors: List[str], section: str, body: dict) -> None:
    """段内关键硬键：缺失/类型错 → 追加可读错误（W-D4）。"""
    for key in REQUIRED_SECTION_KEYS.get(section, ()):
        if key not in body:
            errors.append("%s.%s: missing required key" % (section, key))
            continue
        v = body[key]
        if key in _REQUIRED_NUMERIC:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                errors.append("%s.%s: expected number, got %r" % (section, key, v))
        elif key in _REQUIRED_BOOL:
            if not isinstance(v, bool):
                errors.append("%s.%s: expected bool, got %r" % (section, key, v))
        elif key in _REQUIRED_STR:
            if not isinstance(v, str) or not v:
                errors.append("%s.%s: expected non-empty str, got %r" % (section, key, v))
        if section == "signals" and key == "profile" \
                and isinstance(v, str) and v \
                and v not in _VALID_PROFILES:
            errors.append("signals.profile: unknown profile %r（有效值：%s）"
                          % (v, "/".join(_VALID_PROFILES)))


def validate(cfg: Any) -> List[str]:
    """硬键集校验，返回可读错误清单（空清单 = 通过）。

    顶层硬键：db_path / watchlist（list 且元素含 code）/ blacklist_rules /
    watchlist_core（可交易池单一事实源，缺失曾静默回退全表——P1-24 现为硬键）；
    关键段 risk / execution / signals 必须为 object，且段内关键硬键缺失即报错
    （清单见 REQUIRED_SECTION_KEYS；C-ARC 三个可选执行键不在其列）。
    """
    errors: List[str] = []
    if not isinstance(cfg, dict):
        return ["root: expected object, got %s" % type(cfg).__name__]
    db = cfg.get("db_path")
    if not isinstance(db, str) or not db:
        errors.append("db_path: expected non-empty str, got %r" % (db,))

    def _check_watchlist_field(name: str) -> None:
        wl = cfg.get(name)
        if not isinstance(wl, list) or not wl:
            errors.append("%s: expected non-empty list, got %r"
                          % (name, None if wl is None else type(wl).__name__,))
        else:
            for i, item in enumerate(wl):
                if not isinstance(item, dict) or not item.get("code"):
                    errors.append("%s[%d].code: missing or empty" % (name, i))
                    break

    _check_watchlist_field("watchlist")
    _check_watchlist_field("watchlist_core")   # W-D4（P1-24）：不再静默回退
    br = cfg.get("blacklist_rules")
    if not isinstance(br, dict):
        errors.append("blacklist_rules: expected object, got %r"
                      % (None if br is None else type(br).__name__,))
    for section in ("risk", "execution", "signals"):
        body = cfg.get(section)
        if not isinstance(body, dict):
            errors.append("%s: expected object, got %r"
                          % (section, None if body is None else type(body).__name__,))
            continue
        _check_section_keys(errors, section, body)
    return errors


def load(path: Optional[Path] = None, cache: Optional[Dict[str, Any]] = None) -> dict:
    """mtime 缓存热读（webapp 语义）：改动生效、坏 JSON 保留旧缓存、stat 失败返回 {}。

    path/cache 参数供 webapp.load_config 转发（CONFIG_PATH/_CONFIG_CACHE 模块属性
    保留为 test_webapp 注入 hook，必须运行时读取）。
    """
    p = Path(path) if path is not None else CONFIG_PATH
    c = cache if cache is not None else _GLOBAL_CACHE
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}
    if c.get("mtime") != mtime:
        try:
            c["cfg"] = json.loads(p.read_text(encoding="utf-8"))
            c["mtime"] = mtime
        except Exception:
            return c.get("cfg") or {}  # 坏 JSON：保留旧缓存，下次重试（webapp 原语义）
    return c["cfg"]


def snapshot(path: Optional[Path] = None) -> dict:
    """进程冻结快照（pipeline 语义）：读盘 + validate fail-fast + 返回可变 deepcopy。"""
    p = Path(path) if path is not None else CONFIG_PATH
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ConfigError("config.json JSON 解析失败: %s" % e) from e
    errors = validate(cfg)
    if errors:
        raise ConfigError("config 校验失败（硬键缺失/类型错误）:\n  - "
                          + "\n  - ".join(errors))
    return copy.deepcopy(cfg)


def core_watchlist(cfg: Optional[dict] = None) -> List[dict]:
    """策略可交易池（单一事实源）：config.watchlist_core，缺省退回 watchlist。

    2026-09-13「profile 与自选池错配」修复的锚点：策略会下单的池子 = watchlist_core
    （五组概念共 51 只）；watchlist_extended（35 只）仅供看板观察，不可交易。
    信号计算 / 回测 universe / 决策白名单 / 输入包渲染统一走本函数，避免各处各读一份。
    """
    c = cfg if cfg is not None else load()
    return list(c.get("watchlist_core") or c.get("watchlist", []))


def core_codes(cfg: Optional[dict] = None) -> List[str]:
    """策略可交易池的 6 位码清单。"""
    return [str(w["code"]) for w in core_watchlist(cfg)]


def active_profile() -> str:
    """当前生效的 score profile（config.signals.profile，缺省 reversal_lowvol）。

    signal 表主键为 (code, as_of, profile)，多 profile 行并存；所有生产读取方
    （bundle / 看板 API / regime / 计数）必须按本函数过滤，否则会读到混口径行
    ——2026-09-13「profile 与自选池错配」修复的第二个锚点。
    """
    c = load()
    p = (c.get("signals") or {}).get("profile", "reversal_lowvol")
    return p if p in _VALID_PROFILES else "reversal_lowvol"
