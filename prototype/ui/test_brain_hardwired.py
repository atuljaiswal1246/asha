"""Guards for the hardwired brain (user decision 2026-09-17).

Jarvis runs on exactly one model — DeepSeek V4.1 Flash on the paid go
gateway — and the user has no way to select a different one. These tests lock
in what must hold:

* one constant (``server.BRAIN_MODEL_ID`` / ``server.BRAIN_BASE_URL``) is the
  source of truth for both the talking LLM and the MemoryReviewer;
* ``brain_model_set`` (still accepted for older clients) cannot change either;
* ``brain-pref.json`` cannot override the constant (stale values are ignored);
* a missing provider fails plainly instead of silently running something else;
* the served page offers no model choice (static-asset pattern, cf.
  ``test_ui_hidden.py``).

Run with:

    ../../.venv/bin/python -m unittest test_brain_hardwired -q
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

UI_DIR = Path(__file__).resolve().parent
STATIC = UI_DIR / "static"

import server  # noqa: E402


def _seed_model_cache():
    """Keep LLM construction off the network/shell: a fixed one-item model
    list so ``_oc_models_cached`` never shells out to the opencode CLI."""
    saved = dict(server._OC_MODELS_CACHE)
    server._OC_MODELS_CACHE.update({
        "ts": time.monotonic(),
        "items": [{"id": server.BRAIN_MODEL_ID, "tier": "go",
                   "name": "DeepSeek V4.1 Flash", "free": False}],
    })
    return saved


def _make_llm(**kwargs):
    saved = _seed_model_cache()
    try:
        llm = server.BoundedContextLLM(
            api_key="test-key", base_url="http://127.0.0.1:1/v1", **kwargs
        )
    finally:
        server._OC_MODELS_CACHE.update(saved)
    llm._fallback_enabled = False
    return llm


class HardwiredConstantTests(unittest.TestCase):
    def test_constant_is_the_fixed_model(self):
        self.assertEqual(
            server.BRAIN_MODEL_ID,
            os.environ.get("JARVIS_BRAIN_MODEL", "deepseek-v4.1-flash"),
        )
        if "JARVIS_BRAIN_MODEL" not in os.environ:
            self.assertEqual(server.BRAIN_MODEL_ID, "deepseek-v4.1-flash")

    def test_constant_is_the_go_gateway(self):
        self.assertEqual(
            server.BRAIN_BASE_URL,
            os.environ.get(
                "JARVIS_BRAIN_BASE_URL", "https://opencode.ai/zen/go/v1"
            ),
        )
        if "JARVIS_BRAIN_BASE_URL" not in os.environ:
            self.assertEqual(
                server.BRAIN_BASE_URL, "https://opencode.ai/zen/go/v1"
            )

    def test_default_model_is_the_constant(self):
        self.assertEqual(server._default_model(), server.BRAIN_MODEL_ID)


class LlmHardwiredTests(unittest.TestCase):
    def test_llm_starts_on_the_constant_with_no_selection(self):
        llm = _make_llm()
        self.assertEqual(llm._selected_model, server.BRAIN_MODEL_ID)

    def test_set_brain_model_cannot_change_the_model(self):
        llm = _make_llm()
        for attempt in ("mimo-v2.5-free", "big-pickle", "gpt-5", None, ""):
            llm.set_brain_model(attempt)
            self.assertEqual(
                llm._selected_model, server.BRAIN_MODEL_ID,
                f"set_brain_model({attempt!r}) changed the brain",
            )

    def test_rebuild_providers_reasserts_the_constant(self):
        llm = _make_llm()
        llm._selected_model = "something-else"  # simulate stale state
        saved = _seed_model_cache()  # stay hermetic: no CLI shell-out
        try:
            llm.rebuild_providers()
        finally:
            server._OC_MODELS_CACHE.update(saved)
        self.assertEqual(llm._selected_model, server.BRAIN_MODEL_ID)


class ReviewerHardwiredTests(unittest.TestCase):
    def test_reviewer_settings_come_from_the_one_constant(self):
        base_url, model, _key = server._reviewer_settings()
        self.assertEqual(model, server.BRAIN_MODEL_ID)
        self.assertEqual(base_url, server.BRAIN_BASE_URL)
        # Same value the LLM resolves from — one source of truth.
        llm = _make_llm()
        self.assertEqual(model, llm._selected_model)

    def test_no_separate_memory_model_override(self):
        src = (UI_DIR / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("MEMORY_REVIEW_MODEL", src)
        self.assertNotIn("MEMORY_REVIEW_BASE_URL", src)

    def test_brain_model_set_handler_ignores_the_request(self):
        # on_brain_model_set lives inside run_prototype's closure, so prove
        # the wiring by source: it re-asserts the constant and the old
        # accept-any-valid-model line is gone.
        src = (UI_DIR / "server.py").read_text(encoding="utf-8")
        self.assertIn('pref["model"] = BRAIN_MODEL_ID', src)
        self.assertNotIn("wanted if wanted in valid", src)
        self.assertIn("ignored (hardwired", src)


class BrainPrefTests(unittest.TestCase):
    def test_stale_pref_file_cannot_override_the_constant(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "brain-pref.json"
            fake.write_text(json.dumps({
                "model": "mimo-v2.5-free",  # stale picker-era value
                "reasoning": "medium",
                "onboarded": True,
            }), encoding="utf-8")
            saved = server.BRAIN_PREF_PATH
            server.BRAIN_PREF_PATH = fake
            try:
                pref = server._load_brain_pref()
            finally:
                server.BRAIN_PREF_PATH = saved
        self.assertEqual(pref["model"], server.BRAIN_MODEL_ID)
        # Non-model fields still load from the file.
        self.assertEqual(pref["reasoning"], "medium")
        self.assertTrue(pref["onboarded"])

    def test_save_forces_the_constant(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "brain-pref.json"
            saved = server.BRAIN_PREF_PATH
            server.BRAIN_PREF_PATH = fake
            try:
                server._save_brain_pref({
                    "model": "big-pickle",  # must not survive the write
                    "reasoning": "low",
                    "onboarded": True,
                })
                written = json.loads(fake.read_text(encoding="utf-8"))
            finally:
                server.BRAIN_PREF_PATH = saved
        self.assertEqual(written["model"], server.BRAIN_MODEL_ID)
        self.assertEqual(written["reasoning"], "low")


class NoSilentFallbackTests(unittest.TestCase):
    def test_missing_provider_is_plainly_unavailable(self):
        seen: list = []

        async def _emit(frame):
            seen.append(frame)

        llm = _make_llm(emit_ui_cb=_emit)
        llm._providers = {}  # e.g. no API key — the fixed model has nowhere to run
        self.assertIsNone(llm._selected_provider("text"))
        asyncio.run(llm._emit_brain_error(None))
        self.assertEqual(len(seen), 1)
        msg = getattr(seen[0], "message", "")
        self.assertIn("DeepSeek", msg)
        self.assertNotIn("pick", msg.lower())

    def test_rate_limit_message_names_no_alternative(self):
        seen: list = []

        async def _emit(frame):
            seen.append(frame)

        llm = _make_llm(emit_ui_cb=_emit)

        class _RateLimit(Exception):
            pass

        # Flag it the way _is_rate_limit detects (429), without importing
        # provider internals: piggyback on the real helper's input shape.
        err = _RateLimit("429 rate limited")
        err.status_code = 429
        asyncio.run(llm._emit_brain_error(err))
        self.assertEqual(len(seen), 1)
        msg = getattr(seen[0], "message", "")
        self.assertNotIn("pick", msg.lower())


class NoModelChoiceInPageTests(unittest.TestCase):
    """The served page offers no model choice (static-asset pattern)."""

    def setUp(self):
        self.html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.js = (STATIC / "app.js").read_text(encoding="utf-8")

    def test_brain_trigger_is_read_only_text_not_a_button(self):
        self.assertRegex(self.html, r'<span[^>]*id="brainModelBtn"')
        self.assertNotRegex(self.html, r'<button[^>]*id="brainModelBtn"')

    def test_settings_has_no_model_change_button(self):
        self.assertNotIn("setModelBtn", self.html)
        self.assertNotIn("setModelBtn", self.js)

    def test_ui_never_sends_a_model_choice(self):
        self.assertNotIn("brain_model_set", self.js)

    def test_no_pick_a_model_copy(self):
        self.assertNotIn("Pick a model", self.js)
        self.assertNotIn("chooses the", self.js)

    def test_fixed_model_is_named(self):
        self.assertIn("DeepSeek V4.1 Flash", self.html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
