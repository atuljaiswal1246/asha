"""Turn-persistence tests: every conversation turn is stored exactly once.

Root cause covered here: ``BotTextEcho`` buffered only ``LLMTextFrame`` chunks,
but the TTS service CONSUMES those and re-emits the spoken text as
``TTSTextFrame``. So the frames that actually carried a normal bot reply past
the TTS never reached the session log, and real replies - including Jarvis's
welcome on a new chat - were written nowhere. (The store had 1288 user rows and
0 bot rows.) These tests fail before the fix.

The user-turn path (``TurnRouter`` -> ``log_turn`` -> ``log_session_turn``) is
covered too, so both sides of a conversation are pinned.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402
from sessions import SessionStore  # noqa: E402

DOWN = FrameDirection.DOWNSTREAM


def _tts(text: str) -> TTSTextFrame:
    return TTSTextFrame(text=text, aggregated_by="sentence")


class _Echo(server.BotTextEcho):
    """BotTextEcho without a pipeline: capture pushed frames instead."""

    def __init__(self, session_cb=None):
        super().__init__(session_cb=session_cb)
        self.pushed: list = []

    async def push_frame(self, frame, direction=DOWN):
        self.pushed.append(frame)


class _Router(server.TurnRouter):
    """TurnRouter without a pipeline on the other end of push_frame."""

    async def push_frame(self, frame, direction=DOWN):
        pass


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jarvis-turn-")
        self.store = SessionStore(Path(self.tmp) / "state.db")
        self.sid = self.store.new_session({"client": "test"})
        self.echo = _Echo(
            session_cb=lambda role, text: server.log_session_turn(
                self.store, self.sid, role, text)
        )

    def tearDown(self):
        self.store.close()

    def feed(self, frames):
        async def _go():
            for f in frames:
                await self.echo.process_frame(f, DOWN)

        asyncio.run(_go())

    def rows(self):
        return self.store.messages(self.sid)

    def bot_rows(self):
        return [r for r in self.rows() if r["role"] == "bot"]


class StreamingReplyIsOneRow(_StoreCase):
    """A streaming reply must not create one row per chunk."""

    def test_tts_reply_is_persisted(self):
        self.feed([
            LLMFullResponseStartFrame(),
            _tts("Hello there, how are you?"),
            _tts("I am Jarvis."),
            LLMFullResponseEndFrame(),
        ])
        rows = self.bot_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "Hello there, how are you? I am Jarvis.")

    def test_many_chunks_still_one_row(self):
        self.feed(
            [LLMFullResponseStartFrame()]
            + [_tts(f"sentence {i}.") for i in range(30)]
            + [LLMFullResponseEndFrame()]
        )
        self.assertEqual(len(self.bot_rows()), 1)

    def test_whitespace_only_reply_is_not_stored(self):
        self.feed([LLMFullResponseStartFrame(), _tts("   "), LLMFullResponseEndFrame()])
        self.assertEqual(self.bot_rows(), [])


class GreetingWelcome(_StoreCase):
    """The welcome path must leave both sides of the conversation in the store."""

    def test_greeting_and_reply_reopen_complete(self):
        # Greeting (say_welcome -> LLMRunFrame -> TTS -> BotTextEcho).
        self.feed([
            LLMFullResponseStartFrame(),
            _tts("Hi, I'm Jarvis. How can I help?"),
            LLMFullResponseEndFrame(),
        ])
        # One spoken reply from the user, and Jarvis's answer.
        server.log_session_turn(self.store, self.sid, "user", "Hello Jarvis")
        self.feed([
            LLMFullResponseStartFrame(),
            _tts("Hello! What can I do for you?"),
            LLMFullResponseEndFrame(),
        ])
        rows = self.rows()
        self.assertEqual([r["role"] for r in rows], ["bot", "user", "bot"])
        self.assertEqual(rows[0]["text"], "Hi, I'm Jarvis. How can I help?")
        self.assertEqual(rows[1]["text"], "Hello Jarvis")
        self.assertEqual(rows[2]["text"], "Hello! What can I do for you?")


class UserTurnPath(_StoreCase):
    """The voice user-turn path stores exactly one user row per utterance."""

    def test_voice_transcript_logged_once(self):
        router = _Router(
            session_cb=lambda role, text: server.log_session_turn(
                self.store, self.sid, role, text)
        )

        async def _go():
            await router.process_frame(
                TranscriptionFrame(text="Hello Jarvis", user_id="user",
                                   timestamp="", finalized=True),
                DOWN,
            )

        asyncio.run(_go())
        rows = self.rows()
        self.assertEqual([r["role"] for r in rows], ["user"])
        self.assertEqual(rows[0]["text"], "Hello Jarvis")


class NoDuplicates(_StoreCase):
    """Frames seen on more than one path must still yield a single row."""

    def test_llmtext_chunks_flush_once(self):
        self.feed([
            LLMFullResponseStartFrame(),
            LLMTextFrame(text="Hello"),
            LLMTextFrame(text=" world"),
            LLMFullResponseEndFrame(),
        ])
        rows = self.bot_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "Hello world")

    def test_both_frame_paths_do_not_double_log(self):
        self.feed([
            LLMFullResponseStartFrame(),
            LLMTextFrame(text="Hi"),
            _tts("Hi there."),
            LLMFullResponseEndFrame(),
        ])
        rows = self.bot_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "Hi there.")

    def test_leftover_turn_does_not_merge_into_the_next(self):
        # A code line spoken through TTS carries no end frame on its own; the
        # next response's start must flush it as its own row, not merge it.
        self.feed([_tts("On it.")])
        self.feed([
            LLMFullResponseStartFrame(),
            _tts("Done."),
            LLMFullResponseEndFrame(),
        ])
        self.assertEqual([r["text"] for r in self.bot_rows()], ["On it.", "Done."])


if __name__ == "__main__":
    unittest.main(verbosity=2)
