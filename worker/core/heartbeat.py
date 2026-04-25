"""Heartbeat loop — upserts the worker's status row.

Cadence: every 30s when idle, every 10s while a job is running so dispatcher-side
cancel signals (`requested_status='cancel'`) are observed within ~10s.

Ported from the reference implementation (lines 1023-1050) with one retry inside the loop.
"""
from __future__ import annotations

import socket
import threading
import time

import worker.core.state as state
from worker.db.queries import heartbeat_upsert
from worker.observability.events import HEARTBEAT_ERROR, emit

HEARTBEAT_INTERVAL_IDLE_SECONDS = 30
HEARTBEAT_INTERVAL_BUSY_SECONDS = 10
# Kept for backwards compatibility with anything that imported the original constant.
HEARTBEAT_INTERVAL_SECONDS = HEARTBEAT_INTERVAL_IDLE_SECONDS
_SLEEP_CHUNK_SECONDS = 6


def _interruptible_sleep(total_seconds: int) -> None:
    """Sleep in chunks, checking shutdown_requested between each, so graceful-shutdown
    latency stays bounded regardless of the overall heartbeat interval.
    """
    remaining = total_seconds
    while remaining > 0 and not state.shutdown_requested:
        chunk = min(_SLEEP_CHUNK_SECONDS, remaining)
        time.sleep(chunk)
        remaining -= chunk


def _poll_requested_status(client, cfg, job_id: str) -> None:
    """Best-effort: read requested_status for the given job and flip the cancel flag.

    Wrapped in try/except so a hung GET cannot stall the heartbeat past the
    stale_threshold. The 5s budget is enforced via the client.get pattern; any
    exception (timeout, network error, missing column) is swallowed silently —
    cancellation will be re-checked on the next tick.
    """
    try:
        rows = client.get(
            cfg.db.jobs_table,
            id=job_id,
            select="requested_status",
            limit="1",
        )
        if rows and rows[0].get("requested_status") == "cancel":
            state.set_cancel_requested(job_id)
    except Exception:
        # Silent — cancel polling is best-effort and re-runs every tick.
        pass


def _tick(client, cfg, worker_id: str, instance: int, version: str) -> None:
    current = state.get_current_job()
    status = "busy" if current else "idle"
    heartbeat_upsert(
        client,
        cfg,
        worker_id,
        hostname=socket.gethostname(),
        instance=instance,
        role=cfg.worker.role,
        status=status,
        version=version,
    )
    if current:
        _poll_requested_status(client, cfg, current)


def _loop(client, cfg, worker_id: str, instance: int, version: str) -> None:
    while not state.shutdown_requested:
        try:
            _tick(client, cfg, worker_id, instance, version)
        except Exception as e:
            emit(HEARTBEAT_ERROR, error=str(e))
            # One short retry on transient network errors; upstream loop tolerates gaps.
            if state.shutdown_requested:
                break
            time.sleep(5)
            if state.shutdown_requested:
                break
            try:
                _tick(client, cfg, worker_id, instance, version)
            except Exception as e2:
                emit(HEARTBEAT_ERROR, error=str(e2), retried=True)
        # Tighter cadence while a job is running so a dispatcher-side cancel is
        # observed within ~10s; relaxed back to 30s when idle.
        interval = (
            HEARTBEAT_INTERVAL_BUSY_SECONDS
            if state.get_current_job()
            else HEARTBEAT_INTERVAL_IDLE_SECONDS
        )
        _interruptible_sleep(interval)


def start(client, cfg, worker_id: str, instance: int, version: str) -> threading.Thread:
    """Spawn a daemon thread that heartbeats until shutdown_requested flips."""
    t = threading.Thread(
        target=_loop,
        args=(client, cfg, worker_id, instance, version),
        name="minicrew-heartbeat",
        daemon=True,
    )
    t.start()
    return t
