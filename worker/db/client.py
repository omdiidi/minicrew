"""Thin httpx-based PostgREST wrapper with auto eq.-prefix + operator passthrough."""
from __future__ import annotations

from typing import Any

import httpx

# PostgREST operator prefixes that must be passed through untouched.
# Kept in sync with plan section 11; expanded beyond the reference to cover the full v12 operator set.
_OPERATOR_PREFIXES: tuple[str, ...] = (
    "eq.",
    "neq.",
    "gt.",
    "gte.",
    "lt.",
    "lte.",
    "like.",
    "ilike.",
    "in.",
    "is.",
    "cs.",
    "cd.",
    "ov.",
    "sl.",
    "sr.",
    "nxr.",
    "nxl.",
    "adj.",
    "fts.",
    "plfts.",
    "phfts.",
    "wfts.",
    "not.",
)

# Control params are NOT auto-prefixed — they are PostgREST directives, not filters.
_CONTROL_PARAMS = {"select", "order", "limit", "offset", "on_conflict"}

BATCH_SIZE = 100


def _process_filter(key: str, value: Any) -> tuple[str, str]:
    if key in _CONTROL_PARAMS:
        return key, str(value)
    sval = str(value)
    for prefix in _OPERATOR_PREFIXES:
        if sval.startswith(prefix):
            return key, sval
    return key, f"eq.{sval}"


class PostgrestClient:
    """Stateless PostgREST wrapper. One instance per worker process."""

    def __init__(self, url: str, service_key: str, *, timeout: float = 30.0) -> None:
        self._url = url.rstrip("/")
        self._headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        }
        self._client = httpx.Client(headers=self._headers, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def get(self, table: str, **params: Any) -> list[dict]:
        processed = dict(_process_filter(k, v) for k, v in params.items())
        resp = self._client.get(f"{self._url}/rest/v1/{table}", params=processed)
        resp.raise_for_status()
        return resp.json()

    def post(self, table: str, data: dict | list[dict]) -> None:
        rows: list[dict] = [data] if isinstance(data, dict) else list(data)
        headers = {**self._headers, "Prefer": "return=representation"}
        for i in range(0, len(rows), BATCH_SIZE):
            resp = self._client.post(
                f"{self._url}/rest/v1/{table}",
                json=rows[i : i + BATCH_SIZE],
                headers=headers,
            )
            resp.raise_for_status()

    def upsert(self, table: str, data: dict | list[dict], on_conflict: str) -> None:
        headers = {**self._headers, "Prefer": "return=representation,resolution=merge-duplicates"}
        resp = self._client.post(
            f"{self._url}/rest/v1/{table}",
            json=data,
            headers=headers,
            params={"on_conflict": on_conflict},
        )
        resp.raise_for_status()

    def patch(self, table: str, data: dict, **filters: Any) -> list[dict]:
        headers = {**self._headers, "Prefer": "return=representation"}
        params = dict(_process_filter(k, v) for k, v in filters.items())
        resp = self._client.patch(
            f"{self._url}/rest/v1/{table}",
            json=data,
            headers=headers,
            params=params,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else []

    def rpc(self, function_name: str, params: dict) -> list[dict]:
        resp = self._client.post(
            f"{self._url}/rest/v1/rpc/{function_name}",
            json=params,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else []

    def delete(self, table: str, **filters: Any) -> None:
        headers = {**self._headers, "Prefer": "return=minimal"}
        params = dict(_process_filter(k, v) for k, v in filters.items())
        resp = self._client.delete(
            f"{self._url}/rest/v1/{table}",
            headers=headers,
            params=params,
        )
        resp.raise_for_status()
