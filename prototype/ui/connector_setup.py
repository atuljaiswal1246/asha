"""Guided connector setup — steps, credential validation, and a user key store.

Figma is the pilot. The guide the app shows (``connector_guide``) comes from
``GUIDES``; validation uses figma_rest's Personal Access Token path
(``X-Figma-Token``) against ``GET /v1/me``; and the token is stored in a 0600
JSON file under ``jarvis_paths.data_dir()`` — never in the repo and never in
``.env``. A saved token is also installed into the process environment slot
figma_rest reads (``JARVIS_FIGMA_PAT``) so the brain's Figma tools resolve it.

Nothing here ever logs or echoes the token: every message passes through
``_scrub`` and the store is the only place the value is written.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

import figma_rest
import jarvis_paths

logger = logging.getLogger(__name__)

KEYS_FILENAME = "connector_keys.json"
FIGMA = "figma"

# The process slot figma_rest reads for a Personal Access Token. The alias slot
# is used (not FIGMA_TOKEN) so an explicit, already-working env token is never
# clobbered by the in-app store.
_ENV_BY_CONNECTOR = {FIGMA: figma_rest.PAT_ENV_VARS[-1]}

# figma_rest translates httpx network failures into this exact sentence prefix.
_NETWORK_PREFIX = "Could not reach Figma"

# Belt-and-braces redaction: PATs look like ``figd_...``.
_TOKEN_SHAPE = re.compile(r"figd_[A-Za-z0-9_\-]+")

GUIDES: dict[str, dict] = {
    FIGMA: {
        "id": FIGMA,
        "label": "Figma",
        "icon": "\U0001f3a8",
        "summary": ("Let Jarvis read your Figma design files so it can work "
                    "from the real design."),
        "why": ("Jarvis reads Figma through its official REST API; this token "
                "is how it proves the requests are yours. It is stored on this "
                "computer only and is never shared."),
        "field_label": "Personal access token",
        "field_placeholder": "figd_\u2026",
        "field_hint": ("Figma shows the token only once, right after you create "
                       "it \u2014 copy it before closing that dialog."),
        "docs_url": ("https://developers.figma.com/docs/rest-api/"
                     "personal-access-tokens/"),
        "steps": [
            {"text": "Open Figma\u2019s account settings.",
             "url": "https://www.figma.com/settings",
             "link_label": "Open Figma settings"},
            {"text": "Choose the Security tab, then find Personal access tokens."},
            {"text": "Click Generate new token and name it \u201cJarvis\u201d."},
            {"text": ("Enable the scopes file_content:read (read designs) and "
                      "current_user:read (verify the connection).")},
            {"text": ("Click Generate token and copy it \u2014 Figma shows it "
                      "only once.")},
            {"text": "Paste the token below and press Test connection."},
        ],
    },
}


class ConnectorSetupError(Exception):
    """Unknown connector, missing token, or an unusable store."""


def _scrub(text: str, secret: str = "") -> str:
    """Return *text* with the secret (and any PAT-shaped run) removed."""
    out = str(text or "")
    if secret:
        out = out.replace(secret, "***")
    return _TOKEN_SHAPE.sub("figd_***", out)


# ── user key store ───────────────────────────────────────────────────────────
class KeyStore:
    """A 0600 JSON file of connector credentials in the app data dir."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else (jarvis_paths.data_dir() / KEYS_FILENAME)

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
                fh.write("\n")
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.replace(str(tmp), str(self.path))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def get(self, connector: str) -> dict | None:
        return self._read().get(connector)

    def set(self, connector: str, value: dict) -> None:
        data = self._read()
        data[connector] = value
        self._write(data)

    def delete(self, connector: str) -> bool:
        data = self._read()
        got = data.pop(connector, None)
        self._write(data)
        return got is not None

    def all(self) -> list[str]:
        return sorted(self._read().keys())


def store() -> KeyStore:
    """The live key store, resolved against the current data dir."""
    return KeyStore()


# ── guide content ────────────────────────────────────────────────────────────
def connector_ids() -> list[str]:
    return sorted(GUIDES)


def guide(connector: str, *, store_: KeyStore | None = None) -> dict:
    """Return the guide for *connector*, plus its saved/connected state."""
    meta = GUIDES.get(connector)
    if meta is None:
        raise ConnectorSetupError(f"no setup guide for {connector!r}")
    rec = (store_ or store()).get(connector) or {}
    out = dict(meta)
    out["steps"] = [dict(step) for step in meta["steps"]]
    out["connected"] = bool(rec.get("secret"))
    out["unverified"] = bool(rec.get("unverified"))
    return out


# ── validation ───────────────────────────────────────────────────────────────
class _PatFigmaClient(figma_rest.FigmaClient):
    """FigmaClient pinned to the PAT path, for the /v1/me identity call."""

    def _resolve_auth(self) -> tuple[str, str]:
        return "pat", self._access_token

    def me(self) -> dict:
        return self._request("GET", "/v1/me")


def _result(ok: bool, reason: str, message: str, *, network: bool = False,
            account: str = "", secret: str = "") -> dict:
    return {"ok": bool(ok), "reason": reason, "message": _scrub(message, secret),
            "network": bool(network), "account": account}


def validate(connector: str, secret: str, *, transport=None,
             base_url: str = figma_rest.BASE_URL, timeout: float = 20.0) -> dict:
    """Test a pasted credential with a live, zero-side-effect call.

    Returns ``{ok, reason, message, network, account}``. Never saves; never
    includes the secret in any field. ``transport`` is for mocked tests only.
    """
    if connector not in GUIDES:
        raise ConnectorSetupError(f"no setup guide for {connector!r}")
    secret = (secret or "").strip()
    if not secret:
        return _result(False, "empty", "Paste an access token first.")
    if connector == FIGMA:
        return _validate_figma(secret, transport=transport, base_url=base_url,
                               timeout=timeout)
    raise ConnectorSetupError(f"no validator for {connector!r}")


def _validate_figma(secret: str, *, transport, base_url: str,
                    timeout: float) -> dict:
    client = _PatFigmaClient(secret, base_url=base_url, timeout=timeout,
                             transport=transport)
    try:
        me = client.me()
    except figma_rest.FigmaRateLimited as exc:
        wait = (f" Retry in about {int(exc.retry_after)}s."
                if exc.retry_after else " Retry after a short wait.")
        return _result(False, "rate_limit",
                       "Figma is rate-limiting requests right now." + wait,
                       secret=secret)
    except figma_rest.FigmaAuthError:
        return _result(False, "auth",
                       "Figma rejected that token \u2014 it is invalid or "
                       "expired, or it is missing the required scopes. "
                       "Generate a new token and try again.", secret=secret)
    except figma_rest.FigmaRequestError as exc:
        text = str(exc)
        if text.startswith(_NETWORK_PREFIX):
            return _result(False, "network",
                           "Could not reach Figma \u2014 check your internet "
                           "connection, then test again.", network=True,
                           secret=secret)
        return _result(False, "error", f"Figma returned an error: {text}",
                       secret=secret)
    except Exception as exc:  # noqa: BLE001 - never leak, never crash the UI
        return _result(False, "error",
                       f"Could not validate the token ({type(exc).__name__}).",
                       secret=secret)
    account = ""
    if isinstance(me, dict):
        account = str(me.get("email") or me.get("handle")
                      or me.get("id") or "").strip()
    message = (f"Connected to Figma as {account}." if account
               else "Token is valid \u2014 Figma accepted it.")
    return _result(True, "ok", message, account=account, secret=secret)


# ── persistence + env bridge ─────────────────────────────────────────────────
def _env_name(connector: str) -> str:
    return _ENV_BY_CONNECTOR.get(connector, "")


def _install_env(connector: str, secret: str) -> None:
    name = _env_name(connector)
    if name and secret:
        os.environ[name] = secret


def _clear_env(connector: str) -> None:
    name = _env_name(connector)
    if name:
        os.environ.pop(name, None)


def save(connector: str, secret: str, *, unverified: bool = False,
         store_: KeyStore | None = None) -> dict:
    """Persist a credential and make it available to the connector's tools."""
    if connector not in GUIDES:
        raise ConnectorSetupError(f"no setup guide for {connector!r}")
    secret = (secret or "").strip()
    if not secret:
        raise ConnectorSetupError("no access token to save")
    (store_ or store()).set(connector, {"secret": secret,
                                        "unverified": bool(unverified)})
    _install_env(connector, secret)
    return {"connector": connector, "saved": True, "unverified": bool(unverified)}


def delete(connector: str, *, store_: KeyStore | None = None) -> bool:
    removed = (store_ or store()).delete(connector)
    _clear_env(connector)
    return removed


def install_saved_keys(*, store_: KeyStore | None = None) -> int:
    """Load saved credentials into the process env so tools work after restart."""
    try:
        st = store_ or store()
        records = {cid: st.get(cid) for cid in st.all()}
    except Exception:  # noqa: BLE001 - startup must never die on a bad store
        return 0
    count = 0
    for cid, rec in records.items():
        secret = rec.get("secret") if isinstance(rec, dict) else ""
        if secret:
            _install_env(cid, secret)
            count += 1
    return count
