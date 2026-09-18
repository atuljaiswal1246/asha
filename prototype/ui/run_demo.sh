#!/bin/bash
# DEMO launcher (avatar-free): llama-server + pipecat WS bot (Moonshine/Qwen/Kokoro local) + static UI + app.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BASE="$(dirname "$0")"
export LLM_BASE_URL="${LLM_BASE_URL:-http://127.0.0.1:8080/v1}"

if ! curl -s "${LLM_BASE_URL%/v1}/health" 2>/dev/null | grep -q '"status":"ok"'; then
  echo "[demo] starting llama-server..."
  "$BASE/serve_local.sh" >/dev/null 2>&1 || { echo "llama-server failed"; exit 1; }
fi

pkill -f "web_server.py" 2>/dev/null || true
pkill -9 -f "python -u server.py" 2>/dev/null || true
pkill -f "http.server 8000" 2>/dev/null || true
sleep 1

( cd "$BASE" && nohup "$ROOT/.venv/bin/python" -u server.py > /tmp/asha-ws.log 2>&1 & )
( cd "$BASE" && nohup "$ROOT/.venv/bin/python" -m http.server 8000 --bind 127.0.0.1 --directory static > /tmp/asha-static.log 2>&1 & )
sleep 4
curl -s -o /dev/null -w "ui:%{http_code} " http://127.0.0.1:8000/ || true
echo "ready. Open the app (VoiceAssistant.app) — no downloads needed this time."
