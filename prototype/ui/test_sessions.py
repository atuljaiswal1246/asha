"""Session store tests (A4). Pure stdlib, temp DB."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sessions import SessionStore  # noqa: E402


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asha-sess-")
        self.store = SessionStore(Path(self.tmp) / "state.db")

    def tearDown(self):
        self.store.close()

    def test_new_append_list_messages(self):
        sid = self.store.new_session({"k": "v"})
        self.store.append(sid, "user", "hello world about pytest")
        self.store.append(sid, "bot", "sure")
        rows = self.store.messages(sid)
        self.assertEqual([r["role"] for r in rows], ["user", "bot"])
        self.assertEqual(len(self.store.list_sessions()), 1)
        self.assertEqual(self.store.session_title(sid), "hello world about pytest")

    def test_search_finds_message(self):
        sid = self.store.new_session()
        self.store.append(sid, "user", "the orchestrator uses worktrees")
        hits = self.store.search("worktrees")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["session_id"], sid)

    def test_empty_query_returns_nothing(self):
        self.assertEqual(self.store.search(""), [])

    def test_sessions_are_isolated(self):
        a = self.store.new_session()
        b = self.store.new_session()
        self.store.append(a, "user", "alpha topic")
        self.store.append(b, "user", "beta topic")
        self.assertEqual(len(self.store.messages(a)), 1)
        self.assertEqual(self.store.session_title(a), "alpha topic")


if __name__ == "__main__":
    unittest.main(verbosity=2)
