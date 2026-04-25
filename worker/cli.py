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


def _cmd_preflight(args: argparse.Namespace) -> int:
    from worker.config.loader import ConfigError, load_config
    from worker.platform import detect_platform
    from worker.platform.base import PreflightError

    try:
        cfg = load_config(args.config_path)
    except ConfigError as e:
        print(f"preflight failed: {e}", file=sys.stderr)
        return 1

    try:
        platform = detect_platform(cfg)
        platform.preflight()
    except PreflightError as e:
        print(f"preflight failed: {e}", file=sys.stderr)
        return 1
    print("ok: platform")
    if cfg.dispatch is not None:
        try:
            platform.dispatch_preflight(cfg)
        except PreflightError as e:
            print(f"dispatch preflight failed: {e}", file=sys.stderr)
            return 1
        print("ok: dispatch")
    return 0


def _cmd_check_rpcs(args: argparse.Namespace) -> int:
    """Probe dispatch_check_rpcs against the configured DB. Exit 0 if all present, 1 if any missing."""
    import httpx

    from worker.config.loader import ConfigError, load_config
    from worker.platform.base import _REQUIRED_DISPATCH_RPCS, _storage_base_url

    try:
        cfg = load_config(args.config_path)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    base_url = _storage_base_url(cfg.db.url)
    try:
        result = httpx.post(
            f"{base_url}/rest/v1/rpc/dispatch_check_rpcs",
            headers={
                "Authorization": f"Bearer {cfg.db.service_key}",
                "apikey": cfg.db.service_key,
                "Content-Type": "application/json",
            },
            json={"p_names": list(_REQUIRED_DISPATCH_RPCS)},
            timeout=10,
        )
    except httpx.HTTPError as e:
        print(f"error: dispatch_check_rpcs probe failed: {e}", file=sys.stderr)
        return 1
    if result.status_code != 200:
        print(
            f"error: dispatch_check_rpcs returned HTTP {result.status_code}: {result.text[:200]}",
            file=sys.stderr,
        )
        return 1
    missing = result.json() or []
    if missing:
        print(json.dumps({"missing": sorted(missing)}))
        print(
            "Apply schema/migrations/002_remote_subagent.sql + 003_handoff.sql.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"missing": [], "checked": list(_REQUIRED_DISPATCH_RPCS)}))
    return 0


def _cmd_delete_bundle(args: argparse.Namespace) -> int:
    """Ad-hoc cleanup of a leaked vault bundle. Service-role only."""
    from worker.config.loader import ConfigError, load_config
    from worker.db.client import PostgrestClient

    bundle_id = args.delete_bundle
    kind = args.bundle_type or "transcript"
    if kind not in ("transcript", "mcp"):
        print(f"error: --type must be 'transcript' or 'mcp' (got {kind!r})", file=sys.stderr)
        return 2

    try:
        cfg = load_config(args.config_path)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    rpc_name = (
        "dispatch_delete_transcript_bundle"
        if kind == "transcript"
        else "dispatch_delete_mcp_bundle"
    )

    if not args.yes:
        prompt = f"Delete {kind} bundle {bundle_id} via {rpc_name}? [y/N] "
        try:
            ans = input(prompt).strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("aborted", file=sys.stderr)
            return 1

    client = PostgrestClient(cfg.db.url, cfg.db.service_key)
    try:
        client.rpc(rpc_name, {"p_id": str(bundle_id)})
    except Exception as e:
        print(f"error: {rpc_name} failed: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()
    print(json.dumps({"deleted": str(bundle_id), "kind": kind, "rpc": rpc_name}))
    return 0


def _cmd_list_orphans(args: argparse.Namespace) -> int:
    """SELECT from v_orphan_transcript_bundles + v_orphan_mcp_bundles. Service-role only."""
    from worker.config.loader import ConfigError, load_config
    from worker.db.client import PostgrestClient

    try:
        cfg = load_config(args.config_path)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    client = PostgrestClient(cfg.db.url, cfg.db.service_key)
    try:
        try:
            transcript_orphans = client.get("v_orphan_transcript_bundles", select="*")
        except Exception as e:
            print(f"error: query v_orphan_transcript_bundles failed: {e}", file=sys.stderr)
            return 1
        try:
            mcp_orphans = client.get("v_orphan_mcp_bundles", select="*")
        except Exception as e:
            print(f"error: query v_orphan_mcp_bundles failed: {e}", file=sys.stderr)
            return 1
    finally:
        client.close()

    output = {
        "orphan_transcript_bundles": transcript_orphans or [],
        "orphan_mcp_bundles": mcp_orphans or [],
        "transcript_count": len(transcript_orphans or []),
        "mcp_count": len(mcp_orphans or []),
    }
    print(json.dumps(output, default=str, indent=2))
    return 0


_VALID_DISPATCH_TYPES = ("ad_hoc", "handoff")
_SHA_LEN = 40


def _dispatch_env() -> tuple[str, str]:
    """Resolve (supabase_url, auth_key) from env. Caller may use service_role
    for testing or a user JWT in MINICREW_DISPATCH_JWT for least-privilege.
    """
    url = (
        os.environ.get("SUPABASE_URL")
        or os.environ.get("MINICREW_SUPABASE_URL")
        or ""
    ).rstrip("/")
    key = (
        os.environ.get("MINICREW_DISPATCH_JWT")
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("MINICREW_SUPABASE_SERVICE_KEY")
        or ""
    )
    if not url or not key:
        raise SystemExit(
            "error: set SUPABASE_URL and one of "
            "MINICREW_DISPATCH_JWT / SUPABASE_SERVICE_ROLE_KEY in the environment."
        )
    return url, key


def _cmd_dispatch(args: argparse.Namespace) -> int:
    """Insert a dispatch job (ad_hoc or handoff) and optionally wait for it.

    For ad_hoc: --repo --sha --prompt are required.
    For handoff: --repo --sha --session-id --bundle-id are required.
    """
    import time

    import httpx

    kind = args.dispatch
    if kind not in _VALID_DISPATCH_TYPES:
        print(f"error: --dispatch must be one of {_VALID_DISPATCH_TYPES}", file=sys.stderr)
        return 2

    if not args.repo or not args.sha:
        print("error: --repo and --sha are required.", file=sys.stderr)
        return 2
    if len(args.sha) != _SHA_LEN or any(c not in "0123456789abcdef" for c in args.sha.lower()):
        print("error: --sha must be a 40-char hex commit sha.", file=sys.stderr)
        return 2
    if not args.repo.startswith("https://github.com/"):
        print("error: --repo must be an https://github.com/ URL.", file=sys.stderr)
        return 2

    payload: dict = {"repo": {"url": args.repo, "sha": args.sha.lower()}}
    if args.allow_code_push:
        payload["allow_code_push"] = True

    if kind == "ad_hoc":
        if not args.prompt:
            print("error: --prompt is required for ad_hoc.", file=sys.stderr)
            return 2
        payload["prompt"] = args.prompt
    else:
        if not args.session_id or not args.bundle_id:
            print("error: --session-id and --bundle-id are required for handoff.", file=sys.stderr)
            return 2
        payload["session_id"] = args.session_id
        payload["transcript_bundle_id"] = args.bundle_id
        if args.prompt:
            payload["prompt"] = args.prompt

    base_url, key = _dispatch_env()
    headers = {
        "Authorization": f"Bearer {key}",
        "apikey": key,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    body = [{
        "job_type": kind,
        "status": "pending",
        "payload": payload,
        "requires": {},
        "max_attempts": 1,
    }]
    try:
        resp = httpx.post(f"{base_url}/rest/v1/jobs", json=body, headers=headers, timeout=15)
    except httpx.HTTPError as e:
        print(f"error: insert failed: {e}", file=sys.stderr)
        return 1
    if resp.status_code >= 400:
        print(f"error: insert returned HTTP {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        return 1
    rows = resp.json() or []
    if not rows:
        print("error: insert returned no row (RLS denial?).", file=sys.stderr)
        return 1
    job_id = rows[0]["id"]
    print(json.dumps({"job_id": job_id, "job_type": kind, "status": "pending"}))

    if not args.wait:
        return 0

    deadline = time.time() + max(60, int(args.wait_seconds or 1800))
    last = ""
    while time.time() < deadline:
        try:
            r = httpx.get(
                f"{base_url}/rest/v1/jobs",
                params={
                    "id": f"eq.{job_id}",
                    "select": "id,status,worker_id,started_at,completed_at,error_message,result",
                },
                headers={"Authorization": f"Bearer {key}", "apikey": key},
                timeout=10,
            )
            r.raise_for_status()
            rows = r.json() or []
            if rows:
                row = rows[0]
                snap = json.dumps({k: row.get(k) for k in ("status", "worker_id", "error_message")})
                if snap != last:
                    print(snap)
                    last = snap
                if row["status"] in ("completed", "failed", "cancelled", "error"):
                    print(json.dumps({"final": row}, default=str, indent=2))
                    return 0 if row["status"] == "completed" else 1
        except httpx.HTTPError:
            pass
        time.sleep(5)
    print("error: timed out waiting for terminal status.", file=sys.stderr)
    return 1


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
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Run platform preflight checks (env readiness) and exit.",
    )
    parser.add_argument(
        "--check-rpcs",
        action="store_true",
        help="Probe dispatch RPCs in the configured DB. Exit 1 if any missing.",
    )
    parser.add_argument(
        "--delete-bundle",
        type=str,
        default=None,
        metavar="UUID",
        help="Ad-hoc delete of a leaked vault bundle (service-role only).",
    )
    parser.add_argument(
        "--type",
        dest="bundle_type",
        choices=["transcript", "mcp"],
        default=None,
        help="With --delete-bundle: which bundle type (default: transcript).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="With --delete-bundle: skip the confirmation prompt.",
    )
    parser.add_argument(
        "--list-orphans",
        action="store_true",
        help="List orphan transcript + MCP bundles (service-role only).",
    )
    parser.add_argument(
        "--dispatch",
        choices=list(_VALID_DISPATCH_TYPES),
        default=None,
        help="Insert a job. Use with --repo --sha --prompt (ad_hoc) or "
             "--repo --sha --session-id --bundle-id (handoff). Optional --wait.",
    )
    parser.add_argument("--repo", type=str, default=None, help="https://github.com/<owner>/<repo>")
    parser.add_argument("--sha", type=str, default=None, help="40-char commit sha")
    parser.add_argument("--prompt", type=str, default=None, help="Task prompt (ad_hoc)")
    parser.add_argument("--session-id", dest="session_id", type=str, default=None,
                        help="UUID of the local Claude session to resume (handoff)")
    parser.add_argument("--bundle-id", dest="bundle_id", type=str, default=None,
                        help="UUID of the vault transcript bundle (handoff)")
    parser.add_argument("--allow-code-push", action="store_true",
                        help="Allow the remote session to push a result branch.")
    parser.add_argument("--wait", action="store_true",
                        help="Block until the job reaches a terminal status.")
    parser.add_argument("--wait-seconds", type=int, default=1800,
                        help="Max seconds to --wait (default 1800).")
    parser.add_argument("--version", action="version", version=f"minicrew {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.status:
        return _cmd_status(args)
    if args.validate:
        return _cmd_validate(args)
    if args.preflight:
        return _cmd_preflight(args)
    if args.check_rpcs:
        return _cmd_check_rpcs(args)
    if args.delete_bundle:
        return _cmd_delete_bundle(args)
    if args.list_orphans:
        return _cmd_list_orphans(args)
    if args.dispatch:
        return _cmd_dispatch(args)
    return _cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
