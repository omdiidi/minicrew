# minicrew routing rules

This file is imported into a project's CLAUDE.md via
`@~/.claude/commands/minicrew/routing-rules.md` (or its global
equivalent). It teaches Claude Code WHEN to dispatch a task to a
minicrew worker vs. handle it locally.

## Dispatch to minicrew when:

- The user asks explicitly: "use minicrew", "dispatch this", "run on
  the mac mini", "send to a worker", "across N sessions", "in parallel".
- The task takes substantially longer than a few seconds AND can run
  independently of the current conversation (i.e. doesn't need
  uncommitted files, recent tool output, or in-flight chat context).
- The task is naturally parallelizable (analyze 3 PDFs, audit 5 trades,
  run the same prompt at 3 effort levels) — use `/minicrew:fanout`.
- You want to keep the local conversation responsive while a long
  task runs (e.g. a security audit, a full-repo refactor, a large
  test suite walk).

## Stay local when:

- The task is fast (under ~30 seconds locally).
- The task needs MY in-conversation context: recent file reads, prior
  tool output, decisions made earlier in this conversation, or
  uncommitted edits in the current working directory.
- The task is interactive — needs to ask the user follow-up questions
  mid-execution (the worker can use AskUserQuestion but the round-trip
  via DB is slow; only use minicrew for self-directed work).
- The task is purely about THIS conversation — summarizing what we
  just did, generating a commit message, etc.

## Tie-breakers

- If you're unsure, ASK the user once: "This looks like it could run
  on a minicrew worker (~5 min, fully self-contained) or I can do it
  locally (faster but I'll be tied up). Which?" Don't ask repeatedly.
- If the user's previous message established a preference ("just do
  it locally"), respect that for the rest of the conversation.

## Don't:

- Don't dispatch tasks that require pushing to the user's local git
  remote unless the prompt explicitly says `--allow-code-push` and
  the user has approved it.
- Don't dispatch tasks containing secrets in the prompt body
  (env vars, API keys). The prompt is logged to Supabase Storage as
  part of `caller_log_url`. Use environment-variable references
  the worker can dereference, not literal values.
- **Recursion guard:** if the env var `MINICREW_INSIDE_WORKER` is set
  to `1`, you ARE running inside a minicrew worker session. Never
  dispatch — that creates a loop where workers spawn workers
  indefinitely, exhausting the per-caller cap and burning Anthropic
  budget. Just do the task locally inside the current worker session.
- **Secret scrubbing:** before issuing a dispatch, grep the prompt
  for any of the following patterns. If any match, abort with a warning
  to the user; the prompt would be logged to Storage and visible to
  anyone with `caller_log_url`.
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
