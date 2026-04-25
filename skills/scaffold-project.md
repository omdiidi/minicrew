---
name: minicrew:scaffold-project
description: Run inside a consumer project repo. Creates worker-config/ with a starter config.yaml and prompt template so this project can enqueue jobs.
---

You are adding a `worker-config/` directory to a **consumer project** — a repo that wants to enqueue jobs for a minicrew worker fleet to process. You are **not** in the minicrew repo itself.

Follow these steps in order. Stop on any error and report it.

## 1. Confirm you are in a consumer project, not the minicrew repo

Check the current working directory for `worker/__init__.py`. If that file exists, stop immediately and tell the user: "You appear to be inside the minicrew repo itself. This skill scaffolds a **consumer** project — a different repo that will enqueue jobs. Change to your consumer project's directory and re-run."

If `worker-config/config.yaml` already exists in the current directory, stop and tell the user: "This project already has a `worker-config/config.yaml`. Use `/minicrew:add-job-type` to add another job type, or `/minicrew:tune` to change existing settings."

## 2. Locate the minicrew repo (for schema reference)

Read `~/.claude/minicrew.json` for `repo_path`. If missing, ask the user: "What is the absolute path to your minicrew checkout? (I need it to reference the config schema and payload-schema template.)" Validate the path contains `schema/config.schema.json` and write `~/.claude/minicrew.json` with `{"repo_path": "<that path>"}`. Store as `REPO_PATH`.

## 3. Ask the user for the scaffold inputs

Ask in order:

1. **Project nickname** — a short snake_case string used as a file prefix (e.g., `myapp`).
2. **First job type name** — snake_case, the canonical name of the first kind of work this project will enqueue (e.g., `summarize`, `classify`, `extract`).
3. **Skill to invoke per job** — optional. If the user wants each job to start by invoking a Claude Code skill, ask for its name (e.g., `myorg:process`). Accept `none` or empty.
4. **Default model** — default is `claude-sonnet-4-6`. Accept user override.
5. **Payload schema** — ask yes/no: "Do you want a `payload.schema.json` to validate job payloads before they run? (Recommended for production.)"

Do not guess any of these — ask and wait.

## 4. Create `worker-config/config.yaml`

Create the `worker-config/` directory. Write `worker-config/config.yaml` with this exact structure, substituting the user's answers:

```yaml
schema_version: 1

db:
  jobs_table: jobs
  workers_table: workers
  events_table: worker_events
  url: ${SUPABASE_URL}
  service_key: ${SUPABASE_SERVICE_ROLE_KEY}
  direct_url: ${SUPABASE_DB_URL}

worker:
  prefix: <PROJECT_NICKNAME>
  role: primary
  poll_interval_seconds: null

reaper:
  stale_threshold_seconds: 120
  interval_seconds: 60
  max_attempts: 3

job_types:
  <JOB_TYPE_NAME>:
    description: "TODO: describe this job type"
    mode: single
    skill: <SKILL_OR_NULL>
    model: <MODEL>
    thinking_budget: medium
    timeout_seconds: 3600
    prompt_template: <JOB_TYPE_NAME>.md.j2
    result_filename: result.json
    idle_timeout_seconds: 1500
    result_idle_timeout_seconds: 900

logging:
  level: info
  format: json
  redact_env:
    - SUPABASE_SERVICE_ROLE_KEY
    - SUPABASE_DB_URL
  sinks:
    - type: file
      path: logs/worker-{instance}.log
      rotate: daily
      keep: 30
  job_output:
    capture: true
    retention_days: 7
```

If the user said `none` for the skill, render it as `skill: null` (YAML null, not the string `"none"`).

## 5. Create the starter prompt template

Create `worker-config/prompts/<JOB_TYPE_NAME>.md.j2`:

```
{# Starter prompt for <JOB_TYPE_NAME>. Edit this to match your actual workload. #}
{# Reminder: use `| tojson` for any untrusted payload fields. See minicrew SECURITY.md. #}

You are processing a <JOB_TYPE_NAME> job.

Input payload:
{{ payload | tojson }}

Text to process:
{{ payload.text }}

Write your output to a file named `result.json` in the current directory. The file must be valid JSON.
```

## 6. Optionally create `payload.schema.json`

If the user said yes in step 3, copy `$REPO_PATH/schema/payload.schema.example.json` to `worker-config/payload.schema.json`. Tell the user: "I copied a permissive starter schema. Edit `worker-config/payload.schema.json` to require the fields your job actually needs."

If the user said no, do nothing.

## 7. Validate what you created

Run:

```
"$REPO_PATH/.venv/bin/python" -m worker.config.loader --validate ./worker-config
```

If validation fails, report the error verbatim and stop. Do not proceed to step 8 until validation passes.

## 8. Report the absolute config path

Resolve the absolute path to the `worker-config/` directory you just created. Print it clearly to the user:

```
worker-config absolute path:
<ABSOLUTE_PATH>
```

Tell the user:

> "Your consumer project is now configured. Next:
>
> 1. On your Mac Mini, either run `/minicrew:setup` and point `MINICREW_CONFIG_PATH` at the absolute path above, **or** edit the Mac Mini's `.env` directly to set `MINICREW_CONFIG_PATH=<ABSOLUTE_PATH>` and restart the workers.
> 2. Edit `worker-config/prompts/<JOB_TYPE_NAME>.md.j2` to reflect your actual prompt.
> 3. When you're ready to add more job types, run `/minicrew:add-job-type` in this directory."

Note: on Linux Mint XFCE deployments you can add an optional `platform:` block to `config.yaml` to tune terminal emulator, display mode (visible or tmux), and timeouts. See `docs/LINUX.md` and `docs/CONFIG-REFERENCE.md`.
