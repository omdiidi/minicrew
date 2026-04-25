#!/bin/bash
set -euo pipefail

OS="$(uname -s)"
case "$OS" in
  Darwin|Linux) ;;
  *) echo "error: unsupported OS '$OS'. minicrew supports Darwin and Linux only." >&2; exit 1 ;;
esac

# cd to the repo root (directory of this script) so relative paths resolve.
cd "$(dirname "$0")"

[ -n "${MINICREW_CONFIG_PATH:-}" ] || { echo "error: MINICREW_CONFIG_PATH not set in environment. Export it or source .env." >&2; exit 1; }

# Glob-based inventory catches instances ≥6 — no fixed 1..5 loop.
.venv/bin/python -m worker.platform --config-path "$MINICREW_CONFIG_PATH" uninstall-all

# Harmless on Darwin (systemctl is absent there, so we guard); on Linux the per-instance
# uninstall already runs daemon-reload, but a final one is documented and safe.
if [ "$OS" = "Linux" ]; then
  systemctl --user daemon-reload || true
fi

echo "all minicrew workers uninstalled on $OS"
