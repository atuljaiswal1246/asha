"""Regression guards: startup must LOAD brain-pref.json before anything writes it.

Bug: ``run()`` used to write the fresh-process ``brain_pref_holder`` default
(``onboarded: False``) to disk BEFORE ``_load_brain_pref()`` ever ran, wiping
the persisted ``onboarded: true`` on every start. The later load then read the
wiped file, so every boot re-ran first-time onboarding (mic notice) and the
connect handler deferred the greeting
(``_greeted[0] = bool(brain_pref_holder.get("onboarded"))``).

These tests lock in the fixed order:

* a boot with ``onboarded: true`` on disk leaves it true (no wipe) and does
  not defer the greeting;
* a boot with no ``onboarded`` key still onboards (greeting deferred);
* the hardwired model/reasoning still cannot be overridden by a stale file.

Run with:

    ../../.venv/bin/python -m unittest test_brain_pref -q
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402


def _fresh_process_holder():
    """Simulate a fresh process: holder back at import-time defaults."""
    saved = dict(server.brain_pref_holder)
    server.brain_pref_holder.clear()
    server.brain_pref_holder.update(dict(server._DEFAULT_BRAIN_PREF))
    return saved


def _restore_holder(saved):
    server.brain_pref_holder.clear()
    server.brain_pref_holder.update(saved)


class StartupOrderTests(unittest.TestCase):
    def test_startup_helper_loads_before_it_writes(self):
        src = inspect.getsource(server._startup_load_and_assert_pref)
        self.assertIn("_load_brain_pref()", src)
        self.assertIn("_save_brain_pref(", src)
        self.assertLess(
            src.index("_load_brain_pref()"), src.index("_save_brain_pref("),
            "startup must load the persisted pref before any write",
        )

    def test_run_uses_the_load_first_helper(self):
        src = inspect.getsource(server.run)
        self.assertIn("_startup_load_and_assert_pref()", src)


class OnboardedBootTests(unittest.TestCase):
    def test_boot_with_onboarded_true_does_not_wipe_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "brain-pref.json"
            fake.write_text(json.dumps({
                "model": "deepseek-v4.1-flash",
                "onboarded": True,
                "reasoning": "default",
            }), encoding="utf-8")
            saved_path = server.BRAIN_PREF_PATH
            saved_holder = _fresh_process_holder()
            server.BRAIN_PREF_PATH = fake
            try:
                # Fresh process: holder is still the default (onboarded False).
                self.assertFalse(server.brain_pref_holder.get("onboarded"))
                server._startup_load_and_assert_pref()
                after = json.loads(fake.read_text(encoding="utf-8"))
            finally:
                server.BRAIN_PREF_PATH = saved_path
                _restore_holder(saved_holder)
        # The file must still read onboarded:true afterwards (not wiped).
        self.assertTrue(after.get("onboarded"))

    def test_onboarded_state_does_not_defer_the_greeting(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "brain-pref.json"
            fake.write_text(json.dumps({
                "model": "deepseek-v4.1-flash",
                "onboarded": True,
                "reasoning": "default",
            }), encoding="utf-8")
            saved_path = server.BRAIN_PREF_PATH
            saved_holder = _fresh_process_holder()
            server.BRAIN_PREF_PATH = fake
            try:
                server._startup_load_and_assert_pref()
                # Same expression the connect handler uses to decide:
                # _greeted[0] = bool(brain_pref_holder.get("onboarded")).
                greeted = bool(server.brain_pref_holder.get("onboarded"))
            finally:
                server.BRAIN_PREF_PATH = saved_path
                _restore_holder(saved_holder)
        self.assertTrue(greeted, "onboarded user must greet, not defer")

    def test_boot_without_onboarded_key_still_onboards(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "brain-pref.json"
            fake.write_text(json.dumps({
                "model": "deepseek-v4.1-flash",
                "reasoning": "default",
            }), encoding="utf-8")
            saved_path = server.BRAIN_PREF_PATH
            saved_holder = _fresh_process_holder()
            server.BRAIN_PREF_PATH = fake
            try:
                server._startup_load_and_assert_pref()
                greeted = bool(server.brain_pref_holder.get("onboarded"))
            finally:
                server.BRAIN_PREF_PATH = saved_path
                _restore_holder(saved_holder)
        self.assertFalse(greeted, "first-time user must still onboard")


class HardwiredStaysTests(unittest.TestCase):
    def test_stale_file_values_still_cannot_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "brain-pref.json"
            fake.write_text(json.dumps({
                "model": "mimo-v2.5-free",  # stale picker-era value
                "reasoning": "high",  # stale: must be normalized
                "onboarded": True,
            }), encoding="utf-8")
            saved_path = server.BRAIN_PREF_PATH
            saved_holder = _fresh_process_holder()
            server.BRAIN_PREF_PATH = fake
            try:
                server._startup_load_and_assert_pref()
                after = json.loads(fake.read_text(encoding="utf-8"))
            finally:
                server.BRAIN_PREF_PATH = saved_path
                _restore_holder(saved_holder)
        self.assertEqual(after["model"], server.BRAIN_MODEL_ID)
        self.assertEqual(after["reasoning"], server.HARDWIRED_REASONING)
        self.assertTrue(after["onboarded"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
