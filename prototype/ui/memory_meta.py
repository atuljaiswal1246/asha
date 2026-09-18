"""Sidecar metadata for memory entries: first/last seen times and decay.

``memory.py`` stores memories as plain text entries separated by
``ENTRY_DELIMITER`` (``"\\n§\\n"``) inside ``MEMORY.md`` / ``USER.md``. Those
files are append/replace/remove only, so a memory has no age and no way to be
forgotten except manual deletion. This module keeps the timing information in
a *separate* JSON sidecar (``memory_meta.json`` under the Jarvis data dir) so
the on-disk memory format is never touched.

What lives here:
  * ``entry_key`` — a stable short hash of an entry's text.
  * ``stamp`` / ``touch`` — record first-seen and last-seen times.
  * ``first_seen`` / ``forget`` — read age and drop a single key.
  * ``age_days`` / ``last_seen_days`` — how old an entry is.
  * ``decay_score`` — a pure-math freshness score in (0, 1].
  * ``snapshot`` / ``prune`` / ``decay_report`` — inspect, forget, count.

``memory.py`` uses this store as an optional, fail-open sidecar: it stamps
entries on add/replace and orders recall output by freshness. Decay NEVER
deletes a memory — deletion stays a user action. ``decay_report`` is a plain
report and is deliberately not called from the live memory path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("asha.memory_meta")

# ``memory.py`` defines ENTRY_DELIMITER = "\n§\n". We copy it (rather than
# import memory.py) because importing memory.py pulls in openai/pipecat, which
# this standalone sidecar must not require. Keep the two in sync.
ENTRY_DELIMITER = "\n§\n"

_SECONDS_PER_DAY = 86400.0
_KEY_HEX_LEN = 16


def _data_dir() -> Path:
    """Data-dir resolution matching ``screen_tool._data_dir()``."""
    try:
        import jarvis_paths

        return Path(jarvis_paths.data_dir())
    except Exception:  # noqa: BLE001 - standalone use
        return Path(__file__).resolve().parent.parent / "data"


def entry_key(text: str) -> str:
    """Stable short hash of an entry's text (whitespace-normalised)."""
    normalised = " ".join((text or "").split())
    digest = hashlib.sha256(normalised.encode("utf-8")).hexdigest()
    return digest[:_KEY_HEX_LEN]


def _coerce_time(when: Any) -> float:
    """Accept epoch seconds (int/float) or ``None`` (meaning now)."""
    if when is None:
        return time.time()
    if isinstance(when, (int, float)):
        return float(when)
    if hasattr(when, "timestamp"):
        return float(when.timestamp())
    raise TypeError(f"unsupported timestamp type: {type(when).__name__}")


class MemoryMetaStore:
    """JSON-file sidecar store: ``{key: {"first_seen": ts, "last_seen": ts}}``.

    Thread-safe; every read tolerates a corrupt or missing file by starting
    empty and logging (no exception escapes).
    """

    def __init__(self, path: str | os.PathLike[str]):
        self._path = Path(path)
        self._lock = threading.RLock()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    @property
    def path(self) -> Path:
        return self._path

    # -- persistence --------------------------------------------------------

    def _read(self) -> dict[str, dict[str, float]]:
        if not self._path.exists():
            return {}
        try:
            raw = self._path.read_text(encoding="utf-8")
            if not raw.strip():
                return {}
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("top level is not a JSON object")
            return data
        except Exception as exc:  # noqa: BLE001 - corrupt file must not escape
            logger.warning(
                "[MEM-META] %s unreadable (%s); starting empty",
                self._path.name,
                exc,
            )
            return {}

    def _write(self, data: dict) -> None:
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=f".{self._path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(tmp_name, 0o600)
            except OSError:
                pass
            os.replace(tmp_name, self._path)
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    @staticmethod
    def _record(data: dict, key: str) -> Optional[dict]:
        rec = data.get(key)
        return rec if isinstance(rec, dict) else None

    # -- writes -------------------------------------------------------------

    def stamp(self, key: str, when: Any = None, first_seen: Any = None) -> None:
        """Record first-seen (once) and last-seen (always) for ``key``.

        ``first_seen`` is an optional carried-over age: when it is a number and
        the record has no usable first-seen yet, it is used instead of ``when``
        (this lets a ``replace`` keep the replaced fact's age).
        """
        ts = _coerce_time(when)
        with self._lock:
            data = self._read()
            rec = self._record(data, key)
            if rec is None:
                rec = {}
            first = rec.get("first_seen")
            if not isinstance(first, (int, float)):
                if isinstance(first_seen, (int, float)):
                    rec["first_seen"] = float(first_seen)
                else:
                    rec["first_seen"] = ts
            rec["last_seen"] = ts
            data[key] = rec
            self._write(data)

    def touch(self, key: str) -> None:
        """Update only last-seen (first-seen is set if it was missing)."""
        self.stamp(key)

    def forget(self, key: str) -> bool:
        """Remove ``key`` from the sidecar; True if it was present."""
        with self._lock:
            data = self._read()
            if key not in data:
                return False
            del data[key]
            self._write(data)
            return True

    # -- reads --------------------------------------------------------------

    def age_days(self, key: str, now: Any = None) -> Optional[float]:
        """Days since first-seen, or ``None`` when the key is unknown."""
        with self._lock:
            rec = self._record(self._read(), key)
        if rec is None or not isinstance(rec.get("first_seen"), (int, float)):
            return None
        return (_coerce_time(now) - float(rec["first_seen"])) / _SECONDS_PER_DAY

    def last_seen_days(self, key: str, now: Any = None) -> Optional[float]:
        """Days since last-seen, or ``None`` when the key is unknown."""
        with self._lock:
            rec = self._record(self._read(), key)
        if rec is None or not isinstance(rec.get("last_seen"), (int, float)):
            return None
        return (_coerce_time(now) - float(rec["last_seen"])) / _SECONDS_PER_DAY

    def first_seen(self, key: str, now: Any = None) -> Optional[float]:
        """Stored first-seen epoch seconds, or ``None`` when unknown."""
        with self._lock:
            rec = self._record(self._read(), key)
        if rec is None or not isinstance(rec.get("first_seen"), (int, float)):
            return None
        return float(rec["first_seen"])

    def decay_score(self, key: str, half_life_days: float = 90.0) -> float:
        """Freshness in (0, 1]: 1.0 now, 0.5 at one half-life, → 0 over time.

        Unknown keys are treated as fresh (1.0) — absence of timing data must
        never penalise an existing memory. Pure maths over the stored times.
        """
        if half_life_days <= 0:
            return 0.0
        age = self.age_days(key)
        if age is None:
            return 1.0
        if age < 0:
            age = 0.0
        return min(1.0, 0.5 ** (age / half_life_days))

    def snapshot(self) -> dict:
        """A copy of the raw sidecar, for inspection."""
        with self._lock:
            data = self._read()
        return {k: dict(v) for k, v in data.items() if isinstance(v, dict)}

    def decay_report(self, days: float = 90.0, now: Any = None) -> dict:
        """Count keys whose last-seen is missing or older than ``days``.

        A plain report: it never edits the sidecar. Keys with no usable
        last-seen count as stale (mirrors ``prune``'s maximally-old rule).
        """
        reference = _coerce_time(now)
        with self._lock:
            data = self._read()
        stale = 0
        for rec in data.values():
            last = rec.get("last_seen") if isinstance(rec, dict) else None
            if not isinstance(last, (int, float)):
                stale += 1
                continue
            age = (reference - float(last)) / _SECONDS_PER_DAY
            if age > days:
                stale += 1
        return {"total": len(data), "stale": stale, "days": float(days)}

    def prune(self, older_than_days: float, now: Any = None) -> list[str]:
        """Forget keys whose last-seen is older than ``older_than_days``.

        Keys with no usable last-seen are treated as maximally old. Returns the
        list of removed keys.
        """
        reference = _coerce_time(now)
        removed: list[str] = []
        with self._lock:
            data = self._read()
            for key in list(data.keys()):
                rec = self._record(data, key) or {}
                last = rec.get("last_seen")
                age = (
                    (reference - float(last)) / _SECONDS_PER_DAY
                    if isinstance(last, (int, float))
                    else float("inf")
                )
                if age > older_than_days:
                    removed.append(key)
                    del data[key]
            if removed:
                self._write(data)
        return removed


# -- module-level default store --------------------------------------------

_STORES: dict[str, MemoryMetaStore] = {}
_STORES_LOCK = threading.Lock()


def default_path() -> Path:
    return _data_dir() / "memory_meta.json"


def _store(path: Optional[str | os.PathLike[str]] = None) -> MemoryMetaStore:
    resolved = Path(path) if path is not None else default_path()
    cache_key = str(resolved)
    with _STORES_LOCK:
        store = _STORES.get(cache_key)
        if store is None:
            store = MemoryMetaStore(resolved)
            _STORES[cache_key] = store
        return store


def stamp(key: str, when: Any = None, first_seen: Any = None) -> None:
    _store().stamp(key, when, first_seen)


def touch(key: str) -> None:
    _store().touch(key)


def forget(key: str) -> bool:
    return _store().forget(key)


def age_days(key: str, now: Any = None) -> Optional[float]:
    return _store().age_days(key, now)


def last_seen_days(key: str, now: Any = None) -> Optional[float]:
    return _store().last_seen_days(key, now)


def first_seen(key: str, now: Any = None) -> Optional[float]:
    return _store().first_seen(key, now)


def decay_score(key: str, half_life_days: float = 90.0) -> float:
    return _store().decay_score(key, half_life_days)


def snapshot() -> dict:
    return _store().snapshot()


def decay_report(days: float = 90.0, now: Any = None) -> dict:
    return _store().decay_report(days, now)


def prune(older_than_days: float, now: Any = None) -> list[str]:
    return _store().prune(older_than_days, now)
