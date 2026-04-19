# Config Reference

## For LLMs

**What this covers:** Every field in `worker-config/config.yaml`. Types, whether required, defaults, and examples. Mirrors `schema/config.schema.json` field-for-field.

**Invariants:**
- `schema_version` must be `1`. The loader rejects mismatches.
- Every object level is `additionalProperties: false`. Unknown keys are a hard fail.
- `logging.sinks[].type` currently supports only `file`. `postgres` and `http` are v2-reserved and rejected at load time.
- `mode: fan_out` requires both `groups` (>= 1 item) and `merge`.
- `prompt_template` is a filename, not a path. The loader resolves it under `prompts/`.

**Do not change** field names or enum values without first updating `schema/config.schema.json`; the loader validates against it on every startup.

## `schema_version`

- **Type:** integer (const: 1)
- **Required:** yes
- **Description:** Config schema version. Bump when the schema changes in an incompatible way.

```yaml
schema_version: 1
```

## `db`

Database connection settings. The worker talks to Supabase via PostgREST for most operations and via a direct Postgres connection for the reaper's advisory lock.

| Field           | Type   | Required | Default | Description                                                                 |
|-----------------|--------|----------|---------|-----------------------------------------------------------------------------|
| `jobs_table`    | string | yes      | —       | Name of the jobs table. Must match `schema/template.sql`.                   |
| `workers_table` | string | yes      | —       | Name of the workers table.                                                  |
| `events_table`  | string | yes      | —       | Name of the worker_events table. v2-reserved; unused in v1.                 |
| `url`           | string | yes      | —       | Supabase REST URL. Supports `${ENV_VAR}` interpolation.                     |
| `service_key`   | string | yes      | —       | Supabase service role key. Always redacted from logs.                        |
| `direct_url`    | string | yes      | —       | Direct Postgres URL on port 5432 (NOT pooler 6543). See SUPABASE-SCHEMA.md. |

```yaml
db:
  jobs_table: jobs
  workers_table: workers
  events_table: worker_events
  url: ${SUPABASE_URL}
  service_key: ${SUPABASE_SERVICE_ROLE_KEY}
  direct_url: ${SUPABASE_DB_URL}
```

## `worker`

Per-instance runtime settings.

| Field                   | Type             | Required | Default | Description                                                              |
|-------------------------|------------------|----------|---------|--------------------------------------------------------------------------|
| `prefix`                | string           | yes      | —       | Prepended to the worker id. Full id is `<prefix>-<hostname>-<instance>`. |
| `role`                  | enum             | yes      | —       | `primary` or `secondary`. Drives default poll interval.                  |
| `poll_interval_seconds` | integer \| null  | no       | null    | Explicit poll interval. If null, derived from role (primary=5, secondary=15). |

```yaml
worker:
  prefix: worker
  role: primary
  poll_interval_seconds: null
```

## `reaper`

Opportunistic reaper settings. Exactly one worker reaps per cycle (chosen by Postgres advisory lock).

| Field                      | Type    | Required | Default | Description                                                                                  |
|----------------------------|---------|----------|---------|----------------------------------------------------------------------------------------------|
| `stale_threshold_seconds`  | integer | yes      | —       | Min 30. Workers whose `last_heartbeat` is older than this are treated as dead.               |
| `interval_seconds`         | integer | yes      | —       | Min 10. How often the reaper wakes up.                                                       |
| `max_attempts`             | integer | yes      | —       | Min 1. Default max attempts. A per-row `jobs.max_attempts` overrides this.                   |

```yaml
reaper:
  stale_threshold_seconds: 120
  interval_seconds: 60
  max_attempts: 3
```

## `job_types`

Map of `job_type` name to per-type configuration. Keys are `lowercase_snake_case`. At least one entry is required.

### `job_types.<name>`

| Field                          | Type              | Required     | Default | Description                                                                                  |
|--------------------------------|-------------------|--------------|---------|----------------------------------------------------------------------------------------------|
| `description`                  | string            | no           | —       | Human-readable description.                                                                  |
| `mode`                         | enum              | yes          | —       | `single` or `fan_out`.                                                                       |
| `skill`                        | string \| null    | no           | null    | Optional Claude Code skill invocation prefixed to the rendered prompt (e.g. `my_plugin:analyze`). |
| `model`                        | enum              | yes          | —       | `claude-opus-4-7`, `claude-sonnet-4-6`, or `claude-haiku-4-5`.                               |
| `thinking_budget`              | enum              | yes          | —       | `none`, `medium`, or `high`.                                                                 |
| `timeout_seconds`              | integer           | yes          | —       | Hard cap on wall-clock time. Terminal is torn down when exceeded.                            |
| `prompt_template`              | string            | yes          | —       | Filename under `prompts/`. Not a path.                                                       |
| `result_filename`              | string            | yes          | —       | Name of the result file the session is expected to produce.                                  |
| `idle_timeout_seconds`         | integer           | no           | 1500    | No recursive file activity AND no result file for this long -> kill.                         |
| `result_idle_timeout_seconds`  | integer           | no           | 900     | Result file present but unmodified for this long -> kill.                                    |
| `groups`                       | array             | fan_out only | —       | Parallel group definitions. Min 1.                                                           |
| `merge`                        | object            | fan_out only | —       | Merge step definition.                                                                       |

#### `groups[]` (fan_out only)

| Field             | Type   | Required | Description                                                                     |
|-------------------|--------|----------|---------------------------------------------------------------------------------|
| `name`            | string | yes      | Group id. Becomes the subdirectory `group_<name>` under the session cwd.        |
| `prompt_template` | string | yes      | Filename under `prompts/`.                                                      |
| `result_filename` | string | yes      | Name of the file this group's Claude session is expected to write.              |

#### `merge` (fan_out only)

| Field             | Type   | Required | Description                                                                     |
|-------------------|--------|----------|---------------------------------------------------------------------------------|
| `prompt_template` | string | yes      | Filename under `prompts/` for the merge session.                                |
| `result_filename` | string | yes      | Final result filename written into `jobs.result`.                               |

### Example: single mode

```yaml
job_types:
  summarize:
    description: "Produce a short summary of payload.text."
    mode: single
    skill: null
    model: claude-sonnet-4-6
    thinking_budget: medium
    timeout_seconds: 3600
    prompt_template: summarize.md.j2
    result_filename: result.json
    idle_timeout_seconds: 1500
    result_idle_timeout_seconds: 900
```

### Example: fan_out mode

```yaml
job_types:
  analyze_document:
    description: "Split a document into sections, analyze each, merge."
    mode: fan_out
    model: claude-sonnet-4-6
    thinking_budget: medium
    timeout_seconds: 7200
    prompt_template: analyze_entry.md.j2
    result_filename: final.json
    groups:
      - name: intro
        prompt_template: analyze_group.md.j2
        result_filename: group.json
      - name: body
        prompt_template: analyze_group.md.j2
        result_filename: group.json
      - name: conclusion
        prompt_template: analyze_group.md.j2
        result_filename: group.json
    merge:
      prompt_template: analyze_merge.md.j2
      result_filename: final.json
```

## `logging`

Structured logging configuration. v1 supports only the file sink.

| Field         | Type    | Required | Default | Description                                                                                 |
|---------------|---------|----------|---------|---------------------------------------------------------------------------------------------|
| `level`       | enum    | yes      | —       | `debug`, `info`, `warn`, or `error`.                                                        |
| `format`      | enum    | yes      | —       | Only `json` is supported.                                                                   |
| `redact_env`  | array   | yes      | —       | Names of env vars whose values are masked in logs. `SUPABASE_SERVICE_ROLE_KEY` and `SUPABASE_DB_URL` are always redacted regardless. |
| `sinks`       | array   | yes      | —       | Min 1. Each sink has a `type` discriminator. Only `file` is accepted in v1.                 |
| `job_output`  | object  | no       | —       | Per-job stdout/stderr capture settings.                                                     |

### `sinks[]`

Only `type: file` is supported in v1. Declaring `type: postgres` or `type: http` triggers a loader error pointing at the v2 roadmap.

| Field    | Type    | Required | Description                                                               |
|----------|---------|----------|---------------------------------------------------------------------------|
| `type`   | enum    | yes      | Must be `file`. Other values reserved for v2.                             |
| `path`   | string  | yes      | Log file path. Supports the `{instance}` placeholder.                     |
| `rotate` | enum    | yes      | `daily`, `hourly`, or `none`.                                             |
| `keep`   | integer | yes      | How many rotated files to retain.                                         |

### `job_output`

| Field                | Type    | Required | Description                                                                 |
|----------------------|---------|----------|-----------------------------------------------------------------------------|
| `capture`            | boolean | yes      | Tee Terminal stdout/stderr to `logs/jobs/<job-id>.log`.                     |
| `retention_days`     | integer | yes      | How many days of per-job logs to retain on disk.                            |
| `upload_to_storage`  | boolean | no       | **v2-reserved.** Permitted in config for forward-compatibility but is a no-op in v1. Setting it true does not upload. |

### Example

```yaml
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

## Common mistakes

- **Pooler URL in `db.direct_url`.** Supabase's pooler hostname contains `pooler` and uses port 6543. The worker rejects this on startup. Re-copy the URI from the "Direct connection" tab in Project Settings -> Database.
- **`mode: fan_out` without `groups` or `merge`.** The schema's `oneOf` branches enforce both. Leaving either out produces a loader error pointing at the missing field.
- **Referencing a `prompt_template` that does not exist in `prompts/`.** The loader re-checks every referenced template after schema validation and hard-fails with the missing filename.
- **Setting `sinks[].type: postgres` or `http`.** v2-reserved. Use `file` for v1. The loader will tell you which line is wrong.
- **Top-level typo (e.g. `job_type:` instead of `job_types:`).** `additionalProperties: false` rejects unknown keys; the loader error includes the offending key.
- **Forgetting `schema_version: 1`.** Required. The loader refuses to proceed without it.
