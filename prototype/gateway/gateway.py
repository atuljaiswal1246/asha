"""Jarvis Gateway — hosted OpenAI-compatible endpoint for plan subscribers.

This is the service behind "plans like Claude Code / Codex". A signed-in user's
app points at the gateway with *their* token; the gateway holds the real
provider key, meters every request's tokens against the user's plan quota, and
proxies to the upstream model. Users never see provider keys.

Protocol (what an OpenAI-compatible client needs):
  POST /v1/chat/completions   Authorization: Bearer sk-jarvis-<token>
  GET  /v1/models             the models this plan may use
  GET  /api/me                plan + usage for the caller
  POST /admin/users           (admin key) mint a user + token
  GET  /admin/users           (admin key) list users

Run:  python gateway.py --port 8200
Env:  GATEWAY_UPSTREAM_BASE   e.g. https://opencode.ai/zen/go/v1
      GATEWAY_UPSTREAM_KEY    the provider key the gateway holds
      GATEWAY_UPSTREAM_HEADERS  optional JSON of extra headers (e.g. opencode's)
      GATEWAY_ADMIN_KEY       admin token for /admin/*
      GATEWAY_DB              store path (default prototype/data/gateway.json)
      GATEWAY_PLANS           optional JSON overriding the plan table
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

_HERE = Path(__file__).resolve().parent
_DEFAULT_DB = _HERE.parent / "data" / "gateway.json"

UPSTREAM_BASE = os.environ.get("GATEWAY_UPSTREAM_BASE", "https://opencode.ai/zen/go/v1").rstrip("/")
UPSTREAM_KEY = os.environ.get("GATEWAY_UPSTREAM_KEY", "")
try:
    UPSTREAM_HEADERS = json.loads(os.environ.get("GATEWAY_UPSTREAM_HEADERS", "{}"))
except Exception:  # noqa: BLE001
    UPSTREAM_HEADERS = {}
ADMIN_KEY = os.environ.get("GATEWAY_ADMIN_KEY", "")
DB_PATH = Path(os.environ.get("GATEWAY_DB", str(_DEFAULT_DB)))

# Plan table: monthly token allowance per plan (tune to your economics).
_DEFAULT_PLANS = {
    "free": {"label": "Free", "tokens_month": 150_000, "models": ["deepseek-v4.1-flash", "mimo-v2.5"]},
    "pro": {"label": "Pro", "tokens_month": 6_000_000, "models": ["*"]},
    "max": {"label": "Max", "tokens_month": 30_000_000, "models": ["*"]},
}
try:
    PLANS = json.loads(os.environ.get("GATEWAY_PLANS", "")) or _DEFAULT_PLANS
except Exception:  # noqa: BLE001
    PLANS = _DEFAULT_PLANS

app = FastAPI(title="Jarvis Gateway")


# ── user store ───────────────────────────────────────────────────────────────
def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


class Store:
    """JSON-backed users + usage. One lock, atomic writes. Fine for a starter;
    swap for a DB when you outgrow a single process."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._data = {"users": {}}
        self._load()

    def _load(self) -> None:
        try:
            self._data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            self._data = {"users": {}}
        self._data.setdefault("users", {})

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self._path)

    def add_user(self, plan: str, email: str = "") -> tuple[str, dict]:
        plan = plan if plan in PLANS else "free"
        token = "sk-jarvis-" + secrets.token_urlsafe(32)
        uid = secrets.token_hex(8)
        user = {"id": uid, "email": email, "plan": plan,
                "created": int(time.time()), "usage": {}}
        with self._lock:
            self._data["users"][_hash(token)] = user
            self._save()
        return token, user

    def by_token(self, token: str) -> dict | None:
        return self._data["users"].get(_hash(token))

    def used_this_period(self, user: dict) -> int:
        return int((user.get("usage") or {}).get(_period(), 0))

    def record(self, token: str, tokens: int) -> None:
        with self._lock:
            user = self._data["users"].get(_hash(token))
            if not user:
                return
            usage = user.setdefault("usage", {})
            usage[_period()] = int(usage.get(_period(), 0)) + max(0, int(tokens))
            self._save()

    def all(self) -> list[dict]:
        return list(self._data["users"].values())


STORE = Store(DB_PATH)


def _token_of(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _allowed_models(plan: str) -> list[str]:
    models = (PLANS.get(plan) or {}).get("models", ["*"])
    return models


# ── endpoints ────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "gateway": True, "upstream": UPSTREAM_BASE}


@app.get("/v1/models")
def models(request: Request) -> JSONResponse:
    user = STORE.by_token(_token_of(request))
    if not user:
        return JSONResponse({"error": {"message": "missing or invalid token", "type": "auth_error"}},
                            status_code=401)
    allowed = _allowed_models(user["plan"])
    return JSONResponse({"object": "list", "data": [{"id": m, "object": "model"} for m in allowed]})


@app.get("/api/me")
def me(request: Request) -> JSONResponse:
    user = STORE.by_token(_token_of(request))
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    cap = int((PLANS.get(user["plan"]) or {}).get("tokens_month", 0))
    used = STORE.used_this_period(user)
    return JSONResponse({"plan": user["plan"], "label": (PLANS.get(user["plan"]) or {}).get("label", user["plan"]),
                         "tokens_used": used, "tokens_cap": cap, "period": _period()})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    token = _token_of(request)
    user = STORE.by_token(token)
    if not user:
        return JSONResponse({"error": {"message": "missing or invalid token", "type": "auth_error"}},
                            status_code=401)

    cap = int((PLANS.get(user["plan"]) or {}).get("tokens_month", 0))
    if cap and STORE.used_this_period(user) >= cap:
        return JSONResponse({"error": {"message": f"Monthly quota reached for the {user['plan']} plan.",
                                       "type": "quota_exceeded"}}, status_code=429)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": {"message": "invalid JSON body"}}, status_code=400)

    allowed = _allowed_models(user["plan"])
    if allowed != ["*"] and body.get("model") not in allowed:
        return JSONResponse({"error": {"message": f"model {body.get('model')!r} is not included in your plan",
                                       "type": "model_not_allowed"}}, status_code=403)

    headers = {"Authorization": f"Bearer {UPSTREAM_KEY}", "content-type": "application/json",
               **UPSTREAM_HEADERS}
    url = f"{UPSTREAM_BASE}/chat/completions"
    streaming = bool(body.get("stream"))
    if streaming:
        body.setdefault("stream_options", {}).setdefault("include_usage", True)

    client = httpx.AsyncClient(timeout=600)

    if not streaming:
        try:
            r = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as e:
            await client.aclose()
            return JSONResponse({"error": {"message": f"upstream transport error: {e!r}"}}, status_code=502)
        await client.aclose()
        try:
            data = r.json()
        except ValueError:
            return JSONResponse({"error": {"message": "upstream returned non-JSON"}}, status_code=502)
        if r.status_code < 400:
            STORE.record(token, int((data.get("usage") or {}).get("total_tokens") or 0))
        return JSONResponse(data, status_code=r.status_code)

    async def stream():
        try:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    err = {"error": {"message": f"upstream {resp.status_code}"}}
                    yield f"data: {json.dumps(err)}\n\n".encode()
                    return
                total = 0
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        if payload and payload != "[DONE]":
                            try:
                                chunk = json.loads(payload)
                                total = int((chunk.get("usage") or {}).get("total_tokens") or total)
                            except Exception:  # noqa: BLE001
                                pass
                    yield (line + "\n\n").encode()
                if total:
                    STORE.record(token, total)
        finally:
            await client.aclose()

    return StreamingResponse(stream(), media_type="text/event-stream")


# ── admin ────────────────────────────────────────────────────────────────────
def _is_admin(request: Request) -> bool:
    if not ADMIN_KEY:
        return False
    return hmac.compare_digest(_token_of(request), ADMIN_KEY)


# ── demo provisioning (temporary, payment-free) ──────────────────────────────
_DEMO_SIGNUPS: dict[str, list[float]] = {}
_DEMO_WINDOW = 24 * 3600
_DEMO_MAX_PER_IP = int(os.environ.get("GATEWAY_DEMO_PER_IP", "5"))
_DEMO_MAX_USERS = int(os.environ.get("GATEWAY_DEMO_MAX_USERS", "500"))
_DEMO_PLAN = os.environ.get("GATEWAY_DEMO_PLAN", "free")


def _demo_allowed(ip: str) -> bool:
    now = time.time()
    stamps = [t for t in _DEMO_SIGNUPS.get(ip, []) if now - t < _DEMO_WINDOW]
    if len(stamps) >= _DEMO_MAX_PER_IP:
        _DEMO_SIGNUPS[ip] = stamps
        return False
    stamps.append(now)
    _DEMO_SIGNUPS[ip] = stamps
    return True


@app.post("/api/signup")
async def demo_signup(request: Request) -> JSONResponse:
    """Temporary stand-in for billing: mint a capped demo token, no auth.

    In production this becomes "create account + subscribe"; for the demo it is
    how a friend's freshly-installed app gets working access out of the box.
    """
    if os.environ.get("GATEWAY_DEMO_OPEN", "0") != "1":
        return JSONResponse({"error": "sign-up is closed"}, status_code=403)
    if len(STORE.all()) >= _DEMO_MAX_USERS:
        return JSONResponse({"error": "demo capacity reached"}, status_code=429)
    ip = request.client.host if request.client else "?"
    if not _demo_allowed(ip):
        return JSONResponse({"error": "too many sign-ups from this address"}, status_code=429)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    token, user = STORE.add_user(_DEMO_PLAN, body.get("email", ""))
    plan = PLANS.get(_DEMO_PLAN) or {}
    return JSONResponse({"token": token, "plan": _DEMO_PLAN,
                         "label": plan.get("label", _DEMO_PLAN),
                         "tokens_cap": plan.get("tokens_month", 0)})


@app.post("/admin/users")
async def admin_add_user(request: Request) -> JSONResponse:
    if not _is_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.json()
    token, user = STORE.add_user(body.get("plan", "free"), body.get("email", ""))
    return JSONResponse({"token": token, "user": user})


@app.get("/admin/users")
def admin_list(request: Request) -> JSONResponse:
    if not _is_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse({"users": STORE.all(), "plans": PLANS})


def main() -> None:
    ap = argparse.ArgumentParser(description="Jarvis Gateway")
    ap.add_argument("--host", default=os.environ.get("GATEWAY_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("GATEWAY_PORT", "8200")))
    ap.add_argument("--add-user", metavar="PLAN", help="mint a user token and exit")
    ap.add_argument("--email", default="")
    args = ap.parse_args()

    if args.add_user:
        token, user = STORE.add_user(args.add_user, args.email)
        print(json.dumps({"token": token, "user": user}, indent=2))
        return

    if not UPSTREAM_KEY:
        print("[gateway] warning: GATEWAY_UPSTREAM_KEY is not set — upstream calls will fail")
    import uvicorn
    print(f"[gateway] on http://{args.host}:{args.port}  upstream={UPSTREAM_BASE}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
