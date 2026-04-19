"""Validates SUPABASE_DB_URL is a direct (port 5432) connection, not a pooler."""
from __future__ import annotations

from urllib.parse import urlparse


class DbUrlError(ValueError):
    """Raised when a direct postgres URL is required but a pooler URL was given."""


def assert_db_url_is_direct(url: str) -> None:
    if not url:
        raise DbUrlError("SUPABASE_DB_URL is empty; advisory locks require a direct Postgres URL.")
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("postgresql", "postgres"):
        raise DbUrlError(
            f"SUPABASE_DB_URL must use scheme 'postgresql://' or 'postgres://' (got '{scheme}://'). "
            "See docs/SUPABASE-SCHEMA.md for the correct connection string format."
        )
    host = (parsed.hostname or "").lower()
    # Supabase pooler hostnames include 'pooler' in the subdomain.
    # Pooler also defaults to port 6543 (transaction pool) or 5432-but-pooled; reject on hostname.
    if "pooler" in host:
        raise DbUrlError(
            f"SUPABASE_DB_URL points at the pooler ({host}); advisory locks require the direct connection. "
            "See docs/SUPABASE-SCHEMA.md for retrieval steps."
        )
    if parsed.port is not None and parsed.port != 5432:
        raise DbUrlError(
            f"SUPABASE_DB_URL uses port {parsed.port}; advisory locks require port 5432 (the direct connection). "
            "See docs/SUPABASE-SCHEMA.md for retrieval steps."
        )
