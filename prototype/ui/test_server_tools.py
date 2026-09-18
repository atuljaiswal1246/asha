"""Coding-path tests (D1.1): sandbox, patch tool, and routing.

Importing ``server`` is heavy (pipecat) and reads the repo's .env, so run
this with the project venv:

    .venv/bin/python prototype/ui/test_server_tools.py
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import server  # noqa: E402


class _IsolatedRepo(unittest.TestCase):
    """Point server._REPO_ROOT at a temp dir so tests never touch the real
    active project (which may be the user's app repo)."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix="asha-toolroot-")
        self._saved_root = server._REPO_ROOT
        # Resolve so /var -> /private/var symlink doesn't break _safe_path.
        server._REPO_ROOT = Path(self._tmp).resolve()

    def tearDown(self):
        import shutil
        server._REPO_ROOT = self._saved_root
        shutil.rmtree(self._tmp, ignore_errors=True)


class SafePathTests(_IsolatedRepo):
    def test_relative_path_inside_repo(self):
        self.assertIsNotNone(server._safe_path("notes/ROADMAP.md"))

    def test_absolute_path_inside_repo(self):
        inside = str(Path(server._REPO_ROOT) / "notes" / "ROADMAP.md")
        self.assertIsNotNone(server._safe_path(inside))

    def test_traversal_escape_rejected(self):
        self.assertIsNone(server._safe_path("../../etc/passwd"))

    def test_absolute_escape_rejected(self):
        self.assertIsNone(server._safe_path("/tmp/opencode/nope.txt"))


class ApplyPatchToolTests(_IsolatedRepo):
    REL = "prototype/data/__patch_tool_test__.txt"

    def setUp(self):
        super().setUp()
        self.fp = Path(server._REPO_ROOT) / self.REL
        self.fp.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        try:
            self.fp.unlink()
        except FileNotFoundError:
            pass
        super().tearDown()

    def test_add_then_fuzzy_update(self):
        add = (
            "*** Begin Patch\n"
            f"*** Add File: {self.REL}\n"
            "+def add(a, b):\n"
            "+    return a + b\n"
            "*** End Patch"
        )
        self.assertIn("added", server.apply_patch(add))
        # update with leading-indent + trailing-space drift (strategy 3/2)
        upd = (
            "*** Begin Patch\n"
            f"*** Update File: {self.REL}\n"
            "@@\n"
            "-def add(a, b):   \n"
            "+def add(a: int, b: int) -> int:\n"
            "*** End Patch"
        )
        self.assertIn("updated", server.apply_patch(upd))
        self.assertIn("-> int", self.fp.read_text(encoding="utf-8"))

    def test_escape_rejected_by_wrapper(self):
        patch = (
            "*** Begin Patch\n"
            "*** Add File: /tmp/opencode/escape_me.txt\n"
            "+x\n"
            "*** End Patch"
        )
        out = server.apply_patch(patch)
        self.assertIn("escapes the repository", out)
        self.assertFalse(Path("/tmp/opencode/escape_me.txt").exists())

    def test_malformed_patch_returns_error(self):
        self.assertIn("Error", server.apply_patch("not a patch"))
        self.assertIn("empty patch", server.apply_patch("   "))


class RoutingTests(unittest.TestCase):
    def test_dispatch_from_idle(self):
        self.assertEqual(
            server.classify_code_text("fix the bug in server.py", "idle"),
            "dispatch",
        )

    def test_stop_wins_while_running(self):
        self.assertEqual(
            server.classify_code_text("stop", "running"), "stop"
        )

    def test_plain_chat_is_none(self):
        self.assertEqual(
            server.classify_code_text("what is the weather", "idle"), "none"
        )

    def test_orchestrate_keyword(self):
        self.assertEqual(
            server.classify_code_text("orchestrate add tests", "idle"),
            "orchestrate",
        )

    def test_yes_no_only_when_proposed(self):
        self.assertEqual(server.classify_code_text("yes", "idle"), "none")
        self.assertEqual(server.classify_code_text("yes", "proposed"), "yes")

    def test_coding_request_detects_code_context(self):
        self.assertTrue(server.is_coding_request("refactor the auth module"))
        self.assertFalse(server.is_coding_request("what's the weather"))


class BrainContractTests(unittest.TestCase):
    def test_jarvis_prompt_includes_operating_contract(self):
        t = server.build_system_text("You are Asha.", "")
        self.assertIn("HOW YOU WORK", t)
        self.assertIn("WORK PROTOCOL", t)
        self.assertIn("STYLE SCOPE", t)
        for tool in ("apply_patch", "switch_project", "diagnostics", "lsp"):
            self.assertIn(tool, t)


class ToolUpgradeTests(_IsolatedRepo):
    """A2.2: read line ranges, grep, fuzzy edit — scoped to the active project."""

    REL = "prototype/data/__tool_upgrade_test__.txt"

    def setUp(self):
        super().setUp()
        self.fp = Path(server._REPO_ROOT) / self.REL
        self.fp.parent.mkdir(parents=True, exist_ok=True)
        self.fp.write_text(
            "alpha one\n"
            "def add(a, b):   \n"
            "    return a + b\n"
            "beta two\n",
            encoding="utf-8",
        )

    def tearDown(self):
        try:
            self.fp.unlink()
        except FileNotFoundError:
            pass
        super().tearDown()

    def test_read_line_range_is_numbered(self):
        import asyncio
        out = asyncio.run(server.read_file(self.REL, offset=2, limit=2))
        self.assertIn("2\tdef add(a, b):", out)
        self.assertIn("3\t    return a + b", out)
        self.assertNotIn("alpha one", out)

    def test_grep_finds_pattern_with_glob(self):
        import asyncio
        out = asyncio.run(server.grep("def add", path="prototype/data",
                                      glob="__tool_upgrade_test__.txt"))
        self.assertIn(f"{self.REL}:2:", out)

    def test_edit_fuzzy_matches_whitespace_drift(self):
        import asyncio
        # The file's first line has trailing spaces; the exact substring
        # (without them, plus a second line) is absent → fuzzy path must fire.
        out = asyncio.run(server.edit_file(
            self.REL,
            "def add(a, b):\n    return a + b",
            "def add(a: int, b: int) -> int:\n    return a + b",
        ))
        self.assertIn("fuzzy match", out)
        self.assertIn("-> int", self.fp.read_text(encoding="utf-8"))

    def test_grep_bad_regex_is_reported(self):
        import asyncio
        out = asyncio.run(server.grep("([unclosed", path="prototype/data"))
        self.assertIn("bad regex", out)


class TodoToolTests(unittest.TestCase):
    def test_renders_marks_and_clears(self):
        out = server.todo_update([
            {"content": "a", "status": "completed"},
            {"content": "b", "status": "in_progress"},
            {"content": "c"},
            {"content": "", "status": "pending"},  # dropped
        ])
        self.assertIn("[x] a", out)
        self.assertIn("[~] b", out)
        self.assertIn("[ ] c", out)
        self.assertEqual(server.todo_update([]), "Todo list cleared.")

    def test_bad_status_becomes_pending(self):
        out = server.todo_update([{"content": "x", "status": "weird"}])
        self.assertIn("[ ] x", out)

    def test_non_list_is_error(self):
        self.assertIn("Error", server.todo_update("nope"))


class ContextAndRepoTests(_IsolatedRepo):
    """A5: repo map + compaction digest."""

    def test_repo_map_lists_files_and_respects_cap(self):
        out = server.repo_map("", max_entries=5)
        self.assertNotIn("Error", out)
        if out != "(no files)":
            self.assertLessEqual(len(out.splitlines()), 6)  # 5 + truncation mark

    def test_repo_map_escape_rejected(self):
        self.assertIn("Error", server.repo_map("../../etc"))

    def test_digest_empty_when_no_messages(self):
        self.assertEqual(server.BoundedContextLLM._summarize_dropped([]), "")

    def test_digest_summarizes_roles(self):
        d = server.BoundedContextLLM._summarize_dropped([
            {"role": "user", "content": "add a widget"},
            {"role": "assistant", "content": "done"},
        ])
        self.assertIn("user: add a widget", d)
        self.assertIn("assistant: done", d)


class DiagnosticsTests(_IsolatedRepo):
    """C2: diagnostics (LSP-lite) edit feedback."""

    REL = "prototype/data/__diag_test__.py"

    def test_syntax_error_reported(self):
        p = Path(server._REPO_ROOT) / self.REL
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("def x(:\n", encoding="utf-8")
        try:
            out = server.diagnostics(self.REL)
            # LSP reports "SyntaxError"; the fallback says "Syntax error".
            self.assertTrue("Syntax error" in out or "SyntaxError" in out, out)
        finally:
            p.unlink()

    def test_non_python_has_no_diagnostics(self):
        p = Path(server._REPO_ROOT) / "notes" / "ROADMAP.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# notes\n", encoding="utf-8")
        self.assertIn("No diagnostics available", server.diagnostics("notes/ROADMAP.md"))


class PlanModeTests(_IsolatedRepo):
    """P3: plan mode denies edits except the plan file."""

    def setUp(self):
        super().setUp()
        server._PLAN_MODE["on"] = False

    def tearDown(self):
        server._PLAN_MODE["on"] = False
        p = Path(server._REPO_ROOT) / server._PLAN_MODE["file"]
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        super().tearDown()

    def test_plan_mode_blocks_regular_edit(self):
        import asyncio
        server._PLAN_MODE["on"] = True
        out = asyncio.run(server.write_file("prototype/data/__plan_test__.txt", "x"))
        self.assertIn("plan mode", out)

    def test_plan_mode_allows_plan_file(self):
        import asyncio
        server._PLAN_MODE["on"] = True
        out = asyncio.run(server.write_file(server._PLAN_MODE["file"], "the plan"))
        self.assertNotIn("plan mode", out)

    def test_plan_mode_blocks_shell(self):
        import asyncio
        server._PLAN_MODE["on"] = True
        out = asyncio.run(server.run_bash("echo hi"))
        self.assertIn("plan mode", out)


class ReadonlyBashTests(unittest.TestCase):
    """Read-only shell commands auto-allow (no permission stall)."""

    def test_allows_reads(self):
        for c in ('find /x -name "*.md" | head -30', "ls -la", "git status",
                  "grep -n foo bar.py", "cat README.md", "git diff",
                  'cd /Users/x && ls -la && echo "---" && cat AGENTS.md 2>/dev/null | head -100',
                  "cd /Users/x && find lib -type f | sort",
                  "cd /Users/x && sed -n '160,254p' AGENTS.md"):
            self.assertTrue(server._is_readonly_bash(c), c)

    def test_blocks_writes(self):
        for c in ("rm -rf x", "echo hi > file", "git commit -m x",
                  "curl http://x", "sed -i s/a/b/ f", "mv a b", "pip install x",
                  "cd /tmp && rm -rf x", "cat foo && rm -rf x",
                  "cd /tmp && curl http://x", "python -c 'open(\"f\",\"w\")'"):
            self.assertFalse(server._is_readonly_bash(c), c)


class CompactionTests(unittest.TestCase):
    """#2: the first user message (the task) is pinned; dropped turns summarized."""

    def _llm(self, max_chars=40):
        llm = server.BoundedContextLLM.__new__(server.BoundedContextLLM)
        llm.MAX_CHARS = max_chars
        return llm

    def test_first_user_message_pinned_and_digest_present(self):
        ctx = server.LLMContext()
        ctx.set_messages([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "TASK: write the doc"},
            {"role": "assistant", "content": "working"},
            {"role": "tool", "content": "x" * 500},
            {"role": "assistant", "content": "still working on it"},
        ])
        self._llm()._trim_context(ctx)
        joined = " ".join((m.get("content") or "") for m in ctx.messages)
        self.assertIn("TASK: write the doc", joined)
        self.assertTrue(any("compacted" in (m.get("content") or "")
                            for m in ctx.messages))


if __name__ == "__main__":
    unittest.main(verbosity=2)
