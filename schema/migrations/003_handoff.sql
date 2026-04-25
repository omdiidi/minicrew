-- minicrew schema migration 003 — handoff (mode: handoff) foundations.
-- Apply AFTER 002_remote_subagent.sql. Idempotent. Wraps the entire change set in
-- one transaction so partial application is impossible.

BEGIN;

-- ============================================================
-- Outbound transcript bundle column + indexes.
-- ============================================================
alter table jobs add column if not exists final_transcript_bundle_id uuid;

-- Unique partial index — guarantees one job per outbound bundle (defends ownership lookup).
create unique index if not exists jobs_final_transcript_bundle_id_uq
  on jobs (final_transcript_bundle_id)
  where final_transcript_bundle_id is not null;

-- Lookup index for orphan-inbound sweeper (used by reaper).
create index if not exists jobs_payload_transcript_bundle_id_idx
  on jobs ((payload->>'transcript_bundle_id'))
  where payload ? 'transcript_bundle_id';

-- ============================================================
-- Re-create the caller insert policy from 002 to also forbid
-- final_transcript_bundle_id at insert time. Pairs with
-- dispatch_attach_bundles below (the only allowed attach path).
-- ============================================================
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

-- ============================================================
-- Caller-callable: attach bundle pointers AFTER insert. Bypasses
-- enforce_caller_update_columns via SECURITY DEFINER. Refuses any
-- attach that targets a non-pending row or a row not owned by the
-- caller. transcript_bundle_id is written into payload (where the
-- worker reads it from) rather than a dedicated column.
-- ============================================================
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

-- ============================================================
-- Re-create the column-restriction trigger from 002 with the
-- new column included in the comparison tuple. Trigger from 002
-- already attached; CREATE OR REPLACE on the function suffices.
-- ============================================================
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

-- ============================================================
-- Caller-callable: register an inbound transcript bundle.
-- Server-side size guard prevents Vault DoS.
-- The size cap value MUST match cfg.dispatch.handoff.max_transcript_bundle_bytes;
-- operator updates this RPC (or a config-driven function param) when changing the cap.
-- ============================================================
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

-- ============================================================
-- Caller-callable: fetch the OUTBOUND transcript bundle for a job they own.
-- Keyed by job_id (primary key), NOT bundle_id. Eliminates the predicate-injection
-- and bundle-spoofing surfaces from review.
-- ============================================================
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

-- ============================================================
-- Worker-only: delete a transcript bundle (any direction). Service role bypasses RLS;
-- direct grant restricted to service_role.
-- ============================================================
create or replace function dispatch_delete_transcript_bundle(p_id uuid)
  returns void language plpgsql security definer set search_path = vault, public as $$
begin
  delete from vault.secrets where id = p_id;
end;
$$;
revoke all on function dispatch_delete_transcript_bundle(uuid) from public;
-- service_role only — no `grant execute to authenticated`.

-- ============================================================
-- Preflight RPC: check that a list of function names exists in pg_proc.
-- Returns the subset that are MISSING. Used by worker preflight without
-- relying on error-message string parsing.
-- ============================================================
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

-- ============================================================
-- Helper views for the orphan-bundle reaper sweeps.
-- Lists vault secrets whose name pattern matches our bundles
-- and have NO jobs row referencing them.
-- ============================================================
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
-- service_role reads only.

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
-- service_role reads only. Used by Phase 3's reaper sweep to clean up MCP
-- bundles registered via dispatch_register_mcp_bundle() but never attached to
-- a job (caller-side INSERT failed after Vault registration succeeded).

COMMIT;
