# Deploying to Google Cloud Run (free tier)

The bot runs as a **Discord HTTP interactions endpoint** on Cloud Run instead of a
persistent gateway connection. That means it only runs when someone actually uses a
command, which keeps it comfortably inside Cloud Run's always-free tier
(2M requests/month — this bot uses a few hundred).

**Moving pieces:**

| Piece | Service | Free allowance | Our usage |
|---|---|---|---|
| Bot server | Cloud Run | 2M requests, 180k vCPU-sec/mo | ~hundreds of requests |
| Deferred work (Sheets writes, charts) | Cloud Tasks | 1M operations/mo | ~hundreds |
| Weekly reminder | Cloud Scheduler | 3 jobs free | 1 job |
| Container builds | Cloud Build / Artifact Registry | 120 build-min/day, 0.5 GB storage | occasional deploys |
| Storage | Google Sheets | free | unchanged |

Everything below assumes the **same GCP project** you already use for the Sheets
service account.

---

## 0. Prerequisites

- [gcloud CLI](https://cloud.google.com/sdk/docs/install) installed and logged in:

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
```

## 1. Enable APIs (one-time)

```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com cloudtasks.googleapis.com \
  cloudscheduler.googleapis.com
```

## 2. Create the Cloud Tasks queue (one-time)

Discord requires interactions to be acknowledged within 3 seconds, so slow work
(Sheets writes, chart rendering) is bounced through a Cloud Tasks queue back to
the service's `/process` endpoint.

```bash
gcloud tasks queues create discord-followups --location=us-west1
```

## 3. Configure environment variables

```bash
cp env.yaml.example env.yaml
```

Fill in `env.yaml` (it is gitignored — never commit it):

- `DISCORD_TOKEN`, `DISCORD_APPLICATION_ID`, `DISCORD_PUBLIC_KEY` — all from
  [discord.com/developers/applications](https://discord.com/developers/applications).
  The **Public Key** is on the *General Information* tab; it's new to this setup
  (used to verify that requests really come from Discord).
- `CHECKIN_CHANNEL_ID`, `GOOGLE_SHEET_ID`, `GOOGLE_SHEET_TAB`,
  `GOOGLE_CREDENTIALS_JSON` — same values you used on Railway.
- `TASK_SECRET` — generate: `openssl rand -hex 32` (or any long random string).

## 4. Deploy

From the repo root:

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

Note the service URL it prints, e.g.
`https://fitness-checkin-bot-xxxxx-uw.a.run.app`.

The service account running the service (default compute SA unless you override
with `--service-account`) needs permission to enqueue tasks:

```bash
PROJECT_NUMBER=$(gcloud projects describe $(gcloud config get-value project) --format='value(projectNumber)')
gcloud projects add-iam-policy-binding $(gcloud config get-value project) \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/cloudtasks.enqueuer"
```

Sanity check: `curl https://YOUR-SERVICE-URL/` should return `ok`.

## 5. Point Discord at the service

1. Open [discord.com/developers/applications](https://discord.com/developers/applications) → your app → **General Information**
2. Set **Interactions Endpoint URL** to `https://YOUR-SERVICE-URL/interactions`
3. Click **Save Changes** — Discord immediately sends a PING plus some
   deliberately bad signatures; it only saves if the service answers correctly.
   If it refuses to save, check `DISCORD_PUBLIC_KEY` and the service logs.

> Once this URL is set, Discord stops delivering interactions over the gateway —
> the old Railway/gateway version of the bot is fully superseded.

Then re-register the slash commands (safe to run repeatedly; needs a local
`.env` with `DISCORD_TOKEN` and `DISCORD_APPLICATION_ID`):

```bash
pip install requests python-dotenv
python register_commands.py
```

## 6. Keep-warm ping via Cloud Scheduler

Cloud Run scales to zero and evicts the instance after ~15 minutes idle. Discord's
interaction deadline is a hard **3.000 s**, and a cold start measured 3.9-4.4 s on
2026-07-27 — so the first command after a quiet period was lost, *and* its
interaction token was invalidated, taking the follow-up with it.

One scheduled request per minute keeps an instance alive. `GET /` also primes the
Cloud Tasks client and the Sheets stack on its first call, so that cost is paid by a
request nobody is waiting on rather than inside someone's `/checkin`:

```bash
gcloud scheduler jobs create http fitness-bot-keep-warm   --location=us-west1   --schedule="*/1 * * * *"   --time-zone="Etc/UTC"   --uri="https://YOUR-SERVICE-URL/"   --http-method=GET   --attempt-deadline=30s   --max-retry-attempts=1
```

No auth flags: the service is `--allow-unauthenticated` and `GET /` needs no secret
(unlike `/reminder`). Cost stays $0 — Cloud Scheduler's free tier is 3 jobs and this
is the second, and ~44k pings/month at ~0.1 vCPU-s each is about 2.4% of Cloud Run's
free 180,000 vCPU-seconds.

## 7. Weekly reminder via Cloud Scheduler

The reminder used to be a loop inside the bot process; now Cloud Scheduler calls
`/reminder` on a cron schedule. The old schedule was **Monday 09:00 UTC**:

```bash
gcloud scheduler jobs create http weekly-checkin-reminder \
  --location=us-west1 \
  --schedule="0 9 * * 1" \
  --time-zone="Etc/UTC" \
  --uri="https://YOUR-SERVICE-URL/reminder" \
  --http-method=POST \
  --headers="X-Reminder-Secret=YOUR_TASK_SECRET_VALUE"
```

To change the day/time, edit `--schedule` (standard cron) and `--time-zone`
(e.g. `America/Los_Angeles`) — no code change or redeploy needed:

```bash
gcloud scheduler jobs update http weekly-checkin-reminder \
  --location=us-west1 --schedule="0 9 * * 1" --time-zone="Etc/UTC"
```

Fire it once right now to test:

```bash
gcloud scheduler jobs run weekly-checkin-reminder --location=us-west1
```

## 8. Test checklist

In your Discord server:

- [ ] `/history` → returns the sheet link (fastest, no Sheets API call)
- [ ] `/checkin` → modal opens, weights prefilled, submit → embed appears in the channel, row appears in the sheet
- [ ] `/summary` → embed with recent check-ins
- [ ] `/progress` → chart renders; All-Time / 6 Months / 30 Days buttons switch views
- [ ] Scheduler test run (step 6) posted the reminder embed

---

## Operational notes

- **Cold starts:** with `min-instances 0` the first command after a quiet period
  takes ~2–4 s extra. Deferred commands show Discord's "thinking…" state, so this
  is invisible. The one exception is `/checkin` — modals can't be deferred, so on
  a cold start the prefill read may be skipped (the modal still opens, just with
  placeholders instead of prefilled weights). If that annoys, set
  `PREFILL_TIMEOUT_S=2.0` in env.yaml.
- **Bot shows "offline" in the member list:** cosmetic only. With no gateway
  connection Discord never sees presence. Slash commands work regardless.
- **Logs:** `gcloud run services logs read fitness-checkin-bot --region us-west1`
  (or the Cloud Run console).
- **Redeploying after a code change:** re-run the step 4 `gcloud run deploy`
  command.
- **Failed followups are not retried** — `/process` returns 200 even on errors to
  avoid Cloud Tasks retries double-writing check-in rows. Users see a ⚠️ message
  and can just re-run the command.
- **Keeping it at $0:** the free tier is per-billing-account. Budget alert for
  peace of mind: Billing → Budgets & alerts → create a $1 budget with email
  alerts.
