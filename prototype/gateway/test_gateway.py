"""Tests for the Jarvis Gateway (auth, quota, metering, model gating).

Offline: the upstream provider is faked, so no network and no real keys."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import gateway  # noqa: E402


class _Resp:
    def __init__(self, body: dict, status: int = 200):
        self._body = body
        self.status_code = status

    def json(self) -> dict:
        return self._body


class GatewayTests(unittest.TestCase):
    def setUp(self):
        gateway.STORE = gateway.Store(Path(tempfile.mkdtemp()) / "gw.json")
        gateway.ADMIN_KEY = "adm"
        gateway.UPSTREAM_KEY = "up"
        gateway.PLANS = {
            "free": {"label": "Free", "tokens_month": 100, "models": ["deepseek-v4.1-flash"]},
            "pro": {"label": "Pro", "tokens_month": 100000, "models": ["*"]},
        }
        self.client = TestClient(gateway.app)

    def _token(self, plan="pro"):
        token, _ = gateway.STORE.add_user(plan)
        return token

    def test_health(self):
        self.assertTrue(self.client.get("/api/health").json()["ok"])

    def test_auth_required(self):
        r = self.client.post("/v1/chat/completions", json={"model": "x", "messages": []})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.client.get("/v1/models").status_code, 401)

    def test_admin_mint_requires_key(self):
        self.assertEqual(self.client.post("/admin/users", json={"plan": "pro"}).status_code, 403)
        r = self.client.post("/admin/users", json={"plan": "pro"},
                             headers={"authorization": "Bearer adm"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["token"].startswith("sk-jarvis-"))

    def test_chat_meters_usage(self):
        body = {"choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"total_tokens": 42}}
        orig = httpx.AsyncClient.post

        async def fake_post(self, url, json=None, headers=None):
            return _Resp(body)

        httpx.AsyncClient.post = fake_post
        try:
            token = self._token("pro")
            r = self.client.post("/v1/chat/completions",
                                 json={"model": "deepseek-v4.1-flash",
                                       "messages": [{"role": "user", "content": "hi"}]},
                                 headers={"authorization": f"Bearer {token}"})
            self.assertEqual(r.status_code, 200)
            user = gateway.STORE.by_token(token)
            self.assertEqual(gateway.STORE.used_this_period(user), 42)
        finally:
            httpx.AsyncClient.post = orig

    def test_quota_enforced(self):
        token = self._token("free")
        gateway.STORE.record(token, 100)  # cap is 100
        r = self.client.post("/v1/chat/completions",
                             json={"model": "deepseek-v4.1-flash", "messages": []},
                             headers={"authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 429)

    def test_model_gating(self):
        token = self._token("free")  # free only allows deepseek-v4.1-flash
        r = self.client.post("/v1/chat/completions",
                             json={"model": "gpt-5", "messages": []},
                             headers={"authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 403)

    def test_me_reports_plan_usage(self):
        token = self._token("pro")
        gateway.STORE.record(token, 7)
        r = self.client.get("/api/me", headers={"authorization": f"Bearer {token}"})
        j = r.json()
        self.assertEqual(j["plan"], "pro")
        self.assertEqual(j["tokens_used"], 7)
        self.assertEqual(j["tokens_cap"], 100000)

    def test_demo_signup_mints_token(self):
        gateway._DEMO_SIGNUPS.clear()
        os.environ["GATEWAY_DEMO_OPEN"] = "1"
        try:
            r = self.client.post("/api/signup", json={})
        finally:
            os.environ.pop("GATEWAY_DEMO_OPEN", None)
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertTrue(j["token"].startswith("sk-jarvis-"))
        self.assertEqual(j["plan"], "free")
        self.assertEqual(gateway.STORE.by_token(j["token"])["plan"], "free")

    def test_demo_signup_closed_by_default(self):
        os.environ.pop("GATEWAY_DEMO_OPEN", None)
        self.assertEqual(self.client.post("/api/signup", json={}).status_code, 403)

    def test_demo_signup_rate_limited(self):
        gateway._DEMO_SIGNUPS.clear()
        os.environ["GATEWAY_DEMO_OPEN"] = "1"
        old = gateway._DEMO_MAX_PER_IP
        gateway._DEMO_MAX_PER_IP = 2
        try:
            self.assertEqual(self.client.post("/api/signup", json={}).status_code, 200)
            self.assertEqual(self.client.post("/api/signup", json={}).status_code, 200)
            self.assertEqual(self.client.post("/api/signup", json={}).status_code, 429)
        finally:
            gateway._DEMO_MAX_PER_IP = old
            os.environ.pop("GATEWAY_DEMO_OPEN", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
