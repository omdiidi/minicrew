# Dispatch

## For LLMs

**What this covers:** The technical contract for `mode: ad_hoc` and `mode: handoff` —
the two modes that let a peer Claude Code session (the "caller") submit work to a
minicrew worker. RPC signatures, payload schema, MCP bundle shape, cleanup
responsibilities, retention behavior, trust model.

**Audience split:**

- For the user-facing **how-to** for handoff (prerequisites, `/handoff` usage,
  `/handoff:reattach` flow, gotchas), see [HANDOFF.md](./HANDOFF.md).
- For the per-prompt **rendering contract** (template variables, `_finalize`,
  result_schema, `_progress.jsonl`), see [PROMPTS.md](./PROMPTS.md).

**Invariants (do not change without coordinated SQL + worker edits):**

- The MCP bundle is `{"mcpServers": {...}}` only. The worker rejects any other
  top-level keys.
- The MCP bundle is registered via `dispatch_register_mcp_bundle(jsonb) returns uuid`.
  The transcript bundle is registered via `dispatch_register_transcript_bundle(jsonb)
  returns uuid` (10 MB server-side cap; matches `cfg.dispatch.handoff.max_transcript_bundle_bytes`).
- `dispatch_fetch_outbound_transcript(p_job_id uuid)` is the ONLY caller-callable
  transcript fetch path. Keyed by job_id (PK), ownership = `submitted_by =
  auth.uid()`. Never key by bundle_id from a caller — bundle_id is server-trusted.
- The caller deletes the dispatch branch they pushed; the worker deletes the MCP
  bundle on terminal outcome; the outbound transcript bundle is swept by the
  reaper after `cfg.dispatch.handoff.outbound_retention_days` (default 7).
- MCP-mediated tool calls inside the worker session execute under the **caller's**
  identity (caller's tokens are inside the bundle). Git operations execute under
  the **worker's** GitHub App.

## Quick CLI (testing / one-off dispatch)

For interactive testing without a dispatcher skill, the worker package ships a
`--dispatch` flag. Set `SUPABASE_URL` and one of `MINICREW_DISPATCH_JWT` /
`SUPABASE_SERVICE_ROLE_KEY` in the environment, then:

```bash
# ad_hoc — peer Claude Code session against a cloned repo
python -m worker --dispatch ad_hoc \
  --repo https://github.com/<owner>/<repo> \
  --sha <40-char-commit-sha> \
  --prompt 'count the markdown files; write {"md_count": N} to result.json' \
  --wait

# handoff — resume a captured local session on the worker
python -m worker --dispatch handoff \
  --repo https://github.com/<owner>/<repo> \
  --sha <40-char-commit-sha> \
  --session-id <uuid> \
  --bundle-id <transcript-bundle-uuid> \
  --wait
```

The CLI inserts the job, prints `{"job_id": ...}`, and (with `--wait`) blocks
until the job hits a terminal status. It does **not** register MCP bundles or
push the dispatch branch — those are still the dispatcher skill's job (see
[Caller responsibilities](#caller-responsibilities)). For a fresh-clone ad_hoc
job that doesn't need MCP servers, the CLI is the full happy path.

Use `MINICREW_DISPATCH_JWT` (caller's user JWT) over `SUPABASE_SERVICE_ROLE_KEY`
when possible; service-role bypasses RLS and the per-caller cap.

## Overview

End-to-end shape of dispatching from a peer Claude Code session:

```
caller session                                 worker
--------------                                 ------
1. take a snapshot of the local repo
2. push it to a dispatch branch on origin
3. register MCP bundle      ──RPC──>           Vault row
4. INSERT into jobs(...)    ──REST──>          jobs row (status=pending,
                                               no bundle pointers)
4a. dispatch_attach_bundles ──RPC──>           jobs row gets bundle pointers
                                               poll loop claims it
                                               clones dispatch branch via App token
                                               writes per-job .claude/settings.json
                                               renders builtin prompt
                                               launches Terminal
5. poll jobs.status         <──REST──          (running)
6. tail jobs.caller_log_url <──Storage──       (chunked log streamer)
                                               session writes result.json
                                               worker writes jobs.result
                                               (handoff: outbound transcript bundle)
                                               (ad_hoc with allow_code_push: pushes result branch)
                                               cleanup: delete MCP bundle, delete clone
7. fetch result branch      <──git──           (or fetch outbound transcript via RPC)
8. delete dispatch branch
```

Step 1 is the dispatcher skill's responsibility. Step 8 is also the dispatcher's
responsibility — the worker does not own the caller's git remote.

## Caller responsibilities

The caller (a Claude Code session running the dispatcher skill) owns:

1. **Snapshot.** Stash any local-only state, identify the SHA the worker should
   start from.
2. **Push the dispatch branch.** Pattern: `minicrew/dispatch/<job-uuid>` so the
   namespace is unambiguous.
3. **Register the MCP bundle.** RPC call:
   `select dispatch_register_mcp_bundle('{"mcpServers": {...}}'::jsonb);` returns
   a uuid the caller will attach in step 5.
4. **(handoff only) Register the transcript bundle.** RPC call:
   `select dispatch_register_transcript_bundle('{...}'::jsonb);` returns a uuid the
   caller will attach in step 5.
5. **INSERT the row, then attach bundles.** Two-step:
   - `INSERT INTO jobs(...)` with `submitted_by = auth.uid()`, the appropriate
     `payload`, and **NO** `mcp_bundle_id` / `final_transcript_bundle_id`
     (the RLS insert policy rejects rows that pre-set those columns to defend
     against cross-tenant pointer spoofing).
   - `select dispatch_attach_bundles('<job_id>'::uuid, '<mcp_uuid>'::uuid, '<transcript_uuid>'::uuid);`
     This SECURITY DEFINER RPC verifies you own the row and it is still
     `pending`, then writes `mcp_bundle_id` and merges
     `transcript_bundle_id` into `payload`. Pass `NULL` for either uuid you
     don't have. The transcript_bundle_id ends up in `payload` (where the
     worker reads it from); the dedicated `final_transcript_bundle_id` column
     is reserved for the worker's outbound write.
6. **Poll the row.** `SELECT id, status, progress, caller_log_url FROM jobs WHERE
   id = '<uuid>';` until the status is terminal.
7. **Tail the log.** Optional. `caller_log_url` is a signed Storage URL pointing at
   the chunked log manifest.
8. **Fetch the result.** For ad_hoc with `allow_code_push`: `git fetch origin
   minicrew/result/<job-uuid>`. For handoff: `select
   dispatch_fetch_outbound_transcript('<job-uuid>'::uuid);`.
9. **Delete the dispatch branch.** Always. The worker never deletes branches on
   the caller's behalf.

## Job payload shape

### `mode: ad_hoc`

```json
{
  "prompt": "Refactor the user-stats query to be index-friendly.",
  "repo": {
    "url": "https://github.com/acme/widgets",
    "sha": "8a1f7e0b9c4a4d2c1e3b9c8d7f6e5a4b3c2d1e0f"
  },
  "allow_code_push": false
}
```

Required: `prompt` (non-empty string), `repo.url` (HTTPS GitHub URL), `repo.sha`
(40-hex-char SHA). Optional: `allow_code_push` (default false).

When `allow_code_push: true`, the worker pre-creates `minicrew/result/<job-uuid>`
before launch and pushes it to origin if the session leaves commits on it. When
false, the worker `git remote remove origin` so the session cannot exfiltrate via
push.

### `mode: handoff`

```json
{
  "prompt": "I had to step away — finish the refactor we were discussing.",
  "repo": {
    "url": "https://github.com/acme/widgets",
    "sha": "8a1f7e0b9c4a4d2c1e3b9c8d7f6e5a4b3c2d1e0f"
  },
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "transcript_bundle_id": "11111111-2222-3333-4444-555555555555",
  "allow_code_push": false,
  "timeout_override_seconds": 7200,
  "idle_timeout_override_seconds": 1800
}
```

Required: `repo.url`, `repo.sha`, `session_id` (UUID matching the local Claude Code
session), `transcript_bundle_id` (UUID returned by
`dispatch_register_transcript_bundle`). Optional: `prompt` (omit for the default
preamble, "continue from where you left off"), `allow_code_push` (default false),
`timeout_override_seconds` (capped at `cfg.dispatch.handoff.max_timeout_seconds`),
`idle_timeout_override_seconds` (capped at the same).

## MCP bundle shape

The MCP bundle is a JSON object with exactly one top-level key:

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_…"}
    },
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"]
    }
  }
}
```

The worker writes this verbatim to a per-job `.claude/settings.json` (mode `0600`)
inside the cloned repo. Claude Code then sees the same MCP servers the caller had
configured.

The worker rejects bundles that contain ANY other top-level keys (`hooks`,
`permissions`, `model`, etc.) — only `mcpServers` is shipped.

## Cancellation

The caller cancels by setting the `requested_status` column:

```sql
update jobs set requested_status = 'cancel' where id = '<job_id>';
```

Behavior:

- **Pending jobs** are swept to `status = 'cancelled'` immediately by the
  `trg_sweep_cancel_pending` BEFORE-INSERT-OR-UPDATE trigger. No worker action
  required.
- **Running jobs** are detected by the owning worker's heartbeat thread, which
  polls its own claimed `requested_status` every ~10 seconds while busy. Once
  detected, the watchdog is signalled, the Terminal session is closed, and the
  row is set to `cancelled` (filtered on `status='running'` to prevent late
  PATCH races).

Realistic latency for a running cancel: 10s heartbeat + 15s watchdog poll + close
grace ≈ 30–45s. Pending cancels are instant.

## What the worker does

See [ARCHITECTURE.md](./ARCHITECTURE.md#the-ad_hoc-lifecycle) for the ad_hoc
lifecycle and [ARCHITECTURE.md](./ARCHITECTURE.md#the-handoff-lifecycle) for the
handoff lifecycle. The short version:

1. Claim the row atomically via `claim_next_job_with_cap` (per-caller cap on
   running jobs).
2. Mint a GitHub App install token. Clone the dispatch branch.
3. If `allow_code_push: false`, drop the `origin` remote.
4. If a `mcp_bundle_id` is set, fetch the bundle from Vault, write
   `.claude/settings.json`.
5. (handoff only) Fetch the transcript bundle, write `~/.claude/projects/<encoded>/<session>.jsonl`
   plus subagent JSONL files.
6. Render the built-in template, write `_prompt.txt`, write `_run.sh`.
7. Launch the Terminal. Start `ChunkedLogStreamer` and `ProgressTailer`.
8. On clean exit: read the result file, optionally push the result branch, write
   `jobs.result`. (handoff also: bundle the extended transcript outbound.)
9. Cleanup: delete MCP bundle, delete clone, delete live log Storage prefix
   (if `delete_logs_on_completion: true`).

## Trust model

- **RLS.** `jobs` has Row-Level Security enabled. Callers can SELECT/INSERT only
  rows where `submitted_by = auth.uid()`. Updates are restricted to the
  `requested_status` column via the `enforce_caller_update_columns` trigger
  (service_role and `postgres` bypass).
- **GitHub App scope.** The worker authenticates to GitHub via an App install token
  scoped to the org. The app needs `contents:write` on each target repo for ad_hoc
  push to work; `contents:read` is sufficient for read-only handoff/ad_hoc.
- **MCP secrets.** MCP bundles live in Supabase Vault (`vault.secrets`,
  `vault.decrypted_secrets`). The service role grants `usage on schema vault` and
  `select on vault.decrypted_secrets`; PostgREST's exposed schemas must include
  `vault` (Project Settings → API → Exposed schemas). The worker fetches via the
  decrypted view; callers register via the SECURITY DEFINER RPC.
- **Cleanup guarantees.** On terminal outcome (completed / error / cancelled), the
  MCP bundle is deleted via `dispatch_delete_mcp_bundle`. On non-terminal outcome
  (shutdown/requeue), the bundle is preserved so the next attempt can read it.
  The clone directory is wiped via `shutil.rmtree`. `~/.claude/projects/<encoded>`
  is wiped via `cleanup_session_data` BEFORE `rmtree` (load-bearing order: the
  encoded path is derived from the clone path).

## Handoff

This section adds the handoff-specific contract on top of the shared dispatch
shapes above.

### Inbound transcript bundle

The caller registers the bundle BEFORE inserting the jobs row:

```sql
select dispatch_register_transcript_bundle('{...}'::jsonb);
```

Bundle shape (JSON):

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "top_level": "<full contents of ~/.claude/projects/<encoded>/<session>.jsonl>",
  "subagents": {
    "subagent-001.jsonl": "<full contents>",
    "subagent-002.jsonl": "<full contents>"
  }
}
```

OR, for bundles too large to inline in Vault:

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "storage_ref": {
    "storage_key": "transcripts/<session>-<timestamp>.json.gz",
    "bucket": "minicrew-logs",
    "compressed_bytes": 1234567,
    "uncompressed_bytes": 5678901
  }
}
```

The worker validates the bundle against an allowlist:

- Top-level keys: subset of `{session_id, top_level, subagents, storage_ref}`.
- `session_id`: UUID string.
- `subagents`: object whose keys match `^[A-Za-z0-9_\-]+\.jsonl$`, max 64 files,
  max 5 MB per file.
- `storage_ref.storage_key`: string.

Server-side cap on register: 10 MB total serialized JSON
(`max_transcript_bundle_bytes` default). For bundles approaching this size the
caller should use the Storage path directly (the worker's outbound register helper
does this automatically; the caller's dispatcher skill should mirror that logic).

### Outbound transcript bundle

The worker writes its extended transcript back via the same
`dispatch_register_transcript_bundle` RPC, with Vault inline up to
`cfg.dispatch.handoff.vault_inline_cap_bytes` (default 512 KB) and Storage fallback
(gzip-compressed) above that. The Vault row id is then PATCHed onto
`jobs.final_transcript_bundle_id` (filtered on `id + worker_id + status='running'`).

The caller fetches the outbound bundle via:

```sql
select dispatch_fetch_outbound_transcript('<job_id>'::uuid) as bundle;
```

The RPC checks `submitted_by = auth.uid()` and returns the decrypted JSONB. If the
bundle has a `storage_ref`, the caller (dispatcher skill) downloads from the
Storage URL and gunzips before writing JSONLs to disk.

### Cleanup contract (handoff)

| Artifact                         | Owner                                                                              |
|----------------------------------|------------------------------------------------------------------------------------|
| `minicrew/dispatch/<uuid>` branch| **Caller** deletes after job terminal. Worker never touches caller's remote refs.  |
| `minicrew/result/<uuid>` branch  | **Caller** decides. Worker pushed it; caller can keep, merge, or delete.           |
| MCP bundle (Vault row)           | **Worker** deletes on terminal outcome via `dispatch_delete_mcp_bundle`.           |
| Inbound transcript bundle        | **Worker** deletes on terminal outcome IF `cfg.dispatch.handoff.delete_inbound_on_completion` (default true). |
| Outbound transcript bundle       | **Reaper** sweeps after `cfg.dispatch.handoff.outbound_retention_days` (default 7). NEVER deleted at job-end. |
| Live log Storage prefix `<job_id>/`| **Worker** deletes on terminal outcome IF `cfg.dispatch.log_storage.delete_logs_on_completion`. Otherwise reaper sweeps after `retention_days`. |
| Outbound Storage object `transcripts/<session>-<ts>.json.gz` | **Reaper** sweeps in lockstep with the Vault row pointing at it. |

### MCP fidelity caveat

The worker session sees only the MCPs in the per-job `.claude/settings.json` written
from the registered bundle. If the caller's local environment had MCPs not included
in the bundle, those tools are unavailable on the worker. Failures degrade
gracefully: Claude Code logs the missing tool and continues without it. **If H.0b
characterization (in `tmp/ready-plans/`) reveals that Claude Code hard-errors on
missing MCP-tool-call entries in a resumed JSONL, the worker strips those entries
before launch.** Track [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) for the operator
mitigation.

### Identity caveat

- **MCP-mediated tool calls** execute under the caller's identity. The MCP bundle
  contains the caller's tokens (e.g., a GitHub PAT inside an `env` map); any tool
  call those servers expose runs as the caller.
- **Git operations** (clone, push) execute under the worker's GitHub App. Branches
  pushed by the worker are authored by the App's bot identity, not the caller.

This split means the worker's read of the dispatch branch is auditable to the App
install; tool calls inside the session are auditable to the caller's tokens.
Single-tenant trust model: a worker host is trusted by every user that dispatches
to it.

### Cross-link

For the user-facing how-to (when to use `/handoff`, what `/handoff:reattach` does,
common gotchas), see [HANDOFF.md](./HANDOFF.md).
