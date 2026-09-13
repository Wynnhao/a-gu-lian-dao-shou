#!/bin/zsh
# A股镰刀手 · 统一解释器入口（launchd/控制台/手动共用）
# 选择顺序：项目 venv (.venv) → PATH 上的 python3。
# 此前 plist 硬编码 Xcode 自带 python3 绝对路径，Xcode 升级/卸载即静默断粮。
BASE="${0:A:h:h}"   # deploy/ 的上一级 = 项目根
if [ -x "$BASE/.venv/bin/python3" ]; then
  exec "$BASE/.venv/bin/python3" "$BASE/pipeline/catchup.py" "$@"
fi
exec "$(command -v python3)" "$BASE/pipeline/catchup.py" "$@"
