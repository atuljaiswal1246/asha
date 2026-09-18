"""Tests for the permission rule store (A3). Pure stdlib, temp-file backed."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from permissions import ALLOW, ASK, DENY, PermissionStore  # noqa: E402


class PermissionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asha-perm-")
        self.path = Path(self.tmp) / "permissions.json"

    def test_default_when_no_rule(self):
        s = PermissionStore(self.path)
        self.assertEqual(s.decide("bash: anything"), ASK)
        self.assertEqual(s.decide("bash: anything", default=ALLOW), ALLOW)

    def test_first_match_wins_and_deny_short_circuits(self):
        s = PermissionStore(self.path)
        s.add_rule("bash: git *", DENY)
        s.add_rule("bash: *", ALLOW)
        self.assertEqual(s.decide("bash: git status"), DENY)
        self.assertEqual(s.decide("bash: ls -la"), ALLOW)

    def test_case_insensitive_wildcards(self):
        s = PermissionStore(self.path)
        s.add_rule("write: prototype/*", ALLOW)
        self.assertEqual(s.decide("write: prototype/ui/server.py"), ALLOW)

    def test_persistence_round_trip(self):
        s = PermissionStore(self.path)
        s.add_rule("bash: pytest *", ALLOW)
        again = PermissionStore(self.path)
        self.assertEqual(again.decide("bash: pytest -q"), ALLOW)

    def test_add_rule_replaces_same_pattern(self):
        s = PermissionStore(self.path)
        s.add_rule("bash: rm *", ALLOW)
        s.add_rule("bash: rm *", DENY)
        self.assertEqual(s.decide("bash: rm -rf x"), DENY)
        self.assertEqual(len(s.rules()), 1)

    def test_bad_rules_are_dropped(self):
        s = PermissionStore(self.path)
        s.set_rules([{"pattern": "x", "action": "explode"}, {"action": "allow"}, 7])
        self.assertEqual(s.rules(), [])

    def test_remove_rule(self):
        s = PermissionStore(self.path)
        s.add_rule("bash: *", ALLOW)
        s.remove_rule("bash: *")
        self.assertEqual(s.decide("bash: ls"), ASK)


if __name__ == "__main__":
    unittest.main(verbosity=2)
