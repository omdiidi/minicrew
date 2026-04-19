---
name: minicrew:add-worker
description: Add another worker instance on this Mac Mini.
---

You are adding one more worker instance to the existing minicrew installation on this Mac Mini. This is idempotent at the launchd level — if the target instance number somehow already exists, the install step will refuse rather than clobber.

Follow these steps in order. Stop on any error and report it.

## 1. Locate the repo

Read `~/.claude/minicrew.json` for `repo_path`.

- If the file is missing, tell the user: "minicrew is not installed on this machine yet. Read `SETUP.md` in the minicrew repo first." and stop.
- Otherwise store the path as `REPO_PATH`.

## 2. Enumerate existing instances

Run:

```
launchctl list | grep com.minicrew.worker.
```

Parse the output to extract the instance number at the end of each label (e.g. `com.minicrew.worker.2` → `2`). Store the set of used instance numbers as `USED`.

## 3. Pick the next instance number

Pick the **lowest integer in `1..5` that is NOT in `USED`** as `NEXT_INSTANCE`. This correctly handles non-contiguous installs (for example if instance 2 was torn down and instances 1 and 3 are running, `NEXT_INSTANCE = 2`).

If `USED` contains every number in `1..5`, stop and tell the user: "This machine already has 5 worker instances, which is the supported maximum. Add another Mac Mini with `/minicrew:add-machine` instead."

## 4. Read role and config path from `.env`

Read `$REPO_PATH/.env`. Extract `WORKER_ROLE` (default `primary` if not set) and `MINICREW_CONFIG_PATH`. If `MINICREW_CONFIG_PATH` is missing, stop and tell the user to run `/minicrew:setup` to set it.

## 5. Install the new launchd service

Run:

```
"$REPO_PATH/.venv/bin/python" -m worker.utils.launchd install \
    --instance $NEXT_INSTANCE \
    --role "$WORKER_ROLE" \
    --config-path "$MINICREW_CONFIG_PATH"
```

If this fails, report the stderr verbatim and stop.

## 6. Wait and verify heartbeat

Wait 10 seconds for the new worker to register itself in the `workers` table.

Run:

```
"$REPO_PATH/.venv/bin/python" -m worker --status
```

Parse the JSON output. Confirm the total worker count has increased by exactly 1 and includes an entry for this hostname with instance `$NEXT_INSTANCE`. If the new worker does not appear, tail `$REPO_PATH/logs/worker-$NEXT_INSTANCE.log` and report the last 20 lines.

## 7. Report

Tell the user:
- Instance number added.
- The new worker id (format: `<prefix>-<hostname>-<instance>`).
- Current status from `--status` output.
- Total workers now on this machine.
