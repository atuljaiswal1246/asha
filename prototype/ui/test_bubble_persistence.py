"""Coding-bubble persistence: result/error bubbles must reach the session log.

Root cause covered here: ``_code_send_bubble`` / ``_code_send_result`` in
``server.py`` rendered coding replies straight to the UI via ``emit_to_ui``
and never touched the session store, so reopening that chat lost the whole
coding exchange (user turns, spoken bot turns and the greeting were already
persisted — see ``test_turn_persistence.py``).

Contract pinned here (all through the existing ``log_turn`` seam —
``log_session_turn`` -> ``SessionStore.append``, no new store/schema):
- every ``_send_code_result`` call stores exactly one bot row (final text,
  never one row per chunk — callers pass the finished summary);
- ``_send_code_bubble`` stores exactly one bot row when ``persist=True``
  (error text / terminal outcomes) and zero rows otherwise, so progress
  chatter ("On it…", per-step updates, BrainActivityFrame deltas — which
  bypass these helpers entirely) stays UI-only;
- the UI emit is identical either way (same frame type, text and order).

These tests fail before the fix (``server`` has no such helpers and the
closures never called ``log_turn``) and pass after.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402
from proto import CodingResultFrame  # noqa: E402
from pipecat.frames.frames import LLMTextFrame  # noqa: E402
from sessions import SessionStore  # noqa: E402


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jarvis-bubble-")
        self.store = SessionStore(Path(self.tmp) / "state.db")
        self.sid = self.store.new_session({"client": "test"})
        self.emitted: list = []

        async def _emit(frame):
            self.emitted.append(frame)

        self.emit = _emit
        self.log = lambda role, text: server.log_session_turn(  # noqa: E731
            self.store, self.sid, role, text)

    def tearDown(self):
        self.store.close()

    def drive(self, coro):
        return asyncio.run(coro)

    def rows(self):
        return self.store.messages(self.sid)

    def bot_rows(self):
        return [r for r in self.rows() if r["role"] == "bot"]


class ResultBubbleIsStoredOnce(_Case):
    def test_coding_reply_is_persisted(self):
        self.drive(server._send_code_result(
            self.emit, self.log, "Applied. Added retry to fetch."))
        rows = self.bot_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "Applied. Added retry to fetch.")

    def test_result_still_renders_result_frame(self):
        self.drive(server._send_code_result(self.emit, self.log, "Done."))
        self.assertEqual(len(self.emitted), 1)
        self.assertIsInstance(self.emitted[0], CodingResultFrame)
        self.assertEqual(self.emitted[0].text, "Done.")

    def test_whitespace_result_is_not_stored(self):
        self.drive(server._send_code_result(self.emit, self.log, "   "))
        self.assertEqual(self.bot_rows(), [])
        # …but the UI still shows what it always showed.
        self.assertEqual(len(self.emitted), 1)

    def test_emit_failure_still_stores_and_never_raises(self):
        async def _boom(frame):
            raise RuntimeError("ws gone")

        self.drive(server._send_code_result(_boom, self.log, "Applied. X."))
        self.assertEqual(len(self.bot_rows()), 1)


class ErrorBubbleIsStoredOnce(_Case):
    def test_error_bubble_is_persisted(self):
        self.drive(server._send_code_bubble(
            self.emit, self.log, "Couldn't run that coding task: boom",
            persist=True))
        rows = self.bot_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"],
                         "Couldn't run that coding task: boom")

    def test_error_bubble_still_renders_plain_bubble(self):
        self.drive(server._send_code_bubble(
            self.emit, self.log, "That didn't work: boom", persist=True))
        self.assertEqual(len(self.emitted), 1)
        self.assertIsInstance(self.emitted[0], LLMTextFrame)
        self.assertEqual(self.emitted[0].text, "That didn't work: boom")


class ProgressChatterStaysUiOnly(_Case):
    def test_progress_bubble_is_shown_but_not_stored(self):
        self.drive(server._send_code_bubble(
            self.emit, self.log, "On it — working in demo."))
        self.assertEqual(self.bot_rows(), [])
        self.assertEqual(len(self.emitted), 1)
        self.assertIsInstance(self.emitted[0], LLMTextFrame)
        self.assertEqual(self.emitted[0].text, "On it — working in demo.")

    def test_many_progress_updates_store_nothing(self):
        for i in range(10):
            self.drive(server._send_code_bubble(
                self.emit, self.log, f"Working… step {i}"))
        self.assertEqual(self.bot_rows(), [])
        self.assertEqual(len(self.emitted), 10)

    def test_ui_payload_identical_with_or_without_persist(self):
        self.drive(server._send_code_bubble(
            self.emit, self.log, "Couldn't open that folder: nope",
            persist=True))
        self.drive(server._send_code_bubble(
            self.emit, self.log, "On it — working in demo."))
        kinds = [type(f).__name__ for f in self.emitted]
        self.assertEqual(kinds, ["LLMTextFrame", "LLMTextFrame"])
        self.assertEqual([f.text for f in self.emitted],
                         ["Couldn't open that folder: nope",
                          "On it — working in demo."])
        # Only the error row was stored.
        self.assertEqual([r["text"] for r in self.bot_rows()],
                         ["Couldn't open that folder: nope"])


class ReopenIsComplete(_Case):
    def test_coding_reply_and_error_reopen_in_order(self):
        self.drive(server._send_code_result(
            self.emit, self.log, "Applied. Added retry to fetch."))
        self.drive(server._send_code_bubble(
            self.emit, self.log, "Couldn't revise that change: gone",
            persist=True))
        rows = self.store.messages(self.sid)  # what a reopen reads
        self.assertEqual([(r["role"], r["text"]) for r in rows], [
            ("bot", "Applied. Added retry to fetch."),
            ("bot", "Couldn't revise that change: gone"),
        ])


if __name__ == "__main__":
    unittest.main(verbosity=2)
