"""On-device Google OAuth for Jarvis connectors — no server required.

The Claude flow, but the tokens live on this Mac instead of someone's cloud:

    user clicks Connect  ->  browser opens  ->  sign in + approve
                         ->  loopback callback (127.0.0.1)
                         ->  refresh token saved in the macOS Keychain
                         ->  the connector is ready

Uses a Google "Desktop app" OAuth client. Google explicitly does not treat a
desktop client's secret as confidential, so it is fine to ship it — the user
never sees or enters anything.

Env:
  GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET   the app's OAuth client (shipped)
  JARVIS_OAUTH_PORT                         loopback port (default 56123)
  JARVIS_TOKEN_STORE                        keychain (default on macOS) | file
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oauth_common  # noqa: E402  (shared PKCE + loopback + token store)
try:
    import jarvis_paths
except Exception:  # noqa: BLE001 - usable standalone
    jarvis_paths = None

AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
KEYCHAIN_ACCOUNT = "jarvis"

CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
PORT = int(os.environ.get("JARVIS_OAUTH_PORT", "56123") or 56123)

# What each connector may do — ask for the least that works.
CONNECTORS: dict[str, dict] = {
    "google-calendar": {
        "label": "Google Calendar", "icon": "📅",
        "description": "See your schedule and create events.",
        "scopes": ["https://www.googleapis.com/auth/calendar.events"],
    },
    "gmail": {
        "label": "Gmail", "icon": "✉️",
        "description": "Search and read mail, draft replies.",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly",
                   "https://www.googleapis.com/auth/gmail.compose"],
    },
}


class OAuthError(Exception):
    """Raised when a connect/refresh cannot complete."""


# ── token storage (shared implementation, prefixed per product) ─────────────
_PREFIX = "jarvis-google"
_STORE = None


def FileStore(path=None):
    return oauth_common.FileStore(path, _PREFIX)


def KeychainStore():
    return oauth_common.KeychainStore(_PREFIX)


_STORE = None


def store():
    global _STORE
    if _STORE is None:
        _STORE = oauth_common.make_store(_PREFIX)
    return _STORE


def set_store(s) -> None:
    """Inject a store (tests)."""
    global _STORE
    _STORE = s


# ── OAuth plumbing ───────────────────────────────────────────────────────────
_pkce = oauth_common.pkce


def redirect_uri(port: int | None = None) -> str:
    return f"http://127.0.0.1:{port or PORT}/callback"


def authorize_url(connector: str, state: str, code_challenge: str,
                  port: int | None = None) -> str:
    meta = CONNECTORS.get(connector)
    if not meta:
        raise OAuthError(f"unknown connector {connector!r}")
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri(port),
        "response_type": "code",
        "scope": " ".join(meta["scopes"]),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",     # we need a refresh token
        "prompt": "consent",          # …and one every time, not just the first
    }
    return f"{AUTHORIZE}?{urllib.parse.urlencode(params)}"


def _token_request(data: dict) -> dict:
    r = httpx.post(TOKEN, data=data, timeout=20)
    if r.status_code >= 400:
        raise OAuthError(f"token endpoint: {r.text[:200]}")
    return r.json()


def exchange_code(code: str, code_verifier: str, port: int | None = None) -> dict:
    return _token_request({
        "code": code, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "redirect_uri": redirect_uri(port), "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    })


def _save(connector: str, tok: dict, client_id: str = "",
          client_secret: str = "") -> dict:
    rec = {
        "access_token": tok.get("access_token", ""),
        "refresh_token": tok.get("refresh_token", ""),
        "expires_at": int(time.time()) + int(tok.get("expires_in", 3600)),
        "scopes": tok.get("scope", ""),
    }
    prev = store().get(connector) or {}
    if not rec["refresh_token"]:
        rec["refresh_token"] = prev.get("refresh_token", "")  # Google only sends it once
    for key, value in (("client_id", client_id), ("client_secret", client_secret)):
        rec[key] = value or prev.get(key, "")
    store().set(connector, rec)
    return rec


def _client_for_refresh(rec: dict) -> tuple[str, str]:
    """Client credentials for a token request.

    Prefer what the sign-in was stored with, then the process config, then the
    environment read at call time (a connector subprocess may only have
    inherited GOOGLE_CLIENT_ID from the app server).
    """
    cid = (rec.get("client_id") or CLIENT_ID
           or os.environ.get("GOOGLE_CLIENT_ID", "")).strip()
    secret = (rec.get("client_secret") or CLIENT_SECRET
              or os.environ.get("GOOGLE_CLIENT_SECRET", "")).strip()
    return cid, secret


def event_log_path():
    """Where connector state changes are recorded (runtime data dir, no secrets)."""
    try:
        import jarvis_paths
        base = Path(jarvis_paths.data_dir())
    except Exception:  # noqa: BLE001
        base = Path(__file__).resolve().parents[1] / "data"
    return base / "mcp-events.log"


def log_event(event: str) -> None:
    """Append one timestamped line. Never raises; never write secrets."""
    try:
        path = event_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {event}\n")
    except Exception:  # noqa: BLE001
        pass


def connect(connector: str, *, open_browser: bool = True, timeout: float = 240,
            port: int | None = None) -> dict:
    """Run the whole flow. Blocks until the user approves (or times out)."""
    if connector not in CONNECTORS:
        raise OAuthError(f"unknown connector {connector!r}")
    if not CLIENT_ID or not CLIENT_SECRET:
        raise OAuthError(
            "Google OAuth client not configured. Set GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET (Desktop app type) — see notes/connectors.md.")
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)

    # Bind first so the redirect URI always matches the port we really hold
    # (port=0 -> an ephemeral port, so we never collide with anything).
    with oauth_common.Loopback(port or PORT) as lb:
        url = authorize_url(connector, state, challenge, lb.port)
        if open_browser:
            webbrowser.open(url)
        got = lb.wait(timeout)

    if not got:
        raise OAuthError("timed out waiting for approval")
    if got.get("error"):
        raise OAuthError(f"provider said: {got['error']}")
    if got.get("state") != state:
        raise OAuthError("state mismatch — ignoring this callback")
    rec = _save(connector, exchange_code(got["code"], verifier, lb.port),
                client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    log_event(f"connect {connector} ok")
    return rec


def access_token(connector: str) -> str:
    """A live access token, refreshing it when needed."""
    rec = store().get(connector)
    if not rec:
        raise OAuthError(f"{CONNECTORS.get(connector, {}).get('label', connector)} is not connected")
    if rec.get("access_token") and rec.get("expires_at", 0) > time.time() + 60:
        return rec["access_token"]
    if not rec.get("refresh_token"):
        raise OAuthError("no refresh token — reconnect this connector")
    client_id, client_secret = _client_for_refresh(rec)
    if not client_id:
        label = CONNECTORS.get(connector, {}).get("label", connector)
        raise OAuthError(
            f"{label} can't be refreshed: its saved sign-in has no Google client ID. "
            "Reconnect this connector, or set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET "
            "(Desktop app type).")
    data = {
        "client_id": client_id,
        "refresh_token": rec["refresh_token"],
        "grant_type": "refresh_token",
    }
    if client_secret:
        data["client_secret"] = client_secret
    tok = _token_request(data)
    return _save(connector, tok, client_id=client_id,
                 client_secret=client_secret)["access_token"]


def connected(connector: str) -> bool:
    return store().get(connector) is not None


def disconnect(connector: str) -> bool:
    removed = store().delete(connector)
    log_event(f"disconnect {connector} removed={removed}")
    return removed


def status() -> dict:
    return {cid: {"label": m["label"], "icon": m["icon"], "description": m["description"],
                  "connected": connected(cid)} for cid, m in CONNECTORS.items()}


def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Google OAuth for Jarvis connectors")
    ap.add_argument("action", choices=["connect", "status", "disconnect"])
    ap.add_argument("connector", nargs="?")
    args = ap.parse_args()
    if args.action == "connect":
        rec = connect(args.connector)
        print(f"connected {args.connector} (expires in "
              f"{int(rec['expires_at'] - time.time())}s)")
    elif args.action == "disconnect":
        print("removed" if disconnect(args.connector) else "was not connected")
    else:
        print(json.dumps(status(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
