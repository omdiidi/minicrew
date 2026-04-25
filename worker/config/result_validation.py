"""Result validation: tagged ResultRead dataclass + JSON Schema check.

The orchestration layer reads the session's result file (typically `result.json`) and
hands it here together with the optional `result_schema` from the job_type. This module
returns a tagged dataclass so callers can branch on `ok` without colliding with the
non-JSON `{"raw": str}` sentinel that read_result_safe emits when the file is not JSON.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jsonschema


@dataclass
class ResultRead:
    """Tagged result of reading + validating a session's result file.

    `ok=True` means the result is usable (either schema-passed JSON, or non-JSON when
    no schema was configured). `ok=False` means the result was rejected; `value` still
    carries whatever was read so the orchestrator can persist it for debugging, and
    `error` holds the human-readable reason.
    """

    ok: bool
    value: Any
    error: str | None


class ResultValidationError(ValueError):
    """Raised when a result fails JSON Schema validation in a context that cannot
    handle the failure as a tagged dataclass (currently unused by the engine; reserved
    for callers that prefer exception-based flow).
    """


def validate(value: Any, schema: dict | None) -> ResultRead:
    """Validate `value` against `schema`.

    Cases:
    - `schema is None`: always ok; passthrough.
    - `value` is the `{"raw": str}` non-JSON shape AND schema is set: rejected, since
      a result_schema implies JSON output. Documented in docs/PROMPTS.md.
    - Otherwise: run jsonschema.validate; convert ValidationError to a tagged failure.
    """
    if schema is None:
        return ResultRead(ok=True, value=value, error=None)
    if (
        isinstance(value, dict)
        and set(value.keys()) == {"raw"}
        and isinstance(value.get("raw"), str)
    ):
        return ResultRead(
            ok=False,
            value=value,
            error="result is non-JSON; result_schema requires JSON output",
        )
    try:
        jsonschema.validate(value, schema=schema)
    except jsonschema.ValidationError as e:
        where = "/".join(str(p) for p in e.absolute_path) or "<root>"
        return ResultRead(
            ok=False,
            value=value,
            error=f"result validation failed at {where}: {e.message}",
        )
    return ResultRead(ok=True, value=value, error=None)
