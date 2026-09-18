"""Tests for MemoryStore <-> memory_meta sidecar wiring (stdlib only).

Every test runs in a temp dir, so a temp MEMORY.md gets a temp sidecar and the
real Jarvis data dir is never touched.
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

import memory
import memory_meta


class MemoryWiringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-memwire-"))
        self.path = self.tmp / "MEMORY.md"
        self.meta_path = self.tmp / "memory_meta.json"
        self.store = memory.MemoryStore(self.path, char_limit=10000)

    def _meta(self) -> dict:
        return json.loads(self.meta_path.read_text(encoding="utf-8"))

    def _write_meta(self, data: dict) -> None:
        self.meta_path.write_text(json.dumps(data), encoding="utf-8")

    async def test_add_creates_sidecar_with_key(self):
        result = await self.store.add("My dog's name is Bruno")

        self.assertIn("Added", result)
        self.assertTrue(self.meta_path.exists())
        self.assertIn(memory_meta.entry_key("My dog's name is Bruno"), self._meta())

    async def test_replace_carries_first_seen_and_forgets_old_key(self):
        await self.store.add("My dog's name is Bruno")
        old_key = memory_meta.entry_key("My dog's name is Bruno")
        backdated = time.time() - 50 * 86400
        data = self._meta()
        data[old_key]["first_seen"] = backdated
        self._write_meta(data)

        result = await self.store.replace("Bruno", "My dog's name is Max")

        self.assertIn("Replaced", result)
        new_key = memory_meta.entry_key("My dog's name is Max")
        data = self._meta()
        self.assertIn(new_key, data)
        self.assertNotIn(old_key, data)
        self.assertAlmostEqual(data[new_key]["first_seen"], backdated, places=1)

    async def test_remove_forgets_key(self):
        await self.store.add("My dog's name is Bruno")
        key = memory_meta.entry_key("My dog's name is Bruno")

        result = await self.store.remove("Bruno")

        self.assertIn("Removed", result)
        self.assertNotIn(key, self._meta())

    async def test_stale_entry_ranks_below_fresh_in_snapshot_and_survives(self):
        await self.store.add("Fresh fact about penguins")
        await self.store.add("Stale fact about llamas")
        stale_key = memory_meta.entry_key("Stale fact about llamas")
        data = self._meta()
        data[stale_key]["first_seen"] = time.time() - 500 * 86400
        self._write_meta(data)

        snap = self.store.snapshot()

        self.assertLess(snap.index("Fresh fact"), snap.index("Stale fact"))
        # Decay only reorders; the stale entry is NOT deleted.
        self.assertIn("Stale fact about llamas", self.path.read_text(encoding="utf-8"))
        self.assertIn(stale_key, self._meta())

    async def test_entries_text_order_is_unchanged(self):
        await self.store.add("First entry")
        await self.store.add("Second entry")
        disk_order = self.store.entries_text()

        self.assertEqual(disk_order, "First entry\n§\nSecond entry")

    async def test_corrupt_sidecar_is_fail_open_on_add_snapshot_replace(self):
        self.meta_path.write_bytes(b"\x00\xff not json at all")

        self.assertIsInstance(self.store.snapshot(), str)

        self.meta_path.write_bytes(b"\x00\xff not json at all")
        result = await self.store.add("Penguins live in Antarctica")
        self.assertIn("Added", result)
        self.assertIn(
            "Penguins live in Antarctica", self.path.read_text(encoding="utf-8")
        )

        self.meta_path.write_bytes(b"\x00\xff not json at all")
        result = await self.store.replace("Antarctica", "Penguins live in the cold")
        self.assertIn("Replaced", result)
        self.assertIn(
            "Penguins live in the cold", self.path.read_text(encoding="utf-8")
        )

    async def test_snapshot_did_not_touch_last_seen(self):
        await self.store.add("Fresh fact about penguins")
        await self.store.add("Stale fact about llamas")
        stale_key = memory_meta.entry_key("Stale fact about llamas")
        data = self._meta()
        data[stale_key]["last_seen"] = time.time() - 500 * 86400
        self._write_meta(data)

        self.store.snapshot()

        # Snapshot must not refresh last_seen (that would erase staleness).
        self.assertAlmostEqual(
            self._meta()[stale_key]["last_seen"],
            data[stale_key]["last_seen"],
            places=1,
        )


if __name__ == "__main__":
    unittest.main()
