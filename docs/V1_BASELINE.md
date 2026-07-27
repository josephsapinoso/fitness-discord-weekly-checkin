# Version 1 — the frozen baseline

**Tag:** `v1.0.0` · **Branch:** `v1` · **Deployed as:** `fitness-checkin-bot-00010-m8c`
· **Date:** 2026-07-27

This is the complete, working state of the bot before v2 work begins. It exists so v1 can
be reasoned about and returned to without archaeology. **Rollback runbook is at the
bottom.**

Everything here was verified against production, not inferred. The history of how it got
here — including two bugs that shipped looking fine — is in [TODO.md](../TODO.md).

---

## What v1 is

A Discord bot that runs a weekly fitness check-in for a small private server, storing
history in Google Sheets and progress photos in Discord itself.

It is a **stateless HTTP interactions service**, not a gateway bot. Discord POSTs signed
interactions to a Cloud Run URL; anything slow is pushed to Cloud Tasks and answered
afterwards. That shape is what keeps it inside the free tier.

```
Discord ──POST /interactions──► Cloud Run (Flask/gunicorn)
                                   │  must ack within 3.000s
                                   ├─► immediate reply (modal, ephemeral, autocomplete)
                                   └─► enqueue Cloud Task ──► POST /process
                                                                 │
                                          Sheets · matplotlib · PIL · Discord REST
Cloud Scheduler ──POST /reminder──► weekly prompt   (Mondays 09:00 UTC)
Cloud Scheduler ──GET  /  ────────► keep-warm ping  (every minute)
```

**Runtime:** Python 3.12, Flask 3.0.3 + gunicorn 23 (1 worker, 8 threads — matplotlib is
not fork-safe). 10 pinned dependencies, listed in `requirements.txt`.

**Cost:** $0/month. Scales to zero; 2 Cloud Scheduler jobs against a free tier of 3;
keep-warm burns roughly 2.4% of the free 180,000 vCPU-seconds.

---

## The 3-second rule

The single most important constraint, and the cause of every user-visible failure in v1's
history. **Discord discards any interaction response that takes longer than 3.000 seconds,
and permanently invalidates the interaction token when it does** — so the follow-up work
cannot report the failure either. The user sees nothing at all.

v1 defends this in four layers:

1. **Nothing slow runs before the ack.** All Sheets/image work happens in `/process`.
2. **`_interaction_budget()`** (`app.py`) sizes the one unavoidable inline read — the
   `/checkin` modal prefill, which cannot be deferred — to the time *actually left*, using
   Discord's `X-Signature-Timestamp` to account for cold-boot time already spent. When the
   budget is gone it skips the read and opens an unprefilled modal.
3. **The Cloud Tasks client is built once per process**, not per request. Constructing it
   costs ~2.5s cold (gRPC import + ADC + TLS) and used to be paid inside the deadline.
4. **A keep-warm ping every minute** so an instance is almost never cold.

Measured after these landed: **cold boot 1.680 s server-side, warm 0.004 s** — against a
3.000 s deadline. Before them, the same path measured 3.893 s and 4.379 s and was losing
interactions.

**Any v2 change that adds work before the ack must be measured against this budget.**

---

## Commands

Eight, all registered via `register_commands.py` (a bulk `PUT` that overwrites the whole
set — see the rollback runbook, this matters).

| Command | Ack | Deferred work | What the user sees |
|---|---|---|---|
| `/checkin` | modal | `checkin_submit` | Modal (4 text fields + optional photo). Posts a **public** check-in embed, always, before any photo handling. Then: no photo → `✅ Check-in submitted!`; photo + consent → Day 1 or **🔥 Before & After**; photo, no consent → one-time share prompt |
| `/summary` | deferred | `summary` | **Public** `📊 Latest Check-ins` embed, last 5 of 10 fetched, + sheet link |
| `/progress` | deferred (ephemeral unless `share:true`) | `progress` | `📈 Progress` embed + matplotlib chart + All-Time / 6-Month / 30-Day buttons. Needs ≥2 check-ins |
| `/history` | immediate ephemeral | none | Google Sheet link. Touches no API — cannot fail on latency |
| `/day1` | deferred ephemeral | `set_baseline` | Sets/replaces the before-after baseline. Posts `📸 Day 1` publicly |
| `/collage` | deferred (ephemeral unless `share:true`) | `collage` | `🖼️ Progress Collage` grid, max 9 panels, from the archive channel |
| `/photo-replace` | deferred ephemeral | `photo_replace` | Swaps the photo stored for one date (autocompleted). Deletes both old copies first |
| `/howto` | immediate | none | Pinnable gold explainer embed |

Plus two buttons: `photo_consent:{id}` (the share opt-in) and `progress:{view}:{id}` (chart
window switching). Both verify the clicker owns the interaction.

---

## Data model

**Google Sheets — three tabs.** Columns are only ever *appended on the right*; that
invariant is what makes schema changes and rollbacks safe (see `_ensure_headers`).

| Tab | Shape | Holds |
|---|---|---|
| `Check-ins` | append-only, 8 cols | one row per check-in |
| `Photos` | one row per user, 8 cols | consent flag, Day 1 message ref, and any *pending* photo (URL + **kind** + **date**) |
| `Photo Log` | append-mostly, 8 cols | one row per photo ever archived; replaced rows go inactive rather than being deleted |

**Discord is the blob store.** No image bytes are kept in Sheets or on disk — only message
IDs. Two copies exist: the public post, and a raw PNG in the private archive channel.
Re-fetching a message yields a fresh signed CDN URL, which is how photos survive URL
expiry.

### Non-obvious invariants — break these and things fail silently

1. **A Day 1 post keeps its photo in the *embed*, not in `attachments`.** Uploading a file
   *and* referencing it via `attachment://` makes Discord fold the file into the embed and
   leave `attachments` empty. Reading `attachments[0]` raised `IndexError` and broke every
   before/after for the life of the feature. Use `_message_image_url()`, which handles both
   shapes. Archive-channel messages have no embed, so those *do* carry real attachments.
2. **A parked photo must record what the user was doing** (`Pending Kind`/`Pending Date`),
   not just its URL. Inferring the action from state got it backwards: a parked `/day1`
   posted a before/after instead of resetting the baseline.
3. **`/process` always returns 200, even on failure.** A non-200 makes Cloud Tasks retry,
   which would double-write check-in rows. Errors are reported to the user, not to the
   queue.
4. **A dead interaction token falls back to a DM,** then to a deliberately content-free
   public nudge — it must never reveal that a photo was attached, since the user may not
   have consented to that being known.
5. **`TASK_SECRET` guards both `/process` and `/reminder`.** Rotating it breaks Cloud Tasks
   and the Scheduler job simultaneously.
6. **Snowflake IDs are written `RAW`.** `USER_ENTERED` coerces an 18-digit ID to a float
   and silently loses the low digits.

---

## Known limitations

Accepted in v1, all candidates for v2:

- **`maxScale: 1`.** One instance, so genuinely concurrent users queue. Fine at this size.
- **A `/checkin` while an unshared photo is already pending overwrites it.** Only the most
  recent survives to be shared.
- **`_reply` stays silent on a transient (non-dead-token) Discord error**, leaving the
  interaction spinning. Deliberate — a DM fallback there would spam users on a Discord 5xx.
- **Autocomplete assumes every autocomplete is `/photo-replace`'s `date`.** True today;
  fragile if a second autocompleting option is ever registered.
- **An oversized photo reports the generic "Something went wrong"**, not a size message.
- **No pagination anywhere.** `/summary` fetches 10, `/collage` samples 9 panels.
- **Tests are a plain script**, not pytest — `python tests/test_app.py`, 201 checks,
  exits on first failure.

---

# Rollback runbook

Two paths. **Prefer the first** — it is seconds, needs no build, and cannot be affected by
a broken working tree.

### Path A — shift traffic to the v1 revision (fast, reversible)

Cloud Run keeps old revisions. Sending traffic back is instant:

```bash
gcloud run services update-traffic fitness-checkin-bot \
  --region=us-west1 --to-revisions=fitness-checkin-bot-00010-m8c=100
```

Confirm, then check health:

```bash
gcloud run services describe fitness-checkin-bot --region=us-west1 \
  --format='value(status.traffic)'
curl -sS https://fitness-checkin-bot-lpt4hrhaua-uw.a.run.app/     # -> ok
```

To go back to the newer revision, run the same command with its name. Nothing is lost —
this only moves a pointer.

### Path B — redeploy v1 from source

When the revision is gone, or you want `main` to *be* v1 again:

```bash
git checkout v1          # or: git checkout v1.0.0
./scripts/redeploy.sh    # refuses a dirty tree or a HEAD that isn't origin/main
```

### Then, in both cases

1. **Re-register the commands if v2 added or renamed any.**

   ```bash
   python register_commands.py    # must print 8 commands
   ```

   `register_commands.py` does a bulk overwrite, so v1's list replaces v2's. **Skipping
   this leaves v2-only commands visible in Discord that v1 cannot handle**, and they fail
   with "The application did not respond". This is the most commonly missed rollback step.

2. **Confirm what is actually running.**

   ```bash
   ./scripts/check_deployed.sh    # 0 in sync, 1 drifted, 2 unknown
   ```

3. **Check the scheduler jobs are still enabled** (they are independent of the service, so
   a rollback does not touch them — but a paused keep-warm reintroduces cold starts):

   ```bash
   gcloud scheduler jobs list --location=us-west1   # both ENABLED
   ```

### What about the data?

**The Sheet needs no rollback, by design.** `_ensure_headers` treats a header row *wider*
than the code's as a newer schema and leaves it completely alone — v1 reads the columns it
knows about, ignores the rest, and does not destroy them. So a v2 schema addition is
survivable in both directions, and re-upgrading finds its columns intact.

Two things rollback genuinely cannot undo:

- **Rows already written by v2.** If v2 changes what goes *into* a column, those values
  stay. Only new writes revert to v1's meaning.
- **Discord messages already posted.** Embeds, composites and archive uploads are not
  recalled. They are just messages; delete by hand if it matters.

**If v2 adds a column, do not remove it when rolling back.** Leaving it costs nothing and
keeps the forward path open.

### Verifying a rollback worked

```bash
# no errors since the switch
gcloud logging read 'resource.type="cloud_run_revision" AND
  resource.labels.service_name="fitness-checkin-bot" AND severity>=ERROR' --freshness=15m
```

Then in Discord: `/howto` (synchronous, proves the service answers), `/history`
(ephemeral, no Cloud Task), and `/checkin` **with a photo** — that last one exercises the
modal, the deferred task, Sheets, the image pipeline and the archive in a single action.
It is the highest-value single test in the system.
