"""Static UI server for Jarvis with Cache-Control: no-store (prevents stale-page bugs).

Routes (order matters):
  /desktop/*  →  local opencode desktop renderer from opencode-desktop/
  /api/*      →  proxy to http://127.0.0.1:8201/* (desktop renderer backend calls)
  /opencode/* →  proxy to http://127.0.0.1:8201/* (opencode web UI, no auth)
  /assets/*, /favicon*, /site.webmanifest → proxy to :8201 (opencode SPA assets)
  /*          →  static files from static/
"""
import os
import sys
import urllib.request
import urllib.error
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(os.path.join(_BASE_DIR, "static"))

DESKTOP_DIR = os.path.join(_BASE_DIR, "opencode-desktop")
OPENCODE_UPSTREAM = "http://127.0.0.1:8201"

# opencode desktop's _headers: module scripts MUST be application/javascript
# (text/html breaks module execution → white screen). Follow those rules.
DESKTOP_MIME_OVERRIDES = {
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".css": "text/css",
}


ASHA_ASSETS = {"/", "/index.html", "/app.js", "/styles.css", "/asha.svg"}


def _is_desktop_request(path: str) -> bool:
    """Serve the opencode desktop renderer from local files (NOT proxied)."""
    return path == "/desktop" or path.startswith("/desktop/")


def _is_api_request(path: str) -> bool:
    """The desktop renderer's backend calls — /api/* plus opencode's bare REST
    endpoints (/global/*, /provider, /path, /project, /session/status). The
    desktop renderer calls these at root (unlike the web UI's /api/*)."""
    base = path.split("?", 1)[0]
    if path == "/api" or path.startswith("/api/"):
        return True
    if base == "/provider" or base.startswith("/provider/"):
        return True
    if base == "/path" or base.startswith("/path/"):
        return True
    if base == "/project" or base.startswith("/project/"):
        return True
    if base == "/session" or base.startswith("/session/"):
        return True
    if base.startswith("/global/"):
        return True
    return False


def _is_opencode_request(path: str) -> bool:
    """Route to opencode when the path is /opencode/* OR opencode's absolute
    asset paths (/assets/, /favicon*, /site.webmanifest). opencode's SPA uses
    absolute asset paths, so they must also be proxied or the iframe is blank
    (HTML loads, JS/CSS 404). Jarvis only serves its 4 known files."""
    if path in ASHA_ASSETS:
        return False
    if path.startswith("/opencode"):
        return True
    if path.startswith("/assets/") or path.startswith("/favicon") or path == "/site.webmanifest":
        return True
    return False


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        self._is_desktop_file = False
        super().__init__(*args, **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def guess_type(self, path):
        if self._is_desktop_file:
            _, ext = os.path.splitext(path)
            if ext in DESKTOP_MIME_OVERRIDES:
                return DESKTOP_MIME_OVERRIDES[ext]
        return super().guess_type(path)

    def log_message(self, *args):
        pass

    def _proxy(self):
        """Proxy to the opencode web upstream (:8201)."""
        # /opencode/* → strip the prefix (maps to opencode root).
        # Absolute asset paths (/assets/*, /favicon*) → pass through as-is.
        if self.path.startswith("/opencode"):
            upstream_path = self.path[9:]
        else:
            upstream_path = self.path
        if not upstream_path.startswith("/"):
            upstream_path = "/" + upstream_path
        url = OPENCODE_UPSTREAM + upstream_path

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        req = urllib.request.Request(url, data=body, method=self.command)
        # opencode web 403s the default "Python-urllib/3.13" UA — send a
        # browser-like one so the proxied iframe request is accepted.
        req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")
        for key in ("Content-Type", "Accept", "Authorization"):
            val = self.headers.get(key)
            if val:
                req.add_header(key, val)

        try:
            resp = urllib.request.urlopen(req, timeout=10)
            self.send_response(resp.status)
            for key, val in resp.getheaders():
                if key.lower() in ("content-type", "content-length", "cache-control"):
                    self.send_header(key, val)
            self.end_headers()
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"Upstream error: {e.code}".encode())
        except Exception:
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"opencode web unreachable")

    def _serve_desktop(self):
        """Serve the desktop renderer from opencode-desktop/ as local files."""
        if self.path == "/desktop":
            self.send_response(301)
            self.send_header("Location", "/desktop/")
            self.end_headers()
            return
        original_path = self.path
        original_dir = self.directory
        self.path = "/" + self.path[len("/desktop/"):]
        self.directory = DESKTOP_DIR
        self._is_desktop_file = True
        try:
            super().do_GET()
        finally:
            self.path = original_path
            self.directory = original_dir
            self._is_desktop_file = False

    def do_GET(self):
        if _is_desktop_request(self.path):
            self._serve_desktop()
        elif _is_api_request(self.path) or _is_opencode_request(self.path):
            self._proxy()
        else:
            super().do_GET()

    def do_POST(self):
        if _is_api_request(self.path) or _is_opencode_request(self.path):
            self._proxy()
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self):
        if _is_desktop_request(self.path):
            self._serve_desktop()
        elif _is_api_request(self.path) or _is_opencode_request(self.path):
            self._proxy()
        else:
            super().do_HEAD()


ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
