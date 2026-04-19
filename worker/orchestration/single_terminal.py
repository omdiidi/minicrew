"""Single-terminal orchestration: one job -> one session -> one result."""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from worker.config.render import render_prompt
from worker.db.queries import requeue_job, update_job_status, write_job_result
from worker.observability.events import JOB_COMPLETED, JOB_FAILED, SESSION_LAUNCHED, emit
from worker.orchestration.result_io import read_result_safe
from worker.terminal.launcher import LaunchError, launch_terminal_window, write_prompt_file, write_runner_script
from worker.terminal.shutdown import cleanup_session_data, exit_claude_and_close_window
from worker.terminal.watchdog import (
    RESULT_COMPLETED,
    RESULT_SHUTDOWN,
    wait_for_completion,
)
from worker.utils.paths import repo_root, tmp_root

if TYPE_CHECKING:
    from worker.config.models import Config, JobType


def _job_log_path(cfg: Config, job_id: str) -> Path | None:
    """If job_output.capture is on, compute <repo>/logs/jobs/<job_id>.log and mkdir parents."""
    job_output = cfg.logging.job_output or {}
    if not job_output.get("capture"):
        return None
    path = repo_root() / "logs" / "jobs" / f"{job_id}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def run_single(client, cfg: Config, job: dict, job_type: JobType, *, worker_id: str) -> None:
    job_id = job["id"]
    started = time.time()
    tmpdir = Path(tempfile.mkdtemp(prefix="minicrew_", dir=str(tmp_root())))
    window_id: int | None = None
    try:
        prompt = render_prompt(cfg, job_type, job)
        write_prompt_file(tmpdir, prompt)
        log_path = _job_log_path(cfg, job_id)
        write_runner_script(tmpdir, job_type=job_type, log_path=log_path)

        try:
            window_id = launch_terminal_window(tmpdir)
        except LaunchError as e:
            # C8: started_at must NOT be set on launch failure.
            update_job_status(
                client,
                cfg,
                job_id,
                worker_id,
                status="error",
                error_message=str(e),
                set_completed_at=True,
            )
            emit(JOB_FAILED, job_id=job_id, reason="launch_error", error=str(e))
            return

        # C8: set started_at only after the terminal is actually open.
        update_job_status(client, cfg, job_id, worker_id, status="running", set_started_at=True)
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
            requeue_job(client, cfg, job_id, worker_id, reason="worker shutting down")
            return

        if outcome == RESULT_COMPLETED:
            result = read_result_safe(tmpdir, job_type.result_filename)
            if result is None:
                update_job_status(
                    client,
                    cfg,
                    job_id,
                    worker_id,
                    status="error",
                    error_message="result file unreadable (symlink or traversal rejected)",
                    set_completed_at=True,
                )
                emit(JOB_FAILED, job_id=job_id, mode="single", reason="result_read_failed")
                return
            write_job_result(client, cfg, job_id, worker_id, result)
            emit(
                JOB_COMPLETED,
                job_id=job_id,
                mode="single",
                duration_seconds=round(time.time() - started, 3),
            )
            return

        # timeout / error
        update_job_status(
            client,
            cfg,
            job_id,
            worker_id,
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
            worker_id,
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
            worker_id,
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
