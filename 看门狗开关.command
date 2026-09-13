#!/bin/bash
# A股镰刀手 · 漏开机兜底看门狗：一键安装/卸载开关（双击运行，按当前状态自动切换）
cd "$(dirname "$0")" || exit 1
LABEL="com.agsickle.catchup"
PLIST_SRC="deploy/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "════════ A股镰刀手 · 漏开机兜底看门狗 ════════"

if launchctl list 2>/dev/null | grep -q "$LABEL"; then
  echo "当前状态：✅ 已安装 → 本次执行【卸载】"
  launchctl unload "$PLIST_DST" 2>/dev/null
  rm -f "$PLIST_DST"
  if launchctl list 2>/dev/null | grep -q "$LABEL"; then
    echo "❌ 卸载失败，请手动: launchctl unload $PLIST_DST"
    exit 1
  fi
  echo "✅ 已卸载。漏开机时定时任务将不再自动补跑（可再次双击重新安装）。"
else
  echo "当前状态：⚪ 未安装 → 本次执行【安装】"
  mkdir -p "$HOME/Library/LaunchAgents"
  launchctl unload "$PLIST_DST" 2>/dev/null
  cp "$PLIST_SRC" "$PLIST_DST" && launchctl load "$PLIST_DST"
  if launchctl list 2>/dev/null | grep -q "$LABEL"; then
    echo "✅ 已安装并启动：每 30 分钟尝试补跑漏掉的任务，机器唤醒时自动兜底。"
    echo "────────────────────────────────────────────"
    echo "⚠ 首次使用需一次性授权（项目在桌面，launchd 需要读取权限）："
    echo "   系统设置 → 隐私与安全性 → 完全磁盘访问权限 → + →"
    echo "   Cmd+Shift+G 输入: $(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "$PLIST_DST" 2>/dev/null)"
    echo "   → 打开并开启开关。授权后自动生效，无需其他操作。"
    echo "────────────────────────────────────────────"
  else
    echo "❌ 安装失败，请手动执行: launchctl load $PLIST_DST"
    exit 1
  fi
fi
exit 0
