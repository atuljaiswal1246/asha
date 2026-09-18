"""Asha's single assistant identity — name and voice.

Asha is the only voice of this app. There is no roster, no name
detection and no voice switching: the assistant's voice is read once from
``TTS_VOICE`` (default ``bm_george``) and used for the whole session.
"""

import os

from persona_card import VOICE_POOL  # noqa: F401  (known Kokoro voice set)

ASSISTANT_NAME = "Asha"
DEFAULT_VOICE = "bm_george"


def assistant_voice() -> str:
    """The configured Kokoro voice: ``TTS_VOICE`` or the default (bm_george)."""
    return os.environ.get("TTS_VOICE", DEFAULT_VOICE)
