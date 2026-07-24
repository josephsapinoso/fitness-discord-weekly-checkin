# Fitness Discord Weekly Check-in Bot

A Discord bot that posts weekly prompts, collects structured check-ins via a modal
form, and logs everything to Google Sheets. Runs **serverless on Google Cloud Run's
free tier ($0/month)** as a Discord HTTP interactions endpoint — no always-on server.

## Features

- `/checkin` — opens a modal with all 5 fields; posts a formatted embed to the channel and saves to Sheets
- `/summary` — shows the last 5 check-ins in an embed with a link to the full sheet
- `/progress` — your personal weight chart with All-Time / 6-Month / 30-Day buttons, overall loss, and pace (lbs/week trend); private by default, `share:true` posts it to the channel
- `/history` — sends you the Google Sheet link privately
- **Weekly reminder** — Cloud Scheduler posts a prompt every Monday at 9 AM UTC (configurable, no redeploy needed)

## Architecture

```
Discord ──(signed HTTPS interaction)──▶ Cloud Run: POST /interactions
                                          │  ack within 3s (defer / open modal)
                                          ▼
                                        Cloud Tasks ──▶ POST /process
                                          │  Sheets read/write, chart render,
                                          │  edit the deferred response
                                          ▼
Cloud Scheduler ──(Mon 9:00 UTC)──▶ POST /reminder ──▶ check-in channel
```

There is no gateway connection and no resident process — Discord signs and POSTs
each interaction to Cloud Run, which must acknowledge within 3 seconds. Anything
slower than that (Google Sheets I/O, matplotlib chart rendering) is bounced
through Cloud Tasks back to `/process`, which then edits the deferred response.
One cosmetic side effect: the bot appears "offline" in the member list, but all
commands work.

---

## Setup

### 1. Create the Discord Bot

1. Go to https://discord.com/developers/applications → **New Application**
2. Under **Bot**, click **Add Bot**
3. Copy the **Token** → this is your `DISCORD_TOKEN`
4. On **General Information**, copy **Application ID** → `DISCORD_APPLICATION_ID`
   and **Public Key** → `DISCORD_PUBLIC_KEY`
5. Under **OAuth2 → URL Generator**, select scopes: `bot`, `applications.commands`
6. Under **Bot Permissions**, select: `Send Messages`, `Embed Links`, `Use Slash Commands`
7. Open the generated URL and invite the bot to your server
8. Enable **Developer Mode** in Discord (User Settings → Advanced) then right-click your check-in channel → **Copy ID** → this is `CHECKIN_CHANNEL_ID`

### 2. Create a Google Sheet

1. Go to https://sheets.google.com and create a new spreadsheet
2. Copy the ID from the URL: `docs.google.com/spreadsheets/d/<ID>/edit` → this is `GOOGLE_SHEET_ID`

### 3. Create a Google Service Account

1. Go to https://console.cloud.google.com → create a new project (or use an existing one)
2. Enable the **Google Sheets API** and **Google Drive API**
3. Go to **IAM & Admin → Service Accounts** → **Create Service Account**
4. Download the JSON key file
5. Open the JSON file and copy the entire contents → this is `GOOGLE_CREDENTIALS_JSON`
6. Copy the `client_email` from the JSON, then **share your Google Sheet** with that email (Editor access)

### 4. Deploy to Cloud Run (free)

Follow **[docs/GCP_DEPLOY.md](docs/GCP_DEPLOY.md)** — it covers enabling APIs,
the Cloud Tasks queue, `gcloud run deploy`, pointing Discord's Interactions
Endpoint URL at the service, and the Cloud Scheduler reminder job.

### 5. Run locally (for testing)

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in values
python app.py          # serves on :8080
```

To receive real Discord interactions locally you'd need a public HTTPS tunnel
(e.g. `cloudflared tunnel`) pointed at `:8080` and set as the Interactions
Endpoint URL — usually it's easier to just deploy.

---

## File Structure

```
├── app.py                 # Flask interactions server — commands, modal, embeds
├── discord_api.py         # Discord REST helpers (webhook edits, channel posts)
├── tasks_queue.py         # Cloud Tasks enqueue helper (deferred work)
├── sheets.py              # Google Sheets read/write helpers
├── charts.py              # /progress chart rendering + stats
├── register_commands.py   # One-time slash-command registration
├── Dockerfile             # Cloud Run container
├── requirements.txt
├── env.yaml.example       # Template for Cloud Run env vars (copy to env.yaml)
├── .env.example           # Template for local development env vars
└── docs/GCP_DEPLOY.md     # Full deployment guide
```

## Customization

| What | Where |
|------|-------|
| Change reminder day/time | `gcloud scheduler jobs update` — see docs/GCP_DEPLOY.md step 6 |
| Change sheet tab name | `GOOGLE_SHEET_TAB` env var |
| Add/remove check-in fields | `_checkin_modal()` + `_build_checkin_embed()` in `app.py`, and `HEADERS` + `log_checkin()` in `sheets.py` |
| Change embed colors/emoji | `_build_checkin_embed()` in `app.py` |

## History

Earlier versions ran as a discord.py gateway bot on Railway (see `bot.py` in git
history). Rewritten for Cloud Run when Railway's free trial ended.
