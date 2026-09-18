"""Tests for the skill store + nudger (E1-E3). Pure stdlib, temp dir."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from skills import MAX_DESCRIPTION, MemoryNudger, SkillStore  # noqa: E402


class SkillStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asha-skills-")
        self.store = SkillStore(Path(self.tmp) / "skills")

    def test_save_get_list(self):
        self.store.save("add-endpoint", "Add an endpoint plus a test",
                        "1. edit routes\n2. add test")
        skills = self.store.list()
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0]["name"], "add-endpoint")
        body = self.store.get("add-endpoint")
        self.assertIn("edit routes", body)

    def test_description_budget(self):
        self.store.save("long", "x" * 200, "body")
        self.assertLessEqual(len(self.store.list()[0]["description"]),
                             MAX_DESCRIPTION)

    def test_get_increments_uses(self):
        self.store.save("s", "d", "b")
        self.store.get("s")
        self.assertEqual(self.store.list()[0]["uses"], 1)

    def test_index_lists_descriptions(self):
        self.store.save("one", "first skill", "b")
        self.store.save("two", "second skill", "b")
        idx = self.store.index()
        self.assertIn("one: first skill", idx)
        self.assertIn("two: second skill", idx)

    def test_index_empty_when_no_skills(self):
        self.assertEqual(self.store.index(), "")

    def test_save_requires_body_and_name(self):
        with self.assertRaises(ValueError):
            self.store.save("", "d", "b")
        with self.assertRaises(ValueError):
            self.store.save("n", "d", "")

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.store.get("nope"))

    def test_archive_moves_not_deletes(self):
        self.store.save("gone", "d", "b")
        self.assertTrue(self.store.archive("gone"))
        self.assertEqual(self.store.list(), [])
        self.assertTrue((Path(self.tmp) / "skills" / "_archive" / "gone").is_dir())

    def test_curator_archives_unused_old(self):
        self.store.save("stale", "d", "b")  # uses=0, age 0
        self.assertEqual(self.store.curator(unused_days=0), ["stale"])

    def test_pin_shields_from_curator(self):
        self.store.save("keep", "d", "b")
        self.assertTrue(self.store.pin("keep"))
        self.assertEqual(self.store.curator(unused_days=0), [])
        self.assertTrue(self.store.list()[0]["pinned"])

    def test_unpin_allows_archiving(self):
        self.store.save("x", "d", "b")
        self.store.pin("x")
        self.store.pin("x", pinned=False)
        self.assertEqual(self.store.curator(unused_days=0), ["x"])

    def test_get_records_last_used(self):
        self.store.save("u", "d", "b")
        self.store.get("u")
        body = (Path(self.tmp) / "skills" / "u" / "SKILL.md").read_text()
        self.assertRegex(body, r"last_used: \d{4}-\d{2}-\d{2}")

    def test_curator_archives_used_but_long_idle(self):
        self.store.save("old", "d", "b")
        f = Path(self.tmp) / "skills" / "old" / "SKILL.md"
        f.write_text("---\nname: old\ndescription: d\nuses: 5\n"
                     "saved: 2000-01-01\nlast_used: 2000-01-01\n---\n\nb\n")
        self.assertEqual(self.store.curator(archive_days=90), ["old"])


class MemoryNudgerTests(unittest.TestCase):
    def test_ticks_on_interval(self):
        n = MemoryNudger(every=3)
        self.assertFalse(n.tick())   # 1
        self.assertFalse(n.tick())   # 2
        self.assertTrue(n.tick())    # 3
        self.assertFalse(n.tick())   # 4
        self.assertEqual(n.count, 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
