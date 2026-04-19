"""Dataclass models backing the loaded config. Intentionally small + frozen-friendly."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class DbConfig:
    jobs_table: str
    workers_table: str
    events_table: str
    url: str
    service_key: str
    direct_url: str


@dataclass
class WorkerConfig:
    prefix: str
    role: str
    poll_interval_seconds: int | None = None


@dataclass
class ReaperConfig:
    stale_threshold_seconds: int
    interval_seconds: int
    max_attempts: int


@dataclass
class GroupSpec:
    name: str
    prompt_template: str
    result_filename: str


@dataclass
class MergeSpec:
    prompt_template: str
    result_filename: str


@dataclass
class JobType:
    name: str
    mode: str
    model: str
    thinking_budget: str
    timeout_seconds: int
    prompt_template: str
    result_filename: str
    description: str = ""
    skill: str | None = None
    idle_timeout_seconds: int = 1500
    result_idle_timeout_seconds: int = 900
    groups: list[GroupSpec] = field(default_factory=list)
    merge: MergeSpec | None = None


@dataclass
class LoggingConfig:
    level: str
    format: str
    redact_env: list[str]
    sinks: list[dict[str, Any]]
    job_output: dict[str, Any] = field(default_factory=dict)


@dataclass
class Config:
    schema_version: int
    db: DbConfig
    worker: WorkerConfig
    reaper: ReaperConfig
    job_types: dict[str, JobType]
    logging: LoggingConfig
    prompts_dir: Path = field(default_factory=lambda: Path("."))
    payload_schema: dict | None = None
    # Values that came from redact_env-listed env vars — never emitted to Jinja.
    _secrets: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the full config (including every DB field) into a plain dict.

        Used as the starting point for `public_view`, which then redacts any leaf value
        present in `_secrets`.
        """
        return {
            "schema_version": self.schema_version,
            "db": {
                "jobs_table": self.db.jobs_table,
                "workers_table": self.db.workers_table,
                "events_table": self.db.events_table,
                "url": self.db.url,
                "service_key": self.db.service_key,
                "direct_url": self.db.direct_url,
            },
            "worker": {
                "prefix": self.worker.prefix,
                "role": self.worker.role,
                "poll_interval_seconds": self.worker.poll_interval_seconds,
            },
            "reaper": {
                "stale_threshold_seconds": self.reaper.stale_threshold_seconds,
                "interval_seconds": self.reaper.interval_seconds,
                "max_attempts": self.reaper.max_attempts,
            },
            "job_types": {k: {"mode": v.mode, "model": v.model} for k, v in self.job_types.items()},
            "logging": {
                "level": self.logging.level,
                "format": self.logging.format,
                "redact_env": list(self.logging.redact_env),
            },
        }

    def public_view(self) -> dict[str, Any]:
        """Dict safe to expose inside Jinja templates: every leaf value matching a known
        secret (from `_secrets`, populated by the loader) is replaced with `***`.
        """
        secrets = self._secrets

        def scrub(value: Any) -> Any:
            if isinstance(value, dict):
                return {k: scrub(v) for k, v in value.items()}
            if isinstance(value, list):
                return [scrub(v) for v in value]
            if isinstance(value, str) and value in secrets:
                return "***"
            return value

        return scrub(self.to_dict())
