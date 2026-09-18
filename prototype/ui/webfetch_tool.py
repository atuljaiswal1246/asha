"""Web-fetch tool — pipecat FunctionSchema + async handler.

Provides a single-page text fetcher for the LLM tool layer.
Zero side effects on import.
"""

from __future__ import annotations

import re
import html
from typing import Any

import httpx
from pipecat.adapters.schemas.function_schema import FunctionSchema


# ---------------------------------------------------------------------------
# Core fetcher
# ---------------------------------------------------------------------------

async def web_fetch(url: str, max_chars: int = 6000) -> str:
    """Fetch *url* and return its readable text, truncated to *max_chars*.

    • Uses httpx with a 10 s timeout, redirect-following, desktop User-Agent.
    • Accepts only ``text/html``; returns an error string otherwise.
    • Strips <script>, <style>, <nav> blocks, collapses whitespace, un-escapes
      HTML entities, and truncates to *max_chars* characters.
    • **Never raises** — every failure path returns a human-readable error string.
    """
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(10.0),
            headers={"User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except httpx.TimeoutException:
        return f"[web_fetch error] Timeout fetching {url}"
    except httpx.HTTPStatusError as exc:
        return f"[web_fetch error] HTTP {exc.response.status_code} for {url}"
    except Exception as exc:
        return f"[web_fetch error] {exc}"

    # --- content-type gate ------------------------------------------------
    content_type = resp.headers.get("content-type", "")
    if "text/html" not in content_type:
        return (
            f"[web_fetch error] Non-HTML content-type '{content_type}' — "
            f"cannot extract readable text from {url}"
        )

    text = resp.text

    # --- strip unwanted blocks --------------------------------------------
    text = re.sub(r"<script\b[^>]*>.*?</script>", "", text, flags=re.S | re.I)
    text = re.sub(r"<style\b[^>]*>.*?</style>", "", text, flags=re.S | re.I)
    text = re.sub(r"<nav\b[^>]*>.*?</nav>", "", text, flags=re.S | re.I)

    # --- strip all remaining HTML tags ------------------------------------
    text = re.sub(r"<[^>]+>", " ", text)

    # --- collapse whitespace & un-escape entities -------------------------
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()

    # --- truncate ---------------------------------------------------------
    if len(text) > max_chars:
        text = text[:max_chars] + "…"

    return text


# ---------------------------------------------------------------------------
# pipecat FunctionSchema  (mirrors server.py's _web_search_schema pattern)
# ---------------------------------------------------------------------------

_web_fetch_schema = FunctionSchema(
    name="web_fetch",
    description=(
        "Fetch a web page's readable text. Use this when you need to read "
        "the content of a specific URL."
    ),
    properties={
        "url": {
            "type": "string",
            "description": "The URL to fetch",
        },
        "max_chars": {
            "type": "integer",
            "description": "Maximum characters to return (default 6000)",
            "default": 6000,
        },
    },
    required=["url"],
)


# ---------------------------------------------------------------------------
# Async handler (suitable for pipecat register_function / tool dispatch)
# ---------------------------------------------------------------------------

async def handle_web_fetch(args: dict[str, Any]) -> str:
    """Dispatch helper — takes raw tool-call *args* dict, returns text."""
    url = args.get("url", "")
    max_chars = int(args.get("max_chars", 6000))
    return await web_fetch(url, max_chars=max_chars)
