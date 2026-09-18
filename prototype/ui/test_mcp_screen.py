"""Runtime tests for the MCP screen backend (mcp_screen.handle).

Drives the real handler with a real MCPManager and a fake websocket, so the
UI<->server contract is verified without booting the voice pipeline.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import google_auth  # noqa: E402
import mcp_client  # noqa: E402
import mcp_screen  # noqa: E402


class _Out:
    """Collects sent payloads (stands in for the websocket)."""

    def __init__(self):
        self.msgs: list[dict] = []

    async def __call__(self, payload: dict):
        json.dumps(payload)          # must be JSON-serializable
        self.msgs.append(payload)

    def last(self, type_: str) -> dict:
        for m in reversed(self.msgs):
            if m.get("type") == type_:
                return m
        return {}


class ScreenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asha-screen-")
        self.mgr = mcp_client.MCPManager(Path(self.tmp) / "mcp.json")
        self.out = _Out()
        # Root-cause fix: isolate the token store so no screen test can ever
        # touch the real macOS Keychain (a disconnect used to delete it).
        self._prev_store = google_auth._STORE
        google_auth.set_store(google_auth.FileStore(Path(self.tmp) / "gkeys.json"))
        self._prev_data_dir = os.environ.get("JARVIS_DATA_DIR")
        os.environ["JARVIS_DATA_DIR"] = self.tmp

    def tearDown(self):
        google_auth.set_store(self._prev_store)
        if self._prev_data_dir is None:
            os.environ.pop("JARVIS_DATA_DIR", None)
        else:
            os.environ["JARVIS_DATA_DIR"] = self._prev_data_dir

    def run_(self, msg: dict, project_dir: str = "/tmp/proj"):
        asyncio.run(mcp_screen.handle(msg, self.out, self.mgr, project_dir=project_dir))
        return self.out

    def test_get_lists_servers_and_featured(self):
        out = self.run_({"action": "get"})
        m = out.last("mcp_list")
        self.assertEqual(m["servers"], [])
        names = [f["name"] for f in m["featured"]]
        self.assertIn("deepwiki", names)
        self.assertIn("filesystem", names)
        fs = [f for f in m["featured"] if f["name"] == "filesystem"][0]
        self.assertIn("/tmp/proj", fs["args"])          # project resolved

    def test_add_then_get_round_trip(self):
        self.run_({"action": "add", "name": "remote1", "url": "https://x/mcp"})
        # add/remove reply with the refreshed list (so the screen updates)
        self.assertEqual([s["name"] for s in self.out.last("mcp_list")["servers"]],
                         ["remote1"])
        out = self.run_({"action": "get"})
        self.assertEqual([s["name"] for s in out.last("mcp_list")["servers"]], ["remote1"])

    def test_add_accepts_string_args(self):
        self.run_({"action": "add", "name": "loc", "command": "npx",
                   "args": "-y @me/pkg"})
        s = self.run_({"action": "get"}).last("mcp_list")["servers"][0]
        self.assertEqual(s["args"], ["-y", "@me/pkg"])

    def test_add_with_env_lines(self):
        self.run_({"action": "add", "name": "gworkspace", "command": "npx",
                   "args": ["-y", "@aaronsb/google-workspace-mcp"],
                   "env": "GOOGLE_CLIENT_ID=abc123\n\n# comment\nGOOGLE_CLIENT_SECRET=s3cr3t\nBADLINE"})
        got = self.out.last("mcp_list")["servers"][0]
        self.assertEqual(got["env_keys"], ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"])
        self.assertNotIn("env", got)                       # values not returned
        self.assertNotIn("s3cr3t", json.dumps(got))        # nor leaked in the payload
        saved = json.loads((Path(self.tmp) / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["servers"][0]["env"]["GOOGLE_CLIENT_SECRET"], "s3cr3t")

    def test_add_with_env_dict(self):
        self.run_({"action": "add", "name": "d", "command": "npx",
                   "env": {"A": "1", "B": "2"}})
        self.assertEqual(self.out.last("mcp_list")["servers"][0]["env_keys"], ["A", "B"])

    def test_add_without_env_is_clean(self):
        self.run_({"action": "add", "name": "plain", "command": "npx"})
        self.assertEqual(self.out.last("mcp_list")["servers"][0]["env_keys"], [])

    def test_parse_env_helper(self):
        self.assertIsNone(mcp_screen._parse_env(""))
        self.assertIsNone(mcp_screen._parse_env("no equals here"))
        self.assertEqual(mcp_screen._parse_env({"k": "v"}), {"k": "v"})
        self.assertEqual(mcp_screen._parse_env("A=1\nB=two words "),
                         {"A": "1", "B": "two words"})

    def test_remove(self):
        self.run_({"action": "add", "name": "gone", "url": "https://x/mcp"})
        self.run_({"action": "remove", "name": "gone"})
        self.assertEqual(self.out.last("mcp_list")["servers"], [])

    def test_bad_add_reports_error_not_crash(self):
        self.run_({"action": "add", "name": "oops"})       # no command/url
        self.assertIn("required", self.out.last("mcp_error")["error"])

    def test_duplicate_add_reports_error(self):
        self.run_({"action": "add", "name": "dup", "url": "https://x/mcp"})
        self.run_({"action": "add", "name": "dup", "url": "https://x/mcp"})
        self.assertIn("already exists", self.out.last("mcp_error")["error"])

    def test_featured_entry_is_addable(self):
        feat = self.run_({"action": "get"}).last("mcp_list")["featured"]
        fs = [f for f in feat if f["name"] == "filesystem"][0]
        self.run_({"action": "add", "name": fs["name"], "command": fs["command"],
                   "args": fs["args"]})
        got = self.run_({"action": "get"}).last("mcp_list")["servers"]
        self.assertEqual(got[0]["command"], "npx")
        self.assertIn("/tmp/proj", got[0]["args"])

    def test_search_returns_registry_message(self):
        import httpx

        class _R:
            def raise_for_status(self): pass
            def json(self):
                return {"servers": [{"server": {"name": "io.x/a", "title": "A",
                                                "description": "d",
                                                "remotes": [{"url": "https://a/mcp"}]}}]}

        orig = httpx.get
        httpx.get = lambda *a, **k: _R()
        try:
            self.run_({"action": "search", "query": "a"})
        finally:
            httpx.get = orig
        m = self.out.last("mcp_registry")
        self.assertEqual(m["query"], "a")
        self.assertEqual(m["results"][0]["url"], "https://a/mcp")

    def test_search_failure_reports_error(self):
        import httpx

        def boom(*a, **k): raise RuntimeError("offline")
        orig = httpx.get
        httpx.get = boom
        try:
            self.run_({"action": "search", "query": "x"})
        finally:
            httpx.get = orig
        self.assertIn("registry request failed", self.out.last("mcp_error")["error"])

    def test_apps_lists_google_connectors(self):
        self.run_({"action": "apps"})
        apps = {a["id"]: a for a in self.out.last("mcp_apps")["apps"]}
        self.assertIn("gmail", apps)
        self.assertIn("google-calendar", apps)
        self.assertFalse(apps["gmail"]["connected"])
        self.assertIn("Gmail", apps["gmail"]["label"])

    def _seed_gmail(self, connected: bool, registered: bool) -> None:
        """Put the temp store + manager into a given (connected, registered) cell."""
        if connected:
            google_auth.store().set("gmail", {"access_token": "a", "refresh_token": "r",
                                              "expires_at": 10 ** 12})
        else:
            google_auth.store().delete("gmail")
        if registered:
            if "gmail" not in self.mgr.configured():
                self.mgr.add_server("gmail", "/usr/bin/python", "",
                                    ["google_mcp_server.py", "gmail"])
        elif "gmail" in self.mgr.configured():
            self.mgr.remove_server("gmail")

    def test_apps_reports_the_four_states(self):
        for connected, registered, expected in [
            (True, True, "ready"),
            (True, False, "needs_setup"),
            (False, True, "needs_signin"),
            (False, False, "available"),
        ]:
            with self.subTest(connected=connected, registered=registered):
                self._seed_gmail(connected, registered)
                self.run_({"action": "apps"})
                apps = {a["id"]: a for a in self.out.last("mcp_apps")["apps"]}
                self.assertEqual(apps["gmail"]["state"], expected)

    def test_disconnect_without_confirmation_changes_nothing(self):
        self._seed_gmail(True, True)
        calls = []
        orig = (mcp_screen.google_mcp_server.unregister,
                mcp_screen.google_auth.disconnect)
        mcp_screen.google_mcp_server.unregister = lambda cid: calls.append(("unregister", cid))
        mcp_screen.google_auth.disconnect = lambda cid: calls.append(("disconnect", cid))
        try:
            self.run_({"action": "disconnect", "connector": "gmail"})   # no confirm
        finally:
            (mcp_screen.google_mcp_server.unregister,
             mcp_screen.google_auth.disconnect) = orig
        self.assertEqual(calls, [])
        self.assertTrue(google_auth.connected("gmail"))
        self.assertIn("gmail", self.mgr.configured())
        self.assertIn("confirmation", self.out.last("mcp_error")["error"].lower())

    def test_disconnect_with_confirmation_unregisters_then_deletes(self):
        self._seed_gmail(True, True)
        order = []
        orig = (mcp_screen.google_mcp_server.unregister,
                mcp_screen.google_auth.disconnect)

        def fake_unregister(cid):
            order.append("unregister")
            return self.mgr.remove_server(cid)

        def fake_disconnect(cid):
            order.append("disconnect")
            return google_auth.store().delete(cid)

        mcp_screen.google_mcp_server.unregister = fake_unregister
        mcp_screen.google_auth.disconnect = fake_disconnect
        try:
            self.run_({"action": "disconnect", "connector": "gmail", "confirm": True})
        finally:
            (mcp_screen.google_mcp_server.unregister,
             mcp_screen.google_auth.disconnect) = orig
        self.assertEqual(order, ["unregister", "disconnect"])
        self.assertFalse(google_auth.connected("gmail"))
        self.assertNotIn("gmail", self.mgr.configured())

    def test_repair_paths_never_delete_the_token(self):
        self._seed_gmail(True, True)
        orig = (mcp_screen.google_auth.disconnect,
                mcp_screen.google_mcp_server.register)

        def must_not_disconnect(cid):
            raise AssertionError("disconnect must never run on connect/register")

        def fake_register(cid):
            if cid in self.mgr.configured():
                self.mgr.remove_server(cid)
            self.mgr.add_server(cid, "/usr/bin/python", "",
                                ["google_mcp_server.py", cid])
            return {"name": cid}

        mcp_screen.google_auth.disconnect = must_not_disconnect
        mcp_screen.google_mcp_server.register = fake_register
        try:
            self.run_({"action": "connect", "connector": "gmail"})
            self.run_({"action": "register", "connector": "gmail"})
        finally:
            (mcp_screen.google_auth.disconnect,
             mcp_screen.google_mcp_server.register) = orig
        self.assertTrue(google_auth.connected("gmail"))

    def test_app_action_mapping_matches_states(self):
        """app.js's _mcpAppAction must map each state cell to the right action."""
        if not shutil.which("node"):
            self.skipTest("node not available")
        app = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
        start = app.index("function _mcpAppAction")
        i = app.index("{", start)
        depth, end = 0, None
        for j in range(i, len(app)):
            if app[j] == "{":
                depth += 1
            elif app[j] == "}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        self.assertIsNotNone(end, "could not brace-match _mcpAppAction")
        cases = [
            ({"connected": True, "registered": True}, "Disconnect", "disconnect", True, "Connected"),
            ({"connected": True, "registered": False}, "Finish setup", "register", False, "setup"),
            ({"connected": False, "registered": True}, "Sign in", "connect", False, "sign"),
            ({"connected": False, "registered": False}, "Add", "connect", False, "Not connected"),
        ]
        script = (app[start:end] + "\nconst cases = " +
                  json.dumps([c for c, *_ in cases]) +
                  ";\nconsole.log(JSON.stringify(cases.map(_mcpAppAction)));\n")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(script)
            path = fh.name
        try:
            proc = subprocess.run(["node", path], capture_output=True, text=True,
                                  timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = json.loads(proc.stdout.strip())
        for row, (case, label, action, confirm, status_substr) in zip(got, cases):
            with self.subTest(case=case):
                self.assertEqual(row["label"], label)
                self.assertEqual(row["action"], action)
                self.assertEqual(bool(row.get("confirm")), confirm)
                self.assertIn(status_substr, row["status"])
        # only the ready state is destructive
        self.assertTrue(got[0]["danger"])
        self.assertTrue(got[0]["confirm"])
        for row in got[1:]:
            self.assertFalse(row["danger"])
            self.assertFalse(row["confirm"])

    def _extract_fn(self, app: str, name: str) -> str:
        """Return the source of `function <name>...` by brace matching."""
        start = app.index("function " + name)
        i = app.index("{", start)
        depth, end = 0, None
        for j in range(i, len(app)):
            if app[j] == "{":
                depth += 1
            elif app[j] == "}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        self.assertIsNotNone(end, "could not brace-match " + name)
        return app[start:end]

    def test_registered_connector_renders_once(self):
        """A connector mapped to a curated app must not render a second time."""
        if not shutil.which("node"):
            self.skipTest("node not available")
        app = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
        fns = "\n".join(self._extract_fn(app, n) for n in
                        ("_mcpSlug", "_mcpAppFor", "_mcpOwnedServers", "_mcpOwnedCatalog"))
        script = fns + """
const apps = [{id:'gmail',label:'Gmail'},{id:'google-calendar',label:'Google Calendar'}];
const servers = [{name:'gmail'},{name:'google-calendar'},{name:'filesystem'}];
const catalog = [{id:'io.x/gmail',name:'Gmail'},{id:'io.x/gcal',name:'Google Calendar'},{id:'io.x/slack',name:'Slack'},{id:'io.x/other',name:'Other'}];
console.log(JSON.stringify({
  servers: _mcpOwnedServers(servers, apps).map(s => s.name),
  catalog: _mcpOwnedCatalog(catalog, apps).map(r => r.name),
}));
"""
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(script)
            path = fh.name
        try:
            proc = subprocess.run(["node", path], capture_output=True, text=True,
                                  timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = json.loads(proc.stdout.strip())
        self.assertEqual(got["servers"], ["filesystem"])
        self.assertEqual(got["catalog"], ["Slack", "Other"])

    def test_register_action_failure_surfaces(self):
        """A failed repair must surface an error and never delete the token."""
        self._seed_gmail(True, False)
        orig = mcp_screen.google_mcp_server.register

        def boom(cid):
            raise RuntimeError("boom")

        mcp_screen.google_mcp_server.register = boom
        try:
            self.run_({"action": "register", "connector": "gmail"})
        finally:
            mcp_screen.google_mcp_server.register = orig
        self.assertIn("register", self.out.last("mcp_error")["error"].lower())
        self.assertTrue(google_auth.connected("gmail"))

    def test_failed_state_change_is_logged_without_secrets(self):
        """The top-level error path records action + subject, never secrets."""
        orig = mcp_screen.google_auth.connect

        def boom(cid, **k):
            raise google_auth.OAuthError("nope")

        mcp_screen.google_auth.connect = boom
        try:
            self.run_({"action": "connect", "connector": "gmail"})
        finally:
            mcp_screen.google_auth.connect = orig
        log = (Path(self.tmp) / "mcp-events.log").read_text(encoding="utf-8")
        self.assertIn("connect gmail error", log)
        self.assertNotIn("token", log.lower())
        self.assertNotIn("secret", log.lower())

    def test_connect_signs_in_then_registers_the_mcp_server(self):
        """One click: OAuth runs, then the connector shows up as a server."""
        calls = []
        orig = (mcp_screen.google_auth.connect, mcp_screen.google_auth.connected,
                mcp_screen.google_mcp_server.register,
                mcp_screen.google_mcp_server.unregister,
                mcp_screen.google_auth.disconnect)
        signed_in = set()

        def fake_disconnect(cid):
            # Never call the real one: it must not run against a real store.
            calls.append(("disconnect", cid))
            signed_in.discard(cid)
            return True

        mcp_screen.google_auth.connect = lambda cid, **k: (
            calls.append(("connect", cid)), signed_in.add(cid))
        mcp_screen.google_auth.connected = lambda cid: cid in signed_in
        mcp_screen.google_mcp_server.register = lambda cid: (
            calls.append(("register", cid)),
            self.mgr.add_server(cid, "/usr/bin/python", "",
                                ["google_mcp_server.py", cid]))[0]
        mcp_screen.google_mcp_server.unregister = lambda cid: (
            calls.append(("unregister", cid)), self.mgr.remove_server(cid))[1]
        mcp_screen.google_auth.disconnect = fake_disconnect
        try:
            self.run_({"action": "connect", "connector": "gmail"})
            apps = {a["id"]: a for a in self.out.last("mcp_apps")["apps"]}
            self.assertTrue(apps["gmail"]["connected"])
            self.assertTrue(apps["gmail"]["registered"])
            self.assertIn("gmail", [s["name"] for s in self.out.last("mcp_list")["servers"]])
            self.assertEqual(calls, [("connect", "gmail"), ("register", "gmail")])

            self.run_({"action": "disconnect", "connector": "gmail", "confirm": True})
            self.assertEqual(self.out.last("mcp_list")["servers"], [])
            self.assertIn(("unregister", "gmail"), calls)
            self.assertEqual(calls[-1], ("disconnect", "gmail"))
        finally:
            (mcp_screen.google_auth.connect, mcp_screen.google_auth.connected,
             mcp_screen.google_mcp_server.register,
             mcp_screen.google_mcp_server.unregister,
             mcp_screen.google_auth.disconnect) = orig

    def test_connect_failure_surfaces_an_error(self):
        orig = mcp_screen.google_auth.connect

        def boom(cid, **k):
            raise mcp_screen.google_auth.OAuthError("Google OAuth client not configured")

        mcp_screen.google_auth.connect = boom
        try:
            self.run_({"action": "connect", "connector": "gmail"})
        finally:
            mcp_screen.google_auth.connect = orig
        self.assertIn("not configured", self.out.last("mcp_error")["error"])

    def test_connect_registration_failure_surfaces(self):
        orig = (mcp_screen.google_auth.connect, mcp_screen.google_auth.connected,
                mcp_screen.google_mcp_server.register)
        mcp_screen.google_auth.connect = lambda cid, **k: None
        mcp_screen.google_auth.connected = lambda cid: True

        def boom(cid):
            raise RuntimeError("boom")

        mcp_screen.google_mcp_server.register = boom
        try:
            self.run_({"action": "connect", "connector": "gmail"})
        finally:
            (mcp_screen.google_auth.connect, mcp_screen.google_auth.connected,
             mcp_screen.google_mcp_server.register) = orig
        self.assertIn("register", self.out.last("mcp_error")["error"].lower())
        apps = {a["id"]: a for a in self.out.last("mcp_apps")["apps"]}
        self.assertTrue(apps["gmail"]["connected"])
        self.assertFalse(apps["gmail"]["registered"])

    def test_connect_reloads_manager_after_register(self):
        orig = (mcp_screen.google_auth.connect, mcp_screen.google_auth.connected,
                mcp_screen.google_mcp_server.register)
        mcp_screen.google_auth.connect = lambda cid, **k: None
        mcp_screen.google_auth.connected = lambda cid: True

        def fake_register(cid):
            (Path(self.tmp) / "mcp.json").write_text(json.dumps({
                "servers": [{"name": "gmail", "command": "/usr/bin/python",
                             "args": ["x"]}]}), encoding="utf-8")

        mcp_screen.google_mcp_server.register = fake_register
        try:
            self.run_({"action": "connect", "connector": "gmail"})
        finally:
            (mcp_screen.google_auth.connect, mcp_screen.google_auth.connected,
             mcp_screen.google_mcp_server.register) = orig
        self.assertIn("gmail", self.mgr.configured())
        apps = {a["id"]: a for a in self.out.last("mcp_apps")["apps"]}
        self.assertTrue(apps["gmail"]["registered"])

    def test_connect_already_signed_in_skips_oauth_and_registers(self):
        calls = []
        orig = (mcp_screen.google_auth.connect, mcp_screen.google_auth.connected,
                mcp_screen.google_mcp_server.register)
        mcp_screen.google_auth.connected = lambda cid: True

        def must_not_connect(cid, **k):
            raise AssertionError("OAuth must not run when already signed in")

        def fake_register(cid):
            calls.append(cid)
            self.mgr.add_server(cid, "/usr/bin/python", "",
                                ["google_mcp_server.py", cid])

        mcp_screen.google_auth.connect = must_not_connect
        mcp_screen.google_mcp_server.register = fake_register
        try:
            self.run_({"action": "connect", "connector": "gmail"})
        finally:
            (mcp_screen.google_auth.connect, mcp_screen.google_auth.connected,
             mcp_screen.google_mcp_server.register) = orig
        self.assertEqual(calls, ["gmail"])
        self.assertIn("gmail", self.mgr.configured())
        apps = {a["id"]: a for a in self.out.last("mcp_apps")["apps"]}
        self.assertTrue(apps["gmail"]["registered"])
        self.assertIn("gmail", [s["name"] for s in self.out.last("mcp_list")["servers"]])

    def test_signin_marks_oauth_and_signout_clears_it(self):
        self.run_({"action": "add", "name": "slack", "url": "https://mcp.slack.com/"})
        calls = []
        orig = (mcp_screen.mcp_oauth.connect, mcp_screen.mcp_oauth.disconnect,
                mcp_screen.mcp_oauth.connected)
        signed = set()
        mcp_screen.mcp_oauth.connect = lambda name, url, **k: (
            calls.append(("connect", name, url)), signed.add(name))
        mcp_screen.mcp_oauth.disconnect = lambda name: (
            calls.append(("disconnect", name)), signed.discard(name))[1]
        mcp_screen.mcp_oauth.connected = lambda name: name in signed
        try:
            self.run_({"action": "signin", "name": "slack"})
            got = self.out.last("mcp_list")["servers"][0]
            self.assertTrue(got["oauth"])
            self.assertTrue(got["signed_in"])
            self.assertEqual(calls[0], ("connect", "slack", "https://mcp.slack.com/"))

            self.run_({"action": "signout", "name": "slack"})
            got = self.out.last("mcp_list")["servers"][0]
            self.assertFalse(got["signed_in"])
            self.assertFalse(got["oauth"])
        finally:
            (mcp_screen.mcp_oauth.connect, mcp_screen.mcp_oauth.disconnect,
             mcp_screen.mcp_oauth.connected) = orig

    def test_signin_without_url_is_an_error(self):
        self.run_({"action": "add", "name": "local1", "command": "npx"})
        self.run_({"action": "signin", "name": "local1"})
        self.assertIn("no URL", self.out.last("mcp_error")["error"])

    def test_signin_failure_surfaces(self):
        self.run_({"action": "add", "name": "slack", "url": "https://mcp.slack.com/"})
        orig = mcp_screen.mcp_oauth.connect

        def boom(name, url, **k):
            raise mcp_screen.mcp_oauth.OAuthError("no OAuth metadata")

        mcp_screen.mcp_oauth.connect = boom
        try:
            self.run_({"action": "signin", "name": "slack"})
        finally:
            mcp_screen.mcp_oauth.connect = orig
        self.assertIn("OAuth metadata", self.out.last("mcp_error")["error"])

    def test_add_signin_is_one_click(self):
        """The directory '+': wire the server AND run OAuth, in that order."""
        calls = []
        orig = (mcp_screen.mcp_oauth.connect, mcp_screen.mcp_oauth.connected,
                mcp_screen.mcp_oauth.disconnect)
        mcp_screen.mcp_oauth.connect = lambda n, u, **k: calls.append(("oauth", n, u))
        mcp_screen.mcp_oauth.connected = lambda n: False
        try:
            self.run_({"action": "add_signin", "name": "figma",
                       "url": "https://mcp.figma.com/mcp"})
            got = self.out.last("mcp_list")["servers"][0]
            self.assertEqual(got["name"], "figma")
            self.assertEqual(got["url"], "https://mcp.figma.com/mcp")
            self.assertTrue(got["oauth"])
            self.assertEqual(calls, [("oauth", "figma", "https://mcp.figma.com/mcp")])
            # pressing it again must not duplicate the server
            self.run_({"action": "add_signin", "name": "figma",
                       "url": "https://mcp.figma.com/mcp"})
            self.assertEqual(len(self.out.last("mcp_list")["servers"]), 1)
        finally:
            (mcp_screen.mcp_oauth.connect, mcp_screen.mcp_oauth.connected,
             mcp_screen.mcp_oauth.disconnect) = orig

    def test_add_signin_needs_a_url(self):
        self.run_({"action": "add_signin", "name": "x"})
        self.assertIn("name and url", self.out.last("mcp_error")["error"])

    def test_every_ui_message_type_is_handled(self):
        """The four outbound types the UI switches on are the four we send."""
        import re
        app = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
        handled = set(re.findall(r"m\.type === '(mcp_[a-z]+)'", app))
        self.assertTrue({"mcp_list", "mcp_tools", "mcp_registry", "mcp_error", "mcp_apps"}
                        .issubset(handled), handled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
