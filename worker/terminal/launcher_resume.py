"""Runner-script writer for `claude --resume` flows (mode: handoff).

Separate file from `launcher.py` to honor CLAUDE.md's load-bearing-file rule:
single / fan_out / ad_hoc keep using the byte-identical `write_runner_script` from
`launcher.py`; only handoff resumes call into here.

The effort-budget mapping is sourced from ``worker.terminal._runner_common`` so the
two runner-emitting modules can't drift. ``launcher.py`` still has a private
``_EFFORT_MAP`` — keep both in sync until ``launcher.py`` can import from
``_runner_common``.
"""
from __future__ import annotations

import os
import shlex
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from worker.terminal._runner_common import EFFORT_MAP

if TYPE_CHECKING:
    from worker.config.models import JobType


def write_runner_script_resume(
    cwd: Path,
    *,
    job_type: JobType,
    log_path: Path,
    resume_session_id: str,
) -> Path:
    """Emit a runner that resumes a Claude Code session headlessly:

        claude --resume <id> --print --dangerously-skip-permissions \\
               --model X --effort Y "$(cat _prompt.txt)" 2>&1 | tee <log_path>

    `--print` is REQUIRED for headless `--resume`: without it the resumed session
    drops into an interactive REPL and never exits.
    """
    real_cwd = os.path.realpath(str(cwd))
    runner = cwd / "_run.sh"
    effort = EFFORT_MAP.get(job_type.thinking_budget, "medium")
    parts = [
        "claude",
        "--resume", shlex.quote(resume_session_id),
        "--print",
        "--dangerously-skip-permissions",
        "--model", shlex.quote(job_type.model),
        "--effort", shlex.quote(effort),
        '"$(cat _prompt.txt)"',
    ]
    claude_cmd = " ".join(parts) + f" 2>&1 | tee {shlex.quote(str(log_path))}"
    preamble = "sleep 1\n" if sys.platform == "linux" else ""
    # Recursion guard: every worker session exports MINICREW_INSIDE_WORKER=1 so
    # the Minicrew Mac Mini custom agent refuses to dispatch back into the fleet.
    runner.write_text(
        "#!/bin/bash\n"
        "export MINICREW_INSIDE_WORKER=1\n"
        f"{preamble}"
        f"cd {shlex.quote(real_cwd)}\n"
        f"{claude_cmd}\n",
        encoding="utf-8",
    )
    runner.chmod(runner.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return runner
