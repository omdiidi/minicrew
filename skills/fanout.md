---
name: minicrew:fanout
description: |
  Run the same task across N parallel minicrew workers and merge
  the results. Keywords: in parallel, across N, fan out, N sessions,
  diverse perspectives, multiple workers, parallel dispatch.
  Use when:
  - the user says "across N sessions/workers/instances" or "in parallel"
  - you want diverse perspectives (run with N different effort levels
    or models) on the same task
  - you want to A/B test a prompt
  Default N is 3. Each worker is independent; results return as a
  list. The skill optionally synthesizes a final answer from the N
  results if --merge is set.
---

You are dispatching the SAME task to N minicrew workers in parallel
and collecting all N results. This is the fan-out variant of
`/minicrew:dispatch`.

## Recursion guard

Before dispatching, check if you are running INSIDE a worker session.
Run `bash -c 'echo "${MINICREW_INSIDE_WORKER:-0}"'`. If the output is
`1`, REFUSE — dispatching from inside a worker creates a loop.

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

If any match, ABORT and warn the user.

## Inputs

Same as `/minicrew:dispatch`, plus:

- **n**: integer, default 3. The number of parallel workers.
- **merge**: bool, default false. If true, after all N results return,
  synthesize a single final answer locally summarizing the N.

## Steps

1. Resolve repo + sha as in dispatch.md (run `git config --get
   remote.origin.url` and `git rev-parse HEAD` in cwd, or let the
   CLI infer them).
2. For i in 1..N, dispatch in parallel. Spawn N parallel Bash tool
   calls in a single message. Use `--prompt-base64` to bypass shell-quoting
   risk for any `$`, backticks, `\`, or heredoc-delimiter strings in the
   task text. The CLI also accepts `--prompt <text>` for short, ASCII-only
   prompts. Each call runs:
   ```bash
   B64=$(printf '%s' "<TASK>" | base64 | tr -d '\n')
   python -m worker --dispatch ad_hoc \
     --repo "$REPO" --sha "$SHA" \
     --wait --wait-seconds 1800 \
     --prompt-base64 "$B64"
   ```
   Use `timeout=600000` on each Bash call. Each call's stdout is a
   stream of JSON lines:
   - First line: `{"job_id": "...", "job_type": "...", "status": "pending"}`
   - Then poll snapshots: `{"status": "...", "worker_id": "...", "error_message": null}`
   - On terminal: `{"final": <full row including id, status, result, error_message, ...>}` (pretty-printed, multi-line)

   Parse the LAST `{"final": ...}` block per call and read `final.result`
   for the worker's JSON output. The CLI exits 0 on `completed` and 1
   on any other terminal status.
3. Collect N results as a list. Tag each with its index (1..N) so the
   user can tell them apart.
4. If `--merge` is set, synthesize a single answer by reading the N
   results and producing a unified summary. Present BOTH the synthesis
   and a link/reference to the N raw results.
5. Otherwise, present the list of N results directly.

## Constraints

- Each worker is a separate dispatch — they do NOT share state. If
  the task needs shared state, fan-out is the wrong tool.
- N is bounded by your worker fleet capacity. If you only have one
  worker registered, N=3 will queue serially.
- If any individual dispatch fails, surface the error for that index
  but continue collecting the others. Don't abort the whole fanout
  on a single failure.
