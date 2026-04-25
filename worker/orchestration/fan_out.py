"""Fan-out orchestration: N parallel group sessions + 1 merge session.

Structure ported from the reference implementation's multi-terminal path (lines 491-553),
with every domain reference stripped.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import worker.core.state as state
from worker.config.render import build_env
from worker.db.queries import (
    requeue_job,
    set_status_cancelled,
    update_job_status,
    write_job_result,
)
from worker.observability.events import (
    JOB_CANCELLED,
    JOB_COMPLETED,
    JOB_FAILED,
    SESSION_LAUNCHED,
    emit,
)
from worker.orchestration import partition as _partition
from worker.orchestration.result_io import read_result_safe
from worker.platform.base import LaunchError, SessionHandle
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
    from worker.config.models import Config, GroupSpec, JobType
    from worker.platform.base import Platform


def _job_log_path(cfg: Config, job_id: str, suffix: str) -> Path | None:
    job_output = cfg.logging.job_output or {}
    if not job_output.get("capture"):
        return None
    path = repo_root() / "logs" / "jobs" / f"{job_id}_{suffix}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _resolve_partition(
    payload: dict, job_type: JobType
) -> tuple[list, list[list[int]]]:
    """Return (items, splits) for the configured (or back-compat) partition.

    When `job_type.partition` is None and mode is fan_out we preserve the original
    behavior: resolve `payload.documents` and split with the 'chunks' strategy. Otherwise
    we walk the dotted key per partition.key with the configured strategy.
    """
    if job_type.partition is None:
        items = payload.get("documents") or []
        if not isinstance(items, list):
            items = []
        splits = _partition.split(items, len(job_type.groups), "chunks")
        return items, splits
    items = _partition.resolve_partition_input(payload, job_type.partition.key)
    splits = _partition.split(items, len(job_type.groups), job_type.partition.strategy)
    return items, splits


def _render_with_prefix(cfg: Config, job_type: JobType, template_name: str, extra: dict[str, Any]) -> str:
    """I10: render a named template while still honoring the job_type's skill prefix."""
    env = build_env(cfg.prompts_dir)
    tmpl = env.get_template(template_name)
    rendered = tmpl.render(**extra)
    if job_type.skill:
        rendered = f"/{job_type.skill}\n\n{rendered}"
    return rendered


def _render_group_prompt(
    cfg: Config,
    job: dict,
    job_type: JobType,
    group: GroupSpec,
    document_indices: list[int],
    partition_items: list,
) -> str:
    # Back-compat: existing fan_out templates key on `group.document_indices[0]`. New
    # templates can use `group.items[0]` (the resolved item) or `group.partition_items`
    # (the indices for the configured partition key).
    items = [partition_items[i] for i in document_indices if 0 <= i < len(partition_items)]
    return _render_with_prefix(
        cfg,
        job_type,
        group.prompt_template,
        {
            "job": job,
            "payload": job.get("payload") or {},
            "config": cfg.public_view(),
            "group": {
                "name": group.name,
                "result_filename": group.result_filename,
                "document_indices": document_indices,
                "partition_items": items,
                "items": items,
            },
        },
    )


def _render_merge_prompt(
    cfg: Config,
    job: dict,
    job_type: JobType,
    group_result_paths: list[str],
    missing_groups: list[str],
) -> str:
    assert job_type.merge is not None
    return _render_with_prefix(
        cfg,
        job_type,
        job_type.merge.prompt_template,
        {
            "job": job,
            "payload": job.get("payload") or {},
            "config": cfg.public_view(),
            "group_result_paths": group_result_paths,
            "missing_groups": missing_groups,
            "merge": {"result_filename": job_type.merge.result_filename},
        },
    )


def _launch_group_session(
    group_dir: Path,
    prompt: str,
    job_type: JobType,
    log_path: Path | None,
    platform: Platform,
) -> SessionHandle:
    write_prompt_file(group_dir, prompt)
    write_runner_script(group_dir, job_type=job_type, log_path=log_path)
    handle = platform.launch_session(group_dir)
    (group_dir / "_session.json").write_text(handle.to_json(), encoding="utf-8")
    return handle


def _read_handles(tmpdir: Path) -> list[SessionHandle]:
    """Recover live session handles from every group_* subdir.

    Preference order:
      1. `_session.json` — canonical serialized SessionHandle (new code path).
      2. `_window_id.txt` — legacy fallback for an in-flight job that started under
         pre-Phase-4 code. Reconstructed as a mac SessionHandle.
    """
    handles: list[SessionHandle] = []
    try:
        for entry in os.listdir(tmpdir):
            subdir = tmpdir / entry
            session_path = subdir / "_session.json"
            if session_path.exists():
                try:
                    handles.append(SessionHandle.from_json(session_path.read_text()))
                    continue
                except (OSError, ValueError, KeyError):
                    pass
            legacy = subdir / "_window_id.txt"
            if legacy.exists():
                try:
                    wid_str = legacy.read_text().strip()
                    handles.append(
                        SessionHandle(kind="mac", data={"window_id": int(wid_str)})
                    )
                except (OSError, ValueError):
                    continue
    except OSError:
        pass
    return handles


def _sweep_pending_pids(tmpdir: Path) -> None:
    """Kill any mid-launch orphan terminal processes recorded in `_pending_pid.txt`.

    Linux's `_launch_visible` writes `pid\npgid\n` before it confirms the window exists;
    if the worker crashes or is shut down in that window, the terminal process is still
    alive but no SessionHandle has been produced. Walk every group subdir and SIGKILL
    the pgid so those orphans don't leak.
    """
    try:
        entries = list(os.listdir(tmpdir))
    except OSError:
        return
    for entry in entries:
        pending = tmpdir / entry / "_pending_pid.txt"
        if not pending.exists():
            continue
        try:
            lines = pending.read_text().splitlines()
            if len(lines) < 2:
                continue
            pgid = int(lines[1].strip())
        except (OSError, ValueError):
            continue
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError) as e:
            print(f"[fan_out] pending-pid sweep: pgid {pgid} already gone ({e})", file=sys.stderr)
        try:
            pending.unlink()
        except OSError:
            pass


def run_fan_out(
    client,
    cfg: Config,
    job: dict,
    job_type: JobType,
    *,
    worker_id: str,
    platform: Platform,
) -> None:
    assert job_type.merge is not None
    job_id = job["id"]
    started = time.time()
    tmpdir = Path(tempfile.mkdtemp(prefix="minicrew_fanout_", dir=str(tmp_root())))
    merge_handle: SessionHandle | None = None
    try:
        payload = job.get("payload") or {}
        # C7: compute partition splits so each group knows which items it owns.
        partition_items, splits = _resolve_partition(payload, job_type)

        group_dirs: list[tuple[GroupSpec, Path, list[int]]] = []
        group_handles: dict[str, SessionHandle] = {}
        missing_groups: list[str] = []
        launched_any = False
        for group, doc_indices in zip(job_type.groups, splits, strict=False):
            group_dir = tmpdir / f"group_{group.name}"
            group_dir.mkdir(parents=True, exist_ok=True)
            prompt = _render_group_prompt(cfg, job, job_type, group, doc_indices, partition_items)
            log_path = _job_log_path(cfg, job_id, group.name)
            try:
                handle = _launch_group_session(group_dir, prompt, job_type, log_path, platform)
            except LaunchError as e:
                # F8: launch failure must land in missing_groups so the merge template sees it.
                emit(JOB_FAILED, job_id=job_id, reason="group_launch_error", group=group.name, error=str(e))
                missing_groups.append(group.name)
                continue
            if not launched_any:
                # C8: write started_at only after the FIRST successful group launch.
                update_job_status(client, cfg, job_id, worker_id, status="running", set_started_at=True)
                launched_any = True
            emit(
                SESSION_LAUNCHED,
                job_id=job_id,
                mode="fan_out_group",
                group=group.name,
                window_id=handle.data.get("window_id"),
                handle_kind=handle.kind,
            )
            group_dirs.append((group, group_dir, doc_indices))
            group_handles[group.name] = handle
            # Small stagger so the host's window server doesn't starve under simultaneous launches.
            time.sleep(2)

        if not launched_any:
            update_job_status(
                client,
                cfg,
                job_id,
                worker_id,
                status="error",
                error_message="no fan_out group terminals could be launched",
                set_completed_at=True,
            )
            emit(JOB_FAILED, job_id=job_id, mode="fan_out", reason="no_group_launched")
            return

        # F3: run every group's watchdog in its own thread so a late group can't run
        # unmonitored while an earlier group is still being waited on sequentially.
        results: dict[str, str] = {}
        threads: list[threading.Thread] = []

        def _watch_group(
            gname: str, gdir: Path, gfilename: str, ghandle: SessionHandle
        ) -> None:
            outcome = wait_for_completion(
                cwd=gdir,
                handle=ghandle,
                platform=platform,
                result_filename=gfilename,
                overall_timeout_seconds=job_type.timeout_seconds,
                idle_timeout_seconds=job_type.idle_timeout_seconds,
                result_idle_timeout_seconds=job_type.result_idle_timeout_seconds,
                cancel_check=lambda: state.is_cancel_requested(job_id),
            )
            results[gname] = outcome

        for group, group_dir, _indices in group_dirs:
            ghandle = group_handles.get(group.name)
            if ghandle is None:
                continue
            t = threading.Thread(
                target=_watch_group,
                args=(group.name, group_dir, group.result_filename, ghandle),
                name=f"minicrew-group-{group.name}",
                daemon=True,
            )
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        if state.shutdown_requested:
            # Shutdown wins; clean up and requeue.
            for h in _read_handles(tmpdir):
                platform.close_session(h)
            _sweep_pending_pids(tmpdir)
            requeue_job(client, cfg, job_id, worker_id, reason="worker shutting down during fan_out")
            return

        # Cancellation mid-fan-out: any group that surfaced RESULT_CANCELLED — or a
        # cancel-requested flag still set after join — wins over the merge. Close every
        # group window, mark the parent cancelled, do NOT attempt the merge.
        if any(o == RESULT_CANCELLED for o in results.values()) or state.is_cancel_requested(job_id):
            for h in _read_handles(tmpdir):
                platform.close_session(h)
            _sweep_pending_pids(tmpdir)
            set_status_cancelled(client, cfg, job_id, worker_id)
            emit(JOB_CANCELLED, job_id=job_id, mode="fan_out")
            return

        # F4: read each completed group's result file through read_result_safe (O_NOFOLLOW +
        # containment check). Write the sanitized contents to a parallel _safe file inside the
        # same group dir so the merge prompt references a vetted artifact instead of the raw
        # result path. Groups that fail the safety check land in missing_groups.
        completed_paths: list[str] = []
        for group, group_dir, _indices in group_dirs:
            outcome = results.get(group.name, RESULT_COMPLETED if group.name in results else "missing")
            if outcome != RESULT_COMPLETED:
                missing_groups.append(group.name)
                emit(JOB_FAILED, job_id=job_id, mode="fan_out_group", group=group.name, reason=outcome)
                continue
            group_result = read_result_safe(group_dir, group.result_filename, schema=group.result_schema)
            if group_result is None or not group_result.ok:
                missing_groups.append(group.name)
                emit(
                    JOB_FAILED,
                    job_id=job_id,
                    mode="fan_out_group",
                    group=group.name,
                    reason="group_result_read_failed",
                    error=(group_result.error if group_result else None),
                )
                continue
            safe_path = group_dir / f"_safe_{group.result_filename}"
            try:
                safe_path.write_text(json.dumps(group_result.value), encoding="utf-8")
            except OSError as e:
                missing_groups.append(group.name)
                emit(
                    JOB_FAILED,
                    job_id=job_id,
                    mode="fan_out_group",
                    group=group.name,
                    reason="safe_write_failed",
                    error=str(e),
                )
                continue
            completed_paths.append(str(safe_path.resolve()))

        if not completed_paths:
            update_job_status(
                client,
                cfg,
                job_id,
                worker_id,
                status="error",
                error_message="no fan_out groups produced a result",
                set_completed_at=True,
            )
            emit(JOB_FAILED, job_id=job_id, mode="fan_out", reason="no_group_results")
            return

        # Close every group window before starting the merge — frees up screen + session slots.
        for h in _read_handles(tmpdir):
            platform.close_session(h)

        merge_dir = tmpdir / "merge"
        merge_dir.mkdir(parents=True, exist_ok=True)
        merge_prompt = _render_merge_prompt(cfg, job, job_type, completed_paths, missing_groups)
        write_prompt_file(merge_dir, merge_prompt)
        merge_log = _job_log_path(cfg, job_id, "merge")
        write_runner_script(merge_dir, job_type=job_type, log_path=merge_log)
        merge_handle = platform.launch_session(merge_dir)
        emit(
            SESSION_LAUNCHED,
            job_id=job_id,
            mode="fan_out_merge",
            window_id=merge_handle.data.get("window_id"),
            handle_kind=merge_handle.kind,
        )

        outcome = wait_for_completion(
            cwd=merge_dir,
            handle=merge_handle,
            platform=platform,
            result_filename=job_type.merge.result_filename,
            overall_timeout_seconds=job_type.timeout_seconds,
            idle_timeout_seconds=job_type.idle_timeout_seconds,
            result_idle_timeout_seconds=job_type.result_idle_timeout_seconds,
            cancel_check=lambda: state.is_cancel_requested(job_id),
        )
        merge_handle = None

        if outcome == RESULT_SHUTDOWN:
            requeue_job(client, cfg, job_id, worker_id, reason="worker shutting down during merge")
            return

        if outcome == RESULT_CANCELLED:
            set_status_cancelled(client, cfg, job_id, worker_id)
            emit(JOB_CANCELLED, job_id=job_id, mode="fan_out")
            return

        if outcome == RESULT_COMPLETED:
            result = read_result_safe(
                merge_dir, job_type.merge.result_filename, schema=job_type.merge.result_schema
            )
            if result is None:
                update_job_status(
                    client,
                    cfg,
                    job_id,
                    worker_id,
                    status="error",
                    error_message="merge result file unreadable (symlink or traversal rejected)",
                    set_completed_at=True,
                )
                emit(JOB_FAILED, job_id=job_id, mode="fan_out", reason="result_read_failed")
                return
            if not result.ok:
                update_job_status(
                    client,
                    cfg,
                    job_id,
                    worker_id,
                    status="error",
                    error_message=result.error or "merge result validation failed",
                    set_completed_at=True,
                )
                emit(
                    JOB_FAILED,
                    job_id=job_id,
                    mode="fan_out",
                    reason="result_invalid",
                    error=result.error,
                )
                return
            write_job_result(client, cfg, job_id, worker_id, result.value)
            emit(
                JOB_COMPLETED,
                job_id=job_id,
                mode="fan_out",
                groups_completed=len(completed_paths),
                groups_total=len(job_type.groups),
                duration_seconds=round(time.time() - started, 3),
            )
            return

        update_job_status(
            client,
            cfg,
            job_id,
            worker_id,
            status="error",
            error_message=f"merge session ended with {outcome}",
            set_completed_at=True,
        )
        emit(JOB_FAILED, job_id=job_id, mode="fan_out", reason=outcome)

    except LaunchError as e:
        update_job_status(
            client,
            cfg,
            job_id,
            worker_id,
            status="error",
            error_message=str(e),
            set_completed_at=True,
        )
        emit(JOB_FAILED, job_id=job_id, reason="launch_error", error=str(e))
    except Exception as e:
        update_job_status(
            client,
            cfg,
            job_id,
            worker_id,
            status="error",
            error_message=str(e),
            set_completed_at=True,
        )
        emit(JOB_FAILED, job_id=job_id, reason="exception", error=str(e))
    finally:
        if merge_handle is not None:
            platform.close_session(merge_handle)
        for h in _read_handles(tmpdir):
            platform.close_session(h)
        _sweep_pending_pids(tmpdir)
        # M3: cleanup_session_data on parent tmpdir is a no-op because the parent is never
        # registered as a Claude project; only the group_* and merge subdirs are. Cleanup
        # each real session cwd, then remove the tmpdir tree.
        try:
            for entry in os.listdir(tmpdir):
                sub = tmpdir / entry
                if sub.is_dir() and (entry.startswith("group_") or entry == "merge"):
                    cleanup_session_data(sub)
        except OSError:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)
