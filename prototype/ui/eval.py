"""Jarvis coding eval harness (P1) — the evidence for coding reliability.

Runs a battery of real, independent coding tasks through the orchestrator on a
fresh scratch git project each, applies the verified diffs, then checks the
result functionally. Prints a pass rate and writes prototype/data/eval-results.json.

Usage:
    .venv/bin/python prototype/ui/eval.py [--tasks N] [--keep]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import Orchestrator  # noqa: E402

RESULTS_PATH = Path(__file__).resolve().parent.parent / "data" / "eval-results.json"

TASKS = [
    {
        "name": "two-modules",
        "files": {"README.md": "# eval project\n"},
        "request": (
            "Add two independent modules to this project: strings.py with "
            "is_palindrome(s) (case-insensitive, ignores spaces) and numbers.py "
            "with is_prime(n). Each function must have a docstring."
        ),
        "check": (
            "import strings, numbers\n"
            "assert strings.is_palindrome('Race car') is True\n"
            "assert strings.is_palindrome('abc') is False\n"
            "assert numbers.is_prime(7) is True\n"
            "assert numbers.is_prime(8) is False\n"
            "print('ok')\n"
        ),
    },
    {
        "name": "bugfix-div",
        "files": {
            "calc.py": "def div(a, b):\n    return a / b\n",
            "test_calc.py": (
                "from calc import div\n\n\n"
                "def test_div_zero():\n"
                "    try:\n"
                "        div(1, 0)\n"
                "    except ValueError:\n"
                "        return\n"
                "    raise AssertionError('expected ValueError')\n"
            ),
        },
        "request": (
            "Fix div(a, b) in calc.py so that dividing by zero raises ValueError "
            "instead of ZeroDivisionError. Do not change test_calc.py."
        ),
        "check": "import test_calc\ntest_calc.test_div_zero()\nprint('fixed ok')\n",
    },
    {
        "name": "wordcount",
        "files": {"README.md": "# eval project\n"},
        "request": (
            "Add words.py with word_count(text) that returns a dict mapping each "
            "lowercased whitespace-separated word to its count. Include a docstring."
        ),
        "check": (
            "import words\n"
            "assert words.word_count('a A b') == {'a': 2, 'b': 1}\n"
            "print('ok')\n"
        ),
    },
]


def _write_files(root: Path, files: dict) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _git_init(root: Path) -> None:
    for cmd in (["init", "-q"],
                ["config", "user.email", "eval@asha.local"],
                ["config", "user.name", "Jarvis Eval"]):
        subprocess.run(["git", "-C", str(root), *cmd], capture_output=True, text=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], capture_output=True, text=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"],
                   capture_output=True, text=True)


def _check(root: Path, snippet: str) -> tuple[bool, str]:
    p = subprocess.run([sys.executable, "-c", snippet], cwd=str(root),
                       capture_output=True, text=True, timeout=120)
    out = (p.stdout + p.stderr).strip()
    return p.returncode == 0, (out[-400:] if out else "")


def run_one(spec: dict, workdir: Path) -> dict:
    root = workdir / spec["name"]
    root.mkdir(parents=True, exist_ok=True)
    _write_files(root, spec.get("files", {}))
    _git_init(root)
    t0 = time.time()
    try:
        out = Orchestrator(str(root)).run(spec["request"], apply=True)
    except Exception as e:  # noqa: BLE001
        return {"name": spec["name"], "pass": False, "error": str(e),
                "seconds": round(time.time() - t0, 1)}
    results = out.get("results", [])
    applied = all(r.get("status") == "completed" and r.get("verdict") == "accept"
                  and r.get("applied") for r in results) if results else False
    check_ok, check_out = _check(root, spec.get("check", "print('ok')"))
    passed = bool(out.get("isolated")) and not out.get("conflicts") and applied and check_ok
    return {
        "name": spec["name"],
        "pass": passed,
        "isolated": out.get("isolated"),
        "conflicts": len(out.get("conflicts") or []),
        "tasks": len(results),
        "applied": applied,
        "check_ok": check_ok,
        "check": check_out,
        "reworks": sum(r.get("reworks", 0) for r in results),
        "seconds": round(time.time() - t0, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Jarvis coding eval harness")
    ap.add_argument("--tasks", type=int, default=int(os.environ.get("EVAL_TASKS", "2")))
    ap.add_argument("--keep", action="store_true", help="keep scratch projects")
    args = ap.parse_args()

    selected = TASKS[: max(1, args.tasks)]
    workdir = Path(tempfile.mkdtemp(prefix="asha-eval-"))
    records = []
    try:
        for spec in selected:
            print(f"[eval] running {spec['name']} ...", flush=True)
            rec = run_one(spec, workdir)
            records.append(rec)
            print(f"[eval] {spec['name']}: {'PASS' if rec['pass'] else 'FAIL'} "
                  f"({rec.get('seconds')}s, reworks={rec.get('reworks')})", flush=True)
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)

    passes = sum(1 for r in records if r["pass"])
    rate = passes / len(records) if records else 0.0
    summary = {"passes": passes, "total": len(records), "pass_rate": round(rate, 3),
               "records": records, "when": time.strftime("%Y-%m-%dT%H:%M:%S")}
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n[eval] PASS RATE {passes}/{len(records)} = {rate:.0%}  "
          f"(written to {RESULTS_PATH})")
    return 0 if passes == len(records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
