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


def validate(cfg: Any) -> List[str]:
    """最小硬键集校验，返回可读错误清单（空清单 = 通过）。

    硬键：db_path / watchlist（list 且元素含 code）/ blacklist_rules / risk——
    即迁移前会 KeyError 在业务深处的四处（方案 §1 事实基础）。
    """
    errors: List[str] = []
    if not isinstance(cfg, dict):
        return ["root: expected object, got %s" % type(cfg).__name__]
    db = cfg.get("db_path")
    if not isinstance(db, str) or not db:
        errors.append("db_path: expected non-empty str, got %r" % (db,))
    wl = cfg.get("watchlist")
    if not isinstance(wl, list) or not wl:
        errors.append("watchlist: expected non-empty list, got %r"
                      % (None if wl is None else type(wl).__name__,))
    else:
        for i, item in enumerate(wl):
            if not isinstance(item, dict) or not item.get("code"):
                errors.append("watchlist[%d].code: missing or empty" % i)
                break
    br = cfg.get("blacklist_rules")
    if not isinstance(br, dict):
        errors.append("blacklist_rules: expected object, got %r"
                      % (None if br is None else type(br).__name__,))
    risk = cfg.get("risk")
    if not isinstance(risk, dict):
        errors.append("risk: expected object, got %r"
                      % (None if risk is None else type(risk).__name__,))
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
    return p if p in ("reversal_lowvol", "reversal_lowvol_v2", "momentum") else "reversal_lowvol"
