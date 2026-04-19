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
  max_attempts int not null default 3,
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
