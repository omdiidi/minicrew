-- minicrew schema migration 002 — remote sub-agent foundations.
-- Idempotent. Safe to re-run. Wraps the entire change set in a single transaction so
-- partial application is impossible: any DDL failure rolls back, operators re-run after
-- fixing the underlying issue. See docs/SUPABASE-SCHEMA.md for operator notes
-- (db-schemas exposure for vault, direct_url role bypassrls requirement).

BEGIN;

-- ============================================================
-- Identity: drop enqueued_by, add submitted_by uuid.
-- ============================================================
alter table jobs drop column if exists enqueued_by;
alter table jobs add column if not exists submitted_by uuid;
create index if not exists jobs_submitted_by_status_idx
  on jobs (submitted_by, status) where status = 'running';

-- ============================================================
-- New columns
-- ============================================================
alter table jobs add column if not exists requested_status text
  check (requested_status is null or requested_status in ('cancel'));
alter table jobs add column if not exists progress jsonb;
alter table jobs add column if not exists caller_log_url text;
alter table jobs add column if not exists mcp_bundle_id uuid;

-- ============================================================
-- Cancel-sweep trigger: pending + requested_status='cancel' -> cancelled immediately.
-- Runs BEFORE INSERT/UPDATE on jobs; catches both newly-inserted and flipped rows.
-- ============================================================
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

-- ============================================================
-- Atomic claim with per-caller cap (replaces the GET+PATCH dance for callers with submitted_by).
-- For legacy rows (submitted_by IS NULL), cap is bypassed.
-- ============================================================
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

  -- expiry check
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
-- service_role calls this; no authenticated grant.

-- ============================================================
-- Vault RPCs (caller writes; worker reads via view; worker deletes via RPC).
-- ============================================================
create or replace function dispatch_register_mcp_bundle(p_secret jsonb)
  returns uuid language plpgsql security definer set search_path = vault, public as $$
declare v_id uuid;
begin
  -- vault.create_secret signature in current Supabase: (secret text, name text, description text)
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
-- service_role only.

-- ============================================================
-- RLS
-- ============================================================
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

-- Update policy: enforced via trigger because Postgres RLS can't restrict by column easily.
drop policy if exists jobs_caller_update on jobs;
create policy jobs_caller_update on jobs
  for update using (auth.uid() = submitted_by) with check (auth.uid() = submitted_by);

create or replace function enforce_caller_update_columns() returns trigger language plpgsql as $$
begin
  -- Allow service_role and superusers to change anything; restrict end-user callers
  -- to the requested_status column only. Use current_user/session_user (always
  -- reliable) instead of request.jwt.claim.role (PostgREST-version-dependent GUC).
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

-- ============================================================
-- Vault read access: explicit grant so the service role can SELECT
-- from the decrypted view. Without this, fetch_bundle 401s.
-- ============================================================
grant usage on schema vault to service_role;
grant select on vault.decrypted_secrets to service_role;

-- ============================================================
-- Storage bucket
-- ============================================================
insert into storage.buckets (id, name, public)
  values ('minicrew-logs', 'minicrew-logs', false)
  on conflict do nothing;

COMMIT;
