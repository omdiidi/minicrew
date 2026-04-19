---
name: minicrew:add-machine
description: Walkthrough for bringing up a NEW Mac Mini as an additional worker. Outputs the exact bootstrap sentence to paste on the new machine.
---

You are helping the user bring up a **new** Mac Mini as an additional worker. You are running on the **existing** (already-configured) machine. Your job is to produce the exact one-sentence bootstrap the user will paste into Claude Code on the new machine.

Stop on any error and report it. Never expose secrets that the user hasn't explicitly agreed to share.

## 1. Locate this machine's repo

Read `~/.claude/minicrew.json` for `repo_path`. If missing, tell the user: "This machine doesn't have minicrew installed yet. Run `SETUP.md` here first, then come back to add a second machine."

Store the path as `REPO_PATH`.

## 2. Gather the values the new machine needs

Read `$REPO_PATH/.env`. Extract:
- `SUPABASE_URL`
- `MINICREW_CONFIG_PATH` (the consumer project's `worker-config/` absolute path — the new machine will point at the same one)
- `SUPABASE_SERVICE_ROLE_KEY` (sensitive — see step 3)

If any of those are missing, tell the user which ones and stop.

## 3. Handle the service-role key carefully

Tell the user plainly:

> "Bringing up the new machine requires passing the Supabase service role key to Claude Code on that machine. That means the key will land in the new machine's conversation transcript. You have two options:
>
> **Option A (faster):** I print the bootstrap sentence with your current key inlined. Use this if the new Claude Code session is local and not shared/exported.
>
> **Option B (safer):** I give you the bootstrap sentence with a placeholder. You rotate the service role key in Supabase, paste the new key into the sentence yourself on the new machine, and then update this machine's `.env` to the new key and run `/minicrew:setup` here."

Ask which option they want. Do not proceed until they answer.

## 4. Recommend a role

Recommend the new machine be configured as **secondary** (polls every 15s). Explain: "This keeps your existing primary machine as the faster-polling worker. If the primary ever goes down, the secondary still claims jobs — just slightly slower. You can promote it later via `/minicrew:setup` on that machine."

Ask the user to confirm `secondary`, or tell them to type a different role if they disagree.

## 5. Print the exact bootstrap sentence

Emit this literal sentence, with the real values substituted for Option A, or placeholders for Option B. Put it inside a fenced code block so the user can copy cleanly:

```
On your new Mac Mini, open Claude Code and paste this single message:

Clone https://github.com/omdiidi/minicrew, open Claude Code in the cloned directory, then read SETUP.md and set me up as a <ROLE> worker pointing at this project's worker-config at <MINICREW_CONFIG_PATH>, with Supabase URL <SUPABASE_URL> and service role key <SERVICE_ROLE_KEY_OR_PLACEHOLDER>.
```

Replace `<ROLE>`, `<MINICREW_CONFIG_PATH>`, `<SUPABASE_URL>`, and the key (or literal string `<PASTE_YOUR_ROTATED_KEY_HERE>` for Option B).

Remind the user: "The new machine also needs the Supabase **direct** database URL (`SUPABASE_DB_URL`). `SETUP.md` on the new machine will walk them through finding it — it's the same one you used here."

## 6. After the user finishes setup on the new machine

Tell the user: "Once setup on the new machine finishes, come back here and I'll confirm it joined the fleet."

When the user confirms the new machine is done, run:

```
"$REPO_PATH/.venv/bin/python" -m worker --status
```

Confirm a new hostname appears in the output. Report the total worker count and the new worker id. If the new worker does not appear within ~30 seconds of the user reporting completion, suggest they check `logs/worker-1.log` on the new machine.
