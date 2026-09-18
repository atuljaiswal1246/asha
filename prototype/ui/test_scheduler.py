"""Tests for the cron scheduler (G2). Pure stdlib, temp file."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scheduler import Scheduler, cron_matches, cron_next  # noqa: E402

BASE = datetime(2026, 9, 13, 10, 0, 0)  # Sunday


class CronMathTests(unittest.TestCase):
    def test_every_five_minutes(self):
        n = cron_next("*/5 * * * *", BASE)
        self.assertEqual(n, datetime(2026, 9, 13, 10, 5))

    def test_specific_hour_minute(self):
        n = cron_next("30 18 * * *", BASE)
        self.assertEqual(n, datetime(2026, 9, 13, 18, 30))

    def test_weekday_only(self):
        # 2026-09-13 is Sunday; next Monday-only 9:00 is 2026-09-14.
        n = cron_next("0 9 * * 1", BASE)
        self.assertEqual(n, datetime(2026, 9, 14, 9, 0))

    def test_lists_and_ranges(self):
        self.assertTrue(cron_matches("0 9-17 * * *",
                                     datetime(2026, 9, 13, 12, 0)))
        self.assertTrue(cron_matches("0 0 1,15 * *",
                                     datetime(2026, 9, 15, 0, 0)))

    def test_bad_expression_matches_nothing(self):
        self.assertFalse(cron_matches("not a cron", BASE))
        self.assertIsNone(cron_next("nope", BASE, max_days=1))


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asha-cron-")
        self.path = Path(self.tmp) / "cron.json"
        self.s = Scheduler(self.path)

    def test_add_requires_shape(self):
        with self.assertRaises(ValueError):
            self.s.add({"name": "x"})          # no request/schedule
        with self.assertRaises(ValueError):
            self.s.add({"name": "x", "request": "r", "cron": "bad"})

    def test_interval_due_and_persist(self):
        self.s.add({"name": "job", "request": "do it", "every": 60,
                    "last_run": BASE.timestamp()})
        self.assertEqual(self.s.due(BASE), [])            # nothing elapsed yet
        later = BASE + timedelta(seconds=61)
        due = self.s.due(later)
        self.assertEqual([j["name"] for j in due], ["job"])
        # persisted last_run → not due again immediately
        again = Scheduler(self.path)
        self.assertEqual(again.due(BASE + timedelta(seconds=90)), [])

    def test_disabled_job_never_due(self):
        self.s.add({"name": "off", "request": "r", "every": 1, "enabled": False})
        self.assertEqual(self.s.due(BASE + timedelta(days=1)), [])

    def test_remove(self):
        self.s.add({"name": "a", "request": "r", "every": 10})
        self.s.remove("a")
        self.assertEqual(self.s.jobs(), [])

    def test_run_due_calls_dispatch_and_records(self):
        self.s.add({"name": "a", "request": "r", "every": 60,
                    "last_run": BASE.timestamp()})
        ran = self.s.run_due(lambda j: None, now=BASE + timedelta(seconds=61))
        self.assertEqual(len(ran), 1)
        self.assertTrue(ran[0]["ok"])

    def test_run_due_swallows_dispatch_error(self):
        self.s.add({"name": "a", "request": "r", "every": 60,
                    "last_run": BASE.timestamp()})
        def boom(job):
            raise RuntimeError("nope")
        ran = self.s.run_due(boom, now=BASE + timedelta(seconds=61))
        self.assertFalse(ran[0]["ok"])
        self.assertIn("nope", ran[0]["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
