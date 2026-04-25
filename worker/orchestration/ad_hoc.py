"""ad_hoc orchestration: clone -> per-job .claude/ -> optional pre-create branch ->
run peer Claude session -> optional push -> conditional cleanup.

Bundle deletion is gated on `bundle_safe_to_delete`, set True only on terminal
outcomes (completed / cancelled / error). On RESULT_SHUTDOWN we requeue and leave
the bundles intact so the next attempt can fetch them.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import worker.core.state as state
from worker.config.render import render_builtin_ad_hoc
from worker.db.queries import (
    requeue_job,
    set_status_cancelled,
    update_job_status,
    write_job_result,
)
from worker.integrations.github_app import (
    authenticated_clone,
    mint_install_token,
    precreate_branch,
    push_branch,
    remove_origin,
)
from worker.integrations.log_streamer import ChunkedLogStreamer, ProgressTailer
from worker.integrations.secret_bundle import (
    SecretBundleError,
    delete_bundle,
    fetch_bundle,
)
from worker.observability.events import (
    JOB_CANCELLED,
    JOB_COMPLETED,
    JOB_FAILED,
    SESSION_LAUNCHED,
    emit,
)
from worker.orchestration.result_io import read_result_safe
from worker.platform.base import LaunchError
from worker.terminal.launcher import write_prompt_file, write_runner_script
from worker.terminal.shutdown import cleanup_session_data
from worker.terminal.watchdog import (
    RESULT_CANCELLED,
    RESULT_COMPLETED,
    RESULT_SHUTDOWN,
    wait_for_completion,
)
from worker.utils.paths import repo_root, tmp_root

if TYPE_CHECKING:
    from worker.config.models import Config, JobType
    from worker.platform.base import Platform, SessionHandle


def run_ad_hoc(
    client,
    cfg: Config,
    job: dict,
    job_type: JobType,
    *,
    worker_id: str,
    platform: Platform,
) -> None:
    job_id = job["id"]
    payload = job.get("payload") or {}
    started = time.time()
    tmpdir = Path(tempfile.mkdtemp(prefix="minicrew_adhoc_", dir=str(tmp_root())))
    handle: SessionHandle | None = None
    bundle_id = job.get("mcp_bundle_id")
    bundle_safe_to_delete = False  # gates final cleanup; True only on terminal outcomes
    stop = threading.Event()
    streamers: list[threading.Thread] = []
    cached_token: str | None = None

    def _fail(msg: str) -> None:
        update_job_status(
            client, cfg, job_id, worker_id,
            status="error", error_message=msg, set_completed_at=True,
        )
        emit(JOB_FAILED, job_id=job_id, mode="ad_hoc", reason=msg)

    try:
        # 1. Validate payload shape.
        repo_spec = payload.get("repo") or {}
        prompt_text = payload.get("prompt")
        allow_push = bool(payload.get("allow_code_push", False))
        if not prompt_text or not isinstance(prompt_text, str):
            _fail("payload.prompt missing or not a string")
            bundle_safe_to_delete = True
            return
        if not all(k in repo_spec for k in ("url", "sha")):
            _fail("payload.repo must have url and sha")
            bundle_safe_to_delete = True
            return

        # Cancel checkpoint 1: pre-clone.
        if state.is_cancel_requested(job_id):
            set_status_cancelled(client, cfg, job_id, worker_id)
            emit(JOB_CANCELLED, job_id=job_id, mode="ad_hoc", reason="cancelled before clone")
            bundle_safe_to_delete = True
            return

        # 2. Mint token (cached for clone + push) + clone.
        clone_dir = tmpdir / "repo"
        try:
            cached_token = mint_install_token(cfg)
            authenticated_clone(
                repo_spec["url"], repo_spec["sha"], clone_dir, cached_token,
                timeout=cfg.dispatch.github_app.clone_timeout_seconds or 300,
                cancel_check=lambda: state.is_cancel_requested(job_id),
            )
        except Exception as e:
            # Cancellation surfaces here as GitHubAppError("cancelled before <stage>")
            if "cancelled before" in str(e):
                set_status_cancelled(client, cfg, job_id, worker_id)
                emit(JOB_CANCELLED, job_id=job_id, mode="ad_hoc", reason=str(e))
                bundle_safe_to_delete = True
                return
            _fail(f"clone failed: {e}")
            bundle_safe_to_delete = True
            return

        # Cancel checkpoint 2: post-clone.
        if state.is_cancel_requested(job_id):
            set_status_cancelled(client, cfg, job_id, worker_id)
            emit(JOB_CANCELLED, job_id=job_id, mode="ad_hoc", reason="cancelled after clone")
            bundle_safe_to_delete = True
            return

        # 3. Defense-in-depth: when push is not authorized, drop the origin remote.
        if not allow_push:
            remove_origin(clone_dir)
        else:
            try:
                precreate_branch(clone_dir, f"minicrew/result/{job_id}")
            except Exception as e:
                _fail(f"branch precreate failed: {e}")
                bundle_safe_to_delete = True
                return

        # 4. Fetch & write per-job .claude/settings.json (MCP).
        if bundle_id:
            try:
                bundle = fetch_bundle(client, cfg, bundle_id)
            except SecretBundleError as e:
                _fail(f"mcp bundle fetch failed: {e}")
                bundle_safe_to_delete = True
                return
            claude_dir = clone_dir / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)
            settings_path = claude_dir / "settings.json"
            settings_path.write_text(
                json.dumps({"mcpServers": bundle["mcpServers"]}, indent=2),
                encoding="utf-8",
            )
            settings_path.chmod(0o600)

        # 5. Render wrapper.
        prompt = render_builtin_ad_hoc(
            cfg=cfg, job=job, payload=payload,
            task=prompt_text, allow_code_push=allow_push,
            repo_path=str(clone_dir), repo_url=repo_spec["url"], sha=repo_spec["sha"],
            result_filename=job_type.result_filename,
        )
        write_prompt_file(clone_dir, prompt)

        # 6. Force log capture for ad_hoc (always — needed by ChunkedLogStreamer).
        log_path = repo_root() / "logs" / "jobs" / f"{job_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        write_runner_script(clone_dir, job_type=job_type, log_path=log_path)

        # Cancel checkpoint 3: pre-launch.
        if state.is_cancel_requested(job_id):
            set_status_cancelled(client, cfg, job_id, worker_id)
            emit(JOB_CANCELLED, job_id=job_id, mode="ad_hoc", reason="cancelled before launch")
            bundle_safe_to_delete = True
            return

        # 7. Launch.
        try:
            handle = platform.launch_session(clone_dir)
        except LaunchError as e:
            _fail(f"launch failed: {e}")
            bundle_safe_to_delete = True
            return

        update_job_status(client, cfg, job_id, worker_id, status="running", set_started_at=True)
        emit(
            SESSION_LAUNCHED,
            job_id=job_id,
            mode="ad_hoc",
            window_id=handle.data.get("window_id"),
        )

        # 8. Side threads. ProgressTailer is unconditional for ad_hoc (dispatch is required).
        # ChunkedLogStreamer is gated on log_storage being configured.
        pt = ProgressTailer(
            client=client, cfg=cfg, job_id=job_id,
            worker_id=worker_id, cwd=clone_dir, stop_event=stop,
        )
        pt.start()
        streamers.append(pt)
        if cfg.dispatch.log_storage is not None:
            ls_cfg = cfg.dispatch.log_storage
            retention_seconds = ls_cfg.retention_days * 86400
            ls = ChunkedLogStreamer(
                supabase_base_url=cfg.db.url, service_key=cfg.db.service_key,
                bucket=ls_cfg.bucket, prefix=str(job_id),
                log_path=log_path, chunk_bytes=ls_cfg.chunk_bytes,
                interval=ls_cfg.chunk_interval_seconds,
                retention_seconds=retention_seconds,
                on_first_upload=lambda url: client.patch(
                    cfg.db.jobs_table, {"caller_log_url": url},
                    id=job_id, worker_id=worker_id, status="running",
                ),
                stop_event=stop,
            )
            ls.start()
            streamers.append(ls)

        # 9. Watch.
        outcome = wait_for_completion(
            cwd=clone_dir, handle=handle, platform=platform,
            result_filename=job_type.result_filename,
            overall_timeout_seconds=job_type.timeout_seconds,
            idle_timeout_seconds=job_type.idle_timeout_seconds,
            result_idle_timeout_seconds=job_type.result_idle_timeout_seconds,
            cancel_check=lambda: state.is_cancel_requested(job_id),
        )
        handle = None  # watchdog closed

        # Stop streamers BEFORE writing terminal status to avoid post-state PATCH races.
        stop.set()
        for s in streamers:
            s.join(timeout=5)
        streamers.clear()

        if outcome == RESULT_SHUTDOWN:
            requeue_job(client, cfg, job_id, worker_id, reason="shutdown during ad_hoc")
            # bundle_safe_to_delete stays False — keep bundle for retry.
            return
        if outcome == RESULT_CANCELLED:
            set_status_cancelled(client, cfg, job_id, worker_id)
            emit(JOB_CANCELLED, job_id=job_id, mode="ad_hoc")
            bundle_safe_to_delete = True
            return
        if outcome != RESULT_COMPLETED:
            _fail(f"session ended with {outcome}")
            bundle_safe_to_delete = True
            return

        # 10. Read & validate.
        result = read_result_safe(clone_dir, job_type.result_filename, schema=job_type.result_schema)
        if result is None or not result.ok:
            _fail(result.error if result and result.error else "result file unreadable")
            bundle_safe_to_delete = True
            return
        value = result.value if isinstance(result.value, dict) else {"value": result.value}

        # 11. Optional push. push_branch returns None when the session made no edits.
        if allow_push:
            try:
                pushed_sha = push_branch(
                    clone_dir, f"minicrew/result/{job_id}", cached_token, repo_spec["url"]
                )
                value.setdefault("git", {})
                if pushed_sha is None:
                    value["git"]["pushed"] = False
                    value["git"]["code_changed"] = False
                else:
                    value["git"]["pushed"] = True
                    value["git"]["branch"] = f"minicrew/result/{job_id}"
                    value["git"]["sha"] = pushed_sha
                    value["git"]["code_changed"] = True
            except Exception as e:
                value.setdefault("git", {})["push_error"] = str(e)
                value["git"]["pushed"] = False

        write_job_result(client, cfg, job_id, worker_id, value)
        emit(
            JOB_COMPLETED, job_id=job_id, mode="ad_hoc",
            duration_seconds=round(time.time() - started, 3),
        )
        bundle_safe_to_delete = True

    except Exception as e:
        _fail(f"ad_hoc exception: {e}")
        bundle_safe_to_delete = True
    finally:
        # Order is load-bearing.
        # 1. Stop side threads (defensive — happy path stopped before write_job_result).
        stop.set()
        for s in streamers:
            s.join(timeout=5)
        # 2. Close terminal if watchdog didn't.
        if handle is not None:
            platform.close_session(handle)
        # 3. Cleanup ~/.claude/projects/<encoded> for the clone cwd — BEFORE rmtree.
        try:
            cleanup_session_data(tmpdir / "repo")
        except OSError:
            pass
        # 4. Wipe everything.
        shutil.rmtree(tmpdir, ignore_errors=True)
        # 5. Bundle delete ONLY on terminal outcome.
        if bundle_id and bundle_safe_to_delete:
            try:
                delete_bundle(client, cfg, bundle_id)
            except Exception:
                pass
        # 6. Storage delete (if configured for delete_logs_on_completion and terminal).
        if (
            bundle_safe_to_delete
            and cfg.dispatch is not None
            and cfg.dispatch.log_storage is not None
            and cfg.dispatch.log_storage.delete_logs_on_completion
        ):
            try:
                base = cfg.db.url.rstrip("/")
                if base.endswith("/rest/v1"):
                    base = base[: -len("/rest/v1")]
                import httpx
                with httpx.Client(timeout=10) as h:
                    h.delete(
                        f"{base}/storage/v1/object/{cfg.dispatch.log_storage.bucket}",
                        headers={"Authorization": f"Bearer {cfg.db.service_key}"},
                        json={"prefixes": [str(job_id)]},
                    )
            except Exception:
                pass
