"""Figma REST client — read design files over Figma's official REST API.

The remote Figma MCP server is gated by Figma's client-catalog allowlist (see
``mcp_oauth._hint``), so Jarvis reads Figma with an OAuth bearer token against
https://api.figma.com instead.

Auth reuses the shared MCP token store: ``mcp_oauth.access_token`` resolves a
live (refreshing) bearer token, and the token is fetched lazily — importing or
constructing this module never touches the Keychain or the environment.

Docs: Introduction (base URL), Authentication, Scopes, Figma files, Projects,
Rate Limits, Errors.
"""
from __future__ import annotations

import logging
import os
import secrets
import time
import urllib.parse
import webbrowser

import httpx

import mcp_oauth
import oauth_common

logger = logging.getLogger(__name__)

BASE_URL = "https://api.figma.com"
DEFAULT_DEPTH = 1
MAX_NODE_IDS = 50
MAX_NODES = 500

# Personal Access Token env vars, in preference order. A PAT authenticates the
# REST API with the ``X-Figma-Token`` header — no OAuth app, redirect URI or
# scope registration. Docs: "Authentication" / "Personal access tokens".
PAT_ENV_VARS = ("FIGMA_TOKEN", "JARVIS_FIGMA_PAT")

# Figma REST OAuth 2 endpoints — NOT the MCP server (which is catalog-gated).
# Docs: OAuth apps (authorize/token), "Refreshing tokens" (refresh), Scopes.
AUTHORIZE_URL = "https://www.figma.com/oauth"
TOKEN_URL = "https://api.figma.com/v1/oauth/token"
REFRESH_URL = "https://api.figma.com/v1/oauth/refresh"
CONNECTOR = "Figma"
DEFAULT_SCOPES = ("file_content:read", "folders:read", "projects:read")

_KEEP_KEYS = ("id", "name", "type", "characters", "children")


class FigmaError(Exception):
    """Base class for every Figma REST failure; carries the HTTP status."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class FigmaConfigError(FigmaError):
    """Figma is not connected, or no usable access token is available."""


class FigmaAuthError(FigmaError):
    """HTTP 401/403 — invalid or expired token, or a missing scope."""


class FigmaNotFoundError(FigmaError):
    """HTTP 404 — the file, node, project or folder does not exist."""


class FigmaRequestError(FigmaError):
    """HTTP 400 / other 4xx-5xx, non-JSON body, network failure, bad argument."""


class FigmaRateLimited(FigmaError):
    """HTTP 429 — the leaky bucket is empty; retry per ``Retry-After``."""

    def __init__(self, message: str, *, retry_after: int | None = None,
                 upgrade_link: str = "", plan_tier: str = "",
                 limit_type: str = "") -> None:
        super().__init__(message, status=429)
        self.retry_after = retry_after
        self.upgrade_link = upgrade_link
        self.plan_tier = plan_tier
        self.limit_type = limit_type


# ── bounded tree trimming ────────────────────────────────────────────────────
def _trim_node(node, budget: list[int]):
    """Keep only id/name/type/characters/children, bounded by ``budget[0]``.

    Never mutates ``node``. Returns ``(trimmed_node_or_None, truncated)``.
    """
    if not isinstance(node, dict):
        return None, False
    if budget[0] <= 0:
        return None, True
    budget[0] -= 1
    out = {key: node[key] for key in _KEEP_KEYS if key in node}
    truncated = False
    children = node.get("children")
    if children:
        kept = []
        for child in children:
            if budget[0] <= 0:
                truncated = True
                break
            trimmed, child_truncated = _trim_node(child, budget)
            if trimmed is not None:
                kept.append(trimmed)
            truncated = truncated or child_truncated
        if "children" in out:
            out["children"] = kept
    return out, truncated


def trim_tree(root) -> tuple[dict, bool]:
    """Return a (trimmed document, truncated) pair bounded by ``MAX_NODES``."""
    trimmed, truncated = _trim_node(root, [MAX_NODES])
    return (trimmed if trimmed is not None else {}), truncated


# ── response helpers ─────────────────────────────────────────────────────────
def _short(text: str, limit: int = 200) -> str:
    return " ".join(str(text).split())[:limit]


def _provider_reason(resp: httpx.Response) -> str:
    """A short provider reason from a JSON body — never raw HTML."""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - non-JSON bodies are ignored
        return ""
    if not isinstance(body, dict):
        return ""
    for key in ("message", "err", "error"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return _short(value)
        if isinstance(value, dict):
            for inner in ("message", "err"):
                nested = value.get(inner)
                if isinstance(nested, str) and nested.strip():
                    return _short(nested)
    return ""


def _int_header(resp: httpx.Response, name: str) -> int | None:
    raw = resp.headers.get(name)
    if not raw:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _validate_depth(depth) -> None:
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
        raise FigmaRequestError(
            f"depth must be a positive integer (got {depth!r}).")


# ── connect (one-time OAuth authorisation) ───────────────────────────────────
def _oauth_host() -> str:
    """The loopback host shown in the redirect URI (bind is always 127.0.0.1)."""
    return (os.environ.get("JARVIS_OAUTH_HOST") or "127.0.0.1").strip() or "127.0.0.1"


def redirect_uri(port: int | None = None) -> str:
    """The exact callback URL to register in the Figma OAuth app.

    Figma will only exchange a code against a redirect URL that is listed in the
    app's OAuth credentials, and the exchange must byte-match it — see
    "OAuth apps" in Figma's REST docs.
    """
    chosen = mcp_oauth.default_port() if port is None else port
    return "http://%s:%d/callback" % (_oauth_host(), chosen)


def _client_credentials() -> tuple[str, str]:
    try:
        client = mcp_oauth.preconfigured_client(CONNECTOR, BASE_URL)
    except mcp_oauth.OAuthError as exc:
        raise FigmaConfigError(
            "Figma OAuth app not configured. Set JARVIS_MCP_CLIENT_ID_FIGMA "
            "and JARVIS_MCP_CLIENT_SECRET_FIGMA, then add this exact redirect "
            "URL under the app's OAuth credentials at "
            f"https://www.figma.com/developers/apps: {redirect_uri()}. "
            f"({exc})") from None
    return client["client_id"], client.get("client_secret", "")


def _translate_token_error(text: str, redirect: str) -> FigmaError:
    """Turn Figma's token-endpoint failure into one actionable sentence."""
    low = (text or "").lower()
    if "redirect" in low or "invalid_grant" in low:
        return FigmaConfigError(
            "Figma rejected the callback. Add this exact redirect URL under "
            "the OAuth app's credentials at "
            f"https://www.figma.com/developers/apps: {redirect}")
    if "invalid_client" in low:
        return FigmaConfigError(
            "Figma rejected the app credentials. Check "
            "JARVIS_MCP_CLIENT_ID_FIGMA and JARVIS_MCP_CLIENT_SECRET_FIGMA.")
    return FigmaAuthError(f"Figma sign-in failed: {_short(text)}")


def connect(*, open_browser: bool = True, timeout: float = 240,
            port: int | None = None, scopes=None) -> dict:
    """Authorize the Figma REST API once. Blocks until the user approves.

    Reuses the shared PKCE + loopback + token store: the token lands under the
    same ``Figma`` record ``mcp_oauth.access_token`` refreshes. The token
    endpoint is stored as Figma's dedicated refresh URL so the shared refresher
    calls the right endpoint.
    """
    client_id, client_secret = _client_credentials()
    scope = " ".join(scopes or DEFAULT_SCOPES)
    verifier, challenge = oauth_common.pkce()
    state = secrets.token_urlsafe(24)
    with oauth_common.Loopback(
            mcp_oauth.default_port() if port is None else port,
            host=_oauth_host()) as lb:
        params = {
            "client_id": client_id,
            "redirect_uri": lb.redirect_uri,
            "scope": scope,
            "state": state,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        auth_url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)
        if open_browser:
            webbrowser.open(auth_url)
        got = lb.wait(timeout)
        if not got:
            raise FigmaConfigError(
                "Timed out waiting for Figma approval. If the browser showed "
                "an error, confirm this exact redirect URL is registered in "
                f"the OAuth app: {lb.redirect_uri}")
        if got.get("error"):
            raise FigmaAuthError(
                f"Figma sign-in was refused: {_short(got.get('error'))}. If it "
                f"was a redirect error, register {lb.redirect_uri} in the app.")
        if got.get("state") != state:
            raise FigmaAuthError(
                "Figma callback state did not match — discarded.")
        data = {
            "grant_type": "authorization_code",
            "code": got["code"],
            "redirect_uri": lb.redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        }
        basic = (mcp_oauth._basic(client_id, client_secret)
                 if client_secret else "")
        try:
            tok = mcp_oauth._token_request(
                {"token_endpoint": TOKEN_URL}, data, basic)
        except mcp_oauth.OAuthError as exc:
            raise _translate_token_error(str(exc), lb.redirect_uri) from None
    return mcp_oauth._save(
        CONNECTOR, tok, url=BASE_URL, resource=BASE_URL,
        token_endpoint=REFRESH_URL, client_id=client_id,
        client_secret=client_secret)


def _main() -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description="Figma REST connector for Jarvis")
    ap.add_argument("action", choices=["connect", "disconnect", "status"])
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    if a.action == "connect":
        rec = connect(open_browser=not a.no_browser, port=a.port)
        print(f"connected Figma (expires in {int(rec['expires_at'] - time.time())}s)")
    elif a.action == "disconnect":
        print("removed" if mcp_oauth.disconnect(CONNECTOR) else "was not connected")
    else:
        rec = mcp_oauth.store().get(CONNECTOR) or {}
        print(json.dumps({"connected": bool(rec),
                          "expires_at": rec.get("expires_at"),
                          "redirect_uri": redirect_uri(a.port)}, indent=2))
    return 0


class FigmaClient:
    """A minimal, bounded, read-only Figma REST client."""

    BASE_URL = BASE_URL

    def __init__(self, access_token=None, *, connector: str = "Figma",
                 base_url: str = BASE_URL, timeout: float = 20.0,
                 transport=None) -> None:
        self._access_token = access_token or None
        self._connector = connector
        self._base_url = base_url
        self._timeout = timeout
        self._transport = transport

    # ── token / transport ────────────────────────────────────────────────────
    def _resolve_auth(self) -> tuple[str, str]:
        """Return ``(mode, token)`` where mode is ``"pat"`` or ``"oauth"``.

        An explicitly injected ``access_token`` is honoured first (it is an
        intentional override used by the tool layer and tests); otherwise a
        configured Personal Access Token wins over the stored OAuth bearer
        token, which is unchanged when no PAT is set.
        """
        if self._access_token:
            return "oauth", self._access_token
        for name in PAT_ENV_VARS:
            pat = (os.environ.get(name) or "").strip()
            if pat:
                return "pat", pat
        try:
            token = mcp_oauth.access_token(self._connector, self._base_url)
        except Exception:  # noqa: BLE001 - any store/refresh failure is "not connected"
            token = ""
        if not token:
            raise FigmaConfigError(
                "Figma is not connected: no access token. Set FIGMA_TOKEN to a "
                "Personal Access Token (Figma > Settings > Security > Personal "
                "access tokens). Connect Figma once with "
                "`python figma_rest.py connect`; if Figma rejects the callback, "
                "add this exact redirect URL under the app's OAuth credentials: "
                f"{redirect_uri()}")
        return "oauth", token

    def _request(self, method: str, path: str, params=None) -> dict:
        mode, token = self._resolve_auth()
        logger.info("Figma REST auth mode: %s",
                    "Personal Access Token" if mode == "pat" else "OAuth bearer")
        headers = {"Accept": "application/json"}
        if mode == "pat":
            headers["X-Figma-Token"] = token
        else:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with httpx.Client(base_url=self._base_url, timeout=self._timeout,
                              transport=self._transport) as client:
                resp = client.request(method, path, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise FigmaRequestError(
                f"Could not reach Figma: {_short(exc)}") from None
        return self._decode(resp, mode)

    def _decode(self, resp: httpx.Response, mode: str = "oauth") -> dict:
        status = resp.status_code
        if status == 429:
            retry_after = _int_header(resp, "Retry-After")
            upgrade_link = resp.headers.get("X-Figma-Upgrade-Link", "")
            plan_tier = resp.headers.get("X-Figma-Plan-Tier", "")
            limit_type = resp.headers.get("X-Figma-Rate-Limit-Type", "")
            parts = ["Figma rate limit hit (HTTP 429)."]
            if retry_after is not None:
                parts.append(
                    f"Retry after {retry_after}s (see Figma's Retry-After).")
            else:
                parts.append(
                    "Retry after a short wait (see Figma's Retry-After).")
            facts = []
            if plan_tier:
                facts.append(f"Plan={plan_tier}")
            if limit_type:
                facts.append(f"type={limit_type}")
            if facts:
                parts.append(", ".join(facts) + ".")
            if upgrade_link:
                parts.append(f"Upgrade link: {upgrade_link}")
            raise FigmaRateLimited(
                " ".join(parts), retry_after=retry_after,
                upgrade_link=upgrade_link, plan_tier=plan_tier,
                limit_type=limit_type)
        if status in (401, 403):
            if mode == "pat":
                raise FigmaAuthError(
                    f"Figma rejected the Personal Access Token (HTTP {status}): "
                    "it is invalid or expired, or missing scope "
                    "file_content:read. Generate a new token in Figma > "
                    "Settings > Security > Personal access tokens, set "
                    "FIGMA_TOKEN, and retry.", status=status)
            raise FigmaAuthError(
                f"Figma rejected the OAuth token (HTTP {status}): invalid or "
                "expired, or missing scope file_content:read. Reconnect Figma "
                "and retry.", status=status)
        if status == 404:
            raise FigmaNotFoundError(
                "Figma resource not found (HTTP 404): check the file key, node "
                "id, project id or folder id.", status=status)
        if status >= 400:
            reason = _provider_reason(resp)
            message = f"Figma request failed (HTTP {status})"
            if reason:
                message += f": {reason}"
            if status == 400:
                message = "Figma rejected the request (HTTP 400)"
                if reason:
                    message += f": {reason}"
            raise FigmaRequestError(message, status=status)
        try:
            body = resp.json()
        except ValueError:
            raise FigmaRequestError(
                "Figma returned a non-JSON response", status=status) from None
        if not isinstance(body, dict):
            raise FigmaRequestError(
                "Figma returned an unexpected response", status=status)
        return body

    # ── public API ───────────────────────────────────────────────────────────
    def list_files(self, project_id=None, *, folder_id=None,
                   branch_data: bool = False) -> list[dict]:
        """List a project's or folder's files (Figma has no "all my files")."""
        if not folder_id and not project_id:
            raise FigmaRequestError(
                "Figma has no \"list all my files\" endpoint. Pass a project_id "
                "(scope projects:read) or a folder_id (scope folders:read).")
        params = {"branch_data": "true"} if branch_data else {}
        if folder_id:
            data = self._request("GET", f"/v2/folders/{folder_id}/files", params)
        else:
            data = self._request("GET", f"/v1/projects/{project_id}/files", params)
        files = data.get("files") if isinstance(data, dict) else None
        out = []
        for entry in files or []:
            if not isinstance(entry, dict):
                continue
            out.append({
                "key": entry.get("key"),
                "name": entry.get("name"),
                "last_modified": entry.get("last_modified"),
                "thumbnail_url": entry.get("thumbnail_url"),
            })
        return out

    def get_file(self, key, depth: int = DEFAULT_DEPTH) -> dict:
        """Read one file (depth=1 returns only the Page nodes)."""
        _validate_depth(depth)
        data = self._request("GET", f"/v1/files/{key}", {"depth": depth})
        document, tree_truncated = trim_tree(data.get("document"))
        return {
            "name": data.get("name"),
            "lastModified": data.get("lastModified"),
            "editorType": data.get("editorType"),
            "version": data.get("version"),
            "role": data.get("role"),
            "linkAccess": data.get("linkAccess"),
            "document": document,
            "truncated": bool(data.get("truncated")) or tree_truncated,
        }

    def read_nodes(self, file_key, node_ids, depth: int = DEFAULT_DEPTH) -> dict:
        """Read specific nodes; ids that don't exist are reported as missing."""
        _validate_depth(depth)
        if isinstance(node_ids, str):
            ids = [part.strip() for part in node_ids.split(",") if part.strip()]
        elif isinstance(node_ids, (list, tuple)):
            ids = [str(part).strip() for part in node_ids if str(part).strip()]
        else:
            raise FigmaRequestError(
                "node_ids must be a list, tuple, or comma-separated string of "
                "node ids.")
        if not ids:
            raise FigmaRequestError(
                "node_ids is empty: pass at least one Figma node id (e.g. '1:2').")
        if len(ids) > MAX_NODE_IDS:
            raise FigmaRequestError(
                f"too many node ids ({len(ids)}): Figma accepts at most "
                f"{MAX_NODE_IDS} ids per request.")
        data = self._request(
            "GET", f"/v1/files/{file_key}/nodes",
            {"ids": ",".join(ids), "depth": depth})
        nodes = data.get("nodes")
        if not isinstance(nodes, dict):
            raise FigmaRequestError("Figma returned an unexpected nodes response")
        trimmed: dict = {}
        missing: list[str] = []
        truncated = False
        for node_id, entry in nodes.items():
            if entry is None:
                missing.append(node_id)
                continue
            document = entry.get("document") if isinstance(entry, dict) else entry
            node, node_truncated = trim_tree(document)
            trimmed[node_id] = node
            truncated = truncated or node_truncated
        return {"nodes": trimmed, "missing": missing, "truncated": truncated}


if __name__ == "__main__":
    raise SystemExit(_main())
