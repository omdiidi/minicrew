---
name: minicrew:status
description: Show fleet health (all machines, all instances). Reads from DB so output reflects the entire fleet, not just this machine.
---

You are reporting fleet health for the minicrew deployment. The output covers **every worker on every machine** — not just this machine — because the status command reads directly from the shared database.

## Detect OS first

Run `uname -s` via Bash. The fleet-wide status query is identical on both OSes, but the machine-local service inspection (used when a worker looks unhealthy) differs.
- If output is `Darwin`, follow the "On macOS" section below.
- If output is `Linux`, follow the "On Linux Mint XFCE" section below.
- Otherwise, stop and tell the user: minicrew supports macOS and Linux Mint XFCE only.

## On macOS

Follow these steps in order. Stop on any error and report it.

### 1. Locate the repo

Read `~/.claude/minicrew.json` for `repo_path`. If missing, tell the user: "minicrew is not registered on this machine. If it's installed, tell me the absolute path to the checkout so I can save it; otherwise, run `SETUP.md` first." Persist the path once provided.

Store as `REPO_PATH`.

### 2. Run the status command

Run:

```
"$REPO_PATH/.venv/bin/python" -m worker --status
```

This queries the `workers` table and the `worker_stats` view via PostgREST and emits JSON on stdout. It exits 0 even if there are no workers registered.

If the command errors (non-zero exit code), report the stderr verbatim and stop. Common causes: missing `.env`, wrong `SUPABASE_URL`, Supabase unreachable.

### 3. Parse and render

Parse the JSON output. It will have at least these keys:
- `workers` — list of worker records. Each has `id`, `hostname`, `instance`, `role`, `status`, `last_heartbeat`, `version`.
- `queue_depth` — integer count of `pending` jobs.
- `running_count` — integer count of `running` jobs.
- `recent_errors_1h` — integer count of `error` completions in the last hour.
- `recent_failed_permanent_24h` — integer count of poison-pilled jobs in the last 24 hours.

Render a readable table for the user:

```
Workers:
  ID                       HOSTNAME          INSTANCE   ROLE        STATUS    HEARTBEAT AGE
  <id>                     <hostname>        <n>        <role>      <status>  <age>s
  ...

Queue:
  Pending:                 <queue_depth>
  Running:                 <running_count>
  Errors (last hour):      <recent_errors_1h>
  Failed permanent (24h):  <recent_failed_permanent_24h>
```

Compute heartbeat age as `now - last_heartbeat` in seconds. If a worker's `status` is not `offline` and age > 120s, flag that row with a `(STALE)` suffix so the user can see it.

### 4. Machine-local inspection (optional)

If any worker on **this** machine looks unhealthy in the table above (stale heartbeat, unexpected status), also run:

```
launchctl list | grep com.minicrew.worker
tail -n 50 "$REPO_PATH/logs/worker-<N>.log"
```

for each unhealthy instance `N`. Report what you see.

### 5. Handle the empty case

If the `workers` list is empty, tell the user:

> "No workers are registered. Either nothing has been installed yet, or every worker has marked itself offline. To bring one up, run `SETUP.md` on a machine, or `/minicrew:add-worker` on a machine that already has minicrew installed."

### 6. Report

Summarize at the end:
- Total workers across all machines.
- How many are `idle`, `busy`, `offline`.
- Whether anything looks wrong (stale heartbeats, many recent errors, growing queue).

Do not suggest fixes unless asked — this skill reports health, it doesn't remediate.

## On Linux Mint XFCE

Follow these steps in order. Stop on any error and report it.

### 1. Locate the repo

Read `~/.claude/minicrew.json` for `repo_path`. If missing, tell the user: "minicrew is not registered on this machine. If it's installed, tell me the absolute path to the checkout so I can save it; otherwise, run `SETUP.md` first." Persist the path once provided.

Store as `REPO_PATH`.

### 2. Run the status command

Run:

```
"$REPO_PATH/.venv/bin/python" -m worker --status
```

This queries the `workers` table and the `worker_stats` view via PostgREST and emits JSON on stdout. It exits 0 even if there are no workers registered. (The Supabase query is fleet-wide, so it returns rows for Macs as well as Linux boxes.)

If the command errors (non-zero exit code), report the stderr verbatim and stop. Common causes: missing `.env`, wrong `SUPABASE_URL`, Supabase unreachable.

### 3. Parse and render

Parse the JSON output. It will have at least these keys:
- `workers` — list of worker records. Each has `id`, `hostname`, `instance`, `role`, `status`, `last_heartbeat`, `version`.
- `queue_depth` — integer count of `pending` jobs.
- `running_count` — integer count of `running` jobs.
- `recent_errors_1h` — integer count of `error` completions in the last hour.
- `recent_failed_permanent_24h` — integer count of poison-pilled jobs in the last 24 hours.

Render a readable table for the user:

```
Workers:
  ID                       HOSTNAME          INSTANCE   ROLE        STATUS    HEARTBEAT AGE
  <id>                     <hostname>        <n>        <role>      <status>  <age>s
  ...

Queue:
  Pending:                 <queue_depth>
  Running:                 <running_count>
  Errors (last hour):      <recent_errors_1h>
  Failed permanent (24h):  <recent_failed_permanent_24h>
```

Compute heartbeat age as `now - last_heartbeat` in seconds. If a worker's `status` is not `offline` and age > 120s, flag that row with a `(STALE)` suffix so the user can see it.

### 4. Machine-local inspection (optional)

If any worker on **this** machine looks unhealthy in the table above (stale heartbeat, unexpected status), also run:

```
systemctl --user list-units --all 'minicrew-worker-*'
journalctl --user -u minicrew-worker-<N>.service -n 50 --no-pager
tail -n 50 "$REPO_PATH/logs/worker-<N>.log"
```

for each unhealthy instance `N`. Report what you see. Note: the log file is the canonical source of worker events; the journal contains only the systemd unit's stdout/stderr (which minicrew keeps minimal via `append:` file output).

### 5. Handle the empty case

If the `workers` list is empty, tell the user:

> "No workers are registered. Either nothing has been installed yet, or every worker has marked itself offline. To bring one up, run `SETUP.md` on a machine, or `/minicrew:add-worker` on a machine that already has minicrew installed."

### 6. Report

Summarize at the end:
- Total workers across all machines.
- How many are `idle`, `busy`, `offline`.
- Whether anything looks wrong (stale heartbeats, many recent errors, growing queue).

Do not suggest fixes unless asked — this skill reports health, it doesn't remediate.
