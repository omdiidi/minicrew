# SETUP.md

First-time worker bootstrap. This is the primary entry point for a fresh clone.

## For LLMs executing this file

You are setting up minicrew on a deployment machine. Detect the OS first (`uname -s`:
`Darwin` -> "On macOS" section, `Linux` -> "On Linux Mint XFCE" section, anything else ->
stop and tell the user this OS is not supported in v1). Each step below is idempotent —
running it twice is safe. On any failure, explain the failure to the user verbatim and stop;
do not guess a workaround. Never push to git. Never commit secrets. Collect all user inputs
in a single message rather than drip-feeding questions one at a time. Work through the steps
in order; do not reorder them.

All commands assume the current working directory is this repo's root (the directory
containing this file).

---

# Pick your OS

The two sections below are **self-contained** — pick the one matching your machine and follow
every step top-to-bottom. Do not cross-reference.

- **On macOS** — Mac Mini or any Mac with a display. Uses `launchd` + `Terminal.app`.
- **On Linux Mint XFCE** — Mint with the default XFCE desktop. Uses `systemd` user units +
  `xfce4-terminal`.

---

# On macOS

## Step 1 (macOS) — Prereq check

**Goal:** confirm the machine can run minicrew.

**Commands:**
```bash
which claude
claude --version
python3 --version
which osascript
gh --version   # optional; only needed for later GitHub operations
```

**Success criterion:**
- `which claude` prints a path (Claude Code is installed and in PATH).
- `claude --version` prints a version string.
- `python3 --version` prints `Python 3.11.x` or higher.
- `which osascript` prints `/usr/bin/osascript` (built-in on macOS).
- `gh` is optional — a missing `gh` is not a failure.

**Failure response:** If `claude` is missing, instruct the user to install Claude Code (`npm
install -g @anthropic-ai/claude-code`) and then run `claude` once interactively to
authenticate. If Python is older than 3.11, instruct them to install 3.11+ (Homebrew: `brew
install python@3.11`). Stop until the user confirms fixed.

---

## Step 2 (macOS) — Ask the user

**Goal:** collect every value we need in one message.

**Send ONE message** requesting all of these, with the stated walkthrough for the direct DB
URL:

1. **Supabase project URL** — e.g. `https://abcdefghij.supabase.co`.
2. **Supabase service role key** — from Project Settings -> API -> `service_role` key (NOT
   the anon key).
3. **Supabase direct database URL** — walkthrough: Supabase Dashboard -> Project Settings ->
   Database -> Connection string -> pick the **Direct connection** tab -> copy the
   `postgresql://...:5432/postgres` string. The pooler URL (port 6543) will not work;
   advisory locks require a direct connection.
4. **Role** — `primary` (5s poll interval) or `secondary` (15s poll interval).
5. **Instance count** — integer 1 to 5. Number of worker processes to run on this Mac Mini.
6. **Consumer `worker-config/` absolute path** — the absolute path to the directory in the
   consumer project that contains `config.yaml` and `prompts/`.

**Caveat to communicate to the user in the same message:** the service role key will appear
in the Claude Code conversation transcript when they reply. Recommend running setup inside a
local (non-shared) Claude Code session and rotating the key afterwards if the session was
exported, logged, or shared.

**Success criterion:** user replies with all six values. Do not echo the service role key or
the direct DB URL back to the user after receipt.

**Failure response:** if a value is missing, ask once for the missing value. If the user
cannot locate the direct DB URL in the dashboard, restate the walkthrough and stop.

---

## Step 3 (macOS) — Write `.env`

**Goal:** create a locked-down `.env` containing the credentials.

**Commands:**
```bash
cp -n .env.example .env
chmod 600 .env
```

Then, using an Edit-style write (do NOT echo the key to the shell), populate `.env` with the
values from Step 2:
```
SUPABASE_URL=<value from step 2.1>
SUPABASE_SERVICE_ROLE_KEY=<value from step 2.2>
SUPABASE_DB_URL=<value from step 2.3>
MINICREW_CONFIG_PATH=<value from step 2.6>
WORKER_ROLE=<value from step 2.4>
```

`WORKER_ROLE` is the default role (`primary` or `secondary`) applied to every worker instance
on this machine. Skills such as `/minicrew:add-worker` read it from this file when picking a
role for a new instance.

**Success criterion:** `ls -l .env` shows `-rw-------` (mode 600) and the file contains all
five variables non-empty.

**Failure response:** if `.env` already existed before this step and has values, ask the user
whether to overwrite. Do not silently overwrite existing credentials.

---

## Step 4 (macOS) — Create venv and install requirements

**Goal:** isolated Python environment with runtime deps installed.

**Commands:**
```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

**Success criterion:** `.venv/bin/python -c "import worker"` exits 0 (the worker package
imports cleanly).

**Failure response:** report the `pip install` stderr verbatim. Common cause is psycopg build
failure on older Xcode command-line tools — recommend `xcode-select --install` and retry.
Stop until imports succeed.

---

## Step 5 (macOS) — Run preflight

**Goal:** confirm the platform backend is ready before we install launchd services.

**Commands:**
```bash
.venv/bin/python -m worker --preflight
```

**Success criterion:** exit code 0, stdout `ok`.

**Failure response:** print the preflight error verbatim. On Mac the common causes are a
missing `osascript` (should never happen — it is built-in) or an unwritable
`~/Library/LaunchAgents/`. Stop until preflight is green.

---

## Step 6 (macOS) — Validate consumer `worker-config/`

**Goal:** fail fast if the consumer config is malformed.

**Commands:**
```bash
.venv/bin/python -m worker.config.loader --validate "$MINICREW_CONFIG_PATH"
```

**Success criterion:** exit code 0, stdout beginning with `ok: ` followed by the validated
config path.

**Failure response:** print the validator's error message verbatim. STOP. Ask the user to
fix their `worker-config/config.yaml`.

---

## Step 7 (macOS) — Install skills

**Goal:** copy the repo's skills into `~/.claude/commands/minicrew/`.

**Commands:**
```bash
mkdir -p "$HOME/.claude/commands/minicrew"
cp skills/*.md "$HOME/.claude/commands/minicrew/"
ls "$HOME/.claude/commands/minicrew/"
```

**Success criterion:** `ls` shows all the skill markdown files present.

**Tell the user:** after this step any future Claude Code session on this machine can invoke
`/minicrew:setup`, `/minicrew:add-worker`, `/minicrew:status`, `/minicrew:scaffold-project`,
`/minicrew:tune`, `/minicrew:add-job-type`, `/minicrew:add-machine`, and
`/minicrew:teardown`.

**Failure response:** if `cp` fails, check that `skills/` exists in the current repo; if not,
the user is not in the repo root. Stop.

---

## Step 8 (macOS) — Install launchd services

**Goal:** register N long-running worker services with launchd so they survive reboots and
auto-restart on crash.

**Commands** (loop `$i` from 1 to `$INSTANCE_COUNT` from Step 2.5, with `$ROLE` from Step
2.4):
```bash
for i in $(seq 1 $INSTANCE_COUNT); do
  .venv/bin/python -m worker.platform install \
    --instance $i \
    --role $ROLE \
    --config-path "$MINICREW_CONFIG_PATH"
done
```

**Success criterion:** each invocation exits 0. No `launchctl bootstrap failed` errors.

**Failure response:** if a specific instance fails, print the stderr verbatim. Common cause
is a stale plist from a previous install — the installer retries `bootout` + `bootstrap`
internally up to 3 times; if it still fails, stop and ask the user to run
`launchctl list | grep com.minicrew.worker` and report what they see.

---

## Step 9 (macOS) — Verify

**Goal:** confirm the fleet is alive and polling.

**Commands:**
```bash
sleep 15
launchctl list | grep com.minicrew.worker
tail -n 50 logs/worker-1.log
.venv/bin/python -m worker --status
```

**Success criterion:**
- `launchctl list | grep com.minicrew.worker` shows N rows, one per instance, each with a
  non-`-` PID.
- `tail logs/worker-1.log` contains a `worker_started` event in JSON.
- `python -m worker --status` prints a JSON object with keys `workers`, `queue_depth`,
  `recent_errors_1h`, and `recent_failed_permanent_24h` and exits 0.

**Failure response:** if a worker has PID `-` and a non-zero exit status, open
`logs/worker-<n>.err` and report the stderr to the user.

---

## Step 10 (macOS) — Write `~/.claude/minicrew.json`

**Goal:** record the absolute repo path so installed skills can find the engine.

**Commands:**
```bash
REPO_PATH="$(pwd -P)"
mkdir -p "$HOME/.claude"
cat > "$HOME/.claude/minicrew.json" <<EOF
{"repo_path": "$REPO_PATH"}
EOF
```

**Success criterion:** `cat ~/.claude/minicrew.json` prints valid JSON with a `repo_path` key
pointing to the absolute path of this repo.

---

## Done (macOS)

Report to the user:
- Number of worker instances installed.
- Role (primary or secondary).
- Path to logs (`logs/worker-<n>.log`).
- Instruction: "try `/minicrew:status` in any future Claude Code session to check fleet
  health."

Remind them the service role key is in `.env` with mode 600 and was also pasted into this
conversation transcript; if this session was shared or exported, rotate the key per
[SECURITY.md](./SECURITY.md#key-rotation).

---

# On Linux Mint XFCE

Before starting, read `docs/LINUX.md` for the deep-dive on LightDM auto-login, the X11
threat model, systemd unit environment, logrotate, and `MINICREW_TMPDIR`. The steps below
are the executable install; `docs/LINUX.md` is the runbook.

## Step 1 (Linux) — Prereq check

**Goal:** confirm the machine can run minicrew on Linux Mint XFCE.

**Commands:**
```bash
which claude
claude --version
python3 --version
which wmctrl xdotool xfce4-terminal tmux
echo "XDG_SESSION_TYPE=$XDG_SESSION_TYPE"
echo "DISPLAY=$DISPLAY"
```

**Success criterion:**
- `which claude` prints a path.
- `claude --version` prints a version string.
- `python3 --version` prints `Python 3.11.x` or higher.
- All four of `wmctrl`, `xdotool`, `xfce4-terminal`, `tmux` print a path.
- `XDG_SESSION_TYPE` is `x11` (or empty; not `wayland`).
- `DISPLAY` is set (typically `:0`).

**Failure response:** if `claude` is missing, `npm install -g @anthropic-ai/claude-code`.
If any of `wmctrl`/`xdotool`/`xfce4-terminal`/`tmux` is missing:

```bash
sudo apt install python3-venv wmctrl xdotool xfce4-terminal tmux
```

If `XDG_SESSION_TYPE=wayland`, tell the user to log out, pick **Xfce Session** at the LightDM
login screen (not a Wayland variant), and log back in. If `DISPLAY` is empty, the user is
running this from SSH instead of a desktop terminal — tell them to run from a real
xfce4-terminal window inside the XFCE desktop. Stop until all checks are green.

---

## Step 2 (Linux) — LightDM auto-login (recommended)

**Goal:** boot directly into an XFCE session so the worker has a GUI to open terminals into.

Tell the user this step is recommended but optional — if they always log in manually, they
can skip it. If they want the worker to survive reboots unattended, walk through it:

Edit `/etc/lightdm/lightdm.conf` as root. Under the `[Seat:*]` section, add:

```ini
autologin-user=<username>
autologin-user-timeout=0
```

Replace `<username>` with the uid that will run the worker (see `docs/LINUX.md` for the
dedicated-user recommendation if this is a shared box).

**Success criterion:** after reboot, the machine comes up directly at the XFCE desktop
without a password prompt.

**Failure response:** if the desktop does not auto-login, check `/var/log/lightdm/lightdm.log`
for configuration errors. The file is owned by root; `sudo tail -n 50
/var/log/lightdm/lightdm.log`.

---

## Step 3 (Linux) — Ask the user

**Goal:** collect every value we need in one message.

**Send ONE message** requesting:

1. **Supabase project URL** — e.g. `https://abcdefghij.supabase.co`.
2. **Supabase service role key** — from Project Settings -> API -> `service_role` key.
3. **Supabase direct database URL** — Supabase Dashboard -> Project Settings -> Database ->
   Connection string -> **Direct connection** tab -> the `postgresql://...:5432/postgres`
   string. Pooler URL (port 6543) will not work.
4. **Role** — `primary` (5s) or `secondary` (15s).
5. **Instance count** — integer 1 to 5.
6. **Consumer `worker-config/` absolute path**.

**Caveat:** service role key will appear in the transcript. Recommend a local session and key
rotation if shared.

**Success criterion:** all six values received. Do not echo the service role key or DB URL
back.

---

## Step 4 (Linux) — Write `.env`

**Goal:** create a locked-down `.env` containing the credentials.

**Commands:**
```bash
cp -n .env.example .env
chmod 600 .env
```

Populate `.env` with the values from Step 3:
```
SUPABASE_URL=<value from step 3.1>
SUPABASE_SERVICE_ROLE_KEY=<value from step 3.2>
SUPABASE_DB_URL=<value from step 3.3>
MINICREW_CONFIG_PATH=<value from step 3.6>
WORKER_ROLE=<value from step 3.4>
```

Optional: if `/tmp` is small (Mint's tmpfs defaults to 50% of RAM) and you run large fan-out
jobs, also add:
```
MINICREW_TMPDIR=/home/<username>/.cache/minicrew/tmp
```

**Success criterion:** `ls -l .env` shows `-rw-------` (mode 600) and all five required
variables are non-empty.

**Failure response:** if `.env` already exists with values, ask whether to overwrite.

---

## Step 5 (Linux) — Create venv and install requirements

**Goal:** isolated Python environment.

**Commands:**
```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

**Success criterion:** `.venv/bin/python -c "import worker"` exits 0.

**Failure response:** report stderr verbatim. If `python3 -m venv` fails with
`ensurepip is not available`, the user is missing `python3-venv`:
`sudo apt install python3-venv`.

---

## Step 6 (Linux) — Run preflight

**Goal:** confirm the Linux platform backend is ready before we install systemd units. This
is the single source of truth for "is the environment sane" — it checks Wayland, `$DISPLAY`,
every required tool, and `wmctrl -m` reachability in one shot.

**Commands:**
```bash
.venv/bin/python -m worker --preflight
```

**Success criterion:** exit code 0, stdout `ok`.

**Failure response:** print the preflight error verbatim. Every message includes a
remediation pointer. Common cases:

- `Wayland session detected …` — log out, pick Xfce Session at LightDM, log back in.
- `$DISPLAY is not set` — run from a real XFCE desktop terminal (not SSH), or `export
  DISPLAY=:0 XAUTHORITY=~/.Xauthority` first.
- `missing required tool: wmctrl` (etc.) — `sudo apt install wmctrl xdotool xfce4-terminal
  tmux`.
- `$XDG_RUNTIME_DIR is not set` — run from a desktop terminal, not SSH.
- `wmctrl -m returns empty` — the X session is broken; log out and back in, or reboot.

Full list in `docs/TROUBLESHOOTING.md` (Linux section) and `docs/LINUX.md`. Stop until
preflight is green.

---

## Step 7 (Linux) — Validate consumer `worker-config/`

**Goal:** fail fast if the consumer config is malformed.

**Commands:**
```bash
.venv/bin/python -m worker.config.loader --validate "$MINICREW_CONFIG_PATH"
```

**Success criterion:** exit code 0, stdout beginning with `ok: `.

**Failure response:** print the validator's error verbatim. STOP. Ask the user to fix
`worker-config/config.yaml`.

---

## Step 8 (Linux) — Install skills

**Goal:** copy the repo's skills into `~/.claude/commands/minicrew/`.

**Commands:**
```bash
mkdir -p "$HOME/.claude/commands/minicrew"
cp skills/*.md "$HOME/.claude/commands/minicrew/"
ls "$HOME/.claude/commands/minicrew/"
```

**Success criterion:** `ls` shows all skill markdown files present.

**Tell the user:** after this step any future Claude Code session on this machine can invoke
`/minicrew:setup`, `/minicrew:add-worker`, `/minicrew:status`, `/minicrew:scaffold-project`,
`/minicrew:tune`, `/minicrew:add-job-type`, `/minicrew:add-machine`, and
`/minicrew:teardown`.

---

## Step 9 (Linux) — Install systemd user services

**Goal:** register N long-running worker services with systemd user manager so they survive
logout-relogin cycles and auto-restart on crash (bounded by `StartLimitBurst`).

**Commands** (loop `$i` from 1 to `$INSTANCE_COUNT` from Step 3.5, with `$ROLE` from Step
3.4):
```bash
for i in $(seq 1 $INSTANCE_COUNT); do
  .venv/bin/python -m worker.platform install \
    --instance $i \
    --role $ROLE \
    --config-path "$MINICREW_CONFIG_PATH"
done
```

**Important:** run this as the uid that will own the worker. Do NOT prefix with `sudo` — see
the "DO NOT run `sudo systemctl --user`" warning in `docs/LINUX.md` and `SECURITY.md`.

**Success criterion:** each invocation exits 0 and `systemctl --user status
minicrew-worker-$i.service` reports `active (running)` for each instance.

**Failure response:** if a unit fails to start,
`journalctl --user -u minicrew-worker-$i.service -n 200 --no-pager` surfaces the startup
error. The most common causes are preflight not actually being green (re-run Step 6),
`.env` unreadable, or `MINICREW_CONFIG_PATH` unset in the unit's environment. If
`StartLimitBurst=5` has tripped, `systemctl --user reset-failed
minicrew-worker-$i.service` before retrying.

---

## Step 10 (Linux) — Verify

**Goal:** confirm the fleet is alive and polling.

**Commands:**
```bash
sleep 15
systemctl --user list-units 'minicrew-worker-*.service'
tail -n 50 logs/worker-1.log
.venv/bin/python -m worker --status
```

**Success criterion:**
- `systemctl --user list-units` shows N active units, one per instance.
- `tail logs/worker-1.log` contains a `worker_started` event in JSON.
- `python -m worker --status` prints a JSON object with `workers`, `queue_depth`,
  `recent_errors_1h`, `recent_failed_permanent_24h`.

**Failure response:** if a unit is `failed`,
`journalctl --user -u minicrew-worker-<n>.service -n 200 --no-pager` and report the output.
Cross-reference with `docs/TROUBLESHOOTING.md` (Linux section).

---

## Step 11 (Linux) — Write `~/.claude/minicrew.json`

**Goal:** record the absolute repo path for skill discovery.

**Commands:**
```bash
REPO_PATH="$(pwd -P)"
mkdir -p "$HOME/.claude"
cat > "$HOME/.claude/minicrew.json" <<EOF
{"repo_path": "$REPO_PATH"}
EOF
```

**Success criterion:** `cat ~/.claude/minicrew.json` prints valid JSON with `repo_path`
pointing to the absolute path of this repo.

---

## Step 12 (Linux) — logrotate (recommended)

**Goal:** prevent `logs/worker-*.log` from growing unbounded.

Tell the user this step is recommended but optional. Create
`/etc/logrotate.d/minicrew` with root permissions:

```
/home/<username>/minicrew/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
```

The `copytruncate` directive is required because the systemd unit captures stdout with
`StandardOutput=append:` and holds the file descriptor open across rotation. See
`docs/LOGGING.md` and `docs/LINUX.md` for background.

**Success criterion:** `sudo logrotate --debug /etc/logrotate.d/minicrew` reports the config
as valid and shows the planned rotation.

---

## Done (Linux)

Report to the user:
- Number of worker instances installed.
- Role (primary or secondary).
- Path to logs (`logs/worker-<n>.log`).
- Instruction: "try `/minicrew:status` in any future Claude Code session to check fleet
  health."
- If the machine is shared or multi-use, re-read the "Dedicated-user recommendation" in
  `docs/LINUX.md` — the X11 threat model is real on shared boxes.

Remind them the service role key is in `.env` with mode 600 and was also pasted into this
conversation transcript; if this session was shared or exported, rotate the key per
[SECURITY.md](./SECURITY.md#key-rotation).
