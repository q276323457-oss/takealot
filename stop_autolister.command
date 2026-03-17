#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "========================================"
echo "Takealot 自动上架 - 停止"
echo "========================================"

if pgrep -f "$ROOT/scripts/run_daemon.sh" >/dev/null 2>&1; then
  pkill -f "$ROOT/scripts/run_daemon.sh"
  echo "已停止后台守护进程"
else
  echo "当前没有运行中的守护进程"
fi

read -r -p "按回车键退出..." _
