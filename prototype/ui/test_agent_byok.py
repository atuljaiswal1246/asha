"""Tests for the agent BYOK path: the USER's own key for coding delegation.

No live network. Every store points at a temp dir; the provider env slots are
cleared so a real key in the environment can never affect (or be affected by)
these tests. The core assertion is that the agent resolves the user's stored
key, never a server-side env key, and that the key is never logged or echoed.
"""
from __future__ import annotations

import logging
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import connector_setup  # noqa: E402
import worker_engine as we  # noqa: E402

SECRET = "sk-or-v1-super-secret-agent-key-0000"
SECRET_OC = "oc-secret-agent-key-1111"
OUR_ENV_KEY = "SERVER-SIDE-KEY-MUST-NOT-BE-USED"

_ENV_SLOTS = ("OPENROUTER_API_KEY", "OPENCODE_API_KEY", "SUPERVISOR_API_KEY")


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.append(record.getMessage())
        except Exception:  # noqa: BLE001
            self.messages.append("")


class AgentByokTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="jarvis-byok-"))
        self.store = connector_setup.KeyStore(self.dir / "agent_keys.json")
        self._saved = {name: os.environ.pop(name, None) for name in _ENV_SLOTS}

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _capture(self):
        cap = _Capture()
        logging.getLogger("asha.worker").addHandler(cap)
        return cap

    def _uncapture(self, cap):
        logging.getLogger("asha.worker").removeHandler(cap)

    # ── resolution ────────────────────────────────────────────────────────────
    def test_default_is_openrouter_with_its_base_url_and_model(self):
        r = we.resolve_agent_provider(store_=self.store)
        self.assertEqual(r["provider"], "openrouter")
        self.assertEqual(r["base_url"], "https://openrouter.ai/api/v1")
        self.assertEqual(r["model"], "cohere/north-mini-code:free")
        self.assertFalse(r["configured"])
        self.assertIn("No OpenRouter key", r["message"])

    def test_opencode_choice_uses_go_base_and_model(self):
        we.set_agent_config(provider="opencode", store_=self.store)
        r = we.resolve_agent_provider(store_=self.store)
        self.assertEqual(r["provider"], "opencode")
        self.assertEqual(r["base_url"], we.GO_BASE)
        self.assertEqual(r["model"], "mimo-v2.5")

    def test_explicit_arguments_override_stored_config(self):
        we.set_agent_config(provider="opencode", store_=self.store)
        r = we.resolve_agent_provider(provider="openrouter",
                                      model="openai/gpt-4o-mini",
                                      store_=self.store)
        self.assertEqual(r["provider"], "openrouter")
        self.assertEqual(r["model"], "openai/gpt-4o-mini")
        self.assertTrue(r["model_overridden"])

    def test_stored_model_override(self):
        we.set_agent_config(provider="openrouter",
                            model="anthropic/claude-sonnet-4", store_=self.store)
        r = we.resolve_agent_provider(store_=self.store)
        self.assertEqual(r["model"], "anthropic/claude-sonnet-4")
        self.assertTrue(r["model_overridden"])

    def test_unknown_provider_is_named_and_rejected(self):
        with self.assertRaises(we.WorkerEngineError) as ctx:
            we.set_agent_config(provider="bogus", store_=self.store)
        self.assertIn("bogus", str(ctx.exception))
        with self.assertRaises(we.WorkerEngineError):
            we.resolve_agent_provider(provider="bogus", store_=self.store)

    # ── missing key ───────────────────────────────────────────────────────────
    def test_missing_key_message_is_one_plain_sentence_per_provider(self):
        self.assertEqual(
            we.agent_not_configured_message("openrouter"),
            "No OpenRouter key is configured for the agent. Add your own "
            "OpenRouter API key in Settings to use coding.")
        self.assertEqual(
            we.agent_not_configured_message("opencode"),
            "No OpenCode key is configured for the agent. Add your own "
            "OpenCode API key in Settings to use coding.")
        self.assertNotIn("\n", we.agent_not_configured_message("openrouter"))

    def test_dispatch_fails_honestly_without_a_user_key(self):
        original = we.agent_key_store
        we.agent_key_store = lambda: self.store
        try:
            with self.assertRaises(we.WorkerEngineError) as ctx:
                we.dispatch("do a thing")
            self.assertIn("No OpenRouter key", str(ctx.exception))
        finally:
            we.agent_key_store = original

    # ── store ─────────────────────────────────────────────────────────────────
    def test_saved_key_is_configured_and_store_is_0600(self):
        we.save_agent_key("openrouter", SECRET, store_=self.store)
        r = we.resolve_agent_provider(store_=self.store)
        self.assertTrue(r["configured"])
        self.assertEqual(r["api_key"], SECRET)
        mode = stat.S_IMODE(os.stat(self.store.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_clear_key_returns_to_unconfigured(self):
        we.save_agent_key("openrouter", SECRET, store_=self.store)
        self.assertTrue(we.clear_agent_key("openrouter", store_=self.store))
        self.assertFalse(we.resolve_agent_provider(store_=self.store)["configured"])

    def test_empty_key_is_refused_without_echoing_anything(self):
        with self.assertRaises(we.WorkerEngineError) as ctx:
            we.save_agent_key("openrouter", "   ", store_=self.store)
        self.assertNotIn(SECRET, str(ctx.exception))

    # ── the key is never logged or exposed in errors ──────────────────────────
    def test_key_never_appears_in_logs_or_error_paths(self):
        we.save_agent_key("openrouter", SECRET, store_=self.store)
        cap = self._capture()
        try:
            r = we.resolve_agent_provider(store_=self.store)
            we.build_agent_provider(r)
            pid = we.register_agent_provider(r)
            try:
                we.resolve_agent_provider(provider="bogus", store_=self.store)
            except we.WorkerEngineError as exc:
                self.assertNotIn(SECRET, str(exc))
            try:
                we.save_agent_key("opencode", "", store_=self.store)
            except we.WorkerEngineError as exc:
                self.assertNotIn(SECRET, str(exc))
            self.assertNotIn(SECRET, r.get("message", ""))
            self.assertTrue(pid)
        finally:
            self._uncapture(cap)
        self.assertFalse(any(SECRET in m for m in cap.messages),
                         f"key leaked into logs: {cap.messages}")
        self.assertNotIn(SECRET, repr(self.store.all()))

    # ── our keys are never used for agent traffic ─────────────────────────────
    def test_registered_openrouter_provider_uses_the_stored_user_key(self):
        os.environ["OPENROUTER_API_KEY"] = OUR_ENV_KEY
        we.save_agent_key("openrouter", SECRET, store_=self.store)
        r = we.resolve_agent_provider(store_=self.store)
        self.assertEqual(r["api_key"], SECRET)
        pid = we.register_agent_provider(r)
        try:
            p = we._get_provider(pid)
            self.assertEqual(p.api_key, SECRET)
            self.assertEqual(p.base_url, r["base_url"])
            self.assertNotEqual(p.api_key, OUR_ENV_KEY)
        finally:
            we._provider_registry._INSTANCES.pop(pid, None)

    def test_registered_opencode_provider_ignores_server_go_key(self):
        os.environ["SUPERVISOR_API_KEY"] = OUR_ENV_KEY
        we.set_agent_config(provider="opencode", store_=self.store)
        we.save_agent_key("opencode", SECRET_OC, store_=self.store)
        r = we.resolve_agent_provider(store_=self.store)
        pid = we.register_agent_provider(r)
        try:
            p = we._get_provider(pid)
            self.assertEqual(p.api_key, SECRET_OC)
            self.assertEqual(p.base_url, we.GO_BASE)
        finally:
            we._provider_registry._INSTANCES.pop(pid, None)
        self.assertEqual(os.environ["SUPERVISOR_API_KEY"], OUR_ENV_KEY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
