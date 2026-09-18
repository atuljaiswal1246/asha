#!/bin/bash
# One-shot: point Asha at the Kaggle brain and relaunch everything.
# Usage: ./llm_brain.sh https://xyz.trycloudflare.com
set -euo pipefail
URL="${1:?usage: llm_brain.sh <trycloudflare-url> (with or without /v1)}"
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BASE="$(cd "$(dirname "$0")" && pwd)"

BASE_URL="${URL%/}"
[[ "$BASE_URL" != */v1 ]] && BASE_URL="$BASE_URL/v1"

# 1) env switch
python3 - "$ENV_FILE" "$BASE_URL" <<'EOF'
import re, sys
p, url = sys.argv[1], sys.argv[2]
s = open(p).read()
s = re.sub(r'^LLM_BASE_URL=.*$', f'LLM_BASE_URL={url}', s, flags=re.M)
open(p, 'w').write(s)
print("LLM_BASE_URL ->", url)
EOF

# 2) keep local brain booted as fallback (don't kill python llama; it's the fallback)
true

# 3) restart WS bot + open app
pkill -f "ui/server.py" 2>/dev/null || true
sleep 2
( cd "$BASE" && nohup "$ROOT/.venv/bin/python" -u server.py > /tmp/asha-ws.log 2>&1 & )
sleep 6
pkill -f VoiceAssistant 2>/dev/null || true
sleep 1
open "$BASE/macapp/VoiceAssistant.app"

# 4) verify the remote brain answers through the tunnel
echo "verifying remote brain... (first gen can take ~30s on cold server)"
for i in $(seq 1 6); do
  R=$(curl -s --max-time 25 "$BASE_URL/chat/completions" \
    -H "Content-Type: application/json" -H "Authorization: Bearer sk-asha" \
    -d '{"messages":[{"role":"user","content":"Say OK."}],"max_tokens":4,"stream":false}' 2>/dev/null \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['choices'][0]['message']['content'])" 2>/dev/null || echo "")
  if [ -n "$R" ]; then echo "BRAIN OK: $R"; exit 0; fi
  echo "  attempt $i failed, waiting..."; sleep 10
done
echo "BRAIN NOT REACHABLE YET — app is running, talk will fail until the tunnel is up. Check the Kaggle cell."
exit 1
