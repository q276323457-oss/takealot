#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${ROOT_DIR}"

mkdir -p \
  user_data/logs \
  user_data/data \
  user_data/models \
  user_data/backtest_results \
  user_data/hyperopt_results

if [ ! -f "user_data/config.private.json" ]; then
  cp "user_data/config.private.example.json" "user_data/config.private.json"
  echo "已创建 user_data/config.private.json，请先填写 OKX API / Passphrase 和 WebUI 密码。"
else
  echo "已存在 user_data/config.private.json，保留原文件。"
fi

docker compose pull

echo
echo "准备完成。下一步："
echo "1. 编辑 user_data/config.private.json"
echo "2. 运行 bash scripts/01_download_data.sh"
