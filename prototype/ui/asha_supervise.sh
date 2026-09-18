#!/bin/bash
# Asha 24/7 supervisor: keeps opencode serve + WS bot + static UI alive.
# Runs until the Mac shuts down. Self-heals on any crash.
# (llama-server no longer managed — local LLM removed by user decision 2026-09-11;
#  all brain work runs on OpenCode: free zen first, paid go as backup.)

UI="/Users/neharohilla/Desktop/Jarvis/prototype/ui"
VENV="/Users/neharohilla/Desktop/Jarvis/.venv/bin/python"
LOG="/tmp/asha-ws.log"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
ASHA_ROOT="/Users/neharohilla/Desktop/Jarvis"
OPENCODE_BIN="/opt/homebrew/bin/opencode"
OPENCODE_LOG="/tmp/opencode-serve.log"

SUPERVISOR_PIDFILE="/tmp/asha-supervisor.pid"
SUPERVISOR_LOCKDIR="/tmp/asha-supervisor.lock"
OWN_LOCK=0

# --- single-instance guard (atomic) ---
# `mkdir` is atomic: exactly one racer creates the dir. The old
# check-then-write pidfile was racy, so a second supervisor could start; its
# EXIT trap then ran `pkill -9 -f "python -u server.py"` and killed the live
# server mid-work. The lock dir also makes cleanup ownership-aware.
acquire_lock() {
    if mkdir "$SUPERVISOR_LOCKDIR" 2>/dev/null; then
        OWN_LOCK=1
    else
        OLD_PID=$(cat "$SUPERVISOR_PIDFILE" 2>/dev/null)
        if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
            echo "$(date '+%F %T') [supervisor] another supervisor already running (pid $OLD_PID). Exiting."
            return 1
        fi
        # Stale lock (owner gone) — reclaim it atomically.
        rm -rf "$SUPERVISOR_LOCKDIR"
        if mkdir "$SUPERVISOR_LOCKDIR" 2>/dev/null; then
            OWN_LOCK=1
        else
            echo "$(date '+%F %T') [supervisor] lock contention — another supervisor won. Exiting."
            return 1
        fi
    fi
    echo $$ > "$SUPERVISOR_PIDFILE"
    echo $$ > "$SUPERVISOR_LOCKDIR/pid"
    return 0
}

acquire_lock || exit 0

cleanup() {
    # Only the lock owner may tear down the shared processes.
    [ "$OWN_LOCK" = "1" ] || exit 0
    log "supervisor stopping (queueing shutdown)..."
    rm -f "$SUPERVISOR_PIDFILE"
    rm -rf "$SUPERVISOR_LOCKDIR"
    pkill -9 -f "python -u server.py" 2>/dev/null || true
    if [ -z "${KEEP_OPENCODE:-}" ]; then
        pkill -9 -f "opencode serve" 2>/dev/null || true
    fi
    exit 0
}
trap cleanup TERM INT EXIT

log() { echo "$(date '+%F %T') [supervisor] $*"; }

ensure_static() {
  if ! lsof -ti :8000 -sTCP:LISTEN > /dev/null 2>&1; then
    log "static UI down — restoring..."
    ( cd "$UI" && nohup "$VENV" static_server.py > /tmp/asha-static.log 2>&1 < /dev/null & )
  fi
}

load_opencode_auth() {
  # Serve reads auth from its environment; values live in gitignored .env.
  # Parse auth lines only (never log values).
  local envfile="$ASHA_ROOT/prototype/.env"
  [ -f "$envfile" ] || return 0
  local key val
  for key in OPENCODE_SERVER_USER OPENCODE_SERVER_USERNAME OPENCODE_SERVER_PASSWORD; do
    val=$(grep -E "^${key}=" "$envfile" 2>/dev/null | tail -1 | cut -d= -f2-)
    [ -n "$val" ] && export "$key=$val"
  done
}

ensure_opencode() {
  # M2 coding daemon: local opencode serve on :4096 (session workspace = repo root).
  load_opencode_auth
  # The serve must resolve provider keys from auth.json exactly like the
  # interactive app does. A stray OPENCODE_API_KEY in the environment (e.g. an
  # earlier placeholder "#") would shadow auth.json and make EVERY
  # opencode-provider call fail with HTTP 401 AuthError — an empty assistant
  # message, no file written, nothing actionable. Unset before launch so the
  # daemon always authenticates like the app (auth.json).
  unset OPENCODE_API_KEY 2>/dev/null || true
  local auth=()
  local cred_user="${OPENCODE_SERVER_USER:-${OPENCODE_SERVER_USERNAME:-}}"
  if [ -n "$cred_user" ] && [ -n "${OPENCODE_SERVER_PASSWORD:-}" ]; then
    auth=(-u "$cred_user:$OPENCODE_SERVER_PASSWORD")
  fi
  if ! curl -s -m 4 "${auth[@]}" http://127.0.0.1:4096/api/health > /dev/null 2>&1; then
    log "opencode serve down — starting..."
    ( cd "$ASHA_ROOT" && nohup "$OPENCODE_BIN" serve --hostname 127.0.0.1 --port 4096 \
      > "$OPENCODE_LOG" 2>&1 < /dev/null & )
    for i in $(seq 1 15); do
      curl -s -m 2 "${auth[@]}" http://127.0.0.1:4096/api/health > /dev/null 2>&1 \
        && { log "opencode serve up."; return 0; }
      sleep 1
    done
    log "WARNING: opencode serve did not respond, retrying next cycle."
  fi
}

ensure_opencode_web() {
  # Work-view UI: opencode web on :8201 (no auth — proxied via /opencode/ on :8000).
  if ! lsof -ti :8201 -sTCP:LISTEN > /dev/null 2>&1; then
    log "opencode web down — starting..."
    ( cd "$ASHA_ROOT" && BROWSER=none nohup "$OPENCODE_BIN" web --port 8201 \
      > /tmp/opencode-web.log 2>&1 < /dev/null & )
    for i in $(seq 1 10); do
      curl -s -m 2 http://127.0.0.1:8201/ > /dev/null 2>&1 \
        && { log "opencode web up."; return 0; }
      sleep 1
    done
    log "WARNING: opencode web did not respond, retrying next cycle."
  fi
}

ensure_bot() {
  # Start the bot ONLY if none is serving :7860. Never kill an existing healthy
  # bot — two supervisors racing used to pkill each other's fresh instance.
  P=$(lsof -ti :7860 -sTCP:LISTEN 2>/dev/null | head -1 || true)
  if [ -n "$P" ]; then
    return 0
  fi
  ( cd "$UI" && nohup "$VENV" -u server.py > "$LOG" 2>&1 < /dev/null & )
}

log "Asha supervisor started. Watching 24/7..."

ensure_watcher() {
  # The autonomous watcher (asha_watcher.sh) keeps the brain + agents alive and
  # self-heals. The supervisor restarts it if its heartbeat goes stale (>10 min)
  # or if it is not running at all.
  if bash "$UI/asha_watcher.sh" is-alive 2>/dev/null; then
    return 0
  fi
  log "watcher heartbeat stale or not running — restarting watcher..."
  pkill -9 -f asha_watcher.sh 2>/dev/null || true
  ( nohup bash "$UI/asha_watcher.sh" loop > /dev/null 2>&1 < /dev/null & )
}

while true; do
  ensure_static
  ensure_opencode
  ensure_opencode_web
  ensure_bot
  ensure_watcher
  # Tight poll: check-then-sleep every 2s so a dead bot is restarted
  # within ~2s (the old `sleep 20` before the check added up to 20s of
  # dead-air waiting).
  while true; do
    if ! pgrep -f "python -u server.py" > /dev/null 2>&1; then
      log "bot process died — restarting..."
      break
    fi
    ensure_opencode
    ensure_opencode_web
    ensure_watcher
    sleep 2
  done
done
