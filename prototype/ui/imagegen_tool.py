"""Image generation tool — Leonardo.Ai (BYOK) + pipecat schema.

Provider is chosen by env (default: leonardo):

  IMAGE_PROVIDER=leonardo | pollinations      (default leonardo)

Leonardo (Bring-Your-Own-Key — never hardcode a key):
  LEONARDO_API_KEY    required for Leonardo
  LEONARDO_MODEL_ID   optional (default b24e16ff-06e3-43eb-8d33-4416c2d75876)
  LEONARDO_BASE_URL   optional (default https://cloud.leonardo.ai/api/rest/v1)
  LEONARDO_ALCHEMY    optional "1"/"0" (default 1)

Pollinations (free, keyless) is the free fallback. Resolution order is
**paid-first, free-fallback** (user, 2026-09-18): the paid provider is tried
first; if it fails for ANY reason (no key, HTTP error, quota/429, timeout,
generation failure) the free provider is used instead, and the result carries a
plain note saying the free model was used. Set ``IMAGE_FREE_FALLBACK=0`` to turn
the automatic fallback off. ``IMAGE_PROVIDER=pollinations`` still forces the
free provider alone (no paid call, no "allowance" note).

Images are saved under static/generated/ so the :8000 static server serves them
straight into chat bubbles. Zero side effects on import.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any
from urllib.parse import quote_plus

import httpx
from pipecat.adapters.schemas.function_schema import FunctionSchema


_GENERATED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "generated")
_ERROR_PREFIX = "[imagegen error]"

# Speakable outcomes for the assistant (never a stack trace, never a lie).
_FREE_FALLBACK_NOTE = "I used the free image model - the paid allowance is used up."
_BOTH_FAILED_MSG = (
    "I couldn't generate the image just now - both the paid and free image "
    "models are unavailable. Please try again in a moment."
)
_PAID_FAILED_MSG = (
    "I couldn't generate the image just now - the paid image model is "
    "unavailable and the free fallback is turned off."
)
_FREE_FAILED_MSG = (
    "I couldn't generate the image just now - the free image model is "
    "unavailable. Please try again in a moment."
)
_EMPTY_PROMPT_MSG = "I need a description to make an image - tell me what you would like to see."


_DEFAULT_LEONARDO_BASE = "https://cloud.leonardo.ai/api/rest/v1"
_DEFAULT_LEONARDO_MODEL = "b24e16ff-06e3-43eb-8d33-4416c2d75876"  # Leonardo platform default
_LEONARDO_POLL_TIMEOUT = float(os.environ.get("LEONARDO_POLL_TIMEOUT", "90"))
_LEONARDO_POLL_INTERVAL = float(os.environ.get("LEONARDO_POLL_INTERVAL", "3"))


def _clamp_dim(v: Any, default: int = 512) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = default
    n = max(128, min(n, 1024))
    return n - (n % 8)  # Leonardo wants multiples of 8


def _provider() -> str:
    return (os.environ.get("IMAGE_PROVIDER") or "leonardo").strip().lower()


def _free_fallback_enabled() -> bool:
    """Automatic paid->free fallback is ON unless explicitly disabled."""
    return (os.environ.get("IMAGE_FREE_FALLBACK", "1").strip().lower()
            not in ("0", "false", "no", "off"))


def _with_free_note(path: str) -> str:
    """Attach the free-model note to a successful image path.

    The note rides in a URL fragment: the :8000 static server (and browsers)
    ignore it, so the image still renders, while the words reach the assistant
    through the tool result. This is the only place a note is added — a paid
    result is returned bare, so the paid model is never implied on a free run.
    """
    return f"{path}#{_FREE_FALLBACK_NOTE}"


def _save_image(data: bytes, url_hint: str = "") -> str:
    """Write bytes under static/generated and return the served path."""
    ext = os.path.splitext(url_hint.split("?", 1)[0])[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    os.makedirs(_GENERATED_DIR, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(_GENERATED_DIR, name), "wb") as f:
        f.write(data)
    return f"/generated/{name}"


async def generate_image(
    prompt: str,
    width: int = 512,
    height: int = 512,
    seed: int = -1,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Generate an image, save it locally, return its served URL path.

    Paid-first, free-fallback: Leonardo is tried first; if it fails for any
    reason and the free fallback is enabled, Pollinations is used and the
    result carries ``_FREE_FALLBACK_NOTE``. Explicit ``IMAGE_PROVIDER=
    pollinations`` uses free alone. Never raises.

    Returns e.g. ``/generated/<uuid>.jpg`` on success, or one plain speakable
    sentence when no provider could make the image.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return _EMPTY_PROMPT_MSG
    width = _clamp_dim(width)
    height = _clamp_dim(height)

    if _provider() == "pollinations":
        free_only = await _pollinations(prompt, width, height, seed, transport=transport)
        if not free_only.startswith(_ERROR_PREFIX):
            return free_only
        return _FREE_FAILED_MSG

    paid = await _leonardo(prompt, width, height, transport=transport)
    if not paid.startswith(_ERROR_PREFIX):
        return paid  # paid succeeded: bare path, no free note
    if not _free_fallback_enabled():
        return _PAID_FAILED_MSG

    free = await _pollinations(prompt, width, height, seed, transport=transport)
    if not free.startswith(_ERROR_PREFIX):
        return _with_free_note(free)
    return _BOTH_FAILED_MSG


# ---------------------------------------------------------------------------
# Leonardo.Ai
# ---------------------------------------------------------------------------

async def _leonardo(
    prompt: str, width: int, height: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    key = (os.environ.get("LEONARDO_API_KEY") or "").strip()
    if not key:
        return (
            f"{_ERROR_PREFIX} no Leonardo API key set. Add LEONARDO_API_KEY to "
            "prototype/.env, or set IMAGE_PROVIDER=pollinations for the free backend."
        )
    base = (os.environ.get("LEONARDO_BASE_URL") or _DEFAULT_LEONARDO_BASE).rstrip("/")
    model = (os.environ.get("LEONARDO_MODEL_ID") or _DEFAULT_LEONARDO_MODEL).strip()
    alchemy = (os.environ.get("LEONARDO_ALCHEMY", "1").strip() not in ("0", "false", "False"))
    headers = {
        "authorization": f"Bearer {key}",
        "accept": "application/json",
        "content-type": "application/json",
    }
    payload = {
        "prompt": prompt,
        "modelId": model,
        "width": width,
        "height": height,
        "num_images": 1,
        "alchemy": alchemy,
        "public": False,
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0), transport=transport
        ) as client:
            create = await client.post(f"{base}/generations", headers=headers, json=payload)
            if create.status_code in (401, 403):
                return f"{_ERROR_PREFIX} Leonardo rejected the API key (HTTP {create.status_code})."
            if create.status_code >= 400:
                return f"{_ERROR_PREFIX} Leonardo create failed: HTTP {create.status_code} {create.text[:180]}"
            gen_id = ((create.json() or {}).get("sdGenerationJob") or {}).get("generationId")
            if not gen_id:
                return f"{_ERROR_PREFIX} Leonardo returned no generation id"
            img_url = await _leonardo_poll(client, base, headers, gen_id)
            if not img_url:
                return f"{_ERROR_PREFIX} Leonardo generation did not complete in time"
            dl = await client.get(img_url)
            if dl.status_code != 200 or "image" not in dl.headers.get("content-type", ""):
                return f"{_ERROR_PREFIX} could not download generated image (HTTP {dl.status_code})"
            data = dl.content
    except httpx.TimeoutException:
        return f"{_ERROR_PREFIX} Leonardo request timed out"
    except Exception as exc:  # noqa: BLE001 — tool must never raise
        return f"{_ERROR_PREFIX} {exc}"

    if not data or len(data) < 512:
        return f"{_ERROR_PREFIX} empty image response"
    try:
        return _save_image(data, img_url)
    except Exception as exc:  # noqa: BLE001
        return f"{_ERROR_PREFIX} cannot save image: {exc}"


async def _leonardo_poll(
    client: httpx.AsyncClient, base: str, headers: dict, gen_id: str
) -> str | None:
    """Poll GET /generations/{id} until COMPLETE; return the first image URL."""
    deadline = time.monotonic() + _LEONARDO_POLL_TIMEOUT
    await asyncio.sleep(_LEONARDO_POLL_INTERVAL)
    while time.monotonic() < deadline:
        try:
            resp = await client.get(f"{base}/generations/{gen_id}", headers=headers)
        except Exception:  # noqa: BLE001
            await asyncio.sleep(_LEONARDO_POLL_INTERVAL)
            continue
        if resp.status_code == 200:
            pk = ((resp.json() or {}).get("generations_by_pk") or {})
            status = (pk.get("status") or "").upper()
            if status == "COMPLETE":
                for img in (pk.get("generated_images") or []):
                    if img.get("url"):
                        return img["url"]
                return None
            if status == "FAILED":
                return None
        await asyncio.sleep(_LEONARDO_POLL_INTERVAL)
    return None


# ---------------------------------------------------------------------------
# Pollinations (free, keyless fallback)
# ---------------------------------------------------------------------------

async def _pollinations(
    prompt: str, width: int, height: int, seed: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    if seed is None or int(seed) < 0:
        seed = int(uuid.uuid4().int % 10_000_000)
    url = (
        f"https://image.pollinations.ai/prompt/{quote_plus(prompt)}"
        f"?width={width}&height={height}&seed={seed}&model=flux&nologo=true"
    )
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(120.0), transport=transport
        ) as client:
            resp = await client.get(url)
            ctype = resp.headers.get("content-type", "")
            if resp.status_code != 200 or "image" not in ctype:
                return f"{_ERROR_PREFIX} HTTP {resp.status_code} ({resp.text[:160]})"
            data = resp.content
    except httpx.TimeoutException:
        return f"{_ERROR_PREFIX} timed out generating image"
    except Exception as exc:  # noqa: BLE001
        return f"{_ERROR_PREFIX} {exc}"
    if not data or len(data) < 1024:
        return f"{_ERROR_PREFIX} empty image response"
    try:
        return _save_image(data, ".jpg")
    except Exception as exc:  # noqa: BLE001
        return f"{_ERROR_PREFIX} cannot save image: {exc}"


_generate_image_schema = FunctionSchema(
    name="generate_image",
    description=(
        "Generate an image from a text description and show it in the chat. "
        "Use when the user asks for a picture, drawing, illustration, or "
        "visual. Describe the subject concretely in the prompt."
    ),
    properties={
        "prompt": {
            "type": "string",
            "description": "Concrete visual description of the image to generate",
        },
        "width": {
            "type": "integer",
            "description": "Image width in pixels, 128-1024 (default 512)",
            "default": 512,
        },
        "height": {
            "type": "integer",
            "description": "Image height in pixels, 128-1024 (default 512)",
            "default": 512,
        },
    },
    required=["prompt"],
)


async def handle_generate_image(args: dict[str, Any]) -> str:
    """Dispatch helper — takes raw tool-call *args* dict, returns text."""
    prompt = args.get("prompt", "")
    try:
        width = int(args.get("width", 512))
    except (TypeError, ValueError):
        width = 512
    try:
        height = int(args.get("height", 512))
    except (TypeError, ValueError):
        height = 512
    return await generate_image(prompt, width=width, height=height)
