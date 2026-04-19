"""Idle watchdog that monitors recursive file activity in a session cwd.

Ported from the reference implementation (lines 637-679), parametrized on result
filename + the two idle timeouts.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import worker.core.state as state
from worker.terminal.shutdown import exit_claude_and_close_window

RESULT_COMPLETED = "completed"
RESULT_ERROR = "error"
RESULT_TIMEOUT = "timeout"
RESULT_SHUTDOWN = "shutdown"


def _newest_mtime(cwd: Path) -> float | None:
    newest: float | None = None
    for root, _dirs, files in os.walk(cwd):
        for f in files:
            # Worker control files (_prompt.txt, _run.sh, _window_id.txt) are intentionally skipped —
            # they don't represent actual session progress.
            if f.startswith("_"):
                continue
            try:
                mtime = os.path.getmtime(os.path.join(root, f))
                if newest is None or mtime > newest:
                    newest = mtime
            except OSError:
                continue
    return newest


def wait_for_completion(
    *,
    cwd: Path,
    window_id: int,
    result_filename: str,
    overall_timeout_seconds: int,
    idle_timeout_seconds: int = 1500,
    result_idle_timeout_seconds: int = 900,
    poll_interval: int = 15,
) -> str:
    """Block until the session completes, stalls, or is told to shut down.

    Returns one of RESULT_COMPLETED / RESULT_ERROR / RESULT_TIMEOUT / RESULT_SHUTDOWN.
    A session is considered complete when `result_filename` exists at the top level of
    cwd AND the file size has stopped growing for one poll cycle.
    """
    start = time.time()
    result_file = cwd / result_filename
    last_result_size: int | None = None

    while time.time() - start < overall_timeout_seconds:
        if state.shutdown_requested:
            exit_claude_and_close_window(window_id)
            return RESULT_SHUTDOWN

        elapsed = time.time() - start

        # Completion heuristic: result file exists, has non-zero size, and size is stable across ticks.
        if result_file.exists():
            try:
                size = result_file.stat().st_size
            except OSError:
                size = 0
            if size > 0 and last_result_size == size:
                exit_claude_and_close_window(window_id)
                return RESULT_COMPLETED
            last_result_size = size

        try:
            newest = _newest_mtime(cwd)
            idle_seconds = (time.time() - newest) if newest else elapsed

            if result_file.exists():
                res_idle = time.time() - result_file.stat().st_mtime
                if res_idle > result_idle_timeout_seconds:
                    print(
                        f"[watchdog] STALLED: {result_filename} idle {int(res_idle)}s — terminating",
                        file=sys.stderr,
                    )
                    exit_claude_and_close_window(window_id)
                    return RESULT_TIMEOUT
            else:
                # Guard: only evaluate idle-kill after the session has been running ≥ idle_timeout_seconds,
                # so fresh terminals starting from empty cwds aren't falsely killed before they write anything.
                if idle_seconds > idle_timeout_seconds and elapsed > idle_timeout_seconds:
                    print(
                        f"[watchdog] STALLED: no file activity for {int(idle_seconds)}s at {int(elapsed)}s",
                        file=sys.stderr,
                    )
                    exit_claude_and_close_window(window_id)
                    return RESULT_TIMEOUT
        except OSError:
            pass

        time.sleep(poll_interval)

    print(f"[watchdog] Timed out after {int(time.time() - start)}s", file=sys.stderr)
    exit_claude_and_close_window(window_id)
    return RESULT_TIMEOUT
