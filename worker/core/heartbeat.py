"""Heartbeat loop — every 30s upserts the worker's status row.

Ported from the reference implementation (lines 1023-1050) with one retry inside the loop.
"""
from __future__ import annotations

import socket
import threading
import time

import worker.core.state as state
from worker.db.queries import heartbeat_upsert
from worker.observability.events import HEARTBEAT_ERROR, emit

HEARTBEAT_INTERVAL_SECONDS = 30


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
        current_job_id=current,
    )


def _loop(client, cfg, worker_id: str, instance: int, version: str) -> None:
    while not state.shutdown_requested:
        try:
            _tick(client, cfg, worker_id, instance, version)
        except Exception as e:
            emit(HEARTBEAT_ERROR, error=str(e))
            # One short retry on transient network errors; upstream loop tolerates gaps.
            time.sleep(5)
            try:
                _tick(client, cfg, worker_id, instance, version)
            except Exception as e2:
                emit(HEARTBEAT_ERROR, error=str(e2), retried=True)
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)


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
