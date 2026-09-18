"""Mini coding eval for the native agent loop (measurement, not vibes).

Runs a few real coding tasks through agent_loop on fresh temp projects, then
checks each result functionally. Prints a pass rate.

    .venv/bin/python prototype/ui/loop_eval.py [--model mimo-v2.5-free] [--provider opencode]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)
import agent_loop  # noqa: E402

TASKS = [
    {
        "name": "two-modules",
        "files": {},
        "task": ("Create strings.py with is_palindrome(s) (case-insensitive, ignores "
                 "spaces) and numbers.py with is_prime(n); each with a docstring."),
        "check": ("import strings, numbers\n"
                  "assert strings.is_palindrome('Race car') is True\n"
                  "assert strings.is_palindrome('abc') is False\n"
                  "assert numbers.is_prime(7) is True\n"
                  "assert numbers.is_prime(8) is False\n"
                  "print('ok')\n"),
    },
    {
        "name": "bugfix-div",
        "files": {"calc.py": "def div(a, b):\n    return a / b\n"},
        "task": ("Make div(a, b) in calc.py raise ValueError('cannot divide by zero') "
                 "when b is 0, without changing behaviour otherwise."),
        "check": ("from calc import div\n"
                  "assert div(6, 2) == 3\n"
                  "try:\n    div(1, 0)\nexcept ValueError:\n    print('ok')\n"
                  "else:\n    raise AssertionError('no ValueError')\n"),
    },
    {
        "name": "slugify",
        "files": {},
        "task": ("Create utils.py with slugify(text): lowercase, collapse any run of "
                 "non-alphanumeric characters to a single '-', and strip leading/"
                 "trailing '-'. Include a docstring."),
        "check": ("from utils import slugify\n"
                  "assert slugify('Hello World!') == 'hello-world'\n"
                  "assert slugify('  A  B ') == 'a-b'\n"
                  "print('ok')\n"),
    },
    {
        "name": "extend-class",
        "files": {"bag.py": (
            "class Bag:\n"
            "    def __init__(self):\n"
            "        self._items = []\n\n"
            "    def add(self, x):\n"
            "        self._items.append(x)\n\n"
            "    def size(self):\n"
            "        return len(self._items)\n")},
        "task": ("Add a method peek(self) to class Bag in bag.py that returns the "
                 "most recently added item WITHOUT removing it, or None if the bag "
                 "is empty. Include a docstring."),
        "check": ("from bag import Bag\n"
                  "b = Bag()\n"
                  "assert b.peek() is None\n"
                  "b.add(1); b.add(2)\n"
                  "assert b.peek() == 2 and b.size() == 2\n"
                  "print('ok')\n"),
    },
    {
        "name": "refactor-cross-file",
        "files": {
            "mathlib.py": ("def add(a, b):\n    return a + b\n\n"
                           "def sub(a, b):\n    return a - b\n"),
            "app.py": ("from mathlib import add, sub\n\n"
                       "print(add(2, 3) + sub(5, 1))\n"),
        },
        "task": ("Rename the function sub() to subtract() in mathlib.py and update "
                 "its use in app.py. Then run app.py to confirm it still prints 9."),
        "check": ("import subprocess, sys\n"
                  "out = subprocess.run([sys.executable, 'app.py'], "
                  "capture_output=True, text=True).stdout.strip()\n"
                  "assert out == '9', out\nprint('ok')\n"),
    },
    {
        "name": "bug-hunt-median",
        "files": {
            "stats.py": ("def median(xs):\n    xs = sorted(xs)\n    n = len(xs)\n"
                         "    return xs[n // 2]\n"),
            "test_stats.py": ("from stats import median\n"
                              "assert median([1, 2, 3]) == 2\n"
                              "assert median([1, 2, 3, 4]) == 2.5, "
                              "median([1, 2, 3, 4])\nprint('ok')\n"),
        },
        "task": ("Run test_stats.py — it fails. Fix median() in stats.py so the test "
                 "passes: for an even-length list the median is the average of the "
                 "two middle values. Do not change the test."),
        "check": ("import subprocess, sys\n"
                  "r = subprocess.run([sys.executable, 'test_stats.py'], "
                  "capture_output=True, text=True)\n"
                  "assert r.returncode == 0, r.stderr\nprint('ok')\n"),
    },
]


def _run_one(spec: dict, model: str, provider: str) -> dict:
    d = Path(tempfile.mkdtemp(prefix=f"asha-eval-{spec['name']}-"))
    for name, content in spec.get("files", {}).items():
        (d / name).write_text(content, encoding="utf-8")
    t0 = time.time()
    try:
        out = agent_loop.run_task(spec["task"], str(d), provider_id=provider,
                                  model=model)
    except Exception as e:  # noqa: BLE001
        return {"name": spec["name"], "pass": False, "error": str(e),
                "seconds": round(time.time() - t0, 1)}
    check = subprocess.run([sys.executable, "-c", spec["check"]], cwd=str(d),
                           capture_output=True, text=True, timeout=60)
    return {
        "name": spec["name"],
        "pass": check.returncode == 0,
        "wrote": out.get("wrote"),
        "steps": out.get("steps"),
        "seconds": round(time.time() - t0, 1),
        "detail": (check.stdout + check.stderr).strip()[-200:],
        "dir": str(d),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="mimo-v2.5-free")
    ap.add_argument("--provider", default="opencode")
    ap.add_argument("--tasks", type=int, default=len(TASKS))
    a = ap.parse_args()
    recs = []
    for spec in TASKS[: max(1, a.tasks)]:
        print(f"[eval] {spec['name']} …", flush=True)
        r = _run_one(spec, a.model, a.provider)
        recs.append(r)
        print(f"[eval] {spec['name']}: {'PASS' if r['pass'] else 'FAIL'} "
              f"({r.get('seconds')}s, steps={r.get('steps')}, wrote={r.get('wrote')})",
              flush=True)
    p = sum(1 for r in recs if r["pass"])
    print(f"\n[eval] PASS RATE {p}/{len(recs)} = {p / len(recs):.0%}")
    return 0 if p == len(recs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
