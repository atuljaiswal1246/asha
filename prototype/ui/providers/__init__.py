"""Jarvis native provider layer.

Talk to opencode (zen), openrouter, openai, anthropic, gemini, groq, xai, and
local OpenAI-compatible servers through one normalized interface. Responses
are always OpenAI chat.completions-shaped so the worker engine stays
provider-agnostic.

    from providers import get_provider, chat, list_providers, ProviderConfig, CONFIG
"""

from .base import ModelInfo, Provider, ProviderError, register_catalog
from .registry import custom_providers, get_provider, list_models, list_providers as ls_providers, reset_cache  # noqa: F401
from .config import CONFIG, ProviderConfig, chat  # noqa: F401

list_providers = ls_providers

__all__ = [
    "ModelInfo",
    "Provider",
    "ProviderError",
    "ProviderConfig",
    "CONFIG",
    "chat",
    "get_provider",
    "custom_providers",
    "list_models",
    "list_providers",
    "reset_cache",
]