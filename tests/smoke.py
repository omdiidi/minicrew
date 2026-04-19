"""Mocked end-to-end smoke test.

Drives one full poll cycle — claim -> launch -> result -> completion — against:
  * httpx.MockTransport for the PostgREST client
  * a stub `launch_terminal_window` that writes result.json immediately
  * a stub advisory lock that never acquires (reaper is inert)
  * no real heartbeat, no real subprocess

Exits 0 on success, non-zero on failure. No test framework.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# psycopg import happens eagerly inside worker.db.advisory_lock and worker.core.reaper.
# We never let either run real queries in this smoke test, so a minimal stub is sufficient
# when the dev environment doesn't have psycopg installed (CI installs it; some devs won't).
try:
    import psycopg  # noqa: F401
except ImportError:
    import types

    _stub = types.ModuleType("psycopg")

    class _StubConn:  # pragma: no cover - only hit if reaper thread somehow runs
        pass

    _stub.Connection = _StubConn  # type: ignore[attr-defined]
    _stub.connect = lambda *_a, **_k: _StubConn()  # type: ignore[attr-defined]
    _rows = types.ModuleType("psycopg.rows")
    _rows.dict_row = None  # type: ignore[attr-defined]
    sys.modules["psycopg"] = _stub
    sys.modules["psycopg.rows"] = _rows

# --- Env setup BEFORE any worker import ---
os.environ["MINICREW_CONFIG_PATH"] = str(REPO_ROOT / "examples" / "minimal")
os.environ["SUPABASE_URL"] = "http://localhost:54321"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "dummy-service-key"
os.environ["SUPABASE_DB_URL"] = "postgresql://user:pass@localhost:5432/postgres"


# --- Tracking state ---
class Tracker:
    claim_patch_count = 0
    result_write_count = 0
    claimed_job_seen = False
    completed_job_seen = False
    launcher_called = False


TRACKER = Tracker()


# --- MockTransport handler ---
def _mock_handler(request):
    import httpx

    method = request.method
    path = request.url.path
    qs = dict(request.url.params)

    # GET jobs?status=eq.pending -> return one job the first time, empty after.
    if method == "GET" and path.endswith("/rest/v1/jobs"):
        if qs.get("status") == "eq.pending":
            if TRACKER.claim_patch_count == 0 and not TRACKER.claimed_job_seen:
                TRACKER.claimed_job_seen = True
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": "job-1",
                            "job_type": "summarize",
                            "status": "pending",
                            "payload": {"text": "hello world"},
                            "priority": 0,
                            "attempt_count": 0,
                            "expires_at": None,
                        }
                    ],
                )
            return httpx.Response(200, json=[])
        # startup_recovery lookup (status=running, worker_id=<self>) — return empty
        return httpx.Response(200, json=[])

    # PATCH jobs — claim, started_at, or result write.
    if method == "PATCH" and path.endswith("/rest/v1/jobs"):
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        # Atomic claim is filtered by status=eq.pending.
        if qs.get("status") == "eq.pending":
            TRACKER.claim_patch_count += 1
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "job-1",
                        "job_type": "summarize",
                        "status": "running",
                        "worker_id": body.get("worker_id"),
                        "claimed_at": body.get("claimed_at"),
                        "payload": {"text": "hello world"},
                        "priority": 0,
                        "attempt_count": 0,
                        "expires_at": None,
                    }
                ],
            )
        # Plain update: could be started_at, status=completed with result, or error.
        if body.get("status") == "completed":
            TRACKER.result_write_count += 1
            TRACKER.completed_job_seen = True
        return httpx.Response(200, json=[{"id": "job-1", **body}])

    # POST workers — heartbeat upsert / mark offline.
    if method == "POST" and "/rest/v1/workers" in path:
        return httpx.Response(201, json=[{"id": "mock-worker", "status": "idle"}])

    # GET workers / worker_stats — return empty aggregates.
    if method == "GET" and "/rest/v1/worker_stats" in path:
        return httpx.Response(
            200,
            json=[{"queue_depth": 0, "running_count": 0, "recent_errors_1h": 0, "recent_failed_permanent_24h": 0}],
        )
    if method == "GET" and "/rest/v1/workers" in path:
        return httpx.Response(200, json=[])

    # Fallback: empty OK.
    return httpx.Response(200, json=[])


def _install_mocks() -> None:
    """Patch the modules used by the engine. Must happen before main_loop.run."""
    import httpx

    from worker.db import client as client_mod

    def patched_init(self, url, service_key, *, timeout=30.0):
        self._url = url.rstrip("/")
        self._headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        }
        transport = httpx.MockTransport(_mock_handler)
        self._client = httpx.Client(headers=self._headers, timeout=timeout, transport=transport)

    client_mod.PostgrestClient.__init__ = patched_init

    # Stub launch_terminal_window in launcher AND in single_terminal's bound reference.
    from worker.terminal import launcher as launcher_mod

    def fake_launch(cwd):
        TRACKER.launcher_called = True
        # Write the expected result file immediately.
        result_path = Path(cwd) / "result.json"
        result_path.write_text(
            json.dumps({"summary": "mocked", "word_count_input": 5, "model_notes": ""}),
            encoding="utf-8",
        )
        # Also bump the mtime so the stability check passes on the second tick.
        return 99999

    launcher_mod.launch_terminal_window = fake_launch
    # single_terminal imported the symbol by name at module load.
    from worker.orchestration import single_terminal as st_mod

    st_mod.launch_terminal_window = fake_launch

    # Shutdown helpers: make them no-ops so we don't call osascript.
    from worker.terminal import shutdown as shutdown_mod

    shutdown_mod.exit_claude_and_close_window = lambda *_a, **_k: None
    shutdown_mod.cleanup_session_data = lambda *_a, **_k: None
    st_mod.exit_claude_and_close_window = lambda *_a, **_k: None
    st_mod.cleanup_session_data = lambda *_a, **_k: None
    # watchdog imports exit_claude_and_close_window too; patch there as well.
    from worker.terminal import watchdog as watchdog_mod

    watchdog_mod.exit_claude_and_close_window = lambda *_a, **_k: None

    # Tighten the watchdog's poll interval so the test doesn't hang 15s per tick.
    _orig_wait = watchdog_mod.wait_for_completion

    def fast_wait(*args, **kwargs):
        kwargs["poll_interval"] = 1
        return _orig_wait(*args, **kwargs)

    watchdog_mod.wait_for_completion = fast_wait
    st_mod.wait_for_completion = fast_wait

    # Advisory lock: never acquired -> reaper loops harmlessly.
    from contextlib import contextmanager

    from worker.db import advisory_lock as lock_mod

    @contextmanager
    def fake_lock(db_url):
        yield (False, None)

    lock_mod.reaper_lock = fake_lock
    from worker.core import reaper as reaper_mod

    reaper_mod.reaper_lock = fake_lock

    # Heartbeat: no-op so it doesn't spam the mock transport with real threads.
    from worker.core import heartbeat as hb_mod

    def noop_start(*_a, **_k):
        t = threading.Thread(target=lambda: None, daemon=True)
        t.start()
        return t

    hb_mod.start = noop_start

    # db_url validator: accept our localhost string.
    from worker.utils import db_url as db_url_mod

    db_url_mod.assert_db_url_is_direct = lambda _url: None
    from worker.core import main_loop as main_loop_mod

    main_loop_mod.assert_db_url_is_direct = lambda _url: None

    # signal.signal() refuses to install outside the main thread; we're driving main_loop
    # from a worker thread, so neuter the installer. Shutdown is driven via state.request_shutdown.
    from worker.core import signals as signals_mod

    signals_mod.install = lambda: None
    main_loop_mod.signals.install = lambda: None


def _run_with_timeout() -> int:
    _install_mocks()

    from worker.core import main_loop, state

    opts = main_loop.RunOptions(instance=1, role="primary", poll_interval=1)
    rc_holder: dict = {}

    def target() -> None:
        try:
            rc_holder["rc"] = main_loop.run(opts)
        except Exception as e:
            rc_holder["exc"] = e

    t = threading.Thread(target=target, daemon=True, name="smoke-main")
    t.start()

    # Wait up to 25s for the completed job to be observed, then request shutdown.
    deadline = time.time() + 25
    while time.time() < deadline:
        if TRACKER.completed_job_seen:
            break
        time.sleep(0.2)

    state.request_shutdown()
    t.join(timeout=10)
    if t.is_alive():
        print("FAIL: main loop did not exit within 10s of shutdown request", file=sys.stderr)
        return 1
    if "exc" in rc_holder:
        print(f"FAIL: main loop raised: {rc_holder['exc']!r}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    # Absolute hard timeout — if anything hangs past 30s, abort.
    abort = threading.Event()

    def hard_timeout() -> None:
        if not abort.wait(30):
            print("FAIL: smoke test exceeded 30s hard timeout", file=sys.stderr)
            os._exit(1)

    watchdog = threading.Thread(target=hard_timeout, daemon=True)
    watchdog.start()

    try:
        rc = _run_with_timeout()
    finally:
        abort.set()

    if rc != 0:
        return rc

    # Assertions
    if not TRACKER.launcher_called:
        print("FAIL: fake launcher was never invoked", file=sys.stderr)
        return 1
    if TRACKER.claim_patch_count < 1:
        print("FAIL: claim PATCH was never called", file=sys.stderr)
        return 1
    if not TRACKER.completed_job_seen:
        print("FAIL: job never reached status=completed", file=sys.stderr)
        return 1

    print("OK smoke: claim+launch+result+complete exercised")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
