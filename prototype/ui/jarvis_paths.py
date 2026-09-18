"""Resolve where Jarvis stores data and which project it works in.

Dev (running from the repo) keeps everything under ``prototype/data`` exactly
as before. Packaged apps set:
  * ``JARVIS_DATA_DIR``          -> a user-writable dir (never the .app bundle)
  * ``JARVIS_DEFAULT_PROJECT``   -> a sane default working folder (~/Jarvis)

so a shipped app never writes inside its own bundle and never codes in it.
"""
from __future__ import annotations

import os
from pathlib import Path

_REPO_DATA = Path(__file__).resolve().parents[1] / "data"   # prototype/data
_REPO_ROOT = Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    """Runtime data dir: JARVIS_DATA_DIR when set, else the repo's prototype/data."""
    env = os.environ.get("JARVIS_DATA_DIR", "").strip()
    path = Path(env).expanduser() if env else _REPO_DATA
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def default_project(fallback: str | Path | None = None) -> Path:
    """Default working folder when none is selected.

    JARVIS_DEFAULT_PROJECT when set (packaged app -> ~/Jarvis), else *fallback*
    (dev -> the repo), else the repo root."""
    env = os.environ.get("JARVIS_DEFAULT_PROJECT", "").strip()
    if env:
        path = Path(env).expanduser()
    elif fallback:
        path = Path(fallback).expanduser()
    else:
        path = _REPO_ROOT
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def in_bundle(path: str | Path) -> bool:
    """True when *path* points inside an app bundle (macOS .app), which must
    never be used as a working project."""
    return ".app/Contents" in str(path)
