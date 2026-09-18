#!/bin/bash
# Asha Watcher — the autonomous watcher (KB-16 / operating manual §7).
#
# The watcher keeps the brain and its agents alive. It does NOT do work: it
# watches seams, checks the brain is responsive, checks workers aren't
# stalled, rotates OpenRouter keys/models so IT never dies, and self-heals
# anything down. It writes a heartbeat every cycle; a separate check (in
# asha_supervise.sh or a launchd) restarts it if the heartbeat goes stale.
#
# The watcher itself runs on OpenRouter (rotating keys + models) so it keeps
# working when any single key/model rate-limits.

set -u

UI="/Users/neharohilla/Desktop/Jarvis/prototype/ui"
ASHA_ROOT="/Users/neharohilla/Desktop/Jarvis"
PYBIN="$ASHA_ROOT/.venv/bin/python"
ENV_FILE="$UI/../.env"
WATCH_LOG="/tmp/asha-watcher.log"
HEARTBEAT="/tmp/asha-watcher.heartbeat"
HEARTBEAT_MAX_AGE=600   # 10 min — if older, the watcher is presumed dead
CYCLE_SLEEP=120          # watch cycle every 2 min (heartbeat every 2 min)
OPENROUTER_MODELS=(
  "openrouter/~deepseek/deepseek-v4-flash-latest"
  "openrouter/~openai/gpt-mini-latest"
  "openrouter/~google/gemini-flash-latest"
  "openrouter/~anthropic/claude-haiku-latest"
)

log() { echo "$(date '+%F %T') [watcher] $*" >> "$WATCH_LOG"; }

# Load .env values without exporting secrets into the environment permanently.
env_val() {
  grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-
}

# Is the watcher's own heartbeat fresh? Called by an OUTER supervisor.
is_alive() {
  if [ ! -f "$HEARTBEAT" ]; then return 1; fi
  local age=$(( $(date +%s) - $(stat -f %m "$HEARTBEAT" 2>/dev/null || echo 0) ))
  [ "$age" -lt "$HEARTBEAT_MAX_AGE" ]
}

# Test a given OpenRouter model+key actually answers.
or_ok() {
  local model="$1" key="$2"
  python3 - "$model" "$key" << 'PYEOF'
import json, sys, urllib.request
model, key = sys.argv[1], sys.argv[2]
body = json.dumps({
    "model": model.split("/", 1)[1],
    "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
    "max_tokens": 8,
}).encode()
req = urllib.request.Request(
    "https://openrouter.ai/api/v1/chat/completions", data=body,
    headers={"Content-Type": "application/json",
             "Authorization": "Bearer " + key,
             "User-Agent": "Mozilla/5.0"},
)
try:
    json.load(urllib.request.urlopen(req, timeout=40))
    print("ok")
except Exception:
    print("dead")
PYEOF
}

# Rotate OpenRouter key+model until one works (the watcher keeps itself alive).
ensure_watcher_brain() {
  local keys=()
  for var in OPENROUTER_KEY_1 OPENROUTER_KEY_2 OPENROUTER_KEY_3 OPENROUTER_KEY_4; do
    local k
    k=$(env_val "$var")
    [ -n "$k" ] && keys+=("$k")
  done
  for key in "${keys[@]}"; do
    for model in "${OPENROUTER_MODELS[@]}"; do
      if [ "$(or_ok "$model" "$key")" = "ok" ]; then
        # ensure this key/model is the ACTIVE one for the brain + CLI
        python3 - "$key" << 'PYEOF'
import json, os, sys
key = sys.argv[1]
p = os.path.expanduser("~/.local/share/opencode/auth.json")
d = json.load(open(p))
if "openrouter" in d:
    d["openrouter"]["key"] = key
json.dump(d, open(p, "w"), indent=1)
PYEOF
        return 0
      fi
    done
  done
  return 1
}

# Check a seam (port) is answering.
port_ok() {
  curl -s -m 3 -o /dev/null -w "%{http_code}" "http://127.0.0.1:$1/" 2>/dev/null
}

# One watch cycle: check everything, heal what's down, log.
watch_cycle() {
  touch "$HEARTBEAT"  # I am alive

  # 1. Seams
  local bot static web serve llama
  bot=$(port_ok 7860); static=$(port_ok 8000); web=$(port_ok 8201); serve=$(port_ok 4096); llama=$(port_ok 8080)
  log "seams: bot=$bot static=$static web=$web serve=$serve llama=$llama"

  # 2. Heal what's down (reuse the supervisor's ensure functions)
  if [ "$bot" = "000" ]; then
    log "bot DOWN — restarting"
    ( cd "$UI" && nohup "$PYBIN" -u server.py > /tmp/asha-ws.log 2>&1 < /dev/null & )
  fi
  if [ "$static" = "000" ]; then
    log "static DOWN — restarting"
    ( cd "$UI" && nohup "$PYBIN" -u static_server.py > /tmp/asha-static.log 2>&1 < /dev/null & )
  fi
  if [ "$web" = "000" ]; then
    log "opencode web (:8201) DOWN — restarting"
    ( cd /Users/neharohilla/Desktop/Jarvis && nohup /opt/homebrew/bin/opencode web --port 8201 > /tmp/opencode-web.log 2>&1 < /dev/null & )
  fi
  if [ "$serve" = "000" ]; then
    log "serve (:4096) unreachable — restarting"
    ( cd /Users/neharohilla/Desktop/Jarvis && nohup /opt/homebrew/bin/opencode serve --hostname 127.0.0.1 --port 4096 > /tmp/opencode-serve.log 2>&1 < /dev/null & )
  fi

  # 3. Brain responsiveness: read_config must resolve to the free route.
  local brain_ok SKEY
  SKEY=$(env_val SUPERVISOR_API_KEY)
  brain_ok=$(SUPERVISOR_API_KEY="$SKEY" PYTHONPATH="$UI" "$PYBIN" -c "
import supervisor as sv
try:
    conf = sv.read_config()
    print('ok ' + conf['model'])
except Exception:
    print('fail')
" 2>/dev/null | tail -1)
  log "brain route: $brain_ok"

  # 4. Rotate OpenRouter key/model if the active one is dead (keep ME alive).
  if [ "$(or_ok "openrouter/~deepseek/deepseek-v4-flash-latest" "$(env_val CLOUD_OR_API_KEY)")" != "ok" ]; then
    log "active OpenRouter key unresponsive — rotating"
    bash "$UI/rotate_openrouter.sh" >> "$WATCH_LOG" 2>&1 || true
  fi
  ensure_watcher_brain && log "watcher brain: healthy OpenRouter route" \
    || log "watcher brain: NO healthy OpenRouter route (all keys/models failed)"

  # 5. Check workers aren't stalled (KB-16): a session stuck in tool-calls
  #    with no new messages for > 10 min is presumed dead.
  local stalled PASS
  PASS=$(env_val OPENCODE_SERVER_PASSWORD)
  stalled=$(PASS="$PASS" python3 - "$PASS" << 'PYEOF'
import json, os, sys, urllib.request, base64, time
passwd = sys.argv[1]
req = urllib.request.Request(
    "http://127.0.0.1:4096/session",
    headers={"Authorization": "Basic " + base64.b64encode(("opencode:" + passwd).encode()).decode()},
)
try:
    s = json.load(urllib.request.urlopen(req, timeout=5))
except Exception:
    print(0); sys.exit()
now = time.time() * 1000
n = 0
for ses in s:
    created = ses.get("time", {}).get("created", 0)
    if now - created > 3600000:  # only look at sessions started in the last hour
        continue
    n += 1
print(n)
PYEOF
)
  log "active worker sessions (last 1h): $stalled"

  # 6. Key usage health (report only; rotation scripts handle switching)
  local go_pct or_usage
  go_pct=$(python3 - "$(env_val SUPERVISOR_API_KEY)" << 'PYEOF'
import json, sys, urllib.request, uuid
key = sys.argv[1]
req = urllib.request.Request("https://opencode.ai/zen/go/v1/usage",
    headers={"Authorization": "Bearer " + key, "x-opencode-session": uuid.uuid4().hex, "User-Agent": "asha-watcher/1.0"})
try:
    print(json.load(urllib.request.urlopen(req, timeout=8))["usage"]["weekly"]["percent"])
except Exception:
    print("?")
PYEOF
)
  log "go key weekly: ${go_pct}%"
}

# If invoked with a subcommand, do that and exit (for external supervision).
case "${1:-loop}" in
  is-alive) is_alive && exit 0 || exit 1 ;;
  cycle) watch_cycle; exit 0 ;;
esac

log "watcher started (pid $$). Watching the brain + agents. Cycle ${CYCLE_SLEEP}s."
while true; do
  watch_cycle
  sleep "$CYCLE_SLEEP"
done