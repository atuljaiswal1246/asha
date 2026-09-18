"""Tests for the image tool's paid-first / free-fallback contract.

Zero network: every request goes through ``httpx.MockTransport``. The generated
image dir is redirected to a temp dir so nothing is written into the repo.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402

import imagegen_tool as imagegen  # noqa: E402

# Leonardo's guard rejects images under 512 bytes; Pollinations' under 1024.
_IMG = b"\xff\xd8\xff" + b"p" * 700
_FREE_IMG = b"\xff\xd8\xff" + b"f" * 2000
_LEO_BASE = "https://leo.test/api/rest/v1"
_LEO_IMG = "https://cdn.test/img.jpg"

_ENV_KEYS = (
    "IMAGE_PROVIDER",
    "IMAGE_FREE_FALLBACK",
    "LEONARDO_API_KEY",
    "LEONARDO_BASE_URL",
    "LEONARDO_MODEL_ID",
    "LEONARDO_ALCHEMY",
    "LEONARDO_POLL_TIMEOUT",
    "LEONARDO_POLL_INTERVAL",
)


class Calls:
    def __init__(self):
        self.paid = 0
        self.free = 0


def _leo_ok(request):
    if request.method == "POST":
        return httpx.Response(200, json={"sdGenerationJob": {"generationId": "G1"}})
    if request.url.path.endswith("/G1"):
        return httpx.Response(200, json={
            "generations_by_pk": {
                "status": "COMPLETE",
                "generated_images": [{"url": _LEO_IMG}],
            }
        })
    return httpx.Response(200, content=_IMG, headers={"content-type": "image/jpeg"})


def _free_ok(request):
    return httpx.Response(200, content=_FREE_IMG, headers={"content-type": "image/jpeg"})


def _routed(leo, free, calls):
    def handler(request):
        if "pollinations.ai" in request.url.host:
            calls.free += 1
            return free(request)
        calls.paid += 1
        return leo(request)

    return httpx.MockTransport(handler)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._dir = imagegen._GENERATED_DIR
        self._poll_timeout = imagegen._LEONARDO_POLL_TIMEOUT
        self._poll_interval = imagegen._LEONARDO_POLL_INTERVAL
        imagegen._GENERATED_DIR = self.tmp
        imagegen._LEONARDO_POLL_TIMEOUT = 0.02
        imagegen._LEONARDO_POLL_INTERVAL = 0
        self._env = {k: os.environ.get(k) for k in _ENV_KEYS}
        os.environ.pop("IMAGE_PROVIDER", None)
        os.environ.pop("IMAGE_FREE_FALLBACK", None)
        os.environ["LEONARDO_API_KEY"] = "test-key"
        os.environ["LEONARDO_BASE_URL"] = _LEO_BASE
        os.environ.pop("LEONARDO_MODEL_ID", None)
        os.environ.pop("LEONARDO_ALCHEMY", None)

    def tearDown(self):
        imagegen._GENERATED_DIR = self._dir
        imagegen._LEONARDO_POLL_TIMEOUT = self._poll_timeout
        imagegen._LEONARDO_POLL_INTERVAL = self._poll_interval
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_(self, coro):
        return asyncio.run(coro)


class PaidFirstTests(Base):
    def test_paid_success_returns_bare_path_and_never_calls_free(self):
        calls = Calls()
        result = self.run_(imagegen.generate_image(
            "a red fox", transport=_routed(_leo_ok, _free_ok, calls)))
        self.assertTrue(result.startswith("/generated/"))
        self.assertNotIn("#", result)
        self.assertNotIn(imagegen._FREE_FALLBACK_NOTE, result)
        self.assertGreaterEqual(calls.paid, 1)
        self.assertEqual(calls.free, 0)

    def test_paid_success_does_not_call_a_mocked_free(self):
        async def boom(*_a, **_k):
            raise AssertionError("free provider called on a paid success")

        original = imagegen._pollinations
        imagegen._pollinations = boom
        try:
            result = self.run_(imagegen.generate_image(
                "a red fox",
                transport=_routed(_leo_ok, _leo_ok, Calls())))
        finally:
            imagegen._pollinations = original
        self.assertTrue(result.startswith("/generated/"))


class PaidFailureFallbackTests(Base):
    def _leo_fail(self, kind):
        if kind == "http_500":
            return lambda r: httpx.Response(500, text="server error")
        if kind == "http_401":
            return lambda r: httpx.Response(401, json={})
        if kind == "http_403":
            return lambda r: httpx.Response(403, json={})
        if kind == "http_429_quota":
            return lambda r: httpx.Response(429, text="quota exceeded")
        if kind == "no_generation_id":
            return lambda r: httpx.Response(200, json={})
        if kind == "generation_failed":
            def h(r):
                return httpx.Response(200, json={"generations_by_pk": {"status": "FAILED"}})
            return h
        if kind == "poll_timeout":
            return lambda r: httpx.Response(200, json={"generations_by_pk": {"status": "PENDING"}})
        if kind == "download_not_image":
            def h(r):
                if r.url.host == "cdn.test":
                    return httpx.Response(200, text="nope", headers={"content-type": "text/plain"})
                return _leo_ok(r)
            return h
        if kind == "empty_image":
            def h(r):
                if r.url.host == "cdn.test":
                    return httpx.Response(200, content=b"x", headers={"content-type": "image/jpeg"})
                return _leo_ok(r)
            return h
        if kind == "timeout_exception":
            def h(r):
                raise httpx.ConnectTimeout("timed out")
            return h
        if kind == "generic_exception":
            def h(r):
                raise ValueError("boom")
            return h
        raise AssertionError(f"unknown kind {kind}")

    def test_each_paid_failure_kind_falls_back_to_free_with_note(self):
        kinds = (
            "http_500", "http_401", "http_403", "http_429_quota",
            "no_generation_id", "generation_failed", "poll_timeout",
            "download_not_image", "empty_image", "timeout_exception",
            "generic_exception",
        )
        for kind in kinds:
            with self.subTest(kind=kind):
                calls = Calls()
                transport = _routed(self._leo_fail(kind), _free_ok, calls)
                result = self.run_(imagegen.generate_image("a red fox", transport=transport))
                self.assertGreaterEqual(calls.paid, 1, kind)
                self.assertEqual(calls.free, 1, kind)
                self.assertTrue(result.startswith("/generated/"), result)
                self.assertEqual(result.split("#", 1)[1], imagegen._FREE_FALLBACK_NOTE, result)

    def test_missing_key_falls_back_to_free(self):
        os.environ.pop("LEONARDO_API_KEY", None)
        calls = Calls()
        result = self.run_(imagegen.generate_image(
            "a red fox", transport=_routed(_leo_ok, _free_ok, calls)))
        self.assertEqual(calls.paid, 0)
        self.assertEqual(calls.free, 1)
        self.assertEqual(result.split("#", 1)[1], imagegen._FREE_FALLBACK_NOTE)

    def test_note_is_the_exact_speakable_sentence(self):
        self.assertEqual(
            imagegen._FREE_FALLBACK_NOTE,
            "I used the free image model - the paid allowance is used up.",
        )

    def test_paid_error_text_never_leaks_into_result(self):
        calls = Calls()
        result = self.run_(imagegen.generate_image(
            "a red fox",
            transport=_routed(self._leo_fail("http_429_quota"), _free_ok, calls)))
        self.assertNotIn("[imagegen error]", result)
        self.assertNotIn("429", result)
        self.assertNotIn("quota", result)


class BothFailTests(Base):
    def test_both_fail_is_one_honest_sentence(self):
        def free_500(r):
            return httpx.Response(500, text="down")

        calls = Calls()
        result = self.run_(imagegen.generate_image(
            "a red fox", transport=_routed(lambda r: httpx.Response(500), free_500, calls)))
        self.assertEqual(result, imagegen._BOTH_FAILED_MSG)
        self.assertNotIn("[imagegen error]", result)
        self.assertNotIn("Traceback", result)
        self.assertNotIn("\n", result)
        self.assertEqual(calls.free, 1)

    def test_empty_prompt_is_speakable(self):
        result = self.run_(imagegen.generate_image("   "))
        self.assertEqual(result, imagegen._EMPTY_PROMPT_MSG)


class FallbackToggleTests(Base):
    def test_disabled_fallback_never_calls_free(self):
        os.environ["IMAGE_FREE_FALLBACK"] = "0"
        calls = Calls()
        result = self.run_(imagegen.generate_image(
            "a red fox",
            transport=_routed(lambda r: httpx.Response(500), _free_ok, calls)))
        self.assertEqual(calls.free, 0)
        self.assertEqual(result, imagegen._PAID_FAILED_MSG)

    def test_default_is_on(self):
        self.assertTrue(imagegen._free_fallback_enabled())


class FreeOnlyTests(Base):
    def test_explicit_pollinations_skips_paid_and_has_no_note(self):
        os.environ["IMAGE_PROVIDER"] = "pollinations"
        calls = Calls()
        result = self.run_(imagegen.generate_image(
            "a red fox", transport=_routed(_leo_ok, _free_ok, calls)))
        self.assertTrue(result.startswith("/generated/"))
        self.assertNotIn("#", result)
        self.assertNotIn(imagegen._FREE_FALLBACK_NOTE, result)
        self.assertEqual(calls.paid, 0)
        self.assertEqual(calls.free, 1)

    def test_explicit_pollinations_failure_is_honest(self):
        os.environ["IMAGE_PROVIDER"] = "pollinations"
        calls = Calls()
        result = self.run_(imagegen.generate_image(
            "a red fox",
            transport=_routed(_leo_ok, lambda r: httpx.Response(500), calls)))
        self.assertEqual(result, imagegen._FREE_FAILED_MSG)
        self.assertEqual(calls.paid, 0)


class PollinationsUnitTests(Base):
    def test_success_saves_and_returns_path(self):
        result = self.run_(imagegen._pollinations(
            "a red fox", 256, 256, 7,
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=_FREE_IMG,
                                         headers={"content-type": "image/jpeg"}))))
        self.assertTrue(result.startswith("/generated/"))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, os.path.basename(result))))

    def test_non_image_response_is_an_error(self):
        result = self.run_(imagegen._pollinations(
            "a red fox", 256, 256, 7,
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, text="no", headers={"content-type": "text/html"}))))
        self.assertTrue(result.startswith(imagegen._ERROR_PREFIX))

    def test_too_small_response_is_an_error(self):
        result = self.run_(imagegen._pollinations(
            "a red fox", 256, 256, 7,
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=b"x",
                                         headers={"content-type": "image/jpeg"}))))
        self.assertTrue(result.startswith(imagegen._ERROR_PREFIX))


class HandleTests(Base):
    def test_handle_forwards_prompt_and_dims(self):
        seen = {}

        async def fake(prompt, width=512, height=512, seed=-1, transport=None):
            seen.update(prompt=prompt, width=width, height=height)
            return "/generated/ok.jpg"

        original = imagegen.generate_image
        imagegen.generate_image = fake
        try:
            out = self.run_(imagegen.handle_generate_image(
                {"prompt": "a red fox", "width": 300, "height": 400}))
        finally:
            imagegen.generate_image = original
        self.assertEqual(out, "/generated/ok.jpg")
        self.assertEqual(seen["prompt"], "a red fox")
        self.assertEqual(seen["width"], 300)
        self.assertEqual(seen["height"], 400)


class SchemaUnchangedTests(unittest.TestCase):
    def test_schema_name_description_and_args_unchanged(self):
        s = imagegen._generate_image_schema
        self.assertEqual(s.name, "generate_image")
        self.assertIn("Generate an image from a text description", s.description)
        self.assertEqual(list(s.properties.keys()), ["prompt", "width", "height"])
        self.assertEqual(s.required, ["prompt"])


if __name__ == "__main__":
    unittest.main()
