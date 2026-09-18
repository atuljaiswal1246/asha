#!/usr/bin/env python3
"""Jarvis orchestrator — the brain plans, muscles execute via the opencode CLI.

Locked-in rule (lessons from KB-06 + the auth.json incident):
  * Workers run through the installed ``opencode`` CLI (auth.json + the
    validated client headers). NEVER hand-roll HTTP to opencode.ai/zen|go —
    that is what tripped the abuse filter (429s) and got Go to email us.
  * ``OPENCODE_API_KEY`` is stripped from the child env so it can never shadow
    auth.json (which yields 401s).

Each task runs in its own git worktree (isolation) so parallel workers cannot
clobber one another; the diff is kept for review (rule 9 — always verify).
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import backends  # G1: local/docker execution backends

ROOT = Path(__file__).resolve().parents[2]
OPENCODE = shutil.which("opencode") or "/opt/homebrew/bin/opencode"
WT_BASE = Path(tempfile.gettempdir()) / "opencode" / "orchestrator"

MAX_CONCURRENCY = int(os.environ.get("ORCH_MAX_CONCURRENCY", "3"))
BRAIN_MODEL = os.environ.get("ORCH_BRAIN_MODEL", "opencode-go/deepseek-v4.1-flash")
# Approved model policy (user directive 2026-09-13):
#   free zen = models with 'free' in the name, plus the big-pickle exception.
#   budget go = ONLY mimo-v2.5 and deepseek-v4.1-flash.
WORKER_MODELS = [
    "opencode/big-pickle",                    # Kiran (free exception)
    "opencode/mimo-v2.5-free",                # Ravi
    "opencode/muse-spark-1.3-contributor-free",  # Tara (exact id)
]
GO_MODELS = ["opencode-go/mimo-v2.5", "opencode-go/deepseek-v4.1-flash"]
ALLOWED_MODELS = set(WORKER_MODELS) | set(GO_MODELS)
DEFAULT_MODEL = os.environ.get("ORCH_MODEL", WORKER_MODELS[0])
# Escalate a failed free worker to a budget go model (user-approved, on failure only).
ESCALATE = os.environ.get("ORCH_ESCALATE", "1") == "1"
ESCALATE_MODEL = os.environ.get("ORCH_ESCALATE_MODEL", "opencode-go/mimo-v2.5")
TASK_TIMEOUT = float(os.environ.get("ORCH_TASK_TIMEOUT", "600"))
PLAN_TIMEOUT = float(os.environ.get("ORCH_PLAN_TIMEOUT", "180"))
# B1.2 depth cap: one level of delegation (opencode's task depth cap is 1).
MAX_DEPTH = 1
# B3.2/P5 bounded rework: rejected-for-rework tasks retry up to this many rounds.
REWORK_LIMIT = int(os.environ.get("ORCH_REWORK_LIMIT", "2"))

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _env() -> dict:
    e = dict(os.environ)
    e.pop("OPENCODE_API_KEY", None)  # never shadow auth.json
    e["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + e.get("PATH", "")
    return e


def _clean(out: str) -> str:
    return _ANSI.sub("", out or "").strip()


def _json_blob(text: str):
    """First balanced JSON object in model output (tolerates prose around it)."""
    s = text.find("{")
    if s < 0:
        return None
    depth = 0
    for i in range(s, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[s:i + 1])
                except ValueError:
                    return None
    return None


class Orchestrator:
    """Plan a request into independent tasks, run each via the opencode CLI in
    an isolated worktree, then synthesize the results."""

    def __init__(self, project: str, *, brain_model: str = BRAIN_MODEL,
                 model: str = DEFAULT_MODEL, backend=None):
        self.project = Path(project).expanduser().resolve()
        self.brain_model = brain_model
        self.model = model
        # G1: where worker commands run (local host, or docker when available).
        self._backend = backend or backends.get_backend(
            os.environ.get("ORCH_BACKEND", "local")
        )
        # Per-run namespace so concurrent orchestrators never share worktrees.
        self._runid = uuid.uuid4().hex[:8]
        self._wt_dir = WT_BASE / f"{self.project.name}-{self._runid}"
        self._conflicts: list[dict] = []
        self._cancel = None

    # ── muscle ───────────────────────────────────────────────────────────────
    def _is_git(self) -> bool:
        r = subprocess.run(["git", "-C", str(self.project), "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True)
        return r.returncode == 0 and r.stdout.strip() == "true"

    def _git_clean(self) -> bool:
        """True only when a git project has no uncommitted/untracked changes.

        Worktrees branch from HEAD, so a dirty tree's work would be missing in
        the isolated copy — and applying results back could conflict. We only
        isolate when clean; otherwise we run in-place (sequentially)."""
        if not self._is_git():
            return False
        r = subprocess.run(["git", "-C", str(self.project), "status", "--porcelain"],
                           capture_output=True, text=True)
        return r.returncode == 0 and not r.stdout.strip()

    def _worktree(self, name: str) -> Path:
        if not self._is_git():
            return self.project  # not a repo → run in place (no isolation)
        self._wt_dir.mkdir(parents=True, exist_ok=True)
        wt = self._wt_dir / name
        branch = f"orch/{self._runid}-{name}"
        subprocess.run(["git", "-C", str(self.project), "worktree", "remove", "--force", str(wt)],
                       capture_output=True, text=True)
        shutil.rmtree(wt, ignore_errors=True)
        subprocess.run(["git", "-C", str(self.project), "worktree", "add", "-B", branch, str(wt), "HEAD"],
                       capture_output=True, text=True, timeout=90)
        return wt

    def _opencode(self, cwd: Path, model: str, prompt: str, *, variant: str = "",
                  auto: bool = True, timeout: float = TASK_TIMEOUT,
                  retries: int = 0, backoff: float = 2.0, cancel=None) -> dict:
        args = [OPENCODE, "run", "--dir", str(cwd), "-m", model, "--format", "default"]
        if variant:
            args += ["--variant", variant]
        if auto:
            args += ["--auto"]
        args += [prompt]
        t0 = time.monotonic()
        attempt = 0
        while True:
            if cancel is not None and cancel.is_set():
                return {"ok": False, "text": "", "error": "cancelled",
                        "seconds": time.monotonic() - t0, "attempts": attempt + 1}
            ok, text, err = False, "", ""
            rb = self._backend.run(args, cwd=cwd, timeout=timeout,
                                   cancel=cancel, env=_env())
            ok = rb["ok"]
            text = _clean(rb.get("stdout", ""))
            err = _clean(rb.get("stderr", ""))[:400]
            if err == "cancelled":
                return {"ok": False, "text": "", "error": "cancelled",
                        "seconds": time.monotonic() - t0, "attempts": attempt + 1}
            if ok or attempt >= retries:
                return {"ok": ok, "text": text, "error": err,
                        "seconds": time.monotonic() - t0, "attempts": attempt + 1}
            # Transient failure → back off and retry (longer for rate limits).
            rate_limited = "429" in err or "rate" in err.lower()
            wait = backoff * (2 ** attempt) + random.uniform(0, 0.5)
            if rate_limited:
                wait = max(wait, 5.0)
            time.sleep(wait)
            attempt += 1

    def _diff(self, wt: Path) -> str:
        if not self._is_git():
            return ""
        subprocess.run(["git", "-C", str(wt), "add", "-A"], capture_output=True, text=True)
        s = subprocess.run(["git", "-C", str(wt), "diff", "--cached"], capture_output=True, text=True)
        return s.stdout or ""

    def run_task(self, task: dict) -> dict:
        i = task["_i"]
        goal = task.get("goal", "")
        model = task.get("model") or self.model
        variant = task.get("variant", "")
        wt = Path(task["_wt"]) if task.get("_wt") else self._worktree(f"task{i}")
        isolated = (wt != self.project)
        res = self._opencode(wt, model, goal, variant=variant, retries=2,
                             cancel=getattr(self, "_cancel", None))
        escalated = False
        if not res["ok"] and ESCALATE and model in WORKER_MODELS:
            # Free worker failed → one budget-go retry (user-approved, on failure).
            res = self._opencode(wt, ESCALATE_MODEL, goal, variant=variant)
            model, escalated = ESCALATE_MODEL, True

        # B3.2: verify the diff against the goal (+ explicit acceptance check),
        # then bounded multi-round rework until accepted.
        diff = self._diff(wt) if isolated else ""
        acceptance = (task.get("done_when") or "").strip()
        review_goal = goal + (f"\nACCEPTANCE: {acceptance}" if acceptance else "")
        verify_cmd = (task.get("verify_cmd") or "").strip()
        verdict = {"verdict": "accept", "issues": [], "summary": ""}
        exec_note = ""
        reworks = 0

        def _acceptance() -> dict | None:
            """Run the task's verify_cmd in the worktree; a failure is evidence."""
            nonlocal exec_note
            if not verify_cmd or not diff or not res["ok"]:
                return None
            rb = self._backend.run(["bash", "-lc", verify_cmd], cwd=wt,
                                   timeout=180, env=_env())
            exec_note = ((rb.get("stdout") or "") + (rb.get("stderr") or ""))[-400:]
            if rb["ok"]:
                return None
            return {"verdict": "rework",
                    "issues": [f"acceptance check failed: {exec_note[-300:]}"],
                    "summary": "the verify command failed"}

        if isolated and diff and res["ok"]:
            verdict = _acceptance() or self.verify(review_goal, diff)
            while (verdict["verdict"] == "rework"
                   and reworks < REWORK_LIMIT
                   and res["ok"]):
                reworks += 1
                fb = self._rework_brief(verdict)
                res2 = self._opencode(wt, model, fb, variant=variant)
                if res2["ok"]:
                    res = res2
                diff = self._diff(wt)
                if diff:
                    verdict = _acceptance() or self.verify(review_goal, diff)

        status = "completed" if res["ok"] else "failed"
        return {
            "task_index": i,
            "goal": goal,
            "model": model,
            "escalated": escalated,
            "status": status,
            "exit_reason": ("ok" if res["ok"] else (res.get("error") or "error"))[:120],
            "verdict": verdict.get("verdict", "accept"),
            "issues": verdict.get("issues") or [],
            "reworks": reworks,
            "verify_cmd": verify_cmd,
            "verify_out": exec_note,
            "summary": res["text"][-1500:],
            "error": res["error"],
            "seconds": round(res["seconds"], 1),
            "worktree": str(wt),
            "diff": diff,
        }

    def verify(self, goal: str, diff: str) -> dict:
        """Brain reviews one worker's diff against its brief (B3.2).

        Returns a fixed schema ``{verdict, issues, summary}`` where verdict is
        ``accept`` (satisfies the brief), ``rework`` (fixable), or ``reject``
        (wrong/dangerous). Fail-soft: an unparseable review means accept.
        """
        if not (diff or "").strip():
            return {"verdict": "accept", "issues": [], "summary": "no changes"}
        prompt = (
            "You review a coding agent's work against its brief. The agent saw "
            "only the brief; you are the reviewer.\n"
            f"BRIEF: {goal}\n\nDIFF:\n{diff[:12000]}\n\n"
            "Reply with STRICT JSON only, no prose:\n"
            '{"verdict":"accept"|"rework"|"reject","issues":["..."],"summary":"..."}\n'
            "accept = satisfies the brief; rework = fixable problems; "
            "reject = wrong or dangerous (e.g. deletes unrelated files, wrong scope)."
        )
        res = self._opencode(self.project, self.brain_model, prompt,
                             auto=False, timeout=PLAN_TIMEOUT)
        j = _json_blob(res["text"]) or {}
        v = str(j.get("verdict", "accept")).strip().lower()
        if v not in ("accept", "rework", "reject"):
            v = "accept"
        issues = j.get("issues") or []
        if not isinstance(issues, list):
            issues = [str(issues)]
        return {
            "verdict": v,
            "issues": [str(x) for x in issues][:8],
            "summary": str(j.get("summary") or "")[:400],
        }

    @staticmethod
    def _rework_brief(verdict: dict) -> str:
        issues = verdict.get("issues") or []
        issues_txt = "\n".join(f"- {i}" for i in issues) or "- (no details)"
        return (
            "A reviewer checked your changes against the brief and requests "
            f"changes (one revision round only).\n{verdict.get('summary', '')}\n"
            f"Issues:\n{issues_txt}\n"
            "Revise the edits to address every issue, then summarize. Do not commit."
        )

    # ── brain ────────────────────────────────────────────────────────────────
    def plan(self, request: str, workers: list[dict] | None = None) -> dict:
        if workers:
            roster = "\n".join(
                f"{w.get('index', 0) + 1}. {w.get('name', 'Worker')} — project "
                f"{w.get('project_name') or w.get('project', '')} — model "
                f"{w.get('model', '')}"
                for w in workers
            )
        else:
            roster = "\n".join(f"{i+1}. {m}" for i, m in enumerate(WORKER_MODELS))
        prompt = (
            "You plan parallel coding tasks for a team of worker agents.\n"
            f"REQUEST: {request}\n\nWORKERS (use each worker's model):\n{roster}\n\n"
            f"Allowed models ONLY: {', '.join(WORKER_MODELS + GO_MODELS)}.\n"
            "Reply with STRICT JSON only, no prose:\n"
            '{"tasks":[{"worker":<1-based number>,"goal":"<self-contained brief>",'
            '"done_when":"<how a reviewer judges it done>",'
            '"verify_cmd":"<shell command run in the project that exits 0 when done>",'
            '"model":"<the worker\'s model>"}]}\n'
            "Split into 1-4 INDEPENDENT, non-overlapping tasks. Each goal must be "
            "self-contained (the worker sees only the goal); done_when states the "
            "acceptance check. ONLY create/modify the files named in the goal — "
            "never add extra files (.gitignore, __init__.py, config). Prefer few, "
            "independent tasks."
        )
        res = self._opencode(self.project, self.brain_model, prompt, auto=False, timeout=PLAN_TIMEOUT)
        tasks = (_json_blob(res["text"]) or {}).get("tasks") or []
        for i, t in enumerate(tasks):
            t["_i"] = i
            # Enforce the approved model policy: the brain cannot pick a costly model.
            w = int(t.get("worker", 1) or 1) - 1
            m = (t.get("model") or "").strip()
            t["model"] = m if m in ALLOWED_MODELS else WORKER_MODELS[w % len(WORKER_MODELS)]
        return {"tasks": tasks, "raw": res["text"], "model": self.brain_model}

    def run(self, request: str, workers: list[dict] | None = None, apply: bool = False,
            cancel=None) -> dict:
        self._cancel = cancel
        plan = self.plan(request, workers)
        tasks = plan["tasks"]
        isolated = self._is_git() and self._git_clean()
        results: list[dict] = []
        if cancel is not None and cancel.is_set():
            return {"plan": plan, "results": [], "isolated": isolated,
                    "conflicts": [], "cancelled": True,
                    "summary": "Cancelled before running."}
        if isolated:
            # Pre-create worktrees SERIALLY (concurrent `git worktree add` races).
            for t in tasks:
                t["_wt"] = str(self._worktree(f"task{t['_i']}"))
            with cf.ThreadPoolExecutor(max_workers=min(MAX_CONCURRENCY, len(tasks))) as ex:
                results = list(ex.map(self.run_task, tasks))
            cancelled = cancel is not None and cancel.is_set()
            if apply and not cancelled:
                # B3.2/B5.2: never apply a rejected diff; keep any worktree whose
                # diff failed to apply (conflict) so it can be resolved by hand.
                for r in results:
                    if not self.should_apply(r):
                        r["applied"] = False
                        r["apply_error"] = f"{r.get('verdict', 'rework')} by review"
                        continue
                    r["applied"] = self._apply_diff(r)
                conflicts = [r for r in results
                             if r.get("diff") and not r.get("applied")]
                if conflicts:
                    for r in conflicts:
                        r["worktree_kept"] = True
                    self._conflicts = [
                        {"task_index": r["task_index"],
                         "worktree": r["worktree"],
                         "error": r.get("apply_error", "")}
                        for r in conflicts
                    ]
                else:
                    self._conflicts = []
                    self.cleanup()
        else:
            # Dirty or non-git project: run SEQUENTIALLY in place (no isolation,
            # no clobber). Changes land directly in the project.
            for t in tasks:
                t["_wt"] = str(self.project)
                results.append(self.run_task(t))
        return {"plan": plan, "results": results, "isolated": isolated,
                "conflicts": getattr(self, "_conflicts", []),
                "cancelled": bool(cancel is not None and cancel.is_set()),
                "summary": (self._synthesize(request, results)
                            if results else "Cancelled before running.")}

    @staticmethod
    def should_apply(result: dict) -> bool:
        """Only an accepted diff is applied; rework/reject wait for review."""
        return result.get("verdict") == "accept" and bool(result.get("diff"))

    def _apply_diff(self, r: dict) -> bool:
        diff = r.get("diff") or ""
        if not diff or not self._is_git():
            return False
        last_err = ""
        # Plain apply, then a 3-way merge fallback (independent tasks may both
        # add the same boilerplate file).
        for extra in ([], ["--3way"]):
            p = subprocess.run(
                ["git", "-C", str(self.project), "apply", "--whitespace=nowarn",
                 *extra, "-"],
                input=diff, capture_output=True, text=True, timeout=60,
            )
            if p.returncode == 0:
                return True
            last_err = p.stderr or ""
        # If the only conflict is files that already exist (a duplicate add),
        # the intended state is already on disk — treat as applied.
        new_files = []
        for block in diff.split("diff --git ")[1:]:
            if "new file mode" in block:
                head = block.splitlines()[0]
                if " b/" in head:
                    new_files.append(head.split(" b/", 1)[1])
        if new_files and all((Path(self.project) / f).exists() for f in new_files):
            return True
        r["apply_error"] = last_err[:300]
        return False

    def _synthesize(self, request: str, results: list[dict]) -> str:
        lines = [f"REQUEST: {request}", "", "WORKER RESULTS:"]
        for r in results:
            ok = "changed:" if r["diff"] else "no file changes."
            lines.append(
                f"- task {r['task_index']} ({r['model']}) [{r['status']}] "
                f"review={r.get('verdict', 'accept')} {ok} {r['summary'][:500]}"
            )
            if r.get("issues"):
                lines.append("  review issues: " + "; ".join(r["issues"][:5]))
        prompt = ("Summarize for the user, in 3-6 sentences, what was accomplished based on these "
                  "worker results. Mention any failures or rejected/needs-rework work.\n\n" + "\n".join(lines))
        res = self._opencode(self.project, self.brain_model, prompt, auto=False, timeout=PLAN_TIMEOUT)
        return res["text"]

    # ── housekeeping ─────────────────────────────────────────────────────────
    def cleanup(self):
        if self._wt_dir.exists():
            for wt in self._wt_dir.iterdir():
                subprocess.run(["git", "-C", str(self.project), "worktree", "remove", "--force", str(wt)],
                               capture_output=True, text=True)
            shutil.rmtree(self._wt_dir, ignore_errors=True)
        subprocess.run(["git", "-C", str(self.project), "worktree", "prune"], capture_output=True, text=True)
        br = subprocess.run(["git", "-C", str(self.project), "branch", "--list", f"orch/{self._runid}-*"],
                            capture_output=True, text=True).stdout.split()
        for b in br:
            subprocess.run(["git", "-C", str(self.project), "branch", "-D", b], capture_output=True, text=True)


def _main():
    import argparse
    ap = argparse.ArgumentParser(description="Jarvis orchestrator (CLI-only workers)")
    ap.add_argument("request", nargs="+")
    ap.add_argument("--project", default=str(ROOT))
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--brain", default=BRAIN_MODEL)
    ap.add_argument("--cleanup", action="store_true")
    a = ap.parse_args()
    orch = Orchestrator(a.project, brain_model=a.brain, model=a.model)
    out = orch.run(" ".join(a.request))
    print("\n=== PLAN ===")
    for t in out["plan"]["tasks"]:
        print(f"  [{t['_i']}] worker {t.get('worker')}: {t.get('goal')}  ({t.get('model')})")
    print("\n=== RESULTS ===")
    for r in out["results"]:
        print(f"  task {r['task_index']}: {r['status']} ({r['seconds']}s) worktree={r['worktree']}")
        print("    " + (r["summary"][:200].replace("\n", " ") or r["error"]))
    print("\n=== SUMMARY ===\n" + out["summary"])
    if a.cleanup:
        orch.cleanup()


if __name__ == "__main__":
    _main()
