"""Fan-out orchestration: N parallel group sessions + 1 merge session.

Structure ported from the reference implementation's multi-terminal path (lines 491-553),
with every domain reference stripped.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import worker.core.state as state
from worker.config.render import render_named_template
from worker.db.queries import requeue_job, update_job_status, write_job_result
from worker.observability.events import JOB_COMPLETED, JOB_FAILED, SESSION_LAUNCHED, emit
from worker.terminal.launcher import LaunchError, launch_terminal_window, write_prompt_file, write_runner_script
from worker.terminal.shutdown import cleanup_session_data, exit_claude_and_close_window
from worker.terminal.watchdog import (
    RESULT_COMPLETED,
    RESULT_SHUTDOWN,
    wait_for_completion,
)

if TYPE_CHECKING:
    from worker.config.models import Config, GroupSpec, JobType

# Per-group wall-clock cap; plan inherits the reference default.
GROUP_TIMEOUT_SECONDS = 2400


def _poll_for_file(path: Path, timeout: int, poll_interval: int = 15) -> bool:
    """Wait until `path` exists, has non-zero size, and has stopped growing for one tick."""
    start = time.time()
    last_size: int | None = None
    while time.time() - start < timeout:
        if state.shutdown_requested:
            return False
        if path.exists():
            try:
                size = path.stat().st_size
                if size > 0 and last_size == size:
                    with path.open(encoding="utf-8") as f:
                        json.load(f)
                    return True
                last_size = size
            except (OSError, json.JSONDecodeError):
                pass
        time.sleep(poll_interval)
    return False


def _write_group_session(group_dir: Path, prompt: str, link_from: Path) -> int:
    write_prompt_file(group_dir, prompt)
    write_runner_script(group_dir, link_from=link_from)
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


def _render_group_prompt(cfg: Config, job: dict, group: GroupSpec) -> str:
    return render_named_template(
        cfg,
        group.prompt_template,
        job=job,
        payload=job.get("payload") or {},
        config=cfg.public_view(),
        group={"name": group.name, "result_filename": group.result_filename},
    )


def _render_merge_prompt(
    cfg: Config,
    job: dict,
    job_type: JobType,
    group_paths: list[dict],
    completed: int,
    total: int,
) -> str:
    assert job_type.merge is not None
    return render_named_template(
        cfg,
        job_type.merge.prompt_template,
        job=job,
        payload=job.get("payload") or {},
        config=cfg.public_view(),
        groups=group_paths,
        completed=completed,
        total=total,
        merge={"result_filename": job_type.merge.result_filename},
    )


def _read_result(path: Path):
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def run_fan_out(client, cfg: Config, job: dict, job_type: JobType) -> None:
    assert job_type.merge is not None
    job_id = job["id"]
    tmpdir = Path(tempfile.mkdtemp(prefix="minicrew_fanout_", dir="/tmp"))
    merge_window: int | None = None
    try:
        update_job_status(client, cfg, job_id, status="running", set_started_at=True)

        total = len(job_type.groups)
        group_dirs: list[tuple[GroupSpec, Path]] = []
        for group in job_type.groups:
            group_dir = tmpdir / f"group_{group.name}"
            group_dir.mkdir(parents=True, exist_ok=True)
            prompt = _render_group_prompt(cfg, job, group)
            try:
                window_id = _write_group_session(group_dir, prompt, link_from=tmpdir)
            except LaunchError as e:
                emit(JOB_FAILED, job_id=job_id, reason="group_launch_error", group=group.name, error=str(e))
                continue
            emit(SESSION_LAUNCHED, job_id=job_id, mode="fan_out_group", group=group.name, window_id=window_id)
            group_dirs.append((group, group_dir))
            # Small stagger so Terminal.app doesn't starve under simultaneous osascript invocations.
            time.sleep(2)

        completed = 0
        for group, group_dir in group_dirs:
            result_file = group_dir / group.result_filename
            if _poll_for_file(result_file, timeout=GROUP_TIMEOUT_SECONDS):
                completed += 1

        if state.shutdown_requested:
            for wid in _read_window_ids(tmpdir):
                exit_claude_and_close_window(wid)
            requeue_job(client, cfg, job_id, reason="worker shutting down during fan_out")
            return

        # Close every group window before starting the merge — frees up screen + session slots.
        for wid in _read_window_ids(tmpdir):
            exit_claude_and_close_window(wid)

        if completed == 0:
            update_job_status(
                client,
                cfg,
                job_id,
                status="error",
                error_message="no fan_out groups produced a result",
                set_completed_at=True,
            )
            emit(JOB_FAILED, job_id=job_id, mode="fan_out", reason="no_group_results")
            return

        # Build the merge prompt with pointers to each group's result file.
        group_paths = []
        for group, group_dir in group_dirs:
            rp = (group_dir / group.result_filename).resolve()
            group_paths.append(
                {
                    "name": group.name,
                    "result_filename": group.result_filename,
                    "path": str(rp),
                    "completed": rp.exists(),
                }
            )

        merge_dir = tmpdir / "merge"
        merge_dir.mkdir(parents=True, exist_ok=True)
        merge_prompt = _render_merge_prompt(cfg, job, job_type, group_paths, completed, total)
        write_prompt_file(merge_dir, merge_prompt)
        write_runner_script(merge_dir, link_from=tmpdir)
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
            result = _read_result(merge_dir / job_type.merge.result_filename)
            write_job_result(client, cfg, job_id, result)
            emit(JOB_COMPLETED, job_id=job_id, mode="fan_out", groups_completed=completed, groups_total=total)
            return

        update_job_status(
            client,
            cfg,
            job_id,
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
        cleanup_session_data(tmpdir)
        try:
            for entry in os.listdir(tmpdir):
                sub = tmpdir / entry
                if sub.is_dir() and (entry.startswith("group_") or entry == "merge"):
                    cleanup_session_data(sub)
        except OSError:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)
