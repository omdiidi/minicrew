# Fan-out minicrew example

Demonstrates `mode: fan_out`: one job type, three parallel group terminals, then a merge terminal that combines their outputs.

## What it does

- Defines one job type `analyze_document_set`.
- Splits `payload.documents` (an array) into three groups: `first_third`, `middle_third`, `last_third`.
- Each group terminal renders `prompts/group.md.j2`, summarizes its assigned documents, extracts entities, and writes `group_result.json`.
- When all three groups finish, the merge terminal renders `prompts/merge.md.j2`, reads each `group_result.json`, deduplicates entities, and writes the final `result.json`.

## Wire it up

```
export MINICREW_CONFIG_PATH=/absolute/path/to/minicrew/examples/fan-out
```

Required env: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_DB_URL`.

## Enqueue one job

```
curl -X POST "$SUPABASE_URL/rest/v1/jobs" \
  -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Content-Type: application/json" \
  -H "Prefer: return=representation" \
  -d '{"job_type":"analyze_document_set","payload":{"documents":[{"title":"Doc A","body":"..."},{"title":"Doc B","body":"..."}]}}'
```

## Expected result shape

Once the row reaches `status=completed`, the `result` column holds:

```json
{
  "per_document": [
    { "index": 0, "summary_bullets": ["..."], "entities": ["..."] },
    { "index": 1, "summary_bullets": ["..."], "entities": ["..."] }
  ],
  "entities_deduplicated": ["..."],
  "overall_summary": "A 3-5 sentence synthesis across all documents."
}
```

## Template variables

Rendered with `StrictUndefined` — typos become runtime errors.

**Group template (`group.md.j2`)** receives:

- `payload` — the full job payload.
- `group` — an object with at least `name` (string) and `document_indices` (array of ints).

**Merge template (`merge.md.j2`)** receives:

- `payload` — the full job payload.
- `group_result_paths` — a list of absolute paths to each group's `group_result.json`, in group-definition order.

## Important: `document_indices` is illustrative in v1

The v1 engine does NOT automatically compute `document_indices` by evenly slicing `payload.documents`. The group template shows `{{ group.document_indices | tojson }}` to illustrate where such a value would appear if the engine supplied one.

For v1, choose one of:

1. **Encode the split in the payload.** Enqueue the job with payload shaped like `{"documents":[...], "splits":{"first_third":[0,1], "middle_third":[2,3], "last_third":[4]}}` and adjust the group template to look up `payload.splits[group.name]` instead of `group.document_indices`.
2. **Make groups self-selecting.** Adjust the group template to instruct each group to pick its assigned slice by name (for example, "process documents whose index modulo 3 equals 0 for `first_third`").

The example is left with the illustrative form so the shape is obvious; you will need to customize either the payload or the group template before running it end-to-end.

## When to use fan-out vs single

- Use `single` when the work is one logical chunk that fits in one Claude session.
- Use `fan_out` when the work splits into independent pieces and a final combine step adds value (deduplication, ranking, cross-group synthesis).
