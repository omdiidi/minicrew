#!/bin/bash
set -euo pipefail

echo "=== Phase 1: Preflight ==="
.venv/bin/python -m worker --preflight

# Scenario 1: 3 concurrent open/close.
echo "=== Phase 2: 3 concurrent terminals ==="
TMPDIRS=()
for i in 1 2 3; do
  TMP=$(mktemp -d)
  TMPDIRS+=("$TMP")
  cat > "$TMP/_run.sh" <<'EOF'
#!/bin/bash
sleep 1
echo "hello from $$"
sleep 60
EOF
  chmod +x "$TMP/_run.sh"
  .venv/bin/python - "$TMP" <<'PYEOF' &
import sys, json
from pathlib import Path
from worker.platform import detect_platform
from worker.config.loader import load_config
TMP = Path(sys.argv[1])
h = detect_platform(load_config()).launch_session(TMP)
(TMP / "_handle.json").write_text(h.to_json())
print(f"OPENED {h.kind} in {TMP}")
PYEOF
done
wait
echo "=== 3 windows should be visible. Press Enter to close them all. ==="
read
for d in "${TMPDIRS[@]}"; do
  [ -f "$d/_handle.json" ] || continue
  .venv/bin/python - "$d" <<'PYEOF'
import sys
from pathlib import Path
from worker.platform.base import SessionHandle
from worker.platform import detect_platform
from worker.config.loader import load_config
d = Path(sys.argv[1])
h = SessionHandle.from_json((d / "_handle.json").read_text())
detect_platform(load_config()).close_session(h)
print(f"CLOSED {h.kind}")
PYEOF
done
echo "=== Confirm: no minicrew-* windows in wmctrl -l ==="
wmctrl -l | grep -c minicrew- || echo "ok: 0 minicrew windows remaining"
echo "=== Confirm: no leaked xfce4-terminal processes ==="
ps -eo pid,cmd | grep -E "xfce4-terminal|xterm" | grep -v grep || echo "ok: no terminals"

# Scenario 2: SIGTERM during open (mid-launch race).
echo "=== Phase 3: SIGTERM mid-launch (aborts before wmctrl finds the window) ==="
TMP=$(mktemp -d)
cat > "$TMP/_run.sh" <<'EOF'
#!/bin/bash
sleep 300
EOF
chmod +x "$TMP/_run.sh"
.venv/bin/python - "$TMP" <<'PYEOF' &
import sys, os, time
from pathlib import Path
from worker.platform import detect_platform
from worker.config.loader import load_config
# Force a short timeout to simulate race.
cfg = load_config()
cfg.platform.linux.window_open_timeout_seconds = 1
p = detect_platform(cfg)
try:
    p.launch_session(Path(sys.argv[1]))
except Exception as e:
    print(f"EXPECTED FAIL: {e}")
PYEOF
wait
sleep 5
echo "=== Confirm: no lingering processes from the aborted launch ==="
ps -eo pid,cmd | grep -E "xfce4-terminal|xterm" | grep -v grep || echo "ok: no leaks"
rm -rf "$TMP"

# Scenario 3: systemd unit restart survives a simulated crash.
echo "=== Phase 4: systemd install / crash / auto-restart ==="
.venv/bin/python -m worker.platform install --instance 99 --role primary \
  --config-path "$MINICREW_CONFIG_PATH" --replace-existing
sleep 3
systemctl --user status minicrew-worker-99.service --no-pager | head -5
# Kill it and confirm systemd brings it back within RestartSec.
pkill -f "worker --instance 99" || true
sleep 8
systemctl --user is-active minicrew-worker-99.service || echo "FAIL: did not restart"
.venv/bin/python -m worker.platform --config-path "$MINICREW_CONFIG_PATH" uninstall --instance 99

# Scenario 4: fan-out-like concurrent launch + merge.
echo "=== Phase 5: 3 parallel terminals + 1 sequential 'merge' ==="
# (User manually enqueues a fan-out job via /minicrew:scaffold-project for full coverage.)

echo ""
echo "=== SMOKE COMPLETE ==="
echo "Next: enqueue a real fan_out job and watch it end-to-end."
