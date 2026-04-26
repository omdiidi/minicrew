# Subagent Integration

Minicrew exposes itself to Claude Code through three caller-side surfaces. They
all bottom out on the same load-bearing primitive — `python -m worker --dispatch
ad_hoc --wait` — but each surface is tuned for a different operator workflow.

## For LLMs

This doc explains the three invocation paths, the install layout, the
caller-side base64 extraction contract for `Task()` replies, and the recursion
guard. It is the source of truth for what gets installed under
`~/.claude/commands/minicrew/` and `~/.claude/agents/`. When you add a new
invocation surface, update this doc AND `docs/ARCHITECTURE.md`'s
"Subagent Surface" section AND `docs/COMMANDS.md`'s slash-commands table.

## The three invocation paths

### Path A — Explicit slash skill

The operator types a slash command. Claude Code reads the skill body from
`~/.claude/commands/minicrew/<name>.md`, constructs the dispatch CLI invocation,
and runs it via the Bash tool.

```
operator types:  /minicrew:dispatch "audit this repo for SQL injection"
Claude Code:     reads ~/.claude/commands/minicrew/dispatch.md
                 -> python -m worker --dispatch ad_hoc \
                       --repo <auto> --sha <auto> --prompt "..." --wait
                 -> Bash tool executes
                 -> captures result.json contents
                 -> presents to user inline
```

Two slash skills ship in `skills/`:

- `/minicrew:dispatch <task>` — single ad_hoc dispatch, blocks via `--wait`.
- `/minicrew:fanout N <task>` — N parallel dispatches; optionally synthesize.

This path is the predictable one. It is the right surface when the operator
KNOWS they want to delegate.

### Path B — Native `Task()` custom agent

Claude Code's `Task` tool spawns a sub-Claude with a restricted tool set. The
sub-Claude reads `~/.claude/agents/minicrew-mac-mini.md`, runs the dispatch
CLI, and returns the worker's `result.json` to the calling session inside
delimited markers.

```
Caller Claude (any session, any repo) decides task warrants delegation:
  Task(
    subagent_type="Minicrew Mac Mini",
    description="Audit repo for SQL injection",
    prompt="repo=https://github.com/owner/repo sha=<40hex> ... <task text>"
  )
Claude Code reads ~/.claude/agents/minicrew-mac-mini.md
  -> spawns sub-Claude with tools restricted to Bash + Read
  -> sub-Claude runs python -m worker --dispatch ad_hoc ... --wait
  -> sub-Claude wraps result in base64 markers and returns it
  -> caller Claude extracts the result and continues its conversation
```

The `subagent_type` value must match the `name:` in the agent's frontmatter.
Probe 0b confirmed Claude Code accepts the display-name form
(`"Minicrew Mac Mini"`). The custom-agent file ships at `agents/minicrew-mac-mini.md`
and is installed to `~/.claude/agents/` by `SETUP.md` Step 7 (macOS) / Step 8
(Linux).

### Path C — Prose discovery via routing-rules

The operator types a natural-language request without any slash. Claude Code
matches the prose against installed skill descriptions AND any imported
`routing-rules.md` fragment, and decides whether to dispatch.

```
Operator: "run a security audit across 3 sessions"
Claude Code: matches "across N sessions" against /minicrew:fanout description
             -> invokes /minicrew:fanout with N=3
             -> same as Path A but spawned 3x
```

This path requires the consumer project's `CLAUDE.md` to import the routing
fragment:

```
@~/.claude/commands/minicrew/routing-rules.md
```

Without the import, Claude Code may still match descriptions opportunistically
but will not have the explicit "Dispatch when X / Stay local when Y"
heuristics. The fragment is conservative — when in doubt, stay local.

## Install layout

```
~/.claude/
├── commands/minicrew/
│   ├── dispatch.md                ← Path A: /minicrew:dispatch
│   ├── fanout.md                  ← Path A: /minicrew:fanout
│   ├── routing-rules.md           ← Path C: imported via CLAUDE.md
│   └── ... (other minicrew skills: setup, status, add-worker, ...)
└── agents/
    └── minicrew-mac-mini.md       ← Path B: Task(subagent_type="Minicrew Mac Mini")
```

Both directories are populated by `SETUP.md` Step 7 (macOS) / Step 8 (Linux).
The `cp` is idempotent; re-run setup to refresh after pulling new minicrew
versions.

## Required environment for callers

Even with the agent file installed, the calling Claude Code session needs
`SUPABASE_URL` and one of `MINICREW_DISPATCH_JWT` /
`SUPABASE_SERVICE_ROLE_KEY` exported. The agent shells out to
`python -m worker --dispatch` which reads from env. See SETUP.md for the
standard install.

## Caller-side base64 extraction (for Path B)

The `Minicrew Mac Mini` agent wraps its reply in base64 between explicit
markers so the calling Claude can extract the worker's `result.json` reliably,
even when the result contains the literal delimiter string. The base64
alphabet (A-Z, a-z, 0-9, +, /, =) cannot collide with the delimiter, so worker
output containing ASCII delimiters can never corrupt extraction.

Use this Python helper in caller-side code that needs to consume a Task() reply
programmatically:

```python
import base64, re

def extract_minicrew_result(reply_text: str) -> str:
    """Extract base64-wrapped result from a Minicrew Mac Mini Task() reply.

    Returns decoded UTF-8 text. Falls back to the raw reply when markers are
    absent (e.g. pre-base64 wrapper version, or an unwrapped error string)."""
    m = re.search(
        r"===MINICREW_B64_BEGIN===\s*([A-Za-z0-9+/=\s]+?)\s*===MINICREW_B64_END===",
        reply_text, re.DOTALL,
    )
    if not m:
        return reply_text  # fallback: pre-base64 reply or unwrapped error
    b64 = re.sub(r"\s+", "", m.group(1))
    # strict mode catches corruption early; fall back to "replace" if you need lossy decode
    return base64.b64decode(b64).decode("utf-8", "strict")
```

Truncation: the agent caps the pre-encode body at 700,000 bytes (NOT
characters; the implementation uses `wc -c` after Chunk A's fix to handle
multibyte UTF-8 correctly). Above that, the agent emits a small JSON pointer
(`{"truncated": true, "result_size": N, "job_id": ..., "preview": ...,
"note": "fetch full result via supabase jobs row"}`) instead of the full
body. The caller can fetch the full row from Supabase by `job_id` when the
truncated flag is set.

## Recursion guard

A worker session that has the `Minicrew Mac Mini` agent installed in its own
`~/.claude/agents/` could in principle dispatch back into the fleet — workers
spawning workers indefinitely, exhausting the per-caller cap and burning
budget. To prevent this:

- Every worker runner script (emitted by `worker/terminal/launcher.py` and
  `worker/terminal/launcher_resume.py`) sets `export MINICREW_INSIDE_WORKER=1`
  in its preamble before invoking `claude`. The env var propagates into the
  Claude Code subprocess and any tool calls it makes.
- The `Minicrew Mac Mini` agent body checks `${MINICREW_INSIDE_WORKER:-0}` at
  the top of its instructions. If it sees `1`, it REFUSES to dispatch and
  returns an error object inside the standard base64 markers.
- The `routing-rules.md` fragment also documents this: if you find yourself
  inside a worker (env var set), do the task locally rather than dispatching.

The guard is defense-in-depth: the agent body is the primary gate; the
routing-rules note is operator-visible documentation; the env var export is
the load-bearing detection signal. Adding a new runner-script emitter
(e.g. the in-flight tmux-via-tmux plan) MUST preserve this export — see the
TODO comment in `worker/terminal/_runner_common.py`.

## Coexistence

Path A (slash skills) is the lowest-common-denominator surface. Even if a
machine's Claude Code version pre-dates user-scoped custom agents, the slash
skills still work — they're plain markdown files under
`~/.claude/commands/`. Path B (custom agent) requires Claude Code 2.x+ for
user-scoped `~/.claude/agents/` discovery. If the operator's CC version
doesn't support it, the agent file is harmlessly ignored and operators fall
back to the slash path.

Path C (prose discovery) layers on top of Path A — it never bypasses the
slash skill, it only changes the trigger from operator typing `/` to Claude
Code matching prose against descriptions. If prose discovery doesn't fire on
a given phrasing, iterate the skill description text (more synonym keywords,
stronger first sentence) rather than building a separate code path.

## Validation

After install, verify in this order:

1. `ls ~/.claude/commands/minicrew/dispatch.md ~/.claude/commands/minicrew/fanout.md`
   — slash skills installed.
2. `ls ~/.claude/agents/minicrew-mac-mini.md` — custom agent installed (skip
   if CC version doesn't support user-scoped agents).
3. In an interactive Claude Code session, the `/agents` slash command lists
   `Minicrew Mac Mini` under user-scoped agents.
4. From inside a git repo,
   `python -m worker --dispatch ad_hoc --prompt "say HI" --wait` succeeds
   without explicit `--repo`/`--sha` (CLI infers from cwd).
5. Operator types `/minicrew:dispatch "list .md files"` in a fresh session —
   result returns within a minute.
6. With `routing-rules.md` imported, the prose "audit this repo for dead code
   in 3 sessions" routes to `/minicrew:fanout` with N=3 without explicit slash.

## See also

- `docs/ARCHITECTURE.md` — `Subagent Surface` section: where each path lives
  in the install layout and how it interacts with the dispatch CLI.
- `docs/COMMANDS.md` — flat reference table for `/minicrew:dispatch` and
  `/minicrew:fanout`.
- `docs/DISPATCH.md` — the load-bearing `python -m worker --dispatch ad_hoc`
  CLI contract that every path bottoms out on.
- `docs/HANDOFF.md` — distinct surface for handing off the current session
  rather than dispatching ad-hoc.

## Rollback

To remove the subagent-integration layer (slash skills, custom agent, routing
fragment) without uninstalling the worker engine, see
[docs/TROUBLESHOOTING.md](./TROUBLESHOOTING.md#rolling-back-the-subagent-integration-layer).
