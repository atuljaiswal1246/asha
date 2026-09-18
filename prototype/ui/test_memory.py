"""Tests for memory durability review, cap merge, and read-only suggestions.

Run with:

    ../../.venv/bin/python -m unittest test_memory -v
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

import memory
import memory_meta

REQUEST_TEXT = "check my desktop for the calorie tracker folder"
ACTION_TEXT = (
    "User asked Jarvis to check if there is a 'calorie tracker' folder on their desktop"
)
PREFERENCE_TEXT = "I prefer dark mode"


def _fake_client(payload: str):
    class _Message:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self, content):
            self.message = _Message(content)

    class _Response:
        def __init__(self, content):
            self.choices = [_Choice(content)]

    class _Completions:
        async def create(self, **kwargs):
            return _Response(payload)

    class _Chat:
        def __init__(self):
            self.completions = _Completions()

    class _Client:
        def __init__(self):
            self.chat = _Chat()

    return _Client()


class _FakeContext:
    def __init__(self, messages):
        self.messages = messages


class DurableFactGuardTests(unittest.TestCase):
    def test_request_shaped_texts_are_not_durable(self):
        self.assertFalse(memory.is_durable_fact(REQUEST_TEXT))
        self.assertFalse(memory.is_durable_fact(ACTION_TEXT))
        self.assertFalse(memory.is_durable_fact("Can you check my calendar"))
        self.assertFalse(memory.is_durable_fact("Did I leave the stove on?"))

    def test_preference_and_personal_texts_are_durable(self):
        self.assertTrue(memory.is_durable_fact(PREFERENCE_TEXT))
        self.assertTrue(memory.is_durable_fact("My sister's name is Ada"))
        self.assertTrue(
            memory.is_durable_fact("I am working on the Cal Matters project")
        )


class ReviewerExtractionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-memreview-"))
        self.manager = memory.MemoryManager(
            self.tmp, memory_char_limit=2200, user_char_limit=1375
        )
        self.journal = self.tmp / "journal.jsonl"

    def _user_file(self) -> str:
        return (self.tmp / "USER.md").read_text(encoding="utf-8")

    def _reviewer(self, user_lines, payload):
        reviewer = memory.MemoryReviewer(
            llm_base_url="http://127.0.0.1:1/v1",
            llm_model="test-model",
            context=_FakeContext(
                [{"role": "user", "content": line} for line in user_lines]
            ),
            memory_manager=self.manager,
            journal_path=self.journal,
        )
        reviewer._client = _fake_client(json.dumps(payload))
        return reviewer

    async def test_request_shaped_input_yields_no_save(self):
        reviewer = self._reviewer(
            [REQUEST_TEXT],
            {
                "save_target": "user",
                "entries": [{"action": "add", "content": ACTION_TEXT}],
            },
        )
        await reviewer._run_review()
        self.assertEqual(self._user_file().strip(), "")

    async def test_preference_shaped_input_is_saved(self):
        reviewer = self._reviewer(
            [PREFERENCE_TEXT],
            {
                "save_target": "user",
                "entries": [{"action": "add", "content": PREFERENCE_TEXT}],
            },
        )
        await reviewer._run_review()
        self.assertIn(PREFERENCE_TEXT, self._user_file())


class DuplicatePreferenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-memdup-"))
        self.path = self.tmp / "USER.md"
        self.store = memory.MemoryStore(self.path, char_limit=1375)

    async def test_duplicate_preference_not_appended_twice(self):
        first = await self.store.add(PREFERENCE_TEXT)
        second = await self.store.add(PREFERENCE_TEXT)

        self.assertIn("Added", first)
        self.assertIn("Duplicate", second)
        self.assertEqual(
            self.path.read_text(encoding="utf-8").count(PREFERENCE_TEXT), 1
        )

    async def test_rephrased_preference_not_appended_twice(self):
        await self.store.add("I prefer dark mode")

        result = await self.store.add("I prefer dark mode.")

        self.assertIn("Near-duplicate", result)
        self.assertEqual(len(self.store.entry_list()), 1)


class CapMergeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-memcap-"))
        self.store = memory.MemoryStore(
            self.tmp / "MEMORY.md", char_limit=200, cap_ratio=0.25
        )

    async def test_cap_merges_instead_of_growing(self):
        await self.store.add("My favorite color is blue")
        await self.store.add("Penguins live in Antarctica")
        before = self.store.entry_list()
        self.assertEqual(len(before), 2)

        result = await self.store.add("My favorite color is navy blue")

        self.assertIn("Merged", result)
        after = self.store.entry_list()
        self.assertEqual(len(after), len(before))
        self.assertTrue(any("navy" in entry for entry in after))
        self.assertLessEqual(
            self.store._entries_char_count(after), self.store.char_limit
        )


class SuggestionListTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-memsug-"))
        self.path = self.tmp / "MEMORY.md"
        self.meta_path = self.tmp / "memory_meta.json"
        self.store = memory.MemoryStore(self.path, char_limit=2200)

    async def test_suggestions_are_read_only_and_identify_stale(self):
        await self.store.add("Old fact about llamas")
        await self.store.add("Fresh fact about penguins")
        stale_key = memory_meta.entry_key("Old fact about llamas")
        data = json.loads(self.meta_path.read_text(encoding="utf-8"))
        data[stale_key]["first_seen"] = time.time() - 400 * 86400
        self.meta_path.write_text(json.dumps(data), encoding="utf-8")
        before_file = self.path.read_text(encoding="utf-8")
        before_meta = self.meta_path.read_text(encoding="utf-8")

        suggestions = memory.suggest_stale_entries(self.store)

        texts = [item["text"] for item in suggestions]
        self.assertIn("Old fact about llamas", texts)
        self.assertNotIn("Fresh fact about penguins", texts)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before_file)
        self.assertEqual(self.meta_path.read_text(encoding="utf-8"), before_meta)


if __name__ == "__main__":
    unittest.main(verbosity=2)
