"""The supervisor loop: when an agent reports back, Jarvis wakes up and checks.

This module is the answer to **"never sit idle when an agent has reported
back."** Delegated agents settle into ``done``/``failed`` and, until now,
nothing happened until the user asked. The loop watches the agent registry for
settled runs, and for each one it:

1. **FENCE**  — ``git status --short``; did the agent touch files it was not
   briefed to touch? Those are ``scope_failures`` (findings to report, never
   to hand back for a "fix").
2. **PROOF**  — run the brief's own VERIFY commands (both shell lines and
   list-command literals are understood). When the brief names none, derive an
   honest default: the touched test modules by name, else the affected area's
   full suite (``python -m unittest discover -s prototype/ui -p "test_*.py"
   -q``) plus ``ruff``. A proof that cannot be derived or parsed (or that
   cannot run) makes the check ``inconclusive`` — it is never reported as
   ``verified``.
3. **ARTIFACT** — read the run log tail and, when the brief named a report
   file, confirm it exists and is non-empty (prose is never judged).
4. **VERDICT** — ``verified`` / ``needs_fix`` / ``reported`` / ``inconclusive``.
   A failed proof or missing artifact is a ``work_failure`` → ``needs_fix``
   (rework allowed). Only out-of-scope edits → ``reported`` (no rework: an
   unrelated change is reported, not "fixed").
5. **JOURNAL** — append one JSON line per check to
   ``<data_dir>/agent-verify/journal.jsonl`` (including ``scope_failures``,
   ``work_failures`` and ``rework_allowed``).
6. **REWORK** — keyed off ``rework_allowed`` (never the verdict string) and
   fewer than two rework attempts for the same brief, write a follow-up brief
   and re-run the same agent.

The loop is pure stdlib asyncio, polls at ~15s while anything is awaiting and
~60s when nothing is (never a busy loop), runs at most ONE check at a time, and
never raises out of its body. ``tick()`` is a single, awaitable pass used by
tests; ``verify()`` is a separate synchronous function with an injectable
command runner so tests never launch real tests. A short human line per settle
is emitted through an injectable callback (default no-op) for the UI.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import agents

logger = logging.getLogger(__name__)

INCONCLUSIVE = "inconclusive"
# A scope-only finding (the agent edited files it was not briefed to touch).
# It is a finding to REPORT, never a failure to "fix"; it never re-briefs.
REPORTED = "reported"

# A VERIFY bullet is treated as a command only when it starts with one of these
# (the rest of a brief's VERIFY section is prose and is skipped).
_CMD_PREFIXES = (
    "python", "python3", "pytest", "py.test", "ruff", "npm", "npx", "node",
    "sh", "bash", "make", "cargo", "go", "deno", "bun", "uv", "bin/", "./",
)

_REPORT_RE = re.compile(r"([\w./-]+\.(?:md|txt|json|csv|log|rst))\b")


def _default_data_dir() -> Path:
    try:
        import jarvis_paths

        return Path(jarvis_paths.data_dir())
    except Exception:  # noqa: BLE001 - standalone use
        return Path(__file__).resolve().parents[1] / "data"


def _repo_root() -> str:
    """The repo this file lives in (fence/proof run here by default)."""
    return str(Path(__file__).resolve().parents[2])


def _read_text(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _default_run_cmd(cmd: list[str], cwd=None):
    """Run *cmd* and capture output; the injectable seam for tests."""
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=180)


def _section(brief: str, name: str) -> str:
    """The body of a ``## NAME`` brief section (empty when absent)."""
    m = re.search(rf"^##\s+{re.escape(name)}\s*$(.*?)(?=^##\s|\Z)",
                  brief or "", re.M | re.S)
    return m.group(1) if m else ""


def _bullets(block: str) -> list[str]:
    out = []
    for line in (block or "").splitlines():
        s = line.strip()
        if not s.startswith(("-", "*")):
            continue
        s = s.lstrip("-* ").strip().strip("`").strip()
        if s:
            out.append(s)
    return out


def _allowed_files(brief: str) -> list[str] | None:
    """Paths the brief allowed, ``[]`` for "none", or None when unknown."""
    block = _section(brief, "ALLOWED FILES")
    if not block:
        return None
    files = []
    for s in _bullets(block):
        low = s.lower()
        if low.startswith("(none") or "do not edit" in low:
            continue
        files.append(s)
    return files


def _parse_verify_bullet(s: str) -> tuple[list[str] | None, str]:
    """Parse one VERIFY bullet into ``(cmd, problem)``.

    ``(cmd, "")`` for a runnable command, ``(None, "")`` for ordinary prose,
    and ``(None, reason)`` for a bullet that looks like a command but cannot
    honestly be turned into one. Callers must surface *reason* -- an
    unparseable proof line must never be silently skipped.
    """
    text = (s or "").strip()
    if not text:
        return None, ""
    if text[0] in "[(":
        # Older briefs rendered a list command as its Python repr; parse the
        # literal back when it is safely a flat sequence of strings.
        try:
            value = ast.literal_eval(text)
        except (ValueError, SyntaxError, MemoryError):
            return None, f"unparseable verify command: {text}"
        if (isinstance(value, (list, tuple)) and value
                and all(isinstance(p, str) for p in value)):
            return [str(p) for p in value], ""
        return None, f"unparseable verify command: {text}"
    low = text.lower()
    if any(low.startswith(p) for p in _CMD_PREFIXES):
        try:
            return shlex.split(text), ""
        except ValueError:
            return None, f"unparseable verify command: {text}"
    return None, ""


def _brief_verify_commands(brief: str,
                           problems: list[str] | None = None) -> list[list[str]]:
    """Command lines from the brief's VERIFY section (prose is ignored).

    Both shapes are accepted: a shell line (``ruff check a.py``) and a Python
    list/tuple literal left by an older brief render. A bullet that looks like
    a command but cannot be parsed is appended to *problems* (when a list is
    given) instead of being dropped, so the caller can go inconclusive.
    """
    out = []
    for s in _bullets(_section(brief, "VERIFY")):
        cmd, problem = _parse_verify_bullet(s)
        if cmd:
            out.append(cmd)
        elif problem and problems is not None:
            problems.append(problem)
    return out


def _report_path(brief: str) -> str:
    """A report file path named in REPORT BACK, if any."""
    m = _REPORT_RE.search(_section(brief, "REPORT BACK") or "")
    return m.group(1) if m else ""


def _git_root(path: str) -> str:
    """The enclosing git repo of *path*, or "" when there is none.

    The fence must run in a repository: an earlier version ran it in the run's
    raw workdir and a non-repo directory made `git status` exit 128, which was
    misread as a broken fence and burned a rework attempt for nothing.
    """
    p = os.path.abspath(path or ".")
    while True:
        if os.path.isdir(os.path.join(p, ".git")):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return ""
        p = parent


def _parse_status(out: str) -> list[str]:
    """Paths from ``git status --short`` output."""
    paths = []
    for line in (out or "").splitlines():
        line = line.rstrip()
        if len(line) < 4:
            continue
        rest = line[3:]
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        rest = rest.strip().strip('"')
        if rest:
            paths.append(rest)
    return paths


_FULL_SUITE_REL = os.path.join("prototype", "ui")


def _default_proof_commands(touched: list[str],
                            repo: str = "") -> tuple[list[list[str]], str]:
    """Derive an honest default proof from the touched files.

    - Test modules touched -> run those modules by name.
    - Only other Python touched -> run the affected area's FULL unittest suite
      from the repo root (plus ruff), because a partial/stem-only run does not
      prove the change is safe.
    - If that suite is not present in this repo (so running it would prove
      nothing), no command is invented: the returned reason makes the proof
      inconclusive rather than ``verified``.

    Returns ``(commands, reason)``; *reason* is non-empty only when there is no
    honest default proof to run.
    """
    modules = sorted({
        Path(p).stem for p in touched
        if p.endswith(".py") and Path(p).name.startswith("test_")
    })
    cmds: list[list[str]] = []
    if modules:
        cmds.append([sys.executable, "-m", "unittest", *modules, "-q"])
    py_files = [p for p in touched if p.endswith(".py")]
    if py_files:
        cmds.append(["ruff", "check", *py_files])
        if not modules:
            suite_dir = os.path.join(repo or "", _FULL_SUITE_REL)
            if os.path.isdir(suite_dir):
                cmds.append([sys.executable, "-m", "unittest", "discover",
                             "-s", _FULL_SUITE_REL, "-p", "test_*.py", "-q"])
            else:
                return cmds, (
                    "no default proof: full suite "
                    f"{_FULL_SUITE_REL} not found under {repo or '(unknown)'}")
    return cmds, ""


def _fail_label(cmd: list[str]) -> str:
    joined = " ".join(cmd)
    if "-m" in cmd and "unittest" in cmd:
        return f"tests failed: {joined}"
    if cmd and cmd[0].endswith("ruff"):
        return f"ruff failed: {joined}"
    return f"command failed: {joined}"


def _journal_path(data_dir) -> Path:
    base = Path(data_dir) if data_dir else _default_data_dir()
    return base / "agent-verify" / "journal.jsonl"


def _write_journal(path: Path, row: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except OSError:
        logger.exception("[VERIFY] could not write journal %s", path)


def verify(agent: dict, run_info: dict | None = None, *,
           run_cmd=_default_run_cmd, read=_read_text,
           workdir: str | None = None, data_dir=None) -> dict:
    """Check one settled agent and journal the result. Never raises.

    The three checks are recorded in ``commands`` (with their exit codes) and
    the verdict is one of ``verified``/``needs_fix``/``reported``/
    ``inconclusive``. A journal line is appended to
    ``<data_dir>/agent-verify/journal.jsonl``.
    """
    agent = agent or {}
    run_info = run_info or {}
    aid = agent.get("id") or agent.get("agent_id") or ""
    brief_title = agent.get("brief_title", "")

    reasons: list[str] = []
    scope_failures: list[str] = []
    work_failures: list[str] = []
    scope_files: list[str] = []
    commands: list[dict] = []
    checked = False

    brief_path = run_info.get("brief_path") or ""
    log_path = run_info.get("log_path") or ""
    brief_text = read(brief_path) if brief_path else ""
    allowed = _allowed_files(brief_text) if brief_text else None
    candidate = workdir or run_info.get("workdir") or ""
    repo = _git_root(candidate) if candidate else _repo_root()
    if not repo:
        repo = _repo_root()

    # 1. FENCE -------------------------------------------------------------
    changed: list[str] = []
    fence_ran = False
    rc, out = _invoke(run_cmd, ["git", "status", "--short"], repo)
    if rc is None:
        reasons.append(f"could not run git status: {out}")
    elif rc != 0:
        # Infrastructure, not the agent's fault: an unreadable fence makes the
        # check INCONCLUSIVE so it can never trigger a rework.
        reasons.append(
            f"fence not checkable: git status exited {rc} in {repo or '?'}")
    else:
        fence_ran = True
        checked = True
        commands.append({"cmd": "git status --short", "rc": rc})
        changed = _parse_status(out)
    if allowed and fence_ran:
        violations = [p for p in changed if p not in allowed]
        if violations:
            # Rule 11: an unrelated change is REPORTED, never handed back to an
            # agent to "fix" (that risks more breakage). Each file gets its own
            # finding so the user sees exactly what moved.
            scope_files = list(violations)
            scope_failures = [
                f"out-of-scope change (report, do not fix): {p}"
                for p in violations]
    else:
        # A brief that names no files cannot be fenced: every pre-existing
        # modification in the tree (including the brain's own uncommitted work)
        # would look like a violation. Say so instead of inventing a failure.
        if not allowed:
            reasons.append(
                "allowed files not listed in the brief; fence not confirmed")

    # 2. PROOF -------------------------------------------------------------
    # A proof that could not be derived or parsed is recorded in
    # ``proof_problems`` and forces the verdict to ``inconclusive``: "we did not
    # really check" must never read as ``verified``.
    touched = changed if allowed is None else [p for p in changed if p in allowed]
    proof_problems: list[str] = []
    proof = _brief_verify_commands(brief_text, proof_problems) if brief_text else []
    if not proof and not proof_problems:
        default, default_problem = _default_proof_commands(touched, repo)
        proof = default
        if default_problem:
            proof_problems.append(default_problem)
    for cmd in proof:
        rc, out = _invoke(run_cmd, cmd, repo)
        if rc is None:
            reasons.append(f"could not run {' '.join(cmd)}: {out}")
            continue
        checked = True
        commands.append({"cmd": " ".join(cmd), "rc": rc})
        if rc != 0:
            work_failures.append(f"{_fail_label(cmd)} (exit {rc})")
    if not proof and not proof_problems and not work_failures:
        proof_problems.append(
            "no proof commands in the brief and no default proof derived")

    # 3. ARTIFACT ----------------------------------------------------------
    if log_path:
        tail = read(log_path)
        if tail.strip():
            checked = True
        else:
            reasons.append(f"agent log missing or empty: {log_path}")
    else:
        reasons.append("no agent log recorded for this run")
    report_file = _report_path(brief_text) if brief_text else ""
    if report_file:
        rp = report_file
        if not os.path.isabs(rp) and repo:
            rp = os.path.join(repo, report_file)
        if (read(rp) or "").strip():
            checked = True
        else:
            work_failures.append(f"artifact missing or empty: {report_file}")

    # 4. VERDICT -----------------------------------------------------------
    # Two kinds of finding, two reactions:
    #   work_failures  -> needs_fix (a failed proof is the agent's to fix)
    #   scope_failures -> reported (an unrelated change is reported, not fixed)
    rework_allowed = bool(work_failures)
    if work_failures:
        verdict = agents.NEEDS_FIX
        reasons = list(dict.fromkeys(work_failures + scope_failures))
    elif scope_failures:
        verdict = REPORTED
        reasons = list(dict.fromkeys(scope_failures))
    elif proof_problems:
        # The proof step could not honestly run: never call that verified.
        verdict = INCONCLUSIVE
        reasons = list(dict.fromkeys(reasons + proof_problems))
    elif not checked:
        verdict = INCONCLUSIVE
        reasons = reasons or [
            "nothing could be checked (no brief, run info or commands)"]
    else:
        verdict = agents.VERIFIED
        reasons = reasons or ["all checks passed"]

    result = {
        "verdict": verdict,
        "reasons": reasons,
        "commands": commands,
        "checked": checked,
        "agent_id": aid,
        "brief_title": brief_title,
        "allowed_files": allowed,
        "scope_failures": scope_failures,
        "work_failures": work_failures,
        "scope_files": scope_files,
        "rework_allowed": rework_allowed,
    }

    # 5. JOURNAL -----------------------------------------------------------
    journal = _journal_path(data_dir)
    _write_journal(journal, {
        "timestamp": time.time(),
        "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "agent_id": aid,
        "brief_title": brief_title,
        "verdict": verdict,
        "reasons": reasons,
        "commands": commands,
        "exit_codes": [c.get("rc") for c in commands],
        "scope_failures": scope_failures,
        "work_failures": work_failures,
        "rework_allowed": rework_allowed,
        "log_path": log_path,
        "brief_path": brief_path,
    })
    result["journal_path"] = str(journal)
    return result


def _invoke(run_cmd, cmd: list[str], cwd) -> tuple[int | None, str]:
    """Run one command, returning ``(rc, stdout)`` or ``(None, error)``."""
    try:
        res = run_cmd(cmd, cwd)
    except Exception as e:  # noqa: BLE001 - an injected runner may fail
        return None, str(e)
    rc = getattr(res, "returncode", None)
    out = getattr(res, "stdout", "") or ""
    return rc, out


def _default_runner(agent_id: str, brief: str, **kwargs):
    import agent_runner

    if not kwargs.get("workdir"):
        kwargs["workdir"] = _repo_root()
    return agent_runner.run(agent_id, brief, **kwargs)


class SupervisorLoop:
    """Background loop that verifies agents the moment they report back."""

    MAX_REWORK = 2

    def __init__(self, *, registry=None, runner=None, run_cmd=None, read=None,
                 emit=None, data_dir=None, workdir=None,
                 poll_busy: float = 15.0, poll_idle: float = 60.0):
        self._registry = registry
        self._runner = runner
        self._run_cmd = run_cmd or _default_run_cmd
        self._read = read or _read_text
        self._emit = emit
        self._data_dir = str(data_dir) if data_dir else None
        self._workdir = workdir
        self._poll_busy = poll_busy
        self._poll_idle = poll_idle
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._ticking = False
        self._lock = threading.Lock()
        self._last: list[dict] = []
        self._out_of_scope: list[dict] = []

    # -- wiring ----------------------------------------------------------
    def _reg(self):
        r = self._registry
        if r is None:
            return agents.registry
        if hasattr(r, "awaiting_check"):
            return r
        return r()

    def set_emit(self, emit) -> None:
        """Install the human-line callback (called on the event-loop thread)."""
        self._emit = emit

    def _journal_dir(self) -> Path:
        return _journal_path(self._data_dir).parent

    # -- lifecycle -------------------------------------------------------
    def start(self) -> bool:
        """Start the background task. Idempotent; True when it started now."""
        if self._task is not None and not self._task.done():
            return False
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("[VERIFY] start() needs a running event loop")
            return False
        self._wake = asyncio.Event()
        self._task = self._loop.create_task(self._run())
        logger.info("[VERIFY] supervisor loop started")
        return True

    def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
        logger.info("[VERIFY] supervisor loop stopped")

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def wake(self) -> None:
        """Nudge the loop to check now; safe to call from any thread."""
        ev = self._wake
        if ev is None:
            return
        try:
            loop = self._loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(ev.set)
            else:
                ev.set()
        except Exception:  # noqa: BLE001
            pass

    async def _run(self) -> None:
        while True:
            if self._wake is not None:
                self._wake.clear()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must never die
                logger.exception("[VERIFY] tick failed; continuing")
            busy = False
            try:
                busy = bool(self._reg().awaiting_check())
            except Exception:  # noqa: BLE001
                pass
            timeout = self._poll_busy if busy else self._poll_idle
            try:
                if self._wake is not None:
                    await asyncio.wait_for(self._wake.wait(), timeout)
                else:
                    await asyncio.sleep(timeout)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise

    # -- one pass --------------------------------------------------------
    async def tick(self) -> int:
        """Run one verification pass. Returns how many agents were checked."""
        if self._ticking:
            return 0
        self._ticking = True
        try:
            # Abandoned task rows must not accumulate: drop settled zombies.
            try:
                import tasks as tm

                tm.tasks.prune()
            except Exception:  # noqa: BLE001 - the registry is best-effort
                logger.exception("[VERIFY] task prune failed")
            try:
                pending = list(self._reg().awaiting_check())
            except Exception:  # noqa: BLE001
                logger.exception("[VERIFY] could not read the registry")
                return 0
            processed = 0
            for agent in pending:
                try:
                    result = await asyncio.to_thread(self._process, agent)
                except Exception:  # noqa: BLE001
                    logger.exception("[VERIFY] check crashed for %s",
                                     agent.get("id"))
                    continue
                processed += 1
                line = result.get("line")
                if line and self._emit:
                    try:
                        self._emit(line)
                    except Exception:  # noqa: BLE001
                        pass
            return processed
        finally:
            self._ticking = False

    # -- per-agent (runs in a worker thread) -----------------------------
    def _process(self, agent: dict) -> dict:
        reg = self._reg()
        aid = agent.get("id") or ""
        name = agent.get("name") or aid
        brief_title = agent.get("brief_title", "")
        run_info = self._run_info(aid)

        # Claim it first so a concurrent pass cannot pick the same run twice.
        reg.set_status(aid, agents.VERIFYING, note="checking work")
        self._task_update(agent.get("task_id") or "", status="verifying")
        prior = self._rework_attempts(aid, brief_title)
        try:
            verdict = verify(agent, run_info, run_cmd=self._run_cmd,
                             read=self._read, workdir=self._workdir,
                             data_dir=self._data_dir)
        except Exception as e:  # noqa: BLE001 - verify should not raise, guard anyway
            verdict = {"verdict": INCONCLUSIVE,
                       "reasons": [f"verify crashed: {e}"],
                       "commands": [], "checked": False,
                       "agent_id": aid, "brief_title": brief_title,
                       "allowed_files": None}
            _write_journal(_journal_path(self._data_dir), {
                "timestamp": time.time(), "agent_id": aid,
                "brief_title": brief_title, "verdict": INCONCLUSIVE,
                "reasons": verdict["reasons"], "commands": [],
                "exit_codes": [], "log_path": (run_info or {}).get("log_path", ""),
                "brief_path": (run_info or {}).get("brief_path", ""),
            })

        v = verdict.get("verdict")
        reason = (verdict.get("reasons") or ["no reason recorded"])[0]
        # The rework decision keys off this flag, never the verdict string: a
        # scope-only finding is "reported" and must never be re-briefed.
        rework_allowed = bool(verdict.get("rework_allowed",
                                          v == agents.NEEDS_FIX))
        if v == agents.VERIFIED:
            reg.set_status(aid, agents.VERIFIED, note="verified")
            line = f"{name}: work verified"
        elif v == REPORTED:
            findings = verdict.get("scope_failures") or [reason]
            reg.set_status(aid, agent.get("status") or agents.DONE,
                           note="; ".join(findings))
            line = f"{name}: reported — " + "; ".join(findings)
        elif v == agents.NEEDS_FIX:
            reg.set_status(aid, agents.NEEDS_FIX, note=reason)
            if rework_allowed and prior < self.MAX_REWORK:
                self._rework(agent, verdict)
                line = f"{name}: needs a fix — re-briefed ({reason})"
            else:
                line = f"{name}: needs a fix — rework cap reached ({reason})"
        else:
            reg.set_status(aid, agent.get("status") or agents.DONE,
                           note="check inconclusive")
            line = f"{name}: could not verify — {reason}"

        # Keep the live task registry current: a verified task is removed the
        # moment it is verified (complete + verified = gone); needs_fix/reported
        # stay with their reasons so the brain can see why.
        self._task_verdict(agent.get("task_id") or "", v,
                           verdict.get("reasons") or [])
        self._remember(verdict)
        return {"verdict": verdict, "line": line}

    # -- task registry (best-effort) -------------------------------------
    @staticmethod
    def _task_update(task_id: str, *, status: str | None = None,
                     note: str | None = None) -> None:
        if not task_id:
            return
        try:
            import tasks as tm

            tm.tasks.update(task_id, status=status, note=note)
        except Exception:  # noqa: BLE001 - never break the loop
            logger.exception("[VERIFY] task update failed")

    @staticmethod
    def _task_verdict(task_id: str, verdict: str, reasons: list) -> None:
        if not task_id:
            return
        try:
            import tasks as tm

            tm.tasks.verdict(task_id, verdict, reasons)
            if verdict == agents.VERIFIED:
                tm.tasks.remove(task_id)
            elif verdict not in tm.STATUSES:
                # e.g. "inconclusive": a finding to report, never left live.
                tm.tasks.update(task_id, status=tm.REPORTED)
        except Exception:  # noqa: BLE001 - never break the loop
            logger.exception("[VERIFY] task verdict failed")

    def _run_info(self, agent_id: str) -> dict | None:
        try:
            import agent_runner

            runs = agent_runner.recent_runs()
        except Exception:  # noqa: BLE001
            return None
        for r in reversed(runs):
            if r.get("agent_id") == agent_id:
                return r
        return None

    def _rework_attempts(self, agent_id: str, brief_title: str) -> int:
        """How many times this agent+brief has already come back needs_fix."""
        path = self._journal_dir() / "journal.jsonl"
        if not path.exists():
            return 0
        count = 0
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("agent_id") != agent_id:
                    continue
                if brief_title and row.get("brief_title") != brief_title:
                    continue
                if row.get("verdict") == agents.NEEDS_FIX:
                    count += 1
        except OSError:
            return 0
        return count

    def _rework(self, agent: dict, verdict: dict) -> None:
        allowed = verdict.get("allowed_files")
        if allowed is None:
            allowed = list(agent.get("files") or [])
        reason = "; ".join(verdict.get("reasons") or []) or "verification failed"
        brief = self._followup_brief(agent, reason, allowed)
        runner = self._runner or _default_runner
        try:
            runner(agent.get("id") or agent.get("agent_id") or "", brief,
                   workdir=self._workdir, files=allowed)
        except Exception:  # noqa: BLE001
            logger.exception("[VERIFY] re-brief failed for %s", agent.get("id"))

    def _followup_brief(self, agent: dict, reason: str, allowed) -> str:
        title = agent.get("brief_title") or "previous brief"
        goal = ("A check of your previous work for "
                f"'{title}' found a failure: {reason}\n"
                "Make the minimal fix in the same files, re-run the "
                "verification, and report back.")
        try:
            import agent_runner

            return agent_runner.build_brief(goal=goal, files=list(allowed or []))
        except Exception:  # noqa: BLE001
            return f"# Follow-up brief\n\n## GOAL\n{goal}\n"

    def _remember(self, verdict: dict) -> None:
        row = {
            "agent_id": verdict.get("agent_id"),
            "brief_title": verdict.get("brief_title"),
            "verdict": verdict.get("verdict"),
            "reasons": list(verdict.get("reasons") or []),
            "at": time.time(),
        }
        files = list(verdict.get("scope_files") or [])
        with self._lock:
            self._last.append(row)
            self._last = self._last[-20:]
            if files:
                self._out_of_scope.append({
                    "agent_id": verdict.get("agent_id"),
                    "files": files,
                    "when": time.time(),
                })
                self._out_of_scope = self._out_of_scope[-20:]

    # -- reporting -------------------------------------------------------
    def report(self) -> dict:
        """Registry counts plus the last verdicts seen by this loop.

        ``out_of_scope`` surfaces scope findings (files an agent changed that
        its brief did not allow) newest first, for the user to judge.
        """
        counts = {"awaiting": 0, "verifying": 0, "verified": 0, "needs_fix": 0}
        try:
            reg = self._reg()
            counts["awaiting"] = len(reg.awaiting_check())
            snap = reg.snapshot()
            counts["verifying"] = sum(
                1 for r in snap if r["status"] == agents.VERIFYING)
            counts["verified"] = sum(
                1 for r in snap if r["status"] == agents.VERIFIED)
            counts["needs_fix"] = sum(
                1 for r in snap if r["status"] == agents.NEEDS_FIX)
        except Exception:  # noqa: BLE001
            pass
        with self._lock:
            last = list(self._last)
            out_of_scope = list(reversed(self._out_of_scope))
        return {"running": self.is_running(), "counts": counts,
                "last_verdicts": last, "out_of_scope": out_of_scope}


# Module singleton the server starts and the delegate callback nudges.
loop = SupervisorLoop()
