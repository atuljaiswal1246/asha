#!/bin/bash
# DEMO DAY launcher: engine + brain + Chrome as a standalone app window.
set -euo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"
export LLM_BASE_URL="${LLM_BASE_URL:-http://127.0.0.1:8080/v1}"

if ! curl -s "${LLM_BASE_URL%/v1}/health" 2>/dev/null | grep -q '"status":"ok"'; then
  echo "[demo] starting llama-server..."
  "$BASE/serve_local.sh"
fi

if ! curl -s -o /dev/null http://127.0.0.1:8000/; then
  echo "[demo] starting brain proxy..."
  nohup "$BASE/web_server.py" > /tmp/asha-demo.log 2>&1 &
  sleep 5
fi

echo "[demo] opening Asha in Chrome..."
open -a "Google Chrome" "http://127.0.0.1:8000"
echo "[demo] ready. Talk to Asha — first turn downloads voice models once (~30-60s)."
