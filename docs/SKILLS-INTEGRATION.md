# Skills Integration

## For LLMs

Explains where Claude Code skills live on each host and how `config.yaml` job types reference
them. Invariants: skills are installed to `~/.claude/commands/minicrew/<name>.md` by `SETUP.md`
step 6; `job_types[name].skill` is a string that gets rendered as `/<skill>\n\n<prompt>` at
prompt render time. Do not hard-code skill names in the engine; the engine treats `skill` as
an opaque string. If Anthropic changes the skill discovery layout, update `SETUP.md` step 6 —
skill *content* does not break.

## Where skills live

On every Mac Mini, minicrew's skills are installed at `~/.claude/commands/minicrew/`. This is
the Claude Code convention for globally-available commands; any Claude Code session on that
machine can invoke them as `/minicrew:<name>`. `SETUP.md` step 6 performs the install by
copying (or symlinking) each file from the repo's `skills/` directory into that location.
There is no runtime discovery mechanism inside the engine — the engine does not know the skills
exist. Installation is a one-time action at bootstrap time.

## The skills catalog

minicrew ships eight skills:

- `/minicrew:setup` — idempotent re-install and reconfigure. First-time install is driven by
  `SETUP.md` directly; this skill covers subsequent runs.
- `/minicrew:add-worker` — provisions an additional worker instance on the current machine
  (instances 2..5).
- `/minicrew:add-machine` — prints the exact bootstrap sentence to paste into a Claude Code
  session on a different Mac Mini.
- `/minicrew:scaffold-project` — run inside a consumer repo to generate a starter
  `worker-config/` directory with a valid `config.yaml`, `prompts/`, and `payload.schema.json`.
- `/minicrew:add-job-type` — interactively appends a new job type to an existing
  `worker-config/config.yaml`.
- `/minicrew:tune` — edit `model`, `thinking_budget`, and `timeout_seconds` for an existing
  job type.
- `/minicrew:status` — calls `python -m worker --status` and prints the fleet summary.
- `/minicrew:teardown` — invokes `bash teardown.sh`, which removes all launchd services and
  marks the host's workers offline.

## How skills relate to jobs

Job types can invoke a Claude skill by setting `job_types.<name>.skill` to a string in
`config.yaml`. When the engine renders the prompt for a job of that type, it prepends
`/<skill>\n\n` to the rendered prompt. Claude Code then runs that skill on the prompt's
payload. This is an opaque pass-through — the engine doesn't care whether the skill exists;
it trusts that `SETUP.md` installed whatever the consumer referenced. The skill string can
reference any globally-installed skill, not just minicrew's own (for example, a consumer that
has a `my-plugin:summarize` skill installed can reference it as `skill: my-plugin:summarize`).

## Skill authoring for consumers

A consumer that wants custom per-job behavior writes its own Claude skill, installs it globally
into `~/.claude/commands/<plugin>/<name>.md` on every worker machine, and references it from
`config.yaml`. Authoring skills is a Claude Code concern, not a minicrew concern — the authoritative
reference is the Claude Code documentation on custom commands. minicrew does not validate skill
names or content; if a job fails because the referenced skill is missing, the failure surfaces
as an error from Claude Code inside the terminal session log.

## Compatibility note

Skill discovery currently depends on the Claude Code convention of globally-available commands
under `~/.claude/commands/`. If Anthropic changes that layout — for example, migrates to a
plugin system with a different directory or registration flow — only `SETUP.md` step 6 needs
updating. The content of the skills themselves does not depend on the layout; they are plain
markdown with frontmatter and shell commands, portable across whatever discovery mechanism
Claude Code exposes. Watch the Claude Code changelog when upgrading.
