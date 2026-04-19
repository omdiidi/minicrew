# SETUP.md

First-time worker bootstrap for a Mac Mini. This is the primary entry point for a fresh clone.

## For LLMs executing this file

You are setting up minicrew on a Mac Mini. Each step below is idempotent — running it twice is safe. On any failure, explain the failure to the user verbatim and stop; do not guess a workaround. Never push to git. Never commit secrets. Collect all user inputs in a single message rather than drip-feeding questions one at a time. Work through the steps in order; do not reorder them.

All commands assume the current working directory is this repo's root (the directory containing this file).

---

## Step 1 — Prereq check

**Goal:** confirm the machine can run minicrew.

**Commands:**
```bash
which claude
claude --version
python3 --version
gh --version   # optional; only needed for later GitHub operations
```

**Success criterion:**
- `which claude` prints a path (Claude Code is installed and in PATH).
- `claude --version` prints a version string.
- `python3 --version` prints `Python 3.11.x` or higher.
- `gh` is optional — a missing `gh` is not a failure.

**Failure response:** If `claude` is missing, instruct the user to install Claude Code (`npm install -g @anthropic-ai/claude-code`) and then run `claude` once interactively to authenticate. If Python is older than 3.11, instruct them to install 3.11+ (Homebrew: `brew install python@3.11`). Stop until the user confirms fixed.

---

## Step 2 — Ask the user

**Goal:** collect every value we need in one message.

**Send ONE message** requesting all of these, with the stated walkthrough for the direct DB URL:

1. **Supabase project URL** — e.g. `https://abcdefghij.supabase.co`.
2. **Supabase service role key** — from Project Settings → API → `service_role` key (NOT the anon key).
3. **Supabase direct database URL** — walkthrough: Supabase Dashboard → Project Settings → Database → Connection string → pick the **Direct connection** tab → copy the `postgresql://...:5432/postgres` string. The pooler URL (port 6543) will not work; advisory locks require a direct connection.
4. **Role** — `primary` (5s poll interval) or `secondary` (15s poll interval).
5. **Instance count** — integer 1 to 5. Number of worker processes to run on this Mac Mini.
6. **Consumer `worker-config/` absolute path** — the absolute path to the directory in the consumer project that contains `config.yaml` and `prompts/`.

**Caveat to communicate to the user in the same message:** the service role key will appear in the Claude Code conversation transcript when they reply. Recommend running setup inside a local (non-shared) Claude Code session and rotating the key afterwards if the session was exported, logged, or shared.

**Success criterion:** user replies with all six values. Do not echo the service role key or the direct DB URL back to the user after receipt.

**Failure response:** if a value is missing, ask once for the missing value. If the user cannot locate the direct DB URL in the dashboard, restate the walkthrough and stop.

---

## Step 3 — Write `.env`

**Goal:** create a locked-down `.env` containing the credentials.

**Commands:**
```bash
cp -n .env.example .env
chmod 600 .env
```

Then, using an Edit-style write (do NOT echo the key to the shell), populate `.env` with the values from Step 2:
```
SUPABASE_URL=<value from step 2.1>
SUPABASE_SERVICE_ROLE_KEY=<value from step 2.2>
SUPABASE_DB_URL=<value from step 2.3>
MINICREW_CONFIG_PATH=<value from step 2.6>
```

**Success criterion:** `ls -l .env` shows `-rw-------` (mode 600) and the file contains all four variables non-empty.

**Failure response:** if `.env` already existed before this step and has values, ask the user whether to overwrite. Do not silently overwrite existing credentials.

---

## Step 4 — Create venv and install requirements

**Goal:** isolated Python environment with runtime deps installed.

**Commands:**
```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

**Success criterion:** `.venv/bin/python -c "import worker"` exits 0 (the worker package imports cleanly).

**Failure response:** report the `pip install` stderr verbatim. Common cause is psycopg build failure on older Xcode command-line tools — recommend `xcode-select --install` and retry. Stop until imports succeed.

---

## Step 5 — Validate consumer `worker-config/`

**Goal:** fail fast if the consumer config is malformed, before we install launchd services.

**Commands:**
```bash
.venv/bin/python -m worker.config.loader --validate "$MINICREW_CONFIG_PATH"
```

(Substitute the actual absolute path from Step 2.6 if the shell does not have `.env` loaded.)

**Success criterion:** exit code 0, stdout `config valid`.

**Failure response:** print the validator's error message verbatim (it includes the JSON path to the bad field). STOP. Do not proceed to launchd install with an invalid config. Ask the user to fix their `worker-config/config.yaml` (or run `/minicrew:scaffold-project` in their consumer repo once skills are installed).

---

## Step 6 — Install skills

**Goal:** copy the repo's skills into `~/.claude/commands/minicrew/` so future Claude Code sessions can invoke `/minicrew:setup`, `/minicrew:add-worker`, `/minicrew:status`, etc.

**Commands:**
```bash
mkdir -p "$HOME/.claude/commands/minicrew"
cp skills/*.md "$HOME/.claude/commands/minicrew/"
ls "$HOME/.claude/commands/minicrew/"
```

**Success criterion:** `ls` shows all the skill markdown files present in `~/.claude/commands/minicrew/`.

**Tell the user:** after this step any future Claude Code session (on this machine, in any directory) can invoke `/minicrew:setup`, `/minicrew:add-worker`, `/minicrew:status`, `/minicrew:scaffold-project`, `/minicrew:tune`, `/minicrew:add-job-type`, `/minicrew:add-machine`, and `/minicrew:teardown`.

**Failure response:** if `cp` fails, check that `skills/` exists in the current repo; if not, the user is not in the repo root. Stop.

---

## Step 7 — Install launchd services

**Goal:** register N long-running worker services with launchd so they survive reboots and auto-restart on crash.

**Commands** (loop `$i` from 1 to `$INSTANCE_COUNT` from Step 2.5, with `$ROLE` from Step 2.4):
```bash
for i in $(seq 1 $INSTANCE_COUNT); do
  .venv/bin/python -m worker.utils.launchd install \
    --instance $i \
    --role $ROLE \
    --config-path "$MINICREW_CONFIG_PATH"
done
```

**Success criterion:** each invocation exits 0. No `launchctl bootstrap failed` errors.

**Failure response:** if a specific instance fails, print the launchctl stderr verbatim. Common cause is a stale plist from a previous install — the installer retries `bootout` + `bootstrap` internally up to 3 times; if it still fails, stop and ask the user to run `launchctl list | grep com.minicrew.worker` and report what they see.

---

## Step 8 — Verify

**Goal:** confirm the fleet is alive and polling.

**Commands:**
```bash
sleep 15
launchctl list | grep com.minicrew.worker
tail -n 50 logs/worker-1.log
.venv/bin/python -m worker --status
```

**Success criterion:**
- `launchctl list | grep com.minicrew.worker` shows N rows, one per instance, each with a non-`-` PID (the services are running, not failed).
- `tail logs/worker-1.log` contains a `worker_started` event in JSON.
- `python -m worker --status` prints a JSON object with keys `workers`, `queue_depth`, `recent_failures` and exits 0.

**Failure response:** if a worker is listed but has PID `-` and a non-zero exit status, open `logs/worker-<n>.err` and report the stderr to the user. Common causes: `.env` missing a value, `MINICREW_CONFIG_PATH` not readable by the launchd process, or a pooler URL accidentally supplied as `SUPABASE_DB_URL` (the engine will fail startup with a pointer to `docs/SUPABASE-SCHEMA.md#direct-vs-pooler`).

---

## Step 9 — Write `~/.claude/minicrew.json`

**Goal:** record the absolute repo path so installed skills can find the engine regardless of the user's current working directory.

**Commands:**
```bash
REPO_PATH="$(pwd -P)"
mkdir -p "$HOME/.claude"
cat > "$HOME/.claude/minicrew.json" <<EOF
{"repo_path": "$REPO_PATH"}
EOF
```

**Success criterion:** `cat ~/.claude/minicrew.json` prints valid JSON with a `repo_path` key pointing to the absolute path of this repo.

**Failure response:** if `pwd -P` does not return an absolute path (should be impossible in practice), ask the user for the absolute path to the repo and use that literal value.

---

## Done

Report to the user:
- Number of worker instances installed.
- Role (primary or secondary).
- Path to logs (`logs/worker-<n>.log`).
- Instruction: "try `/minicrew:status` in any future Claude Code session to check fleet health."

Remind them the service role key is in `.env` with mode 600 and was also pasted into this conversation transcript; if this session was shared or exported, rotate the key per [SECURITY.md](./SECURITY.md#key-rotation).
