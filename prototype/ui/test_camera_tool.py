"""Tests for Jarvis's camera — helper mocked, real hardware marked and skipped."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import camera_tool
from camera_tool import CameraError


def _jpeg_bytes() -> bytes:
    return b"\xff\xd8\xff\xe0" + b"jarvis-camera-test" + b"\xff\xd9"


def _run_writing(returncode: int = 0, stderr: str = ""):
    def run(cmd, **kwargs):
        if returncode == 0:
            Path(cmd[1]).write_bytes(_jpeg_bytes())
        return mock.Mock(returncode=returncode, stderr=stderr, stdout="")
    return run


class BinaryBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)

    def _patch_cache(self):
        return mock.patch.object(camera_tool.screen_tool, "_data_dir",
                                 return_value=self.cache)

    def test_swiftc_missing_raises_clear_error(self):
        with self._patch_cache(), \
             mock.patch.object(camera_tool.shutil, "which", return_value=None):
            with self.assertRaises(CameraError) as cm:
                camera_tool._camera_binary()
        self.assertIn("swiftc", str(cm.exception))

    def test_swiftc_failure_raises_clear_error(self):
        proc = mock.Mock(returncode=1, stderr="error: no such module 'AVFoundation'",
                         stdout="")
        with self._patch_cache(), \
             mock.patch.object(camera_tool.shutil, "which", return_value="/usr/bin/swiftc"), \
             mock.patch.object(camera_tool.subprocess, "run", return_value=proc):
            with self.assertRaises(CameraError) as cm:
                camera_tool._camera_binary()
        self.assertIn("could not build", str(cm.exception))
        self.assertIn("AVFoundation", str(cm.exception))

    def test_cached_binary_is_reused(self):
        tools = self.cache / "tools"
        tools.mkdir(parents=True, exist_ok=True)
        binary = tools / "jarvis_camera"
        source = tools / "jarvis_camera.swift"
        binary.write_bytes(b"binary")
        source.write_text(camera_tool._CAPTURE_SRC)
        with self._patch_cache(), \
             mock.patch.object(camera_tool.subprocess, "run") as run:
            self.assertEqual(camera_tool._camera_binary(), binary)
            run.assert_not_called()


class AvailableTests(unittest.TestCase):
    def test_true_when_helper_and_camera(self):
        with mock.patch.object(camera_tool, "_camera_binary",
                               return_value=Path("/tmp/cam")), \
             mock.patch.object(camera_tool.subprocess, "run",
                               return_value=mock.Mock(returncode=0)):
            self.assertTrue(camera_tool.available())

    def test_false_when_no_camera(self):
        with mock.patch.object(camera_tool, "_camera_binary",
                               return_value=Path("/tmp/cam")), \
             mock.patch.object(camera_tool.subprocess, "run",
                               return_value=mock.Mock(returncode=2)):
            self.assertFalse(camera_tool.available())

    def test_false_when_helper_unbuildable(self):
        with mock.patch.object(camera_tool, "_camera_binary",
                               side_effect=CameraError("swiftc not found")):
            self.assertFalse(camera_tool.available())

    def test_false_when_check_raises(self):
        with mock.patch.object(camera_tool, "_camera_binary",
                               return_value=Path("/tmp/cam")), \
             mock.patch.object(camera_tool.subprocess, "run",
                               side_effect=OSError("boom")):
            self.assertFalse(camera_tool.available())


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_success_returns_jpeg_path(self):
        out = Path(self.tmp.name) / "shot.jpg"
        with mock.patch.object(camera_tool, "_camera_binary",
                               return_value=Path("/tmp/cam")), \
             mock.patch.object(camera_tool.subprocess, "run",
                               side_effect=_run_writing(0)):
            got = camera_tool.capture(str(out))
        self.assertEqual(got, out)
        self.assertTrue(out.exists())
        self.assertEqual(out.read_bytes()[:2], b"\xff\xd8")

    def test_default_path_when_none_given(self):
        with mock.patch.object(camera_tool, "_camera_binary",
                               return_value=Path("/tmp/cam")), \
             mock.patch.object(camera_tool.subprocess, "run",
                               side_effect=_run_writing(0)):
            got = camera_tool.capture()
        self.assertEqual(got.name, "jarvis-camera.jpg")
        self.assertTrue(got.exists())

    def test_no_camera_message(self):
        out = Path(self.tmp.name) / "shot.jpg"
        with mock.patch.object(camera_tool, "_camera_binary",
                               return_value=Path("/tmp/cam")), \
             mock.patch.object(camera_tool.subprocess, "run",
                               return_value=mock.Mock(returncode=2, stderr="no camera")):
            with self.assertRaises(CameraError) as cm:
                camera_tool.capture(str(out))
        self.assertIn("no camera", str(cm.exception))

    def test_permission_denied_message(self):
        out = Path(self.tmp.name) / "shot.jpg"
        with mock.patch.object(camera_tool, "_camera_binary",
                               return_value=Path("/tmp/cam")), \
             mock.patch.object(camera_tool.subprocess, "run",
                               return_value=mock.Mock(returncode=3, stderr="denied")):
            with self.assertRaises(CameraError) as cm:
                camera_tool.capture(str(out))
        self.assertIn("System Settings", str(cm.exception))

    def test_capture_failure_message(self):
        out = Path(self.tmp.name) / "shot.jpg"
        with mock.patch.object(camera_tool, "_camera_binary",
                               return_value=Path("/tmp/cam")), \
             mock.patch.object(camera_tool.subprocess, "run",
                               return_value=mock.Mock(returncode=4, stderr="timed out")):
            with self.assertRaises(CameraError) as cm:
                camera_tool.capture(str(out))
        self.assertIn("could not take a frame", str(cm.exception))

    def test_timeout_message(self):
        out = Path(self.tmp.name) / "shot.jpg"
        with mock.patch.object(camera_tool, "_camera_binary",
                               return_value=Path("/tmp/cam")), \
             mock.patch.object(camera_tool.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(cmd="cam", timeout=30)):
            with self.assertRaises(CameraError) as cm:
                camera_tool.capture(str(out))
        self.assertIn("in time", str(cm.exception))

    def test_helper_compile_failure_propagates_clearly(self):
        with mock.patch.object(camera_tool, "_camera_binary",
                               side_effect=CameraError("swiftc not found")):
            with self.assertRaises(CameraError):
                camera_tool.capture()


class LookTests(unittest.TestCase):
    def test_no_camera_is_a_message_not_an_error(self):
        with mock.patch.object(camera_tool, "capture",
                               side_effect=CameraError("no camera found on this Mac")):
            report = camera_tool.look("what is this")
        self.assertIn("could not look", report)
        self.assertIn("no camera", report)

    def test_ocr_only_includes_path(self):
        shot = Path("/tmp/jarvis-camera.jpg")
        with mock.patch.object(camera_tool, "capture", return_value=shot), \
             mock.patch.object(camera_tool.screen_tool, "ocr", return_value="HELLO"), \
             mock.patch.object(camera_tool.screen_tool, "vision_ready",
                               return_value=False):
            report = camera_tool.look()
        self.assertIn("HELLO", report)
        self.assertIn("[camera:", report)
        self.assertIn(str(shot), report)

    def test_question_reuses_screen_tool_look(self):
        shot = Path("/tmp/jarvis-camera.jpg")
        with mock.patch.object(camera_tool, "capture", return_value=shot), \
             mock.patch.object(camera_tool.screen_tool, "ocr", return_value="HELLO"), \
             mock.patch.object(camera_tool.screen_tool, "vision_ready",
                               return_value=True), \
             mock.patch.object(camera_tool.screen_tool, "look",
                               return_value="A RED MUG") as vision:
            report = camera_tool.look("what is this")
        vision.assert_called_once_with(shot, "what is this")
        self.assertIn("A RED MUG", report)
        self.assertIn(str(shot), report)

    def test_question_without_vision_falls_back_to_ocr(self):
        shot = Path("/tmp/jarvis-camera.jpg")
        with mock.patch.object(camera_tool, "capture", return_value=shot), \
             mock.patch.object(camera_tool.screen_tool, "ocr", return_value="TEXT ONLY"), \
             mock.patch.object(camera_tool.screen_tool, "vision_ready",
                               return_value=False), \
             mock.patch.object(camera_tool.screen_tool, "look") as vision:
            report = camera_tool.look("read it")
        vision.assert_not_called()
        self.assertIn("TEXT ONLY", report)

    def test_vision_failure_falls_back_to_ocr(self):
        shot = Path("/tmp/jarvis-camera.jpg")
        with mock.patch.object(camera_tool, "capture", return_value=shot), \
             mock.patch.object(camera_tool.screen_tool, "ocr", return_value="TEXT"), \
             mock.patch.object(camera_tool.screen_tool, "vision_ready",
                               return_value=True), \
             mock.patch.object(camera_tool.screen_tool, "look",
                               side_effect=RuntimeError("model down")):
            report = camera_tool.look("what is this")
        self.assertIn("TEXT", report)
        self.assertIn("vision model failed", report)

    def test_ocr_failure_falls_back_to_message(self):
        shot = Path("/tmp/jarvis-camera.jpg")
        with mock.patch.object(camera_tool, "capture", return_value=shot), \
             mock.patch.object(camera_tool.screen_tool, "ocr",
                               side_effect=RuntimeError("no vision")):
            report = camera_tool.look()
        self.assertIn("ocr unavailable", report)
        self.assertIn("[camera:", report)


class LookDegradationTests(unittest.TestCase):
    """Every hardware failure is a message the user can act on, never a raise."""

    def test_permission_denied_names_system_settings(self):
        with mock.patch.object(camera_tool, "capture", side_effect=CameraError(
                "camera access was not granted — turn it on in System Settings → "
                "Privacy & Security → Camera")):
            report = camera_tool.look("what is this")
        self.assertIn("could not look", report)
        self.assertIn("System Settings", report)
        self.assertIn("Camera", report)

    def test_compile_failure_is_a_message_not_an_error(self):
        with mock.patch.object(camera_tool, "capture", side_effect=CameraError(
                "swiftc not found — the camera needs the Xcode command line tools")):
            report = camera_tool.look()
        self.assertIn("could not look", report)
        self.assertIn("swiftc", report)

    def test_no_camera_message_is_actionable(self):
        with mock.patch.object(camera_tool, "capture",
                               side_effect=CameraError("no camera found on this Mac")):
            report = camera_tool.look()
        self.assertIn("no camera", report)

    def test_success_returns_spoken_text_and_path(self):
        shot = Path("/tmp/jarvis-camera.jpg")
        with mock.patch.object(camera_tool, "capture", return_value=shot), \
             mock.patch.object(camera_tool.screen_tool, "ocr",
                               return_value="A RED MUG"), \
             mock.patch.object(camera_tool.screen_tool, "vision_ready",
                               return_value=False):
            report = camera_tool.look("what is this")
        self.assertIn("A RED MUG", report)
        self.assertIn(str(shot), report)


class ToolRegistrationTests(unittest.TestCase):
    """look_through_camera must stay wired in both tiers (like read_screen)."""

    def test_voice_tier_registers_the_camera_tool(self):
        import server

        schemas = {s.name: s for s in server._voice_tool_schemas()}
        self.assertIn("look_through_camera", schemas,
                      "look_through_camera fell out of the voice tool list")
        self.assertIn("question", schemas["look_through_camera"].properties)
        self.assertEqual(schemas["look_through_camera"].required, [])

    def test_coding_tier_registers_the_camera_tool(self):
        import agent_loop

        names = [t["function"]["name"] for t in agent_loop.TOOLS]
        self.assertIn("look_through_camera", names,
                      "look_through_camera fell out of the coding tool list")


@unittest.skipUnless(os.environ.get("JARVIS_CAMERA_HW") == "1",
                     "set JARVIS_CAMERA_HW=1 with a real camera to run")
class HardwareTests(unittest.TestCase):
    def test_real_capture_writes_a_jpeg(self):
        if not camera_tool.available():
            self.skipTest("no camera available to this process")
        shot = camera_tool.capture()
        self.assertTrue(shot.exists())
        self.assertGreater(shot.stat().st_size, 1024)
        self.assertEqual(shot.read_bytes()[:2], b"\xff\xd8")
