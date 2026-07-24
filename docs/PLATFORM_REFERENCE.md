# Fitness Check-in Bot — Platform & Tools Reference

This document explains every platform, service, and library used in this project, why it was chosen, and how it fits into the overall architecture.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                     Discord Server                       │
│   Users type /checkin → Bot posts embed to channel      │
└────────────────────┬────────────────────────────────────┘
                     │ Discord Gateway (WebSocket)
┌────────────────────▼────────────────────────────────────┐
│                  Railway (Cloud Host)                    │
│   Runs bot.py 24/7 as a worker process                  │
│                                                          │
│   ┌──────────────┐    ┌────────────────────────────┐   │
│   │   bot.py     │───►│        sheets.py            │   │
│   │  discord.py  │    │  gspread + Google Auth      │   │
│   └──────────────┘    └────────────────┬───────────┘   │
└────────────────────────────────────────┼────────────────┘
                                         │ HTTPS API calls
┌────────────────────────────────────────▼────────────────┐
│              Google Sheets (Data Storage)                │
│   Spreadsheet: Fitness Check-in Bot                     │
│   Tab: Check-ins                                         │
└─────────────────────────────────────────────────────────┘
```

---

## Discord

**What it is:** A real-time messaging platform originally built for gaming communities, now widely used for group coordination of all kinds. It supports text channels, voice, video, and rich bot integrations.

**Role in this project:** The user interface. Members interact with the bot entirely within Discord — no separate app or website is needed. The bot lives in a private server channel dedicated to fitness check-ins.

**Key concepts used:**
- **Slash commands** (`/checkin`, `/summary`, `/history`): Registered commands that Discord surfaces with autocomplete in the chat input. Unlike message-based commands, slash commands are first-class Discord features with built-in discovery.
- **Modals**: Pop-up forms that Discord renders natively in the client when triggered by a command. Used here to collect all 5 check-in fields in one structured interaction.
- **Embeds**: Richly formatted message cards with titles, fields, colors, and thumbnails. Used to display formatted check-in summaries.
- **Gateway API**: A persistent WebSocket connection between the bot and Discord's servers. The bot stays connected and receives real-time events (slash command invocations, etc.) over this connection.

**Website:** [discord.com](https://discord.com) | **Developer Portal:** [discord.com/developers](https://discord.com/developers)

---

## Discord Developer Portal

**What it is:** Discord's web console for managing bot applications. Think of it as the "admin panel" for your bot's identity.

**Role in this project:** Used to create the bot application, generate the bot token (the password the bot uses to authenticate with Discord), and set up the OAuth2 invite URL.

**Key concepts used:**
- **Application:** The container for a Discord integration. Has an Application ID (client ID) and can have a Bot user attached to it.
- **Bot Token:** A secret string that authenticates your code as the bot user. Treat it like a password — anyone with the token can control your bot.
- **OAuth2 Scopes:** `bot` grants the bot permission to join servers; `applications.commands` lets it register and use slash commands.
- **Bot Permissions:** A bitmask of specific actions the bot is allowed to perform (Send Messages, Embed Links, Use Application Commands).

---

## discord.py

**What it is:** The most popular Python library for building Discord bots. Wraps Discord's REST API and Gateway (WebSocket) API into Pythonic abstractions.

**Version used:** `2.4.0`

**Role in this project:** The core of `bot.py`. Handles the WebSocket connection to Discord, registers slash commands, dispatches interaction events, and provides helper classes for building embeds and modals.

**Key components used:**
- `discord.Client`: The main bot class managing the connection lifecycle.
- `discord.app_commands.CommandTree`: Registers and handles slash commands.
- `discord.ui.Modal`: Base class for the check-in form.
- `discord.ui.TextInput`: Individual text fields inside the modal.
- `discord.Embed`: Builds the formatted check-in cards posted to the channel.
- `discord.ext.tasks`: The `@tasks.loop` decorator that runs the weekly reminder on a schedule.

**Documentation:** [discordpy.readthedocs.io](https://discordpy.readthedocs.io)

---

## Google Sheets

**What it is:** Google's cloud-based spreadsheet application, part of Google Workspace. Spreadsheets are stored in Google Drive and accessible via a REST API.

**Role in this project:** The persistent data store for all check-ins. Every submission is appended as a row with a timestamp, user info, and all five check-in fields. Chosen because it's free, requires no database setup, and gives the group a human-readable view of their history with built-in charting and filtering.

**Key concepts used:**
- **Spreadsheet ID:** The unique identifier in the sheet's URL (`/spreadsheets/d/<ID>/edit`). Used to target the correct file via the API.
- **Worksheet/Tab:** A named sheet within a spreadsheet. The bot uses a tab called `Check-ins`.
- **Row append:** The primary write operation — adds a new row at the bottom of the sheet with each check-in.
- **`get_all_records()`:** Reads all rows as a list of dictionaries keyed by the header row, used to generate the `/summary` embed.

---

## Google Cloud Platform (GCP)

**What it is:** Google's suite of cloud computing services. For this project, only the IAM (Identity and Access Management) and API enablement features are used — no compute or storage resources are provisioned.

**Role in this project:** Hosts the **service account** that allows the bot to authenticate with Google's APIs without requiring a user to log in interactively.

**Key concepts used:**
- **Project:** A logical container in GCP that groups resources, APIs, and billing. All API usage and service accounts are scoped to a project.
- **Service Account:** A non-human Google identity used by applications (rather than people) to authenticate with Google APIs. Think of it as a "robot Google account" for your bot.
- **Service Account Key (JSON):** A downloaded credentials file containing a private key. The bot uses this to sign API requests, proving it is authorized to access your spreadsheet.
- **API Enablement:** Each Google API must be explicitly enabled per-project before it can be called. This project enables the Google Sheets API and Google Drive API.
- **IAM (Identity and Access Management):** Google's system for controlling who (or what) can access which resources. The service account is granted Editor access to the specific spreadsheet by sharing the sheet with its email address.

**Website:** [console.cloud.google.com](https://console.cloud.google.com)

---

## gspread

**What it is:** A Python library that provides a clean, high-level interface to the Google Sheets API v4. Handles authentication, pagination, and data serialization.

**Version used:** `6.1.2`

**Role in this project:** All Google Sheets read/write operations in `sheets.py` go through gspread. It abstracts away the raw HTTP calls to the Sheets API.

**Key methods used:**
- `gspread.authorize()`: Creates an authenticated client using Google OAuth2 credentials.
- `client.open_by_key()`: Opens a spreadsheet by its ID.
- `spreadsheet.worksheet()` / `spreadsheet.add_worksheet()`: Opens or creates a named tab.
- `ws.append_row()`: Appends a list of values as a new row.
- `ws.get_all_records()`: Returns all rows as a list of dicts.

**Documentation:** [docs.gspread.org](https://docs.gspread.org)

---

## google-auth

**What it is:** Google's official Python authentication library. Handles OAuth2 token acquisition, refresh, and signing using service account credentials.

**Version used:** `2.29.0`

**Role in this project:** Used by `sheets.py` to load the service account JSON credentials and produce a signed token that gspread attaches to every API request. Works behind the scenes — you don't call it directly in application code.

**Documentation:** [google-auth.readthedocs.io](https://google-auth.readthedocs.io)

---

## Railway

**What it is:** A modern cloud hosting platform designed for simplicity. Supports any language or framework, auto-detects build configuration, and offers a generous free tier. Comparable to Heroku but more developer-friendly.

**Role in this project:** Hosts and runs the bot as a **worker process** (a long-running background process with no HTTP server). The bot stays connected to Discord 24/7 without requiring your personal computer to be on.

**Key concepts used:**
- **Worker:** A Railway service type for processes that run continuously but don't serve HTTP traffic. Defined by the `worker:` entry in `Procfile`.
- **Procfile:** A text file at the project root that tells Railway (and Heroku-compatible platforms) how to start the application. In this project: `worker: python bot.py`.
- **Environment Variables:** Railway injects all configured variables into the process environment at runtime. This is how the bot gets its token, channel ID, and credentials without them being hardcoded.
- **Nixpacks:** Railway's build system that auto-detects the language (Python) and installs dependencies from `requirements.txt` without a `Dockerfile`.
- **Free tier:** Railway gives $5 of credit per month. A simple Python worker uses roughly $0.50–$1.00/month, comfortably within the free allowance.
- **GitHub integration:** Railway connects to a GitHub repo and automatically re-deploys whenever you push a new commit to the `main` branch.

**Website:** [railway.app](https://railway.app)

---

## GitHub

**What it is:** The world's most popular platform for hosting Git repositories. Provides version control, collaboration tools, and integrations with deployment platforms.

**Role in this project:** Stores the bot's source code and serves as the deployment trigger for Railway. Every `git push` to `main` automatically kicks off a new Railway build and deployment.

**Key concepts used:**
- **Private repository:** The repo is private, meaning only you (and explicitly invited collaborators) can see the code. Important since the repo structure reveals the bot's capabilities.
- **GitHub App (Railway):** Railway installs a GitHub App with read access to your repo so it can pull code on each deploy.
- **`.gitignore`:** Prevents secrets (`.env`, `credentials.json`) and generated files (`__pycache__`) from being committed to the repo.

---

## python-dotenv

**What it is:** A Python library that loads environment variables from a `.env` file into `os.environ` at runtime.

**Version used:** `1.0.1`

**Role in this project:** When running the bot locally for testing, `load_dotenv()` at the top of `bot.py` reads the `.env` file so the bot has access to all configuration without needing to manually export environment variables in your shell. On Railway, the `.env` file is not used — Railway injects the variables directly.
