"""Execution backends for worker commands (G1).

A backend decides *where* a command runs: on the host (``local``) or inside a
container (``docker``) for isolation. Select with ``ORCH_BACKEND=local|docker|
auto`` (``auto`` = docker when installed, else local).

Docker is optional: when it isn't installed the orchestrator stays on the
local backend, so behavior is unchanged by default.
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path


def docker_available() -> bool:
    return shutil.which("docker") is not None


class LocalRunner:
    """Run a command on the host (the historical behavior)."""

    name = "local"

    def run(self, argv: list[str], cwd=None, timeout: float = 600,
            cancel: threading.Event | None = None, env=None) -> dict:
        t0 = time.monotonic()
        cwd_s = str(cwd) if cwd else None
        if cancel is None:
            try:
                p = subprocess.run(argv, cwd=cwd_s, capture_output=True,
                                   text=True, env=env, timeout=timeout)
                return {"ok": p.returncode == 0, "stdout": p.stdout,
                        "stderr": p.stderr, "seconds": time.monotonic() - t0}
            except subprocess.TimeoutExpired:
                return {"ok": False, "stdout": "", "stderr": "timeout",
                        "seconds": time.monotonic() - t0}
        proc = subprocess.Popen(argv, cwd=cwd_s, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env)
        deadline = time.monotonic() + timeout
        while True:
            if cancel.is_set():
                proc.kill()
                proc.communicate()
                return {"ok": False, "stdout": "", "stderr": "cancelled",
                        "seconds": time.monotonic() - t0}
            if proc.poll() is not None:
                out, err = proc.communicate()
                return {"ok": proc.returncode == 0, "stdout": out, "stderr": err,
                        "seconds": time.monotonic() - t0}
            if time.monotonic() > deadline:
                proc.kill()
                proc.communicate()
                return {"ok": False, "stdout": "", "stderr": "timeout",
                        "seconds": time.monotonic() - t0}
            time.sleep(0.3)


class DockerRunner:
    """Run a command inside a container with the project mounted at /work."""

    name = "docker"

    def __init__(self, image: str = "python:3.12-slim", network: bool = True,
                 extra_args: list[str] | None = None):
        self.image = image
        self.network = network
        self.extra_args = list(extra_args or [])

    def command(self, argv: list[str], cwd) -> list[str]:
        return [
            "docker", "run", "--rm", "-i",
            "--network", "bridge" if self.network else "none",
            "-v", f"{Path(cwd).resolve()}:/work",
            "-w", "/work",
            *self.extra_args,
            self.image,
            *argv,
        ]

    def run(self, argv: list[str], cwd=None, timeout: float = 600,
            cancel: threading.Event | None = None, env=None) -> dict:
        cmd = self.command(argv, cwd)
        return LocalRunner().run(cmd, cwd=None, timeout=timeout,
                                 cancel=cancel, env=env)


def get_backend(name: str = "auto", **kw):
    """Resolve a backend by name; ``auto`` prefers docker when available."""
    name = (name or "auto").strip().lower()
    if name == "auto":
        name = "docker" if docker_available() else "local"
    if name == "docker" and docker_available():
        return DockerRunner(**kw)
    if name == "ssh" and kw.get("host"):
        return SSHRunner(**kw)
    return LocalRunner()


class SSHRunner:
    """Run a command on a remote host over ``ssh`` (P7).

    The command is wrapped as ``cd <remote_root> && <argv>`` and every token is
    shell-quoted; ``local`` execution happens on the remote host.
    """

    name = "ssh"

    def __init__(self, host: str, user: str = "", port: int | None = None,
                 remote_root: str = ".", extra_args: list[str] | None = None):
        self.host = host
        self.user = user
        self.port = port
        self.remote_root = remote_root or "."
        self.extra_args = list(extra_args or [])

    def command(self, argv: list[str], cwd=None) -> list[str]:
        target = f"{self.user}@{self.host}" if self.user else self.host
        remote = " ".join(shlex.quote(a) for a in argv)
        remote_cmd = f"cd {shlex.quote(self.remote_root)} && {remote}"
        args = ["ssh"]
        if self.port:
            args += ["-p", str(self.port)]
        args += list(self.extra_args) + [target, remote_cmd]
        return args

    def run(self, argv: list[str], cwd=None, timeout: float = 600,
            cancel: threading.Event | None = None, env=None) -> dict:
        return LocalRunner().run(self.command(argv, cwd), cwd=None,
                                 timeout=timeout, cancel=cancel, env=env)
