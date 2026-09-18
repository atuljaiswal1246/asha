"""Tests for the channel gateway (G3). No network — fake transport/channel."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from channels import FakeChannel, Gateway, TelegramChannel  # noqa: E402


class TelegramChannelTests(unittest.TestCase):
    def test_send_and_get_updates_use_transport(self):
        calls = []

        def transport(method, payload):
            calls.append((method, payload))
            return {"ok": True, "result": []}

        ch = TelegramChannel("tok", transport=transport)
        ch.send(123, "hi")
        ch.get_updates(offset=5)
        self.assertEqual(calls[0][0], "sendMessage")
        self.assertEqual(calls[0][1]["chat_id"], 123)
        self.assertEqual(calls[1][0], "getUpdates")
        self.assertEqual(calls[1][1]["offset"], 5)


class GatewayTests(unittest.TestCase):
    def _update(self, uid, chat, text):
        return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}

    def test_routes_and_replies(self):
        ch = FakeChannel([self._update(1, 42, "hello")])
        gw = Gateway(ch, lambda t: f"echo:{t}")
        self.assertEqual(gw.poll_once(), 1)
        self.assertEqual(ch.sent, [(42, "echo:hello")])

    def test_offset_advances(self):
        ch = FakeChannel([self._update(7, 1, "a")])
        gw = Gateway(ch, lambda t: "ok")
        gw.poll_once()
        self.assertEqual(gw._offset, 8)

    def test_handler_error_becomes_reply(self):
        ch = FakeChannel([self._update(1, 1, "x")])

        def boom(_t):
            raise RuntimeError("bad")

        Gateway(ch, boom).poll_once()
        self.assertIn("[error] bad", ch.sent[0][1])

    def test_allowlist_blocks_unknown_chat(self):
        ch = FakeChannel([self._update(1, 99, "hello")])
        gw = Gateway(ch, lambda t: "nope", allow=[42])
        gw.poll_once()
        self.assertEqual(ch.sent, [])

    def test_empty_text_ignored(self):
        ch = FakeChannel([self._update(1, 1, "   ")])
        Gateway(ch, lambda t: "x").poll_once()
        self.assertEqual(ch.sent, [])

    def test_transport_error_is_swallowed(self):
        class Boom:
            def get_updates(self, offset=None, timeout=0):
                raise RuntimeError("net down")

        gw = Gateway(Boom(), lambda t: "x")
        self.assertEqual(gw.poll_once(), 0)
        self.assertIn("net down", gw.last_error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
