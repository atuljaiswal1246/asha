"""M2 coding-dispatch engine (notes/agent-delegation.md §5, milestone M2).

Asha front -> opencode serve (local headless HTTP) -> free zen agent.

Pure module: stdlib + httpx only. No pipecat imports, no side effects at
import time — safe to import off-bot (tests, scripts).

Config via env (never logged):
  OPENCODE_SERVER_URL       default http://127.0.0.1:4096
  OPENCODE_SERVER_USER / OPENCODE_SERVER_PASSWORD  (basic auth; omit both
                            if the server runs without auth)
  OPENCODE_SERVER_USERNAME is accepted as a fallback for USER (opencode's
  own serve convention uses USERNAME).

Live API shapes (verified against opencode 1.18.29, /doc):
  POST /api/session {agent, model:{providerID,id}} -> {data:{id,...}}
  POST /api/session/{id}/prompt {prompt:{text}}   -> {data:{id: msg_...}}
  GET  /api/session/{id}/message                  -> {data:[msg...]}
  GET  /api/session/{id}/message/{messageID}      -> {data:msg}

NOTE: POST /api/session/{id}/wait exists but answers 503
("Session wait is not available yet") even on healthy sessions, so dispatch
polls GET message until the last assistant message carries time.completed.
"""

import logging
import os
import time
import uuid

import httpx

logger = logging.getLogger("asha.opencode")

DEFAULT_SERVER_URL = "http://127.0.0.1:4096"
DEFAULT_PROVIDER_ID = "opencode"
DEFAULT_MODEL_ID = "big-pickle"  # free zen slot, cost $0
GO_PROVIDER_ID = "opencode-go"  # paid tier — same models' cheap twins only
GO_DIRECT_URL = "https://opencode.ai/zen/go/v1/chat/completions"
# Per-1M-token rates for go-direct cost math (verified via /config/providers).
GO_RATES = {
    "mimo-v2.5": (0.14, 0.28),
}
DEFAULT_AGENT = "build"
DEFAULT_PERMISSION_TIMEOUT = 60.0  # max wait per permission ask (server bound)
GRACE_AFTER_DENY = 90.0  # let the agent answer post-deny before "blocked"
WORKER_BUDGET = 90.0  # free-worker answer budget before go-twin fallback

# Model-matched paid fallback (cost rule: DeepSeek is brain-only — briefs +
# diff review — and must NEVER do muscle work). Free zen model -> its cheap
# go twin, same family. Models with no twin (big-pickle) -> universal cheap
# fallback mimo-v2.5 (go). muse-spark-1.3-contributor is EXCLUDED: the direct
# go route rejects it (403), so it is not a valid go id — universal applies.
GO_FALLBACK_TABLE = {
    "mimo-v2.5-free": "mimo-v2.5",
}
UNIVERSAL_GO_FALLBACK = "mimo-v2.5"  # go
_FORBIDDEN_MUSCLE_SUBSTRINGS = ("deepseek",)  # brain-only, never muscle work

# M2 routing heuristic (agent-delegation.md §4): cheap keyword gate.
# Runs in the voice turn but never blocks it — dispatch is deferred.
_REQUEST_VERBS = ("fix", "bug", "refactor", "debug", "script", "endpoint", "code")
_REQUEST_PHRASES = (
    "add endpoint",
    "add an endpoint",
    "write function",
    "write a function",
    "write code",
    "write a script",
    "write script",
    "create file",
    "create a file",
)


class OpencodeToolError(RuntimeError):
    """Serve unreachable / auth failed / agent produced nothing."""


class WorkerFailed(OpencodeToolError):
    """Free worker exhausted: finish:"error", completed-empty-text, or no
    answer within the worker budget. dispatch() catches this (and only this)
    to retry once on the model-matched go twin. Carries .reason plus the
    failed serve session ids for traceability."""

    def __init__(self, reason: str, message: str, session_id: str = "",
                 prompt_message_id: str = ""):
        super().__init__(message)
        self.reason = reason
        self.session_id = session_id
        self.prompt_message_id = prompt_message_id


def _resolve_go_fallback(model_id: str) -> str:
    """Model-matched go twin for a free zen model; universal cheap fallback
    when the model has no twin. Never resolves to a brain-only model."""
    twin = GO_FALLBACK_TABLE.get(model_id, UNIVERSAL_GO_FALLBACK)
    if any(s in twin.lower() for s in _FORBIDDEN_MUSCLE_SUBSTRINGS):
        raise OpencodeToolError(
            f"refusing muscle work: fallback {twin!r} is brain-only"
        )
    return twin


def _reject_brain_muscle(model_id: str) -> None:
    """Hard cost rule: DeepSeek (brain) must never do coding muscle work."""
    if any(s in (model_id or "").lower() for s in _FORBIDDEN_MUSCLE_SUBSTRINGS):
        raise OpencodeToolError(
            f"refusing muscle work on brain-only model {model_id!r}"
        )


def is_coding_request(text: str) -> bool:
    """Cheap heuristic: is this utterance a coding task for the zen agent?"""
    if not text:
        return False
    lowered = text.lower()
    return any(v in lowered for v in _REQUEST_VERBS) or any(
        p in lowered for p in _REQUEST_PHRASES
    )


def _config() -> tuple:
    url = os.environ.get("OPENCODE_SERVER_URL", DEFAULT_SERVER_URL).rstrip("/")
    user = os.environ.get("OPENCODE_SERVER_USER", "") or os.environ.get(
        "OPENCODE_SERVER_USERNAME", ""
    )
    password = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
    return url, user, password


def _client(timeout: float) -> httpx.Client:
    url, user, password = _config()
    auth = (user, password) if user and password else None
    return httpx.Client(base_url=url, auth=auth, timeout=timeout)


def _post(client: httpx.Client, path: str, payload: dict | None) -> dict:
    url, _, _ = _config()
    try:
        resp = client.post(path, json=payload)
    except httpx.ConnectError:
        raise OpencodeToolError(
            f"opencode serve unreachable at {url} — is it running? "
            "(supervisor launch pending user OK)"
        )
    except httpx.HTTPError as e:
        raise OpencodeToolError(f"opencode serve transport error: {type(e).__name__}")
    if resp.status_code == 401:
        raise OpencodeToolError(
            "opencode serve auth failed — check "
            "OPENCODE_SERVER_USER/OPENCODE_SERVER_PASSWORD"
        )
    if resp.status_code >= 400:
        raise OpencodeToolError(
            f"opencode serve POST {path} -> {resp.status_code}: "
            f"{resp.text[:200]}"
        )
    try:
        body = resp.json()
    except ValueError:
        raise OpencodeToolError(f"opencode serve POST {path}: non-JSON reply")
    return body.get("data", body)


def _get(client: httpx.Client, path: str):
    url, _, _ = _config()
    try:
        resp = client.get(path)
    except httpx.ConnectError:
        raise OpencodeToolError(
            f"opencode serve unreachable at {url} — is it running? "
            "(supervisor launch pending user OK)"
        )
    except httpx.HTTPError as e:
        raise OpencodeToolError(f"opencode serve transport error: {type(e).__name__}")
    if resp.status_code == 401:
        raise OpencodeToolError(
            "opencode serve auth failed — check "
            "OPENCODE_SERVER_USER/OPENCODE_SERVER_PASSWORD"
        )
    if resp.status_code >= 400:
        raise OpencodeToolError(
            f"opencode serve GET {path} -> {resp.status_code}: {resp.text[:200]}"
        )
    try:
        body = resp.json()
    except ValueError:
        raise OpencodeToolError(f"opencode serve GET {path}: non-JSON reply")
    return body.get("data", body)


def _message_text(msg: dict) -> str:
    """Extract readable text from a session message of either role."""
    if not isinstance(msg, dict):
        return ""
    if msg.get("type") == "user":
        text = msg.get("text", "")
        return text if isinstance(text, str) else ""
    parts = []
    content = msg.get("content") or []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text", "")
            if isinstance(text, str) and text.strip():
                parts.append(text)
    return "\n".join(parts)


def _reply_permission(client: httpx.Client, session_id: str,
                      request_id: str, decision: str) -> bool:
    """Answer one permission ask: POST .../permission/{id}/reply.

    The endpoint answers 204 No Content (no JSON body). Returns True if the
    reply landed; False if the request was already resolved (404 tolerated).
    """
    url, _, _ = _config()
    try:
        resp = client.post(
            f"/api/session/{session_id}/permission/{request_id}/reply",
            json={"reply": decision},
        )
    except httpx.ConnectError:
        raise OpencodeToolError(
            f"opencode serve unreachable at {url} — is it running?"
        )
    except httpx.HTTPError as e:
        raise OpencodeToolError(
            f"opencode serve transport error: {type(e).__name__}"
        )
    if resp.status_code == 401:
        raise OpencodeToolError(
            "opencode serve auth failed — check "
            "OPENCODE_SERVER_USER/OPENCODE_SERVER_PASSWORD"
        )
    if resp.status_code == 404:
        return False
    if resp.status_code >= 400:
        raise OpencodeToolError(
            f"opencode serve permission reply -> {resp.status_code}: "
            f"{resp.text[:200]}"
        )
    return True


def _last_completed_assistant(messages: list) -> dict | None:
    # Newest by creation time (list order is not contractual).
    best: dict | None = None
    best_ts = -1
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("type") != "assistant":
            continue
        ts = (m.get("time") or {}).get("created", -1) if isinstance(m.get("time"), dict) else -1
        if (m.get("time") or {}).get("completed") and ts >= best_ts:
            best, best_ts = m, ts
    return best


def read_result(session_id: str, message_id: str | None = None,
                timeout: float = 30.0) -> str:
    """Return assistant message text for a session.

    message_id=None -> last completed assistant message; otherwise fetch
    that exact message. Raises OpencodeToolError if there is nothing yet.
    """
    with _client(timeout) as client:
        if message_id:
            text = _message_text(_get(client, f"/api/session/{session_id}/message/{message_id}"))
        else:
            messages = _get(client, f"/api/session/{session_id}/message")
            if not isinstance(messages, list):
                raise OpencodeToolError("opencode serve: unexpected message list shape")
            last = _last_completed_assistant(messages)
            if last is None:
                raise OpencodeToolError(
                    f"opencode serve: no completed assistant message in {session_id} yet"
                )
            text = _message_text(last)
    if not text.strip():
        raise OpencodeToolError(
            f"opencode serve: assistant message in {session_id} has no text"
        )
    return text


def _wait_for_answer(client: httpx.Client, session_id: str,
                     timeout: float, poll_interval: float,
                     on_permission=None,
                     permission_timeout: float = DEFAULT_PERMISSION_TIMEOUT,
                     grace_after_deny: float = GRACE_AFTER_DENY,
                     cancel=None) -> tuple:
    """Quiescent wait for the final assistant message, with permission asks.

    Each poll also checks GET permission (session-scoped). A pending request
    is handed to on_permission(request) -> "once" | "reject" exactly once;
    the callback MUST return within permission_timeout (the server bounds its
    ask with default-deny). Without a callback, requests are rejected
    immediately (fail-safe: never hang, never auto-allow).
    Returns (last_message_or_None, denied_resources).
    Raises OpencodeToolError on timeout (no answer, no deny) or cancel.
    """
    deadline = time.monotonic() + timeout
    last: dict | None = None
    last_id: str | None = None
    stable = 0
    seen_perm_ids: set = set()
    denied: list = []
    last_deny_at: float | None = None
    while time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            raise OpencodeToolError("dispatch cancelled")
        try:
            pending = _get(client, f"/api/session/{session_id}/permission")
        except OpencodeToolError as e:
            logger.warning(f"[CODE] permission poll failed (tolerated): {e}")
            pending = []
        if isinstance(pending, list):
            for req in pending:
                if not isinstance(req, dict):
                    continue
                rid = req.get("id", "")
                if not rid or rid in seen_perm_ids:
                    continue
                seen_perm_ids.add(rid)
                action = req.get("action", "?")
                resources = req.get("resources") or []
                logger.info(f"[CODE] permission asked: {action}: {resources}")
                decision = "reject"
                if on_permission is not None:
                    t0 = time.monotonic()
                    try:
                        decision = on_permission(req) or "reject"
                    except Exception as e:
                        logger.warning(f"[CODE] permission handler failed: {e!r}")
                        decision = "reject"
                    if time.monotonic() - t0 > permission_timeout * 2:
                        logger.warning("[CODE] permission handler overran its bound")
                else:
                    logger.info("[CODE] no permission handler — auto-reject")
                if decision not in ("once", "reject"):
                    logger.warning(f"[CODE] bad permission decision {decision!r} → reject")
                    decision = "reject"
                try:
                    landed = _reply_permission(client, session_id, rid, decision)
                except OpencodeToolError as e:
                    logger.warning(f"[CODE] permission reply failed (tolerated): {e}")
                    continue
                if not landed:
                    logger.info(f"[CODE] permission {rid} already resolved")
                if decision == "reject":
                    denied.extend([r for r in resources if r not in denied])
                    last_deny_at = time.monotonic()
                    logger.info(f"[CODE] denied: {resources}")
                else:
                    logger.info(f"[CODE] allowed once: {resources}")
        messages = _get(client, f"/api/session/{session_id}/message")
        if isinstance(messages, list):
            cand = _last_completed_assistant(messages)
            cand_id = cand.get("id") if isinstance(cand, dict) else None
            stable = stable + 1 if (cand_id is not None and cand_id == last_id) else 0
            last, last_id = cand, cand_id
            if isinstance(cand, dict) and cand.get("finish") == "error":
                err = cand.get("error") or {}
                msg = err.get("message") if isinstance(err, dict) else "unknown provider error"
                raise WorkerFailed(
                    "error",
                    "opencode serve: provider run failed — "
                    + str(msg) + f" (session {session_id})",
                    session_id=session_id,
                )
            if last is not None and stable >= 3 and _message_text(last).strip():
                return last, denied
            if (denied and last_deny_at is not None
                    and time.monotonic() - last_deny_at > grace_after_deny):
                return None, denied
        time.sleep(poll_interval)
    if last is None and not denied:
        raise WorkerFailed(
            "timeout",
            f"opencode serve: agent produced no answer in {session_id} "
            f"within {timeout:.0f}s",
            session_id=session_id,
        )
    if (last is not None and not _message_text(last).strip() and not denied):
        raise WorkerFailed(
            "empty",
            f"opencode serve: assistant message in {session_id} completed "
            "with no text",
            session_id=session_id,
        )
    return last, denied


def _session_usage(client: httpx.Client, session_id: str) -> tuple:
    """Best-effort (tokens, cost) for synthesized results; zeros on failure."""
    try:
        info = _get(client, f"/api/session/{session_id}")
        tokens = info.get("tokens") or {}
        cost = info.get("cost", 0)
        try:
            cost = float(cost)
        except (TypeError, ValueError):
            cost = 0.0
        return {"input": tokens.get("input", 0),
                "output": tokens.get("output", 0)}, cost
    except OpencodeToolError:
        return {"input": 0, "output": 0}, 0.0


def _blocked_result(client: httpx.Client, session_id: str,
                    prompt_message_id: str, denied: list,
                    model_id: str, provider_id: str,
                    tier: str = "free") -> dict:
    tokens, cost = _session_usage(client, session_id)
    text = ("Blocked — permission denied for: " + ", ".join(denied) + ". "
            "The task could not proceed without access.")
    logger.info(f"[CODE] blocked {session_id} denied={denied}")
    return {
        "session_id": session_id,
        "prompt_message_id": prompt_message_id,
        "message_id": "",
        "text": text,
        "tokens": tokens,
        "cost": cost,
        "model_id": model_id,
        "provider_id": provider_id,
        "status": "blocked",
        "permissions_denied": list(denied),
        "tier": tier,
    }


def _result_shape(last: dict, session_id: str, prompt_message_id: str,
                  model_id: str, provider_id: str, status: str,
                  denied: list | None = None, tier: str = "free") -> dict:
    tokens = last.get("tokens") or {}
    cost = last.get("cost", 0)
    try:
        cost = float(cost)
    except (TypeError, ValueError):
        cost = 0.0
    logger.info(
        f"[CODE] done {session_id} "
        f"tokens={tokens.get('input', '?')}/{tokens.get('output', '?')} "
        f"cost={cost}"
    )
    return {
        "session_id": session_id,
        "prompt_message_id": prompt_message_id,
        "message_id": last.get("id", ""),
        "text": _message_text(last),
        "tokens": {
            "input": tokens.get("input", 0),
            "output": tokens.get("output", 0),
        },
        "cost": cost,
        "model_id": model_id,
        "provider_id": provider_id,
        "status": status,  # "done" | "proposed" | "blocked" — held, never auto-applied
        "permissions_denied": list(denied or []),
        "tier": tier,  # "free" | "paid-go"
    }


def _attempt(brief: str, model_id: str, provider_id: str, agent: str,
             timeout: float, poll_interval: float,
             on_session, on_permission,
             permission_timeout: float, grace_after_deny: float,
             cancel, tier: str) -> dict:
    """One worker attempt on one model. Raises WorkerFailed when the worker
    is exhausted (error / empty / timeout); other OpencodeToolErrors (auth,
    transport, cancel, blocked-shape handled by caller) propagate."""
    with _client(timeout) as client:
        session = _post(client, "/api/session", {
            "agent": agent,
            "model": {"providerID": provider_id, "id": model_id},
        })
        session_id = session.get("id", "")
        if not session_id:
            raise OpencodeToolError("opencode serve: session create returned no id")
        logger.info(f"[CODE] session {session_id} ({provider_id}/{model_id})")

        prompt = _post(client, f"/api/session/{session_id}/prompt",
                       {"prompt": {"text": brief.strip()}})
        prompt_message_id = prompt.get("id", "")
        logger.info(f"[CODE] prompt admitted {prompt_message_id or '?'}")
        if on_session is not None:
            on_session(session_id, prompt_message_id)

        try:
            last, denied = _wait_for_answer(
                client, session_id, timeout, poll_interval,
                on_permission=on_permission, permission_timeout=permission_timeout,
                grace_after_deny=grace_after_deny, cancel=cancel)
        except WorkerFailed as wf:
            wf.prompt_message_id = wf.prompt_message_id or prompt_message_id
            raise
        if last is None:
            return _blocked_result(client, session_id, prompt_message_id,
                                   denied, model_id, provider_id, tier)
        return _result_shape(last, session_id, prompt_message_id,
                             model_id, provider_id, "done", denied, tier)


def _go_api_key() -> str:
    """Bearer key for the direct go endpoint. Never logged."""
    key = os.environ.get("OPENCODE_API_KEY", "").strip()
    if not key:
        raise OpencodeToolError(
            "go-twin fallback unavailable: OPENCODE_API_KEY is not set"
        )
    return key


def _go_direct_attempt(brief: str, twin: str, timeout: float,
                       cancel=None, session_id: str = "") -> dict:
    """One paid go-twin attempt via the DIRECT go endpoint (not the serve).

    The serve's session API cannot resolve go twins (ModelUnavailableError),
    but POST {GO_DIRECT_URL} with the twin id works. Answer-only turn (no
    workspace tools on this route — fine for proposal/short-answer fallback).
    Model-echo checked fail-closed; cost from the twin's go rates. No serve
    session is created for this leg. Carries x-opencode-session (the failed
    serve session id when it is already 32-hex, else a fresh uuid) plus a
    fixed User-Agent — requests without the session header may error.
    Raises OpencodeToolError on any failure.
    """
    _reject_brain_muscle(twin)
    if cancel is not None and cancel.is_set():
        raise OpencodeToolError("dispatch cancelled")
    api_key = _go_api_key()
    payload = {
        "model": twin,
        "messages": [{"role": "user", "content": brief.strip()}],
    }
    go_session = (session_id or "")
    if not (len(go_session) == 32 and all(
            c in "0123456789abcdefABCDEF" for c in go_session)):
        go_session = uuid.uuid4().hex
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                GO_DIRECT_URL, json=payload,
                headers={
                    "Authorization": "Bearer " + api_key,
                    "x-opencode-session": go_session,
                    "User-Agent": "asha-opencode-tool/1.0",
                },
            )
    except httpx.HTTPError as e:
        raise OpencodeToolError(f"go-direct unreachable ({type(e).__name__}): {e}")
    if resp.status_code == 401:
        raise OpencodeToolError("go-direct auth failed — check OPENCODE_API_KEY")
    if resp.status_code != 200:
        detail = ""
        try:
            err = resp.json()
            if isinstance(err, dict):
                inner = err.get("data") if isinstance(err.get("data"), dict) else err
                einfo = (inner.get("error") if isinstance(inner, dict)
                         and isinstance(inner.get("error"), dict) else None)
                if isinstance(einfo, dict):
                    detail = einfo.get("message", "") or ""
        except ValueError:
            pass
        raise OpencodeToolError(
            f"go-direct HTTP {resp.status_code} for {twin}"
            + (" — " + detail[:160] if detail else "")
        )
    try:
        body = resp.json()
    except ValueError:
        raise OpencodeToolError("go-direct returned non-JSON")
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        body = body["data"]
    if not isinstance(body, dict):
        raise OpencodeToolError("go-direct returned invalid body shape")
    echoed = body.get("model") or ""
    if echoed != twin:
        raise OpencodeToolError(
            f"go-direct model mismatch — asked {twin}, gateway ran "
            f"{echoed or 'UNKNOWN'}"
        )
    choices = body.get("choices") or []
    first = choices[0] if isinstance(choices, list) and choices else None
    msg = first.get("message") if isinstance(first, dict) else None
    text = ((msg.get("content") or msg.get("text") or "")
            if isinstance(msg, dict) else "")
    if not isinstance(text, str) or not text.strip():
        raise OpencodeToolError(f"go-direct {twin} produced no text")
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    pin = int(usage.get("prompt_tokens") or 0)
    pout = int(usage.get("completion_tokens") or 0)
    rate_in, rate_out = GO_RATES.get(twin, (0.0, 0.0))
    cost = (pin * rate_in + pout * rate_out) / 1_000_000.0
    logger.info(
        f"[CODE] go-direct {twin} tokens={pin}/{pout} cost={cost:.6f}"
    )
    return {
        "session_id": "",
        "prompt_message_id": "",
        "message_id": "",
        "text": text,
        "tokens": {"input": pin, "output": pout},
        "cost": cost,
        "model_id": twin,
        "provider_id": GO_PROVIDER_ID,
        "status": "done",
        "permissions_denied": [],
        "tier": "paid-go",
    }


def dispatch(brief: str, model_id: str = DEFAULT_MODEL_ID,
             agent: str = DEFAULT_AGENT,
             provider_id: str = DEFAULT_PROVIDER_ID,
             timeout: float = 600.0, poll_interval: float = 3.0,
             on_session=None, on_permission=None,
             permission_timeout: float = DEFAULT_PERMISSION_TIMEOUT,
             grace_after_deny: float = GRACE_AFTER_DENY,
             cancel=None, worker_budget: float = WORKER_BUDGET) -> dict:
    """Send a coding brief to a free zen agent; wait for the final answer.

    on_session(session_id, prompt_message_id) is called right after the
    prompt is admitted (before the agent finishes) so callers can track and
    stop the run mid-flight. on_permission(request) -> "once" | "reject"
    answers agent permission asks (must return within permission_timeout;
    without it, asks are rejected immediately). cancel (threading.Event)
    aborts the wait promptly.

    Worker reliability (cost rule): the free worker gets worker_budget
    seconds. On exhaustion (finish:"error", completed-empty-text, or no
    answer in budget — WorkerFailed only) dispatch retries ONCE via the
    DIRECT go endpoint (the serve's session API cannot resolve go twins).
    Model-matched twin or universal mimo-v2.5; answer-only turn, tier paid-go.
    DeepSeek (brain-only) is refused for muscle work outright. Auth/transport/
    cancel errors never trigger the paid retry. Returns the result shape
    (status "done" or "blocked", tier "free" | "paid-go").
    Blocks (sync httpx) — callers on the voice loop MUST run this off the
    hot path (asyncio.to_thread). Raises OpencodeToolError on failure.
    """
    if not brief or not brief.strip():
        raise OpencodeToolError("dispatch: empty brief")
    _reject_brain_muscle(model_id)
    try:
        return _attempt(brief, model_id, provider_id, agent,
                        worker_budget, poll_interval,
                        on_session, on_permission,
                        permission_timeout, grace_after_deny,
                        cancel, "free")
    except WorkerFailed as e:
        twin = _resolve_go_fallback(model_id)
        logger.info(
            f"[CODE] worker failed ({e.reason}), "
            f"fallback to go twin {twin} (direct go endpoint)"
        )
        try:
            result = _go_direct_attempt(brief, twin, timeout, cancel,
                                        session_id=e.session_id)
        except OpencodeToolError as ge:
            raise OpencodeToolError(
                f"opencode serve: paid go twin {twin} also failed: {ge}"
            )
        result["session_id"] = e.session_id
        result["prompt_message_id"] = e.prompt_message_id
        return result
        # NOTE: non-WorkerFailed OpencodeToolErrors from the free attempt
        # (auth/transport/cancel) propagate unchanged — honest errors.


PROPOSE_DIRECTIVE = (
    "Reply with a plan only: what you WOULD change (files + steps) and why. "
    "Do NOT edit, create, or delete any files."
)


def propose(brief: str, model_id: str = DEFAULT_MODEL_ID,
            agent: str = DEFAULT_AGENT,
            provider_id: str = DEFAULT_PROVIDER_ID,
            timeout: float = 600.0, poll_interval: float = 3.0,
            on_session=None, on_permission=None,
            permission_timeout: float = DEFAULT_PERMISSION_TIMEOUT,
            grace_after_deny: float = GRACE_AFTER_DENY,
            cancel=None, worker_budget: float = WORKER_BUDGET) -> dict:
    """Apply-gate step 1 (§6A.3): the agent proposes, nothing is applied.

    Same result shape with status "proposed" — the supervisor HOLDS it
    pending the user's yes/no. No worktree writes by construction.
    Inherits dispatch's free→direct-go-twin fallback (tier tagged in result).
    """
    if not brief or not brief.strip():
        raise OpencodeToolError("propose: empty brief")
    full = brief.strip() + "\n\n(" + PROPOSE_DIRECTIVE + ")"
    result = dispatch(full, model_id=model_id, agent=agent,
                      provider_id=provider_id, timeout=timeout,
                      poll_interval=poll_interval, on_session=on_session,
                      on_permission=on_permission,
                      permission_timeout=permission_timeout,
                      grace_after_deny=grace_after_deny, cancel=cancel,
                      worker_budget=worker_budget)
    if result.get("status") == "done":
        result["status"] = "proposed"
    return result


def followup(session_id: str, text: str,
             timeout: float = 600.0, poll_interval: float = 3.0,
             on_session=None, on_permission=None,
             permission_timeout: float = DEFAULT_PERMISSION_TIMEOUT,
             grace_after_deny: float = GRACE_AFTER_DENY,
             cancel=None) -> dict:
    """Apply-gate step 2 (§6A.3): continue a session after the user said yes.

    Sends a follow-up prompt in the SAME session and waits for the answer.
    Returns the result shape (status "done").
    """
    if not text or not text.strip():
        raise OpencodeToolError("followup: empty text")
    with _client(timeout) as client:
        prompt = _post(client, f"/api/session/{session_id}/prompt",
                       {"prompt": {"text": text.strip()}})
        prompt_message_id = prompt.get("id", "")
        logger.info(f"[CODE] followup admitted {prompt_message_id or '?'} "
                    f"in {session_id}")
        if on_session is not None:
            on_session(session_id, prompt_message_id)
        last, denied = _wait_for_answer(
            client, session_id, timeout, poll_interval,
            on_permission=on_permission, permission_timeout=permission_timeout,
            grace_after_deny=grace_after_deny, cancel=cancel)
        model = ((last or {}).get("model") or {})
        if last is None:
            return _blocked_result(client, session_id, prompt_message_id,
                                   denied, model.get("id", ""),
                                   model.get("providerID", ""))
        return _result_shape(last, session_id, prompt_message_id,
                             model.get("id", ""), model.get("providerID", ""),
                             "done", denied,
                             "paid-go" if model.get("providerID") == GO_PROVIDER_ID else "free")


def abort_session(session_id: str, timeout: float = 30.0) -> bool:
    """Abort an in-flight run: POST /session/:id/abort (verified in /doc).

    True if the abort landed; False if the session was already finished or
    absent (404/409 tolerated — nothing left to stop). Raises
    OpencodeToolError on transport/auth failure.
    """
    with _client(timeout) as client:
        url, _, _ = _config()
        try:
            resp = client.post(f"/session/{session_id}/abort", json={})
        except httpx.ConnectError:
            raise OpencodeToolError(
                f"opencode serve unreachable at {url} — is it running?"
            )
        except httpx.HTTPError as e:
            raise OpencodeToolError(
                f"opencode serve transport error: {type(e).__name__}"
            )
        if resp.status_code == 401:
            raise OpencodeToolError(
                "opencode serve auth failed — check "
                "OPENCODE_SERVER_USER/OPENCODE_SERVER_PASSWORD"
            )
        if resp.status_code in (404, 409):
            return False
        if resp.status_code >= 400:
            raise OpencodeToolError(
                f"opencode serve abort -> {resp.status_code}: {resp.text[:200]}"
            )
        return True


def stage_revert(session_id: str, prompt_message_id: str,
                 timeout: float = 30.0) -> dict:
    """Stage a revert of one turn: the USER prompt message is the turn root
    (verified: staging the assistant message stages nothing). Returns
    {messageID, snapshot, diff, files:[{path,status,...}]}."""
    with _client(timeout) as client:
        return _post(client, f"/api/session/{session_id}/revert/stage",
                     {"messageID": prompt_message_id})


def commit_revert(session_id: str, timeout: float = 60.0) -> None:
    """Commit the staged revert (restores the worktree)."""
    with _client(timeout) as client:
        url, _, _ = _config()
        try:
            resp = client.post(f"/api/session/{session_id}/revert/commit", json={})
        except httpx.ConnectError:
            raise OpencodeToolError(
                f"opencode serve unreachable at {url} — is it running?"
            )
        except httpx.HTTPError as e:
            raise OpencodeToolError(
                f"opencode serve transport error: {type(e).__name__}"
            )
        if resp.status_code == 401:
            raise OpencodeToolError(
                "opencode serve auth failed — check "
                "OPENCODE_SERVER_USER/OPENCODE_SERVER_PASSWORD"
            )
        if resp.status_code >= 400:
            raise OpencodeToolError(
                f"opencode serve revert/commit -> {resp.status_code}: "
                f"{resp.text[:200]}"
            )


def revert_decision(files: list) -> tuple:
    """Pure rule: commit the staged revert ONLY when every staged file has
    status "deleted" (created by the session — safe to remove). Returns
    (commit: bool, held: [paths]). Anything else is HELD for an explicit
    user decision, so a stop can never wipe collaborators' uncommitted work.
    """
    files = files or []
    if not files:
        return True, []
    held = [f.get("path", "?") for f in files if f.get("status") != "deleted"]
    if held:
        return False, held
    return True, []


def stop_session(session_id: str, prompt_message_id: str,
                 timeout: float = 30.0) -> dict:
    """Abort an in-flight run and conditionally revert partial changes.

    Returns {aborted, reverted, held_files} (see revert_decision for the
    commit rule). Raises OpencodeToolError on transport/auth failure.
    """
    aborted = abort_session(session_id, timeout=timeout)
    staged = stage_revert(session_id, prompt_message_id, timeout=timeout)
    files = staged.get("files") or []
    commit, held = revert_decision(files)
    if not commit:
        logger.warning(f"[CODE] stop holds revert for user decision: {held}")
        return {"aborted": aborted, "reverted": False, "held_files": held}
    if files:
        commit_revert(session_id, timeout=timeout)
        removed = [f.get("path", "?") for f in files]
        logger.info(f"[CODE] stop reverted session files: {removed}")
    return {"aborted": aborted, "reverted": True, "held_files": []}
