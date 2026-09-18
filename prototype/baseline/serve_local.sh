#!/bin/bash
# Start the local llama.cpp server (Phase 0 stack). Restart-friendly: kills old one first.
set -euo pipefail
MODEL="${QWEN_GGUF:-$HOME/Models/Qwen3.5-4B-Q4_K_M.gguf}"
PORT="${LLAMA_PORT:-8080}"

if [ ! -f "$MODEL" ]; then
  echo "Model not found: $MODEL" >&2
  echo "Download: curl -sL -o $MODEL https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/main/Qwen3.5-4B-Q4_K_M.gguf" >&2
  exit 1
fi

pkill -f "llama-server" 2>/dev/null && sleep 2 || true
nohup llama-server -m "$MODEL" --host 127.0.0.1 --port "$PORT" -c 4096 -np 1 \
  --chat-template-kwargs '{"enable_thinking": false}' \
  --log-file /tmp/llama-server.log > /tmp/llama-server.out 2>&1 &

echo "llama-server starting on http://127.0.0.1:$PORT ..."
for i in $(seq 1 30); do
  if curl -s "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '"status":"ok"'; then
    echo "ready."
    exit 0
  fi
  sleep 2
done
echo "server did not become ready; check /tmp/llama-server.log" >&2
exit 1
