"""Thin service wrapper around db.queries.claim_next_job — for symmetry with the rest of core."""
from __future__ import annotations

from worker.db.queries import claim_next_job


def next_job(client, cfg, worker_id: str, version: str) -> dict | None:
    return claim_next_job(client, cfg, worker_id, version)
