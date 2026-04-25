"""Single-terminal orchestration: one job -> one session -> one result."""
from __future__ import annotations

import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import worker.core.state as state
from worker.config.render import render_prompt
from worker.db.queries import (
    requeue_job,
    set_status_cancelled,
    update_job_status,
    write_job_result,
)
from worker.integrations.log_streamer import ChunkedLogStreamer, ProgressTailer
from worker.observability.events import (
    JOB_CANCELLED,
    JOB_COMPLETED,
    JOB_FAILED,
    SESSION_LAUNCHED,
    emit,
)
from worker.orchestration.result_io import read_result_safe
from worker.platform.base import LaunchError
from worker.terminal.launcher import write_prompt_file, write_runner_script
from worker.terminal.shutdown import cleanup_session_data
from worker.terminal.watchdog import (
    RESULT_CANCELLED,
    RESULT_COMPLETED,
    RESULT_SHUTDOWN,
    wait_for_completion,
)
from worker.utils.paths import repo_root, tmp_root

if TYPE_CHECKING:
    from worker.config.models import Config, JobType
    from worker.platform.base import Platform, SessionHandle


def _job_log_path(cfg: Config, job_id: str) -> Path | None:
    """If job_output.capture is on, compute <repo>/logs/jobs/<job_id>.log and mkdir parents."""
    job_output = cfg.logging.job_output or {}
    if not job_output.get("capture"):
        return None
    path = repo_root() / "logs" / "jobs" / f"{job_id}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def run_single(
    client,
    cfg: Config,
    job: dict,
    job_type: JobType,
    *,
    worker_id: str,
    platform: Platform,
) -> None:
    job_id = job["id"]
    started = time.time()
    tmpdir = Path(tempfile.mkdtemp(prefix="minicrew_", dir=str(tmp_root())))
    handle: SessionHandle | None = None
    stop = threading.Event()
    streamers: list[threading.Thread] = []
    try:
        prompt = render_prompt(cfg, job_type, job)
        write_prompt_file(tmpdir, prompt)
        log_path = _job_log_path(cfg, job_id)
        write_runner_script(tmpdir, job_type=job_type, log_path=log_path)

        try:
            handle = platform.launch_session(tmpdir)
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
        emit(
            SESSION_LAUNCHED,
            job_id=job_id,
            mode="single",
            window_id=handle.data.get("window_id"),
            handle_kind=handle.kind,
        )

        # S13 invariant: side threads only when dispatch is configured. Pure-batch
        # installs ship zero new threads from the orchestrator.
        if cfg.dispatch is not None:
            pt = ProgressTailer(
                client=client,
                cfg=cfg,
                job_id=job_id,
                worker_id=worker_id,
                cwd=tmpdir,
                stop_event=stop,
            )
            pt.start()
            streamers.append(pt)
            if cfg.dispatch.log_storage is not None and log_path is not None:
                ls_cfg = cfg.dispatch.log_storage
                retention_seconds = ls_cfg.retention_days * 86400
                ls = ChunkedLogStreamer(
                    supabase_base_url=cfg.db.url,
                    service_key=cfg.db.service_key,
                    bucket=ls_cfg.bucket,
                    prefix=str(job_id),
                    log_path=log_path,
                    chunk_bytes=ls_cfg.chunk_bytes,
                    interval=ls_cfg.chunk_interval_seconds,
                    retention_seconds=retention_seconds,
                    on_first_upload=lambda url: client.patch(
                        cfg.db.jobs_table,
                        {"caller_log_url": url},
                        id=job_id,
                        worker_id=worker_id,
                        status="running",
                    ),
                    stop_event=stop,
                )
                ls.start()
                streamers.append(ls)

        outcome = wait_for_completion(
            cwd=tmpdir,
            handle=handle,
            platform=platform,
            result_filename=job_type.result_filename,
            overall_timeout_seconds=job_type.timeout_seconds,
            idle_timeout_seconds=job_type.idle_timeout_seconds,
            result_idle_timeout_seconds=job_type.result_idle_timeout_seconds,
            cancel_check=lambda: state.is_cancel_requested(job_id),
        )
        handle = None  # watchdog already closed the window

        # Stop streamers BEFORE writing terminal status to avoid post-completion races.
        stop.set()
        for s in streamers:
            s.join(timeout=5)
        streamers.clear()

        if outcome == RESULT_SHUTDOWN:
            requeue_job(client, cfg, job_id, worker_id, reason="worker shutting down")
            return

        if outcome == RESULT_CANCELLED:
            set_status_cancelled(client, cfg, job_id, worker_id)
            emit(JOB_CANCELLED, job_id=job_id, mode="single")
            return

        if outcome == RESULT_COMPLETED:
            result = read_result_safe(tmpdir, job_type.result_filename, schema=job_type.result_schema)
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
            if not result.ok:
                update_job_status(
                    client,
                    cfg,
                    job_id,
                    worker_id,
                    status="error",
                    error_message=result.error or "result validation failed",
                    set_completed_at=True,
                )
                emit(JOB_FAILED, job_id=job_id, mode="single", reason="result_invalid", error=result.error)
                return
            write_job_result(client, cfg, job_id, worker_id, result.value)
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
        # Defensive double-stop: the happy path already stopped before write_job_result;
        # exception/early-return paths land here with streamers still running.
        stop.set()
        for s in streamers:
            s.join(timeout=5)
        if handle is not None:
            platform.close_session(handle)
        cleanup_session_data(tmpdir)
        shutil.rmtree(tmpdir, ignore_errors=True)
