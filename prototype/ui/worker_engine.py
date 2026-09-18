"""Native multi-agent coding engine (replaces opencode_tool.py + opencode serve).

Implements the agent loop (model → tool_calls → local execution → loop) against
the zen API directly. No local opencode process required.

Design:
  - zen is OpenAI-compatible chat/completions (confirmed)
  - Tool calls are executed locally via stdlib (subprocess, pathlib, httpx)
  - Permission gating: bash/write/edit ask the user; read/glob/grep allowed
  - Session store in-memory: list_sessions() + session_messages() replace serve polling

Public API mirrors opencode_tool.py so server.py swaps imports only.
"""

import glob
import json
import logging
import os
import pathlib
import re
import subprocess
import threading
import time
import uuid
from typing import Callable

import httpx

import connector_setup  # noqa: E402  # user-key store pattern (0600 JSON)
import jarvis_paths  # noqa: E402  # app data dir (never the bundle/repo)
from agent_loop import THIRD_PARTY_RULE  # noqa: E402  # standing rule, no cycle
from providers import Provider as _Provider  # noqa: E402
from providers import ProviderError as _ProviderError  # noqa: E402
from providers import CONFIG as _PROVIDER_CONFIG  # noqa: E402
from providers import get_provider as _get_provider  # noqa: E402
from providers import registry as _provider_registry  # noqa: E402
from providers.openai_compatible import (  # noqa: E402
    OpenAICompatibleProvider,
    OpenCodeProvider,
)

try:  # optional, additive session persistence — a broken import must never matter
    import session_persist
except Exception:  # noqa: BLE001
    session_persist = None

logger = logging.getLogger("asha.worker")

# ── Defaults (mirror opencode_tool.py) ───────────────────────────────────────

ZEN_BASE = "https://opencode.ai/zen/v1"
GO_BASE = "https://opencode.ai/zen/go/v1"
DEFAULT_MODEL_ID = "big-pickle"
DEFAULT_PROVIDER_ID = "opencode"
DEFAULT_AGENT = "build"
DEFAULT_PERMISSION_TIMEOUT = 60.0
GRACE_AFTER_DENY = 90.0
WORKER_BUDGET = float(os.environ.get("WORKER_BUDGET_SEC", "900"))
MAX_AGENT_ROUNDS = int(os.environ.get("MAX_AGENT_ROUNDS", "60"))

GO_RATES = {
    "mimo-v2.5": (0.14, 0.28),
}
GO_FALLBACK_TABLE = {
    "mimo-v2.5-free": "mimo-v2.5",
}
UNIVERSAL_GO_FALLBACK = "mimo-v2.5"
FORBIDDEN_MUSCLE_SUBSTRINGS = ("deepseek",)

_PROPOSE_DIRECTIVE = (
    "\n\n(REPLY WITH A PLAN ONLY: what you WOULD change (files + steps) and why. "
    "Do NOT edit, create, or delete any files. After the plan, reply STOP.)"
)

SYSTEM_PROMPT = (
    "You are Jarvis's coding agent. You work inside the user's repository. "
    "Read files before modifying them. Edit precisely — minimal, surgical changes. "
    "Never commit. Never modify config/env/scripts without explicit instruction. "
    "Be concise in your final summary.\n" + THIRD_PARTY_RULE
)


# ── Tool definitions (OpenAI function-calling format) ────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a shell command and return its output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                    "timeout_sec": {"type": "integer", "description": "Timeout in seconds (default 30)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a file and return its contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Create or overwrite a file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write"},
                    "content": {"type": "string", "description": "Full file content"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "Replace an exact string in a file with a new string.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "old_string": {"type": "string", "description": "Exact text to find (must be unique)"},
                    "new_string": {"type": "string", "description": "Replacement text"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern (e.g. **/*.py)"},
                    "path": {"type": "string", "description": "Directory to search in (default cwd)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents for a regex pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern"},
                    "path": {"type": "string", "description": "File or directory to search"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "webfetch",
            "description": "Fetch a URL and return its text content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                },
                "required": ["url"],
            },
        },
    },
]

# Tools that require user permission before execution
_TOOLS_ASK = {"bash", "write", "edit"}


# ── Errors ───────────────────────────────────────────────────────────────────

class WorkerEngineError(RuntimeError):
    pass


class WorkerFailed(WorkerEngineError):
    def __init__(self, reason: str, session_id: str = "", prompt_message_id: str = ""):
        super().__init__(f"worker failed: {reason}")
        self.reason = reason
        self.session_id = session_id
        self.prompt_message_id = prompt_message_id


# ── NativeSession ────────────────────────────────────────────────────────────

class NativeSession:
    """In-memory session with activity feed (mirrors opencode serve's session shape).

    `messages` holds opencode-style message dicts (collected into a queue for
    both server.py pollers):
      {id, role, content: [parts], finish, model, tokens}
    where a part is either
      {"type": "text", "text": "..."} or
      {"type": "tool", "name": "bash", "state": {"status": "running"|"completed"|"error",
                                                 "input": {...}, "content": [{"text": ...}]}}
    """

    def __init__(self, session_id: str, agent: str, model: str, provider_id: str,
                 reasoning: str = "", project: str = ""):
        self.id = session_id
        self.agent = agent
        self.model = model
        self.provider_id = provider_id
        self.reasoning = reasoning or ""      # "default" | none|low|medium|high
        self.project = project or ""          # per-worker workspace root
        self.title = agent
        self.status = "running"  # running | completed | error
        self.created_ms = int(time.time() * 1000)
        self.updated_ms = self.created_ms
        self.messages: list[dict] = []  # opencode-style messages (both pollers read this)
        self.llm_messages: list[dict] = []  # LLM-format history (role/content/tool_calls)
        self.finish = ""  # "stop" | "error" | ""
        self.tokens = {"input": 0, "output": 0}
        self.cost = 0.0
        self.cancel = threading.Event()
        self.denied: list[str] = []
        self._lock = threading.Lock()

    def add_event(self, ev: dict):
        """Append a single opencode-style message event to the pollable queue."""
        with self._lock:
            self.messages.append({
                "id": f"msg_{len(self.messages):04d}_{self.id[:8]}",
                "role": "assistant",
                "content": [ev],
                "finish": "",
                "model": self.model,
                "tokens": dict(self.tokens),
                "time": {"updated": int(time.time() * 1000)},
            })
            self.updated_ms = int(time.time() * 1000)

    def append_llm(self, msg: dict):
        with self._lock:
            self.llm_messages.append(msg)
            self.updated_ms = int(time.time() * 1000)

    def set_finish(self, finish: str):
        with self._lock:
            self.finish = finish
            self.status = "completed" if finish == "stop" else "error"
            if self.messages:
                self.messages[-1]["finish"] = finish
            self.updated_ms = int(time.time() * 1000)
        _persist_session(self)

    def to_state(self) -> dict:
        """JSON-safe snapshot for persistence (lock/Event are never serialized)."""
        with self._lock:
            return {
                "id": self.id,
                "agent": self.agent,
                "model": self.model,
                "provider_id": self.provider_id,
                "reasoning": self.reasoning,
                "project": self.project,
                "title": self.title,
                "status": self.status,
                "created_ms": self.created_ms,
                "updated_ms": self.updated_ms,
                "finish": self.finish,
                "tokens": _safe_json(self.tokens),
                "cost": self.cost,
                "messages": _safe_json(self.messages),
                "llm_messages": _safe_json(self.llm_messages),
            }

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "title": self.title,
                "agent": self.agent,
                "time": {"updated": self.updated_ms},
            }

    def messages_snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.messages)


# ── Session registry ─────────────────────────────────────────────────────────

_SESSIONS: dict[str, NativeSession] = {}
_REPO_ROOT: str = ""

# ── Session persistence (additive, fail-open) ─────────────────────────────────
# Sessions are still authoritative in ``_SESSIONS``; the store is a best-effort
# mirror so a dispatched session survives a restart. Every path is guarded.

_DATA_DIR: str = ""
_SESSION_STORE = None  # lazily built SessionStore; False = build failed once
SESSION_STORE_MAX = 50  # plain constant cap — no env var


def _safe_json(value, _depth: int = 0):
    """Best-effort JSON-safe conversion; unknown types fall back to ``str``."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if _depth > 20:
        return str(value)
    if isinstance(value, dict):
        return {str(k): _safe_json(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json(v, _depth + 1) for v in value]
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def _session_store():
    """Lazily build the session store; None when persistence is unavailable.

    Construction is attempted once; a failure is remembered (``False``) so we
    neither retry nor re-log on every call. Never raises.
    """
    global _SESSION_STORE
    if _SESSION_STORE is not None:
        return _SESSION_STORE or None
    if session_persist is None or not _DATA_DIR:
        return None
    try:
        _SESSION_STORE = session_persist.SessionStore(
            os.path.join(_DATA_DIR, "session_store")
        )
    except Exception as e:  # noqa: BLE001 - persistence is optional
        logger.warning(f"[SESSION] store unavailable: {e!r}")
        _SESSION_STORE = False
        return None
    return _SESSION_STORE


def _session_from_state(state: dict):
    """Rebuild a ``NativeSession`` from a persisted dict; None on any error.

    A session saved as ``running`` means the process died mid-run, so it is
    normalised to an error rather than presented as still running.
    """
    try:
        session = NativeSession(
            str(state["id"]),
            str(state.get("agent", DEFAULT_AGENT)),
            str(state.get("model", DEFAULT_MODEL_ID)),
            str(state.get("provider_id", DEFAULT_PROVIDER_ID)),
            reasoning=str(state.get("reasoning", "") or ""),
            project=str(state.get("project", "") or ""),
        )
        session.title = str(state.get("title", session.title) or session.title)
        try:
            session.created_ms = int(state.get("created_ms", session.created_ms))
            session.updated_ms = int(state.get("updated_ms", session.updated_ms))
        except (TypeError, ValueError):
            pass
        session.status = str(state.get("status", session.status) or session.status)
        session.finish = str(state.get("finish", "") or "")
        tokens = state.get("tokens")
        session.tokens = dict(tokens) if isinstance(tokens, dict) else {"input": 0, "output": 0}
        try:
            session.cost = float(state.get("cost", 0.0) or 0.0)
        except (TypeError, ValueError):
            session.cost = 0.0
        messages = state.get("messages")
        session.messages = list(messages) if isinstance(messages, list) else []
        llm_messages = state.get("llm_messages")
        session.llm_messages = list(llm_messages) if isinstance(llm_messages, list) else []
        if session.status == "running":
            session.status = "error"
            session.finish = "error"
        return session
    except Exception as e:  # noqa: BLE001 - corrupt rows are skipped
        logger.warning(f"[SESSION] cannot restore session: {e!r}")
        return None


def _persist_session(session) -> None:
    """Save a session to disk. Fail-open: logs and returns, never raises."""
    try:
        store = _session_store()
        if store is None:
            return
        store.save(session.id, session.to_state())
    except Exception as e:  # noqa: BLE001 - persistence is optional
        logger.warning(f"[SESSION] persist failed for {getattr(session, 'id', '?')}: {e!r}")


def _restore_sessions() -> int:
    """Load persisted sessions into memory, then cap the store. Never raises."""
    store = _session_store()
    if store is None:
        return 0
    try:
        ids = store.list_ids()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[SESSION] restore list failed: {e!r}")
        return 0
    restored = 0
    for session_id in ids:
        try:
            state = store.load(session_id)
            if not isinstance(state, dict):
                continue
            session = _session_from_state(state)
            if session is None:
                continue
            if session.id not in _SESSIONS:
                _SESSIONS[session.id] = session
                restored += 1
        except Exception as e:  # noqa: BLE001 - one bad row must not stop the rest
            logger.warning(f"[SESSION] restore failed for {session_id}: {e!r}")
    try:
        pruned = store.prune_oldest(SESSION_STORE_MAX)
        if pruned:
            logger.info(f"[SESSION] pruned {len(pruned)} old session(s)")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[SESSION] prune failed: {e!r}")
    return restored


def _set_repo_root(path: str):
    global _REPO_ROOT
    _REPO_ROOT = path


def get_repo_root() -> str:
    """Active project worktree — the single source of truth that the Brain
    (git diff/status for review) and all 3 workers (tool cwd, glob/grep base,
    git revert) read from. Set via set_active_project() / init_projects()."""
    return _REPO_ROOT


# Per-session tool root (thread-local): lets each worker run its tools in its
# OWN project without moving the global active project. Falls back to the
# global repo root when unset, so single-project behavior is unchanged.
_TOOL_ROOT = threading.local()


def _current_root() -> str:
    return getattr(_TOOL_ROOT, "root", "") or _REPO_ROOT


def _set_tool_root(path: str):
    _TOOL_ROOT.root = path or ""


def _clear_tool_root():
    _TOOL_ROOT.root = ""


# ── Project store ─────────────────────────────────────────────────────────────
# Mirrors opencode's project model: a persisted most-recently-used list of
# {worktree, name, updated_at} records; the top entry is the active project.
# One active project applies to the whole system — Brain + all 3 workers.

_PROJECTS_FILE: str = ""
_PROJECTS: list[dict] = []  # MRU first; each: {worktree, name, updated_at}
_PROJECT_CHOSEN: bool = False  # True once the user explicitly picks a project
_PROJECTS_MAX = 10
_PROJECTS_LOCK = threading.Lock()


def _set_data_dir(data_dir: str):
    global _PROJECTS_FILE, _DATA_DIR, _SESSION_STORE
    os.makedirs(data_dir, exist_ok=True)
    _DATA_DIR = data_dir
    _SESSION_STORE = None  # rebuild lazily against the new dir
    _PROJECTS_FILE = os.path.join(data_dir, "projects.json")
    _load_projects()


def _load_projects():
    global _PROJECTS, _PROJECT_CHOSEN
    rows: list[dict] = []
    chosen = False
    if _PROJECTS_FILE:
        try:
            with open(_PROJECTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                rows = data.get("projects") or []
                if "chosen" in data:
                    chosen = bool(data["chosen"])
                else:
                    # Legacy stores (pre-Phase 3) that already hold projects
                    # count as chosen — don't re-prompt users who have one.
                    chosen = bool(rows)
        except Exception:
            rows = []
    _PROJECTS = [
        p for p in rows
        if isinstance(p, dict) and p.get("worktree") and os.path.isdir(p["worktree"])
    ]
    # A stored project only counts as a real choice if the user made one.
    _PROJECT_CHOSEN = chosen and bool(_PROJECTS)


def _save_projects():
    if not _PROJECTS_FILE:
        return
    try:
        with open(_PROJECTS_FILE, "w", encoding="utf-8") as f:
            json.dump({"projects": _PROJECTS, "chosen": _PROJECT_CHOSEN}, f, indent=2)
    except Exception as e:
        logger.warning(f"[PROJECT] save failed: {e!r}")


def is_project_chosen() -> bool:
    """True once the user has explicitly picked a project (Phase 3 gate)."""
    with _PROJECTS_LOCK:
        return bool(_PROJECT_CHOSEN)


def mark_project_chosen():
    global _PROJECT_CHOSEN
    with _PROJECTS_LOCK:
        _PROJECT_CHOSEN = True
        _save_projects()



def _project_name(worktree: str) -> str:
    base = os.path.basename(os.path.normpath(worktree))
    return base or worktree


def list_projects() -> list[dict]:
    """Recent projects, MRU first, existing dirs only."""
    with _PROJECTS_LOCK:
        return list(_PROJECTS)


def get_active_project() -> dict:
    """{worktree, name} for the currently selected project."""
    root = get_repo_root()
    if not root:
        return {"worktree": "", "name": ""}
    return {"worktree": root, "name": _project_name(root)}


def set_active_project(path: str) -> dict:
    """Validate *path*, persist it MRU-first, and make it the active project
    for the Brain and every worker. Raises WorkerEngineError on bad input."""
    p = os.path.abspath(os.path.expanduser((path or "").strip()))
    if not p:
        raise WorkerEngineError("project path empty")
    if not os.path.isdir(p):
        raise WorkerEngineError(f"not a directory: {p}")
    global _PROJECTS, _PROJECT_CHOSEN
    with _PROJECTS_LOCK:
        row = {"worktree": p, "name": _project_name(p), "updated_at": time.time()}
        _PROJECTS = [r for r in _PROJECTS if r.get("worktree") != p]
        _PROJECTS.insert(0, row)
        _PROJECTS = _PROJECTS[:_PROJECTS_MAX]
        _PROJECT_CHOSEN = True
        _save_projects()
    _set_repo_root(p)
    return {"worktree": p, "name": _project_name(p)}


def init_projects(data_dir: str, default_root: str):
    """Boot hook: load the persisted project store and restore the last active
    project as the system-wide working folder. The fallback *default_root* is
    used as a working folder but does NOT count as a user choice (Phase 3:
    coding is gated until the user picks a project)."""
    _set_data_dir(data_dir)
    recent = list_projects()
    if recent:
        _set_repo_root(recent[0]["worktree"])
    elif default_root:
        _set_repo_root(default_root)
    _init_workers(data_dir)
    _restore_sessions()


# ── Per-worker config (the 3 muscles: project + model + reasoning) ────────────
# Each Work slot is an independent agent with its own folder, model, and
# thinking level. Persisted to workers.json; the Brain reads this roster to
# decide who does what.

_WORKERS_FILE: str = ""
_WORKERS: list[dict] = []
_WORKERS_LOCK = threading.Lock()
WORKER_COUNT = 3
# Default muscles (free opencode zen models) — user can change each one.
WORKER_DEFAULT_MODELS = ["big-pickle", "mimo-v2.5-free", "muse-spark-1.3-contributor-free"]
# Providers a worker may draw from (each has its own key/pool).
WORKER_PROVIDERS = ("opencode", "opencode-go", "openrouter", "openai",
                    "anthropic", "gemini", "groq", "xai", "local")
# The crew's names (light theme: ray · sun · star) — short, voice-friendly.
WORKER_NAMES = ["Kiran", "Ravi", "Tara"]


def worker_name(i: int) -> str:
    return WORKER_NAMES[i] if 0 <= i < len(WORKER_NAMES) else f"Worker {i + 1}"


def _init_workers(data_dir: str):
    global _WORKERS_FILE, _WORKERS
    _WORKERS_FILE = os.path.join(data_dir, "workers.json")
    rows: list = []
    try:
        with open(_WORKERS_FILE, "r", encoding="utf-8") as f:
            rows = (json.load(f) or {}).get("workers") or []
    except Exception:
        rows = []
    out: list[dict] = []
    for i in range(WORKER_COUNT):
        row = rows[i] if i < len(rows) and isinstance(rows[i], dict) else {}
        prov = (row.get("provider") or "opencode").strip()
        out.append({
            # "" = follow the active project (resolved at use time), so workers
            # never get stuck on a stale folder after the user switches projects.
            "project": row.get("project") or "",
            "provider": prov if prov in WORKER_PROVIDERS else "opencode",
            "model": row.get("model") or WORKER_DEFAULT_MODELS[i],
            "reasoning": row.get("reasoning") or "default",
        })
    _WORKERS = out


def _save_workers():
    if not _WORKERS_FILE:
        return
    try:
        with open(_WORKERS_FILE, "w", encoding="utf-8") as f:
            json.dump({"workers": _WORKERS}, f, indent=2)
    except Exception as e:
        logger.warning(f"[WORKER] save failed: {e!r}")


def _effective_project(w: dict) -> str:
    """A worker with no pinned project follows the active project."""
    return w.get("project") or get_repo_root()


def list_workers() -> list[dict]:
    with _WORKERS_LOCK:
        out = []
        for w in _WORKERS:
            d = dict(w)
            d["project"] = _effective_project(w)
            d["project_pinned"] = bool(w.get("project"))
            out.append(d)
        return out


def set_worker(index: int, *, project: str | None = None,
               provider: str | None = None, model: str | None = None,
               reasoning: str | None = None) -> dict:
    """Update one worker's config. Raises WorkerEngineError on bad input."""
    if not (0 <= index < len(_WORKERS)):
        raise WorkerEngineError(f"worker index out of range: {index}")
    with _WORKERS_LOCK:
        w = _WORKERS[index]
        if project is not None:
            raw = (project or "").strip()
            if raw in ("", "auto"):
                w["project"] = ""
            else:
                p = os.path.abspath(os.path.expanduser(raw))
                if not os.path.isdir(p):
                    raise WorkerEngineError(f"not a directory: {p}")
                # Choosing the active project means "follow it", not "pin".
                w["project"] = "" if p == get_repo_root() else p
        if provider is not None:
            pv = (provider or "").strip()
            if pv in WORKER_PROVIDERS:
                w["provider"] = pv
        if model is not None and model.strip():
            w["model"] = model.strip()
        if reasoning is not None:
            lv = reasoning.strip().lower()
            w["reasoning"] = lv if lv in ("default", "none", "low", "medium", "high") else "default"
        _save_workers()
        return dict(w)


def list_sessions() -> list[dict]:
    """Return active sessions (mirrors GET /api/session shape)."""
    cutoff_ms = (time.time() - 3600) * 1000
    return [s.snapshot() for s in _SESSIONS.values() if s.created_ms >= cutoff_ms]


def session_messages(session_id: str) -> list[dict]:
    """Return messages for a session (mirrors GET /api/session/{id}/message shape).

    Each message has:
      - id, role
      - content: list of parts (type:"tool" | type:"text")
      - finish: "stop" | "error" | ""
      - tokens, cost, model
    """
    s = _SESSIONS.get(session_id)
    if not s:
        return []
    return s.messages_snapshot()


def _register_session(session: NativeSession):
    _SESSIONS[session.id] = session
    logger.info(f"[ENGINE] session {session.id} registered ({session.model})")
    _persist_session(session)


def _unregister_session(session_id: str):
    _SESSIONS.pop(session_id, None)
    try:
        store = _session_store()
        if store is not None:
            store.delete(session_id)
    except Exception as e:  # noqa: BLE001 - persistence is optional
        logger.warning(f"[SESSION] delete failed for {session_id}: {e!r}")


# ── Tool executors ───────────────────────────────────────────────────────────

def _exec_bash(command: str, timeout_sec: int = 30) -> str:
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout_sec, cwd=_current_root() or None,
        )
        out = result.stdout + result.stderr
        return out[:8000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout_sec}s"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


def _exec_read(path: str) -> str:
    try:
        p = pathlib.Path(path)
        if not p.exists():
            return f"ERROR: file not found: {path}"
        if p.stat().st_size > 200_000:
            return f"ERROR: file too large ({p.stat().st_size} bytes)"
        return p.read_text(errors="replace")[:8000]
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


def _exec_write(path: str, content: str) -> str:
    try:
        p = pathlib.Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


def _exec_edit(path: str, old_string: str, new_string: str) -> str:
    try:
        p = pathlib.Path(path)
        if not p.exists():
            return f"ERROR: file not found: {path}"
        text = p.read_text(errors="replace")
        count = text.count(old_string)
        if count == 0:
            return f"ERROR: old_string not found in {path}"
        if count > 1:
            return f"ERROR: old_string found {count} times — must be unique"
        new_text = text.replace(old_string, new_string, 1)
        p.write_text(new_text)
        return f"edited {path}"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


def _exec_glob(pattern: str, path: str = "") -> str:
    try:
        base = path or _current_root() or "."
        matches = glob.glob(os.path.join(base, pattern), recursive=True)
        matches.sort(key=lambda f: os.path.getmtime(f) if os.path.exists(f) else 0, reverse=True)
        return "\n".join(matches[:100]) if matches else "(no matches)"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


def _exec_grep(pattern: str, path: str = "") -> str:
    try:
        base = pathlib.Path(path or _current_root() or ".")
        compiled = re.compile(pattern)
        matches = []
        if base.is_file():
            files = [base]
        else:
            files = [f for f in base.rglob("*") if f.is_file() and ".git" not in f.parts][:2000]
        for fp in files:
            try:
                text = fp.read_text(errors="replace")
                for i, line in enumerate(text.splitlines(), 1):
                    if compiled.search(line):
                        matches.append(f"{fp}:{i}: {line[:120]}")
                        if len(matches) >= 100:
                            return "\n".join(matches)
            except Exception:
                continue
        return "\n".join(matches) if matches else "(no matches)"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


def _exec_webfetch(url: str) -> str:
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "asha-worker/1.0"})
            text = resp.text[:8000]
            return text
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


_TOOL_EXECUTORS: dict[str, Callable] = {
    "bash": lambda args: _exec_bash(args.get("command", ""), args.get("timeout_sec", 30)),
    "read": lambda args: _exec_read(args.get("path", "")),
    "write": lambda args: _exec_write(args.get("path", ""), args.get("content", "")),
    "edit": lambda args: _exec_edit(args.get("path", ""), args.get("old_string", ""), args.get("new_string", "")),
    "glob": lambda args: _exec_glob(args.get("pattern", ""), args.get("path", "")),
    "grep": lambda args: _exec_grep(args.get("pattern", ""), args.get("path", "")),
    "webfetch": lambda args: _exec_webfetch(args.get("url", "")),
}


def _tool_resource(tool: str, args: dict) -> str:
    """Human-readable resource description for permission asks."""
    if tool == "bash":
        return args.get("command", "")[:120]
    if tool in ("write", "edit"):
        return args.get("path", "")
    if tool == "read":
        return args.get("path", "")
    if tool == "webfetch":
        return args.get("url", "")[:120]
    return str(args)[:120]


# ── Agent BYOK: the USER's own key for coding delegation ─────────────────────
# The agent (coding delegation) runs on the user's own key, never ours. The
# user picks ONE provider — opencode or openrouter — and supplies their own API
# key. The key lives in the app data dir (0600, via connector_setup.KeyStore),
# never in .env and never in the shipped bundle. Brain/voice keep the server-
# side key resolved elsewhere; nothing here reads or writes that path.

AGENT_PROVIDERS = ("opencode", "openrouter")
DEFAULT_AGENT_PROVIDER = "openrouter"

# Base URL and default model per provider. OpenCode's free zen tier is
# client-gated (measured 403 FreeTierError for a plain key, headers and all),
# so the opencode route targets the paid go endpoint our client headers can
# reach. OpenRouter accepts a plain key, so its free coding model is the
# working default the other way.
AGENT_BASE_URL = {
    "opencode": GO_BASE,
    "openrouter": "https://openrouter.ai/api/v1",
}
AGENT_DEFAULT_MODEL = {
    "opencode": "mimo-v2.5",
    "openrouter": "cohere/north-mini-code:free",
}
AGENT_PROVIDER_LABEL = {"opencode": "OpenCode", "openrouter": "OpenRouter"}
# The env slot each provider's public key normally lives in. Only used to tell
# the user where to also look; the agent never reads it (see below).
AGENT_KEY_ENV = {"opencode": "OPENCODE_API_KEY", "openrouter": "OPENROUTER_API_KEY"}

AGENT_KEYS_FILENAME = "agent_keys.json"
_AGENT_CONFIG_KEY = "config"
# Registry id prefix for the user-key provider instance injected below.
AGENT_PROVIDER_ID = "agent-byok"


def _agent_provider(provider_id: str) -> str:
    pid = (provider_id or "").strip().lower()
    if pid not in AGENT_PROVIDERS:
        raise WorkerEngineError(
            f"unknown agent provider {provider_id!r}; choose opencode or openrouter"
        )
    return pid


def agent_key_store() -> connector_setup.KeyStore:
    """The agent BYOK store — the same 0600 KeyStore pattern as connectors."""
    return connector_setup.KeyStore(jarvis_paths.data_dir() / AGENT_KEYS_FILENAME)


def get_agent_config(store_=None) -> dict:
    """The user's agent provider choice + optional model override."""
    rec = (store_ or agent_key_store()).get(_AGENT_CONFIG_KEY) or {}
    if not isinstance(rec, dict):
        rec = {}
    provider = str(rec.get("provider") or DEFAULT_AGENT_PROVIDER).strip().lower()
    if provider not in AGENT_PROVIDERS:
        provider = DEFAULT_AGENT_PROVIDER
    return {"provider": provider, "model": str(rec.get("model") or "").strip()}


def set_agent_config(provider: str | None = None, model: str | None = None,
                     store_=None) -> dict:
    """Persist the agent provider choice and/or model override."""
    st = store_ or agent_key_store()
    cfg = get_agent_config(st)
    if provider is not None:
        cfg["provider"] = _agent_provider(provider)
    if model is not None:
        cfg["model"] = (model or "").strip()
    st.set(_AGENT_CONFIG_KEY, cfg)
    return cfg


def get_agent_key(provider_id: str, store_=None) -> str:
    """The stored user key for *provider_id* ("" when none). Never logged."""
    pid = _agent_provider(provider_id)
    rec = (store_ or agent_key_store()).get(pid) or {}
    if not isinstance(rec, dict):
        return ""
    return str(rec.get("secret") or "").strip()


def save_agent_key(provider_id: str, secret: str, store_=None) -> dict:
    """Store the user's own agent key (0600). The value is never logged."""
    pid = _agent_provider(provider_id)
    secret = (secret or "").strip()
    if not secret:
        raise WorkerEngineError("no agent key to save")
    (store_ or agent_key_store()).set(pid, {"secret": secret})
    return {"provider": pid, "saved": True, "configured": True}


def clear_agent_key(provider_id: str, store_=None) -> bool:
    """Remove the user's stored key for a provider. Returns True if one existed."""
    pid = _agent_provider(provider_id)
    return (store_ or agent_key_store()).delete(pid)


def agent_not_configured_message(provider_id: str = "") -> str:
    """One plain sentence for the honest failure when no user key exists."""
    pid = (provider_id or "").strip().lower()
    label = AGENT_PROVIDER_LABEL.get(pid, AGENT_PROVIDER_LABEL[DEFAULT_AGENT_PROVIDER])
    return (f"No {label} key is configured for the agent. Add your own "
            f"{label} API key in Settings to use coding.")


def resolve_agent_provider(provider: str | None = None, model: str | None = None,
                           store_=None) -> dict:
    """Resolve the agent provider, base URL, model and the USER's key.

    Order: explicit arguments > stored config > provider defaults. ``configured``
    is False when no user key is stored, and ``message`` then holds the one
    plain sentence to show. The key is for internal use only — callers must
    never log or surface it.
    """
    st = store_ or agent_key_store()
    cfg = get_agent_config(st)
    pid = _agent_provider(provider or cfg["provider"] or DEFAULT_AGENT_PROVIDER)
    override = (model if model is not None else cfg["model"]) or ""
    override = override.strip()
    key = get_agent_key(pid, st)
    out = {
        "provider": pid,
        "base_url": AGENT_BASE_URL[pid],
        "model": override or AGENT_DEFAULT_MODEL[pid],
        "model_overridden": bool(override),
        "api_key": key,
        "configured": bool(key),
    }
    if not key:
        out["message"] = agent_not_configured_message(pid)
    return out


def build_agent_provider(resolved: dict | None = None) -> _Provider | None:
    """Build a provider bound to the USER's key (None when none is stored).

    Built directly, not from env, so a server-side key can never shadow it: the
    opencode route always uses the go base URL with the stored key, regardless
    of any SUPERVISOR_API_KEY / OPENCODE_API_KEY in the environment.
    """
    r = resolved or resolve_agent_provider()
    if not r.get("configured"):
        return None
    if r["provider"] == "opencode":
        p = OpenCodeProvider(api_key=r["api_key"])
        p.base_url = r["base_url"]
        return p
    p = OpenAICompatibleProvider(api_key=r["api_key"])
    p.provider_id = "openrouter"
    p.name = "OpenRouter"
    p.base_url = r["base_url"]
    return p


def register_agent_provider(resolved: dict | None = None) -> str:
    """Make the user-key provider available to the shared provider registry.

    Returns the provider id agent_loop/worker_engine should use, or "" when no
    user key is stored. The injected instance is the ONLY source of the key on
    this path — the registry is never asked to resolve an env key for it.
    """
    r = resolved or resolve_agent_provider()
    provider = build_agent_provider(r)
    if provider is None:
        return ""
    pid = f"{AGENT_PROVIDER_ID}:{r['provider']}"
    provider.provider_id = pid
    _provider_registry._INSTANCES[pid] = provider
    return pid


# ── Provider routing ─────────────────────────────────────────────────────────

def _provider_for(provider_id: str = DEFAULT_PROVIDER_ID):
    # A BYOK agent id rebuilds from the user's stored key, never from env.
    if str(provider_id).startswith(AGENT_PROVIDER_ID + ":"):
        provider = build_agent_provider()
        if provider is not None:
            provider.provider_id = provider_id
            return provider
    try:
        return _get_provider(provider_id)
    except ValueError as e:
        raise WorkerEngineError(f"unknown provider {provider_id!r}: {e}")


def _chat_completion(messages: list, model: str, tools: list | None,
                     session_id: str, timeout: float = 300,
                     provider_id: str = DEFAULT_PROVIDER_ID,
                     reasoning: str = "") -> dict:
    """One chat completion turn routed through the native provider layer.

    Every provider returns the OpenAI chat.completions shape, so the extractors
    (text / tool_calls / usage) in the agent loop stay provider-agnostic.
    """
    provider = _provider_for(provider_id)
    if not provider.is_configured():
        raise WorkerEngineError(f"{provider.name}: no API key configured")
    try:
        body = provider.chat(messages, model, tools, session_id=session_id,
                             timeout=timeout, reasoning=reasoning)
    except _ProviderError as e:
        raise WorkerEngineError(str(e))
    return body


def _go_chat_completion(messages: list, model: str, session_id: str,
                        timeout: float = 60) -> dict:
    """Paid go-twin turn (answer-only) via the opencode-go provider."""
    provider = _provider_for("opencode-go")
    if not provider.is_configured():
        raise WorkerEngineError("opencode-go: no API key configured")
    try:
        body = provider.chat(messages, model, None, session_id=session_id, timeout=timeout)
    except _ProviderError as e:
        raise WorkerEngineError(str(e))
    return body


# ── Agent loop ───────────────────────────────────────────────────────────────

def _extract_text(body: dict) -> str:
    """Extract assistant text from chat completion response."""
    choices = body.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    return msg.get("content") or ""


def _extract_tool_calls(body: dict) -> list[dict]:
    """Extract tool_calls from chat completion response."""
    choices = body.get("choices") or []
    if not choices:
        return []
    msg = choices[0].get("message") or {}
    return msg.get("tool_calls") or []


def _extract_usage(body: dict) -> dict:
    usage = body.get("usage") or {}
    return {
        "input": usage.get("prompt_tokens", 0),
        "output": usage.get("completion_tokens", 0),
    }


def _run_agent_loop(
    session: NativeSession,
    user_text: str,
    on_permission: Callable | None = None,
    permission_timeout: float = DEFAULT_PERMISSION_TIMEOUT,
    grace_after_deny: float = GRACE_AFTER_DENY,
    cancel: threading.Event | None = None,
    budget: float = WORKER_BUDGET,
    propose_mode: bool = False,
) -> dict:
    """Core agent loop: send → tool_calls → execute → loop → done.

    Returns the result shape matching opencode_tool.py's contract.
    """
    # Run this worker's tools in ITS project (falls back to the global active
    # project when the session has none).
    _set_tool_root(getattr(session, "project", "") or "")
    system_prompt = SYSTEM_PROMPT
    prompt_text = user_text
    if propose_mode:
        prompt_text += _PROPOSE_DIRECTIVE

    llm_messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt_text},
    ]
    session.append_llm({"role": "user", "content": prompt_text})

    # Push the user prompt to the UI feed
    session.add_event({"type": "text", "text": prompt_text[:300]})
    session.add_event({"type": "reasoning", "text": "agent started"})
    session.append_llm({"role": "reasoning", "content": "agent started"})

    all_denied: list[str] = []
    start = time.monotonic()
    last_body = {}
    rounds = 0

    while rounds < MAX_AGENT_ROUNDS:
        # Budget check
        elapsed = time.monotonic() - start
        if elapsed > budget:
            logger.info(f"[ENGINE] session {session.id} budget exhausted ({budget}s)")
            break
        if cancel and cancel.is_set():
            logger.info(f"[ENGINE] session {session.id} cancelled")
            break

        rounds += 1
        try:
            body = _chat_completion(llm_messages, session.model, TOOLS, session.id,
                                    provider_id=session.provider_id,
                                    reasoning=session.reasoning)
        except WorkerEngineError as e:
            if "429" in str(e):
                raise WorkerFailed("rate-limited", session.id)
            raise WorkerFailed(str(e), session.id)

        last_body = body
        usage = _extract_usage(body)
        session.tokens["input"] += usage.get("input", 0)
        session.tokens["output"] += usage.get("output", 0)

        choice = (body.get("choices") or [{}])[0]
        finish = choice.get("finish_reason") or ""
        msg = choice.get("message") or {}
        assistant_content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []

        if assistant_content:
            session.add_event({"type": "text", "text": assistant_content[:300]})

        # No tool calls → agent is done
        if not tool_calls:
            session.append_llm({"role": "assistant", "content": assistant_content})
            break

        # Append assistant message with tool_calls to history
        llm_messages.append({
            "role": "assistant",
            "content": assistant_content or None,
            "tool_calls": tool_calls,
        })
        session.append_llm({"role": "assistant", "content": assistant_content or "",
                            "tool_calls": tool_calls})

        # Execute each tool call
        for tc in tool_calls:
            call_id = tc.get("id", "")
            func = tc.get("function", {})
            tool_name = func.get("name", "")
            try:
                args = json.loads(func.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}

            resource = _tool_resource(tool_name, args)

            # Emit activity: tool running
            input_meta = {"path": args.get("path", "")}
            if tool_name == "bash":
                input_meta["command"] = args.get("command", "")[:120]
            session.add_event({
                "type": "tool",
                "name": tool_name,
                "state": {"status": "running", "input": input_meta, "content": []},
            })

            # Permission check
            if tool_name in _TOOLS_ASK:
                permitted = True
                if on_permission is not None:
                    req = {"action": tool_name, "resources": [resource]}
                    try:
                        decision = on_permission(req)
                    except Exception as e:
                        logger.warning(f"[ENGINE] permission callback error: {e}")
                        decision = "reject"
                    if decision != "once":
                        permitted = False
                else:
                    permitted = False

                if not permitted:
                    result_text = f"Permission denied for {tool_name} ({resource}). Task cannot proceed without this access."
                    all_denied.append(f"{tool_name}({resource})")
                    session.add_event({
                        "type": "tool",
                        "name": tool_name,
                        "state": {
                            "status": "error",
                            "input": input_meta,
                            "content": [{"type": "text", "text": "permission denied"}],
                        },
                    })
                    llm_messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": result_text,
                    })
                    continue

            # Execute tool
            executor = _TOOL_EXECUTORS.get(tool_name)
            if not executor:
                result_text = f"ERROR: unknown tool: {tool_name}"
            else:
                try:
                    result_text = executor(args)
                except Exception as e:
                    result_text = f"ERROR: {type(e).__name__}: {e}"

            # Truncate result for LLM context
            llm_result = result_text[:6000]

            # Emit activity: tool completed
            session.add_event({
                "type": "tool",
                "name": tool_name,
                "state": {
                    "status": "completed",
                    "input": input_meta,
                    "content": [{"type": "text", "text": llm_result[:200]}],
                },
            })

            # Append tool result to messages
            llm_messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": llm_result,
            })

        # End of round: mirror partial progress so a crash still leaves a record.
        _persist_session(session)

    # Compute result
    text = _extract_text(last_body) if last_body else ""
    tokens = {"input": session.tokens["input"], "output": session.tokens["output"]}

    status = "done"
    if all_denied:
        status = "blocked"

    return {
        "session_id": session.id,
        "prompt_message_id": f"prompt_{session.id}",
        "message_id": f"msg_{uuid.uuid4().hex[:12]}",
        "text": text,
        "tokens": tokens,
        "cost": 0.0,
        "model_id": session.model,
        "provider_id": session.provider_id,
        "status": status,
        "permissions_denied": list(all_denied),
        "tier": "free",
    }


# ── Git revert support ───────────────────────────────────────────────────────

def _git_head() -> str:
    """Get current git HEAD commit."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=_current_root() or None, timeout=5,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _git_diff_files() -> list[dict]:
    """Get files changed since HEAD with status."""
    try:
        r = subprocess.run(
            ["git", "diff", "--name-status", "HEAD"],
            capture_output=True, text=True, cwd=_current_root() or None, timeout=10,
        )
        files = []
        for line in r.stdout.strip().splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                status_code, path = parts
                status_map = {"M": "modified", "A": "added", "D": "deleted",
                              "R": "renamed", "C": "copied"}
                files.append({"path": path, "status": status_map.get(status_code, status_code)})
        return files
    except Exception:
        return []


# ── Public API ───────────────────────────────────────────────────────────────

def is_coding_request(text: str) -> bool:
    """True when a request should engage the coding / file tools.

    Barrier removed (user directive: functionality first, code like opencode):
    no narrow magic-word gate — fire whenever the text names code artifacts OR
    starts with an action verb, so inspection/review requests ("check the code
    of the app") reach the coder too.
    """
    if not text:
        return False
    t = text.lower().strip()
    context_words = (
        "code", "repo", "repository", "project", "file", "folder", "directory",
        "function", "class", "module", "bug", "error", "exception", "test",
        "script", "endpoint", "api", "app", "database", "import", "config",
        "package", "dependency", "refactor", "commit", "branch", "readme",
        ".py", ".js", ".ts", ".html", ".css", ".json",
    )
    if any(w in t for w in context_words):
        return True
    strong_verbs = ("fix", "refactor", "debug", "implement", "unit test",
                    "run the tests", "run tests", "pull up")
    if any(t.startswith(v) for v in strong_verbs):
        return True
    phrases = ("write function", "write code", "write script", "add endpoint",
               "add a file", "make a file", "pull up", "show me the")
    return any(p in t for p in phrases)


def dispatch(
    brief: str,
    model_id: str | None = None,
    agent: str = DEFAULT_AGENT,
    provider_id: str | None = None,
    timeout: float = 600.0,
    poll_interval: float = 3.0,
    on_session: Callable | None = None,
    on_permission: Callable | None = None,
    permission_timeout: float = DEFAULT_PERMISSION_TIMEOUT,
    grace_after_deny: float = GRACE_AFTER_DENY,
    cancel: threading.Event | None = None,
    worker_budget: float = WORKER_BUDGET,
    reasoning: str = "",
    project: str = "",
) -> dict:
    """Dispatch a coding brief to a worker agent; wait for the final answer.

    Defaults to the system provider/model config (providers.CONFIG worker
    slot); any explicit model_id/provider_id overrides it.

    Result shape matches opencode_tool.py: {session_id, prompt_message_id,
    message_id, text, tokens, cost, model_id, provider_id, status, permissions_denied, tier}.
    """
    if not brief or not brief.strip():
        raise WorkerEngineError("dispatch: empty brief")

    if not provider_id:
        resolved = resolve_agent_provider()
        if not resolved["configured"]:
            raise WorkerEngineError(resolved["message"])
        provider_id = register_agent_provider(resolved) or resolved["provider"]
        if not model_id:
            model_id = resolved["model"]
    if not model_id:
        model_id = _PROVIDER_CONFIG.worker_model or DEFAULT_MODEL_ID

    session_id = uuid.uuid4().hex
    prompt_message_id = f"prompt_{session_id}"

    session = NativeSession(session_id, agent, model_id, provider_id,
                            reasoning=reasoning, project=project)
    session.title = brief[:60].split("\n")[0]
    _register_session(session)

    if on_session is not None:
        on_session(session_id, prompt_message_id)

    try:
        result = _run_agent_loop(
            session, brief.strip(),
            on_permission=on_permission,
            permission_timeout=permission_timeout,
            grace_after_deny=grace_after_deny,
            cancel=cancel,
            budget=worker_budget,
            propose_mode=False,
        )
        # Mark session complete
        session.set_finish("stop")
        return result
    except WorkerFailed as wf:
        session.set_finish("error")
        if str(session.provider_id).startswith(AGENT_PROVIDER_ID + ":"):
            # BYOK agent traffic never falls back to a server-side key.
            raise WorkerEngineError(wf.reason)
        # Go-twin fallback (answer-only, no tools)
        twin = GO_FALLBACK_TABLE.get(model_id, UNIVERSAL_GO_FALLBACK)
        logger.info(f"[ENGINE] worker failed ({wf.reason}), trying go twin {twin}")
        try:
            go_result = _go_direct_attempt(brief.strip(), twin, timeout, cancel, session_id)
            go_result["session_id"] = session_id
            go_result["prompt_message_id"] = prompt_message_id
            return go_result
        except WorkerEngineError as ge:
            raise WorkerEngineError(f"go twin {twin} also failed: {ge}")
    except Exception as e:
        session.status = "error"
        raise


def propose(
    brief: str,
    model_id: str | None = None,
    agent: str = DEFAULT_AGENT,
    provider_id: str | None = None,
    timeout: float = 600.0,
    poll_interval: float = 3.0,
    on_session: Callable | None = None,
    on_permission: Callable | None = None,
    permission_timeout: float = DEFAULT_PERMISSION_TIMEOUT,
    grace_after_deny: float = GRACE_AFTER_DENY,
    cancel: threading.Event | None = None,
    worker_budget: float = WORKER_BUDGET,
    reasoning: str = "",
    project: str = "",
) -> dict:
    """Plan-only dispatch. Result status is 'proposed'."""
    if not brief or not brief.strip():
        raise WorkerEngineError("propose: empty brief")

    result = dispatch(
        brief, model_id=model_id, agent=agent, provider_id=provider_id,
        timeout=timeout, poll_interval=poll_interval, on_session=on_session,
        on_permission=on_permission, permission_timeout=permission_timeout,
        grace_after_deny=grace_after_deny, cancel=cancel,
        worker_budget=worker_budget, reasoning=reasoning, project=project,
    )
    if result.get("status") == "done":
        result["status"] = "proposed"
    return result


def followup(
    session_id: str,
    text: str,
    timeout: float = 600.0,
    poll_interval: float = 3.0,
    on_session: Callable | None = None,
    on_permission: Callable | None = None,
    permission_timeout: float = DEFAULT_PERMISSION_TIMEOUT,
    grace_after_deny: float = GRACE_AFTER_DENY,
    cancel: threading.Event | None = None,
) -> dict:
    """Continue a session with a follow-up prompt."""
    if not text or not text.strip():
        raise WorkerEngineError("followup: empty text")

    session = _SESSIONS.get(session_id)
    if not session:
        raise WorkerEngineError(f"session {session_id} not found")

    prompt_message_id = f"prompt_{uuid.uuid4().hex[:12]}"

    if on_session is not None:
        on_session(session_id, prompt_message_id)

    result = _run_agent_loop(
        session, text.strip(),
        on_permission=on_permission,
        permission_timeout=permission_timeout,
        grace_after_deny=grace_after_deny,
        cancel=cancel,
        budget=timeout,
        propose_mode=False,
    )
    session.set_finish("stop")
    result["prompt_message_id"] = prompt_message_id
    return result


def abort_session(session_id: str, timeout: float = 30.0) -> bool:
    """Cancel a running session. Returns True if cancelled."""
    session = _SESSIONS.get(session_id)
    if not session:
        return False
    if session.status != "running":
        return False
    session.cancel.set()
    session.set_finish("error")
    return True


def stage_revert(session_id: str, prompt_message_id: str,
                 timeout: float = 30.0) -> dict:
    """Stage a revert: snapshot files changed since HEAD."""
    files = _git_diff_files()
    return {"files": files, "messageID": prompt_message_id}


def commit_revert(session_id: str, timeout: float = 60.0) -> None:
    """Restore the worktree to pre-turn state using git checkout."""
    try:
        subprocess.run(
            ["git", "checkout", "HEAD", "--", "."],
            capture_output=True, text=True, cwd=_current_root() or None, timeout=timeout,
        )
    except Exception as e:
        raise WorkerEngineError(f"git checkout failed: {e}")


def revert_decision(files: list) -> tuple[bool, list]:
    """Safe revert rule: commit ONLY if every file is 'deleted' (created by session)."""
    files = files or []
    if not files:
        return True, []
    held = [f.get("path", "?") for f in files if f.get("status") != "deleted"]
    if held:
        return False, held
    return True, []


def stop_session(session_id: str, prompt_message_id: str,
                 timeout: float = 30.0) -> dict:
    """Abort + conditional revert. Returns {aborted, reverted, held_files}."""
    aborted = abort_session(session_id, timeout=timeout)
    staged = stage_revert(session_id, prompt_message_id, timeout=timeout)
    files = staged.get("files") or []
    commit, held = revert_decision(files)
    if not commit:
        logger.warning(f"[ENGINE] stop holds revert for user decision: {held}")
        return {"aborted": aborted, "reverted": False, "held_files": held}
    if files:
        commit_revert(session_id, timeout=timeout)
        removed = [f.get("path", "?") for f in files]
        logger.info(f"[ENGINE] stop reverted session files: {removed}")
    return {"aborted": aborted, "reverted": True, "held_files": []}


def _go_direct_attempt(brief: str, twin: str, timeout: float,
                       cancel: threading.Event | None = None,
                       session_id: str = "") -> dict:
    """One paid go-twin attempt (answer-only, no tools)."""
    if cancel is not None and cancel.is_set():
        raise WorkerEngineError("dispatch cancelled")
    go_session = session_id or uuid.uuid4().hex
    messages = [{"role": "user", "content": brief}]
    body = _go_chat_completion(messages, twin, go_session, timeout)
    text = _extract_text(body)
    usage = _extract_usage(body)

    # Check rate for cost
    rates = GO_RATES.get(twin, (0.14, 0.28))
    cost = (usage.get("input", 0) * rates[0] + usage.get("output", 0) * rates[1]) / 1_000_000

    return {
        "session_id": go_session,
        "prompt_message_id": f"prompt_{go_session}",
        "message_id": f"msg_{uuid.uuid4().hex[:12]}",
        "text": text,
        "tokens": usage,
        "cost": cost,
        "model_id": twin,
        "provider_id": GO_BASE,
        "status": "done",
        "permissions_denied": [],
        "tier": "paid-go",
    }
