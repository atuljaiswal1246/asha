"""OAuth for remote MCP servers — the "click Connect, sign in" path.

Remote MCP servers (Slack's mcp.slack.com, Notion, Linear, Sentry, Google's
official endpoints) authenticate with OAuth, not a pasted token. This module
does the spec dance for them:

  1. ask the server who it is            -> 401 + WWW-Authenticate
  2. read its protected-resource metadata (RFC 9728)
  3. read the authorization server metadata (RFC 8414)
  4. register ourselves if allowed        (RFC 7591, dynamic client registration)
  5. authorization code + PKCE + resource (RFC 8707) over the loopback callback
  6. store the refresh token (Keychain) and refresh on demand

The user only ever signs in and approves.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
import urllib.parse
import webbrowser

import httpx

import oauth_common

PREFIX = "jarvis-mcp"
_STORE = None
TIMEOUT = 20.0


class OAuthError(Exception):
    """Raised when a remote server cannot be authorized."""


def store():
    global _STORE
    if _STORE is None:
        _STORE = oauth_common.make_store(PREFIX)
    return _STORE


def set_store(s) -> None:
    """Inject a store (tests)."""
    global _STORE
    _STORE = s


def _client_name() -> str:
    return os.environ.get("JARVIS_MCP_CLIENT_NAME", "Jarvis")


# ── discovery ────────────────────────────────────────────────────────────────
def _challenge_metadata_url(url: str) -> str:
    """Ask the server for a 401 and read its resource_metadata pointer."""
    try:
        r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {}}, timeout=TIMEOUT,
                       headers={"content-type": "application/json",
                                "accept": "application/json, text/event-stream"})
    except Exception:  # noqa: BLE001 - fall back to well-known
        return ""
    if r.status_code != 401:
        return ""
    header = r.headers.get("www-authenticate", "")
    m = re.search(r'resource_metadata="([^"]+)"', header)
    return m.group(1) if m else ""


def _well_known(origin: str, suffix: str) -> dict:
    for path in (f"/.well-known/{suffix}",
                 f"/.well-known/oauth-authorization-server{suffix}",
                 f"/.well-known/openid-configuration{suffix}"):
        try:
            r = httpx.get(origin.rstrip("/") + path, timeout=TIMEOUT)
            if r.status_code == 200 and isinstance(r.json(), dict):
                return r.json()
        except Exception:  # noqa: BLE001
            continue
    return {}


def discover(url: str) -> dict:
    """Return {resource, prm, asm} for a remote MCP server URL."""
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(url))
    prm: dict = {}
    meta_url = _challenge_metadata_url(url)
    if meta_url:
        try:
            prm = httpx.get(meta_url, timeout=TIMEOUT).json()
        except Exception:  # noqa: BLE001
            prm = {}
    if not prm:
        prm = _well_known(origin, "oauth-protected-resource")
    if not prm:
        raise OAuthError("this server does not advertise OAuth metadata")

    servers = prm.get("authorization_servers") or []
    as_url = servers[0] if servers else origin
    asm = _well_known(as_url, "oauth-authorization-server")
    if not asm.get("authorization_endpoint") or not asm.get("token_endpoint"):
        raise OAuthError("could not read the authorization server metadata")
    return {"resource": prm.get("resource") or url, "prm": prm, "asm": asm}


def _hint(name: str, url: str) -> str:
    """The real blocker, when a provider gates its server — not a guess.

    From Figma's docs: "Only clients listed in the Figma MCP Catalog can
    connect to the Figma MCP Server." Its remote server is allowlisted, so no
    self-registered OAuth app opens it.
    """
    host = (urllib.parse.urlsplit(url).hostname or "").lower() if url else ""
    if host.endswith("figma.com"):
        return ("Figma only allows clients listed in its MCP Catalog (VS Code, "
                "Cursor, Claude Code, Codex) to use the remote MCP server, so "
                "Jarvis needs Figma's approval (join the waitlist). Workaround: "
                "enable the Figma desktop app's local server (Dev Mode → MCP "
                "server → Enable) and add http://127.0.0.1:3845/mcp")
    return ""


def register(asm: dict, redirect_uri: str, scopes: list[str],
             name: str = "", url: str = "") -> dict:
    """Our preconfigured client when we have one, else dynamic registration."""
    try:
        return preconfigured_client(name, url)
    except OAuthError:
        pass
    endpoint = asm.get("registration_endpoint")
    if endpoint:
        body = {
            "client_name": _client_name(),
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "application_type": "native",
        }
        if scopes:
            body["scope"] = " ".join(scopes)
        try:
            r = httpx.post(endpoint, json=body, timeout=TIMEOUT)
            if r.status_code < 400:
                out = r.json()
                if out.get("client_id"):
                    return {"client_id": out["client_id"],
                            "client_secret": out.get("client_secret", "")}
        except Exception:  # noqa: BLE001 - fall through to the preconfigured client
            pass
    try:
        return preconfigured_client(name, url)
    except OAuthError as exc:
        raise OAuthError(_hint(name, url) or str(exc)) from None


def _env_slug(name: str) -> str:
    import re
    return re.sub(r"[^A-Z0-9]+", "_", (name or "").upper()).strip("_")


_GENERIC_HOSTS = {"mcp", "www", "api", "server", "com", "io", "ai", "app",
                  "dev", "cloud", "net", "org", "mcp-server"}


def _slugs(name: str, url: str = "") -> list[str]:
    """Env-var suffixes to try for a provider, most specific first.

    ``Figma-MCP-Server`` and ``mcp.figma.com`` both have to find
    ``JARVIS_MCP_CLIENT_ID_FIGMA``.
    """
    import re
    out: list[str] = []

    def add(value: str) -> None:
        value = _env_slug(value)
        if value and value not in out:
            out.append(value)

    add(name)
    for token in re.split(r"[^A-Za-z0-9]+", name or ""):
        add(token)
    host = urllib.parse.urlsplit(url).hostname or "" if url else ""
    for label in host.split("."):
        if label.lower() not in _GENERIC_HOSTS:
            add(label)
    return out


def preconfigured_client(name: str = "", url: str = "") -> dict:
    """A client we registered with the provider ourselves (per server, or global).

    Some providers (Figma) reject dynamic registration, so the operator
    registers one app and we use it for every user.
    """
    slugs = _slugs(name, url)
    for slug in [*slugs, ""]:
        cid = os.environ.get(f"JARVIS_MCP_CLIENT_ID_{slug}" if slug
                             else "JARVIS_MCP_CLIENT_ID", "")
        if not cid:
            continue
        if not slug:
            secret = os.environ.get("JARVIS_MCP_CLIENT_SECRET", "")
            if secret:
                return {"client_id": cid, "client_secret": secret}
            continue
        return {"client_id": cid,
                "client_secret": os.environ.get(
                    f"JARVIS_MCP_CLIENT_SECRET_{slug}", "")}
    hint = f"_{slugs[0]}" if slugs else ""
    raise OAuthError(
        "this provider does not allow automatic client registration — register "
        f"an OAuth app with it once, then set JARVIS_MCP_CLIENT_ID{hint} / "
        f"JARVIS_MCP_CLIENT_SECRET{hint} in .env (see notes/connectors.md)")


# ── the flow ─────────────────────────────────────────────────────────────────
def _basic(client_id: str, client_secret: str) -> str:
    import base64
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _token_request(asm: dict, data: dict, basic: str = "") -> dict:
    """Figma (and others) require client credentials via HTTP Basic, not the body."""
    headers = {"accept": "application/json"}
    if basic:
        headers["Authorization"] = basic
    r = httpx.post(asm["token_endpoint"], data=data, timeout=TIMEOUT, headers=headers)
    if r.status_code >= 400:
        raise OAuthError(f"token endpoint: {r.text[:200]}")
    return r.json()


def _save(name: str, tok: dict, **extra) -> dict:
    rec = dict(extra)
    rec.update({
        "access_token": tok.get("access_token", ""),
        "refresh_token": tok.get("refresh_token", ""),
        "expires_at": int(time.time()) + int(tok.get("expires_in", 3600)),
        "scopes": tok.get("scope", ""),
    })
    prev = store().get(name) or {}
    if not rec["refresh_token"]:
        rec["refresh_token"] = prev.get("refresh_token", "")
    for key in ("client_id", "client_secret"):
        if not rec.get(key):
            rec[key] = prev.get(key, "")
    store().set(name, rec)
    return rec


def default_port() -> int:
    """A fixed port, because providers that pre-register apps pin the redirect."""
    return int(os.environ.get("JARVIS_OAUTH_PORT", "56123") or 56123)


def connect(name: str, url: str, *, open_browser: bool = True,
            timeout: float = 240, port: int | None = None) -> dict:
    """Authorize a remote MCP server. Blocks until the user approves."""
    gate = _hint(name, url)
    if gate:
        # A provider whose docs gate its server (Figma's catalog allowlist):
        # don't open a browser on a door we know is locked.
        raise OAuthError(gate)
    disc = discover(url)
    asm, resource = disc["asm"], disc["resource"]
    scopes = list(disc["prm"].get("scopes_supported") or [])

    verifier, challenge = oauth_common.pkce()
    state = secrets.token_urlsafe(24)
    with oauth_common.Loopback(default_port() if port is None else port,
                               host=os.environ.get("JARVIS_OAUTH_HOST", "localhost")) as lb:
        client = register(asm, lb.redirect_uri, scopes, name)
        params = {
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": lb.redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": resource,
        }
        if scopes:
            params["scope"] = " ".join(scopes)
        url_out = asm["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)
        if open_browser:
            webbrowser.open(url_out)
        got = lb.wait(timeout)

        if not got:
            raise OAuthError("timed out waiting for approval")
        if got.get("error"):
            raise OAuthError(f"provider said: {got['error']}")
        if got.get("state") != state:
            raise OAuthError("state mismatch — ignoring this callback")
        data = {
            "grant_type": "authorization_code",
            "code": got["code"],
            "redirect_uri": lb.redirect_uri,
            "client_id": client["client_id"],
            "code_verifier": verifier,
            "resource": resource,
        }
        basic = ""
        if client.get("client_secret"):
            basic = _basic(client["client_id"], client["client_secret"])
        tok = _token_request(asm, data, basic)

    return _save(name, tok, url=url, resource=resource,
                 token_endpoint=asm["token_endpoint"],
                 client_id=client["client_id"],
                 client_secret=client.get("client_secret", ""))


def access_token(name: str, url: str = "") -> str:
    """A live access token for this server, refreshing when needed."""
    rec = store().get(name)
    if not rec:
        raise OAuthError(f"{name} is not signed in")
    if rec.get("access_token") and rec.get("expires_at", 0) > time.time() + 60:
        return rec["access_token"]
    if not rec.get("refresh_token"):
        raise OAuthError("no refresh token — sign in again")
    if not rec.get("token_endpoint"):
        raise OAuthError("missing token endpoint — sign in again")
    data = {
        "grant_type": "refresh_token",
        "refresh_token": rec["refresh_token"],
        "client_id": rec.get("client_id", ""),
        "resource": rec.get("resource") or url,
    }
    basic = ""
    if rec.get("client_secret"):
        basic = _basic(rec.get("client_id", ""), rec["client_secret"])
    tok = _token_request({"token_endpoint": rec["token_endpoint"]}, data, basic)
    return _save(name, tok, url=rec.get("url", url),
                 resource=rec.get("resource", ""),
                 token_endpoint=rec["token_endpoint"])["access_token"]


def connected(name: str) -> bool:
    return store().get(name) is not None


def disconnect(name: str) -> bool:
    return store().delete(name)


def auth_headers(name: str, url: str = "") -> dict:
    """Headers for an MCP request, or {} when the server needs no OAuth."""
    if not connected(name):
        return {}
    return {"Authorization": "Bearer " + access_token(name, url)}


def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="OAuth for remote MCP servers")
    ap.add_argument("action", choices=["connect", "disconnect", "status"])
    ap.add_argument("name")
    ap.add_argument("url", nargs="?")
    a = ap.parse_args()
    if a.action == "connect":
        connect(a.name, a.url or "")
        print(f"signed in to {a.name}")
    elif a.action == "disconnect":
        print("removed" if disconnect(a.name) else "was not signed in")
    else:
        print(json.dumps({a.name: {"connected": connected(a.name),
                                   "expires_at": (store().get(a.name) or {}).get("expires_at")}},
                         indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
