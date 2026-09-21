#!/usr/bin/env bash
# Deploy the dashboard (python + web bundle) to the jetson.
#
# WHY THIS EXISTS RATHER THAN A BARE RSYNC: a service worker only updates when
# the browser sees a byte-different sw.js. Copying new HTML/CSS/JS does NOT do
# that on its own, so already-installed clients keep running the old build
# indefinitely -- the classic PWA failure. This stamps sw.js with a content
# hash on every deploy, which is what makes installed clients notice.
#
# The stamp is a hash of the web bundle, not a timestamp, so redeploying
# unchanged files does not needlessly churn every client's cache.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ENVFILE="$HERE/../.env"
# shellcheck disable=SC1090
[ -f "$ENVFILE" ] && . "$ENVFILE"
NANO="${JETSON_USER:?set JETSON_USER in .env}@${JETSON_IP:?set JETSON_IP in .env}"
SRC="$HERE/../src/ups-dash"
REMOTE="~/projects/ups-dash"

cd "$SRC"

# Hash everything the browser actually loads. sw.js itself is excluded, since
# its own stamp is what we are about to compute.
STAMP=$(find web -type f \( -name '*.html' -o -name '*.js' -o -name '*.css' \
          -o -name '*.webmanifest' -o -name '*.png' \) ! -name 'sw.js' \
        | LC_ALL=C sort | xargs cat 2>/dev/null | sha256sum | cut -c1-12)

echo "build stamp: $STAMP"

rsync -az --delete --exclude '__pycache__' ./ "$NANO:$REMOTE/"

# Substitute on the remote so the working tree keeps its placeholder and the
# next deploy still has something to replace.
ssh -o ConnectTimeout=15 "$NANO" \
  "sed -i 's/__BUILD_STAMP__/$STAMP/' $REMOTE/web/sw.js && \
   grep -q '$STAMP' $REMOTE/web/sw.js && echo '  sw.js stamped'"

echo "deployed. Clients pick this up on their next load or within 15 minutes."
