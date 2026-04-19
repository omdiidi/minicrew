"""Postgres transaction-level advisory lock for single-reaper guarantee across the fleet."""
from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

# Stable bigint derived from blake2b(b"minicrew-reaper", digest_size=8), signed interpretation.
# Decimal: 3093725731989265026  |  Hex: 0x2aef1f2977b49a82  (computed at import time; pinned for DBA lookup).
REAPER_LOCK_KEY: int = int.from_bytes(
    hashlib.blake2b(b"minicrew-reaper", digest_size=8).digest(),
    "big",
    signed=True,
)


@contextmanager
def reaper_lock(db_url: str) -> Iterator[tuple[bool, psycopg.Connection]]:
    """Open a direct postgres connection, try the xact advisory lock inside BEGIN.

    Yields (acquired, conn). When acquired=True the caller may execute queries on
    `conn` — they run inside the still-open transaction so any writes are atomic with
    the lock. On context exit the transaction commits (or rolls back on exception),
    which releases the xact-scoped lock.
    """
    conn = psycopg.connect(db_url, autocommit=False)
    try:
        with conn.cursor() as cur:
            # Explicit BEGIN is required so pg_try_advisory_xact_lock binds to a real transaction.
            cur.execute("BEGIN")
            cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (REAPER_LOCK_KEY,))
            row = cur.fetchone()
            acquired = bool(row[0]) if row else False
        try:
            yield acquired, conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
