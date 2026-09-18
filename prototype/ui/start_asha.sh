#!/bin/bash
# Start Asha (24/7): detached background supervisor (auto-restarts bot on any
# death, keeps opencode + static UI alive) + VoiceAssistant app. One command.
# Survives app close / crashes. Resurrects after reboot with: bash start_asha.sh
set -euo pipefail
DIR="/Users/neharohilla/Desktop/Jarvis/prototype/ui"
SV="/tmp/asha-supervisor.log"
LOCK="/tmp/asha-start.lock"

# Single-instance guard for start_asha.sh itself (portable: no flock on macOS)
if mkdir "$LOCK" 2>/dev/null; then
    trap 'rm -rf "$LOCK"' EXIT
else
    echo "[start] another start_asha.sh is running. Exiting."
    exit 0
fi

chmod +x "$DIR/asha_supervise.sh"
chmod +x "$DIR/macapp/VoiceAssistant.app/Contents/MacOS/VoiceAssistant" 2>/dev/null || true

echo "[start] stopping any previous supervisor..."
pkill -9 -f asha_supervise.sh 2>/dev/null || true
# Wait until every old supervisor is gone so the new one is the only instance.
for i in $(seq 1 15); do
  pgrep -f asha_supervise.sh >/dev/null 2>&1 || break
  sleep 1
done
pkill -9 -f "python -u server.py" 2>/dev/null || true
sleep 1

echo "[start] launching 24/7 supervisor in background..."
nohup bash "$DIR/asha_supervise.sh" > "$SV" 2>&1 < /dev/null &
disown

echo "[start] waiting for bot (up to ~30s)..."
for i in $(seq 1 30); do
  if grep -aq "pipeline is now ready" /tmp/asha-ws.log 2>/dev/null; then break; fi
  sleep 1
done
sleep 2

echo "[start] opening VoiceAssistant.app..."
open "$DIR/macapp/VoiceAssistant.app"

echo "[start] done."
echo "    bot pid        : $(pgrep -f 'python -u server.py' | head -1)"
echo "    supervisor     : $(pgrep -fl asha_supervise | head -1 | awk '{print $1}')"
echo "    ui             : $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/ || echo DOWN)"
echo ""
echo "    Asha is now running 24/7 — she survives app closes, crashes,"
echo "    the 5-min idle kill (fixed), and everything else until shutdown."
echo "    After reboot: run this same command again."
