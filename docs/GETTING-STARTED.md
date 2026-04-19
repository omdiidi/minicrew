# Getting Started

## For LLMs

Human-oriented walkthrough. The step-by-step executable version is `SETUP.md`; this document
explains the why, not the exact commands. Do not turn this into a numbered recipe; keep the
prose register. Invariants to preserve: `SUPABASE_DB_URL` must be the direct 5432 URL, the
consumer ships a `worker-config/` directory, and skills install into `~/.claude/commands/minicrew/`.
Everything else is narrative.

## What you're setting up

A minicrew deployment is a small fleet of Mac Minis that run Claude Code sessions on your
behalf. You write the work to a Supabase queue; the fleet picks it up, runs a terminal session
per job, and writes the answer back. Before you begin, you need a Mac Mini (or any macOS
machine with a display you can see), a Supabase project you own, and a working Claude Code
install that is already authenticated. Authentication is the one thing you cannot automate —
run `claude` interactively once to log in before starting setup.

## Preparing Supabase

Create the schema by applying `schema/template.sql` to your Supabase database. You can do this
through the SQL editor in the Supabase dashboard or via `psql`. The template creates a `jobs`
table, a `workers` table, a reserved `worker_events` table for a future Postgres log sink, the
`requeue_stale_jobs_for_worker` RPC used by the reaper, and a `worker_stats` view used by
`python -m worker --status`. RLS is left off in the template; enable it per your deployment's
threat model (see `docs/SUPABASE-SCHEMA.md` for guidance).

Once the schema is in place, collect three values from the Supabase dashboard: your project URL
(Project Settings, API), your service role key (same page; the long one, not the anon key), and
your direct database URL. The direct URL is the important one. Under Project Settings, Database,
Connection string, look for the entry labelled "direct connection" on port 5432 — not the
pooler URL on port 6543. minicrew's reaper uses Postgres advisory locks, and those do not
behave correctly through pgBouncer in transaction-pool mode. The worker will refuse to start if
you hand it a pooler URL, so save yourself the detour and copy the direct one.

## Populating the environment

On the Mac Mini, clone the repo, copy `.env.example` to `.env`, and fill in the three Supabase
values plus `MINICREW_CONFIG_PATH`, which points at your consumer project's `worker-config/`
directory. The worker refuses to start without this env var set; it is where you tell the
engine which job types exist and what prompts to render.

## Creating a consumer `worker-config/`

In the project you want the fleet to process work for, create a directory named
`worker-config/`. It needs at minimum a `config.yaml` declaring `schema_version: 1`, your DB
credentials references, a `worker` section, a `reaper` section, and one or more `job_types`.
Each job type points at a Jinja prompt template under `worker-config/prompts/`, and each
prompt template is what Claude Code actually runs inside its terminal session. The easiest way
to get this right is to let the `/minicrew:scaffold-project` skill generate starter files for
you and then edit them. You can also copy from `examples/minimal/` as a starting point.

## Running the setup skill

With `.env` populated and a `worker-config/` in place, invoke `/minicrew:setup` from a Claude
Code session on the Mac Mini. The skill idempotently re-creates the Python virtualenv,
installs requirements, validates your consumer config against the JSON Schema, installs the
skills into `~/.claude/commands/minicrew/`, generates a launchd plist per instance, and boots
the services. If anything fails — a missing env var, an invalid config, a pooler URL — the
skill surfaces the error and stops; nothing is left half-installed.

## Verifying the fleet

Two commands confirm a healthy install: `launchctl list | grep com.minicrew.worker` should
return one row per instance, and `python -m worker --status` should return JSON listing your
workers in status `idle` or `busy`, along with the current queue depth and recent failure
counts. If a worker appears as `offline`, check its log at `logs/worker-<instance>.log` for a
legible startup error — the most common cause is a typo in `.env`.

## For the copy-paste version see SETUP.md

`SETUP.md` is the Claude-executable script form of this walkthrough. It is numbered, it is
idempotent, and it assumes you are letting Claude Code drive the install. Prefer it if you're
doing this on a real machine; this document exists to explain what the script is doing and why.
