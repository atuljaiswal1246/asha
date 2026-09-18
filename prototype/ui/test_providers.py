"""Tests for the BYOK provider layer: custom OpenAI-compatible endpoints."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import providers.registry as reg  # noqa: E402
from providers.runtime import CustomProviders  # noqa: E402


class CustomProvidersTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.store = CustomProviders(self.dir / "providers.json")

    def test_add_get_remove(self):
        pub = self.store.add("My Endpoint", "https://api.example.com/v1",
                             "sk-secret", ["m-a", "m-b"])
        self.assertEqual(pub["provider_id"], "custom:my-endpoint")
        self.assertTrue(pub["configured"])
        self.assertNotIn("api_key", pub)                 # never exposed
        self.assertEqual(self.store.get("my-endpoint")["api_key"], "sk-secret")
        self.assertEqual(len(self.store.list()), 1)
        self.assertTrue(self.store.remove("my-endpoint"))
        self.assertEqual(self.store.list(), [])

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.store.add("", "https://x/v1")
        with self.assertRaises(ValueError):
            self.store.add("n", "")
        self.store.add("dup", "https://x/v1")
        with self.assertRaises(ValueError):
            self.store.add("dup", "https://y/v1")

    def test_persists_across_instances(self):
        self.store.add("p", "https://x/v1", "k", ["m"])
        again = CustomProviders(self.dir / "providers.json")
        self.assertEqual(again.get("p")["base_url"], "https://x/v1")

    def test_corrupt_file_is_empty(self):
        (self.dir / "broken.json").write_text("{not json")
        self.assertEqual(CustomProviders(self.dir / "broken.json").list(), [])


class RegistryCustomTests(unittest.TestCase):
    def setUp(self):
        self._orig = reg._CUSTOM
        reg._CUSTOM = CustomProviders(Path(tempfile.mkdtemp()) / "providers.json")
        reg.reset_cache()

    def tearDown(self):
        reg._CUSTOM = self._orig
        reg.reset_cache()

    def test_custom_provider_builds_and_lists(self):
        reg._CUSTOM.add("Acme", "https://acme.ai/v1", "sk-1", ["acme-large"])
        reg.reset_cache()
        p = reg.get_provider("custom:acme")
        self.assertTrue(p.is_configured())
        self.assertEqual(p.base_url, "https://acme.ai/v1")
        self.assertEqual([m.id for m in p.models()], ["acme-large"])
        ids = [x["provider_id"] for x in reg.list_providers()]
        self.assertIn("custom:acme", ids)

    def test_unknown_custom_raises(self):
        with self.assertRaises(ValueError):
            reg.get_provider("custom:missing")

    def test_unconfigured_without_key(self):
        reg._CUSTOM.add("NoKey", "https://x/v1", "", [])
        reg.reset_cache()
        self.assertFalse(reg.get_provider("custom:nokey").is_configured())


if __name__ == "__main__":
    unittest.main(verbosity=2)
