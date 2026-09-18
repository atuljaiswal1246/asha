"""Coding supervisor (notes/agent-delegation.md §5-6).

The user's brain writes coding briefs and judges worker diffs (the
human-gate substitute). It is NEVER the voice tier (qwen3.8-27b). Brain work
is FREE-FIRST by default: the ``free`` tier (``mimo-v2.5-free`` on the
opencode zen/v1 pool, $0/M) with ONE per-call escalation to the ``cheap``
tier (``mimo-v2.5`` on the opencode-go gateway, ~2.4x cheaper than the paid
deepseek route) when the shared free pool is rate-limited (HTTP 429 /
FreeUsageLimitError). An explicit override can still pin DeepSeek V4 Flash
(``opencode-go``/``opencode``). Fail-closed: if the gateway key is
missing/unusable, both :func:`write_brief` and :func:`review_diff` raise and
coding delegation does NOT run — there is never a silent fallback to the
voice tier and never a model that is not allowlisted.

Transport: DIRECT to the verified OpenAI-compatible gateways — one plain
``POST {base}/chat/completions`` with ``"model": <configured model>`` and
``Authorization: Bearer <key>`` (``mimo-v2.5-free`` on
https://opencode.ai/zen/v1, ``mimo-v2.5``/``deepseek-v4.1-flash`` on
https://opencode.ai/zen/go/v1). This is the original design shape; the local
``opencode serve`` session API round-trip was retired because the serve has a
DEFAULT-MODEL branch (a model-less session could fall back to the paid
opencode/gpt-6-astra). The direct call has no default branch and no serve
routing: the model is sent in the body and the gateway echoes it on every
response, which we verify on EVERY call.

SAFETY (never complete a call off-route; the serve once misrouted to the paid
opencode/gpt-6-astra, so every call is gated before AND after inference):
  1. Pre-flight allowlist — :func:`_assert_allowed_route` rejects any
     provider/model pair outside :data:`_ALLOWED_MODELS` BEFORE a single
     inference token is spent. The list holds the free/cheap brain routes
     (opencode/mimo-v2.5-free, opencode-go/mimo-v2.5) plus the explicit
     DeepSeek V4 Flash routes — never astra, never gpt-*, never the voice tier.
  2. Post-run :func:`_actual_model` — the gateway's echoed response model must
     equal the configured supervisor model, else the result is discarded with
     SupervisorError (never the voice tier, never astra).
3. Fail-closed config — a missing supervisor key/base URL raises
      SupervisorError. Only transient "no answer"/"no text" flaps are retried
      (``attempts``); auth, reachability, non-2xx HTTP and model-identity
      mismatches are configuration, not health, and raise immediately. A free
      zen pool rate-limit (HTTP 429 / FreeUsageLimitError) is the ONE error
      that escalates once to the cheap tier before giving up.

LIVE (KB-08): the brain is wired into the coding path — server.py calls
write_brief on every coding task and review_diff on the work result
(``_code_make_brief`` / ``_code_review_diff``, fail-soft: brain down = raw-text
brief / worker result kept, never a blocked task; both calls run off the voice
hot path via ``asyncio.to_thread``). Brain spend is now bounded:
   - In-process DAILY budget (~200 turns / $0.50 per calendar day, defaults;
     optional SUPERVISOR_DAILY_TURN_CAP / SUPERVISOR_DAILY_COST_CAP overrides,
     no .env change). At the cap every brain call raises SupervisorError and
     callers fail-soft — see :func:`daily_usage`.
   - Worst-case brain-DOWN wait ~60 s per attempt (default act timeout for
     write_brief/review_diff; unreachable/auth/non-2xx/model mismatch raise
     after the FIRST attempt — only "no answer"/"no text" flaps ride out the
     bounded retries).

Structured workbench output (additive, optional, backwards-compatible;
server.py consumption unchanged): write_brief returns an optional "plan"
list parsed best-effort from its free-text PLAN section (the workbench's first
timeline entry), and review_diff returns optional "actions"/"files"
completion entries parsed from its JSON. Every existing field keeps its exact
shape; the model omitting any of the new fields never fails the call.

Skills (LIVE): on a coding task, write_brief optionally matches the skills
runtime (skills.match_skill/load_skill, Jarvis's own prototype/skills/ first),
curates the candidate with skill_curate's 5 guardrails (no secrets, no
destructive defaults, personal-data gate, voice-fit, verified-only), and —
only when it ACCEPTS — injects the SKILL.md body into the prompt as a
"SKILL (follow this if applicable):" section BEFORE the brain replies.
Fail-soft and read-only: a missing runtime, a no-match, a curator reject, or a
loader error all leave the brief unchanged and never block coding
(agent-delegation.md §10). The skill text is never executed here.

Config from env at CALL time (never logged; secrets never in errors):
  SUPERVISOR_BASE_URL   gateway base override (default: per resolved route)
  SUPERVISOR_API_KEY    gateway Bearer key (falls back to OPENCODE_API_KEY)
  SUPERVISOR_MODEL      model override (must be allowlisted; unset = free-first)
  SUPERVISOR_PROVIDER   provider override: opencode-go|opencode|free|cheap
                        (unset = free-first: free mimo-v2.5-free ->
                        cheap mimo-v2.5 -> fail; never a non-allowlisted model)
  SUPERVISOR_DAILY_TURN_CAP   optional in-process daily turn cap (default 200)
  SUPERVISOR_DAILY_COST_CAP   optional in-process daily USD cost cap (default 0.50)

Runtime route override (no restart): :func:`set_provider_override` /
:func:`clear_provider_override` switch the brain's ROUTE at runtime.
``free`` = mimo-v2.5-free on the zen/v1 free pool ($0/M), ``cheap`` =
mimo-v2.5 on the opencode-go gateway, ``opencode-go``/``opencode`` =
DeepSeek V4 Flash on the opencode-go gateway. Every route is allowlist-checked
BEFORE any spend; an unknown provider raises SupervisorError, never a silent
default. The daily budget is ONE shared counter across all routes, so
switching cannot dodge the cap.

Pure module: stdlib + httpx. No pipecat imports, no side effects at import —
safe to import off-bot (tests, scripts).
"""

import datetime
import json
import logging
import os
import re
import time

import httpx

import skill_curate
from providers.openai_compatible import _oc_ulid  # validated header ids (KB-06 follow-on)

logger = logging.getLogger("asha.supervisor")

# Validated opencode-client request shape (KB-06 follow-on 2026-09-11): the
# go gateway requires the full header set (x-opencode-project/session/request
# as ULIDs + client: cli + opencode UA). uuid4/custom-UA shapes are flagged as
# abuse → 429 even under quota. Project+session are stable per process for
# turn correlation; request id is minted per call in _chat_send. Never logged.
_PROJECT_ID = _oc_ulid("wrk")
_SESSION_ID = _oc_ulid("ses")

DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"  # legacy env-pin base
DEFAULT_MODEL = "deepseek-v4.1-flash"  # explicit env/override model
DEFAULT_PROVIDER = "opencode-go"  # legacy route when only SUPERVISOR_MODEL is set
ALLOWED_VERDICTS = ("accept", "reject", "rework")
_KEY_VARS = ("SUPERVISOR_API_KEY", "OPENCODE_API_KEY")

# Default brain routing: FREE zen tier first (mimo-v2.5-free, $0/M) with ONE
# per-call escalation to CHEAP (mimo-v2.5 go) when the free pool is exhausted.
# The explicit deepseek routes (opencode-go/opencode) are untouched.
_DEFAULT_ROUTER = "free"
_CHEAP_ROUTER = "cheap"
_FREE_MODEL = "mimo-v2.5-free"
_CHEAP_MODEL = "mimo-v2.5"

# Layer 1 — hard allowlist: the supervisor brain runs FREE-FIRST on the zen
# free pool (opencode/mimo-v2.5-free, $0/M) or the cheap go gateway
# (opencode-go/mimo-v2.5), falling back to DeepSeek V4 Flash only on the
# explicit opencode-go/opencode routes. Anything else — astra, gpt-*, the
# voice tier, or any non-allowlisted provider/model — is rejected
# with SupervisorError BEFORE any dispatch, via _assert_allowed_route() on
# every read_config(), belt-and-suspenders on top of _actual_model().
_ALLOWED_MODELS = (
    ("opencode", "mimo-v2.5-free"),
    ("opencode-go", "mimo-v2.5"),
    ("opencode-go", "deepseek-v4.1-flash"),
    ("opencode", "deepseek-v4.1-flash"),
)

# Runtime route resolution: resolver name -> {base_url, api_key_env, model,
# provider-label}. "provider" is the allowlist label the route carries in conf
# (free = the opencode zen/v1 pool, cheap = the opencode-go gateway).
_PROVIDER_RESOLVERS = {
    "free": {  # FREE zen tier — $0/M brain work (shared pool, rate-limits)
        "provider": "opencode",
        "base_url": "https://opencode.ai/zen/v1",
        "api_key_env": "SUPERVISOR_API_KEY",
        "model": "mimo-v2.5-free",
    },
    "cheap": {  # CHEAP tier — mimo-v2.5 on the go gateway (free-pool fallback)
        "provider": "opencode-go",
        "base_url": "https://opencode.ai/zen/go/v1",
        "api_key_env": "SUPERVISOR_API_KEY",
        "model": "mimo-v2.5",
    },
    "opencode-go": {  # DeepSeek V4.1 Flash on the go gateway (explicit)
        "provider": "opencode-go",
        "base_url": "https://opencode.ai/zen/go/v1",
        "api_key_env": "SUPERVISOR_API_KEY",
        "model": "deepseek-v4.1-flash",
    },
    "opencode": {  # UI alias — DeepSeek V4.1 Flash on the go gateway
        "provider": "opencode",
        "base_url": "https://opencode.ai/zen/go/v1",
        "api_key_env": "SUPERVISOR_API_KEY",
        "model": "deepseek-v4.1-flash",
    },
}

# Module-level runtime route override; None = use env. Set atomically by
# set_provider_override()/clear_provider_override() with no restart.
_provider_override = None

# _actual_model() accepts an echo that EQUALS the configured model OR (for
# OpenRouter, which echoes a namespaced deepseek/deepseek-v4.1-flash) ENDS WITH
# the bare DeepSeek model. Every echo names the exact model that ran.
_RAW_MODEL_SUFFIX = "deepseek-v4.1-flash"

# Per-1M-token USD rates by model for the money ledger (the daily budget guard
# and the caller logs run on the real cost of the route that actually ran).
_MODEL_RATES_USD = {
    "mimo-v2.5-free": (0.00, 0.00),  # zen/v1 free pool — $0/M
    "mimo-v2.5": (0.14, 0.28),  # opencode-go cheap tier
    "deepseek-v4.1-flash": (0.22, 0.66),  # opencode-go paid tier (rate TBC)
}
_DEFAULT_RATE_USD = _MODEL_RATES_USD["deepseek-v4.1-flash"]

# In-process daily brain budget — bounds the PAID supervisor on a busy day
# (it is live on every coding task since KB-08). The voice tier is never here;
# the guard exists because a busy session could otherwise burn the paid brain
# all day. Defaults are generous for real use (~200 turns ≈ $0.06-0.10/day);
# ops may override via SUPERVISOR_DAILY_TURN_CAP / SUPERVISOR_DAILY_COST_CAP
# (optional env reads — NO .env change required). In-process = resets on
# restart; still a real first-line guard against runaway sessions. When the
# cap is reached, every brain call raises SupervisorError and callers fail-soft
# (raw-text fallback) — coding delegation degrades, never blocks mid-task.
_DAILY_TURN_CAP = 200
_DAILY_COST_CAP_USD = 0.50

_day_key = datetime.date.today().isoformat()
_day_turns = 0
_day_cost = 0.0


def _today_key() -> str:
    return datetime.date.today().isoformat()


def _reset_day_if_needed() -> None:
    global _day_key, _day_turns, _day_cost
    today = _today_key()
    if _day_key != today:
        _day_key = today
        _day_turns = 0
        _day_cost = 0.0


def _daily_budget() -> tuple[int, float]:
    cap_turns = int(os.environ.get("SUPERVISOR_DAILY_TURN_CAP", "") or _DAILY_TURN_CAP)
    cap_cost = float(os.environ.get("SUPERVISOR_DAILY_COST_CAP", "") or _DAILY_COST_CAP_USD)
    return cap_turns, cap_cost


def _check_daily_budget() -> None:
    """Fail-closed BEFORE any spend once today's brain budget is consumed."""
    _reset_day_if_needed()
    cap_turns, cap_cost = _daily_budget()
    if _day_turns >= cap_turns or _day_cost >= cap_cost:
        raise SupervisorError(
            "supervisor fail-closed: daily brain budget reached "
            f"({_day_turns} turns / ${_day_cost:.4f} vs cap "
            f"{cap_turns} turns / ${cap_cost:.2f}) — coding delegation "
            "paused for today; callers fail-soft (raw-text fallback)"
        )


def _record_daily_usage(cost: float) -> None:
    """Count one successful brain turn + its real cost (money is the ledger)."""
    global _day_turns, _day_cost
    _reset_day_if_needed()
    _day_turns += 1
    _day_cost += cost


def daily_usage() -> dict:
    """Read-only in-process brain usage: {day, turns, cost_usd, cap_*}."""
    _reset_day_if_needed()
    cap_turns, cap_cost = _daily_budget()
    return {
        "day": _day_key,
        "turns": _day_turns,
        "cost_usd": round(_day_cost, 4),
        "cap_turns": cap_turns,
        "cap_cost_usd": cap_cost,
    }


def _load_operating_manual() -> str:
    """Best-effort: load notes/asha-brain-operating-manual.md at import.

    Missing/unreadable/empty -> ''. Never raises — the module must import
    cleanly with or without the manual on disk.
    """
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.normpath(
                os.path.join(
                    here, "..", "..", "notes", "asha-brain-operating-manual.md"
                )
            ),
            os.path.normpath(
                os.path.join(os.getcwd(), "notes", "asha-brain-operating-manual.md")
            ),
            "notes/asha-brain-operating-manual.md",
        ]
        for path in candidates:
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read().strip()
                if text:
                    return text
            except OSError:
                continue
        return ""
    except Exception:
        return ""


_OPERATING_MANUAL = _load_operating_manual()

BRIEF_SYSTEM = (
    "You are Jarvis's coding supervisor (DeepSeek V4 Flash), writing an implementation "
    "brief for an autonomous coding agent that edits a codebase. Produce a concise, "
    "structured brief with exactly these sections, in this order: PLAN, GOAL, FILES, "
    "CONSTRAINTS, VERIFY STEPS. PLAN is a short numbered list of concrete steps the "
    "agent should take in order, one per line (e.g. 1. create hello_world.py / 2. add "
    "print('hello')) — the workbench renders it as the first timeline entry. Be "
    "specific about file paths and verification commands. No preamble."
    + (
        "\n\nOPERATING MANUAL (how this brain works — follow these rules):\n"
        + _OPERATING_MANUAL
        if _OPERATING_MANUAL
        else ""
    )
)

_NO_TOOLS = (
    "Reply with the requested text only. Do NOT use tools, do not edit/create/delete "
    "any files, and do not run any commands in this session."
)

REVIEW_SYSTEM = (
    "You are Jarvis's coding supervisor (DeepSeek V4 Flash), the human-gate substitute. "
    "You receive a coding brief and the DIFF a worker agent produced. Judge whether the "
    "diff correctly implements the brief. Reply with STRICT JSON only: "
    '''{"verdict": "accept"|"reject"|"rework", "summary": "<one sentence>", '''
    '''"issues": ["<specific issue>", ...], "actions": ["<numbered: what the agent did, '''
    '''e.g. 1. added GET /health>", ...], "files": ["<path the diff touched, '''
    '''e.g. prototype/ui/server.py>", ...]}'''
    " accept = fully correct; rework = close but needs changes; reject = wrong, "
    "unrelated, or violates constraints. verdict/summary/issues are required. "
    "actions and files are OPTIONAL extras the workbench renders as the completion "
    "entry — summarize what the diff actually did when it is clear, otherwise omit "
    "them; omitting them must not invalidate the verdict."
    + (
        "\n\nOPERATING MANUAL (how this brain works — follow these rules):\n"
        + _OPERATING_MANUAL
        if _OPERATING_MANUAL
        else ""
    )
)


class SupervisorError(RuntimeError):
    """Raised on fail-closed config errors or supervisor/parse failures."""


def _env_or_default(key: str, default: str) -> str:
    return os.environ.get(key, default).strip() or default


def read_config() -> dict:
    """Resolve supervisor routing (fail-closed on missing gateway auth).

    Returns {url, api_key, model, provider}. When a runtime route override is
    set (see :func:`set_provider_override`), the override's resolver dict is
    used as-is. Otherwise env vars resolve, and with BOTH ``SUPERVISOR_MODEL``
    and ``SUPERVISOR_PROVIDER`` unset the default is FREE-FIRST:
    ``mimo-v2.5-free`` on the zen/v1 free pool (provider label ``opencode``).
    Setting ``SUPERVISOR_PROVIDER`` (e.g. ``free``/``cheap``/``opencode-go``)
    or ``SUPERVISOR_MODEL`` pins the route explicitly. Allowlist-checked on
    EVERY read. Raises SupervisorError when no suitable gateway key is
    configured.
    """
    override = _provider_override
    if override is not None:
        url = override["url"]
        api_key = os.environ.get(override["api_key_env"], "").strip()
        model = override["model"]
        provider = override["provider"]
    else:
        provider_env = os.environ.get("SUPERVISOR_PROVIDER", "").strip().lower()
        model_env = os.environ.get("SUPERVISOR_MODEL", "").strip()
        if not provider_env and not model_env:
            resolve = _PROVIDER_RESOLVERS[_DEFAULT_ROUTER]
            provider = resolve["provider"]
            model = resolve["model"]
            url = resolve["base_url"]
            api_key = os.environ.get(resolve["api_key_env"], "").strip()
            if not api_key:
                api_key = os.environ.get("OPENCODE_API_KEY", "").strip()
        else:
            if provider_env in _PROVIDER_RESOLVERS:
                resolve = _PROVIDER_RESOLVERS[provider_env]
                provider = resolve["provider"]
                model = model_env or resolve["model"]
                url = _env_or_default("SUPERVISOR_BASE_URL", resolve["base_url"])
            else:
                url = _env_or_default("SUPERVISOR_BASE_URL", DEFAULT_BASE_URL)
                provider = provider_env or DEFAULT_PROVIDER
                model = model_env or DEFAULT_MODEL
            api_key = os.environ.get("SUPERVISOR_API_KEY", "").strip()
            if not api_key:
                api_key = os.environ.get("OPENCODE_API_KEY", "").strip()
    if not api_key:
        raise SupervisorError(
            "supervisor fail-closed: coding delegation disabled — "
            + (f"{override['api_key_env']} missing for the {provider} override"
               if override is not None else
               "no go-gateway key configured "
               "(SUPERVISOR_API_KEY and OPENCODE_API_KEY both missing; "
               "free-first mimo-v2.5-free -> cheap mimo-v2.5 -> "
               "DeepSeek V4 Flash, never the voice tier, no fallback)")
        )
    conf = {"url": url, "api_key": api_key, "model": model, "provider": provider}
    _assert_allowed_route(conf)
    return conf


def set_provider_override(provider: str) -> dict:
    """Set a runtime brain-ROUTE override (no restart).

    Resolves ``provider`` -> {base_url, api_key_env, model, provider-label} and
    allowlist-checks the route BEFORE any spend. ``free`` = mimo-v2.5-free on
    the zen/v1 free pool ($0/M); ``cheap`` = mimo-v2.5 on the opencode-go
    gateway; ``opencode-go``/``opencode`` = DeepSeek V4 Flash on the go
    gateway. An
    unknown/unsupported provider raises SupervisorError (fail-closed, never a
    silent default).

    Returns the resolved route so callers can confirm what is now in effect.
    The override applies on the NEXT read_config()/dispatch; clear it with
    :func:`clear_provider_override` to return to env resolution.
    """
    if not provider or not str(provider).strip():
        raise SupervisorError(
            "supervisor fail-closed: set_provider_override needs a provider name"
        )
    name = str(provider).strip().lower()
    if name not in _PROVIDER_RESOLVERS:
        raise SupervisorError(
            "supervisor fail-closed: unknown route provider "
            f"{provider!r} (supported: "
            + ", ".join(sorted(_PROVIDER_RESOLVERS))
            + "; no silent default)"
        )
    resolve = _PROVIDER_RESOLVERS[name]
    route = {
        "provider": resolve["provider"],
        "url": resolve["base_url"],
        "model": resolve["model"],
        "api_key_env": resolve["api_key_env"],
    }
    _assert_allowed_route({**route, "api_key": "x"})  # fail-closed BEFORE spend
    global _provider_override
    _provider_override = route
    return route


def clear_provider_override() -> None:
    """Return to env-based routing (no restart). No-op when no override set."""
    global _provider_override
    _provider_override = None


def _assert_allowed_route(conf: dict) -> None:
    """Layer 1: the configured route MUST be on the hard allowlist.

    Anything else — the paid opencode zen slot (``opencode/*``), ``astra``,
    ``gpt-*``, or any third-party provider — fails closed with SupervisorError
    BEFORE a single inference token is spent.
    """
    pair = (conf["provider"], conf["model"])
    if pair in _ALLOWED_MODELS:
        return
    raise SupervisorError(
        f"supervisor fail-closed: {pair[0]}/{pair[1]} is not an allowed "
        "supervisor model (allowlist: "
        + ", ".join("%s/%s" % p for p in _ALLOWED_MODELS)
        + "; never a paid fallback, never zen opencode/*, never astra/gpt-*)"
    )


def is_configured() -> bool:
    """True only when a gateway key + allowlisted route resolve. No network."""
    try:
        read_config()
        return True
    except SupervisorError:
        return False


def _actual_model(result: dict, conf: dict) -> dict:
    """Layer 2: the gateway's echoed response model must equal configuration.

    The configured gateway returns the ACTUAL model that produced the turn in
    ``response["model"]``. An echo passes when it EQUALS the configured model
    OR (OpenRouter namespaces its echoes, e.g. ``deepseek/deepseek-v4.1-flash``)
    ENDS WITH the bare supervisor model ``deepseek-v4.1-flash`` — both name the
    same DeepSeek V4 Flash brain. If the echo is missing or mismatched, the
    call fails closed (never the voice tier, never a default-branch fallback).
    """
    echo = (result.get("model") or "").strip()
    ok = echo == conf["model"] or echo.endswith(_RAW_MODEL_SUFFIX)
    if not ok:
        raise SupervisorError(
            "supervisor fail-closed: routed model mismatch — expected "
            + f"{conf['provider']}/{conf['model']}, gateway ran "
            + f"{conf['provider']}/{echo or 'UNKNOWN'} (never the voice tier)"
        )
    return {"id": echo, "providerID": conf["provider"]}


def _chat_send(messages: list, conf: dict, timeout: float) -> dict:
    """POST one chat completion to the go gateway.

    Returns {text, model, provider, tokens, cost, endpoint, status, usage}.
    Raises SupervisorError (fail-closed) on transport/auth/non-2xx/invalid
    shape. A successful-but-empty turn raises "produced no answer"/"has no
    text" (transient — the retry decision lives in :func:`_dispatch_text`).
    """
    url = conf["url"].rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": "Bearer " + conf["api_key"],
        "x-opencode-project": _PROJECT_ID,
        "x-opencode-session": _SESSION_ID,
        "x-opencode-request": _oc_ulid("msg"),
        "x-opencode-client": "cli",
        "User-Agent": "opencode/1.18.29",
    }
    payload = {"model": conf["model"], "messages": messages}
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise SupervisorError(f"supervisor fail-closed: gateway unreachable ({e})")
    if resp.status_code != 200:
        detail = ""
        try:
            err = resp.json()
            if isinstance(err, dict):
                inner = err.get("data") if isinstance(err.get("data"), dict) else err
                einfo = inner.get("error") if isinstance(inner, dict) and isinstance(inner.get("error"), dict) else None
                if isinstance(einfo, dict):
                    detail = einfo.get("message", "") or ""
        except ValueError:
            pass
        raise SupervisorError(
            "supervisor fail-closed: gateway HTTP " + str(resp.status_code)
            + (" — " + detail[:160] if detail else "")
        )
    try:
        body = resp.json()
    except ValueError:
        raise SupervisorError("supervisor fail-closed: gateway returned non-JSON")
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        body = body["data"]
    if not isinstance(body, dict):
        raise SupervisorError("supervisor fail-closed: gateway returned invalid body shape")
    if isinstance(body.get("error"), dict):
        raise SupervisorError(
            "supervisor fail-closed: gateway error — "
            + ((body["error"].get("message", "") or "")[:200])
        )
    echoed = body.get("model") or ""
    choices = body.get("choices") or []
    if not isinstance(choices, list) or not choices:
        raise SupervisorError("supervisor produced no answer")
    first = choices[0]
    msg = first.get("message") if isinstance(first, dict) else None
    text = (msg.get("content") or msg.get("text") or "") if isinstance(msg, dict) else ""
    if not isinstance(text, str) or not text.strip():
        raise SupervisorError("supervisor has no text")
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    pin = int(usage.get("prompt_tokens") or 0)
    pout = int(usage.get("completion_tokens") or 0)
    tokens = pin + pout
    rate_in, rate_out = _MODEL_RATES_USD.get(
        conf["model"], _DEFAULT_RATE_USD
    )
    cost = (pin * rate_in + pout * rate_out) / 1_000_000.0
    return {
        "text": text,
        "model": echoed,
        "provider": conf["provider"],
        "tokens": tokens,
        "cost": cost,
        "endpoint": url,
        "status": "ok",
        "usage": usage,
    }


def _is_free_pool_limit(err: SupervisorError, conf: dict) -> bool:
    """True when a failed free-tier call is the shared free pool being
    exhausted (HTTP 429 / FreeUsageLimitError / a rate-limit message), which
    is the one error that may escalate once to the cheap tier. Never escalates
    on any other error, and never when the current conf is not the free tier.
    """
    if conf["model"] != _FREE_MODEL:
        return False
    msg = str(err)
    if "429" in msg or "FreeUsageLimitError" in msg:
        return True
    return bool(re.search(r"rate\s*limit", msg, re.I))


def _dispatch_text(system: str, user: str, timeout: float, attempts: int) -> dict:
    """Run one text-only brain turn, FREE-FIRST with a cheap fallback.

    Default routing is the free zen pool (``mimo-v2.5-free``, $0/M). If a
    free-tier call fails specifically because the shared free pool is
    exhausted (HTTP 429 / FreeUsageLimitError), the conf escalates ONCE to
    the cheap tier (``mimo-v2.5`` on the go gateway) before giving up —
    an ADDITIONAL escalation on top of the existing transient retry logic,
    never a replacement for it.

    Bounded retries (``attempts``) ride out transient gateway health flaps
    ("no answer"/"no text"). Auth, reachability, non-2xx HTTP and model
    identity mismatches retry no more — they are configuration, not health.
    Enforces the gateway-echoed model on EVERY call (fail-closed). Checks the
    in-process daily budget BEFORE any spend; records usage after success.
    """
    _check_daily_budget()  # in-process daily cap — fail-closed before spend
    global _provider_override
    transient = ("produced no answer", "has no text")
    last_err: SupervisorError | None = None
    escalated = False
    for attempt in range(1, attempts + 1):
        conf = read_config()  # includes the hard allowlist guard (layer 1)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        try:
            result = _chat_send(messages, conf, timeout)
        except SupervisorError as e:
            last_err = e
            if (not escalated
                    and conf["model"] == _FREE_MODEL
                    and _is_free_pool_limit(e, conf)):
                escalated = True
                resolve = _PROVIDER_RESOLVERS[_CHEAP_ROUTER]
                _provider_override = {
                    "provider": resolve["provider"],
                    "url": resolve["base_url"],
                    "model": resolve["model"],
                    "api_key_env": resolve["api_key_env"],
                }
                logger.warning(
                    "[SUPERVISOR] free zen pool rate-limited — escalating "
                    f"once to cheap tier ({resolve['model']}) for this call"
                )
                continue
            if any(s in str(e) for s in transient) and attempt < attempts:
                wait = 10 * attempt
                logger.warning(
                    f"[SUPERVISOR] attempt {attempt}/{attempts} produced no "
                    f"answer (gateway flap?) — retrying in {wait}s"
                )
                time.sleep(wait)
                continue
            raise SupervisorError(
                f"supervisor fail-closed: {e} (never the voice tier)"
            )
        _actual_model(result, conf)  # raises on mismatch (fail-closed)
        if escalated:
            _provider_override = None  # escalation is once-per-call
        _record_daily_usage(result["cost"])  # money is the ledger
        logger.info(
            f"[SUPERVISOR] model={result['model']} (echoed) "
            f"endpoint={result['endpoint']} tokens={result['tokens']} "
            f"cost=${result['cost']:.4f} status={result['status']}"
        )
        return result
    if escalated:
        _provider_override = None
    raise SupervisorError(
        f"supervisor fail-closed: {last_err} after {attempts} attempts "
        f"(never the voice tier)"
    )


# PLAN section of a free-text brief: a PLAN header line followed by numbered
# steps, ending at the next section header (GOAL|FILES|CONSTRAINTS|VERIFY
# STEPS) or EOF. Th model varies its formatting (bare "PLAN"/"GOAL" lines,
# "PLAN:"/"GOAL:" with colon, markdown-bold "**PLAN**", or a single inline
# "PLAN: 1. …" heading), so a small line scanner handles it instead of one
# regex. A step line (digit- or bullet-led) is never a section terminator, and
# prose like "plan to fix…" never counts as a header (header lines must be the
# keyword alone or keyword+delimiter+content). Fenced code blocks inside a step
# are skipped so they do not become fake steps. Best-effort by design:
# _parse_plan returns None on any malformed/missing plan — never an error.
_PLAN_HEADER_ONLY_RE = re.compile(
    r"^\s*\*{0,2}\s*PLAN\*{0,2}\s*(?:[:.-]\s*)?\*{0,2}\s*$", re.I | re.M
)
_PLAN_INLINE_STEP_RE = re.compile(
    r"^\s*\*{0,2}\s*PLAN\*{0,2}\s*[:.-]+[ \t]+(\S.*?)\*{0,2}\s*$", re.I | re.M
)
_SECTION_HEADER_RE = re.compile(
    r"^\s*\*{0,2}\s*(?:GOAL|FILES|CONSTRAINTS|VERIFY(?:\s+STEPS)?)\*{0,2}"
    r"\s*(?:[:.-]\s+\S|[:.-]\s*\*{0,2}\s*$|\s*$)",
    re.I | re.M,
)
_FIRST_STEP_RE = re.compile(r"^(?:\d+[.)\-]|[-•])")
_LEADING_BULLET_RE = re.compile(r"^[-*•]+\s+")


def _plan_header(line: str):
    """One line -> ("header", None) | ("inline", first_step) | None."""
    s = line.strip()
    m = _PLAN_INLINE_STEP_RE.match(s)
    if m:
        return "inline", m.group(1)
    if _PLAN_HEADER_ONLY_RE.match(s):
        return "header", None
    return None


def _is_section_header(line: str) -> bool:
    s = line.strip()
    if not s or _FIRST_STEP_RE.match(s):
        return False
    return bool(_SECTION_HEADER_RE.match(s))


def _parse_plan(brief):
    """Best-effort: extract the numbered PLAN steps from a free-text brief.

    Returns a list of step lines (keeping the model's numbering), or None when
    the model omitted the PLAN section. Never raises — a missing/malformed
    plan is an optional field, not an error (the workbench falls back to the
    text-only brief as its first timeline entry).
    """
    if not brief or not isinstance(brief, str) or not brief.strip():
        return None
    lines = brief.splitlines()
    start = next((i for i, ln in enumerate(lines) if _plan_header(ln)), None)
    if start is None:
        return None
    _, inline = _plan_header(lines[start])
    steps = [inline] if inline is not None else []
    in_fence = False
    for ln in lines[start + 1:]:
        s = ln.strip()
        if in_fence:
            if s.startswith("```"):
                in_fence = False
            continue
        if s.startswith("```"):
            in_fence = True
            continue
        if not s:
            continue
        if _is_section_header(s):
            break
        steps.append(s)
    if not steps or not _FIRST_STEP_RE.match(steps[0]):
        return None
    return [_LEADING_BULLET_RE.sub("", s) for s in steps]


def _maybe_inject_skill(user: str, task_text: str):
    """Best-effort: inject a curated skill's body into a brief prompt.

    Flow: skills.match_skill(task) -> skills.load_skill(name) ->
    skill_curate.curate_skill(candidate). Only an ACCEPT injects a
    "SKILL (follow this if applicable):" block before the brain replies;
    anything else leaves ``user`` unchanged. Fail-soft in every path — a
    missing skills runtime, no match, a curator reject, a loader error or a
    malformed skill never blocks coding. Read-only: the skill text is only
    passed to the brain; nothing here ever executes a script.

    Returns (user, skill_name_or_None).
    """
    try:
        import skills

        hit = skills.match_skill(task_text or "")
    except Exception as e:
        logger.debug(f"[SUPERVISOR] skill match unavailable (fail-soft): {e!r}")
        return user, None
    if not hit:
        return user, None
    name = hit.name if hasattr(hit, "name") else (hit.get("name") if isinstance(hit, dict) else None)
    if not name:
        return user, None
    try:
        loaded = skills.load_skill(name)
    except Exception as e:
        logger.debug(f"[SUPERVISOR] skill load failed (fail-soft): {e!r}")
        return user, None
    if not loaded:
        return user, None
    body = loaded.body if hasattr(loaded, "body") else (loaded.get("body") if isinstance(loaded, dict) else None)
    if not body or not str(body).strip():
        return user, None
    frontmatter = (
        loaded.frontmatter
        if hasattr(loaded, "frontmatter") and isinstance(loaded.frontmatter, dict)
        else None
    )
    candidate = {"name": name, "body": str(body)}
    if frontmatter:
        candidate["frontmatter"] = frontmatter
    desc = loaded.description if hasattr(loaded, "description") else None
    if desc:
        candidate["description"] = desc
    try:
        verdict = skill_curate.curate_skill(candidate)
    except Exception as e:
        logger.warning(f"[SUPERVISOR] skill curation failed (fail-soft): {e!r}")
        return user, None
    if verdict["verdict"] != "accept":
        logger.info(
            f"[SUPERVISOR] skill {name} not curated ({verdict['reason']}); brief unchanged"
        )
        return user, None
    logger.info(f"[SUPERVISOR] skill {name} curated — injected into brief")
    block = "\n\nSKILL (follow this if applicable):\n" + str(body).strip()[:6000]
    return user + block, name


def write_brief(user_request, context=None, *, timeout=60, attempts=3):
    """Ask DeepSeek V4 Flash for a structured coding brief via the go gateway.

    Returns dict: {"brief", "plan"(optional), "skill"(optional), "model",
    "provider", "tokens", "cost", "endpoint"}. "brief" keeps its free-text
    shape (sections PLAN, GOAL, FILES, CONSTRAINTS, VERIFY STEPS); "plan" is
    an ADDITIVE, optional numbered step list parsed best-effort from the
    brief's PLAN section; "skill" names a curated Agent Skill whose body was
    injected into the prompt (additive, optional, fail-soft). Raises
    SupervisorError (fail-closed) if the gateway key is missing, the gateway
    is unreachable/refuses auth, the echoed model is not the configured
    supervisor model, or the model produced nothing after ``attempts`` runs
    (transient gateway flaps are retried).
    """
    user_parts = ["CODING REQUEST:", user_request]
    if context:
        user_parts.append("CONTEXT:")
        user_parts.append(
            json.dumps(context, indent=2) if not isinstance(context, str) else context
        )
    user = "\n\n".join(user_parts) + "\n\n" + _NO_TOOLS
    user, skill_name = _maybe_inject_skill(user, str(user_request or ""))
    conf = read_config()
    result = _dispatch_text(BRIEF_SYSTEM, user, timeout, attempts)
    result["brief"] = result.pop("text")
    plan = _parse_plan(result["brief"])
    if plan:
        result["plan"] = plan
    if skill_name:
        result["skill"] = skill_name
    return result


_JSON_SCAN = re.compile(r"\{.*\}", re.S)


def _repair_json_quotes(content: str) -> str:
    """Best-effort: escape unescaped inner double quotes inside JSON strings.

    The model sometimes embeds code with quotes in a string value (e.g. a
    summary containing ``print("hello")``), which is invalid JSON. A ``"``
    closes a string only when the next non-space char is a JSON structure
    token (``,`` ``}`` ``]`` ``:``) or end of input; anything else is an inner
    quote to be escaped. Falls through to the plain error if the repair still
    does not parse — best-effort, never masks a real verdict.
    """
    out = []
    in_str = False
    prev_esc = False
    n = len(content)
    i = 0
    while i < n:
        ch = content[i]
        if in_str:
            if prev_esc:
                out.append(ch)
                prev_esc = False
                i += 1
                continue
            if ch == "\\":
                out.append(ch)
                prev_esc = True
                i += 1
                continue
            if ch == '"':
                j = i + 1
                while j < n and content[j] in " \t":
                    j += 1
                nxt = content[j] if j < n else None
                if nxt is None or nxt in ",}]:":
                    in_str = False  # structural close (no inner quote)
                    out.append(ch)
                else:
                    out.append('\\"')  # inner quote inside a string value
                i += 1
                continue
            out.append(ch)
            i += 1
        else:
            if ch == '"':
                in_str = True
            out.append(ch)
            i += 1
    return "".join(out)


def _parse_json(content: str) -> dict:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    m = _JSON_SCAN.search(cleaned)
    if m:
        blob = m.group(0)
        for candidate in (blob, _repair_json_quotes(blob)):
            try:
                return json.loads(candidate)
            except ValueError:
                continue
    raise SupervisorError("supervisor returned unparseable JSON: " + content[:200])


def review_diff(diff_text, brief, *, timeout=60, attempts=3):
    """Ask DeepSeek V4 Flash to judge a worker diff against the brief.

    Returns dict: {"verdict": accept|reject|rework, "summary", "issues",
    "actions"(optional), "files"(optional), "model", "provider", "tokens",
    "cost", "endpoint"}. verdict/summary/issues keep their exact shape
    (server.py consumes them). actions (numbered what-the-agent-did) and files
    (paths touched) are ADDITIVE, optional completion entries the workbench
    renders — parsed best-effort and omitted when the model leaves them out.
    Raises SupervisorError (fail-closed) on config, transport or parse
    failure, or when the model produces nothing after ``attempts`` runs.
    """
    user = (
        "BRIEF:\n" + (brief or "")
        + "\n\nDIFF:\n" + (diff_text or "")
        + "\n\n" + _NO_TOOLS
    )
    conf = read_config()
    result = _dispatch_text(REVIEW_SYSTEM, user, timeout, attempts)
    parsed = _parse_json(result["text"])
    verdict = str(parsed.get("verdict", "")).strip().lower()
    if verdict not in ALLOWED_VERDICTS:
        raise SupervisorError(
            "supervisor verdict parse failed: expected accept|reject|rework, got "
            + repr(parsed.get("verdict"))
        )
    result.pop("text", None)
    result["verdict"] = verdict
    result["summary"] = parsed.get("summary", "")
    issues = parsed.get("issues", [])
    result["issues"] = issues if isinstance(issues, list) else [str(issues)]
    actions = parsed.get("actions")
    if isinstance(actions, list):
        result["actions"] = [str(a) for a in actions if str(a).strip()]
    elif actions and str(actions).strip():
        result["actions"] = [str(actions)]
    files = parsed.get("files")
    if isinstance(files, list):
        result["files"] = [str(f) for f in files if str(f).strip()]
    elif files and str(files).strip():
        result["files"] = [str(files)]
    return result


