"""Opportunistic reaper thread — one worker at a time (enforced by advisory lock).

Runs on its own thread so the poll loop stays responsive. Queries inside `run_one_cycle`
go through the locked psycopg connection (NOT PostgREST), guaranteeing no interleaving
writes from another reaper.

When `cfg.dispatch` is configured the reaper additionally sweeps three classes of
dispatch artifacts (outbound transcript retention, orphan inbound transcripts,
orphan MCP bundles). The sweep runs under the same advisory lock as the stale-worker
requeue so only one worker in the fleet performs the work per tick.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import psycopg
from psycopg.rows import dict_row

from worker.db.advisory_lock import reaper_lock
from worker.db.client import PostgrestClient
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


def _outbound_retention_days(cfg: Config) -> int:
    """Resolve the outbound transcript retention window. Default 7 days when unset."""
    if cfg.dispatch is None or cfg.dispatch.handoff is None:
        return 7
    return cfg.dispatch.handoff.outbound_retention_days


def _sweep_dispatch_artifacts(client: PostgrestClient, cfg: Config) -> None:
    """Three sub-sweeps run under the reaper advisory lock (one worker at a time).

    Each sweep is best-effort and idempotent — failures inside one bundle/job do
    not abort the others. The caller wraps the whole function in try/except so a
    sweep crash never kills the reaper thread.
    """
    if cfg.dispatch is None:
        return

    # 1. Outbound transcript retention: bundles attached to jobs that completed
    # more than retention_days ago. Delete the Vault row, also best-effort delete
    # the storage prefix, and clear the column on the jobs row.
    days = _outbound_retention_days(cfg)
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    try:
        expired = client.get(
            cfg.db.jobs_table,
            select="id,final_transcript_bundle_id",
            final_transcript_bundle_id="not.is.null",
            completed_at=f"lt.{cutoff}",
        )
    except httpx.HTTPError as e:
        emit(REAPER_ERROR, error=f"outbound retention query failed: {e}")
        expired = []

    storage_base = cfg.db.url.rstrip("/")
    if storage_base.endswith("/rest/v1"):
        storage_base = storage_base[: -len("/rest/v1")]

    for row in expired:
        bundle_id = row.get("final_transcript_bundle_id")
        job_id = row.get("id")
        if not bundle_id or not job_id:
            continue

        # If the bundle has a storage_ref, resolve the actual Storage key from
        # the decrypted Vault payload so we delete the right object. The buggy
        # earlier path used `transcripts/{job_id}`, which never matched the
        # `transcripts/{session_id}-{ts}.json.gz` key written at register time.
        storage_key: str | None = None
        try:
            bundle_rows = client.get(
                cfg.dispatch.mcp_bundle.decrypted_view,
                id=f"eq.{bundle_id}",
                select="decrypted_secret",
                limit="1",
            )
            if bundle_rows:
                payload = json.loads(bundle_rows[0].get("decrypted_secret") or "{}")
                ref = payload.get("storage_ref") or {}
                key = ref.get("storage_key")
                if isinstance(key, str) and key:
                    storage_key = key
        except (httpx.HTTPError, json.JSONDecodeError, TypeError) as e:
            emit(
                REAPER_ERROR,
                error=f"read bundle {bundle_id} for storage_key failed: {e}",
            )

        if storage_key:
            try:
                httpx.delete(
                    f"{storage_base}/storage/v1/object/{cfg.dispatch.log_storage.bucket}/{storage_key}",
                    headers={
                        "Authorization": f"Bearer {cfg.db.service_key}",
                        "apikey": cfg.db.service_key,
                    },
                    timeout=10,
                )
            except httpx.HTTPError as e:
                emit(
                    REAPER_ERROR,
                    error=f"storage scrub for key {storage_key} failed: {e}",
                )

        try:
            client.rpc("dispatch_delete_transcript_bundle", {"p_id": str(bundle_id)})
        except httpx.HTTPError as e:
            emit(REAPER_ERROR, error=f"delete outbound bundle {bundle_id} failed: {e}")
        try:
            client.patch(
                cfg.db.jobs_table,
                {"final_transcript_bundle_id": None},
                id=str(job_id),
            )
        except httpx.HTTPError as e:
            emit(
                REAPER_ERROR,
                error=f"clear final_transcript_bundle_id on job {job_id} failed: {e}",
            )

    # 2. Orphan inbound transcript bundles older than 24h.
    cutoff_24h = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    try:
        orphans_t = client.get(
            "v_orphan_transcript_bundles",
            select="id",
            created_at=f"lt.{cutoff_24h}",
        )
    except httpx.HTTPError as e:
        emit(REAPER_ERROR, error=f"orphan transcript view query failed: {e}")
        orphans_t = []
    for row in orphans_t:
        bundle_id = row.get("id")
        if not bundle_id:
            continue
        try:
            client.rpc("dispatch_delete_transcript_bundle", {"p_id": str(bundle_id)})
        except httpx.HTTPError as e:
            emit(REAPER_ERROR, error=f"delete orphan transcript {bundle_id} failed: {e}")

    # 3. Orphan MCP bundles older than 24h.
    try:
        orphans_m = client.get(
            "v_orphan_mcp_bundles",
            select="id",
            created_at=f"lt.{cutoff_24h}",
        )
    except httpx.HTTPError as e:
        emit(REAPER_ERROR, error=f"orphan mcp view query failed: {e}")
        orphans_m = []
    for row in orphans_m:
        bundle_id = row.get("id")
        if not bundle_id:
            continue
        try:
            client.rpc(cfg.dispatch.mcp_bundle.delete_rpc, {"p_id": str(bundle_id)})
        except httpx.HTTPError as e:
            emit(REAPER_ERROR, error=f"delete orphan mcp bundle {bundle_id} failed: {e}")


def reaper_thread(cfg: Config, stop_event: threading.Event) -> None:
    # Reuse a single PostgREST client across ticks so we don't pay TLS handshake
    # cost on every interval. Lifecycle bound to this thread.
    rest_client: PostgrestClient | None = None
    if cfg.dispatch is not None:
        rest_client = PostgrestClient(cfg.db.url, cfg.db.service_key)
    try:
        while not stop_event.is_set():
            try:
                with reaper_lock(cfg.db.direct_url) as (acquired, conn):
                    if not acquired:
                        # Another worker holds the lock this cycle; just wait.
                        stop_event.wait(cfg.reaper.interval_seconds)
                        continue
                    start = time.time()
                    count_requeued = run_one_cycle(cfg, conn)
                    # Dispatch-artifact sweep gated behind the same advisory lock
                    # so N workers don't all attempt the same deletes in parallel.
                    if rest_client is not None:
                        try:
                            _sweep_dispatch_artifacts(rest_client, cfg)
                        except Exception as e:
                            emit(REAPER_ERROR, error=f"dispatch sweep failed: {e}")
                    emit(
                        REAPER_RAN,
                        count_requeued=count_requeued,
                        duration_seconds=round(time.time() - start, 3),
                    )
            except Exception as e:
                emit(REAPER_ERROR, error=str(e))
            stop_event.wait(cfg.reaper.interval_seconds)
    finally:
        if rest_client is not None:
            rest_client.close()
