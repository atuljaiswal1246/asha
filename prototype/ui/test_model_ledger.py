"""Unit tests for model_ledger (the append-only per-model run ledger).

Offline: the module tests write their own JSONL files into a temp dir. The hook
tests drive ``agent_runner`` with a tiny fake shell script, exactly as
``test_agent_runner`` does, and then read the ledger the settled run wrote.

    ../../.venv/bin/python -m unittest test_model_ledger -q
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_runner  # noqa: E402
import agents  # noqa: E402
import model_ledger  # noqa: E402


def _wait_for(pred, timeout: float = 5.0, interval: float = 0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


def _row(agent_id: str, model: str, started: float, finished: float,
         **extra) -> dict:
    row = {
        "schema_version": model_ledger.SCHEMA_VERSION,
        "ts": finished,
        "agent_id": agent_id,
        "model": model,
        "title": "a brief",
        "workdir": "/tmp/work",
        "started": started,
        "finished": finished,
        "duration": round(finished - started, 3),
        "exit_code": 0,
        "note": "done",
        "run_dir": "/tmp/run",
        "tokens": None,
    }
    row.update(extra)
    return row


class RecordLoadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="model-ledger-test-")
        self.path = Path(self._tmp) / "model-ledger.jsonl"

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_record_and_load_roundtrip(self):
        now = time.time()
        written = model_ledger.record(
            agent_id="priya", model="opencode-go/deepseek-v4.1-flash",
            title="Build the ledger", workdir="/w", started=now - 12,
            finished=now, exit_code=0, note="done", run_dir="/r",
            path=self.path)
        self.assertIsNotNone(written)
        self.assertEqual(written["schema_version"], model_ledger.SCHEMA_VERSION)
        self.assertEqual(written["duration"], 12.0)
        self.assertIsNone(written["tokens"])

        rows = list(model_ledger.load(self.path))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["agent_id"], "priya")
        self.assertEqual(rows[0]["model"], "opencode-go/deepseek-v4.1-flash")
        self.assertEqual(rows[0]["title"], "Build the ledger")
        self.assertEqual(rows[0]["exit_code"], 0)
        self.assertEqual(rows[0]["run_dir"], "/r")

    def test_record_appends_never_rewrites(self):
        for i in range(3):
            model_ledger.record(agent_id=f"a{i}", model="m", path=self.path)
        self.assertEqual(len(list(model_ledger.load(self.path))), 3)

    def test_load_skips_blank_and_malformed_lines(self):
        self.path.write_text(
            '{"schema_version": 1, "model": "m"}\n'
            "\n"
            "not json at all\n"
            '{"schema_version": 1, "model": "n"}\n',
            encoding="utf-8")
        rows = list(model_ledger.load(self.path))
        self.assertEqual([r["model"] for r in rows], ["m", "n"])

    def test_load_missing_file_yields_nothing(self):
        missing = Path(self._tmp) / "nope.jsonl"
        self.assertEqual(list(model_ledger.load(missing)), [])

    def test_load_is_a_generator_so_a_big_file_streams(self):
        self.assertIsInstance(model_ledger.load(self.path),
                              types.GeneratorType)

    def test_record_failure_is_fail_open_and_logged(self):
        broken = Path(self._tmp) / "model-ledger.jsonl"
        broken.mkdir()
        with self.assertLogs("model_ledger", level="ERROR"):
            result = model_ledger.record(agent_id="a", model="m", path=broken)
        self.assertIsNone(result)

    def test_summary_over_a_large_ledger(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            for i in range(5000):
                fh.write(json.dumps(_row("a", "big-model", 100.0, 101.0)) + "\n")
        summary = model_ledger.summary(path=self.path)
        self.assertEqual(summary[0]["runs"], 5000)
        self.assertEqual(summary[0]["median_duration"], 1.0)


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="model-ledger-summary-")
        self.journal = Path(self._tmp) / "journal.jsonl"

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _journal(self, *entries):
        with open(self.journal, "w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")

    def test_aggregates_runs_and_joins_verdicts(self):
        now = time.time()
        self._journal(
            {"timestamp": now - 29, "agent_id": "amit", "verdict": "verified"},
            {"timestamp": now + 4, "agent_id": "amit", "verdict": "needs_fix"},
            {"timestamp": now + 5, "agent_id": "rahul", "verdict": "verified"},
        )
        rows = [
            _row("amit", "model-x", now - 20, now),
            _row("amit", "model-x", now - 40, now - 30),
            _row("rahul", "model-x", now - 10, now - 5),
            _row("priya", "model-y", now - 8, now - 4),
        ]
        summary = model_ledger.summary(rows=rows, journal=self.journal)
        by_model = {r["model"]: r for r in summary}

        x = by_model["model-x"]
        self.assertEqual(x["runs"], 3)
        self.assertEqual(x["checked"], 3)
        self.assertEqual(x["verdicts"], {"verified": 2, "needs_fix": 1})
        self.assertAlmostEqual(x["verify_rate"], 2 / 3)
        self.assertIsNotNone(x["first_seen"])
        self.assertIsNotNone(x["last_seen"])

        y = by_model["model-y"]
        self.assertEqual(y["runs"], 1)
        self.assertEqual(y["checked"], 0)
        self.assertIsNone(y["verify_rate"])
        self.assertEqual(y["median_duration"], 4.0)

    def test_verdict_join_needs_the_same_agent_and_window(self):
        now = time.time()
        self._journal(
            {"timestamp": now + 1, "agent_id": "other", "verdict": "verified"},
            {"timestamp": now + model_ledger.DEFAULT_MATCH_WINDOW + 100,
             "agent_id": "amit", "verdict": "verified"},
        )
        rows = [_row("amit", "m", now - 10, now)]
        summary = model_ledger.summary(rows=rows, journal=self.journal)
        self.assertEqual(summary[0]["checked"], 0)

    def test_median_duration_and_sort_by_runs(self):
        rows = [
            _row("a", "few", 0, 2),
            _row("b", "many", 0, 1),
            _row("b", "many", 0, 3),
            _row("b", "many", 0, 5),
        ]
        summary = model_ledger.summary(rows=rows, journal=self.journal)
        self.assertEqual([r["model"] for r in summary], ["many", "few"])
        self.assertEqual(summary[0]["median_duration"], 3.0)

    def test_render_report_table_and_json(self):
        now = time.time()
        self._journal(
            {"timestamp": now + 1, "agent_id": "amit", "verdict": "verified"})
        rows = [_row("amit", "opencode/muse-spark", now - 5, now - 3)]
        summary = model_ledger.summary(rows=rows, journal=self.journal)
        table = model_ledger.render_report(summary)
        self.assertIn("opencode/muse-spark", table)
        self.assertIn("100%", table)
        self.assertIn("model", table.splitlines()[0])
        payload = json.loads(model_ledger.render_report(summary, as_json=True))
        self.assertEqual(payload["schema_version"],
                         model_ledger.SCHEMA_VERSION)
        self.assertEqual(payload["models"][0]["runs"], 1)

    def test_render_report_empty(self):
        self.assertEqual(model_ledger.render_report([]), "no runs recorded")

    def test_cli_report_json(self):
        rows = [_row("amit", "cli-model", 100.0, 101.0)]
        path = Path(self._tmp) / "model-ledger.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = model_ledger.main(["report", "--json", "--data-dir", self._tmp])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["models"][0]["model"], "cli-model")


class AgentRunnerHookTests(unittest.TestCase):
    """The single hook: a settled run appends one ledger row, fail-open."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="model-ledger-hook-")
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

    def _script(self, body: str) -> str:
        p = Path(self._tmp) / "fake-opencode.sh"
        p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        p.chmod(0o755)
        return str(p)

    def test_settled_run_is_recorded(self):
        exe = self._script('echo "working"\nsleep 0.1\nexit 0\n')
        brief = agent_runner.build_brief(goal="Record me in the ledger")
        run = agent_runner.run("amit", brief, model="opencode/test-model",
                               binary=exe, workdir=self._tmp, timeout=10)
        self.assertTrue(_wait_for(lambda: run.state != "running", 5))

        rows = list(model_ledger.load(Path(self._tmp) / "model-ledger.jsonl"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["agent_id"], "amit")
        self.assertEqual(rows[0]["model"], "opencode/test-model")
        self.assertEqual(rows[0]["title"], "Record me in the ledger")
        self.assertEqual(rows[0]["exit_code"], 0)
        self.assertIsNotNone(rows[0]["duration"])
        self.assertEqual(rows[0]["run_dir"],
                         str(Path(run.brief_path).parent))

    def test_broken_ledger_path_cannot_break_a_run(self):
        # A directory where the ledger file should be makes the append fail.
        (Path(self._tmp) / "model-ledger.jsonl").mkdir()
        exe = self._script('echo "still fine"\nsleep 0.1\nexit 0\n')
        logging.disable(logging.CRITICAL)
        try:
            run = agent_runner.run(
                "rahul", agent_runner.build_brief(goal="ledger is broken"),
                binary=exe, workdir=self._tmp, timeout=10)
            self.assertTrue(_wait_for(lambda: run.state != "running", 5))
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(run.state, "done")
        status = next(r for r in agents.registry.snapshot()
                      if r["id"] == "rahul")["status"]
        self.assertEqual(status, "done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
