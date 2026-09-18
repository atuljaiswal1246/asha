"""Minimal LSP client over stdio (C2+).

Speaks the Language Server Protocol (Content-Length framed JSON-RPC) to a
language server such as ``pylsp``. The only operation Jarvis needs is
``diagnostics(path, text)``: didOpen a document and collect the
``textDocument/publishDiagnostics`` notification the server emits.

A background reader thread feeds a queue so waits are bounded by a timeout.
"""
from __future__ import annotations

import json
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path


def server_command(language: str = "python"):
    """Best-effort language-server command for a language."""
    if language == "python":
        exe = shutil.which("pylsp")
        if exe:
            return [exe]
    return None


def _frame(msg: dict) -> bytes:
    body = json.dumps(msg).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


class LSPClient:
    """One language-server process over stdio."""

    def __init__(self, command: list[str], root: str, timeout: float = 15.0):
        self._command = list(command)
        self._root = str(Path(root).resolve())
        self._timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._id = 0
        self._inbox: "queue.Queue[dict]" = queue.Queue()
        self._reader: threading.Thread | None = None
        self._open: dict[str, int] = {}  # uri -> version

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self._proc = subprocess.Popen(
            self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=self._root,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        root_uri = Path(self._root).as_uri()
        self._request("initialize", {
            "processId": None,
            "rootUri": root_uri,
            "capabilities": {},
            "workspaceFolders": [{"uri": root_uri, "name": Path(self._root).name}],
        })
        self._notify("initialized", {})

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            self._notify("shutdown", {})
            self._notify("exit", {})
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ── framing ──────────────────────────────────────────────────────────────
    def _read_loop(self) -> None:
        stream = self._proc.stdout if self._proc else None
        while stream is not None:
            headers: dict = {}
            while True:
                line = stream.readline()
                if not line:
                    return
                if line in (b"\r\n", b"\n"):
                    break
                try:
                    k, v = line.decode("ascii", "replace").split(":", 1)
                    headers[k.strip().lower()] = v.strip()
                except ValueError:
                    continue
            try:
                length = int(headers.get("content-length", "0"))
            except ValueError:
                return
            if length <= 0:
                continue
            body = stream.read(length)
            try:
                self._inbox.put(json.loads(body.decode("utf-8")))
            except ValueError:
                continue

    def _write(self, msg: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("LSP server is not running")
        self._proc.stdin.write(_frame(msg))
        self._proc.stdin.flush()

    def _notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict, timeout: float | None = None):
        self._id += 1
        rid = self._id
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + (timeout or self._timeout)
        while time.monotonic() < deadline:
            try:
                msg = self._inbox.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                break
            if msg.get("id") == rid:
                return msg.get("result")
        raise TimeoutError(f"LSP {method} timed out")

    # ── diagnostics ──────────────────────────────────────────────────────────
    def diagnostics(self, path: str, text: str, wait: float = 2.0) -> list[dict]:
        """Open (or update) ``path`` and collect publishDiagnostics for it."""
        uri = self._prepare(path, text)
        diags: list[dict] = []
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                msg = self._inbox.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                break
            if msg.get("method") == "textDocument/publishDiagnostics":
                params = msg.get("params") or {}
                if params.get("uri") == uri:
                    diags = params.get("diagnostics", []) or []
                    # Got our diagnostics — allow a short grace for more, then stop.
                    deadline = min(deadline, time.monotonic() + 0.4)
        return diags

    def _prepare(self, path: str, text: str) -> str:
        """Open/refresh a document and drain stale notifications."""
        uri = Path(path).resolve().as_uri()
        lang = "python" if str(path).endswith(".py") else "plaintext"
        while True:
            try:
                self._inbox.get_nowait()
            except queue.Empty:
                break
        if uri in self._open:
            self._open[uri] += 1
            self._notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": self._open[uri]},
                "contentChanges": [{"text": text}],
            })
        else:
            self._open[uri] = 1
            self._notify("textDocument/didOpen", {
                "textDocument": {"uri": uri, "languageId": lang,
                                 "version": 1, "text": text},
            })
        return uri

    def document_symbols(self, path: str, text: str) -> list[dict]:
        uri = self._prepare(path, text)
        res = self._request("textDocument/documentSymbol",
                            {"textDocument": {"uri": uri}})
        return res or []

    def hover(self, path: str, text: str, line: int, character: int) -> str:
        uri = self._prepare(path, text)
        res = self._request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": max(0, line - 1), "character": max(0, character - 1)},
        })
        if not res:
            return ""
        contents = res.get("contents")
        if isinstance(contents, str):
            return contents.strip()
        if isinstance(contents, dict):
            return (contents.get("value") or "").strip()
        if isinstance(contents, list):
            parts = []
            for c in contents:
                parts.append(c if isinstance(c, str) else (c or {}).get("value", ""))
            return "\n".join(p for p in parts if p).strip()
        return ""

    def definition(self, path: str, text: str, line: int, character: int) -> list[dict]:
        uri = self._prepare(path, text)
        res = self._request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": {"line": max(0, line - 1), "character": max(0, character - 1)},
        })
        if not res:
            return []
        return res if isinstance(res, list) else [res]

    def references(self, path: str, text: str, line: int, character: int) -> list[dict]:
        uri = self._prepare(path, text)
        res = self._request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": {"line": max(0, line - 1), "character": max(0, character - 1)},
            "context": {"includeDeclaration": True},
        })
        return res or []


def lsp_diagnostics(path: str, root: str, text: str) -> str | None:
    """Return formatted diagnostics via LSP, or None when unavailable.

    The server process is cached per root so repeated calls are cheap.
    """
    cmd = server_command("python" if str(path).endswith(".py") else "")
    if not cmd:
        return None
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(root)
        if client is None:
            client = LSPClient(cmd, root)
            try:
                client.start()
            except Exception:
                return None
            _CLIENTS[root] = client
    try:
        diags = client.diagnostics(path, text)
    except Exception:
        # Server may have died — restart once, then give up.
        try:
            client.close()
            client.start()
            diags = client.diagnostics(path, text)
        except Exception:
            _CLIENTS.pop(root, None)
            return None
    if not diags:
        return f"No diagnostics ({Path(cmd[0]).name} clean)."
    lines = []
    for d in diags:
        sev = {1: "error", 2: "warning", 3: "info", 4: "hint"}.get(
            d.get("severity"), "issue")
        rng = (d.get("range") or {}).get("start") or {}
        loc = f"{rng.get('line', 0) + 1}:{rng.get('character', 0) + 1}"
        lines.append(f"{loc} [{sev}] {d.get('message', '').strip()}")
    return "\n".join(lines)


_CLIENTS: dict[str, LSPClient] = {}
_CLIENTS_LOCK = threading.Lock()

_SYMBOL_KINDS = {
    1: "file", 2: "module", 3: "namespace", 4: "package", 5: "class",
    6: "method", 7: "property", 8: "field", 9: "constructor", 10: "enum",
    11: "interface", 12: "function", 13: "variable", 14: "constant",
}


def _get_client(root: str, cmd: list[str]) -> LSPClient | None:
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(root)
        if client is None:
            client = LSPClient(cmd, root)
            try:
                client.start()
            except Exception:
                return None
            _CLIENTS[root] = client
    return client


def _format_symbols(symbols: list[dict], depth: int = 0) -> str:
    lines: list[str] = []
    for s in symbols or []:
        kind = _SYMBOL_KINDS.get(s.get("kind"), "symbol")
        rng = s.get("range") or s.get("location", {}).get("range") or {}
        line = (rng.get("start") or {}).get("line", 0) + 1
        lines.append(f"{'  ' * depth}{kind} {s.get('name', '')} (line {line})")
        if s.get("children"):
            lines.append(_format_symbols(s["children"], depth + 1))
    return "\n".join(x for x in lines if x)


def _format_locations(locs: list[dict]) -> str:
    out = []
    for loc in locs or []:
        uri = loc.get("uri") or (loc.get("targetUri"))
        rng = (loc.get("range") or loc.get("targetRange") or {})
        line = (rng.get("start") or {}).get("line", 0) + 1
        if uri:
            out.append(f"{Path(uri.replace('file://', '')).name}:{line}")
    return "\n".join(out)


def lsp_action(path: str, root: str, text: str, action: str,
               line: int = 1, character: int = 1) -> str | None:
    """Run one LSP action (symbols|definition|hover); None when unavailable."""
    cmd = server_command("python" if str(path).endswith(".py") else "")
    if not cmd:
        return None
    client = _get_client(root, cmd)
    if client is None:
        return None
    try:
        if action == "symbols":
            return _format_symbols(client.document_symbols(path, text)) or "No symbols."
        if action == "definition":
            return _format_locations(client.definition(path, text, line, character)) \
                or "No definition found."
        if action == "references":
            return _format_locations(client.references(path, text, line, character)) \
                or "No references found."
        if action == "hover":
            return client.hover(path, text, line, character) or "No hover info."
    except Exception:
        return None
    return None
