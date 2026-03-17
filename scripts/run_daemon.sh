#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ ! -d .venv ]; then
  echo "[ERROR] .venv not found. run setup first."
  exit 1
fi

mkdir -p logs

source .venv/bin/activate
: "${BROWSER_USER_DATA_DIR:=$ROOT/browser_profile}"
: "${BROWSER_PROFILE_DIRECTORY:=Default}"
: "${STORAGE_STATE_1688:=$ROOT/.runtime/auth/1688.json}"
: "${STORAGE_STATE_TAKEALOT:=$ROOT/.runtime/auth/takealot.json}"
export BROWSER_USER_DATA_DIR
export BROWSER_PROFILE_DIRECTORY
export STORAGE_STATE_1688
export STORAGE_STATE_TAKEALOT
mkdir -p "$BROWSER_USER_DATA_DIR"
mkdir -p "$(dirname "$STORAGE_STATE_1688")" "$(dirname "$STORAGE_STATE_TAKEALOT")"
while true; do
  TS="$(date '+%Y-%m-%d %H:%M:%S')"
  echo "[$TS] running one cycle..."
  if PYTHONPATH=src python run.py --headless --automate-portal --portal-mode draft --limit 1; then
    echo "[$TS] cycle done"
  else
    echo "[$TS] cycle failed"
  fi
  sleep 300
done
