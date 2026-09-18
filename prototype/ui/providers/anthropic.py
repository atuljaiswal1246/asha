"""Native Anthropic Messages API provider.

Anthropic's wire format differs from OpenAI's (POST /v1/messages, x-api-key +
anthropic-version headers, system as a top-level param, content blocks, and a
max_tokens requirement). This provider transparently converts:
  - OpenAI tool defs -> {name, description, input_schema}
  - OpenAI history -> {system, messages} with tool_use / tool_result blocks
  - native response -> OpenAI chat.completions shape (choices/usage), so the
    worker_engine agent loop needs zero provider-specific code.
"""

from __future__ import annotations

import json

import httpx

from .base import Provider, ProviderError

DEFAULT_ANTHROPIC_BASE = "https://api.anthropic.com"
DEFAULT_MAX_TOKENS = 4096
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(Provider):
    provider_id = "anthropic"
    name = "Anthropic"
    default_model = "claude-sonnet-4-5"
    base_url = DEFAULT_ANTHROPIC_BASE
    key_env = "ANTHROPIC_API_KEY"
    max_tokens = DEFAULT_MAX_TOKENS

    def chat(
        self,
        messages: list[dict],
        model: str,
        tools: list[dict] | None = None,
        *,
        session_id: str = "",
        timeout: float = 300.0,
        max_tokens: int | None = None,
    ) -> dict:
        if not self.api_key:
            raise ProviderError("Anthropic: no API key configured", 401, self.provider_id)

        system, native_messages = self._messages_to_anthropic(messages)

        payload: dict = {
            "model": model,
            "max_tokens": max_tokens or self.max_tokens,
            "messages": native_messages,
        }
        if system:
            payload["system"] = system
        native_tools = self._native_tools(tools)
        if native_tools:
            payload["tools"] = native_tools

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }

        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                resp = client.post(
                    f"{self.base_url}/v1/messages",
                    json=payload,
                    headers=headers,
                )
        except httpx.HTTPError as e:
            raise ProviderError(f"Anthropic transport error: {e!r}", 0, self.provider_id)

        if resp.status_code == 401:
            raise ProviderError("Anthropic auth failed — check ANTHROPIC_API_KEY", 401, self.provider_id)
        if resp.status_code == 429:
            raise ProviderError("Anthropic rate-limited (429)", 429, self.provider_id)
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json()
                detail = (body.get("error") or {}).get("message", "")[:200]
            except Exception:
                pass
            raise ProviderError(f"Anthropic HTTP {resp.status_code}: {detail}",
                                resp.status_code, self.provider_id)
        try:
            body = resp.json()
        except ValueError:
            raise ProviderError("Anthropic returned non-JSON", resp.status_code, self.provider_id)
        return self._normalize_response(body, model)

    # ── conversions ─────────────────────────────────────────────────────────

    def _native_tools(self, tools: list[dict] | None) -> list[dict] | None:
        if not tools:
            return tools
        native = []
        for t in tools:
            fn = t.get("function") or {}
            native.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            })
        return native

    def _messages_to_anthropic(self, messages: list[dict]) -> tuple[str, list[dict]]:
        """Split OpenAI history into (system_text, anthropic messages)."""
        system_parts: list[str] = []
        native: list[dict] = []

        for m in messages:
            role = m.get("role")
            content = m.get("content")

            if role == "system":
                if content:
                    system_parts.append(content)
                continue

            if role == "tool":
                # OpenAI tool result -> anthropic tool_result block (user turn)
                tool_call_id = m.get("tool_call_id", "")
                native.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_call_id,
                        "content": (content or "")[:6000],
                    }],
                })
                continue

            # user / assistant text + optional tool_calls
            blocks: list[dict] = []
            if content:
                blocks.append({"type": "text", "text": content})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    arguments = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "input": arguments,
                })
            if blocks:
                native.append({"role": role if role in ("user", "assistant") else "user",
                               "content": blocks})

        return "\n\n".join(system_parts), native

    def _normalize_response(self, body: dict, model: str) -> dict:
        """Anthropic -> OpenAI chat.completions shape."""
        content_blocks = body.get("content") or []
        text_parts = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
        text = " ".join(p for p in text_parts if p)

        tool_calls = []
        for b in content_blocks:
            if b.get("type") != "tool_use":
                continue
            tool_calls.append({
                "id": b.get("id", ""),
                "type": "function",
                "function": {
                    "name": b.get("name", ""),
                    "arguments": json.dumps(b.get("input") or {}),
                },
            })

        stop = body.get("stop_reason", "")
        finish_reason = {"end_turn": "stop", "stop_sequence": "stop",
                         "max_tokens": "length", "tool_use": "tool_calls"}.get(stop, stop)

        usage = body.get("usage") or {}
        return {
            "id": body.get("id", ""),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text,
                    "tool_calls": tool_calls or None,
                },
                "finish_reason": finish_reason,
            }],
            "usage": {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
            },
        }