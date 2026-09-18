"""Tests for the Figma REST client (figma_rest.py).

Uses httpx.MockTransport for zero-network requests. Every test passes an
explicit ``access_token`` so mcp_oauth / the Keychain is never touched.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402

import figma_rest  # noqa: E402
import mcp_oauth  # noqa: E402


_MODULE_PAT = {}


def setUpModule():
    # `import server` (via test_figma_tools) loads .env, which may set a real
    # PAT; keep this module hermetic so OAuth-path tests stay deterministic.
    for name in figma_rest.PAT_ENV_VARS:
        _MODULE_PAT[name] = os.environ.pop(name, None)


def tearDownModule():
    for name, value in _MODULE_PAT.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


class Recorder:
    """Callable MockTransport handler that records every request."""

    def __init__(self, routes):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get(request.url.path)
        if handler is None:
            return httpx.Response(404, json={"err": "no route " + request.url.path})
        return handler(request)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


def make_client(handler, token="test-token", **kwargs):
    transport = httpx.MockTransport(handler)
    return figma_rest.FigmaClient(access_token=token, transport=transport, **kwargs)


def compact_ok(request):
    return httpx.Response(200, json={"files": [
        {"key": "k1", "name": "Design", "last_modified": "2026-01-02T00:00:00Z",
         "thumbnail_url": "https://cdn.example/t.png", "extra": "ignored"},
        {"key": "k2", "name": "Library"},
    ]})


def node(node_id, name="n", node_type="FRAME", children=None):
    out = {"id": node_id, "name": name, "type": node_type,
           "absoluteBoundingBox": {"x": 1, "y": 2}, "fills": []}
    if children is not None:
        out["children"] = children
    return out


class ListFilesTests(unittest.TestCase):
    def test_folder_happy_path(self):
        rec = Recorder({"/v2/folders/F1/files": compact_ok})
        client = make_client(rec)
        files = client.list_files(folder_id="F1")
        self.assertEqual(rec.last.url.path, "/v2/folders/F1/files")
        self.assertEqual(rec.last.headers["Authorization"], "Bearer test-token")
        self.assertEqual(rec.last.headers["Accept"], "application/json")
        self.assertEqual(files, [
            {"key": "k1", "name": "Design",
             "last_modified": "2026-01-02T00:00:00Z",
             "thumbnail_url": "https://cdn.example/t.png"},
            {"key": "k2", "name": "Library", "last_modified": None,
             "thumbnail_url": None},
        ])
        self.assertEqual(set(files[0].keys()),
                         {"key", "name", "last_modified", "thumbnail_url"})

    def test_project_happy_path_and_branch_data(self):
        rec = Recorder({"/v1/projects/P1/files": compact_ok})
        client = make_client(rec)
        client.list_files(project_id="P1")
        self.assertEqual(rec.last.url.path, "/v1/projects/P1/files")
        self.assertNotIn("branch_data", rec.last.url.params)
        client.list_files(project_id="P1", branch_data=True)
        self.assertEqual(rec.last.url.params["branch_data"], "true")

    def test_no_id_is_an_actionable_error(self):
        client = make_client(lambda r: httpx.Response(200, json={}))
        with self.assertRaises(figma_rest.FigmaRequestError) as cm:
            client.list_files()
        msg = str(cm.exception)
        self.assertIn("project_id", msg)
        self.assertIn("folder_id", msg)

    def test_branch_data_only_when_true_on_folder(self):
        rec = Recorder({"/v2/folders/F1/files": compact_ok})
        client = make_client(rec)
        client.list_files(folder_id="F1")
        self.assertNotIn("branch_data", rec.last.url.params)


class GetFileTests(unittest.TestCase):
    def test_happy_path_trims_document(self):
        doc = node("0:0", "Page 1", "CANVAS", children=[
            node("1:1", "Frame", "FRAME", children=[
                node("1:2", "Label", "TEXT", children=[]),
            ]),
        ])
        rec = Recorder({"/v1/files/abc": lambda r: httpx.Response(200, json={
            "name": "My File", "lastModified": "2026-01-01T00:00:00Z",
            "editorType": "figma", "version": "42", "role": "viewer",
            "linkAccess": "view", "document": doc, "truncated": False})})
        client = make_client(rec)
        out = client.get_file("abc")
        self.assertEqual(rec.last.url.path, "/v1/files/abc")
        self.assertEqual(rec.last.url.params["depth"], "1")
        self.assertEqual(out["name"], "My File")
        self.assertEqual(out["version"], "42")
        self.assertFalse(out["truncated"])
        for banned in ("absoluteBoundingBox", "fills"):
            self.assertNotIn(banned, out["document"])
            self.assertNotIn(banned, out["document"]["children"][0])
        self.assertEqual(out["document"]["children"][0]["children"][0]["id"], "1:2")

    def test_missing_top_level_fields_become_none(self):
        rec = Recorder({"/v1/files/abc": lambda r: httpx.Response(
            200, json={"document": node("0:0")})})
        out = make_client(rec).get_file("abc")
        self.assertIsNone(out["name"])
        self.assertIsNone(out["lastModified"])
        self.assertEqual(out["document"]["id"], "0:0")

    def test_large_tree_is_truncated(self):
        deepest = node("leaf")
        for i in range(figma_rest.MAX_NODES + 10):
            deepest = node(f"n{i}", children=[deepest])
        rec = Recorder({"/v1/files/big": lambda r: httpx.Response(
            200, json={"name": "big", "document": deepest})})
        out = make_client(rec).get_file("big")
        self.assertTrue(out["truncated"])

    def test_rejects_bad_depth(self):
        client = make_client(lambda r: httpx.Response(200, json={}))
        for bad in (0, -1, "1", 1.5, True):
            with self.assertRaises(figma_rest.FigmaRequestError):
                client.get_file("abc", depth=bad)


class ReadNodesTests(unittest.TestCase):
    def test_happy_path_and_missing(self):
        rec = Recorder({"/v1/files/abc/nodes": lambda r: httpx.Response(200, json={
            "nodes": {
                "A:1": {"document": node("A:1", "Frame", "FRAME")},
                "B:2": None,
            }})})
        client = make_client(rec)
        out = client.read_nodes("abc", ["A:1", "B:2"])
        self.assertEqual(rec.last.url.path, "/v1/files/abc/nodes")
        self.assertEqual(rec.last.url.params["ids"], "A:1,B:2")
        self.assertEqual(rec.last.url.params["depth"], "1")
        self.assertEqual(out["missing"], ["B:2"])
        self.assertIn("A:1", out["nodes"])
        self.assertNotIn("absoluteBoundingBox", out["nodes"]["A:1"])
        self.assertFalse(out["truncated"])

    def test_accepts_comma_string(self):
        rec = Recorder({"/v1/files/abc/nodes": lambda r: httpx.Response(
            200, json={"nodes": {"A:1": {"document": node("A:1")}}})})
        make_client(rec).read_nodes("abc", "A:1")
        self.assertEqual(rec.last.url.params["ids"], "A:1")

    def test_empty_ids_rejected(self):
        client = make_client(lambda r: httpx.Response(200, json={}))
        for empty in ("", "  ", ",", []):
            with self.assertRaises(figma_rest.FigmaRequestError):
                client.read_nodes("abc", empty)

    def test_too_many_ids_rejected(self):
        client = make_client(lambda r: httpx.Response(200, json={}))
        too_many = [f"1:{i}" for i in range(figma_rest.MAX_NODE_IDS + 1)]
        with self.assertRaises(figma_rest.FigmaRequestError) as cm:
            client.read_nodes("abc", too_many)
        self.assertIn(str(figma_rest.MAX_NODE_IDS), str(cm.exception))


class ErrorTests(unittest.TestCase):
    def test_401_is_auth_error(self):
        client = make_client(lambda r: httpx.Response(401, json={"err": "Forbidden"}))
        with self.assertRaises(figma_rest.FigmaAuthError) as cm:
            client.get_file("abc")
        msg = str(cm.exception)
        self.assertTrue(any(w in msg for w in ("reconnect", "expired", "invalid")))
        self.assertNotIn("test-token", msg)
        self.assertEqual(cm.exception.status, 401)

    def test_403_is_auth_error(self):
        client = make_client(lambda r: httpx.Response(403, json={"message": "nope"}))
        with self.assertRaises(figma_rest.FigmaAuthError):
            client.get_file("abc")

    def test_429_carries_retry_guidance(self):
        def handler(request):
            return httpx.Response(429, headers={
                "Retry-After": "60",
                "X-Figma-Upgrade-Link": "https://figma.com/upgrade",
                "X-Figma-Plan-Tier": "starter",
                "X-Figma-Rate-Limit-Type": "low",
            }, json={"message": "rate limited"})
        client = make_client(handler)
        with self.assertRaises(figma_rest.FigmaRateLimited) as cm:
            client.get_file("abc")
        exc = cm.exception
        self.assertEqual(exc.retry_after, 60)
        self.assertEqual(exc.upgrade_link, "https://figma.com/upgrade")
        self.assertEqual(exc.plan_tier, "starter")
        self.assertEqual(exc.limit_type, "low")
        msg = str(exc)
        self.assertIn("Retry-After", msg)
        self.assertIn("retry", msg.lower())
        self.assertIn("https://figma.com/upgrade", msg)

    def test_404_is_not_found(self):
        client = make_client(lambda r: httpx.Response(404, json={"err": "missing"}))
        with self.assertRaises(figma_rest.FigmaNotFoundError):
            client.get_file("abc")

    def test_400_uses_provider_reason_safely(self):
        client = make_client(lambda r: httpx.Response(
            400, json={"err": "bad\nrequest"}))
        with self.assertRaises(figma_rest.FigmaRequestError) as cm:
            client.get_file("abc")
        self.assertIn("bad request", str(cm.exception))

    def test_non_json_success_body_is_a_request_error(self):
        client = make_client(lambda r: httpx.Response(200, text="<html>oops</html>"))
        with self.assertRaises(figma_rest.FigmaRequestError):
            client.get_file("abc")

    def test_network_failure_is_a_request_error(self):
        def handler(request):
            raise httpx.ConnectError("connection refused", request=request)
        client = make_client(handler)
        with self.assertRaises(figma_rest.FigmaRequestError) as cm:
            client.get_file("abc")
        self.assertIn("Could not reach Figma", str(cm.exception))


class PatAuthTests(unittest.TestCase):
    """PAT auth: X-Figma-Token when set, Bearer when not, never a leak."""

    def setUp(self):
        self._saved = {name: os.environ.pop(name, None)
                       for name in figma_rest.PAT_ENV_VARS}

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_pat_header_is_sent_when_env_set(self):
        os.environ["FIGMA_TOKEN"] = "pat-secret-xyz"
        rec = Recorder({"/v2/folders/F1/files": compact_ok})
        make_client(rec, token=None).list_files(folder_id="F1")
        self.assertEqual(rec.last.headers["X-Figma-Token"], "pat-secret-xyz")
        self.assertNotIn("Authorization", rec.last.headers)

    def test_explicit_access_token_wins_over_pat(self):
        os.environ["FIGMA_TOKEN"] = "pat-secret-xyz"
        rec = Recorder({"/v2/folders/F1/files": compact_ok})
        make_client(rec, token="oauth-token").list_files(folder_id="F1")
        self.assertEqual(rec.last.headers["Authorization"], "Bearer oauth-token")
        self.assertNotIn("X-Figma-Token", rec.last.headers)

    def test_pat_is_preferred_over_the_oauth_store(self):
        os.environ["FIGMA_TOKEN"] = "pat-secret-xyz"
        calls: list[tuple] = []
        original = mcp_oauth.access_token
        mcp_oauth.access_token = lambda *a, **k: calls.append((a, k)) or "stored"
        try:
            rec = Recorder({"/v2/folders/F1/files": compact_ok})
            make_client(rec, token=None).list_files(folder_id="F1")
        finally:
            mcp_oauth.access_token = original
        self.assertEqual(rec.last.headers["X-Figma-Token"], "pat-secret-xyz")
        self.assertNotIn("Authorization", rec.last.headers)
        self.assertEqual(calls, [])

    def test_alias_env_var_is_accepted(self):
        os.environ["JARVIS_FIGMA_PAT"] = "alias-secret"
        rec = Recorder({"/v2/folders/F1/files": compact_ok})
        make_client(rec, token=None).list_files(folder_id="F1")
        self.assertEqual(rec.last.headers["X-Figma-Token"], "alias-secret")

    def test_bearer_is_sent_when_no_pat(self):
        rec = Recorder({"/v2/folders/F1/files": compact_ok})
        make_client(rec).list_files(folder_id="F1")
        self.assertEqual(rec.last.headers["Authorization"], "Bearer test-token")
        self.assertNotIn("X-Figma-Token", rec.last.headers)

    def test_401_names_pat_mode_and_never_echoes_token(self):
        secret = "super-secret-pat-987"
        os.environ["FIGMA_TOKEN"] = secret
        client = make_client(
            lambda r: httpx.Response(401, json={"err": "nope"}), token=None)
        with self.assertRaises(figma_rest.FigmaAuthError) as cm:
            client.get_file("abc")
        msg = str(cm.exception)
        self.assertIn("Personal Access Token", msg)
        self.assertIn("Settings", msg)
        self.assertNotIn(secret, msg)

    def test_401_names_oauth_mode(self):
        client = make_client(
            lambda r: httpx.Response(403, json={"message": "nope"}))
        with self.assertRaises(figma_rest.FigmaAuthError) as cm:
            client.get_file("abc")
        msg = str(cm.exception)
        self.assertIn("OAuth", msg)
        self.assertIn("Reconnect", msg)
        self.assertNotIn("test-token", msg)

    def test_pat_never_leaks_into_logs_or_error_messages(self):
        secret = "super-secret-pat-987"
        os.environ["FIGMA_TOKEN"] = secret
        with self.assertLogs("figma_rest", level="INFO") as logs:
            client = make_client(
                lambda r: httpx.Response(401, json={"err": "nope"}), token=None)
            with self.assertRaises(figma_rest.FigmaAuthError) as cm:
                client.get_file("abc")
        logged = "\n".join(logs.output)
        self.assertIn("Personal Access Token", logged)
        self.assertNotIn(secret, logged)
        self.assertNotIn(secret, str(cm.exception))


class ConnectionTests(unittest.TestCase):
    def test_import_and_construct_never_touch_the_store(self):
        calls: list[tuple] = []
        original = mcp_oauth.access_token
        mcp_oauth.access_token = lambda *a, **k: calls.append((a, k)) or "x"
        try:
            figma_rest.FigmaClient()
            self.assertEqual(calls, [])
        finally:
            mcp_oauth.access_token = original

    def test_missing_token_is_config_error_and_no_request(self):
        calls: list[httpx.Request] = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={})

        original = mcp_oauth.access_token

        def boom(*a, **k):
            raise mcp_oauth.OAuthError("not signed in")

        mcp_oauth.access_token = boom
        try:
            client = figma_rest.FigmaClient(transport=httpx.MockTransport(handler))
            with self.assertRaises(figma_rest.FigmaConfigError) as cm:
                client.get_file("abc")
            self.assertIn("Connect Figma", str(cm.exception))
            self.assertEqual(calls, [])
        finally:
            mcp_oauth.access_token = original

    def test_lazy_token_is_resolved_at_request_time(self):
        def handler(request):
            return httpx.Response(200, json={"files": []})

        calls: list[tuple] = []
        original = mcp_oauth.access_token
        mcp_oauth.access_token = lambda *a, **k: calls.append((a, k)) or "lazy-token"
        try:
            client = make_client(handler, token=None)
            self.assertIsNone(client._access_token)
            self.assertEqual(calls, [])
            self.assertEqual(client.list_files(folder_id="F1"), [])
            self.assertEqual(calls, [(("Figma", figma_rest.BASE_URL), {})])
        finally:
            mcp_oauth.access_token = original


class TrimTests(unittest.TestCase):
    def test_trim_tree_respects_budget_and_does_not_mutate(self):
        tree = node("root", children=[node(f"c{i}") for i in range(600)])
        before = len(tree["children"])
        trimmed, truncated = figma_rest.trim_tree(tree)
        self.assertEqual(len(tree["children"]), before)
        self.assertTrue(truncated)
        self.assertLessEqual(len(trimmed["children"]), figma_rest.MAX_NODES)

    def test_trim_keeps_only_whitelisted_keys(self):
        trimmed, truncated = figma_rest.trim_tree(
            {"id": "1", "name": "x", "type": "FRAME", "characters": "hi",
             "x": 1, "y": 2, "fills": [], "children": []})
        self.assertEqual(set(trimmed.keys()),
                         {"id", "name", "type", "characters", "children"})
        self.assertFalse(truncated)


if __name__ == "__main__":
    unittest.main(verbosity=2)
