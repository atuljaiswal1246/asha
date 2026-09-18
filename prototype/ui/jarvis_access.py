"""First-run access provisioning (payment-free demo path).

If the machine has no provider configured and a gateway URL is set
(``JARVIS_GATEWAY_URL``), sign up for a demo token, store it under
``prototype/data/gateway-token``, and let the ``jarvis`` provider (which reads
that token) serve the plan. Used by BOTH entry points — the full app
(``server.py``, voice + coding) and the headless core (``jarvis_core.py``).

Order of preference:
  1. an explicit JARVIS_TOKEN (env), or an existing gateway-token file
  2. a BYOK key already configured (user brought their own key)
  3. else: POST /api/signup to the gateway for a capped demo token
Never raises; a failure just leaves the app unconfigured (the UI can retry).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from providers import registry
import jarvis_paths

_DATA = jarvis_paths.data_dir()
_TOKEN_FILE = _DATA / "gateway-token"
_LOCK = threading.Lock()


def gateway_url() -> str:
    return (os.environ.get("JARVIS_GATEWAY_URL") or "").strip().rstrip("/")


def token_present() -> bool:
    return bool((os.environ.get("JARVIS_TOKEN") or "").strip()) or _TOKEN_FILE.is_file()


def _byok_configured() -> bool:
    for p in registry.list_providers():
        if p.get("provider_id") in ("local", "jarvis"):
            continue
        if p.get("configured"):
            return True
    return False


def ensure_access(timeout: float = 20.0) -> dict:
    """Make sure the app has something to talk to. Returns a small status dict."""
    if token_present():
        return {"status": "have-token"}
    url = gateway_url()
    if not url:
        return {"status": "no-gateway"}
    if _byok_configured():
        return {"status": "byok"}
    # Anonymous demo sign-up is opt-in only (default OFF). The demo path is:
    # the host ships their own key (BYOK) and revokes it later — no free tokens.
    if os.environ.get("JARVIS_DEMO_SIGNUP", "0") != "1":
        return {"status": "disabled"}
    with _LOCK:
        if token_present():  # another thread won the race
            return {"status": "have-token"}
        try:
            import httpx
            r = httpx.post(f"{url}/api/signup", json={}, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            token = (data.get("token") or "").strip()
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "error": repr(e)[:200]}
        if not token:
            return {"status": "error", "error": "gateway returned no token"}
        try:
            _DATA.mkdir(parents=True, exist_ok=True)
            _TOKEN_FILE.write_text(token, encoding="utf-8")
        except OSError:
            pass
        os.environ["JARVIS_TOKEN"] = token
        registry.reset_cache()
        return {"status": "provisioned", "plan": data.get("plan", "free")}
