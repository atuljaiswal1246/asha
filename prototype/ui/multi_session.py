"""multi_session.py — more than one window, one Asha.

**Owner, 2026-09-30:** *"Allow multiple sessions"*

## Why this is not a preference

The app allowed exactly one client, and enforced it by refusing every other one with
`1013 "Server already has a connected client"`. That single rule cost more than the feature it
was protecting, and every cost was measured:

| what it did | receipt |
|---|---|
| my diagnostics stole the slot from his window | his page read **"connecting"** in front of a healthy server, repeatedly |
| stale tabs held it for an hour | **15** of them, found and closed |
| a guard could not be run while he was using the app | `test_the_composer_actually_sends.py` correctly refuses to report green at `SOCKET: 3` |
| a client leaving mid-turn poisoned health | `[LLM] pipeline error: received 1001 (going away)` → `/health` served **503** afterwards, with `voice_usable=PASS` |

That last one is the reason this is a product change and not a testing convenience. **A browser
closing is not an unhealthy product**, and health that says 503 because a window closed is
health that will cry wolf at the worst moment.

## What the rule actually was, and where

Not our code. `pipecat 1.8.1`, `transports/websocket/server.py`:

```python
class SingleClientWebsocketServerTransport:
    async def _client_handler(self, websocket):
        if self._websocket and self._websocket.state is State.OPEN:
            await websocket.close(code=1013, reason="Server already has a connected client")
            return          # <-- and on_client_connected is NEVER called for a refused client
```

and, on the output side, one socket to write to:

```python
class SingleClientWebsocketServerOutputTransport:
    async def _write_frame(self, frame):
        payload = await self._params.serializer.serialize(frame)
        if payload and self._websocket:
            await self._websocket.send(payload)      # L494
```

So there are exactly two places to change and no more. Both are overridden here, and everything
else is untouched: `server_run.py` has **19** read sites of `ws_holder["ws"]` and **one** write
site, and the write site is what gets a `Fanout` instead of a socket. Nineteen call sites keep
working unchanged because `Fanout` has a `send()`.

## The design, and the one thing that needed care

**One pipeline, N windows.** The LLM, the context and the voice are one. A second window is not a
second Asha — it is a second *view* of the same conversation, and it sees her replies because
every outbound frame is written to every socket.

`Fanout.send()` is therefore the whole feature: it writes to all live sockets and **prunes the
dead ones**, so a closed tab cannot make a send raise and take the turn down with it. That is
also why the 1001-induced 503 stops: a departed client is dropped, not failed.

**THE THING THAT WOULD HAVE BROKEN IT, and did not.** `_on_client_connected`
(server_run.py:3519) calls `context.set_messages([])` — on connect. With one client that is
correct: a refresh redraws and the history is re-requested. **With two clients, the second window
to connect would have wiped the shared conversation out from under the first one.** So the reset
is now gated on being the first client, and the comment at that line says why. I did not find
that by reading; I found it by asking what the second connect would do, which is the question
this module exists to make unnecessary to answer twice.

## What is NOT here, deliberately

- **Voice.** One voice port, one speaker. A second window cannot be talking out loud at the same
  time; that is a hardware fact, not a policy, and pretending otherwise would be a lie in a
  different place.
- **Separate conversations per window.** Both windows share one context on purpose. Per-window
  histories would mean two Ashas, and there is one.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class Fanout:
    """Stands in for "the socket" and writes to every live one.

    `server_run.py` reads `ws_holder["ws"]` in nineteen places and then calls `.send(...)`. This
    object has a `send()`, so none of those nineteen lines changed. **A refactor that touches
    nineteen call sites to add a feature is a refactor that will be got wrong**, and the count
    is the reason this is an object and not a loop.
    """

    def __init__(self) -> None:
        self._sockets: list[Any] = []

    # -- membership ---------------------------------------------------------------------
    def add(self, ws: Any) -> None:
        if ws not in self._sockets:
            self._sockets.append(ws)

    def discard(self, ws: Any) -> None:
        """Remove one socket. By identity, not equality — a websocket can be unhashable and two
        can compare equal, and dropping the wrong window is worse than dropping none."""
        self._sockets = [s for s in self._sockets if s is not ws]

    def __len__(self) -> int:
        return len(self._sockets)

    def __bool__(self) -> bool:
        return bool(self._sockets)

    @property
    def sockets(self) -> list[Any]:
        return list(self._sockets)

    def peers(self) -> list[str]:
        """Who is connected, for `/health`. The ADDRESS is the fact; a pid is not on the wire and
        guessing one from the peer port would be inventing a number."""
        return [str(getattr(s, "remote_address", "") or "") for s in self._sockets]

    # -- the two shims pipecat and our own code read off `self._websocket` ---------------
    @property
    def state(self):
        """OPEN while anyone is connected. pipecat's own checks are `if self._websocket and
        self._websocket.state is State.OPEN`, so this is the attribute that decides whether the
        transport believes it has a client."""
        from websockets.protocol import State  # local: keeps import cost off module load

        return State.OPEN if self._sockets else State.CLOSED

    @property
    def remote_address(self):
        return getattr(self._sockets[0], "remote_address", None) if self._sockets else None

    # -- writing ------------------------------------------------------------------------
    async def send(self, payload: Any) -> None:
        """To every live socket. A dead one is DROPPED, not raised.

        That is the whole 1001 fix. pipecat's own `_write_frame` wraps its single send in
        `except ConnectionClosed`, so a departed client cannot fail a frame there either — but
        ours can hold several, and one closed tab among three must not silence the other two, and
        must not turn a normal window close into a `pipeline error` and a 503.
        """
        for ws in list(self._sockets):
            try:
                await ws.send(payload)
            except Exception as exc:  # noqa: BLE001 - any failure here is that socket's problem
                if type(exc).__name__ != "ConnectionClosed":
                    logger.debug("fanout: dropping a socket that failed to accept a frame: %r", exc)
                self.discard(ws)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        for ws in list(self._sockets):
            try:
                await ws.close(code=code, reason=reason)
            except Exception:  # noqa: BLE001 - closing a closed socket is not an error worth raising
                pass
            finally:
                self.discard(ws)


def build(base_transport: type, input_base: type, output_base: type, fanout: Fanout):
    """Build the three classes that make pipecat serve N windows.

    **THREE, not one, and the third was the trap.** pipecat's `SingleClientWebsocketServerTransport`
    is the object the app constructs, and it builds its own two halves in `input()` / `output()`:

        def input(self)  -> SingleClientWebsocketServerInputTransport:   # hardcoded
        def output(self) -> SingleClientWebsocketServerOutputTransport:  # hardcoded

    So overriding the input half alone changes nothing — the app calls `transport.input()` and
    gets pipecat's single-client class anyway, with the `1013` still in it. **A subclass that
    looks right and is never used is the quietest possible failure**, and it is why
    `test_the_second_window_is_not_refused.py` drives two real sockets rather than asserting that
    a class exists.

    **A FACTORY, not module-level subclasses, and the reason matters.** pipecat is imported inside
    this call so the module can be imported and tested without it, and so each class is a genuine
    subclass of *whatever version is installed* rather than of a name this file remembered.

    The receive loop is pipecat's own body with ONE thing removed — the rejection — and that is
    the whole input-side change. Reimplemented rather than delegated, because delegating would run
    the rejection.
    """
    import websockets
    from pipecat.frames.frames import InputAudioRawFrame, InputTransportMessageFrame

    class MultiSessionInputTransport(input_base):  # type: ignore[misc, valid-base]
        """Accepts every client. pipecat's `_client_handler` minus the `1013`.

        pipecat 1.8.1 `transports/websocket/server.py:243-303`, unchanged except:
          * L251-256 (the rejection) — GONE. That is the feature.
          * L258 `self._websocket = websocket` — kept, so pipecat's own `state` checks and its
            keepalive still see a live client. The FIRST socket stays the primary; the rest are
            additional views of the same pipeline.
          * L300-301 (clear on disconnect) — kept, but only when it is still ours.
        """

        async def _client_handler(self, websocket):  # noqa: D401 - pipecat's own name
            peer = str(getattr(websocket, "remote_address", "") or "")
            if not getattr(self, "_websocket", None) or \
                    getattr(getattr(self._websocket, "state", None), "name", "") != "OPEN":
                self._websocket = websocket          # first client: the primary
            fanout.add(websocket)
            logger.info("multi-session: client connected from %s (%d window(s) open)",
                        peer, len(fanout))

            await self._callbacks.on_client_connected(websocket)

            if not self._monitor_task and self._params.session_timeout:
                self._monitor_task = self.create_task(
                    self._monitor_websocket(websocket, self._params.session_timeout)
                )

            try:
                async for message in websocket:
                    if not self._params.serializer:
                        continue
                    frame = await self._params.serializer.deserialize(message)
                    if not frame:
                        continue
                    if isinstance(frame, InputAudioRawFrame):
                        await self.push_audio_frame(frame)
                    elif isinstance(frame, InputTransportMessageFrame):
                        await self.broadcast_frame(
                            InputTransportMessageFrame, message=frame.message)
                    else:
                        await self.push_frame(frame)
            except websockets.ConnectionClosed as exc:
                # Logged WITH the close code, because "the receive loop just ended" is otherwise
                # indistinguishable from "this window went away" — and those are opposite faults.
                logger.info("multi-session: client %s closed (code=%s reason=%r)", peer,
                            getattr(getattr(exc, "rcvd", None), "code", None),
                            getattr(getattr(exc, "rcvd", None), "reason", None))
            except Exception as e:  # noqa: BLE001 - one bad window must not stop the others
                logger.error("multi-session: receiving from %s failed: %s (%s)",
                             peer, e.__class__.__name__, e)
            else:
                # A NORMAL exit from `async for` means the connection ended cleanly. Measured
                # 2026-09-30 with two windows: both closed ~1ms after the handshake and NEITHER
                # exception branch fired, so this line is the only thing that can say so, and
                # "it closed" is not a diagnosis.
                logger.info("multi-session: client %s receive loop ended normally", peer)

            try:
                await self._callbacks.on_client_disconnected(websocket)
            except Exception:  # noqa: BLE001 - a failing handler must still free the socket
                pass

            fanout.discard(websocket)
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass
            # Only clear the primary when it is still ours: another window may already be in.
            if self._websocket is websocket:
                self._websocket = fanout.sockets[0] if fanout.sockets else None
            logger.info("multi-session: client %s gone (%d window(s) open)",
                        peer, len(fanout))

    class MultiSessionOutputTransport(output_base):  # type: ignore[misc, valid-base]
        """Writes each frame to EVERY window instead of to one.

        pipecat 1.8.1 `transports/websocket/server.py:482-500 _write_frame`, with L494-495
        `await self._websocket.send(payload)` replaced by a fan-out. Everything else — the
        serializer, the `ConnectionClosed` tolerance, the success flag — is pipecat's.

        **`_write_frame` is a genuine override, not a monkey-patch**, so a future pipecat that
        changes the method shows up as a failing test rather than as a silent behaviour change.
        """

        async def set_client_connection(self, websocket) -> None:
            """REGISTER, NEVER EVICT. pipecat 1.8.1 `server.py:355-369`, with the close removed.

            **THIS WAS THE LAST REFUSAL, and it was not the one the first version of this change
            fixed.** pipecat's own comment says why it is there:

                # The input transport gates new connections, so by the time we set a new
                # client here the previous one (if any) is already gone. Close any stale
                # reference just in case before tracking the new connection.
                if self._websocket and self._websocket is not websocket:
                    await self._websocket.close()

            That comment is a statement about the *input* transport — and the first half of this
            work deleted the gate it depends on. So the output half went on evicting, and the
            symptom looked like my fan-out working: the second window connected cleanly, **both
            windows received the greeting**, and then the FIRST window's receive loop ended
            normally, 1ms after the second one arrived.

                06:32:11.038  client connected from 63972
                06:32:11.039  client 63970 receive loop ended normally

            Nobody was rejected. `Rejecting client` was 0. The product simply still had one
            client — enforced from the other side, by a method I had read and not changed. **This
            is the whole of tonight's fault class in one method: the read happened, and the
            change did not follow from it.**

            So: record the socket, close nothing. `_websocket` keeps pointing at the first one so
            pipecat's own `state` checks still see a live client.
            """
            if websocket is None:
                self._websocket = fanout.sockets[0] if fanout.sockets else None
                return
            if getattr(self, "_websocket", None) is None:
                self._websocket = websocket
            fanout.add(websocket)

        async def _write_frame(self, frame) -> bool:
            if not self._params.serializer:
                return False
            success = False
            try:
                payload = await self._params.serializer.serialize(frame)
                if payload and len(fanout):
                    await fanout.send(payload)
                    success = True
            except websockets.ConnectionClosed:
                logger.debug("multi-session: client disconnected while sending")
            return success

        # ─────────────────────────────────────────────────────────────────────────────
        # THE PACING SLEEP IS WHERE THE ROUGHNESS IS MADE.  Owner: *"wrong it's not set
        # right"*, 2026-10-02, after hearing it.
        #
        # pipecat 1.8.1 `transports/websocket/fastapi.py`, read in full:
        #
        #     setup()          self._send_interval = (self.audio_chunk_size
        #                                                / self.sample_rate) / 2     # :456
        #     _write_frame()   await self._write_audio_sleep()                        # :555
        #     _write_audio_sleep():
        #         """Simulate audio playback timing with appropriate delays."""
        #         # Simulate a clock.
        #         sleep_duration = max(0, self._next_send_time - current_time)
        #         await asyncio.sleep(sleep_duration)
        #
        # SO: after every frame the transport sleeps HALF the frame's own duration. With a
        # 100 ms frame that is ~50 ms of sleep per ~101 ms of audio, so audio is delivered at
        # 101 / 151 = **0.67x real time** while the browser plays it at 1.0x. **The missing
        # third has to come from somewhere, and it comes from silence the browser inserts.**
        #
        # MEASURED, in a real visible browser, `window.__ashaGaps` — not inferred:
        #
        #     buffers 44   mean gap 22.67 ms   worst 22.67 ms
        #     last 10 gaps [22.67, 22.67, 22.67, 22.67, ... ]   steps > 5ms: 0 of 42
        #
        # 22.67 ms is 544 samples at 24 kHz, **identical on every single boundary**. That is a
        # clock, not jitter, and it is the sound the owner called "not set right".
        #
        # WHY CHANGING THE FRAME SIZE DID NOT HELP, and it is the reason this took a browser to
        # find: `_send_interval` is computed FROM the chunk size, so `/2` means half the sleep is
        # always added back. Bigger frames gave a bigger sleep. Both sides scaled together and
        # the mean gap stayed put — which is exactly what the A/B measured:
        #
        #     ASHA_AUDIO_CHUNK_10MS=4    336 frames  mean 15.90 ms  worst 4560 ms
        #     ASHA_AUDIO_CHUNK_10MS=10    96 frames  mean 16.67 ms  worst   24 ms
        #
        # **WHAT THIS OVERRIDE DOES: it does not sleep, so frames go out as fast as the model
        # produces them.** Measured generation is **0.41x real time** — Kokoro makes 8.1 s of
        # audio in 3.3 s — so the whole utterance now arrives in about a third of the wall time
        # it takes to play, and the browser's own playhead simply runs ahead of the clock instead
        # of behind it. `PLAY_LEAD = 0.12` already bounds how far ahead, and `drain()` stops
        # filling once it is, so this cannot grow without limit.
        #
        # **THE COST, stated because it is real.** Sending as fast as produced means the audio
        # arrives in a burst, so the first sound waits for the model's first chunk (measured TTFB
        # 1.9 s for a 10 s sentence) instead of starting sooner. That latency is the trade for
        # removing a 22.67 ms hole ~10 times a second, and it is why this is behind a flag rather
        # than a deletion.
        #
        # ASHA_PACE_AUDIO=0 restores pipecat's pacing exactly, one restart apart.
        #
        # This is a genuine override, like `_write_frame` above, so a future pipecat that renames
        # or re-points the method shows up as a failing test rather than as silence.
        async def _write_audio_sleep(self):
            if os.environ.get("ASHA_PACE_AUDIO", "0") == "1":
                return await super()._write_audio_sleep()
            return None

    class MultiSessionTransport(base_transport):  # type: ignore[misc, valid-base]
        """The object the app actually constructs, so the two halves above are the ones it uses.

        Without these two overrides the whole change is inert: the app calls `transport.input()`
        and pipecat hands back its own single-client class, so the `1013` stays and the refusal
        stays, and nothing anywhere reports a problem.

        **THE ARGUMENTS ARE COPIED, NOT REMEMBERED.** pipecat 1.8.1:

            self._input  = SingleClientWebsocketServerInputTransport(
                self, self._host, self._port, self._params, self._callbacks, name=self._input_name)
            self._output = SingleClientWebsocketServerOutputTransport(
                self, self._params, name=self._output_name)

        I first wrote `params=..., client_ping_timeout=...` from memory — **an argument list I had
        not read** — and the process died with `AttributeError: 'MultiSessionTransport' object has
        no attribute '_client_ping_timeout'`. That is the whole of tonight's fault in one line:
        plausible code that looked right and was a guess dressed as a copy. A verbatim copy fails
        LOUDLY the moment pipecat changes the signature; a remembered one fails quietly later,
        somewhere else, in a sentence nobody wrote.
        """

        def input(self):  # type: ignore[no-untyped-def]
            if not self._input:
                self._input = MultiSessionInputTransport(
                    self, self._host, self._port, self._params, self._callbacks,
                    name=self._input_name)
            return self._input

        def output(self):  # type: ignore[no-untyped-def]
            if not self._output:
                self._output = MultiSessionOutputTransport(
                    self, self._params, name=self._output_name)
            return self._output

    # `MultiSessionOutputTransport` is nested in this function because it closes over `fanout`,
    # which is why it cannot be imported by name. `prototype/ui/test_the_pacing_sleep_is_gone.py`
    # needs it to prove the pacing sleep is really gone, and a test that has to dig a nested class
    # out of a closure is a test that is not reading the object the app runs.
    MultiSessionTransport.output_transport_class = MultiSessionOutputTransport

    return MultiSessionTransport
