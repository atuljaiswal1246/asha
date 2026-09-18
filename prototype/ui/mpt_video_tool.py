"""MoneyPrinterTurbo (MPT) video generation client — an OPTIONAL local add-on.

MoneyPrinterTurbo is a separate local service (its own install under
``~/Library/Application Support/MoneyPrinterTurbo``). Asha only talks to its
HTTP API; it never vendors, installs or starts MPT.

Config comes from the environment (gitignored ``prototype/.env``):

  MONEYPRINTER_BASE_URL   default ``http://127.0.0.1:8080``
  MONEYPRINTER_API_KEY    optional; sent as ``x-api-key``. It must match the
                          ``[app] api_key`` in MPT's own ``config.toml``.
                          Empty = no auth (only safe on 127.0.0.1).

The key is auto-discovered so a user never has to paste it: an explicit
``MONEYPRINTER_API_KEY`` always wins, otherwise ``api_key()`` reads
``[app] api_key`` from MPT's own ``config.toml``. The config is located by
``MONEYPRINTER_CONFIG`` (a file path) or ``MONEYPRINTER_HOME`` (a directory)
when set, else ``~/Library/Application Support/MoneyPrinterTurbo``. A missing,
unreadable or malformed config simply yields no key — never a crash — and the
value is never logged.

Every function is BYOK and fail-soft: a failure returns (or raises
``MptError`` with) a short, honest sentence suitable for voice, never raw
provider text or a stack trace. If MPT is not running, ``healthy()`` is
``False`` and ``make_video()`` returns the "optional add-on" message instead of
an error — a normal state, not a fault.

Asha supplies the script and the footage keywords itself, so MPT needs no LLM
key of its own; the script is kept to a sentence or two.

Footage has to come from somewhere: the default render path is stock footage via
Pexels, so MPT needs a free Pexels key in its own ``config.toml``
(``pexels_api_keys``). When that key is missing the add-on cannot render, and
``make_video()`` says so in one honest sentence instead of silently failing.
The alternative is local footage: put clips in MPT's
``storage/local_videos/`` folder and set ``video_source = "local"`` in MPT's
``config.toml`` — an explicit, opt-in choice that never changes the default.
``source="local"`` may also be passed directly to ``make_video()``.

Renders are long, so ``make_video()`` never blocks a voice turn: it spawns the
job, returns an ack, and leaves polling to ``watch()`` (driven by the server,
which speaks the result when it lands). The wait is bounded by
``MONEYPRINTER_JOB_TIMEOUT`` (default 15 min); on timeout the user is told.

Output is saved under ``static/generated/`` (served by the :8000 static server).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Awaitable, Callable

import httpx
from pipecat.adapters.schemas.function_schema import FunctionSchema

try:  # Python 3.11+ (the runtime venv is 3.13); degrades to "no config" if absent.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


_GENERATED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "generated")
_JOBS_PATH = os.path.join(_GENERATED_DIR, ".mpt_jobs.json")

_DEFAULT_BASE = "http://127.0.0.1:8080"
_MPT_HOME_ENV = "MONEYPRINTER_HOME"
_MPT_CONFIG_ENV = "MONEYPRINTER_CONFIG"
_DEFAULT_MPT_HOME = "~/Library/Application Support/MoneyPrinterTurbo"
_HEALTH_TIMEOUT = float(os.environ.get("MONEYPRINTER_HEALTH_TIMEOUT", "3"))
_HTTP_TIMEOUT = float(os.environ.get("MONEYPRINTER_HTTP_TIMEOUT", "30"))
_POLL_INTERVAL = float(os.environ.get("MONEYPRINTER_POLL_INTERVAL", "5"))
# Documented total wait before the job is declared timed out. A 15-minute render
# window covers a normal stock-footage + Edge TTS video on this Mac.
_JOB_TIMEOUT = float(os.environ.get("MONEYPRINTER_JOB_TIMEOUT", "900"))

_STATE_FAILED = -1
_STATE_COMPLETE = 1

_DEFAULT_ASPECT = "9:16"
_DEFAULT_VOICE = "en-US-AriaNeural"

# Footage sources. Stock footage is the default; local footage is opt-in only
# (via MPT's own ``video_source = "local"`` or an explicit ``source`` argument),
# never a silent fallback.
_DEFAULT_SOURCE = "pexels"
_LOCAL_SOURCE = "local"
# The stock providers MPT supports, mapped to the config field holding the keys.
_STOCK_KEY_FIELDS = {
    "pexels": "pexels_api_keys",
    "pixabay": "pixabay_api_keys",
    "coverr": "coverr_api_keys",
}
# Extensions MPT's ``storage/local_videos/`` accepts (matches its upload API).
_LOCAL_MATERIAL_EXTS = (
    ".mp4", ".mov", ".avi", ".flv", ".mkv", ".webm",
    ".jpg", ".jpeg", ".png", ".bmp",
)

_ERROR_PREFIX = "[video error]"

# The exact lines the user hears. The ack is returned the moment the job is
# spawned; the completion line is what the server asks the brain to speak.
ACK_MSG = "On it — I'll tell you when it's ready."
# Only ever returned when MPT is genuinely absent (healthy() is False).
NOT_INSTALLED_MSG = (
    "Video generation is an optional add-on that isn't installed or running on "
    "this Mac, so I can't make videos yet — and I haven't installed anything "
    "myself. If you'd like it, I can walk you through the one-time setup: run "
    "MoneyPrinterTurbo locally and give it a footage key such as a free Pexels key."
)
# MPT is reachable, but the default (stock footage) path has no provider key.
# Returned as plain speech, not an error: the user just hasn't finished setup.
NO_FOOTAGE_MSG = (
    "The video add-on is running, but it has no footage source set up yet, so I "
    "can't start a video. Add a free Pexels API key in the add-on's settings, or "
    "put a few clips in its local footage folder and tell it to use local footage."
)
# The user (or MPT's config) opted into local footage, but no clips are there.
NO_LOCAL_FOOTAGE_MSG = (
    "The video add-on is set to use local footage, but its local footage folder "
    "is empty, so I can't start a video. Add a few video clips there and ask me "
    "again."
)
# The remaining failure lines are each spoken as-is when that path is hit.
KEY_REJECTED_MSG = (
    "The video add-on is running, but it rejected the key Asha sent, so I can't "
    "start a render until the add-on's API key and Asha's copy match."
)
BUSY_MSG = (
    "The video add-on is busy — its render queue is full — so nothing was "
    "started. Try again in a minute."
)
ADDON_OFFLINE_MSG = (
    "The video add-on isn't answering right now, so I couldn't start the render. "
    "If it was just stopped, start it again and ask me once more."
)
ADDON_SLOW_MSG = (
    "The video add-on didn't answer in time, so I couldn't start the render. It "
    "may still be starting up — try again shortly."
)
DOWNLOAD_FAILED_MSG = (
    "The render finished, but I couldn't download the video file from the "
    "add-on, so there's nothing to show yet."
)
RENDER_FAILED_MSG = (
    "The video add-on started the render but it failed during the {stage} stage, "
    "so there's no video. It may need a quick settings check."
)
RENDER_TIMEOUT_MSG = (
    "The video is taking longer than {minutes} minutes, so I stopped waiting. It "
    "may still finish on the add-on — ask me again in a bit."
)


class MptError(Exception):
    """A short, user-actionable MPT failure. Never raw provider text."""


def completion_line(topic: str, path: str) -> str:
    """The sentence the brain speaks when a render lands."""
    return f"Your video is ready — {topic}. It's saved at {path}."


# ---------------------------------------------------------------------------
# Config / HTTP helpers
# ---------------------------------------------------------------------------

def _base() -> str:
    return (os.environ.get("MONEYPRINTER_BASE_URL") or _DEFAULT_BASE).rstrip("/")


def _mpt_home() -> str:
    """MPT's data dir: ``MONEYPRINTER_HOME`` when set, else the macOS default."""
    override = (os.environ.get(_MPT_HOME_ENV) or "").strip()
    return os.path.expanduser(override or _DEFAULT_MPT_HOME)


def _config_paths() -> list[str]:
    """Candidate ``config.toml`` paths, most specific first."""
    paths: list[str] = []
    explicit = (os.environ.get(_MPT_CONFIG_ENV) or "").strip()
    if explicit:
        paths.append(os.path.expanduser(explicit))
    paths.append(os.path.join(_mpt_home(), "config.toml"))
    return paths


def _key_from_config() -> str:
    """Read ``[app] api_key`` from MPT's own config.toml.

    Missing, unreadable or malformed config all mean "no discovered key" — the
    common, harmless case when MPT runs unauthenticated on 127.0.0.1. The value
    is returned to the caller only and never logged.
    """
    if tomllib is None:  # pragma: no cover — 3.11+ always has it
        return ""
    for path in _config_paths():
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except (OSError, tomllib.TOMLDecodeError, ValueError):
            continue
        app = data.get("app") if isinstance(data, dict) else None
        if not isinstance(app, dict):
            continue
        key = app.get("api_key")
        if isinstance(key, str) and key.strip():
            return key.strip()
    return ""


def api_key() -> str:
    """The MPT API key: explicit env first, then auto-discovered from config.

    Returns ``""`` when neither is available (MPT's unauthenticated mode).
    """
    env_key = (os.environ.get("MONEYPRINTER_API_KEY") or "").strip()
    if env_key:
        return env_key
    return _key_from_config()


def _config_app() -> dict | None:
    """The ``[app]`` table from MPT's config.toml, or ``None`` when unavailable.

    Reading only; secrets are never logged. ``None`` means "could not verify",
    which callers treat as "don't block" so a non-standard MPT layout never
    causes a false "not configured" answer.
    """
    if tomllib is None:  # pragma: no cover — 3.11+ always has it
        return None
    for path in _config_paths():
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except (OSError, tomllib.TOMLDecodeError, ValueError):
            continue
        app = data.get("app") if isinstance(data, dict) else None
        if isinstance(app, dict):
            return app
    return None


def _nonempty_keys(value: Any) -> list[str]:
    """Normalise a provider key field to the non-empty keys it holds."""
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        return []
    return [p.strip() for p in parts if p.strip()]


def local_footage_dir() -> str:
    """MPT's local footage folder (where its upload API stores clips)."""
    return os.path.join(_mpt_home(), "storage", "local_videos")


def local_footage_files() -> list[str]:
    """Names of usable clips in MPT's local footage folder. Never raises."""
    try:
        names = os.listdir(local_footage_dir())
    except OSError:
        return []
    return sorted(
        name for name in names
        if not name.startswith(".")
        and os.path.splitext(name)[1].lower() in _LOCAL_MATERIAL_EXTS
    )


def resolve_source(explicit: str = "") -> str:
    """Which footage source to use, without ever changing the default silently.

    An explicit argument wins; otherwise MPT's own config may opt in to local
    footage (``video_source = "local"``); otherwise the stock default applies.
    """
    chosen = (explicit or "").strip().lower()
    if chosen:
        return chosen
    app = _config_app() or {}
    configured = str(app.get("video_source") or "").strip().lower()
    return _LOCAL_SOURCE if configured == _LOCAL_SOURCE else _DEFAULT_SOURCE


def footage_available(source: str) -> bool:
    """Whether *source* is usable, as far as MPT's config lets us tell.

    Local footage means clips exist on disk. For stock providers, an
    unreadable/absent config is treated as "available" (fail open) so we only
    report the missing-key condition when we have actually verified it.
    """
    if source == _LOCAL_SOURCE:
        return bool(local_footage_files())
    app = _config_app()
    if app is None:
        return True
    field = _STOCK_KEY_FIELDS.get(source)
    if field is None:
        return True
    return bool(_nonempty_keys(app.get(field)))


def _headers() -> dict:
    key = api_key()
    headers = {"accept": "application/json", "content-type": "application/json"}
    if key:
        headers["x-api-key"] = key
    return headers


def _http_error(status: int, where: str) -> MptError:
    if status in (401, 403):
        return MptError(KEY_REJECTED_MSG)
    if status == 429:
        return MptError(BUSY_MSG)
    return MptError(
        f"The video add-on returned an unexpected error (HTTP {status}) while {where}."
    )


def _data(payload: Any) -> dict:
    """Pull ``data`` out of MPT's ``{status, data, ...}`` envelope."""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        # Be forgiving: some builds return the object unwrapped.
        return payload
    return {}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

async def healthy(transport: httpx.AsyncBaseTransport | None = None) -> bool:
    """True when MPT answers ``GET /ping`` with ``pong``. Never raises."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_HEALTH_TIMEOUT), transport=transport
        ) as client:
            resp = await client.get(f"{_base()}/ping")
            return resp.status_code == 200 and "pong" in (resp.text or "").lower()
    except Exception:  # noqa: BLE001 — down is a normal state
        return False


_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with",
    "about", "into", "from", "at", "by", "is", "are", "was", "were", "be",
    "this", "that", "these", "those", "it", "its", "as", "how", "why",
    "make", "made", "video", "short", "please", "me", "my", "your",
})


def derive_terms(topic: str, script: str = "", limit: int = 6) -> str:
    """Best-effort footage keywords from the topic/script when none are given."""
    text = f"{topic} {script}".lower()
    words: list[str] = []
    for raw in text.replace(",", " ").replace(".", " ").split():
        w = "".join(ch for ch in raw if ch.isalnum())
        if len(w) < 3 or w in _STOPWORDS or w in words:
            continue
        words.append(w)
        if len(words) >= limit:
            break
    return ",".join(words)


def _payload(
    topic: str, script: str, terms: str, aspect: str, voice: str, source: str
) -> dict:
    body = {
        "video_subject": topic,
        "video_script": (script or topic).strip(),
        "video_terms": terms,
        "video_source": source,
        "video_aspect": aspect or _DEFAULT_ASPECT,
        "voice_name": voice or _DEFAULT_VOICE,
        "subtitle_enabled": True,
        "bgm_type": "random",
        "video_count": 1,
    }
    if source == _LOCAL_SOURCE:
        # MPT resolves these names inside its own ``storage/local_videos``.
        body["video_materials"] = [
            {"provider": _LOCAL_SOURCE, "url": name, "duration": 0}
            for name in local_footage_files()
        ]
    return body


async def spawn(
    topic: str,
    script: str = "",
    terms: str = "",
    aspect: str = _DEFAULT_ASPECT,
    voice: str = _DEFAULT_VOICE,
    *,
    source: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Queue an MPT render and return its task id. Raises ``MptError``.

    Asha passes *script* and *terms* so MPT never needs its own LLM key. The
    footage *source* defaults to the stock path (Pexels); use ``"local"`` only
    when the user has explicitly opted in and clips exist.
    """
    topic = (topic or "").strip()
    if not topic:
        raise MptError("make_video needs a topic.")
    resolved = resolve_source(source)
    if resolved == _LOCAL_SOURCE and not local_footage_files():
        raise MptError(NO_LOCAL_FOOTAGE_MSG)
    body = _payload(
        topic, script, terms or derive_terms(topic, script), aspect, voice, resolved
    )
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_HTTP_TIMEOUT), transport=transport
        ) as client:
            resp = await client.post(
                f"{_base()}/api/v1/videos", headers=_headers(), json=body
            )
    except httpx.TimeoutException:
        raise MptError(ADDON_SLOW_MSG)
    except Exception:  # noqa: BLE001 — down/unreachable is a normal state
        raise MptError(ADDON_OFFLINE_MSG)
    if resp.status_code in (401, 403, 429):
        raise _http_error(resp.status_code, "starting the render")
    if resp.status_code >= 400:
        raise _http_error(resp.status_code, "starting the render")
    task_id = str(_data(resp.json()).get("task_id") or "").strip()
    if not task_id:
        raise MptError(
            "The video add-on accepted the job but didn't return a task id, so "
            "I can't track the render."
        )
    return task_id


async def status(
    task_id: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Fetch one task's progress. Raises ``MptError`` on failure."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_HTTP_TIMEOUT), transport=transport
        ) as client:
            resp = await client.get(
                f"{_base()}/api/v1/tasks/{task_id}", headers=_headers()
            )
    except httpx.TimeoutException:
        raise MptError(ADDON_OFFLINE_MSG)
    except Exception:  # noqa: BLE001 — down/unreachable is a normal state
        raise MptError(ADDON_OFFLINE_MSG)
    if resp.status_code in (401, 403, 429):
        raise _http_error(resp.status_code, "checking the render")
    if resp.status_code == 404:
        raise MptError(
            "The video add-on no longer knows about that render — it may have "
            "restarted and lost the job, so try making it again."
        )
    if resp.status_code >= 400:
        raise _http_error(resp.status_code, "checking the render")
    return _data(resp.json())


async def download(
    task_id: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Download ``final-1.mp4``, save under ``static/generated/``, return its
    served path. Raises ``MptError`` on failure."""
    safe_id = "".join(ch for ch in str(task_id) if ch.isalnum() or ch in ("-", "_"))
    if not safe_id:
        raise MptError("invalid video add-on task id.")
    os.makedirs(_GENERATED_DIR, exist_ok=True)
    dest = os.path.join(_GENERATED_DIR, f"mpt-{safe_id}.mp4")
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return f"/generated/mpt-{safe_id}.mp4"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_HTTP_TIMEOUT), transport=transport
        ) as client:
            resp = await client.get(
                f"{_base()}/api/v1/download/{task_id}/final-1.mp4",
                headers=_headers(),
            )
    except httpx.TimeoutException:
        raise MptError(DOWNLOAD_FAILED_MSG)
    except Exception:  # noqa: BLE001 — down/unreachable is a normal state
        raise MptError(ADDON_OFFLINE_MSG)
    if resp.status_code in (401, 403, 429):
        raise _http_error(resp.status_code, "downloading the video")
    if resp.status_code == 404:
        raise MptError(DOWNLOAD_FAILED_MSG)
    if resp.status_code >= 400:
        raise _http_error(resp.status_code, "downloading the video")
    data = resp.content
    if not data:
        raise MptError(DOWNLOAD_FAILED_MSG)
    with open(dest, "wb") as f:
        f.write(data)
    return f"/generated/mpt-{safe_id}.mp4"


def _clean_stage(task: dict) -> str:
    """A short, safe stage name from MPT's status, defaulting to 'render'."""
    raw = str(task.get("failed_stage") or "").strip()
    if raw and len(raw) <= 24 and raw.replace("_", "").replace("-", "").isalnum():
        return raw
    return "render"


async def wait_for_video(
    task_id: str,
    *,
    topic: str = "",
    timeout: float | None = None,
    interval: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    on_progress: Callable[[str], Awaitable[None] | None] | None = None,
) -> str:
    """Poll until the render completes, then download it and return its path.

    Bounded by *timeout* (default ``MONEYPRINTER_JOB_TIMEOUT``, 15 min); on
    timeout it raises ``MptError`` telling the user the render took too long.
    """
    deadline = time.monotonic() + (timeout if timeout is not None else _JOB_TIMEOUT)
    every = interval if interval is not None else _POLL_INTERVAL
    while time.monotonic() < deadline:
        st = await status(task_id, transport=transport)
        state = st.get("state")
        if state == _STATE_COMPLETE:
            return await download(task_id, transport=transport)
        if state == _STATE_FAILED:
            raise MptError(RENDER_FAILED_MSG.format(stage=_clean_stage(st)))
        if on_progress is not None:
            try:
                out = on_progress(f"{str(topic or 'video')}: {st.get('progress', 0)}%")
                if hasattr(out, "__await__"):
                    await out
            except Exception:  # noqa: BLE001 — progress is cosmetic
                pass
        await asyncio.sleep(every)
    waited = timeout if timeout is not None else _JOB_TIMEOUT
    raise MptError(RENDER_TIMEOUT_MSG.format(minutes=max(1, int(waited // 60))))


# ---------------------------------------------------------------------------
# Background job persistence (best-effort; enables resume after a restart)
# ---------------------------------------------------------------------------

def _load_jobs() -> dict:
    try:
        with open(_JOBS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — no file / corrupt = empty
        return {}


def _save_jobs(jobs: dict) -> None:
    try:
        os.makedirs(_GENERATED_DIR, exist_ok=True)
        tmp = _JOBS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(jobs, f)
        os.replace(tmp, _JOBS_PATH)
    except Exception:  # noqa: BLE001 — persistence must never break a job
        pass


def register_job(task_id: str, topic: str) -> None:
    """Persist a running job so an Asha restart can resume polling it."""
    if not task_id:
        return
    jobs = _load_jobs()
    jobs[task_id] = {"task_id": task_id, "topic": topic, "started": time.time()}
    _save_jobs(jobs)


def clear_job(task_id: str) -> None:
    jobs = _load_jobs()
    if jobs.pop(task_id, None) is not None:
        _save_jobs(jobs)


def pending_jobs() -> list[dict]:
    """Jobs persisted by a previous run (for resume). Never raises."""
    return list(_load_jobs().values())


# ---------------------------------------------------------------------------
# Background watcher (server-driven; does the speaking via on_done)
# ---------------------------------------------------------------------------

async def watch(
    task_id: str,
    topic: str,
    on_done: Callable[[bool, str, str], Awaitable[None] | None],
    *,
    timeout: float | None = None,
    interval: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    on_progress: Callable[[str], Awaitable[None] | None] | None = None,
) -> None:
    """Poll one render in the background and hand the outcome to *on_done*.

    ``on_done(ok, detail, topic)`` gets the served path on success or a short
    failure reason otherwise. Never raises.
    """
    try:
        path = await wait_for_video(
            task_id, topic=topic, timeout=timeout, interval=interval,
            transport=transport, on_progress=on_progress,
        )
        clear_job(task_id)
        await _call(on_done, True, path, topic)
    except MptError as exc:
        clear_job(task_id)
        await _call(on_done, False, str(exc), topic)
    except Exception as exc:  # noqa: BLE001 — watcher must never crash the loop
        clear_job(task_id)
        await _call(on_done, False, f"{_ERROR_PREFIX} {exc}", topic)


async def _call(cb, *args) -> None:
    try:
        out = cb(*args)
        if hasattr(out, "__await__"):
            await out
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Tool entry point (shared by both tiers)
# ---------------------------------------------------------------------------

async def make_video(
    topic: str,
    aspect: str = _DEFAULT_ASPECT,
    voice: str = _DEFAULT_VOICE,
    script: str = "",
    terms: str = "",
    *,
    source: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
    on_done: Callable[[bool, str, str], Awaitable[None] | None] | None = None,
    on_progress: Callable[[str], Awaitable[None] | None] | None = None,
    timeout: float | None = None,
    interval: float | None = None,
) -> str:
    """Start a video in the background; return immediately with an ack.

    Returns ``NOT_INSTALLED_MSG`` when MPT is down (never an error), an honest
    sentence when the footage source is not set up, the ack once the job is
    queued, or a short ``[video error] ...`` string on failure. When *on_done*
    is given a watcher is started that polls to completion.
    """
    topic = (topic or "").strip()
    if not topic:
        return f"{_ERROR_PREFIX} what should the video be about?"
    if not await healthy(transport=transport):
        return NOT_INSTALLED_MSG
    resolved = resolve_source(source)
    if not footage_available(resolved):
        if resolved == _LOCAL_SOURCE:
            return NO_LOCAL_FOOTAGE_MSG
        return NO_FOOTAGE_MSG
    try:
        task_id = await spawn(
            topic, script=script, terms=terms, aspect=aspect, voice=voice,
            source=resolved, transport=transport,
        )
    except MptError as exc:
        return f"{_ERROR_PREFIX} {exc}"
    register_job(task_id, topic)
    if on_done is not None:
        try:
            asyncio.get_event_loop().create_task(
                watch(task_id, topic, on_done, timeout=timeout,
                      interval=interval, transport=transport,
                      on_progress=on_progress)
            )
        except Exception:  # noqa: BLE001 — no loop: job still runs, just unwatched
            pass
    return ACK_MSG


_make_video_schema = FunctionSchema(
    name="make_video",
    description=(
        "Make a short stock-footage video from a topic. Returns at once — the "
        "finished video arrives later, so tell the user you're on it. Always "
        "supply a one-or-two sentence script and a few footage keywords "
        "(comma-separated) so no extra service is needed."
    ),
    properties={
        "topic": {
            "type": "string",
            "description": "What the video is about (required)",
        },
        "script": {
            "type": "string",
            "description": "One or two spoken sentences to narrate the video",
        },
        "terms": {
            "type": "string",
            "description": "3-6 comma-separated keywords for stock footage",
        },
        "aspect": {
            "type": "string",
            "enum": ["9:16", "16:9", "1:1"],
            "description": "Video shape (default 9:16 vertical)",
            "default": "9:16",
        },
        "voice": {
            "type": "string",
            "description": "Edge TTS voice name (default en-US-AriaNeural)",
            "default": "en-US-AriaNeural",
        },
    },
    required=["topic"],
)


async def handle_make_video(args: dict[str, Any]) -> str:
    """Dispatch helper — raw tool-call args -> ack/error string."""
    return await make_video(
        (args.get("topic") or ""),
        aspect=(args.get("aspect") or _DEFAULT_ASPECT),
        voice=(args.get("voice") or _DEFAULT_VOICE),
        script=(args.get("script") or ""),
        terms=(args.get("terms") or ""),
        source=(args.get("source") or ""),
    )
