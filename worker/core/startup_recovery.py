"""Boot-time sweep: re-queue any 'running' jobs left stamped with this worker_id.

If a worker crashed hard (power loss, OOM), its jobs will still be in 'running' with
its worker_id — nothing else reaps them because from the reaper's perspective the
worker just came back online. This runs before the poll loop to clear that state.
"""
from __future__ import annotations

from worker.db.queries import get_own_running_jobs, requeue_job
from worker.observability.events import STARTUP_REQUEUED, emit


def requeue_own_jobs(client, cfg, worker_id: str) -> int:
    stale = get_own_running_jobs(client, cfg, worker_id)
    count = 0
    for row in stale:
        requeue_job(client, cfg, row["id"], worker_id, reason=f"startup recovery: worker {worker_id} restarted")
        emit(STARTUP_REQUEUED, job_id=row["id"])
        count += 1
    return count
