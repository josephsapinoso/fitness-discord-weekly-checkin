# Fitness Discord Weekly Check-in Bot

A Discord bot that posts weekly prompts, collects structured check-ins via a modal
form, and logs everything to Google Sheets. Runs **serverless on Google Cloud Run's
free tier ($0/month)** as a Discord HTTP interactions endpoint — no always-on server.

> **Version 1 is frozen at tag [`v1.0.0`](../../releases/tag/v1.0.0)** (branch `v1`).
> [docs/V1_BASELINE.md](docs/V1_BASELINE.md) documents that baseline — the commands, the
> data model, the invariants not to break, and a **rollback runbook**. Read it before
> starting v2 work; the invariants section covers three failure modes that look fine in
> testing and break silently in production.

## Features

- `/checkin` — opens a modal with all 5 fields **plus an optional progress photo**; posts a formatted embed to the channel and saves to Sheets
- `/day1` — set (or replace) your "Day 1" baseline photo, used for before/after comparisons
- `/summary` — shows the last 5 check-ins in an embed with a link to the full sheet
- `/progress` — your personal weight chart with All-Time / 6-Month / 30-Day buttons, overall loss, and pace (lbs/week trend); private by default, `share:true` posts it to the channel
- `/history` — sends you the Google Sheet link privately
- `/collage` — a grid of your archived progress photos over time
- `/photo-replace` — swap the photo stored for a given date (with autocomplete)
- `/howto` — a pinnable explainer for how the weekly check-in works
- **Weekly reminder** — Cloud Scheduler posts a prompt every Monday at 9 AM UTC (configurable, no redeploy needed)

### Progress photos & before/after

Attach a photo to your `/checkin` (or set one with `/day1`) and the bot posts a
**Day 1 → Now** comparison to the channel so everyone can see your before/after.

- **One-time opt-in:** the first time you attach a photo the bot asks you to
  confirm (privately) before anything is shared publicly — your text check-in
  still posts as usual. Nothing is posted until you tap **Share it**.
- **Day 1 is automatic** the first time you share a photo; `/day1` sets or
  replaces it explicitly. Every later photo is composited against Day 1.
- **Privacy:** EXIF/GPS metadata is stripped from every photo, uploads are size-
  capped, and the private Google Sheet stores only Discord message references —
  never image bytes or expiring URLs. Photos live in your check-in channel, so
  keep that channel restricted to your group. There is no automated deletion:
  to remove someone's photos, delete the messages in Discord and clear their row
  in the **Photos** sheet tab.

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
6. Under **Bot Permissions**, select: `Send Messages`, `Embed Links`, `Use Slash Commands`, `Attach Files` (to upload progress photos), and `Read Message History` (to re-fetch the Day 1 photo for before/after comparisons)
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
├── sheets.py              # Google Sheets read/write helpers (check-ins + photo state)
├── charts.py              # /progress chart rendering + stats
├── images.py              # progress-photo normalize + before/after compositing
├── register_commands.py   # One-time slash-command registration
├── Dockerfile             # Cloud Run container
├── requirements.txt
├── env.yaml.example       # Template for Cloud Run env vars (copy to env.yaml)
├── .env.example           # Template for local development env vars
├── docs/GCP_DEPLOY.md     # Full deployment guide
├── docs/V1_BASELINE.md    # Frozen v1 surface + rollback runbook
├── scripts/redeploy.sh    # Deploy main (refuses a dirty or non-main tree)
├── scripts/check_deployed.sh  # Is production running origin/main?
└── scripts/rollback_to.sh     # Shift traffic back to a tagged release
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
