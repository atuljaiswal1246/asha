import asyncio
import os
import sys

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.moonshine.stt import MoonshineSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.workers.runner import WorkerRunner

from observer import StageObserver

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, "..", ".env"), override=True)

logger.remove(0)
logger.add(sys.stderr, level="INFO")

RNNOISE = os.environ.get("RNNOISE", "true").lower() == "true"
INTERRUPT_ENABLED = os.environ.get("INTERRUPT_ENABLED", "true").lower() == "true"


def _dev(name: str):
    v = os.environ.get(name)
    return int(v) if v is not None and v != "" else None


async def main():
    extra_params: dict = {}
    if RNNOISE:
        from pipecat.audio.filters.rnnoise_filter import RNNoiseFilter

        # pyrnnoise 0.4.3 vs audiolab 0.5.x API drift: Graph signature changed rate= -> sample_rate=.
        import audiolab.av as _audiolab_av

        _orig_graph_init = _audiolab_av.Graph.__init__

        def _graph_init(self, *args, **kwargs):
            if "rate" in kwargs:
                kwargs["sample_rate"] = kwargs.pop("rate")
            _orig_graph_init(self, *args, **kwargs)

        _audiolab_av.Graph.__init__ = _graph_init
        extra_params["audio_in_filter"] = RNNoiseFilter()

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            input_device_index=_dev("AUDIO_IN_DEVICE"),
            output_device_index=_dev("AUDIO_OUT_DEVICE"),
            **extra_params,
        )
    )

    stt = MoonshineSTTService(
        settings=MoonshineSTTService.Settings(
            model=os.environ.get("STT_MODEL", "small-streaming"),
            language=os.environ.get("STT_LANGUAGE", "en"),
        )
    )

    pipeline_parts: list = [transport.input(), stt]

    tts = KokoroTTSService(
        settings=KokoroTTSService.Settings(voice=os.environ.get("TTS_VOICE", "af_heart"))
    )

    llm = OpenAILLMService(
        api_key="local",
        base_url=os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
        settings=OpenAILLMService.Settings(
            system_instruction=os.environ.get(
                "SYSTEM_PROMPT",
                "You are a kind, concise assistant in a voice conversation.",
            ),
            max_tokens=int(os.environ.get("LLM_MAX_TOKENS", "128")),
            temperature=float(os.environ.get("LLM_TEMPERATURE", "0.7")),
            top_p=float(os.environ.get("LLM_TOP_P", "0.8")),
        ),
    )

    vad_params = VADParams(
        confidence=float(os.environ.get("VAD_CONFIDENCE", "0.75")),
        start_secs=float(os.environ.get("VAD_START_SECS", "0.25")),
        stop_secs=float(os.environ.get("VAD_STOP_SECS", "0.25")),
        min_volume=float(os.environ.get("VAD_MIN_VOLUME", "0.7")),
    )

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(params=vad_params),
            user_turn_strategies=UserTurnStrategies(
                start=[VADUserTurnStartStrategy(enable_interruptions=INTERRUPT_ENABLED)]
            ),
        ),
    )

    pipeline = Pipeline(
        pipeline_parts
        + [
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    observer = StageObserver()
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            idle_timeout_secs=None,  # never self-cancel when the room is quiet
        ),
        processor_unusable_policy=ProcessorUnusablePolicy.END,
        observers=[observer],
    )

    runner = WorkerRunner()
    await runner.add_workers(worker)

    print("Baseline ready: speak 15 utterances (3 repeats each of the 5 prompts), then Ctrl-C.")
    context.add_message(
        {"role": "user", "content": "Please introduce yourself in one short sentence."}
    )
    await worker.queue_frames([LLMRunFrame()])

    await runner.run()

    try:
        await runner.run()
    finally:
        env_snapshot = {
            "phase": "0-polished",
            "llama_server": "http://127.0.0.1:8080/v1 (llama.cpp, Qwen3.5-4B-Q4_K_M GGUF)",
            "chat_template_kwargs": {"enable_thinking": False},
            "rnnoise": RNNOISE,
            "interrupt_enabled": INTERRUPT_ENABLED,
            "llm": {k: os.environ.get(k) for k in ("LLM_MODEL", "LLM_MAX_TOKENS", "LLM_TEMPERATURE", "LLM_TOP_P")},
            "stt": {k: os.environ.get(k) for k in ("STT_MODEL", "STT_LANGUAGE")},
            "tts": {"voice": os.environ.get("TTS_VOICE")},
            "vad": {k: os.environ.get(k) for k in ("VAD_CONFIDENCE", "VAD_START_SECS", "VAD_STOP_SECS", "VAD_MIN_VOLUME")},
        }
        out = os.environ.get("BASELINE_OUT", os.path.join(HERE, "..", "..", "baseline.json"))
        observer.dump(out, env_snapshot)


if __name__ == "__main__":
    asyncio.run(main())
