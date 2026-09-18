"""Tests for the MCP stdio client (C1). Uses a fake MCP server subprocess."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp_client import MCPError, MCPManager, tool_text  # noqa: E402
import mcp_client  # noqa: E402

FAKE_SERVER = textwrap.dedent(
    '''
    import sys, json
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if "id" not in msg:
            continue
        m, rid = msg.get("method"), msg["id"]
        if m == "initialize":
            res = {"protocolVersion": "2024-11-05", "capabilities": {},
                   "serverInfo": {"name": "fake"}}
        elif m == "tools/list":
            res = {"tools": [{"name": "echo", "description": "Echo text"},
                             {"name": "env", "description": "Read an env var"}]}
        elif m == "tools/call":
            args = (msg.get("params") or {}).get("arguments") or {}
            if (msg.get("params") or {}).get("name") == "env":
                import os
                res = {"content": [{"type": "text",
                                    "text": os.environ.get(args.get("var", ""), "MISSING")}]}
            else:
                res = {"content": [{"type": "text", "text": args.get("text", "")}]}
        else:
            res = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": res}) + "\\n")
        sys.stdout.flush()
    '''
)


class MCPClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asha-mcp-")
        self.server_py = Path(self.tmp) / "fake_server.py"
        self.server_py.write_text(FAKE_SERVER, encoding="utf-8")
        self.config = Path(self.tmp) / "mcp.json"
        self.config.write_text(json.dumps({
            "servers": [{"name": "fake", "command": sys.executable,
                         "args": [str(self.server_py)]}]
        }), encoding="utf-8")
        self.mgr = MCPManager(self.config)

    def tearDown(self):
        self.mgr.close()

    def test_configured_servers(self):
        self.assertEqual(self.mgr.configured(), ["fake"])

    def test_list_tools(self):
        tools = self.mgr.list_tools()
        self.assertEqual(tools[0]["server"], "fake")
        self.assertEqual(tools[0]["name"], "echo")

    def test_call_tool_round_trip(self):
        res = self.mgr.call_tool("fake", "echo", {"text": "hello mcp"})
        self.assertEqual(tool_text(res), "hello mcp")

    def test_unknown_server_raises(self):
        with self.assertRaises(MCPError):
            self.mgr.call_tool("nope", "echo", {})

    def test_missing_config_is_empty(self):
        mgr = MCPManager(Path(self.tmp) / "does-not-exist.json")
        self.assertEqual(mgr.configured(), [])

    def test_env_reaches_the_server_process(self):
        """A configured env var must be visible to the spawned server."""
        cfg = Path(self.tmp) / "mcp.json"
        cfg.write_text(json.dumps({"servers": [
            {"name": "fake", "command": sys.executable, "args": [str(self.server_py)],
             "env": {"JARVIS_TEST_TOKEN": "s3cret"}}]}), encoding="utf-8")
        mgr = MCPManager(cfg)
        try:
            res = mgr.call_tool("fake", "env", {"var": "JARVIS_TEST_TOKEN"})
            self.assertEqual(tool_text(res), "s3cret")
            self.assertEqual(mgr.call_tool("fake", "env", {"var": "NOPE_NOT_SET"},
                                           ).get("content")[0]["text"], "MISSING")
        finally:
            mgr.close()

    def test_env_is_redacted_in_servers(self):
        cfg = Path(self.tmp) / "mcp-creds.json"
        mgr = MCPManager(cfg)
        mgr.add_server("withcreds", command="npx", args=["-y", "pkg"],
                       env={"GOOGLE_CLIENT_SECRET": "topsecret"})
        got = mgr.servers()[0]
        self.assertEqual(got["env_keys"], ["GOOGLE_CLIENT_SECRET"])
        self.assertNotIn("env", got)
        self.assertNotIn("topsecret", json.dumps(got))


class _Resp:
    def __init__(self, body: dict):
        self.status_code = 200
        self.headers: dict = {}
        self.text = json.dumps(body)
        self._body = body

    def json(self):
        return self._body


class RemoteMCPTests(unittest.TestCase):
    """Streamable-HTTP transport, with httpx.post faked."""

    def _fake_post(self):
        def post(url, json=None, headers=None, timeout=None, follow_redirects=None):
            m = json.get("method")
            if m == "initialize":
                res = {"protocolVersion": "2024-11-05", "capabilities": {}}
            elif m == "tools/list":
                res = {"tools": [{"name": "search", "description": "web search"}]}
            elif m == "tools/call":
                res = {"content": [{"type": "text", "text": "result-ok"}]}
            else:
                res = {}
            return _Resp({"jsonrpc": "2.0", "id": json.get("id"), "result": res})
        return post

    def _mgr(self):
        cfg = Path(self.tmp) / "mcp.json"
        cfg.write_text(json.dumps({"servers": [
            {"name": "remote", "url": "https://mcp.example/mcp"}]}), encoding="utf-8")
        return MCPManager(cfg)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asha-mcp-remote-")
        import httpx
        self._orig = httpx.post
        httpx.post = self._fake_post()

    def tearDown(self):
        import httpx
        httpx.post = self._orig

    def test_remote_list_and_call(self):
        mgr = self._mgr()
        tools = mgr.list_tools()
        self.assertEqual(tools[0]["name"], "search")
        res = mgr.call_tool("remote", "search", {"q": "x"})
        self.assertEqual(tool_text(res), "result-ok")

    def test_manager_add_remove_and_redact(self):
        cfg = Path(self.tmp) / "mcp2.json"
        mgr = MCPManager(cfg)
        mgr.add_server("remote", url="https://mcp.example/mcp",
                       headers={"Authorization": "Bearer secret"})
        got = mgr.servers()[0]
        self.assertEqual(got["url"], "https://mcp.example/mcp")
        self.assertNotIn("headers", got)                      # not leaked
        self.assertEqual(got["header_keys"], ["Authorization"])
        self.assertTrue(mgr.remove_server("remote"))
        self.assertEqual(mgr.servers(), [])


class RegistryTests(unittest.TestCase):
    """Open MCP Registry discovery (offline)."""

    def test_normalize_prefers_remote(self):
        n = mcp_client._normalize_registry({
            "name": "io.x/tools", "title": "Tools", "description": "d",
            "remotes": [{"type": "streamable-http", "url": "https://x/mcp"}],
            "packages": [{"registryType": "npm", "identifier": "pkg"}]})
        self.assertEqual(n["transport"], "remote")
        self.assertEqual(n["url"], "https://x/mcp")
        self.assertEqual(n["name"], "Tools")

    def test_normalize_npm_package(self):
        n = mcp_client._normalize_registry({
            "name": "io.x/fs",
            "packages": [{"registryType": "npm", "identifier": "@me/fs",
                          "runtimeHint": "npx", "runtimeArguments": [{"value": "-y"}],
                          "environmentVariables": [{"name": "TOKEN"}]}]})
        self.assertEqual(n["transport"], "local")
        self.assertEqual(n["command"], "npx")
        self.assertEqual(n["args"], ["-y", "@me/fs"])
        self.assertEqual(n["env_keys"], ["TOKEN"])

    def test_normalize_oci_package(self):
        n = mcp_client._normalize_registry({
            "name": "io.x/db",
            "packages": [{"registryType": "oci", "identifier": "me/db:latest"}]})
        self.assertEqual(n["command"], "docker")
        self.assertIn("me/db:latest", n["args"])

    def test_normalize_unaddable(self):
        n = mcp_client._normalize_registry({"name": "io.x/empty"})
        self.assertEqual(n["transport"], "none")

    def test_search_registry_filters_and_dedupes(self):
        import httpx
        body = {"servers": [
            {"server": {"name": "io.x/a", "title": "A", "description": "d",
                        "remotes": [{"url": "https://a/mcp"}]}},
            {"server": {"name": "io.x/a", "title": "A", "description": "d",
                        "remotes": [{"url": "https://a/mcp"}]}},
            {"server": {"name": "io.x/empty"}},
        ]}

        class _R:
            def raise_for_status(self): pass
            def json(self): return body

        orig = httpx.get
        httpx.get = lambda *a, **k: _R()
        try:
            out = mcp_client.search_registry("x", limit=10)
        finally:
            httpx.get = orig
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["url"], "https://a/mcp")

    def test_search_registry_error(self):
        import httpx
        def boom(*a, **k): raise RuntimeError("nope")
        orig = httpx.get
        httpx.get = boom
        try:
            with self.assertRaises(MCPError):
                mcp_client.search_registry("x")
        finally:
            httpx.get = orig


class FeaturedTests(unittest.TestCase):
    """Curated starters are always addable."""

    def test_every_entry_has_a_target(self):
        for s in mcp_client.featured_servers():
            self.assertTrue(s.get("url") or s.get("command"), s["name"])
            self.assertTrue(s.get("name") and s.get("description"), s["name"])

    def test_project_placeholder_substituted(self):
        got = {s["name"]: s for s in mcp_client.featured_servers("/tmp/proj")}
        self.assertIn("/tmp/proj", got["filesystem"]["args"])
        self.assertNotIn("{project}", " ".join(got["filesystem"]["args"]))

    def test_placeholder_left_alone_without_project(self):
        got = {s["name"]: s for s in mcp_client.featured_servers("")}
        self.assertIn("{project}", got["filesystem"]["args"])

    def test_featured_records_add_cleanly(self):
        import tempfile
        cfg = Path(tempfile.mkdtemp(prefix="asha-feat-")) / "mcp.json"
        mgr = MCPManager(cfg)
        for s in mcp_client.featured_servers("/tmp/proj"):
            mgr.add_server(s["name"], s.get("command", ""), s.get("url", ""), s.get("args"))
        self.assertEqual(sorted(mgr.configured()),
                         sorted(s["name"] for s in mcp_client.FEATURED_SERVERS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
