# Queueing, Priority, and Multi-Machine

## For LLMs

Describes how jobs are prioritized, how the reaper works, and how multiple machines coordinate.
Load-bearing invariants: `SUPABASE_DB_URL` must be the direct 5432 URL (pooler URLs are rejected
at startup); the reaper holds a `pg_try_advisory_xact_lock` and writes only if acquired; the
reaper requires at least two live workers to cover the case where one is stuck. Do not change
the advisory-lock key or the polling-interval tiers without updating `ARCHITECTURE.md` and
`schema/template.sql` simultaneously.

## Priority via polling interval

minicrew uses a two-tier priority scheme controlled entirely by polling interval. Primary
workers poll the `jobs` table every five seconds; secondary workers poll every fifteen. Over
time a primary claims roughly three times as many jobs as a secondary on the same queue depth.
This is not deterministic — a secondary sometimes wins a particular job — but it is predictable
in aggregate, requires zero coordination between workers, and survives all the usual failure
modes. Deterministic priority windows (secondary workers explicitly skipping jobs younger than
some threshold) are a v2 idea; polling-interval priority is the proven v1 approach.

## Multi-machine setup

Adding a second Mac Mini is a one-shot skill invocation. On the new machine, run
`/minicrew:add-machine`. The skill prints the exact sentence to paste into a Claude Code
session on that machine; the session clones the repo, runs `/minicrew:setup`, and the new
worker shows up in `workers` table heartbeats within thirty seconds. No inter-machine
communication exists and none is needed — every worker coordinates via the shared Postgres
database.

Mixed-OS fleets are supported. Macs and Linux Mints coordinate through the same Supabase
database via the shared `jobs` + `workers` tables; each worker process uses its own platform
backend (osascript + launchd on Mac, xfce4-terminal + systemd on Linux). No special
configuration is needed for heterogeneous fleets beyond ensuring each box meets its
OS-specific prerequisites — see `docs/LINUX.md` for the Mint deep-dive.

## Multi-instance per machine

Each Mac Mini supports 1..5 worker instances, supervised by launchd. Each instance is an
independent `python -m worker` process with a distinct `--instance N` argument. The setup
skill generates one plist per instance (`com.minicrew.worker.1`, `com.minicrew.worker.2`, ...)
under `~/Library/LaunchAgents/`. Instances on the same machine share no state; they claim
independently from the database just like instances on different machines would.

## The reaper in detail

The reaper runs on its own thread inside every worker process. This is deliberate: there is no
designated reaper node, so there is no single point of failure. Every cycle (default every 60
seconds) each worker opens a direct Postgres connection, begins a transaction, and calls
`pg_try_advisory_xact_lock(REAPER_LOCK_KEY)`. The key is a stable int64 derived from
`blake2b(b"minicrew-reaper", digest_size=8)`, so every worker in the fleet is bidding for the
same lock. Exactly one worker gets `true` back; every other worker gets `false` and goes back
to sleep. The winner queries `workers` for rows with `last_heartbeat` older than the stale
threshold (default 120 seconds), marks those workers `status='offline'`, and calls the
`requeue_stale_jobs_for_worker` RPC for each. The RPC increments `attempt_count` and
transitions the job back to `pending` — unless the next attempt would exceed `max_attempts`, in
which case the job moves to `failed_permanent` with a descriptive error message. `max_attempts`
is the total number of runs allowed, so `max_attempts=3` permits up to three attempts; the
third attempt's failure triggers the poison transition (the row never claims a fourth time).
This is the poison-pill protection: a job that crashes its retry budget is frozen for human
review rather than cycling forever.

## Pooler vs direct connection

The advisory lock is load-bearing. It must be on the **direct** Postgres connection (port
5432), not the pooler (port 6543). pgBouncer in transaction-pool mode — the default for
Supabase pooled connections — releases advisory locks at transaction boundaries the client
doesn't control, which makes the exactly-one guarantee unsound. `worker/utils/db_url.py`
inspects `SUPABASE_DB_URL` at startup and refuses to boot if the hostname contains `pooler`.
You can find the correct URL in the Supabase dashboard under Project Settings, Database,
Connection string, labelled "direct connection" and ending in `:5432/postgres`.

## Single-worker liveness

A fleet of exactly one worker on one Mac Mini cannot self-heal. If that worker is the one
stuck, no other worker exists to reap it; the reaper thread inside the stuck worker is also
stuck. Launchd's `KeepAlive` directive restarts the process if it exits, and the idle
watchdog kills hung terminal sessions before they consume the full job timeout, but neither of
those covers a worker that is deadlocked at the Python level without exiting. Any fleet of two
or more instances — on the same machine or across machines — is self-healing through the
reaper. Single-worker deployments are supported but rely on the launchd+watchdog belt-and-
braces instead of reaper redundancy.

## Expiry

Jobs can carry an `expires_at` column. If a worker claims a job whose `expires_at` is in the
past, it transitions the job to `cancelled` instead of `running` and moves on. The worker that
inspects the row is the one that cancels it; there is no dedicated expiry sweep.

## Tag-based routing

The `jobs.requires` column is a reserved `jsonb` field for v2 tag-based routing. In v1 it is
unused: every worker claims any pending job. Consumers can write to the column now without
affecting claim behavior; adding the intersection filter to the claim query is a v2 feature.

## FAQ

**What if the queue is empty?** The worker sleeps for its poll interval (5 or 15 seconds) and
polls again. An empty queue is indistinguishable from a healthy idle state.

**What if Supabase is down?** The worker catches the exception, emits a `poll_loop_error`
event, sleeps for the poll interval, and tries again. It does not exit. When Supabase returns,
polling resumes with no intervention.

**What if a worker crashes mid-job?** Two mechanisms cover this. On the next reboot,
`startup_recovery` queries for `running` rows claimed by its own worker id and requeues them.
Separately, after the stale-heartbeat threshold (120 seconds by default), any other worker's
reaper cycle requeues the job. Whichever fires first wins.
