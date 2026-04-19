# Logging

## For LLMs

Describes logging sinks, event catalog, redaction, and job-output capture for v1. Invariants:
v1 has exactly one supported sink type (`file`); `postgres` and `http` sinks are reserved but
rejected by the loader; `SUPABASE_SERVICE_ROLE_KEY` and `SUPABASE_DB_URL` must always appear in
`logging.redact_env`; events are JSON-lines with `ts`, `event_type`, and `worker_id` as
universal fields. Do not add new event types without updating the catalog below. The v2
roadmap section documents what is intentionally deferred.

## v1 surface

v1 supports a single logging sink type: `file`. Log lines are JSON, one event per line, rotated
daily, retained for thirty days by default. Both the retention count and the rotation cadence
are configurable in `config.yaml`. Two additional sink types (`postgres` and `http`) are
reserved in the JSON Schema so consumers can future-proof their config, but the loader refuses
to initialize them with the message `sink type 'postgres' is reserved for v2` or `sink type
'http' is reserved for v2`. Document this up front — configuring a `postgres` sink today will
fail at startup, not silently do nothing.

## Event catalog

Every event emitted by the engine has `ts` (ISO-8601 UTC), `event_type` (snake_case), and
`worker_id`. Per-event fields are listed below.

- `worker_started` — `version`, `role` (`primary` or `secondary`).
- `worker_stopped` — no extra fields.
- `config_loaded` — `path` (the `MINICREW_CONFIG_PATH` absolute path).
- `job_claimed` — `job_id`, `job_type`.
- `job_completed` — `job_id`, `duration_seconds`.
- `job_failed` — `job_id`, `error` (string; truncated at 2KB).
- `session_launched` — `job_id`, `window_id` (the macOS window identifier returned by
  `osascript`).
- `watchdog_killed` — `job_id`, `reason` (one of `idle`, `result_stale`).
- `startup_requeued` — `job_id`.
- `reaper_ran` — `count_requeued` (integer).
- `reaper_requeued` — `worker_id` (the stale worker being reaped), `count`.
- `heartbeat_error` — `error`.
- `poll_loop_error` — `error`.

## Log format

Every line is a JSON object. Field order is not significant but is typically: `ts`,
`event_type`, `worker_id`, then event-specific fields. Example:

```json
{"ts":"2026-04-18T20:31:04.128Z","event_type":"job_claimed","worker_id":"worker-mini-1","job_id":"3f7e...","job_type":"summarize"}
```

One JSON object per line, no embedded newlines. Consumers reading the file should line-split
and `json.loads` each line independently.

## Redaction

The `logging.redact_env` list in `config.yaml` names environment variables whose *values* must
never appear in any log line. Before a line is written, the formatter scans the rendered
string and replaces any occurrence of each listed variable's value with `***`. Two entries are
always present regardless of config: `SUPABASE_SERVICE_ROLE_KEY` and `SUPABASE_DB_URL`.
Consumers can add more (for example, an OpenAI API key used in a custom skill).

## Job output capture

Each Terminal session's stdout and stderr are tee'd to `logs/jobs/<job-id>.log`. This is
independent of the worker event stream; it captures the actual Claude Code session output for
that job. Retention is configured per `logging.job_output.retention_days` (default 7). Capture
can be disabled by setting `logging.job_output.capture: false` — useful for privacy-sensitive
deployments where session transcripts should not be written to disk.

## Where logs go

All log paths are relative to the repo root on the Mac Mini:

- `logs/worker-<instance>.log` — the worker event stream (one per instance).
- `logs/worker-<instance>.err` — stderr captured by launchd (startup errors land here).
- `logs/jobs/<job-id>.log` — per-job session transcript when `job_output.capture` is true.

The `logs/` directory is created by `SETUP.md` step 6 and is in `.gitignore`.

## v2 roadmap

The following are deferred to v2 and intentionally not implemented in v1. They are mentioned
here so that a future contributor knows where to hook in and a current reader knows not to try.

- **Postgres sink.** Writes event rows to the `worker_events` table that already exists in
  `schema/template.sql`. Shape is `{ts, worker_id, event_type, payload jsonb}`.
- **HTTP sink.** Forwards event lines to an HTTP endpoint for ingestion by Axiom, Datadog,
  Betterstack, or any other log aggregator that accepts JSON-over-HTTPS.
- **Supabase Storage upload for job output.** After a job completes, the per-job log file
  uploads to a Storage bucket and the resulting URL is written to a `jobs.log_url` column.
  This is distinct from the v1 local-file capture; both can coexist.
