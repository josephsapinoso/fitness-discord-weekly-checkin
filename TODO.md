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

## 3. Grant Discord permissions — **still outstanding, manual**

The bot needs **Attach Files** and **Read Message History** in the check-in channel.
Without both, the text check-in still posts but **photos silently fail**. Either set
them on the channel/role in the Discord app, or re-invite with the corrected bitmask:

```
https://discord.com/oauth2/authorize?client_id=1512317224180781119&scope=bot+applications.commands&permissions=2147600384
```

`2147600384` = Send Messages + Embed Links + Attach Files + Read Message History +
Use Application Commands.

## 4. Smoke test in Discord — **still outstanding**

- [ ] `/checkin` → the modal shows a **"Progress photo (optional)"** upload field
- [ ] Attach a photo + submit → one-time **"Share it 📸"** opt-in → tapping it posts a
      **Day 1** message to the channel
- [ ] A second `/checkin` with a photo → a **Before & After** composite posts
- [ ] A **Photos** tab has appeared in the Google Sheet
- [ ] `/checkin` with **no** photo still works exactly as before
