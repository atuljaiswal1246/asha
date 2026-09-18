"""The Projects board: the one place Jarvis's tracked work lives.

Two words that must never be confused:

* A **repo project** is the folder Jarvis codes in (the picker above the
  composer). It is owned by :mod:`worker_engine`.
* A **Project** (this module, always capitalised in the UI) is a body of work
  being tracked. It *may* optionally link to a repo project through its
  ``repo`` field, which is how live agent work finds the Project it belongs to.

Shape of the data (one documented schema, ``schema_version`` on every record):

* Project: ``id, name, note, repo, created_at, updated_at``
* Card (a task in a Project): ``id, project_id, title, area, owner, priority,
  needs, notes, status, source, live, agent_id, created_at, updated_at``

The board columns — the **closed** status vocabulary, in order — are
:data:`STATUSES`. Nothing else is a status.

Two hard rules, both enforced here rather than by convention:

1. **Standardised and model-agnostic.** :func:`render_table` is the single
   canonical renderer; its column names are exactly ``id, project, task,
   status, note, owner, priority`` and its output is byte-identical for the
   same cards, so a small free model, a paid model and a human all read the
   same text. ``note`` is capped at :data:`NOTE_MAX` chars and single-line.
2. **Jarvis and the user both write; agents and workers are read-only.**
   Every mutating method takes ``writer=False`` by default and raises
   :class:`PermissionError` unless the caller passes ``writer=True``.
   Writers are Jarvis (the brain's path: the voice tool and the live-task
   sync) and the user (via the app UI surface); agents and workers never
   pass it, so the guard keeps them read-only.

Persistence is a single JSON file at ``<data_dir>/board.json`` (same data-dir
resolution as ``tasks.py`` / ``agent_runner.py``), written atomically. A corrupt
file never crashes: it is kept as ``board.json.bad`` and the board starts empty.

Pure stdlib, thread-safe with a single lock, importable standalone.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

SCHEMA_VERSION = 1

BACKLOG = "Backlog"
IN_PROGRESS = "In progress"
WAITING = "Waiting on you"
BLOCKED = "Blocked"
DONE = "Done"

# The board columns, in order. This is the CLOSED status vocabulary.
STATUSES = (BACKLOG, IN_PROGRESS, WAITING, BLOCKED, DONE)
_STATUS_ORDER = {s: i for i, s in enumerate(STATUSES)}

PRIORITIES = ("P0", "P1", "P2", "P3")
_PRIORITY_ORDER = {p: i for i, p in enumerate(PRIORITIES)}
_DEFAULT_PRIORITY = "P2"

NOTE_MAX = 120
TITLE_MAX = 200

# The exact column names of the canonical table (see :func:`render_table`).
TABLE_COLUMNS = ("id", "project", "task", "status", "note", "owner", "priority")

# Sources for a card.
SOURCE_USER = "user"
SOURCE_AGENT = "agent"
SOURCE_JARVIS = "jarvis"


def _default_path() -> Path:
    """``<data_dir>/board.json`` — same resolution as tasks/agent_runner."""
    try:
        import jarvis_paths

        return Path(jarvis_paths.data_dir()) / "board.json"
    except Exception:  # noqa: BLE001 - standalone use
        return Path(__file__).resolve().parents[1] / "data" / "board.json"


def _repo_root() -> str:
    """The folder Jarvis codes in, for the seeded "Jarvis" Project's ``repo``."""
    return str(Path(__file__).resolve().parents[2])


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


def _one_line(text: object, limit: int = NOTE_MAX) -> str:
    """Collapse *text* to a single line, capped at *limit* characters."""
    words = " ".join(str(text or "").split())
    return words[:limit]


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


# ── the seed ─────────────────────────────────────────────────────────────────
# The user's 34 tracked items, recovered from the now-deleted notes/todo.csv.
# Each row: (id, area, task, owner, status, priority, needs, notes). The id is
# preserved in the card's notes so nothing is lost.
_SEED_ROWS = [
    ("T01", "Contract", "Read-the-docs-first law before any third-party integration",
     "Brain", "Done", "P1", "", "In agent loop, worker engine and brain contract"),
    ("T02", "MCP", "Honest gating when a provider allowlists its server (Figma)",
     "Brain", "Done", "P1", "", "connect() refuses upfront instead of opening a browser on a dead end"),
    ("T03", "MCP", "Per-connector credential resolution (register dropped the provider name)",
     "Brain", "Done", "P1", "", "Figma creds now resolve; generic error names the real blocker"),
    ("T04", "Eyes", "read_screen / look_at_image in both tiers",
     "Brain", "Done", "P0", "", "OCR on-device + vision model; per-window capture never steals focus"),
    ("T05", "Eyes", "Grant Screen Recording to Jarvis",
     "You", "Waiting", "P0", "Screen Recording permission", "Until then read_screen returns an actionable message and sees nothing"),
    ("T06", "Media", "generate_image available to the coding agent",
     "Brain", "Done", "P1", "", "Fixes 'make me a logo' turning into a coding spree"),
    ("T07", "Mac app", "Real Dock icon from the Jarvis logo",
     "Brain", "Done", "P1", "", "Packaged app had no CFBundleIconFile at all"),
    ("T08", "Mac app", "Title-bar inset so buttons stop colliding with the brand",
     "Brain", "Done", "P1", "", "html.macapp inset + drag-anywhere window"),
    ("T09", "Mac app", "Calmer light mode + composer redesign + responsive layout",
     "Brain", "Done", "P2", "", "Icon rail <=980px, stacked <=820px"),
    ("T10", "Agents", "Six permanent named agents (Amit Anjali Rahul Priya Vikram Sneha)",
     "Brain", "Done", "P1", "", "No persona, brief-only, one writer per file"),
    ("T11", "Agents", "Sidebar roster under Library with collapse",
     "Brain", "Done", "P1", "", "Status only; no clicks, no chat channel"),
    ("T12", "Agents", "agent_runner: brief -> sanctioned CLI -> supervise",
     "Brain", "Done", "P0", "", "Exposed as one delegate tool; proven with live runs"),
    ("T13", "Agents", "Supervisor loop: wake on completion and verify",
     "Brain", "Done", "P0", "", "fence -> proof -> artifact -> verdict -> journal; rework max 2"),
    ("T14", "Agents", "Proof step actually executes VERIFY commands",
     "Brain", "Done", "P0", "", "Were stringified and skipped; a failing command can no longer reach verified"),
    ("T15", "Agents", "Task registry: live status always available to the brain",
     "Agent", "In flight", "P0", "", "Removed the moment a task is verified; one-line [work now] note per turn"),
    ("T16", "Research", "Three studies: gaps / camera / wake word",
     "Agent", "Done", "P2", "", "Gaps 7/7 verified vs code; camera claims verified"),
    ("T17", "Research", "Verify the wake-word report's claims",
     "Brain", "Queued", "P2", "", "Spot-check against the /tmp clones"),
    ("T18", "Contract", "Rule 10 never idle + rule 11 report-don't-touch",
     "Brain", "Done", "P0", "", "Also enforced in every agent brief (OBSERVATIONS section)"),
    ("T19", "Contract", "Freeze VAD 0.25/0.75/0.7",
     "Brain", "Done", "P0", "", "ROADMAP + AGENTS.md + comment above the params"),
    ("T20", "Memory", "Decay / similarity / session persistence modules",
     "Brain", "Done", "P2", "", "Built, tested, deliberately inert"),
    ("T21", "Memory", "Wire the memory/session modules in",
     "Brain", "Decision", "P2", "Your go-ahead", "One at a time, each behind its own checkpoint"),
    ("T22", "Connectors", "Google Cloud Desktop OAuth client",
     "You", "Waiting", "P1", "GOOGLE_CLIENT_ID + SECRET", "Gmail + Calendar connectors are written and waiting"),
    ("T23", "Connectors", "Figma via REST using the published OAuth app",
     "Brain", "Decision", "P2", "Your go-ahead", "Works today; remote MCP stays blocked by Figma's catalog"),
    ("T24", "Camera", "Camera access for Jarvis",
     "You", "Decision", "P2", "your yes/no", "Needs NSCameraUsageDescription + camera entitlement"),
    ("T25", "Voice", "Wake word \"Hey Jarvis\"",
     "You", "Decision", "P2", "your yes/no", "Increment 1 touches the voice path; VAD is frozen"),
    ("T26", "Voice", "Backchanneling / prosody / full-duplex",
     "You", "Decision", "P3", "your pick", "Ranked in the gaps report"),
    ("T27", "Cleanup", "Persona-vs-agent cleanup (old worker label says Ashish)",
     "Brain", "Decision", "P2", "your call", "personas.py:57, proto.py:277 - only Jarvis talks now"),
    ("T28", "Perf", "Re-measure TTFT against the 559 ms baseline",
     "Brain", "Queued", "P1", "", "Today added a per-turn status note and new tools"),
    ("T29", "Deploy", "Sync proof fix + task registry into the app bundle and relaunch",
     "Brain", "Queued", "P1", "", "agent_runner.py and supervisor_loop.py changed after the last sync"),
    ("T30", "Cleanup", "server.py:5657 and agent_loop.py still str() each VERIFY arg",
     "Brain", "Queued", "P3", "", "Reported not touched (rule 11); harmless today"),
    ("T31", "Figma", "Figma remote MCP (catalog allowlist)",
     "External", "Blocked", "P2", "Figma approval", "Waitlist; desktop local MCP is the workaround"),
    ("T32", "Release", "Windows installer runtime verify / signing / notarization",
     "External", "Blocked", "P2", "", "Pre-existing, untouched today"),
    ("T33", "Release", "Self-host deploy path untested",
     "External", "Blocked", "P3", "", "No Docker/Linux available here"),
    ("T34", "Release", "Public Gmail/Calendar scopes need Google verification",
     "External", "Blocked", "P3", "Figma-style review", "Fine for you + test users meanwhile"),
]

# old todo.csv status -> board column.
_SEED_STATUS = {
    "done": DONE,
    "queued": IN_PROGRESS,
    "in flight": IN_PROGRESS,
    "waiting": WAITING,
    "decision": WAITING,
    "blocked": BLOCKED,
}


def seed_state() -> dict:
    """A fresh, seeded store state (two Projects + the 34 cards)."""
    now = time.time()
    projects = {
        "p1": {
            "schema_version": SCHEMA_VERSION,
            "id": "p1",
            "name": "Jarvis",
            "note": "Jarvis itself, tracked on its own board.",
            "repo": _repo_root(),
            "created_at": now,
            "updated_at": now,
        },
        "p2": {
            "schema_version": SCHEMA_VERSION,
            "id": "p2",
            "name": "Cal Matters",
            "note": "",
            "repo": "",
            "created_at": now,
            "updated_at": now,
        },
    }
    cards: dict[str, dict] = {}
    for i, (sid, area, task, owner, status, priority, needs, notes) in enumerate(
            _SEED_ROWS, 1):
        cid = f"c{i}"
        cards[cid] = {
            "schema_version": SCHEMA_VERSION,
            "id": cid,
            "project_id": "p1",
            "title": _one_line(task, TITLE_MAX),
            "area": area,
            "owner": owner,
            "priority": priority if priority in _PRIORITY_ORDER else _DEFAULT_PRIORITY,
            "needs": needs,
            "notes": _one_line(f"{sid}: {notes}" if notes else sid),
            "status": _SEED_STATUS.get(status.strip().lower(), BACKLOG),
            "created_at": now,
            "updated_at": now,
            "source": SOURCE_USER,
            "live": False,
            "agent_id": "",
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "projects": projects,
        "cards": cards,
        "seq": {"p": len(projects), "c": len(cards)},
    }


def render_table(cards) -> str:
    """ONE canonical renderer for cards — same bytes for model and human.

    Column names are exactly :data:`TABLE_COLUMNS` (``id, project, task,
    status, note, owner, priority``), rows are sorted by project, then board
    column, then priority, then id, and cells are left-padded to the widest
    value. ``note`` is single-line and capped at :data:`NOTE_MAX` chars.
    """
    rows = []
    for c in (cards or []):
        note = _one_line(c.get("note", c.get("notes", "")), NOTE_MAX)
        rows.append({
            "id": _one_line(c.get("id", ""), 40),
            "project": _one_line(c.get("project", c.get("project_id", "")), 60),
            "task": _one_line(c.get("task", c.get("title", "")), TITLE_MAX),
            "status": _one_line(c.get("status", "")),
            "note": note,
            "owner": _one_line(c.get("owner", "")),
            "priority": _one_line(c.get("priority", "")),
        })
    rows.sort(key=lambda r: (
        r["project"], _STATUS_ORDER.get(r["status"], len(STATUSES)),
        _PRIORITY_ORDER.get(r["priority"], len(PRIORITIES)), r["id"]))
    widths = []
    for col in TABLE_COLUMNS:
        widths.append(max([len(col)] + [len(r[col]) for r in rows]))

    def line(cells) -> str:
        return "| " + " | ".join(
            str(v).ljust(w) for v, w in zip(cells, widths)) + " |"

    out = [line(TABLE_COLUMNS),
           "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
    out += [line([r[col] for col in TABLE_COLUMNS]) for r in rows]
    return "\n".join(out)


class Board:
    """Thread-safe Projects store. Writers are Jarvis and the user;
    agents and workers are read-only (the guard enforces it)."""

    def __init__(self, path: str | os.PathLike | None = None):
        self._path = Path(path) if path is not None else _default_path()
        self._lock = threading.RLock()
        self._loaded = False
        self._projects: dict[str, dict] = {}
        self._cards: dict[str, dict] = {}
        self._seq = {"p": 0, "c": 0}
        self.recovery_notice = ""

    # -- persistence -----------------------------------------------------
    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            self.load()

    def load(self, path: str | os.PathLike | None = None) -> dict:
        """Read the JSON store (seeding on first run). Never raises on bad data."""
        target = Path(path) if path is not None else self._path
        with self._lock:
            self._path = target
            self._loaded = True
            self.recovery_notice = ""
            if not target.exists():
                state = seed_state()
                self._apply(state)
                self._save_locked()
                return {"ok": True, "seeded": True, "recovered": False,
                        "notice": ""}
            try:
                state = json.loads(target.read_text(encoding="utf-8"))
                if not isinstance(state, dict):
                    raise ValueError("board.json is not an object")
                self._apply(state)
                return {"ok": True, "seeded": False, "recovered": False,
                        "notice": ""}
            except Exception as e:  # noqa: BLE001 - corrupt file must not crash
                bad = target.with_suffix(target.suffix + ".bad")
                try:
                    os.replace(target, bad)
                except OSError:
                    pass
                self._apply(seed_state())
                self.recovery_notice = (
                    f"board.json was unreadable ({e}); kept as {bad.name} and "
                    "started from the seed.")
                self._save_locked()
                return {"ok": True, "seeded": True, "recovered": True,
                        "notice": self.recovery_notice}

    def _apply(self, state: dict) -> None:
        projects = state.get("projects") or {}
        cards = state.get("cards") or {}
        self._projects = {str(k): dict(v) for k, v in projects.items()}
        # Live cards are process-local: a card left live by a dead process is
        # stale, so drop it on load rather than show work that is not running.
        self._cards = {str(k): dict(v) for k, v in cards.items()
                       if not v.get("live")}
        seq = state.get("seq") or {}
        self._seq = {"p": int(seq.get("p") or len(self._projects)),
                     "c": int(seq.get("c") or len(self._cards))}

    def save(self, path=None, *, writer: bool = False) -> None:
        """Write the store atomically. Writers-only (Jarvis and the user;
        see the module docstring)."""
        self._require_writer(writer)
        with self._lock:
            if path is not None:
                self._path = Path(path)
            self._save_locked()

    def _save_locked(self) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "projects": self._projects,
            "cards": self._cards,
            "seq": self._seq,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self._path)

    @staticmethod
    def _require_writer(writer: bool) -> None:
        if not writer:
            raise PermissionError(
                "the Projects board is read-only for agents and workers; only "
                "Jarvis's own path may write (pass writer=True)")

    # -- projects --------------------------------------------------------
    def projects(self) -> list[dict]:
        self._ensure_loaded()
        with self._lock:
            return [self._project_view(p) for p in self._projects.values()]

    def _project_view(self, p: dict) -> dict:
        counts = {s: 0 for s in STATUSES}
        for c in self._cards.values():
            if c.get("project_id") == p["id"] and c.get("status") in counts:
                counts[c["status"]] += 1
        return {
            "schema_version": p.get("schema_version", SCHEMA_VERSION),
            "id": p["id"],
            "name": p.get("name", ""),
            "note": p.get("note", ""),
            "repo": p.get("repo", ""),
            "created_at": p.get("created_at", 0.0),
            "updated_at": p.get("updated_at", 0.0),
            "counts": counts,
            "total": sum(counts.values()),
        }

    def get_project(self, project_id: str) -> dict | None:
        self._ensure_loaded()
        with self._lock:
            p = self._projects.get(str(project_id))
            return self._project_view(p) if p else None

    def resolve_project(self, key: str) -> str | None:
        """A project id from an id or (case-insensitive) name; None if unknown."""
        self._ensure_loaded()
        if not key:
            return None
        key = str(key).strip()
        with self._lock:
            if key in self._projects:
                return key
            low = key.lower()
            for pid, p in self._projects.items():
                if (p.get("name") or "").strip().lower() == low:
                    return pid
            return None

    def add_project(self, name: str, note: str = "", repo: str = "", *,
                    writer: bool = False) -> dict:
        self._require_writer(writer)
        self._ensure_loaded()
        name = _one_line(name, TITLE_MAX) or "Untitled project"
        now = time.time()
        with self._lock:
            self._seq["p"] += 1
            pid = f"p{self._seq['p']}"
            self._projects[pid] = {
                "schema_version": SCHEMA_VERSION,
                "id": pid,
                "name": name,
                "note": _one_line(note, 500),
                "repo": str(repo or "").strip(),
                "created_at": now,
                "updated_at": now,
            }
            self._save_locked()
            return self._project_view(self._projects[pid])

    def update_project(self, project_id: str, *, writer: bool = False,
                       **fields) -> dict | None:
        self._require_writer(writer)
        self._ensure_loaded()
        with self._lock:
            p = self._projects.get(str(project_id))
            if p is None:
                return None
            for key in ("name", "note", "repo"):
                if key in fields and fields[key] is not None:
                    value = fields[key]
                    p[key] = (str(value).strip() if key == "repo"
                              else _one_line(value, 500 if key == "note"
                                             else TITLE_MAX))
            p["updated_at"] = time.time()
            self._save_locked()
            return self._project_view(p)

    def remove_project(self, project_id: str, delete_cards: bool = True, *,
                       writer: bool = False) -> bool:
        self._require_writer(writer)
        self._ensure_loaded()
        with self._lock:
            pid = str(project_id)
            if pid not in self._projects:
                return False
            del self._projects[pid]
            if delete_cards:
                for cid in [c for c, v in self._cards.items()
                            if v.get("project_id") == pid]:
                    del self._cards[cid]
            else:
                for c in self._cards.values():
                    if c.get("project_id") == pid:
                        c["project_id"] = ""
            self._save_locked()
            return True

    # -- cards -----------------------------------------------------------
    def get_card(self, card_id: str) -> dict | None:
        self._ensure_loaded()
        with self._lock:
            c = self._cards.get(str(card_id))
            if c is None:
                return None
            return self._card_view(c)

    def _card_view(self, c: dict) -> dict:
        elapsed = 0.0
        if c.get("live"):
            elapsed = round(max(0.0, time.time() - _as_float(c.get("created_at"))), 1)
        out = dict(c)
        out.setdefault("schema_version", SCHEMA_VERSION)
        out["elapsed_secs"] = elapsed
        p = self._projects.get(c.get("project_id") or "")
        out["project"] = (p or {}).get("name", "")
        return out

    def cards(self, project_id: str | None = None) -> list[dict]:
        self._ensure_loaded()
        with self._lock:
            rows = [self._card_view(c) for c in self._cards.values()
                    if project_id is None or c.get("project_id") == project_id]
        rows.sort(key=lambda r: (
            _STATUS_ORDER.get(r.get("status"), len(STATUSES)),
            _PRIORITY_ORDER.get(r.get("priority"), len(PRIORITIES)),
            _as_float(r.get("created_at")), r.get("id", "")))
        return rows

    def columns(self, project_id: str | None = None) -> list[dict]:
        """Five columns in board order: ``[{name, count, cards}]``."""
        self._ensure_loaded()
        cards = self.cards(project_id)
        out = []
        for status in STATUSES:
            column_cards = [c for c in cards if c.get("status") == status]
            out.append({"name": status, "count": len(column_cards),
                        "cards": column_cards})
        return out

    def table_rows(self, project_id: str | None = None) -> list[dict]:
        """Canonical ``TABLE_COLUMNS`` rows for the table view / renderer."""
        rows = []
        for c in self.cards(project_id):
            rows.append({
                "id": c.get("id", ""),
                "project": c.get("project", ""),
                "task": c.get("title", ""),
                "status": c.get("status", ""),
                "note": c.get("notes", ""),
                "owner": c.get("owner", ""),
                "priority": c.get("priority", ""),
            })
        return rows

    def render_table(self, project_id: str | None = None) -> str:
        return render_table(self.table_rows(project_id))

    def add_card(self, project_id: str, title: str, *, writer: bool = False,
                 **fields) -> dict:
        self._require_writer(writer)
        self._ensure_loaded()
        with self._lock:
            pid = str(project_id)
            if pid not in self._projects:
                raise KeyError(f"unknown project {project_id!r}")
            now = time.time()
            self._seq["c"] += 1
            cid = f"c{self._seq['c']}"
            status = str(fields.get("status") or BACKLOG)
            if status not in _STATUS_ORDER:
                status = BACKLOG
            priority = str(fields.get("priority") or _DEFAULT_PRIORITY)
            if priority not in _PRIORITY_ORDER:
                priority = _DEFAULT_PRIORITY
            self._cards[cid] = {
                "schema_version": SCHEMA_VERSION,
                "id": cid,
                "project_id": pid,
                "title": _one_line(title, TITLE_MAX) or "Untitled card",
                "area": _one_line(fields.get("area", ""), 60),
                "owner": _one_line(fields.get("owner", "") or "You", 60),
                "priority": priority,
                "needs": _one_line(fields.get("needs", ""), NOTE_MAX),
                "notes": _one_line(fields.get("notes", ""), NOTE_MAX),
                "status": status,
                "created_at": now,
                "updated_at": now,
                "source": str(fields.get("source") or SOURCE_USER),
                "live": bool(fields.get("live", False)),
                "agent_id": str(fields.get("agent_id") or ""),
            }
            self._save_locked()
            return self._card_view(self._cards[cid])

    def update_card(self, card_id: str, *, writer: bool = False,
                    **fields) -> dict | None:
        self._require_writer(writer)
        self._ensure_loaded()
        with self._lock:
            c = self._cards.get(str(card_id))
            if c is None:
                return None
            for key in ("title", "area", "owner", "needs", "notes", "source"):
                if key in fields and fields[key] is not None:
                    limit = TITLE_MAX if key == "title" else NOTE_MAX
                    if key == "source":
                        c[key] = str(fields[key])
                    else:
                        c[key] = _one_line(fields[key], limit)
            if "priority" in fields and fields["priority"]:
                c["priority"] = (str(fields["priority"])
                                 if str(fields["priority"]) in _PRIORITY_ORDER
                                 else _DEFAULT_PRIORITY)
            if "status" in fields and fields["status"]:
                status = str(fields["status"])
                if status not in _STATUS_ORDER:
                    return None
                c["status"] = status
            c["updated_at"] = time.time()
            self._save_locked()
            return self._card_view(c)

    def move_card(self, card_id: str, status: str, *,
                  writer: bool = False) -> dict | None:
        self._require_writer(writer)
        status = str(status or "")
        if status not in _STATUS_ORDER:
            return None
        return self.update_card(card_id, status=status, writer=True)

    def remove_card(self, card_id: str, *, writer: bool = False) -> bool:
        self._require_writer(writer)
        self._ensure_loaded()
        with self._lock:
            if self._cards.pop(str(card_id), None) is None:
                return False
            self._save_locked()
            return True

    # -- live agent work -------------------------------------------------
    def _project_for_repo(self, repo: str) -> str:
        """Resolve a Project from a repo folder path, else the first Project."""
        repo = str(repo or "").strip()
        if repo:
            try:
                target = str(Path(repo).expanduser().resolve())
            except (OSError, RuntimeError):
                target = repo
            for pid, p in self._projects.items():
                pr = str(p.get("repo") or "").strip()
                if not pr:
                    continue
                try:
                    same = str(Path(pr).expanduser().resolve()) == target
                except (OSError, RuntimeError):
                    same = pr == repo
                if same:
                    return pid
        if self._projects:
            return next(iter(self._projects))
        return ""

    def upsert_live(self, agent_id: str, title: str, note: str = "",
                    repo: str = "", *, writer: bool = False) -> dict:
        """Make live agent work appear on the board (and update on each note)."""
        self._require_writer(writer)
        self._ensure_loaded()
        with self._lock:
            agent_id = str(agent_id or "").strip() or "agent"
            pid = self._project_for_repo(repo)
            if not pid:
                # No project to attach to: open a home for live work.
                now = time.time()
                self._seq["p"] += 1
                pid = f"p{self._seq['p']}"
                self._projects[pid] = {
                    "schema_version": SCHEMA_VERSION, "id": pid,
                    "name": "Live work", "note": "", "repo": str(repo or ""),
                    "created_at": now, "updated_at": now,
                }
            existing = next((c for c in self._cards.values()
                             if c.get("agent_id") == agent_id and c.get("live")), None)
            now = time.time()
            if existing is None:
                self._seq["c"] += 1
                cid = f"c{self._seq['c']}"
                self._cards[cid] = {
                    "schema_version": SCHEMA_VERSION,
                    "id": cid,
                    "project_id": pid,
                    "title": _one_line(title, TITLE_MAX) or "agent work",
                    "area": "",
                    "owner": _agent_name(agent_id),
                    "priority": _DEFAULT_PRIORITY,
                    "needs": "",
                    "notes": _one_line(note),
                    "status": IN_PROGRESS,
                    "created_at": now,
                    "updated_at": now,
                    "source": SOURCE_AGENT,
                    "live": True,
                    "agent_id": agent_id,
                }
                card = self._cards[cid]
            else:
                card = existing
                if title:
                    card["title"] = _one_line(title, TITLE_MAX)
                if note:
                    card["notes"] = _one_line(note)
                card["owner"] = _agent_name(agent_id)
                card["status"] = IN_PROGRESS
                card["live"] = True
                card["updated_at"] = now
            self._save_locked()
            return self._card_view(card)

    def clear_live(self, agent_id: str, *, writer: bool = False) -> bool:
        """Remove the live card for *agent_id* (when its work is verified)."""
        self._require_writer(writer)
        self._ensure_loaded()
        with self._lock:
            agent_id = str(agent_id or "").strip()
            for cid, c in list(self._cards.items()):
                if c.get("live") and c.get("agent_id") == agent_id:
                    del self._cards[cid]
                    self._save_locked()
                    return True
            return False

    # -- the brain's one-liner -------------------------------------------
    def summary_line(self) -> str:
        """ONE short line the brain can always use; never "".

        e.g. ``2 projects: Jarvis - 3 in progress, 8 waiting on you; Cal
        Matters - 4 backlog``.
        """
        self._ensure_loaded()
        projects = self.projects()
        n = len(projects)
        if n == 0:
            return "No projects yet."
        parts = []
        for p in projects:
            counts = [(s, p["counts"].get(s, 0)) for s in STATUSES
                      if p["counts"].get(s)]
            detail = ", ".join(f"{c} {s.lower()}" for s, c in counts) or "no cards"
            parts.append(f"{p['name']} - {detail}")
        return f"{n} project{'s' if n != 1 else ''}: " + "; ".join(parts)


# Module singleton the server, the live-task sync and the screen share.
board = Board()
