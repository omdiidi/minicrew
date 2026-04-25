<div align="center">

# minicrew

**minicrew runs on Mac Minis and Linux Mint XFCE boxes.** A fleet of visible, unattended
Claude Code sessions.

*Queue-driven. Self-healing. Zero-terminal setup.*

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE)
[![Runs on: macOS | Linux Mint XFCE](https://img.shields.io/badge/runs%20on-macOS%20%7C%20Linux%20Mint%20XFCE-lightgrey.svg)](#requirements)
[![Python: 3.11+](https://img.shields.io/badge/python-3.11+-green.svg)](./requirements.txt)
[![Status: v0.1.0](https://img.shields.io/badge/status-v0.1.0-orange.svg)](https://github.com/omdiidi/minicrew/releases)

</div>

---

## The one-sentence install

Clone the repo, open Claude Code in it, and say:

> *"read SETUP.md and set me up."*

That's it. Claude handles the Supabase credentials prompt, venv, launchd services, and skill installation. No shell commands to memorize, ever.

---

## What it actually is

Most worker-queue patterns hide the work inside a headless subprocess. **minicrew does the opposite**: every job opens a real, visible `Terminal.app` window running a full `claude` session — Read, Write, Bash, WebSearch, every tool Claude Code supports. You can watch work happen in real time, tail logs by eye, or intervene when debugging. When the job finishes, the window closes itself.

No human has to click anything. Between jobs, the worker polls a Supabase table, claims the next row atomically, launches the next session. When a session finishes — or gets stuck and the watchdog kills it — the worker writes the result back and loops.

```
   ┌────────────────┐       ┌─────────────────────────────────┐
   │ Your project   │──────▶│  Supabase                        │
   │ (enqueues jobs)│       │  jobs table (the queue)          │
   └────────────────┘       └────────────┬────────────────────┘
                                         │ atomic claim
            ┌────────────────────────────┼────────────────────────────┐
            ▼                            ▼                            ▼
   ┌──────────────────┐         ┌──────────────────┐         ┌──────────────────┐
   │  Mac Mini 1      │         │  Mac Mini 2      │         │  Mac Mini N      │
   │  role: primary   │         │  role: secondary │         │  role: secondary │
   │                  │         │                  │         │                  │
   │  [term] [term]   │         │  [term] [term]   │         │  [term] [term]   │
   │  [term] [term]   │         │  [term]          │         │  [term] [term]   │
   │                  │         │                  │         │                  │
   │  up to 5 parallel│         │  up to 5 parallel│         │  up to 5 parallel│
   └──────────────────┘         └──────────────────┘         └──────────────────┘
          \                            |                           /
           \___________________________|__________________________/
                                       │
                          Fleet-wide coordination:
                          atomic claim, heartbeats,
                          opportunistic reaper
```

---

## Standout capabilities

### Visible, unattended Claude Code sessions

Every job = one real `Terminal.app` window = one full Claude Code session with unrestricted tool access. Not a subprocess. Not a headless API call. The window is deliberately visible so engineers can audit work during dev and tail it with their eyes. It disappears when the job completes. `--dangerously-skip-permissions` is the intentional tradeoff — [SECURITY.md](./SECURITY.md) covers the posture and a planned hardened-mode alternative.

### Fan-out mode — parallel Claude sessions + merge, each with full tool access

Claude Code's built-in Agent tool can't use WebSearch, Bash, or any network tool from sub-agents. minicrew's `fan_out` mode fixes that: declare `mode: fan_out` with N groups in your `config.yaml`, and the engine launches N parallel `Terminal.app` windows, each a completely independent Claude Code session, then runs a final `merge` session that consolidates the group outputs.

Ideal for:

- Per-document analysis across a corpus (each group = subset of documents)
- Multi-region pricing sweeps (each group = a region, all with WebSearch)
- Independent classification of 30 images (split into 3 groups of 10, merge results)
- Any "N independent pieces + 1 combine" workflow

Per-group watchdogs run in parallel threads. A group that hangs is killed independently; its name is threaded into `missing_groups` so the merge template handles partial results.

### Skills per job type

Every job type in `config.yaml` can invoke any globally-installed Claude Code skill:

```yaml
job_types:
  analyze_contract:
    skill: my-legal-plugin:analyze
    model: claude-opus-4-7
    thinking_budget: high
```

The engine prefixes the rendered prompt with `/<skill>` so Claude runs your skill end-to-end on the job payload. Pair with custom Anthropic-side plugins (local or published) for domain-specific tool access — your skills inherit the full Claude Code environment.

### Tune cost vs quality per workload

Config maps directly to Claude Code CLI flags:

| Config field        | CLI flag      | Values                                                              |
| ------------------- | ------------- | ------------------------------------------------------------------- |
| `model`             | `--model`     | `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5`          |
| `thinking_budget`   | `--effort`    | `none` → `low`, `medium` → `medium`, `high` → `high`                |

Change it with a conversation: *"/minicrew:tune analyze_contract, switch to sonnet and medium"*. Workers pick it up on their next restart.

### Multi-machine scaling math

No inter-worker messaging. No Redis. No ZooKeeper. The database is the sole coordination layer.

| Machines | Instances each | Concurrent sessions | Typical use                          |
| -------: | :------------: | :-----------------: | ------------------------------------ |
|        1 |        1       |          1          | Dev / trial                          |
|        1 |        3       |          3          | Single-box production                |
|        3 |        3       |          9          | Small fleet                          |
|        5 |        5       |         25          | Full-capacity homelab                |
|       10 |        5       |         50          | At this scale consider v2 routing    |

Add a new machine with `/minicrew:add-machine` — the skill prints the exact one-sentence bootstrap to paste on the new Mac Mini. First machine defaults to `primary` (polls every 5s), additional machines default to `secondary` (polls every 15s) so the primary wins the race on fresh jobs.

### Self-healing via opportunistic reaper

Every worker runs its own reaper thread — but a transaction-level Postgres advisory lock (`pg_try_advisory_xact_lock`) guarantees exactly one reaper runs per cycle across the entire fleet. No leader election, no coordination service, no single point of failure.

When a worker dies mid-job, another worker's reaper detects the stale heartbeat (default: 120 seconds), requeues the orphaned job, and increments `attempt_count`. Poison-pill protection: after `max_attempts` total runs (default 3), the job becomes `failed_permanent` for human review instead of cycling forever.

### Atomic claim — no double processing

The claim is a single PostgREST PATCH with a `status='pending'` filter. Two workers racing for the same row — one wins by row count, the other gets nothing and moves on. Simple, proven, impossible to double-claim.

### Idle watchdog per session

Recursive file-activity walk of each session's working directory. Claude writing files = session alive. No writes for 25 minutes, and no result file? The watchdog terminates the Terminal window and marks the job `error`. Result file exists but hasn't been touched in 15 minutes (hung post-processing)? Same. Recovery time on a stuck job drops from "2-hour job timeout" to "≤30 min."

### LLM-native configuration

The config contract is intentionally boring so an AI agent can edit it reliably:

- **YAML + Jinja** for job type declarations and prompt templates
- **JSON Schema** (strict, `additionalProperties: false`) validates at load
- **`StrictUndefined` + `finalize=json.dumps`** so template typos fail loudly and payload values auto-JSON-encode
- **`payload.schema.json`** per consumer validates job payloads before launching Claude

Any agent handed your repo URL can read [INTEGRATE.md](./INTEGRATE.md) in one pass and wire up a consumer project end-to-end.

---

## Quick start: enqueue a job

With a worker running, insert a row via curl:

```bash
curl -X POST "$SUPABASE_URL/rest/v1/jobs" \
  -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY" \
  -H "Content-Type: application/json" \
  -H "Prefer: return=representation" \
  -d '{"job_type":"summarize","payload":{"text":"Lorem ipsum..."}}'
```

Within one poll interval a Terminal window opens on a Mac Mini, Claude summarizes the text, writes `result.json`, the worker PATCHes the row to `status='completed'`, and the window closes. Full patterns for FastAPI, Next.js server actions, and Supabase Edge Functions: [INTEGRATE.md](./INTEGRATE.md).

---

## Security posture

| Layer                        | Defense                                                                                                        |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Secrets                      | `.env` only (chmod 0600), never in launchd plists. Worker loads `.env` at startup.                             |
| Redaction                    | Structured-logging filter masks any value whose source env var name is in `logging.redact_env`.                |
| Prompt injection             | Jinja `finalize=` callback auto-JSON-encodes non-strings. Untrusted text must pipe through `| tojson`.         |
| Path traversal               | Config loader and `result_io` enforce realpath containment; JSON Schema `pattern` constraints on filenames.    |
| Symlink attacks              | `read_result_safe` uses `O_NOFOLLOW` plus fd-based containment check.                                          |
| Stale-worker overwrites      | Every job mutation filters by `(id, worker_id)` — a stale worker cannot clobber a reclaimed row.               |
| Audit trail                  | Immutable `claimed_at`, `started_at`, `completed_at`, `worker_id`, `worker_version` on every row.              |

Full threat model, key-rotation walkthrough, and RLS guidance in [SECURITY.md](./SECURITY.md).

---

## What you get in the box

```
minicrew/
├── worker/                     Python engine (atomic claim, reaper, watchdog, launcher)
├── schema/
│   ├── template.sql            Supabase DDL: jobs, workers, worker_events, RPC, view
│   └── config.schema.json      Strict JSON Schema for worker-config validation
├── skills/                     Eight conversational Claude Code skills
│   ├── setup.md                  /minicrew:setup       — re-setup / reconfigure
│   ├── add-worker.md             /minicrew:add-worker  — add another instance
│   ├── add-machine.md            /minicrew:add-machine — bootstrap another Mac Mini
│   ├── scaffold-project.md       /minicrew:scaffold-project — wire a consumer repo
│   ├── add-job-type.md           /minicrew:add-job-type — append a job type
│   ├── tune.md                   /minicrew:tune         — change model / effort / timeout
│   ├── status.md                 /minicrew:status       — fleet-wide health
│   └── teardown.md               /minicrew:teardown     — remove all workers
├── examples/
│   ├── minimal/                  Single-session text summarization
│   └── fan-out/                  Three parallel groups + merge document analysis
├── docs/                       Deep-dive documentation
└── ci/                         Domain-scrub patterns (CI enforces generic-template invariant)
```

---

## Documentation map

| Audience                                | Start here                                                    |
| --------------------------------------- | ------------------------------------------------------------- |
| Setting up a Mac Mini                   | [SETUP.md](./SETUP.md) (Claude-executable)                    |
| Wiring a consumer project               | [INTEGRATE.md](./INTEGRATE.md) (agent-consumable)             |
| How the engine works                    | [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)                |
| Deploying on Linux Mint XFCE            | [docs/LINUX.md](./docs/LINUX.md)                              |
| Queue priority, multi-machine, reaper   | [docs/QUEUEING.md](./docs/QUEUEING.md)                        |
| Single vs fan-out mode                  | [docs/ORCHESTRATION.md](./docs/ORCHESTRATION.md)              |
| Tuning model + thinking budget          | [docs/MODEL-TUNING.md](./docs/MODEL-TUNING.md)                |
| Supabase schema + RLS guidance          | [docs/SUPABASE-SCHEMA.md](./docs/SUPABASE-SCHEMA.md)          |
| Config reference                        | [docs/CONFIG-REFERENCE.md](./docs/CONFIG-REFERENCE.md)        |
| Threat model                            | [SECURITY.md](./SECURITY.md)                                  |
| AI agents integrating from outside      | [llms.txt](./llms.txt) and [INTEGRATE.md](./INTEGRATE.md)     |

---

## Requirements

| OS                | Required binaries                                           | Required services     |
| ----------------- | ----------------------------------------------------------- | --------------------- |
| macOS             | `claude`, `osascript` (built-in)                            | `launchd` (built-in)  |
| Linux Mint XFCE   | `claude`, `wmctrl`, `xdotool`, `xfce4-terminal`, `tmux`     | `systemd` (user bus)  |

- Python 3.11+ on either OS.
- Claude Code (`npm install -g @anthropic-ai/claude-code`) authenticated on the machine.
- A Supabase project (free tier is fine for development).

### What's different on Linux

minicrew on Linux mirrors the Mac Mini pattern — same Python engine, same Supabase schema,
same atomic-claim + reaper + watchdog behavior. What changes is the platform-abstracted
backend: instead of osascript + launchd + Terminal.app, the Linux build uses `xfce4-terminal`
(or `tmux` headlessly) + `wmctrl`/`xdotool` + systemd user units. Everything OS-specific
lives behind the `Platform` protocol in `worker/platform/`; mixed-OS fleets coordinate
through the same database with no special configuration. Mint-specific setup (LightDM
auto-login, X11 session selection, systemd unit environment, logrotate with `copytruncate`,
`MINICREW_TMPDIR` for tmpfs pressure, the X11 threat model and dedicated-user
recommendation) is covered in [docs/LINUX.md](./docs/LINUX.md).

---

## Roadmap

- **v0.2** — Linux support (systemd, GNOME Terminal / xterm equivalent of Terminal.app)
- **v0.2** — Postgres and HTTP logging sinks (schema already reserves `worker_events`)
- **v0.3** — Tag-based routing (`requires: [heavy, vision]` on jobs, `capabilities: [...]` on workers)
- **v0.3** — Config hot-reload via SIGHUP
- **Later** — Pluggable queue backend (Redis Streams, SQS, Cloudflare Queues)

---

## License

MIT. See [LICENSE](./LICENSE).

Built for and named after the first deployment environment — a homelab of Mac Minis doing real work, headless-server style but with faces.
