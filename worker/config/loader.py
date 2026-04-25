"""Loads and validates worker-config/config.yaml against schema/config.schema.json."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from worker.config.models import (
    Config,
    DbConfig,
    DispatchConfig,
    GitHubAppConfig,
    GroupSpec,
    HandoffConfig,
    JobType,
    LinuxPlatformConfig,
    LogStorageConfig,
    LoggingConfig,
    McpBundleConfig,
    MergeSpec,
    PartitionSpec,
    PlatformConfig,
    ReaperConfig,
    WorkerConfig,
)


class ConfigError(ValueError):
    """Raised when config loading or validation fails."""


_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")
_ALWAYS_REDACT = {"SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_DB_URL"}


def _interpolate_env(value: Any, secrets: set[str], redact_env_names: set[str]) -> Any:
    if isinstance(value, str):

        def sub(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ConfigError(f"Environment variable {name} referenced in config is not set.")
            if name in redact_env_names or name in _ALWAYS_REDACT:
                secrets.add(os.environ[name])
            return os.environ[name]

        return _ENV_PATTERN.sub(sub, value)
    if isinstance(value, list):
        return [_interpolate_env(v, secrets, redact_env_names) for v in value]
    if isinstance(value, dict):
        return {k: _interpolate_env(v, secrets, redact_env_names) for k, v in value.items()}
    return value


def _load_schema() -> dict:
    # Schema lives at the repo root under schema/config.schema.json.
    schema_path = Path(__file__).resolve().parent.parent.parent / "schema" / "config.schema.json"
    return json.loads(schema_path.read_text(encoding="utf-8"))


def _build_job_type(name: str, raw: dict) -> JobType:
    groups: list[GroupSpec] = []
    for g in raw.get("groups") or []:
        groups.append(
            GroupSpec(
                name=g["name"],
                prompt_template=g["prompt_template"],
                result_filename=g["result_filename"],
                result_schema=g.get("result_schema"),
            )
        )
    merge = None
    if raw.get("merge"):
        merge = MergeSpec(
            prompt_template=raw["merge"]["prompt_template"],
            result_filename=raw["merge"]["result_filename"],
            result_schema=raw["merge"].get("result_schema"),
        )
    partition = None
    if raw.get("partition"):
        partition = PartitionSpec(
            key=raw["partition"]["key"],
            strategy=raw["partition"]["strategy"],
        )
    return JobType(
        name=name,
        mode=raw["mode"],
        model=raw["model"],
        thinking_budget=raw["thinking_budget"],
        timeout_seconds=int(raw["timeout_seconds"]),
        # prompt_template is optional — modes 'ad_hoc' / 'handoff' use built-in templates.
        prompt_template=raw.get("prompt_template"),
        result_filename=raw["result_filename"],
        description=raw.get("description", ""),
        skill=raw.get("skill"),
        idle_timeout_seconds=int(raw.get("idle_timeout_seconds", 1500)),
        result_idle_timeout_seconds=int(raw.get("result_idle_timeout_seconds", 900)),
        groups=groups,
        merge=merge,
        partition=partition,
        result_schema=raw.get("result_schema"),
    )


def _build_dispatch(raw: dict) -> DispatchConfig:
    """Parse the top-level `dispatch` block. Required sub-keys are validated by the JSON schema;
    this function only fills the dataclasses with the parsed/defaulted values.
    """
    gh_raw = raw["github_app"]
    github_app = GitHubAppConfig(
        app_id=gh_raw["app_id"],
        private_key_env=gh_raw["private_key_env"],
        installation_id_env=gh_raw["installation_id_env"],
        clone_timeout_seconds=int(gh_raw.get("clone_timeout_seconds", 300)),
    )
    ls_raw = raw["log_storage"]
    log_storage = LogStorageConfig(
        bucket=ls_raw.get("bucket", "minicrew-logs"),
        chunk_bytes=int(ls_raw.get("chunk_bytes", 262144)),
        chunk_interval_seconds=int(ls_raw.get("chunk_interval_seconds", 5)),
        delete_logs_on_completion=bool(ls_raw.get("delete_logs_on_completion", False)),
        retention_days=int(ls_raw.get("retention_days", 7)),
    )
    mcp_raw = raw.get("mcp_bundle") or {}
    mcp_bundle = McpBundleConfig(
        decrypted_view=mcp_raw.get("decrypted_view", "vault.decrypted_secrets"),
        register_rpc=mcp_raw.get("register_rpc", "dispatch_register_mcp_bundle"),
        delete_rpc=mcp_raw.get("delete_rpc", "dispatch_delete_mcp_bundle"),
        delete_mcp_on_completion=bool(mcp_raw.get("delete_mcp_on_completion", True)),
    )
    handoff: HandoffConfig | None = None
    if "handoff" in raw:
        h_raw = raw.get("handoff") or {}
        handoff = HandoffConfig(
            outbound_retention_days=int(h_raw.get("outbound_retention_days", 7)),
            max_transcript_bundle_bytes=int(h_raw.get("max_transcript_bundle_bytes", 10 * 1024 * 1024)),
            vault_inline_cap_bytes=int(h_raw.get("vault_inline_cap_bytes", 512 * 1024)),
            max_timeout_seconds=int(h_raw.get("max_timeout_seconds", 86400)),
            delete_inbound_on_completion=bool(h_raw.get("delete_inbound_on_completion", True)),
        )
    return DispatchConfig(
        github_app=github_app,
        log_storage=log_storage,
        mcp_bundle=mcp_bundle,
        max_concurrent_per_caller=int(raw.get("max_concurrent_per_caller", 10)),
        handoff=handoff,
    )


def load_config(path: str | Path | None = None) -> Config:
    path = path or os.environ.get("MINICREW_CONFIG_PATH")
    if not path:
        raise ConfigError("MINICREW_CONFIG_PATH not set. Run SETUP.md first.")
    root = Path(path).resolve()
    config_file = root / "config.yaml"
    if not config_file.exists():
        raise ConfigError(
            f"No config.yaml at {config_file}. Run /minicrew:scaffold-project in your consumer repo."
        )

    raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"config.yaml root must be a mapping, got {type(raw).__name__}")

    redact_env_names: set[str] = set((raw.get("logging") or {}).get("redact_env") or [])
    secrets: set[str] = set()
    raw = _interpolate_env(raw, secrets, redact_env_names)

    schema = _load_schema()
    try:
        jsonschema.validate(raw, schema=schema)
    except jsonschema.ValidationError as e:
        where = "/".join(str(p) for p in e.absolute_path) or "<root>"
        raise ConfigError(f"Config validation failed at {where}: {e.message}") from e

    if raw.get("schema_version") != 1:
        raise ConfigError(
            f"Unsupported schema_version {raw.get('schema_version')}. This worker supports 1."
        )

    db_raw = raw["db"]
    db = DbConfig(
        jobs_table=db_raw["jobs_table"],
        workers_table=db_raw["workers_table"],
        events_table=db_raw["events_table"],
        url=db_raw["url"],
        service_key=db_raw["service_key"],
        direct_url=db_raw["direct_url"],
    )
    worker_raw = raw["worker"]
    worker = WorkerConfig(
        prefix=worker_raw["prefix"],
        role=worker_raw["role"],
        poll_interval_seconds=worker_raw.get("poll_interval_seconds"),
    )
    reaper_raw = raw["reaper"]
    reaper = ReaperConfig(
        stale_threshold_seconds=int(reaper_raw["stale_threshold_seconds"]),
        interval_seconds=int(reaper_raw["interval_seconds"]),
        max_attempts=int(reaper_raw["max_attempts"]),
    )
    job_types = {name: _build_job_type(name, jt) for name, jt in raw["job_types"].items()}
    logging_raw = raw["logging"]
    logging_cfg = LoggingConfig(
        level=logging_raw["level"],
        format=logging_raw["format"],
        redact_env=list(logging_raw.get("redact_env") or []),
        sinks=list(logging_raw["sinks"]),
        job_output=dict(logging_raw.get("job_output") or {}),
    )

    cfg = Config(
        schema_version=1,
        db=db,
        worker=worker,
        reaper=reaper,
        job_types=job_types,
        logging=logging_cfg,
        prompts_dir=root / "prompts",
        _secrets=secrets,
    )

    # ----------------------------------------------------------------------
    # Dispatch block (Phase 2a/3). Optional in pure-batch installs; HARD-required
    # when any job_type uses mode 'ad_hoc' or 'handoff'. When mode == 'handoff'
    # we additionally require dispatch.handoff to be present (no silent default).
    # See docs/SUPABASE-SCHEMA.md for migration steps.
    # ----------------------------------------------------------------------
    dispatch_raw = raw.get("dispatch")
    needs_dispatch_modes = {jt.mode for jt in cfg.job_types.values()} & {"ad_hoc", "handoff"}
    has_handoff_mode = any(jt.mode == "handoff" for jt in cfg.job_types.values())
    if needs_dispatch_modes and not dispatch_raw:
        raise ConfigError(
            "dispatch block required when any job_type has mode in "
            f"{sorted(needs_dispatch_modes)}; see docs/SUPABASE-SCHEMA.md"
        )
    if dispatch_raw:
        cfg.dispatch = _build_dispatch(dispatch_raw)
        if has_handoff_mode and cfg.dispatch.handoff is None:
            raise ConfigError(
                "dispatch.handoff block required when any job_type has mode: handoff. "
                "Add 'dispatch.handoff: {}' to accept defaults; see docs/SUPABASE-SCHEMA.md"
            )

    # Emit a structured warning for fan_out job_types that omit `partition` — they fall
    # back to the documents-keyed chunks shim. Best-effort: never fail the loader on this.
    for _jt_name, _jt in cfg.job_types.items():
        if _jt.mode == "fan_out" and _jt.partition is None:
            try:
                # local import to avoid cycles
                from worker.observability.events import (
                    FAN_OUT_PARTITION_DEPRECATED,
                    emit,
                )

                emit(
                    FAN_OUT_PARTITION_DEPRECATED,
                    job_type=_jt_name,
                    note="omitting 'partition' falls back to the documents-keyed chunks shim",
                )
            except Exception:  # noqa: BLE001 — observability must never break loader
                pass

    platform_raw = raw.get("platform") or {}
    platform_kind = platform_raw.get("kind", "auto")
    linux_raw = platform_raw.get("linux") or {}
    linux_cfg: LinuxPlatformConfig | None = None
    if platform_kind == "linux" or (platform_kind == "auto" and sys.platform == "linux"):
        linux_cfg = LinuxPlatformConfig(
            display_mode=linux_raw.get("display_mode", "visible"),
            terminal_emulator=linux_raw.get("terminal_emulator", "xfce4-terminal"),
            window_open_timeout_seconds=int(linux_raw.get("window_open_timeout_seconds", 15)),
            exit_grace_seconds=int(linux_raw.get("exit_grace_seconds", 30)),
            sigterm_to_sigkill_seconds=int(linux_raw.get("sigterm_to_sigkill_seconds", 9)),
        )
    cfg.platform = PlatformConfig(kind=platform_kind, linux=linux_cfg)

    # Validate referenced prompt templates exist on disk — a missing file is a hard fail at load time.
    # Also enforce path traversal guard: resolved template path must sit under prompts_dir.
    prompts_root_resolved = cfg.prompts_dir.resolve()

    def _check_template(label: str, template_name: str) -> Path:
        tmpl = (cfg.prompts_dir / template_name).resolve()
        try:
            tmpl.relative_to(prompts_root_resolved)
        except ValueError as exc:
            raise ConfigError(
                f"{label} template '{template_name}' resolves outside {prompts_root_resolved} "
                "(path traversal rejected)"
            ) from exc
        if not tmpl.exists():
            raise ConfigError(f"{label} references missing {tmpl}")
        return tmpl

    for name, jt in cfg.job_types.items():
        # ad_hoc and handoff use built-in templates packaged inside worker.builtin_prompts;
        # there is nothing to check on disk under prompts_dir for those modes.
        if jt.mode in ("ad_hoc", "handoff"):
            continue
        _check_template(f"job_type '{name}'", jt.prompt_template)
        if jt.mode == "fan_out":
            for group in jt.groups:
                _check_template(f"fan_out group '{group.name}'", group.prompt_template)
            if jt.merge:
                _check_template("fan_out merge", jt.merge.prompt_template)

    payload_schema_file = root / "payload.schema.json"
    if payload_schema_file.exists():
        cfg.payload_schema = json.loads(payload_schema_file.read_text(encoding="utf-8"))

    return cfg


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m worker.config.loader")
    parser.add_argument("--validate", type=str, help="Path to a worker-config directory")
    args = parser.parse_args(argv)
    target = args.validate or os.environ.get("MINICREW_CONFIG_PATH")
    if not target:
        print("error: no path given and MINICREW_CONFIG_PATH not set", file=sys.stderr)
        return 2
    try:
        load_config(target)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"ok: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
