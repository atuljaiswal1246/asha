#!/usr/bin/env bash
# Isolated git worktree per agent task (so parallel agents never clobber each other).
#
# Usage:
#   scripts/agent_worktree.sh add <name>     # create worktree on branch agent/<name>, prints its path
#   scripts/agent_worktree.sh remove <name>  # remove worktree + branch
#   scripts/agent_worktree.sh list
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE="${TMPDIR:-/tmp}/opencode/worktrees"
cmd="${1:-}"; name="${2:-}"

case "$cmd" in
  add)
    [ -n "$name" ] || { echo "usage: agent_worktree.sh add <name>" >&2; exit 2; }
    mkdir -p "$BASE"
    WT="$BASE/$name"
    if [ -d "$WT" ]; then echo "$WT"; exit 0; fi
    git -C "$ROOT" worktree add -b "agent/$name" "$WT" HEAD >/dev/null
    echo "$WT";;
  remove)
    [ -n "$name" ] || { echo "usage: agent_worktree.sh remove <name>" >&2; exit 2; }
    git -C "$ROOT" worktree remove --force "$BASE/$name" 2>/dev/null || true
    git -C "$ROOT" branch -D "agent/$name" 2>/dev/null || true
    echo "removed $name";;
  list)
    git -C "$ROOT" worktree list;;
  *)
    echo "usage: agent_worktree.sh add|remove|list <name>" >&2; exit 2;;
esac
