# GitHub App setup

End-to-end walkthrough for provisioning the GitHub App that minicrew workers
use to clone (and optionally push) private repositories during ad_hoc and
handoff jobs.

## For LLMs

**What this covers:** registering a GitHub App, generating its private key,
installing it on an org or user account, locating the `app_id` and
`installation_id`, wiring those values into the worker's `.env` and
`worker-config/config.yaml`, and verifying with `python -m worker --preflight`.
Also: rotation, multi-org notes, and the most common 401/403/404 failures.

**Invariants:** the App needs `Contents: Read & write` and nothing else for v1.
`Metadata: Read-only` is auto-required by GitHub. Webhooks MUST be disabled —
minicrew never receives them. The PEM private key goes in `.env` (never the
launchd plist, never the YAML); both raw multi-line PEM and single-line
base64-encoded PEM are accepted by `worker/integrations/github_app.py:_load_pem`.

## Why a GitHub App

- **Single shared identity for the worker fleet.** One App can clone and push
  to every repo it is installed on — no per-worker PAT.
- **Short-lived install tokens.** The worker mints a fresh installation token
  (1h TTL) per claim cycle. There is no long-lived credential to rotate on the
  worker host beyond the App's private key.
- **Multi-repo, multi-tenant.** One App + one keypair can be installed on many
  orgs and many repos. Permissions are gated per installation.

## Step 1: Register the App

1. Go to <https://github.com/settings/apps/new> for a personal account, or
   open your org → **Settings** → **Developer settings** → **GitHub Apps** →
   **New GitHub App**.
2. Set:
   - **GitHub App name:** `minicrew-worker-<your-org>`
   - **Homepage URL:** any valid URL (your org's public URL works).
   - **Webhook:** uncheck **Active**. Webhooks are not used.
   - **Repository permissions:**
     - **Contents:** Read & write — required for clone + push of result branches.
     - **Metadata:** Read-only (auto-required by GitHub).
     - No other permissions are needed for v1.
   - **Where can this GitHub App be installed?:** "Only on this account" if
     personal, or org-only if you registered under an org.
3. Click **Create GitHub App**.

## Step 2: Generate a private key

On the App's settings page, scroll to **Private keys** → **Generate a private
key**. A `.pem` file downloads. Treat the contents as a secret — they
authenticate the App in JWT signing.

## Step 3: Install the App on your org/repos

Top of the App page → **Install App** → choose your account or org → choose
**All repositories** OR explicitly select the repos minicrew needs to clone
and push. Click **Install**.

## Step 4: Find the `installation_id`

After install, GitHub redirects to the installation settings page. The URL is:

- Personal: `https://github.com/settings/installations/<NUMBER>`
- Org: `https://github.com/organizations/<ORG>/settings/installations/<NUMBER>`

The `<NUMBER>` is the `installation_id` you wire into the worker.

## Step 5: Find the `app_id`

On the App's main settings page (where you generated the private key), the
**App ID** is shown near the top under the App name, e.g. `App ID: 12345`.

## Step 6: Configure the minicrew worker `.env`

Add to the worker host's `.env` (the one referenced by the launchd plist /
systemd unit; see `SETUP.md`). The `.env` file is loaded at startup by the
worker process.

Multi-line PEM form (most common):

```
MINICREW_GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----
<paste full PEM contents here>
-----END RSA PRIVATE KEY-----"
MINICREW_GITHUB_INSTALLATION_ID=12345678
```

Or base64-encoded PEM (single line — easier for env-loaders that mishandle
multi-line values):

```
MINICREW_GITHUB_APP_PRIVATE_KEY=LS0tLS1CRUdJTiBSU0EgUFJJVkFURSBLRVktLS0tLQo...
MINICREW_GITHUB_INSTALLATION_ID=12345678
```

`_load_pem` in `worker/integrations/github_app.py` accepts both formats — it
detects the `-----BEGIN` header and falls back to base64 decode otherwise.

`chmod 600 .env`. Preflight refuses to start otherwise.

## Step 7: Configure `worker-config/config.yaml`

Add to the `dispatch:` block:

```yaml
dispatch:
  github_app:
    app_id: "12345"
    private_key_env: "MINICREW_GITHUB_APP_PRIVATE_KEY"
    installation_id_env: "MINICREW_GITHUB_INSTALLATION_ID"
    clone_timeout_seconds: 300
```

`app_id` is a string. `private_key_env` and `installation_id_env` are the
**names** of the env vars holding the PEM and installation id (not the values).
Indirection via env-var-name keeps the YAML safe to commit.

## Step 8: Verify

Run preflight:

```
python -m worker --preflight
```

This calls `dispatch_preflight_common`, which mints a test installation token
end-to-end and probes the App's `permissions.contents` scope. On success you
see `ok`. On failure you see one of:

- `GitHub App token mint failed: ...` — see Troubleshooting below.
- `GitHub App lacks contents:write permission (got: 'read')` — re-do Step 1's
  permission grant and re-accept on each installation.

## Troubleshooting

### `401 Bad credentials`

Wrong private key OR wrong `app_id`. The PEM and the App ID must come from the
same App. Re-download the PEM (Step 2) and re-confirm the App ID (Step 5).

### `404 Not Found` on installation token mint

Wrong `installation_id`. Re-open the App's Install page (Step 4) and copy the
number from the URL. If you reinstalled the App, the id will have changed.

### Push 403 from worker on a real job

The App lacks `contents:write` permission OR the App is not installed on the
target repository. Re-check Step 1 (permissions) and Step 3 (installation
includes target repo). After changing permissions, every installation must
**accept** the new permission set on the installation page — GitHub does not
auto-grant.

### `GitHub App lacks contents:write permission`

Surfaced by preflight Step 8. Edit the App's permissions on github.com, set
**Contents: Read & write**, then visit each installation and accept the
updated permission set. Re-run preflight.

### Clock skew

The JWT is issued with a 60-second validity window. If the worker host's clock
drifts more than ~30s from NTP, token mint fails with a vague auth error. Run
`date` on the worker host and confirm it is in sync.

## Rotating the private key

1. App's settings page → **Private keys** → **Generate a private key**. Both
   the old and new keys are valid until you delete the old one.
2. Update `MINICREW_GITHUB_APP_PRIVATE_KEY` in `.env` on every worker host.
3. Restart workers: `launchctl kickstart -k gui/$UID/com.minicrew.worker.<N>`
   (Mac) or `systemctl --user restart minicrew-worker-<N>.service` (Linux).
4. Verify with `python -m worker --preflight` on each host.
5. Return to the App's settings page and **delete the old private key**.

## Multi-org setup

Register a separate App per org (the App lives under a single account/org).
Each worker host configures one App via env. To run the same fleet against
multiple orgs, run separate worker instances each with its own
`MINICREW_GITHUB_APP_PRIVATE_KEY` / `MINICREW_GITHUB_INSTALLATION_ID` and its
own `worker-config/config.yaml` `github_app` block. Full multi-tenant support
(routing jobs to per-tenant Apps within one worker) is out of scope for v1.
