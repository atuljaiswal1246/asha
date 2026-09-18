#!/bin/bash
# Stop Asha: kill supervisor + bot + app. opencode serve stays up (fast relaunch).
DIR="/Users/neharohilla/Desktop/Jarvis/prototype/ui"

echo "[stop] killing 24/7 supervisor..."
pkill -9 -f asha_supervise 2>/dev/null || true
sleep 1
pkill -9 -f "python -u server.py" 2>/dev/null || true
P=$(lsof -ti :7860 -sTCP:LISTEN 2>/dev/null || true)
[ -n "$P" ] && kill -9 "$P" 2>/dev/null || true

if [ -n "$(pgrep -f 'VoiceAssistant/Contents/MacOS' 2>/dev/null || true)" ]; then
  echo "[stop] quitting VoiceAssistant.app..."
  osascript -e 'quit app "VoiceAssistant"' 2>/dev/null || true
  sleep 1
  pkill -f "VoiceAssistant/Contents/MacOS" 2>/dev/null || true
fi

echo "[stop] done. bot processes: $(pgrep -f 'python -u server.py' | wc -l | tr -d ' ') | supervisors: $(pgrep -f asha_supervise | wc -l | tr -d ' ')"
