#!/usr/bin/env python3
"""Headless Asha client — drives the app exactly like the browser does.

Connects to the single-client WS (:7860), optionally switches to Work mode,
sends a chat message, and prints every frame the server sends back (audio
frames are summarised). This lets the supervisor reproduce a user's session,
find issues, and verify fixes without a browser.

Usage:
    .venv/bin/python scripts/asha_client.py --text "hello"
    .venv/bin/python scripts/asha_client.py --file brief.txt --work-mode --seconds 240
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import websockets

URI = "ws://127.0.0.1:7860"


async def run(text: str, seconds: float, work_mode: bool, brain: str | None,
              approve: bool, quiet_audio: bool) -> None:
    t0 = time.time()

    async with websockets.connect(URI, max_size=None) as ws:
        print(f"[client] connected to {URI}", flush=True)

        async def recv_loop() -> None:
            while True:
                msg = await ws.recv()
                dt = time.time() - t0
                if isinstance(msg, (bytes, bytearray)):
                    if not quiet_audio:
                        print(f"[{dt:6.1f}] <audio {len(msg)}b>", flush=True)
                    continue
                try:
                    j = json.loads(msg)
                except Exception:
                    print(f"[{dt:6.1f}] <raw> {str(msg)[:160]}", flush=True)
                    continue
                t = j.get("type", "?")
                body = {k: v for k, v in j.items() if k != "type"}
                body_s = json.dumps(body, ensure_ascii=False)
                print(f"[{dt:6.1f}] {t}: {body_s[:500]}", flush=True)
                # Auto-allow permission asks so the run can proceed unattended.
                if approve and t == "permission":
                    pid = j.get("permissionID", "")
                    await ws.send(json.dumps({
                        "type": "permission_response", "permissionID": pid,
                        "response": "allow",
                    }))
                    print(f"[{dt:6.1f}] >>> auto-approved permission {pid}", flush=True)
                if approve and t == "worker_permission":
                    await ws.send(json.dumps({
                        "type": "permission_response",
                        "worker_id": j.get("worker_id", ""),
                        "decision": "allow",
                    }))
                    print(f"[{dt:6.1f}] >>> auto-allowed worker permission", flush=True)
                if approve and t == "proposal":
                    sid = j.get("session_id", "")
                    await ws.send(json.dumps({"type": "approve", "session_id": sid}))
                    print(f"[{dt:6.1f}] >>> auto-approved proposal {sid}", flush=True)

        rt = asyncio.create_task(recv_loop())
        await asyncio.sleep(3.0)
        if work_mode:
            await ws.send(json.dumps({"type": "work_mode", "enabled": True}))
            print("[client] work_mode on", flush=True)
            await asyncio.sleep(1.5)
        if brain:
            await ws.send(json.dumps({"type": "brain_model_set", "model": brain}))
            print(f"[client] brain_model_set {brain}", flush=True)
            await asyncio.sleep(1.5)

        print(f"[client] >>> sending {len(text)} chars", flush=True)
        await ws.send(json.dumps({"type": "text", "data": text}))
        await asyncio.sleep(seconds)
        rt.cancel()
        try:
            await rt
        except asyncio.CancelledError:
            pass
    print("[client] done", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="")
    ap.add_argument("--file", default="")
    ap.add_argument("--seconds", type=float, default=180)
    ap.add_argument("--work-mode", action="store_true")
    ap.add_argument("--brain", default="")
    ap.add_argument("--approve", action="store_true")
    ap.add_argument("--quiet-audio", action="store_true")
    a = ap.parse_args()
    text = a.text
    if a.file:
        with open(a.file, "r", encoding="utf-8") as f:
            text = f.read()
    if not text.strip():
        print("nothing to send", file=sys.stderr)
        return 2
    asyncio.run(run(text, a.seconds, a.work_mode, a.brain or None,
                    a.approve, a.quiet_audio))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
