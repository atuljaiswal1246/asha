"""Tests for the plugin/hook surface. Pure stdlib, temp dir, no network."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hooks  # noqa: E402


class HookTests(unittest.TestCase):
    def setUp(self):
        hooks.reset()

    def tearDown(self):
        hooks.reset()

    def test_register_tool_exposes_schema_and_handler(self):
        api = hooks.PluginAPI()
        api.register_tool("shout", "Uppercase.", {"type": "object"},
                          lambda args: (args.get("text") or "").upper())
        names = [t["function"]["name"] for t in hooks.extra_tools()]
        self.assertIn("shout", names)
        self.assertEqual(hooks.tool_handlers()["shout"]({"text": "hi"}), "HI")

    def test_register_tool_rejects_bad_input(self):
        api = hooks.PluginAPI()
        with self.assertRaises(ValueError):
            api.register_tool("", "d", {}, lambda a: "x")
        with self.assertRaises(ValueError):
            api.register_tool("t", "d", {}, None)

    def test_emit_calls_handlers(self):
        seen = []
        hooks.PluginAPI().on("post_tool", lambda **kw: seen.append(kw))
        hooks.emit("post_tool", name="read_file", args={}, result="ok")
        self.assertEqual(seen[0]["name"], "read_file")

    def test_pre_tool_can_block(self):
        hooks.PluginAPI().on("pre_tool",
                             lambda name, args: "NO" if name == "run_bash" else None)
        self.assertEqual(hooks.emit("pre_tool", name="run_bash", args={}), "NO")
        self.assertIsNone(hooks.emit("pre_tool", name="read_file", args={}))

    def test_broken_handler_is_fail_open(self):
        hooks.PluginAPI().on("post_tool", lambda **kw: 1 / 0)
        # must not raise
        self.assertIsNone(hooks.emit("post_tool", name="x", args={}, result="r"))

    def test_load_plugins_and_once(self):
        d = Path(tempfile.mkdtemp())
        (d / "p.py").write_text(
            "def register(api):\n"
            "    api.register_tool('t', 'd', {'type':'object'},\n"
            "                      lambda args: 'ran')\n")
        self.assertEqual(hooks.load_plugins(d), 1)
        self.assertEqual(hooks.load_plugins(d), 0)  # idempotent
        self.assertEqual(hooks.tool_handlers()["t"]({}), "ran")

    def test_broken_plugin_is_skipped(self):
        d = Path(tempfile.mkdtemp())
        (d / "bad.py").write_text("def register(api):\n    raise RuntimeError('x')\n")
        self.assertEqual(hooks.load_plugins(d), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
