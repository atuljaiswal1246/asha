#!/usr/bin/env python3
"""Tests for the TTS silence guard in server.py.

Stdlib ``unittest`` only (no pytest). The guard exists because pipecat's TTS
base class closes an audio context that produces no frame within 3.0 s and
reports ``TTS context <id> completed with no audio``; Kokoro synthesizes a
whole sentence in one blocking pass, so a long sentence can be declared silent
before its audio is ready (Jarvis log 2026-09-18 02:44).
"""

import asyncio
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pipecat.services.kokoro.tts as kokoro_mod  # noqa: E402
from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame  # noqa: E402

import server  # noqa: E402

REPRO_TEXT = "It never spoils because it's so low in water and so acidic that bacteria and mould simply can't grow in it, and bees add an enzyme that keeps making a little hydrogen peroxide."

KOKORO_DIR = Path("~/.cache/pipecat/kokoro-onnx").expanduser()
KOKORO_MODEL = KOKORO_DIR / "kokoro-v1.0.onnx"
KOKORO_VOICES = KOKORO_DIR / "voices-v1.0.bin"

# Real on-device render is opt-in only (timing/hardware dependent, flaky in CI):
# run it on purpose with JARVIS_REAL_RENDER=1 python3 test_tts_guard.py -v
REAL_RENDER_ENABLED = os.environ.get("JARVIS_REAL_RENDER") == "1"


def _audio(ctx: str = "ctx") -> TTSAudioRawFrame:
    return TTSAudioRawFrame(
        audio=b"\x00\x00" * 160,
        sample_rate=24000,
        num_channels=1,
        context_id=ctx,
    )


def _collect(svc, text: str, ctx: str = "ctx") -> list:
    """Run ``svc.run_tts`` to completion in a fresh event loop."""

    async def _gen():
        return [frame async for frame in svc.run_tts(text, ctx)]

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_gen())
    finally:
        loop.close()


class SplitTextTests(unittest.TestCase):
    def setUp(self):
        self.svc = server.GuardedKokoroTTSService.__new__(
            server.GuardedKokoroTTSService
        )

    def test_split_text_breaks_long_reply(self):
        segments = self.svc._split_text(REPRO_TEXT)
        self.assertGreater(len(segments), 1)
        for segment in segments:
            self.assertLessEqual(
                len(segment), 90, f"segment exceeds limit: {segment!r}"
            )
        rejoined = " ".join(segments)
        collapsed = " ".join(REPRO_TEXT.split())
        self.assertEqual(rejoined, collapsed)

    def test_single_segment_for_short_text(self):
        short = "Jarvis. Just Jarvis."
        self.assertEqual(self.svc._split_text(short), [short])


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.svc = server.GuardedKokoroTTSService.__new__(
            server.GuardedKokoroTTSService
        )
        self.calls = []
        self._original = kokoro_mod.KokoroTTSService.run_tts
        self.addCleanup(self._restore)

    def _restore(self):
        kokoro_mod.KokoroTTSService.run_tts = self._original

    def _install_fake(self, handler):
        """Install a fake ``run_tts``.

        ``handler(text, call_index)`` returns the list of frames to yield for
        that call, where ``call_index`` is the 0-based index of that text.
        """
        calls = self.calls

        async def fake(self, text, context_id):
            index = calls.count(text)
            calls.append(text)
            for frame in handler(text, index):
                yield frame

        kokoro_mod.KokoroTTSService.run_tts = fake

    def test_short_reply_uses_one_shot_path(self):
        text = "Jarvis. Just Jarvis."
        self._install_fake(lambda _t, _i: [_audio()])

        frames = _collect(self.svc, text)

        self.assertEqual(self.calls, [text])
        audio = [f for f in frames if isinstance(f, TTSAudioRawFrame)]
        self.assertEqual(len(audio), 1)

    def test_guard_retries_on_no_audio_signal(self):
        text = "This piece is short enough to stay whole."

        def handler(_t, index):
            return [] if index == 0 else [_audio()]

        self._install_fake(handler)

        with self.assertLogs("asha.server", level="WARNING") as logs:
            frames = _collect(self.svc, text)

        self.assertEqual(self.calls.count(text), 2, "retry did not happen")
        audio = [f for f in frames if isinstance(f, TTSAudioRawFrame)]
        self.assertEqual(len(audio), 1)
        self.assertTrue(
            any("produced no audio" in line for line in logs.output),
            f"expected a retry warning, got: {logs.output}",
        )

    def test_no_double_speak_after_partial_audio(self):
        text = "This one starts audio then errors."

        def handler(_t, _index):
            return [_audio(), ErrorFrame(error="boom")]

        self._install_fake(handler)

        frames = _collect(self.svc, text)

        self.assertEqual(self.calls.count(text), 1, "guard retried after audio")
        audio = [f for f in frames if isinstance(f, TTSAudioRawFrame)]
        self.assertEqual(len(audio), 1)
        self.assertFalse(any(isinstance(f, ErrorFrame) for f in frames))

    def test_no_audio_twice_splits_into_shorter_pieces(self):
        text = " ".join(["alpha"] * 15)
        self.assertLessEqual(len(text), 90)
        self.assertGreater(len(self.svc._split_text(text, len(text) // 2)), 1)

        def handler(t, _index):
            return [] if t == text else [_audio()]

        self._install_fake(handler)

        with self.assertLogs("asha.server", level="WARNING") as logs:
            frames = _collect(self.svc, text)

        self.assertEqual(self.calls.count(text), 2)
        shorter = [c for c in self.calls if c != text]
        self.assertTrue(shorter, "guard never retried with shorter pieces")
        audio = [f for f in frames if isinstance(f, TTSAudioRawFrame)]
        self.assertGreaterEqual(len(audio), 1)
        self.assertTrue(
            any("shorter pieces" in line for line in logs.output),
            f"expected a 'shorter pieces' warning, got: {logs.output}",
        )


class ReproTextGuardTests(unittest.TestCase):
    """Deterministic guard coverage for REPRO_TEXT (always runs).

    Proves the same behaviour the real render checks — the guard splits the
    reproduction text and produces audio for every part — with mocked
    synthesis, so the default suite stays deterministic.
    """

    def setUp(self):
        self.svc = server.GuardedKokoroTTSService.__new__(
            server.GuardedKokoroTTSService
        )
        self.calls = []
        self._original = kokoro_mod.KokoroTTSService.run_tts
        self.addCleanup(self._restore)

    def _restore(self):
        kokoro_mod.KokoroTTSService.run_tts = self._original

    def test_repro_text_produces_audio_via_segments(self):
        segments = self.svc._split_text(REPRO_TEXT)
        self.assertGreater(len(segments), 1)
        for segment in segments:
            self.assertLessEqual(
                len(segment), 90, f"segment exceeds limit: {segment!r}"
            )

        calls = self.calls

        async def fake(self, text, context_id):
            calls.append(text)
            yield _audio(context_id)

        kokoro_mod.KokoroTTSService.run_tts = fake

        frames = _collect(self.svc, REPRO_TEXT, "ctx")

        self.assertEqual(calls, segments)
        audio = [f for f in frames if isinstance(f, TTSAudioRawFrame)]
        self.assertEqual(len(audio), len(segments))


@unittest.skipUnless(
    REAL_RENDER_ENABLED,
    "real render test skipped: set JARVIS_REAL_RENDER=1 to run",
)
@unittest.skipUnless(
    KOKORO_MODEL.exists() and KOKORO_VOICES.exists(),
    "kokoro model files not present",
)
class RealRenderTests(unittest.TestCase):
    def test_real_reproduction_text_produces_audio(self):
        svc = server.GuardedKokoroTTSService(
            settings=server.KokoroTTSService.Settings(voice="bm_george")
        )
        svc._sample_rate = 24000

        async def _render():
            frames = []
            first_audio_at = None
            start = time.time()
            async for frame in svc.run_tts(REPRO_TEXT, "ctx"):
                frames.append(frame)
                if isinstance(frame, TTSAudioRawFrame) and first_audio_at is None:
                    first_audio_at = time.time() - start
            return frames, first_audio_at

        loop = asyncio.new_event_loop()
        try:
            frames, first_audio_at = loop.run_until_complete(_render())
        finally:
            loop.close()

        self.assertTrue(
            any(isinstance(f, TTSAudioRawFrame) for f in frames),
            "guard produced no audio for the reproduction text",
        )
        self.assertIsNotNone(first_audio_at, "no first-audio timestamp recorded")
        self.assertLess(
            first_audio_at,
            3.0,
            f"first audio took {first_audio_at:.3f}s; pipecat's idle window is 3.0s",
        )


if __name__ == "__main__":
    unittest.main()
