"""Provider abstraction layer for Jarvis workers.

Every provider normalizes its chat-completion response into the **OpenAI
chat.completions shape** ({choices:[{message,finish_reason}], usage}) so the
agent loop in worker_engine.py (which extracts text / tool_calls / usage from
that shape) is provider-agnostic. Tool definitions are also accepted in OpenAI
function-calling format and converted to each provider's native schema.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any


class ProviderError(RuntimeError):
    """Raised for auth failures, rate limits, and HTTP errors.

    ``code`` mirrors the HTTP status when we have one (401/429/5xx/0).
    """

    def __init__(self, message: str, code: int = 0, provider_id: str = ""):
        super().__init__(message)
        self.code = code
        self.provider_id = provider_id


@dataclass(frozen=True)
class ModelInfo:
    """Static catalog entry for a provider model (for pickers / info)."""

    id: str
    provider_id: str
    name: str = ""
    # USD per 1M tokens; (0, 0) means unknown/free-ish
    input_cost_m: float = 0.0
    output_cost_m: float = 0.0
    tier: str = "paid"  # "free" | "paid"
    supports_tools: bool = True
    context_tokens: int = 0
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "provider_id": self.provider_id,
            "name": self.name,
            "input_cost_m": self.input_cost_m,
            "output_cost_m": self.output_cost_m,
            "tier": self.tier,
            "supports_tools": self.supports_tools,
            "context_tokens": self.context_tokens,
            "notes": self.notes,
        }


class Provider(abc.ABC):
    """One configured upstream (base URL + api key + model catalog)."""

    provider_id = ""
    name = ""
    default_model = ""
    # Interval of currencies the provider charges in (for cost math)
    requires_key = True
    key_env: str = ""

    def __init__(self, api_key: str = ""):
        self.api_key = api_key

    def is_configured(self) -> bool:
        return not self.requires_key or bool(self.api_key)

    def models(self) -> list[ModelInfo]:
        override = getattr(self, "_models_override", None)
        if override is not None:
            return override
        return _CATALOG.get(self.provider_id, [])

    def info(self) -> dict:
        d = {
            "provider_id": self.provider_id,
            "name": self.name,
            "default_model": self.default_model,
            "configured": self.is_configured(),
            "models": [m.to_dict() for m in self.models()],
        }
        cid = getattr(self, "_custom_id", "")
        if cid:  # user-defined OpenAI-compatible endpoint
            d["custom"] = True
            d["id"] = cid
            d["base_url"] = getattr(self, "base_url", "")
        return d

    @abc.abstractmethod
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
        """One non-streaming turn. Returns OpenAI-shaped response dict.

        Raises ProviderError on any failure (auth, 429, HTTP, non-JSON).
        """
        raise NotImplementedError

    #
    # OpenAI → provider-native conversions used by subclasses.
    #

    def _native_tools(self, tools: list[dict] | None) -> Any:
        """Translate OpenAI function-calling tools to this provider's schema.

        Default: pass through unchanged (OpenAI-compatible providers).
        """
        return tools

    def _native_messages(self, messages: list[dict]) -> Any:
        """Translate OpenAI role/content/tool_calls history to this provider.

        Default: pass through unchanged (OpenAI-compatible providers).
        """
        return messages

    def _normalize_response(self, body: Any, model: str) -> dict:
        """Each provider maps its native response to the OpenAI shape here.

        Default: assume the provider already returned the OpenAI shape
        (OpenAI-compatible providers do).
        """
        if isinstance(body, dict) and isinstance(body.get("data"), dict):
            body = body["data"]
        return body


# Per-provider static catalogs (edited as models change). Filled by registry.py.
_CATALOG: dict[str, list[ModelInfo]] = {}


def register_catalog(provider_id: str, models: list[ModelInfo]):
    _CATALOG[provider_id] = list(models)