"""Orchestration dispatch: chooses single-terminal vs. fan-out based on job_type.mode."""
from __future__ import annotations

from typing import TYPE_CHECKING

from worker.config.payload_schema import PayloadValidationError, validate_payload
from worker.observability.events import JOB_FAILED, emit

if TYPE_CHECKING:
    from worker.config.models import Config
    from worker.platform.base import Platform


def run(client, cfg: Config, job: dict, *, worker_id: str, platform: Platform) -> None:
    job_type_name = job.get("job_type", "")
    if job_type_name not in cfg.job_types:
        from worker.db.queries import update_job_status

        emit(JOB_FAILED, job_id=job["id"], reason=f"unknown job_type {job_type_name}")
        update_job_status(
            client,
            cfg,
            job["id"],
            worker_id,
            status="error",
            error_message=f"unknown job_type: {job_type_name}",
            set_completed_at=True,
        )
        return

    job_type = cfg.job_types[job_type_name]

    # I13: payload validation must mark the job as error, not raise into the poll loop.
    try:
        validate_payload(job.get("payload") or {}, cfg.payload_schema)
    except PayloadValidationError as e:
        from worker.db.queries import update_job_status

        update_job_status(
            client,
            cfg,
            job["id"],
            worker_id,
            status="error",
            error_message=str(e),
            set_completed_at=True,
        )
        emit(JOB_FAILED, job_id=job["id"], reason="payload_invalid", error=str(e))
        return

    if job_type.mode == "fan_out":
        from worker.orchestration.fan_out import run_fan_out

        run_fan_out(client, cfg, job, job_type, worker_id=worker_id, platform=platform)
    elif job_type.mode == "ad_hoc":
        from worker.orchestration.ad_hoc import run_ad_hoc

        run_ad_hoc(client, cfg, job, job_type, worker_id=worker_id, platform=platform)
    elif job_type.mode == "handoff":
        from worker.orchestration.handoff import run_handoff

        run_handoff(client, cfg, job, job_type, worker_id=worker_id, platform=platform)
    else:
        from worker.orchestration.single_terminal import run_single

        run_single(client, cfg, job, job_type, worker_id=worker_id, platform=platform)
