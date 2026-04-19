"""Fan-out orchestration: N parallel group sessions + 1 merge session.

Structure ported from the reference implementation's multi-terminal path (lines 491-553),
with every domain reference stripped.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import worker.core.state as state
from worker.config.render import build_env
from worker.db.queries import requeue_job, update_job_status, write_job_result
from worker.observability.events import JOB_COMPLETED, JOB_FAILED, SESSION_LAUNCHED, emit
from worker.orchestration.result_io import read_result_safe
from worker.terminal.launcher import LaunchError, launch_terminal_window, write_prompt_file, write_runner_script
from worker.terminal.shutdown import cleanup_session_data, exit_claude_and_close_window
from worker.terminal.watchdog import (
    RESULT_COMPLETED,
    RESULT_SHUTDOWN,
    wait_for_completion,
)
from worker.utils.paths import repo_root

if TYPE_CHECKING:
    from worker.config.models import Config, GroupSpec, JobType


def _job_log_path(cfg: Config, job_id: str, suffix: str) -> Path | None:
    job_output = cfg.logging.job_output or {}
    if not job_output.get("capture"):
        return None
    path = repo_root() / "logs" / "jobs" / f"{job_id}_{suffix}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _split_document_indices(payload: dict, group_count: int) -> list[list[int]]:
    """Evenly distribute len(payload.documents) across `group_count` groups.

    Early groups receive an extra index when the count doesn't divide evenly.
    """
    docs = payload.get("documents") or []
    n = len(docs)
    if group_count <= 0 or n == 0:
        return [[] for _ in range(max(group_count, 0))]
    per, extra = divmod(n, group_count)
    out: list[list[int]] = []
    idx = 0
    for i in range(group_count):
        take = per + (1 if i < extra else 0)
        out.append(list(range(idx, idx + take)))
        idx += take
    return out


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
) -> str:
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
) -> int:
    write_prompt_file(group_dir, prompt)
    write_runner_script(group_dir, job_type=job_type, log_path=log_path)
    window_id = launch_terminal_window(group_dir)
    (group_dir / "_window_id.txt").write_text(str(window_id), encoding="utf-8")
    return window_id


def _read_window_ids(tmpdir: Path) -> list[int]:
    ids: list[int] = []
    try:
        for entry in os.listdir(tmpdir):
            wid_path = tmpdir / entry / "_window_id.txt"
            if wid_path.exists():
                try:
                    ids.append(int(wid_path.read_text().strip()))
                except (ValueError, OSError):
                    continue
    except OSError:
        pass
    return ids


def run_fan_out(client, cfg: Config, job: dict, job_type: JobType, *, worker_id: str) -> None:
    assert job_type.merge is not None
    job_id = job["id"]
    started = time.time()
    tmpdir = Path(tempfile.mkdtemp(prefix="minicrew_fanout_", dir="/tmp"))
    merge_window: int | None = None
    try:
        payload = job.get("payload") or {}
        # C7: compute document index splits so each group knows which docs it owns.
        splits = _split_document_indices(payload, len(job_type.groups))

        group_dirs: list[tuple[GroupSpec, Path, list[int]]] = []
        launched_any = False
        for group, doc_indices in zip(job_type.groups, splits, strict=False):
            group_dir = tmpdir / f"group_{group.name}"
            group_dir.mkdir(parents=True, exist_ok=True)
            prompt = _render_group_prompt(cfg, job, job_type, group, doc_indices)
            log_path = _job_log_path(cfg, job_id, group.name)
            try:
                window_id = _launch_group_session(group_dir, prompt, job_type, log_path)
            except LaunchError as e:
                emit(JOB_FAILED, job_id=job_id, reason="group_launch_error", group=group.name, error=str(e))
                continue
            if not launched_any:
                # C8: write started_at only after the FIRST successful group launch.
                update_job_status(client, cfg, job_id, worker_id, status="running", set_started_at=True)
                launched_any = True
            emit(SESSION_LAUNCHED, job_id=job_id, mode="fan_out_group", group=group.name, window_id=window_id)
            group_dirs.append((group, group_dir, doc_indices))
            # Small stagger so Terminal.app doesn't starve under simultaneous osascript invocations.
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

        # I9: per-group watchdog using job_type's configured idle timeouts, not a hardcoded constant.
        # Each group gets its own wait_for_completion on its own cwd + result filename.
        # overall_timeout_seconds is the job_type's timeout_seconds; we also honor shutdown mid-wait.
        missing_groups: list[str] = []
        completed_paths: list[str] = []
        for group, group_dir, _indices in group_dirs:
            # Read window_id back from disk (it was written at launch).
            try:
                wid = int((group_dir / "_window_id.txt").read_text().strip())
            except (OSError, ValueError):
                wid = 0
            outcome = wait_for_completion(
                cwd=group_dir,
                window_id=wid,
                result_filename=group.result_filename,
                overall_timeout_seconds=job_type.timeout_seconds,
                idle_timeout_seconds=job_type.idle_timeout_seconds,
                result_idle_timeout_seconds=job_type.result_idle_timeout_seconds,
            )
            if state.shutdown_requested:
                # Shutdown wins; clean up and requeue.
                for wid2 in _read_window_ids(tmpdir):
                    exit_claude_and_close_window(wid2)
                requeue_job(client, cfg, job_id, reason="worker shutting down during fan_out")
                return
            if outcome == RESULT_COMPLETED:
                result_path = (group_dir / group.result_filename).resolve()
                completed_paths.append(str(result_path))
            else:
                missing_groups.append(group.name)
                emit(JOB_FAILED, job_id=job_id, mode="fan_out_group", group=group.name, reason=outcome)

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
        for wid in _read_window_ids(tmpdir):
            exit_claude_and_close_window(wid)

        merge_dir = tmpdir / "merge"
        merge_dir.mkdir(parents=True, exist_ok=True)
        merge_prompt = _render_merge_prompt(cfg, job, job_type, completed_paths, missing_groups)
        write_prompt_file(merge_dir, merge_prompt)
        merge_log = _job_log_path(cfg, job_id, "merge")
        write_runner_script(merge_dir, job_type=job_type, log_path=merge_log)
        merge_window = launch_terminal_window(merge_dir)
        emit(SESSION_LAUNCHED, job_id=job_id, mode="fan_out_merge", window_id=merge_window)

        outcome = wait_for_completion(
            cwd=merge_dir,
            window_id=merge_window,
            result_filename=job_type.merge.result_filename,
            overall_timeout_seconds=job_type.timeout_seconds,
            idle_timeout_seconds=job_type.idle_timeout_seconds,
            result_idle_timeout_seconds=job_type.result_idle_timeout_seconds,
        )
        merge_window = None

        if outcome == RESULT_SHUTDOWN:
            requeue_job(client, cfg, job_id, reason="worker shutting down during merge")
            return

        if outcome == RESULT_COMPLETED:
            result = read_result_safe(merge_dir, job_type.merge.result_filename)
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
            write_job_result(client, cfg, job_id, worker_id, result)
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
        if merge_window is not None:
            exit_claude_and_close_window(merge_window)
        for wid in _read_window_ids(tmpdir):
            exit_claude_and_close_window(wid)
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


