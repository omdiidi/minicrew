# Commands

Flat reference of every command available in a minicrew install. No prose, no
architecture. For the why-and-how, see the doc each command links to.

## Worker control

| Command | What |
|---|---|
| `bash setup.sh` | Install one or more worker instances on this host. |
| `bash teardown.sh` | Uninstall every worker on this host. |
| `python -m worker --preflight` | Verify host readiness (display, terminal, vault, github app, storage bucket). |
| `python -m worker --instance N --role primary\|secondary` | Run a worker in foreground (debug). |
| `python -m worker --status` | Print queue depth, worker statuses, recent errors from `worker_stats`. |
| `python -m worker.platform install --instance N --role primary\|secondary` | Install a single worker (what `setup.sh` calls). |
| `python -m worker.platform uninstall --instance N` | Uninstall a single worker. |
| `python -m worker.platform uninstall-all` | Uninstall every worker on this host. |
| `launchctl list \| grep com.minicrew` | Mac: list installed worker services. |
| `launchctl bootout gui/$(id -u)/com.minicrew.worker.N` | Mac: stop a single worker without uninstalling. |
| `systemctl --user status minicrew-worker-N.service` | Linux: status of one worker. |
| `journalctl --user -u minicrew-worker-N.service -n 200 --no-pager` | Linux: tail one worker's journal. |
| `tail -f logs/worker-N.log` | Follow a worker's stdout. |
| `tail -f logs/jobs/<job_id>.log` | Follow one job's tee'd Claude Code transcript. |

Examples:

```
bash setup.sh
python -m worker --preflight
python -m worker --instance 1 --role primary
python -m worker.platform uninstall --instance 2
tail -f logs/worker-1.log
```

## Dispatch (insert a job from the CLI)

Set `SUPABASE_URL` and one of `MINICREW_DISPATCH_JWT` / `SUPABASE_SERVICE_ROLE_KEY`
in the environment first. See [DISPATCH.md](./DISPATCH.md#quick-cli-testing--one-off-dispatch).

| Command | What |
|---|---|
| `python -m worker --dispatch ad_hoc --repo URL --sha SHA --prompt "..."` | Insert an `ad_hoc` job. Add `--wait` to block on terminal status. |
| `python -m worker --dispatch ad_hoc --prompt-base64 BASE64 ...` | Use base64-encoded prompt (bypass shell quoting; safer for prompts containing $, backticks, heredoc delimiters). Mutually exclusive with `--prompt`. |
| `python -m worker --dispatch handoff --repo URL --sha SHA --session-id UUID --bundle-id UUID` | Insert a `handoff` job. Bundle must be pre-registered via `dispatch_register_transcript_bundle`. |
| `python -m worker --dispatch ... --allow-code-push` | Allow the remote session to push a result branch. |

Example:

```
export SUPABASE_URL=https://<ref>.supabase.co
export SUPABASE_SERVICE_ROLE_KEY=<key>
python -m worker --dispatch ad_hoc \
  --repo https://github.com/owner/repo \
  --sha 36d5144da26f8a69c030ea60e7237a13ff7a2a85 \
  --prompt 'list py files; write {"count": N} to result.json' \
  --wait
```

## Config

| Command | What |
|---|---|
| `python -m worker.config.loader --validate ./worker-config` | Validate a config dir; non-zero exit on failure. |

Example:

```
python -m worker.config.loader --validate ./worker-config
```

## Skills (slash commands inside Claude Code, after `SETUP.md` Step 7/8)

Installed under `~/.claude/commands/minicrew/`. See [SUBAGENT-INTEGRATION.md](./SUBAGENT-INTEGRATION.md).

| Command | What |
|---|---|
| `/minicrew:setup` | Interactive walkthrough to install workers on this host. |
| `/minicrew:teardown` | Interactive walkthrough to uninstall. |
| `/minicrew:status` | Show queue + workers + recent errors. |
| `/minicrew:add-worker` | Add another worker instance (instances 2..5). |
| `/minicrew:add-machine` | Print the bootstrap sentence for a new host. |
| `/minicrew:add-job-type` | Append a new job_type entry to `worker-config/config.yaml`. |
| `/minicrew:scaffold-project` | Initialize `worker-config/` in a consumer repo. |
| `/minicrew:tune` | Adjust `model` / `thinking_budget` / `timeout_seconds` on existing job_types. |
| `/minicrew:dispatch <task>` | Send an ad_hoc task to a worker and block on the result. See [SUBAGENT-INTEGRATION.md](./SUBAGENT-INTEGRATION.md). |
| `/minicrew:fanout N <task>` | Run the same task across N parallel workers; optionally synthesize. See [SUBAGENT-INTEGRATION.md](./SUBAGENT-INTEGRATION.md). |

## Handoff (caller-side, dispatcher skill in user's dotfiles)

See [HANDOFF.md](./HANDOFF.md) for the user-facing how-to and
[DISPATCH.md](./DISPATCH.md#handoff) for the technical contract.

| Command | What |
|---|---|
| `/handoff [optional instruction] [allow_code_push=bool] [timeout=N]` | Hand off the current Claude Code session. |
| `/handoff:status <job_id>` | Show queue position, running state, latest progress line, live log URL. |
| `/handoff:reattach <job_id>` | Pull worker's continued transcript locally; print `claude --resume <session-id>`. |
| `/handoff:cancel <job_id>` | Cooperative cancel (sets `requested_status='cancel'`). |

Examples:

```
/handoff
/handoff finish the refactor we were just discussing
/handoff allow_code_push=true timeout=7200
/handoff:status 7c2e0a1b-3d4e-5f6a-7b8c-9d0e1f2a3b4c
/handoff:reattach 7c2e0a1b-3d4e-5f6a-7b8c-9d0e1f2a3b4c
/handoff:cancel 7c2e0a1b-3d4e-5f6a-7b8c-9d0e1f2a3b4c
```

## Database

Run via the Supabase SQL editor or `psql "$SUPABASE_DB_URL" -c "<sql>"`.

### Schema apply order

| Command | What |
|---|---|
| `apply schema/template.sql` | Fresh install: tables, indexes, functions, view. |
| `apply schema/migrations/002_remote_subagent.sql` | Phase 2 migration: identity, RLS, cancel, MCP, log URL, atomic claim, Vault RPCs, Storage bucket. |
| `apply schema/migrations/003_handoff.sql` | Phase 3 migration. **Apply AFTER 002.** Adds `final_transcript_bundle_id` + transcript RPCs + orphan views + `dispatch_check_rpcs` probe. |

### Inspecting state

```sql
-- fleet snapshot
select * from worker_stats;

-- recent jobs
select id, job_type, status, submitted_by, claimed_at
  from jobs order by created_at desc limit 20;

-- live progress for one job
select id, status, progress from jobs where id = '<job_id>';

-- live log URL (signed)
select caller_log_url from jobs where id = '<job_id>';
```

### Cancellation

```sql
update jobs set requested_status = 'cancel' where id = '<job_id>';
```

Pending → swept by trigger immediately. Running → owner heartbeat detects within
~10s.

### Handoff

```sql
-- fetch the worker's outbound transcript bundle (ownership-checked)
select dispatch_fetch_outbound_transcript('<job_id>'::uuid) as bundle;

-- extend a running handoff's timeout (capped at cfg.dispatch.handoff.max_timeout_seconds)
update jobs set payload = payload || '{"timeout_override_seconds": 28800}'::jsonb
 where id = '<job_id>';
```

## Dispatch (caller-side, from another Claude Code session)

The dispatcher skill (in user dotfiles) handles this; the SQL contract is below
in case you need to drive it manually.

### Register MCP bundle

```sql
select dispatch_register_mcp_bundle(
  '{"mcpServers": {"github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]}}}'::jsonb
);
-- returns uuid
```

### Register transcript bundle (handoff)

```sql
select dispatch_register_transcript_bundle(
  '{"session_id": "550e8400-e29b-41d4-a716-446655440000",
    "top_level": "<full jsonl text>",
    "subagents": {"sub1.jsonl": "<...>"}}'::jsonb
);
-- returns uuid
```

### Insert ad_hoc row

```sql
insert into jobs (job_type, payload, submitted_by, mcp_bundle_id) values (
  'ad_hoc',
  '{"prompt": "Refactor the slow query.",
    "repo": {"url": "https://github.com/acme/widgets",
             "sha": "8a1f7e0b9c4a4d2c1e3b9c8d7f6e5a4b3c2d1e0f"},
    "allow_code_push": false}'::jsonb,
  auth.uid(),
  (select dispatch_register_mcp_bundle('{"mcpServers": {}}'::jsonb))
) returning id;
```

### Insert handoff row

```sql
insert into jobs (job_type, payload, submitted_by, mcp_bundle_id) values (
  'handoff',
  jsonb_build_object(
    'repo', jsonb_build_object(
      'url', 'https://github.com/acme/widgets',
      'sha', '8a1f7e0b9c4a4d2c1e3b9c8d7f6e5a4b3c2d1e0f'),
    'session_id', '550e8400-e29b-41d4-a716-446655440000',
    'transcript_bundle_id', (select dispatch_register_transcript_bundle('{...}'::jsonb))::text,
    'allow_code_push', false
  ),
  auth.uid(),
  (select dispatch_register_mcp_bundle('{"mcpServers": {}}'::jsonb))
) returning id;
```

## Logs / debug

| Command | What |
|---|---|
| `cat <session_tmpdir>/_prompt.txt` | Exact bytes the session received. |
| `cat <session_tmpdir>/_run.sh` | Runner script the Terminal executed. |
| `ls ~/.claude/projects/` | Claude Code's per-project session transcript dirs. |
| `tail -f logs/jobs/<job_id>.log` | Live Terminal session transcript (tee'd). |

The session tmpdir is logged when the worker launches a session (event
`session_launched` in `logs/worker-N.log`). It is preserved until the worker's
cleanup sweep on the next claim cycle.
