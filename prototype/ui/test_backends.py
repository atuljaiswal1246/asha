"""Tests for execution backends (G1). Pure stdlib."""
from __future__ import annotations

import os
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backends  # noqa: E402
from backends import DockerRunner, LocalRunner, get_backend  # noqa: E402


class LocalRunnerTests(unittest.TestCase):
    def test_runs_and_captures(self):
        r = LocalRunner().run([sys.executable, "-c", "print('hi')"], timeout=10)
        self.assertTrue(r["ok"])
        self.assertIn("hi", r["stdout"])

    def test_nonzero_is_not_ok(self):
        r = LocalRunner().run([sys.executable, "-c", "raise SystemExit(3)"], timeout=10)
        self.assertFalse(r["ok"])

    def test_timeout(self):
        r = LocalRunner().run([sys.executable, "-c", "import time; time.sleep(5)"],
                              timeout=0.3)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stderr"], "timeout")

    def test_cancel_kills(self):
        ev = threading.Event()
        ev.set()
        r = LocalRunner().run([sys.executable, "-c", "import time; time.sleep(5)"],
                              timeout=10, cancel=ev)
        self.assertFalse(r["ok"])
        self.assertEqual(r["stderr"], "cancelled")


class DockerRunnerTests(unittest.TestCase):
    def test_command_mounts_and_wraps(self):
        cmd = DockerRunner(image="python:3.12-slim").command(
            ["echo", "hi"], cwd="/tmp/proj")
        self.assertEqual(cmd[0], "docker")
        self.assertIn("python:3.12-slim", cmd)
        self.assertTrue(any("/tmp/proj:/work" in a for a in cmd))
        self.assertEqual(cmd[-2:], ["echo", "hi"])

    def test_network_can_be_disabled(self):
        cmd = DockerRunner(network=False).command(["x"], cwd="/tmp")
        self.assertIn("none", cmd)

    def test_extra_args_included(self):
        cmd = DockerRunner(extra_args=["-e", "K=V"]).command(["x"], cwd="/tmp")
        self.assertIn("K=V", cmd)


class SelectionTests(unittest.TestCase):
    def test_auto_falls_back_to_local_without_docker(self):
        orig = backends.docker_available
        backends.docker_available = lambda: False
        try:
            self.assertIsInstance(get_backend("auto"), LocalRunner)
            self.assertIsInstance(get_backend("docker"), LocalRunner)  # no docker → fallback
        finally:
            backends.docker_available = orig

    def test_explicit_local(self):
        self.assertIsInstance(get_backend("local"), LocalRunner)

    def test_docker_when_available(self):
        orig = backends.docker_available
        backends.docker_available = lambda: True
        try:
            self.assertIsInstance(get_backend("auto"), DockerRunner)
            self.assertIsInstance(get_backend("docker"), DockerRunner)
        finally:
            backends.docker_available = orig


class SSHRunnerTests(unittest.TestCase):
    """P7: ssh backend command construction."""

    def test_command_quotes_and_cds(self):
        cmd = backends.SSHRunner(host="srv", user="me", port=2222,
                                 remote_root="/srv/app").command(
            ["python", "-c", "print('x')"])
        self.assertEqual(cmd[0], "ssh")
        self.assertIn("2222", cmd)
        self.assertEqual(cmd[-2], "me@srv")
        self.assertIn("cd /srv/app", cmd[-1])
        self.assertIn("python", cmd[-1])

    def test_selection_ssh_with_host(self):
        from backends import SSHRunner
        self.assertIsInstance(get_backend("ssh", host="h"), SSHRunner)

    def test_selection_ssh_without_host_falls_back(self):
        self.assertIsInstance(get_backend("ssh"), LocalRunner)


if __name__ == "__main__":
    unittest.main(verbosity=2)
