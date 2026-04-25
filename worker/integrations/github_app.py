"""GitHub App install-token minting + git clone/push helpers (httpx-based).

GitHub's git smart-HTTP requires URL-embedded creds (`x-access-token:<token>@`) or a
Basic auth header. Bearer-style http.extraheader is not honored. Token is removed from
on-disk .git/config immediately after clone (origin is reset to the bare URL); it is
re-injected per-operation for fetch/push and never written back to config. Subprocess
timeouts are mandatory; token is scrubbed from any error output before raising.
"""
from __future__ import annotations

import base64
import os
import subprocess
import urllib.parse
import time
from pathlib import Path
from typing import Callable

import httpx
import jwt


class GitHubAppError(RuntimeError):
    pass


def _load_pem(env_name: str) -> bytes:
    """Load a PEM private key from an env var.

    Supports either raw PEM (starts with ``-----BEGIN``) or base64-encoded PEM
    (single-line, easier to ship through environment-variable plumbing).
    """
    raw = os.environ.get(env_name)
    if not raw:
        raise GitHubAppError(f"{env_name} not set")
    if raw.lstrip().startswith("-----BEGIN"):
        return raw.encode()
    try:
        return base64.b64decode(raw)
    except Exception as e:
        raise GitHubAppError(f"{env_name} is neither raw PEM nor valid base64: {e}") from e


def mint_app_jwt(app_id: str, pem: bytes) -> str:
    """Mint a short-lived GitHub App JWT (RS256, ~9 minute expiry per GitHub limits)."""
    now = int(time.time())
    return jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": app_id},
        pem,
        algorithm="RS256",
    )


def mint_install_token(cfg, http: httpx.Client | None = None) -> str:
    """Exchange an App JWT for an installation access token.

    Three-attempt exponential backoff (1s, 2s, 4s) on transport errors or non-201 responses.
    Raises GitHubAppError on final failure with the last server response truncated.
    """
    app = cfg.dispatch.github_app
    pem = _load_pem(app.private_key_env)
    install_id = os.environ.get(app.installation_id_env)
    if not install_id:
        raise GitHubAppError(f"{app.installation_id_env} not set")
    jwt_token = mint_app_jwt(app.app_id, pem)
    own_client = http is None
    client = http or httpx.Client(timeout=10)
    last_err: str | None = None
    try:
        for attempt in range(3):
            try:
                r = client.post(
                    f"https://api.github.com/app/installations/{install_id}/access_tokens",
                    headers={
                        "Authorization": f"Bearer {jwt_token}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                if r.status_code == 201:
                    return r.json()["token"]
                last_err = f"{r.status_code} {r.text[:200]}"
            except httpx.HTTPError as e:
                last_err = str(e)
            time.sleep(2 ** attempt)
    finally:
        if own_client:
            client.close()
    raise GitHubAppError(f"install token mint failed after 3 tries: {last_err}")


def authenticated_clone(
    repo_url: str,
    sha: str,
    dest: Path,
    token: str,
    *,
    timeout: int = 300,
    cancel_check: Callable[[], bool] | None = None,
) -> None:
    """Clone + fetch-by-sha + checkout, with a cancel checkpoint between each subprocess.

    cancel_check is an optional callable returning True when the orchestrator wants
    to abort. We check before each subprocess so cancel during a slow clone aborts
    after the current step completes — bounded latency = current step's remaining
    runtime, not the full helper.
    """
    if not repo_url.startswith("https://github.com/"):
        raise GitHubAppError(f"refusing to clone non-https-github URL: {repo_url}")
    # GitHub's git smart-HTTP accepts URL-embedded creds with the
    # `x-access-token:<install-token>` username form. Bearer-style
    # http.extraheader is not honored by GitHub for git push/clone.
    safe_token = urllib.parse.quote(token, safe="")
    auth_url = repo_url.replace("https://", f"https://x-access-token:{safe_token}@", 1)

    def _check_cancel(stage: str) -> None:
        if cancel_check and cancel_check():
            raise GitHubAppError(f"cancelled before {stage}")

    try:
        _check_cancel("clone")
        subprocess.run(
            [
                "git", "clone",
                "--filter=blob:none", "--no-tags", "--quiet",
                auth_url, str(dest),
            ],
            check=True, capture_output=True, timeout=timeout,
        )
        _check_cancel("fetch")
        # GitHub supports `git fetch <url> <sha>` for full 40-char SHAs
        # (uploadpack.allowAnySHA1InWant is on by default). Schema enforces 40-char hex.
        # Pass the auth URL explicitly so the token isn't read from .git/config.
        subprocess.run(
            ["git", "-C", str(dest), "fetch", "--quiet", auth_url, sha],
            check=True, capture_output=True, timeout=timeout,
        )
        # Rewrite origin to the public URL so the token doesn't persist on disk.
        # push_branch() re-injects the token at push time via the same URL form.
        subprocess.run(
            ["git", "-C", str(dest), "remote", "set-url", "origin", repo_url],
            check=True, capture_output=True, timeout=30,
        )
        _check_cancel("checkout")
        subprocess.run(
            ["git", "-C", str(dest), "checkout", "--quiet", sha],
            check=True, capture_output=True, timeout=60,
        )
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", "replace").replace(token, "***")
        raise GitHubAppError(f"git operation failed: {stderr.strip()}") from None
    except subprocess.TimeoutExpired as e:
        raise GitHubAppError(f"git operation timed out after {timeout}s") from e


def remove_origin(clone_dir: Path) -> None:
    """Drop the origin remote so the inner session can't accidentally push to it.

    check=False because a missing origin is a no-op, not a fault."""
    subprocess.run(
        ["git", "-C", str(clone_dir), "remote", "remove", "origin"],
        check=False, capture_output=True,
    )


def precreate_branch(clone_dir: Path, branch: str) -> None:
    """Create + check out a fresh branch where the result will be committed."""
    subprocess.run(
        ["git", "-C", str(clone_dir), "checkout", "-b", branch],
        check=True, capture_output=True,
    )


def push_branch(clone_dir: Path, branch: str, token: str, repo_url: str) -> str | None:
    """Stage, commit, and push the result branch.

    Returns the pushed sha, or None if there were no changes to push (the inner session
    made no edits). Caller distinguishes None ('no diff, no push') from raised exception
    ('push failed'). 403 from the remote maps to a clear missing-permission error.
    """
    safe_token = urllib.parse.quote(token, safe="")
    auth_url = repo_url.replace("https://", f"https://x-access-token:{safe_token}@", 1)
    subprocess.run(
        ["git", "-C", str(clone_dir), "add", "-A"],
        check=True, capture_output=True,
    )
    diff = subprocess.run(
        ["git", "-C", str(clone_dir), "diff", "--cached", "--quiet"],
        capture_output=True,
    )
    if diff.returncode == 0:
        # Nothing staged after `add -A` => session made zero changes; do not push.
        return None
    try:
        subprocess.run(
            ["git", "-C", str(clone_dir), "commit", "-m", f"minicrew result {branch}"],
            check=True, capture_output=True,
        )
        sha = subprocess.run(
            ["git", "-C", str(clone_dir), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(clone_dir), "push", auth_url, branch],
            check=True, capture_output=True, timeout=120,
        )
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", "replace").replace(token, "***")
        if "403" in stderr or "Permission" in stderr:
            raise GitHubAppError(
                f"GitHub App lacks contents:write permission on {repo_url}"
            ) from None
        raise GitHubAppError(f"git push failed: {stderr.strip()}") from None
    except subprocess.TimeoutExpired as e:
        raise GitHubAppError(f"git push timed out: {e}") from e
    return sha
