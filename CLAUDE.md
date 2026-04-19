# CLAUDE.md

Routes Claude Code sessions that land in this repo.

## You are in the minicrew repo.

minicrew is a generic Claude Code worker template for Mac Mini fleets: polling, atomic job claim, Terminal.app session launch, idle watchdog, heartbeat, opportunistic reaper. Consumers wire in their job types via a YAML config + Jinja prompt templates. Skills live in `~/.claude/commands/minicrew/` after setup.

## Routing

- If the user asks you to set up, install, add a worker, or configure this Mac Mini → read [SETUP.md](./SETUP.md) and follow it top-to-bottom.
- If the user asks you to integrate an external project with this worker, or hands you the repo URL from a consumer project → read [INTEGRATE.md](./INTEGRATE.md) and follow it. Never modify this repo from a consumer-side integration.
- If the user is working on the engine code itself (changes under `worker/`) → read [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) for runtime context before editing.

## Rules

- Never push to the GitHub remote without explicit user authorization. Show the diff, wait for approval.
- Never reference the original source domain of this architecture. This is a generic template; keep it generic. Neutral example job types only (`summarize`, `classify`, `analyze_document`).
- `--dangerously-skip-permissions` is intentional for headless automation. See [SECURITY.md](./SECURITY.md) before questioning or removing it.
- Do not edit `worker/terminal/launcher.py` or `worker/terminal/shutdown.py` without first reading [docs/TROUBLESHOOTING.md](./docs/TROUBLESHOOTING.md). The `osascript` and Terminal.app incantations in those files are load-bearing — the tab-to-window id lookup, the `/exit` shutdown sequence, and the `~/.claude/projects/` cleanup each guard against a specific reproducible failure mode.

## Version

Read the current version from the [VERSION](./VERSION) file at repo root.
