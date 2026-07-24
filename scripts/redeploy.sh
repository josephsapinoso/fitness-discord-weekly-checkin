#!/usr/bin/env bash
#
# Redeploy the fitness check-in bot to Cloud Run after the progress-photos change
# (adds progress pictures, /day1 baseline, and before/after posts).
#
# This is a CODE-ONLY redeploy of an existing service — the one-time setup
# (APIs, Cloud Tasks queue, env.yaml, Discord Interactions Endpoint URL, Cloud
# Scheduler) is already done. Full first-time guide: docs/GCP_DEPLOY.md.
#
# Prerequisites on the machine that runs this:
#   • gcloud CLI installed and authenticated to the bot's GCP project
#   • env.yaml present and filled in (gitignored; same one used for prior deploys)
#   • .env present with DISCORD_TOKEN + DISCORD_APPLICATION_ID (for step 4)
#   • python + pip available (for register_commands.py)
#
# Safe to re-run. Override the service name/region if yours differ:
#   SERVICE=my-bot REGION=us-central1 ./scripts/redeploy.sh
#
set -euo pipefail

SERVICE="${SERVICE:-fitness-checkin-bot}"
REGION="${REGION:-us-west1}"

step() { printf '\n\033[1;36m▶ %s\033[0m\n' "$1"; }

cd "$(dirname "$0")/.."   # repo root, regardless of where this is invoked from

step "1/5  Sync merged code (main)"
git checkout main
git pull origin main

step "2/5  Deploy to Cloud Run  ($SERVICE / $REGION)"
if [ ! -f env.yaml ]; then
  echo "ERROR: env.yaml not found. Copy env.yaml.example to env.yaml and fill it in." >&2
  exit 1
fi
# No env.yaml changes are required: the new GOOGLE_PHOTOS_TAB (default "Photos")
# and MAX_IMAGE_BYTES (default 10 MiB) both have sensible defaults. requirements
# are unchanged (Pillow already ships with matplotlib), so this just rebuilds the
# container with the new code.
# --cpu-boost: Discord discards any interaction response that takes more than
# 3.000s, and a cold start (gunicorn + matplotlib import) measured 3.006s —
# just over the line, so the first /checkin after an idle period was lost.
# Startup CPU boost buys that back while still scaling to zero when idle.
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --memory 512Mi \
  --min-instances 0 \
  --max-instances 1 \
  --cpu-boost \
  --env-vars-file env.yaml

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
echo "Service URL: $URL"

step "3/5  Health check"
curl -fsS "$URL/" && echo "  -> ok"

step "4/5  Register slash commands (adds /day1)"
# Needs a local .env with DISCORD_TOKEN + DISCORD_APPLICATION_ID.
# Should print 5 commands: /checkin, /summary, /progress, /history, /day1
python register_commands.py

step "5/5  MANUAL — grant the bot two Discord permissions"
cat <<'EOF'
This step is NOT automatable via gcloud — do it in the Discord app:

  Server Settings -> the check-in channel (or the bot's role), enable:
    * Attach Files          (upload progress photos)
    * Read Message History  (re-fetch the Day 1 photo for before/after)

Without these, the text check-in still posts but photos silently fail to upload.
EOF

cat <<EOF

Redeploy complete. Smoke test in Discord:
  1. /checkin  -> the modal now shows a "Progress photo (optional)" upload field.
     Attach a photo, submit -> you get a one-time "Share it" opt-in -> a Day 1
     post appears in the check-in channel.
  2. A second /checkin with a photo -> a "Before & After" composite posts.
  3. A new "Photos" tab appears in the Google Sheet.

Rollback if needed (list revisions, route traffic to the previous one):
  gcloud run revisions list --service $SERVICE --region $REGION
  gcloud run services update-traffic $SERVICE --region $REGION --to-revisions <PREV_REVISION>=100
EOF
