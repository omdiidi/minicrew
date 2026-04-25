-- ============================================================
-- 004_bundle_fetch_rpc.sql
--
-- Worker-side bundle fetch was reading vault.decrypted_secrets directly via
-- PostgREST. That requires exposing the `vault` schema to PostgREST, which is
-- a broad surface (all vault rows, not just minicrew bundles). We replace
-- that read with two SECURITY DEFINER RPCs in `public`, mirroring the
-- existing register/delete pattern. PostgREST only ever sees `public`.
--
-- Worker switches from
--   GET /rest/v1/vault.decrypted_secrets?id=eq.<uuid>&select=decrypted_secret
-- to
--   POST /rest/v1/rpc/dispatch_fetch_mcp_bundle           {p_id: uuid}
--   POST /rest/v1/rpc/dispatch_fetch_transcript_bundle    {p_id: uuid}
--
-- Both return TEXT (the raw decrypted_secret JSON). The worker validates the
-- shape; these RPCs are pure key-by-uuid lookups and trust the worker as
-- service_role caller.
-- ============================================================

create or replace function public.dispatch_fetch_mcp_bundle(p_id uuid)
  returns text language plpgsql security definer set search_path = vault, public as $$
declare
  v_secret text;
begin
  select decrypted_secret into v_secret
    from vault.decrypted_secrets
    where id = p_id;
  if v_secret is null then
    raise exception 'mcp bundle % not found', p_id using errcode = 'no_data_found';
  end if;
  return v_secret;
end;
$$;

revoke all on function public.dispatch_fetch_mcp_bundle(uuid) from public;
-- service_role only; worker never calls this with a user JWT.
grant execute on function public.dispatch_fetch_mcp_bundle(uuid) to service_role;


create or replace function public.dispatch_fetch_transcript_bundle(p_id uuid)
  returns text language plpgsql security definer set search_path = vault, public as $$
declare
  v_secret text;
begin
  select decrypted_secret into v_secret
    from vault.decrypted_secrets
    where id = p_id;
  if v_secret is null then
    raise exception 'transcript bundle % not found', p_id using errcode = 'no_data_found';
  end if;
  return v_secret;
end;
$$;

revoke all on function public.dispatch_fetch_transcript_bundle(uuid) from public;
grant execute on function public.dispatch_fetch_transcript_bundle(uuid) to service_role;


-- Update the dispatch_check_rpcs verifier inputs (referenced by --check-rpcs).
-- The Python side _REQUIRED_DISPATCH_RPCS list is updated to include both new
-- names; SQL itself doesn't need a verifier-list change.
