"""Runtime custom providers (BYOK): user-defined OpenAI-compatible endpoints.

A packaged app must let a user add "any platform that offers an API key"
without editing ``.env``. We store them in ``<data>/providers.json``
(gitignored runtime data), one entry per endpoint::

    {"id": "my-endpoint", "name": "My Endpoint",
     "base_url": "https://api.example.com/v1", "api_key": "sk-...",
     "models": ["model-a", "model-b"]}

The registry exposes each entry as provider id ``custom:<id>`` so it flows
through the existing OpenAI-compatible path unchanged. Keys are never returned
by :meth:`list`/:meth:`get` (only a boolean ``configured``).
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "endpoint"


class CustomProviders:
    """Filesystem-backed store of user-defined OpenAI-compatible providers."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._items: list[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            items = data.get("custom") if isinstance(data, dict) else data
            self._items = [i for i in (items or []) if isinstance(i, dict)
                           and i.get("id") and i.get("base_url")]
        except Exception:  # noqa: BLE001 — missing/corrupt file => empty
            self._items = []

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"custom": self._items}, indent=2) + "\n",
                              encoding="utf-8")

    def list(self) -> list[dict]:
        return [self._public(i) for i in self._items]

    def get(self, cid: str) -> dict | None:
        """Full entry (WITH key) for internal registry use, or None."""
        for i in self._items:
            if i.get("id") == cid:
                return i
        return None

    def add(self, name: str, base_url: str, api_key: str = "",
            models: list[str] | None = None, cid: str = "") -> dict:
        name = (name or "").strip()
        base_url = (base_url or "").strip()
        if not name:
            raise ValueError("provider name is required")
        if not base_url:
            raise ValueError("base URL is required")
        cid = cid or _slug(name)
        if any(i.get("id") == cid for i in self._items):
            raise ValueError(f"provider '{cid}' already exists")
        entry = {"id": cid, "name": name, "base_url": base_url,
                 "api_key": (api_key or "").strip(),
                 "models": [m.strip() for m in (models or []) if m and m.strip()]}
        self._items.append(entry)
        self._save()
        return self._public(entry)

    def remove(self, cid: str) -> bool:
        before = len(self._items)
        self._items = [i for i in self._items if i.get("id") != cid]
        if len(self._items) != before:
            self._save()
            return True
        return False

    @staticmethod
    def _public(entry: dict) -> dict:
        return {
            "id": entry.get("id", ""),
            "provider_id": f"custom:{entry.get('id', '')}",
            "name": entry.get("name", ""),
            "base_url": entry.get("base_url", ""),
            "models": list(entry.get("models") or []),
            "configured": bool(entry.get("api_key")),
        }
