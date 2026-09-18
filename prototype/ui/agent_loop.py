"""Native Jarvis agent loop (no OpenCode dependency).

This is the piece Jarvis was missing: the same shape as opencode's loop — send the
task + tools to the model, execute the tools it asks for, feed results back, and
repeat until the model stops calling tools. One continuous loop, clean context,
bounded steps. It reuses Jarvis's own model routing (`providers.chat`) and its own
tools (file ops, apply_patch, grep, bash, web search/fetch).

Used by the supervisor to test parity with opencode; later wired into the app so
work requests run through it (voice narrates the result).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

import apply_patch as ap
import hooks
import lsp_client
from providers import chat as provider_chat, get_provider

THIRD_PARTY_RULE = (
    "THIRD-PARTY RULE \u2014 before you write or change ANY code that talks to an "
    "outside service (API, OAuth provider, MCP server, SDK, CLI, cloud console), "
    "READ that vendor's OFFICIAL documentation first: web_fetch its docs or "
    "quickstart page and look for authentication, scopes, redirect URIs, "
    "allowlists, prerequisites, plan/approval requirements and rate limits. "
    "Then follow what it says. NEVER guess endpoints, scopes, redirect rules or "
    "auth flows, and never learn a provider's rules by trial-and-error against "
    "the live service \u2014 a 403, a login screen or a reachable endpoint is not a "
    "specification. If the docs say the integration needs an allowlist, vetting, "
    "partner approval or a paid/enterprise plan, say that BEFORE building, and "
    "tell the user the real blocker rather than an invented one. Name the doc "
    "you followed when you report back."
)

SYSTEM = (
    "You are Jarvis, a coding agent. You do the work yourself with your tools.\n"
    "You work in ONE project directory; run_bash executes there and file paths "
    "in tools are relative to it (run_bash prints its [cwd: ...] to confirm). "
    "Never cd around or reference absolute paths outside it.\n"
    "Orient once: if the task names its files, read them directly; otherwise "
    "use glob/repo_map to locate them. Do NOT also run ls/bash to orient "
    "after repo_map, and do not run both glob and repo_map.\n"
    "Work efficiently: use glob/grep to locate files, read ONLY the relevant "
    "ones (never re-read a file you already read). For many small files, use "
    "read_many (paths/glob) or a single run_bash (grep/cat/wc) instead of reading "
    "them one by one. Make independent tool calls in ONE reply so they run in "
    "parallel (e.g. write several new files, or read several files, together) "
    "instead of one call per turn. Do 2-5 web searches if you "
    "need outside info, then WRITE/edit with write_file / edit_file / "
    "apply_patch (full content, exact path), and VERIFY by actually running it "
    "with run_bash. After an edit, verify ONCE by running it — do not re-read "
    "the file or repeat a check that already passed. Run diagnostics on a "
    "Python file only when an edit might "
    "have broken it — the verification command is your main check. Prefer tools "
    "that are already available (stdlib, `python`); "
    "if something is missing (e.g. pytest), use an available alternative (e.g. "
    "plain `python -c` with asserts) instead of giving up. Only report success "
    "if the verification command actually succeeded \u2014 never claim tests pass "
    "without seeing them pass. Do not stop to ask; keep going until the work "
    "is done and verified.\n"
    "You decide which tools to use — the user never names them. Before answering, "
    "ask whether a tool would answer better or more surely than memory or a "
    "guess; if it would, call it now. If the answer is on the user's screen or in "
    "an image, use read_screen / look_at_image; if they hold something up to the "
    "camera, use look_through_camera; if they want a picture made, use "
    "generate_image. Look at the situation, not the exact words. Only ask when the "
    "goal is ambiguous or the action cannot be undone. "
    "When an agent you delegated to finishes, check its work yourself straight "
    "away and keep checking until the user is back — never wait to be asked, and "
    "never sit idle while a report is unverified. "
    "Never change anything unrelated to what you were asked to do: if you "
    "notice something odd elsewhere, report it to the user and leave it alone "
    "\u2014 working behaviour is not yours to 'improve' in passing. "
    "Delegating is normal: when work is long, self-contained or parallel, hand "
    "it to an agent with a proper brief and keep talking — never make the user "
    "ask for that.\n"
    "Keep the user's projects tracked on the board: when they mention work to "
    "do, add it to the right project; when they ask how things are going, read "
    "the board. Keep it current without being asked.\n"
    "Agents are your team. Any of them can do any task - the title only tells "
    "you who fits best, and it never excuses anyone from work. Hire an agent "
    "only when the user asks for one: propose the name, title and "
    "responsibilities, then wait for their yes.\n"
    + THIRD_PARTY_RULE
)

_N = {"type": "string"}


def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props,
                       "required": required}}}


def normalize_verify(value):
    """Normalise a delegate VERIFY argument into a list of command entries.

    The model may send a single command string, a list of command strings, or a
    list whose entries are argv lists (``["python3", "-c", "print(1)"]``). An
    argv entry is kept as a list so ``build_brief`` can quote/join it; a string
    entry is split on newlines so a multi-line command becomes one command per
    line instead of being mangled by the brief's bullet parser. ``None`` becomes
    an empty list. Empty entries are dropped.
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    elif not isinstance(value, (list, tuple)):
        value = [value]
    out = []
    for entry in value:
        if isinstance(entry, (list, tuple)):
            parts = [str(p).strip() for p in entry if str(p).strip()]
            if parts:
                out.append(parts)
        else:
            for line in str(entry).splitlines():
                line = line.strip()
                if line:
                    out.append(line)
    return out


TOOLS = [
    _fn("repo_map", "List the project's files (bounded tree) to orient yourself.",
        {"path": _N, "max_entries": {"type": "integer"}}, []),
    _fn("glob", "Find files by glob pattern, e.g. 'lib/services/*.dart'.",
        {"pattern": _N}, ["pattern"]),
    _fn("grep", "Regex-search file contents; returns path:line: text. context=N "
        "adds N surrounding lines; files_only=true lists just matching files.",
        {"pattern": _N, "path": _N, "glob": _N, "context": {"type": "integer"},
         "files_only": {"type": "boolean"}}, ["pattern"]),
    _fn("read_file", "Read a file (optionally a line range).",
        {"path": _N, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
        ["path"]),
    _fn("read_many", "Read MANY small files in ONE call (paths list and/or a glob). "
        "Prefer this over many read_file calls when inspecting lots of small files.",
        {"paths": {"type": "array", "items": _N}, "glob": _N}, []),
    _fn("write_file", "Create/overwrite a file with the FULL content.",
        {"path": _N, "content": _N}, ["path", "content"]),
    _fn("edit_file", "Replace an exact substring (fuzzy fallback) in a file. "
        "Pass replace_all=true to replace every occurrence.",
        {"path": _N, "old_string": _N, "new_string": _N,
         "replace_all": {"type": "boolean"}},
        ["path", "old_string", "new_string"]),
    _fn("apply_patch", "Apply an OpenAI-style multi-file patch (*** Begin Patch ...).",
        {"patch": _N}, ["patch"]),
    _fn("move_file", "Move/rename a file within the project.",
        {"src": _N, "dst": _N}, ["src", "dst"]),
    _fn("delete_file", "Delete a file within the project.",
        {"path": _N}, ["path"]),
    _fn("run_bash", "Run a shell command in the project (verify your work).",
        {"command": _N}, ["command"]),
    _fn("run_python", "Run a Python snippet in the project and return its output "
        "(no shell; capped at 30s / 10MB). Prefer this over `run_bash python -c` "
        "for quick checks.",
        {"code": _N, "timeout": {"type": "integer"}}, ["code"]),
    _fn("diagnostics", "Compile + lint a Python file (errors/warnings) after editing.",
        {"path": _N}, ["path"]),
    _fn("lsp", "Language server: action 'symbols' (file outline), 'definition', "
        "'references' (find all usages), or 'hover' (definition/references/hover "
        "need line+character, 1-based).",
        {"action": _N, "path": _N, "line": {"type": "integer"},
         "character": {"type": "integer"}}, ["action", "path"]),
    _fn("mcp_list_tools", "List tools from configured MCP servers.",
        {"server": _N}, []),
    _fn("mcp_call", "Call a tool on a configured MCP server (arguments: object).",
        {"server": _N, "tool": _N, "arguments": {"type": "object"}},
        ["server", "tool"]),
    _fn("task", "Delegate a self-contained sub-task to a fresh agent; only its "
        "summary returns (keeps your context clean). Give a clear goal; "
        "optionally pass context (facts/files to hand off) and tools "
        "('read' = research only, default; 'all' = may also write/edit). "
        "Issue several task calls in one reply to run them in parallel.",
        {"goal": _N, "context": _N,
         "tools": {"type": "string", "enum": ["read", "all"]}}, ["goal"]),
    _fn("delegate", "Hand a piece of work to one of your permanent agents. Write "
        "the brief yourself (goal, the files it may touch, what not to do, how "
        "to verify, what to report). Use it for work that is long, self-contained "
        "or parallel — then keep talking to the user; the agent's result comes "
        "back to you and you report it. Agents cannot talk to the user and must "
        "never be given instructions they could not verify.",
        {"goal": _N, "files": {"type": "array", "items": _N},
         "do_not": {"type": "array", "items": _N},
         "verify": {"type": "array", "items": _N},
         "agent": _N, "model": _N,
         "title": {"type": "string",
                   "description": "Prefer an agent with this title; any agent "
                   "may still take it"}}, ["goal"]),
    _fn("agents_status", "What each of your agents is doing right now: agent, "
        "brief title, status, elapsed time, last note and any verdict/reasons. "
        "Only live work is listed; a task disappears as soon as it is verified.",
        {}, []),
    _fn("team", "Manage the agent team. Actions: 'list' shows every agent with "
        "name, title, responsibilities and status; 'counts' shows title -> "
        "count; 'hire' proposes a new agent (requires name, title, "
        "responsibilities — does NOT create yet); 'confirm' creates the "
        "proposed agent; 'retire' deactivates a named agent.",
        {"action": {"type": "string", "enum": ["list", "counts", "hire",
                                               "confirm", "retire"]},
         "name": _N, "title": _N,
         "responsibilities": {"type": "array", "items": _N}}, []),
    _fn("projects", "Read the user's Projects board (the bodies of work they "
        "are tracking). action 'list' shows project names with per-column "
        "counts and the cards in progress / waiting on you; 'projects' lists "
        "just the names and card counts. Read-only for agents — only Jarvis "
        "adds or changes cards.",
        {"action": {"type": "string", "enum": ["list", "projects"]},
         "project": _N, "card_id": _N}, []),
    _fn("todo", "Track a multi-step task's plan. Pass the FULL items list (each "
        "{text, status} with status pending|in_progress|done; at most one "
        "in_progress). Call with no items to view the current list.",
        {"items": {"type": "array", "items": {
            "type": "object", "properties": {"text": _N, "status": {
                "type": "string", "enum": ["pending", "in_progress", "done"]}},
            "required": ["text"]}}}, []),
    _fn("skill_list", "List Jarvis's saved reusable skills (how-to playbooks).",
        {}, []),
    _fn("skill_get", "Read a saved skill's full steps by name.",
        {"name": _N}, ["name"]),
    _fn("skill_save", "Save a reusable skill you just worked out, for future "
        "tasks: name, a one-line description (<=60 chars), and the how-to body.",
        {"name": _N, "description": _N, "body": _N}, ["name", "description", "body"]),
    _fn("web_search", "Search the web; returns LLM-ready snippets with URLs.",
        {"query": _N, "num_results": {"type": "integer"}}, ["query"]),
    _fn("web_fetch", "Fetch a URL and return its readable text.",
        {"url": _N, "max_chars": {"type": "integer"}}, ["url"]),
    _fn("generate_image",
        "Create an image file from a text prompt and return its path. Use when "
        "the user asks for a picture, drawing, illustration or visual.",
        {"prompt": _N, "width": {"type": "integer"},
         "height": {"type": "integer"}, "seed": {"type": "integer"}},
        ["prompt"]),
    _fn("make_video",
        "Start a short stock-footage video from a topic in the background and "
        "return at once. Supply a one-or-two sentence script and 3-6 "
        "comma-separated footage keywords. The finished video arrives later.",
        {"topic": _N, "script": _N, "terms": _N,
         "aspect": {"type": "string", "enum": ["9:16", "16:9", "1:1"]},
         "voice": _N},
        ["topic"]),
    _fn("read_screen",
        "See what is on the user's screen right now. Captures the screen and "
        "reads it: on-device OCR of every text, plus a vision model when you "
        "pass a question (use for designs, charts, images, bugs on screen). "
        "Needs Screen Recording permission for the Jarvis app; if the capture "
        "comes back empty, tell the user to grant it.",
        {"question": _N, "window": {"type": "boolean"}}, []),
    _fn("look_at_image",
        "Look at an image file on disk (PNG/JPG screenshot, exported design, "
        "photo) and answer a question about it with a vision model.",
        {"path": _N, "question": _N}, ["path"]),
    _fn("look_through_camera",
        "Look through the user's camera right now and describe what it sees. "
        "Captures one frame and reads it: on-device OCR of any text, plus a "
        "vision model when you pass a question. Use it when the user refers to "
        "something in front of them ('what is this', a document, an object). "
        "If Camera permission is missing the result says how to grant it.",
        {"question": _N}, []),
]

_SKIP = {".git", "node_modules", "__pycache__", ".venv", "build", "dist"}
PLUGIN_DIR = Path(os.environ.get("JARVIS_DATA_DIR") or (Path(__file__).resolve().parents[1] / "data")) / "plugins"
_DEFAULT_SKILLS_DIR = Path(os.environ.get("JARVIS_DATA_DIR") or (Path(__file__).resolve().parents[1] / "data")) / "skills"
_SKILL_STORES: dict = {}


def _skills():
    """Skill store for the agent loop (isolated via AGENT_SKILLS_DIR in tests)."""
    import skills as skills_mod
    d = Path(os.environ.get("AGENT_SKILLS_DIR") or _DEFAULT_SKILLS_DIR)
    key = str(d)
    if key not in _SKILL_STORES:
        _SKILL_STORES[key] = skills_mod.SkillStore(d)
    return _SKILL_STORES[key]

# Hardline command guard (mirrors Hermes's unconditional-deny idea): refuse to
# run commands that destroy the machine or its filesystem wholesale. Normal
# work (build/test/git/rm -rf ./build) is unaffected. Override with
# AGENT_ALLOW_DANGEROUS=1 when the user explicitly wants a risky command.
_BASH_DENY = [
    (r"\brm\s+(-[a-zA-Z]+\s+)*-[a-zA-Z]*[rf][a-zA-Z]*\s+/(?:\s|\*|$)",
     "rm -rf /"),
    (r"\brm\s+(-[a-zA-Z]+\s+)*-[a-zA-Z]*[rf][a-zA-Z]*\s+(?:~|\$HOME)(?:\s|$)",
     "rm -rf home"),
    (r"\bmkfs(\.\w+)?\b", "mkfs (format a filesystem)"),
    (r"\bdd\b[^\n]*\bof=/dev/", "dd to a raw device"),
    (r">\s*/dev/(sd|disk|nvme|hd)", "overwrite a raw device"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "shutdown/reboot"),
    (r"\bchmod\s+-R\s+777\s+/(?:\s|$)", "chmod -R 777 /"),
    (r"\bchown\s+-R\s+[^\s]+\s+/(?:\s|$)", "chown -R /"),
]


def _blocked_bash(command: str) -> str:
    """Return the matched reason if *command* is hardline-destructive, else ''."""
    if os.environ.get("AGENT_ALLOW_DANGEROUS") == "1":
        return ""
    for pattern, reason in _BASH_DENY:
        if re.search(pattern, command or ""):
            return reason
    return ""


# ── secret redaction (secret_scope) — shared module (also used by the logger) ─
from redact import redact as _redact  # noqa: E402


# ── narrow-waist tool gating: don't ship schemas whose prerequisite is absent ─
def _mcp_configured() -> bool:
    try:
        data = json.loads((Path(__file__).resolve().parents[1] / "data"
                           / "mcp.json").read_text(encoding="utf-8"))
        return bool(data.get("servers"))
    except Exception:  # noqa: BLE001
        return False


def _tool_enabled(name: str) -> bool:
    """Omit a tool when it could only error (Hermes 'narrow waist'): MCP tools
    with no servers configured, web_search with no search API key."""
    if name in ("mcp_list_tools", "mcp_call"):
        return _mcp_configured()
    if name == "web_search":
        return bool(os.environ.get("EXA_API_KEY"))
    return True


def _truncate(text: str, limit: int) -> str:
    """Head+tail truncation with an explicit elision marker: big outputs lose
    the least useful middle instead of everything after the cut (errors and
    summaries usually live at the END)."""
    if limit <= 0 or len(text) <= limit:
        return text
    keep = max(0, limit - 120)
    head = keep * 2 // 3
    tail = keep - head
    omitted = len(text) - head - tail
    marker = f"\n... [{omitted} chars omitted of {len(text)}] ...\n"
    return text[:head] + marker + (text[-tail:] if tail else "")


def _looks_binary(q: Path) -> bool:
    """True when the first KB contains a NUL byte (likely a binary file)."""
    try:
        with q.open("rb") as f:
            return b"\x00" in f.read(1024)
    except OSError:
        return False


def _expand_braces(pattern: str) -> list[str]:
    """Expand one level of shell-style braces, e.g. '*.{py,pyi}' ->
    ['*.py', '*.pyi'] (recursively). Python's glob has no brace support."""
    m = re.search(r"\{([^{}]*)\}", pattern or "")
    if not m:
        return [pattern]
    pre, post = pattern[:m.start()], pattern[m.end():]
    return [out for opt in m.group(1).split(",")
            for out in _expand_braces(pre + opt + post)]


class _Sandbox:
    def __init__(self, root: str):
        self.root = Path(root).expanduser().resolve()

    def path(self, p: str) -> Path | None:
        q = Path(p)
        if not q.is_absolute():
            q = self.root / q
        try:
            q.resolve().relative_to(self.root)
            return q
        except ValueError:
            return None


_MCP_MGR = None


def _get_mcp():
    global _MCP_MGR
    if _MCP_MGR is None:
        import mcp_client
        _MCP_MGR = mcp_client.MCPManager(
            Path(__file__).resolve().parents[1] / "data" / "mcp.json")
    return _MCP_MGR


def _make_exec(root: Path, touched: set | None = None, on_task=None,
               read_only: bool = False):
    box = _Sandbox(str(root))
    touched = touched if touched is not None else set()

    def mcp_list_tools(server: str = "") -> str:
        try:
            tools = _get_mcp().list_tools(server or None)
        except Exception as e:
            return f"MCP error: {e}"
        if not tools:
            return "No MCP servers configured."
        return "\n".join(
            (f"{t.get('server')}: ERROR {t['error']}" if t.get("error")
             else f"{t['server']}.{t.get('name', '')}: {t.get('description', '')}")
            for t in tools)

    def mcp_call(server: str, tool: str, arguments: dict) -> str:
        try:
            import mcp_client
            res = _get_mcp().call_tool(server, tool, arguments or {})
            return mcp_client.tool_text(res) or "(no text content)"
        except Exception as e:
            return f"MCP error: {e}"

    def read(p: str, offset: int = 0, limit: int = 0) -> str:
        q = box.path(p)
        if q is None:
            return f"Error: path outside project: {p}"
        if q.is_dir():
            names = sorted(x.name + ("/" if x.is_dir() else "")
                           for x in q.iterdir() if x.name not in _SKIP)
            listed = "\n".join(names[:200]) or "(empty)"
            return f"{p or '.'}/ (directory):\n{listed}"
        if not q.is_file():
            return f"Error: file not found: {p}"
        if _looks_binary(q):
            return f"Error: {p} looks like a binary file (not shown)."
        text = q.read_text(encoding="utf-8", errors="replace")
        if offset or limit:
            lines = text.splitlines()
            s = max(0, offset - 1) if offset else 0
            e = s + limit if limit else len(lines)
            body = "\n".join(f"{s+i+1}\t{ln}" for i, ln in enumerate(lines[s:e]))
            return f"{body}\n... [lines {s+1}-{min(e, len(lines))} of {len(lines)}]"
        if len(text) > 30000:
            lines = text.count("\n") + 1
            return (_truncate(text, 30000)
                    + f"\n... [file is {len(text)} chars / {lines} lines — read a "
                      "range with offset+limit to see the rest]")
        return text

    def read_many(paths, glob_pat: str = "") -> str:
        """Read many small files in one call (paths list and/or a glob)."""
        names = list(paths or [])
        if glob_pat:
            names += [str(p.relative_to(box.root))
                      for pat in _expand_braces(glob_pat)
                      for p in box.root.glob(pat)
                      if p.is_file() and not any(x in p.parts for x in _SKIP)]
        if not names:
            return "Error: give a paths list and/or a glob"
        out, total = [], 0
        for p in names[:80]:
            q = box.path(p)
            if q is None or not q.is_file():
                out.append(f"### {p}\n(not found)")
                continue
            if _looks_binary(q):
                out.append(f"### {p}\n(binary file, skipped)")
                continue
            t = q.read_text(encoding="utf-8", errors="replace")
            out.append(f"### {p}\n{t}")
            total += len(t)
            if total > 40000:
                out.append("... (truncated: too many files)")
                break
        return "\n".join(out)

    def write(p: str, content: str) -> str:
        q = box.path(p)
        if q is None:
            return "Error: path outside project"
        content = content or ""
        existed = q.is_file()
        old = q.read_text(encoding="utf-8", errors="replace") if existed else ""
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_text(content, encoding="utf-8")
        touched.add(str(q.resolve()))
        if existed and old != content:
            import difflib
            diff = [d for d in difflib.unified_diff(
                old.splitlines(), content.splitlines(), n=0)]
            added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
            removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
            return f"Overwrote {p}: +{added}/-{removed} lines ({len(content)} chars)"
        return f"Wrote {len(content)} chars to {p}"

    def edit(p: str, old: str, new: str, replace_all: bool = False) -> str:
        q = box.path(p)
        if q is None or not q.is_file():
            return f"Error: file not found: {p}"
        text = q.read_text(encoding="utf-8", errors="replace")
        n = text.count(old)
        if n == 1 or (replace_all and n >= 1):
            q.write_text(text.replace(old, new), encoding="utf-8")
            touched.add(str(q.resolve()))
            return f"Edited {p} ({n} occurrence(s))."
        if n > 1:
            return (f"Error: old_string appears {n} times in {p}; make it unique "
                    "or pass replace_all=true.")
        try:
            updated = ap.apply_hunks(text, [ap.Hunk(old.splitlines(), new.splitlines())])
            q.write_text(updated, encoding="utf-8")
            touched.add(str(q.resolve()))
            return "Edited (fuzzy)."
        except Exception as e:
            return f"Error: old_string not found ({e})"

    def move_file(src: str, dst: str) -> str:
        s, d = box.path(src), box.path(dst)
        if s is None or d is None:
            return "Error: path outside project"
        if not s.is_file():
            return f"Error: file not found: {src}"
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))
        touched.add(str(s.resolve()))
        touched.add(str(d.resolve()))
        return f"Moved {src} -> {dst}"

    def delete_file(path: str) -> str:
        q = box.path(path)
        if q is None:
            return "Error: path outside project"
        if q.is_dir():
            return f"Error: {path} is a directory (delete files individually)."
        if not q.is_file():
            return f"Error: file not found: {path}"
        q.unlink()
        touched.add(str(q.resolve()))
        return f"Deleted {path}"

    def apply(patch: str) -> str:
        for m in re.finditer(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$",
                             patch or "", re.M):
            q = box.path(m.group(1).strip())
            if q is not None:
                touched.add(str(q.resolve()))
        def r(p): return read(p)
        def w(p, c): write(p, c)
        def d(p):
            q = box.path(p)
            if q and q.is_file():
                q.unlink()
        try:
            return ap.apply_patch_text(patch, r, w, d)
        except Exception as e:
            return f"Error applying patch: {e}"

    def glob(pattern: str) -> str:
        pat = pattern or "**/*"
        if Path(pat).is_absolute():
            return f"Error: use a project-relative glob (got {pat})"
        hits = set()
        for p in _expand_braces(pat):
            hits.update(str(f.relative_to(box.root)) for f in box.root.glob(p)
                        if f.is_file() and not any(x in f.parts for x in _SKIP))
        hits = sorted(hits)
        if not hits:
            return f"No files match {pattern}"
        if len(hits) > 300:
            return "\n".join(hits[:300]) + f"\n... [showing 300 of {len(hits)} files]"
        return "\n".join(hits)

    def grep(pattern: str, path: str = "", glob: str = "", context: int = 0,
             files_only: bool = False) -> str:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"Error: bad regex: {e}"
        base = box.path(path) if path else box.root
        if base is None:
            return "Error: path outside project"
        if base.is_file():
            files = [base]
        else:
            files = [f for pat in _expand_braces(glob or "**/*")
                     for f in base.glob(pat)]
        out: list[str] = []
        hits: set[str] = set()
        ctx = max(0, min(int(context or 0), 5))
        for f in files:
            if not f.is_file() or any(x in f.parts for x in _SKIP):
                continue
            if f.stat().st_size > 2_000_000 or _looks_binary(f):
                continue
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            rel = f.relative_to(box.root)
            for i, ln in enumerate(lines, 1):
                if not rx.search(ln):
                    continue
                if files_only:
                    hits.add(str(rel))
                    break
                if ctx:
                    lo, hi = max(0, i - 1 - ctx), min(len(lines), i + ctx)
                    for j in range(lo, hi):
                        sep = ":" if j == i - 1 else "-"
                        out.append(f"{rel}:{j+1}{sep} {lines[j][:200]}")
                else:
                    out.append(f"{rel}:{i}: {ln[:200]}")
                if len(out) >= 200:
                    return "\n".join(out)
        if files_only:
            return "\n".join(sorted(hits)) or f"No matches for {pattern!r}"
        return "\n".join(out) or f"No matches for {pattern!r}"

    def repo_map(path: str = "", max_entries: int = 250) -> str:
        base = box.path(path) if path else box.root
        if base is None:
            return "Error: path outside project"
        out = []
        for p in sorted(base.rglob("*")):
            rel = p.relative_to(box.root)
            if any(x in rel.parts for x in _SKIP) or len(rel.parts) > 4 or p.is_dir():
                continue
            out.append(str(rel))
            if len(out) >= max_entries:
                out.append("... (truncated)")
                break
        return "\n".join(out) or "(no files)"

    def bash(command: str) -> str:
        if read_only:
            return "Error: read-only mode — shell disabled."
        blocked = _blocked_bash(command)
        if blocked:
            return (f"Blocked by the command guard: {blocked}. This looks "
                    "destructive/irreversible. If you truly need it, explain "
                    "why; set AGENT_ALLOW_DANGEROUS=1 to override.")
        try:
            p = subprocess.run(command, shell=True, cwd=str(box.root),
                               capture_output=True, text=True, timeout=120)
            out = ((p.stdout or "") + (p.stderr or ""))[:8000] or "(no output)"
            return f"[cwd: {box.root} | exit {p.returncode}]\n{out}"
        except Exception as e:
            return f"Error: {e}"

    todo_state = {"items": [], "rev": 0}

    def todo(items=None) -> str:
        """Per-task checklist. The model passes the full list each time; at most
        one item may be in_progress."""
        if items is not None:
            if not isinstance(items, list):
                return "Error: items must be a list of {text, status}"
            clean, seen_prog = [], False
            for it in items:
                if isinstance(it, str):
                    text, status = it, "pending"
                elif isinstance(it, dict):
                    text = str(it.get("text", "")).strip()
                    status = str(it.get("status", "pending")).strip().lower()
                else:
                    continue
                if not text:
                    continue
                if status not in ("pending", "in_progress", "done"):
                    status = "pending"
                if status == "in_progress":
                    if seen_prog:
                        status = "pending"
                    seen_prog = True
                clean.append({"text": text, "status": status})
            todo_state["items"] = clean
            todo_state["rev"] += 1
        if not todo_state["items"]:
            return "Todo list is empty."
        marks = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
        lines = [f"Todo (rev {todo_state['rev']}):"]
        lines += [f"{marks[it['status']]} {i}. {it['text']}"
                  for i, it in enumerate(todo_state["items"], 1)]
        return "\n".join(lines)

    def projects_status(action: str = "list", project: str = "",
                        card_id: str = "") -> str:
        """READ-ONLY view of the Projects board.

        Agents and workers are strictly read-only (the store enforces it too);
        only Jarvis's own paths may add, update, move or remove a card.
        """
        try:
            import board as board_mod
        except Exception as e:  # noqa: BLE001
            return f"projects unavailable: {e}"
        store = board_mod.board
        action = (action or "list").strip().lower()
        if action not in ("list", "projects"):
            return ("Error: agents are read-only on the Projects board; only "
                    "Jarvis may add, update, move or remove cards.")
        projects = store.projects()
        if not projects:
            return "No projects yet."
        if action == "projects":
            return "; ".join(
                f"{p['name']} ({p['total']} cards)" for p in projects)
        lines = [store.summary_line()]
        for p in projects:
            cards = store.cards(p["id"])
            for col in ("In progress", "Waiting on you"):
                titles = [c["title"] for c in cards if c["status"] == col]
                if titles:
                    lines.append(f"{p['name']} {col.lower()}: "
                                 + ", ".join(titles[:8]))
        return "\n".join(lines)

    def agents_status() -> str:
        """Compact text of the live agent tasks (never empty)."""
        try:
            from tasks import tasks as _tasks

            rows = _tasks.snapshot()
        except Exception as e:  # noqa: BLE001
            return f"agents_status failed: {e}"
        if not rows:
            return "no agents working"
        lines = []
        for r in rows:
            line = (f"- {r['agent_name'] or r['agent_id']} [{r['status']}] "
                    f"{r.get('label') or r['title'] or '(untitled)'} "
                    f"({int(r['seconds'])}s)")
            if r["verdict"]:
                line += f" | verdict: {r['verdict']}"
            if r["reasons"]:
                line += " | " + "; ".join(str(x) for x in r["reasons"][:3])
            lines.append(line)
        return "\n".join(lines)

    def team(action: str = "list", name: str = "", title: str = "",
             responsibilities=None) -> str:
        """Manage the agent team (list, counts, hire, confirm, retire)."""
        try:
            from agents import registry
        except Exception as e:  # noqa: BLE001
            return f"team tool unavailable: {e}"
        action = (action or "list").strip().lower()
        if action == "list":
            rows = registry.snapshot()
            if not rows:
                return "No agents in the team."
            lines = []
            for r in rows:
                resp = ", ".join(r.get("responsibilities") or [])
                lines.append(
                    f"- {r['name']} - {r['title']} - {resp} [{r['status']}]")
            return "\n".join(lines)
        if action == "counts":
            counts = registry.team_counts()
            if not counts:
                return "No agents."
            return "\n".join(f"- {t}: {c}" for t, c in sorted(counts.items()))
        if action == "hire":
            name = (name or "").strip()
            title = (title or "").strip()
            resp = [r.strip() for r in (responsibilities or []) if r.strip()]
            if not name or not title or not resp:
                return ("Error: hire requires name, title, and at least one "
                        "responsibility.")
            try:
                proposal = registry.hire(name, title, resp)
                registry.pending_proposal = proposal
                return (f"New agent - Name: {name}, Title: {title}, "
                        f"Responsibilities: {', '.join(resp)}. "
                        "Say yes and I will add them to the team.")
            except ValueError as e:
                return f"Error: {e}"
        if action == "confirm":
            proposal = registry.pending_proposal
            if not proposal:
                return "Error: no pending hire proposal to confirm."
            try:
                registry.confirm(proposal)
                registry.pending_proposal = None
                return f"{proposal['name']} has been added to the team."
            except Exception as e:  # noqa: BLE001
                return f"Error confirming: {e}"
        if action == "retire":
            name = (name or "").strip()
            if not name:
                return "Error: retire requires the agent's name."
            aid = name.lower().replace(" ", "")
            if registry.retire(aid):
                return f"{name} has been retired from the team."
            return f"Error: no agent named {name!r}."
        return f"Error: unknown team action {action!r}"

    def skill_list() -> str:
        try:
            rows = _skills().list()
        except Exception as e:  # noqa: BLE001
            return f"Error: {e}"
        if not rows:
            return "No skills saved yet."
        return "\n".join(f"- {r['name']}: {r['description']}" for r in rows)

    def skill_get(name: str) -> str:
        try:
            body = _skills().get(name)
        except Exception as e:  # noqa: BLE001
            return f"Error: {e}"
        return body or f"No skill named {name!r}."

    def skill_save(name: str, description: str, body: str) -> str:
        if not (name or "").strip() or not (body or "").strip():
            return "Error: skill_save needs a name and a body."
        try:
            import skill_curate
            verdict = skill_curate.curate_skill(
                {"name": name, "description": description, "body": body,
                 "verified": True})  # agent-authored: trusted provenance
            if verdict.get("verdict") != "accept":
                return f"Skill rejected: {verdict.get('reason')}"
        except Exception:  # noqa: BLE001
            pass
        try:
            _skills().save(name, description, body, created_by="jarvis")
            return f"Saved skill {name!r} for future tasks."
        except Exception as e:  # noqa: BLE001
            return f"Error saving skill: {e}"

    def run_python(code: str, timeout: int = 30) -> str:
        if read_only:
            return "Error: read-only mode — code execution disabled."
        """Run a Python snippet in the project (no shell) with CPU/file-size
        rlimits and a wall timeout. Snippet is written to a temp file inside the
        project so imports and relative paths work."""
        timeout = max(1, min(int(timeout or 30), 120))
        fd, tmp = tempfile.mkstemp(suffix=".py", dir=str(box.root))
        os.close(fd)
        path = Path(tmp)
        path.write_text(code or "", encoding="utf-8")
        bootstrap = (
            "import resource\n"
            f"for _r, _l in (('RLIMIT_CPU', {timeout}), "
            "('RLIMIT_FSIZE', %d)):\n" % (10 * 1024 * 1024)
            + "    try: resource.setrlimit(getattr(resource, _r), (_l, _l))\n"
            + "    except Exception: pass\n")
        try:
            p = subprocess.run(
                [sys.executable, "-c",
                 bootstrap + "import sys; exec(compile(open(sys.argv[1]).read(),"
                             " sys.argv[1], 'exec'))", str(path)],
                cwd=str(box.root), capture_output=True, text=True, timeout=timeout)
            out = ((p.stdout or "") + (p.stderr or ""))[:8000] or "(no output)"
            return f"[python exit={p.returncode}]\n{out}"
        except subprocess.TimeoutExpired:
            return f"Error: run_python timed out after {timeout}s"
        except Exception as e:  # noqa: BLE001
            return f"Error: {e}"
        finally:
            try:
                path.unlink()
            except OSError:
                pass

    def diagnostics(p: str) -> str:
        q = box.path(p)
        if q is None or not q.is_file():
            return f"Error: file not found: {p}"
        if q.suffix != ".py":
            return "No diagnostics (python only)."
        import py_compile
        try:
            py_compile.compile(str(q), doraise=True)
        except py_compile.PyCompileError as e:
            return f"Syntax error:\n{e}"
        except Exception as e:
            return f"Error compiling: {e}"
        for tool, args in (("ruff", ["check", "--output-format", "concise",
                                     "--no-cache"]), ("pyflakes", [])):
            exe = shutil.which(tool)
            if not exe:
                continue
            try:
                r = subprocess.run([exe, *args, str(q)], capture_output=True,
                                   text=True, timeout=30)
            except Exception as e:
                return f"Error running {tool}: {e}"
            out = (r.stdout or r.stderr or "").strip()
            return out or f"No diagnostics ({tool} clean)."
        return "Clean (syntax)."

    def lsp(action: str, p: str, line: int = 1, character: int = 1) -> str:
        q = box.path(p)
        if q is None or not q.is_file():
            return f"Error: file not found: {p}"
        text = q.read_text(encoding="utf-8", errors="replace")
        try:
            out = lsp_client.lsp_action(str(q), str(box.root), text,
                                        action or "symbols", line, character)
        except Exception as e:
            return f"LSP error: {e}"
        return out or "LSP unavailable for this file."

    def web_search(query: str, num_results: int = 6) -> str:
        import httpx
        exa = os.environ.get("EXA_API_KEY", "")
        if not exa:
            return ("Web search needs an Exa API key to be set before I can look "
                    "anything up online.")
        try:
            r = httpx.post("https://api.exa.ai/search",
                           headers={"x-api-key": exa},
                           json={"query": query, "numResults": num_results,
                                 "type": "auto",
                                 "contents": {"text": {"maxCharacters": 1200}}},
                           timeout=25)
            r.raise_for_status()
            parts = [f"- {x.get('title')} ({x.get('url')}): "
                     f"{(x.get('text') or '')[:600]}"
                     for x in r.json().get("results", [])]
            return "\n".join(parts) or "No results found."
        except Exception:  # noqa: BLE001
            return "Web search failed. I could not reach Exa just now."

    def web_fetch(url: str, max_chars: int = 6000) -> str:
        import httpx
        try:
            r = httpx.get(url, timeout=25, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", r.text)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
            text = re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", text))
            return text[:max_chars]
        except Exception as e:
            return f"web_fetch failed: {e}"

    def read_screen(question: str = "", window: bool = False) -> str:
        try:
            import screen_tool

            return screen_tool.see_screen(question)
        except Exception as e:  # noqa: BLE001
            return f"read_screen failed: {e}"

    def look_at_image(path: str, question: str = "") -> str:
        try:
            import screen_tool

            return screen_tool.look(path, question)
        except Exception as e:  # noqa: BLE001
            return f"look_at_image failed: {e}"

    def look_through_camera(question: str = "") -> str:
        try:
            import camera_tool

            return camera_tool.look(question)
        except Exception as e:  # noqa: BLE001
            return f"look_through_camera failed: {e}"

    def generate_image(prompt: str, width: int = 512, height: int = 512,
                       seed: int = -1) -> str:
        try:
            import imagegen_tool

            out = imagegen_tool.generate_image(prompt, width, height, seed)
            if hasattr(out, "__await__"):
                import asyncio

                out = asyncio.run(out)
            return out
        except Exception as e:  # noqa: BLE001
            return f"generate_image failed: {e}"

    def make_video(topic: str, script: str = "", terms: str = "",
                   aspect: str = "9:16", voice: str = "en-US-AriaNeural") -> str:
        try:
            import mpt_video_tool

            out = mpt_video_tool.make_video(topic, aspect, voice, script, terms)
            if hasattr(out, "__await__"):
                import asyncio

                out = asyncio.run(out)
            return out
        except Exception as e:  # noqa: BLE001
            return f"make_video failed: {e}"

    def delegate(goal: str, files=None, do_not=None, verify=None,
                 agent: str = "", model: str = "", title: str = "") -> str:
        """Hand work to a permanent agent via the opencode CLI; return a short
        ack (never the log — the result comes back to the caller separately)."""
        goal = (goal or "").strip()
        if not goal:
            return "Error: delegate needs a goal."
        try:
            import agent_runner
            from agents import registry
        except Exception as e:  # noqa: BLE001
            return f"delegate unavailable: {e}"
        agent_id = (agent or "").strip() or registry.pick(title or "")
        row = next((r for r in registry.snapshot() if r["id"] == agent_id), None)
        name = (row or {}).get("name") or agent_id
        verify = normalize_verify(verify)
        brief = agent_runner.build_brief(goal=goal, files=files, do_not=do_not,
                                         verify=verify)
        try:
            run_obj = agent_runner.run(
                agent_id, brief, model=(model or "").strip(), workdir=str(root),
                title=goal.splitlines()[0][:80], files=files)
        except Exception as e:  # noqa: BLE001
            return f"Could not hand that to {name}: {e}"
        if run_obj.state != "running":
            return f"{name} is not available: {run_obj.note or 'not launched'}"
        return (f"handed to {name} ({run_obj.model}); log: {run_obj.log_path}. "
                "This may take a while — its result will come back to you; keep "
                "talking to the user.")

    return {
        "read_file": lambda a: read(a.get("path", ""), a.get("offset", 0), a.get("limit", 0)),
        "read_many": lambda a: read_many(a.get("paths") or [], a.get("glob", "")),
        "write_file": lambda a: write(a.get("path", ""), a.get("content", "")),        "edit_file": lambda a: edit(a.get("path", ""), a.get("old_string", ""),
                                    a.get("new_string", ""),
                                    a.get("replace_all", False)),
        "apply_patch": lambda a: apply(a.get("patch", "")),
        "move_file": lambda a: move_file(a.get("src", ""), a.get("dst", "")),
        "delete_file": lambda a: delete_file(a.get("path", "")),
        "glob": lambda a: glob(a.get("pattern", "**/*")),
        "grep": lambda a: grep(a.get("pattern", ""), a.get("path", ""),
                               a.get("glob", ""), a.get("context", 0),
                               a.get("files_only", False)),
        "repo_map": lambda a: repo_map(a.get("path", ""), a.get("max_entries", 250)),
        "run_bash": lambda a: bash(a.get("command", "")),
        "run_python": lambda a: run_python(a.get("code", ""), a.get("timeout", 30)),
        "todo": lambda a: todo(a.get("items")),
        "agents_status": lambda a: agents_status(),
        "team": lambda a: team(a.get("action", "list"), a.get("name", ""),
                               a.get("title", ""),
                               a.get("responsibilities") or []),
        "projects": lambda a: projects_status(a.get("action", "list"),
                                              a.get("project", ""),
                                              a.get("card_id", "")),
        "skill_list": lambda a: skill_list(),
        "skill_get": lambda a: skill_get(a.get("name", "")),
        "skill_save": lambda a: skill_save(a.get("name", ""),
                                           a.get("description", ""),
                                           a.get("body", "")),
        "diagnostics": lambda a: diagnostics(a.get("path", "")),
        "lsp": lambda a: lsp(a.get("action", "symbols"), a.get("path", ""),
                            a.get("line", 1), a.get("character", 1)),
        "mcp_list_tools": lambda a: mcp_list_tools(a.get("server", "")),
        "mcp_call": lambda a: mcp_call(a.get("server", ""), a.get("tool", ""),
                                       a.get("arguments") or {}),
        "task": lambda a: (on_task(a.get("goal", ""), a.get("context", ""),
                                   a.get("tools", "read")) if on_task
                           else "task tool unavailable"),
        "delegate": lambda a: delegate(a.get("goal", ""), a.get("files") or [],
                                       a.get("do_not") or [],
                                       a.get("verify") or [],
                                       a.get("agent", "") or "",
                                       a.get("model", "") or "",
                                       a.get("title", "") or ""),
        "web_search": lambda a: web_search(a.get("query", ""), a.get("num_results", 6)),
        "web_fetch": lambda a: web_fetch(a.get("url", ""), a.get("max_chars", 6000)),
        "generate_image": lambda a: generate_image(a.get("prompt", ""),
                                                   a.get("width", 512),
                                                   a.get("height", 512),
                                                   a.get("seed", -1)),
        "make_video": lambda a: make_video(a.get("topic", ""),
                                           a.get("script", ""),
                                           a.get("terms", ""),
                                           a.get("aspect", "9:16"),
                                           a.get("voice", "en-US-AriaNeural")),
        "read_screen": lambda a: read_screen(a.get("question", ""), a.get("window", False)),
        "look_at_image": lambda a: look_at_image(a.get("path", ""), a.get("question", "")),
        "look_through_camera": lambda a: look_through_camera(a.get("question", "")),
    }


def _stream_collect(provider, model: str, messages: list[dict], tools, on_token):
    """Assemble a full OpenAI-shaped response from a provider SSE stream,
    calling ``on_token(delta_text)`` for each content delta."""
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    calls: dict = {}
    usage: dict | None = None
    for chunk in provider.stream(messages, model, tools=tools, timeout=300):
        if chunk.get("usage"):
            usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        c = delta.get("content")
        if c:
            text_parts.append(c)
            if on_token:
                try:
                    on_token(c)
                except Exception:
                    pass
        r = delta.get("reasoning_content")
        if r:
            reasoning_parts.append(r)
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]
    tcs = [{"id": v["id"] or f"call_{i}", "type": "function",
            "function": {"name": v["name"], "arguments": v["arguments"]}}
           for i, v in sorted(calls.items())]
    msg: dict = {"role": "assistant", "content": "".join(text_parts)}
    if reasoning_parts:
        # thinking models (deepseek) require this echoed back on the next turn
        msg["reasoning_content"] = "".join(reasoning_parts)
    if tcs:
        msg["tool_calls"] = tcs
    resp: dict = {"choices": [{"message": msg,
                               "finish_reason": "tool_calls" if tcs else "stop"}]}
    if usage:
        resp["usage"] = usage
    return resp


def _chat_with_retry(provider_id: str, model: str, messages: list[dict], *,
                     retries: int = 3, tools=None, on_token=None) -> dict:
    """Chat completion (streaming when on_token is given) with backoff on
    transient failures (429/transport/timeout, and gateway 5xx / occasional
    "could not be processed" 400s)."""
    import time
    last: Exception | None = None
    use_tools = TOOLS if tools is None else tools

    def _call() -> dict:
        if on_token is not None:
            return _stream_collect(get_provider(provider_id), model, messages,
                                   use_tools, on_token)
        return provider_chat(provider_id, model, messages, tools=use_tools, timeout=300)

    for i in range(retries + 1):
        try:
            return _call()
        except Exception as e:  # noqa: BLE001
            last = e
            low = str(e).lower()
            if "reasoning_content" in low and i < retries:
                # thinking-mode gateways are finicky about echoing reasoning
                # back; retry once with it stripped (the other valid mode).
                for m in messages:
                    if isinstance(m, dict):
                        m.pop("reasoning_content", None)
                continue
            transient = (
                "429" in low or "rate" in low or "transport" in low
                or "timeout" in low or "timed out" in low
                or "could not be processed" in low or "upstream request failed" in low
                or "500" in low or "502" in low or "503" in low or "504" in low
                or "internal server" in low or "overloaded" in low)
            if i < retries and transient:
                time.sleep(1.5 * (i + 1))
                continue
            raise
    raise last if last else RuntimeError("chat failed")


_PLAN_SYSTEM = (
    "You are Jarvis in PLAN mode: research only. You may use read tools and web "
    "search/fetch, but you must NOT change any files. Investigate the project, "
    "then present a concrete step-by-step plan (what to change, in which files, "
    "and why), and stop. Do not call write/edit/patch."
)

# Sub-agent prompt: a delegated child gets a fresh context and must return only
# a concise summary (the parent never sees its intermediate tool calls).
SUBAGENT_SYSTEM = (
    "You are Jarvis working as a focused SUB-AGENT on ONE self-contained "
    "sub-task for a parent agent. Complete only that sub-task with your tools, "
    "then reply with a concise summary of the RESULT — the facts you found or "
    "the files you changed, not a play-by-play. Do not ask questions; if "
    "something is missing, state the assumption you made and proceed.\n"
    "You work in ONE project directory; run_bash executes there and file paths "
    "are relative to it. Orient once (glob/repo_map), read only relevant files "
    "(never re-read), batch independent calls in one reply, and verify by "
    "running the code."
)

# Delegation tree: depth 0 = the top-level agent. MAX_DEPTH caps nesting
# (Hermes-style flat default: parent -> child, child cannot spawn). Raise via
# AGENT_MAX_DEPTH for orchestrator children.
MAX_DEPTH = int(os.environ.get("AGENT_MAX_DEPTH", "1"))
MAX_SUMMARY_CHARS = int(os.environ.get("AGENT_SUBAGENT_SUMMARY_CHARS", "6000"))


def _subtask_kwargs(depth: int, tools: str) -> dict:
    """Resolve a delegated child's run_task kwargs from the parent's depth and
    the requested toolset ('read' | 'all'). Children may delegate further only
    while they stay under MAX_DEPTH (orchestrator role)."""
    return {"read_only": tools != "all",
            "allow_task": depth + 1 < MAX_DEPTH,
            "depth": depth + 1}


MAX_HISTORY_CHARS = int(os.environ.get("AGENT_CTX_MAX_CHARS", "180000"))
_COMPACT_MARK = "[Earlier steps compacted"


def _tool_brief(tc: dict) -> str:
    """'name(path-or-command)' — a compact hint of what a tool call did."""
    fn = tc.get("function") or {}
    name = fn.get("name", "?")
    try:
        a = json.loads(fn.get("arguments") or "{}")
    except Exception:  # noqa: BLE001
        a = {}
    hint = (a.get("path") or a.get("command") or a.get("pattern") or a.get("goal")
            or a.get("query") or a.get("name") or "")
    return f"{name}({str(hint)[:60]})" if hint else name


def _digest_dropped(dropped: list) -> str:
    """Structured digest of turns that fall out of the window: the assistant's
    stated decisions plus the tool trail (which tools ran, with a hint of their
    arguments, and what they returned), so progress survives compaction."""
    if not dropped:
        return ""
    lines = [_COMPACT_MARK + " \u2014 keep this in mind]"]
    seen: set[str] = set()
    for m in dropped[-80:]:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content") if isinstance(m.get("content"), str) else ""
        if content.startswith(_COMPACT_MARK):
            continue  # never re-digest an earlier digest
        if role == "assistant" and m.get("tool_calls"):
            line = "- called: " + ", ".join(_tool_brief(tc)
                                            for tc in m["tool_calls"])
        elif role == "assistant":
            line = f"- said: {' '.join(content.split())[:300]}"
        elif role == "tool":
            line = f"- result: {' '.join(content.split())[:300]}"
        elif role == "user":
            line = f"- asked: {' '.join(content.split())[:200]}"
        else:
            continue
        if not line.split(": ", 1)[-1].strip() or line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return "\n".join(lines)[:8000] if len(lines) > 1 else ""


def _window_messages(messages: list, max_chars: int = MAX_HISTORY_CHARS) -> list:
    """Bound the live context to ~max_chars while (a) always keeping the system
    prompt, (b) never separating an assistant tool_call from its tool results,
    (c) pinning the latest compaction digest, and (d) folding dropped turns into
    one fresh digest instead of losing them."""
    if not messages:
        return messages
    system = messages[0]
    body = messages[1:]
    total = sum(len(m.get("content") or "") for m in messages if isinstance(m, dict))
    if total <= max_chars:
        return messages
    # pin the most recent digest so summaries are never re-lost
    pin_idx = None
    for i, m in enumerate(body):
        if (isinstance(m, dict) and isinstance(m.get("content"), str)
                and m["content"].startswith(_COMPACT_MARK)):
            pin_idx = i
    pinned = [body[pin_idx]] if pin_idx is not None else []
    rest = [m for i, m in enumerate(body) if i != pin_idx]
    keep: list = []
    consumed = 0
    for m in reversed(rest):
        c = len(m.get("content") or "") if isinstance(m, dict) else 0
        if keep and consumed + c > max_chars:
            break
        consumed += c
        keep.append(m)
    keep.reverse()
    # never start the window on an orphan tool result (its call was dropped)
    while keep and isinstance(keep[0], dict) and keep[0].get("role") == "tool":
        keep.pop(0)
    dropped = rest[: len(rest) - len(keep)]
    out = [system]
    if pinned:
        # keep the most recent (model) summary — never re-lose it
        out.append(pinned[0])
    new_digest = _digest_dropped(dropped)
    if new_digest:
        out.append({"role": "user", "content": new_digest})
    out.extend(keep)
    return out


def _cap_history(ms: list) -> list:
    """Bounded session history for continuation (system + windowed tail)."""
    return _window_messages(ms)


# ── real context compression (agent path only — a model call is acceptable) ──
COMPACT_TRIGGER_RATIO = float(os.environ.get("AGENT_COMPACT_TRIGGER", "0.75"))
PROTECT_FIRST = int(os.environ.get("AGENT_PROTECT_FIRST", "3"))
PROTECT_LAST = int(os.environ.get("AGENT_PROTECT_LAST", "12"))
MAX_SUMMARY_CHARS = int(os.environ.get("AGENT_SUMMARY_MAX_CHARS", "6000"))

SUMMARIZER_SYSTEM = (
    "You compress an agent's working context. Given the middle of a coding "
    "session, write a dense progress summary that keeps: the user's goal, the "
    "decisions made, files created/edited (with paths), key findings, commands "
    "that passed or failed, and what remains. Drop chit-chat and raw tool "
    "output. Terse bullet points. Do not invent anything."
)


def _render_for_summary(middle: list) -> str:
    """Flatten the middle turns into text for the summarizer (tool names kept,
    raw output truncated)."""
    parts: list[str] = []
    for m in middle:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content") if isinstance(m.get("content"), str) else ""
        if role == "assistant" and m.get("tool_calls"):
            parts.append("assistant called: "
                         + ", ".join(_tool_brief(tc) for tc in m["tool_calls"]))
            if content.strip():
                parts.append(f"assistant said: {content.strip()[:500]}")
        elif role == "tool":
            parts.append(f"tool result: {content.strip()[:500]}")
        else:
            parts.append(f"{role}: {content.strip()[:800]}")
    return "\n".join(parts)


def _fix_tool_boundaries(head: list, middle: list, tail: list) -> tuple:
    """Never leave an orphan tool result (in tail) or tool call (in head) whose
    counterpart landed in the middle that is about to be summarized away."""
    while tail and isinstance(tail[0], dict) and tail[0].get("role") == "tool":
        call_id = tail[0].get("tool_call_id")
        prev = middle[-1] if middle and isinstance(middle[-1], dict) else None
        prev_ids = {tc.get("id") for tc in (prev.get("tool_calls") or [])} if prev else set()
        if prev is not None and prev.get("role") == "assistant" and call_id in prev_ids:
            tail.insert(0, middle.pop())  # keep the matching call with its result
        else:
            tail.pop(0)  # orphan result (its call is gone) — drop it
    while head and isinstance(head[-1], dict) and head[-1].get("role") == "assistant" \
            and head[-1].get("tool_calls"):
        middle.insert(0, head.pop())
    return head, middle, tail


def _summarize_middle(messages: list, *, provider_id: str, model: str,
                      protect_first: int, protect_last: int) -> list | None:
    """Replace the middle of the conversation with a model-written summary,
    keeping the system prompt, the first ``protect_first`` and the last
    ``protect_last`` turns intact. Returns None on any failure."""
    if not messages or len(messages) < 3:
        return None
    system = (messages[0] if isinstance(messages[0], dict)
              and messages[0].get("role") == "system" else None)
    body = messages[1:] if system else messages
    if len(body) <= protect_first + protect_last:
        return None
    head = list(body[:protect_first])
    tail = list(body[-protect_last:])
    middle = list(body[protect_first:len(body) - protect_last])
    head, middle, tail = _fix_tool_boundaries(head, middle, tail)
    if not middle:
        return None
    rendered = _render_for_summary(middle)[:60000]
    if not rendered.strip():
        return None
    try:
        resp = _chat_with_retry(
            provider_id, model,
            [{"role": "system", "content": SUMMARIZER_SYSTEM},
             {"role": "user", "content": "Conversation so far:\n" + rendered}],
            retries=1, tools=[])
    except Exception:  # noqa: BLE001
        return None
    msg = (resp.get("choices") or [{}])[0].get("message") or {}
    text = (msg.get("content") or "").strip()
    if not text:
        return None
    summary = {"role": "user",
               "content": f"{_COMPACT_MARK} (summary) \u2014 keep this in mind]\n"
                          f"{text[:MAX_SUMMARY_CHARS]}"}
    return ([system] if system else []) + head + [summary] + tail


def _maintain_context(messages: list, *, provider_id: str, model: str,
                      max_chars: int = MAX_HISTORY_CHARS) -> list:
    """Bound the live context. Over the trigger, summarize the middle with the
    model (protect first/last); always finish with the deterministic hard cap so
    an unavailable model can never blow the window."""
    total = sum(len(m.get("content") or "") for m in messages if isinstance(m, dict))
    if total > int(max_chars * COMPACT_TRIGGER_RATIO):
        compressed = _summarize_middle(messages, provider_id=provider_id,
                                       model=model, protect_first=PROTECT_FIRST,
                                       protect_last=PROTECT_LAST)
        if compressed:
            messages = compressed
    return _window_messages(messages, max_chars)


MAX_PARALLEL_TASKS = int(os.environ.get("AGENT_MAX_PARALLEL_TASKS", "4"))


def _run_calls(calls: list, execs: dict, on_step, max_tool_chars: int,
               parallel_tasks: int = MAX_PARALLEL_TASKS) -> list:
    """Execute a turn's tool calls concurrently, preserving their order.

    Independent calls run in parallel (matching OpenCode/Hermes batching):
    reads, writes to different files, web calls, task fan-out. Calls that
    could conflict serialize on a shared lock: same-file write/edit/apply/
    diagnostics/read, and all run_bash (shared shell)."""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    results: list = [None] * len(calls)
    locks: dict = {}

    def _conflict_keys(name: str, args: dict) -> list:
        if name in ("write_file", "edit_file", "diagnostics", "lsp", "read_file"):
            p = (args or {}).get("path", "")
            return [p] if p else []
        if name == "apply_patch":
            return list(re.findall(
                r"^\*\*\* (?:Add|Update|Delete) File: (.+)$",
                (args or {}).get("patch") or "", re.M))
        if name == "run_bash":
            return ["$shell"]
        return []

    def _run(i: int, tc: dict):
        fn = tc.get("function") or {}
        name = fn.get("name", "")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:  # noqa: BLE001
            args = {}
        held = []
        try:
            for k in sorted(set(_conflict_keys(name, args))):
                lock = locks.setdefault(k, threading.Lock())
                lock.acquire()
                held.append(lock)
            if on_step:
                try:
                    on_step(name, args)
                except Exception:  # noqa: BLE001
                    pass
            blocked = hooks.emit("pre_tool", name=name, args=args)
            if blocked:
                out = blocked
            else:
                try:
                    out = execs.get(name, lambda a: f"unknown tool {name}")(args)
                except Exception as e:  # a bad tool call must not abort the task
                    out = f"Error: tool {name} raised {type(e).__name__}: {e}"
                hooks.emit("post_tool", name=name, args=args, result=out)
        finally:
            for lock in held:
                lock.release()
        results[i] = ({"role": "tool", "tool_call_id": tc.get("id"),
                       "content": _truncate(_redact(str(out)), max_tool_chars)},
                      name, out)

    with ThreadPoolExecutor(max_workers=min(parallel_tasks, len(calls))) as ex:
        futs = [ex.submit(_run, i, c) for i, c in enumerate(calls)]
        for f in futs:
            f.result()
    return results


def run_task(task: str, root: str, *, provider_id: str = "opencode",
             model: str = "mimo-v2.5-free", on_step: Callable | None = None,
             max_steps: int = 80, max_tool_chars: int = 12000,
             cancel=None, read_only: bool = False,
             history: list | None = None, allow_task: bool = True,
             on_token=None, depth: int = 0) -> dict:
    """Run one task to completion. Returns {text, steps, wrote, files, messages}.

    Pass the previous result's ``messages`` back as ``history`` to continue the
    same session (multi-turn coding). ``depth`` is the delegation level (0 =
    top-level); children run at ``depth+1`` and may delegate further only while
    ``depth+1 < MAX_DEPTH`` (orchestrator role)."""
    touched: set = set()
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0,
                   "total_tokens": 0, "cached_tokens": 0, "cost": 0.0}

    def _on_task(goal: str, context: str = "", tools: str = "read") -> str:
        goal_text = goal if not context else f"{goal}\n\nContext to use:\n{context}"
        child = run_task(
            goal_text, root, provider_id=provider_id, model=model,
            max_steps=25, **_subtask_kwargs(depth, tools))
        return (child.get("text") or "(no result)")[:MAX_SUMMARY_CHARS]

    execs = _make_exec(Path(root), touched, _on_task if allow_task else None,
                       read_only=read_only)
    hooks.load_plugins(PLUGIN_DIR)
    execs.update(hooks.tool_handlers())
    if read_only:
        tools = [t for t in TOOLS if t["function"]["name"] not in
                 ("write_file", "edit_file", "apply_patch", "move_file",
                  "delete_file", "run_bash", "run_python")]
    else:
        tools = TOOLS
    if not allow_task:
        tools = [t for t in tools if t["function"]["name"] != "task"]
    tools = [t for t in tools if _tool_enabled(t["function"]["name"])]
    tools = tools + hooks.extra_tools()
    if depth > 0:
        system = SUBAGENT_SYSTEM
    else:
        system = _PLAN_SYSTEM if read_only else SYSTEM
    try:  # progressive disclosure: index saved skills into the prompt
        skills_index = _skills().index()
        if skills_index:
            system = system + "\n\n" + skills_index
    except Exception:  # noqa: BLE001
        pass
    if history:
        messages = list(history)
        if not messages or messages[0].get("role") != "system":
            messages = [{"role": "system", "content": system}] + messages
    else:
        messages = [{"role": "system", "content": system}]
    messages.append({"role": "user", "content": task})
    wrote = False
    for step in range(max_steps):
        if cancel is not None and cancel.is_set():
            return {"text": "Stopped.", "steps": step, "wrote": wrote,
                    "cancelled": True, "files": sorted(touched),
                    "usage": usage_total,
                    "messages": messages}
        messages = _maintain_context(messages, provider_id=provider_id, model=model)
        hooks.emit("pre_llm", messages=messages, model=model)
        resp = _chat_with_retry(provider_id, model, messages, tools=tools,
                                on_token=on_token)
        hooks.emit("post_llm", model=model, response=resp)
        _u = resp.get("usage") or {}
        usage_total["prompt_tokens"] += int(_u.get("prompt_tokens") or 0)
        usage_total["completion_tokens"] += int(_u.get("completion_tokens") or 0)
        usage_total["total_tokens"] += int(_u.get("total_tokens") or 0)
        usage_total["cached_tokens"] += int(
            (_u.get("prompt_tokens_details") or {}).get("cached_tokens")
            or _u.get("prompt_cache_hit_tokens") or 0)
        usage_total["cost"] += float(resp.get("cost") or 0.0)
        msg = (resp.get("choices") or [{}])[0].get("message") or {}
        calls = msg.get("tool_calls") or []
        if not calls:
            return {"text": (msg.get("content") or "").strip(),
                    "steps": step + 1, "wrote": wrote,
                    "files": sorted(touched),
                    "usage": usage_total,
                    "messages": _cap_history(messages)}
        messages.append(msg)
        for rmsg, name, out in _run_calls(calls, execs, on_step, max_tool_chars):
            if name in ("write_file", "edit_file", "apply_patch", "move_file", "delete_file") and \
                    not str(out).startswith("Error"):
                wrote = True
            messages.append(rmsg)
    return {"text": "(max steps reached)", "steps": max_steps,
            "wrote": wrote, "files": sorted(touched),
            "usage": usage_total,
            "messages": _cap_history(messages)}


if __name__ == "__main__":
    import argparse

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)
    ap_ = argparse.ArgumentParser(description="Run one task through the native agent loop")
    ap_.add_argument("--task-file", required=True)
    ap_.add_argument("--root", required=True)
    ap_.add_argument("--model", default="mimo-v2.5-free")
    ap_.add_argument("--provider", default="opencode")
    ap_.add_argument("--max-steps", type=int, default=80)
    ap_.add_argument("--read-only", action="store_true")
    args = ap_.parse_args()

    def _step(name, a):
        print(f"[step] {name} {str(a)[:90]}", flush=True)

    task_text = Path(args.task_file).read_text(encoding="utf-8")
    result = run_task(task_text, args.root, provider_id=args.provider,
                      model=args.model, on_step=_step, max_steps=args.max_steps,
                      read_only=args.read_only)
    print(f"\n=== DONE: {result['steps']} steps, wrote={result['wrote']} ===")
    print(result["text"][:800])

