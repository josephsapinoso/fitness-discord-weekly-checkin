# Fitness Check-in Bot — Operations & Maintenance Guide

This document covers how to manage the bot after it's live: updating code, monitoring health, changing settings, extending functionality, troubleshooting, and security practices.

---

## Day-to-Day Operations

### How Deployments Work

Every `git push` to the `main` branch on GitHub triggers an automatic Railway redeploy. The workflow is:

```
Edit code locally → git add/commit/push → Railway detects push
→ Nixpacks builds a new image → New container replaces old one
→ Bot reconnects to Discord (typically <60 seconds downtime)
```

### Checking Bot Status

1. Open [railway.app](https://railway.app) → your project → the `worker` service.
2. **Deployments tab:** Shows build and runtime history. A green `ACTIVE` badge means the bot is running.
3. **Console tab:** Live terminal output from the running bot. Shows connection events, check-in logs, and any errors.
4. **Metrics tab:** CPU and memory usage over time.

### Viewing Logs

In Railway: **Deployments tab → View logs** on any deployment.

Alternatively, the **Console tab** shows live output. Log lines look like:

```
2026-06-04 10:00:01 INFO Logged in as Fitness Check-in Bot#4138 (id=1512317224180781119)
2026-06-04 10:00:01 INFO Commands synced, reminder task started.
2026-06-04 09:00:00 INFO Weekly reminder posted to channel 1457444714528772176.
```

---

## Changing the Reminder Schedule

The reminder fires based on three environment variables. To change the day or time:

1. In Railway: **Variables tab** → find and edit the relevant variable(s).
2. Click **Deploy** to apply.

| Variable | Default | Notes |
|----------|---------|-------|
| `REMINDER_WEEKDAY` | `0` (Monday) | 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun |
| `REMINDER_HOUR` | `9` | UTC hour (0–23). Convert from your timezone: EST = UTC-5, PST = UTC-8 |
| `REMINDER_MINUTE` | `0` | Minute within the hour |

**Example — Move to Friday at 5 PM EST (10 PM UTC):**
```
REMINDER_WEEKDAY=4
REMINDER_HOUR=22
REMINDER_MINUTE=0
```

---

## Adding or Removing Check-in Fields

The check-in fields are defined in two places. Both must be updated together.

### 1. Update the Modal (`bot.py`)

Each field is a `discord.ui.TextInput` inside `CheckinModal`. Add, remove, or rename fields here:

```python
class CheckinModal(discord.ui.Modal, title="Weekly Fitness Check-in 💪"):
    current_weight = discord.ui.TextInput(label="Current Weight", ...)
    last_week_weight = discord.ui.TextInput(label="Last Week's Weight", ...)
    # Add a new field:
    body_fat = discord.ui.TextInput(
        label="Body Fat %",
        placeholder="e.g. 18%",
        required=False,   # optional field
        max_length=20,
    )
```

Also update `on_submit` to pass the new field to `sheets.log_checkin()`.

### 2. Update the Sheet Schema (`sheets.py`)

Add the new column name to `HEADERS` and add the value to `log_checkin()`:

```python
HEADERS = [
    "Timestamp", "User ID", "Username",
    "Current Weight", "Last Week Weight", "Starting Weight",
    "Body Fat %",          # ← new column
    "Proud Of", "Can Work On",
]

def log_checkin(..., body_fat: str) -> None:
    ws.append_row([ts, str(user_id), username, current_weight,
                   last_week_weight, starting_weight,
                   body_fat,         # ← new value
                   proud_of, can_work_on], ...)
```

> **Note:** Existing rows in the sheet won't have a value for new columns — they'll just be blank. That's fine; the header row drives the schema.

---

## Changing the Check-in Channel

1. In Discord (with Developer Mode on), right-click the new channel → **Copy Channel ID**.
2. In Railway: **Variables tab** → update `CHECKIN_CHANNEL_ID` → **Deploy**.

---

## Troubleshooting

### Bot is offline / not responding to commands

1. Check Railway → **Deployments** — is the status `ACTIVE`? If it shows `CRASHED`, click **View logs** to see the error.
2. Common causes:
   - `DISCORD_TOKEN` is invalid or expired → reset the token in the Discord Developer Portal and update the Railway variable.
   - A Python syntax error was deployed → check the build logs for a traceback.

### `/checkin` command doesn't appear in Discord

Slash commands are synced globally when the bot starts. This can take up to 1 hour to propagate across all Discord servers after a fresh deployment. If it's been more than an hour:
1. Check Railway logs for `Commands synced` — if missing, the bot may have crashed before syncing.
2. Kick and re-invite the bot to force a permission refresh.

### Check-in submits but nothing appears in Google Sheets

1. Check Railway logs for `Failed to save check-in:` errors.
2. Common causes:
   - `GOOGLE_CREDENTIALS_JSON` is malformed — make sure it's a single line of valid JSON with no surrounding quotes.
   - The service account email no longer has Editor access to the sheet — re-share the sheet.
   - The Google Sheets or Drive API was disabled in GCP — re-enable at [console.cloud.google.com/apis](https://console.cloud.google.com/apis).

### Bot posts check-in embed but not to the right channel

Verify `CHECKIN_CHANNEL_ID` matches the channel you want. Remember the ID is a large integer, not the channel name.

### Weekly reminder didn't fire

1. Confirm `REMINDER_WEEKDAY`, `REMINDER_HOUR`, and `REMINDER_MINUTE` are set correctly (all in UTC).
2. The reminder fires within a 5-minute window after the target time. If the bot restarted during that window, it will miss it and fire next week.
3. Check Railway logs around the expected time for `Weekly reminder posted`.

---

## Updating the Bot

```bash
# Make your changes locally, then:
git add .
git commit -m "Description of change"
git push origin main
# Railway auto-deploys within ~60 seconds
```

If you want to test locally before pushing:

```bash
pip install -r requirements.txt
python bot.py
```

The bot will connect using your `.env` file. Note that local and Railway deployments share the same Discord token and channel, so commands will work from either — don't run both simultaneously.

---

## Cost & Usage Monitoring

Railway's **free tier** gives $5 of credit per month. To check current usage:

1. Railway → top-right avatar → **Account Settings → Billing**.
2. A simple Python worker typically costs $0.50–$1.00/month — well within the free limit.

If you ever approach the limit, the **Metrics tab** in the service shows memory and CPU usage. The bot is intentionally lightweight: it idles between events and uses minimal resources.

---

## Security Best Practices

### Secrets Management

| Secret | Where it lives | What to do if compromised |
|--------|----------------|--------------------------|
| `DISCORD_TOKEN` | Railway env var | Reset in Discord Developer Portal → update Railway var |
| `GOOGLE_CREDENTIALS_JSON` | Railway env var | Delete the key in GCP IAM → create a new one → update Railway var |
| GitHub PAT (used during setup) | Should be revoked | github.com/settings/tokens → Revoke |

**Never:**
- Commit `.env` or `credentials.json` to Git
- Share your bot token publicly
- Leave old/compromised credentials active

### Limiting Bot Permissions

The bot is invited with only the permissions it needs:
- Send Messages
- Embed Links
- Attach Files (upload progress photos)
- Read Message History (re-fetch the Day 1 photo for before/after)
- Use Application Commands

Avoid granting Administrator permission — it's unnecessary and increases risk.

### Rotating Credentials

It's good practice to rotate the bot token and service account key every 6–12 months:

1. **Discord token:** Reset Token in the Developer Portal → update `DISCORD_TOKEN` in Railway.
2. **Google service account key:** GCP → Service Accounts → Keys → Add Key → delete the old key → update `GOOGLE_CREDENTIALS_JSON` in Railway.

---

## Extending the Bot

### Ideas for Future Features

**Progress tracking commands:**
- `/progress @user` — Show a user's weight trend over time pulled from the sheet
- `/leaderboard` — Rank members by total weight lost since starting weight

**Automated summaries:**
- Post a weekly group summary automatically (e.g., every Monday after the check-in window closes)
- Use the Google Sheets API to generate a chart and attach it as an image

**Streak tracking:**
- Track consecutive weeks checked in and post streak milestones

**Notifications:**
- DM members who haven't checked in by a certain day of the week

### Adding a New Slash Command

```python
@bot.tree.command(name="progress", description="Show your weight progress over time")
async def progress(interaction: discord.Interaction) -> None:
    records = sheets.get_latest_checkins(limit=100)
    # filter to this user, calculate trend, build embed...
    await interaction.response.send_message(embed=embed)
```

After adding, push to GitHub — the command syncs automatically when the bot restarts.
