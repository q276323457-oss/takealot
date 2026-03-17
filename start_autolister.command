#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

mkdir -p logs

echo "========================================"
echo "Takealot 自动上架 - 一键启动"
echo "目录: $ROOT"
echo "========================================"

FIRST_SETUP=0
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo "[1/5] 创建 Python 虚拟环境..."
  python3 -m venv .venv
  FIRST_SETUP=1
fi

# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"

if ! python -c "import playwright, requests, PIL, yaml, dotenv" >/dev/null 2>&1; then
  echo "[2/5] 安装依赖..."
  pip install -r requirements.txt
  FIRST_SETUP=1
else
  echo "[2/5] 依赖已就绪"
fi

if [ "$FIRST_SETUP" -eq 1 ]; then
  echo "[3/5] 安装浏览器驱动（Playwright）..."
  python -m playwright install chromium msedge
else
  echo "[3/5] 浏览器驱动保持现状"
fi

if [ ! -f "$ROOT/config/selectors.yaml" ] && [ -f "$ROOT/config/selectors.example.yaml" ]; then
  cp "$ROOT/config/selectors.example.yaml" "$ROOT/config/selectors.yaml"
  echo "[4/5] 已生成 config/selectors.yaml（请按实际页面选择器修改）"
else
  echo "[4/5] 配置文件已就绪"
fi

if pgrep -f "$ROOT/scripts/run_daemon.sh" >/dev/null 2>&1; then
  echo "[5/5] 守护进程已在运行，无需重复启动"
else
  echo "[5/5] 启动后台守护进程（每5分钟执行一次）..."
  nohup "$ROOT/scripts/run_daemon.sh" > "$ROOT/logs/daemon.out" 2>&1 &
  sleep 1
fi

PID_LIST="$(pgrep -f "$ROOT/scripts/run_daemon.sh" || true)"
if [ -n "$PID_LIST" ]; then
  echo ""
  echo "已启动，进程 PID: $PID_LIST"
  echo "日志文件: $ROOT/logs/daemon.out"
else
  echo "启动失败，请查看日志: $ROOT/logs/daemon.out"
fi

echo ""
echo "提示："
echo "1) 首次请先手动登录 1688 与 Takealot（同一浏览器 profile）"
echo "2) 如需停止，可执行: pkill -f run_daemon.sh"
echo ""
read -r -p "按回车键退出..." _
