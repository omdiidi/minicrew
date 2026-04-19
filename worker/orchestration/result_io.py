"""Safe result-file read helper shared by single and fan-out orchestrators.

Enforces two invariants before opening:
  1. The resolved realpath must remain inside the session cwd (no traversal via symlink).
  2. The open itself uses O_NOFOLLOW so a symlink at the final component is rejected.

Callers receive None on any anomaly; they should treat that as a read failure and mark
the job as error.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def read_result_safe(cwd: Path, filename: str) -> Any:
    """Read and JSON-decode the result file, or return None on any safety/read failure."""
    candidate = cwd / filename
    try:
        real_cwd = os.path.realpath(str(cwd))
        real_candidate = os.path.realpath(str(candidate))
    except OSError:
        return None

    # Traversal guard: realpath must stay under the session cwd.
    real_cwd_with_sep = real_cwd.rstrip(os.sep) + os.sep
    if real_candidate != real_cwd and not real_candidate.startswith(real_cwd_with_sep):
        return None

    if not candidate.exists():
        return None

    try:
        fd = os.open(str(candidate), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        # ELOOP if the final path component is a symlink; other errors treated uniformly.
        return None

    try:
        with os.fdopen(fd, "r", encoding="utf-8") as fp:
            text = fp.read()
    except OSError:
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Non-JSON result files are preserved as raw strings so consumers can still inspect them.
        return {"raw": text}
