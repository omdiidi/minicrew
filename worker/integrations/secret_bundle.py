"""Vault-backed bundle helpers.

Two distinct bundle classes are handled here:

1. **MCP bundles** (Phase 2a) — caller-supplied per-job MCP server config, fetched once
   via the configured decrypted view, validated against ``ALLOWED_BUNDLE_TOP_KEYS``,
   then materialized into the cloned working tree by the orchestrator.

2. **Transcript bundles** (Phase 3 handoff) — bidirectional. Inbound: caller ships the
   prior session JSONL + every subagent JSONL into Vault; worker fetches and writes them
   under ``~/.claude/projects/<encoded-cwd>/``. Outbound: worker re-bundles the extended
   transcripts after the resumed session completes and stores them back via Vault (or
   via Storage when over ``vault_inline_cap_bytes``).

Both classes use the same allowlist/sanitize discipline: malformed shape fails-closed
with ``SecretBundleError`` rather than passing untrusted data into Claude Code.
"""
from __future__ import annotations

import gzip
import json
import re
import time
from uuid import UUID

import httpx


# --- MCP bundle constants ----------------------------------------------------

ALLOWED_BUNDLE_TOP_KEYS = {"mcpServers"}


# --- Transcript bundle constants ---------------------------------------------

ALLOWED_TRANSCRIPT_TOP_KEYS = {"top_level", "subagents", "session_id", "storage_ref"}
SUBAGENT_FILENAME_RE = re.compile(r"\A[A-Za-z0-9_\-]+\.jsonl\Z")
MAX_SUBAGENT_FILES = 64
MAX_SUBAGENT_FILE_BYTES = 5 * 1024 * 1024


class SecretBundleError(RuntimeError):
    pass


# =============================================================================
# MCP bundle helpers (Phase 2a)
# =============================================================================


def fetch_bundle(client, cfg, bundle_id) -> dict:
    """Read the decrypted MCP bundle via the SECURITY DEFINER bridge RPC; validate shape.

    PostgREST does not expose the `vault` schema directly. The bridge RPC keeps the
    decryption read inside `public` and confined to service_role.
    """
    try:
        raw = client.rpc(
            "dispatch_fetch_mcp_bundle", {"p_id": str(bundle_id)},
        )
    except Exception as e:
        raise SecretBundleError(f"mcp bundle {bundle_id} not found: {e}") from None
    # rpc() may return list[dict] or scalar text depending on PostgREST version.
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if isinstance(raw, dict):
        raw = next(iter(raw.values()), None)
    if not raw:
        raise SecretBundleError(f"mcp bundle {bundle_id} not found")
    try:
        bundle = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as e:
        raise SecretBundleError(f"mcp bundle {bundle_id} not valid JSON: {e}") from e
    if not isinstance(bundle, dict):
        raise SecretBundleError("mcp bundle must be a JSON object")
    extra = set(bundle.keys()) - ALLOWED_BUNDLE_TOP_KEYS
    if extra:
        raise SecretBundleError(f"mcp bundle has disallowed top-level keys: {extra}")
    if not isinstance(bundle.get("mcpServers"), dict):
        raise SecretBundleError("mcp bundle.mcpServers must be an object")
    return bundle


def delete_bundle(client, cfg, bundle_id) -> None:
    """Best-effort delete via SECURITY DEFINER RPC. Never raises into orchestrator cleanup."""
    if not cfg.dispatch.mcp_bundle.delete_mcp_on_completion:
        from worker.observability.events import DISPATCH_BUNDLE_RETAINED, emit
        emit(
            DISPATCH_BUNDLE_RETAINED,
            bundle_id=str(bundle_id),
            kind="mcp",
            reason="mcp_bundle.delete_mcp_on_completion=false",
        )
        return
    try:
        client.rpc(cfg.dispatch.mcp_bundle.delete_rpc, {"p_id": str(bundle_id)})
    except Exception as e:
        from worker.observability.events import POLL_LOOP_ERROR, emit
        emit(POLL_LOOP_ERROR, error=f"vault delete failed for bundle {bundle_id}: {e}")


# =============================================================================
# Transcript bundle helpers (Phase 3 handoff)
# =============================================================================


def _validate_transcript_bundle_shape(bundle: dict) -> None:
    """Strict allowlist + sanitize. Used both inbound (worker fetch) and outbound
    (worker register) so a malformed bundle in either direction fails-closed."""
    if not isinstance(bundle, dict):
        raise SecretBundleError("transcript bundle must be a JSON object")
    extra = set(bundle.keys()) - ALLOWED_TRANSCRIPT_TOP_KEYS
    if extra:
        raise SecretBundleError(f"transcript bundle has disallowed top-level keys: {extra}")
    if not isinstance(bundle.get("session_id"), str):
        raise SecretBundleError("transcript bundle.session_id must be a string")
    try:
        UUID(bundle["session_id"])
    except (ValueError, KeyError):
        raise SecretBundleError("transcript bundle.session_id must be a UUID")
    # Either top_level + subagents, OR a storage_ref (Storage-backed large bundle).
    if "storage_ref" in bundle:
        ref = bundle["storage_ref"]
        if not isinstance(ref, dict) or not isinstance(ref.get("storage_key"), str):
            raise SecretBundleError(
                "transcript bundle.storage_ref must be {storage_key: str, ...}"
            )
        return
    if not isinstance(bundle.get("top_level"), str):
        raise SecretBundleError(
            "transcript bundle.top_level must be a string (the JSONL contents)"
        )
    subs = bundle.get("subagents") or {}
    if not isinstance(subs, dict):
        raise SecretBundleError(
            "transcript bundle.subagents must be an object {filename: jsonl-text}"
        )
    if len(subs) > MAX_SUBAGENT_FILES:
        raise SecretBundleError(
            f"transcript bundle has too many subagent files: {len(subs)}"
        )
    for fname, content in subs.items():
        if not SUBAGENT_FILENAME_RE.match(fname):
            raise SecretBundleError(
                f"transcript bundle subagent filename invalid: {fname!r}"
            )
        if len(fname) > 200:
            raise SecretBundleError(
                f"transcript bundle subagent filename too long: {fname[:50]}..."
            )
        if "\x00" in fname or "\n" in fname:
            raise SecretBundleError(
                f"transcript bundle subagent filename has control bytes: {fname!r}"
            )
        if not isinstance(content, str):
            raise SecretBundleError(
                f"transcript bundle subagent {fname} content must be string"
            )
        if len(content.encode("utf-8")) > MAX_SUBAGENT_FILE_BYTES:
            raise SecretBundleError(f"transcript bundle subagent {fname} too large")


def fetch_transcript_bundle(client, cfg, bundle_id) -> dict:
    """Worker-side fetch via the SECURITY DEFINER bridge RPC. May resolve a Storage reference."""
    try:
        raw = client.rpc(
            "dispatch_fetch_transcript_bundle", {"p_id": str(bundle_id)},
        )
    except Exception as e:
        raise SecretBundleError(f"transcript bundle {bundle_id} not found: {e}") from None
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if isinstance(raw, dict):
        raw = next(iter(raw.values()), None)
    if not raw:
        raise SecretBundleError(f"transcript bundle {bundle_id} not found")
    raw_size = len(raw)
    handoff_cfg = cfg.dispatch.handoff
    max_bytes = (
        handoff_cfg.max_transcript_bundle_bytes if handoff_cfg else 10 * 1024 * 1024
    )
    if raw_size > max_bytes:
        raise SecretBundleError(f"transcript bundle exceeds max bytes ({raw_size})")
    try:
        bundle = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as e:
        raise SecretBundleError(f"transcript bundle {bundle_id} not valid JSON: {e}") from e
    _validate_transcript_bundle_shape(bundle)
    if "storage_ref" in bundle:
        bundle = _resolve_storage_bundle(cfg, bundle["storage_ref"])
        if "storage_ref" in bundle:
            raise SecretBundleError("nested storage_ref in resolved transcript bundle")
        _validate_transcript_bundle_shape(bundle)
    return bundle


def _resolve_storage_bundle(cfg, storage_ref: dict) -> dict:
    """Download a Storage-backed bundle and return the inflated JSON."""
    base = cfg.db.url.rstrip("/")
    if base.endswith("/rest/v1"):
        base = base[: -len("/rest/v1")]
    key = storage_ref["storage_key"]
    bucket = cfg.dispatch.log_storage.bucket
    with httpx.Client(timeout=30) as h:
        r = h.get(
            f"{base}/storage/v1/object/{bucket}/{key}",
            headers={"Authorization": f"Bearer {cfg.db.service_key}"},
        )
        r.raise_for_status()
    decompressed = gzip.decompress(r.content).decode("utf-8")
    return json.loads(decompressed)


def register_transcript_bundle(client, cfg, payload: dict) -> str:
    """Worker-side register of an OUTBOUND bundle.

    Validates shape, picks Vault-inline OR Storage-backed path based on serialized size,
    returns the Vault uuid suitable for ``jobs.final_transcript_bundle_id``.
    """
    _validate_transcript_bundle_shape(payload)
    handoff_cfg = cfg.dispatch.handoff
    inline_cap = handoff_cfg.vault_inline_cap_bytes if handoff_cfg else 512 * 1024
    max_total = (
        handoff_cfg.max_transcript_bundle_bytes if handoff_cfg else 10 * 1024 * 1024
    )

    serialized = json.dumps(payload)
    serialized_bytes = serialized.encode("utf-8")

    if len(serialized_bytes) <= inline_cap:
        rows = client.rpc("dispatch_register_transcript_bundle", {"p_secret": payload})
        return _extract_uuid(rows)

    if len(serialized_bytes) > max_total:
        raise SecretBundleError(
            f"outbound transcript bundle is {len(serialized_bytes)} bytes; "
            f"exceeds max {max_total}"
        )
    compressed = gzip.compress(serialized_bytes)
    key = f"transcripts/{payload['session_id']}-{int(time.time())}.json.gz"
    base = cfg.db.url.rstrip("/")
    if base.endswith("/rest/v1"):
        base = base[: -len("/rest/v1")]
    bucket = cfg.dispatch.log_storage.bucket
    with httpx.Client(timeout=30) as h:
        r = h.put(
            f"{base}/storage/v1/object/{bucket}/{key}",
            headers={
                "Authorization": f"Bearer {cfg.db.service_key}",
                "x-upsert": "true",
                "Content-Type": "application/gzip",
            },
            content=compressed,
        )
        r.raise_for_status()
    ref_payload = {
        "session_id": payload["session_id"],
        "storage_ref": {
            "storage_key": key,
            "bucket": bucket,
            "compressed_bytes": len(compressed),
            "uncompressed_bytes": len(serialized_bytes),
        },
    }
    rows = client.rpc("dispatch_register_transcript_bundle", {"p_secret": ref_payload})
    return _extract_uuid(rows)


def delete_transcript_bundle(client, cfg, bundle_id) -> None:
    """Best-effort delete via SECURITY DEFINER RPC. Gated by the handoff-specific flag,
    NOT the MCP-bundle flag (transcripts and MCP bundles have independent retention)."""
    if not (cfg.dispatch.handoff and cfg.dispatch.handoff.delete_inbound_on_completion):
        from worker.observability.events import DISPATCH_BUNDLE_RETAINED, emit
        emit(
            DISPATCH_BUNDLE_RETAINED,
            bundle_id=str(bundle_id),
            kind="transcript",
            reason="handoff.delete_inbound_on_completion=false",
        )
        return
    try:
        client.rpc("dispatch_delete_transcript_bundle", {"p_id": str(bundle_id)})
    except Exception as e:
        from worker.observability.events import POLL_LOOP_ERROR, emit
        emit(POLL_LOOP_ERROR, error=f"vault transcript delete failed for {bundle_id}: {e}")


def _extract_uuid(rows) -> str:
    """Unwrap PostgREST RPC return shape ``[{"dispatch_register_*": "<uuid>"}]``."""
    if not rows:
        raise SecretBundleError("RPC returned no id")
    val = rows[0]
    if isinstance(val, dict):
        return next(iter(val.values()))
    return str(val)
