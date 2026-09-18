import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv, set_key

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import httpx
from openai import AsyncOpenAI, DefaultAsyncHttpxClient, RateLimitError

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import ErrorFrame, LLMRunFrame, LLMTextFrame, TTSAudioRawFrame, TTSStoppedFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.utils.errors import ErrorCategory
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.moonshine.stt import MoonshineSTTService
from pipecat.utils.text.base_text_aggregator import (
    Aggregation,
    AggregationType,
    BaseTextAggregator,
)
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.base_llm import BaseOpenAILLMService
from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
from pipecat.turns.user_start.transcription_user_turn_start_strategy import TranscriptionUserTurnStartStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.transports.websocket.server import (
    SingleClientWebsocketServerParams,
    SingleClientWebsocketServerTransport,
)
from pipecat.workers.runner import WorkerRunner

from pipecat.adapters.schemas.function_schema import FunctionSchema

from proto import (
    AgentPartFrame,
    AgentsFrame,
    AudioToggleFrame,
    BotImageFrame,
    BrainActivityFrame,
    BrainConfigFrame,
    BrainErrorFrame,
    BrainFrame,
    CatalogFrame,
    CodingResultFrame,
    FsListingFrame,
    FsNativeResultFrame,
    MemoryFrame,
    ProjectGateFrame,
    ProjectsFrame,
    RecentSessionsFrame,
    SessionMessagesFrame,
    StatusFrame,
    UIFrameSerializer,
    WorkSessionsFrame,
    WorkerActivityFrame,
    WorkersFrame,
)  # noqa: E402

from pipecat.transports.websocket.server import SingleClientWebsocketServerOutputTransport  # noqa: E402
from memory import ENTRY_DELIMITER, MemoryManager, MemoryReviewer, TurnSignal  # noqa: E402
from recall import Recaller  # noqa: E402
from sessions import SessionStore  # noqa: E402
from webfetch_tool import (  # noqa: E402
    _web_fetch_schema,
    handle_web_fetch,
)
from imagegen_tool import (  # noqa: E402
    _generate_image_schema,
    handle_generate_image,
)
from mpt_video_tool import _make_video_schema  # noqa: E402  # optional video add-on
from worker_engine import (  # noqa: E402
    abort_session as opencode_abort_session,
    followup as opencode_followup,
    is_coding_request,
    propose as opencode_propose,
    stop_session as opencode_stop_session,
    list_sessions as we_list_sessions,
    session_messages as we_session_messages,
    resolve_agent_provider,
    register_agent_provider,
    get_active_project,
    get_repo_root,
    init_projects,
    is_project_chosen,
    list_projects,
    list_workers,
    mark_project_chosen,
    set_active_project,
    set_worker,
    worker_name,
)
from personas import ASSISTANT_NAME, assistant_voice  # noqa: E402
from backchannel import BackchannelProcessor  # noqa: E402  # pre-rendered "mm-hm"
import folder_picker  # noqa: E402  # server-side project folder picker
import orchestrator as orchestrator_mod  # noqa: E402  # brain→CLI workers (Phase 1)
import apply_patch as apply_patch_mod  # noqa: E402  # A2.1 fuzzy multi-file patch tool
import snapshot as snapshot_mod  # noqa: E402  # P0.4 git-backed snapshot/revert
import agent_loop  # noqa: E402  # native agent loop (model <-> tools until done)
import figma_rest  # noqa: E402  # Figma REST tools (read design files)
import permissions as permissions_mod  # noqa: E402  # A3 wildcard permission rules
import mcp_client as mcp_mod  # noqa: E402  # C1 MCP stdio client
import mcp_screen  # noqa: E402  # MCP screen backend (audio-free, testable)
import board as board_mod  # noqa: E402  # B: Projects board store (Jarvis's own)
import board_screen  # noqa: E402  # B: Projects screen backend (audio-free, testable)
import skills as skills_mod  # noqa: E402  # E1-E3 skill library
import scheduler as scheduler_mod  # noqa: E402  # G2 cron/automations
import lsp_client as lsp_mod  # noqa: E402  # C2+ real language server
import supervisor  # noqa: E402  # KB-08: brain tier — import + call only, never modify
import providers as providers_pkg  # noqa: E402
import jarvis_paths  # noqa: E402  # data-dir + default-project resolution
from providers import CONFIG as _PROVIDERS_CONFIG  # noqa: E402
from providers import ProviderError  # noqa: E402
from providers.openai_compatible import _oc_ulid  # noqa: E402  # validated header ids


def _provider_cfg_update(msg: dict):
    """Apply provider config set (validates ids/models before mutating CONFIG)."""
    _PROVIDERS_CONFIG.update(
        brain_provider=msg.get("brain_provider"),
        brain_model=msg.get("brain_model"),
        worker_provider=msg.get("worker_provider"),
        worker_model=msg.get("worker_model"),
    )


def providers_config():
    return _PROVIDERS_CONFIG


def providers_list():
    return providers_pkg.list_providers()


_UI_PROVIDER_IDS = ("opencode", "opencode-go", "jarvis", "openrouter", "openai",
                    "anthropic", "gemini", "groq", "xai", "local")


def _ui_provider_rows() -> list[dict]:
    """Providers shown in Settings: the built-ins plus user-defined custom ones."""
    return [p for p in providers_list()
            if p.get("provider_id") in _UI_PROVIDER_IDS
            or str(p.get("provider_id", "")).startswith("custom:")]


def _provider_ready(pid: str) -> bool:
    """True when the provider id exists and has its key/base URL configured."""
    try:
        return providers_pkg.get_provider(pid).is_configured()
    except Exception:  # noqa: BLE001
        return False


# ── BRAIN TRANSPORT (env-driven; OmniRoute is the shipped default) ──────────
# HARDWIRED BRAIN (user decision 2026-09-17): Jarvis runs on exactly one
# model — DeepSeek V4.1 Flash — and there is no picker and no per-user choice:
# the talking LLM and the MemoryReviewer both resolve from these constants,
# and brain-pref.json cannot override them. The JARVIS_* env vars are an
# invisible ops/test override only — they are NEVER surfaced in the UI.
#
# TRANSPORT (user decision 2026-09-18): the packaged app must NOT default to a
# personal OpenCode subscription endpoint, so the endpoint/credentials are now
# a first-class, env-driven choice. Resolution order (first match wins):
#
#   1. JARVIS_BRAIN_BASE_URL — explicit base URL (legacy product setting;
#      highest precedence, backwards compatible).
#   2. JARVIS_BRAIN_TRANSPORT — explicit transport selector:
#        deepseek  -> DEEPSEEK_BASE_URL    (default https://api.deepseek.com)
#        omniroute -> OMNIROUTE_BASE_URL   (default http://127.0.0.1:20128/v1)
#        opencode  -> https://opencode.ai/zen/go/v1   (DEV-ONLY fallback)
#   3. auto (transport unset/auto):
#        packaged app (JARVIS_DATA_DIR set) -> deepseek   (shipping default)
#        dev/source  (JARVIS_DATA_DIR unset) -> opencode   (dev fallback)
#
# OmniRoute remains fully supported when selected explicitly, but it is an
# optional add-on and never the default (user decision 2026-09-18: "direct
# path is better, one api key that's it").
#
# The OpenCode endpoint is never selected when a product setting
# (JARVIS_BRAIN_BASE_URL or JARVIS_BRAIN_TRANSPORT) is present. Keys are read
# from JARVIS_BRAIN_API_KEY, then SUPERVISOR_API_KEY, then OPENCODE_API_KEY
# (for the custom/opencode paths), OMNIROUTE_API_KEY for OmniRoute, and
# DEEPSEEK_API_KEY for DeepSeek direct. A missing key is never a crash: the
# resolver returns an empty key and the transport is logged so the user can
# answer "what is my brain actually talking to?".
_DEFAULT_BRAIN_MODEL = "deepseek-v4.1-flash"
_DEFAULT_OMNIROUTE_BASE_URL = "http://127.0.0.1:20128/v1"
_DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
_DEV_OPENCODE_BASE_URL = "https://opencode.ai/zen/go/v1"
# Transports that may run without a key (a local gateway can be keyless).
_KEYLESS_BRAIN_TRANSPORTS = ("omniroute",)


def _is_packaged(env=None) -> bool:
    """True for the shipped app: its launcher always sets JARVIS_DATA_DIR."""
    env = os.environ if env is None else env
    return bool((env.get("JARVIS_DATA_DIR") or "").strip())


def _resolve_brain_transport(env=None) -> dict:
    """Resolve the brain transport from the environment (pure, testable).

    Returns ``{"transport", "base_url", "api_key", "key_env", "model",
    "source"}``. Never raises: an absent key yields ``api_key`` "" (the caller
    decides whether a provider can be built), so absence of keys cannot crash.
    """
    env = os.environ if env is None else env

    def get(name):
        return (env.get(name) or "").strip()

    def legacy_key():
        """(key, env_name) for the custom/opencode paths, legacy order."""
        for name in ("SUPERVISOR_API_KEY", "OPENCODE_API_KEY"):
            if get(name):
                return get(name), name
        return "", ""

    model = get("JARVIS_BRAIN_MODEL") or _DEFAULT_BRAIN_MODEL
    explicit_base = get("JARVIS_BRAIN_BASE_URL")
    explicit_key = get("JARVIS_BRAIN_API_KEY")
    selector = get("JARVIS_BRAIN_TRANSPORT").lower()

    # 1. Explicit base URL = legacy/product setting; wins over everything.
    if explicit_base:
        if explicit_key:
            key, key_env = explicit_key, "JARVIS_BRAIN_API_KEY"
        else:
            key, key_env = legacy_key()
        return {"transport": "custom", "base_url": explicit_base,
                "api_key": key, "key_env": key_env, "model": model,
                "source": "JARVIS_BRAIN_BASE_URL"}

    # 2. Explicit transport selector.
    if selector == "omniroute":
        key = get("OMNIROUTE_API_KEY")
        return {"transport": "omniroute",
                "base_url": get("OMNIROUTE_BASE_URL") or _DEFAULT_OMNIROUTE_BASE_URL,
                "api_key": key,
                "key_env": "OMNIROUTE_API_KEY" if key else "",
                "model": model, "source": "JARVIS_BRAIN_TRANSPORT"}
    if selector == "deepseek":
        key = get("DEEPSEEK_API_KEY")
        return {"transport": "deepseek",
                "base_url": get("DEEPSEEK_BASE_URL") or _DEFAULT_DEEPSEEK_BASE_URL,
                "api_key": key,
                "key_env": "DEEPSEEK_API_KEY" if key else "",
                "model": model, "source": "JARVIS_BRAIN_TRANSPORT"}
    if selector == "opencode":
        key, key_env = legacy_key()
        return {"transport": "opencode", "base_url": _DEV_OPENCODE_BASE_URL,
                "api_key": key, "key_env": key_env, "model": model,
                "source": "JARVIS_BRAIN_TRANSPORT"}

    # 3. Auto: packaged ships the DeepSeek-direct default; source keeps the dev
    # OpenCode fallback. OmniRoute stays available via the explicit selector.
    if _is_packaged(env):
        key = get("DEEPSEEK_API_KEY")
        return {"transport": "deepseek",
                "base_url": get("DEEPSEEK_BASE_URL") or _DEFAULT_DEEPSEEK_BASE_URL,
                "api_key": key,
                "key_env": "DEEPSEEK_API_KEY" if key else "",
                "model": model, "source": "auto:packaged"}
    key, key_env = legacy_key()
    return {"transport": "opencode", "base_url": _DEV_OPENCODE_BASE_URL,
            "api_key": key, "key_env": key_env, "model": model,
            "source": "auto:dev"}


_BRAIN_TRANSPORT_INFO = _resolve_brain_transport()
BRAIN_MODEL_ID = _BRAIN_TRANSPORT_INFO["model"]
BRAIN_BASE_URL = _BRAIN_TRANSPORT_INFO["base_url"]
BRAIN_TRANSPORT = _BRAIN_TRANSPORT_INFO["transport"]
BRAIN_API_KEY = _BRAIN_TRANSPORT_INFO["api_key"]
BRAIN_KEY_ENV = _BRAIN_TRANSPORT_INFO["key_env"]
_BRAIN_TRANSPORT_LOGGED = False


def _log_brain_transport(info=None, force=False) -> None:
    """Log the selected transport once at startup (never the key itself)."""
    global _BRAIN_TRANSPORT_LOGGED
    if _BRAIN_TRANSPORT_LOGGED and not force:
        return
    _BRAIN_TRANSPORT_LOGGED = True
    info = info or _BRAIN_TRANSPORT_INFO
    host = urlparse(info.get("base_url") or "").netloc or (info.get("base_url") or "")
    logger.info(
        "[LLM] brain transport: transport=%s base=%s host=%s model=%s key_env=%s",
        info.get("transport"), info.get("base_url"), host, info.get("model"),
        info.get("key_env") or "(none)",
    )


# HARDWIRED REASONING (user decision 2026-09-17): the reasoning effort is
# always the model default — the field is omitted from every model call so
# the model decides what's best for it. There is no user control and no
# pref/env override: reasoning_set is accepted-and-ignored, and neither a
# stale brain-pref.json value nor REASONING_EFFORT in .env can re-enable a
# non-default effort. Invisible to the user by design.
HARDWIRED_REASONING = "default"


def _reasoning_extra() -> dict:
    """LLM `extra` for the hardwired effort. Always {} (field omitted)."""
    return {}


# ── CONVERSATIONAL-TURN THINKING SHORTCUT (user-approved 2026-09-18) ─────────
# A short spoken turn with nothing to work out ("hey, how are you?") does not
# pay to think first, so its request goes out with thinking disabled. Every
# other turn is left exactly as it is today — the model decides. Work turns
# are byte-identical to before. The rules live HERE, in one place, so tuning
# is a one-line edit: conversational requires short AND every word from the
# conversational vocabulary AND an explicit greeting / thanks / small-talk /
# acknowledgement / yes-no intent. Anything else is a WORK turn. Never applied
# to a turn that already used a tool or carries an image; a doubtful reply is
# re-asked once with thinking on (BoundedContextLLM._conversational_reask).
TURN_CLASS_CONVERSATIONAL = "conversational"
TURN_CLASS_WORK = "work"
_CONVERSATIONAL_MAX_WORDS = 12
# Verified provider shape from the old reasoning control (before it was
# hardwired): the OpenAI-compatible body field that disables thinking.
_REASONING_OFF_EXTRA = {"extra_body": {"reasoning_effort": "none"}}

_CONVERSATIONAL_PHRASES = frozenset({
    "how are you", "how are you doing", "how you doing", "how's it going",
    "hows it going", "how is it going", "what's up", "whats up", "sup",
    "how have you been", "how you been", "nice to meet you",
    "long time no see", "good to see you", "good to hear from you",
    "how was your day", "how's your day", "hows your day",
    "thank you", "thanks so much", "thank you so much",
    "good morning", "good afternoon", "good evening", "good night",
    "are you there", "you there", "you're welcome", "youre welcome",
    "no problem", "of course", "my pleasure", "sounds good",
    "you are the best", "you're the best",
})
_CONVERSATIONAL_INTENT = frozenset({
    "hi", "hey", "hello", "howdy", "hiya", "yo", "greetings", "morning",
    "afternoon", "evening", "night", "thanks", "thank", "thankyou", "thx",
    "ty", "ta", "cheers", "ok", "okay", "k", "kk", "sure", "alright",
    "awright", "cool", "nice", "great", "perfect", "awesome", "good",
    "yep", "yup", "yeah", "yah", "yes", "ya", "nope", "no", "nah", "right",
    "indeed", "exactly", "correct", "true", "understood", "noted", "bye",
    "goodbye", "later", "ciao", "welcome", "sorry", "congrats",
    "congratulations",
})
_CONVERSATIONAL_VOCAB = _CONVERSATIONAL_INTENT | frozenset({
    "i", "i'm", "im", "you", "you're", "youre", "your", "we", "us", "how",
    "how's", "hows", "are", "is", "it", "what", "what's", "whats", "up",
    "doing", "doin", "been", "going", "there", "today", "this", "that",
    "the", "a", "an", "my", "me", "well", "so", "much", "very", "too", "to",
    "and", "but", "oh", "ah", "hm", "hmm", "mm", "mhm", "uh", "huh", "see",
    "mind", "all", "now", "not", "don't", "dont", "never", "for", "of", "in",
    "on", "at", "with", "be", "was", "were", "am", "do", "did", "got",
    "get", "have", "has", "had", "please", "again", "one", "here", "about",
    "hope", "glad", "happy", "feeling", "feel", "weekend", "yesterday",
    "tomorrow", "sounds", "best", "problem", "welcome", "pleasure", "course",
})
# Exact work words (no substrings: "report" must not match "repo"). A short
# phrase made only of conversational words is classified before this list, so
# words like "how"/"what" here never steal a greeting.
_WORK_HINTS = frozenset({
    "code", "file", "files", "repo", "repository", "screen", "build", "fix",
    "write", "create", "debug", "test", "tests", "run", "search", "find",
    "implement", "design", "refactor", "summary", "summarize", "summarise",
    "research", "project", "error", "bug", "plan", "compare", "analyse",
    "analyze", "explain", "why", "how", "check", "read", "open", "list",
    "show", "make", "add", "remove", "update", "change", "delete", "install",
    "deploy", "help", "need", "want", "could", "would", "should", "tell",
    "give", "look", "send", "report",
})


def classify_turn_text(text: str) -> tuple[str, str]:
    """Classify one user utterance: ``(turn_class, signal)``.

    Conservative on purpose: conversational only when the utterance is short,
    every word is conversational vocabulary, and it carries an explicit
    greeting / thanks / small-talk / acknowledgement / yes-no intent. Anything
    else is a WORK turn — the request is left completely unchanged.
    """
    raw = (text or "").strip().lower()
    if not raw:
        return TURN_CLASS_WORK, "empty"
    norm = re.sub(r"[^a-z0-9' ]+", " ", raw)
    norm = re.sub(r"\s+", " ", norm).strip()
    if norm in _CONVERSATIONAL_PHRASES:
        return TURN_CLASS_CONVERSATIONAL, f"phrase:{norm}"
    words = norm.split()
    if len(words) >= _CONVERSATIONAL_MAX_WORDS:
        return TURN_CLASS_WORK, f"long:{len(words)}-words"
    if (words and all(w in _CONVERSATIONAL_VOCAB for w in words)
            and any(w in _CONVERSATIONAL_INTENT for w in words)):
        intent = next(w for w in words if w in _CONVERSATIONAL_INTENT)
        return TURN_CLASS_CONVERSATIONAL, f"intent:{intent}"
    for hint in sorted(_WORK_HINTS):
        if hint in words:
            return TURN_CLASS_WORK, f"work-term:{hint}"
    return TURN_CLASS_WORK, "no-conversational-intent"


def _default_model() -> str:
    """The model used when the user hasn't chosen one. Hardwired (2026-09-17):
    always the single brain constant — never a plan fallback, never a pick."""
    return BRAIN_MODEL_ID


def _reviewer_settings() -> tuple:
    """Wiring for the MemoryReviewer (module-level so tests can prove it).

    Hardwired (2026-09-17): the reviewer runs on the SAME single constant as
    the talking brain — whatever the brain is, memory is. Same resolved
    transport and model; the key is whatever ``_resolve_brain_transport``
    selected (JARVIS_BRAIN_API_KEY, then SUPERVISOR_API_KEY, then
    OPENCODE_API_KEY for the legacy/opencode paths)."""
    return (BRAIN_BASE_URL, BRAIN_MODEL_ID, BRAIN_API_KEY)


def _byok_model_sources() -> list[tuple[str, str, list]]:
    """Configured non-opencode providers with models: [(provider_id, label,
    [model dicts])]. Feeds both the model picker and the voice provider map."""
    out = []
    for p in providers_list():
        pid = p.get("provider_id", "")
        if pid in ("opencode", "opencode-go", "local") or not p.get("configured"):
            continue
        models = p.get("models") or []
        if models:
            out.append((pid, p.get("name") or pid, models))
    return out


# Env var names the UI is allowed to write (never anything else).
_ALLOWED_KEY_ENV = {
    "OPENCODE_API_KEY", "SUPERVISOR_API_KEY", "OPENROUTER_API_KEY",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
    "GROQ_API_KEY", "XAI_API_KEY",
}
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _provider_key_save(env_name: str, value: str) -> dict:
    """Persist one API key to .env (gitignored) and reload provider state.

    *env_name* must be allowlisted. Empty *value* clears the key (provider
    becomes unconfigured). Returns {env, set, validator} where validator is
    the short manual name (or "") matching providers/registry.py.
    """
    if env_name not in _ALLOWED_KEY_ENV:
        raise ProviderError(f"key not allowlisted: {env_name}", 403, "")
    value = (value or "").strip()
    set_key(str(_ENV_FILE), env_name, value, quote_mode="never" if value else "always")
    os.environ[env_name] = value
    providers_pkg.reset_cache()
    validator = {
        "OPENCODE_API_KEY": "opencode",
        "SUPERVISOR_API_KEY": "opencode-go",
        "OPENROUTER_API_KEY": "openrouter",
        "OPENAI_API_KEY": "openai",
        "ANTHROPIC_API_KEY": "anthropic",
        "GEMINI_API_KEY": "gemini",
        "GROQ_API_KEY": "groq",
        "XAI_API_KEY": "xai",
    }.get(env_name, "")
    return {"env": env_name, "set": bool(value), "validator": validator}


def _provider_key_validate(provider_id: str, timeout: float = 12.0) -> bool:
    """Ping one provider with a single tiny turn. True when the key works.

    Uses the provider's default model (or first catalog entry). Raises on
    401/403/HTTP errors so the caller can surface bad-key detail.
    """
    from providers import get_provider as _gp, ProviderError as _PE  # noqa: F401
    provider = _gp(provider_id)
    if not provider.is_configured():
        raise ProviderError(f"{provider_id}: no key set", 401, provider_id)
    models = provider.models()
    model = provider.default_model or (models[0].id if models else "big-pickle")
    provider.chat(
        [{"role": "user", "content": "Reply with the single word: ping"}],
        model,
        tools=None,
        timeout=timeout,
    )
    return True


logger = logging.getLogger("asha.server")
logging.basicConfig(level=logging.INFO)


class _RedactingFilter(logging.Filter):
    """Scrub API keys/secrets from log records (secret_scope; Hermes #25)."""

    def filter(self, record):
        try:
            import redact
            if isinstance(record.msg, str):
                record.msg = redact.redact(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(
                    redact.redact(a) if isinstance(a, str) else a
                    for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: (redact.redact(v) if isinstance(v, str) else v)
                               for k, v in record.args.items()}
        except Exception:  # noqa: BLE001
            pass
        return True


for _h in logging.getLogger().handlers:
    _h.addFilter(_RedactingFilter())

# One line at startup answers "what is my brain actually talking to?".
_log_brain_transport()


# ---------------------------------------------------------------------------
# Web search tool (Exa) — Asha internet access
# ---------------------------------------------------------------------------

_EXA_API_KEY = os.environ.get("EXA_API_KEY", "")
_EXA_ENDPOINT = "https://api.exa.ai/search"

_NO_EXA_KEY_MSG = ("Web search needs an Exa API key to be set before I can look "
                   "anything up online.")


async def exa_search(query: str, max_results: int = 6) -> str:
    """Search the web via Exa (LLM-optimized, live-crawled text)."""
    if not _EXA_API_KEY:
        return _NO_EXA_KEY_MSG
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                _EXA_ENDPOINT,
                headers={"x-api-key": _EXA_API_KEY,
                         "Content-Type": "application/json"},
                json={
                    "query": query,
                    "numResults": max_results,
                    "type": "auto",
                    "contents": {"text": {"maxCharacters": 1200}},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            parts = []
            for r in data.get("results", []):
                title = r.get("title", "")
                url = r.get("url", "")
                text = (r.get("text") or r.get("summary") or "").strip()
                if text:
                    parts.append(f"- {title} ({url}): {text[:600]}")
            return "\n".join(parts) if parts else "No results found."
    except Exception as e:
        logger.warning(f"[SEARCH] Exa failed: {e!r}")
        return "Web search failed. I could not reach Exa just now."


async def web_search(query: str, max_results: int = 6) -> str:
    """Web search via Exa (LLM-optimized, livecrawl)."""
    return await exa_search(query, max_results)


# ---------------------------------------------------------------------------
# File tools — search and read files on the user's machine
# ---------------------------------------------------------------------------

_HOME = os.path.expanduser("~")
# Limits removed/raised (user directive: functionality first, code like opencode)
_MAX_READ_CHARS = int(os.environ.get("MAX_READ_CHARS", "30000"))
_MAX_SEARCH_RESULTS = int(os.environ.get("MAX_SEARCH_RESULTS", "200"))
_MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "50"))


async def file_search(query: str, path: str = "") -> str:
    """Search for files by name or content. Returns matching file paths."""
    search_dir = os.path.join(_HOME, path) if path else _HOME
    if not os.path.isdir(search_dir):
        return f"Directory not found: {search_dir}"
    try:
        # Search by filename (find) and content (grep) in parallel
        name_matches = []
        content_matches = []

        # Split query into words for flexible matching
        words = query.split()
        find_patterns = []
        for i, word in enumerate(words):
            if len(word) >= 2:
                if i > 0:
                    find_patterns.append("-o")
                find_patterns.extend(["-iname", f"*{word}*"])

        # Find files by name (match any word)
        if find_patterns:
            proc = await asyncio.create_subprocess_exec(
                "find", search_dir, "-type", "f",
                "(", *find_patterns, ")",
                "-not", "-path", "*/.*", "-not", "-path", "*/Library/*",
                "-not", "-path", "*/Cache/*", "-not", "-path", "*/node_modules/*",
                "-maxdepth", "5",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                "find", search_dir, "-type", "f", "-iname", f"*{query}*",
                "-not", "-path", "*/.*", "-not", "-path", "*/Library/*",
                "-not", "-path", "*/Cache/*", "-not", "-path", "*/node_modules/*",
                "-maxdepth", "5",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        raw = stdout.decode(errors="replace").strip()
        name_matches = raw.split("\n")[:_MAX_SEARCH_RESULTS] if raw else []
        logger.info(f"[FILE] find results: {len(name_matches)} matches for '{query}'")

        # Search file contents (only for text-like files)
        try:
            proc2 = await asyncio.create_subprocess_exec(
                "grep", "-rl", "-i", "--include=*.txt", "--include=*.md",
                "--include=*.py", "--include=*.js", "--include=*.ts",
                "--include=*.json", "--include=*.csv", "--include=*.log",
                "--include=*.html", "--include=*.css", "--include=*.sh",
                query, search_dir,
                "--exclude-dir=.git", "--exclude-dir=node_modules",
                "--exclude-dir=Library", "--exclude-dir=Cache",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=10)
            raw2 = stdout2.decode(errors="replace").strip()
            content_matches = raw2.split("\n")[:_MAX_SEARCH_RESULTS] if raw2 else []
            logger.info(f"[FILE] grep results: {len(content_matches)} matches for '{query}'")
        except asyncio.TimeoutError:
            content_matches = []
            logger.warning("[FILE] grep timed out")

        parts = []
        if name_matches and name_matches[0]:
            parts.append("Files matching name:")
            for f in name_matches[:_MAX_SEARCH_RESULTS]:
                parts.append(f"  {f}")
        if content_matches and content_matches[0]:
            parts.append("Files containing the text:")
            for f in content_matches[:_MAX_SEARCH_RESULTS]:
                parts.append(f"  {f}")
        if not parts:
            return f"No files found matching '{query}'"
        return "\n".join(parts)
    except Exception as e:
        logger.warning(f"[FILE] file_search failed: {e!r}")
        return f"File search failed: {e}"


async def read_file(path: str, offset: int = 0, limit: int = 0) -> str:
    """Read a file and return its contents. Supports text, code, and PDFs.

    For text files, pass ``offset`` (1-based first line) and/or ``limit``
    (max lines) to read a range; the returned lines are then numbered.
    """
    # Resolve relative paths from home; fall back to the repo root so
    # files just written by write_file/edit_file (repo-anchored) read back
    # without the model needing to know two different base dirs.
    candidates = []
    if os.path.isabs(path):
        candidates = [path]
    else:
        candidates = [os.path.join(_HOME, path)]
        try:
            repo_root = str(_REPO_ROOT)
            if repo_root != _HOME:
                candidates.append(os.path.join(repo_root, path))
        except NameError:
            pass
    resolved = next((c for c in candidates if os.path.isfile(c)), None)
    if resolved is None:
        return f"File not found: {path}"
    path = resolved
    try:
        size = os.path.getsize(path)
        if size > 10 * 1024 * 1024:  # 10MB limit
            return f"File too large ({size:,} bytes). Max 10MB."
        # Handle PDFs
        if path.lower().endswith(".pdf"):
            try:
                import io
                import pdfplumber
                with pdfplumber.open(path) as pdf:
                    pages = []
                    for page in pdf.pages[:30]:
                        t = page.extract_text()
                        if t:
                            pages.append(t)
                    text = "\n\n--- page break ---\n\n".join(pages)
            except Exception:
                return "Failed to read PDF."
        else:
            # Read text file
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(_MAX_READ_CHARS * 4)  # read extra to account for unicode
        # Optional line range (offset=0 and limit=0 means "whole file").
        if offset or limit:
            lines = text.splitlines()
            start = max(0, (offset - 1) if offset > 0 else 0)
            end = start + limit if limit > 0 else len(lines)
            selected = lines[start:end]
            if not selected:
                return f"(no lines at offset {offset})"
            return "\n".join(
                f"{start + i + 1}\t{ln}" for i, ln in enumerate(selected)
            )
        if len(text) > _MAX_READ_CHARS:
            text = text[:_MAX_READ_CHARS] + f"\n\n... [truncated — {len(text):,} chars total]"
        return text if text.strip() else "File is empty."
    except Exception as e:
        logger.warning(f"[FILE] read_file failed: {e!r}")
        return f"Failed to read file: {e}"


# Tool schemas for file operations
_file_search_schema = FunctionSchema(
    name="file_search",
    description="Search for files on the user's machine by name or content. Use this when the user asks to find files, locate documents, or search for something stored on their computer.",
    properties={
        "query": {
            "type": "string",
            "description": "Search term — can be a filename, keyword, or phrase to search for in file contents",
        },
        "path": {
            "type": "string",
            "description": "Optional subdirectory to search in (relative to home). Leave empty to search entire home directory.",
        },
    },
    required=["query"],
)

_read_file_schema = FunctionSchema(
    name="read_file",
    description="Read the contents of a file on the user's machine. Supports text, code, and PDF files. Use this after file_search to read a specific file. For large text files pass offset/limit to read a numbered line range.",
    properties={
        "path": {
            "type": "string",
            "description": "Full path to the file, or relative path from home directory",
        },
        "offset": {
            "type": "integer",
            "description": "Optional 1-based first line to read (line ranges only)",
        },
        "limit": {
            "type": "integer",
            "description": "Optional max number of lines to read",
        },
    },
    required=["path"],
)
_web_search_schema = FunctionSchema(
    name="web_search",
    description="Search the internet for current information. Use this when the user asks about current events, news, weather, facts, or anything you don't know from memory.",
    properties={
        "query": {
            "type": "string",
            "description": "The search query",
        },
    },
    required=["query"],
)

# opencode-style text tool calls. MiMo (opencode zen/go) sometimes emits a
# function call as inline text instead of a native tool_calls field:
#   <tool_call><function=web_search><parameter=query>weather in Mumbai</parameter></function></tool_call>
# The pipeline would otherwise read this raw tag aloud. We parse + execute it
# like a native tool round (see BoundedContextLLM._intercept_text_tool_calls).
_TEXT_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*<function=([\w.-]+)>(.*?)</function>\s*</tool_call>",
    re.DOTALL | re.IGNORECASE,
)
_TEXT_TOOL_PARAM_RE = re.compile(r"<parameter=([^>]+)>(.*?)</parameter>", re.DOTALL)


def _parse_text_tool_call(text):
    """Return (name, args, raw_tag) for the first complete text tool call, else None."""
    m = _TEXT_TOOL_CALL_RE.search(text or "")
    if not m:
        return None
    args = {}
    for pm in _TEXT_TOOL_PARAM_RE.finditer(m.group(2)):
        key = pm.group(1).strip()
        val = pm.group(2).strip()
        try:
            val = json.loads(val)
        except Exception:
            pass
        args[key] = val
    return m.group(1), args, m.group(0)


# ---------------------------------------------------------------------------
# Voice-tier coding tools — edit code in a voice turn
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]  # prototype/ui/server.py -> repo root
_BASH_FORBIDDEN_PATTERNS = (
    "rm -rf /",
    "rm -rf ~",
    "rm -rf $HOME",
    "rm -rf ${HOME}",
    "rm -rf ~/",
    "mkfs",
    "dd ",
    "sudo",
    "|sh",
    "| sh",
    "|bash",
    "| bash",
)
_TOOL_OUTPUT_TRUNCATE = int(os.environ.get("TOOL_OUTPUT_TRUNCATE", "20000"))  # max chars for tool output over ws

# A3: ordered wildcard permission rules (global, persisted under Jarvis's data/).
# JARVIS_DATA_DIR (packaged app) keeps this out of the .app bundle; dev keeps
# prototype/data.
_ASHA_DATA_DIR = jarvis_paths.data_dir()

# Agent session persistence: the native loop's message history survives restarts.
_AGENT_SESSION_FILE = _ASHA_DATA_DIR / "agent-session.json"


def _load_agent_session(project: str) -> tuple[list, list]:
    try:
        d = json.loads(_AGENT_SESSION_FILE.read_text(encoding="utf-8"))
        if d.get("project") == project:
            return d.get("messages") or [], d.get("last_files") or []
    except Exception:
        pass
    return [], []


def _save_agent_session(project: str, messages: list, files: list) -> None:
    try:
        _ASHA_DATA_DIR.mkdir(parents=True, exist_ok=True)
        _AGENT_SESSION_FILE.write_text(
            json.dumps({"project": project, "messages": messages[-42:],
                        "last_files": files}), encoding="utf-8")
    except Exception:
        pass
_PERMS = permissions_mod.PermissionStore(_ASHA_DATA_DIR / "permissions.json")

# Read-only shell heads that a coding agent may run without a permission prompt.
_READONLY_BASH_HEADS = (
    "ls", "cat", "head", "tail", "grep", "rg", "find", "wc", "pwd", "tree",
    "file", "stat", "du", "df", "echo", "which", "type", "basename", "dirname",
    "sort", "uniq", "cut", "sed", "awk", "tr", "nl", "realpath", "readlink",
    "cd", "true", "printf",
)
_READONLY_BASH_FORBID = (
    " >", ">>", " -exec", " -delete", " sed -i", " tee ", " rm ", " mv ",
    " cp ", "sudo", "chmod", "chown", "truncate", "dd ", "mkfs", "curl", "wget",
    "git push", "git reset", "git checkout", "git commit", "git clean",
    "git branch -d", "git branch -D", "pip install", "npm install",
    "python -c", "python3 -c", "sh -c", "bash -c",
)
_CD_PREFIX_RE = re.compile(r"^\s*cd\s+(?:'[^']*'|\"[^\"]*\"|\S+)\s*&&\s*(.+)$")


def _is_readonly_bash(cmd: str) -> bool:
    """True for commands that only read/inspect (never write the tree).

    Handles the agents' common ``cd <dir> && <read>`` wrapper (and ``&&`` /
    ``;`` / ``||`` chains) by requiring EVERY segment to be read-only.
    """
    c = (cmd or "").strip()
    if not c:
        return False
    low = " " + c + " "
    if any(bad in low for bad in _READONLY_BASH_FORBID):
        return False
    m = _CD_PREFIX_RE.match(c)
    if m:
        c = m.group(1).strip()
    for seg in re.split(r"&&|\|\||;", c):
        seg = seg.strip()
        if not seg:
            continue
        head = seg.split()[0]
        if head == "git":
            if not any(sub in seg for sub in (
                "git status", "git diff", "git log", "git show", "git remote",
                "git rev-parse", "git ls-files", "git describe",
            )):
                return False
        elif head not in _READONLY_BASH_HEADS:
            return False
    return True
# P3: plan mode — research-only; edit tools are denied except the plan file.
_PLAN_MODE: dict = {"on": False, "file": "PLAN.md"}
_MCP = mcp_mod.MCPManager(_ASHA_DATA_DIR / "mcp.json")  # C1
_SKILLS = skills_mod.SkillStore(_ASHA_DATA_DIR / "skills")  # E1-E3
try:  # B: live agent work appears on the Projects board (Jarvis's own path)
    import tasks as _tasks_mod

    _tasks_mod.tasks.enable_board_sync(True)
except Exception:  # noqa: BLE001
    pass
_NUDGER = skills_mod.MemoryNudger(20)  # E3
_CRON = scheduler_mod.Scheduler(_ASHA_DATA_DIR / "cron.json")  # G2


def _safe_path(target: str) -> str | None:
    """Resolve *target* and confirm it stays under _REPO_ROOT.

    Relative paths anchor at the repo root (NOT the process CWD, which
    varies with how the bot was launched). Returns the resolved absolute
    path on success, ``None`` when the path escapes the repo (symlink
    traversal, ``..`` tricks, etc.).
    """
    p = Path(target)
    if not p.is_absolute():
        p = _REPO_ROOT / p
    resolved = p.resolve()
    try:
        resolved.relative_to(_REPO_ROOT)
    except ValueError:
        return None
    return str(resolved)


def _bash_forbidden(command: str) -> str | None:
    """Return a human-readable reason when *command* is dangerous, else None."""
    cmd_lower = (command or "").lower()
    for pat in _BASH_FORBIDDEN_PATTERNS:
        if pat in cmd_lower:
            return f"Blocked: command contains forbidden pattern '{pat}'"
    return None


async def write_file(path: str, content: str) -> str:
    """Create or overwrite a text file. Returns status string."""
    safe = _safe_path(path)
    if safe is None:
        return "Error: path resolves outside the repository"
    if _PERMS.decide(f"write: {path}") == permissions_mod.DENY:
        return f"Error: blocked by permission rule (write: {path})"
    if _PLAN_MODE["on"] and safe != _safe_path(_PLAN_MODE["file"]):
        return ("Error: plan mode is on — edits are disabled. Write the plan to "
                f"{_PLAN_MODE['file']} or turn plan mode off first.")
    try:
        p = Path(safe)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content or "", encoding="utf-8")
        return f"Wrote {len(content or '')} chars to {safe}"
    except Exception as e:
        return f"Error writing file: {e}"


async def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Exact-match single replacement. Returns status string."""
    safe = _safe_path(path)
    if safe is None:
        return "Error: path resolves outside the repository"
    if _PERMS.decide(f"write: {path}") == permissions_mod.DENY:
        return f"Error: blocked by permission rule (write: {path})"
    if _PLAN_MODE["on"] and safe != _safe_path(_PLAN_MODE["file"]):
        return ("Error: plan mode is on — edits are disabled. Write the plan to "
                f"{_PLAN_MODE['file']} or turn plan mode off first.")
    try:
        p = Path(safe)
        if not p.is_file():
            return f"Error: file not found: {safe}"
        original = p.read_text(encoding="utf-8")
    except Exception as e:
        return f"Error reading file: {e}"
    if not old_string:
        return "Error: old_string must not be empty"
    count = original.count(old_string)
    if count > 1:
        return f"Error: old_string matches {count} times — must match exactly once"
    if count == 0:
        # Fuzzy fallback (whitespace / quote / unicode drift) via apply_patch.
        try:
            updated = apply_patch_mod.apply_hunks(
                original,
                [apply_patch_mod.Hunk(
                    old_lines=old_string.splitlines(),
                    new_lines=new_string.splitlines(),
                )],
            )
        except Exception as e:
            return f"Error: old_string not found (fuzzy match failed: {e})"
        try:
            p.write_text(updated, encoding="utf-8")
            return f"Edited {safe} (fuzzy match, {len(old_string)} → {len(new_string)} chars)"
        except Exception as e:
            return f"Error writing file: {e}"
    updated = original.replace(old_string, new_string, 1)
    try:
        p.write_text(updated, encoding="utf-8")
        return f"Edited {safe} ({len(old_string)} → {len(new_string)} chars)"
    except Exception as e:
        return f"Error writing file: {e}"


def apply_patch(patch_text: str) -> str:
    """Apply an OpenAI-style multi-file patch inside the repo (A2.1).

    Uses the 4-pass fuzzy matcher (exact → trimEnd → trim → unicode-normalized)
    so edits land on real code despite whitespace/quote drift. Every path is
    sandboxed with ``_safe_path``; returns a summary or an error string, never
    raises.
    """
    if not (patch_text or "").strip():
        return "Error: empty patch"

    def _read(p: str) -> str:
        safe = _safe_path(p)
        if safe is None:
            raise ValueError(f"path escapes the repository: {p}")
        return Path(safe).read_text(encoding="utf-8")

    def _write(p: str, content: str) -> None:
        safe = _safe_path(p)
        if safe is None:
            raise ValueError(f"path escapes the repository: {p}")
        if _PERMS.decide(f"write: {p}") == permissions_mod.DENY:
            raise ValueError(f"blocked by permission rule: {p}")
        if _PLAN_MODE["on"] and safe != _safe_path(_PLAN_MODE["file"]):
            raise ValueError("plan mode is on — edits are disabled")
        fp = Path(safe)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")

    def _delete(p: str) -> None:
        safe = _safe_path(p)
        if safe is None:
            raise ValueError(f"path escapes the repository: {p}")
        if _PERMS.decide(f"write: {p}") == permissions_mod.DENY:
            raise ValueError(f"blocked by permission rule: {p}")
        if _PLAN_MODE["on"] and safe != _safe_path(_PLAN_MODE["file"]):
            raise ValueError("plan mode is on — edits are disabled")
        fp = Path(safe)
        if fp.is_file():
            fp.unlink()

    try:
        return apply_patch_mod.apply_patch_text(
            patch_text, _read, _write, _delete
        )
    except apply_patch_mod.PatchError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error applying patch: {e}"


async def run_bash(command: str, timeout_sec: int = 30) -> str:
    """Run a shell command with cwd forced to repo root.

    Refuses dangerous commands (returns an error string, never raises).
    Timeout-kills long-running processes and truncates output to
    ``_TOOL_OUTPUT_TRUNCATE`` chars.
    """
    forbidden = _bash_forbidden(command)
    if forbidden:
        return f"Error: {forbidden}"
    if _PERMS.decide(f"bash: {command}") == permissions_mod.DENY:
        return f"Error: blocked by permission rule (bash: {command[:120]})"
    if _PLAN_MODE["on"]:
        return ("Error: plan mode is on — shell is disabled for edits. Use "
                "read_file / grep / lsp to research, then turn plan mode off.")
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(_REPO_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return f"Error: command timed out after {timeout_sec}s"
        output = (stdout or b"").decode(errors="replace")
        if len(output) > _TOOL_OUTPUT_TRUNCATE:
            output = output[:_TOOL_OUTPUT_TRUNCATE] + "\n... (truncated)"
        return output if output.strip() else "(no output)"
    except Exception as e:
        return f"Error: {e}"


async def list_files(pattern: str) -> str:
    """Glob relative paths under the repo. Returns matching paths."""
    try:
        matches = sorted(
            str(p.relative_to(_REPO_ROOT))
            for p in _REPO_ROOT.glob(pattern or "**/*")
            if p.is_file()
        )
        if not matches:
            return f"No files matching '{pattern}'"
        if len(matches) > _MAX_SEARCH_RESULTS:
            shown = matches[:_MAX_SEARCH_RESULTS]
            return (
                f"{len(matches)} files found (showing first "
                f"{_MAX_SEARCH_RESULTS}):\n" + "\n".join(shown)
            )
        return "\n".join(matches)
    except Exception as e:
        return f"Error: {e}"


async def grep(pattern: str, path: str = "", glob: str = "") -> str:
    """Regex-search file contents under the repo. Returns ``path:line: text``.

    ``path`` scopes to a file or subdirectory; ``glob`` filters filenames
    (e.g. ``**/*.py``). Capped at ``_MAX_SEARCH_RESULTS`` matches.
    """
    if not pattern:
        return "Error: empty pattern"
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"Error: bad regex: {e}"
    base = _safe_path(path) if path else str(_REPO_ROOT)
    if base is None:
        return "Error: path resolves outside the repository"
    base_p = Path(base)
    if base_p.is_file():
        files = [base_p]
    elif base_p.is_dir():
        files = [p for p in base_p.glob(glob or "**/*") if p.is_file()]
    else:
        return f"Error: no such path: {path}"
    out: list[str] = []
    scanned = 0
    for fp in files:
        if scanned >= 4000 or len(out) >= _MAX_SEARCH_RESULTS:
            break
        scanned += 1
        try:
            if fp.stat().st_size > 2_000_000:
                continue
            text = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        try:
            rel = fp.relative_to(_REPO_ROOT)
        except ValueError:
            rel = fp
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                out.append(f"{rel}:{i}: {line[:300]}")
                if len(out) >= _MAX_SEARCH_RESULTS:
                    break
    if not out:
        return f"No matches for {pattern!r}"
    suffix = f"\n... (stopped at {_MAX_SEARCH_RESULTS} matches)" if len(out) >= _MAX_SEARCH_RESULTS else ""
    return "\n".join(out) + suffix


_REPO_MAP_SKIP = {".git", "node_modules", "__pycache__", ".venv", "dist",
                  "build", ".next", ".mypy_cache", ".pytest_cache"}


def repo_map(path: str = "", max_entries: int = 200) -> str:
    """Bounded structural map of the active project (A5).

    Lists files up to 4 levels deep, skipping heavy/generated dirs, capped at
    ``max_entries`` — so the brain can orient without globbing thousands of
    files.
    """
    base = _safe_path(path) if path else str(_REPO_ROOT)
    if base is None:
        return "Error: path resolves outside the repository"
    root = Path(base)
    if not root.exists():
        return f"Error: no such path: {path or root}"
    lines: list[str] = []
    try:
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if any(part in _REPO_MAP_SKIP for part in rel.parts):
                continue
            if p.is_dir() or len(rel.parts) > 4:
                continue
            lines.append(str(rel))
            if len(lines) >= max(1, int(max_entries)):
                lines.append("... (truncated)")
                break
    except Exception as e:
        return f"Error building repo map: {e}"
    return "\n".join(lines) if lines else "(no files)"


def diagnostics(path: str) -> str:
    """Edit feedback for a file (C2, LSP-lite).

    Python: syntax via ``py_compile``, then lint via ``ruff`` (preferred) or
    ``pyflakes`` when available. Non-Python files report no diagnostics.
    """
    safe = _safe_path(path)
    if safe is None:
        return "Error: path resolves outside the repository"
    fp = Path(safe)
    if not fp.is_file():
        return f"Error: file not found: {path}"
    if fp.suffix.lower() != ".py":
        return f"No diagnostics available for '{fp.suffix or 'this file'}' (python only)."
    # C2+: prefer a real language server (pylsp) when enabled/available.
    if os.environ.get("ASHA_LSP", "1") != "0":
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
            lsp_out = lsp_mod.lsp_diagnostics(safe, str(_REPO_ROOT), text)
            if lsp_out is not None:
                return lsp_out
        except Exception:
            pass
    import py_compile
    try:
        py_compile.compile(safe, doraise=True)
    except py_compile.PyCompileError as e:
        return f"Syntax error:\n{e}"
    except Exception as e:
        return f"Error compiling: {e}"
    for tool, args in (("ruff", ["check", "--output-format", "concise", "--no-cache"]),
                       ("pyflakes", [])):
        exe = shutil.which(tool)
        if not exe:
            continue
        try:
            p = subprocess.run([exe, *args, safe], capture_output=True,
                               text=True, timeout=30)
        except Exception as e:
            return f"Error running {tool}: {e}"
        out = (p.stdout or p.stderr or "").strip()
        return out if out else f"No diagnostics ({tool} clean)."
    return "Clean (syntax). Install ruff or pyflakes for lint diagnostics."


# Working task list the brain maintains across a multi-step job (single user).
_TODO_STATE: list[dict] = []
# P4: background task registry (orchestration/dispatch jobs), for visibility.
_TASKS: dict[str, dict] = {}
_TASK_SEQ = 0

# Loop guard: the same tool+args repeated this many times is a stuck agent, not
# progress (the free model tends to re-read the same docs forever).
_TOOL_CALL_LOG: list = []


def _task_register(kind: str, request: str) -> str:
    global _TASK_SEQ
    _TASK_SEQ += 1
    tid = f"t{_TASK_SEQ}"
    _TASKS[tid] = {"id": tid, "kind": kind, "request": (request or "")[:200],
                   "status": "running", "started": time.time()}
    return tid


def _task_finish(tid: str, status: str = "done", summary: str = "") -> None:
    t = _TASKS.get(tid)
    if t is not None:
        t["status"] = status
        t["summary"] = (summary or "")[:200]
        t["ended"] = time.time()
_TODO_MARKS = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
_TODO_STATUSES = tuple(_TODO_MARKS)


def todo_update(todos) -> str:
    """Replace the working todo list (opencode TodoWrite style).

    Each item: ``{"content": str, "status": "pending"|"in_progress"|"completed"}``.
    """
    global _TODO_STATE
    if not isinstance(todos, list):
        return "Error: todos must be a list"
    cleaned: list[dict] = []
    for t in todos:
        if not isinstance(t, dict):
            continue
        content = str(t.get("content", "")).strip()
        status = str(t.get("status", "pending")).strip()
        if status not in _TODO_MARKS:
            status = "pending"
        if content:
            cleaned.append({"content": content, "status": status})
    _TODO_STATE = cleaned
    if not cleaned:
        return "Todo list cleared."
    return "Todo list:\n" + "\n".join(
        f"{_TODO_MARKS[t['status']]} {t['content']}" for t in cleaned
    )


# Coding tool schemas — voice-tier edit workflow:
#   read (read_file / file_search) → edit → verify (run_bash) → summarize
_write_file_schema = FunctionSchema(
    name="write_file",
    description=(
        "Create or overwrite a text file inside the repo. "
        "Use after read_file/file_search to understand the code, then "
        "write_file/edit_file to change it, then run_bash to verify "
        "(e.g. python -m py_compile), and finally summarize for voice "
        "(1-2 sentences)."
    ),
    properties={
        "path": {
            "type": "string",
            "description": "Path relative to repo root or absolute path under the repo",
        },
        "content": {
            "type": "string",
            "description": "Full text content to write into the file",
        },
    },
    required=["path", "content"],
)

_edit_file_schema = FunctionSchema(
    name="edit_file",
    description=(
        "Replace an exact substring in a file. The old_string must appear "
        "exactly once. Use read_file first to find the exact text, then "
        "edit_file, then run_bash to verify (py_compile or similar), and "
        "summarize for voice (1-2 sentences)."
    ),
    properties={
        "path": {
            "type": "string",
            "description": "Path relative to repo root or absolute path under the repo",
        },
        "old_string": {
            "type": "string",
            "description": "Exact text to find and replace (must match exactly once)",
        },
        "new_string": {
            "type": "string",
            "description": "Replacement text",
        },
    },
    required=["path", "old_string", "new_string"],
)

_run_bash_schema = FunctionSchema(
    name="run_bash",
    description=(
        "Run a shell command with cwd set to the repo root. "
        "Use to verify edits: 'python -m py_compile <file>' or "
        "'git diff'. Dangerous commands (rm -rf, sudo, etc.) are blocked. "
        "After verification, summarize the result for voice in 1-2 sentences."
    ),
    properties={
        "command": {
            "type": "string",
            "description": "Shell command to execute",
        },
        "timeout_sec": {
            "type": "integer",
            "description": "Max seconds before the command is killed (default 30)",
        },
    },
    required=["command"],
)

_list_files_schema = FunctionSchema(
    name="list_files",
    description=(
        "List files in the repo matching a glob pattern. "
        "Use to discover files before reading or editing them."
    ),
    properties={
        "pattern": {
            "type": "string",
            "description": (
                "Glob pattern relative to repo root, e.g. "
                "'prototype/**/*.py' or '**/*.json'"
            ),
        },
    },
    required=["pattern"],
)


_grep_schema = FunctionSchema(
    name="grep",
    description=(
        "Search file CONTENTS with a regular expression and return matching "
        "lines as path:line: text. Use to locate code (e.g. where a function is "
        "defined or called). Scope with path (file or subdir) and glob "
        "(e.g. '**/*.py')."
    ),
    properties={
        "pattern": {
            "type": "string",
            "description": "Python regular expression to search for",
        },
        "path": {
            "type": "string",
            "description": "Optional file or subdirectory (relative to repo root) to scope the search",
        },
        "glob": {
            "type": "string",
            "description": "Optional filename filter when path is a directory, e.g. '**/*.py'",
        },
    },
    required=["pattern"],
)


_todo_schema = FunctionSchema(
    name="todo",
    description=(
        "Maintain a working task list for a multi-step job so progress is "
        "visible. Send the FULL list each time (it replaces the previous one); "
        "mark items in_progress/completed as you go. Use for tasks with 3+ "
        "steps."
    ),
    properties={
        "todos": {
            "type": "array",
            "description": "The full task list",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "What the step is"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                    },
                },
                "required": ["content"],
            },
        },
    },
    required=["todos"],
)


_question_schema = FunctionSchema(
    name="question",
    description=(
        "Ask the user ONE clarifying question when you are genuinely blocked "
        "and cannot proceed safely (missing project, ambiguous target, "
        "destructive choice). The question is shown and spoken; the user's "
        "answer arrives on the next turn."
    ),
    properties={
        "question": {
            "type": "string",
            "description": "The single question to ask",
        },
    },
    required=["question"],
)


_search_sessions_schema = FunctionSchema(
    name="search_sessions",
    description=(
        "Search PAST conversations (full-text) and return matching messages. "
        "Use when the user refers to something discussed earlier ('what did we "
        "decide about X', 'the bug we hit yesterday')."
    ),
    properties={
        "query": {"type": "string", "description": "Keywords to search for"},
        "limit": {
            "type": "integer",
            "description": "Max messages to return (default 8)",
        },
    },
    required=["query"],
)


_repo_map_schema = FunctionSchema(
    name="repo_map",
    description=(
        "Show a bounded structural map (file tree, up to 4 levels deep) of the "
        "active project. Use it to orient yourself before reading or editing "
        "instead of globbing thousands of files."
    ),
    properties={
        "path": {
            "type": "string",
            "description": "Optional subdirectory (relative to repo root)",
        },
        "max_entries": {
            "type": "integer",
            "description": "Max files to list (default 200)",
        },
    },
    required=[],
)


_diagnostics_schema = FunctionSchema(
    name="diagnostics",
    description=(
        "Report compile errors and lint diagnostics for a file (Python). Call "
        "right after editing code and fix what it reports before finishing."
    ),
    properties={
        "path": {
            "type": "string",
            "description": "File to check (relative to repo root)",
        },
    },
    required=["path"],
)


_lsp_schema = FunctionSchema(
    name="lsp",
    description=(
        "Language-server actions on a file: 'symbols' (outline of functions/"
        "classes), 'definition' (where a name is defined — needs line+character), "
        "'hover' (docs/type at line+character). Lines/characters are 1-based."
    ),
    properties={
        "action": {"type": "string", "enum": ["symbols", "definition", "hover"]},
        "path": {"type": "string", "description": "File relative to repo root"},
        "line": {"type": "integer", "description": "1-based line (definition/hover)"},
        "character": {"type": "integer", "description": "1-based column (definition/hover)"},
    },
    required=["action", "path"],
)


_plan_mode_schema = FunctionSchema(
    name="plan_mode",
    description=(
        "Turn research-only plan mode ON/OFF. While ON, edit and shell tools "
        "are denied (except writing the plan file) so you can investigate "
        "safely before changing code. Turn it OFF to execute."
    ),
    properties={
        "enabled": {"type": "boolean", "description": "true = plan mode on"},
    },
    required=["enabled"],
)


_tools_schema = FunctionSchema(
    name="tools",
    description=(
        "List the tools you can call, with a one-line description of each. Use "
        "when unsure what you can do."
    ),
    properties={},
    required=[],
)


_tasks_schema = FunctionSchema(
    name="tasks",
    description=(
        "List background tasks (orchestration/dispatch jobs) with their status. "
        "Use to check what is still running."
    ),
    properties={},
    required=[],
)


_agents_status_schema = FunctionSchema(
    name="agents_status",
    description=(
        "What each of your agents is doing right now: agent, brief title, "
        "status, elapsed time, last note and any verdict/reasons. Only live "
        "work is listed; a task disappears as soon as it is verified. Use when "
        "you need detail beyond the live status note."
    ),
    properties={},
    required=[],
)


_team_schema = FunctionSchema(
    name="team",
    description=(
        "Manage the agent team. Actions: 'list' shows every agent with name, "
        "title, responsibilities and status; 'counts' shows title -> count; "
        "'hire' proposes a new agent (requires name, title, responsibilities — "
        "does NOT create yet); 'confirm' creates the proposed agent; 'retire' "
        "deactivates a named agent."
    ),
    properties={
        "action": {
            "type": "string",
            "enum": ["list", "counts", "hire", "confirm", "retire"],
            "description": "What to do on the team",
        },
        "name": {"type": "string", "description": "Agent name (hire/retire)"},
        "title": {"type": "string", "description": "Job title (hire)"},
        "responsibilities": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Responsibilities list (hire)",
        },
    },
    required=["action"],
)


_delegate_schema = FunctionSchema(
    name="delegate",
    description=(
        "Hand a piece of work to one of your permanent agents. Write the brief "
        "yourself (goal, the files it may touch, what not to do, how to verify, "
        "what to report). Use it for work that is long, self-contained or "
        "parallel — then keep talking to the user; the agent's result comes back "
        "to you and you report it. Agents cannot talk to the user and must never "
        "be given instructions they could not verify."
    ),
    properties={
        "goal": {
            "type": "string",
            "description": "What the agent must accomplish, in full",
        },
        "files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The only files the agent may touch",
        },
        "do_not": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Things the agent must not do",
        },
        "verify": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Commands/checks the agent must run and show output for",
        },
        "agent": {
            "type": "string",
            "description": "Optional agent id; omit and a free one is chosen",
        },
        "model": {
            "type": "string",
            "description": "Optional model override — paid only if the user asked",
        },
        "title": {
            "type": "string",
            "description": "Prefer an agent with this title; any agent may still "
            "take it",
        },
    },
    required=["goal"],
)


_skill_list_schema = FunctionSchema(
    name="skill_list",
    description="List the reusable skills you have learned (name + description).",
    properties={},
    required=[],
)


_skill_get_schema = FunctionSchema(
    name="skill_get",
    description=(
        "Fetch the full step-by-step body of a learned skill before redoing a "
        "task you have a skill for."
    ),
    properties={
        "name": {"type": "string", "description": "Skill name from skill_list"},
    },
    required=["name"],
)


_skill_save_schema = FunctionSchema(
    name="skill_save",
    description=(
        "Save a reusable skill after solving a non-trivial task, so you can "
        "repeat it faster next time. Provide a short description (<=60 chars) "
        "and a concise markdown body of the steps."
    ),
    properties={
        "name": {"type": "string", "description": "Short kebab-case name"},
        "description": {"type": "string", "description": "<=60 char summary"},
        "body": {"type": "string", "description": "Markdown steps/recipe"},
    },
    required=["name", "description", "body"],
)


_cron_schema = FunctionSchema(
    name="cron",
    description=(
        "Manage scheduled automations. action=list shows jobs; action=add/"
        "remove manages them. A job needs a request and either a 5-field cron "
        "expression or an 'every' interval in seconds."
    ),
    properties={
        "action": {"type": "string", "enum": ["list", "add", "remove"]},
        "name": {"type": "string", "description": "Job name"},
        "request": {"type": "string", "description": "What to run"},
        "cron": {"type": "string", "description": "5-field cron, e.g. '0 9 * * *'"},
        "every": {"type": "integer", "description": "Interval in seconds"},
    },
    required=["action"],
)


_mcp_list_schema = FunctionSchema(
    name="mcp_list_tools",
    description=(
        "List the tools available from configured MCP servers (external tool "
        "servers). Use before mcp_call_tool when you need an outside capability."
    ),
    properties={
        "server": {
            "type": "string",
            "description": "Optional server name to narrow the listing",
        },
    },
    required=[],
)


_mcp_call_schema = FunctionSchema(
    name="mcp_call_tool",
    description=(
        "Call a tool on a configured MCP server and return its text output. "
        "Use mcp_list_tools first to discover server names and tool names."
    ),
    properties={
        "server": {"type": "string", "description": "MCP server name"},
        "tool": {"type": "string", "description": "Tool name on that server"},
        "arguments": {
            "type": "object",
            "description": "Arguments object for the tool",
        },
    },
    required=["server", "tool"],
)


_read_screen_schema = FunctionSchema(
    name="read_screen",
    description=(
        "See what is on the user's screen right now. Use it whenever the answer "
        "depends on what they can see rather than on something they said — "
        "'this', 'here', 'that error', a design, a chart, an app, anything they "
        "did not spell out. On-device OCR always; with a `question` a vision "
        "model reads the picture too. Never ask the user to describe their "
        "screen when you can look."
    ),
    properties={
        "question": {
            "type": "string",
            "description": "Optional: what to read off the screen (uses the vision model)",
        },
    },
    required=[],
)

_look_at_image_schema = FunctionSchema(
    name="look_at_image",
    description=(
        "Look at an image file (screenshot, exported design, photo) and answer "
        "a question about it. Use it for any image on disk the user refers to."
    ),
    properties={
        "path": {"type": "string", "description": "Path to the image file"},
        "question": {"type": "string", "description": "What to find in the image"},
    },
    required=["path"],
)

_look_through_camera_schema = FunctionSchema(
    name="look_through_camera",
    description=(
        "Look through the user's camera right now and describe what it sees. "
        "Use it when the answer depends on something in front of them — 'what "
        "is this', a document, an object, a whiteboard, a label. On-device OCR "
        "always; with a `question` a vision model reads the picture too. Never "
        "ask the user to describe what the camera can see."
    ),
    properties={
        "question": {
            "type": "string",
            "description": "Optional: what to look for through the camera (uses the vision model)",
        },
    },
    required=[],
)


# ── Figma (REST) ─────────────────────────────────────────────────────────────
# Figma's remote MCP server is gated to its own client catalog, so Jarvis reads
# designs over the official REST API with the shared OAuth token. Output is
# bounded so a large file cannot flood the context.
_FIGMA_MAX_FILES = 50
_FIGMA_MAX_LINES = 200
_FIGMA_MAX_CHARS = 6000


def _figma_client():
    return figma_rest.FigmaClient()


def _figma_depth(value) -> int:
    try:
        depth = int(value)
    except (TypeError, ValueError):
        return figma_rest.DEFAULT_DEPTH
    return depth if depth >= 1 else figma_rest.DEFAULT_DEPTH


def _figma_clip(lines, *, limit=_FIGMA_MAX_LINES, chars=_FIGMA_MAX_CHARS) -> str:
    kept, used, cut = [], 0, False
    for line in lines:
        if len(kept) >= limit or used + len(line) + 1 > chars:
            cut = True
            break
        kept.append(line)
        used += len(line) + 1
    text = "\n".join(kept)
    if cut:
        text += f"\n… (truncated to {limit} lines / {chars} chars)"
    return text


def _figma_tree_lines(node, indent=0, lines=None):
    if lines is None:
        lines = []
    if not isinstance(node, dict):
        return lines
    label = node.get("name") or ""
    ntype = node.get("type") or "NODE"
    nid = node.get("id") or ""
    line = f"{'  ' * indent}{ntype} {label} (#{nid})"
    chars = node.get("characters")
    if chars:
        line += f" — {str(chars)[:80]!r}"
    lines.append(line)
    for child in node.get("children") or []:
        _figma_tree_lines(child, indent + 1, lines)
    return lines


def figma_files(args, *, client=None) -> str:
    """Tool body: list a Figma project's or folder's files."""
    args = args or {}
    project_id = str(args.get("project_id") or "").strip() or None
    folder_id = str(args.get("folder_id") or "").strip() or None
    try:
        files = (client or _figma_client()).list_files(
            project_id, folder_id=folder_id,
            branch_data=bool(args.get("branch_data")))
    except figma_rest.FigmaError as exc:
        return str(exc)
    if not files:
        return "No Figma files found in that project or folder."
    lines = [f"Figma files ({len(files)}):"]
    for f in files[:_FIGMA_MAX_FILES]:
        lines.append(
            f"- key={f.get('key')}  {f.get('name')}  "
            f"(modified {f.get('last_modified')})")
    if len(files) > _FIGMA_MAX_FILES:
        lines.append(f"… and {len(files) - _FIGMA_MAX_FILES} more")
    return _figma_clip(lines)


def figma_file(args, *, client=None) -> str:
    """Tool body: read one Figma file's bounded page/frame tree."""
    args = args or {}
    key = str(args.get("key") or args.get("file_key") or "").strip()
    if not key:
        return "Error: a Figma file key is required (figma_file key=...)."
    try:
        data = (client or _figma_client()).get_file(
            key, depth=_figma_depth(args.get("depth")))
    except figma_rest.FigmaError as exc:
        return str(exc)
    if not data.get("document"):
        return f"Figma file {key} returned no document."
    lines = [f"Figma file {data.get('name')!r} "
             f"(version {data.get('version')}, {data.get('editorType')})",
             "Pages and nodes:"]
    _figma_tree_lines(data["document"], 0, lines)
    if data.get("truncated"):
        lines.append("… (Figma tree itself truncated; lower depth or use figma_nodes)")
    return _figma_clip(lines)


def figma_nodes(args, *, client=None) -> str:
    """Tool body: read specific Figma nodes by id."""
    args = args or {}
    key = str(args.get("file_key") or args.get("key") or "").strip()
    ids = args.get("node_ids")
    if not key or not ids:
        return "Error: file_key and node_ids are required (figma_nodes ...)."
    try:
        data = (client or _figma_client()).read_nodes(
            key, ids, depth=_figma_depth(args.get("depth")))
    except figma_rest.FigmaError as exc:
        return str(exc)
    nodes = data.get("nodes") or {}
    if not nodes and not data.get("missing"):
        return "Figma returned no nodes."
    lines = [f"Figma nodes in {key}:"]
    for node_id, node in nodes.items():
        lines.append(f"- (#{node_id})")
        _figma_tree_lines(node, 1, lines)
    if data.get("missing"):
        lines.append("Missing (no such node): " + ", ".join(data["missing"]))
    if data.get("truncated"):
        lines.append("… (tree itself truncated; use a lower depth)")
    return _figma_clip(lines)


_figma_files_schema = FunctionSchema(
    name="figma_files",
    description=(
        "List the files in a Figma project or folder. Figma has no \"list all "
        "my files\" endpoint, so give a project_id or folder_id (from its "
        "URL). Returns each file's key, name and last-modified time; pass the "
        "key to figma_file or figma_nodes."
    ),
    properties={
        "project_id": {"type": "string", "description": "Figma project id"},
        "folder_id": {"type": "string", "description": "Figma folder id"},
        "branch_data": {"type": "boolean",
                        "description": "Include branch metadata"},
    },
    required=[],
)

_figma_file_schema = FunctionSchema(
    name="figma_file",
    description=(
        "Read a Figma design file's structure by file key: pages and, up to "
        "depth, their frames, layers and text. Use depth=1 for just pages; go "
        "deeper only when needed — output is bounded."
    ),
    properties={
        "key": {"type": "string",
                "description": "Figma file key (from /file/<key>/... in its URL)"},
        "depth": {"type": "integer",
                  "description": "Tree depth; 1 = pages only (default 1)"},
    },
    required=["key"],
)

_figma_nodes_schema = FunctionSchema(
    name="figma_nodes",
    description=(
        "Read specific nodes (frames, layers, text) from a Figma file by id — "
        "the focused alternative to figma_file. Node ids look like '12:34'; "
        "up to 50 per call. Ids that don't exist come back as missing."
    ),
    properties={
        "file_key": {"type": "string", "description": "Figma file key"},
        "node_ids": {"type": "string",
                     "description": "Comma-separated node ids, e.g. '12:34,56:78'"},
        "depth": {"type": "integer",
                  "description": "Tree depth; 1 = the node only (default 1)"},
    },
    required=["file_key", "node_ids"],
)

_apply_patch_schema = FunctionSchema(
    name="apply_patch",
    description=(
        "Apply a multi-file patch in the OpenAI patch format:\n"
        "*** Begin Patch\n*** Update File: path\n@@\n context\n-old line\n+new line\n"
        "*** Add File: path\n+new line\n*** Delete File: path\n*** End Patch\n"
        "Prefer this over edit_file for multi-file edits or whitespace-sensitive "
        "code: it fuzzy-matches each hunk (exact, trailing-space, whitespace, "
        "unicode). Then run_bash to verify and summarize in 1-2 sentences."
    ),
    properties={
        "patch": {
            "type": "string",
            "description": "The full patch text (Begin Patch ... End Patch)",
        },
    },
    required=["patch"],
)


_switch_project_schema = FunctionSchema(
    name="switch_project",
    description=(
        "Switch Asha's active project folder — the folder it reads, writes, "
        "and runs commands in. Use when the user asks to open / switch to / "
        "work in a project. Call with a name when you heard one; call with no "
        "name to list the recent projects."
    ),
    properties={
        "name": {
            "type": "string",
            "description": "Project or folder name to switch to (optional).",
        },
    },
    required=[],
)


# The Projects board tool. Distinct from switch_project (the repo folder): these
# are the bodies of work the user tracks. Only the brain writes; the store
# enforces that, so this is the one place that passes writer=True.
_BOARD_ACTIONS = frozenset({
    "projects_list", "project_add", "project_update", "project_remove",
    "card_add", "card_update", "card_move", "card_remove", "board_get",
})


def _projects_tool(args) -> str:
    """Run one Projects-board action and return a short human summary."""
    args = args or {}
    store = board_mod.board
    action = str(args.get("action") or "list").strip().lower()
    project_key = str(args.get("project") or "").strip()

    def _project_id() -> str:
        pid = store.resolve_project(project_key)
        if pid is None:
            raise ValueError(f"unknown project {project_key!r}")
        return pid

    try:
        if action in ("list", "projects"):
            projects = store.projects()
            if not projects:
                return "No projects yet."
            if action == "projects":
                return "; ".join(
                    f"{p['name']} ({p['total']} cards)" for p in projects)
            lines = [store.summary_line()]
            for p in projects:
                cards = store.cards(p["id"])
                for col in ("In progress", "Waiting on you"):
                    titles = [c["title"] for c in cards if c["status"] == col]
                    if titles:
                        lines.append(f"{p['name']} {col.lower()}: "
                                     + ", ".join(titles[:8]))
            return "\n".join(lines)
        if action == "add_project":
            name = str(args.get("title") or args.get("project") or "").strip()
            p = store.add_project(name or "New project",
                                  note=args.get("notes", ""),
                                  repo=args.get("repo", ""), writer=True)
            return f"Added project {p['name']}. {store.summary_line()}"
        if action == "add_card":
            pid = _project_id()
            title = str(args.get("title") or "").strip()
            if not title:
                return "Error: add_card needs a title"
            fields = {k: args[k] for k in
                      ("area", "owner", "priority", "needs", "notes", "status")
                      if args.get(k) is not None}
            c = store.add_card(pid, title, writer=True, **fields)
            pname = (store.get_project(pid) or {}).get("name", project_key)
            return f"Added card {c['id']} to {pname}. {store.summary_line()}"
        if action == "update_card":
            cid = str(args.get("card_id") or "").strip()
            fields = {k: args[k] for k in
                      ("title", "area", "owner", "priority", "needs",
                       "notes", "status")
                      if args.get(k) is not None}
            if store.update_card(cid, writer=True, **fields) is None:
                return f"Error: unknown card {cid!r}"
            return f"Updated card {cid}. {store.summary_line()}"
        if action == "move_card":
            cid = str(args.get("card_id") or "").strip()
            status = str(args.get("status") or "").strip()
            if store.move_card(cid, status, writer=True) is None:
                return (f"Error: could not move card {cid!r} to {status!r} "
                        "(unknown card, or not one of the five columns)")
            return f"Moved card {cid} to {status}. {store.summary_line()}"
        if action == "remove_card":
            cid = str(args.get("card_id") or "").strip()
            if not store.remove_card(cid, writer=True):
                return f"Error: unknown card {cid!r}"
            return f"Removed card {cid}. {store.summary_line()}"
        return f"Error: unknown projects action {action!r}"
    except PermissionError as e:
        return f"Error: {e}"
    except Exception as e:  # noqa: BLE001 - surfaces to the model
        return f"Error: {e}"


_projects_schema = FunctionSchema(
    name="projects",
    description=(
        "Track the user's projects (bodies of work) on the Projects board: "
        "list them, add a project, or add/update/move/remove its cards. This is "
        "NOT switch_project (the repo folder Asha codes in) — a project here "
        "is work being tracked and may optionally link to a repo folder. Use "
        "when the user mentions work to do or asks how things are going."
    ),
    properties={
        "action": {
            "type": "string",
            "enum": ["list", "projects", "add_project", "add_card",
                     "update_card", "move_card", "remove_card"],
            "description": "What to do on the board",
        },
        "project": {"type": "string",
                    "description": "Project name or id"},
        "card_id": {"type": "string", "description": "Card id (c1, c2, ...)"},
        "title": {"type": "string", "description": "Card title (or new project name)"},
        "status": {"type": "string", "enum": list(board_mod.STATUSES),
                   "description": "Board column"},
        "owner": {"type": "string", "description": "You | Asha | an agent name"},
        "priority": {"type": "string", "enum": list(board_mod.PRIORITIES)},
        "area": {"type": "string"},
        "needs": {"type": "string", "description": "What the card waits on"},
        "notes": {"type": "string", "description": "One short line"},
        "repo": {"type": "string",
                 "description": "Optional repo folder this project links to"},
    },
    required=["action"],
)


def _voice_tool_schemas() -> list:
    """The chat/voice tier's tool list.

    Module-level so a test can assert a tool is registered here — a tool that
    silently drops out of this list stops being callable by the model.
    """
    return [
        _web_search_schema, _file_search_schema, _read_file_schema,
        _write_file_schema, _edit_file_schema, _run_bash_schema, _list_files_schema,
        _grep_schema,
        _todo_schema, _question_schema,
        _search_sessions_schema,
        _repo_map_schema,
        _mcp_list_schema, _mcp_call_schema,
        _diagnostics_schema,
        _lsp_schema, _plan_mode_schema,
        _tools_schema,
        _tasks_schema,
        _agents_status_schema,
        _team_schema,
        _skill_list_schema, _skill_get_schema, _skill_save_schema,
        _cron_schema,
        _apply_patch_schema,
        _web_fetch_schema, _generate_image_schema, _switch_project_schema,
        _make_video_schema,
        _read_screen_schema, _look_at_image_schema, _look_through_camera_schema,
        _figma_files_schema, _figma_file_schema, _figma_nodes_schema,
        _delegate_schema,
        _projects_schema,
    ]


# KB-08: brain tier (DeepSeek V4 Flash) wired into the coding path only.
# supervisor.py is Big Pickle's — import + call, never modify. The brain
# writes the brief and judges the diff; it never edits (both calls are
# _NO_TOOLS, model-echo verified). Fail-soft everywhere: brain down means
# raw-text brief / worker result kept — never a blocked task.
# The diff/review/apply leg tracks the system-wide active project (Brain +
# workers all share it) via worker_engine.get_repo_root().
_CODE_DIFF_MAX_CHARS = 20000


def _worker_roster(busy_index: int | None = None) -> list[dict]:
    """The 3 muscles with their project/model/thinking + live status — the
    roster the brain reads to decide who does a task."""
    rows: list[dict] = []
    for i, w in enumerate(list_workers()):
        proj = w.get("project") or get_repo_root()
        rows.append({
            "index": i,
            "n": i + 1,
            "name": worker_name(i),
            "project": os.path.basename(os.path.normpath(proj)) if proj else "",
            "path": proj,
            "model": w.get("model") or "",
            "reasoning": w.get("reasoning", "default"),
            "status": "busy" if busy_index == i else "idle",
        })
    return rows


def _worker_roster_text(rows: list[dict]) -> str:
    lines = [
        "WHO DOES THE WORK \u2014 pick exactly ONE worker (prefer the worker whose "
        "project matches the request; else an idle worker; else the first):",
    ]
    for r in rows:
        lines.append(
            f"{r['n']}. {r.get('name') or ('Worker ' + str(r['n']))} \u2014 project: "
            f"{r['project']} ({r['path']}) \u2014 model: {r['model']} \u2014 thinking: "
            f"{r['reasoning']} \u2014 {r['status']}"
        )
    lines.append(
        "Begin your reply with one line exactly `WORKER: <n>` (n = 1..3, the "
        "chosen worker), then the brief sections."
    )
    return "\n".join(lines)


_WORKER_LINE_RE = re.compile(r"^[ \t]*WORKER:[ \t]*(\d+)[ \t]*$", re.M)


def _parse_worker_choice(brief_text: str) -> int | None:
    m = _WORKER_LINE_RE.search(brief_text or "")
    if not m:
        return None
    n = int(m.group(1))
    return (n - 1) if 1 <= int(n) <= 3 else None


def _clean_brief(brief_text: str) -> str:
    return _WORKER_LINE_RE.sub("", brief_text or "").strip()


def _model_provider_id(model_id: str) -> str:
    if "|" in (model_id or ""):  # BYOK composite id: 'provider|model'
        return model_id.split("|", 1)[0]
    try:
        for m in _oc_models_cached():
            if m["id"] == model_id:
                return "opencode-go" if m.get("tier") == "go" else "opencode"
    except Exception:
        pass
    return "opencode"


def _fallback_worker(text: str, rows: list[dict]) -> int:
    low = (text or "").lower()
    for r in rows:
        if r["project"] and r["project"].lower() in low:
            return r["index"]
    for r in rows:
        if r["status"] == "idle":
            return r["index"]
    return rows[0]["index"] if rows else 0


def _code_make_brief(raw_text: str, roster_text: str = "") -> tuple:
    """Brain-written brief (sync — callers run it off the voice hot path).

    Returns (brief_text, used_brain). Fail-soft: any brain failure (or an
    empty brief) falls back to the raw user text + warning, so coding still
    runs without the brain. The brain is a quality layer, not a gate.
    """
    raw = (raw_text or "").strip()
    try:
        brain = supervisor.write_brief(raw, context=roster_text or None,
                                      timeout=15, attempts=1)
        structured = ((brain or {}).get("brief") or "").strip()
        if structured:
            logger.info(
                "[CODE] brain brief used "
                f"(model={brain.get('model')} cost={brain.get('cost')})"
            )
            return structured, True
        logger.warning("[CODE] brain brief empty — falling back to raw text")
    except Exception as e:
        logger.warning(f"[CODE] write_brief failed (fail-soft, raw text): {e!r}")
    return raw, False


def _code_worktree_diff(root: str = "") -> str:
    """VCS reality (agent-delegation §5): git diff + untracked names on the
    repo the task ran in. Read-only — never touches the index.
    Returns '' when the tree is clean. Raises RuntimeError when git fails.
    """
    try:
        root = root or get_repo_root() or str(Path(__file__).resolve().parent.parent)
        diff = subprocess.run(
            ["git", "-C", root, "diff", "--"],
            capture_output=True, text=True, timeout=30,
        )
        status = subprocess.run(
            ["git", "-C", root, "status", "--porcelain", "--"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        raise RuntimeError(f"git unavailable: {e!r}")
    if diff.returncode != 0:
        raise RuntimeError(f"git diff failed: {(diff.stderr or '')[:200]}")
    out = diff.stdout or ""
    if status.returncode == 0 and status.stdout:
        untracked = [ln for ln in status.stdout.splitlines() if ln.startswith("??")]
        if untracked:
            out += "\nUntracked files:\n" + "\n".join(untracked) + "\n"
    return out[:_CODE_DIFF_MAX_CHARS]


def _code_review_diff(diff_text: str, brief_text: str):
    """Brain-judged diff (sync — callers run it off the voice hot path).

    Returns the review dict (verdict accept|rework|reject, summary, issues)
    or None when there is nothing to judge (clean tree) or on any brain
    failure (fail-soft: the worker result is kept, never lost).
    """
    if not (diff_text or "").strip():
        return None
    try:
        review = supervisor.review_diff(diff_text, brief_text or "")
        logger.info(
            "[CODE] brain verdict: "
            f"{review.get('verdict')} ({(review.get('summary') or '')[:120]})"
        )
        return review
    except Exception as e:
        logger.warning(f"[CODE] review_diff failed (fail-soft, keeping result): {e!r}")
        return None


async def _ui_write_transport_frame(self, frame):
    try:
        if not isinstance(frame, str) and type(frame).__name__ == "StatusFrame":
            logger.info("[STATUS] outbound StatusFrame object")
    except Exception as e:
        logger.warning(f"[STATUS] outbound frame inspection failed: {type(frame).__name__}: {e!r}")
    await self._write_frame(frame)


SingleClientWebsocketServerOutputTransport.write_transport_frame = _ui_write_transport_frame  # noqa: E402


class _CloudProvider:
    """One row of the notes/brain-routing.md §6A provider table.

    Credentials + model are fixed at construction (from .env); only
    ``cooldown_until`` / ``probing`` mutate at runtime.
    """

    def __init__(self, name, base_url, api_key, model, client):
        self.name = name
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.client = client
        self.cooldown_until = 0.0  # time.monotonic() ts; 0.0 = healthy
        self.probing = False  # True while a cooled-down provider is retried


def _make_noretry_client(api_key, base_url, default_headers, timeout):
    """AsyncOpenAI client with retries disabled.

    pipecat's create_client() silently drops max_retries/timeout kwargs, so a
    Groq 429 was retried inside the SDK (default max_retries=2, honoring the
    Retry-After header ≈15s) before our router ever saw it. Constructing
    directly makes max_retries=0 effective: the 429 raises to
    get_chat_completions in milliseconds and rotation fires. Otherwise
    identical to pipecat's client (same connection limits).

    timeout may be a float (uniform) or an httpx.Timeout (per-phase: connect /
    read / write / pool). A wide read is used so the brain can keep "thinking"
    (streaming reasoning) without being cut by the httpx client.
    """
    return AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        http_client=DefaultAsyncHttpxClient(
            limits=httpx.Limits(
                max_keepalive_connections=100, max_connections=1000, keepalive_expiry=None
            )
        ),
        default_headers=default_headers,
        max_retries=0,
        timeout=timeout,
    )


def _is_rate_limit(exc: Exception) -> bool:
    """Belt-and-suspenders 429 detection: explicit RateLimitError check plus
    the duck-typed status_code other SDK errors carry."""
    if isinstance(exc, RateLimitError):
        return True
    return getattr(exc, "status_code", None) == 429


# Minimal, bounded stand-in for a tool result that never arrived. Synthesizing
# it keeps an assistant `tool_calls` pair complete instead of 400-ing dispatch.
_MISSING_TOOL_RESULT = "[tool result not received]"


class BoundedContextLLM(OpenAILLMService):
    """Keeps chat history under the model's context window so the pipeline
    can never die from a 400 exceed-context error.

    Provider model (hardwired 2026-09-17 per user decision): ONLY
    BRAIN_MODEL_ID on the go gateway — no picker, no fallback / rotation.
    When its provider is missing (e.g. no API key) turns fail plainly
    ('unavailable') instead of quietly running something else. System-
    instruction handling is untouched (it lives in ``self._settings`` and is
    composed per request regardless of provider).
    """

    MAX_CHARS = int(os.environ.get("LLM_CONTEXT_MAX_CHARS", "220000"))

    def __init__(self, *args, emit_brain_cb=None, emit_ui_cb=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._emit_brain_cb = emit_brain_cb
        self._emit_ui_cb = emit_ui_cb or emit_brain_cb
        try:
            cloud_timeout = float(os.environ.get("CLOUD_TIMEOUT", "5"))
        except ValueError:
            cloud_timeout = 5.0
        self._cloud_timeout = cloud_timeout
        try:
            self._fallback_cooldown = float(os.environ.get("FALLBACK_COOLDOWN", "60"))
        except ValueError:
            self._fallback_cooldown = 60.0
        # Idle watchdog: only fires when NOTHING arrives (no content, no
        # reasoning, no tool deltas) for this long. A brain actively streaming
        # reasoning is never cut — "if it's thinking, we can't time out".
        try:
            self._idle_timeout = float(os.environ.get("CLOUD_IDLE_TIMEOUT", "60"))
        except ValueError:
            self._idle_timeout = 60.0
        self._providers: dict = {}
        self._active_provider: _CloudProvider | None = None
        # Runtime brain-model preference (brain_model_set from the picker/strip).
        # None = Auto (free-first chain order); a model is forced to the front.
        self._preferred_model: str | None = None
        # Fallback across FREE providers only, opt-in (default OFF — respects the
        # "no silent fallback" decision). Never falls back to go/paid models.
        self._fallback_enabled = os.environ.get("LLM_FALLBACK", "0").strip().lower() in ("1", "true", "yes")
        self._free_models: set[str] = set()
        self._go_models: set[str] = set()
        # Per-turn reasoning surfacing state (reset in get_chat_completions).
        self._reasoning_started = False
        self._reasoning_buf = ""
        self._reasoning_last = -1.0
        self._reasoning_flush_on = None
        # Hardwired brain (2026-09-17): the single BRAIN_MODEL_ID. Never
        # None — when the provider is missing (e.g. no key) the error path
        # says so plainly instead of falling back to something else.
        self._selected_model: str = BRAIN_MODEL_ID
        # Build one provider per deployable opencode model (zen + go). Built
        # again when an API key is added at runtime (rebuild_providers).
        self._build_opencode_providers()
        # Tool round cap (voice-tier coding tools): at most _MAX_TOOL_ROUNDS
        # agentic tool-using rounds per voice turn, then force text-only.
        self._tool_rounds: int = 0
        # Auto-continue: if the model stops to ask after doing some work, we
        # re-prompt it to finish (bounded). Reset per user turn.
        self._turn_tools: int = 0
        self._turn_continues: int = 0
        self._turn_writes: int = 0
        self._turn_wants_write: bool = False
        self._write_nudged: bool = False
        # Full reasoning text captured for the CURRENT turn (thinking mode).
        # _note_reasoning feeds it; the next get_chat_completions attaches it to
        # the assistant message pipecat appended, then clears it.
        self._reasoning_full: str = ""
        # Conversational-turn shortcut (user-approved 2026-09-18): when on, a
        # turn the local classifier marks conversational goes out with thinking
        # disabled. Tests/measurement flip this to compare with/without.
        self._conversational_shortcut = True
        # Set by run_prototype: executes a text-format <tool_call> (MiMo's
        # fallback tool format) exactly like the native pipecat tool handlers.
        self._text_tool_executor = None
        # Bound base method for the post-tool re-ask (avoids recursion through
        # BoundedContextLLM.get_chat_completions' provider chain).
        self._base_completions = BaseOpenAILLMService.get_chat_completions

    def _build_opencode_providers(self):
        """Build one provider per deployable opencode model (zen + go).

        The chain uses ONLY the selected model — no fallback/rotation (user
        decision: no backup to go models; go models are picked on purpose,
        never used as a silent backup). The opencode list below needs
        OPENCODE_API_KEY; the hardwired brain model itself is (re)built from
        the resolved transport (see ``_resolve_brain_transport``), so it runs
        on OmniRoute/DeepSeek without any OpenCode key."""
        import httpx as _httpx
        self._providers = {}
        self._free_models = set()
        self._go_models = set()
        opencode_key = os.environ.get("OPENCODE_API_KEY", "")
        zen_base = "https://opencode.ai/zen/v1"
        # The opencode model list always points at the opencode gateway; the
        # hardwired brain model is overridden by the transport below.
        go_base = _DEV_OPENCODE_BASE_URL
        opencode_headers = {
            # Validated opencode-client request shape (KB-06 follow-on):
            # x-opencode-project/session/request + client:cli + opencode UA.
            # uuid4/custom UA shapes get flagged as abuse → 429 even under
            # quota. Per-call ses/msg ids are minted at construction.
            "x-opencode-project": _oc_ulid("wrk"),
            "x-opencode-client": "cli",
            "User-Agent": "opencode/1.18.29",
            "x-opencode-session": _oc_ulid("ses"),
            "x-opencode-request": _oc_ulid("msg"),
        }
        # Per-phase httpx timeout: connect/write/pool use CLOUD_TIMEOUT; read is
        # wide so the idle watchdog (not httpx) does the breaking — and only on
        # total silence. Continuous reasoning chunks keep the read alive.
        client_timeout = _httpx.Timeout(
            connect=self._cloud_timeout,
            read=self._idle_timeout + 30,
            write=self._cloud_timeout,
            pool=self._cloud_timeout,
        )
        built = 0
        if opencode_key:
            go_key = os.environ.get("SUPERVISOR_API_KEY", "") or opencode_key
            for m in _oc_models_cached():
                mid, tier = m["id"], m.get("tier", "free")
                if not mid or mid in self._providers:
                    continue
                if tier == "go":
                    self._go_models.add(mid)
                    self._providers[mid] = _CloudProvider(
                        name=f"OpenCode-Go-{mid}",
                        base_url=go_base,
                        api_key=go_key,
                        model=mid,
                        client=_make_noretry_client(
                            api_key=go_key,
                            base_url=go_base,
                            default_headers=opencode_headers,
                            timeout=client_timeout,
                        ),
                    )
                else:
                    self._free_models.add(mid)
                    self._providers[mid] = _CloudProvider(
                        name=f"OpenCode-{mid}",
                        base_url=zen_base,
                        api_key=opencode_key,
                        model=mid,
                        client=_make_noretry_client(
                            api_key=opencode_key,
                            base_url=zen_base,
                            default_headers=opencode_headers,
                            timeout=client_timeout,
                        ),
                    )
                built += 1
        else:
            logger.warning("[LLM] no OPENCODE_API_KEY — opencode models skipped")
        # BYOK: one entry per (provider, model), keyed 'provider|model' so ids
        # from different providers never collide.
        for pid, label, models in _byok_model_sources():
            try:
                prov = providers_pkg.get_provider(pid)
            except Exception:  # noqa: BLE001
                continue
            base = getattr(prov, "base_url", "")
            key = getattr(prov, "api_key", "")
            if not base:
                continue
            for m in models:
                mid = m.get("id") if isinstance(m, dict) else getattr(m, "id", "")
                if not mid:
                    continue
                cid = f"{pid}|{mid}"
                if cid in self._providers:
                    continue
                self._providers[cid] = _CloudProvider(
                    name=f"{label} \u00b7 {mid}",
                    base_url=base,
                    api_key=key,
                    model=mid,
                    client=_make_noretry_client(
                        api_key=key or "none",
                        base_url=base,
                        default_headers={},
                        timeout=client_timeout,
                    ),
                )
                built += 1
        # Brain transport (2026-09-18): the one hardwired model resolves from
        # the env-driven transport, so a packaged app talks to OmniRoute (or
        # DeepSeek direct) without OpenCode. Only built when the transport is
        # usable — a key is present, or it is a keyless local gateway —
        # otherwise the model stays plainly unavailable exactly as before. The
        # opencode transport is left to the loop above when it already built
        # the model (identical base/key/headers), avoiding a duplicate client.
        if (BRAIN_BASE_URL
                and (BRAIN_API_KEY or BRAIN_TRANSPORT in _KEYLESS_BRAIN_TRANSPORTS)
                and (BRAIN_TRANSPORT != "opencode"
                     or BRAIN_MODEL_ID not in self._providers)):
            # Fidelity: OmniRoute must pass the brain's prompt unchanged; the
            # per-request switch is the documented compression off-switch
            # (spike contract: anjali-20260918-122351/run.log).
            if BRAIN_TRANSPORT == "opencode":
                brain_headers = opencode_headers
            elif BRAIN_TRANSPORT == "omniroute":
                brain_headers = {"x-omniroute-compression": "off"}
            else:
                brain_headers = {}
            if BRAIN_MODEL_ID not in self._providers:
                built += 1
            self._providers[BRAIN_MODEL_ID] = _CloudProvider(
                name=f"Brain-{BRAIN_TRANSPORT}-{BRAIN_MODEL_ID}",
                base_url=BRAIN_BASE_URL,
                api_key=BRAIN_API_KEY,
                model=BRAIN_MODEL_ID,
                client=_make_noretry_client(
                    api_key=BRAIN_API_KEY or "none",
                    base_url=BRAIN_BASE_URL,
                    default_headers=brain_headers,
                    timeout=client_timeout,
                ),
            )
        if built:
            logger.info(f"[LLM] built {built} brain providers (opencode + BYOK)")


    def rebuild_providers(self):
        """Rebuild the opencode providers now (called after an API key is
        saved at runtime) and re-apply the hardwired brain model."""
        self._build_opencode_providers()
        self.set_brain_model(BRAIN_MODEL_ID)

    @staticmethod
    def _delta_text(chunk):
        """Pull the incremental text content out of one streamed chunk
        (dict or SDK object), matching how pipecat reads it."""
        try:
            if isinstance(chunk, dict):
                choices = chunk.get("choices") or []
                if not choices:
                    return ""
                delta = choices[0].get("delta") or {}
                return delta.get("content") or ""
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                return ""
            delta = getattr(choices[0], "delta", None)
            if delta is None:
                return ""
            return getattr(delta, "content", "") or ""
        except Exception:
            return ""

    @staticmethod
    def _delta_toolcall(chunk) -> bool:
        """True when this streamed chunk carries a tool-call delta."""
        try:
            if isinstance(chunk, dict):
                choices = chunk.get("choices") or []
                delta = (choices[0].get("delta") or {}) if choices else {}
                return bool(delta.get("tool_calls"))
            choices = getattr(chunk, "choices", None) or []
            delta = getattr(choices[0], "delta", None) if choices else None
            return bool(getattr(delta, "tool_calls", None))
        except Exception:
            return False

    async def _maybe_continue(self, context, stream, has_tools):
        """Pass a stream through; if the model stops to ASK a question after
        having done some work (but no tool call in this final reply), nudge it
        to finish — bounded, so a weak model can't stall on "what next?"."""
        text_buf: list[str] = []
        saw_tool = False
        async for chunk in stream:
            if self._delta_toolcall(chunk):
                saw_tool = True
            d = self._delta_text(chunk)
            if d:
                text_buf.append(d)
            yield chunk
        if not has_tools:
            return
        # A requested write that hasn't happened yet is unfinished work: keep
        # pushing the model to produce it (bounded), even alongside tool calls.
        wants_write_unmet = (
            self._turn_wants_write and self._turn_writes == 0
            and self._turn_tools > 0
        )
        if wants_write_unmet and self._turn_continues < 4:
            self._turn_continues += 1
            logger.info(
                f"[LLM] requested write still pending after {self._turn_tools} "
                f"tool call(s) \u2192 write nudge #{self._turn_continues}"
            )
            context.set_messages(list(context.messages) + [
                {"role": "system", "content": (
                    "You have NOT produced the file the user asked for yet. Stop "
                    "researching and call write_file NOW with the exact path and "
                    "the full content. Do not describe it \u2014 write it."
                )},
            ])
            if saw_tool:
                return  # pipecat re-invokes with the nudge in context
            retry = await self._base_completions(self, context)
            async for c in self._maybe_continue(context, retry, has_tools):
                yield c
            return
        if saw_tool:
            return
        final = "".join(text_buf).strip()
        low = final.lower()
        stopped = final.endswith("?") or any(p in low for p in (
            "would you like", "what would you like", "which one", "let me know",
            "do you want me to", "shall i", "what next", "what's next",
            "what should", "let me know if",
        ))
        wants_write_unmet = (
            self._turn_wants_write and self._turn_writes == 0
            and self._turn_tools > 0
        )
        if self._turn_tools == 0 or self._turn_continues >= 3:
            return
        if not (stopped or wants_write_unmet):
            return
        self._turn_continues += 1
        logger.info(
            f"[LLM] model stopped before finishing ({self._turn_tools} tool "
            f"call(s), writes={self._turn_writes}) \u2192 auto-continue "
            f"#{self._turn_continues}"
        )
        try:
            await self._emit_phase("answering")
        except Exception:
            pass
        if wants_write_unmet and not stopped:
            nudge = (
                "You have researched but you have NOT created the file the user "
                "asked for. Do that now: call write_file with the exact path the "
                "user named and the full content. Do not stop until the file "
                "exists, then give a one or two sentence summary."
            )
        else:
            nudge = (
                "You have NOT finished the task. Do not ask the user what to do "
                "next and do not stop to confirm. Continue using your tools now "
                "and complete the work. Only stop once the deliverable exists, "
                "then give a one or two sentence summary."
            )
        context.set_messages(list(context.messages) + [
            {"role": "system", "content": nudge},
        ])
        retry = await self._base_completions(self, context)
        async for c in self._maybe_continue(context, retry, has_tools):
            yield c

    # Phrases that mean the no-thinking reply actually needed the model to
    # think (a clarifying question). Keep this list small and explicit.
    _CLARIFY_MARKERS = (
        "i need more detail", "need more detail", "more context",
        "more information", "could you clarify", "can you clarify",
        "could you tell me more", "can you tell me more", "what do you mean",
        "which one do you mean", "i'm not sure what", "im not sure what",
        "could you be more specific", "can you be more specific",
    )
    _REASK_BUFFER_CHARS = 160

    @classmethod
    def _looks_like_clarification(cls, text: str) -> bool:
        low = (text or "").strip().lower()
        if not low:
            return False
        return any(marker in low for marker in cls._CLARIFY_MARKERS)

    async def _conversational_reask(self, context, stream):
        """Safety net for the thinking-off shortcut.

        Pass the conversational stream through. If the model turns out to have
        needed the turn after all — it emits a tool call, or answers with a
        clarifying question / "I need more detail" — the no-thinking attempt is
        discarded (before anything is spoken: tool calls carry no text, and a
        doubtful reply is caught at its first sentence boundary) and the turn
        is re-asked ONCE with the model's normal thinking. If the no-thinking
        stream itself errors before anything was spoken, the same retry runs so
        the turn never fails. If it errors after text was released, the error
        propagates exactly as it would on a work turn.
        """
        pending: list = []
        text = ""
        released = False

        def _doubt() -> str:
            if any(self._delta_toolcall(c) for c in pending):
                return "tool-call"
            if self._looks_like_clarification(text):
                return "clarification"
            return ""

        def _boundary() -> bool:
            return (
                len(text) >= self._REASK_BUFFER_CHARS
                or text.rstrip().endswith((".", "!", "?"))
            )

        async def _retry():
            self._settings.extra = _reasoning_extra()
            retry_stream = await self._base_completions(self, context)
            async for c in retry_stream:
                yield c

        try:
            async for chunk in stream:
                if released:
                    yield chunk
                    continue
                pending.append(chunk)
                text += self._delta_text(chunk)
                reason = _doubt()
                if reason:
                    logger.info(
                        f"[LLM] conversational reply doubted ({reason}) \u2014 "
                        "re-asking once with thinking on"
                    )
                    async for c in _retry():
                        yield c
                    return
                if _boundary():
                    for c in pending:
                        yield c
                    pending = []
                    released = True
        except Exception:
            if not released:
                logger.info(
                    "[LLM] conversational no-thinking stream failed \u2014 "
                    "re-asking once with thinking on"
                )
                async for c in _retry():
                    yield c
                return
            raise
        if pending:
            for c in pending:
                yield c

    @staticmethod
    def _delta_reasoning(chunk):
        """Pull the incremental reasoning text out of one streamed chunk
        (dict or SDK object). Empty when the chunk has no thinking text."""
        try:
            if isinstance(chunk, dict):
                choices = chunk.get("choices") or []
                if not choices:
                    return ""
                delta = choices[0].get("delta") or {}
                return delta.get("reasoning") or ""
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                return ""
            delta = getattr(choices[0], "delta", None)
            if delta is None:
                return ""
            return getattr(delta, "reasoning", "") or ""
        except Exception:
            return ""

    @staticmethod
    def _text_chunk(text):
        """Synthetic streamed chunk carrying already-buffered plain text, as a
        light object so pipecat's _process_context can read delta.content."""
        from types import SimpleNamespace as _NS
        return _NS(
            usage=None,
            model=None,
            choices=[_NS(index=0, delta=_NS(content=text, tool_calls=None), finish_reason=None)],
        )

    async def _intercept_text_tool_calls(self, context, stream, remaining=3):
        """Consume a model stream; if MiMo emits an opencode-style text
        <tool_call>, execute the tool and re-ask the model so the pipeline only
        ever sees the final spoken answer — never the raw tag. Normal replies
        stream through untouched (no latency added). Reasoning deltas are
        surfaced live to the UI (ChatGPT-style "what the brain is doing")."""
        buf = ""
        answered = False
        async for chunk in stream:
            reasoning = self._delta_reasoning(chunk)
            if reasoning:
                await self._note_reasoning(reasoning)
                yield chunk
                continue
            delta = self._delta_text(chunk)
            if not delta:
                yield chunk
                continue
            if not buf and not delta.lstrip().startswith("<"):
                if not answered:
                    answered = True
                    await self._flush_reasoning()
                    await self._emit_phase("answering")
                yield chunk
                continue
            if buf and not buf.lstrip().startswith("<"):
                # previous <...> never became a tag; release it, keep streaming
                if not answered:
                    answered = True
                    await self._flush_reasoning()
                    await self._emit_phase("answering")
                yield self._text_chunk(buf)
                buf = ""
                yield chunk
                continue
            buf += delta
            parsed = _parse_text_tool_call(buf)
            if parsed is None:
                continue
            name, args, raw = parsed
            logger.info(f"[LLM] text-format tool call: {name} {json.dumps(args)[:160]}")
            await self._flush_reasoning()
            try:
                result = await self._text_tool_executor(name, args) if self._text_tool_executor else None
            except Exception as e:
                logger.warning(f"[LLM] text-tool {name} failed: {e!r}")
                result = f"tool error: {type(e).__name__}: {e}"
            if not result:
                result = "(no result)"
            self._tool_rounds = 0
            tool_id = _oc_ulid("call")
            context.set_messages(list(context.messages) + [
                {"role": "assistant", "content": raw, "tool_calls": [
                    {"id": tool_id, "type": "function",
                     "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]},
                {"role": "tool", "content": result[:1400], "tool_call_id": tool_id},
            ])
            if remaining <= 1:
                context.set_tools([])
                logger.info("[LLM] text-tool round cap reached — forcing text-only answer")
            retry = await self._base_completions(self, context)
            final = self._intercept_text_tool_calls(context, retry, remaining - 1)
            async for c in final:
                yield c
            return
        if buf:
            yield self._text_chunk(buf)
        await self._flush_reasoning()
        await self._emit_phase("done")

    async def _note_reasoning(self, text: str):
        """Accumulate incremental reasoning text and push it to the UI (at
        most every ~250ms or every ~900 chars) so the brain's thinking is
        visible live. First reasoning delta also flips the status to
        'reasoning' so the orb shows the thinking sweep."""
        if not text:
            return
        if not self._reasoning_started:
            self._reasoning_started = True
            if self._emit_ui_cb:
                try:
                    await self._emit_ui_cb(StatusFrame(state="reasoning"))
                except Exception:
                    logger.warning("[LLM] reasoning status emit failed", exc_info=True)
        self._reasoning_buf += text
        self._reasoning_full += text
        now = time.monotonic()
        if self._reasoning_last < 0 or now - self._reasoning_last >= 0.25 or len(self._reasoning_buf) >= 900:
            self._reasoning_last = now
            chunk_text = self._reasoning_buf
            self._reasoning_buf = ""
            if self._emit_ui_cb:
                try:
                    await self._emit_ui_cb(BrainActivityFrame(phase="reasoning", text=chunk_text))
                except Exception:
                    logger.warning("[LLM] reasoning frame emit failed", exc_info=True)

    async def _flush_reasoning(self):
        """Push any remaining reasoning buffer (called when the answer begins
        or the stream ends, so the last thinking is not lost)."""
        if not self._reasoning_buf:
            return
        text = self._reasoning_buf
        self._reasoning_buf = ""
        if self._emit_ui_cb:
            try:
                await self._emit_ui_cb(BrainActivityFrame(phase="reasoning", text=text))
            except Exception:
                logger.warning("[LLM] reasoning flush emit failed", exc_info=True)

    def _attach_pending_reasoning(self, context):
        """Move the reasoning captured for the turn that just streamed onto the
        assistant message it belongs to (the last assistant message that has no
        reasoning_content yet), then clear it for the next turn.

        DeepSeek thinking mode (docs: "Thinking Mode" -> "Tool Calls"): with the
        ``tools`` parameter present every prior assistant turn's
        ``reasoning_content`` must be passed back or the API returns 400. The
        stream capture path (``_note_reasoning``) is where that text lives."""
        reasoning = self._reasoning_full
        self._reasoning_full = ""
        if not reasoning:
            return
        for m in reversed(list(context.messages)):
            if not isinstance(m, dict):
                continue
            if m.get("role") != "assistant":
                continue
            if "reasoning_content" not in m:
                m["reasoning_content"] = reasoning
            break

    @staticmethod
    def _ensure_reasoning_content(messages) -> int:
        """Guarantee every assistant message carries ``reasoning_content``.

        Required by DeepSeek thinking mode whenever the request carries the
        ``tools`` parameter -- even for turns with no tool call (docs: "Thinking
        Mode" -> "Tool Calls"). History we never captured (a transcript resumed
        from the session store, or a turn whose reasoning was empty) gets an
        empty string, which the API accepts. Returns how many were filled."""
        filled = 0
        for m in messages:
            if (isinstance(m, dict) and m.get("role") == "assistant"
                    and "reasoning_content" not in m):
                m["reasoning_content"] = ""
                filled += 1
        return filled

    async def _emit_phase(self, phase: str, tool: str = "", detail: str = ""):
        """Push a brain-activity phase change (answer began / tool / done)."""
        if not self._emit_ui_cb:
            return
        try:
            await self._emit_ui_cb(
                BrainActivityFrame(phase=phase, tool=tool, detail=detail)
            )
        except Exception:
            logger.warning(f"[LLM] activity phase emit failed ({phase})", exc_info=True)

    async def _wrap_idle(self, stream, idle_timeout: float):
        """Idle watchdog (the only real timeout gate on the stream).

        Yields every chunk received — content, reasoning, tool deltas — and
        resets the clock each time. If NOTHING at all has arrived for
        idle_timeout, the brain is dead (not thinking): mark the active
        provider cooled-down and abort so rotation can take over next turn.
        A brain actively streaming reasoning is never cut ("if it's thinking,
        we can't time out" — user rule).
        """
        last_kind = "init"
        while True:
            try:
                chunk = await asyncio.wait_for(anext(stream), timeout=idle_timeout)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError:
                prov = self._active_provider
                if prov is not None:
                    prov.cooldown_until = time.monotonic() + self._fallback_cooldown
                if self._emit_ui_cb:
                    try:
                        await self._emit_ui_cb(StatusFrame(state="ready"))
                    except Exception:
                        pass
                logger.warning(
                    f"[LLM] {last_kind} for {idle_timeout:.0f}s (no chunks at all) — "
                    "aborting stream"
                )
                raise
            last_kind = "streaming" if self._delta_text(chunk) or self._delta_reasoning(chunk) else "meta"
            yield chunk

    MAX_CHARS = int(os.environ.get("LLM_CONTEXT_MAX_CHARS", "220000"))
    # Number of most-recent assistant tool-call groups whose tool results are
    # replayed verbatim. Older tool output is compacted to a short note so a
    # stale investigation cannot dominate the voice context (see _trim_context).
    TOOL_RESULT_KEEP = 3

    def _compact_old_tool_results(self, messages):
        """Compact tool results that a later user turn has superseded.

        A new user message is a turn boundary: every ``tool`` message before
        the LAST user message belongs to an earlier turn whose work the user has
        moved on from, so its content is replaced by a short note. Tool messages
        at/after the last user message are the current work-in-progress and stay
        verbatim. When there is no user message to anchor on, fall back to
        keeping the newest ``TOOL_RESULT_KEEP`` tool-call groups.

        The message dict, its ``role == "tool"`` and its ``tool_call_id`` are
        left exactly as-is, so the assistant ``tool_calls`` / ``tool`` pairing
        stays valid for the provider. ``reasoning_content``, ``system`` and
        ``user`` messages are never touched, and no message is ever dropped."""
        if not messages:
            return messages
        id_to_name: dict = {}
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "assistant":
                for tc in (m.get("tool_calls") or []):
                    tid = (tc or {}).get("id") if isinstance(tc, dict) else None
                    if tid:
                        id_to_name[tid] = ((tc.get("function") or {}).get("name")
                                           or "tool")
        last_user = -1
        for i, m in enumerate(messages):
            if isinstance(m, dict) and m.get("role") == "user":
                last_user = i
        if last_user >= 0:
            # Turn-boundary rule: anything before the current turn is stale.
            old_tool_idx = {
                i for i, m in enumerate(messages)
                if i < last_user and isinstance(m, dict)
                and m.get("role") == "tool"
            }
        else:
            # Fallback: no user turn to anchor on. Keep the newest N groups.
            groups = []
            for i, m in enumerate(messages):
                if not (isinstance(m, dict) and m.get("role") == "assistant"):
                    continue
                ids = {tc.get("id") for tc in (m.get("tool_calls") or [])
                       if isinstance(tc, dict) and tc.get("id")}
                if not ids:
                    continue
                tool_idx = []
                j = i + 1
                while j < len(messages):
                    nxt = messages[j]
                    if (isinstance(nxt, dict) and nxt.get("role") == "tool"
                            and nxt.get("tool_call_id") in ids):
                        tool_idx.append(j)
                        j += 1
                    else:
                        break
                groups.append(tool_idx)
            keep_groups = max(0, int(self.TOOL_RESULT_KEEP))
            old_tool_idx = set()
            for tool_idx in groups[: max(0, len(groups) - keep_groups)]:
                old_tool_idx.update(tool_idx)
        for idx in old_tool_idx:
            m = messages[idx]
            c = m.get("content")
            if not isinstance(c, str) or not c:
                continue
            name = id_to_name.get(m.get("tool_call_id"), "tool")
            note = f"[earlier tool output for {name}, {len(c)} chars, omitted]"
            if len(note) < len(c):
                m["content"] = note
        return messages

    def _trim_context(self, context: LLMContext):
        messages = list(context.messages)
        if not messages:
            return

        def role(m):
            return m.get("role") if isinstance(m, dict) else getattr(m, "role", "")

        def content(m):
            return (m.get("content") or "") if isinstance(m, dict) else (getattr(m, "content", "") or "")

        system = [m for m in messages if role(m) == "system"]
        rest = [m for m in messages if role(m) != "system"]
        # Pin the first user message (the task) so it is never compacted away —
        # the model must always remember what it was asked to do.
        head: list = []
        body = rest
        if rest and role(rest[0]) == "user":
            head = [rest[0]]
            body = rest[1:]
        keep: list = []
        consumed = 0
        for m in reversed(body):
            c = len(content(m))
            if consumed + c > self.MAX_CHARS and keep:
                break
            consumed += c
            keep.append(m)
        keep.reverse()
        # Compact stale tool output in place (contents only) before the digest
        # is built. The list length is unchanged, so the dropped-slice below
        # stays correct; pairing, ordering and reasoning_content are preserved.
        keep = self._compact_old_tool_results(keep)
        # Compaction: keep the gist of dropped turns as one compact system note
        # (assistant decisions + tool trail), instead of silently losing them.
        dropped = body[: len(body) - len(keep)]
        digest = self._summarize_dropped(dropped)
        prefix = list(system[:1]) + head
        # Keep exactly the newest live work note so the brain always has it.
        # It sits after the (pinned) system prompt + first user turn, so the
        # prompt-cache prefix is unchanged; only the newest note survives.
        notes = [m for m in system[1:]
                 if str(content(m)).startswith(_WORK_NOTE_PREFIX)]
        if notes:
            prefix.append(notes[-1])
        if digest:
            prefix.append({"role": "system", "content": digest})
        context.set_messages(prefix + keep)

    @staticmethod
    def _summarize_dropped(msgs) -> str:
        """Structured digest of dropped turns: decisions + tool trail, deduped,
        never re-digesting an earlier digest. Deterministic (no model call on the
        latency hot path — speed law)."""
        if not msgs:
            return ""
        marker = "[Earlier conversation, compacted"
        lines = [marker + " \u2014 keep this in mind]"]
        seen: set[str] = set()
        for m in msgs[-80:]:
            role = m.get("role") if isinstance(m, dict) else getattr(m, "role", "")
            c = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
            tcs = (m.get("tool_calls") if isinstance(m, dict) else None) or []
            if not isinstance(c, str) or not c.strip():
                if role == "assistant" and tcs:
                    names = ",".join((tc.get("function") or {}).get("name", "?")
                                     for tc in tcs)
                    line = f"- called: {names}"
                    if line not in seen:
                        seen.add(line)
                        lines.append(line)
                continue
            text = " ".join(c.split())
            if text.startswith(marker):
                continue
            if text.startswith(("You are ", "Introduce yourself", "The family in this app")):
                continue
            line = f"- {role}: {text[:400]}"
            if line in seen:
                continue
            seen.add(line)
            lines.append(line)
        return "\n".join(lines)[:12000] if len(lines) > 1 else ""

    def _estimate_tokens(self, context: LLMContext) -> int:
        """Rough outgoing-token estimate (chars/4 + completion reserve) for
        the Groq tokens/min pre-emption budget."""
        total_chars = 0
        for m in context.messages:
            c = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
            if isinstance(c, str):
                total_chars += len(c)
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        total_chars += len(part["text"])
                    elif isinstance(part, dict) and "image_url" in part:
                        # Base64 length is NOT the token cost (vision tokens
                        # are per-image); count a fixed estimate instead so a
                        # ~1MB data URL doesn't fake-trip the TPM pre-empt.
                        total_chars += self._IMAGE_TOKEN_ESTIMATE_CHARS
        system = self._settings.system_instruction or ""
        total_chars += len(system) if isinstance(system, str) else 0
        reserve = (
            self._settings.max_tokens
            if isinstance(self._settings.max_tokens, int)
            else 128
        )
        return total_chars // 4 + reserve

    # ---- Vision-turn support (chat attachments; voice hot path untouched) ----

    _IMAGE_PLACEHOLDER = "[earlier attached image — already seen]"
    _IMAGE_TOKEN_ESTIMATE_CHARS = 8000  # ~2k tokens/img; base64 length is NOT the token cost

    @staticmethod
    def _content_parts(m):
        c = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        return c if isinstance(c, list) else []

    def _context_has_image(self, context) -> bool:
        for m in context.messages:
            for part in self._content_parts(m):
                if isinstance(part, dict) and "image_url" in part:
                    return True
        return False

    def _strip_old_images(self, context):
        """Keep base64 out of history: only the latest image-carrying user
        message keeps its image parts; older ones collapse to a placeholder
        so follow-up turns don't re-send megabytes (quota + context window).
        Multi-turn on the CURRENT image still works."""
        messages = list(context.messages)
        latest_idx = -1
        for i, m in enumerate(messages):
            role = m.get("role") if isinstance(m, dict) else getattr(m, "role", "")
            if role != "user":
                continue
            for part in self._content_parts(m):
                if isinstance(part, dict) and "image_url" in part:
                    latest_idx = i
                    break
        if latest_idx < 0:
            return
        stripped = 0
        for i, m in enumerate(messages):
            if i == latest_idx:
                continue
            c = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
            if not isinstance(c, list):
                continue
            if not any(isinstance(p, dict) and "image_url" in p for p in c):
                continue
            new_content = [
                {"type": "text", "text": self._IMAGE_PLACEHOLDER}
                if (isinstance(p, dict) and "image_url" in p) else p
                for p in c
            ]
            if isinstance(m, dict):
                m["content"] = new_content
            else:
                try:
                    m.content = new_content
                except Exception:
                    pass
            stripped += 1
        if stripped:
            logger.info(f"[LLM] stripped image parts from {stripped} older message(s)")

    # ── Per-turn classification for the conversational thinking shortcut ─────

    @staticmethod
    def _last_user_text(context) -> str:
        """The text of the latest user message ('' when none). Content parts
        are joined; an image-only turn yields its text parts only."""
        for m in reversed(list(context.messages)):
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return " ".join(
                    p.get("text", "") for p in c
                    if isinstance(p, dict) and isinstance(p.get("text"), str)
                )
            return ""
        return ""

    @staticmethod
    def _tools_used_this_turn(context) -> bool:
        """True when the current user turn already produced a tool call or tool
        result — such a turn is work and must never take the shortcut."""
        msgs = list(context.messages)
        last_user = -1
        for i, m in enumerate(msgs):
            if isinstance(m, dict) and m.get("role") == "user":
                last_user = i
        for m in msgs[last_user + 1:]:
            if not isinstance(m, dict):
                continue
            if m.get("role") == "tool" or (
                    m.get("role") == "assistant" and m.get("tool_calls")):
                return True
        return False

    def classify_turn(self, context) -> tuple[str, str]:
        """Turn class + the signal that decided it. The text rules live in
        ``classify_turn_text`` (one named place); this adds the turn-level
        guards: an attached image or a turn that already used a tool is work."""
        if self._context_has_image(context):
            return TURN_CLASS_WORK, "image-attached"
        if self._turn_tools > 0 or self._tools_used_this_turn(context):
            return TURN_CLASS_WORK, "tool-used-this-turn"
        return classify_turn_text(self._last_user_text(context))

    def _selected_provider(self, for_turn: str):
        """The single provider for the user's chosen model — no fallback /
        rotation (user decision: no backup to go models; go is picked on
        purpose, never used as a silent backup).

        for_turn: "text" | "tool" | "vision" (only used for logging).
        Returns None when the selected model's provider is in cooldown."""
        now = time.monotonic()
        model = self._selected_model
        prov = self._providers.get(model) if model else None
        if prov is not None and now >= prov.cooldown_until:
            return prov
        # Opt-in fallback across FREE models only (default off — respects the
        # no-silent-fallback decision; never falls back to go/paid).
        if self._fallback_enabled:
            for mid in sorted(self._free_models):
                if mid == model:
                    continue
                p = self._providers.get(mid)
                if p is not None and now >= p.cooldown_until:
                    logger.warning(f"[LLM] {for_turn}: {model!r} unavailable → "
                                   f"fallback to free model {mid!r} (LLM_FALLBACK=1)")
                    return p
        logger.warning(
            f"[LLM] {for_turn} turn but selected model {model!r} "
            "unavailable (no fallback by decision)"
        )
        return None

    def _tool_chain(self):
        prov = self._selected_provider("tool")
        return [prov] if prov else []

    def _vision_chain(self):
        prov = self._selected_provider("vision")
        return [prov] if prov else []

    def _use_provider(self, prov: _CloudProvider):
        """Point this service at a provider for one attempt. Auth comes from
        the provider's own client; only client+model change — settings
        (system instruction, temperature, max_tokens) are shared. The validated
        opencode request shape (KB-06 follow-on) lives in each client's
        default_headers, minted at construction."""
        self._client = prov.client
        self._active_provider = prov
        self._settings.model = prov.model

    def set_brain_model(self, model: str | None):
        """Pin the runtime brain model to the hardwired constant (2026-09-17).

        The argument is accepted-and-ignored so older clients that still send
        brain_model_set keep working: the model always ends up BRAIN_MODEL_ID.
        Clears that provider's cooldown so the fixed choice is actually tried.
        When the provider is missing (e.g. no API key) _selected_model still
        names the fixed model, so the error path says plainly that it is
        unavailable instead of quietly running something else."""
        self._selected_model = BRAIN_MODEL_ID
        prov = self._providers.get(BRAIN_MODEL_ID)
        if prov is not None:
            prov.cooldown_until = 0.0
            prov.probing = False

    def _build_chain(self) -> list:
        """Single-provider chain: only the user's chosen opencode model, no
        fallback/rotation."""
        prov = self._selected_provider("text")
        return [prov] if prov else []

    def _classify_error(self, exception):
        """Provider/gateway failures are transient from the pipeline's point of view.
        The LLM service itself is healthy; BoundedContextLLM's own per-provider
        cooldown decides when to retry. Never let a failed turn mark this service
        unusable (that would tear the 24/7 pipeline down)."""
        return ErrorCategory.APPLICATION

    @staticmethod
    def _sanitize_tool_messages(messages):
        """Return a structurally valid OpenAI message list.

        Every `tool` message must carry a non-empty `tool_call_id` that matches a
        preceding assistant `tool_calls` id; no dangling/duplicated ids; no empty
        tool content; no assistant `tool_calls: []`. Invalid `tool` messages are
        dropped. An assistant `tool_calls` id that no `tool` message answers gets
        a minimal synthesized `tool` result so the pair is never left broken (the
        shape that produced a 400); anything else invalid is dropped (logged once
        by the caller)."""
        out = []
        pending = []
        answered = set()
        dropped = 0

        def _complete_pending():
            for tid in pending:
                if tid in answered:
                    continue
                out.append({
                    "role": "tool",
                    "tool_call_id": tid,
                    "content": _MISSING_TOOL_RESULT,
                })
            pending.clear()
            answered.clear()

        for m in messages:
            if not isinstance(m, dict):
                out.append(m)
                continue
            role = m.get("role")
            if role == "assistant":
                _complete_pending()
                tcs = m.get("tool_calls")
                if not isinstance(tcs, list) or not tcs:
                    m.pop("tool_calls", None)
                else:
                    ids, seen = [], set()
                    for tc in tcs:
                        tid = (tc or {}).get("id")
                        if tid and tid not in seen:
                            seen.add(tid)
                            ids.append(tc)
                    if not ids:
                        m.pop("tool_calls", None)
                    else:
                        m["tool_calls"] = ids
                        pending[:] = [tc["id"] for tc in ids]
                out.append(m)
            elif role == "tool":
                tid = m.get("tool_call_id")
                content = m.get("content")
                if tid:
                    # The id was answered by *some* tool message, even if that
                    # message is itself invalid and dropped below.
                    answered.add(tid)
                if not tid or tid not in pending or (isinstance(content, str) and not content.strip()):
                    dropped += 1
                    continue
                pending.remove(tid)
                out.append(m)
            else:
                _complete_pending()
                out.append(m)
        _complete_pending()
        return out, dropped

    async def get_chat_completions(self, context):
        # Reasoning captured during the previous turn belongs to the assistant
        # message pipecat appended right after it; move it across before this
        # request (thinking-mode gateways 400 if it is dropped).
        self._attach_pending_reasoning(context)
        # Per-turn reasoning-surfacing state (reset before any provider work).
        self._reasoning_started = False
        self._reasoning_buf = ""
        self._reasoning_last = -1.0
        self._active_provider = None
        self._strip_old_images(context)
        self._trim_context(context)
        _original = list(context.messages)
        _messages, _dropped = self._sanitize_tool_messages(_original)
        if _dropped:
            logger.warning(f"[LLM] dropped {_dropped} dangling tool message(s) before dispatch")
        if _messages != _original:
            context.set_messages(_messages)
        # Per-turn class (one log line: the class + the signal that decided it).
        # Conversational turns go out with thinking disabled; work turns are
        # byte-identical to before (the model decides).
        turn_class, turn_signal = self.classify_turn(context)
        thinking_off = (
            turn_class == TURN_CLASS_CONVERSATIONAL
            and self._conversational_shortcut
        )
        logger.info(
            f"[LLM] turn={turn_class} thinking="
            f"{'off' if thinking_off else 'model-default'} ({turn_signal})"
        )
        has_tools = context.tools is not None and not (
            hasattr(context.tools, '__len__') and len(context.tools) == 0
        )
        if has_tools:
            self._tool_rounds += 1
            if self._tool_rounds > _MAX_TOOL_ROUNDS:
                context.set_tools([])
                has_tools = False
                self._tool_rounds = 0
                logger.warning("[LLM] tool round cap reached — forcing text-only")
        else:
            self._tool_rounds = 0
        if has_tools:
            _filled = self._ensure_reasoning_content(context.messages)
            if _filled:
                logger.info(
                    f"[LLM] added reasoning_content to {_filled} assistant "
                    "message(s) for DeepSeek thinking mode"
                )
        if has_tools:
            chain = self._tool_chain()
            if not chain:
                await self._emit_brain_error(None)
                raise RuntimeError("[LLM] tool-calling turn needs providers; unavailable")
            logger.info("[LLM] tool-calling turn (no fallback)")
        elif self._context_has_image(context):
            chain = self._vision_chain()
            if not chain:
                await self._emit_brain_error(None)
                raise RuntimeError("[LLM] image turn needs vision providers; unavailable")
            logger.info("[LLM] vision turn (no fallback)")
        else:
            chain = self._build_chain()
        last_err: Exception | None = None
        for i, prov in enumerate(chain):
            try:
                self._use_provider(prov)
                if thinking_off:
                    self._settings.extra = dict(_REASONING_OFF_EXTRA)
                try:
                    stream = await super().get_chat_completions(context)
                except Exception:
                    # Safety net 2: never fail the turn over the shortcut — fall
                    # back to the normal (thinking-enabled) request.
                    if not thinking_off:
                        raise
                    logger.info(
                        "[LLM] conversational no-thinking request failed \u2014 "
                        "falling back to the model's normal request"
                    )
                    self._settings.extra = _reasoning_extra()
                    stream = await super().get_chat_completions(context)
                    thinking_off = False
            except Exception as e:
                rate_limited = _is_rate_limit(e)
                prov.cooldown_until = (
                    time.monotonic() + self._fallback_cooldown
                )
                prov.probing = False
                if i + 1 < len(chain):
                    nxt = chain[i + 1].name
                    if rate_limited:
                        logger.info(
                            f"[LLM] {prov.name} rate-limited (429), "
                            f"rotating to {nxt}"
                        )
                    else:
                        err = f"{type(e).__name__}: {e}"
                        logger.info(
                            f"[LLM] {prov.name} failed ({err[:160]}), "
                            f"rotating to {nxt}"
                        )
                else:
                    err = f"{type(e).__name__}: {e}"
                    logger.info(
                        f"[LLM] {prov.name} failed ({err[:160]}); "
                        "all providers failed"
                    )
                last_err = e
                continue
            finally:
                # The off-extra applies to the single request only; work turns
                # (and every later call) keep the hardwired default.
                self._settings.extra = _reasoning_extra()
            if prov.probing:
                prov.probing = False
                logger.info(f"[LLM] recovered to {prov.name}")
            logger.info(f"[LLM] using {prov.name} ({prov.model})")
            if self._emit_brain_cb:
                await self._emit_brain_cb(BrainFrame(provider=prov.name, model=prov.model))
            # Safety net 1 (tool call / clarifying question) sits closest to the
            # raw stream so a doubtful no-thinking reply is re-asked before the
            # tool interceptors see it.
            if thinking_off:
                stream = self._conversational_reask(context, stream)
            if has_tools and self._text_tool_executor is not None:
                stream = self._intercept_text_tool_calls(context, stream)
            if has_tools:
                stream = self._maybe_continue(context, stream, has_tools)
            return self._wrap_idle(stream, self._idle_timeout)
        await self._emit_brain_error(last_err)
        raise last_err if last_err is not None else RuntimeError(
            "[LLM] no provider available"
        )

    async def _emit_brain_error(self, err: Exception | None, model: str | None = None):
        """Tell the UI why a turn failed (rate limit, bad key, ...) so the user
        sees it instead of silent dead-air."""
        if not self._emit_ui_cb:
            return
        mid = self._selected_model or model or BRAIN_MODEL_ID
        if err is None:
            if self._selected_model:
                # Provider exists but is cooling down (recently rate-limited).
                kind = "rate_limit"
                msg = "Asha runs on DeepSeek V4.1 Flash, which is busy right now \u2014 try again in a moment."
            else:
                kind = "unavailable"
                msg = "Asha runs on DeepSeek V4.1 Flash, which isn't reachable right now \u2014 check the OpenCode key and try again."
        elif _is_rate_limit(err):
            kind = "rate_limit"
            msg = "DeepSeek V4.1 Flash is rate-limited right now \u2014 try again in a moment."
        else:
            code = getattr(err, "status_code", None) or getattr(err, "code", None)
            if code in (401, 403):
                kind = "auth"
                msg = "That model rejected your key \u2014 check your OpenCode key and try again."
            else:
                kind = "error"
                msg = "Asha couldn't reach the model. Try again in a moment."
        try:
            await self._emit_ui_cb(BrainErrorFrame(kind=kind, message=msg, model=mid))
        except Exception:
            pass


def set_active_system(llm, text: str) -> bool:
    """Replace the LLM system instruction with an EXACT single-message string.

    Uses pipecat's non-compounding compose path (base + appended joined into ONE
    system message; llama.cpp Qwen3.5 template rejects a second system message).
    ``text`` is the complete system prompt (identity/base + memory blocks).
    Returns True if the instruction actually changed. Byte-stable strings keep
    the prefix cache warm; if unchanged we touch nothing.
    """
    parts = [text] if text else []
    composed = "\n\n".join(p for p in parts if p)
    if not composed or composed == llm._settings.system_instruction:
        return False
    llm._base_system_instruction = None
    llm._appended_system_instructions = [text] if text else []
    llm._compose_system_instruction()
    return True


IDENTITY_ANCHOR = "You are {name}. Never say you are someone else."


# Rule 11, shared verbatim by the brain contract and the coding agent's
# doctrine: never change an unrelated thing as a side effect of another task.
_SCOPE_RULE = (
    "Never change anything unrelated to what you were asked to do: if you "
    "notice something odd elsewhere, report it to the user and leave it alone "
    "\u2014 working behaviour is not yours to 'improve' in passing.\n"
)

# The after-report duty, shared verbatim by the brain contract and the coding
# agent's doctrine (see the sync below _BRAIN_CONTRACT).
_VERIFY_AFTER_REPORT = (
    "When an agent you delegated to finishes, check its work yourself straight "
    "away and keep checking until the user is back \u2014 never wait to be "
    "asked, and never sit idle while a report is unverified.\n"
) + _SCOPE_RULE

# The brain's operating contract: the tools exist, but the model needs to know
# WHEN to use which. Injected into Jarvis's system prompt at compose time. Kept
# decision-first ("if X, do Y") — this is what turns a chat model into a brain.
_BRAIN_CONTRACT = (
    "HOW YOU WORK (you are an agent, not a chatbot):\n"
    "You are Asha. You talk AND you do the work yourself with your tools; "
    "there are no workers to delegate to.\n"
    "STYLE SCOPE: the 'speak in one or two short sentences, no markdown or "
    "lists' rule applies ONLY to what you SAY out loud. It does NOT apply to "
    "files you write \u2014 write complete, well-structured Markdown/code in files.\n"
    "WORK PROTOCOL \u2014 when asked to build, fix, research, write or produce:\n"
    "1. FIND what you need; do not read the whole repo. Use repo_map / glob / "
    "grep to locate, then read_file ONLY the relevant files. Never re-read a "
    "file you already read.\n"
    "2. RESEARCH briefly: for outside information use web_search (2-5 queries), "
    "then stop.\n"
    "3. PRODUCE the deliverable with write_file / edit_file / apply_patch \u2014 "
    "the FULL content, at the exact path the user named.\n"
    "4. VERIFY with run_bash (and diagnostics after editing Python).\n"
    "5. Only then reply in one or two short spoken sentences summarizing it.\n"
    "VERIFY WITH YOUR EYES when the change is one the user will see: after "
    "anything that affects the UI, a layout, a window, or how the voice behaves, "
    "look at it with read_screen and confirm the change actually landed before "
    "you say it is done \u2014 if you cannot see it, say so plainly instead of "
    "claiming success.\n"
    "Keep tool use EFFICIENT \u2014 a handful of targeted calls, not dozens. Never "
    "stop mid-task to ask 'what would you like?', and do not ask permission to "
    "do the obvious next step \u2014 just do it.\n"
    "- Plain conversation or a quick question: answer, no tools.\n"
    "- switch_project changes project; search_sessions recalls earlier chat; "
    "repo_map shows layout; mcp_list_tools then mcp_call_tool for outside tools.\n"
    "- diagnostics after Python edits; lsp for symbols/definitions.\n"
    "- You have eyes: read_screen looks at what is on the user's screen right "
    "now (on-device OCR, plus a vision model when you pass a question); "
    "look_at_image does the same for an image file; look_through_camera looks "
    "through the webcam at whatever the user holds up. Use them whenever the "
    "answer is on screen (a design, a chart, an error, an app) instead of "
    "asking the user to describe it.\n"
    "- plan_mode(true) for research-only; tools lists your tools; skill_save "
    "saves a recipe; cron schedules recurring work.\n"
    "- Never claim work is done unless a tool result proves it.\n"
    "Never assert a cause, a fix, or that something does not exist as fact "
    "without checking it first: cite the evidence (file:line, a log line, a test, "
    "a doc page), or say plainly that it is an unverified hypothesis and what "
    "would confirm it \u2014 and when the user challenges you, re-check instead of "
    "defending.\n"
    "YOU DECIDE, the user does not instruct you tool by tool. Every tool below is "
    "yours to use at your own judgement; never wait to be told, and never make the "
    "user name a tool or know that a capability exists.\n"
    "Before you answer anything, ask: could a tool answer this better, faster or "
    "more surely than my memory, a guess, or a question to the user? If yes, use "
    "the tool NOW, then answer.\n"
    "Read the situation, not the wording: if they say 'this', 'here', 'that "
    "error', 'my screen', 'this design' and the answer is on screen — read_screen. "
    "If they refer to an image/screenshot on disk — look_at_image. If they hold "
    "something up to the camera — look_through_camera. If they want a "
    "picture made — generate_image. If they ask about the outside world — "
    "web_search/web_fetch. If they ask about their own code or files — repo_map / "
    "grep / read_file. Getting this wrong is a failure; using a tool you did not "
    "need is not.\n"
    "Only ask the user something when the goal itself is ambiguous, or before an "
    "action you cannot undo. 'Which of these two do you mean?' is fine; 'should I "
    "look at your screen?' is not.\n"
    "DECIDE, DO NOT ASK — and remember WHO you are asking. The user can be "
    "anyone: an engineer, a salesperson, a designer, a student, or someone who "
    "has never opened a terminal. Before you ask them anything, first ask "
    "yourself: can I settle this on my own, safely, without breaking something "
    "that already works? If yes, settle it, do it, and just tell them what you "
    "did and why \u2014 asking a question you could have answered yourself wastes "
    "their time, and a cautious question is not a virtue. Only bring it to them "
    "when the decision is genuinely theirs: what they want (their taste, "
    "preference, or the goal itself), their money, something that cannot be "
    "undone, or a change to something already working. And when you do ask, ask "
    "in THEIR language, about the outcome they want \u2014 never in technical terms. "
    "A technical question ('should the model be X or Y?', 'which file?', 'is that "
    "path right?') gets a shrug or a guess from most people, not an answer; that "
    "choice is YOURS to make. If they cannot judge it, do not make them.\n"
    "Delegating is normal: when work is long, self-contained or parallel, hand "
    "it to an agent with a proper brief and keep talking — never make the user "
    "ask for that.\n"
    "Keep the user's projects tracked on the board: when they mention work to "
    "do, add it to the right project; when they ask how things are going, read "
    "the board. Keep it current without being asked.\n"
    "Do not volunteer status updates, summaries or next steps about work the user has not asked about in this turn. If something is genuinely blocking, say it once, briefly, and move on. When the user says something social (\"I'm good\", \"nothing right now\"), answer that \u2014 do not turn it into a work report.\n"
    "Agents are your team. Any of them can do any task - the title only tells "
    "you who fits best, and it never excuses anyone from work. Hire an agent "
    "only when the user asks for one: propose the name, title and "
    "responsibilities, then wait for their yes.\n"
) + _VERIFY_AFTER_REPORT + agent_loop.THIRD_PARTY_RULE

# The same after-report and scope duties are appended to the coding agent's
# doctrine here, once, as a safety net. Keeping the two prompts in sync is the
# point: the brain and its muscles both know that a finished report is not a
# finished job, and that an unrelated oddity is reported, never "fixed" in
# passing. Sync on the newest rule (_SCOPE_RULE) so neither line duplicates.
if _SCOPE_RULE.strip() not in agent_loop.SYSTEM:
    agent_loop.SYSTEM = agent_loop.SYSTEM + _VERIFY_AFTER_REPORT


def build_system_text(base: str, memory_suffix: str) -> str:
    """Full system prompt = identity base + brain operating contract + skills
    index + frozen memory blocks + identity anchor. Jarvis is the only identity
    this app has, so the contract and anchor are always included."""
    anchor = IDENTITY_ANCHOR.format(name=ASSISTANT_NAME)
    parts = [p for p in (base, _BRAIN_CONTRACT, _SKILLS.index(), memory_suffix, anchor) if p]
    return "\n\n".join(parts)


class StatusRelay(FrameProcessor):
    """Emits {type:'status',state:'loading'} on connect and {type:'status',state:'ready'}
    when the first TTSStoppedFrame arrives downstream of TTS (or after a 15s watchdog)."""

    def __init__(self):
        super().__init__()
        self._ready = False
        self._watchdog_task: asyncio.Task | None = None

    def start_watchdog(self):
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()

        async def _watch():
            try:
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                return
            if not self._ready:
                self._ready = True
                await self.push_frame(StatusFrame(state="ready"), FrameDirection.DOWNSTREAM)

        self._watchdog_task = asyncio.create_task(_watch())

    def cancel_watchdog(self):
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()

    async def process_frame(self, frame, direction):
        if isinstance(frame, TTSStoppedFrame) and not self._ready:
            self._ready = True
            self.cancel_watchdog()
            logger.info("[STATUS] ready emitted (greeting finished)")
            await self.push_frame(StatusFrame(state="ready"), direction)
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class BotTextEcho(FrameProcessor):
    """TTS consumes LLMTextFrames, so the assistant aggregator never sees the
    bot's text and the UI would never get reply bubbles. Re-emit a copy after
    the TTS so a bot transcription can flow to the output transport."""

    _QUOTE_MAP = str.maketrans({
        "\u201c": '"', "\u201d": '"',   # curly double quotes
        "\u2018": "'", "\u2019": "'",   # curly single quotes
        "\u2013": "-", "\u2014": "-",   # en/em dash → hyphen
    })

    @staticmethod
    def _sanitize_tts(text: str) -> str:
        """Normalize punctuation that trips the phonemizer."""
        t = text.translate(BotTextEcho._QUOTE_MAP)
        return t

    def __init__(self, session_cb=None):
        super().__init__()
        self._session_cb = session_cb
        self._bot_buf: list = []          # token chunks (LLMTextFrame)
        self._bot_sentences: list = []    # whole sentences (TTSTextFrame)

    def _flush_bot_turn(self):
        """Append the buffered response as ONE bot turn (never one row per
        streaming chunk). TTSTextFrame sentences win when present because that
        is the text the TTS actually spoke; otherwise fall back to raw chunks.
        """
        buf, sentences = self._bot_buf, self._bot_sentences
        self._bot_buf, self._bot_sentences = [], []
        if not self._session_cb:
            return
        text = (" ".join(sentences) if sentences else "".join(buf)).strip()
        if text:
            self._session_cb("bot", text)

    async def process_frame(self, frame, direction):
        from pipecat.frames.frames import (
            LLMFullResponseEndFrame,
            LLMFullResponseStartFrame,
            LLMTextFrame,
            TTSTextFrame,
        )

        if isinstance(frame, LLMTextFrame):
            await self.push_frame(frame, direction)
            sanitized = self._sanitize_tts(frame.text)
            # NOTE: LLMTextFrame takes text only (no user_id — that kwarg is
            # TranscriptionFrame's); passing it raises TypeError, which the
            # pipeline converts to an ErrorFrame and kills the bubble echo.
            await self.push_frame(LLMTextFrame(text=sanitized), direction)
            if self._session_cb and frame.text and frame.text.strip():
                self._bot_buf.append(frame.text)
            return
        if isinstance(frame, TTSTextFrame):
            # TTS consumes the LLM's LLMTextFrames and re-emits the spoken text
            # as TTSTextFrames, so THIS is the frame that carries a normal
            # reply past the TTS. Buffer it; the response-end frame flushes it
            # once (a streaming reply must not create one row per chunk).
            if self._session_cb and frame.text and frame.text.strip():
                self._bot_sentences.append(frame.text)
        elif isinstance(frame, LLMFullResponseStartFrame):
            # A prior turn still buffered (e.g. a code line spoken through the
            # TTS without its own end frame) must not merge into this response.
            self._flush_bot_turn()
        elif isinstance(frame, LLMFullResponseEndFrame):
            # Turn-complete signal (TTSService re-emits it once per response).
            self._flush_bot_turn()
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class AudioToggleProcessor(FrameProcessor):
    """Front-runner gate: turns the mic/listening on or off with the VAD.

    When disabled, VAD parameters make speech detection effectively
    impossible, so no turns can start from noise while typed chat keeps
    working.
    """

    def __init__(self, vad: SileroVADAnalyzer, default_params: VADParams, on_enabled=None):
        super().__init__()
        self._vad = vad
        self._default_params = default_params
        # Optional listener for the mic's enabled/disabled state (the
        # backchannel uses it to stay silent while the mic is muted).
        self._on_enabled = on_enabled

    async def process_frame(self, frame, direction):
        if isinstance(frame, AudioToggleFrame):
            if self._on_enabled is not None:
                try:
                    self._on_enabled(frame.enabled)
                except Exception:  # noqa: BLE001
                    logger.warning("[MIC] toggle notify failed", exc_info=True)
            if frame.enabled:
                self._vad.set_params(self._default_params)
            else:
                off = VADParams(confidence=1.0, min_volume=1.0, start_secs=60.0, stop_secs=60.0)
                self._vad.set_params(off)
            return None
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


# M2 coding-task dialog (agent-delegation.md §6A): pure classifiers so the
# stop/yes/no/dispatch routing is unit-testable without booting the pipeline.
_CODE_STOP_WORDS = frozenset({
    "stop", "cancel", "abort", "stop that", "cancel that", "never mind",
    "forget it", "forget that", "don't do that", "dont do that",
})
_CODE_YES_WORDS = frozenset({
    "yes", "yeah", "yep", "yup", "yes please", "do it", "apply it",
    "go ahead", "go for it", "sounds good", "ok do it", "okay do it",
})
_CODE_NO_WORDS = frozenset({
    "no", "nope", "no thanks", "don't", "dont", "don't apply it",
    "dont apply it", "not yet", "cancel",
})
_CODE_ALLOW_WORDS = frozenset({
    "allow", "allow once", "yes", "yeah", "yep", "go ahead",
    "always", "always allow", "allow always", "always yes",
})
_CODE_ALWAYS_WORDS = frozenset({
    "always", "always allow", "allow always", "always yes",
})
_CODE_DENY_WORDS = frozenset({
    "deny", "reject", "no", "nope", "don't allow", "dont allow",
})
_CODE_ACTIVE = ("running", "proposed", "applying", "awaiting_permission")


def _normalize_code_text(text: str) -> str:
    return (text or "").strip().lower().rstrip(".!?")


def _is_always(text: str) -> bool:
    """True when an allow answer means 'always allow' (persist a rule)."""
    return _normalize_code_text(text) in _CODE_ALWAYS_WORDS


def classify_code_text(text: str, code_status: str) -> str:
    """Route one user utterance in the coding-task dialog.

    code_status: "idle" | "running" | "proposed" | "applying" | "awaiting_permission".
    Returns "stop" | "yes" | "no" | "allow" | "deny" | "dispatch" | "busy" | "none".
    Stop wins while a task is open; yes/no only count while a proposal is
    pending; allow/deny only count while a permission ask is pending; a fresh
    coding request dispatches only from idle (otherwise "busy" — single-user,
    one task at a time).
    """
    norm = _normalize_code_text(text)
    if not norm:
        return "none"
    if code_status in _CODE_ACTIVE and norm in _CODE_STOP_WORDS:
        return "stop"
    if code_status == "idle" and norm.startswith(("orchestrate", "orchestration")):
        return "orchestrate"
    if code_status == "awaiting_permission":
        if norm in _CODE_ALLOW_WORDS:
            return "allow"
        if norm in _CODE_DENY_WORDS:
            return "deny"
    if code_status == "proposed":
        if norm in _CODE_YES_WORDS:
            return "yes"
        if norm in _CODE_NO_WORDS:
            return "no"
    if is_coding_request(text):
        return "dispatch" if code_status == "idle" else "busy"
    return "none"


def _split_store_entries(store) -> list:
    """Current entry texts of one MemoryStore (split, no "(empty)" sentinel)."""
    text = store.entries_text()
    if not text or text.strip() == "(empty)":
        return []
    return [e.strip() for e in text.split(ENTRY_DELIMITER) if e.strip()]


def build_memory_entries(manager=None) -> list:
    """Full memory list for the UI panel: [{kind, text, id}]; id = kind:index
    (proto.py MemoryFrame contract). Falls back to the module memory_manager
    so run() closures stay thin."""
    mgr = manager if manager is not None else memory_manager
    entries: list = []
    for kind, store in (("user", mgr.user_store), ("memory", mgr.memory_store)):
        for i, text in enumerate(_split_store_entries(store)):
            entries.append({"kind": kind, "text": text, "id": f"{kind}:{i}"})
    return entries


async def apply_memory_edit(manager, action: str, entry_id: str,
                            new_text: str = "") -> str:
    """Apply a UI panel edit/delete: resolve kind:index against CURRENT store
    contents, then replace(old_text, new_text) / remove(old_text).
    Returns the store's result string. Raises ValueError on bad/stale id."""
    try:
        kind, idx = (entry_id or "").split(":", 1)
        idx = int(idx)
    except (ValueError, AttributeError):
        raise ValueError(f"bad memory id {entry_id!r}")
    if kind not in ("user", "memory"):
        raise ValueError(f"bad memory kind {kind!r}")
    store = manager.user_store if kind == "user" else manager.memory_store
    current = _split_store_entries(store)
    if not (0 <= idx < len(current)):
        raise ValueError(
            f"memory entry {entry_id!r} no longer exists — refresh the panel"
        )
    old_text = current[idx]
    if action == "edit":
        if not (new_text or "").strip():
            raise ValueError("empty replacement text")
        return await store.replace(old_text, new_text.strip())
    if action == "delete":
        return await store.remove(old_text)
    raise ValueError(f"unknown memory action {action!r}")


def _speak_summary(text: str, max_chars: int = 280,
                   max_sentences: int = 2) -> str:
    """Trim a brain result to 1-2 short spoken sentences for TTS.

    Pure helper (unit-tested): collapses whitespace, strips code backticks
    (they confuse the phonemizer), keeps the first sentences, caps length
    at a word boundary. Punctuation is kept for TTS prosody.
    """
    t = " ".join((text or "").split()).replace("`", "")
    if not t:
        return ""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", t) if p.strip()]
    out = " ".join(parts[:max_sentences]) if len(parts) > 1 else t
    if len(out) > max_chars:
        cut = out[:max_chars].rsplit(" ", 1)[0] or out[:max_chars]
        out = cut
    return out


async def _code_speak(text: str, tts_holder: dict) -> bool:
    """Speak a short line through the normal TTS queue (parallel to bubbles).

    Injects an LLMTextFrame at the tts stage via queue_frame, so it is
    synthesized exactly like brain output (BotTextEcho also echoes it as a
    bubble downstream). Non-blocking: returns once queued; the voice loop,
    mic, and ashaReady state are untouched. Returns True if queued.
    """
    clean = BotTextEcho._sanitize_tts((text or "").strip())
    if not clean:
        return False
    tts = (tts_holder or {}).get("tts")
    if tts is None:
        logger.warning("[CODE] speak skipped: no TTS processor")
        return False
    logger.info(f"[CODE] speak ({len(clean)} chars): {clean[:80]}")
    await tts.queue_frame(
        LLMTextFrame(text=clean), FrameDirection.DOWNSTREAM
    )
    return True


# M2b recall-intent gate (cheap keyword check like is_coding_request).
# Read-only when matched — worst case is a no-hits log line. Deliberately
# does NOT match memory-SAVE phrasing ("remember that my dog is…").
_RECALL_PATTERNS = (
    "did we discuss",
    "did we talk about",
    "have we talked about",
    "do you remember",
    "did i tell you",
    "did i mention",
    "did you mention",
    "what do we know about",
    "what do you know about",
    "remember when",
    "what did we",
    "last week",
    "last time we",
    "recall",
)


def is_recall_request(text: str) -> bool:
    """Cheap heuristic: is this utterance asking about conversation history?"""
    if not text:
        return False
    lowered = text.lower()
    return any(p in lowered for p in _RECALL_PATTERNS)


# Scaffolding + stopwords stripped to turn a recall question into an FTS
# query. SessionStore.search matches one exact phrase, so the terms must
# stay in question order and stay contiguous-friendly ("dentist appointment",
# not "the dentist appointment is next").
_RECALL_STRIP = _RECALL_PATTERNS + (
    "talked about",
    "tell me",
    "please",
    "right",
)
_RECALL_STOPWORDS = frozenset({
    "the", "a", "an", "my", "your", "we", "you", "i", "me", "us",
    "about", "of", "to", "in", "on", "for", "is", "are", "was", "were",
    "do", "does", "did", "and", "or", "that", "this", "it", "at", "there",
    "what", "when", "where", "how", "why", "which", "who",
})


def _recall_terms(text: str, max_terms: int = 8) -> str:
    """Reduce a recall question to content terms for the session FTS index."""
    lowered = (text or "").lower()
    for p in _RECALL_STRIP:
        lowered = lowered.replace(p, " ")
    terms = [
        t for t in re.findall(r"[a-z0-9]+", lowered)
        if len(t) >= 2 and t not in _RECALL_STOPWORDS
    ]
    return " ".join(terms[:max(1, max_terms)])


def format_recall_context(hits, max_hits: int = 4, max_chars: int = 150) -> str:
    """Render recall hits as an LLM context block ("" when no usable hits)."""
    lines = []
    for h in (hits or [])[:max(1, max_hits)]:
        if not isinstance(h, dict):
            continue
        text = ((h.get("snippet") or h.get("text") or "").strip()
                .replace("\n", " "))
        if not text:
            continue
        if len(text) > max_chars:
            text = (text[:max_chars].rsplit(" ", 1)[0] or text[:max_chars]) + "…"
        role = h.get("role", "")
        who = f"{role}: " if role in ("user", "assistant") else ""
        lines.append(f"[{h.get('source', '?')}] {who}{text}")
    if not lines:
        return ""
    return ("[Session recall — past turns/notes relevant to the question below. "
            "Use these to answer; if irrelevant, ignore.]\n" + "\n".join(lines))


def log_session_turn(store, session_id, role: str, text: str) -> bool:
    """Append one turn to the session log. Never raises; False on no-op."""
    text = (text or "").strip()
    if store is None or not session_id or not text:
        return False
    try:
        store.append(session_id, role, text)
        return True
    except Exception as e:
        logger.warning(f"[SESSION] append failed: {e!r}")
        return False


async def _send_code_bubble(emit_fn, log_fn, text: str, *,
                            persist: bool = False) -> None:
    """Emit one coding bubble to the UI, optionally storing it as a bot turn.

    The UI emit is byte-identical either way (same frame, text, order);
    ``persist=True`` additionally records the text via ``log_fn`` (the
    ``log_turn`` seam) so the turn survives a session reopen. Progress
    chatter keeps the default ``persist=False`` (UI-only); error text and
    other human-meaningful outcomes pass ``persist=True``. The store write
    never raises past this point and never blocks the bubble.
    """
    try:
        # NOTE: LLMTextFrame takes text only (no user_id kwarg in this
        # pipecat version) — keep it constructible or no bubble shows.
        await emit_fn(LLMTextFrame(text=text))
    except Exception as e:
        logger.warning(f"[CODE] bubble failed: {e!r}")
    if persist:
        try:
            log_fn("bot", text)
        except Exception as e:
            logger.warning(f"[SESSION] code bubble log failed: {e!r}")


async def _send_code_result(emit_fn, log_fn, text: str) -> None:
    """Emit one coding-task outcome bubble and store it as a bot turn.

    Results are always human-meaningful (the final answer, a stop/drop
    outcome, a block notice), so they are always persisted — exactly one
    row per call; callers pass the final text, never streaming chunks.
    """
    try:
        await emit_fn(CodingResultFrame(text=text))
        logger.info(f"[CODE] result bubble ({len(text)} chars)")
    except Exception as e:
        logger.warning(f"[CODE] result bubble failed: {e!r}")
    try:
        log_fn("bot", text)
    except Exception as e:
        logger.warning(f"[SESSION] code result log failed: {e!r}")


async def maybe_inject_recall(context, recaller, text: str) -> int:
    """If text is a recall question, run recall and inject hits as a user
    message AHEAD of the live turn so the brain answers from history.
    Returns hits injected (0 = no-op). Failures log-and-continue (voice first).
    """
    if not is_recall_request(text):
        return 0
    terms = _recall_terms(text)
    if not terms:
        logger.info("[RECALL] intent detected, no search terms — nothing injected")
        return 0
    try:
        hits = recaller.recall(terms, limit=4)
    except Exception as e:
        logger.warning(f"[RECALL] recall failed: {e!r}")
        return 0
    block = format_recall_context(hits)
    if not block:
        logger.info(f"[RECALL] no hits for {terms!r} — nothing injected")
        return 0
    context.add_message({"role": "user", "content": block})
    logger.info(f"[RECALL] injected {len(hits)} hits for {terms!r}")
    return len(hits)


# Marker on the live work note so the context trimmer can deliberately keep
# exactly the newest one (and never the system prompt prefix).
_WORK_NOTE_PREFIX = "[work now]"


def maybe_inject_work_note(context) -> int:
    """Append ONE system note describing what the agents are doing right now.

    Called ahead of the user's turn. Read-only, in-memory, no I/O; returns 1
    when a note was added, else 0. The note is a *turn-local* system message
    (never the system prompt prefix), so the prompt cache prefix is untouched.
    Any failure is skipped silently — the voice path is sacred.
    """
    try:
        from tasks import tasks as _tasks

        line = _tasks.summary_line()
    except Exception:  # noqa: BLE001 - never break the voice path
        return 0
    if not line:
        return 0
    try:
        context.add_message({"role": "system",
                             "content": f"{_WORK_NOTE_PREFIX} {line}"})
        logger.info(f"[WORK] live note injected: {line}")
        return 1
    except Exception:  # noqa: BLE001
        return 0


class WorkNoteInjector(FrameProcessor):
    """Voice-path live-work gate (sits before the user aggregator): when agents
    have live work, append one short status note to the turn's own context so
    the brain always knows what they are doing — no tool call needed.

    Read-only, in-memory (no filesystem), never blocks and never touches audio,
    VAD or TTS. A failure is skipped silently."""

    def __init__(self, context):
        super().__init__()
        self._context = context

    async def process_frame(self, frame, direction):
        from pipecat.frames.frames import TranscriptionFrame

        if isinstance(frame, TranscriptionFrame) and frame.user_id in ("", "user"):
            maybe_inject_work_note(self._context)
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class RecallInjector(FrameProcessor):
    """Voice-path recall gate (sits before the user aggregator): on a recall
    question, inject history hits into context ahead of the live turn.
    Read-only, local sqlite (ms-scale); never blocks or breaks voice."""

    def __init__(self, context, recaller):
        super().__init__()
        self._context = context
        self._recaller = recaller

    async def process_frame(self, frame, direction):
        from pipecat.frames.frames import TranscriptionFrame

        if isinstance(frame, TranscriptionFrame) and frame.user_id in ("", "user"):
            await maybe_inject_recall(self._context, self._recaller,
                                      frame.text or "")
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class TurnRouter(FrameProcessor):
    """Carries one user turn through the non-LLM side paths.

    Jarvis is the only voice: there is no name detection, no roster and no
    voice switching. This processor echoes the user's transcript to the UI,
    forwards the turn to the coding dialog, and logs it to the session store.
    """

    def __init__(self, output_frame_cb=None, code_cb=None, session_cb=None):
        super().__init__()
        self._output_frame_cb = output_frame_cb
        self._code_cb = code_cb
        self._session_cb = session_cb
        self._work_mode = False

    def set_work_mode(self, enabled: bool):
        """Toggle work-mode dictation. When ON, transcripts echo to UI but
        skip the coding dialog, session log, and LLM."""
        self._work_mode = enabled
        logger.info(f"[WORK] work_mode = {enabled}")

    async def process_frame(self, frame, direction):
        from pipecat.frames.frames import TranscriptionFrame

        if isinstance(frame, TranscriptionFrame) and frame.user_id in ("", "user"):
            text = frame.text

            # Echo the user's transcript to the UI immediately (chat + talk
            #    both expect to see what the user said, in conversation order).
            #    Off the hot path: serialize + ws.send, never blocks the LLM.
            if self._output_frame_cb and text and text.strip():
                try:
                    await self._output_frame_cb(TranscriptionFrame(
                        text=text, user_id="user",
                        timestamp=frame.timestamp,
                        finalized=frame.finalized,
                    ))
                except Exception as e:
                    logger.warning(f"[TURN] user transcript echo failed: {e!r}")

            # M2 coding-task dialog (§6A): forward user text; the dialog
            #    callback classifies (stop/yes/no/dispatch) and defers work —
            #    the frame still flows through, so the voice turn never blocks.
            if self._code_cb:
                self._code_cb(text)

            # M2b session log: every user turn is stored (cheap sqlite).
            if self._session_cb:
                self._session_cb("user", text)

        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
load_dotenv(os.path.join(ROOT, ".env"), override=True)
from proto import UIFrameSerializer  # noqa: E402

WS_HOST = os.environ.get("WS_HOST", "127.0.0.1")
WS_PORT = int(os.environ.get("WS_PORT", "7860"))
INTERRUPT_ENABLED = os.environ.get("INTERRUPT_ENABLED", "true").lower() == "true"

MEMORY_ENABLED = os.environ.get("MEMORY_ENABLED", "true").lower() == "true"
PERMISSION_TIMEOUT = float(os.environ.get("PERMISSION_TIMEOUT_SEC", "60"))
MEMORY_DIR = Path(ROOT) / "data" / "memory"
memory_manager = MemoryManager(
    data_dir=MEMORY_DIR,
    memory_char_limit=int(os.environ.get("MEMORY_CHAR_LIMIT", "2200")),
    user_char_limit=int(os.environ.get("USER_CHAR_LIMIT", "1375")),
)

# Brain preference file (prototype/data/brain-pref.json): persists the
# onboarding flag across restarts. The model AND reasoning fields are KEPT in
# the file for backward compatibility but have NO effect: _load/_save force
# the model to BRAIN_MODEL_ID, and every writer normalizes reasoning to
# HARDWIRED_REASONING while the LLM wiring always uses _reasoning_extra()
# ({} — field omitted), so stale values (e.g. picker-era entries) can never
# change what runs.
BRAIN_PREF_PATH = Path(ROOT) / "data" / "brain-pref.json"
# Default: hardwired brain model + reasoning at the model's own default (the
# "default" knob — field omitted, model decides).
_DEFAULT_BRAIN_PREF = {"model": BRAIN_MODEL_ID, "reasoning": "default",
                       "onboarded": False}
brain_pref_holder = dict(_DEFAULT_BRAIN_PREF)


_OC_MODELS_CACHE: dict = {"ts": 0.0, "items": None}
_OC_MODEL_TTL = 900.0  # refresh every 15 min


def _oc_models_cached(force: bool = False) -> list[dict]:
    """Real opencode model list (zen + go), from the CLI — not the static
    catalog. Returns [{"id", "tier"}] with tier "free"|"go". Falls back to
    the catalog only if the CLI is missing or fails."""
    import time
    now = time.monotonic()
    if (not force and _OC_MODELS_CACHE["items"] is not None
            and now - _OC_MODELS_CACHE["ts"] < _OC_MODEL_TTL):
        return _OC_MODELS_CACHE["items"]
    import shutil, subprocess
    items: list[dict] = []
    bin_path = shutil.which("opencode")
    if bin_path:
        env = dict(os.environ)
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "")
        for prov_id in ("opencode", "opencode-go"):
            prefix = prov_id + "/"
            tier = "go" if prov_id == "opencode-go" else "free"
            try:
                proc = subprocess.run(
                    [bin_path, "models", prov_id, "--verbose"], capture_output=True,
                    text=True, timeout=15, env=env,
                )
                items.extend(_parse_oc_models(proc.stdout, prefix, tier))
            except Exception as e:
                logger.warning(f"[LLM] opencode models {prov_id} failed: {e!r}")
    if items:
        _OC_MODELS_CACHE.update(ts=now, items=items)
        free_n = sum(1 for m in items if m.get("free"))
        logger.info(f"[LLM] opencode model list: {len(items)} ({free_n} free) (zen + go)")
        return items
    try:
        fallback = []
        for m in (providers_pkg.list_models("opencode")
                  + providers_pkg.list_models("opencode-go")):
            mid = m.get("id", "") if isinstance(m, dict) else getattr(m, "id", "")
            pid = m.get("provider_id", "") if isinstance(m, dict) else getattr(m, "provider_id", "")
            if mid:
                tier = "go" if pid == "opencode-go" else "free"
                fallback.append({"id": mid, "tier": tier, "name": mid,
                                 "free": "free" in mid or mid == "big-pickle"})
        return fallback
    except Exception as e:
        logger.warning(f"[BRAINPREF] model list failed: {e!r}")
        return []


def _parse_oc_models(text: str, prefix: str, tier: str) -> list[dict]:
    """Parse `opencode models <provider> --verbose` into model rows.

    The CLI prints `provider/id` then the model's JSON metadata. `name` is
    opencode's own display name; `free` is true when input+output cost is 0
    (the same rule opencode uses for its Free badge)."""
    out: list[dict] = []
    cur_id = None
    buf: list[str] = []

    def flush():
        if not cur_id or not buf:
            return
        try:
            d = json.loads("\n".join(buf))
        except ValueError:
            return
        cost = d.get("cost") or {}
        is_free = cost.get("input", 1) == 0 and cost.get("output", 1) == 0
        out.append({
            "id": cur_id,
            "tier": tier,
            "name": d.get("name") or cur_id,
            "free": bool(is_free),
        })

    for line in text.splitlines():
        if line.startswith(prefix):
            flush()
            cur_id = line[len(prefix):].strip()
            buf = []
        elif cur_id is not None:
            buf.append(line)
    flush()
    return out


def _deployable_model_ids() -> set:
    """ids of all opencode models (zen + go) the user may pick — never
    hardcoded; the dropdown and persisted-model validation share this."""
    return {m["id"] for m in _oc_models_cached()}


def _load_brain_pref() -> dict:
    try:
        raw = json.loads(BRAIN_PREF_PATH.read_text("utf-8"))
        pref = dict(_DEFAULT_BRAIN_PREF)
        if isinstance(raw, dict):
            pref.update(raw)
        # Hardwired (2026-09-17): a persisted model — including a stale
        # picker-era value — must never override the constant. Discard it.
        if (pref.get("model") or "") != BRAIN_MODEL_ID:
            logger.info("[BRAINPREF] persisted model ignored (brain is hardwired)")
        pref["model"] = BRAIN_MODEL_ID
        return pref
    except (OSError, ValueError):
        return dict(_DEFAULT_BRAIN_PREF)


def _save_brain_pref(pref: dict) -> None:
    # The file must never become a back-door model override: force the
    # constant on every write (reasoning/onboarded still persist).
    pref = dict(pref)
    pref["model"] = BRAIN_MODEL_ID
    try:
        BRAIN_PREF_PATH.parent.mkdir(parents=True, exist_ok=True)
        BRAIN_PREF_PATH.write_text(
            json.dumps(pref, indent=2, sort_keys=True), "utf-8",
        )
    except OSError as e:
        logger.warning(f"[BRAINPREF] save failed: {e!r}")

# M2b session recall: append-only turn log (prototype/data/state.db,
# gitignored) + combined knowledge/session recaller sharing one store.
# Connections open lazily on first use.
session_store = SessionStore()
recaller = Recaller(session_store=session_store)

# --- Worker engine init (no local opencode needed) ---
_TOOL_OUTPUT_MAX = 2000  # truncate tool output to ~2k chars over ws
# One system-wide active project (Brain + workers). Restore the last one the
# user opened (projects.json in the data dir) or fall back to the Jarvis dev tree.
_default_project = jarvis_paths.default_project(ROOT)
if jarvis_paths.in_bundle(_default_project):
    _default_project = Path.home() / "Jarvis"     # never the app bundle
init_projects(str(jarvis_paths.data_dir()), str(_default_project))
_REPO_ROOT = Path(get_repo_root())


def _startup_load_and_assert_pref() -> dict:
    """Startup sequence for the brain pref: LOAD before any write.

    A fresh process starts with ``brain_pref_holder`` at the defaults
    (``onboarded: False``), so writing it first would erase the real file
    and every boot would re-run first-time onboarding. Load the persisted
    file into the holder first, then enforce the hardwired model/reasoning
    and persist — a stale file value can still never become an override.
    Returns the holder."""
    loaded = _load_brain_pref()
    brain_pref_holder.update(loaded)
    brain_pref_holder["model"] = BRAIN_MODEL_ID
    brain_pref_holder["reasoning"] = HARDWIRED_REASONING
    _save_brain_pref(brain_pref_holder)
    return brain_pref_holder


# ---------------------------------------------------------------------------
# Clause-first text aggregation (2026-09-18)
# ---------------------------------------------------------------------------
class ClauseTextAggregator(BaseTextAggregator):
    """Release TTS text at the first clause, not the whole sentence.

    Kokoro synthesizes whatever text pipecat hands it in one pass, and pipecat's
    default aggregator waits for a full sentence, so the listener hears nothing
    until the sentence is complete. This aggregator flushes at the first clause
    boundary, so speech can begin after a few words while the rest of the
    sentence is still streaming in.

    This is OUR aggregator, injected after construction. pipecat 1.8.1 exposes
    only ``TextAggregationMode.SENTENCE`` and ``TextAggregationMode.TOKEN``
    (``pipecat/services/tts_service.py:82-93``) and hardcodes
    ``SimpleTextAggregator`` in the base ``__init__``
    (``tts_service.py:329``); it has no public ``set_text_aggregator`` and no
    clause mode. Assigning ``self._text_aggregator`` after construction is the
    supported extension point. No voice, model, config or VAD value changes here.

    A chunk is released when a clause mark (``, ; :``) or sentence mark
    (``. ! ? …``) is followed by whitespace. The lookahead avoids splitting
    numbers like ``1,000`` and joined forms like ``Hello,there``. A hard cap
    (``max_chars``, default 160) releases at the last space so nothing is ever
    chopped mid-word; if there is no space the buffer keeps growing until the
    next boundary or ``flush()``.
    """

    CLAUSE_MARKS = ",;:"
    SENTENCE_MARKS = ".!?\u2026"
    BREAK_MARKS = CLAUSE_MARKS + SENTENCE_MARKS

    def __init__(self, *, max_chars: int = 160):
        # BaseTextAggregator runs AggregationType(aggregation_type), which rejects
        # unknown strings, so we register as SENTENCE and emit our own "clause"
        # string type (Aggregation accepts any str; _process_text_frame only
        # special-cases AggregationType.TOKEN).
        super().__init__(aggregation_type=AggregationType.SENTENCE)
        self._max_chars = max_chars
        self._text = ""
        self._pending_break = False

    @property
    def text(self) -> Aggregation:
        return Aggregation(text=self._text.strip(), type="clause")

    async def aggregate(self, text: str):
        for char in text:
            self._text += char

            if self._pending_break:
                if char.isspace():
                    chunk = self._text
                    self._text = ""
                    self._pending_break = False
                    if chunk.strip():
                        yield Aggregation(text=chunk.strip(), type="clause")
                    continue
                self._pending_break = False

            if char in self.BREAK_MARKS:
                self._pending_break = True
            elif len(self._text) >= self._max_chars:
                cut = self._text.rfind(" ")
                if cut > 0:
                    chunk = self._text[:cut]
                    self._text = self._text[cut + 1 :]
                    if chunk.strip():
                        yield Aggregation(text=chunk.strip(), type="clause")

    async def flush(self) -> Aggregation | None:
        text = self._text.strip()
        self._text = ""
        self._pending_break = False
        if text:
            return Aggregation(text=text, type="clause")
        return None

    async def handle_interruption(self):
        self._text = ""
        self._pending_break = False

    async def reset(self):
        self._text = ""
        self._pending_break = False


# ---------------------------------------------------------------------------
# TTS silence guard (2026-09-18)
# ---------------------------------------------------------------------------
class GuardedKokoroTTSService(KokoroTTSService):
    """Kokoro TTS that can never leave a reply silent.

    pipecat's TTS base class closes an audio context that produces no frame
    within ``stop_frame_timeout_s`` (3.0 s) and reports ``TTS context <id>
    completed with no audio``. Kokoro synthesizes a whole sentence in one
    blocking pass, so a long sentence can exceed that window and be declared
    silent before its audio is ready (Jarvis log 2026-09-18 02:44: a
    176-character sentence closed at 3.009 s).

    This wrapper splits long text into short pieces so the first audio of each
    piece always arrives inside the window, and retries a piece that yields no
    audio, then speaks it as smaller pieces. Voice, model and speed are
    untouched; a short (normal) reply keeps the original one-shot path.
    """

    def __init__(self, *, settings=None, model_path=None, voices_path=None, **kwargs):
        super().__init__(
            settings=settings,
            model_path=model_path,
            voices_path=voices_path,
            **kwargs,
        )
        # Clause-first aggregation: replace pipecat's hardcoded SimpleTextAggregator
        # (tts_service.py:329) with ours so speech starts at the first clause.
        self._text_aggregator = self._make_text_aggregator()

    @staticmethod
    def _make_text_aggregator() -> ClauseTextAggregator:
        return ClauseTextAggregator()

    _MAX_SEGMENT_CHARS = 90
    _SENTENCE_SPLIT = re.compile(r"(?<=[.!?\u2026])\s+")
    _CLAUSE_SPLIT = re.compile(r"(?<=[,;:])\s+")

    async def run_tts(self, text: str, context_id: str):
        async for frame in self._run_guarded(text, context_id):
            yield frame

    def _hard_split(self, text: str, limit: int) -> list[str]:
        pieces, current = [], ""
        for word in text.split():
            candidate = word if not current else f"{current} {word}"
            if len(candidate) <= limit:
                current = candidate
                continue
            if current:
                pieces.append(current)
            while len(word) > limit:
                pieces.append(word[:limit])
                word = word[limit:]
            current = word
        if current:
            pieces.append(current)
        return pieces

    def _split_text(self, text: str, limit: int | None = None) -> list[str]:
        limit = limit or self._MAX_SEGMENT_CHARS
        text = (text or "").strip()
        if not text:
            return []
        if len(text) <= limit:
            return [text]

        atoms: list[str] = []
        for sentence in self._SENTENCE_SPLIT.split(text):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= limit:
                atoms.append(sentence)
                continue
            for clause in self._CLAUSE_SPLIT.split(sentence):
                clause = clause.strip()
                if not clause:
                    continue
                if len(clause) <= limit:
                    atoms.append(clause)
                else:
                    atoms.extend(self._hard_split(clause, limit))

        segments, current = [], ""
        for atom in atoms:
            candidate = atom if not current else f"{current} {atom}"
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    segments.append(current)
                current = atom
        if current:
            segments.append(current)
        return segments or [text]

    async def _speak(self, segment: str, context_id: str, stats: dict):
        async for frame in super().run_tts(segment, context_id):
            if isinstance(frame, ErrorFrame):
                stats["error"] = getattr(frame, "error", str(frame))
                continue
            if isinstance(frame, TTSAudioRawFrame):
                stats["audio"] += 1
            yield frame

    async def _run_guarded(self, text: str, context_id: str):
        segments = self._split_text(text)
        if len(segments) > 1:
            logger.info(
                f"[TTS-GUARD] long reply ({len(text)} chars) split into "
                f"{len(segments)} parts so TTS cannot be declared silent"
            )

        for index, segment in enumerate(segments, 1):
            stats = {"audio": 0, "error": None}
            async for frame in self._speak(segment, context_id, stats):
                yield frame
            if stats["audio"]:
                continue

            detail = f" ({stats['error']})" if stats["error"] else ""
            logger.warning(
                f"[TTS-GUARD] part {index}/{len(segments)} produced no audio "
                f"for {len(segment)} chars{detail}; retrying"
            )
            stats = {"audio": 0, "error": None}
            async for frame in self._speak(segment, context_id, stats):
                yield frame
            if stats["audio"]:
                continue

            pieces = self._split_text(segment, max(30, len(segment) // 2))
            if len(pieces) <= 1:
                logger.warning(
                    f"[TTS-GUARD] retry of part {index}/{len(segments)} still "
                    "produced no audio"
                )
                continue
            logger.warning(
                f"[TTS-GUARD] part {index}/{len(segments)} still silent; "
                f"speaking it as {len(pieces)} shorter pieces"
            )
            for piece in pieces:
                async for frame in self._speak(piece, context_id, {"audio": 0, "error": None}):
                    yield frame


async def _warm_kokoro_tts(tts):
    """Prime Kokoro's ONNX session off the critical path. Never raises.

    The first synthesis in a fresh process pays ONNX/one-time setup costs that
    the first real reply would otherwise absorb. This runs a throwaway
    synthesis as a background task at startup, so the first reply's first audio
    is warm-path. It is intentionally non-blocking: ``run()`` schedules it with
    ``asyncio.create_task`` and does not await it.
    """
    if tts is None:
        return
    try:
        if not getattr(tts, "_sample_rate", 0):
            tts._sample_rate = 24000
        start = time.perf_counter()
        async for frame in tts.run_tts("Warming up.", "tts-warmup"):
            if isinstance(frame, TTSAudioRawFrame):
                logger.info(
                    f"[TTS-WARM] Kokoro warm in "
                    f"{(time.perf_counter() - start) * 1000:.0f} ms"
                )
                return
        logger.warning("[TTS-WARM] Kokoro warm-up produced no audio")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[TTS-WARM] Kokoro warm-up failed: {e!r}")


async def run():
    # First-run access: provision a demo plan token when nothing is configured
    # (no payment needed). No-op once a token/BYOK key exists.
    try:
        import jarvis_access
        logger.info(f"[ACCESS] {jarvis_access.ensure_access()}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[ACCESS] provisioning skipped: {e!r}")
    # Hardwired brain (2026-09-17): the model is the constant, never a pick.
    # Load-first: the persisted onboarded flag must be read before anything
    # writes, or a fresh-process default (onboarded False) wipes the file
    # and onboarding re-runs on every start. Stale model/reasoning values
    # still never survive (forced to the constants inside).
    try:
        _startup_load_and_assert_pref()
        logger.info(f"[ACCESS] hardwired model: {BRAIN_MODEL_ID}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[ACCESS] hardwired model skipped: {e!r}")
    loop = asyncio.get_running_loop()
    llm_holder = {}
    tts_holder = {}
    user_agg_holder = {}
    ws_holder = {}

    async def on_text(frame):
        images = list(getattr(frame, "images", []) or [])
        files = list(getattr(frame, "files", []) or [])
        text = frame.text or ""

        # Build file context: prepend extracted text from PDFs/code/text files.
        file_context_parts = []
        for f in files:
            t = (f.get("text") or "").strip()
            name = f.get("name", "unknown")
            if t:
                if len(t) > 15_000:
                    t = t[:15_000] + f"\n... [truncated, {len((f.get('text') or '')):,} chars total]"
                file_context_parts.append(f"[attached file: {name}]\n{t}")
        if file_context_parts:
            file_context = "\n\n".join(file_context_parts)
            if text.strip():
                text = file_context + "\n\n" + text
            else:
                text = file_context + "\n\nPlease analyze the attached file(s)."
            try:
                frame.text = text
            except Exception:
                pass

        if images and not text.strip():
            text = "What's in this image?"
            try:
                frame.text = text
            except Exception:
                pass
        handled = on_code_text(text)
        # New user turn → reset the auto-continue counters.
        _llm0 = llm_holder.get("llm")
        if _llm0 is not None:
            _llm0._turn_tools = 0
            _llm0._turn_continues = 0
            _llm0._turn_writes = 0
            _llm0._write_nudged = False
            _llm0._turn_wants_write = bool(re.search(
                r"\b(write|create|save|produce|generate|document|design doc|"
                r"readme|report|file)\b", text or "", re.I))
        log_turn("user", text + (f" [image x{len(images)}]" if images else "") + (f" [file x{len(files)}]" if files else ""))
        # E3: periodic nudge to reflect on whether a skill/memory is worth saving.
        if _NUDGER.tick():
            logger.info(f"[SKILL] memory nudge due (turn {_NUDGER.count})")
            try:
                context.add_message({
                    "role": "system",
                    "content": ("Reminder: if the last task produced a reusable "
                                "procedure, save it with skill_save."),
                })
            except Exception:
                pass
        # Undo the last task's changes (before anything else).
        _norm = (text or "").strip().lower().rstrip(".!?")
        if (not _agent_state["running"]
                and _norm in ("undo", "revert", "undo that", "revert that",
                              "undo the last task", "revert the last task")
                and _agent_state.get("last_files")):
            asyncio.create_task(_undo_last_task())
            return
        # A running task: "stop" cancels it; anything else → busy notice.
        if _agent_state["running"]:
            if _norm in ("stop", "stop it", "cancel", "abort", "halt",
                         "never mind", "nevermind"):
                _agent_state["cancel"].set()
                await _code_send_bubble("Stopping\u2026")
            else:
                await _code_send_bubble(
                    "Already working on a task \u2014 say 'stop' to cancel it."
                )
            return
        # Work requests run through the NATIVE agent loop (reliable, opencode-
        # class); plain chat stays on the fast voice brain.
        if (not handled and not turn_router._work_mode
                and is_coding_request(text or "")):
            handled = True
            plan_only = (
                _norm.startswith(("plan ", "plan:", "plan how", "just plan"))
                or "don't change" in _norm or "dont change" in _norm
                or "without changing" in _norm or "no changes" in _norm
                or "research and plan" in _norm
            )
            asyncio.create_task(_run_agent_task(text or "", read_only=plan_only))
        if handled or turn_router._work_mode:
            return
        await maybe_inject_recall(context, recaller, text or "")
        agg = user_agg_holder.get("agg")
        if agg is not None:
            if images:
                from pipecat.frames.frames import UserImageRawFrame

                for mime, blob, w, h in images:
                    try:
                        await agg.queue_frame(
                            UserImageRawFrame(
                                image=blob,
                                size=(w, h),
                                format=mime,
                                user_id="user",
                                text=None,
                                append_to_context=True,
                            ),
                            FrameDirection.DOWNSTREAM,
                        )
                    except Exception as e:
                        logger.warning(f"[IMG] dropping image ({e!r}); text-only turn")
                logger.info(f"[IMG] queued {len(images)} image(s) for vision turn")
            if files:
                logger.info(f"[FILE] {len(files)} file(s) injected as text context")
            await agg.queue_frame(frame, FrameDirection.DOWNSTREAM)

    session_holder: dict = {}

    def log_turn(role: str, text: str):
        """Append one turn to the active session log (cheap, sync sqlite)."""
        if log_session_turn(session_store, session_holder.get("id"), role, text):
            logger.debug(f"[SESSION] +{role} ({len((text or '').strip())} chars)")

    # M2 coding-task dialog, single-user state (agent-delegation.md §6A).
    _code_state = {
        "status": "idle",  # idle | running | proposed | applying | awaiting_permission
        "session_id": None,
        "prompt_message_id": None,
        "proposal": None,
        "brief": None,  # KB-08: brain-written brief (raw-text fallback) for review_diff
        "gen": 0,  # bumped on every transition; threads discard on mismatch
        "cancel": None,  # threading.Event: set by stop to release worker waits
        "perm_event": None,  # threading.Event for the open permission ask
        "perm_decision": None,  # "once" | "reject" once the user answers
        "perm_desc": "",  # human description of the open ask
        "perm_prev": "running",  # status to restore after the ask resolves
        "worker_index": None,  # Phase 2b: which worker the current task runs on
        "worker_project": "",  # Phase 2c: the workspace that task runs in
        "pending_request": None,  # Phase 3: request waiting on a project choice
    }

    # Agent-part poller: background task that streams opencode session
    # parts to the UI while a coding session is active.
    _poller_task: asyncio.Task | None = None
    _poller_last_seen: int = 0  # index into message list (reset on session change)
    _poller_session_gen: int = 0  # guards against stale-session emissions

    async def _poll_agent_parts():
        """Background poller: GET /api/session/{id}/message every ~1.5s while
        a coding session is active. Emits new parts as AgentPartFrames when
        work mode is on. Never blocks the voice loop (KB-15 rule)."""
        nonlocal _poller_last_seen, _poller_session_gen
        my_gen = _poller_session_gen
        sid = _code_state.get("session_id")
        if not sid:
            return
        _poller_last_seen = 0
        logger.info(f"[CODE] poller started for session {sid}")
        try:
            while True:
                await asyncio.sleep(1.5)
                # Guard: if session changed or was cleared, stop.
                cur_sid = _code_state.get("session_id")
                cur_gen = _code_state.get("gen", 0)
                if cur_sid != sid or cur_gen != my_gen:
                    logger.info(f"[CODE] poller: session changed ({sid} -> {cur_sid}), stopping")
                    return
                messages = we_session_messages(sid)
                if not isinstance(messages, list):
                    continue
                # Only process messages we haven't seen yet.
                new_msgs = messages[_poller_last_seen:]
                if not new_msgs:
                    continue
                _poller_last_seen = len(messages)
                for msg in new_msgs:
                        # Guard again: session may have been cleared mid-loop.
                        if _code_state.get("session_id") != sid:
                            return
                        parts = msg.get("content") or []
                        for part in parts:
                            kind = part.get("type", "")
                            if kind == "reasoning":
                                text = part.get("text", "")
                                if text and turn_router._work_mode:
                                    await emit_to_ui(AgentPartFrame(
                                        kind="reasoning", text=text,
                                    ))
                            elif kind == "text":
                                text = part.get("text", "")
                                if text and turn_router._work_mode:
                                    await emit_to_ui(AgentPartFrame(
                                        kind="text", text=text,
                                    ))
                            elif kind == "tool":
                                tool_name = part.get("name", "")
                                state = part.get("state") or {}
                                tool_status = state.get("status", "")
                                inp = state.get("input") or {}
                                command = inp.get("command", "") if isinstance(inp, dict) else ""
                                # Collect output from content list.
                                content = state.get("content") or []
                                raw_output = ""
                                if isinstance(content, list):
                                    raw_output = "\n".join(
                                        c.get("text", "") for c in content
                                        if isinstance(c, dict) and c.get("text")
                                    )
                                truncated = False
                                if len(raw_output) > _TOOL_OUTPUT_MAX:
                                    raw_output = raw_output[:_TOOL_OUTPUT_MAX] + "\n... (truncated)"
                                    truncated = True
                                if turn_router._work_mode:
                                    await emit_to_ui(AgentPartFrame(
                                        kind="tool", tool=tool_name,
                                        status=tool_status, command=command,
                                        output=raw_output, truncated=truncated,
                                    ))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[CODE] poller error: {e!r}")
        finally:
            logger.info(f"[CODE] poller stopped for session {sid}")

    def _start_poller(session_id: str):
        """Start the agent-part poller for a new coding session."""
        nonlocal _poller_task, _poller_session_gen
        # Cancel any existing poller first.
        if _poller_task and not _poller_task.done():
            _poller_task.cancel()
        _poller_session_gen = _code_state["gen"]
        # Schedule on the MAIN event loop, not the current (worker) thread:
        # _cb runs inside asyncio.to_thread (opencode dispatch), where
        # asyncio.ensure_future finds no running loop -> the coroutine was
        # never awaited and the propose flow broke with 'no current event
        # loop in thread'. loop.create_task binds to the real loop.
        _poller_task = loop.create_task(_poll_agent_parts())

    def _stop_poller():
        """Cancel the agent-part poller (called on session end / disconnect)."""
        nonlocal _poller_task
        if _poller_task and not _poller_task.done():
            _poller_task.cancel()
            _poller_task = None

    # --- Worker activity poller (work mode panels) ---
    _worker_poller_task: asyncio.Task | None = None
    _worker_poller_sessions: dict[str, int] = {}  # session_id -> last_seen_msg_count

    async def _poll_worker_activity():
        """Push live activity from native worker sessions to UI panels.
        Runs while work mode is active. One push every 2s."""
        logger.info("[WORK] activity poller started")
        try:
            while turn_router._work_mode:
                await asyncio.sleep(2)
                try:
                    sessions = we_list_sessions()
                except Exception:
                    continue
                if not isinstance(sessions, list):
                    continue
                now_ms = time.time() * 1000
                ACTIVE_CUTOFF_MS = 3600_000
                for s in sessions:
                    sid = s.get("id", "")
                    if not sid:
                        continue
                    updated = (s.get("time") or {}).get("updated", 0)
                    if now_ms - updated > ACTIVE_CUTOFF_MS:
                        continue
                    try:
                        messages = we_session_messages(sid)
                    except Exception:
                        continue
                    if not isinstance(messages, list) or not messages:
                        continue
                    last_count = _worker_poller_sessions.get(sid, 0)
                    if len(messages) <= last_count:
                        continue
                    _worker_poller_sessions[sid] = len(messages)
                    # Build activity feed from message content
                    activity = []
                    for msg in messages:
                        content = msg.get("content") or []
                        for part in content:
                            kind = part.get("type", "")
                            if kind == "tool":
                                name = part.get("name", "")
                                state = part.get("state") or {}
                                status = state.get("status", "")
                                inp = state.get("input") or {}
                                file_path = ""
                                command = ""
                                if isinstance(inp, dict):
                                    file_path = inp.get("path", "") or inp.get("file", "") or inp.get("filePath", "")
                                    command = inp.get("command", "")
                                text = ""
                                if status == "completed":
                                    out = state.get("content") or []
                                    if isinstance(out, list):
                                        texts = [c.get("text", "") for c in out if isinstance(c, dict) and c.get("text")]
                                        text = " ".join(texts)[:200]
                                activity.append({
                                    "kind": "tool",
                                    "tool": name,
                                    "status": status,
                                    "file": file_path,
                                    "command": command[:120],
                                    "text": text[:200],
                                })
                            elif kind == "text":
                                txt = part.get("text", "")
                                if txt:
                                    activity.append({"kind": "text", "text": txt[:300]})
                    # Determine session status
                    last_msg = messages[-1] if messages else {}
                    finish = last_msg.get("finish", "")
                    session_status = "running"
                    if finish == "stop":
                        session_status = "completed"
                    elif finish == "error":
                        session_status = "error"
                    title = s.get("title") or s.get("agent", "worker")
                    agent = s.get("agent", "opencode")
                    await emit_to_ui(WorkerActivityFrame(
                        worker_id=sid, status=session_status,
                        activity=activity[-20:], title=title, agent=agent,
                    ))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[WORK] activity poller error: {e!r}")
        finally:
            logger.info("[WORK] activity poller stopped")

    def _start_worker_poller():
        nonlocal _worker_poller_task
        _worker_poller_sessions.clear()
        if _worker_poller_task and not _worker_poller_task.done():
            _worker_poller_task.cancel()
        _worker_poller_task = loop.create_task(_poll_worker_activity())

    def _stop_worker_poller():
        nonlocal _worker_poller_task
        if _worker_poller_task and not _worker_poller_task.done():
            _worker_poller_task.cancel()
            _worker_poller_task = None

    async def _code_send_bubble(text: str, *, persist: bool = False):
        await _send_code_bubble(emit_to_ui, log_turn, text, persist=persist)

    async def _code_send_result(text: str):
        """Coding-task outcome bubble — renders amber (⚡ Ashish) in the UI,
        distinct from normal bot_text chatter. Always stored (one bot row)
        so the reply is still there when the chat is reopened."""
        await _send_code_result(emit_to_ui, log_turn, text)

    async def _emit_worker_update(worker_id: str, status: str, name: str = ""):
        """Push a worker status update to the UI."""
        ws = ws_holder.get("ws")
        if ws:
            try:
                await ws.send(json.dumps({
                    "type": "worker_update",
                    "worker_id": worker_id,
                    "status": status,
                    "name": name,
                }))
            except Exception:
                pass

    async def _emit_worker_permission(worker_id: str, action: str, target: str):
        """Push a permission request for a specific worker to the UI."""
        ws = ws_holder.get("ws")
        if ws:
            try:
                await ws.send(json.dumps({
                    "type": "worker_permission",
                    "worker_id": worker_id,
                    "action": action,
                    "target": target,
                }))
            except Exception:
                pass

    async def _emit_worker_permission_clear(worker_id: str):
        ws = ws_holder.get("ws")
        if ws:
            try:
                await ws.send(json.dumps({
                    "type": "worker_permission_clear",
                    "worker_id": worker_id,
                }))
            except Exception:
                pass

    async def _emit_brain_status(text: str):
        ws = ws_holder.get("ws")
        if ws:
            try:
                await ws.send(json.dumps({
                    "type": "brain_status",
                    "text": text,
                }))
            except Exception:
                pass

    def _code_on_session_maker(mygen: int, phase: str):
        """Register the live session id for stop-mid-run. If the generation
        moved on (stop won the race), contain the orphan via stop_session —
        the deleted-only revert rule keeps it safe."""
        def _cb(session_id: str, prompt_message_id: str):
            if (_code_state["gen"] == mygen
                    and _code_state["status"] in ("running", "applying")):
                _code_state["session_id"] = session_id
                _code_state["prompt_message_id"] = prompt_message_id
                _start_poller(session_id)
                # notify UI of new worker
                asyncio.run_coroutine_threadsafe(
                    _emit_worker_update(session_id, "running", phase),
                    loop,
                )
            else:
                logger.warning(f"[CODE] orphan {phase} session {session_id} — containing")
                try:
                    opencode_stop_session(session_id, prompt_message_id)
                except Exception as e:
                    logger.warning(f"[CODE] orphan contain failed: {e!r}")
        return _cb

    def _code_permission_maker(mygen: int):
        """Answer one agent permission ask (worker thread): emit the ask as a
        proposal-style bubble, wait for allow/deny, default-deny on timeout.
        Contract with opencode_tool: always returns within PERMISSION_TIMEOUT.
        """
        def _ask(req: dict) -> str:
            action = (req or {}).get("action", "?")
            resources = (req or {}).get("resources") or []
            desc = f"{action}: {', '.join(resources)}"[:300]
            logger.info(f"[CODE] permission asked: {desc}")
            st = _code_state
            if st["gen"] != mygen or st["status"] not in ("running", "applying"):
                logger.info("[CODE] permission arrived stale → reject")
                return "reject"
            # A coding agent must be able to LOOK around without a permission
            # dance: safe read-only shell commands are auto-allowed.
            first = str(resources[0]) if resources else ""
            if action == "bash" and _is_readonly_bash(first):
                logger.info(f"[CODE] auto-allow read-only bash: {first[:80]}")
                return "once"
            # A3: consult ordered wildcard rules before asking the user.
            subject = f"{action}: {', '.join(resources)}"
            rule = _PERMS.decide(subject)
            if rule == permissions_mod.ALLOW:
                logger.info(f"[CODE] permission auto-allow by rule: {subject}")
                return "once"
            if rule == permissions_mod.DENY:
                logger.info(f"[CODE] permission auto-deny by rule: {subject}")
                return "reject"
            ev = threading.Event()
            st["perm_prev"] = st["status"]
            st["perm_event"] = ev
            st["perm_decision"] = None
            st["perm_desc"] = desc
            st["status"] = "awaiting_permission"
            # push permission to worker panel overlay
            session_id = st.get("session_id", "")
            if session_id:
                asyncio.run_coroutine_threadsafe(
                    _emit_worker_permission(session_id, action, desc),
                    loop,
                )
            fut = asyncio.run_coroutine_threadsafe(
                _code_send_bubble(
                    f"Agent wants access to {desc} — say 'allow' or 'deny' "
                    f"(auto-denies in {int(PERMISSION_TIMEOUT)}s)."
                ),
                loop,
            )
            try:
                fut.result(timeout=10)
            except Exception as e:
                logger.warning(f"[CODE] permission ask bubble failed: {e!r}")
            decided = ev.wait(PERMISSION_TIMEOUT)
            decision = st.get("perm_decision") or "reject"
            if st["gen"] != mygen:
                decision = "reject"  # stopped meanwhile
            st["perm_event"] = None
            st["perm_decision"] = None
            if st["status"] == "awaiting_permission":
                st["status"] = st.get("perm_prev", "running")
            # clear permission overlay in worker panel
            if session_id:
                asyncio.run_coroutine_threadsafe(
                    _emit_worker_permission_clear(session_id),
                    loop,
                )
            if not decided:
                logger.info(f"[CODE] permission default-deny (no answer): {desc}")
                fut2 = asyncio.run_coroutine_threadsafe(
                    _code_send_bubble(
                        "No answer — denied by default. The task is blocked "
                        "without that access.",
                        persist=True,
                    ),
                    loop,
                )
                try:
                    fut2.result(timeout=10)
                except Exception:
                    pass
            else:
                logger.info(f"[CODE] permission {decision} by user: {desc}")
            if decided and decision == "always":
                # A3: 'always' learns an allow rule so we never ask again.
                _PERMS.add_rule(subject, permissions_mod.ALLOW)
                logger.info(f"[CODE] learned allow rule: {subject}")
                decision = "once"
            return decision if decided else "reject"
        return _ask

    def on_code_text(text: str) -> bool:
        """One-agent mode: there is no coding dialog.

        The single brain handles everything with its own tools, so this is a
        no-op that always returns False (the caller then feeds the turn to the
        brain). Kept as a stub so the TurnRouter code_cb wiring stays valid.
        """
        return False

    _agent_state = {"running": False, "cancel": threading.Event(),
                    "last_files": [], "messages": []}
    # Resume the last agent session for the active project (survives restarts).
    try:
        _agent_state["messages"], _agent_state["last_files"] = \
            _load_agent_session(get_repo_root())
        if _agent_state["messages"]:
            logger.info(f"[AGENT] resumed session "
                        f"({len(_agent_state['messages'])} msgs)")
    except Exception:
        pass

    async def _undo_last_task():
        """Revert the last task: restore its pre-task git snapshot when present,
        else the per-file fallback (tracked → git checkout; new → delete)."""
        project = str(Path(get_repo_root()).resolve())
        snap = _agent_state.get("snapshot")
        if snap:
            n = await asyncio.to_thread(snapshot_mod.restore, project, snap)
            _agent_state["snapshot"] = None
            _agent_state["last_files"] = []
            await _code_send_bubble(
                f"Reverted the last task from its snapshot ({n} path(s) restored).")
            await _code_speak("Reverted the last task's changes.", tts_holder)
            return
        files = _agent_state.get("last_files") or []
        if not files:
            await _code_send_bubble("Nothing to undo.")
            return

        def _do() -> int:
            n = 0
            root = Path(project)
            for f in files:
                p = Path(f)
                try:
                    rel = str(p.relative_to(root))
                except ValueError:
                    continue
                r = subprocess.run(
                    ["git", "-C", project, "ls-files", "--error-unmatch", rel],
                    capture_output=True, text=True)
                if r.returncode == 0:
                    subprocess.run(["git", "-C", project, "checkout", "--", rel],
                                   capture_output=True, text=True)
                elif p.is_file():
                    p.unlink()
                n += 1
            return n

        n = await asyncio.to_thread(_do)
        _agent_state["last_files"] = []
        await _code_send_bubble(f"Reverted {n} file(s) from the last task.")
        await _code_speak("Reverted the last task's changes.", tts_holder)

    async def _run_agent_task(request: str, read_only: bool = False):
        """Run a work request through the NATIVE agent loop (no workers, no
        opencode): stream each tool step to the UI, then speak the outcome."""
        request = (request or "").strip()
        if not request:
            return
        project = get_repo_root()
        # Coding-agent model resolution:
        #   1) the user's own BYOK agent provider/key wins (their provider + model),
        #   2) else a BYOK picker selection 'provider|model',
        #   3) else a provider explicitly chosen in Settings,
        #   4) else the opencode model + lookup, else the default go model.
        # A packaged app has no dev env key to fall back to: with no user key the
        # agent degrades with one plain sentence instead of using our key.
        cfg = providers_config()
        cfg_provider = (getattr(cfg, "brain_provider", "") or "").strip().lower()
        pref_model = (brain_pref_holder.get("model") or "").strip() or _default_model()
        byok = resolve_agent_provider()
        if byok["configured"]:
            provider_id = register_agent_provider(byok) or byok["provider"]
            model = byok["model"]
        elif _is_packaged():
            await _code_send_bubble(byok["message"], persist=True)
            await emit_to_ui(StatusFrame(state="ready"))
            return
        elif "|" in pref_model:
            provider_id, model = pref_model.split("|", 1)
        elif cfg_provider and cfg_provider != "opencode" and _provider_ready(cfg_provider):
            provider_id = cfg_provider
            model = (getattr(cfg, "brain_model", "") or "").strip() or BRAIN_MODEL_ID
        else:
            model = pref_model
            provider_id = _model_provider_id(model) or "opencode"
        # P0.4: snapshot the project before the task so undo restores it exactly.
        _agent_state["snapshot"] = await asyncio.to_thread(
            snapshot_mod.capture, str(project))
        _agent_state["running"] = True
        _agent_state["cancel"].clear()
        await emit_to_ui(StatusFrame(state="coding"))
        await _code_send_bubble(
            ("Planning (research only) in " if read_only else "On it \u2014 working in ")
            + f"{Path(project).name}."
        )
        logger.info(f"[AGENT] task start project={project} model={model} "
                    f"read_only={read_only}")

        def _on_step(name: str, args: dict):
            detail = ""
            if isinstance(args, dict):
                for k in ("path", "filePath", "command", "query", "url", "pattern"):
                    if args.get(k):
                        detail = f"{k}={str(args[k])[:70]}"
                        break
            try:
                asyncio.run_coroutine_threadsafe(
                    emit_to_ui(BrainActivityFrame(phase="tool", tool=name, detail=detail)),
                    loop,
                )
            except Exception:
                pass

        def _on_token(delta: str):
            try:
                asyncio.run_coroutine_threadsafe(
                    emit_to_ui(BrainActivityFrame(phase="reasoning", text=delta)),
                    loop,
                )
            except Exception:
                pass

        from agents import registry
        agent_id = registry.pick()          # the brain decides; this is the fallback
        registry.start(agent_id, request[:80])
        await _push_agents()
        try:
            await emit_to_ui(BrainActivityFrame(phase="start"))
        except Exception:
            pass
        task_ok = False
        try:
            out = await asyncio.to_thread(
                agent_loop.run_task, request, project,
                provider_id=provider_id, model=model, on_step=_on_step,
                cancel=_agent_state["cancel"], read_only=read_only,
                history=_agent_state.get("messages") or None,
                on_token=_on_token,
            )
            task_ok = True
        except Exception as e:
            logger.warning(f"[AGENT] failed: {e!r}")
            await _code_send_bubble(f"That didn't work: {e}", persist=True)
            await emit_to_ui(StatusFrame(state="ready"))
            return
        finally:
            _agent_state["running"] = False
            try:
                await emit_to_ui(BrainActivityFrame(phase="done"))
            except Exception:
                pass
            registry.finish(agent_id, ok=task_ok)
            try:
                await _push_agents()
            except Exception:
                pass
        _agent_state["last_files"] = out.get("files") or []
        _agent_state["messages"] = out.get("messages") or []
        _save_agent_session(project, _agent_state["messages"],
                            _agent_state["last_files"])
        if out.get("cancelled"):
            await _code_speak("Stopped.", tts_holder)
            await _code_send_result("Stopped.")
            await emit_to_ui(StatusFrame(state="ready"))
            return
        summary = (out.get("text") or "").strip() or "Done."
        logger.info(
            f"[AGENT] done steps={out.get('steps')} wrote={out.get('wrote')}"
        )
        await _code_speak(_speak_summary(summary), tts_holder)
        await _code_send_result(summary[:1500])
        await emit_to_ui(StatusFrame(state="ready"))

    async def _code_resolve(kind: str):
        """Shared stop/drop path: wake any permission wait, abort +
        conditional revert, then ready."""
        st = _code_state
        if st["status"] not in ("running", "proposed", "applying",
                                "awaiting_permission"):
            logger.info(f"[CODE] {kind} with no active run → no-op")
            return "noop"
        st["gen"] += 1
        ev = st.get("perm_event")
        st["perm_event"] = None
        if ev is not None:
            st["perm_decision"] = "reject"
            ev.set()  # release the permission waiter (deny)
        ce = st.get("cancel")
        st["cancel"] = None
        if ce is not None:
            ce.set()  # release the worker poll promptly
        sid, pid = st["session_id"], st["prompt_message_id"]
        st.update(status="idle", session_id=None, prompt_message_id=None,
                  proposal=None)
        _stop_poller()
        held: list = []
        if sid and pid:
            try:
                res = await asyncio.to_thread(opencode_stop_session, sid, pid)
                held = res.get("held_files") or []
                logger.info(
                    f"[CODE] {kind}: aborted={res.get('aborted')} "
                    f"reverted={res.get('reverted')} held={held}"
                )
            except Exception as e:
                logger.warning(f"[CODE] {kind} cleanup failed: {e!r}")
                await emit_to_ui(StatusFrame(state="ready"))
                await _code_send_bubble(f"Couldn't stop cleanly: {e}", persist=True)
                return "error"
        await emit_to_ui(StatusFrame(state="ready"))
        if held:
            names = ", ".join(held)
            if kind == "stopped":
                await _code_speak(
                    "Stopped. Some changed files were left for your review.",
                    tts_holder,
                )
                await _code_send_result(
                    f"Stopped. These files were touched and left as-is "
                    f"for your review: {names}"
                )
            else:
                await _code_speak(
                    "OK, dropped. Some changed files were left for your review.",
                    tts_holder,
                )
                await _code_send_result(
                    f"OK, dropped. These files were touched and left as-is "
                    f"for your review: {names}"
                )
        elif kind == "stopped":
            await _code_speak("Stopped. Nothing was applied.", tts_holder)
            await _code_send_result("Stopped — nothing was applied.")
        else:
            await _code_speak("OK, dropped. Nothing was applied.", tts_holder)
            await _code_send_result("OK, dropped — nothing was applied.")
        return "resolved"

    async def _run_orchestrate(request: str):
        """Phase 1: plan a request and run workers via the opencode CLI, then
        report. Isolated worktrees when the project is a clean git repo, else
        sequential in-place. Always off the voice hot path."""
        request = (request or "").strip()
        for kw in ("orchestrate", "orchestration"):
            if request.lower().startswith(kw):
                request = request[len(kw):].strip(" :,-")
                break
        if not request:
            await _code_send_bubble("Tell me what to orchestrate.")
            return
        project = get_repo_root()
        task_id = _task_register("orchestrate", request)
        await emit_to_ui(StatusFrame(state="coding"))
        await _code_send_bubble(f"Orchestrating in {Path(project).name}: {request}")
        # F1: narrate the start so a long parallel run isn't dead air.
        await _code_speak("Planning this out and splitting it across workers.", tts_holder)
        # F2: register a cancel event so "stop" (barge-in) aborts the run.
        cancel = threading.Event()
        prev_status = _code_state.get("status")
        if prev_status == "idle":
            _code_state["status"] = "running"
            _code_state["cancel"] = cancel
        try:
            orch = orchestrator_mod.Orchestrator(project)
            roster = _worker_roster()
            out = await asyncio.to_thread(orch.run, request, roster, True, cancel)
        except Exception as e:
            logger.warning(f"[ORCH] failed: {e!r}")
            _task_finish(task_id, "failed", str(e))
            await _code_send_bubble(f"Orchestration failed: {e}", persist=True)
            await emit_to_ui(StatusFrame(state="ready"))
            return
        finally:
            if prev_status == "idle" and _code_state.get("cancel") is cancel:
                _code_state["status"] = "idle"
                _code_state["cancel"] = None
        if out.get("cancelled"):
            _task_finish(task_id, "cancelled")
            await _code_speak("Stopped. Nothing was applied.", tts_holder)
            await _code_send_bubble("Stopped — orchestration cancelled.")
            await emit_to_ui(StatusFrame(state="ready"))
            return
        mode = "parallel (isolated worktrees)" if out.get("isolated") else "sequential (in-place)"
        await _code_send_bubble(f"Ran {len(out['results'])} worker(s) — {mode}.")
        for r in out["results"]:
            tag = "applied" if r.get("applied") else ("changes" if r.get("diff") else "no change")
            review = r.get("verdict", "accept")
            review_txt = "" if review == "accept" else f", review={review}"
            await _code_send_bubble(
                f"{r['model'].split('/')[-1]} — task {r['task_index']} "
                f"[{r['status']}{review_txt}, {tag}] {r['summary'][:180]}"
            )
        await _code_send_bubble(out.get("summary", ""))
        conf = out.get("conflicts") or []
        if conf:
            await _code_send_bubble(
                f"{len(conf)} change(s) could not be applied automatically \u2014 "
                "left in worktrees for review:"
            )
            for c in conf:
                await _code_send_bubble(
                    f"task {c['task_index']}: {c['worktree']} "
                    f"({c.get('error') or 'conflict'})"
                )
        # F1: speak the outcome instead of leaving dead air.
        _task_finish(task_id, "done", out.get("summary", ""))
        await _code_speak(_speak_summary(out.get("summary", "")), tts_holder)
        await emit_to_ui(StatusFrame(state="ready"))

    async def on_orchestrate(msg: dict):
        await _run_orchestrate(msg.get("request") or "")

    async def on_code_action(action: str, text: str):
        """Deferred coding dialog step (never on the voice hot path)."""
        st = _code_state
        if action == "stop":
            await _code_resolve("stopped")
            return
        if action == "orchestrate":
            await _run_orchestrate(text)
            return
        if action in ("allow", "deny"):
            if st["status"] != "awaiting_permission" or st.get("perm_event") is None:
                return
            if action == "allow":
                st["perm_decision"] = "always" if _is_always(text) else "once"
            else:
                st["perm_decision"] = "reject"
            st["perm_event"].set()
            logger.info(f"[CODE] permission {action} by user")
            return
        if action in ("yes", "no"):
            if st["status"] != "proposed" or not st["session_id"]:
                return
            if action == "no":
                await _code_resolve("dropped")
                return
            # yes → apply phase in the SAME session (M3 will supervise).
            st["gen"] += 1
            mygen = st["gen"]
            st["status"] = "applying"
            sid = st["session_id"]
            cancel = threading.Event()
            st["cancel"] = cancel
            try:
                result = await asyncio.to_thread(
                    opencode_followup, sid,
                    "Apply the plan you just proposed. Make the edits now. "
                    "Do not commit anything.",
                    on_session=_code_on_session_maker(mygen, "apply"),
                    on_permission=_code_permission_maker(mygen),
                    permission_timeout=PERMISSION_TIMEOUT,
                    cancel=cancel,
                )
            except Exception as e:
                if mygen != st["gen"]:
                    logger.info("[CODE] apply error discarded (stopped meanwhile)")
                    return
                logger.warning(f"[CODE] apply failed: {e!r}")
                st["status"] = "idle"
                st["session_id"] = None
                _stop_poller()
                await emit_to_ui(StatusFrame(state="ready"))
                await _code_send_bubble(f"Couldn't apply that change: {e}", persist=True)
                return
            if mygen != st["gen"]:
                logger.info("[CODE] apply result discarded (stopped meanwhile)")
                return
            if result.get("status") == "blocked":
                st.update(status="idle", session_id=None, prompt_message_id=None,
                          proposal=None, cancel=None)
                _stop_poller()
                await emit_to_ui(StatusFrame(state="ready"))
                blocked = (result.get("text") or "").strip()[:1200]
                await _code_speak(_speak_summary(blocked), tts_holder)
                await _code_send_result(blocked)
                return
            summary = (result.get("text") or "").strip() or "(empty result)"
            # KB-08: brain judges the real VCS diff before reporting done.
            # review None = fail-soft (brain down / clean tree) → report done.
            review = None
            if mygen == st["gen"]:
                try:
                    diff_text = await asyncio.to_thread(_code_worktree_diff, st.get('worker_project') or '')
                except Exception as e:
                    logger.warning(f"[CODE] brain review skipped (diff failed): {e!r}")
                    diff_text = ""
                if diff_text.strip():
                    review = await asyncio.to_thread(
                        _code_review_diff, diff_text, st.get("brief") or ""
                    )
            verdict = (review or {}).get("verdict")
            if verdict == "rework" and mygen == st["gen"]:
                # Bounded single retry: re-brief the SAME session with the
                # brain's feedback (≤1 retry — a still-dirty result is
                # reported as needs-changes, never retried again).
                issues = (review.get("issues") or [])[:8]
                issues_txt = "\n".join(f"- {i}" for i in issues) or "- (no details)"
                fb = (
                    "The supervisor reviewed your diff against the brief and "
                    "requests changes — one revision round only:\n"
                    f"{(review.get('summary') or '').strip()}\n"
                    f"Issues:\n{issues_txt}\n"
                    "Revise the edits to address every issue, then summarize. "
                    "Do not commit anything."
                )
                logger.info(f"[CODE] brain rework → one retry in session={sid}")
                st["gen"] += 1
                mygen = st["gen"]
                cancel = threading.Event()
                st["cancel"] = cancel
                try:
                    result = await asyncio.to_thread(
                        opencode_followup, sid, fb,
                        on_session=_code_on_session_maker(mygen, "rework"),
                        on_permission=_code_permission_maker(mygen),
                        permission_timeout=PERMISSION_TIMEOUT,
                        cancel=cancel,
                    )
                except Exception as e:
                    if mygen != st["gen"]:
                        logger.info("[CODE] rework error discarded (stopped meanwhile)")
                        return
                    logger.warning(f"[CODE] rework failed: {e!r}")
                    st["status"] = "idle"
                    st["session_id"] = None
                    _stop_poller()
                    await emit_to_ui(StatusFrame(state="ready"))
                    await _code_send_bubble(f"Couldn't revise that change: {e}", persist=True)
                    return
                if mygen != st["gen"]:
                    logger.info("[CODE] rework result discarded (stopped meanwhile)")
                    return
                if result.get("status") == "blocked":
                    st.update(status="idle", session_id=None, prompt_message_id=None,
                              proposal=None, cancel=None)
                    _stop_poller()
                    await emit_to_ui(StatusFrame(state="ready"))
                    blocked = (result.get("text") or "").strip()[:1200]
                    await _code_speak(_speak_summary(blocked), tts_holder)
                    await _code_send_result(blocked)
                    return
                summary = (result.get("text") or "").strip() or "(empty result)"
                try:
                    diff_text = await asyncio.to_thread(_code_worktree_diff, st.get('worker_project') or '')
                except Exception as e:
                    logger.warning(f"[CODE] rework review skipped (diff failed): {e!r}")
                    diff_text = ""
                review = None
                if diff_text.strip() and mygen == st["gen"]:
                    review = await asyncio.to_thread(
                        _code_review_diff, diff_text, st.get("brief") or ""
                    )
                verdict = (review or {}).get("verdict")
                if verdict not in ("accept", None):
                    # Still dirty after the one retry — report needs-changes
                    # and leave the tree for the user (user is the final gate).
                    issues2 = (review.get("issues") or [])[:8]
                    issues_txt2 = "\n".join(f"- {i}" for i in issues2) or "- (no details)"
                    st.update(status="idle", session_id=None, prompt_message_id=None,
                              proposal=None)
                    _stop_poller()
                    await emit_to_ui(StatusFrame(state="ready"))
                    await _code_speak(
                        "I revised it once, but it still needs changes.", tts_holder
                    )
                    await _code_send_result(
                        "Needs changes (revised once, still off-brief):\n"
                        f"{issues_txt2}"[:1500]
                    )
                    return
            if verdict == "reject" and mygen == st["gen"]:
                # Brain rejected the diff → revert the apply turn via the
                # existing stop machinery (deleted-only rule keeps it safe)
                # and report nothing applied. A rejected diff is never kept.
                logger.info(f"[CODE] brain reject → reverting apply turn session={sid}")
                reverted, held = False, []
                try:
                    res = await asyncio.to_thread(
                        opencode_stop_session, sid, st.get("prompt_message_id")
                    )
                    reverted = bool(res.get("reverted"))
                    held = res.get("held_files") or []
                except Exception as e:
                    logger.warning(f"[CODE] reject revert failed: {e!r}")
                st.update(status="idle", session_id=None, prompt_message_id=None,
                          proposal=None, cancel=None)
                _stop_poller()
                await emit_to_ui(StatusFrame(state="ready"))
                why = (review.get("summary") or "").strip()[:300]
                if reverted and not held:
                    await _code_speak(
                        "The brain rejected that change — nothing was applied.",
                        tts_holder,
                    )
                    await _code_send_result(f"Rejected — nothing applied. Brain: {why}")
                elif held:
                    names = ", ".join(held)
                    await _code_speak(
                        "The brain rejected that change. "
                        "Some files were left for your review.",
                        tts_holder,
                    )
                    await _code_send_result(
                        "Rejected — these files were touched and left as-is "
                        f"for your review: {names}. Brain: {why}"
                    )
                else:
                    await _code_speak("The brain rejected that change.", tts_holder)
                    await _code_send_result(
                        "Rejected — could not revert cleanly, "
                        f"review the tree. Brain: {why}"
                    )
                return
            if mygen != st["gen"]:
                logger.info("[CODE] apply result discarded (stopped meanwhile)")
                return
            st.update(status="idle", session_id=None, prompt_message_id=None,
                      proposal=None)
            _stop_poller()
            logger.info(
                f"[CODE] applied session={sid} cost={result.get('cost')} "
                f"tier={result.get('tier')} model={result.get('model_id')} "
                f"chars={len(summary)}"
            )
            await emit_to_ui(StatusFrame(state="ready"))
            await _code_speak(_speak_summary(summary), tts_holder)
            await _code_send_result(f"Applied. {summary[:1200]}")
            return
        if action == "dispatch":
            if not is_project_chosen():
                logger.info("[CODE] gate: no project chosen — prompting")
                await _code_speak("Sure \u2014 which project should I work in?", tts_holder)
                await emit_to_ui(ProjectGateFrame(
                    message="Pick a project folder so I know where to work."
                ))
                st["pending_request"] = text
                return
            if st["status"] != "idle":
                await _code_send_bubble(
                    "Already working on a coding task — say 'stop' to cancel it first."
                )
                return
            st["gen"] += 1
            mygen = st["gen"]
            st["status"] = "running"
            cancel = threading.Event()
            st["cancel"] = cancel
            await emit_to_ui(StatusFrame(state="coding"))
            await _code_speak("On it. I'll write that script now.", tts_holder)
            logger.info("[CODE] dispatching coding task...")
            # KB-08: brain writes the brief. Phase 2b: the brain also gets the
            # worker roster and picks WHO does the task (WORKER: n); we then run
            # it on that worker's project + model. Fail-soft to raw text.
            roster = _worker_roster(st.get("worker_index") if st.get("status") != "idle" else None)
            roster_text = _worker_roster_text(roster)
            brief_text, used_brain = await asyncio.to_thread(_code_make_brief, text, roster_text)
            wi = _parse_worker_choice(brief_text)
            if wi is None:
                wi = _fallback_worker(text, roster)
            worker = (list_workers()[wi] if 0 <= wi < len(list_workers()) else {})
            st["worker_index"] = wi
            # Run the task in the chosen worker's project + model. The project
            # is threaded into the session (per-worker tools), NOT applied to
            # the global active project, so the Brain's own project is stable.
            wproj = worker.get("project") or get_repo_root()
            wmodel = worker.get("model") or ""
            st["worker_project"] = wproj
            brief_text = _clean_brief(brief_text)
            st["brief"] = brief_text
            logger.info(
                f"[CODE] brief source: {'brain' if used_brain else 'raw text'} "
                f"→ {worker_name(wi)} ({wmodel or 'default'}) in {wproj}"
            )
            prov_id = worker.get("provider") or (_model_provider_id(wmodel) if wmodel else None)
            brief = (
                f"{brief_text}\n\n"
                f"(Workspace: {wproj}. Stay inside it. Do not commit anything.)"
            )
            from agents import registry
            agent_id = registry.pick()      # the brain decides; this is the fallback
            registry.start(agent_id, brief_text[:80])
            await _push_agents()
            propose_ok = False
            try:
                result = await asyncio.to_thread(
                    opencode_propose, brief,
                    model_id=(wmodel or None),
                    provider_id=prov_id,
                    reasoning=(worker.get("reasoning") or ""),
                    project=wproj,
                    on_session=_code_on_session_maker(mygen, "propose"),
                    on_permission=_code_permission_maker(mygen),
                    permission_timeout=PERMISSION_TIMEOUT,
                    cancel=cancel,
                )
                propose_ok = True
            except Exception as e:
                if mygen != st["gen"]:
                    logger.info("[CODE] propose error discarded (stopped meanwhile)")
                    return
                logger.warning(f"[CODE] propose failed: {e!r}")
                st["status"] = "idle"
                st["session_id"] = None
                _stop_poller()
                await emit_to_ui(StatusFrame(state="ready"))
                await _code_send_bubble(f"Couldn't run that coding task: {e}", persist=True)
                return
            finally:
                registry.finish(agent_id, ok=propose_ok)
                try:
                    await _push_agents()
                except Exception:
                    pass
            if mygen != st["gen"]:
                logger.info("[CODE] proposal discarded (stopped meanwhile)")
                return
            if result.get("status") == "blocked":
                st.update(status="idle", session_id=None, prompt_message_id=None,
                          proposal=None, cancel=None)
                _stop_poller()
                await emit_to_ui(StatusFrame(state="ready"))
                blocked = (result.get("text") or "").strip()[:1200]
                await _code_speak(_speak_summary(blocked), tts_holder)
                await _code_send_result(blocked)
                return
            summary = (result.get("text") or "").strip()
            if not summary:
                st.update(status="idle", session_id=None, proposal=None)
                _stop_poller()
                await emit_to_ui(StatusFrame(state="ready"))
                await _code_send_bubble("The agent came back empty — nothing proposed.",
                                          persist=True)
                return
            st["status"] = "proposed"
            st["proposal"] = summary
            logger.info(
                f"[CODE] proposed session={result.get('session_id')} "
                f"cost={result.get('cost')} tier={result.get('tier')} "
                f"model={result.get('model_id')} chars={len(summary)}"
            )
            await _code_send_result(
                f"{summary[:1200]}"
            )
            await _code_speak(_speak_summary(summary), tts_holder)

    async def on_approve(session_id: str):
        """UI approve button → yes-path, session-match checked. While a
        permission ask is open, approve means allow (single pending ask)."""
        st = _code_state
        if st["status"] == "awaiting_permission":
            logger.info(f"[CODE] approve button → allow ({session_id})")
            await on_code_action("allow", f"button:{session_id}")
            return
        if st["status"] != "proposed":
            logger.info(f"[CODE] approve with no proposal → ignored ({session_id})")
            return
        if session_id and session_id != st["session_id"]:
            logger.warning(
                f"[CODE] approve session mismatch: {session_id} "
                f"vs {st['session_id']} → ignored"
            )
            await _code_send_bubble(
                "That approval doesn't match the pending change — "
                "it may have expired.",
                persist=True,
            )
            return
        await on_code_action("yes", f"button:{session_id}")

    async def on_reject(session_id: str):
        """UI reject button → no-path, session-match checked. While a
        permission ask is open, reject means deny (single pending ask)."""
        st = _code_state
        if st["status"] == "awaiting_permission":
            logger.info(f"[CODE] reject button → deny ({session_id})")
            await on_code_action("deny", f"button:{session_id}")
            return
        if st["status"] != "proposed":
            logger.info(f"[CODE] reject with no proposal → ignored ({session_id})")
            return
        if session_id and session_id != st["session_id"]:
            logger.warning(
                f"[CODE] reject session mismatch: {session_id} "
                f"vs {st['session_id']} → ignored"
            )
            return
        await on_code_action("no", f"button:{session_id}")

    async def on_memory_edit(action: str, msg: dict):
        """UI memory panel edit/delete → store op → refresh push."""
        entry_id = (msg or {}).get("id", "")
        new_text = (msg or {}).get("text", "")
        try:
            result = await apply_memory_edit(
                memory_manager, action, entry_id, new_text
            )
            logger.info(f"[MEM] panel {action} {entry_id}: {result}")
        except Exception as e:
            logger.warning(f"[MEM] panel {action} {entry_id} failed: {e!r}")
            await _code_send_bubble(f"Couldn't update that memory: {e}", persist=True)
        entries = build_memory_entries()
        await emit_to_ui(MemoryFrame(entries))
        logger.info(f"[MEM] MemoryFrame re-emitted ({len(entries)} entries)")

    async def on_worker_permission_response(worker_id: str, decision: str):
        """Worker panel Allow/Deny button → route to the running permission wait."""
        st = _code_state
        if st["status"] != "awaiting_permission":
            logger.info(f"[CODE] worker permission {decision} but no pending ask → ignored")
            return
        if st.get("session_id") != worker_id:
            logger.info(f"[CODE] worker permission {decision} for {worker_id} but pending is {st.get('session_id')} → ignored")
            return
        resolved = "once" if decision == "allow" else "reject"
        st["perm_decision"] = resolved
        if st["perm_event"]:
            st["perm_event"].set()
        logger.info(f"[CODE] worker permission → {resolved} (worker={worker_id})")

    async def emit_to_ui(frame):
        # Bypass the live audio queue: serialize + write directly to the
        # websocket so control frames reach the UI immediately. (Routing via
        # status_relay sent them through the pipeline output transport, which
        # queues non-audio frames behind in-flight TTS audio.)
        payload = await transport.output()._params.serializer.serialize(frame)
        ws = ws_holder.get("ws")
        if payload and ws:
            await ws.send(payload)

    async def on_work_mode(enabled: bool):
        """Toggle work-mode dictation. When ON, transcripts echo to UI but
        skip the coding dialog, session log, and LLM.
        On entry, fetch and push the opencode session list + start activity poller."""
        turn_router.set_work_mode(enabled)
        if enabled:
            _start_worker_poller()
            await _push_projects()
            try:
                raw = we_list_sessions()
                if isinstance(raw, list):
                    now_ms = time.time() * 1000
                    ACTIVE_CUTOFF_MS = 3600_000  # 1 hour
                    MAX_WORKERS = 3
                    sessions = []
                    for s in raw:
                        updated = (s.get("time") or {}).get("updated", 0)
                        if now_ms - updated > ACTIVE_CUTOFF_MS:
                            continue
                        sessions.append({
                            "id": s.get("id", ""),
                            "name": s.get("title") or s.get("agent", "worker"),
                            "agent": s.get("agent", "opencode"),
                            "status": "running",
                        })
                        if len(sessions) >= MAX_WORKERS:
                            break
                    await emit_to_ui(WorkSessionsFrame(sessions=sessions))
                else:
                    await emit_to_ui(WorkSessionsFrame(sessions=[]))
            except Exception as e:
                logger.warning(f"[WORK] session list fetch failed: {e!r}")
                await emit_to_ui(WorkSessionsFrame(sessions=[]))
        else:
            _stop_worker_poller()

    # -- Recent sessions (sidebar 'Recent' list) -------------------------------

    async def on_recent_sessions_get():
        """Push the recent chat sessions to the UI. Called on connect, on
        'new_chat', and when the client requests a refresh."""
        try:
            rows = session_store.list_sessions(limit=12)
        except Exception as e:
            logger.warning(f"[SESSION] recent list failed: {e!r}")
            await emit_to_ui(RecentSessionsFrame(sessions=[]))
            return
        live_id = session_holder.get("id", "")
        sessions = []
        for r in rows:
            sid = r.get("session_id", "")
            if not sid:
                continue
            sessions.append({
                "session_id": sid,
                "title": session_store.session_title(sid, fallback="Chat"),
                "message_count": r.get("message_count") or 0,
                "last_time": r.get("last_time") or r.get("created") or "",
                "is_live": sid == live_id,
            })
        if live_id and not any(s["session_id"] == live_id for s in sessions):
            sessions.insert(0, {
                "session_id": live_id,
                "title": session_store.session_title(live_id, fallback="New chat"),
                "message_count": 0,
                "last_time": "",
                "is_live": True,
            })
        await emit_to_ui(RecentSessionsFrame(sessions=sessions))

    async def on_session_open(session_id: str):
        """Load one stored session's history into the chat panel and resume it
        (A4): adopt the session id and restore recent turns into the LLM
        context so the conversation continues with its history."""
        try:
            if not session_id:
                raise ValueError("empty session id")
            rows = session_store.messages(session_id)
            messages = [{
                "role": r.get("role", "bot"),
                "text": r.get("text", ""),
                "time": r.get("time", ""),
            } for r in rows]
        except Exception as e:
            logger.warning(f"[SESSION] open failed: {e!r}")
            messages = []
            rows = []
        await emit_to_ui(SessionMessagesFrame(messages=messages))
        try:
            session_holder["id"] = session_id
            recent = rows[-20:]
            hist = [{
                "role": "assistant" if (r.get("role") == "bot") else r.get("role", "user"),
                "content": (r.get("text") or "")[:4000],
            } for r in recent if (r.get("text") or "").strip()]
            if hist:
                context.set_messages(hist)
                logger.info(
                    f"[SESSION] resumed {session_id} with {len(hist)} turns "
                    f"into context"
                )
        except Exception as e:
            logger.warning(f"[SESSION] resume into context failed: {e!r}")
        await on_recent_sessions_get()

    async def on_new_chat():
        """Reset the conversation: new session, cleared LLM context, fresh
        welcome. The UI clears itself on click."""
        turn_router.set_work_mode(False)
        try:
            _stop_poller()
        except Exception as e:
            logger.warning(f"[NEWCHAT] stop poller: {e!r}")
        context.set_messages([])
        _agent_state["messages"] = []
        _agent_state["last_files"] = []
        try:
            _save_agent_session(get_repo_root(), [], [])
        except Exception:
            pass
        session_holder["id"] = session_store.new_session({"client": "ws"})
        if MEMORY_ENABLED:
            memory_manager.initialize(
                os.environ.get("SYSTEM_PROMPT", "You are a kind, concise assistant.")
            )
            set_active_system(
                llm,
                build_system_text(
                    os.environ.get("SYSTEM_PROMPT", "You are a kind, concise assistant."),
                    memory_manager.system_instruction_suffix(),
                ),
            )
        elif os.environ.get("SYSTEM_PROMPT", "You are a kind, concise assistant."):
            set_active_system(llm, os.environ.get("SYSTEM_PROMPT", "You are a kind, concise assistant."))
        entries = build_memory_entries()
        await emit_to_ui(MemoryFrame(entries))
        await emit_to_ui(BrainFrame(provider="Local", model=""))
        await emit_to_ui(StatusFrame(state="ready"))
        await on_recent_sessions_get()
        for f in say_welcome(ASSISTANT_NAME):
            await worker.queue_frames([f])

    # -- Project selector (Brain + workers share one active project) ------------

    async def _push_projects():
        act = get_active_project()
        await emit_to_ui(ProjectsFrame(current=act.get("worktree", ""), projects=list_projects()))

    async def on_projects_get():
        await _push_projects()

    async def _push_workers():
        names = {m["id"]: m["name"] for m in _brain_models()}
        labels = {p["id"]: p["name"] for p in _provider_catalog()}
        rows = []
        for i, w in enumerate(list_workers()):
            proj = w.get("project") or get_repo_root()
            prov = w.get("provider") or "opencode"
            rows.append({
                "index": i,
                "name": worker_name(i),
                "project": proj,
                "project_name": os.path.basename(os.path.normpath(proj)) if proj else "",
                "provider": prov,
                "provider_name": labels.get(prov, prov),
                "model": w.get("model") or "",
                "model_name": names.get(w.get("model"), w.get("model") or ""),
                "reasoning": w.get("reasoning", "default"),
            })
        await emit_to_ui(WorkersFrame(workers=rows))

    def _agents_status_text() -> str:
        """Compact text of the live agent tasks (agent, title, status, elapsed,
        last note, verdict/reasons). "" when nothing is live."""
        try:
            from tasks import tasks as _tasks

            rows = _tasks.snapshot()
        except Exception:  # noqa: BLE001
            return "no agents working"
        if not rows:
            return "no agents working"
        lines = []
        for r in rows:
            line = (f"- {r['agent_name'] or r['agent_id']} [{r['status']}] "
                    f"{r.get('label') or r['title'] or '(untitled)'} "
                    f"({int(r['seconds'])}s)")
            if r["verdict"]:
                line += f" | verdict: {r['verdict']}"
            if r["reasons"]:
                line += " | " + "; ".join(str(x) for x in r["reasons"][:3])
            lines.append(line)
        return "\n".join(lines)

    def _team_tool(args) -> str:
        """Handle the team tool: list, counts, hire, confirm, retire."""
        from agents import registry
        action = (args.get("action") or "list").strip().lower()
        if action == "list":
            rows = registry.snapshot()
            if not rows:
                return "No agents in the team."
            lines = []
            for r in rows:
                resp = ", ".join(r.get("responsibilities") or [])
                lines.append(
                    f"- {r['name']} - {r['title']} - {resp} [{r['status']}]")
            return "\n".join(lines)
        if action == "counts":
            counts = registry.team_counts()
            if not counts:
                return "No agents."
            return "\n".join(f"- {t}: {c}" for t, c in sorted(counts.items()))
        if action == "hire":
            name = (args.get("name") or "").strip()
            title = (args.get("title") or "").strip()
            resp = [r.strip() for r in (args.get("responsibilities") or [])
                    if r.strip()]
            if not name or not title or not resp:
                return ("Error: hire requires name, title, and at least one "
                        "responsibility.")
            try:
                proposal = registry.hire(name, title, resp)
                registry.pending_proposal = proposal
                return (f"New agent - Name: {name}, Title: {title}, "
                        f"Responsibilities: {', '.join(resp)}. "
                        "Say yes and I will add them to the team.")
            except ValueError as e:
                return f"Error: {e}"
        if action == "confirm":
            proposal = registry.pending_proposal
            if not proposal:
                return "Error: no pending hire proposal to confirm."
            try:
                registry.confirm(proposal)
                registry.pending_proposal = None
                return f"{proposal['name']} has been added to the team."
            except Exception as e:  # noqa: BLE001
                return f"Error confirming: {e}"
        if action == "retire":
            name = (args.get("name") or "").strip()
            if not name:
                return "Error: retire requires the agent's name."
            aid = name.lower().replace(" ", "")
            if registry.retire(aid):
                return f"{name} has been retired from the team."
            return f"Error: no agent named {name!r}."
        return f"Error: unknown team action {action!r}"

    async def _push_agents():
        from agents import registry
        snap = registry.snapshot()
        try:
            from tasks import tasks as _tasks

            task_rows = _tasks.snapshot()
        except Exception:  # noqa: BLE001
            task_rows = []
        await emit_to_ui(AgentsFrame(agents=snap, running=registry.running_count(),
                                     total=len(snap), tasks=task_rows))

    _PROVIDER_LABELS = {
        "opencode": "OpenCode Zen",
        "opencode-go": "OpenCode Go",
        "openrouter": "OpenRouter",
        "openai": "OpenAI",
        "anthropic": "Anthropic",
        "gemini": "Gemini",
        "groq": "Groq",
        "xai": "xAI",
    }

    def _provider_catalog() -> list[dict]:
        """Every provider a worker may use, with its models (opencode from the
        live CLI list; the rest from the provider catalog)."""
        out: list[dict] = []
        try:
            oc = _oc_models_cached()
        except Exception:
            oc = []
        zen: list[dict] = []
        go: list[dict] = []
        for m in oc:
            row = {"id": m["id"], "name": m.get("name") or m["id"],
                   "free": bool(m.get("free"))}
            (go if m.get("tier") == "go" else zen).append(row)
        out.append({"id": "opencode", "name": "OpenCode Zen", "models": zen})
        out.append({"id": "opencode-go", "name": "OpenCode Go", "models": go})
        for pid in ("openrouter", "openai", "anthropic", "gemini", "groq", "xai"):
            try:
                ms = [
                    {"id": (m.get("id") if isinstance(m, dict) else getattr(m, "id", "")),
                     "name": (m.get("name") if isinstance(m, dict) else getattr(m, "name", ""))}
                    for m in providers_pkg.list_models(pid)
                ]
            except Exception:
                ms = []
            out.append({"id": pid, "name": _PROVIDER_LABELS[pid], "models": ms})
        return out

    async def on_catalog_get():
        await emit_to_ui(CatalogFrame(providers=_provider_catalog()))

    async def on_workers_get():
        await _push_workers()
        await _push_agents()

    async def on_worker_set(msg: dict):
        try:
            idx = int(msg.get("index"))
        except (TypeError, ValueError):
            return
        try:
            await asyncio.to_thread(
                set_worker, idx,
                project=msg.get("project"),
                provider=msg.get("provider"),
                model=msg.get("model"),
                reasoning=msg.get("reasoning"),
            )
            if msg.get("project"):
                await asyncio.to_thread(mark_project_chosen)
        except Exception as e:
            logger.warning(f"[WORKER] set failed ({idx}): {e!r}")
        await _push_workers()
        await _push_agents()

    async def on_project_set(path: str):
        """UI project-set: reselect the system-wide repo root."""
        st = _code_state
        if st["status"] != "idle":
            try:
                await _code_send_bubble("Finish the running task before switching projects.")
            except Exception:
                pass
            return
        def _do_set():
            global _REPO_ROOT
            p = set_active_project(path)
            _REPO_ROOT = Path(p["worktree"])
            return p
        try:
            project = await asyncio.to_thread(_do_set)
        except Exception as e:
            logger.warning(f"[CODE] project_set failed: {e!r}")
            try:
                await _code_send_bubble(f"Couldn't open that folder: {e}", persist=True)
            except Exception:
                pass
            return
        logger.info(f"[CODE] active project → {project.get('worktree')}")
        # New project → resume that project's agent session if one exists.
        try:
            _agent_state["messages"], _agent_state["last_files"] = \
                _load_agent_session(project.get("worktree") or "")
        except Exception:
            _agent_state["messages"] = []
            _agent_state["last_files"] = []
        await _push_projects()
        # Phase 3: if a coding request was waiting on a project, run it now.
        pending = st.get("pending_request")
        if pending:
            st["pending_request"] = None
            logger.info("[CODE] running the request that was waiting on a project")
            asyncio.get_event_loop().create_task(_run_agent_task(pending))

    async def on_fs_list(msg: dict):
        """Folder-picker: list one directory (sandboxed to home + /Volumes)."""
        path = (msg.get("path") or "").strip()
        try:
            data = await asyncio.to_thread(folder_picker.list_dir, path)
            logger.info(f"[PICKER] listing {data['path']} ({len(data['entries'])} folders)")
            await emit_to_ui(FsListingFrame(
                path=data["path"], parent=data["parent"],
                entries=data["entries"], error="",
            ))
        except Exception as e:
            logger.info(f"[PICKER] list failed ({path}): {e}")
            await emit_to_ui(FsListingFrame(path=path, parent="", entries=[], error=str(e)))

    async def on_fs_native():
        """Folder-picker: open the macOS 'choose folder' dialog."""
        try:
            path = await asyncio.to_thread(folder_picker.choose_native_folder)
            await emit_to_ui(FsNativeResultFrame(path=path, error=""))
        except Exception as e:
            logger.info(f"[PICKER] native dialog failed: {e}")
            await emit_to_ui(FsNativeResultFrame(path="", error=str(e)))

    async def on_brain_provider(provider: str):
        """Accept brain provider switch from UI Settings, or serve usage stats.
        Off the voice hot path (KB-15): config message, not pipeline."""
        if provider == "__usage__":
            usage = supervisor.daily_usage()
            ws = ws_holder.get("ws")
            if ws:
                await ws.send(json.dumps({"type": "brain_usage", **usage}))
            return
        try:
            await asyncio.to_thread(supervisor.set_provider_override, provider)
            transport = "opencode"
            ws = ws_holder.get("ws")
            if ws:
                await ws.send(json.dumps({"type": "brain_provider_ok", "transport": transport}))
        except Exception as e:
            logger.warning(f"[BRAIN] provider override failed: {e!r}")
            ws = ws_holder.get("ws")
            if ws:
                await ws.send(json.dumps({"type": "brain_provider_error", "error": str(e)}))

    async def on_provider_config(msg: dict):
        """Get or set the provider layer config (brain + worker provider/model).

        Wire:
          UI -> server: {"type":"provider_config_get"}
                           or {"type":"provider_config_set", "brain_provider":..., "worker_model":...}
          server -> UI: {"type":"provider_config_ok", "config": {...}, "providers": [...]}
        """
        ws = ws_holder.get("ws")
        try:
            if msg.get("type") == "provider_config_set":
                await asyncio.to_thread(
                    _provider_cfg_update, msg,
                )
            config = providers_config().to_dict()
            provider_rows = _ui_provider_rows()
            if ws:
                await ws.send(json.dumps({
                    "type": "provider_config_ok",
                    "config": config,
                    "providers": provider_rows,
                }))
        except Exception as e:
            logger.warning(f"[PROVIDERS] config failed: {e!r}")
            if ws:
                await ws.send(json.dumps({
                    "type": "provider_config_error",
                    "error": str(e),
                }))

    def _brain_models():
        """All deployable opencode models (zen + go) — sent to clients for
        display/back-compat only. The brain itself is hardwired (2026-09-17)
        and never picks from this list. ids stay the real wire ids; names are
        friendly labels + a Free/Go tag a normal person understands."""
        brand = {
            "gpt": "GPT", "glm": "GLM", "qwen": "Qwen", "claude": "Claude",
            "deepseek": "DeepSeek", "gemini": "Gemini", "grok": "Grok",
            "kimi": "Kimi", "mimo": "MiMo", "minimax": "MiniMax",
            "nemotron": "Nemotron", "ling": "Ling", "spark": "Spark",
            "opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku",
            "codex": "Codex", "astra": "Astra",
        }

        def humanize(mid: str) -> str:
            words: list[str] = []
            for part in mid.split("-"):
                if not part:
                    continue
                if part in brand:
                    words.append(brand[part])
                elif part == "free":
                    words.append("Free")
                elif part == "exp":
                    words.append("Exp")
                elif part[0].isdigit() and words and words[-1].replace(".", "").isdigit():
                    words[-1] = words[-1] + "." + part
                else:
                    words.append(part[:1].upper() + part[1:])
            return " ".join(words)

        try:
            out = []
            for m in _oc_models_cached():
                mid, tier = m["id"], m.get("tier", "free")
                if not mid or any(o.get("id") == mid for o in out):
                    continue
                name = m.get("name") or ""
                if not name or name == mid:
                    name = humanize(mid)
                out.append({
                    "id": mid,
                    "name": name,
                    "group": "OpenCode Go" if tier == "go" else "OpenCode Zen",
                    "free": bool(m.get("free")),
                })
            # BYOK providers (OpenAI, Anthropic, OpenRouter, custom, …) — ids
            # are 'provider|model' so they never collide with opencode ids.
            seen = {o["id"] for o in out}
            for pid, label, models in _byok_model_sources():
                for m in models:
                    mid = m.get("id") if isinstance(m, dict) else getattr(m, "id", "")
                    if not mid:
                        continue
                    cid = f"{pid}|{mid}"
                    if cid in seen:
                        continue
                    name = (m.get("name") if isinstance(m, dict) else "") or mid
                    out.append({"id": cid, "name": name, "group": label, "free": False})
            return out
        except Exception as e:
            logger.warning(f"[BRAINPREF] model list failed: {e!r}")
            return []

    def _brain_model_display(model_id: str) -> str:
        """Friendly name for a brain model id (falls back to the id)."""
        if not model_id:
            return "no model"
        for m in _brain_models():
            if m["id"] == model_id:
                return m["name"]
        return model_id

    async def _push_brain_config():
        pref = brain_pref_holder
        await emit_to_ui(BrainConfigFrame(
            model=pref.get("model"),
            reasoning=pref.get("reasoning", "default"),
            models=_brain_models(),
            key_present=bool(os.environ.get("OPENCODE_API_KEY")),
            onboarded=bool(pref.get("onboarded")),
        ))
        # Keep the Work-view "Brain" strip honest: show the SELECTED model,
        # not the stale hardcoded placeholder.
        await _emit_brain_status(f"{_brain_model_display(pref.get('model'))} — idle")

    async def on_brain_model_set(msg: dict):
        """Accept-and-ignore: the brain is hardwired (2026-09-17), so a model
        choice from an older client must not change what runs.

        Wire (kept for backward compatibility — older clients still send it):
          UI -> server: {"type":"brain_model_set", "model":"mimo-v2.5-free"}
          server -> UI: {"type":"brain_config","model":...,"reasoning":...,
                          "models":[...], "key_present":...}
        Re-asserts the constant, persists it (harmless — load/save force it
        anyway), and pushes the config so the old client stays in sync."""

        pref = brain_pref_holder
        wanted = (msg.get("model") or "").strip()
        pref["model"] = BRAIN_MODEL_ID
        _save_brain_pref(pref)
        llm.set_brain_model(pref["model"])
        logger.info(f"[BRAINPREF] brain_model_set ignored (hardwired "
                    f"{BRAIN_MODEL_ID}; client sent {wanted!r})")
        await _push_brain_config()

    async def on_reasoning_set(msg: dict):
        """Accept-and-ignore: reasoning is hardwired to the model default
        (2026-09-17), so a level from an older client must not change the
        model call.

        Wire (kept for backward compatibility — older clients still send it):
          UI -> server: {"type":"reasoning_set","level":"medium"}
          server -> UI: {"type":"brain_config","model":...,"reasoning":...}
        Re-asserts the default (field omitted → the model decides), persists
        it so a stale pref can never re-enable it, and pushes the config so
        the old client stays in sync."""
        pref = brain_pref_holder
        wanted = (msg.get("level") or "").strip().lower()
        llm._settings.extra = _reasoning_extra()
        pref["reasoning"] = HARDWIRED_REASONING
        _save_brain_pref(pref)
        logger.info(f"[BRAINPREF] reasoning_set ignored (hardwired default; "
                    f"client sent {wanted!r})")
        await _push_brain_config()

    async def on_onboarding_done():
        """Notice-3 OK: the user finished onboarding — mark it done, persist,
        and fire the greeting. This is the ONLY place onboarding completes, so
        a reload before the mic OK resumes the flow instead of skipping it."""
        brain_pref_holder["onboarded"] = True
        _save_brain_pref(brain_pref_holder)
        await _push_brain_config()
        if _greeted[0]:
            logger.info("[BRAINPREF] onboarding_done — already greeted this session")
            return
        _greeted[0] = True
        logger.info("[BRAINPREF] onboarding complete — greeting")
        for f in say_welcome(ASSISTANT_NAME):
            await worker.queue_frames([f])

    async def on_provider_key(msg: dict):
        """Save (or clear) an API key from the settings UI.

        Wire:
          UI -> server: {"type":"provider_key_set", "env":"OPENAI_API_KEY", "value":"sk-..."}
          server -> UI: {"type":"provider_key_ok",   "env":..., "set":bool, "validated":bool|None}
                          or {"type":"provider_key_error", "env":..., "error":"..."}
        The key is persisted to .env, providers rebuild, and the UI advances
        immediately (the model list is cached, so it is fast). A best-effort
        live ping validates the key afterward and reports a follow-up
        provider_key_ok, so the user never waits on the network.
        """
        ws = ws_holder.get("ws")
        env = (msg.get("env") or "").strip()
        value = (msg.get("value") or "").strip()
        if not ws:
            return
        try:
            saved = await asyncio.to_thread(_provider_key_save, env, value)
        except Exception as e:
            logger.warning(f"[PROVIDERS] key save failed ({env}): {e!r}")
            await ws.send(json.dumps({"type": "provider_key_error", "env": env, "error": str(e)}))
            return

        # Rebuild providers (needed before a model can be selected) and tell
        # the UI right away — no waiting on the validation ping.
        if env == "OPENCODE_API_KEY" and saved.get("set"):
            await asyncio.to_thread(llm.rebuild_providers)
        await ws.send(json.dumps({
            "type": "provider_key_ok",
            "env": env,
            "set": saved["set"],
            "validated": None,
            "detail": "",
        }))
        # Push the refreshed configured/flags to the settings panel.
        config = providers_config().to_dict()
        provider_rows = _ui_provider_rows()
        await ws.send(json.dumps({
            "type": "provider_config_ok",
            "config": config,
            "providers": provider_rows,
        }))
        # Fresh brain_config so the client advances to the model notice.
        await _push_brain_config()

        # Best-effort live validation: one token from the default model. Sent
        # as a second provider_key_ok so it only updates status, not the flow.
        if saved["set"] and saved["validator"]:
            validated = None
            detail = ""
            try:
                validated = await asyncio.to_thread(
                    _provider_key_validate, saved["validator"], 12.0,
                )
            except Exception as e:
                detail = str(e)[:200]
            await ws.send(json.dumps({
                "type": "provider_key_ok",
                "env": env,
                "set": True,
                "validated": validated,
                "detail": detail,
            }))

    async def on_provider_custom(msg: dict):
        """Add/remove a user-defined OpenAI-compatible provider (BYOK).

        Wire:
          UI -> server: {"type":"provider_custom","action":"add","name":...,
                         "base_url":...,"api_key":...,"models":[...]}
                        or {"type":"provider_custom","action":"remove","id":...}
          server -> UI: {"type":"provider_custom_ok","providers":[...]}
                        or {"type":"provider_custom_error","error":"..."}
        """
        ws = ws_holder.get("ws")
        if not ws:
            return
        action = (msg.get("action") or "").strip().lower()
        store = providers_pkg.custom_providers()
        try:
            if action == "add":
                models = msg.get("models")
                if isinstance(models, str):
                    models = [m for m in re.split(r"[,\n]+", models) if m.strip()]
                await asyncio.to_thread(store.add, msg.get("name", ""),
                                        msg.get("base_url", ""),
                                        msg.get("api_key", ""), models or [])
            elif action == "remove":
                await asyncio.to_thread(store.remove, (msg.get("id") or "").strip())
            else:
                raise ValueError(f"unknown action: {action!r}")
            providers_pkg.reset_cache()
            await ws.send(json.dumps({
                "type": "provider_custom_ok",
                "providers": _ui_provider_rows(),
                "config": providers_config().to_dict(),
            }))
        except Exception as e:
            logger.warning(f"[PROVIDERS] custom {action} failed: {e!r}")
            await ws.send(json.dumps({
                "type": "provider_custom_error",
                "error": str(e),
            }))

    async def on_mcp(msg: dict):
        """MCP screen: list / add / remove / test servers; search the registry.

        Delegates to mcp_screen (kept audio-free so it is testable/reusable).
        """
        ws = ws_holder.get("ws")
        if not ws:
            return

        async def _send(payload: dict):
            await ws.send(json.dumps(payload))

        # The Projects screen rides the same incoming "mcp" channel (proto.py is
        # not ours to edit); "screen":"projects" or a board action routes to
        # board_screen, exactly as MCP actions route to mcp_screen.
        if ((msg.get("screen") or "").strip().lower() == "projects"
                or str(msg.get("action") or "").strip().lower() in _BOARD_ACTIONS):
            await board_screen.handle(msg, _send, board_mod.board)
            return

        await mcp_screen.handle(msg, _send, _MCP, project_dir=get_repo_root())

    async def on_user_stop():
        """User pressed Stop: cancel the in-flight voice turn (thinking or
        speaking) by broadcasting an interruption, then stop any coding work."""
        try:
            from pipecat.frames.frames import InterruptionFrame
            await worker.queue_frames([InterruptionFrame()])
            logger.info("[STOP] interrupted current turn")
        except Exception as e:
            logger.warning(f"[STOP] interruption failed: {e!r}")
        if _agent_state.get("running"):
            _agent_state["cancel"].set()
            logger.info("[STOP] cancelling running agent task")
        try:
            await on_code_action("stop", "")
        except Exception as e:
            logger.warning(f"[STOP] code stop failed: {e!r}")
        try:
            await emit_to_ui(StatusFrame(state="ready"))
        except Exception:
            pass

    transport = SingleClientWebsocketServerTransport(
        params=SingleClientWebsocketServerParams(
            serializer=UIFrameSerializer(
                text_cb=on_text,
                stop_cb=on_user_stop,
                approve_cb=on_approve,
                reject_cb=on_reject,
                permission_cb=None,
                worker_permission_cb=on_worker_permission_response,
                memory_cb=on_memory_edit,
                work_mode_cb=on_work_mode,
                project_set_cb=on_project_set,
                projects_cb=on_projects_get,
                fs_list_cb=on_fs_list,
                fs_native_cb=on_fs_native,
                workers_get_cb=on_workers_get,
                worker_set_cb=on_worker_set,
                catalog_cb=on_catalog_get,
                orchestrate_cb=on_orchestrate,
                brain_provider_cb=on_brain_provider,
                provider_config_cb=on_provider_config,
                provider_key_cb=on_provider_key,
                provider_custom_cb=on_provider_custom,
                mcp_cb=on_mcp,
                recent_sessions_cb=on_recent_sessions_get,
                new_chat_cb=on_new_chat,
                session_open_cb=on_session_open,
                brain_model_set_cb=on_brain_model_set,
                reasoning_set_cb=on_reasoning_set,
                onboarding_done_cb=on_onboarding_done,
            ),
            add_wav_header=True,
            audio_in_enabled=True,
            audio_out_enabled=True,
            allowed_origins=[],
        ),
        host=WS_HOST,
        port=WS_PORT,
    )

    stt = MoonshineSTTService(
        settings=MoonshineSTTService.Settings(
            model=os.environ.get("STT_MODEL", "small-streaming"),
            language=os.environ.get("STT_LANGUAGE", "en"),
        )
    )
    tts = GuardedKokoroTTSService(
        settings=KokoroTTSService.Settings(voice=os.environ.get("TTS_VOICE", "bm_george")),
        # Bundled builds point these at shipped model files; unset => pipecat's
        # normal cached download (dev / self-host).
        model_path=os.environ.get("KOKORO_MODEL_PATH") or None,
        voices_path=os.environ.get("KOKORO_VOICES_PATH") or None,
    )
    tts_holder["tts"] = tts
    # Warm Kokoro off the critical path: the first synthesis in a fresh process
    # pays one-time ONNX setup. Fire-and-forget; readiness is not delayed.
    # Keep a strong ref: the loop holds only a weak one, so without this the
    # warm-up Task could be GC'd mid-flight.
    tts_holder["warm_task"] = asyncio.create_task(_warm_kokoro_tts(tts))
    brain_pref = _load_brain_pref()
    # Hardwired reasoning (2026-09-17): always the model default — the saved
    # pref and REASONING_EFFORT in .env are both ignored, so a stale value
    # can never re-enable a non-default effort. _reasoning_extra() is {} —
    # the field is omitted from every model call and the model decides.
    llm = BoundedContextLLM(
        api_key=os.environ.get("LLM_API_KEY", "local"),
        base_url=os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8080/v1"),
        emit_brain_cb=emit_to_ui,
        emit_ui_cb=emit_to_ui,
        settings=OpenAILLMService.Settings(
            system_instruction=os.environ.get(
                "SYSTEM_PROMPT",
                "You are a kind, concise assistant in a voice conversation.",
            ),
            max_tokens=int(os.environ.get("LLM_MAX_TOKENS", "32000")),
            temperature=float(os.environ.get("LLM_TEMPERATURE", "0.7")),
            top_p=float(os.environ.get("LLM_TOP_P", "0.8")),
            extra=_reasoning_extra(),
        ),
    )
    # Hardwired brain (2026-09-17): the model is always BRAIN_MODEL_ID —
    # set_brain_model ignores its argument and pins the constant. The
    # provider for it was built above; when it is missing (e.g. no API key)
    # turns fail plainly instead of falling back to something else.
    llm.set_brain_model(brain_pref.get("model"))
    brain_pref_holder.update(brain_pref)
    # Hardwired reasoning (2026-09-17): a stale file value must never survive
    # — normalize the holder (in memory and on disk) to the default.
    brain_pref_holder["reasoning"] = HARDWIRED_REASONING
    _save_brain_pref(brain_pref_holder)
    # Greeting guard: this connection greets once — either on connect (model
    # already chosen) or on onboarding_done (notice-3 OK).
    _greeted = [bool(brain_pref.get("model"))]

    async def _run_generate_image(args):
        prompt = args.get("prompt", "")
        logger.info(f"[IMAGE] generate_image called: {prompt[:80]!r}")
        result = await handle_generate_image(dict(args))
        if result.startswith("/generated/"):
            caption = f"Here's your image: {prompt[:120]}"
            await emit_to_ui(BotImageFrame(url=result, caption=caption))
            result = (
                f"Image ready and shown in the chat ({result}). "
                f"Describe it briefly for voice in one short sentence."
            )
        logger.info(f"[IMAGE] result: {result[:200]}")
        return result

    def _video_callbacks(reg_id):
        """Progress + completion callbacks for one background video job."""
        import mpt_video_tool

        from tasks import tasks as _tasks

        async def _progress(note):
            if reg_id:
                try:
                    _tasks.update(reg_id, note=note)
                except Exception:  # noqa: BLE001
                    pass

        async def _done(ok, detail, top):
            if reg_id:
                try:
                    _tasks.finish(reg_id, ok, note=detail[:200])
                    if ok:
                        _tasks.verdict(reg_id, "verified", reasons=[])
                        _tasks.remove(reg_id)
                except Exception:  # noqa: BLE001
                    pass
            if ok:
                content = (
                    mpt_video_tool.completion_line(top, detail)
                    + " Tell the user in one short spoken sentence that it is "
                    "ready; do not read the file path aloud."
                )
            else:
                content = (
                    f"The video about {top} could not be made: {detail}. "
                    "Tell the user plainly and briefly, in your own voice."
                )
            try:
                context.add_message({"role": "system", "content": content})
            except Exception:  # noqa: BLE001
                pass
            try:
                await worker.queue_frames([LLMRunFrame()])
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[VIDEO] report run failed: {e!r}")

        return _progress, _done

    _resumed_videos: set = set()

    async def _resume_videos():
        """Pick up renders persisted by a previous Jarvis run and keep polling."""
        import mpt_video_tool

        from tasks import tasks as _tasks

        for job in mpt_video_tool.pending_jobs():
            tid = (job or {}).get("task_id") or ""
            topic = (job or {}).get("topic") or "your video"
            if not tid or tid in _resumed_videos:
                continue
            _resumed_videos.add(tid)
            try:
                reg_id = _tasks.create("video", f"resumed video: {topic}", files=[])
            except Exception:  # noqa: BLE001
                reg_id = ""
            progress, done = _video_callbacks(reg_id)
            try:
                asyncio.get_event_loop().create_task(
                    mpt_video_tool.watch(tid, topic, done, on_progress=progress))
                logger.info(f"[VIDEO] resumed job {tid} ({topic[:60]!r})")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[VIDEO] resume failed: {e!r}")

    async def _run_make_video(args):
        """Queue a background video and return an immediate ack.

        MPT is an optional local add-on: when it is not running this is a
        graceful "not installed" answer, never an error."""
        import mpt_video_tool

        from tasks import tasks as _tasks

        topic = (args.get("topic") or "").strip()
        if not topic:
            return f"{mpt_video_tool._ERROR_PREFIX} what should the video be about?"
        logger.info(f"[VIDEO] make_video called: {topic[:80]!r}")
        reg_id = ""
        try:
            reg_id = _tasks.create("video", f"making video: {topic}", files=[])
        except Exception:  # noqa: BLE001
            pass
        _progress, _done = _video_callbacks(reg_id)
        try:
            result = await mpt_video_tool.make_video(
                topic,
                aspect=(args.get("aspect") or "9:16"),
                voice=(args.get("voice") or "en-US-AriaNeural"),
                script=(args.get("script") or ""),
                terms=(args.get("terms") or ""),
                on_done=_done,
                on_progress=_progress,
            )
        except Exception as e:  # noqa: BLE001
            result = f"{mpt_video_tool._ERROR_PREFIX} {e}"
        logger.info(f"[VIDEO] result: {result[:200]}")
        if result != mpt_video_tool.ACK_MSG and reg_id:
            try:
                _tasks.finish(reg_id, False, note=result[:200])
                _tasks.remove(reg_id)
            except Exception:  # noqa: BLE001
                pass
        return result

    async def _watch_delegate(agent_id, name, run_obj):
        """Wait for a delegated agent, then hand its result to the brain.

        The result arrives as a system message (never as a chat message from
        the agent) and the brain is nudged to verify it and speak in its own
        voice."""
        try:
            rc = await asyncio.to_thread(run_obj.result)
        except Exception:  # noqa: BLE001
            rc = 1
        try:
            await _push_agents()
        except Exception:  # noqa: BLE001
            pass
        detail = run_obj.tail(12) or "(no output)"
        status = "finished" if rc == 0 else "failed"
        try:
            context.add_message({
                "role": "system",
                "content": (
                    f"Delegated work for agent {name} {status} (exit {rc}). "
                    f"Result tail:\n{detail[:3000]}\n"
                    "Verify it against the brief yourself before you tell the "
                    "user; the user never sees the agent."),
            })
        except Exception:  # noqa: BLE001
            pass
        try:
            await worker.queue_frames([LLMRunFrame()])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DELEGATE] report run failed: {e!r}")

    async def _delegate_tool(args):
        """Launch one permanent agent off the voice hot path; return a short ack.

        Voice turns never block here: the opencode CLI run is backgrounded and
        watched in its own task, and the agent's result is delivered to the
        brain afterwards (see ``_watch_delegate``)."""
        import agent_runner
        import supervisor_loop
        from agents import registry
        goal = (args.get("goal") or "").strip()
        if not goal:
            return "Error: delegate needs a goal."
        files = [str(f).strip() for f in (args.get("files") or []) if str(f).strip()]
        do_not = [str(x).strip() for x in (args.get("do_not") or []) if str(x).strip()]
        verify = agent_loop.normalize_verify(args.get("verify"))
        agent_id = (args.get("agent") or "").strip() or registry.pick(
            args.get("title") or "")
        model = (args.get("model") or "").strip()
        row = next((r for r in registry.snapshot() if r["id"] == agent_id), None)
        name = (row or {}).get("name") or agent_id
        brief = agent_runner.build_brief(goal=goal, files=files, do_not=do_not,
                                         verify=verify)
        try:
            workdir = get_repo_root()
        except Exception:  # noqa: BLE001
            workdir = str(_REPO_ROOT)

        def _check_now(_agent_id, _rc, _note, _run):
            # Called once from the watcher thread the moment the run settles:
            # wake the verification loop now instead of on its next poll.
            try:
                supervisor_loop.loop.wake()
            except Exception:  # noqa: BLE001
                pass

        try:
            supervisor_loop.loop.start()  # idempotent; a delegate implies a loop
        except Exception:  # noqa: BLE001
            pass

        try:
            run_obj = await asyncio.to_thread(
                agent_runner.run, agent_id, brief, model=model, workdir=workdir,
                title=goal.splitlines()[0][:80], files=files,
                on_done=_check_now)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DELEGATE] launch failed: {e!r}")
            return f"Could not hand that to {name}: {e}"
        try:
            await _push_agents()
        except Exception:  # noqa: BLE001
            pass
        if run_obj.state != "running":
            return f"{name} is not available: {run_obj.note or 'not launched'}"
        asyncio.get_event_loop().create_task(
            _watch_delegate(agent_id, name, run_obj))
        return (f"handed to {name} ({run_obj.model}); log: {run_obj.log_path}. "
                "This may take a while — its result will come back to you; keep "
                "talking to the user.")

    async def _exec_text_tool(name, args):
        """Single source of truth for the voice-tier tools. Both the native
        pipecat handlers and MiMo's text-format <tool_call> fallback round run
        through here."""
        # Loop guard: block the same (tool, args) after 3 repeats.
        try:
            key = (name, json.dumps(args, sort_keys=True, default=str)[:500])
        except Exception:
            key = (name, str(args)[:500])
        _TOOL_CALL_LOG.append(key)
        if len(_TOOL_CALL_LOG) > 40:
            del _TOOL_CALL_LOG[:-40]
        if _TOOL_CALL_LOG.count(key) >= 4:
            logger.warning(f"[LLM] repeat-tool guard tripped: {name} {key[1][:120]}"            )
            return (
                f"You have already called {name} with these exact arguments several "
                "times. Stop repeating it — use the result you already have and "
                "continue the task (make the edit or write the file)."
            )
        _llm = llm_holder.get("llm")
        if _llm is not None:
            _llm._turn_tools += 1
            if name in ("write_file", "edit_file", "apply_patch"):
                _llm._turn_writes += 1
        detail = ""
        if isinstance(args, dict):
            for k in ("query", "url", "command", "path", "pattern"):
                if k in args and args[k]:
                    detail = f"{k}={str(args[k])[:60]}"
                    break
        try:
            await emit_to_ui(BrainActivityFrame(phase="tool", tool=name, detail=detail))
        except Exception:
            logger.warning("[LLM] tool activity emit failed", exc_info=True)
        if name == "web_search":
            query = args.get("query", "")
            logger.info(f"[SEARCH] web_search called: {query}")
            result = await web_search(query)
            logger.info(f"[SEARCH] result: {result[:200]}")
            return result
        if name == "file_search":
            query = args.get("query", "")
            path = args.get("path", "")
            logger.info(f"[FILE] file_search called: query={query!r} path={path!r}")
            result = await file_search(query, path)
            logger.info(f"[FILE] result: {result[:200]}")
            return result
        if name == "read_file":
            path = args.get("path", "")
            offset = int(args.get("offset", 0) or 0)
            limit = int(args.get("limit", 0) or 0)
            logger.info(f"[FILE] read_file called: {path!r} offset={offset} limit={limit}")
            result = await read_file(path, offset, limit)
            logger.info(f"[FILE] result: {result[:200]}")
            return result
        if name == "write_file":
            path = args.get("path", "")
            content = args.get("content", "")
            logger.info(f"[CODING] write_file called: {path!r} ({len(content)} chars)")
            result = await write_file(path, content)
            logger.info(f"[CODING] result: {result[:200]}")
            return result
        if name == "edit_file":
            path = args.get("path", "")
            old_string = args.get("old_string", "")
            new_string = args.get("new_string", "")
            logger.info(f"[CODING] edit_file called: {path!r}")
            result = await edit_file(path, old_string, new_string)
            logger.info(f"[CODING] result: {result[:200]}")
            return result
        if name == "run_bash":
            command = args.get("command", "")
            timeout_sec = args.get("timeout_sec", 30)
            logger.info(f"[CODING] run_bash called: {command!r}")
            result = await run_bash(command, timeout_sec)
            logger.info(f"[CODING] result: {result[:200]}")
            return result
        if name == "list_files":
            pattern = args.get("pattern", "**/*")
            logger.info(f"[CODING] list_files called: {pattern!r}")
            result = await list_files(pattern)
            logger.info(f"[CODING] result: {result[:200]}")
            return result
        if name == "grep":
            pattern = args.get("pattern", "")
            path = args.get("path", "")
            glob = args.get("glob", "")
            logger.info(f"[CODING] grep called: {pattern!r} path={path!r} glob={glob!r}")
            result = await grep(pattern, path, glob)
            logger.info(f"[CODING] result: {result[:200]}")
            return result
        if name == "todo":
            logger.info(f"[CODING] todo called ({len(args.get('todos') or [])} items)")
            return todo_update(args.get("todos") or [])
        if name == "question":
            q = (args.get("question") or "").strip()
            logger.info(f"[CODING] question: {q[:120]!r}")
            if not q:
                return "Error: empty question"
            await _code_send_bubble(q)
            try:
                await _code_speak(q, tts_holder)
            except Exception:
                pass
            return "Asked the user the question; waiting for their reply."
        if name == "search_sessions":
            q = (args.get("query") or "").strip()
            logger.info(f"[SESSION] search_sessions: {q[:120]!r}")
            if not q:
                return "Error: empty query"
            try:
                rows = session_store.search(q, limit=int(args.get("limit", 8) or 8))
            except Exception as e:
                return f"Error searching sessions: {e}"
            if not rows:
                return f"No past messages matching {q!r}"
            return "\n".join(
                f"[{session_store.session_title(r['session_id'], fallback='chat')}] "
                f"{r['role']}: {(r['text'] or '')[:200]}"
                for r in rows
            )
        if name == "repo_map":
            path = args.get("path", "")
            result = repo_map(path, int(args.get("max_entries", 200) or 200))
            logger.info(f"[CODING] repo_map: {path!r} ({len(result)} chars)")
            return result
        if name == "mcp_list_tools":
            server = args.get("server") or None
            tools = _MCP.list_tools(server)
            logger.info(f"[MCP] list_tools({server!r}) -> {len(tools)}")
            if not tools:
                return "No MCP servers configured."
            lines = []
            for t in tools:
                if t.get("error"):
                    lines.append(f"{t.get('server')}: ERROR {t['error']}")
                else:
                    lines.append(
                        f"{t['server']}.{t.get('name', '')}: {t.get('description', '')}"
                    )
            return "\n".join(lines)
        if name == "mcp_call_tool":
            server = (args.get("server") or "").strip()
            tool = (args.get("tool") or "").strip()
            logger.info(f"[MCP] call {server}.{tool}")
            if not server or not tool:
                return "Error: server and tool are required"
            try:
                res = _MCP.call_tool(server, tool, args.get("arguments") or {})
            except Exception as e:
                return f"Error calling MCP tool: {e}"
            txt = mcp_mod.tool_text(res)
            return txt or "(no text content)"
        if name == "diagnostics":
            result = diagnostics(args.get("path", ""))
            logger.info(f"[CODING] diagnostics: {result[:160]}")
            return result
        if name == "lsp":
            action = (args.get("action") or "symbols").strip().lower()
            path = args.get("path", "")
            safe = _safe_path(path)
            if safe is None or not Path(safe).is_file():
                return f"Error: file not found: {path}"
            try:
                text = Path(safe).read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                return f"Error reading file: {e}"
            out = lsp_mod.lsp_action(
                safe, str(_REPO_ROOT), text, action,
                int(args.get("line", 1) or 1), int(args.get("character", 1) or 1),
            )
            logger.info(f"[LSP] {action} {path!r} -> {str(out)[:120]}")
            return out if out is not None else "LSP unavailable for this file."
        if name == "plan_mode":
            on = bool(args.get("enabled", True))
            _PLAN_MODE["on"] = on
            logger.info(f"[PLAN] mode {'on' if on else 'off'}")
            return ("Plan mode ON — edits are denied; research only."
                    if on else "Plan mode OFF — edits allowed again.")
        if name == "tools":
            items = getattr(context, "tools", None) or []
            lines = []
            for t in items:
                n = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else "")
                d = getattr(t, "description", "") or ""
                if n:
                    lines.append(f"- {n}: {' '.join(d.split())[:110]}")
            return "\n".join(lines) if lines else "No tools registered."
        if name == "tasks":
            if not _TASKS:
                return "No background tasks."
            return "\n".join(
                f"- {t['id']} [{t['status']}] {t['kind']}: {t['request'][:80]}"
                for t in _TASKS.values()
            )
        if name == "agents_status":
            return _agents_status_text()
        if name == "team":
            return _team_tool(args)
        if name == "skill_list":
            skills = _SKILLS.list()
            if not skills:
                return "No skills yet."
            return "\n".join(
                f"- {s['name']}: {s['description']} (uses={s['uses']})"
                for s in skills
            )
        if name == "skill_get":
            nm = (args.get("name") or "").strip()
            content = _SKILLS.get(nm)
            logger.info(f"[SKILL] get {nm!r} -> {'hit' if content else 'miss'}")
            return content or f"No skill named {nm!r}"
        if name == "skill_save":
            try:
                path = _SKILLS.save(args.get("name", ""),
                                    args.get("description", ""),
                                    args.get("body", ""))
            except Exception as e:
                return f"Error saving skill: {e}"
            logger.info(f"[SKILL] saved {path}")
            return f"Saved skill to {path}"
        if name == "cron":
            action = (args.get("action") or "list").strip().lower()
            if action == "list":
                jobs = _CRON.jobs()
                if not jobs:
                    return "No scheduled jobs."
                return "\n".join(
                    f"- {j['name']}: {j['request'][:80]} "
                    f"[{j.get('cron') or ('every ' + str(j.get('every')) + 's')}]"
                    for j in jobs
                )
            if action == "add":
                try:
                    _CRON.add({
                        "name": args.get("name", ""),
                        "request": args.get("request", ""),
                        "cron": args.get("cron", ""),
                        "every": args.get("every"),
                    })
                except Exception as e:
                    return f"Error adding job: {e}"
                return f"Scheduled job {args.get('name')!r}."
            if action == "remove":
                _CRON.remove(args.get("name", ""))
                return f"Removed job {args.get('name')!r}."
            return f"Unknown cron action {action!r}"
        if name == "apply_patch":
            patch_text = args.get("patch", "")
            logger.info(f"[CODING] apply_patch called ({len(patch_text)} chars)")
            result = apply_patch(patch_text)
            logger.info(f"[CODING] result: {result[:200]}")
            return result
        if name == "web_fetch":
            url = args.get("url", "")
            logger.info(f"[SEARCH] web_fetch called: {url!r}")
            result = await handle_web_fetch(dict(args))
            logger.info(f"[SEARCH] result: {result[:200]}")
            return result
        if name == "generate_image":
            return await _run_generate_image(args)
        if name == "make_video":
            return await _run_make_video(args)
        if name == "read_screen":
            import screen_tool

            question = (args.get("question") or "").strip()
            logger.info(f"[EYES] read_screen called (question={question[:80]!r})")
            result = await asyncio.to_thread(screen_tool.see_screen, question)
            logger.info(f"[EYES] result: {result[:200]}")
            return result
        if name == "look_at_image":
            import screen_tool

            path = (args.get("path") or "").strip()
            question = (args.get("question") or "").strip()
            logger.info(f"[EYES] look_at_image called: {path!r}")
            result = await asyncio.to_thread(screen_tool.look, path, question)
            logger.info(f"[EYES] result: {result[:200]}")
            return result
        if name == "look_through_camera":
            import camera_tool

            question = (args.get("question") or "").strip()
            logger.info(f"[EYES] look_through_camera called (question={question[:80]!r})")
            result = await asyncio.to_thread(camera_tool.look, question)
            logger.info(f"[EYES] result: {result[:200]}")
            return result
        if name == "figma_files":
            result = await asyncio.to_thread(figma_files, args)
            logger.info(f"[FIGMA] figma_files -> {result[:160]}")
            return result
        if name == "figma_file":
            result = await asyncio.to_thread(figma_file, args)
            logger.info(f"[FIGMA] figma_file -> {result[:160]}")
            return result
        if name == "figma_nodes":
            result = await asyncio.to_thread(figma_nodes, args)
            logger.info(f"[FIGMA] figma_nodes -> {result[:160]}")
            return result
        if name == "delegate":
            return await _delegate_tool(args)
        if name == "projects":
            return _projects_tool(args)
        logger.warning(f"[LLM] unknown text-tool name: {name!r}")
        return f"[unsupported tool: {name}]"

    async def _handle_web_search(params):
        result = await _exec_text_tool("web_search", params.arguments)
        await params.result_callback(result)

    async def _handle_file_search(params):
        result = await _exec_text_tool("file_search", params.arguments)
        await params.result_callback(result)

    async def _handle_read_file(params):
        result = await _exec_text_tool("read_file", params.arguments)
        await params.result_callback(result)

    async def _handle_write_file(params):
        result = await _exec_text_tool("write_file", params.arguments)
        await params.result_callback(result)

    async def _handle_edit_file(params):
        result = await _exec_text_tool("edit_file", params.arguments)
        await params.result_callback(result)

    async def _handle_run_bash(params):
        result = await _exec_text_tool("run_bash", params.arguments)
        await params.result_callback(result)

    async def _handle_list_files(params):
        result = await _exec_text_tool("list_files", params.arguments)
        await params.result_callback(result)

    async def _handle_grep(params):
        result = await _exec_text_tool("grep", params.arguments)
        await params.result_callback(result)

    async def _handle_todo(params):
        result = await _exec_text_tool("todo", params.arguments)
        await params.result_callback(result)

    async def _handle_question(params):
        result = await _exec_text_tool("question", params.arguments)
        await params.result_callback(result)

    async def _handle_search_sessions(params):
        result = await _exec_text_tool("search_sessions", params.arguments)
        await params.result_callback(result)

    async def _handle_repo_map(params):
        result = await _exec_text_tool("repo_map", params.arguments)
        await params.result_callback(result)

    async def _handle_mcp_list_tools(params):
        result = await _exec_text_tool("mcp_list_tools", params.arguments)
        await params.result_callback(result)

    async def _handle_mcp_call(params):
        result = await _exec_text_tool("mcp_call_tool", params.arguments)
        await params.result_callback(result)

    async def _handle_diagnostics(params):
        result = await _exec_text_tool("diagnostics", params.arguments)
        await params.result_callback(result)

    async def _handle_lsp(params):
        result = await _exec_text_tool("lsp", params.arguments)
        await params.result_callback(result)

    async def _handle_plan_mode(params):
        result = await _exec_text_tool("plan_mode", params.arguments)
        await params.result_callback(result)

    async def _handle_tools(params):
        result = await _exec_text_tool("tools", params.arguments)
        await params.result_callback(result)

    async def _handle_tasks(params):
        result = await _exec_text_tool("tasks", params.arguments)
        await params.result_callback(result)

    async def _handle_agents_status(params):
        result = await _exec_text_tool("agents_status", params.arguments)
        await params.result_callback(result)

    async def _handle_skill_list(params):
        result = await _exec_text_tool("skill_list", params.arguments)
        await params.result_callback(result)

    async def _handle_skill_get(params):
        result = await _exec_text_tool("skill_get", params.arguments)
        await params.result_callback(result)

    async def _handle_skill_save(params):
        result = await _exec_text_tool("skill_save", params.arguments)
        await params.result_callback(result)

    async def _handle_cron(params):
        result = await _exec_text_tool("cron", params.arguments)
        await params.result_callback(result)

    async def _handle_apply_patch(params):
        result = await _exec_text_tool("apply_patch", params.arguments)
        await params.result_callback(result)

    async def _handle_web_fetch(params):
        result = await _exec_text_tool("web_fetch", params.arguments)
        await params.result_callback(result)

    async def _handle_generate_image(params):
        result = await _exec_text_tool("generate_image", params.arguments)
        await params.result_callback(result)

    async def _handle_make_video(params):
        result = await _exec_text_tool("make_video", params.arguments)
        await params.result_callback(result)

    async def _handle_read_screen(params):
        result = await _exec_text_tool("read_screen", params.arguments)
        await params.result_callback(result)

    async def _handle_look_at_image(params):
        result = await _exec_text_tool("look_at_image", params.arguments)
        await params.result_callback(result)

    async def _handle_look_through_camera(params):
        result = await _exec_text_tool("look_through_camera", params.arguments)
        await params.result_callback(result)

    async def _handle_figma_files(params):
        result = await _exec_text_tool("figma_files", params.arguments)
        await params.result_callback(result)

    async def _handle_figma_file(params):
        result = await _exec_text_tool("figma_file", params.arguments)
        await params.result_callback(result)

    async def _handle_figma_nodes(params):
        result = await _exec_text_tool("figma_nodes", params.arguments)
        await params.result_callback(result)

    async def _handle_delegate(params):
        result = await _exec_text_tool("delegate", params.arguments)
        await params.result_callback(result)

    async def _handle_projects(params):
        result = await _exec_text_tool("projects", params.arguments)
        await params.result_callback(result)

    async def _handle_switch_project(params):
        """Voice: switch the active project by name (or list recents)."""
        try:
            name = (params.arguments.get("name") or "").strip()
        except Exception:
            name = ""
        recents = list_projects()
        if not name:
            names = ", ".join((p.get("name") or "") for p in recents) or "none yet"
            await _code_send_bubble(f"Your projects: {names}.")
            await params.result_callback(f"Recent projects: {names}")
            return
        key = name.lower()
        match = None
        for p in recents:
            nm = (p.get("name") or "").lower()
            base = os.path.basename(os.path.normpath(p.get("worktree") or "")).lower()
            if key in (nm, base) or (nm and key in nm):
                match = p
                break
        if match is None:
            await _code_send_bubble(f"I don't have a project called {name}.")
            await params.result_callback(f"No project named {name}.")
            return
        await on_project_set(match["worktree"])
        await params.result_callback(f"Switched to {match.get('name') or name}.")

    llm.register_function("web_search", _handle_web_search)
    llm.register_function("file_search", _handle_file_search)
    llm.register_function("read_file", _handle_read_file)
    llm.register_function("write_file", _handle_write_file)
    llm.register_function("edit_file", _handle_edit_file)
    llm.register_function("run_bash", _handle_run_bash)
    llm.register_function("list_files", _handle_list_files)
    llm.register_function("grep", _handle_grep)
    llm.register_function("todo", _handle_todo)
    llm.register_function("question", _handle_question)
    llm.register_function("search_sessions", _handle_search_sessions)
    llm.register_function("repo_map", _handle_repo_map)
    llm.register_function("mcp_list_tools", _handle_mcp_list_tools)
    llm.register_function("mcp_call_tool", _handle_mcp_call)
    llm.register_function("diagnostics", _handle_diagnostics)
    llm.register_function("lsp", _handle_lsp)
    llm.register_function("plan_mode", _handle_plan_mode)
    llm.register_function("tools", _handle_tools)
    llm.register_function("tasks", _handle_tasks)
    llm.register_function("agents_status", _handle_agents_status)

    async def _handle_team(params):
        result = await _exec_text_tool("team", params.arguments)
        await params.result_callback(result)
    llm.register_function("team", _handle_team)
    llm.register_function("skill_list", _handle_skill_list)
    llm.register_function("skill_get", _handle_skill_get)
    llm.register_function("skill_save", _handle_skill_save)
    llm.register_function("cron", _handle_cron)
    llm.register_function("apply_patch", _handle_apply_patch)
    llm.register_function("web_fetch", _handle_web_fetch)
    llm.register_function("generate_image", _handle_generate_image)
    llm.register_function("make_video", _handle_make_video)
    llm.register_function("read_screen", _handle_read_screen)
    llm.register_function("look_at_image", _handle_look_at_image)
    llm.register_function("look_through_camera", _handle_look_through_camera)
    llm.register_function("figma_files", _handle_figma_files)
    llm.register_function("figma_file", _handle_figma_file)
    llm.register_function("figma_nodes", _handle_figma_nodes)
    llm.register_function("delegate", _handle_delegate)
    llm.register_function("switch_project", _handle_switch_project)
    llm.register_function("projects", _handle_projects)
    llm._text_tool_executor = _exec_text_tool
    llm_holder["llm"] = llm
    tts_holder["tts"] = tts

    # ── FROZEN (user rule, 2026-09-17) ────────────────────────────────────
    # These four values are working fine. Do NOT retune them, "improve" them,
    # or experiment around the voice hot path without the user's explicit
    # say-so. See "Frozen by the user" in notes/ROADMAP.md.
    vad_params = VADParams(
        confidence=float(os.environ.get("VAD_CONFIDENCE", "0.75")),
        start_secs=float(os.environ.get("VAD_START_SECS", "0.25")),
        stop_secs=float(os.environ.get("VAD_STOP_SECS", "0.25")),
        min_volume=float(os.environ.get("VAD_MIN_VOLUME", "0.7")),
    )

    context = LLMContext()
    context.set_tools(_voice_tool_schemas())
    # Resume any video renders persisted by a previous Jarvis run.
    asyncio.get_event_loop().create_task(_resume_videos())
    vad_analyzer = SileroVADAnalyzer(params=vad_params)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=vad_analyzer,
            user_turn_strategies=UserTurnStrategies(
                start=[
                    VADUserTurnStartStrategy(enable_interruptions=INTERRUPT_ENABLED),
                    TranscriptionUserTurnStartStrategy(),
                ]
            ),
        ),
    )

    user_agg_holder["agg"] = user_aggregator

    turn_router = TurnRouter(
        output_frame_cb=emit_to_ui,
        code_cb=on_code_text,
        session_cb=log_turn,
    )

    recall_injector = RecallInjector(context, recaller)
    work_note_injector = WorkNoteInjector(context)

    status_relay = StatusRelay()

    # Backchannel: a pre-rendered "mm-hm" when the reply is slow to speak.
    # Sits just before the output transport, so its injected audio goes
    # straight to playback and never touches the real reply path.
    backchannel = BackchannelProcessor()

    reviewer = None
    if MEMORY_ENABLED:
        # Reviewer runs on the hardwired brain constant (user decision
        # 2026-09-17): same go gateway, same model as the voice path.
        _oc_key = os.environ.get("OPENCODE_API_KEY", "")
        _oc_headers = {
            # validated opencode-client shape (KB-06 follow-on)
            "x-opencode-project": _oc_ulid("wrk"),
            "x-opencode-client": "cli",
            "User-Agent": "opencode/1.18.29",
            "x-opencode-session": _oc_ulid("ses"),
            "x-opencode-request": _oc_ulid("msg"),
        }
        # Hardwired (2026-09-17): the reviewer runs on the SAME single
        # constant as the talking brain — whatever the brain is, memory is.
        (_reviewer_base_url, _reviewer_model,
         _reviewer_api_key) = _reviewer_settings()
        reviewer = MemoryReviewer(
            llm_base_url=_reviewer_base_url,
            llm_model=_reviewer_model,
            llm_api_key=_reviewer_api_key,
            extra_headers=_oc_headers,
            context=context,
            memory_manager=memory_manager,
            review_interval=int(os.environ.get("MEMORY_REVIEW_INTERVAL", "10")),
            idle_seconds=float(os.environ.get("MEMORY_REVIEW_IDLE", "60")),
            journal_path=MEMORY_DIR / "review-journal.jsonl",
        )

    def say_welcome(name=ASSISTANT_NAME):
        context.add_message(
            {
                "role": "user",
                "content": f"Introduce yourself as {name}. Your name is {name}. Say it in one small friendly spoken sentence.",
            }
        )
        return [LLMRunFrame()]

    @transport.event_handler("on_client_connected")
    async def _on_client_connected(transport, websocket):
        ws_holder["ws"] = websocket
        context.set_messages([])
        session_holder["id"] = session_store.new_session({"client": "ws"})
        logger.info(f"[SESSION] opened {session_holder['id']}")
        if tts:
            await tts.set_voice(assistant_voice())
        if MEMORY_ENABLED:
            memory_manager.initialize(
                os.environ.get("SYSTEM_PROMPT", "You are a kind, concise assistant.")
            )
            set_active_system(
                llm,
                build_system_text(
                    os.environ.get("SYSTEM_PROMPT", "You are a kind, concise assistant."),
                    memory_manager.system_instruction_suffix(),
                ),
            )
            reviewer.start_idle_watchdog()
        elif os.environ.get("SYSTEM_PROMPT", "You are a kind, concise assistant."):
            set_active_system(llm, os.environ.get("SYSTEM_PROMPT", "You are a kind, concise assistant."))
        # The verification watchdog: settles from delegated agents are picked up
        # and checked here, so Jarvis never sits idle while a report is
        # unverified (start() is idempotent across reconnects).
        try:
            import supervisor_loop

            def _emit_verify(line):
                logger.info(f"[VERIFY] {line}")
                try:
                    asyncio.ensure_future(_push_agents())
                except Exception:  # noqa: BLE001
                    pass

            supervisor_loop.loop.set_emit(_emit_verify)
            supervisor_loop.loop.start()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[VERIFY] supervisor loop failed to start: {e!r}")
        status_relay._ready = False
        await status_relay.push_frame(StatusFrame(state="loading"), FrameDirection.DOWNSTREAM)
        entries = build_memory_entries()
        await emit_to_ui(MemoryFrame(entries))
        logger.info(f"[MEM] MemoryFrame emitted on connect ({len(entries)} entries)")
        await emit_to_ui(BrainFrame(provider="Local", model=""))
        # Brain config for onboarding: key popup (no key), model notice (no
        # model yet), reasoning + mic notices, then greet. Greeting on connect
        # happens only for a fully onboarded user; onboarding completes only on
        # the mic OK (onboarding_done), so a reload mid-flow resumes it.
        _greeted[0] = bool(brain_pref_holder.get("onboarded"))
        await _push_brain_config()
        await _push_workers()
        await _push_agents()
        await _push_projects()
        await on_catalog_get()
        status_relay.start_watchdog()
        if _greeted[0]:
            for f in say_welcome(ASSISTANT_NAME):
                await worker.queue_frames([f])
        else:
            logger.info("[BRAINPREF] onboarding pending — greeting deferred")
        asyncio.get_event_loop().create_task(on_recent_sessions_get())

    @transport.event_handler("on_client_disconnected")
    async def _on_client_disconnected(transport, websocket):
        turn_router.set_work_mode(False)
        supervisor.clear_provider_override()
        _stop_poller()
        status_relay.cancel_watchdog()
        if reviewer:
            reviewer.cancel()
        session_holder.pop("id", None)
        try:
            session_store.close()
        except Exception as e:
            logger.warning(f"[SESSION] close failed: {e!r}")
        logger.info("[SESSION] closed")

    pipeline = Pipeline(
        [
            transport.input(),
            AudioToggleProcessor(vad_analyzer, vad_params, on_enabled=backchannel.set_mic_enabled),
            stt,
            turn_router,
            recall_injector,
            work_note_injector,
            user_aggregator,
            llm,
            tts,
            BotTextEcho(session_cb=log_turn),
            TurnSignal(reviewer),
            status_relay,
            backchannel,
            transport.output(),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            idle_timeout_secs=None,
        ),
        idle_timeout_secs=None,
        processor_unusable_policy=ProcessorUnusablePolicy.CONTINUE,
    )

    @worker.event_handler("on_pipeline_error")
    async def _on_pipeline_error(worker_, frame):
        # Non-fatal by design (defect 2026-09-17): a provider error must never
        # take the stack down. Log it plainly and keep the session usable.
        proc = getattr(getattr(frame, "processor", None), "name", "?")
        cat = getattr(getattr(frame, "category", None), "value", "unknown")
        logger.error(f"[LLM] pipeline error ({cat}) on {proc}: {getattr(frame, 'error', '')}")

    runner = WorkerRunner()
    await runner.add_workers(worker)

    async def _cron_loop():
        """G2: opt-in scheduler (CRON_ENABLED=1). Off by default."""
        if os.environ.get("CRON_ENABLED", "0") != "1":
            return
        logger.info("[CRON] scheduler enabled")
        while True:
            try:
                ran = _CRON.run_due(
                    lambda job: asyncio.get_event_loop().create_task(
                        _run_orchestrate(job["request"]))
                )
                if ran:
                    logger.info(f"[CRON] ran {len(ran)} job(s)")
            except Exception as e:
                logger.warning(f"[CRON] tick failed: {e!r}")
            await asyncio.sleep(30)

    asyncio.create_task(_cron_loop())

    async def _skill_curator_loop():
        """E3: archive stale skills (never delete; pinned are kept). Runs once
        shortly after boot, then daily. Off the voice hot path."""
        await asyncio.sleep(60)
        while True:
            try:
                archived = await asyncio.to_thread(_SKILLS.curator)
                if archived:
                    logger.info(f"[SKILLS] curator archived {len(archived)}: {archived}")
            except Exception as e:
                logger.warning(f"[SKILLS] curator failed: {e!r}")
            await asyncio.sleep(24 * 3600)

    asyncio.create_task(_skill_curator_loop())
    await runner.run()


if __name__ == "__main__":
    import signal

    PID_FILE = "/tmp/asha-server.pid"

    def _read_pid():
        try:
            with open(PID_FILE) as f:
                return int(f.read().strip())
        except Exception:
            return None

    def _is_alive(pid):
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    existing = _read_pid()
    if existing and _is_alive(existing):
        try:
            import subprocess
            result = subprocess.run(
                ["ps", "-p", str(existing), "-o", "command="],
                capture_output=True, text=True, timeout=3,
            )
            cmd = (result.stdout or "").strip()
        except Exception:
            cmd = ""
        if "asha" not in cmd and existing != os.getpid():
            print(f"Another Asha server is already running (pid {existing}). Not starting a duplicate.", flush=True)
            sys.exit(1)

    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    def _on_exit(*_args):
        try:
            if os.path.exists(PID_FILE):
                os.unlink(PID_FILE)
        except Exception:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_exit)
        except Exception:
            pass

    asyncio.run(run())
