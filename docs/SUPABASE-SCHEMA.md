# Supabase Schema

## For LLMs

**What this covers:** The three tables (`jobs`, `workers`, `worker_events`), one view (`worker_stats`), and one function (`requeue_stale_jobs_for_worker`) that the minicrew engine reads and writes. How to apply them to a fresh Supabase project. Which environment variables the worker needs. The distinction between Supabase's direct Postgres URL (port 5432) and the pooler URL (port 6543), and why the worker rejects the pooler.

**Invariants (do not change without a migration plan):**
- Status string literals (`pending`, `running`, `completed`, `error`, `failed_permanent`, `cancelled`) are filtered by the worker as strings. Do not rename them.
- `jobs.payload` and `jobs.result` are `jsonb`. Consumers store arbitrary shapes inside them.
- `jobs.enqueued_by` is consumer-populated. The engine never writes to it.
- `jobs.requires` is reserved for v2 routing. The engine does not filter on it in v1.
- `worker_events` is reserved for v2 log sinks. The v1 engine never writes to it.
- RLS ships OFF. If you enable RLS, follow the policies in the "RLS guidance" section.

**Do not change:** the `status` CHECK constraint values, the `requeue_stale_jobs_for_worker` signature, or the columns the engine writes during atomic claim (`worker_id`, `claimed_at`, `started_at`, `worker_version`, `attempt_count`).

## Overview

`schema/template.sql` defines everything the worker needs in one apply:

| Object                             | Kind     | Purpose                                                        |
|------------------------------------|----------|----------------------------------------------------------------|
| `jobs`                             | table    | The work queue. Consumers insert; workers claim and update.     |
| `workers`                          | table    | One row per running worker instance. Heartbeats update it.     |
| `worker_events`                    | table    | v2-reserved log sink target. Empty in v1.                      |
| `requeue_stale_jobs_for_worker`    | function | Reaper RPC. Requeues jobs from a dead worker atomically.       |
| `worker_stats`                     | view     | Aggregate counts powering `python -m worker --status`.         |

## How to apply

**Option A — Supabase SQL editor:**

1. Open your Supabase project.
2. Click **SQL Editor**.
3. Paste the contents of `schema/template.sql`.
4. Click **Run**.

**Option B — `psql`:**

```bash
psql "$SUPABASE_DB_URL" -f schema/template.sql
```

Apply exactly once per project. Re-applying is safe for the `create extension`, `create or replace function`, and `create or replace view` statements, but the `create table` statements will fail if the tables already exist. Use `drop table ... cascade` first if you intend to reset.

## Required environment variables

| Var                          | Purpose                                                                                                                              |
|------------------------------|--------------------------------------------------------------------------------------------------------------------------------------|
| `SUPABASE_URL`               | REST URL, e.g. `https://<project>.supabase.co`. The PostgREST client uses this for claim, heartbeat, and status reads/writes.        |
| `SUPABASE_SERVICE_ROLE_KEY`  | Service role key. Bypasses RLS. Treat as a secret. Always redacted from logs.                                                        |
| `SUPABASE_DB_URL`            | Direct Postgres connection string on port **5432**. Used only by the reaper's advisory lock. Must NOT be the pooler URL on 6543.     |

These belong in the worker machine's `.env` (never committed). `.env.example` lists them as placeholders.

## Direct vs pooler URL

The reaper uses `pg_try_advisory_xact_lock` to guarantee exactly one worker requeues stale jobs per cycle. Advisory locks are session-scoped and require a real Postgres connection. Supabase's pooler URL (port 6543) is pgBouncer running in transaction-pool mode; advisory lock behavior on a pooler is undefined and will silently break the reaper.

**How to find the correct URL in Supabase:**

1. Open **Project Settings** in the left nav.
2. Click **Database**.
3. Scroll to **Connection string**.
4. Select the **Direct connection** tab (not "Transaction" or "Session" pooler tabs).
5. Copy the URI. The port is **5432**.

Example shapes (hostnames are illustrative):

- Direct (correct):  `postgresql://postgres:PWD@db.xxxxxxxxxxxx.supabase.co:5432/postgres`
- Pooler (WRONG):    `postgresql://postgres.xxxx:PWD@aws-0-us-east-1.pooler.supabase.com:6543/postgres`

The worker's `worker/utils/db_url.py` rejects hostnames containing `pooler` on startup with a message pointing back to this section. If you see that error, re-copy the URL from the Direct connection tab.

## Tables

### `jobs`

The work queue. Consumers insert rows; the engine claims and updates them.

| Column            | Type          | Notes                                                                                        |
|-------------------|---------------|----------------------------------------------------------------------------------------------|
| `id`              | uuid PK       | `gen_random_uuid()`.                                                                         |
| `job_type`        | text          | Must match a key in the consumer's `config.yaml` `job_types`.                                |
| `status`          | text          | Check constraint: `pending`, `running`, `completed`, `error`, `failed_permanent`, `cancelled`. |
| `priority`        | int           | Higher wins. `jobs_claim_idx` orders by `priority desc, created_at`.                         |
| `worker_id`       | text          | Set by engine on claim; cleared when requeued.                                               |
| `claimed_at`      | timestamptz   | Set at atomic-claim success.                                                                 |
| `started_at`      | timestamptz   | Set when the Terminal session actually launches (distinct from claim for fan_out).           |
| `completed_at`    | timestamptz   | Set on completion, error, or cancellation.                                                   |
| `expires_at`      | timestamptz   | Optional deadline. Jobs past this are cancelled on claim attempt.                            |
| `attempt_count`   | int           | Incremented by the reaper on requeue.                                                        |
| `max_attempts`    | int (nullable)| Per-row override. When NULL (default), the reaper uses `cfg.reaper.max_attempts` from the worker's config via `coalesce`. Set explicitly on a row only when you want to override the global default for that specific job. |
| `requires`        | jsonb         | **v2 reserved.** No v1 filter.                                                               |
| `payload`         | jsonb         | Consumer-shaped input. Validated against `payload.schema.json` if present.                    |
| `result`          | jsonb         | Engine writes the final result document here.                                                |
| `error_message`   | text          | Engine or reaper writes a human-readable message on error/failed_permanent.                  |
| `enqueued_by`     | text          | **Consumer-populated.** Engine never writes.                                                 |
| `worker_version`  | text          | Engine writes at claim.                                                                      |
| `created_at`      | timestamptz   | Default `now()`.                                                                             |

**Indexes:**
- `jobs_claim_idx on jobs (priority desc, created_at) where status = 'pending'` — partial index powering the claim query.
- `jobs_worker_status_idx on jobs (worker_id, status)` — powers startup recovery and reaper selects.

**Gotcha:** the `status` CHECK constraint is the source of truth for allowed transitions. Adding a new status is a migration, not a config change.

### `workers`

One row per worker instance. The heartbeat thread upserts every 30s.

| Column          | Type          | Notes                                                          |
|-----------------|---------------|----------------------------------------------------------------|
| `id`            | text PK       | `<prefix>-<hostname>-<instance>`.                              |
| `hostname`      | text          | Machine hostname.                                              |
| `instance`      | int           | 1..N on a given host.                                          |
| `role`          | text          | `primary` or `secondary`.                                      |
| `status`        | text          | `idle`, `busy`, or `offline`.                                  |
| `last_heartbeat`| timestamptz   | Upserted every ~30s. The reaper reads this.                    |
| `version`       | text          | Worker code version.                                           |
| `started_at`    | timestamptz   | Set once at boot.                                              |

**Indexes:** `workers_heartbeat_idx on workers (last_heartbeat) where status != 'offline'`.

**Gotcha:** if two worker instances start with the same `id` (same prefix, hostname, and instance number), they will clobber each other's heartbeats. Keep instance numbers unique per host.

### `worker_events`

**v2 reserved.** The v1 engine does not read or write this table. It exists so consumers can pre-provision and a later Postgres log sink does not require a new migration.

| Column       | Type          | Notes                        |
|--------------|---------------|------------------------------|
| `id`         | bigserial PK  | Monotonic id.                |
| `ts`         | timestamptz   | Default `now()`.             |
| `worker_id`  | text          | Nullable.                    |
| `event_type` | text          | e.g. `worker_started`.       |
| `payload`    | jsonb         | Event-specific fields.       |

**Index:** `worker_events_ts_idx on worker_events (ts desc)`.

## Functions

### `requeue_stale_jobs_for_worker(p_worker_id text, p_default_max int) returns int`

Called exclusively by the reaper thread, inside the advisory-lock transaction. Takes the id of a worker whose heartbeat is too old and:

1. Selects all `running` jobs owned by that worker.
2. Increments `attempt_count`.
3. Transitions each to `pending` unless the next attempt would reach `coalesce(max_attempts, p_default_max)`, in which case `failed_permanent` and a descriptive `error_message` are written. `max_attempts` is the total number of runs allowed: with `max_attempts=3`, attempts 1 and 2 fail and requeue; the third attempt's failure triggers the poison transition (the row never claims a fourth time).
4. Clears `worker_id`, `claimed_at`, and `started_at`.
5. Returns the number of rows requeued.

The engine does not call this directly outside the reaper path. Do not call it from application code.

## Views

### `worker_stats`

Single-row aggregate view. Returns:

| Column                          | Meaning                                                                             |
|---------------------------------|-------------------------------------------------------------------------------------|
| `idle_count`                    | Count of `workers` with `status = 'idle'`.                                          |
| `busy_count`                    | Count of `workers` with `status = 'busy'`.                                          |
| `offline_count`                 | Count of `workers` with `status = 'offline'`.                                       |
| `queue_depth`                   | Count of `jobs` with `status = 'pending'`.                                          |
| `running_count`                 | Count of `jobs` with `status = 'running'`.                                          |
| `recent_errors_1h`              | Count of `jobs` with `status = 'error'` completed in the last hour.                 |
| `recent_failed_permanent_24h`   | Count of `jobs` with `status = 'failed_permanent'` completed in the last 24 hours.  |

`python -m worker --status` reads this view via PostgREST and prints JSON. The view exists because PostgREST cannot do `COUNT(*) GROUP BY` natively in a single round trip.

## RLS guidance

The schema ships with RLS OFF. If you want to enable RLS for defence-in-depth, the following pattern mirrors how the worker expects to be authorized.

- **`jobs`** — readable only by the service role (the worker) and an authenticated backend role your app uses for enqueue.
- **`workers`** — readable by any authenticated role (so dashboards can list the fleet); writeable only by the service role.
- **`worker_events`** — same pattern as `workers`.

Sample policies:

```sql
-- Enable RLS.
alter table jobs           enable row level security;
alter table workers        enable row level security;
alter table worker_events  enable row level security;

-- jobs: service role full access.
create policy jobs_service_all on jobs
  for all to service_role using (true) with check (true);

-- jobs: your backend role can insert rows attributed to itself, and read those rows back.
-- Adjust the role name to match the role your consumer backend authenticates as.
-- The `enqueued_by` match keeps the audit trail meaningful and prevents spoofing.
create policy jobs_backend_insert on jobs
  for insert to authenticated
  with check (enqueued_by = auth.uid()::text);
create policy jobs_backend_select on jobs
  for select to authenticated using (enqueued_by = auth.uid()::text);

-- workers: service role full access.
create policy workers_service_all on workers
  for all to service_role using (true) with check (true);

-- workers: read-only for authenticated dashboards.
create policy workers_read on workers
  for select to authenticated using (true);

-- worker_events: service role full access; authenticated read-only.
create policy events_service_all on worker_events
  for all to service_role using (true) with check (true);
create policy events_read on worker_events
  for select to authenticated using (true);
```

Adjust role names to match your Supabase setup. The worker itself only needs the service role, so it operates unaffected by these policies.

## Extending the payload/result shapes

`jobs.payload` and `jobs.result` are `jsonb`. Consumers can add arbitrary structure without schema migrations. Two recommendations:

- If you want the engine to validate payloads before launching Claude Code, place a JSON Schema at `worker-config/payload.schema.json`. `schema/payload.schema.example.json` is a permissive starter.
- Keep result shapes stable once downstream consumers rely on them. Version them inside the document (`{"version": 1, ...}`) rather than renaming columns.

## Migration strategy

Add columns with defaults so existing rows backfill:

```sql
alter table jobs add column tenant_id text;
alter table jobs add column cost_cents int not null default 0;
```

Do **not** rename the `status` enum values. The engine filters on string literals; renames silently break claim and reaper queries. If you truly need to rename, deploy the engine change first with dual-read logic, then migrate the column, then remove the old-value handling.

Adding new CHECK values is fine:

```sql
alter table jobs drop constraint jobs_status_check;
alter table jobs add constraint jobs_status_check
  check (status in ('pending','running','completed','error','failed_permanent','cancelled','paused'));
```

Engine releases should document which status values they understand.
