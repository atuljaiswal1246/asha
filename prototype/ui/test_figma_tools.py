"""Tests for the Figma tools as exposed to the brain (server.py + figma_rest).

Mocked HTTP only — no live Figma calls, no Keychain, no token store writes.
Importing ``server`` is heavy (pipecat), so run with the project venv:

    ../../.venv/bin/python -m unittest test_figma_tools -v
"""
from __future__ import annotations

import os
import sys
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402

import figma_rest  # noqa: E402
import mcp_oauth  # noqa: E402
import server  # noqa: E402


def _node(node_id="1:1", name="Frame", node_type="FRAME", children=None):
    out = {"id": node_id, "name": name, "type": node_type,
           "absoluteBoundingBox": {"x": 0, "y": 0}, "fills": [],
           "children": children if children is not None else []}
    return out


class Recorder:
    def __init__(self, routes):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get(request.url.path)
        if handler is None:
            return httpx.Response(404, json={"err": "no route " + request.url.path})
        return handler(request)


def _client(handler, token="test-token"):
    return figma_rest.FigmaClient(
        access_token=token, transport=httpx.MockTransport(handler))


class ToolRegistrationTests(unittest.TestCase):
    """The three tools must stay in the voice-tier tool list."""

    def test_figma_tools_are_registered(self):
        names = [s.name for s in server._voice_tool_schemas()]
        for tool in ("figma_files", "figma_file", "figma_nodes"):
            self.assertIn(tool, names, f"{tool} fell out of the tool list")

    def test_schema_shapes_are_speech_friendly(self):
        schemas = {s.name: s for s in server._voice_tool_schemas()}
        self.assertEqual(schemas["figma_files"].required, [])
        self.assertEqual(schemas["figma_file"].required, ["key"])
        self.assertEqual(schemas["figma_nodes"].required,
                         ["file_key", "node_ids"])


class FilesToolTests(unittest.TestCase):
    def test_happy_path(self):
        rec = Recorder({"/v1/projects/P1/files": lambda r: httpx.Response(
            200, json={"files": [{"key": "k1", "name": "Design",
                                  "last_modified": "2026-01-02T00:00:00Z"}]})})
        out = server.figma_files({"project_id": "P1"}, client=_client(rec))
        self.assertIn("k1", out)
        self.assertIn("Design", out)
        self.assertIn("Figma files (1)", out)

    def test_no_ids_is_actionable(self):
        out = server.figma_files({}, client=_client(
            lambda r: httpx.Response(200, json={})))
        self.assertIn("project_id", out)
        self.assertIn("folder_id", out)


class FileToolTests(unittest.TestCase):
    def test_happy_path_is_bounded_and_labelled(self):
        doc = _node("0:0", "Page 1", "CANVAS", children=[
            _node("1:1", "Card", "FRAME", children=[]),
        ])
        rec = Recorder({"/v1/files/abc": lambda r: httpx.Response(200, json={
            "name": "My File", "version": "42", "editorType": "figma",
            "document": doc, "truncated": False})})
        out = server.figma_file({"key": "abc"}, client=_client(rec))
        self.assertIn("My File", out)
        self.assertIn("Page 1", out)
        self.assertIn("Card", out)
        self.assertIn("#1:1", out)

    def test_missing_key_is_an_error_not_a_request(self):
        called: list[httpx.Request] = []
        out = server.figma_file({}, client=_client(
            lambda r: called.append(r) or httpx.Response(200, json={})))
        self.assertIn("required", out.lower())
        self.assertEqual(called, [])

    def test_large_file_output_is_bounded(self):
        kids = [_node(f"1:{i}", f"Node {i}") for i in range(600)]
        doc = _node("0:0", "Doc", "DOCUMENT", children=kids)
        rec = Recorder({"/v1/files/big": lambda r: httpx.Response(200, json={
            "name": "Big", "version": "1", "editorType": "figma",
            "document": doc, "truncated": False})})
        out = server.figma_file({"key": "big"}, client=_client(rec))
        self.assertIn("truncated", out)
        self.assertLessEqual(out.count("\n") + 1, server._FIGMA_MAX_LINES + 2)


class NodesToolTests(unittest.TestCase):
    def test_happy_path_and_missing(self):
        rec = Recorder({"/v1/files/abc/nodes": lambda r: httpx.Response(200, json={
            "nodes": {"1:1": {"document": _node("1:1", "Card")},
                      "9:9": None}})})
        out = server.figma_nodes({"file_key": "abc", "node_ids": "1:1,9:9"},
                                 client=_client(rec))
        self.assertIn("Card", out)
        self.assertIn("Missing", out)
        self.assertIn("9:9", out)


class DegradeTests(unittest.TestCase):
    def setUp(self):
        # A real PAT may be present in .env (loaded by `import server`); these
        # tests exercise the OAuth/no-token paths, so isolate the environment.
        self._saved_pat = {name: os.environ.pop(name, None)
                           for name in figma_rest.PAT_ENV_VARS}

    def tearDown(self):
        for name, value in self._saved_pat.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_no_token_names_the_exact_redirect_uri(self):
        called: list[httpx.Request] = []
        client = figma_rest.FigmaClient(
            transport=httpx.MockTransport(
                lambda r: called.append(r) or httpx.Response(200, json={})))
        with mock.patch.dict(os.environ,
                             {"JARVIS_OAUTH_HOST": "", "JARVIS_OAUTH_PORT": ""}):
            with mock.patch.object(
                    figma_rest.mcp_oauth, "access_token",
                    side_effect=mcp_oauth.OAuthError("not signed in")):
                out = server.figma_file({"key": "abc"}, client=client)
        self.assertIn("not connected", out.lower())
        self.assertIn("http://127.0.0.1:56123/callback", out)
        self.assertEqual(called, [])

    def test_expired_token_is_short_and_actionable(self):
        out = server.figma_file(
            {"key": "abc"},
            client=_client(lambda r: httpx.Response(401, json={"err": "nope"})))
        self.assertIn("401", out)
        self.assertIn("Reconnect", out)
        self.assertNotIn("Bearer", out)

    def test_rate_limit_reports_retry_after(self):
        def handler(request):
            return httpx.Response(429, headers={
                "Retry-After": "30", "X-Figma-Plan-Tier": "starter",
                "X-Figma-Rate-Limit-Type": "low"}, json={"message": "slow down"})
        out = server.figma_nodes(
            {"file_key": "abc", "node_ids": "1:1"}, client=_client(handler))
        self.assertIn("429", out)
        self.assertIn("30", out)
        self.assertIn("starter", out)

    def test_transport_failure_is_not_raw_text(self):
        def handler(request):
            raise httpx.ConnectError("connection refused", request=request)
        out = server.figma_file({"key": "abc"}, client=_client(handler))
        self.assertIn("Could not reach Figma", out)
        self.assertNotIn("Traceback", out)


class ConnectPathTests(unittest.TestCase):
    def test_redirect_uri_is_the_documented_loopback(self):
        with mock.patch.dict(os.environ,
                             {"JARVIS_OAUTH_HOST": "", "JARVIS_OAUTH_PORT": ""}):
            self.assertEqual(figma_rest.redirect_uri(),
                             "http://127.0.0.1:56123/callback")

    def test_missing_credentials_names_the_redirect_uri(self):
        cleared = {k: "" for k in (
            "JARVIS_MCP_CLIENT_ID_FIGMA", "JARVIS_MCP_CLIENT_SECRET_FIGMA",
            "JARVIS_MCP_CLIENT_ID", "JARVIS_MCP_CLIENT_SECRET")}
        with mock.patch.dict(os.environ, cleared):
            with self.assertRaises(figma_rest.FigmaConfigError) as cm:
                figma_rest._client_credentials()
        self.assertIn("http://127.0.0.1:56123/callback", str(cm.exception))

    def test_redirect_rejection_is_an_actionable_error(self):
        exc = figma_rest._translate_token_error(
            "token endpoint: {\"error\": \"redirect_uri mismatch\"}",
            "http://127.0.0.1:56123/callback")
        self.assertIsInstance(exc, figma_rest.FigmaConfigError)
        self.assertIn("http://127.0.0.1:56123/callback", str(exc))

    def test_connect_saves_a_refreshable_record(self):
        class FakeLoopback:
            def __init__(self, port, host="127.0.0.1"):
                self.port = port
                self.host = host
                self.redirect_uri = figma_rest.redirect_uri(port)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def wait(self, timeout):
                return {"code": "the-code", "state": "fixed-state"}

        saved: dict = {}

        def fake_save(name, tok, **extra):
            saved["name"], saved["extra"] = name, extra
            return {"access_token": tok.get("access_token", "")}

        with mock.patch.object(figma_rest.mcp_oauth, "preconfigured_client",
                               return_value={"client_id": "cid",
                                             "client_secret": "secret"}), \
                mock.patch.object(figma_rest.oauth_common, "Loopback",
                                  FakeLoopback), \
                mock.patch.object(figma_rest.webbrowser, "open",
                                  return_value=True), \
                mock.patch.object(figma_rest.secrets, "token_urlsafe",
                                  return_value="fixed-state"), \
                mock.patch.object(figma_rest.mcp_oauth, "_token_request",
                                  return_value={"access_token": "tok",
                                                "refresh_token": "ref",
                                                "expires_in": 3600}), \
                mock.patch.object(figma_rest.mcp_oauth, "_save",
                                  side_effect=fake_save):
            rec = figma_rest.connect(open_browser=False)
        self.assertEqual(rec["access_token"], "tok")
        self.assertEqual(saved["name"], "Figma")
        self.assertEqual(saved["extra"]["token_endpoint"],
                         figma_rest.REFRESH_URL)
        self.assertEqual(saved["extra"]["client_id"], "cid")


if __name__ == "__main__":
    unittest.main(verbosity=2)
