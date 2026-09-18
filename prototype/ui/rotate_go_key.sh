#!/bin/bash
# Rotate the ACTIVE OpenCode Go API key across the user's key pool.
# Quota is PER-KEY (weekly reset), so switching the active key multiplies
# usable go budget. The key POOL (.env OPENCODE_API_KEY / OPENCODE_API_KEY_2)
# is fixed inventory and is NEVER overwritten. Only the ACTIVE key
# (SUPERVISOR_API_KEY in .env + auth.json opencode-go/opencode) rotates to
# the lowest-usage key. Checks usage via the gateway's /usage endpoint.
set -e

ENV_FILE="$(dirname "$0")/../.env"
AUTH_FILE="$HOME/.local/share/opencode/auth.json"

usage_pct() {
  local key="$1"
  python3 - "$key" << 'PYEOF'
import json, sys, urllib.request, uuid
key = sys.argv[1]
req = urllib.request.Request(
    "https://opencode.ai/zen/go/v1/usage",
    headers={"Authorization": "Bearer " + key,
             "x-opencode-session": uuid.uuid4().hex,
             "User-Agent": "asha-rotate/1.0"},
)
try:
    r = json.load(urllib.request.urlopen(req, timeout=10))
    u = r.get("usage", {}).get("weekly", {})
    print(u.get("percent", -1))
except Exception:
    print(-1)
PYEOF
}

echo "=== key pool usage (weekly %) ==="
POOL_VARS=(OPENCODE_API_KEY OPENCODE_API_KEY_2)
POOL=()
for var in "${POOL_VARS[@]}"; do
  key=$(grep -E "^$var=" "$ENV_FILE" | head -1 | cut -d= -f2-)
  if [ -n "$key" ]; then
    pct=$(usage_pct "$key")
    printf "  %-18s → %s%%\n" "$var" "$pct"
    POOL+=("$key")
  fi
done

echo ""
echo "=== picking the lowest-usage key ==="
BEST=""
BEST_PCT=101
for key in "${POOL[@]}"; do
  pct=$(usage_pct "$key")
  if [ "$pct" -ge 0 ] && [ "$pct" -lt "$BEST_PCT" ]; then
    BEST_PCT=$pct
    BEST=$key
  fi
done

if [ -z "$BEST" ]; then
  echo "ERROR: no usable key found"
  exit 1
fi

ACTIVE=$(grep -E "^SUPERVISOR_API_KEY=" "$ENV_FILE" | head -1 | cut -d= -f2-)
if [ "$ACTIVE" = "$BEST" ]; then
  echo "Best key already active (${BEST:0:8}... @ ${BEST_PCT}%). No change."
  exit 0
fi

echo "Switching ACTIVE key → ${BEST:0:8}... (weekly ${BEST_PCT}%)"

# Backup before editing
cp "$AUTH_FILE" "$AUTH_FILE.bak"
cp "$ENV_FILE" "$ENV_FILE.bak"

# 1. .env: only SUPERVISOR_API_KEY (the ACTIVE slot) changes.
python3 - "$ENV_FILE" "$BEST" << 'PYEOF'
import re, sys
path, key = sys.argv[1], sys.argv[2]
lines = open(path).read().splitlines()
out = []
for ln in lines:
    if re.match(r"^SUPERVISOR_API_KEY=", ln):
        out.append(re.split(r"=", ln, maxsplit=1)[0] + "=" + key)
    else:
        out.append(ln)
open(path, "w").write("\n".join(out) + "\n")
PYEOF

# 2. auth.json: opencode-go and opencode providers use the new active key.
python3 - "$AUTH_FILE" "$BEST" << 'PYEOF'
import json, sys
path, key = sys.argv[1], sys.argv[2]
d = json.load(open(path))
for prov in ("opencode-go", "opencode"):
    if prov in d:
        d[prov]["key"] = key
json.dump(d, open(path, "w"), indent=1)
PYEOF

echo "Updated .env (SUPERVISOR_API_KEY) + auth.json. Restart the bot to pick up:"
echo "  pkill -9 -f 'python -u server.py' && cd prototype/ui && nohup .venv/bin/python -u server.py > /tmp/asha-ws.log 2>&1 &"
echo "A NEW opencode session (for you) will use the new key automatically."