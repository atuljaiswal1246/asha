"""Unit tests for supervisor_loop (the after-report verification loop).

Offline: no network, and no real subprocess — every check injects a fake
command runner (``FakeRunner``) and a fake reader (``FakeRead``). The one
``on_done`` test uses the same tiny fake-binary technique as
``test_agent_runner.py``.

    ../../.venv/bin/python -m unittest test_supervisor_loop -q
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agents  # noqa: E402
import agent_runner  # noqa: E402
import supervisor_loop as sl  # noqa: E402


def _wait_for(pred, timeout: float = 5.0, interval: float = 0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


def _brief(allowed=("a.py",), verify=("python -m unittest test_foo -q",),
           report="report back briefly") -> str:
    files = "\n".join(f"- {f}" for f in allowed) or \
        "- (none — do not edit any file)"
    ver = "\n".join(f"- {v}" for v in verify) or \
        "- State exactly what you ran and show its verbatim output."
    return (f"# Agent brief\n\n## GOAL\ndo the small thing\n\n"
            f"## ALLOWED FILES\n{files}\n\n## DO NOT\n- commit\n\n"
            f"## VERIFY\n{ver}\n\n## REPORT BACK\n{report}\n")


class FakeRead:
    """Injectable reader: an in-memory map, else the real file, else ''."""

    def __init__(self, files: dict | None = None):
        self.files = files or {}

    def __call__(self, path) -> str:
        if str(path) in self.files:
            return self.files[str(path)]
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError:
            return ""


class FakeRunner:
    """Injectable command runner: records calls, returns canned results."""

    def __init__(self, *, git_out: str = "", git_rc: int = 0,
                 results: dict | None = None):
        self.git_out = git_out
        self.git_rc = git_rc
        self.results = results or {}
        self.calls: list[tuple] = []

    def __call__(self, cmd, cwd=None):
        cmd = list(cmd)
        self.calls.append((cmd, cwd))
        if cmd[:2] == ["git", "status"]:
            return SimpleNamespace(returncode=self.git_rc, stdout=self.git_out)
        return SimpleNamespace(returncode=self.results.get(tuple(cmd), 0),
                               stdout="")


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sl-test-"))
        self._old_data = os.environ.get("JARVIS_DATA_DIR")
        os.environ["JARVIS_DATA_DIR"] = str(self.tmp)
        self.reg = agents.AgentRegistry()

    def tearDown(self):
        if self._old_data is None:
            os.environ.pop("JARVIS_DATA_DIR", None)
        else:
            os.environ["JARVIS_DATA_DIR"] = self._old_data
        shutil.rmtree(self.tmp, ignore_errors=True)

    def journal(self) -> list[dict]:
        p = self.tmp / "agent-verify" / "journal.jsonl"
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]

    def run_info(self, brief_text: str, log_text: str = "done\n") -> dict:
        bp = self.tmp / "brief.md"
        bp.write_text(brief_text, encoding="utf-8")
        lp = self.tmp / "run.log"
        lp.write_text(log_text, encoding="utf-8")
        return {"brief_path": str(bp), "log_path": str(lp),
                "workdir": str(self.tmp)}


class VerifyTests(_TmpCase):
    def test_out_of_scope_change_is_reported_not_needs_fix(self):
        info = self.run_info(_brief(allowed=("a.py",)))
        runner = FakeRunner(git_out=" M a.py\n?? sneaky.py\n")
        res = sl.verify({"id": "amit", "brief_title": "t", "status": "done"},
                        info, run_cmd=runner, read=FakeRead(),
                        workdir=str(self.tmp), data_dir=self.tmp)
        self.assertEqual(res["verdict"], "reported")
        self.assertFalse(res["rework_allowed"])
        self.assertEqual(res["scope_files"], ["sneaky.py"])
        self.assertTrue(any("sneaky.py" in r for r in res["reasons"]),
                        res["reasons"])
        self.assertTrue(any("out-of-scope change (report, do not fix)" in r
                            for r in res["reasons"]), res["reasons"])
        self.assertEqual(res["work_failures"], [])

    def test_allowed_file_change_is_not_a_violation(self):
        info = self.run_info(_brief(allowed=("a.py",)))
        runner = FakeRunner(git_out=" M a.py\n")
        res = sl.verify({"id": "amit", "brief_title": "t", "status": "done"},
                        info, run_cmd=runner, read=FakeRead(),
                        workdir=str(self.tmp), data_dir=self.tmp)
        self.assertEqual(res["verdict"], "verified")
        self.assertEqual(res["allowed_files"], ["a.py"])

    def test_failing_tests_need_fix(self):
        info = self.run_info(_brief())
        cmd = ["python", "-m", "unittest", "test_foo", "-q"]
        runner = FakeRunner(git_out=" M a.py\n", results={tuple(cmd): 1})
        res = sl.verify({"id": "amit", "brief_title": "t", "status": "done"},
                        info, run_cmd=runner, read=FakeRead(),
                        workdir=str(self.tmp), data_dir=self.tmp)
        self.assertEqual(res["verdict"], "needs_fix")
        self.assertTrue(res["rework_allowed"])
        self.assertTrue(res["work_failures"])
        self.assertTrue(any("tests failed" in r for r in res["reasons"]),
                        res["reasons"])

    def test_all_good_is_verified_and_journals(self):
        info = self.run_info(_brief())
        runner = FakeRunner(git_out=" M a.py\n")
        res = sl.verify({"id": "amit", "brief_title": "t", "status": "done"},
                        info, run_cmd=runner, read=FakeRead(),
                        workdir=str(self.tmp), data_dir=self.tmp)
        self.assertEqual(res["verdict"], "verified")
        rows = self.journal()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["agent_id"], "amit")
        self.assertEqual(rows[0]["verdict"], "verified")
        self.assertIn("exit_codes", rows[0])
        self.assertIn("scope_failures", rows[0])
        self.assertIn("work_failures", rows[0])
        self.assertIn("rework_allowed", rows[0])
        self.assertTrue(rows[0]["log_path"])
        self.assertTrue(res["journal_path"].endswith("journal.jsonl"))

    def test_missing_artifact_needs_fix(self):
        info = self.run_info(_brief(report="write report.md here"))
        info["log_path"] = str(self.tmp / "missing.log")
        runner = FakeRunner(git_out=" M a.py\n")
        res = sl.verify({"id": "amit", "brief_title": "t", "status": "done"},
                        info, run_cmd=runner, read=FakeRead(),
                        workdir=str(self.tmp), data_dir=self.tmp)
        self.assertEqual(res["verdict"], "needs_fix")
        self.assertTrue(any("artifact missing" in r for r in res["reasons"]),
                        res["reasons"])

    def test_nothing_checkable_is_inconclusive(self):
        def boom(cmd, cwd=None):
            raise RuntimeError("no git here")
        res = sl.verify({"id": "amit", "brief_title": "t", "status": "failed"},
                        None, run_cmd=boom, read=lambda p: "",
                        workdir=str(self.tmp), data_dir=self.tmp)
        self.assertEqual(res["verdict"], "inconclusive")
        self.assertFalse(res["checked"])

    def test_build_brief_list_command_round_trips_through_the_parser(self):
        # Bug 1: build_brief used to render a list as a Python repr, so the
        # verifier found no command and silently skipped the proof.
        brief = agent_runner.build_brief(
            goal="g", files=["a.py"],
            verify=[["python3", "-c", "print(1)"], "ruff check a.py"])
        self.assertEqual(
            sl._brief_verify_commands(brief),
            [["python3", "-c", "print(1)"], ["ruff", "check", "a.py"]])

    def test_old_repr_brief_is_parsed_back_and_not_dropped(self):
        # Existing briefs (and callers that str() a list) still carry the repr.
        brief = _brief(allowed=("a.py",),
                       verify=("['python3', '-c', 'print(1)']",))
        self.assertEqual(sl._brief_verify_commands(brief),
                         [["python3", "-c", "print(1)"]])

    def test_failing_verify_command_is_needs_fix_and_names_it(self):
        # The regression test: a failing proof must actually execute and land
        # in work_failures, not be skipped as unparseable prose.
        cmd = ["python3", "-c", "import sys; sys.exit(3)"]
        brief = agent_runner.build_brief(
            goal="g", files=["a.py"], verify=[cmd])
        info = self.run_info(brief)
        runner = FakeRunner(git_out=" M a.py\n", results={tuple(cmd): 3})
        res = sl.verify({"id": "amit", "brief_title": "t", "status": "done"},
                        info, run_cmd=runner, read=FakeRead(),
                        workdir=str(self.tmp), data_dir=self.tmp)
        self.assertEqual(res["verdict"], "needs_fix")
        self.assertTrue(res["rework_allowed"])
        self.assertTrue(any("exit 3" in w for w in res["work_failures"]),
                        res["work_failures"])
        self.assertIn(cmd, [c for c, _ in runner.calls])

    def test_list_verify_command_that_passes_is_verified(self):
        cmd = ["python3", "-c", "print(1)"]
        brief = agent_runner.build_brief(
            goal="g", files=["a.py"], verify=[cmd])
        info = self.run_info(brief)
        runner = FakeRunner(git_out=" M a.py\n")
        res = sl.verify({"id": "amit", "brief_title": "t", "status": "done"},
                        info, run_cmd=runner, read=FakeRead(),
                        workdir=str(self.tmp), data_dir=self.tmp)
        self.assertEqual(res["verdict"], "verified")
        self.assertIn(cmd, [c for c, _ in runner.calls])

    def test_unparseable_proof_is_inconclusive_not_verified(self):
        info = self.run_info(_brief(allowed=("a.py",), verify=("[123, 456]",)))
        runner = FakeRunner(git_out=" M a.py\n")
        res = sl.verify({"id": "amit", "brief_title": "t", "status": "done"},
                        info, run_cmd=runner, read=FakeRead(),
                        workdir=str(self.tmp), data_dir=self.tmp)
        self.assertEqual(res["verdict"], "inconclusive")
        self.assertTrue(any("unparseable" in r for r in res["reasons"]),
                        res["reasons"])

    def test_absent_proof_is_inconclusive_not_verified(self):
        # No command in VERIFY and nothing testable touched -> no honest proof
        # can be derived, so the result must not be "verified".
        info = self.run_info(_brief(allowed=("a.md",),
                                    verify=("just show the output",)))
        runner = FakeRunner(git_out=" M a.md\n")
        res = sl.verify({"id": "amit", "brief_title": "t", "status": "done"},
                        info, run_cmd=runner, read=FakeRead(),
                        workdir=str(self.tmp), data_dir=self.tmp)
        self.assertEqual(res["verdict"], "inconclusive")
        self.assertTrue(res["reasons"])


class LoopTests(_TmpCase):
    def _loop(self, *, run_cmd, runner=None, read=None, info=None):
        lp = sl.SupervisorLoop(registry=self.reg, runner=runner,
                               run_cmd=run_cmd, read=read or FakeRead(),
                               data_dir=self.tmp, workdir=str(self.tmp))
        if info is not None:
            lp._run_info = lambda aid: info
        return lp

    def _settle(self, agent_id="amit", title="t"):
        self.reg.start(agent_id, title, files=["a.py"])
        self.reg.finish(agent_id, ok=False, note="done")

    def test_tick_is_safe_on_an_empty_registry(self):
        lp = self._loop(run_cmd=FakeRunner())
        self.assertEqual(asyncio.run(lp.tick()), 0)

    def test_start_is_idempotent(self):
        lp = self._loop(run_cmd=FakeRunner())

        async def go():
            self.assertTrue(lp.start())
            self.assertFalse(lp.start())
            lp.stop()

        asyncio.run(go())

    def test_needs_fix_rebriefs_then_caps_at_two(self):
        info = self.run_info(_brief(allowed=("a.py",)))
        # A failed proof is a *work* failure: that is the kind that reworks.
        cmd = ["python", "-m", "unittest", "test_foo", "-q"]
        runner = FakeRunner(git_out=" M a.py\n", results={tuple(cmd): 1})
        reworks: list[tuple] = []

        def fake_agent_runner(agent_id, brief, **kw):
            reworks.append((agent_id, brief))
            return SimpleNamespace()

        lp = self._loop(run_cmd=runner, runner=fake_agent_runner, info=info)
        for _ in range(3):
            self._settle()
            asyncio.run(lp.tick())
        # two re-briefs (cap), never a third
        self.assertEqual(len(reworks), 2)
        self.assertTrue(all(a == "amit" for a, _ in reworks))
        self.assertIn("failure", reworks[0][1].lower())
        self.assertEqual(self.reg.snapshot()[0]["status"], "needs_fix")

    def test_verified_run_is_not_rebriefed(self):
        info = self.run_info(_brief(allowed=("a.py",)))
        reworks: list = []
        lp = self._loop(run_cmd=FakeRunner(git_out=" M a.py\n"),
                        runner=lambda *a, **k: reworks.append(a), info=info)
        self._settle()
        asyncio.run(lp.tick())
        self.assertEqual(reworks, [])
        self.assertEqual(self.reg.snapshot()[0]["status"], "verified")
        self.assertEqual(self.reg.verified_count(), 1)

    def test_scope_only_reports_and_never_rebriefs(self):
        info = self.run_info(_brief(allowed=("a.py",)))
        reworks: list = []

        def must_not_run(*a, **k):
            reworks.append(a)
            raise AssertionError("reported findings must never be re-briefed")

        lp = self._loop(run_cmd=FakeRunner(git_out="?? sneaky.py\n"),
                        runner=must_not_run, info=info)
        self._settle()
        asyncio.run(lp.tick())
        self.assertEqual(reworks, [])
        self.assertNotEqual(self.reg.snapshot()[0]["status"], "needs_fix")
        # The finding is surfaced for the user, not reworked.
        oos = lp.report()["out_of_scope"]
        self.assertEqual(len(oos), 1)
        self.assertEqual(oos[0]["agent_id"], "amit")
        self.assertEqual(oos[0]["files"], ["sneaky.py"])
        self.assertTrue(oos[0]["when"])
        row = self.journal()[0]
        self.assertEqual(row["verdict"], "reported")
        self.assertFalse(row["rework_allowed"])
        self.assertTrue(row["scope_failures"])
        self.assertEqual(row["work_failures"], [])

    def test_inconclusive_never_rebriefs(self):
        def boom(cmd, cwd=None):
            raise RuntimeError("nothing runnable")

        reworks: list = []
        lp = self._loop(run_cmd=boom, read=lambda p: "",
                        runner=lambda *a, **k: reworks.append(a))
        lp._run_info = lambda aid: None
        self._settle()
        asyncio.run(lp.tick())
        self.assertEqual(reworks, [])
        # inconclusive restores the settled status and stops re-checking
        self.assertEqual(self.reg.awaiting_check(), [])
        self.assertEqual(self.reg.snapshot()[0]["status"], "failed")

    def test_report_has_counts_and_last_verdicts(self):
        info = self.run_info(_brief(allowed=("a.py",)))
        lp = self._loop(run_cmd=FakeRunner(git_out=" M a.py\n"), info=info)
        self._settle()
        asyncio.run(lp.tick())
        rep = lp.report()
        self.assertEqual(rep["counts"]["verified"], 1)
        self.assertEqual(len(rep["last_verdicts"]), 1)
        self.assertEqual(rep["last_verdicts"][0]["verdict"], "verified")


class OnDoneTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="sl-ondone-")
        self._scripts = tempfile.mkdtemp(prefix="sl-scripts-")
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

    def _script(self, name: str, body: str) -> str:
        p = Path(self._scripts) / name
        p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        p.chmod(0o755)
        return str(p)

    def test_on_done_called_once_when_a_run_exits(self):
        binp = self._script("ok.sh", 'echo "did the thing"\nsleep 0.1\nexit 0\n')
        calls: list = []
        r = agent_runner.run("amit", _brief(), binary=binp, workdir=self._tmp,
                             timeout=10, on_done=lambda *a: calls.append(a))
        self.assertTrue(_wait_for(lambda: not r.running(), 5))
        self.assertTrue(_wait_for(lambda: len(calls) == 1, 3))
        self.assertEqual(len(calls), 1)
        agent_id, rc, note, run = calls[0]
        self.assertEqual(agent_id, "amit")
        self.assertEqual(rc, 0)
        self.assertIs(run, r)

    def test_a_raising_on_done_never_breaks_the_watcher(self):
        binp = self._script("ok.sh", 'echo "fine"\nsleep 0.1\nexit 0\n')

        def bad(*_a):
            raise RuntimeError("callback exploded")

        r = agent_runner.run("anjali", _brief(), binary=binp, workdir=self._tmp,
                             timeout=10, on_done=bad)
        self.assertTrue(_wait_for(lambda: r.state != "running", 5))
        self.assertEqual(r.state, "done")
        self.assertEqual(agents.registry.snapshot()[1]["status"], "done")

    def test_recent_runs_exposes_brief_and_log(self):
        binp = self._script("ok.sh", 'echo "hi"\nsleep 0.05\nexit 0\n')
        r = agent_runner.run("rahul", _brief(), binary=binp, workdir=self._tmp,
                             timeout=10)
        runs = agent_runner.recent_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["agent_id"], "rahul")
        self.assertEqual(runs[0]["brief_path"], str(r.brief_path))
        self.assertEqual(runs[0]["log_path"], str(r.log_path))
        self.assertIsNotNone(runs[0]["started_at"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class FenceResolutionTests(unittest.TestCase):
    """Live bug (2026-09-17): a non-repo workdir made git fail 128 and was
    misread as a broken fence, triggering a pointless rework."""

    def test_git_root_walks_up(self):
        import os
        import tempfile
        import supervisor_loop as sl
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "a", "b"))
            self.assertEqual(sl._git_root(os.path.join(tmp, "a", "b")), "")
            os.makedirs(os.path.join(tmp, ".git"))
            self.assertEqual(sl._git_root(os.path.join(tmp, "a", "b")), tmp)

    def test_unreadable_fence_is_inconclusive_not_needs_fix(self):
        import supervisor_loop as sl
        def runner(cmd, cwd=None):
            if cmd[:2] == ["git", "status"]:
                return 128, "fatal: not a git repository"
            return 0, "ok"
        res = sl.verify({"id": "amit", "brief_title": "t"},
                        {"brief_path": "", "log_path": ""},
                        run_cmd=runner, read=lambda p: "x",
                        workdir="/tmp", data_dir="/tmp/jv-x")
        self.assertEqual(res["verdict"], "inconclusive")
        self.assertNotIn("git status failed", " ".join(res["reasons"]))

    def test_no_allowed_files_never_invents_a_violation(self):
        import supervisor_loop as sl
        def runner(cmd, cwd=None):
            if cmd[:2] == ["git", "status"]:
                return 0, " M some/other/file.py\\n?? new.py"
            return 0, "ok"
        res = sl.verify({"id": "amit", "brief_title": "t"},
                        {"brief_path": "", "log_path": "x"},
                        run_cmd=runner, read=lambda p: "ok",
                        workdir="/tmp", data_dir="/tmp/jv-y")
        self.assertNotEqual(res["verdict"], "needs_fix")
        self.assertIn("fence not confirmed", " ".join(res["reasons"]))


class Task4WorkdirTests(unittest.TestCase):
    """Live bug: a run launched from a clean repo with no explicit ``workdir``
    recorded ``workdir=""``, so verify fell back to ``_repo_root()`` and fenced
    the brain's own (dirty) repo -- inventing an out-of-scope change and a false
    ``needs_fix``. No real git, no real agent: a ``.git`` marker plus an
    injected command runner."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="sl-t4-repo-"))
        os.makedirs(self.repo / ".git")  # a git marker, not a real repo
        self.tmp = Path(tempfile.mkdtemp(prefix="sl-t4-data-"))
        self._scripts = tempfile.mkdtemp(prefix="sl-t4-scripts-")
        self._old_data = os.environ.get("JARVIS_DATA_DIR")
        os.environ["JARVIS_DATA_DIR"] = str(self.tmp)
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
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self._scripts, ignore_errors=True)

    def test_clean_repo_run_is_verified_not_fenced_against_the_brain_repo(self):
        binp = os.path.join(self._scripts, "agent.sh")
        Path(binp).write_text(
            "#!/bin/sh\necho 'did a small thing'\nsleep 0.05\nexit 0\n")
        os.chmod(binp, 0o755)

        old = os.getcwd()
        os.chdir(self.repo)
        try:
            # No workdir kwarg: the run's real directory is the process cwd.
            r = agent_runner.run("amit", _brief(allowed=("README.md",)),
                                 binary=binp, timeout=10)
            self.assertTrue(_wait_for(lambda: r.state != "running", 5))
        finally:
            os.chdir(old)

        info = next(x for x in agent_runner.recent_runs()
                    if x["agent_id"] == "amit")
        self.assertEqual(os.path.realpath(info["workdir"]),
                         os.path.realpath(self.repo))

        brain = os.path.realpath(sl._repo_root())

        def runner(cmd, cwd=None):
            if cmd[:2] == ["git", "status"]:
                # The brain's repo is dirty; the agent's real repo is clean.
                dirty = ("?? stray_brain_work.py\n"
                         if os.path.realpath(cwd or "") == brain else "")
                return SimpleNamespace(returncode=0, stdout=dirty)
            return SimpleNamespace(returncode=0, stdout="")

        res = sl.verify({"id": "amit", "brief_title": "t", "status": "done"},
                        info, run_cmd=runner, read=FakeRead(),
                        data_dir=self.tmp)
        self.assertEqual(res["verdict"], "verified")
        self.assertEqual(res["scope_failures"], [])
