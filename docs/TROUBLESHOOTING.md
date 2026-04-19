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
Claude Code is not authenticated on this machine, or the trust dialog is blocking the headless
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

## When to open an issue

If you hit a failure mode not covered above, capture `logs/worker-<instance>.log` and
`logs/worker-<instance>.err` (with any secret values redacted), note the git SHA of your
minicrew checkout, and open an issue on the GitHub repository. Include the smallest
`config.yaml` and prompt template that reproduces the problem.
