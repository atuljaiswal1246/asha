"""Ordered wildcard permission rules (A3).

Rules are evaluated top-to-bottom and the FIRST match wins, so a ``deny``
placed before an ``allow`` short-circuits. Patterns are shell-style wildcards
(fnmatch) matched case-insensitively against a *subject* string such as
``"bash: git status"`` or ``"write: prototype/ui/server.py"``.

Rules persist to JSON so an "always allow" choice survives restarts.
"""
from __future__ import annotations

import fnmatch
import json
import os
import threading
from pathlib import Path

ALLOW = "allow"
DENY = "deny"
ASK = "ask"
_ACTIONS = (ALLOW, DENY, ASK)


class PermissionStore:
    """Load/save/evaluate an ordered list of ``{pattern, action}`` rules."""

    def __init__(self, path: str | os.PathLike):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._rules: list[dict] = []
        self.load()

    # ── persistence ──────────────────────────────────────────────────────────
    def load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            rules = data.get("rules") if isinstance(data, dict) else None
        except Exception:
            rules = None
        with self._lock:
            self._rules = self._clean(rules or [])

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"rules": self._rules}, indent=2), encoding="utf-8"
            )
            tmp.replace(self._path)
        except Exception:
            pass

    @staticmethod
    def _clean(rules) -> list[dict]:
        out: list[dict] = []
        for r in rules or []:
            if not isinstance(r, dict):
                continue
            pattern = str(r.get("pattern", "")).strip()
            action = str(r.get("action", "")).strip().lower()
            if pattern and action in _ACTIONS:
                out.append({"pattern": pattern, "action": action})
        return out

    # ── accessors ────────────────────────────────────────────────────────────
    def rules(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._rules]

    def set_rules(self, rules) -> list[dict]:
        with self._lock:
            self._rules = self._clean(rules or [])
        self.save()
        return self.rules()

    def add_rule(self, pattern: str, action: str, *, index: int | None = None) -> list[dict]:
        pattern = (pattern or "").strip()
        action = (action or "").strip().lower()
        if not pattern or action not in _ACTIONS:
            return self.rules()
        with self._lock:
            self._rules = [r for r in self._rules if r.get("pattern") != pattern]
            rule = {"pattern": pattern, "action": action}
            if index is None:
                self._rules.append(rule)
            else:
                self._rules.insert(max(0, index), rule)
        self.save()
        return self.rules()

    def remove_rule(self, pattern: str) -> list[dict]:
        with self._lock:
            self._rules = [r for r in self._rules if r.get("pattern") != pattern]
        self.save()
        return self.rules()

    # ── evaluation ───────────────────────────────────────────────────────────
    def decide(self, subject: str, default: str = ASK) -> str:
        """First matching rule's action, else ``default``.

        Deny short-circuits by order: if a deny rule matches before any allow,
        the result is deny and no later allow is consulted.
        """
        s = (subject or "").lower()
        with self._lock:
            rules = list(self._rules)
        for r in rules:
            if fnmatch.fnmatch(s, r["pattern"].lower()):
                return r["action"]
        return default
