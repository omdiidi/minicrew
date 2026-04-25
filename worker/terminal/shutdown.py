"""Claude session-data cleanup (`~/.claude/projects` + `~/.claude/session-env` sweep).

Terminal-window teardown lives in `worker.platform.<os>.close_session` — this module
only handles the cross-platform filesystem cleanup that runs after a session ends.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path


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
