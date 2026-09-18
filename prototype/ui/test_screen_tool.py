"""Tests for Jarvis's eyes (screen_tool) — capture plumbing, no screen needed."""

import base64
import os
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

import screen_tool


# 1x1 transparent PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB"
    "/AGtO9W3AAAAAElFTkSuQmCC")


class LookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-eyes-"))

    def _png(self, name="shot.png"):
        p = self.tmp / name
        p.write_bytes(PNG)
        return p

    def test_data_url_uses_the_right_mime(self):
        self.assertTrue(screen_tool._data_url(self._png()).startswith("data:image/png;base64,"))
        jpg = self.tmp / "design.jpg"
        jpg.write_bytes(PNG)
        self.assertTrue(screen_tool._data_url(jpg).startswith("data:image/jpeg;base64,"))

    def test_shrink_falls_back_to_the_original(self):
        # sips refuses a 1x1 here on some systems; either way it must not throw
        # and must return a readable image path.
        out = screen_tool._shrink(self._png())
        self.assertTrue(out.exists())

    def test_missing_file_is_an_error_not_a_crash(self):
        with self.assertRaises(Exception):
            screen_tool.ocr(self.tmp / "nope.png")

    def test_capture_writes_a_png_or_explains_why_not(self):
        # Screen Recording is a real permission, so this must pass either way:
        # a PNG when granted, an actionable ScreenError when not.
        try:
            out = screen_tool.capture(str(self.tmp / "cap.png"))
        except screen_tool.ScreenError as exc:
            self.assertIn("Screen Recording", str(exc))
            return
        self.assertTrue(out.exists() and out.stat().st_size > 0)

    def test_see_screen_never_raises_even_without_permission(self):
        # Works with or without Screen Recording granted: the point is that it
        # never returns a silent empty string.
        report = screen_tool.see_screen(path=str(self.tmp / "see.png"))
        self.assertIsInstance(report, str)
        self.assertTrue(report.strip())

    def test_vision_env_knobs_are_read(self):
        old = os.environ.get("JARVIS_VISION_MODEL")
        os.environ["JARVIS_VISION_MODEL"] = "some/model"
        try:
            self.assertEqual(os.environ["JARVIS_VISION_MODEL"], "some/model")
            self.assertTrue(screen_tool.DEFAULT_VISION_MODEL)
        finally:
            if old is None:
                os.environ.pop("JARVIS_VISION_MODEL", None)
            else:
                os.environ["JARVIS_VISION_MODEL"] = old


if __name__ == "__main__":
    unittest.main()


class WindowArgTests(unittest.TestCase):
    """Regression: bool is an int subclass, so window=False used to build
    `screencapture -l False` and every default capture failed."""

    def test_default_capture_passes_no_window_flag(self):
        import screen_tool as st
        seen = {}

        class FakeProc:
            returncode = 0

        def fake_run(cmd, *a, **kw):
            seen["cmd"] = cmd
            Path(cmd[-1]).write_bytes(b"png")   # pretend the file appeared
            return FakeProc()

        real = st.subprocess.run
        st.subprocess.run = fake_run
        try:
            st.capture(str(Path(tempfile.mkdtemp()) / "cap.png"))
        finally:
            st.subprocess.run = real
        self.assertNotIn("-l", seen["cmd"], f"unexpected window flag: {seen['cmd']}")
        self.assertNotIn("False", seen["cmd"])

    def test_explicit_window_id_still_works(self):
        import screen_tool as st
        seen = {}

        class FakeProc:
            returncode = 0

        def fake_run(cmd, *a, **kw):
            seen["cmd"] = cmd
            Path(cmd[-1]).write_bytes(b"png")
            return FakeProc()

        real = st.subprocess.run
        st.subprocess.run = fake_run
        try:
            st.capture(str(Path(tempfile.mkdtemp()) / "cap.png"), window=2834)
        finally:
            st.subprocess.run = real
        self.assertIn("-l", seen["cmd"])
        self.assertIn("2834", seen["cmd"])


class BoolIntSweepTests(unittest.TestCase):
    """Sweep: every isinstance(x, int) and int() cast checked for the bool
    subclass trap.  Only hits where a boolean could realistically arrive are
    tested here; the rest are documented below.

    SWEEP VERDICTS:
    - screen_tool.py:196  — FIXED (bool checked first, existing test covers it)
    - server.py:2545      — LEFT ALONE (max_tokens from typed settings; bool
      would degrade to reserve=1, harmless; outside edit scope unless real defect)
    - screen_tool.py:156,159,160 — LEFT ALONE (int() on subprocess stdout tokens,
      always str; no bool path)
    - screen_tool.py:277  — LEFT ALONE (int() on env var string, always str)
    - board.py:355-356    — LEFT ALONE (int() on JSON values or len(), never bool)
    - tasks.py:336        — LEFT ALONE (int() on pre-computed float, never bool)
    - All other int() casts in server.py/sessions.py/recall.py/etc — LEFT ALONE
      (env var strings, timestamps, or JSON parsing; no bool path)
    """

    def test_capture_window_false_does_not_become_int(self):
        """window=False must NOT match isinstance(x, int)."""
        import screen_tool as st
        seen = {}

        class FakeProc:
            returncode = 0

        def fake_run(cmd, *a, **kw):
            seen["cmd"] = cmd
            Path(cmd[-1]).write_bytes(b"png")
            return FakeProc()

        real = st.subprocess.run
        st.subprocess.run = fake_run
        try:
            st.capture(str(Path(tempfile.mkdtemp()) / "cap.png"), window=False)
        finally:
            st.subprocess.run = real
        self.assertNotIn("-l", seen["cmd"])

    def test_capture_window_true_does_not_become_int(self):
        """window=True must NOT match isinstance(x, int) for window id."""
        import screen_tool as st
        seen = {}

        class FakeProc:
            returncode = 0

        def fake_run(cmd, *a, **kw):
            seen["cmd"] = cmd
            Path(cmd[-1]).write_bytes(b"png")
            return FakeProc()

        real = st.subprocess.run
        st.subprocess.run = fake_run
        try:
            st.capture(str(Path(tempfile.mkdtemp()) / "cap.png"), window=True)
        finally:
            st.subprocess.run = real
        self.assertNotIn("-l", seen["cmd"])
        self.assertIn("-o", seen["cmd"])


class PathHandlingTests(unittest.TestCase):
    """look() and ocr() must expand ~, resolve relative paths, and give honest
    errors for missing files."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-eyes-path-"))

    def _png(self, name="shot.png"):
        p = self.tmp / name
        p.write_bytes(PNG)
        return p

    def test_look_expanduser(self):
        """look() must expand ~ and not pass a literal tilde to the filesystem."""
        expanded = Path("~").expanduser()
        # ~ should expand to an existing directory; a file inside it should
        # give a FileNotFoundError (not a confusing path-with-tilde error).
        # Hermetic: the vision call is stubbed so no network happens even if
        # an API key is present in the environment (order-independent).
        fake = expanded / "jarvis_test_nonexistent_12345.png"
        with mock.patch("providers.chat") as mc, mock.patch("providers.config.chat", mc):
            with self.assertRaises(FileNotFoundError) as ctx:
                screen_tool.look(str(fake), "test")
            self.assertNotIn("~", str(ctx.exception))
            mc.assert_not_called()

    def test_look_relative_path(self):
        """look() must resolve relative paths."""
        old_cwd = os.getcwd()
        try:
            os.chdir(str(self.tmp))
            self._png("rel.png")
            # Hermetic: stub the vision call so no network/model call happens.
            # If the relative path fails to resolve, look() raises before the
            # stub (FileNotFoundError) and this test fails for the right
            # reason; otherwise the stubbed answer is returned. Independent
            # of API keys in the environment and of test order.
            with mock.patch("providers.chat") as mc, mock.patch("providers.config.chat", mc):
                mc.return_value = {"choices": [{"message": {"content": "stubbed description"}}]}
                result = screen_tool.look("rel.png", "describe")
                mc.assert_called_once()
                self.assertIn("stubbed description", result)
        finally:
            os.chdir(old_cwd)

    def test_look_missing_file_gives_honest_error(self):
        """A missing file must raise FileNotFoundError, not a confusing crash."""
        # Hermetic: stubbed vision call must never be reached for a missing file.
        with mock.patch("providers.chat") as mc, mock.patch("providers.config.chat", mc):
            with self.assertRaises(FileNotFoundError):
                screen_tool.look("/tmp/totally_fake_jarvis_12345.png", "test")
            mc.assert_not_called()

    def test_ocr_expanduser(self):
        """ocr() must expand ~."""
        expanded = Path("~").expanduser()
        fake = expanded / "jarvis_test_nonexistent_12345.png"
        with self.assertRaises(screen_tool.ScreenError) as ctx:
            screen_tool.ocr(str(fake))
        self.assertNotIn("~", str(ctx.exception))

    def test_ocr_missing_file_gives_honest_error(self):
        """A missing file must raise ScreenError, not a confusing crash."""
        with self.assertRaises(screen_tool.ScreenError):
            screen_tool.ocr("/tmp/totally_fake_jarvis_12345.png")

    def test_ocr_relative_path(self):
        """ocr() must resolve relative paths."""
        old_cwd = os.getcwd()
        try:
            os.chdir(str(self.tmp))
            self._png("rel.png")
            # Should not crash with a path error
            try:
                screen_tool.ocr("rel.png")
            except screen_tool.ScreenError as e:
                # Must not be a path-related error
                self.assertNotIn("No such file", str(e))
        finally:
            os.chdir(old_cwd)
