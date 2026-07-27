# Fitness Check-in Bot — Operations & Maintenance Guide

This document covers how to manage the bot after it's live: deploying code, monitoring health, changing settings, extending functionality, troubleshooting, and security practices.

The bot runs as a stateless **Discord HTTP interactions endpoint on Cloud Run**. For the
first-time deployment see [GCP_DEPLOY.md](GCP_DEPLOY.md); for shipping a change to an
already-live service see [REDEPLOY_CHECKLIST.md](REDEPLOY_CHECKLIST.md).

---

## Day-to-Day Operations

### How Deployments Work

Deploys are **manual and explicit** — pushing to GitHub runs the tests but deploys nothing.
From the repo root:

```bash
./scripts/redeploy.sh          # wraps the command below + health check + command registration
```

or by hand:

```bash
gcloud run deploy fitness-checkin-bot \
  --source . \
  --region us-west1 \
  --allow-unauthenticated \
  --memory 512Mi \
  --min-instances 0 \
  --max-instances 1 \
  --env-vars-file env.yaml
```

```
Edit code locally → gcloud run deploy → Cloud Build builds the container
→ new revision goes live → traffic shifts to it (no downtime; Cloud Run
   drains the old revision)
```

The service URL is stable across revisions, so the Discord Interactions Endpoint URL never
needs updating after the first deploy.

### Checking Service Status

```bash
# Is it up?
curl https://YOUR-SERVICE-URL/          # → ok

# Revisions and which is serving traffic
gcloud run services describe fitness-checkin-bot --region us-west1

# Recent logs
gcloud run services logs read fitness-checkin-bot --region us-west1 --limit 50
```

Or use the Cloud Run console: **Cloud Run → fitness-checkin-bot → Revisions / Logs / Metrics**.

Because `min-instances` is 0, "no running instance" is the normal idle state — not an
outage. The service spins up on the next request.

### Viewing Logs

Log lines look like:

```
2026-07-24 09:00:00 INFO Enqueued task kind=checkin_submit
2026-07-24 09:00:02 INFO Weekly reminder posted.
2026-07-24 09:00:05 WARNING Prefill skipped: TimeoutError
```

Useful filters:

```bash
# Errors and warnings only
gcloud run services logs read fitness-checkin-bot --region us-west1 --limit 100 \
  | grep -E "ERROR|WARNING"

# Cloud Tasks queue health (failed followups show up here)
gcloud tasks queues describe discord-followups --location us-west1
```

---

## Changing the Reminder Schedule

The reminder is a **Cloud Scheduler** job that POSTs to `/reminder` — not an environment
variable, and not a loop in the code. Changing it needs no redeploy:

```bash
gcloud scheduler jobs update http weekly-checkin-reminder \
  --location=us-west1 \
  --schedule="0 9 * * 1" \
  --time-zone="Etc/UTC"
```

`--schedule` is standard cron (`minute hour day-of-month month day-of-week`).

| Goal | `--schedule` | `--time-zone` |
|------|--------------|---------------|
| Monday 9:00 AM UTC (current) | `0 9 * * 1` | `Etc/UTC` |
| Friday 5:00 PM Pacific | `0 17 * * 5` | `America/Los_Angeles` |
| Sunday 8:30 PM Eastern | `30 20 * * 0` | `America/New_York` |

Setting `--time-zone` to a real zone (rather than converting to UTC by hand) means the
reminder follows daylight saving automatically.

Test it immediately without waiting for the schedule:

```bash
gcloud scheduler jobs run weekly-checkin-reminder --location=us-west1
```

To change the reminder's wording, edit `_reminder_embed()` in `app.py` and redeploy.

---

## Managing Progress Photos

Photos are posted as messages in the check-in channel. The **`Photos` sheet tab** stores
only per-user state — never image bytes:

| Column | Meaning |
|--------|---------|
| `User ID` / `Username` | who the row belongs to |
| `Consent` | `yes` once the user has opted into public sharing |
| `Day1 Ref` | Discord **message id** holding the durable Day 1 photo |
| `Pending URL` | a not-yet-consented photo's short-lived CDN URL, cleared on consent |

Discord attachment URLs expire, which is why `Day1 Ref` stores a message id — the bot
re-fetches the message (`discord_api.get_message`) to get a fresh signed URL whenever it
needs the Day 1 image.

**Deleting a user's photos** — there is no automated deletion. Do both:

1. Delete the photo messages in the Discord channel.
2. Clear that user's row in the `Photos` tab (at minimum `Day1 Ref` and `Pending URL`).

Clearing only the sheet leaves the images visible in Discord; deleting only the messages
leaves a dangling `Day1 Ref` that makes the next before/after fail.

**Resetting someone's Day 1** — clear their `Day1 Ref` cell, or have them run `/day1`
with a new photo (which overwrites it).

**Revoking consent** — set `Consent` to blank. Their next photo goes back to the one-time
opt-in prompt.

**Size and format limits** live in `discord_api.download_image()`: PNG/JPEG/WebP only, and
`MAX_IMAGE_BYTES` (default 10 MiB) enforced both from `Content-Length` and while streaming.
`images.normalize()` then strips EXIF/GPS and caps the largest dimension at 1600px.

---

## Adding or Removing Check-in Fields

The check-in fields are defined in several places. All must be updated together.

### 1. Update the Modal (`app.py`)

Text fields are built by `_checkin_modal()`. Discord allows a **maximum of 5 components**
in a modal, and the photo upload occupies one of them — so the modal is currently **full**
at 5 text inputs + 1 upload. Adding a field means removing one, or dropping the upload.

```python
text_input(
    "body_fat", "Body Fat %",
    "e.g. 18%", max_length=20,
),
```

Note `text_input()` hardcodes `required: True` — make it a parameter if you want an
optional field.

### 2. Read the New Value (`app.py`)

`_modal_values()` flattens submitted text components generically, so it needs no change
(it skips the type-19 upload component, which `_modal_photo_url()` resolves separately).
But `_task_checkin_submit()` passes fields explicitly to `sheets.log_checkin()` — add
yours there, and to `_build_checkin_embed()` if it should appear in the posted embed.

### 3. Update the Sheet Schema (`sheets.py`)

Add the column to `HEADERS` and the value to `log_checkin()`:

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

> **Careful:** `_get_sheet()` compares row 1 against `HEADERS` and inserts a fresh header
> row if they differ. Changing `HEADERS` without also fixing the live sheet will insert a
> second header row above your data. Update the sheet's header row by hand to match.
> The same applies to `PHOTO_HEADERS` and the `Photos` tab.

> Existing rows won't have values for new columns — they'll be blank, which is fine.
> `get_all_records()` keys off the header row.

### 4. Update the Tests

`tests/test_app.py` asserts the modal's component count and specific field values. Adjust
those assertions to match, then re-run the suite.

---

## Changing the Check-in Channel

1. In Discord (with Developer Mode on), right-click the new channel → **Copy Channel ID**.
2. Update `CHECKIN_CHANNEL_ID` in `env.yaml`.
3. Redeploy, or update just the variable without a rebuild:

```bash
gcloud run services update fitness-checkin-bot --region us-west1 \
  --update-env-vars CHECKIN_CHANNEL_ID=NEW_CHANNEL_ID
```

Make sure the bot has Send Messages, Embed Links, Attach Files, and Read Message History
in the new channel.

> Moving channels **breaks existing before/after comparisons**: `Day1 Ref` message ids are
> looked up in `CHECKIN_CHANNEL_ID`, so Day 1 photos left behind in the old channel can no
> longer be fetched. Either keep the old channel readable and migrate deliberately, or have
> users re-run `/day1` in the new one.

---

## Running the Tests

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt cryptography
python tests/test_app.py
```

That's the same install CI uses (`.github/workflows/tests.yml`), which runs the suite on
Python 3.11 and 3.12 for every push and pull request. `cryptography` is a test-only
dependency — it backs the Ed25519 nacl stub — which is why it isn't in `requirements.txt`.

The suite is a plain script, not pytest. It exercises the real Flask app, real matplotlib
and Pillow rendering, and real Ed25519 signature verification, while stubbing Google
Sheets, Cloud Tasks, and Discord's REST API (see `tests/stubs/`). It exits non-zero on the
first failure and prints `All N checks passed ✅` on success.

Use Python 3.11–3.13; the pinned `numpy`/`matplotlib` versions have no 3.14 wheels yet.

---

## Troubleshooting

### Commands fail instantly with "The application did not respond"

Discord requires acknowledgement within **3 seconds**. Causes:

1. The service is erroring — check logs for a traceback.
2. A cold start exceeded the budget. Deferred commands (`/summary`, `/progress`,
   `/day1`) are immune only once warm — the Cloud Tasks *enqueue* happens before the
   ack, so on a cold process it is the enqueue that blows the deadline. Check that
   the `fitness-bot-keep-warm` scheduler job is still firing (`gcloud scheduler jobs
   describe fitness-bot-keep-warm --location us-west1`); a missed deadline also kills
   the interaction token, which shows up in the logs as `Interaction token dead`.
3. `TASK_SECRET` mismatch, so `/process` returns 403 and the followup never lands.

### Discord won't save the Interactions Endpoint URL

Discord sends a PING plus deliberately malformed signatures, and only saves if your service
answers both correctly. Check that `DISCORD_PUBLIC_KEY` is the **Public Key** from General
Information (not the token, not the application ID), and that `curl https://YOUR-SERVICE-URL/`
returns `ok`.

### `/checkin` modal opens but weights aren't prefilled

Expected on a cold start. Modals can't be deferred, so the prefill Sheets read has a hard
budget (`PREFILL_TIMEOUT_S`, default 1.4s); on timeout the modal opens with placeholders
instead. Logs show `Prefill skipped: ...`. Raise it to `2.0` in `env.yaml` if it bothers
you — but leave headroom under Discord's 3-second limit.

### Text check-in posts but the photo doesn't

This split is deliberate — `_task_checkin_submit()` posts the text embed first so a photo
failure can never lose the written check-in. Check, in order:

1. **Bot permissions** — Attach Files and Read Message History in the check-in channel.
   This is the most common cause, and it fails silently from the user's perspective.
2. **Consent** — an un-opted-in user gets the private "Share it 📸" prompt instead of a
   public post. Check their `Consent` cell in the `Photos` tab.
3. **Size/type rejection** — logs show `image exceeds size limit` or
   `unsupported image content-type`. Only PNG, JPEG, and WebP are accepted.
4. **Dangling `Day1 Ref`** — if the Day 1 message was deleted from Discord, `get_message`
   404s and the before/after fails. Clear the `Day1 Ref` cell to start over.

### `/day1` doesn't appear in Discord

Re-run `python register_commands.py` and confirm it prints **8** commands. This is the
easiest step to forget after deploying the photo feature. Global commands can take up to
an hour to propagate.

### `/checkin` submits but nothing appears in Google Sheets

1. Check logs for `Task checkin_submit failed`.
2. Common causes:
   - `GOOGLE_CREDENTIALS_JSON` malformed — must be valid JSON, single-quoted in `env.yaml`.
   - The service account lost Editor access to the sheet — re-share it.
   - The Sheets or Drive API was disabled — re-enable at [console.cloud.google.com/apis](https://console.cloud.google.com/apis).

### A command shows "⚠️ Something went wrong"

That's the catch-all in `/process`. The real traceback is in the Cloud Run logs under
`Task <kind> failed`. Failed followups are **deliberately not retried** — `/process`
returns 200 even on error, because a Cloud Tasks retry could double-write a check-in row.
Users just re-run the command.

### Weekly reminder didn't fire

```bash
gcloud scheduler jobs describe weekly-checkin-reminder --location us-west1
```

Check `lastAttemptTime` and the job's state. A 403 means the `X-Reminder-Secret` header
doesn't match `TASK_SECRET`. Run the job manually to reproduce.

### Bot shows offline in the member list

Cosmetic and expected — there's no gateway connection, so Discord has no presence to show.
Slash commands are unaffected.

---

## Cost & Usage Monitoring

Everything sits inside GCP's always-free tier:

| Service | Free allowance | This bot's usage |
|---------|----------------|------------------|
| Cloud Run | 2M requests, 180k vCPU-sec/month | hundreds of requests |
| Cloud Tasks | 1M operations/month | hundreds |
| Cloud Scheduler | 3 jobs | 1 job |
| Cloud Build / Artifact Registry | 120 build-min/day, 0.5 GB storage | occasional deploys |
| Google Sheets | free | unchanged |

Expected bill: **$0/month**. Set a budget alert for peace of mind: Billing → Budgets &
alerts → create a $1 budget with email notifications.

The main way to accidentally spend money is leaving `--min-instances` above 0, which keeps
a billable instance warm. Keep it at 0. Photo compositing is the most memory-hungry
operation; if you see OOM restarts in the logs, raise `--memory` from 512Mi rather than
adding instances.

---

## Security Best Practices

### Secrets Management

| Secret | Where it lives | What to do if compromised |
|--------|----------------|--------------------------|
| `DISCORD_TOKEN` | `env.yaml` → Cloud Run env var | Reset in the Developer Portal → redeploy |
| `DISCORD_PUBLIC_KEY` | `env.yaml` → Cloud Run env var | Not secret — it's a public verification key |
| `GOOGLE_CREDENTIALS_JSON` | `env.yaml` → Cloud Run env var | Delete the key in GCP IAM → create a new one → redeploy |
| `TASK_SECRET` | `env.yaml` + the Scheduler job header | Rotate in both places together, or `/reminder` starts 403-ing |
| GitHub PAT (if used during setup) | Should be revoked | github.com/settings/tokens → Revoke |

**Never:**
- Commit `.env`, `env.yaml`, or `credentials.json` (all gitignored — keep it that way)
- Embed a personal access token in the git remote URL (`git remote -v` will expose it);
  use the `gh` CLI or a credential helper
- Share the bot token publicly
- Leave old or compromised credentials active

For a stronger setup, move secrets into **Secret Manager** and reference them with
`--set-secrets` on `gcloud run deploy` instead of `--env-vars-file`. Env vars are visible
to anyone with Cloud Run console access to the project.

### Endpoint Exposure

The service is deployed `--allow-unauthenticated` because Discord must reach
`/interactions` without GCP credentials. Each endpoint is protected individually:

- `/interactions` — Ed25519 signature verification against `DISCORD_PUBLIC_KEY`
- `/process` — `X-Task-Secret` header must equal `TASK_SECRET`
- `/reminder` — `X-Reminder-Secret` header must equal `TASK_SECRET`
- `/` — health check, returns `ok`, harmless

### Photo Privacy

- EXIF/GPS metadata is stripped on ingest (`images.normalize()` re-encodes to PNG).
- Inbound images are content-type allowlisted and size-capped before decoding, and
  Pillow's decompression-bomb ceiling is tightened to ~40 MP.
- Nothing is shared publicly until the user taps the one-time opt-in.
- The sheet stores message references, never image bytes or long-lived URLs.
- Photos live in the check-in channel indefinitely — **keep that channel private to your
  group**, and see "Managing Progress Photos" above for manual deletion.

### Limiting Bot Permissions

The bot is invited with only what it needs:
- Send Messages
- Embed Links
- Attach Files (upload progress photos)
- Read Message History (re-fetch the Day 1 photo for before/after)
- Use Application Commands

Avoid granting Administrator permission — it's unnecessary and increases risk.

### Rotating Credentials

Rotate the bot token and service account key every 6–12 months:

1. **Discord token:** Reset Token in the Developer Portal → update `env.yaml` → redeploy.
2. **Google service account key:** GCP → Service Accounts → Keys → Add Key → update
   `env.yaml` → redeploy → delete the old key.

---

## Extending the Bot

### Ideas for Future Features

**Progress tracking commands:**
- `/progress @user` — view someone else's trend (the current `/progress` is self-only)
- `/leaderboard` — rank members by total weight lost since starting weight

**Automated summaries:**
- Post a weekly group summary automatically — a second Cloud Scheduler job hitting a new
  endpoint is the natural shape, since the reminder job already proves the pattern
- Render a group chart with matplotlib the way `/progress` does for individuals

**Photo features:**
- A `/photos` command to list or clear your own stored photos (self-service deletion)
- Multi-point comparisons (Day 1 → midpoint → now) rather than just two panels

**Streak tracking:**
- Track consecutive weeks checked in and post streak milestones

**Notifications:**
- DM members who haven't checked in by a certain day of the week

### Adding a New Slash Command

Three places, in this order:

**1. Declare it** in `register_commands.py`:

```python
COMMANDS = [
    ...,
    {"name": "leaderboard", "description": "Rank members by total weight lost"},
]
```

Then run `python register_commands.py` to push the list to Discord. It bulk-overwrites, so
it's safe to re-run — but a command you forget to add here will never appear.

**2. Handle it** in `_handle_command()` in `app.py`. The critical decision is whether the
work fits inside Discord's 3-second ack window:

```python
# Fast path — no I/O, answer directly
if name == "leaderboard":
    return jsonify({
        "type": CHANNEL_MESSAGE,
        "data": {"content": "...", "flags": EPHEMERAL},
    })
```

```python
# Slow path — anything touching Sheets, matplotlib, or image downloads
if name == "leaderboard":
    tasks_queue.enqueue(
        {"kind": "leaderboard", "token": interaction["token"]}, _self_url()
    )
    return jsonify({"type": DEFERRED_CHANNEL_MESSAGE})
```

Use the slow path unless you're certain there's no I/O — `/history` is the only command
answering directly, and only because it builds a URL from an env var.

**3. Do the work** in `process_task()`, dispatched on `kind`:

```python
def _task_leaderboard(payload: dict) -> None:
    import sheets
    records = sheets.get_latest_checkins(limit=500)
    # rank, build embed...
    discord_api.edit_original_response(payload["token"], {"embeds": [embed]})
```

Then redeploy. Add coverage in `tests/test_app.py` — the existing deferred-command tests
are the template.
