---
name: minicrew:tune
description: Change model, thinking budget, or timeout for a job_type in the current project's config.yaml.
---

You are changing tuning parameters for an existing `job_type` in a consumer project's `worker-config/config.yaml`. Run this skill in the consumer project root.

Follow these steps in order. Stop on any error and report it.

## 1. Confirm location

Check that `worker-config/config.yaml` exists in the current directory. If not, stop and tell the user: "I can't find `worker-config/config.yaml` in the current directory. Either `cd` to your consumer project root, or run `/minicrew:scaffold-project` first."

## 2. Locate the minicrew repo

Read `~/.claude/minicrew.json` for `repo_path`. If missing, ask the user for the absolute path and persist it. Store as `REPO_PATH`.

## 3. List the available job types

Read `worker-config/config.yaml`. Enumerate the keys under `job_types:`. Show them to the user and ask: "Which job type do you want to tune?"

If the user's answer is not one of the listed keys, refuse and ask again.

## 4. Ask which field to change

Ask the user: "Which field? (pick one)"

Allowed fields:
- `model`
- `thinking_budget` (`none` | `medium` | `high`)
- `timeout_seconds`
- `idle_timeout_seconds`
- `result_idle_timeout_seconds`

If the user types a field not in this list, refuse and re-ask. Do not allow tuning structural fields (`mode`, `prompt_template`, `groups`, `merge`) via this skill — direct them to `/minicrew:add-job-type` or manual editing for structural changes.

## 5. Ask for the new value

Ask for the new value. Enforce the simple type checks:
- `thinking_budget` must be one of `none`, `medium`, `high`.
- The three `*_seconds` fields must be positive integers.
- `model` is a free-form string.

Reject and re-ask on type mismatch.

## 6. Update the config preserving formatting

Prefer `ruamel.yaml` if available in the minicrew venv — it round-trips comments and key order cleanly. Check:

```
"$REPO_PATH/.venv/bin/python" -c "import ruamel.yaml" 2>/dev/null && echo yes || echo no
```

If `ruamel.yaml` is available, use it to load, mutate only the target field, and dump. Otherwise fall back to a targeted line-based edit: locate the target `job_types.<NAME>.<FIELD>:` line and replace only its value, leaving surrounding lines byte-identical.

Never rewrite the whole file from a parsed-and-re-dumped pyyaml object — pyyaml will lose comments and may reorder keys.

## 7. Validate

Run:

```
"$REPO_PATH/.venv/bin/python" -m worker.config.loader --validate ./worker-config
```

Stop on validation error. Report the stderr verbatim. If the user asks, offer to revert.

## 8. Report and remind about restart

Tell the user:
- Which job type and field were changed.
- Old value → new value.

Remind them to restart workers so the change takes effect:

> "Workers running on Mac Minis will not pick up this change until they restart. v1 has no hot-reload. For each Mac Mini running this project's workers:
>
> `launchctl kickstart -k gui/$(id -u)/com.minicrew.worker.1`
>
> Repeat for instances 2..N. Or run `/minicrew:setup` on each machine."
