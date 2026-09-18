"""OpenAI-compatible providers: opencode (zen), openrouter, openai, gemini,
groq, xai, and local servers (LM Studio / vLLM / llama.cpp, etc).

All of these speak the chat/completions wire format; they differ only in
base_url, api key, and (for opencode/zen) the extra x-opencode-* headers the
CLI sets. Responses are already OpenAI-shaped, so `_normalize_response` only
unwraps the optional `{data: {...}}` envelope some gateways add.
"""

from __future__ import annotations

import json

import httpx

from .base import Provider, ProviderError


class OpenAICompatibleProvider(Provider):
    provider_id = "openai-compatible"
    name = "OpenAI Compatible"
    default_model = ""
    base_url = ""
    # Headers to merge over the standard Authorization, e.g. opencode's
    # x-opencode-* set. Set by subclasses/factories.
    extra_headers: dict[str, str] = {}

    def chat(
        self,
        messages: list[dict],
        model: str,
        tools: list[dict] | None = None,
        *,
        session_id: str = "",
        timeout: float = 300.0,
        reasoning: str = "",
        max_tokens: int | None = None,
    ) -> dict:
        if self.requires_key and not self.api_key:
            raise ProviderError(f"{self.name}: no API key configured", 401, self.provider_id)

        payload: dict = {"model": model, "messages": self._native_messages(messages)}
        native_tools = self._native_tools(tools)
        if native_tools:
            payload["tools"] = native_tools
        if reasoning and reasoning != "default":
            payload["reasoning_effort"] = reasoning
        if max_tokens:
            payload["max_tokens"] = max_tokens

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }

        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                resp = client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
        except httpx.HTTPError as e:
            raise ProviderError(f"{self.name} transport error: {e!r}", 0, self.provider_id)

        if resp.status_code == 401:
            raise ProviderError(f"{self.name} auth failed — check key", 401, self.provider_id)
        if resp.status_code == 429:
            raise ProviderError(f"{self.name} rate-limited (429)", 429, self.provider_id)
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json()
                detail = (body.get("error") or {}).get("message", "")[:200]
            except Exception:
                pass
            raise ProviderError(f"{self.name} HTTP {resp.status_code}: {detail}",
                                resp.status_code, self.provider_id)
        try:
            body = resp.json()
        except ValueError:
            raise ProviderError(f"{self.name} returned non-JSON", resp.status_code, self.provider_id)
        return self._normalize_response(body, model)

    def _extra_dynamic_headers(self) -> dict:
        """Per-request headers (overridden by providers that need them)."""
        return {}

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
            **self._extra_dynamic_headers(),
        }

    def stream(self, messages: list[dict], model: str, tools: list[dict] | None = None,
               *, timeout: float = 300.0, reasoning: str = ""):
        """Yield OpenAI-shaped SSE chunk dicts (content deltas + tool calls).

        Raises ProviderError on auth/rate/HTTP/transport failures.
        """
        if self.requires_key and not self.api_key:
            raise ProviderError(f"{self.name}: no API key configured", 401, self.provider_id)
        payload: dict = {"model": model, "messages": self._native_messages(messages),
                         "stream": True,
                         "stream_options": {"include_usage": True}}
        native_tools = self._native_tools(tools)
        if native_tools:
            payload["tools"] = native_tools
        if reasoning and reasoning != "default":
            payload["reasoning_effort"] = reasoning
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                with client.stream("POST", f"{self.base_url}/chat/completions",
                                   json=payload, headers=self._headers()) as resp:
                    if resp.status_code == 401:
                        raise ProviderError(f"{self.name} auth failed — check key",
                                            401, self.provider_id)
                    if resp.status_code == 429:
                        raise ProviderError(f"{self.name} rate-limited (429)",
                                            429, self.provider_id)
                    if resp.status_code >= 400:
                        resp.read()
                        raise ProviderError(f"{self.name} HTTP {resp.status_code}",
                                            resp.status_code, self.provider_id)
                    for line in resp.iter_lines():
                        if not line:
                            continue
                        if line.startswith("data:"):
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            try:
                                yield json.loads(data)
                            except ValueError:
                                continue
        except httpx.HTTPError as e:
            raise ProviderError(f"{self.name} transport error: {e!r}", 0, self.provider_id)


# ── opencode / zen (needs opencode CLI headers) ──────────────────────────────

class OpenCodeProvider(OpenAICompatibleProvider):
    """opencode.ai zen — free + go tiers. Reuses the validated CLI's header
    set (x-opencode-project/session/request/client + UA) which the gateway
    requires; the workspace/session ids are minted once at process start the
    same way worker_engine.py always did."""

    provider_id = "opencode"
    name = "OpenCode Zen"
    default_model = "big-pickle"

    def __init__(self, api_key: str = "", workspace_id: str = ""):
        super().__init__(api_key=api_key)
        self.workspace_id = workspace_id or _oc_ulid("wrk")
        # One session per provider instance (a conversation), like the CLI —
        # a stable x-opencode-session is what lets the gateway reuse the
        # upstream prompt cache; minting a new one per dispatch forces 0% cache.
        self.session_id = _oc_ulid("ses")
        self.extra_headers = {
            "x-opencode-project": self.workspace_id,
            "x-opencode-client": "cli",
            "User-Agent": "opencode/1.18.29",  # matches installed validated client
        }

    def _extra_dynamic_headers(self) -> dict:
        # Stable session (cache-friendly); fresh request id per dispatch.
        return {
            "x-opencode-session": self.session_id,
            "x-opencode-request": _oc_ulid("msg"),
        }

    def chat(self, messages, model, tools=None, *, session_id="", timeout=300.0,
             reasoning=""):
        if self.requires_key and not self.api_key:
            raise ProviderError(f"{self.name}: no API key configured", 401, self.provider_id)

        payload: dict = {"model": model, "messages": self._native_messages(messages)}
        native_tools = self._native_tools(tools)
        if native_tools:
            payload["tools"] = native_tools
        if reasoning and reasoning != "default":
            payload["reasoning_effort"] = reasoning

        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                resp = client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=self._headers(),
                )
        except httpx.HTTPError as e:
            raise ProviderError(f"{self.name} transport error: {e!r}", 0, self.provider_id)

        if resp.status_code == 401:
            raise ProviderError(f"{self.name} auth failed — check OPENCODE_API_KEY", 401, self.provider_id)
        if resp.status_code == 429:
            raise ProviderError(f"{self.name} rate-limited (429)", 429, self.provider_id)
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json()
                detail = (body.get("error") or {}).get("message", "")[:200]
            except Exception:
                pass
            raise ProviderError(f"{self.name} HTTP {resp.status_code}: {detail}",
                                resp.status_code, self.provider_id)
        try:
            body = resp.json()
        except ValueError:
            raise ProviderError(f"{self.name} returned non-JSON", resp.status_code, self.provider_id)
        return self._normalize_response(body, model)


class OpenCodeGoProvider(OpenCodeProvider):
    """opencode zen go — cheap paid twin (answer-only fallback). Supports the
    same header set; only the base_url + billing differ."""

    provider_id = "opencode-go"
    name = "OpenCode Go"
    default_model = "mimo-v2.5"


# ── opencode ulid helpers (from worker_engine.py, kept local) ───────────────

_OC_LAST_MILLIS: list[int] = [0]
_OC_COUNTER: list[int] = [0]


def _oc_ulid_counter() -> int:
    import time
    millis = int(time.time() * 1000)
    if millis != _OC_LAST_MILLIS[0]:
        _OC_LAST_MILLIS[0] = millis
        _OC_COUNTER[0] = 0
    _OC_COUNTER[0] += 1
    return _OC_COUNTER[0]


def _oc_random_base62(length: int) -> str:
    import os
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    return "".join(chars[byte % 62] for byte in os.urandom(length))


def _oc_ulid(prefix: str, descending: bool = False) -> str:
    import time
    millis = int(time.time() * 1000)
    value = (millis * 0x1000 + _oc_ulid_counter()) & 0xFFFFFFFFFFFF
    if descending:
        value = (~value) & 0xFFFFFFFFFFFF
    time_hex = value.to_bytes(6, "big").hex()
    random_part = _oc_random_base62(14)
    return f"{prefix}_{time_hex}{random_part}"