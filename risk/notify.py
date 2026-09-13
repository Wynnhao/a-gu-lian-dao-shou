"""主动通知：kill 触发 / readback 失败 / 成交失败 / pending 生成等需要立即人工
介入的事件，此前只写库等"人碰巧在看板前"，现在推送 macOS 通知中心 + 可选 webhook。

通道（config.notify，全部 best-effort，失败只记日志绝不抛异常）：
- osascript：macOS 通知中心（本机使用的主通道，零依赖）；
- webhook_url：企业微信/Bark/Server酱 等通用 JSON POST（{"title", "body"}）。
"""
import json
import logging
import os
import subprocess
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

log = logging.getLogger("notify")

_CFG = None


def _cfg() -> dict:
    global _CFG
    if _CFG is None:
        try:
            _CFG = json.loads((BASE / "config.json").read_text(
                encoding="utf-8")).get("notify", {})
        except Exception as e:  # noqa: BLE001
            log.warning("notify 配置读取失败: %s", repr(e))
            _CFG = {}
    return _CFG


def _osascript(title: str, body: str) -> bool:
    try:
        script = 'display notification %s with title %s' % (
            json.dumps(body or " "), json.dumps(title))
        subprocess.run(["osascript", "-e", script], timeout=5,
                       capture_output=True, check=False)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("osascript 通知失败: %s", repr(e)[:120])
        return False


def _webhook(url: str, title: str, body: str) -> bool:
    try:
        data = json.dumps({"title": title, "body": body}).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("webhook 通知失败: %s", repr(e)[:120])
        return False


def notify(title: str, body: str = "") -> dict:
    """发送通知，返回 {"osascript": bool, "webhook": bool}（未启用通道为 None）。

    环境变量 AGSICKLE_DISABLE_NOTIFY=1（测试用）直接短路，避免跑测试时弹系统通知。
    """
    if os.environ.get("AGSICKLE_DISABLE_NOTIFY") == "1":
        return {"osascript": None, "webhook": None}
    cfg = _cfg()
    if not cfg.get("enabled", True):
        return {"osascript": None, "webhook": None}
    out = {"osascript": None, "webhook": None}
    if cfg.get("osascript", True):
        out["osascript"] = _osascript(title, body)
    url = cfg.get("webhook_url") or ""
    if url:
        out["webhook"] = _webhook(url, title, body)
    log.info("notify [%s] %s -> %s", title, body, out)
    return out
