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
    GroupSpec,
    JobType,
    LoggingConfig,
    MergeSpec,
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
            )
        )
    merge = None
    if raw.get("merge"):
        merge = MergeSpec(
            prompt_template=raw["merge"]["prompt_template"],
            result_filename=raw["merge"]["result_filename"],
        )
    return JobType(
        name=name,
        mode=raw["mode"],
        model=raw["model"],
        thinking_budget=raw["thinking_budget"],
        timeout_seconds=int(raw["timeout_seconds"]),
        prompt_template=raw["prompt_template"],
        result_filename=raw["result_filename"],
        description=raw.get("description", ""),
        skill=raw.get("skill"),
        idle_timeout_seconds=int(raw.get("idle_timeout_seconds", 1500)),
        result_idle_timeout_seconds=int(raw.get("result_idle_timeout_seconds", 900)),
        groups=groups,
        merge=merge,
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
