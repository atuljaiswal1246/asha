"""Provider config: which provider/model the brain and the workers use.

Selection is env-driven with safe defaults (opencode/big-pickle — the
existing free zen defaults), so the system boots identically without any
.env changes. Env vars (all optional):

  PROVIDER_BRAIN     provider_id for the brain          (default: opencode)
  PROVIDER_BRAIN_MODEL  model id for the brain          (default: big-pickle)
  PROVIDER_WORKER    provider_id for the workers        (default: opencode)
  PROVIDER_WORKER_MODEL model id for the workers        (default: big-pickle)

All 3 workers share the same worker provider/model; the brain shares the same
project folder as everyone (system-wide). A single `PROVIDER`/`PROVIDER_MODEL`
override sets both brain and workers at once.
"""

from __future__ import annotations

import os

from .base import ProviderError
from .registry import get_provider as _get_provider, list_providers as _list_providers


class ProviderConfig:
    def __init__(self):
        self.brain_provider = (os.environ.get("PROVIDER_BRAIN", "") or
                               os.environ.get("PROVIDER", "") or "opencode").strip().lower()
        self.brain_model = (os.environ.get("PROVIDER_BRAIN_MODEL", "") or
                            os.environ.get("PROVIDER_MODEL", "") or "big-pickle").strip()
        self.worker_provider = (os.environ.get("PROVIDER_WORKER", "") or
                                os.environ.get("PROVIDER", "") or "opencode").strip().lower()
        self.worker_model = (os.environ.get("PROVIDER_WORKER_MODEL", "") or
                             os.environ.get("PROVIDER_MODEL", "") or "big-pickle").strip()

    def to_dict(self) -> dict:
        return {
            "brain": {"provider": self.brain_provider, "model": self.brain_model},
            "worker": {"provider": self.worker_provider, "model": self.worker_model},
        }

    def update(self, brain_provider: str | None = None, brain_model: str | None = None,
               worker_provider: str | None = None, worker_model: str | None = None):
        """Validate + apply a config change (in-memory, runtime only)."""
        if brain_provider is not None:
            _get_provider(brain_provider)  # raises on unknown id
            self.brain_provider = brain_provider.strip().lower()
        if brain_model is not None:
            _get_provider(self.brain_provider)  # keep model tied to a live provider
            self.brain_model = brain_model.strip()
        if worker_provider is not None:
            _get_provider(worker_provider)
            self.worker_provider = worker_provider.strip().lower()
        if worker_model is not None:
            _get_provider(self.worker_provider)
            self.worker_model = worker_model.strip()
        return self.to_dict()


# Global single instance (the system always has ONE active config).
CONFIG = ProviderConfig()


def get_provider(provider_id: str):
    """Registry passthrough (raises ProviderError on unknown/misconfigured)."""
    try:
        return _get_provider(provider_id)
    except ValueError as e:
        raise ProviderError(str(e), 0, provider_id)


def chat(provider_id: str, model: str, messages: list[dict], tools=None, *,
         timeout: float = 300.0, max_tokens: int | None = None) -> dict:
    """One-shot chat completion routed by provider_id (OpenAI-shaped response).

    Stand-in for worker_engine._chat_completion: the engine passes its session
    through so opencode/zen can mint the per-run headers.
    """
    provider = _get_provider(provider_id or "opencode")
    if not provider.is_configured():
        raise ProviderError(f"{provider.name}: missing API key", 401, provider_id)
    return provider.chat(messages, model=model, tools=tools, timeout=timeout,
                         max_tokens=max_tokens)


def list_providers() -> list[dict]:
    return _list_providers()


def list_models(provider_id: str) -> list[dict]:
    from .registry import list_models as lm
    return lm(provider_id)