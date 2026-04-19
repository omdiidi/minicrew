"""Log sinks. v1 ships FileSink with daily rotation; Postgres/HTTP are reserved for v2."""
from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any


class FileSink:
    """Wraps a TimedRotatingFileHandler so sink setup is uniform across types."""

    def __init__(self, *, path: str, rotate: str, keep: int, instance: int) -> None:
        resolved = Path(path.format(instance=instance))
        resolved.parent.mkdir(parents=True, exist_ok=True)
        when = {"daily": "D", "hourly": "H", "none": "D"}.get(rotate, "D")
        # `when='D'` with backupCount handles daily rotation + retention.
        self.handler = TimedRotatingFileHandler(
            resolved, when=when, backupCount=keep, encoding="utf-8", utc=True
        )

    def as_handler(self) -> logging.Handler:
        return self.handler


class PostgresSink:
    """Reserved for v2. Loader rejects this sink type up front; this class is here for schema parity."""

    def __init__(self, **_: Any) -> None:
        raise NotImplementedError("sink type 'postgres' is reserved for v2")


class HttpSink:
    """Reserved for v2. Loader rejects this sink type up front; this class is here for schema parity."""

    def __init__(self, **_: Any) -> None:
        raise NotImplementedError("sink type 'http' is reserved for v2")
