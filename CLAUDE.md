# CLAUDE.md

Routes Claude Code sessions that land in this repo.

## You are in the minicrew repo.

minicrew is a generic Claude Code worker template for Mac Mini and Linux Mint XFCE fleets: polling, atomic job claim, visible-terminal session launch, idle watchdog, heartbeat, opportunistic reaper. Consumers wire in their job types via a YAML config + Jinja prompt templates. Skills live in `~/.claude/commands/minicrew/` after setup.

### Platform layer

Everything OS-specific lives behind the `Platform` protocol in `worker/platform/`.
`MacPlatform` wraps osascript + launchd + Terminal.app; `LinuxPlatform` wraps xfce4-terminal
(or tmux) + wmctrl/xdotool + systemd user units. `detect_platform()` auto-picks based on
`sys.platform`; `python -m worker.platform {install,uninstall,uninstall-all}` is the
canonical service-management CLI and is what `setup.sh`/`teardown.sh` delegate to.
`python -m worker --preflight` is the single source of truth for "is this box actually ready"
— it dispatches to `platform.preflight()` which raises on Wayland, missing `$DISPLAY`,
missing `wmctrl`/`xdotool`/`xfce4-terminal`, or a broken window manager on Linux, and on
missing `osascript` or unwritable `~/Library/LaunchAgents/` on Mac. Do not add OS-specific
branches outside `worker/platform/`; keep the rest of the engine platform-agnostic.

## Routing

- If the user asks you to set up, install, add a worker, or configure this Mac Mini → read [SETUP.md](./SETUP.md) and follow it top-to-bottom.
- If the user asks you to integrate an external project with this worker, or hands you the repo URL from a consumer project → read [INTEGRATE.md](./INTEGRATE.md) and follow it. Never modify this repo from a consumer-side integration.
- If the user is working on the engine code itself (changes under `worker/`) → read [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) for runtime context before editing.
- If the user is dispatching a task from another Claude Code session → read [docs/DISPATCH.md](./docs/DISPATCH.md).
- If the user is handing off a running Claude Code session to a remote worker → read [docs/HANDOFF.md](./docs/HANDOFF.md). For the technical contract behind it (RPC shapes, payload schema), see [docs/DISPATCH.md](./docs/DISPATCH.md).

## Rules

- Never push to the GitHub remote without explicit user authorization. Show the diff, wait for approval.
- Never reference the original source domain of this architecture. This is a generic template; keep it generic. Neutral example job types only (`summarize`, `classify`, `analyze_document`).
- `--dangerously-skip-permissions` is intentional for unattended automation (Terminal windows are visible by design but no one is clicking through permission dialogs between tool calls). See [SECURITY.md](./SECURITY.md) before questioning or removing it.
- Do not edit `worker/terminal/launcher.py` or `worker/terminal/shutdown.py` without first reading [docs/TROUBLESHOOTING.md](./docs/TROUBLESHOOTING.md). The `osascript` and Terminal.app incantations in those files are load-bearing — the tab-to-window id lookup, the `/exit` shutdown sequence, and the `~/.claude/projects/` cleanup each guard against a specific reproducible failure mode.

## Version

Read the current version from the [VERSION](./VERSION) file at repo root.

@CLAUDE.local.md
