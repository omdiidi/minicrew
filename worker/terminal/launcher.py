"""Prompt + runner-script writers for a Claude Code session.

Session launch itself has moved to `worker.platform.<os>` — this module now only
prepares the on-disk artifacts (`_prompt.txt`, `_run.sh`) that each platform's
`launch_session` invokes.
"""
from __future__ import annotations

import os
import shlex
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from worker.config.models import JobType


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

    On Linux, a `sleep 1` is prepended so the terminal window's transient title (set via
    `--title=minicrew-<uuid>`) stays match-able by wmctrl during the launch poll window;
    Claude Code's OSC-0 title repaint would otherwise replace it before we ever see it.
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

    preamble = "sleep 1\n" if sys.platform == "linux" else ""
    script = (
        "#!/bin/bash\n"
        f"{preamble}"
        f"cd {shlex.quote(real_cwd)}\n"
        f"{claude_cmd}\n"
    )
    runner.write_text(script, encoding="utf-8")
    runner.chmod(runner.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return runner
