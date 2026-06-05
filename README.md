# Fitness Discord Weekly Check-in Bot

A Discord bot that posts weekly prompts, collects structured check-ins via a modal form, and logs everything to Google Sheets.

## Features

- `/checkin` — opens a modal with all 5 fields; posts a formatted embed to the channel and saves to Sheets
- `/summary` — shows the last 5 check-ins in an embed with a link to the full sheet
- `/history` — sends you the Google Sheet link privately
- **Weekly reminder** — posts a prompt every Monday at 9 AM UTC (configurable)

---

## Setup

### 1. Create the Discord Bot

1. Go to https://discord.com/developers/applications → **New Application**
2. Under **Bot**, click **Add Bot**
3. Copy the **Token** → this is your `DISCORD_TOKEN`
4. Under **OAuth2 → URL Generator**, select scopes: `bot`, `applications.commands`
5. Under **Bot Permissions**, select: `Send Messages`, `Embed Links`, `Use Slash Commands`
6. Open the generated URL and invite the bot to your server
7. Enable **Developer Mode** in Discord (User Settings → Advanced) then right-click your check-in channel → **Copy ID** → this is `CHECKIN_CHANNEL_ID`

### 2. Create a Google Sheet

1. Go to https://sheets.google.com and create a new spreadsheet
2. Copy the ID from the URL: `docs.google.com/spreadsheets/d/<ID>/edit`  → this is `GOOGLE_SHEET_ID`

### 3. Create a Google Service Account

1. Go to https://console.cloud.google.com → create a new project (or use an existing one)
2. Enable the **Google Sheets API** and **Google Drive API**
3. Go to **IAM & Admin → Service Accounts** → **Create Service Account**
4. Download the JSON key file
5. Open the JSON file and copy the entire contents → this is `GOOGLE_CREDENTIALS_JSON`
6. Copy the `client_email` from the JSON, then **share your Google Sheet** with that email (Editor access)

### 4. Configure Environment Variables

Copy `.env.example` to `.env` and fill in all values:

```
cp .env.example .env
```

For local development, fill in `.env`. For Railway, add each variable in the Railway dashboard.

### 5. Run Locally (for testing)

```bash
pip install -r requirements.txt
python bot.py
```

### 6. Deploy to Railway (free hosting)

1. Push this folder to a GitHub repo
2. Go to https://railway.app → **New Project → Deploy from GitHub repo**
3. Select your repo
4. In the Railway dashboard, go to **Variables** and add all env vars from `.env.example`
5. Railway will auto-detect the `Procfile` and start the bot as a worker

> **Free tier note:** Railway's free Starter plan includes $5/month credit. A simple Discord bot typically uses well under $1/month.

---

## File Structure

```
├── bot.py            # Main bot — commands, scheduler, embeds
├── sheets.py         # Google Sheets read/write helpers
├── requirements.txt
├── Procfile          # Tells Railway how to run the bot
├── railway.toml      # Railway config
└── .env.example      # Template for environment variables
```

---

## Customization

| What | Where |
|------|-------|
| Change reminder day/time | `REMINDER_WEEKDAY`, `REMINDER_HOUR`, `REMINDER_MINUTE` env vars |
| Change sheet tab name | `GOOGLE_SHEET_TAB` env var |
| Add/remove check-in fields | Edit `CheckinModal` in `bot.py` and `HEADERS` + `log_checkin()` in `sheets.py` |
| Change embed colors/emoji | Edit `_build_checkin_embed()` in `bot.py` |
