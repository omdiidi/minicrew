"""Clean teardown of Terminal.app sessions + ~/.claude session data sweep.

Ported from the reference implementation (lines 200-262), including the session-env
time-based sweep from lines 250-261 (explicitly preserved per plan).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def exit_claude_and_close_window(window_id: int) -> None:
    """Send `/exit` to the Claude prompt, wait for graceful shutdown, close the window.

    `/exit` is Claude Code's built-in clean shutdown; SIGTERM against the process leaves
    zombies and pops dialogs. Verified chain: /exit -> Claude exits -> shell `exit 0` ->
    [Process completed] -> window closes with no dialog.
    """
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'tell application "Terminal"\n    do script "/exit" in tab 1 of window id {window_id}\nend tell\n',
            ],
            capture_output=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"[shutdown] /exit send failed (window may be gone): {e}", file=sys.stderr)

    # Wait for the exit chain: /exit -> Claude exits -> _run.sh finishes -> exit 0 -> shell exits.
    time.sleep(5)

    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'tell application "Terminal" to close window id {window_id} saving no',
            ],
            capture_output=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"[shutdown] window close failed (may already be closed): {e}", file=sys.stderr)


def cleanup_session_data(cwd: str | Path) -> None:
    """Delete ~/.claude/projects/<encoded-path> and sweep ~/.claude/session-env older than 1h.

    `encoded-path` is the realpath of cwd with `/` replaced by `-` — this matches Claude Code's
    per-project session-transcript directory naming. Those transcripts grow 4-44MB per session
    and bloat disk if not reaped.
    """
    real_cwd = os.path.realpath(str(cwd))
    encoded = real_cwd.replace("/", "-")

    claude_dir = Path.home() / ".claude"

    projects_dir = claude_dir / "projects"
    if projects_dir.is_dir():
        try:
            for entry in os.listdir(projects_dir):
                if entry == encoded:
                    target = projects_dir / entry
                    shutil.rmtree(target, ignore_errors=True)
                    print(f"[cleanup] Removed project session: {entry}", file=sys.stderr)
        except OSError:
            pass

    # Time-based sweep of ~/.claude/session-env/ — anything older than 1 hour is old enough to drop.
    session_env_dir = claude_dir / "session-env"
    if session_env_dir.is_dir():
        one_hour_ago = time.time() - 3600
        try:
            for entry in os.listdir(session_env_dir):
                entry_path = session_env_dir / entry
                try:
                    if entry_path.is_dir() and entry_path.stat().st_mtime < one_hour_ago:
                        shutil.rmtree(entry_path, ignore_errors=True)
                        print(f"[cleanup] Removed session-env: {entry}", file=sys.stderr)
                except OSError:
                    continue
        except OSError:
            pass
