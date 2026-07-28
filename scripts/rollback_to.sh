#!/usr/bin/env bash
#
# Roll production back to a tagged release by shifting Cloud Run traffic.
#
#   ./scripts/rollback_to.sh v1.0.0             # show what would happen (default)
#   ./scripts/rollback_to.sh v1.0.0 --execute   # actually shift traffic
#
# This is the fast path: no rebuild, no source upload, seconds to take effect,
# and reversible by running it again for another tag. Falls back to a redeploy
# only when the revision has been garbage-collected (see docs/V1_BASELINE.md).
#
# Dry run by default, because rolling production back is not something to do by
# typo. Nothing is changed unless --execute is passed.
set -euo pipefail

SERVICE="${SERVICE:-fitness-checkin-bot}"
REGION="${REGION:-us-west1}"

TAG="${1:-}"
EXECUTE="${2:-}"

if [ -z "$TAG" ]; then
  echo "usage: $0 <tag> [--execute]" >&2
  echo "  e.g. $0 v1.0.0" >&2
  exit 2
fi

cd "$(dirname "$0")/.."

# ^{commit} matters: an annotated tag resolves to the TAG OBJECT's sha, which
# never matches a revision label and would silently find nothing.
if ! SHA="$(git rev-parse "${TAG}^{commit}" 2>/dev/null)"; then
  echo "ERROR: no such tag or commit: $TAG" >&2
  echo "  known tags: $(git tag -l | tr '\n' ' ')" >&2
  exit 2
fi

REVISION="$(gcloud run revisions list --service="$SERVICE" --region="$REGION" \
  --format='value(metadata.name)' --filter="metadata.labels.commit=$SHA" | head -n1)"

if [ -z "$REVISION" ]; then
  echo "No Cloud Run revision was built from $TAG (${SHA:0:12})." >&2
  echo "  Either it predates commit labelling, or it has been garbage-collected." >&2
  echo "  Use the redeploy path instead:" >&2
  echo "    git checkout $TAG && ./scripts/redeploy.sh" >&2
  exit 1
fi

CURRENT="$(gcloud run services describe "$SERVICE" --region="$REGION" \
  --format='value(status.latestReadyRevisionName)')"

echo "tag:      $TAG (${SHA:0:12})"
echo "revision: $REVISION"
echo "current:  $CURRENT"

if [ "$REVISION" = "$CURRENT" ]; then
  echo "Already serving that revision — nothing to do."
  exit 0
fi

if [ "$EXECUTE" != "--execute" ]; then
  echo
  echo "Dry run. To actually roll back, re-run with --execute:"
  echo "  $0 $TAG --execute"
  exit 0
fi

gcloud run services update-traffic "$SERVICE" --region="$REGION" \
  --to-revisions="$REVISION=100"

echo
echo "Traffic now on $REVISION."
echo "Remaining manual step: if the newer version added or renamed slash commands,"
echo "re-register them from this tag or Discord will still show commands it can't answer:"
echo "  git checkout $TAG && python register_commands.py"
