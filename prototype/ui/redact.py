"""Secret redaction (secret_scope): keys must never reach the model or logs.

Shared by the agent loop (tool output) and the server log formatter. Matches
well-known key shapes plus the exact values of secret-looking environment
variables, so a leaked key is scrubbed wherever it appears.
"""
from __future__ import annotations

import os
import re

_SECRET_ENV_RE = re.compile(r"(API_KEY|_KEY|_TOKEN|_SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I)
_SECRET_VALUE_RES = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),          # openai/opencode
    re.compile(r"\bsk-or-v1-[a-f0-9]{32,}\b"),         # openrouter
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),         # google
    re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b"),  # slack
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),     # github
]
_CACHE: list[str] | None = None


def secret_values() -> list[str]:
    """Exact values of env vars whose names look secret (cached per process)."""
    global _CACHE
    if _CACHE is None:
        _CACHE = [v for k, v in os.environ.items()
                  if v and len(v) >= 12 and _SECRET_ENV_RE.search(k)]
    return _CACHE


def redact(text: str) -> str:
    """Scrub known secret patterns + exact env secret values."""
    if not text:
        return text
    for rx in _SECRET_VALUE_RES:
        text = rx.sub("[REDACTED]", text)
    for v in secret_values():
        if v in text:
            text = text.replace(v, "[REDACTED]")
    return text


def reset_cache() -> None:
    global _CACHE
    _CACHE = None
