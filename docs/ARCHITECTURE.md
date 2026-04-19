# Architecture

## For LLMs

This file describes engine internals and runtime topology for minicrew. Load-bearing invariants:
the atomic-claim UPDATE with WHERE status='pending' is the sole coordination primitive for claim;
the reaper must hold `pg_try_advisory_xact_lock` before writing; heartbeats upsert on conflict id;
worker id format is `<prefix>-<hostname>-<instance>`. Do not change these without a schema
migration plan. The file layout section mirrors the on-disk tree; keep it in sync when modules move.

## The two-surface product

minicrew ships two inseparable surfaces. The **engine** is a Python daemon that polls a Supabase
job queue, claims jobs atomically, launches a visible macOS Terminal.app window per job, and
records results back to the database. The **skills** are Claude Code markdown commands installed
into `~/.claude/commands/minicrew/` on each host. After the initial bootstrap walk-through in
`SETUP.md`, every subsequent user action (adding a worker, scaffolding a consumer project, tuning
a job type, checking status, tearing down) happens through a conversational skill. The user never
memorizes a bash command.

## Runtime topology

A deployment is one or more Mac Minis. Each machine runs one to five worker instances supervised
by launchd. Each instance is an independent `python -m worker` process with a stable identifier
`<prefix>-<hostname>-<instance>` (for example `worker-minicrew-mini-1`). Instances do not
communicate with one another; the Postgres database is the sole coordination layer. Primary
machines poll every five seconds; secondary machines poll every fifteen. There is no leader, no
broadcast channel, and no discovery protocol. A machine goes online by registering a heartbeat
row and goes offline by missing heartbeats long enough for the reaper to mark it.

## The job lifecycle

A job begins life when a consumer inserts a row into the `jobs` table with `status='pending'`.
The next worker to claim it transitions the row to `status='running'` with a stamped
`worker_id`, `claimed_at`, and `worker_version`. When the orchestrator launches the terminal
session, `started_at` is written. On completion, the worker writes `result` (JSONB) and sets
`status='completed'` with `completed_at`. On failure, `status='error'` and `error_message` are
set. If the worker dies or the idle watchdog kills the session, the reaper (or startup recovery
on the next boot) requeues the row back to `pending` and increments `attempt_count`; past
`max_attempts` the row becomes `failed_permanent` with a descriptive `error_message`.

## Atomic claim

The claim is a two-step dance. First, the worker reads one row with `status='pending'` ordered
by priority and created_at. Then it issues `UPDATE jobs SET status='running', worker_id=..., ...
WHERE id=? AND status='pending'`. PostgREST returns the updated rows; the row count is the
winner signal. If two workers see the same pending row simultaneously, only one update matches
the `status='pending'` predicate; the loser gets an empty array back and loops. The database's
MVCC row lock during the UPDATE provides the mutual exclusion — no explicit locking is needed
on the client side.

## Priority model

There are two priority tiers, controlled entirely by polling interval: primary (5 seconds) and
secondary (15 seconds). Over time, primary workers claim roughly three times as many jobs as
secondaries. This is not deterministic — a secondary occasionally beats a primary on any given
job — but it is predictable in aggregate and requires zero coordination. For v1 this is
sufficient; deterministic priority windows are intentionally deferred.

## Heartbeats

Every worker writes a row to the `workers` table every thirty seconds. The write is an upsert
keyed on `id` (`on_conflict=id`), updating `last_heartbeat`, `status` (one of `idle`, `busy`,
`offline`), and `version`. The reaper treats any row whose `last_heartbeat` is older than the
configured stale threshold (default 120 seconds) as a crashed worker and requeues its jobs.

## Reaper (opportunistic)

Every worker runs a reaper thread. The thread opens a direct Postgres connection (not PostgREST),
begins a transaction, calls `pg_try_advisory_xact_lock(REAPER_LOCK_KEY)`, and inspects the
return value. If the lock was not acquired, the thread releases by committing and sleeps.
If the lock was acquired, the thread queries for stale workers, marks them offline, and calls
the `requeue_stale_jobs_for_worker` RPC for each. On commit, the advisory lock releases
automatically. Exactly one worker in the fleet runs the reaper in any given cycle; no writes
happen without the lock. No heartbeat, no election, no coordination protocol.

## Single-worker liveness caveat

A fleet with one worker cannot self-heal: if that single worker becomes the stuck one, no other
worker exists to reap it. Launchd `KeepAlive` restarts the process if it exits; the idle
watchdog kills hung terminal sessions before they consume the full job timeout. These two
mechanisms cover the single-worker case. Any fleet with two or more instances — on the same
machine or across machines — self-heals through the reaper.

## Graceful shutdown

On SIGTERM or SIGINT the worker sets a shared `shutdown_requested` flag. If a job is in flight,
the main loop releases the claim by updating the row back to `status='pending'`, clearing
`worker_id` and `started_at`. The worker marks its own `workers` row `status='offline'` and
exits. No in-flight work is lost; the next worker to poll picks it up. SIGHUP is not handled in
v1 — configuration reload requires a full restart.

## Idle watchdog

Each terminal session has a dedicated watchdog thread. Every few seconds it performs a recursive
`os.walk` over the session's cwd, noting the most recent file modification time across all
non-dot, non-underscore files. Two kill conditions fire. First: if elapsed run time exceeds 25
minutes and no file has been modified in 25 minutes and the expected result file is missing,
the session is killed and the job returns a timeout error. Second: if the result file exists
but has not been modified in 15 minutes, the session is killed (this catches hung
post-processing). The 25-minute guard on the first condition prevents merge sessions — which
start from an empty cwd — from being killed prematurely.

## Startup recovery

On startup the worker queries for any rows where `status='running'` and `worker_id` matches its
own identifier. These can only exist if the worker crashed without graceful shutdown. Each such
row is requeued to `pending` with `worker_id=null` and `started_at=null`, and a
`startup_requeued` event is emitted. The worker then enters its normal poll loop.

## File layout

```
worker/
  __init__.py              # exports __version__
  __main__.py              # entry point: python -m worker
  cli.py                   # arg parsing (--instance, --role, --status, --reload)
  core/
    main_loop.py           # the poll-claim-orchestrate-sleep loop
    claim.py               # claim_next_job wrapper
    heartbeat.py           # 30-second upsert to workers table
    reaper.py              # reaper thread + run_one_reaper_cycle
    signals.py             # SIGTERM/SIGINT handlers
    state.py               # current_job_id, shutdown_requested flag
    startup_recovery.py    # requeue own jobs left as running on boot
  terminal/
    launcher.py            # osascript window open + pre-trust + _run.sh render
    watchdog.py            # recursive mtime walk kill conditions
    shutdown.py            # /exit + window close + cwd cleanup
  orchestration/
    __init__.py            # dispatch on mode
    single_terminal.py     # default mode: one job, one window, one result
    fan_out.py             # N parallel group windows + 1 merge window
  config/
    loader.py              # YAML + env interpolation + JSON Schema validation
    models.py              # Pydantic-style Config, JobType, ReaperConfig
    render.py              # Jinja env with StrictUndefined + finalize callback
    payload_schema.py      # optional payload.schema.json validator
  db/
    client.py              # PostgREST wrapper with eq.-autoprefix
    queries.py             # claim, update, write_result, mark_offline
    advisory_lock.py       # reaper_lock context manager over psycopg
  observability/
    events.py              # event constants, JSON formatter, redaction filter
    sinks.py               # FileSink with daily rotation; v2 sinks reject
    setup.py               # wire stdlib logging into sinks
  utils/
    version.py             # read VERSION file
    paths.py               # absolute-path helpers and trust-dialog writer
    launchd.py             # plist render + install/uninstall
    db_url.py              # validate direct URL; reject pooler hosts
```
