"""Data-access layer: PostgREST client + queries + advisory-lock helper."""
from __future__ import annotations

from worker.db.client import PostgrestClient
from worker.db.queries import (
    claim_next_job,
    get_workers,
    mark_worker_offline,
    update_job_status,
    write_job_result,
)

__all__ = [
    "PostgrestClient",
    "claim_next_job",
    "get_workers",
    "mark_worker_offline",
    "update_job_status",
    "write_job_result",
]
