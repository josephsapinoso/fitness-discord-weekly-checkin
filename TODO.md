# TODO — progress-photos feature

Steps 1 and 2 are **done** (2026-07-23). Full procedure, for reference or a future
redeploy: [docs/REDEPLOY_CHECKLIST.md](docs/REDEPLOY_CHECKLIST.md).

## ~~1. Deploy to Cloud Run~~ — done

Deployed via `scripts/redeploy.sh` after installing the gcloud CLI locally
(`winget install --id Google.CloudSDK -e`).

- Revision **`fitness-checkin-bot-00002-sh6`**, serving 100% of traffic
- Previous revision (pre-photos, rollback target): `fitness-checkin-bot-00001-fgk`
- Health check: `curl https://fitness-checkin-bot-lpt4hrhaua-uw.a.run.app/` → `ok`

## ~~2. Register slash commands~~ — done

`.env` now carries `DISCORD_APPLICATION_ID`, `DISCORD_PUBLIC_KEY`, `TASK_SECRET`,
`TASKS_LOCATION`, and `TASKS_QUEUE` (synced from `env.yaml`), so it matches
`.env.example`. `register_commands.py` registered **5** commands, confirmed against
the live Discord API: `/checkin`, `/summary`, `/history`, `/progress` (share),
`/day1` (photo attachment).

## ~~3. Grant Discord permissions~~ — already in place

Verified against the Discord API, not by eye. In **No Don't Eat The Cake** →
`#✅weekly-checkins`, the `Fitness Check-in Bot` role already has Send Messages,
Embed Links, **Attach Files**, and **Read Message History** (computed from
`@everyone` + role + channel overwrites; no Administrator shortcut). Nothing to do.

## ~~3b. Modal was broken~~ — found by the smoke test, fixed

`/checkin` failed for **everyone** with "The application did not respond". The
progress-photos change had pushed the modal to **6** top-level components; Discord
allows *"Between 1 and 5 (inclusive)"* and discarded the payload, while Cloud Run
logged a healthy 200. Fixed in `c026815`: Starting Weight dropped from the modal and
recovered server-side from the sheet. Deployed as revision
**`fitness-checkin-bot-00003-7jn`**.

Also added `--cpu-boost`: a cold start measured **3.006 s** against Discord's
**3.000 s** deadline, eating the first interaction after idle. Now **1.866 s** cold.

## 4. Smoke test in Discord — partially done

- [x] `/checkin` → modal shows all 5 fields including **"Progress photo (optional)"**,
      with Last Week's Weight prefilled from the sheet
- [ ] Attach a photo + submit → one-time **"Share it 📸"** opt-in → tapping it posts a
      **Day 1** message to the channel
- [ ] A second `/checkin` with a photo → a **Before & After** composite posts
- [ ] A **Photos** tab has appeared in the Google Sheet
- [ ] `/checkin` with **no** photo still works exactly as before

The unchecked items need a real submission: a genuine weight and a real progress
photo, posted permanently to the group channel. That's yours to run — see the
rollback command above if anything looks wrong.
