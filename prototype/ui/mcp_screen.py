"""MCP screen backend — list / add / remove / test servers + registry discovery.

Lives outside the voice pipeline so the screen's behavior can be tested (and
reused by the headless core) without booting audio.

Messages in  (UI -> server): {"type": "mcp", "action": "get|add|remove|test|search", ...}
                              {"type": "mcp", "action": "connector_get|connector_test|connector_save|connector_delete", ...}
Messages out (server -> UI): {"type": "mcp_list",  "servers": [...], "featured": [...]}
                             {"type": "mcp_tools", "server": ..., "tools": [...]}
                             {"type": "mcp_registry", "query": ..., "results": [...]}
                             {"type": "mcp_apps", "apps": [...]}
                             {"type": "connector_guide", "id": ..., "steps": [...], ...}
                             {"type": "connector_result", "connector": ..., "ok": ..., "message": ...}
                             {"type": "mcp_error",  "error": ...}
"""
from __future__ import annotations

import asyncio

import mcp_client as mcp_mod
import mcp_oauth
import google_auth
import google_mcp_server
import connector_setup

# A credential saved by the guide lives in the user data dir, not the repo;
# load it into the process slot figma_rest reads so the brain's tools work
# after a restart even before the directory is opened.
try:
    connector_setup.install_saved_keys()
except Exception:  # noqa: BLE001 - startup must never die on a bad store
    pass


def _url_of(manager, name: str) -> str:
    for s in manager.servers():
        if s.get("name") == name:
            return s.get("url", "")
    return ""


async def _send_list(send, manager, project_dir: str) -> None:
    await send({"type": "mcp_list", "servers": manager.servers(),
                "featured": mcp_mod.featured_servers(project_dir)})


async def _handle_connector(msg: dict, send, manager, action: str,
                            project_dir: str) -> None:
    """Guided credential setup for a built-in connector (Figma pilot)."""
    cid = (msg.get("connector") or "").strip().lower()

    async def _state():
        await send(_apps(manager))
        await _send_list(send, manager, project_dir)

    if action == "connector_get":
        await send({"type": "connector_guide", **connector_setup.guide(cid)})
        return
    if action == "connector_delete":
        removed = await asyncio.to_thread(connector_setup.delete, cid)
        await send({"type": "connector_result", "connector": cid, "ok": True,
                    "saved": False, "removed": removed,
                    "message": ("Removed the saved token." if removed
                                else "Nothing was saved for this connector.")})
        await _state()
        return

    token = msg.get("token") or ""
    res = await asyncio.to_thread(connector_setup.validate, cid, token)
    if res.get("ok"):
        await asyncio.to_thread(connector_setup.save, cid, token)
        await send({"type": "connector_result", "connector": cid, "ok": True,
                    "saved": True, "unverified": False,
                    "account": res.get("account", ""),
                    "message": f"{res['message']} Saved to this app."})
    elif action == "connector_save" and res.get("network"):
        await asyncio.to_thread(connector_setup.save, cid, token, unverified=True)
        await send({"type": "connector_result", "connector": cid, "ok": True,
                    "saved": True, "unverified": True,
                    "message": ("Saved \u2014 but it could not be verified "
                                "while Figma was unreachable. Test the "
                                "connection when you are back online.")})
    else:
        await send({"type": "connector_result", "connector": cid, "ok": False,
                    "saved": False, "reason": res.get("reason", "error"),
                    "network": bool(res.get("network")),
                    "message": res.get("message", "Validation failed.")})
        return
    await _state()


def _apps(manager) -> dict:
    """The built-in apps: connection state + whether their MCP server is wired."""
    registered = set(manager.configured())
    out = []
    for cid, meta in google_auth.CONNECTORS.items():
        connected = google_auth.connected(cid)
        is_registered = cid in registered
        if connected and is_registered:
            state = "ready"
        elif connected and not is_registered:
            state = "needs_setup"
        elif not connected and is_registered:
            state = "needs_signin"
        else:
            state = "available"
        out.append({"id": cid, "label": meta["label"], "icon": meta["icon"],
                    "description": meta["description"],
                    "connected": connected, "registered": is_registered,
                    "state": state})
    for cid in connector_setup.connector_ids():
        g = connector_setup.guide(cid)
        connected = bool(g.get("connected"))
        out.append({"id": cid, "label": g["label"], "icon": g["icon"],
                    "description": g["summary"],
                    "connected": connected, "registered": connected,
                    "state": "ready" if connected else "needs_key",
                    "auth": "credential",
                    "unverified": bool(g.get("unverified"))})
    return {"type": "mcp_apps", "apps": out}


def _parse_env(raw) -> dict | None:
    """Accept {"K": "v"} or "K=v\\nK2=v2" (as typed in the UI form)."""
    if not raw:
        return None
    if isinstance(raw, dict):
        out = {str(k).strip(): str(v) for k, v in raw.items() if str(k).strip()}
        return out or None
    out: dict[str, str] = {}
    for line in str(raw).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key:
            out[key] = val.strip()
    return out or None


async def handle(msg: dict, send, manager, *, project_dir: str = "") -> None:
    """Run one MCP-screen message. `send` awaits a dict payload."""
    action = (msg.get("action") or "get").strip().lower()
    try:
        if action in ("connector_get", "connector_test", "connector_save",
                      "connector_delete"):
            await _handle_connector(msg, send, manager, action, project_dir)
            return
        if action == "search":
            query = (msg.get("query") or "").strip()
            page = await asyncio.to_thread(mcp_mod.registry_page, query, "",
                                           int(msg.get("limit") or 100))
            await send({"type": "mcp_registry", "query": query,
                        "results": page["results"], "next": page["next"],
                        "append": False})
            return
        if action == "browse":
            query = (msg.get("query") or "").strip()
            page = await asyncio.to_thread(mcp_mod.registry_page, query,
                                           msg.get("cursor") or "",
                                           int(msg.get("limit") or 100))
            await send({"type": "mcp_registry", "query": query,
                        "results": page["results"], "next": page["next"],
                        "append": True})
            return
        if action == "apps":
            await send(_apps(manager))
            return
        if action == "connect":
            cid = (msg.get("connector") or "").strip()
            if not google_auth.connected(cid):
                await asyncio.to_thread(google_auth.connect, cid)   # opens the browser
            try:
                await asyncio.to_thread(google_mcp_server.register, cid)
            except Exception as e:  # noqa: BLE001 - surface, don't leave the UI stale
                manager.reload()
                await send(_apps(manager))
                await _send_list(send, manager, project_dir)
                await send({"type": "mcp_error",
                            "error": f"{cid} signed in, but registering its MCP server failed: {e}"})
                return
            manager.reload()   # pick up the file written by register()
            await send(_apps(manager))
            await _send_list(send, manager, project_dir)
            return
        if action == "register":
            cid = (msg.get("connector") or "").strip()
            if not google_auth.connected(cid):
                raise mcp_mod.MCPError(
                    f"{cid} is not signed in; sign in before finishing setup")
            try:
                await asyncio.to_thread(google_mcp_server.register, cid)
            except Exception as e:  # noqa: BLE001
                manager.reload()
                await send(_apps(manager))
                await _send_list(send, manager, project_dir)
                await send({"type": "mcp_error",
                            "error": f"registering {cid} failed: {e}"})
                return
            manager.reload()
            await send(_apps(manager))
            await _send_list(send, manager, project_dir)
            return
        if action == "disconnect":
            cid = (msg.get("connector") or "").strip()
            if msg.get("confirm") is not True:
                google_auth.log_event(f"disconnect {cid} blocked no-confirmation")
                await send({"type": "mcp_error",
                            "error": f"Disconnecting {cid} requires explicit confirmation"})
                return
            await asyncio.to_thread(google_mcp_server.unregister, cid)
            manager.reload()
            await asyncio.to_thread(google_auth.disconnect, cid)
            await send(_apps(manager))
            await _send_list(send, manager, project_dir)
            return
        if action == "add_signin":
            # One click from the directory: wire the server and sign in to it.
            name = (msg.get("name") or "").strip()
            url = (msg.get("url") or "").strip()
            if not name or not url:
                raise mcp_mod.MCPError("name and url are required")
            if name not in manager.configured():
                await asyncio.to_thread(manager.add_server, name, "", url)
            await asyncio.to_thread(manager.mark_oauth, name, True)
            await asyncio.to_thread(mcp_oauth.connect, name, url)   # opens the browser
            await _send_list(send, manager, project_dir)
            return
        if action == "signin":
            name = (msg.get("name") or "").strip()
            url = msg.get("url") or _url_of(manager, name)
            if not url:
                raise mcp_mod.MCPError(f"{name!r} has no URL to sign in to")
            await asyncio.to_thread(mcp_oauth.connect, name, url)  # opens the browser
            await asyncio.to_thread(manager.mark_oauth, name, True)
            await _send_list(send, manager, project_dir)
            return
        if action == "signout":
            name = (msg.get("name") or "").strip()
            await asyncio.to_thread(mcp_oauth.disconnect, name)
            await asyncio.to_thread(manager.mark_oauth, name, False)
            await _send_list(send, manager, project_dir)
            return
        if action == "add":
            args = msg.get("args") or []
            if isinstance(args, str):
                args = [a for a in args.split() if a]
            await asyncio.to_thread(manager.add_server, msg.get("name", ""),
                                    msg.get("command", ""), msg.get("url", ""), args,
                                    _parse_env(msg.get("env")), msg.get("headers") or None)
        elif action == "remove":
            await asyncio.to_thread(manager.remove_server, (msg.get("name") or "").strip())
        elif action == "test":
            name = (msg.get("name") or "").strip()
            tools = await asyncio.to_thread(manager.list_tools, name)
            await send({"type": "mcp_tools", "server": name, "tools": tools})
            return
        await _send_list(send, manager, project_dir)
    except Exception as e:  # noqa: BLE001 - surfaces to the UI
        who = (msg.get("connector") or msg.get("name") or "").strip()
        google_auth.log_event(f"{action} {who} error {type(e).__name__}".strip())
        await send({"type": "mcp_error", "error": str(e)})
