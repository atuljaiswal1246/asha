"""Tests for the backchannel timing rules (backchannel.py).

Stdlib ``unittest`` only. These cover the four rules the brief names:

* plays once per user turn,
* suppressed when the reply is fast,
* never plays over the user's speech (and never when the mic is muted),
* cancelled mid-clip when the real reply starts.
"""

import asyncio
import tempfile
import unittest
import wave
from pathlib import Path

from pipecat.frames.frames import (
    OutputAudioRawFrame,
    TTSStartedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

import backchannel as bc


def _asset(name="mm_hm.wav", samples=2400, sample_rate=24000):
    """A small raw-PCM asset (default 0.1 s of silence)."""
    return bc.BackchannelAsset(
        name=name,
        pcm=b"\x00\x00" * samples,
        sample_rate=sample_rate,
        num_channels=1,
    )


class PickVariantTests(unittest.TestCase):
    def test_empty(self):
        self.assertIsNone(bc.pick_variant(0))

    def test_single(self):
        self.assertEqual(bc.pick_variant(1), 0)

    def test_rotation_never_repeats(self):
        self.assertEqual(bc.pick_variant(3, None), 0)
        self.assertEqual(bc.pick_variant(3, 0), 1)
        self.assertEqual(bc.pick_variant(3, 1), 2)
        self.assertEqual(bc.pick_variant(3, 2), 0)


class LoadAssetsTests(unittest.TestCase):
    def test_reads_wav_files_and_skips_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with wave.open(str(Path(tmp) / "mm_hm.wav"), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(b"\x00\x00" * 100)
            assets = bc.load_assets(tmp)
            self.assertEqual([a.name for a in assets], ["mm_hm.wav"])
            self.assertEqual(assets[0].sample_rate, 24000)
            self.assertEqual(len(assets[0].pcm), 200)


class DeciderTests(unittest.TestCase):
    def test_not_due_before_the_delay(self):
        d = bc.BackchannelDecider(delay_secs=0.35)
        d.turn_ended(now=100.0)
        self.assertFalse(d.due(now=100.34))
        self.assertFalse(d.due(now=100.0))
        self.assertTrue(d.due(now=100.35))

    def test_plays_once_per_turn(self):
        d = bc.BackchannelDecider(delay_secs=0.35)
        d.turn_ended(now=10.0)
        self.assertTrue(d.due(now=10.4))
        d.mark_played()
        self.assertFalse(d.due(now=11.0))
        self.assertEqual(d.suppression_reason(11.0), "already-played")

    def test_suppressed_when_the_reply_is_fast(self):
        d = bc.BackchannelDecider(delay_secs=0.35)
        d.turn_ended(now=1.0)
        self.assertEqual(d.reply_started(), 1)
        self.assertFalse(d.due(now=1.5))
        self.assertEqual(d.suppression_reason(1.5), "reply-started")

    def test_never_plays_over_user_speech(self):
        d = bc.BackchannelDecider(delay_secs=0.35)
        d.turn_ended(now=2.0)
        d.user_started()
        self.assertFalse(d.due(now=2.5))
        # Even after the user stops, that turn's acknowledgement is gone.
        d.user_stopped()
        self.assertFalse(d.due(now=2.6))
        self.assertEqual(d.suppression_reason(2.6), "no-pending-turn")

    def test_cancelled_while_playing_when_reply_starts(self):
        d = bc.BackchannelDecider(delay_secs=0.35)
        d.turn_ended(now=3.0)
        d.mark_played()
        self.assertFalse(d.playing_should_cancel())
        d.reply_started()
        self.assertTrue(d.playing_should_cancel())

    def test_muted_mic_blocks_and_suppresses(self):
        d = bc.BackchannelDecider(delay_secs=0.35)
        d.turn_ended(now=4.0)
        d.set_mic_enabled(False)
        self.assertFalse(d.due(now=4.5))
        self.assertEqual(d.suppression_reason(4.5), "no-pending-turn")
        self.assertTrue(d.playing_should_cancel())

    def test_disabled_switch(self):
        d = bc.BackchannelDecider(enabled=False)
        d.turn_ended(now=5.0)
        self.assertFalse(d.due(now=99.0))
        self.assertEqual(d.suppression_reason(99.0), "disabled")

    def test_a_second_turn_can_play_again(self):
        d = bc.BackchannelDecider(delay_secs=0.1)
        d.turn_ended(now=6.0)
        d.mark_played()
        d.turn_ended(now=7.0)
        self.assertTrue(d.due(now=7.2))


class PlayerTests(unittest.IsolatedAsyncioTestCase):
    async def test_plays_every_chunk(self):
        pushed = []

        async def push(frame):
            pushed.append(frame)

        async def no_sleep(_):
            return None

        player = bc.BackchannelPlayer(push, sleep=no_sleep, chunk_secs=0.04)
        complete = await player.play(_asset(samples=9600))  # 0.4 s
        self.assertTrue(complete)
        self.assertEqual(len(pushed), 10)
        self.assertTrue(all(type(f) is OutputAudioRawFrame for f in pushed))

    async def test_cancelled_mid_clip_when_reply_starts(self):
        pushed = []
        state = {"reply_started": False}

        async def push(frame):
            pushed.append(frame)
            state["reply_started"] = True  # reply starts after the first chunk

        async def no_sleep(_):
            return None

        player = bc.BackchannelPlayer(push, sleep=no_sleep, chunk_secs=0.04)
        complete = await player.play(
            _asset(samples=9600), should_cancel=lambda: state["reply_started"]
        )
        self.assertFalse(complete)
        self.assertEqual(len(pushed), 1)


class _Recorder:
    def __init__(self):
        self.frames = []

    async def push(self, frame, direction=None):
        self.frames.append(frame)


class ProcessorTests(unittest.IsolatedAsyncioTestCase):
    def _make(self, delay=0.0):
        rec = _Recorder()
        proc = bc.BackchannelProcessor(
            assets=[_asset(samples=240)],
            decider=bc.BackchannelDecider(delay_secs=delay),
            player=bc.BackchannelPlayer(rec.push, sleep=lambda _: asyncio.sleep(0)),
        )
        proc.push_frame = rec.push
        return proc, rec

    async def test_processor_plays_after_a_slow_turn(self):
        proc, rec = self._make()
        await proc.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        for _ in range(50):
            await asyncio.sleep(0.001)
            if any(type(f) is OutputAudioRawFrame for f in rec.frames):
                break
        audio = [f for f in rec.frames if type(f) is OutputAudioRawFrame]
        self.assertEqual(len(audio), 1)

    async def test_processor_suppresses_a_fast_reply(self):
        proc, rec = self._make()
        await proc.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await proc.process_frame(
            TTSStartedFrame(context_id="ctx"), FrameDirection.DOWNSTREAM
        )
        await asyncio.sleep(0.05)
        audio = [f for f in rec.frames if type(f) is OutputAudioRawFrame]
        self.assertEqual(audio, [])

    async def test_processor_suppresses_over_user_speech(self):
        proc, rec = self._make()
        await proc.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await proc.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await asyncio.sleep(0.05)
        audio = [f for f in rec.frames if type(f) is OutputAudioRawFrame]
        self.assertEqual(audio, [])


if __name__ == "__main__":
    unittest.main()
