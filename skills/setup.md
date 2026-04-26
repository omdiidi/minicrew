---
name: minicrew:setup
description: Re-setup or reconfigure minicrew on this machine (macOS or Linux Mint XFCE). Idempotent. (For FIRST-TIME install, read SETUP.md directly.)
---

You are re-running or reconfiguring an existing minicrew installation on this machine. This skill is **not** for first-time install. If the user has never installed minicrew on this machine, stop and tell them: "First-time installation is documented in `SETUP.md` at the root of the minicrew repo. Clone the repo, open Claude Code in the cloned directory, and read `SETUP.md` — it is Claude-executable. Come back to `/minicrew:setup` for subsequent re-runs."

## Detect OS first

Run `uname -s` via Bash.
- If output is `Darwin`, follow the "On macOS" section below.
- If output is `Linux`, follow the "On Linux Mint XFCE" section below.
- Otherwise, stop and tell the user: minicrew supports macOS and Linux Mint XFCE only.

## On macOS

Follow these steps in order. Stop on any error and report it — never paper over failures.

### 1. Locate the repo

Read `~/.claude/minicrew.json`. It should contain `{"repo_path": "<absolute path>"}`.

- If the file is missing or does not contain `repo_path`, ask the user: "What is the absolute path to your minicrew checkout on this machine?" Validate the answer exists as a directory and contains `worker/__init__.py` and `SETUP.md`. Then write `~/.claude/minicrew.json` with `{"repo_path": "<that path>"}`.
- If the file exists but the path no longer exists on disk, tell the user and ask for the new path. Update the file.

Store the resolved path as `REPO_PATH` for the rest of this skill.

### 2. Pre-trust the repo directory

Run:

```
"$REPO_PATH/.venv/bin/python" -m worker.utils.paths trust "$REPO_PATH"
```

If the venv does not exist yet, fall back to the system Python 3.11+ and run the same command — the trust writer does not depend on the venv. If trust fails, report the stderr and stop.

### 3. Ask only what the user wants to change

Ask the user: "Which of these do you want to change? (pick any subset, or 'none' to just re-register launchd)"

- role (primary vs secondary)
- instance count (1-5)
- config path (`MINICREW_CONFIG_PATH`)

For each item the user picks, ask for the new value. Skip items they decline. If they say "none", keep all existing `.env` values as-is.

### 4. Tear down existing services

Run:

```
bash "$REPO_PATH/teardown.sh"
```

This removes all current launchd services for minicrew on this machine. Workers in flight will requeue their jobs and mark themselves offline as they exit.

### 5. Update `.env` if needed

If the user changed any values in step 3, read `$REPO_PATH/.env`, update only the keys the user changed (`WORKER_ROLE`, `MINICREW_CONFIG_PATH`), and write it back. Never touch keys the user did not ask to change. Never print the service-role key back to the user.

If a `worker-config/` config path changed, validate the new path first:

```
"$REPO_PATH/.venv/bin/python" -m worker.config.loader --validate "$NEW_CONFIG_PATH"
```

Stop on validation error.

### 6. Re-install launchd services

For each instance from 1 to N (where N is the instance count), run:

```
"$REPO_PATH/.venv/bin/python" -m worker.platform install \
    --instance $i \
    --role "$ROLE" \
    --config-path "$MINICREW_CONFIG_PATH"
```

If any instance install fails, stop and report the stderr. Do not continue silently.

### 7. Refresh skills and agents

Re-copy the repo's skills and agents so any updates pulled in `git pull` are reflected in this user's Claude Code installation. Both copies are idempotent.

```
mkdir -p "$HOME/.claude/commands/minicrew"
cp "$REPO_PATH"/skills/*.md "$HOME/.claude/commands/minicrew/"

mkdir -p "$HOME/.claude/agents"
if [ -d "$REPO_PATH/agents" ] && ls "$REPO_PATH"/agents/*.md >/dev/null 2>&1; then
  cp "$REPO_PATH"/agents/*.md "$HOME/.claude/agents/"
fi

ls "$HOME/.claude/commands/minicrew/"
ls "$HOME/.claude/agents/"
```

This mirrors `SETUP.md` Step 7 (the first-time install). If `agents/` does not exist in the repo (older minicrew checkout), skip the agents copy silently. If the user's Claude Code version pre-dates user-scoped agents discovery, the agents copy is inert (no harm).

### 8. Verify

Run:

```
launchctl list | grep com.minicrew.worker
```

Confirm there are exactly N entries. If not, report the mismatch.

Tail the first log for a few seconds to confirm the worker booted:

```
tail -n 20 "$REPO_PATH/logs/worker-1.log"
```

Look for a `worker_started` event in the output. If you don't see one within ~15 seconds, report what you did see and stop.

### 9. Report

Summarize to the user:
- Instance count now running on this machine.
- Role.
- Config path.
- Whether the `worker_started` event was observed in the log.

Suggest `/minicrew:status` for a fleet-wide view across all machines.

## On Linux Mint XFCE

Follow these steps in order. Stop on any error and report it — never paper over failures.

### 1. Locate the repo

Read `~/.claude/minicrew.json`. It should contain `{"repo_path": "<absolute path>"}`.

- If the file is missing or does not contain `repo_path`, ask the user: "What is the absolute path to your minicrew checkout on this machine?" Validate the answer exists as a directory and contains `worker/__init__.py` and `SETUP.md`. Then write `~/.claude/minicrew.json` with `{"repo_path": "<that path>"}`.
- If the file exists but the path no longer exists on disk, tell the user and ask for the new path. Update the file.

Store the resolved path as `REPO_PATH` for the rest of this skill.

### 2. Verify prerequisites are installed

Confirm the required apt packages are present. Run:

```
dpkg -s python3-venv wmctrl xdotool xfce4-terminal tmux >/dev/null 2>&1 && echo OK || echo MISSING
```

If the output is `MISSING`, tell the user to run:

```
sudo apt install python3-venv wmctrl xdotool xfce4-terminal tmux
```

Then verify Claude Code is on PATH:

```
which claude
```

If it is not installed, tell the user to install it via:

```
sudo npm install -g @anthropic-ai/claude-code
```

(Or user-level npm if they prefer; either works as long as `claude` ends up on `PATH`.)

### 3. Verify the XFCE X11 session

Visible-terminal mode requires an **Xfce Session (X11)** — Wayland sessions cannot be driven by wmctrl/xdotool. Check:

```
echo "$XDG_SESSION_TYPE"
```

- If the output is `x11` or empty, you're good.
- If the output is `wayland`, stop and tell the user: at the LightDM login screen, click the session-type icon (usually in the top-right or near the username) and select **Xfce Session** (not "Xfce Session (Wayland)" or GNOME/Cinnamon). Log back in, then re-run this skill.

**Do NOT run `loginctl enable-linger`.** Lingering would start the user manager before login, but visible-terminal mode needs a real GUI session to spawn windows — enabling linger breaks it. If the user has previously enabled linger on this machine, tell them to disable it with:

```
sudo loginctl disable-linger "$USER"
```

### 4. Pre-trust the repo directory

Run:

```
"$REPO_PATH/.venv/bin/python" -m worker.utils.paths trust "$REPO_PATH"
```

If the venv does not exist yet, fall back to the system Python 3.11+ and run the same command. If trust fails, report the stderr and stop.

### 5. Ask only what the user wants to change

Ask the user: "Which of these do you want to change? (pick any subset, or 'none' to just re-register the systemd user services)"

- role (primary vs secondary)
- instance count (1-5)
- config path (`MINICREW_CONFIG_PATH`)

For each item the user picks, ask for the new value. Skip items they decline. If they say "none", keep all existing `.env` values as-is.

### 6. Update `.env` if needed

If `$REPO_PATH/.env` does not exist, copy `$REPO_PATH/.env.example` to `$REPO_PATH/.env` and tell the user to fill in `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_DB_URL`, and `MINICREW_CONFIG_PATH`. Stop until they confirm.

If the user changed any values in step 5, read `$REPO_PATH/.env`, update only the keys the user changed (`WORKER_ROLE`, `MINICREW_CONFIG_PATH`), and write it back. Never touch keys the user did not ask to change. Never print the service-role key back to the user.

If a `worker-config/` config path changed, validate the new path first:

```
"$REPO_PATH/.venv/bin/python" -m worker.config.loader --validate "$NEW_CONFIG_PATH"
```

Stop on validation error.

### 7. Run the setup script

Determine the instance count `N` (from the user's answer in step 5, or the current count if unchanged). Run:

```
bash "$REPO_PATH/setup.sh" --workers $N --role "$ROLE"
```

`setup.sh` will dispatch to `python -m worker.platform install` under the hood for each instance. If it fails, report the stderr verbatim and stop.

### 7b. Refresh skills and agents

Re-copy the repo's skills and agents so any updates pulled in `git pull` are reflected in this user's Claude Code installation. Both copies are idempotent.

```
mkdir -p "$HOME/.claude/commands/minicrew"
cp "$REPO_PATH"/skills/*.md "$HOME/.claude/commands/minicrew/"

mkdir -p "$HOME/.claude/agents"
if [ -d "$REPO_PATH/agents" ] && ls "$REPO_PATH"/agents/*.md >/dev/null 2>&1; then
  cp "$REPO_PATH"/agents/*.md "$HOME/.claude/agents/"
fi

ls "$HOME/.claude/commands/minicrew/"
ls "$HOME/.claude/agents/"
```

This mirrors `SETUP.md` Step 8 (the first-time install). If `agents/` does not exist in the repo (older minicrew checkout), skip the agents copy silently. If the user's Claude Code version pre-dates user-scoped agents discovery, the agents copy is inert (no harm).

### 8. Optional: dedicated `minicrew` user (shared boxes only)

If this box is shared with other users or other workloads, recommend creating a dedicated `minicrew` system user and configuring LightDM to auto-login as that user. Under X11, any process running as the same uid can inject keystrokes into the Claude terminal via xdotool; a dedicated uid contains that blast radius. On a dedicated box (Mac-Mini-equivalent), this is optional.

If the user wants to set it up, tell them:

```
sudo adduser minicrew
# ... then configure /etc/lightdm/lightdm.conf autologin-user=minicrew ...
```

Point them at `docs/LINUX.md` for the complete walkthrough.

### 9. Verify

Run:

```
systemctl --user list-units --all 'minicrew-worker-*'
```

Confirm there are exactly N units and they are `active (running)`. If not, report the mismatch.

Tail the first log for a few seconds to confirm the worker booted:

```
tail -n 20 "$REPO_PATH/logs/worker-1.log"
```

Look for a `worker_started` event in the output. If you don't see one within ~15 seconds, report what you did see and stop.

### 10. Report

Summarize to the user:
- Instance count now running on this machine.
- Role.
- Config path.
- Whether the `worker_started` event was observed in the log.

Suggest `/minicrew:status` for a fleet-wide view across all machines.
