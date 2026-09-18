"""Projects screen backend — the WS actions behind the in-app board.

Mirrors :mod:`mcp_screen`: it lives outside the voice pipeline so the screen's
behaviour can be tested (and reused) without booting audio. Every action
returns the *full* payload after the change, so the UI never has to stitch
state together:

    {"type": "board_state",
     "projects": [{id, name, repo, counts, total}],
     "project":  {id, name, repo, counts, total} | None,
     "columns":  [{name, count, cards}],
     "cards":    [canonical table rows across all projects],
     "render":   "<canonical render_table text>",
     "statuses": [...], "priorities": [...]}

Messages in  (UI -> server): {"type": "mcp", "screen": "projects",
                              "action": "projects_list|project_add|...", ...}
Messages out (server -> UI): {"type": "board_state", ...}
                             {"type": "board_error", "error": ...}

Writes go through the brain's own path (``writer=True``); the store is the
enforcement point, not this module's convention. Disk-touching calls run on a
worker thread so the event loop never blocks.
"""
from __future__ import annotations

import asyncio

import board as board_mod


def _payload(store, project_key: str = "") -> dict:
    """The full post-change payload (sync; call via ``asyncio.to_thread``)."""
    projects = store.projects()
    selected = None
    if project_key:
        pid = store.resolve_project(project_key)
        selected = store.get_project(pid) if pid else None
    if selected is None and projects:
        selected = projects[0]
    columns = store.columns(selected["id"]) if selected else []
    rows = store.table_rows()
    payload = {
        "type": "board_state",
        "projects": projects,
        "project": selected,
        "columns": columns,
        "cards": rows,
        "render": board_mod.render_table(rows),
        "statuses": list(board_mod.STATUSES),
        "priorities": list(board_mod.PRIORITIES),
    }
    if store.recovery_notice:
        payload["notice"] = store.recovery_notice
    return payload


def _need_project(store, key: str) -> str:
    pid = store.resolve_project(key)
    if pid is None:
        raise ValueError(f"unknown project: {key!r}")
    return pid


def _card_project(store, card_id: str) -> str:
    card = store.get_card(card_id)
    if card is None:
        raise ValueError(f"unknown card: {card_id!r}")
    return card.get("project_id") or ""


async def handle(msg: dict, send, store=None) -> None:
    """Run one Projects-screen message. ``send`` awaits a dict payload."""
    store = store if store is not None else board_mod.board
    action = (msg.get("action") or "projects_list").strip().lower()
    selected_key = (msg.get("project") or "").strip()
    try:
        if action == "projects_list":
            await send(await asyncio.to_thread(_payload, store, selected_key))
            return
        if action == "board_get":
            await send(await asyncio.to_thread(_payload, store, selected_key))
            return
        if action == "project_add":
            def _add():
                p = store.add_project(msg.get("name", ""),
                                      note=msg.get("note", ""),
                                      repo=msg.get("repo", ""), writer=True)
                return _payload(store, p["id"])
            await send(await asyncio.to_thread(_add))
            return
        if action == "project_update":
            def _update():
                pid = _need_project(store, selected_key)
                fields = {k: msg[k] for k in ("name", "note", "repo")
                          if k in msg and msg[k] is not None}
                p = store.update_project(pid, writer=True, **fields)
                return _payload(store, (p or {}).get("id", pid))
            await send(await asyncio.to_thread(_update))
            return
        if action == "project_remove":
            def _remove():
                pid = _need_project(store, selected_key)
                if not store.remove_project(
                        pid, delete_cards=bool(msg.get("delete_cards", True)),
                        writer=True):
                    raise ValueError(f"unknown project: {selected_key!r}")
                return _payload(store, "")
            await send(await asyncio.to_thread(_remove))
            return
        if action == "card_add":
            def _add_card():
                pid = _need_project(store, selected_key)
                title = (msg.get("title") or "").strip()
                if not title:
                    raise ValueError("card_add needs a title")
                fields = {k: msg[k] for k in
                          ("area", "owner", "priority", "needs", "notes", "status")
                          if k in msg and msg[k] is not None}
                store.add_card(pid, title, writer=True, **fields)
                return _payload(store, pid)
            await send(await asyncio.to_thread(_add_card))
            return
        if action == "card_update":
            def _update_card():
                pid = _card_project(store, msg.get("card_id", ""))
                fields = {k: msg[k] for k in
                          ("title", "area", "owner", "priority", "needs",
                           "notes", "status")
                          if k in msg and msg[k] is not None}
                if store.update_card(msg.get("card_id", ""),
                                     writer=True, **fields) is None:
                    raise ValueError(f"unknown card: {msg.get('card_id')!r}")
                return _payload(store, pid)
            await send(await asyncio.to_thread(_update_card))
            return
        if action == "card_move":
            def _move():
                card_id = msg.get("card_id", "")
                pid = _card_project(store, card_id)
                if store.move_card(card_id, msg.get("status", ""),
                                   writer=True) is None:
                    raise ValueError(
                        f"unknown card or status: {card_id!r} / "
                        f"{msg.get('status')!r}")
                return _payload(store, pid)
            await send(await asyncio.to_thread(_move))
            return
        if action == "card_remove":
            def _remove_card():
                card_id = msg.get("card_id", "")
                pid = _card_project(store, card_id)
                if not store.remove_card(card_id, writer=True):
                    raise ValueError(f"unknown card: {card_id!r}")
                return _payload(store, pid)
            await send(await asyncio.to_thread(_remove_card))
            return
        await send({"type": "board_error",
                    "error": f"unknown projects action: {action!r}"})
    except PermissionError as e:
        await send({"type": "board_error", "error": str(e)})
    except Exception as e:  # noqa: BLE001 - surfaces to the UI
        await send({"type": "board_error", "error": str(e)})
