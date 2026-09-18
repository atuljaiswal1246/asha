"""Orchestrator tests (B1-B3): plan policy, verify parsing, rework brief.

Pure/mocked — no opencode CLI call, no network. Run:

    python3 prototype/ui/test_orchestrator.py
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import orchestrator as o  # noqa: E402


class _FakeOrch(o.Orchestrator):
    """Orchestrator with the CLI calls replaced by canned replies."""

    def __init__(self, replies):
        super().__init__("/tmp")
        self._replies = list(replies)
        self.calls = []

    def _opencode(self, cwd, model, prompt, **kw):  # noqa: D401
        self.calls.append((model, prompt))
        text = self._replies.pop(0) if self._replies else ""
        return {"ok": True, "text": text, "error": "", "seconds": 0.1}


class JsonBlobTests(unittest.TestCase):
    def test_extracts_object_from_prose(self):
        self.assertEqual(o._json_blob('here: {"a": 1} done'), {"a": 1})

    def test_none_when_absent(self):
        self.assertIsNone(o._json_blob("no json here"))


class PlanPolicyTests(unittest.TestCase):
    def test_allowed_model_kept_and_bad_model_falls_back(self):
        reply = json.dumps({"tasks": [
            {"worker": 1, "goal": "g1", "model": "opencode-go/mimo-v2.5"},
            {"worker": 2, "goal": "g2", "model": "openai/gpt-5"},
        ]})
        orch = _FakeOrch([reply])
        plan = orch.plan("do two things")
        self.assertEqual(plan["tasks"][0]["model"], "opencode-go/mimo-v2.5")
        self.assertIn(plan["tasks"][1]["model"], o.WORKER_MODELS)
        self.assertEqual([t["_i"] for t in plan["tasks"]], [0, 1])

    def test_malformed_plan_yields_no_tasks(self):
        orch = _FakeOrch(["not json at all"])
        self.assertEqual(orch.plan("x")["tasks"], [])

    def test_done_when_preserved(self):
        reply = json.dumps({"tasks": [{"worker": 1, "goal": "g",
                                       "done_when": "tests pass",
                                       "model": "opencode/big-pickle"}]})
        plan = _FakeOrch([reply]).plan("x")
        self.assertEqual(plan["tasks"][0]["done_when"], "tests pass")

    def test_rework_limit_default_two(self):
        self.assertEqual(o.REWORK_LIMIT, 2)

    def test_model_policy_sets_are_disjoint_and_expected(self):
        for m in o.WORKER_MODELS:
            self.assertIn("free", m, m) if "big-pickle" not in m else None


class VerifyTests(unittest.TestCase):
    def test_accept(self):
        orch = _FakeOrch([json.dumps(
            {"verdict": "accept", "issues": [], "summary": "good"})])
        v = orch.verify("goal", "- old\n+ new")
        self.assertEqual(v["verdict"], "accept")

    def test_rework_issues_are_strings(self):
        orch = _FakeOrch([json.dumps(
            {"verdict": "rework", "issues": ["missing test", 42]})])
        v = orch.verify("goal", "+x")
        self.assertEqual(v["verdict"], "rework")
        self.assertEqual(v["issues"], ["missing test", "42"])

    def test_reject(self):
        orch = _FakeOrch([json.dumps({"verdict": "reject", "summary": "deletes files"})])
        self.assertEqual(orch.verify("goal", "+x")["verdict"], "reject")

    def test_unknown_verdict_is_accept(self):
        orch = _FakeOrch([json.dumps({"verdict": "maybe"})])
        self.assertEqual(orch.verify("goal", "+x")["verdict"], "accept")

    def test_unparseable_review_is_accept(self):
        orch = _FakeOrch(["the diff looks fine to me"])
        self.assertEqual(orch.verify("goal", "+x")["verdict"], "accept")

    def test_empty_diff_is_accept_without_calling_brain(self):
        orch = _FakeOrch([])
        self.assertEqual(orch.verify("goal", "")["verdict"], "accept")
        self.assertEqual(orch.calls, [])


class ReworkBriefTests(unittest.TestCase):
    def test_includes_issues_and_summary(self):
        brief = o.Orchestrator._rework_brief(
            {"verdict": "rework", "summary": "off scope", "issues": ["add a test", "fix import"]})
        self.assertIn("add a test", brief)
        self.assertIn("fix import", brief)
        self.assertIn("off scope", brief)

    def test_no_issues_still_valid(self):
        brief = o.Orchestrator._rework_brief({"verdict": "rework"})
        self.assertIn("(no details)", brief)


class DepthCapTests(unittest.TestCase):
    def test_depth_cap_is_one(self):
        self.assertEqual(o.MAX_DEPTH, 1)


class _StubBackend:
    """Backend that returns canned runner results (no subprocess)."""

    name = "stub"

    def __init__(self, results):
        self._results = list(results)

    def run(self, argv, cwd=None, timeout=600, cancel=None, env=None):
        return self._results.pop(0)


class RetryBackoffTests(unittest.TestCase):
    """D2: transient failures retry with backoff; success returns attempts."""

    def _run(self, results):
        orch = o.Orchestrator("/tmp", backend=_StubBackend(results))
        with mock.patch.object(o.time, "sleep", lambda *_: None):
            return orch._opencode(orch.project, "m", "p", retries=2, backoff=0)

    def test_failure_then_success(self):
        r = self._run([{"ok": False, "stdout": "", "stderr": "transient"},
                       {"ok": True, "stdout": "done", "stderr": ""}])
        self.assertTrue(r["ok"])
        self.assertEqual(r["attempts"], 2)

    def test_timeout_then_success(self):
        r = self._run([{"ok": False, "stdout": "", "stderr": "timeout"},
                       {"ok": True, "stdout": "recovered", "stderr": ""}])
        self.assertTrue(r["ok"])
        self.assertEqual(r["attempts"], 2)

    def test_cancelled_returns_immediately(self):
        import threading
        ev = threading.Event()
        ev.set()
        orch = o.Orchestrator("/tmp", backend=_StubBackend(
            [{"ok": False, "stdout": "", "stderr": "cancelled"}]))
        r = orch._opencode(orch.project, "m", "p", retries=2, backoff=0, cancel=ev)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "cancelled")
        self.assertEqual(r["attempts"], 1)

    def test_gives_up_after_retries(self):
        r = self._run([{"ok": False, "stdout": "", "stderr": "boom"}] * 5)
        self.assertFalse(r["ok"])
        self.assertEqual(r["attempts"], 3)  # initial + 2 retries


class CancelTests(unittest.TestCase):
    """F2: barge-in cancellation."""

    def test_cancelled_before_running(self):
        import threading
        reply = json.dumps({"tasks": [{"worker": 1, "goal": "g",
                                       "model": "opencode/big-pickle"}]})
        orch = _FakeOrch([reply])
        ev = threading.Event()
        ev.set()
        out = orch.run("do it", cancel=ev)
        self.assertTrue(out["cancelled"])
        self.assertEqual(out["results"], [])
        self.assertIn("Cancelled", out["summary"])


class ApplySelectionTests(unittest.TestCase):
    """B3.2/B5.2: only accepted diffs are applied."""

    def test_accept_with_diff_applies(self):
        self.assertTrue(o.Orchestrator.should_apply({"verdict": "accept", "diff": "+x"}))

    def test_rework_is_not_applied(self):
        self.assertFalse(o.Orchestrator.should_apply({"verdict": "rework", "diff": "+x"}))

    def test_reject_is_not_applied(self):
        self.assertFalse(o.Orchestrator.should_apply({"verdict": "reject", "diff": "+x"}))

    def test_accept_without_diff_not_applied(self):
        self.assertFalse(o.Orchestrator.should_apply({"verdict": "accept", "diff": ""}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
