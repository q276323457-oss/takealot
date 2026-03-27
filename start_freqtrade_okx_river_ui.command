#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [ ! -x "$ROOT/.venv/bin/python" ]; then
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"

if ! python -c "import PySide6" >/dev/null 2>&1; then
  pip install -r requirements.txt
fi

python "$ROOT/freqtrade_okx_river_v1/gui_qt.py"
