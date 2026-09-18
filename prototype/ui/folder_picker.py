"""Server-side folder picker for choosing a project directory.

Jarvis runs on the user's own machine, so the server (not the browser) is what
can see real filesystem paths. This module provides:

  * list_dir(path)            — sandboxed immediate-subdirectory listing
  * choose_native_folder()    — the macOS "choose folder" dialog via osascript

Nothing here scans the disk: list_dir returns only the immediate children of
the requested directory, and only within the allowed roots.
"""

from __future__ import annotations

import os
import subprocess
import sys

# Only these roots (and their descendants) may be listed. Home covers the
# common case; /Volumes lets the user reach mounted drives.
ALLOWED_ROOTS: list[str] = [
    os.path.expanduser("~"),
    "/Volumes",
]


class FolderPickerError(Exception):
    """Raised for a bad path / permission / OS failure. Message is user-safe."""


def _is_allowed(path: str) -> bool:
    """True when *path* is one of the allowed roots or inside one of them.

    Real paths are compared so symlinks cannot escape the sandbox.
    """
    rp = os.path.realpath(path)
    for root in ALLOWED_ROOTS:
        rr = os.path.realpath(root)
        if rp == rr or rp.startswith(rr + os.sep):
            return True
    return False


def _default_path() -> str:
    return os.path.expanduser("~")


def list_dir(path: str) -> dict:
    """Immediate subdirectories of *path*, sandboxed.

    Returns {path, parent, entries, git} where entries is a list of
    {name, path, git} sorted by name. ``parent`` is "" at an allowed root.
    """
    raw = (path or "").strip() or _default_path()
    p = os.path.abspath(os.path.expanduser(raw))
    if not _is_allowed(p):
        raise FolderPickerError("That folder is outside the folders Jarvis may browse.")
    if not os.path.isdir(p):
        raise FolderPickerError("That folder doesn't exist.")

    entries: list[dict] = []
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    if e.name.startswith("."):
                        continue
                    if not e.is_dir(follow_symlinks=True):
                        continue
                    entries.append({
                        "name": e.name,
                        "path": os.path.join(p, e.name),
                        "git": os.path.isdir(os.path.join(p, e.name, ".git")),
                    })
                except OSError:
                    continue
    except PermissionError:
        raise FolderPickerError("Jarvis doesn't have permission to read that folder.")
    except OSError as e:
        raise FolderPickerError(f"Couldn't read that folder: {e}")

    entries.sort(key=lambda x: x["name"].lower())

    parent = os.path.dirname(p.rstrip(os.sep))
    if parent == p or not _is_allowed(parent):
        parent = ""

    return {
        "path": p,
        "parent": parent,
        "entries": entries,
        "git": os.path.isdir(os.path.join(p, ".git")),
    }


def choose_native_folder() -> str:
    """Open the macOS folder dialog and return the chosen path ("" if cancelled)."""
    if sys.platform != "darwin":
        raise FolderPickerError("The system folder dialog is only available on macOS.")
    script = 'POSIX path of (choose folder with prompt "Select a project folder for Jarvis")'
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        return ""
    except FileNotFoundError:
        raise FolderPickerError("osascript not found.")
    if proc.returncode != 0:
        return ""  # user cancelled
    return (proc.stdout or "").strip()
