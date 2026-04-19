# minicrew

Generic Claude Code worker template for Mac Mini fleets — zero-terminal setup, queue-driven, production-ready.

## What it is

minicrew runs headless Claude Code sessions on a fleet of Mac Minis. Each worker polls a Supabase-backed job queue, claims jobs atomically, and launches a visible macOS Terminal.app window hosting a `claude` session for each job. When the job finishes, the worker records the result back to Supabase and loops.

The engine is domain-agnostic. Any project that can insert a row into a Postgres table can enqueue work: document summarization, classification, analysis, multi-step reasoning, anything Claude Code can do. Consumers configure job types via a YAML file plus Jinja prompt templates — no Python required.

Multi-machine, multi-instance, and self-healing by design. A built-in opportunistic reaper (guarded by a Postgres advisory lock so exactly one worker reaps per cycle) requeues jobs from dead workers. A per-session idle watchdog kills stuck Claude sessions. All user-facing operations (install, add a worker, check status, tear down) are conversational Claude Code skills — you never memorize a command.

## One-sentence bootstrap

Clone `github.com/omdiidi/minicrew`, open Claude Code in the directory, and say "read SETUP.md and set me up."

## Why

- Skills ride on a globally-installed Claude Code skills directory (`~/.claude/commands/minicrew/`) so every subsequent action is a slash-command, not a shell invocation.
- Configuration is declarative YAML + Jinja, validated against JSON Schema at load — legible to both humans and agents, diff-friendly, no hidden state.
- No memorized terminal commands. Setup, scaling, tuning, and teardown are conversations.

## Architecture at a glance

- Poller — polls Supabase every 5s (primary) or 15s (secondary) for a pending job.
- Atomic claim — `UPDATE ... WHERE status='pending'` row-count check; exactly one worker wins.
- Terminal launcher — `osascript` opens Terminal.app, runs `claude --dangerously-skip-permissions` with the rendered prompt.
- Idle watchdog — recursive file-mtime walk over the session cwd; kills sessions stuck for 25 minutes without file activity or 15 minutes with a stale result file.
- Opportunistic reaper — runs on its own thread in every worker; a transaction-level Postgres advisory lock (`pg_try_advisory_xact_lock`) guarantees exactly one reaper cycle runs at a time across the fleet.
- Heartbeat — every 30s, the worker upserts its row in the `workers` table.

Full runtime topology, failure modes, and port-5432-vs-pooler gotchas in [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md).

## Use as a consumer

Point your AI agent at `https://github.com/omdiidi/minicrew` and tell it to read [INTEGRATE.md](./INTEGRATE.md). That one file is self-contained: it describes the jobs-table contract, what your project provides (`worker-config/` directory with a `config.yaml` plus Jinja prompt templates), what the worker writes back, and copy-paste-ready enqueue patterns for curl, FastAPI, Next.js server actions, and Supabase Edge Functions. The agent proposes the diff; you approve. No human terminal work on the consumer side.

Alternatively, run `/minicrew:scaffold-project` in your project once the skills are installed, and the skill walks you through the same integration interactively.

## Status

v1. Mac-only (osascript + launchd + Terminal.app). Supabase-only (thin PostgREST wrapper; direct 5432 Postgres connection for advisory locks). Linux support and a pluggable multi-backend database layer are on the roadmap.

## License

MIT. See [LICENSE](./LICENSE).
