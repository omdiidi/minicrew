"""Mocked end-to-end smoke test.

Drives one full poll cycle — claim -> launch -> result -> completion — against:
  * httpx.MockTransport for the PostgREST client
  * a fake Platform whose `launch_session` writes result.json immediately
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

    # POST /rest/v1/rpc/claim_next_job_with_cap — atomic claim RPC (Phase 2a). Returns the
    # claimed row the first time, empty after.
    if method == "POST" and path.endswith("/rest/v1/rpc/claim_next_job_with_cap"):
        if not TRACKER.claimed_job_seen:
            TRACKER.claimed_job_seen = True
            TRACKER.claim_patch_count += 1
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "job-1",
                        "job_type": "summarize",
                        "status": "running",
                        "worker_id": "mock-worker",
                        "payload": {"text": "hello world"},
                        "priority": 0,
                        "attempt_count": 0,
                        "expires_at": None,
                    }
                ],
            )
        return httpx.Response(200, json=[])

    # GET jobs (legacy path; startup_recovery + cancel-poll). Always empty in this smoke.
    if method == "GET" and path.endswith("/rest/v1/jobs"):
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


class _FakePlatform:
    """Minimal Platform stand-in.

    launch_session writes `result.json` into cwd so the watchdog's completion check
    trips on the very next poll. close_session is a no-op. preflight is a no-op.
    Service install/uninstall stubs are present so any code path that introspects
    the object doesn't AttributeError.
    """

    name = "fake"

    def preflight(self):
        return None

    def launch_session(self, cwd):
        from worker.platform.base import SessionHandle

        TRACKER.launcher_called = True
        result_path = Path(cwd) / "result.json"
        result_path.write_text(
            json.dumps({"summary": "mocked", "word_count_input": 5, "model_notes": ""}),
            encoding="utf-8",
        )
        return SessionHandle(kind="fake", data={"window_id": 99999})

    def close_session(self, handle):
        return None

    def install_service(self, **_kwargs):
        return None

    def uninstall_service(self, **_kwargs):
        return None

    def installed_instances(self):
        return []


FAKE_PLATFORM = _FakePlatform()


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

    # Force the platform factory to hand back our fake, regardless of host OS. This also
    # covers any code path that calls detect_platform directly.
    from worker import platform as platform_pkg

    platform_pkg.detect_platform = lambda _cfg: FAKE_PLATFORM

    # main_loop.py (pre-Phase-5) calls `orchestration.run(..., worker_id=...)` without a
    # platform kwarg. Intercept at the main_loop.orchestration seam and inject the fake.
    from worker.core import main_loop as main_loop_mod
    from worker.orchestration import run as real_orchestration_run

    class _OrchShim:
        @staticmethod
        def run(client, cfg, job, *, worker_id, platform=None):
            return real_orchestration_run(
                client, cfg, job, worker_id=worker_id, platform=platform or FAKE_PLATFORM
            )

    main_loop_mod.orchestration = _OrchShim

    # Cleanup helpers: make them no-ops so we don't touch ~/.claude during the test.
    from worker.terminal import shutdown as shutdown_mod

    shutdown_mod.cleanup_session_data = lambda *_a, **_k: None
    from worker.orchestration import single_terminal as st_mod

    st_mod.cleanup_session_data = lambda *_a, **_k: None

    # Tighten the watchdog's poll interval so the test doesn't hang 15s per tick.
    from worker.terminal import watchdog as watchdog_mod

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


# =============================================================================
# Wave 4B unit smokes — Phase 1/2/3 surface area
#
# Bare-assertion style matches the existing harness; each test is self-contained
# and prints "OK <name>" on success. main() collects failures rather than aborting
# on the first to surface as much info as possible per run.
# =============================================================================


def _make_stub_config():
    """Build a minimal in-memory Config sufficient for the renderers + queries tests.

    Renderers only call cfg.public_view(); queries only read cfg.db.jobs_table; the
    secret_bundle storage path reads cfg.db.url, cfg.db.service_key, cfg.dispatch.*.
    """
    from worker.config.models import (
        Config, DbConfig, DispatchConfig, GitHubAppConfig, HandoffConfig,
        LogStorageConfig, LoggingConfig, McpBundleConfig, ReaperConfig, WorkerConfig,
    )

    return Config(
        schema_version=1,
        db=DbConfig(
            jobs_table="jobs",
            workers_table="workers",
            events_table="events",
            url="http://localhost:54321/rest/v1",
            service_key="dummy-service-key",
            direct_url="postgresql://u:p@localhost:5432/d",
        ),
        worker=WorkerConfig(prefix="w", role="primary"),
        reaper=ReaperConfig(stale_threshold_seconds=600, interval_seconds=60, max_attempts=3),
        job_types={},
        logging=LoggingConfig(level="INFO", format="json", redact_env=[], sinks=[]),
        dispatch=DispatchConfig(
            github_app=GitHubAppConfig(
                app_id="1", private_key_env="GH_KEY", installation_id_env="GH_INST"
            ),
            log_storage=LogStorageConfig(bucket="minicrew-logs"),
            mcp_bundle=McpBundleConfig(),
            handoff=HandoffConfig(vault_inline_cap_bytes=1024, max_transcript_bundle_bytes=10 * 1024 * 1024),
        ),
    )


def test_partition_split_and_resolve() -> None:
    from worker.orchestration.partition import resolve_partition_input, split

    assert split([], 3, "chunks") == [[], [], []]
    assert split([1, 2, 3, 4, 5], 3, "chunks") == [[0, 1], [2, 3], [4]]
    assert split([1, 2, 3], 5, "chunks") == [[0], [1], [2], [], []]
    assert split([1, 2, 3], 2, "copies") == [[0, 1, 2], [0, 1, 2]]
    assert split([], 0, "chunks") == []

    assert resolve_partition_input({"documents": [1, 2]}, "documents") == [1, 2]
    assert resolve_partition_input({"a": {"b": [1]}}, "a.b") == [1]
    assert resolve_partition_input({}, "missing") == []
    print("OK partition.split + resolve_partition_input edge cases")


def test_render_builtin_ad_hoc() -> None:
    from worker.config.render import render_builtin_ad_hoc

    cfg = _make_stub_config()
    job = {"id": "job-xyz"}
    payload = {}
    common = dict(
        cfg=cfg,
        job=job,
        payload=payload,
        task="REVIEW the diff in src/foo.py",
        repo_path="/tmp/clone-abc",
        repo_url="https://github.com/example/repo",
        sha="deadbeef1234",
        result_filename="result.json",
    )

    no_push = render_builtin_ad_hoc(allow_code_push=False, **common)
    assert "REVIEW the diff in src/foo.py" in no_push
    assert "/tmp/clone-abc" in no_push
    assert "https://github.com/example/repo" in no_push
    assert "deadbeef1234" in no_push
    assert "result.json" in no_push
    # No-push branch verbiage:
    assert "NOT authorized" in no_push or "not authorized" in no_push.lower()
    assert "origin" in no_push and "removed" in no_push

    push = render_builtin_ad_hoc(allow_code_push=True, **common)
    assert "REVIEW the diff in src/foo.py" in push
    # Push branch verbiage:
    assert "result branch" in push
    assert "push" in push.lower()
    print("OK render_builtin_ad_hoc both branches")


def test_render_builtin_handoff() -> None:
    from worker.config.render import render_builtin_handoff

    cfg = _make_stub_config()
    job = {"id": "job-handoff"}
    payload = {}

    # Phase 3 review fix #27: housekeeping must appear in BOTH instruction branches.
    HOUSEKEEPING_MARKERS = (
        "summary",          # result file shape
        "exit_status",      # result file shape
        "_progress.jsonl",  # progress reporting
        "--print",          # exit semantics
    )

    for ui in (None, "finish the refactor"):
        for allow_push in (False, True):
            out = render_builtin_handoff(
                cfg=cfg,
                job=job,
                payload=payload,
                user_instruction=ui,
                allow_code_push=allow_push,
                result_filename="result.json",
                job_id="job-handoff",
            )
            assert "result.json" in out, f"result_filename missing (ui={ui!r}, push={allow_push})"
            for marker in HOUSEKEEPING_MARKERS:
                assert marker in out, (
                    f"missing housekeeping marker {marker!r} for ui={ui!r}, push={allow_push}"
                )
            if allow_push:
                assert "minicrew/result/" in out, "push branch should mention result branch"
                assert "code_changed" in out
            else:
                assert "origin" in out and "removed" in out, "no-push branch must mention origin removal"
            if ui is not None:
                assert "finish the refactor" in out
    print("OK render_builtin_handoff both instruction branches x both push branches")


def test_result_validation() -> None:
    from worker.config.result_validation import validate

    assert validate({"x": 1}, None).ok is True
    assert validate({"raw": "hello"}, None).ok is True

    bad = validate({"raw": "hello"}, {"type": "object", "required": ["x"]})
    assert bad.ok is False
    assert bad.error and "non-JSON" in bad.error

    assert validate({"x": 1}, {"type": "object", "required": ["x"]}).ok is True

    missing = validate({}, {"type": "object", "required": ["x"]})
    assert missing.ok is False
    assert missing.error
    print("OK ResultRead validation 5-case matrix")


def test_transcript_bundle_shape_allowlist() -> None:
    from worker.integrations.secret_bundle import (
        SUBAGENT_FILENAME_RE,
        SecretBundleError,
        _validate_transcript_bundle_shape,
    )

    valid_uuid = "11111111-2222-3333-4444-555555555555"

    # Sanity on regex itself.
    assert SUBAGENT_FILENAME_RE.match("abc-DEF_123.jsonl")
    assert not SUBAGENT_FILENAME_RE.match("../escape.jsonl")
    # \A...\Z (not ^...$) so trailing newline is rejected.
    assert not SUBAGENT_FILENAME_RE.match("foo.jsonl\n")

    # Valid bundle: no raise.
    _validate_transcript_bundle_shape(
        {"session_id": valid_uuid, "top_level": "{}", "subagents": {}}
    )

    def expect_raise(bundle, label):
        try:
            _validate_transcript_bundle_shape(bundle)
        except SecretBundleError:
            return
        raise AssertionError(f"expected SecretBundleError for: {label}")

    expect_raise(
        {"session_id": valid_uuid, "top_level": "{}", "evil": "x"},
        "extra top-level key",
    )
    expect_raise(
        {"session_id": "not-a-uuid", "top_level": "{}", "subagents": {}},
        "non-UUID session_id",
    )
    expect_raise(
        {"session_id": valid_uuid, "top_level": "{}", "subagents": {"../escape.jsonl": "x"}},
        "path-traversal subagent name",
    )
    expect_raise(
        {"session_id": valid_uuid, "top_level": "{}", "subagents": {"foo\x00bar.jsonl": "x"}},
        "NUL-byte filename",
    )
    expect_raise(
        {"session_id": valid_uuid, "top_level": "{}", "subagents": {".hidden": "x"}},
        "filename without .jsonl",
    )
    expect_raise(
        {"session_id": valid_uuid, "top_level": "{}", "subagents": {"foo.jsonl\n": "x"}},
        "trailing newline in filename",
    )
    expect_raise(
        {
            "session_id": valid_uuid,
            "top_level": "{}",
            "subagents": {("A" * 250 + ".jsonl"): "x"},
        },
        "filename over 200-char length cap",
    )

    # Too many files (>64).
    too_many = {f"f{i}.jsonl": "x" for i in range(65)}
    expect_raise(
        {"session_id": valid_uuid, "top_level": "{}", "subagents": too_many},
        "too many subagent files",
    )

    # Oversize file (>5MB).
    expect_raise(
        {
            "session_id": valid_uuid,
            "top_level": "{}",
            "subagents": {"big.jsonl": "x" * (5 * 1024 * 1024 + 1)},
        },
        "oversize subagent file",
    )

    # Storage-ref bundle: no raise.
    _validate_transcript_bundle_shape(
        {"session_id": valid_uuid, "storage_ref": {"storage_key": "transcripts/foo"}}
    )
    print("OK _validate_transcript_bundle_shape allowlist")


class _FakePostgrest:
    """Records every patch() call. Returns one dummy row to mimic a successful update."""

    def __init__(self):
        self.patches: list[dict] = []

    def patch(self, table, data, **filters):
        self.patches.append({"table": table, "data": data, "filters": filters})
        return [{"id": filters.get("id", "x")}]


def test_set_status_cancelled_filter() -> None:
    from worker.db.queries import set_status_cancelled

    cfg = _make_stub_config()
    fc = _FakePostgrest()
    ok = set_status_cancelled(fc, cfg, "job-1", "worker-1")
    assert ok is True
    assert len(fc.patches) == 1
    call = fc.patches[0]
    assert call["table"] == "jobs"
    assert call["filters"].get("status") == "running", (
        "status='running' filter is required to prevent completion races"
    )
    assert call["filters"].get("id") == "job-1"
    assert call["filters"].get("worker_id") == "worker-1"
    assert call["data"]["status"] == "cancelled"
    print("OK set_status_cancelled patches with status='running' filter")


def test_write_progress_and_final_bundle_filter() -> None:
    from worker.db.queries import write_final_transcript_bundle_id, write_progress

    cfg = _make_stub_config()

    fc = _FakePostgrest()
    write_progress(fc, cfg, "job-1", "worker-1", {"phase": "x"})
    assert fc.patches[-1]["filters"].get("status") == "running"
    assert fc.patches[-1]["data"] == {"progress": {"phase": "x"}}

    fc2 = _FakePostgrest()
    write_final_transcript_bundle_id(fc2, cfg, "job-1", "worker-1", "bundle-uuid-9")
    assert fc2.patches[-1]["filters"].get("status") == "running"
    assert fc2.patches[-1]["data"] == {"final_transcript_bundle_id": "bundle-uuid-9"}
    print("OK write_progress + write_final_transcript_bundle_id filter on status='running'")


def test_cancel_checkpoint_simulation() -> None:
    from worker.core import state

    # Reset to a clean slate (state is module-global).
    state.set_current_job(None)
    state.set_current_job("job-1")
    state.set_cancel_requested("job-1")
    assert state.is_cancel_requested("job-1") is True
    assert state.is_cancel_requested("other") is False

    # S11: clearing the current job slot must wipe any pending cancel flag.
    state.set_current_job(None)
    assert state.is_cancel_requested("job-1") is False
    print("OK cancel checkpoint state transitions (set + clear-on-job-end)")


def test_register_transcript_bundle_storage_fallback() -> None:
    """Small bundle -> RPC inline. Large bundle -> Storage PUT + RPC with storage_ref only."""
    import httpx as _httpx

    from worker.integrations import secret_bundle as sb_mod

    cfg = _make_stub_config()
    valid_uuid = "11111111-2222-3333-4444-555555555555"

    # ---- small bundle: inline ----
    rpc_calls: list[dict] = []

    class _RpcRecorder:
        def rpc(self, name, params):
            rpc_calls.append({"name": name, "params": params})
            return [{name: "vault-uuid-small"}]

    small = {"session_id": valid_uuid, "top_level": "{}", "subagents": {}}
    bid_small = sb_mod.register_transcript_bundle(_RpcRecorder(), cfg, small)
    assert bid_small == "vault-uuid-small"
    assert len(rpc_calls) == 1
    inline_payload = rpc_calls[0]["params"]["p_secret"]
    assert "storage_ref" not in inline_payload
    assert inline_payload["top_level"] == "{}"

    # ---- large bundle: Storage PUT then RPC with storage_ref only ----
    rpc_calls2: list[dict] = []
    put_calls: list[dict] = []

    class _RpcRecorder2:
        def rpc(self, name, params):
            rpc_calls2.append({"name": name, "params": params})
            return [{name: "vault-uuid-large"}]

    class _FakeResp:
        def raise_for_status(self):
            return None

    class _FakeHttpx:
        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def put(self, url, headers=None, content=None):
            put_calls.append({"url": url, "headers": headers, "content_len": len(content)})
            return _FakeResp()

    # Patch httpx.Client used inside the storage path.
    orig_client = sb_mod.httpx.Client
    sb_mod.httpx.Client = _FakeHttpx
    try:
        # Build a bundle that exceeds the 1024-byte inline cap configured in _make_stub_config.
        big_top = "x" * 4096
        large = {"session_id": valid_uuid, "top_level": big_top, "subagents": {}}
        bid_large = sb_mod.register_transcript_bundle(_RpcRecorder2(), cfg, large)
    finally:
        sb_mod.httpx.Client = orig_client

    assert bid_large == "vault-uuid-large"
    assert len(put_calls) == 1, "expected one Storage PUT for oversized bundle"
    assert "/storage/v1/object/minicrew-logs/transcripts/" in put_calls[0]["url"]
    assert len(rpc_calls2) == 1
    ref_payload = rpc_calls2[0]["params"]["p_secret"]
    assert "storage_ref" in ref_payload
    assert "top_level" not in ref_payload, "RPC payload must omit top_level when using storage_ref"
    assert ref_payload["storage_ref"]["bucket"] == "minicrew-logs"
    print("OK register_transcript_bundle inline-vs-storage dispatch")


_UNIT_TESTS = [
    test_partition_split_and_resolve,
    test_render_builtin_ad_hoc,
    test_render_builtin_handoff,
    test_result_validation,
    test_transcript_bundle_shape_allowlist,
    test_set_status_cancelled_filter,
    test_write_progress_and_final_bundle_filter,
    test_cancel_checkpoint_simulation,
    test_register_transcript_bundle_storage_fallback,
]


def run_unit_tests() -> int:
    failures = 0
    for fn in _UNIT_TESTS:
        try:
            fn()
        except Exception as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e!r}", file=sys.stderr)
    return failures


if __name__ == "__main__":
    # Order matters: the integration smoke patches modules at import-time via
    # _install_mocks(); importing worker.db.queries / worker.integrations.* in the
    # unit tests first captures the un-patched objects and breaks the patches.
    # Run integration first, units second.
    integration_rc = main()
    unit_failures = run_unit_tests()
    if unit_failures:
        print(f"FAIL: {unit_failures} unit smoke(s) failed", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(integration_rc)
