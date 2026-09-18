"""Git-backed snapshot / revert for agent tasks (P0.4).

`capture(root)` before a task; `restore(root, snap)` to undo it. Uses git
objects only — `git stash create` mints a commit for the current working tree
WITHOUT touching the index or the stash list; a clean tree falls back to HEAD.
Untracked files are recorded so files the task creates get removed on revert.

Pure subprocess; zero side effects on import.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)


def capture(root: str) -> dict | None:
    """Return {"sha", "untracked"} for *root*, or None if not a git repo."""
    if not root or not Path(root).is_dir():
        return None
    if _git(root, "rev-parse", "--is-inside-work-tree").returncode != 0:
        return None
    sha = ""
    r = _git(root, "stash", "create")
    if r.returncode == 0:
        sha = (r.stdout or "").strip()
    if not sha:  # clean tree → restore target is HEAD
        h = _git(root, "rev-parse", "HEAD")
        sha = (h.stdout or "").strip() if h.returncode == 0 else ""
    untracked = [ln for ln in
                 (_git(root, "ls-files", "--others", "--exclude-standard").stdout or "").splitlines()
                 if ln.strip()]
    return {"sha": sha, "untracked": untracked}


def restore(root: str, snap: dict) -> int:
    """Revert tracked files to the snapshot and delete files created since.
    Returns a count of paths changed. Never raises."""
    if not snap or not root or not Path(root).is_dir():
        return 0
    n = 0
    sha = snap.get("sha")
    if sha:
        r = _git(root, "checkout", sha, "--", ".")
        if r.returncode == 0:
            n += 1
    before = set(snap.get("untracked") or [])
    now = set(ln for ln in
              (_git(root, "ls-files", "--others", "--exclude-standard").stdout or "").splitlines()
              if ln.strip())
    for rel in sorted(now - before):
        p = Path(root) / rel
        try:
            if p.is_file():
                p.unlink()
                n += 1
        except OSError:
            pass
    return n
