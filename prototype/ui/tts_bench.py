#!/usr/bin/env python3
"""TTS latency bench: cold-start and first-clause aggregation.

Run from ``prototype/ui``::

    ../../.venv/bin/python tts_bench.py

The script measures, before and after the ``server.py`` change:

* COLD first-audio for S1, in a fresh process (re-exec via ``--cold``) so the
  ONNX session is genuinely cold.
* WARM first-audio for each sentence after a throwaway warm-up synthesis.
* AGGREGATION delay: ms from the first token fed to the aggregator to the
  first chunk it yields (this is how much text pipecat buffers before it even
  asks Kokoro to speak).
* FIRST-AUDIO for the whole sentence vs. for the aggregator's first chunk.

It introspects ``svc._text_aggregator`` and never imports a class that may not
exist yet, so the same file works before and after the server change. It only
uses the stdlib plus existing project deps and writes no files.
"""

import argparse
import asyncio
import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from pipecat.frames.frames import TTSAudioRawFrame  # noqa: E402

SENTENCES = [
    ("S1", "Sure, I can take a look at that for you right now."),
    ("S2", "The build finished, but two tests are still failing, so I paused the deploy."),
    ("S3", "Yes."),
    ("S4", "Let me check the logs first, then I'll tell you exactly what went wrong."),
    ("S5", "I've updated the file and the tests pass; do you want me to commit it?"),
    (
        "S6",
        "That is a much longer sentence without any early punctuation at all so it can "
        "only be released by the hard cap or the final flush at the very end of the stream.",
    ),
]


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def _build_service():
    import server

    return server.GuardedKokoroTTSService(
        settings=server.KokoroTTSService.Settings(
            voice=os.environ.get("TTS_VOICE", "bm_george")
        )
    )


async def _first_audio_ms(svc, text: str, context_id: str) -> float:
    """Time from calling ``run_tts`` to the first ``TTSAudioRawFrame`` (ms)."""
    start = time.perf_counter()
    try:
        async for frame in svc.run_tts(text, context_id):
            if isinstance(frame, TTSAudioRawFrame):
                return (time.perf_counter() - start) * 1000.0
    except Exception as exc:  # noqa: BLE001
        print(f"    ! run_tts failed for {context_id!r}: {exc!r}", file=sys.stderr)
    return float("nan")


async def _aggregate(agg, sentence: str) -> dict:
    """Feed ``sentence`` word-by-word through ``agg`` and describe what came out."""
    await agg.reset()
    tokens = re.findall(r"\S+\s*", sentence)
    chunks: list[str] = []
    start = time.perf_counter()
    first_ms: float | None = None

    for token in tokens:
        async for yielded in agg.aggregate(token):
            if first_ms is None:
                first_ms = (time.perf_counter() - start) * 1000.0
            chunks.append(yielded.text)
        await asyncio.sleep(0.005)

    tail = await agg.flush()
    if tail is not None and tail.text:
        if first_ms is None:
            first_ms = (time.perf_counter() - start) * 1000.0
        chunks.append(tail.text)

    first_chunk = chunks[0] if chunks else ""
    return {
        "first_ms": first_ms if first_ms is not None else float("nan"),
        "first_chunk": first_chunk,
        "first_len": len(first_chunk),
        "first_is_whole": _norm(first_chunk) == _norm(sentence),
        "emitted": " ".join(chunks),
        "n_chunks": len(chunks),
        "text_ok": _norm(" ".join(chunks)) == _norm(sentence),
    }


async def _measure_sentence(svc, sid: str, sentence: str) -> dict:
    agg = svc._text_aggregator
    info = await _aggregate(agg, sentence)
    whole_ms = await _first_audio_ms(svc, sentence, f"{sid}-whole")
    chunk_text = info["first_chunk"] or sentence
    chunk_ms = await _first_audio_ms(svc, chunk_text, f"{sid}-chunk")
    info.update(
        {
            "id": sid,
            "sentence": sentence,
            "chars": len(sentence),
            "whole_ms": whole_ms,
            "chunk_ms": chunk_ms,
        }
    )
    return info


async def _cold() -> None:
    """Fresh-process cold first-audio for S1; prints one JSON line."""
    svc = _build_service()
    svc._sample_rate = 24000
    ms = await _first_audio_ms(svc, SENTENCES[0][1], "cold-s1")
    print(json.dumps({"cold_first_audio_ms": ms}), flush=True)


async def _warm() -> None:
    svc = _build_service()
    svc._sample_rate = 24000

    start = time.perf_counter()
    warm_text = "Warming up."
    async for frame in svc.run_tts(warm_text, "tts-warmup"):
        if isinstance(frame, TTSAudioRawFrame):
            break
    warm_ms = (time.perf_counter() - start) * 1000.0
    print(f"[WARMUP] throwaway synthesis first audio in {warm_ms:.1f} ms")

    rows = []
    for sid, sentence in SENTENCES:
        rows.append(await _measure_sentence(svc, sid, sentence))

    agg_name = type(svc._text_aggregator).__name__

    header = (
        f"{'id':<4} {'chars':>6} {'1st_chars':>10} {'agg_ms':>9} "
        f"{'whole_ms':>10} {'chunk_ms':>10}"
    )
    print()
    print("TTS AGGREGATION / FIRST-AUDIO BENCH")
    print(f"aggregator: {agg_name}")
    print("-" * len(header))
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['id']:<4} {r['chars']:>6} {r['first_len']:>10} "
            f"{r['first_ms']:>9.1f} {r['whole_ms']:>10.1f} {r['chunk_ms']:>10.1f}"
        )
    print("-" * len(header))
    med = lambda key: statistics.median([r[key] for r in rows])  # noqa: E731
    print(
        f"{'MED':<4} {'':>6} {med('first_len'):>10.0f} {med('first_ms'):>9.1f} "
        f"{med('whole_ms'):>10.1f} {med('chunk_ms'):>10.1f}"
    )

    print()
    print("first chunk per sentence:")
    for r in rows:
        whole = " (whole sentence)" if r["first_is_whole"] else ""
        preview = r["first_chunk"]
        if len(preview) > 60:
            preview = preview[:57] + "..."
        print(
            f"  {r['id']} n={r['n_chunks']:<2} chars={r['first_len']:<4} "
            f"{preview!r}{whole}"
        )

    ok = all(r["text_ok"] for r in rows)
    print(f"\n[CHECK] concatenated aggregations reconstruct every sentence: {ok}")

    cold_ms = _run_cold()
    warm_s1 = rows[0]["whole_ms"]
    print(f"[COLD] first audio S1 (fresh process): {cold_ms:.1f} ms")
    print(f"[WARM] first audio S1 (after warm-up):   {warm_s1:.1f} ms")
    if cold_ms == cold_ms and warm_s1 == warm_s1:  # not NaN
        print(f"[DELTA] cold - warm: {cold_ms - warm_s1:.1f} ms")


def _run_cold() -> float:
    """Re-exec this script with ``--cold`` and read its JSON result."""
    proc = subprocess.run(
        [sys.executable, str(HERE / "tts_bench.py"), "--cold"],
        capture_output=True,
        text=True,
        cwd=str(HERE),
    )
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "cold_first_audio_ms" in data:
            return float(data["cold_first_audio_ms"])
    print("    ! cold subprocess produced no JSON result", file=sys.stderr)
    if proc.stdout:
        print(proc.stdout, file=sys.stderr)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    return float("nan")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cold",
        action="store_true",
        help="measure one cold synthesis in this fresh process and print JSON",
    )
    args = parser.parse_args()

    if args.cold:
        asyncio.run(_cold())
        return 0

    asyncio.run(_warm())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
