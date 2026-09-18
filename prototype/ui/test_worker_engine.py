"""Tests for worker_engine session persistence (offline; no provider calls)."""

import tempfile
import unittest
from pathlib import Path

import worker_engine as we


class WorkerEnginePersistTests(unittest.TestCase):
    def setUp(self):
        self._orig = {
            "DATA_DIR": we._DATA_DIR,
            "SESSION_STORE": we._SESSION_STORE,
            "PROJECTS_FILE": we._PROJECTS_FILE,
            "PROJECTS": list(we._PROJECTS),
            "PROJECT_CHOSEN": we._PROJECT_CHOSEN,
        }
        self.tmp = tempfile.mkdtemp(prefix="jarvis-we-")
        we._set_data_dir(self.tmp)
        we._SESSIONS.clear()
        we._SESSION_STORE = None

    def tearDown(self):
        we._SESSIONS.clear()
        we._SESSION_STORE = None
        we._DATA_DIR = self._orig["DATA_DIR"]
        we._PROJECTS_FILE = self._orig["PROJECTS_FILE"]
        we._PROJECTS = self._orig["PROJECTS"]
        we._PROJECT_CHOSEN = self._orig["PROJECT_CHOSEN"]

    def _session(self, sid="s1"):
        s = we.NativeSession(sid, "build", "big-pickle", "opencode")
        s.title = "test session"
        s.add_event({"type": "text", "text": "hello"})
        s.append_llm({"role": "user", "content": "hi"})
        return s

    def test_to_state_round_trip_preserves_fields(self):
        s = self._session("abc")
        s.tokens = {"input": 5, "output": 7}
        state = s.to_state()
        restored = we._session_from_state(state)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.id, "abc")
        self.assertEqual(restored.model, "big-pickle")
        self.assertEqual(restored.messages, s.messages)
        self.assertEqual(restored.llm_messages, s.llm_messages)
        self.assertEqual(restored.tokens, {"input": 5, "output": 7})

    def test_to_state_excludes_lock_and_event(self):
        state = self._session("abc").to_state()
        self.assertNotIn("_lock", state)
        self.assertNotIn("cancel", state)
        self.assertNotIn("denied", state)

    def test_to_state_tolerates_non_serializable_values(self):
        s = self._session("abc")
        s.messages.append({"id": "m", "thing": object()})
        state = s.to_state()
        self.assertIsInstance(state["messages"][-1]["thing"], str)

    def test_restored_running_becomes_error(self):
        s = self._session("run1")
        self.assertEqual(s.status, "running")
        we._persist_session(s)
        we._SESSIONS.clear()
        we._restore_sessions()
        restored = we._SESSIONS["run1"]
        self.assertEqual(restored.status, "error")
        self.assertEqual(restored.finish, "error")

    def test_save_restore_cycle_survives_simulated_restart(self):
        s = self._session("survivor")
        we._register_session(s)
        self.assertTrue(Path(we._session_store().dir, "survivor.json").exists())
        we._SESSIONS.clear()
        restored = we._restore_sessions()
        self.assertEqual(restored, 1)
        msgs = we.session_messages("survivor")
        self.assertTrue(msgs)
        self.assertEqual(msgs[0]["content"][0]["text"], "hello")

    def test_corrupt_file_is_skipped_without_raising(self):
        store = we._session_store()
        (Path(store.dir) / "bad.json").write_text("{ not json", encoding="utf-8")
        self.assertEqual(we._restore_sessions(), 0)

    def test_persist_noops_when_session_persist_unavailable(self):
        original = we.session_persist
        we.session_persist = None
        we._SESSION_STORE = None
        try:
            s = self._session("abc")
            we._persist_session(s)  # must not raise
            self.assertEqual(we._restore_sessions(), 0)
        finally:
            we.session_persist = original

    def test_store_unavailable_without_data_dir(self):
        we._DATA_DIR = ""
        we._SESSION_STORE = None
        self.assertIsNone(we._session_store())
        we._persist_session(self._session("abc"))  # must not raise
        self.assertEqual(we._restore_sessions(), 0)

    def test_prune_cap_keeps_store_bounded(self):
        store = we._session_store()
        for i in range(we.SESSION_STORE_MAX + 5):
            store.save(f"s{i:03d}", {"id": f"s{i:03d}"})
        we._restore_sessions()
        self.assertLessEqual(len(store.list_ids()), we.SESSION_STORE_MAX)

    def test_unregister_deletes_persisted_file(self):
        s = self._session("gone")
        we._register_session(s)
        store = we._session_store()
        self.assertIn("gone", store.list_ids())
        we._unregister_session("gone")
        self.assertNotIn("gone", store.list_ids())


if __name__ == "__main__":
    unittest.main()
