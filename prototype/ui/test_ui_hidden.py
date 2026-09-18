"""Guard: a `hidden` element must not be re-shown by a `display:` rule.

The MCP panel was `display:flex` with the `hidden` attribute — the author rule
beats the UA `[hidden]{display:none}`, so the panel covered the whole app on
every load. Every panel needs a matching `.<sel>[hidden]{display:none}` rule.
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STATIC = Path(__file__).resolve().parent / "static"


def _initially_hidden(html: str) -> tuple[set[str], set[str]]:
    classes: set[str] = set()
    ids: set[str] = set()
    for tag in re.findall(r"<[a-z][^>]*\shidden[^>]*>", html, re.I):
        m = re.search(r'class="([^"]+)"', tag)
        if m:
            classes.update(m.group(1).split())
        m = re.search(r'id="([^"]+)"', tag)
        if m:
            ids.add(m.group(1))
    return classes, ids


def _display_and_hidden(css: str) -> tuple[set[str], set[str]]:
    display_sel: set[str] = set()
    hidden_ok: set[str] = set()
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if "display" not in body:
            continue
        for tok in sel.split(","):
            tok = tok.strip()
            if tok.endswith("[hidden]"):
                hidden_ok.add(tok[: -len("[hidden]")])
            elif re.fullmatch(r"[.#][\w-]+", tok):
                display_sel.add(tok)
    return display_sel, hidden_ok


class HiddenRuleTests(unittest.TestCase):
    def setUp(self):
        self.html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.css = (STATIC / "styles.css").read_text(encoding="utf-8")

    def test_no_hidden_element_is_shown_by_a_display_rule(self):
        classes, ids = _initially_hidden(self.html)
        display_sel, hidden_ok = _display_and_hidden(self.css)
        bad = [s for s in (["." + c for c in classes] + ["#" + i for i in ids])
               if s in display_sel and s not in hidden_ok]
        self.assertEqual(bad, [], "these hidden elements can never hide: " + str(bad))

    def test_the_scan_actually_finds_things(self):
        """If the regexes stop matching, the guard is silently useless."""
        classes, ids = _initially_hidden(self.html)
        display_sel, hidden_ok = _display_and_hidden(self.css)
        self.assertGreater(len(classes), 5)
        self.assertGreater(len(display_sel), 5)
        self.assertGreater(len(hidden_ok), 5)

    def test_mcp_panel_is_hideable(self):
        self.assertIn(".mcp-screen[hidden]", self.css)
        self.assertRegex(self.html, r'id="mcpScreen"[^>]*\shidden')

    def test_no_reasoning_choice_in_page(self):
        """Reasoning effort is hardwired to the model default (2026-09-17):
        the served page must offer no reasoning control and show no
        reasoning setting. (The live "thinking" activity display is
        server-driven status, not a choice, and must stay.)"""
        html = self.html
        css = self.css
        js = (STATIC / "app.js").read_text(encoding="utf-8")
        for token in ('id="reasonPicker"', 'id="brainReasonBtn"',
                      'brainReasonBtnName', 'reasoning_set', '_REASON_OPTIONS',
                      '_populateReasoning', '_selectReasoning',
                      'data-field="reasoning"', 'REASON_LABELS',
                      'How thoughtful', 'How hard this worker thinks'):
            self.assertNotIn(token, html + js,
                             f"reasoning control still served: {token}")
        for token in ('.reason-picker', '.rp-item', '.bs-trigger--reason'):
            self.assertNotIn(token, css,
                             f"reasoning style still served: {token}")
        # Guard against over-deletion: the thinking-activity display stays.
        self.assertIn("showBrainActivity('reasoning', '')", js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
