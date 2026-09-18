"""Guards for the hardwired reasoning effort (user decision 2026-09-17).

The reasoning effort is always the model default — the field is omitted from
every model call so the model decides what's best for it. There is no user
control and no pref/env override. These tests lock in what must hold:

* one constant (``server.HARDWIRED_REASONING == "default"``) plus
  ``server._reasoning_extra() == {}`` is the only wiring;
* an LLM built the way startup builds it carries no effort override;
* a stale ``brain-pref.json`` reasoning value cannot re-enable one, and the
  startup path ignores both the pref and ``REASONING_EFFORT`` from .env;
* ``reasoning_set`` (still accepted for older clients) cannot change the
  model call — the work path still writes no effort. The one exception is the
  user-approved conversational-turn shortcut (2026-09-18), whose thinking-off
  switch is the only place ``reasoning_effort`` appears in server.py;
* the served page offers no reasoning choice (static-asset pattern, cf.
  ``test_ui_hidden.py``).

Run with:

    ../../.venv/bin/python -m unittest test_reasoning_fixed -q
"""
from __future__ import annotations

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
    def test_constant_is_the_default(self):
        self.assertEqual(server.HARDWIRED_REASONING, "default")

    def test_extra_is_empty_field_omitted(self):
        self.assertEqual(server._reasoning_extra(), {})
        self.assertNotIn("reasoning_effort",
                         json.dumps(server._reasoning_extra()))


class ModelCallCarriesNoEffortTests(unittest.TestCase):
    def test_startup_wiring_carries_no_effort(self):
        """Build the LLM exactly the way startup does and prove the model
        call carries no effort override."""
        from pipecat.services.openai.llm import OpenAILLMService
        llm = _make_llm(settings=OpenAILLMService.Settings(
            extra=server._reasoning_extra(),
        ))
        self.assertEqual(llm._settings.extra, {})
        self.assertNotIn("reasoning_effort",
                         json.dumps(llm._settings.extra))

    def test_stale_pref_cannot_reenable_an_effort(self):
        """A stale brain-pref.json reasoning value loads (backward compat)
        but the effective wiring still omits the field."""
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "brain-pref.json"
            fake.write_text(json.dumps({
                "model": server.BRAIN_MODEL_ID,
                "reasoning": "high",  # stale: must never take effect
                "onboarded": True,
            }), encoding="utf-8")
            saved = server.BRAIN_PREF_PATH
            server.BRAIN_PREF_PATH = fake
            try:
                pref = server._load_brain_pref()
            finally:
                server.BRAIN_PREF_PATH = saved
        self.assertEqual(pref["reasoning"], "high")
        # ...yet startup ignores it: the extra is always the hardwired one.
        self.assertEqual(server._reasoning_extra(), {})

    def test_startup_ignores_pref_and_env(self):
        src = (UI_DIR / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("pref_reasoning", src)
        self.assertNotIn('os.environ.get("REASONING_EFFORT"', src)


class ReasoningSetIgnoredTests(unittest.TestCase):
    def test_handler_is_accept_and_ignore(self):
        src = (UI_DIR / "server.py").read_text(encoding="utf-8")
        self.assertIn("reasoning_set ignored (hardwired", src)
        self.assertIn("reasoning_set_cb=on_reasoning_set", src)

    def test_effort_literal_only_in_conversational_shortcut(self):
        """The wire field may appear ONLY in the approved conversational
        shortcut's off-switch — the work path still writes no effort, so a
        work turn cannot carry what the normal code never writes."""
        src = (UI_DIR / "server.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("reasoning_effort"), 1)
        self.assertIn('"extra_body": {"reasoning_effort": "none"}', src)
        self.assertEqual(server._reasoning_extra(), {})


class NoReasoningChoiceInPageTests(unittest.TestCase):
    """The served page offers no reasoning choice (static-asset pattern)."""

    def setUp(self):
        self.html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.js = (STATIC / "app.js").read_text(encoding="utf-8")
        self.css = (STATIC / "styles.css").read_text(encoding="utf-8")

    def test_no_reason_picker_or_trigger(self):
        self.assertNotIn('id="reasonPicker"', self.html)
        self.assertNotIn('id="brainReasonBtn"', self.html)
        self.assertNotIn("brainReasonBtnName", self.html)

    def test_ui_never_sends_a_reasoning_choice(self):
        self.assertNotIn("reasoning_set", self.js)
        self.assertNotIn("_REASON_OPTIONS", self.js)

    def test_no_reasoning_styles(self):
        self.assertNotIn(".reason-picker", self.css)
        self.assertNotIn(".rp-item", self.css)


if __name__ == "__main__":
    unittest.main(verbosity=2)
