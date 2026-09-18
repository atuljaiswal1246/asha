"""Tests for session_persist — atomic save, robust load, list and prune."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import session_persist


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-sessions-"))
        self.store = session_persist.SessionStore(self.tmp)

    def test_round_trip_save_load(self):
        data = {"model": "deepseek", "history": [1, 2, 3]}
        self.store.save("abc", data)
        self.assertEqual(self.store.load("abc"), data)

    def test_load_missing_is_none(self):
        self.assertIsNone(self.store.load("never-saved"))

    def test_save_is_atomic_and_leaves_no_temp_files(self):
        before = set(os.listdir(self.tmp))
        self.store.save("abc", {"x": 1})
        after = set(os.listdir(self.tmp))
        self.assertEqual(after - before, {"abc.json"})

    def test_overwrite_replaces_content(self):
        self.store.save("abc", {"n": 1})
        self.store.save("abc", {"n": 2})
        self.assertEqual(self.store.load("abc"), {"n": 2})

    def test_corrupt_json_loads_as_none_without_raising(self):
        (self.tmp / "bad.json").write_text("{ not json", encoding="utf-8")
        self.assertIsNone(self.store.load("bad"))

    def test_half_written_json_loads_as_none(self):
        (self.tmp / "half.json").write_text('{"a": [1, 2', encoding="utf-8")
        self.assertIsNone(self.store.load("half"))

    def test_non_object_json_loads_as_none(self):
        (self.tmp / "list.json").write_text("[1, 2, 3]", encoding="utf-8")
        self.assertIsNone(self.store.load("list"))

    def test_list_ids_sorted_and_ignores_temp_files(self):
        self.store.save("b", {"x": 1})
        self.store.save("a", {"x": 1})
        (self.tmp / ".a.junk.tmp").write_text("nope", encoding="utf-8")
        self.assertEqual(self.store.list_ids(), ["a", "b"])

    def test_delete_reports_existence(self):
        self.store.save("abc", {"x": 1})
        self.assertTrue(self.store.delete("abc"))
        self.assertFalse(self.store.delete("abc"))
        self.assertIsNone(self.store.load("abc"))

    def test_prune_drops_old_and_keeps_fresh(self):
        self.store.save("old", {"x": 1})
        self.store.save("new", {"x": 2})
        old_path = self.tmp / "old.json"
        stale = time.time() - 100 * 86400
        os.utime(old_path, (stale, stale))
        removed = self.store.prune(max_age_days=30)
        self.assertEqual(removed, ["old"])
        self.assertEqual(self.store.list_ids(), ["new"])

    def test_prune_oldest_keeps_newest_n(self):
        for i, sid in enumerate(["a", "b", "c", "d"]):
            self.store.save(sid, {"n": i})
            path = self.tmp / f"{sid}.json"
            ts = time.time() - (10 - i) * 60
            os.utime(path, (ts, ts))
        removed = self.store.prune_oldest(2)
        self.assertEqual(removed, ["a", "b"])
        self.assertEqual(self.store.list_ids(), ["c", "d"])

    def test_prune_oldest_removes_oldest_first(self):
        self.store.save("old", {"x": 1})
        self.store.save("mid", {"x": 2})
        self.store.save("new", {"x": 3})
        now = time.time()
        os.utime(self.tmp / "old.json", (now - 300, now - 300))
        os.utime(self.tmp / "mid.json", (now - 200, now - 200))
        os.utime(self.tmp / "new.json", (now - 100, now - 100))
        removed = self.store.prune_oldest(1)
        self.assertEqual(removed, ["mid", "old"])
        self.assertEqual(self.store.list_ids(), ["new"])

    def test_prune_oldest_returns_sorted_ids(self):
        for sid in ("zeta", "alpha", "mike"):
            self.store.save(sid, {"x": 1})
            os.utime(self.tmp / f"{sid}.json", (time.time() - 60, time.time() - 60))
        removed = self.store.prune_oldest(0)
        self.assertEqual(removed, ["alpha", "mike", "zeta"])

    def test_prune_oldest_zero_removes_all(self):
        self.store.save("a", {"x": 1})
        self.store.save("b", {"x": 1})
        self.assertEqual(self.store.prune_oldest(0), ["a", "b"])
        self.assertEqual(self.store.list_ids(), [])

    def test_prune_oldest_negative_treated_as_zero(self):
        self.store.save("a", {"x": 1})
        self.assertEqual(self.store.prune_oldest(-5), ["a"])
        self.assertEqual(self.store.list_ids(), [])

    def test_prune_oldest_keeps_all_when_under_cap(self):
        self.store.save("a", {"x": 1})
        self.store.save("b", {"x": 1})
        self.assertEqual(self.store.prune_oldest(10), [])
        self.assertEqual(self.store.list_ids(), ["a", "b"])

    def test_prune_oldest_missing_dir_is_safe(self):
        missing = session_persist.SessionStore(self.tmp / "gone")
        (self.tmp / "gone").rmdir()
        self.assertEqual(missing.prune_oldest(5), [])

    def test_unsafe_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.save("../escape", {"x": 1})
        with self.assertRaises(ValueError):
            self.store.load("a/b")

    def test_save_requires_a_dict(self):
        with self.assertRaises(TypeError):
            self.store.save("abc", ["not", "a", "dict"])

    def test_file_mode_is_owner_only_where_supported(self):
        path = self.store.save("abc", {"x": 1})
        if os.name != "posix":
            self.skipTest("posix-only permission check")
        self.assertEqual(oct(path.stat().st_mode & 0o777), oct(0o600))

    def test_disk_payload_is_valid_json(self):
        self.store.save("abc", {"x": 1})
        raw = (self.tmp / "abc.json").read_text(encoding="utf-8")
        self.assertEqual(json.loads(raw), {"x": 1})


class DefaultDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-sessionenv-"))
        self._old = os.environ.get("JARVIS_DATA_DIR")
        os.environ["JARVIS_DATA_DIR"] = str(self.tmp)
        self._repo_dir = (
            Path(session_persist.__file__).resolve().parent.parent
            / "data"
            / "session_store"
        )
        self._repo_existed = self._repo_dir.exists()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("JARVIS_DATA_DIR", None)
        else:
            os.environ["JARVIS_DATA_DIR"] = self._old

    def test_default_dir_honours_env(self):
        store = session_persist.SessionStore()
        store.save("abc", {"x": 1})
        self.assertTrue((self.tmp / "session_store" / "abc.json").exists())

    def test_default_dir_does_not_touch_repo(self):
        session_persist.SessionStore().save("abc", {"x": 1})
        self.assertEqual(self._repo_dir.exists(), self._repo_existed)


if __name__ == "__main__":
    unittest.main()
