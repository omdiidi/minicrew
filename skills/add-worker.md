---
name: minicrew:add-worker
description: Add another worker instance on this machine (macOS or Linux Mint XFCE).
---

You are adding one more worker instance to the existing minicrew installation on this machine. This is idempotent at the service level — if the target instance number somehow already exists, the install step will refuse rather than clobber.

## Detect OS first

Run `uname -s` via Bash.
- If output is `Darwin`, follow the "On macOS" section below.
- If output is `Linux`, follow the "On Linux Mint XFCE" section below.
- Otherwise, stop and tell the user: minicrew supports macOS and Linux Mint XFCE only.

## On macOS

Follow these steps in order. Stop on any error and report it.

### 1. Locate the repo

Read `~/.claude/minicrew.json` for `repo_path`.

- If the file is missing, tell the user: "minicrew is not installed on this machine yet. Read `SETUP.md` in the minicrew repo first." and stop.
- Otherwise store the path as `REPO_PATH`.

### 2. Enumerate existing instances

Run:

```
launchctl list | grep com.minicrew.worker
```

Parse the output to extract the instance number at the end of each label (e.g. `com.minicrew.worker.2` → `2`). Store the set of used instance numbers as `USED`.

### 3. Pick the next instance number

Pick the **lowest integer in `1..5` that is NOT in `USED`** as `NEXT_INSTANCE`. This correctly handles non-contiguous installs (for example if instance 2 was torn down and instances 1 and 3 are running, `NEXT_INSTANCE = 2`).

If `USED` contains every number in `1..5`, stop and tell the user: "This machine already has 5 worker instances, which is the supported maximum. Add another machine with `/minicrew:add-machine` instead."

### 4. Read role and config path from `.env`

Read `$REPO_PATH/.env`. Extract `WORKER_ROLE` (default `primary` if not set) and `MINICREW_CONFIG_PATH`. If `MINICREW_CONFIG_PATH` is missing, stop and tell the user to run `/minicrew:setup` to set it.

### 5. Install the new launchd service

Run:

```
"$REPO_PATH/.venv/bin/python" -m worker.platform install \
    --instance $NEXT_INSTANCE \
    --role "$WORKER_ROLE" \
    --config-path "$MINICREW_CONFIG_PATH"
```

If this fails, report the stderr verbatim and stop.

### 6. Wait and verify heartbeat

Wait 10 seconds for the new worker to register itself in the `workers` table.

Run:

```
"$REPO_PATH/.venv/bin/python" -m worker --status
```

Parse the JSON output. Confirm the total worker count has increased by exactly 1 and includes an entry for this hostname with instance `$NEXT_INSTANCE`. If the new worker does not appear, tail `$REPO_PATH/logs/worker-$NEXT_INSTANCE.log` and report the last 20 lines.

### 7. Report

Tell the user:
- Instance number added.
- The new worker id (format: `<prefix>-<hostname>-<instance>`).
- Current status from `--status` output.
- Total workers now on this machine.

## On Linux Mint XFCE

Follow these steps in order. Stop on any error and report it.

### 1. Locate the repo

Read `~/.claude/minicrew.json` for `repo_path`.

- If the file is missing, tell the user: "minicrew is not installed on this machine yet. Read `SETUP.md` in the minicrew repo first." and stop.
- Otherwise store the path as `REPO_PATH`.

### 2. Enumerate existing instances

Run:

```
systemctl --user list-units --all 'minicrew-worker-*'
```

Parse the output to extract the instance number from each unit name (e.g. `minicrew-worker-2.service` → `2`). Store the set of used instance numbers as `USED`.

### 3. Pick the next instance number

Pick the **lowest integer in `1..5` that is NOT in `USED`** as `NEXT_INSTANCE`. This correctly handles non-contiguous installs.

If `USED` contains every number in `1..5`, stop and tell the user: "This machine already has 5 worker instances, which is the supported maximum. Add another machine with `/minicrew:add-machine` instead."

### 4. Read role from `.env`

Read `$REPO_PATH/.env`. Extract `WORKER_ROLE` (default `primary` if not set). If `MINICREW_CONFIG_PATH` is missing, stop and tell the user to run `/minicrew:setup` to set it.

### 5. Run setup.sh to add the instance

Determine the current count `N` from step 2 and compute the new total `N + 1`. Run:

```
bash "$REPO_PATH/setup.sh" --workers $((N + 1)) --role "$WORKER_ROLE"
```

`setup.sh` is idempotent — existing instances are left alone; only the new instance is installed. If this fails, report the stderr verbatim and stop.

### 6. Verify the new unit is running

Run:

```
systemctl --user status minicrew-worker-$NEXT_INSTANCE.service
```

Confirm it shows `active (running)`. If it is not running, view its journal for diagnostics:

```
journalctl --user -u minicrew-worker-$NEXT_INSTANCE.service -n 50 --no-pager
```

The canonical log, however, is the file. Tail it to confirm `worker_started`:

```
tail -n 20 "$REPO_PATH/logs/worker-$NEXT_INSTANCE.log"
```

### 7. Wait and verify heartbeat

Wait 10 seconds for the new worker to register itself in the `workers` table.

Run:

```
"$REPO_PATH/.venv/bin/python" -m worker --status
```

Parse the JSON output. Confirm the total worker count has increased by exactly 1 and includes an entry for this hostname with instance `$NEXT_INSTANCE`. If the new worker does not appear, tail `$REPO_PATH/logs/worker-$NEXT_INSTANCE.log` and report the last 20 lines.

### 8. Report

Tell the user:
- Instance number added.
- The new worker id (format: `<prefix>-<hostname>-<instance>`).
- Current status from `--status` output.
- Total workers now on this machine.
