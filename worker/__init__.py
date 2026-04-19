"""minicrew worker package: polls a queue and runs visible Terminal.app sessions."""
from __future__ import annotations

from worker.utils.version import read_version

__version__ = read_version()

__all__ = ["__version__"]
