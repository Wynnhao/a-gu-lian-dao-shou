#!/bin/zsh
# A股镰刀手 · 盘中分钟快照录制器入口（launchd 共用 run_catchup.sh 模式）
# 选择顺序：项目 venv (.venv) → PATH 上的 python3（plist 不硬编码解释器，
# 此前 Xcode stub python3 断粮教训——见 memory: pipeline必须用.venv解释器）
BASE="${0:A:h:h}"   # deploy/ 的上一级 = 项目根
if [ -x "$BASE/.venv/bin/python3" ]; then
  exec "$BASE/.venv/bin/python3" "$BASE/pipeline/recorder.py" "$@"
fi
exec "$(command -v python3)" "$BASE/pipeline/recorder.py" "$@"
