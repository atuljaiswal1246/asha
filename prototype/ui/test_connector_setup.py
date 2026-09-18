"""Tests for guided connector setup (connector_setup.py) + its MCP wiring.

No live network: every HTTP path uses httpx.MockTransport. The store always
points at a temp dir, and the PAT env slots are cleared/restored so a real
token in the environment can never affect (or be affected by) these tests.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402

import connector_setup  # noqa: E402
import figma_rest  # noqa: E402
import google_auth  # noqa: E402
import mcp_client  # noqa: E402
import mcp_screen  # noqa: E402


SECRET = "figd_super-secret-token-123"
PAT_ENV = figma_rest.PAT_ENV_VARS[-1]      # JARVIS_FIGMA_PAT


class _Out:
    """Collects sent payloads (stands in for the websocket)."""

    def __init__(self):
        self.msgs: list[dict] = []

    async def __call__(self, payload: dict):
        json.dumps(payload)                 # must be JSON-serializable
        self.msgs.append(payload)

    def last(self, type_: str) -> dict:
        for m in reversed(self.msgs):
            if m.get("type") == type_:
                return m
        return {}


class _PatEnvMixin:
    """Drop any real PAT from the process env for the duration of a test."""

    def _pop_pat_env(self):
        self._saved_pat = {name: os.environ.pop(name, None)
                           for name in figma_rest.PAT_ENV_VARS}

    def _restore_pat_env(self):
        for name, value in self._saved_pat.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class KeyStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jarvis-connsetup-")
        self.store = connector_setup.KeyStore(Path(self.tmp) / "connector_keys.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_round_trip_and_delete(self):
        self.store.set("figma", {"secret": SECRET, "unverified": False})
        self.assertEqual(self.store.get("figma")["secret"], SECRET)
        self.assertEqual(self.store.all(), ["figma"])
        self.assertTrue(self.store.delete("figma"))
        self.assertIsNone(self.store.get("figma"))
        self.assertFalse(self.store.delete("figma"))

    def test_file_is_0600(self):
        self.store.set("figma", {"secret": SECRET})
        mode = stat.S_IMODE(os.stat(self.store.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_missing_or_corrupt_file_reads_empty(self):
        self.assertEqual(self.store.all(), [])
        self.store.path.write_text("{ not json", encoding="utf-8")
        self.assertIsNone(self.store.get("figma"))


class GuideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jarvis-connsetup-")
        self.store = connector_setup.KeyStore(Path(self.tmp) / "connector_keys.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_figma_guide_has_steps_link_and_field(self):
        g = connector_setup.guide("figma", store_=self.store)
        self.assertEqual(g["id"], "figma")
        self.assertEqual(g["label"], "Figma")
        self.assertTrue(g["summary"])
        self.assertTrue(g["field_label"])
        self.assertTrue(g["field_placeholder"])
        self.assertGreaterEqual(len(g["steps"]), 3)
        self.assertTrue(all(isinstance(s, dict) and s.get("text")
                            for s in g["steps"]))
        urls = [s.get("url") for s in g["steps"] if s.get("url")]
        self.assertTrue(any("figma.com/settings" in u for u in urls), urls)
        self.assertIn("developers.figma.com", g["docs_url"])

    def test_unknown_connector_raises(self):
        with self.assertRaises(connector_setup.ConnectorSetupError):
            connector_setup.guide("nope", store_=self.store)

    def test_connected_reflects_the_store(self):
        self.assertFalse(connector_setup.guide("figma", store_=self.store)["connected"])
        self.store.set("figma", {"secret": SECRET, "unverified": True})
        g = connector_setup.guide("figma", store_=self.store)
        self.assertTrue(g["connected"])
        self.assertTrue(g["unverified"])

    def test_guide_never_contains_the_saved_secret(self):
        self.store.set("figma", {"secret": SECRET})
        dumped = json.dumps(connector_setup.guide("figma", store_=self.store))
        self.assertNotIn(SECRET, dumped)


class ValidationTests(unittest.TestCase):
    def _validate(self, handler, secret=SECRET):
        return connector_setup.validate(
            "figma", secret, transport=httpx.MockTransport(handler))

    def test_success_names_account_and_sends_pat_header(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["token"] = request.headers.get("X-Figma-Token")
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={
                "id": "1", "email": "neha@example.com", "handle": "neha"})

        res = self._validate(handler)
        self.assertTrue(res["ok"])
        self.assertEqual(res["reason"], "ok")
        self.assertEqual(res["account"], "neha@example.com")
        self.assertIn("neha@example.com", res["message"])
        self.assertEqual(seen["path"], "/v1/me")
        self.assertEqual(seen["token"], SECRET)
        self.assertIsNone(seen["auth"])
        self.assertNotIn(SECRET, json.dumps(res))

    def test_success_falls_back_to_handle(self):
        res = self._validate(
            lambda r: httpx.Response(200, json={"id": "1", "handle": "neha"}))
        self.assertTrue(res["ok"])
        self.assertIn("neha", res["message"])

    def test_401_is_auth_failure(self):
        res = self._validate(lambda r: httpx.Response(401, json={"err": "nope"}))
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "auth")
        self.assertIn("invalid or expired", res["message"])
        self.assertNotIn(SECRET, res["message"])

    def test_403_is_auth_failure(self):
        res = self._validate(lambda r: httpx.Response(403, json={"message": "no"}))
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "auth")

    def test_429_is_rate_limit(self):
        def handler(request):
            return httpx.Response(429, headers={"Retry-After": "60"},
                                  json={"message": "slow down"})
        res = self._validate(handler)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "rate_limit")
        self.assertIn("60", res["message"])

    def test_network_failure_is_flagged(self):
        def handler(request):
            raise httpx.ConnectError("connection refused", request=request)
        res = self._validate(handler)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "network")
        self.assertTrue(res["network"])
        self.assertIn("reach Figma", res["message"])

    def test_other_http_error_is_generic(self):
        res = self._validate(lambda r: httpx.Response(500, json={"err": "boom"}))
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "error")

    def test_empty_token_is_rejected_without_a_request(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={})

        res = connector_setup.validate("figma", "   ",
                                       transport=httpx.MockTransport(handler))
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "empty")
        self.assertEqual(calls, [])


class SecretHygieneTests(_PatEnvMixin, unittest.TestCase):
    def setUp(self):
        self._pop_pat_env()
        self.tmp = tempfile.mkdtemp(prefix="jarvis-connsetup-")
        self.store = connector_setup.KeyStore(Path(self.tmp) / "connector_keys.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self._restore_pat_env()

    def test_secret_never_appears_in_messages_or_logs(self):
        def handler(request):
            # A hostile provider echoes the token back; the guide must still
            # never surface it.
            return httpx.Response(401, json={"err": SECRET})

        with self.assertLogs(level="INFO") as logs:
            res = connector_setup.validate(
                "figma", SECRET, transport=httpx.MockTransport(handler))
            connector_setup.save("figma", SECRET, store_=self.store)
        logged = "\n".join(logs.output)
        self.assertNotIn(SECRET, json.dumps(res))
        self.assertNotIn(SECRET, logged)
        self.assertNotIn(SECRET, res["message"])

    def test_scrub_removes_pat_shaped_text(self):
        self.assertNotIn("figd_abc", connector_setup._scrub("x figd_abc y"))


class BrainPathTests(_PatEnvMixin, unittest.TestCase):
    """A saved token must reach figma_rest without any repo file."""

    def setUp(self):
        self._pop_pat_env()
        self.tmp = tempfile.mkdtemp(prefix="jarvis-connsetup-")
        self.store = connector_setup.KeyStore(Path(self.tmp) / "connector_keys.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self._restore_pat_env()

    def test_saved_key_resolves_as_a_pat_for_the_tools(self):
        connector_setup.save("figma", SECRET, store_=self.store)
        self.assertEqual(figma_rest.FigmaClient()._resolve_auth(), ("pat", SECRET))

    def test_install_saved_keys_loads_env(self):
        self.store.set("figma", {"secret": SECRET})
        self.assertIsNone(os.environ.get(PAT_ENV))
        self.assertEqual(connector_setup.install_saved_keys(store_=self.store), 1)
        self.assertEqual(os.environ.get(PAT_ENV), SECRET)

    def test_delete_clears_env(self):
        connector_setup.save("figma", SECRET, store_=self.store)
        self.assertEqual(os.environ.get(PAT_ENV), SECRET)
        self.assertTrue(connector_setup.delete("figma", store_=self.store))
        self.assertIsNone(os.environ.get(PAT_ENV))


class ScreenWiringTests(_PatEnvMixin, unittest.TestCase):
    """The MCP screen actions drive the guide and flip the reported state."""

    def setUp(self):
        self._pop_pat_env()
        self.tmp = tempfile.mkdtemp(prefix="jarvis-connsetup-")
        self._prev_data_dir = os.environ.get("JARVIS_DATA_DIR")
        os.environ["JARVIS_DATA_DIR"] = self.tmp
        self._prev_store = google_auth._STORE
        google_auth.set_store(google_auth.FileStore(Path(self.tmp) / "gkeys.json"))
        self.mgr = mcp_client.MCPManager(Path(self.tmp) / "mcp.json")
        self.out = _Out()

    def tearDown(self):
        google_auth.set_store(self._prev_store)
        if self._prev_data_dir is None:
            os.environ.pop("JARVIS_DATA_DIR", None)
        else:
            os.environ["JARVIS_DATA_DIR"] = self._prev_data_dir
        shutil.rmtree(self.tmp, ignore_errors=True)
        self._restore_pat_env()

    def _run(self, msg: dict):
        asyncio.run(mcp_screen.handle(msg, self.out, self.mgr,
                                      project_dir="/tmp/proj"))

    def _patch_validate(self, result):
        original = connector_setup.validate

        def fake(cid, token, **kwargs):
            return dict(result)

        connector_setup.validate = fake
        self.addCleanup(lambda: setattr(connector_setup, "validate", original))

    def _apps(self) -> dict:
        msg = self.out.last("mcp_apps")
        rows = msg["apps"] if msg else mcp_screen._apps(self.mgr)["apps"]
        return {a["id"]: a for a in rows}

    def test_connector_get_returns_the_figma_guide(self):
        self._run({"action": "connector_get", "connector": "figma"})
        g = self.out.last("connector_guide")
        self.assertEqual(g["id"], "figma")
        self.assertTrue(g["field_label"])
        self.assertTrue(any("figma.com/settings" in (s.get("url") or "")
                            for s in g["steps"]))
        self.assertFalse(g["connected"])

    def test_successful_test_saves_and_flips_state_to_ready(self):
        self._patch_validate({"ok": True, "reason": "ok", "network": False,
                              "account": "neha@example.com",
                              "message": "Connected to Figma as neha@example.com."})
        self._run({"action": "connector_test", "connector": "figma",
                   "token": SECRET})
        res = self.out.last("connector_result")
        self.assertTrue(res["ok"])
        self.assertTrue(res["saved"])
        app = self._apps()["figma"]
        self.assertTrue(app["connected"])
        self.assertEqual(app["state"], "ready")
        self.assertEqual(app["auth"], "credential")
        stored = connector_setup.KeyStore(
            Path(self.tmp) / "connector_keys.json").get("figma")
        self.assertEqual(stored["secret"], SECRET)
        self.assertFalse(stored["unverified"])
        self.assertEqual(os.environ.get(PAT_ENV), SECRET)

    def test_failed_test_does_not_save_and_keeps_state(self):
        self._patch_validate({"ok": False, "reason": "auth", "network": False,
                              "message": "Figma rejected that token."})
        self._run({"action": "connector_test", "connector": "figma",
                   "token": SECRET})
        res = self.out.last("connector_result")
        self.assertFalse(res["ok"])
        self.assertFalse(res["saved"])
        self.assertIsNone(connector_setup.KeyStore(
            Path(self.tmp) / "connector_keys.json").get("figma"))
        self.assertEqual(self._apps()["figma"]["state"], "needs_key")

    def test_network_failure_offers_unverified_save(self):
        self._patch_validate({"ok": False, "reason": "network", "network": True,
                              "message": "Could not reach Figma."})
        self._run({"action": "connector_test", "connector": "figma",
                   "token": SECRET})
        res = self.out.last("connector_result")
        self.assertFalse(res["ok"])
        self.assertTrue(res["network"])
        self.assertIsNone(connector_setup.KeyStore(
            Path(self.tmp) / "connector_keys.json").get("figma"))

        self._run({"action": "connector_save", "connector": "figma",
                   "token": SECRET})
        res = self.out.last("connector_result")
        self.assertTrue(res["ok"])
        self.assertTrue(res["saved"])
        self.assertTrue(res["unverified"])
        stored = connector_setup.KeyStore(
            Path(self.tmp) / "connector_keys.json").get("figma")
        self.assertTrue(stored["unverified"])
        app = self._apps()["figma"]
        self.assertTrue(app["connected"])
        self.assertTrue(app["unverified"])

    def test_bad_token_is_never_saved_even_via_save_anyway(self):
        self._patch_validate({"ok": False, "reason": "auth", "network": False,
                              "message": "Figma rejected that token."})
        self._run({"action": "connector_save", "connector": "figma",
                   "token": SECRET})
        res = self.out.last("connector_result")
        self.assertFalse(res["ok"])
        self.assertIsNone(connector_setup.KeyStore(
            Path(self.tmp) / "connector_keys.json").get("figma"))

    def test_delete_removes_key_and_state(self):
        self._patch_validate({"ok": True, "reason": "ok", "network": False,
                              "account": "neha",
                              "message": "Connected to Figma as neha."})
        self._run({"action": "connector_test", "connector": "figma",
                   "token": SECRET})
        self._run({"action": "connector_delete", "connector": "figma"})
        res = self.out.last("connector_result")
        self.assertTrue(res["ok"])
        self.assertTrue(res["removed"])
        self.assertIsNone(connector_setup.KeyStore(
            Path(self.tmp) / "connector_keys.json").get("figma"))
        self.assertEqual(self._apps()["figma"]["state"], "needs_key")
        self.assertIsNone(os.environ.get(PAT_ENV))

    def test_screen_actions_never_log_the_secret(self):
        self._patch_validate({"ok": False, "reason": "auth", "network": False,
                              "message": "Figma rejected that token."})
        self._run({"action": "connector_test", "connector": "figma",
                   "token": SECRET})
        log_path = Path(self.tmp) / "mcp-events.log"
        log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        self.assertNotIn(SECRET, log)
        self.assertNotIn(SECRET, json.dumps(self.out.msgs))


class ConnectorUiTests(unittest.TestCase):
    """app.js must route a credential app to the guide, not Google OAuth."""

    def _extract_fn(self, app: str, name: str) -> str:
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
        assert end is not None, "could not brace-match " + name
        return app[start:end]

    def test_credential_app_opens_the_guide(self):
        if not shutil.which("node"):
            self.skipTest("node not available")
        app = (Path(__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
        fn = self._extract_fn(app, "_mcpAppAction")
        cases = [
            {"id": "figma", "label": "Figma", "auth": "credential",
             "connected": False, "registered": False, "state": "needs_key"},
            {"id": "figma", "label": "Figma", "auth": "credential",
             "connected": True, "registered": True, "state": "ready"},
        ]
        script = (fn + "\nconst cases = " + json.dumps(cases)
                  + ";\nconsole.log(JSON.stringify(cases.map(_mcpAppAction)));\n")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(script)
            path = fh.name
        try:
            import subprocess
            proc = subprocess.run(["node", path], capture_output=True, text=True,
                                  timeout=30)
        finally:
            os.unlink(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = json.loads(proc.stdout.strip())
        for row in got:
            self.assertEqual(row["action"], "connector_setup")
            self.assertFalse(row["confirm"])
            self.assertFalse(row["danger"])
        self.assertEqual(got[0]["label"], "Set up")
        self.assertIn("access token", got[0]["status"])
        self.assertEqual(got[1]["label"], "Update key")
        self.assertIn("Connected", got[1]["status"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
