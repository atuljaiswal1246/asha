"""Tests for MemoryStore dedupe wiring through memory_similar (stdlib only)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import memory
import memory_similar


class MemoryDedupeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-memdedupe-"))
        self.path = self.tmp / "memory.md"
        self.store = memory.MemoryStore(self.path, char_limit=10000)

    def _content(self) -> str:
        return self.path.read_text(encoding="utf-8")

    async def test_rephrase_is_skipped_and_file_unchanged(self):
        first = await self.store.add("My dog's name is Bruno")
        self.assertIn("Added", first)
        before = self._content()

        result = await self.store.add("Dog's name is Bruno")

        self.assertIn("Near-duplicate", result)
        self.assertIn("replace", result.lower())
        self.assertEqual(self._content(), before)

    async def test_entry_that_adds_detail_is_saved(self):
        await self.store.add("My favorite color is blue")

        result = await self.store.add(
            "My favorite color is blue and I also like green"
        )

        self.assertIn("Added", result)
        self.assertIn("I also like green", self._content())

    async def test_fail_open_when_similarity_raises(self):
        with mock.patch.object(
            memory_similar, "near_duplicate", side_effect=RuntimeError("boom")
        ):
            result = await self.store.add("Penguins live in Antarctica")

        self.assertIn("Added", result)
        self.assertIn("Penguins live in Antarctica", self._content())

    async def test_exact_duplicate_still_skipped(self):
        await self.store.add("My dog's name is Bruno")
        before = self._content()

        result = await self.store.add("My dog's name is Bruno")

        self.assertIn("Duplicate", result)
        self.assertEqual(self._content(), before)


if __name__ == "__main__":
    unittest.main()
