#!/bin/bash
# Asha app backend: llama-server (if needed) + brain proxy (static dist + streaming Qwen).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BASE="$(dirname "$0")"
export LLM_BASE_URL="${LLM_BASE_URL:-http://127.0.0.1:8080/v1}"

if ! curl -s "${LLM_BASE_URL%/v1}/health" 2>/dev/null | grep -q '"status":"ok"'; then
  echo "[asha] starting llama-server..."
  "$BASE/serve_local.sh" >/dev/null 2>&1 || { echo "llama-server failed"; exit 1; }
fi

echo "[asha] brain proxy on http://127.0.0.1:8000"
exec "$ROOT/.venv/bin/python" -u "$BASE/web_server.py"
