"""Tests for remote-MCP OAuth (mcp_oauth.py).

A fake authorization server plus a fake MCP endpoint stand in for Slack et al,
so the whole discovery -> registration -> PKCE -> token -> refresh path is
exercised for real. No network, no browser, no Keychain.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mcp_oauth  # noqa: E402
import oauth_common  # noqa: E402


class FakeOAuthServer:
    """401 + WWW-Authenticate, metadata, registration, token endpoint."""

    def __init__(self, *, register=True, challenge=True, token_status=200):
        self.requests: list[tuple[str, dict]] = []
        self.register_enabled = register
        self.use_challenge = challenge
        self.token_status = token_status
        self.issued = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _read(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                ctype = self.headers.get("Content-Type", "")
                if "form-urlencoded" in ctype:
                    return {k: v[0] for k, v in
                            urllib.parse.parse_qs(raw.decode()).items()}
                try:
                    return json.loads(raw or b"{}")
                except ValueError:
                    return {"_raw": raw.decode(errors="replace")}

            def do_POST(self):  # noqa: N802
                body = self._read()
                outer.requests.append((self.path, body))
                if self.path == "/mcp":
                    if outer.use_challenge:
                        self.send_response(401)
                        self.send_header(
                            "WWW-Authenticate",
                            'Bearer resource_metadata="%s/.well-known/oauth-protected-resource"'
                            % outer.base)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    return self._json(200, {"jsonrpc": "2.0", "id": body.get("id"),
                                            "result": {"tools": []}})
                if self.path == "/register":
                    if not outer.register_enabled:
                        return self._json(404, {"error": "no_dcr"})
                    return self._json(200, {"client_id": "cid-1"})
                if self.path == "/token":
                    if outer.token_status >= 400:
                        return self._json(outer.token_status, {"error": "invalid_grant"})
                    outer.issued += 1
                    body_out = {"access_token": "at-%d" % outer.issued,
                                "expires_in": 3600,
                                "scope": "read"}
                    if body.get("grant_type") == "authorization_code":
                        body_out["refresh_token"] = "rt-1"
                    return self._json(200, body_out)
                return self._json(404, {})

            def do_GET(self):  # noqa: N802
                outer.requests.append((self.path, {}))
                if self.path == "/.well-known/oauth-protected-resource":
                    return self._json(200, {"resource": outer.base,
                                            "authorization_servers": [outer.base],
                                            "scopes_supported": ["read", "write"]})
                if self.path == "/.well-known/oauth-authorization-server":
                    return self._json(200, {
                        "authorization_endpoint": outer.base + "/authorize",
                        "token_endpoint": outer.base + "/token",
                        "registration_endpoint": outer.base + "/register"})
                return self._json(404, {})

            def log_message(self, *a):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:%d" % self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()

    @property
    def mcp_url(self):
        return self.base + "/mcp"

    def find(self, path):
        return [b for p, b in self.requests if p == path]


class FakeBrowser:
    """Stands in for the user: approve -> hit the loopback with code+state."""

    def __init__(self, *, state_override=None, error=None):
        self.url = ""
        self.state_override = state_override
        self.error = error

    def __call__(self, url):
        self.url = url
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        params = {"code": "AUTHCODE"}
        if self.error:
            params = {"error": self.error}
        else:
            params["state"] = self.state_override or q["state"][0]
        target = q["redirect_uri"][0] + "?" + urllib.parse.urlencode(params)

        def hit():
            time.sleep(0.15)
            try:
                urllib.request.urlopen(target).read()
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=hit, daemon=True).start()
        return True


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jarvis-mcpoauth-")
        mcp_oauth.set_store(oauth_common.FileStore(Path(self.tmp) / "t.json", "jarvis-mcp"))
        self.srv = FakeOAuthServer()
        self.browser = FakeBrowser()
        self._open = mcp_oauth.webbrowser.open
        mcp_oauth.webbrowser.open = self.browser

    def tearDown(self):
        mcp_oauth.webbrowser.open = self._open
        mcp_oauth.set_store(None)
        self.srv.stop()

    # ── discovery ────────────────────────────────────────────────────────────
    def test_discovers_via_www_authenticate(self):
        disc = mcp_oauth.discover(self.srv.mcp_url)
        self.assertEqual(disc["asm"]["token_endpoint"], self.srv.base + "/token")
        self.assertEqual(disc["resource"], self.srv.base)
        self.assertEqual(disc["prm"]["scopes_supported"], ["read", "write"])

    def test_falls_back_to_well_known_when_no_challenge(self):
        self.srv.use_challenge = False
        disc = mcp_oauth.discover(self.srv.mcp_url)
        self.assertIn("token_endpoint", disc["asm"])

    def test_no_metadata_is_a_clear_error(self):
        self.srv.use_challenge = False
        self.srv.stop()
        with self.assertRaises(mcp_oauth.OAuthError) as cm:
            mcp_oauth.discover("http://127.0.0.1:1/mcp")
        self.assertIn("OAuth metadata", str(cm.exception))

    # ── the flow ─────────────────────────────────────────────────────────────
    def test_connect_registers_then_exchanges_with_pkce(self):
        rec = mcp_oauth.connect("slack", self.srv.mcp_url, port=0)
        auth_q = urllib.parse.parse_qs(urllib.parse.urlparse(self.browser.url).query)
        self.assertEqual(auth_q["code_challenge_method"], ["S256"])
        self.assertEqual(auth_q["client_id"], ["cid-1"])
        self.assertEqual(auth_q["resource"], [self.srv.base])
        self.assertEqual(auth_q["scope"], ["read write"])
        self.assertTrue(auth_q["code_challenge"][0])
        # the token call carried the verifier + the same resource
        token_body = self.srv.find("/token")[0]
        self.assertEqual(token_body["grant_type"], "authorization_code")
        self.assertEqual(token_body["code"], "AUTHCODE")
        self.assertEqual(token_body["resource"], self.srv.base)
        self.assertTrue(token_body["code_verifier"])
        self.assertEqual(rec["refresh_token"], "rt-1")
        self.assertTrue(mcp_oauth.connected("slack"))

    def test_state_mismatch_is_rejected(self):
        self.browser.state_override = "NOT-THE-STATE"
        with self.assertRaises(mcp_oauth.OAuthError) as cm:
            mcp_oauth.connect("slack", self.srv.mcp_url, port=0)
        self.assertIn("state mismatch", str(cm.exception))
        self.assertFalse(mcp_oauth.connected("slack"))

    def test_user_denial_is_reported(self):
        self.browser.error = "access_denied"
        with self.assertRaises(mcp_oauth.OAuthError) as cm:
            mcp_oauth.connect("slack", self.srv.mcp_url, port=0)
        self.assertIn("access_denied", str(cm.exception))

    def test_access_token_reused_then_refreshed(self):
        mcp_oauth.connect("slack", self.srv.mcp_url, port=0)
        before = self.srv.issued
        self.assertEqual(mcp_oauth.access_token("slack"), "at-%d" % before)  # reused
        self.assertEqual(self.srv.issued, before)
        rec = mcp_oauth.store().get("slack")
        rec["expires_at"] = int(time.time()) - 5
        mcp_oauth.store().set("slack", rec)
        fresh = mcp_oauth.access_token("slack")            # refreshed
        self.assertNotEqual(fresh, "at-%d" % before)
        self.assertEqual(self.srv.find("/token")[-1]["grant_type"], "refresh_token")

    def test_refresh_failure_is_reported(self):
        mcp_oauth.connect("slack", self.srv.mcp_url, port=0)
        rec = mcp_oauth.store().get("slack")
        rec["expires_at"] = int(time.time()) - 5
        mcp_oauth.store().set("slack", rec)
        self.srv.token_status = 400
        with self.assertRaises(mcp_oauth.OAuthError):
            mcp_oauth.access_token("slack")

    def test_not_signed_in_is_reported(self):
        with self.assertRaises(mcp_oauth.OAuthError) as cm:
            mcp_oauth.access_token("nope")
        self.assertIn("not signed in", str(cm.exception))

    def test_registration_failure_is_explained(self):
        self.srv.register_enabled = False
        os.environ.pop("JARVIS_MCP_CLIENT_ID", None)
        with self.assertRaises(mcp_oauth.OAuthError) as cm:
            mcp_oauth.connect("slack", self.srv.mcp_url, port=0)
        self.assertIn("JARVIS_MCP_CLIENT_ID", str(cm.exception))

    def test_disconnect(self):
        mcp_oauth.connect("slack", self.srv.mcp_url, port=0)
        self.assertTrue(mcp_oauth.disconnect("slack"))
        self.assertFalse(mcp_oauth.connected("slack"))


class ClientIntegrationTests(unittest.TestCase):
    """The token actually reaches the request, through the real client."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jarvis-mcpint-")
        mcp_oauth.set_store(oauth_common.FileStore(Path(self.tmp) / "t.json", "jarvis-mcp"))
        self.srv = FakeOAuthServer()
        self.mcp_requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.mcp_requests.append({"auth": self.headers.get("Authorization", ""),
                                           "body": body, "path": self.path})
                out = {"jsonrpc": "2.0", "id": body.get("id"),
                       "result": {"protocolVersion": "2024-11-05", "capabilities": {},
                                  "serverInfo": {"name": "fake-remote"}}
                       if body.get("method") == "initialize" else
                       {"tools": [{"name": "ping", "description": "p"}]}}
                raw = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/mcp" % self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self._httpd.shutdown()
        mcp_oauth.set_store(None)
        self.srv.stop()

    def test_remote_server_sends_the_bearer_from_the_provider(self):
        import mcp_client

        browser = FakeBrowser()
        orig = mcp_oauth.webbrowser.open
        mcp_oauth.webbrowser.open = lambda u: (browser.__call__(u), None)[0]
        try:
            mcp_oauth.connect("remote1", self.srv.mcp_url, port=0)
        finally:
            mcp_oauth.webbrowser.open = orig

        srv = mcp_client.RemoteMCPServer(
            "remote1", self.url,
            token_provider=lambda: mcp_oauth.access_token("remote1"))
        srv.start()
        tools = srv.list_tools()
        self.assertEqual(tools[0]["name"], "ping")
        self.assertTrue(self.mcp_requests[0]["auth"].startswith("Bearer at-"),
                        self.mcp_requests[0]["auth"])

    def test_manager_flags_and_signs_in_via_config(self):
        import mcp_client

        cfg = Path(self.tmp) / "mcp.json"
        cfg.write_text(json.dumps({"servers": [
            {"name": "remote1", "url": self.srv.mcp_url, "oauth": True}]}),
            encoding="utf-8")
        mgr = mcp_client.MCPManager(cfg)
        got = mgr.servers()[0]
        self.assertTrue(got["oauth"])
        self.assertFalse(got["signed_in"])          # not yet
        self.assertTrue(mgr.mark_oauth("remote1", True))

        browser = FakeBrowser()
        orig = mcp_oauth.webbrowser.open
        mcp_oauth.webbrowser.open = lambda u: (browser.__call__(u), None)[0]
        try:
            mcp_oauth.connect("remote1", self.srv.mcp_url, port=0)
        finally:
            mcp_oauth.webbrowser.open = orig
        self.assertTrue(mgr.servers()[0]["signed_in"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
