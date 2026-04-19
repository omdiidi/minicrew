"""Path helpers: resolve config path, tmpdir, log dir; exposes a `trust <dir>` subcommand."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def config_path() -> Path | None:
    value = os.environ.get("MINICREW_CONFIG_PATH")
    return Path(value).resolve() if value else None


def repo_root() -> Path:
    # worker/utils/paths.py -> worker/utils -> worker -> <repo root>
    return Path(__file__).resolve().parent.parent.parent


def log_dir() -> Path:
    # Logs live under the repo root unless a caller overrides explicitly.
    return repo_root() / "logs"


def tmp_root() -> Path:
    override = os.environ.get("MINICREW_TMPDIR")
    if override:
        return Path(override)
    return Path("/tmp")


def trust_directory(directory: str | Path) -> None:
    """Writes projects[<dir>].hasTrustDialogAccepted=true in ~/.claude.json.

    Pre-trusts the target directory and its realpath only. We deliberately do NOT trust
    `/tmp` or `/private/tmp` wholesale — doing so would accept any future Claude Code
    session cwd landing under /tmp without its own opt-in, which is a lateral-trust
    vector for anyone with shell access on the machine.
    """
    claude_json = Path.home() / ".claude.json"
    directory = str(directory)
    try:
        if claude_json.exists():
            config = json.loads(claude_json.read_text())
        else:
            config = {}
        projects = config.setdefault("projects", {})
        projects.setdefault(directory, {})["hasTrustDialogAccepted"] = True
        real_dir = os.path.realpath(directory)
        if real_dir != directory:
            projects.setdefault(real_dir, {})["hasTrustDialogAccepted"] = True
        claude_json.write_text(json.dumps(config, indent=2))
    except OSError as e:
        # Best-effort — don't crash the worker over a trust cache write failure.
        print(f"[trust] Warning: could not pre-trust directory {directory}: {e}", file=sys.stderr)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m worker.utils.paths")
    sub = parser.add_subparsers(dest="cmd", required=True)
    trust_parser = sub.add_parser("trust", help="Pre-trust a directory in ~/.claude.json")
    trust_parser.add_argument("directory")
    args = parser.parse_args(argv)
    if args.cmd == "trust":
        trust_directory(args.directory)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
