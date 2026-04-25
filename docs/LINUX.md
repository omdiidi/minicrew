# Linux Mint XFCE

## For LLMs

Authoritative deep-dive for deploying minicrew on Linux Mint with the XFCE desktop. The Mac
Mini deployment is the reference; this document covers the Linux-specific parts. Load-bearing
invariants: visible mode requires an X11 session (Wayland is rejected at preflight); the
systemd user unit must hardcode `DISPLAY`, `XAUTHORITY`, and `XDG_SESSION_TYPE=x11` because
those are not inherited; `loginctl enable-linger` must NOT be used — the visible terminal
launch needs a real GUI session; `PartOf=graphical-session.target` is intentionally absent so
screen-lock transitions do not requeue in-flight jobs. The X11 threat model (any same-uid
process can inject keystrokes via xdotool) motivates the dedicated-user recommendation on
shared boxes.

## Overview

minicrew on Linux mirrors the Mac Mini pattern. The same Python engine polls the same
Supabase `jobs` queue, claims rows atomically, and launches a visible terminal per job. What
changes is the platform-abstracted backend: instead of osascript + launchd + Terminal.app, the
Linux build uses `xfce4-terminal` + `wmctrl`/`xdotool` + systemd user units. Every cross-OS
seam is isolated behind the `Platform` protocol in `worker/platform/`; the orchestrator,
watchdog, reaper, heartbeat, and claim paths are identical on both OSes. A mixed-OS fleet
(some Mac Minis, some Linux Mints) coordinates through the same Supabase database with no
special configuration — each worker process uses its own platform backend.

## Prerequisites

Install the system packages up front. Everything visible-mode needs must be present before
you run `bash setup.sh` — preflight will refuse to continue otherwise.

```bash
sudo apt update
sudo apt install python3-venv wmctrl xdotool xfce4-terminal tmux
```

- `python3-venv` — required to create the worker's `.venv`.
- `wmctrl` — list and match the newly-spawned terminal window by title.
- `xdotool` — type the `/exit` shutdown sequence into the terminal when a job completes.
- `xfce4-terminal` — the default terminal emulator. `xterm` is accepted as a fallback.
- `tmux` — required for the headless `display_mode: tmux` variant, even if you are on
  visible mode today.

Additionally:

- Claude Code: `npm install -g @anthropic-ai/claude-code`, then run `claude` once
  interactively to authenticate. `which claude` must return a path.
- A Supabase project with `schema/template.sql` applied (same as the Mac deployment —
  nothing is OS-specific in the schema).

## Dedicated-user recommendation

**On X11, any process running under the same uid can inject keystrokes into any X window
belonging to that uid, using `xdotool` or the raw XTEST extension.** minicrew's Claude
sessions run with `--dangerously-skip-permissions`, which grants those sessions unrestricted
filesystem and Bash access. A sibling process running in your desktop session — even a
browser extension, a misbehaving editor plugin, or a background service — can script keystrokes
into the Claude terminal and escalate to arbitrary code execution.

On a dedicated appliance box where the only workload is minicrew (the Mac-Mini-equivalent
deployment pattern), this is an acceptable threat model. On a shared or multi-use box — a
developer laptop, a family machine, any box where other graphical applications run — create a
dedicated `minicrew` uid and run the worker only as that user.

```bash
# Create the dedicated user (no password; login via LightDM auto-login).
sudo adduser --disabled-password --gecos "" minicrew

# Give them a home directory and place the repo there.
sudo -u minicrew -H bash -c 'cd ~ && git clone <your-minicrew-fork> minicrew'

# Switch LightDM auto-login to this user (see next section).
```

Log out of your personal desktop session, log back in as `minicrew` (or configure LightDM
auto-login to that user), run `bash setup.sh` in `~/minicrew`, and verify `whoami` inside a
worker-spawned terminal reports `minicrew`. The worker process, the Claude session, and any
shell commands Claude invokes all run under this isolated uid.

On a dedicated appliance box you can skip this step; acknowledge the posture in your runbook.

## LightDM auto-login

Visible mode needs a real X session up before the worker tries to open a terminal window.
LightDM (Linux Mint's default display manager) supports auto-login directly.

Edit `/etc/lightdm/lightdm.conf` as root:

```ini
[Seat:*]
autologin-user=minicrew
autologin-user-timeout=0
```

Replace `minicrew` with the actual uid you want the session to run as. If the `[Seat:*]`
section already exists with other directives, add `autologin-user=` and
`autologin-user-timeout=0` without removing the existing content. Reboot to verify the
session comes up directly at the XFCE desktop without a password prompt.

## X11 vs Wayland

minicrew's visible mode requires X11. The terminal-window match loop uses `wmctrl` and
`xdotool`, which talk to the X server directly; neither works under Wayland, and Mint's
Cinnamon-on-Wayland and GNOME-on-Wayland sessions would reject the automation silently.

When LightDM presents the login screen, click the session-type selector (the small gear or
settings icon next to the user name) and pick **Xfce Session** or **Xfce Session (X11)**.
Avoid anything labelled Wayland. On auto-login installations the session type defaults to the
most recent successful login, so logging in manually once with the right selection pins it.

If the `$XDG_SESSION_TYPE` environment variable is `wayland` when the worker starts,
`platform.preflight()` raises and the worker refuses to boot, with a remediation message
pointing at this section. Defense-in-depth: the preflight also probes `wmctrl -m` to confirm
the window manager is reachable.

## `DISPLAY` and `XAUTHORITY` in systemd

systemd user units do NOT inherit `DISPLAY`, `XAUTHORITY`, or `DBUS_SESSION_BUS_ADDRESS` from
the XFCE session by default. The user manager starts before the graphical session in some
configurations, and even when it starts after, the environment handoff is not automatic. As a
result, the worker's unit file hardcodes:

```
Environment=DISPLAY=:0
Environment=XAUTHORITY=%h/.Xauthority
Environment=XDG_SESSION_TYPE=x11
```

`%h` is systemd's user-home substitution — it expands to the home directory of the user the
unit runs under. If the terminal never opens after the worker claims a job, check the unit's
journal with `journalctl --user -u minicrew-worker-1.service -n 100` and look for a preflight
error referencing `$DISPLAY`. Inside the failing unit, you can also `echo $DISPLAY` at the
top of the worker's startup log to confirm the environment variable actually arrived.

If your X server runs on a display other than `:0` (rare, but possible on multi-seat boxes),
edit the generated unit file to match. `setup.sh` generates a conservative default.

## `XDG_SESSION_TYPE=x11` hardcoded in the unit

Preflight rejects `XDG_SESSION_TYPE=wayland` as a hard fail. Because systemd user units do
not inherit this variable from the graphical session, a unit running under an X11 desktop
would see `$XDG_SESSION_TYPE` unset, which preflight interprets as "unknown — probably fine."
Hardcoding `Environment=XDG_SESSION_TYPE=x11` in the unit lets preflight affirm explicitly
that the unit was generated for an X11 host. If you ever move a generated unit file to a
Wayland box, preflight still rejects on the `wmctrl -m` probe, so the guard is belt-and-braces.

## `StartLimitBurst` + `StartLimitIntervalSec`

The worker's unit file sets:

```
StartLimitBurst=5
StartLimitIntervalSec=300
```

`Restart=on-failure` would otherwise loop the worker forever if preflight fails (missing
`wmctrl`, Wayland session, `$DISPLAY` unset). Each restart spends a few seconds before
preflight fails again, pegging CPU and flooding the journal. Bounding the loop to five
restarts per five minutes makes the failure state observable in `systemctl --user status` —
the unit enters `failed` after the burst and stays there until you `systemctl --user
reset-failed` or fix the environment.

## `PartOf=graphical-session.target` intentionally absent

The unit uses `After=graphical-session.target` but NOT `PartOf=`. If `PartOf=` were set,
systemd would stop the worker whenever the graphical session transitions — screen-lock,
fast-user-switch, screensaver activation on some Mint builds. Stopping the worker mid-job
requeues the in-flight row (graceful shutdown via SIGTERM; the claim releases back to
`pending`), which is correct behavior for an intentional stop but an unhelpful side-effect of
a screen-lock. `After=` gives us the ordering guarantee (the worker does not start before
the graphical session is up) without the lifecycle coupling.

## `loginctl enable-linger` — why we do NOT use it

systemd user manager lingering (via `loginctl enable-linger <user>`) starts the user manager
at boot rather than at first login, which would let systemd user units run headlessly without
a GUI session. This is the obvious choice for daemons that do not need X, but minicrew's
visible mode explicitly needs a live XFCE session to spawn terminal windows into. Enabling
linger and then auto-logging-in still works (the linger doesn't disable login), but it adds a
second way for the user manager to come up and breaks the preflight's assumption that
`DISPLAY`, `XAUTHORITY`, and `wmctrl -m` are all available by the time the worker starts.

Rule of thumb: on a visible-mode deployment, the user manager starts on GUI login (via
pam_systemd) and shuts down on logout. Do not `loginctl enable-linger`. For headless
`display_mode: tmux` deployments, lingering is acceptable (see next section).

## `display_mode: tmux` — headless mode

On a box with no X server (a headless Linux server with remote SSH access only), set
`platform.linux.display_mode: tmux` in your `config.yaml`. Each job spawns a detached tmux
session instead of a visible terminal window. The Claude session runs inside tmux, the
watchdog polls the session cwd for result files as usual, and the close path sends `kill-
session` instead of `/exit`+window-close.

Trade-offs:
- **No visible window for manual debug.** Tailing what Claude is doing requires `tmux
  attach -t minicrew-<uuid>` from an SSH shell — doable but less immediate than glancing at
  the desktop.
- Preflight only requires `tmux`, not `wmctrl`/`xdotool`/`xfce4-terminal` — so you can run
  tmux mode on a server with no desktop environment at all.
- Lingering (`loginctl enable-linger`) is acceptable and usually correct in this mode:
  headless servers do not have a GUI session to hang the user manager on.

Visible mode is the default because it matches the Mac Mini UX (glance at the screen, see
work happening). Tmux mode exists for deployments where there is simply no display.

## Log rotation

The systemd unit captures stdout and stderr with `StandardOutput=append:/path/to/log` and
`StandardError=append:/path/to/log`. systemd holds the file descriptor **open** for the
lifetime of the unit. Standard logrotate's default rotation (rename + create a new file)
leaves the worker writing into the renamed file, which drops off logrotate's retention
bookkeeping and eventually consumes disk.

Use `copytruncate` in your logrotate config. `copytruncate` copies the current file contents
to the rotated name and then truncates the original in place, preserving the open file
descriptor. Example `/etc/logrotate.d/minicrew`:

```
/home/minicrew/minicrew/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
```

Adjust the path prefix to match your actual repo location. Two notes:

- Linux Mint's default logrotate runs daily via `/etc/cron.daily/logrotate`. No additional
  timer setup is needed.
- minicrew ALSO writes its own rotating file sink internally (configured under
  `logging.sinks[].rotate: daily` in `config.yaml`). The systemd-captured logs are
  belt-and-braces for startup errors that happen before the Python logging subsystem is up.
  The primary source of truth remains `logs/worker-<instance>.log` managed by the application.

## `MINICREW_TMPDIR` tunable

Linux Mint mounts `/tmp` as `tmpfs` by default, capped at 50% of system RAM. Large fan-out
jobs that rasterize PDFs, unpack archives, or do any kind of staging can fill `/tmp` quickly —
especially on 8GB or 16GB boxes.

If you see `No space left on device` errors inside session cwds and `df -h /tmp` shows usage
pressure, redirect minicrew's per-job staging to a disk-backed directory by setting:

```
# in .env
MINICREW_TMPDIR=/home/minicrew/.cache/minicrew/tmp
```

The worker honors `MINICREW_TMPDIR` as an override for the per-job tempdir root. The
directory is created if it does not exist. Using a path under `~/.cache/` gives you the
expected XDG semantics — `tmpwatch`/`systemd-tmpfiles` cleans it on a cadence you can tune.

## Troubleshooting

Each item below reproduces the preflight failure verbatim on the left and the fix on the
right. All messages come from `python -m worker --preflight` or from the worker's startup
log.

### `Wayland session detected — minicrew visible mode requires X11.`

Symptom: worker refuses to start; `journalctl --user -u minicrew-worker-1.service` shows the
preflight error.

Fix: log out, at the LightDM login screen pick **Xfce Session** (not the Wayland variant),
log back in. Persist the choice by auto-logging-in once successfully.

### `$DISPLAY is not set.`

Symptom: the preflight error references missing `DISPLAY`, typically when running the worker
from a systemd unit or an SSH session.

Fix: the generated unit file must carry `Environment=DISPLAY=:0` and
`Environment=XAUTHORITY=%h/.Xauthority`. Confirm with
`systemctl --user cat minicrew-worker-1.service`. If you are running manually from an SSH
shell, the shell does not have a DISPLAY of its own — you need to either run inside a real
GUI session or `export DISPLAY=:0 XAUTHORITY=~/.Xauthority` first.

Alternative for hand-run SSH sessions:
`systemctl --user import-environment DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS` run from
inside an XFCE desktop terminal imports the variables into the user manager.

### `missing required tool: wmctrl` (or `xdotool`, or `xfce4-terminal`)

Symptom: preflight fails with an explicit missing-tool message.

Fix: `sudo apt install wmctrl xdotool xfce4-terminal tmux`. Re-run preflight.

### Window never opens (terminal exits before `wmctrl` finds it, or title gets clobbered)

Symptom: `session_launched` events never appear; the worker logs say the wmctrl poll loop
timed out.

Cause: xfce4-terminal on some Mint builds double-forks, losing the initial PID. Or Claude
Code emits an OSC 0 escape sequence that resets the window title before wmctrl has a chance
to match on it.

Fix: the worker's `_run.sh` on Linux prepends a one-second sleep before `exec`ing `claude`,
giving the poll loop a title-stable window to match against. If you are running the latest
worker and still see this, check `platform.linux.window_open_timeout_seconds` in
`config.yaml` — increasing it from the default 15 to 30 helps on slow hardware.

### `$XDG_RUNTIME_DIR is not set`

Symptom: preflight fails with this message, usually when running `bash setup.sh` from an SSH
session rather than from a desktop terminal.

Fix: run setup from a terminal inside the actual XFCE desktop (open `xfce4-terminal` from the
Mint menu, cd into the repo, run `bash setup.sh`). pam_systemd sets `XDG_RUNTIME_DIR` at
graphical login; SSH sessions don't have one by default. Alternatively,
`export XDG_RUNTIME_DIR=/run/user/$(id -u)` manually, but you still need the runtime
directory to actually exist, which requires pam_systemd to have set it up at some point.

### `wmctrl -m` returns empty

Symptom: preflight says the window manager is not reachable despite `$DISPLAY` being set.

Cause: the X session is broken (a crashed window manager), or the session is Wayland after
all and you missed the earlier guard.

Fix: log out and back in. If the problem persists, `startxfce4` from a TTY to force a fresh
session, or reboot.

### systemd unit won't start

Symptom: `systemctl --user status minicrew-worker-1.service` shows `failed`.

Fix: always look at the journal first:

```
journalctl --user -u minicrew-worker-1.service -n 200 --no-pager
```

Common causes, in order: preflight failure (see above), `.env` missing or unreadable,
`MINICREW_CONFIG_PATH` unset, Python virtualenv missing (`setup.sh` was not run). The burst
limit (`StartLimitBurst=5`) may have tripped — run
`systemctl --user reset-failed minicrew-worker-1.service` after fixing the root cause.

### DO NOT run `sudo systemctl --user`

**Warning.** Running `sudo systemctl --user` spawns a root user manager that reads your
`.env` file as root and leaks its contents into the root-visible journal, defeating the
`chmod 600 .env` protection. Always run `systemctl --user` as the owning uid (no `sudo`).

If you need to inspect a unit running under a different uid, `sudo machinectl shell
user@.host` gives you a shell under that uid without the root-reads-env problem. Or simply
`su - <user>` and run `systemctl --user status` there.
