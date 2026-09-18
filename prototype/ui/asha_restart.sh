#!/bin/bash
# Restart Asha's VoicePipeline server — single instance, always.
set -euo pipefail

VENV="/Users/neharohilla/Desktop/Jarvis/.venv/bin/python"
UI_DIR="/Users/neharohilla/Desktop/Jarvis/prototype/ui"
PID_FILE="/tmp/asha-server.pid"
LOG="/tmp/asha-ws.log"

if [ "${1:-start}" = "stop" ]; then
  echo "[stop] killing Asha server..."
  if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null || true)
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
      kill -9 "$OLD_PID" 2>/dev/null || true
    fi
  fi
  pkill -9 -f "python -u server.py" 2>/dev/null || true
  FPORT=$(lsof -ti :7860 -sTCP:LISTEN 2>/dev/null || true)
  if [ -n "$FPORT" ]; then kill -9 $FPORT 2>/dev/null || true; fi
  sleep 1
  LEFT=$(pgrep -f "python -u server.py" | wc -l | tr -d ' ')
  echo "[stop] remaining processes: $LEFT"
  exit 0
fi

echo "[1/5] stopping existing Asha servers..."
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE" 2>/dev/null || true)
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    kill -9 "$OLD_PID" 2>/dev/null || true
    echo "    killed pid from pidfile: $OLD_PID"
  fi
fi
pkill -9 -f "python -u server.py" 2>/dev/null || true
# free the port even if something else squats on it
FPORT=$(lsof -ti :7860 -sTCP:LISTEN 2>/dev/null || true)
if [ -n "$FPORT" ]; then
  kill -9 $FPORT 2>/dev/null || true
  echo "    killed port owner: $FPORT"
fi

echo "[2/5] waiting for port 7860 to be free..."
for i in $(seq 1 20); do
  if [ -z "$(lsof -ti :7860 -sTCP:LISTEN 2>/dev/null || true)" ]; then break; fi
  sleep 0.5
done
if [ -n "$(lsof -ti :7860 -sTCP:LISTEN 2>/dev/null || true)" ]; then
  echo "ERROR: port 7860 is still occupied. Aborting."; exit 1
fi

echo "[3/5] starting Asha server..."
rm -f "$PID_FILE"
cd "$UI_DIR"
nohup "$VENV" -u server.py > "$LOG" 2>&1 < /dev/null &
disown

echo "[4/5] waiting for ready..."
for i in $(seq 1 24); do
  if grep -aq "pipeline is now ready" "$LOG" 2>/dev/null; then break; fi
  sleep 0.5
done
sleep 1

echo "[5/5] verification:"
COUNT=$(pgrep -f "python -u server.py" | wc -l | tr -d ' ')
PID=$(lsof -ti :7860 -sTCP:LISTEN 2>/dev/null | head -1 || true)
echo "    processes : $COUNT"
echo "    pid       : $PID"
if [ "$COUNT" != "1" ]; then
  echo "WARNING: expected exactly 1 process, found $COUNT!"
fi
echo "    ready     : $(grep -ac 'pipeline is now ready' "$LOG" || true)"
