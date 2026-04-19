"""Launch a Claude Code session inside a visible Terminal.app window.

Ported from the reference implementation (lines 175-285) with every domain reference stripped.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

from worker.utils.paths import trust_directory


class LaunchError(RuntimeError):
    """Raised when the Terminal.app session cannot be opened."""


def write_prompt_file(cwd: Path, prompt: str) -> Path:
    prompt_path = cwd / "_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    return prompt_path


def write_runner_script(cwd: Path, *, link_from: Path | None = None) -> Path:
    """Write `_run.sh` that execs Claude Code with the prompt text.

    Uses the exact reference pattern `claude --dangerously-skip-permissions "$(cat _prompt.txt)"`.
    When `link_from` is provided, documents in that directory are symlinked into `cwd` first
    (used by fan_out groups that share the parent session's documents).
    """
    real_cwd = os.path.realpath(str(cwd))
    runner = cwd / "_run.sh"
    link_block = ""
    if link_from is not None:
        real_src = os.path.realpath(str(link_from))
        link_block = (
            f'for doc in "{real_src}"/*; do\n'
            f'    base=$(basename "$doc")\n'
            '    case "$base" in _*|.*|group_*|merge) continue;; esac\n'
            '    [ -f "$doc" ] && ln -sf "$doc" . 2>/dev/null\n'
            "done\n"
        )
    script = (
        "#!/bin/bash\n"
        f'cd "{real_cwd}"\n'
        f"{link_block}"
        'claude --dangerously-skip-permissions "$(cat _prompt.txt)"\n'
    )
    runner.write_text(script, encoding="utf-8")
    runner.chmod(runner.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return runner


def launch_terminal_window(cwd: Path) -> int:
    """Open Terminal.app, run `_run.sh`, return the window id.

    The `return id of window 1 whose tabs contains t` incantation is load-bearing — osascript
    returns a tab reference from `do script`, and we need the enclosing window id for later
    cleanup via `close window id ...`. Ported verbatim from reference lines 264-285.
    """
    trust_directory(str(cwd))
    runner = cwd / "_run.sh"
    if not runner.exists():
        raise LaunchError(f"_run.sh missing at {runner}")
    runner.chmod(0o755)
    # `; exit 0` makes the shell exit once Claude exits, so the window enters "Process completed".
    script = (
        'tell application "Terminal"\n'
        f'    set t to do script "bash \\"{runner}\\"; exit 0"\n'
        "    return id of window 1 whose tabs contains t\n"
        "end tell\n"
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as e:
        raise LaunchError(f"osascript timed out launching Terminal at {cwd}") from e
    stdout = (result.stdout or "").strip()
    if result.returncode != 0 or not stdout.isdigit():
        raise LaunchError(
            f"osascript launch failed (rc={result.returncode}): {(result.stderr or '').strip()}"
        )
    window_id = int(stdout)
    print(f"[launcher] Terminal launched at {cwd} (window {window_id})", file=sys.stderr)
    return window_id
