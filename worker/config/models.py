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
    result_schema: dict | None = None


@dataclass
class MergeSpec:
    prompt_template: str
    result_filename: str
    result_schema: dict | None = None


@dataclass
class PartitionSpec:
    """fan_out partition spec: how to split a payload list across groups.

    `key` is a dotted path into payload (e.g. 'documents'); `strategy` is one of
    'chunks' (round-robin remainder split) or 'copies' (every group sees all items).
    """

    key: str
    strategy: str


@dataclass
class JobType:
    name: str
    mode: str
    model: str
    thinking_budget: str
    timeout_seconds: int
    # Optional: required when mode in ('single', 'fan_out'); None for 'ad_hoc' / 'handoff'
    # which use built-in templates rendered via worker.config.render.render_builtin_*.
    prompt_template: str | None
    result_filename: str
    description: str = ""
    skill: str | None = None
    idle_timeout_seconds: int = 1500
    result_idle_timeout_seconds: int = 900
    groups: list[GroupSpec] = field(default_factory=list)
    merge: MergeSpec | None = None
    partition: PartitionSpec | None = None
    result_schema: dict | None = None


@dataclass
class LoggingConfig:
    level: str
    format: str
    redact_env: list[str]
    sinks: list[dict[str, Any]]
    job_output: dict[str, Any] = field(default_factory=dict)


@dataclass
class LinuxPlatformConfig:
    display_mode: str = "visible"
    terminal_emulator: str = "xfce4-terminal"
    window_open_timeout_seconds: int = 15
    exit_grace_seconds: int = 30
    sigterm_to_sigkill_seconds: int = 9


@dataclass
class PlatformConfig:
    kind: str = "auto"
    linux: LinuxPlatformConfig | None = None


@dataclass
class GitHubAppConfig:
    """GitHub App credentials used to clone (and optionally push to) caller-supplied repos."""

    app_id: str
    private_key_env: str
    installation_id_env: str
    clone_timeout_seconds: int = 300


@dataclass
class LogStorageConfig:
    """Supabase Storage bucket settings for chunk-rotated transcripts and outbound bundle fallbacks."""

    bucket: str = "minicrew-logs"
    chunk_bytes: int = 262144
    chunk_interval_seconds: int = 5
    delete_logs_on_completion: bool = False
    retention_days: int = 7


@dataclass
class McpBundleConfig:
    """Vault-backed MCP bundle settings for ad_hoc dispatch."""

    decrypted_view: str = "vault.decrypted_secrets"
    register_rpc: str = "dispatch_register_mcp_bundle"
    delete_rpc: str = "dispatch_delete_mcp_bundle"
    delete_mcp_on_completion: bool = True


@dataclass
class HandoffConfig:
    """Handoff (mode: handoff) settings.

    Phase 3 hard-errors when a handoff job_type is configured without a `dispatch.handoff`
    block in YAML — there is no silent default. The dataclass defaults below define the
    semantics applied when the operator passes an empty `dispatch.handoff: {}` block.
    """

    outbound_retention_days: int = 7
    # 10 MB; keep in sync with v_max in dispatch_register_transcript_bundle().
    max_transcript_bundle_bytes: int = 10 * 1024 * 1024
    # 512 KB; over this, outbound bundles fall back to Storage instead of inline Vault.
    vault_inline_cap_bytes: int = 512 * 1024
    max_timeout_seconds: int = 86400
    delete_inbound_on_completion: bool = True


@dataclass
class DispatchConfig:
    """Top-level dispatch block.

    Required (and validated by the loader) when any job_type has mode 'ad_hoc' or
    'handoff'. Absent in pure-batch installs, in which case the worker emits zero
    new background threads and zero new HTTP calls.
    """

    github_app: GitHubAppConfig
    log_storage: LogStorageConfig
    mcp_bundle: McpBundleConfig
    max_concurrent_per_caller: int = 10
    handoff: HandoffConfig | None = None


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
    platform: PlatformConfig | None = None
    dispatch: DispatchConfig | None = None
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
