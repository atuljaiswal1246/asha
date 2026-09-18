#!/usr/bin/env python3
"""Tests for clause-first TTS text aggregation in server.py.

The app replaces pipecat's hardcoded ``SimpleTextAggregator``
(``pipecat/services/tts_service.py:329``) with ``ClauseTextAggregator`` so
speech can start at the first clause instead of waiting for a whole sentence.
These tests exercise the aggregator directly; importing ``server`` does not load
the Kokoro model.
"""

import asyncio
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipecat.frames.frames import TTSAudioRawFrame  # noqa: E402

import server  # noqa: E402

SENTENCES = [
    "Sure, I can take a look at that for you right now.",
    "The build finished, but two tests are still failing, so I paused the deploy.",
    "Yes.",
    "Let me check the logs first, then I'll tell you exactly what went wrong.",
    "I've updated the file and the tests pass; do you want me to commit it?",
    "no punctuation here at all just a run of ordinary words",
]

TOKENS = re.compile(r"\S+\s*")


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def _feed(agg, sentence: str, delay: float = 0.0) -> list[str]:
    """Feed ``sentence`` word-by-word, then flush; return the emitted chunks."""

    async def _run():
        chunks: list[str] = []
        for token in TOKENS.findall(sentence):
            async for yielded in agg.aggregate(token):
                chunks.append(yielded.text)
            if delay:
                await asyncio.sleep(delay)
        tail = await agg.flush()
        if tail is not None and tail.text:
            chunks.append(tail.text)
        await agg.reset()
        return chunks

    return asyncio.run(_run())


class ClauseTextAggregatorTests(unittest.TestCase):
    def setUp(self):
        self.agg = server.ClauseTextAggregator()

    def test_first_clause_emitted_at_first_boundary(self):
        chunks = _feed(self.agg, "Sure, I can do that.")
        self.assertEqual(chunks[0], "Sure,")

    def test_lookahead_does_not_split_number(self):
        chunks = _feed(self.agg, "The value is 1,000 dollars.")
        for chunk in chunks:
            self.assertFalse(chunk.endswith("1,"), f"split inside 1,000: {chunks!r}")
            self.assertFalse(chunk.startswith("000"), f"split inside 1,000: {chunks!r}")
        self.assertEqual(_norm(" ".join(chunks)), _norm("The value is 1,000 dollars."))

    def test_concatenation_reconstructs_input(self):
        for sentence in SENTENCES:
            with self.subTest(sentence=sentence):
                chunks = _feed(self.agg, sentence)
                self.assertEqual(_norm(" ".join(chunks)), _norm(sentence), chunks)

    def test_no_mid_word_chop(self):
        for sentence in SENTENCES:
            with self.subTest(sentence=sentence):
                chunks = _feed(self.agg, sentence)
                self.assertTrue(chunks)
                # Joining with a single space only reconstructs the input if every
                # chunk boundary fell on whitespace or punctuation (a mid-word cut
                # would introduce a space the input does not have).
                self.assertEqual(_norm(" ".join(chunks)), _norm(sentence), chunks)
                # Each adjacent pair must rejoin without merging or splitting words.
                for prev, nxt in zip(chunks, chunks[1:]):
                    self.assertEqual(
                        _norm(prev + " " + nxt),
                        _norm(prev) + " " + _norm(nxt),
                        f"bad boundary between {prev!r} and {nxt!r}",
                    )

    def test_hard_cap_never_exceeds_limit_or_chops_word(self):
        agg = server.ClauseTextAggregator(max_chars=160)
        sentence = " ".join(["alpha"] * 60)
        self.assertGreater(len(sentence), 300)
        chunks = _feed(agg, sentence)
        self.assertGreaterEqual(len(chunks), 2)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 160, chunk)
            self.assertEqual(chunk, _norm(chunk))
        self.assertEqual(_norm(" ".join(chunks)), _norm(sentence))

    def test_make_text_aggregator_returns_clause_aggregator(self):
        agg = server.GuardedKokoroTTSService._make_text_aggregator()
        self.assertIsInstance(agg, server.ClauseTextAggregator)


class _FakeTTS:
    """Minimal stand-in for a TTS service's async ``run_tts`` generator."""

    def __init__(self):
        self.calls = []

    async def run_tts(self, text, context_id):
        self.calls.append((text, context_id))
        yield TTSAudioRawFrame(
            audio=b"\x00\x00" * 160,
            sample_rate=24000,
            num_channels=1,
            context_id=context_id,
        )


class WarmUpTests(unittest.TestCase):
    def test_warmup_logs_one_line_and_sets_sample_rate(self):
        tts = _FakeTTS()
        with self.assertLogs("asha.server", level="INFO") as logs:
            asyncio.run(server._warm_kokoro_tts(tts))
        self.assertTrue(
            any("[TTS-WARM] Kokoro warm in" in line for line in logs.output),
            logs.output,
        )
        self.assertEqual(tts._sample_rate, 24000)
        self.assertEqual(len(tts.calls), 1)

    def test_warmup_none_is_noop(self):
        asyncio.run(server._warm_kokoro_tts(None))


if __name__ == "__main__":
    unittest.main()
