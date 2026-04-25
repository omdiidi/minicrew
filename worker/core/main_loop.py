"""Main poll loop — assembled from the rest of `core`. Called by cli.main()."""
from __future__ import annotations

import socket
import sys
import threading
import time
from dataclasses import dataclass

from dotenv import load_dotenv

import worker.core.claim as claim
import worker.core.heartbeat as heartbeat
import worker.core.reaper as reaper
import worker.core.signals as signals
import worker.core.startup_recovery as startup_recovery
import worker.core.state as state
import worker.orchestration as orchestration
from worker import __version__
from worker.config.loader import load_config
from worker.config.models import Config
from worker.db.client import PostgrestClient
from worker.db.queries import mark_worker_offline, requeue_job
from worker.observability.events import (
    JOB_CLAIMED,
    POLL_LOOP_ERROR,
    WORKER_STARTED,
    WORKER_STOPPED,
    emit,
)
from worker.observability.setup import setup as setup_observability
from worker.platform import detect_platform
from worker.platform.base import PreflightError
from worker.utils.db_url import assert_db_url_is_direct
from worker.utils.paths import repo_root


@dataclass
class RunOptions:
    instance: int = 1
    role: str | None = None
    poll_interval: int | None = None
    config_path: str | None = None


def _resolve_poll_interval(cfg: Config, opts: RunOptions) -> int:
    if opts.poll_interval is not None:
        return opts.poll_interval
    if cfg.worker.poll_interval_seconds is not None:
        return cfg.worker.poll_interval_seconds
    return 5 if cfg.worker.role == "primary" else 15


def run(opts: RunOptions) -> int:
    # F2: load .env at process start so SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY /
    # SUPABASE_DB_URL are available to load_config() without the launchd plist
    # carrying them. The plist now only sets MINICREW_CONFIG_PATH + PATH.
    env_path = repo_root() / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)

    cfg = load_config(opts.config_path)
    if opts.role:
        cfg.worker.role = opts.role
    assert_db_url_is_direct(cfg.db.direct_url)

    worker_id = f"{cfg.worker.prefix}-{socket.gethostname()}-{opts.instance}"
    setup_observability(cfg.logging, worker_id, opts.instance, cfg=cfg)
    emit(WORKER_STARTED, version=__version__, role=cfg.worker.role, instance=opts.instance)

    # Construct the platform once at startup and reuse it for every job.
    # preflight() failure is fatal — we must not enter the poll loop with a
    # broken environment (e.g. Wayland session, missing wmctrl, no DISPLAY).
    platform = detect_platform(cfg)
    try:
        platform.preflight()
    except PreflightError as e:
        emit(POLL_LOOP_ERROR, error=f"preflight failed: {e}")
        print(f"preflight failed: {e}", file=sys.stderr)
        raise

    # When ad_hoc / handoff job_types are configured, run the extended preflight
    # (operator MCP isolation, GitHub App, Storage bucket reachability + anon-block,
    # required server-side RPCs). Same fail-fast policy as the basic preflight.
    if cfg.dispatch is not None:
        try:
            platform.dispatch_preflight(cfg)
        except PreflightError as e:
            emit(POLL_LOOP_ERROR, error=f"dispatch preflight failed: {e}")
            print(f"dispatch preflight failed: {e}", file=sys.stderr)
            raise

    signals.install()

    client = PostgrestClient(cfg.db.url, cfg.db.service_key)
    try:
        heartbeat.start(client, cfg, worker_id, opts.instance, __version__)

        reaper_stop = threading.Event()
        reaper_t = threading.Thread(
            target=reaper.reaper_thread,
            args=(cfg, reaper_stop),
            name="minicrew-reaper",
            daemon=True,
        )
        reaper_t.start()

        startup_recovery.requeue_own_jobs(client, cfg, worker_id)

        poll_interval = _resolve_poll_interval(cfg, opts)

        while not state.shutdown_requested:
            try:
                job = claim.next_job(client, cfg, worker_id, __version__)
                if job is None:
                    time.sleep(poll_interval)
                    continue
                if state.shutdown_requested:
                    requeue_job(client, cfg, job["id"], worker_id, reason=f"worker {worker_id} shutting down")
                    break
                emit(JOB_CLAIMED, job_id=job["id"], job_type=job.get("job_type"))
                state.set_current_job(job["id"])
                try:
                    orchestration.run(client, cfg, job, worker_id=worker_id, platform=platform)
                finally:
                    state.set_current_job(None)
            except Exception as e:
                emit(POLL_LOOP_ERROR, error=str(e))
                time.sleep(poll_interval)

        reaper_stop.set()
        try:
            mark_worker_offline(client, cfg, worker_id)
        except Exception as e:
            emit(POLL_LOOP_ERROR, error=f"mark_worker_offline failed: {e}")
        emit(WORKER_STOPPED)
        return 0
    finally:
        client.close()
