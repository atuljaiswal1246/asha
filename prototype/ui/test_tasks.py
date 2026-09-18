"""Unit tests for the live task registry (tasks.py).

Pure in-memory, no network, no server. Run with:

    ../../.venv/bin/python -m unittest test_tasks -q
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agents  # noqa: E402
import tasks as tasks_mod  # noqa: E402
from tasks import TaskRegistry  # noqa: E402


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.reg = TaskRegistry()

    def test_create_then_update_sets_times(self):
        tid = self.reg.create("amit", "read the docs", files=["a.py"])
        row = self.reg.active()[0]
        self.assertEqual(row["agent_name"], "Amit")
        self.assertEqual(row["title"], "read the docs")
        self.assertEqual(row["status"], "working")
        self.assertEqual(row["files"], ["a.py"])
        self.assertIsNotNone(row["started_at"])
        self.assertIsNone(row["ended_at"])

        time.sleep(0.02)
        self.assertTrue(self.reg.update(tid, status="working", note="reading"))
        row = self.reg.active()[0]
        self.assertEqual(row["note"], "reading")
        self.assertGreaterEqual(row["updated_at"], row["started_at"])

    def test_update_rejects_unknown_status_and_id(self):
        tid = self.reg.create("amit", "t")
        self.assertFalse(self.reg.update(tid, status="bogus"))
        self.assertFalse(self.reg.update("nope", status="working"))

    def test_finish_marks_failed_and_sets_end(self):
        tid = self.reg.create("amit", "t")
        time.sleep(0.02)
        self.assertTrue(self.reg.finish(tid, ok=False, note="boom"))
        row = self.reg.active()[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["note"], "boom")
        self.assertIsNotNone(row["ended_at"])
        self.assertGreaterEqual(row["ended_at"], row["started_at"])
        self.assertFalse(self.reg.finish("nope", ok=True))

    def test_finish_ok_awaits_verification(self):
        tid = self.reg.create("priya", "run tests")
        self.reg.finish(tid, ok=True)
        row = self.reg.active()[0]
        self.assertEqual(row["status"], "verifying")
        self.assertIsNotNone(row["ended_at"])

    def test_verdict_records_reasons_and_attempts(self):
        tid = self.reg.create("priya", "run tests")
        self.reg.finish(tid, ok=True)
        self.assertTrue(self.reg.verdict(tid, "needs_fix", ["tests failed"]))
        row = self.reg.active()[0]
        self.assertEqual(row["status"], "needs_fix")
        self.assertEqual(row["verdict"], "needs_fix")
        self.assertEqual(row["reasons"], ["tests failed"])
        self.assertEqual(row["attempts"], 1)
        # An unrecognised verdict keeps the status but is still recorded.
        self.assertTrue(self.reg.verdict(tid, "inconclusive", ["no proof"]))
        row = self.reg.active()[0]
        self.assertEqual(row["status"], "needs_fix")
        self.assertEqual(row["verdict"], "inconclusive")
        self.assertEqual(row["attempts"], 2)


class RemovalTests(unittest.TestCase):
    def setUp(self):
        self.reg = TaskRegistry()

    def test_verified_is_removed(self):
        tid = self.reg.create("amit", "t")
        self.reg.finish(tid, ok=True)
        self.reg.verdict(tid, "verified", ["all checks passed"])
        self.assertTrue(self.reg.remove(tid))
        self.assertEqual(self.reg.active(), [])
        self.assertEqual(self.reg.summary_line(), "")

    def test_remove_verified_returns_ids_only(self):
        keep = self.reg.create("amit", "keep me")
        self.reg.finish(keep, ok=True)
        self.reg.verdict(keep, "needs_fix", ["fail"])

        gone = self.reg.create("priya", "gone")
        self.reg.finish(gone, ok=True)
        self.reg.verdict(gone, "verified", ["ok"])

        removed = self.reg.remove_verified()
        self.assertEqual(removed, [gone])
        self.assertEqual([r["id"] for r in self.reg.active()], [keep])

    def test_needs_fix_and_reported_stay_with_reasons(self):
        a = self.reg.create("amit", "fix this")
        self.reg.finish(a, ok=True)
        self.reg.verdict(a, "needs_fix", ["tests failed"])
        b = self.reg.create("priya", "scope slip")
        self.reg.finish(b, ok=True)
        self.reg.verdict(b, "reported", ["out-of-scope change: sneaky.py"])

        rows = {r["id"]: r for r in self.reg.active()}
        self.assertEqual(rows[a]["status"], "needs_fix")
        self.assertEqual(rows[a]["reasons"], ["tests failed"])
        self.assertEqual(rows[b]["status"], "reported")
        self.assertIn("sneaky.py", rows[b]["reasons"][0])


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.reg = TaskRegistry()

    def test_empty_when_nothing_is_live(self):
        self.assertEqual(self.reg.summary_line(), "")

    def test_names_agent_title_and_age(self):
        tid = self.reg.create("amit", "reading notes")
        # Backdate so the age is deterministic.
        self.reg._tasks[tid].started_at = time.time() - 42
        line = self.reg.summary_line()
        self.assertIn("Amit", line)
        self.assertIn("reading notes", line)
        self.assertIn("(42s)", line)
        self.assertTrue(line.startswith("1 agent: "), line)

    def test_note_wins_over_title_and_pluralises(self):
        a = self.reg.create("amit", "reading notes")
        self.reg.update(a, note="reading notes")
        self.reg.create("priya", "running tests")
        line = self.reg.summary_line()
        self.assertTrue(line.startswith("2 agents: "), line)
        self.assertIn("Amit - reading notes", line)
        self.assertIn("Priya - running tests", line)

    def test_verified_task_is_gone_from_the_line(self):
        tid = self.reg.create("amit", "t")
        self.reg.finish(tid, ok=True)
        self.assertNotEqual(self.reg.summary_line(), "")
        self.reg.verdict(tid, "verified", ["ok"])
        self.reg.remove(tid)
        self.assertEqual(self.reg.summary_line(), "")


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.reg = TaskRegistry()

    def test_drops_old_failed_but_never_a_live_one(self):
        stale = self.reg.create("amit", "old failed")
        self.reg.finish(stale, ok=False)
        self.reg._tasks[stale].started_at = time.time() - 100000
        self.reg._tasks[stale].ended_at = time.time() - 100000

        live = self.reg.create("priya", "still working")
        self.reg._tasks[live].started_at = time.time() - 100000

        dropped = self.reg.prune(max_age_secs=3600)
        self.assertEqual(dropped, [stale])
        self.assertEqual([r["id"] for r in self.reg.active()], [live])

    def test_drops_old_needs_fix(self):
        tid = self.reg.create("amit", "fix")
        self.reg.finish(tid, ok=True)
        self.reg.verdict(tid, "needs_fix", ["nope"])
        self.reg._tasks[tid].started_at = time.time() - 200000
        self.reg._tasks[tid].updated_at = time.time() - 200000
        self.reg._tasks[tid].ended_at = time.time() - 200000
        self.assertEqual(self.reg.prune(3600), [tid])


class AgentsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.reg = agents.AgentRegistry()

    def test_start_finish_use_the_registry(self):
        self.assertTrue(self.reg.start("amit", "read the docs"))
        row = self.reg.snapshot()[0]
        self.assertEqual(row["status"], "working")
        self.assertTrue(self.reg.note("amit", "reading"))
        self.assertTrue(self.reg.finish("amit", ok=True))

    def test_a_registry_error_never_changes_agent_state(self):
        with mock.patch.object(tasks_mod.tasks, "create",
                               side_effect=RuntimeError("boom")):
            self.assertTrue(self.reg.start("anjali", "t"))
            self.assertEqual(self.reg.snapshot()[1]["status"], "working")
        with mock.patch.object(tasks_mod.tasks, "update",
                               side_effect=RuntimeError("boom")):
            self.assertTrue(self.reg.note("anjali", "still fine"))
        with mock.patch.object(tasks_mod.tasks, "finish",
                               side_effect=RuntimeError("boom")):
            self.assertTrue(self.reg.finish("anjali", ok=False, note="x"))
            self.assertEqual(self.reg.snapshot()[1]["status"], "failed")


class ConcurrencyTests(unittest.TestCase):
    def test_parallel_create_update_does_not_corrupt_the_snapshot(self):
        reg = TaskRegistry()
        created: list[str] = []
        lock = threading.Lock()

        def worker(n: int):
            local = []
            for i in range(50):
                tid = reg.create(f"agent{n}", f"task {i}")
                reg.update(tid, note=f"n{n}-{i}")
                local.append(tid)
            with lock:
                created.extend(local)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rows = reg.active()
        self.assertEqual(len(rows), 400)
        self.assertEqual(len({r["id"] for r in rows}), 400)
        self.assertEqual({r["agent_id"] for r in rows},
                         {f"agent{n}" for n in range(8)})
        for r in rows:
            self.assertIn(r["status"], tasks_mod.STATUSES)
            self.assertIsNotNone(r["started_at"])
            self.assertTrue(r["note"].startswith("n"))


if __name__ == "__main__":
    unittest.main()
