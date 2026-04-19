"""Single-terminal orchestration: one job -> one session -> one result."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from worker.config.render import render_prompt
from worker.db.queries import requeue_job, update_job_status, write_job_result
from worker.observability.events import JOB_COMPLETED, JOB_FAILED, SESSION_LAUNCHED, emit
from worker.terminal.launcher import LaunchError, launch_terminal_window, write_prompt_file, write_runner_script
from worker.terminal.shutdown import cleanup_session_data, exit_claude_and_close_window
from worker.terminal.watchdog import (
    RESULT_COMPLETED,
    RESULT_SHUTDOWN,
    wait_for_completion,
)

if TYPE_CHECKING:
    from worker.config.models import Config, JobType


def _read_result(cwd: Path, filename: str):
    fp = cwd / filename
    if not fp.exists():
        return None
    text = fp.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Non-JSON result files are preserved as raw strings so consumers can still inspect them.
        return {"raw": text}


def run_single(client, cfg: Config, job: dict, job_type: JobType) -> None:
    job_id = job["id"]
    tmpdir = Path(tempfile.mkdtemp(prefix="minicrew_", dir="/tmp"))
    window_id: int | None = None
    try:
        prompt = render_prompt(cfg, job_type, job)
        write_prompt_file(tmpdir, prompt)
        write_runner_script(tmpdir)

        # Mark the job as actually started at launch time — distinct from claim, so fan_out mode's
        # group queueing semantics are preserved even when we're the single-terminal path.
        update_job_status(client, cfg, job_id, status="running", set_started_at=True)

        window_id = launch_terminal_window(tmpdir)
        emit(SESSION_LAUNCHED, job_id=job_id, mode="single", window_id=window_id)

        outcome = wait_for_completion(
            cwd=tmpdir,
            window_id=window_id,
            result_filename=job_type.result_filename,
            overall_timeout_seconds=job_type.timeout_seconds,
            idle_timeout_seconds=job_type.idle_timeout_seconds,
            result_idle_timeout_seconds=job_type.result_idle_timeout_seconds,
        )
        window_id = None  # watchdog already closed the window

        if outcome == RESULT_SHUTDOWN:
            requeue_job(client, cfg, job_id, reason="worker shutting down")
            return

        if outcome == RESULT_COMPLETED:
            result = _read_result(tmpdir, job_type.result_filename)
            write_job_result(client, cfg, job_id, result)
            emit(JOB_COMPLETED, job_id=job_id, mode="single")
            return

        # timeout / error
        update_job_status(
            client,
            cfg,
            job_id,
            status="error",
            error_message=f"session ended with {outcome}",
            set_completed_at=True,
        )
        emit(JOB_FAILED, job_id=job_id, mode="single", reason=outcome)

    except LaunchError as e:
        update_job_status(
            client,
            cfg,
            job_id,
            status="error",
            error_message=str(e),
            set_completed_at=True,
        )
        emit(JOB_FAILED, job_id=job_id, reason="launch_error", error=str(e))
    except Exception as e:
        update_job_status(
            client,
            cfg,
            job_id,
            status="error",
            error_message=str(e),
            set_completed_at=True,
        )
        emit(JOB_FAILED, job_id=job_id, reason="exception", error=str(e))
    finally:
        if window_id is not None:
            exit_claude_and_close_window(window_id)
        cleanup_session_data(tmpdir)
        shutil.rmtree(tmpdir, ignore_errors=True)
