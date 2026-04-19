---
name: minicrew:setup
description: Re-setup or reconfigure minicrew on this Mac Mini. Idempotent. (For FIRST-TIME install, read SETUP.md directly.)
---

You are re-running or reconfiguring an existing minicrew installation on this Mac Mini. This skill is **not** for first-time install. If the user has never installed minicrew on this machine, stop and tell them: "First-time installation is documented in `SETUP.md` at the root of the minicrew repo. Clone the repo, open Claude Code in the cloned directory, and read `SETUP.md` — it is Claude-executable. Come back to `/minicrew:setup` for subsequent re-runs."

Follow these steps in order. Stop on any error and report it — never paper over failures.

## 1. Locate the repo

Read `~/.claude/minicrew.json`. It should contain `{"repo_path": "<absolute path>"}`.

- If the file is missing or does not contain `repo_path`, ask the user: "What is the absolute path to your minicrew checkout on this machine?" Validate the answer exists as a directory and contains `worker/__init__.py` and `SETUP.md`. Then write `~/.claude/minicrew.json` with `{"repo_path": "<that path>"}`.
- If the file exists but the path no longer exists on disk, tell the user and ask for the new path. Update the file.

Store the resolved path as `REPO_PATH` for the rest of this skill.

## 2. Pre-trust the repo directory

Run:

```
"$REPO_PATH/.venv/bin/python" -m worker.utils.paths trust "$REPO_PATH"
```

If the venv does not exist yet, fall back to the system Python 3.11+ and run the same command — the trust writer does not depend on the venv. If trust fails, report the stderr and stop.

## 3. Ask only what the user wants to change

Ask the user: "Which of these do you want to change? (pick any subset, or 'none' to just re-register launchd)"

- role (primary vs secondary)
- instance count (1-5)
- config path (`MINICREW_CONFIG_PATH`)

For each item the user picks, ask for the new value. Skip items they decline. If they say "none", keep all existing `.env` values as-is.

## 4. Tear down existing services

Run:

```
bash "$REPO_PATH/teardown.sh"
```

This removes all current launchd services for minicrew on this machine. Workers in flight will requeue their jobs and mark themselves offline as they exit.

## 5. Update `.env` if needed

If the user changed any values in step 3, read `$REPO_PATH/.env`, update only the keys the user changed (`WORKER_ROLE`, `MINICREW_CONFIG_PATH`), and write it back. Never touch keys the user did not ask to change. Never print the service-role key back to the user.

If a `worker-config/` config path changed, validate the new path first:

```
"$REPO_PATH/.venv/bin/python" -m worker.config.loader --validate "$NEW_CONFIG_PATH"
```

Stop on validation error.

## 6. Re-install launchd services

For each instance from 1 to N (where N is the instance count), run:

```
"$REPO_PATH/.venv/bin/python" -m worker.utils.launchd install \
    --instance $i \
    --role "$ROLE" \
    --config-path "$MINICREW_CONFIG_PATH"
```

If any instance install fails, stop and report the stderr. Do not continue silently.

## 7. Verify

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

## 8. Report

Summarize to the user:
- Instance count now running on this machine.
- Role.
- Config path.
- Whether the `worker_started` event was observed in the log.

Suggest `/minicrew:status` for a fleet-wide view across all machines.
