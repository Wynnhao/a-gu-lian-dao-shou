"""零流量守卫 wrapper：用 sys.addaudithook 拦截 socket/urllib 事件后 runpy 目标脚本。

tcpdump 的无 root 等效方案（结构性重构 Phase 1a 验收）：pipeline 黑盒测试经本
wrapper 跑真脚本，任何真实网络调用（socket.connect / getaddrinfo / urllib.Request）
都会被记入违规清单并就地抛错（pipeline 各联网点均有 try/except 降级，流程继续）。

用法：python3 tests/net_guard.py <violations_log> <script> [args...]
- 退出码透传目标脚本；
- 违规清单（每行一条）写 <violations_log>，测试断言其为空。

akshare/pandas 在挂 hook 前预热 import：import 期行为不属于被测对象
（脚本 import 链命中已缓存模块，不再触发 import 期副作用）。
"""
import runpy
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

GUARD_EVENTS = {
    "socket.connect",
    "socket.getaddrinfo",
    "socket.gethostbyname",
    "urllib.Request",
}


def main() -> None:
    log_path = Path(sys.argv[1])
    script = sys.argv[2]
    sys.argv = [script] + sys.argv[3:]

    violations = []

    def _hook(event, args):
        if event in GUARD_EVENTS:
            target = str(args[0])[:160] if args else "?"
            violations.append("%s -> %s" % (event, target))
            raise RuntimeError("net_guard: 拦截网络调用 %s %s" % (event, target))

    import akshare  # noqa: F401
    import pandas  # noqa: F401
    sys.addaudithook(_hook)

    code = 0
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as e:
        if e.code is None:
            code = 0
        elif isinstance(e.code, int):
            code = e.code
        else:
            print(str(e.code))
            code = 1
    finally:
        log_path.write_text("\n".join(violations), encoding="utf-8")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
