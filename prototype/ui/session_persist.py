"""Standalone persistence for worker sessions (additive, opt-in).

Today ``worker_engine.py`` keeps sessions in memory only, so they are lost on
restart (gap #3 in the gaps report). This module is a *separate* store that
imports nothing from ``worker_engine.py`` or ``server.py``:

  * ``SessionStore(dir=None)`` — save/load/list/delete/prune sessions.
  * one JSON file per session, written atomically (temp file + ``os.replace``),
    mode ``0o600`` where the OS allows it.
  * a corrupt or half-written file is reported and skipped, never raised.

IMPORTANT: nothing calls this yet. Wiring it into ``worker_engine`` is a
separate, deliberate step — the engine's in-memory dict stays the source of
truth until then.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("asha.session_persist")

_SAFE_ID = re.compile(r"[A-Za-z0-9._-]+")
_SECONDS_PER_DAY = 86400.0


def _data_dir() -> Path:
    """Data-dir resolution matching ``screen_tool._data_dir()``."""
    try:
        import jarvis_paths

        return Path(jarvis_paths.data_dir())
    except Exception:  # noqa: BLE001 - standalone use
        return Path(__file__).resolve().parent.parent / "data"


def _safe_id(session_id: str) -> str:
    if not session_id or not _SAFE_ID.fullmatch(session_id):
        raise ValueError(f"unsafe session id: {session_id!r}")
    if session_id in (".", ".."):
        raise ValueError(f"unsafe session id: {session_id!r}")
    return session_id


class SessionStore:
    """One JSON file per session under ``dir`` (default: data/session_store)."""

    def __init__(self, dir: Optional[str | os.PathLike[str]] = None):
        self._dir = (
            Path(dir) if dir is not None else _data_dir() / "session_store"
        )
        self._lock = threading.RLock()
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    @property
    def dir(self) -> Path:
        return self._dir

    def _path(self, session_id: str) -> Path:
        return self._dir / f"{_safe_id(session_id)}.json"

    # -- writes -------------------------------------------------------------

    def save(self, session_id: str, data: dict) -> Path:
        """Atomically write ``data`` for ``session_id``. Returns the file path."""
        if not isinstance(data, dict):
            raise TypeError("session data must be a dict")
        path = self._path(session_id)
        payload = json.dumps(data, indent=2, sort_keys=True)
        with self._lock:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self._dir),
                prefix=f".{path.stem}.",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.chmod(tmp_name, 0o600)
                except OSError:
                    pass
                os.replace(tmp_name, path)
            finally:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
        return path

    def delete(self, session_id: str) -> bool:
        """Remove a session file. Returns False when it did not exist."""
        path = self._path(session_id)
        with self._lock:
            try:
                path.unlink()
                return True
            except FileNotFoundError:
                return False

    def prune(self, max_age_days: float, now: Optional[float] = None) -> list[str]:
        """Delete sessions older than ``max_age_days`` (by file mtime)."""
        reference = time.time() if now is None else float(now)
        removed: list[str] = []
        with self._lock:
            for session_id in self.list_ids():
                path = self._path(session_id)
                try:
                    age = (reference - path.stat().st_mtime) / _SECONDS_PER_DAY
                except OSError:
                    continue
                if age > max_age_days:
                    try:
                        path.unlink()
                        removed.append(session_id)
                    except OSError:
                        continue
        return removed

    def prune_oldest(self, max_sessions: int, now: Optional[float] = None) -> list[str]:
        """Keep the newest ``max_sessions`` sessions by file mtime, delete the rest.

        Oldest are deleted first. Returns the removed ids, sorted. ``max_sessions``
        below zero is treated as zero. Never raises.
        """
        try:
            limit = int(max_sessions)
        except (TypeError, ValueError):
            limit = 0
        if limit < 0:
            limit = 0
        removed: list[str] = []
        with self._lock:
            entries: list[tuple[float, str]] = []
            for session_id in self.list_ids():
                path = self._path(session_id)
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                entries.append((mtime, session_id))
            # Newest first; anything past the cap is the oldest and goes first.
            entries.sort(key=lambda item: item[0], reverse=True)
            for _mtime, session_id in entries[limit:]:
                path = self._path(session_id)
                try:
                    path.unlink()
                    removed.append(session_id)
                except OSError:
                    continue
        return sorted(removed)

    # -- reads --------------------------------------------------------------

    def load(self, session_id: str) -> Optional[dict]:
        """Load a session, or ``None`` when missing/corrupt (never raises)."""
        path = self._path(session_id)
        with self._lock:
            try:
                raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return None
            except OSError as exc:
                logger.warning("[SESSION] cannot read %s: %s", path.name, exc)
                return None
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning("[SESSION] corrupt %s (%s); ignoring", path.name, exc)
                return None
            if not isinstance(data, dict):
                logger.warning("[SESSION] %s is not a JSON object; ignoring", path.name)
                return None
            return data

    def list_ids(self) -> list[str]:
        """Sorted session ids (``*.json`` stems, temp files excluded)."""
        try:
            names = os.listdir(self._dir)
        except OSError:
            return []
        ids = [
            name[:-5]
            for name in names
            if name.endswith(".json") and not name.startswith(".")
        ]
        return sorted(ids)
