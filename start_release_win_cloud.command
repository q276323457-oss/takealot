#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

bash "$ROOT/scripts/release_win_cloud.sh"

echo
read -r -p "按回车关闭窗口..."
