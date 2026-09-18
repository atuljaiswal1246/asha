"""Shared OAuth pieces: PKCE, a loopback callback, and a token store.

Used by both the Google connectors (google_auth.py) and remote MCP servers
(mcp_oauth.py), so there is exactly one implementation of each. Nothing here
knows about any provider: a prefix namespaces the stored tokens (for example
"jarvis-google" or "jarvis-mcp").
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path


def pkce() -> tuple[str, str]:
    """Return (verifier, S256 challenge)."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def data_dir() -> Path:
    try:
        import jarvis_paths
        return Path(jarvis_paths.data_dir())
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parents[1] / "data"


# ── loopback callback ────────────────────────────────────────────────────────
_RESULT: dict = {}


def _make_handler(path: str):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parts = urllib.parse.urlparse(self.path)
            if parts.path != path:
                self.send_response(404)
                self.end_headers()
                return
            q = urllib.parse.parse_qs(parts.query)
            _RESULT.clear()
            _RESULT.update({k: v[0] for k, v in q.items()})
            ok = "code" in _RESULT
            msg = ("<h1>Connected</h1><p>You can close this tab and return to Jarvis.</p>"
                   if ok else
                   "<h1>Not connected</h1><p>Jarvis did not receive a code.</p>")
            page = ("<!doctype html><body style='font:16px -apple-system,system-ui;"
                    "background:#0e121e;color:#e8ebf5;display:flex;align-items:center;"
                    "justify-content:center;height:100vh;margin:0;text-align:center'>"
                    "<div>" + msg + "</div></body>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page.encode())

        def log_message(self, *a):
            pass

    return Handler


class Loopback:
    """A one-shot http://127.0.0.1:<port>/<path> catcher.

    The socket is bound first and the real port exposed, so the redirect URI
    always matches and port=0 never collides with anything.
    """

    def __init__(self, preferred_port: int = 0, path: str = "/callback",
                 host: str = "127.0.0.1"):
        self.path = path
        self.host = host          # shown in the redirect URI; we still bind 127.0.0.1
        _RESULT.clear()
        self._httpd = http.server.HTTPServer(("127.0.0.1", preferred_port),
                                             _make_handler(path))
        self._httpd.timeout = 1
        self.port = self._httpd.server_address[1]

    @property
    def redirect_uri(self) -> str:
        return "http://%s:%d%s" % (self.host, self.port, self.path)

    def __enter__(self) -> "Loopback":
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def wait(self, timeout: float = 240) -> dict:
        deadline = time.time() + timeout
        while not _RESULT and time.time() < deadline:
            time.sleep(0.25)
        return dict(_RESULT)

    def __exit__(self, *exc) -> None:
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:  # noqa: BLE001
            pass


# ── token storage ────────────────────────────────────────────────────────────
class FileStore:
    """0600 JSON in the data dir (non-macOS fallback, and tests)."""

    def __init__(self, path: Path | None = None, prefix: str = "jarvis"):
        self._path = path or (data_dir() / (prefix + "_tokens.json"))

    def _read(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.chmod(self._path, 0o600)

    def get(self, key: str) -> dict | None:
        return self._read().get(key)

    def set(self, key: str, value: dict) -> None:
        data = self._read()
        data[key] = value
        self._write(data)

    def delete(self, key: str) -> bool:
        data = self._read()
        got = data.pop(key, None)
        self._write(data)
        return got is not None

    def all(self) -> list[str]:
        return sorted(self._read().keys())


class KeychainStore:
    """One generic-password item per key (macOS Keychain)."""

    ACCOUNT = "jarvis"

    def __init__(self, prefix: str = "jarvis"):
        self._prefix = prefix

    def _service(self, key: str) -> str:
        return self._prefix + "-" + key

    def get(self, key: str) -> dict | None:
        r = subprocess.run(
            ["security", "find-generic-password", "-a", self.ACCOUNT,
             "-s", self._service(key), "-w"], capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        try:
            return json.loads(r.stdout.strip())
        except Exception:  # noqa: BLE001
            return None

    def set(self, key: str, value: dict) -> None:
        subprocess.run(
            ["security", "add-generic-password", "-a", self.ACCOUNT,
             "-s", self._service(key), "-w", json.dumps(value), "-U"],
            check=True, capture_output=True)

    def delete(self, key: str) -> bool:
        if self.get(key) is None:
            return False
        subprocess.run(
            ["security", "delete-generic-password", "-a", self.ACCOUNT,
             "-s", self._service(key)], capture_output=True)
        return True

    def all(self) -> list[str]:
        r = subprocess.run(["security", "dump-keychain"],
                           capture_output=True, text=True)
        out = []
        for chunk in r.stdout.split("0x00000007"):
            for line in chunk.splitlines():
                if '"svce"' in line and self._prefix in line:
                    name = line.split('"')[3] if line.count('"') > 3 else ""
                    if name.startswith(self._prefix + "-"):
                        out.append(name[len(self._prefix) + 1:])
        return sorted(set(out))


def make_store(prefix: str, path: Path | None = None):
    """Keychain on macOS, a 0600 file elsewhere (override with JARVIS_TOKEN_STORE)."""
    kind = os.environ.get("JARVIS_TOKEN_STORE", "").strip().lower()
    if not kind:
        import sys
        kind = "keychain" if sys.platform == "darwin" else "file"
    if kind == "keychain":
        return KeychainStore(prefix)
    return FileStore(path, prefix)
