import json
import time
from dataclasses import dataclass

from pipecat.frames.frames import (
    LLMTextFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed


@dataclass
class TurnRec:
    index: int = 0
    t_user_started: float | None = None
    t_user_stopped: float | None = None
    t_transcript: float | None = None
    t_llm_text: float | None = None
    t_tts_first: float | None = None
    t_tts_last: float | None = None
    t_tts_stopped: float | None = None
    transcript: str = ""
    response_text: str = ""
    barged_in: bool = False
    _printed_llm: bool = False

    @property
    def has_speech(self) -> bool:
        return bool(self.transcript)

    def as_dict(self) -> dict:
        t0 = self.t_user_started
        if t0 is None or not self.has_speech:
            return {"index": self.index, "incomplete": True}
        end = self.t_tts_last or self.t_tts_stopped
        d = {
            "index": self.index,
            "transcript": self.transcript,
            "response_text": self.response_text,
            "barged_in": self.barged_in,
        }
        if self.t_transcript:
            d["stt_ms"] = _ms(self.t_transcript - t0)
        if self.t_user_stopped and self.t_transcript:
            d["stt_after_stop_ms"] = _ms(self.t_transcript - self.t_user_stopped)
        if self.t_llm_text and self.t_transcript:
            d["llm_start_ms"] = _ms(self.t_llm_text - self.t_transcript)
        if self.t_tts_first:
            d["ttfb_ms"] = _ms(self.t_tts_first - t0)
        if self.t_tts_first and self.t_user_stopped:
            # TARGET METRIC: first audio byte measured from end of user speech
            d["ttfb_after_stop_ms"] = _ms(self.t_tts_first - self.t_user_stopped)
        if self.t_tts_last and self.t_tts_first:
            d["tts_render_ms"] = _ms(self.t_tts_last - self.t_tts_first)
        if end:
            d["total_ms"] = _ms(end - t0)
        return d


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


class StageObserver(BaseObserver):
    """Latch per-stage wall-clock timings for each user turn.

    Noise robustness: VAD bursts fire many ``UserStartedSpeakingFrame`` events
    per real utterance. We only commit a new turn record once a *finalized
    transcript* has been seen; bursts before that update the open record.
    """

    def __init__(self) -> None:
        super().__init__()
        self._turns: list[TurnRec] = []
        self._cur: TurnRec | None = None

    async def on_push_frame(self, data: FramePushed) -> None:
        f = data.frame
        t = time.monotonic()

        if isinstance(f, UserStartedSpeakingFrame):
            if self._cur is None or self._cur.has_speech:
                self._cur = TurnRec(index=len(self._turns) + 1, t_user_started=t)
            elif self._cur.t_user_started is None:
                self._cur.t_user_started = t
            return
        if isinstance(f, UserStoppedSpeakingFrame):
            if self._cur is not None and self._cur.t_user_stopped is None:
                self._cur.t_user_stopped = t
            return
        if isinstance(f, TranscriptionFrame):
            if self._cur is not None and f.finalized:
                if not self._cur.transcript:
                    print(f"[turn {self._cur.index}] transcript: {f.text}", flush=True)
                self._cur.transcript = f.text
                self._cur.t_transcript = t
            return
        if isinstance(f, LLMTextFrame):
            if self._cur is not None and self._cur.t_transcript is not None:
                self._cur.response_text += f.text
                self._cur.t_llm_text = self._cur.t_llm_text or t
                if not self._cur._printed_llm:
                    self._cur._printed_llm = True
                    print(f"[turn {self._cur.index}] llm: {f.text}", flush=True)
            return
        if isinstance(f, TTSAudioRawFrame):
            if self._cur is not None:
                self._cur.t_tts_first = self._cur.t_tts_first or t
                self._cur.t_tts_last = t
            return
        if isinstance(f, TTSStoppedFrame):
            if self._cur is not None:
                self._cur.t_tts_stopped = t
                self._turns.append(self._cur)
                self._cur = None

    def results(self) -> dict:
        if self._cur is not None and self._cur.has_speech:
            self._turns.append(self._cur)
            self._cur = None
        return [t.as_dict() for t in self._turns]

    def dump(self, path: str, env_snapshot: dict) -> None:
        turns = self.results()
        complete = [t for t in turns if not t.get("incomplete")]
        report = {
            "meta": env_snapshot,
            "turns": turns,
            "stats": {
                "turns_total": len(turns),
                "turns_complete": len(complete),
                "turns_interrupted": sum(1 for t in complete if t.get("barged_in")),
            },
        }
        for metric in (
            "stt_ms",
            "stt_after_stop_ms",
            "llm_start_ms",
            "ttfb_ms",
            "ttfb_after_stop_ms",
            "tts_render_ms",
            "total_ms",
        ):
            vals = sorted(t[metric] for t in complete if t.get(metric) is not None)
            if vals:
                report["stats"].update(
                    {
                        f"{metric}_p50": vals[len(vals) // 2],
                        f"{metric}_p95": vals[min(len(vals) - 1, int(len(vals) * 0.95))],
                        f"{metric}_n": len(vals),
                    }
                )
        with open(path, "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"[observer] baseline written to {path}", flush=True)
        print(json.dumps(report["stats"], indent=2), flush=True)
