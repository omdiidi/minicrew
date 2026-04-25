"""Shared runner-script constants used by both launcher.py and launcher_resume.py.

Single source of truth for ``EFFORT_MAP`` so the two runner-emitting modules can't
drift on the model-effort mapping. ``launcher_resume.py`` imports from here.

NOTE: launcher.py also has a private _EFFORT_MAP — keep both in sync until launcher.py
can import from here. (CLAUDE.md treats launcher.py as load-bearing and forbids edits
without first reading docs/TROUBLESHOOTING.md.)
"""
from __future__ import annotations

EFFORT_MAP: dict[str, str] = {
    "none": "low",
    "medium": "medium",
    "high": "high",
}
