"""Backchannel acknowledgements for Jarvis.

A short, pre-rendered "mm-hm" fills the silence between the end of the user's
turn and the first audio of a slow reply. Nothing is synthesized on the turn:
the clips are rendered once, in the app's own voice, by ``render_assets()`` and
stored under ``backchannel_assets/``.

Timing rule (the only rule): after ``UserStoppedSpeakingFrame``, wait
``BACKCHANNEL_DELAY_SECS`` (350 ms). If no bot audio (``TTSStartedFrame`` or
``TTSAudioRawFrame``) has started by then, play one clip. A fast reply never
doubles up because the reply start cancels the pending clip. A clip that is
already playing stops early if the reply starts, if the user starts speaking,
or if the mic is muted. At most one clip per user turn.

Switch: ``BACKCHANNEL_ENABLED`` (module constant — set it to ``False`` to turn
the feature off). It is deliberately not an env var and not a config edit.

Render command (from ``prototype/ui``)::

    ../../.venv/bin/python backchannel.py

This module only depends on pipecat plus the existing app modules; the
renderer imports ``server`` for the exact TTS class/settings the app uses.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from pipecat.frames.frames import (
    ErrorFrame,
    OutputAudioRawFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger("asha.backchannel")

ASSETS_DIR = Path(__file__).resolve().parent / "backchannel_assets"

# The off switch (module constant, not an env var / config value).
BACKCHANNEL_ENABLED = True

# How long after the user stops before we decide the reply is "slow".
BACKCHANNEL_DELAY_SECS = 0.35

# Spoken text, in rotation order; file names mirror the phrases. The phrases
# are rendered short on purpose (~0.6-1.0 s with bm_george): the client gates
# mic capture while any audio plays, so a long acknowledgement would hold up
# barge-in. Edit these and re-run the renderer to retune.
BACKCHANNEL_TEXTS = {
    "mm_hm.wav": "Hmm.",
    "let_me_see.wav": "Let's see.",
    "one_moment.wav": "One sec.",
}


def pick_variant(count: int, previous: int | None = None) -> int | None:
    """Next clip index in rotation, never repeating the previous one."""
    if count <= 0:
        return None
    if count == 1 or previous is None:
        return 0
    return (previous + 1) % count


@dataclass
class BackchannelAsset:
    """One pre-rendered clip: raw 16-bit PCM plus its format."""

    name: str
    pcm: bytes
    sample_rate: int = 24000
    num_channels: int = 1


def load_assets(directory: Path = ASSETS_DIR) -> list[BackchannelAsset]:
    """Read the rendered clips from ``directory`` (missing files are skipped)."""
    assets: list[BackchannelAsset] = []
    for name in BACKCHANNEL_TEXTS:
        path = Path(directory) / name
        try:
            with wave.open(str(path), "rb") as wav:
                assets.append(
                    BackchannelAsset(
                        name=name,
                        pcm=wav.readframes(wav.getnframes()),
                        sample_rate=wav.getframerate(),
                        num_channels=wav.getnchannels(),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[BACKCHANNEL] asset {path} unreadable: {exc!r}")
    return assets


class BackchannelDecider:
    """The timing rules, with no audio and no pipecat — so they are testable.

    Times are seconds from any monotonic clock; the caller passes ``now`` in.
    """

    def __init__(self, *, enabled: bool = BACKCHANNEL_ENABLED,
                 delay_secs: float = BACKCHANNEL_DELAY_SECS):
        self.enabled = bool(enabled)
        self.delay_secs = delay_secs
        self.mic_enabled = True
        self.user_speaking = False
        self._turn = 0
        self._pending: int | None = None
        self._deadline: float | None = None
        self._played: int | None = None
        self._reply_started: int | None = None

    @property
    def pending_turn(self) -> int | None:
        return self._pending

    def set_mic_enabled(self, enabled: bool) -> None:
        self.mic_enabled = bool(enabled)
        if not self.mic_enabled:
            self._pending = None
            self._deadline = None

    def user_started(self) -> None:
        self.user_speaking = True
        self._pending = None
        self._deadline = None

    def user_stopped(self) -> None:
        self.user_speaking = False

    def turn_ended(self, now: float) -> int:
        """A user turn just ended; arm the delay and return the turn id."""
        self._turn += 1
        self._pending = self._turn
        self._deadline = now + self.delay_secs
        self._reply_started = None
        return self._turn

    def reply_started(self) -> int | None:
        """Bot audio started; cancel any pending clip. Returns the arm it killed."""
        pending = self._pending
        self._reply_started = self._turn
        self._pending = None
        self._deadline = None
        return pending

    def due(self, now: float) -> bool:
        if not self.enabled or self._pending is None:
            return False
        if not self.mic_enabled or self.user_speaking:
            return False
        return now >= (self._deadline or 0.0)

    def mark_played(self) -> None:
        """Record that a clip was played for the pending turn (never twice)."""
        self._played = self._pending
        self._pending = None
        self._deadline = None

    def playing_should_cancel(self) -> bool:
        """True once a playing clip must stop (reply started / user / muted)."""
        return (
            self.user_speaking
            or not self.mic_enabled
            or self._reply_started is not None
        )

    def suppression_reason(self, now: float) -> str:
        """Human-readable reason ``due()`` is False, for the log line."""
        if not self.enabled:
            return "disabled"
        if self._pending is None:
            if self._reply_started == self._turn:
                return "reply-started"
            if self._played == self._turn:
                return "already-played"
            return "no-pending-turn"
        if not self.mic_enabled:
            return "mic-muted"
        if self.user_speaking:
            return "user-speaking"
        if now < (self._deadline or 0.0):
            return "reply-not-yet-late"
        return "due"


class BackchannelPlayer:
    """Pushes one clip downstream in small, real-time-paced chunks.

    ``should_cancel()`` is checked before every chunk so a clip stops cleanly
    when the reply starts (or the user speaks / the mic mutes). Returns True
    when the whole clip was played, False when it was cancelled early.
    """

    def __init__(self, push, *, sleep=asyncio.sleep, chunk_secs: float = 0.04):
        self._push = push
        self._sleep = sleep
        self._chunk_secs = chunk_secs

    async def play(self, asset: BackchannelAsset, should_cancel=None) -> bool:
        channels = max(1, asset.num_channels or 1)
        frame_bytes = 2 * channels
        chunk_bytes = max(frame_bytes, int(asset.sample_rate * self._chunk_secs) * frame_bytes)
        for offset in range(0, len(asset.pcm), chunk_bytes):
            if should_cancel is not None and should_cancel():
                return False
            chunk = asset.pcm[offset : offset + chunk_bytes]
            await self._push(
                OutputAudioRawFrame(
                    audio=chunk,
                    sample_rate=asset.sample_rate,
                    num_channels=channels,
                )
            )
            await self._sleep(len(chunk) / frame_bytes / asset.sample_rate)
        return True


class BackchannelProcessor(FrameProcessor):
    """Plays a pre-rendered acknowledgement when the reply is slow to speak.

    Sits just before the output transport. It watches the frames that already
    flow past that point and injects a plain ``OutputAudioRawFrame`` (not a
    ``TTSAudioRawFrame``), so the acknowledgement never looks like the bot's
    reply to the rest of the pipeline and never touches the real reply path.
    """

    def __init__(self, *, assets=None, decider=None, player=None,
                 clock=time.monotonic):
        super().__init__()
        self._assets = list(assets) if assets is not None else load_assets()
        self._decider = decider or BackchannelDecider()
        self._clock = clock
        self._player = player or BackchannelPlayer(self.push_frame)
        self._timer_task: asyncio.Task | None = None
        self._play_task: asyncio.Task | None = None
        self._variant: int | None = None

    # -- mic state (fed by AudioToggleProcessor) ---------------------------
    def set_mic_enabled(self, enabled: bool) -> None:
        was_pending = self._decider.pending_turn is not None
        self._decider.set_mic_enabled(enabled)
        if not enabled:
            self._cancel_timer()
            if was_pending:
                logger.info("[BACKCHANNEL] suppressed (mic-muted)")

    # -- frame flow --------------------------------------------------------
    async def process_frame(self, frame, direction):
        if isinstance(frame, UserStartedSpeakingFrame):
            had_pending = self._decider.pending_turn is not None
            self._decider.user_started()
            self._cancel_timer()
            if had_pending:
                logger.info("[BACKCHANNEL] suppressed (user-speaking)")
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._on_turn_ended()
        elif isinstance(frame, (TTSStartedFrame, TTSAudioRawFrame)):
            pending = self._decider.reply_started()
            self._cancel_timer()
            if pending is not None:
                logger.info("[BACKCHANNEL] suppressed (reply-started)")
        elif isinstance(frame, ErrorFrame):
            if self._decider.pending_turn is not None:
                self._decider.reply_started()
                self._cancel_timer()
                logger.info("[BACKCHANNEL] suppressed (pipeline-error)")
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

    # -- internals ---------------------------------------------------------
    def _cancel_timer(self) -> None:
        if self._timer_task is not None and not self._timer_task.done():
            self._timer_task.cancel()
        self._timer_task = None

    def _on_turn_ended(self) -> None:
        self._cancel_timer()
        self._decider.user_stopped()
        turn = self._decider.turn_ended(self._clock())
        self._timer_task = asyncio.ensure_future(self._wait_then_play(turn))

    async def _wait_then_play(self, turn: int) -> None:
        try:
            await asyncio.sleep(self._decider.delay_secs)
        except asyncio.CancelledError:
            return
        if not self._decider.due(self._clock()):
            logger.info(
                "[BACKCHANNEL] suppressed "
                f"({self._decider.suppression_reason(self._clock())})"
            )
            return
        asset = self._pick_asset()
        if asset is None:
            logger.info("[BACKCHANNEL] suppressed (no-assets)")
            return
        self._decider.mark_played()
        logger.info(f"[BACKCHANNEL] play {asset.name}")
        self._play_task = asyncio.ensure_future(self._run_play(asset))

    def _pick_asset(self) -> BackchannelAsset | None:
        index = pick_variant(len(self._assets), self._variant)
        if index is None:
            return None
        self._variant = index
        return self._assets[index]

    async def _run_play(self, asset: BackchannelAsset) -> None:
        try:
            completed = await self._player.play(asset, self._decider.playing_should_cancel)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[BACKCHANNEL] play failed: {exc!r}")
            return
        if not completed:
            logger.info("[BACKCHANNEL] play cancelled")


def _write_wav(path: Path, pcm: bytes, sample_rate: int, channels: int) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)


def render_assets(out_dir: Path = ASSETS_DIR) -> list[str]:
    """Render the clips once, in the app's own voice, and write them as WAV.

    Command (from ``prototype/ui``)::

        ../../.venv/bin/python backchannel.py

    Uses the same TTS class/settings/model paths the running app uses (via
    ``server``), so the clips match Jarvis's voice exactly. No new dependency,
    no voice/model/config change.
    """
    import server  # the app's own TTS construction

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    async def _render() -> None:
        svc = server.GuardedKokoroTTSService(
            settings=server.KokoroTTSService.Settings(
                voice=server.assistant_voice(),
            ),
            model_path=os.environ.get("KOKORO_MODEL_PATH") or None,
            voices_path=os.environ.get("KOKORO_VOICES_PATH") or None,
        )
        # Outside a running pipeline the sample rate is unset; pin Kokoro's
        # native 24 kHz so synthesis does not resample against 0.
        svc._sample_rate = 24000
        for name, text in BACKCHANNEL_TEXTS.items():
            chunks: list[bytes] = []
            sample_rate, channels = 24000, 1
            async for frame in svc.run_tts(text, f"backchannel-{name}"):
                if isinstance(frame, TTSAudioRawFrame):
                    chunks.append(frame.audio)
                    sample_rate = frame.sample_rate or sample_rate
                    channels = frame.num_channels or channels
            pcm = b"".join(chunks)
            if not pcm:
                raise RuntimeError(f"{name}: no audio for {text!r}")
            _write_wav(out_dir / name, pcm, sample_rate, channels)
            logger.info(
                f"[BACKCHANNEL] rendered {name} ({len(pcm)} bytes "
                f"@ {sample_rate} Hz, {channels}ch)"
            )

    asyncio.run(_render())
    return sorted(p.name for p in out_dir.glob("*.wav"))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("rendered:", ", ".join(render_assets()))
