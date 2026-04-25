---
name: minicrew:teardown
description: Stop and remove all minicrew workers on this machine (macOS or Linux Mint XFCE). Workers mark themselves offline as they exit.
---

You are tearing down every minicrew worker service on **this** machine. Workers on other machines are not affected. In-flight jobs will be requeued by their workers' graceful-shutdown handlers.

## Detect OS first

Run `uname -s` via Bash.
- If output is `Darwin`, follow the "On macOS" section below.
- If output is `Linux`, follow the "On Linux Mint XFCE" section below.
- Otherwise, stop and tell the user: minicrew supports macOS and Linux Mint XFCE only.

## On macOS

Follow these steps in order. Stop on any error and report it.

### 1. Locate the repo

Read `~/.claude/minicrew.json` for `repo_path`. If the file is missing, tell the user: "minicrew does not appear to be registered on this machine. If there are launchd services anyway, I can still remove them — tell me the absolute path to the minicrew checkout." Store as `REPO_PATH`.

### 2. List existing services

Run:

```
launchctl list | grep com.minicrew.worker
```

If there are zero entries, tell the user: "No minicrew workers are running on this machine. Nothing to tear down." and stop.

Otherwise, capture the list of service labels and their instance numbers for the confirmation prompt.

### 3. Confirm with the user

Show the list and ask explicitly:

> "This will stop N workers on this machine:
>
> <list of labels>
>
> Each worker will finish its current job or requeue it, mark itself offline in the database, and exit. Workers on other machines are not affected. Continue? (yes/no)"

Do not proceed until the user types `yes` (or equivalent affirmative). If they say no, stop and confirm nothing was changed.

### 4. Run the teardown script

Run:

```
bash "$REPO_PATH/teardown.sh"
```

`teardown.sh` dispatches to `python -m worker.platform uninstall-all`, which discovers every installed instance via filesystem glob (not a fixed `1..5` range) and removes each one, tolerating missing ones. If the script itself errors, report the stderr verbatim and stop.

### 5. Verify

Run again:

```
launchctl list | grep com.minicrew.worker
```

Confirm the output is empty. If any service remains, report which ones and suggest the user retry the teardown or manually bootout the stragglers via:

```
launchctl bootout gui/$(id -u)/<label>
```

### 6. Report

Tell the user:
- How many workers were torn down on this machine.
- Confirmation that nothing remains in `launchctl list`.
- Reminder: "Workers on other machines are unaffected. Use `/minicrew:status` to confirm the fleet-wide state."

## On Linux Mint XFCE

Follow these steps in order. Stop on any error and report it.

### 1. Locate the repo

Read `~/.claude/minicrew.json` for `repo_path`. If the file is missing, tell the user: "minicrew does not appear to be registered on this machine. If there are systemd user services anyway, I can still remove them — tell me the absolute path to the minicrew checkout." Store as `REPO_PATH`.

### 2. List existing services

Run:

```
systemctl --user list-units --all 'minicrew-worker-*'
```

If there are zero entries, tell the user: "No minicrew workers are running on this machine. Nothing to tear down." and stop.

Otherwise, capture the list of unit names and their instance numbers for the confirmation prompt.

### 3. Confirm with the user

Show the list and ask explicitly:

> "This will stop N workers on this machine:
>
> <list of units>
>
> Each worker will finish its current job or requeue it, mark itself offline in the database, and exit. Workers on other machines are not affected. Continue? (yes/no)"

Do not proceed until the user types `yes` (or equivalent affirmative). If they say no, stop and confirm nothing was changed.

### 4. Run the teardown script

Run:

```
bash "$REPO_PATH/teardown.sh"
```

`teardown.sh` dispatches to `python -m worker.platform uninstall-all`, which discovers every installed instance via filesystem glob (not a fixed `1..5` range) and removes each one, tolerating missing ones. If the script itself errors, report the stderr verbatim and stop.

**Never run `sudo systemctl --user` with minicrew units** — systemd would read `.env` as root and leak its contents into the root-visible journal.

### 5. Verify

Run again:

```
systemctl --user list-units --all 'minicrew-worker-*'
```

Confirm the output lists **no** minicrew units (the header `0 loaded units listed.` is the goal). If any unit remains, report which ones and suggest the user re-run the teardown or manually stop the straggler:

```
systemctl --user stop minicrew-worker-<N>.service
systemctl --user disable minicrew-worker-<N>.service
rm -f ~/.config/systemd/user/minicrew-worker-<N>.service
systemctl --user daemon-reload
```

### 6. Report

Tell the user:
- How many workers were torn down on this machine.
- Confirmation that `systemctl --user list-units --all 'minicrew-worker-*'` is empty.
- Reminder: "Workers on other machines are unaffected. Use `/minicrew:status` to confirm the fleet-wide state."
