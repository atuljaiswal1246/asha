#!/bin/bash
# Phase 0 run: ensure llama-server is up, then run the instrumented bot.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LLM_URL="http://127.0.0.1:8080"
export LLM_BASE_URL="$LLM_URL/v1"
export NLTK_ALLOW_PROXIED_URLOPEN="${NLTK_ALLOW_PROXIED_URLOPEN:-1}"

if ! curl -s "$LLM_URL/health" 2>/dev/null | grep -q '"status":"ok"'; then
  echo "[run] llama-server not running — starting it via ./serve_local.sh"
  "$(dirname "$0")/serve_local.sh"
fi

if [ -z "${AUDIO_IN_DEVICE:-}" ]; then
  echo "[run] hint: set AUDIO_IN_DEVICE / AUDIO_OUT_DEVICE in prototype/.env (0..N from device list)."
fi

cd "$ROOT/prototype/baseline"
exec "$ROOT/.venv/bin/python" -u bot.py
