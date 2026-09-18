"""Plugin + hook surface (Hermes-style): extend Jarvis without touching core.

Plugins are ``*.py`` files in ``prototype/data/plugins/`` (gitignored runtime
data). Each may define ``register(api)`` and use it to add tools or subscribe
to events::

    def register(api):
        def shout(args):
            return (args.get("text") or "").upper()
        api.register_tool(
            name="shout",
            description="Uppercase the given text.",
            parameters={"type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"]},
            handler=shout,
        )
        api.on("post_tool", lambda **kw: None)

Events emitted by the agent loop:
  - ``pre_tool``  (name, args)      -> return a string to BLOCK/short-circuit
  - ``post_tool`` (name, args, result)
  - ``pre_llm``   (messages, model)
  - ``post_llm``  (model, response)

Everything is fail-open: a broken plugin is skipped, never breaks the agent.
No plugins loaded => zero overhead.
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

logger = logging.getLogger("jarvis.hooks")

_LOADED = False
_TOOLS: dict[str, dict] = {}          # name -> {"schema", "handler"}
_HANDLERS: dict[str, list] = {}       # event -> [callable]


def _schema(name: str, description: str, parameters: dict) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description, "parameters": parameters}}


class PluginAPI:
    """The object passed to each plugin's ``register(api)``."""

    def register_tool(self, name: str, description: str, parameters: dict,
                      handler) -> None:
        if not name or not callable(handler):
            raise ValueError("register_tool needs a name and a callable handler")
        _TOOLS[name] = {
            "schema": _schema(name, description or "", parameters or
                              {"type": "object", "properties": {}}),
            "handler": handler,
        }

    def on(self, event: str, fn) -> None:
        if callable(fn):
            _HANDLERS.setdefault(event, []).append(fn)


def load_plugins(directory: str | Path) -> int:
    """Import every ``*.py`` in *directory* once and call ``register``. Returns
    the number of plugins loaded. Never raises."""
    global _LOADED
    if _LOADED:
        return 0
    _LOADED = True
    d = Path(directory)
    if not d.is_dir():
        return 0
    n = 0
    for f in sorted(d.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"jarvis_plugin_{f.stem}", f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            register = getattr(mod, "register", None)
            if callable(register):
                register(PluginAPI())
                n += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[hooks] plugin {f.name} failed: {e!r}")
    if n:
        logger.info(f"[hooks] loaded {n} plugin(s)")
    return n


def extra_tools() -> list[dict]:
    return [t["schema"] for t in _TOOLS.values()]


def tool_handlers() -> dict:
    return {name: t["handler"] for name, t in _TOOLS.items()}


def emit(event: str, **kwargs):
    """Notify subscribers. For ``pre_tool``, a non-empty string return blocks
    the call (the string becomes the tool result). Fail-open."""
    blocking = None
    for fn in _HANDLERS.get(event, []):
        try:
            out = fn(**kwargs)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[hooks] {event} handler failed: {e!r}")
            continue
        if event == "pre_tool" and isinstance(out, str) and out.strip():
            blocking = out
    return blocking


def reset() -> None:
    """Clear loaded state (tests)."""
    global _LOADED
    _LOADED = False
    _TOOLS.clear()
    _HANDLERS.clear()
