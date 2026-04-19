"""Safe result-file read helper shared by single and fan-out orchestrators.

Defense-in-depth against symlink-swap attacks on the final path component. The
primary guarantee comes from `O_NOFOLLOW`: if the final component is a symlink at
open time, the open fails with ELOOP. We additionally verify after the open that
the fd's resolved path sits inside the session cwd.

**TOCTOU caveat:** an attacker who controls an intermediate directory on the path
can still race a symlink swap before the open. This helper does not close that
window; it only protects against non-racy symlink attacks on the final component
and against casual traversal attempts. A fully race-free variant would need
`openat2(RESOLVE_NO_SYMLINKS | RESOLVE_BENEATH)` on Linux (not available on
macOS), or per-component `O_NOFOLLOW` traversal. This helper is intentionally
limited to the simpler defense.

Callers receive None on any anomaly; they should treat that as a read failure and
mark the job as error.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def read_result_safe(cwd: Path, filename: str) -> Any:
    """Read and JSON-decode the result file, or return None on any safety/read failure.

    Order of operations (defense-in-depth):
      1. Open the candidate path with O_NOFOLLOW — a symlink at the final component
         raises ELOOP and we reject.
      2. Resolve the fd's path (best-effort on macOS via realpath of the candidate
         path; on Linux `/proc/self/fd/<fd>` would be tighter) and verify it sits
         under the session cwd realpath.
      3. Only then read and JSON-decode.
    """
    candidate = cwd / filename

    # Open FIRST with O_NOFOLLOW. If the final component is a symlink (the most
    # likely exfiltration vector for a prompt-injected session), open fails with
    # ELOOP before we touch any content.
    try:
        fd = os.open(str(candidate), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None

    try:
        try:
            real_cwd = os.path.realpath(str(cwd))
        except OSError:
            os.close(fd)
            return None

        # macOS lacks /proc/self/fd; fall back to realpath-of-candidate. The final
        # component is already protected by O_NOFOLLOW above, so this residual
        # check is the intermediate-directory check (best-effort).
        try:
            real_candidate = os.path.realpath(str(candidate))
        except OSError:
            os.close(fd)
            return None

        real_cwd_with_sep = real_cwd.rstrip(os.sep) + os.sep
        if real_candidate != real_cwd and not real_candidate.startswith(real_cwd_with_sep):
            os.close(fd)
            return None

        try:
            with os.fdopen(fd, "r", encoding="utf-8") as fp:
                text = fp.read()
        except OSError:
            return None
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Non-JSON result files are preserved as raw strings so consumers can still inspect them.
        return {"raw": text}
