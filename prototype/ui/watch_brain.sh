#!/bin/bash
# Watches the Kaggle dataset mailbox for the brain URL; auto-switches the app when found.
set -euo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"
PULL=/tmp/asha_pull
mkdir -p "$PULL"
LAST=""

echo "[watch] polling asha-brain-link mailbox..."
while true; do
  "$BASE/../../.venv/bin/kaggle" datasets download -d atuljaiswal1246/asha-brain-link -p "$PULL" --unzip -f -q 2>/dev/null || true
  URL=$(cat "$PULL/link.txt" 2>/dev/null | tr -d '[:space:]')
  if [ -n "$URL" ] && [[ "$URL" == *trycloudflare.com* ]] && [ "$URL" != "$LAST" ]; then
    LAST="$URL"
    echo "[watch] NEW BRAIN URL: $URL  -> auto-switching"
    "$BASE/llm_brain.sh" "$URL" || echo "[watch] switch failed"
    echo "[watch] switched. will watch for URL changes (re-push when session renews)."
  fi
  sleep 60
done
