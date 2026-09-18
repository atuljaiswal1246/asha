"""Unit tests for the Projects board store (board.py).

Pure stdlib, tmp dirs only, no network, no server. Run with:

    ../../.venv/bin/python -m unittest test_board -q
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import board as board_mod  # noqa: E402


def _fresh() -> board_mod.Board:
    d = tempfile.mkdtemp(prefix="board-test-")
    return board_mod.Board(os.path.join(d, "board.json"))


class SeedTests(unittest.TestCase):
    def test_seed_creates_two_projects_and_34_cards(self):
        b = _fresh()
        names = [p["name"] for p in b.projects()]
        self.assertEqual(names, ["Jarvis", "Cal Matters"])
        self.assertEqual(len(b.cards()), 34)
        jarvis = next(p for p in b.projects() if p["name"] == "Jarvis")
        self.assertTrue(jarvis["repo"])
        cal = next(p for p in b.projects() if p["name"] == "Cal Matters")
        self.assertEqual(cal["repo"], "")

    def test_seed_status_mapping(self):
        b = _fresh()
        by_note = {c["notes"].split(":")[0]: c for c in b.cards()}
        self.assertEqual(by_note["T01"]["status"], board_mod.DONE)
        self.assertEqual(by_note["T05"]["status"], board_mod.WAITING)
        self.assertEqual(by_note["T17"]["status"], board_mod.IN_PROGRESS)
        self.assertEqual(by_note["T21"]["status"], board_mod.WAITING)
        self.assertEqual(by_note["T31"]["status"], board_mod.BLOCKED)

    def test_seed_only_when_file_absent(self):
        b = _fresh()
        b.add_card("p1", "extra", writer=True)
        # Reloading the same path must not re-seed (the card stays, no dupes).
        b2 = board_mod.Board(b._path)
        self.assertEqual(len(b2.cards()), 35)

    def test_schema_version_on_every_record(self):
        b = _fresh()
        for p in b.projects():
            self.assertEqual(p["schema_version"], board_mod.SCHEMA_VERSION)
        for c in b.cards():
            self.assertEqual(c["schema_version"], board_mod.SCHEMA_VERSION)


class ProjectCrudTests(unittest.TestCase):
    def setUp(self):
        self.b = _fresh()

    def test_add_update_remove(self):
        p = self.b.add_project("Demo", note="hello", repo="/tmp/demo", writer=True)
        self.assertTrue(p["id"])
        self.assertEqual(p["total"], 0)
        up = self.b.update_project(p["id"], name="Demo 2", writer=True)
        self.assertEqual(up["name"], "Demo 2")
        self.assertTrue(self.b.remove_project(p["id"], writer=True))
        self.assertIsNone(self.b.get_project(p["id"]))

    def test_remove_project_deletes_cards(self):
        p = self.b.add_project("Demo", writer=True)
        c = self.b.add_card(p["id"], "task", writer=True)
        self.b.remove_project(p["id"], delete_cards=True, writer=True)
        self.assertIsNone(self.b.get_card(c["id"]))

    def test_resolve_by_name_and_id(self):
        self.assertEqual(self.b.resolve_project("Jarvis"), "p1")
        self.assertEqual(self.b.resolve_project("p1"), "p1")
        self.assertIsNone(self.b.resolve_project("nope"))


class CardCrudTests(unittest.TestCase):
    def setUp(self):
        self.b = _fresh()

    def test_add_update_move_remove(self):
        c = self.b.add_card("p1", "Write docs", owner="You", priority="P1",
                            status="Backlog", writer=True)
        self.assertEqual(c["owner"], "You")
        up = self.b.update_card(c["id"], title="Write MORE docs", writer=True)
        self.assertEqual(up["title"], "Write MORE docs")
        mv = self.b.move_card(c["id"], "Done", writer=True)
        self.assertEqual(mv["status"], "Done")
        self.assertTrue(self.b.remove_card(c["id"], writer=True))
        self.assertIsNone(self.b.get_card(c["id"]))

    def test_unknown_ids_are_none(self):
        self.assertIsNone(self.b.update_card("nope", title="x", writer=True))
        self.assertIsNone(self.b.move_card("nope", "Done", writer=True))
        self.assertFalse(self.b.remove_card("nope", writer=True))
        self.assertIsNone(self.b.get_card("nope"))

    def test_bad_status_move_is_none(self):
        self.assertIsNone(self.b.move_card("c1", "Nonsense", writer=True))

    def test_add_card_unknown_project_raises(self):
        with self.assertRaises(KeyError):
            self.b.add_card("nope", "x", writer=True)

    def test_note_is_capped_and_single_line(self):
        c = self.b.add_card("p1", "t", notes="line one\n" + ("x" * 400),
                            writer=True)
        self.assertLessEqual(len(c["notes"]), board_mod.NOTE_MAX)
        self.assertNotIn("\n", c["notes"])

    def test_bad_priority_defaults(self):
        c = self.b.add_card("p1", "t", priority="P9", writer=True)
        self.assertEqual(c["priority"], board_mod._DEFAULT_PRIORITY)


class OrderingTests(unittest.TestCase):
    def setUp(self):
        self.b = _fresh()
        self.p = self.b.add_project("Ordering", writer=True)["id"]

    def test_columns_are_the_five_in_order(self):
        cols = self.b.columns(self.p)
        self.assertEqual([c["name"] for c in cols], list(board_mod.STATUSES))

    def test_cards_sorted_by_status_then_priority_then_age(self):
        self.b.add_card(self.p, "p3", priority="P3", status="Done", writer=True)
        self.b.add_card(self.p, "p1", priority="P1", status="Backlog", writer=True)
        self.b.add_card(self.p, "p0", priority="P0", status="Backlog", writer=True)
        rows = self.b.cards(self.p)
        self.assertEqual([c["title"] for c in rows], ["p0", "p1", "p3"])

    def test_column_counts_match(self):
        self.b.add_card(self.p, "a", status="Backlog", writer=True)
        self.b.add_card(self.p, "b", status="Backlog", writer=True)
        self.b.add_card(self.p, "c", status="Done", writer=True)
        counts = {c["name"]: c["count"] for c in self.b.columns(self.p)}
        self.assertEqual(counts["Backlog"], 2)
        self.assertEqual(counts["Done"], 1)
        self.assertEqual(counts["In progress"], 0)


class PersistenceTests(unittest.TestCase):
    def test_round_trip_through_the_file(self):
        b = _fresh()
        c = b.add_card("p1", "persisted", owner="Jarvis", writer=True)
        b2 = board_mod.Board(b._path)
        again = b2.get_card(c["id"])
        self.assertIsNotNone(again)
        self.assertEqual(again["title"], "persisted")
        self.assertEqual(again["owner"], "Jarvis")

    def test_corrupt_file_is_kept_as_bad_and_board_starts_empty(self):
        d = tempfile.mkdtemp(prefix="board-test-")
        path = os.path.join(d, "board.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        b = board_mod.Board(path)
        result = b.load()
        self.assertTrue(result["recovered"])
        self.assertTrue(result["notice"])
        self.assertTrue(os.path.exists(os.path.join(d, "board.json.bad")))
        # It reseeds rather than crashing, and the notice is exposed to the UI.
        self.assertEqual(len(b.projects()), 2)
        self.assertTrue(b.recovery_notice)

    def test_atomic_save_leaves_no_tmp_file(self):
        b = _fresh()
        b.add_card("p1", "x", writer=True)
        leftovers = [f for f in os.listdir(os.path.dirname(b._path))
                     if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        with open(b._path, encoding="utf-8") as fh:
            json.load(fh)  # valid JSON on disk


class LiveWorkTests(unittest.TestCase):
    def setUp(self):
        self.b = _fresh()

    def test_upsert_live_then_clear(self):
        repo = self.b.projects()[0]["repo"]
        card = self.b.upsert_live("amit", "reading the docs", note="on p3",
                                  repo=repo, writer=True)
        self.assertTrue(card["live"])
        self.assertEqual(card["owner"], "Amit")
        self.assertEqual(card["status"], board_mod.IN_PROGRESS)
        self.assertTrue(any(c["id"] == card["id"] for c in self.b.cards()))
        # A second upsert for the same agent updates in place.
        card2 = self.b.upsert_live("amit", "reading the docs", note="now p7",
                                   repo=repo, writer=True)
        self.assertEqual(card2["id"], card["id"])
        live = [c for c in self.b.cards() if c["live"]]
        self.assertEqual(len(live), 1)
        self.assertTrue(self.b.clear_live("amit", writer=True))
        self.assertEqual([c for c in self.b.cards() if c["live"]], [])
        self.assertFalse(self.b.clear_live("amit", writer=True))

    def test_live_card_goes_to_the_repo_project(self):
        cal = next(p for p in self.b.projects() if p["name"] == "Cal Matters")
        # Give Cal Matters a repo, then live work in it must land there.
        self.b.update_project(cal["id"], repo="/tmp/cal-matters", writer=True)
        card = self.b.upsert_live("priya", "build it", repo="/tmp/cal-matters",
                                  writer=True)
        self.assertEqual(card["project_id"], cal["id"])

    def test_live_cards_are_not_persisted(self):
        b = _fresh()
        b.upsert_live("amit", "x", writer=True)
        b2 = board_mod.Board(b._path)
        self.assertEqual([c for c in b2.cards() if c["live"]], [])


class ReadOnlyRuleTests(unittest.TestCase):
    """Hard rule: only the brain writes. Every mutator rejects a read-only caller."""

    def setUp(self):
        self.b = _fresh()

    def test_every_mutating_method_rejects_a_read_only_caller(self):
        calls = [
            lambda: self.b.add_project("X"),
            lambda: self.b.update_project("p1", name="X"),
            lambda: self.b.remove_project("p1"),
            lambda: self.b.add_card("p1", "X"),
            lambda: self.b.update_card("c1", title="X"),
            lambda: self.b.move_card("c1", "Done"),
            lambda: self.b.remove_card("c1"),
            lambda: self.b.upsert_live("amit", "X"),
            lambda: self.b.clear_live("amit"),
            lambda: self.b.save(),
        ]
        for call in calls:
            with self.assertRaises(PermissionError):
                call()

    def test_read_methods_are_open(self):
        self.assertTrue(self.b.projects())
        self.assertTrue(self.b.cards())
        self.assertTrue(self.b.columns("p1"))
        self.assertIsNotNone(self.b.get_card("c1"))
        self.assertIsNotNone(self.b.get_project("p1"))
        self.assertTrue(self.b.summary_line())

    def test_brain_can_still_write(self):
        self.assertTrue(self.b.add_project("Y", writer=True))


class SummaryTests(unittest.TestCase):
    def test_summary_shape(self):
        b = _fresh()
        line = b.summary_line()
        self.assertTrue(line)
        self.assertIn("2 projects:", line)
        self.assertIn("Jarvis -", line)
        self.assertIn("Cal Matters", line)
        # lowercased column names, no title-case status words leaking in
        self.assertIn("in progress", line)
        self.assertIn("waiting on you", line)

    def test_never_empty(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "board.json")
        b = board_mod.Board(path)
        b.remove_project("p1", writer=True)
        b.remove_project("p2", writer=True)
        self.assertTrue(b.summary_line())


EXPECTED_TABLE = (
    "| id | project | task      | status      | note           | owner  | priority |\n"
    "|----|---------|-----------|-------------|----------------|--------|----------|\n"
    "| c1 | Alpha   | Ship docs | Backlog     |                | You    | P1       |\n"
    "| c2 | Beta    | Fix login | In progress | waiting on API | Jarvis | P0       |")


class RenderTableTests(unittest.TestCase):
    FIXTURE = [
        {"id": "c2", "project": "Beta", "task": "Fix login",
         "status": "In progress", "note": "waiting on API",
         "owner": "Jarvis", "priority": "P0"},
        {"id": "c1", "project": "Alpha", "task": "Ship docs",
         "status": "Backlog", "note": "", "owner": "You", "priority": "P1"},
    ]

    def test_exact_rendered_string_for_the_fixture(self):
        self.assertEqual(board_mod.render_table(self.FIXTURE), EXPECTED_TABLE)

    def test_render_is_stable_and_input_order_independent(self):
        again = board_mod.render_table(list(reversed(self.FIXTURE)))
        self.assertEqual(again, EXPECTED_TABLE)

    def test_header_is_the_canonical_columns(self):
        header = board_mod.render_table([]).splitlines()[0]
        self.assertEqual(
            [p.strip() for p in header.strip("|").split("|")],
            list(board_mod.TABLE_COLUMNS))

    def test_board_render_matches_module_renderer(self):
        b = _fresh()
        self.assertEqual(b.render_table("p1"),
                         board_mod.render_table(b.table_rows("p1")))


class ConcurrencyTests(unittest.TestCase):
    def test_parallel_mutations_do_not_corrupt_the_board(self):
        b = _fresh()
        start = len(b.cards())
        created = []
        lock = threading.Lock()

        def worker(n: int):
            local = []
            for i in range(10):
                c = b.add_card("p1", f"w{n}-{i}", writer=True)
                local.append(c["id"])
            with lock:
                created.extend(local)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rows = b.cards()
        self.assertEqual(len(rows), start + 40)
        self.assertEqual(len({c["id"] for c in rows}), start + 40)
        self.assertEqual(set(created), {c["id"] for c in rows} & set(created))


if __name__ == "__main__":
    unittest.main()
