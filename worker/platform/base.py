"""Platform protocol + SessionHandle + exceptions for minicrew's OS-specific seam."""
from __future__ import annotations

import json
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx

if TYPE_CHECKING:
    from worker.config.models import Config


class PreflightError(RuntimeError):
    """Raised when the runtime environment cannot host terminal sessions."""


class LaunchError(RuntimeError):
    """Raised when a terminal session cannot be opened."""


class CloseError(RuntimeError):
    """Raised when a terminal session cannot be closed cleanly (best-effort)."""


@dataclass
class SessionHandle:
    kind: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        import json
        return json.dumps({"kind": self.kind, "data": self.data})

    @classmethod
    def from_json(cls, payload: str) -> SessionHandle:
        import json
        obj = json.loads(payload)
        return cls(kind=obj["kind"], data=obj.get("data") or {})


class Platform(Protocol):
    name: str

    def preflight(self) -> None: ...

    def dispatch_preflight(self, cfg: Config) -> None:
        """Extended preflight checks when cfg.dispatch is configured.

        Raises PreflightError on any failure. Default impl raises NotImplementedError.
        """
        ...

    def launch_session(self, cwd: Path) -> SessionHandle: ...

    def close_session(self, handle: SessionHandle) -> None: ...

    def install_service(
        self,
        *,
        instance: int,
        role: str,
        poll_interval: int | None,
        config_path: Path,
        python: Path,
        worker_pkg_root: Path,
        log_dir: Path,
        replace_existing: bool,
    ) -> None: ...

    def uninstall_service(self, *, instance: int) -> None: ...

    def installed_instances(self) -> list[int]: ...


# All RPCs the worker calls when handling ad_hoc / handoff jobs. Used by
# `dispatch_preflight_common` to verify the database side is provisioned.
_REQUIRED_DISPATCH_RPCS: tuple[str, ...] = (
    "claim_next_job_with_cap",
    "dispatch_register_mcp_bundle",
    "dispatch_fetch_mcp_bundle",
    "dispatch_delete_mcp_bundle",
    "dispatch_register_transcript_bundle",
    "dispatch_fetch_transcript_bundle",
    "dispatch_fetch_outbound_transcript",
    "dispatch_delete_transcript_bundle",
    "dispatch_check_rpcs",
)


def _storage_base_url(rest_url: str) -> str:
    """Derive the Supabase project base URL from a PostgREST URL.

    cfg.db.url is canonically `https://<project>.supabase.co/rest/v1` (or local equiv).
    We strip the trailing `/rest/v1` so callers can compose other API paths
    (`/storage/v1/bucket/<name>`).
    """
    base = rest_url.rstrip("/")
    if base.endswith("/rest/v1"):
        base = base[: -len("/rest/v1")]
    return base


def dispatch_preflight_common(cfg: Config) -> None:
    """Platform-agnostic dispatch preflight: 7 checks, raise PreflightError on first failure.

    Both MacPlatform and LinuxPlatform delegate to this function; nothing here is
    OS-specific. Imports are kept local so the module load cost stays the same when
    cfg.dispatch is absent (the caller never invokes this).
    """
    from worker.utils.paths import repo_root

    # 1. Operator MCP isolation: ~/.claude/settings.json must NOT have user-level mcpServers.
    operator_settings = Path.home() / ".claude" / "settings.json"
    if operator_settings.exists():
        try:
            data = json.loads(operator_settings.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise PreflightError(
                f"{operator_settings} could not be parsed as JSON: {e}. "
                "ad_hoc/handoff dispatch requires the operator's user-level mcpServers "
                "to be empty (cross-tenant isolation)."
            ) from e
        servers = data.get("mcpServers") or {}
        if servers:
            raise PreflightError(
                f"{operator_settings} has mcpServers configured. ad_hoc/handoff "
                "dispatch requires the operator's user-level mcpServers to be empty "
                "(cross-tenant isolation). Move them into project-local "
                ".claude/settings.json files instead."
            )

    # 2. Required tooling on PATH for clone + result push + MCP launch.
    for tool in ("git", "node", "npx"):
        if shutil.which(tool) is None:
            raise PreflightError(
                f"required binary {tool!r} not found on PATH. "
                "ad_hoc/handoff dispatch needs git (clone/push), node + npx (MCP servers)."
            )

    # 3. .env at repo root must be 0600 if it exists — Supabase + GitHub App credentials.
    env_path = repo_root() / ".env"
    if env_path.exists():
        mode = env_path.stat().st_mode & 0o777
        if mode != 0o600:
            raise PreflightError(
                f"{env_path} mode is {oct(mode)}; must be 0o600. "
                "Run: chmod 600 .env"
            )

    # 4. GitHub App can mint an installation token end-to-end (PEM, install id, network all OK).
    from worker.integrations.github_app import GitHubAppError, mint_install_token

    try:
        mint_install_token(cfg)
    except GitHubAppError as e:
        raise PreflightError(f"GitHub App token mint failed: {e}") from e
    except Exception as e:  # pragma: no cover - defensive
        raise PreflightError(f"GitHub App token mint failed: {e}") from e

    # 5. Storage bucket reachable with the service-role key.
    base_url = _storage_base_url(cfg.db.url)
    bucket = cfg.dispatch.log_storage.bucket
    bucket_url = f"{base_url}/storage/v1/bucket/{bucket}"
    try:
        with httpx.Client(timeout=10) as http:
            r = http.head(
                bucket_url,
                headers={
                    "Authorization": f"Bearer {cfg.db.service_key}",
                    "apikey": cfg.db.service_key,
                },
            )
    except httpx.HTTPError as e:
        raise PreflightError(
            f"Storage bucket {bucket!r} unreachable at {bucket_url}: {e}"
        ) from e
    if r.status_code not in (200, 204):
        raise PreflightError(
            f"Storage bucket {bucket!r} HEAD returned {r.status_code} for service role; "
            "create the bucket and grant service-role read/write."
        )

    # 6. Storage bucket must NOT be anon-readable (cross-tenant isolation).
    # Send the request with no auth headers — anonymous access. Acceptable: 401/403/404.
    try:
        with httpx.Client(timeout=10) as http:
            anon_resp = http.head(bucket_url)
    except httpx.HTTPError as e:
        raise PreflightError(
            f"Storage bucket {bucket!r} anon-probe failed at {bucket_url}: {e}"
        ) from e
    if anon_resp.status_code == 200:
        raise PreflightError(
            f"Storage bucket {bucket!r} is anon-readable; "
            "tighten policies to service-role-only."
        )

    # 6b. Object-level anon probes. Bucket metadata (above) reflects bucket.public,
    # but Storage RLS is enforced per-object on storage.objects: a bucket can be
    # public=false yet still grant anon SELECT on a prefix via custom policy. Probe
    # non-existent paths in the prefixes the worker writes; expect 401/403/404. A 200
    # means anon can read those objects (or a public-listing policy returns synthetic
    # 200s) — fail closed.
    prefixes_to_probe = [
        f"{bucket}/transcripts/__preflight_probe__",
        f"{bucket}/__preflight_probe__/transcript.000.log",
    ]
    for probe_path in prefixes_to_probe:
        try:
            r = httpx.get(  # NO Authorization header — anon
                f"{base_url}/storage/v1/object/{probe_path}",
                timeout=5,
            )
        except httpx.HTTPError:
            continue
        if r.status_code == 200:
            raise PreflightError(
                f"Storage object {probe_path} is anon-readable — "
                "tighten storage.objects RLS policies to service-role-only."
            )
        # 401/403/404 = anon blocked, all good

    # 7. All required dispatch RPCs exist server-side.
    # NOTE: dispatch_check_rpcs returns the names that are MISSING (not those present).
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
        raise PreflightError(
            f"dispatch_check_rpcs probe failed: {e}. "
            "Apply schema/migrations/002_remote_subagent.sql + 003_handoff.sql."
        ) from e
    if result.status_code != 200:
        raise PreflightError(
            f"dispatch_check_rpcs returned HTTP {result.status_code}: {result.text[:200]}. "
            "Apply schema/migrations/002_remote_subagent.sql + 003_handoff.sql."
        )
    missing = result.json() or []
    if missing:
        raise PreflightError(
            f"Handoff/dispatch RPCs missing in database: {sorted(missing)}. "
            "Apply schema/migrations/002_remote_subagent.sql + 003_handoff.sql."
        )

    # 8. GitHub App permission scope check — verify the App has contents:write,
    # otherwise allow_code_push=true jobs will fail at push time after hours of work.
    try:
        from worker.integrations.github_app import _load_pem, mint_app_jwt
        import httpx as _httpx, os as _os
        app_cfg = cfg.dispatch.github_app
        pem = _load_pem(app_cfg.private_key_env)
        install_id = _os.environ.get(app_cfg.installation_id_env)
        jwt_tok = mint_app_jwt(app_cfg.app_id, pem)
        r = _httpx.get(
            f"https://api.github.com/app/installations/{install_id}",
            headers={"Authorization": f"Bearer {jwt_tok}", "Accept": "application/vnd.github+json"},
            timeout=10,
        )
        if r.status_code == 200:
            perms = (r.json() or {}).get("permissions", {})
            contents = perms.get("contents")
            if contents not in ("write", "admin"):
                raise PreflightError(
                    f"GitHub App lacks contents:write permission (got: {contents!r}). "
                    "ad_hoc/handoff jobs with allow_code_push=true will fail at push time. "
                    "Edit the App's permissions on github.com to add Contents: Read & write, then "
                    "accept the new permission on each installation."
                )
    except PreflightError:
        raise
    except Exception as e:
        # Soft-fail: log but don't block — permissions endpoint may be temporarily unreachable.
        import sys as _sys
        print(f"[preflight] could not verify GitHub App permissions: {e}", file=_sys.stderr)


# Used by Mac's _ensure_env_locked_down and other callers wanting consistent perms.
_ENV_MODE = stat.S_IRUSR | stat.S_IWUSR
