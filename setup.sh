#!/bin/bash
set -euo pipefail

WORKERS=1
ROLE=primary
POLL_INTERVAL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --workers) WORKERS="$2"; shift 2 ;;
    --role) ROLE="$2"; shift 2 ;;
    --poll-interval) POLL_INTERVAL="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

command -v claude >/dev/null 2>&1 || { echo "error: 'claude' not in PATH. Install Claude Code and authenticate first." >&2; exit 1; }
[ -f .env ] || { echo "error: .env not found. Copy .env.example to .env and fill in credentials." >&2; exit 1; }
chmod 600 .env
[ -n "${MINICREW_CONFIG_PATH:-}" ] || { echo "error: MINICREW_CONFIG_PATH not set in environment. Export it or source .env." >&2; exit 1; }

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
mkdir -p logs

for i in $(seq 1 "$WORKERS"); do
  if [ -n "$POLL_INTERVAL" ]; then
    .venv/bin/python -m worker.utils.launchd install --instance "$i" --role "$ROLE" --config-path "$MINICREW_CONFIG_PATH" --poll-interval "$POLL_INTERVAL" --replace-existing
  else
    .venv/bin/python -m worker.utils.launchd install --instance "$i" --role "$ROLE" --config-path "$MINICREW_CONFIG_PATH" --replace-existing
  fi
done

echo "installed $WORKERS worker(s). verify: launchctl list | grep com.minicrew.worker ; tail -f logs/worker-1.log"
