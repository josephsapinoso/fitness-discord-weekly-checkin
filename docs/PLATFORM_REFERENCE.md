# Fitness Check-in Bot — Platform & Tools Reference

This document explains every platform, service, and library used in this project, why it was chosen, and how it fits into the overall architecture.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                        Discord Server                        │
│  Users run /checkin, /day1, /progress → bot posts to channel │
└───────────────────────────┬─────────────────────────────────┘
                            │ signed HTTPS POST (Ed25519)
                            │ must be acknowledged in 3 seconds
┌───────────────────────────▼─────────────────────────────────┐
│                  Cloud Run: fitness-checkin-bot              │
│                  Flask + gunicorn, scales to zero            │
│                                                              │
│   POST /interactions  verify signature → ack (defer/modal)   │
│   POST /process       deferred work, called back by Tasks    │
│   POST /reminder      called by Cloud Scheduler              │
│   GET  /              health check                           │
│                                                              │
│   app.py ──┬── sheets.py  (gspread + google-auth)            │
│            ├── charts.py  (matplotlib + numpy)               │
│            ├── images.py  (Pillow + matplotlib)              │
│            └── discord_api.py (requests → Discord REST)      │
└────┬──────────────────────┬─────────────────────┬───────────┘
     │ enqueue              │ HTTPS               │ HTTPS
┌────▼─────────┐   ┌────────▼──────────┐  ┌───────▼─────────┐
│ Cloud Tasks  │   │  Google Sheets    │  │  Discord REST   │
│ discord-     │   │  Check-ins tab    │  │  post messages, │
│ followups    │   │  Photos tab       │  │  edit responses │
└────┬─────────┘   └───────────────────┘  └─────────────────┘
     │ POST /process (X-Task-Secret)
     └─────────────▶ back to Cloud Run

Cloud Scheduler ──(cron: Mon 09:00 UTC)──▶ POST /reminder
```

The defining constraint is Discord's **3-second acknowledgement deadline**. Anything slower
than that — Sheets I/O, matplotlib rendering, downloading and compositing photos — is
acknowledged immediately with a "deferred" response, handed to Cloud Tasks, and completed
by a second request to `/process`, which then edits the original response.

---

## Discord

**What it is:** A real-time messaging platform originally built for gaming communities, now widely used for group coordination of all kinds. It supports text channels, voice, video, and rich bot integrations.

**Role in this project:** The user interface. Members interact with the bot entirely within Discord — no separate app or website is needed. The bot lives in a private server channel dedicated to fitness check-ins. It also serves as the **photo store**: progress photos live as channel messages, and the sheet holds only message references.

**Key concepts used:**
- **Slash commands** (`/checkin`, `/summary`, `/progress`, `/history`, `/day1`): Registered commands that Discord surfaces with autocomplete in the chat input. Registered once via `register_commands.py`, which bulk-overwrites the global command list.
- **HTTP interactions endpoint**: Instead of the bot connecting out to Discord, Discord POSTs each interaction to a URL you configure. Every request is signed; the service must verify it and respond within 3 seconds.
- **Interaction response types**: Reply immediately (type 4), open a modal (type 9), or *defer* (types 5/6) to buy time and edit the response later via webhook.
- **Modals**: Pop-up forms rendered natively by the Discord client. Used to collect all 5 check-in fields in one structured interaction, plus an optional **File Upload** component (type 19, wrapped in a Label type 18) for a progress photo. Modals **cannot be deferred**, which is why the prefill Sheets read runs under a hard timeout.
- **Message components**: Buttons carrying a `custom_id`. Used for the `/progress` view switcher (All-Time / 6 Months / 30 Days) and the one-time photo-sharing opt-in.
- **Embeds**: Richly formatted message cards with titles, fields, colors, and thumbnails.
- **Ephemeral messages** (flag 64): Visible only to the invoking user. Used for confirmations, the consent prompt, and `/progress` by default.

**Note:** the **Gateway API** (a persistent WebSocket) is *not* used. That's why the bot appears offline in the member list — Discord has no presence to report. It's cosmetic; commands are unaffected.

**Website:** [discord.com](https://discord.com) | **Developer Portal:** [discord.com/developers](https://discord.com/developers)

---

## Discord Developer Portal

**What it is:** Discord's web console for managing bot applications. Think of it as the "admin panel" for your bot's identity.

**Role in this project:** Used to create the bot application, generate the bot token, retrieve the public key used for signature verification, set the **Interactions Endpoint URL**, and build the OAuth2 invite URL.

**Key concepts used:**
- **Application:** The container for a Discord integration. Has an Application ID (client ID) and can have a Bot user attached to it.
- **Bot Token:** A secret string that authenticates your code as the bot user. Treat it like a password.
- **Public Key:** A non-secret Ed25519 verification key. The service checks every incoming request's `X-Signature-Ed25519` header against it — this is what proves a request genuinely came from Discord and not an attacker who found the URL.
- **Interactions Endpoint URL:** Where Discord POSTs interactions. Discord validates it before saving by sending a PING plus deliberately malformed signatures; it only saves if your service answers both correctly.
- **OAuth2 Scopes:** `bot` grants the bot permission to join servers; `applications.commands` lets it register and use slash commands.
- **Bot Permissions:** A bitmask of allowed actions — Send Messages, Embed Links, Attach Files, Read Message History, Use Application Commands.

---

## Google Cloud Run

**What it is:** A serverless container platform. You give it a container image (or source, which it builds for you); it runs the container on demand behind an HTTPS URL, scales to zero when idle, and bills only for requests actually served.

**Role in this project:** Hosts the entire bot. Chosen when Railway's free trial ended, because the HTTP-interactions model maps perfectly onto request-driven serverless: the bot only needs to exist while someone is running a command.

**Key concepts used:**
- **Revision:** An immutable deployment. Each `gcloud run deploy` creates one; traffic shifts to it with no downtime, and rollback is just pointing traffic at a previous revision.
- **Scale to zero (`--min-instances 0`):** No instance runs while idle, which is what keeps the cost at $0. The trade-off is **cold starts** of ~2–4 s on the first request after a quiet period.
- **`--allow-unauthenticated`:** Required, since Discord can't present GCP credentials. Endpoint security comes from signature verification and shared secrets instead.
- **`$PORT` and gunicorn:** Cloud Run injects `PORT`; the container's `CMD` binds gunicorn to it with 1 worker and 8 threads (matplotlib isn't fork-safe, and traffic is low).
- **Free tier:** 2M requests and 180k vCPU-seconds per month — orders of magnitude more than this bot uses.

**Website:** [cloud.google.com/run](https://cloud.google.com/run)

---

## Google Cloud Tasks

**What it is:** A managed queue for HTTP tasks. You enqueue a request description; Cloud Tasks delivers it to your endpoint asynchronously with retries and rate limiting.

**Role in this project:** The mechanism for beating Discord's 3-second deadline. `/interactions` acknowledges instantly, enqueues a task describing the real work, and Cloud Tasks POSTs back to the same service at `/process` a moment later.

**Key concepts used:**
- **Queue** (`discord-followups`): Created once with `gcloud tasks queues create`.
- **HTTP target task:** The task body is JSON — the interaction token plus a `kind` field (`checkin_submit`, `summary`, `progress`, `set_baseline`, `grant_consent`) that `/process` dispatches on.
- **Shared-secret auth:** Every task carries an `X-Task-Secret` header matched against `TASK_SECRET`.
- **Retries — deliberately defeated:** `/process` returns **200 even on failure**, because a retry could double-write a check-in row. Users see a ⚠️ and re-run instead.
- **Interaction tokens:** Valid for 15 minutes, so the deferred work has ample time.

---

## Google Cloud Scheduler

**What it is:** Managed cron. Fires HTTP requests on a schedule.

**Role in this project:** Posts the weekly check-in prompt. In the old gateway bot this was a `@tasks.loop` inside the resident process; with no resident process, it became an external cron job hitting `POST /reminder`.

**Key concepts used:**
- **Cron schedule + time zone:** `--schedule="0 9 * * 1" --time-zone="Etc/UTC"`. Setting a real IANA zone makes the reminder follow daylight saving automatically.
- **Custom headers:** Carries `X-Reminder-Secret`, checked against `TASK_SECRET`.
- **Manual runs:** `gcloud scheduler jobs run` fires it immediately for testing.
- **Free tier:** 3 jobs; this uses 1.

Changing the schedule requires **no redeploy** — it lives entirely in the job definition.

---

## Flask

**What it is:** A minimal Python web framework.

**Version used:** `3.0.3`

**Role in this project:** Serves the four endpoints in `app.py`. The app is deliberately thin — routing, JSON in/out, and `abort()` for auth failures. `ProxyFix` middleware is applied so `request.host_url` reflects the real external URL behind Cloud Run's proxy (used to build the Cloud Tasks callback URL when `SELF_URL` isn't set).

**Documentation:** [flask.palletsprojects.com](https://flask.palletsprojects.com)

---

## gunicorn

**What it is:** A production WSGI server for Python.

**Version used:** `23.0.0`

**Role in this project:** Runs the Flask app inside the container. Configured as `--workers 1 --threads 8 --timeout 60`: one process because matplotlib is not fork-safe, threads because the work is I/O-bound (Sheets, Discord REST).

---

## PyNaCl

**What it is:** Python bindings to libsodium, providing modern cryptographic primitives.

**Version used:** `1.5.0`

**Role in this project:** `VerifyKey.verify()` checks the Ed25519 signature Discord attaches to every interaction request. This is the security boundary for `/interactions` — an unsigned or badly signed request gets a 401.

**Note:** the test suite substitutes a stub (`tests/stubs/nacl/`) backed by the `cryptography` package, so tests exercise real Ed25519 without requiring libsodium to build.

---

## Google Sheets

**What it is:** Google's cloud-based spreadsheet application. Spreadsheets are stored in Google Drive and accessible via a REST API.

**Role in this project:** The persistent data store. Chosen because it's free, requires no database setup, and gives the group a human-readable view of their history with built-in charting and filtering.

**Two tabs:**
- **`Check-ins`** — append-only; one row per submission with timestamp, user info, and the five check-in fields.
- **`Photos`** — one row per user holding photo-sharing consent, the Day 1 message reference, and any pending (not-yet-consented) photo URL. Kept separate so the append-only check-in log stays append-only.

**Key concepts used:**
- **Spreadsheet ID:** The unique identifier in the sheet's URL (`/spreadsheets/d/<ID>/edit`).
- **Worksheet/Tab:** Both tabs are auto-created with their header rows on first use.
- **Row append / cell update:** `append_row()` for check-ins; targeted `update_cell()` for photo state.
- **`get_all_records()`:** Reads all rows as dicts keyed by the header row. Note it returns numeric-looking cells as `int`/`float`, so values are coerced to `str` before display.

---

## Google Cloud Platform (GCP)

**What it is:** Google's suite of cloud computing services. This project uses Cloud Run, Cloud Tasks, Cloud Scheduler, Cloud Build, Artifact Registry, and IAM — all within one project, all within the always-free tier.

**Role in this project:** Hosts the service *and* the **service account** that authenticates to the Sheets API without an interactive login.

**Key concepts used:**
- **Project:** A logical container grouping resources, APIs, and billing.
- **Service Account:** A non-human Google identity used by applications. A "robot Google account" for the bot.
- **Service Account Key (JSON):** Downloaded credentials containing a private key, supplied to the service as `GOOGLE_CREDENTIALS_JSON`.
- **API Enablement:** Each Google API must be explicitly enabled per-project.
- **IAM:** The Cloud Run runtime service account additionally needs `roles/cloudtasks.enqueuer` to enqueue its own followup work.
- **Cloud Build:** `gcloud run deploy --source .` uses it to build the Dockerfile into a container image stored in Artifact Registry.

**Website:** [console.cloud.google.com](https://console.cloud.google.com)

---

## gspread

**What it is:** A Python library providing a clean, high-level interface to the Google Sheets API v4.

**Version used:** `6.1.2`

**Role in this project:** All Google Sheets read/write operations in `sheets.py`.

**Key methods used:**
- `gspread.authorize()`: Creates an authenticated client from Google OAuth2 credentials.
- `client.open_by_key()`: Opens a spreadsheet by its ID.
- `spreadsheet.worksheet()` / `add_worksheet()`: Opens or creates a named tab.
- `ws.append_row()` / `ws.insert_row()` / `ws.update_cell()`: Writes.
- `ws.get_all_records()` / `ws.find()`: Reads.

**Documentation:** [docs.gspread.org](https://docs.gspread.org)

---

## google-auth

**What it is:** Google's official Python authentication library. Handles OAuth2 token acquisition, refresh, and signing using service account credentials.

**Version used:** `2.29.0`

**Role in this project:** Loads the service account JSON and produces signed tokens that gspread attaches to every API request. Also provides `google.auth.default()`, used to auto-detect the GCP project id at runtime when `GOOGLE_CLOUD_PROJECT` isn't set.

**Documentation:** [google-auth.readthedocs.io](https://google-auth.readthedocs.io)

---

## google-cloud-tasks

**What it is:** The Python client library for Cloud Tasks.

**Version used:** `2.16.4`

**Role in this project:** `tasks_queue.enqueue()` builds a `CloudTasksClient`, resolves the queue path, and creates an HTTP task pointing back at `/process`. Imported lazily inside the function so the module doesn't pay the (substantial) gRPC import cost on cold start unless a task is actually enqueued.

---

## requests

**What it is:** The standard Python HTTP client library.

**Version used:** `2.32.3`

**Role in this project:** Every call to Discord's REST API in `discord_api.py` — editing deferred responses, posting channel messages, fetching stored messages, and downloading photos. Multipart uploads (charts, photos) are built by hand as a `payload_json` part plus a `files[0]` part, which is Discord's required format.

**Documentation:** [requests.readthedocs.io](https://requests.readthedocs.io)

---

## matplotlib

**What it is:** Python's most widely used plotting library.

**Version used:** `3.9.2`

**Role in this project:** Two jobs. In `charts.py` it renders the `/progress` weight chart — line plot, least-squares trend line, annotations, styled to Discord's dark theme. In `images.py` it lays out the side-by-side before/after photo composite. Both render headlessly (`matplotlib.use("Agg")`) straight into an in-memory PNG, never touching disk.

**Documentation:** [matplotlib.org](https://matplotlib.org)

---

## numpy

**What it is:** The foundational array/numerics library for Python.

**Version used:** `2.1.3`

**Role in this project:** `np.polyfit()` computes the least-squares trend used for both the chart's trend line and the "pace" figure (lbs/week). Using a fit rather than first-vs-last endpoints means one bad weigh-in doesn't distort the reported pace.

---

## Pillow (PIL)

**What it is:** The Python imaging library — decoding, transforming, and encoding images.

**Version used:** whatever matplotlib pulls in (it's a matplotlib dependency, so it adds nothing to `requirements.txt`)

**Role in this project:** Progress-photo hygiene in `images.py`. `normalize()` decodes the upload, converts to RGB (which flattens alpha and **drops EXIF including GPS coordinates**), caps the largest dimension at 1600px, and re-encodes as PNG. `Image.MAX_IMAGE_PIXELS` is tightened to ~40 MP to bound decompression bombs.

**Documentation:** [pillow.readthedocs.io](https://pillow.readthedocs.io)

---

## GitHub

**What it is:** The most popular platform for hosting Git repositories, with built-in CI via GitHub Actions.

**Role in this project:** Stores the source code and runs the test suite. **It does not deploy** — unlike the old Railway setup, pushing to `main` triggers tests only; deployment is an explicit `gcloud run deploy`.

**Key concepts used:**
- **Private repository:** Only you and invited collaborators can see the code.
- **GitHub Actions** (`.github/workflows/tests.yml`): Runs `python tests/test_app.py` on Python 3.11 and 3.12 for every push to `main` and every pull request.
- **`.gitignore`:** Keeps secrets (`.env`, `env.yaml`, `credentials.json`) and generated files (`__pycache__`, `.venv`) out of the repo.

---

## python-dotenv

**What it is:** A Python library that loads environment variables from a `.env` file into `os.environ` at runtime.

**Version used:** `1.0.1`

**Role in this project:** `load_dotenv()` at the top of `app.py` and `register_commands.py` reads `.env` for local development and for the one-off command-registration script. On Cloud Run there is no `.env` — configuration comes from `env.yaml`, injected as real environment variables at deploy time.
