"""Channel gateway (G3): reach Jarvis from chat platforms.

A channel is any object with ``get_updates(offset)`` and ``send(chat_id, text)``;
:class:`Gateway` polls it and routes each message through a handler, sending the
reply back. The concrete :class:`TelegramChannel` talks to the Telegram Bot API
over stdlib HTTP, but takes an injectable ``transport`` so it can be tested
without a network or a real bot token.

Wiring to Jarvis's brain is opt-in (``TELEGRAM_TOKEN`` + ``CHANNELS_ENABLED=1``);
the module itself is transport-agnostic.
"""
from __future__ import annotations

import json
import urllib.request


class TelegramChannel:
    """Telegram Bot API client (sendMessage / getUpdates)."""

    def __init__(self, token: str, transport=None):
        self.token = token
        self._transport = transport or self._http

    def _http(self, method: str, payload: dict) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def send(self, chat_id, text: str) -> dict:
        return self._transport("sendMessage", {"chat_id": chat_id, "text": text})

    def get_updates(self, offset=None, timeout: int = 25) -> dict:
        payload: dict = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        return self._transport("getUpdates", payload)


class Gateway:
    """Poll a channel and answer each message with ``handler(text)``."""

    def __init__(self, channel, handler, allow=None):
        self._channel = channel
        self._handler = handler
        self._allow = set(allow) if allow else None
        self._offset = None
        self.last_error: str | None = None

    def poll_once(self) -> int:
        """Process one batch of updates; returns how many replies were sent."""
        try:
            res = self._channel.get_updates(self._offset) or {}
        except Exception as e:  # noqa: BLE001 - keep the gateway alive
            self.last_error = str(e)
            return 0
        sent = 0
        for update in res.get("result", []):
            self._offset = max(self._offset or 0, update.get("update_id", 0) + 1)
            message = update.get("message") or update.get("edited_message") or {}
            text = (message.get("text") or "").strip()
            chat_id = (message.get("chat") or {}).get("id")
            if not text or chat_id is None:
                continue
            if self._allow is not None and chat_id not in self._allow:
                continue
            try:
                reply = self._handler(text)
            except Exception as e:  # noqa: BLE001
                reply = f"[error] {e}"
            if reply:
                try:
                    self._channel.send(chat_id, str(reply)[:4000])
                    sent += 1
                except Exception as e:  # noqa: BLE001
                    self.last_error = str(e)
        return sent


class FakeChannel:
    """In-memory channel for tests: queue updates, record sent messages."""

    def __init__(self, updates=None):
        self.updates = list(updates or [])
        self.sent: list[tuple] = []
        self._consumed = False

    def get_updates(self, offset=None, timeout: int = 0):
        if self._consumed:
            return {"result": []}
        self._consumed = True
        return {"result": self.updates}

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))
        return {"ok": True}
