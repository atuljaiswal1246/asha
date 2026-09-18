"""Launch headless Jarvis agents through the sanctioned opencode CLI.

**The opencode CLI is the ONLY channel.** We never call ``opencode.ai/zen/*``
(or any provider endpoint) directly: direct calls made with generic headers are
flagged as abuse and rate-limited with 429s (KB-06). Every run goes out as
``opencode run ...`` so the request carries the same validated client identity
the CLI/supervisor uses.

**Paid models are opt-in only.** An agent runs on its roster model
(``opencode/mimo-v2.5-free`` by default); a caller's ``model`` override is
honoured only when explicitly passed — this module never chooses a paid model
on its own.

The flow mirrors the supervisor's: a *brief* file with fixed sections
(``GOAL``, ``ALLOWED FILES``, ``DO NOT``, ``VERIFY``, ``OBSERVATIONS``,
``REPORT BACK``) is written to the data dir, the CLI is backgrounded with
stdout+stderr captured to ``run.log``, a watcher thread copies its last output
line into the agent registry for the sidebar cue, and on exit the agent is
marked done/failed so the brain can verify the result and speak it. Pure stdlib
and importable standalone.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

LOGGER = logging.getLogger(__name__)

BRIEF_SECTIONS = ("GOAL", "ALLOWED FILES", "DO NOT", "VERIFY", "OBSERVATIONS",
                  "REPORT BACK")

_DEFAULT_REPORT = (
    "which edits you made with file:line, the verbatim output of the verify "
    "commands, and anything you could not do."
)

# Rule 11 ("Report, don't touch"): a brief must always ask for findings, and
# must always forbid acting on them. The standing DO-NOT line is added to every
# brief unless the caller already listed it.
_DEFAULT_OBSERVATIONS = (
    "- anything you noticed that is odd and unrelated to this brief, with "
    "file:line \u2014 do NOT change it, just list it here (write 'none' if "
    "nothing)."
)

_STANDING_SCOPE_RULE = (
    "Do not change anything unrelated to this brief. If you notice something "
    "odd, put it in OBSERVATIONS and leave it alone."
)

_NOTE_CHARS = 80
_POLL_SECS = 0.2


def _shell_quote(part: str) -> str:
    """Quote one command part so the rendered line stays shell-executable.

    A bare ``print(1)`` is not, because the shell treats the parentheses as
    syntax; wrapping it in double quotes both keeps the part intact and lets
    ``shlex.split``/``_brief_verify_commands`` parse it back to the same command.
    """
    if part and re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", part):
        return part
    return '"' + part.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_verify(items) -> str:
    """Render VERIFY entries as one shell-executable line each.

    An entry may be a ready-made shell string (kept verbatim) or a list/tuple
    of argv parts (joined with spaces, parts quoted when needed). This is the
    format ``_brief_verify_commands`` can parse back into a command, unlike a
    Python ``repr`` of a list.
    """
    rows = []
    for entry in (items or []):
        if isinstance(entry, (list, tuple)):
            line = " ".join(_shell_quote(str(p)) for p in entry).strip()
        else:
            line = str(entry).strip()
        if line:
            rows.append(line)
    return ("\n".join(f"- {r}" for r in rows)
            if rows else "- State exactly what you ran and show its verbatim output.")


def build_brief(*, goal: str, files: list[str] | None = None,
                do_not: list[str] | None = None,
                verify: list[str | list[str] | tuple[str, ...]] | None = None,
                report: str = "",
                observations: list[str] | None = None) -> str:
    """Render a markdown brief with the six fixed sections.

    ``files`` is the ONLY list of paths the agent may touch; empty means it
    must not edit anything. ``verify`` is a list whose entries are either a
    ready-made shell command string (``"ruff check a.py"``) or a list/tuple of
    argv parts (``["python3", "-c", "print(1)"]``); each entry is rendered as
    one shell-executable line so the verifier can run it back. ``report`` falls
    back to a sensible default when the caller gives none. ``observations``
    falls back to the standing rule: report oddities, never fix them (rule 11).
    The standing DO-NOT line is always present unless the caller already listed
    it.
    """
    def _bullets(items, empty: str) -> str:
        rows = [str(x).strip() for x in (items or []) if str(x).strip()]
        return "\n".join(f"- {r}" for r in rows) if rows else empty

    goal_text = (goal or "").strip() or "(no goal given)"
    report_text = (report or "").strip() or _DEFAULT_REPORT

    do_not_rows = [str(x).strip() for x in (do_not or []) if str(x).strip()]
    if not any("unrelated to this brief" in r.lower() for r in do_not_rows):
        do_not_rows.append(_STANDING_SCOPE_RULE)
    if do_not:
        do_not_text = _bullets(do_not_rows, "")
    else:
        do_not_text = (
            "- Do not commit. Do not touch config, .env or scripts. "
            "Do not restart any server.\n- " + _STANDING_SCOPE_RULE)

    sections = {
        "GOAL": goal_text,
        "ALLOWED FILES": _bullets(files, "- (none — do not edit any file)"),
        "DO NOT": do_not_text,
        "VERIFY": _render_verify(verify),
        "OBSERVATIONS": _bullets(observations, _DEFAULT_OBSERVATIONS),
        "REPORT BACK": report_text,
    }
    lines = ["# Agent brief", ""]
    for name in BRIEF_SECTIONS:
        lines.append(f"## {name}")
        lines.append(sections[name])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _data_dir() -> Path:
    """Same data-dir resolution as ``screen_tool._data_dir()``."""
    try:
        import jarvis_paths

        return Path(jarvis_paths.data_dir())
    except Exception:  # noqa: BLE001 - standalone use
        root = Path(__file__).resolve().parent.parent
        return root / "data"


def _registry():
    """The live agent registry (resolved at call time so tests can swap it)."""
    import agents

    return agents.registry


def _row(agent_id: str) -> dict | None:
    for row in _registry().snapshot():
        if row["id"] == agent_id:
            return row
    return None


def _goal_first_line(brief: str) -> str:
    """The first non-empty line of the brief's GOAL section (for the cue)."""
    m = re.search(r"^##\s+GOAL\s*$(.*?)(?=^##\s|\Z)",
                  brief or "", re.M | re.S)
    block = m.group(1) if m else (brief or "")
    for line in block.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:80]
    return ""


def _trim(text: str, limit: int = _NOTE_CHARS) -> str:
    return " ".join((text or "").split())[:limit]


def _new_run_dir(agent_id: str) -> Path:
    base = _data_dir() / "agent-runs"
    base.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = base / f"{agent_id}-{stamp}"
    i = 1
    while candidate.exists():
        candidate = base / f"{agent_id}-{stamp}-{i}"
        i += 1
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def _tail_lines(path: Path, lines: int) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    rows = text.splitlines()
    if lines and lines > 0:
        rows = rows[-lines:]
    return "\n".join(rows)


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _clean_line(line: str) -> str:
    """A log line as the user should see it: no ANSI colour, no bare escapes.

    The opencode CLI emits colour codes even when piped, so without this the
    sidebar cue showed '\x1b[0m' for the first seconds of every run.
    """
    return _ANSI_RE.sub("", line).replace("\x1b", "").strip()


def _read_new(path: Path, pos: int) -> tuple[str, int]:
    try:
        with open(path, "rb") as fh:
            fh.seek(pos)
            data = fh.read()
    except OSError:
        return "", pos
    return data.decode("utf-8", errors="replace"), pos + len(data)


def _kill_group(proc: subprocess.Popen | None) -> None:
    """Kill a backgrounded run (and its whole process group), never raising."""
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:  # noqa: BLE001
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass


class Run:
    """One backgrounded agent run: paths, process, and its live exit state."""

    def __init__(self, *, agent_id: str, brief_path: Path, log_path: Path,
                 model: str, workdir: str, pid: int = 0,
                 proc: subprocess.Popen | None = None,
                 state: str = "running", note: str = "",
                 title: str = "") -> None:
        self.agent_id = agent_id
        self.brief_path = Path(brief_path)
        self.log_path = Path(log_path)
        self.started_at = time.time()
        self.ended_at: float | None = (None if state == "running"
                                       else time.time())
        self.model = model
        self.workdir = workdir
        self.title = title
        self.pid = pid
        self.note = note
        self.state = state  # running | done | failed
        self._proc = proc
        self._rc: int | None = None
        self._lock = threading.Lock()

    def running(self) -> bool:
        with self._lock:
            if self.state != "running":
                return False
            return self._proc is not None and self._proc.poll() is None

    def _set(self, state: str, rc: int | None = None) -> None:
        with self._lock:
            self.state = state
            if rc is not None:
                self._rc = rc
            if state != "running" and self.ended_at is None:
                self.ended_at = time.time()

    def tail(self, lines: int = 20) -> str:
        return _tail_lines(self.log_path, lines)

    def result(self, timeout: float | None = None) -> int:
        """Exit code of the run; waits up to *timeout* seconds when given."""
        with self._lock:
            proc = self._proc
            cached = self._rc
        if proc is None:
            return cached if cached is not None else 1
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            rc = 1
        self._set("done" if rc == 0 else "failed", rc)
        return rc


# agent_id -> Run for every run this process has launched.
_RUNS: dict[str, Run] = {}
_RUNS_LOCK = threading.Lock()


def _fire_done(on_done, agent_id: str, rc: int, note: str, run: "Run") -> None:
    """Record the settled run, then call the settle callback once, never raising.

    Both the done and the timeout paths pass through here exactly once, so this
    is where the model ledger gets its row (the ledger's job is evidence for the
    promotion rule -- see ``model_ledger``). The write is fail-open in two ways:
    ``model_ledger.record`` swallows its own errors, and this call is wrapped
    again, because a ledger problem must never break or delay a delegation. A
    failing callback must likewise not kill the watcher, so that is swallowed
    too (the status/cue updates have already happened by the time this is
    called).
    """
    try:
        import model_ledger

        model_ledger.record(
            agent_id=agent_id,
            model=getattr(run, "model", "") or "",
            title=getattr(run, "title", "") or "",
            workdir=getattr(run, "workdir", "") or "",
            started=getattr(run, "started_at", None),
            finished=getattr(run, "ended_at", None) or time.time(),
            exit_code=rc,
            note=note,
            run_dir=str(Path(run.brief_path).parent) if run.brief_path else "",
        )
    except Exception:  # noqa: BLE001 - a ledger write must never break a run
        LOGGER.exception("model ledger write failed for %s", agent_id)

    if on_done is None:
        return
    try:
        on_done(agent_id, rc, note, run)
    except Exception:  # noqa: BLE001
        pass


def _watch(run: Run, proc: subprocess.Popen, reg, timeout: float,
           on_note, on_done=None) -> None:
    """Follow ``run.log`` as it grows, update the cue, and settle on exit."""
    log_path = run.log_path
    deadline = time.time() + timeout if timeout and timeout > 0 else None
    pos = 0
    last = ""
    timed_out = False
    while True:
        new, pos = _read_new(log_path, pos)
        for line in new.splitlines():
            s = _clean_line(line)
            if not s:
                continue
            last = s
            note = _trim(s)
            try:
                reg.note(run.agent_id, note)
            except Exception:  # noqa: BLE001
                pass
            run.note = note
            if on_note:
                try:
                    on_note(note)
                except Exception:  # noqa: BLE001
                    pass
        rc = proc.poll()
        if rc is not None:
            break
        if deadline is not None and time.time() > deadline:
            timed_out = True
            _kill_group(proc)
            try:
                rc = proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                rc = None
            break
        time.sleep(_POLL_SECS)
    if timed_out:
        rc = rc if rc is not None else 1
        note = f"timed out after {int(timeout)}s"
        run._set("failed", rc)
        try:
            reg.finish(run.agent_id, ok=False, note=note)
        except Exception:  # noqa: BLE001
            pass
        _fire_done(on_done, run.agent_id, rc, note, run)
        return
    ok = rc == 0
    note = _clean_line(last) or ("done" if ok else "failed")
    run._set("done" if ok else "failed", rc)
    try:
        reg.finish(run.agent_id, ok=ok, note=note)
    except Exception:  # noqa: BLE001
        pass
    _fire_done(on_done, run.agent_id, rc, note, run)


def run(agent_id: str, brief: str, *, model: str = "", workdir: str = "",
        title: str = "", files: list[str] | None = None,
        timeout: float | None = None, binary: str | None = None,
        on_note=None, on_done=None) -> Run:
    """Launch one agent on *brief* through the opencode CLI, backgrounded.

    Returns a :class:`Run` immediately; the watcher thread updates the registry
    cue and settles the agent's status on exit. ``on_done(agent_id, rc, note,
    run)`` is called exactly once from the watcher when the run settles (never
    raising out of the watcher). Never raises: a missing agent, a blocked file,
    a missing binary or a launch failure all come back as a Run that is already
    ``failed`` with an explanatory note.
    """
    # Record the directory the agent actually runs in. When the caller does not
    # name one, the CLI inherits the process cwd, so that is the run's real
    # workdir -- recording "" here made the verifier fall back to the brain's
    # own repo and fence unrelated files as out-of-scope (false needs_fix).
    workdir = (workdir or "").strip() or os.getcwd()

    reg = _registry()
    row = _row(agent_id)
    if row is None:
        run_dir = _new_run_dir(agent_id)
        brief_path = run_dir / "brief.md"
        log_path = run_dir / "run.log"
        brief_path.write_text(brief or "", encoding="utf-8")
        log_path.write_text(f"unknown agent: {agent_id}\n", encoding="utf-8")
        return Run(agent_id=agent_id, brief_path=brief_path, log_path=log_path,
                   model=model, workdir=workdir, state="failed",
                   note=f"unknown agent {agent_id!r}")

    use_model = (model or "").strip() or row.get("model") or ""
    title = (title or "").strip() or _goal_first_line(brief) or "agent brief"
    reg.start(agent_id, _goal_first_line(brief) or title, list(files or []))
    after = _row(agent_id)
    if after is not None and after.get("status") == "blocked":
        note = after.get("note") or "blocked"
        reg.finish(agent_id, ok=False, note=note)
        run_dir = _new_run_dir(agent_id)
        brief_path = run_dir / "brief.md"
        log_path = run_dir / "run.log"
        brief_path.write_text(brief or "", encoding="utf-8")
        log_path.write_text(note + "\n", encoding="utf-8")
        return Run(agent_id=agent_id, brief_path=brief_path, log_path=log_path,
                   model=use_model, workdir=workdir, state="failed", note=note)

    run_dir = _new_run_dir(agent_id)
    brief_path = run_dir / "brief.md"
    log_path = run_dir / "run.log"
    brief_path.write_text(brief or "", encoding="utf-8")
    log_path.write_text("", encoding="utf-8")

    exe = (binary or os.environ.get("JARVIS_OPENCODE_BIN", "").strip()
           or shutil.which("opencode"))
    if not exe:
        note = "opencode CLI not found (set JARVIS_OPENCODE_BIN)"
        log_path.write_text(note + "\n", encoding="utf-8")
        reg.finish(agent_id, ok=False, note=note)
        return Run(agent_id=agent_id, brief_path=brief_path, log_path=log_path,
                   model=use_model, workdir=workdir, state="failed", note=note)

    if timeout is None:
        try:
            timeout = float(os.environ.get("JARVIS_AGENT_TIMEOUT", "900"))
        except ValueError:
            timeout = 900.0

    cmd = [exe, "run", "-m", use_model]
    if workdir:
        cmd += ["--dir", workdir]
    if title:
        cmd += ["--title", title]
    cmd += ["--auto", brief or ""]

    try:
        with open(log_path, "ab") as logf:
            proc = subprocess.Popen(
                cmd, cwd=workdir or None, stdout=logf,
                stderr=subprocess.STDOUT, start_new_session=True)
    except FileNotFoundError:
        note = f"opencode CLI not found: {exe} (set JARVIS_OPENCODE_BIN)"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(note + "\n")
        reg.finish(agent_id, ok=False, note=note)
        return Run(agent_id=agent_id, brief_path=brief_path, log_path=log_path,
                   model=use_model, workdir=workdir, state="failed", note=note)
    except Exception as e:  # noqa: BLE001
        note = f"launch failed: {e}"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(note + "\n")
        reg.finish(agent_id, ok=False, note=note)
        return Run(agent_id=agent_id, brief_path=brief_path, log_path=log_path,
                   model=use_model, workdir=workdir, state="failed", note=note)

    run_obj = Run(agent_id=agent_id, brief_path=brief_path, log_path=log_path,
                  model=use_model, workdir=workdir, pid=proc.pid, proc=proc,
                  title=title)
    with _RUNS_LOCK:
        _RUNS[agent_id] = run_obj
    watcher = threading.Thread(
        target=_watch, args=(run_obj, proc, reg, timeout, on_note, on_done),
        name=f"agent-watch-{agent_id}", daemon=True)
    watcher.start()
    return run_obj


def stop(agent_id: str) -> bool:
    """Kill a running agent's process group. True when one was running."""
    with _RUNS_LOCK:
        run_obj = _RUNS.get(agent_id)
    if run_obj is None or not run_obj.running():
        return False
    _kill_group(run_obj._proc)
    return True


def active() -> list[str]:
    """Agent ids with a run currently in flight."""
    with _RUNS_LOCK:
        return [aid for aid, r in _RUNS.items() if r.running()]


def recent_runs() -> list[dict]:
    """Every run this process has launched, newest state included.

    One entry per agent (``_RUNS`` is keyed by agent id, so a re-run replaces
    the previous entry). Enough for the verification loop to find the brief and
    the log: ``{agent_id, brief_path, log_path, workdir, rc, started_at,
    ended_at}``.
    """
    with _RUNS_LOCK:
        runs = list(_RUNS.values())
    return [{
        "agent_id": r.agent_id,
        "brief_path": str(r.brief_path),
        "log_path": str(r.log_path),
        "workdir": r.workdir,
        "rc": r._rc,
        "started_at": r.started_at,
        "ended_at": r.ended_at,
    } for r in runs]
