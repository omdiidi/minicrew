# Orchestration Modes

## For LLMs

Describes `single` (default) and `fan_out` orchestration modes. Invariants: `mode: single` is
the default and covers the common case; `mode: fan_out` requires `groups` (at least one) and
`merge` in the job type config; each group writes its own `result_filename` inside
`<tempdir>/group_<name>/`, the merge terminal reads them and writes the top-level
`result_filename`. Do not conflate fan-out (full Claude Code sessions per group) with Claude
Code's internal Agent tool (sub-agents inside one session). They are different tools.

## Single mode (default)

The default mode is `single`. One job produces one Terminal window hosting one Claude Code
session that writes one result file. This covers the overwhelming common case: summarization,
extraction, classification, analysis of a single input, one-shot transformations. The flow is:

```
  jobs row (pending)
        |
        v
  [worker claims]
        |
        v
  [worker creates tempdir, renders prompt, launches Terminal]
        |
        v
  +-----------------+
  | Terminal window |
  |                 |
  | Claude Code     |
  | runs prompt,    |
  | writes          |
  | result.json     |
  +-----------------+
        |
        v
  [watchdog detects result.json or kills on idle]
        |
        v
  [worker reads result.json, writes jobs.result]
        |
        v
  jobs row (completed)
```

Nothing else. No fan-out, no merge, no inter-session coordination. If your job fits this
shape, use single mode.

## Fan-out mode

Fan-out is an opt-in mode for jobs with N independently-processable parts where combining is
itself a separate operation. The canonical example: a multi-document analysis where each
document is analyzed independently, then a merge step produces a cross-document summary. The
engine launches one Terminal window per group in parallel, waits for each group's result, then
launches a merge Terminal that reads all group outputs and produces the job's final result.

Fan-out is worth reaching for when each group genuinely needs its own Claude Code session —
typically because each group needs WebSearch or Bash or other tools that Claude Code's
internal Agent tool cannot use inside a sub-agent. The cost is more total wall time and more
total tokens; the benefit is full tool access per group.

## Fan-out config schema

A fan-out job type declares `mode: fan_out`, a list of `groups`, and a `merge` block:

```yaml
job_types:
  multi_doc_analysis:
    description: "Analyze each document then summarize across them"
    mode: fan_out
    model: claude-sonnet-4-6
    thinking_budget: medium
    timeout_seconds: 7200
    result_filename: final.json
    groups:
      - name: doc_a
        prompt_template: group.md.j2
        result_filename: group_result.json
        idle_timeout_seconds: 1500
        result_idle_timeout_seconds: 900
      - name: doc_b
        prompt_template: group.md.j2
        result_filename: group_result.json
      - name: doc_c
        prompt_template: group.md.j2
        result_filename: group_result.json
    merge:
      prompt_template: merge.md.j2
      idle_timeout_seconds: 1500
      result_idle_timeout_seconds: 900
```

Every group can use the same template (most common; the template branches on a payload
variable indicating which group it is) or distinct templates (for heterogeneous group work).
The merge template receives the list of group outputs as a Jinja variable.

## Directory layout per session

For a fan-out job the engine creates a job tempdir and, within it, one subdirectory per group
plus a merge subdirectory:

```
<tempdir>/
  group_doc_a/
    _prompt.txt
    _run.sh
    group_result.json    # written by this group's Claude Code session
  group_doc_b/
    _prompt.txt
    _run.sh
    group_result.json
  group_doc_c/
    _prompt.txt
    _run.sh
    group_result.json
  merge/
    _prompt.txt
    _run.sh
    final.json           # written by the merge session; becomes jobs.result
```

Each group's Claude Code session runs with `cwd` set to its own subdirectory. The watchdog
watches each directory independently. The merge session's cwd contains the other group
directories as siblings so the merge prompt can reference them.

## Failure modes

If a group terminal times out or errors, the engine records the failure for that group and
still runs the merge with the remaining groups' outputs. The merge prompt is expected to
handle a partial set of inputs gracefully — typically by noting which groups are missing and
producing a best-effort result. If every group fails, the merge still runs but will likely
produce an error; this is acceptable. If the merge terminal itself times out or errors, the
job as a whole is recorded as `error`; partial group outputs are not used.

## Not a replacement for internal Agent tool

Claude Code's built-in Agent tool spawns sub-agents inside a single session. Those sub-agents
share context with the parent and can pass structured results back efficiently, but they
cannot invoke the full tool set — WebSearch in particular is unavailable inside sub-agents.
Fan-out gives you independent Claude Code sessions per group with full tool access, at the
cost of more wall time and token consumption. Use fan-out when each group needs tools the
Agent tool cannot provide. Use the Agent tool internally when sub-queries are purely
computational over data the session already has.
