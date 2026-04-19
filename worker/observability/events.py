"""Event name constants, JSON formatter, env-var redaction filter."""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from typing import Any

# Event name constants — v1 set from plan section 2 "Events emitted".
WORKER_STARTED = "worker_started"
WORKER_STOPPED = "worker_stopped"
HEARTBEAT_ERROR = "heartbeat_error"
JOB_CLAIMED = "job_claimed"
JOB_COMPLETED = "job_completed"
JOB_FAILED = "job_failed"
REAPER_RAN = "reaper_ran"
REAPER_REQUEUED = "reaper_requeued"
REAPER_ERROR = "reaper_error"
WATCHDOG_KILLED = "watchdog_killed"
SESSION_LAUNCHED = "session_launched"
POLL_LOOP_ERROR = "poll_loop_error"
CONFIG_RELOADED = "config_reloaded"
STARTUP_REQUEUED = "startup_requeued"

# Env vars whose values are ALWAYS masked in log output, regardless of config.
_ALWAYS_REDACT: tuple[str, ...] = ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_DB_URL")

_context_lock = threading.Lock()
_context: dict[str, Any] = {}

# Secret literal values injected by the config loader. Supplements the env-var values
# resolved by RedactionFilter when the filter is instantiated.
_extra_redacted_values: set[str] = set()
_extra_lock = threading.Lock()


def set_context(**kwargs: Any) -> None:
    """Attach persistent fields (e.g. worker_id, version) emitted on every event."""
    with _context_lock:
        _context.update(kwargs)


def set_redacted_values(values: set[str]) -> None:
    """Register additional literal values to redact from every event.

    Called from observability.setup after config load so the loader's collected
    `Config._secrets` values participate in redaction alongside the static env var set.
    """
    with _extra_lock:
        _extra_redacted_values.clear()
        _extra_redacted_values.update(v for v in values if v)


def _current_secret_values(base: list[str]) -> list[str]:
    with _extra_lock:
        extras = list(_extra_redacted_values)
    return [v for v in (base + extras) if v]


def redact_mapping(d: dict[str, Any], secrets: list[str]) -> dict[str, Any]:
    """Recursively replace any string leaf whose value contains a secret with a masked copy."""

    def scrub(value: Any) -> Any:
        if isinstance(value, str):
            out = value
            for s in secrets:
                if s and s in out:
                    out = out.replace(s, "***")
            return out
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value

    return scrub(d)


def _snapshot_context() -> dict[str, Any]:
    with _context_lock:
        return dict(_context)


class RedactionFilter(logging.Filter):
    """Replaces any occurrences of configured env-var values inside log payloads."""

    def __init__(self, extra_env_names: list[str] | None = None) -> None:
        super().__init__()
        names = list(_ALWAYS_REDACT) + list(extra_env_names or [])
        # Only redact values that are non-empty strings — empty env vars would wildcard-match.
        self._values = [v for v in (os.environ.get(n) for n in names) if v]

    def current_values(self) -> list[str]:
        return _current_secret_values(self._values)

    def filter(self, record: logging.LogRecord) -> bool:
        values = self.current_values()
        for v in values:
            if not v:
                continue
            if isinstance(record.msg, str) and v in record.msg:
                record.msg = record.msg.replace(v, "***")
            if record.args:
                record.args = tuple(
                    (a.replace(v, "***") if isinstance(a, str) else a) for a in record.args
                )
            if hasattr(record, "fields") and isinstance(record.fields, dict):
                record.fields = {k: _redact_value(val, v) for k, val in record.fields.items()}
        return True


def _redact_value(val: Any, secret: str) -> Any:
    if isinstance(val, str) and secret in val:
        return val.replace(secret, "***")
    return val


class JsonFormatter(logging.Formatter):
    """Emits one JSON object per line with event, timestamp, level, and attached context."""

    def format(self, record: logging.LogRecord) -> str:
        secrets = _current_secret_values([os.environ.get(n, "") for n in _ALWAYS_REDACT])
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "event_type": getattr(record, "event", record.getMessage()),
        }
        payload.update(redact_mapping(_snapshot_context(), secrets))
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(redact_mapping(fields, secrets))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def emit(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit a single structured event."""
    logger = logging.getLogger("minicrew")
    logger.log(level, event, extra={"event": event, "fields": fields})
