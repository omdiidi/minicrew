"""Command-line entry points: `python -m worker`, `--status`, `--validate`."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from worker import __version__


def _cmd_status(args: argparse.Namespace) -> int:
    # Import lazily so `--help` doesn't trigger a full config load.
    from worker.config.loader import ConfigError, load_config
    from worker.db.client import PostgrestClient
    from worker.db.queries import get_worker_stats, get_workers

    try:
        cfg = load_config(args.config_path)
    except ConfigError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1

    client = PostgrestClient(cfg.db.url, cfg.db.service_key)
    try:
        workers = get_workers(client, cfg)
        stats = get_worker_stats(client)
    finally:
        client.close()

    output = {
        "version": __version__,
        "workers": workers,
        "queue_depth": stats.get("queue_depth", 0),
        "running_count": stats.get("running_count", 0),
        "recent_errors_1h": stats.get("recent_errors_1h", 0),
        "recent_failed_permanent_24h": stats.get("recent_failed_permanent_24h", 0),
    }
    print(json.dumps(output, default=str))
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    from worker.config.loader import ConfigError, load_config

    target = args.validate or os.environ.get("MINICREW_CONFIG_PATH")
    if not target:
        print("error: no path given and MINICREW_CONFIG_PATH not set", file=sys.stderr)
        return 2
    try:
        load_config(target)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"ok: {target}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from worker.core.main_loop import RunOptions, run

    role = args.role or os.environ.get("WORKER_ROLE")
    poll = args.poll_interval
    if poll is None and os.environ.get("POLL_INTERVAL"):
        try:
            poll = int(os.environ["POLL_INTERVAL"])
        except ValueError:
            poll = None
    opts = RunOptions(
        instance=args.instance,
        role=role,
        poll_interval=poll,
        config_path=args.config_path,
    )
    return run(opts)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m worker", description="minicrew worker daemon")
    parser.add_argument("--instance", type=int, default=1, choices=[1, 2, 3, 4, 5])
    parser.add_argument("--role", choices=["primary", "secondary"], default=None)
    parser.add_argument("--poll-interval", type=int, default=None)
    parser.add_argument(
        "--config-path",
        type=Path,
        default=None,
        help="Path to worker-config directory (falls back to MINICREW_CONFIG_PATH).",
    )
    parser.add_argument("--status", action="store_true", help="Print fleet status JSON and exit.")
    parser.add_argument("--validate", type=str, default=None, help="Validate a config path and exit.")
    parser.add_argument("--version", action="version", version=f"minicrew {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.status:
        return _cmd_status(args)
    if args.validate:
        return _cmd_validate(args)
    return _cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
