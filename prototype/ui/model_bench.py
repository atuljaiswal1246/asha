#!/usr/bin/env python3
"""Model A/B bench — run representative tasks across models, in isolated
worktrees, and report pass/fail + time. Dev tool (drives the opencode CLI only).

Usage: .venv/bin/python prototype/ui/model_bench.py [--concurrency N]
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from orchestrator import Orchestrator, OPENCODE, WT_BASE, _env, _clean  # noqa: E402

MODELS = [
    "opencode/big-pickle",                    # free (exception)
    "opencode/mimo-v2.5-free",                # free
    "opencode/muse-spark-1.3-contributor-free",  # free
    "opencode-go/mimo-v2.5",                  # go budget
    "opencode-go/deepseek-v4.1-flash",        # go budget
]


def _run(wt: Path, cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=str(wt), capture_output=True, text=True, timeout=60)
    return p.returncode, (p.stdout + p.stderr)


def check_create(wt: Path):
    f = wt / "hello.py"
    if not f.exists():
        return False, "hello.py missing"
    ok = _run(wt, [sys.executable, "hello.py"])
    return ("Hello, Asha" in ok[1]), ok[1][:80]


def check_fizz(wt: Path):
    f = wt / "fizz.py"
    if not f.exists():
        return False, "fizz.py missing"
    rc, out = _run(wt, [sys.executable, "fizz.py"])
    expected = "\n".join(
        "FizzBuzz" if i % 15 == 0 else "Fizz" if i % 3 == 0 else "Buzz" if i % 5 == 0 else str(i)
        for i in range(1, 16)
    )
    got = out.strip()
    return (expected in got.replace("\r", "")), got[:80]


def check_selfcheck(wt: Path):
    if not (wt / "calc.py").exists() or not (wt / "check.py").exists():
        return False, "missing calc.py/check.py"
    rc, out = _run(wt, [sys.executable, "check.py"])
    return (rc == 0), (out.strip().splitlines()[-1][:80] if out.strip() else f"exit {rc}")


TASKS = [
    ("create", "Create hello.py that prints exactly: Hello, Asha", check_create),
    ("fizzbuzz", "Create fizz.py: for 1..15 print Fizz for multiples of 3, Buzz for multiples of 5, "
                 "FizzBuzz for both, else the number, one per line. Running `python fizz.py` must "
                 "print the correct sequence.", check_fizz),
    ("selfcheck", "Create calc.py with an add(a, b) function, and check.py that asserts add(2,3)==5 and "
                  "add(-1,1)==0 then prints OK. Running `python check.py` must exit 0.", check_selfcheck),
]


def _one(orch: Orchestrator, model: str, task: tuple, wt: Path) -> dict:
    name, prompt, check = task
    t0 = time.monotonic()
    res = orch._opencode(wt, model, prompt, auto=True, timeout=240)
    secs = time.monotonic() - t0
    try:
        passed, detail = check(wt)
    except Exception as e:
        passed, detail = False, f"check error: {e}"
    return {"model": model, "task": name, "passed": passed, "seconds": round(secs, 1),
            "detail": detail, "ok_run": res["ok"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=4)
    a = ap.parse_args()
    orch = Orchestrator(str(Path(__file__).resolve().parents[2]))
    # Pre-create every worktree SERIALLY (parallel `git worktree add` races).
    worktrees = {}
    for m, t in [(m, t) for m in MODELS for t in TASKS]:
        safe = m.replace("/", "_")
        worktrees[(m, t[0])] = orch._worktree(f"bench_{safe}_{t[0]}")
    pairs = [(m, t) for m in MODELS for t in TASKS]
    results = []
    with cf.ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        futs = [ex.submit(_one, orch, m, t, worktrees[(m, t[0])]) for m, t in pairs]
        for f in cf.as_completed(futs):
            r = f.result()
            results.append(r)
            print(f"  {r['model']:34s} {r['task']:9s} {'PASS' if r['passed'] else 'FAIL':4s} "
                  f"{r['seconds']:5.1f}s  {r['detail'][:50]}")
    print("\n=== SCORECARD ===")
    for m in MODELS:
        rs = [r for r in results if r["model"] == m]
        passes = sum(1 for r in rs if r["passed"])
        avg = sum(r["seconds"] for r in rs) / max(1, len(rs))
        print(f"  {m:34s} {passes}/{len(rs)} pass   avg {avg:5.1f}s")
    orch.cleanup()
    for b in subprocess.run(["git", "-C", str(orch.project), "branch", "--list", "orch/bench*"],
                            capture_output=True, text=True).stdout.split():
        subprocess.run(["git", "-C", str(orch.project), "branch", "-D", b], capture_output=True)
    subprocess.run(["git", "-C", str(orch.project), "worktree", "prune"], capture_output=True)


if __name__ == "__main__":
    main()
