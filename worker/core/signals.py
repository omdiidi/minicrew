"""SIGTERM / SIGINT handlers — flip state.shutdown_requested and let the poll loop wind down."""
from __future__ import annotations

import signal
from types import FrameType

import worker.core.state as state


def _handle(_signum: int, _frame: FrameType | None) -> None:
    state.request_shutdown()


def install() -> None:
    """Install SIGTERM + SIGINT handlers. SIGHUP is intentionally NOT handled in v1."""
    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
