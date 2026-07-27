# Fitness Check-in Bot — Setup Guide

This document is a complete step-by-step recap of how the bot is built and deployed from scratch. Use it as a reference if you ever need to re-create the setup or deploy to a new environment.

The bot runs as a **Discord HTTP interactions endpoint on Google Cloud Run** — a stateless
web service Discord calls directly, not an always-on process holding a gateway connection.
See [PLATFORM_REFERENCE.md](PLATFORM_REFERENCE.md) for why, and [GCP_DEPLOY.md](GCP_DEPLOY.md)
for the deploy commands themselves.

---

## Overview

The bot requires these external accounts/services to be configured before it can run:

| Service | Purpose |
|---------|---------|
| Discord Developer Portal | Create the bot identity; get its token, application ID, and public key |
| Google Cloud Platform | Host the Cloud Run service, Cloud Tasks queue, Cloud Scheduler job, and the Sheets service account |
| Google Sheets | Store all check-in data and per-user photo state |
| GitHub | Version control and CI. Deploys are run manually — pushing does **not** deploy |

---

## Prerequisites

- Python 3.11–3.13 installed locally (the deployed container uses 3.12; the pinned
  `numpy`/`matplotlib` have no 3.14 wheels yet)
- Git installed and configured
- The [gcloud CLI](https://cloud.google.com/sdk/docs/install) installed and logged in
- A GitHub account
- A Google account (Gmail)
- A Discord account with admin access to your server

---

## Step 1 — Create the Discord Bot Application

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) and log in.
2. Click **New Application** → name it `Fitness Check-in Bot` → agree to terms → **Create**.
3. In the left sidebar, click **Bot**.
4. Under **Token**, click **Reset Token** → confirm with your 2FA code → **Copy** the token. Save this as `DISCORD_TOKEN`.
5. Go to **General Information** and copy two more values:
   - **Application ID** → save as `DISCORD_APPLICATION_ID`
   - **Public Key** → save as `DISCORD_PUBLIC_KEY`

   The public key is what the service uses to verify incoming requests really come from
   Discord (`_verify_signature()` in `app.py`). Without it, nothing works.
6. Under **Privileged Gateway Intents** — none are needed. This bot never opens a gateway
   connection, so intents are irrelevant to it.

### Invite the Bot to Your Server

7. Navigate directly to the following URL (replace `CLIENT_ID` with the **Application ID**):

```
https://discord.com/oauth2/authorize?client_id=CLIENT_ID&scope=bot+applications.commands&permissions=2147600384
```

That bitmask grants Send Messages (2048), Embed Links (16384), Attach Files (32768), Read
Message History (65536), and Use Application Commands (2147483648).

**Attach Files** is what the progress-photo feature added to the older `2147567616` value —
it's needed to upload photos, and **Read Message History** to re-fetch the stored Day 1
message for before/after comparisons. Without both, the text check-in still posts but
photos silently fail.

8. Select your server from the dropdown → **Authorize**.

### Get the Channel ID

9. In Discord, enable **Developer Mode**: User Settings → Advanced → Developer Mode.
10. Right-click the check-in channel → **Copy Channel ID**. Save this as `CHECKIN_CHANNEL_ID`.

> Progress photos are posted into this channel and stay there permanently — the sheet only
> stores message references, never image bytes. Keep the channel restricted to your group.

---

## Step 2 — Create the Google Sheet

1. Go to [sheets.new](https://sheets.new) to create a blank spreadsheet.
2. Rename it to something descriptive (e.g., `Fitness Check-in Bot`).
3. Copy the spreadsheet ID from the URL:
   ```
   docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit
   ```
   Save this as `GOOGLE_SHEET_ID`.

You don't need to create tabs or header rows by hand. `sheets.py` creates both tabs on
first use:

- **`Check-ins`** — one append-only row per check-in
- **`Photos`** — one row per user tracking photo consent, the Day 1 message reference, and
  any pending (not-yet-consented) photo URL

---

## Step 3 — Create a Google Cloud Project & Service Account

### Create the Project

1. Go to [console.cloud.google.com/projectcreate](https://console.cloud.google.com/projectcreate).
2. Name it `fitness-checkin-bot` → **Create**.

Use this same project for the Cloud Run deployment in Step 6 — the deploy guide assumes it.

### Enable APIs

3. Navigate to **APIs & Services → Library**.
4. Search for and enable **Google Sheets API**.
5. Search for and enable **Google Drive API**.

(The Cloud Run / Tasks / Scheduler / Build APIs get enabled in Step 6 by one gcloud command.)

### Create the Service Account

6. Go to **IAM & Admin → Service Accounts → Create Service Account**.
7. Name it `fitness-checkin-sheets` → **Create and Continue** → **Done**.

### Download Credentials

8. Click the service account → **Keys** tab → **Add Key → Create new key → JSON → Create**.
9. A `.json` file downloads to your computer. Its full contents become `GOOGLE_CREDENTIALS_JSON`.

### Share the Sheet

10. Open your Google Sheet → **Share**.
11. Paste the service account email (visible in the JSON file as `client_email`):
    ```
    fitness-checkin-sheets@<project-id>.iam.gserviceaccount.com
    ```
12. Set permission to **Editor** → **Send**.

---

## Step 4 — Set Up the GitHub Repository

1. Create a private GitHub repo named `fitness-discord-weekly-checkin`.
2. Push the project files:
   ```bash
   git init -b main
   git add .
   git commit -m "Initial commit: fitness check-in bot"
   git remote add origin https://github.com/<username>/fitness-discord-weekly-checkin.git
   git push -u origin main
   ```

> **Important:** `.gitignore` already excludes `.env`, `env.yaml`, and `credentials.json`.
> Never commit secrets, and don't embed a personal access token in the remote URL — use
> the `gh` CLI or a credential helper instead.

Pushing runs the test suite via GitHub Actions (`.github/workflows/tests.yml`) but does
**not** deploy anything. Deploys are explicit `gcloud run deploy` commands (Step 6).

---

## Step 5 — Create the Local `.env` File

`.env` is only used for running and testing locally, and by `register_commands.py`. The
deployed service reads its configuration from `env.yaml` instead (Step 6).

```bash
cp .env.example .env
```

Fill in the values you collected above. See the reference table at the bottom of this
document for what each one does.

---

## Step 6 — Deploy to Cloud Run

The full first-time procedure lives in **[GCP_DEPLOY.md](GCP_DEPLOY.md)**. In outline:

1. Enable the Cloud Run, Build, Artifact Registry, Tasks, and Scheduler APIs.
2. Create the `discord-followups` Cloud Tasks queue.
3. `cp env.yaml.example env.yaml` and fill it in (same values as `.env`, YAML-quoted).
4. `gcloud run deploy fitness-checkin-bot --source . --env-vars-file env.yaml ...`
5. Set the **Interactions Endpoint URL** in the Discord Developer Portal to
   `https://YOUR-SERVICE-URL/interactions`. Discord validates it before saving.
6. Register the slash commands: `python register_commands.py` (must print **8**).
7. Create the Cloud Scheduler job that POSTs to `/reminder` every Monday.

Steps 5 and 7 have no equivalent in a gateway bot — Discord needs to know where to send
interactions, and the weekly reminder is an external cron job rather than a loop inside a
resident process.

For **redeploying an already-live service** (e.g. shipping a code change), skip the
one-time infrastructure and use [REDEPLOY_CHECKLIST.md](REDEPLOY_CHECKLIST.md) or
`./scripts/redeploy.sh`.

---

## Step 7 — Verify the Bot is Working

1. `curl https://YOUR-SERVICE-URL/` → should return `ok`.
2. In Discord, type `/history` — fastest check; returns the sheet link with no Sheets API call.
3. Type `/checkin` — a modal with 5 text fields plus an optional **Progress photo** upload
   should appear, with weights prefilled if you've checked in before.
4. Fill it in and submit — a formatted embed should post to the channel.
5. Check your Google Sheet — a new row should appear under the `Check-ins` tab.
6. Type `/progress` — after two or more check-ins, a chart renders with view buttons.
7. Attach a photo to a `/checkin`, or run `/day1`. The first time, the bot privately asks
   you to confirm public sharing; tapping **Share it 📸** posts your Day 1 photo. A later
   check-in photo posts a Day 1 → Now composite. A `Photos` tab appears in the sheet.

> The bot appears **offline** in the Discord member list. That's expected and cosmetic —
> with no gateway connection Discord never sees a presence for it. Commands work fine.

---

## Environment Variable Reference

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `DISCORD_TOKEN` | yes | Bot token from the Developer Portal | `MTUx...` |
| `DISCORD_APPLICATION_ID` | yes | Application ID; used to build interaction webhook URLs | `123456789012345678` |
| `DISCORD_PUBLIC_KEY` | yes | Public key; verifies Ed25519 request signatures | `abcdef0123...` |
| `CHECKIN_CHANNEL_ID` | yes | Channel the check-in embeds, photos, and reminders post to | `1457444714528772176` |
| `GOOGLE_SHEET_ID` | yes | Spreadsheet ID from the Google Sheets URL | `1Gva...` |
| `GOOGLE_SHEET_TAB` | no | Check-ins tab name (default `Check-ins`) | `Check-ins` |
| `GOOGLE_PHOTOS_TAB` | no | Photo-state tab name, auto-created (default `Photos`) | `Photos` |
| `GOOGLE_CREDENTIALS_JSON` | yes\* | Full service account JSON, single line | `{"type":"service_account",...}` |
| `GOOGLE_CREDENTIALS_FILE` | no | Path to a credentials JSON file instead of the env var (default `credentials.json`) | `credentials.json` |
| `TASK_SECRET` | yes | Shared secret guarding `/process` and `/reminder` | `openssl rand -hex 32` output |
| `MAX_IMAGE_BYTES` | no | Max inbound photo size before decoding (default 10 MiB) | `10485760` |
| `TASKS_LOCATION` | no | Cloud Tasks queue region (default `us-west1`) | `us-west1` |
| `TASKS_QUEUE` | no | Cloud Tasks queue name (default `discord-followups`) | `discord-followups` |
| `GOOGLE_CLOUD_PROJECT` | no | GCP project; auto-detected on Cloud Run | `fitness-checkin-bot` |
| `SELF_URL` | no | Base URL for Cloud Tasks callbacks; falls back to the request host | `https://...run.app` |
| `PREFILL_TIMEOUT_S` | no | Seconds to wait on the `/checkin` prefill read (default `1.4`) | `2.0` |
| `PORT` | no | Set by Cloud Run; local dev defaults to `8080` | `8080` |

\* `GOOGLE_CREDENTIALS_JSON` is required unless you supply `GOOGLE_CREDENTIALS_FILE` or
rely on Application Default Credentials.

The reminder schedule is **not** an environment variable — it lives in the Cloud Scheduler
job's cron expression. See [GCP_DEPLOY.md](GCP_DEPLOY.md) step 6.
