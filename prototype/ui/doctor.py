"""Jarvis doctor — quick health check of the coding/runtime environment.

Run:  .venv/bin/python prototype/ui/doctor.py [--network]

Prints PASS/WARN/FAIL for Python, the venv, `.env` key NAMES (never values),
data-dir writability, the opencode/hermes CLIs, and (with --network) whether
the go gateway is reachable. Never prints secret values.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

_UI = Path(__file__).resolve().parent
_DATA = _UI.parent / "data"
_ENV = _UI.parent / ".env"

_KEY_NAMES = [
    "OPENCODE_API_KEY", "OPENCODE_API_KEY_2", "OPENCODE_API_KEY_3",
    "SUPERVISOR_API_KEY", "OPENROUTER_API_KEY", "OPENROUTER_KEY_1",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "XAI_API_KEY",
    "GROQ_API_KEY", "EXA_API_KEY", "LEONARDO_API_KEY",
]


def _load_env() -> dict:
    vals: dict = {}
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV, override=False)
    except Exception:  # noqa: BLE001
        pass
    for k in _KEY_NAMES:
        if os.environ.get(k):
            vals[k] = True
    return vals


def run(network: bool = False) -> int:
    problems = 0

    def report(ok: bool, label: str, detail: str = "", warn: bool = False):
        nonlocal problems
        tag = "PASS" if ok else ("WARN" if warn else "FAIL")
        if not ok and not warn:
            problems += 1
        print(f"[{tag}] {label}" + (f" — {detail}" if detail else ""))

    py = sys.version_info
    report(py >= (3, 11), "python", f"{py.major}.{py.minor}.{py.micro}")

    venv = _UI.parent.parent / ".venv"
    report(venv.exists(), "venv", str(venv) if venv.exists() else "missing .venv")

    env_present = _ENV.exists()
    report(env_present, ".env present", str(_ENV) if env_present else "missing",
           warn=not env_present)
    keys = _load_env()
    report(bool(keys), "env keys set (names only)",
           ", ".join(sorted(keys)) or "none")

    try:
        _DATA.mkdir(parents=True, exist_ok=True)
        probe = _DATA / ".doctor-probe"
        probe.write_text("ok")
        probe.unlink()
        report(True, "data dir writable", str(_DATA))
    except OSError as e:
        report(False, "data dir writable", repr(e))

    for cli in ("opencode", "hermes", "git"):
        p = shutil.which(cli)
        if not p and cli == "hermes":
            cand = Path.home() / ".hermes" / "venvs" / "hermes-dev" / "bin" / "hermes"
            p = str(cand) if cand.exists() else None
        report(bool(p), f"{cli} available", p or "not found", warn=(cli == "hermes"))

    if network:
        try:
            import httpx
            key = os.environ.get("SUPERVISOR_API_KEY") or os.environ.get("OPENCODE_API_KEY")
            r = httpx.get("https://opencode.ai/zen/go/v1/models",
                          headers={"Authorization": f"Bearer {key}",
                                   "x-opencode-client": "cli",
                                   "User-Agent": "opencode/1.18.29"},
                          timeout=15)
            report(r.status_code == 200, "go gateway reachable", f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            report(False, "go gateway reachable", repr(e)[:80])

    print(f"\n{'All good.' if problems == 0 else f'{problems} problem(s) found.'}")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Jarvis environment health check")
    ap.add_argument("--network", action="store_true", help="also probe the go gateway")
    raise SystemExit(run(ap.parse_args().network))
