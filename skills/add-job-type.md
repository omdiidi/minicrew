---
name: minicrew:add-job-type
description: Interactively append a new job_type to the current project's worker-config/config.yaml.
---

You are adding a new `job_type` entry to an existing consumer project's `worker-config/config.yaml`. Run this skill in the root of the consumer project (the directory that contains `worker-config/`).

Follow these steps in order. Stop on any error and report it.

## 1. Confirm location

Check that `worker-config/config.yaml` exists in the current directory. If not, stop and tell the user: "I can't find `worker-config/config.yaml` in the current directory. Either `cd` to your consumer project root, or run `/minicrew:scaffold-project` first to create the initial config."

## 2. Locate the minicrew repo (for validation)

Read `~/.claude/minicrew.json` for `repo_path`. If missing, ask the user for the absolute path to their minicrew checkout and persist it to `~/.claude/minicrew.json`. Store as `REPO_PATH`.

## 3. Ask the user for the new job type fields

Ask in order, one at a time:

1. **Name** — snake_case. Confirm it is not already a key under `job_types` in the current config (read `worker-config/config.yaml` to check). If it is, refuse and ask for a different name.
2. **Mode** — `single` or `fan_out`. Default `single`.
3. **Model** — default `claude-sonnet-4-6`.
4. **Thinking budget** — `none`, `medium`, or `high`. Default `medium`.
5. **Timeout seconds** — default `3600`.
6. **Skill** — optional Claude Code skill to prefix the prompt with, e.g. `myorg:process`. Accept `none` or empty.
7. If mode is `fan_out`:
   - **Group names** — comma-separated list of snake_case group names (e.g., `alpha, beta, gamma`).
   - Merge prompt is implied and will live at `worker-config/prompts/<NAME>_merge.md.j2`.

## 4. Append the new job type to `config.yaml`

Preserve existing formatting, comments, and key order. Prefer `ruamel.yaml` if it is installed in the minicrew venv (it round-trips comments); otherwise fall back to appending a carefully-indented block under the `job_types:` mapping.

To detect ruamel availability:

```
"$REPO_PATH/.venv/bin/python" -c "import ruamel.yaml" 2>/dev/null && echo yes || echo no
```

### Single-mode entry shape

```yaml
  <NAME>:
    description: "TODO: describe this job type"
    mode: single
    skill: <SKILL_OR_null>
    model: <MODEL>
    thinking_budget: <BUDGET>
    timeout_seconds: <TIMEOUT>
    prompt_template: <NAME>.md.j2
    result_filename: result.json
    idle_timeout_seconds: 1500
    result_idle_timeout_seconds: 900
```

### Fan-out entry shape

```yaml
  <NAME>:
    description: "TODO: describe this job type"
    mode: fan_out
    skill: <SKILL_OR_null>
    model: <MODEL>
    thinking_budget: <BUDGET>
    timeout_seconds: <TIMEOUT>
    result_filename: result.json
    idle_timeout_seconds: 1500
    result_idle_timeout_seconds: 900
    groups:
      - name: <GROUP_1>
        prompt_template: <NAME>_<GROUP_1>.md.j2
        result_filename: group_result.json
      - name: <GROUP_2>
        prompt_template: <NAME>_<GROUP_2>.md.j2
        result_filename: group_result.json
    merge:
      prompt_template: <NAME>_merge.md.j2
      result_filename: result.json
```

## 5. Create prompt template stubs

### Single mode

Create `worker-config/prompts/<NAME>.md.j2`:

```
{# Starter prompt for <NAME>. Edit to match the real workload. #}
{# Use `| tojson` for untrusted payload fields — see minicrew SECURITY.md. #}

Process this payload:

{{ payload | tojson }}

Write your result to `result.json` in the current directory.
```

### Fan-out mode

For each group name, create `worker-config/prompts/<NAME>_<GROUP>.md.j2`:

```
{# Group <GROUP> prompt for <NAME> job type. #}

You are handling the <GROUP> group of a <NAME> job.

Payload:

{{ payload | tojson }}

Write your group's output to `group_result.json` in the current directory.
```

And `worker-config/prompts/<NAME>_merge.md.j2`:

```
{# Merge prompt for <NAME> job type. #}

You are merging the outputs of all groups for a <NAME> job.

Each group has written a `group_result.json` file. Read them, combine the data, and write the merged result to `result.json` in the current directory.
```

## 6. Validate the resulting config

Run:

```
"$REPO_PATH/.venv/bin/python" -m worker.config.loader --validate ./worker-config
```

If validation fails, report the error verbatim. Revert the config changes you made if the user asks.

## 7. Report

Summarize:
- New job type name.
- Mode.
- Files created under `worker-config/prompts/`.

Remind the user:

> "Workers currently running will not pick up this change until they restart — v1 has no config hot-reload. On each Mac Mini running a worker for this project, either run `/minicrew:setup` or `launchctl kickstart -k gui/$(id -u)/com.minicrew.worker.1` (and the same for instances 2..N)."
