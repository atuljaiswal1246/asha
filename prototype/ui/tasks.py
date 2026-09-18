"""The live task registry: the single source of truth for what the agents are
doing *right now*.

The rule (set by the user): a task lives in this registry only while it is
live. The moment a task is **complete AND verified it is removed** — no stale
rows, no history to wade through. History already exists in the verify journal
(``<data_dir>/agent-verify/journal.jsonl``); this registry is for "now".

The brain must never have to go back into the conversation to work out what each
agent is doing: the registry keeps itself current (``agents.py`` creates a task
on start, updates it on note, finishes it on exit; ``supervisor_loop.py``
records the verdict and removes a verified task immediately), so
``summary_line()`` is always a cheap, true one-liner.

Design: pure stdlib, thread-safe with a single lock, importable standalone (it
never imports ``agents`` at module load — the agent name is looked up lazily and
degrades to the raw id). No I/O on the hot path: ``summary_line()`` is called on
every user turn and only reads in-memory state.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

_HAS_TEXT = re.compile(r"[A-Za-z0-9]")

# Task status vocabulary: the agents-registry words plus task-only ones.
QUEUED = "queued"
WORKING = "working"
VERIFYING = "verifying"
VERIFIED = "verified"
NEEDS_FIX = "needs_fix"
REPORTED = "reported"
FAILED = "failed"

STATUSES = frozenset(
    {QUEUED, WORKING, VERIFYING, VERIFIED, NEEDS_FIX, REPORTED, FAILED}
)
# "Live" = still on the brain's plate. A verified task is removed at once, so it
# is never actually live.
LIVE_STATUSES = frozenset({QUEUED, WORKING, VERIFYING})

_DEFAULT_MAX_AGE = 24 * 3600


def _agent_name(agent_id: str) -> str:
    """Human name for an agent id, or the id itself (lazy, never raises)."""
    try:
        from agents import AGENTS

        for a in AGENTS:
            if a.id == agent_id:
                return a.name
    except Exception:  # noqa: BLE001 - standalone use
        pass
    return agent_id


def _task_repo(agent_id: str) -> str:
    """The folder an agent's run works in, for the board's project resolution.

    The live task registry itself has no workdir, so the run record is the
    source of truth. Lazy and best-effort: a missing runner just means "".
    """
    try:
        import agent_runner

        for r in agent_runner.recent_runs():
            if r.get("agent_id") == agent_id:
                return r.get("workdir") or ""
    except Exception:  # noqa: BLE001 - standalone use
        pass
    return ""


@dataclass
class Task:
    """One live piece of agent work."""

    id: str
    agent_id: str
    agent_name: str = ""
    title: str = ""
    files: list = field(default_factory=list)
    status: str = WORKING
    started_at: float = 0.0
    updated_at: float = 0.0
    ended_at: float | None = None
    note: str = ""
    attempts: int = 0
    verdict: str = ""
    reasons: list = field(default_factory=list)
    log_path: str = ""
    brief_path: str = ""

    def as_dict(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        end = self.ended_at if self.ended_at is not None else now
        started = self.started_at or now
        note = (self.note or "").strip()
        # A one-line display label: the live note when it carries real text
        # (the CLI's last line is often just a code fence), else the brief.
        label = (note if note and _HAS_TEXT.search(note)
                 else (self.title or "").strip() or self.status)
        return {
            "label": label,
            "id": self.id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "title": self.title,
            "files": list(self.files),
            "status": self.status,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "ended_at": self.ended_at,
            "note": self.note,
            "attempts": self.attempts,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "log_path": self.log_path,
            "brief_path": self.brief_path,
            "seconds": round(max(0.0, end - started), 1),
        }


class TaskRegistry:
    """Thread-safe, always-current registry of agent tasks.

    One lock guards every mutation and read, so concurrent creating/updating
    can never corrupt a snapshot.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}
        self._seq = 0
        # Off by default so unit tests never touch the real board file; the
        # server turns it on at startup (enable_board_sync) so live agent work
        # shows up on the Projects board.
        self.board_sync = False

    def enable_board_sync(self, on: bool = True) -> None:
        """Turn the live Projects-board sync on (Jarvis's own path only)."""
        self.board_sync = bool(on)

    # -- live Projects-board sync (best-effort; never changes task state) --
    def _board_sync_task(self, agent_id: str, title: str, note: str,
                         status: str) -> None:
        """Mirror one task onto the Projects board, or clear it when not live.

        Called after the registry lock is released so no disk I/O happens under
        it. Any board error is swallowed: the registry is the source of truth.
        """
        if not self.board_sync:
            return
        try:
            import board as board_mod

            if status in LIVE_STATUSES:
                board_mod.board.upsert_live(
                    agent_id, title or note or status, note=note,
                    repo=_task_repo(agent_id or ""), writer=True)
            else:
                board_mod.board.clear_live(agent_id or "", writer=True)
        except Exception:  # noqa: BLE001 - board errors never break agents
            pass

    def _board_clear_live(self, agent_ids) -> None:
        if not self.board_sync:
            return
        try:
            import board as board_mod

            for agent_id in agent_ids:
                board_mod.board.clear_live(agent_id or "", writer=True)
        except Exception:  # noqa: BLE001
            pass

    # -- mutations -------------------------------------------------------
    def create(self, agent_id: str, title: str, files=None, log_path: str = "",
               brief_path: str = "") -> str:
        """Register a new task; returns its id. Status starts ``working``."""
        now = time.time()
        with self._lock:
            self._seq += 1
            tid = f"t{self._seq}"
            self._tasks[tid] = Task(
                id=tid,
                agent_id=agent_id or "",
                agent_name=_agent_name(agent_id or ""),
                title=(title or "").strip(),
                files=list(files or []),
                status=WORKING,
                started_at=now,
                updated_at=now,
                log_path=log_path or "",
                brief_path=brief_path or "",
            )
            task = self._tasks[tid]
            agent, ttl, note, status = (
                task.agent_id, task.title, task.note, task.status)
        self._board_sync_task(agent, ttl, note, status)
        return tid

    def update(self, task_id: str, *, status: str | None = None,
               note: str | None = None) -> bool:
        """Update a task's status and/or note. False for unknown id/status."""
        if status is not None and status not in STATUSES:
            return False
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return False
            if status is not None:
                t.status = status
            if note is not None:
                t.note = note or ""
            t.updated_at = time.time()
            agent, ttl, tnote, tstatus = t.agent_id, t.title, t.note, t.status
        self._board_sync_task(agent, ttl, tnote, tstatus)
        return True

    def finish(self, task_id: str, ok: bool, note: str = "") -> bool:
        """The agent's run has ended: mark it failed, or complete (awaiting the
        verdict) when *ok*. Sets ``ended_at``."""
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return False
            now = time.time()
            t.ended_at = now
            t.updated_at = now
            # Complete but not yet verified is not "verified": it stays live
            # (verifying) until the supervisor reaches a verdict.
            t.status = VERIFYING if ok else FAILED
            if note:
                t.note = note
            agent, ttl, tnote, tstatus = t.agent_id, t.title, t.note, t.status
        self._board_sync_task(agent, ttl, tnote, tstatus)
        return True

    def verdict(self, task_id: str, verdict: str,
                reasons=None) -> bool:
        """Record a verification verdict (verified / needs_fix / reported / ...).

        An unrecognised verdict keeps the current status (the verdict string is
        still stored) so a caller can never push the registry into an unknown
        state.
        """
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return False
            t.verdict = str(verdict or "")
            if reasons is not None:
                t.reasons = list(reasons)
            t.attempts += 1
            if t.verdict in STATUSES:
                t.status = t.verdict
            t.updated_at = time.time()
            agent, ttl, tnote, tstatus = t.agent_id, t.title, t.note, t.status
        self._board_sync_task(agent, ttl, tnote, tstatus)
        return True

    def remove(self, task_id: str) -> bool:
        """Drop a task from the registry (used the moment it is verified)."""
        with self._lock:
            t = self._tasks.pop(task_id, None)
            agent = t.agent_id if t is not None else ""
        if t is not None:
            # Verified work leaves the board on its own.
            self._board_clear_live([agent])
            return True
        return False

    def remove_verified(self) -> list[str]:
        """Remove every ``verified`` task; returns the removed ids."""
        with self._lock:
            ids = [tid for tid, t in self._tasks.items()
                   if t.status == VERIFIED]
            agents = [self._tasks[tid].agent_id for tid in ids]
            for tid in ids:
                self._tasks.pop(tid, None)
        self._board_clear_live(agents)
        return ids

    def prune(self, max_age_secs: float = _DEFAULT_MAX_AGE) -> list[str]:
        """Drop abandoned (not live) tasks older than *max_age_secs* so nothing
        lingers forever. Live tasks are never touched."""
        now = time.time()
        with self._lock:
            ids = []
            for tid, t in self._tasks.items():
                if t.status in LIVE_STATUSES:
                    continue
                # Age is measured from when the task settled, so recording a
                # verdict does not keep a zombie alive forever.
                last = t.ended_at or t.updated_at or t.started_at or 0.0
                if now - last > max_age_secs:
                    ids.append(tid)
            agents = [self._tasks[tid].agent_id for tid in ids]
            for tid in ids:
                self._tasks.pop(tid, None)
        self._board_clear_live(agents)
        return ids

    # -- reads -----------------------------------------------------------
    def active(self) -> list[dict]:
        """Every task not yet verified/removed, newest first."""
        now = time.time()
        with self._lock:
            rows = [t.as_dict(now) for t in self._tasks.values()
                    if t.status != VERIFIED]
        rows.sort(key=lambda r: (r["started_at"], r["id"]), reverse=True)
        return rows

    def snapshot(self) -> list[dict]:
        """The active tasks, for the UI/brain."""
        return self.active()

    def summary_line(self) -> str:
        """ONE short line for the brain; "" when there is nothing live.

        Cheap and terse by design — called on every user turn, so it only reads
        in-memory state. Example:
        "2 agents: Amit - reading notes (42s), Priya - running tests (11s)"
        """
        rows = self.active()
        if not rows:
            return ""
        parts = []
        for r in rows:
            parts.append(f"{r['agent_name'] or r['agent_id']} - {r['label']} "
                         f"({int(r['seconds'])}s)")
        n = len(rows)
        return f"{n} agent{'s' if n != 1 else ''}: " + ", ".join(parts)


# Module singleton the server, the agents registry and the supervisor loop share.
tasks = TaskRegistry()
