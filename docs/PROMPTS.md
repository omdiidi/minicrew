# Prompts

## For LLMs

**What this covers:** The full contract between minicrew and the Claude Code session it
launches inside a Terminal window: how a prompt template gets rendered, which variables
are in scope, how the result file gets read back, how progress reporting works, how
fan-out templates receive their split input, and where to look on disk when a job
misbehaves.

**Invariants (do not change without coordinated edits to `worker/config/render.py`,
`worker/orchestration/result_io.py`, and `worker/integrations/log_streamer.py`):**

- The Jinja environment uses `StrictUndefined`; any typo in a template variable name is
  a hard render failure, never a silent empty string.
- The `_finalize` callback converts non-string Python values into JSON strings before
  insertion. Strings pass through unchanged.
- Result files are read with `O_NOFOLLOW` and a path-traversal containment check
  (`Path.resolve()` must remain inside the session cwd).
- `_progress.jsonl` is read only when `cfg.dispatch is not None`. Batch installs
  without a `dispatch:` block see no progress threads at all.
- Lines in `_progress.jsonl` larger than 64 KB are silently dropped. A torn last line
  is preserved across tailer ticks (the tailer buffers up to the last newline).
- Fan-out templates always receive `group.document_indices`; new templates can also key
  on `group.items`. Both are kept for back-compat.
- `result_schema` validation requires JSON output. The non-JSON fallback shape
  `{"raw": "<text>"}` is by design returned unvalidated; consumers who set a
  `result_schema` MUST instruct their session to write JSON.

## The contract in one paragraph

minicrew renders a Jinja template into a string, writes that string to `_prompt.txt`
inside a per-job temp directory, writes a tiny `_run.sh` runner that invokes
`claude … "$(cat _prompt.txt)"`, opens a visible Terminal window with that runner,
and waits. The session is expected to write a single result file (the
`result_filename` configured for the job type) into that same directory before
exiting. The worker reads the result file, validates it (optionally) against
`result_schema`, and writes it to `jobs.result`. That is the entire surface area.

## Template variables in scope

Every prompt template renders with three top-level variables:

| Name      | Type          | Source                                                                     |
|-----------|---------------|----------------------------------------------------------------------------|
| `job`     | dict          | The full `jobs` row (id, job_type, payload, attempt_count, etc.).          |
| `payload` | dict          | Shorthand for `job.payload`. Most templates only need this.                |
| `config`  | object        | A redacted view of `cfg` (no secrets). Read-only.                          |

Fan-out group templates additionally receive a `group` object whose contents are
described in the fan-out section below.

### Worked example

Given a `payload` of `{"text": "the cat sat", "max_words": 5}` and a template:

```jinja
Summarize the following in at most {{ payload.max_words }} words:

{{ payload.text }}
```

The rendered prompt is:

```
Summarize the following in at most 5 words:

the cat sat
```

If `payload.max_words` were missing, render fails with `UndefinedError: 'max_words' is
undefined` — the worker logs the error, marks the job `error`, and does NOT launch a
Terminal. This is by design: a typo in a template should never silently ship a malformed
prompt to a paid model.

## The `_finalize` callback

`worker/config/render.py` configures a `finalize` callback on the Jinja environment.
The rule is:

- A `str` passes through unchanged.
- Anything else (`dict`, `list`, `int`, `bool`, `None`) is serialized with
  `json.dumps(value, ensure_ascii=False, sort_keys=True)`.

This means `{{ payload.options }}` where `options` is a dict produces compact JSON
(`{"flag": true, "n": 3}`) instead of Python's `repr` (`{'flag': True, 'n': 3}`).
Templates that want pretty-printed JSON should call `tojson(indent=2)` explicitly.

## `StrictUndefined`

The Jinja environment uses `jinja2.StrictUndefined`. Accessing a missing variable, or
attribute access on an undefined value, raises immediately. Templates cannot test for
existence with `{% if foo %}` against a missing key — use `{% if 'foo' in payload %}`
or wrap the lookup in `payload.get(...)` style via a custom Jinja filter (none ship
by default; consumers can add filters via a future extension hook).

## The `/skill` prefix

When a job type sets `skill: my-plugin:analyze` in `config.yaml`, the worker prepends
`/my-plugin:analyze\n\n` to the rendered prompt before writing `_prompt.txt`. The
session sees:

```
/my-plugin:analyze

<rendered prompt body>
```

Claude Code interprets the leading `/…` as a slash command and dispatches to the
installed skill. The engine itself never validates the skill name — if the skill is
not installed on the host, Claude Code fails inside the Terminal session and the job
ends with no result file.

**The skill prefix is NOT applied for `mode: ad_hoc` or `mode: handoff`.** Those modes
use built-in templates (`worker/builtin_prompts/ad_hoc.md.j2` and `handoff.md.j2`)
and ignore the `skill` field entirely; the schema forbids `skill` on those modes.

## Result file contract

The session is expected to write `result_filename` (configured per job type) into
its working directory. The worker:

1. Opens the file with `O_NOFOLLOW` so a symlink swap mid-flight is rejected.
2. Resolves the path and re-checks it is inside the session cwd. Path-traversal
   attempts (a session that writes `../../etc/passwd` and symlinks `result.json` to
   it) fail closed.
3. Parses as JSON. On failure, falls back to `{"raw": "<full file text>"}`.
4. If the job type has a `result_schema`, validates the parsed JSON against it.
5. Writes the (possibly validated) value to `jobs.result`.

### Result validation contract (`ResultRead`)

`worker/config/result_validation.py` defines:

```python
@dataclass
class ResultRead:
    ok: bool
    value: Any
    error: str | None
```

The orchestrator only writes `ResultRead.value` to `jobs.result` when `ok=True`.
When `ok=False`, the orchestrator writes `ResultRead.error` to `jobs.error_message`
and marks the job `error`.

`result_schema` is a JSON Schema object. When set:

- A successfully-parsed JSON value is validated against the schema; failures produce
  `ok=False` with a path-tagged error like
  `result validation failed at /summary: 'summary' is a required property`.
- The non-JSON `{"raw": "<text>"}` fallback always returns
  `ok=False, error="result is non-JSON; result_schema requires JSON output"`. If you
  want freeform text results, leave `result_schema` unset.

## Progress reporting

Long-running sessions can append JSON Lines to `_progress.jsonl` inside the session
cwd. The worker's `ProgressTailer` thread reads new bytes every 3 seconds and writes
the most recent complete line to `jobs.progress` (jsonb).

Important details:

- **Gated behind `cfg.dispatch is not None`.** Batch deployments without a `dispatch:`
  block in their config get zero new background threads. The tailer is only started
  when dispatch is configured.
- **64 KB cap per line.** Lines exceeding `MAX_PROGRESS_LINE_BYTES = 64 * 1024` are
  silently dropped. Use them for short status pings, not log dumps.
- **Torn-tail safety.** A line written but not flushed (or in the middle of a flush)
  is buffered until its trailing newline arrives in a later tick.
- **Last-write-wins.** The tailer publishes only the single most recent complete line
  per tick. Intermediate lines between ticks are not lost from the file (they remain
  appended) but only the latest reaches `jobs.progress`.
- **Ownership-aware PATCH.** `write_progress` filters on `id + worker_id +
  status='running'`. If the worker loses the claim or the job moves to a terminal
  state, the tailer detects zero rows updated and stops.
- `_progress.jsonl` is treated as a control file by `worker/terminal/watchdog.py`
  and does not count as "user activity" for idle-timeout purposes.

### Progress line shape

Anything JSON-serializable. A simple convention:

```json
{"phase": "analyzing", "step": 3, "of": 7, "note": "scoring rule 12"}
```

The caller can poll `jobs.progress` to surface live status to the user.

## Fan-out

A `mode: fan_out` job type splits its input across N parallel groups, then runs a
merge session over the group outputs.

### Per-group templates

Each group runs in `<tempdir>/group_<name>/`. The group's prompt template receives
the standard `job`, `payload`, `config` plus a `group` object:

| `group` attribute    | Description                                                                   |
|----------------------|-------------------------------------------------------------------------------|
| `name`               | The group id from `groups[].name`.                                            |
| `result_filename`    | The group's expected output filename.                                         |
| `document_indices`   | **Back-compat:** list of integer indices into `payload.documents`.            |
| `items`              | List of integer indices into `payload[partition.key]` (NEW key).              |
| `partition_items`    | List of the actual selected items (NOT indices). Convenience for templates.   |

The merge template receives all `group` objects via `groups` plus the per-group
result filenames; it is expected to read each group's `result_filename` from the
sibling directory.

### Partition strategies

The optional `partition` block on a fan_out job type controls how `payload[key]` is
split across the configured groups:

| Strategy | Behavior                                                                                                |
|----------|---------------------------------------------------------------------------------------------------------|
| `chunks` | Even-ish split. With 7 items and 3 groups: `[3, 2, 2]` (the `divmod` remainder distributes left-to-right). |
| `copies` | Every group sees every item. Useful for "rate this against rule X" patterns where the rule varies by group. |

When `partition` is omitted on a fan_out job, the loader emits a one-time
`FAN_OUT_PARTITION_DEPRECATED` event per worker boot per job type and behaves as if
`{key: "documents", strategy: "chunks"}` were set. Existing templates that key on
`group.document_indices[0]` keep working.

### Worked example: chunks

```yaml
job_types:
  analyze_document:
    mode: fan_out
    partition: { key: "sections", strategy: "chunks" }
    groups:
      - { name: a, prompt_template: group.md.j2, result_filename: out.json }
      - { name: b, prompt_template: group.md.j2, result_filename: out.json }
    merge: { prompt_template: merge.md.j2, result_filename: final.json }
```

With `payload = {"sections": ["intro", "body", "conclusion", "appendix"]}`:

- Group `a` template renders with `group.items = [0, 1]` and
  `group.partition_items = ["intro", "body"]`.
- Group `b` template renders with `group.items = [2, 3]` and
  `group.partition_items = ["conclusion", "appendix"]`.

### Worked example: copies

Same config but `strategy: copies`:

- Group `a` template renders with `group.items = [0, 1, 2, 3]` and
  `group.partition_items = ["intro", "body", "conclusion", "appendix"]`.
- Group `b` renders identically.

## Debugging

When a session fails, the per-job temp directory is preserved until the next cleanup
sweep. Useful files:

| File                           | Contents                                                                  |
|--------------------------------|---------------------------------------------------------------------------|
| `_prompt.txt`                  | Exact bytes the session received.                                         |
| `_run.sh`                      | Runner script the Terminal executed.                                      |
| `<result_filename>`            | What the session wrote (if anything).                                     |
| `_progress.jsonl`              | All progress lines, including dropped-by-cap lines (visible by `wc -l`).  |
| `_session.json`                | `SessionHandle` snapshot for shutdown sweep.                              |
| `logs/jobs/<job_id>.log`       | Full `tee`'d stdout/stderr from the Claude Code session.                  |

For fan-out jobs, each group has its own `_prompt.txt` / `_run.sh` /
`_progress.jsonl` under `group_<name>/`.

## Worked examples by mode

### `mode: single`

Standard single-session job. The whole template renders once; the session writes one
result file. See [ORCHESTRATION.md](./ORCHESTRATION.md#single-mode-default).

### `mode: fan_out`

Per-group templates plus a merge template. See
[ORCHESTRATION.md](./ORCHESTRATION.md#fan-out-mode) and the partition section above.

### `mode: ad_hoc`

The session is dispatched from a peer Claude Code session. The prompt template is a
built-in (`worker/builtin_prompts/ad_hoc.md.j2`); consumers cannot override it. See
[DISPATCH.md](./DISPATCH.md) for the caller-side contract.

### `mode: handoff`

The session resumes an existing transcript via `claude --resume <session-id>
--print`. The prompt template is `worker/builtin_prompts/handoff.md.j2`; the
housekeeping (result-file shape, push contract, progress reporting, `--print` exit
semantics) lives outside the user-instruction `if/else` so both the
default-preamble path and the user-supplied-instruction path get it. See
[HANDOFF.md](./HANDOFF.md) for the user-facing flow and
[DISPATCH.md](./DISPATCH.md#handoff) for the technical contract.

## Note on `result_schema` and non-JSON sessions

`result_schema` requires a JSON-shape result. If the session writes plain text, the
worker reads it as `{"raw": "<text>"}` and `ResultRead` returns `ok=False` with an
explanatory error. There is no way to assert "schema-validate JSON, or accept any
plain text" in v1 — the choice is binary. If you want a permissive default, leave
`result_schema` unset and validate downstream.
