#!/usr/bin/env bash
# Asha test gate (D1.2): run every coding-path test suite, fail loudly.
#
# Pure/stdlib suites run under any python3; the server-tools and lsp suites
# need the project venv / pylsp (they skip cleanly if unavailable).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
UI="$ROOT/prototype/ui"

run() {
  local label="$1"; shift
  local log="/tmp/asha-test-${label// /-}.log"
  if "$PY" "$@" >"$log" 2>&1; then
    echo "[test] $label: $(tail -1 "$log")"
  else
    echo "[test] $label: FAILED"
    tail -10 "$log"
    exit 1
  fi
}

run "apply_patch"    "$UI/test_apply_patch.py"
run "orchestrator"   "$UI/test_orchestrator.py"
run "permissions"    "$UI/test_permissions.py"
run "sessions"       "$UI/test_sessions.py"
run "mcp client"     "$UI/test_mcp_client.py"
run "skills"         "$UI/test_skills.py"
run "backends"       "$UI/test_backends.py"
run "scheduler"      "$UI/test_scheduler.py"
run "channels"       "$UI/test_channels.py"
run "lsp client"     "$UI/test_lsp_client.py"
run "agent loop"     "$UI/test_agent_loop.py"
run "server tools"   "$UI/test_server_tools.py"

echo "[test] all green"
