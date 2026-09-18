"""Provider registry: catalogs + env-driven factory.

Catalogs are informational (pickers / cost display). The engine never
validates a model slug against a catalog — any string is passed through, so a
provider update doesn't require a code change.

Env wiring (all read via config.py, keys documented in prototype/.env):
  - opencode       OPENCODE_API_KEY         (free zen pool)
  - opencode-go    SUPERVISOR_API_KEY        (paid zen twin)
  - openrouter     OPENROUTER_API_KEY        (fallback: CLOUD_OR_*)
  - openai         OPENAI_API_KEY
  - anthropic      ANTHROPIC_API_KEY
  - gemini         GEMINI_API_KEY
  - groq           GROQ_API_KEY              (fallback: CLOUD_GROQ_*)
  - xai            XAI_API_KEY
  - local          (no key; LOCAL_BASE_URL, default http://127.0.0.1:8080/v1)
"""

from __future__ import annotations

import os
from pathlib import Path

from .base import ModelInfo, Provider, register_catalog
from .openai_compatible import OpenAICompatibleProvider, OpenCodeGoProvider, OpenCodeProvider
from .anthropic import AnthropicProvider, DEFAULT_ANTHROPIC_BASE
from .runtime import CustomProviders

ZEN_BASE = "https://opencode.ai/zen/v1"
GO_BASE = "https://opencode.ai/zen/go/v1"

# User-defined OpenAI-compatible endpoints (BYOK "any platform"), stored in
# the resolved data dir (JARVIS_DATA_DIR in a packaged app) so a shipped app
# never writes into its own bundle.
import jarvis_paths  # noqa: E402

_DATA_DIR = jarvis_paths.data_dir()
_CUSTOM = CustomProviders(_DATA_DIR / "providers.json")

_PROVIDER_NAMES = {
    "opencode": "OpenCode Zen", "opencode-go": "OpenCode Go",
    "openrouter": "OpenRouter", "openai": "OpenAI", "anthropic": "Anthropic",
    "gemini": "Gemini", "groq": "Groq", "xai": "xAI", "local": "Local",
    "jarvis": "Jarvis",
}


def _jarvis_token() -> str:
    """The plan token: env JARVIS_TOKEN, else the provisioned file."""
    token = (os.environ.get("JARVIS_TOKEN") or "").strip()
    if token:
        return token
    try:
        return (_DATA_DIR / "gateway-token").read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        return ""


def jarvis_url() -> str:
    """Gateway base (no /v1), e.g. http://host:8200. Empty when unset."""
    return (os.environ.get("JARVIS_GATEWAY_URL") or "").rstrip("/")


def custom_providers() -> CustomProviders:
    return _CUSTOM


# ── Catalogs (informational) ─────────────────────────────────────────────────

def _m(provider_id, model_id, name, input_cost, output_cost, tier,
        supports_tools=True, context_tokens=0, notes=""):
    return ModelInfo(id=model_id, provider_id=provider_id, name=name,
                     input_cost_m=input_cost, output_cost_m=output_cost,
                     tier=tier, supports_tools=supports_tools,
                     context_tokens=context_tokens, notes=notes)


register_catalog("opencode", [
    _m("opencode", "big-pickle", "big-pickle (zen free)", 0.0, 0.0, "free", notes="default worker model"),
    _m("opencode", "mimo-v2.5-free", "mimo-v2.5 free (zen)", 0.0, 0.0, "free", context_tokens=137_000),
    _m("opencode", "deep-pickle", "deep-pickle (zen)", 0.0, 0.0, "free", notes="brain-only, never muscle"),
])

register_catalog("opencode-go", [
    _m("opencode-go", "mimo-v2.5", "mimo-v2.5 (go paid)", 0.14, 0.28, "paid", context_tokens=137_000),
    _m("opencode-go", "deepseek-v4.1-flash", "DeepSeek V4.1 Flash (go paid)", 0.22, 0.66, "paid",
       context_tokens=137_000, notes="capable coding brain; default go model"),
])

register_catalog("jarvis", [
    _m("jarvis", "deepseek-v4.1-flash", "DeepSeek V4.1 Flash (plan)", 0, 0, "plan"),
    _m("jarvis", "mimo-v2.5", "MiMo V2.5 (plan)", 0, 0, "plan"),
])

register_catalog("openrouter", [
    _m("openrouter", "openrouter/auto", "OpenRouter Auto", 0.0, 0.0, "paid", supports_tools=True,
       notes="auto-routes to the cheapest capable model"),
    _m("openrouter", "openai/gpt-4o", "OpenAI GPT-4o", 2.5, 10.0, "paid"),
    _m("openrouter", "anthropic/claude-sonnet-4", "Claude Sonnet 4", 3.0, 15.0, "paid"),
])

register_catalog("openai", [
    _m("openai", "gpt-4o", "GPT-4o", 2.5, 10.0, "paid", context_tokens=128_000),
    _m("openai", "gpt-4o-mini", "GPT-4o mini", 0.15, 0.60, "paid", context_tokens=128_000),
    _m("openai", "o3-mini", "o3-mini", 1.10, 4.40, "paid", context_tokens=200_000),
])

register_catalog("anthropic", [
    _m("anthropic", "claude-sonnet-4-5", "Claude Sonnet 4.5", 3.0, 15.0, "paid", context_tokens=200_000),
    _m("anthropic", "claude-haiku-4-5", "Claude Haiku 4.5", 1.0, 5.0, "paid", context_tokens=200_000),
    _m("anthropic", "claude-opus-4-1", "Claude Opus 4.1", 15.0, 75.0, "paid", context_tokens=200_000),
])

register_catalog("gemini", [
    _m("gemini", "gemini-2.0-flash", "Gemini 2.0 Flash", 0.10, 0.40, "paid", context_tokens=1_000_000),
    _m("gemini", "gemini-2.0-flash-lite", "Gemini 2.0 Flash-Lite", 0.075, 0.30, "paid", context_tokens=1_000_000),
    _m("gemini", "gemini-2.5-pro", "Gemini 2.5 Pro", 1.25, 10.0, "paid", context_tokens=1_000_000),
])

register_catalog("groq", [
    _m("groq", "llama-3.3-70b-versatile", "Llama 3.3 70B", 0.59, 0.79, "paid", context_tokens=131_072),
    _m("groq", "llama-3.1-8b-instant", "Llama 3.1 8B", 0.05, 0.08, "paid", context_tokens=131_072),
])

register_catalog("xai", [
    _m("xai", "grok-3", "Grok 3", 3.0, 15.0, "paid"),
    _m("xai", "grok-3-mini", "Grok 3 mini", 0.30, 0.50, "paid"),
])

register_catalog("local", [
    _m("local", "local-default", "Local server model", 0.0, 0.0, "free",
       notes="set by LOCAL_BASE_URL; model id passed through as typed"),
])


# ── Factory ──────────────────────────────────────────────────────────────────

_INSTANCES: dict[str, Provider] = {}


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name, "")
    v = v or os.environ.get(f"{name}_ALT", "")
    return v.strip() or default


def get_provider(provider_id: str) -> Provider:
    """Build (and cache) the configured provider instance for *provider_id*.

    Instances are cached so opencode's per-process workspace/session ids stay
    stable. Raise ValueError for unknown ids. Returned provider.is_configured()
    tells the caller whether the matching API key exists in env.
    """
    pid = (provider_id or "").lower()
    cached = _INSTANCES.get(pid)
    if cached is not None:
        return cached

    instance = _build_provider(pid)
    _INSTANCES[pid] = instance
    return instance


def reset_cache():
    """Clear cached instances (tests / config reload)."""
    _INSTANCES.clear()


def _build_provider(pid: str) -> Provider:
    if pid.startswith("custom:"):
        cid = pid.split(":", 1)[1]
        entry = _CUSTOM.get(cid)
        if entry is None:
            raise ValueError(f"unknown custom provider: {cid!r}")
        p = OpenAICompatibleProvider(api_key=entry.get("api_key", ""))
        p.provider_id = pid
        p.name = entry.get("name") or cid
        p.base_url = entry["base_url"]
        p.requires_key = True
        p.key_env = "CUSTOM_API_KEY"
        p._custom_id = cid
        p._models_override = [ModelInfo(id=m, provider_id=pid, name=m)
                              for m in (entry.get("models") or [])]
        return p

    if pid == "jarvis":
        base = jarvis_url() or "http://127.0.0.1:8200"
        if not base.endswith("/v1"):
            base = base + "/v1"
        p = _compat("jarvis", _jarvis_token(), base)
        p.key_env = "JARVIS_TOKEN"
        return p

    if pid == "opencode":
        p = OpenCodeProvider(api_key=os.environ.get("OPENCODE_API_KEY", "").strip())
        p.base_url = ZEN_BASE
        return p

    if pid == "opencode-go":
        key = os.environ.get("SUPERVISOR_API_KEY", "") or os.environ.get("OPENCODE_API_KEY", "")
        p = OpenCodeGoProvider(api_key=key.strip())
        p.base_url = GO_BASE
        return p

    if pid == "openrouter":
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            key = os.environ.get("CLOUD_OR_API_KEY", "")
        base = os.environ.get("CLOUD_OR_BASE_URL", "") or "https://openrouter.ai/api/v1"
        return _compat("openrouter", key, base)

    if pid == "openai":
        return _compat("openai", os.environ.get("OPENAI_API_KEY", ""),
                       os.environ.get("OPENAI_BASE_URL", "") or "https://api.openai.com/v1")

    if pid == "anthropic":
        p = AnthropicProvider(api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip())
        p.base_url = os.environ.get("ANTHROPIC_BASE_URL", "") or DEFAULT_ANTHROPIC_BASE
        return p

    if pid == "gemini":
        # Google exposes an OpenAI-compatible endpoint, so reuse it directly.
        return _compat("gemini", os.environ.get("GEMINI_API_KEY", ""),
                       "https://generativelanguage.googleapis.com/v1beta/openai")

    if pid == "groq":
        key = os.environ.get("GROQ_API_KEY", "")
        if not key:
            key = os.environ.get("CLOUD_GROQ_API_KEY", "")
        base = os.environ.get("GROQ_BASE_URL", "") or os.environ.get("CLOUD_GROQ_BASE_URL", "") \
            or "https://api.groq.com/openai/v1"
        return _compat("groq", key, base)

    if pid == "xai":
        return _compat("xai", os.environ.get("XAI_API_KEY", ""), "https://api.x.ai/v1")

    if pid == "local":
        base = os.environ.get("LOCAL_BASE_URL", "") or os.environ.get("LLM_BASE_URL", "") \
            or "http://127.0.0.1:8080/v1"
        key = os.environ.get("LOCAL_API_KEY", "") or os.environ.get("LLM_API_KEY", "")
        return _compat("local", key, base)

    raise ValueError(f"unknown provider id: {pid!r}")


def _compat(pid: str, api_key: str, base_url: str) -> OpenAICompatibleProvider:
    p = OpenAICompatibleProvider(api_key=api_key)
    p.provider_id = pid
    p.name = _PROVIDER_NAMES.get(pid, pid.title())
    p.base_url = base_url
    p.requires_key = pid != "local"
    p.key_env = f"{pid.upper()}_API_KEY"
    return p


def list_providers() -> list[dict]:
    """All known providers with configured/model catalog info."""
    ids = ["opencode", "opencode-go", "jarvis", "openrouter", "openai",
           "anthropic", "gemini", "groq", "xai", "local"]
    out = []
    for pid in ids:
        try:
            p = get_provider(pid)
            out.append(p.info())
        except Exception as e:  # never break enumeration on one bad provider
            out.append({"provider_id": pid, "name": pid, "error": str(e)})
    for entry in _CUSTOM.list():  # user-defined OpenAI-compatible endpoints
        try:
            out.append(get_provider(entry["provider_id"]).info())
        except Exception as e:  # noqa: BLE001
            out.append({"provider_id": entry["provider_id"],
                        "name": entry.get("name", entry["provider_id"]),
                        "custom": True, "error": str(e)})
    return out


def list_models(provider_id: str) -> list[dict]:
    try:
        return [m.to_dict() for m in get_provider(provider_id).models()]
    except Exception:
        return []