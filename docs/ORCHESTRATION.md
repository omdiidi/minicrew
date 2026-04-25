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
watches each directory independently. The merge session's cwd is a peer of the group dirs;
the merge prompt receives absolute paths to each group's result file and reads them via the
standard file-read tool.

**Note:** earlier revisions of the runner script symlinked documents from the parent tempdir
into each group's cwd. That behavior was removed as an exfiltration vector — the runner
script no longer touches paths outside the session cwd. If a consumer needs files available
inside a session cwd, the enqueuer should write them into the payload or the engine entrypoint
should copy them explicitly.

## Platform-opaque session handles

Session handles are platform-opaque. The orchestrator gets back a `SessionHandle` with a
`kind` discriminator (`mac`, `linux_xfce4`, `linux_xterm`, `linux_tmux`) and a `data` dict
of platform-specific identifiers; the watchdog and close paths never interpret the contents —
they hand the handle back to `platform.close_session` and let the platform act on it.

In fan-out mode each group's handle is persisted as `_session.json` in that group's cwd, so a
SIGTERM or crash-restart can reopen the file and close every live session during the shutdown
sweep. Legacy `_window_id.txt` (Mac-only, pre-platform-layer) is still read during the
one-release upgrade window and wrapped into a synthetic `SessionHandle(kind='mac', data={
'window_id': int(...)})` — this prevents leaked Terminal.app windows if a worker restarts
mid-upgrade with an in-flight Mac job. After one release the legacy fallback goes away.

On Linux, `LinuxPlatform.launch_session` writes `_pending_pid.txt` (two lines: PID, PGID)
into the session cwd **before** entering the wmctrl poll loop. Orchestration shutdown paths
always sweep `_pending_pid.txt` in addition to closing registered handles, so a terminal that
opens milliseconds after the worker decides to abort still gets killed by process group. This
closes the mid-launch SIGTERM race that would otherwise leak an xfce4-terminal + bash + claude
subtree to the systemd user manager.

## Failure modes

If a group terminal times out or errors, the engine records the failure for that group and
still runs the merge with the remaining groups' outputs. The merge prompt is expected to
handle a partial set of inputs gracefully — typically by noting which groups are missing and
producing a best-effort result. If every group fails, the merge still runs but will likely
produce an error; this is acceptable. If the merge terminal itself times out or errors, the
job as a whole is recorded as `error`; partial group outputs are not used.

## Partition strategies

A fan_out job type can declare an optional `partition` block to control how a list
inside `payload` is split across the configured groups:

```yaml
job_types:
  analyze_document:
    mode: fan_out
    partition:
      key: sections        # dotted path into payload (e.g. "data.items")
      strategy: chunks     # "chunks" or "copies"
    groups: [...]
    merge: {...}
```

| Strategy | Behavior | Group template variable values |
|----------|----------|--------------------------------|
| `chunks` | Even-ish split. With 7 items / 3 groups: `[3, 2, 2]` (`divmod` remainder distributes left-to-right). | `group.items = [0,1,2]`, `group.partition_items = ["a","b","c"]` |
| `copies` | Every group sees every item. | `group.items = [0..n-1]`, `group.partition_items = <full list>` |

Each group's prompt template receives:

- `group.document_indices` — back-compat key, identical to `group.items`. Existing
  templates that key on this keep working.
- `group.items` — list of integer indices into `payload[partition.key]`.
- `group.partition_items` — list of the actual selected items (convenience).

When `partition` is omitted on a fan_out job, the loader emits a one-time
`FAN_OUT_PARTITION_DEPRECATED` event per worker boot per job type and behaves as
if `{key: "documents", strategy: "chunks"}` were set. Existing fan_out configs
keep working without modification.

See [PROMPTS.md](./PROMPTS.md#fan-out) for worked render examples.

## ad_hoc mode

`mode: ad_hoc` is for jobs dispatched from a peer Claude Code session. The worker
clones a caller-provided repo, writes a per-job `.claude/settings.json` from the
caller's MCP bundle, renders a built-in prompt template, launches a Terminal
session, and (optionally) pushes a result branch. Consumers do NOT author prompt
templates for ad_hoc; the wrapper is `worker/builtin_prompts/ad_hoc.md.j2`. See
[DISPATCH.md](./DISPATCH.md) for the caller-side contract and
[ARCHITECTURE.md](./ARCHITECTURE.md#the-ad_hoc-lifecycle) for the engine
lifecycle.

## handoff mode

`mode: handoff` resumes an existing local Claude Code session on the worker via
`claude --resume <session-id> --print`. The caller ships their MCP bundle plus
their session's transcript files (top-level JSONL + subagent JSONLs); the worker
writes them into `~/.claude/projects/<encoded>/` before launch and bundles the
extended transcript back via Vault (with Storage fallback for large bundles) so
the caller can `/handoff:reattach` later. See [HANDOFF.md](./HANDOFF.md) for the
user-facing how-to and [DISPATCH.md](./DISPATCH.md#handoff) for the contract.

## Progress tailing

When `cfg.dispatch is not None`, every orchestrator (single, fan_out, ad_hoc,
handoff) starts a `ProgressTailer` thread that reads `_progress.jsonl` from the
session cwd and writes the latest complete line to `jobs.progress`. Batch
deployments without a `dispatch:` block get zero progress threads — fully
gated. See [PROMPTS.md](./PROMPTS.md#progress-reporting) for the line shape and
caps.

## Not a replacement for internal Agent tool

Claude Code's built-in Agent tool spawns sub-agents inside a single session. Those sub-agents
share context with the parent and can pass structured results back efficiently, but they
cannot invoke the full tool set — WebSearch in particular is unavailable inside sub-agents.
Fan-out gives you independent Claude Code sessions per group with full tool access, at the
cost of more wall time and token consumption. Use fan-out when each group needs tools the
Agent tool cannot provide. Use the Agent tool internally when sub-queries are purely
computational over data the session already has.
