"""A connected Google app, exposed as an MCP server over stdio.

One server per connector, so each shows up as its own card and can be removed
on its own:

    {"name": "google-calendar", "command": "<python>",
     "args": ["google_mcp_server.py", "google-calendar"]}

Tools return the caller's real data using the token from google_auth (which
refreshes it as needed). Because it speaks MCP, the agent needs no new code:
mcp_list_tools / mcp_call_tool already reach it.

Run: python google_mcp_server.py <connector>
"""
from __future__ import annotations

import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import google_auth  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
CAL = "https://www.googleapis.com/calendar/v3"
GMAIL = "https://gmail.googleapis.com/gmail/v1"


class ToolError(Exception):
    """Tool-level failure, reported inside the MCP result."""


def _http():
    import httpx
    return httpx


def _get(url: str, token: str, **kw) -> dict:
    httpx = _http()
    r = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=20, **kw)
    if r.status_code >= 400:
        raise ToolError(_explain(r))
    return r.json()


def _post(url: str, token: str, body: dict) -> dict:
    httpx = _http()
    r = httpx.post(url, headers={"Authorization": f"Bearer {token}"}, json=body, timeout=20)
    if r.status_code >= 400:
        raise ToolError(_explain(r))
    return r.json()


def _explain(r) -> str:
    try:
        return r.json().get("error", {}).get("message") or r.text[:200]
    except Exception:  # noqa: BLE001
        return r.text[:200]


# ── calendar ─────────────────────────────────────────────────────────────────
def list_events(token: str, args: dict) -> str:
    params = {"singleEvents": "true", "orderBy": "startTime",
              "maxResults": min(int(args.get("max_results") or 10), 50)}
    if args.get("time_min"):
        params["timeMin"] = args["time_min"]
    if args.get("time_max"):
        params["timeMax"] = args["time_max"]
    items = _get(f"{CAL}/calendars/primary/events", token, params=params).get("items", [])
    if not items:
        return "No events in that window."
    lines = []
    for e in items:
        start = (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date", "")
        lines.append(f"- {start} · {e.get('summary', '(no title)')}")
    return "\n".join(lines)


def create_event(token: str, args: dict) -> str:
    body = {"summary": args.get("summary", "(no title)"),
            "start": {"dateTime": args["start"]}, "end": {"dateTime": args["end"]}}
    if args.get("description"):
        body["description"] = args["description"]
    e = _post(f"{CAL}/calendars/primary/events", token, body)
    return f"Created “{e.get('summary')}” at {(e.get('start') or {}).get('dateTime', '')}"


# ── gmail ────────────────────────────────────────────────────────────────────
def search_threads(token: str, args: dict) -> str:
    params = {"q": args.get("query", ""),
              "maxResults": min(int(args.get("max_results") or 10), 50)}
    msgs = _get(f"{GMAIL}/users/me/messages", token, params=params).get("messages", [])
    if not msgs:
        return "No matching mail."
    return "\n".join(f"- {m['id']}" for m in msgs)


def get_message(token: str, args: dict) -> str:
    m = _get(f"{GMAIL}/users/me/messages/{args['id']}", token, params={"format": "metadata"})
    hdrs = {h["name"].lower(): h["value"] for h in (m.get("payload") or {}).get("headers", [])}
    return (f"From: {hdrs.get('from', '')}\nTo: {hdrs.get('to', '')}\n"
            f"Subject: {hdrs.get('subject', '')}\nDate: {hdrs.get('date', '')}\n"
            f"Snippet: {m.get('snippet', '')}")


def create_draft(token: str, args: dict) -> str:
    import email.message

    msg = email.message.EmailMessage()
    msg["To"] = args.get("to", "")
    msg["Subject"] = args.get("subject", "")
    msg.set_content(args.get("body", ""))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    out = _post(f"{GMAIL}/users/me/drafts", token, {"message": {"raw": raw}})
    return f"Draft created (id {out.get('id')})"


def _schema(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": props, "required": required}}


TOOLSETS: dict[str, list[dict]] = {
    "google-calendar": [
        {"schema": _schema("list_events", "List upcoming calendar events (ISO-8601 times).",
                           {"time_min": {"type": "string"}, "time_max": {"type": "string"},
                            "max_results": {"type": "integer"}}, []),
         "run": list_events},
        {"schema": _schema("create_event", "Create a calendar event.",
                           {"summary": {"type": "string"}, "start": {"type": "string"},
                            "end": {"type": "string"}, "description": {"type": "string"}},
                           ["summary", "start", "end"]),
         "run": create_event},
    ],
    "gmail": [
        {"schema": _schema("search_threads", "Search mail with Gmail query syntax.",
                           {"query": {"type": "string"}, "max_results": {"type": "integer"}},
                           ["query"]),
         "run": search_threads},
        {"schema": _schema("get_message", "Read one message by id.",
                           {"id": {"type": "string"}}, ["id"]),
         "run": get_message},
        {"schema": _schema("create_draft", "Draft an email (does not send).",
                           {"to": {"type": "string"}, "subject": {"type": "string"},
                            "body": {"type": "string"}}, ["to", "subject", "body"]),
         "run": create_draft},
    ],
}


# ── MCP protocol ─────────────────────────────────────────────────────────────
def handle(msg: dict, connector: str) -> dict | None:
    """One JSON-RPC message in, one (or no) response out."""
    if "id" not in msg:
        return None                                   # notification
    rid, method = msg["id"], msg.get("method")
    params = msg.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
            "serverInfo": {"name": f"jarvis-{connector}", "version": "1.0"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"tools": [t["schema"] for t in TOOLSETS.get(connector, [])]}}
    if method == "tools/call":
        name = params.get("name", "")
        tool = next((t for t in TOOLSETS.get(connector, [])
                     if t["schema"]["name"] == name), None)
        if tool is None:
            return _err(rid, f"unknown tool {name!r}")
        try:
            token = google_auth.access_token(connector)
            text = tool["run"](token, params.get("arguments") or {})
        except google_auth.OAuthError as e:
            return _err(rid, str(e))
        except ToolError as e:
            return _err(rid, str(e))
        except Exception as e:  # noqa: BLE001
            return _err(rid, f"{name} failed: {e!r}")
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text", "text": text}], "isError": False}}
    return _err(rid, f"unsupported method {method!r}")


def _err(rid, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": str(message)}}


def main(argv: list[str]) -> int:
    connector = argv[1] if len(argv) > 1 else ""
    if connector not in TOOLSETS:
        print(f"usage: {argv[0]} <{'|'.join(TOOLSETS)}>", file=sys.stderr)
        return 2
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            out = handle(msg, connector)
        except Exception as e:  # noqa: BLE001
            out = _err(msg.get("id"), repr(e))
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
    return 0


def server_spec(connector: str) -> dict:
    """The mcp.json entry for this connector (absolute interpreter + script)."""
    return {"name": connector, "command": sys.executable,
            "args": [os.path.abspath(__file__), connector]}


def register(connector: str) -> dict:
    """Register this connector with the app's MCP config."""
    import mcp_client
    try:
        mgr = mcp_client.MCPManager()
        spec = server_spec(connector)
        if connector in mgr.configured():
            mgr.remove_server(connector)
        out = mgr.add_server(spec["name"], spec["command"], "", spec["args"][1:])
    except Exception as e:  # noqa: BLE001
        google_auth.log_event(f"register {connector} error {type(e).__name__}")
        raise
    google_auth.log_event(f"register {connector} ok")
    return out


def unregister(connector: str) -> bool:
    import mcp_client

    removed = mcp_client.MCPManager().remove_server(connector)
    google_auth.log_event(f"unregister {connector} removed={removed}")
    return removed


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
