"""Background log streamers shared by every dispatch orchestrator.

Two daemon threads, both gated behind ``cfg.dispatch is not None`` at the orchestrator
layer so pure-batch installs ship zero traffic to Storage / progress endpoints:

- ``ProgressTailer`` — tails ``<cwd>/_progress.jsonl``, parses the latest complete JSON
  line, PATCHes ``jobs.progress`` (filtered to ``status='running'`` server-side via
  ``write_progress``). Buffers between ticks so a half-written line never gets parsed.

- ``ChunkedLogStreamer`` — tails ``logs/jobs/<job_id>.log``, rotates new bytes into
  ≤chunk_bytes pieces uploaded as ``<bucket>/<prefix>/transcript.NNN.log``, maintains
  ``<bucket>/<prefix>/manifest.json``, and signs the manifest URL on the first
  successful upload (so ``caller_log_url`` becomes available mid-run).

Both use httpx (the project standard); both are best-effort and never raise into
the orchestrator. The orchestrator joins them with a short timeout BEFORE writing
the terminal job result so a late ``write_progress`` can't race the completion row.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx


MAX_PROGRESS_LINE_BYTES = 64 * 1024


class ProgressTailer(threading.Thread):
    """Tail ``<cwd>/_progress.jsonl`` and PATCH ``jobs.progress`` with the latest line.

    Buffer-between-ticks discipline: the file position only advances to the last
    complete-line boundary; partial trailing bytes carry into the next tick. Lines
    over MAX_PROGRESS_LINE_BYTES are dropped (defends against runaway prompt junk).
    Returns from ``run`` when ``write_progress`` reports the row is no longer ours
    or no longer running (PATCH affected zero rows).
    """

    def __init__(self, *, client, cfg, job_id, worker_id, cwd: Path, stop_event):
        super().__init__(daemon=True, name=f"minicrew-progress-{job_id[:8]}")
        self.client = client
        self.cfg = cfg
        self.job_id = job_id
        self.worker_id = worker_id
        self.cwd = cwd
        self.stop_event = stop_event
        self._buf = b""
        self._offset = 0

    def run(self) -> None:
        # Lazy import: write_progress lives in worker.db.queries which is added in Wave 2A.
        # Importing here also avoids any module-load-time circular with observability/events.
        from worker.db.queries import write_progress

        path = self.cwd / "_progress.jsonl"
        while not self.stop_event.is_set():
            if path.exists():
                try:
                    with path.open("rb") as f:
                        f.seek(self._offset)
                        chunk = f.read()
                        new_offset = f.tell()
                except OSError:
                    chunk = b""
                    new_offset = self._offset
                if chunk:
                    self._buf += chunk
                    last_complete = self._buf.rfind(b"\n")
                    if last_complete >= 0:
                        complete = self._buf[:last_complete]
                        self._buf = self._buf[last_complete + 1 :]
                        # Advance offset only to the last complete-line boundary.
                        self._offset = new_offset - len(self._buf)
                        last_line = None
                        for line in complete.split(b"\n"):
                            if not line.strip():
                                continue
                            if len(line) > MAX_PROGRESS_LINE_BYTES:
                                continue
                            try:
                                last_line = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                        if last_line is not None:
                            if not write_progress(
                                self.client, self.cfg, self.job_id,
                                self.worker_id, last_line,
                            ):
                                # Lost ownership or row no longer 'running' — stop tailing.
                                return
            self.stop_event.wait(timeout=3)


class ChunkedLogStreamer(threading.Thread):
    """Rotate fresh log bytes into Supabase Storage as numbered chunks + manifest.

    Each tick: read everything past ``_uploaded_offset``; split into ``chunk_bytes``
    pieces; PUT each as ``<bucket>/<prefix>/transcript.NNN.log``; rewrite
    ``<bucket>/<prefix>/manifest.json`` with the cumulative ``{chunks, total_bytes}``.
    On the first successful upload the manifest URL is signed (``expiresIn`` =
    ``retention_seconds``) and handed to ``on_first_upload`` so the orchestrator can
    PATCH ``caller_log_url`` mid-run.

    Best-effort: every IO is wrapped in a try/except so the streamer never raises.
    """

    def __init__(
        self,
        *,
        supabase_base_url: str,
        service_key: str,
        bucket: str,
        prefix: str,
        log_path: Path,
        chunk_bytes: int,
        interval: int,
        on_first_upload,
        stop_event,
        retention_seconds: int,
    ):
        super().__init__(daemon=True, name=f"minicrew-log-{prefix[:12]}")
        self.base = supabase_base_url.rstrip("/")
        if self.base.endswith("/rest/v1"):
            self.base = self.base[: -len("/rest/v1")]
        self.service_key = service_key
        self.bucket = bucket
        self.prefix = prefix
        self.log_path = log_path
        self.chunk_bytes = chunk_bytes
        self.interval = interval
        self.on_first_upload = on_first_upload
        self.stop_event = stop_event
        self.retention_seconds = retention_seconds
        self._http = httpx.Client(timeout=20)
        self._first_done = False
        self._uploaded_offset = 0
        self._chunk_idx = 0
        self._chunks: list[str] = []

    def _put_object(self, key: str, body: bytes) -> bool:
        try:
            r = self._http.put(
                f"{self.base}/storage/v1/object/{self.bucket}/{key}",
                headers={
                    "Authorization": f"Bearer {self.service_key}",
                    "x-upsert": "true",
                    "Content-Type": "text/plain",
                    "Cache-Control": "no-store",
                },
                content=body,
            )
        except httpx.HTTPError:
            return False
        return r.status_code in (200, 201)

    def _sign(self, key: str) -> str:
        r = self._http.post(
            f"{self.base}/storage/v1/object/sign/{self.bucket}/{key}",
            headers={"Authorization": f"Bearer {self.service_key}"},
            json={"expiresIn": max(60, self.retention_seconds)},
        )
        return f"{self.base}/storage/v1{r.json()['signedURL']}"

    def _flush_chunk(self) -> None:
        if not self.log_path.exists():
            return
        try:
            with self.log_path.open("rb") as f:
                f.seek(self._uploaded_offset)
                new = f.read()
        except OSError:
            return
        if not new:
            return
        for i in range(0, len(new), self.chunk_bytes):
            piece = new[i : i + self.chunk_bytes]
            key = f"{self.prefix}/transcript.{self._chunk_idx:03d}.log"
            if not self._put_object(key, piece):
                return
            self._chunks.append(key)
            self._chunk_idx += 1
        self._uploaded_offset += len(new)
        manifest = {"chunks": self._chunks, "total_bytes": self._uploaded_offset}
        self._put_object(
            f"{self.prefix}/manifest.json",
            json.dumps(manifest).encode(),
        )
        if not self._first_done:
            try:
                signed = self._sign(f"{self.prefix}/manifest.json")
                self.on_first_upload(signed)
                self._first_done = True
            except Exception:
                # Never let a sign-URL failure abort the streamer; we'll retry next tick.
                pass

    def run(self) -> None:
        try:
            while not self.stop_event.is_set():
                try:
                    self._flush_chunk()
                except Exception:
                    pass  # never raise from streamer
                self.stop_event.wait(timeout=self.interval)
            # Final flush on shutdown.
            try:
                self._flush_chunk()
            except Exception:
                pass
        finally:
            self._http.close()
