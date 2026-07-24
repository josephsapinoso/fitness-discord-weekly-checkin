# Redeploy checklist — progress-photos change

To-do list for pushing the **progress pictures + `/day1` + before/after** feature
(merged in PR #1) to the live bot. This is a **code-only redeploy of an existing
Cloud Run service** — the one-time infrastructure (APIs, Cloud Tasks queue,
`env.yaml`, Discord Interactions Endpoint URL, Cloud Scheduler) is already set up
and can be skipped. Full first-time guide: [`GCP_DEPLOY.md`](GCP_DEPLOY.md).

## Fast path

Run the script from the repo root, on a machine with `gcloud` authenticated to
the bot's project and `env.yaml` + `.env` present:

```bash
./scripts/redeploy.sh
# or, if your service/region differ from the defaults:
SERVICE=fitness-checkin-bot REGION=us-west1 ./scripts/redeploy.sh
```

The script automates steps 1–4 below and prints reminders for step 5 (which must
be done by hand in Discord).

## The steps

- [ ] **1. Sync merged code** — `git checkout main && git pull origin main`
- [ ] **2. Deploy to Cloud Run** — `gcloud run deploy fitness-checkin-bot --source . --region us-west1 --allow-unauthenticated --memory 512Mi --min-instances 0 --max-instances 1 --cpu-boost --env-vars-file env.yaml`
      - Keep `--cpu-boost`. Discord drops any interaction response slower than
        3.000s; a cold start without boost measured 3.006s and silently ate the
        first `/checkin` after an idle period.
      - No `env.yaml` edits required. Optional new vars `GOOGLE_PHOTOS_TAB`
        (default `Photos`), `GOOGLE_PHOTO_LOG_TAB` (default `Photo Log`) and
        `MAX_IMAGE_BYTES` (default 10 MiB) only if you want to override them.
      - **`ARCHIVE_CHANNEL_ID`** enables `/collage` and `/photo-replace`. Create a
        private channel only the bot can post in, copy its ID, and add it to
        `env.yaml`. Left unset, those two commands reply "photo history isn't set
        up" and everything else works normally.
      - No new dependencies — `requirements.txt` is unchanged (Pillow ships with
        matplotlib).
- [ ] **3. Health check** — `curl https://YOUR-SERVICE-URL/` returns `ok`
- [ ] **4. Register slash commands** — `python register_commands.py`
      (needs `.env` with `DISCORD_TOKEN` + `DISCORD_APPLICATION_ID`). Must print
      **5** commands including `/day1`, or the command won't appear in Discord.
- [ ] **5. Grant Discord permissions (manual, in the Discord app)** — the bot needs
      **Attach Files** and **Read Message History** in the check-in channel.
      Without these the text check-in still posts but **photos silently fail**.

## Smoke test (in Discord)

- [ ] `/checkin` → the modal shows a **"Progress photo (optional)"** upload field
- [ ] Attach a photo + submit → one-time **"Share it 📸"** opt-in prompt → tapping
      it posts a **Day 1** message to the channel
- [ ] A second `/checkin` with a photo → a **Before & After** composite posts
- [ ] A **Photos** tab has appeared in the Google Sheet
- [ ] `/checkin` with **no** photo still works exactly as before

## Rollback

```bash
gcloud run revisions list --service fitness-checkin-bot --region us-west1
gcloud run services update-traffic fitness-checkin-bot --region us-west1 --to-revisions <PREV_REVISION>=100
```

## Gotchas

- The two easiest steps to forget are **#4** (`/day1` won't show up) and **#5**
  (photos won't post).
- Failed follow-ups are not retried — `/process` returns 200 on error so Cloud
  Tasks doesn't double-write. A failed photo upload shows the user a ⚠️ and they
  can re-run; the text check-in has already posted independently.
