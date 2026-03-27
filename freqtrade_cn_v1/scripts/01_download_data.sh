#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TIMERANGE="${1:-20230101-}"
MODE="${2:-append}"

cd "${ROOT_DIR}"

if [ ! -f "user_data/config.private.json" ]; then
  echo "缺少 user_data/config.private.json，请先运行 bash scripts/00_prepare.sh"
  exit 1
fi

DOWNLOAD_FLAGS=(--prepend)

if [ "${MODE}" = "fresh" ]; then
  DOWNLOAD_FLAGS=(--erase)
fi

docker compose run --rm freqtrade download-data \
  --config /freqtrade/user_data/config.base.json \
  --config /freqtrade/user_data/config.private.json \
  "${DOWNLOAD_FLAGS[@]}" \
  --pairs BTC/USDT ETH/USDT \
  --timeframes 4h \
  --timerange "${TIMERANGE}"
