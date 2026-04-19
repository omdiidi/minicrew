"""Reads the repo-root VERSION file once at import time."""
from __future__ import annotations

from pathlib import Path


def _repo_root() -> Path:
    # worker/utils/version.py -> worker/utils -> worker -> <repo root>
    return Path(__file__).resolve().parent.parent.parent


def read_version() -> str:
    vfile = _repo_root() / "VERSION"
    if not vfile.exists():
        return "0.0.0"
    return vfile.read_text(encoding="utf-8").strip() or "0.0.0"
