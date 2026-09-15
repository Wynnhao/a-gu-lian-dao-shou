"""报告/日志/会话/回测/文档等文件内容接口。

REPORTS_DIR/LOGS_DIR/SESSION_DIR 为 server 模块属性（test_webapp 注入 hook），
函数体内延迟访问，替换即生效。
"""
import json
import re
from pathlib import Path
from typing import List

from webapp.api.common import clamp_int, safe_join

LOG_WHITELIST = ("fetch", "news", "macro", "signal", "ai", "pipeline", "exec")
SESSION_KINDS = {"bundle_md": "bundle.md", "bundle_json": "bundle.json",
                 "decision": "decision.json"}
FILE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _srv():
    from webapp import server
    return server


def api_reports(qs: dict) -> List[dict]:
    out: List[dict] = []
    REPORTS_DIR = _srv().REPORTS_DIR
    if REPORTS_DIR.is_dir():
        for p in REPORTS_DIR.iterdir():
            if p.is_file() and p.suffix == ".md":
                st = p.stat()
                out.append({"file": p.name, "size": int(st.st_size),
                            "mtime": int(st.st_mtime)})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def api_report(qs: dict) -> dict:
    name = str((qs.get("file") or [""])[0])
    if not FILE_RE.match(name) or ".." in name:
        raise ValueError("非法文件名")
    REPORTS_DIR = _srv().REPORTS_DIR
    path = safe_join(REPORTS_DIR, name)
    if path is None or path.parent != REPORTS_DIR.resolve() or not path.is_file():
        raise FileNotFoundError("报告不存在")
    return {"name": name, "markdown": path.read_text(encoding="utf-8", errors="replace")}


def api_logs(qs: dict) -> dict:
    name = str((qs.get("name") or [""])[0])
    if name not in LOG_WHITELIST:
        raise ValueError("日志名须为 %s 之一" % ("/".join(LOG_WHITELIST),))
    lines = clamp_int((qs.get("lines") or ["200"])[0], 200, 1, 2000)
    LOGS_DIR = _srv().LOGS_DIR
    path = safe_join(LOGS_DIR, name + ".log")
    if path is None or path.parent != LOGS_DIR.resolve():
        raise ValueError("非法日志路径")
    out: List[str] = []
    if path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace")
        out = text.splitlines()[-lines:]
    return {"name": name, "lines": out}


def api_sessions(qs: dict) -> List[dict]:
    out: List[dict] = []
    SESSION_DIR = _srv().SESSION_DIR
    if not SESSION_DIR.is_dir():
        return out
    for p in SESSION_DIR.iterdir():
        if p.is_dir() and DATE_RE.match(p.name):
            out.append({"date": p.name, "has_bundle": (p / "bundle.md").is_file(),
                        "has_decision": (p / "decision.json").is_file()})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def api_session(qs: dict) -> dict:
    date = str((qs.get("date") or [""])[0])
    kind = str((qs.get("kind") or [""])[0])
    if not DATE_RE.match(date):
        raise ValueError("日期格式须为 YYYY-MM-DD")
    if kind not in SESSION_KINDS:
        raise ValueError("kind 须为 bundle_md/bundle_json/decision")
    SESSION_DIR = _srv().SESSION_DIR
    folder = safe_join(SESSION_DIR, date)
    if folder is None or folder.parent != SESSION_DIR.resolve() or not folder.is_dir():
        raise FileNotFoundError("会话目录不存在")
    path = safe_join(folder, SESSION_KINDS[kind])
    if path is None or path.parent != folder.resolve() or not path.is_file():
        raise FileNotFoundError("文件不存在")
    return {"date": date, "kind": kind, "file": SESSION_KINDS[kind],
            "content": path.read_text(encoding="utf-8", errors="replace")}


def api_backtest(qs: dict) -> dict:
    path = safe_join(_srv().LOGS_DIR, "backtest_result.json")
    if path is None or not path.is_file():
        return {"missing": True}
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        return {"raw": data}
    except ValueError:
        return {"missing": True, "raw_text": text[:5000]}


DOC_FILES = {
    "decision_playbook": Path(__file__).resolve().parent.parent.parent
    / "docs" / "决策策略与工作流.md",
    "strategy_lib": Path(__file__).resolve().parent.parent.parent / "docs" / "策略库.md",
}


def api_doc(qs: dict) -> dict:
    name = str(qs.get("name", [""])[0])
    path = DOC_FILES.get(name)
    if path is None:
        raise ValueError("未知文档: %s" % name)
    if not path.is_file():
        return {"name": name, "missing": True}
    return {"name": name, "markdown": path.read_text(encoding="utf-8", errors="replace")}


# ---------------------------------------------------------------- 自选股概念分组
