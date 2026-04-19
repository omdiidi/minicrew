"""Repository layer: every PostgREST mutation/read used by services lives here."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from worker.observability.events import POLL_LOOP_ERROR, emit

if TYPE_CHECKING:
    from worker.config.models import Config
    from worker.db.client import PostgrestClient


class ClaimError(RuntimeError):
    """Raised when atomic claim interaction with PostgREST fails unexpectedly."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def claim_next_job(
    client: PostgrestClient, cfg: Config, worker_id: str, version: str
) -> dict | None:
    """Atomically claim the highest-priority pending job for this worker.

    Returns the claimed job dict, or None if no job was available / another worker
    won the race.
    """
    rows = client.get(
        cfg.db.jobs_table,
        status="pending",
        order="priority.desc,created_at.asc",
        limit="1",
        select="*",
    )
    if not rows:
        return None
    job = rows[0]

    expires_at = job.get("expires_at")
    if expires_at:
        # The reference parses `...Z` suffixes; .fromisoformat accepts them from 3.11+
        # but we normalize anyway for older serialization paths.
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if expires < datetime.now(UTC):
            # Filter on status='pending' so we don't race another worker that just claimed
            # it; set completed_at so worker_stats time-bounded counts register the cancel.
            client.patch(
                cfg.db.jobs_table,
                {"status": "cancelled", "completed_at": _now_iso()},
                id=job["id"],
                status="pending",
            )
            return None

    # The second filter `status="pending"` is what provides atomicity — if another
    # worker flipped the row to 'running' between the GET and this PATCH, row count
    # is zero and we return None.
    claimed = client.patch(
        cfg.db.jobs_table,
        {
            "status": "running",
            "worker_id": worker_id,
            "claimed_at": _now_iso(),
            "worker_version": version,
        },
        id=job["id"],
        status="pending",
    )
    if not claimed:
        return None
    return claimed[0]


def update_job_status(
    client: PostgrestClient,
    cfg: Config,
    job_id: str,
    worker_id: str,
    *,
    status: str,
    error_message: str | None = None,
    set_completed_at: bool = False,
    set_started_at: bool = False,
) -> bool:
    """Ownership-filtered status update. Returns True if a row was written, False otherwise.

    The PATCH is filtered by `id` AND `worker_id` so a reaper that already requeued the
    job (clearing worker_id) cannot be overwritten by a late-arriving update from the
    original (stale) worker.
    """
    patch: dict[str, Any] = {"status": status}
    if error_message is not None:
        patch["error_message"] = error_message[:2000]
    elif status == "completed":
        # I14: clear any stale error_message from a previous failed attempt.
        patch["error_message"] = None
    if set_completed_at:
        patch["completed_at"] = _now_iso()
    if set_started_at:
        patch["started_at"] = _now_iso()
    rows = client.patch(cfg.db.jobs_table, patch, id=job_id, worker_id=worker_id)
    if not rows:
        emit(
            POLL_LOOP_ERROR,
            error="update_job_status: no rows updated (worker lost ownership)",
            job_id=job_id,
            worker_id=worker_id,
            attempted_status=status,
        )
        return False
    return True


def write_job_result(
    client: PostgrestClient, cfg: Config, job_id: str, worker_id: str, result: Any
) -> bool:
    """Ownership-filtered result write. Returns True if a row was written, False otherwise."""
    rows = client.patch(
        cfg.db.jobs_table,
        {
            "result": result,
            "status": "completed",
            "completed_at": _now_iso(),
            "error_message": None,
        },
        id=job_id,
        worker_id=worker_id,
        status="running",
    )
    if not rows:
        emit(
            POLL_LOOP_ERROR,
            error="write_job_result: no rows updated (worker lost ownership)",
            job_id=job_id,
            worker_id=worker_id,
        )
        return False
    return True


def requeue_job(
    client: PostgrestClient,
    cfg: Config,
    job_id: str,
    reason: str,
) -> None:
    """Requeue a running job, incrementing attempt_count and honoring max_attempts.

    Mirrors the reaper's poison-pill logic so startup-recovery retries count toward
    the job's retry budget. TODO: collapse into a single RPC call once the reaper
    RPC grows to accept a caller-supplied reason.
    """
    rows = client.get(
        cfg.db.jobs_table,
        id=job_id,
        select="attempt_count,max_attempts",
        limit="1",
    )
    if not rows:
        return
    current = int(rows[0].get("attempt_count") or 0)
    per_row_max = rows[0].get("max_attempts")
    effective_max = int(per_row_max) if per_row_max is not None else int(cfg.reaper.max_attempts)
    next_attempt = current + 1
    if next_attempt > effective_max:
        client.patch(
            cfg.db.jobs_table,
            {
                "status": "failed_permanent",
                "worker_id": None,
                "started_at": None,
                "claimed_at": None,
                "attempt_count": next_attempt,
                "completed_at": _now_iso(),
                "error_message": (
                    f"Exceeded max_attempts={effective_max}: {reason}"
                )[:2000],
            },
            id=job_id,
        )
    else:
        client.patch(
            cfg.db.jobs_table,
            {
                "status": "pending",
                "worker_id": None,
                "started_at": None,
                "claimed_at": None,
                "attempt_count": next_attempt,
                "error_message": reason[:2000],
            },
            id=job_id,
        )


def get_own_running_jobs(
    client: PostgrestClient, cfg: Config, worker_id: str
) -> list[dict]:
    return client.get(
        cfg.db.jobs_table,
        status="running",
        worker_id=worker_id,
        select="id",
    )


def heartbeat_upsert(
    client: PostgrestClient,
    cfg: Config,
    worker_id: str,
    *,
    hostname: str,
    instance: int,
    role: str,
    status: str,
    version: str,
) -> None:
    client.upsert(
        cfg.db.workers_table,
        {
            "id": worker_id,
            "hostname": hostname,
            "instance": instance,
            "role": role,
            "status": status,
            "last_heartbeat": _now_iso(),
            "version": version,
        },
        on_conflict="id",
    )


def mark_worker_offline(client: PostgrestClient, cfg: Config, worker_id: str) -> None:
    client.upsert(
        cfg.db.workers_table,
        {
            "id": worker_id,
            "status": "offline",
            "last_heartbeat": _now_iso(),
        },
        on_conflict="id",
    )


def get_workers(client: PostgrestClient, cfg: Config) -> list[dict]:
    return client.get(cfg.db.workers_table, select="*", order="id.asc")


def get_worker_stats(client: PostgrestClient) -> dict:
    """Read the aggregates view defined in schema/template.sql.

    PostgREST can't do COUNT/GROUP BY natively, so we expose a single-row view.
    """
    rows = client.get("worker_stats", select="*")
    return rows[0] if rows else {}
