"""Tests for first-run access provisioning (jarvis_access)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402

import jarvis_access  # noqa: E402
import providers.registry as registry  # noqa: E402


class AccessTests(unittest.TestCase):
    def setUp(self):
        self._file = jarvis_access._TOKEN_FILE
        jarvis_access._TOKEN_FILE = Path(tempfile.mkdtemp()) / "gateway-token"
        self._list = registry.list_providers
        self._reset = registry.reset_cache
        registry.list_providers = lambda: []          # no BYOK
        registry.reset_cache = lambda: None
        self._env = {k: os.environ.get(k) for k in ("JARVIS_TOKEN", "JARVIS_GATEWAY_URL")}
        os.environ.pop("JARVIS_TOKEN", None)
        os.environ["JARVIS_GATEWAY_URL"] = "http://gw.test"
        os.environ["JARVIS_DEMO_SIGNUP"] = "1"

    def tearDown(self):
        jarvis_access._TOKEN_FILE = self._file
        registry.list_providers = self._list
        registry.reset_cache = self._reset
        os.environ.pop("JARVIS_DEMO_SIGNUP", None)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_no_gateway(self):
        os.environ.pop("JARVIS_GATEWAY_URL", None)
        self.assertEqual(jarvis_access.ensure_access()["status"], "no-gateway")

    def test_byok_short_circuits(self):
        registry.list_providers = lambda: [{"provider_id": "openai", "configured": True}]
        self.assertEqual(jarvis_access.ensure_access()["status"], "byok")

    def test_provisions_token(self):
        class _R:
            def raise_for_status(self): pass

            def json(self): return {"token": "sk-jarvis-demo", "plan": "free"}

        orig = httpx.post
        httpx.post = lambda *a, **k: _R()
        try:
            res = jarvis_access.ensure_access()
        finally:
            httpx.post = orig
        self.assertEqual(res["status"], "provisioned")
        self.assertTrue(jarvis_access._TOKEN_FILE.is_file())
        self.assertTrue(jarvis_access.token_present())

    def test_gateway_error_is_soft(self):
        def boom(*a, **k):
            raise httpx.ConnectError("down")

        orig = httpx.post
        httpx.post = boom
        try:
            res = jarvis_access.ensure_access()
        finally:
            httpx.post = orig
        self.assertEqual(res["status"], "error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
