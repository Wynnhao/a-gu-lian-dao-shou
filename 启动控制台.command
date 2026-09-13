#!/bin/bash
# A股镰刀手 · 一键启动控制台（双击运行；控制台在后台常驻，关掉窗口不影响）
cd "$(dirname "$0")" || exit 1
PY="$(command -v python3)"
PORT=8317
URL="http://127.0.0.1:$PORT/"

echo "════════ A股镰刀手 · AI交易员看板 ════════"

if curl -s -o /dev/null --max-time 2 "$URL/api/overview"; then
  echo "✅ 控制台已在运行，直接打开浏览器"
else
  echo "⏳ 启动中…（日志: logs/webapp.log）"
  nohup "$PY" webapp/server.py > logs/webapp.log 2>&1 &
  for i in $(seq 1 20); do
    sleep 0.5
    if curl -s -o /dev/null --max-time 2 "$URL/api/overview"; then break; fi
  done
  if curl -s -o /dev/null --max-time 2 "$URL/api/overview"; then
    echo "✅ 控制台已启动（后台常驻，关掉本窗口不影响）"
  else
    echo "❌ 启动失败，请查看 logs/webapp.log"
    exit 1
  fi
fi

open "$URL"
echo "🌐 已在浏览器打开 $URL"
echo "（停止控制台: pkill -f webapp/server.py）"
exit 0
