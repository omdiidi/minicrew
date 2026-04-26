---
name: minicrew:dispatch
description: |
  Dispatch a task to a remote minicrew worker (Mac Mini or Linux fleet)
  and wait for the result. Keywords: dispatch, minicrew, remote, Mac Mini,
  worker, background, offload, delegate. Use this when:
  - the task is self-contained and runs against a known repo+sha
  - the task takes >2 minutes locally OR you want to keep working
    while it runs
  - the task is naturally parallelizable (use /minicrew:fanout instead
    if you want N copies)
  - the user explicitly asks to "use minicrew", "dispatch this",
    "run this on the mac mini", or "send this to a worker"
  Do NOT use this for local edits, fast (<30s) tasks, or tasks that
  need my in-conversation context (uncommitted files, recent tool
  output, conversation history).
---

You are dispatching a task to a minicrew worker. The worker runs a
visible Claude Code TUI on the remote machine, executes the user's
task, writes `result.json`, and returns the result. This skill blocks
until the worker reports a terminal status.

## Recursion guard

Before dispatching, check if you are running INSIDE a worker session.
Run `bash -c 'echo "${MINICREW_INSIDE_WORKER:-0}"'`. If the output is
`1`, REFUSE — dispatching from inside a worker creates a loop.
Tell the user "I'm running inside a minicrew worker session; doing
this task locally instead of dispatching" and execute the task
directly (or refuse if you cannot).

## Secret scrubber

Before constructing the dispatch, grep the prompt for these patterns:

- `sk-` (Anthropic, OpenAI, Stripe live)
- `sk_test_`, `sk_live_` (Stripe)
- `ghp_`, `gho_`, `ghu_`, `ghs_` (GitHub tokens)
- `github_pat_` (GitHub fine-grained PATs)
- `glpat-` (GitLab PATs)
- `dop_` (DigitalOcean tokens)
- `npm_` (npm tokens)
- `AKIA` (AWS access keys)
- `ASIA` (AWS session tokens)
- `AIza` (Google Cloud API keys)
- `xoxb-`, `xoxa-`, `xoxp-` (Slack tokens)
- `eyJhbG` (JWT prefix — Supabase service_role, etc.)
- `-----BEGIN` (PEM private keys)
- `postgres://`, `postgresql://`, `mysql://`, `mongodb://`, `mongodb+srv://` (DB connection strings)
- lines matching `[A-Z_]+_KEY=` or `[A-Z_]+_SECRET=` or `[A-Z_]+_TOKEN=` (generic env-var assignments)

If any match, ABORT and warn the user: "the prompt appears to contain
a secret; the dispatch payload is logged to Supabase Storage and
visible to anyone with `caller_log_url`. Use environment-variable
references the worker can dereference, not literal values."

## Inputs (from $ARGUMENTS or inferred)

- **prompt**: the task text. Required. If the user said
  "use minicrew with this: <task>", the prompt is `<task>`.
  If they invoked the skill with no args, ask for the task in one
  short follow-up.
- **repo**: GitHub URL (`https://github.com/<owner>/<repo>`).
  Default: derive from `git config --get remote.origin.url` in
  the current cwd. If not a git repo, ask the user.
- **sha**: 40-char commit SHA. Default: `git rev-parse HEAD`. If
  HEAD has uncommitted changes, warn the user (worker will run
  against the clean SHA, not the dirty working copy).
- **model**: optional override. Defaults to whatever the worker's
  config.yaml ad_hoc job_type specifies.
- **effort**: optional override (low/medium/high).

## Steps

1. Resolve inputs. Run `git config --get remote.origin.url` and
   `git rev-parse HEAD` in cwd. Convert ssh URLs (`git@github.com:owner/repo`)
   to https form. Note: the `python -m worker --dispatch` CLI now
   does this inference itself when `--repo`/`--sha` are omitted, so
   you can either pre-resolve them and pass explicitly OR omit and
   let the CLI infer.
2. Validate: prompt non-empty, repo starts with
   `https://github.com/`, sha is 40 hex chars.
3. Build the bash command. Use `--prompt-base64` to bypass shell-quoting risk
   for any `$`, backticks, `\`, or heredoc-delimiter strings in the task text.
   The CLI also accepts `--prompt <text>` for short, ASCII-only prompts.
   ```bash
   B64=$(printf '%s' "<TASK>" | base64 | tr -d '\n')
   python -m worker --dispatch ad_hoc \
     --repo "$REPO" --sha "$SHA" \
     --wait --wait-seconds 1800 \
     --prompt-base64 "$B64"
   ```
   Run via Bash tool. Capture stdout — it's a stream of JSON lines:
   - First line: `{"job_id": "...", "job_type": "...", "status": "pending"}`
   - Then poll snapshots: `{"status": "...", "worker_id": "...", "error_message": null}`
   - On terminal: `{"final": <full row including id, status, result, error_message, ...>}` (pretty-printed, multi-line)

   Parse the LAST `{"final": ...}` block and read `final.result` for the
   worker's JSON output. The CLI exits 0 on `completed` and 1 on any
   other terminal status.

   The Bash tool's default timeout is 2 minutes; pass `timeout=600000`
   (10-min ceiling) when you expect long runs, or omit `--wait` and
   poll separately.
4. If status is `completed`, present the `result` (the JSON object the
   worker wrote to result.json) to the user.
5. If status is anything else, surface the `error_message` and tell
   the user the job_id so they can inspect via Supabase.

## Caveats

- The worker does NOT clone git submodules. If the repo uses
  submodules, the task must not depend on submodule contents.
- If the task involves files in the operator's UNCOMMITTED working
  directory, the worker won't see them (it clones the repo at the
  SHA you pass). Either commit + push first, or acknowledge in the
  prompt that the worker should NOT modify those files.
- Never construct `--repo` against a non-github.com URL — the worker
  rejects it.

## Constraints

- The worker's environment must have `SUPABASE_URL` and either
  `MINICREW_DISPATCH_JWT` or `SUPABASE_SERVICE_ROLE_KEY` exported.
  If Bash returns "error: set SUPABASE_URL...", tell the user to
  export those (point at SETUP.md).
