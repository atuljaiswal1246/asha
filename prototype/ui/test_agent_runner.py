"""Unit tests for agent_runner (the sanctioned opencode-CLI agent launcher).

Offline: no network and no real model. Every run points ``binary=`` at a tiny
fake shell script written into a temp dir. Poll with a deadline; never sleep
long.

    ../../.venv/bin/python -m unittest test_agent_runner -q
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_runner  # noqa: E402
import agents  # noqa: E402


def _wait_for(pred, timeout: float = 5.0, interval: float = 0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


class _FakeScripts(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="agent-runner-test-")
        self._scripts = tempfile.mkdtemp(prefix="agent-runner-scripts-")
        self._old_data = os.environ.get("JARVIS_DATA_DIR")
        os.environ["JARVIS_DATA_DIR"] = self._tmp
        self._old_registry = agents.registry
        agents.registry = agents.AgentRegistry()
        with agent_runner._RUNS_LOCK:
            agent_runner._RUNS.clear()

    def tearDown(self):
        for aid in list(agent_runner.active()):
            agent_runner.stop(aid)
        _wait_for(lambda: not agent_runner.active(), 3.0)
        with agent_runner._RUNS_LOCK:
            agent_runner._RUNS.clear()
        agents.registry = self._old_registry
        if self._old_data is None:
            os.environ.pop("JARVIS_DATA_DIR", None)
        else:
            os.environ["JARVIS_DATA_DIR"] = self._old_data
        shutil.rmtree(self._tmp, ignore_errors=True)
        shutil.rmtree(self._scripts, ignore_errors=True)

    def script(self, name: str, body: str) -> str:
        p = Path(self._scripts) / name
        p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        p.chmod(0o755)
        return str(p)

    def status(self, agent_id: str) -> str:
        return next(r for r in agents.registry.snapshot()
                    if r["id"] == agent_id)["status"]

    def note(self, agent_id: str) -> str:
        return next(r for r in agents.registry.snapshot()
                    if r["id"] == agent_id)["note"]


class BriefTests(unittest.TestCase):
    def test_build_brief_has_all_six_sections(self):
        brief = agent_runner.build_brief(goal="Add a widget")
        for section in ("GOAL", "ALLOWED FILES", "DO NOT", "VERIFY",
                        "OBSERVATIONS", "REPORT BACK"):
            self.assertIn(f"## {section}", brief)
        self.assertIn("Add a widget", brief)
        self.assertEqual(len(agent_runner.BRIEF_SECTIONS), 6)
        # OBSERVATIONS sits after VERIFY and before REPORT BACK.
        self.assertLess(brief.index("## VERIFY"), brief.index("## OBSERVATIONS"))
        self.assertLess(brief.index("## OBSERVATIONS"),
                        brief.index("## REPORT BACK"))

    def test_allowed_files_lists_paths(self):
        brief = agent_runner.build_brief(
            goal="Patch it", files=["prototype/ui/a.py", "notes/b.md"])
        self.assertIn("prototype/ui/a.py", brief)
        self.assertIn("notes/b.md", brief)

    def test_default_report_back_when_none_given(self):
        brief = agent_runner.build_brief(goal="g")
        self.assertIn("verbatim output", brief)
        self.assertIn("could not do", brief)

    def test_observations_section_defaults_to_the_standing_rule(self):
        brief = agent_runner.build_brief(goal="g")
        self.assertIn("## OBSERVATIONS", brief)
        self.assertIn("unrelated to this brief", brief)
        self.assertIn("do NOT change it", brief)
        self.assertIn("write 'none' if nothing", brief)

    def test_verify_list_entries_render_as_shell_lines(self):
        # A list command must be rendered as an executable shell line, not as a
        # Python repr the verifier cannot parse back.
        brief = agent_runner.build_brief(
            goal="g", files=["a.py"],
            verify=[["python3", "-c", "print(1)"], "ruff check a.py"])
        self.assertIn('- python3 -c "print(1)"', brief)
        self.assertIn("- ruff check a.py", brief)
        self.assertNotIn("['python3'", brief)

    def test_standing_do_not_line_is_in_every_brief(self):
        for kwargs in ({}, {"do_not": ["Do not deploy."]}):
            brief = agent_runner.build_brief(goal="g", **kwargs)
            self.assertIn("Do not change anything unrelated to this brief",
                          brief)
            self.assertIn("put it in OBSERVATIONS and leave it alone", brief)
        # A caller that already listed it does not get a duplicate.
        brief = agent_runner.build_brief(
            goal="g", do_not=[agent_runner._STANDING_SCOPE_RULE])
        self.assertEqual(brief.count("Do not change anything unrelated"), 1)


class RunTests(_FakeScripts):
    def test_run_writes_brief_and_a_growing_log(self):
        binp = self.script("ok.sh",
                           'echo "line one"\nsleep 0.2\necho "line two"\n'
                           'sleep 0.2\nexit 0\n')
        brief = agent_runner.build_brief(goal="do a small thing")
        r = agent_runner.run("amit", brief, binary=binp, workdir=self._tmp,
                             timeout=10)
        self.assertTrue(_wait_for(lambda: not r.running(), 5))
        self.assertIn("do a small thing", r.brief_path.read_text(encoding="utf-8"))
        log = r.log_path.read_text(encoding="utf-8")
        self.assertIn("line one", log)
        self.assertIn("line two", log)

    def test_clean_exit_marks_agent_done(self):
        binp = self.script("ok.sh", 'echo "ok"\nsleep 0.1\nexit 0\n')
        r = agent_runner.run("amit", agent_runner.build_brief(goal="g"),
                             binary=binp, workdir=self._tmp, timeout=10)
        self.assertTrue(_wait_for(lambda: r.state != "running", 5))
        self.assertEqual(r.state, "done")
        self.assertEqual(self.status("amit"), "done")

    def test_run_without_workdir_records_the_process_cwd(self):
        # Task 4 root cause: an omitted workdir left the recorded workdir empty,
        # so the verifier fell back to the brain's own repo. The run's real
        # directory is the process cwd -- record it.
        binp = self.script("ok.sh", 'echo "ok"\nsleep 0.05\nexit 0\n')
        old = os.getcwd()
        os.chdir(self._tmp)
        try:
            r = agent_runner.run("amit", agent_runner.build_brief(goal="g"),
                                 binary=binp, timeout=10)
            self.assertTrue(_wait_for(lambda: r.state != "running", 5))
        finally:
            os.chdir(old)
        row = next(x for x in agent_runner.recent_runs()
                   if x["agent_id"] == "amit")
        self.assertEqual(os.path.realpath(row["workdir"]),
                         os.path.realpath(self._tmp))

    def test_exit_one_marks_agent_failed(self):
        binp = self.script("fail.sh", 'echo "boom"\nsleep 0.1\nexit 1\n')
        r = agent_runner.run("anjali", agent_runner.build_brief(goal="g"),
                             binary=binp, workdir=self._tmp, timeout=10)
        self.assertTrue(_wait_for(lambda: r.state != "running", 5))
        self.assertEqual(r.state, "failed")
        self.assertEqual(self.status("anjali"), "failed")

    def test_watcher_sets_a_progress_note(self):
        binp = self.script("slow.sh",
                           'echo "studying the docs"\nsleep 3\nexit 0\n')
        agent_runner.run("rahul", agent_runner.build_brief(goal="g"),
                         binary=binp, workdir=self._tmp, timeout=10)
        self.assertTrue(_wait_for(lambda: self.note("rahul") != "", 4))
        self.assertIn("studying", self.note("rahul"))
        agent_runner.stop("rahul")

    def test_second_run_on_same_file_is_blocked_not_launched(self):
        binp = self.script("slow.sh", 'echo "holding"\nsleep 5\nexit 0\n')
        first = agent_runner.run("amit", agent_runner.build_brief(goal="one"),
                                 binary=binp, workdir=self._tmp,
                                 files=["shared.py"], timeout=10)
        self.assertTrue(first.running())
        second = agent_runner.run("anjali", agent_runner.build_brief(goal="two"),
                                  binary=binp, workdir=self._tmp,
                                  files=["shared.py"], timeout=10)
        self.assertFalse(second.running())
        self.assertEqual(second.state, "failed")
        self.assertIn("shared.py", second.note)
        self.assertEqual(second.pid, 0)
        self.assertNotIn("anjali", agent_runner.active())
        agent_runner.stop("amit")

    def test_missing_binary_is_a_clean_failure(self):
        r = agent_runner.run("priya", agent_runner.build_brief(goal="g"),
                             binary="/nonexistent/opencode-not-here-xyz",
                             workdir=self._tmp)
        self.assertFalse(r.running())
        self.assertEqual(r.state, "failed")
        self.assertIn("not found", r.note.lower())
        self.assertEqual(self.status("priya"), "failed")

    def test_model_defaults_to_roster_and_override_wins(self):
        binp = self.script("ok.sh", 'echo "ok"\nsleep 0.05\nexit 0\n')
        expected = next(r for r in agents.registry.snapshot()
                        if r["id"] == "vikram")["model"]
        r1 = agent_runner.run("vikram", agent_runner.build_brief(goal="g"),
                              binary=binp, workdir=self._tmp, timeout=10)
        self.assertEqual(r1.model, expected)
        _wait_for(lambda: not r1.running(), 5)
        r2 = agent_runner.run("sneha", agent_runner.build_brief(goal="g"),
                              model="opencode/big-pickle", binary=binp,
                              workdir=self._tmp, timeout=10)
        self.assertEqual(r2.model, "opencode/big-pickle")
        _wait_for(lambda: not r2.running(), 5)

    def test_stop_ends_a_sleeping_run(self):
        binp = self.script("stop.sh", 'echo "sleeping"\nsleep 30\nexit 0\n')
        r = agent_runner.run("amit", agent_runner.build_brief(goal="g"),
                             binary=binp, workdir=self._tmp, timeout=60)
        self.assertTrue(_wait_for(lambda: r.running(), 2))
        self.assertTrue(agent_runner.stop("amit"))
        self.assertTrue(_wait_for(lambda: self.status("amit") == "failed", 5))
        self.assertEqual(r.state, "failed")

    def test_timeout_kills_and_marks_failed(self):
        binp = self.script("slow.sh", 'echo "tick"\nsleep 30\nexit 0\n')
        r = agent_runner.run("sneha", agent_runner.build_brief(goal="g"),
                             binary=binp, workdir=self._tmp, timeout=1.0)
        self.assertTrue(_wait_for(lambda: r.state != "running", 6))
        self.assertEqual(r.state, "failed")
        self.assertIn("timed out", self.note("sneha").lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)


class AnsiLogTests(unittest.TestCase):
    """The cue must show text, never escape codes (real bug, 2026-09-17)."""

    def test_clean_line_strips_colour_and_bare_escapes(self):
        import agent_runner as ar
        self.assertEqual(ar._clean_line("\x1b[0m"), "")
        self.assertEqual(ar._clean_line("\x1b[1;32mMECHANISM OK\x1b[0m"), "MECHANISM OK")
        self.assertEqual(ar._clean_line("  plain  "), "plain")
