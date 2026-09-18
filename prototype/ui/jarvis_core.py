"""Jarvis core — headless agent service (HTTP + WebSocket). No pipecat, no voice.

This is the single engine behind BOTH delivery paths:
  * the desktop app (bundled, runs locally), and
  * a self-hosted / BYOC server (run it on your VPS; clients connect remotely).

It exposes the coding agent over a tiny JSON protocol so any client (the web UI
here, the desktop shell, a bot, curl) can drive it:

  WS  /ws   client -> {"type":"task","request":"...","project":"/path",
                       "provider":"opencode-go","model":"deepseek-v4.1-flash"}
            client -> {"type":"cancel"}
            server -> {"type":"status","state":"running|idle"}
            server -> {"type":"step","name":"write_file","args":{...}}
            server -> {"type":"token","text":"..."}
            server -> {"type":"done","text":"...","files":[...],"steps":N,"usage":{}}
            server -> {"type":"error","message":"..."}

  GET /api/health     -> {"ok": true, "voice": false}
  GET /api/providers  -> configured providers (BYOK)
  GET /               -> a minimal built-in UI

Run:  python jarvis_core.py [--host 127.0.0.1] [--port 8100]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import threading
from pathlib import Path

from dotenv import load_dotenv, set_key
from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

_UI_DIR = Path(__file__).resolve().parent
_ENV_FILE = _UI_DIR.parent / ".env"
load_dotenv(_ENV_FILE, override=False)

import agent_loop  # noqa: E402
import providers as providers_pkg  # noqa: E402

app = FastAPI(title="Jarvis core")
app.mount("/static", StaticFiles(directory=str(_UI_DIR / "static")), name="static")

_ALLOWED_KEY_ENV = {
    "OPENCODE_API_KEY", "SUPERVISOR_API_KEY", "OPENROUTER_API_KEY",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
    "GROQ_API_KEY", "XAI_API_KEY",
}

_DEFAULT_PROJECT = os.environ.get("JARVIS_PROJECT", str(_UI_DIR.parent.parent))
_DEFAULT_MODEL = os.environ.get("JARVIS_MODEL", "deepseek-v4.1-flash")
_DEFAULT_PROVIDER = os.environ.get("JARVIS_PROVIDER", "opencode-go")


def resolve_route(model: str = "", provider: str = "") -> tuple[str, str]:
    """(provider_id, model) from a selection. Composite 'provider|model' (the
    BYOK picker id) wins; otherwise an explicit provider; else the defaults."""
    model = (model or _DEFAULT_MODEL).strip()
    if "|" in model:
        pid, model = model.split("|", 1)
        return pid.strip(), model.strip()
    return (provider or _DEFAULT_PROVIDER).strip(), model


def _models() -> list[dict]:
    """Opencode (zen+go) + every configured BYOK provider, as picker rows."""
    out: list[dict] = []
    seen: set[str] = set()
    for pid, group in (("opencode", "OpenCode Zen"), ("opencode-go", "OpenCode Go")):
        for m in providers_pkg.list_models(pid):
            mid = m.get("id")
            if mid and mid not in seen:
                seen.add(mid)
                out.append({"id": mid, "name": m.get("name") or mid, "group": group})
    for p in providers_pkg.list_providers():
        pid = p.get("provider_id", "")
        if pid in ("opencode", "opencode-go", "local") or not p.get("configured"):
            continue
        for m in (p.get("models") or []):
            mid = m.get("id") if isinstance(m, dict) else getattr(m, "id", "")
            cid = f"{pid}|{mid}"
            if mid and cid not in seen:
                seen.add(cid)
                out.append({"id": cid, "name": (m.get("name") or mid), "group": p.get("name") or pid})
    return out


@app.get("/api/health")
def health() -> dict:
    import jarvis_access
    return {"ok": True, "voice": False, "models": len(_models()),
            "access": "token" if jarvis_access.token_present() else "none"}


@app.post("/api/ensure-access")
def ensure_access_ep() -> JSONResponse:
    """First-run demo provisioning (capped free token) — no payment needed."""
    import jarvis_access
    return JSONResponse(jarvis_access.ensure_access())


@app.get("/api/providers")
def get_providers():
    return JSONResponse(providers_pkg.list_providers())


@app.get("/api/models")
def get_models():
    return JSONResponse(_models())


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    cancel = threading.Event()
    busy = {"task": None}

    def emit(obj: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, obj)

    async def drain() -> None:
        while True:
            obj = await queue.get()
            if obj is None:
                return
            await websocket.send_json(obj)

    drain_task = asyncio.create_task(drain())

    async def run_task(msg: dict) -> None:
        provider, model = resolve_route(msg.get("model", ""), msg.get("provider", ""))
        project = (msg.get("project") or _DEFAULT_PROJECT)
        emit({"type": "status", "state": "running", "provider": provider, "model": model})
        try:
            out = await asyncio.to_thread(
                agent_loop.run_task, msg.get("request", ""), project,
                provider_id=provider, model=model,
                on_step=lambda name, args: emit({"type": "step", "name": name, "args": args}),
                on_token=lambda t: emit({"type": "token", "text": t}),
                cancel=cancel, read_only=bool(msg.get("read_only")))
            emit({"type": "done", "text": out.get("text", ""), "files": out.get("files", []),
                  "steps": out.get("steps"), "usage": out.get("usage")})
        except Exception as e:  # noqa: BLE001
            emit({"type": "error", "message": repr(e)})
        finally:
            emit({"type": "status", "state": "idle"})
            busy["task"] = None

    try:
        while True:
            msg = await websocket.receive_json()
            kind = msg.get("type")
            if kind == "task":
                if busy["task"] and not busy["task"].done():
                    emit({"type": "error", "message": "A task is already running."})
                    continue
                cancel.clear()
                busy["task"] = asyncio.create_task(run_task(msg))
            elif kind == "cancel":
                cancel.set()
    except WebSocketDisconnect:
        pass
    finally:
        cancel.set()
        await queue.put(None)
        try:
            await drain_task
        except Exception:  # noqa: BLE001
            pass


_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Jarvis</title>
<style>body{background:#0a0c10;color:#cfe3ff;font:14px/1.5 ui-monospace,Menlo,monospace;margin:0}
#log{padding:16px;white-space:pre-wrap}#in{display:flex;gap:8px;padding:12px;border-top:1px solid #1c2530}
input{flex:1;background:#111721;color:#cfe3ff;border:1px solid #24303d;border-radius:8px;padding:10px}
button{background:#0a84ff;color:#fff;border:0;border-radius:8px;padding:10px 16px;cursor:pointer}
.step{color:#6f8bb0}.tok{color:#9ad0ff}#st{color:#7ee787}</style></head><body>
<div id="log"><b>Jarvis core</b>\n<span id="st">connecting…</span></div>
<div id="in"><input id="t" placeholder="Ask Jarvis to build something…" autofocus>
<button onclick="send()">Run</button></div>
<script>
let ws,proj=null;const log=document.getElementById('log'),st=document.getElementById('st');
function line(s,cls){const d=document.createElement('div');if(cls)d.className=cls;d.textContent=s;log.appendChild(d);window.scrollTo(0,document.body.scrollHeight);}
function connect(){ws=new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/ws');
 ws.onopen=()=>st.textContent='ready';ws.onclose=()=>{st.textContent='disconnected';setTimeout(connect,1500);};
 ws.onmessage=e=>{const m=JSON.parse(e.data);
  if(m.type==='step')line('→ '+m.name,'step');
  else if(m.type==='token')line(m.text,'tok');
  else if(m.type==='done')line('\\n'+m.text+'\\n ['+(m.steps||'?')+' steps]');
  else if(m.type==='error')line('⚠ '+m.message);
  else if(m.type==='status')st.textContent=m.state;};}
function send(){const t=document.getElementById('t');if(!t.value.trim())return;line('\\n> '+t.value);
 ws.send(JSON.stringify({type:'task',request:t.value}));t.value='';}
document.getElementById('t').addEventListener('keydown',e=>{if(e.key==='Enter')send();});
connect();
</script></body></html>"""


@app.post("/api/key")
def save_key(body: dict = Body(...)) -> JSONResponse:
    """Persist a provider API key to .env (gitignored) and reload providers."""
    env = (body.get("env") or "").strip()
    value = (body.get("value") or "").strip()
    if env not in _ALLOWED_KEY_ENV:
        return JSONResponse({"error": f"key not allowlisted: {env}"}, status_code=403)
    set_key(str(_ENV_FILE), env, value, quote_mode="never" if value else "always")
    os.environ[env] = value
    providers_pkg.reset_cache()
    return JSONResponse({"ok": True, "env": env, "set": bool(value)})


@app.post("/api/providers/custom")
def add_custom(body: dict = Body(...)) -> JSONResponse:
    models = body.get("models")
    if isinstance(models, str):
        models = [m.strip() for m in models.replace(",", "\n").splitlines() if m.strip()]
    try:
        entry = providers_pkg.custom_providers().add(
            body.get("name", ""), body.get("base_url", ""),
            body.get("api_key", ""), models or [])
        providers_pkg.reset_cache()
        return JSONResponse(entry)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=400)


@app.delete("/api/providers/custom/{cid}")
def del_custom(cid: str) -> JSONResponse:
    ok = providers_pkg.custom_providers().remove(cid)
    providers_pkg.reset_cache()
    return JSONResponse({"ok": ok})


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    page = _UI_DIR / "static" / "core.html"
    if page.is_file():
        return page.read_text(encoding="utf-8")
    return _PAGE


def main() -> None:
    ap = argparse.ArgumentParser(description="Jarvis core (headless agent service)")
    ap.add_argument("--host", default=os.environ.get("JARVIS_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("JARVIS_CORE_PORT", "8100")))
    args = ap.parse_args()
    import jarvis_access
    print(f"[core] access: {jarvis_access.ensure_access().get('status')}")
    import uvicorn
    print(f"[core] Jarvis core on http://{args.host}:{args.port}  (voice: off)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
