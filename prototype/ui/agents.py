"""Permanent agent roster for Jarvis delegation.

Jarvis delegates work to a small permanent set of named agents and stays free
to talk to the user. Each agent works ONLY from a brief Jarvis writes; the user
never talks to an agent directly. This module is a pure in-memory registry —
no I/O, no network — importable on its own so the server and tests can read it.

Model policy: every agent defaults to the free zen tier
(``opencode/mimo-v2.5-free``). Paid models are opt-in: set the
``JARVIS_AGENT_MODEL`` env var, or edit an agent's ``model`` field here, only
when the user explicitly asks for a paid model. The roster is a plain module
constant so it stays trivial to edit.

Each agent carries a name, title, and responsibilities (list of short lines).
Titles are a human label, the brain's routing preference, and the future unit
of billing — but they are NOT a permission fence. Every agent is capable of
doing any task; the title only tells you who fits best, and it never excuses
anyone from work. There are no per-title toolsets, no per-title permissions,
and no refusal path.
"""

import os
import threading
import time
from dataclasses import dataclass

DEFAULT_MODEL = os.environ.get("JARVIS_AGENT_MODEL", "opencode/mimo-v2.5-free")

# Status vocabulary for a single agent.
IDLE = "idle"
WORKING = "working"
DONE = "done"
FAILED = "failed"
BLOCKED = "blocked"
# Post-run verification states, driven by supervisor_loop: an agent that has
# settled is picked up, checked (VERIFYING), then marked VERIFIED or NEEDS_FIX.
VERIFYING = "verifying"
VERIFIED = "verified"
NEEDS_FIX = "needs_fix"

# The full valid vocabulary; ``set_status`` rejects anything else.
STATUSES = frozenset(
    {IDLE, WORKING, DONE, FAILED, BLOCKED, VERIFYING, VERIFIED, NEEDS_FIX}
)
# A settled run is done/failed — the states the verification loop waits for.
SETTLED_STATUSES = frozenset({DONE, FAILED})


@dataclass
class Agent:
    """One permanent agent: a name, title, and responsibilities.

    Every agent is capable of doing any task — the title only tells you who
    fits best, and it never excuses anyone from work. Titles are a human
    label, the brain's routing preference, and the future unit of billing.
    """
    id: str
    name: str
    title: str
    responsibilities: list[str]
    model: str = DEFAULT_MODEL
    color: str = "#2fd0ff"


# The 7 permanent agents — each carries a name, title, and responsibilities.
# Titles are a human label (routing preference, future billing unit); they are
# NOT a permission fence. Edit this list to add/remove/repoint an agent.
AGENTS: list[Agent] = [
    Agent(id="amit", name="Amit", title="Backend Developer",
          responsibilities=["APIs, schemas, migrations, server performance"],
          color="#2fd0ff"),
    Agent(id="anjali", name="Anjali", title="Backend Developer",
          responsibilities=["data models, background jobs, integrations"],
          color="#4ade80"),
    Agent(id="rahul", name="Rahul", title="Frontend Developer",
          responsibilities=["UI, layout, accessibility, app styling"],
          color="#a78bfa"),
    Agent(id="priya", name="Priya", title="Tester",
          responsibilities=["tests, reproductions, edge cases, regressions"],
          color="#d29922"),
    Agent(id="vikram", name="Vikram", title="Project Manager",
          responsibilities=["plans, sequencing, blockers, status reporting"],
          color="#f472b6"),
    Agent(id="sneha", name="Sneha", title="SEO Manager",
          responsibilities=["keywords, metadata, content structure, ranking"],
          color="#06b6d4"),
    Agent(id="karan", name="Karan", title="Copy Writer",
          responsibilities=["copy, tone, docs, product text"],
          color="#f59e0b"),
]


class AgentRegistry:
    """Thread-safe live status for the permanent agents.

    Mutations take a single module-level lock. ``files`` are serialized: if two
    agents are handed the same path, the second is recorded as ``blocked`` with
    a note naming the file (the repo forbids concurrent edits to one file).
    Finishing an agent releases its files and promotes any agent that was
    blocked on them.
    """

    def __init__(self, agents: list[Agent] | None = None):
        self._lock = threading.Lock()
        self._agents: list[Agent] = list(agents if agents is not None else AGENTS)
        self._by_id: dict[str, Agent] = {a.id: a for a in self._agents}
        self._state: dict[str, dict] = {a.id: self._blank() for a in self._agents}
        self._pending_proposal: dict | None = None

    @staticmethod
    def _blank() -> dict:
        return {
            "status": IDLE,
            "brief_title": "",
            "note": "",
            "started_at": None,
            "ended_at": None,
            "files": [],
            # The live task-registry row for this run ("" when the registry is
            # unavailable). Kept so the verification loop can settle the task.
            "task_id": "",
            # False until the verification loop has picked this run up; a new
            # run (start/finish) resets it so every settle is checked once.
            "checked": False,
        }

    # -- task registry (best-effort: never changes agent state on failure) ----
    @staticmethod
    def _task_create(agent_id: str, title: str, files: list,
                     log_path: str, brief_path: str, queued: bool) -> str:
        """Create the live task for this run; "" on any registry error."""
        try:
            import tasks as tm

            tid = tm.tasks.create(agent_id, title, files=files,
                                  log_path=log_path, brief_path=brief_path)
            if tid and queued:
                tm.tasks.update(tid, status=tm.QUEUED)
            return tid
        except Exception:  # noqa: BLE001 - registry errors never break agents
            return ""

    @staticmethod
    def _task_update(task_id: str, *, status: str | None = None,
                     note: str | None = None) -> None:
        if not task_id:
            return
        try:
            import tasks as tm

            tm.tasks.update(task_id, status=status, note=note)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _task_finish(task_id: str, ok: bool, note: str) -> None:
        if not task_id:
            return
        try:
            import tasks as tm

            tm.tasks.finish(task_id, ok, note)
        except Exception:  # noqa: BLE001
            pass

    def _conflict(self, agent_id: str, files: list[str]):
        """Return (path, holder_id) for the first file claimed by another
        working agent, or None when every file is free."""
        for path in files:
            for other_id, st in self._state.items():
                if other_id == agent_id:
                    continue
                if st["status"] == WORKING and path in st["files"]:
                    return path, other_id
        return None

    def _promote_blocked(self) -> None:
        """Promote blocked agents whose files became free (called on finish)."""
        for agent_id, st in self._state.items():
            if st["status"] != BLOCKED:
                continue
            if self._conflict(agent_id, st["files"]) is None:
                st["status"] = WORKING
                st["note"] = ""
                self._task_update(st.get("task_id"), status=WORKING)

    def start(self, agent_id: str, brief_title: str = "",
              files: list[str] | None = None, log_path: str = "",
              brief_path: str = "") -> bool:
        """Mark an agent working on a brief, claiming the files it may touch.

        Also opens the agent's live task-registry row (superseding any earlier
        task for the same agent, so the "now" view stays clean). A registry
        error never changes the agent state.
        """
        claimed = list(dict.fromkeys(files or []))
        with self._lock:
            st = self._state.get(agent_id)
            if st is None:
                return False
            st["brief_title"] = brief_title or ""
            st["note"] = ""
            st["started_at"] = time.time()
            st["ended_at"] = None
            st["files"] = claimed
            st["checked"] = False
            conflict = self._conflict(agent_id, claimed)
            if conflict is not None:
                path, holder = conflict
                st["status"] = BLOCKED
                st["note"] = f"blocked: {path} held by {holder}"
            else:
                st["status"] = WORKING
            prev = st.get("task_id") or ""
            if prev:
                try:
                    import tasks as tm

                    tm.tasks.remove(prev)
                except Exception:  # noqa: BLE001
                    pass
            st["task_id"] = self._task_create(
                agent_id, st["brief_title"], claimed, log_path, brief_path,
                queued=(st["status"] == BLOCKED))
            return True

    def finish(self, agent_id: str, ok: bool = True, note: str = "") -> bool:
        """Mark an agent done/failed, release its files, promote the blocked."""
        with self._lock:
            st = self._state.get(agent_id)
            if st is None:
                return False
            st["ended_at"] = time.time()
            st["status"] = DONE if ok else FAILED
            st["files"] = []
            st["checked"] = False
            if note:
                st["note"] = note
            self._task_finish(st.get("task_id") or "", ok, note or st["note"])
            self._promote_blocked()
            return True

    def set_status(self, agent_id: str, status: str, note: str = "") -> bool:
        """Set an agent's status (thread-safe), validated against the vocabulary.

        Returns False for an unknown agent or an unknown status. Entering any
        verification state marks the run as picked up, so ``awaiting_check``
        never returns it twice.
        """
        if status not in STATUSES:
            return False
        with self._lock:
            st = self._state.get(agent_id)
            if st is None:
                return False
            st["status"] = status
            if note:
                st["note"] = note
            if status in (VERIFYING, VERIFIED, NEEDS_FIX):
                st["checked"] = True
            return True

    def mark_checked(self, agent_id: str, checked: bool = True) -> bool:
        """Flag a run as picked up (or not) by the verification loop."""
        with self._lock:
            st = self._state.get(agent_id)
            if st is None:
                return False
            st["checked"] = bool(checked)
            return True

    def awaiting_check(self) -> list[dict]:
        """Runs that settled (done/failed) and have not been checked yet.

        One row per agent, shaped like a ``snapshot`` row but without the time
        fields — just what the verification loop needs to brief/verify itself.
        """
        with self._lock:
            rows: list[dict] = []
            for a in self._agents:
                st = self._state[a.id]
                if st["status"] in SETTLED_STATUSES and not st.get("checked"):
                    rows.append({
                        "id": a.id,
                        "name": a.name,
                        "title": a.title,
                        "responsibilities": list(a.responsibilities),
                        "model": a.model,
                        "color": a.color,
                        "status": st["status"],
                        "brief_title": st["brief_title"],
                        "note": st["note"],
                        "files": list(st["files"]),
                        # Lets the verification loop settle the live task row.
                        "task_id": st.get("task_id") or "",
                    })
            return rows

    def verified_count(self) -> int:
        """How many agents currently hold a ``verified`` verdict."""
        with self._lock:
            return sum(1 for st in self._state.values()
                       if st["status"] == VERIFIED)

    def note(self, agent_id: str, text: str) -> bool:
        """Set a one-line progress line for an agent (and its live task)."""
        with self._lock:
            st = self._state.get(agent_id)
            if st is None:
                return False
            st["note"] = text or ""
            task_id = st.get("task_id") or ""
        self._task_update(task_id, note=text or "")
        return True

    def snapshot(self) -> list[dict]:
        """Full status rows for every agent, in roster order."""
        with self._lock:
            now = time.time()
            rows: list[dict] = []
            for a in self._agents:
                st = self._state[a.id]
                started = st["started_at"]
                ended = st["ended_at"]
                if started is None:
                    seconds = 0.0
                else:
                    seconds = round(max(0.0, (ended if ended is not None else now)
                                        - started), 1)
                rows.append({
                    "id": a.id,
                    "name": a.name,
                    "title": a.title,
                    "responsibilities": list(a.responsibilities),
                    "model": a.model,
                    "color": a.color,
                    "status": st["status"],
                    "brief_title": st["brief_title"],
                    "note": st["note"],
                    "started_at": started,
                    "ended_at": ended,
                    "seconds": seconds,
                    "files": list(st["files"]),
                })
            return rows

    def running_count(self) -> int:
        """How many agents are actively working right now."""
        with self._lock:
            return sum(1 for st in self._state.values() if st["status"] == WORKING)

    def summary(self) -> dict:
        """Cheap counts for the sidebar cue.

        Always carries the legacy ``running``/``total`` keys; the verification
        counts (``verifying``/``verified``/``needs_fix``) are added only when
        non-zero so the base shape stays backward compatible for callers that
        compare it exactly.
        """
        with self._lock:
            out = {"running": sum(1 for st in self._state.values()
                                  if st["status"] == WORKING),
                   "total": len(self._agents)}
            for label, status in (("verifying", VERIFYING),
                                  ("verified", VERIFIED),
                                  ("needs_fix", NEEDS_FIX)):
                n = sum(1 for st in self._state.values()
                        if st["status"] == status)
                if n:
                    out[label] = n
            return out

    def pick(self, title: str = "") -> str:
        """An agent id to hand the next piece of work to.

        With a *title*, prefer a free agent whose title matches (case-
        insensitive, singular/plural tolerant), else the first free agent.
        With no title, the first free agent. An agent whose title does not
        match is still eligible — never returned as "not my job".
        """
        with self._lock:
            norm_title = (title or "").strip().lower()
            if norm_title:
                for a in self._agents:
                    if (self._state[a.id]["status"] not in (WORKING, BLOCKED)
                            and a.title.lower() == norm_title):
                        return a.id
            for a in self._agents:
                if self._state[a.id]["status"] not in (WORKING, BLOCKED):
                    return a.id
            return self._agents[0].id if self._agents else ""

    def team_counts(self) -> dict[str, int]:
        """Title -> number of ACTIVE agents (for future billing)."""
        with self._lock:
            counts: dict[str, int] = {}
            for a in self._agents:
                counts[a.title] = counts.get(a.title, 0) + 1
            return counts

    def active_count(self) -> int:
        """How many agents are in the roster."""
        with self._lock:
            return len(self._agents)

    def hire(self, name: str, title: str, responsibilities: list[str],
             model: str = "") -> dict:
        """Hire a new agent (in-memory only). Validates the three required
        fields (non-empty name and title, at least one responsibility), makes
        a unique id from the name, assigns a roster colour, returns the new
        agent dict. Does NOT create the agent — that is done by ``confirm``."""
        name = (name or "").strip()
        title = (title or "").strip()
        responsibilities = [r.strip() for r in (responsibilities or []) if r.strip()]
        if not name:
            raise ValueError("name is required")
        if not title:
            raise ValueError("title is required")
        if not responsibilities:
            raise ValueError("at least one responsibility is required")
        agent_id = name.lower().replace(" ", "")
        if agent_id in self._by_id:
            raise ValueError(f"agent {name!r} already exists")
        use_model = (model or "").strip() or DEFAULT_MODEL
        colour = _next_colour(self)
        return {
            "id": agent_id,
            "name": name,
            "title": title,
            "responsibilities": responsibilities,
            "model": use_model,
            "color": colour,
        }

    def confirm(self, proposal: dict) -> Agent:
        """Actually create the agent from a hire proposal. Returns the Agent."""
        with self._lock:
            a = Agent(
                id=proposal["id"],
                name=proposal["name"],
                title=proposal["title"],
                responsibilities=proposal["responsibilities"],
                model=proposal.get("model", DEFAULT_MODEL),
                color=proposal.get("color", "#2fd0ff"),
            )
            self._agents.append(a)
            self._by_id[a.id] = a
            self._state[a.id] = self._blank()
            return a

    def retire(self, agent_id: str) -> bool:
        """Mark an agent inactive (removed from roster/counts but not deleted,
        so future billing history stays explainable)."""
        with self._lock:
            if agent_id not in self._by_id:
                return False
            self._agents = [a for a in self._agents if a.id != agent_id]
            del self._by_id[agent_id]
            self._state.pop(agent_id, None)
            return True

    @property
    def pending_proposal(self) -> dict | None:
        """The current pending hire proposal (one at a time)."""
        return self._pending_proposal

    @pending_proposal.setter
    def pending_proposal(self, value: dict | None) -> None:
        self._pending_proposal = value


# Roster colour palette (cycles for hired agents).
_COLOURS = [
    "#2fd0ff", "#4ade80", "#a78bfa", "#d29922", "#f472b6", "#06b6d4",
    "#f59e0b", "#ef4444", "#8b5cf6", "#10b981", "#f97316", "#6366f1",
]


def _next_colour(reg: AgentRegistry) -> str:
    """Pick the next unused colour from the palette."""
    used = {a.color for a in reg._agents}
    for c in _COLOURS:
        if c not in used:
            return c
    return _COLOURS[len(reg._agents) % len(_COLOURS)]


# Module-level singleton the server pushes to the UI.
registry = AgentRegistry()
