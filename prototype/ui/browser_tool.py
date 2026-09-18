"""Minimal Chrome DevTools Protocol (CDP) browser tool.

A small, explicit, dependency-light CDP client built on stdlib + ``websockets``
+ ``httpx`` (no new dependencies).

Design goals
------------
* **Safe by default.** It refuses to type into password fields and refuses
  destructive clicks unless the caller explicitly opts in.
* **Testable without a browser.** The DOM/safety logic lives in pure helpers
  plus a :class:`Page` class that accepts any object with a ``call(method,
  params, timeout)`` method.  The real transport is :class:`_CDP`, which wraps a
  ``websockets`` connection; tests inject a fake.
* **Never touches the real profile.** ``launch`` always passes a dedicated,
  non-default ``--user-data-dir`` (required since Chrome 136 — see
  https://developer.chrome.com/blog/remote-debugging-port).
* **Never hangs.** Every CDP call and every wait has a timeout.
* **Never persists page content or typed text.** Nothing is logged.

This module only ever connects to ``127.0.0.1`` / ``localhost``.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect as _ws_connect

__all__ = [
    "BrowserError",
    "SafetyError",
    "BrowserNotFoundError",
    "WaitTimeoutError",
    "Page",
    "LaunchedBrowser",
    "MARKER_NAME",
    "launch",
    "close",
    "targets",
    "pages",
    "browser_version",
    "page_ws_url",
    "connect_page",
    "select_page",
    "find_marked_page",
    "ensure_marked_tab",
    "find_browser",
    "is_destructive_label",
    "assert_not_destructive",
    "assert_not_password",
    "goto",
    "url",
    "title",
    "text",
    "find",
    "click",
    "type_text",
    "press",
    "wait_for",
    "screenshot",
    "main",
]

DEFAULT_PORT = 9222
DEFAULT_TIMEOUT = 15.0
DEFAULT_POLL = 0.2
DEFAULT_TEXT_LIMIT = 20000

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

# Every tab this tool creates is tagged with this window.name so later,
# stateless CLI invocations can find the same tab deterministically instead
# of blindly using pages[0] (which may be an extension or the human's tab).
MARKER_NAME = "jarvis-cdp"

# Targets on these schemes, plus auth pages, are never auto-selected: the tool
# only ever drives a tab it created itself.
_SKIP_URL_PREFIXES = ("chrome://", "chrome-extension://", "edge://", "devtools://")
_SKIP_URL_SUBSTRINGS = ("accounts.google.com", "login.microsoftonline.com")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class BrowserError(Exception):
    """Any failure talking to the browser or driving it."""


class SafetyError(BrowserError):
    """A guard rail refused a potentially destructive or credential action."""


class BrowserNotFoundError(BrowserError):
    """No usable browser binary could be found."""


class WaitTimeoutError(BrowserError, TimeoutError):
    """A ``wait_for`` poll timed out.  Also a ``TimeoutError``."""


# --------------------------------------------------------------------------- #
# Pure safety helpers (unit-tested without a browser)
# --------------------------------------------------------------------------- #
DESTRUCTIVE_WORDS = (
    "delete",
    "deletes",
    "deleting",
    "deleted",
    "deletion",
    "remove",
    "removes",
    "removing",
    "removed",
    "removal",
    "permanently",
    "permanent",
    "disable",
    "disables",
    "disabling",
    "disabled",
    "revoke",
    "revokes",
    "revoking",
    "revoked",
    "destroy",
    "destroys",
    "destroying",
    "destroyed",
    "destruction",
    "erase",
    "erases",
    "erasing",
    "erased",
    "erasure",
    "drop",
    "drops",
    "dropping",
    "dropped",
    "unlink",
    "unlinks",
    "unlinking",
    "unlinked",
    "wipe",
    "wipes",
    "wiping",
    "wiped",
    "terminate",
    "terminates",
    "terminating",
    "terminated",
)

_DESTRUCTIVE_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(DESTRUCTIVE_WORDS), re.IGNORECASE
)


def is_destructive_label(label: Optional[str]) -> bool:
    """True if ``label`` contains a whole-word destructive verb.

    Whole-word matching keeps "Delegates" and "dropdown" safe while still
    catching "Delete bucket" and "Drop all tables".
    """
    if not label:
        return False
    return bool(_DESTRUCTIVE_RE.search(label))


def element_label(desc: dict) -> str:
    """Human-readable identifying text for an element descriptor."""
    parts = [
        desc.get("text") or "",
        desc.get("aria_label") or "",
        desc.get("value") or "",
    ]
    return " ".join(p.strip() for p in parts if p).strip()


def assert_not_destructive(desc: dict, allow_destructive: bool = False) -> None:
    """Raise :class:`SafetyError` for a destructive control unless allowed."""
    if allow_destructive:
        return
    label = element_label(desc)
    if is_destructive_label(label):
        raise SafetyError(
            "refusing destructive control %r; pass allow_destructive=True to "
            "override" % label
        )


def assert_not_password(desc: dict) -> None:
    """Raise :class:`SafetyError` when the element is a password input."""
    tag = (desc.get("tag") or "").lower()
    etype = (desc.get("type") or "").lower()
    selector = str(desc.get("selector") or "").lower().replace(" ", "").replace('"', "").replace("'", "")
    if etype == "password":
        raise SafetyError(
            "refusing to type into a password field; the human signs in, not "
            "the tool"
        )
    if tag == "input" and "type=password" in selector:
        raise SafetyError(
            "refusing to type into a password field; the human signs in, not "
            "the tool"
        )


# --------------------------------------------------------------------------- #
# Browser binary resolution
# --------------------------------------------------------------------------- #
_BROWSER_BINARIES: dict[str, list[str]] = {
    "edge": [
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Microsoft Edge Beta.app/Contents/MacOS/Microsoft Edge Beta",
        "/Applications/Microsoft Edge Dev.app/Contents/MacOS/Microsoft Edge Dev",
    ],
    "chrome": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    ],
    "chromium": [
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Chromium.app/Contents/MacOS/chromium",
    ],
}

_FORBIDDEN_PROFILE_PARTS = (
    "library/application support/microsoft edge",
    "library/application support/google/chrome",
    "library/application support/chromium",
)


def find_browser(browser: str = "edge") -> str:
    """Return the executable path for ``browser`` or raise clearly."""
    key = (browser or "edge").lower()
    candidates = list(_BROWSER_BINARIES.get(key, []))
    which = shutil.which(browser) if browser else None
    if which:
        candidates.append(which)
    for cand in candidates:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    raise BrowserNotFoundError(
        "could not find browser %r; looked for: %s" % (browser, candidates or key)
    )


def _assert_non_default_profile(path: str) -> str:
    """Reject any path that resolves to a real browser profile."""
    abspath = os.path.abspath(os.path.expanduser(path))
    low = abspath.lower()
    for bad in _FORBIDDEN_PROFILE_PARTS:
        if bad in low:
            raise BrowserError(
                "refusing to use the default browser profile %r; debugging "
                "requires a dedicated --user-data-dir" % abspath
            )
    return abspath


def _default_profile_dir(port: int = DEFAULT_PORT) -> str:
    """A dedicated profile dir outside the repo (system temp)."""
    base = os.path.join(tempfile.gettempdir(), "jarvis-browser-profiles")
    return os.path.join(base, str(int(port)))


# --------------------------------------------------------------------------- #
# Loopback-only HTTP + websocket helpers
# --------------------------------------------------------------------------- #
def _assert_loopback(url: str) -> str:
    host = urlparse(url).hostname
    if host not in _LOOPBACK_HOSTS:
        raise BrowserError("refusing non-loopback URL: %s" % url)
    return url


def _http_get_json(url: str, timeout: float) -> Any:
    _assert_loopback(url)
    try:
        resp = httpx.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except BrowserError:
        raise
    except Exception as exc:  # noqa: BLE001 - converted to a clear error
        raise BrowserError("cannot reach %s: %s" % (url, exc)) from exc


def browser_version(port: int = DEFAULT_PORT, timeout: float = 5.0) -> dict:
    """GET /json/version -> browser metadata incl. webSocketDebuggerUrl."""
    return _http_get_json(
        "http://127.0.0.1:%d/json/version" % int(port), timeout
    )


def targets(port: int = DEFAULT_PORT, timeout: float = 5.0) -> list[dict]:
    """GET /json/list -> list of debug targets."""
    data = _http_get_json("http://127.0.0.1:%d/json/list" % int(port), timeout)
    return data if isinstance(data, list) else []


def pages(port: int = DEFAULT_PORT, timeout: float = 5.0) -> list[dict]:
    """Page-typed targets only."""
    return [t for t in targets(port, timeout) if t.get("type") == "page"]


def page_ws_url(port: int = DEFAULT_PORT, timeout: float = 5.0, index: int = 0) -> str:
    ps = pages(port, timeout)
    if not ps:
        raise BrowserError("no page targets on port %d" % int(port))
    try:
        url = ps[index]["webSocketDebuggerUrl"]
    except (KeyError, IndexError) as exc:
        raise BrowserError("page target has no webSocketDebuggerUrl") from exc
    return _assert_loopback(url)


def _open_ws(url: str, timeout: float = DEFAULT_TIMEOUT):
    _assert_loopback(url)
    try:
        return _ws_connect(
            url,
            open_timeout=timeout,
            close_timeout=timeout,
            max_size=None,
            proxy=None,
            # Connect directly (not as a context manager). This is the
            # documented escape hatch in websockets >=14; the returned
            # connection still supports recv(timeout=...).
            legacy=True,
        )
    except Exception as exc:  # noqa: BLE001 - converted to a clear error
        raise BrowserError("cannot connect to %s: %s" % (url, exc)) from exc


# --------------------------------------------------------------------------- #
# CDP transport
# --------------------------------------------------------------------------- #
class _CDP:
    """JSON-RPC over a websocket.  Injectable: pass any ``ws`` with
    ``send(str)`` / ``recv(timeout=...)`` / ``close()``."""

    def __init__(self, ws: Any, timeout: float = DEFAULT_TIMEOUT):
        self._ws = ws
        self.timeout = float(timeout)
        self._id = 0

    def call(
        self, method: str, params: Optional[dict] = None, timeout: Optional[float] = None
    ) -> dict:
        self._id += 1
        mid = self._id
        msg: dict[str, Any] = {"id": mid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._ws.send(json.dumps(msg))
        except Exception as exc:  # noqa: BLE001
            raise BrowserError("send failed for %s: %s" % (method, exc)) from exc

        budget = float(timeout) if timeout is not None else self.timeout
        deadline = time.monotonic() + budget
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserError(
                    "CDP timeout after %ss waiting for %s" % (budget, method)
                )
            try:
                raw = self._ws.recv(timeout=remaining)
            except TimeoutError as exc:
                raise BrowserError(
                    "CDP timeout after %ss waiting for %s" % (budget, method)
                ) from exc
            except WebSocketException as exc:
                raise BrowserError(
                    "websocket closed while waiting for %s: %s" % (method, exc)
                ) from exc
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", "replace")
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict) or data.get("id") != mid:
                continue
            if "error" in data:
                err = data["error"]
                msg_txt = err.get("message") if isinstance(err, dict) else err
                raise BrowserError("%s failed: %s" % (method, msg_txt))
            return data.get("result") or {}

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Deterministic tab selection
# --------------------------------------------------------------------------- #
def _is_skippable_url(url: Optional[str]) -> bool:
    """True for internal/extension/auth pages the tool must never auto-drive."""
    u = (url or "").lower()
    if any(u.startswith(prefix) for prefix in _SKIP_URL_PREFIXES):
        return True
    return any(sub in u for sub in _SKIP_URL_SUBSTRINGS)


def _read_window_name(ws_url: str, timeout: float = DEFAULT_TIMEOUT) -> Optional[str]:
    """Read only ``window.name`` from a target. The single allowed cross-tab call."""
    try:
        ws = _open_ws(ws_url, timeout)
    except BrowserError:
        return None
    try:
        cdp = _CDP(ws, timeout)
        res = cdp.call(
            "Runtime.evaluate",
            {"expression": "window.name", "returnByValue": True},
            timeout=timeout,
        )
        return (res.get("result") or {}).get("value")
    except BrowserError:
        return None
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass


def find_marked_page(
    port: int = DEFAULT_PORT,
    ps: Optional[list[dict]] = None,
    timeout: float = 5.0,
) -> Optional[dict]:
    """Return the ``jarvis-cdp`` page target, or None."""
    page_list = pages(port, timeout) if ps is None else ps
    for target in page_list:
        if _is_skippable_url(target.get("url")):
            continue
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            continue
        if _read_window_name(ws_url, timeout) == MARKER_NAME:
            return target
    return None


def select_page(
    port: int = DEFAULT_PORT,
    index: Optional[int] = None,
    url_contains: Optional[str] = None,
    marked: bool = True,
    timeout: float = 5.0,
) -> dict:
    """Choose a page target deterministically.

    Priority: explicit ``index`` -> explicit ``url_contains`` -> marked tab ->
    first non-internal/non-auth page.  Never silently returns an extension or
    auth tab.
    """
    page_list = pages(port, timeout)
    if not page_list:
        raise BrowserError("no page targets on port %d" % int(port))
    if index is not None:
        try:
            return page_list[index]
        except IndexError as exc:
            raise BrowserError(
                "page index %s out of range (%d pages)" % (index, len(page_list))
            ) from exc
    if url_contains is not None:
        for target in page_list:
            if url_contains in (target.get("url") or ""):
                return target
        raise BrowserError("no page target URL contains %r" % url_contains)
    if marked:
        marked_target = find_marked_page(port, ps=page_list, timeout=timeout)
        if marked_target is not None:
            return marked_target
    for target in page_list:
        if not _is_skippable_url(target.get("url")):
            return target
    raise BrowserError(
        "no tool page on port %d; run `goto <url>` to create the marked tab"
        % int(port)
    )


def _page_from_info(info: dict, port: int, timeout: float) -> "Page":
    ws_url = info.get("webSocketDebuggerUrl")
    if not ws_url:
        raise BrowserError("selected page has no webSocketDebuggerUrl")
    return Page(_CDP(_open_ws(ws_url, timeout), timeout), timeout=timeout)


def ensure_marked_tab(
    port: int = DEFAULT_PORT, timeout: float = DEFAULT_TIMEOUT
) -> dict:
    """Return the marked page, creating and tagging one if necessary."""
    info = find_marked_page(port, timeout=timeout)
    if info is not None:
        return info

    version = browser_version(port, timeout=timeout)
    browser_ws = version.get("webSocketDebuggerUrl")
    if not browser_ws:
        raise BrowserError("no browser webSocketDebuggerUrl on port %d" % int(port))
    ws = _open_ws(browser_ws, timeout)
    try:
        created = _CDP(ws, timeout).call(
            "Target.createTarget", {"url": "about:blank"}, timeout=timeout
        )
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
    target_id = created.get("targetId")
    if not target_id:
        raise BrowserError("Target.createTarget returned no targetId")

    info = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for target in pages(port, min(2.0, timeout)):
            if target.get("id") == target_id:
                info = target
                break
        if info is not None:
            break
        time.sleep(0.1)
    if info is None:
        raise BrowserError("created target %s never appeared in /json/list" % target_id)

    ws = _open_ws(info["webSocketDebuggerUrl"], timeout)
    try:
        _CDP(ws, timeout).call(
            "Runtime.evaluate",
            {
                "expression": "window.name = %s" % json.dumps(MARKER_NAME),
                "returnByValue": True,
            },
            timeout=timeout,
        )
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
    return info


# --------------------------------------------------------------------------- #
# DOM descriptor JS
# --------------------------------------------------------------------------- #
_DESCRIBE_JS = r"""
(() => {
  const SEL = __SEL__;
  const WANT = __WANT__;
  function vis(el) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' &&
           st.display !== 'none' && st.opacity !== '0';
  }
  function labelOf(el) {
    const t = (el.innerText || '').trim();
    if (t) return t;
    return (el.getAttribute('aria-label') || el.value || el.getAttribute('title') || '').trim();
  }
  function uniq(el) {
    if (el.id) {
      const s = '#' + CSS.escape(el.id);
      try { if (document.querySelectorAll(s).length === 1) return s; } catch (e) {}
    }
    for (const attr of ['name', 'data-testid']) {
      const v = el.getAttribute && el.getAttribute(attr);
      if (v) {
        const s = el.tagName.toLowerCase() + '[' + attr + '="' + CSS.escape(v) + '"]';
        try { if (document.querySelectorAll(s).length === 1) return s; } catch (e) {}
      }
    }
    let parts = [];
    let node = el;
    while (node && node.nodeType === 1) {
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const sibs = [...parent.children].filter(c => c.tagName === node.tagName);
        if (sibs.length > 1) part += ':nth-of-type(' + (sibs.indexOf(node) + 1) + ')';
      }
      parts.unshift(part);
      const path = parts.join(' > ');
      try { if (document.querySelectorAll(path).length === 1) return path; } catch (e) {}
      node = parent;
    }
    return parts.join(' > ');
  }
  function resolve() {
    if (SEL) return document.querySelector(SEL);
    if (WANT) {
      const w = String(WANT).toLowerCase();
      const nodes = [...document.querySelectorAll(
        'a,button,input,select,textarea,[role="button"],[role="link"],[role="menuitem"],[onclick],[tabindex]')];
      let partial = null;
      for (const el of nodes) {
        const t = labelOf(el).toLowerCase();
        if (t === w && vis(el)) return el;
        if (!partial && t.includes(w) && vis(el)) partial = el;
      }
      if (partial) return partial;
      const any = [...document.querySelectorAll('*')].filter(
        el => vis(el) && labelOf(el).toLowerCase().includes(w));
      if (any.length) {
        any.sort((a, b) => a.children.length - b.children.length);
        return any[0];
      }
    }
    return null;
  }
  const el = resolve();
  if (!el) return {found: false};
  const r = el.getBoundingClientRect();
  return {
    found: true,
    selector: uniq(el),
    tag: el.tagName,
    type: (el.type || '').toLowerCase(),
    text: (el.innerText || '').trim().slice(0, 500),
    aria_label: (el.getAttribute('aria-label') || '').slice(0, 200),
    value: (typeof el.value === 'string' ? el.value : '').slice(0, 200),
    rect: {x: r.x, y: r.y, width: r.width, height: r.height},
    visible: vis(el)
  };
})()
"""


def _describe_expression(selector: Optional[str] = None, text: Optional[str] = None) -> str:
    return _DESCRIBE_JS.replace("__SEL__", json.dumps(selector)).replace(
        "__WANT__", json.dumps(text)
    )


def _focus_expression(selector: str) -> str:
    return (
        "(() => { const el = document.querySelector(%s); if (!el) return false;"
        " el.scrollIntoView({block: 'center'}); el.focus(); return true; })()"
        % json.dumps(selector)
    )


def _text_expression(selector: Optional[str]) -> str:
    if selector:
        return (
            "(() => { const el = document.querySelector(%s);"
            " return el ? el.innerText : null; })()" % json.dumps(selector)
        )
    return "document.body ? document.body.innerText : ''"


# --------------------------------------------------------------------------- #
# Keyboard mapping
# --------------------------------------------------------------------------- #
_KEYMAP: dict[str, dict] = {
    "enter": {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r"},
    "return": {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r"},
    "tab": {"key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9, "text": "\t"},
    "escape": {"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27, "text": ""},
    "esc": {"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27, "text": ""},
    "backspace": {"key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8, "text": ""},
    "space": {"key": " ", "code": "Space", "windowsVirtualKeyCode": 32, "text": " "},
    "delete": {"key": "Delete", "code": "Delete", "windowsVirtualKeyCode": 46, "text": ""},
}


def _key_params(key: str) -> dict:
    low = (key or "").lower()
    if low in _KEYMAP:
        return dict(_KEYMAP[low])
    if len(key) == 1:
        up = key.upper()
        code = "Key" + up if up.isalpha() else ("Digit" + key if key.isdigit() else "")
        return {
            "key": key,
            "code": code,
            "windowsVirtualKeyCode": ord(up),
            "text": key,
            "unmodifiedText": key,
        }
    return {"key": key, "code": key, "windowsVirtualKeyCode": 0, "text": ""}


# --------------------------------------------------------------------------- #
# Page: the testable, injectable driver
# --------------------------------------------------------------------------- #
class Page:
    """Drives one page target through an injectable CDP connection.

    ``cdp`` may be a real :class:`_CDP` or any object exposing
    ``call(method, params, timeout=...)``.
    """

    def __init__(
        self,
        cdp: Any,
        timeout: float = DEFAULT_TIMEOUT,
        poll: float = DEFAULT_POLL,
    ):
        self._cdp = cdp
        self.timeout = float(timeout)
        self.poll = float(poll)

    # -- low level ---------------------------------------------------------
    def _eval(self, expression: str, timeout: Optional[float] = None) -> Any:
        res = self._cdp.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
            timeout=timeout if timeout is not None else self.timeout,
        )
        if res.get("exceptionDetails"):
            det = res["exceptionDetails"]
            raise BrowserError(
                "page script error: %s" % (det.get("text") or det)
            )
        return (res.get("result") or {}).get("value")

    def describe(
        self,
        text: Optional[str] = None,
        selector: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> dict:
        if not text and not selector:
            raise BrowserError("describe needs text or selector")
        value = self._eval(
            _describe_expression(selector=selector, text=text), timeout=timeout
        )
        if not isinstance(value, dict):
            return {"found": False}
        return value

    # -- discovery ---------------------------------------------------------
    def find(self, text: Optional[str] = None, selector: Optional[str] = None) -> dict:
        desc = self.describe(text=text, selector=selector)
        if not desc.get("found"):
            raise BrowserError(
                "element not found (text=%r selector=%r)" % (text, selector)
            )
        return {"selector": desc.get("selector"), "text": desc.get("text")}

    # -- navigation --------------------------------------------------------
    def goto(self, url: str, wait: bool = True, timeout: Optional[float] = None) -> dict:
        res = self._cdp.call(
            "Page.navigate",
            {"url": url},
            timeout=timeout if timeout is not None else self.timeout,
        )
        if res.get("errorText"):
            raise BrowserError("navigation failed: %s" % res["errorText"])
        if wait:
            self._wait_ready(timeout=timeout if timeout is not None else self.timeout)
        return res

    def _wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                state = self._eval("document.readyState", timeout=timeout)
            except BrowserError:
                state = None
            if state == "complete":
                return
            if time.monotonic() >= deadline:
                raise BrowserError("page did not finish loading within %ss" % timeout)
            time.sleep(min(self.poll, max(0.0, deadline - time.monotonic())))

    def url(self) -> str:
        return self._eval("location.href")

    def title(self) -> str:
        return self._eval("document.title")

    def text(
        self, selector: Optional[str] = None, limit: int = DEFAULT_TEXT_LIMIT
    ) -> str:
        value = self._eval(_text_expression(selector))
        if value is None:
            raise BrowserError("element not found: %r" % selector)
        if not isinstance(value, str):
            value = str(value)
        return value[:limit]

    # -- interaction -------------------------------------------------------
    def click(
        self,
        text: Optional[str] = None,
        selector: Optional[str] = None,
        allow_destructive: bool = False,
        timeout: Optional[float] = None,
    ) -> Optional[str]:
        desc = self.describe(text=text, selector=selector, timeout=timeout)
        if not desc.get("found"):
            raise BrowserError(
                "element not found (text=%r selector=%r)" % (text, selector)
            )
        assert_not_destructive(desc, allow_destructive=allow_destructive)
        sel = desc.get("selector")
        if sel:
            self._eval(_focus_expression(sel), timeout=timeout)
            remeasured = self.describe(selector=sel, timeout=timeout)
            if remeasured.get("rect"):
                desc = remeasured
        rect = desc.get("rect") or {}
        x = float(rect.get("x", 0)) + float(rect.get("width", 0)) / 2.0
        y = float(rect.get("y", 0)) + float(rect.get("height", 0)) / 2.0
        for etype, buttons in (("mousePressed", 1), ("mouseReleased", 0)):
            self._cdp.call(
                "Input.dispatchMouseEvent",
                {
                    "type": etype,
                    "x": x,
                    "y": y,
                    "button": "left",
                    "buttons": buttons,
                    "clickCount": 1,
                },
                timeout=timeout if timeout is not None else self.timeout,
            )
        return sel

    def type_text(
        self, selector: str, text: str, timeout: Optional[float] = None
    ) -> bool:
        desc = self.describe(selector=selector, timeout=timeout)
        if not desc.get("found"):
            raise BrowserError("element not found: %r" % selector)
        assert_not_password(desc)
        sel = desc.get("selector") or selector
        self._eval(_focus_expression(sel), timeout=timeout)
        self._cdp.call(
            "Input.insertText",
            {"text": text},
            timeout=timeout if timeout is not None else self.timeout,
        )
        return True

    def press(self, key: str, timeout: Optional[float] = None) -> bool:
        params = _key_params(key)
        down: dict[str, Any] = {"type": "keyDown"}
        down.update(params)
        up: dict[str, Any] = {"type": "keyUp"}
        for name in ("key", "code", "windowsVirtualKeyCode"):
            if name in params:
                up[name] = params[name]
        budget = timeout if timeout is not None else self.timeout
        self._cdp.call("Input.dispatchKeyEvent", down, timeout=budget)
        self._cdp.call("Input.dispatchKeyEvent", up, timeout=budget)
        return True

    def wait_for(
        self,
        text: Optional[str] = None,
        selector: Optional[str] = None,
        timeout: float = 20,
        interval: Optional[float] = None,
    ) -> dict:
        interval = interval if interval is not None else self.poll
        deadline = time.monotonic() + timeout
        while True:
            try:
                desc = self.describe(text=text, selector=selector, timeout=timeout)
            except BrowserError:
                desc = {"found": False}
            if desc.get("found"):
                return {"selector": desc.get("selector"), "text": desc.get("text")}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WaitTimeoutError(
                    "timeout after %ss waiting for text=%r selector=%r"
                    % (timeout, text, selector)
                )
            time.sleep(min(interval, remaining))

    def screenshot(self, path: str) -> str:
        res = self._cdp.call(
            "Page.captureScreenshot", {"format": "png"}, timeout=self.timeout
        )
        data = res.get("data")
        if not data:
            raise BrowserError("screenshot returned no data")
        try:
            raw = base64.b64decode(data)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError("invalid screenshot data: %s" % exc) from exc
        abspath = os.path.abspath(path)
        parent = os.path.dirname(abspath)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(abspath, "wb") as fh:
            fh.write(raw)
        return abspath

    def close(self) -> None:
        close = getattr(self._cdp, "close", None)
        if callable(close):
            close()


# --------------------------------------------------------------------------- #
# Launch / close
# --------------------------------------------------------------------------- #
class LaunchedBrowser:
    """Handle for a browser started by :func:`launch`."""

    def __init__(
        self,
        proc: subprocess.Popen,
        port: int,
        user_data_dir: str,
        executable: str,
    ):
        self.proc = proc
        self.port = int(port)
        self.user_data_dir = user_data_dir
        self.executable = executable
        self.version: Optional[dict] = None

    def wait_ready(self, timeout: float = 30.0) -> "LaunchedBrowser":
        deadline = time.monotonic() + timeout
        last: Optional[Exception] = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise BrowserError(
                    "browser exited early (code %s)" % self.proc.returncode
                )
            try:
                self.version = browser_version(self.port, timeout=2.0)
                return self
            except BrowserError as exc:
                last = exc
                time.sleep(0.25)
        raise BrowserError(
            "browser debugging endpoint did not come up on port %d within %ss: %s"
            % (self.port, timeout, last)
        )

    def _terminate(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass

    def close(self, timeout: float = 5.0) -> None:
        try:
            close(self.port, timeout=timeout)
        except BrowserError:
            pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.proc.poll() is None:
            time.sleep(0.1)
        self._terminate()


def launch(
    user_data_dir: Optional[str] = None,
    port: int = DEFAULT_PORT,
    browser: str = "edge",
    timeout: float = 30.0,
    window_size: Optional[tuple[int, int]] = (1280, 900),
) -> LaunchedBrowser:
    """Launch a browser with remote debugging on a dedicated profile.

    Always uses a NON-default ``--user-data-dir`` (required since Chrome 136).
    Returns only once ``/json/version`` answers, else raises.

    ``window_size=(w, h)`` passes ``--window-size`` so the app renders its
    normal desktop layout (important for text/selector matching); ``None``
    omits the flag.
    """
    executable = find_browser(browser)
    if user_data_dir is None:
        user_data_dir = _default_profile_dir(port)
    user_data_dir = _assert_non_default_profile(user_data_dir)
    os.makedirs(user_data_dir, exist_ok=True)
    args = [
        executable,
        "--remote-debugging-port=%d" % int(port),
        "--user-data-dir=%s" % user_data_dir,
        "--no-first-run",
        "--no-default-browser-check",
        # Keep the launched browser quiet: no extensions/first-run/sync noise,
        # which otherwise adds chrome-extension:// tabs that confuse targeting.
        "--disable-extensions",
        "--disable-component-extensions-with-background-pages",
        "--disable-sync",
        "--no-service-autorun",
        "--password-store=basic",
    ]
    if window_size:
        args.append("--window-size=%d,%d" % (int(window_size[0]), int(window_size[1])))
    args.append("about:blank")
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise BrowserError("failed to launch %s: %s" % (executable, exc)) from exc
    handle = LaunchedBrowser(proc, port, user_data_dir, executable)
    try:
        handle.wait_ready(timeout)
    except Exception:
        handle._terminate()
        raise
    return handle


def close(port: int = DEFAULT_PORT, timeout: float = 5.0) -> bool:
    """Close a running browser via the browser-level websocket.

    Stateless: works for a browser launched in a previous CLI invocation.
    Returns True if a Browser.close was delivered, False if nothing answered.
    """
    try:
        version = browser_version(port, timeout=timeout)
    except BrowserError:
        return False
    ws_url = version.get("webSocketDebuggerUrl")
    if not ws_url:
        return False
    ws = _open_ws(ws_url, timeout)
    try:
        cdp = _CDP(ws, timeout)
        try:
            cdp.call("Browser.close", timeout=timeout)
        except BrowserError:
            pass
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
    return True


def connect_page(
    port: int = DEFAULT_PORT,
    index: Optional[int] = None,
    url_contains: Optional[str] = None,
    marked: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
) -> Page:
    """Connect to a page target; by default the marked ``jarvis-cdp`` tab."""
    info = select_page(
        port,
        index=index,
        url_contains=url_contains,
        marked=marked,
        timeout=timeout,
    )
    return _page_from_info(info, port, timeout)


# --------------------------------------------------------------------------- #
# Module-level convenience API (opens + closes a connection per call)
# --------------------------------------------------------------------------- #
def _with_page(
    fn: Callable[[Page], Any],
    port: int,
    timeout: float,
    index: Optional[int] = None,
    url_contains: Optional[str] = None,
    marked: bool = True,
) -> Any:
    page = connect_page(
        port=port,
        index=index,
        url_contains=url_contains,
        marked=marked,
        timeout=timeout,
    )
    try:
        return fn(page)
    finally:
        page.close()


def goto(
    url: str,
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
    index: Optional[int] = None,
    url_contains: Optional[str] = None,
) -> dict:
    """Navigate the marked tab, creating/tagging it first if needed."""
    if index is None and url_contains is None:
        info = ensure_marked_tab(port, timeout)
    else:
        info = select_page(
            port, index=index, url_contains=url_contains, marked=False, timeout=timeout
        )
    page = _page_from_info(info, port, timeout)
    try:
        return page.goto(url, timeout=timeout)
    finally:
        page.close()


def url(
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
    index: Optional[int] = None,
    url_contains: Optional[str] = None,
) -> str:
    return _with_page(
        lambda p: p.url(), port, timeout, index=index, url_contains=url_contains
    )


def title(
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
    index: Optional[int] = None,
    url_contains: Optional[str] = None,
) -> str:
    return _with_page(
        lambda p: p.title(), port, timeout, index=index, url_contains=url_contains
    )


def text(
    selector: Optional[str] = None,
    limit: int = DEFAULT_TEXT_LIMIT,
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
    index: Optional[int] = None,
    url_contains: Optional[str] = None,
) -> str:
    return _with_page(
        lambda p: p.text(selector, limit=limit),
        port,
        timeout,
        index=index,
        url_contains=url_contains,
    )


def find(
    text: Optional[str] = None,
    selector: Optional[str] = None,
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
    index: Optional[int] = None,
    url_contains: Optional[str] = None,
) -> dict:
    return _with_page(
        lambda p: p.find(text=text, selector=selector),
        port,
        timeout,
        index=index,
        url_contains=url_contains,
    )


def click(
    text: Optional[str] = None,
    selector: Optional[str] = None,
    allow_destructive: bool = False,
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
    index: Optional[int] = None,
    url_contains: Optional[str] = None,
) -> Optional[str]:
    return _with_page(
        lambda p: p.click(
            text=text,
            selector=selector,
            allow_destructive=allow_destructive,
            timeout=timeout,
        ),
        port,
        timeout,
        index=index,
        url_contains=url_contains,
    )


def type_text(
    selector: str,
    text: str,
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
    index: Optional[int] = None,
    url_contains: Optional[str] = None,
) -> bool:
    return _with_page(
        lambda p: p.type_text(selector, text, timeout=timeout),
        port,
        timeout,
        index=index,
        url_contains=url_contains,
    )


def press(
    key: str,
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
    index: Optional[int] = None,
    url_contains: Optional[str] = None,
) -> bool:
    return _with_page(
        lambda p: p.press(key, timeout=timeout),
        port,
        timeout,
        index=index,
        url_contains=url_contains,
    )


def wait_for(
    text: Optional[str] = None,
    selector: Optional[str] = None,
    timeout: float = 20,
    port: int = DEFAULT_PORT,
    index: Optional[int] = None,
    url_contains: Optional[str] = None,
) -> dict:
    return _with_page(
        lambda p: p.wait_for(text=text, selector=selector, timeout=timeout),
        port,
        max(timeout, DEFAULT_TIMEOUT),
        index=index,
        url_contains=url_contains,
    )


def screenshot(
    path: str,
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
    index: Optional[int] = None,
    url_contains: Optional[str] = None,
) -> str:
    return _with_page(
        lambda p: p.screenshot(path),
        port,
        timeout,
        index=index,
        url_contains=url_contains,
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_window_size(value: Optional[str]) -> Optional[tuple[int, int]]:
    if value is None or str(value).lower() in ("", "none"):
        return None
    try:
        width, height = str(value).lower().replace(" ", "").split("x")
        return (int(width), int(height))
    except (ValueError, AttributeError) as exc:
        raise argparse.ArgumentTypeError(
            "window size must be WxH, e.g. 1280x900, or 'none'"
        ) from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Minimal CDP browser tool (loopback only)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_port(p: argparse.ArgumentParser) -> None:
        p.add_argument("--port", type=int, default=DEFAULT_PORT)

    def add_target(p: argparse.ArgumentParser) -> None:
        p.add_argument("--index", type=int, default=None)
        p.add_argument("--match-url", dest="url_contains", default=None)

    p = sub.add_parser("launch", help="launch a browser on a dedicated profile")
    add_port(p)
    p.add_argument("--browser", default="edge")
    p.add_argument("--user-data-dir", default=None)
    p.add_argument(
        "--window-size",
        type=_parse_window_size,
        default=(1280, 900),
        help="WxH (default 1280x900); pass 'none' to omit --window-size",
    )

    p = sub.add_parser("close", help="close the browser on a port")
    add_port(p)

    p = sub.add_parser("targets", help="list debug targets")
    add_port(p)

    p = sub.add_parser("goto", help="navigate the marked tab")
    add_port(p)
    add_target(p)
    p.add_argument("url")
    p.add_argument("--no-wait", action="store_true")

    p = sub.add_parser("url", help="print current URL")
    add_port(p)
    add_target(p)

    p = sub.add_parser("title", help="print page title")
    add_port(p)
    add_target(p)

    p = sub.add_parser("text", help="print visible text")
    add_port(p)
    add_target(p)
    p.add_argument("selector", nargs="?", default=None)

    p = sub.add_parser("find", help="locate an element")
    add_port(p)
    add_target(p)
    p.add_argument("--text", default=None)
    p.add_argument("--selector", default=None)

    p = sub.add_parser("click", help="click an element")
    add_port(p)
    add_target(p)
    p.add_argument("--text", default=None)
    p.add_argument("--selector", default=None)
    p.add_argument("--allow-destructive", action="store_true")

    p = sub.add_parser("type", help="type into an element")
    add_port(p)
    add_target(p)
    p.add_argument("--selector", required=True)
    p.add_argument("--text", required=True)

    p = sub.add_parser("press", help="press a key")
    add_port(p)
    add_target(p)
    p.add_argument("key")

    p = sub.add_parser("wait", help="wait for an element")
    add_port(p)
    add_target(p)
    p.add_argument("--text", default=None)
    p.add_argument("--selector", default=None)
    p.add_argument("--timeout", type=float, default=20)

    p = sub.add_parser("screenshot", help="capture a PNG")
    add_port(p)
    add_target(p)
    p.add_argument("path")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    port = getattr(args, "port", DEFAULT_PORT)
    index = getattr(args, "index", None)
    url_contains = getattr(args, "url_contains", None)
    try:
        if args.cmd == "launch":
            handle = launch(
                user_data_dir=args.user_data_dir,
                port=port,
                browser=args.browser,
                window_size=args.window_size,
            )
            print(
                json.dumps(
                    {
                        "launched": True,
                        "port": handle.port,
                        "browser": handle.executable,
                        "user_data_dir": handle.user_data_dir,
                        "version": handle.version,
                    }
                )
            )
        elif args.cmd == "close":
            print(json.dumps({"closed": close(port)}))
        elif args.cmd == "targets":
            print(json.dumps(targets(port), indent=2))
        elif args.cmd == "goto":
            print(
                json.dumps(
                    goto(
                        args.url,
                        port=port,
                        index=index,
                        url_contains=url_contains,
                    )
                )
            )
        elif args.cmd == "url":
            print(url(port, index=index, url_contains=url_contains))
        elif args.cmd == "title":
            print(title(port, index=index, url_contains=url_contains))
        elif args.cmd == "text":
            print(
                text(
                    args.selector, port=port, index=index, url_contains=url_contains
                )
            )
        elif args.cmd == "find":
            print(
                json.dumps(
                    find(
                        args.text,
                        args.selector,
                        port=port,
                        index=index,
                        url_contains=url_contains,
                    )
                )
            )
        elif args.cmd == "click":
            print(
                json.dumps(
                    {
                        "clicked": click(
                            args.text,
                            args.selector,
                            allow_destructive=args.allow_destructive,
                            port=port,
                            index=index,
                            url_contains=url_contains,
                        )
                    }
                )
            )
        elif args.cmd == "type":
            print(
                json.dumps(
                    {
                        "typed": type_text(
                            args.selector,
                            args.text,
                            port=port,
                            index=index,
                            url_contains=url_contains,
                        )
                    }
                )
            )
        elif args.cmd == "press":
            print(
                json.dumps(
                    {
                        "pressed": press(
                            args.key, port=port, index=index, url_contains=url_contains
                        )
                    }
                )
            )
        elif args.cmd == "wait":
            print(
                json.dumps(
                    wait_for(
                        args.text,
                        args.selector,
                        timeout=args.timeout,
                        port=port,
                        index=index,
                        url_contains=url_contains,
                    )
                )
            )
        elif args.cmd == "screenshot":
            print(
                screenshot(
                    args.path, port=port, index=index, url_contains=url_contains
                )
            )
        else:  # pragma: no cover - argparse enforces known commands
            print("unknown command", file=sys.stderr)
            return 2
    except BrowserError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
