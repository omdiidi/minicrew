"""Opportunistic reaper thread — one worker at a time (enforced by advisory lock).

Runs on its own thread so the poll loop stays responsive. Queries inside `run_one_cycle`
go through the locked psycopg connection (NOT PostgREST), guaranteeing no interleaving
writes from another reaper.
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import psycopg
from psycopg.rows import dict_row

from worker.db.advisory_lock import reaper_lock
from worker.observability.events import REAPER_ERROR, REAPER_RAN, REAPER_REQUEUED, emit

if TYPE_CHECKING:
    from worker.config.models import Config


def run_one_cycle(cfg: Config, conn: psycopg.Connection) -> int:
    """Find stale workers, mark them offline, call the requeue RPC for each.

    Returns the total number of jobs requeued in this cycle so the caller can emit it.
    """
    total_requeued = 0
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id FROM workers
             WHERE last_heartbeat < NOW() - make_interval(secs => %s)
               AND status != 'offline'
            """,
            (cfg.reaper.stale_threshold_seconds,),
        )
        stale_ids = [r["id"] for r in cur.fetchall()]

        for wid in stale_ids:
            cur.execute("UPDATE workers SET status='offline' WHERE id=%s", (wid,))
            cur.execute(
                "SELECT requeue_stale_jobs_for_worker(%s, %s)",
                (wid, cfg.reaper.max_attempts),
            )
            row = cur.fetchone()
            count = row["requeue_stale_jobs_for_worker"] if row else 0
            total_requeued += count or 0
            emit(REAPER_REQUEUED, worker_id=wid, count=count)
    return total_requeued


def reaper_thread(cfg: Config, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            with reaper_lock(cfg.db.direct_url) as (acquired, conn):
                if not acquired:
                    # Another worker holds the lock this cycle; just wait.
                    stop_event.wait(cfg.reaper.interval_seconds)
                    continue
                start = time.time()
                count_requeued = run_one_cycle(cfg, conn)
                emit(
                    REAPER_RAN,
                    count_requeued=count_requeued,
                    duration_seconds=round(time.time() - start, 3),
                )
        except Exception as e:
            emit(REAPER_ERROR, error=str(e))
        stop_event.wait(cfg.reaper.interval_seconds)
