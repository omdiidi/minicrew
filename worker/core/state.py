"""Shared process-level state — kept minimal and explicit so tests can monkeypatch it."""
from __future__ import annotations

import threading

# Module-level flag flipped by signal handlers; read from every blocking loop.
shutdown_requested: bool = False

# Guards current_job_id across the heartbeat thread + main loop.
lock: threading.Lock = threading.Lock()

# Id of the job currently executing on this worker, or None when idle.
current_job_id: str | None = None


def request_shutdown() -> None:
    global shutdown_requested
    shutdown_requested = True


def set_current_job(job_id: str | None) -> None:
    global current_job_id
    with lock:
        current_job_id = job_id


def get_current_job() -> str | None:
    with lock:
        return current_job_id
