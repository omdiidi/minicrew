# Handoff

## What handoff is

You're in the middle of a Claude Code session. Something pulls you away — a meeting,
a flight, a kid, sleep. You don't want to abandon the session and you don't want to
keep your laptop awake babysitting it. Handoff lets you ship the live session to a
remote minicrew worker, which resumes it via `claude --resume <session-id> --print`,
keeps working, writes back its extended transcript, and waits for you. When you come
back you run `/handoff:reattach <job_id>`, the dispatcher pulls the worker's
continued JSONL into your local `~/.claude/projects/`, and you run `claude --resume
<session-id>` to step back into the conversation as if you'd never left.

This is different from [`mode: ad_hoc`](./DISPATCH.md): ad_hoc starts a fresh
Claude Code session with a one-shot prompt. Handoff continues an existing one,
preserving the full conversational context, tool-use history, and any subagent
sessions.

## Prerequisites

Before you can use `/handoff` you need:

1. **Phase 2 deployed.** A minicrew fleet with `dispatch:` configured in
   `worker-config/config.yaml`. See [SETUP.md](../SETUP.md) for first-time install
   and [docs/CONFIG-REFERENCE.md](./CONFIG-REFERENCE.md#dispatch) for the dispatch
   block.
2. **003 migration applied.** `apply schema/migrations/003_handoff.sql` in the
   Supabase SQL editor (or `psql -f`). Apply AFTER `002_remote_subagent.sql`. See
   [SUPABASE-SCHEMA.md](./SUPABASE-SCHEMA.md#how-to-apply).
3. **Dispatcher skill installed in your dotfiles.** The `/handoff`,
   `/handoff:status`, `/handoff:reattach`, and `/handoff:cancel` commands live in
   your personal `~/.claude/commands/` (NOT in this repo). They are caller-side
   tools.
4. **GitHub App installed on the org.** The same App used for ad_hoc dispatch.
   Needs `contents:read` (and `contents:write` if you want `allow_code_push=true`)
   on the repo you're working in.
5. **MCP bundle setup.** Your operator has a way to capture your session's MCP
   server config (the `mcpServers` map from your local `.claude/settings.json`)
   and ship it to Vault as part of the handoff. The dispatcher skill handles
   this.
6. **Supabase Vault + Storage configured.** The `vault` schema must be added to
   PostgREST's exposed schemas (Project Settings → API → Exposed schemas) and the
   `minicrew-logs` bucket must exist (created by the 002 migration; verified by
   `python -m worker --preflight`).
7. **Operator's `~/.claude/settings.json` has `mcpServers: {}`.** This is the
   "operator-empty rule" — the worker host MUST NOT have MCP servers configured at
   the user level, because those would leak into every dispatched session and make
   handoffs non-reproducible. Preflight enforces this.

## The /handoff command

```
/handoff                                          # default preamble
/handoff finish the refactor we discussed and push when done
/handoff allow_code_push=true
/handoff timeout=7200                             # 2h cap instead of 4h default
/handoff allow_code_push=true timeout=7200 finish the refactor and push when done
```

Argument summary:

| Arg                          | Default     | Meaning                                                                |
|------------------------------|-------------|------------------------------------------------------------------------|
| (free text)                  | empty       | Optional instruction. Empty → "continue from where you left off."      |
| `allow_code_push=bool`       | `false`     | If true, worker pre-creates a result branch and pushes it on success.  |
| `timeout=N`                  | 14400 (4h)  | Hard cap on wall-clock seconds. Capped at `cfg.dispatch.handoff.max_timeout_seconds` (default 86400). |

The dispatcher skill prints the `job_id` it created so you can track it.

## What happens during handoff

When you run `/handoff`:

1. **Caller-side snapshot.** The dispatcher skill commits any uncommitted edits
   into a snapshot commit on a fresh branch named `minicrew/dispatch/<job-uuid>`.
2. **Push.** The branch is pushed to `origin`. The worker will clone from this
   ref.
3. **Bundle MCP.** Your local `~/.claude/settings.json` `mcpServers` map is
   uploaded to Vault via `dispatch_register_mcp_bundle(jsonb)`. You get back a
   uuid.
4. **Bundle transcript.** Your current session's transcript files
   (`~/.claude/projects/<encoded-pwd>/<session-id>.jsonl` plus any subagent
   `.jsonl` files) are uploaded to Vault via
   `dispatch_register_transcript_bundle(jsonb)`. You get back a uuid. If the bundle
   is over `vault_inline_cap_bytes` (default 512 KB), the dispatcher gzips it and
   uploads to the `minicrew-logs` Storage bucket, registering only a reference in
   Vault.
5. **INSERT.** A single `jobs` row is created with `mode='handoff'`, your
   `mcp_bundle_id`, and `payload.transcript_bundle_id`. RLS ensures `submitted_by =
   auth.uid()`.
6. **Poll.** The dispatcher prints the job_id and either exits (so you can leave)
   or stays in foreground tailing `caller_log_url` while the worker runs your
   continued session.

The worker's side is described in [ARCHITECTURE.md](./ARCHITECTURE.md#the-handoff-lifecycle).

## Tracking a handoff

```
/handoff:status <job_id>
```

Prints something like:

```
job_id: 7c2e0a1b-…
status: running
worker: worker-mini2-1
started: 2026-04-24T14:03:11Z
elapsed: 00:42:18
progress: {"phase": "running tests", "step": 4, "of": 7}
log_url: https://<project>.supabase.co/storage/v1/object/sign/minicrew-logs/7c2e0a1b-…/manifest.json?token=…
```

The `log_url` is a signed URL pointing at the chunked log manifest. You can `curl`
the manifest, then `curl` each chunk, to read what the worker session is doing in
real time. (The dispatcher skill `/handoff:status --follow` does this for you.)

## Reattaching when you get back

```
/handoff:reattach <job_id>
```

What this does:

1. Calls `select dispatch_fetch_outbound_transcript('<job_id>'::uuid);` to get the
   worker's extended transcript bundle (Vault inline OR Storage reference).
2. If your local `~/.claude/projects/<encoded-pwd>/<session-id>.jsonl` already
   exists (it will — you ran the session locally before handing off), the
   dispatcher backs it up to:
   ```
   ~/.claude/projects/<encoded-pwd>/<session-id>.local-backup-<ISO-timestamp>.jsonl
   ```
   This always happens, even if the local file appears unchanged. The backup is
   your insurance against the worker overwriting an edit you made between
   handoff and reattach.
3. If your local mtime is **newer** than the worker bundle's stored timestamp
   (`vault.secrets.created_at`), the dispatcher prints a warning before
   overwriting. You may have made local edits during a window where the worker
   was already running; review the backup before discarding.
4. Writes the worker's `top_level` JSONL to `<session-id>.jsonl` and the worker's
   subagent JSONLs to `<session-id>/subagents/<filename>.jsonl`.
5. Prints the resume command:
   ```
   claude --resume <session-id>
   ```

Run that command and you're back in the conversation. All the worker's tool calls,
file edits, and subagent invocations are visible in the transcript.

## Cancelling a handoff

```
/handoff:cancel <job_id>
```

Or directly via SQL:

```sql
update jobs set requested_status = 'cancel' where id = '<job_id>';
```

Behavior:

- **Pending handoff** (worker hasn't claimed it yet): swept to `cancelled`
  immediately by the BEFORE-trigger.
- **Running handoff**: detected by the owning worker's heartbeat thread within
  ~10s, then the watchdog closes the Terminal session within another ~15s. The
  worker still attempts a best-effort outbound bundle so partial work is
  recoverable via `/handoff:reattach`.

## Common gotchas

### "I lost a 30s window of local edits after reattach"

`/handoff:reattach` always backs up your local JSONL before overwriting:

```
~/.claude/projects/<encoded-pwd>/<session-id>.local-backup-<ISO-timestamp>.jsonl
```

To recover edits you made between handoff and reattach: `cat` the backup, find the
JSONL events you care about, and decide whether to merge them in manually. There
is no automatic merge — the two sides diverged the moment the worker started.

### "Reattach says 'transcript bundle missing or expired'"

Outbound transcript bundles live in Vault for `cfg.dispatch.handoff.outbound_retention_days`
days (default 7). After that the reaper sweeps them. If you reattach more than 7
days after the worker completed:

- Bump `cfg.dispatch.handoff.outbound_retention_days` for future jobs.
- Restart workers to pick up the new value.
- For the lost bundle: nothing to recover — the encrypted blob is gone.

### "MCP X tool was unavailable during my handoff"

Two possibilities:

1. **Your bundle didn't include MCP X.** The dispatcher skill captured your
   `mcpServers` map at handoff time. If MCP X was added to your local config AFTER
   the handoff, the worker doesn't see it. Re-handoff to refresh.
2. **The worker's MCP environment differs from yours.** The operator-empty rule
   says the worker's `~/.claude/settings.json` `mcpServers` MUST be empty. If the
   operator added something there, it's only in your bundle's MCPs that count.
   The worker session has access to exactly what your bundle shipped — nothing
   more, nothing less.

### "The worker session never started using my MCPs"

The per-job `.claude/settings.json` mechanism writes your bundled `mcpServers` to
`<clone>/.claude/settings.json` BEFORE launching `claude --resume`. Claude Code
reads project-local settings on startup. If the resumed session never invokes a
tool from your MCPs, that's a session-side decision, not a config-loading bug —
inspect `logs/jobs/<job_id>.log` to see if Claude Code logged a tool registration
or a tool error.

If you instead see `MCP server <name> not found` in the log: the operator's
preflight should have caught this; report it.

## Privacy notes

- **Encryption.** Everything in `vault.secrets` (MCP bundles, transcript bundles)
  is encrypted at rest by Supabase Vault. Only the service_role and the
  ownership-checked RPC (`dispatch_fetch_outbound_transcript`) can decrypt.
- **Storage.** Live job logs and gzipped large transcripts live in the
  `minicrew-logs` Storage bucket, which is service-role-only. Preflight verifies
  anon access is blocked. Signed URLs (with short TTL) are how callers tail.
- **Retention.**
  - Live job logs: `cfg.dispatch.log_storage.retention_days` (default 7) or
    immediately on `delete_logs_on_completion: true`.
  - Outbound transcript bundles: `cfg.dispatch.handoff.outbound_retention_days`
    (default 7).
  - Inbound transcript bundles: deleted on terminal outcome if
    `cfg.dispatch.handoff.delete_inbound_on_completion` (default true).
  - Orphan bundles (caller registered but INSERT failed): swept after 24h via
    `v_orphan_transcript_bundles` / `v_orphan_mcp_bundles` views.
- **Who can read what.** RLS on `jobs` ensures callers see only their own rows.
  `dispatch_fetch_outbound_transcript` enforces ownership at the function level.
  Operators with service_role keys can read everything.

## FAQ

**Can I hand off mid-tool-call?**
Yes. The resumed session sees the pending tool call in the transcript. The default
preamble explicitly tells it to "exercise judgment: proceed with the obvious next
step, or capture the question and exit." If you want different behavior, pass an
explicit instruction: `/handoff if you hit any decision point, write it to the
result file and exit`.

**Can I hand off twice with the same session?**
Yes. The first handoff's outbound bundle becomes orphaned (the second handoff
writes a new `final_transcript_bundle_id` to the same row OR creates a new jobs
row). The reaper sweeps the orphan after retention.

**Can I extend a running handoff's timeout?**
Yes:

```sql
update jobs set payload = payload || '{"timeout_override_seconds": 28800}'::jsonb
 where id = '<job_id>';
```

The watchdog re-reads the cap on its next tick. Capped at
`cfg.dispatch.handoff.max_timeout_seconds` (default 86400 / 24h). The schema
rejects values above 86400 at INSERT time; if you hit "cap exceeded" via this
SQL, the worker logs the rejection and continues with the previous cap.

**Can I hand off to a worker I don't trust?**
The trust model is single-tenant: every worker the user dispatches to runs under
the user's identity (caller's tokens are inside the MCP bundle). Don't dispatch
to a host you wouldn't paste your tokens into.

## Examples

### Code research handoff (no push)

You're investigating why a query is slow. You want the worker to keep poking at
it while you go to lunch.

```
/handoff keep digging into the slow user-stats query — try the index ideas in the
plan above, run benchmarks, write findings to the result file
```

When you reattach, you have:
- The full transcript of the worker's investigation.
- A `result.json` with the worker's findings (in `jobs.result`).
- No code changes pushed (default `allow_code_push=false`).

### Plan review handoff (no push)

You drafted a plan and want a second pass while you sleep.

```
/handoff review the implementation plan we just wrote, list every risk you can
think of, score each by impact/likelihood, write to the result file
```

The worker reasons about the plan, possibly using your MCP tools to check
external docs. You reattach to a transcript full of analysis plus a structured
risk list in `jobs.result`.

### Full implementation handoff (with push)

You agreed on what to build. You want the worker to actually build it.

```
/handoff allow_code_push=true implement the changes in the plan above, run the
existing test suite, fix anything you break, then exit
```

The worker:
- Pre-creates `minicrew/result/<job-uuid>` before launch.
- Implements the changes on that branch.
- Runs tests.
- On clean exit, the worker pushes the branch.

You reattach, then `git fetch origin minicrew/result/<job-uuid>` and review the
diff before merging.
