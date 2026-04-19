#!/bin/bash
set -euo pipefail

REMOVED=0
for i in 1 2 3 4 5; do
  if .venv/bin/python -m worker.utils.launchd uninstall --instance "$i" 2>/dev/null; then
    REMOVED=$((REMOVED + 1))
  fi || true
done
echo "teardown complete: $REMOVED instance slot(s) processed."
