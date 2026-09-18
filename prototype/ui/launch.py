"""Cross-platform launcher for the Jarvis app (macOS / Windows / Linux).

Starts the backend (static UI + WebSocket bot) and opens the UI. The Mac
``.app`` and the Windows shell both call this, so there is ONE start path.

  python launch.py                 start everything, open the UI
  python launch.py --no-browser    start only (the app shell embeds the UI)
  python launch.py --port 8010     use a different static port

Requires the project venv (``.venv`` at the repo root) or the current Python
already having ``prototype/requirements.txt`` installed.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

UI_DIR = Path(__file__).resolve().parent
REPO = UI_DIR.parent.parent


def _python() -> str:
    """Prefer the repo venv's interpreter; else the current one."""
    for cand in (REPO / ".venv" / "bin" / "python",
                 REPO / ".venv" / "Scripts" / "python.exe"):
        if cand.exists():
            return str(cand)
    return sys.executable


def _wait_http(url: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status < 500:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Launch the Jarvis app")
    ap.add_argument("--port", type=int, default=int(os.environ.get("JARVIS_UI_PORT", "8000")))
    ap.add_argument("--host", default=os.environ.get("JARVIS_HOST", "127.0.0.1"),
                    help="bind host (use 0.0.0.0 in a container/VPS)")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    env = dict(os.environ)
    env.setdefault("WS_HOST", args.host)  # WS bot binds the same host
    py = _python()
    procs: list[subprocess.Popen] = []

    def _spawn(cmd: list[str]) -> subprocess.Popen:
        creationflags = 0x00000008 if os.name == "nt" else 0  # DETACHED_PROCESS
        return subprocess.Popen(cmd, cwd=str(UI_DIR), env=env,
                                creationflags=creationflags)

    print(f"[launch] python: {py}")
    print("[launch] starting backend…")
    procs.append(_spawn([py, "-u", "server.py"]))

    print(f"[launch] starting static UI on {args.host}:{args.port}")
    procs.append(_spawn([py, "-m", "http.server", str(args.port),
                         "--bind", args.host, "--directory", "static"]))

    url = f"http://127.0.0.1:{args.port}/"
    ok = _wait_http(url, 30)
    print(f"[launch] UI {'ready' if ok else 'not responding yet'}: {url}")

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    try:
        for p in procs:
            p.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            if p.poll() is None:
                try:
                    if os.name == "nt":
                        p.terminate()
                    else:
                        p.send_signal(signal.SIGTERM)
                except Exception:  # noqa: BLE001
                    pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
