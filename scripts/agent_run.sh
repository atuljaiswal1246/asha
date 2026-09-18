#!/usr/bin/env bash
# Dispatch a scoped coding brief to an opencode CLI agent (a "muscle").
#
# SAFE BY CONSTRUCTION — lessons paid for in KB-06 and the auth.json incident:
#   * drives the installed `opencode` CLI, which authenticates via auth.json and
#     sends the validated client header shape. NEVER hand-roll HTTP to
#     opencode.ai/zen|go — that is what triggered the abuse-filter 429s.
#   * unsets OPENCODE_API_KEY so it can never shadow auth.json (401s).
#
# Usage:
#   scripts/agent_run.sh --dir <worktree> --model <provider/model> \
#       [--variant high] [--auto] --task "<brief>"
#   (pass a file with --task @path/to/brief.txt)
#
# Rules:
#   * one module/file per task; never point two agents at the same dir.
#   * prints the diff afterwards so the brain reviews it (rule 9).
#   * prefer free models (opencode/big-pickle, ...) for mechanical work;
#     use paid go models for hard, multi-tool tasks (KB-16).
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"

DIR=""; MODEL=""; VARIANT=""; TASK=""; AUTO=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2;;
    --model|-m) MODEL="$2"; shift 2;;
    --variant) VARIANT="$2"; shift 2;;
    --task) TASK="$2"; shift 2;;
    --auto) AUTO=1; shift;;
    *) echo "agent_run: unknown arg: $1" >&2; exit 2;;
  esac
done
[ -n "$DIR" ] || { echo "agent_run: --dir required" >&2; exit 2; }
[ -n "$MODEL" ] || { echo "agent_run: --model required" >&2; exit 2; }
[ -n "$TASK" ] || { echo "agent_run: --task required" >&2; exit 2; }
if [ "${TASK:0:1}" = "@" ]; then TASK="$(cat "${TASK:1}")"; fi

LOGDIR="${TMPDIR:-/tmp}/opencode/agent-logs"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/$(date +%Y%m%d-%H%M%S)-$(basename "$DIR").log"

ARGS=(run --dir "$DIR" -m "$MODEL" --format json)
[ -n "$VARIANT" ] && ARGS+=(--variant "$VARIANT")
[ "$AUTO" = 1 ] && ARGS+=(--auto)
ARGS+=("$TASK")

echo "[agent_run] model=$MODEL dir=$DIR variant=${VARIANT:-default} auto=$AUTO"
echo "[agent_run] log=$LOG"
env -u OPENCODE_API_KEY opencode "${ARGS[@]}" >"$LOG" 2>&1 \
  || echo "[agent_run] opencode exited non-zero (see log)"

echo "=== changed files ==="
git -C "$DIR" status --porcelain -- . 2>/dev/null || true
echo "=== diff stat ==="
git -C "$DIR" diff --stat -- . 2>/dev/null || true
echo "[agent_run] full log: $LOG"
