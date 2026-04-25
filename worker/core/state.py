"""Shared process-level state — kept minimal and explicit so tests can monkeypatch it."""
from __future__ import annotations

import threading

# Module-level flag flipped by signal handlers; read from every blocking loop.
shutdown_requested: bool = False

# Guards current_job_id across the heartbeat thread + main loop.
lock: threading.Lock = threading.Lock()

# Id of the job currently executing on this worker, or None when idle.
current_job_id: str | None = None

# Id of the job for which a cancel was requested (matched by the job runner before
# starting / between phases). Cleared whenever the current job slot is cleared so
# the flag can never carry from one job to the next.
cancel_requested_for: str | None = None


def request_shutdown() -> None:
    global shutdown_requested
    shutdown_requested = True


def set_current_job(job_id: str | None) -> None:
    global current_job_id, cancel_requested_for
    with lock:
        current_job_id = job_id
        if job_id is None:
            cancel_requested_for = None


def get_current_job() -> str | None:
    with lock:
        return current_job_id


def set_cancel_requested(job_id: str) -> None:
    global cancel_requested_for
    with lock:
        cancel_requested_for = job_id


def clear_cancel_requested() -> None:
    global cancel_requested_for
    with lock:
        cancel_requested_for = None


def is_cancel_requested(job_id: str) -> bool:
    with lock:
        return cancel_requested_for == job_id
