"""Hermetic tests for browser_tool.

No real browser and no network are used: a fake CDP connection is injected.
Run: ../../.venv/bin/python -m unittest test_browser_tool -v
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import browser_tool  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeCDP:
    """Injectable connection: records calls, returns canned results."""

    def __init__(self, responses=None, eval_handler=None):
        self.responses = dict(responses or {})
        self.eval_handler = eval_handler
        self.calls: list[tuple[str, dict | None]] = []
        self.closed = False

    def call(self, method, params=None, timeout=None):
        self.calls.append((method, params))
        if method == "Runtime.evaluate":
            if self.eval_handler is not None:
                return self.eval_handler((params or {}).get("expression", ""))
            return {"result": {"type": "undefined"}}
        return self.responses.get(method, {})

    def methods(self):
        return [m for m, _ in self.calls]

    def close(self):
        self.closed = True


def descriptor(**overrides):
    base = {
        "found": True,
        "selector": "#el",
        "tag": "BUTTON",
        "type": "",
        "text": "",
        "aria_label": "",
        "value": "",
        "rect": {"x": 0.0, "y": 0.0, "width": 20.0, "height": 10.0},
        "visible": True,
    }
    base.update(overrides)
    return {"result": {"type": "object", "value": base}}


def eval_router(desc_result, focus=True):
    """Return an eval handler that answers describe vs focus expressions."""

    def handler(expression):
        if "getBoundingClientRect" in expression:
            return desc_result
        if "scrollIntoView" in expression:
            return {"result": {"type": "boolean", "value": focus}}
        return {"result": {"type": "undefined"}}

    return handler


class FakeWS:
    """A websockets-like object good enough for _CDP."""

    def __init__(self, result=None):
        self._result = result
        self.sent: list[dict] = []
        self._queue: list[str] = []
        self.closed = False

    def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if self._result is not None:
            self._queue.append(json.dumps({"id": msg["id"], "result": self._result}))

    def recv(self, timeout=None):
        if self._queue:
            return self._queue.pop(0)
        raise TimeoutError("no message")

    def close(self):
        self.closed = True


class ErrorWS(FakeWS):
    def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        self._queue.append(
            json.dumps({"id": msg["id"], "error": {"code": -1, "message": "boom"}})
        )


class EventThenResultWS(FakeWS):
    """Sends a stray event before the matching response."""

    def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        self._queue.append(json.dumps({"method": "Page.loadEventFired", "params": {}}))
        if self._result is not None:
            self._queue.append(json.dumps({"id": msg["id"], "result": self._result}))


# --------------------------------------------------------------------------- #
# Safety pattern matching
# --------------------------------------------------------------------------- #
class SafetyPatternTests(unittest.TestCase):
    def test_destructive_positive(self):
        for label in (
            "Delete bucket",
            "delete",
            "Remove selected",
            "Permanently erase",
            "Revoke access",
            "Disable account",
            "Drop all tables",
            "Destroy everything",
        ):
            self.assertTrue(browser_tool.is_destructive_label(label), label)

    def test_destructive_negative(self):
        for label in (
            "Delegates",
            "delegate",
            "dropdown",
            "Dropdown menu",
            "Save",
            "Search",
            "Reset",
            "Deletegate",  # not a real word, but must not match
            "",
            None,
        ):
            self.assertFalse(browser_tool.is_destructive_label(label), label)

    def test_assert_not_destructive_raises(self):
        desc = {"text": "Delete bucket", "aria_label": "", "value": ""}
        with self.assertRaises(browser_tool.SafetyError):
            browser_tool.assert_not_destructive(desc)

    def test_assert_not_destructive_allows_with_flag(self):
        desc = {"text": "Delete bucket", "aria_label": "", "value": ""}
        browser_tool.assert_not_destructive(desc, allow_destructive=True)

    def test_element_label_combines_fields(self):
        desc = {"text": "", "aria_label": "Remove", "value": "x"}
        self.assertEqual(browser_tool.element_label(desc), "Remove x")


# --------------------------------------------------------------------------- #
# Password refusal
# --------------------------------------------------------------------------- #
class PasswordGuardTests(unittest.TestCase):
    def test_type_text_refuses_password_field(self):
        cdp = FakeCDP(
            eval_handler=eval_router(
                descriptor(selector="#pw", tag="INPUT", type="password")
            )
        )
        page = browser_tool.Page(cdp)
        with self.assertRaises(browser_tool.SafetyError):
            page.type_text("#pw", "hunter2")
        self.assertNotIn("Input.insertText", cdp.methods())

    def test_type_text_refuses_selector_style_password(self):
        cdp = FakeCDP(
            eval_handler=eval_router(
                descriptor(
                    selector='input[type="password"]', tag="INPUT", type="password"
                )
            )
        )
        page = browser_tool.Page(cdp)
        with self.assertRaises(browser_tool.SafetyError):
            page.type_text('input[type="password"]', "hunter2")

    def test_assert_not_password_allows_normal_input(self):
        browser_tool.assert_not_password(
            {"tag": "INPUT", "type": "text", "selector": "#name"}
        )

    def test_type_text_happy_path_inserts(self):
        cdp = FakeCDP(
            eval_handler=eval_router(descriptor(selector="#name", tag="INPUT", type="text"))
        )
        page = browser_tool.Page(cdp)
        self.assertTrue(page.type_text("#name", "Ada"))
        self.assertIn(("Input.insertText", {"text": "Ada"}), cdp.calls)


# --------------------------------------------------------------------------- #
# Destructive click guard
# --------------------------------------------------------------------------- #
class DestructiveClickTests(unittest.TestCase):
    def test_click_refuses_destructive(self):
        cdp = FakeCDP(eval_handler=eval_router(descriptor(text="Delete bucket")))
        page = browser_tool.Page(cdp)
        with self.assertRaises(browser_tool.SafetyError):
            page.click(text="Delete bucket")
        self.assertNotIn("Input.dispatchMouseEvent", cdp.methods())

    def test_click_allows_destructive_with_flag(self):
        cdp = FakeCDP(eval_handler=eval_router(descriptor(text="Delete bucket")))
        page = browser_tool.Page(cdp)
        page.click(text="Delete bucket", allow_destructive=True)
        mouse = [(m, p) for m, p in cdp.calls if m == "Input.dispatchMouseEvent"]
        self.assertEqual(len(mouse), 2)
        self.assertEqual(mouse[0][1]["type"], "mousePressed")
        self.assertEqual(mouse[1][1]["type"], "mouseReleased")
        # centre of the 20x10 box at (0,0)
        self.assertEqual(mouse[0][1]["x"], 10.0)
        self.assertEqual(mouse[0][1]["y"], 5.0)

    def test_click_refuses_missing_element(self):
        cdp = FakeCDP(
            eval_handler=eval_router({"result": {"type": "object", "value": {"found": False}}})
        )
        page = browser_tool.Page(cdp)
        with self.assertRaises(browser_tool.BrowserError):
            page.click(text="Nope")


# --------------------------------------------------------------------------- #
# wait_for / timeouts
# --------------------------------------------------------------------------- #
class WaitForTests(unittest.TestCase):
    def test_wait_for_timeout_raises_quickly(self):
        cdp = FakeCDP(
            eval_handler=eval_router({"result": {"type": "object", "value": {"found": False}}})
        )
        page = browser_tool.Page(cdp, poll=0.01)
        started = time.monotonic()
        with self.assertRaises(browser_tool.BrowserError):
            page.wait_for(text="never", timeout=0.05)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_wait_for_timeout_is_timeout_error(self):
        cdp = FakeCDP(
            eval_handler=eval_router({"result": {"type": "object", "value": {"found": False}}})
        )
        page = browser_tool.Page(cdp, poll=0.01)
        with self.assertRaises(TimeoutError):
            page.wait_for(selector="#x", timeout=0.05)

    def test_wait_for_success_returns_selector(self):
        cdp = FakeCDP(eval_handler=eval_router(descriptor(selector="#ready", text="Ready")))
        page = browser_tool.Page(cdp, poll=0.01)
        out = page.wait_for(text="Ready", timeout=1)
        self.assertEqual(out["selector"], "#ready")


# --------------------------------------------------------------------------- #
# Happy paths through the fake connection
# --------------------------------------------------------------------------- #
class HappyPathTests(unittest.TestCase):
    def test_goto_sends_page_navigate(self):
        cdp = FakeCDP(
            responses={"Page.navigate": {"frameId": "F1"}},
            eval_handler=lambda expr: {"result": {"type": "string", "value": "complete"}},
        )
        page = browser_tool.Page(cdp)
        page.goto("http://example.test/")
        self.assertIn(("Page.navigate", {"url": "http://example.test/"}), cdp.calls)
        self.assertIn("Runtime.evaluate", cdp.methods())

    def test_screenshot_decodes_base64_and_writes_file(self):
        payload = b"\x89PNG\r\n\x1a\nfake-image-bytes"
        cdp = FakeCDP(
            responses={
                "Page.captureScreenshot": {
                    "data": base64.b64encode(payload).decode("ascii")
                }
            }
        )
        page = browser_tool.Page(cdp)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "shot.png")
            out = page.screenshot(path)
            self.assertEqual(out, os.path.abspath(path))
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), payload)
        self.assertEqual(cdp.calls[0][0], "Page.captureScreenshot")
        self.assertEqual(cdp.calls[0][1], {"format": "png"})

    def test_text_returns_and_limits(self):
        cdp = FakeCDP(
            eval_handler=lambda expr: {"result": {"type": "string", "value": "abcdef"}}
        )
        page = browser_tool.Page(cdp)
        self.assertEqual(page.text(limit=3), "abc")

    def test_text_missing_selector_raises(self):
        cdp = FakeCDP(
            eval_handler=lambda expr: {"result": {"type": "string", "value": None}}
        )
        page = browser_tool.Page(cdp)
        with self.assertRaises(browser_tool.BrowserError):
            page.text("#missing")

    def test_url_and_title(self):
        values = {"location.href": "http://x/", "document.title": "T"}
        cdp = FakeCDP(
            eval_handler=lambda expr: {
                "result": {"type": "string", "value": values.get(expr, "")}
            }
        )
        page = browser_tool.Page(cdp)
        self.assertEqual(page.url(), "http://x/")
        self.assertEqual(page.title(), "T")

    def test_find_returns_stable_selector(self):
        cdp = FakeCDP(eval_handler=eval_router(descriptor(selector="#go", text="Go")))
        page = browser_tool.Page(cdp)
        self.assertEqual(page.find(text="Go"), {"selector": "#go", "text": "Go"})

    def test_press_enter_sends_keydown_and_keyup(self):
        cdp = FakeCDP()
        page = browser_tool.Page(cdp)
        page.press("Enter")
        key_events = [p for m, p in cdp.calls if m == "Input.dispatchKeyEvent"]
        self.assertEqual([e["type"] for e in key_events], ["keyDown", "keyUp"])
        self.assertEqual(key_events[0]["key"], "Enter")
        self.assertEqual(key_events[0]["windowsVirtualKeyCode"], 13)


# --------------------------------------------------------------------------- #
# _CDP transport
# --------------------------------------------------------------------------- #
class CDPTransportTests(unittest.TestCase):
    def test_call_sends_jsonrpc_and_returns_result(self):
        ws = FakeWS({"frameId": "F1"})
        cdp = browser_tool._CDP(ws, timeout=1.0)
        out = cdp.call("Page.navigate", {"url": "http://x/"})
        self.assertEqual(out, {"frameId": "F1"})
        self.assertEqual(ws.sent[0]["method"], "Page.navigate")
        self.assertEqual(ws.sent[0]["params"], {"url": "http://x/"})
        self.assertIn("id", ws.sent[0])

    def test_call_raises_on_error_response(self):
        ws = ErrorWS()
        cdp = browser_tool._CDP(ws, timeout=1.0)
        with self.assertRaises(browser_tool.BrowserError):
            cdp.call("Runtime.evaluate")

    def test_call_skips_stray_events(self):
        ws = EventThenResultWS({"ok": True})
        cdp = browser_tool._CDP(ws, timeout=1.0)
        self.assertEqual(cdp.call("Browser.getVersion"), {"ok": True})

    def test_call_times_out(self):
        ws = FakeWS(None)  # recv always raises TimeoutError
        cdp = browser_tool._CDP(ws, timeout=0.01)
        with self.assertRaises(browser_tool.BrowserError):
            cdp.call("Runtime.evaluate")


# --------------------------------------------------------------------------- #
# Browser resolution + profile guard (filesystem only)
# --------------------------------------------------------------------------- #
class BrowserResolutionTests(unittest.TestCase):
    def test_unknown_browser_raises(self):
        with self.assertRaises(browser_tool.BrowserNotFoundError):
            browser_tool.find_browser("netscape-navigator-9000")

    def test_edge_if_installed(self):
        path = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
        if not os.path.isfile(path):
            self.skipTest("Edge not installed on this machine")
        self.assertEqual(browser_tool.find_browser("edge"), path)

    def test_default_profile_rejected(self):
        bad = os.path.expanduser(
            "~/Library/Application Support/Microsoft Edge"
        )
        with self.assertRaises(browser_tool.BrowserError):
            browser_tool._assert_non_default_profile(bad)

    def test_temp_profile_accepted(self):
        good = os.path.join(tempfile.gettempdir(), "jarvis-browser-profiles", "9222")
        self.assertEqual(
            browser_tool._assert_non_default_profile(good), os.path.abspath(good)
        )

    def test_loopback_only(self):
        with self.assertRaises(browser_tool.BrowserError):
            browser_tool._assert_loopback("http://example.com:9222/json/list")


# --------------------------------------------------------------------------- #
# Deterministic tab selection (marked tab)
# --------------------------------------------------------------------------- #
def _page_target(tid, url, ws=None):
    return {
        "id": tid,
        "type": "page",
        "url": url,
        "title": "",
        "webSocketDebuggerUrl": ws
        or ("ws://127.0.0.1:9222/devtools/page/%s" % tid),
    }


class MarkedTabSelectionTests(unittest.TestCase):
    def _patched(self, page_list, names):
        def fake_read(ws_url, timeout=None):
            return names.get(ws_url)

        return mock.patch.multiple(
            browser_tool,
            pages=mock.Mock(return_value=page_list),
            _read_window_name=mock.Mock(side_effect=fake_read),
        )

    def test_marked_tab_chosen_over_extension(self):
        ext = _page_target("ext", "chrome-extension://abc/welcome.html")
        marked = _page_target("marked", "http://localhost:8000/")
        names = {marked["webSocketDebuggerUrl"]: browser_tool.MARKER_NAME}
        with self._patched([ext, marked], names):
            info = browser_tool.select_page(9222)
        self.assertEqual(info["id"], "marked")

    def test_default_skips_extension_and_chrome(self):
        ext = _page_target("ext", "chrome-extension://abc/welcome.html")
        chrome = _page_target("chrome", "chrome://settings/")
        real = _page_target("real", "http://localhost:8000/")
        with self._patched([ext, chrome, real], {}):
            info = browser_tool.select_page(9222)
        self.assertEqual(info["id"], "real")

    def test_fallback_without_marked_picks_real_not_extension(self):
        ext = _page_target("ext", "chrome-extension://abc/welcome.html")
        real = _page_target("real", "about:blank")
        with self._patched([ext, real], {}):
            info = browser_tool.select_page(9222, marked=False)
        self.assertEqual(info["id"], "real")

    def test_only_internal_pages_raises(self):
        ext = _page_target("ext", "chrome-extension://abc/welcome.html")
        chrome = _page_target("chrome", "chrome://settings/")
        with self._patched([ext, chrome], {}):
            with self.assertRaises(browser_tool.BrowserError):
                browser_tool.select_page(9222)

    def test_explicit_index_is_honoured(self):
        ext = _page_target("ext", "chrome-extension://abc/welcome.html")
        real = _page_target("real", "http://localhost:8000/")
        with self._patched([ext, real], {}):
            self.assertEqual(browser_tool.select_page(9222, index=0)["id"], "ext")
            self.assertEqual(browser_tool.select_page(9222, index=1)["id"], "real")
            with self.assertRaises(browser_tool.BrowserError):
                browser_tool.select_page(9222, index=9)

    def test_explicit_url_contains(self):
        local = _page_target("local", "http://localhost:8000/")
        other = _page_target("other", "https://example.test/")
        with self._patched([local, other], {}):
            self.assertEqual(
                browser_tool.select_page(9222, url_contains="example.test")["id"],
                "other",
            )
            with self.assertRaises(browser_tool.BrowserError):
                browser_tool.select_page(9222, url_contains="no-such-url")

    def test_connect_page_opens_marked_ws(self):
        ext = _page_target("ext", "chrome-extension://abc/x.html")
        marked = _page_target("marked", "http://localhost:8000/")
        names = {marked["webSocketDebuggerUrl"]: browser_tool.MARKER_NAME}
        opened = []

        def fake_open(url, timeout=None):
            opened.append(url)
            return FakeWS(None)

        with mock.patch.multiple(
            browser_tool,
            pages=mock.Mock(return_value=[ext, marked]),
            _read_window_name=mock.Mock(
                side_effect=lambda ws, timeout=None: names.get(ws)
            ),
            _open_ws=mock.Mock(side_effect=fake_open),
        ):
            page = browser_tool.connect_page(9222)
        self.assertIsInstance(page, browser_tool.Page)
        self.assertEqual(opened, [marked["webSocketDebuggerUrl"]])

    def test_ensure_marked_tab_creates_and_tags(self):
        created = _page_target("new", "about:blank")
        calls = {"n": 0}

        def fake_pages(port, timeout=5.0):
            calls["n"] += 1
            return [] if calls["n"] == 1 else [created]

        sent = []

        class RecordingWS(FakeWS):
            def send(self, raw):
                msg = json.loads(raw)
                sent.append(msg)
                if msg["method"] == "Target.createTarget":
                    result = {"targetId": "new"}
                else:
                    result = {}
                self._queue.append(json.dumps({"id": msg["id"], "result": result}))

        with mock.patch.multiple(
            browser_tool,
            pages=mock.Mock(side_effect=fake_pages),
            _read_window_name=mock.Mock(return_value=None),
            browser_version=mock.Mock(
                return_value={
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/b"
                }
            ),
            _open_ws=mock.Mock(side_effect=lambda url, timeout=None: RecordingWS()),
        ):
            info = browser_tool.ensure_marked_tab(9222)
        self.assertEqual(info["id"], "new")
        methods = [m["method"] for m in sent]
        self.assertIn("Target.createTarget", methods)
        self.assertIn("Runtime.evaluate", methods)
        evals = [m for m in sent if m["method"] == "Runtime.evaluate"]
        self.assertIn(browser_tool.MARKER_NAME, json.dumps(evals))


# --------------------------------------------------------------------------- #
# Launch hardening + window size
# --------------------------------------------------------------------------- #
class LaunchArgsTests(unittest.TestCase):
    def _run(self, **kwargs):
        captured = {}

        class FakeProc:
            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        def fake_popen(args, **kw):
            captured["args"] = list(args)
            return FakeProc()

        with mock.patch.multiple(
            browser_tool,
            find_browser=mock.Mock(return_value="/Applications/Fake Edge"),
            browser_version=mock.Mock(return_value={"Browser": "Fake"}),
        ), mock.patch.object(browser_tool.os, "makedirs"), mock.patch.object(
            browser_tool.subprocess, "Popen", side_effect=fake_popen
        ):
            handle = browser_tool.launch(port=9335, **kwargs)
        return captured["args"], handle

    def test_launch_includes_window_size_and_hardening(self):
        args, handle = self._run()
        self.assertIn("--window-size=1280,900", args)
        for flag in (
            "--disable-extensions",
            "--disable-component-extensions-with-background-pages",
            "--disable-sync",
            "--no-service-autorun",
            "--password-store=basic",
        ):
            self.assertIn(flag, args)
        self.assertIn("--remote-debugging-port=9335", args)
        self.assertTrue(any(a.startswith("--user-data-dir=") for a in args))
        self.assertTrue(handle.user_data_dir.startswith(tempfile.gettempdir()))

    def test_launch_window_size_none_omits_flag(self):
        args, _ = self._run(window_size=None)
        self.assertFalse(any(a.startswith("--window-size") for a in args))

    def test_launch_custom_window_size(self):
        args, _ = self._run(window_size=(1000, 700))
        self.assertIn("--window-size=1000,700", args)

    def test_parse_window_size(self):
        self.assertEqual(browser_tool._parse_window_size("1280x900"), (1280, 900))
        self.assertEqual(browser_tool._parse_window_size(" 800X600 "), (800, 600))
        self.assertIsNone(browser_tool._parse_window_size("none"))
        self.assertIsNone(browser_tool._parse_window_size(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)