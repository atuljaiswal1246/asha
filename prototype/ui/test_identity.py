"""Guards for Jarvis's single identity: one name, one voice, no roster.

The persona system was removed fully (user decision, 2026-09-17). These tests
lock in what must now hold: exactly one identity (Jarvis) with the configured
TTS voice, the brain contract still composed by ``build_system_text``, no
persona frame types in the wire serializer, and the frozen VAD values.

Run with:

    ../../.venv/bin/python -m unittest test_identity -q
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import persona_card  # noqa: E402
import personas  # noqa: E402

UI_DIR = Path(__file__).resolve().parent


class IdentityTests(unittest.TestCase):
    def test_single_identity_is_asha(self):
        self.assertEqual(personas.ASSISTANT_NAME, "Asha")

    def test_no_roster_or_name_map(self):
        for gone in ("PERSONAS", "PersonaRegistry", "_NAME_MAP",
                     "_PERSONA_ALIASES", "Persona"):
            self.assertFalse(
                hasattr(personas, gone),
                f"personas.{gone} still exists — the roster was not removed",
            )

    def test_voice_defaults_to_bm_george_and_is_in_pool(self):
        old = os.environ.pop("TTS_VOICE", None)
        try:
            self.assertEqual(personas.assistant_voice(), "bm_george")
            self.assertIn(personas.assistant_voice(), persona_card.VOICE_POOL)
        finally:
            if old is not None:
                os.environ["TTS_VOICE"] = old

    def test_voice_reads_the_configured_env(self):
        old = os.environ.get("TTS_VOICE")
        try:
            os.environ["TTS_VOICE"] = "am_michael"
            self.assertEqual(personas.assistant_voice(), "am_michael")
        finally:
            if old is None:
                os.environ.pop("TTS_VOICE", None)
            else:
                os.environ["TTS_VOICE"] = old


class SystemPromptTests(unittest.TestCase):
    def test_build_system_text_still_produces_the_brain_contract(self):
        import server

        t = server.build_system_text("You are Asha.", "")
        self.assertIn("HOW YOU WORK", t)
        self.assertIn("WORK PROTOCOL", t)
        self.assertIn("Asha", t)

    def test_brain_is_told_to_decide_and_not_to_ask_technical_questions(self):
        """The user can be anyone; asking them a technical question wastes their
        time. This rule is the user's own words and must survive edits."""
        import server

        t = server.build_system_text("You are Asha.", "")
        self.assertIn("DECIDE, DO NOT ASK", t)
        self.assertIn("The user can be anyone", t)
        self.assertIn("never in technical terms", t)
        self.assertIn("asking a question you could have answered yourself wastes", t)
        # the rule must still name what genuinely belongs to the user
        for theirs in ("their money", "cannot be undone", "their taste"):
            self.assertIn(theirs, t)

    def test_brain_must_verify_visually_and_not_assert_unchecked_facts(self):
        """Two real failures must not come back: claiming a visible change
        without looking at it, and stating an unchecked cause as fact."""
        import server

        t = server.build_system_text("You are Asha.", "")
        self.assertIn("VERIFY WITH YOUR EYES", t)
        self.assertIn("look at it with read_screen and confirm the change", t)
        self.assertIn("say so plainly instead of claiming success", t)

        self.assertIn("Never assert a cause, a fix, or that something does not exist as fact", t)
        self.assertIn("unverified hypothesis", t)
        self.assertIn("re-check instead of defending", t)

        # the original "prove it" rule must survive alongside them
        self.assertIn("Never claim work is done unless a tool result proves it", t)


class NoPersonaFramesTests(unittest.TestCase):
    def test_proto_has_no_persona_frames(self):
        import proto

        self.assertFalse(hasattr(proto, "PersonaFrame"))
        self.assertFalse(hasattr(proto, "PersonasFrame"))

    def test_serializer_has_no_persona_message_types(self):
        from proto import UIFrameSerializer

        serializer = UIFrameSerializer()
        # The chip-switch message no longer exists on the client protocol.
        self.assertIsNone(
            asyncio.run(serializer.deserialize(
                json.dumps({"type": "persona_switch", "name": "Neha"})
            ))
        )
        src = (UI_DIR / "proto.py").read_text(encoding="utf-8")
        self.assertNotIn('"type": "persona"', src)
        self.assertNotIn('"type": "personas"', src)


class VadFrozenTests(unittest.TestCase):
    """VAD tuning is frozen: same env vars, same defaults (0.75/0.25/0.25/0.7)."""

    def test_vad_defaults_are_unchanged(self):
        src = (UI_DIR / "server.py").read_text(encoding="utf-8")
        self.assertIn('VAD_CONFIDENCE", "0.75"', src)
        self.assertIn('VAD_START_SECS", "0.25"', src)
        self.assertIn('VAD_STOP_SECS", "0.25"', src)
        self.assertIn('VAD_MIN_VOLUME", "0.7"', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
