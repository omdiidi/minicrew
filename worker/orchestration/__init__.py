"""Orchestration dispatch: chooses single-terminal vs. fan-out based on job_type.mode."""
from __future__ import annotations

from typing import TYPE_CHECKING

from worker.config.payload_schema import validate_payload
from worker.observability.events import JOB_FAILED, emit

if TYPE_CHECKING:
    from worker.config.models import Config


def run(client, cfg: Config, job: dict) -> None:
    job_type_name = job.get("job_type", "")
    if job_type_name not in cfg.job_types:
        emit(JOB_FAILED, job_id=job["id"], reason=f"unknown job_type {job_type_name}")
        from worker.db.queries import update_job_status

        update_job_status(
            client,
            cfg,
            job["id"],
            status="error",
            error_message=f"unknown job_type: {job_type_name}",
            set_completed_at=True,
        )
        return

    job_type = cfg.job_types[job_type_name]

    # Validate payload at the boundary — trust nothing coming in from the enqueuer.
    validate_payload(job.get("payload") or {}, cfg.payload_schema)

    if job_type.mode == "fan_out":
        from worker.orchestration.fan_out import run_fan_out

        run_fan_out(client, cfg, job, job_type)
    else:
        from worker.orchestration.single_terminal import run_single

        run_single(client, cfg, job, job_type)
