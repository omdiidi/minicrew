-- minicrew generic schema template.
-- Apply this to your Supabase (or plain Postgres) database before starting workers.
-- See docs/SUPABASE-SCHEMA.md for a walkthrough, RLS guidance, and migration strategy.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- jobs: the work queue. Consumers insert rows; workers claim and update them.
-- ---------------------------------------------------------------------------
create table jobs (
  id uuid primary key default gen_random_uuid(),
  job_type text not null,
  status text not null default 'pending'
    check (status in ('pending','running','completed','error','failed_permanent','cancelled')),
  priority int not null default 0,
  worker_id text,
  claimed_at timestamptz,
  started_at timestamptz,
  completed_at timestamptz,
  expires_at timestamptz,
  attempt_count int not null default 0,
  -- Nullable per-row override. When NULL, the reaper's `coalesce(max_attempts, p_default_max)`
  -- falls back to cfg.reaper.max_attempts from the worker config. Set explicitly on a job row
  -- only when you want to override the global default for that specific job.
  max_attempts int,
  -- RESERVED for v2 routing; engine does not filter on `requires` in v1.
  requires jsonb not null default '[]'::jsonb,
  payload jsonb not null default '{}'::jsonb,
  result jsonb,
  error_message text,
  -- CONSUMER-populated; engine never writes to `enqueued_by`.
  enqueued_by text,
  worker_version text,
  created_at timestamptz not null default now()
);

create index jobs_claim_idx on jobs (priority desc, created_at) where status = 'pending';
create index jobs_worker_status_idx on jobs (worker_id, status);

-- ---------------------------------------------------------------------------
-- workers: one row per running worker instance. Heartbeats update it every ~30s.
-- ---------------------------------------------------------------------------
create table workers (
  id text primary key,
  hostname text not null,
  instance int not null,
  role text not null check (role in ('primary','secondary')),
  status text not null check (status in ('idle','busy','offline')),
  last_heartbeat timestamptz not null default now(),
  version text,
  started_at timestamptz not null default now()
);

create index workers_heartbeat_idx on workers (last_heartbeat) where status != 'offline';

-- ---------------------------------------------------------------------------
-- worker_events: v2 reserved -- file sink is the v1 log destination. Creating
-- this table now is safe and saves a later migration when Postgres or HTTP
-- sinks arrive. The v1 engine does not read from or write to this table.
-- ---------------------------------------------------------------------------
create table if not exists worker_events (
  id bigserial primary key,
  ts timestamptz not null default now(),
  worker_id text,
  event_type text not null,
  payload jsonb not null default '{}'::jsonb
);
create index worker_events_ts_idx on worker_events (ts desc);

-- ---------------------------------------------------------------------------
-- requeue_stale_jobs_for_worker: called by the reaper (inside an advisory-lock
-- transaction) to requeue jobs owned by a worker that has missed heartbeats.
-- Honors per-row `max_attempts`, falling back to `p_default_max` via coalesce.
-- Clears `claimed_at` and `started_at`; on poison-pill, writes `error_message`.
-- ---------------------------------------------------------------------------
create or replace function requeue_stale_jobs_for_worker(p_worker_id text, p_default_max int)
returns int language plpgsql as $$
declare n_requeued int := 0;
begin
  with stale as (
    select id,
           attempt_count + 1 as next_attempt,
           coalesce(max_attempts, p_default_max) as effective_max
      from jobs
     where worker_id = p_worker_id and status = 'running'
  )
  update jobs j set
    status = case when s.next_attempt >= s.effective_max then 'failed_permanent' else 'pending' end,
    worker_id = null,
    started_at = null,
    claimed_at = null,
    attempt_count = s.next_attempt,
    completed_at = case when s.next_attempt >= s.effective_max then now() else j.completed_at end,
    error_message = case when s.next_attempt >= s.effective_max
                         then format('Exceeded max_attempts=%s (worker %s)', s.effective_max, p_worker_id)
                         else null end
  from stale s where j.id = s.id;
  get diagnostics n_requeued = row_count;
  return n_requeued;
end;
$$;

-- ---------------------------------------------------------------------------
-- worker_stats: aggregate view for `python -m worker --status`. PostgREST
-- cannot do COUNT/GROUP BY natively, so a view is the portable answer.
-- ---------------------------------------------------------------------------
create or replace view worker_stats as
  select
    (select count(*) from workers where status = 'idle')    as idle_count,
    (select count(*) from workers where status = 'busy')    as busy_count,
    (select count(*) from workers where status = 'offline') as offline_count,
    (select count(*) from jobs    where status = 'pending') as queue_depth,
    (select count(*) from jobs    where status = 'running') as running_count,
    (select count(*) from jobs    where status = 'error'            and completed_at > now() - interval '1 hour')
      as recent_errors_1h,
    (select count(*) from jobs    where status = 'failed_permanent' and completed_at > now() - interval '24 hours')
      as recent_failed_permanent_24h;

-- ---------------------------------------------------------------------------
-- RLS guidance lives in docs/SUPABASE-SCHEMA.md. Schema ships with RLS OFF.
-- ---------------------------------------------------------------------------

-- ===========================================================================
-- v2 (Phase 2a) — remote sub-agent foundations.
-- Mirrors schema/migrations/002_remote_subagent.sql for fresh installs.
-- Idempotent: safe to apply alongside the migration on existing databases.
-- ===========================================================================

BEGIN;

-- Identity: drop enqueued_by, add submitted_by uuid.
alter table jobs drop column if exists enqueued_by;
alter table jobs add column if not exists submitted_by uuid;
create index if not exists jobs_submitted_by_status_idx
  on jobs (submitted_by, status) where status = 'running';

-- New columns
alter table jobs add column if not exists requested_status text
  check (requested_status is null or requested_status in ('cancel'));
alter table jobs add column if not exists progress jsonb;
alter table jobs add column if not exists caller_log_url text;
alter table jobs add column if not exists mcp_bundle_id uuid;

-- Cancel-sweep trigger: pending + requested_status='cancel' -> cancelled immediately.
create or replace function sweep_cancel_pending() returns trigger language plpgsql as $$
begin
  if new.status = 'pending' and new.requested_status = 'cancel' then
    new.status := 'cancelled';
    new.completed_at := now();
    new.error_message := 'cancelled before claim';
  end if;
  return new;
end;
$$;
drop trigger if exists trg_sweep_cancel_pending on jobs;
create trigger trg_sweep_cancel_pending before insert or update on jobs
  for each row execute function sweep_cancel_pending();

-- Atomic claim with per-caller cap (replaces the GET+PATCH dance).
create or replace function claim_next_job_with_cap(
  p_worker_id text, p_version text, p_cap int default 10
) returns setof jobs language plpgsql as $$
declare
  v_job jobs%rowtype;
begin
  select * into v_job from jobs
   where status = 'pending'
     and (requested_status is null)
     and (
       submitted_by is null
       or (select count(*) from jobs j2
             where j2.submitted_by = jobs.submitted_by
               and j2.status = 'running') < p_cap
     )
   order by priority desc, created_at asc
   for update skip locked
   limit 1;

  if not found then return; end if;

  if v_job.expires_at is not null and v_job.expires_at < now() then
    update jobs set status='cancelled', completed_at=now()
     where id=v_job.id;
    return;
  end if;

  update jobs set
    status='running',
    worker_id=p_worker_id,
    claimed_at=now(),
    worker_version=p_version
   where id=v_job.id
   returning * into v_job;

  return next v_job;
end;
$$;
revoke all on function claim_next_job_with_cap(text, text, int) from public;

-- Vault RPCs.
create or replace function dispatch_register_mcp_bundle(p_secret jsonb)
  returns uuid language plpgsql security definer set search_path = vault, public as $$
declare v_id uuid;
begin
  v_id := vault.create_secret(p_secret::text, 'minicrew_mcp_' || gen_random_uuid()::text, 'minicrew ad_hoc MCP bundle');
  return v_id;
end;
$$;
revoke all on function dispatch_register_mcp_bundle(jsonb) from public;
grant execute on function dispatch_register_mcp_bundle(jsonb) to authenticated;

create or replace function dispatch_delete_mcp_bundle(p_id uuid)
  returns void language plpgsql security definer set search_path = vault, public as $$
begin
  delete from vault.secrets where id = p_id;
end;
$$;
revoke all on function dispatch_delete_mcp_bundle(uuid) from public;

-- SECURITY DEFINER bridge so the worker can fetch the decrypted MCP bundle
-- without exposing `vault` schema in PostgREST. service_role only.
create or replace function dispatch_fetch_mcp_bundle(p_id uuid)
  returns text language plpgsql security definer set search_path = vault, public as $$
declare
  v_secret text;
begin
  select decrypted_secret into v_secret from vault.decrypted_secrets where id = p_id;
  if v_secret is null then
    raise exception 'mcp bundle % not found', p_id using errcode = 'no_data_found';
  end if;
  return v_secret;
end;
$$;
revoke all on function dispatch_fetch_mcp_bundle(uuid) from public;
grant execute on function dispatch_fetch_mcp_bundle(uuid) to service_role;

-- RLS
alter table jobs enable row level security;

drop policy if exists jobs_caller_select on jobs;
create policy jobs_caller_select on jobs
  for select using (auth.uid() = submitted_by);

drop policy if exists jobs_caller_insert on jobs;
create policy jobs_caller_insert on jobs
  for insert with check (
    auth.uid() = submitted_by
    and status = 'pending'
    and worker_id is null
    and result is null
    and started_at is null
    and claimed_at is null
    and mcp_bundle_id is null
  );

drop policy if exists jobs_caller_update on jobs;
create policy jobs_caller_update on jobs
  for update using (auth.uid() = submitted_by) with check (auth.uid() = submitted_by);

create or replace function enforce_caller_update_columns() returns trigger language plpgsql as $$
begin
  if current_user in ('service_role', 'postgres') or session_user in ('service_role', 'postgres') then
    return new;
  end if;
  if (new.id, new.job_type, new.status, new.priority, new.worker_id, new.claimed_at,
      new.started_at, new.completed_at, new.expires_at, new.attempt_count, new.max_attempts,
      new.requires, new.payload, new.result, new.error_message, new.submitted_by,
      new.progress, new.caller_log_url, new.mcp_bundle_id, new.worker_version)
   is distinct from
     (old.id, old.job_type, old.status, old.priority, old.worker_id, old.claimed_at,
      old.started_at, old.completed_at, old.expires_at, old.attempt_count, old.max_attempts,
      old.requires, old.payload, old.result, old.error_message, old.submitted_by,
      old.progress, old.caller_log_url, old.mcp_bundle_id, old.worker_version)
  then
    raise exception 'callers may only update requested_status';
  end if;
  return new;
end;
$$;
drop trigger if exists trg_enforce_caller_update_columns on jobs;
create trigger trg_enforce_caller_update_columns before update on jobs
  for each row execute function enforce_caller_update_columns();

-- Vault read access
grant usage on schema vault to service_role;
grant select on vault.decrypted_secrets to service_role;

-- Storage bucket
insert into storage.buckets (id, name, public)
  values ('minicrew-logs', 'minicrew-logs', false)
  on conflict do nothing;

COMMIT;

-- ===========================================================================
-- v3 (Phase 3) — handoff foundations.
-- Mirrors schema/migrations/003_handoff.sql for fresh installs.
-- Idempotent: safe to re-apply.
-- ===========================================================================

BEGIN;

-- Outbound transcript bundle column + indexes.
alter table jobs add column if not exists final_transcript_bundle_id uuid;

create unique index if not exists jobs_final_transcript_bundle_id_uq
  on jobs (final_transcript_bundle_id)
  where final_transcript_bundle_id is not null;

create index if not exists jobs_payload_transcript_bundle_id_idx
  on jobs ((payload->>'transcript_bundle_id'))
  where payload ? 'transcript_bundle_id';

-- Re-create caller insert policy to also forbid final_transcript_bundle_id at insert.
drop policy if exists jobs_caller_insert on jobs;
create policy jobs_caller_insert on jobs
  for insert with check (
    auth.uid() = submitted_by
    and status = 'pending'
    and worker_id is null
    and result is null
    and started_at is null
    and claimed_at is null
    and mcp_bundle_id is null
    and final_transcript_bundle_id is null
  );

-- Caller-callable: attach bundle pointers AFTER insert via SECURITY DEFINER
-- (bypasses enforce_caller_update_columns). Verifies caller owns row + still pending.
create or replace function dispatch_attach_bundles(
  p_job_id uuid,
  p_mcp_bundle_id uuid,
  p_transcript_bundle_id uuid default null
) returns void language plpgsql security definer set search_path = public as $$
declare
  v_owner uuid;
begin
  select submitted_by into v_owner from jobs
   where id = p_job_id and status = 'pending'
   for update;
  if v_owner is null then
    raise exception 'job % not found or not pending', p_job_id;
  end if;
  if v_owner is distinct from auth.uid() then
    raise exception 'not authorized to attach bundles to job %', p_job_id;
  end if;
  update jobs set
    mcp_bundle_id = coalesce(p_mcp_bundle_id, mcp_bundle_id),
    payload = case
      when p_transcript_bundle_id is not null
      then payload || jsonb_build_object('transcript_bundle_id', p_transcript_bundle_id::text)
      else payload
    end
   where id = p_job_id;
end;
$$;
revoke all on function dispatch_attach_bundles(uuid, uuid, uuid) from public;
grant execute on function dispatch_attach_bundles(uuid, uuid, uuid) to authenticated;

-- Re-create the column-restriction trigger function with final_transcript_bundle_id
-- included in BOTH tuple sides. Trigger from v2 stays attached.
create or replace function enforce_caller_update_columns() returns trigger language plpgsql as $$
begin
  if current_user in ('service_role', 'postgres') or session_user in ('service_role', 'postgres') then
    return new;
  end if;
  if (new.id, new.job_type, new.status, new.priority, new.worker_id, new.claimed_at,
      new.started_at, new.completed_at, new.expires_at, new.attempt_count, new.max_attempts,
      new.requires, new.payload, new.result, new.error_message, new.submitted_by,
      new.progress, new.caller_log_url, new.mcp_bundle_id, new.worker_version,
      new.final_transcript_bundle_id)
   is distinct from
     (old.id, old.job_type, old.status, old.priority, old.worker_id, old.claimed_at,
      old.started_at, old.completed_at, old.expires_at, old.attempt_count, old.max_attempts,
      old.requires, old.payload, old.result, old.error_message, old.submitted_by,
      old.progress, old.caller_log_url, old.mcp_bundle_id, old.worker_version,
      old.final_transcript_bundle_id)
  then
    raise exception 'callers may only update requested_status';
  end if;
  return new;
end;
$$;

-- Caller-callable: register an inbound transcript bundle (with server-side size guard).
create or replace function dispatch_register_transcript_bundle(p_secret jsonb)
  returns uuid language plpgsql security definer set search_path = vault, public as $$
declare
  v_id uuid;
  v_size int;
  v_max int := 10485760;  -- 10 MB; keep in sync with cfg.dispatch.handoff.max_transcript_bundle_bytes
begin
  v_size := octet_length(p_secret::text);
  if v_size > v_max then
    raise exception 'transcript bundle size % exceeds max %', v_size, v_max;
  end if;
  v_id := vault.create_secret(
    p_secret::text,
    'minicrew_transcript_' || gen_random_uuid()::text,
    'minicrew handoff transcript bundle (inbound or outbound)'
  );
  return v_id;
end;
$$;
revoke all on function dispatch_register_transcript_bundle(jsonb) from public;
grant execute on function dispatch_register_transcript_bundle(jsonb) to authenticated;

-- Caller-callable: fetch an OUTBOUND transcript for a job they own (keyed by job_id).
create or replace function dispatch_fetch_outbound_transcript(p_job_id uuid)
  returns jsonb language plpgsql security definer set search_path = vault, public as $$
declare
  v_owner uuid;
  v_bundle_id uuid;
  v_secret text;
begin
  select submitted_by, final_transcript_bundle_id
    into v_owner, v_bundle_id
    from jobs where id = p_job_id;
  if v_owner is null then
    raise exception 'job % not found', p_job_id;
  end if;
  if v_owner is distinct from auth.uid() then
    raise exception 'not authorized to fetch transcript for job %', p_job_id;
  end if;
  if v_bundle_id is null then
    raise exception 'job % has no outbound transcript bundle (not yet completed?)', p_job_id;
  end if;
  select decrypted_secret into v_secret from vault.decrypted_secrets where id = v_bundle_id;
  if v_secret is null then
    raise exception 'transcript bundle missing or expired';
  end if;
  return v_secret::jsonb;
end;
$$;
revoke all on function dispatch_fetch_outbound_transcript(uuid) from public;
grant execute on function dispatch_fetch_outbound_transcript(uuid) to authenticated;

-- Worker-only: delete a transcript bundle (any direction).
create or replace function dispatch_delete_transcript_bundle(p_id uuid)
  returns void language plpgsql security definer set search_path = vault, public as $$
begin
  delete from vault.secrets where id = p_id;
end;
$$;
revoke all on function dispatch_delete_transcript_bundle(uuid) from public;

-- SECURITY DEFINER bridge so the worker can fetch the inbound transcript
-- bundle without exposing `vault` schema in PostgREST. service_role only.
create or replace function dispatch_fetch_transcript_bundle(p_id uuid)
  returns text language plpgsql security definer set search_path = vault, public as $$
declare
  v_secret text;
begin
  select decrypted_secret into v_secret from vault.decrypted_secrets where id = p_id;
  if v_secret is null then
    raise exception 'transcript bundle % not found', p_id using errcode = 'no_data_found';
  end if;
  return v_secret;
end;
$$;
revoke all on function dispatch_fetch_transcript_bundle(uuid) from public;
grant execute on function dispatch_fetch_transcript_bundle(uuid) to service_role;

-- Preflight RPC: returns the subset of given function names that are MISSING.
create or replace function dispatch_check_rpcs(p_names text[])
  returns text[] language plpgsql security definer set search_path = pg_catalog, public as $$
declare
  v_missing text[] := array[]::text[];
  v_name text;
begin
  foreach v_name in array p_names loop
    if not exists (select 1 from pg_proc where proname = v_name) then
      v_missing := array_append(v_missing, v_name);
    end if;
  end loop;
  return v_missing;
end;
$$;
revoke all on function dispatch_check_rpcs(text[]) from public;
grant execute on function dispatch_check_rpcs(text[]) to service_role;

-- Helper views for the orphan-bundle reaper sweeps.
create or replace view v_orphan_transcript_bundles as
  select s.id, s.created_at
    from vault.secrets s
   where s.name like 'minicrew_transcript_%'
     and not exists (
       select 1 from jobs j
        where j.final_transcript_bundle_id = s.id
           or (j.payload->>'transcript_bundle_id')::uuid = s.id
     );
revoke all on v_orphan_transcript_bundles from public;
grant select on v_orphan_transcript_bundles to service_role;

create or replace view v_orphan_mcp_bundles as
  select s.id, s.created_at
    from vault.secrets s
   where s.name like 'minicrew_mcp_%'
     and not exists (
       select 1 from jobs j
        where j.mcp_bundle_id = s.id
     );
revoke all on v_orphan_mcp_bundles from public;
grant select on v_orphan_mcp_bundles to service_role;

COMMIT;
