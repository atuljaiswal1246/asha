"""Runtime tests for the Projects screen backend (board_screen.handle).

Each action must return the full payload after the change; unknown ids must be
clean errors; a mutation must show up in the very next read. Pure stdlib, tmp
dirs only, no network, no server. Run with:

    ../../.venv/bin/python -m unittest test_board_screen -q
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import board as board_mod  # noqa: E402
import board_screen  # noqa: E402


class ScreenTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="board-screen-")
        self.store = board_mod.Board(os.path.join(d, "board.json"))
        self.out: list[dict] = []

    async def _send(self, payload: dict):
        self.out.append(payload)

    def run_msg(self, msg: dict) -> dict:
        self.out = []
        asyncio.run(board_screen.handle(msg, self._send, self.store))
        self.assertEqual(len(self.out), 1, self.out)
        return self.out[0]

    def assert_payload(self, p: dict) -> None:
        self.assertEqual(p["type"], "board_state", p)
        for key in ("projects", "project", "columns", "cards", "render",
                    "statuses", "priorities"):
            self.assertIn(key, p)
        self.assertEqual(len(p["columns"]), 5)

    # -- read actions ----------------------------------------------------
    def test_projects_list_returns_full_payload(self):
        p = self.run_msg({"action": "projects_list"})
        self.assert_payload(p)
        self.assertEqual([x["name"] for x in p["projects"]],
                         ["Jarvis", "Cal Matters"])
        self.assertEqual(p["project"]["name"], "Jarvis")

    def test_board_get_selects_by_name(self):
        p = self.run_msg({"action": "board_get", "project": "Cal Matters"})
        self.assert_payload(p)
        self.assertEqual(p["project"]["name"], "Cal Matters")
        self.assertEqual(sum(c["count"] for c in p["columns"]), 0)

    # -- project mutations ----------------------------------------------
    def test_project_add_update_remove(self):
        added = self.run_msg({"action": "project_add", "name": "New",
                              "repo": "/tmp/new"})
        self.assert_payload(added)
        pid = added["project"]["id"]
        self.assertEqual(added["project"]["name"], "New")

        updated = self.run_msg({"action": "project_update", "project": pid,
                                "name": "Renamed"})
        self.assertEqual(updated["project"]["name"], "Renamed")

        removed = self.run_msg({"action": "project_remove", "project": pid})
        self.assert_payload(removed)
        self.assertNotIn("Renamed", [x["name"] for x in removed["projects"]])

    def test_project_remove_unknown_is_clean_error(self):
        p = self.run_msg({"action": "project_remove", "project": "ghost"})
        self.assertEqual(p["type"], "board_error")
        self.assertIn("ghost", p["error"])

    # -- card mutations --------------------------------------------------
    def test_card_add_shows_up_in_next_read(self):
        added = self.run_msg({"action": "card_add", "project": "Jarvis",
                              "title": "A brand new card"})
        self.assert_payload(added)
        card = next(c for c in added["cards"] if c["task"] == "A brand new card")
        self.assertEqual(card["project"], "Jarvis")

        read = self.run_msg({"action": "board_get", "project": "Jarvis"})
        self.assertTrue(any(c["task"] == "A brand new card" for c in read["cards"]))

    def test_card_update_move_remove(self):
        added = self.run_msg({"action": "card_add", "project": "Jarvis",
                              "title": "Movable"})
        cid = next(c["id"] for c in added["cards"] if c["task"] == "Movable")

        moved = self.run_msg({"action": "card_move", "card_id": cid,
                              "status": "Done"})
        card = next(c for c in moved["cards"] if c["id"] == cid)
        self.assertEqual(card["status"], "Done")

        updated = self.run_msg({"action": "card_update", "card_id": cid,
                                "title": "Renamed card", "notes": "short"})
        card = next(c for c in updated["cards"] if c["id"] == cid)
        self.assertEqual(card["task"], "Renamed card")
        self.assertEqual(card["note"], "short")

        removed = self.run_msg({"action": "card_remove", "card_id": cid})
        self.assertFalse(any(c["id"] == cid for c in removed["cards"]))

    def test_card_add_without_title_is_clean_error(self):
        p = self.run_msg({"action": "card_add", "project": "Jarvis"})
        self.assertEqual(p["type"], "board_error")

    def test_card_add_unknown_project_is_clean_error(self):
        p = self.run_msg({"action": "card_add", "project": "ghost",
                          "title": "x"})
        self.assertEqual(p["type"], "board_error")
        self.assertIn("ghost", p["error"])

    def test_card_move_unknown_status_is_clean_error(self):
        p = self.run_msg({"action": "card_move", "card_id": "c1",
                          "status": "Nowhere"})
        self.assertEqual(p["type"], "board_error")

    def test_card_mutations_unknown_card_are_clean_errors(self):
        for action in ("card_update", "card_move", "card_remove"):
            p = self.run_msg({"action": action, "card_id": "ghost",
                              "title": "x", "status": "Done"})
            self.assertEqual(p["type"], "board_error", (action, p))
            self.assertIn("ghost", p["error"])

    def test_unknown_action_is_clean_error(self):
        p = self.run_msg({"action": "wat"})
        self.assertEqual(p["type"], "board_error")

    # -- live work is visible to the screen ------------------------------
    def test_live_work_appears_and_disappears(self):
        self.store.upsert_live("amit", "reading the docs", note="page 3",
                               writer=True)
        shown = self.run_msg({"action": "board_get", "project": "Jarvis"})
        live = [c for c in shown["columns"][1]["cards"] if c.get("live")]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["owner"], "Amit")

        self.store.clear_live("amit", writer=True)
        after = self.run_msg({"action": "board_get", "project": "Jarvis"})
        self.assertFalse(any(c.get("live") for c in after["cards"]))

    def test_render_payload_is_the_canonical_table(self):
        p = self.run_msg({"action": "projects_list"})
        self.assertEqual(p["render"], board_mod.render_table(p["cards"]))


if __name__ == "__main__":
    unittest.main()
