"""Tests for memory_meta — decay maths and sidecar robustness, no network."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import memory_meta


class EntryKeyTests(unittest.TestCase):
    def test_key_is_stable_short_and_distinct(self):
        key = memory_meta.entry_key("My dog's name is Bruno")
        self.assertEqual(key, memory_meta.entry_key("My dog's name is Bruno"))
        self.assertEqual(len(key), 16)
        self.assertNotEqual(key, memory_meta.entry_key("My cat's name is Bruno"))

    def test_key_ignores_whitespace_runs(self):
        self.assertEqual(
            memory_meta.entry_key("a  b\tc"),
            memory_meta.entry_key(" a b c "),
        )

    def test_delimiter_matches_memory_format(self):
        self.assertEqual(memory_meta.ENTRY_DELIMITER, "\n§\n")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-metan-"))
        self.path = self.tmp / "memory_meta.json"
        self.store = memory_meta.MemoryMetaStore(self.path)

    def test_stamp_records_first_and_last_seen(self):
        first = time.time() - 10 * 86400
        self.store.stamp("k", when=first)
        self.assertAlmostEqual(self.store.age_days("k"), 10.0, places=2)
        self.assertAlmostEqual(self.store.last_seen_days("k"), 10.0, places=2)
        self.store.touch("k")
        self.assertAlmostEqual(self.store.last_seen_days("k"), 0.0, places=2)
        # first-seen survives a touch.
        self.assertAlmostEqual(self.store.age_days("k"), 10.0, places=2)

    def test_unknown_key_is_fresh_and_scoreless(self):
        self.assertIsNone(self.store.age_days("nope"))
        self.assertIsNone(self.store.last_seen_days("nope"))
        self.assertEqual(self.store.decay_score("nope"), 1.0)

    def test_decay_at_half_life_boundaries(self):
        now = time.time()
        self.store.stamp("fresh", when=now)
        self.assertAlmostEqual(self.store.decay_score("fresh"), 1.0, places=3)

        self.store.stamp("one", when=now - 90 * 86400)
        self.assertAlmostEqual(self.store.decay_score("one"), 0.5, places=3)

        self.store.stamp("two", when=now - 180 * 86400)
        self.assertAlmostEqual(self.store.decay_score("two"), 0.25, places=3)

    def test_decay_respects_custom_half_life(self):
        now = time.time()
        self.store.stamp("k", when=now - 30 * 86400)
        self.assertAlmostEqual(
            self.store.decay_score("k", half_life_days=30), 0.5, places=3
        )
        self.assertEqual(self.store.decay_score("k", half_life_days=0), 0.0)

    def test_stamp_carries_first_seen_when_record_is_new(self):
        now = time.time()
        self.store.stamp("k", when=now, first_seen=now - 50 * 86400)
        self.assertAlmostEqual(self.store.first_seen("k"), now - 50 * 86400, places=1)
        self.assertAlmostEqual(self.store.age_days("k"), 50.0, places=1)
        self.assertAlmostEqual(self.store.last_seen_days("k"), 0.0, places=1)

    def test_stamp_ignores_carried_seen_when_record_already_aged(self):
        now = time.time()
        self.store.stamp("k", when=now - 10 * 86400)
        # A later stamp must not overwrite the existing first_seen.
        self.store.stamp("k", when=now, first_seen=now - 999 * 86400)
        self.assertAlmostEqual(self.store.age_days("k"), 10.0, places=1)

    def test_first_seen_returns_epoch_or_none(self):
        now = time.time()
        self.store.stamp("k", when=now)
        self.assertAlmostEqual(self.store.first_seen("k"), now, places=1)
        self.assertIsNone(self.store.first_seen("missing"))

    def test_forget_removes_key_and_reports(self):
        self.store.stamp("k")
        self.assertTrue(self.store.forget("k"))
        self.assertFalse(self.store.forget("k"))
        self.assertIsNone(self.store.age_days("k"))

    def test_decay_report_counts_fresh_and_stale(self):
        now = time.time()
        self.store.stamp("fresh", when=now)
        self.store.stamp("stale", when=now - 200 * 86400)
        self.store.stamp("missing_last", when=now)
        data = self.store.snapshot()
        data["missing_last"].pop("last_seen", None)
        self.path.write_text(json.dumps(data), encoding="utf-8")

        report = self.store.decay_report(days=90, now=now)
        self.assertEqual(report["total"], 3)
        self.assertEqual(report["stale"], 2)
        self.assertEqual(report["days"], 90.0)

    def test_decay_report_does_not_edit_the_sidecar(self):
        now = time.time()
        self.store.stamp("k", when=now - 200 * 86400)
        before = self.store.snapshot()
        self.store.decay_report(days=90, now=now)
        self.assertEqual(self.store.snapshot(), before)

    def test_corrupt_json_starts_empty_without_raising(self):
        self.path.write_text("{ this is not json", encoding="utf-8")
        self.assertIsNone(self.store.age_days("k"))
        self.store.stamp("k")
        self.assertIsNotNone(self.store.age_days("k"))
        json.loads(self.path.read_text(encoding="utf-8"))

    def test_non_object_json_is_tolerated(self):
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(self.store.snapshot(), {})
        self.store.stamp("k")
        self.assertIsNotNone(self.store.age_days("k"))

    def test_missing_file_is_tolerated(self):
        missing = memory_meta.MemoryMetaStore(self.tmp / "nope.json")
        self.assertEqual(missing.snapshot(), {})
        self.assertIsNone(missing.age_days("k"))

    def test_snapshot_is_a_copy(self):
        self.store.stamp("k")
        snap = self.store.snapshot()
        snap["k"]["first_seen"] = 0
        self.assertNotEqual(self.store.snapshot()["k"]["first_seen"], 0)

    def test_prune_forgets_only_old_entries(self):
        now = time.time()
        self.store.stamp("old", when=now - 200 * 86400)
        self.store.stamp("new", when=now)
        removed = self.store.prune(older_than_days=90, now=now)
        self.assertEqual(removed, ["old"])
        self.assertIsNone(self.store.age_days("old"))
        self.assertIsNotNone(self.store.age_days("new"))

    def test_writes_only_under_its_own_path(self):
        self.store.stamp("k")
        self.assertTrue(self.path.exists())
        children = sorted(p.name for p in self.tmp.iterdir())
        self.assertEqual(children, ["memory_meta.json"])


class ModuleLevelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-metaenv-"))
        self._old = os.environ.get("JARVIS_DATA_DIR")
        os.environ["JARVIS_DATA_DIR"] = str(self.tmp)
        self._repo_fallback = (
            Path(memory_meta.__file__).resolve().parent.parent
            / "data"
            / "memory_meta.json"
        )
        self._repo_existed = self._repo_fallback.exists()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("JARVIS_DATA_DIR", None)
        else:
            os.environ["JARVIS_DATA_DIR"] = self._old

    def test_env_overrides_data_dir(self):
        memory_meta.stamp("k")
        self.assertTrue((self.tmp / "memory_meta.json").exists())

    def test_env_use_does_not_touch_repo_by_default(self):
        memory_meta.stamp("k")
        self.assertTrue((self.tmp / "memory_meta.json").exists())
        # The no-env fallback location must not gain a file from this call.
        self.assertEqual(self._repo_fallback.exists(), self._repo_existed)

    def test_module_level_mirrors(self):
        now = time.time()
        memory_meta.stamp("aged", when=now, first_seen=now - 30 * 86400)
        self.assertAlmostEqual(
            memory_meta.first_seen("aged"), now - 30 * 86400, places=1
        )
        memory_meta.stamp("stale", when=now - 10 * 86400)
        report = memory_meta.decay_report(days=1, now=now)
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["stale"], 1)
        self.assertTrue(memory_meta.forget("aged"))
        self.assertFalse(memory_meta.forget("aged"))


if __name__ == "__main__":
    unittest.main()
