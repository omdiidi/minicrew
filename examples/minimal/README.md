# Minimal minicrew example

The smallest useful minicrew consumer: one `job_type` called `summarize` that takes an input string and produces a short summary.

## What it does

- Defines a single job type `summarize` running in `mode: single`.
- Uses `claude-sonnet-4-6` with a medium thinking budget.
- Renders `prompts/summarize.md.j2` with the job's payload.
- Claude writes `result.json` in the job's working directory; the engine reads that file and stores it in the `result` column of the `jobs` row.

## Wire it up

Point the worker at this directory via the `MINICREW_CONFIG_PATH` environment variable. Use an absolute path:

```
export MINICREW_CONFIG_PATH=/absolute/path/to/minicrew/examples/minimal
```

The worker loads `config.yaml` from that directory and resolves `prompt_template: summarize.md.j2` against `${MINICREW_CONFIG_PATH}/prompts/`.

Required environment variables (referenced by `config.yaml`):

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `SUPABASE_DB_URL`

## Enqueue one job

Use PostgREST directly against the `jobs` table:

```
curl -X POST "$SUPABASE_URL/rest/v1/jobs" \
  -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Content-Type: application/json" \
  -H "Prefer: return=representation" \
  -d '{"job_type":"summarize","payload":{"text":"Lorem ipsum dolor sit amet..."}}'
```

The response contains the new row, including its `id`.

## Read the result

Poll the row until `status=completed`:

```
curl "$SUPABASE_URL/rest/v1/jobs?select=status,result&id=eq.<id>" \
  -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY"
```

Expected `result` shape once complete:

```json
{
  "summary": "A short 3-5 sentence summary of payload.text.",
  "word_count_input": 123,
  "model_notes": ""
}
```

Terminal states: `completed`, `failed`, `cancelled`. If `status=failed`, inspect the `error` column and the worker logs.

## Template variables

The `summarize.md.j2` template is rendered with `StrictUndefined`, so any typo becomes a runtime error. Variables available:

- `payload` — the full JSON payload from the `jobs` row.

That's it. For fan-out jobs with groups, see `examples/fan-out/`.
