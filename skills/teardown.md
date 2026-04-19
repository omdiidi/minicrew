---
name: minicrew:teardown
description: Stop and remove all minicrew workers on this Mac Mini. Workers mark themselves offline as they exit.
---

You are tearing down every minicrew worker launchd service on **this** Mac Mini. Workers on other machines are not affected. In-flight jobs will be requeued by their workers' graceful-shutdown handlers.

Follow these steps in order. Stop on any error and report it.

## 1. Locate the repo

Read `~/.claude/minicrew.json` for `repo_path`. If the file is missing, tell the user: "minicrew does not appear to be registered on this machine. If there are launchd services anyway, I can still remove them — tell me the absolute path to the minicrew checkout." Store as `REPO_PATH`.

## 2. List existing services

Run:

```
launchctl list | grep com.minicrew.worker
```

If there are zero entries, tell the user: "No minicrew workers are running on this machine. Nothing to tear down." and stop.

Otherwise, capture the list of service labels and their instance numbers for the confirmation prompt.

## 3. Confirm with the user

Show the list and ask explicitly:

> "This will stop N workers on this machine:
>
> <list of labels>
>
> Each worker will finish its current job or requeue it, mark itself offline in the database, and exit. Workers on other machines are not affected. Continue? (yes/no)"

Do not proceed until the user types `yes` (or equivalent affirmative). If they say no, stop and confirm nothing was changed.

## 4. Run the teardown script

Run:

```
bash "$REPO_PATH/teardown.sh"
```

`teardown.sh` invokes `python -m worker.utils.launchd uninstall --instance $i` for instances 1..5, tolerating missing ones. If the script itself errors (as opposed to individual instance uninstalls reporting "not loaded"), report the stderr verbatim and stop.

## 5. Verify

Run again:

```
launchctl list | grep com.minicrew.worker
```

Confirm the output is empty. If any service remains, report which ones and suggest the user retry the teardown or manually bootout the stragglers via:

```
launchctl bootout gui/$(id -u)/<label>
```

## 6. Report

Tell the user:
- How many workers were torn down on this machine.
- Confirmation that nothing remains in `launchctl list`.
- Reminder: "Workers on other machines are unaffected. Use `/minicrew:status` to confirm the fleet-wide state."
