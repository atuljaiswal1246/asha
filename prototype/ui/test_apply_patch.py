"""Tests for apply_patch.py (stdlib unittest — pytest is not installed).

Run with:  python3 prototype/ui/test_apply_patch.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from apply_patch import (  # noqa: E402  (path setup must run first)
    FilePatch,
    Hunk,
    PatchError,
    apply_hunks,
    apply_patch_text,
    parse_patch,
)


class ParseTests(unittest.TestCase):
    def test_parse_add_update_delete(self) -> None:
        text = (
            "*** Begin Patch\n"
            "*** Add File: a.py\n"
            "+def one():\n"
            "+    return 1\n"
            "*** Update File: b.py\n"
            "@@\n"
            "-old1\n"
            "+new1\n"
            "@@ section two\n"
            " keep\n"
            "+added\n"
            "*** Delete File: c.py\n"
            "*** End Patch\n"
        )
        patches = parse_patch(text)
        self.assertEqual([p.op for p in patches], ["add", "update", "delete"])

        add = patches[0]
        self.assertEqual(add.path, "a.py")
        self.assertEqual(add.content, ["def one():", "    return 1"])
        self.assertEqual(add.hunks, [])

        upd = patches[1]
        self.assertEqual(upd.path, "b.py")
        self.assertEqual(len(upd.hunks), 2)
        self.assertEqual(upd.hunks[0].old_lines, ["old1"])
        self.assertEqual(upd.hunks[0].new_lines, ["new1"])
        self.assertEqual(upd.hunks[1].old_lines, ["keep"])
        self.assertEqual(upd.hunks[1].new_lines, ["keep", "added"])
        self.assertEqual(upd.content, [])

        delete = patches[2]
        self.assertEqual(delete.path, "c.py")
        self.assertEqual(delete.hunks, [])
        self.assertEqual(delete.content, [])

    def test_parse_removed_and_added_empty_lines(self) -> None:
        text = (
            "*** Begin Patch\n"
            "*** Update File: f.txt\n"
            "@@\n"
            "-\n"
            "+\n"
            " keep\n"
            "*** End Patch\n"
        )
        patches = parse_patch(text)
        hunk = patches[0].hunks[0]
        self.assertEqual(hunk.old_lines, ["", "keep"])
        self.assertEqual(hunk.new_lines, ["", "keep"])

    def test_parse_end_of_file_ignored(self) -> None:
        text = (
            "*** Begin Patch\n"
            "*** Update File: f.txt\n"
            "@@\n"
            "-a\n"
            "+b\n"
            "*** End of File\n"
            "@@\n"
            "-c\n"
            "+d\n"
            "*** End of File\n"
            "*** End Patch\n"
        )
        patches = parse_patch(text)
        self.assertEqual(len(patches[0].hunks), 2)
        self.assertEqual(patches[0].hunks[0].old_lines, ["a"])
        self.assertEqual(patches[0].hunks[1].old_lines, ["c"])

    def test_malformed_cases(self) -> None:
        cases = [
            # Missing Begin
            "*** Add File: a\n+x\n*** End Patch\n",
            # Missing End
            "*** Begin Patch\n*** Add File: a\n+x\n",
            # Unknown directive
            "*** Begin Patch\n*** Move File: a\n*** End Patch\n",
            # Add with no content
            "*** Begin Patch\n*** Add File: a.py\n*** End Patch\n",
            # Add with a non-'+' line
            "*** Begin Patch\n*** Add File: a.py\nnot prefixed\n*** End Patch\n",
            # Update with no hunks
            "*** Begin Patch\n*** Update File: a.py\n*** End Patch\n",
            # Update with a malformed body line
            "*** Begin Patch\n*** Update File: a.py\n@@\njunk line\n*** End Patch\n",
            # Empty hunk
            "*** Begin Patch\n*** Update File: a.py\n@@\n@@\n*** End Patch\n",
            # Missing path
            "*** Begin Patch\n*** Add File:\n+x\n*** End Patch\n",
            # Content after End Patch
            "*** Begin Patch\n*** Add File: a\n+x\n*** End Patch\nstray\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(PatchError):
                    parse_patch(text)

    def test_parse_leading_and_trailing_blank_lines_ok(self) -> None:
        text = (
            "\n"
            "*** Begin Patch\n"
            "*** Add File: a\n"
            "+x\n"
            "*** End Patch\n"
            "\n"
        )
        patches = parse_patch(text)
        self.assertEqual(patches[0].path, "a")


class ApplyHunksTests(unittest.TestCase):
    def test_exact_update(self) -> None:
        original = "def foo():\n    return 1\n"
        hunks = [Hunk(old_lines=["    return 1"], new_lines=["    return 2"])]
        self.assertEqual(apply_hunks(original, hunks), "def foo():\n    return 2\n")

    def test_no_hunks_returns_original(self) -> None:
        self.assertEqual(apply_hunks("abc\n", []), "abc\n")

    def test_trim_end_trailing_whitespace(self) -> None:
        original = "x = 1  \ny = 2\n"  # trailing spaces on the first line
        hunks = [Hunk(old_lines=["x = 1"], new_lines=["x = 10"])]
        self.assertEqual(apply_hunks(original, hunks), "x = 10\ny = 2\n")

    def test_trim_leading_indent_mismatch(self) -> None:
        original = "    return 1\n"  # 4 spaces
        hunks = [Hunk(old_lines=["  return 1"], new_lines=["  return 2"])]  # 2 spaces
        self.assertEqual(apply_hunks(original, hunks), "  return 2\n")

    def test_normalized_unicode_quotes_nbsp_endash(self) -> None:
        original = "total = 1\u20132\nname = \u201chello\u201d\nflag = 5\u00a07\n"
        hunks = [
            Hunk(
                old_lines=["total = 1-2", 'name = "hello"', "flag = 5 7"],
                new_lines=["TOTAL = 1", 'NAME = "hello"', "FLAG = 57"],
            )
        ]
        self.assertEqual(
            apply_hunks(original, hunks),
            "TOTAL = 1\nNAME = \"hello\"\nFLAG = 57\n",
        )

    def test_multi_hunk_update(self) -> None:
        original = "line1 = 1\nline2 = 2\nline3 = 3\nline4 = 4\n"
        hunks = [
            Hunk(old_lines=["line2 = 2"], new_lines=["line2 = 22"]),
            Hunk(old_lines=["line4 = 4"], new_lines=["line4 = 44"]),
        ]
        result = apply_hunks(original, hunks)
        self.assertEqual(
            result, "line1 = 1\nline2 = 22\nline3 = 3\nline4 = 44\n"
        )

    def test_ordering_prefers_after_previous_hunk(self) -> None:
        original = "x = 1\nx = 1\n"
        hunks = [
            Hunk(old_lines=["x = 1"], new_lines=["y = 1"]),
            Hunk(old_lines=["x = 1"], new_lines=["y = 2"]),
        ]
        self.assertEqual(apply_hunks(original, hunks), "y = 1\ny = 2\n")

    def test_strategy_locks_into_place(self) -> None:
        # Hunk 1 can only match via trimEnd -> the file locks trimEnd.
        # Hunk 2 has a leading-indent mismatch that trimEnd cannot fix, so it
        # must raise rather than relax to a looser strategy.
        original = "a = 1  \n  b = 2\n"
        hunks = [
            Hunk(old_lines=["a = 1"], new_lines=["a = 10"]),
            Hunk(old_lines=["b = 2"], new_lines=["b = 20"]),
        ]
        with self.assertRaises(PatchError):
            apply_hunks(original, hunks)

    def test_pure_insertion_appends_at_end(self) -> None:
        original = "a\nb\n"
        hunks = [Hunk(old_lines=[], new_lines=["c"])]
        self.assertEqual(apply_hunks(original, hunks), "a\nb\nc\n")

    def test_pure_insertion_into_empty_file(self) -> None:
        hunks = [Hunk(old_lines=[], new_lines=["only"])]
        self.assertEqual(apply_hunks("", hunks), "only")
        self.assertEqual(apply_hunks("\n", hunks), "\nonly\n")

    def test_trailing_newline_not_preserved_when_absent(self) -> None:
        original = "a\nb"
        hunks = [Hunk(old_lines=["b"], new_lines=["c"])]
        self.assertEqual(apply_hunks(original, hunks), "a\nc")

    def test_no_match_raises_with_first_old_line(self) -> None:
        with self.assertRaises(PatchError) as ctx:
            apply_hunks("alpha\nbeta\n", [Hunk(old_lines=["gamma"], new_lines=["x"])])
        self.assertIn("gamma", str(ctx.exception))

    def test_no_match_raises_after_insertion(self) -> None:
        # Insertion moves a hunk's expected context; the created line must not
        # accidentally satisfy a later exact search for it.
        original = "keep\n"
        hunks = [
            Hunk(old_lines=[], new_lines=["ghost"]),
            Hunk(old_lines=["ghost"], new_lines=["reveal"]),
        ]
        with self.assertRaises(PatchError):
            apply_hunks(original, hunks)


class ApplyPatchTextTests(unittest.TestCase):
    @staticmethod
    def _make_fs(start: dict[str, str]):
        def read_file(path: str) -> str:
            if path not in start:
                raise PatchError(f"file missing: {path}")
            return start[path]

        def write_file(path: str, content: str) -> None:
            start[path] = content

        def delete_file(path: str) -> None:
            start.pop(path, None)

        return read_file, write_file, delete_file

    def test_add_file(self) -> None:
        fs: dict[str, str] = {}
        read, write, delete = self._make_fs(fs)
        text = (
            "*** Begin Patch\n"
            "*** Add File: new.txt\n"
            "+a\n"
            "+b\n"
            "*** End Patch\n"
        )
        self.assertEqual(apply_patch_text(text, read, write, delete), "Applied: 1 added")
        self.assertEqual(fs["new.txt"], "a\nb\n")

    def test_delete_file(self) -> None:
        fs = {"gone.txt": "bye\n"}
        read, write, delete = self._make_fs(fs)
        text = "*** Begin Patch\n*** Delete File: gone.txt\n*** End Patch\n"
        self.assertEqual(apply_patch_text(text, read, write, delete), "Applied: 1 deleted")
        self.assertNotIn("gone.txt", fs)

    def test_multi_file_patch(self) -> None:
        fs = {"api.py": "def f():\n    return 1\n", "legacy.py": "old\n"}
        read, write, delete = self._make_fs(fs)
        text = (
            "*** Begin Patch\n"
            "*** Add File: utils.py\n"
            "+def helper():\n"
            "+    return 42\n"
            "*** Update File: api.py\n"
            "@@\n"
            " def f():\n"
            "-    return 1\n"
            "+    return 2\n"
            "*** Delete File: legacy.py\n"
            "*** End Patch\n"
        )
        summary = apply_patch_text(text, read, write, delete)
        self.assertEqual(summary, "Applied: 1 updated, 1 added, 1 deleted")
        self.assertEqual(fs["utils.py"], "def helper():\n    return 42\n")
        self.assertEqual(fs["api.py"], "def f():\n    return 2\n")
        self.assertNotIn("legacy.py", fs)

    def test_missing_file_raises_patch_error(self) -> None:
        read, write, delete = self._make_fs({})
        text = "*** Begin Patch\n*** Update File: nope.txt\n@@\n-x\n*** End Patch\n"
        with self.assertRaises(PatchError):
            apply_patch_text(text, read, write, delete)

    def test_update_unlocated_raises_naming_path(self) -> None:
        fs = {"app.py": "print('hi')\n"}
        read, write, delete = self._make_fs(fs)
        text = "*** Begin Patch\n*** Update File: app.py\n@@\n-nooooo\n+yes\n*** End Patch\n"
        with self.assertRaises(PatchError) as ctx:
            apply_patch_text(text, read, write, delete)
        message = str(ctx.exception)
        self.assertIn("app.py", message)
        self.assertIn("nooooo", message)

    def test_multiple_updates_to_same_file(self) -> None:
        fs = {"f.py": "a\nb\nc\n"}
        read, write, delete = self._make_fs(fs)
        text = (
            "*** Begin Patch\n"
            "*** Update File: f.py\n"
            "@@\n"
            "-a\n"
            "+a1\n"
            "@@\n"
            "-c\n"
            "+c1\n"
            "*** End Patch\n"
        )
        summary = apply_patch_text(text, read, write, delete)
        self.assertEqual(summary, "Applied: 1 updated")
        self.assertEqual(fs["f.py"], "a1\nb\nc1\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)