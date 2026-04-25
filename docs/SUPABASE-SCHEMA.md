# Supabase Schema

## For LLMs

**What this covers:** The three tables (`jobs`, `workers`, `worker_events`), one view (`worker_stats`), and one function (`requeue_stale_jobs_for_worker`) that the minicrew engine reads and writes. How to apply them to a fresh Supabase project. Which environment variables the worker needs. The distinction between Supabase's direct Postgres URL (port 5432) and the pooler URL (port 6543), and why the worker rejects the pooler.

**Invariants (do not change without a migration plan):**
- Status string literals (`pending`, `running`, `completed`, `error`, `failed_permanent`, `cancelled`) are filtered by the worker as strings. Do not rename them.
- `jobs.payload` and `jobs.result` are `jsonb`. Consumers store arbitrary shapes inside them.
- `jobs.submitted_by` is consumer-populated (uuid; matches `auth.uid()` for
  RLS). The engine never writes to it. Was `enqueued_by text` in v1 — renamed
  in 002.
- `jobs.requires` is reserved for v2 routing. The engine does not filter on it in v1.
- `worker_events` is reserved for v2 log sinks. The v1 engine never writes to it.
- RLS on `jobs` is **enabled by 002** with caller-side policies; the worker (service_role) bypasses. Pre-002 (v1) installs ship with RLS OFF — see the "RLS guidance" section below for the v1 pattern.

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

## Applying migrations

minicrew ships SQL migrations under `schema/migrations/`. Apply IN ORDER. Each
file is wrapped in `BEGIN; ... COMMIT;` and uses idempotent DDL (`if not
exists`, `create or replace`, `drop ... if exists` before re-create) — re-running
a migration is safe. A failure mid-migration rolls the whole file back; fix and
re-run.

Order:

1. **Initial schema** — `schema/template.sql` (fresh installs only). Contains
   the v2 + v3 columns inline; if you apply this on a fresh project you can
   skip 002 and 003. The migration files exist for **upgrading existing v1
   installs** in place.
2. **`schema/migrations/002_remote_subagent.sql`** — Phase 2 dispatch
   infrastructure (RLS, MCP bundles, atomic claim RPC, log Storage bucket,
   cancel-sweep trigger).
3. **`schema/migrations/003_handoff.sql`** — Phase 3 handoff (transcript
   bundles + outbound retention + `dispatch_attach_bundles` RPC + orphan views).
   **Apply AFTER 002.**
4. **`schema/migrations/004_bundle_fetch_rpc.sql`** — Adds two SECURITY DEFINER
   bridge RPCs (`dispatch_fetch_mcp_bundle`, `dispatch_fetch_transcript_bundle`)
   so the worker can read decrypted bundles WITHOUT exposing the `vault` schema
   to PostgREST. **Apply AFTER 003.** (Already inlined in `template.sql` for
   fresh installs.)

**Option A — Supabase SQL editor:**

1. Open your Supabase project → **SQL Editor**.
2. Paste the contents of `schema/template.sql`. Run.
3. (Upgrading from v1) Paste `002_remote_subagent.sql`. Run.
4. (Adding handoff) Paste `003_handoff.sql`. Run.
5. (Upgrading from v3 → v4) Paste `004_bundle_fetch_rpc.sql`. Run.

**Option B — `psql`:**

```bash
psql "$SUPABASE_DB_URL" -f schema/template.sql
psql "$SUPABASE_DB_URL" -f schema/migrations/002_remote_subagent.sql
psql "$SUPABASE_DB_URL" -f schema/migrations/003_handoff.sql
psql "$SUPABASE_DB_URL" -f schema/migrations/004_bundle_fetch_rpc.sql
```

**Option C — Supabase CLI:** `supabase db push --include-seed`.

After applying, verify:

- `python -m worker --check-rpcs` — confirms every dispatch RPC is callable
  with the configured service-role key. Exits non-zero (and prints which RPCs
  are missing) if anything is unprovisioned.
- `python -m worker --preflight` — full readiness check including operator MCP
  isolation, GitHub App token mint, GitHub App `contents:write` scope, Storage
  bucket policies, and RPC presence.

If preflight fails with `Handoff/dispatch RPCs missing in database: [...]`, you
forgot to apply 002 or 003. Re-apply (idempotent) and re-run preflight.

## Operator prerequisites for dispatch

Two operator-side configurations are NOT in SQL but ARE required for the
dispatch surface to work:

1. **`SUPABASE_DB_URL` must be a role that bypasses RLS.** The reaper's
   `requeue_stale_jobs_for_worker` runs over `direct_url` and writes to `jobs`.
   If the role is RLS-enforced (e.g., a custom `app_user`), the reaper silently
   no-ops. Use the `postgres` superuser or a custom role with `bypassrls`.
3. **Storage bucket `minicrew-logs` must exist and be private.** Created
   automatically by 002 (`insert into storage.buckets ... on conflict do
   nothing`). Preflight verifies existence AND that anon access is blocked. If
   you tighten/loosen Storage policies later, run `python -m worker --preflight`
   to re-verify.

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
| `submitted_by`     | uuid          | **v2.** Caller's `auth.uid()`. RLS enforces SELECT/INSERT/UPDATE only when this matches. Renamed from `enqueued_by text` in 002. |
| `requested_status` | text          | **v2.** `null` or `'cancel'`. Caller writes via `update`; trigger sweeps pending → cancelled; running detected by heartbeat. |
| `progress`         | jsonb         | **v2.** Latest progress line written by `ProgressTailer` from `_progress.jsonl`.            |
| `caller_log_url`   | text          | **v2.** Signed URL to the chunked log manifest in Storage. PATCHed once on first chunk upload. |
| `mcp_bundle_id`    | uuid          | **v2.** FK-by-convention into `vault.secrets.id`. Worker reads via `vault.decrypted_secrets`. |
| `final_transcript_bundle_id` | uuid | **v3 (handoff).** FK-by-convention into `vault.secrets.id` for the worker's outbound transcript bundle. Unique partial index ensures one job per outbound bundle. |
| `worker_version`  | text          | Engine writes at claim.                                                                      |
| `created_at`      | timestamptz   | Default `now()`.                                                                             |

**Indexes:**
- `jobs_claim_idx on jobs (priority desc, created_at) where status = 'pending'` — partial index powering the claim query.
- `jobs_worker_status_idx on jobs (worker_id, status)` — powers startup recovery and reaper selects.
- `jobs_submitted_by_status_idx on jobs (submitted_by, status) where status = 'running'` — **v2.** Powers per-caller cap inside `claim_next_job_with_cap`.
- `jobs_final_transcript_bundle_id_uq on jobs (final_transcript_bundle_id) where final_transcript_bundle_id is not null` — **v3.** Unique partial. Defends ownership lookup.
- `jobs_payload_transcript_bundle_id_idx on jobs ((payload->>'transcript_bundle_id')) where payload ? 'transcript_bundle_id'` — **v3.** Powers orphan-inbound sweeper.

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

### `claim_next_job_with_cap(p_worker_id text, p_version text, p_cap int default 10) returns setof jobs`

**Added by 002.** Atomic claim with per-caller cap. Replaces the v1
GET-then-PATCH dance for callers with `submitted_by` set. For legacy rows where
`submitted_by IS NULL`, the cap is bypassed (so v1 batch consumers see no
behavior change).

Behavior:
1. `SELECT … FROM jobs WHERE status='pending' AND requested_status IS NULL AND
   (submitted_by IS NULL OR <count of submitted_by's running jobs> < p_cap)
   ORDER BY priority DESC, created_at ASC FOR UPDATE SKIP LOCKED LIMIT 1`.
2. If `expires_at < now()`, mark `cancelled` and return empty.
3. UPDATE to `running` with worker_id/claimed_at/worker_version, return the row.

Service-role only (no `grant execute to authenticated`).

### `sweep_cancel_pending()` trigger

**Added by 002.** `BEFORE INSERT OR UPDATE ON jobs FOR EACH ROW`. When
`new.status = 'pending' AND new.requested_status = 'cancel'`, mutates the row to
`status='cancelled'`, `completed_at=now()`,
`error_message='cancelled before claim'` before the row hits storage. Catches
both newly-inserted-already-cancelled rows and the race where a caller flips
`requested_status` between claim attempts.

### `enforce_caller_update_columns()` trigger

**Added by 002, re-created by 003 with the new `final_transcript_bundle_id`
column included in the comparison tuple.** `BEFORE UPDATE ON jobs FOR EACH
ROW`. service_role and `postgres` bypass; everyone else can change ONLY
`requested_status`. Implementation uses a tuple `IS DISTINCT FROM` comparison
across every other column. When 003 added `final_transcript_bundle_id`, the
trigger function was re-CREATEd with the new column included; the trigger
itself was already attached to the table, so no `DROP TRIGGER` was needed.

### Vault RPCs (added by 002 + 003 + 004)

| RPC | Caller | Purpose |
|---|---|---|
| `dispatch_register_mcp_bundle(p_secret jsonb) returns uuid` | authenticated (caller) | SECURITY DEFINER. Inserts an encrypted `vault.secrets` row named `minicrew_mcp_<uuid>` and returns the id. |
| `dispatch_fetch_mcp_bundle(p_id uuid) returns text` | service_role (worker) | SECURITY DEFINER bridge. Returns `decrypted_secret` from `vault.decrypted_secrets`. **Added by 004** — lets the worker fetch the bundle without exposing the `vault` schema in PostgREST. |
| `dispatch_delete_mcp_bundle(p_id uuid) returns void` | service_role (worker) | SECURITY DEFINER. Deletes the Vault row. |
| `dispatch_register_transcript_bundle(p_secret jsonb) returns uuid` | authenticated (caller and worker) | SECURITY DEFINER. Server-side cap of 10 MB on serialized JSON. Used inbound (caller registers) AND outbound (worker registers extended transcript). |
| `dispatch_fetch_transcript_bundle(p_id uuid) returns text` | service_role (worker) | SECURITY DEFINER bridge. Returns `decrypted_secret` for an inbound or outbound transcript bundle. **Added by 004** — same rationale as the MCP fetch RPC. |
| `dispatch_fetch_outbound_transcript(p_job_id uuid) returns jsonb` | authenticated (caller) | SECURITY DEFINER. Ownership-checked (`submitted_by = auth.uid()`). Keyed by job_id (PK), NOT bundle_id — eliminates predicate-injection / bundle-spoofing surfaces. Returns the decrypted JSONB. |
| `dispatch_delete_transcript_bundle(p_id uuid) returns void` | service_role (worker) | SECURITY DEFINER. Used for both inbound delete (gated by `cfg.dispatch.handoff.delete_inbound_on_completion`) and outbound retention sweep. |
| `dispatch_check_rpcs(p_names text[]) returns text[]` | service_role (worker preflight) | SECURITY DEFINER. **Returns the subset of names that are MISSING** from `pg_proc` (NOT the names that exist). Empty array means all present. Worker preflight uses this directly without parsing error strings. |

**Server-side transcript size cap.** `dispatch_register_transcript_bundle`
enforces `octet_length(p_secret::text) <= v_max` where `v_max := 10485760`
(10 MB) is hardcoded in the function body. This MUST stay in sync with
`cfg.dispatch.handoff.max_transcript_bundle_bytes`. If an operator bumps the
config above 10 MB, they must also `CREATE OR REPLACE` the RPC with the new
`v_max` value, or registration silently fails at the SQL layer for bundles in
between.

### Storage

**Bucket `minicrew-logs` created by 002.** Private (no anon access). Used for:

- `<job_id>/chunk-NNNN.log` and `<job_id>/manifest.json` — chunked Terminal
  session logs uploaded by `ChunkedLogStreamer`.
- `transcripts/<session>-<unix-ts>.json.gz` — gzipped large outbound transcript
  bundles when the serialized JSON exceeds `cfg.dispatch.handoff.vault_inline_cap_bytes`.

Required policies (service-role-only — preflight refuses to start if anon can
read):

```sql
-- Default Supabase policy already restricts to authenticated by default;
-- the worker uses the service role and bypasses RLS.
-- Verify no anon SELECT policy exists on storage.objects for this bucket.
```

Preflight runs an HTTP HEAD against the bucket without auth and refuses to
start if it succeeds.

### Orphan-bundle views (003)

| View | Purpose |
|---|---|
| `v_orphan_transcript_bundles` | `vault.secrets` rows named `minicrew_transcript_*` with no jobs row referencing them via `final_transcript_bundle_id` OR `payload->>'transcript_bundle_id'`. Reaper sweeps rows older than 24h to catch caller-side INSERT failures. |
| `v_orphan_mcp_bundles` | `vault.secrets` rows named `minicrew_mcp_*` with no jobs row referencing them via `mcp_bundle_id`. Same 24h sweep. Retroactive parallel to the transcript view; closes the gap where 002's MCP bundles could leak after a caller-side INSERT failure. |

Both views are service-role-only. The reaper queries them inside the same
advisory-lock cycle as `requeue_stale_jobs_for_worker`.

### Reaper sweepers (added by 002 + 003)

The reaper, when `cfg.dispatch is not None`, runs three additional sweeps per
cycle (in addition to `requeue_stale_jobs_for_worker`):

1. **Outbound transcript retention.** For each `final_transcript_bundle_id NOT
   NULL` job whose `completed_at < now() - interval '<outbound_retention_days>
   days'` (default 7): call `dispatch_delete_transcript_bundle`, also delete the
   corresponding Storage object if `storage_ref` exists, then PATCH
   `final_transcript_bundle_id = NULL`.
2. **Orphan inbound transcript bundles.** Query `v_orphan_transcript_bundles`
   for rows older than 24h; delete each.
3. **Orphan MCP bundles.** Query `v_orphan_mcp_bundles` for rows older than 24h;
   delete each.

Storage retention (live job logs) is also swept here when
`cfg.dispatch.log_storage.delete_logs_on_completion: false`: prefixes older than
`retention_days` are deleted.

## Row-Level Security (RLS) — required for dispatch

**The 002 migration enables RLS on `jobs` and adds caller policies.** Unlike v1
where RLS was optional, v2's caller-facing surface relies on RLS to enforce
that callers see only their own rows.

Policies created by 002:

- `jobs_caller_select` — SELECT allowed when `auth.uid() = submitted_by`.
- `jobs_caller_insert` — INSERT allowed when `auth.uid() = submitted_by AND
  status = 'pending' AND worker_id IS NULL AND result IS NULL AND started_at
  IS NULL AND claimed_at IS NULL`. (Caller cannot fabricate a row that looks
  already-claimed or already-completed.)
- `jobs_caller_update` — UPDATE allowed when `auth.uid() = submitted_by` for
  both USING and WITH CHECK. **Column restriction enforced by the
  `enforce_caller_update_columns` trigger** (RLS in Postgres cannot easily
  restrict by column).

The service_role bypasses all RLS; the worker uses the service role.

The `enforce_caller_update_columns` trigger uses `current_user`/`session_user`
checks (always reliable in Postgres) rather than
`request.jwt.claim.role` (PostgREST-version-dependent GUC).

For batch-only deployments without `dispatch:`, RLS is still enabled by 002 but
the worker (service_role) is unaffected. Legacy callers that wrote rows
without `submitted_by` will fail INSERT under RLS — they must be migrated to
populate `submitted_by`. The `claim_next_job_with_cap` RPC includes a
back-compat branch (`submitted_by IS NULL` bypasses the per-caller cap) so
existing rows in flight at the time of migration still claim.

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

**Note:** The 002 migration enables RLS on `jobs` with the
`jobs_caller_select` / `_insert` / `_update` policies described in the "Row-Level
Security (RLS) — required for dispatch" section above. The pattern below is the
**v1 pre-002 reference** for batch-only deployments where you want defense-in-depth
RLS without the dispatch surface. If you applied 002, you do NOT need these
policies on `jobs` — they're already in place.

The pattern below also covers `workers` and `worker_events`, which 002 does NOT
modify.

The schema ships with RLS OFF on `workers` and `worker_events`. If you want to enable RLS for defence-in-depth, the following pattern mirrors how the worker expects to be authorized.

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
-- The `submitted_by` match keeps the audit trail meaningful and prevents spoofing.
-- NOTE: post-002 the column type is uuid; drop the ::text cast.
-- This sample is the v1 pre-002 form (column was text). For v2+, the
-- 002 migration has already created equivalent policies — do not duplicate.
create policy jobs_backend_insert on jobs
  for insert to authenticated
  with check (submitted_by = auth.uid());
create policy jobs_backend_select on jobs
  for select to authenticated using (submitted_by = auth.uid());

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
