#!/bin/bash
set -euo pipefail

OS="$(uname -s)"
case "$OS" in
  Darwin|Linux) ;;
  *) echo "error: unsupported OS '$OS'. minicrew supports Darwin and Linux only." >&2; exit 1 ;;
esac

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

# Preflight is the single source of truth for "is this machine ready".
# It reads the config, dispatches to the right platform, and checks everything that platform needs.
.venv/bin/python -m worker --preflight || { echo "error: preflight failed. Fix the issues above and re-run." >&2; exit 1; }

for i in $(seq 1 "$WORKERS"); do
  if [ -n "$POLL_INTERVAL" ]; then
    .venv/bin/python -m worker.platform install --instance "$i" --role "$ROLE" --config-path "$MINICREW_CONFIG_PATH" --poll-interval "$POLL_INTERVAL" --replace-existing
  else
    .venv/bin/python -m worker.platform install --instance "$i" --role "$ROLE" --config-path "$MINICREW_CONFIG_PATH" --replace-existing
  fi
done

case "$OS" in
  Darwin) echo "installed $WORKERS worker(s). verify: launchctl list | grep com.minicrew.worker ; tail -f logs/worker-1.log" ;;
  Linux)  echo "installed $WORKERS worker(s). verify: systemctl --user list-units --all 'minicrew-worker-*' ; tail -f logs/worker-1.log" ;;
esac
