# Architecture

## For LLMs

This file describes engine internals and runtime topology for minicrew. Load-bearing invariants:
the atomic-claim UPDATE with WHERE status='pending' is the sole coordination primitive for claim;
the reaper must hold `pg_try_advisory_xact_lock` before writing; heartbeats upsert on conflict id;
worker id format is `<prefix>-<hostname>-<instance>`. Do not change these without a schema
migration plan. The file layout section mirrors the on-disk tree; keep it in sync when modules move.

## The two-surface product

minicrew ships two inseparable surfaces. The **engine** is a Python daemon that polls a Supabase
job queue, claims jobs atomically, launches a visible macOS Terminal.app window per job, and
records results back to the database. The **skills** are Claude Code markdown commands installed
into `~/.claude/commands/minicrew/` on each host. After the initial bootstrap walk-through in
`SETUP.md`, every subsequent user action (adding a worker, scaffolding a consumer project, tuning
a job type, checking status, tearing down) happens through a conversational skill. The user never
memorizes a bash command.

## Subagent Surface

Beyond the engine and the operator skills, minicrew exposes a third surface
aimed at OTHER Claude Code sessions that want to delegate work to the fleet.
This is the "subagent surface" and it has three caller-side paths, all of
which bottom out on the same load-bearing primitive: `python -m worker
--dispatch ad_hoc --wait`.

- **Path A — slash skill.** `skills/dispatch.md` and `skills/fanout.md` install
  to `~/.claude/commands/minicrew/` alongside the operator skills. An operator
  types `/minicrew:dispatch <task>` (or `/minicrew:fanout N <task>`); Claude
  Code reads the skill body, constructs the dispatch CLI invocation, and runs
  it via the Bash tool with `--wait` so the call blocks until the worker
  reports a terminal status.
- **Path B — `Task()` custom agent.** `agents/minicrew-mac-mini.md` installs
  to `~/.claude/agents/`. Any Claude Code session can spawn
  `Task(subagent_type="Minicrew Mac Mini", prompt=...)`; the sub-Claude reads
  the agent body, runs the dispatch CLI under tool restrictions (Bash + Read
  only), and returns the worker's `result.json` to the calling session inside
  base64-wrapped delimited markers. The base64 wrapper is a delimiter-injection
  defence: the base64 alphabet cannot collide with the marker string.
- **Path C — prose discovery via routing-rules.** `skills/routing-rules.md`
  installs to `~/.claude/commands/minicrew/` as an importable CLAUDE.md
  fragment. Consumer projects add `@~/.claude/commands/minicrew/routing-rules.md`
  to their `CLAUDE.md`; Claude Code then matches operator prose against the
  fragment's "Dispatch when X / Stay local when Y" heuristics and may invoke
  Path A without an explicit slash.

The three paths are additive, not exclusive. Path A is the lowest common
denominator (works on any Claude Code version that supports markdown
commands). Path B requires user-scoped `~/.claude/agents/` discovery
(Claude Code 2.x+); when unsupported the agent file is harmlessly ignored.
Path C layers on top of Path A — it changes the trigger but not the
implementation.

A recursion guard ties the surface together: every worker session's runner
script (emitted by `worker/terminal/launcher.py` and `launcher_resume.py`)
exports `MINICREW_INSIDE_WORKER=1`, and the `Minicrew Mac Mini` agent body
refuses to dispatch when it sees that env var. This prevents workers from
spawning workers indefinitely if the agent ever ends up installed inside a
worker's `~/.claude/agents/` (e.g. on a developer machine that is also a
worker host). Full details and the caller-side base64 extraction helper live
in `docs/SUBAGENT-INTEGRATION.md`.

See [COMMANDS.md](./COMMANDS.md#dispatch-insert-a-job-from-the-cli) for the
flat command reference.

## Runtime topology

A deployment is one or more Mac Minis. Each machine runs one to five worker instances supervised
by launchd. Each instance is an independent `python -m worker` process with a stable identifier
`<prefix>-<hostname>-<instance>` (for example `worker-minicrew-mini-1`). Instances do not
communicate with one another; the Postgres database is the sole coordination layer. Primary
machines poll every five seconds; secondary machines poll every fifteen. There is no leader, no
broadcast channel, and no discovery protocol. A machine goes online by registering a heartbeat
row and goes offline by missing heartbeats long enough for the reaper to mark it.

## Platform abstraction layer

Everything OS-specific in minicrew hides behind a `Platform` protocol defined in
`worker/platform/base.py`. The protocol declares `launch_session(cwd) -> SessionHandle`,
`close_session(handle)`, `preflight()`, and the service-management surface
(`install_service`, `uninstall_service`, `installed_instances`). Two implementations ship in
v1: `MacPlatform` (osascript + launchd + Terminal.app) and `LinuxPlatform` (xfce4-terminal or
tmux + wmctrl/xdotool + systemd user units). `worker/platform/__init__.py:detect_platform`
picks the right one based on `sys.platform` (`darwin` -> mac, `linux` -> linux, anything else
-> hard fail at startup); the user can override with an explicit `platform.kind` in
`config.yaml`.

Preflight is a strict contract. Before the poll loop ever starts, `platform.preflight()`
must succeed; any required tool missing, wrong session type (Wayland on Linux), or broken
desktop environment raises `PreflightError` with a remediation message and the worker exits.
There is no silent fallback — the caller is told exactly what is wrong.

`SessionHandle` is an opaque, serializable record of a live terminal session. It carries a
`kind` discriminator (`mac`, `linux_xfce4`, `linux_xterm`, `linux_tmux`) and a `data` dict
with platform-specific identifiers (Terminal.app window id on Mac; PID, PGID, window id, and
title on Linux; tmux session name in headless mode). The orchestrator, watchdog, and close
paths never reach into `data` directly — they hand the handle back to `platform.close_session`
and let the platform interpret it.

In fan-out mode, each group's session handle is persisted to `<group>/_session.json` so the
shutdown sweep can close every live window on SIGTERM or restart. During the one-release
upgrade window a legacy `_window_id.txt` is still read (wrapped into a synthetic
`SessionHandle(kind='mac', data={'window_id': int(...)})`) so an in-flight Mac job does not
leak its window during a mid-upgrade restart. On Linux, `LinuxPlatform.launch_session` writes
a `_pending_pid.txt` file holding the PID and PGID **before** entering the wmctrl poll loop;
orchestration shutdown paths always sweep `_pending_pid.txt` in addition to
`_session.json` files, so a terminal that opens milliseconds after the worker decides to
abort is still killed by process group.

The service-management CLI lives at `python -m worker.platform` (subcommands: `install`,
`uninstall`, `uninstall-all`, `list`). `bash setup.sh` and `bash teardown.sh` delegate to it
after detecting the OS; there is no OS-specific shell logic in the install path beyond
dispatching.

## The job lifecycle

A job begins life when a consumer inserts a row into the `jobs` table with `status='pending'`.
The next worker to claim it transitions the row to `status='running'` with a stamped
`worker_id`, `claimed_at`, and `worker_version`. When the orchestrator launches the terminal
session, `started_at` is written. On completion, the worker writes `result` (JSONB) and sets
`status='completed'` with `completed_at`. On failure, `status='error'` and `error_message` are
set. If the worker dies or the idle watchdog kills the session, the reaper (or startup recovery
on the next boot) requeues the row back to `pending` and increments `attempt_count`; once the
retry budget is exhausted (`max_attempts` is the total number of runs allowed, so with
`max_attempts=3` the third attempt's failure poisons the row) it becomes `failed_permanent`
with a descriptive `error_message`.

## Atomic claim

The claim is a two-step dance. First, the worker reads one row with `status='pending'` ordered
by priority and created_at. Then it issues `UPDATE jobs SET status='running', worker_id=..., ...
WHERE id=? AND status='pending'`. PostgREST returns the updated rows; the row count is the
winner signal. If two workers see the same pending row simultaneously, only one update matches
the `status='pending'` predicate; the loser gets an empty array back and loops. The database's
MVCC row lock during the UPDATE provides the mutual exclusion — no explicit locking is needed
on the client side.

## Priority model

There are two priority tiers, controlled entirely by polling interval: primary (5 seconds) and
secondary (15 seconds). Over time, primary workers claim roughly three times as many jobs as
secondaries. This is not deterministic — a secondary occasionally beats a primary on any given
job — but it is predictable in aggregate and requires zero coordination. For v1 this is
sufficient; deterministic priority windows are intentionally deferred.

## Heartbeats

Every worker writes a row to the `workers` table every thirty seconds. The write is an upsert
keyed on `id` (`on_conflict=id`), updating `last_heartbeat`, `status` (one of `idle`, `busy`,
`offline`), and `version`. The reaper treats any row whose `last_heartbeat` is older than the
configured stale threshold (default 120 seconds) as a crashed worker and requeues its jobs.

## Reaper (opportunistic)

Every worker runs a reaper thread. The thread opens a direct Postgres connection (not PostgREST),
begins a transaction, calls `pg_try_advisory_xact_lock(REAPER_LOCK_KEY)`, and inspects the
return value. If the lock was not acquired, the thread releases by committing and sleeps.
If the lock was acquired, the thread queries for stale workers, marks them offline, and calls
the `requeue_stale_jobs_for_worker` RPC for each. On commit, the advisory lock releases
automatically. Exactly one worker in the fleet runs the reaper in any given cycle; no writes
happen without the lock. No heartbeat, no election, no coordination protocol.

## Single-worker liveness caveat

A fleet with one worker cannot self-heal: if that single worker becomes the stuck one, no other
worker exists to reap it. Launchd `KeepAlive` restarts the process if it exits; the idle
watchdog kills hung terminal sessions before they consume the full job timeout. These two
mechanisms cover the single-worker case. Any fleet with two or more instances — on the same
machine or across machines — self-heals through the reaper.

## Graceful shutdown

On SIGTERM or SIGINT the worker sets a shared `shutdown_requested` flag. If a job is in flight,
the main loop releases the claim by updating the row back to `status='pending'`, clearing
`worker_id` and `started_at`. The worker marks its own `workers` row `status='offline'` and
exits. No in-flight work is lost; the next worker to poll picks it up. SIGHUP is not handled in
v1 — configuration reload requires a full restart.

## Idle watchdog

Each terminal session has a dedicated watchdog thread. Every few seconds it performs a recursive
`os.walk` over the session's cwd, noting the most recent file modification time across all
non-dot, non-underscore files. Two kill conditions fire. First: if elapsed run time exceeds 25
minutes and no file has been modified in 25 minutes and the expected result file is missing,
the session is killed and the job returns a timeout error. Second: if the result file exists
but has not been modified in 15 minutes, the session is killed (this catches hung
post-processing). The 25-minute guard on the first condition prevents merge sessions — which
start from an empty cwd — from being killed prematurely.

## Startup recovery

On startup the worker queries for any rows where `status='running'` and `worker_id` matches its
own identifier. These can only exist if the worker crashed without graceful shutdown. Each such
row is requeued to `pending` with `worker_id=null` and `started_at=null`, and a
`startup_requeued` event is emitted. The worker then enters its normal poll loop.

## The ad_hoc lifecycle

`mode: ad_hoc` is the dispatch surface for peer Claude Code sessions: a caller
pushes a snapshot branch, registers an MCP bundle in Vault, INSERTs a `jobs`
row with `submitted_by = auth.uid()`, and waits. The worker:

1. **Claim.** `claim_next_job_with_cap` RPC enforces a per-caller cap on
   simultaneously-running jobs.
2. **Mint a GitHub App install token.** Cached for clone + push within this job.
3. **Clone.** `git -c http.extraHeader='Authorization: Bearer <token>' clone …`
   into `<tmpdir>/repo`. Cancellable via `cancel_check`.
4. **Origin handling.** If `allow_code_push: false`, `git remote remove origin`.
   If `true`, pre-create `minicrew/result/<job_id>` so the inner session is on
   the result branch from launch.
5. **MCP write.** If `mcp_bundle_id` is set, fetch the Vault row via
   `vault.decrypted_secrets`, write `<clone>/.claude/settings.json` (mode 0600)
   containing `{"mcpServers": {...}}`.
6. **Render.** `render_builtin_ad_hoc` generates the prompt from
   `worker/builtin_prompts/ad_hoc.md.j2`. Consumers cannot override this template.
7. **Launch.** Standard `launch_session(cwd)` flow; the runner script tees the
   session log to `logs/jobs/<job_id>.log` (forced for ad_hoc).
8. **Side threads.** `ChunkedLogStreamer` uploads log chunks to Storage with a
   manifest at `<bucket>/<job_id>/manifest.json`; first upload PATCHes
   `caller_log_url` onto the row. `ProgressTailer` watches `_progress.jsonl`.
9. **Watch.** Watchdog returns one of `RESULT_COMPLETED`, `RESULT_SHUTDOWN`,
   `RESULT_CANCELLED`, or a timeout/error string.
10. **Read + optional push.** `read_result_safe` (with `result_schema`
    validation) produces a `ResultRead`. If `allow_code_push: true` and the
    session left commits on the result branch, `push_branch` runs under the
    cached App token; the resulting SHA is folded into `result.value.git`.
11. **Cleanup.** Order is load-bearing:
    - Stop side threads (BEFORE writing terminal status).
    - Close terminal handle if watchdog didn't.
    - `cleanup_session_data(<clone>)` wipes
      `~/.claude/projects/<encoded-clone-path>/`.
    - `shutil.rmtree(tmpdir)`.
    - On terminal outcome only: `dispatch_delete_mcp_bundle` and (if
      configured) Storage prefix delete.

The `bundle_safe_to_delete` flag gates step 11's bundle delete: it is set True
only on terminal outcomes (completed / error / cancelled). On
`RESULT_SHUTDOWN` the row is requeued and the bundle is preserved for the next
attempt.

## The handoff lifecycle

`mode: handoff` resumes an existing local Claude Code session on the worker via
`claude --resume <session-id> --print`. The end-to-end flow:

```
caller-side                        worker-side
-----------                        -----------
1. snapshot local edits
2. push minicrew/dispatch/<id>
3. dispatch_register_mcp_bundle
4. dispatch_register_transcript_bundle
   (Vault inline up to vault_inline_cap_bytes;
    Storage fallback gzipped above that)
5. INSERT jobs row (mode=handoff)
                                   6. claim via RPC
                                   7. mint App token, clone dispatch branch
                                   8. remove origin OR precreate result branch
                                   9. write per-job .claude/settings.json (MCP)
                                  10. fetch_transcript_bundle
                                      (resolves storage_ref if present)
                                  11. write to ~/.claude/projects/<encoded>/
                                       - <session-id>.jsonl
                                       - <session-id>/subagents/*.jsonl
                                  12. render_builtin_handoff
                                      (housekeeping outside if/else;
                                       user_instruction OR default preamble inside)
                                  13. write_runner_script_resume:
                                       claude --resume <id> --print
                                         --dangerously-skip-permissions
                                         --model X --effort Y "$(cat _prompt.txt)"
                                       2>&1 | tee <log>
                                  14. launch Terminal; start
                                       ChunkedLogStreamer + ProgressTailer
                                  15. wait_for_completion (caps caller-supplied
                                       timeout overrides at max_timeout_seconds)
                                  16. _try_bundle_outbound:
                                       read extended <session-id>.jsonl + subagents,
                                       register_transcript_bundle,
                                       PATCH final_transcript_bundle_id
                                  17. optional push of result branch
                                  18. write jobs.result, mark completed
                                  19. cleanup: stop streamers, close terminal,
                                       cleanup_session_data, rmtree, delete MCP +
                                       inbound transcript (if configured); outbound
                                       transcript stays under retention
20. /handoff:reattach <job_id>
    - dispatch_fetch_outbound_transcript
    - back up local <session-id>.jsonl
    - write worker's continued JSONL + subagents
    - print: claude --resume <session-id>
```

Cancel checkpoints are inserted after every IO step (clone, branch ops, MCP
fetch, transcript fetch, transcript write, prompt render, runner write,
pre-launch). On cancel/timeout/error, `_try_bundle_outbound` is called BEFORE
`cleanup_session_data` wipes the project directory — so even partial work is
recoverable via `/handoff:reattach`.

`worker/terminal/launcher.py` is NOT modified for handoff. Single, fan_out, and
ad_hoc continue to emit byte-identical runner scripts via `write_runner_script`.
Handoff is the only consumer of `write_runner_script_resume` (in
`worker/terminal/launcher_resume.py`). This honors the CLAUDE.md load-bearing-file
rule.

## Worker timeline

Per worker boot, in order:

1. `cli.py` parses args.
2. Config loaded, validated against `schema/config.schema.json`.
3. `platform.preflight()` — host readiness (X11 vs Wayland, missing tools,
   Terminal.app on Mac, etc.). Hard-fails with `PreflightError` on any miss.
4. **`platform.dispatch_preflight(cfg)`** — only when `cfg.dispatch is not None`.
   Verifies the GitHub App can mint a token; verifies the Storage bucket exists
   and is NOT anon-readable; verifies the expected dispatch RPCs exist via
   `dispatch_check_rpcs(text[])` (returns the subset that are MISSING — empty
   array means all present).
5. `startup_recovery` — requeue any `running` rows owned by this worker id.
6. Heartbeat thread started (30s when idle, 10s when busy).
7. Reaper thread started (opportunistic, advisory-lock gated).
8. **Side threads (per-job, dispatch-gated):** `ChunkedLogStreamer` and
   `ProgressTailer` start at job-launch when `cfg.dispatch is not None`. Both
   stop BEFORE the orchestrator writes the terminal status (load-bearing — avoids
   post-state PATCH races).
9. Poll loop: claim → orchestrate → sleep.

## File layout

```
worker/
  __init__.py              # exports __version__
  __main__.py              # entry point: python -m worker
  cli.py                   # arg parsing (--instance, --role, --status, --validate)
  core/
    main_loop.py           # the poll-claim-orchestrate-sleep loop
    claim.py               # claim_next_job wrapper
    heartbeat.py           # 30-second upsert to workers table
    reaper.py              # reaper thread + run_one_reaper_cycle
    signals.py             # SIGTERM/SIGINT handlers
    state.py               # current_job_id, shutdown_requested flag
    startup_recovery.py    # requeue own jobs left as running on boot
  terminal/
    launcher.py            # osascript window open + pre-trust + _run.sh render
    watchdog.py            # recursive mtime walk kill conditions
    shutdown.py            # /exit + window close + cwd cleanup
  orchestration/
    __init__.py            # dispatch on mode
    single_terminal.py     # default mode: one job, one window, one result
    fan_out.py             # N parallel group windows + 1 merge window
  platform/
    __init__.py            # detect_platform factory + argparse (install/uninstall/uninstall-all)
    __main__.py            # python -m worker.platform entrypoint
    base.py                # Platform protocol + SessionHandle + exceptions
    mac.py                 # MacPlatform — osascript + launchd
    linux.py               # LinuxPlatform — xfce4-terminal/tmux + systemd-user
  config/
    loader.py              # YAML + env interpolation + JSON Schema validation
    models.py              # Pydantic-style Config, JobType, ReaperConfig
    render.py              # Jinja env with StrictUndefined + finalize callback
    payload_schema.py      # optional payload.schema.json validator
  db/
    client.py              # PostgREST wrapper with eq.-autoprefix
    queries.py             # claim, update, write_result, mark_offline
    advisory_lock.py       # reaper_lock context manager over psycopg
  observability/
    events.py              # event constants, JSON formatter, redaction filter
    sinks.py               # FileSink with daily rotation; v2 sinks reject
    setup.py               # wire stdlib logging into sinks
  utils/
    version.py             # read VERSION file
    paths.py               # absolute-path helpers and trust-dialog writer
    db_url.py              # validate direct URL; reject pooler hosts
```
