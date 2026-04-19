"""Wires stdlib logging to the configured sinks plus the redaction filter."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from worker.observability.events import JsonFormatter, RedactionFilter, set_context
from worker.observability.sinks import FileSink

if TYPE_CHECKING:
    from worker.config.models import LoggingConfig

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}


def setup(logging_cfg: LoggingConfig, worker_id: str, instance: int) -> None:
    """Idempotent: removes old handlers, installs fresh ones from config."""
    logger = logging.getLogger("minicrew")
    logger.setLevel(_LEVELS.get(logging_cfg.level, logging.INFO))
    logger.propagate = False

    for h in list(logger.handlers):
        logger.removeHandler(h)
    for f in list(logger.filters):
        logger.removeFilter(f)

    formatter = JsonFormatter()
    redactor = RedactionFilter(logging_cfg.redact_env)
    logger.addFilter(redactor)

    for sink_cfg in logging_cfg.sinks:
        if sink_cfg.get("type") == "file":
            sink = FileSink(
                path=sink_cfg["path"],
                rotate=sink_cfg.get("rotate", "daily"),
                keep=int(sink_cfg.get("keep", 30)),
                instance=instance,
            )
            handler = sink.as_handler()
            handler.setFormatter(formatter)
            handler.addFilter(redactor)
            logger.addHandler(handler)

    set_context(worker_id=worker_id, instance=instance)
