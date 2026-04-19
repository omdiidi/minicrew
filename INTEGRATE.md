# INTEGRATE.md

Wire a consumer project to a running minicrew worker. Self-contained; read this whole file, then propose a diff to the user.

## For LLMs integrating a consumer project

You are integrating an external project with a minicrew worker. Read this entire file before producing any diff. Then present your proposed changes to the user and wait for approval before executing.

Rules:
- Never modify the minicrew repo itself from a consumer integration. All changes live inside the consumer project.
- All job rows are inserted into the consumer's Supabase `jobs` table. The worker polls that table and writes results back to the same rows.
- You do not need worker code access, worker source, or worker repo clone to wire up a consumer. You only need: a Supabase project, the service role key, and the ability to add files to the consumer repo.
- Use schema validation at system boundaries. Do not invent column names; use the schema in `## The data contract` below verbatim.

Deliverables for a typical integration:
1. A `worker-config/` directory committed to the consumer repo with a `config.yaml` and a `prompts/` subdirectory.
2. The SQL in `## The data contract` applied to the consumer's Supabase project.
3. Enqueue code in the consumer backend using one of the patterns in `## Enqueue patterns`.
4. Run the three curl commands in `## Verification checklist` to prove the round-trip works end-to-end.

## What minicrew is

minicrew is a generic Claude Code worker template for Mac Mini fleets. It polls a Supabase-backed `jobs` table, claims pending rows atomically, launches a Terminal.app window running `claude --dangerously-skip-permissions` with a Jinja-rendered prompt, monitors the session with an idle watchdog, writes the result back to the row, and loops. Consumers configure job types (model, prompt template, timeout, result filename) via a YAML file; no Python required on the consumer side.

## The data contract

Apply this SQL to your Supabase project. This is the core of the minicrew `schema/template.sql`; copy it verbatim.

```sql
create extension if not exists pgcrypto;

create table jobs (
  id uuid primary key default gen_random_uuid(),
  job_type text not null,
  status text not null default 'pending'
    check (status in ('pending','running','completed','error','failed_permanent','cancelled')),
  priority int not null default 0,
  worker_id text,
  claimed_at timestamptz,
  started_at timestamptz,
  completed_at timestamptz,
  expires_at timestamptz,
  attempt_count int not null default 0,
  max_attempts int not null default 3,
  requires jsonb not null default '[]'::jsonb,
  payload jsonb not null default '{}'::jsonb,
  result jsonb,
  error_message text,
  enqueued_by text,
  worker_version text,
  created_at timestamptz not null default now()
);
create index jobs_claim_idx on jobs (priority desc, created_at) where status = 'pending';

create table workers (
  id text primary key,
  hostname text not null,
  instance int not null,
  role text not null check (role in ('primary','secondary')),
  status text not null check (status in ('idle','busy','offline')),
  last_heartbeat timestamptz not null default now(),
  version text,
  started_at timestamptz not null default now()
);
```

**Payload shape:** free-form JSON under `jobs.payload`. Whatever keys the consumer sets here are available to the prompt template as `{{ payload.<key> }}`. Define a stable shape per job type; document it in the consumer repo.

**Result shape:** free-form JSON written by the worker to `jobs.result` on completion. The worker reads the file declared by `job_types.<name>.result_filename` in `config.yaml` and stores its parsed content here.

**Status enum:** `pending` (awaiting claim), `running` (claimed and in progress), `completed` (success, `result` populated), `error` (failure, `error_message` populated, may retry), `failed_permanent` (exceeded `max_attempts`, no retry), `cancelled` (expired past `expires_at` or manually cancelled).

## What your project provides

A `worker-config/` directory in the consumer repo:

```
worker-config/
  config.yaml              # job types, tables, models, timeouts, logging
  prompts/
    <job_type>.md.j2       # one Jinja template per job type
  payload.schema.json      # OPTIONAL; if present, engine validates payload before launch
```

Starter `worker-config/config.yaml`:

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
  prefix: worker
  role: primary

reaper:
  stale_threshold_seconds: 120
  interval_seconds: 60
  max_attempts: 3

job_types:
  summarize:
    description: "Brief summary of a document"
    mode: single
    model: claude-sonnet-4-6
    thinking_budget: medium
    timeout_seconds: 3600
    prompt_template: summarize.md.j2
    result_filename: result.json
```

Starter `worker-config/prompts/summarize.md.j2`:

```
You will produce a brief summary.

Source text:
{{ payload.text | tojson }}

Write a 2-3 sentence summary as JSON:
{"summary": "<your summary here>"}

Save the JSON to a file named `result.json` in the current working directory.
```

## What the worker provides back

- Status transitions on the `jobs` row: `pending` → `running` (on claim) → `completed` or `error` or `failed_permanent`.
- `jobs.claimed_at`, `jobs.started_at`, `jobs.completed_at` timestamps.
- `jobs.worker_id` and `jobs.worker_version` stamped on claim for debuggability.
- `jobs.result` populated with the parsed contents of the job's `result_filename`.
- `jobs.error_message` populated on failure.
- `jobs.attempt_count` incremented per retry.
- Per-instance file logs on the Mac Mini under `logs/worker-<n>.log` (JSON events) and per-job stdout/stderr under `logs/jobs/<job-id>.log`.

## Minimal working example

`worker-config/config.yaml`:
```yaml
schema_version: 1
db:
  jobs_table: jobs
  workers_table: workers
  events_table: worker_events
  url: ${SUPABASE_URL}
  service_key: ${SUPABASE_SERVICE_ROLE_KEY}
  direct_url: ${SUPABASE_DB_URL}
worker: { prefix: worker, role: primary }
reaper: { stale_threshold_seconds: 120, interval_seconds: 60, max_attempts: 3 }
job_types:
  summarize:
    mode: single
    model: claude-sonnet-4-6
    thinking_budget: medium
    timeout_seconds: 600
    prompt_template: summarize.md.j2
    result_filename: result.json
```

`worker-config/prompts/summarize.md.j2`:
```
Summarize the following text in 2-3 sentences and save as JSON to result.json:
{{ payload.text | tojson }}
Format: {"summary": "<text>"}
```

Enqueue one row:
```bash
curl -X POST "$SUPABASE_URL/rest/v1/jobs" \
  -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Content-Type: application/json" \
  -H "Prefer: return=representation" \
  -d '{"job_type":"summarize","payload":{"text":"hello world"}}'
```

## Enqueue patterns

### 1. curl against PostgREST (works anywhere)

```bash
curl -X POST "$SUPABASE_URL/rest/v1/jobs" \
  -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Content-Type: application/json" \
  -H "Prefer: return=representation" \
  -d '{
    "job_type": "summarize",
    "priority": 0,
    "payload": {"text": "The full document text goes here."}
  }'
```

Both `apikey` and `Authorization: Bearer` headers are required for service-role writes.

### 2. FastAPI endpoint with httpx

```python
import os
import httpx
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SRK = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

class SummarizeRequest(BaseModel):
    text: str

@app.post("/summarize")
async def enqueue_summarize(req: SummarizeRequest):
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{SUPABASE_URL}/rest/v1/jobs",
            headers={
                "apikey": SRK,
                "Authorization": f"Bearer {SRK}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            json={
                "job_type": "summarize",
                "payload": {"text": req.text},
            },
        )
        resp.raise_for_status()
        return {"job_id": resp.json()[0]["id"]}
```

### 3. Next.js server action

```typescript
// app/actions/enqueue.ts
"use server";

export async function enqueueSummarize(text: string) {
  const url = process.env.SUPABASE_URL!;
  const srk = process.env.SUPABASE_SERVICE_ROLE_KEY!;
  const res = await fetch(`${url}/rest/v1/jobs`, {
    method: "POST",
    headers: {
      apikey: srk,
      Authorization: `Bearer ${srk}`,
      "Content-Type": "application/json",
      Prefer: "return=representation",
    },
    body: JSON.stringify({
      job_type: "summarize",
      payload: { text },
    }),
  });
  if (!res.ok) throw new Error(`enqueue failed: ${res.status}`);
  const [row] = await res.json();
  return row.id as string;
}
```

### 4. Supabase Edge Function with supabase-js

```typescript
import { createClient } from "npm:@supabase/supabase-js@2";

Deno.serve(async (req) => {
  const { text } = await req.json();
  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );
  const { data, error } = await supabase
    .from("jobs")
    .insert({ job_type: "summarize", payload: { text } })
    .select("id")
    .single();
  if (error) return new Response(error.message, { status: 500 });
  return Response.json({ job_id: data.id });
});
```

## Verification checklist

Replace `$SUPABASE_URL` and `$SRK` with the actual project URL and service role key. Capture the returned job id from step 1 into `$JOB_ID` for steps 2 and 3.

### 1. Insert a test job

```bash
curl -X POST "$SUPABASE_URL/rest/v1/jobs" \
  -H "apikey: $SRK" \
  -H "Authorization: Bearer $SRK" \
  -H "Content-Type: application/json" \
  -H "Prefer: return=representation" \
  -d '{"job_type":"summarize","payload":{"text":"hello"}}'
```

Expected: HTTP 201, JSON array with one row, `status` is `pending`, `id` is a UUID. Record the `id`.

### 2. Poll for running status

```bash
curl "$SUPABASE_URL/rest/v1/jobs?id=eq.$JOB_ID&select=status,worker_id,claimed_at" \
  -H "apikey: $SRK" \
  -H "Authorization: Bearer $SRK"
```

Expected within 10-30 seconds: one row with `status: "running"`, a non-null `worker_id`, and a non-null `claimed_at`. If it stays `pending` for more than two minutes, no worker is polling this project (check `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` on the worker machine; check `launchctl list | grep com.minicrew.worker`).

### 3. Poll for completion and confirm result

```bash
curl "$SUPABASE_URL/rest/v1/jobs?id=eq.$JOB_ID&select=status,result,error_message" \
  -H "apikey: $SRK" \
  -H "Authorization: Bearer $SRK"
```

Expected within the configured `timeout_seconds`: one row with `status: "completed"` and a non-null `result` JSON object. If `status: "error"`, read `error_message`; if `status: "failed_permanent"`, the job exceeded `max_attempts`.

## When to need more

- Multi-step fan-out/fan-in (N parallel sessions + one merge) — see [docs/ORCHESTRATION.md](./docs/ORCHESTRATION.md).
- Priority tuning, reaper behavior, multi-machine coordination — see [docs/QUEUEING.md](./docs/QUEUEING.md).
- Picking a model and thinking budget per job type — see [docs/MODEL-TUNING.md](./docs/MODEL-TUNING.md).
