"""Tests for the LSP client (C2+). Skips when pylsp is not installed."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lsp_client  # noqa: E402


@unittest.skipUnless(shutil.which("pylsp"), "pylsp not installed")
class LSPDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="asha-lsp-"))
        self.file = self.dir / "sample.py"
        self.file.write_text(
            "def f():\n    return undefined_name + 1\n", encoding="utf-8")

    def test_reports_undefined_name(self):
        out = lsp_client.lsp_diagnostics(str(self.file), str(self.dir),
                                         self.file.read_text())
        self.assertIsNotNone(out)
        self.assertIn("Undefined name", out)

    def test_clean_file_reports_no_diagnostics(self):
        self.file.write_text("def f():\n    return 1\n", encoding="utf-8")
        out = lsp_client.lsp_diagnostics(str(self.file), str(self.dir),
                                         self.file.read_text())
        self.assertIn("No diagnostics", out)

    def test_unknown_language_returns_none(self):
        txt = self.dir / "notes.md"
        txt.write_text("hello\n", encoding="utf-8")
        self.assertIsNone(lsp_client.lsp_diagnostics(str(txt), str(self.dir), "hello"))

    def test_symbols_outline(self):
        self.file.write_text(
            "def alpha():\n    pass\n\n\nclass Beta:\n    pass\n", encoding="utf-8")
        out = lsp_client.lsp_action(str(self.file), str(self.dir),
                                    self.file.read_text(), "symbols")
        self.assertIn("alpha", out)
        self.assertIn("Beta", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
