# Fitness Check-in Bot — Setup Guide

This document is a complete step-by-step recap of how the bot was built and deployed from scratch. Use it as a reference if you ever need to re-create the setup or deploy to a new environment.

---

## Overview

The bot requires four external accounts/services to be configured before it can run:

| Service | Purpose |
|---------|---------|
| Discord Developer Portal | Create the bot identity and get its token |
| Google Cloud Platform | Host the service account that writes to Sheets |
| Google Sheets | Store all check-in data |
| Railway | Host and run the bot 24/7 for free |

---

## Prerequisites

- Python 3.11+ installed locally
- Git installed and configured
- A GitHub account
- A Google account (Gmail)
- A Discord account with admin access to your server

---

## Step 1 — Create the Discord Bot Application

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) and log in.
2. Click **New Application** → name it `Fitness Check-in Bot` → agree to terms → **Create**.
3. In the left sidebar, click **Bot**.
4. Under **Token**, click **Reset Token** → confirm with your 2FA code → **Copy** the token. Save this as `DISCORD_TOKEN`.
5. Still under **Bot**, scroll to **Privileged Gateway Intents** — no extra intents are needed for this bot.

### Invite the Bot to Your Server

6. Navigate directly to the following URL (replace `CLIENT_ID` with the **Application ID** from the General Information page):

```
https://discord.com/oauth2/authorize?client_id=CLIENT_ID&scope=bot+applications.commands&permissions=2147567616
```

7. Select your server from the dropdown → **Authorize**.

### Get the Channel ID

8. In Discord, enable **Developer Mode**: User Settings → Advanced → Developer Mode.
9. Right-click the check-in channel → **Copy Channel ID**. Save this as `CHECKIN_CHANNEL_ID`.

---

## Step 2 — Create the Google Sheet

1. Go to [sheets.new](https://sheets.new) to create a blank spreadsheet.
2. Rename it to something descriptive (e.g., `Fitness Check-in Bot`).
3. Copy the spreadsheet ID from the URL:
   ```
   docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit
   ```
   Save this as `GOOGLE_SHEET_ID`.

---

## Step 3 — Create a Google Cloud Project & Service Account

### Create the Project

1. Go to [console.cloud.google.com/projectcreate](https://console.cloud.google.com/projectcreate).
2. Name it `fitness-checkin-bot` → **Create**.

### Enable APIs

3. Navigate to **APIs & Services → Library**.
4. Search for and enable **Google Sheets API**.
5. Search for and enable **Google Drive API**.

### Create the Service Account

6. Go to **IAM & Admin → Service Accounts → Create Service Account**.
7. Name it `fitness-checkin-sheets` → **Create and Continue** → **Done**.

### Download Credentials

8. Click the service account → **Keys** tab → **Add Key → Create new key → JSON → Create**.
9. A `.json` file downloads to your computer. This file contains `GOOGLE_CREDENTIALS_JSON`.

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

> **Important:** The `.gitignore` already excludes `.env` and `credentials.json`. Never commit secrets to GitHub.

---

## Step 5 — Create the `.env` File

Run the helper script from the project folder (requires the credentials JSON to be in `~/Downloads`):

```bash
python create_env.py
```

This generates a `.env` file with all required variables:

```
DISCORD_TOKEN=<your-bot-token>
CHECKIN_CHANNEL_ID=<your-channel-id>
GOOGLE_SHEET_ID=<your-spreadsheet-id>
GOOGLE_SHEET_TAB=Check-ins
GOOGLE_CREDENTIALS_JSON=<full-json-string>
REMINDER_WEEKDAY=0
REMINDER_HOUR=9
REMINDER_MINUTE=0
```

---

## Step 6 — Deploy to Railway

1. Go to [railway.app](https://railway.app) → **Log in with GitHub**.
2. Click **New Project → GitHub Repository**.
3. If no repos appear, click **Configure GitHub App** → grant access to the repo → refresh.
4. Select `fitness-discord-weekly-checkin`.
5. Railway auto-detects the `Procfile` and starts a build.
6. Go to the service → **Variables → Raw Editor**.
7. Paste all contents from your `.env` file (without comment lines) → **Update Variables → Deploy**.
8. Wait ~1 minute. The status should change to **ACTIVE — Online**.

---

## Step 7 — Verify the Bot is Working

1. Open Discord and go to your check-in channel.
2. Type `/checkin` — a modal with 5 fields plus an optional progress-photo upload should appear.
3. Fill it in and submit — a formatted embed should post to the channel.
4. Check your Google Sheet — a new row should appear under the `Check-ins` tab.
5. (Optional) Attach a photo when checking in, or run `/day1` — the first time,
   the bot asks you to confirm public sharing; after that it posts your Day 1
   photo and, on later check-ins, a Day 1 → Now before/after image. Per-user
   photo state is tracked in the `Photos` tab.

---

## Environment Variable Reference

| Variable | Description | Example |
|----------|-------------|---------|
| `DISCORD_TOKEN` | Bot token from Discord Developer Portal | `MTUx...` |
| `CHECKIN_CHANNEL_ID` | Discord channel ID for the check-in channel | `1457444714528772176` |
| `GOOGLE_SHEET_ID` | Spreadsheet ID from the Google Sheets URL | `1Gva...` |
| `GOOGLE_SHEET_TAB` | Tab name inside the spreadsheet | `Check-ins` |
| `GOOGLE_PHOTOS_TAB` | Tab name for per-user progress-photo state (auto-created) | `Photos` |
| `GOOGLE_CREDENTIALS_JSON` | Full service account credentials JSON (single line) | `{"type":"service_account",...}` |
| `MAX_IMAGE_BYTES` | Max inbound progress-photo size (bytes) | `10485760` |
| `REMINDER_WEEKDAY` | Day of week for weekly prompt (0=Mon, 6=Sun) | `0` |
| `REMINDER_HOUR` | UTC hour for the weekly reminder | `9` |
| `REMINDER_MINUTE` | UTC minute for the weekly reminder | `0` |
