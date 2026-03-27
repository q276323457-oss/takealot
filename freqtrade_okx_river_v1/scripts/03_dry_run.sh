#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${ROOT_DIR}"

if [ ! -f "user_data/config.private.json" ]; then
  echo "缺少 user_data/config.private.json，请先运行 bash scripts/00_prepare.sh"
  exit 1
fi

docker compose up -d

echo "合约模拟盘已启动。"
echo "WebUI: http://127.0.0.1:8080"
echo "查看日志: bash scripts/05_logs.sh"
