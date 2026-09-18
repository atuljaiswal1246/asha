---
name: voice-debugging
description: "Debug STT, TTS, and voice pipeline issues: latency, dropped frames, provider errors."
version: 1.0.0
author: Asha
license: Proprietary (all rights reserved)
platforms: [macos]
metadata:
  hermes:
    tags: [voice, stt, tts, pipeline, debugging, latency, audio]
    related_skills: [code-dispatch]
---

# Voice Debugging

## Overview

Use this skill when diagnosing issues with Asha's voice pipeline: STT failures, TTS glitches, audio dropouts, or latency regressions.

## Common Symptoms

- **STT returning empty/garbage**: Check `AudioToggle` state, verify microphone permissions, test with `whisper --model base` directly.
- **TTS not playing**: Verify `tts.holder()` is not None, check provider quota, look for `WorkerFailed` exceptions in logs.
- **High latency (>2s TTFT)**: Profile each pipeline stage. Typical breakdown: STT ~200ms, LLM first token ~300ms, TTS ~100ms. Anything beyond = regression.
- **Completion tokens = 1**: Almost always means an `await` in the LLM hot path. Check `notes/llm-hot-path-rule.md` (KB-15).

## Debugging Steps

1. **Isolate the stage**: Add temporary `print(time.time())` at each processor boundary.
2. **Check provider health**: `curl -s https://api.groq.com/openai/v1/models | jq` for Groq.
3. **Verify pipeline order**: `transport.input() → AudioToggle → stt → persona_router → recall_injector → llm → CaptionTap → tts → BotTextEcho → TurnSignal → status_relay → transport.output()`
4. **Check for KB-15 violations**: Any `await` in `process_frame()` on the LLM path = forbidden.

## Prevention

- Never `await` network/UI writes inside pipeline processors.
- Use `asyncio.Queue` + fire-and-forget for any side effects.
- Monitor `completion_tokens` metric — regression to 1 = hot path block.
