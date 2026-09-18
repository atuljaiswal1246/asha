#!/bin/bash
# Phase 0-UI: llama-server (if needed) + pipecat WS server + static page.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
: "${LLM_BASE_URL:=http://127.0.0.1:8080/v1}"
export LLM_BASE_URL

BASE="$(dirname "$0")"

if ! curl -s "${LLM_BASE_URL%/v1}/health" 2>/dev/null | grep -q '"status":"ok"'; then
  echo "[ui] starting llama-server via serve_local.sh"
  ( cd "$BASE" && ./serve_local.sh ) || ( echo "[ui] opening new terminal for setup — run: $BASE/serve_local.sh" && exit 1 )
fi

echo "[ui] starting WS bot on :7860 and static UI on :8000"
( cd "$BASE" && exec "$ROOT/.venv/bin/python" -u server.py ) &
(cd "$BASE" && exec "$ROOT/.venv/bin/python" -m http.server 8000 --bind 127.0.0.1 --directory static) &
wait
