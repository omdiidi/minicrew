# Troubleshooting

## For LLMs

Known failure modes organized as Symptom / Cause / Fix. Invariants to preserve: do not swap
symptoms for vague phrasing — the exact text is what a user greps their log for. When adding
new entries, follow the three-header pattern. Do not invent external URLs; point to other docs
in this repo or to generic product docs without specific URLs. When to open an issue goes last.

## Worker starts then exits immediately

### Symptom
`tail logs/worker-1.log` shows one line then nothing. `launchctl list | grep com.minicrew.worker`
shows the service but its process ID changes constantly (launchd is restarting it).

### Cause
`.env` is missing or `MINICREW_CONFIG_PATH` is not exported in the service environment.

### Fix
Open `.env` and confirm every variable from `.env.example` is populated. Re-run
`/minicrew:setup` to regenerate the launchd plist (it embeds the env vars into the plist).

## Worker logs "rejecting pooler URL" on startup

### Symptom
Startup fails with `rejecting pooler URL: <hostname contains 'pooler'>` or a similar message
from `worker/utils/db_url.py`.

### Cause
`SUPABASE_DB_URL` in `.env` is pointing at the pooler endpoint (port 6543). minicrew's reaper
needs the direct connection because advisory locks do not survive pgBouncer transaction
pooling.

### Fix
In the Supabase dashboard go to Project Settings, Database, Connection string, and copy the
entry labelled "direct connection" on port 5432. Paste it into `.env` and restart. See
`docs/SUPABASE-SCHEMA.md` for the walkthrough.

## Worker claims jobs but Terminal windows never open

### Symptom
Logs show `job_claimed` events but no `session_launched` events. Jobs sit in `status='running'`
until the reaper requeues them.

### Cause
Claude Code is not authenticated on this machine, or the trust dialog is blocking the unattended
launch.

### Fix
Run `claude` interactively once and complete the auth flow. If auth is fine but sessions still
do not launch, clear the trust entries for the job tempdirs: `rm ~/.claude.json` will reset
them (your auth persists separately). Re-run a job.

## Terminal windows open but Claude Code never runs

### Symptom
Windows pop up with the `_run.sh` script visible but Claude Code either hangs at a permission
prompt or never starts.

### Cause
Claude Code is prompting for permission despite `--dangerously-skip-permissions`. This usually
means the installed Claude Code version does not honor the flag in that mode.

### Fix
`npm update -g @anthropic-ai/claude-code` to the latest version. Verify with
`claude --version`.

## Jobs complete but `result` column is null

### Symptom
`jobs` row shows `status='completed'` but `result` is `null`.

### Cause
The job's prompt did not write the expected `result_filename` into the session's cwd. The
worker considers the job complete if the session exits cleanly even when no result file
appears.

### Fix
Inspect `logs/jobs/<job-id>.log` to see what the session actually did. Fix the prompt template
to explicitly instruct Claude Code to write the result file — most often the template is
ambiguous about the output path.

## Prompts over ~100KB fail to launch

### Symptom
Terminal window opens, `_run.sh` runs, but `claude` exits immediately with an argv-too-large
error or silently.

### Cause
macOS has a hard ~256KB argv size limit and `claude "$(cat _prompt.txt)"` inlines the entire
prompt into argv. Large prompts hit the limit.

### Fix
Do not inline large content. Write it to a separate file inside the session cwd (for example
`input.txt`) and reference the file from the prompt: `the content to process is in input.txt in
this directory`. The prompt template can itself write the input file before invoking the
content-bearing portion.

## Reaper never runs

### Symptom
A worker is clearly stuck (heartbeat stopped) but its jobs never get requeued. No
`reaper_ran` events in any log.

### Cause
The fleet has exactly one worker and that one worker is the stuck one. The reaper thread
inside the stuck process is also stuck.

### Fix
Add at least one more instance (`/minicrew:add-worker`) or one more machine
(`/minicrew:add-machine`). With two or more live instances the reaper self-heals. In the
meantime, launchd's `KeepAlive` should restart the stuck worker if it exits; if it is
deadlocked without exiting, kill it manually: `launchctl kickstart -k
gui/$UID/com.minicrew.worker.1`.

## `launchctl bootstrap` fails "Service already loaded"

### Symptom
`/minicrew:setup` or `bash setup.sh` reports `launchctl bootstrap failed: Service already
loaded` even after a teardown.

### Cause
`launchctl bootout` is asynchronous; a subsequent `bootstrap` can race against the still-pending
teardown.

### Fix
The engine retries this sequence three times with a short sleep. If it still fails after three
attempts, manually unload and retry: `launchctl bootout gui/$UID/com.minicrew.worker.1` (repeat
per instance), wait a few seconds, then re-run `/minicrew:setup`.

## `python -m worker --status` returns empty

### Symptom
The command exits with `{"workers": [], "queue_depth": 0, "recent_failures": 0}` even though
you believe a worker is running.

### Cause
No worker has ever written a heartbeat. Either none is installed, or every install failed
before reaching the heartbeat phase.

### Fix
Check `logs/worker-*.err` for startup errors captured by launchd. If that file is empty,
re-run `SETUP.md` from the top. If it has errors, triage them — typically `.env` or
`MINICREW_CONFIG_PATH` issues.

## Skills not recognized ("unknown command")

### Symptom
Claude Code session says `/minicrew:setup` is an unknown command, or tab-completion does not
find it.

### Cause
Skills are not installed under `~/.claude/commands/minicrew/`. This is done by `SETUP.md` step
6; a fresh clone without running setup has no skills registered.

### Fix
Run `SETUP.md` step 6 manually: copy every `*.md` file from this repo's `skills/` directory
into `~/.claude/commands/minicrew/`. For example `mkdir -p ~/.claude/commands/minicrew && cp
skills/*.md ~/.claude/commands/minicrew/`. Start a new Claude Code session and retry.

## Advisory lock never acquired

### Symptom
Every worker logs `reaper_ran` with `count_requeued=0` even when stale workers obviously exist,
or no worker ever logs `reaper_ran` at all.

### Cause
`SUPABASE_DB_URL` is wrong (points at the pooler or a non-existent host), or the database is
not reachable from the Mac Mini's network.

### Fix
Test the connection directly: `psql "$SUPABASE_DB_URL" -c "select 1"`. If that fails, fix the
URL or the network. If it succeeds, check that the URL is the direct connection per
`docs/SUPABASE-SCHEMA.md`.

## Linux

All Linux-specific failure modes from `python -m worker --preflight` and the systemd user
unit. Full runbook in `docs/LINUX.md`; this section is the symptom index.

### Preflight: `Wayland session detected — minicrew visible mode requires X11.`

#### Symptom
Worker refuses to start; `journalctl --user -u minicrew-worker-1.service` shows the preflight
error above.

#### Cause
The user logged into a Wayland session instead of Xfce-on-X11. `$XDG_SESSION_TYPE=wayland` is
a hard fail because `wmctrl` and `xdotool` cannot drive a Wayland compositor.

#### Fix
Log out. At the LightDM login screen, click the session-type selector and pick **Xfce
Session** (avoid any "Wayland" variant). Log back in, confirm `echo $XDG_SESSION_TYPE` prints
`x11`, and restart the unit.

### Preflight: `$DISPLAY is not set.`

#### Symptom
Preflight error references missing `DISPLAY`, typically when running under a systemd unit or
from an SSH session.

#### Cause
systemd user services do not inherit `DISPLAY` from the XFCE session automatically. The
worker's unit file must carry `Environment=DISPLAY=:0` and
`Environment=XAUTHORITY=%h/.Xauthority`.

#### Fix
`systemctl --user cat minicrew-worker-1.service` and confirm both Environment lines exist.
If running manually from SSH, `export DISPLAY=:0 XAUTHORITY=~/.Xauthority` first, or
`systemctl --user import-environment DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS` from inside
a real XFCE desktop terminal.

### Preflight: `missing required tool: wmctrl` (or `xdotool`, `xfce4-terminal`)

#### Symptom
Preflight fails with an explicit missing-tool message.

#### Cause
Required binaries are not installed.

#### Fix
`sudo apt install wmctrl xdotool xfce4-terminal tmux` and re-run
`python -m worker --preflight`.

### Window never opens (terminal exits before wmctrl finds it, or title gets clobbered by OSC 0)

#### Symptom
The worker claims jobs (you see `job_claimed` events) but no `session_launched` events
appear. Logs show the wmctrl poll loop timing out.

#### Cause
xfce4-terminal on some Mint builds double-forks, losing the initial PID. Or Claude Code
emits an OSC 0 escape sequence that rewrites the window title before wmctrl matches on it.

#### Fix
The Linux `_run.sh` prepends a one-second sleep before `exec`ing `claude` to give the poll
loop a title-stable window. If you still see this on slow hardware, increase
`platform.linux.window_open_timeout_seconds` in `config.yaml` from 15 to 30.

### Preflight: `$XDG_RUNTIME_DIR is not set`

#### Symptom
Running `bash setup.sh` from SSH rather than from a desktop terminal produces this error.

#### Cause
pam_systemd sets `XDG_RUNTIME_DIR` at graphical login; SSH sessions don't have one by
default.

#### Fix
Open `xfce4-terminal` from inside the actual XFCE desktop (via the Mint menu) and run setup
there. Alternatively, `export XDG_RUNTIME_DIR=/run/user/$(id -u)` and ensure the directory
actually exists — but for the first install it is cleaner to run from the desktop.

### `wmctrl -m` returns empty (broken X session)

#### Symptom
Preflight reports the window manager is not reachable even though `$DISPLAY` is set.

#### Cause
The X session itself is broken — crashed window manager, or a session type mismatch missed
by the explicit Wayland check.

#### Fix
Log out and back in. If the problem persists, drop to a TTY (`Ctrl+Alt+F3`) and run
`startxfce4` manually, or reboot.

### systemd unit won't start

#### Symptom
`systemctl --user status minicrew-worker-1.service` shows `failed`.

#### Cause
Preflight failure (see entries above), `.env` missing or unreadable,
`MINICREW_CONFIG_PATH` unset, or missing Python virtualenv.

#### Fix
Always look at the journal first:

```
journalctl --user -u minicrew-worker-1.service -n 200 --no-pager
```

Fix the root cause, then `systemctl --user reset-failed minicrew-worker-1.service` (the
`StartLimitBurst=5` throttle may have tripped), then
`systemctl --user restart minicrew-worker-1.service`.

### DO NOT run `sudo systemctl --user` (data-leak warning)

#### Symptom
Running `sudo systemctl --user …` appears to work but the unit behaves strangely; `.env`
contents show up in the root-visible journal.

#### Cause
`sudo systemctl --user` spawns a root user manager that reads the target user's `.env` as
root, bypassing the `chmod 600 .env` protection and leaking the contents into root's journal.

#### Fix
Always run `systemctl --user` as the owning uid (no `sudo`). If you need to inspect a unit
running under a different uid, `sudo machinectl shell user@.host` gives you a shell under
that uid without the env-leak. Rotate the Supabase service role key if you already leaked
`.env` into root's journal (see `SECURITY.md`).

## Dispatch (ad_hoc / handoff)

All failure modes specific to the `dispatch:` block. See [DISPATCH.md](./DISPATCH.md)
for the contract and [HANDOFF.md](./HANDOFF.md) for the user-facing handoff flow.

### Worker claims jobs but they instantly land `error` with "unknown job_type: X"

#### Symptom
Job goes pending → claimed → error with `error_message="unknown job_type: ad_hoc"`
(or `handoff`) within a couple of seconds. Worker stays running.

#### Cause
`worker-config/config.yaml` has no entry for `ad_hoc` / `handoff` under
`job_types`, OR the worker process loaded a stale config and has not been
restarted. The orchestration router rejects any job whose `job_type` is not in
`cfg.job_types` regardless of what `dispatch:` is configured to do.

#### Fix
1. Confirm `worker-config/config.yaml` has both entries (`mode: ad_hoc` and
   `mode: handoff`). The schema forbids `skill` and `prompt_template` keys on
   these modes — even `skill: null` fails validation. Run
   `python -m worker --validate worker-config` to confirm.
2. Hard-kill any stale worker process (`pkill -9 -f "python.*worker"`), then
   relaunch. Plain SIGTERM may leave a zombie holding the old config.

### Worker claim silently no-ops every poll cycle (jobs stay `pending` forever)

#### Symptom
A worker is heartbeating, but pending jobs are never claimed. `worker.log`
shows poll loop ticks but no `job claimed` events. Manual SQL of
`claim_next_job_with_cap` from psql succeeds and returns the row.

#### Cause
Argument-name mismatch between Python and SQL on the claim RPC. Symptom
appeared in v1 → v2 upgrades where the Python helper passed
`p_worker_version` while the SQL function expected `p_version`. PostgREST
reports a 4xx with no detail; the worker logs it but keeps polling.

#### Fix
Ensure `worker/db/queries.py` passes `p_version` (singular) in the
`claim_next_job_with_cap` RPC call. Re-run after `git pull`.

### `git clone` fails with "could not read Username for github.com"

#### Symptom
ad_hoc / handoff worker logs `clone failed: git operation failed: fatal: could
not read Username for 'https://github.com': Device not configured`. Preflight
passes; install-token mint succeeds; the failure is at `git clone` time.

#### Cause
GitHub's git smart-HTTP does not honor `Authorization: Bearer <token>` via
`http.extraheader`. It requires URL-embedded creds in the
`https://x-access-token:<token>@github.com/...` form (or an
`Authorization: Basic <base64(x-access-token:<token>)>` header). Older code
used the Bearer header — which GitHub silently ignores — so git fell through
to interactive credential prompt and failed.

#### Fix
Ensure `worker/integrations/github_app.py` builds an `auth_url` with
`x-access-token:<urllib.parse.quote(token)>@` and passes that URL to `git
clone`/`fetch`/`push`. The token is URL-encoded to handle reserved chars.
Origin is reset to the bare public URL after clone so the token doesn't sit
in `.git/config`.

### Worker fetch fails with PostgREST 404 on `vault.decrypted_secrets`

#### Symptom
Handoff (or ad_hoc with MCP bundle) errors with
`Client error '404 Not Found' for url '.../rest/v1/vault.decrypted_secrets?...'`.

#### Cause
The `vault` schema is not exposed by PostgREST and **must not** be exposed
(it would surface every encrypted secret in the project). Older worker code
read the view directly. Fixed in v4: the worker now calls
`dispatch_fetch_mcp_bundle(p_id)` and `dispatch_fetch_transcript_bundle(p_id)`
SECURITY DEFINER bridge RPCs in the `public` schema instead.

#### Fix
Apply `schema/migrations/004_bundle_fetch_rpc.sql` (idempotent) and pull the
worker code that calls the new RPCs. Re-run `python -m worker --check-rpcs` —
both new names should appear in `_REQUIRED_DISPATCH_RPCS` and report present.

### Preflight: GitHub App auth failure

#### Symptom
Worker startup fails with `dispatch_preflight: GitHubAppError: ...` — variants:
`no install token`, `expired private key`, `installation_id wrong`,
`401 Bad credentials`.

#### Cause
- `GITHUB_APP_PRIVATE_KEY` env var is missing, malformed, or stale.
- `GITHUB_APP_INSTALLATION_ID` does not match the App's installation on the org.
- Clock skew on the worker host caused the JWT `exp` to land in the past.

#### Fix
1. Re-export the private key from the GitHub App settings page (Settings → Apps
   → Edit → Private keys → Generate). PEM content goes into the env var.
2. Re-confirm the installation ID: open the App on the org, the URL is
   `https://github.com/organizations/<org>/settings/installations/<INSTALL_ID>`.
3. `date` on the worker host should be within ~30s of NTP. The JWT is generated
   with a 60s validity window.

### MCP bundle: Vault row missing

#### Symptom
`SecretBundleError: bundle <uuid> not found` in `logs/worker-N.log`.

#### Cause
Caller registered the bundle, INSERTed the jobs row, then the bundle was
deleted (manually, or by an aggressive orphan sweep) before the worker claimed
the row.

#### Fix
- Check `select * from vault.secrets where id = '<uuid>';` — empty confirms
  deletion.
- Caller must re-register and INSERT a new jobs row.
- If this happens repeatedly, raise the orphan-sweep threshold in the reaper
  (currently hardcoded to 24h; bump in `worker/core/reaper.py`).

### MCP bundle: not valid JSON

#### Symptom
`SecretBundleError: bundle <uuid> not valid JSON: Expecting value`.

#### Cause
Caller wrote a non-JSON string into Vault (most often a Python `repr` instead of
`json.dumps`).

#### Fix
Re-register the bundle with proper JSON. The worker reads
`vault.decrypted_secrets.decrypted_secret`, which is a `text` column; whatever
the caller passed to `dispatch_register_mcp_bundle(p_secret jsonb)` must round-trip
through `jsonb` cleanly.

### MCP bundle: disallowed top keys

#### Symptom
`SecretBundleError: bundle has disallowed top-level keys: {'hooks',
'permissions'}`.

#### Cause
The MCP bundle contract is strictly `{"mcpServers": {...}}`. Anything else —
hooks, permissions, model overrides, allowedTools — is rejected.

#### Fix
Strip the bundle to `{"mcpServers": {...}}` before registering. If the caller
needs to ship hooks or permissions, that's a feature gap; file an issue.

### Vault not exposed (PostgREST 404)

#### Symptom
`HTTPStatusError: 404 Not Found` when the worker tries
`GET /rest/v1/vault.decrypted_secrets?id=eq.<uuid>`.

#### Cause
The `vault` schema is not in PostgREST's exposed schemas.

#### Fix
Supabase project → Settings → API → **Exposed schemas** → add `vault`. Save and
restart the worker. (PostgREST reloads automatically; the worker just needs the
next claim cycle.)

### Storage bucket missing

#### Symptom
Preflight fails with `Storage bucket 'minicrew-logs' not found`.

#### Cause
The 002 migration's `insert into storage.buckets (id, name, public) values
('minicrew-logs', 'minicrew-logs', false) on conflict do nothing` did not run,
or the bucket was manually deleted.

#### Fix
Re-apply 002 (idempotent), or create the bucket via Supabase Dashboard:
Storage → New bucket → name `minicrew-logs`, public OFF.

### Storage bucket anon-readable

#### Symptom
Preflight refuses to start with `Storage bucket 'minicrew-logs' is anon-readable;
tighten policies to service-role-only.`

#### Cause
A Storage policy was added that grants anon SELECT on the bucket. Live job logs
and gzipped transcripts would be readable by anyone with the project URL.

#### Fix
Supabase project → Storage → minicrew-logs → Policies → remove any anon SELECT
policy. Re-run preflight.

### Operator MCP non-empty

#### Symptom
Preflight fails with `~/.claude/settings.json has non-empty mcpServers; minicrew
requires this to be empty for ad_hoc/handoff to be reproducible.`

#### Cause
The operator (the user the worker runs as) has MCP servers configured at the
user level. Those would leak into every dispatched session and break the
single-tenant trust assumption.

#### Fix
Edit `~/.claude/settings.json` and set `"mcpServers": {}`. If the operator needs
MCPs for their own interactive Claude Code sessions on the same host, configure
them per-project (in the project's `.claude/settings.json`) rather than at the
user level.

### Push 403

#### Symptom
ad_hoc job with `allow_code_push: true` completes, but `result.value.git.push_error`
contains `403 Forbidden` or `Resource not accessible by integration`.

#### Cause
The GitHub App lacks `contents:write` permission on the target repo, or the
installation does not include the target repo.

#### Fix
- GitHub App settings → Permissions → Repository permissions → **Contents:
  Read and write**. Apps can change permissions; users with the app installed
  must accept the new permission set.
- Installation page → confirm the target repo is in the App's repository
  selection (either "All repositories" or explicitly selected).

### Handoff: bundle missing or expired

#### Symptom
`/handoff:reattach <job_id>` fails with `transcript bundle missing or expired`,
or `select dispatch_fetch_outbound_transcript('<job_id>')` returns the same.

#### Cause
The reaper swept the outbound bundle. Default retention is 7 days
(`cfg.dispatch.handoff.outbound_retention_days`).

#### Fix
- For the lost bundle: nothing to recover — the encrypted blob is gone.
- For future jobs: bump `cfg.dispatch.handoff.outbound_retention_days` and
  restart workers.

### Handoff: session_id UUID mismatch

#### Symptom
Job ends with `error_message: transcript bundle session_id mismatch:
bundle=<x> payload=<y>`.

#### Cause
The caller registered a transcript bundle for one session_id but submitted a
jobs row pointing at the bundle with a different `payload.session_id`. The
worker checks both and refuses to launch with mismatched IDs (defense-in-depth
against bundle swapping).

#### Fix
The dispatcher skill should derive `payload.session_id` from the same source
as the bundle's `session_id`. If you're driving this manually, ensure both
match.

### Handoff: MCP unavailable on resume

#### Symptom
The resumed worker session logs `MCP server <name> not found` or fails on a
tool call.

#### Cause
The bundle didn't include that MCP, OR (per H.0b characterization) Claude Code
hard-errors on missing MCP-tool-call entries in the resumed JSONL.

#### Fix
If H.0b revealed graceful degradation, the worker continues without the tool.
If H.0b revealed a hard error, the worker strips MCP-tool-call entries from
the on-disk JSONL before launch (see `worker/orchestration/handoff.py` step
"H.0b fallback hook"). If you see the hard-error variant in production with no
strip happening, the H.0b hook is not active — file an issue.

### Handoff: Storage download failure on reattach

#### Symptom
`/handoff:reattach <job_id>` prints `failed to download
transcripts/<session>-<ts>.json.gz from Storage`.

#### Cause
The outbound bundle had a `storage_ref` (was over `vault_inline_cap_bytes`).
The Storage object is missing — either the live-log Storage retention sweep
mistakenly deleted it (it should not — outbound transcripts live under
`transcripts/`, not `<job_id>/`), or the Storage bucket policy changed.

#### Fix
Verify the object exists: `select * from storage.objects where bucket_id =
'minicrew-logs' and name = 'transcripts/<session>-<ts>.json.gz';`. If missing,
nothing to recover. If present, check Storage policies allow service-role
SELECT.

### "git operation failed: refusing to clone non-https-github URL"

#### Symptom
ad_hoc / handoff job fails late with `git operation failed: refusing to clone
non-https-github URL`. Logs show the worker had already fetched the MCP bundle
and written per-job `.claude/settings.json` before the failure.

#### Cause
The orchestrator validates `payload.repo.url` against `^https://github.com/`
only when the clone subprocess starts — i.e. after the MCP bundle has been
fetched and the per-job `.claude/settings.json` materialized. A bad URL is
discoverable up-front but currently surfaces late, wasting the bundle fetch.

#### Fix
Validate the URL pattern at submission time (caller-side dispatcher skill)
before INSERT. Reject anything that does not match `^https://github.com/<owner>/<repo>(\.git)?$`
client-side. Worker-side validation remains the security boundary; this is an
operator quality-of-life improvement.

### Handoff: cap exceeded

#### Symptom
INSERT into jobs fails with `payload.timeout_override_seconds: 90000 exceeds
maximum 86400`.

#### Cause
The schema rejects `timeout_override_seconds` and `idle_timeout_override_seconds`
above the hard cap. `cfg.dispatch.handoff.max_timeout_seconds` (default 86400)
is the source of truth; the JSON Schema in `schema/config.schema.json` mirrors
it.

#### Fix
Either reduce the requested override below 86400, or bump
`cfg.dispatch.handoff.max_timeout_seconds` AND the corresponding `maximum` in
the payload schema. Restart workers + re-validate config.

## `/minicrew:dispatch` or `Task("Minicrew Mac Mini", ...)` refused with "MINICREW_INSIDE_WORKER=1"

### Symptom
Dispatch returns immediately with `error: refusing dispatch from inside a
minicrew worker session (MINICREW_INSIDE_WORKER=1)` (CLI exit 2), or the
custom agent reply contains `{"error": "refused: cannot dispatch from inside
a minicrew worker session"}`.

### Cause
The recursion guard is firing. The runner script that started this Claude
Code session exports `MINICREW_INSIDE_WORKER=1` to prevent workers from
spawning workers indefinitely. Either:
- You're legitimately inside a worker session and the guard is doing its
  job (most common), OR
- The env var is set in your shell rc and is leaking into a non-worker
  session, OR
- You manually set the var and forgot.

### Fix
For a one-off override (use sparingly): `unset MINICREW_INSIDE_WORKER` in
the current shell, then re-dispatch. To check whether you're really inside
a worker: `pgrep -fl "python -m worker"` should return non-empty if you
are. If the var is leaking from your shell rc, remove the export from
`~/.zshrc` / `~/.bashrc`.

## Rolling back the subagent-integration layer

### Symptom
The custom agent / slash skills / CLAUDE.md routing fragment misbehave
(auto-dispatching when they shouldn't, prose discovery never firing,
etc.) and you want to disable the entire integration without uninstalling
the worker engine.

### Cause
Three install surfaces accumulate state:
- `~/.claude/commands/minicrew/dispatch.md`, `fanout.md`, `routing-rules.md`
- `~/.claude/agents/minicrew-mac-mini.md`
- The `@~/.claude/commands/minicrew/routing-rules.md` import in any
  consumer CLAUDE.md.

### Fix
```bash
rm ~/.claude/commands/minicrew/dispatch.md
rm ~/.claude/commands/minicrew/fanout.md
rm ~/.claude/commands/minicrew/routing-rules.md
rm ~/.claude/agents/minicrew-mac-mini.md
# Then in any consumer CLAUDE.md that imported routing-rules.md, remove the line:
#   @~/.claude/commands/minicrew/routing-rules.md
```
The `python -m worker --dispatch` CLI keeps working (it's the load-bearing
primitive); only the slash-command, custom-agent, and prose-discovery
surfaces are removed.

## Caller-side base64 extraction raises UnicodeDecodeError

### Symptom
Calling Claude received a `Task(subagent_type="Minicrew Mac Mini", ...)`
reply, extracted the body between `===MINICREW_B64_BEGIN===` markers, and
the decoder raised `UnicodeDecodeError`.

### Cause
The shipped extraction helper in `docs/SUBAGENT-INTEGRATION.md` uses
`.decode("utf-8", "strict")`, which raises immediately on invalid bytes.
A worker that returned non-UTF-8 bytes (binary blob, mis-encoded text)
surfaces the failure rather than silently substituting `U+FFFD`.

### Fix
This is intentional. To accept lossy decode (replace invalid bytes with
`U+FFFD`), change `"strict"` to `"replace"` in your local copy of the
extraction helper. Strict mode is recommended for any caller that
processes the result programmatically — silent corruption is harder to
debug than a raised exception.

## Operator: driving Mac Mini via Chrome Remote Desktop

### Symptom
Shell commands typed into the Mac Mini terminal via Chrome Remote Desktop arrive
mangled: uppercase letters become lowercase, `&` becomes `7`, `:` becomes `;`,
`(` becomes `9`, `&&` becomes `77`. Clipboard pastes re-inject stale content.

### Cause
Chrome Remote Desktop drops the Shift modifier between keypresses on macOS
hosts. `type_text` (or any keystroke-stream input) is affected. The clipboard
sync between local and remote is sticky/cached.

### Fix
- Use lowercase-only commands when typing through Remote Desktop.
- Use `press_key(":")` (or any other Shift-required single character) for the
  one-off shifted chars in URLs (`https:` → `https COLON //`).
- Avoid `&&` (use `;` chains).
- Better: enable SSH on the Mac Mini, OR upload longer scripts to a public
  Supabase Storage bucket and have the remote shell `curl` and run them
  (avoids any keystroke fidelity issues entirely).

## When to open an issue

If you hit a failure mode not covered above, capture `logs/worker-<instance>.log` and
`logs/worker-<instance>.err` (with any secret values redacted), note the git SHA of your
minicrew checkout, and open an issue on the GitHub repository. Include the smallest
`config.yaml` and prompt template that reproduces the problem.
