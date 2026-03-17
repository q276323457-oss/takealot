#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# Silence macOS system Tk deprecation warning.
export TK_SILENCE_DEPRECATION=1

if [ ! -x "$ROOT/.venv/bin/python" ]; then
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"

if ! python -c "import requests, PIL, yaml, dotenv, playwright" >/dev/null 2>&1; then
  pip install -r requirements.txt
  python -m playwright install chromium msedge
fi

if ! python -c "import PySide6" >/dev/null 2>&1; then
  pip install PySide6==6.8.1
fi

python "$ROOT/gui_qt.py"
