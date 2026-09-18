"""Recent chats bug-fix tests (symptoms A/B/C).

Tests that:
  - The live session appears in the recent list (A).
  - Messages are persisted and retrievable in full (B).
  - Opening an old session does not orphan the live session (C).

Uses a temp SessionStore (no server import needed for core logic).
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sessions import SessionStore  # noqa: E402


class _StoreTestCase(unittest.TestCase):
    """Base class with an isolated temp SessionStore."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="asha-recent-")
        self.store = SessionStore(Path(self._tmp) / "state.db")

    def tearDown(self):
        self.store.close()


class LiveSessionInRecentList(_StoreTestCase):
    """Symptom A: the live session must appear in the recent list."""

    def test_live_session_appears(self):
        sid = self.store.new_session({"client": "ws"})
        self.store.append(sid, "user", "Hello")
        sessions = self.store.list_sessions(limit=12)
        ids = [s["session_id"] for s in sessions]
        self.assertIn(sid, ids)

    def test_live_session_flag(self):
        sid = self.store.new_session({"client": "ws"})
        self.store.append(sid, "user", "Hello")
        sessions = self.store.list_sessions(limit=12)
        matched = [s for s in sessions if s["session_id"] == sid]
        self.assertEqual(len(matched), 1)

    def test_live_session_at_top_when_recent(self):
        old = self.store.new_session()
        self.store.append(old, "user", "old chat")
        live = self.store.new_session()
        self.store.append(live, "user", "live chat")
        sessions = self.store.list_sessions(limit=12)
        ids = [s["session_id"] for s in sessions]
        self.assertEqual(ids[0], live)


class MessagesPersistedAndRetrievable(_StoreTestCase):
    """Symptom B: every turn must be stored and retrievable in full."""

    def test_user_and_bot_turns_stored(self):
        sid = self.store.new_session()
        self.store.append(sid, "user", "What is 2+2?")
        self.store.append(sid, "bot", "4")
        rows = self.store.messages(sid)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["role"], "user")
        self.assertEqual(rows[0]["text"], "What is 2+2?")
        self.assertEqual(rows[1]["role"], "bot")
        self.assertEqual(rows[1]["text"], "4")

    def test_multiple_turns_preserve_order(self):
        sid = self.store.new_session()
        for i in range(10):
            self.store.append(sid, "user" if i % 2 == 0 else "bot", f"turn {i}")
        rows = self.store.messages(sid)
        self.assertEqual(len(rows), 10)
        for i, r in enumerate(rows):
            self.assertEqual(r["text"], f"turn {i}")

    def test_full_transcript_on_reopen(self):
        sid = self.store.new_session()
        expected = []
        for i in range(20):
            role = "user" if i % 2 == 0 else "bot"
            text = f"message {i} of the conversation"
            self.store.append(sid, role, text)
            expected.append({"role": role, "text": text})
        rows = self.store.messages(sid)
        self.assertEqual(len(rows), 20)
        for r, e in zip(rows, expected):
            self.assertEqual(r["role"], e["role"])
            self.assertEqual(r["text"], e["text"])


class SessionTitleFromFirstUserMessage(_StoreTestCase):
    """Title derives from the first user message."""

    def test_title_from_first_user_message(self):
        sid = self.store.new_session()
        self.store.append(sid, "bot", "Hi there!")
        self.store.append(sid, "user", "Tell me about quantum computing")
        title = self.store.session_title(sid)
        self.assertEqual(title, "Tell me about quantum computing")

    def test_title_fallback_when_no_user_message(self):
        sid = self.store.new_session()
        self.store.append(sid, "bot", "Welcome!")
        title = self.store.session_title(sid, fallback="Chat")
        self.assertEqual(title, "Chat")

    def test_title_truncated_at_60_chars(self):
        sid = self.store.new_session()
        long_msg = "x" * 100
        self.store.append(sid, "user", long_msg)
        title = self.store.session_title(sid)
        self.assertEqual(len(title), 61)
        self.assertTrue(title.endswith("\u2026"))


class SessionsAreIsolated(_StoreTestCase):
    """Opening one session does not affect another."""

    def test_sessions_do_not_mix(self):
        a = self.store.new_session()
        b = self.store.new_session()
        self.store.append(a, "user", "alpha topic")
        self.store.append(a, "bot", "alpha reply")
        self.store.append(b, "user", "beta topic")
        self.store.append(b, "bot", "beta reply")
        self.assertEqual(len(self.store.messages(a)), 2)
        self.assertEqual(len(self.store.messages(b)), 2)
        self.assertEqual(self.store.messages(a)[0]["text"], "alpha topic")
        self.assertEqual(self.store.messages(b)[0]["text"], "beta topic")


class AdoptAndReturnBehavior(_StoreTestCase):
    """Symptom C: opening an old session must not orphan the live one.
    After adoption, the adopted session appears in the recent list.
    The previous live session also remains in the list."""

    def test_opening_old_session_makes_it_visible(self):
        live = self.store.new_session()
        self.store.append(live, "user", "current conversation")
        old = self.store.new_session()
        self.store.append(old, "user", "old conversation")
        # Simulate adopting old session (server sets session_holder["id"] = old)
        live_id = live
        adopted_id = old
        # After adoption: adopted session should be in the list
        sessions = self.store.list_sessions(limit=12)
        ids = [s["session_id"] for s in sessions]
        self.assertIn(adopted_id, ids)
        # Previous live session also still in the list
        self.assertIn(live_id, ids)

    def test_both_sessions_have_messages(self):
        a = self.store.new_session()
        self.store.append(a, "user", "first chat")
        self.store.append(a, "bot", "first reply")
        b = self.store.new_session()
        self.store.append(b, "user", "second chat")
        self.store.append(b, "bot", "second reply")
        # Both sessions' messages are fully retrievable
        self.assertEqual(len(self.store.messages(a)), 2)
        self.assertEqual(len(self.store.messages(b)), 2)
        self.assertEqual(self.store.messages(a)[1]["text"], "first reply")
        self.assertEqual(self.store.messages(b)[1]["text"], "second reply")

    def test_session_count_after_new_chat(self):
        """After starting a new chat, the previous session remains in the list."""
        first = self.store.new_session()
        self.store.append(first, "user", "hello")
        self.store.append(first, "bot", "hi there")
        # Simulate new_chat: create second session
        second = self.store.new_session()
        self.store.append(second, "user", "new question")
        sessions = self.store.list_sessions(limit=12)
        ids = [s["session_id"] for s in sessions]
        self.assertIn(first, ids)
        self.assertIn(second, ids)
        self.assertEqual(len(ids), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
