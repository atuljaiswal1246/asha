"""Tests for the on-device Google connector (OAuth + MCP server).

No network, no real Google, no browser: the token endpoint and the Google
REST API are stubbed, and tokens go to a temp file store instead of the
Keychain.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import google_auth  # noqa: E402
import google_mcp_server as gmcp  # noqa: E402


class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.text = body, status, json.dumps(body)

    def json(self):
        return self._body


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jarvis-oauth-")
        google_auth.set_store(google_auth.FileStore(Path(self.tmp) / "tok.json"))
        self._old_data_dir = os.environ.get("JARVIS_DATA_DIR")
        os.environ["JARVIS_DATA_DIR"] = self.tmp
        os.environ["GOOGLE_CLIENT_ID"] = "cid.apps.googleusercontent.com"
        os.environ["GOOGLE_CLIENT_SECRET"] = "shipped-secret"
        google_auth.CLIENT_ID = "cid.apps.googleusercontent.com"
        google_auth.CLIENT_SECRET = "shipped-secret"

    def tearDown(self):
        google_auth.set_store(None)
        if self._old_data_dir is None:
            os.environ.pop("JARVIS_DATA_DIR", None)
        else:
            os.environ["JARVIS_DATA_DIR"] = self._old_data_dir

    def test_authorize_url_has_pkce_offline_and_scopes(self):
        verifier, challenge = google_auth._pkce()
        url = google_auth.authorize_url("gmail", "st4te", challenge)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertEqual(q["code_challenge"], [challenge])
        self.assertEqual(q["access_type"], ["offline"])
        self.assertEqual(q["client_id"], ["cid.apps.googleusercontent.com"])
        self.assertEqual(q["state"], ["st4te"])
        self.assertIn("gmail.readonly", q["scope"][0])
        self.assertEqual(q["code_challenge"], [challenge])

    def test_pkce_challenge_matches_verifier(self):
        verifier, challenge = google_auth._pkce()
        expect = base64.urlsafe_b64encode(
            __import__("hashlib").sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.assertEqual(challenge, expect)

    def test_connect_runs_full_flow(self):
        import httpx
        seen = {}

        orig_post, orig_open = httpx.post, google_auth.webbrowser.open

        def fake_post(url, data=None, timeout=None):
            seen["url"], seen["data"] = url, data
            return _Resp({"access_token": "at-1", "refresh_token": "rt-1",
                          "expires_in": 3600, "scope": "gmail.readonly"})

        def fake_open(url):
            seen["browser"] = url
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            # simulate the user approving: hit the loopback callback
            def hit():
                time.sleep(0.2)
                urllib.request.urlopen(
                    f"{q['redirect_uri'][0]}?code=CODE123&state={q['state'][0]}").read()
            threading.Thread(target=hit, daemon=True).start()
            return True

        httpx.post, google_auth.webbrowser.open = fake_post, fake_open
        try:
            rec = google_auth.connect("gmail", timeout=10, port=0)
        finally:
            httpx.post, google_auth.webbrowser.open = orig_post, orig_open

        self.assertEqual(seen["url"], google_auth.TOKEN)
        self.assertEqual(seen["data"]["code"], "CODE123")
        self.assertEqual(seen["data"]["grant_type"], "authorization_code")
        self.assertIn("code_verifier", seen["data"])       # PKCE proven
        self.assertIn("accounts.google.com", seen["browser"])
        self.assertEqual(rec["refresh_token"], "rt-1")
        self.assertEqual(rec["client_id"], "cid.apps.googleusercontent.com")
        self.assertEqual(google_auth.store().get("gmail")["client_id"],
                         "cid.apps.googleusercontent.com")
        self.assertTrue(google_auth.connected("gmail"))

    def test_state_mismatch_is_rejected(self):
        import httpx
        orig_post, orig_open = httpx.post, google_auth.webbrowser.open
        httpx.post = lambda *a, **k: _Resp({"access_token": "x"})

        def evil(url):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

            def hit():
                time.sleep(0.2)
                urllib.request.urlopen(
                    f"{q['redirect_uri'][0]}?code=CODE&state=WRONG").read()
            threading.Thread(target=hit, daemon=True).start()
            return True

        google_auth.webbrowser.open = evil
        try:
            with self.assertRaises(google_auth.OAuthError):
                google_auth.connect("gmail", timeout=10, port=0)
        finally:
            httpx.post, google_auth.webbrowser.open = orig_post, orig_open
        self.assertFalse(google_auth.connected("gmail"))

    def test_access_token_refreshes_when_expired(self):
        import httpx
        google_auth.store().set("gmail", {"access_token": "old", "refresh_token": "rt",
                                          "expires_at": int(time.time()) - 10})
        calls = []

        def fake_post(url, data=None, timeout=None):
            calls.append(data["grant_type"])
            return _Resp({"access_token": "fresh", "expires_in": 3600})

        orig = httpx.post
        httpx.post = fake_post
        try:
            self.assertEqual(google_auth.access_token("gmail"), "fresh")
        finally:
            httpx.post = orig
        self.assertEqual(calls, ["refresh_token"])

    def test_access_token_reused_while_valid(self):
        google_auth.store().set("gmail", {"access_token": "good", "refresh_token": "rt",
                                          "expires_at": int(time.time()) + 3600})
        import httpx
        orig = httpx.post
        httpx.post = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no refresh"))
        try:
            self.assertEqual(google_auth.access_token("gmail"), "good")
        finally:
            httpx.post = orig

    def test_refresh_token_survives_google_not_resending_it(self):
        """Google only sends the refresh token on first consent."""
        google_auth.store().set("gmail", {"access_token": "a", "refresh_token": "keepme",
                                          "expires_at": int(time.time()) - 10})
        import httpx
        orig = httpx.post
        httpx.post = lambda *a, **k: _Resp({"access_token": "new", "expires_in": 3600})
        try:
            google_auth.access_token("gmail")
        finally:
            httpx.post = orig
        self.assertEqual(google_auth.store().get("gmail")["refresh_token"], "keepme")

    def test_not_connected_raises(self):
        with self.assertRaises(google_auth.OAuthError):
            google_auth.access_token("google-calendar")

    def test_refresh_uses_client_id_stored_in_record(self):
        """A record written after this change carries its own client id."""
        import httpx
        google_auth.CLIENT_ID = ""
        google_auth.CLIENT_SECRET = ""
        os.environ.pop("GOOGLE_CLIENT_ID", None)
        os.environ.pop("GOOGLE_CLIENT_SECRET", None)
        google_auth.store().set("google-calendar", {
            "access_token": "old", "refresh_token": "rt",
            "expires_at": int(time.time()) - 10,
            "client_id": "stored-cid.apps.googleusercontent.com",
            "client_secret": "stored-secret"})
        seen = {}
        orig = httpx.post

        def fake_post(url, data=None, timeout=None):
            seen.update(data)
            return _Resp({"access_token": "fresh", "expires_in": 3600})

        httpx.post = fake_post
        try:
            self.assertEqual(google_auth.access_token("google-calendar"), "fresh")
        finally:
            httpx.post = orig
            google_auth.CLIENT_ID = "cid.apps.googleusercontent.com"
            google_auth.CLIENT_SECRET = "shipped-secret"
            os.environ["GOOGLE_CLIENT_ID"] = "cid.apps.googleusercontent.com"
            os.environ["GOOGLE_CLIENT_SECRET"] = "shipped-secret"
        self.assertEqual(seen["client_id"], "stored-cid.apps.googleusercontent.com")
        self.assertEqual(seen["client_secret"], "stored-secret")

    def test_legacy_record_refresh_falls_back_to_configured_env(self):
        """Legacy record (no client id) + configured env refreshes, no reconnect."""
        import httpx
        google_auth.CLIENT_ID = ""
        google_auth.CLIENT_SECRET = ""
        os.environ["GOOGLE_CLIENT_ID"] = "env-cid.apps.googleusercontent.com"
        os.environ["GOOGLE_CLIENT_SECRET"] = "env-secret"
        google_auth.store().set("google-calendar", {
            "access_token": "expired", "refresh_token": "legacy-rt",
            "expires_at": int(time.time()) - 10})          # no client_id
        seen = {}
        orig = httpx.post

        def fake_post(url, data=None, timeout=None):
            seen.update(data)
            return _Resp({"access_token": "fresh", "expires_in": 3600})

        httpx.post = fake_post
        try:
            self.assertEqual(google_auth.access_token("google-calendar"), "fresh")
        finally:
            httpx.post = orig
            google_auth.CLIENT_ID = "cid.apps.googleusercontent.com"
            google_auth.CLIENT_SECRET = "shipped-secret"
            os.environ["GOOGLE_CLIENT_ID"] = "cid.apps.googleusercontent.com"
            os.environ["GOOGLE_CLIENT_SECRET"] = "shipped-secret"
        self.assertEqual(seen["client_id"], "env-cid.apps.googleusercontent.com")
        self.assertEqual(seen["client_secret"], "env-secret")

    def test_refresh_without_any_client_id_is_actionable(self):
        """No client id in the record, the config, or the env -> clear message,
        never a raw provider error and never a network call."""
        import httpx
        google_auth.CLIENT_ID = ""
        google_auth.CLIENT_SECRET = ""
        old_env = {k: os.environ.pop(k, None)
                   for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")}
        google_auth.store().set("google-calendar", {
            "access_token": "expired", "refresh_token": "legacy-rt",
            "expires_at": int(time.time()) - 10})          # no client_id
        orig = httpx.post
        httpx.post = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not call the token endpoint"))
        try:
            with self.assertRaises(google_auth.OAuthError) as cm:
                google_auth.access_token("google-calendar")
        finally:
            httpx.post = orig
            google_auth.CLIENT_ID = "cid.apps.googleusercontent.com"
            google_auth.CLIENT_SECRET = "shipped-secret"
            if old_env["GOOGLE_CLIENT_ID"] is not None:
                os.environ["GOOGLE_CLIENT_ID"] = old_env["GOOGLE_CLIENT_ID"]
            if old_env["GOOGLE_CLIENT_SECRET"] is not None:
                os.environ["GOOGLE_CLIENT_SECRET"] = old_env["GOOGLE_CLIENT_SECRET"]
        msg = str(cm.exception)
        self.assertIn("Reconnect", msg)
        self.assertNotIn("token endpoint", msg)

    def test_missing_client_credentials_is_explained(self):
        google_auth.CLIENT_ID = ""
        try:
            with self.assertRaises(google_auth.OAuthError) as cm:
                google_auth.connect("gmail", open_browser=False, timeout=1)
        finally:
            google_auth.CLIENT_ID = "cid.apps.googleusercontent.com"
        self.assertIn("GOOGLE_CLIENT_ID", str(cm.exception))

    def test_status_and_disconnect(self):
        google_auth.store().set("gmail", {"access_token": "a", "refresh_token": "r",
                                          "expires_at": int(time.time()) + 99})
        st = google_auth.status()
        self.assertTrue(st["gmail"]["connected"])
        self.assertFalse(st["google-calendar"]["connected"])
        self.assertTrue(google_auth.disconnect("gmail"))
        self.assertFalse(google_auth.connected("gmail"))

    def test_disconnect_writes_an_event_line_without_secrets(self):
        old = os.environ.get("JARVIS_DATA_DIR")
        os.environ["JARVIS_DATA_DIR"] = self.tmp
        google_auth.store().set("gmail", {"access_token": "a", "refresh_token": "r",
                                          "expires_at": int(time.time()) + 99})
        try:
            self.assertTrue(google_auth.disconnect("gmail"))
            log = Path(self.tmp) / "mcp-events.log"
            self.assertTrue(log.exists())
            text = log.read_text(encoding="utf-8")
        finally:
            if old is None:
                os.environ.pop("JARVIS_DATA_DIR", None)
            else:
                os.environ["JARVIS_DATA_DIR"] = old
        self.assertIn("disconnect gmail", text)
        self.assertNotIn("access_token", text)
        self.assertNotIn("refresh_token", text)


class FakeGoogle:
    """Stub for the Google REST calls the tools make."""

    def __init__(self, calendar=None, gmail=None):
        self.calendar = calendar or {"items": [
            {"start": {"dateTime": "2026-09-16T10:00:00+05:30"}, "summary": "Standup"}]}
        self.gmail = gmail or {"messages": [{"id": "m1"}]}
        self.calls: list[str] = []

    def install(self):
        import httpx
        # save BOTH - uninstall restored only `get`, so a patched `post` leaked
        # into every later test module and broke test_mcp_oauth's registration.
        self._orig = httpx.get
        self._orig_post = httpx.post
        holder = self

        class _R:
            def __init__(self, body):
                self.status_code, self._b, self.text = 200, body, ""

            def json(self):
                return self._b

        def get(url, headers=None, params=None, timeout=None):
            holder.calls.append(url)
            return _R(holder.calendar if "calendar/v3" in url else holder.gmail)

        class _P:
            def __init__(self, body):
                self.status_code, self._b, self.text = 200, body, ""

            def json(self):
                return self._b

        def post(url, headers=None, json=None, timeout=None):
            holder.calls.append(url)
            return _P({"id": "d1", "summary": json.get("summary", ""),
                       "start": json.get("start", {})})

        httpx.get, httpx.post = get, post
        return self

    def uninstall(self):
        import httpx
        httpx.get = self._orig
        httpx.post = self._orig_post


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jarvis-gtools-")
        google_auth.set_store(google_auth.FileStore(Path(self.tmp) / "tok.json"))
        self._old_data_dir = os.environ.get("JARVIS_DATA_DIR")
        os.environ["JARVIS_DATA_DIR"] = self.tmp
        for cid in ("gmail", "google-calendar"):
            google_auth.store().set(cid, {"access_token": "tok", "refresh_token": "rt",
                                          "expires_at": int(time.time()) + 3600})
        self.fake = FakeGoogle().install()

    def tearDown(self):
        self.fake.uninstall()
        google_auth.set_store(None)
        if self._old_data_dir is None:
            os.environ.pop("JARVIS_DATA_DIR", None)
        else:
            os.environ["JARVIS_DATA_DIR"] = self._old_data_dir

    def test_calendar_list_events(self):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "list_events", "arguments": {"max_results": 5}}}
        out = gmcp.handle(msg, "google-calendar")
        self.assertIn("Standup", out["result"]["content"][0]["text"])

    def test_gmail_search(self):
        msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
               "params": {"name": "search_threads", "arguments": {"query": "is:unread"}}}
        out = gmcp.handle(msg, "gmail")
        self.assertIn("m1", out["result"]["content"][0]["text"])

    def test_tools_list_is_per_connector(self):
        cal = gmcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "google-calendar")
        gmail = gmcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "gmail")
        self.assertEqual([t["name"] for t in cal["result"]["tools"]],
                         ["list_events", "create_event"])
        self.assertEqual([t["name"] for t in gmail["result"]["tools"]],
                         ["search_threads", "get_message", "create_draft"])

    def test_notifications_get_no_reply(self):
        self.assertIsNone(gmcp.handle({"jsonrpc": "2.0",
                                       "method": "notifications/initialized"}, "gmail"))

    def test_unknown_tool_is_an_error_not_a_crash(self):
        out = gmcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": "nope"}}, "gmail")
        self.assertIn("unknown tool", out["error"]["message"])

    def test_disconnected_connector_reports_clearly(self):
        google_auth.disconnect("gmail")
        out = gmcp.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                           "params": {"name": "search_threads", "arguments": {"query": "x"}}},
                          "gmail")
        self.assertIn("not connected", out["error"]["message"])

    def test_upstream_error_is_a_tool_error(self):
        import httpx

        class _Bad:
            status_code, text = 403, '{"error":{"message":"insufficient scope"}}'

            def json(self):
                return {"error": {"message": "insufficient scope"}}

        orig = httpx.get
        httpx.get = lambda *a, **k: _Bad()
        try:
            out = gmcp.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                               "params": {"name": "list_events", "arguments": {}}},
                              "google-calendar")
        finally:
            httpx.get = orig
        self.assertIn("insufficient scope", out["error"]["message"])


class StdioRoundTripTests(unittest.TestCase):
    """The real thing: spawn the server and speak MCP to it like the app does."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jarvis-roundtrip-")
        self._env = {k: os.environ.get(k) for k in ("JARVIS_DATA_DIR", "JARVIS_TOKEN_STORE")}
        os.environ["JARVIS_DATA_DIR"] = self.tmp
        os.environ["JARVIS_TOKEN_STORE"] = "file"     # never touch the real Keychain
        (Path(self.tmp) / "google_tokens.json").write_text(json.dumps({
            "google-calendar": {"access_token": "tok", "refresh_token": "rt",
                                "expires_at": int(time.time()) + 3600}}), encoding="utf-8")

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_manager_lists_tools_from_the_connector(self):
        import mcp_client

        cfg = Path(self.tmp) / "mcp.json"
        cfg.write_text(json.dumps({"servers": [{
            "name": "google-calendar", "command": sys.executable,
            "args": [os.path.abspath(gmcp.__file__), "google-calendar"]}]}), encoding="utf-8")
        mgr = mcp_client.MCPManager(cfg)
        try:
            tools = mgr.list_tools("google-calendar")
        finally:
            mgr.close()
        names = [t.get("name") for t in tools]
        self.assertIn("list_events", names)


class RegistrationPersistTests(unittest.TestCase):
    """register() must write where the app's manager actually reads."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jarvis-register-")
        self._old = os.environ.get("JARVIS_DATA_DIR")
        os.environ["JARVIS_DATA_DIR"] = self.tmp

    def tearDown(self):
        if self._old is None:
            os.environ.pop("JARVIS_DATA_DIR", None)
        else:
            os.environ["JARVIS_DATA_DIR"] = self._old

    def test_register_persists_where_manager_reads(self):
        import mcp_client

        gmcp.register("gmail")
        self.assertTrue((Path(self.tmp) / "mcp.json").exists())
        self.assertIn("gmail", mcp_client.MCPManager().configured())
        self.assertEqual(Path(mcp_client.default_config_path()),
                         Path(self.tmp) / "mcp.json")

        gmcp.unregister("gmail")
        self.assertNotIn("gmail", mcp_client.MCPManager().configured())


if __name__ == "__main__":
    unittest.main(verbosity=2)
