"""Guards for the env-driven brain transport (user decision 2026-09-18).

The packaged app must NOT default to a personal OpenCode subscription
endpoint. The brain's endpoint/credentials are a first-class, env-driven
choice; the OpenCode endpoint is a dev-only fallback that is never selected
when a product setting is present. These tests lock in:

* the exact resolution order (product setting wins over the dev fallback);
* the packaged default is the local OmniRoute gateway;
* the DeepSeek-direct option works without any OpenCode key;
* absence of keys never crashes, and never flips a product choice;
* the startup log names the transport and base host and never the key.

Run with:

    ../../.venv/bin/python -m unittest test_brain_transport -q
"""
from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402


class ResolveBrainTransportTests(unittest.TestCase):
    def test_dev_default_is_the_opencode_fallback(self):
        info = server._resolve_brain_transport({})
        self.assertEqual(info["transport"], "opencode")
        self.assertEqual(info["base_url"], "https://opencode.ai/zen/go/v1")
        self.assertEqual(info["model"], "deepseek-v4.1-flash")

    def test_packaged_default_is_local_omniroute(self):
        info = server._resolve_brain_transport({"JARVIS_DATA_DIR": "/tmp/jarvis"})
        self.assertEqual(info["transport"], "omniroute")
        self.assertEqual(info["base_url"], "http://127.0.0.1:20128/v1")
        self.assertEqual(info["source"], "auto:packaged")

    def test_omniroute_base_url_is_configurable(self):
        info = server._resolve_brain_transport({
            "JARVIS_DATA_DIR": "/tmp/jarvis",
            "OMNIROUTE_BASE_URL": "http://127.0.0.1:9999/v1",
        })
        self.assertEqual(info["transport"], "omniroute")
        self.assertEqual(info["base_url"], "http://127.0.0.1:9999/v1")

    def test_product_transport_wins_over_dev_fallback(self):
        # A product setting must beat the dev OpenCode fallback even when an
        # OpenCode key is present.
        info = server._resolve_brain_transport({
            "JARVIS_BRAIN_TRANSPORT": "omniroute",
            "OPENCODE_API_KEY": "sk-oc-dev",
            "SUPERVISOR_API_KEY": "sk-sup-dev",
        })
        self.assertEqual(info["transport"], "omniroute")
        self.assertNotEqual(info["base_url"], "https://opencode.ai/zen/go/v1")

    def test_explicit_base_url_wins_over_everything(self):
        info = server._resolve_brain_transport({
            "JARVIS_BRAIN_BASE_URL": "https://brain.example.com/v1",
            "JARVIS_BRAIN_TRANSPORT": "omniroute",
            "JARVIS_DATA_DIR": "/tmp/jarvis",
            "OPENCODE_API_KEY": "sk-oc-dev",
        })
        self.assertEqual(info["transport"], "custom")
        self.assertEqual(info["base_url"], "https://brain.example.com/v1")
        self.assertEqual(info["source"], "JARVIS_BRAIN_BASE_URL")

    def test_explicit_base_url_key_prefers_dedicated_var(self):
        info = server._resolve_brain_transport({
            "JARVIS_BRAIN_BASE_URL": "https://brain.example.com/v1",
            "JARVIS_BRAIN_API_KEY": "sk-product",
            "OPENCODE_API_KEY": "sk-oc-dev",
        })
        self.assertEqual(info["api_key"], "sk-product")
        self.assertEqual(info["key_env"], "JARVIS_BRAIN_API_KEY")

    def test_explicit_base_url_falls_back_to_legacy_keys(self):
        info = server._resolve_brain_transport({
            "JARVIS_BRAIN_BASE_URL": "https://brain.example.com/v1",
            "SUPERVISOR_API_KEY": "sk-sup-dev",
            "OPENCODE_API_KEY": "sk-oc-dev",
        })
        self.assertEqual(info["api_key"], "sk-sup-dev")
        self.assertEqual(info["key_env"], "SUPERVISOR_API_KEY")

    def test_deepseek_direct_path(self):
        info = server._resolve_brain_transport({
            "JARVIS_BRAIN_TRANSPORT": "deepseek",
            "DEEPSEEK_API_KEY": "sk-deepseek",
        })
        self.assertEqual(info["transport"], "deepseek")
        self.assertEqual(info["base_url"], "https://api.deepseek.com")
        self.assertEqual(info["key_env"], "DEEPSEEK_API_KEY")

    def test_deepseek_base_url_is_configurable(self):
        info = server._resolve_brain_transport({
            "JARVIS_BRAIN_TRANSPORT": "deepseek",
            "DEEPSEEK_BASE_URL": "https://deepseek.example.com",
        })
        self.assertEqual(info["base_url"], "https://deepseek.example.com")

    def test_opencode_selector_is_the_dev_path(self):
        info = server._resolve_brain_transport({
            "JARVIS_BRAIN_TRANSPORT": "opencode",
            "OPENCODE_API_KEY": "sk-oc-dev",
        })
        self.assertEqual(info["transport"], "opencode")
        self.assertEqual(info["base_url"], "https://opencode.ai/zen/go/v1")

    def test_missing_keys_never_crash(self):
        for env in ({}, {"JARVIS_BRAIN_TRANSPORT": "deepseek"},
                    {"JARVIS_BRAIN_TRANSPORT": "omniroute"},
                    {"JARVIS_DATA_DIR": "/tmp/jarvis"}):
            info = server._resolve_brain_transport(env)
            self.assertIsInstance(info["base_url"], str)
            self.assertTrue(info["base_url"])
            self.assertIsInstance(info["api_key"], str)

    def test_model_override_is_preserved(self):
        info = server._resolve_brain_transport({"JARVIS_BRAIN_MODEL": "custom-model"})
        self.assertEqual(info["model"], "custom-model")

    def test_constants_are_the_resolved_dev_values(self):
        self.assertEqual(server.BRAIN_MODEL_ID, "deepseek-v4.1-flash")
        self.assertEqual(server.BRAIN_TRANSPORT, "opencode")
        self.assertEqual(server.BRAIN_BASE_URL, "https://opencode.ai/zen/go/v1")


class TransportLogTests(unittest.TestCase):
    def test_log_names_transport_host_and_key_env_without_the_key(self):
        secret = "sk-super-secret-value-123"
        info = {
            "transport": "deepseek",
            "base_url": "https://api.deepseek.com",
            "api_key": secret,
            "key_env": "DEEPSEEK_API_KEY",
            "model": "deepseek-v4.1-flash",
        }
        with self.assertLogs("asha.server", level="INFO") as cap:
            server._log_brain_transport(info, force=True)
        line = "\n".join(cap.output)
        self.assertIn("transport=deepseek", line)
        self.assertIn("host=api.deepseek.com", line)
        self.assertIn("key_env=DEEPSEEK_API_KEY", line)
        self.assertNotIn(secret, line)

    def test_log_says_none_when_no_key_supplied(self):
        info = {
            "transport": "omniroute",
            "base_url": "http://127.0.0.1:20128/v1",
            "api_key": "",
            "key_env": "",
            "model": "deepseek-v4.1-flash",
        }
        with self.assertLogs("asha.server", level="INFO") as cap:
            server._log_brain_transport(info, force=True)
        self.assertIn("key_env=(none)", "\n".join(cap.output))

    def test_source_guards(self):
        from pathlib import Path
        src = Path(__file__).resolve().parent / "server.py"
        text = src.read_text(encoding="utf-8")
        # The documented compression off-switch for the OmniRoute path.
        self.assertIn('"x-omniroute-compression": "off"', text)
        # The startup log line exists and never interpolates a key value.
        self.assertIn('"[LLM] brain transport: transport=%s base=%s host=%s',
                      text)


class LlmTransportWiringTests(unittest.TestCase):
    def _seed_model_cache(self):
        saved = dict(server._OC_MODELS_CACHE)
        server._OC_MODELS_CACHE.update({
            "ts": time.monotonic(),
            "items": [{"id": server.BRAIN_MODEL_ID, "tier": "go",
                       "name": "DeepSeek V4.1 Flash", "free": False}],
        })
        return saved

    def _build_with_transport(self, transport, base_url, api_key):
        saved = (server.BRAIN_TRANSPORT, server.BRAIN_BASE_URL,
                 server.BRAIN_API_KEY)
        server.BRAIN_TRANSPORT, server.BRAIN_BASE_URL, server.BRAIN_API_KEY = (
            transport, base_url, api_key)
        cache = self._seed_model_cache()
        try:
            llm = server.BoundedContextLLM(
                api_key="test-key", base_url="http://127.0.0.1:1/v1")
        finally:
            server._OC_MODELS_CACHE.update(cache)
            (server.BRAIN_TRANSPORT, server.BRAIN_BASE_URL,
             server.BRAIN_API_KEY) = saved
        llm._fallback_enabled = False
        return llm

    def test_brain_provider_points_at_deepseek_without_opencode(self):
        llm = self._build_with_transport(
            "deepseek", "https://api.deepseek.com", "sk-deepseek")
        prov = llm._providers.get(server.BRAIN_MODEL_ID)
        self.assertIsNotNone(prov, "brain provider missing for DeepSeek")
        self.assertEqual(prov.base_url, "https://api.deepseek.com")

    def test_brain_provider_disables_omniroute_compression(self):
        llm = self._build_with_transport(
            "omniroute", "http://127.0.0.1:20128/v1", "")
        prov = llm._providers.get(server.BRAIN_MODEL_ID)
        self.assertIsNotNone(prov, "brain provider missing for OmniRoute")
        self.assertEqual(prov.base_url, "http://127.0.0.1:20128/v1")
        headers = dict(getattr(prov.client, "default_headers", {}) or {})
        self.assertEqual(headers.get("x-omniroute-compression"), "off")

    def test_absent_keys_do_not_crash_construction(self):
        saved_oc = os.environ.pop("OPENCODE_API_KEY", None)
        saved_sup = os.environ.pop("SUPERVISOR_API_KEY", None)
        saved = (server.BRAIN_TRANSPORT, server.BRAIN_BASE_URL,
                 server.BRAIN_API_KEY)
        server.BRAIN_TRANSPORT, server.BRAIN_BASE_URL, server.BRAIN_API_KEY = (
            "opencode", "https://opencode.ai/zen/go/v1", "")
        try:
            llm = server.BoundedContextLLM(
                api_key="test-key", base_url="http://127.0.0.1:1/v1")
        finally:
            if saved_oc is not None:
                os.environ["OPENCODE_API_KEY"] = saved_oc
            if saved_sup is not None:
                os.environ["SUPERVISOR_API_KEY"] = saved_sup
            (server.BRAIN_TRANSPORT, server.BRAIN_BASE_URL,
             server.BRAIN_API_KEY) = saved
        # No key in dev must not conjure a provider (unchanged behaviour).
        self.assertIsNone(llm._providers.get(server.BRAIN_MODEL_ID))


if __name__ == "__main__":
    unittest.main(verbosity=2)
