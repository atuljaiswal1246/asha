"""Unit tests for the permanent agent registry (agents.py) and its frame.

Pure in-memory, no network, no server. Run with:

    ../../.venv/bin/python -m unittest test_agents -q
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agents  # noqa: E402
from proto import AgentsFrame, UIFrameSerializer  # noqa: E402


class RosterTests(unittest.TestCase):
    def test_seven_agents_with_required_fields(self):
        self.assertEqual(len(agents.AGENTS), 7)
        for a in agents.AGENTS:
            self.assertTrue(a.id, f"{a.id} missing id")
            self.assertTrue(a.name, f"{a.id} missing name")
            self.assertTrue(a.title, f"{a.id} missing title")
            self.assertTrue(a.responsibilities, f"{a.id} missing responsibilities")
            self.assertTrue(a.color.startswith("#"))
            self.assertTrue(a.model)

    def test_ids_are_unique_and_lowercase(self):
        ids = [a.id for a in agents.AGENTS]
        self.assertEqual(len(ids), len(set(ids)))
        for i in ids:
            self.assertEqual(i, i.lower())

    def test_expected_roster(self):
        self.assertEqual(
            [a.id for a in agents.AGENTS],
            ["amit", "anjali", "rahul", "priya", "vikram", "sneha", "karan"],
        )

    def test_module_singleton(self):
        self.assertEqual(len(agents.registry.snapshot()), 7)

    def test_two_backend_developers(self):
        backend = [a for a in agents.AGENTS if a.title == "Backend Developer"]
        self.assertEqual(len(backend), 2)

    def test_every_agent_has_nonempty_name_title_responsibilities(self):
        for a in agents.AGENTS:
            self.assertIsInstance(a.name, str)
            self.assertTrue(a.name.strip(), f"{a.id} has empty name")
            self.assertIsInstance(a.title, str)
            self.assertTrue(a.title.strip(), f"{a.id} has empty title")
            self.assertIsInstance(a.responsibilities, list)
            self.assertTrue(len(a.responsibilities) > 0,
                            f"{a.id} has no responsibilities")


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.reg = agents.AgentRegistry()

    def _row(self, agent_id):
        return next(r for r in self.reg.snapshot() if r["id"] == agent_id)

    def test_start_finish_moves_status_and_sets_times(self):
        self.assertTrue(self.reg.start("amit", "read the docs"))
        row = self._row("amit")
        self.assertEqual(row["status"], "working")
        self.assertEqual(row["brief_title"], "read the docs")
        self.assertIsNotNone(row["started_at"])
        self.assertIsNone(row["ended_at"])
        self.assertEqual(self.reg.running_count(), 1)

        time.sleep(0.06)
        self.assertTrue(self.reg.finish("amit", ok=True))
        row = self._row("amit")
        self.assertEqual(row["status"], "done")
        self.assertIsNotNone(row["ended_at"])
        self.assertGreater(row["seconds"], 0.0)
        self.assertEqual(self.reg.running_count(), 0)

    def test_finish_failed_and_unknown(self):
        self.reg.start("anjali", "edit a file")
        self.reg.finish("anjali", ok=False, note="boom")
        row = self._row("anjali")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["note"], "boom")
        self.assertFalse(self.reg.start("nobody", "x"))
        self.assertFalse(self.reg.finish("nobody"))

    def test_note_sets_progress_line(self):
        self.reg.start("rahul", "write notes")
        self.assertTrue(self.reg.note("rahul", "drafting now"))
        self.assertEqual(self._row("rahul")["note"], "drafting now")

    def test_same_file_blocks_second_then_promotes(self):
        self.reg.start("amit", "read", files=["notes/ROADMAP.md"])
        self.reg.start("anjali", "edit", files=["notes/ROADMAP.md"])
        blocked = self._row("anjali")
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("notes/ROADMAP.md", blocked["note"])
        self.assertEqual(self.reg.running_count(), 1)

        self.reg.finish("amit")
        promoted = self._row("anjali")
        self.assertEqual(promoted["status"], "working")
        self.assertEqual(self.reg.running_count(), 1)

    def test_distinct_files_do_not_block(self):
        self.reg.start("amit", "read", files=["a.py"])
        self.reg.start("anjali", "edit", files=["b.py"])
        self.assertEqual(self._row("amit")["status"], "working")
        self.assertEqual(self._row("anjali")["status"], "working")
        self.assertEqual(self.reg.running_count(), 2)

    def test_snapshot_shape_and_summary(self):
        snap = self.reg.snapshot()
        self.assertEqual(len(snap), 7)
        expected = {
            "id", "name", "title", "responsibilities", "model", "color",
            "status", "brief_title", "note", "started_at", "ended_at",
            "seconds", "files",
        }
        self.assertEqual(set(snap[0].keys()), expected)
        self.assertTrue(all(r["status"] == "idle" for r in snap))
        self.assertEqual(self.reg.summary(), {"running": 0, "total": 7})
        self.reg.start("sneha", "batch")
        self.assertEqual(self.reg.summary(), {"running": 1, "total": 7})

    def test_team_counts_by_title(self):
        counts = self.reg.team_counts()
        self.assertEqual(counts["Backend Developer"], 2)
        self.assertEqual(counts["Frontend Developer"], 1)
        self.assertEqual(counts["Tester"], 1)
        self.assertEqual(counts["Project Manager"], 1)
        self.assertEqual(counts["SEO Manager"], 1)
        self.assertEqual(counts["Copy Writer"], 1)

    def test_team_counts_ignores_retired(self):
        self.reg.retire("karan")
        counts = self.reg.team_counts()
        self.assertNotIn("Copy Writer", counts)
        self.assertEqual(sum(counts.values()), 6)

    def test_active_count(self):
        self.assertEqual(self.reg.active_count(), 7)
        self.reg.retire("karan")
        self.assertEqual(self.reg.active_count(), 6)

    def test_pick_prefers_matching_title(self):
        agent_id = self.reg.pick("Backend Developer")
        row = self._row(agent_id)
        self.assertEqual(row["title"], "Backend Developer")

    def test_pick_when_busy_returns_any_free(self):
        self.reg.start("amit", "task1")
        self.reg.start("anjali", "task2")
        # Both backend developers are busy, but pick still returns an agent
        agent_id = self.reg.pick("Backend Developer")
        self.assertIn(agent_id, [a["id"] for a in self.reg.snapshot()])
        row = self._row(agent_id)
        self.assertNotIn(row["status"], ("working", "blocked"))

    def test_pick_no_title_returns_first_free(self):
        agent_id = self.reg.pick("")
        self.assertIn(agent_id, [a["id"] for a in self.reg.snapshot()])

    def test_no_title_gates_capability(self):
        """Regression: every agent has the same capability regardless of title.

        The runner receives the same default model and there is no per-title
        restriction. This test asserts that invariant explicitly."""
        for a in self.reg.snapshot():
            self.assertEqual(a["model"], agents.DEFAULT_MODEL)
            # No agent has a "capabilities" or "tools" field that gates work
            self.assertNotIn("capabilities", a)
            self.assertNotIn("tools", a)
            self.assertNotIn("permissions", a)


class HireRetireTests(unittest.TestCase):
    def setUp(self):
        self.reg = agents.AgentRegistry()

    def test_hire_rejects_missing_name(self):
        with self.assertRaises(ValueError) as ctx:
            self.reg.hire("", "Backend Developer", ["APIs"])
        self.assertIn("name", str(ctx.exception))

    def test_hire_rejects_missing_title(self):
        with self.assertRaises(ValueError) as ctx:
            self.reg.hire("Zara", "", ["APIs"])
        self.assertIn("title", str(ctx.exception))

    def test_hire_rejects_empty_responsibilities(self):
        with self.assertRaises(ValueError) as ctx:
            self.reg.hire("Zara", "Backend Developer", [])
        self.assertIn("responsibility", str(ctx.exception))

    def test_hire_returns_proposal_not_agent(self):
        proposal = self.reg.hire("Zara", "Backend Developer", ["APIs"])
        self.assertIn("id", proposal)
        self.assertIn("name", proposal)
        self.assertIn("title", proposal)
        # The agent should NOT appear in the roster yet
        self.assertNotIn("zara", [a["id"] for a in self.reg.snapshot()])

    def test_confirm_creates_one_agent(self):
        proposal = self.reg.hire("Zara", "Backend Developer", ["APIs"])
        self.reg.confirm(proposal)
        snap = self.reg.snapshot()
        self.assertEqual(len(snap), 8)
        zara = next(a for a in snap if a["id"] == "zara")
        self.assertEqual(zara["name"], "Zara")
        self.assertEqual(zara["title"], "Backend Developer")

    def test_retire_drops_from_counts_and_snapshot(self):
        proposal = self.reg.hire("Zara", "Backend Developer", ["APIs"])
        self.reg.confirm(proposal)
        self.assertTrue(self.reg.retire("zara"))
        snap = self.reg.snapshot()
        self.assertNotIn("zara", [a["id"] for a in snap])
        counts = self.reg.team_counts()
        self.assertEqual(counts["Backend Developer"], 2)  # amit + anjali only

    def test_retire_unknown_returns_false(self):
        self.assertFalse(self.reg.retire("nonexistent"))

    def test_proposal_stored_one_at_a_time(self):
        proposal1 = self.reg.hire("Zara", "Backend Developer", ["APIs"])
        self.reg.pending_proposal = proposal1
        self.assertEqual(self.reg.pending_proposal["name"], "Zara")
        proposal2 = self.reg.hire("Leo", "Frontend Developer", ["UI"])
        self.reg.pending_proposal = proposal2
        self.assertEqual(self.reg.pending_proposal["name"], "Leo")
        # First proposal is gone
        self.reg.pending_proposal = None
        self.assertIsNone(self.reg.pending_proposal)


class PickTests(unittest.TestCase):
    def setUp(self):
        self.reg = agents.AgentRegistry()

    def test_pick_returns_free_agent(self):
        agent_id = self.reg.pick()
        self.assertIn(agent_id, [a["id"] for a in self.reg.snapshot()])

    def test_pick_avoids_busy_agents(self):
        self.reg.start("amit", "task1")
        picked = self.reg.pick()
        self.assertNotEqual(picked, "amit")

    def test_pick_case_insensitive_title(self):
        self.reg.start("amit", "task1")
        picked = self.reg.pick("backend developer")
        row = next(r for r in self.reg.snapshot() if r["id"] == picked)
        self.assertEqual(row["title"], "Backend Developer")

    def test_pick_all_busy_returns_first(self):
        for a in self.reg.snapshot():
            self.reg.start(a["id"], "work")
        picked = self.reg.pick("Tester")
        self.assertIsNotNone(picked)


class FrameTests(unittest.TestCase):
    def test_agents_frame_fields(self):
        frame = AgentsFrame(agents=[{"id": "amit"}], running=1, total=7)
        self.assertEqual(frame.agents, [{"id": "amit"}])
        self.assertEqual(frame.running, 1)
        self.assertEqual(frame.total, 7)

    def test_serializer_emits_agents_type(self):
        serializer = UIFrameSerializer()
        payload = asyncio.run(serializer.serialize(
            AgentsFrame(agents=[{"id": "amit", "status": "working"}],
                        running=1, total=7)
        ))
        msg = json.loads(payload)
        self.assertEqual(msg["type"], "agents")
        self.assertEqual(msg["running"], 1)
        self.assertEqual(msg["total"], 7)
        self.assertEqual(msg["agents"][0]["id"], "amit")


class CodingResultTests(unittest.TestCase):
    """The coding-result bubble is a Jarvis work path — one voice, no labels."""

    def test_serializes_text_without_persona_field(self):
        from proto import CodingResultFrame
        frame = CodingResultFrame(text="done")
        payload = asyncio.run(UIFrameSerializer().serialize(frame))
        msg = json.loads(payload)
        self.assertEqual(msg["type"], "coding_result")
        self.assertEqual(msg["text"], "done")
        self.assertNotIn("persona", msg)
        self.assertNotIn("Ashish", payload)


class DoctrineLineTests(unittest.TestCase):
    def test_doctrine_line_in_agent_loop_system(self):
        import agent_loop
        self.assertIn(
            "Agents are your team",
            agent_loop.SYSTEM,
            "doctrine line missing from agent_loop.SYSTEM",
        )
        self.assertIn(
            "Any of them can do any task",
            agent_loop.SYSTEM,
            "capability clause missing from agent_loop.SYSTEM",
        )

    def test_doctrine_line_in_server_brain_contract(self):
        import server
        self.assertIn(
            "Agents are your team",
            server._BRAIN_CONTRACT,
            "doctrine line missing from server._BRAIN_CONTRACT",
        )
        self.assertIn(
            "Any of them can do any task",
            server._BRAIN_CONTRACT,
            "capability clause missing from server._BRAIN_CONTRACT",
        )


if __name__ == "__main__":
    unittest.main()
