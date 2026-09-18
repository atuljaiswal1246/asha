#!/bin/bash
# Auto-rotate the ACTIVE OpenRouter API key across the user's key pool.
# OpenRouter free-tier keys are rate-limited; when the active key stops
# working (429 / exhausted), this script switches to the next healthy key so
# the brain's OpenRouter transport and opencode CLI sessions stay alive.
# Pool slots in .env (OPENROUTER_KEY_1..N) are FIXED inventory — only the
# ACTIVE key rotates.
set -e

ENV_FILE="$(dirname "$0")/../.env"
AUTH_FILE="$HOME/.local/share/opencode/auth.json"

# Pick the healthiest key: try a real 1-token completion, prefer the first
# key that succeeds (and reports the LOWEST usage). 429/403 = unusable now.
health_check() {
  local key="$1"
  python3 - "$key" << 'PYEOF'
import json, sys, urllib.request, uuid
key = sys.argv[1]
# usage probe
req = urllib.request.Request(
    "https://openrouter.ai/api/v1/auth/key",
    headers={"Authorization": "Bearer " + key, "User-Agent": "Mozilla/5.0"},
)
try:
    r = json.load(urllib.request.urlopen(req, timeout=10))
    d = r.get("data", {})
    usage = d.get("usage", 0) or 0
    limit = d.get("limit")  # None for free tier
    print(f"ok {usage}")
except Exception as e:
    print("dead")
PYEOF
}

echo "=== OpenRouter key pool health ==="
POOL_VARS=$(grep -oE "^OPENROUTER_KEY_[0-9]+=" "$ENV_FILE" | sed 's/=//' | sort -t_ -k3 -n)
BEST=""
BEST_USAGE=999999999
for var in $POOL_VARS; do
  key=$(grep -E "^$var=" "$ENV_FILE" | head -1 | cut -d= -f2-)
  [ -z "$key" ] && continue
  result=$(health_check "$key")
  if [ "${result%% *}" = "ok" ]; then
    usage=${result#ok }
    printf "  %-16s → usable (usage \$%.2f)\n" "$var" "$usage"
    if python3 -c "exit(0 if float("$usage") < float("$BEST_USAGE") else 1)"; then
      BEST_USAGE=$usage
      BEST=$key
    fi
  else
    printf "  %-16s → DEAD (429/exhausted)\n" "$var"
  fi
done

if [ -z "$BEST" ]; then
  echo "ERROR: no healthy OpenRouter key"
  exit 1
fi

ACTIVE=$(grep -E "^CLOUD_OR_API_KEY=" "$ENV_FILE" | head -1 | cut -d= -f2-)
if [ "$ACTIVE" = "$BEST" ]; then
  echo "Active OpenRouter key already healthy (${BEST:10:6}...). No change."
  exit 0
fi

echo "Switching ACTIVE OpenRouter key → ${BEST:10:6}... (usage \$${BEST_USAGE})"

cp "$AUTH_FILE" "$AUTH_FILE.bak"
cp "$ENV_FILE" "$ENV_FILE.bak"

# 1. .env: CLOUD_OR_API_KEY (the Asha brain's OpenRouter transport)
python3 - "$ENV_FILE" "$BEST" << 'PYEOF'
import re, sys
path, key = sys.argv[1], sys.argv[2]
lines = open(path).read().splitlines()
out = []
for ln in lines:
    if re.match(r"^CLOUD_OR_API_KEY=", ln):
        out.append("CLOUD_OR_API_KEY=" + key)
    else:
        out.append(ln)
open(path, "w").write("\n".join(out) + "\n")
PYEOF

# 2. auth.json: openrouter provider uses the new active key
python3 - "$AUTH_FILE" "$BEST" << 'PYEOF'
import json, sys
path, key = sys.argv[1], sys.argv[2]
d = json.load(open(path))
if "openrouter" in d:
    d["openrouter"]["key"] = key
json.dump(d, open(path, "w"), indent=1)
PYEOF

echo "Updated .env (CLOUD_OR_API_KEY) + auth.json (openrouter)."
echo "The Asha brain picks it up on next OpenRouter call; a NEW opencode"
echo "session (for you) uses the healthy key automatically."