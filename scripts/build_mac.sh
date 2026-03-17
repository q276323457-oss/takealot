#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

APP_NAME="西安众创南非Takealot自建链接AI工具"
DIST_DIR="$ROOT/dist"
BUILD_DIR="$ROOT/build"

python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt pyinstaller

rm -rf "$DIST_DIR" "$BUILD_DIR"

pyinstaller \
  --noconfirm \
  --windowed \
  --name "$APP_NAME" \
  --paths "$ROOT/src" \
  --collect-submodules takealot_autolister \
  --add-data "$ROOT/config:config" \
  --add-data "$ROOT/input:input" \
  --add-data "$ROOT/.env.example:." \
  --add-data "$ROOT/README.md:." \
  gui_qt.py

echo "✅ mac 构建完成：$DIST_DIR/$APP_NAME.app"
echo "提示：可用 hdiutil 手动打包 dmg。"

