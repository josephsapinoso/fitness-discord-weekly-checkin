#!/usr/bin/env bash
#
# Is production actually running origin/main?
#
# Deploying is not the only way prod and main diverge — the failure on
# 2026-07-27 was the opposite: PR #3 was merged and then simply never deployed,
# so a fix everyone believed was live had never run. A deploy-time guard cannot
# catch that, because no deploy happens. This is the check you can run any time
# (or from CI on a schedule) to answer the question directly.
#
# Read-only: describes the service and reads git refs. Changes nothing.
#
# Exit codes:  0 = in sync   1 = drifted   2 = cannot determine
set -euo pipefail

SERVICE="${SERVICE:-fitness-checkin-bot}"
REGION="${REGION:-us-west1}"

cd "$(dirname "$0")/.."

git fetch -q origin main
MAIN_SHA="$(git rev-parse origin/main)"

LIVE_SHA="$(gcloud run services describe "$SERVICE" --region "$REGION" \
  --format='value(spec.template.metadata.labels.commit)' 2>/dev/null || true)"
REVISION="$(gcloud run services describe "$SERVICE" --region "$REGION" \
  --format='value(status.latestReadyRevisionName)' 2>/dev/null || true)"

if [ -z "$LIVE_SHA" ]; then
  echo "UNKNOWN: revision $REVISION carries no 'commit' label."
  echo "  Revisions deployed before this label existed can't be identified."
  echo "  Run scripts/redeploy.sh once to start stamping them."
  exit 2
fi

if [ "$LIVE_SHA" = "$MAIN_SHA" ]; then
  echo "IN SYNC: $REVISION is origin/main @ ${MAIN_SHA:0:12}"
  exit 0
fi

echo "DRIFTED: production is not running origin/main." >&2
echo "  live ($REVISION): ${LIVE_SHA:0:12}" >&2
echo "  origin/main:      ${MAIN_SHA:0:12}" >&2
if git merge-base --is-ancestor "$LIVE_SHA" "$MAIN_SHA" 2>/dev/null; then
  echo "  Prod is BEHIND main. Commits merged but never deployed:" >&2
  git log --oneline "$LIVE_SHA..$MAIN_SHA" >&2
  echo "  Fix: ./scripts/redeploy.sh" >&2
else
  echo "  Prod is not an ancestor of main — it was deployed from a branch or a stale tree." >&2
fi
exit 1
