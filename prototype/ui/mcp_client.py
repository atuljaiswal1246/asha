"""Minimal MCP (Model Context Protocol) client over stdio (C1).

Speaks JSON-RPC 2.0 to a server process: ``initialize`` -> ``tools/list`` ->
``tools/call``. Servers come from ``mcp.json``:

    {"servers": [{"name": "fs", "command": "npx",
                  "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                  "env": {"KEY": "..."}}]}

The client is lazy: a server process starts on first use and stays alive.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

PROTOCOL_VERSION = "2024-11-05"


class MCPError(Exception):
    """Raised on protocol errors, unknown servers, or timeouts."""


class MCPServer:
    """One MCP server process spoken to over stdio JSON-RPC."""

    def __init__(self, name: str, command: str, args=None, env=None,
                 cwd=None, timeout: float = 20.0):
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.cwd = cwd
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._id = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        env = dict(os.environ)
        env.update(self.env)
        self._proc = subprocess.Popen(
            [self.command, *self.args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env, cwd=self.cwd,
        )
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "asha", "version": "1.0"},
        })
        self._notify("notifications/initialized", {})

    def _request(self, method: str, params: dict, timeout: float | None = None):
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                raise MCPError(f"MCP server {self.name!r} is not running")
            self._id += 1
            rid = self._id
            self._proc.stdin.write(
                json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                            "params": params}) + "\n"
            )
            self._proc.stdin.flush()
            deadline = time.monotonic() + (timeout or self.timeout)
            while time.monotonic() < deadline:
                line = self._proc.stdout.readline()
                if not line:
                    raise MCPError(f"MCP server {self.name!r} closed the stream")
                try:
                    resp = json.loads(line)
                except ValueError:
                    continue
                if resp.get("id") != rid:
                    continue
                if "error" in resp:
                    err = resp["error"]
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    raise MCPError(f"{method} failed: {msg}")
                return resp.get("result")
            raise MCPError(f"{method} timed out after {timeout or self.timeout}s")

    def _notify(self, method: str, params: dict) -> None:
        if self._proc is None or self._proc.poll() is not None:
            return
        with self._lock:
            self._proc.stdin.write(
                json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n"
            )
            self._proc.stdin.flush()

    def list_tools(self) -> list[dict]:
        res = self._request("tools/list", {})
        return (res or {}).get("tools", [])

    def call_tool(self, tool: str, arguments: dict | None = None):
        return self._request("tools/call",
                             {"name": tool, "arguments": arguments or {}})

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


class RemoteMCPServer:
    """One MCP server over the Streamable HTTP transport.

    JSON-RPC is POSTed to a single URL; the reply is JSON or an SSE stream.
    Optional static auth via ``headers`` (e.g. {"Authorization": "Bearer …"});
    a server-issued ``mcp-session-id`` is captured and echoed."""

    def __init__(self, name: str, url: str, headers=None, timeout: float = 30.0,
                 token_provider=None):
        self.name = name
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self._id = 0
        self._started = False
        self._session_id = ""
        self._token_provider = token_provider   # called per request: OAuth tokens rotate
        self._lock = threading.Lock()

    def _post(self, payload: dict):
        import httpx
        headers = {"content-type": "application/json",
                   "accept": "application/json, text/event-stream", **self.headers}
        if self._token_provider is not None:
            token = self._token_provider()
            if token:
                headers["Authorization"] = "Bearer " + token
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        try:
            r = httpx.post(self.url, json=payload, headers=headers,
                           timeout=self.timeout, follow_redirects=True)
        except Exception as e:  # noqa: BLE001
            raise MCPError(f"{self.name}: transport error: {e!r}")
        sid = r.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid
        if r.status_code >= 400:
            raise MCPError(f"{self.name}: HTTP {r.status_code}: {r.text[:200]}")
        if "text/event-stream" in r.headers.get("content-type", ""):
            return self._parse_sse(r.text, payload.get("id"))
        try:
            return r.json()
        except ValueError:
            return self._parse_sse(r.text, payload.get("id"))

    @staticmethod
    def _parse_sse(text: str, rid):
        for line in (text or "").splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            if msg.get("id") == rid:
                return msg
        return None

    def start(self) -> None:
        if self._started:
            return
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "jarvis", "version": "1.0"},
        })
        self._notify("notifications/initialized", {})
        self._started = True

    def _request(self, method: str, params: dict):
        with self._lock:
            self._id += 1
            resp = self._post({"jsonrpc": "2.0", "id": self._id,
                               "method": method, "params": params})
            if not isinstance(resp, dict):
                raise MCPError(f"{method}: no JSON-RPC response")
            if "error" in resp:
                err = resp["error"]
                msg = err.get("message") if isinstance(err, dict) else str(err)
                raise MCPError(f"{method} failed: {msg}")
            return resp.get("result")

    def _notify(self, method: str, params: dict) -> None:
        try:
            self._post({"jsonrpc": "2.0", "method": method, "params": params})
        except Exception:  # noqa: BLE001
            pass

    def list_tools(self) -> list[dict]:
        res = self._request("tools/list", {})
        return (res or {}).get("tools", [])

    def call_tool(self, tool: str, arguments: dict | None = None):
        return self._request("tools/call",
                             {"name": tool, "arguments": arguments or {}})

    def close(self) -> None:
        self._started = False


class MCPManager:
    """Loads ``mcp.json`` and manages lazy server processes."""

    def __init__(self, config_path: str | os.PathLike | None = None):
        self._path = Path(config_path) if config_path is not None else default_config_path()
        self._config: list[dict] = []
        self._servers: dict[str, MCPServer] = {}
        self.reload()

    def reload(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            servers = data.get("servers") if isinstance(data, dict) else None
        except Exception:
            servers = None
        self._config = [s for s in (servers or []) if isinstance(s, dict) and s.get("name")]

    def configured(self) -> list[str]:
        return [s["name"] for s in self._config]

    def _server(self, name: str):
        if name in self._servers:
            srv = self._servers[name]
            srv.start()
            return srv
        conf = next((s for s in self._config if s.get("name") == name), None)
        if conf is None:
            raise MCPError(f"unknown MCP server {name!r}")
        if conf.get("url"):
            token_provider = None
            if conf.get("oauth"):
                import mcp_oauth  # lazy: keeps the stdio path import-light

                def token_provider(conf=conf):
                    return mcp_oauth.access_token(name, conf.get("url", ""))
            srv = RemoteMCPServer(name=name, url=conf["url"], headers=conf.get("headers"),
                                  timeout=float(conf.get("timeout", 30)),
                                  token_provider=token_provider)
        elif conf.get("command"):
            srv = MCPServer(
                name=name, command=conf["command"], args=conf.get("args"),
                env=conf.get("env"), cwd=conf.get("cwd"),
                timeout=float(conf.get("timeout", 20)),
            )
        else:
            raise MCPError(f"MCP server {name!r} has no command or url")
        srv.start()
        self._servers[name] = srv
        return srv

    def list_tools(self, server: str | None = None) -> list[dict]:
        out: list[dict] = []
        for conf in self._config:
            name = conf["name"]
            if server and name != server:
                continue
            try:
                for t in self._server(name).list_tools():
                    out.append({
                        "server": name,
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                    })
            except Exception as e:  # noqa: BLE001 - surface per-server
                out.append({"server": name, "error": str(e)})
        return out

    def call_tool(self, server: str, tool: str, arguments: dict | None = None):
        return self._server(server).call_tool(tool, arguments)

    # ── management (for the MCP screen) ──────────────────────────────────────
    def servers(self) -> list[dict]:
        """Configured servers (env values redacted to key names)."""
        out = []
        for c in self._config:
            oauth = bool(c.get("oauth"))
            signed_in = False
            if oauth:
                try:
                    import mcp_oauth
                    signed_in = mcp_oauth.connected(c.get("name", ""))
                except Exception:  # noqa: BLE001
                    signed_in = False
            out.append({
                "name": c.get("name", ""),
                "command": c.get("command", ""),
                "args": c.get("args") or [],
                "url": c.get("url", ""),
                "env_keys": sorted((c.get("env") or {}).keys()),
                "header_keys": sorted((c.get("headers") or {}).keys()),
                "oauth": oauth,
                "signed_in": signed_in,
            })
        return out

    def mark_oauth(self, name: str, enabled: bool = True) -> bool:
        """Flag a remote server as OAuth-authenticated (so we send a live token)."""
        for c in self._config:
            if c.get("name") == name:
                c["oauth"] = bool(enabled)
                self._save()
                return True
        return False

    def add_server(self, name: str, command: str = "", url: str = "",
                   args: list | None = None, env: dict | None = None,
                   headers: dict | None = None) -> dict:
        name, command, url = (name or "").strip(), (command or "").strip(), (url or "").strip()
        if not name:
            raise MCPError("server name is required")
        if not command and not url:
            raise MCPError("a command (local) or URL (remote) is required")
        if any(s.get("name") == name for s in self._config):
            raise MCPError(f"server {name!r} already exists")
        entry: dict = {"name": name}
        if command:
            entry["command"] = command
            if args:
                entry["args"] = [str(a) for a in args]
        if url:
            entry["url"] = url
        if env:
            entry["env"] = env
        if headers:
            entry["headers"] = headers
        self._config.append(entry)
        self._save()
        return entry

    def remove_server(self, name: str) -> bool:
        before = len(self._config)
        self._config = [s for s in self._config if s.get("name") != name]
        if len(self._config) != before:
            self._save()
            srv = self._servers.pop(name, None)
            if srv:
                try:
                    srv.close()
                except Exception:  # noqa: BLE001
                    pass
            return True
        return False

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"servers": self._config}, indent=2) + "\n",
                              encoding="utf-8")

    def close(self) -> None:
        for srv in self._servers.values():
            srv.close()
        self._servers.clear()


def tool_text(result) -> str:
    """Flatten an MCP tools/call result into plain text."""
    parts: list[str] = []
    for item in (result or {}).get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
    return "\n".join(parts).strip()


# ── open MCP Registry (discovery) ───────────────────────────────────────────
REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0/servers"

# Curated starters, each verified to connect with no configuration.
# "{project}" is replaced with the active project folder before display/add.
FEATURED_SERVERS: list[dict] = [
    {"name": "deepwiki",
     "description": "Ask questions about any GitHub repo's docs.",
     "transport": "remote", "url": "https://mcp.deepwiki.com/mcp"},
    {"name": "context7",
     "description": "Up-to-date library and framework documentation.",
     "transport": "remote", "url": "https://mcp.context7.com/mcp"},
    {"name": "memory",
     "description": "Long-term memory the assistant can read and write.",
     "transport": "local", "command": "npx",
     "args": ["-y", "@modelcontextprotocol/server-memory"]},
    {"name": "fetch",
     "description": "Fetch any URL and read it as clean text.",
     "transport": "local", "command": "uvx", "args": ["mcp-server-fetch"]},
    {"name": "git",
     "description": "Inspect and manage the active project's Git repo.",
     "transport": "local", "command": "uvx", "args": ["mcp-server-git"]},
    {"name": "filesystem",
     "description": "Read and edit files in the active project folder.",
     "transport": "local", "command": "npx",
     "args": ["-y", "@modelcontextprotocol/server-filesystem", "{project}"]},
]


def featured_servers(project_dir: str = "") -> list[dict]:
    """Curated starters with the active project folder substituted in."""
    out: list[dict] = []
    for s in FEATURED_SERVERS:
        e = dict(s)
        if project_dir:
            e["args"] = [str(a).replace("{project}", project_dir) for a in s.get("args", [])]
        out.append(e)
    return out


def _normalize_registry(server: dict) -> dict:
    """Map a registry entry to a Jarvis 'add' record (remote preferred)."""
    name = server.get("title") or server.get("name") or ""
    desc = (server.get("description") or "").strip()[:200]
    rid = server.get("name", "")
    for remote in (server.get("remotes") or []):
        url = remote.get("url", "")
        if url:
            return {"id": rid, "name": name, "description": desc,
                    "transport": "remote", "url": url}
    for pkg in (server.get("packages") or []):
        rt = (pkg.get("registryType") or "").lower()
        ident = pkg.get("identifier") or ""
        if rt == "npm" and ident:
            args = ["-y", ident]
            for ra in (pkg.get("runtimeArguments") or []):
                if ra.get("value") and ra["value"] not in args:
                    args.insert(0, ra["value"])
            return {"id": rid, "name": name, "description": desc, "transport": "local",
                    "command": pkg.get("runtimeHint") or "npx", "args": args,
                    "env_keys": [v.get("name") for v in (pkg.get("environmentVariables") or [])
                                 if v.get("name")]}
        if rt == "oci" and ident:
            return {"id": rid, "name": name, "description": desc, "transport": "local",
                    "command": "docker", "args": ["run", "-i", "--rm", ident]}
    return {"id": rid, "name": name, "description": desc, "transport": "none"}


# Connectors people actually want, in rough order of interest. We do not
# invent entries: each is matched against the live registry by title, so what
# we pin is real and resolvable. The open registry has no popularity data and
# no categories, so this list *is* the curation (Claude does the same by hand).
POPULAR = [
    "GitHub", "Slack", "Notion", "Gmail", "Google Calendar", "Google Drive",
    "Linear", "Sentry", "Stripe", "Figma", "Atlassian", "Jira", "Confluence",
    "Asana", "HubSpot", "Intercom", "Airtable", "Supabase", "Neon", "Postgres",
    "MongoDB", "Redis", "Elasticsearch", "Vercel", "Cloudflare", "Netlify",
    "Playwright", "Puppeteer", "Firecrawl", "Exa", "Obsidian",
    "Shopify", "Zapier", "Datadog", "Grafana", "PayPal", "Dropbox", "Canva",
    "Miro", "Zoom", "Salesforce", "Box", "Todoist", "ClickUp", "Trello",
    "Monday", "Calendly", "Typeform", "Mailchimp", "Twilio", "Zendesk",
    "Snowflake", "BigQuery", "Databricks", "Turso", "Railway", "Render",
    "Neon", "Hugging Face", "Replicate", "OpenAI", "Anthropic", "Perplexity",
    "ElevenLabs", "Cloudinary", "Sentry", "Grafana", "Prometheus",
]
PLATFORM_HOSTS = (".vercel.app", ".netlify.app", ".workers.dev", ".pages.dev",
                  ".herokuapp.com", ".onrender.com", ".github.io", ".fly.dev",
                  ".trycloudflare.com", ".glitch.me", ".repl.co", ".up.railway.app")
_POPULAR_CACHE = {"at": 0.0, "items": []}
POPULAR_TTL = 24 * 3600


def _popular_cache_path():
    try:
        import jarvis_paths
        return Path(jarvis_paths.data_dir()) / "popular.json"
    except Exception:  # noqa: BLE001
        return _HERE.parent / "data" / "popular.json"


def default_config_path() -> Path:
    """The app's ``mcp.json`` in the runtime data dir (read at call time)."""
    try:
        import jarvis_paths
        return Path(jarvis_paths.data_dir()) / "mcp.json"
    except Exception:  # noqa: BLE001
        return _HERE.parent / "data" / "mcp.json"


_HERE = Path(__file__).resolve().parent


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _host_tier(url: str, want: str) -> int:
    """2 when a hostname label is exactly the name (mcp.linear.app), else 0.

    Looks at labels, not the whole URL, so resellers and lookalikes drop out:
    nordicmcp.eu/mcp/stripe and a2awire.com/.../github-changelog score 0.
    """
    if not url:
        return 0
    import urllib.parse
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:  # noqa: BLE001
        return 0
    w = _slug(want)
    # A hosting platform's own name is not the vendor's domain.
    if any(host.endswith(sfx) for sfx in PLATFORM_HOSTS):
        return 0
    labels = [part for part in host.split(".") if part]
    return 2 if any(part == w for part in labels) else 0


def _match_score(title: str, url: str, want: str) -> tuple:
    """Rank a candidate. Only an exact title or the vendor's own domain wins."""
    t, w = _slug(title), _slug(want)
    if t == w or t in (w + "mcp", w + "mcpserver", w + "server"):
        tier = 3
    elif t.startswith(w) and len(t) <= len(w) + 6:
        tier = 2
    else:
        tier = 0
    return (tier, _host_tier(url, want), -len(title or ""))


def _candidates(want: str) -> list[tuple]:
    import httpx
    try:
        r = httpx.get(REGISTRY_URL, params={"search": want, "limit": 10},
                      timeout=25, follow_redirects=True)
        r.raise_for_status()
        servers = (r.json() or {}).get("servers") or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for e in servers:
        srv = e.get("server") or {}
        meta = ((e.get("_meta") or {}).get(
            "io.modelcontextprotocol.registry/official") or {})
        if meta.get("isLatest") is False:
            continue
        title = (srv.get("title") or "").strip()
        if not title or not srv.get("description"):
            continue
        n = _normalize_registry(srv)
        if n.get("transport") == "none" or not n.get("name"):
            continue
        out.append((_match_score(title, n.get("url", ""), want), n))
    return out


def _scan_for_popular() -> list[dict]:
    """Look up each wanted connector in parallel and keep the real ones."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        scored = list(pool.map(_candidates, POPULAR))
    out, seen = [], set()
    for want, cands in zip(POPULAR, scored):
        # keep only exact-title matches or the vendor's own domain
        cands = [c for c in cands if c[0][0] >= 3 or c[0][1] >= 2]
        if not cands:
            continue
        cands.sort(key=lambda c: c[0], reverse=True)
        n = cands[0][1]
        key = n.get("id") or n["name"]
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out


def _platform_hosted(item: dict) -> bool:
    url = item.get("url") or ""
    return any(url.lower().find(sfx) >= 0 for sfx in PLATFORM_HOSTS)


def popular_connectors(force: bool = False) -> list[dict]:
    """Curated top connectors, cached to disk so the panel opens instantly."""
    import time as _t
    now = _t.time()
    if not force and _POPULAR_CACHE["items"] and now - _POPULAR_CACHE["at"] < POPULAR_TTL:
        return _POPULAR_CACHE["items"]
    path = _popular_cache_path()
    if not force and path.exists() and now - path.stat().st_mtime < POPULAR_TTL:
        try:
            items = [i for i in json.loads(path.read_text(encoding="utf-8"))
                     if not _platform_hosted(i)]
            if items:
                _POPULAR_CACHE.update({"at": now, "items": items})
                return items
        except Exception:  # noqa: BLE001
            pass
    items = [i for i in _scan_for_popular() if not _platform_hosted(i)]
    if items:
        _POPULAR_CACHE.update({"at": now, "items": items})
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    return items


def registry_page(query: str = "", cursor: str = "", limit: int = 100) -> dict:
    """One page of the live registry: {"results": [...], "next": cursor}.

    The registry lists every published version, so we keep only the latest —
    what you browse is the catalog, not its history.
    """
    import httpx
    params: dict = {"limit": max(1, min(int(limit), 100))}
    if query:
        params["search"] = query
    if cursor:
        params["cursor"] = cursor
    try:
        r = httpx.get(REGISTRY_URL, params=params, timeout=25, follow_redirects=True)
        r.raise_for_status()
        data = r.json() or {}
    except Exception as e:  # noqa: BLE001
        raise MCPError(f"registry request failed: {e!r}")
    out, seen = [], set()
    for e in data.get("servers") or []:
        meta = ((e.get("_meta") or {}).get(
            "io.modelcontextprotocol.registry/official") or {})
        if meta.get("isLatest") is False:
            continue
        n = _normalize_registry(e.get("server") or {})
        # Anyone can publish to the open registry, so untitled entries are the
        # raw-id / drive-by ones ("Inside Ads", "ac.tandem/docs-mcp"). A title
        # is the cheapest quality signal we have; Claude curates for real.
        if not (e.get("server") or {}).get("title"):
            continue
        if n.get("transport") == "none" or not n.get("name"):
            continue
        key = n.get("id") or n["name"]
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    # Pin the curated set first (no query only), so the panel opens with clean,
    # recognisable connectors instead of raw registry order.
    top: list[dict] = []
    if not query:
        have = {r.get("id") or r["name"] for r in out}
        for item in popular_connectors():
            key = item.get("id") or item["name"]
            if key not in have:
                have.add(key)
                top.append(item)
        out = top + out
    return {"results": out, "top": len(top),
            "next": ((data.get("metadata") or {}).get("nextCursor") or "")}


def search_registry(query: str = "", limit: int = 12) -> list[dict]:
    """Search the open MCP Registry; return addable entries (remote preferred)."""
    return registry_page(query, "", limit)["results"]
