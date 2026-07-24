# TODO — remaining deploy steps for the progress-photos feature

The code and docs are merged and the test suite passes (85/85). These two steps push
the change to the live bot and can't be completed from the current environment. Full
procedure: [docs/REDEPLOY_CHECKLIST.md](docs/REDEPLOY_CHECKLIST.md).

## 1. Deploy to Cloud Run

`gcloud` is not installed on this machine, so `gcloud run deploy` / `scripts/redeploy.sh`
can't run here. Two options:

- **(a) Install the gcloud CLI locally** — install from
  <https://cloud.google.com/sdk/docs/install>, then:
  ```bash
  gcloud auth login
  gcloud config set project <project>
  ./scripts/redeploy.sh
  ```
- **(b) Deploy from Cloud Shell in the browser** — gcloud and auth are already present
  there. Paste the contents of `deploy_cloudshell.sh` (repo root) into
  <https://shell.cloud.google.com>. It is gitignored because it embeds secrets, so
  it will **not** be in the copy Cloud Shell clones — you have to paste it.

After deploying, health-check: `curl https://YOUR-SERVICE-URL/` should return `ok`.

## 2. Register slash commands

`.env` is missing `DISCORD_APPLICATION_ID` (the value is already in `env.yaml`).

1. Add `DISCORD_APPLICATION_ID=<value from env.yaml>` to `.env`.
2. Run `python register_commands.py`.
3. Verify it prints **5** commands (checkin, summary, progress, history, day1) — if it
   shows fewer, `/day1` won't appear in Discord.

## 3. Grant Discord permissions (manual, handled by the user)

The bot needs **Attach Files** and **Read Message History** in the check-in channel, or
photos silently fail. See REDEPLOY_CHECKLIST.md step 5.
