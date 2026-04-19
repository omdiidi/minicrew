"""Launch a Claude Code session inside a visible Terminal.app window.

Ported from the reference implementation (lines 175-285) with every domain reference stripped.
"""
from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from worker.utils.paths import trust_directory

if TYPE_CHECKING:
    from worker.config.models import JobType


class LaunchError(RuntimeError):
    """Raised when the Terminal.app session cannot be opened."""


# Map config `thinking_budget` -> Claude Code CLI `--effort` flag. Explicit mapping so
# a consumer typo doesn't silently fall through to an unsupported value. Keep in sync with
# the enum in schema/config.schema.json.
_EFFORT_MAP = {
    "none": "low",
    "medium": "medium",
    "high": "high",
}


def write_prompt_file(cwd: Path, prompt: str) -> Path:
    prompt_path = cwd / "_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    return prompt_path


def write_runner_script(
    cwd: Path,
    *,
    job_type: JobType | None = None,
    log_path: Path | None = None,
) -> Path:
    """Write `_run.sh` that execs Claude Code with the prompt text.

    Uses the exact reference pattern `claude --dangerously-skip-permissions "$(cat _prompt.txt)"`.
    When `job_type` is provided, the model and effort level are passed as CLI flags.
    When `log_path` is provided, stdout+stderr are tee'd into it.
    """
    real_cwd = os.path.realpath(str(cwd))
    runner = cwd / "_run.sh"

    if job_type is not None:
        effort = _EFFORT_MAP.get(job_type.thinking_budget, "medium")
        model_arg = f"--model {shlex.quote(job_type.model)}"
        effort_arg = f"--effort {shlex.quote(effort)}"
    else:
        model_arg = ""
        effort_arg = ""

    claude_cmd = (
        f'claude --dangerously-skip-permissions {model_arg} {effort_arg} '
        f'"$(cat _prompt.txt)"'
    ).strip()
    if log_path is not None:
        claude_cmd = f'{claude_cmd} 2>&1 | tee {shlex.quote(str(log_path))}'

    script = (
        "#!/bin/bash\n"
        f"cd {shlex.quote(real_cwd)}\n"
        f"{claude_cmd}\n"
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
    # F10: defense-in-depth — escape backslash and double-quote in the runner path before
    # interpolating into the AppleScript literal. Current callers produce random tempdir
    # paths, but an attacker who influences the path shouldn't be able to break out of the
    # "do script" string.
    runner_escaped = str(runner).replace("\\", "\\\\").replace('"', '\\"')
    # `; exit 0` makes the shell exit once Claude exits, so the window enters "Process completed".
    script = (
        'tell application "Terminal"\n'
        f'    set t to do script "bash \\"{runner_escaped}\\"; exit 0"\n'
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
