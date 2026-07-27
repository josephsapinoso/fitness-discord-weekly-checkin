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
`.env.example`. `register_commands.py` registered **8** commands, confirmed against
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

*(Correction, 2026-07-27: the "gunicorn + matplotlib import" attribution above was
wrong — matplotlib is imported lazily and has never been on the `/interactions`
path. See §7.)*

## ~~3c. `/checkin` "did not respond" again on cold starts~~ — fixed

Recurred **2026-07-27**: a check-in ~9 h after the 2 AM reminder hit a cold
(scaled-to-zero) instance and failed with "The application did not respond". The
modal can't be deferred, and the inline prefill Sheets read was bounded only by
`PREFILL_TIMEOUT_S` (1.4 s) — which counts *after* the handler starts. A ~1.9 s
cold boot **plus** that 1.4 s read (**plus** an unbounded first-time `import
sheets`) overran the 3 s window. The 07-24 smoke test passed only because the
instance was warm.

Fixed by sizing the read to the time actually left: `X-Signature-Timestamp`
tells us when Discord sent the interaction, so `now − that` is how much of the
3 s is already gone (cold boot included). `_interaction_budget()` bounds the
read to the remainder (minus a ship-the-reply margin) and skips it entirely when
spent, so the modal always opens in time — degrading to "no prefill" instead of
failing. The heavy `import sheets` also moved into the timed worker so its cold
cost is bounded too. Same guard applied to `/photo-replace` autocomplete.
Regression test added (stale-timestamp `/checkin` → modal opens, sheet not read).

This bounded the *prefill read* correctly, but the cold start itself was still
~2.5 s of per-request Cloud Tasks client construction — see §7, which removes that
cost and keeps an instance warm so the budget is rarely tight in the first place.

## ~~4. Smoke test in Discord~~ — done, with one box that was never true

Run on 2026-07-24. Verified: check-in row 16 logged with Starting Weight 201
(derived from the sheet, not asked for), `Photos` tab created with consent recorded
and `Day1 Ref` set, Day 1 photo posted.

- [x] `/checkin` → modal shows all 5 fields including **"Progress photo (optional)"**,
      with Last Week's Weight prefilled from the sheet
- [x] Attach a photo + submit → one-time **"Share it 📸"** opt-in → tapping it posts a
      **Day 1** message to the channel
- [x] A **Photos** tab has appeared in the Google Sheet
- [x] `/checkin` with **no** photo still works exactly as before
- [x] A second `/checkin` with a photo → a **Before & After** composite posts
      — **never actually run when this was first ticked, and it was broken.**
      Genuinely verified 2026-07-27 21:50 UTC. See §6.

## ~~5. Create the photo archive channel~~ — done

`#photo-archive` created as a private text channel (`@everyone` denied View; the
`Fitness Check-in Bot` role granted View / Send / Attach Files / Read Message History,
all verified via the API). `ARCHIVE_CHANNEL_ID` is set in `env.yaml` and `.env`, live
as of revision **`fitness-checkin-bot-00005-prx`**. The `Photo Log` tab now exists in
the sheet with its headers.

`/collage` correctly reports "No progress photos yet" rather than "photo history isn't
set up" — the read path is wired end to end.

**Photo history starts now.** Photos submitted before the archive existed were never
retained as individual images (only the before/after composites were ever uploaded),
so there is nothing to backfill. The next `/checkin` with a photo writes the first
`Photo Log` row, after which `/collage` and `/photo-replace` have data to work with.

## ~~6. The before/after composite never worked~~ — fixed 2026-07-27

A `/checkin` accepted the numbers (row written, 18:51 UTC) but never shared the photo:

```
File "/app/app.py", line 931, in _post_progress_photo
    day1_png = discord_api.download_image(day1_msg["attachments"][0]["url"])
IndexError: list index out of range
```

**Cause.** Uploading a file *and* referencing it from an embed as
`attachment://day1.png` makes Discord **fold the file into the embed and leave
`attachments` empty**. So `attachments[0]` never existed on any Day 1 post — the
first photo (which posts a Day 1) always worked, and every photo after it died.
Verified against the live API: both members' stored Day 1 messages report **0
attachments** with a live `embeds[0].image.url`, while archive-channel messages
(posted with no embed) do carry a real attachment.

The tests missed it because the fixture hand-built a message shape Discord never
returns. That fixture now models the real shape, and reproduces the `IndexError`
against the old code.

**Fix.** `_message_image_url()` resolves either shape, and `_day1_png()` prefers the
raw PNG behind the `Photo Log` archive ref, falls back to the embed image (which is
what recovers baselines predating the Photo Log), and if neither can be read posts
the new photo as a fresh Day 1 instead of failing the check-in.

- [x] **Verified end to end 2026-07-27 21:50 UTC.** `/checkin` with a photo posted
      `🔥 Before & After — JoeLotto` to the check-in channel, and the first
      `Photo Log` row for that user was written (`kind=progress`, archived,
      active). Deployed as revision **`fitness-checkin-bot-00006-c66`**.

## ~~7. Cold starts were silently eating interactions~~ — fixed 2026-07-27

Separate bug, same day. `/interactions` took **4.379 s** (18:10) and **3.893 s**
(18:45) against Discord's hard **3.000 s** deadline, because the service scales to
zero and is evicted after ~15 min idle. A missed deadline also *invalidates the
interaction token*, so the follow-up died with
`404 {"message": "Unknown Webhook", "code": 10015}` — a `/day1` photo was parked as
`pending_url` and its "Share it 📸" button never arrived. `/process` then tried to
apologise on the same dead token and swallowed that failure too, so the member saw
nothing at all.

**Not matplotlib.** It is imported lazily and only ever from `/process`. The cost was
`tasks_queue.enqueue()` building a `CloudTasksClient()` **per request** — gRPC import,
ADC resolution and a TLS handshake, ~2.5 s cold — plus a second ADC lookup because
`GOOGLE_CLOUD_PROJECT` was not in `env.yaml`. It also explains the warm split: 0.68 s
for commands that enqueue vs 0.087 s for those that don't.

**Fix.** The client is now built once per process and cached (with one rebuild-and-retry
in case CPU throttling froze the channel), `GOOGLE_CLOUD_PROJECT` is set, `GET /` warms
the client and the Sheets stack, and a Cloud Scheduler job pings `GET /` every minute so
the warm-up is paid by a request nobody is waiting on. A dead token now falls back to a
DM, and then to a deliberately content-free public nudge that doesn't reveal that a photo
was attached.
