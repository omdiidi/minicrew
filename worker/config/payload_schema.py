"""Optional payload.schema.json validator — invoked before launching a session."""
from __future__ import annotations

from typing import Any

import jsonschema


class PayloadValidationError(ValueError):
    """Raised when a job payload fails the consumer's payload.schema.json."""


def validate_payload(payload: Any, schema: dict | None) -> None:
    if schema is None:
        return
    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as e:
        where = "/".join(str(p) for p in e.absolute_path) or "<root>"
        raise PayloadValidationError(f"payload invalid at {where}: {e.message}") from e
