"""handoff orchestration: resume an existing Claude Code session on the worker, then ship
the extended transcript back to Vault (with Storage fallback) for caller-side reattach.

Key invariants:
- `bundle_safe_to_delete` gates final cleanup; True only on terminal outcomes.
- Cancel checkpoints AFTER every IO step.
- `_try_bundle_outbound` is best-effort and never raises; called from success +
  exception + cancel + timeout paths BEFORE `cleanup_session_data` wipes the
  project dir.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import worker.core.state as state
from worker.config.render import render_builtin_handoff
from worker.db.queries import (
    requeue_job,
    set_status_cancelled,
    update_job_status,
    write_final_transcript_bundle_id,
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
    SUBAGENT_FILENAME_RE,
    SecretBundleError,
    delete_bundle,
    delete_transcript_bundle,
    fetch_bundle,
    fetch_transcript_bundle,
    register_transcript_bundle,
)
from worker.observability.events import (
    JOB_CANCELLED,
    JOB_COMPLETED,
    JOB_FAILED,
    POLL_LOOP_ERROR,
    SESSION_LAUNCHED,
    emit,
)
from worker.orchestration.result_io import read_result_safe
from worker.platform.base import LaunchError
from worker.terminal.launcher import write_prompt_file
from worker.terminal.launcher_resume import write_runner_script_resume
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


def _encoded_path(cwd: Path) -> str:
    return os.path.realpath(str(cwd)).replace("/", "-")


def _try_bundle_outbound(
    client,
    cfg: Config,
    job_id: str,
    worker_id: str,
    proj_dir: Path,
    session_id: str,
    result_value: dict,
) -> str | None:
    """Best-effort outbound bundling. Reads extended transcripts from proj_dir, registers
    in Vault (with Storage fallback for large), patches final_transcript_bundle_id on row.
    Returns the bundle UUID on success, None on any failure. Never raises.
    """
    try:
        top_level_path = proj_dir / f"{session_id}.jsonl"
        if not top_level_path.exists():
            return None
        extended_top = top_level_path.read_text(encoding="utf-8")
        extended_subs: dict[str, str] = {}
        sub_dir = proj_dir / session_id / "subagents"
        if sub_dir.is_dir():
            for f in sub_dir.iterdir():
                if f.is_file() and SUBAGENT_FILENAME_RE.match(f.name):
                    extended_subs[f.name] = f.read_text(encoding="utf-8")
        outbound = {
            "session_id": session_id,
            "top_level": extended_top,
            "subagents": extended_subs,
        }
    except Exception as e:
        emit(
            POLL_LOOP_ERROR,
            error=f"outbound transcript bundling failed for {job_id}: {e}",
        )
        return None
    try:
        bundle_id = register_transcript_bundle(client, cfg, outbound)
    except Exception as e:
        emit(
            POLL_LOOP_ERROR,
            error=f"outbound transcript register failed for {job_id}: {e}",
        )
        return None
    try:
        if not write_final_transcript_bundle_id(client, cfg, job_id, worker_id, bundle_id):
            # Lost ownership (PATCH returned no rows). Compensate by deleting
            # the just-registered Vault row so it doesn't leak.
            try:
                client.rpc("dispatch_delete_transcript_bundle", {"p_id": str(bundle_id)})
            except Exception:
                pass
            return None
        return bundle_id
    except Exception as e:
        try:
            client.rpc("dispatch_delete_transcript_bundle", {"p_id": str(bundle_id)})
        except Exception:
            pass
        emit(POLL_LOOP_ERROR, error=f"outbound bundle PATCH failed; rolled back: {e}")
        return None


def run_handoff(
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
    tmpdir = Path(tempfile.mkdtemp(prefix="minicrew_handoff_", dir=str(tmp_root())))
    handle: SessionHandle | None = None
    mcp_bundle_id = job.get("mcp_bundle_id")
    transcript_bundle_id = (payload.get("transcript_bundle_id") or "").strip() or None
    bundle_safe_to_delete = False
    stop = threading.Event()
    streamers: list[threading.Thread] = []
    cached_token: str | None = None
    proj_dir: Path | None = None
    session_id: str = ""

    def _fail(msg: str) -> None:
        update_job_status(
            client, cfg, job_id, worker_id,
            status="error", error_message=msg, set_completed_at=True,
        )
        emit(JOB_FAILED, job_id=job_id, mode="handoff", reason=msg)

    def _cancel_check_or_exit(stage: str) -> bool:
        """Returns True if cancelled — caller should set bundle_safe_to_delete=True and return."""
        if state.is_cancel_requested(job_id):
            set_status_cancelled(client, cfg, job_id, worker_id)
            emit(JOB_CANCELLED, job_id=job_id, mode="handoff", reason=f"cancelled at {stage}")
            return True
        return False

    try:
        # 1. Validate payload shape.
        repo_spec = payload.get("repo") or {}
        session_id_raw = (payload.get("session_id") or "").strip()
        user_instruction = payload.get("prompt")
        allow_push = bool(payload.get("allow_code_push", False))
        if not session_id_raw or not transcript_bundle_id:
            _fail("payload.session_id and payload.transcript_bundle_id required")
            bundle_safe_to_delete = True
            return
        try:
            UUID(session_id_raw)
            UUID(transcript_bundle_id)
        except ValueError:
            _fail("payload.session_id / transcript_bundle_id must be UUIDs")
            bundle_safe_to_delete = True
            return
        session_id = session_id_raw
        if not all(k in repo_spec for k in ("url", "sha")):
            _fail("payload.repo must have url and sha")
            bundle_safe_to_delete = True
            return

        # Cap caller-supplied timeout overrides.
        handoff_cfg = cfg.dispatch.handoff if cfg.dispatch is not None else None
        max_timeout = handoff_cfg.max_timeout_seconds if handoff_cfg else 86400
        timeout_override = payload.get("timeout_override_seconds")
        idle_override = payload.get("idle_timeout_override_seconds")
        overall_timeout = min(
            int(timeout_override or job_type.timeout_seconds or 14400), max_timeout
        )
        idle_timeout = min(
            int(idle_override or job_type.idle_timeout_seconds or 1800), max_timeout
        )

        if _cancel_check_or_exit("payload-validate"):
            bundle_safe_to_delete = True
            return

        # 2. Mint token + clone (cancel-aware via authenticated_clone's cancel_check).
        clone_dir = tmpdir / "repo"
        try:
            cached_token = mint_install_token(cfg)
            authenticated_clone(
                repo_spec["url"], repo_spec["sha"], clone_dir, cached_token,
                timeout=cfg.dispatch.github_app.clone_timeout_seconds or 300,
                cancel_check=lambda: state.is_cancel_requested(job_id),
            )
        except Exception as e:
            if "cancelled before" in str(e):
                set_status_cancelled(client, cfg, job_id, worker_id)
                emit(JOB_CANCELLED, job_id=job_id, mode="handoff", reason=str(e))
                bundle_safe_to_delete = True
                return
            _fail(f"clone failed: {e}")
            bundle_safe_to_delete = True
            return

        if _cancel_check_or_exit("post-clone"):
            bundle_safe_to_delete = True
            return

        # 3. Branch / origin handling.
        if not allow_push:
            remove_origin(clone_dir)
        else:
            try:
                precreate_branch(clone_dir, f"minicrew/result/{job_id}")
            except Exception as e:
                _fail(f"branch precreate failed: {e}")
                bundle_safe_to_delete = True
                return

        if _cancel_check_or_exit("post-branch"):
            bundle_safe_to_delete = True
            return

        # 4. Per-job .claude/settings.json (MCP).
        if mcp_bundle_id:
            try:
                bundle = fetch_bundle(client, cfg, mcp_bundle_id)
            except SecretBundleError as e:
                _fail(f"mcp bundle fetch failed: {e}")
                bundle_safe_to_delete = True
                return
            claude_local_dir = clone_dir / ".claude"
            claude_local_dir.mkdir(parents=True, exist_ok=True)
            settings_path = claude_local_dir / "settings.json"
            settings_path.write_text(
                json.dumps({"mcpServers": bundle["mcpServers"]}, indent=2),
                encoding="utf-8",
            )
            settings_path.chmod(0o600)

        if _cancel_check_or_exit("post-mcp"):
            bundle_safe_to_delete = True
            return

        # 5. Fetch + write inbound transcript.
        try:
            tbundle = fetch_transcript_bundle(client, cfg, transcript_bundle_id)
        except SecretBundleError as e:
            _fail(f"transcript fetch failed: {e}")
            bundle_safe_to_delete = True
            return
        if tbundle.get("session_id") != session_id:
            _fail(
                f"transcript bundle session_id mismatch: "
                f"bundle={tbundle.get('session_id')!r} payload={session_id!r}"
            )
            bundle_safe_to_delete = True
            return

        encoded = _encoded_path(clone_dir)
        proj_dir = Path.home() / ".claude" / "projects" / encoded
        proj_dir.mkdir(parents=True, exist_ok=True)
        try:
            (proj_dir / f"{session_id}.jsonl").write_text(
                tbundle["top_level"], encoding="utf-8"
            )
            subs = tbundle.get("subagents") or {}
            if subs:
                sub_dir = proj_dir / session_id / "subagents"
                sub_dir.mkdir(parents=True, exist_ok=True)
                for fname, content in subs.items():
                    # Defense-in-depth: filename already validated upstream in
                    # _validate_transcript_bundle_shape. Re-check here just before write.
                    if not SUBAGENT_FILENAME_RE.match(fname):
                        raise ValueError(f"unsafe subagent filename: {fname!r}")
                    (sub_dir / fname).write_text(content, encoding="utf-8")
        except Exception as e:
            _fail(f"transcript write failed: {e}")
            bundle_safe_to_delete = True
            return

        if _cancel_check_or_exit("post-transcript-write"):
            bundle_safe_to_delete = True
            return

        # 6. Render continuation prompt.
        prompt = render_builtin_handoff(
            cfg=cfg, job=job, payload=payload,
            user_instruction=user_instruction,
            allow_code_push=allow_push,
            result_filename=job_type.result_filename,
            job_id=job_id,
        )
        write_prompt_file(clone_dir, prompt)

        # 7. Runner with --resume and --print.
        log_path = repo_root() / "logs" / "jobs" / f"{job_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        write_runner_script_resume(
            clone_dir, job_type=job_type, log_path=log_path, resume_session_id=session_id,
        )

        if _cancel_check_or_exit("pre-launch"):
            bundle_safe_to_delete = True
            return

        # 8. Launch.
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
            mode="handoff",
            window_id=handle.data.get("window_id"),
        )

        # 9. Side threads.
        if cfg.dispatch is not None and cfg.dispatch.log_storage is not None:
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
        pt = ProgressTailer(
            client=client, cfg=cfg, job_id=job_id,
            worker_id=worker_id, cwd=clone_dir, stop_event=stop,
        )
        pt.start()
        streamers.append(pt)

        # 10. Watch with handoff-specific timeouts.
        outcome = wait_for_completion(
            cwd=clone_dir, handle=handle, platform=platform,
            result_filename=job_type.result_filename,
            overall_timeout_seconds=overall_timeout,
            idle_timeout_seconds=idle_timeout,
            result_idle_timeout_seconds=job_type.result_idle_timeout_seconds or 1800,
            cancel_check=lambda: state.is_cancel_requested(job_id),
        )
        handle = None

        # Stop streamers BEFORE writing terminal status.
        stop.set()
        for s in streamers:
            s.join(timeout=5)
        streamers.clear()

        if outcome == RESULT_SHUTDOWN:
            requeue_job(client, cfg, job_id, worker_id, reason="shutdown during handoff")
            # bundle_safe_to_delete stays False — bundles preserved for retry.
            return
        if outcome == RESULT_CANCELLED:
            # Best-effort outbound bundle even on cancel — partial work may still be useful.
            if proj_dir is not None:
                _try_bundle_outbound(client, cfg, job_id, worker_id, proj_dir, session_id, {})
            set_status_cancelled(client, cfg, job_id, worker_id)
            emit(JOB_CANCELLED, job_id=job_id, mode="handoff")
            bundle_safe_to_delete = True
            return
        if outcome != RESULT_COMPLETED:
            # Best-effort outbound bundle on timeout/error.
            if proj_dir is not None:
                _try_bundle_outbound(client, cfg, job_id, worker_id, proj_dir, session_id, {})
            _fail(f"session ended with {outcome}")
            bundle_safe_to_delete = True
            return

        # 11. Read result.
        result = read_result_safe(
            clone_dir, job_type.result_filename, schema=job_type.result_schema
        )
        if result is None or not result.ok:
            if proj_dir is not None:
                _try_bundle_outbound(client, cfg, job_id, worker_id, proj_dir, session_id, {})
            _fail(result.error if result and result.error else "result file unreadable")
            bundle_safe_to_delete = True
            return
        value = result.value if isinstance(result.value, dict) else {"value": result.value}

        # 12. Outbound bundle (success path).
        if proj_dir is not None:
            _try_bundle_outbound(client, cfg, job_id, worker_id, proj_dir, session_id, value)
            # Outbound id NOT duplicated into result.value — it's already on the row column.

        # 13. Optional push.
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
            JOB_COMPLETED, job_id=job_id, mode="handoff",
            duration_seconds=round(time.time() - started, 3),
        )
        bundle_safe_to_delete = True

    except Exception as e:
        # Best-effort outbound on exception path.
        if proj_dir is not None and session_id:
            _try_bundle_outbound(client, cfg, job_id, worker_id, proj_dir, session_id, {})
        _fail(f"handoff exception: {e}")
        bundle_safe_to_delete = True
    finally:
        # Order is load-bearing.
        stop.set()
        for s in streamers:
            s.join(timeout=5)
        if handle is not None:
            platform.close_session(handle)
        # cleanup_session_data wipes ~/.claude/projects/<encoded> — runs AFTER outbound
        # bundling already completed in success/exception paths above.
        if proj_dir is not None:
            try:
                cleanup_session_data(tmpdir / "repo")
            except OSError:
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)
        if bundle_safe_to_delete:
            if mcp_bundle_id:
                try:
                    delete_bundle(client, cfg, mcp_bundle_id)
                except Exception:
                    pass
            if transcript_bundle_id:
                try:
                    delete_transcript_bundle(client, cfg, transcript_bundle_id)
                except Exception:
                    pass
        # Storage delete (live transcript chunks, NOT the outbound transcript bundle which
        # lives in transcripts/<session>-*.json.gz under retention).
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
