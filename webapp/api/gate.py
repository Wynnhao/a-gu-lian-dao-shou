"""人工闸门：pending 盘点 + confirm/reject（唯一写操作，走 runner.py CLI）。"""
import json
import re
from pathlib import Path
from typing import List

from webapp.api.common import safe_join


def _srv():
    from webapp import server
    return server


_BY_RE = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff]+")


def api_pending(qs: dict) -> List[dict]:
    out: List[dict] = []
    ORDERS_DIR = _srv().ORDERS_DIR
    if not ORDERS_DIR.is_dir():
        return out
    files = sorted(ORDERS_DIR.glob("*/pending_*.json"))
    for p in files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        out.append({
            "decision_id": data.get("decision_id"),
            "path": str(Path("logs") / "orders" / p.parent.name / p.name),
            "decision": data.get("decision") or {},
            "verdict": data.get("verdict") or {},
            "confirm_hint": data.get("confirm_hint") or "",
            "reject_hint": data.get("reject_hint") or "",
            "created_at": data.get("created_at") or "",
        })
    out.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return out


def api_confirm(body: dict) -> dict:
    try:
        did = int(body.get("decision_id"))
    except (TypeError, ValueError):
        raise ValueError("decision_id 必须为整数")
    by = _BY_RE.sub("", str(body.get("by") or "")).strip()[:40] or "human"
    rc, out = _srv().run_runner(["confirm", "--decision-id", str(did), "--by", by])
    return {"ok": rc == 0, "output": out}


def api_reject(body: dict) -> dict:
    try:
        did = int(body.get("decision_id"))
    except (TypeError, ValueError):
        raise ValueError("decision_id 必须为整数")
    by = _BY_RE.sub("", str(body.get("by") or "")).strip()[:40] or "human"
    reason = re.sub(r"\s+", " ", str(body.get("reason") or "")).strip()[:500]
    if not reason:
        raise ValueError("否决必须填写理由")
    rc, out = _srv().run_runner(["reject", "--decision-id", str(did),
                          "--reason=%s" % reason, "--by", by])
    return {"ok": rc == 0, "output": out}


# ---------------------------------------------------------------- 决策工作流聚合
