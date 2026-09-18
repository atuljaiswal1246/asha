"""Brain proxy: serves the Jarvis web frontend and streams Qwen (llama-server) for it.

The browser frontend (react-ai-voice-avatar, MIT) owns mic STT + 3D lip-sync + Kokoro TTS
on-device. This server only: 1) serves static dist, 2) forwards user text to the local
llama-server (OpenAI-compatible) and streams the reply text back.
"""
import asyncio
import os
import sys
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"), override=True)

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
import aiohttp  # noqa: E402

LLAMA_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8080/v1")
DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webfront", "dist")
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "You are Asha, a warm, kind, quick-witted personal assistant who talks like a close friend.",
)
MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "128"))
TEMP = float(os.environ.get("LLM_TEMPERATURE", "0.7"))
TOP_P = float(os.environ.get("LLM_TOP_P", "0.8"))

messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

app = FastAPI()


@app.post("/v1/chat")
async def chat(body: dict):
    text = (body.get("text") or "").strip()
    if not text:
        return {"error": "empty text"}
    messages.append({"role": "user", "content": text})

    async def stream():
        llm_payload = {
            "model": "local",
            "messages": messages,
            "stream": True,
            "max_tokens": MAX_TOKENS,
            "temperature": TEMP,
            "top_p": TOP_P,
        }
        buffer = ""
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    LLAMA_URL + "/chat/completions", json=llm_payload, timeout=None
                ) as resp:
                    async for line in resp.content:
                        line = line.decode("utf-8", "ignore").strip()
                        if not line.startswith("data:") or line == "data: [DONE]":
                            continue
                        delta = line[5:].strip()
                        if not delta:
                            continue
                        import json

                        try:
                            piece = json.loads(delta)["choices"][0]["delta"].get("content", "")
                        except Exception:
                            continue
                        if not piece:
                            continue
                        buffer += piece
                        yield piece
        except Exception as e:
            print(f"brain proxy error: {e}", file=sys.stderr)
        print("[brain] assistant:", buffer[:120], file=sys.stderr)
        messages.append({"role": "assistant", "content": buffer})
        if len(messages) > 14:
            messages[:] = messages[:1] + messages[-12:]

    return StreamingResponse(stream(), media_type="text/plain; charset=utf-8")


@app.get("/")
async def root():
    return FileResponse(os.path.join(DIST, "index.html"))


app.mount("/assets", StaticFiles(directory=os.path.join(DIST, "assets")), name="assets")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
